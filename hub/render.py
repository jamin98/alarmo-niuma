"""聊天记录页、额度页，以及各主题共用的字体 / 时长格式。工位画面在 scenes.py。"""
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

W, H = 320, 240
TOP = 16

BAR = (26, 28, 38)
CHAT_BG = (18, 20, 28)
WHITE = (240, 240, 245)
GREY = (150, 155, 170)
RED = (235, 64, 52)
YELLOW = (250, 200, 60)
GREEN = (80, 210, 120)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]


def find_font(path=None):
    for p in ([path] if path else []) + FONT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    raise SystemExit("找不到中文字体，请执行: sudo apt install fonts-wqy-microhei")


def fmt_dur(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60:02d}:{sec % 60:02d}"


class Renderer:
    def __init__(self, font_path=None):
        p = find_font(font_path)
        self.font = ImageFont.truetype(p, 12)
        self.big = ImageFont.truetype(p, 26)

    # ---- 文字工具 ----
    def fit(self, text, width, font=None):
        font = font or self.font
        if font.getlength(text) <= width:
            return text
        while text and font.getlength(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def wrap(self, text, width, first_indent=0):
        lines, cur, cw = [], "", first_indent
        for ch in text:
            w = self.font.getlength(ch)
            if cw + w > width and cur:
                lines.append(cur)
                cur, cw = "", 0
            cur += ch
            cw += w
        if cur:
            lines.append(cur)
        return lines or [""]

    # ---- 布局 ----
    # ---- 主入口：只负责聊天记录页和额度页 ----
    def render(self, office, now):
        if office.view == "usage":
            return self._usage_page(office, now)
        return self._chat_page(office, now)

    def _top_bar(self, d, office, now, stalled):
        d.rectangle([0, 0, W, TOP - 1], BAR)
        sil = office.silence(now)
        if not office.bridge_online(now):
            right, col = "桥接离线", YELLOW
        elif stalled:
            right, col = "卡住 " + fmt_dur(sil), (RED if int(now * 2) % 2 else WHITE)
        elif office.anyone_typing(now):
            right, col = "干活中", GREEN
        else:
            right, col = "静默 " + fmt_dur(sil), GREY
        rw = self.font.getlength(right)
        d.text((W - 8 - rw, 1), right, font=self.font, fill=col)
        d.ellipse([W - 18 - rw, 5, W - 12 - rw, 11], col)
        d.text((8, 1), self.fit(office.group, W - rw - 40), font=self.font, fill=WHITE)

    def _toast(self, d, office, now):
        text, t0, kind = office.toast
        if not text or now - t0 > 3.0:
            return
        col = {"ok": GREEN, "warn": YELLOW, "err": RED}.get(kind, GREEN)
        tw = self.font.getlength(text)
        x0 = (W - tw) / 2 - 10
        d.rounded_rectangle([x0, 22, x0 + tw + 20, 44], 6, fill=(20, 22, 30), outline=col)
        d.text((W / 2, 33), text, font=self.font, fill=col, anchor="mm")

    @staticmethod
    def usage_color(pct):
        return GREEN if pct < 70 else YELLOW if pct < 90 else RED

    def _usage_page(self, office, now):
        """额度页：三项额度的大进度条 + Grok Bot 每周额度的重置倒计时。"""
        img = Image.new("RGB", (W, H), CHAT_BG)
        d = ImageDraw.Draw(img)
        u = office.usage
        self._top_bar(d, office, now, office.stalled(now))
        d.text((10, TOP + 6), "额度", font=self.font, fill=WHITE)
        plan = u.get("plan")
        if plan:
            tw = self.font.getlength(plan + " 套餐")
            d.text((W - 10 - tw, TOP + 6), plan + " 套餐", font=self.font, fill=GREY)
        if not u:
            d.text((W / 2, 120), "还没有额度数据", font=self.font, fill=GREY, anchor="mm")
            d.text((W / 2, 140), "桥接需要 secrets.toml 里的 Cursor Cookie", font=self.font, fill=GREY, anchor="mm")
        elif not u.get("ok"):
            d.text((W / 2, 120), self.fit(u.get("error") or "读取失败", W - 20), font=self.font, fill=RED, anchor="mm")
        else:
            y = TOP + 34
            for key, label in office.USAGE_LABELS:
                pct = office.usage_pct(key)
                d.text((10, y), label, font=self.font, fill=WHITE)
                if pct is None:
                    d.text((W - 10, y), "—", font=self.font, fill=GREY, anchor="ra")
                else:
                    col = self.usage_color(pct)
                    if pct >= 95 and int(now * 3) % 2:
                        col = WHITE
                    d.text((W - 10, y), f"{pct:.0f}%", font=self.font, fill=col, anchor="ra")
                    d.rounded_rectangle([10, y + 17, W - 10, y + 29], 4, fill=(44, 48, 62))
                    fill_w = max(2, int((W - 20) * min(pct, 100) / 100))
                    d.rounded_rectangle([10, y + 17, 10 + fill_w, y + 29], 4, fill=col)
                if key == "grok" and u["grok"].get("reset"):
                    left = u["grok"]["reset"] - now
                    days, hours = int(left // 86400), int(left % 86400 // 3600)
                    t = datetime.fromtimestamp(u["grok"]["reset"])
                    d.text((10, y + 32), f"{t.month}月{t.day}日重置 · " + (f"还剩 {days} 天 {hours} 小时" if left > 0 else "即将重置"),
                           font=self.font, fill=GREY)
                    y += 16
                y += 50
            if office.quota_blocked():
                d.text((W / 2, H - 22), "Grok Bot 额度已用完，到重置前会暂停", font=self.font, fill=RED, anchor="mm")
        age = int(now - u["ts"]) if u.get("ts") else None
        foot = "更新于 " + (f"{age // 60} 分钟前" if age is not None and age >= 60 else f"{age} 秒前") if age is not None else ""
        d.text((10, H - 16), foot + "    按大按钮返回", font=self.font, fill=(100, 106, 124))
        self._toast(d, office, now)
        return img

    def _chat_page(self, office, now):
        from scenes import bot_pal          # scenes 依赖本模块，这里延迟导入
        img = Image.new("RGB", (W, H), CHAT_BG)
        d = ImageDraw.Draw(img)
        rows = []
        for m in office.messages:
            idx = office.index(m.sender)
            col = bot_pal(office.bots[idx])["light"] if idx >= 0 else GREY
            head = self.fit(m.sender, 90) + ": "
            for k, ln in enumerate(self.wrap(m.text if office.show_chat else "…", W - 14, first_indent=self.font.getlength(head))):
                rows.append((head if k == 0 else None, col, ln))
        per_page = (H - TOP - 4) // 15
        max_scroll = max(0, len(rows) - per_page)
        office.chat_scroll = min(max(0, office.chat_scroll), max_scroll)
        end = len(rows) - office.chat_scroll
        y = TOP + 3
        for head, col, ln in rows[max(0, end - per_page):end]:
            x = 7
            if head:
                d.text((x, y), head, font=self.font, fill=col)
                x += self.font.getlength(head)
            d.text((x, y), ln, font=self.font, fill=WHITE)
            y += 15
        if not rows:
            d.text((8, TOP + 4), "还没有消息", font=self.font, fill=GREY)
        if max_scroll:
            bar_h = max(12, (H - TOP) * per_page // len(rows))
            pos = (H - TOP - bar_h) * (max_scroll - office.chat_scroll) // max_scroll
            d.rectangle([W - 3, TOP + pos, W - 1, TOP + pos + bar_h], GREY)
        self._top_bar(d, office, now, office.stalled(now))
        self._toast(d, office, now)
        return img
