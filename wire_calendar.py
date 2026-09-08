"""Wire the market calendar into start.sh, the trader and the health check.

    python3 wire_calendar.py

Three places need it, and the third is the one that matters:

  start.sh        exit on a holiday instead of rebuilding reference data for
                  13,000 symbols and streaming nothing for twelve hours.

  healthcheck     do not restart or alert on a closed day. Silence on a
                  holiday is correct, and an alarm that cries wolf on
                  Thanksgiving is an alarm nobody reads in March.

  PaperTrader     flatten at 12:50 on a half day. The 15:50 flatten never
                  arrives when the market closes at 13:00, so positions
                  carry overnight — precisely what the flatten exists to
                  prevent, failing silently twice a year.
"""

import pathlib
import re

# --- start.sh --------------------------------------------------------
sh = pathlib.Path("start.sh")
s = sh.read_text()
if "tools.calendar" not in s:
    s = s.replace('''DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
    echo "  weekend, not starting."
    exit 0
fi''',
'''DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
    echo "  weekend, not starting."
    exit 0
fi

# Market holidays. Without this the bot woke on Labor Day 2026, rebuilt
# reference data for 13,000 symbols and streamed nothing for twelve hours.
if ! "$PYTHON" -m tools.calendar --quiet 2>/dev/null; then
    echo "  market closed today, not starting."
    exit 0
fi''')
    sh.write_text(s)
    print("start.sh patched")
else:
    print("start.sh already wired")

# --- healthcheck ------------------------------------------------------
hc = pathlib.Path("tools/healthcheck.py")
h = hc.read_text()
if "is_trading_day" not in h:
    h = h.replace('''def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END''',
'''def in_session(now: datetime) -> bool:
    """Weekday, market open, and inside the session.

    Restarting or alerting on a holiday is worse than useless: the scanner is
    correctly idle, and an alarm that cries wolf on Thanksgiving is one
    nobody reads in March.
    """
    from tools.calendar import is_trading_day

    if not is_trading_day(now.date()):
        return False
    return SESSION_START <= now.time() <= SESSION_END''')
    hc.write_text(h)
    print("healthcheck patched")
else:
    print("healthcheck already wired")

# --- PaperTrader half-day flatten -------------------------------------
p = pathlib.Path("engine/paper.py")
t = p.read_text()
if "flatten_time" not in t:
    old = '''hard_exit = time.fromisoformat(self.cfg["execution"]["hard_exit_time"])'''
    new = '''# Ten minutes before the close, which is 12:50 on a half day. A
        # 15:50 flatten never arrives when the market shuts at 13:00, so
        # positions carry overnight twice a year without a word.
        from tools.calendar import flatten_time
        hard_exit = flatten_time(now.date(),
                                 self.cfg["execution"]["hard_exit_time"])'''
    if old in t:
        p.write_text(t.replace(old, new))
        print("paper.py patched")
    else:
        print("paper.py: pattern not found, check line ~630")
else:
    print("paper.py already wired")
