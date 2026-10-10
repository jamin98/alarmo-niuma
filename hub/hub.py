#!/usr/bin/env python3
"""牛马办公室 hub：收 Windows 桥接上报的群聊状态，渲染画面推给 Alarmo，处理按键。

    python3 hub.py                 # 正常运行
    python3 hub.py --demo          # 不接桥接，用假数据演示
    浏览器打开 http://<debian>:8765/ 是模拟器，不接 Alarmo 也能看效果、点按钮
"""
import argparse
import asyncio
import sys
import io
import itertools
import random
import socket
import struct
import time
from collections import OrderedDict
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
from pathlib import Path

import numpy as np
from aiohttp import web

import device
import scenes
from render import Renderer
from state import Office

HERE = Path(__file__).parent


THEME_NAMES = {"blueprint": "蓝图", "neon": "夜班作战室", "barn": "牛马牧场", "space": "太空舰桥",
               "meeting": "视频会议", "tower": "加班写字楼", "wheel": "牛马摩天轮", "derby": "KPI 赛马场"}
THEME_ORDER = ["blueprint", "neon", "barn", "space", "meeting", "tower", "wheel", "derby"]


class Hub:
    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        o = cfg.get("office", {})
        self.office = Office(
            group=o.get("group", "牛马办公室"),
            humans=tuple(o.get("humans", ["我"])),
            stall_seconds=float(o.get("stall_seconds", 180)),
            sleep_after=float(o.get("sleep_after", 45)),
            show_chat=bool(o.get("show_chat", False)),
            aliases={str(k): str(v) for k, v in cfg.get("names", {}).items()},
            auto_short=bool(o.get("auto_short", True)),
            remote={str(k): str(v) for k, v in cfg.get("remote_bots", {}).items()},
            remote_timeout=float(o.get("remote_timeout", 1800)),
        )
        self.font_path = cfg.get("render", {}).get("font") or None
        self.renderer = Renderer(self.font_path)        # 聊天记录页和额度页
        self.themes = {}
        self.theme = cfg.get("render", {}).get("theme", THEME_ORDER[0])
        if self.theme not in scenes.THEMES:             # 已删掉的旧主题（pixel / classroom / classic）退回默认
            print(f"主题 {self.theme!r} 已不存在，改用 {THEME_ORDER[0]}")
            self.theme = THEME_ORDER[0]
        self.buttons = cfg.get("buttons", {})
        self.office.poke_text = str(self.buttons.get("poke_text", self.office.poke_text))
        self.token = cfg.get("server", {}).get("token", "")
        self.fps = int(cfg.get("device", {}).get("fps", 10))
        self.commands = []                 # 等桥接来取的指令
        self.cmd_event = None          # 在事件循环里按需创建（Python < 3.10 的 Event 会绑定创建时的循环）
        self.cmd_ids = itertools.count(1)
        self.pending = {}                  # id -> 发出时间
        self.pending_ok = {}               # id -> 发成功时的提示
        self.pending_cb = {}               # id -> 桥接回报结果后要做的事 fn(ok, error)
        self.clips = {}                    # 等桥接来取的录音 {id: WAV 字节}
        self.https_port = 0
        self.last_send = 0.0
        self.frame = None
        self.frame_id = 0
        self._jpg = (None, None, b"")
        self.rgb565 = OrderedDict()        # 最近几十帧的 RGB565 画面 {帧号: (240, 320) uint16}，算增量用
        self.device_stats = {}             # Core2 上报的性能统计
        self.rle_log = []                  # 最近 100 次 /frame.rle 请求的计时（排查卡顿用）
        self.demo = demo
        self.link = None
        self.loop = None

    # ---- 设备 ----
    def start_device(self):
        d = self.cfg.get("device", {})
        port = d.get("port", "")
        if not port:
            return
        ts = self.loop.call_soon_threadsafe
        self.link = device.AlarmoLink(
            port,
            on_button=lambda m: ts(self.on_buttons, m),
            on_dial=lambda deg: ts(self.on_dial, deg),
            on_online=lambda ok: ts(self.on_device, ok),
            brightness=float(d.get("brightness", 1.0)),
        )
        self.link.start()

    def on_device(self, ok):
        self.office.device_online = ok

    def on_buttons(self, mask):
        if mask & device.BTN_DIAL:
            self.press("dial")
        if mask & device.BTN_NOTIFY:
            self.press("notify")
        if mask & device.BTN_BACK:
            self.press("back")

    def on_dial(self, deg):
        # 工位画面里：往一个方向转 = 聊天记录，往另一个方向转 = 额度页；记录里继续转 = 翻页；按大按钮回到工位
        if self.office.view == "office":
            self.office.view = "chat" if deg > 0 else "usage"
            self.office.chat_scroll = 0
        elif self.office.view == "usage":
            self.office.view = "office"
        else:
            self.office.chat_scroll += 1 if deg > 0 else -1

    # ---- 按键逻辑 ----
    def press(self, button):
        now = time.time()
        of = self.office
        if button == "notify":              # 右上：换肤
            order = THEME_ORDER
            nxt = order[(order.index(self.theme) + 1) % len(order)] if self.theme in order else order[0]
            self.theme = nxt
            of.view = "office"
            of.say("皮肤: " + THEME_NAMES[nxt], "ok", now)
            return
        if button == "chat":                # Core2 中键：显示 / 隐藏聊天内容；在聊天记录 / 额度页里是回到工位
            if of.view != "office":
                of.view = "office"
                return
            of.show_chat = not of.show_chat
            of.say("显示聊天记录" if of.show_chat else "隐藏聊天记录", "ok", now)
            return
        if button == "back":                # 左上：一键鼓励 —— 老板挨个走到每个人面前发大饼，同时往群里发一句
            if of.tour_at(now):
                of.say("老板正在发饼", "warn", now)
                return
            texts = self.buttons.get("encourage_texts") or ["大家辛苦了"]
            if self.send_text(random.choice(texts), now):
                of.view = "office"
                of.encourage(now)
            return
        if of.view != "office":             # 聊天记录 / 额度页里按大按钮只是返回工位，不抽鞭子、不发消息
            of.view = "office"
            return
        of.whip(now)
        self.send_text(self.buttons.get("dial_text", ""), now)

    def poke(self, i, now=None):
        """点名盯第 i 个 bot：私聊它一句 poke_text，老板跑过去盯着它。"""
        now = now or time.time()
        of = self.office
        if not 0 <= i < len(of.bots):
            return False
        if of.tour_at(now):
            of.say("老板正忙", "warn", now)
            return False
        bot = of.bots[i]
        if not self.send_text(of.poke_text, now, to=self.full_name(bot.name), ok_text="已私聊 " + bot.name):
            return False
        of.view = "office"
        of.poke(i, now)
        if self.demo:                       # 演示模式：过几秒假装它回了一句私聊
            reply = random.choice(["收到老板，马上干", "在干了在干了，别盯了", "老板我刚才在想方案…"])
            asyncio.get_event_loop().call_later(4.0, lambda: of.add_message(bot.name, reply, private=True))
        return True

    def send_text(self, text, now, to=None, ok_text="已发到群里"):
        """让桥接发一句话。to = None 发到群里；to = bot 完整名，私聊它。"""
        of = self.office
        if not text:
            return False
        if now - self.last_send < float(self.buttons.get("cooldown", 3.0)):
            of.say("别点太快", "warn", now)
            return False
        self.last_send = now
        if self.demo:
            of.say(ok_text + "（演示）", "ok", now)
            if to is None:
                of.add_message(of.humans[0], text, now)
            return True
        if not of.bridge_online(now):
            of.say("桥接离线，没发出去", "err", now)
            return False
        cmd = {"type": "send", "text": text}
        if to is not None:
            cmd.update(type="send_private", to=to)
        self.queue(cmd, now, ok_text=ok_text)
        of.say("发送中…", "warn", now)
        return True

    def queue(self, cmd, now, ok_text=None, cb=None, extra=0.0):
        """把一条指令交给桥接（它长轮询 /api/commands 来取）。extra = 比平常多等几秒才算超时。返回指令 id。"""
        cid = next(self.cmd_ids)
        self.commands.append(dict(cmd, id=cid))
        self.pending[cid] = now + extra
        if ok_text:
            self.pending_ok[cid] = ok_text
        if cb:
            self.pending_cb[cid] = cb
        if self.cmd_event:
            self.cmd_event.set()
        return cid

    def full_name(self, short):
        return next((f for f, sh in self.office.full2short.items() if sh == short), short)

    # ---- 按住说话 ----
    # 在你手上的设备（手机 / 电脑浏览器、Core2）上录音，松手把 16kHz 单声道 WAV 传上来；
    # 桥接把它播放进跑 Grok Bot 那台电脑上的虚拟麦克风，同时点 Grok Bot 自带的「开始语音输入」，
    # 由 Grok 听写成文字后发出去。
    VOICE_MAX = 90.0                   # 按住后这么久还没把录音传上来（比如网页关了），就当取消

    def voice_start(self, i, now=None):
        """按住：i = bot 下标（私聊它），-1 = 发到群里。录音在你手上的设备上进行，这里只记下发给谁、亮出"录音中"。"""
        now = now or time.time()
        of = self.office
        if of.voice and of.voice["state"] == "sending":
            of.say("上一段还在发", "warn", now)
            return False
        to = of.bots[i].name if 0 <= i < len(of.bots) else None
        of.voice = {"to": to, "state": "recording", "t0": now, "rec_t0": now, "id": next(self.cmd_ids)}
        of.view = "office"
        return True

    def voice_cancel(self, now=None):
        of = self.office
        if of.voice and of.voice["state"] == "recording":
            of.voice = None
            of.say("已取消", "warn", now)
            return True
        return False

    def voice_clip(self, wav, now=None):
        """松手：收到录音。交给桥接播放进虚拟麦克风，让 Grok 听写并发出去。返回 (ok, 错误)。"""
        now = now or time.time()
        of = self.office
        v = of.voice
        if not v or v["state"] != "recording":
            return False, "没在录音"
        secs = wav_seconds(wav)
        if secs is None:
            of.voice = None
            of.say("录音格式不对", "err", now)
            return False, "要 16 位 PCM 的 WAV"
        if secs < 0.6:
            of.voice = None
            of.say("说话时间太短", "warn", now)
            return False, "太短"
        who = "群里" if not v["to"] else v["to"]
        if self.demo:
            of.voice = None
            of.say(f"已说给 {who}（演示，{secs:.0f} 秒）", "ok", now)
            return True, ""
        if not of.bridge_online(now):
            of.voice = None
            of.say("桥接离线，没发出去", "err", now)
            return False, "桥接离线"
        self.clips = {v["id"]: wav}
        self.queue({"type": "voice_play", "to": self.full_name(v["to"]) if v["to"] else None,
                    "clip": v["id"], "seconds": round(secs, 1)},
                   now, cb=lambda ok, err: self._voice_done(ok, err, who), extra=secs + 20)
        v["state"] = "sending"
        return True, ""

    def _voice_done(self, ok, error, who):
        self.office.voice = None
        self.clips = {}
        if ok:
            self.office.say("已说给 " + who, "ok")
        else:
            self.office.say("没发出去: " + error[:14], "err")

    def painter(self):
        if self.office.view in ("chat", "usage"):
            return self.renderer
        if self.theme not in self.themes:
            self.themes[self.theme] = scenes.THEMES[self.theme](self.font_path)
        return self.themes[self.theme]

    # ---- 主循环 ----
    async def render_loop(self):
        period = 1.0 / self.fps
        next_t = time.time()
        while True:
            t = time.time()
            self.office.tick(t)
            for cid, t0 in list(self.pending.items()):
                if t - t0 > 30:
                    del self.pending[cid]
                    self.pending_ok.pop(cid, None)
                    cb = self.pending_cb.pop(cid, None)
                    if cb:
                        cb(False, "桥接没回应")
                    else:
                        self.office.say("发送超时", "err", t)
            v = self.office.voice
            if v and v["state"] == "recording" and t - v["rec_t0"] > self.VOICE_MAX:
                self.voice_cancel(t)
            img = self.painter().render(self.office, t)
            self.frame = img
            self.frame_id += 1
            if self.link:
                self.link.submit(np.asarray(img))
            # 按下一帧的绝对时间等：Windows 上 asyncio.sleep 只有约 15.6ms 精度、常常提前醒，醒早了就再补
            next_t = max(next_t + period, time.time() - period)
            await asyncio.sleep(0.005)
            while next_t - time.time() > 0.001:
                await asyncio.sleep(next_t - time.time())

    async def demo_loop(self):
        of = self.office
        names = ["架构师", "后端", "前端", "测试", "运维", "文档"]
        colors = ["#f97316", "#3b82f6", "#22c55e", "#a855f7", "#ef4444", "#eab308"]
        of.group = "订单簿重构"
        of.set_bots([{"name": n, "color": c} for n, c in zip(names, colors)])
        lines = ["方案写好了，接口见 docs/design.md", "单测跑过了 42/42", "我在改增量更新的 diff 逻辑",
                 "CI 挂了，在看日志", "PR 已提，等 review", "部署脚本更新完毕", "文档补了一节回滚步骤"]
        while True:
            of.bridge_seen = time.time()
            a = random.choice(names)
            roll = random.random()
            if roll < 0.12:
                of.add_message(a, random.choice(["老板，这个需求我觉得有坑", "悄悄说一句，CI 又红了", "能不能加点额度"]), private=True)
            elif roll < 0.35:
                b = random.choice([n for n in names if n != a])
                of.add_message(a, f"{random.choice(lines)}，@{b} 接一下")
            elif roll < 0.6:
                of.set_status(a, "typing")
                of.add_message(a, random.choice(lines))
            elif roll < 0.75:
                of.set_status(a, "waiting")
            else:
                of.set_status(a, "idle")
            # 偶尔全员卡住，演示"卡住"的效果
            if random.random() < 0.12:
                for n in names:
                    of.set_status(n, "idle")
                of.last_activity = time.time() - of.stall_seconds - 30
                for b in of.bots:
                    b.typing_until = 0
                for _ in range(120):
                    of.bridge_seen = time.time()
                    if of.silence(time.time()) < 20:
                        break
                    await asyncio.sleep(0.1)
            await asyncio.sleep(random.uniform(1.5, 4.0))

    # ---- HTTP ----
    def check(self, request):
        if self.token and request.headers.get("X-Token") != self.token:
            raise web.HTTPUnauthorized(text="bad token")

    def apply_event(self, ev, now):
        of = self.office
        t = ev.get("type")
        if t == "message":
            of.add_message(str(ev.get("sender", "?")), str(ev.get("text", "")), now,
                           private=bool(ev.get("private", False)))
        elif t == "status":
            of.set_status(str(ev["name"]), str(ev.get("status", "idle")), now)
        elif t == "handoff":
            of.handoff(str(ev["src"]), str(ev["dst"]), now)
        elif t == "usage":
            of.set_usage(ev, now)
        elif t == "remote":
            of.set_remote(str(ev["name"]), str(ev.get("state", "")), now)
        elif t == "note":
            of.set_note(str(ev["name"]), str(ev.get("text", "")))
        elif t == "bots":
            if ev.get("group"):
                of.group = str(ev["group"])
            of.set_bots(list(ev.get("bots", [])), now)
        else:
            raise web.HTTPBadRequest(text=f"unknown event type: {t!r}")

    async def h_events(self, request):
        self.check(request)
        body = await request.json()
        now = time.time()
        if request.query.get("from") != "bot":
            self.office.bridge_seen = now
        for ev in body if isinstance(body, list) else [body]:
            self.apply_event(ev, now)
        return web.json_response({"ok": True})

    async def h_commands(self, request):
        self.check(request)
        self.office.bridge_seen = time.time()
        wait = min(30.0, float(request.query.get("wait", "25")))
        if self.cmd_event is None:
            self.cmd_event = asyncio.Event()
        if not self.commands:
            self.cmd_event.clear()
            try:
                await asyncio.wait_for(self.cmd_event.wait(), wait)
            except asyncio.TimeoutError:
                pass
        self.office.bridge_seen = time.time()
        cmds, self.commands = self.commands, []
        return web.json_response({"commands": cmds})

    async def h_result(self, request):
        self.check(request)
        body = await request.json()
        self.pending.pop(body.get("id"), None)
        cb = self.pending_cb.pop(body.get("id"), None)
        if cb:
            cb(bool(body.get("ok")), str(body.get("error", "")))
            return web.json_response({"ok": True})
        ok_text = self.pending_ok.pop(body.get("id"), "已发到群里")
        if body.get("ok"):
            self.office.say(ok_text, "ok")
        else:
            self.office.say("发送失败: " + str(body.get("error", ""))[:16], "err")
        return web.json_response({"ok": True})

    async def h_button(self, request):
        self.check(request)
        body = await request.json()
        if "dial" in body:
            self.on_dial(float(body["dial"]))
        else:
            self.press(str(body.get("button", "dial")))
        return web.json_response({"ok": True})

    async def h_poke(self, request):
        """点名：{"x": .., "y": ..}（屏幕坐标 0..319 / 0..239，按当前主题找点中的 bot）或 {"name": 短名}。"""
        self.check(request)
        body = await request.json()
        of = self.office
        if "name" in body:
            i = of.index(str(body["name"]))
        else:
            if of.view != "office":
                return web.json_response({"ok": False, "error": "不在工位画面"})
            i = self.painter().bot_at(of, float(body.get("x", -1)), float(body.get("y", -1)))
        if i < 0:
            return web.json_response({"ok": False, "error": "没点中"})
        return web.json_response({"ok": self.poke(i), "bot": of.bots[i].name})

    async def h_voice(self, request):
        """按住说话。{"action": "start", "x": .., "y": ..}：按住的是某只牛马 = 私聊它，按住"我" = 发到群里；
        {"action": "cancel"}：取消。松手后把录音 POST 到 /api/voice/clip。"""
        self.check(request)
        body = await request.json()
        of = self.office
        if body.get("action") in ("cancel", "stop"):
            return web.json_response({"ok": self.voice_cancel()})
        if of.view != "office":
            return web.json_response({"ok": False, "error": "不在工位画面"})
        x, y = float(body.get("x", -1)), float(body.get("y", -1))
        theme = self.painter()
        i = theme.bot_at(of, x, y)
        if i < 0 and not theme.me_at(of, x, y):
            return web.json_response({"ok": False, "error": "没按中"})
        return web.json_response({"ok": self.voice_start(i), "to": of.bots[i].name if i >= 0 else "群聊"})

    async def h_voice_clip(self, request):
        """松手：请求体是 16kHz 单声道 16 位 PCM 的 WAV。"""
        self.check(request)
        ok, err = self.voice_clip(await request.read())
        return web.json_response({"ok": ok, "error": err})

    async def h_voice_audio(self, request):
        """桥接来取录音。"""
        self.check(request)
        wav = self.clips.get(int(request.query.get("id", "0")))
        if wav is None:
            raise web.HTTPNotFound(text="没有这段录音")
        return web.Response(body=wav, content_type="audio/wav")

    async def h_theme(self, request):
        self.check(request)
        body = await request.json()
        name = str(body.get("theme", ""))
        if name not in scenes.THEMES:
            raise web.HTTPBadRequest(text="unknown theme")
        self.theme = name
        return web.json_response({"ok": True, "theme": name})

    async def h_options(self, request):
        self.check(request)
        body = await request.json()
        if "show_chat" in body:
            self.office.show_chat = bool(body["show_chat"])
        return web.json_response({"ok": True, "show_chat": self.office.show_chat})

    async def h_frame(self, request):
        buf = io.BytesIO()
        (self.frame or self.renderer.render(self.office, time.time())).save(buf, "PNG")
        return web.Response(body=buf.getvalue(), content_type="image/png",
                            headers={"Cache-Control": "no-store"})

    def _jpeg(self, q):
        """当前帧的 JPEG，同一帧同一画质只编码一次（多个客户端共用）。"""
        fid, jq, data = self._jpg
        if fid != self.frame_id or jq != q:
            buf = io.BytesIO()
            img = self.frame or self.renderer.render(self.office, time.time())
            img.convert("RGB").save(buf, "JPEG", quality=q, subsampling=0)
            fid, data = self.frame_id, buf.getvalue()
            self._jpg = (fid, q, data)
        return fid, data

    # ---- Core2 用的增量画面：只传变了的 16x16 小块，RGB565 + 行程压缩，Core2 不用解 JPEG ----
    RLE_TILE = 16                      # 以前是 40：抽鞭子时一帧 40~60KB，改小块后少传 20~40%（Core2 不用改，矩形大小在数据里）

    def _frame565(self):
        """当前帧的 RGB565（按帧号缓存最近 40 帧）。"""
        fid = self.frame_id
        arr = self.rgb565.get(fid)
        if arr is None:
            img = np.asarray((self.frame or self.renderer.render(self.office, time.time())).convert("RGB"), dtype=np.uint16)
            arr = ((img[:, :, 0] >> 3) << 11) | ((img[:, :, 1] >> 2) << 5) | (img[:, :, 2] >> 3)
            self.rgb565[fid] = arr
            while len(self.rgb565) > 40:
                self.rgb565.popitem(last=False)
        return fid, arr

    @staticmethod
    def rle565(px):
        """一维 uint16 像素 -> [(个数-1, 低字节, 高字节)…]，一段最长 256 个。"""
        starts = np.concatenate(([0], np.flatnonzero(px[1:] != px[:-1]) + 1))
        lens = np.diff(np.concatenate((starts, [len(px)])))
        pieces = (lens + 255) // 256
        idx = np.repeat(np.arange(len(starts)), pieces)
        first = np.concatenate(([0], np.cumsum(pieces)[:-1]))
        ordinal = np.arange(len(idx)) - first[idx]
        plen = np.where(ordinal == pieces[idx] - 1, lens[idx] - 256 * (pieces[idx] - 1), 256)
        out = np.empty((len(idx), 3), np.uint8)
        out[:, 0] = plen - 1
        v = px[starts[idx]]
        out[:, 1] = v & 0xFF
        out[:, 2] = v >> 8
        return out.tobytes()

    def frame_rle(self, base):
        """和第 base 帧比，变了的小块按行合并成矩形。base 不在缓存里（或 0）就整帧发。
        格式：b"NRLE" + <帧号 u32><整帧 u8><矩形数 u16>，每个矩形 <x y w h u16><字节数 u32> + 行程数据（逐行）。"""
        fid, cur = self._frame565()
        prev = self.rgb565.get(base) if base else None
        T = self.RLE_TILE
        rows, cols = cur.shape[0] // T, cur.shape[1] // T
        if prev is None:
            changed = np.ones((rows, cols), bool)
        else:
            changed = (prev != cur).reshape(rows, T, cols, T).any(axis=(1, 3))
        parts = []
        for ty in range(rows):
            tx = 0
            while tx < cols:
                if not changed[ty, tx]:
                    tx += 1
                    continue
                x0 = tx
                while tx < cols and changed[ty, tx]:
                    tx += 1
                x, y, w = x0 * T, ty * T, (tx - x0) * T
                data = self.rle565(np.ascontiguousarray(cur[y:y + T, x:x + w]).reshape(-1))
                parts.append(struct.pack("<HHHHI", x, y, w, T, len(data)) + data)
        return fid, b"NRLE" + struct.pack("<IBH", fid, prev is None, len(parts)) + b"".join(parts)

    async def h_frame_rle(self, request):
        """Core2 取画面：?after=<帧号> 等到有新帧再返回（最多 1 秒）；?base=<Core2 屏幕上现在的帧号> 只发变了的部分。"""
        try:
            after = int(request.query.get("after", "-1"))
            base = int(request.query.get("base", "0"))
        except ValueError:
            after, base = -1, 0
        t_in = time.time()
        end = t_in + 1.0
        while self.frame_id == after and time.time() < end:
            await asyncio.sleep(0.005)
        t_wait = time.time()
        fid, body = self.frame_rle(base)
        t_out = time.time()
        # 排查卡顿用：记下每次取帧在 hub 上花的时间、和上一次回复之间隔了多久、是不是新连接
        peer = request.transport.get_extra_info("peername") if request.transport else None
        log = self.rle_log
        prev = log[-1] if log else None
        log.append({"in": t_in, "out": t_out, "wait": t_wait - t_in, "enc": t_out - t_wait, "bytes": len(body),
                    "gap": (t_in - prev["out"]) if prev else 0.0, "port": peer[1] if peer else 0,
                    "skipped": max(0, fid - after - 1) if after >= 0 else 0})
        if len(log) > 100:
            del log[0]
        resp = web.Response(body=body, content_type="application/octet-stream",
                            headers={"Cache-Control": "no-store", "X-Frame-Id": str(fid)})
        return resp

    async def h_frame_stream(self, request):
        """Core2 取画面（推流版）：一个连接一直开着，hub 有新帧就推一条 <长度 u32> + NRLE 增量帧。
        hub 自己记着推过去的最后一帧，下一条就以它为基准。每推一条都等 Core2 在 TCP 层把它全部确认收到，
        再算下一条：路上最多只有一帧，Core2 慢的时候中间的帧直接跳过，延迟不会越积越大。
        比每帧一问一答快：实测 Core2 收完一帧到发出下一个请求要 ~100ms（ESP32 HTTPClient 内部），推流没有这一步。
        Core2 要整帧（刚连上、从设置回来）就断开重连：新连接第一条一定是整帧。"""
        resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream", "Cache-Control": "no-store"})
        resp.force_close()
        await resp.prepare(request)
        tr = request.transport
        if tr is not None:
            tr.set_write_buffer_limits(high=1, low=0)               # 有没发出去的就等：帧不在 hub 里排队
        writer = resp._payload_writer
        sock = tr.get_extra_info("socket") if tr is not None else None
        await writer.drain()
        for _ in range(250):                              # 先等响应头被对方确认，再开始记账
            ti = tcp_info(sock) if sock is not None else None
            if ti is None or ti.BytesInFlight == 0:
                break
            await asyncio.sleep(0.002)
        acks = AckTracker(sock)
        base = last = 0
        try:
            while True:
                t_in = time.time()
                while self.frame_id == last:
                    await asyncio.sleep(0.005)
                t_wait = time.time()
                fid, body = self.frame_rle(base)
                t_enc = time.time()
                msg = struct.pack("<I", len(body)) + body
                await resp.write(msg)
                await writer.drain()                                    # aiohttp 默认攒满 64KB 才等，这里每条都等
                acks.wrote(len(msg))
                end = time.time() + 2.0
                while time.time() < end:                                # 等 Core2 把这一帧全部确认收到
                    if not acks.pending():                              # 0 = 收完了；None = 系统不支持，不等
                        break
                    await asyncio.sleep(0.002)
                t_out = time.time()
                prev = self.rle_log[-1] if self.rle_log else None
                self.rle_log.append({"in": t_in, "out": t_out, "wait": t_wait - t_in, "enc": t_enc - t_wait,
                                     "send": t_out - t_enc, "bytes": len(body), "gap": 0.0,
                                     "port": tr.get_extra_info("peername")[1] if tr else 0,
                                     "skipped": max(0, fid - last - 1) if last else 0})
                if len(self.rle_log) > 100:
                    del self.rle_log[0]
                base = last = fid
        except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError, RuntimeError):
            pass
        return resp

    async def h_device_stats(self, request):
        """Core2 每几秒报一次性能统计（帧率、各环节耗时），出现在 /api/state 的 core2 里，方便远程排查卡顿。"""
        self.check(request)
        self.device_stats = dict(await request.json(), ts=round(time.time()))
        return web.json_response({"ok": True})

    async def h_frame_jpg(self, request):
        """给 M5Stack Core2 这类自己联网取画面的小屏：320x240 JPEG，体积比 PNG 小、解码快。
        ?q=1..95 画质，默认 80；响应头 X-Frame-Id 是帧序号。
        ?after=<帧序号>：等到有比它新的一帧再返回（最多等 1 秒），客户端就不会重复下载同一帧。"""
        q = max(1, min(95, int(request.query.get("q", "80"))))
        try:
            after = int(request.query.get("after", "-1"))
        except ValueError:
            after = -1
        end = time.time() + 1.0
        while self.frame_id == after and time.time() < end:      # hub 重启后帧号变小，也算"新的"
            await asyncio.sleep(0.005)
        fid, data = self._jpeg(q)
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store", "X-Frame-Id": str(fid)})

    async def h_stream(self, request):
        """MJPEG 推流：一个连接一直开着，画面变了才推一帧，不用客户端反复请求。
        浏览器里 <img src="/stream.mjpg"> 就能播。?q= 同上。"""
        q = max(1, min(95, int(request.query.get("q", "85"))))
        resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame",
                                           "Cache-Control": "no-store"})
        await resp.prepare(request)
        last = -1
        try:
            while True:
                if self.frame_id != last:
                    last, data = self._jpeg(q)
                    head = ("--frame" + chr(13) + chr(10) + "Content-Type: image/jpeg" + chr(13) + chr(10) + "Content-Length: %d" % len(data) + chr(13) + chr(10) + chr(13) + chr(10)).encode()
                    await resp.write(head + data + bytes([13, 10]))
                await asyncio.sleep(0.03)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def h_state(self, request):
        snap = self.office.snapshot(time.time())
        snap["device"] = self.link.stats if self.link else None
        snap["theme"] = self.theme
        snap["https_port"] = self.https_port
        snap["core2"] = self.device_stats
        log = [e for e in self.rle_log if time.time() - e["out"] < 10]
        if log:
            avg = lambda k: round(sum(e[k] for e in log) / len(log) * 1000, 1)
            snap["frame_rle"] = {"frames_10s": len(log), "hub_wait_ms": avg("wait"), "hub_encode_ms": avg("enc"),
                                 "hub_send_ms": round(sum(e.get("send", 0) for e in log) / len(log) * 1000, 1),
                                 "gap_ms": avg("gap"), "avg_bytes": sum(e["bytes"] for e in log) // len(log),
                                 "new_connections": len({e["port"] for e in log}), "skipped_frames": sum(e["skipped"] for e in log)}
        return web.json_response(snap)

    async def h_index(self, request):
        return web.FileResponse(HERE / "static" / "sim.html")

    async def on_startup(self, app):
        self.loop = asyncio.get_running_loop()
        self.start_device()
        app["tasks"] = [asyncio.create_task(self.render_loop())]
        if self.demo:
            app["tasks"].append(asyncio.create_task(self.demo_loop()))

    def app(self):
        app = web.Application(client_max_size=8 * 1024 * 1024)      # 录音最长约 60 秒，约 2MB
        app.add_routes([
            web.get("/", self.h_index),
            web.get("/frame.png", self.h_frame),
            web.get("/frame.jpg", self.h_frame_jpg),
            web.get("/frame.rle", self.h_frame_rle),
            web.get("/frame.stream", self.h_frame_stream),
            web.post("/api/device_stats", self.h_device_stats),
            web.get("/stream.mjpg", self.h_stream),
            web.get("/api/state", self.h_state),
            web.post("/api/events", self.h_events),
            web.get("/api/commands", self.h_commands),
            web.post("/api/result", self.h_result),
            web.post("/api/button", self.h_button),
            web.post("/api/poke", self.h_poke),
            web.post("/api/voice", self.h_voice),
            web.post("/api/voice/clip", self.h_voice_clip),
            web.get("/api/voice/audio", self.h_voice_audio),
            web.post("/api/theme", self.h_theme),
            web.post("/api/options", self.h_options),
        ])
        app.on_startup.append(self.on_startup)
        return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--demo", action="store_true", help="用假数据演示，不需要桥接")
    ap.add_argument("--no-device", action="store_true", help="不连 Alarmo，只开模拟器")
    ap.add_argument("--list-ports", action="store_true", help="列出本机串口并标出 Alarmo，然后退出")
    args = ap.parse_args()
    if args.list_ports:
        from serial.tools import list_ports
        mine = device.alarmo_ports()
        for p in list_ports.comports():
            print(("* " if p.device in mine else "  ") + f"{p.device}  VID:PID={p.vid or 0:04x}:{p.pid or 0:04x}  {p.description}")
        print("\n带 * 的是 Alarmo。" if mine else "\n没有发现 Alarmo（固件装好后才会出现）。")
        return
    cfg_path = Path(args.config)
    if not cfg_path.exists() and cfg_path == HERE / "config.toml" and (HERE / "config.example.toml").exists():
        import shutil                     # 第一次运行：从示例复制一份（config.toml 不进 git）
        shutil.copyfile(HERE / "config.example.toml", cfg_path)
        print(f"已从 config.example.toml 复制出 {cfg_path}，按需修改")
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    if args.no_device:
        cfg.setdefault("device", {})["port"] = ""
    try:
        asyncio.run(serve(Hub(cfg, demo=args.demo), cfg.get("server", {})))
    except KeyboardInterrupt:
        pass


