"""直播字幕 v1：流式 zipformer，边说边出字（快，但串字多，适合纯杂谈）。

直接跑::

    python -m live_deck.asr.streaming 123456 --seconds 60
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import av
    import numpy as np
    import sherpa_onnx
except ImportError as exc:  # 没装 [asr] 时给出人话，而不是 traceback
    raise SystemExit(
        f"缺少字幕依赖（{exc.name}）。装一下：pip install -e .[asr]（GPU 用 .[asr-gpu]）"
    ) from exc

from .. import bili
from ..config import get_settings

S = get_settings()
MODEL_DIR = S.streaming_model_dir


def build_recognizer() -> sherpa_onnx.OnlineRecognizer:
    """加载流式模型。缺权重时明确报错，不要静默跑空。"""
    if not MODEL_DIR.is_dir():
        raise SystemExit(
            f"找不到字幕模型：{MODEL_DIR}（跑 live-deck models install）"
            "  跑 `live-deck models install streaming`，"
            "或改 config.toml 里的 asr.streaming_model_dir"
        )
    # LIVE_ASR_MODEL=fp32 可换全精度权重（同一个目录里就有，便于 A/B）
    suffix = "" if os.environ.get("LIVE_ASR_MODEL") == "fp32" else ".int8"
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(MODEL_DIR / "tokens.txt"),
        encoder=str(MODEL_DIR / f"encoder-epoch-99-avg-1{suffix}.onnx"),
        decoder=str(MODEL_DIR / f"decoder-epoch-99-avg-1{suffix}.onnx"),
        joiner=str(MODEL_DIR / f"joiner-epoch-99-avg-1{suffix}.onnx"),
        num_threads=2,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=1.2,
        rule3_min_utterance_length=20.0,
    )


def stamp() -> str:
    return time.strftime("%H:%M:%S")


# ── 人声分离（2026-10-22）：BGM 会跟着混音一起被识别成乱码 ──────────────────
# Demucs htdemucs 在 3050 上实测：3 秒音频 230ms（13x 实时），显存 0.54GB。
# 切片跑，代价是识别链路多等「半片 + 处理时间」。
def build_separator(chunk_s: float):
    try:
        import torch
        from demucs.pretrained import get_model

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = get_model("htdemucs").to(device).eval()
        print(f"[{stamp()}] Demucs 已加载（{device}），切片 {chunk_s}s", flush=True)
        return model, device
    except Exception as e:
        print(f"[{stamp()}] Demucs 加载失败，退回原始音频：{e}", flush=True)
        return None, "cpu"


def vocals_of(model, device, chunk: np.ndarray, sr_in: int) -> np.ndarray:
    """mono float32 @sr_in → 人声 mono float32 @16000。"""
    import torch
    from demucs.apply import apply_model

    x = torch.from_numpy(chunk).float().view(1, 1, -1).repeat(1, 2, 1).to(device)
    with torch.no_grad():
        out = apply_model(model, x, device=device, progress=False)
    vocals = out[0][-1].mean(0).cpu().numpy()  # 最后一轨就是 vocals
    if sr_in != 16000 and len(vocals) > 1:
        n = int(len(vocals) * 16000 / sr_in)
        vocals = np.interp(np.linspace(0, len(vocals) - 1, n), np.arange(len(vocals)), vocals).astype(np.float32)
    return vocals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("room", type=int, help="直播间号（短号或真实号都行）")
    ap.add_argument("--seconds", type=float, default=0, help="跑多少秒后自动停（0=一直跑）")
    ap.add_argument("--separate", dest="separate", action="store_true",
                    default=S.asr.separate, help="用 Demucs 分离人声再识别（默认开）")
    ap.add_argument("--no-separate", dest="separate", action="store_false",
                    help="关掉人声分离，直接用混音识别")
    ap.add_argument("--sep-chunk", type=float, default=2.0,
                    help="分离切片长度（秒）；越小延迟越低、分离上下文越短")
    args = ap.parse_args()

    room_id = bili.resolve_room(args.room)
    url, kind = bili.play_url(room_id)
    print(f"[{stamp()}] 房间 {args.room} -> {room_id}，流类型 {kind}", flush=True)

    rec = build_recognizer()
    print(f"[{stamp()}] 模型已加载，开始识别（Ctrl+C 停）", flush=True)

    sep_model, sep_dev = (None, "cpu")
    if args.separate:
        sep_model, sep_dev = build_separator(args.sep_chunk)

    container = av.open(url, options={
        "headers": (f"Referer: https://live.bilibili.com/{room_id}\r\n"
                    f"User-Agent: {bili.UA}\r\n"
                    f"Cookie: {bili.cookie_header(required=False)}\r\n"),
        "rw_timeout": "8000000",
    })
    stream = rec.create_stream()

    def pump(samples: np.ndarray) -> None:
        """把一段 16k 人声喂进识别器，顺便吐字幕。"""
        nonlocal last_partial
        stream.accept_waveform(16000, samples)
        while rec.is_ready(stream):
            rec.decode_stream(stream)
        text = rec.get_result(stream)
        now = time.time()
        if text and now - last_partial > 0.4:
            last_partial = now
            sys.stderr.write(f"\r  … {text}")
            sys.stderr.flush()
        if rec.is_endpoint(stream):
            final = rec.get_result(stream).strip()
            if final:
                sys.stderr.write("\r" + " " * 60 + "\r")
                sys.stderr.flush()
                print(f"[{stamp()}] {final}", flush=True)
            rec.reset(stream)

    sr_in = 44100 if sep_model is not None else 16000
    resampler = av.AudioResampler(
        format="flt" if sep_model is not None else "s16", layout="mono", rate=sr_in
    )
    chunk_n = int(sr_in * args.sep_chunk)
    buf = np.zeros(0, dtype=np.float32)

    started = time.time()
    last_partial = 0.0
    try:
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                samples = rf.to_ndarray().reshape(-1).astype(np.float32)
                if sep_model is None:
                    # 没开分离：s16 → [-1,1]
                    pump(samples / 32768.0)
                    continue
                buf = np.concatenate([buf, samples])
                while len(buf) >= chunk_n:
                    piece = buf[:chunk_n]
                    buf = buf[chunk_n:]
                    try:
                        piece = vocals_of(sep_model, sep_dev, piece, sr_in)
                    except Exception as e:
                        print(f"[{stamp()}] 分离失败（用原音）：{e}", flush=True)
                    pump(piece)
            if args.seconds and time.time() - started > args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            container.close()
        except Exception:
            pass
    print(f"[{stamp()}] 结束", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
