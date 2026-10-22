"""配置层：缺省值、TOML 覆盖、相对路径、播种。"""

from __future__ import annotations

import pytest

from live_deck import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("LIVE_DECK_CONFIG", raising=False)
    monkeypatch.delenv("LIVE_DECK_DATA_DIR", raising=False)
    real_find = config.find_config
    # 只屏蔽「自动发现」，显式传入的路径仍然要按真实逻辑解析
    monkeypatch.setattr(
        config,
        "find_config",
        lambda explicit=None: real_find(explicit) if explicit else None,
    )


def test_defaults_without_config():
    settings = config.load()
    assert settings.config_file is None
    assert settings.deck.port == 8765
    assert settings.deck.asr_engine == "sensevoice"
    assert settings.fish.send_danmaku is False
    assert settings.fish.allow_gift is False
    assert settings.fish.gift_daily_budget == 0
    assert settings.data_dir == settings.root / "data"


def test_reads_toml_and_resolves_relative_paths(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[deck]
port = 9000
asr_engine = "streaming"

[asr]
model_dir = "assets/models"

[fish]
min_gap = 42
send_danmaku = true
addresses = ["鱼", "肥鱼"]

[paths]
data_dir = "runtime"
""",
        encoding="utf-8",
    )
    settings = config.load(cfg)
    assert settings.deck.port == 9000
    assert settings.deck.asr_engine == "streaming"
    assert settings.fish.min_gap == 42
    assert settings.fish.send_danmaku is True
    assert settings.asr_model_dir == tmp_path / "assets" / "models"
    assert settings.data_dir == tmp_path / "runtime"
    assert settings.logs_dir == tmp_path / "runtime" / "logs"


def test_data_dir_env_wins(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[paths]\ndata_dir = 'runtime'\n", encoding="utf-8")
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("LIVE_DECK_DATA_DIR", str(other))
    settings = config.load(cfg)
    assert settings.data_dir == other


def test_unknown_key_is_a_warning_not_an_error(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[deck]\nprot = 1234\n", encoding="utf-8")
    settings = config.load(cfg)
    assert settings.deck.port == 8765
    assert any("deck.prot" in warning for warning in settings.warnings)


def test_invalid_engine_rejected(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[deck]\nasr_engine = 'magic'\n", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(cfg)


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(config.ConfigError, match="不存在"):
        config.load(tmp_path / "nope.toml")


def test_ensure_dirs_seeds_examples(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    settings = config.load(cfg)
    # 让 root 指向仓库，才能找到 persona/examples
    settings.root = config.project_root()
    settings.ensure_dirs()
    assert settings.samples_dir.is_dir()
    assert settings.knowledge_dir.is_dir()
    assert settings.good_samples.is_file()
    assert settings.bad_samples.is_file()
