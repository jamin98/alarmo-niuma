#!/usr/bin/env python3
"""牛马办公室 - Windows 桥接。

Grok Bot 没有开放 API，所以这里用 Windows 的 UI 自动化直接操作桌面端：
  读：定时扫一遍窗口里的文字，找出新消息和每个 bot 的状态，上报给 hub
  写：hub 说要发消息时，把窗口切到前台，按官方快捷键切到群聊、粘贴、回车
      私聊（点名）时先切到那个 bot 的单聊，确认标题对上了再发，发完切回原来的聊天

用法:
    pip install uiautomation
    python bridge.py                 正常运行
    python bridge.py --probe         把窗口的控件树导出到 uia_dump.txt（调试/校准用）
    python bridge.py --send "测试"    只测试往群里发一句话
    python bridge.py --send-private 名字 "测试"   只测试私聊某个 bot 一句话（发完切回原来的聊天）
    python bridge.py --voice-test [名字] [--wav 录音.wav]
                                     只测试"按住说话"：把录音（默认一段 2 秒的小声提示音）播放进虚拟麦克风，
                                     让 Grok 听写，然后取消、清掉转出来的字（不发送）；不写名字 = 群聊
    python bridge.py --dry-run       只打印解析结果，不上报
    python bridge.py --questions     只打印界面上认出来的提问卡片（bot 问你、让你选的那种），校准用

Python 3.11+ 自带 tomllib，更低版本请 pip install tomli。电脑锁屏、或远程桌面断开时，Windows 不允许模拟按键，发消息会失败。
"""
import argparse
import hashlib
import json
import re
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent


# ---------------------------------------------------------------- hub 通信
class HubClient:
    def __init__(self, base, token=""):
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, method, path, body=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Token", self.token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def events(self, evs):
        if evs:
            self._req("POST", "/api/events", evs)

    def commands(self, wait):
        return self._req("GET", f"/api/commands?wait={wait}", timeout=wait + 10).get("commands", [])

    def result(self, cid, ok, error=""):
        self._req("POST", "/api/result", {"id": cid, "ok": ok, "error": error})

    def clip(self, cid):
        """取一段录音（WAV 字节）。"""
        req = urllib.request.Request(self.base + f"/api/voice/audio?id={cid}")
        if self.token:
            req.add_header("X-Token", self.token)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()


# ---------------------------------------------------------------- 往虚拟麦克风里放录音（Windows 自带的 waveOut，不用装库）
def wave_out_devices():
    """[(编号, 名字)]。名字最长 31 个字，如 "CABLE Input (VB-Audio Virtual C"。"""
    import ctypes
    from ctypes import wintypes

    class CAPS(ctypes.Structure):
        _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                    ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD)]
    winmm = ctypes.windll.winmm
    out = []
    for i in range(winmm.waveOutGetNumDevs()):
        caps = CAPS()
        if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            out.append((i, caps.szPname))
    return out


def find_wave_out(name):
    for i, n in wave_out_devices():
        if name.lower() in n.lower():
            return i
    return None


def play_wav(dev, wav):
    """把 WAV（16 位 PCM）从编号 dev 的播放设备放出来，放完才返回。dev = -1 用系统默认设备。"""
    import ctypes
    import io
    import wave
    from ctypes import wintypes
    with wave.open(io.BytesIO(wav)) as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        pcm = w.readframes(w.getnframes())
    if width != 2:
        raise RuntimeError("录音要 16 位 PCM")

    class FMT(ctypes.Structure):
        _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                    ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                    ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]

    class HDR(ctypes.Structure):
        _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD), ("dwBytesRecorded", wintypes.DWORD),
                    ("dwUser", ctypes.c_size_t), ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                    ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]
    winmm = ctypes.windll.winmm
    winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT, ctypes.POINTER(FMT),
                                  ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    for f in ("waveOutPrepareHeader", "waveOutWrite", "waveOutUnprepareHeader"):
        getattr(winmm, f).argtypes = [ctypes.c_void_p, ctypes.POINTER(HDR), wintypes.UINT]
    winmm.waveOutReset.argtypes = winmm.waveOutClose.argtypes = [ctypes.c_void_p]
    fmt = FMT(1, ch, rate, rate * ch * 2, ch * 2, 16, 0)
    h = ctypes.c_void_p()
    r = winmm.waveOutOpen(ctypes.byref(h), dev & 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0)
    if r != 0:
        raise RuntimeError(f"打不开播放设备(错误 {r})")
    buf = ctypes.create_string_buffer(pcm, len(pcm))
    hdr = HDR(ctypes.cast(buf, ctypes.c_void_p), len(pcm), 0, 0, 0, 0, None, 0)
    try:
        winmm.waveOutPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        winmm.waveOutWrite(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        end = time.time() + len(pcm) / fmt.nAvgBytesPerSec + 5
        while not hdr.dwFlags & 1 and time.time() < end:       # WHDR_DONE
            time.sleep(0.05)
    finally:
        winmm.waveOutReset(h)
        winmm.waveOutUnprepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        winmm.waveOutClose(h)


def beep_wav(seconds=2.0, rate=16000):
    """测试用：一段小声的提示音。"""
    import io
    import math
    import struct
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * 440 * k / rate)))
                               for k in range(int(seconds * rate))))
    return out.getvalue()


# ---------------------------------------------------------------- 解析（纯函数，可在任何系统上测试）
# Grok Bot 桌面端是 Electron，界面文字都带在无障碍树里（--probe 可导出）。已确认的结构：
#   侧边栏  ListControl  class 含 sand-agents-list__list
#     └ ListItem → Button(name="<对话名>, 工作中|需要关注|未读动态")，第一个 Text 是对话名
#   聊天头  Text  AutomationId=sand-conversation-heading   未命名群聊时 = "成员1、成员2、…"
#   记录    class 含 sand-transcript-row 的 Group，Name = "<发言人> HH:MM:SS"（自己显示为"你"）
#     └ sand-message-action-anchor 的 Name = "<发言人> <正文>"；sand-inserted-chip 按钮 = @提及
#   输入框  Edit  class 含 sand-prompt-field
WORKING_KW = ("工作中", "working")
WAITING_KW = ("需要关注", "需要你批准", "needs attention")
ROW_RE = re.compile(r"^(?P<who>.+?) (?P<ts>\d{1,2}:\d{2}(?::\d{2})?)$")


def parse_sidebar(items):
    """items: [(button_name, first_text)] -> {对话名: "typing"|"waiting"|"idle"}"""
    out = {}
    for btn, name, *_ in items:
        label = name or btn.split(",")[0]
        tail = btn[len(name):] if name and btn.startswith(name) else btn
        if any(k in tail for k in WAITING_KW):
            out[label] = "waiting"
        elif any(k in tail for k in WORKING_KW):
            out[label] = "typing"
        else:
            out[label] = "idle"
    return out


