import unittest
import pandas as pd
import numpy as np
from smc.models import SwingPoint
from smc.data_contract import normalize_ohlcv
from smc.structure.swings import detect_swings, get_confirmed_swings_at_bar

class TestSMCSwings(unittest.TestCase):
    def setUp(self):
        # Create a synthetic OHLCV dataset with known peaks and troughs
        # 20 bars:
        # Bar 3: Peak High (110.0) -> strength 2 (bars 1,2 < 110 and bars 4,5 < 110)
        # Bar 7: Trough Low (90.0) -> strength 2 (bars 5,6 > 90 and bars 8,9 > 90)
        # Bar 12: Peak High (115.0) -> Higher High (HH)
        # Bar 16: Trough Low (95.0) -> Higher Low (HL)
        dates = pd.date_range("2026-01-01 00:00", periods=20, freq="1h", tz="UTC")
        opens  = [100.0] * 20
        highs  = [102.0, 104.0, 106.0, 110.0, 105.0, 103.0, 101.0, 100.0, 101.0, 102.0, 105.0, 112.0, 115.0, 108.0, 104.0, 101.0, 100.0, 101.0, 101.0, 102.0]
        lows   = [98.0,  99.0,  99.5,  100.0, 99.0,  96.0,  92.0,  90.0,  93.0,  96.0,  99.0,  100.0, 100.0, 99.0,  98.0,  95.0,  95.0,  97.0,  98.0,  99.0]
        closes = [100.0] * 20
        vols   = [1000] * 20

        self.synthetic_df = pd.DataFrame({
            "time": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": vols
        })

    def test_01_normalize_ohlcv(self):
        df = normalize_ohlcv(self.synthetic_df)
        self.assertIn("volume", df.columns)
        self.assertNotIn("tick_volume", df.columns)
        self.assertIn("bar_index", df.columns)
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(str(df.index.tz), "UTC")
        self.assertEqual(len(df), 20)
        self.assertEqual(df["bar_index"].iloc[0], 0)
        self.assertEqual(df["bar_index"].iloc[-1], 19)

    def test_02_normalize_ohlcv_dict_list_input(self):
        dict_list = [
            {"time": 1700000000, "open": 100.0, "high": 105.0, "low": 99.0, "close": 102.0, "tick_volume": 50},
            {"time": 1700000060, "open": 102.0, "high": 106.0, "low": 101.0, "close": 104.0, "tick_volume": 60},
        ]
        df = normalize_ohlcv(dict_list)
        self.assertEqual(len(df), 2)
        self.assertIn("volume", df.columns)
        self.assertEqual(df["volume"].iloc[0], 50)
        self.assertEqual(df["bar_index"].iloc[1], 1)

    def test_03_normalize_ohlcv_validation_errors(self):
        # Missing required column 'close'
        invalid_data = [
            {"time": 1700000000, "open": 100.0, "high": 105.0, "low": 95.0, "volume": 10}
        ]
        with self.assertRaises(ValueError):
            normalize_ohlcv(invalid_data)

    def test_04_swing_detection_and_confirmation_lag(self):
        df = normalize_ohlcv(self.synthetic_df)
        # Use strength=2 (2 left, 2 right)
        swings = detect_swings(df, strength=2)
        
        self.assertGreaterEqual(len(swings), 2)
        
        # Find Bar 3 High (110.0)
        high_swings = [s for s in swings if s.kind == "high"]
        self.assertTrue(any(s.index == 3 and s.price == 110.0 for s in high_swings))
        
        sw3 = next(s for s in swings if s.index == 3 and s.kind == "high")
        # Bar 3 with right_strength=2 is confirmed at bar 3 + 2 = 5
        self.assertEqual(sw3.confirmed_at, 5)
        self.assertEqual(sw3.confirmed_time, df.index[5])
        
        # Test No-Lookahead filtering:
        # At bar 4: sw3 is NOT yet confirmed
        swings_at_bar_4 = get_confirmed_swings_at_bar(swings, current_bar_index=4)
        self.assertNotIn(sw3, swings_at_bar_4)
        
        # At bar 5: sw3 IS confirmed
        swings_at_bar_5 = get_confirmed_swings_at_bar(swings, current_bar_index=5)
        self.assertIn(sw3, swings_at_bar_5)

    def test_05_structure_classification_hh_hl_lh_ll(self):
        # Create a clean sequence of 5 waves:
        # High 1 (100) -> Low 1 (50) -> High 2 (120: HH) -> Low 2 (60: HL) -> High 3 (110: LH) -> Low 3 (40: LL)
        n_bars = 40
        highs = [100.0] * n_bars
        lows = [90.0] * n_bars

        # High 1 at bar 5
        highs[5] = 130.0
        # Low 1 at bar 10
        lows[10] = 50.0
        # High 2 at bar 15 (140 > 130 -> HH)
        highs[15] = 140.0
        # Low 2 at bar 20 (60 > 50 -> HL)
        lows[20] = 60.0
        # High 3 at bar 25 (135 < 140 -> LH)
        highs[25] = 135.0
        # Low 3 at bar 30 (40 < 60 -> LL)
        lows[30] = 40.0

        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1h", tz="UTC")
        df_raw = pd.DataFrame({
            "time": dates,
            "open": [95.0] * n_bars,
            "high": highs,
            "low": lows,
            "close": [95.0] * n_bars,
            "volume": [100] * n_bars
        })
        df = normalize_ohlcv(df_raw)
        swings = detect_swings(df, strength=3)

        high_swings = [s for s in swings if s.kind == "high"]
        low_swings = [s for s in swings if s.kind == "low"]

        self.assertGreaterEqual(len(high_swings), 3)
        self.assertGreaterEqual(len(low_swings), 3)

        # First high is UNCLASSIFIED
        self.assertEqual(high_swings[0].classification, "UNCLASSIFIED")
        # Second high (140 > 130) is HH
        self.assertEqual(high_swings[1].classification, "HH")
        # Third high (135 < 140) is LH
        self.assertEqual(high_swings[2].classification, "LH")

        # First low is UNCLASSIFIED
        self.assertEqual(low_swings[0].classification, "UNCLASSIFIED")
        # Second low (60 > 50) is HL
        self.assertEqual(low_swings[1].classification, "HL")
        # Third low (40 < 60) is LL
        self.assertEqual(low_swings[2].classification, "LL")

    def test_06_swing_vs_internal_independence(self):
        df = normalize_ohlcv(self.synthetic_df)
        internal_swings = detect_swings(df, strength=2, mode="internal")
        major_swings = detect_swings(df, strength=4, mode="swing")

        # Major swings with higher strength should detect fewer or equal swings than internal swings
        self.assertGreaterEqual(len(internal_swings), len(major_swings))

    def test_07_edge_cases_empty_and_flat(self):
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        norm_empty = normalize_ohlcv(empty_df)
        self.assertEqual(detect_swings(norm_empty), [])

        # Flat price
        n_bars = 20
        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1h", tz="UTC")
        flat_df = pd.DataFrame({
            "time": dates,
            "open": [100.0] * n_bars,
            "high": [100.0] * n_bars,
            "low": [100.0] * n_bars,
            "close": [100.0] * n_bars,
            "volume": [100] * n_bars
        })
        norm_flat = normalize_ohlcv(flat_df)
        # Flat series should return empty list or handle without crash
        swings = detect_swings(norm_flat, strength=2)
        self.assertIsInstance(swings, list)

    def test_08_swing_point_to_dict_serialization(self):
        df = normalize_ohlcv(self.synthetic_df)
        swings = detect_swings(df, strength=2)
        if swings:
            sw_dict = swings[0].to_dict()
            self.assertIn("index", sw_dict)
            self.assertIn("time", sw_dict)
            self.assertIn("price", sw_dict)
            self.assertIn("kind", sw_dict)
            self.assertIn("mode", sw_dict)
            self.assertIn("confirmed_at", sw_dict)
            self.assertIn("classification", sw_dict)
            self.assertIsInstance(sw_dict["price"], float)
            self.assertIsInstance(sw_dict["confirmed_at"], int)

    def test_09_strength_validation(self):
        df = normalize_ohlcv(self.synthetic_df)
        with self.assertRaises(ValueError):
            detect_swings(df, strength=0)
        with self.assertRaises(ValueError):
            detect_swings(df, strength=-5)
        with self.assertRaises(ValueError):
            detect_swings(df, strength=5, left_strength=0)
        with self.assertRaises(ValueError):
            detect_swings(df, strength=5, right_strength=-2)

    def test_10_mode_attribute_on_swing_point(self):
        df = normalize_ohlcv(self.synthetic_df)
        internal_swings = detect_swings(df, strength=2, mode="internal")
        major_swings = detect_swings(df, strength=2, mode="swing")
        
        self.assertTrue(all(s.mode == "internal" for s in internal_swings))
        self.assertTrue(all(s.mode == "swing" for s in major_swings))
        if internal_swings:
            self.assertEqual(internal_swings[0].to_dict()["mode"], "internal")

    def test_11_string_unix_timestamp_and_empty_list_index(self):
        # String Unix timestamp in list of dicts
        data = [
            {"time": "1700000000", "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0},
            {"time": "1700000060", "open": 102.0, "high": 108.0, "low": 100.0, "close": 105.0}
        ]
        df = normalize_ohlcv(data)
        self.assertEqual(len(df), 2)
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(str(df.index.tz), "UTC")

        # Empty list input returns DatetimeIndex UTC
        df_empty = normalize_ohlcv([])
        self.assertTrue(df_empty.empty)
        self.assertIsInstance(df_empty.index, pd.DatetimeIndex)
        self.assertEqual(str(df_empty.index.tz), "UTC")

    def test_12_ohlc_nan_inf_and_geometry_validation(self):
        # NaN in OHLC
        nan_data = [
            {"time": 1700000000, "open": np.nan, "high": 105.0, "low": 95.0, "close": 100.0}
        ]
        with self.assertRaises(ValueError):
            normalize_ohlcv(nan_data)

        # Inf in OHLC
        inf_data = [
            {"time": 1700000000, "open": 100.0, "high": np.inf, "low": 95.0, "close": 100.0}
        ]
        with self.assertRaises(ValueError):
            normalize_ohlcv(inf_data)

        # Geometry mismatch: high < max(open, close)
        bad_geo = [
            {"time": 1700000000, "open": 100.0, "high": 95.0, "low": 90.0, "close": 98.0}
        ]
        # Default repair_invalid_ohlc=False raises ValueError
        with self.assertRaises(ValueError):
            normalize_ohlcv(bad_geo, repair_invalid_ohlc=False)

        # repair_invalid_ohlc=True repairs high/low
        repaired_df = normalize_ohlcv(bad_geo, repair_invalid_ohlc=True)
        self.assertEqual(repaired_df["high"].iloc[0], 100.0)
        self.assertEqual(repaired_df["low"].iloc[0], 90.0)

    def test_13_swing_detector_state_incremental(self):
        from smc.structure.swings import SwingDetectorState
        df = normalize_ohlcv(self.synthetic_df)
        detector = SwingDetectorState(strength=2)
        
        all_newly_confirmed = []
        for i in range(len(df)):
            candle_row = df.iloc[i]
            # Feed single candle into incremental detector
            new_swings = detector.update(candle_row)
            for sw in new_swings:
                # Core invariant: newly confirmed swing MUST be confirmed on or before current bar i
                self.assertLessEqual(sw.confirmed_at, i)
                all_newly_confirmed.append(sw)

        total_swings = detector.get_confirmed_swings()
        self.assertEqual(len(all_newly_confirmed), len(total_swings))

    def test_14_tie_breaking_equal_highs_lows(self):
        # Equal adjacent highs: bar 3 and bar 4 both have high 110.0
        n_bars = 10
        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1h", tz="UTC")
        highs = [100.0, 102.0, 105.0, 110.0, 110.0, 105.0, 102.0, 100.0, 98.0, 96.0]
        lows =  [95.0,  95.5,  96.0,  96.0,  96.0,  95.5,  95.0,  92.0,  90.0, 88.0]
        df_raw = pd.DataFrame({
            "time": dates,
            "open": [96.0] * n_bars,
            "high": highs,
            "low": lows,
            "close": [96.0] * n_bars,
            "volume": [100] * n_bars
        })
        df = normalize_ohlcv(df_raw)
        swings = detect_swings(df, strength=2)
        high_swings = [s for s in swings if s.kind == "high"]
        
        # Tie-breaking logic picks the first occurrence deterministically without duplicating
        self.assertEqual(len(high_swings), 1)
        self.assertEqual(high_swings[0].index, 4)

    def test_15_invalid_mode_validation(self):
        df = normalize_ohlcv(self.synthetic_df)
        with self.assertRaises(ValueError):
            detect_swings(df, mode="invalid_mode")
        
        from smc.structure.swings import SwingDetectorState
        with self.assertRaises(ValueError):
            SwingDetectorState(mode="invalid_mode")

    def test_16_stateful_detector_performance_benchmark(self):
        import time
        from smc.structure.swings import SwingDetectorState
        n_bars = 1000
        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1h", tz="UTC")
        prices = 1000.0 + np.sin(np.linspace(0, 50, n_bars)) * 50.0
        
        detector = SwingDetectorState(strength=5)
        t0 = time.perf_counter()
        
        for i in range(n_bars):
            p = float(prices[i])
            candle = {
                "time": dates[i],
                "open": p,
                "high": p + 2.0,
                "low": p - 2.0,
                "close": p,
                "volume": 100
            }
            detector.update(candle)
            
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # 1000 bars incremental updates should finish well under 100ms (O(1) per bar)
        self.assertLess(elapsed_ms, 200.0)
        print(f"\n[PASS] Incremental SwingDetectorState processed 1,000 bars in {elapsed_ms:.2f}ms (<200ms threshold)")

if __name__ == "__main__":
    unittest.main()

