"""
tests/test_smc_fvg.py
=====================
Unit tests for the FVG detection module (smc/zones/fvg.py).
"""

import unittest
import pandas as pd
import numpy as np

from smc.data_contract import normalize_ohlcv
from smc.zones.fvg import detect_fvgs, FVGTracker


def _make_df(candles, repair=True):
    """Helper: build a normalized DataFrame from a list of OHLCV dicts."""
    return normalize_ohlcv(candles, repair_invalid_ohlc=repair)


def _candle(t, o, h, l, c, repair=True):
    """Return a single-row dict. high/low will be repaired if invalid."""
    h = max(h, o, c)
    l = min(l, o, c)
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


class TestSMCFVG(unittest.TestCase):
    # ------------------------------------------------------------------
    # 01 Bullish FVG detected
    # ------------------------------------------------------------------
    def test_01_bullish_fvg_detected(self):
        """bar[i-1].high < bar[i+1].low => Bullish FVG confirmed at bar[i+1]."""
        candles = [
            _candle("2024-01-01", 100, 105, 98, 102),  # bar 0
            _candle("2024-01-02", 103, 110, 102, 108), # bar 1 (middle)
            _candle("2024-01-03", 111, 115, 107, 113), # bar 2 (right)
            # bar0.high=105, bar2.low=107 => 105 < 107 => Bullish FVG
        ]
        df = _make_df(candles)
        fvgs = detect_fvgs(df, mode="swing")
        self.assertEqual(len(fvgs), 1)
        fvg = fvgs[0]
        self.assertEqual(fvg.direction, "bullish")
        self.assertAlmostEqual(fvg.bottom, 105.0)  # bar0.high
        self.assertAlmostEqual(fvg.top, 107.0)     # bar2.low
        self.assertEqual(fvg.index, 1)              # middle bar
        self.assertEqual(fvg.confirmed_at, 2)       # right bar

    # ------------------------------------------------------------------
    # 02 Bearish FVG detected
    # ------------------------------------------------------------------
    def test_02_bearish_fvg_detected(self):
        """bar[i-1].low > bar[i+1].high => Bearish FVG confirmed at bar[i+1]."""
        candles = [
            _candle("2024-01-01", 120, 125, 115, 118), # bar 0
            _candle("2024-01-02", 116, 118, 110, 112), # bar 1 (middle)
            _candle("2024-01-03", 108, 112, 100, 105), # bar 2 (right)
            # bar0.low=115, bar2.high=112 => 115 > 112 => Bearish FVG
        ]
        df = _make_df(candles)
        fvgs = detect_fvgs(df, mode="swing")
        self.assertEqual(len(fvgs), 1)
        fvg = fvgs[0]
        self.assertEqual(fvg.direction, "bearish")
        self.assertAlmostEqual(fvg.top, 115.0)     # bar0.low
        self.assertAlmostEqual(fvg.bottom, 112.0)  # bar2.high
        self.assertEqual(fvg.index, 1)
        self.assertEqual(fvg.confirmed_at, 2)

    # ------------------------------------------------------------------
    # 03 No gap => no FVG
    # ------------------------------------------------------------------
    def test_03_no_gap_no_fvg(self):
        """Touching candles (bar[i-1].high == bar[i+1].low) should NOT create FVG."""
        candles = [
            _candle("2024-01-01", 100, 105, 98, 104),  # bar 0, high=105
            _candle("2024-01-02", 105, 110, 104, 108), # bar 1
            _candle("2024-01-03", 107, 112, 105, 110), # bar 2, low=105
            # bar0.high=105 == bar2.low=105 => NO gap => no FVG
        ]
        df = _make_df(candles)
        fvgs = detect_fvgs(df, mode="swing")
        self.assertEqual(len(fvgs), 0)

    # ------------------------------------------------------------------
    # 04 min_gap_pct filter
    # ------------------------------------------------------------------
    def test_04_min_gap_pct_filter(self):
        """Tiny gap filtered out when gap/bottom < min_gap_pct."""
        # Create a gap of 0.01 at bottom 100 => gap_pct = 0.0001 (0.01%)
        candles = [
            _candle("2024-01-01", 100, 100.01, 99, 100.01), # bar0.high=100.01
            _candle("2024-01-02", 100.02, 100.05, 100.0, 100.03),
            _candle("2024-01-03", 100.04, 100.10, 100.02, 100.08), # bar2.low=100.02
        ]
        df = _make_df(candles)
        # With no filter: should have an FVG
        fvgs_no_filter = detect_fvgs(df, mode="swing", min_gap_pct=0.0)
        self.assertEqual(len(fvgs_no_filter), 1)
        # With large filter: filtered out
        fvgs_filtered = detect_fvgs(df, mode="swing", min_gap_pct=0.01)
        self.assertEqual(len(fvgs_filtered), 0)

    # ------------------------------------------------------------------
    # 05 confirmed_at is bar i+1 index
    # ------------------------------------------------------------------
    def test_05_confirmed_at_lag(self):
        """confirmed_at must equal bar_index of the right bar (i+1), not i."""
        candles = [
            _candle("2024-01-01", 100, 105, 98, 102),   # bar 0
            _candle("2024-01-02", 103, 112, 102, 110),  # bar 1 (middle)
            _candle("2024-01-03", 113, 120, 108, 118),  # bar 2 (right)
        ]
        df = _make_df(candles)
        fvgs = detect_fvgs(df, mode="swing")
        self.assertTrue(len(fvgs) >= 1)
        # confirmed_at must equal bar_index of bar 2 which is 2
        for fvg in fvgs:
            self.assertEqual(fvg.confirmed_at, 2, "confirmed_at should be bar_index of right bar")

        # Verify current_bar_index cutoff works
        fvgs_early = detect_fvgs(df, mode="swing", current_bar_index=1)
        self.assertEqual(len(fvgs_early), 0, "No FVG should be confirmed at bar 1 if right bar is bar 2")

    # ------------------------------------------------------------------
    # 06 FVGTracker parity with batch detect_fvgs
    # ------------------------------------------------------------------
    def test_06_fvg_tracker_parity(self):
        """Incremental FVGTracker results must match batch detect_fvgs."""
        np.random.seed(42)
        n = 30
        times = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
        candles = []
        for i in range(n):
            o = float(prices[i])
            c = float(prices[i] + np.random.randn() * 0.3)
            h = max(o, c) + abs(np.random.randn()) * 0.2
            l = min(o, c) - abs(np.random.randn()) * 0.2
            candles.append({
                "time": times[i], "open": o, "high": h, "low": l, "close": c
            })

        df = normalize_ohlcv(candles, repair_invalid_ohlc=True)
        batch_fvgs = detect_fvgs(df, mode="swing")

        tracker = FVGTracker(mode="swing")
        for _, row in df.iterrows():
            tracker.update(row)

        tracker_fvgs = tracker.get_all_fvgs()

        # Count should match
        self.assertEqual(len(batch_fvgs), len(tracker_fvgs),
            f"Batch detected {len(batch_fvgs)} FVGs but tracker found {len(tracker_fvgs)}")

        # Sort both for comparison
        batch_sorted   = sorted(batch_fvgs,   key=lambda f: (f.confirmed_at, f.index))
        tracker_sorted = sorted(tracker_fvgs, key=lambda f: (f.confirmed_at, f.index))

        for b, t in zip(batch_sorted, tracker_sorted):
            self.assertEqual(b.direction, t.direction)
            self.assertAlmostEqual(b.top, t.top, places=8)
            self.assertAlmostEqual(b.bottom, t.bottom, places=8)
            self.assertEqual(b.confirmed_at, t.confirmed_at)

    # ------------------------------------------------------------------
    # 07 FVG fill tracking
    # ------------------------------------------------------------------
    def test_07_fvg_filled_tracking(self):
        """After FVG is confirmed, if price returns to the gap it should be filled."""
        candles = [
            _candle("2024-01-01", 100, 105, 98, 102),   # bar 0, high=105
            _candle("2024-01-02", 103, 115, 102, 112),  # bar 1 (middle)
            _candle("2024-01-03", 113, 120, 110, 118),  # bar 2 (right), low=110 => gap=[105,110] bullish
            _candle("2024-01-04", 118, 121, 116, 119),  # bar 3, low=116 > 105 => not filled
            _candle("2024-01-05", 118, 119, 104, 106),  # bar 4, low=104 <= 105 (bottom) => filled!
        ]
        df = _make_df(candles)
        fvgs = detect_fvgs(df, mode="swing")
        bullish_fvgs = [f for f in fvgs if f.direction == "bullish"]
        self.assertTrue(len(bullish_fvgs) >= 1, "Expected at least one bullish FVG")
        fvg = bullish_fvgs[0]
        self.assertTrue(fvg.filled, "FVG should be marked as filled")
        self.assertEqual(fvg.filled_at, 4, f"Expected filled_at=4, got {fvg.filled_at}")

        # Test via tracker
        tracker = FVGTracker(mode="swing")
        for _, row in df.iterrows():
            tracker.update(row)
        tracker_fvgs = [f for f in tracker.get_all_fvgs() if f.direction == "bullish"]
        self.assertTrue(len(tracker_fvgs) >= 1)
        self.assertTrue(tracker_fvgs[0].filled, "Tracker FVG should also be filled")
        self.assertEqual(tracker_fvgs[0].filled_at, 4)


if __name__ == "__main__":
    unittest.main()
