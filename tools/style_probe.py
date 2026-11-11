#!/usr/bin/env python3
"""语气对照探针：同一句台词，两种语气 × 两条合成路径。

在项目根目录运行：
    python3 tools/style_probe.py

产出写到 samples/style-compare/（四个 wav + result.json）。
用来确认 MiMo 的流式模式是否仍然忽略 style——接口哪天修好了，脚本会直接反映出来。
"""
import hashlib
import io
import json
import os
import struct
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "samples", "style-compare")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from brain.replier import load_dotenv  # noqa: E402

load_dotenv()
import voice  # noqa: E402

TEXT = "你别急啊，我这不正在数钱嘛。"
STYLES = [
    ("fast", "极快 · 咋呼", "语速极快，声音拔高，像被人踩了尾巴一样咋呼"),
    ("slow", "极慢 · 有气无力", "语速极慢，声音压得很低，有气无力，像熬了三天夜"),
]


def pcm_to_wav(pcm, rate=voice.PCM_RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_info(blob):
    with wave.open(io.BytesIO(blob), "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    return rate, struct.unpack("<%dh" % (len(raw) // 2), raw), n / float(rate)


def median_pitch(samples, rate):
    win = int(rate * 0.04)
    hop = int(rate * 0.01)
    lo, hi = int(rate / 400.0), int(rate / 80.0)
    peak = max(abs(s) for s in samples) or 1
    found = []
    for start in range(0, len(samples) - win, hop):
        frame = samples[start:start + win]
        if sum(abs(s) for s in frame) / win < peak * 0.12:
            continue
        best, best_lag = 0.0, 0
        for lag in range(lo, hi):
            acc = sum(frame[i] * frame[i + lag] for i in range(0, win - lag, 4))
            if acc > best:
                best, best_lag = acc, lag
        if best_lag:
            found.append(rate / float(best_lag))
    if not found:
        return 0.0
    found.sort()
    return found[len(found) // 2]


def run_concat(text, style):
    voice._cache.clear()
    t0 = time.perf_counter()
    pcm = b"".join(voice.stream_pcm(text, "mimo_soda", style))
    return pcm_to_wav(pcm), int((time.perf_counter() - t0) * 1000)


def envelope(samples, buckets=120):
    """压成一条振幅包络，给页面画波形用。"""
    if not samples:
        return []
    step = max(1, len(samples) // buckets)
    out = []
    for i in range(buckets):
        chunk = samples[i * step:(i + 1) * step]
        if not chunk:
            out.append(0)
            continue
        out.append(max(abs(s) for s in chunk))
    top = max(out) or 1
    return [round(v * 100.0 / top) for v in out]


def run_whole(text, style):
    voice._cache.clear()
    t0 = time.perf_counter()
    blob, _mime, used = voice.synthesize(text, "mimo_soda", style)
    if used != "mimo_soda":
        raise SystemExit("意外降级到 %s" % used)
    return blob, int((time.perf_counter() - t0) * 1000)


def main():
    os.makedirs(OUT, exist_ok=True)
    voice.warm()
    rows = []
    for key, label, style in STYLES:
        for mode, mode_label, fn in (("stream", "流式（改前）", run_concat),
                                     ("whole", "非流式（改后）", run_whole)):
            blob, ms = fn(TEXT, style)
            rate, samples, dur = wav_info(blob)
            f0 = median_pitch(samples, rate)
            name = "%s-%s.wav" % (key, mode)
            with open(os.path.join(OUT, name), "wb") as f:
                f.write(blob)
            rows.append({"key": key, "style": label, "mode": mode, "mode_label": mode_label,
                         "file": name, "ms": ms, "dur": round(dur, 3), "f0": round(f0, 1),
                         "env": envelope(samples),
                         "md5": hashlib.md5(blob).hexdigest()[:8]})
            print("%-16s %-14s 请求%5dms  音频%.2fs  基频%6.1fHz  %s"
                  % (label, mode_label, ms, dur, f0, name))
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump({"text": TEXT, "rows": rows}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
