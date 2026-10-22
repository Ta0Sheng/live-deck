"""桌面窗口宿主：优先用 pywebview（WebView2），装不上就退回浏览器。

``live-deck app`` 走这里：

1. 8765 没人在听 → 在后台把服务端拉起来（无控制台窗口）；
2. 等端口就绪 → 开一个真正的应用窗口（不是浏览器套壳）；
3. 关窗口 = 退出：连同服务端、字幕、直播鱼一起收掉。

没有 pywebview（或它在当前 Python 上装不上）时，退化为「起服务 + 打开默认浏览器」，
功能一模一样，只是窗口是浏览器的。这条退路让 macOS / Linux 也能用。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser

from .config import get_settings

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RECHARGE_URL = "https://link.bilibili.com/p/center/index#/user-center/my-info/operation"


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _child_env() -> dict[str, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    src = str(get_settings().root / "src")
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if src not in parts:
        env["PYTHONPATH"] = os.pathsep.join([src, *parts])
    return env


def _start_server(room: int, port: int) -> subprocess.Popen:
    settings = get_settings()
    settings.ensure_dirs()
    log = open(settings.logs_dir / "server.log", "ab")
    python = settings.deck.python or sys.executable
    cmd = [
        python, "-m", "live_deck.deck.server",
        "--room", str(room), "--port", str(port),
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(settings.root),
        env=_child_env(),
        stdout=log,
        stderr=log,
        creationflags=CREATE_NO_WINDOW,
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    """关窗口时把孙子进程（字幕、直播鱼）一起收掉，别留孤儿。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


class Api:
    """给页面用的一小组接口（JS 里 ``window.pywebview.api.xxx`` 调）。"""

    def open_recharge(self) -> bool:
        """在程序里再开一个窗口打开 B站 充值页，不经过系统浏览器。"""
        import webview

        webview.create_window(
            "B站 充值",
            RECHARGE_URL,
            width=1000,
            height=820,
            min_size=(720, 560),
            background_color="#ffffff",
        )
        return True


def _run_in_browser(url: str, proc: subprocess.Popen | None) -> int:
    """没有原生窗口时的退路：开浏览器，服务端继续跑。"""
    print("没有可用的原生窗口，改用默认浏览器。")
    print(f"  地址：{url}")
    webbrowser.open(url)
    if proc is None:
        return 0
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _kill_tree(proc)
    return 0


def main(room: int = 0) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    port = settings.deck.port

    if not room:
        try:
            room = int(settings.last_room_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            room = 0

    proc: subprocess.Popen | None = None
    if port_open(port):
        print(f"{port} 已经有服务在跑，直接开窗口")
    else:
        proc = _start_server(room, port)
        for _ in range(120):
            if port_open(port):
                break
            time.sleep(0.25)
        else:
            print(f"服务没起来，看 {settings.logs_dir / 'server.log'}")
            _kill_tree(proc)
            return 1

    url = f"http://127.0.0.1:{port}/?v={int(time.time())}"

    try:
        import webview
    except ImportError:
        return _run_in_browser(url, proc)

    def _browser_fallback(reason: str) -> int:
        print(f"原生窗口起不来（{reason}），改用浏览器。")
        return _run_in_browser(url, proc)

    storage = settings.state_dir / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.create_window(
            "直播互动台",
            url,
            width=1380,
            height=900,
            min_size=(980, 620),
            background_color="#0d1117",
            text_select=True,
            js_api=Api(),
            on_top=False,
        )
        webview.start(
            gui="edgechromium" if os.name == "nt" else None,
            private_mode=False,
            storage_path=str(storage),
        )
    except Exception as exc:  # pywebview 的 pythonnet 后端在新 Python 上可能直接崩
        return _browser_fallback(f"{type(exc).__name__}: {exc}")

    if proc is not None:
        _kill_tree(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
