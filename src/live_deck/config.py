"""集中配置：一个 ``config.toml`` 管住所有路径、阈值和开关。

设计原则
--------
1. **没有配置文件也能启动**：缺省值全部落在仓库内的相对路径，方便第一次跑。
2. **相对路径都相对 config.toml 所在目录**，所以整个文件夹搬走不需要改任何东西。
3. **密钥永远不进代码**：只记文件路径，路径本身写在 ``[secrets]`` 里。
4. **环境变量优先级最高**：``LIVE_DECK_CONFIG`` / ``LIVE_DECK_DATA_DIR`` /
   ``DEEPSEEK_API_KEY`` 可以在不碰配置文件的情况下覆盖。

典型用法::

    from live_deck.config import get_settings
    S = get_settings()
    print(S.logs_dir, S.deck.port)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AsrCfg",
    "ConfigError",
    "DeckCfg",
    "FishCfg",
    "SecretsCfg",
    "Settings",
    "find_config",
    "get_settings",
    "load",
    "project_root",
    "reset_cache",
]

CONFIG_NAME = "config.toml"
PKG_ROOT = Path(__file__).resolve().parents[2]  # src/live_deck/config.py -> 仓库根


class ConfigError(RuntimeError):
    """配置文件本身有问题（语法错、类型错）。"""


# ── 各大块 ────────────────────────────────────────────────────────────────────


@dataclass
class DeckCfg:
    """界面 / 服务端。"""

    host: str = "127.0.0.1"
    port: int = 8765
    python: str = ""
    frame_interval: int = 60
    asr_engine: str = "sensevoice"
    ai_fix: bool = True
    fish: bool = True
    ffmpeg: str = ""
    open_browser: bool = True


@dataclass
class AsrCfg:
    """字幕。``python`` 留空 = 用当前解释器。"""

    python: str = ""
    model_dir: str = "models/sensevoice"
    streaming_model_dir: str = "models/streaming-zipformer"
    chunk_seconds: float = 4.0
    clean: bool = True
    clean_every: float = 30.0
    clean_window: float = 36.0
    threads: int = 8
    separate: bool = True


@dataclass
class FishCfg:
    """直播鱼。所有发送行为默认关闭。"""

    enabled: bool = True
    persona: str = "persona/persona.md"
    memory: str = ""
    name: str = "大肥鱼"
    call_word: str = "鱼"
    addresses: list[str] = field(default_factory=lambda: ["大肥鱼", "肥鱼", "这条鱼"])
    model: str = "deepseek-chat"
    flash_model: str = "deepseek-flash"
    max_total: int = 999
    min_gap: int = 75
    max_per_hour: int = 999
    veto: float = 3.3
    vision: int = 120
    frame_ttl: int = 45
    delay_comp: float = 2.3
    temperature: float = 0.7
    max_tokens: int = 40
    send_danmaku: bool = False
    allow_gift: bool = False
    gift_daily_budget: int = 0
    owner_uid: str = ""
    bot_uid: int = 0
    good_samples: str = "data/samples/good.md"
    bad_samples: str = "data/samples/bad.md"
    ctx_good_samples: str = "data/samples/context-good.md"
    ctx_bad_samples: str = "data/samples/context-bad.md"


@dataclass
class SecretsCfg:
    deepseek_key_file: str = ".secrets/deepseek_key.txt"
    bili_credential_file: str = ".secrets/bili_credential.json"


# ── 找配置文件 ────────────────────────────────────────────────────────────────


def project_root() -> Path:
    """仓库根目录：优先认 pyproject.toml + src 的那一层。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists() and (parent / "src").is_dir():
            return parent
    return PKG_ROOT


def find_config(explicit: str | Path | None = None) -> Path | None:
    """按「显式参数 → 环境变量 → 当前目录 → 仓库根」的顺序找 config.toml。"""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    env = os.environ.get("LIVE_DECK_CONFIG")
    if env:
        path = Path(env).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    for base in (Path.cwd(), project_root()):
        candidate = base / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


# ── 解析 ──────────────────────────────────────────────────────────────────────


def _merge(obj: Any, data: dict[str, Any], where: str, warnings: list[str]) -> None:
    """把 TOML 字典按字段名灌进 dataclass；未知键只警告，不炸。"""
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key.startswith("_") or key == "comment":
            continue
        if key not in known:
            warnings.append(f"{where}{key}：未知配置项，已忽略")
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value, f"{where}{key}.", warnings)
        else:
            setattr(obj, key, value)


