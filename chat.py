#!/usr/bin/env python3
"""输入 → DeepSeek → 输出。

用法：
    python3 chat.py                     交互模式
    python3 chat.py "你好"               单次提问，打印完就退出
    python3 chat.py --persona           带直播人设启动
    python3 chat.py --model deepseek-reasoner
"""

from __future__ import annotations

import argparse
import sys
import time

from brain.persona import PLAIN_PROMPT, load_persona
from brain.replier import DeepSeek, DeepSeekError, StyleSplitter, format_reply

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"

MAX_HISTORY_MESSAGES = 20


def paint(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text



class Session:
    def __init__(self, model: str | None = None, temperature: float = 1.0, use_persona: bool = False) -> None:
        self.client = DeepSeek(model=model, temperature=temperature)
        self.use_persona = use_persona
        self.history: list[dict] = []

    @property
    def persona_name(self) -> str:
        return "直播人设" if self.use_persona else "普通助手"

    def system_prompt(self) -> str:
        return load_persona() if self.use_persona else PLAIN_PROMPT

    def build_messages(self, user_text: str) -> list[dict]:
        messages = [{"role": "system", "content": self.system_prompt()}]
        messages.extend(self.history[-MAX_HISTORY_MESSAGES:])
        messages.append({"role": "user", "content": user_text})
        return messages

    def ask(self, user_text: str) -> str | None:
        messages = self.build_messages(user_text)
        start = time.perf_counter()
        first_token_at: float | None = None
        buffer: list[str] = []
        # 第一行是语气指示，终端里单独用灰色显示，不混进台词
        splitter = StyleSplitter()
        style_shown = False

        def emit(chunk: str) -> None:
            sys.stdout.write(chunk)
            sys.stdout.flush()

        try:
            for piece in self.client.stream_chat(messages):
                if first_token_at is None:
                    first_token_at = time.perf_counter() - start
                body = splitter.feed(piece)
                if splitter.style and not style_shown:
                    style_shown = True
                    emit(paint(f"(语气：{splitter.style})\n", DIM))
                if body:
                    buffer.append(body)
                    emit(body)
            tail = splitter.finish()
            if tail:
                buffer.append(tail)
                emit(tail)
        except KeyboardInterrupt:
            print(paint("\n[已中断，本条不进上下文]", DIM))
            return None
        except DeepSeekError as exc:
            print()
            print(paint(f"[调用失败] {exc}", YELLOW))
            return None

        elapsed = time.perf_counter() - start
        answer = "".join(buffer).strip()
        if not answer:
            print(paint("[模型返回空]", YELLOW))
            return None

        print()
        print(
            paint(
                f"  首字 {first_token_at * 1000:.0f}ms · 共 {elapsed * 1000:.0f}ms · {len(answer)} 字",
                DIM,
            )
        )

        self.history.append({"role": "user", "content": user_text})
        # 存完整格式：上下文里带语气行，模型才不会写着写着忘了
        self.history.append({"role": "assistant", "content": format_reply(splitter.style, answer)})
        return answer

    def reset(self) -> None:
        self.history.clear()
        print(paint("[上下文已清空]", DIM))


HELP = """
可用命令（以冒号开头）：
  :reset     清空上下文
  :persona   在普通助手 / 直播人设 之间切换
  :history   查看当前上下文条数
  :help      显示这份帮助
  :exit      退出（也可直接输入 exit 或按 Ctrl-D）
直接输入内容即发送给模型。
""".strip()


def run_once(session: Session, text: str) -> int:
    answer = session.ask(text)
    return 0 if answer else 1


def run_repl(session: Session) -> int:
    print(paint("DeepSeek 对话回路", BOLD))
    print(paint(f"模型 {session.client.model} · {session.persona_name} · 输入 :help 看命令", DIM))
    print()

    while True:
        try:
            raw = input(paint("你 > ", CYAN))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = raw.strip()
        if not text:
            continue

        if text in {"exit", "quit", ":q", ":exit", ":quit"}:
            break
        if text == ":reset":
            session.reset()
            continue
        if text == ":persona":
            session.use_persona = not session.use_persona
            print(paint(f"[已切换到 {session.persona_name}]", DIM))
            continue
        if text == ":history":
            sent = min(len(session.history), MAX_HISTORY_MESSAGES)
            print(paint(f"[累计 {len(session.history)} 条，下次实际发送最近 {sent} 条]", DIM))
            continue
        if text in {":help", ":h", "help"}:
            print(HELP)
            continue
        if text.startswith(":"):
            print(paint(f"[未知命令 {text}，输入 :help 查看]", YELLOW))
            continue

        sys.stdout.write(paint("AI > ", BOLD))
        sys.stdout.flush()
        session.ask(text)
        print()

    print(paint("再见。", DIM))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="输入 → DeepSeek → 输出")
    parser.add_argument("prompt", nargs="*", help="单次提问内容")
    parser.add_argument("--model", default=None, help="覆盖 .env 里的模型名")
    parser.add_argument("--temp", type=float, default=1.0, help="温度，直播建议 0.8~1.1")
    parser.add_argument("--persona", action="store_true", help="加载 brain/persona.md 作为系统提示")
    parser.add_argument("--plain", action="store_true", help="强制使用普通助手提示词")
    args = parser.parse_args()

    use_persona = args.persona and not args.plain

    try:
        session = Session(model=args.model, temperature=args.temp, use_persona=use_persona)
    except DeepSeekError as exc:
        print(paint(f"[初始化失败] {exc}", YELLOW))
        return 1

    if args.prompt:
        return run_once(session, " ".join(args.prompt))
    return run_repl(session)


if __name__ == "__main__":
    sys.exit(main())
