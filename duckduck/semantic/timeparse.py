"""
Time windows in questions, in English and Portuguese — what the rule-based
extractor reads (an LLM extractor computes its own and it is validated).

A *period* is a day, week, month or year, or an instant:

- ISO dates, with an optional time: ``2026-09-20``, ``2026-09-20 14:30``;
  slash dates day-first (``20/09/2026``), month-first only when day-first
  can't be a date (``09/25/2026``);
- ``today`` / ``hoje``, ``yesterday`` / ``ontem``, ``tomorrow`` /
  ``amanhã``, ``the day before yesterday`` / ``anteontem``;
- ``this``/``last`` ``week``/``month``/``year`` — ``esta semana``, ``semana
  passada``, ``este mês``, ``mês passado``, ``este ano``, ``ano passado``;
- a month, after ``in``/``em``/``de``/``since``… or with a year:
  ``in september``, ``setembro de 2026`` (no year: this year, or last year
  if that month hasn't come yet);
- a weekday after ``on``/``na``/``no``/``since``/``desde``…: the last one
  (today counts).

and a window is built from periods:

- ``between P and Q`` / ``entre P e Q`` / ``from P to Q`` / ``de P até Q`` →
  from the start of P to the **end** of Q (Q's whole day counts);
- ``since P`` / ``desde P`` / ``a partir de P`` → from the start of P;
  ``after P`` / ``depois de P`` → from the end of P;
- ``before P`` / ``antes de P`` → until the start of P; ``until P`` /
  ``até P`` → until the end of P;
- a period alone → that period. The current one (today, this week…) has no
  end: "logins today" means so far today.
- ``last 3 days`` / ``últimos 3 dias`` / ``nas últimas 24 horas`` → a
  window ending now; so does ``the last week / month / year`` (rolling: "in
  the last year" is the past 365 days). The calendar ones are ``this year``,
  ``ano passado``, ``semana passada``, a month's or a year's name.

Times are naive and in the same clock as ``now`` (UTC by default: "today"
is the UTC day).
"""

import calendar
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "segunda": 0, "segunda-feira": 0, "terça": 1, "terca": 1, "terça-feira": 1, "terca-feira": 1,
    "quarta": 2, "quarta-feira": 2, "quinta": 3, "quinta-feira": 3, "sexta": 4, "sexta-feira": 4,
    "sábado": 5, "sabado": 5, "domingo": 6,
}
_UNITS = {
    "minute": 1 / 60, "minutes": 1 / 60, "min": 1 / 60, "mins": 1 / 60, "minuto": 1 / 60, "minutos": 1 / 60,
    "hour": 1, "hours": 1, "hr": 1, "hrs": 1, "h": 1, "hora": 1, "horas": 1,
    "day": 24, "days": 24, "d": 24, "dia": 24, "dias": 24,
    "week": 168, "weeks": 168, "w": 168, "semana": 168, "semanas": 168,
    "month": 720, "months": 720, "mês": 720, "mes": 720, "meses": 720,
    "year": 8760, "years": 8760, "yr": 8760, "yrs": 8760, "ano": 8760, "anos": 8760,
}


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_ISO = r"\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
_SLASH = r"\d{1,2}/\d{1,2}/\d{4}"
_REL_DAY = (r"the\s+day\s+before\s+yesterday|day\s+before\s+yesterday|anteontem|today|hoje|yesterday|ontem|"
            r"tomorrow|amanh[ãa]")
_REL_SPAN = (r"(?:this|last|previous|past)\s+(?:week|month|year)|"
             r"(?:est[ae]|ess[ae]|nest[ae]|ness[ae])\s+(?:semana|m[êe]s|ano)|"
             r"(?:a\s+|o\s+|na\s+|no\s+)?(?:semana|m[êe]s|ano)\s+passad[oa]")
_MONTH = rf"(?:{_alt(_MONTHS)})(?:\s+(?:de\s+)?\d{{4}})?"
_WEEKDAY = _alt(_WEEKDAYS)
#: a period that stands on its own (weekdays and bare month names need a preposition — "may" is a verb)
_PERIOD = rf"(?:{_ISO}|{_SLASH}|{_REL_DAY}|{_REL_SPAN}|(?:{_alt(_MONTHS)})\s+(?:de\s+)?\d{{4}})"
#: a period after a preposition: also a weekday or a month without a year
_PERIOD_AFTER = rf"(?:{_PERIOD}|{_MONTH}|{_WEEKDAY})"
_ART = r"(?:(?:the|o|a|os|as|on|na|no|em|in)\s+)?"

_BETWEEN = re.compile(
    rf"\b(?:between|entre|from|de|desde)\s+{_ART}(?P<a>{_PERIOD_AFTER})\s+(?:and|e|to|till|until|through|thru|a|at[ée])"
    rf"\s+{_ART}(?P<b>{_PERIOD_AFTER})(?![\w-])", re.I)
_SINCE = re.compile(rf"\b(?P<w>since|starting(?:\s+from)?|after|desde|a\s+partir\s+d[eoa]|depois\s+d[eoa])\s+{_ART}"
                    rf"(?P<a>{_PERIOD_AFTER})(?![\w-])", re.I)
_BEFORE = re.compile(rf"\b(?P<w>before|until|till|antes\s+d[eoa]|at[ée])\s+{_ART}(?P<a>{_PERIOD_AFTER})(?![\w-])",
                     re.I)
