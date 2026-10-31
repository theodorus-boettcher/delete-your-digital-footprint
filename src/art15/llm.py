"""The model: local only (Ollama on this machine)."""

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx

Ask = Callable[[str, dict[str, Any]], dict[str, Any]]

OLLAMA = "http://127.0.0.1:11434"


def ollama(model: str, prompt: str, schema: dict[str, Any], images: list[bytes] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        message["images"] = [base64.b64encode(i).decode() for i in images]
    r = httpx.post(
        f"{OLLAMA}/api/chat",
        json={"model": model, "messages": [message], "format": schema, "stream": False, "think": False,
              "options": {"temperature": 0, "num_ctx": 16384}},
        timeout=600,
    )
    r.raise_for_status()
    out: dict[str, Any] = json.loads(r.json()["message"]["content"])
    return out


def make_ask(env: dict[str, str]) -> Ask:
    model = env.get("ART15_MODEL", "qwen3:8b")
    return lambda prompt, schema: ollama(model, prompt, schema)


def models() -> list[str]:
    r = httpx.get(f"{OLLAMA}/api/tags", timeout=5)
    r.raise_for_status()
    return [m["name"] for m in r.json()["models"]]
