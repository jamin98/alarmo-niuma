"""和 Alarmo 固件之间的 USB 串口协议。

主机 -> 设备（小端）:
    A5 5A | cmd | flags | r0 r1 c0 c1 (各 u16) | len (u32) | payload
    cmd 1 = 画矩形。r/c 是面板原生坐标（240 列 x 320 行，竖屏）:
            row = 319 - x, col = y。像素顺序：row 递增为外层，col 递增为内层，
            每像素 3 字节 B,G,R（和官方 payload 的 imgconv.py 一致）。
            flags bit0 = 1 时 payload 为 RLE：[n-1][B][G][R] 重复。
    cmd 2 = 背光，r0 = 0..1000
    cmd 3 = 请求设备立刻上报一次按键状态
设备 -> 主机:
    A5 5A 81 | buttons | dial_lo dial_hi (0.1 度) | seq | xor
"""
import glob
import struct
import sys
import threading
import time

import numpy as np

W, H = 320, 240
TILE = 40
MAGIC = b"\xA5\x5A"
CMD_RECT, CMD_BRIGHT, CMD_HELLO = 1, 2, 3
BTN_NOTIFY, BTN_BACK, BTN_DIAL = 1, 2, 4


def rle_encode(px):
    """px: (N,3) uint8 -> RLE 字节串。"""
    v = (px[:, 0].astype(np.uint32) << 16) | (px[:, 1].astype(np.uint32) << 8) | px[:, 2]
    starts = np.concatenate(([0], np.flatnonzero(v[1:] != v[:-1]) + 1))
    lens = np.diff(np.concatenate((starts, [len(v)])))
    pieces = (lens + 255) // 256
    idx = np.repeat(np.arange(len(starts)), pieces)
    first = np.concatenate(([0], np.cumsum(pieces)[:-1]))
    ordinal = np.arange(len(idx)) - first[idx]
    plen = np.where(ordinal == pieces[idx] - 1, lens[idx] - 256 * (pieces[idx] - 1), 256)
    out = np.empty((len(idx), 4), np.uint8)
    out[:, 0] = plen - 1
    out[:, 1:] = px[starts[idx]]
    return out.tobytes()


def encode_rect(frame, x, y, w, h):
    """frame: (240,320,3) RGB uint8。返回一个完整的画矩形数据包。"""
    assert (w * h) % 2 == 0, "像素数必须是偶数"
    tile = frame[y:y + h, x:x + w]
    px = np.ascontiguousarray(tile[:, ::-1].transpose(1, 0, 2)[:, :, ::-1]).reshape(-1, 3)
    raw = px.tobytes()
    rle = rle_encode(px)
    flags, payload = (1, rle) if len(rle) < len(raw) else (0, raw)
    head = MAGIC + struct.pack("<BBHHHHI", CMD_RECT, flags, W - (x + w), W - x - 1, y, y + h - 1, len(payload))
    return head + payload


def encode_full(frame):
    return [encode_rect(frame, 0, 0, W, H)]


