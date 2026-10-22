"""直播互动台 —— 一个房间，四条数据流。

只读是默认状态：只收弹幕、跑字幕、看画面。发弹幕和送礼各有一道显式总闸
（``fish.send_danmaku`` / ``fish.allow_gift``），默认都关着。

启动::

    live-deck serve --room 123456 --open
    python -m live_deck.deck.server --room 123456
    python -m live_deck.deck.server --demo      # 不连 B站，看界面
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
import webbrowser
from collections import deque
from pathlib import Path

from aiohttp import web
from bilibili_api import live as blive

from .. import bili, demo
from ..config import get_settings

S = get_settings()
APP = Path(__file__).parent
WEB = APP / "web"
ASR_LOG_DIR = S.logs_dir
CREATE_NO_WINDOW = bili.CREATE_NO_WINDOW
WALLET_CACHE: dict = {"ts": 0.0, "data": {}}
# 「鱼，」这种点名：只认逗号，和直播鱼保持同一套规则
CALL_RE = re.compile(rf"^\s*{re.escape(S.fish.call_word)}\s*[，,]")
BOT_UID = bili.bot_uid()


def child_env() -> dict[str, str]:
    """子进程环境：UTF-8 输出 + 把 src/ 放进 PYTHONPATH（没装包也能跑）。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    src = str(S.root / "src")
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if src not in parts:
        env["PYTHONPATH"] = os.pathsep.join([src, *parts])
    return env


async def get_wallet() -> dict:
    """直播钱包：100 金瓜子 = 1 电池；查不到不致命。"""
    if time.time() - WALLET_CACHE["ts"] < 60 and WALLET_CACHE["data"]:
        return WALLET_CACHE["data"]
    try:
        data = await asyncio.to_thread(bili.wallet)
    except Exception as e:
        data = {"ok": False, "error": str(e)[:120]}
    WALLET_CACHE.update(ts=time.time(), data=data)
    return data

GOOD_FILE = S.good_samples
BAD_FILE = S.bad_samples
# 语境轴单独两个文件（这个场面该不该接）
CTX_GOOD_FILE = S.ctx_good_samples
CTX_BAD_FILE = S.ctx_bad_samples
MODE_FILE = S.mode_file
MODE_LABEL = {"draft": "草稿", "live": "公开"}


def read_mode() -> str:
    try:
        m = MODE_FILE.read_text(encoding="utf-8").strip().lower()
        if m.startswith("live"):
            return "live"
        if m.startswith(("draft", "dry")):
            return "draft"
    except OSError:
        pass
    return "draft"


def write_mode(mode: str) -> None:
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text("live\n" if mode == "live" else "draft\n", encoding="utf-8")
ASR_LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(.+)$")
# live_asr.py 的启动横幅也会带时间戳，别当成一句人话
ASR_NOISE = ("模型已加载", "开始识别", "流类型")
# v2 的启动/收尾横幅同理会混进来
ASR_BANNER = re.compile(r"^(room \d+ ->|SenseVoice \+ Demucs 就绪|结束$)")
# 只有标点的行（模型在静音上吐个「。」）不算字幕
ASR_PUNCT = re.compile(r"^[\s。，、？！…—\-.,!?~～:：;；\"'“”‘’（）()\[\]【】]*$")


def asr_is_speech(text: str) -> bool:
    """带时间戳的行里，哪些真的是人话。"""
    if not text or ASR_PUNCT.match(text):
        return False
    return not any(n in text for n in ASR_NOISE) and not ASR_BANNER.match(text)

# live_chat.py 的三行格式
FISH_PREVIEW = re.compile(r"^(\d{2}:\d{2}:\d{2}) 准备发[:：]\s*(.+?)\s*[（(][\d.]+s 后[）)]\s*$")
FISH_DRAFT = re.compile(r"^(\d{2}:\d{2}:\d{2}) 【草稿】\s*(.+)$")
FISH_SENT = re.compile(r"^(\d{2}:\d{2}:\d{2}) 已发 #(\d+)[:：]\s*(.+)$")


def parse_fish_line(line: str) -> dict | None:
    line = line.rstrip()
    m = FISH_PREVIEW.match(line)
    if m:
        return {"t": "fish", "kind": "preview", "time": m.group(1), "text": m.group(2), "ts": now()}
    m = FISH_DRAFT.match(line)
    if m:
        return {"t": "fish", "kind": "draft", "time": m.group(1), "text": m.group(2), "ts": now()}
    m = FISH_SENT.match(line)
    if m:
        return {
            "t": "fish",
            "kind": "sent",
            "time": m.group(1),
            "n": int(m.group(2)),
            "text": m.group(3),
            "ts": now(),
        }
    return None


