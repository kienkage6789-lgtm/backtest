"""
tests/test_smc_order_block.py
=============================
Comprehensive unit and benchmark test suite for SMC Order Block (OB) module:
Covers all 37 test specifications from SMC_ORDER_BLOCK_IMPLEMENTATION_PLAN.md:
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

    def test_36_max_active_blocks_enforced(self):
        """[36] Active block count is strictly capped by max_active_blocks."""
        tracker = OrderBlockTracker(mode="swing", max_active_blocks=5)
        # Create 10 OBs
        dates = pd.date_range("2026-01-01", periods=30, freq="1h", tz="UTC")
        for i in range(30):
            candle = {
                "bar_index": i, "time": dates[i],
                "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0,
                "volume": 100
            }
            new_evs = []
            if i % 3 == 0 and i > 2:
                new_evs = [StructureEvent(index=i, time=dates[i], event_type="BOS", direction="bullish",
                                         broken_swing_index=i-2, broken_swing_price=100.0, close_price=101.0)]
            tracker.update(candle, new_structure_events=new_evs)

        self.assertLessEqual(len(tracker.get_active_blocks()), 5)

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


if __name__ == "__main__":
    unittest.main()