async def serve(hub, s):
    """http 给 Alarmo / 桥接 / 本机网页用；https 给手机和别的电脑的浏览器用（只有 https 页面才能用麦克风录音）。"""
    host, port = s.get("host", "0.0.0.0"), int(s.get("port", 8765))
    runner = web.AppRunner(hub.app())
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f"======== Running on http://{host}:{port} ========")
    hp = int(s.get("https_port", 0) or 0)
    if hp:
        ctx = ssl_context(HERE / "certs", s)
        if ctx:
            await web.TCPSite(runner, host, hp, ssl_context=ctx).start()
            hub.https_port = hp
            ips = [ip for ip in local_ips() if not ip.startswith("127.")]
            print(f"======== https 端口 {hp}：手机 / 别的电脑用浏览器打开 "
                  + "、".join(f"https://{ip}:{hp}" for ip in ips[:3]) + "（自签证书，第一次要点「继续访问」）")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def local_ips():
    import socket
    ips = {"127.0.0.1"}
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:                                    # 默认路由那张网卡的地址
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            ips.add(sk.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def ssl_context(folder, s):
    """https 证书：[server] cert/key 指定了就用；否则用 openssl 生成一张自签证书放在 hub/certs/。"""
    import shutil
    import socket
    import ssl
    import subprocess
    crt, key = Path(s.get("cert") or folder / "hub.crt"), Path(s.get("key") or folder / "hub.key")
    if not (crt.exists() and key.exists()):
        exe = shutil.which("openssl") or next((p for p in (r"C:\Program Files\Git\usr\bin\openssl.exe",
                                                          r"C:\Program Files\Git\mingw64\bin\openssl.exe")
                                               if Path(p).exists()), None)
        if not exe:
            print("没有 openssl，https 没开：装 Git for Windows / apt install openssl，或在 [server] 里配置 cert、key")
            return None
        folder.mkdir(parents=True, exist_ok=True)
        san = ",".join([f"IP:{ip}" for ip in local_ips()] + ["DNS:localhost", f"DNS:{socket.gethostname()}"])
        r = subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
                            "-keyout", str(key), "-out", str(crt), "-subj", "/CN=niuma-hub",
                            "-addext", "subjectAltName=" + san], capture_output=True, text=True)
        if r.returncode != 0:
            print("生成 https 证书失败，https 没开：", (r.stderr or "").strip()[-200:])
            return None
        print("已生成自签证书", crt)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(crt), str(key))
    return ctx


