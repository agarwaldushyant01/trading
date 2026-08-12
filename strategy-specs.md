# Strategy specs — v0.1 draft

Every number below is my guess, filled in so you have something to argue with.
Anything marked **[CHECK]** is one I'm least confident about. Change them and
I'll regenerate the code.

Conventions: all times ET. "Listed" = Nasdaq / NYSE / AMEX only, no OTC.
`equity` = account value at session start.

---

## Global risk policy

Applies to every setup. These are hard limits enforced in code, not guidelines.

| Rule | Value |
|---|---|
| Account equity | $50,000 |
| Risk per trade | 0.5% of equity = $250 |
| Max position size | 10% of equity, or 1% of the stock's 20-day average volume, whichever is smaller |
| Max concurrent positions | 3 |
| Max daily loss | 2% of equity → flatten everything, stop trading for the day |
| Max consecutive losing days | 3 → bot stops, requires manual restart |
| Order type | Marketable limit only, never market. Extended hours: limit only (Alpaca requirement) |
| Slippage assumption in backtest | 2% for velocity entries, 0.5% otherwise **[CHECK]** |

**Position sizing formula (used by all setups):**

```
risk_dollars = equity * 0.005                       # $250
risk_dollars = min(risk_dollars, daily_budget_left)  # never exceed the day's cap
risk_dollars = risk_dollars * setup_multiplier       # 1.0 default, 0.5 overnight, 0.25 Setup 3

stop_distance = entry_price - stop_price
if stop_distance < 0.5 * ATR(14):                    # stop inside the noise
    stop_price   = entry_price - 0.5 * ATR(14)       # widen it
    stop_distance = entry_price - stop_price

shares = floor(risk_dollars / stop_distance)
shares = min(shares, equity * 0.10 / entry_price)    # 10% concentration cap
shares = min(shares, avg_20d_volume * 0.01)          # liquidity cap
```

**Daily budget.** `daily_budget_left` starts each session at 2% of equity ($1,000)
and is decremented by each realized loss. At zero: flatten and stop for the day.
A new trade is rejected if its risk exceeds what remains, rather than being
silently downsized to fit.

**Skip conditions.** Skip the trade if the stop is more than 20% below entry
(too wide to size sensibly), or if the resulting share count is zero.

**Halt handling:** if a position is open and the stock halts, cancel all resting
orders. On resumption, re-evaluate from scratch rather than restoring the old
stop — the price may have gapped through it.

---

## Setup 1 — VWAP reclaim

*Most mechanical of the six. Build this first.*

**Universe filter**
- Listed, price $1.00–$20.00
- Shares outstanding < 30M (float proxy)
- Session volume > 3× the 20-day average by the time of signal
- Stock printed a high of day at least 15% above prior close earlier in the session **[CHECK]**

**Setup condition (must happen before entry is possible)**
1. Stock makes its HOD
2. Price subsequently trades below VWAP for at least two consecutive 5-minute bars

**Entry trigger**
- First 5-minute bar that *closes* above VWAP
- That bar's volume ≥ average of the prior three 5-minute bars
- Enter at the open of the following bar, marketable limit at +0.5%
- No entries after 15:00

**Stop**
- Low of the reclaim bar, minus one cent
- If that is more than 8% below entry, skip the trade

**Time stop**
- Exit at 15:50 regardless of P&L
- Also exit if price closes below VWAP on any subsequent 5-minute bar **[CHECK]**

**Target**
- Primary: prior day's high, or +15% from entry, whichever comes first
- You described holding overnight and selling at the next open. That is a
  **different strategy** with different risk (overnight gap exposure). Spec'd
  separately as Setup 1b so the two get tested independently.

**Invalidation**
- Two consecutive 5-min closes back below VWAP
- Volume dries up: three consecutive bars below 0.3× the reclaim bar's volume

**Verdict:** automate.

---

## Setup 1b — VWAP reclaim, overnight hold

Identical to Setup 1 through entry. Differences:

- No intraday target; hold through the close
- Exit on the next session's open (first 5 minutes), market-on-open order
- Stop still applies intraday. If stopped out, no overnight position
- Hard rule: overnight positions sized at **half** the normal risk, since the
  stop cannot protect you against a gap

**Verdict:** automate, but only after Setup 1's numbers are known. Test both,
compare, keep the better one — don't run both.

---

## Setup 2 — Bounce on a former runner

**Universe filter**
- Listed, price $0.75–$20.00
- Had an intraday move of ≥ 50% on some day in the last 30 trading days **[CHECK]**
- Has since declined ≥ 40% from that runner-day high
- Current session volume > 2× 20-day average

