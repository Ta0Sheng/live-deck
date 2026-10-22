"""直播鱼：读字幕 + 收弹幕 → 调模型 → 像个人一样说一句（或者干脆不说）。

它是一个独立进程（``python -m live_deck.fish``），由服务端托管，也可以单独跑。
所有发送行为受配置里两个总闸控制：

* ``fish.send_danmaku``（默认 false）—— 关着就永远只出草稿；
* ``fish.allow_gift``（默认 false）—— 关着就永远不会花钱。

用法::

    live-deck fish --room 123456 --dry
    python -m live_deck.fish --room 123456 --asr-log data/logs/asr-live-123456.log
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import aiohttp
import requests
from bilibili_api import Api, live
from bilibili_api.live import Danmaku
from bilibili_api.live import API as LIVE_API
from bilibili_api.utils.network import _enc_wbi, _get_mixin_key

from .. import bili
from ..config import get_settings
from .budget import GiftBudget
from .persona import load_persona

S = get_settings()
BANNED = re.compile(r"骗|诈骗|违法|犯罪|刑|未成年|举报|起诉|人渣|骗子")

SYSTEM_PROMPT = load_persona()

# 语料只从 data/ 读：仓库里带示例，用户自己的样本也写这里，不进 git。
LOGDIR = S.logs_dir
CORPUS_PATH = S.root / "persona" / "corpus" / "tone.md"
GOOD_PATH = S.good_samples
BAD_PATH = S.bad_samples
corpus_lines: list[str] = []
good_lines: list[str] = []
bad_lines: list[str] = []
# 2026-10-22：语境轴（这个场面该不该接），和「像不像人」分开学
ctx_good: list[str] = []
ctx_bad: list[str] = []


def _read_bullets(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip().lstrip("-· ").strip()
        if s and not s.startswith(("#", ">")):
            rows.append(s)
    return rows[-n:]


def load_good(n: int = 24) -> list[str]:
    """用户夸过的发言：学味道（怎么问、怎么回扣），别照抄。"""
    return _read_bullets(GOOD_PATH, n)


def load_bad(n: int = 12) -> list[str]:
    """用户点名扣分的说法：反面例子，别学这个语气。"""
    return _read_bullets(BAD_PATH, n)


# 语境轴单独记 —— 「这个场面该不该接」和「这话说得像不像人」是两回事。
CTX_GOOD_PATH = S.ctx_good_samples
CTX_BAD_PATH = S.ctx_bad_samples


def load_ctx_good(n: int = 8) -> list[str]:
    return _read_bullets(CTX_GOOD_PATH, n)


def load_ctx_bad(n: int = 8) -> list[str]:
    return _read_bullets(CTX_BAD_PATH, n)
DM_LOG_DIR = S.danmaku_dir
# 这间房的「名词/疑问 → 答案」：鱼问明白的记在这，下次不再问第二遍
KB_DIR = S.knowledge_dir
kb_lines: list[str] = []
question_samples: list[str] = []
last_q_refresh = 0.0
# 最近问过的问题：避免一直问同一句（问一次没人答，过一阵再问）
RECENT_ASKS: deque[str] = deque(maxlen=3)
# 最近 5 条里问了几条：问得太密就提醒它别再问
Q_FLAGS: deque[bool] = deque(maxlen=5)
last_reload = 0.0
# 刚查到的资料：下一条发言可以用上（自然说出来，而不是"我搜过了"）
LAST_SEARCH: dict = {"term": "", "summary": "", "ts": 0.0}
SEARCH_BUDGET: dict = {"n": 0, "ts": 0.0}
PENDING: dict = {"q": "", "ts": 0.0, "tries": 0}


def load_kb(room: int, n: int = 40) -> list[str]:
    f = KB_DIR / f"{room}.md"
    if not f.exists():
        return []
    rows = [line.strip() for line in f.read_text(encoding="utf-8", errors="replace").splitlines()]
    return [line for line in rows if line.startswith("- ")][-n:]


def load_question_samples(room: int, n: int = 12) -> list[str]:
    """从这间房的真实弹幕里挑出观众问过的问句，当提问范本（学口气，不抄内容）。"""
    f = DM_LOG_DIR / f"{room}.jsonl"
    if not f.exists():
        return []
    rows = f.read_text(encoding="utf-8", errors="replace").splitlines()[-600:]
    out: list[str] = []
    for ln in rows:
        try:
            x = str(json.loads(ln).get("x") or "")
        except Exception:
            continue
        if 2 <= len(x) <= 28 and looks_like_question(x) and not SELF_CALL.match(x):
            out.append(x)
    return out[-n:]


def save_kb(room: int, q: str, a: str) -> None:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    f = KB_DIR / f"{room}.md"
    new = not f.exists()
    line = f"- {q} ｜ 答：{a}"
    with f.open("a", encoding="utf-8") as fh:
        if new:
            fh.write(f"# 直播间 {room} 的知识库\n\n> 鱼问过、也问明白的东西，一行一条，自动追加。\n\n")
        fh.write(line + "\n")
    kb_lines.append(line)
danmaku_style = ""
signal_lines: deque = deque(maxlen=12)
SIGNAL_PATH: Path | None = None


def load_signals(room: int, n: int = 10) -> deque:
    """读上次留下的暗号示范句（用户以「。」开头发的弹幕）。"""
    f = DM_LOG_DIR / f"暗号-{room}.jsonl"
    if not f.exists():
        return deque(maxlen=12)
    rows = f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    out = deque(maxlen=12)
    for ln in rows:
        try:
            out.append(json.loads(ln)["x"])
        except Exception:
            pass
    return out
room_id_for_log = 0


def load_danmaku_style(n: int = 60) -> str:
    """从记录下来的弹幕里学这间房的说话习惯：长度、问句、笑声、标点。"""
    f = DM_LOG_DIR / f"{room_id_for_log}.jsonl"
    if not f.exists():
        return ""
    rows = f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    texts = []
    for ln in rows:
        try:
            texts.append(json.loads(ln)["x"])
        except Exception:
            pass
    if len(texts) < 20:
        return ""
    lens = [len(t) for t in texts]
    avg = sum(lens) / len(lens)
    q = sum(1 for t in texts if "？" in t or "?" in t) / len(texts)
    haha = sum(1 for t in texts if "哈哈" in t or "笑死" in t) / len(texts)
    marks = sum(1 for t in texts if "…" in t or "。。" in t) / len(texts)
    return (f"这间房弹幕：一句平均 {avg:.0f} 字；问句占 {q:.0%}；"
            f"「哈哈/笑死」占 {haha:.0%}；用省略号或叠句号的占 {marks:.0%}。")


def load_corpus(n: int = 30) -> list[str]:
    """从 persona/corpus/tone.md 里随机抽 n 条，当语感参考（可选文件）。"""
    if not CORPUS_PATH.exists():
        return []
    try:
        text = CORPUS_PATH.read_text(encoding="utf-8")
    except Exception as e:
        log(f"语料读取失败：{e}")
        return []
    pool_pref, pool_rest = [], []
    for ln in text.splitlines():
        if not ln.startswith("- 【"):
            continue
        m = re.match(r"- 【(.+?)】(.*)", ln)
        if not m:
            continue
        tag, body = m.group(1), m.group(2).split("　｜问：")[0].strip()
        if any(w in body for w in ("尾巴", "鲸鱼", "算力", "模型", "AI", "女仆", "机器人")):
            continue
        (pool_pref if tag in ("问答", "唱歌", "被夸", "游戏") else pool_rest).append(body)
    if not pool_pref and not pool_rest:
        return []
    # 七成挑"俏皮/自嘲/胡想"那类，三成留点毒
    k1 = min(len(pool_pref), max(0, int(n * 0.75)))
    k2 = min(len(pool_rest), max(0, n - k1))
    return random.sample(pool_pref, k1) + random.sample(pool_rest, k2)


_LOGFILE: Path | None = None


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def get_cred():
    """登录态统一从 live_deck.bili 取（扫码或手写 JSON 都行）。"""
    return bili.load_credential()


# 自己发的弹幕不参与判定；能点单送礼的只有 owner（空 = 谁都不行）
BOT_UID = bili.bot_uid()
OWNER_UID = S.fish.owner_uid
# 「鱼，」开头 = 弹幕里在叫这只鱼。谁发都算（2026-10-20 改成不再只看主人）。
# 只认逗号，不认冒号（用户要求：鱼： 不算）。
CALL_RE = re.compile(rf"^\s*{re.escape(S.fish.call_word)}\s*[，,]")
# 「鱼，」是观众叫它的暗号，它自己绝不能这么开口
SELF_CALL = re.compile(
    rf"^\s*({re.escape(S.fish.name)}|{re.escape(S.fish.call_word)})\s*[，,、:：]\s*"
)


def strip_self_call(t: str) -> str:
    return SELF_CALL.sub("", (t or "").strip()).strip()
GIFT_URL = "https://api.live.bilibili.com/xlive/revenue/v2/gift/sendGoldMultiUser"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0"

# 可用表情目录（内存，进程退出就没了）：unique -> {unique,text,desc}
# 不写死清单：能用哪些表情取决于账号在这间房的权限，所以从别人真发过的弹幕里采集。
emote_catalog: dict[str, dict] = {}


async def send_ticket(room_id: int, anchor: int, gift_id: int = 31164, price: int = 100) -> dict:
    """送 1 张人气票（gift_id 31164，抓包复刻）。"""
    cred = get_cred()
    params = {
        "uid": BOT_UID,
        "gift_id": gift_id,
        "ruid": anchor,
        "send_ruid": 0,
        "gift_num": 1,
        "coin_type": "gold",
        "bag_id": 0,
        "platform": "pc",
        "biz_code": "Live",
        "biz_id": room_id,
        "storm_beat_id": 0,
        "metadata": "",
        "price": price,
        "receive_users": json.dumps([{"uid": anchor}], separators=(",", ":")),
        "live_statistics": json.dumps({"pc_client": "pcWeb", "jumpfrom": "85001", "room_category": "0",
                                       "source_event": 0, "trackid": "-99998",
                                       "official_channel": {"program_room_id": "-99998", "program_up_id": "-99998"}},
                                      separators=(",", ":")),
        "statistics": json.dumps({"platform": 5, "pc_client": "pcWeb", "appId": 100}, separators=(",", ":")),
        "web_location": "444.8",
        "csrf": cred.bili_jct,
    }
    signed = _enc_wbi(dict(params), await _get_mixin_key(cred))
    cookies = {"SESSDATA": cred.sessdata, "bili_jct": cred.bili_jct, "buvid3": cred.buvid3}
    headers = {"accept": "*/*", "origin": "https://live.bilibili.com",
               "referer": f"https://live.bilibili.com/{room_id}", "user-agent": UA}
    async with aiohttp.ClientSession(cookies=cookies) as s:
        async with s.post(GIFT_URL, params=signed, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json()


# ── 表情弹幕（2026-10-21 加）───────────────────────────────────────────────
# 表情和文字走同一个接口 msg/send，多两个参数：dm_type=1 + emoticon(JSON 字符串)。
# 权限决定能用哪些表情，所以不写死清单 —— 从收到的弹幕里捡（别人真发过 = 这间房接受）。
def harvest_emoticon(info) -> None:
    try:
        head = info[0] if info else None
        if not isinstance(head, list):
            return
        for seg in head:
            if isinstance(seg, dict) and seg.get("emoticon_unique"):
                uniq = str(seg["emoticon_unique"])
                if uniq and uniq not in emote_catalog:
                    emote_catalog[uniq] = {
                        "unique": uniq,
                        "text": str(info[1] or "[表情]"),
                        "desc": str(seg.get("descript") or seg.get("emoji") or ""),
                    }
                    log(f"采集到可用表情：{emote_catalog[uniq]['text']}（{uniq}）")
                return
    except Exception:
        pass


async def send_emoticon(room_id: int, entry: dict) -> dict:
    """发一条表情弹幕（参数照 msg/send 网页版：dm_type=1 + emoticon）。"""
    cred = get_cred()
    data = {
        "roomid": room_id,
        "color": 16777215,
        "fontsize": 25,
        "mode": 1,
        "msg": entry.get("text") or "[表情]",
        "rnd": int(time.time()),
        "bubble": 0,
        "dm_type": 1,
        "emoticon": json.dumps(
            {"emoticon_unique": entry["unique"], "bulge_display": 0}, separators=(",", ":")
        ),
    }
    return await Api(**LIVE_API["operate"]["send_danmaku"], credential=cred).update_data(**data).result


def echoes(text: str, src: str, min_len: int = 7) -> bool:
    """是不是把主播刚说的话原样搬过来了（连续 7 个字一样就算复述）。"""
    if not text or not src or len(text) < min_len:
        return False
    for i in range(len(text) - min_len + 1):
        if text[i:i + min_len] in src:
            return True
    return False


def _similar(a: str, b: str) -> bool:
    """判断两条弹幕意思是不是同一句（容忍改写/删词）。"""
    a2 = a.replace(" ", "").replace("，", "").replace(",", "")
    b2 = b.replace(" ", "").replace("，", "").replace(",", "")
    if not a2 or not b2:
        return False
    if a2 == b2 or a2 in b2 or b2 in a2:
        return True
    ga = {a2[i:i + 2] for i in range(len(a2) - 1)} or {a2}
    gb = {b2[i:i + 2] for i in range(len(b2) - 1)} or {b2}
    inter = len(ga & gb)
    return inter / max(1, min(len(ga), len(gb))) > 0.6


async def ask_model(key: str, context: str, image_jpeg: bytes | None = None) -> str:
    # 记忆是可选的：新电脑上 data/ 是空的，这里自然没有可带的记忆。
    mem_file = S.memory_file
    try:
        mem = mem_file.read_text(encoding="utf-8")[:1200] if mem_file else ""
    except Exception:
        mem = ""
    system = SYSTEM_PROMPT
    if corpus_lines:
        system += "\n\n【Neuro 的真实台词样本 —— 学句式、节奏、收尾，别照抄】\n" + "\n".join(corpus_lines)
    if danmaku_style:
        system += ("\n\n【这间房观众怎么说话（跟着节奏，别学内容）】\n" + danmaku_style
                   + "\n（问句占比低不代表你也少问：你是新来的，看不懂就问，问得自然就行）")
    if signal_lines:
        system += ("\n\n【使用者亲自发的示范句 —— 学他的语气、断句、说话角度，别照抄内容】\n"
                   + "\n".join(f"· {s}" for s in signal_lines))
    if mem:
        system += "\n\n【你和 QQ/B站 共用的记忆，用得上就自然带出来，别整段复述】\n" + mem
    if good_lines:
        system += ("\n\n【用户亲口夸过「观感不错」的发言 —— 学这个味道（怎么问、怎么回扣、怎么用学到的词），别照抄。"
                   "注意：「我搜过了」这种话只有系统真给了你资料才能说】\n"
                   + "\n".join(f"✓ {g}" for g in good_lines))
    if bad_lines:
        system += ("\n\n【扣分的说法：同样是场面 + 那句话，这样的接法在这个场面里不像人】\n"
                   + "\n".join(f"✗ {b}" for b in bad_lines))
    # 2026-10-22：语境轴 —— 只判「这个场面该不该开口」，和像不像人分开学
    if ctx_good:
        system += ("\n\n【用户标过「这个场面接得对」的】\n" + "\n".join(f"· {g}" for g in ctx_good))
    if ctx_bad:
        system += ("\n\n【用户标过「这个场面不该接」的 —— 语境不对，话再好也别开口】\n"
                   + "\n".join(f"· {b}" for b in ctx_bad))
    if question_samples:
        system += ("\n\n【这间房观众怎么问问题 —— 学他们提问的角度和口气：短、具体、像随口一问；别照抄内容】\n"
                   + "\n".join(f"· {q}" for q in question_samples))
    if LAST_SEARCH["summary"] and time.time() - LAST_SEARCH["ts"] < 180:
        system += (f"\n\n【你刚上网查到的】{LAST_SEARCH['term']}：{LAST_SEARCH['summary']}\n"
                   "用一句自然的话带出来（「哦，蓝色杯子是个B站UP主」那种），别只念关键词，也别再说「我搜过了」")
    if RECENT_ASKS:
        system += ("\n\n【你刚才已经问过这些 —— 别原样再问第二遍，没人答就换个角度或者先聊别的】\n"
                   + "\n".join(f"· {q}" for q in RECENT_ASKS))
    n_q = sum(1 for f in Q_FLAGS if f)
    if n_q >= 2:
        system += (f"\n\n【你最近问得太多了（近 5 条里有 {n_q} 条是问句）—— 这一条别再问，接梗、吐槽或者自嘲】")
    if kb_lines:
        system += ("\n\n【你已经问明白的东西 —— 别再问第二遍；用得上的时候自然带一句，别整段背】\n"
                   + "\n".join(kb_lines))
    user_content = context
    if image_jpeg:
        # 带图：DeepSeek 这个接口是能吃图的（实测 200），图片走 data URI。
        b64 = base64.b64encode(image_jpeg).decode()
        user_content = [
            {"type": "text", "text": context},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]
    payload = {
        "model": S.fish.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 90 if image_jpeg else S.fish.max_tokens,
        "temperature": S.fish.temperature,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=40),
        ) as r:
            data = await r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


# ── 疑问 → 答案：问过的问题，等弹幕/主播回答了，就记进知识库 ────────────────
LEARN_PROMPT = (
    "你在帮一条直播间的观众记住它问过的问题的答案。\n"
    "它问过：{q}\n\n"
    "之后直播间的消息：\n{msgs}\n\n"
    "如果里面有人明确回答了这个问题、或解释了这个词，输出一行：\n"
    "答：<一句话答案，20 字以内>\n"
    "如果没有回答、或者看不出来，只输出 SKIP。不要猜、不要编、不要解释。"
)

QUESTION_RE = re.compile(r"[?？]|吗$|呢$|啥|什么|为什么|怎么|谁|哪|是啥")


def looks_like_question(t: str) -> bool:
    return bool(QUESTION_RE.search((t or "").strip()))


SEARCH_RE = re.compile(r"^\[查[:：]\s*(.{1,24}?)\s*\]$")
# 「投喂」：演练用。她直接写「投喂 小心心 x2」（不带中括号）也认，统一格式化成弹幕，不真扣钱
FEED_RE = re.compile(r"^\[?投喂[:：，,\s]*([^\]，,]{1,12}?)\s*(?:[x×*]\s*(\d{1,2}))?\s*\]?$")
# 「X 是啥 / X 是什么梗」—— 她自己去问的时候，顺手替她查一下
QUESTION_TERM = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})\s*(?:到底)?(?:是个?啥|是什么梗|是什么|啥意思)")
SEARCHED: dict[str, float] = {}


async def search_bili(term: str, n: int = 6) -> list[str]:
    """B站搜一下：梗、番、人、道具 —— 比通用搜索更贴这间房。"""
    return await bili.search_videos(term, n)


async def summarize_search(key: str, term: str, titles: list[str]) -> str:
    prompt = (
        f"下面是 B站搜「{term}」的前几条标题。用一句中文（25 字以内）告诉观众：它大概是什么、"
        f"为什么直播间会刷它。\n" + "\n".join(f"- {t}" for t in titles)
        + "\n\n规则：标题里反复出现的词就是线索（人名/作品名/歌名/事件名）；"
          "只要看得出方向就大胆说，别因为不精确就回「不知道」；"
          "**提到的名字必须是标题里出现过的，标题没有的名字一个都不许编**；"
          "只有标题完全看不出信息才回「不知道」。"
    )
    payload = {
        "model": S.fish.flash_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 60,
        "temperature": 0.2,
        "thinking": {"type": "disabled"},
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            data = await r.json()
    return (data["choices"][0]["message"]["content"] or "").strip().strip("。")


async def do_search(key: str, room: int, term: str) -> str:
    """搜 + 一句话总结 + 记进知识库。返回总结；没搜到返回空串。"""
    titles = await search_bili(term)
    if not titles:
        return ""
    summary = await summarize_search(key, term, titles)
    if summary and summary != "不知道" and len(summary) <= 60:
        save_kb(room, term, summary)
        log(f"搜到了：{term} → {summary}")
        return summary
    return ""


def judge_system() -> str:
    """打分用的判据：把用户点过名的那两批例子带上，才评得准。"""
    s = (
        "你是直播间里的老观众，在判断一条弹幕像不像真人说的。\n"
        "像人：接住此刻正在发生的事、有情绪、有具体的疑问、短。\n"
        "不像人：复述主播刚说的话、总结/升华/上价值、挑刺阴阳、答非所问、空洞捧场、"
        "同一个东西问第二遍、场面完全不对还硬接。\n"
        "例子前面的【】是当时的场面 —— 判断时要先看场面对不对。\n"
        "硬规则：吐槽主播的设备/网速/画面/游戏优化 → 最多 2 分；复述主播刚说的原话 → 最多 2 分；"
        "对主播本人的真诚评价（夸、调侃、感慨）→ 至少 3 分。\n"
    )
    if good_lines:
        s += "\n像人的（加分）：\n" + "\n".join(f"✓ {g}" for g in good_lines[-10:])
    if bad_lines:
        s += "\n不像人的（扣分）：\n" + "\n".join(f"✗ {b}" for b in bad_lines[-6:])
    s += ("\n\n先一句话说理由（这句子在这个场面里像不像人，卡在哪），再单独一行写「分数：X」。\n"
           "1=一眼机器人（复述/总结/挑刺/答非所问）；2=有点机械；3=普通观众水平；4=自然、有细节；5=很像真人。")
    return s



async def judge_human(key: str, ctx: str, reply: str) -> float:
    """就这一刻的语境，给这句话的「像人度」打 1-5 分。"""
    payload = {
        "model": S.fish.model,
        "messages": [{"role": "system", "content": judge_system()}, {"role": "user", "content": f"【此刻语境】\n{ctx[:700]}\n\n【这条弹幕】\n{reply}"}],
        "max_tokens": 80,
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                data = await r.json()
        txt = (data["choices"][0]["message"]["content"] or "").strip()
        m = re.search(r"分数[:：]\s*(\d+(?:\.\d+)?)", txt) or re.search(r"(\d+(?:\.\d+)?)\s*/?\s*5", txt)
        v = float(m.group(1)) if m else 3.0
        return max(1.0, min(5.0, v))
    except Exception as e:
        log(f"打分失败（按 3 分算）：{e}")
        return 3.0


def write_score(room: int, score: float, ctx: str, reply: str) -> None:
    """分数落盘：jsonl 给程序看，md 给人看。"""
    try:
        with (LOGDIR / f"打分-{room}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "s": round(score, 1),
                                "ctx": ctx[-300:], "x": reply}, ensure_ascii=False) + "\n")
        md = LOGDIR / f"打分-{room}.md"
        new = not md.exists()
        with md.open("a", encoding="utf-8") as f:
            if new:
                f.write(f"# 像人度打分 · {room}\n\n> 时间 / 分数 / 当时语境 / 那句话。1=像机器人，5=像真人。\n\n")
            f.write(f"- {time.strftime('%H:%M:%S')} **{score:.1f}/5** ｜ {reply}\n  - 语境：{ctx[-160:]}\n")
    except Exception as e:
        log(f"写打分失败：{e}")

async def learn_once(key: str, room: int, q: str, msgs: list[str]) -> bool:
    """问句是否被回答；被回答就落进知识库，返回 True。"""
    payload = {
        "model": S.fish.flash_model,
        "messages": [{"role": "user", "content": LEARN_PROMPT.format(q=q, msgs="\n".join(msgs[-14:]))}],
        "max_tokens": 60,
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            data = await r.json()
    out = (data["choices"][0]["message"]["content"] or "").strip()
    if not out or out.upper().startswith("SKIP"):
        return False
    a = out.split("答：", 1)[1].strip() if "答：" in out else out
    a = a.strip("。 ")
    if not (2 <= len(a) <= 60):
        return False
    save_kb(room, q, a)
    log(f"学会了：{q} → {a}")
    return True


# ── 读画面 ───────────────────────────────────────────────────────────────
# 从直播流抽一帧，走 stdout 进内存，再 base64 塞给多模态接口 —— 不落盘。


async def grab_frame_jpeg(room_id: int) -> bytes:
    """抽当前直播画面一帧，返回 jpeg 字节（失败返回 b''）。"""
    return await asyncio.to_thread(bili.grab_frame_jpeg, room_id)


async def room_is_live(room_id: int) -> bool:
    """这个房间现在是不是在直播。查不到就当还在播。"""
    return await asyncio.to_thread(bili.is_live, room_id)


async def summarize_scene(key: str, asr_tail: list[str], dm_tail: list[str], frame: bytes) -> str:
    """看一眼画面 + 最近的语音/弹幕，用一句话概括现在的场面。"""
    head = "\n".join(asr_tail[-6:])
    chat = " | ".join(dm_tail[-8:])
    b64 = base64.b64encode(frame).decode()
    body = {
        "model": S.fish.model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (
                "这是 B站 直播间的画面和现场信息。用一句话（30 字内）概括：主播在做什么、在聊什么、"
                "弹幕在起什么哄。只描述事实，不评价。\n\n"
                f"最近语音：{head}\n最近弹幕：{chat}")},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]}],
        "max_tokens": 60,
        "temperature": 0.2,
    }
    r = requests.post("https://api.deepseek.com/chat/completions",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json=body, timeout=60)
    return r.json()["choices"][0]["message"]["content"].strip().replace("\n", " ")[:60]


def other_fish_pid(room: int) -> int | None:
    """同一个房间是不是已经有一条鱼在跑了（防孤儿重复）。"""
    try:
        import psutil
    except Exception:
        return None
    me = os.getpid()
    parent = os.getppid()
    for p in psutil.process_iter(["pid", "cmdline"]):
        pid = p.info["pid"]
        if pid in (me, parent):
            continue
        cl = " ".join(p.info["cmdline"] or [])
        if ("live_deck.fish" in cl or "live_deck/fish" in cl) and f"--room {room}" in cl:
            return pid
    return None

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", type=int, required=True)
    ap.add_argument("--asr-log", required=True)
    ap.add_argument("--max-total", type=int, default=S.fish.max_total)
    ap.add_argument("--min-gap", type=int, default=S.fish.min_gap)
    ap.add_argument("--max-per-hour", type=int, default=S.fish.max_per_hour)
    ap.add_argument("--corpus", type=int, default=30, help="加载多少条真实台词做语感参考（0=关）")
    ap.add_argument("--dry", action="store_true", help="只生成不发：把要发的话打成【草稿】给人看")
    ap.add_argument("--vision", type=int, default=S.fish.vision, help="每隔多少秒抓一帧画面给模型参考（0=关）")
    ap.add_argument("--frame-ttl", type=int, default=S.fish.frame_ttl, help="发言时若画面超过这么多秒就重新抓一帧（0=不抓）")
    ap.add_argument("--mode-file", default="", help="模式开关文件：内容 live=真发，draft=只生成不发；每次循环重读")
    ap.add_argument("--delay-comp", type=float, default=S.fish.delay_comp,
                    help="扣掉人声分离(GPU)带来的额外延迟（秒）")
    ap.add_argument("--veto", type=float, default=S.fish.veto,
                    help="自我否决阈值（0–5）：像人度低于它就不发，越高越少说")
    args = ap.parse_args()
    # 硬闸：配置没开，任何命令行参数都不能让它真发
    if not S.fish.send_danmaku:
        args.dry = True

    def read_mode() -> str:
        """文件优先（界面/切换模式.cmd 改它），没有文件就按 --dry 算。"""
        if args.mode_file:
            try:
                with open(args.mode_file, "r", encoding="utf-8") as f:
                    m = f.read().strip().lower()
                if m.startswith("live"):
                    return "live"
                if m.startswith(("draft", "dry")):
                    return "draft"
            except OSError:
                pass
        return "draft" if args.dry else "live"

    global corpus_lines, danmaku_style, room_id_for_log, _LOGFILE, kb_lines
    global good_lines, bad_lines, last_reload, ctx_good, ctx_bad
    S.ensure_dirs()
    _LOGFILE = LOGDIR / f"livechat-{args.room}.out"
    room_id_for_log = args.room
    corpus_lines = load_corpus(args.corpus) if args.corpus else []
    good_lines = load_good()
    bad_lines = load_bad()
    ctx_good = load_ctx_good()
    ctx_bad = load_ctx_bad()
    if bad_lines:
        log(f"已载入 {len(bad_lines)} 条扣分说法（反面例子）")
    if good_lines:
        log(f"已载入 {len(good_lines)} 条被夸过的发言（学味道）")
    danmaku_style = load_danmaku_style()
    kb_lines = load_kb(args.room)
    global question_samples, last_q_refresh
    question_samples = load_question_samples(args.room)
    last_q_refresh = time.time()
    if question_samples:
        log(f"已载入 {len(question_samples)} 条观众问句范本")
    if kb_lines:
        log(f"已载入 {len(kb_lines)} 条这间房的知识（问过的词/梗）")
    global signal_lines
    signal_lines = load_signals(args.room)
    if signal_lines:
        log(f"已载入 {len(signal_lines)} 条暗号示范句")
    if danmaku_style:
        log(f"弹幕习惯：{danmaku_style}")
    if corpus_lines:
        log(f"语料已加载 {len(corpus_lines)} 条（参考句式，不照抄）")

    dup = other_fish_pid(args.room)
    if dup:
        log(f"房间里已经有一条鱼在跑（pid={dup}），这条不重复启动，直接退出")
        return
    key = bili.deepseek_key()
    asr_log = Path(args.asr_log)

    budget = GiftBudget(S.gift_ledger_file, S.fish.gift_daily_budget)
    danmaku_buf: deque[str] = deque(maxlen=40)
    room = live.LiveRoom(room_display_id=args.room, credential=get_cred())
    # 带凭据连接：匿名连接不会被算作在线观众
    dm = live.LiveDanmaku(args.room, credential=get_cred(), max_retry=5)

    # 主动"进房"：让直播间把这条连接算进在线/高能榜
    async def _enter_room() -> None:
        cred = get_cred()
        try:
            async with aiohttp.ClientSession(cookies={
                "SESSDATA": cred.sessdata,
                "bili_jct": cred.bili_jct,
                "buvid3": cred.buvid3,
            }) as s:
                await s.post(
                    "https://api.live.bilibili.com/xlive/web-room/v1/index/roomEntryAction",
                    data={"room_id": args.room, "platform": "web", "csrf": cred.bili_jct},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://live.bilibili.com/{args.room}"},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            log("已发送进房请求")
        except Exception as e:
            log(f"进房请求失败: {e}")

    asyncio.create_task(_enter_room())

    # 主播 uid：用来识别"主播在跟我说话"
    anchor_uid = ""
    try:
        _info = await room.get_room_info()
        anchor_uid = str(_info.get("room_info", {}).get("uid", ""))
        log(f"主播 uid = {anchor_uid}")
    except Exception as e:
        log(f"取主播 uid 失败: {e}")

    addressed_at = 0.0
    called_at = 0.0
    last_gift_at = 0.0
    pending_frame: bytes | None = None
    frame_auto = False
    scene_note = ""
    last_frame = 0.0
    last_scene = 0.0

    @dm.on("DANMU_MSG")
    async def _on_dm(event):
        nonlocal addressed_at, last_gift_at, pending_frame, called_at
        info = event.get("data", {}).get("info", [])
        try:
            text = str(info[1])
            uid = str(info[2][0])
            harvest_emoticon(info)
            if uid == str(BOT_UID):
                return  # 自己发的弹幕不参与判定，免得被自己的「鱼，」叫醒
            # 「。」开头 = 使用者的暗号：当成示范句学习，不当作普通弹幕回
            if text and text[0] == "。":
                body = text[1:].strip()
                if body:
                    signal_lines.append(body)
                    log(f"暗号（示范句）：{body}")
                    try:
                        DM_LOG_DIR.mkdir(parents=True, exist_ok=True)
                        with (DM_LOG_DIR / f"暗号-{room_id_for_log}.jsonl").open("a", encoding="utf-8") as f:
                            f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "u": info[2][1], "x": body},
                                               ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                return

            danmaku_buf.append(f"{info[2][1]}: {text}")
            log(f"弹幕 {info[2][1]}: {text}")
            try:
                DM_LOG_DIR.mkdir(parents=True, exist_ok=True)
                with (DM_LOG_DIR / f"{room_id_for_log}.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "u": info[2][1], "x": text}, ensure_ascii=False) + "\n")
            except Exception:
                pass
            if anchor_uid and uid == anchor_uid and ("大肥鱼" in text or "肥鱼" in text or "这条鱼" in text):
                addressed_at = time.time()
                log(f"主播点名了：{text}")
            # 「鱼，」开头 —— 谁发都算在叫它（2026-10-20：不再只盯主人）。
            if CALL_RE.match(text):
                called_at = time.time()
                log(f"有人叫鱼（{info[2][1]}，uid={uid}）：{text}")
            # 主人让鱼去查东西：直接搜，下一条回复就能用上
            if uid == OWNER_UID:
                mm = re.search(r"搜(?:索)?\s*[「“\"'‘]?\s*([^」”\"'’\s，,。！!？?]{1,16})", text)
                if mm:
                    term = mm.group(1).strip()

                    async def _search_now(t: str = term) -> None:
                        s = await do_search(key, args.room, t)
                        if s:
                            LAST_SEARCH.update(term=t, summary=s, ts=time.time())

                    asyncio.create_task(_search_now())
                    log(f"主人让我查：{term}")
            # 主人在弹幕里点单送礼。
            # 2026-10-20 修：以前只要听到「人气票」就发 gift_id=31164，但 B站 上
            # 31164 其实是「粉丝团灯牌」（100 金瓜子）—— 主人要人气票，发出去的是灯牌。
            # 真正的人气票：33987 是背包免费票（silver/bag_gift=1，要走背包接口），
            # 33988 是能直接买的人气票（gold，100 金瓜子，和灯牌同价）。这里用 33988。
            # 【强制】送礼只认主人（uid == OWNER_UID）：全文件只有这一处调 send_ticket，
            # 别人在弹幕里怎么点单都进不来这里 —— 这是代码层的硬闸，不依赖模型自觉。
            # 只有「鱼，……」这样点单才算（2026-10-22 用户要求）：以前随便说个「灯牌」就触发。
            if OWNER_UID and uid == OWNER_UID and CALL_RE.match(text):
                want = None
                log(f"[点单] 收到：{text}")
                if "粉丝牌" in text or "灯牌" in text or "牌子" in text:
                    want = ("粉丝团灯牌", 31164, 100)
                elif "人气票" in text or "送个票" in text:
                    want = ("人气票", 33988, 100)
                log(f"[点单] 解析结果：{want}")
                if want and not S.fish.allow_gift:
                    log("送礼总闸没开（fish.allow_gift=false），这次只当没听见")
                    want = None
                if want and not budget.can_spend(want[2]):
                    log(f"今日礼物预算不够（上限 {S.fish.gift_daily_budget} 金瓜子），跳过")
                    want = None
                if want:
                    name, gid, price = want
                    if time.time() - last_gift_at > 60:
                        last_gift_at = time.time()
                        res = await send_ticket(args.room, int(anchor_uid or 0), gift_id=gid, price=price)
                        code = res.get("code")
                        log(f"主人要{name} → gift_id={gid} code={code} {res.get('message')}")
                        if code not in (0, "0", None):
                            log(f"⚠️ {name} 没送出去：{res.get('message')}")
                        else:
                            budget.record(price)
                    else:
                        log(f"主人又要{name}，但 60 秒内已送过，跳过")

            # 主人让看画面：从直播流抽一帧，塞给下一条回复（2026-10-20 加）
            if uid == OWNER_UID and any(
                k in text
                for k in (
                    # 简繁两套都要认：主人常打繁体（「你能讀畫面嗎」），只写简体时抽帧不会触发
                    "看画面", "看畫面", "读画面", "讀畫面", "画面里", "畫面裡", "画面中", "畫面中",
                    "画面有", "畫面有", "看看画面", "看看畫面", "读图", "讀圖", "看屏幕",
                    "看直播", "看到啥", "看到什么", "看到什麼",
                )
            ):
                log("主人要看画面，抽一帧…")
                try:
                    jpg = await grab_frame_jpeg(args.room)
                    if jpg:
                        pending_frame = jpg
                        log(f"抽帧成功：{len(jpg) // 1024} KB，下一条回复带上")
                    else:
                        log("抽帧失败：ffmpeg 没吐出画面")
                except Exception as e:
                    log(f"抽帧异常：{e}")
        except Exception:
            pass

    listener = asyncio.create_task(dm.connect())

    async def learn_loop() -> None:
        """问出去的话，等弹幕/主播来答；两分钟内每 15 秒看一眼，答了就记进知识库。"""
        while True:
            await asyncio.sleep(15)
            q = PENDING["q"]
            if not q or time.time() - PENDING["ts"] < 15:
                continue
            if PENDING["tries"] >= 8:  # 两分钟没人答，就算了
                PENDING["q"] = ""
                continue
            PENDING["tries"] += 1
            try:
                tail = [line.strip() for line in asr_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-8:]]
            except Exception:
                tail = []
            msgs = list(danmaku_buf)[-10:] + tail
            if not msgs:
                continue
            try:
                done = await learn_once(key, args.room, q, msgs)
            except Exception as e:
                log(f"学疑问失败：{e}")
                continue
            if done:
                PENDING["q"] = ""

    learn_task = asyncio.create_task(learn_loop())
    log(f"蹲房间 {args.room}：最多 {args.max_total} 条，间隔≥{args.min_gap}s，每小时≤{args.max_per_hour}")
    log("版本：观众提问版（问句学习 + 知识库 + 不重复问）")
    last_mode = read_mode()
    log(f"当前模式：{'草稿（只生成不发）' if last_mode == 'draft' else '公开（真发弹幕）'}"
        + (f"  开关文件 {args.mode_file}" if args.mode_file else ""))

    sent_total = 0
    sent_times: list[float] = []
    sent_texts: deque[str] = deque(maxlen=12)
    last_send = 0.0
    next_ok_at = 0.0
    last_dm_at = time.time()
    last_voice_at = time.time()
    last_dm_seen = ""
    last_voice_seen = ""
    last_sig = None
    last_asr_seen = ""
    last_live_check = time.time()
    offline_streak = 0
    last_q_at = 0.0

    try:
        while sent_total < args.max_total:
            await asyncio.sleep(10)
            now = time.time()

            m_now = read_mode()
            if m_now != last_mode:
                last_mode = m_now
                log(f"模式切换：{'草稿（只生成不发）' if m_now == 'draft' else '公开（从现在起真发弹幕）'}")

            # 界面上点 ✔/✖ 会写进样本文件，这边每 5 分钟重读一次，不用重启
            if now - last_reload > 300:
                last_reload = now
                g2, b2 = load_good(), load_bad()
                if g2 != good_lines or b2 != bad_lines:
                    good_lines, bad_lines = g2, b2
                    log(f"重新加载样本：加分 {len(g2)} 条 / 扣分 {len(b2)} 条")
                cg, cb = load_ctx_good(), load_ctx_bad()
                if cg != ctx_good or cb != ctx_bad:
                    ctx_good, ctx_bad = cg, cb
                    log(f"重新加载语境样本：对 {len(cg)} 条 / 不对 {len(cb)} 条")
            # 每 5 分钟重新抓一遍观众最近的问法，让说话方式跟着房间走
            if now - last_q_refresh > 300:
                last_q_refresh = now
                fresh = load_question_samples(args.room)
                if fresh and fresh != question_samples:
                    question_samples = fresh
                    log(f"更新观众问句范本：{len(fresh)} 条")

            # 直播结束了就下班，别赖在已经关掉的房间里（2026-10-21）：
            # 每 90 秒查一次，连续两次确认不在播才走（单次查询失败/抖动不算）。
            if now - last_live_check >= 90:
                last_live_check = now
                if await room_is_live(args.room):
                    offline_streak = 0
                else:
                    offline_streak += 1
                    log(f"房间 {args.room} 不在直播中（第 {offline_streak} 次确认）")
                    if offline_streak >= 2:
                        log("直播结束了，下班。剩下的交给刷B站那边")
                        break

            hourly = [t for t in sent_times if now - t < 3600]
            just_addressed = (now - addressed_at) < 90
            just_called = (now - called_at) < 90
            forced = just_addressed or just_called
            if len(hourly) >= args.max_per_hour:
                continue
            # 冷场：两分多钟没弹幕、也没新字幕
            quiet = (now - last_dm_at > 120) and (now - last_voice_at > 120)
            if not forced and not quiet:
                if now < next_ok_at:
                    continue
                # 随机「这会儿懒得说」——被点名或冷场时不触发
                if random.random() < 0.6:
                    continue

            asr_tail = []
            try:
                with asr_log.open("r", encoding="utf-8", errors="ignore") as f:
                    asr_tail = [line.strip() for line in f.readlines()[-10:] if line.strip() and line.startswith("[")]
            except Exception:
                pass

            dm_tail = list(danmaku_buf)[-10:]
            if dm_tail and dm_tail[-1] != last_dm_seen:
                last_dm_seen = dm_tail[-1]
                last_dm_at = now
            if asr_tail and asr_tail[-1] != last_voice_seen:
                last_voice_seen = asr_tail[-1]
                last_voice_at = now
            if not asr_tail and not dm_tail:
                log("暂无上下文，继续等")
                continue

            # 主播口头上叫你（ASR 里出现名字）也算点名：只看最新一行，避免重复触发
            if asr_tail:
                latest = asr_tail[-1]
                if latest != last_asr_seen:
                    last_asr_seen = latest
                    if "大肥鱼" in latest or "肥鱼" in latest or "这条鱼" in latest:
                        addressed_at = time.time()
                        log(f"主播口头上点名：{latest}")

            sig = (asr_tail[-1] if asr_tail else "") + "|" + (dm_tail[-1] if dm_tail else "")
            if sig == last_sig:
                continue
            last_sig = sig

            context = "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n" + "\n".join(asr_tail[-8:])
            if scene_note:
                context = f"【现在的场面（你刚看了一眼，别复述画面本身）】{scene_note}\n\n" + context
            context += "\n\n最近弹幕：\n" + "\n".join(dm_tail[-8:])
            if quiet:
                context += "\n\n【直播间冷场了（两分多钟没人说话）—— 你可以主动起个话头，短、具体】"
            if just_called and not just_addressed:
                context = ("【弹幕里有人用「鱼，」叫你（谁发都算，不限于主人）—— 这条是给你的。"
                           "回不回你自己看心情：想接就接一句，不想理就直接输出 SKIP】\n\n") + context
            if just_addressed:
                context = "【主播刚点名跟你说话，这一条必须回，不许 SKIP】\n\n" + context
            # 已经好一阵没提问了 → 给个轻提醒（别硬问，没有值得问的就正常接话）
            if sent_total >= 6 and time.time() - last_q_at > 600:
                context += ("\n\n【你有一阵没提问了。如果这会儿确实有看不懂的东西，可以问一句；"
                            "没有就正常接梗，别硬问】")
            if args.frame_ttl and pending_frame is None and (time.time() - last_frame) >= args.frame_ttl:
                try:
                    fr = await grab_frame_jpeg(args.room)
                    if fr:
                        pending_frame = fr
                        last_frame = time.time()
                        frame_auto = True
                except Exception as e:
                    log(f"抓画面失败：{e}")

            if args.vision and (time.time() - last_scene) >= args.vision:
                try:
                    fr = await grab_frame_jpeg(args.room)
                    if fr:
                        scene_note = await summarize_scene(key, asr_tail, dm_tail, fr)
                        last_scene = time.time()
                        log(f"场面：{scene_note}")
                except Exception as e:
                    log(f"看画面失败：{e}")
            if pending_frame is not None:
                if frame_auto:
                    context = ("【这是你此刻看到的直播画面 —— 这条回复必须和它有关："
                               "画面在打游戏就聊游戏里正在发生的事，在聊天就接住话题。"
                               "别答非所问，也别只描述画面本身】\n\n") + context
                else:
                    context = ("【主人刚让你看直播画面，下面这张就是现在的画面 —— 直接说画面里有什么，"
                               "≤30 字，别描述右侧弹幕栏，别 SKIP】\n\n") + context

            try:
                reply = await ask_model(key, context, pending_frame)
            except Exception as e:
                log(f"模型调用失败: {e}")
                continue
            finally:
                # 帧只看一次：这张图已经过期了，别拿它喂下一条
                pending_frame = None
                frame_auto = False

            if not reply or reply.upper().startswith("SKIP"):
                log("跳过（没话可接）")
                continue

            # 「鱼，」是观众叫她的暗号，不是她的口癖（用户 2026-10-21 明确要求）：
            # 提示词里已经禁止，这里再兜一道，模型学样也会被剥掉。
            reply = reply.replace("\n", " ").strip().strip('"').strip("'")
            # 她自己决定要查资料：输出 [查:词] 就先去搜，不自说自话
            m_search = SEARCH_RE.match(reply)
            if m_search:
                term = m_search.group(1).strip()
                now_ts = time.time()
                if now_ts - SEARCH_BUDGET["ts"] > 3600:
                    SEARCH_BUDGET["n"] = 0
                    SEARCH_BUDGET["ts"] = now_ts
                if SEARCH_BUDGET["n"] < 8:
                    SEARCH_BUDGET["n"] += 1
                    log(f"她想去查：{term}")
                    summary = await do_search(key, args.room, term)
                    if summary:
                        LAST_SEARCH.update(term=term, summary=summary, ts=time.time())
                else:
                    log("这小时查得太多了，这次不让查")
                continue

            # 「投喂」演练：格式化成一条普通弹幕，不调真实送礼接口
            is_feed = False
            m_feed = FEED_RE.match(reply)
            if m_feed:
                gname = m_feed.group(1).strip()
                gnum = max(1, min(9, int(m_feed.group(2) or 1)))
                reply = f"投喂 {gname} x{gnum}"
                is_feed = True
                log(f"（演练）她投喂了：{gname} x{gnum}")

            reply = strip_self_call(reply)[:40]
            reply = strip_self_call(reply)[:40]

            # 她自己在问「X 是啥」→ 顺手替她查，下一条就能用上（真人就是这样）
            mq = QUESTION_TERM.search(reply)
            if mq:
                term = mq.group(1).strip()
                known = any(term in line for line in kb_lines)
                if not known and time.time() - SEARCHED.get(term, 0) > 1800:
                    SEARCHED[term] = time.time()

                    async def _bg(t: str = term) -> None:
                        s = await do_search(key, args.room, t)
                        if s:
                            LAST_SEARCH.update(term=t, summary=s, ts=time.time())

                    asyncio.create_task(_bg())
                    log(f"她问了「{term}」，顺手查一下")
            Q_FLAGS.append(looks_like_question(reply))
            # 只发表情也是选项：模型输出 [表情] 就从采集到的目录里挑一个发（2026-10-21）
            want_emote = reply.strip() in ("[表情]", "[表情包]", "[sticker]")
            if not want_emote and any(_similar(reply, old) for old in sent_texts):
                log(f"去重跳过：{reply}")
                continue
            if not want_emote and BANNED.search(reply):
                log(f"拦截（含指控词）：{reply}")
                continue
            # 复述主播刚说的话 = 一眼机器人（2026-10-21 用户点名的扣分项）
            if not want_emote and asr_tail and echoes(reply, asr_tail[-1], 7):
                log(f"复述太像，跳过：{reply}")
                continue

            # 像人度：就这一刻的语境，这句话像不像真人说的（用户 2026-10-21 要求）
            ctx = " / ".join([x for x in [scene_note] if x])
            ctx += "\n主播：" + " / ".join(asr_tail[-2:])
            ctx += "\n弹幕：" + " / ".join(dm_tail[-4:])
            score = 5.0 if is_feed else await judge_human(key, ctx, reply)
            write_score(args.room, score, ctx, reply)
            if score < args.veto:
                log(f"自我否决（{score:.1f}/5 < {args.veto:.1f}），不发：{reply}")
                continue
            # 发弹幕的等待：原设定 3–8 秒（期望 5.5s）。
            # 开了人声分离后，识别链路本身会多出 GPU 处理时间 ⇒ 从这边扣掉同样的量，
            # 使「等待 + GPU处理」的期望仍等于原来的 5.5 秒（用户 2026-10-21 要求）。
            delay = random.uniform(3, 8) - max(0.0, float(args.delay_comp))
            delay = max(0.3, delay)
            log(f"准备发：[{score:.0f}/5] {'[表情]' if want_emote else reply}（{delay:.1f}s 后）")
            await asyncio.sleep(delay)
            try:
                if want_emote:
                    if not emote_catalog:
                        log("没有可用表情（这间房还没出现过表情弹幕），这条跳过")
                        continue
                    entry = random.choice(list(emote_catalog.values()))
                    if read_mode() == "draft" or not S.fish.send_danmaku:
                        log(f"【草稿】表情 {entry.get('text')}")
                        continue
                    res = await send_emoticon(args.room, entry)
                    code = res.get("code") if isinstance(res, dict) else None
                    if code not in (0, "0", None):
                        log(f"表情发送失败：code={code} {res.get('message') if isinstance(res, dict) else ''}")
                        continue
                    log(f"已发 #{sent_total + 1}: 表情 {entry.get('text')}")
                elif read_mode() == "draft" or not S.fish.send_danmaku:
                    log(f"【草稿】[{score:.0f}/5] {reply}")
                else:
                    res = await room.send_danmaku(Danmaku(text=reply, color="ffffff"))
                    code = res.get("code") if isinstance(res, dict) else None
                    if code not in (0, "0", None):
                        # 以前这里不校验返回值：B站 拒了也照样打「已发」
                        log(f"被 B站 拒了 code={code}：{reply}")
                        continue
                    log(f"已发 #{sent_total + 1}: [{score:.0f}/5] {reply}")
                    # 真发出去的问句才等答案（草稿没人回，学了也白学）
                    if looks_like_question(reply):
                        PENDING.update(q=reply, ts=time.time(), tries=0)
                        RECENT_ASKS.append(reply)
                        last_q_at = time.time()
                        log(f"记下疑问：{reply}")
                sent_total += 1
                last_send = time.time()
                next_ok_at = last_send + random.uniform(max(45, args.min_gap * 0.6), args.min_gap * 3.5)
                sent_times.append(last_send)
                sent_texts.append(reply)
            except Exception as e:
                log(f"发送失败: {e}")
    finally:
        try:
            await dm.disconnect()
        except Exception:
            pass
        listener.cancel()
        learn_task.cancel()
        log(f"结束，共发 {sent_total} 条")


if __name__ == "__main__":
    asyncio.run(main())
