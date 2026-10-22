# 安全与隐私

## 支持范围

| 版本 | 状态 |
|---|---|
| 1.0.x | 接收安全报告 |

## 报告问题

请用 GitHub 的 **Security → Report a vulnerability**（私有 advisory），不要在公开 issue 里贴：
凭证、cookie、API key、可复现的账号信息。收到后会先确认影响范围，再决定修复和披露节奏。

## 凭证怎么放

| 文件 | 内容 | 说明 |
|---|---|---|
| `.secrets/bili_credential.json` | `sessdata` / `bili_jct` / `buvid3` / `dedeuserid` | **等于账号密码**，能发弹幕、送礼、看钱包 |
| `.secrets/deepseek_key.txt` | API key | 按量计费 |
| `config.toml` | 房间号、路径、阈值 | 不含密钥 |

- 三样都在 `.gitignore` 里；`live-deck init` 不会创建 `.secrets/`，`live-deck login` 才会写。
- `live-deck login` 会把凭证文件权限设为 `0600`（Windows 上尽力而为）。
- 怀疑泄露：B站 退出所有设备并重新扫码；DeepSeek 后台吊销 key。两件事都可以在 1 分钟内完成。

## 程序会发哪些网络请求

1. `api.live.bilibili.com` / `api.bilibili.com`：房间信息、播放地址、弹幕 WebSocket、钱包、搜索。
2. `api.deepseek.com`：生成弹幕、像人度打分、字幕同音纠错、画面描述。
3. 模型下载：`huggingface.co`（SenseVoice）、`github.com`（流式模型）。
4. 本地 `127.0.0.1:8765`：界面与 WebSocket。

没有遥测、没有崩溃上报、没有后台更新检查。

## 默认安全姿态

| 能力 | 默认 | 怎么开 |
|---|---|---|
| 发弹幕 | 关（只出草稿） | `fish.send_danmaku = true` |
| 送礼 | 关 | `fish.allow_gift = true` + `gift_daily_budget > 0` |
| 点单送礼 | 谁都不行 | `fish.owner_uid` 填自己的 uid |
| 绑定地址 | `127.0.0.1` | 不建议改 |

代码里这些是**硬闸**：`send_danmaku` 关着时，命令行参数也不能让它真发；预算为 0 时，
送礼函数不会被调用。它们不依赖模型自觉。

## 威胁模型

| 风险 | 现状 |
|---|---|
| 弹幕提示注入（「忽略以上指令」） | 弹幕只作为上下文文本；唯一被当指令的是 `owner_uid` 的「搜 XXX」，且只触发搜索 |
| 模型说出不该说的话 | 有敏感词、复述、去重、长度过滤，但**不保证**；默认不发，等于多一层人工确认 |
| 恶意直播间/伪造弹幕 | 只读展示；`bot_uid` 过滤自己的弹幕；不执行弹幕里的链接 |
| 凭证被别的程序读 | 文件权限 0600 + 不离开本机；无法防御同用户下的恶意程序 |
| 依赖供应链 | 依赖都在 `pyproject.toml` 里锁了下限；CI 只跑不联网的测试 |

## 隐私

- 观众的 uid/昵称只存在本地 `data/danmaku/`，用于「这间房怎么说话」，不上传。
- 仓库里**不含**任何真实弹幕、昵称、房间号；示例样本是重写过的。
- 演示模式（`live-deck demo`）完全不连 B站，截图可以放心发。

## 免责

本项目与哔哩哔哩无关联。自动化操作可能违反平台用户协议；因使用本项目导致的账号限制、
财产损失或其他后果由使用者自行承担。