def _abs(root: Path, value: str | Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (root / path)


@dataclass
class Settings:
    """解析完成后的配置 + 绝对路径。"""

    root: Path
    config_file: Path | None
    deck: DeckCfg
    asr: AsrCfg
    fish: FishCfg
    secrets: SecretsCfg
    data_dir: Path
    warnings: list[str] = field(default_factory=list)

    # 常用派生路径
    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"

    @property
    def danmaku_dir(self) -> Path:
        return self.data_dir / "danmaku"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def persona_file(self) -> Path:
        return _abs(self.root, self.fish.persona)

    @property
    def memory_file(self) -> Path | None:
        if not str(self.fish.memory).strip():
            return None
        return _abs(self.root, self.fish.memory)

    @property
    def good_samples(self) -> Path:
        return _abs(self.root, self.fish.good_samples)

    @property
    def bad_samples(self) -> Path:
        return _abs(self.root, self.fish.bad_samples)

    @property
    def ctx_good_samples(self) -> Path:
        return _abs(self.root, self.fish.ctx_good_samples)

    @property
    def ctx_bad_samples(self) -> Path:
        return _abs(self.root, self.fish.ctx_bad_samples)

    @property
    def asr_model_dir(self) -> Path:
        return _abs(self.root, self.asr.model_dir)

    @property
    def streaming_model_dir(self) -> Path:
        return _abs(self.root, self.asr.streaming_model_dir)

    @property
    def key_file(self) -> Path:
        return _abs(self.root, self.secrets.deepseek_key_file)

    @property
    def credential_file(self) -> Path:
        return _abs(self.root, self.secrets.bili_credential_file)

    @property
    def mode_file(self) -> Path:
        return self.state_dir / "mode.txt"

    @property
    def last_room_file(self) -> Path:
        return self.state_dir / "last_room.txt"

    @property
    def gift_ledger_file(self) -> Path:
        return self.state_dir / "gift-ledger.json"

    # 目录创建 & 样例播种
    def ensure_dirs(self, seed: bool = True) -> None:
        for path in (
            self.data_dir,
            self.logs_dir,
            self.samples_dir,
            self.knowledge_dir,
            self.danmaku_dir,
            self.state_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if seed:
            self._seed_samples()

    def _seed_samples(self) -> None:
        """首次运行把仓库里的示例复制到 data/，之后只改 data/，仓库保持干净。"""
        pairs = (
            ("persona/examples/good.md", self.good_samples),
            ("persona/examples/bad.md", self.bad_samples),
            ("persona/examples/context-good.md", self.ctx_good_samples),
            ("persona/examples/context-bad.md", self.ctx_bad_samples),
        )
        for src_rel, dst in pairs:
            src = self.root / src_rel
            if dst.exists() or not src.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass

    def as_dict(self) -> dict[str, Any]:
        """给 doctor 看的一份扁平快照（不含密钥）。"""
        return {
            "root": str(self.root),
            "config": str(self.config_file) if self.config_file else None,
            "data_dir": str(self.data_dir),
            "port": self.deck.port,
            "asr_engine": self.deck.asr_engine,
            "persona": str(self.persona_file),
            "send_danmaku": self.fish.send_danmaku,
            "allow_gift": self.fish.allow_gift,
        }


def load(config_file: str | Path | None = None) -> Settings:
    """读配置。没有配置文件时全部用缺省值。"""
    path = find_config(config_file)
    root = path.parent if path else project_root()

    raw: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"配置文件不存在：{path}")
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} 语法错误：{exc}") from exc
        except OSError as exc:
            raise ConfigError(f"读不到 {path}：{exc}") from exc

    warnings: list[str] = []
    deck, asr, fish, secrets = DeckCfg(), AsrCfg(), FishCfg(), SecretsCfg()
    _merge(deck, raw.get("deck", {}) or {}, "deck.", warnings)
    _merge(asr, raw.get("asr", {}) or {}, "asr.", warnings)
    _merge(fish, raw.get("fish", {}) or {}, "fish.", warnings)
    _merge(secrets, raw.get("secrets", {}) or {}, "secrets.", warnings)

    paths_raw = raw.get("paths", {}) or {}
    data_value = os.environ.get("LIVE_DECK_DATA_DIR") or paths_raw.get("data_dir") or "data"

    settings = Settings(
        root=root,
        config_file=path,
        deck=deck,
        asr=asr,
        fish=fish,
        secrets=secrets,
        data_dir=_abs(root, data_value),
        warnings=warnings,
    )

    # 基本校验：早失败，给出能照做的提示
    if not isinstance(deck.port, int) or not (1 <= deck.port <= 65535):
        raise ConfigError(f"deck.port 不合法：{deck.port!r}")
    if deck.asr_engine not in ("sensevoice", "streaming"):
        raise ConfigError(
            f"deck.asr_engine 只能是 sensevoice / streaming，现在是 {deck.asr_engine!r}"
        )
    if fish.temperature < 0 or fish.temperature > 2:
        raise ConfigError(f"fish.temperature 应在 0–2：{fish.temperature}")
    if not isinstance(fish.addresses, list):
        raise ConfigError("fish.addresses 必须是字符串数组")
    return settings


_CACHE: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """进程级单例。"""
    global _CACHE
    if _CACHE is None or reload:
        _CACHE = load()
    return _CACHE


def reset_cache() -> None:
    """测试用：丢掉单例。"""
    global _CACHE
    _CACHE = None
