"""The tick list of `art15 review`: plain curses, so no new dependency."""

import curses
import locale
import sys
from dataclasses import dataclass
from typing import Any

HELP = "arrows: move   space: tick   a: all safe   n: none   enter: save   q: cancel"
ENTER = {10, 13, curses.KEY_ENTER}


@dataclass
class Row:
    slug: str
    name: str
    contact: str
    msgs: int
    note: str
    risky: bool
    ticked: bool
    detail: str = ""


def run(rows: list[Row]) -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("`art15 review` needs a terminal.")
    locale.setlocale(locale.LC_ALL, "")  # names can be non-ASCII
    return bool(curses.wrapper(tick, rows))


def _put(screen: Any, y: int, text: str, attr: int = 0) -> None:
    try:
        screen.addnstr(y, 0, text, screen.getmaxyx()[1] - 1, attr)
    except curses.error:  # a terminal too small for this line
        pass


def tick(screen: Any, rows: list[Row]) -> bool:
    """Tick rows in place. True saves, False leaves without saving."""
    try:
        curses.curs_set(0)
    except curses.error:  # not every terminal can hide the cursor
        pass
    at = top = 0
    while True:
        height = max(1, screen.getmaxyx()[0] - 4)
        top = min(at, max(top, at - height + 1))
        screen.erase()
        _put(screen, 0, f"{sum(r.ticked for r in rows)} of {len(rows)} chosen. Risky ones (!) start unticked.")
        for y, r in enumerate(rows[top : top + height]):
            text = f"[{'x' if r.ticked else ' '}] {r.name[:24]:<24} {r.contact[:34]:<34} {r.msgs:>5}  {r.note}"
            _put(screen, y + 2, text, curses.A_REVERSE if top + y == at else 0)
        _put(screen, height + 2, rows[at].detail)
        _put(screen, height + 3, HELP)
        screen.refresh()
        key = screen.getch()
        if key in (curses.KEY_UP, ord("k")):
            at = max(at - 1, 0)
        elif key in (curses.KEY_DOWN, ord("j")):
            at = min(at + 1, len(rows) - 1)
        elif key == curses.KEY_PPAGE:
            at = max(at - height, 0)
        elif key == curses.KEY_NPAGE:
            at = min(at + height, len(rows) - 1)
        elif key == ord(" "):
            rows[at].ticked = not rows[at].ticked
        elif key == ord("a"):
            for r in rows:
                r.ticked = r.ticked or not r.risky
        elif key == ord("n"):
            for r in rows:
                r.ticked = False
        elif key in ENTER:
            return True
        elif key == ord("q"):  # not Esc: a stray one would lose every tick
            return False