def now() -> float:
    return time.time()


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ── 广播 ──────────────────────────────────────────────────────────────────────


class Hub:
    def __init__(self) -> None:
        self.clients: set[web.WebSocketResponse] = set()
        # 最近事件回放：界面晚打开也能立刻看到刚才的弹幕/字幕/画面
        self.buffer: deque[dict] = deque(maxlen=160)

    async def push(self, ev: dict) -> None:
        self.buffer.append(ev)
        if not self.clients:
            return
        data = json.dumps(ev, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:
                self.clients.discard(ws)

    def backlog(self) -> list[dict]:
        """按类型限量回放，避免一进来被 160 条弹幕淹没。"""
        keep: list[dict] = []
        caps = {"scene": 3, "asr": 40, "danmaku": 60, "fish": 12}
        counts: dict[str, int] = {}
        for ev in reversed(self.buffer):
            t = ev.get("t")
            cap = caps.get(t or "", 0)
            if not cap:
                continue
            n = counts.get(t, 0)
            if n >= cap:
                continue
            counts[t] = n + 1
            keep.append(ev)
        keep.reverse()
        return keep


# ── 画面识别 ──────────────────────────────────────────────────────────────────

SCENE_PROMPT = (
    "这是 B站 直播间的实时画面。用一句中文（30 字以内、不加引号）说清楚："
    "主播此刻在做什么、画面里最值得注意的是什么。看不出就直说。"
)


def ask_scene(key: str, jpeg: bytes) -> str:
    """一帧 + 一句话。无状态：不带历史，成本可控。"""
    import requests

    b64 = base64.b64encode(jpeg).decode()
    payload = {
        "model": "deepseek-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SCENE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            "detail": "low",
                        },
                    },
                ],
            }
        ],
        "max_tokens": 80,
        "temperature": 0.6,
        "thinking": {"type": "disabled"},
    }
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


FIX_PROMPT = (
    "你是直播字幕校对。下面这句是语音识别原文，可能有同音错字、漏字、断句错误。\n"
    "只允许：改同音/近音错字、去掉明显重复的词、补标点。\n"
    "不允许：增加原文没有的内容、改写意思、解释、加引号。\n"
    "拿不准就原样保留。只输出这一句。\n\n原文："
)


def ai_fix_text(key: str, text: str) -> str:
    """让 DeepSeek 只做同音纠错。失败或改得太离谱就返回原文。"""
    import requests

    payload = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": FIX_PROMPT + text}],
        "max_tokens": 200,
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    out = (r.json()["choices"][0]["message"]["content"] or "").strip().strip('"“”')
    # 防跑偏：空的、解释性的、或者长度翻倍的一律不认
    if not out or len(out) > max(24, len(text) * 2):
        return text
    return out


# ── 房间 ──────────────────────────────────────────────────────────────────────