**Setup condition**
- Stock has made a lower low on each of the last 3 sessions (confirming the
  downtrend is still active, so you're not catching a knife mid-fall)

**Entry trigger**
- A 5-minute bar that closes above the high of the prior bar
- AND closes above the 9-period EMA on the 5-minute chart
- AND that bar's volume ≥ 2× the average of the prior six bars
- Enter at the next bar's open

**Stop**
- Low of the trigger bar
- Or -7%, whichever is tighter **[CHECK]**

**Time stop**
- Exit if not up 8% within 90 minutes of entry
- Hard exit at 15:50

**Target**
- +25% from entry (your stated 20–30%), scaled: sell half at +15%, remainder
  trails with a stop at the low of the last completed 5-min bar **[CHECK]**

The scale-out is my addition. You mentioned selling too early and watching it
run — a runner leg on half the position is the mechanical fix for that. Cut it
if you'd rather keep it simple.

**Invalidation**
- Price makes a new low below the setup low

**Verdict:** automate.

---

## Setup 3 — Base pre-buy

*The one I'd expect to fail the backtest. Worth testing precisely for that.*

**Universe filter**
- Listed, price $0.50–$10.00
- Had an intraday move of ≥ 50% on some day in the last 90 trading days
- Has declined ≥ 60% from that high

**Setup condition ("settled at a low, no bounce coming")**
- 10-day price range is less than 12% of current price **[CHECK]** — i.e. it has
  gone flat
- 10-day average volume is less than 25% of the runner-day volume — i.e. interest
  has left
- No new low in the last 5 sessions

**Entry trigger**
- No trigger. This is a scheduled buy: enter at 15:45 on the day the setup
  condition first becomes true

**Stop**
- `entry - 2.0 * ATR(14, daily)`, floored at -20% **[CHECK]**
- ATR-based because a fixed percentage cannot know how noisy a given name is.
  A -5% stop here would be inside one day's normal range on these stocks — you
  would be shaken out by noise long before the thesis had a chance to play out.
  Dollar loss is controlled by position size, not by stop width.

**Time stop**
- **This is the critical parameter.** Exit after 15 trading days if nothing has
  happened **[CHECK]**. Without this, capital sits dead indefinitely

**Target**
- +30%, sell half; trail the remainder with a 10% stop from the high

**Position size**
- Quarter the normal risk. Low conviction, long hold, uses up a position slot

**Verdict:** test before automating. If the backtest shows the winners don't
pay for the dead capital and the losers, cut this setup entirely.

---

## Setup 4 — News catalyst

**Universe filter**
- Listed, price $0.50–$20.00
- Shares outstanding < 50M

**Setup condition**
- A qualifying headline within the last 10 minutes. Categories:
  - FDA approval / clearance / breakthrough designation
  - Government or defense contract award
  - Patent granted or allowed
  - Financing: registered direct, private placement, offering priced

**Important:** financing news is the odd one out. The other three are demand
shocks; a financing is dilution. Treat it as a **separate signal** and let the
backtest tell you whether it belongs. My guess is it behaves differently. **[CHECK]**

**Entry trigger**
- Price is ≥ 5% above the pre-news 5-minute VWAP
- Volume in the 5 minutes since the headline ≥ 5× the average 5-min volume
- Enter immediately, marketable limit at +2%
- **Do not chase**: if price is already more than 30% above the pre-news level,
  skip. The move happened without you

**Stop**
- The pre-news price level, or -10%, whichever is tighter

**Time stop**
- Exit at 15:50 same session **[CHECK]** — you described holding for days;
  I'd test the same-day version first since it's cleaner to measure

**Target**
- +25%, scale half; trail the rest

**Verdict:** automate last. Needs a news feed (Benzinga) that you don't have
yet, so this one is gated on a purchase, not on code.

---

## Setup 5 — Premarket vertical spike

**Universe filter**
- Listed, price $0.50–$20.00
- Premarket session only (04:00–09:30)

**Entry trigger**
- Price up ≥ 30% within a rolling 60-second window **[CHECK]** — you said 50% in
  a second, but a one-second window will mostly catch prints you can't fill on
- Cumulative premarket volume ≥ 200,000 shares (so it's actually tradeable)
- Enter with a limit at +5% over last, cancel if unfilled within 3 seconds

**Stop**
- -12% from fill **[CHECK]**. Wide, because premarket spreads on these are wide

**Time stop**
- Exit by 09:35 if the target hasn't hit — do not carry a premarket spike into
  the regular session

**Target**
- Prior day / week / month high, per your rule. Concretely: the *nearest* of
  those three that sits above entry. If all three are below entry, use +40%

**Halt risk**
- Assume LULD halts will interrupt roughly half of these. The backtest must
  model halt gaps or the results are fiction

**Verdict: keep manual for now.** Not because the code is hard — because your
read on which China small-cap is being run is doing work that these rules don't
capture. Automate the *alerting*, keep the *deciding*.

---

## Setup 6 — Repeat-appearance filter

Not a setup — a universe modifier that applies to Setups 2, 3, and 4.

- Maintain a rolling 10-session count of how many times each ticker has fired
  on your own scanner
- Rank candidates by that count, descending
- When more than one candidate qualifies simultaneously, take the one with the
  higher count

Cheap to compute once the scanner logs everything. Test whether it actually
improves outcomes — it might do nothing.

---

## What the backtest must answer

For each setup, one row:

| Setup | Trades | Hit rate | Avg win | Avg loss | Expectancy | Max drawdown |
|---|---|---|---|---|---|---|

Plus, per setup, the distribution of **maximum adverse excursion** — how far
trades went against you before working. That is what tells you whether my stop
guesses above are too tight, too loose, or about right. Don't hand-tune the
stops; let MAE set them.

---

## What I need from you

Only this, and only where you disagree:

1. The **[CHECK]** numbers — which are wrong, and what should they be
2. Setup 3: is my read right that "settled at a low" means flat range plus dead
   volume, or do you mean something else
3. Setup 5: happy to leave it manual, or do you want it automated anyway
4. Scale-outs: keep them, or exit all at once

Everything else I'll build from this document.
