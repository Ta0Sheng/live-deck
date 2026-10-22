"""直播字幕 v2：PyAV 拉流 → Demucs 人声 → 能量切句 → SenseVoice。

比 v1 慢（实时 ~8 秒），但错字少得多；另有一路 fp32 + 长上下文的「精修」句，
每 30 秒回看一次，写完再交给服务端做同音字纠错。

直接跑::

    python -m live_deck.asr.sensevoice 123456 --seconds 60
"""

from __future__ import annotations

import argparse
import re
import threading
import time
from collections import deque

try:
    import av
    import numpy as np
    import sherpa_onnx
    import torch
    import torchaudio
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
except ImportError as exc:  # 没装 [asr] / [asr-gpu] 时给出人话
    raise SystemExit(
        f"缺少字幕依赖（{exc.name}）。装一下：pip install -e .[asr-gpu]（只跑 CPU 用 .[asr]）"
    ) from exc

from .. import bili
from ..config import get_settings
from . import streaming as base

S = get_settings()
MODEL_DIR = S.asr_model_dir


def build_recognizer(fp32: bool = False, threads: int = 4):
    """fp32 = 精修通道用的全精度模型；权重不在就退回 int8，别让整路字幕挂掉。"""
    if not MODEL_DIR.is_dir():
        raise SystemExit(
            f"找不到字幕模型：{MODEL_DIR}（跑 live-deck models install）"
            "  跑 `live-deck models install sensevoice`，"
            "或改 config.toml 里的 asr.model_dir"
        )
    rel = "model.onnx" if fp32 else "model.int8.onnx"
    if fp32 and not (MODEL_DIR / rel).is_file():
        print(f"[{base.stamp()}] 没有 fp32 权重，精修通道退回 int8", flush=True)
        rel = "model.int8.onnx"
    if not (MODEL_DIR / rel).is_file():
        raise SystemExit(
            f"缺少模型文件：{MODEL_DIR / rel}（跑 live-deck models install）"
            "  跑 `live-deck models install sensevoice`"
        )
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL_DIR / rel),
        tokens=str(MODEL_DIR / "tokens.txt"),
        use_itn=True,
        language="zh",
        num_threads=threads,
    )


