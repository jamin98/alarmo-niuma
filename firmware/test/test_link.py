#!/usr/bin/env python3
"""用伪终端假装一台 Alarmo，验证 hub 的串口线程：
画面经 固件解析器 还原后要和最后一帧一致；设备上报的按键/滚轮要正确回调。
运行: python3 test_link.py   （需要 gcc、pyserial，仅 Linux）"""
import os
import pty
import subprocess
import sys
import tempfile
import time
import tty
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "hub"))
import device  # noqa: E402
from render import Renderer  # noqa: E402
from state import Office  # noqa: E402
from test_proto import panel_to_screen  # noqa: E402


def report(buttons, dial, seq):
    p = bytearray([0xA5, 0x5A, 0x81, buttons, dial & 0xFF, dial >> 8, seq])
    x = 0
    for b in p:
        x ^= b
    return bytes(p + bytes([x]))


def main():
    tmp = Path(tempfile.mkdtemp())
    exe = str(tmp / "host_test")
    subprocess.run(["gcc", "-O1", "-o", exe, str(HERE / "host_test.c"), str(HERE.parent / "source" / "proto.c")], check=True)

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    os.set_blocking(master, False)
    events = []
    link = device.AlarmoLink(os.ttyname(slave), on_button=lambda m: events.append(("btn", m)),
                             on_dial=lambda d: events.append(("dial", d)),
                             on_online=lambda ok: events.append(("online", ok)), brightness=0.5)
    link.start()

    R = Renderer()
    T = 1e6
    o = Office(group="串口测试")
    o.set_bots(["架构牛", "后端马", "前端小牛", "测试老马"], T)
    o.bridge_seen = T + 1e5
    o.set_status("架构牛", "typing", T)
    received = bytearray()

    def pump():
        try:
            while True:
                received.extend(os.read(master, 65536))
        except (BlockingIOError, OSError):
            pass

    last = None
    for i in range(40):
        if i == 20:
            o.whip(T + 2.0)
        last = np.asarray(R.render(o, T + i * 0.1))
        link.submit(last)
        t_end = time.time() + 0.1
        while time.time() < t_end:
            pump()
            time.sleep(0.005)
        if i == 5:
            os.write(master, b"\x00\xff" + report(0, 900, 0))          # 前面带垃圾字节
        if i == 10:
            os.write(master, report(device.BTN_DIAL, 900, 1))           # 按下大按钮
        if i == 12:
            os.write(master, report(device.BTN_DIAL, 900, 2))           # 按住不放：不应重复触发
        if i == 14:
            os.write(master, report(0, 1100, 3)[:3])                    # 一个包拆成两半到达
        if i == 15:
            os.write(master, report(0, 1100, 3)[3:])                    # 松开 + 滚轮转 20 度
        if i == 18:
            os.write(master, report(device.BTN_BACK | device.BTN_NOTIFY, 3500, 4))  # 跨 0 度：-120 度
    for _ in range(100):
        pump()
        time.sleep(0.01)

    out = subprocess.run([exe, "64"], input=bytes(received), capture_output=True, check=True)
    info = out.stderr.decode().strip()
    same = np.array_equal(panel_to_screen(out.stdout), last)
    print(f"收到 {len(received)} 字节, 像素一致={same}, {info}")
    print("回调:", events)
    want = [("online", True), ("btn", device.BTN_DIAL), ("dial", 20.0),
            ("btn", device.BTN_BACK | device.BTN_NOTIFY), ("dial", -120.0)]
    ok = same and "errors=0 bad=0 brightness=500 hellos=1" in info and events == want
    print("OK" if ok else "FAIL")
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
