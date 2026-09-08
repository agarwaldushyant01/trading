"""Stop the bot opening positions minutes before it closes them.

    python3 apply_late_entry_fix.py

On 2026-09-08 it opened SPAL at 15:51, ANY and CBAT at 15:52, LOVE and MGRX
at 15:56, HDGE at 15:59 and AXG at 16:01. Every one was closed within seconds
by the 15:50 flatten — six round trips in eight minutes paying the spread
twice each for nothing, and one entry after the market had shut.

Applied as a patch rather than a file replacement: my copy of paper.py does
not have the calendar wiring or the target fixes made on this machine, and
replacing the file would silently revert them. That has happened four times.
"""

import pathlib

p = pathlib.Path("engine/paper.py")
s = p.read_text()

if "no_entry_before_close_min" in s:
    print("already applied")
    raise SystemExit(0)

old = """        if self.halted_for_day or self._halted_for_overexposure():
            return
        if alert.symbol in self.taken_today:
            return"""

new = """        if self.halted_for_day or self._halted_for_overexposure():
            return

        # No entries near the close. A position opened at 15:51 has nine
        # minutes to live and is closed by the time rule before it can do
        # anything except pay the spread. A trade needs room to work, and
        # stopping entries well before the flatten is the only sensible
        # reading of a rule that says nothing carries overnight.
        from tools.calendar import flatten_time

        flatten = flatten_time(now.date(),
                               self.cfg["execution"]["hard_exit_time"])
        buffer_min = self.cfg["execution"].get("no_entry_before_close_min", 45)
        cutoff = flatten.hour * 60 + flatten.minute - buffer_min
        if now.hour * 60 + now.minute >= cutoff:
            return

        if alert.symbol in self.taken_today:
            return"""

if old not in s:
    print("PATTERN NOT FOUND — consider_with_stop may have changed")
    raise SystemExit(1)

p.write_text(s.replace(old, new))
print("patched engine/paper.py")

# And the setting, so the buffer is visible rather than buried in a default.
c = pathlib.Path("config/rules.yaml")
t = c.read_text()
if "no_entry_before_close_min" not in t:
    t = t.replace("  hard_exit_time:",
"""  # Stop opening positions this many minutes before the flatten. At 45,
  # nothing opens after 15:05 on a normal day or 12:05 on a half day.
  no_entry_before_close_min: 45

  hard_exit_time:""")
    c.write_text(t)
    print("patched config/rules.yaml")
