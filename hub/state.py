"""办公室状态：有哪些牛马、各自在干嘛、谁把活交给了谁。"""
import hashlib
import re
import time
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field

MAX_BOTS = 6

# 交接动画时间线（秒）
T_GO, T_DROP, T_BACK = 1.4, 0.5, 1.0
T_HANDOFF = T_GO + T_DROP + T_BACK
# 老板发大饼巡视：走到一个 bot 面前 -> 发饼 -> 下一个 -> 回老板位
T_E_WALK, T_E_GIVE, T_E_BACK = 0.6, 1.0, 0.9
# 点名盯人：老板跑到这个 bot 面前，盯着它说"干活，我在盯着你"，再跑回去
T_P_SAY = 2.4
POKE_REPLY_WINDOW = 1800.0  # 盯过之后这么久里它私聊回的话，直接显示在它头顶
# 抽鞭子时间线（秒）
T_WHIP_HIT = 0.45      # 鞭梢落下
T_WHIP_JUMP = 0.95     # 跳起结束
T_WHIP_END = 4.5       # 惊慌打字结束

STATUSES = ("typing", "idle", "waiting")

# 自动缩短名字时先去掉这些词（工位名牌只放得下 3~4 个字）
SHORT_DROP = ("机器人", "服务端", "服务器", "工程师", "记录员", "游戏")


def _width(t):
    return sum(2 if ord(c) > 255 else 1 for c in t)


def auto_short(name, limit=8):
    """没配映射时的兜底：去掉"游戏/服务端/工程师"之类的词；还长（CJK 算 2 宽，超过 8）就截短。"""
    if _width(name) <= limit:
        return name
    out = name
    for w in SHORT_DROP:
        if len(out) > len(w):
            out = out.replace(w, "")
    out = out.strip(" -_·") or name
    while _width(out) > limit and len(out) > 1:
        out = out[:-1]
    return out


def parse_color(v):
    """'#rrggbb' 或 [r,g,b] -> (r,g,b)；无法识别返回 None。"""
    try:
        if isinstance(v, str):
            v = v.strip().lstrip("#")
            if len(v) == 6:
                return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
        elif v is not None and len(v) == 3:
            return tuple(max(0, min(255, int(x))) for x in v)
    except (ValueError, TypeError):
        pass
    return None


@dataclass
class Bot:
    name: str
    status: str = "idle"
    status_since: float = 0.0
    typing_until: float = 0.0      # 收到消息时临时显示打字
    sticky: bool = False           # 状态由桥接明确给出
    color: tuple = None            # bot 在 Grok Bot 里的头像颜色 (r,g,b)；没有就按名字生成
    say_text: str = ""             # 正在说的话（气泡/字幕）
    say_t0: float = 0.0
    say_until: float = 0.0
    say_private: bool = False      # True = 私聊我（悄悄话）
    say_inplace: bool = False      # 私聊，但它是在回复你的点名：不走过来说悄悄话，头顶直接冒对话框
    poked_at: float = -1e9         # 上次被点名盯的时间
    wait_note: str = ""            # 在等你批准什么（Grok Bot 侧边栏里的"需要批准：…"），挂在公告栏上


@dataclass
class Message:
    ts: float
    sender: str
    text: str
    is_bot: bool
    private: bool = False


@dataclass
class Handoff:
    src: int
    dst: int
    t0: float


