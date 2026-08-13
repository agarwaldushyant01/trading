"""Tests for data/shares_outstanding.py — parsing and merge logic only."""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import date

from data.shares_outstanding import (
    build_cik_to_ticker,
    extract_shares,
    merge_periods,
    recent_periods,
)


# ------------------------------------------------------------- ticker map

def test_cik_map_is_keyed_by_cik_not_row_number():
    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
    }
    assert build_cik_to_ticker(payload) == {320193: "AAPL", 789019: "MSFT"}


def test_rows_without_a_ticker_are_skipped():
    payload = {"0": {"cik_str": 1, "ticker": "", "title": "Private Filer"}}
    assert build_cik_to_ticker(payload) == {}


# ---------------------------------------------------------------- frames

def test_extract_maps_cik_to_ticker():
    frames = {"data": [{"cik": 320193, "val": 15_000_000_000}]}
    assert extract_shares(frames, {320193: "AAPL"}) == {"AAPL": 15e9}


def test_unknown_cik_is_dropped():
    """A filer with no ticker — a fund or private company — is not tradeable."""
    frames = {"data": [{"cik": 999999, "val": 5_000_000}]}
    assert extract_shares(frames, {320193: "AAPL"}) == {}


def test_zero_and_missing_values_are_dropped():
    frames = {"data": [
        {"cik": 1, "val": 0},
        {"cik": 2},
        {"cik": 3, "val": 8_000_000},
    ]}
    got = extract_shares(frames, {1: "AAAA", 2: "BBBB", 3: "CCCC"})
    assert got == {"CCCC": 8e6}


def test_empty_frames_payload():
    assert extract_shares({}, {320193: "AAPL"}) == {}


# ---------------------------------------------------------------- periods

def test_periods_walk_backwards_from_the_current_quarter():
    assert recent_periods(date(2026, 3, 10), 3) == ["CY2026Q1I", "CY2025Q4I",
                                                    "CY2025Q3I"]


def test_periods_cross_the_year_boundary():
    assert recent_periods(date(2026, 2, 1), 2) == ["CY2026Q1I", "CY2025Q4I"]


def test_q4_is_derived_from_december():
    assert recent_periods(date(2026, 12, 31), 1) == ["CY2026Q4I"]


# ------------------------------------------------------------------ merge

def test_newer_quarters_win():
    newest = {"AAAA": 9_000_000}
    older = {"AAAA": 5_000_000}
    assert merge_periods([newest, older]) == {"AAAA": 9_000_000}


def test_older_quarters_fill_gaps():
    """A company that skipped last quarter still gets a value."""
    newest = {"AAAA": 9_000_000}
    older = {"AAAA": 5_000_000, "BBBB": 3_000_000}
    assert merge_periods([newest, older]) == {"AAAA": 9_000_000,
                                              "BBBB": 3_000_000}
