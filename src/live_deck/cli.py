"""``live-deck`` 命令行：装环境、体检、登录、下模型、起程序。

设计目标：**新电脑上只需要记住四个命令**。

    live-deck init        # 生成 config.toml + data/ 目录
    live-deck doctor      # 告诉你还缺什么（含修复命令）
    live-deck models install
    live-deck app         # 或 live-deck demo（不登录也能看界面）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, ConfigError, get_settings

OK = "[ OK ]"
WARN = "[WARN]"
BAD = "[FAIL]"
INFO = "[ -- ]"

SENSEVOICE_BASE = (
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
)
SENSEVOICE_FILES = (("tokens.txt", "SenseVoice tokens"), ("model.int8.onnx", "SenseVoice int8"))
STREAMING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23.tar.bz2"
)
STREAMING_DIR = "streaming-zipformer"


# ── 小工具 ────────────────────────────────────────────────────────────────────


def _settings():
    return get_settings(reload=True)


def _head(title: str) -> None:
    print()
    print(f"== {title} ==")


def _mask(secret: str) -> str:
    if not secret:
        return "(空)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:3]}…{secret[-3:]}（{len(secret)} 字符）"


def _open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            import webbrowser

            webbrowser.open(path.as_uri())
    except OSError:
        pass


def _project_python_env() -> dict[str, str]:
    settings = _settings()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    src = str(settings.root / "src")
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if src not in parts:
        env["PYTHONPATH"] = os.pathsep.join([src, *parts])
    return env


# ── init ──────────────────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    settings = _settings()
    base = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd()
    target = base / CONFIG_NAME

    if target.exists() and not args.force:
        print(f"{target} 已经存在。要覆盖加 --force（旧文件会备份成 .bak）。")
        return 1
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup)
        print(f"已备份旧配置 -> {backup}")

    example = settings.root / "config.example.toml"
    if not example.is_file():
        print(f"找不到模板：{example}")
        return 1

    base.mkdir(parents=True, exist_ok=True)
    shutil.copy2(example, target)
    os.environ["LIVE_DECK_CONFIG"] = str(target)
    settings = _settings()
    settings.ensure_dirs()

    _head("配置已生成")
    print(f"  配置文件  {target}")
    print(f"  数据目录  {settings.data_dir}")
    print(f"  人格文件  {settings.persona_file}")
    print()
    print("接下来：")
    print("  1) live-deck doctor          看看缺什么")
    print("  2) live-deck login           B站 扫码登录（要发弹幕/送礼才需要）")
    print("  3) live-deck models install  下字幕模型（约 300 MB）")
    print("  4) live-deck demo            先看看界面（不用账号）")
    print("  5) live-deck app             正式启动")
    return 0


# ── doctor ────────────────────────────────────────────────────────────────────


def _dep_state(module: str) -> tuple[bool, str]:
    import importlib.util

    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return False, ""
    return spec is not None, ""


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings()
    problems = 0
    warnings = 0

    def report(tag: str, name: str, detail: str = "") -> None:
        nonlocal problems, warnings
        if tag == BAD:
            problems += 1
        elif tag == WARN:
            warnings += 1
        print(f"  {tag} {name:<30} {detail}")

    print(f"live-deck {__version__}  ·  doctor")
    print()

    # 1. 基础环境
    py_ok = sys.version_info >= (3, 11)
    report(
        OK if py_ok else BAD,
        "Python",
        f"{sys.version.split()[0]}（需要 >= 3.11）" if py_ok else f"{sys.version.split()[0]} 太旧，需要 >= 3.11",
    )

    if settings.config_file:
        report(OK, "config.toml", str(settings.config_file))
    else:
        report(WARN, "config.toml", "没找到，全部用缺省值（可以先 live-deck init）")
    for warning in settings.warnings:
        report(WARN, "配置项", warning)

    # 2. 目录可写
    try:
        settings.ensure_dirs()
        probe = settings.data_dir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report(OK, "数据目录", str(settings.data_dir))
    except OSError as exc:
        report(BAD, "数据目录", f"不可写：{exc}")

    # 3. 人格
    try:
        from .fish.persona import persona_stats

        stats = persona_stats()
        report(OK, "人格文件", f"{stats['chars']} 字 / {stats['lines']} 行  {stats['path']}")
    except Exception as exc:  # noqa: BLE001
        report(BAD, "人格文件", str(exc))

    # 4. 密钥 / 凭证
    from . import bili

    try:
        key = bili.deepseek_key(required=False)
        if key:
            report(OK, "DeepSeek key", _mask(key))
        else:
            report(WARN, "DeepSeek key", f"没有（{settings.key_file}）—— 直播鱼会不可用")
    except Exception as exc:  # noqa: BLE001
        report(WARN, "DeepSeek key", str(exc))

    cred = {}
    try:
        cred = bili.read_credential_dict(required=False)
        if cred.get("sessdata"):
            uid = cred.get("dedeuserid") or "?"
            report(OK, "B站 凭证", f"uid {uid}，SESSDATA {_mask(cred['sessdata'])}")
        else:
            report(WARN, "B站 凭证", f"没有（{settings.credential_file}）—— 跑 live-deck login")
    except Exception as exc:  # noqa: BLE001
        report(WARN, "B站 凭证", str(exc))

    # 5. 依赖
    deps = [
        ("aiohttp", "服务端 websocket", True),
        ("requests", "B站 接口", True),
        ("bilibili_api", "弹幕 / 房间", True),
        ("psutil", "进程清理", False),
        ("av", "字幕拉流", False),
        ("numpy", "字幕", False),
        ("sherpa_onnx", "字幕识别", False),
        ("torch", "人声分离", False),
        ("demucs", "人声分离", False),
        ("webview", "桌面窗口", False),
    ]
    for module, label, required in deps:
        present, _ = _dep_state(module)
        if present:
            report(OK, label, module)
        else:
            tag = BAD if required else WARN
            report(tag, label, f"缺 {module}（pip install -e \".[asr]\"）")

    # 6. 模型
    sense = settings.asr_model_dir
    have_sense = (sense / "tokens.txt").is_file() and (sense / "model.int8.onnx").is_file()
    report(
        OK if have_sense else WARN,
        "SenseVoice 模型",
        str(sense) if have_sense else "没下（live-deck models install sensevoice）",
    )
    stream = settings.streaming_model_dir
    have_stream = (stream / "tokens.txt").is_file()
    report(
        OK if have_stream else WARN,
        "流式模型",
        str(stream) if have_stream else "没下（live-deck models install streaming）",
    )

    # 7. ffmpeg / 端口
    try:
        report(OK, "ffmpeg", bili.ffmpeg_path())
    except Exception as exc:  # noqa: BLE001
        report(WARN, "ffmpeg", str(exc))

    with socket.socket() as sock:
        busy = sock.connect_ex((settings.deck.host, settings.deck.port)) == 0
    report(WARN if busy else OK, f"端口 {settings.deck.port}", "已被占用（可能已经开着一个）" if busy else "空闲")

    # 8. 发送总闸（这一屏幕就是安全状态）
    print()
    print("  发送能力：")
    report(
        WARN if settings.fish.send_danmaku else OK,
        "  fish.send_danmaku",
        "开（会真的发弹幕）" if settings.fish.send_danmaku else "关（只出草稿）",
    )
    report(
        WARN if settings.fish.allow_gift else OK,
        "  fish.allow_gift",
        f"开，每日上限 {settings.fish.gift_daily_budget} 金瓜子"
        if settings.fish.allow_gift
        else "关（不会花钱）",
    )

    print()
    if problems:
        print(f"结论：{problems} 个必须解决的问题，{warnings} 个提醒。")
    elif warnings:
        print(f"结论：可以跑。{warnings} 个提醒不影响启动（缺的功能会自己降级）。")
    else:
        print("结论：全部就绪。")
    return 1 if problems and not args.allowed else 0


# ── login ─────────────────────────────────────────────────────────────────────


async def _qr_login(settings) -> int:
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

    login = QrCodeLogin()
    # bilibili-api 会把二维码和临时图写到 %TEMP%；有些环境（sandbox / pythonw）
    # 的 TEMP 就是当前目录 —— 临时指到 data/state/tmp，别在仓库根目录拉屎。
    tmp_dir = settings.state_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    old_temp, old_tmp = os.environ.get("TEMP"), os.environ.get("TMP")
    os.environ["TEMP"] = os.environ["TMP"] = str(tmp_dir)
    try:
        await login.generate_qrcode()
        picture = login.get_qrcode_picture()
    finally:
        for key, value in (("TEMP", old_temp), ("TMP", old_tmp)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    qr_path = settings.state_dir / "login-qr.png"
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    qr_path.write_bytes(picture.content)

    print(f"二维码已保存：{qr_path}")
    print("用手机 B站 扫码 → 确认登录（建议勾选「记住我」）。")
    _open_path(qr_path)

    last = None
    deadline = time.time() + 180
    while time.time() < deadline:
        state = await login.check_state()
        if state != last:
            messages = {
                QrCodeLoginEvents.SCAN: "  已扫码，等你在手机上确认…",
                QrCodeLoginEvents.CONF: "  已确认，正在取凭证…",
                QrCodeLoginEvents.TIMEOUT: "  二维码过期了，重新跑一次 live-deck login",
                QrCodeLoginEvents.DONE: "  登录成功",
            }
            print(messages.get(state, f"  {state}"))
            last = state
        if state == QrCodeLoginEvents.DONE:
            credential = login.get_credential()
            data = {
                "sessdata": credential.sessdata,
                "bili_jct": credential.bili_jct,
                "buvid3": credential.buvid3,
                "dedeuserid": credential.dedeuserid,
            }
            settings.credential_file.parent.mkdir(parents=True, exist_ok=True)
            settings.credential_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            try:
                settings.credential_file.chmod(0o600)
            except OSError:
                pass
            print(f"凭证已写入：{settings.credential_file}")
            print(f"账号 uid：{data['dedeuserid'] or '?'}（这个文件等于账号密码，别提交）")
            return 0
        if state == QrCodeLoginEvents.TIMEOUT:
            return 1
        await asyncio.sleep(2)
    print("等了 3 分钟没结果，先退出。")
    return 1


def cmd_login(args: argparse.Namespace) -> int:
    settings = _settings()
    settings.ensure_dirs()
    try:
        return asyncio.run(_qr_login(settings))
    except ImportError:
        print("缺少 bilibili-api-python：pip install -e .")
        return 1


# ── models ────────────────────────────────────────────────────────────────────


def _download(url: str, dest: Path, label: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": f"live-deck/{__version__}"})
    done = 0
    total = 0
    last_percent = -10
    print(f"  下载 {label} -> {dest}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        total = int(response.headers.get("Content-Length") or 0)
        with part.open("wb") as handle:
            while True:
                chunk = response.read(1 << 18)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                percent = int(done * 100 / total) if total else 0
                if percent - last_percent >= 10:
                    last_percent = percent
                    print(f"    {percent:3d}%  {done / 1048576:.0f} MB")
    if total and part.stat().st_size != total:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"下载不完整：{part.stat().st_size} != {total}")
    part.replace(dest)


def _extract_archive(archive: Path, models_dir: Path, final_name: str) -> Path:
    staging = models_dir / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with tarfile.open(archive, "r:bz2") as tar:
        try:
            tar.extractall(staging, filter="data")
        except TypeError:  # Python < 3.12 没有 filter 参数
            tar.extractall(staging)
    tops = [p for p in staging.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise RuntimeError(f"压缩包结构不认识：{[p.name for p in tops]}")
    target = models_dir / final_name
    if target.exists():
        shutil.rmtree(target)
    tops[0].rename(target)
    shutil.rmtree(staging)
    archive.unlink(missing_ok=True)
    return target


def _install_sensevoice(settings, full: bool) -> None:
    target = settings.asr_model_dir
    target.mkdir(parents=True, exist_ok=True)
    wanted = list(SENSEVOICE_FILES)
    if full:
        wanted.append(("model.onnx", "SenseVoice fp32（精修通道用）"))
    for name, label in wanted:
        dest = target / name
        if dest.is_file():
            print(f"  已有 {name}，跳过")
            continue
        _download(f"{SENSEVOICE_BASE}/{name}", dest, label)
    print(f"  完成：{target}")


def _install_streaming(settings) -> None:
    target = settings.streaming_model_dir
    if (target / "tokens.txt").is_file():
        print(f"  已有 {target}，跳过")
        return
    archive = settings.models_dir / "streaming.tar.bz2"
    _download(STREAMING_URL, archive, "流式 zipformer（74 MB）")
    path = _extract_archive(archive, settings.models_dir, STREAMING_DIR)
    print(f"  完成：{path}")


def cmd_models(args: argparse.Namespace) -> int:
    settings = _settings()
    settings.models_dir.mkdir(parents=True, exist_ok=True)

    if args.action == "status":
        for label, path, needed in (
            ("SenseVoice", settings.asr_model_dir, ("tokens.txt", "model.int8.onnx")),
            ("流式", settings.streaming_model_dir, ("tokens.txt",)),
        ):
            missing = [n for n in needed if not (path / n).is_file()]
            if missing:
                print(f"  {label:<12} 没装全  {path}  缺：{', '.join(missing)}")
            else:
                size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
                print(f"  {label:<12} 就绪    {path}  {size / 1048576:.0f} MB")
        return 0

    targets = ["sensevoice", "streaming"] if args.target == "all" else [args.target]
    _head("下载模型")
    try:
        if "sensevoice" in targets:
            _install_sensevoice(settings, args.full)
        if "streaming" in targets:
            _install_streaming(settings)
    except (urllib.error.URLError, RuntimeError, OSError, tarfile.TarError) as exc:
        print(f"下载失败：{exc}")
        print("模型可以手动下，放到 config 里对应的目录即可：")
        print(f"  SenseVoice: {SENSEVOICE_BASE}")
        print(f"  流式:       {STREAMING_URL}")
        return 1
    return 0


# ── 运行 ──────────────────────────────────────────────────────────────────────


def cmd_serve(args: argparse.Namespace) -> int:
    from .deck.server import serve

    asyncio.run(
        serve(
            room=args.room,
            port=args.port,
            asr_engine=args.asr,
            open_browser=not args.no_open,
        )
    )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .deck.server import serve

    print("演示模式：不连 B站、不调模型。Ctrl+C 退出。")
    asyncio.run(serve(room=123456, demo=True, open_browser=not args.no_open))
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from .host import main as host_main

    return host_main(room=args.room)


def _run_module(module: str, rest: list[str]) -> int:
    settings = _settings()
    cmd = [sys.executable, "-m", module, *rest]
    try:
        return subprocess.call(cmd, cwd=str(settings.root), env=_project_python_env())
    except KeyboardInterrupt:
        return 130


def cmd_fish(args: argparse.Namespace) -> int:
    return _run_module("live_deck.fish", list(args.rest))


def cmd_asr(args: argparse.Namespace) -> int:
    return _run_module("live_deck.asr.sensevoice", list(args.rest))


def cmd_persona(args: argparse.Namespace) -> int:
    from .fish.persona import load_persona, persona_stats

    stats = persona_stats()
    text = load_persona()
    print(f"人格文件：{stats['path']}")
    print(f"{stats['chars']} 字 / {stats['lines']} 行")
    if args.full:
        print("-" * 60)
        print(text)
    else:
        print("-" * 60)
        print("\n".join(text.splitlines()[: args.lines]))
        print("…（加 --full 看全文）")
    return 0


# ── eval ─────────────────────────────────────────────────────────────────────


def _eval_paths():
    settings = _settings()
    root = settings.data_dir / "eval"
    return root / "scenes.jsonl", root / "runs"


def _eval_make(ev, settings, args) -> int:
    scenes_path, _ = _eval_paths()
    if scenes_path.exists() and not args.force:
        print(f"{scenes_path} 已经存在。要重建加 --force（旧的会被覆盖）。")
        return 1
    logs = Path(args.logs).expanduser() if args.logs else settings.logs_dir
    danmaku = Path(args.danmaku).expanduser() if args.danmaku else settings.danmaku_dir
    scenes = ev.collect_scenes(logs, danmaku, n=args.n)
    if not scenes:
        print(f"在 {logs} 里没找到可用的字幕日志。")
        print("也可以指到旧项目的日志目录：live-deck eval make --logs <你的 直播日志 目录>")
        return 1
    ev.save_scenes(scenes, scenes_path)
    source = "真实日志" if scenes[0].source == "log" else "内置示例"
    print(f"已生成 {len(scenes)} 个场面（{source}） -> {scenes_path}")
    print("下一步：live-deck eval run")
    return 0


def _ask_score() -> float | None:
    while True:
        raw = input("  1-5 分（s=跳过这句，q=保存并退出）：").strip().lower()
        if raw in ("q", "quit"):
            return None
        if raw in ("s", "skip"):
            return 0.0
        try:
            value = float(raw)
        except ValueError:
            value = 0
        if 1 <= value <= 5:
            return value
        print("  输入 1-5，或 s / q")


async def _eval_run_async(ev, settings, args) -> int:
    from . import bili
    from .fish import engine

    scenes_path, runs_dir = _eval_paths()
    if args.scenes:
        scenes_path = Path(args.scenes).expanduser()
    scenes = ev.load_scenes(scenes_path)
    if not scenes:
        print(f"没有评测场面：{scenes_path}")
        print("先生成：live-deck eval make")
        return 1
    if args.limit and args.limit > 0:
        scenes = scenes[: args.limit]

    key = bili.deepseek_key(required=False)
    if not key:
        print("评测需要生成候选句，得先配 DeepSeek key（live-deck login 不管这个）。")
        return 1

    engine.reload_samples()
    meta = {
        "created": time.time(),
        "model": settings.fish.model,
        "retrieval": settings.fish.retrieval,
        "per_scene": max(1, args.per_scene),
        "auto": bool(args.auto),
        "scenes": str(scenes_path),
    }
    run_path = ev.new_run_path(runs_dir)
    rows: list[dict] = []
    current_room: int | None = None

    print(f"评测 {len(scenes)} 个场面，每个 {meta['per_scene']} 条候选；结果写 {run_path}")
    try:
        for index, scene in enumerate(scenes, 1):
            if scene.room.isdigit() and int(scene.room) != current_room:
                current_room = int(scene.room)
                engine.prepare_room(current_room)
            replies: list[str] = []
            for _ in range(meta["per_scene"]):
                try:
                    replies.append(await engine.ask_model(key, scene.context))
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{index}] 生成失败：{exc}")
                    replies.append("")

            for position, reply in enumerate(replies, 1):
                if not reply or reply.upper().startswith("SKIP"):
                    rows.append(
                        {"scene": scene.id, "reply": "", "score": 0, "skipped": True, "auto": meta["auto"]}
                    )
                    print(f"  [{index}] 候选 {position}：SKIP")
                    continue
                if args.auto:
                    score = await engine.judge_human(key, scene.context, reply)
                else:
                    print()
                    print(f"[{index}/{len(scenes)}] {scene.id}  {scene.time}  room={scene.room}")
                    print(scene.context[-700:])
                    print(f"  候选 {position}：{reply}")
                    score = _ask_score()
                    if score is None:
                        raise KeyboardInterrupt
                rows.append(
                    {
                        "scene": scene.id,
                        "reply": reply,
                        "score": float(score),
                        "skipped": float(score) <= 0,
                        "auto": meta["auto"],
                    }
                )
    finally:
        ev.save_run(run_path, meta, rows)
        stats = ev.summarize(rows)
        print()
        print(f"已保存 {len(rows)} 条 -> {run_path}")
        print(
            f"均分 {stats['mean']}/5（{stats['scored']} 条），"
            f"跳过 {stats['skip_rate']:.0%}，平均 {stats['avg_len']} 字"
        )
    return 0


def _eval_run(ev, settings, args) -> int:
    try:
        return asyncio.run(_eval_run_async(ev, settings, args))
    except KeyboardInterrupt:
        print("\n已中断；已经打的分都保存了。")
        return 130


def _eval_report(ev, settings, args) -> int:
    _, runs_dir = _eval_paths()
    runs = sorted(runs_dir.glob("*.jsonl"))
    if not runs:
        print(f"还没有评测记录：{runs_dir}")
        print("先 live-deck eval make，再 live-deck eval run")
        return 1
    runs = runs[-max(1, args.runs) :]
    print(f"{'时间':<16} {'模型':<16} {'检索':<5} {'条数':>4} {'均分':>5} {'跳过':>4} {'长度':>4}")
    previous: float | None = None
    for path in runs:
        meta, rows = ev.read_run(path)
        stats = ev.summarize(rows)
        stamp = time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        model = str(meta.get("model") or "?")
        retrieval = "开" if meta.get("retrieval") else "关"
        delta = ""
        if previous is not None:
            delta = f" ({stats['mean'] - previous:+.2f})"
        print(
            f"{stamp:<16} {model:<16} {retrieval:<5} {stats['scored']:>4} "
            f"{stats['mean']:>5}{delta} {stats['skip_rate']:>4.0%} {stats['avg_len']:>4}"
        )
        previous = float(stats["mean"])
    print()
    print("注：均分只统计打了分的候选；跳过率 = SKIP 的比例。不同次的场面固定，才可比。")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import eval as ev

    settings = _settings()
    action = getattr(args, "eval_action", None) or "report"
    if action == "make":
        return _eval_make(ev, settings, args)
    if action == "run":
        return _eval_run(ev, settings, args)
    return _eval_report(ev, settings, args)


# ── 入口 ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live-deck",
        description="B站 直播间的本地副驾驶：弹幕、字幕、画面理解和一位会沉默的观众。",
    )
    parser.add_argument("--version", action="version", version=f"live-deck {__version__}")
    parser.add_argument("--config", default=None, help="config.toml 路径（默认找当前目录）")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="生成 config.toml 和 data/ 目录")
    p.add_argument("--dir", default=None, help="生成到哪个目录（默认当前目录）")
    p.add_argument("--force", action="store_true", help="覆盖已有配置（先备份）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="体检：缺什么、怎么修")
    p.add_argument("--allowed", action="store_true", help="即使有问题也返回 0（CI 用）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("login", help="B站 扫码登录，写凭证文件")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("models", help="字幕模型：下载 / 查看")
    p.add_argument("action", nargs="?", choices=("install", "status"), default="status")
    p.add_argument("target", nargs="?", choices=("sensevoice", "streaming", "all"), default="all")
    p.add_argument("--full", action="store_true", help="SenseVoice 额外下 fp32 权重（约 900 MB）")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("serve", help="起服务端（浏览器）")
    p.add_argument("--room", type=int, default=0)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--asr", choices=("sensevoice", "streaming"), default=None)
    p.add_argument("--no-open", action="store_true", help="不要自动开浏览器")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("demo", help="演示模式：不连 B站、不调模型，只看界面")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("app", help="桌面窗口（没有 pywebview 时自动用浏览器）")
    p.add_argument("--room", type=int, default=0)
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("fish", help="单独跑直播鱼（参数透传）")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_fish)

    p = sub.add_parser("asr", help="单独跑字幕（参数透传）")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_asr)

    p = sub.add_parser("persona", help="看人格文件")
    p.add_argument("--full", action="store_true")
    p.add_argument("--lines", type=int, default=15)
    p.set_defaults(func=cmd_persona)

    p = sub.add_parser("eval", help="离线评测：固定场面、盲评、比较版本")
    eval_sub = p.add_subparsers(dest="eval_action")
    make = eval_sub.add_parser("make", help="从字幕日志生成固定场面")
    make.add_argument("--n", type=int, default=40)
    make.add_argument("--logs", default=None, help="字幕日志目录（默认 data/logs）")
    make.add_argument("--danmaku", default=None, help="弹幕目录（默认 data/danmaku）")
    make.add_argument("--force", action="store_true")
    make.set_defaults(func=cmd_eval)
    run = eval_sub.add_parser("run", help="生成候选并打分")
    run.add_argument("--per-scene", type=int, default=1)
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--auto", action="store_true", help="用评审器自动打分（不手动）")
    run.add_argument("--scenes", default=None)
    run.set_defaults(func=cmd_eval)
    report = eval_sub.add_parser("report", help="比较最近几次评测")
    report.add_argument("--runs", type=int, default=3)
    report.set_defaults(func=cmd_eval)
    p.set_defaults(func=cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.config:
        os.environ["LIVE_DECK_CONFIG"] = str(Path(args.config).expanduser().resolve())
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"配置有问题：{exc}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