QUESTION_DONE_SHOW = 120.0     # 确认完的问题在公告栏上再挂这么久（绿色「已确认：…」）
# bot 要你回话拍板，分两档：
#   确定：话里带约定的标记（默认「【待你拍板】」，config 里 [office] ask_mark 可改）-> 卡片自己弹出来一直挂着，点选项 = 替你 @它回这句
#   猜的：群里 @你（私聊不用）+ 关键词 + 请求语气，而且关键词前面没有「不再 / 已经…」-> 只在公告栏挂一条提醒，点一下才打开卡片
# （Grok Bot 自己的提问卡片走桥接的 question 事件，本来就是确定的。）
ASK_WORD_RE = re.compile(r"同意|批准|拍板|放行|授权|点头|回(?:复|个)?\s*[「『“\"]")   # 不含「确认」：bot 常说「确认：…」是报结果
ASK_STRICT_RE = re.compile(r"同意|批准|拍板|放行|授权|回(?:复|个)?\s*[「『“\"]")   # 私聊里没有 @，要求更明确
ASK_NEG_RE = re.compile(r"不|没|别|无需|无须|免|已|按你|照你|你之前|你刚")   # 同一小句里关键词前面有这些：是在说「不用等你同意 / 已经批了」
ASK_CLAUSE_RE = re.compile(r"[，。；！？,.;!?\s]+")
ASK_AXA_RE = re.compile(r"(.)不\1")   # 「要不要 / 同不同意 / 行不行」是在问你，里面的「不」不算否定
ASK_REQ_RE = re.compile(r"请你|麻烦你|需要你|要你|等你|你看|是否|可否|能否|行不行|可不可以|[吗么][？?]?|[？?]|回(?:复|个)?\s*[「『“\"]")
ASK_HINT_SHOW = 600.0          # 猜的提醒在公告栏上挂这么久没人理就收掉
ASK_QUOTE_RE = re.compile(r"回(?:复|个)?\s*[「『“\"]([^」』”\"]{1,12})[」』”\"]")
ASK_ALT_RE = re.compile(r"\s*(?:或者?|还是|、|/|／)\s*[「『“\"]([^」』”\"]{1,12})[」』”\"]")   # 回「换」或「不换」：后面的也是选项
CARD_MOVED_ON = 30.0           # Grok 提问卡片挂出来这么久以后，问的那个 bot 又说话了 = 已经有人选过 / 它不等了，卡片自己收掉
QUESTION_SNOOZE = 60.0         # 待确认的卡片会自己弹出来一直挂着；点「稍后」先收起这么久，到点再弹


@dataclass
class Question:
    """bot 在 Grok Bot 里弹出的带选项的提问卡片，要你选一个。"""
    qid: str
    bot: str                       # 工位短名（认不出是谁问的就是空字符串）
    title: str
    options: list
    recommended: int = -1          # 建议选哪个（下标），没有建议是 -1
    state: str = "pending"         # pending = 等你选 | sending = 桥接正在替你点 | answered = 确认完了
    answer: str = ""
    t0: float = 0.0
    done_at: float = 0.0
    detail: str = ""               # 问题下面那行说明
    chat: str = None               # 卡片在哪个聊天："" = 群聊，否则是单聊名；None = 老桥接没报
    kind: str = "card"             # card = Grok Bot 里的提问卡片 | ask = bot @你要你回话（选了就替你回一句）
    private: bool = False          # ask：是私聊里问的（回话也私聊回）
    sure: bool = True              # ask：False = 只是猜它要你回（没带标记），公告栏提醒一下，不自己弹出来


