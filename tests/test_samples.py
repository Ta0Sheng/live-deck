"""样本解析与检索：让旧经验回来，又不让新反馈失效。"""

from __future__ import annotations

from live_deck.fish.samples import (
    Sample,
    load_samples,
    parse_line,
    select_samples,
    similarity,
)


def test_parse_scene_and_note():
    sample = parse_line("- 【主播在打 boss】先清小怪 —— 复述原话")
    assert sample is not None
    assert sample.scene == "主播在打 boss"
    assert sample.reply == "先清小怪"
    assert sample.note == "复述原话"


def test_parse_plain_and_skip_headers():
    sample = parse_line("- 这关怎么过")
    assert sample is not None
    assert sample.scene == ""
    assert sample.reply == "这关怎么过"
    assert parse_line("# 标题") is None
    assert parse_line("> 说明") is None
    assert parse_line("- ") is None


def test_prompt_rendering():
    sample = Sample(scene="主播卡关了", reply="绕后面走", note="")
    assert sample.prompt("✓") == "✓ 【主播卡关了】绕后面走"


def test_load_samples_reads_file(tmp_path):
    path = tmp_path / "good.md"
    path.write_text("# 标题\n- 【A】一\n- 【B】二\n", encoding="utf-8")
    samples = load_samples(path)
    assert [s.reply for s in samples] == ["一", "二"]
    assert [s.reply for s in load_samples(path, limit=1)] == ["二"]
    assert load_samples(tmp_path / "none.md") == []


def test_similarity_prefers_containment():
    context = "主播在打 boss，第二阶段会召唤小怪，先清小怪再打本体"
    assert similarity("打 boss 召唤小怪", context) > 0.5
    assert similarity("今天吃什么", context) == 0


def test_select_prefers_relevant_over_recent():
    pool = [
        Sample(scene="主播在打 boss 召唤小怪", reply="先清小怪", order=0),
        Sample(scene="主播在聊晚饭", reply="吃的什么", order=1),
        Sample(scene="主播在唱歌", reply="这首歌好听", order=2),
    ]
    chosen = select_samples(pool, "这把 boss 又召唤小怪了", k=1, recent=0)
    assert chosen[0].reply == "先清小怪"


def test_select_always_keeps_recent():
    pool = [
        Sample(scene="无关一", reply="一", order=0),
        Sample(scene="无关二", reply="二", order=1),
        Sample(scene="刚刚被夸的那句", reply="新样本", order=2),
    ]
    chosen = select_samples(pool, "完全不同的场面", k=1, recent=1)
    assert any(sample.reply == "新样本" for sample in chosen)


def test_select_empty_and_cold_start():
    assert select_samples([], "任意", k=8, recent=4) == []
    pool = [Sample(scene="", reply=str(i), order=i) for i in range(5)]
    chosen = select_samples(pool, "毫无重合的语境", k=2, recent=1)
    assert chosen
    assert len(chosen) <= 3
