"""GDPR deadlines. Art. 12(3): one month from receipt, extendable by two further months.

Periods are counted as in Regulation 1182/71 Art. 3: a month ends on the same date in the
following month, or on the last day of that month when the date does not exist (Jan 31 -> Feb 28/29),
and a period ending on a Saturday or Sunday runs to the next working day.
"""

import calendar
from datetime import date, timedelta

ADMONITION_DAYS = 14  # the datenanfragen admonition letter asks for an answer within two weeks


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def next_working_day(d: date) -> date:
    # shortcut: weekends only, no public holidays; holidays differ per country.
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def request_deadline(sent: date, extended: bool = False) -> date:
    return next_working_day(add_months(sent, 3 if extended else 1))


def admonition_deadline(sent: date) -> date:
    return next_working_day(sent + timedelta(days=ADMONITION_DAYS))