@dataclass
class Office:
    group: str = "牛马办公室"
    humans: tuple = ("我", "You", "Me")
    stall_seconds: float = 180.0
    sleep_after: float = 45.0
    bots: list = field(default_factory=list)
    messages: deque = field(default_factory=lambda: deque(maxlen=80))
    handoffs: list = field(default_factory=list)
    whip_t0: float = -1e9
    usage: dict = field(default_factory=dict)         # 额度：见 set_usage
    usage_alert_until: float = 0.0
    remote_state: dict = field(default_factory=dict)  # 云端任务状态 {短名: {"state","since","seen"}}
    cloud_return: dict = field(default_factory=dict)  # 云端干完、产物回来的时刻 {短名: ts}，用来播"包裹落下"动画
    remote_timeout: float = 1800.0                    # 超过这么久没再确认还在外包，就当它已经结束
    remote: dict = field(default_factory=dict)        # 配置里的云端 bot {完整名: 标签}，如 {"go游戏服务端": "Cursor"}
    enc_t0: float = -1e9               # 一键鼓励（老板发大饼）开始时间
    enc_order: list = field(default_factory=list)
    tour_kind: str = "pie"             # pie = 发大饼巡视 | poke = 点名盯一个人
    poke_text: str = "干活，我在盯着你"
    last_activity: float = field(default_factory=time.time)
    toast: tuple = ("", 0.0, "ok")
    voice: dict = None             # 正在录的语音：{"to": bot 短名或 None(群聊), "state": preparing|recording|sending, "t0", "rec_t0"}
    view: str = "office"
    chat_scroll: int = 0
    bridge_seen: float = 0.0
    device_online: bool = False
    show_chat: bool = False        # False = 黑板/气泡/聊天页不显示聊天内容，只显示谁在说话、几个人在干活
    aliases: dict = field(default_factory=dict)       # 配置里的名字映射 {完整名: 短名}
    auto_short: bool = True
    full2short: dict = field(default_factory=dict)    # 已经见过的 {完整名: 短名}
    questions: list = field(default_factory=list)     # 提问卡片 [Question]，先来的在前
    question_open: str = ""        # 屏幕上点开了哪张提问卡片（qid），空 = 没点开（没确认的还是会自己弹出来）
    question_snooze: dict = field(default_factory=dict)   # 点了「稍后」的卡片 {qid: 到这个时刻再弹}
    ask_mark: str = "待你拍板"     # bot 话里带「【待你拍板】」= 确定要你回话；空 = 不认标记
    ask_expired: set = field(default_factory=set)         # 挂满 ASK_HINT_SHOW 自己收掉的提醒，记录里再看到也不重挂
    question_closed: set = field(default_factory=set)     # 你点了「关掉」/ 自己收掉的卡片：桥接再报也不重挂

    # ---- 名字映射 ----
    def short(self, name):
        """完整名 -> 工位上显示的短名。两个 bot 缩成同一个名字时，后来的保留原名。"""
        if name in self.full2short:
            return self.full2short[name]
        s = self.aliases.get(name) or (auto_short(name) if self.auto_short else name)
        if s in self.full2short.values():
            s = name
        self.full2short[name] = s
        return s

    def fix_mentions(self, text):
        """把正文里的 @完整名 换成 @短名，这样交接（@）判断才对得上。"""
        for full in sorted(self.full2short, key=len, reverse=True):
            short = self.full2short[full]
            if full != short:
                text = text.replace("@" + full, "@" + short)
        return text

    # ---- 查询 ----
    def index(self, name):
        for i, b in enumerate(self.bots):
            if b.name == name:
                return i
        return -1

    def is_worker(self, name):
        """name（完整名或短名）是不是工位上的 bot。工位还没报上来时都算，免得 hub 刚启动漏掉。"""
        s = self.full2short.get(name) or self.aliases.get(name) or (auto_short(name) if self.auto_short else name)
        return not self.bots or self.index(s) >= 0 or self.index(name) >= 0

    def bridge_online(self, now):
        return now - self.bridge_seen < 45

    def effective_status(self, bot, now):
        if bot.status == "waiting":
            return "waiting"
        if bot.status == "typing" or now < bot.typing_until:
            return "typing"
        return "idle"

    def anyone_typing(self, now):
        return any(self.effective_status(b, now) == "typing" for b in self.bots)

    def silence(self, now):
        return max(0.0, now - self.last_activity)

    def stalled(self, now):
        """卡住 = 太久没动静且没人干活。外包给云端的活还在跑时不算卡住。"""
        return (bool(self.bots) and self.silence(now) > self.stall_seconds
                and not self.anyone_typing(now) and not self.any_outsourced(now))

    # ---- 外包给云端 ----
    def outsourced(self, name, now=None):
        st = self.remote_state.get(name)
        now = now or time.time()
        return bool(st and st["state"] == "working" and now - st["seen"] < self.remote_timeout)

    def any_outsourced(self, now=None):
        return any(self.outsourced(n, now) for n in list(self.remote_state))

    def outsourced_since(self, now=None):
        """最早的一个还在外包的任务开始了多久（秒）；没有返回 None。"""
        now = now or time.time()
        ts = [st["since"] for n, st in self.remote_state.items() if self.outsourced(n, now)]
        return now - min(ts) if ts else None

    def set_remote(self, name, state, now=None):
        """桥接上报云端任务状态：working | review | merged | finished | failed | ""（没有任务）。"""
        now = now or time.time()
        name = self.short(name)
        old = self.remote_state.get(name)
        if not state:
            self.remote_state.pop(name, None)
            return
        if old and old["state"] == state:
            old["seen"] = now
            return
        self.remote_state[name] = {"state": state, "since": now, "seen": now}
        self.last_activity = now                          # 状态变化算一次动静
        if state == "working":
            self.say("已外包给云端", "ok", now)
        elif state == "failed":
            self.say("云端运行失败", "err", now)
        else:
            self.say("云端干完了", "ok", now)
            self.cloud_return[name] = now
            i = self.index(name)
            if i >= 0:
                self.bots[i].typing_until = max(self.bots[i].typing_until, now + 6.0)

    # ---- 修改 ----
    def ensure_bot(self, name, now=None):
        i = self.index(name)
        if i >= 0:
            return i
        if len(self.bots) >= MAX_BOTS:
            return -1
        self.bots.append(Bot(name=name, status_since=now or time.time()))
        return len(self.bots) - 1

    def set_bots(self, names, now=None):
        now = now or time.time()
        # 每一项可以是名字，也可以是 {"name": ..., "color": "#rrggbb"}
        items = [(n, None) if isinstance(n, str) else (str(n.get("name", "")), parse_color(n.get("color")))
                 for n in names]
        items = [(self.short(n), c) for n, c in items if n][:MAX_BOTS]
        old = {b.name: b for b in self.bots}
        same = [n for n, _ in items] == [b.name for b in self.bots]
        self.bots = [old.get(n) or Bot(name=n, status_since=now) for n, _ in items]
        for b, (_, c) in zip(self.bots, items):
            if c:
                b.color = c
        if not same:
            self.handoffs.clear()

    def set_status(self, name, status, now=None):
        now = now or time.time()
        name = self.short(name)
        if status not in STATUSES:
            status = "idle"
        i = self.ensure_bot(name, now)
        if i < 0:
            return
        b = self.bots[i]
        b.sticky = True
        if b.status != status:
            b.status, b.status_since = status, now
            if status == "typing":
                self.last_activity = now

    def set_note(self, name, text):
        """某个 bot 在等你批准的事（空字符串 = 没有）。"""
        i = self.index(self.short(name))
        if i >= 0:
            self.bots[i].wait_note = " ".join(str(text or "").split())[:160]

    def notices(self, now):
        """公告栏：[(bot 下标, 文字)]，在等你批准的 bot 各一条。"""
        out = []
        for i, b in enumerate(self.bots):
            if self.effective_status(b, now) == "waiting":
                out.append((i, b.wait_note or "等你批准"))
        return out

    def board_items(self, now):
        """公告栏上的每一行，要紧的在前：[{"kind": question|approval|hint|confirmed, "bot": 下标或 -1, "text", "qid"}]。
        question = 等你选的提问卡片（点一下打开选项）；hint = 猜它要你回话（点一下打开）；confirmed = 刚确认完的，挂一会儿。"""
        out, hints = [], []
        for q in self.questions:
            if q.state == "answered":
                continue
            if q.sure or q.state == "sending":
                text = ("提交中… " if q.state == "sending" else "要你回：" if q.kind == "ask" else "待确认：") + q.title
                out.append({"kind": "question", "bot": self.index(q.bot), "text": text, "qid": q.qid})
            else:
                hints.append({"kind": "hint", "bot": self.index(q.bot), "text": "可能要你回：" + q.title, "qid": q.qid})
        out += [{"kind": "approval", "bot": i, "text": t, "qid": ""} for i, t in self.notices(now)]
        out += hints
        for q in reversed(self.questions):
            if q.state == "answered" and now - q.done_at < QUESTION_DONE_SHOW:
                out.append({"kind": "confirmed", "bot": self.index(q.bot), "text": f"已确认：{q.answer}（{q.title}）",
                            "qid": q.qid})
        return out

    # ---- 提问卡片 ----
    def question(self, qid):
        return next((q for q in self.questions if q.qid == qid), None)

    def shown_question(self, now=None):
        """屏幕上该摆出来的提问卡片：点开的那张；没点开就是最早一张还没确认、也没按「稍后」的。没有返回 None。"""
        now = now or time.time()
        q = self.question(self.question_open)
        if q is not None and q.state != "answered":
            return q
        return next((q for q in self.questions                 # 猜的（sure=False）不自己弹，点公告栏才打开
                     if q.state != "answered" and q.sure and self.question_snooze.get(q.qid, 0) <= now), None)

    def snooze_question(self, qid, now=None):
        self.question_snooze[qid] = (now or time.time()) + QUESTION_SNOOZE
        if self.question_open == qid:
            self.question_open = ""

    def set_question(self, qid, bot, title, options, recommended=-1, now=None, detail="", chat=None):
        """桥接看到一张等你选的提问卡片。同一张（qid 相同）再报一次只更新内容。关掉过的不再挂，返回 None。"""
        now = now or time.time()
        if qid in self.question_closed:
            return None
        bot = self.short(bot) if bot else ""
        title = " ".join(str(title).split())[:200]
        detail = " ".join(str(detail).split())[:200]
        options = [" ".join(str(o).split())[:120] for o in options if str(o).strip()][:6]
        q = self.question(qid)
        if q is None:
            q = Question(qid, bot, title, options, recommended, t0=now, detail=detail, chat=chat)
            self.questions.append(q)
            self.questions = self.questions[-12:]
            self.last_activity = now
            self.say("有问题等你确认", "warn", now)
        elif q.state != "answered":
            q.bot, q.title, q.options, q.recommended = bot or q.bot, title, options, recommended
            q.detail = detail or q.detail
            q.chat = q.chat if chat is None else chat
            if q.state == "sending" and now - q.done_at > 30:
                q.state = "pending"                       # 替你点了半天卡片还在：当没点成
        return q

    def answer_question(self, qid, answer, now=None):
        """确认完了（你在屏幕上选的、或者在电脑上直接点的）。"""
        now = now or time.time()
        q = self.question(qid)
        if q is None:
            return
        first = q.state != "answered"
        q.state, q.answer, q.done_at = "answered", " ".join(str(answer).split())[:120] or q.answer or "已处理", now
        if self.question_open == qid:
            self.question_open = ""
        self.question_snooze.pop(qid, None)
        if first:
            self.say("已确认：" + q.answer, "ok", now)

    def drop_question(self, qid):
        """卡片从界面上消失了、又不知道选了什么：没确认完的直接拿掉。"""
        q = self.question(qid)
        if q is not None and q.state != "answered":
            self.questions.remove(q)
        self.question_snooze.pop(qid, None)
        if self.question_open == qid:
            self.question_open = ""

    def close_question(self, qid, answer=None, now=None):
        """收掉一张卡片，以后桥接再报也不挂：answer=None 是你点了「关掉」（直接拿掉），否则记成已处理。"""
        self.question_closed.add(qid)
        if answer is None:
            self.drop_question(qid)
        else:
            self.answer_question(qid, answer, now)

    def ask_options(self, text, private=False):
        """bot 的这句话是不是要你回话拍板。是就返回 (给你选的回话 ["同意", "不同意"], 确定吗)，不是返回 None。
        确定 = 带了约定的标记；不确定 = 群里 @你（私聊不用）+ 关键词 + 请求语气，而且关键词不是「不再等你授权」这种说法。"""
        sure = bool(self.ask_mark_re().search(text))
        if not sure:
            if not private and not any("@" + h in text for h in self.humans if h):
                return None
            words = ASK_STRICT_RE if private else ASK_WORD_RE
            clauses = [ASK_AXA_RE.sub(r"\1", c) for c in ASK_CLAUSE_RE.split(text)]
            if not any(not ASK_NEG_RE.search(c[:m.start()])
                       for c in clauses for m in words.finditer(c)) or not ASK_REQ_RE.search(text):
                return None
        opts = []
        for m in ASK_QUOTE_RE.finditer(text):
            found, end = [m.group(1)], m.end()
            while (alt := ASK_ALT_RE.match(text, end)) is not None:
                found.append(alt.group(1))
                end = alt.end()
            for o in found:
                o = o.strip()
                if o and o not in opts:
                    opts.append(o)
        opts = opts[:4] or ["同意"]
        if opts == ["同意"]:
            opts.append("不同意")
        return opts, sure

    def ask_mark_re(self):
        """「【待你拍板】」：括号是全角、半角、方括号都认。"""
        if not self.ask_mark:
            return re.compile(r"(?!)")
        return re.compile(r"[【\[〔(（]\s*" + re.escape(self.ask_mark) + r"\s*[】\]〕)）]")

    @staticmethod
    def ask_id(bot, text):
        return "ask" + hashlib.sha1(f"{bot}\n{text}".encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def ask_bumps(old_sure, new_sure, private, same_bot):
        """新来一句要你回的，前面没回的那张要不要顶掉：群里后来的顶掉前面的（一般是 PM 汇总过的那句），
        私聊里每个 bot 一张；猜的不顶掉确定的。"""
        return (same_bot or not private) and (new_sure or not old_sure)

    def add_ask(self, bot, text, options, private, now, sure=True):
        """bot 要你回话：挂一张 ask 卡片（sure=False 只在公告栏提醒）。这句已经挂过（或回过、提醒过期了）就不动。"""
        qid = self.ask_id(bot, text)
        if self.question(qid) is not None or qid in self.ask_expired or qid in self.question_closed:
            return
        for q in list(self.questions):
            if (q.kind == "ask" and q.private == private and q.state == "pending"
                    and self.ask_bumps(q.sure, sure, private, q.bot == bot)):
                self.drop_question(q.qid)
        title = self.ask_mark_re().sub(" ", text).strip(" ，,:：")
        for h in self.humans:                                  # 开头的「@你」不用再显示
            if h and title.startswith("@" + h):
                title = title[len(h) + 1:].strip(" ，,:：")
        q = self.set_question(qid, bot, title, options, -1, now, chat=None)
        q.kind, q.private, q.sure = "ask", bool(private), bool(sure)
        q.title = " ".join(title.split())[:300]
        if not sure:
            self.say(f"{bot}可能要你回话", "ok", now)

    def scan_asks(self, messages, private=False, now=None):
        """桥接报来的、界面上现在看得到的一段聊天记录 [(发言人, 正文)]（启动、切聊天、hub 重启时报）：
        里面 bot 要你回话、之后你还没回的，补挂上——之前没赶上当新消息报上来的也不漏。"""
        now = now or time.time()
        pending, replied = {}, []                          # {bot 短名: (正文, 选项, 确定吗)}，[(卡片 id, 你回的话)]
        for sender, text in messages:
            text = self.fix_mentions(" ".join(str(text).split()))
            if sender in self.humans:                      # 你回过话：同 add_message 的规则收掉
                named = [b for b in pending if "@" + b in text]
                for b in named or ([] if "@" in text else list(pending)):
                    replied.append((self.ask_id(b, pending.pop(b)[0]), text))
                continue
            if not self.is_worker(sender):                 # 不是工位上的 bot（比如 Grok Bot 自带的助手）：不算
                continue
            got = self.ask_options(text, private)
            if not got:
                continue
            opts, sure = got
            bot = self.short(sender)
            for b, (t, _o, s) in list(pending.items()):    # 同 add_ask 的顶掉规则
                if self.ask_bumps(s, sure, private, b == bot):
                    replied.append((self.ask_id(b, t), None))
                    del pending[b]
            if bot not in pending:                         # 它前面那句确定的没被这句猜的顶掉：留着前面那句
                pending[bot] = (text, opts, sure)
        for qid, reply in replied:                         # 已经挂着的卡片、记录里看到你回过了 / 被顶掉了：收起来
            q = self.question(qid)
            if q is not None and q.state == "pending":
                if reply is None:
                    self.drop_question(qid)
                else:
                    self.answer_question(qid, reply[:40], now)
        for b, (text, opts, sure) in sorted(pending.items(), key=lambda kv: not kv[1][2]):   # 确定的先挂，猜的顶不掉它
            self.add_ask(b, text, opts, private, now, sure)

    def add_message(self, sender, text, now=None, private=False):
        now = now or time.time()
        is_human = sender in self.humans
        if not is_human:
            sender = self.short(sender)
        text = self.fix_mentions(" ".join(str(text).split()))
        i = -1 if is_human else self.ensure_bot(sender, now)
        self.messages.append(Message(now, sender, text, i >= 0, bool(private)))
        self.chat_scroll = 0
        if is_human:                       # 你自己回过话了（电脑上打的也算）：同一边（群 / 私聊）等你回的卡片就收起来；
            asks = [q for q in self.questions          # 话里 @ 了谁就只收那个 bot 的
                    if q.kind == "ask" and q.state == "pending" and q.private == bool(private)]
            named = [q for q in asks if q.bot and "@" + q.bot in text]
            for q in named or ([] if "@" in text else asks):
                self.answer_question(q.qid, text[:40], now)
        elif i >= 0:
            for q in list(self.questions):         # 问你的 bot 接着说话了：Grok 提问卡片会让它停下来等，说明已经选过 / 不等了
                if q.kind == "card" and q.state == "pending" and q.bot == sender and now - q.t0 > CARD_MOVED_ON:
                    self.close_question(q.qid, "已处理（它接着干了）", now)
            got = self.ask_options(text, private)
            if got:
                self.add_ask(sender, text, got[0], private, now, got[1])
        if i < 0:
            return
        self.last_activity = now
        b = self.bots[i]
        # 被点名后私聊回的话是回给你的，就算不展示聊天内容也照样显示原文
        inplace = bool(private) and now - b.poked_at < POKE_REPLY_WINDOW
        show = self.show_chat or inplace
        b.say_text, b.say_t0, b.say_private, b.say_inplace = (text if show else "…"), now, bool(private), inplace
        b.say_until = now + (min(10.0 if inplace else 8.0, 3.0 + len(text) / 8.0) if show else 2.5)
        if private:
            return                         # 悄悄话不算群里的动作，也不触发交接
        b.typing_until = now + 4.0
        if not b.sticky:
            b.status_since = now
        # 消息里 @ 了别的 bot，就当作交接
        for j, other in enumerate(self.bots):
            if j != i and ("@" + other.name) in text:
                self.handoff(sender, other.name, now)

    def handoff(self, src, dst, now=None):
        now = now or time.time()
        i, j = self.ensure_bot(self.short(src), now), self.ensure_bot(self.short(dst), now)
        if i < 0 or j < 0 or i == j:
            return
        if any(h.src == i and now - h.t0 < T_HANDOFF for h in self.handoffs):
            return
        self.handoffs.append(Handoff(i, j, now))
        self.last_activity = now
        self.bots[j].typing_until = max(self.bots[j].typing_until, now + T_GO + T_DROP + 5.0)

    def encourage(self, now=None):
        """老板挨个走到每个 bot 面前发大饼。已经在发就不重复开始。返回是否开始。"""
        now = now or time.time()
        if self.tour_at(now) or not self.bots:
            return False
        self.enc_t0, self.enc_order, self.tour_kind = now, list(range(len(self.bots))), "pie"
        self.last_activity = now
        return True

    def poke(self, i, now=None):
        """老板跑到第 i 个 bot 面前盯着它。已经在巡视就不开始。返回是否开始。"""
        now = now or time.time()
        if self.tour_at(now) or not 0 <= i < len(self.bots):
            return False
        self.enc_t0, self.enc_order, self.tour_kind = now, [i], "poke"
        self.bots[i].poked_at = now
        self.last_activity = now
        return True

    def tour_at(self, now):
        """当前巡视进度：None 或 {"stage": walk|give|back, "k": 第几个, "bot": bot 下标, "p": 0..1, "kind": pie|poke}"""
        tr = self._tour_at(now)
        if tr:
            tr["kind"] = self.tour_kind
            if self.tour_kind == "poke":
                tr["say"] = self.poke_text
        return tr

    def _tour_at(self, now):
        n = len(self.enc_order)
        t = now - self.enc_t0
        if t < 0 or n == 0:
            return None
        give = T_P_SAY if self.tour_kind == "poke" else T_E_GIVE
        per = T_E_WALK + give
        if t < n * per:
            k = int(t // per)
            r = t - k * per
            bot = self.enc_order[k]
            if bot >= len(self.bots):
                return None
            if r < T_E_WALK:
                p = r / T_E_WALK
                return {"stage": "walk", "k": k, "bot": bot, "p": p * p * (3 - 2 * p)}
            return {"stage": "give", "k": k, "bot": bot, "p": (r - T_E_WALK) / give}
        tb = t - n * per
        if tb < T_E_BACK:
            p = tb / T_E_BACK
            return {"stage": "back", "k": n, "bot": self.enc_order[-1], "p": p * p * (3 - 2 * p)}
        return None

    def cheering(self, i, now):
        tr = self.tour_at(now)
        return bool(tr and tr["kind"] == "pie" and tr["stage"] == "give" and tr["bot"] == i)

    def stared(self, i, now):
        """正被老板盯着（点名）。"""
        tr = self.tour_at(now)
        return bool(tr and tr["kind"] == "poke" and tr["stage"] == "give" and tr["bot"] == i)

    # ---- 额度 ----
    USAGE_LABELS = (("grok", "Grok Bot 每周"), ("cursor", "Cursor 模型"), ("other", "其他模型"))

    def set_usage(self, ev, now=None):
        """桥接上报的额度。ev = {ok, error, plan, grok:{pct, reset}, cursor, other, total}。
        某项首次越过 80% / 95% 时弹提示；Grok Bot 每周额度到 100% 时 quota_blocked 为真。"""
        now = now or time.time()
        old = self.usage if self.usage.get("ok") else {}
        new = {"ok": bool(ev.get("ok")), "error": str(ev.get("error", ""))[:60], "ts": now,
               "plan": str(ev.get("plan", "")), "total": ev.get("total")}
        g = ev.get("grok") or {}
        reset = None
        if g.get("reset"):
            try:
                reset = datetime.fromisoformat(str(g["reset"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        new["grok"] = {"pct": g.get("pct"), "reset": reset}
        new["cursor"], new["other"] = ev.get("cursor"), ev.get("other")
        self.usage = new
        if not new["ok"]:
            return
        for key, label in self.USAGE_LABELS:
            cur = new["grok"]["pct"] if key == "grok" else new.get(key)
            before = (old.get("grok", {}).get("pct") if key == "grok" else old.get(key)) if old else None
            if cur is None or before is None:
                continue
            for th in (95, 80):
                if before < th <= cur:
                    self.say(f"{label}额度已用 {cur:.0f}%", "warn" if th == 80 else "err", now)
                    self.usage_alert_until = now + 8.0
                    break

    def usage_pct(self, key):
        u = self.usage
        if not u.get("ok"):
            return None
        return u["grok"]["pct"] if key == "grok" else u.get(key)

    def quota_blocked(self):
        p = self.usage_pct("grok")
        return p is not None and p >= 100

    def remote_label(self, name):
        """name 是工位上的短名；返回云端标签（如 "Cursor"），不是云端 bot 返回 None。"""
        for full, label in self.remote.items():
            if self.short(full) == name:
                return label or "云"
        return None

    def whip(self, now=None):
        self.whip_t0 = now or time.time()

    def say(self, text, kind="ok", now=None):
        self.toast = (text, now or time.time(), kind)

    def tick(self, now):
        self.handoffs = [h for h in self.handoffs if now - h.t0 < T_HANDOFF]
        for q in list(self.questions):     # 猜它要你回的提醒，挂久了没人理（也没点开）就收掉
            if (q.kind == "ask" and not q.sure and q.state == "pending" and now - q.t0 > ASK_HINT_SHOW
                    and self.question_open != q.qid):
                self.drop_question(q.qid)
                self.ask_expired.add(q.qid)
        # 桥接掉线后，它之前报的状态不可信了
        if self.bridge_seen and not self.bridge_online(now):
            for b in self.bots:
                if b.sticky and b.status != "idle":
                    b.status, b.status_since, b.sticky = "idle", now, False

    def snapshot(self, now):
        return {
            "group": self.group,
            "silence": round(self.silence(now)),
            "stalled": self.stalled(now),
            "bridge_online": self.bridge_online(now),
            "device_online": self.device_online,
            "view": self.view,
            "bots": [{"name": b.name, "status": self.effective_status(b, now)} for b in self.bots],
            "show_chat": self.show_chat,
            "usage": self.usage,
            "remote": {n: st["state"] for n, st in self.remote_state.items()},
            "notices": [{"bot": self.bots[i].name, "text": t} for i, t in self.notices(now)],
            "questions": [{"id": q.qid, "kind": q.kind, "sure": q.sure, "bot": q.bot, "title": q.title, "detail": q.detail, "options": q.options,
                           "recommended": q.recommended, "state": q.state, "answer": q.answer}
                          for q in self.questions if q.state != "answered" or now - q.done_at < QUESTION_DONE_SHOW],
            "voice": self.voice,
            "messages": [{"sender": m.sender, "text": m.text if self.show_chat else "…"} for m in list(self.messages)[-10:]],
        }
