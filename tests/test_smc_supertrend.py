"""
tests/test_smc_supertrend.py
============================
Unit tests for Supertrend indicator calculation, band flips, noise filtering, and zero-lookahead prefix invariance.
"""

import math
import unittest
import pandas as pd
import numpy as np

from smc.indicators.supertrend import (
    SupertrendIndicator,
    SupertrendState,
    calculate_supertrend,
)


class TestSupertrendIndicator(unittest.TestCase):
    """Test suite for Supertrend indicator."""

    def test_01_warmup_period(self):
        """Bars before atr_length must have NaN ATR and neutral unconfirmed bias."""
        ind = SupertrendIndicator(atr_length=10, multiplier=3.0, min_bars_held=2)
        states = []
        for i in range(9):
            st = ind.update(high=100.0 + i, low=95.0 + i, close=98.0 + i)
            states.append(st)
            self.assertTrue(math.isnan(st.atr))
            self.assertTrue(math.isnan(st.supertrend))
            self.assertEqual(st.trend, 0)
            self.assertEqual(st.bias, "neutral")
            self.assertFalse(st.is_confirmed)

    def test_02_initial_confirmation(self):
        """At bar 10, ATR is initialized, trend is set, but min_bars_held=2 leaves bias neutral until bar 11."""
        ind = SupertrendIndicator(atr_length=10, multiplier=3.0, min_bars_held=2)
        for i in range(9):
            ind.update(high=100.0, low=90.0, close=95.0)

        # Bar 10 (index 9)
        st10 = ind.update(high=105.0, low=95.0, close=104.0)
        self.assertFalse(math.isnan(st10.atr))
        self.assertGreater(st10.atr, 0)
        self.assertEqual(st10.consecutive_bars, 1)
        # Because min_bars_held=2, 1st bar of trend is neutral
        self.assertEqual(st10.bias, "neutral")
        self.assertFalse(st10.is_confirmed)

        # Bar 11 (index 10) - trend holds
        st11 = ind.update(high=110.0, low=100.0, close=108.0)
        self.assertEqual(st11.consecutive_bars, 2)
        self.assertEqual(st11.trend, 1)
        self.assertEqual(st11.bias, "bullish")
        self.assertTrue(st11.is_confirmed)
        self.assertLess(st11.supertrend, st11.close)

    def test_03_bearish_flip(self):
        """A strong drop below lower band flips trend to bearish (-1)."""
        ind = SupertrendIndicator(atr_length=5, multiplier=2.0, min_bars_held=1)
        # Establish bullish trend
        for i in range(6):
            ind.update(high=100.0 + i * 2, low=95.0 + i * 2, close=99.0 + i * 2)

        self.assertEqual(ind.last_state.trend, 1)
        lower_band = ind.last_state.final_lower

        # Drop significantly below lower band
        st_flip = ind.update(high=lower_band - 5, low=lower_band - 15, close=lower_band - 10)
        self.assertEqual(st_flip.trend, -1)
        self.assertEqual(st_flip.bias, "bearish")
        self.assertTrue(st_flip.is_confirmed)
        self.assertGreater(st_flip.supertrend, st_flip.close)

    def test_04_noise_filter_rapid_flip(self):
        """Rapid flips with min_bars_held=2 remain neutral on 1st bar."""
        ind = SupertrendIndicator(atr_length=5, multiplier=1.5, min_bars_held=2)
        for i in range(10):
            ind.update(high=100.0, low=90.0, close=95.0)

        # Force flip to bearish
        st_bear = ind.update(high=70.0, low=50.0, close=55.0)
        self.assertEqual(st_bear.trend, -1)
        self.assertEqual(st_bear.consecutive_bars, 1)
        # 1st bar of flip is filtered out (neutral)
        self.assertEqual(st_bear.bias, "neutral")

        # 2nd bar confirms
        st_bear2 = ind.update(high=65.0, low=50.0, close=55.0)
        self.assertEqual(st_bear2.consecutive_bars, 2)
        self.assertEqual(st_bear2.bias, "bearish")

    def test_05_prefix_invariance(self):
        """Feeding first N bars must yield the exact same values as first N bars of N+K run (zero lookahead)."""
        np.random.seed(42)
        n = 50
        k = 20
        highs = 100.0 + np.cumsum(np.random.randn(n + k)) + np.random.uniform(1.0, 3.0, n + k)
        lows = highs - np.random.uniform(1.0, 3.0, n + k)
        closes = (highs + lows) / 2.0 + np.random.uniform(-0.5, 0.5, n + k)

        # Run N bars
        ind_n = SupertrendIndicator(atr_length=10, multiplier=3.0, min_bars_held=2)
        states_n = [ind_n.update(highs[i], lows[i], closes[i]) for i in range(n)]

        # Run N+K bars
        ind_all = SupertrendIndicator(atr_length=10, multiplier=3.0, min_bars_held=2)
        states_all = [ind_all.update(highs[i], lows[i], closes[i]) for i in range(n + k)]

        for i in range(n):
            sn = states_n[i]
            sa = states_all[i]
            self.assertEqual(sn.trend, sa.trend)
            self.assertEqual(sn.bias, sa.bias)
            self.assertEqual(sn.consecutive_bars, sa.consecutive_bars)
            self.assertEqual(sn.is_confirmed, sa.is_confirmed)
            if math.isnan(sn.supertrend):
                self.assertTrue(math.isnan(sa.supertrend))
            else:
                self.assertAlmostEqual(sn.supertrend, sa.supertrend, places=7)

    def test_06_calculate_supertrend_df(self):
        """calculate_supertrend helper dataframe matches iterative updates."""
        df = pd.DataFrame({
            "high": [100.0, 102.0, 105.0, 104.0, 108.0, 110.0, 109.0, 112.0, 115.0, 114.0, 116.0, 118.0],
            "low": [95.0, 97.0, 100.0, 99.0, 103.0, 105.0, 104.0, 107.0, 110.0, 109.0, 111.0, 113.0],
            "close": [98.0, 100.0, 103.0, 102.0, 106.0, 108.0, 107.0, 110.0, 113.0, 112.0, 114.0, 117.0],
        })
        res_df = calculate_supertrend(df, atr_length=5, multiplier=2.0, min_bars_held=2)
        self.assertIn("supertrend", res_df.columns)
        self.assertIn("supertrend_bias", res_df.columns)
        self.assertEqual(len(res_df), len(df))


if __name__ == "__main__":
    unittest.main()