def tcp_info(sock):
    """Windows：一个 TCP 连接的内部状态（已发未确认字节、RTT、对方窗口…），拿不到返回 None。排查卡顿用。"""
    if sys.platform != "win32" or sock is None:
        return None
    import ctypes
    from ctypes import wintypes

    class TCP_INFO_v0(ctypes.Structure):
        _fields_ = [("State", ctypes.c_int), ("Mss", wintypes.ULONG), ("ConnectionTimeMs", ctypes.c_uint64),
                    ("TimestampsEnabled", ctypes.c_ubyte), ("RttUs", wintypes.ULONG), ("MinRttUs", wintypes.ULONG),
                    ("BytesInFlight", wintypes.ULONG), ("Cwnd", wintypes.ULONG), ("SndWnd", wintypes.ULONG),
                    ("RcvWnd", wintypes.ULONG), ("RcvBuf", wintypes.ULONG), ("BytesOut", ctypes.c_uint64),
                    ("BytesIn", ctypes.c_uint64), ("BytesReordered", wintypes.ULONG), ("BytesRetrans", wintypes.ULONG),
                    ("FastRetrans", wintypes.ULONG), ("DupAcksIn", wintypes.ULONG), ("TimeoutEpisodes", wintypes.ULONG),
                    ("SynRetrans", ctypes.c_ubyte)]
    ver, info, ret = wintypes.DWORD(0), TCP_INFO_v0(), wintypes.DWORD(0)
    ws = ctypes.windll.ws2_32
    ws.WSAIoctl.argtypes = [ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
    if ws.WSAIoctl(sock.fileno(), 0xD8000027, ctypes.byref(ver), 4, ctypes.byref(info), ctypes.sizeof(info),
                   ctypes.byref(ret), None, None) != 0:
        return None
    return info


class AckTracker:
    """这条连接上，hub 写出去的字节里还有多少对方没确认（包括还卡在系统发送缓冲里没发出去的）。
    Windows：SIO_TCP_INFO 的 BytesInFlight 只算已经发上网线的，所以改用"对方已确认的总字节 ≈ 发出 - 在路上"
    （重传不扣：就算 BytesOut 里含重传，也只是偶尔早一点推下一帧，不会卡住），
    和 hub 自己写出去的总字节比；Linux：TIOCOUTQ 本来就是"没发 + 没确认"。都拿不到时 pending() 返回 None。"""

    def __init__(self, sock):
        self.sock, self.written, self.base = sock, 0, None
        if sys.platform == "win32":
            ti = tcp_info(sock)
            if ti is not None:                            # 从这里开始算（响应头已经写出去了）
                self.base = ti.BytesOut - ti.BytesInFlight

    def wrote(self, n):
        self.written += n

    def pending(self):
        if self.sock is None:
            return None
        if sys.platform == "win32":
            ti = tcp_info(self.sock)
            if ti is None or self.base is None:
                return None
            acked = ti.BytesOut - ti.BytesInFlight - self.base
            return max(0, self.written - acked)
        try:
            import fcntl
            import termios
            return struct.unpack("i", fcntl.ioctl(self.sock.fileno(), termios.TIOCOUTQ, b"\0\0\0\0"))[0]
        except (OSError, ImportError, AttributeError):
            return None


def wav_seconds(wav):
    """WAV 时长（秒）；不是 16 位 PCM 的 WAV 返回 None。"""
    import wave
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() not in (1, 2):
                return None
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


if __name__ == "__main__":
    main()
