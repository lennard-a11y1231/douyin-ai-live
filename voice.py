#!/usr/bin/env python3
"""把文字变成声音。

两个引擎，页面上可以随时切换：

  mimo  —— 小米 MiMo-V2.5-TTS。音色自然，官方已支持低延迟流式（pcm16 分块返回）。
           但每次请求有 2.5~3 秒的固定前置开销，且要复用 HTTPS 连接才划算
           （小米域名 TLS 握手要 0.8 秒，每次重连等于白扔近 1 秒）。
  say   —— macOS 自带语音。0.5 秒出音，音色机械，用来兜底和快速预览。

mimo 失败、超时、没配 key 时自动降级到 say，直播间不会因为一次网络抖动就哑掉。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections import OrderedDict

SAY = "/usr/bin/say"
FALLBACK_PRESET = "tingting"
PCM_RATE = 24000  # MiMo 流式输出固定 24kHz 单声道 PCM16LE
STYLE_LIMIT = 80  # 语气指示最长留这么多字，模型话痨时不至于把请求撑爆

MIMO_HOST = "api.xiaomimimo.com"
MIMO_PATH = "/v1/chat/completions"

# 大肥鱼的说话方式。作为 user 消息传给 MiMo，只影响语气不影响内容。
FISH_STYLE = (
    "二十多岁的年轻人，语速偏快，语气松弛，带一点痞气的笑意，"
    "像在跟朋友抬杠，口语化，不要播音腔，不要刻意卖萌。"
)

# engine 决定后端；segment 决定前端要不要按句切开合成。
#   segment=True  —— 系统语音固定开销小，切得越细越早出声
#   segment=False —— MiMo 前置开销大，整段一次合成更连贯也更省
#   stream=True   —— 走流式，边生成边播，长句子能省几秒
PRESETS = OrderedDict(
    [
        ("mimo_soda", {"label": "苏打 · MiMo 男声", "engine": "mimo", "voice": "苏打", "style": FISH_STYLE,
                       "segment": False, "stream": True}),
        ("mimo_ice", {"label": "冰糖 · MiMo 女声", "engine": "mimo", "voice": "冰糖", "style": FISH_STYLE,
                      "segment": False, "stream": True}),
        ("mimo_jasmine", {"label": "茉莉 · MiMo 女声", "engine": "mimo", "voice": "茉莉", "style": FISH_STYLE,
                          "segment": False, "stream": True}),
        ("mimo_birch", {"label": "白桦 · MiMo 男声", "engine": "mimo", "voice": "白桦", "style": FISH_STYLE,
                        "segment": False, "stream": True}),
        ("tingting", {"label": "婷婷 · 系统（秒出）", "engine": "say", "voice": "Tingting", "rate": 195,
                      "segment": True, "stream": False}),
        ("flo", {"label": "Flo · 系统", "engine": "say", "voice": "Flo (Chinese (China mainland))",
                 "rate": 200, "segment": True, "stream": False}),
        ("sandy", {"label": "Sandy · 系统", "engine": "say", "voice": "Sandy (Chinese (China mainland))",
                   "rate": 205, "segment": True, "stream": False}),
        ("meijia", {"label": "美佳 · 系统", "engine": "say", "voice": "Meijia", "rate": 195,
                    "segment": True, "stream": False}),
    ]
)

DEFAULT_PRESET = "mimo_soda"
MAX_CACHE = 128

_cache = OrderedDict()
_cache_lock = threading.Lock()

# 每个线程一条长连接。ThreadingHTTPServer 每请求一个线程，正好对应得上。
_local = threading.local()

_NOISE = str.maketrans({ch: None for ch in "*#`~_[]|"})


class VoiceError(Exception):
    """合成失败，调用方应该降级成「只出字不出声」，不要中断直播。"""


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def default_preset():
    """默认音色。每次现读环境变量，改 .env 里的 VOICE_PRESET 不用重启。"""
    name = _env("VOICE_PRESET", DEFAULT_PRESET)
    return name if name in PRESETS else DEFAULT_PRESET


def _clean(text):
    text = (text or "").translate(_NOISE).strip()
    text = text.replace("……", "。").replace("…", "。")
    return text


def _cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        _cache.move_to_end(key)
        return item


def _cache_put(key, value):
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE:
            _cache.popitem(last=False)


# ---------------- HTTP 长连接 ----------------

def _conn(timeout):
    """当前线程复用的 HTTPS 连接。没有就建一条。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = http.client.HTTPSConnection(MIMO_HOST, timeout=timeout)
        _local.conn = conn
    return conn


