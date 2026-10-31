"""`art15 eval`: the local model on replies and field names the user labelled by hand."""

import csv
from dataclasses import dataclass
from html import escape
from pathlib import Path


@dataclass
class Row:
    key: str  # message id or field name
    truth: str
    pick: str  # the model's own label
    final: str  # after the checks (quote, plain rules)
    seconds: float = 0.0


def summary(rows: list[Row]) -> dict[str, float]:
    """raw: the model's label is right. to_user: the checks sent it to the user. auto: right among the rest."""
    n = len(rows) or 1
    auto = [r for r in rows if not (r.final == "other" and r.pick != "other")]
    return {
        "n": len(rows),
        "raw_accuracy": sum(r.pick == r.truth for r in rows) / n,
        "to_user": 1 - len(auto) / n,
        "auto_accuracy": sum(r.final == r.truth for r in auto) / len(auto) if auto else 0.0,
        "seconds": sum(r.seconds for r in rows) / n,
    }


def read_labels(path: Path, key: str, label: str) -> dict[str, str]:
    with path.open(newline="") as f:
        return {r[key]: r[label].strip() for r in csv.DictReader(f) if r.get(label, "").strip()}


def write_template(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    trs = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class=wrap><table><tr>{th}</tr>{trs}</table></div>"


def text_table(head: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)) for r in [head, *rows])
