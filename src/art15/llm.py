"""The model, local by default (Ollama). A cloud model is used only when --allow-cloud is given,
Jev (TypeSafe AI) only when --jev is given."""

import base64
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

Ask = Callable[[str, dict[str, Any]], dict[str, Any]]

OLLAMA = "http://127.0.0.1:11434"
# OpenRouter serves Jev with a plain OpenRouter key (TypeSafe paused its own signups on 2026-09-22).
# TypeSafe's own endpoint takes the same body: ART15_JEV_URL=https://api.typesafe.ai/v1/systemone, model jev-1.13.0.
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.14"  # pinned so eval numbers stay comparable
JEV_BATCH = 100  # questions per request
usage: Counter[str] = Counter()  # Jev input tokens and cost in this run, for `art15 eval`


@dataclass
class Decision:
    choice: str
    probabilities: dict[str, float]

    @property
    def p(self) -> float:
        return self.probabilities.get(self.choice, 0.0)


# question name -> (instructions, {option: description})
Questions = dict[str, tuple[str, dict[str, str]]]
Decide = Callable[[str, Questions], dict[str, Decision]]


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


def cloud(url: str, key: str, model: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    # json_object plus the schema in the prompt works on every OpenAI-compatible API, json_schema does not.
    r = httpx.post(
        url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "temperature": 0, "response_format": {"type": "json_object"},
              "messages": [{"role": "user", "content": f"{prompt}\n\nAnswer with JSON matching this schema:\n{json.dumps(schema)}"}]},
        timeout=300,
    )
    r.raise_for_status()
    out: dict[str, Any] = json.loads(r.json()["choices"][0]["message"]["content"])
    return out


def make_ask(env: dict[str, str], allow_cloud: bool) -> Ask:
    if allow_cloud and env.get("ART15_CLOUD_URL"):
        return lambda prompt, schema: cloud(env["ART15_CLOUD_URL"], env.get("ART15_CLOUD_KEY", ""), env["ART15_CLOUD_MODEL"], prompt, schema)
    model = env.get("ART15_MODEL", "qwen3:8b")
    return lambda prompt, schema: ollama(model, prompt, schema)


def _post(url: str, key: str, body: dict[str, Any], client: httpx.Client | None, tries: int = 4) -> dict[str, Any]:
    post = client.post if client else httpx.post
    for attempt in range(tries):
        r = post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=60)
        if (r.status_code in (429, 529) or r.status_code >= 500) and attempt < tries - 1:
            time.sleep(float(r.headers.get("retry-after", 2**attempt)))
            continue
        r.raise_for_status()
        out: dict[str, Any] = r.json()
        return out
    raise AssertionError("unreachable")


def jev(url: str, key: str, model: str, state: str, questions: Questions,
        client: httpx.Client | None = None) -> dict[str, Decision]:
    """Choice questions about one text. Every question is answered on its own, so many go in one request."""
    out: dict[str, Decision] = {}
    names = list(questions)
    for i in range(0, len(names), JEV_BATCH):
        body = {"model": model, "state": state, "questions": {
            n: {"type": "choice", "instructions": questions[n][0], "criteria": questions[n][1]}
            for n in names[i : i + JEV_BATCH]}}
        data = _post(url, key, body, client)
        for n, a in data["answers"].items():
            probs = {k: float(v) for k, v in (a.get("probabilities") or {a["choice"]: a.get("confidence", 0.0)}).items()}
            out[n] = Decision(str(a["choice"]), probs)
        usage.update({k: v for k, v in data.get("usage", {}).items() if isinstance(v, (int, float))})
    return out


def make_decide(env: dict[str, str]) -> Decide:
    url, key, model = env.get("ART15_JEV_URL", JEV_URL), env["ART15_JEV_KEY"], env.get("ART15_JEV_MODEL", JEV_MODEL)
    return lambda state, questions: jev(url, key, model, state, questions)


def models() -> list[str]:
    r = httpx.get(f"{OLLAMA}/api/tags", timeout=5)
    r.raise_for_status()
    return [m["name"] for m in r.json()["models"]]
