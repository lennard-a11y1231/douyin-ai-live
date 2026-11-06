#!/usr/bin/env python3
"""本地预览服务。

    python3 server.py

然后浏览器打开 http://127.0.0.1:8765

左边是模拟的直播画面（AI 说的话显示成字幕），
右边是互动消息流，底部输入框手动模拟观众弹幕。
只用标准库，不装任何东西。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import voice
from brain.persona import load_persona
from brain.replier import ROOT, DeepSeek, DeepSeekError, StyleSplitter, format_reply

WEB_DIR = os.path.join(ROOT, "web")
MAX_HISTORY = 20


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# 默认只绑本机：本地预览、SSH 隧道都够用，也不会把接口暴露到公网。
# 部署到服务器且确实要公网直连时，才设 LIVE_HOST=0.0.0.0。
HOST = (os.environ.get("LIVE_HOST") or "127.0.0.1").strip()
PORT = _env_int("LIVE_PORT", 8765)

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}

_history: list = []
_history_lock = threading.Lock()
_client = None


class QuietServer(ThreadingHTTPServer):
    """浏览器关掉 keep-alive 连接时别再刷一屏 traceback。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LiveRelay/0.1"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, ctype, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/voices":
            self._send_json({"voices": voice.presets(), "default": voice.default_preset()})
            return

        route = STATIC_FILES.get(self.path.split("?")[0])
        if route is None:
            if self.path.startswith("/favicon"):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json({"error": "not found"}, 404)
            return

        name, ctype = route
        path = os.path.join(WEB_DIR, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_json({"error": "页面文件缺失：" + name}, 500)
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/reset":
            with _history_lock:
                _history.clear()
            self._send_json({"ok": True})
            return

        if self.path == "/api/tts":
            self._handle_tts()
            return

        if self.path != "/api/danmaku":
            self.send_json_404()
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send_json({"error": "请求格式错误"}, 400)
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            self._send_json({"error": "内容为空"}, 400)
            return

        self._stream_reply(text)

    def send_json_404(self):
        self._send_json({"error": "not found"}, 404)

    def _handle_tts(self):
        """一句话进来，音频出去。前端按句调用，边生成边出声。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send_json({"error": "请求格式错误"}, 400)
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            self._send_json({"error": "内容为空"}, 400)
            return

        style = str(payload.get("style") or "").strip()
        name = payload.get("voice")
        if payload.get("stream") and voice.supports_stream(name):
            self._handle_tts_stream(text, name, style)
            return

        started = time.perf_counter()
        try:
            data, mime, used = voice.synthesize(text, name, style)
        except voice.VoiceError as exc:
            self._send_json({"error": str(exc)}, 503)
            return

        cost = int((time.perf_counter() - started) * 1000)
        self._send_bytes(data, mime, {"X-Synth-Ms": str(cost), "X-TTS-Voice": used})

    def _handle_tts_stream(self, text, name, style=None):
        """流式：MiMo 吐一块 PCM 就往浏览器写一块，前端边收边播。

        先把第一块拿到手再发响应头，这样上游报错时还能返回 503，
        前端可以退回到非流式（服务端那边会自动降级成系统语音）。
        """
        started = time.perf_counter()
        chunks = voice.stream_pcm(text, name, style)
        try:
            first = next(chunks)
        except StopIteration:
            self._send_json({"error": "没有音频"}, 503)
            return
        except voice.VoiceError as exc:
            self._send_json({"error": str(exc)}, 503)
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/pcm")
        self.send_header("X-TTS-Voice", name or voice.default_preset())
        self.send_header("X-PCM-Rate", str(voice.PCM_RATE))
        self.send_header("X-First-Ms", str(int((time.perf_counter() - started) * 1000)))
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def write(piece):
            self.wfile.write(("%X\r\n" % len(piece)).encode("ascii"))
            self.wfile.write(piece)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        try:
            write(first)
            for piece in chunks:
                if piece:
                    write(piece)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except voice.VoiceError:
            pass  # 音频已经开始放了，只能把这一句截断
        finally:
            # 1) 显式关闭生成器：前端中途断开时 MiMo 那条连接里还有没读完的响应，
            #    不关掉就会被下一个请求复用，于是这一句的音频串到下一句去。
            try:
                chunks.close()
            except (RuntimeError, ValueError):
                pass
            # 2) 必须补 chunked 的终止块。少了它，浏览器那边的 body reader
            #    永远等不到 done，前端就卡在「等音频」上不动了。
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass


    def _begin_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _event(self, obj):
        """写一个 SSE 事件。客户端断开时返回 False，调用方应停止推送。"""
        frame = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
        try:
            self.wfile.write(("%X\r\n" % len(frame)).encode("ascii"))
            self.wfile.write(frame)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _end_stream(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_reply(self, text):
        with _history_lock:
            messages = [{"role": "system", "content": load_persona()}]
            messages.extend(_history[-MAX_HISTORY:])
            messages.append({"role": "user", "content": text})

        self._begin_stream()
        alive = self._event({"type": "start"})
        started = time.perf_counter()
        first_ms = None
        pieces = []

        # 模型第一行是语气指示，拆出来单独发一个 style 事件：
        # 台词上屏、语气送去语音合成，两者互不污染。
        splitter = StyleSplitter()
        style_sent = False

        try:
            for piece in _client.stream_chat(messages):
                if first_ms is None:
                    first_ms = int((time.perf_counter() - started) * 1000)

                body = splitter.feed(piece)
                if splitter.style and not style_sent:
                    style_sent = True
                    if alive:
                        alive = self._event({"type": "style", "text": splitter.style})
                if not body:
                    continue

                pieces.append(body)
                if alive:
                    alive = self._event({"type": "delta", "text": body})

            tail = splitter.finish()
            if tail:
                pieces.append(tail)
                if alive:
                    alive = self._event({"type": "delta", "text": tail})
            if splitter.style and not style_sent and alive:
                style_sent = True
                alive = self._event({"type": "style", "text": splitter.style})
        except DeepSeekError as exc:
            if alive:
                self._event({"type": "error", "message": str(exc)})
            self._end_stream()
            return
        except (BrokenPipeError, ConnectionResetError):
            self._end_stream()
            return

        answer = "".join(pieces).strip()
        total_ms = int((time.perf_counter() - started) * 1000)

        if answer:
            with _history_lock:
                _history.append({"role": "user", "content": text})
                # 历史里存完整格式（含语气行），否则模型看到全是「没写语气」的回复，几轮后就忘了
                _history.append(
                    {"role": "assistant", "content": format_reply(splitter.style, answer)}
                )

        if alive:
            self._event(
                {
                    "type": "done",
                    "first_ms": first_ms if first_ms is not None else total_ms,
                    "total_ms": total_ms,
                    "chars": len(answer),
                    "style": splitter.style,
                }
            )
        self._end_stream()


def main():
    global _client
    try:
        _client = DeepSeek()
    except DeepSeekError as exc:
        print("[启动失败] %s" % exc)
        return 1

    if not os.path.isdir(WEB_DIR):
        print("[启动失败] 找不到页面目录 %s" % WEB_DIR)
        return 1

    warmed_say, warmed_conn = voice.warm()
    engine = voice.engine_of(voice.default_preset())

    server = QuietServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print("大肥鱼直播中继已启动")
    print("  模型 %s · 人设 brain/persona.md" % _client.model)
    print("  语音 %s（%s）· 系统语音%s · MiMo 连接%s"
          % (voice.default_preset(), engine,
             "就绪" if warmed_say else "不可用",
             "已预热" if warmed_conn else "未建立"))
    shown = "127.0.0.1" if HOST in ("0.0.0.0", "") else HOST
    print("  在浏览器打开 http://%s:%d" % (shown, PORT))
    if HOST == "0.0.0.0":
        print("  注意：已监听全部网卡，接口没有鉴权，别把端口裸在公网上")
    print("  按 Ctrl-C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
