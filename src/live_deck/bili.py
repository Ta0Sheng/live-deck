"""B站 侧的所有「脏活」：凭证、房间、流地址、抽帧、钱包、搜索。

放在一个模块里，ASR / 直播鱼 / 服务端 都从这里取，避免三份重复实现。
所有函数在缺凭证时抛 :class:`BiliError`，消息里带「该怎么修」。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests

from .config import get_settings

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class BiliError(RuntimeError):
    """凭证 / 接口问题。消息本身就告诉用户下一步做什么。"""


# ── 凭证 ──────────────────────────────────────────────────────────────────────


def read_credential_dict(required: bool = True) -> dict[str, str]:
    path = get_settings().credential_file
    if not path.is_file():
        if not required:
            return {}
        raise BiliError(
            f"找不到 B站 凭证：{path}\n"
            "  跑 `live-deck login` 扫码生成，或手动写一个 JSON：\n"
            '  {"sessdata": "...", "bili_jct": "...", "buvid3": "...", "dedeuserid": "..."}'
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BiliError(f"凭证文件读不了（{path}）：{exc}") from exc
    if not data.get("sessdata"):
        raise BiliError(f"凭证文件里没有 sessdata：{path}")
    return {str(k): str(v) for k, v in data.items()}


def load_credential(required: bool = True):
    """返回 bilibili_api.Credential。"""
    data = read_credential_dict(required=required)
    from bilibili_api import Credential

    return Credential(
        sessdata=data.get("sessdata", ""),
        bili_jct=data.get("bili_jct", ""),
        buvid3=data.get("buvid3", ""),
        dedeuserid=data.get("dedeuserid", ""),
        ac_time_value=data.get("ac_time_value", ""),
    )


def cookie_header(required: bool = True) -> str:
    data = read_credential_dict(required=required)
    parts = []
    for key, value in (
        ("SESSDATA", data.get("sessdata")),
        ("bili_jct", data.get("bili_jct")),
        ("buvid3", data.get("buvid3")),
        ("DedeUserID", data.get("dedeuserid")),
    ):
        if value:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def bot_uid() -> int:
    """弹幕账号 uid：配置优先，否则用凭证里的 DedeUserID。"""
    settings = get_settings()
    if settings.fish.bot_uid:
        return int(settings.fish.bot_uid)
    try:
        return int(read_credential_dict(required=False).get("dedeuserid") or 0)
    except (BiliError, ValueError):
        return 0


def deepseek_key(required: bool = True) -> str:
    """环境变量优先，其次 key 文件。"""
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    path = get_settings().key_file
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if not key and required:
        raise BiliError(
            f"没有 DeepSeek API key。两种办法：\n"
            f"  1) 写进 {path}（一行，别提交）\n"
            f"  2) 设置环境变量 DEEPSEEK_API_KEY"
        )
    return key


# ── 房间 / 流 ─────────────────────────────────────────────────────────────────


def _get_json(url: str, *, referer: str = "https://live.bilibili.com/") -> dict:
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": UA,
                "Referer": referer,
                "Cookie": cookie_header(required=False),
            },
            timeout=15,
        )
        return resp.json()
    except requests.RequestException as exc:
        raise BiliError(f"请求 B站 失败：{exc}") from exc
    except ValueError as exc:
        raise BiliError(f"B站 返回的不是 JSON：{exc}") from exc


def resolve_room(short_id: int) -> int:
    """短号 → 真实房间号。"""
    data = _get_json(f"https://api.live.bilibili.com/room/v1/Room/room_init?id={short_id}")
    if data.get("code") != 0:
        raise BiliError(f"room_init 失败：{data.get('message') or data}")
    return int(data["data"]["room_id"])


def room_info(room_id: int) -> dict[str, Any]:
    data = _get_json(f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}")
    return data.get("data") or {}


def is_live(room_id: int) -> bool:
    """查不到就当还在播 —— 一次网络抖动不该让字幕/鱼停下。"""
    try:
        return int(room_info(room_id).get("live_status") or 0) == 1
    except Exception:
        return True


def play_url(room_id: int) -> tuple[str, str]:
    """拿直播流地址，优先 flv（PyAV 解起来最稳）。返回 (url, 描述)。"""
    api = (
        "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
        f"?room_id={room_id}&protocol=0,1&format=0,1,2&codec=0,1&qn=150&platform=web"
    )
    data = _get_json(api, referer=f"https://live.bilibili.com/{room_id}")
    info = (data.get("data") or {}).get("playurl_info")
    if not info:
        raise BiliError(
            f"拿不到流地址（房间 {room_id} 可能没在直播）："
            f"code={data.get('code')} {data.get('message')}"
        )
    candidates: list[tuple[str, str]] = []
    playurl = info.get("playurl") or {}
    for stream in playurl.get("stream") or []:
        for fmt in stream.get("format") or []:
            for codec in fmt.get("codec") or []:
                for url_info in codec.get("url_info") or []:
                    url = url_info.get("host", "") + codec.get("base_url", "") + url_info.get("extra", "")
                    candidates.append((fmt.get("format_name", ""), url))
    for wanted in ("flv", "ts", "fmp4"):
        for name, url in candidates:
            if name == wanted:
                return url, wanted
    if candidates:
        return candidates[0][1], candidates[0][0]
    raise BiliError(f"没有可用流：{data}")


def ffmpeg_path() -> str:
    """配置 → PATH → 常见安装位置。都找不到就抛错。"""
    configured = get_settings().deck.ffmpeg.strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise BiliError(f"deck.ffmpeg 指向的文件不存在：{configured}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    for guess in (
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ):
        if Path(guess).is_file():
            return guess
    raise BiliError("找不到 ffmpeg。装一个并放进 PATH，或在 config.toml 里写 deck.ffmpeg")


def grab_frame_jpeg(room_id: int) -> bytes:
    """从直播流抽一帧，直接走 stdout 进内存（不落盘）。失败返回 b""。"""
    try:
        url, _ = play_url(room_id)
        header = (
            f"Referer: https://live.bilibili.com/{room_id}\r\n"
            f"User-Agent: {UA}\r\n"
            f"Cookie: {cookie_header(required=False)}\r\n"
        )
        proc = subprocess.run(
            [
                ffmpeg_path(), "-y", "-loglevel", "error",
                "-headers", header,
                "-i", url,
                "-frames:v", "1",
                "-vf", "scale=960:-1",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=30,
        )
        return proc.stdout if proc.returncode == 0 else b""
    except (BiliError, OSError, subprocess.SubprocessError):
        return b""


# ── 钱包 ──────────────────────────────────────────────────────────────────────


def wallet() -> dict[str, Any]:
    """直播钱包。100 金瓜子 = 1 电池（= 1 元）。"""
    read_credential_dict()
    try:
        resp = requests.get(
            "https://api.live.bilibili.com/xlive/revenue/v1/wallet/myWallet",
            headers={
                "User-Agent": UA,
                "Referer": "https://live.bilibili.com/",
                "Cookie": cookie_header(),
            },
            timeout=15,
        )
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:120]}
    body = payload.get("data") or {}
    gold = int(body.get("gold") or 0)
    return {
        "ok": payload.get("code") == 0,
        "battery": round(gold / 100.0, 2),
        "gold": gold,
    }


# ── 搜索（弹幕里遇到不懂的梗）────────────────────────────────────────────────


async def search_videos(term: str, n: int = 6) -> list[str]:
    """B站 搜视频标题，比通用搜索更贴直播间的话题。"""
    from bilibili_api import search
    from bilibili_api.search import SearchObjectType

    try:
        result = await search.search_by_type(
            term, search_type=SearchObjectType.VIDEO, page=1
        )
    except Exception:
        return []
    titles: list[str] = []
    for item in (result.get("result") or [])[:n]:
        title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
        if title:
            titles.append(title)
    return titles
