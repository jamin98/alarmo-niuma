"""几套可选的画面主题。每个主题都是 render(office, now) -> 320x240 的 PIL 图像。

    blueprint  A 建筑蓝图：纯俯视平面图，右侧竖黑板
    neon       B 夜班作战室：霓虹俯视图，大家围着会议桌
    barn       C 牛马牧场：草垛当椅背的牛棚，右边院子里农场主
    space      D 太空舰桥：座舱椅 + 全息屏，右边舰长台和舷窗
    meeting    E 视频会议：4x2 摄像头宫格 + 实时字幕 + 底部工具栏
    tower      F 加班写字楼：夜景大楼剖面，3 层 x 2 间，中间电梯井，楼顶广告牌
    wheel      G 牛马摩天轮：轿厢绕圈转，干活的人越多转得越快
    derby      H KPI 赛马场：一人一条赛道，跑得越远说明最近越活跃

共同规则：
  - 牛马全身颜色由 bot 自己的颜色生成（没给颜色就按名字算一个固定的）
  - 群聊发言：身边冒正常气泡当字幕，同时写到黑板上
  - 私聊我：虚线气泡的悄悄话，不上黑板
"""
import colorsys
import hashlib
import math
import time
from datetime import datetime
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from render import find_font, fmt_dur
from state import T_BACK, T_DROP, T_GO, T_WHIP_END, T_WHIP_HIT, T_WHIP_JUMP

W, H = 320, 240
WHITE = (244, 244, 248)
INK = (24, 24, 30)
RED = (235, 64, 52)
YELLOW = (250, 204, 70)
GREEN = (96, 214, 130)
CHALK = (232, 236, 226)
CODE = [(120, 220, 130), (110, 190, 255), (255, 190, 110), (230, 120, 200)]


# ------------------------------------------------------------------ 颜色
def _hls(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return (int(r * 255), int(g * 255), int(b * 255))


def name_color(name):
    """没拿到 bot 颜色时，按名字算一个固定的颜色。"""
    h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16) / 0xFFFFFF
    return _hls(h, 0.55, 0.62)


@lru_cache(maxsize=256)
def palette(color):
    """由一个 bot 颜色派生出整只牛马的配色。"""
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in color))
    fl = min(0.64, max(0.40, l))
    return {
        "base": color,
        "fur": _hls(h, fl, s),
        "dark": _hls(h, fl * 0.55, s),
        "deep": _hls(h, fl * 0.34, s),
        "light": _hls(h, fl + 0.20, s * 0.95),
        "muzzle": _hls(h, 0.88, s * 0.55),
        "glow": _hls(h, 0.66, min(1.0, s * 1.2 + 0.1)),
    }


def bot_pal(bot):
    return palette(tuple(bot.color) if bot.color else name_color(bot.name))


def species(i):
    return "ox" if i % 2 == 0 else "horse"


# ------------------------------------------------------------------ 状态 -> 姿势
def pose_of(office, i, now):
    bot = office.bots[i]
    tw = now - office.whip_t0
    if any(h.src == i for h in office.handoffs):
        return "away"
    if bot.say_private and not bot.say_inplace and now < bot.say_until:
        return "whisper"
    if T_WHIP_HIT <= tw < T_WHIP_JUMP:
        return "jump"
    if T_WHIP_JUMP <= tw < T_WHIP_END:
        return "panic"
    if office.quota_blocked():
        return "sleep"                                  # Grok Bot 额度用完了，大家只能趴着等重置
    if office.outsourced(bot.name, now):
        return "cloud"                                  # 活外包给云端了：头顶一朵云，不睡觉也不算卡住
    if office.cheering(i, now):
        return "jump"                                   # 老板把饼递过来：举手欢呼
    if office.stared(i, now):
        return "panic"                                  # 老板点名盯着：哆嗦着狂敲键盘
    st = office.effective_status(bot, now)
    if st == "typing":
        return "type"
    if st == "waiting":
        return "wait"
    idle_for = now - max(bot.status_since, bot.typing_until, bot.say_until)
    if office.stalled(now) or idle_for > office.sleep_after:
        return "sleep"
    return "idle"


def talking(bot, now):
    """头顶要冒的对话框文字：群聊发言，或者回复点名的私聊。"""
    if bot.say_text and now < bot.say_until and (not bot.say_private or bot.say_inplace):
        return bot.say_text
    return None


def office_remote(office, bot):
    return bool(office and office.remote_label(bot.name))


def ease(p):
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p)


def handoff_phase(h, now):
    """-> (位置 0..1, 手里有没有文件, 是否已送达)"""
    t = now - h.t0
    if t < T_GO:
        return ease(t / T_GO), True, False
    if t < T_GO + T_DROP:
        return 1.0, False, True
    return 1 - ease((t - T_GO - T_DROP) / T_BACK), False, True


def whisper_phase(bot, now, walk=0.7):
    """走到老板身边 -> 说悄悄话 -> 走回来。返回 (位置 0..1, 是否正在说)"""
    t, left = now - bot.say_t0, bot.say_until - now
    if t < walk:
        return ease(t / walk), False
    if left < walk:
        return ease(left / walk), False
    return 1.0, True


# ------------------------------------------------------------------ 像素头像（正脸）
def face(d, cx, top, sp, P, eye="open", mouth=False):
    """正脸，约 18x16 逻辑像素，cx 为中线。"""
    fur, dark, muz = P["fur"], P["dark"], P["muzzle"]
    if sp == "ox":
        horn = (246, 238, 212)
        d.line([(cx - 7, top + 2), (cx - 9, top + 1), (cx - 9, top - 1)], horn)
        d.line([(cx + 6, top + 2), (cx + 8, top + 1), (cx + 8, top - 1)], horn)
        d.rectangle([cx - 9, top + 4, cx - 7, top + 5], dark)
        d.rectangle([cx + 6, top + 4, cx + 8, top + 5], dark)
        d.rectangle([cx - 6, top + 1, cx + 5, top + 9], fur)
        d.rectangle([cx - 5, top, cx + 4, top], fur)
        d.rectangle([cx - 2, top, cx + 1, top + 1], dark)
        d.rectangle([cx - 5, top + 8, cx + 4, top + 12], muz)
        d.point([(cx - 3, top + 10), (cx + 2, top + 10)], dark)
        ex, ey, my = 4, top + 4, top + 12
    else:
        d.polygon([(cx - 6, top + 1), (cx - 5, top - 3), (cx - 3, top)], fur)
        d.polygon([(cx + 5, top + 1), (cx + 4, top - 3), (cx + 2, top)], fur)
        d.rectangle([cx - 5, top, cx + 4, top + 8], fur)
        d.rectangle([cx - 4, top + 8, cx + 3, top + 13], fur)
        d.rectangle([cx - 4, top + 10, cx + 3, top + 13], muz)
        d.rectangle([cx - 2, top - 2, cx + 1, top + 3], dark)
        d.point([(cx - 2, top + 12), (cx + 1, top + 12)], dark)
        ex, ey, my = 4, top + 4, top + 13
    for sx in (-ex, ex - 1):
        x = cx + sx
        if eye == "open":
            d.rectangle([x, ey, x + 1, ey + 1], INK)
            d.point([(x, ey)], WHITE)
        elif eye == "wide":
            d.rectangle([x - 1, ey - 1, x + 2, ey + 2], WHITE)
            d.rectangle([x, ey, x + 1, ey + 1], INK)
        else:
            d.line([(x - 1, ey + 1), (x + 2, ey + 1)], INK)
    if mouth:
        d.rectangle([cx - 1, my - 1, cx, my], (160, 40, 40))


