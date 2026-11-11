"""DeepSeek 客户端（零第三方依赖）。

只用标准库 urllib 实现，任何 Python 3.8+ 都能直接跑，不需要 pip install。
两种调用方式：
    stream_chat(messages) -> 逐段 yield 文本，适合实时场景（直播必须用这个）
    complete(messages)    -> 一次拿到全文 + token 用量，适合后台批处理
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")


class DeepSeekError(RuntimeError):
    """调用失败时抛出，调用方应该捕获并走兜底话术。"""


# ---------------- 语气指示的拆分 ----------------
# 模型第一行输出 `[语气]…[/语气]`，这一行给语音合成当风格提示，下面的才是台词。
# 模型不一定守规矩，所以两种写法都认，判不出来就整段当台词，一个字都不多等。

_OPENERS = ("[语气]", "【语气】")
_CLOSERS = ("[/语气]", "【/语气】")
_PROBE_LIMIT = 120  # 试探上限：超过这么多字还没闭合，就认定它没打算闭合
_MAX_MARK = max(len(m) for m in _OPENERS + _CLOSERS)


class StyleSplitter:
    """边收流边分离语气指示，返回应当上屏的正文。

    只做「开头一次」判断：第一行是语气就拿走，判完立刻进入放行状态，
    后面每个增量原样返回。这样即使模型没按格式写，也只多等一个 chunk。
    """

    def __init__(self, limit: int = _PROBE_LIMIT) -> None:
        self.style = ""
        self.limit = limit
        self._pending = ""
        self._closed = False
        self._trim = False  # 刚切完标记，下一段开头的换行要去掉
        self._leaked = False  # 放弃解析过：后面出现的标记也要从正文里清掉
        self._tail = ""  # 放弃解析后，留住可能被 chunk 切断的那半个标记

    def feed(self, piece: str) -> str:
        if self._closed:
            if self._leaked:
                return self._strip_leak(piece)
            if self._trim:
                piece = piece.lstrip("\r\n \t\u3000")
                if not piece:
                    return ""
                self._trim = False
            return piece

        self._pending += piece
        head = self._pending.lstrip(" \t\r\n\u3000")
        if not head:
            self._pending = head
            return ""

        # 开头既不是标记、也不是标记的半个前缀，才认定它没打算写语气。
        # 流式下一个 chunk 可能只到「[语」这种程度，得等它长完再判断。
        if not any(head.startswith(op) or op.startswith(head) for op in _OPENERS):
            # 模型没照格式写（或者干脆是闲聊）：整段直接上屏，一个字都不多等
            self._closed = True
            self._pending = ""
            return head

        for opener, closer in zip(_OPENERS, _CLOSERS):
            if not head.startswith(opener):
                continue
            end = head.find(closer, len(opener))
            if end < 0:
                # 忘了写闭合标记，但确实分了行：第一行当语气，后面当正文
                newline = head.find("\n", len(opener))
                if newline > 0:
                    return self._close(head[len(opener) : newline], head[newline + 1 :])
                if len(head) > self.limit:
                    # 开了不收也不分行，别再等了，把标记去掉当正文
                    self._closed = True
                    self._leaked = True
                    self._pending = ""
                    return _strip_marks(head)
                self._pending = head
                return ""
            return self._close(head[len(opener) : end], head[end + len(closer) :])

        return ""

    def _close(self, style: str, rest: str) -> str:
        """定下语气，返回正文开头（闭合标记后的换行要去掉）。"""
        self.style = _clean_style(style)
        self._closed = True
        self._pending = ""
        trimmed = rest.lstrip("\r\n \t\u3000")
        # 闭合标记正好落在 chunk 末尾时，换行在下一个 chunk 里，得继续去掉
        self._trim = not trimmed
        return trimmed

    def _strip_leak(self, piece: str) -> str:
        """放弃解析之后的清理。

        标记可能被 chunk 从中间切断，所以尾巴里可能不完整的部分先留住，
        等下一个 chunk 拼完整了再判断——只在这一条罕见路径上多等几个字。
        """
        buf = self._tail + piece
        cut = len(buf)
        for i in range(max(0, len(buf) - _MAX_MARK), len(buf)):
            if buf[i] in "[【":
                cut = i
                break
        if cut < len(buf) and len(buf) - cut < _MAX_MARK:
            self._tail = buf[cut:]
            return _strip_marks(buf[:cut])
        self._tail = ""
        return _strip_marks(buf)

    def finish(self) -> str:
        """流结束时的残留。宁可把标记一起吐出去，也不能吞字。"""
        if self._closed:
            tail = self._tail
            self._tail = ""
            return _strip_marks(tail)
        self._closed = True
        rest = _strip_marks(self._pending)
        self._pending = ""
        return rest.lstrip("\r\n \t\u3000")


def _strip_marks(text: str) -> str:
    for mark in _OPENERS + _CLOSERS:
        text = text.replace(mark, "")
    return text


def _clean_style(text: str) -> str:
    """语气指示的收尾清理。

    模型偶尔用单个 `]` 收尾（写成 `[语气]…无所谓]` 而不是 `[/语气]`），
    那个落单的括号会跟着一起送进语音合成，念出来就是一张多余的嘴。
    只在首尾各削一层括号，中间的内容一个字都不动。
    """
    text = _strip_marks(text).strip()
    while text and text[0] in "[【":
        text = text[1:].lstrip()
    while text and text[-1] in "]】":
        text = text[:-1].rstrip()
    return text


def split_style(text: str):
    """一次拿到全文时用：返回 (语气, 正文)。"""
    splitter = StyleSplitter()
    body = splitter.feed(text or "") + splitter.finish()
    return splitter.style, body.strip()


def format_reply(style: str, body: str) -> str:
    """拼回「语气 + 台词」的完整格式，存进对话历史。

    历史里如果只留台词，模型看到的全是「没有语气行的回复」，
    几轮之后就会开始漏写。所以上下文里必须是完整格式。
    """
    if not style:
        return body
    return "[语气]%s[/语气]\n%s" % (style, body)


def load_dotenv(path: str = ENV_PATH) -> None:
    """把 .env 读进环境变量。已存在的变量不覆盖，方便临时用 export 改。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class Result:
    text: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Dict = field(default_factory=dict)


