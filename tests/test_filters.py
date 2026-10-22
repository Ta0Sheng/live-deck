"""文本过滤器：这些函数决定了「什么话不会发出去」。"""

from __future__ import annotations

from live_deck.fish import engine


def test_similar_detects_rewrites():
    assert engine._similar("这关我卡了三天", "这关我卡了三天")
    assert engine._similar("这关我卡了三天", "这关我卡了三天了")
    assert not engine._similar("今天好热", "这游戏真好玩")


def test_echoes_detects_copying_the_streamer():
    streamer = "这个 boss 我打了三天才过一次"
    assert engine.echoes("我打了三天才过一次", streamer)
    assert not engine.echoes("三天", streamer)
    assert not engine.echoes("你打不过就换路线", streamer)


def test_self_call_is_stripped():
    assert engine.strip_self_call("鱼，这关怎么过") == "这关怎么过"
    assert engine.strip_self_call("大肥鱼：别问了") == "别问了"
    assert engine.strip_self_call("普通弹幕") == "普通弹幕"


def test_call_detection_only_for_comma():
    assert engine.CALL_RE.match("鱼，你在吗")
    assert not engine.CALL_RE.match("鱼：你在吗")


def test_banned_words():
    for text in ("这是诈骗", "举报他", "未成年别来"):
        assert engine.BANNED.search(text)
    assert not engine.BANNED.search("这关怎么过")


def test_question_detection():
    assert engine.looks_like_question("这是啥")
    assert engine.looks_like_question("为什么")
    assert engine.looks_like_question("你吃了吗")
    assert not engine.looks_like_question("好耶")


def test_search_and_feed_protocol():
    match = engine.SEARCH_RE.match("[查: 蓝色杯子]")
    assert match and match.group(1).strip() == "蓝色杯子"
    feed = engine.FEED_RE.match("投喂 小心心 x2")
    assert feed and feed.group(1) == "小心心" and feed.group(2) == "2"


def test_read_bullets_skips_headers_and_comments(tmp_path):
    path = tmp_path / "samples.md"
    path.write_text(
        "# 标题\n> 说明\n\n- 第一条\n- 第二条\n- 第三条\n",
        encoding="utf-8",
    )
    assert engine._read_bullets(path, 2) == ["第二条", "第三条"]
    assert engine._read_bullets(tmp_path / "missing.md", 2) == []
