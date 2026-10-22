"""演示模式：没有账号也能看到完整界面，靠的就是这份脚本。"""

from __future__ import annotations

import asyncio

from live_deck import demo

ALLOWED = {"enter", "danmaku", "asr", "scene", "fish", "mode"}


def test_scene_image_is_inline_svg():
    assert demo.SCENE_IMAGE.startswith("data:image/svg+xml;base64,")
    assert "DEMO" in demo.SCENE_SVG


def test_timeline_shape():
    assert len(demo.TIMELINE) >= 10
    for delay, event in demo.TIMELINE:
        assert isinstance(delay, (int, float)) and delay >= 0
        assert event["t"] in ALLOWED
    fish = [event for _, event in demo.TIMELINE if event["t"] == "fish"]
    assert {event["kind"] for event in fish} >= {"preview", "draft", "sent"}


def test_run_pushes_events_with_timestamps():
    class FakeHub:
        def __init__(self):
            self.events = []

        async def push(self, event):
            self.events.append(event)

    async def scenario():
        hub = FakeHub()
        task = asyncio.create_task(demo.run(hub))
        await asyncio.sleep(0.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return hub.events

    events = asyncio.run(scenario())
    assert events
    assert all("ts" in event for event in events)