class Room:
    def __init__(
        self,
        hub: Hub,
        room_no: int,
        key: str,
        frame_interval: int = 60,
        asr_cmd: tuple[str, ...] | None = None,
        ai_fix: bool = True,
        fish_enabled: bool = True,
    ) -> None:
        self.hub = hub
        self.asr_cmd = asr_cmd or build_asr_cmd(S.deck.asr_engine)
        self.room_no = int(room_no)
        self.room_id = self.room_no
        self.key = key
        self.frame_interval = frame_interval
        self.title = ""
        self.uname = ""
        self.live = False
        self.started = now()
        self.closed = False
        self.asr_ok = False
        self.last_asr = ""
        # 精修通道：fp32 + 长上下文的句子从队列进来，AI 校正后写给直播鱼
        self.clean_q: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.last_clean_ts = 0.0
        self.ai_fix = ai_fix
        self.dm_ok = False
        self.tasks: list[asyncio.Task] = []
        self.asr_proc: asyncio.subprocess.Process | None = None
        self.dm = None
        # 直播鱼也由本程序托管：一个窗口 = 一个程序，不再有额外的黑框
        self.fish_enabled = fish_enabled
        self.fish_ok = False
        self.fish_proc: asyncio.subprocess.Process | None = None

    # 快照
    def state(self) -> dict:
        return {
            "room": self.room_no,
            "room_id": self.room_id,
            "title": self.title,
            "uname": self.uname,
            "live": self.live,
            "asr": self.asr_ok,
            "danmaku": self.dm_ok,
            "fish": self.fish_ok,
            "mode": read_mode(),
            "since": self.started,
            "frame_interval": self.frame_interval,
        }

    async def open(self) -> None:
        room = blive.LiveRoom(room_display_id=self.room_no, credential=bili.load_credential())
        try:
            info = await room.get_room_info()
            self.room_id = int(info.get("room_info", {}).get("room_id") or self.room_no)
            self.title = info.get("room_info", {}).get("title") or ""
            self.live = int(info.get("room_info", {}).get("live_status") or 0) == 1
            self.uname = info.get("anchor_info", {}).get("base_info", {}).get("uname") or ""
        except Exception as e:
            log(f"取房间信息失败（照常继续）：{e}")

        stale = kill_stale_children(self.room_no)
        if stale:
            log(f"清掉 {stale} 个残留子进程（上次重启留下的孤儿）")
            await asyncio.sleep(1)
        await self.hub.push({"t": "room", **self.state()})
        log(f"进入 {self.room_no} -> {self.room_id} 「{self.title}」 主播 {self.uname} live={self.live}")

        self.tasks = [
            asyncio.create_task(self._danmaku_loop(), name="danmaku"),
            asyncio.create_task(self._asr_loop(), name="asr"),
            asyncio.create_task(self._clean_worker(), name="clean"),
            asyncio.create_task(self._frame_loop(), name="frame"),
            asyncio.create_task(self._fish_log_loop(), name="fishlog"),
            asyncio.create_task(self._fish_proc_loop(), name="fish"),
        ]

    async def close(self) -> None:
        self.closed = True
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self.asr_proc and self.asr_proc.returncode is None:
            try:
                self.asr_proc.kill()
            except Exception:
                pass
        if self.fish_proc and self.fish_proc.returncode is None:
            try:
                self.fish_proc.kill()
            except Exception:
                pass
        if self.dm:
            try:
                await self.dm.disconnect()
            except Exception:
                pass
        log(f"离开 {self.room_no}")

    # 弹幕
    async def _danmaku_loop(self) -> None:
        while not self.closed:
            try:
                dm = blive.LiveDanmaku(self.room_no, credential=bili.load_credential(), max_retry=5)

                @dm.on("DANMU_MSG")
                async def _on_dm(event):
                    info = (event.get("data") or {}).get("info") or []
                    try:
                        text = str(info[1])
                        uid = str(info[2][0])
                        uname = str(info[2][1])
                    except Exception:
                        return
                    if uid == str(BOT_UID):
                        return
                    ev = {
                        "t": "danmaku",
                        "uid": uid,
                        "user": uname,
                        "text": text,
                        "toFish": bool(CALL_RE.match(text)),
                        "ts": now(),
                    }
                    await self.hub.push(ev)

                @dm.on("INTERACT_WORD")
                async def _on_enter(event):
                    d = event.get("data") or {}
                    try:
                        uname = (d.get("uname") or "").strip()
                    except Exception:
                        return
                    if uname:
                        await self.hub.push(
                            {"t": "enter", "user": uname, "ts": now()}
                        )

                self.dm = dm
                self.dm_ok = True
                await self.hub.push({"t": "status", **self.state()})
                await dm.connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.dm_ok = False
                log(f"弹幕断了：{e}")
                await self.hub.push({"t": "status", **self.state()})
            if not self.closed:
                await asyncio.sleep(5)

    # 字幕
    def _mirror_asr(self, stamp: str, text: str) -> None:
        """同时写一份给直播鱼读（它读 asr-live-<房间>.log 的最后 10 行）。"""
        try:
            path = ASR_LOG_DIR / f"asr-live-{self.room_no}.log"
            with path.open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {text}\n")
        except Exception:
            pass

    async def _clean_worker(self) -> None:
        """精修句 →（可选）AI 纠错 → 写给直播鱼 + 推给界面。"""
        while not self.closed:
            stamp, text = await self.clean_q.get()
            out = text
            if self.ai_fix:
                try:
                    out = await asyncio.to_thread(ai_fix_text, self.key, text)
                except Exception as e:
                    log(f"AI 纠错失败（用原文）：{e}")
            self.last_clean_ts = now()
            self._mirror_asr(stamp, out)
            await self.hub.push(
                {
                    "t": "asr",
                    "clean": True,
                    "time": stamp,
                    "text": out,
                    "raw": text if out != text else None,
                    "ts": now(),
                }
            )

    async def _asr_loop(self) -> None:
        env = child_env()
        delay = 4
        while not self.closed:
            # 下播了就别拉流：以前这里 4 秒重启一次，整夜空转 6000 多次
            if not await room_alive(self.room_no):
                self.asr_ok = False
                self.live = False
                log(f"房间没开播，暂停拉流（{delay} 秒后再看）")
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
                continue
            self.live = True
            kill_stale_children(self.room_no, {x.pid for x in (self.asr_proc, self.fish_proc) if x})
            started_at = time.time()
            proc = None
            last_err: deque[str] = deque(maxlen=6)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.asr_cmd,
                    str(self.room_no),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=str(S.root),
                    creationflags=CREATE_NO_WINDOW,
                )
                self.asr_proc = proc
                self.asr_ok = True
                await self.hub.push({"t": "status", **self.state()})
                log(f"ASR 启动 pid={proc.pid}")

                async def drain_err() -> None:
                    assert proc is not None and proc.stderr is not None
                    while True:
                        raw = await proc.stderr.readline()
                        if not raw:
                            break
                        txt = raw.decode("utf-8", "ignore").strip()
                        if txt:
                            last_err.append(txt)

                err_task = asyncio.create_task(drain_err())
                assert proc.stdout is not None
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "ignore").strip()
                    m = ASR_LINE.match(line)
                    if not m:
                        continue
                    text = m.group(2)
                    if text.startswith("#clean-error"):
                        log(f"精修失败：{text[len('#clean-error'):].strip()}")
                        continue
                    if text.startswith("#clean "):
                        # 慢而准的那一路：交给校正线程，别堵住实时字幕
                        self.clean_q.put_nowait((m.group(1), text[len("#clean "):].strip()))
                        continue
                    if asr_is_speech(text):
                        if text == self.last_asr:  # 模型在静音上复读同一句
                            continue
                        self.last_asr = text
                        # 精修通道在跑就让它写日志；它哑了超过 2 分钟才用实时句兜底
                        if now() - self.last_clean_ts > 120:
                            self._mirror_asr(m.group(1), text)
                        await self.hub.push(
                            {"t": "asr", "time": m.group(1), "text": text, "ts": now()}
                        )
                code = await proc.wait()
                err_task.cancel()
                if code != 0:
                    log(f"ASR 退出 code={code} | {' / '.join(last_err)[:300]}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"ASR 异常：{type(e).__name__}: {e}")
            finally:
                self.asr_ok = False
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if self.closed:
                break
            try:
                await self.hub.push({"t": "status", **self.state()})
            except Exception:
                pass
            # 起不到一分钟就退 = 多半没流，翻倍退避；跑够久就恢复 4 秒节奏
            if time.time() - started_at < 60:
                delay = min(60, delay * 2)
                log(f"ASR 起不到一分钟就退，{delay} 秒后再试")
            else:
                delay = 4
            await asyncio.sleep(delay)

    # 鱼的草稿：tail live_chat.py 的输出，把「准备发 / 【草稿】/ 已发」推到界面
    def _fish_path(self) -> Path:
        return ASR_LOG_DIR / f"livechat-{self.room_no}.out"

    async def _fish_log_loop(self) -> None:
        path = self._fish_path()
        # 回头读 8 KB，界面一打开就能看到最近几条草稿
        offset = max(0, path.stat().st_size - 8192) if path.exists() else 0
        last_mode = read_mode()
        while not self.closed:
            try:
                if path.exists():
                    size = path.stat().st_size
                    if size < offset:
                        offset = 0  # 被截断
                    if size > offset:
                        with path.open("rb") as f:
                            f.seek(offset)
                            raw = f.read()
                            offset = f.tell()
                        try:
                            text = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            text = raw.decode("gbk", "ignore")
                        for line in text.splitlines():
                            ev = parse_fish_line(line)
                            if ev:
                                await self.hub.push(ev)
                # 模式可能被 切换模式.cmd 改掉，这边发现变化就推给界面
                m = read_mode()
                if m != last_mode:
                    last_mode = m
                    await self.hub.push({"t": "mode", "mode": m, "ts": now()})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"读鱼日志失败：{e}")
            await asyncio.sleep(1.5)

    # 直播鱼本体：本程序托管，退出自动重启；界面里能开/关
    async def _fish_proc_loop(self) -> None:
        out_path = ASR_LOG_DIR / f"livechat-{self.room_no}.out"
        asr_log = ASR_LOG_DIR / f"asr-live-{self.room_no}.log"
        cmd = [
            sys.executable,
            "-m",
            "live_deck.fish",
            "--room", str(self.room_no),
            "--asr-log", str(asr_log),
            "--mode-file", str(MODE_FILE),
            "--vision", str(S.fish.vision),
            "--frame-ttl", str(S.fish.frame_ttl),
            "--max-total", str(S.fish.max_total),
            "--min-gap", str(S.fish.min_gap),
            "--max-per-hour", str(S.fish.max_per_hour),
            "--veto", str(S.fish.veto),
            "--delay-comp", str(S.fish.delay_comp),
        ]
        env = child_env()
        delay = 3
        while not self.closed:
            if not self.fish_enabled:
                await asyncio.sleep(1)
                continue
            # 下播了就把鱼收起来，别整夜反复「上班-下班」（昨晚 205 次）
            if not await room_alive(self.room_no):
                self.fish_ok = False
                log(f"房间没开播，直播鱼先不拉起来（{delay} 秒后再看）")
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
                continue
            kill_stale_children(self.room_no, {x.pid for x in (self.asr_proc, self.fish_proc) if x})
            started_at = time.time()
            try:
                with out_path.open("ab") as out:
                    self.fish_proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(S.root),
                        stdout=out,
                        stderr=out,
                        env=env,
                        creationflags=CREATE_NO_WINDOW,
                    )
                self.fish_ok = True
                await self.hub.push({"t": "status", **self.state()})
                log(f"直播鱼启动 pid={self.fish_proc.pid}")
                code = await self.fish_proc.wait()
                self.fish_ok = False
                if not self.closed:
                    log(f"直播鱼退出 code={code}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.fish_ok = False
                log(f"直播鱼起不来：{type(e).__name__}: {e}")
            finally:
                self.fish_proc = None
            if self.closed:
                break
            try:
                await self.hub.push({"t": "status", **self.state()})
            except Exception:
                pass
            # 3 分钟内就退 = 多半下播了，退避；跑久了再退就按正常节奏重来
            if time.time() - started_at < 180:
                delay = min(60, delay * 2)
                log(f"直播鱼起不到 3 分钟就退，{delay} 秒后再看")
            else:
                delay = 3
            await asyncio.sleep(delay)

    # 画面
    async def _frame_loop(self) -> None:
        await asyncio.sleep(8)  # 等流稳
        while not self.closed:
            # 没开播就别抓帧：不然每分钟一条「拿不到流地址」，还会触发 B 站接口
            if not self.live:
                await asyncio.sleep(max(60, self.frame_interval))
                continue
            try:
                jpeg = await asyncio.to_thread(bili.grab_frame_jpeg, self.room_id)
                if not jpeg:
                    await self.hub.push({"t": "scene", "fail": True, "desc": "抓不到画面", "ts": now()})
                else:
                    desc = await asyncio.to_thread(ask_scene, self.key, jpeg)
                    await self.hub.push(
                        {
                            "t": "scene",
                            "desc": desc,
                            "img": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                            "ts": now(),
                        }
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"画面识别失败：{e}")
                await self.hub.push({"t": "scene", "fail": True, "desc": f"识别失败：{e}"[:120], "ts": now()})
            await asyncio.sleep(max(15, self.frame_interval))


class DemoRoom:
    """演示房间：只有状态和一条脚本流，不碰 B站、不调模型。"""

    def __init__(self, hub: Hub, room_no: int) -> None:
        self.hub = hub
        self.room_no = int(room_no)
        self.room_id = self.room_no
        self.title = "演示直播间 · 只有界面是真的"
        self.uname = "DEMO"
        self.live = True
        self.started = now()
        self.closed = False
        self.asr_ok = True
        self.dm_ok = True
        self.fish_ok = True
        self.fish_enabled = True
        self.tasks: list[asyncio.Task] = []

    def state(self) -> dict:
        return {
            "room": self.room_no,
            "room_id": self.room_id,
            "title": self.title,
            "uname": self.uname,
            "live": True,
            "asr": self.asr_ok,
            "danmaku": self.dm_ok,
            "fish": self.fish_ok,
            "mode": "draft",
            "since": self.started,
            "frame_interval": 60,
            "demo": True,
        }

    async def open(self) -> None:
        await self.hub.push({"t": "room", **self.state()})
        log(f"进入演示房间 {self.room_no}")
        self.tasks = [asyncio.create_task(demo.run(self.hub), name="demo")]

    async def close(self) -> None:
        self.closed = True
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        log(f"离开演示房间 {self.room_no}")


# ── HTTP / WS ────────────────────────────────────────────────────────────────


class App:
    def __init__(
        self,
        key: str,
        frame_interval: int,
        asr_cmd: tuple[str, ...],
        ai_fix: bool = True,
        fish_enabled: bool = True,
        demo: bool = False,
    ) -> None:
        self.hub = Hub()
        self.key = key
        self.frame_interval = frame_interval
        self.asr_cmd = asr_cmd
        self.ai_fix = ai_fix
        self.fish_enabled = fish_enabled
        self.demo = demo
        self.room: Room | None = None
        self.lock = asyncio.Lock()

    async def switch(self, room_no: int | None) -> None:
        async with self.lock:
            if self.room:
                await self.room.close()
                self.room = None
            # 2026-10-22：进新直播间一律回到「草稿（不发）」—— 默认只给本人看，
            # 要不要真发由本人当场点按钮；程序启动时也走这里。
            if room_no and self.demo:
                self.room = DemoRoom(self.hub, room_no)
                await self.room.open()
                return
            if room_no:
                write_mode("draft")
                self.room = Room(
                    self.hub,
                    room_no,
                    self.key,
                    self.frame_interval,
                    self.asr_cmd,
                    self.ai_fix,
                    self.fish_enabled,
                )
                await self.room.open()

    async def state(self) -> dict:
        return {"room": self.room.state() if self.room else None, "mode": read_mode()}

    async def on_enter(self, req: web.Request) -> web.Response:
        body = await req.json()
        raw = str(body.get("room") or "").strip()
        if not raw.isdigit():
            return web.json_response({"ok": False, "error": "房间号必须是数字"}, status=400)
        if self.demo:
            return web.json_response(
                {"ok": True, "room": self.room.state() if self.room else None, "mode": "draft"}
            )
        try:
            S.last_room_file.parent.mkdir(parents=True, exist_ok=True)
            S.last_room_file.write_text(raw, encoding="utf-8")
        except Exception:
            pass
        await self.switch(int(raw))
        return web.json_response({"ok": True, "room": self.room.state() if self.room else None,
                                  "mode": read_mode()})

    async def on_leave(self, req: web.Request) -> web.Response:
        await self.switch(None)
        return web.json_response({"ok": True})

    async def on_state(self, req: web.Request) -> web.Response:
        return web.json_response(await self.state())

    async def on_wallet(self, req: web.Request) -> web.Response:
        if self.demo:
            return web.json_response({"ok": True, "battery": 12.34, "gold": 1234})
        return web.json_response(await get_wallet())

    async def on_open_url(self, req: web.Request) -> web.Response:
        body = await req.json()
        url = str(body.get("url") or "")
        # 只允许跳 B站 的充值页，别让它变成任意命令执行
        if not (url.startswith("https://pay.bilibili.com") or url.startswith("https://link.bilibili.com")):
            return web.json_response({"ok": False, "error": "只允许 B站 充值页"}, status=400)
        # 用默认浏览器打开（通常是已经登录 B站 的那个）
        try:
            webbrowser.open_new(url)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True})
    async def on_mode(self, req: web.Request) -> web.Response:
        body = await req.json()
        mode = "live" if str(body.get("mode")) == "live" else "draft"
        write_mode(mode)
        log(f"模式 -> {MODE_LABEL[mode]}")
        await self.hub.push({"t": "mode", "mode": mode, "ts": now()})
        return web.json_response({"ok": True, "mode": mode})

    async def on_fish(self, req: web.Request) -> web.Response:
        body = await req.json()
        on = bool(body.get("on"))
        if self.room:
            self.room.fish_enabled = on
            if not on and self.room.fish_proc and self.room.fish_proc.returncode is None:
                try:
                    self.room.fish_proc.kill()
                except Exception:
                    pass
            log(f"直播鱼 -> {'开' if on else '关'}")
            await self.hub.push({"t": "status", **self.room.state()})
        return web.json_response({"ok": True, "fish": on})

    async def on_rate(self, req: web.Request) -> web.Response:
        if self.demo:
            return web.json_response({"ok": True})
        body = await req.json()
        text = str(body.get("text") or "").strip()
        verdict = str(body.get("verdict") or "").strip()
        # 2026-10-22：三键制（本人定）——
        #   ok        ✔     两个都对（语境合适 + 说话像人）→ 同时进「好弹幕」和「语境自然」
        #   human_bad ✘人   说话方式不像人（主要错误）      → 进「别这么说」
        #   ctx_bad   ✘景   语境不对，这个场面不该开口      → 进「语境不对」
        # 负例分两轴、正面合一：省点击，又不让两条学习线互相污染。
        if verdict == "good":
            verdict = "ok"
        elif verdict == "bad":
            verdict = "human_bad"
        if not text or verdict not in ("ok", "human_bad", "ctx_bad"):
            return web.json_response({"ok": False, "error": "参数不对"}, status=400)
        # 语境：最近一条画面描述 + 最近几条弹幕
        scene = ""
        dms: list[str] = []
        for ev in reversed(self.hub.buffer):
            if ev.get("t") == "scene" and not scene and ev.get("desc"):
                scene = str(ev["desc"])
            elif ev.get("t") == "danmaku" and len(dms) < 3:
                dms.append(f"{ev.get('user')}: {ev.get('text')}")
            if scene and len(dms) >= 3:
                break
        ctx = (scene + " / " if scene else "") + "；".join(reversed(dms))
        paths = {
            "ok": [GOOD_FILE, CTX_GOOD_FILE],
            "human_bad": [BAD_FILE],
            "ctx_bad": [CTX_BAD_FILE],
        }[verdict]
        for path in paths:
            # 同一条弹幕可能既有「准备发」又有「已发」两行，只记一次
            try:
                if path.exists() and f"】{text}" in path.read_text(encoding="utf-8", errors="ignore"):
                    continue
            except Exception:
                pass
            try:
                with path.open("a", encoding="utf-8") as f:
                    if path.exists() and path.stat().st_size:
                        with path.open("rb") as rf:
                            rf.seek(-1, 2)
                            if rf.read(1) != b"\n":
                                f.write("\n")
                    f.write(f"- 【{ctx[:90]}】{text}\n")
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=500)
        label = {"ok": "✓ 都对", "human_bad": "✘ 不像人", "ctx_bad": "✘ 语境不对"}[verdict]
        log(f"用户打分 {label}：{text}")
        return web.json_response({"ok": True})
    async def on_quit(self, req: web.Request) -> web.Response:
        log("收到退出：关房间、杀子进程")
        if self.room:
            await self.room.close()
            self.room = None
        asyncio.get_running_loop().call_later(0.5, os._exit, 0)
        return web.json_response({"ok": True})

    async def on_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(req)
        self.hub.clients.add(ws)
        try:
            await ws.send_str(json.dumps({"t": "hello", **(await self.state())}, ensure_ascii=False))
            for ev in self.hub.backlog():
                await ws.send_str(json.dumps(ev, ensure_ascii=False))
            async for _ in ws:
                pass
        finally:
            self.hub.clients.discard(ws)
        return ws

    async def on_index(self, req: web.Request) -> web.StreamResponse:
        # 不许缓存：WebView2 会拿旧 HTML，界面改了看不到（2026-10-22 踩过）
        resp = web.FileResponse(WEB / "index.html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp


def kill_stale_children(room_no: int, keep: set[int] | None = None) -> int:
    """清掉**不属于本进程**的同房间子进程（上次重启留下的孤儿）。

    以前这里按命令行匹配就杀，会把刚拉起来的孩子也杀掉（server 日志里
    一串 code=15）。现在先看祖先进程：只要这条链上有自己，就不动。
    """
    try:
        import psutil
    except Exception:
        return 0
    me = os.getpid()
    keep = keep or set()
    killed = 0
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = proc.info["pid"]
            cl = " ".join(proc.info["cmdline"] or [])
        except Exception:
            continue
        if pid == me or pid in keep or not cl:
            continue
        if not ("live_deck.fish" in cl or "live_deck.asr" in cl):
            continue
        if str(room_no) not in cl:
            continue
        # 往上走三代，看是不是自己的孩子
        ours = False
        try:
            up = proc.parent()
            for _ in range(3):
                if up is None:
                    break
                if up.pid == me:
                    ours = True
                    break
                up = up.parent()
        except Exception:
            pass
        if ours:
            continue
        try:
            proc.kill()
            killed += 1
        except Exception:
            pass
    return killed

async def room_alive(room_no: int) -> bool:
    """房间还在不在播。查不到就当在播 —— 不能因为一次网络抖动把字幕停了。"""
    try:
        return await asyncio.to_thread(bili.is_live, room_no)
    except Exception:
        return True

def build_asr_cmd(engine: str) -> tuple[str, ...]:
    """拼出字幕子进程的命令行。

    用当前解释器（``sys.executable``）而不是写死的 venv 路径：依赖装在哪，
    子进程就在哪跑。``asr.python`` 可以覆盖（比如 GPU 版另开一个 venv）。
    """
    py = S.asr.python or sys.executable
    if engine == "streaming":
        return (
            py, "-m", "live_deck.asr.streaming",
            "--seconds", "99999",
            "--separate" if S.asr.separate else "--no-separate",
        )
    return (
        py, "-m", "live_deck.asr.sensevoice",
        "--seconds", "99999",
        "--chunk", str(S.asr.chunk_seconds),
        *(() if S.asr.clean else ("--no-clean",)),
    )


def build(
    key: str,
    frame_interval: int,
    port: int,
    asr_cmd: tuple[str, ...],
    ai_fix: bool = True,
    fish_enabled: bool = True,
    demo: bool = False,
) -> web.Application:
    a = App(key, frame_interval, asr_cmd, ai_fix, fish_enabled, demo)
    app = web.Application()
    app["core"] = a
    app.router.add_get("/", a.on_index)
    app.router.add_get("/ws", a.on_ws)
    app.router.add_get("/api/state", a.on_state)
    app.router.add_post("/api/enter", a.on_enter)
    app.router.add_post("/api/leave", a.on_leave)
    app.router.add_post("/api/mode", a.on_mode)
    app.router.add_post("/api/fish", a.on_fish)
    app.router.add_post("/api/quit", a.on_quit)
    app.router.add_post("/api/rate", a.on_rate)
    app.router.add_get("/api/wallet", a.on_wallet)
    app.router.add_post("/api/open-url", a.on_open_url)
    app.router.add_static("/static/", WEB, show_index=False)
    return app


async def serve(
    room: int = 0,
    port: int | None = None,
    asr_engine: str | None = None,
    ai_fix: bool | None = None,
    fish: bool | None = None,
    demo: bool = False,
    open_browser: bool = False,
) -> None:
    """起服务。被 CLI、desktop host 和 ``python -m`` 共用。"""
    S.ensure_dirs()
    # 用 pythonw 启动时没有控制台，sys.stdout 是 None，print 会炸
    if sys.stdout is None:
        sys.stdout = open(S.logs_dir / "server.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout

    port = port or S.deck.port
    engine = asr_engine or S.deck.asr_engine
    ai_fix = S.deck.ai_fix if ai_fix is None else ai_fix
    fish = S.deck.fish if fish is None else fish
    key = "" if demo else bili.deepseek_key(required=False)
    if not demo and not key:
        log("没有 DeepSeek key：画面描述、AI 纠错和直播鱼会不可用（其余功能照常）")
    app = build(key, S.deck.frame_interval, port, build_asr_cmd(engine), ai_fix, fish, demo)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, S.deck.host, port)
    try:
        await site.start()
    except OSError as e:
        # 多半是程序已经开着了，这次只当「再开一个窗口」
        log(f"{port} 已被占用，本次不再启动（{e}）")
        await runner.cleanup()
        return
    url = f"http://{S.deck.host}:{port}"
    log(f"直播互动台：{url}")
    log(f"字幕引擎 {engine}；AI 纠错 {'开' if ai_fix else '关'}；直播鱼 {'开' if fish else '关'}")
    if demo:
        log("演示模式：不连 B站、不调模型，四条流都是脚本喂的")

    core: App = app["core"]
    if room:
        asyncio.create_task(core.switch(room))
    if open_browser:
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    ap = argparse.ArgumentParser(description="直播互动台服务端")
    ap.add_argument("--room", type=int, default=0)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--asr", dest="asr_engine", choices=("sensevoice", "streaming"), default=None)
    ap.add_argument("--no-ai-fix", dest="ai_fix", action="store_false", default=None)
    ap.add_argument("--no-fish", dest="fish", action="store_false", default=None)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--open", dest="open_browser", action="store_true")
    args = ap.parse_args()
    await serve(**vars(args))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
