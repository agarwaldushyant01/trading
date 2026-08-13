"""Shares outstanding from SEC EDGAR — free, no API key.

    python -m data.shares_outstanding --email you@example.com

Writes data/shares_outstanding.json, which data/reference.py picks up.

Approach: the xbrl/frames endpoint returns one fact for EVERY filer in a
single request, so this is a handful of calls rather than one per symbol.
Fetching companyfacts per company would mean thousands of 1-10MB downloads.

A useful side effect: only operating companies report
dei:EntityCommonStockSharesOutstanding. ETFs, closed-end funds, warrants and
units do not, so anything absent from this map is dropped by the reference
loader. That is what trims Alpaca's 13,000 "US equities" down to the few
thousand names your setups actually apply to.

Caveats worth knowing:
  - Shares outstanding is NOT float. It includes insider and restricted
    shares, so it overstates tradeable float, often badly on small caps.
    Good enough as a filter, wrong as a precise number.
  - Values update quarterly, from 10-Q and 10-K cover pages. A company that
    did a large offering last month will look smaller than it is — which
    matters for your financing-news setup specifically.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from datetime import date

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FRAMES_URL = (
    "https://data.sec.gov/api/xbrl/frames/dei/"
    "EntityCommonStockSharesOutstanding/shares/{period}.json"
)
OUT_PATH = pathlib.Path(__file__).parent / "shares_outstanding.json"

# SEC fair-access policy: identify yourself, stay under 10 requests/second.
RATE_LIMIT_SLEEP = 0.15


def _get(url: str, email: str) -> dict:
    """requests rather than urllib: it ships its own certificate bundle, so
    this works without depending on the machine's SSL configuration.
    """
    response = requests.get(
        url, headers={"User-Agent": f"trading-bot {email}"}, timeout=60
    )
    response.raise_for_status()
    return response.json()


# --------------------------------------------------------------- pure core

def build_cik_to_ticker(tickers_payload: dict) -> dict[int, str]:
    """company_tickers.json is keyed by row number, not ticker.

    Shape: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
    """
    return {
        int(row["cik_str"]): row["ticker"]
        for row in tickers_payload.values()
        if row.get("ticker")
    }


def extract_shares(frames_payload: dict, cik_to_ticker: dict[int, str]) -> dict[str, float]:
    """Pull {ticker: shares} out of one frames response.

    Frames shape: {"data": [{"cik": 320193, "val": 15000000000, ...}, ...]}
    CIKs with no ticker (private filers, funds) are skipped.
    """
    out = {}
    for row in frames_payload.get("data", []):
        ticker = cik_to_ticker.get(row.get("cik"))
        value = row.get("val")
        if ticker and value and value > 0:
            out[ticker] = float(value)
    return out


def recent_periods(as_of: date, count: int = 5) -> list[str]:
    """Quarter labels for the frames API, newest first.

    The 'I' suffix means instantaneous — a point-in-time value like a share
    count, as opposed to a value measured over a period like revenue.
    """
    year, quarter = as_of.year, (as_of.month - 1) // 3 + 1
    periods = []
    for _ in range(count):
        periods.append(f"CY{year}Q{quarter}I")
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
    return periods


def merge_periods(payloads: list[dict[str, float]]) -> dict[str, float]:
    """Newest first: earlier entries win, older quarters only fill gaps.

    Companies file on different schedules, so no single quarter covers
    everyone. Walking back a few quarters catches the stragglers without
    letting stale values overwrite fresh ones.
    """
    merged: dict[str, float] = {}
    for payload in payloads:
        for ticker, shares in payload.items():
            merged.setdefault(ticker, shares)
    return merged


# -------------------------------------------------------------------- I/O

def build(email: str, as_of: date, quarters: int = 5) -> dict[str, float]:
    print("Fetching ticker -> CIK map...", file=sys.stderr)
    cik_to_ticker = build_cik_to_ticker(_get(TICKERS_URL, email))
    print(f"  {len(cik_to_ticker)} tickers with an SEC filing history",
          file=sys.stderr)

    payloads = []
    for period in recent_periods(as_of, quarters):
        try:
            frames = _get(FRAMES_URL.format(period=period), email)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {period}: unavailable ({exc})", file=sys.stderr)
            continue
        extracted = extract_shares(frames, cik_to_ticker)
        print(f"  {period}: {len(extracted)} companies", file=sys.stderr)
        payloads.append(extracted)
        time.sleep(RATE_LIMIT_SLEEP)

    if not payloads:
        raise SystemExit("No data returned. Check the email arg and network.")

    return merge_periods(payloads)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--email", required=True,
                   help="SEC requires a contact address in the User-Agent")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--quarters", type=int, default=5)
    args = p.parse_args()

    shares = build(args.email, date.fromisoformat(args.date), args.quarters)
    OUT_PATH.write_text(json.dumps(shares, indent=0, sort_keys=True))

    low_float = sum(1 for v in shares.values() if v < 30_000_000)
    print(f"\nWrote {len(shares)} symbols to {OUT_PATH}", file=sys.stderr)
    print(f"  {low_float} under 30M shares outstanding", file=sys.stderr)


if __name__ == "__main__":
    main()