def other_asr_pid(room: int) -> int | None:
    """同房间是不是已经有一个 ASR 在跑（防孤儿重复拉流）。"""
    try:
        import os

        import psutil
    except Exception:
        return None
    me = os.getpid()
    parent = os.getppid()
    for p in psutil.process_iter(["pid", "cmdline"]):
        pid = p.info["pid"]
        if pid in (me, parent):
            continue
        cl = " ".join(p.info["cmdline"] or [])
        if "live_asr_sv.py" in cl and f" {room}" in cl:
            return pid
    return None

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("room", type=int)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--chunk", type=float, default=S.asr.chunk_seconds)
    ap.add_argument("--clean-every", type=float, default=S.asr.clean_every,
                    help="精修通道多久出一段（秒）")
    ap.add_argument("--clean-window", type=float, default=S.asr.clean_window,
                    help="精修时回看多少秒音频")
    ap.add_argument("--no-clean", dest="clean", action="store_false", default=S.asr.clean)
    a = ap.parse_args()

    dup = other_asr_pid(a.room)
    if dup:
        print(f"[{base.stamp()}] 已有 ASR 在跑（pid={dup}），这个不重复拉流", flush=True)
        return 0
    rid = bili.resolve_room(a.room)
    url, kind = bili.play_url(rid)
    print(f"[{base.stamp()}] room {a.room} -> {rid} {kind}", flush=True)

    rec = build_recognizer(threads=S.asr.threads)
    # 精修通道：全精度 + 36 秒上下文，慢一点但准得多；和实时那路互不阻塞
    rec_clean = build_recognizer(fp32=True, threads=2) if a.clean else None
    model = get_model("htdemucs")
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    vidx = model.sources.index("vocals")
    print(f"[{base.stamp()}] SenseVoice + Demucs 就绪（{device}）", flush=True)

    container = av.open(url, options={
        "headers": (f"Referer: https://live.bilibili.com/{rid}\r\n"
                    f"User-Agent: {bili.UA}\r\n"
                    f"Cookie: {bili.cookie_header(required=False)}\r\n"),
        "rw_timeout": "8000000",
    })
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)

    sr = 44100
    need = int(sr * a.chunk)
    buf = np.zeros(0, dtype=np.float32)
    silence = 0.0
    started = time.time()

    def decode(x: np.ndarray):
        if len(x) < 16000 * 0.8:
            return
        s = rec.create_stream()
        s.accept_waveform(16000, x)
        rec.decode_stream(s)
        t = (s.result.text or "").strip()
        if t:
            print(f"[{base.stamp()}] {t}", flush=True)

    # ── 精修通道：回看 36 秒 + fp32，慢几十秒但字准得多 ────────────────────
    clean_ring: deque[tuple[float, np.ndarray]] = deque()
    clean_lock = threading.Lock()
    clean_state = {"busy": False, "last": 0.0}

    def split_sent(text: str) -> list[str]:
        return [p.strip() for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]

    def clean_n_samples() -> int:
        with clean_lock:
            return sum(len(x) for _, x in clean_ring)

    def run_clean() -> None:
        try:
            with clean_lock:
                items = list(clean_ring)
            if not items:
                return
            seg = np.concatenate([x for _, x in items])[-int(16000 * a.clean_window):]
            # 这行字对应的"说出来的时刻"：往前数够 seg 的长度
            acc = 0
            start_wall = items[-1][0]
            for w, x in reversed(items):
                acc += len(x)
                start_wall = w
                if acc >= len(seg):
                    break
            s = rec_clean.create_stream()
            s.accept_waveform(16000, np.ascontiguousarray(seg))
            rec_clean.decode_stream(s)
            txt = (s.result.text or "").strip()
            stamp = time.strftime("%H:%M:%S", time.localtime(start_wall))
            for sent in split_sent(txt):
                print(f"[{stamp}] #clean {sent}", flush=True)
            clean_state["last"] = time.time()
        except Exception as e:  # noqa: BLE001
            print(f"[{base.stamp()}] #clean-error {e}", flush=True)
        finally:
            clean_state["busy"] = False

    pending = []
    pending_n = 0
    try:
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                arr = rf.to_ndarray().astype(np.float32) / 32768.0
                # PyAV 对打包格式给的是 (1, 2n) 的交错采样（L0 R0 L1 R1 …），
                # 不是 (2, n)。当成双声道喂 Demucs 会把音频拉成两倍长、字全糊。
                if arr.ndim == 1 or arr.shape[0] == 1:
                    arr = arr.reshape(-1, 2).T
                pending.append(arr)
                pending_n += arr.shape[1]
                if pending_n < need:
                    continue
                chunk = np.concatenate(pending, axis=1)[:, :need]
                pending, pending_n = [], 0
                wav = torch.from_numpy(chunk)
                with torch.no_grad():
                    src = apply_model(model, wav[None], device=device, progress=False)[0]
                voc = src[vidx].mean(0)
                m16 = torchaudio.functional.resample(voc, sr, 16000).numpy()
                buf = np.concatenate([buf, m16])

                if rec_clean is not None:
                    with clean_lock:
                        clean_ring.append((time.time(), m16))
                        while sum(len(x) for _, x in clean_ring) > 16000 * (a.clean_window + 12):
                            clean_ring.popleft()
                    if (
                        not clean_state["busy"]
                        and time.time() - clean_state["last"] >= a.clean_every
                        and clean_n_samples() >= 16000 * a.clean_window * 0.9
                    ):
                        clean_state["busy"] = True
                        threading.Thread(target=run_clean, daemon=True).start()

                step = 1600
                i = 0
                while i + step <= len(m16):
                    seg = m16[i:i + step]
                    rms = float(np.sqrt(np.mean(seg ** 2)))
                    silence = silence + 0.1 if rms < 0.012 else 0.0
                    i += step
                    if silence >= 0.5 and len(buf) >= 16000 * 1.5:
                        cut = len(buf) - int(16000 * 0.4)
                        decode(buf[:cut])
                        buf = np.zeros(0, dtype=np.float32)
                        silence = 0.0
                        break
                if len(buf) >= 16000 * 12:
                    decode(buf)
                    buf = np.zeros(0, dtype=np.float32)
                    silence = 0.0

            if a.seconds and time.time() - started > a.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            container.close()
        except Exception:
            pass
    if len(buf) >= 16000:
        decode(buf)
    print(f"[{base.stamp()}] 结束", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())