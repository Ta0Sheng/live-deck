# 部署

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.11+（服务端实测 3.14；窗口宿主建议 3.12） |
| 系统 | Windows 10/11 为主；macOS / Linux 可用浏览器模式 |
| ffmpeg | 在 PATH 里（画面抽帧用） |
| 显卡 | 可选。NVIDIA + CUDA 时字幕走 GPU；没有就 CPU（慢，但能跑） |
| 账号 | 一个能登录 B站 的账号；发弹幕需要实名，送礼需要余额 |

## 2. Windows（推荐路径）

```powershell
git clone https://github.com/YOUR_NAME/live-deck.git
cd live-deck

python -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip

pip install -e ".[asr,window]"
live-deck init
live-deck demo
```

看到界面后，正式使用：

```powershell
live-deck login                  # 手机扫码
live-deck models install         # 约 300 MB
live-deck doctor
live-deck app
```

### GPU 字幕

```powershell
pip install -e ".[asr-gpu]"
```

`torch` 的 CUDA 版本要和驱动匹配。装完用下面这条确认：

```powershell
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

打印 `True` 就会自动用 GPU；否则退回 CPU（`asr.py` 里会自动判断，不需要改配置）。

### pywebview 与 Python 版本

`pywebview` 依赖 `pythonnet`，后者对新版 Python 的支持经常滞后。两种情况：

- 装得上：`live-deck app` 开原生窗口。
- 装不上：跳过 `[window]`，`live-deck app` 会自动用默认浏览器打开，功能完全一样。

如果窗口宿主和服务端分在两个 venv（比如 3.12 装 pywebview、3.14 装服务端依赖），
在 `config.toml` 里把 `deck.python` 指向**服务端**那个解释器的绝对路径。
宿主只负责起窗口，字幕/鱼仍然用服务端解释器拉起来。

## 3. macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[asr]"
live-deck init
live-deck demo
```

- 窗口走浏览器（没有 WebView2）。
- `ffmpeg` 用系统包管理器装：`brew install ffmpeg` / `sudo apt install ffmpeg`。
- GPU：Linux 上 `.[asr-gpu]` 可用；macOS 的 MPS 支持取决于 torch 版本，没验证过。

## 4. 模型

```bash
live-deck models status
live-deck models install                  # SenseVoice int8 + 流式
live-deck models install sensevoice       # 只下 SenseVoice
live-deck models install sensevoice --full  # 额外下 fp32（约 900 MB，精修更准）
live-deck models install streaming
```

默认落点：

```
models/sensevoice/            tokens.txt, model.int8.onnx, (model.onnx)
models/streaming-zipformer/   tokens.txt, encoder/decoder/joiner*.onnx
```

离线机器：在能上网的机器上下好，整个 `models/` 拷过去即可；或在 `config.toml` 里把
`asr.model_dir` / `asr.streaming_model_dir` 指到已有目录（支持绝对路径）。

## 5. 后台运行

### Windows 计划任务

```powershell
# 登录后启动，无窗口
schtasks /Create /TN "live-deck" /SC ONLOGON /TR "C:\path\to\live-deck\.venv\Scripts\live-deck.exe serve --room 123456" /RL LIMITED
```

### Linux systemd（用户级）

```ini
# ~/.config/systemd/user/live-deck.service
[Unit]
Description=live-deck
After=network-online.target

[Service]
WorkingDirectory=%h/live-deck
ExecStart=%h/live-deck/.venv/bin/live-deck serve --room 123456 --no-open
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now live-deck
```

## 6. 更新

```bash
git pull
pip install -e ".[asr]"     # 依赖有变动时
live-deck doctor
```

`config.toml`、`data/`、`.secrets/` 都在 gitignore 里，更新不会覆盖。

## 7. 端口与防火墙

服务只监听 `127.0.0.1`（`deck.host`），默认不对外。要换端口改 `deck.port`；
如果 8765 被占用，`live-deck doctor` 会提示。

## 8. 卸载

删掉项目目录即可。数据都在 `data/`，凭证在 `.secrets/`，没有写注册表、没有系统服务。
