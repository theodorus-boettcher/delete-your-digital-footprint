import json
from email.message import EmailMessage
from typing import Any

import httpx

from art15 import audit, evals, llm
from art15.classify import classify, redact

KEY = "sk-test"


def client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def answer(questions: dict[str, Any], pick: str, p: float) -> dict[str, Any]:
    return {"answers": {n: {"type": "choice", "choice": pick, "probabilities": {pick: p}, "confidence": p} for n in questions},
            "usage": {"input_tokens": 10, "cost": 0.001}}


def test_request_shape_batching_and_retry() -> None:
    seen: list[dict[str, Any]] = []
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        assert req.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json=answer(body["questions"], "a", 0.9))

    questions = {f"q{i}": ("Which?", {"a": "first", "b": "second"}) for i in range(250)}
    out = llm.jev("https://x/decisions", KEY, "typesafe/jev-1.14", "some text", questions, client(handler))
    assert len(out) == 250 and out["q249"].choice == "a" and out["q249"].p == 0.9
    assert [len(b["questions"]) for b in seen] == [100, 100, 50]  # the 429 was retried, then 3 batches
    assert seen[0]["model"] == "typesafe/jev-1.14" and seen[0]["state"] == "some text"
    assert seen[0]["questions"]["q0"] == {"type": "choice", "instructions": "Which?", "criteria": {"a": "first", "b": "second"}}


def test_missing_probabilities_fall_back_to_confidence() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"q": {"choice": "b", "confidence": 0.7}}})

    assert llm.jev("https://x", KEY, "m", "s", {"q": ("?", {"a": "", "b": ""})}, client(handler))["q"].p == 0.7


def test_redact() -> None:
    text = ("Hi Ada Rossi, your file is ready: https://dl.shop.co.uk/x?token=abc123\n"
            "Your password: Zx81!\nOrder 12345678. Contact privacy@shop.co.uk. Please download it.")
    out = redact(text, ["Ada Rossi", "ada@example.org"])
    assert "[link: dl.shop.co.uk]" in out and "token" not in out
    assert "Zx81" not in out and "[password line]" in out
    assert "12345678" not in out and "privacy@shop.co.uk" not in out
    assert "Ada" not in out and "Rossi" not in out and "download" in out


def reply(body: str, attachment: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "privacy@shop.co.uk", "Re: request [art15-abcd]"
    msg.set_content(body)
    if attachment:
        msg.add_attachment(b"PK", maintype="application", subtype="zip", filename="Ada_Rossi_data.zip")
    return msg


def test_classify_with_jev_threshold_and_checks() -> None:
    sent: list[str] = []

    def decide(pick: str, p: float) -> llm.Decide:
        def f(state: str, questions: llm.Questions) -> dict[str, llm.Decision]:
            sent.append(state)
            assert set(questions["label"][1]) >= {"acknowledged", "data_delivered", "other"}
            return {"label": llm.Decision(pick, {pick: p})}
        return f

    def no_ask(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("the local model is not used with --jev")

    ack = reply("Dear Ada Rossi, we have received your request.")
    assert classify(ack, no_ask, decide("acknowledged", 0.95), 0.8, ["Ada Rossi"]).label == "acknowledged"
    low = classify(ack, no_ask, decide("acknowledged", 0.6), 0.8)
    assert (low.label, low.pick, low.confidence, low.backend) == ("other", "acknowledged", 0.6, "jev")
    # "data delivered" without an attachment or a link fails the plain-rule check
    assert classify(ack, no_ask, decide("data_delivered", 0.99), 0.8).label == "other"
    data = reply("Please find your data attached.", attachment=True)
    assert classify(data, no_ask, decide("data_delivered", 0.99), 0.8, ["Ada Rossi"]).label == "data_delivered"
    assert "Rossi" not in sent[0] and "Ada_Rossi" not in sent[-1] and "Attachments: 1 (zip)" in sent[-1]


def test_fields_with_jev_names_only() -> None:
    item = audit.Item("a.json", "json", ["profile.firstName", "zzq", "wobble"])
    asked: list[str] = []

    def decide(state: str, questions: llm.Questions) -> dict[str, llm.Decision]:
        asked.extend(q[0] for q in questions.values())
        assert state == audit.FIELD_STATE
        return {k: llm.Decision("inferences", {"inferences": 0.9 if "zzq" in q[0] else 0.3}) for k, q in questions.items()}

    audit.categorize([item], None, decide)
    assert item.categories == {"profile.firstName": "identity", "zzq": "inferences"}  # wobble: p 0.3, dropped
    assert len(asked) == 2  # the rules knew firstName


def test_eval_metrics() -> None:
    rows = [evals.Row("1", "acknowledged", "acknowledged", "acknowledged", 0.97),
            evals.Row("2", "refusal", "acknowledged", "other", 0.6),  # sent to the user
            evals.Row("3", "data_delivered", "data_delivered", "data_delivered", 0.85),
            evals.Row("4", "extension", "acknowledged", "acknowledged", 0.9)]
    m = evals.summary(rows)
    assert (m["raw_accuracy"], m["to_user"]) == (0.5, 0.25) and round(m["auto_accuracy"], 2) == 0.67
    assert evals.thresholds(rows)[2] == (0.8, 0.75, 2 / 3)
    assert [n for _, n, _ in evals.calibration(rows)] == [0, 1, 2, 1]
