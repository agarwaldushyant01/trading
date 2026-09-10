# CLAUDE.md

Context for working on this repository. Read this before changing anything.

---

## What this is

An automated day-trading bot for low-float US small caps, running on paper
through Alpaca. It scans ~13,000 symbols on real-time SIP data, detects chart
patterns on five-minute bars, sizes positions against a risk budget, and
manages exits.

**It is not currently profitable.** Three weeks in, every session has lost
money. Read "Where this actually stands" before assuming any part of it works.

---

## The two rules that matter most

**1. Run the checks before shipping anything.**

```bash
python3 -m harness.replay --check          # 5 regressions, ~10 seconds
python3 -m harness.session --date <day>    # session-level assertions
```

Every one of those regressions is a bug that reached production and cost real
paper money. Three of them shipped in code that had passing unit tests. The
checks exist because component tests were not enough.

**2. Send patches, not whole files.**

The owner applies changes on their machine. Replacing a file with a copy from
elsewhere silently reverts work that is already there — this happened four
times in the first three weeks and caused several apparent "new" bugs. Use
targeted `sed` or a small Python script that finds and replaces a specific
block, and have it print whether the pattern matched.

---

## Architecture

```
data/reference.py       universe build: SEC share counts + daily bars
scanner/scanner.py      streaming scanner (legacy path, spike rules)
patterns/
  geometry.py           swing points, trendlines, the three-tap rule
  confluence.py         demand zone, bottom wick, small bodies, quarter levels
  detect.py             falling wedge, pennant, ascending triangle, retest
  character.py          stock character from daily history
  dive.py               dumpster diving (thin-market fallback only)
engine/
  paper.py              PaperTrader: sizing, orders, exits, risk limits
  rules.py              the older spike/repeat/new_high rules
  alerts.py             the shared Alert type
drivers/
  pattern_live.py       THE LIVE PATH — patterns on 5-minute bars
  paper_live.py         older path, spike rules
harness/
  broker.py             fake Alpaca that refuses orders the way Alpaca does
  replay.py             regression checks
  session.py            whole-session property assertions
tools/                  analysis, logging, tuning, calendar, health check
```

---

## Hard-won knowledge

These were expensive to learn. Do not re-derive them.

### Alpaca behaviour

- **Market orders do not fill outside 09:30–16:00.** `close_position()`
  submits a market order. A stop triggered at 07:00 sat queued until the open
  and filled 23% below its level. Extended-hours exits must be marketable
  limit orders with `extended_hours=True`.
- **`last_equity` rolls at the session boundary.** Comparing equity against it
  outside market hours shows a drawdown that never happened. The loss cap
  fired at 00:17 on a phantom 3.8% loss and flattened the book.
- **Shares held for an open order cannot be sold again.** The refusal is
  `insufficient qty available`. Treating that as permanent left positions
  unmanaged for hours.
- **Positions do not appear the instant an order is submitted.** Several bars
  closing in the same second each passed the position-count check.
- **One websocket connection.** Five-minute bars are aggregated from the
  one-minute stream, not subscribed separately.

### The pattern behind most defects

Nearly every bug has been **temporal**: the loss cap at midnight, market
orders in premarket, a 15:50 flatten on a day the market closes at 13:00,
entries at 15:59 closed seconds later.

They share a cause — code written from a picture of a function and its inputs
rather than a session and its clock. When adding anything time-dependent, ask
what it does at 04:00, at 15:51, on a half day, and at midnight.

`PaperTrader.clock` is injectable for exactly this reason. Use `self.clock()`,
never `datetime.now()`.

### Strategy findings

- Seven mechanical entry rules have been tested. **All negative.**
- 210 trades across seven exit variants. **All negative.** Letting winners run
  raised the win rate from 24% to 39% and made expectancy *worse*, because the
  average winner stayed at +3.7% while the average loser doubled.
- The detector agrees with the owner's own decisions **53% of the time** —
  chance. Six rounds of tuning moved it between 41% and 53%.
- Buying the biggest movers is **backwards**: winners had a lower median
  percent change at alert (13.4%) than losers (18.9%). Ceilings, not floors.
- The owner's manual trading is profitable: 64 logged trades, ~62% win rate,
  4:1 payoff, roughly +14% expectancy. The gap between that and the bot is the
  whole problem.

---

## Where this actually stands

**Working and proven:** universe build, scanner, pattern geometry, risk
sizing, exit management, broker reconciliation, position persistence,
scheduling, health checks that self-heal, the harnesses.

**Not working:** entry selection. Nothing tested has an edge.

**The current experiment:** the owner logs every manual trade and pass with
`tools/log.py`. At 100 labelled decisions, `tools/tune.py` searches nine
detector parameters against them with a date-split holdout. The one time a
threshold was calibrated this way — the character filter's pump-and-dump rate
— the hand-picked value was wrong by nearly a factor of two.

Currently at **64 of 100**.

**Open gaps the detector has no representation for**, from the owner's own
logged reasons: news catalysts, Twitter posts, reverse-split runs, "volume at
open", "slow mover" as a pass reason, and "missed this" being recorded as a
pass when it was not a decision to reject.

---

## Daily operation

```
03:25  Mac wakes (pmset)
03:30  launchd runs start.sh -> exec drivers.pattern_live
04:00  trading begins
       health check every 15 min, restarts on failure
15:05  no new entries (45 min before the flatten)
15:50  flatten, nothing carries overnight
16:30  nightly study
```

Three launchd jobs: `com.trading.scanner`, `com.trading.healthcheck`,
`com.trading.study`.

Notifications go to ntfy. **Titles and bodies must be Latin-1 encodable** —
an em-dash silently killed every notification for a full session.

---

## Commands

```bash
# checks
python3 -m harness.replay --check
python3 -m harness.session --date 2026-09-08

# after a session
python3 -m tools.day_report
python3 -m tools.replay_exits --date 2026-09-08

# the tuning loop
python3 -m tools.log SYM 1.23 1.45 --note "why"
python3 -m tools.log --passed SYM --note "why not"
python3 -m tools.log --show
python3 -m tools.tune

# maintenance
python3 -m tools.build_character        # weekly
python3 -m tools.calendar               # is the market open
./start.sh                              # manual start
launchctl kickstart -k gui/$(id -u)/com.trading.scanner
```

---

## Working with the owner

They trade these setups profitably by hand and have taught the system its
chart knowledge directly — support and resistance, the three-tap rule,
confluences, wick rejection, fakeout versus breakout, volume as a qualifier
rather than a signal.

**Their judgment is the reference, not mine.** When a threshold disagrees with
a decision they made, the threshold is what is wrong. Ask rather than guess:
every time a parameter has been set by asking them, it moved substantially.

They have very limited time and want near-zero daily involvement — the target
is roughly thirty minutes a week reading one summary. Do not build things that
need daily attention.

Be direct about what is not working. Three weeks of paper losses have been
frustrating, and softening the picture would not have helped.
