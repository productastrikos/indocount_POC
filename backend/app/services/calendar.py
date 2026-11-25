"""Shift calendar.

Two shifts, each with 8.5 hours of scheduled presence, a 30-minute unpaid
lunch and two paid 10-minute breaks:
    Shift 1  06:00-14:30   breaks 08:00, 12:30   lunch 10:00-10:30
    Shift 2  14:30-23:00   breaks 16:30, 21:00   lunch 18:30-19:00
Productive time = 510 - 30 - 20 = 460 min; paid time = 480 min.
Second shift earns a +$1/h differential (config)."""
from datetime import date, datetime, time, timedelta

from ..config import get_settings

SHIFTS = {
    1: {"start": time(6, 0), "end": time(14, 30),
        "breaks": [(time(8, 0), time(8, 10)), (time(12, 30), time(12, 40))],
        "lunch": (time(10, 0), time(10, 30))},
    2: {"start": time(14, 30), "end": time(23, 0),
        "breaks": [(time(16, 30), time(16, 40)), (time(21, 0), time(21, 10))],
        "lunch": (time(18, 30), time(19, 0))},
}
DAY_START = time(6, 0)
DAY_END = time(23, 0)
PRESENCE_MIN = 510
UNPAID_LUNCH_MIN = 30
PAID_BREAK_MIN = 20


def _m(t: time) -> int:
    return t.hour * 60 + t.minute


def _productive_windows_for_shift(shift: int):
    s = SHIFTS[shift]
    stops = sorted([s["lunch"], *s["breaks"]], key=lambda w: _m(w[0]))
    windows, cur = [], _m(s["start"])
    for a, b in stops:
        if _m(a) > cur:
            windows.append((cur, _m(a)))
        cur = max(cur, _m(b))
    if _m(s["end"]) > cur:
        windows.append((cur, _m(s["end"])))
    extra = get_settings().extra_break_minutes
    if extra and windows:  # configurable extra break trimmed from the last window
        a, b = windows[-1]
        windows[-1] = (a, max(a, b - extra))
    return windows


WINDOWS = {sh: _productive_windows_for_shift(sh) for sh in SHIFTS}  # minutes from midnight


def productive_minutes_per_shift(shift: int = 1) -> float:
    return float(sum(b - a for a, b in WINDOWS[shift]))


def paid_minutes_per_shift() -> float:
    return float(PRESENCE_MIN - UNPAID_LUNCH_MIN)


def shift_of(dt: datetime) -> int | None:
    m = dt.hour * 60 + dt.minute
    for sh, s in SHIFTS.items():
        if _m(s["start"]) <= m < _m(s["end"]):
            return sh
    return None


def shift_bounds(d: date, shift: int) -> tuple[datetime, datetime]:
    s = SHIFTS[shift]
    return datetime.combine(d, s["start"]), datetime.combine(d, s["end"])


def _day_windows(d: date):
    """Productive windows of a day as (datetime_start, datetime_end, shift)."""
    out = []
    base = datetime.combine(d, time(0, 0))
    for sh in (1, 2):
        for a, b in WINDOWS[sh]:
            out.append((base + timedelta(minutes=a), base + timedelta(minutes=b), sh))
    return out


def productive_minutes(a: datetime, b: datetime) -> float:
    if b <= a:
        return 0.0
    total = 0.0
    d = a.date()
    while d <= b.date():
        for ws, we, _ in _day_windows(d):
            lo, hi = max(a, ws), min(b, we)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60
        d += timedelta(days=1)
    return total


def segments(a: datetime, b: datetime):
    """Split [a, b) into productive pieces per (date, hour, shift).
    Returns [(date, hour, shift, productive_minutes, start, end)]."""
    out = []
    if b <= a:
        return out
    d = a.date()
    while d <= b.date():
        for ws, we, sh in _day_windows(d):
            lo, hi = max(a, ws), min(b, we)
            cur = lo
            while cur < hi:
                nxt = min(hi, cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                out.append((cur.date(), cur.hour, sh, (nxt - cur).total_seconds() / 60, cur, nxt))
                cur = nxt
        d += timedelta(days=1)
    return out


def add_productive(t: datetime, minutes: float) -> datetime:
    """Advance t by `minutes` of productive time (skipping breaks and nights)."""
    remaining = max(0.0, minutes)
    d = t.date()
    for _ in range(400):  # bounded walk (~1 year)
        for ws, we, _sh in _day_windows(d):
            if we <= t:
                continue
            start = max(t, ws)
            avail = (we - start).total_seconds() / 60
            if remaining <= avail:
                return start + timedelta(minutes=remaining)
            remaining -= avail
        d += timedelta(days=1)
        t = datetime.combine(d, time(0, 0))
    return t


def planned_minutes_in_hour(d: date, hour: int, shift: int) -> float:
    a = datetime.combine(d, time(hour, 0))
    b = a + timedelta(hours=1)
    lo, hi = shift_bounds(d, shift)
    return productive_minutes(max(a, lo), min(b, hi)) if min(b, hi) > max(a, lo) else 0.0


def hours_for_shift(shift: int):
    s = SHIFTS[shift]
    first = s["start"].hour
    last = s["end"].hour if s["end"].minute else s["end"].hour - 1
    return list(range(first, last + 1))


def paid_minutes_elapsed(d: date, shift: int, now: datetime) -> float:
    """Paid minutes worked so far in a shift (presence minus unpaid lunch)."""
    lo, hi = shift_bounds(d, shift)
    if now <= lo:
        return 0.0
    end = min(now, hi)
    presence = (end - lo).total_seconds() / 60
    la, lb = SHIFTS[shift]["lunch"]
    la, lb = datetime.combine(d, la), datetime.combine(d, lb)
    lunch = max(0.0, (min(end, lb) - max(lo, la)).total_seconds() / 60) if end > la else 0.0
    return max(0.0, presence - lunch)


def is_production_time(dt: datetime) -> bool:
    return any(ws <= dt < we for ws, we, _ in _day_windows(dt.date()))