def dominant_color(img, box):
    """头像区域里的主色 (r,g,b)。box=(l,t,r,b)。背景取区域左边缘的颜色，排除掉；
    头像里的眼睛、嘴巴只占几个像素，量化后取出现最多的那一档。"""
    from collections import Counter
    l, t, r, b = box
    crop = img.crop((l, t, r, b)).convert("RGB")
    w, h = crop.size
    px = list(crop.getdata())
    bg = px[(h // 2) * w]                                   # 区域最左一列正中，是列表条目的底色
    bins, sums = Counter(), {}
    for p in px:
        if sum(abs(p[i] - bg[i]) for i in range(3)) < 60:
            continue
        key = (p[0] >> 4, p[1] >> 4, p[2] >> 4)
        bins[key] += 1
        a = sums.setdefault(key, [0, 0, 0])
        for i in range(3):
            a[i] += p[i]
    if not bins:
        return None
    key, n = bins.most_common(1)[0]
    return tuple(v // n for v in sums[key])


def hex_color(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


AGENT_STATES = {"工作中": "working", "待审核": "review", "已合并": "merged", "已完成": "finished", "运行失败": "failed"}
AGENT_ROW_RE = re.compile(r"^在 (?:Cursor|Web) 中打开 (?P<name>.+?), (?P<state>工作中|待审核|已合并|已完成|运行失败)(?:, #(?P<pr>\d+))?$")
AGENT_PRIORITY = ("working", "failed", "review", "finished", "merged")


def parse_agent_rows(names):
    """界面里的云端任务行（Cursor 云端 agent）：按钮名形如 "在 Cursor 中打开 <任务名>, 工作中, #123"。
    返回 [(任务名, 状态)]；状态是 working/review/merged/finished/failed。"""
    out = []
    for n in names:
        m = AGENT_ROW_RE.match((n or "").strip())
        if m:
            out.append((m["name"], AGENT_STATES[m["state"]]))
    return out


def overall_remote_state(rows):
    """多条任务合成一个状态：有在跑的就是 working，其次失败，其次待审核，最后已完成/已合并。"""
    states = {s for _, s in rows}
    for s in AGENT_PRIORITY:
        if s in states:
            return s
    return ""


def select_bots(items, members, limit=6):
    """从侧边栏挑出要上工位的 bot（工位最多 6 个）。
    不要群聊条目（名字里有 、）和主 Bot；当前群的成员优先，其次是正在干活/需要关注的，最后按侧边栏顺序。"""
    st = parse_sidebar(items)
    names = [(n, "主 Bot" in btn) for btn, n, *_ in items if n and "、" not in n]
    names = [n for n, main in names if not main]
    rank = lambda n: (n not in members, st.get(n, "idle") == "idle", names.index(n))
    return sorted(names, key=rank)[:limit]


def sidebar_previews(items, bots):
    """{bot: 该 bot 单聊里最后一条消息的预览}，用来发现没打开的聊天里的新动静。"""
    return {n: pv for btn, n, pv, *_ in [(i[0], i[1], i[2] if len(i) > 2 else "") for i in items] if n in bots and pv}


def parse_row(row_name, anchor_name, chips):
    """一行聊天记录 -> (发言人, 正文, 时间) 或 None。chips 是被 @ 的名字。"""
    m = ROW_RE.match((row_name or "").strip())
    if not m or not anchor_name:
        return None
    who, ts = m["who"], m["ts"]
    text = anchor_name.strip()
    if text.startswith(who):
        text = text[len(who):].strip()
    for c in chips:
        text = text.replace(c, "@" + c, 1)
    return who, text, ts


# ---------------------------------------------------------------- 提问卡片（bot 问你、给几个选项让你选）
# 卡片外层控件的 ClassName 里含这些字就当提问卡片（不分大小写）。Grok Bot 改版对不上时，在 config.toml 的
# question_class 里改：卡片显示在屏幕上时跑 python bridge.py --probe，在 uia_dump.txt 里找卡片那一层的 class
# Grok Bot 0.68.1 实测：卡片外层是 sand-message-card（里面问题、说明两行字，选项是按钮，右上角一个「忽略问题」）
QUESTION_CLASS = ("sand-message-card", "question", "ask-user", "askuser", "ask_user")
QUESTION_SKIP = {"提交", "确认", "确定", "跳过", "取消", "关闭", "其他", "其它", "自定义", "发送", "复制", "忽略问题", "忽略",
                 "Submit", "Confirm", "Skip", "Cancel", "Close", "Other", "Send", "Copy"}
QUESTION_SUBMIT = ("提交", "确认", "确定", "发送", "Submit", "Confirm", "Send")
ENTRY_RE = re.compile(r"(?:%40|@|s)(\d+)-timestamp$")    # 聊天记录每一行的序号（行里时间戳控件 id 的末尾）
OPTION_LETTER_RE = re.compile(r"^\s*([A-Fa-f])\s*[.、:：)）]\s*")
REC_WORD_RE = re.compile(r"[（(]?\s*(?:建议|推荐|Recommended)\s*[)）]?\s*[：:]?\s*")
REC_LETTER_RE = re.compile(r"(?:建议|推荐|Recommended)\s*(?:选)?\s*([A-Fa-f])(?![A-Za-z])")


def parse_question(texts, buttons, selected=()):
    """一张提问卡片里的文字和按钮 -> {"qid", "title", "detail", "options": [显示用], "raw": [按钮原名], "recommended", "answer"}；
    不像提问（选项少于 2 个）返回 None。"""
    raw, seen = [], set()
    for b in buttons:
        b = " ".join(str(b).split())
        if b and b not in seen and b not in QUESTION_SKIP:
            seen.add(b)
            raw.append(b)
    if len(raw) < 2:
        return None
    texts = [" ".join(str(t).split()) for t in texts if str(t).strip() and " ".join(str(t).split()) not in seen]
    title = next((t for t in texts if t.endswith(("？", "?"))), "") or max(texts, key=len, default="")
    # sand-message-card 不只是提问卡片（云端任务卡片「main 运行中 / 停止… / 打开…」也是）：
    # 要么带「忽略问题」按钮，要么有一句问话，才算问你
    if not title.endswith(("？", "?")) and not any(" ".join(str(b).split()) == "忽略问题" for b in buttons):
        return None
    detail = " ".join(t for t in texts if t != title)[:200]          # 问题下面那行说明
    rec, options = -1, []
    for k, b in enumerate(raw):
        label = OPTION_LETTER_RE.sub("", b)
        if REC_WORD_RE.search(label):
            rec = k if rec < 0 else rec
            label = REC_WORD_RE.sub("", label).strip()
        options.append(label or b)
    if rec < 0:                                           # 「建议 A：…」写在正文里
        m = next((REC_LETTER_RE.search(t) for t in texts if REC_LETTER_RE.search(t)), None)
        if m and ord(m.group(1).upper()) - 65 < len(raw):
            rec = ord(m.group(1).upper()) - 65
    answer = next((options[raw.index(s)] for s in selected if s in raw), "")
    qid = hashlib.sha1((title + "\n" + "\n".join(raw)).encode("utf-8")).hexdigest()[:12]
    return {"qid": qid, "title": title, "detail": detail, "options": options, "raw": raw, "recommended": rec,
            "answer": answer}


def members_from_title(title, humans):
    """未命名群聊的标题就是成员列表，用 、 分隔；去掉你自己。"""
    if "、" not in title:
        return []
    return [n.strip() for n in title.split("、") if n.strip() and n.strip() not in humans]


class Tracker:
    """记住已经见过的消息，只把新出现的报上去。"""

    def __init__(self):
        self.seen = None
        self.statuses = {}

    @staticmethod
    def _key(sender, text, ts, n):
        return hashlib.md5(f"{sender}\0{ts}\0{text}\0{n}".encode()).hexdigest()

    def _keys(self, messages):
        keys, count = [], {}
        for sender, text, ts in messages:
            n = count.get((sender, text, ts), 0)
            count[(sender, text, ts)] = n + 1
            keys.append((self._key(sender, text, ts, n), sender, text))
        return keys

    def rebase(self, messages):
        """切到另一个聊天时调用：那个聊天里已有的记录不算新消息。"""
        self.seen = (self.seen or set()) | {k for k, _, _ in self._keys(messages)}

    def update(self, statuses, messages, private=False):
        events = []
        keys = self._keys(messages)
        if self.seen is None:
            self.seen = {k for k, _, _ in keys}       # 启动时界面上已有的内容不算新消息
        else:
            for k, sender, text in keys:
                if k not in self.seen:
                    self.seen.add(k)
                    events.append({"type": "message", "sender": sender, "text": text[:500], "private": private})
        for name, st in statuses.items():
            if self.statuses.get(name) != st:
                self.statuses[name] = st
                events.append({"type": "status", "name": name, "status": st})
        return events


# ---------------------------------------------------------------- 额度（Cursor 控制台的只读接口）
class CursorUsage:
    """Grok Bot 的额度由 Cursor 后端提供。只调两个只读接口：
        POST /api/dashboard/get-sand-usage-status   -> Grok Bot 每周额度（usagePercent, nextResetTimestampUtc）
        POST /api/dashboard/get-current-period-usage -> Cursor 模型 / 其他模型（autoPercentUsed, apiPercentUsed）
    登录会话 Cookie 从 secrets.toml 或环境变量 NIUMA_CURSOR_SESSION 读，不写盘、不打印。"""
    BASE = "https://cursor.com"

    def __init__(self, session):
        self.session = session

    def _post(self, path):
        req = urllib.request.Request(self.BASE + path, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.BASE)
        req.add_header("Referer", self.BASE + "/dashboard/spending")
        req.add_header("Cookie", "WorkosCursorSessionToken=" + self.session)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")

    def agents(self, watch=None):
        """云端任务（Cursor 云端 agent）的总状态：working | failed | finished | ""（没有在跑的）。
        watch = 只看这些任务的 bcId（如 {"bc-8bbb…"}）；空 = 看全部。返回 (状态, {bcId: 任务名})。
        POST /api/background-composer/list，每个任务有 status 和 workflowStatus 两个枚举，
        只见过 BACKGROUND_COMPOSER_STATUS_FINISHED / CLOUD_AGENT_WORKFLOW_STATUS_IDLE，
        其它取值按关键字猜（RUNNING/CREATING 算在跑，ERROR/FAILED 算失败），见到没见过的会打印出来。"""
        req = urllib.request.Request(self.BASE + "/api/background-composer/list", method="POST",
                                     data=json.dumps({"n": 20, "include_status": True, "include_archived": False}).encode())
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.BASE)
        req.add_header("Referer", self.BASE + "/agents")
        req.add_header("Cookie", "WorkosCursorSessionToken=" + self.session)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36")
        with urllib.request.urlopen(req, timeout=20) as r:
            items = json.loads(r.read() or b"{}").get("composers", [])
        if watch:
            items = [i for i in items if i.get("bcId") in watch]
        return overall_agent_state(items), {i.get("bcId"): str(i.get("name", "")) for i in items}

    def fetch(self):
        """返回给 hub 的 usage 事件。出错也返回事件（ok=False + error），让画面能提示。"""
        try:
            sand = self._post("/api/dashboard/get-sand-usage-status")
            cur = self._post("/api/dashboard/get-current-period-usage")
        except urllib.error.HTTPError as e:
            msg = "Cookie 已过期，请更新 secrets.toml" if e.code in (401, 403) else f"额度接口返回 {e.code}"
            return {"type": "usage", "ok": False, "error": msg}
        except Exception as e:
            return {"type": "usage", "ok": False, "error": "额度读取失败: " + type(e).__name__}
        pu = cur.get("planUsage", {})
        return {"type": "usage", "ok": True, "plan": sand.get("cursorPlanName", ""),
                "grok": {"pct": sand.get("usagePercent"), "reset": sand.get("nextResetTimestampUtc")},
                "cursor": pu.get("autoPercentUsed"), "other": pu.get("apiPercentUsed"),
                "total": pu.get("totalPercentUsed")}


_SEEN_ENUMS = set()


def agent_state(item):
    """一个云端任务 -> working | failed | finished | ""。"""
    st = str(item.get("status", "")).upper()
    wf = str(item.get("workflowStatus", "")).upper()
    for e in (st, wf):
        if e and e not in _SEEN_ENUMS and not e.endswith(("FINISHED", "IDLE")):
            _SEEN_ENUMS.add(e)
            print("云端任务出现新的状态值:", e)
    text = st + " " + wf
    if any(k in text for k in ("ERROR", "FAILED", "FAILURE")):
        return "failed"
    if any(k in text for k in ("RUNNING", "CREATING", "PENDING", "STARTING", "IN_PROGRESS", "ACTIVE", "WORKING", "QUEUED")):
        return "working"
    if "FINISHED" in text or "COMPLETED" in text:
        return "finished"
    return ""


def parse_agent_ids(values):
    """配置里的云端任务：bcId 或任务网址（https://cursor.com/agents/bc-…?…）都行 -> {bcId}。"""
    out = set()
    for v in values or []:
        out.update(re.findall(r"bc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(v).lower()))
    return out


def overall_agent_state(items):
    states = {agent_state(i) for i in items}
    for s_ in ("working", "failed", "finished"):
        if s_ in states:
            return s_
    return ""


def load_cursor_session(here):
    import os
    tok = os.environ.get("NIUMA_CURSOR_SESSION", "").strip()
    f = Path(here) / "secrets.toml"
    if not tok and f.exists():
        tok = str(tomllib.loads(f.read_text(encoding="utf-8")).get("cursor_session", "")).strip()
    return tok


# ---------------------------------------------------------------- Windows 界面操作
class GrokWindow:
    def __init__(self, cfg):
        import uiautomation as auto
        self.auto = auto
        self.cfg = cfg
        self.title = cfg.get("window_title", "Grok Bot")
        self.win = None
        self.group_title = cfg.get("group", "")   # 群聊的标题，主循环里看到群聊时更新
        self.qclass = tuple(s.lower() for s in cfg.get("question_class", QUESTION_CLASS) if s)

    def find(self):
        auto = self.auto
        if self.win is not None:
            try:
                if self.win.Exists(0, 0):
                    return self.win
            except Exception:
                pass
        self.win = None
        for c in auto.GetRootControl().GetChildren():
            try:
                if c.ClassName == "Chrome_WidgetWin_1" and self.title.lower() in (c.Name or "").lower():
                    self.win = c
                    return c
            except Exception:
                continue
        return None

    def _walk(self, root, depth=0, max_depth=40):
        """深度优先遍历，产出 (控件, 深度)。UIA 调用慢，只在需要的子树里用。"""
        if depth > max_depth:
            return
        try:
            kids = root.GetChildren()
        except Exception:
            return
        for k in kids:
            yield k
            yield from self._walk(k, depth + 1, max_depth)

    def snapshot(self):
        """读一次界面。返回 dict，窗口找不到/最小化/无障碍树为空时返回 None。

        {"title": 当前聊天标题, "sidebar": [(按钮名, 对话名, 最后一条预览)], "rows": [(行名, 正文, [@名字])]}
        """
        win = self.find()
        if win is None:
            return None
        title, sidebar, rows, seen_input, agent_names, cards = "", [], [], False, [], []
        entries, row_ctx = [], None           # row_ctx = 刚走过的聊天行 (rect, 行名, 正文, chips, 序号)，卡片在它里面就是它发的
        for c in self._walk(win):
            try:
                cls, ctype = c.ClassName or "", c.ControlTypeName
            except Exception:
                continue
            if c.AutomationId == "sand-conversation-heading":
                title = c.Name or ""
            elif ctype == "ListControl" and "sand-agents-list__list" in cls:
                for item in c.GetChildren():
                    btn, texts, rect = "", [], None
                    for d in self._walk(item, 0, 4):
                        if d.ControlTypeName == "ButtonControl" and not btn:
                            btn = d.Name or ""
                            r = d.BoundingRectangle
                            rect = (r.left, r.top, r.right, r.bottom)
                        elif d.ControlTypeName == "TextControl" and d.Name:
                            texts.append(d.Name)
                    if btn:
                        sidebar.append((btn, texts[0] if texts else "", texts[1] if len(texts) > 1 else "", rect))
            elif ctype == "ButtonControl" and (c.Name or "").startswith("在 ") and "中打开 " in (c.Name or ""):
                agent_names.append(c.Name)
            elif ctype == "GroupControl" and "sand-transcript-row" in cls:
                anchor, chips, entry = "", [], None
                for d in self._walk(c, 0, 12):
                    dc = d.ClassName or ""
                    if "sand-message-action-anchor" in dc and not anchor:
                        anchor = d.Name or ""
                    elif "sand-inserted-chip" in dc:
                        chips.append(d.Name or "")
                    elif "sand-row-timestamp" in dc and entry is None:
                        m = ENTRY_RE.search(d.AutomationId or "")
                        entry = int(m.group(1)) if m else None
                rows.append((c.Name or "", anchor, chips))
                if entry is not None:
                    entries.append(entry)
                try:
                    r = c.BoundingRectangle
                    row_ctx = ((r.left, r.top, r.right, r.bottom), c.Name or "", anchor, chips, entry)
                except Exception:
                    row_ctx = None
            elif self.qclass and ctype in ("GroupControl", "CustomControl", "PaneControl", "ListControl") \
                    and any(f in cls.lower() for f in self.qclass):
                cards.append((c, row_ctx))
        if not title and not sidebar:
            return None                       # 最小化时 Chromium 不生成无障碍树
        done = []
        return {"title": title, "sidebar": sidebar, "rows": rows, "agents": agent_names, "entries": entries,
                "questions": [q for q, _ in self._read_cards(cards, done)], "done_cards": done}

    _OPTION_TYPES = ("ButtonControl", "RadioButtonControl", "ListItemControl", "CheckBoxControl")

    def _read_cards(self, cards, done=None):
        """[(提问卡片控件, 它所在的聊天行 row_ctx 或 None)] -> [(parse_question 的结果, 卡片控件)]。
        结果里另加 who（哪个 bot 发的，认不出是空）和 entry（卡片在第几行聊天记录，认不出是 None）。
        卡片里面套着的小块（选项列表之类）也会被匹配上，只留最外层；同一张卡片只留一次。
        已经选掉的卡片（Grok 把选项按钮收起来，只留问题和选中的那一行字）把里面的字放进 done。"""
        rects = []
        for c, _ctx in cards:
            try:
                r = c.BoundingRectangle
                rects.append((r.left, r.top, r.right, r.bottom))
            except Exception:
                rects.append(None)
        out, seen = [], set()
        for k, (c, ctx) in enumerate(cards):
            rk = rects[k]
            if rk is None or any(j != k and rj and rj != rk and rj[0] <= rk[0] and rj[1] <= rk[1]
                                 and rj[2] >= rk[2] and rj[3] >= rk[3] for j, rj in enumerate(rects)):
                continue                                  # 被别的卡片控件包着：是里面的一块
            texts, buttons, selected = [], [], []
            for d in self._walk(c, 0, 14):
                try:
                    dt, name = d.ControlTypeName, (d.Name or "").strip()
                except Exception:
                    continue
                if not name:
                    continue
                if dt == "TextControl":
                    texts.append(name)
                elif dt in self._OPTION_TYPES:
                    buttons.append(name)
                    if self._is_selected(d):
                        selected.append(" ".join(name.split()))
            q = parse_question(texts, buttons, selected)
            if q is None and done is not None and texts:
                done.append([" ".join(t.split()) for t in texts])
            if q and q["qid"] not in seen:
                q["who"], q["entry"] = "", None
                if ctx and ctx[0][0] <= rk[0] and ctx[0][1] <= rk[1] and ctx[0][2] >= rk[2] and ctx[0][3] >= rk[3]:
                    r = parse_row(ctx[1], ctx[2] or q["title"], ctx[3])
                    q["who"], q["entry"] = (r[0] if r else ""), ctx[4]
                seen.add(q["qid"])
                out.append((q, c))
        return out

    def _is_selected(self, c):
        P = self.auto.PatternId
        try:
            p = c.GetPattern(P.SelectionItemPattern)
            if p and p.IsSelected:
                return True
        except Exception:
            pass
        try:
            p = c.GetPattern(P.TogglePattern)
            if p and p.ToggleState == 1:
                return True
        except Exception:
            pass
        return False

    def question_cards(self, root=None):
        """现在界面上（root 下面）的提问卡片 [(解析结果, 卡片控件)]。"""
        root = root or self.find()
        if root is None:
            return []
        cards = []
        for c in self._walk(root):
            try:
                if c.ControlTypeName in ("GroupControl", "CustomControl", "PaneControl", "ListControl") \
                        and any(f in (c.ClassName or "").lower() for f in self.qclass):
                    cards.append((c, None))
            except Exception:
                continue
        return self._read_cards(cards)

    def _card(self, qid, root=None):
        return next(((q, c) for q, c in self.question_cards(root) if q["qid"] == qid), None)

    def _scroll_find(self, qid, pages=40):
        """卡片被新消息顶上去了（对话记录只渲染看得见的一段）：一页页往上翻着找。
        返回 (找到的卡片或 None, 翻回原位的函数)。"""
        vp = self._find(name="对话记录", timeout=0.5)
        try:
            p = vp.GetPattern(self.auto.PatternId.ScrollPattern) if vp is not None else None
        except Exception:
            p = None
        if not p:
            return None, lambda: None
        start = p.VerticalScrollPercent
        back = lambda: p.SetScrollPercent(-1, 100 if start >= 99 else start, waitTime=0.2)
        try:
            for _ in range(pages):
                if p.VerticalScrollPercent <= 0:
                    break
                p.Scroll(self.auto.ScrollAmount.NoAmount, self.auto.ScrollAmount.LargeDecrement, waitTime=0.35)
                found = self._card(qid, vp)
                if found is not None:
                    return found, back
        except Exception:
            pass
        back()
        return None, lambda: None

    def answer(self, qid, index, option, bot="", chat=None):
        """替你在提问卡片上选第 index 个选项（必要时再点「提交」）。卡片不在眼前就先切到它所在的聊天
        （chat：空 = 群聊，否则是单聊名；老 hub 不给 chat 时按 bot 的单聊找），还没有就往上翻着找。"""
        found = self._card(qid)
        target = bot if chat is None else (chat or self.group_title)
        back, unscroll = "", lambda: None
        if found is None and target:
            here = self.heading()
            if not self._same_chat(here, target):
                back = here
                if self.open_chat(target, keys=False):        # 只点侧边栏，不抢前台
                    found = self._card(qid)
            if found is None and self._same_chat(self.heading(), target):
                found, unscroll = self._scroll_find(qid)
        try:
            self._answer_card(found, index, option)
        finally:
            try:
                time.sleep(0.3)
                unscroll()                                    # 翻上去找的：翻回原来的位置
            except Exception:
                pass
            if back:
                self._go_back(back, target)

    def _answer_card(self, found, index, option):
        if found is None:
            raise RuntimeError("界面上找不到这张提问卡片")
        q, card = found
        if not 0 <= index < len(q["raw"]) or q["options"][index] != option:
            raise RuntimeError("卡片上的选项变了")
        raw = q["raw"][index]
        target = submit = None
        for d in self._walk(card, 0, 14):
            try:
                dt, name = d.ControlTypeName, " ".join((d.Name or "").split())
            except Exception:
                continue
            if dt in self._OPTION_TYPES and name == raw and target is None:
                target = d
            elif dt == "ButtonControl" and name in QUESTION_SUBMIT:
                submit = d
        if target is None:
            raise RuntimeError("找不到这个选项的按钮")
        self._invoke(target)
        if submit is not None:                            # 先选再提交的卡片
            time.sleep(0.3)
            self._invoke(submit)

    def capture(self):
        """用 PrintWindow 截 Grok Bot 窗口，窗口被别的窗口挡住也能截到。返回 (PIL 图, 窗口左上角屏幕坐标)。"""
        import ctypes
        from ctypes import wintypes
        from PIL import Image
        win = self.find()
        if win is None or self.auto.IsIconic(win.NativeWindowHandle):
            return None
        r = win.BoundingRectangle
        w, h = r.right - r.left, r.bottom - r.top
        u, g, V = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.c_void_p
        u.GetWindowDC.restype, u.GetWindowDC.argtypes = V, [V]
        u.ReleaseDC.argtypes = [V, V]
        u.PrintWindow.argtypes = [V, V, ctypes.c_uint]
        g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = V, [V]
        g.CreateCompatibleBitmap.restype, g.CreateCompatibleBitmap.argtypes = V, [V, ctypes.c_int, ctypes.c_int]
        g.SelectObject.argtypes = [V, V]
        g.DeleteObject.argtypes = [V]
        g.DeleteDC.argtypes = [V]
        g.GetDIBits.argtypes = [V, V, ctypes.c_uint, ctypes.c_uint, V, V, ctypes.c_uint]

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("a", wintypes.LONG), ("b", wintypes.LONG),
                        ("c", wintypes.DWORD), ("d", wintypes.DWORD)]
        hwnd = win.NativeWindowHandle
        hdc = u.GetWindowDC(hwnd)
        mdc = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, w, h)
        g.SelectObject(mdc, bmp)
        ok = u.PrintWindow(hwnd, mdc, 2)                          # 2 = PW_RENDERFULLCONTENT，Chromium 窗口必须带
        bi = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(hwnd, hdc)
        if not ok:
            return None
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB"), (r.left, r.top)

    def avatar_colors(self, sidebar, names):
        """{bot名: (r,g,b)}：每个侧边栏条目最左边是头像，截下来取主色。条目不在可见范围内的跳过。"""
        todo = [(btn, name, rect) for btn, name, _pv, rect in sidebar if name in names and rect]
        if not todo:
            return {}
        cap = self.capture()
        if cap is None:
            return {}
        img, (ox, oy) = cap
        out = {}
        for btn, name, (l, t, r, b) in todo:
            l, t, r, b = l - ox, t - oy, r - ox, b - oy
            if t < 0 or b > img.size[1] or l < 0:
                continue
            c = dominant_color(img, (l + 6, t + 6, l + 6 + 40, b - 6))
            if c:
                out[name] = c
        return out

    def probe(self, path):
        auto = self.auto
        lines = []
        tops = auto.GetRootControl().GetChildren()
        lines.append("== 顶层窗口 ==")
        for c in tops:
            lines.append(f"  [{c.ControlTypeName}] name={c.Name!r} class={c.ClassName!r} pid={c.ProcessId}")
        win = self.find()
        if win is None:
            lines.append(f"\n没找到标题包含 {self.title!r} 的窗口，请对照上面的列表修改 config.toml 里的 window_title")
        else:
            lines.append(f"\n== 目标窗口 name={win.Name!r} class={win.ClassName!r} pid={win.ProcessId} ==")
            lines.append("(class 是 Chrome_WidgetWin_1 说明是 Electron/Chromium 内核)")
            n = 0
            for c, depth in auto.WalkControl(win, includeTop=True, maxDepth=60):
                try:
                    r = c.BoundingRectangle
                    lines.append(f"{'  ' * depth}[{c.ControlTypeName}] name={c.Name!r} id={c.AutomationId!r} "
                                 f"class={c.ClassName!r} rect=({r.left},{r.top},{r.right},{r.bottom})")
                except Exception as e:
                    lines.append(f"{'  ' * depth}<读取失败 {e}>")
                n += 1
                if n >= 8000:
                    lines.append("... 超过 8000 个控件，截断")
                    break
            lines.append(f"\n共 {n} 个控件")
            if n < 30:
                lines.append("控件很少：Chromium 可能没打开无障碍树。请给 Grok Bot 的快捷方式加上启动参数 "
                             "--force-renderer-accessibility 后重启它再试。")
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        return win is not None, len(lines)

    def _paste(self, text):
        auto = self.auto
        try:
            old = auto.GetClipboardText()
        except Exception:
            old = None
        auto.SetClipboardText(text)
        time.sleep(0.05)
        auto.SendKeys("{Ctrl}v", waitTime=0.15)
        if old is not None:
            time.sleep(0.15)
            try:
                auto.SetClipboardText(old)
            except Exception:
                pass

    def _to_front(self, h):
        """把窗口弄到前台。Windows 不让后台进程直接抢前台，依次尝试：
        直接切 -> 先按一下 Alt 再切 -> 把输入线程挂到当前前台窗口再切。"""
        import ctypes
        auto = self.auto
        u = ctypes.windll.user32
        if auto.IsIconic(h):
            auto.ShowWindow(h, 9)                 # SW_RESTORE
            time.sleep(0.4)

        def ok():
            time.sleep(0.25)
            return auto.GetForegroundWindow() == h

        auto.SetForegroundWindow(h)
        if ok():
            return True
        u.keybd_event(0x12, 0, 0, 0)              # Alt 按下/抬起，让系统认为刚有用户输入
        u.keybd_event(0x12, 0, 2, 0)
        auto.SetForegroundWindow(h)
        if ok():
            return True
        fg = u.GetForegroundWindow()
        t_fg = u.GetWindowThreadProcessId(fg, None)
        t_me = ctypes.windll.kernel32.GetCurrentThreadId()
        u.AttachThreadInput(t_me, t_fg, True)
        try:
            u.BringWindowToTop(h)
            u.SetForegroundWindow(h)
        finally:
            u.AttachThreadInput(t_me, t_fg, False)
        return ok()

    def heading(self):
        """当前打开的聊天的标题；读不到返回空字符串。"""
        win = self.find()
        if win is None:
            return ""
        try:
            c = self.auto.Control(searchFromControl=win, AutomationId="sand-conversation-heading", searchDepth=40)
            if c.Exists(0.5, 0.1):
                return (c.Name or "").strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _same_chat(heading, name):
        return heading == name or (name in heading and "、" not in heading and "、" not in name)

    def _wait_heading(self, name, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self._same_chat(self.heading(), name):
                return True
            time.sleep(0.25)
        return False

    def _sidebar_button(self, name):
        """侧边栏里对话名正好是 name 的那个条目按钮；不在列表里返回 None。"""
        win = self.find()
        if win is None:
            return None
        for c in self._walk(win):
            try:
                if c.ControlTypeName != "ListControl" or "sand-agents-list__list" not in (c.ClassName or ""):
                    continue
            except Exception:
                continue
            for item in c.GetChildren():
                btn, first = None, ""
                for d in self._walk(item, 0, 4):
                    if d.ControlTypeName == "ButtonControl" and btn is None:
                        btn = d
                    elif d.ControlTypeName == "TextControl" and d.Name and not first:
                        first = d.Name
                if btn is not None and (first or (btn.Name or "").split(",")[0]).strip() == name:
                    return btn
            return None
        return None

    def _find(self, name=None, cls=None, timeout=2.0):
        """在 Grok Bot 窗口里找控件：name 完全相等，cls 是 ClassName 里的一段。找不到返回 None。"""
        win = self.find()
        if win is None:
            return None

        def cmp(c, depth):
            try:
                return ((name is None or (c.Name or "") == name)
                        and (cls is None or cls in (c.ClassName or "")))
            except Exception:
                return False
        c = self.auto.Control(searchFromControl=win, searchDepth=80, Compare=cmp)
        try:
            return c if c.Exists(timeout, 0.15) else None
        except Exception:
            return None

    def _invoke(self, c):
        """点一下控件。只走无障碍接口（Invoke / 展开菜单 / 默认动作），不动鼠标、窗口不用在前台；都不支持就报错。"""
        P = self.auto.PatternId
        for pid, act in ((P.InvokePattern, lambda p: p.Invoke()),
                         (P.ExpandCollapsePattern, lambda p: p.Expand()),
                         (P.LegacyIAccessiblePattern, lambda p: p.DoDefaultAction())):
            try:
                p = c.GetPattern(pid)
            except Exception:
                p = None
            if p:
                act(p)
                return
        raise RuntimeError(f"{c.Name!r} 没法在后台点")

    def _collapse(self, c):
        """把 c 展开的菜单收起来。"""
        try:
            p = c.GetPattern(self.auto.PatternId.ExpandCollapsePattern)
            if p:
                p.Collapse()
        except Exception:
            pass

    def _prompt_text(self):
        """输入框里现在的文字（空输入框读出来是一个换行）。"""
        f = self._find(cls="sand-prompt-field", timeout=1.0)
        try:
            return (f.GetPattern(self.auto.PatternId.ValuePattern).Value or "").strip() if f else ""
        except Exception:
            return ""

    def _wait_dictation_done(self, timeout=15.0):
        """等听写结束：录音条没了，"正在转写语音输入…"也没了。"""
        end = time.time() + timeout
        while time.time() < end:
            if (self._find(cls="sand-recording-chip", timeout=0.2) is None
                    and self._find(name="正在转写语音输入…", timeout=0.2) is None):
                return True
            time.sleep(0.3)
        return False

    def voice_play(self, to, wav, send=True):
        """按住说话：切到 to 的单聊（None = 群聊），点 Grok Bot 自带的「开始语音输入」，同时把录音放进虚拟麦克风，
        放完点「发送消息」，由 Grok 听写成文字发出去；send=False 时改成停止听写并清掉转出来的字（测试用）。
        Grok Bot 用的麦克风要是虚拟声卡的录音端（如 CABLE Output）。全程不抢前台、不动鼠标。"""
        devname = self.cfg.get("voice_device", "CABLE Input")
        dev = find_wave_out(devname)
        if dev is None:
            raise RuntimeError(f"没找到虚拟麦克风 {devname}")
        back = self.heading()
        target = to or self.group_title or back
        if not target:
            raise RuntimeError("不知道群聊是哪个")
        if not self.open_chat(target, keys=False):
            raise RuntimeError(f"没切到 {target}")
        try:
            if self._prompt_text():
                raise RuntimeError("输入框里有没发的草稿")
            mic = self._find(name="开始语音输入")
            if mic is None:
                raise RuntimeError("找不到开始语音输入按钮")
            self._invoke(mic)
            if self._find(cls="sand-recording-chip", timeout=4.0) is None:
                raise RuntimeError("Grok 没开始听写(麦克风?)")
            time.sleep(0.6)                                   # 等它真正开始收音
            play_wav(dev, wav)
            time.sleep(0.6)                                   # 最后一个字别被截掉
            if send:
                btn = self._find(cls="sand-prompt-voice-send", timeout=1.5)
                if btn is None:
                    raise RuntimeError("找不到发送按钮(听写停了?)")
                self._invoke(btn)
                if not self._wait_dictation_done(30.0):
                    raise RuntimeError("转写太久，没确认发出去")
                time.sleep(0.8)
                if self._prompt_text():
                    raise RuntimeError("转好的字留在输入框里没发")
            else:
                self._cancel_dictation()
        except Exception:
            self._cancel_dictation()
            raise
        finally:
            self._go_back(back, target)

    def _cancel_dictation(self):
        """停止听写，清掉转出来的字。"""
        chip = self._find(cls="sand-recording-chip", timeout=0.5)
        if chip is not None:
            try:
                self._invoke(chip)
            except Exception:
                pass
            self._wait_dictation_done()
            time.sleep(0.5)
        f = self._find(cls="sand-prompt-field", timeout=1.0)
        if f is not None and self._prompt_text():
            try:
                f.GetPattern(self.auto.PatternId.ValuePattern).SetValue("")
            except Exception:
                pass

    def _exe_path(self):
        """Grok Bot.exe 在哪：正在跑就从它的窗口进程读；没在跑用 config 的 grok_exe，再不行用默认安装位置。"""
        import ctypes
        import os
        win = self.find()
        if win is not None:
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, win.ProcessId)             # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                try:
                    buf, n = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
                    if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                        self.exe = buf.value
                finally:
                    k.CloseHandle(h)
        return (getattr(self, "exe", "") or self.cfg.get("grok_exe")
                or os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Grok Bot", "Grok Bot.exe"))

    def restart(self, timeout=40.0):
        """重启 Grok Bot：把 Grok Bot.exe 的进程全杀掉（托盘、执行子进程一起），重新打开，等窗口出来放到前台。"""
        import os
        import subprocess
        exe = self._exe_path()
        if not os.path.isfile(exe):
            raise RuntimeError(f"找不到 {exe}，在 config.toml 里填 grok_exe")
        name = os.path.basename(exe)
        flags = 0x08000000                                              # CREATE_NO_WINDOW
        subprocess.run(["taskkill", "/F", "/T", "/IM", name], capture_output=True, creationflags=flags)
        end = time.time() + 10
        while time.time() < end:                                        # 等进程都退干净，不然新开的会被单实例锁挡回去
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], capture_output=True,
                                 creationflags=flags).stdout.decode("mbcs", "replace")
            if name.lower() not in out.lower():
                break
            time.sleep(0.5)
        self.win = None
        os.startfile(exe)                                               # 和双击一样打开，不挂在桥接进程下面
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1.0)
            win = self.find()
            if win is not None and self.heading():                      # 界面加载出来了（有聊天标题）
                break
        else:
            raise RuntimeError("重开了但等不到窗口")
        if not self._to_front(win.NativeWindowHandle):
            raise RuntimeError("重开了，但窗口切不到前台(锁屏?)")

    def _go_back(self, back, target):
        if back and not self._same_chat(back, target):
            time.sleep(0.4)
            if not self.open_chat(back, keys=False):
                print(f"没能切回原来的聊天 {back!r}，请手动切回群聊")

    def open_chat(self, name, timeout=3.0, keys=True):
        """切到名字叫 name 的聊天，以聊天标题对上为准。先点侧边栏条目，不行再用 Ctrl+K 搜（keys=False 时不搜，
        因为按键要求窗口在前台）。"""
        auto = self.auto
        if self._same_chat(self.heading(), name):
            return True
        btn = self._sidebar_button(name)
        if btn is not None:
            try:
                self._invoke(btn)
            except Exception:
                pass
            if self._wait_heading(name, timeout):
                return True
        if not keys:
            return False
        auto.SendKeys("{Ctrl}k", waitTime=0.5)            # 官方快捷键：搜索/跳转面板
        self._paste(name)
        time.sleep(0.7)
        auto.SendKeys("{Enter}", waitTime=0.9)
        if self._wait_heading(name, timeout):
            return True
        auto.SendKeys("{Esc}", waitTime=0.2)               # 搜不到时把面板关掉
        return False

    def send_private(self, name, text):
        """私聊 name 一句话：切到它的单聊 -> 确认标题 -> 发送 -> 切回原来的聊天。标题对不上就不发。"""
        auto = self.auto
        win = self.find()
        if win is None:
            raise RuntimeError("没找到 Grok Bot 窗口")
        h = win.NativeWindowHandle
        back = self.heading()
        prev = auto.GetForegroundWindow()
        if not self._to_front(h):
            raise RuntimeError("窗口切不到前台(锁屏?)")
        try:
            if not self.open_chat(name):
                raise RuntimeError(f"没切到 {name} 的私聊")
            auto.SendKeys("{Ctrl}l", waitTime=0.3)            # 聚焦输入框
            self._paste(text)
            time.sleep(0.2)
            auto.SendKeys("{Enter}", waitTime=0.5)
        finally:
            if back and not self._same_chat(back, name):
                time.sleep(0.5)
                if not self.open_chat(back):
                    print(f"没能切回原来的聊天 {back!r}，请手动切回群聊")
            if self.cfg.get("send", {}).get("restore_focus", True) and prev and prev != h:
                time.sleep(0.2)
                try:
                    auto.SetForegroundWindow(prev)
                except Exception:
                    pass

    def send(self, text):
        auto = self.auto
        win = self.find()
        if win is None:
            raise RuntimeError("没找到 Grok Bot 窗口")
        h = win.NativeWindowHandle
        prev = auto.GetForegroundWindow()
        if not self._to_front(h):
            raise RuntimeError("窗口切不到前台(锁屏?)")
        s = self.cfg.get("send", {})
        try:
            if s.get("switch", "search") == "search" and self.cfg.get("group"):
                auto.SendKeys("{Ctrl}k", waitTime=0.5)        # 官方快捷键：搜索/跳转面板
                self._paste(self.cfg.get("group", ""))
                time.sleep(0.7)
                auto.SendKeys("{Enter}", waitTime=0.9)
            auto.SendKeys("{Ctrl}l", waitTime=0.3)            # 官方快捷键：聚焦输入框
            self._paste(text)
            time.sleep(0.2)
            auto.SendKeys("{Enter}", waitTime=0.2)            # 回车发送
        finally:
            if s.get("restore_focus", True) and prev and prev != h:
                time.sleep(0.2)
                try:
                    auto.SetForegroundWindow(prev)
                except Exception:
                    pass


# ---------------------------------------------------------------- 主循环
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--send", metavar="TEXT")
    ap.add_argument("--send-private", nargs=2, metavar=("NAME", "TEXT"))
    ap.add_argument("--voice-test", nargs="?", const="", metavar="NAME")
    ap.add_argument("--wav", metavar="FILE", help="--voice-test 用的录音（16 位 PCM WAV）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--questions", action="store_true", help="只打印界面上认出来的提问卡片（校准 question_class 用）")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.exists() and cfg_path == HERE / "config.toml" and (HERE / "config.example.toml").exists():
        import shutil                     # 第一次运行：从示例复制一份（config.toml 不进 git）
        shutil.copyfile(HERE / "config.example.toml", cfg_path)
        print(f"已从 config.example.toml 复制出 {cfg_path}，按需修改")
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    if sys.platform != "win32":
        sys.exit("桥接只能在装了 Grok Bot 桌面端的 Windows 上运行")
    gw = GrokWindow(cfg)

    if args.probe:
        out = HERE / "uia_dump.txt"
        found, n = gw.probe(out)
        print(f"已导出 {n} 行到 {out}" + ("" if found else "（没找到目标窗口，文件里有全部窗口标题）"))
        return
    if args.questions:
        cards = gw.question_cards()
        print(json.dumps([q for q, _ in cards], ensure_ascii=False, indent=1) if cards else
              f"没认出提问卡片（question_class = {list(gw.qclass)}）。卡片显示着的时候跑 --probe，"
              "在 uia_dump.txt 里找包着问题和选项的那一层，把它 class 里的一段填到 config.toml 的 question_class")
        return
    if args.send:
        gw.send(args.send)
        print("已发送，请到群里确认")
        return
    if args.voice_test is not None:
        print("播放设备:", wave_out_devices())
        wav = Path(args.wav).read_bytes() if args.wav else beep_wav()
        gw.voice_play(args.voice_test or None, wav, send=False)
        print("Grok 听写过了一遍，已取消、清掉转出来的字并切回原来的聊天（没有发送）")
        return
    if args.send_private:
        gw.send_private(*args.send_private)
        print(f"已私聊 {args.send_private[0]}，请到它的单聊里确认")
        return

    cfg_bots = list(cfg.get("bots", []))[:6]
    me = cfg.get("me", "我")
    humans = {me, "你", "You", "Me", "我"}
    poll = float(cfg.get("poll_seconds", 2))
    hub = HubClient(cfg["hub"], cfg.get("token", ""))
    tracker = Tracker()
    last_hello = 0.0
    last_bots = None
    previews = None
    last_title = None
    last_members = None
    last_notes = {}
    last_questions = {}                     # 报过的提问卡片 {qid: [在哪个聊天看到的, 内容签名, 第几行, 上报的事件]}
    last_remote = (None, 0.0)
    colors, last_color_scan, sent_colors = {}, 0.0, None
    usage_box, usage_next = {}, 0.0
    agent_box, agent_next, agent_sent = {}, 0.0, None
    session = load_cursor_session(HERE)
    usage = CursorUsage(session) if session and cfg.get("usage_poll_minutes", 5) else None
    if usage is None:
        print("没有配置 Cursor 登录 Cookie（bridge/secrets.toml），额度不会显示")
    watch = parse_agent_ids(cfg.get("cloud_agents", []))     # 只看这些云端任务；空 = 看全部
    if watch:
        print("外包只看这些云端任务:", ", ".join(sorted(watch)))
        if usage is None:
            print("指定了 cloud_agents 但没有 Cursor Cookie（secrets.toml），读不到任务状态，外包不会显示")
    cfg_colors = {k: v for k, v in cfg.get("colors", {}).items()}      # 手动指定的颜色，优先
    warned = False
    print(f"桥接启动，hub = {cfg['hub']}，bots = {cfg_bots or '自动（取自聊天标题）'}")

    while True:
        try:
            now = time.time()
            cmds = [] if args.dry_run else hub.commands(int(poll))
            if args.dry_run:
                time.sleep(poll)
            for c in cmds:
                if c.get("type") not in ("send", "send_private", "voice_play", "answer", "restart_grok"):
                    continue
                try:
                    if c["type"] == "restart_grok":           # 你在 Core2 上按住了左键（发饼）
                        print("重启 Grok Bot…")
                        gw.restart()
                        print("Grok Bot 已重启，放到了前台")
                    elif c["type"] == "answer":                 # 你在屏幕上选了提问卡片的某个选项
                        gw.answer(c["qid"], int(c["index"]), c["option"], c.get("bot", ""), c.get("chat"))
                        print(f"已替你选：{c['option']}（{c.get('title', '')}）")
                    elif c["type"] == "voice_play":
                        gw.voice_play(c.get("to"), hub.clip(c["clip"]))
                        print(f"已说给 {c.get('to') or '群聊'}（{c.get('seconds')} 秒，Grok 听写）")
                    elif c["type"] == "send_private":
                        gw.send_private(c["to"], c["text"])
                        print(f"已私聊 {c['to']}:", c["text"])
                    else:
                        gw.send(c["text"])
                        print("已发送:", c["text"])
                    hub.result(c["id"], True)
                except Exception as e:
                    hub.result(c["id"], False, str(e))
                    print("发送失败:", e)

            if usage is not None and now >= usage_next:        # 额度每几分钟查一次，放到线程里，不卡主循环
                usage_next = now + 60 * float(cfg.get("usage_poll_minutes", 5))
                import threading
                threading.Thread(target=lambda: usage_box.update(ev=usage.fetch()), daemon=True).start()
            if usage_box.get("ev") and not args.dry_run:
                hub.events([usage_box.pop("ev")])
            # 云端任务状态：直接问 Cursor（比读界面准，也不要求打开 Go 的聊天页），默认每 20 秒一次
            owner = cfg.get("cloud_owner", "")
            if usage is not None and owner and now >= agent_next:
                agent_next = now + float(cfg.get("cloud_poll_seconds", 20))

                def _poll():
                    try:
                        agent_box["state"], names = usage.agents(watch)
                        if watch and names != agent_box.get("names"):
                            agent_box["names"] = names
                            print("盯着的云端任务:", "；".join(f"{k} {v}" for k, v in names.items()) or "（列表里没找到）")
                    except Exception as e:
                        agent_box["err"] = type(e).__name__
                import threading as _t
                _t.Thread(target=_poll, daemon=True).start()
            if "state" in agent_box and not args.dry_run:
                st = agent_box.pop("state")
                # 干完的状态只在「刚从跑变成干完」时上报一次（触发包裹落下动画）；平时没有任务就报空
                if st in ("finished",) and agent_sent != "working":
                    st = ""
                if st != agent_sent:
                    agent_sent = st
                    hub.events([{"type": "remote", "name": owner, "state": st}])
            snap = gw.snapshot()
            if snap is None:
                if not warned:
                    print(f"读不到 {gw.title!r} 窗口的内容：窗口不存在，或已最小化（最小化时 Chromium 不生成界面树）")
                    warned = True
                continue
            warned = False
            title = snap["title"]
            is_group = "、" in title or (cfg.get("group") and cfg["group"] in title)
            if is_group or last_members is None:
                members = members_from_title(title, humans)
                last_members = members
                if is_group:
                    gw.group_title = title
            else:
                members = last_members                   # 正停在某个单聊里（录语音时）：工位还按群成员排
            group_title = title if is_group else (gw.group_title or title)
            bots = cfg_bots or select_bots(snap["sidebar"], members)
            if bots and now - last_color_scan > 60 and cfg.get("avatar_colors", True):
                last_color_scan = now                       # 头像颜色基本不变，一分钟扫一次就够
                try:
                    colors.update(gw.avatar_colors(snap["sidebar"], set(bots)))
                except Exception as e:
                    print("读头像颜色失败:", e)
            use = {n: hex_color(colors[n]) for n in bots if n in colors}
            use.update({n: v for n, v in cfg_colors.items() if n in bots})
            if bots and (bots != last_bots or use != sent_colors or now - last_hello > 30):
                sent_colors = dict(use)
                if not args.dry_run:
                    hub.events([{"type": "bots", "group": cfg.get("group") or (group_title if "、" not in group_title else f"群聊·{len(bots)}只"),
                                 "bots": [{"name": n, "color": use[n]} if n in use else n for n in bots]}])
                tracker.statuses.clear()          # hub 可能重启过，状态重新报一遍
                last_notes.clear()
                for lq in last_questions.values():  # 提问卡片也重新报一遍（滚出去看不见的也报，hub 可能重启过）
                    lq[1] = None
                last_bots, last_hello = bots, now

            side = parse_sidebar(snap["sidebar"])
            statuses = {b: side.get(b, "idle") for b in bots}
            messages = []
            for row_name, anchor, chips in snap["rows"]:
                r = parse_row(row_name, anchor, chips)
                if r:
                    who, text, ts = r
                    messages.append((me if who in humans else who, text, ts))
            backlog = title != last_title or last_hello == now     # 启动 / 换了聊天 / hub 可能重启过
            if last_title is not None and title != last_title:
                tracker.rebase(messages)              # 换了个聊天：里面已有的记录不当新消息
            last_title = title
            events = tracker.update(statuses, messages, private=not is_group)
            if backlog and messages:                  # 已有的记录也报一份给 hub，只用来补挂「@你要你回话」的卡片
                events.append({"type": "backlog", "private": not is_group,
                               "messages": [[w, t[:500]] for w, t, _ts in messages[-40:]]})
            # 云端任务：只有"云端 bot 自己的聊天"打开时才看得到它的任务行；看到就上报，看不到不乱清零
            owner = cfg.get("cloud_owner", "")
            rows_a = parse_agent_rows(snap.get("agents", []))
            if owner and not watch and (title == owner or rows_a):     # 指定了 cloud_agents 时只信接口，界面上别的任务行不算
                rs = overall_remote_state(rows_a) if title == owner or rows_a else None
                if rs is not None and (rs != last_remote[0] or now - last_remote[1] > 30):
                    last_remote = (rs, now)
                    events.append({"type": "remote", "name": owner, "state": rs})
            # 没打开的 bot 单聊：预览文字变了，就当作它悄悄发来一句
            pv = sidebar_previews(snap["sidebar"], bots)
            if previews is not None:
                for n, t in pv.items():
                    if statuses.get(n) == "waiting":
                        continue                          # 在等批准时预览是"需要批准：…"，挂公告，不当悄悄话
                    if previews.get(n) != t and not t.startswith(("你 ", "你:", "你：")):
                        events.append({"type": "message", "sender": n, "text": t[:500], "private": True})
            previews = pv
            # 提问卡片：新出现的 / 选项或已选的变了就报；同一个聊天里卡片不见了报 gone。
            # 切到别的聊天看不到不算；被新消息顶上去、滚出渲染范围的也不算（还等着你选）——卡片那一行
            # 还在渲染范围里、卡片却没了，才是在电脑上选掉 / 忽略了
            now_q = {q["qid"]: q for q in snap.get("questions", [])}
            ents = snap.get("entries") or []
            for qid, q in now_q.items():
                sig = (q["title"], q["detail"], tuple(q["options"]), q["recommended"], q["answer"])
                old = last_questions.get(qid)
                ev = {"type": "question", "id": qid, "title": q["title"], "detail": q["detail"],
                      "name": q["who"] if q["who"] and q["who"] not in humans else ("" if is_group else title),
                      "chat": "" if is_group else title, "options": q["options"],
                      "recommended": q["recommended"], "answer": q["answer"]}
                if old is None or old[1] != sig:
                    events.append(ev)
                entry = q["entry"] if q["entry"] is not None else (old[2] if old else None)
                last_questions[qid] = [title, sig, entry, ev]
            for qid, lq in list(last_questions.items()):
                if qid in now_q:
                    continue
                where, sig, entry, ev = lq
                if where == title and (entry is None or (ents and min(ents) <= entry <= max(ents))):
                    picked = next((o for t in snap.get("done_cards", []) if ev["title"] in t
                                   for o in ev["options"] if any(o in x for x in t if x != ev["title"])), "")
                    events.append({"type": "question_gone", "id": qid, "answer": picked})
                    del last_questions[qid]
                elif sig is None:                         # hub 可能重启过：看不见的卡片也再报一遍
                    events.append(ev)
                    lq[1] = (ev["title"], ev["detail"], tuple(ev["options"]), ev["recommended"], ev["answer"])
            # 公告：在等你批准的 bot，把侧边栏里它的预览（"需要批准：…"）报上去
            for n in bots:
                note = pv.get(n, "") if statuses.get(n) == "waiting" else ""
                if last_notes.get(n) != note:
                    last_notes[n] = note
                    events.append({"type": "note", "name": n, "text": note[:200]})
            if args.dry_run:
                print(json.dumps({"title": title, "bots": bots, "statuses": statuses,
                                  "last_messages": [(w, t[:60], ts) for w, t, ts in messages[-3:]],
                                  "new_events": events}, ensure_ascii=False, indent=1))
            else:
                hub.events(events)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print("连不上 hub:", e)
            last_hello = 0.0
            time.sleep(3)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("出错:", type(e).__name__, e)
            time.sleep(2)


if __name__ == "__main__":
    main()
