"""Is the market open today, and when does it close?

    python -m tools.calendar              # today
    python -m tools.calendar --date 2026-11-27
    python -m tools.calendar --list       # the year's closures

start.sh has always skipped weekends and never skipped holidays, so on
2026-09-07 — Labor Day — the bot woke at 03:25, rebuilt reference data for
13,000 symbols, opened a websocket and sat receiving nothing for twelve
hours. Harmless, but it also means a health check that alerts on silence has
nothing to distinguish "market closed" from "feed broken".

Half-days matter more than closures. On the day after Thanksgiving and
Christmas Eve the market closes at 13:00, so a 15:50 flatten never fires and
positions carry overnight — which is exactly the failure mode the flatten
exists to prevent.

Dates are hard-coded rather than fetched. Alpaca does publish a calendar
endpoint, but a network call at 03:30 that fails leaves the bot with no
answer at all, and these dates are known years ahead.
"""

from __future__ import annotations

from datetime import date, time

# NYSE closures. Extend as the years roll on.
HOLIDAYS = {
    2026: [
        (1, 1),    # New Year's Day
        (1, 19),   # Martin Luther King Jr. Day
        (2, 16),   # Washington's Birthday
        (4, 3),    # Good Friday
        (5, 25),   # Memorial Day
        (6, 19),   # Juneteenth
        (7, 3),    # Independence Day observed
        (9, 7),    # Labor Day
        (11, 26),  # Thanksgiving
        (12, 25),  # Christmas
    ],
    2027: [
        (1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5),
        (9, 6), (11, 25), (12, 24),
    ],
}

# Days the market closes at 13:00.
HALF_DAYS = {
    2026: [(11, 27), (12, 24)],
    2027: [(11, 26)],
}


def is_holiday(day: date) -> bool:
    return (day.month, day.day) in HOLIDAYS.get(day.year, [])


def is_half_day(day: date) -> bool:
    return (day.month, day.day) in HALF_DAYS.get(day.year, [])


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and not is_holiday(day)


def close_time(day: date) -> time:
    """When the regular session ends."""
    return time(13, 0) if is_half_day(day) else time(16, 0)


def flatten_time(day: date, normal: str = "15:50") -> time:
    """When to be flat.

    Ten minutes before the close, so a half day flattens at 12:50 rather than
    at a 15:50 that will never arrive.
    """
    if is_half_day(day):
        return time(12, 50)
    hour, minute = (int(x) for x in normal.split(":"))
    return time(hour, minute)


def why_closed(day: date) -> str | None:
    if day.weekday() >= 5:
        return "weekend"
    if is_holiday(day):
        return "market holiday"
    return None


def main() -> None:
    import argparse
    import sys

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None)
    p.add_argument("--list", action="store_true")
    p.add_argument("--quiet", action="store_true",
                   help="exit 0 if trading, 1 if not; print nothing")
    args = p.parse_args()

    if args.list:
        for year in sorted(HOLIDAYS):
            print(f"\n  {year} closures")
            for month, dom in HOLIDAYS[year]:
                d = date(year, month, dom)
                print(f"    {d}  {d:%A}")
            if HALF_DAYS.get(year):
                print(f"  {year} half days (close 13:00)")
                for month, dom in HALF_DAYS[year]:
                    print(f"    {date(year, month, dom)}")
        print()
        return

    day = date.fromisoformat(args.date) if args.date else date.today()
    trading = is_trading_day(day)

    if args.quiet:
        sys.exit(0 if trading else 1)

    if not trading:
        print(f"  {day} — CLOSED ({why_closed(day)})")
    elif is_half_day(day):
        print(f"  {day} — open, HALF DAY, closes 13:00, "
              f"flatten at {flatten_time(day):%H:%M}")
    else:
        print(f"  {day} — open, closes 16:00, "
              f"flatten at {flatten_time(day):%H:%M}")


if __name__ == "__main__":
    main()