_ON = re.compile(rf"\b(?:on|in|during|na|no|em|durante)\s+{_ART}(?P<a>{_MONTH}|{_WEEKDAY})(?![\w-])", re.I)
_ALONE = re.compile(rf"(?<![\w-])(?P<a>{_PERIOD})(?![\w-])", re.I)
_LAST_N = re.compile(
    rf"\b(?:(?:in|during|over|within|for|nas|nos|durante)\s+)?(?:the\s+|as\s+|os\s+)?"
    rf"(?:last|past|previous|[úu]ltim[oa]s?|passad[oa]s)\s+(\d+(?:[.,]\d+)?)?\s*({_alt(_UNITS)})\b", re.I)


class Window:
    """A resolved time window: ``last_hours``, or ``start`` (inclusive) / ``end`` (exclusive)."""

    def __init__(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
                 last_hours: Optional[float] = None, text: str = ""):
        self.start, self.end, self.last_hours, self.text = start, end, last_hours, text


def find_window(text: str, now: datetime) -> Optional[Tuple[Window, int, int]]:
    """The time window ``text`` asks about, and where it is (to take those words out) — or ``None``."""
    m = _LAST_N.search(text)
    if m:
        amount = float((m.group(1) or "1").replace(",", "."))
        return Window(last_hours=amount * _UNITS[m.group(2).lower()], text=m.group(0).strip()), m.start(), m.end()
    for pattern in (_BETWEEN, _SINCE, _BEFORE, _ON, _ALONE):
        for m in pattern.finditer(text):
            window = _window(pattern, m, now)
            if window is not None:
                return window, m.start(), m.end()
    return None


def _window(pattern: "re.Pattern", m: "re.Match", now: datetime) -> Optional[Window]:
    a = _period(m.group("a"), now)
    if a is None:
        return None
    text = m.group(0).strip()
    if pattern is _BETWEEN:
        b = _period(m.group("b"), now)
        if b is None:
            return None
        start, end = min(a[0], b[0]), max(a[1], b[1])
        return Window(start=start, end=end, text=text)
    if pattern is _SINCE:
        after = m.group("w").lower().startswith(("after", "depois"))
        return Window(start=a[1] if after else a[0], text=text)
    if pattern is _BEFORE:
        until = m.group("w").lower().startswith(("until", "till", "at"))
        return Window(end=a[1] if until else a[0], text=text)
    start, end = a
    if start <= now < end:  # the current day/week/month: so far
        end = None
    return Window(start=start, end=end, text=text)


def _period(raw: str, now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """``[start, end)`` of one period expression (an instant has ``start == end``)."""
    s = " ".join(raw.lower().split())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    one = timedelta(days=1)
    if re.fullmatch(_ISO, s):
        parsed = _iso(s)
        if parsed is None:
            return None
        return (parsed, parsed) if (" " in s or "t" in s) else (parsed, parsed + one)
    if re.fullmatch(_SLASH, s):
        a, b, y = (int(x) for x in s.split("/"))
        for d, mo in ((a, b), (b, a)):  # day-first; month-first only when day-first isn't a date
            try:
                start = datetime(y, mo, d)
                return start, start + one
            except ValueError:
                continue
        return None
    if re.fullmatch(r"(the\s+)?day before yesterday|anteontem", s):
        return day - 2 * one, day - one
    if s in ("today", "hoje"):
        return day, day + one
    if s in ("yesterday", "ontem"):
        return day - one, day
    if s in ("tomorrow", "amanhã", "amanha"):
        return day + one, day + 2 * one
    span = re.fullmatch(r"(this|last|previous|past|est[ae]|ess[ae]|nest[ae]|ness[ae])\s+(week|month|year|semana|m[êe]s|ano)"
                        r"|(?:a |o |na |no )?(semana|m[êe]s|ano) passad[oa]", s)
    if span:
        unit = span.group(2) or span.group(3)
        unit = {"semana": "week", "mês": "month", "mes": "month", "ano": "year"}.get(unit, unit)
        last = bool(span.group(3)) or (span.group(1) or "") in ("last", "previous", "past")
        return _span(unit, day, last)
    month = re.fullmatch(rf"({_alt(_MONTHS)})(?:\s+(?:de\s+)?(\d{{4}}))?", s)
    if month:
        mo = _MONTHS[month.group(1)]
        year = int(month.group(2)) if month.group(2) else (now.year if mo <= now.month else now.year - 1)
        start = datetime(year, mo, 1)
        return start, datetime(year + (mo == 12), mo % 12 + 1, 1)
    if s in _WEEKDAYS:
        back = (day.weekday() - _WEEKDAYS[s]) % 7
        return day - back * one, day - back * one + one
    return None


def _span(unit: str, day: datetime, last: bool) -> Tuple[datetime, datetime]:
    if unit == "week":
        start = day - timedelta(days=day.weekday())  # weeks start on Monday
        start = start - timedelta(days=7) if last else start
        return start, start + timedelta(days=7)
    if unit == "month":
        start = day.replace(day=1)
        if last:
            start = (start - timedelta(days=1)).replace(day=1)
        days = calendar.monthrange(start.year, start.month)[1]
        return start, start + timedelta(days=days)
    start = day.replace(month=1, day=1)
    if last:
        start = start.replace(year=start.year - 1)
    return start, start.replace(year=start.year + 1)


def _iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("t", "T").replace(" ", "T"))
    except ValueError:
        return None
