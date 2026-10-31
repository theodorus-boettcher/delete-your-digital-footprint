import io
import json
import zipfile
from pathlib import Path
from typing import Any

from art15 import audit

LETTER = """Dear Ada,
Attached is a copy of your personal data. We process your data for the purposes of providing the service and fraud prevention.
Your data is shared with our payment provider Stripe Payments Europe Ltd.
You can complain to your supervisory authority."""


def make_export(tmp: Path) -> Path:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("activity/searches.csv", "query,timestamp,ip_address\nshoes,2026-01-01,1.2.3.4\nbags,2026-01-02,1.2.3.4\n")
    path = tmp / "export.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("account.json", json.dumps({"profile": {"firstName": "Ada", "email": "a@x.org"},
                                                "orders": [{"orderId": 1, "items": [{"price": 3}]}, {"orderId": 2}]}))
        z.writestr("ads/interests.html", "<table><tr><th>Interest</th></tr><tr><td>Hiking</td></tr></table>")
        z.writestr("photos/1.jpg", b"\xff\xd8")
        z.writestr("misc/zzq.json", json.dumps([{"wobble": 1}]))
        z.writestr("activity.zip", inner.getvalue())
    return path


def test_inventory_unpacks_nested_zips_and_keeps_the_original(tmp_path: Path) -> None:
    export = make_export(tmp_path)
    roots = audit.unpack(export, tmp_path / "unpacked")
    items = {it.file: it for it in audit.inventory(roots)}
    assert export.exists()
    assert set(items) == {"export/account.json", "export/ads/interests.html", "export/photos/1.jpg",
                          "export/misc/zzq.json", "export/activity/activity/searches.csv"}
    acc = items["export/account.json"]
    assert acc.fields == ["orders[].items[].price", "orders[].orderId", "profile.email", "profile.firstName"]
    assert acc.records == 2
    assert items["export/activity/activity/searches.csv"].records == 2
    assert items["export/ads/interests.html"].fields == ["Interest"]


def test_categories_by_rule_then_model(tmp_path: Path) -> None:
    items = audit.inventory(audit.unpack(make_export(tmp_path), tmp_path / "u"))
    asked: list[str] = []

    def fake(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        asked.extend(schema["required"])
        return {n: "other" for n in schema["required"]} | {"misc": "nonsense"}

    audit.categorize(items, fake)
    found = audit.categories_found(items)
    assert {"identity", "contact", "purchases", "activity", "device_ids", "inferences", "content"} <= set(found)
    assert sorted(asked) == ["[].wobble", "timestamp"]  # only fields no rule knows go to the model
    assert "export/account.json: profile.firstName" in found["identity"]


def test_evidence_rule(tmp_path: Path) -> None:
    items = audit.inventory(audit.unpack(make_export(tmp_path), tmp_path / "u"))

    def fake(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "purposes": {"status": "present", "file": "reply.txt",
                         "quote": "We process your data for the purposes of providing the service"},
            "recipients": {"status": "present", "file": "reply.txt", "quote": "Shared with our payment provider Stripe Payments Europe Ltd"},
            "retention": {"status": "present", "file": "reply.txt", "quote": "We keep your data for 3 years."},  # made up
        }

    fs = {f.item: f for f in audit.checklist(items, LETTER, fake)}
    assert fs["purposes"].status == "present" and fs["purposes"].file == "reply.txt"
    assert fs["recipients"].status == "present"  # case and the final full stop don't matter
    assert fs["retention"].status == "missing"  # the quote is not in any document: dropped
    assert fs["complaint"].status == "check"  # the model said nothing, a keyword passage exists
    assert fs["copy"].status == "present" and fs["machine_readable"].status == "present"
    assert 0 < audit.score(list(fs.values())) < 1


def test_policy_diff(tmp_path: Path) -> None:
    items = audit.inventory(audit.unpack(make_export(tmp_path), tmp_path / "u"))
    audit.categorize(items, None)
    policy = audit.policy_text("<html><nav>menu</nav><p>We collect your precise location when you use the app.</p>"
                               "<p>We collect your email address.</p></html>")

    def fake(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"declared": [{"category": "location", "quote": "We collect your precise location when you use the app."},
                             {"category": "contact", "quote": "We collect your email address."},
                             {"category": "communications", "quote": "We read your chats."}]}  # not in the policy

    fs = {f.item: f.status for f in audit.policy_diff(policy, "https://x/privacy", items, fake)}
    assert fs == {"location": "missing", "contact": "present"}