def _describe_http_error(status: int, body: str) -> str:
    hint = {
        400: "请求格式有问题（检查 messages 结构、model 名）",
        401: "API Key 无效或已过期",
        402: "账户余额不足",
        404: "接口地址不对，检查 DEEPSEEK_BASE_URL",
        422: "参数校验失败",
        429: "触发限流，降低并发或加间隔",
        500: "服务端错误，可重试",
        503: "服务繁忙，可重试",
    }.get(status, "未知错误")
    snippet = (body or "").strip().replace("\n", " ")[:300]
    return "HTTP %d：%s\n服务端返回：%s" % (status, hint, snippet)


class DeepSeek:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 1.0,
        timeout: int = 30,
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise DeepSeekError("缺少 DEEPSEEK_API_KEY，请检查 .env 文件")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.temperature = temperature
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return self.base_url + "/chat/completions"

    def _headers(self, stream: bool) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def _payload(self, messages: List[Dict], stream: bool) -> Dict:
        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature,
        }

    def _open(self, messages: List[Dict], stream: bool):
        """发请求并返回可读的响应对象，把各种网络异常统一成 DeepSeekError。"""
        body = json.dumps(self._payload(messages, stream), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body, headers=self._headers(stream), method="POST"
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise DeepSeekError(_describe_http_error(exc.code, detail)) from exc
        except socket.timeout as exc:
            raise DeepSeekError("请求超时（%ds），考虑调大 timeout" % self.timeout) from exc
        except urllib.error.URLError as exc:
            raise DeepSeekError("网络请求失败：%s" % (exc.reason,)) from exc

    def stream_chat(self, messages: List[Dict]) -> Iterator[str]:
        """逐段 yield 增量文本。首个 chunk 到达的时间就是首字延迟。"""
        response = self._open(list(messages), stream=True)
        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    yield piece
        finally:
            response.close()

    def complete(self, messages: List[Dict]) -> Result:
        """一次性拿全文，带 token 用量。"""
        response = self._open(list(messages), stream=False)
        try:
            raw = response.read()
        finally:
            response.close()
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise DeepSeekError("响应不是合法 JSON：%s" % exc) from exc
        choices = data.get("choices") or []
        if not choices:
            raise DeepSeekError("响应里没有 choices：%s" % str(data)[:200])
        usage = data.get("usage") or {}
        return Result(
            text=(choices[0].get("message") or {}).get("content", ""),
            model=data.get("model", ""),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            raw=data,
        )

    def reply(self, text: str, system: Optional[str] = None) -> str:
        """最短路径：一句话进，一句话出。"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": text})
        return self.complete(messages).text


if __name__ == "__main__":
    print(DeepSeek().reply("用一句话说明你是谁"))