def encode_diff(prev, cur):
    """只发变化了的 40x40 小块，同一行相邻的小块合并。"""
    if prev is None:
        return encode_full(cur)
    changed = (prev != cur).any(axis=2).reshape(H // TILE, TILE, W // TILE, TILE).any(axis=(1, 3))
    packets = []
    for ty in range(H // TILE):
        tx = 0
        while tx < W // TILE:
            if not changed[ty, tx]:
                tx += 1
                continue
            x0 = tx
            while tx < W // TILE and changed[ty, tx]:
                tx += 1
            packets.append(encode_rect(cur, x0 * TILE, ty * TILE, (tx - x0) * TILE, TILE))
    return packets


def encode_brightness(level):
    return MAGIC + struct.pack("<BBHHHHI", CMD_BRIGHT, 0, int(max(0.0, min(1.0, level)) * 1000), 0, 0, 0, 0)


def encode_hello():
    return MAGIC + struct.pack("<BBHHHHI", CMD_HELLO, 0, 0, 0, 0, 0, 0)


class EventParser:
    """解析设备上报的 8 字节事件包，容忍中间夹杂垃圾字节。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(MAGIC)
            if i < 0:
                del self.buf[:-1]
                break
            del self.buf[:i]
            if len(self.buf) < 8:
                break
            pkt = bytes(self.buf[:8])
            x = 0
            for b in pkt[:7]:
                x ^= b
            if pkt[2] == 0x81 and x == pkt[7]:
                out.append((pkt[3], pkt[4] | (pkt[5] << 8), pkt[6]))
                del self.buf[:8]
            else:
                del self.buf[:1]
        return out


USB_VID, USB_PID = 0x1209, 0x0001          # 固件的 USB 描述符（firmware/source/usbd_desc.c）


def alarmo_ports():
    """按 USB 的 VID:PID / 厂商名找 Alarmo 的串口。Linux(含树莓派)、Windows、macOS 通用。"""
    from serial.tools import list_ports
    hits = []
    for p in list_ports.comports():
        text = " ".join(str(x) for x in (p.manufacturer, p.product, p.description) if x)
        if (p.vid, p.pid) == (USB_VID, USB_PID) or "NiumaOffice" in text or "Alarmo" in text:
            hits.append(p.device)
    return sorted(hits)


def find_port(spec):
    """spec 可以是:
        ""/"auto"             自动按 VID:PID 找（推荐，所有系统都能用）
        "COM5"                Windows 串口名
        "/dev/ttyACM0"        Linux / 树莓派 / macOS 设备名
        "/dev/serial/by-id/usb-NiumaOffice_Alarmo*"   带通配符，取第一个
    """
    spec = (spec or "").strip()
    if spec.lower() in ("", "auto"):
        try:
            hits = alarmo_ports()
        except ImportError:
            return None
        return hits[0] if hits else None
    if not any(ch in spec for ch in "*?["):
        return spec
    hits = sorted(glob.glob(spec))
    return hits[0] if hits else None


def no_port_hint(spec):
    if sys.platform == "win32":
        return "找不到 Alarmo 的串口（设备管理器 > 端口 里应该有一个 USB 串行设备；装完固件后才会出现）"
    return "找不到 Alarmo 的串口 " + (spec or "auto") + "（装完固件才会出现；Linux 下确认用户在 dialout 组）"


class AlarmoLink(threading.Thread):
    """后台线程：把最新一帧推给设备，把按键事件回调出去。

    on_button(mask) 在按键按下的那一刻调用；on_dial(delta_deg) 在滚轮转动时调用；
    on_online(bool) 在连接状态变化时调用。回调都在本线程里执行。
    """

    def __init__(self, port, on_button, on_dial, on_online, brightness=1.0):
        super().__init__(daemon=True)
        self.port_pattern = port
        self.on_button, self.on_dial, self.on_online = on_button, on_dial, on_online
        self.brightness = brightness
        self._latest = None
        self._cv = threading.Condition()
        self.stats = {"fps": 0.0, "kbps": 0.0, "port": None, "error": None}

    def submit(self, frame):
        with self._cv:
            self._latest = frame
            self._cv.notify()

    def run(self):
        import serial  # 延迟导入：没装 pyserial 也能跑模拟器
        while True:
            port = find_port(self.port_pattern)
            if not port:
                self.stats["error"] = no_port_hint(self.port_pattern)
                time.sleep(2)
                continue
            try:
                with serial.Serial(port, 115200, timeout=0, write_timeout=3) as ser:
                    self.stats.update(port=port, error=None)
                    self.on_online(True)
                    self._session(ser)
            except Exception as e:  # 拔线、断电都走这里
                self.stats["error"] = f"{type(e).__name__}: {e}"
            self.on_online(False)
            time.sleep(2)

    def _session(self, ser):
        parser = EventParser()
        prev = None
        last_buttons, last_dial = 0, None
        sent_bytes, sent_frames, t_stat = 0, 0, time.time()
        ser.write(encode_brightness(self.brightness) + encode_hello())
        while True:
            with self._cv:
                self._cv.wait(timeout=0.05)
                frame, self._latest = self._latest, None
            data = ser.read(4096)
            if data:
                for buttons, dial, _seq in parser.feed(data):
                    pressed = buttons & ~last_buttons
                    last_buttons = buttons
                    if pressed:
                        self.on_button(pressed)
                    if last_dial is not None:
                        delta = (dial - last_dial + 1800) % 3600 - 1800
                        if abs(delta) >= 30:
                            self.on_dial(delta / 10.0)
                            last_dial = dial
                    else:
                        last_dial = dial
            if frame is None:
                continue
            for pkt in encode_diff(prev, frame):
                ser.write(pkt)
                sent_bytes += len(pkt)
            ser.flush()
            prev = frame
            sent_frames += 1
            now = time.time()
            if now - t_stat >= 2:
                self.stats.update(fps=round(sent_frames / (now - t_stat), 1),
                                  kbps=round(sent_bytes / 1024 / (now - t_stat), 1))
                sent_bytes, sent_frames, t_stat = 0, 0, now