def _drop_conn():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass
        _local.conn = None


def _mimo_send(payload, timeout, stream):
    """发一次请求，返回 (resp, conn)。连接出错时重连重试一次（keep-alive 过期很常见）。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "api-key": _env("MIMO_API_KEY"),
        "Authorization": "Bearer " + _env("MIMO_API_KEY"),
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in (1, 2):
        conn = _conn(timeout)
        try:
            conn.request("POST", MIMO_PATH, raw, headers)
            resp = conn.getresponse()
            if stream:
                return resp, conn
            body = resp.read()
            if resp.will_close:
                _drop_conn()
            return body, conn
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            last_error = exc
            _drop_conn()
            if attempt == 2:
                break
    raise VoiceError("连不上 MiMo：%s" % (last_error,))


def _explain(status, body):
    hint = {
        400: "请求格式有问题（检查 messages / audio 结构）",
        401: "MiMo 的 API Key 无效或已过期",
        402: "MiMo 账户余额不足",
        403: "没有该模型的权限",
        404: "接口地址不对，检查 MIMO_BASE_URL",
        429: "MiMo 限流，稍后再试",
    }.get(status, "MiMo 返回 %d" % status)
    return "%s：%s" % (hint, body[:160])


def _mimo_payload(text, preset, stream, style=None):
    """组请求。style 是语气指示，由 DeepSeek 按句生成，放在 user 消息里。

    MiMo 的约定就是：user 说「该怎么念」，assistant 说「念什么」。
    style 为空时退回音色的默认风格，保证任何时候都有语气。
    """
    style = (style or preset.get("style") or "").strip()[:STYLE_LIMIT]
    messages = []
    if style:
        messages.append({"role": "user", "content": style})
    messages.append({"role": "assistant", "content": text})
    return {
        "model": _env("MIMO_TTS_MODEL", "mimo-v2.5-tts"),
        "messages": messages,
        "audio": {"format": "pcm16" if stream else "wav", "voice": preset["voice"]},
        "stream": bool(stream),
    }


def _synth_mimo(text, preset, style=None):
    if not _env("MIMO_API_KEY"):
        raise VoiceError("没有配置 MIMO_API_KEY")

    timeout = float(_env("MIMO_TIMEOUT", "20"))
    raw, _ = _mimo_send(_mimo_payload(text, preset, False, style), timeout, False)

    try:
        data = json.loads(raw)
    except ValueError:
        raise VoiceError("MiMo 返回的不是 JSON")

    if isinstance(data, dict) and data.get("error"):
        raise VoiceError("MiMo 报错：" + str(data["error"])[:160])

    try:
        encoded = data["choices"][0]["message"]["audio"]["data"]
    except (KeyError, IndexError, TypeError):
        raise VoiceError("MiMo 返回结构不对：" + json.dumps(data, ensure_ascii=False)[:160])

    if not encoded:
        raise VoiceError("MiMo 返回了空音频（可能命中内容过滤）")

    blob = base64.b64decode(encoded)
    if len(blob) < 128:
        raise VoiceError("MiMo 音频过短")
    return blob, "audio/wav"


def stream_pcm(text, name=None, style=None):
    """流式合成，逐块 yield 24kHz 单声道 PCM16。供前端边收边播。

    出错时抛 VoiceError，但**只在第一个字节之前**才有意义——
    已经开始往外吐音频后再失败，调用方只能把这一句截断。
    """
    text = _clean(text)
    if not text:
        return

    preset = PRESETS[name if name in PRESETS else default_preset()]
    if preset.get("engine") != "mimo":
        raise VoiceError("这个音色不支持流式")
    if not _env("MIMO_API_KEY"):
        raise VoiceError("没有配置 MIMO_API_KEY")

    timeout = float(_env("MIMO_TIMEOUT", "20"))
    resp, conn = _mimo_send(_mimo_payload(text, preset, True, style), timeout, True)

    finished = False
    try:
        if resp.status != 200:
            body = resp.read()
            _drop_conn()
            raise VoiceError(_explain(resp.status, body.decode("utf-8", "ignore")))

        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]":
                finished = True
                break
            try:
                event = json.loads(chunk)
            except ValueError:
                continue
            delta = ((event.get("choices") or [{}])[0].get("delta") or {})
            audio = delta.get("audio") or {}
            encoded = audio.get("data")
            if encoded:
                yield base64.b64decode(encoded)
        finished = True
    finally:
        # 没读完就退出（前端断开）时，连接里还有残留数据，不能还给连接池
        if not finished:
            _drop_conn()


# ---------------- 系统语音 ----------------

def _synth_say(text, preset):
    if not os.path.exists(SAY):
        raise VoiceError("这台机器上没有 say 命令，系统语音只在 macOS 上可用")

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="fish_")
    os.close(fd)
    try:
        cmd = [
            SAY, "-v", preset["voice"], "-r", str(preset["rate"]),
            "-o", path, "--file-format=WAVE", "--data-format=LEI16@22050", text,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
        if proc.returncode != 0:
            raise VoiceError("系统语音合成失败：" + proc.stderr.decode("utf-8", "ignore")[:120])
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 128:
            raise VoiceError("合成结果为空")
        return data, "audio/wav"
    except subprocess.TimeoutExpired:
        raise VoiceError("系统语音合成超时")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------- 对外接口 ----------------

def synthesize(text, name=None, style=None):
    """整段合成，返回 (字节, mime, 实际使用的音色)。同样的句子走缓存。

    style 是这一句的语气指示（DeepSeek 生成，随句子变），
    传空则用音色预设里的默认风格。系统语音没有情感参数，会忽略它。
    """
    text = _clean(text)
    if not text:
        raise VoiceError("没有内容可合成")

    name = name if name in PRESETS else default_preset()
    preset = PRESETS[name]
    style = (style or "").strip()[:STYLE_LIMIT]
    key = "%s|%s|%s" % (name, style, text)

    hit = _cache_get(key)
    if hit is not None:
        return hit[0], hit[1], name

    engine = preset.get("engine")
    try:
        if engine == "mimo":
            data, mime = _synth_mimo(text, preset, style)
        else:
            data, mime = _synth_say(text, preset)
    except VoiceError:
        if engine != "mimo":
            raise
        # 降级：MiMo 挂了就用系统语音顶上，一句话都不少说
        data, mime = _synth_say(text, PRESETS[FALLBACK_PRESET])
        _cache_put(key, (data, mime))
        return data, mime, FALLBACK_PRESET

    _cache_put(key, (data, mime))
    return data, mime, name


def presets():
    out = []
    for pid, conf in PRESETS.items():
        out.append({
            "id": pid,
            "label": conf["label"],
            "engine": conf["engine"],
            "segment": bool(conf.get("segment", True)),
            "stream": bool(conf.get("stream", False)),
        })
    return out


def supports_stream(name):
    preset = PRESETS.get(name if name in PRESETS else default_preset())
    return bool(preset.get("stream", False))


def engine_of(name):
    preset = PRESETS.get(name if name in PRESETS else default_preset())
    return preset.get("engine", "say")


def warm():
    """启动时预热：建好 TLS 连接 + 把系统语音引擎加载进内存。

    小米域名的 TLS 握手要 0.8 秒，提前做掉，第一句就能省下来。
    """
    ok_say = ok_conn = False
    try:
        synthesize("预热", FALLBACK_PRESET)
        ok_say = True
    except VoiceError:
        pass

    if _env("MIMO_API_KEY"):
        try:
            conn = _conn(10.0)
            conn.request("GET", "/v1/models", headers={"api-key": _env("MIMO_API_KEY")})
            conn.getresponse().read()
            ok_conn = True
        except (http.client.HTTPException, OSError, TimeoutError):
            _drop_conn()
    return ok_say, ok_conn
