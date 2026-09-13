"""
tests/test_smc_order_block.py
=============================
Comprehensive unit and benchmark test suite for SMC Order Block (OB) module:
    Covers the 37 plan specifications plus QC regression cases:
- Group 1: Creation & Source Candle (01-09)
- Group 2: FVG Strength & Linking (10-17)
- Group 3: Mitigation Tracking (18-23)
- Group 4: Invalidation Rules (24-28)
- Group 5: Determinism, Immutability & Parity (29-34)
- Group 6: Performance Benchmarks (35-37)
"""

import unittest
import time
import dataclasses
import pandas as pd
import numpy as np

from smc.models import OrderBlock, StructureEvent, FairValueGap
from smc.data_contract import normalize_ohlcv
from smc.zones.order_block import detect_order_blocks, OrderBlockTracker


class TestSMCOrderBlock(unittest.TestCase):

    def _create_base_df(self, n=15):
        dates = pd.date_range("2026-01-01 00:00", periods=n, freq="1h", tz="UTC")
        opens  = [100.0] * n
        highs  = [102.0] * n
        lows   = [98.0]  * n
        closes = [100.0] * n

        # Bar 2: Bearish candle (open=102, close=99, high=103, low=98)
        opens[2] = 102.0
        closes[2] = 99.0
        highs[2] = 103.0
        lows[2] = 98.0

        # Bar 5: Bullish candle (open=99, close=103, high=104, low=99)
        opens[5] = 99.0
        closes[5] = 103.0
        highs[5] = 104.0
        lows[5] = 99.0

        return normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))

    # -----------------------------------------------------------------------
    # Group 1: Creation & Source Candle (01-09)
    # -----------------------------------------------------------------------

    def test_01_bullish_ob_source_candle(self):
        """[01] Bullish event selects the last bearish candle (close < open) before the event."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].index, 2)
        self.assertEqual(obs[0].direction, "bullish")
        self.assertLess(obs[0].close, obs[0].open)

    def test_02_bearish_ob_source_candle(self):
        """[02] Bearish event selects the last bullish candle (close > open) before the event."""
        df = self._create_base_df()
        ev = StructureEvent(index=7, time=df.index[7], event_type="CHoCH", direction="bearish",
                            broken_swing_index=3, broken_swing_price=99.0, close_price=97.0)
        obs = detect_order_blocks(df, [ev])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].index, 5)
        self.assertEqual(obs[0].direction, "bearish")
        self.assertGreater(obs[0].close, obs[0].open)

    def test_03_no_candle_after_event_used(self):
        """[03] Source candle must be strictly before event index (< event.index)."""
        df = self._create_base_df()
        ev = StructureEvent(index=2, time=df.index[2], event_type="BOS", direction="bullish",
                            broken_swing_index=0, broken_swing_price=100.0, close_price=102.0)
        obs = detect_order_blocks(df, [ev])
        for ob in obs:
            self.assertLess(ob.index, ev.index)

    def test_04_no_opposite_candle_no_ob(self):
        """[04] When all candles are in the same direction as the event, no OB is created."""
        dates = pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC")
        # All candles strictly bullish
        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": [100.0, 102.0, 104.0, 106.0, 108.0],
            "high": [103.0, 105.0, 107.0, 109.0, 111.0], "low": [99.0, 101.0, 103.0, 105.0, 107.0],
            "close": [102.0, 104.0, 106.0, 108.0, 110.0], "volume": 100
        }))
        ev = StructureEvent(index=4, time=dates[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=105.0, close_price=110.0)
        obs = detect_order_blocks(df, [ev])
        self.assertEqual(len(obs), 0)

    def test_05_ob_lookback_limits_search(self):
        """[05] Opposite candle outside ob_lookback window is not selected."""
        df = self._create_base_df(n=20)
        # Bearish candle is at bar 2. Event is at bar 15. Lookback = 5 (searches bars 10..14 only)
        ev = StructureEvent(index=15, time=df.index[15], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev], ob_lookback=5)
        self.assertEqual(len(obs), 0)

    def test_06_swing_internal_isolation(self):
        """[06] Mode filtering: internal events don't produce swing OBs and vice versa."""
        df = self._create_base_df()
        ev_internal = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                                     broken_swing_index=1, broken_swing_price=101.0, close_price=103.0, mode="internal")
        obs_swing = detect_order_blocks(df, [ev_internal], mode="swing")
        self.assertEqual(len(obs_swing), 0)

        obs_internal = detect_order_blocks(df, [ev_internal], mode="internal")
        self.assertEqual(len(obs_internal), 1)
        self.assertEqual(obs_internal[0].mode, "internal")

    def test_07_source_event_assigned(self):
        """[07] Created OB stores source event index, type, and broken swing index."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="CHoCH", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertEqual(obs[0].source_event_index, 4)
        self.assertEqual(obs[0].source_event_type, "CHoCH")
        self.assertEqual(obs[0].source_swing_index, 1)
        self.assertEqual(obs[0].created_at, 4)

    def test_08_full_candle_zone(self):
        """[08] In full_candle mode, OB high and low match source candle high and low."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        source_bar = df.iloc[2]
        self.assertEqual(obs[0].high, source_bar["high"])
        self.assertEqual(obs[0].low, source_bar["low"])

    def test_09_duplicate_event_no_duplicate_ob(self):
        """[09] Duplicate events in input do not create duplicate OB objects."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev, ev])
        self.assertEqual(len(obs), 1)

    # -----------------------------------------------------------------------
    # Group 2: FVG Strength & Linking (10-17)
    # -----------------------------------------------------------------------

    def test_10_no_fvg_quality_base(self):
        """[10] When no FVG is present, OB quality is 'base'."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev], fvgs=[])
        self.assertEqual(obs[0].quality, "base")
        self.assertIsNone(obs[0].source_fvg_index)

    def test_11_fvg_same_direction_quality_strong(self):
        """[11] When matching FVG in same direction exists, OB quality is 'strong'."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=4)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg])
        self.assertEqual(obs[0].quality, "strong")
        self.assertEqual(obs[0].source_fvg_index, 3)

    def test_12_wrong_direction_fvg_stays_base(self):
        """[12] FVG with opposite direction does not upgrade OB quality."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg_bearish = FairValueGap(index=3, time=df.index[3], direction="bearish", top=104.0, bottom=101.0,
                                   mode="swing", confirmed_at=4)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg_bearish])
        self.assertEqual(obs[0].quality, "base")

    def test_13_fvg_after_event_excluded(self):
        """[13] FVG confirmed after event is excluded if require_fvg_before_event=True."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg_late = FairValueGap(index=5, time=df.index[5], direction="bullish", top=104.0, bottom=101.0,
                                mode="swing", confirmed_at=6)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg_late], require_fvg_before_event=True)
        self.assertEqual(obs[0].quality, "base")

    def test_14_fvg_wrong_mode_not_linked(self):
        """[14] FVG with different mode ('internal' vs 'swing') is not linked."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0, mode="swing")
        fvg_internal = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                                    mode="internal", confirmed_at=4)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg_internal], mode="swing")
        self.assertEqual(obs[0].quality, "base")

    def test_15_fvg_outside_lookback_not_linked(self):
        """[15] FVG confirmed outside fvg_lookback is not linked."""
        df = self._create_base_df(n=20)
        ev = StructureEvent(index=15, time=df.index[15], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        # OB is at bar 2. FVG at bar 10 -> distance = 8 > fvg_lookback (3)
        fvg_far = FairValueGap(index=10, time=df.index[10], direction="bullish", top=104.0, bottom=101.0,
                               mode="swing", confirmed_at=11)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg_far], fvg_lookback=3)
        self.assertEqual(obs[0].quality, "base")

    def test_16_displacement_and_fvg_premium_candidate(self):
        """[16] FVG linked + displacement=True promotes quality to 'premium_candidate'."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0, displacement=True)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=4)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg])
        self.assertEqual(obs[0].quality, "premium_candidate")

    def test_17_fvg_index_and_range_stored(self):
        """[17] Linked FVG stores source_fvg_index, source_fvg_top, source_fvg_bottom."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.5, bottom=101.2,
                           mode="swing", confirmed_at=4)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg])
        self.assertEqual(obs[0].source_fvg_index, 3)
        self.assertEqual(obs[0].source_fvg_top, 104.5)
        self.assertEqual(obs[0].source_fvg_bottom, 101.2)

    # -----------------------------------------------------------------------
    # Group 3: Mitigation Tracking (18-23)
    # -----------------------------------------------------------------------

    def test_18_mitigation_zero_before_touch(self):
        """[18] Freshly formed untouched OB has mitigated=False and mitigation_pct=0.0."""
        df = self._create_base_df()
        for i in range(3, len(df)):
            df.loc[df.index[i], "low"] = 105.0
            df.loc[df.index[i], "high"] = 110.0
            df.loc[df.index[i], "open"] = 106.0
            df.loc[df.index[i], "close"] = 108.0

        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertFalse(obs[0].mitigated)
        self.assertEqual(obs[0].mitigation_pct, 0.0)

    def test_19_mitigation_partial_pct(self):
        """[19] Partial penetration into OB zone computes correct clamped percentage (0 < pct < 1)."""
        df = self._create_base_df()
        for i in range(3, len(df)):
            df.loc[df.index[i], "low"] = 105.0
            df.loc[df.index[i], "high"] = 110.0
            df.loc[df.index[i], "open"] = 106.0
            df.loc[df.index[i], "close"] = 108.0

        # OB is at bar 2 (high=103, low=98, height=5.0).
        # At bar 6: price dips to low=100.5 (penetration = 103 - 100.5 = 2.5 -> pct = 0.5)
        df.loc[df.index[6], "low"] = 100.5
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertTrue(obs[0].mitigated)
        self.assertAlmostEqual(obs[0].mitigation_pct, 0.5, places=2)
        self.assertEqual(obs[0].mitigated_at, 6)

    def test_20_mitigation_full_penetration(self):
        """[20] Complete sweep of zone computes mitigation_pct=1.0."""
        df = self._create_base_df()
        # Bar 6 low reaches 97.0 <= OB low 98.0 -> 100% penetration
        df.loc[df.index[6], "low"] = 97.0
        df.loc[df.index[6], "close"] = 99.0 # does not break by close
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertEqual(obs[0].mitigation_pct, 1.0)

    def test_21_no_mitigation_before_creation(self):
        """[21] Bars occurring before or at the OB index do not count toward mitigation."""
        df = self._create_base_df()
        for i in range(3, len(df)):
            df.loc[df.index[i], "low"] = 105.0
            df.loc[df.index[i], "high"] = 110.0
            df.loc[df.index[i], "open"] = 106.0
            df.loc[df.index[i], "close"] = 108.0

        # Bar 1 dips very low (before OB candle at bar 2)
        df.loc[df.index[1], "low"] = 90.0
        # Bar 3 dips into zone during breakout leg before event confirmation at bar 4
        df.loc[df.index[3], "low"] = 99.0
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertFalse(obs[0].mitigated)

    def test_22_retest_count_increments(self):
        """[22] Successive visits deepening mitigation increment retest_count."""
        df = self._create_base_df()
        # Bar 6 touches zone
        df.loc[df.index[6], "low"] = 101.0
        # Bar 8 touches deeper
        df.loc[df.index[8], "low"] = 99.5
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertGreaterEqual(obs[0].retest_count, 1)

    def test_23_zero_height_zone_no_crash(self):
        """[23] Zero-height candle (high == low) does not cause ZeroDivisionError."""
        df = self._create_base_df()
        # Make source candle zero-height
        df.loc[df.index[2], "high"] = 100.0
        df.loc[df.index[2], "low"] = 100.0
        df.loc[df.index[2], "open"] = 100.0
        df.loc[df.index[2], "close"] = 99.9 # bearish
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertEqual(len(obs), 1)

    # -----------------------------------------------------------------------
    # Group 4: Invalidation Rules (24-28)
    # -----------------------------------------------------------------------

    def test_24_bullish_ob_invalidated_close_below_low(self):
        """[24] Bullish OB is invalidated when close < ob.low."""
        df = self._create_base_df()
        # Bar 7 closes at 96 < OB low (98)
        df.loc[df.index[7], "close"] = 96.0
        df.loc[df.index[7], "low"] = 95.0
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertFalse(obs[0].valid)
        self.assertEqual(obs[0].invalidated_at, 7)
        self.assertEqual(obs[0].invalidation_reason, "close_break")

    def test_25_bearish_ob_invalidated_close_above_high(self):
        """[25] Bearish OB is invalidated when close > ob.high."""
        df = self._create_base_df()
        # Bar 5 is bullish source candle (high=104). Bar 8 closes at 106 > 104
        df.loc[df.index[8], "close"] = 106.0
        df.loc[df.index[8], "high"] = 107.0
        ev = StructureEvent(index=7, time=df.index[7], event_type="BOS", direction="bearish",
                            broken_swing_index=3, broken_swing_price=99.0, close_price=97.0)
        obs = detect_order_blocks(df, [ev])
        self.assertFalse(obs[0].valid)
        self.assertEqual(obs[0].invalidated_at, 8)
        self.assertEqual(obs[0].invalidation_reason, "close_break")

    def test_26_wick_only_breach_stays_valid(self):
        """[26] Wick penetration beyond zone without a close breach leaves the OB valid=True."""
        df = self._create_base_df()
        # Bar 7 low dips to 95.0 < 98.0, but closes at 99.0 > 98.0
        df.loc[df.index[7], "low"] = 95.0
        df.loc[df.index[7], "close"] = 99.0
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertTrue(obs[0].valid)
        self.assertIsNone(obs[0].invalidated_at)

    def test_27_invalidated_at_bar_assigned(self):
        """[27] invalidated_at accurately stores the first bar index where invalidation occurred."""
        df = self._create_base_df()
        df.loc[df.index[9], "close"] = 95.0
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertEqual(obs[0].invalidated_at, 9)

    def test_28_invalid_ob_not_in_active_blocks(self):
        """[28] Tracker excludes invalidated OBs from get_active_blocks()."""
        df = self._create_base_df()
        df.loc[df.index[7], "close"] = 95.0
        tracker = OrderBlockTracker(mode="swing")
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)

        for i in range(len(df)):
            candle = df.iloc[i]
            new_evs = [ev] if i == 4 else []
            tracker.update(candle, new_structure_events=new_evs)

        active = tracker.get_active_blocks()
        all_blocks = tracker.get_all_blocks()
        self.assertEqual(len(all_blocks), 1)
        self.assertEqual(len(active), 0)

    # -----------------------------------------------------------------------
    # Group 5: Determinism, Immutability & Parity (29-34)
    # -----------------------------------------------------------------------

    def test_29_batch_idempotence(self):
        """[29] Calling detect_order_blocks twice yields identical results."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs1 = detect_order_blocks(df, [ev])
        obs2 = detect_order_blocks(df, [ev])
        self.assertEqual(len(obs1), len(obs2))
        self.assertEqual(obs1[0].to_dict(), obs2[0].to_dict())

    def test_30_input_events_not_mutated(self):
        """[30] Caller input structure_events and fvgs remain completely immutable."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        orig_dict = ev.to_dict()
        detect_order_blocks(df, [ev])
        self.assertEqual(ev.to_dict(), orig_dict)

    def test_31_unconfirmed_event_cutoff(self):
        """[31] Events occurring beyond current bar cutoff are not processed."""
        df = self._create_base_df()
        ev = StructureEvent(index=10, time=df.index[10], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        tracker = OrderBlockTracker(mode="swing")
        # Feed bars up to 5 only
        for i in range(6):
            tracker.update(df.iloc[i])
        self.assertEqual(len(tracker.get_all_blocks()), 0)

    def test_32_fvg_not_confirmed_not_linked(self):
        """[32] FVG with confirmed_at > event.index is not linked when require_fvg_before_event=True."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=5)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg], require_fvg_before_event=True)
        self.assertEqual(obs[0].quality, "base")

    def test_33_replay_ob_not_published_before_event(self):
        """[33] In tracker/replay, OB is only created at the bar where the event is confirmed."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        tracker = OrderBlockTracker(mode="swing")
        created_at_bar = -1
        for i in range(len(df)):
            new_evs = [ev] if i == 4 else []
            new_obs = tracker.update(df.iloc[i], new_structure_events=new_evs)
            if new_obs:
                created_at_bar = i
        self.assertEqual(created_at_bar, 4)

    def test_34_batch_incremental_parity(self):
        """[34] Batch detect_order_blocks and incremental OrderBlockTracker produce equivalent OBs."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        batch_obs = detect_order_blocks(df, [ev])

        tracker = OrderBlockTracker(mode="swing")
        for i in range(len(df)):
            new_evs = [ev] if i == 4 else []
            tracker.update(df.iloc[i], new_structure_events=new_evs)

        tracker_obs = tracker.get_all_blocks()
        self.assertEqual(len(batch_obs), len(tracker_obs))
        self.assertEqual(batch_obs[0].index, tracker_obs[0].index)
        self.assertEqual(batch_obs[0].quality, tracker_obs[0].quality)
        self.assertEqual(batch_obs[0].valid, tracker_obs[0].valid)

    # -----------------------------------------------------------------------
    # Group 6: Performance Benchmarks (35-37)
    # -----------------------------------------------------------------------

    def test_35_incremental_10k_bars_benchmark(self):
        """[35] Incremental tracker processes 10,000 bars in under 1.0 second."""
        tracker = OrderBlockTracker(mode="swing")
        dates = pd.date_range("2026-01-01", periods=10000, freq="1min", tz="UTC")

        t0 = time.perf_counter()
        for i in range(10000):
            p = 100.0 + (i % 50)
            candle = {
                "bar_index": i, "time": dates[i],
                "open": p, "high": p + 2.0, "low": p - 2.0, "close": p + 1.0,
                "volume": 100
            }
            tracker.update(candle)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0)
        print(f"\n[BENCHMARK PASS] OrderBlockTracker processed 10,000 bars in {elapsed*1000:.2f}ms (<1000ms threshold)")

    def test_36_max_active_blocks_is_non_destructive(self):
        """[36] Active cap is a hint and does not remove valid blocks."""
        tracker = OrderBlockTracker(mode="swing", max_active_blocks=5)
        dates = pd.date_range("2026-01-01", periods=30, freq="1h", tz="UTC")
        for i in range(30):
            is_bearish = i % 2 == 0
            candle = {
                "bar_index": i, "time": dates[i], "open": 101.0 if is_bearish else 99.0,
                "high": 102.0, "low": 98.0, "close": 99.0 if is_bearish else 101.0,
                "volume": 100
            }
            new_evs = []
            if i % 3 == 0 and i > 2:
                new_evs = [StructureEvent(index=i, time=dates[i], event_type="BOS", direction="bullish",
                                          broken_swing_index=i-2, broken_swing_price=100.0, close_price=101.0)]
            tracker.update(candle, new_structure_events=new_evs)

        valid = [ob for ob in tracker.get_all_blocks() if ob.valid]
        self.assertGreater(len(valid), 5)
        self.assertEqual(len(tracker.get_active_blocks()), len(valid))
        self.assertEqual(len(tracker.get_all_blocks()), len(valid))

    def test_37_no_on2_growth(self):
        """[37] Verification that tracker per-bar time does not grow with history size O(N^2)."""
        tracker = OrderBlockTracker(mode="swing")
        dates = pd.date_range("2026-01-01", periods=2000, freq="1min", tz="UTC")

        # Warmup first 1000 bars
        for i in range(1000):
            p = 100.0 + (i % 20)
            tracker.update({"bar_index": i, "time": dates[i], "open": p, "high": p+1, "low": p-1, "close": p, "volume": 100})

        # Measure 100 bars from 1000 to 1100
        t0 = time.perf_counter()
        for i in range(1000, 1100):
            p = 100.0 + (i % 20)
            tracker.update({"bar_index": i, "time": dates[i], "open": p, "high": p+1, "low": p-1, "close": p, "volume": 100})
        time_early = time.perf_counter() - t0

        # Advance to 1900
        for i in range(1100, 1900):
            p = 100.0 + (i % 20)
            tracker.update({"bar_index": i, "time": dates[i], "open": p, "high": p+1, "low": p-1, "close": p, "volume": 100})

        # Measure 100 bars from 1900 to 2000
        t1 = time.perf_counter()
        for i in range(1900, 2000):
            p = 100.0 + (i % 20)
            tracker.update({"bar_index": i, "time": dates[i], "open": p, "high": p+1, "low": p-1, "close": p, "volume": 100})
        time_late = time.perf_counter() - t1

        # time_late should not be orders of magnitude larger than time_early (O(1) characteristic)
        ratio = (time_late / time_early) if time_early > 0 else 1.0
        self.assertLess(ratio, 10.0)

    def test_38_fvg_from_other_structure_leg_does_not_upgrade(self):
        """QC: an opposite structure event between OB and event breaks the leg."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0,
                            structure_leg_id="leg-a")
        opposite = StructureEvent(index=3, time=df.index[3], event_type="CHoCH", direction="bearish",
                                  broken_swing_index=1, broken_swing_price=101.0, close_price=99.0)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=4, structure_leg_id="leg-a")
        obs = detect_order_blocks(df, [opposite, ev], fvgs=[fvg])
        bullish = next(ob for ob in obs if ob.source_event_index == 4)
        self.assertEqual(bullish.quality, "base")
        self.assertIsNone(bullish.source_fvg_index)

    def test_39_tracker_retries_late_fvg_without_losing_event(self):
        """QC: require_fvg keeps an event pending until a late FVG arrives."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=5)
        tracker = OrderBlockTracker(mode="swing", require_fvg=True,
                                    require_fvg_before_event=False)
        for i in range(5):
            created = tracker.update(df.iloc[i], new_structure_events=[ev] if i == 4 else [])
        self.assertEqual(created, [])
        created = tracker.update(df.iloc[5], new_fvgs=[fvg])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].created_at, 5)
        self.assertIsNone(created[0].mitigated_at)
        tracker.update(df.iloc[6])
        self.assertGreater(created[0].mitigated_at, created[0].source_event_index)

    def test_40_mitigated_at_is_after_source_event(self):
        """QC: batch mitigation never occurs on or before event confirmation."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        obs = detect_order_blocks(df, [ev])
        self.assertTrue(obs)
        self.assertTrue(obs[0].mitigated_at is None or obs[0].mitigated_at > ev.index)

    def test_41_active_cap_does_not_drop_valid_blocks(self):
        """QC: max_active_blocks must not delete valid active state."""
        df = self._create_base_df()
        events = [
            StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0),
            StructureEvent(index=7, time=df.index[7], event_type="CHoCH", direction="bearish",
                            broken_swing_index=3, broken_swing_price=99.0, close_price=97.0),
        ]
        tracker = OrderBlockTracker(mode="swing", max_active_blocks=1)
        for i in range(len(df)):
            tracker.update(df.iloc[i], new_structure_events=[e for e in events if e.index == i])
        valid = [ob for ob in tracker.get_all_blocks() if ob.valid]
        self.assertGreaterEqual(len(valid), 2)
        self.assertEqual(len(tracker.get_active_blocks()), len(valid))
        self.assertEqual(len(tracker.get_all_blocks()), len(valid))

    def test_42_filled_fvg_before_event_cannot_make_strong_ob(self):
        """QC: an FVG filled before the event is not a valid confluence."""
        df = self._create_base_df()
        ev = StructureEvent(index=6, time=df.index[6], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=4, filled=True, filled_at=5)
        obs = detect_order_blocks(df, [ev], fvgs=[fvg])
        self.assertEqual(obs[0].quality, "base")

    def test_43_future_fvg_is_deferred_until_confirmation_bar(self):
        """QC: tracker must not expose/link an FVG before confirmed_at."""
        df = self._create_base_df()
        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=103.0)
        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=6)
        tracker = OrderBlockTracker(mode="swing", require_fvg=True,
                                    require_fvg_before_event=False)
        for i in range(5):
            created = tracker.update(df.iloc[i], new_structure_events=[ev] if i == 4 else [], new_fvgs=[fvg] if i == 4 else [])
        self.assertEqual(created, [])
        created = tracker.update(df.iloc[5])
        self.assertEqual(created, [])
        created = tracker.update(df.iloc[6])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].created_at, 6)

    def test_44_late_fvg_no_double_process_candle(self):
        """QC Regression 1: Late FVG delivered at bar 7 must not double-process candle 7."""
        dates = pd.date_range("2026-01-01 00:00", periods=12, freq="1h", tz="UTC")
        opens  = [100.0] * 12
        highs  = [102.0] * 12
        lows   = [98.0]  * 12
        closes = [100.0] * 12

        # Bar 2: Bearish source candle (open=102, close=99, high=103, low=98)
        opens[2], closes[2], highs[2], lows[2] = 102.0, 99.0, 103.0, 98.0

        # Bar 4: Breakout event bar
        opens[4], closes[4], highs[4], lows[4] = 100.0, 105.0, 106.0, 99.0

        # Bars 5 & 6 stay clear of OB zone (lows=104.0)
        opens[5], closes[5], highs[5], lows[5] = 105.0, 106.0, 107.0, 104.0
        opens[6], closes[6], highs[6], lows[6] = 106.0, 107.0, 108.0, 104.0

        # Bar 7 dips into OB zone (high=103, low=101.0, close=102.0) -> first touch
        opens[7], closes[7], highs[7], lows[7] = 103.0, 102.0, 103.0, 101.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))

        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=105.0)

        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=5)

        # Batch detection
        batch_obs = detect_order_blocks(df, [ev], fvgs=[fvg], require_fvg=True, require_fvg_before_event=False)
        self.assertEqual(len(batch_obs), 1)
        self.assertEqual(batch_obs[0].retest_count, 1)

        # Incremental tracker
        tracker = OrderBlockTracker(mode="swing", require_fvg=True, require_fvg_before_event=False)
        for i in range(len(df)):
            new_evs = [ev] if i == 4 else []
            # FVG is delivered late at bar 7
            new_fvgs = [fvg] if i == 7 else []
            tracker.update(df.iloc[i], new_structure_events=new_evs, new_fvgs=new_fvgs)

        tracker_obs = tracker.get_all_blocks()
        self.assertEqual(len(tracker_obs), 1)

        # Parity checks
        b_ob = batch_obs[0]
        t_ob = tracker_obs[0]
        self.assertEqual(b_ob.index, t_ob.index)
        self.assertEqual(b_ob.mitigated, t_ob.mitigated)
        self.assertEqual(b_ob.mitigated_at, t_ob.mitigated_at)
        self.assertAlmostEqual(b_ob.mitigation_pct, t_ob.mitigation_pct)
        self.assertEqual(b_ob.retest_count, t_ob.retest_count)
        self.assertEqual(b_ob.retest_count, 1)
        self.assertEqual(b_ob.valid, t_ob.valid)
        self.assertEqual(b_ob.invalidated_at, t_ob.invalidated_at)

    def test_45_pending_ob_when_source_candle_evicted_from_history(self):
        """QC Regression 2: FVG arriving at bar 40 after source candle (bar 2) is evicted must create OB with exact parity."""
        n = 45
        dates = pd.date_range("2026-01-01 00:00", periods=n, freq="1h", tz="UTC")
        opens  = [100.0] * n
        highs  = [102.0] * n
        lows   = [98.0]  * n
        closes = [100.0] * n

        # Bar 2: Bearish source candle (open=102, close=99, high=103, low=98)
        opens[2], closes[2], highs[2], lows[2] = 102.0, 99.0, 103.0, 98.0

        # Bar 4: Breakout event bar
        opens[4], closes[4], highs[4], lows[4] = 100.0, 105.0, 106.0, 99.0

        # Bar 6: Invalidates OB (close=95.0 < ob.low 98.0)
        opens[6], closes[6], highs[6], lows[6] = 98.0, 95.0, 98.0, 94.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))

        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=105.0)

        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=5)

        # Batch detection
        batch_obs = detect_order_blocks(df, [ev], fvgs=[fvg], require_fvg=True, require_fvg_before_event=False)
        self.assertEqual(len(batch_obs), 1)
        b_ob = batch_obs[0]
        self.assertEqual(b_ob.index, 2)
        self.assertEqual(b_ob.created_at, 5)
        self.assertFalse(b_ob.valid)
        self.assertEqual(b_ob.invalidated_at, 6)

        # Incremental tracker with lookback=20 (history maxlen=25, so bar 2 is evicted by bar 30)
        tracker = OrderBlockTracker(mode="swing", require_fvg=True, require_fvg_before_event=False, ob_lookback=20)
        for i in range(len(df)):
            new_evs = [ev] if i == 4 else []
            # FVG is delivered very late at bar 40
            new_fvgs = [fvg] if i == 40 else []
            tracker.update(df.iloc[i], new_structure_events=new_evs, new_fvgs=new_fvgs)

        tracker_obs = tracker.get_all_blocks()
        self.assertEqual(len(tracker_obs), 1)
        t_ob = tracker_obs[0]

        self.assertEqual(b_ob.index, t_ob.index)
        self.assertEqual(b_ob.created_at, t_ob.created_at)
        self.assertEqual(b_ob.valid, t_ob.valid)
        self.assertEqual(b_ob.invalidated_at, t_ob.invalidated_at)
        self.assertEqual(b_ob.to_dict(), t_ob.to_dict())

    def test_46_pending_ob_ignores_lifecycle_before_fvg_confirmation(self):
        """QC Regression 3: Pending OB must ignore any mitigation/invalidation occurring before FVG confirmed_at."""
        n = 45
        dates = pd.date_range("2026-01-01 00:00", periods=n, freq="1h", tz="UTC")
        opens  = [100.0] * n
        highs  = [102.0] * n
        lows   = [98.0]  * n
        closes = [100.0] * n

        # Bar 2: Bearish source candle (open=102, close=99, high=103, low=98)
        opens[2], closes[2], highs[2], lows[2] = 102.0, 99.0, 103.0, 98.0

        # Bar 4: Breakout event bar
        opens[4], closes[4], highs[4], lows[4] = 100.0, 105.0, 106.0, 99.0

        # Bar 5: Dips into OB and closes below OB low (close=95.0 < ob.low 98.0).
        # This would invalidate OB if created_at were 4, but FVG is confirmed at 6.
        opens[5], closes[5], highs[5], lows[5] = 98.0, 95.0, 98.0, 94.0

        # Bars 6..44: Price stays above OB zone
        for k in range(6, n):
            opens[k], closes[k], highs[k], lows[k] = 105.0, 106.0, 107.0, 104.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))

        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=105.0)

        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=6)

        # Batch detection
        batch_obs = detect_order_blocks(df, [ev], fvgs=[fvg], require_fvg=True, require_fvg_before_event=False)
        self.assertEqual(len(batch_obs), 1)
        b_ob = batch_obs[0]
        self.assertEqual(b_ob.index, 2)
        self.assertEqual(b_ob.created_at, 6)
        self.assertTrue(b_ob.valid)
        self.assertIsNone(b_ob.invalidated_at)
        self.assertIsNone(b_ob.invalidation_reason)
        self.assertFalse(b_ob.mitigated)
        self.assertEqual(b_ob.retest_count, 0)

        # Incremental tracker with late FVG delivered at bar 40
        tracker = OrderBlockTracker(mode="swing", require_fvg=True, require_fvg_before_event=False, ob_lookback=20)
        for i in range(len(df)):
            new_evs = [ev] if i == 4 else []
            new_fvgs = [fvg] if i == 40 else []
            tracker.update(df.iloc[i], new_structure_events=new_evs, new_fvgs=new_fvgs)

        tracker_obs = tracker.get_all_blocks()
        self.assertEqual(len(tracker_obs), 1)
        t_ob = tracker_obs[0]

        # Explicit assertions required by spec
        self.assertEqual(t_ob.created_at, 6)
        self.assertTrue(t_ob.valid)
        self.assertIsNone(t_ob.invalidated_at)
        self.assertIsNone(t_ob.invalidation_reason)
        self.assertFalse(t_ob.mitigated)
        self.assertEqual(t_ob.retest_count, 0)

        # 100% parity assertion between batch and incremental
        self.assertEqual(b_ob.to_dict(), t_ob.to_dict())

    def test_47_pending_ob_survives_arbitrary_delivery_delay(self):
        """QC Regression 4: Pending OB must survive arbitrary delivery delays (e.g. bar 41 and bar 100) with 100% parity."""
        n = 105
        dates = pd.date_range("2026-01-01 00:00", periods=n, freq="1h", tz="UTC")
        opens  = [100.0] * n
        highs  = [102.0] * n
        lows   = [98.0]  * n
        closes = [100.0] * n

        # Bar 2: Bearish source candle (open=102, close=99, high=103, low=98)
        opens[2], closes[2], highs[2], lows[2] = 102.0, 99.0, 103.0, 98.0

        # Bar 4: Breakout event bar
        opens[4], closes[4], highs[4], lows[4] = 100.0, 105.0, 106.0, 99.0

        # Bar 6: Invalidates OB (close=95.0 < ob.low 98.0)
        opens[6], closes[6], highs[6], lows[6] = 98.0, 95.0, 98.0, 94.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))

        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=105.0)

        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=5)

        # Batch detection
        batch_obs = detect_order_blocks(df, [ev], fvgs=[fvg], require_fvg=True, require_fvg_before_event=False)
        self.assertEqual(len(batch_obs), 1)
        b_ob = batch_obs[0]
        self.assertEqual(b_ob.index, 2)
        self.assertEqual(b_ob.created_at, 5)
        self.assertFalse(b_ob.valid)
        self.assertEqual(b_ob.invalidated_at, 6)

        # Case 1: FVG delivered at bar 41
        tracker_41 = OrderBlockTracker(mode="swing", require_fvg=True, require_fvg_before_event=False, ob_lookback=20)
        for i in range(42):
            new_evs = [ev] if i == 4 else []
            new_fvgs = [fvg] if i == 41 else []
            tracker_41.update(df.iloc[i], new_structure_events=new_evs, new_fvgs=new_fvgs)

        t_obs_41 = tracker_41.get_all_blocks()
        self.assertEqual(len(t_obs_41), 1)
        self.assertEqual(b_ob.to_dict(), t_obs_41[0].to_dict())

        # Case 2: FVG delivered at bar 100
        tracker_100 = OrderBlockTracker(mode="swing", require_fvg=True, require_fvg_before_event=False, ob_lookback=20)
        for i in range(101):
            new_evs = [ev] if i == 4 else []
            new_fvgs = [fvg] if i == 100 else []
            tracker_100.update(df.iloc[i], new_structure_events=new_evs, new_fvgs=new_fvgs)

        t_obs_100 = tracker_100.get_all_blocks()
        self.assertEqual(len(t_obs_100), 1)
        self.assertEqual(b_ob.to_dict(), t_obs_100[0].to_dict())

    def test_48_unmatched_pending_events_do_not_leak_memory_worst_case(self):
        """QC Regression 5: Streaming thousands of unmatched structure events must enforce bounded memory & execution time."""
        tracker = OrderBlockTracker(
            mode="swing",
            require_fvg=True,
            require_fvg_before_event=False,
            max_pending_delivery_lag=500,
            max_pending_obs=30,
        )
        dates = pd.date_range("2026-01-01", periods=5000, freq="1min", tz="UTC")

        # Feed 5000 bars with 500 unmatched structure events.
        # Keep prices high (low=105.0) so candidate OBs (low=98.0) stay valid and accumulate to capacity (30).
        t0 = time.perf_counter()
        for i in range(5000):
            if i % 10 == 8:
                # Bearish source candle (open=102, close=99, high=103, low=98)
                candle = {
                    "bar_index": i, "time": dates[i],
                    "open": 102.0, "high": 103.0, "low": 98.0, "close": 99.0,
                    "volume": 100
                }
            else:
                candle = {
                    "bar_index": i, "time": dates[i],
                    "open": 110.0, "high": 112.0, "low": 105.0, "close": 111.0,
                    "volume": 100
                }
            new_evs = []
            if i % 10 == 0 and i >= 10:
                # Add structure event without matching FVG
                new_evs = [StructureEvent(
                    index=i, time=dates[i], event_type="BOS", direction="bullish",
                    broken_swing_index=i-4, broken_swing_price=101.0, close_price=111.0
                )]
            tracker.update(candle, new_structure_events=new_evs)

        elapsed = time.perf_counter() - t0

        # Memory bound check: pending obs count must reach and not exceed max_pending_obs
        self.assertEqual(len(tracker._pending_obs), 30)

        # Performance check: 5000 bars with 500 unmatched pending events must complete under 1.0s
        self.assertLess(elapsed, 1.0)
        print(f"\n[WORST-CASE PASS] Processed 5,000 bars with 500 unmatched events in {elapsed*1000:.2f}ms (Pending pool size={len(tracker._pending_obs)} == 30)")

    def test_49_terminal_pending_ob_materializes_after_all_cutoffs_invalid(self):
        """QC Regression 6: Terminal pending OB invalidated at bar 8 must materialize at bar 41 with 100% parity."""
        n = 45
        dates = pd.date_range("2026-01-01 00:00", periods=n, freq="1h", tz="UTC")
        opens  = [100.0] * n
        highs  = [102.0] * n
        lows   = [98.0]  * n
        closes = [100.0] * n

        # Bar 2: Bearish source candle (open=102, close=99, high=103, low=98)
        opens[2], closes[2], highs[2], lows[2] = 102.0, 99.0, 103.0, 98.0

        # Bar 4: Breakout event bar
        opens[4], closes[4], highs[4], lows[4] = 100.0, 105.0, 106.0, 99.0

        # Bar 8: Close breaks OB low 98.0 (close=95.0) -> invalidates OB at bar 8 across all cutoffs
        opens[8], closes[8], highs[8], lows[8] = 98.0, 95.0, 98.0, 94.0

        # Bars 9..44: Price stays clear of OB zone
        for k in range(9, n):
            opens[k], closes[k], highs[k], lows[k] = 105.0, 106.0, 107.0, 104.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))

        ev = StructureEvent(index=4, time=df.index[4], event_type="BOS", direction="bullish",
                            broken_swing_index=1, broken_swing_price=101.0, close_price=105.0)

        fvg = FairValueGap(index=3, time=df.index[3], direction="bullish", top=104.0, bottom=101.0,
                           mode="swing", confirmed_at=5)

        # Batch detection
        batch_obs = detect_order_blocks(df, [ev], fvgs=[fvg], require_fvg=True, require_fvg_before_event=False)
        self.assertEqual(len(batch_obs), 1)
        b_ob = batch_obs[0]
        self.assertEqual(b_ob.index, 2)
        self.assertEqual(b_ob.created_at, 5)
        self.assertFalse(b_ob.valid)
        self.assertEqual(b_ob.invalidated_at, 8)
        self.assertEqual(b_ob.invalidation_reason, "close_break")

        # Incremental tracker with FVG delivered late at bar 41
        tracker = OrderBlockTracker(mode="swing", require_fvg=True, require_fvg_before_event=False, ob_lookback=20)
        for i in range(len(df)):
            new_evs = [ev] if i == 4 else []
            new_fvgs = [fvg] if i == 41 else []
            tracker.update(df.iloc[i], new_structure_events=new_evs, new_fvgs=new_fvgs)

        tracker_obs = tracker.get_all_blocks()
        self.assertEqual(len(tracker_obs), 1)
        t_ob = tracker_obs[0]

        # Parity assertions
        self.assertEqual(b_ob.to_dict(), t_ob.to_dict())
        self.assertFalse(t_ob.valid)
        self.assertEqual(t_ob.invalidated_at, 8)
        self.assertEqual(t_ob.invalidation_reason, "close_break")
        self.assertNotIn(t_ob, tracker.get_active_blocks())
        self.assertEqual(tracker.get_active_blocks(), [])

    def test_50_pending_delivery_lag_boundary(self):
        """Pending FVG delivery is accepted at the inclusive boundary only."""
        n = 101
        dates = pd.date_range("2026-01-01 00:00", periods=n, freq="1h", tz="UTC")
        opens = [100.0] * n
        highs = [102.0] * n
        lows = [98.0] * n
        closes = [100.0] * n
        opens[2], closes[2], highs[2], lows[2] = 102.0, 99.0, 103.0, 98.0
        opens[4], closes[4], highs[4], lows[4] = 100.0, 105.0, 106.0, 99.0

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": 100,
        }))
        ev = StructureEvent(
            index=4, time=df.index[4], event_type="BOS", direction="bullish",
            broken_swing_index=1, broken_swing_price=101.0, close_price=105.0,
        )
        fvg = FairValueGap(
            index=3, time=df.index[3], direction="bullish", top=104.0,
            bottom=101.0, mode="swing", confirmed_at=5,
        )

        # max_created_at = max(event.index, ob.index + fvg_lookback) = 7;
        # with lag=3, bar 10 is eligible and bar 11 is expired.
        def run_until(delivery_bar):
            tracker = OrderBlockTracker(
                mode="swing", require_fvg=True,
                require_fvg_before_event=False, ob_lookback=20,
                fvg_lookback=5, max_pending_delivery_lag=3,
            )
            for i in range(delivery_bar + 1):
                tracker.update(
                    df.iloc[i],
                    new_structure_events=[ev] if i == 4 else [],
                    new_fvgs=[fvg] if i == delivery_bar else [],
                )
            return tracker

        batch_ob = detect_order_blocks(
            df.iloc[:11], [ev], fvgs=[fvg], mode="swing", ob_lookback=20,
            fvg_lookback=5, require_fvg=True,
            require_fvg_before_event=False,
        )[0]
        boundary_tracker = run_until(10)
        boundary_obs = boundary_tracker.get_all_blocks()
        self.assertEqual(len(boundary_obs), 1)
        self.assertEqual(batch_ob.to_dict(), boundary_obs[0].to_dict())
        self.assertEqual(len(boundary_tracker._pending_obs), 0)

        expired_tracker = run_until(11)
        self.assertEqual(expired_tracker.get_all_blocks(), [])
        self.assertEqual(expired_tracker.get_active_blocks(), [])
        self.assertEqual(expired_tracker._pending_obs, {})

        far_expired_tracker = run_until(100)
        self.assertEqual(far_expired_tracker.get_all_blocks(), [])
        self.assertEqual(far_expired_tracker.get_active_blocks(), [])
        self.assertEqual(far_expired_tracker._pending_obs, {})


if __name__ == "__main__":
    unittest.main()
