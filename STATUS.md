# Where this project stands — 21 August 2026

## The original idea

Codify six setups you trade by hand on low-float small caps, so a machine
would spot them and act while you slept or worked. Alpaca for data and
execution, your own scanner over the whole listed universe, a five-phase
roadmap ending in unattended automation.

Risk policy settled early and hasn't changed: 0.5% per trade, 2% daily loss
cap, three concurrent positions, marketable limit orders only.

---

## What got built

**Data.** SEC EDGAR share counts, Alpaca daily bars, a universe of ~13,000
listed common stocks rebuilt each morning.

**Scanner.** Streams every symbol on real-time SIP, flags gaps and velocity
moves against time-of-day-adjusted relative volume.

**Rules and execution.** Three setups (spike, repeat, new_high), position
sizing from a risk budget, stop and target on every entry, a 15:50 flatten.

**Safety.** Broker reconciliation at startup, positions persisted to disk,
an overexposure halt, and a daily loss cap that flattens and stops.

**Analysis.** Four tools that replay real candidates against alternative
parameters — `judgment`, `exit_sweep`, `entry_study`, `nightly`.

**Automation.** launchd jobs for the trader, health checks, and the nightly
study.

---

## What the build revealed

Two structural mistakes, both mine, both found in the first live session.

**Full-day metrics applied mid-premarket.** The daily-volume floor and the
float-turnover check compared a partial morning against a whole-day
threshold, so almost everything was rejected at 6am.

**Float used as a universe filter.** Around 8,000 symbols have no SEC share
count — mostly foreign private issuers filing annually rather than
quarterly, which is exactly the sub-$1 China small-cap category being
traded. They were silently deleted before any rule saw them. The universe
went from 4,046 to 12,951 once fixed.

Also fixed: a websocket error that killed the process instead of
reconnecting, and in-memory position tracking that a restart wiped.

---

## Two days of trading

**Day one — 20 August. Equity $100,000 → $89,427.**

Not a strategy result. Eleven positions were open against a limit of three,
none had stops, and SGLY sat at −54% untouched all day. Every restart, and
there were nine while tuning, wiped the in-memory position list: the trader
forgot its open positions, leaving them unmanaged, and opened three more
believing it held none. The loss cap never fired because it counted only
realised losses and nothing ever closed.

**Day two — 21 August. Equity $89,432 → $90,590.**

The fixes held. 1.6 million bars, 181 candidates, 5 entries, 16 exits, every
position flattened at 15:50, zero open at the close. The exact failure of day
one, fixed and demonstrated.

---

## What the analysis found

Once there was real data — 146 candidates with outcomes — three findings, in
increasing order of importance.

**The approval gate was costing you.** 14 approved trades averaged −8.4%.
The 66 you passed on, replayed under the same rules, averaged −3.2%. And 58
of 68 expired unanswered, because you were trading your own book at the time.
The gate was measuring your availability, not your judgment.

**No exit geometry works.** Every stop/target combination tested is
negative. The grid runs monotonically to its tightest corner — 5% stop, 10%
target, still −1.00% per trade. When an optimiser wants the shortest possible
hold, it is saying the entries have no edge to capture.

**No entry timing rescues it.** Waiting for a 3%, 5% or 8% pullback, waiting
for a reclaim, waiting for a red bar then a green one — all negative, win
rates 20-32%.

### The one signal worth acting on

Winners had a **lower** percent change at alert than losers (13.4% vs 18.9%)
and **lower** relative volume (68× vs 89×). By setup, `new_high` was 39%
profitable against `spike` at 23%.

The bigger the move when the alert fires, the worse the trade. That is what
buying exhaustion looks like — and the rules were built to select for
exactly the thing associated with losing.

---

## What changed as a result

**Ceilings instead of floors.** Candidates above 25% change or 150× relative
volume are now excluded. This is the hypothesis the coming week tests.

**Tighter exits.** 8% stop, 15% target, replacing 12/25.

**No approval gate.** The bot trades its rules and tells you what it did.

**A nightly study** that replays each day against a fixed grid, pools the
results, and reports — but never edits config. Adoption requires 200+ trades
in a configuration, positive expectancy, and a gap wider than two standard
errors.

---

## How next week differs

| | Last week | Next week |
|---|---|---|
| Alerts | asked permission, expired in 120s | reports what it did |
| Selection | biggest movers | moves under 25% only |
| Exits | 12% / 25% | 8% / 15% |
| Restarts | orphaned positions | recovered with real stops |
| Analysis | manual, by request | automatic at 16:30 |

Expect **fewer trades**. Some days none. That is the ceilings working.

---

## What it needs from you

**Daily: nothing.** Starts at 3:30, trades from 4:00, flattens at 15:50,
studies at 16:30. Health checks push only on failure.

**Weekly: read one notification.** It says one of five things and only the
last needs anything:

- Still collecting — nothing to do
- Nothing profitable yet — nothing to do
- Something positive, too few trades — nothing to do
- Leader not yet significant — nothing to do
- **A change clears the bar** — worth ten minutes

**Physically: lid open, plugged in.** Sleep is disabled; the screen going
dark is fine. Moving to a server removes even this, and is worth doing when
you have an unhurried weekend.

---

## The honest position

Nothing tested is profitable. The best configuration loses about 1% per
trade. The ceilings are a hypothesis drawn from two days of data and may not
survive contact with a third.

What has been established is that the machinery works — it finds real
movers, sizes them correctly, manages exits, and stops itself when losing.
That was worth building regardless of what the rules turn out to be worth.

What has *not* been established is that any of this makes money. Realistic
timeline: two or three weeks accumulating enough candidates to find a
configuration worth testing, then two or three weeks confirming it forward on
data it wasn't fitted to. Five or six weeks before live money is a sensible
question — and the answer may still be no.

Your manual trading works. Nothing here should replace it until something
demonstrably beats it.