@lru_cache(maxsize=1024)
def spr_front(sp, color, pose, f):
    """正脸半身像（坐着，下半身被桌子挡住）。44x30 逻辑像素，放大 2 倍。"""
    P = palette(color)
    im = Image.new("RGBA", (44, 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx = 22
    bob = (0, 2, 0, 1)[f % 4] if pose == "type" else (0, 3, 1, 3)[f % 4] if pose == "panic" else 0
    top = {"sleep": 11, "jump": 1}.get(pose, 6 + bob)
    if pose != "sleep":
        d.rectangle([cx - 8, top + 13, cx + 7, 29], P["dark"])          # 身体
        d.rectangle([cx - 2, top + 13, cx + 1, top + 15], P["light"])   # 领口
    else:
        d.rectangle([cx - 9, top + 9, cx + 8, 29], P["dark"])
    if pose in ("type", "panic"):                                       # 两只手交替砸键盘
        a = f % 2
        for s_, up in ((-1, a), (1, 1 - a)):
            hy = top + 22 - up * (4 if pose == "type" else 7)
            d.line([(cx + s_ * 8, top + 15), (cx + s_ * 6, hy)], P["dark"], width=2)
            d.rectangle([cx + s_ * 6 - 1, hy - 1, cx + s_ * 6 + 1, hy + 1], P["fur"])
    if pose == "wait":
        d.line([(cx + 7, top + 15), (cx + 11, top + 6), (cx + 11, top)], P["dark"], width=2)
        d.rectangle([cx + 10, top - 3, cx + 12, top - 1], P["fur"])
    if pose == "jump":
        for s in (-1, 1):
            d.line([(cx + s * 8, top + 15), (cx + s * 12, top + 6)], P["dark"], width=2)
            d.rectangle([cx + s * 12 - 1, top + 3, cx + s * 12 + 1, top + 5], P["fur"])
    eye = {"sleep": "closed", "jump": "wide", "panic": "wide", "blink": "closed"}.get(pose, "open")
    face(d, cx, top, sp, P, eye=eye, mouth=pose in ("jump", "talk"))
    return im.resize((88, 60), Image.NEAREST)


@lru_cache(maxsize=512)
def spr_walker(sp, color, f, carrying, flip=False):
    """正脸全身站立小人。26x34 逻辑像素，放大 2 倍。"""
    P = palette(color)
    im = Image.new("RGBA", (26, 34), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, top = 13, 4 + f % 2
    d.rectangle([cx - 6, top + 13, cx + 5, top + 22], P["dark"])
    a, b = (3, 2) if f % 2 == 0 else (2, 3)
    d.rectangle([cx - 5, top + 23, cx - 2, top + 23 + a], P["deep"])
    d.rectangle([cx + 1, top + 23, cx + 4, top + 23 + b], P["deep"])
    face(d, cx, top, sp, P)
    if carrying:
        d.rectangle([cx - 4, top + 15, cx + 3, top + 22], (250, 250, 240))
        for yy in (17, 19, 21):
            d.line([(cx - 3, top + yy), (cx + 2, top + yy)], (150, 150, 160))
        d.rectangle([cx - 6, top + 17, cx - 5, top + 18], P["fur"])
        d.rectangle([cx + 4, top + 17, cx + 5, top + 18], P["fur"])
    im = im.resize((52, 68), Image.NEAREST)
    return im.transpose(Image.FLIP_LEFT_RIGHT) if flip else im


@lru_cache(maxsize=8)
def spr_boss(f=0):
    """我：戴墨镜的监工。"""
    im = Image.new("RGBA", (22, 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, top = 11, 3
    d.rectangle([cx - 7, top + 10, cx + 6, 29], (36, 38, 52))
    d.polygon([(cx - 2, top + 10), (cx + 1, top + 10), (cx, top + 16)], WHITE)
    d.rectangle([cx - 1, top + 11, cx, top + 15], RED)
    d.rectangle([cx - 4, top + 1, cx + 3, top + 9], (240, 200, 160))
    d.rectangle([cx - 5, top - 1, cx + 4, top + 2], (50, 36, 30))
    d.rectangle([cx - 5, top + 2, cx - 5, top + 4], (50, 36, 30))
    d.rectangle([cx - 4, top + 4, cx + 3, top + 5], INK)
    d.point([(cx - 1, top + 5), (cx, top + 5)], (240, 200, 160))
    d.line([(cx - 2, top + 8), (cx + 1, top + 8)], (150, 80, 70))
    return im.resize((44, 60), Image.NEAREST)


# ------------------------------------------------------------------ 主题基类
class Theme:
    key = "base"
    title = ""
    GAUGE_ON_BOARD = True              # 有专门放额度的位置的主题（像素 / 牧场 / 太空）改成 False

    def __init__(self, font_path=None):
        p = find_font(font_path)
        self.font = ImageFont.truetype(p, 12)
        self.big = ImageFont.truetype(p, 26)
        self.huge = ImageFont.truetype(p, 44)

    # ---- 点屏幕选人 ----
    def hit_boxes(self, office):
        """每个 bot 在画面上占的矩形 [(x0, y0, x1, y1)]，按 bot 下标排。各主题自己实现。"""
        return []

    def boss_box(self, office):
        """"我"在画面上占的矩形；按住它 = 往群里发语音。各主题自己实现。"""
        return None

    def bot_at(self, office, x, y):
        """屏幕坐标 (x, y) 点中的 bot 下标；没点中返回 -1。"""
        for i, (x0, y0, x1, y1) in enumerate(self.hit_boxes(office)[:len(office.bots)]):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        return -1

    def me_at(self, office, x, y):
        b = self.boss_box(office)
        return bool(b and b[0] <= x <= b[2] and b[1] <= y <= b[3])

    # ---- 文字 ----
    def fit(self, text, width):
        if self.font.getlength(text) <= width:
            return text
        while text and self.font.getlength(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def wrap(self, text, width, first_indent=0, max_lines=99):
        lines, cur, cw = [], "", first_indent
        for ch in text:
            w = self.font.getlength(ch)
            if cw + w > width and cur:
                lines.append(cur)
                cur, cw = "", 0
                if len(lines) == max_lines:
                    lines[-1] = self.fit(lines[-1] + "…", width - (first_indent if max_lines == 1 else 0))
                    return lines
            cur += ch
            cw += w
        if cur:
            lines.append(cur)
        return lines or [""]

    def status_text(self, office, now):
        sil = office.silence(now)
        if not office.bridge_online(now):
            return "桥接离线", YELLOW
        if office.stalled(now):
            return "卡住 " + fmt_dur(sil), RED
        if office.anyone_typing(now):
            return "干活中", GREEN
        out = office.outsourced_since(now)
        if out is not None:
            return "外包中 " + fmt_dur(out), (110, 180, 255)
        return "静默 " + fmt_dur(sil), (170, 176, 190)

    # ---- 黑板 ----
    def board(self, d, office, now, box, chalk=CHALK, name_light=True, title=None):
        """把群聊记录用粉笔写在 box=(x0,y0,x1,y1) 里，最新的在最下面，最新一条逐字写出。"""
        if not office.show_chat:
            return self.dashboard(d, office, now, box, chalk, title)
        x0, y0, x1, y1 = box
        width = x1 - x0
        y_top = y0
        if title:
            d.text((x0, y0), self.fit(title, width), font=self.font, fill=chalk)
            d.line([(x0, y0 + 14), (x1, y0 + 14)], tuple(int(c * 0.55) for c in chalk))
            y_top = y0 + 17
        max_lines = max(1, (y1 - y_top + 2) // 14)
        msgs = [m for m in office.messages if not m.private]
        rows = []
        for k, m in enumerate(reversed(msgs)):
            idx = office.index(m.sender)
            col = (bot_pal(office.bots[idx])["light" if name_light else "fur"] if idx >= 0 else (255, 236, 150))
            head = self.fit(m.sender, 62) + ":"
            hw = self.font.getlength(head) + 3
            text = m.text
            if k == 0:                                   # 最新一条：粉笔正在写
                text = text[:max(1, int((now - m.ts) * 28))]
            wrapped = self.wrap(text, width, first_indent=hw)
            block = [(head if j == 0 else None, col, ln) for j, ln in enumerate(wrapped)]
            if not rows and len(block) > max_lines:
                block = block[:max_lines]
            if len(rows) + len(block) > max_lines:
                break
            rows = block + rows
        y = y_top
        if not rows:
            d.text((x0, y), "（还没有人说话）", font=self.font, fill=tuple(int(c * 0.6) for c in chalk))
        for head, col, ln in rows:
            x = x0
            if head:
                d.text((x, y), head, font=self.font, fill=col)
                x += self.font.getlength(head) + 3
            d.text((x, y), ln, font=self.font, fill=chalk)
            y += 14

    def dashboard(self, d, office, now, box, chalk=CHALK, title=None):
        """不展示聊天内容时，黑板上写：几个人在干活、谁刚说过话、每个人的状态点。"""
        x0, y0, x1, y1 = box
        width = x1 - x0
        dim = tuple(int(c * 0.6) for c in chalk)
        y = y0
        if title:
            d.text((x0, y0), self.fit(title, width), font=self.font, fill=chalk)
            d.line([(x0, y0 + 14), (x1, y0 + 14)], tuple(int(c * 0.55) for c in chalk))
            y = y0 + 17
        poses = [pose_of(office, i, now) for i in range(len(office.bots))]
        work = sum(p in ("type", "panic", "jump") for p in poses)
        wait = sum(p == "wait" for p in poses)
        nap = sum(p == "sleep" for p in poses)
        x = x0
        for label, num, col in (("干活", work, GREEN), ("待批", wait, YELLOW), ("睡觉", nap, (150, 160, 190))):
            seg = f"{label} {num}"
            d.text((x, y), seg, font=self.font, fill=col if num else dim)
            x += self.font.getlength(seg) + 12
            if x > x1 - 30 and label != "睡觉":
                x, y = x0, y + 14
        y += 16
        talk = [m for m in office.messages if not m.private]
        if talk and y + 12 <= y1:
            m = talk[-1]
            ago = max(0, int(now - m.ts))
            d.text((x0, y), self.fit(f"刚说话: {m.sender} · {fmt_dur(ago)}前", width), font=self.font, fill=chalk)
            y += 16
        x = x0                                        # 每个 bot 一个状态点 + 名字，放不下就换行
        for i, bot in enumerate(office.bots):
            if y + 12 > y1 + 2:
                break
            nm = self.fit(bot.name, 52)
            w = 12 + self.font.getlength(nm) + 8
            if x + w > x1 and x > x0:
                x, y = x0, y + 14
                if y + 12 > y1 + 2:
                    break
            col = {"type": GREEN, "panic": GREEN, "jump": GREEN, "wait": YELLOW}.get(poses[i], (120, 128, 150))
            r = 3 + (1 if poses[i] in ("type", "panic") and int(now * 6) % 2 else 0)
            d.ellipse([x + 4 - r, y + 7 - r, x + 4 + r, y + 7 + r], col)
            d.text((x + 11, y), nm, font=self.font, fill=chalk)
            x += w
        if self.GAUGE_ON_BOARD and office.usage:
            narrow = x1 - x0 < 140
            need = 3 * 14 if narrow else 16
            if y1 - need > y + 10:
                self.gauge_strip(d, office, now, (x0, y + 8, x1, y1 + 2), chalk)

    # ---- 气泡 ----
    def bubble(self, d, anchor, text, private=False, side=1, max_w=112, max_lines=2,
               bg=WHITE, fg=INK, edge=INK, bounds=(2, 2, W - 2, H - 2)):
        """anchor 是说话者嘴边的点。群聊是实线气泡，悄悄话是虚线气泡加"嘘"。"""
        if private:
            text = "嘘…" + text
        lines = self.wrap(text, max_w - 10, max_lines=max_lines)
        bw = int(max(self.font.getlength(ln) for ln in lines)) + 10
        bh = len(lines) * 14 + 5
        ax, ay = anchor
        x0 = ax + 8 if side > 0 else ax - 8 - bw
        y0 = ay - bh - 4
        x0 = max(bounds[0], min(bounds[2] - bw, x0))
        y0 = max(bounds[1], min(bounds[3] - bh, y0))
        x1, y1 = x0 + bw, y0 + bh
        tail = [(ax, ay), (min(max(ax + side * 6, x0 + 4), x1 - 10), y1 - 1),
                (min(max(ax + side * 14, x0 + 10), x1 - 4), y1 - 1)]
        if private:
            pbg = tuple(int(c * 0.86) for c in bg)
            d.rounded_rectangle([x0, y0, x1, y1], 5, fill=pbg)
            d.polygon(tail, fill=pbg)
            self._dashed_box(d, x0, y0, x1, y1, edge)
            fg = tuple(int(c + (140 - c) * 0.35) for c in fg)
        else:
            d.polygon(tail, fill=bg, outline=edge)
            d.rounded_rectangle([x0, y0, x1, y1], 5, fill=bg, outline=edge)
            d.line([tail[1], tail[2]], bg)
        for k, ln in enumerate(lines):
            d.text((x0 + 5, y0 + 2 + k * 14), ln, font=self.font, fill=fg)

    @staticmethod
    def _dashed_box(d, x0, y0, x1, y1, col, dash=3):
        for x in range(int(x0) + 3, int(x1) - 2, dash * 2):
            d.line([(x, y0), (min(x + dash - 1, x1 - 3), y0)], col)
            d.line([(x, y1), (min(x + dash - 1, x1 - 3), y1)], col)
        for y in range(int(y0) + 3, int(y1) - 2, dash * 2):
            d.line([(x0, y), (x0, min(y + dash - 1, y1 - 3))], col)
            d.line([(x1, y), (x1, min(y + dash - 1, y1 - 3))], col)

    def mark(self, d, x, y, text, bg, fg=INK):
        d.ellipse([x - 7, y - 7, x + 7, y + 7], bg, outline=INK)
        d.text((x, y), text, font=self.font, fill=fg, anchor="mm")

    def zzz(self, d, x, y, now, i, col):
        k = (now * 1.1 + i * 0.37) % 1.0
        for ox in (0, 1):
            d.text((x + k * 9 + ox, y - k * 12), "Z" if k > 0.5 else "z", font=self.font, fill=col)

    # ---- 夸张的打字 / 惊慌 / 跳起 ----
    def type_fx(self, d, x, y, now, i, col, spread=22, rise=24):
        """敲键盘：键帽声一个个往上蹦，旁边溅火花。"""
        for k in range(3):
            ph = (now * 2.6 + k / 3 + i * 0.17) % 1.0
            tx = x + (k - 1) * spread * 0.6 + math.sin((ph + k) * 7) * 3
            d.text((tx, y - ph * rise), ("咔", "哒", "噼", "啪")[(k + int(now * 3) + i) % 4], font=self.font,
                   fill=col, anchor="mm")
        for k in range(3):
            a = (now * 17 + k * 2.1 + i) % 6.283
            r0 = 5 + (int(now * 20) + k) % 3 * 2
            d.line([(x + math.cos(a) * r0 * 3, y + 10 + math.sin(a) * r0),
                    (x + math.cos(a) * (r0 * 3 + 5), y + 10 + math.sin(a) * (r0 + 3))], YELLOW)

    def panic_fx(self, d, x, y, now, i, w=22):
        """惊慌：汗珠甩出去，头顶一串抖动的感叹号，两侧是速度线。"""
        t = now * 9 + i
        for s_ in (-1, 1):
            ph = (t * 0.35 + (0 if s_ < 0 else 0.5)) % 1.0
            sx, sy = x + s_ * (w - 4 + ph * 14), y + 6 + ph * 14
            d.ellipse([sx - 1, sy - 2, sx + 1, sy + 2], (140, 210, 255))
            d.line([(sx, sy - 2), (sx, sy - 5)], (140, 210, 255))
        jx = int(math.sin(now * 40 + i) * 2)
        d.text((x + jx, y - 12), "!!", font=self.font, fill=RED, anchor="mm")
        for k in range(3):
            yy = y + k * 5 - 2
            off = (int(now * 24) + k) % 3 * 3
            d.line([(x - w - 6 - off, yy), (x - w - 14 - off, yy)], WHITE)
            d.line([(x + w + 6 + off, yy), (x + w + 14 + off, yy)], WHITE)

    @staticmethod
    def jump_dy(office, now, height=18, i=None):
        """被抽那一下跳多高（负数 = 往上）；收到大饼时原地蹦。"""
        if i is not None and office.cheering(i, now):
            return -int(height * 0.7 * abs(math.sin(now * 9)))
        tw = now - office.whip_t0
        if T_WHIP_HIT <= tw < T_WHIP_JUMP:
            return -int(height * math.sin((tw - T_WHIP_HIT) / (T_WHIP_JUMP - T_WHIP_HIT) * math.pi))
        return 0

    @staticmethod
    def jitter(now, i, amp=2):
        """惊慌打字时整个人哆嗦。"""
        k = int(now * 30) + i * 5
        return (k * 7 % (2 * amp + 1)) - amp, (k * 11 % (2 * amp + 1)) - amp

    # ---- 额度 ----
    @staticmethod
    def usage_color(pct):
        return GREEN if pct < 70 else YELLOW if pct < 90 else RED

    def gauge_column(self, d, office, now, box, label_col=WHITE):
        """三根竖条：G=Grok Bot 每周、C=Cursor 模型、O=其他模型。box = (x0, y0, x1, y1)。"""
        x0, y0, x1, y1 = box
        cw = (x1 - x0) // 3
        for k, (key, tag) in enumerate((("grok", "G"), ("cursor", "C"), ("other", "O"))):
            cx = x0 + k * cw + cw // 2
            pct = office.usage_pct(key)
            top, bot = y0 + 13, y1 - 14
            d.rectangle([cx - 6, top, cx + 6, bot], (36, 38, 50), outline=(90, 94, 110))
            if pct is None:
                d.text((cx, (top + bot) / 2), "?", font=self.font, fill=(130, 134, 150), anchor="mm")
                d.text((cx, y0 + 5), "--", font=self.font, fill=(130, 134, 150), anchor="mm")
            else:
                col = self.usage_color(pct)
                if pct >= 95 and int(now * 3) % 2:
                    col = WHITE
                h = max(2, int((bot - top - 2) * min(pct, 100) / 100))
                d.rectangle([cx - 5, bot - h, cx + 5, bot - 1], col)
                d.text((cx, y0 + 5), f"{pct:.0f}", font=self.font, fill=col, anchor="mm")
            d.text((cx, y1 - 5), tag, font=self.font, fill=label_col, anchor="mm")

    def gauge_strip(self, d, office, now, box, chalk=CHALK):
        """横向小条，放在黑板底部。窄板（宽度 < 140）自动改成竖着排三行。"""
        x0, y0, x1, y1 = box
        narrow = x1 - x0 < 140
        n = 3
        for k, (key, tag) in enumerate((("grok", "Grok"), ("cursor", "Cur"), ("other", "其他"))):
            pct = office.usage_pct(key)
            if narrow:
                yy = y1 - (n - k) * 14
                xa, xb = x0, x1
            else:
                seg = (x1 - x0) / n
                xa, xb, yy = x0 + k * seg, x0 + (k + 1) * seg - 6, y1 - 13
            d.text((xa, yy), tag, font=self.font, fill=chalk)
            bx0 = xa + self.font.getlength(tag) + 3
            txt = "--" if pct is None else f"{pct:.0f}%"
            tw = self.font.getlength(txt)
            bx1 = xb - tw - 3
            d.rectangle([bx0, yy + 4, bx1, yy + 9], (40, 44, 52))
            col = (130, 134, 150) if pct is None else self.usage_color(pct)
            if pct is not None:
                if pct >= 95 and int(now * 3) % 2:
                    col = WHITE
                d.rectangle([bx0, yy + 4, bx0 + max(1, int((bx1 - bx0) * min(pct, 100) / 100)), yy + 9], col)
            d.text((xb, yy), txt, font=self.font, fill=col, anchor="ra")

    def quota_overlay(self, d, office, now):
        """额度提示：越过 80%/95% 时边框闪一下；Grok Bot 额度用完时底部一条红条。"""
        if now < office.usage_alert_until and int(now * 4) % 2 == 0:
            for k in range(2):
                d.rectangle([k, k, W - 1 - k, H - 1 - k], outline=YELLOW)
        if office.quota_blocked():
            reset = office.usage.get("grok", {}).get("reset")
            when = ""
            if reset:
                t = datetime.fromtimestamp(reset)
                when = f" · {t.month}月{t.day}日恢复"
            label = "Grok Bot 额度已用完" + when
            tw = self.font.getlength(label)
            d.rectangle([0, H - 17, W, H], (60, 12, 12))
            d.text((W / 2, H - 9), label, font=self.font, fill=RED if int(now * 2) % 2 else WHITE, anchor="mm")

    # ---- 云端 bot：活在云上的 Cursor 里干 ----
    def cloud(self, d, cx, cy, now, i, col=(236, 240, 250), edge=(120, 130, 160)):
        """头顶一朵云，云下面三个点轮流亮，表示「在云上干活」。"""
        wob = int(math.sin(now * 3 + i) * 2)
        for dx, dy, r in ((-9, 1, 6), (0, -3, 8), (9, 1, 6), (0, 3, 7)):
            d.ellipse([cx + dx - r, cy + dy + wob - r, cx + dx + r, cy + dy + wob + r], col, outline=edge)
        for dx, dy, r in ((-9, 1, 6), (0, -3, 8), (9, 1, 6), (0, 3, 7)):
            d.ellipse([cx + dx - r + 1, cy + dy + wob - r + 1, cx + dx + r - 1, cy + dy + wob + r - 1], col)
        for k in range(3):
            on = int(now * 3) % 3 == k
            d.ellipse([cx - 7 + k * 7 - 1, cy + 13 + wob - 1, cx - 7 + k * 7 + 1, cy + 13 + wob + 1], (70, 150, 250) if on else (150, 160, 190))

    def remote_bar(self, d, office, bot, x, y, w):
        """云端 bot 名牌下的额度条（用 Cursor 模型额度）。"""
        if not office.remote_label(bot.name):
            return
        pct = office.usage_pct("cursor")
        d.rectangle([x, y, x + w, y + 3], (30, 32, 42))
        if pct is not None:
            d.rectangle([x, y, x + max(1, int(w * min(pct, 100) / 100)), y + 3], self.usage_color(pct))

    def package(self, d, x, y, now):
        """云包裹：白盒子 + 彩带，飞行中一闪一闪。"""
        d.rectangle([x - 5, y - 4, x + 5, y + 4], (250, 250, 240), outline=(120, 120, 140))
        d.line([(x - 5, y), (x + 5, y)], (230, 90, 90))
        d.line([(x, y - 4), (x, y + 4)], (230, 90, 90))
        if int(now * 12) % 2:
            d.point([(x - 8, y - 6), (x + 8, y - 5), (x + 7, y + 7)], YELLOW)

    # ---- 老板发大饼 ----
    def pie(self, d, x, y, r):
        d.ellipse([x - r, y - r, x + r, y + r], (150, 96, 40))
        d.ellipse([x - r + 1, y - r + 1, x + r - 1, y + r - 1], (236, 192, 108))
        d.ellipse([x - r + 3, y - r + 3, x + r - 3, y + r - 3], (246, 214, 138))
        d.arc([x - r + 3, y - r + 3, x + r - 3, y + r - 3], 200, 290, (255, 240, 190))
        if r >= 9:
            d.text((x, y), "饼", font=self.font, fill=(138, 84, 30), anchor="mm")

    def boss_walk(self, office, now, home, stops):
        """老板巡视时的位置。返回 (tr, (x, y), 在不在走路) 或 None。stops[i] 是站在第 i 个 bot 面前的点。"""
        tr = office.tour_at(now)
        if not tr:
            return None
        k, bot, p = tr["k"], tr["bot"], tr["p"]
        if tr["stage"] == "walk":
            a = home if k == 0 else stops[office.enc_order[k - 1]]
            b, moving = stops[bot], True
        elif tr["stage"] == "give":
            a = b = stops[bot]
            moving, p = False, 0
        else:
            a, b, moving = stops[bot], home, True
        pos = (a[0] + (b[0] - a[0]) * p, a[1] + (b[1] - a[1]) * p)
        if moving:
            pos = (pos[0], pos[1] - abs(math.sin(now * 14)) * 2)          # 走路一颠一颠
        return tr, pos, moving

    def tour_overlay(self, d, now, tr, pos, head, say="大家辛苦了", bounds=(2, 2, W - 2, H - 2)):
        """发饼阶段：饼从老板手里飞到 bot 头上并鼓起来，bot 头顶冒星星，老板头上一个气泡。
        点名阶段：老板盯着它（一道红色视线 + 头顶跳动的"盯"），气泡里是点名的话。"""
        if tr["stage"] != "give":
            return
        if tr.get("kind") == "poke":
            self.stare(d, now, (pos[0] + 6, pos[1] - 28), head)
            self.bubble(d, (pos[0] + 6, pos[1] - 36), tr.get("say") or say, bounds=bounds, max_w=118)
            return
        p = tr["p"]
        q = min(1.0, p / 0.4)
        hx, hy = pos[0] + 16, pos[1] - 20
        e = q * q * (3 - 2 * q)
        px = hx + (head[0] - hx) * e
        py = hy + (head[1] - hy) * e - math.sin(q * math.pi) * 16
        r = 6 + int(e * 5) + (int(abs(math.sin(now * 10)) * 2) if q >= 1 else 0)
        self.pie(d, px, py, r)
        if q >= 1:
            for k in range(5):
                a = now * 6 + k * 1.256
                sx, sy = head[0] + math.cos(a) * 22, head[1] - 6 + math.sin(a) * 10
                d.text((sx, sy), "★" if k % 2 else "♥", font=self.font, fill=YELLOW if k % 2 else (255, 110, 150), anchor="mm")
        self.bubble(d, (pos[0] + 6, pos[1] - 36), say, bounds=bounds, max_w=80)

    def stare(self, d, now, eye, head):
        """老板盯人：视线是一串往目标流动的红点，目标头顶一个一跳一跳的"盯"。"""
        ex, ey = eye
        hx, hy = head
        dist = max(1.0, math.hypot(hx - ex, hy - ey))
        for k in range(int(dist // 7)):
            t = (k * 7 / dist + now * 0.8) % 1.0
            d.ellipse([ex + (hx - ex) * t - 1, ey + (hy - ey) * t - 1, ex + (hx - ex) * t + 1, ey + (hy - ey) * t + 1], RED)
        jy = -abs(math.sin(now * 8)) * 4
        for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            d.text((hx + 16 + ox, hy - 16 + jy + oy), "盯", font=self.font, fill=INK, anchor="mm")
        d.text((hx + 16, hy - 16 + jy), "盯", font=self.font, fill=RED, anchor="mm")

    # ---- 抽鞭子、卡住、提示 ----
    def whip(self, d, office, now, rope=(150, 84, 40), edge=(40, 24, 12), hit=(236, 96)):
        tw = now - office.whip_t0
        if not 0 <= tw < 1.4:
            return
        if tw < 1.1:
            cx, cy, R = 160, -6, 230
            if tw < 0.25:
                ang, lag = math.radians(200 - 30 * (tw / 0.25)), math.radians(-45)
            else:
                p = ease((tw - 0.25) / 0.3)
                ang, lag = math.radians(170 - 155 * p), math.radians(60 * (1 - p * 0.6))
            tip = (cx + R * math.cos(ang), cy + R * math.sin(ang))
            ctl = (cx + R * 0.55 * math.cos(ang + lag), cy + R * 0.55 * math.sin(ang + lag))
            pts = []
            for i in range(17):
                t = i / 16
                pts.append(((1 - t) ** 2 * cx + 2 * (1 - t) * t * ctl[0] + t * t * tip[0],
                            (1 - t) ** 2 * (cy + 8) + 2 * (1 - t) * t * ctl[1] + t * t * tip[1]))
            for col, extra in ((edge, 2), (rope, 0)):
                for i in range(16):
                    d.line([pts[i], pts[i + 1]], col, width=(7 if i < 4 else 5 if i < 11 else 3) + extra)
        if T_WHIP_HIT <= tw < T_WHIP_HIT + 0.9:
            bx, by = hit
            q = (tw - T_WHIP_HIT) / 0.9
            for k in range(14):                                   # 放射线
                a = k * math.pi / 7 + 0.3
                r0, r1 = 22 + q * 40, 52 + q * 90
                d.line([(bx + r0 * math.cos(a), by + r0 * 0.75 * math.sin(a)),
                        (bx + r1 * math.cos(a), by + r1 * 0.75 * math.sin(a))], YELLOW if k % 2 else WHITE,
                       width=4 if q < 0.4 else 2)
            for ring in (0, 1):                                   # 冲击波
                rr = 14 + q * (110 + ring * 60) - ring * 10
                if rr > 0:
                    d.ellipse([bx - rr, by - rr * 0.75, bx + rr, by + rr * 0.75], outline=WHITE if ring == 0 else YELLOW,
                              width=3 if q < 0.5 else 1)
            if q < 0.7:
                for ox, oy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-3, 3), (3, -3)):
                    d.text((bx + ox, by + oy), "啪!!", font=self.huge, fill=INK, anchor="mm")
                d.text((bx, by), "啪!!", font=self.huge, fill=WHITE if int(now * 20) % 2 else YELLOW, anchor="mm")

    def shake(self, office, now):
        """挨抽那一下整屏抖几抖。整屏一挪每个小块都变了，Core2 那边每帧都得整屏重传，所以只抖 0.3 秒。"""
        tw = now - office.whip_t0
        k = int(now * 20)
        if T_WHIP_HIT <= tw < T_WHIP_HIT + 0.3:
            return (k * 7 % 11) - 5, (k * 11 % 11) - 5
        return 0, 0

    def rush_banner(self, d, office, now):
        """惊慌阶段：顶上一条红黄警示带，写"全员冲刺"，屏幕边缘一圈红框。
        红框不闪：一闪整圈边上的小块都跟着变，Core2 每帧要重传大半个屏幕，动画就卡了。"""
        tw = now - office.whip_t0
        if not T_WHIP_HIT + 0.25 <= tw < T_WHIP_END - 0.6:
            return
        for k in range(3):
            d.rectangle([k, k, W - 1 - k, H - 1 - k], outline=RED)
        d.rectangle([0, 0, W, 15], (30, 20, 20))
        for x in range(-16 + int(now * 40) % 16, W, 16):
            d.polygon([(x, 15), (x + 8, 15), (x + 16, 0), (x + 8, 0)], YELLOW)
        label = "加急！全员冲刺！！"
        tw_ = self.font.getlength(label)
        d.rectangle([W / 2 - tw_ / 2 - 6, 1, W / 2 + tw_ / 2 + 6, 14], (30, 20, 20))
        d.text((W / 2, 8), label, font=self.font, fill=RED if int(now * 8) % 2 else WHITE, anchor="mm")

    def stall_frame(self, d, office, now):
        if office.stalled(now):
            a = int(now * 1.5) % 2                                    # 亮暗两档慢慢闪：每帧都变的话 Core2 每帧都要重传四条边
            col = tuple(int(RED[j] * (0.45 + 0.55 * a)) for j in range(3))
            for k in range(3):
                d.rectangle([k, k, W - 1 - k, H - 1 - k], outline=col)

    def toast(self, d, office, now):
        text, t0, kind = office.toast
        if not text or now - t0 > 3.0:
            return
        col = {"ok": GREEN, "warn": YELLOW, "err": RED}.get(kind, GREEN)
        tw = self.font.getlength(text)
        d.rounded_rectangle([(W - tw) / 2 - 10, H - 30, (W + tw) / 2 + 10, H - 8], 6, fill=(20, 22, 30), outline=col)
        d.text((W / 2, H - 19), text, font=self.font, fill=col, anchor="mm")

    # ---- 公告栏：等你批准的事挂在屏幕顶上 ----
    def notice_board(self, img, d, office, now):
        items = office.notices(now)
        if not items:
            return 0
        rows = items[:2]
        h = 15 * len(rows) + 2
        d.rectangle([0, 0, W, h], (255, 226, 110))
        d.line([(0, h), (W, h)], (150, 110, 20))
        for k, (i, text) in enumerate(rows):
            y = 1 + k * 15
            bot = office.bots[i]
            d.ellipse([4, y + 3, 11, y + 10], RED, outline=INK)                    # 图钉
            d.line([(8, y + 10), (10, y + 13)], INK)
            head = self.fit(bot.name, 54) + "："
            hx = 15
            d.rectangle([hx, y + 2, hx + 3, y + 11], bot_pal(bot)["fur"])
            d.text((hx + 6, y), head, font=self.font, fill=INK)
            x0 = hx + 6 + self.font.getlength(head)
            more = f"+{len(items) - 2}" if k == len(rows) - 1 and len(items) > 2 else ""
            x1 = W - 4 - (self.font.getlength(more) + 4 if more else 0)
            tw = self.font.getlength(text)
            if tw <= x1 - x0:
                d.text((x0, y), text, font=self.font, fill=INK)
            else:                                                                   # 太长：跑马灯
                lane = Image.new("RGB", (int(x1 - x0), 15), (255, 226, 110))
                ld = ImageDraw.Draw(lane)
                period = tw + 40
                off = (now * 30 + i * 37) % period
                for rep_x in (-off, period - off):
                    ld.text((rep_x, 0), text, font=self.font, fill=INK)
                img.paste(lane, (int(x0), y))
            if more:
                d.text((W - 4, y), more, font=self.font, fill=(120, 60, 10), anchor="ra")
        return h + 1

    # ---- 录音状态条：按住牛马 / 我 发语音时 ----
    def voice_badge(self, d, office, now, top):
        v = office.voice
        if not v:
            return
        who = "群聊" if not v.get("to") else v["to"] + "（私聊）"
        st = v.get("state")
        if st == "recording":
            sec = int(now - v.get("rec_t0", now))
            text, col = f"录音中 {sec // 60:02d}:{sec % 60:02d} → {who}  松开发送", RED
        else:
            text, col = f"Grok 听写中 → {who}", YELLOW
        tw = self.font.getlength(text)
        x0, y0 = (W - tw) / 2 - 18, top + 4
        d.rounded_rectangle([x0, y0, x0 + tw + 26, y0 + 18], 9, fill=(20, 20, 26), outline=col, width=2)
        on = st != "recording" or int(now * 3) % 2 == 0
        d.ellipse([x0 + 7, y0 + 5, x0 + 15, y0 + 13], col if on else (60, 30, 30))
        d.text((x0 + 20, y0 + 9), text, font=self.font, fill=WHITE, anchor="lm")
        if st == "recording":                                                       # 声波
            for k in range(5):
                hh = 2 + int(abs(math.sin(now * 9 + k * 1.1)) * 5)
                d.rectangle([x0 + tw + 30 + k * 4, y0 + 9 - hh, x0 + tw + 32 + k * 4, y0 + 9 + hh], RED)

    def finish(self, img, office, now, **whip_kw):
        d = ImageDraw.Draw(img)
        self.stall_frame(d, office, now)
        self.whip(d, office, now, **whip_kw)
        top = self.notice_board(img, d, office, now)
        self.rush_banner(d, office, now)
        self.voice_badge(d, office, now, top)
        self.quota_overlay(d, office, now)
        self.toast(d, office, now)
        tw = now - office.whip_t0
        if T_WHIP_HIT <= tw < T_WHIP_HIT + 0.16:                          # 挨抽瞬间整屏一闪
            img = Image.blend(img, Image.new("RGB", (W, H), WHITE), 0.8 * (1 - (tw - T_WHIP_HIT) / 0.16))
        sx, sy = self.shake(office, now)
        if sx or sy:
            out = Image.new("RGB", (W, H), INK)
            out.paste(img, (sx, sy))
            return out
        return img


# ------------------------------------------------------------------ 正脸工位（牧场 / 太空共用的基类）
class PixelOffice(Theme):
    key, title = "desk", "正脸工位"
    CELL_W, CELL_H = 78, 78
    BOARD = (10, 8, 230, 68)
    BOSS = (282, 168)

    def cell(self, i, n):
        cols = 3 if n > 4 else 2 if n > 1 else 1
        r, c = divmod(i, cols)
        in_row = min(cols, n - r * cols)
        off = (238 - in_row * self.CELL_W) // 2
        return off + c * self.CELL_W, 84 + r * self.CELL_H

    def background(self, d, office, now):
        d.rectangle([0, 0, W, 78], (226, 214, 190))                     # 墙
        d.rectangle([0, 74, W, 78], (150, 118, 84))                     # 踢脚线
        for y in range(79, H, 12):                                      # 木地板
            d.rectangle([0, y, 238, y + 11], (196, 160, 112) if (y // 12) % 2 else (188, 152, 106))
            for x in range((y * 7) % 46, 238, 46):
                d.line([(x, y), (x, y + 11)], (170, 136, 94))
        d.rectangle([242, 79, W, H], (132, 62, 66))                     # 老板间地毯
        for y in range(84, H, 10):
            d.line([(246, y), (W - 4, y)], (146, 74, 76))
        d.rectangle([238, 79, 241, 150], (110, 88, 66))                 # 隔断，下面留门
        d.rectangle([238, 196, 241, H], (110, 88, 66))
        # 黑板
        x0, y0, x1, y1 = self.BOARD
        d.rectangle([x0 - 5, y0 - 4, x1 + 5, y1 + 4], (150, 108, 66), outline=(96, 66, 40))
        d.rectangle([x0 - 2, y0 - 1, x1 + 2, y1 + 1], (34, 58, 48))
        d.rectangle([x0 + 20, y1 + 5, x0 + 60, y1 + 6], (240, 240, 230))  # 粉笔
        d.rectangle([x1 - 40, y1 + 4, x1 - 22, y1 + 7], (90, 110, 150))   # 板擦
        # 状态牌
        text, col = self.status_text(office, now)
        d.rounded_rectangle([244, 8, 314, 44], 4, fill=(30, 32, 44), outline=(96, 66, 40))
        d.text((279, 18), self.fit(office.group, 64), font=self.font, fill=WHITE, anchor="mm")
        blink = col == RED and int(now * 2) % 2 == 0
        d.text((279, 34), text, font=self.font, fill=WHITE if blink else col, anchor="mm")
        d.text((279, 60), "老板间", font=self.font, fill=(120, 96, 70), anchor="mm")
        d.polygon([(273, 70), (285, 70), (279, 76)], (120, 96, 70))

    # 家具颜色，牧场 / 太空主题改这几个
    CHAIR, TOP, FRONT, FRONT_EDGE = (74, 78, 96), (214, 172, 120), (168, 122, 78), (140, 100, 62)
    LAPTOP, NAME_FG = (44, 48, 60), WHITE

    def chair(self, d, x, y):
        d.rounded_rectangle([x + 16, y + 10, x + 61, y + 52], 6, fill=self.CHAIR)

    def desk(self, img, d, x, y, bot, i, pose, now):
        office = self._office
        P = bot_pal(bot)
        f = int(now * (16 if pose == "panic" else 10))
        self.chair(d, x, y)
        jx = jy = 0
        if pose == "panic":
            jx, jy = self.jitter(now, i, 2)
        dy = self.jump_dy(office, now, 20, i) if pose == "jump" else 0
        remote = office.remote_label(bot.name)
        cloud_work = pose == "cloud" or (bool(remote) and pose in ("type", "panic"))
        failed = office.remote_state.get(bot.name, {}).get("state") == "failed"
        if pose not in ("away", "whisper"):
            if dy < -3:                                                                   # 跳起来，地上留个影子
                d.ellipse([x + 22, y + 56, x + 56, y + 62], (0, 0, 0))
            spose = "idle" if cloud_work else pose                                        # 云端 bot 不敲键盘，在等云上干完
            s = spr_front(species(i), P["base"], spose if spose != "idle" or (int(now * 10) + i * 13) % 37 else "blink",
                          f % 4 if spose in ("type", "panic") else 0)
            img.paste(s, (x - 5 + jx, y - 12 + dy + jy), s)
        ty = jy if pose in ("type", "panic") else 0
        d.rectangle([x + 3, y + 38, x + 74, y + 44], self.TOP)                           # 桌面
        d.rectangle([x + 5, y + 45, x + 72, y + 64], self.FRONT)                         # 桌子正面
        d.rectangle([x + 5, y + 45, x + 72, y + 46], self.FRONT_EDGE)
        lit = pose in ("type", "panic", "jump")
        flick = lit and f % 2 == 0
        d.rectangle([x + 27, y + 30 + ty, x + 50, y + 41], self.LAPTOP)                  # 笔记本背面
        d.rectangle([x + 37, y + 34 + ty, x + 40, y + 37 + ty],
                    (P["light"] if flick else P["glow"]) if lit else (90, 94, 110))
        if cloud_work:
            self.cloud(d, x + 38, y - 4, now, i)                                         # 头顶一朵云
            st = office.remote_state.get(bot.name)
            if st and st["state"] == "working":                                          # 云旁边挂外包计时
                d.text((x + 56, y - 14), fmt_dur(now - st["since"]), font=self.font, fill=(110, 180, 255))
            for k in range(3):                                                           # 云里往下掉小雨点（数据）
                ph = (now * 1.8 + k / 3 + i * 0.2) % 1.0
                d.line([(x + 28 + k * 10, y + 8 + ph * 22), (x + 28 + k * 10, y + 11 + ph * 22)], (110, 170, 250))
        elif pose in ("type", "panic"):
            a = f % 2
            amp = 3 if pose == "type" else 5
            d.rectangle([x + 18, y + 35 - a * amp, x + 24, y + 39 - a * amp], P["fur"])  # 两只手轮流砸
            d.rectangle([x + 53, y + 33 + a * amp - amp + 3, x + 59, y + 37 + a * amp - amp + 3], P["fur"])
            for k in range(8):                                                           # 两侧飘出来的代码
                ph = (now * 2.4 + k * 0.125 + i * 0.2) % 1.0
                cx = x + (4 if k % 2 == 0 else 62) + (k * 5 + i * 3) % 9
                d.rectangle([cx, y + 32 - ph * 30, cx + 3 + k % 4, y + 33 - ph * 30], CODE[(k + i) % 4])
            self.type_fx(d, x + 8, y + 34, now, i, P["light"], spread=12, rise=20)
            self.type_fx(d, x + 68, y + 34, now, i, P["light"], spread=12, rise=20)
            if pose == "panic":
                self.panic_fx(d, x + 38, y + 12, now, i, w=26)
        if self.PLANK:
            d.rectangle([x + 14, y + 49, x + 63, y + 61], self.PLANK, outline=self.FRONT_EDGE)
        d.text((x + 38, y + 55), self.fit(bot.name, 64), font=self.font, fill=self.NAME_FG, anchor="mm")
        d.rectangle([x + 8, y + 51, x + 11, y + 58], P["fur"])                           # 名牌色条
        if remote:                                                                       # 云端 bot：名牌右侧小云 + 额度条
            for ox, oy, rr in ((0, 1, 2), (3, -1, 3), (6, 1, 2)):
                d.ellipse([x + 60 + ox - rr, y + 55 + oy - rr, x + 60 + ox + rr, y + 55 + oy + rr], (236, 240, 250))
            self.remote_bar(d, office, bot, x + 14, y + 61, 48)

    PLANK = None
    GAUGE_ON_BOARD = False
    GAUGE_BOX = (246, 82, 314, 134)

    def hit_boxes(self, office):
        n = len(office.bots)
        return [(x, y - 12, x + self.CELL_W, y + 66) for x, y in (self.cell(i, n) for i in range(n))]

    def boss_box(self, office):
        bx, by = self.BOSS
        return (246, by - 40, W - 1, by + 56)

    def boss_sprite(self, img, d, bx, by):
        boss = spr_boss()
        img.paste(boss, (bx - 22, by - 34), boss)

    def boss(self, img, d, bx, by, away=False):
        d.rectangle([252, by + 22, 314, by + 30], (96, 66, 44))                          # 老板桌
        d.rectangle([254, by + 31, 312, by + 52], (70, 46, 30))
        boss = spr_boss()
        if not away:
            img.paste(boss, (bx - 22, by - 34), boss)
        d.rectangle([252, by + 22, 314, by + 30], (96, 66, 44))
        d.text((283, by + 42), "我", font=self.font, fill=(230, 200, 150), anchor="mm")

    def render(self, office, now):
        self._office = office
        img = Image.new("RGB", (W, H), INK)
        d = ImageDraw.Draw(img)
        self.background(d, office, now)
        self.board(d, office, now, (self.BOARD[0] + 3, self.BOARD[1] + 1, self.BOARD[2] - 3, self.BOARD[3]))
        n = len(office.bots)
        cells = [self.cell(i, n) for i in range(n)]
        self.gauge_column(d, office, now, self.GAUGE_BOX, label_col=self.NAME_FG)
        bx, by = self.BOSS
        stops = [(max(24, x - 4), y + 34) for x, y in cells]
        walk = self.boss_walk(office, now, (bx, by), stops)
        self.boss(img, d, bx, by, away=walk is not None)

        poses = [pose_of(office, i, now) for i in range(n)]
        for i, bot in enumerate(office.bots):
            self.desk(img, d, cells[i][0], cells[i][1], bot, i, poses[i], now)
        over = []
        for i, bot in enumerate(office.bots):
            x, y = cells[i]
            P = bot_pal(bot)
            if poses[i] == "sleep":
                self.zzz(d, x + 54, y + 12, now, i, P["deep"])
            elif poses[i] == "wait":
                self.mark(d, x + 62, y - 2, "!", YELLOW)
            text = talking(bot, now)
            if text and poses[i] not in ("away", "whisper"):
                over.append(((x + 46, y + 18), text, False, 1 if x < 120 else -1))
            if poses[i] == "whisper":
                p, speaking = whisper_phase(bot, now)
                sx, sy = x + 12, y - 4
                tx, ty = bx - 62, by - 30
                w = spr_walker(species(i), P["base"], int(now * 7) % 2 if not speaking else 0, False, tx < sx)
                px, py = sx + (tx - sx) * p, sy + (ty - sy) * p
                img.paste(w, (int(px), int(py)), w)
                if speaking:
                    over.append(((int(px) + 40, int(py) + 26), bot.say_text, True, -1))
        for h in office.handoffs:
            if h.src >= n or h.dst >= n:
                continue
            p, carrying, arrived = handoff_phase(h, now)
            sx, sy = cells[h.src][0] + 12, cells[h.src][1] - 4
            tx, ty = cells[h.dst][0] - 22, cells[h.dst][1] - 2
            bot = office.bots[h.src]
            moving = p not in (0.0, 1.0)
            if office.remote_label(bot.name):                       # 云端 bot 交接：从云里掉下一个包裹，飞到对方桌上
                cx0, cy0 = cells[h.src][0] + 38, cells[h.src][1] - 6
                cx1, cy1 = cells[h.dst][0] + 62, cells[h.dst][1] + 30
                if carrying:
                    self.package(d, cx0 + (cx1 - cx0) * p, cy0 + (cy1 - cy0) * p - math.sin(p * math.pi) * 30, now)
            else:
                w = spr_walker(species(h.src), bot_pal(bot)["base"], int(now * 7) % 2 if moving else 0, carrying)
                img.paste(w, (int(sx + (tx - sx) * p), int(sy + (ty - sy) * p)), w)
            if arrived:
                ox, oy = cells[h.dst][0] + 56, cells[h.dst][1] + 31
                d.rectangle([ox, oy, ox + 11, oy + 8], (250, 250, 240), outline=(120, 120, 130))
                d.line([(ox + 2, oy + 3), (ox + 9, oy + 3)], (150, 150, 160))
                d.line([(ox + 2, oy + 6), (ox + 9, oy + 6)], (150, 150, 160))
                if now - h.t0 < T_GO + T_DROP + 0.6:
                    self.mark(d, cells[h.dst][0] + 62, cells[h.dst][1] - 2, "!", RED, WHITE)
        for name, t0 in list(office.cloud_return.items()):          # 云端干完：包裹从云里落到桌上
            i = office.index(name)
            if i < 0 or now - t0 > 3.0:
                continue
            q = (now - t0) / 1.6
            cx, cy = cells[i][0] + 38, cells[i][1] - 6
            if q < 1.0:
                self.cloud(d, cx, cy + 2, now, i)
                self.package(d, cx, cy + 10 + (cells[i][1] + 28 - cy) * q * q, now)
            else:
                self.mark(d, cells[i][0] + 62, cells[i][1] - 2, "!", GREEN, WHITE)
        for anchor, text, private, side in over:
            self.bubble(d, anchor, text, private=private, side=side)
        if walk:
            tr, pos, moving = walk
            self.boss_sprite(img, d, int(pos[0]), int(pos[1]))
            cx, cy = cells[tr["bot"]]
            self.tour_overlay(d, now, tr, pos, (cx + 38, cy + 4))
        return self.finish(img, office, now)


# ------------------------------------------------------------------ 俯视小人（A、B 共用）
def token(d, cx, cy, sp, P, u=-1, hands=None, edge=WHITE, neon=False):
    """从正上方看的牛马。u=-1 面朝上（北），u=+1 面朝下。hands = 两只手往前伸多远。"""
    def B(x0, y0, x1, y1):
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]

    body = P["deep"] if neon else P["dark"]
    head = P["dark"] if neon else P["fur"]
    line = P["glow"] if neon else edge
    d.ellipse(B(cx - 13, cy - u * 3 - 7, cx + 13, cy - u * 3 + 7), body, outline=line)
    if hands:
        for s, reach in zip((-1, 1), hands):
            d.line([(cx + s * 11, cy - u * 2), (cx + s * 6, cy + u * reach)], body, width=4)
            d.ellipse(B(cx + s * 6 - 2, cy + u * reach - 2, cx + s * 6 + 2, cy + u * reach + 2), P["fur"], outline=line)
    if sp == "ox":
        horn = P["glow"] if neon else (246, 238, 212)
        for s in (-1, 1):
            d.line([(cx + s * 6, cy + u * 2), (cx + s * 11, cy + u * 5), (cx + s * 10, cy + u * 10)], horn, width=2)
        d.ellipse(B(cx - 5, cy + u * 8 - 4, cx + 5, cy + u * 8 + 4), P["muzzle"] if not neon else P["fur"], outline=line)
        d.ellipse(B(cx - 8, cy - 8, cx + 8, cy + 8), head, outline=line)
        d.rectangle(B(cx - 2, cy + u * 1 - 1, cx + 2, cy + u * 4 + 1), P["deep"] if not neon else P["glow"])
    else:
        for s in (-1, 1):
            d.polygon([(cx + s * 3, cy + u * 4), (cx + s * 9, cy + u * 3), (cx + s * 7, cy + u * 9)], head, outline=line)
        d.ellipse(B(cx - 4, cy + u * 10 - 6, cx + 4, cy + u * 10 + 6), P["light"] if not neon else P["fur"], outline=line)
        d.ellipse(B(cx - 8, cy - 8, cx + 8, cy + 8), head, outline=line)
        d.line([(cx, cy + u * 5), (cx, cy - u * 9)], P["deep"] if not neon else P["glow"], width=3)   # 鬃毛


def boss_token(d, cx, cy, neon=False):
    d.ellipse([cx - 13, cy - 7, cx + 13, cy + 7], (36, 38, 52), outline=WHITE)
    d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], (50, 36, 30) if not neon else (40, 44, 60), outline=WHITE)
    d.ellipse([cx - 5, cy - 10, cx + 5, cy - 3], (240, 200, 160), outline=WHITE)   # 脸朝前
    d.rectangle([cx - 6, cy - 8, cx + 6, cy - 6], INK)                              # 墨镜


# ------------------------------------------------------------------ A 建筑蓝图
class Blueprint(Theme):
    key, title = "blueprint", "A 建筑蓝图"
    BG, GRID, GRID2 = (20, 56, 108), (27, 66, 120), (36, 80, 138)
    LINE = (226, 236, 250)
    BOARD = (214, 8, 314, 232)
    BOSS = (62, 218)

    def cell(self, i, n):
        cols = 3 if n > 4 else 2 if n > 1 else 1
        r, c = divmod(i, cols)
        in_row = min(cols, n - r * cols)
        off = 8 + (196 - in_row * 65) // 2
        return off + c * 65, 24 + r * 68

    def hit_boxes(self, office):
        n = len(office.bots)
        return [(x, y, x + 65, y + 66) for x, y in (self.cell(i, n) for i in range(n))]

    def boss_box(self, office):
        bx, by = self.BOSS
        return (bx - 30, by - 30, bx + 34, by + 16)

    def background(self, d, office, now):
        L = self.LINE
        d.rectangle([0, 0, W, H], self.BG)
        for x in range(0, W, 8):
            d.line([(x, 0), (x, H)], self.GRID2 if x % 40 == 0 else self.GRID)
        for y in range(0, H, 8):
            d.line([(0, y), (W, y)], self.GRID2 if y % 40 == 0 else self.GRID)
        # 外墙和隔墙（留出门洞）
        d.rectangle([5, 19, 206, 235], outline=L, width=2)
        for a, b in ((5, 34), (58, 132), (156, 206)):
            d.line([(a, 161), (b, 161)], L, width=2)
        d.line([(98, 161), (98, 235)], L, width=2)
        d.arc([34 - 24, 161 - 24, 34 + 24, 161 + 24], 0, 90, L)          # 门的开启弧线
        d.line([(34, 161), (34, 185)], L)
        d.arc([132 - 24, 161 - 24, 132 + 24, 161 + 24], 0, 90, L)
        d.line([(132, 161), (132, 185)], L)
        faint = (120, 158, 208)
        d.text((124, 214), "茶水间", font=self.font, fill=faint)
        # 茶水间家具：沙发、圆桌、咖啡机、盆栽
        d.rounded_rectangle([166, 214, 200, 230], 4, outline=L)
        d.line([(170, 218), (196, 218)], L)
        d.ellipse([160, 190, 180, 210], outline=L)
        d.rectangle([184, 166, 200, 178], outline=L)
        d.ellipse([188, 169, 194, 175], outline=L)
        d.ellipse([104, 212, 122, 230], outline=L)
        for a in range(0, 360, 60):
            d.line([(113, 221), (113 + 8 * math.cos(math.radians(a)), 221 + 8 * math.sin(math.radians(a)))], L)
        d.text((10, 164), "老板室", font=self.font, fill=faint)
        # 标题栏
        text, col = self.status_text(office, now)
        d.text((8, 3), self.fit(office.group, 110), font=self.font, fill=L)
        blink = col == RED and int(now * 2) % 2 == 0
        tw = self.font.getlength(text)
        d.ellipse([196 - tw - 10, 6, 196 - tw - 4, 12], col)
        d.text((198 - tw, 3), text, font=self.font, fill=WHITE if blink else col)
        # 黑板
        x0, y0, x1, y1 = self.BOARD
        d.rectangle([x0 - 4, y0 - 4, x1 + 4, y1 + 4], (150, 108, 66), outline=(96, 66, 40))
        d.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], (22, 30, 28))

    def station(self, d, x, y, bot, i, pose, now):
        L, P = self.LINE, bot_pal(bot)
        cx, cy = x + 32, y + 36
        lit = pose in ("type", "panic", "jump")
        d.rectangle([x + 6, y + 2, x + 58, y + 20], (30, 72, 130), outline=L)                 # 桌
        d.rectangle([x + 20, y + 4, x + 44, y + 8], P["glow"] if lit else (70, 104, 156), outline=L)  # 显示器
        d.rectangle([x + 22, y + 12, x + 42, y + 17], outline=L)                              # 键盘
        if pose in ("away", "whisper"):
            d.arc([cx - 13, cy - 12, cx + 13, cy + 12], 20, 160, L)                           # 空椅子
        else:
            f = int(now * (14 if pose == "panic" else 8))
            dy = {"sleep": -9}.get(pose, 0)
            if pose == "jump":
                dy = self.jump_dy(self._office, now, 12, i)
                d.ellipse([cx - 12, cy + 10, cx + 12, cy + 16], outline=L)               # 起跳留下的影圈
            jx = jy = 0
            if pose == "panic":
                jx, jy = self.jitter(now, i, 2)
            hands = None
            if pose in ("type", "panic"):
                amp = 5 if pose == "panic" else 3
                hands = (20 + (f % 2) * amp, 20 + (1 - f % 2) * amp)
            elif pose == "sleep":
                hands = (13, 13)
            elif pose == "jump":
                hands = (-14, -14)
            elif pose == "wait":
                hands = (4, 20)
            d.arc([cx - 14, cy - 14, cx + 14, cy + 13], 20, 160, L)
            token(d, cx + jx, cy + dy + jy, species(i), P, -1, hands=hands, edge=L)
            if pose == "cloud" or (office_remote(self._office, bot) and pose in ("type", "panic")):
                self.cloud(d, cx, y - 8, now, i)
            elif pose in ("type", "panic"):
                self.type_fx(d, cx, y + 24, now, i, P["glow"], spread=26, rise=22)
            if pose == "panic":
                self.panic_fx(d, cx, cy - 2, now, i, w=20)
        d.text((cx, y + 59), self.fit(bot.name, 62), font=self.font, fill=WHITE, anchor="mm")

    def render(self, office, now):
        self._office = office
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        self.background(d, office, now)
        b = self.BOARD
        self.board(d, office, now, (b[0] + 3, b[1] + 1, b[2] - 2, b[3]), title="群聊黑板")
        n = len(office.bots)
        cells = [self.cell(i, n) for i in range(n)]
        bx, by = self.BOSS
        stops = [(x + 6, y + 40) for x, y in cells]
        walk = self.boss_walk(office, now, (bx, by), stops)
        d.rectangle([bx - 24, by - 26, bx + 24, by - 14], (30, 72, 130), outline=self.LINE)   # 老板桌
        if walk is None:
            boss_token(d, bx, by)
        d.text((bx + 18, by), "我", font=self.font, fill=WHITE, anchor="lm")
        poses = [pose_of(office, i, now) for i in range(n)]
        for i, bot in enumerate(office.bots):
            self.station(d, cells[i][0], cells[i][1], bot, i, poses[i], now)
        over = []
        for i, bot in enumerate(office.bots):
            x, y = cells[i]
            P = bot_pal(bot)
            cx, cy = x + 32, y + 36
            if poses[i] == "sleep":
                self.zzz(d, cx + 14, cy - 12, now, i, WHITE)
            elif poses[i] == "wait":
                self.mark(d, cx + 22, cy - 12, "!", YELLOW)
            text = talking(bot, now)
            if text and poses[i] not in ("away", "whisper"):
                over.append(((cx + 6, cy - 8), text, False, 1 if cx < 110 else -1))
            if poses[i] == "whisper":
                p, speaking = whisper_phase(bot, now)
                tx, ty = bx - 32, by
                px, py = cx + (tx - cx) * p, cy + (ty - cy) * p
                token(d, px, py, species(i), P, -1, edge=self.LINE)
                if speaking:
                    over.append(((int(px) + 8, int(py) - 10), bot.say_text, True, 1))
        for h in office.handoffs:
            if h.src >= n or h.dst >= n:
                continue
            p, carrying, arrived = handoff_phase(h, now)
            sx, sy = cells[h.src][0] + 32, cells[h.src][1] + 36
            tx, ty = cells[h.dst][0] + 6, cells[h.dst][1] + 40
            px, py = sx + (tx - sx) * p, sy + (ty - sy) * p
            if 0 < p < 1:                                                   # 脚印虚线
                for k in range(0, int(p * 10)):
                    q = k / 10
                    d.point([(sx + (tx - sx) * q, sy + (ty - sy) * q)], self.LINE)
            token(d, px, py, species(h.src), bot_pal(office.bots[h.src]), -1, edge=self.LINE)
            if carrying:
                d.rectangle([px - 5, py - 20, px + 5, py - 10], WHITE, outline=INK)
            if arrived:
                ox, oy = cells[h.dst][0] + 46, cells[h.dst][1] + 9
                d.rectangle([ox, oy, ox + 9, oy + 9], WHITE, outline=INK)
                if now - h.t0 < T_GO + T_DROP + 0.6:
                    self.mark(d, cells[h.dst][0] + 54, cells[h.dst][1] + 24, "!", RED, WHITE)
        for anchor, text, private, side in over:
            self.bubble(d, anchor, text, private=private, side=side, max_w=106, bounds=(2, 2, 208, H - 2))
        if walk:
            tr, pos, moving = walk
            boss_token(d, pos[0], pos[1])
            cx, cy = cells[tr["bot"]][0] + 32, cells[tr["bot"]][1] + 36
            self.tour_overlay(d, now, tr, (pos[0], pos[1] - 4), (cx, cy - 22), bounds=(2, 2, 208, H - 2))
        return self.finish(img, office, now, hit=(150, 96))



# ------------------------------------------------------------------ B 夜班作战室
class NeonRoom(Theme):
    key, title = "neon", "B 夜班作战室"
    BG = (9, 11, 20)
    BOARD = (10, 8, 310, 78)
    TABLE = (64, 140, 250, 188)
    BOSS = (284, 164)

    def seat(self, i, n):
        top = (n + 1) // 2
        row, k = (0, i) if i < top else (1, i - top)
        count = top if row == 0 else n - top
        x0, x1 = self.TABLE[0] + 30, self.TABLE[2] - 30
        x = (x0 + x1) // 2 if count == 1 else x0 + (x1 - x0) * k // (count - 1)
        return (x, 122, 1) if row == 0 else (x, 206, -1)

    def hit_boxes(self, office):
        n = len(office.bots)
        return [(x - 26, y - 28, x + 26, y + 28) for x, y, _ in (self.seat(i, n) for i in range(n))]

    def boss_box(self, office):
        bx, by = self.BOSS
        return (bx - 24, by - 30, bx + 24, by + 40)

    def render(self, office, now):
        from PIL import ImageChops, ImageFilter
        self._office = office
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        cyan, mag = (60, 220, 240), (240, 80, 200)
        for x in range(0, W, 16):                                          # 地面网格
            d.line([(x, 86), (x, H)], (16, 22, 38))
        for y in range(86, H, 16):
            d.line([(0, y), (W, y)], (16, 22, 38))
        d.rectangle([2, 86, W - 3, H - 3], outline=(40, 70, 110))
        bx0, by0, bx1, by1 = self.BOARD
        d.rectangle([bx0 - 4, by0 - 4, bx1 + 4, by1 + 4], (12, 16, 24), outline=cyan)
        tx0, ty0, tx1, ty1 = self.TABLE
        d.rounded_rectangle([tx0, ty0, tx1, ty1], 14, fill=(18, 24, 40), outline=mag, width=2)
        d.line([(tx0 + 18, (ty0 + ty1) // 2), (tx1 - 18, (ty0 + ty1) // 2)], (50, 30, 70))
        # 左侧状态灯
        text, col = self.status_text(office, now)
        lx, ly = 30, 150
        pulse = 0.5 + 0.5 * math.sin(now * (6 if col == RED else 2))
        d.ellipse([lx - 12, ly - 12, lx + 12, ly + 12], tuple(int(c * (0.5 + 0.5 * pulse)) for c in col), outline=col)
        r = 14 + int(pulse * 6)
        d.ellipse([lx - r, ly - r, lx + r, ly + r], outline=tuple(int(c * 0.6) for c in col))
        n = len(office.bots)
        seats = [self.seat(i, n) for i in range(n)]
        poses = [pose_of(office, i, now) for i in range(n)]
        bx, by = self.BOSS
        walk = self.boss_walk(office, now, (bx, by), [(x - 26, y + u * 4) for x, y, u in seats])
        d.rounded_rectangle([bx - 20, by - 26, bx + 20, by + 26], 8, outline=(90, 96, 120))
        if walk is None:
            boss_token(d, bx, by, neon=True)
        else:
            tr, pos, moving = walk
            boss_token(d, pos[0], pos[1], neon=True)
        whisperers = []
        for i, bot in enumerate(office.bots):
            x, y, u = seats[i]
            P = bot_pal(bot)
            pose = poses[i]
            f = int(now * (14 if pose == "panic" else 8))
            lit = pose in ("type", "panic", "jump")
            ly0 = y + u * 20
            d.rectangle([x - 10, min(ly0, ly0 + u * 9), x + 10, max(ly0, ly0 + u * 9)],
                        P["glow"] if lit else (26, 32, 48), outline=P["glow"])       # 笔记本
            if lit:
                for k in range(3):
                    yy = ly0 + u * (2 + k * 3)
                    d.line([(x - 7, yy), (x - 7 + (f * 3 + k * 5 + i) % 13, yy)], P["deep"])
            if pose == "away":
                pose = "idle"                                              # 交接是数据包飞过桌面，人不离座
            if pose == "whisper":
                whisperers.append(i)
                d.ellipse([x - 9, y - 9, x + 9, y + 9], outline=(60, 66, 86))
                continue
            dy = {"sleep": u * 9, "jump": self.jump_dy(office, now, 16, i) * u}.get(pose, 0)
            if pose == "panic":
                x, dy = x + self.jitter(now, i, 2)[0], self.jitter(now, i, 2)[1]
            if pose == "cloud" or (office_remote(office, bot) and pose in ("type", "panic")):
                self.cloud(d, x, y - u * 34, now, i, col=(40, 60, 110), edge=P["glow"])
            elif pose in ("type", "panic"):
                self.type_fx(d, x, y + u * 36, now, i, P["glow"], spread=30, rise=24)
            if pose == "panic":
                self.panic_fx(d, x, y, now, i, w=24)
            hands = None
            if pose in ("type", "panic"):
                amp = 5 if pose == "panic" else 3
                hands = (20 + (f % 2) * amp, 20 + (1 - f % 2) * amp)
            elif pose == "sleep":
                hands = (12, 12)
            elif pose == "jump":
                hands = (-13, -13)
            elif pose == "wait":
                hands = (3, 19)
            token(d, x, y + dy, species(i), P, u, hands=hands, neon=True)
        for i in whisperers:
            bot = office.bots[i]
            x, y, u = seats[i]
            p, speaking = whisper_phase(bot, now)
            tx, ty = bx - 6, by - u * (-30)
            px, py = x + (tx - x) * p, y + (ty - y) * p
            token(d, px, py, species(i), bot_pal(bot), -1 if py > by else 1, neon=True)
        for h in office.handoffs:                                          # 交接：发光的数据包
            if h.src >= n or h.dst >= n:
                continue
            p, carrying, arrived = handoff_phase(h, now)
            sx, sy, su = seats[h.src]
            tx, ty, tu = seats[h.dst]
            sy, ty = sy + su * 24, ty + tu * 24
            col = bot_pal(office.bots[h.src])["glow"]
            if carrying:
                for k in range(6):
                    q = max(0.0, p - k * 0.05)
                    px, py = sx + (tx - sx) * q, sy + (ty - sy) * q
                    r = 5 - k * 0.7
                    d.ellipse([px - r, py - r, px + r, py + r], col if k == 0 else tuple(int(c * (0.8 - k * 0.12)) for c in col))
            elif now - h.t0 < T_GO + T_DROP + 0.6:
                d.ellipse([tx - 9, ty - 9, tx + 9, ty + 9], outline=col, width=2)
        # 辉光：把整张图模糊后叠回去，文字在这之后画，保证清晰
        glow = img.filter(ImageFilter.GaussianBlur(3)).point(lambda v: int(v * 0.75))
        img = ImageChops.add(img, glow)
        d = ImageDraw.Draw(img)
        d.rectangle([bx0, by0, bx1, by1], (12, 16, 24))
        self.board(d, office, now, (bx0 + 4, by0 + 1, bx1 - 4, by1), chalk=(214, 240, 244))
        blink = col == RED and int(now * 2) % 2 == 0
        d.text((lx, ly + 30), text.split(" ")[0], font=self.font, fill=WHITE if blink else col, anchor="mm")
        if " " in text:
            d.text((lx, ly + 44), text.split(" ")[1], font=self.font, fill=WHITE if blink else col, anchor="mm")
        d.text((lx, 100), self.fit(office.group, 56), font=self.font, fill=(150, 170, 200), anchor="mm")
        d.text((bx, by + 36), "我", font=self.font, fill=WHITE, anchor="mm")
        over = []
        for i, bot in enumerate(office.bots):
            x, y, u = seats[i]
            P = bot_pal(bot)
            d.text((x, y - u * 22), self.fit(bot.name, 58), font=self.font, fill=P["glow"], anchor="mm")
            if poses[i] == "sleep":
                self.zzz(d, x + 14, y - 6, now, i, P["glow"])
            elif poses[i] == "wait":
                self.mark(d, x + 22, y - 6, "!", YELLOW)
            text_ = talking(bot, now)
            if text_ and poses[i] != "whisper":
                over.append(((x + 8, y - 4), text_, False, 1 if x < 170 else -1, P))
        for i in whisperers:
            bot = office.bots[i]
            p, speaking = whisper_phase(bot, now)
            if speaking:
                u = seats[i][2]
                over.append(((bx - 14, by + (30 if u < 0 else -30) - 6), bot.say_text, True, -1, bot_pal(bot)))
        for h in office.handoffs:
            if h.dst < n and handoff_phase(h, now)[2] and now - h.t0 < T_GO + T_DROP + 0.6:
                self.mark(d, seats[h.dst][0] + 22, seats[h.dst][1] - 6, "!", RED, WHITE)
        for anchor, text_, private, side, P in over:
            self.bubble(d, anchor, text_, private=private, side=side, bg=(16, 20, 34), fg=WHITE, edge=P["glow"],
                        bounds=(2, 84, W - 2, H - 2))
        if walk:
            tr, pos, moving = walk
            sx, sy, su = seats[tr["bot"]]
            self.tour_overlay(d, now, tr, (pos[0], pos[1] - 10), (sx, sy - su * 14), bounds=(2, 84, W - 2, H - 2))
        return self.finish(img, office, now, rope=(240, 80, 200), edge=(60, 10, 50), hit=(230, 120))




# ------------------------------------------------------------------ C 牛马牧场
class Barn(PixelOffice):
    def boss_sprite(self, img, d, bx, by):
        boss = spr_boss()
        img.paste(boss, (bx - 22, by - 34), boss)
        d.ellipse([bx - 17, by - 30, bx + 17, by - 22], (236, 204, 104), outline=(150, 112, 40))   # 草帽
        d.rectangle([bx - 8, by - 38, bx + 8, by - 26], (236, 204, 104), outline=(150, 112, 40))
        d.rectangle([bx - 8, by - 30, bx + 8, by - 28], (200, 60, 50))

    key, title = "barn", "C 牛马牧场"
    CHAIR, TOP, FRONT, FRONT_EDGE = (226, 192, 92), (196, 146, 88), (146, 100, 58), (108, 70, 40)
    LAPTOP, NAME_FG = (70, 72, 84), (255, 240, 205)
    PLANK = (120, 80, 46)
    CHALK_ = (255, 244, 214)

    def chair(self, d, x, y):                                             # 草垛当椅背
        d.rounded_rectangle([x + 12, y + 12, x + 65, y + 54], 8, fill=self.CHAIR, outline=(176, 134, 48))
        for k in range(7):
            sx = x + 16 + k * 7
            d.line([(sx, y + 16 + (k * 5) % 9), (sx + 4, y + 26 + (k * 7) % 11)], (186, 146, 58))

    def background(self, d, office, now):
        for y in range(0, 79):                                              # 天空渐变
            t = y / 78
            d.line([(0, y), (W, y)], (int(120 + 90 * t), int(190 + 40 * t), int(240 + 8 * t)))
        sx, sy = 292, 24                                                    # 太阳，光线慢慢转
        for k in range(10):
            a = k * math.pi / 5 + now * 0.4
            d.line([(sx + 13 * math.cos(a), sy + 13 * math.sin(a)), (sx + 20 * math.cos(a), sy + 20 * math.sin(a))],
                   (255, 226, 120), width=2)
        d.ellipse([sx - 10, sy - 10, sx + 10, sy + 10], (255, 214, 90))
        for k, (cy, spd, wd) in enumerate(((16, 7, 34), (46, 4, 44))):      # 云飘过
            cx = (now * spd + k * 130) % (W + 90) - 45
            d.ellipse([cx - wd / 2, cy - 7, cx + wd / 2, cy + 7], (252, 252, 255))
            d.ellipse([cx - wd / 4, cy - 12, cx + wd / 4, cy + 3], (252, 252, 255))
        d.ellipse([-40, 56, 150, 110], (112, 178, 98))                      # 远山
        d.ellipse([110, 60, 330, 120], (92, 160, 86))
        d.rectangle([0, 74, W, 78], (96, 62, 36))
        d.rectangle([0, 79, 238, H], (150, 100, 58))                        # 牛棚木板墙
        for x in range(0, 238, 13):
            d.line([(x, 79), (x, H)], (130, 86, 48))
        for y in range(79, H, 12):
            d.rectangle([0, y + 9, 238, y + 11], (118, 78, 44))
        d.rectangle([0, H - 14, 238, H], (214, 178, 84))                    # 地上铺的干草
        for x in range(0, 238, 6):
            d.line([(x, H - 14), (x + 3, H - 8 + x % 5)], (190, 150, 60))
        d.rectangle([242, 79, W, H], (110, 178, 98))                        # 院子草地
        for y in range(84, H, 10):
            for x in range(246 + (y % 20), W, 20):
                d.line([(x, y), (x + 1, y - 3)], (84, 150, 76))
        for y in (100, 150, 200):                                           # 白栅栏
            d.rectangle([242, y, W, y + 3], (240, 236, 224))
        d.rectangle([238, 79, 243, H], (96, 62, 36))
        x0, y0, x1, y1 = self.BOARD
        d.rectangle([x0 - 5, y0 - 4, x1 + 5, y1 + 4], (150, 104, 60), outline=(96, 62, 36))     # 木牌子
        d.rectangle([x0 - 2, y0 - 1, x1 + 2, y1 + 1], (58, 40, 30))
        text, col = self.status_text(office, now)
        d.rounded_rectangle([244, 8, 314, 44], 4, fill=(120, 80, 46), outline=(80, 52, 30))
        d.text((279, 18), self.fit(office.group, 64), font=self.font, fill=(255, 240, 205), anchor="mm")
        blink = col == RED and int(now * 2) % 2 == 0
        d.text((279, 34), text, font=self.font, fill=WHITE if blink else col, anchor="mm")
        d.text((279, 60), "农场主", font=self.font, fill=(70, 50, 30), anchor="mm")
        d.polygon([(273, 70), (285, 70), (279, 76)], (70, 50, 30))

    def boss(self, img, d, bx, by, away=False):
        d.rectangle([252, by + 22, 314, by + 30], (170, 120, 70))
        d.rectangle([254, by + 31, 312, by + 52], (126, 86, 50))
        if not away:
            self.boss_sprite(img, d, bx, by)
        d.rectangle([252, by + 22, 314, by + 30], (170, 120, 70))
        d.text((283, by + 42), "我", font=self.font, fill=(255, 236, 190), anchor="mm")


# ------------------------------------------------------------------ D 太空舰桥
class Space(PixelOffice):
    key, title = "space", "D 太空舰桥"

    def boss_sprite(self, img, d, bx, by):
        boss = spr_boss()
        img.paste(boss, (bx - 22, by - 34), boss)
        d.rectangle([bx - 9, by - 33, bx + 9, by - 29], (30, 50, 120), outline=(190, 210, 255))    # 舰长帽
        d.rectangle([bx - 2, by - 32, bx + 2, by - 30], YELLOW)

    CHAIR, TOP, FRONT, FRONT_EDGE = (34, 42, 72), (92, 108, 148), (44, 56, 90), (60, 200, 232)
    LAPTOP, NAME_FG = (20, 26, 44), (170, 236, 255)
    CY = (60, 200, 232)

    def chair(self, d, x, y):                                             # 座舱椅
        d.rounded_rectangle([x + 14, y + 10, x + 63, y + 54], 10, fill=self.CHAIR, outline=(70, 90, 140))
        d.line([(x + 22, y + 16), (x + 22, y + 46)], (60, 80, 130))
        d.line([(x + 55, y + 16), (x + 55, y + 46)], (60, 80, 130))

    def background(self, d, office, now):
        d.rectangle([0, 0, W, 78], (10, 14, 30))                           # 舱壁
        d.rectangle([0, 74, W, 78], (50, 64, 104))
        d.rectangle([0, 79, 238, H], (20, 26, 46))                          # 金属地板
        for x in range(0, 238, 26):
            d.line([(x, 79), (x, H)], (32, 42, 72))
        for y in range(90, H, 26):
            d.line([(0, y), (238, y)], (32, 42, 72))
        for k in range(0, 238, 13):                                         # 地板导光条，一格格流动
            if (k // 13 + int(now * 4)) % 6 == 0:
                d.rectangle([k, H - 6, k + 9, H - 4], self.CY)
        d.rectangle([242, 79, W, H], (14, 20, 40))
        d.rectangle([238, 79, 243, H], (70, 84, 120))
        for y in range(80, H, 14):                                          # 舱门黄黑警示线
            d.polygon([(238, y), (243, y), (243, y + 7), (238, y + 7)], YELLOW if (y // 14) % 2 else INK)
        x0, y0, x1, y1 = self.BOARD
        d.rectangle([x0 - 5, y0 - 4, x1 + 5, y1 + 4], (24, 34, 62), outline=self.CY)               # 全息屏
        d.rectangle([x0 - 2, y0 - 1, x1 + 2, y1 + 1], (6, 14, 30))
        for k in range(0, x1 - x0 + 10, 8):                                 # 边框跑马灯
            on = (k // 8 + int(now * 6)) % 5 == 0
            d.rectangle([x0 - 5 + k, y0 - 4, x0 - 2 + k, y0 - 3], WHITE if on else (30, 90, 110))
        scan = y0 + int(now * 22) % (y1 - y0)                                # 扫描线
        d.line([(x0 - 2, scan), (x1 + 2, scan)], (20, 60, 80))
        text, col = self.status_text(office, now)
        d.rounded_rectangle([244, 8, 314, 44], 4, fill=(14, 22, 44), outline=self.CY)
        d.text((279, 18), self.fit(office.group, 64), font=self.font, fill=(220, 244, 255), anchor="mm")
        blink = col == RED and int(now * 2) % 2 == 0
        d.text((279, 34), text, font=self.font, fill=WHITE if blink else col, anchor="mm")
        wx0, wy0, wx1, wy1 = 246, 49, 312, 76                               # 舷窗：行星、星星、路过的飞船
        d.rounded_rectangle([wx0, wy0, wx1, wy1], 6, fill=(4, 6, 16), outline=(90, 110, 160), width=2)
        for k in range(14):
            sx = wx0 + 4 + (k * 37) % (wx1 - wx0 - 8)
            sy = wy0 + 4 + (k * 19) % (wy1 - wy0 - 8)
            tw_ = (math.sin(now * 3 + k * 1.7) + 1) / 2
            c = int(90 + 160 * tw_)
            d.point([(sx, sy)], (c, c, min(255, c + 20)))
        d.ellipse([wx1 - 30, wy0 + 8, wx1 - 6, wy0 + 32], (70, 110, 210))
        d.arc([wx1 - 36, wy0 + 14, wx1, wy0 + 28], 200, 340, (170, 200, 255))
        shipx = wx0 + (now * 9) % (wx1 - wx0)
        d.polygon([(shipx, wy0 + 6), (shipx + 5, wy0 + 8), (shipx, wy0 + 10)], (220, 230, 255))

    def boss(self, img, d, bx, by, away=False):
        d.rectangle([252, by + 22, 314, by + 30], (92, 108, 148))
        d.rectangle([254, by + 31, 312, by + 52], (30, 40, 72), outline=self.CY)
        for k in range(5):                                                  # 指挥台上闪烁的灯
            on = (k + int(now_t(self))) % 2 == 0
            d.rectangle([260 + k * 11, by + 36, 264 + k * 11, by + 40], self.CY if on else (30, 80, 100))
        if not away:
            self.boss_sprite(img, d, bx, by)
        d.rectangle([252, by + 22, 314, by + 30], (92, 108, 148))
        d.text((283, by + 46), "舰长", font=self.font, fill=(170, 236, 255), anchor="mm")


def now_t(theme):
    return time.time() * 2


# ------------------------------------------------------------------ 新皮肤共用的小工具
BLUE = (110, 180, 255)


def lerp(a, b, t):
    return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(len(a)))


def sprite_pose(pose, now, i):
    if pose in ("away", "cloud", "whisper"):
        return "idle"
    if pose == "idle" and (int(now * 10) + i * 13) % 37 == 0:
        return "blink"
    return pose


def paste_clip(dst, spr, x, y, box):
    """把 spr 贴到 (x, y)，只露出 box 里面的部分。"""
    bx0, by0, bx1, by1 = box
    cx0, cy0 = max(bx0 - x, 0), max(by0 - y, 0)
    cx1, cy1 = min(bx1 - x, spr.width), min(by1 - y, spr.height)
    if cx1 <= cx0 or cy1 <= cy0:
        return
    part = spr.crop((cx0, cy0, cx1, cy1))
    dst.paste(part, (x + cx0, y + cy0), part)


def small_boss():
    return spr_boss().resize((22, 30), Image.NEAREST)


def small_walker(sp, color, f, carrying, flip=False):
    return spr_walker(sp, color, f, carrying, flip).resize((26, 34), Image.NEAREST)


def file_icon(d, x, y, col=(250, 250, 240)):
    d.polygon([(x - 5, y - 6), (x + 2, y - 6), (x + 5, y - 3), (x + 5, y + 6), (x - 5, y + 6)], col, outline=INK)
    for yy in (-2, 1, 4):
        d.line([(x - 3, y + yy), (x + 3, y + yy)], (150, 150, 160))


# ------------------------------------------------------------------ E 视频会议
class Meeting(Theme):
    key, title = "meeting", "E 视频会议"
    GAUGE_ON_BOARD = False
    TW, TH = 78, 72
    BAR = (24, 26, 33)

    def tile_xy(self, k):
        c, r = k % 4, k // 4
        return 2 + c * 79, 19 + r * 74

    def hit_boxes(self, office):
        return [(x, y, x + self.TW, y + self.TH) for x, y in (self.tile_xy(i) for i in range(len(office.bots)))]

    def boss_box(self, office):
        x, y = self.tile_xy(len(office.bots))
        return (x, y, x + self.TW, y + self.TH)

    def tile_center(self, k):
        x, y = self.tile_xy(k)
        return x + self.TW // 2, y + self.TH // 2

    # ---- 一格摄像头 ----
    def bot_tile(self, img, d, k, bot, i, pose, now, office):
        TW, TH = self.TW, self.TH
        x, y = self.tile_xy(k)
        P = bot_pal(bot)
        tile = Image.new("RGB", (TW, TH), P["deep"])
        td = ImageDraw.Draw(tile)
        remote = office.remote_label(bot.name)
        cloud_work = pose == "cloud" or (bool(remote) and pose in ("type", "panic"))
        if pose == "sleep":                                               # 睡着 = 摄像头关了
            td.rectangle([0, 0, TW, TH], (36, 38, 48))
            td.ellipse([TW / 2 - 15, 5, TW / 2 + 15, 35], P["fur"])
            td.text((TW / 2, 20), bot.name[:1], font=self.big, fill=WHITE, anchor="mm")
            td.text((TW / 2, 46), "摄像头已关", font=self.font, fill=(130, 134, 150), anchor="mm")
            self.zzz(td, TW / 2 + 16, 14, now, i, (170, 176, 200))
        elif cloud_work:                                                  # 云端干活 = 共享屏幕
            td.rectangle([0, 0, TW, TH], (20, 22, 30))
            td.rectangle([0, 0, TW, 11], (40, 44, 60))
            td.text((3, -1), "共享屏幕", font=self.font, fill=BLUE)
            for row in range(7):
                h = (row * 2654435761 + int(now * 3) * 97 + i * 31) & 0xFFFF
                x0 = 4 + (h % 3) * 6
                td.line([(x0, 16 + row * 6), (min(TW - 4, x0 + 10 + (h >> 4) % 40), 16 + row * 6)], CODE[(h >> 9) % 4], width=2)
            st = office.remote_state.get(bot.name)
            if st and st["state"] == "working":
                td.text((4, TH - 30), fmt_dur(now - st["since"]), font=self.font, fill=BLUE)
            face = spr_front(species(i), P["base"], "idle", 0).resize((44, 30), Image.NEAREST)  # 右下角小窗是本人
            td.rectangle([TW - 30, TH - 34, TW - 2, TH - 16], P["deep"], outline=(90, 94, 110))
            paste_clip(tile, face, TW - 38, TH - 37, (TW - 29, TH - 33, TW - 2, TH - 16))
            self.cloud(td, TW - 16, 20, now, i)
        else:
            for yy in range(TH):                                          # 背景：自己家的墙 + 书架
                td.line([(0, yy), (TW, yy)], lerp(P["dark"], P["deep"], 0.35 + 0.65 * yy / TH))
            td.rectangle([4, 16, 26, 17], lerp(P["deep"], INK, 0.5))
            for b in range(5):
                td.rectangle([5 + b * 4, 8 + (b * 3) % 4, 7 + b * 4, 15], CODE[(b + i) % 4])
            td.ellipse([TW - 16, 6, TW - 6, 16], (250, 230, 160) if pose != "sleep" else (60, 60, 60))  # 台灯
            sp = sprite_pose(pose, now, i)
            if pose == "whisper" or (talking(bot, now) and sp == "idle"):
                sp = "talk" if int(now * 6) % 2 else "idle"
            f = int(now * (16 if pose == "panic" else 10)) % 4
            dy = self.jump_dy(office, now, 10, i) if pose == "jump" else 0
            jx, jy = self.jitter(now, i, 2) if pose == "panic" else (0, 0)
            s = spr_front(species(i), P["base"], sp, f if sp in ("type", "panic") else 0)
            tile.paste(s, (TW // 2 - 44 + jx, TH - 58 + dy + jy), s)
            td.rectangle([16, TH - 8, TW - 16, TH], (40, 42, 52))           # 键盘边
            if pose in ("type", "panic"):
                self.type_fx(td, 12, 40, now, i, P["light"], spread=10, rise=20)
                self.type_fx(td, TW - 12, 40, now, i, P["light"], spread=10, rise=20)
            if pose == "panic":
                self.panic_fx(td, TW // 2, 16, now, i, w=22)
            if pose == "wait":                                            # 举手
                td.rounded_rectangle([TW - 32, 3, TW - 3, 17], 4, fill=YELLOW)
                td.text((TW - 17, 10), "举手", font=self.font, fill=INK, anchor="mm")
            if pose == "whisper":
                td.rounded_rectangle([3, 3, 44, 17], 4, fill=(120, 90, 200))
                td.text((24, 10), "私聊中", font=self.font, fill=WHITE, anchor="mm")
        # 名字 + 麦克风
        nm = self.fit(bot.name, 46)
        nw = self.font.getlength(nm)
        td.rectangle([2, TH - 15, nw + 18, TH - 2], (10, 10, 14))
        speaking = talking(bot, now) or pose == "whisper"
        mic = GREEN if speaking or pose in ("type", "panic") else (200, 70, 70)
        td.rounded_rectangle([5, TH - 12, 8, TH - 6], 1, fill=mic)
        if not (speaking or pose in ("type", "panic")):
            td.line([(3, TH - 4), (10, TH - 13)], (200, 70, 70))
        td.text((12, TH - 9), nm, font=self.font, fill=WHITE, anchor="lm")
        if remote:
            self.remote_bar(td, office, bot, nw + 22, TH - 10, TW - nw - 28)
        img.paste(tile, (x, y))
        # 边框：说话绿框，悄悄话紫色虚线
        if speaking and pose != "whisper":
            w = 2 + int(abs(math.sin(now * 8)) * 1.5)
            d.rectangle([x - 1, y - 1, x + TW, y + TH], outline=GREEN, width=w)
            for b in range(4):                                            # 声音条
                hgt = 2 + int(abs(math.sin(now * 12 + b * 1.3)) * 6)
                d.rectangle([x + TW - 20 + b * 4, y + TH - 5 - hgt, x + TW - 18 + b * 4, y + TH - 5], GREEN)
        elif pose == "whisper":
            self._dashed_box(d, x - 1, y - 1, x + TW, y + TH, (180, 140, 255))
        else:
            d.rectangle([x - 1, y - 1, x + TW, y + TH], outline=(52, 54, 66))

    def me_tile(self, img, d, k, office, now, tour):
        x, y = self.tile_xy(k)
        TW, TH = self.TW, self.TH
        d.rectangle([x, y, x + TW - 1, y + TH - 1], (44, 40, 36))
        for yy in range(y, y + 40, 6):                                    # 老板身后的落地窗百叶
            d.line([(x + 6, yy), (x + TW - 7, yy)], (70, 64, 56))
        img.paste(spr_boss(), (x + TW // 2 - 22, y + TH - 56), spr_boss())
        d.rectangle([x + 2, y + TH - 15, x + 50, y + TH - 2], (10, 10, 14))
        d.text((x + 6, y + TH - 9), "我·主持人", font=self.font, fill=YELLOW, anchor="lm")
        d.rectangle([x - 1, y - 1, x + TW, y + TH], outline=YELLOW if tour else (52, 54, 66), width=2 if tour else 1)

    def info_tile(self, d, k, office, now):
        x, y = self.tile_xy(k)
        d.rectangle([x, y, x + self.TW - 1, y + self.TH - 1], (28, 30, 40), outline=(52, 54, 66))
        d.text((x + 5, y + 3), "额度", font=self.font, fill=(170, 176, 190))
        if office.usage:
            self.gauge_strip(d, office, now, (x + 4, y + 18, x + self.TW - 4, y + self.TH - 4), WHITE)
        else:
            d.text((x + 5, y + 24), "未配置", font=self.font, fill=(110, 114, 130))

    def toolbar(self, d, now):
        y0 = 210
        d.rectangle([0, y0, W, H], self.BAR)
        items = (("mic", "静音"), ("cam", "视频"), ("pie", "发大饼"), ("whip", "催一下"), ("skin", "换肤"))
        for k, (ic, label) in enumerate(items):
            cx, cy = 26 + k * 52, y0 + 10
            col = {"pie": (246, 200, 120), "whip": (255, 140, 110)}.get(ic, (200, 204, 216))
            if ic == "mic":
                d.rounded_rectangle([cx - 3, cy - 6, cx + 3, cy + 2], 3, fill=col)
                d.arc([cx - 6, cy - 4, cx + 6, cy + 5], 0, 180, col)
            elif ic == "cam":
                d.rounded_rectangle([cx - 8, cy - 5, cx + 3, cy + 4], 2, fill=col)
                d.polygon([(cx + 3, cy - 1), (cx + 8, cy - 5), (cx + 8, cy + 4)], col)
            elif ic == "pie":
                self.pie(d, cx, cy, 6)
            elif ic == "whip":
                d.line([(cx - 7, cy + 6), (cx - 2, cy - 2)], (120, 70, 30), width=3)
                d.arc([cx - 4, cy - 8, cx + 10, cy + 6], 180, 330, col, width=2)
            else:
                for j, c in enumerate((RED, YELLOW, GREEN, BLUE)):
                    a = j * math.pi / 2 + 0.6
                    d.ellipse([cx + 4 * math.cos(a) - 3, cy + 4 * math.sin(a) - 3,
                               cx + 4 * math.cos(a) + 3, cy + 4 * math.sin(a) + 3], c)
            d.text((cx, y0 + 23), label, font=self.font, fill=(170, 176, 190), anchor="mm")
        d.rounded_rectangle([272, y0 + 7, 314, y0 + 24], 8, fill=(210, 50, 50))
        d.text((293, y0 + 15), "离开", font=self.font, fill=WHITE, anchor="mm")

    def render(self, office, now):
        img = Image.new("RGB", (W, H), (14, 15, 20))
        d = ImageDraw.Draw(img)
        # 顶栏：录制中 + 会议名 + 状态
        d.rectangle([0, 0, W, 16], self.BAR)
        if int(now * 2) % 2:
            d.ellipse([5, 4, 13, 12], RED)
        d.text((17, 8), self.fit(office.group, 150), font=self.font, fill=WHITE, anchor="lm")
        text, col = self.status_text(office, now)
        d.text((W - 6, 8), text, font=self.font, fill=col, anchor="rm")
        n = len(office.bots)
        poses = [pose_of(office, i, now) for i in range(n)]
        tr = office.tour_at(now)
        for i, bot in enumerate(office.bots):
            self.bot_tile(img, d, i, bot, i, poses[i], now, office)
        me = n
        self.me_tile(img, d, me, office, now, tr)
        if n + 1 < 8:
            self.info_tile(d, n + 1, office, now)
        for k in range(n + 2, 8):
            x, y = self.tile_xy(k)
            d.rectangle([x, y, x + self.TW - 1, y + self.TH - 1], (22, 24, 30), outline=(40, 42, 52))
            d.text((x + self.TW / 2, y + self.TH / 2), "等待加入…", font=self.font, fill=(80, 84, 100), anchor="mm")
        # 实时字幕（就是黑板）
        d.rectangle([0, 168, W, 208], (6, 6, 9))
        d.rounded_rectangle([4, 171, 30, 185], 3, fill=(50, 54, 70))
        d.text((17, 178), "字幕", font=self.font, fill=(190, 196, 210), anchor="mm")
        self.board(d, office, now, (36, 170, W - 6, 206), chalk=WHITE)
        self.toolbar(d, now)
        # 交接：文件在两格之间飞
        for h in office.handoffs:
            if h.src >= n or h.dst >= n:
                continue
            p, carrying, arrived = handoff_phase(h, now)
            (sx, sy), (tx, ty) = self.tile_center(h.src), self.tile_center(h.dst)
            if carrying:
                px, py = sx + (tx - sx) * p, sy + (ty - sy) * p - math.sin(p * math.pi) * 26
                for q in range(1, 6):                                     # 虚线轨迹
                    qq = p * q / 6
                    d.point([(sx + (tx - sx) * qq, sy + (ty - sy) * qq - math.sin(qq * math.pi) * 26)], WHITE)
                if office.remote_label(office.bots[h.src].name):
                    self.package(d, px, py, now)
                else:
                    file_icon(d, px, py)
            if arrived:
                x, y = self.tile_xy(h.dst)
                d.rounded_rectangle([x + 3, y + 3, x + 50, y + 17], 4, fill=GREEN)
                d.text((x + 26, y + 10), "收到文件", font=self.font, fill=INK, anchor="mm")
        for i, bot in enumerate(office.bots):                             # 回复点名的私聊：格子上冒对话框
            if bot.say_inplace and talking(bot, now):
                x, y = self.tile_xy(i)
                self.bubble(d, (x + self.TW // 2, y + 24), bot.say_text, side=1 if x < 160 else -1, max_w=140,
                            bounds=(2, 18, W - 2, 206))
        # 悄悄话：虚线信封飘到我的格子，旁边虚线气泡
        mx, my = self.tile_center(me)
        for i, bot in enumerate(office.bots):
            if poses[i] != "whisper":
                continue
            p, speaking = whisper_phase(bot, now)
            sx, sy = self.tile_center(i)
            ex, ey = sx + (mx - sx) * p, sy + (my - sy) * p
            for q in range(0, 10):
                qq = q / 10 * p
                if q % 2 == 0:
                    d.point([(sx + (mx - sx) * qq, sy + (my - sy) * qq)], (180, 140, 255))
            d.rectangle([ex - 6, ey - 4, ex + 6, ey + 4], (230, 220, 255), outline=(120, 90, 200))
            d.line([(ex - 6, ey - 4), (ex, ey + 1), (ex + 6, ey - 4)], (120, 90, 200))
            if speaking:                                                  # 字幕栏上盖一条私聊横幅
                d.rectangle([2, 169, W - 3, 207], (34, 24, 52))
                self._dashed_box(d, 2, 169, W - 3, 207, (180, 140, 255))
                d.rounded_rectangle([6, 172, 64, 186], 3, fill=(120, 90, 200))
                d.text((35, 179), "私聊 → 我", font=self.font, fill=WHITE, anchor="mm")
                d.text((70, 179), self.fit(bot.name, 60), font=self.font, fill=bot_pal(bot)["light"], anchor="lm")
                d.text((8, 199), self.fit("嘘…" + bot.say_text, W - 20), font=self.font, fill=(230, 220, 255), anchor="lm")
        # 发大饼：饼从我的格子飞进对方格子
        if tr and tr["stage"] == "give":
            hx, hy = self.tile_center(tr["bot"])
            x, y = self.tile_xy(me)
            self.tour_overlay(d, now, tr, (x + 20, y + 40), (hx, hy - 10), bounds=(2, 18, W - 2, 206))
        return self.finish(img, office, now)


# ------------------------------------------------------------------ F 加班写字楼
class Tower(Theme):
    key, title = "tower", "F 加班写字楼"
    GAUGE_ON_BOARD = False
    FLOOR_Y = (58, 114, 170)
    FLOOR_H = 54
    ROOM_X = (12, 174)
    ROOM_W = 134
    SHAFT = (148, 172)
    PENT = (232, 8, 312, 56)

    def room(self, k):
        return self.ROOM_X[k % 2], self.FLOOR_Y[k // 2]

    def hit_boxes(self, office):
        return [(x, y, x + self.ROOM_W, y + self.FLOOR_H) for x, y in (self.room(i) for i in range(len(office.bots)))]

    def boss_box(self, office):
        return self.PENT

    def background(self, d, office, now):
        for y in range(0, 60, 12):                                        # 夜空，分色带
            d.rectangle([0, y, W, y + 11], lerp((10, 12, 34), (34, 30, 70), y / 60))
        for k in range(26):
            sx, sy = (k * 73) % W, (k * 29) % 54
            c = int(110 + 120 * (math.sin(now * 2 + k) + 1) / 2)
            d.point([(sx, sy)], (c, c, min(255, c + 30)))
        d.rectangle([0, 56, W, 226], (44, 46, 66))                        # 楼体
        d.rectangle([6, 54, W - 6, 57], (70, 74, 100))
        for y in (112, 168, 224):                                         # 楼板
            d.rectangle([6, y, W - 6, y + 2], (70, 74, 100))
        d.rectangle([0, 226, W, H], (26, 26, 32))                         # 马路
        for x in range(-20 + int(now * 30) % 40, W, 40):
            d.rectangle([x, 233, x + 18, 234], (200, 190, 120))
        cx = (now * 46) % (W + 60) - 30                                   # 路过的车
        d.rounded_rectangle([cx, 229, cx + 28, 237], 3, fill=(200, 70, 60))
        d.rectangle([cx + 6, 226, cx + 20, 230], (200, 70, 60))
        d.rectangle([cx + 26, 231, cx + 29, 233], YELLOW)

    def gauges_tanks(self, d, office, now):
        """楼顶三个水箱：水位 = 额度用量。"""
        for k, (key, tag) in enumerate((("grok", "G"), ("cursor", "C"), ("other", "O"))):
            x = 8 + k * 18
            pct = office.usage_pct(key)
            d.line([(x + 2, 46), (x + 2, 54)], (90, 94, 120))
            d.line([(x + 12, 46), (x + 12, 54)], (90, 94, 120))
            d.rounded_rectangle([x, 16, x + 14, 46], 3, fill=(30, 32, 46), outline=(120, 126, 160))
            if pct is not None:
                col = self.usage_color(pct)
                if pct >= 95 and int(now * 3) % 2:
                    col = WHITE
                hh = int(28 * min(pct, 100) / 100)
                d.rectangle([x + 1, 45 - hh, x + 13, 45], col)
                d.text((x + 7, 8), f"{pct:.0f}", font=self.font, fill=col, anchor="mm")
            d.text((x + 7, 30), tag, font=self.font, fill=WHITE, anchor="mm")

    def billboard(self, d, office, now):
        x0, y0, x1, y1 = 62, 2, 226, 53
        d.line([(x0 + 30, y1), (x0 + 30, 56)], (90, 94, 120), width=2)
        d.line([(x1 - 30, y1), (x1 - 30, 56)], (90, 94, 120), width=2)
        d.rectangle([x0, y0, x1, y1], (16, 10, 26), outline=(255, 90, 170), width=2)
        for k, bx in enumerate(range(x0 + 4, x1 - 2, 8)):                 # 边框跑马灯
            on = (k + int(now * 6)) % 4 == 0
            d.point([(bx, y0), (bx, y1)], WHITE if on else (255, 90, 170))
        self.board(d, office, now, (x0 + 5, y0 + 3, x1 - 5, y1 - 2), chalk=(255, 220, 240), title=office.group)

    def penthouse(self, img, d, office, now, away):
        x0, y0, x1, y1 = self.PENT
        text, col = self.status_text(office, now)
        d.rectangle([x0, y0, x1, y0 + 14], (20, 20, 30), outline=col)
        blink = col == RED and int(now * 2) % 2 == 0
        d.text(((x0 + x1) / 2, y0 + 7), text, font=self.font, fill=WHITE if blink else col, anchor="mm")
        d.rectangle([x0 + 4, y0 + 16, x1 - 4, y1], (60, 56, 70))
        d.rectangle([x0 + 8, y0 + 19, x1 - 26, y1], (240, 210, 140))      # 老板窗户，灯一直亮着
        if not away:
            paste_clip(img, spr_boss(), x0 + 14, y0 + 20, (x0 + 8, y0 + 19, x1 - 26, y1))
        d.text((x1 - 15, y0 + 30), "我", font=self.font, fill=(240, 210, 140), anchor="mm")

    def room_draw(self, img, d, k, bot, i, pose, now, office):
        x, y = self.room(k)
        P = bot_pal(bot)
        rw, rh = self.ROOM_W, self.FLOOR_H
        lit = pose not in ("sleep", "away")
        bright = pose in ("type", "panic", "jump", "whisper") or talking(bot, now)
        wall = (236, 206, 140) if bright else (150, 140, 120) if lit else (22, 26, 46)
        d.rectangle([x, y, x + rw, y + rh], wall)
        if lit:                                                           # 墙上挂钟 + 海报
            d.ellipse([x + 8, y + 6, x + 20, y + 18], WHITE, outline=INK)
            a = now * 0.5 + i
            d.line([(x + 14, y + 12), (x + 14 + 4 * math.cos(a), y + 12 + 4 * math.sin(a))], INK)
            if pose != "wait":
                d.rectangle([x + rw - 22, y + 6, x + rw - 8, y + 24], P["light"], outline=P["dark"])
        remote = office.remote_label(bot.name)
        cloud_work = pose == "cloud" or (bool(remote) and pose in ("type", "panic"))
        if pose != "away":
            sp = sprite_pose("idle" if cloud_work else pose, now, i)
            if pose == "whisper":
                sp = "talk" if int(now * 6) % 2 else "idle"
            f = int(now * (16 if pose == "panic" else 10)) % 4
            dy = self.jump_dy(office, now, 12, i) if pose == "jump" else 0
            jx, jy = self.jitter(now, i, 2) if pose == "panic" else (0, 0)
            s = spr_front(species(i), P["base"], sp, f if sp in ("type", "panic") else 0)
            paste_clip(img, s, x + rw // 2 - 44 + jx, y + rh - 64 + dy + jy, (x, y, x + rw, y + rh))
            if pose == "whisper":                                         # 打电话给老板
                hx, hy = x + rw // 2 + 14, y + 20
                d.rounded_rectangle([hx - 2, hy - 8, hx + 3, hy + 8], 2, fill=INK)
        d.rectangle([x + 20, y + rh - 16, x + rw - 20, y + rh - 14], (120, 84, 50))   # 桌子
        d.rectangle([x + 22, y + rh - 14, x + rw - 22, y + rh], (150, 106, 66))
        mon = P["glow"] if pose in ("type", "panic", "jump") else (70, 110, 200) if pose == "sleep" else (90, 94, 110)
        d.rectangle([x + rw // 2 - 12, y + rh - 26, x + rw // 2 + 11, y + rh - 16], (40, 42, 52))
        d.rectangle([x + rw // 2 - 10, y + rh - 24, x + rw // 2 + 9, y + rh - 18], mon)
        if pose == "sleep":                                               # 关灯后显示器的光照到脸上
            d.polygon([(x + rw // 2 - 10, y + rh - 24), (x + rw // 2 + 9, y + rh - 24),
                       (x + rw // 2 + 20, y + rh - 46), (x + rw // 2 - 21, y + rh - 46)], outline=(40, 60, 110))
        d.text((x + rw // 2, y + rh - 7), self.fit(bot.name, 80), font=self.font, fill=(255, 240, 210), anchor="mm")
        d.rectangle([x + 26, y + rh - 11, x + 29, y + rh - 4], P["fur"])
        if remote:
            self.remote_bar(d, office, bot, x + 34, y + rh - 3, rw - 68)
        # 窗框 + 玻璃反光
        d.rectangle([x, y, x + rw, y + rh], outline=(96, 100, 130), width=2)
        d.line([(x + 6, y + 3), (x + 18, y + 3)], (255, 255, 255) if lit else (60, 70, 100))
        d.line([(x + 3, y + 6), (x + 3, y + 14)], (255, 255, 255) if lit else (60, 70, 100))
        if pose in ("type", "panic"):
            self.type_fx(d, x + 20, y + 26, now, i, P["deep"] if bright else P["light"], spread=10, rise=18)
            self.type_fx(d, x + rw - 22, y + 26, now, i, P["deep"] if bright else P["light"], spread=10, rise=18)
        if pose == "panic":
            self.panic_fx(d, x + rw // 2, y + 10, now, i, w=26)
        if pose == "sleep":
            self.zzz(d, x + rw // 2 + 20, y + 14, now, i, (150, 170, 230))
        if pose == "wait":                                                # 窗上贴了一张黄便签
            d.rectangle([x + rw - 30, y + 4, x + rw - 8, y + 24], YELLOW)
            d.text((x + rw - 19, y + 14), "!", font=self.font, fill=INK, anchor="mm")
        if cloud_work:
            self.cloud(d, x + rw - 30, y + 14, now, i)
            st = office.remote_state.get(bot.name)
            if st and st["state"] == "working":
                d.text((x + 6, y + 4), fmt_dur(now - st["since"]), font=self.font, fill=(40, 90, 200))

    def shaft(self, img, d, office, now, cab_y, rider=None):
        sx0, sx1 = self.SHAFT
        d.rectangle([sx0, 58, sx1, 224], (20, 22, 34))
        d.line([((sx0 + sx1) / 2, 58), ((sx0 + sx1) / 2, cab_y)], (90, 94, 120))
        for fy in self.FLOOR_Y:                                           # 每层的楼层灯
            on = abs(cab_y - (fy + 6)) < 4
            d.ellipse([sx0 + 9, fy - 1, sx0 + 15, fy + 5], YELLOW if on else (60, 60, 70))
        d.rectangle([sx0 + 1, cab_y, sx1 - 1, cab_y + 46], (120, 124, 150), outline=(180, 184, 210))
        d.rectangle([sx0 + 3, cab_y + 3, sx1 - 3, cab_y + 44], (230, 220, 180))
        if rider:
            img.paste(rider, (int((sx0 + sx1) / 2 - rider.width / 2), int(cab_y + 46 - rider.height)), rider)

    def floor_cab_y(self, f):
        return self.FLOOR_Y[f] + 6

    def render(self, office, now):
        img = Image.new("RGB", (W, H), INK)
        d = ImageDraw.Draw(img)
        self.background(d, office, now)
        self.gauges_tanks(d, office, now)
        self.billboard(d, office, now)
        n = len(office.bots)
        poses = [pose_of(office, i, now) for i in range(n)]
        tr = office.tour_at(now)
        self.penthouse(img, d, office, now, away=bool(tr))
        for i, bot in enumerate(office.bots):
            self.room_draw(img, d, i, bot, i, poses[i], now, office)
        for k in range(n, 6):                                             # 没人的房间：黑着灯
            x, y = self.room(k)
            d.rectangle([x, y, x + self.ROOM_W, y + self.FLOOR_H], (18, 22, 40), outline=(96, 100, 130), width=2)
            d.line([(x + 6, y + 3), (x + 18, y + 3)], (60, 70, 100))
            d.line([(x + 3, y + 6), (x + 3, y + 14)], (60, 70, 100))
        cab_y, rider, over = self.floor_cab_y(2), None, []
        mid = (self.SHAFT[0] + self.SHAFT[1]) / 2
        for h in office.handoffs:                                         # 交接：走进电梯，坐到对方楼层，再走出来
            if h.src >= n or h.dst >= n:
                continue
            p, carrying, arrived = handoff_phase(h, now)
            bot = office.bots[h.src]
            fs, fd = h.src // 2, h.dst // 2
            (rx0, ry0), (rx1, ry1) = self.room(h.src), self.room(h.dst)
            w = small_walker(species(h.src), bot_pal(bot)["base"], int(now * 7) % 2, carrying)
            if p < 0.3:
                q = p / 0.3
                ax = rx0 + self.ROOM_W / 2
                px, fy = ax + (mid - ax) * q, fs
                cab_y = self.floor_cab_y(fs)
                img.paste(w, (int(px - 13), int(self.FLOOR_Y[fy] + self.FLOOR_H - 34)), w)
            elif p < 0.7:
                q = ease((p - 0.3) / 0.4)
                cab_y = self.floor_cab_y(fs) + (self.floor_cab_y(fd) - self.floor_cab_y(fs)) * q
                rider = w
            else:
                q = (p - 0.7) / 0.3
                bx = rx1 + self.ROOM_W / 2
                px = mid + (bx - mid) * q
                cab_y = self.floor_cab_y(fd)
                img.paste(w, (int(px - 13), int(self.FLOOR_Y[fd] + self.FLOOR_H - 34)), w)
            if arrived:
                self.mark(d, rx1 + 16, ry1 + 30, "!", RED, WHITE)
                file_icon(d, rx1 + self.ROOM_W - 30, ry1 + self.FLOOR_H - 20)
        bx, by = self.PENT[0] + 30, self.PENT[1] + 40
        for i, bot in enumerate(office.bots):                             # 悄悄话 = 给楼顶老板打电话
            x, y = self.room(i)
            t = talking(bot, now)
            if t and poses[i] not in ("away", "whisper"):
                over.append(((x + self.ROOM_W // 2 + 10, y + 16), t, False, 1 if x < 160 else -1))
            if poses[i] == "whisper":
                p, speaking = whisper_phase(bot, now)
                hx, hy = x + self.ROOM_W // 2 + 16, y + 12
                for q in range(0, 20):
                    if q % 2 == 0 and q / 20 < p:
                        t0 = q / 20
                        zx = hx + (bx - hx) * t0 + math.sin(t0 * 30) * 3
                        d.point([(zx, hy + (by - hy) * t0)], (200, 170, 255))
                if speaking:
                    over.append(((hx + 2, hy + 4), bot.say_text, True, 1 if x < 160 else -1))
        if tr:                                                            # 发大饼：老板坐电梯一层层下来
            fb = tr["bot"] // 2
            if tr["stage"] == "back":
                cab_y = self.floor_cab_y(fb) + (8 - self.floor_cab_y(fb)) * tr["p"]
            elif tr["stage"] == "walk":
                prev = self.floor_cab_y(office.enc_order[tr["k"] - 1] // 2) if tr["k"] else 8
                cab_y = prev + (self.floor_cab_y(fb) - prev) * tr["p"]
            else:
                cab_y = self.floor_cab_y(fb)
            rider = small_boss()
        self.shaft(img, d, office, now, cab_y, rider)
        for anchor, text, private, side in over:
            self.bubble(d, anchor, text, private=private, side=side, bounds=(2, 58, W - 2, 224))
        if tr and tr["stage"] == "give":
            rx, ry = self.room(tr["bot"])
            self.tour_overlay(d, now, tr, (mid - 8, cab_y + 30), (rx + self.ROOM_W // 2, ry + 14),
                              bounds=(2, 58, W - 2, 224))
        return self.finish(img, office, now)


# ------------------------------------------------------------------ G 牛马摩天轮
class Wheel(Theme):
    key, title = "wheel", "G 牛马摩天轮"
    GAUGE_ON_BOARD = False
    C = (116, 104)
    R = 72
    BOOTH = (232, 176, 316, 238)

    def __init__(self, font_path=None):
        super().__init__(font_path)
        self._rot, self._last = -math.pi / 2, None

    def spin(self, office, now, poses):
        """转速 = 在干活的人数；卡住就停；挨鞭子时猛转。发饼时把对方轿厢转到最下面。"""
        dt = 0 if self._last is None else max(0.0, min(0.5, now - self._last))
        self._last = now
        tw = now - office.whip_t0
        work = sum(p in ("type", "panic", "jump", "cloud") for p in poses)
        speed = 0 if office.stalled(now) else 0.05 + 0.06 * work
        if 0.45 <= tw < 2.5:
            speed = 2.5
        tr = office.tour_at(now)
        n = max(1, len(office.bots))
        if tr and tr["stage"] != "back":
            want = math.pi / 2 - tr["bot"] * 2 * math.pi / n
            diff = (want - self._rot + math.pi) % (2 * math.pi) - math.pi
            self._rot += diff * min(1.0, dt * 6 if self._last else 1.0)
        else:
            self._rot += speed * dt
        return speed

    def pivot(self, k, n):
        a = self._rot + k * 2 * math.pi / n
        return self.C[0] + self.R * math.cos(a), self.C[1] + self.R * math.sin(a)

    def hit_boxes(self, office):
        n = max(1, len(office.bots))
        return [(px - 28, py, px + 28, py + 52) for px, py in (self.pivot(i, n) for i in range(len(office.bots)))]

    def boss_box(self, office):
        return self.BOOTH

    def background(self, d, office, now, running):
        for y in range(0, H, 20):                                         # 黄昏，分色带（整片渐变压不动，USB 吃不消）
            d.rectangle([0, y, W, y + 19], lerp((60, 50, 110), (250, 150, 110), y / H))
        d.ellipse([170, 150, 230, 210], (255, 200, 120))
        d.rectangle([0, 222, W, H], (70, 120, 70))
        for x in range(0, W, 7):
            d.line([(x, 222), (x + 2, 218)], (60, 104, 60))
        for k, (bx, col) in enumerate(((20, RED), (208, YELLOW))):        # 飘走的气球
            by = 200 - ((now * 9 + k * 70) % 220)
            d.line([(bx + 4, by + 10), (bx + 2 + math.sin(now + k) * 2, by + 22)], WHITE)
            d.ellipse([bx, by, bx + 8, by + 10], col)
        cx, cy = self.C
        d.line([(cx, cy), (cx - 46, 222)], (230, 230, 240), width=4)       # 支架
        d.line([(cx, cy), (cx + 46, 222)], (230, 230, 240), width=4)
        d.line([(cx - 30, 180), (cx + 30, 180)], (230, 230, 240), width=2)
        R = self.R
        d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(240, 240, 250), width=3)
        d.ellipse([cx - R + 8, cy - R + 8, cx + R - 8, cy + R - 8], outline=(200, 200, 220), width=1)
        for k in range(24):                                               # 轮圈彩灯：转起来就追着亮
            a = self._rot + k * math.pi / 12
            lx, ly = cx + R * math.cos(a), cy + R * math.sin(a)
            on = running and (k + int(now * 8)) % 3 == 0
            col = (255, 240, 140) if on else (120, 110, 120) if not running else (240, 120, 180)
            d.ellipse([lx - 2, ly - 2, lx + 2, ly + 2], col)

    def cabin(self, img, d, k, n, bot, i, pose, now, office):
        px, py = self.pivot(k, n)
        P = bot_pal(bot)
        swing = math.sin(now * 2 + i) * 2 if pose != "sleep" else 0
        x0, y0 = int(px - 26 + swing), int(py + 6)
        d.line([(px, py), (px + swing, y0)], (220, 220, 230), width=2)
        d.chord([x0 + 6, y0 - 6, x0 + 46, y0 + 8], 180, 360, P["dark"])  # 圆顶
        d.rounded_rectangle([x0, y0, x0 + 52, y0 + 44], 6, fill=P["fur"], outline=P["deep"])
        lit = pose not in ("sleep", "away")
        win = (x0 + 5, y0 + 4, x0 + 47, y0 + 30)
        d.rectangle(win, (250, 236, 190) if lit else (24, 28, 50))
        remote = office.remote_label(bot.name)
        cloud_work = pose == "cloud" or (bool(remote) and pose in ("type", "panic"))
        if pose != "away":
            sp = sprite_pose("idle" if cloud_work else pose, now, i)
            if pose == "whisper":
                sp = "talk" if int(now * 6) % 2 else "idle"
            dy = self.jump_dy(office, now, 6, i) if pose == "jump" else 0
            jx, jy = self.jitter(now, i, 1) if pose == "panic" else (0, 0)
            f = int(now * 10) % 4
            s = spr_front(species(i), P["base"], sp, f if sp in ("type", "panic") else 0)
            paste_clip(img, s, x0 + 26 - 44 + jx, y0 + 4 - 6 + dy + jy, win)
        d.rectangle(win, outline=P["deep"])
        d.text((x0 + 26, y0 + 37), self.fit(bot.name, 46), font=self.font, fill=WHITE, anchor="mm")
        if remote:
            self.remote_bar(d, office, bot, x0 + 6, y0 + 45, 40)
        cx, cy = x0 + 26, y0 + 10
        if pose == "sleep":
            self.zzz(d, x0 + 40, y0 + 6, now, i, (200, 210, 255))
        elif pose == "wait":
            self.mark(d, x0 + 50, y0 - 2, "!", YELLOW)
        elif pose in ("type", "panic") and not cloud_work:
            for q in range(6):                                            # 窗口往外冒代码
                ph = (now * 2 + q / 6 + i * 0.3) % 1.0
                side = -1 if q % 2 else 1
                d.rectangle([cx + side * (28 + ph * 10), cy + 4 - ph * 14, cx + side * (28 + ph * 10) + 3, cy + 5 - ph * 14],
                            CODE[(q + i) % 4])
            if pose == "panic":
                self.panic_fx(d, cx, cy, now, i, w=28)
        if cloud_work:
            self.cloud(d, x0 + 46, y0 - 4, now, i)
            st = office.remote_state.get(bot.name)
            if st and st["state"] == "working":
                d.text((x0 + 26, y0 + 52), fmt_dur(now - st["since"]), font=self.font, fill=(30, 80, 190), anchor="mm")
        return (cx, cy)

    def side_panel(self, img, d, office, now, away):
        text, col = self.status_text(office, now)
        x0, y0, x1, y1 = 230, 2, 318, 34                                  # 游乐园招牌
        d.rounded_rectangle([x0, y0, x1, y1], 6, fill=(150, 30, 60), outline=YELLOW, width=2)
        for k, bx in enumerate(range(x0 + 6, x1 - 2, 8)):
            d.point([(bx, y0 + 1), (bx, y1 - 1)], WHITE if (k + int(now * 6)) % 3 == 0 else YELLOW)
        d.text(((x0 + x1) / 2, y0 + 11), self.fit(office.group, 82), font=self.font, fill=WHITE, anchor="mm")
        blink = col == RED and int(now * 2) % 2 == 0
        d.text(((x0 + x1) / 2, y0 + 24), text, font=self.font, fill=WHITE if blink else col, anchor="mm")
        bx0, by0, bx1, by1 = 232, 38, 316, 124                            # 广播告示牌

        d.rectangle([bx0, by0, bx1, by1], (40, 70, 56), outline=(150, 104, 60), width=3)
        self.board(d, office, now, (bx0 + 5, by0 + 4, bx1 - 4, by1 - 4), title="广播")
        d.rounded_rectangle([bx0, 128, bx1, 172], 4, fill=(30, 30, 44), outline=(150, 104, 60), width=2)   # 额度：电量牌
        self.gauge_strip(d, office, now, (bx0 + 5, 129, bx1 - 4, 172), WHITE)
        x0, y0, x1, y1 = self.BOOTH                                       # 售票亭
        d.rectangle([x0 + 4, y0 + 14, x1 - 4, y1 - 16], (250, 240, 220))
        for k in range(6):
            d.polygon([(x0 + k * 14, y0), (x0 + k * 14 + 14, y0), (x0 + k * 14 + 14, y0 + 14), (x0 + k * 14, y0 + 14)],
                      RED if k % 2 else WHITE)
        win = (x0 + 12, y0 + 18, x1 - 12, y1 - 22)
        d.rectangle(win, (90, 130, 170))
        if not away:
            paste_clip(img, spr_boss(), (x0 + x1) // 2 - 22, y0 + 18, win)
        d.rectangle([x0 + 8, y1 - 22, x1 - 8, y1 - 16], (150, 104, 60))
        d.text(((x0 + x1) / 2, y1 - 8), "售票 · 我", font=self.font, fill=INK, anchor="mm")

    def render(self, office, now):
        img = Image.new("RGB", (W, H), INK)
        d = ImageDraw.Draw(img)
        n = len(office.bots)
        poses = [pose_of(office, i, now) for i in range(n)]
        speed = self.spin(office, now, poses)
        self.background(d, office, now, running=speed > 0)
        cx, cy = self.C
        for k in range(n):
            px, py = self.pivot(k, n)
            d.line([(cx, cy), (px, py)], (230, 230, 240), width=2)
        d.ellipse([cx - 12, cy - 12, cx + 12, cy + 12], (250, 220, 120), outline=(160, 110, 40), width=2)
        d.text((cx, cy), "牛", font=self.font, fill=(140, 70, 20), anchor="mm")
        tr = office.tour_at(now)
        self.side_panel(img, d, office, now, away=bool(tr))
        heads, over = [], []
        for i, bot in enumerate(office.bots):
            heads.append(self.cabin(img, d, i, n, bot, i, poses[i], now, office))
        for i, bot in enumerate(office.bots):
            hx, hy = heads[i]
            t = talking(bot, now)
            side = 1 if hx < cx else -1
            if t and poses[i] not in ("away", "whisper"):
                over.append(((hx + side * 22, hy - 8), t, False, side))
            if poses[i] == "whisper":                                     # 悄悄话：对讲机虚线连到售票亭
                p, speaking = whisper_phase(bot, now)
                tx, ty = self.BOOTH[0] + 20, self.BOOTH[1] + 40
                for q in range(0, 24, 2):
                    t0 = q / 24
                    if t0 < p:
                        d.point([(hx + (tx - hx) * t0, hy + (ty - hy) * t0)], (230, 200, 255))
                if speaking:
                    over.append(((hx + side * 22, hy - 8), bot.say_text, True, side))
        for h in office.handoffs:                                         # 交接：文件顺着辐条滑到中心再滑过去
            if h.src >= n or h.dst >= n:
                continue
            p, carrying, arrived = handoff_phase(h, now)
            (sx, sy), (tx, ty) = self.pivot(h.src, n), self.pivot(h.dst, n)
            if carrying:
                if p < 0.5:
                    q = p / 0.5
                    px, py = sx + (cx - sx) * q, sy + (cy - sy) * q
                else:
                    q = (p - 0.5) / 0.5
                    px, py = cx + (tx - cx) * q, cy + (ty - cy) * q
                if office.remote_label(office.bots[h.src].name):
                    self.package(d, px, py - 6, now)
                else:
                    d.line([(px, py - 12), (px, py - 4)], (60, 60, 70))
                    file_icon(d, px, py)
            if arrived:
                hx, hy = heads[h.dst]
                self.mark(d, hx - 22, hy - 14, "!", RED, WHITE)
        for anchor, text, private, side in over:
            self.bubble(d, anchor, text, private=private, side=side, max_w=100, bounds=(2, 2, 228, H - 2))
        if tr:                                                            # 发大饼：老板走到站台，轿厢转下来一个发一个
            home = (274, 236)
            stop = (cx + 40, 236)
            if tr["stage"] == "walk" and tr["k"] == 0:
                pos = (home[0] + (stop[0] - home[0]) * tr["p"], home[1] + (stop[1] - home[1]) * tr["p"])
            elif tr["stage"] == "back":
                pos = (stop[0] + (home[0] - stop[0]) * tr["p"], stop[1] + (home[1] - stop[1]) * tr["p"])
            else:
                pos = stop
            b = small_boss()
            img.paste(b, (int(pos[0] - 11), int(pos[1] - 30)), b)
            if tr["stage"] == "give":
                hx, hy = heads[tr["bot"]]
                self.tour_overlay(d, now, tr, (pos[0] - 14, pos[1] + 2), (hx, hy), bounds=(2, 2, 228, H - 2))
                for k in range(3 if tr["kind"] == "pie" else 0):          # 发饼时放烟花
                    t = (now * 0.9 + k * 0.33) % 1.0
                    fx, fy = 30 + k * 80, 50 - k * 10
                    for a in range(10):
                        ang = a * math.pi / 5
                        r = 4 + t * 14
                        d.point([(fx + r * math.cos(ang), fy + r * math.sin(ang))], (YELLOW, WHITE, (255, 120, 200))[k])
        return self.finish(img, office, now, hit=(116, 112))


# ------------------------------------------------------------------ H KPI 赛马场
def spr_runner(sp, color, pose, f):
    """侧面奔跑的牛 / 马，面朝右。22x15 逻辑像素，放大 2 倍。"""
    P = palette(color)
    im = Image.new("RGBA", (24, 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    fur, dark, deep, muz = P["fur"], P["dark"], P["deep"], P["muzzle"]
    lying = pose == "sleep"
    by = 9 if lying else 6
    run = pose in ("type", "panic")
    # 腿
    if lying:
        d.line([(7, by + 5), (11, by + 5)], deep)
        d.line([(13, by + 5), (17, by + 5)], deep)
    else:
        if run:
            ph = f % 2
            legs = ((6, -2 if ph else 2), (8, 2 if ph else -2), (14, -2 if ph else 2), (16, 2 if ph else -2))
        elif pose == "jump":
            legs = ((6, -2), (8, -1), (14, 1), (16, 2))
        else:
            legs = ((6, 0), (8, 0), (14, 0), (16, 0))
        for lx, sw in legs:
            d.line([(lx, by + 4), (lx + sw, by + 8)], deep)
            d.point([(lx + sw, by + 8)], INK)
    d.rectangle([5, by, 17, by + 4], fur)                                 # 身体
    d.rectangle([7, by + 3, 15, by + 4], P["light"])
    if sp == "ox":
        d.line([(5, by + 1), (2, by + 4)], dark)
        d.rectangle([1, by + 4, 2, by + 6], deep)
        hx, hy = 17, by - 1
        d.rectangle([hx, hy, hx + 4, hy + 4], fur)
        d.rectangle([hx + 3, hy + 2, hx + 5, hy + 5], muz)
        d.line([(hx + 1, hy - 1), (hx, hy - 3)], (246, 238, 212))
        d.line([(hx + 3, hy - 1), (hx + 4, hy - 3)], (246, 238, 212))
        ey = hy + 1
        ex = hx + 2
    else:
        d.line([(5, by + 1), (1, by + 3)], dark, width=2)
        d.rectangle([15, by - 4, 17, by + 1], fur)                        # 脖子
        d.rectangle([14, by - 4, 15, by], dark)                           # 鬃毛
        hx, hy = 16, by - 6
        d.rectangle([hx, hy, hx + 4, hy + 3], fur)
        d.rectangle([hx + 3, hy + 1, hx + 6, hy + 4], muz)
        d.point([(hx + 1, hy - 1), (hx + 1, hy - 2)], fur)
        ey = hy + 1
        ex = hx + 2
    if lying or pose == "blink":
        d.line([(ex, ey + 1), (ex + 1, ey + 1)], INK)
    else:
        d.point([(ex, ey)], INK)
    if pose == "wait":                                                    # 举小黄旗
        d.line([(12, by), (12, by - 9)], (120, 90, 60))
        d.rectangle([12, by - 9, 16, by - 6], YELLOW)
    return im.resize((48, 32), Image.NEAREST)


class Derby(Theme):
    key, title = "derby", "H KPI 赛马场"
    GAUGE_ON_BOARD = False
    TOP, BOTTOM = 20, 188
    START, FINISH = 46, 230

    def lanes(self, n):
        lh = min(42, (self.BOTTOM - self.TOP) // max(1, n))
        y0 = self.TOP + (self.BOTTOM - self.TOP - lh * n) // 2
        return y0, lh

    def hit_boxes(self, office):
        n = len(office.bots)
        y0, lh = self.lanes(n)
        return [(0, y0 + i * lh, self.FINISH, y0 + (i + 1) * lh) for i in range(n)]

    def boss_box(self, office):
        return (244, 24, 316, 98)

    @staticmethod
    def progress(office, i, now):
        """最近 30 分钟这个 bot 在群里说了几句话 + 正在干活，按全场最多的人归一化。"""
        counts = [0.0] * len(office.bots)
        for m in office.messages:
            k = office.index(m.sender)
            if k >= 0 and not m.private and now - m.ts < 1800:
                counts[k] += 1
        for k, b in enumerate(office.bots):
            if office.effective_status(b, now) == "typing":
                counts[k] += 1.5
        top = max(counts) or 1
        return 0.06 + 0.84 * counts[i] / top

    def background(self, d, office, now, n):
        d.rectangle([0, 0, W, self.TOP - 1], (18, 70, 44))
        d.text((8, 9), self.fit(office.group + " 杯", 170), font=self.font, fill=(255, 236, 160), anchor="lm")
        text, col = self.status_text(office, now)
        blink = col == RED and int(now * 2) % 2 == 0
        d.text((W - 6, 9), text, font=self.font, fill=WHITE if blink else col, anchor="rm")
        d.rectangle([0, self.TOP, 240, self.BOTTOM], (70, 140, 70))                       # 内场草地
        y0, lh = self.lanes(n)
        for k in range(n):
            ly = y0 + k * lh
            d.rectangle([0, ly, 240, ly + lh - 1], (184, 128, 80) if k % 2 else (176, 120, 74))
            for x in range(self.START + (k * 11) % 9, self.FINISH, 9):                      # 蹄印
                d.point([(x, ly + lh - 6 - (x % 3))], (150, 100, 60))
            d.line([(0, ly), (240, ly)], (240, 236, 220))
        d.line([(0, y0 + n * lh), (240, y0 + n * lh)], (240, 236, 220))
        for yy in range(self.TOP, self.BOTTOM, 4):                                        # 终点线
            for xx in range(self.FINISH, self.FINISH + 8, 4):
                d.rectangle([xx, yy, xx + 3, yy + 3], WHITE if ((xx - self.FINISH) // 4 + (yy - self.TOP) // 4) % 2 else INK)
        d.rectangle([240, self.TOP, W, self.BOTTOM], (36, 50, 66))                          # 看台
        for yy in range(self.TOP + 82, self.BOTTOM, 6):
            d.line([(240, yy), (W, yy)], (46, 62, 80))

    def gate(self, d, k, bot, ly, lh, rank):
        P = bot_pal(bot)
        d.rectangle([0, ly + 1, self.START - 2, ly + lh - 1], (24, 64, 40))
        d.rectangle([0, ly + 1, 3, ly + lh - 1], P["fur"])
        d.text((6, ly + lh / 2), self.fit(bot.name, 36), font=self.font, fill=WHITE, anchor="lm")
        d.line([(self.START - 2, ly), (self.START - 2, ly + lh)], (220, 220, 220))

    def render(self, office, now):
        img = Image.new("RGB", (W, H), INK)
        d = ImageDraw.Draw(img)
        n = len(office.bots)
        poses = [pose_of(office, i, now) for i in range(n)]
        self.background(d, office, now, n)
        y0, lh = self.lanes(n)
        prog = [self.progress(office, i, now) for i in range(n)]
        order = sorted(range(n), key=lambda i: -prog[i])
        rank = {i: r + 1 for r, i in enumerate(order)}
        tr = office.tour_at(now)
        # 解说台
        bx0, by0, bx1, by1 = 244, 24, 316, 98
        d.rectangle([bx0, by0, bx1, by1], (60, 70, 90), outline=(140, 150, 180))
        d.rectangle([bx0 + 4, by0 + 4, bx1 - 4, by1 - 20], (140, 190, 230))
        paste_clip(img, spr_boss(), (bx0 + bx1) // 2 - 22, by0 + 8, (bx0 + 4, by0 + 4, bx1 - 4, by1 - 20))
        d.line([((bx0 + bx1) / 2 + 12, by1 - 30), ((bx0 + bx1) / 2 + 8, by1 - 22)], INK, width=2)          # 话筒
        d.ellipse([(bx0 + bx1) / 2 + 9, by1 - 34, (bx0 + bx1) / 2 + 15, by1 - 28], (60, 60, 70))
        d.text(((bx0 + bx1) / 2, by1 - 10), "解说 · 我", font=self.font, fill=WHITE, anchor="mm")
        self.gauge_column(d, office, now, (bx0, 104, bx1, self.BOTTOM - 2))
        heads, over = [], []
        for i, bot in enumerate(office.bots):
            ly = y0 + i * lh
            self.gate(d, i, bot, ly, lh, rank[i])
            P = bot_pal(bot)
            pose = poses[i]
            remote = office.remote_label(bot.name)
            cloud_work = pose == "cloud" or (bool(remote) and pose in ("type", "panic"))
            x = self.START + (self.FINISH - 52 - self.START) * prog[i]
            run = pose in ("type", "panic") and not cloud_work
            if run:
                x += math.sin(now * 3 + i) * 3
            fy = ly + lh - 32 + 2
            dy = self.jump_dy(office, now, 12, i) if pose == "jump" else 0
            jx, jy = self.jitter(now, i, 1) if pose == "panic" else (0, 0)
            sp = "idle" if cloud_work else sprite_pose(pose, now, i)
            f = int(now * (16 if pose == "panic" else 9))
            if pose == "whisper":                                          # 悄悄话：掉头跑到看台边
                p, speaking = whisper_phase(bot, now)
                x = x + (self.FINISH - 46 - x) * p
            if cloud_work:
                self.cloud(d, int(x) + 24, fy + 30, now, i)
            if pose not in ("away",):
                s = spr_runner(species(i), P["base"], sp, f)
                img.paste(s, (int(x) + jx, fy + dy + jy - (8 if cloud_work else 0)), s)
            if run:                                                       # 扬起的尘土
                for q in range(4):
                    ph = (now * 3 + q / 4 + i * 0.2) % 1.0
                    d.ellipse([x - 2 - ph * 14, fy + 26 - ph * 6, x + 2 - ph * 14, fy + 30 - ph * 6], (214, 176, 130))
            if pose != "away":
                bx_, by_ = int(x) + 15 + jx, fy + dy + jy - (8 if cloud_work else 0) + (17 if pose == "sleep" else 11)
                d.rectangle([bx_, by_, bx_ + 11, by_ + 10], WHITE, outline=INK)                 # 号码布 = 名次
                d.text((bx_ + 6, by_ + 5), str(rank[i]), font=self.font, fill=INK, anchor="mm")
            hx, hy = int(x) + 40, fy + 6
            heads.append((hx, hy))
            if pose == "sleep":
                self.zzz(d, hx - 4, hy + 2, now, i, WHITE)
            if pose == "panic":
                self.panic_fx(d, hx - 16, hy + 6, now, i, w=20)
            if cloud_work:
                st = office.remote_state.get(bot.name)
                if st and st["state"] == "working":
                    d.text((int(x) + 50, fy + 12), fmt_dur(now - st["since"]), font=self.font, fill=(30, 70, 170))
            if remote:
                self.remote_bar(d, office, bot, 6, ly + lh - 5, 34)
            t = talking(bot, now)
            if t and pose != "whisper":
                over.append(((hx, hy), t, False, 1 if hx < 140 else -1))
            if pose == "whisper":
                p, speaking = whisper_phase(bot, now)
                if speaking:
                    over.append(((hx, hy), bot.say_text, True, 1))
        for h in office.handoffs:                                         # 交接 = 扔接力棒
            if h.src >= n or h.dst >= n:
                continue
            p, carrying, arrived = handoff_phase(h, now)
            (sx, sy), (tx, ty) = heads[h.src], heads[h.dst]
            if carrying:
                px, py = sx + (tx - sx) * p, sy + (ty - sy) * p - math.sin(p * math.pi) * 30
                if office.remote_label(office.bots[h.src].name):
                    self.package(d, px, py, now)
                else:
                    a = now * 14
                    ex, ey = 6 * math.cos(a), 6 * math.sin(a)
                    d.line([(px - ex, py - ey), (px + ex, py + ey)], bot_pal(office.bots[h.src])["light"], width=4)
                    d.line([(px - ex, py - ey), (px + ex, py + ey)], WHITE, width=1)
            if arrived:
                self.mark(d, tx + 6, ty - 10, "!", RED, WHITE)
        # 解说字幕（黑板）
        d.rectangle([0, self.BOTTOM + 1, W, H], (10, 12, 16))
        d.rectangle([0, self.BOTTOM + 1, W, self.BOTTOM + 2], YELLOW)
        self.board(d, office, now, (6, self.BOTTOM + 5, W - 6, H - 2), chalk=WHITE)
        for anchor, text, private, side in over:
            self.bubble(d, anchor, text, private=private, side=side, max_w=110, bounds=(2, self.TOP + 2, 238, self.BOTTOM))
        if tr:                                                            # 发大饼：老板从看台往赛道扔饼
            pos = (bx0 + 2, by0 + 50)
            if tr["stage"] == "give":
                self.tour_overlay(d, now, tr, pos, heads[tr["bot"]], bounds=(2, self.TOP + 2, W - 2, self.BOTTOM))
        return self.finish(img, office, now)


THEMES = {"blueprint": Blueprint, "neon": NeonRoom, "barn": Barn, "space": Space,
          "meeting": Meeting, "tower": Tower, "wheel": Wheel, "derby": Derby}
