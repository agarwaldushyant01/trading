"""Make notifications survive non-Latin-1 characters.

    python3 fix_notify.py

ntfy carries the title and body in HTTP headers, which must be Latin-1
encodable. An em-dash in a message text raised

    'latin-1' codec can't encode character '\\u2014'

and the alert was dropped silently — so on 2026-09-04 the trader received no
phone notifications at all while the bot took seven entries.

Rather than hunting em-dashes through every message string, this sanitises
once at the point of sending: common typographic characters are replaced with
their ASCII equivalents and anything else unencodable is dropped. A slightly
plainer notification is always better than none.
"""

import pathlib
import re

p = pathlib.Path("alerts/notify.py")
src = p.read_text()

if "_latin1_safe" in src:
    print("already patched")
    raise SystemExit(0)

helper = '''

def _latin1_safe(text: str) -> str:
    """Return text that can go in an HTTP header.

    ntfy puts the title and message in headers, which are Latin-1 only. A
    single em-dash silently killed every notification on 2026-09-04.
    """
    if not isinstance(text, str):
        return text
    swaps = {"\\u2014": "-", "\\u2013": "-", "\\u2019": "'", "\\u2018": "'",
             "\\u201c": '"', "\\u201d": '"', "\\u2026": "...",
             "\\u00b7": "-", "\\u2192": "->"}
    for bad, good in swaps.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "ignore").decode("latin-1")
'''

# Insert the helper after the imports.
lines = src.split("\n")
insert_at = 0
for i, line in enumerate(lines):
    if line.startswith(("import ", "from ")):
        insert_at = i + 1
src = "\n".join(lines[:insert_at]) + helper + "\n".join(lines[insert_at:])

# Sanitise at the top of send().
match = re.search(r"(\n(\s+)def send\(self[^\n]*\n)", src)
if not match:
    print("could not find a send() method to patch", file=__import__("sys").stderr)
    raise SystemExit(1)

indent = match.group(2) + "    "
body_start = match.end(1)

# Skip a docstring if there is one.
rest = src[body_start:]
if rest.lstrip().startswith(('"""', "'''")):
    quote = rest.lstrip()[:3]
    close = rest.index(quote, rest.index(quote) + 3) + 3
    body_start += close
    src_head, src_tail = src[:body_start], src[body_start:]
    src = src_head + f"\n{indent}title = _latin1_safe(title)\n" \
                     f"{indent}message = _latin1_safe(message)" + src_tail
else:
    src = src[:body_start] + f"{indent}title = _latin1_safe(title)\n" \
                             f"{indent}message = _latin1_safe(message)\n" \
          + src[body_start:]

p.write_text(src)
print("patched alerts/notify.py")
