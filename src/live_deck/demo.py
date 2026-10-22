"""演示模式：不连 B站、不调模型，用脚本喂出四条流。

用途有两个：

* 第一次 clone 下来，没有账号和 API key 也能看到完整界面（``live-deck demo``）；
* 截图/录屏时不会出现真实观众昵称。
"""

from __future__ import annotations

import asyncio
import base64
import time

SCENE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
<defs>
<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#151d2e"/><stop offset="1" stop-color="#3a1f3d"/>
</linearGradient>
</defs>
<rect width="960" height="540" fill="url(#g)"/>
<circle cx="700" cy="170" r="130" fill="#ffb86b" opacity="0.16"/>
<circle cx="770" cy="320" r="90" fill="#58a6ff" opacity="0.16"/>
<rect x="60" y="60" width="300" height="26" rx="13" fill="#ffffff" opacity="0.08"/>
<rect x="60" y="104" width="210" height="26" rx="13" fill="#ffffff" opacity="0.06"/>
<text x="48" y="486" fill="#8b949e" font-family="Segoe UI, sans-serif" font-size="26">DEMO · 直播画面占位</text>
</svg>"""

SCENE_IMAGE = "data:image/svg+xml;base64," + base64.b64encode(SCENE_SVG.encode("utf-8")).decode()

# (间隔秒数, 事件)。循环播放；时间戳在发送时补。
TIMELINE: list[tuple[float, dict]] = [
    (0.3, {"t": "enter", "user": "路过的观众"}),
    (1.2, {"t": "danmaku", "user": "今天也在熬夜", "text": "主播晚上好"}),
    (1.0, {"t": "danmaku", "user": "只会打简单模式", "text": "这关我卡了一个礼拜了"}),
    (1.4, {"t": "asr", "time": "", "text": "今天先把上次那个关卡打完，别催别催。"}),
    (1.0, {"t": "danmaku", "user": "键盘侠本侠", "text": "右边那个箱子可以推"}),
    (2.0, {"t": "fish", "kind": "preview", "text": "[3/5] 那个箱子我上次也推过 推不动"}),
    (1.5, {"t": "asr", "time": "", "text": "推不动是吧，那我绕后面看看。"}),
    (1.6, {"t": "danmaku", "user": "一条小咸鱼", "text": "鱼，你觉得这关设计得怎么样"}),
    (1.2, {"t": "danmaku", "user": "路过的观众", "text": "2333333"}),
    (1.5, {"t": "asr", "time": "", "text": "这个设计挺坏的，看起来只有一条路。"}),
    (2.2, {"t": "fish", "kind": "draft", "text": "[4/5] 绕后那条路我看了半天才看到 藏得真深"}),
    (2.0, {"t": "scene", "desc": "主播在玩横版闯关游戏，画面右侧是滚动的弹幕。", "img": SCENE_IMAGE}),
    (2.4, {"t": "asr", "time": "", "text": "哦——原来在这里，我说怎么一直过不去。",
           "clean": True, "raw": "哦原来在这里我说怎么一直过不去"}),
    (2.0, {"t": "danmaku", "user": "今天也在熬夜", "text": "好耶"}),
    (1.6, {"t": "fish", "kind": "sent", "text": "[5/5] 藏这么深 换我我也得卡三天"}),
    (3.0, {"t": "asr", "time": "", "text": "行，过了。下一关听说更难。"}),
    (2.0, {"t": "danmaku", "user": "只会打简单模式", "text": "下播之前能通关吗"}),
    (1.8, {"t": "fish", "kind": "preview", "text": "[3/5] 这句先不接 看他怎么回"}),
    (3.5, {"t": "asr", "time": "", "text": "通不了，今天就打到这儿。"}),
    (4.0, {"t": "mode", "mode": "draft"}),
]


async def run(hub) -> None:
    """无限循环地往 hub 里推事件。"""
    while True:
        for delay, event in TIMELINE:
            await asyncio.sleep(delay)
            payload = dict(event)
            payload["ts"] = time.time()
            if payload.get("t") in ("asr", "fish", "danmaku") and not payload.get("time"):
                payload["time"] = time.strftime("%H:%M:%S")
            await hub.push(payload)
