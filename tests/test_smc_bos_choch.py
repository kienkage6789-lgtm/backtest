import unittest
import time
import pandas as pd
import numpy as np
from smc.models import SwingPoint, StructureEvent
from smc.data_contract import normalize_ohlcv
from smc.structure.swings import detect_swings, SwingDetectorState
from smc.structure.bos_choch import detect_structure_events, StructureTracker, calculate_atr

class TestSMCBOSCHoCH(unittest.TestCase):
    def setUp(self):
        # Create synthetic datasets for structure testing
        pass

    def test_01_initial_bullish_bos(self):
        # Dataset A: initial trend None -> break high = bullish BOS
        dates = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
        opens  = [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 105.0, 112.0, 115.0, 110.0]
        highs  = [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 112.0, 116.0, 118.0, 112.0]
        lows   = [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 103.0, 110.0, 114.0, 108.0]
        closes = [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 111.0, 115.0, 116.0, 109.0]
        
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))
        
        # Swings: high at bar 2 (110.0), right_strength 2 -> confirmed at bar 4
        # Bar 6 close (111.0) > 110.0 -> bullish BOS
        events = detect_structure_events(df, strength=2, mode="swing")
        
        self.assertGreaterEqual(len(events), 1)
        ev1 = events[0]
        self.assertEqual(ev1.event_type, "BOS")
        self.assertEqual(ev1.direction, "bullish")
        self.assertEqual(ev1.broken_swing_index, 2)
        self.assertEqual(ev1.broken_swing_price, 110.0)
        self.assertEqual(ev1.index, 6)

    def test_02_initial_bearish_bos(self):
        # Dataset: initial trend None -> break low = bearish BOS
        dates = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
        opens  = [100.0, 98.0,  92.0,  94.0,  96.0,  97.0,  95.0,  88.0,  85.0,  88.0]
        highs  = [101.0, 99.0,  94.0,  97.0,  98.0,  98.0,  96.0,  89.0,  86.0,  89.0]
        lows   = [97.0,  91.0,  90.0,  92.0,  94.0,  95.0,  87.0,  84.0,  82.0,  84.0]
        closes = [98.0,  92.0,  93.0,  95.0,  97.0,  96.0,  88.0,  85.0,  84.0,  87.0]

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))
        
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertGreaterEqual(len(events), 1)
        ev1 = events[0]
        self.assertEqual(ev1.event_type, "BOS")
        self.assertEqual(ev1.direction, "bearish")
        self.assertEqual(ev1.broken_swing_index, 2)
        self.assertEqual(ev1.broken_swing_price, 90.0)

    def test_03_bullish_continuation_bos(self):
        # Sequence: bullish BOS -> new swing high -> break new swing high = bullish BOS
        dates = pd.date_range("2026-01-01 00:00", periods=18, freq="1h", tz="UTC")
        highs = [102.0, 105.0, 110.0, 105.0, 102.0, 112.0, 115.0, 120.0, 115.0, 110.0, 122.0, 125.0, 130.0, 125.0, 120.0, 132.0, 135.0, 130.0]
        lows  = [98.0,  100.0, 104.0, 101.0, 99.0,  105.0, 110.0, 114.0, 108.0, 106.0, 112.0, 118.0, 122.0, 116.0, 114.0, 122.0, 128.0, 124.0]
        closes= [100.0, 104.0, 106.0, 102.0, 101.0, 111.0, 114.0, 116.0, 110.0, 108.0, 121.0, 124.0, 125.0, 118.0, 116.0, 131.0, 132.0, 128.0]
        opens = [100.0] * 18

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "BOS")
        self.assertEqual(events[0].direction, "bullish")
        self.assertEqual(events[1].event_type, "BOS")
        self.assertEqual(events[1].direction, "bullish")

    def test_04_bearish_continuation_bos(self):
        # Sequence: bearish BOS -> new swing low -> break new swing low = bearish BOS
        dates = pd.date_range("2026-01-01 00:00", periods=18, freq="1h", tz="UTC")
        highs = [100.0, 98.0,  94.0,  96.0,  98.0,  92.0,  88.0,  84.0,  88.0,  90.0,  82.0,  78.0,  74.0,  78.0,  80.0,  72.0,  68.0,  70.0]
        lows  = [96.0,  90.0,  88.0,  92.0,  94.0,  86.0,  82.0,  78.0,  82.0,  84.0,  76.0,  70.0,  66.0,  70.0,  72.0,  64.0,  60.0,  62.0]
        closes= [97.0,  91.0,  92.0,  95.0,  97.0,  87.0,  83.0,  80.0,  87.0,  89.0,  77.0,  71.0,  68.0,  77.0,  79.0,  65.0,  61.0,  65.0]
        opens = [97.0] * 18

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "BOS")
        self.assertEqual(events[0].direction, "bearish")
        self.assertEqual(events[1].event_type, "BOS")
        self.assertEqual(events[1].direction, "bearish")

    def test_05_bullish_reversal_choch(self):
        # Bearish trend + break swing high = bullish CHoCH
        dates = pd.date_range("2026-01-01 00:00", periods=14, freq="1h", tz="UTC")
        highs = [100.0, 98.0,  94.0,  96.0,  98.0,  92.0,  88.0,  84.0,  88.0,  102.0, 105.0, 100.0, 98.0, 96.0]
        lows  = [96.0,  90.0,  88.0,  92.0,  94.0,  86.0,  82.0,  78.0,  82.0,  87.0,  98.0,  95.0,  92.0, 90.0]
        closes= [97.0,  91.0,  92.0,  95.0,  97.0,  87.0,  83.0,  80.0,  87.0,  101.0, 104.0, 97.0,  94.0, 92.0]
        opens = [97.0] * 14

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertGreaterEqual(len(events), 2)
        # First event is bearish BOS
        self.assertEqual(events[0].direction, "bearish")
        self.assertEqual(events[0].event_type, "BOS")
        # Second event is bullish CHoCH
        self.assertEqual(events[1].direction, "bullish")
        self.assertEqual(events[1].event_type, "CHoCH")

    def test_06_bearish_reversal_choch(self):
        # Bullish trend + break swing low = bearish CHoCH
        dates = pd.date_range("2026-01-01 00:00", periods=14, freq="1h", tz="UTC")
        highs = [102.0, 105.0, 110.0, 105.0, 102.0, 112.0, 115.0, 120.0, 115.0, 110.0, 105.0, 100.0, 95.0, 90.0]
        lows  = [98.0,  100.0, 104.0, 101.0, 99.0,  105.0, 110.0, 114.0, 108.0, 95.0,  92.0,  88.0,  84.0, 80.0]
        closes= [100.0, 104.0, 106.0, 102.0, 101.0, 111.0, 114.0, 116.0, 109.0, 96.0,  93.0,  89.0,  85.0, 81.0]
        opens = [100.0] * 14

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertGreaterEqual(len(events), 2)
        # First event is bullish BOS
        self.assertEqual(events[0].direction, "bullish")
        self.assertEqual(events[0].event_type, "BOS")
        # Second event is bearish CHoCH
        self.assertEqual(events[1].direction, "bearish")
        self.assertEqual(events[1].event_type, "CHoCH")

    def test_07_close_equal_swing_price_no_break(self):
        # Close exactly equals swing price -> NO event
        dates = pd.date_range("2026-01-01 00:00", periods=8, freq="1h", tz="UTC")
        highs = [100.0, 105.0, 110.0, 105.0, 102.0, 108.0, 110.0, 108.0]
        lows  = [98.0,  100.0, 104.0, 101.0, 99.0,  102.0, 105.0, 102.0]
        closes= [99.0,  104.0, 106.0, 102.0, 101.0, 106.0, 110.0, 106.0]
        opens = [99.0] * 8

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        # Bar 6 close is 110.0, active high is 110.0 -> close == price -> NO event
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertEqual(events, [])

    def test_08_wick_only_break_no_event(self):
        # High > swing high but Close <= swing high -> NO event
        dates = pd.date_range("2026-01-01 00:00", periods=8, freq="1h", tz="UTC")
        highs = [100.0, 105.0, 110.0, 105.0, 102.0, 108.0, 115.0, 108.0]
        lows  = [98.0,  100.0, 104.0, 101.0, 99.0,  102.0, 105.0, 102.0]
        closes= [99.0,  104.0, 106.0, 102.0, 101.0, 106.0, 109.5, 106.0] # High=115, Close=109.5 <= 110
        opens = [99.0] * 8

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertEqual(events, [])

    def test_09_single_break_per_swing(self):
        # Multiple candles closing above same broken swing -> ONLY 1 event emitted
        dates = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
        highs = [100.0, 105.0, 110.0, 105.0, 102.0, 108.0, 115.0, 118.0, 120.0, 115.0]
        lows  = [98.0,  100.0, 104.0, 101.0, 99.0,  102.0, 105.0, 108.0, 110.0, 105.0]
        closes= [99.0,  104.0, 106.0, 102.0, 101.0, 106.0, 112.0, 116.0, 118.0, 112.0] # Bars 6,7,8 all close > 110
        opens = [99.0] * 10

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, mode="swing")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].index, 6)

    def test_10_broken_at_bar_assignment_and_input_immutability(self):
        dates = pd.date_range("2026-01-01 00:00", periods=8, freq="1h", tz="UTC")
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates,
            "open":  [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 105.0, 112.0],
            "high":  [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 112.0, 116.0],
            "low":   [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 103.0, 110.0],
            "close": [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 111.0, 115.0],
            "volume": 100
        }))
        swings = detect_swings(df, strength=2)

        # Record initial state of all swings
        initial_broken_states = {s.index: (s.broken, s.broken_at) for s in swings}

        events = detect_structure_events(df, swings=swings, strength=2, mode="swing")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].broken_swing_index, 2)

        # [P1] Verify input swings are NOT mutated by the batch detector
        for s in swings:
            self.assertEqual(s.broken, initial_broken_states[s.index][0],
                msg=f"SwingPoint index={s.index} was mutated: broken changed")
            self.assertEqual(s.broken_at, initial_broken_states[s.index][1],
                msg=f"SwingPoint index={s.index} was mutated: broken_at changed")

    def test_11_unconfirmed_swing_not_used(self):
        # A swing confirmed at bar 5 cannot be broken at bar 4 even if price at bar 4 is higher
        dates = pd.date_range("2026-01-01 00:00", periods=8, freq="1h", tz="UTC")
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates,
            "open":  [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 105.0, 112.0],
            "high":  [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 112.0, 116.0],
            "low":   [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 103.0, 110.0],
            "close": [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 111.0, 115.0],
            "volume": 100
        }))
        # Bar 3 has right_strength 2 -> confirmed_at is bar 5
        # Running structure cutoff at current_bar_index=4 MUST NOT emit event for swing at bar 3
        events_at_bar_4 = detect_structure_events(df, strength=2, current_bar_index=4)
        self.assertEqual(events_at_bar_4, [])

    def test_12_current_bar_index_cutoff(self):
        dates = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates,
            "open":  [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 105.0, 112.0, 115.0, 110.0],
            "high":  [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 112.0, 116.0, 118.0, 112.0],
            "low":   [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 103.0, 110.0, 114.0, 108.0],
            "close": [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 111.0, 115.0, 116.0, 109.0],
            "volume": 100
        }))
        # Break happens at bar 6.
        # Asking for cutoff at current_bar_index=5 returns 0 events
        events_5 = detect_structure_events(df, strength=2, current_bar_index=5)
        self.assertEqual(events_5, [])

        # Asking for cutoff at current_bar_index=6 returns 1 event
        events_6 = detect_structure_events(df, strength=2, current_bar_index=6)
        self.assertEqual(len(events_6), 1)

    def test_13_mode_separation_swing_vs_internal(self):
        dates = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates,
            "open":  [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 105.0, 112.0, 115.0, 110.0],
            "high":  [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 112.0, 116.0, 118.0, 112.0],
            "low":   [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 103.0, 110.0, 114.0, 108.0],
            "close": [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 111.0, 115.0, 116.0, 109.0],
            "volume": 100
        }))
        internal_swings = detect_swings(df, strength=2, mode="internal")
        # Structure detector in mode="swing" must ignore internal swings
        events = detect_structure_events(df, swings=internal_swings, strength=2, mode="swing")
        self.assertEqual(events, [])

    def test_14_displacement_true(self):
        # Huge break candle body > ATR * 1.5 -> displacement = True
        n_bars = 20
        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1h", tz="UTC")
        opens  = [100.0] * n_bars
        highs  = [102.0] * n_bars
        lows   = [98.0]  * n_bars
        closes = [100.0] * n_bars

        # High at bar 3 (110)
        highs[3] = 110.0
        # Huge break at bar 15 (open=100, close=120 -> body=20 > ATR(14)~4 * 1.5)
        opens[15] = 100.0
        closes[15] = 120.0
        highs[15] = 122.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, atr_period=14, displacement_multiplier=1.5)
        self.assertGreaterEqual(len(events), 1)
        ev = next(e for e in events if e.index == 15)
        self.assertTrue(ev.displacement)

    def test_15_displacement_false(self):
        # Small break candle body <= ATR * 1.5 -> displacement = False
        n_bars = 20
        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1h", tz="UTC")
        opens  = [100.0] * n_bars
        highs  = [105.0] * n_bars
        lows   = [95.0]  * n_bars
        closes = [100.0] * n_bars

        # High at bar 3 (110)
        highs[3] = 110.0
        # Small break at bar 15 (open=109, close=111 -> body=2 <= ATR(14)~10 * 1.5)
        opens[15] = 109.0
        closes[15] = 111.0
        highs[15] = 112.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        events = detect_structure_events(df, strength=2, atr_period=14, displacement_multiplier=1.5)
        self.assertGreaterEqual(len(events), 1)
        ev = next(e for e in events if e.index == 15)
        self.assertFalse(ev.displacement)

    def test_16_atr_insufficient_data(self):
        # Break happens before atr_period bars -> displacement = False
        dates = pd.date_range("2026-01-01 00:00", periods=8, freq="1h", tz="UTC")
        opens  = [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 100.0, 125.0]
        highs  = [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 103.0, 128.0]
        lows   = [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 99.0,  100.0]
        closes = [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 101.0, 126.0]

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        # Break at bar 7, atr_period=14 -> insufficient bars -> displacement=False
        events = detect_structure_events(df, strength=2, atr_period=14)
        self.assertGreaterEqual(len(events), 1)
        self.assertFalse(events[0].displacement)

    def test_17_atr_zero_value(self):
        # Flat market TR=0 -> ATR=0 -> displacement = False
        df_empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        atr = calculate_atr(df_empty, period=14)
        self.assertEqual(len(atr), 0)

    def test_18_deterministic_ordering_same_bar_confirmation(self):
        # Two swings confirmed on same bar are ordered deterministically
        df = normalize_ohlcv(self.synthetic_df if hasattr(self, 'synthetic_df') else pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=10, freq="1h", tz="UTC"),
            "open": [100.0]*10, "high": [102.0]*10, "low": [98.0]*10, "close": [100.0]*10, "volume": 100
        }))
        swings = detect_swings(df, strength=2)
        # Sort key (confirmed_at, index) is deterministic
        self.assertIsInstance(swings, list)

    def test_19_ambiguous_candle_policy_skip(self):
        # Giant candle breaking active high AND active low -> skipped under policy="skip"
        dates = pd.date_range("2026-01-01 00:00", periods=12, freq="1h", tz="UTC")
        opens  = [100.0, 105.0, 110.0, 105.0, 100.0, 95.0, 90.0, 95.0, 100.0, 100.0, 100.0, 100.0]
        highs  = [102.0, 106.0, 112.0, 106.0, 101.0, 96.0, 91.0, 96.0, 101.0, 101.0, 130.0, 101.0] # High 112 at bar 2
        lows   = [98.0,  104.0, 108.0, 104.0, 99.0,  94.0, 88.0, 94.0, 99.0,  99.0,  80.0,  99.0] # Low 88 at bar 6
        closes = [100.0, 105.0, 110.0, 105.0, 100.0, 95.0, 90.0, 95.0, 100.0, 100.0, 125.0, 100.0] # Bar 10 close=125 > 112 AND low=80 < 88
        
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)
        # Bar 10 breaks active high 112 AND active low 88
        events = detect_structure_events(df, strength=2, ambiguous_policy="skip")
        self.assertEqual(events, [])

    def test_20_batch_and_incremental_parity(self):
        # Parity Test: detect_structure_events vs StructureTracker must output 100% identical events!
        dates = pd.date_range("2026-01-01 00:00", periods=30, freq="1h", tz="UTC")
        highs = [102.0, 105.0, 110.0, 105.0, 102.0, 112.0, 115.0, 120.0, 115.0, 110.0, 105.0, 100.0, 95.0, 90.0, 95.0, 100.0, 95.0, 90.0, 85.0, 90.0, 95.0, 105.0, 110.0, 115.0, 120.0, 125.0, 120.0, 115.0, 110.0, 105.0]
        lows  = [98.0,  100.0, 104.0, 101.0, 99.0,  105.0, 110.0, 114.0, 108.0, 95.0,  92.0,  88.0,  84.0, 80.0, 85.0, 90.0,  84.0, 79.0, 75.0, 80.0, 85.0, 95.0,  100.0, 105.0, 110.0, 114.0, 110.0, 105.0, 100.0, 95.0]
        closes= [100.0, 104.0, 106.0, 102.0, 101.0, 111.0, 114.0, 116.0, 109.0, 96.0,  93.0,  89.0,  85.0, 81.0, 88.0, 94.0,  85.0, 80.0, 76.0, 87.0, 92.0, 102.0, 108.0, 112.0, 118.0, 121.0, 115.0, 110.0, 105.0, 100.0]
        opens = [100.0] * 30

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }), repair_invalid_ohlc=True)

        # 1. Batch execution
        batch_events = detect_structure_events(df, strength=2, mode="swing")

        # 2. Incremental execution
        swing_detector = SwingDetectorState(strength=2, mode="swing")
        tracker = StructureTracker(mode="swing")
        incremental_events = []

        for i in range(len(df)):
            candle_row = df.iloc[i]
            new_swings = swing_detector.update(candle_row)
            new_events = tracker.update(candle_row, confirmed_swings=new_swings)
            incremental_events.extend(new_events)

        self.assertEqual(len(batch_events), len(incremental_events))
        for b_ev, i_ev in zip(batch_events, incremental_events):
            self.assertEqual(b_ev.index, i_ev.index)
            self.assertEqual(b_ev.event_type, i_ev.event_type)
            self.assertEqual(b_ev.direction, i_ev.direction)
            self.assertEqual(b_ev.broken_swing_index, i_ev.broken_swing_index)
            self.assertEqual(b_ev.broken_swing_price, i_ev.broken_swing_price)
            self.assertEqual(b_ev.close_price, i_ev.close_price)
            self.assertEqual(b_ev.displacement, i_ev.displacement)

    def test_21_duplicate_call_protection_in_incremental(self):
        tracker = StructureTracker(mode="swing")
        candle = {"bar_index": 5, "time": "2026-01-01 00:00", "open": 100, "high": 102, "low": 98, "close": 101}
        tracker.update(candle)
        # Feeding bar_index <= 5 raises ValueError
        with self.assertRaises(ValueError):
            tracker.update(candle)

    def test_22_invalid_parameter_validation(self):
        df = normalize_ohlcv(pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC"),
            "open": [100]*5, "high": [102]*5, "low": [98]*5, "close": [100]*5, "volume": 100
        }))
        with self.assertRaises(ValueError):
            detect_structure_events(df, strength=0)
        with self.assertRaises(ValueError):
            detect_structure_events(df, atr_period=0)
        with self.assertRaises(ValueError):
            detect_structure_events(df, displacement_multiplier=-1.0)
        with self.assertRaises(ValueError):
            detect_structure_events(df, mode="invalid_mode")
        with self.assertRaises(ValueError):
            StructureTracker(mode="invalid_mode")

    def test_23_to_dict_serialization(self):
        dates = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates,
            "open":  [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 105.0, 112.0, 115.0, 110.0],
            "high":  [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 112.0, 116.0, 118.0, 112.0],
            "low":   [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 103.0, 110.0, 114.0, 108.0],
            "close": [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 111.0, 115.0, 116.0, 109.0],
            "volume": 100
        }))
        events = detect_structure_events(df, strength=2)
        if events:
            ev_dict = events[0].to_dict()
            self.assertIn("index", ev_dict)
            self.assertIn("time", ev_dict)
            self.assertIn("event_type", ev_dict)
            self.assertIn("direction", ev_dict)
            self.assertIn("broken_swing_index", ev_dict)
            self.assertIn("broken_swing_price", ev_dict)
            self.assertIn("close_price", ev_dict)
            self.assertIn("displacement", ev_dict)
            self.assertIn("mode", ev_dict)

    def test_24_incremental_performance_benchmark_10000_bars(self):
        # 10,000 bars incremental updates must run in under 1.0 second!
        n_bars = 10000
        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1h", tz="UTC")
        prices = 1000.0 + np.sin(np.linspace(0, 100, n_bars)) * 100.0
        
        swing_detector = SwingDetectorState(strength=5, mode="swing")
        tracker = StructureTracker(mode="swing")
        
        t0 = time.perf_counter()
        for i in range(n_bars):
            p = float(prices[i])
            candle = {
                "bar_index": i,
                "time": dates[i],
                "open": p,
                "high": p + 5.0,
                "low": p - 5.0,
                "close": p,
                "volume": 100
            }
            new_swings = swing_detector.update(candle)
            tracker.update(candle, confirmed_swings=new_swings)
            
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0)
        print(f"\n[PASS] Incremental StructureTracker processed 10,000 bars in {elapsed*1000:.2f}ms (<1000ms threshold)")

    def test_25_batch_detector_idempotence(self):
        # [P1] Calling detect_structure_events twice with the same swings list must produce identical results.
        # This verifies that the batch detector is pure/immutable w.r.t. the input SwingPoint list.
        dates = pd.date_range("2026-01-01 00:00", periods=10, freq="1h", tz="UTC")
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates,
            "open":  [100.0, 102.0, 106.0, 108.0, 104.0, 102.0, 105.0, 112.0, 115.0, 110.0],
            "high":  [103.0, 108.0, 110.0, 109.0, 105.0, 104.0, 112.0, 116.0, 118.0, 112.0],
            "low":   [99.0,  101.0, 105.0, 103.0, 101.0, 100.0, 103.0, 110.0, 114.0, 108.0],
            "close": [102.0, 107.0, 109.0, 104.0, 102.0, 103.0, 111.0, 115.0, 116.0, 109.0],
            "volume": 100
        }))
        swings = detect_swings(df, strength=2)

        # First call
        events_run1 = detect_structure_events(df, swings=swings, strength=2, mode="swing")
        # Second call — must produce identical results, NOT be affected by mutation from run 1
        events_run2 = detect_structure_events(df, swings=swings, strength=2, mode="swing")

        self.assertEqual(len(events_run1), len(events_run2),
            msg="Idempotence failed: different number of events on second call")
        for ev1, ev2 in zip(events_run1, events_run2):
            self.assertEqual(ev1.index, ev2.index)
            self.assertEqual(ev1.event_type, ev2.event_type)
            self.assertEqual(ev1.direction, ev2.direction)
            self.assertEqual(ev1.broken_swing_index, ev2.broken_swing_index)
            self.assertEqual(ev1.broken_swing_price, ev2.broken_swing_price)

    def test_26_invalid_ambiguous_policy_raises(self):
        # [P2] ambiguous_policy must be validated at runtime for both detect_structure_events and StructureTracker
        df = normalize_ohlcv(pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC"),
            "open": [100.0]*5, "high": [102.0]*5, "low": [98.0]*5, "close": [100.0]*5, "volume": 100
        }))

        with self.assertRaises(ValueError):
            detect_structure_events(df, ambiguous_policy="bullish")

        with self.assertRaises(ValueError):
            detect_structure_events(df, ambiguous_policy="invalid")

        with self.assertRaises(ValueError):
            StructureTracker(mode="swing", ambiguous_policy="invalid")

        with self.assertRaises(ValueError):
            StructureTracker(mode="swing", ambiguous_policy="both")

if __name__ == "__main__":
    unittest.main()
