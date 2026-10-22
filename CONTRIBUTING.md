# 参与开发

## 本地环境

```bash
git clone https://github.com/YOUR_NAME/live-deck.git
cd live-deck
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
live-deck init
live-deck demo                   # 不需要账号/模型，验证界面
```

字幕相关改动再装 `.[asr]`；窗口改动装 `.[window]`。

## 测试与检查

```bash
pytest                 # 36 个离线测试，不联网、不需要账号
ruff check src tests
python -m compileall -q src
```

CI 在 Ubuntu / Windows × Python 3.11–3.13 上跑这三条。**不要**在 CI 里加需要登录或真实房间的测试。

## 代码地图

| 路径 | 职责 |
|---|---|
| `src/live_deck/config.py` | 配置解析；改配置项先改这里 |
| `src/live_deck/bili.py` | 所有 B站 接口；换平台只需换这一层 |
| `src/live_deck/deck/server.py` | 编排、WebSocket、HTTP API、子进程管理 |
| `src/live_deck/fish/engine.py` | 决策循环；改行为多半在这里 |
| `src/live_deck/fish/persona.py` | 人格加载；记忆分离在这一层 |
| `src/live_deck/asr/` | 两套字幕引擎，尽量只依赖 `bili` 和配置 |
| `src/live_deck/demo.py` | 演示数据；改界面时用它截图 |

## 提交前

- [ ] `pytest` 和 `ruff check` 通过
- [ ] 没有提交 `config.toml`、`data/`、`models/`、`.secrets/`
- [ ] 没有真实昵称、uid、房间号、截图里的观众信息
- [ ] 新配置项写进 `config.example.toml`，并在 README 的配置表里加一行
- [ ] 改行为的话，同步更新 `docs/architecture.md` 或对应文档

## 提交信息

用一句话说清「改了什么、为什么」，中文英文都行。示例：

```
fix(asr): reshape interleaved stereo before demucs
feat(fish): add per-day gift budget ledger
docs: explain persona vs memory
```

## 隐私红线

这个项目处理的是真人直播间的数据。PR 里**不要**包含：

- 任何人的 SESSDATA / bili_jct / API key（包括打码前的原文）
- 真实房间的完整弹幕日志、观众昵称、uid
- 未打码的直播截图

需要举例就用 `persona/examples/` 里那种重写过的样本，或 `live-deck demo` 的截图。

## 行为准则

只讨论代码和设计，不攻击人。涉及平台规则的改动（更激进的重试、绕过限制）默认不接受。
