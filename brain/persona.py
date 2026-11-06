"""人设文件的加载。chat.py 和 server.py 共用，保证两边读到的是同一份。"""

from __future__ import annotations

import os

PERSONA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.md")
PLAIN_PROMPT = "你是一个乐于助人的中文助手，回答简洁准确，不说废话。"


def load_persona(fallback: str = PLAIN_PROMPT) -> str:
    """每条消息都重读一次，改完 persona.md 存盘就生效，不用重启。"""
    try:
        with open(PERSONA_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return content or fallback
    except FileNotFoundError:
        return fallback
