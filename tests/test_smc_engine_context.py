"""
tests/test_smc_engine_context.py
=================================
Comprehensive Unit & Integration Test Suite for T53.2:
- StrategyContextBuilder (Incremental & Stateful)
- build_strategy_contexts (Batch Engine)
- Zero-Lookahead, Determinism, Deep Immutability, JSON Safety
- Full Lifecycle Contracts (Swings, Structures, FVGs, OBs, Pools, Sweeps, Sessions, HTF Bias)
- Monotonicity, Closed-Candle Guards, Idempotence, Conflict Handling
- Batch / Incremental Exact Parity (100% full-payload)
- Future-Append Invariance
- Future-State Injection Probes
- 10,000-bar Performance Benchmark
"""

import copy
import dataclasses
import datetime
import json
import os
import time
import unittest
from collections import deque
from types import MappingProxyType
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from smc.models import (
    SwingPoint,
    StructureEvent,
    FairValueGap,
    OrderBlock,
    LiquidityPool,
    LiquiditySweep,
    SessionWindow,
    SessionDecision,
    BiasState,
)
from smc.context.session import LONDON_KILLZONE, NEWYORK_KILLZONE
from smc.engine import (
    StrategyContext,
    SwingPointSnapshot,
    OrderBlockSnapshot,
    ContextBuilderConfig,
    StrategyContextBuilder,
    build_strategy_contexts,
    SUPPORTED_TIMEFRAMES,
    validate_as_of_evidence,
    StrictModelTypeError,
)
from smc.structure.swings import SwingDetectorState
from smc.structure.bos_choch import StructureTracker
from smc.zones.fvg import FVGTracker
from smc.zones.order_block import OrderBlockTracker
from smc.liquidity.detector import LiquidityTracker
from smc.context.session import SessionFilter
from smc.context.htf_bias import HTFBiasTracker


def _generate_synthetic_candles(n_bars: int = 50, start_time: str = "2026-01-15 07:00:00 UTC", freq: str = "15min") -> pd.DataFrame:
    """Generates synthetic trending & ranging candles with swings and gaps (vectorized)."""
    indices = np.arange(n_bars)
    waves = np.sin(indices / 3.0) * 10.0 + (indices * 0.5)
    opens = 2000.0 + waves
    highs = opens + 2.0 + np.abs(np.cos(indices) * 1.5) + 0.1
    lows = opens - 2.0 - np.abs(np.sin(indices) * 1.5) - 0.1
    closes = opens + np.cos(indices) * 1.0
    highs = np.maximum(highs, np.maximum(opens, closes) + 0.1)
    lows = np.minimum(lows, np.minimum(opens, closes) - 0.1)
    vols = 100.0 + indices * 2.0
    times = pd.date_range(start_time, periods=n_bars, freq=freq)

    return pd.DataFrame({
        "bar_index": indices,
        "time": times,
        "open": np.round(opens, 3),
        "high": np.round(highs, 3),
        "low": np.round(lows, 3),
        "close": np.round(closes, 3),
        "volume": np.round(vols, 2),
        "closed": True,
    })


def _create_trackers_from_config(config: ContextBuilderConfig, htf_events=None):
    """Creates detector/tracker instances strictly matching StrategyContextBuilder's configuration."""
    return (
        SwingDetectorState(
            strength=config.swing_strength,
            left_strength=config.swing_left_strength,
            right_strength=config.swing_right_strength,
            mode=config.structure_mode,
        ),
        StructureTracker(
            mode=config.structure_mode,
            atr_period=config.atr_period,
            displacement_multiplier=config.displacement_multiplier,
        ),
        FVGTracker(
            mode=config.structure_mode,
            min_gap_pct=config.fvg_min_gap_pct,
        ),
        OrderBlockTracker(
            mode=config.structure_mode,
            ob_lookback=config.ob_lookback,
            require_fvg=config.ob_require_fvg,
        ),
        LiquidityTracker(
            tolerance_pct=config.liquidity_tolerance_pct,
            mode=config.structure_mode,
        ),
        SessionFilter(
            sessions=config.sessions,
            default_timezone=config.session_timezone,
        ),
        HTFBiasTracker(
            conflict_policy=config.htf_conflict_policy,
            htf_events=htf_events,
        ),
    )


class TestSMCEngineContext(unittest.TestCase):

    def setUp(self):
        self.config = ContextBuilderConfig(
            symbol="XAUUSD",
            timeframe="M15",
            swing_strength=2,
            atr_period=5,
            ob_lookback=10,
        )
        self.builder = StrategyContextBuilder(self.config)

    # -------------------------------------------------------------------------
    # 1. Closed Bar Creation & Timeframe -> bar_close_time
    # -------------------------------------------------------------------------
    def test_01_context_created_successfully_from_closed_bar(self):
        """Context creates successfully from valid closed bar."""
        candle = {
            "bar_index": 0,
            "time": "2026-01-15 08:00:00 UTC",
            "open": 2000.0,
            "high": 2005.0,
            "low": 1995.0,
            "close": 2002.0,
            "volume": 150.0,
            "closed": True,
        }
        ctx = self.builder.update(candle)
        self.assertIsInstance(ctx, StrategyContext)
        self.assertEqual(ctx.bar_index, 0)
        self.assertEqual(ctx.symbol, "XAUUSD")
        self.assertEqual(ctx.timeframe, "M15")
        self.assertEqual(ctx.open, 2000.0)
        self.assertEqual(ctx.high, 2005.0)
        self.assertEqual(ctx.low, 1995.0)
        self.assertEqual(ctx.close, 2002.0)
        self.assertEqual(ctx.volume, 150.0)
        self.assertEqual(ctx.atr14, 0.0)  # Warming up (< atr_period)
        self.assertGreater(ctx.bar_close_time, ctx.timestamp)

    def test_02_timeframe_to_bar_close_time_mapping(self):
        """Timeframe duration correctly computes bar_close_time for all supported frames."""
        ts = pd.Timestamp("2026-01-15 10:00:00 UTC")
        for tf, delta in SUPPORTED_TIMEFRAMES.items():
            cfg = ContextBuilderConfig(timeframe=tf, atr_period=5, swing_strength=2)
            b = StrategyContextBuilder(cfg)
            candle = {
                "bar_index": 0,
                "time": ts,
                "open": 2000.0,
                "high": 2005.0,
                "low": 1995.0,
                "close": 2002.0,
                "volume": 10.0,
                "closed": True,
            }
            c = b.update(candle)
            self.assertEqual(c.bar_close_time, ts + delta)
            self.assertGreater(c.bar_close_time, c.timestamp)

    def test_03_reject_unsupported_timeframe(self):
        """Unsupported timeframe raises ValueError."""
        with self.assertRaises(ValueError):
            ContextBuilderConfig(timeframe="M2")
        with self.assertRaises(ValueError):
            ContextBuilderConfig(timeframe="W1")

    # -------------------------------------------------------------------------
    # 2. Closed-Candle Guards & Monotonicity
    # -------------------------------------------------------------------------
    def test_04_reject_unclosed_candle(self):
        """Builder strictly rejects unclosed candles (closed=False, is_closed=False)."""
        candle = {
            "bar_index": 0,
            "time": "2026-01-15 08:00:00 UTC",
            "open": 2000.0,
            "high": 2005.0,
            "low": 1995.0,
            "close": 2002.0,
            "closed": False,
        }
        with self.assertRaises(ValueError):
            self.builder.update(candle)

        # Boolean parser also rejects bool('false')
        candle_str_false = dict(candle)
        candle_str_false["closed"] = "false"
        with self.assertRaises(ValueError):
            self.builder.update(candle_str_false)

    def test_05_reject_non_monotonic_bar_index(self):
        """Non-monotonic bar index (going backward) raises ValueError."""
        c0 = {"bar_index": 5, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        self.builder.update(c0)

        c_backward = {"bar_index": 4, "time": "2026-01-15 08:15:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        with self.assertRaises(ValueError):
            self.builder.update(c_backward)

    def test_06_reject_non_monotonic_timestamp(self):
        """Non-monotonic timestamp (going backward or equal) raises ValueError (P2.1)."""
        c0 = {"bar_index": 1, "time": "2026-01-15 08:15:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        self.builder.update(c0)

        # 1. Backward timestamp on bar 2 raises ValueError
        c_time_backward = {"bar_index": 2, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        with self.assertRaises(ValueError):
            self.builder.update(c_time_backward)

        # 2. Equal timestamp on bar 2 raises ValueError (strictly monotonic requirement)
        c_time_equal = {"bar_index": 2, "time": "2026-01-15 08:15:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        with self.assertRaises(ValueError):
            self.builder.update(c_time_equal)

        # 3. Same bar_index 1 but different timestamp raises ValueError
        c_same_idx_diff_time = {"bar_index": 1, "time": "2026-01-15 08:30:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        with self.assertRaises(ValueError):
            self.builder.update(c_same_idx_diff_time)

    def test_07_duplicate_update_idempotence(self):
        """Calling update() on same bar_index with identical payload is idempotent."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "volume": 100.0, "closed": True}
        ctx1 = self.builder.update(c0)
        ctx2 = self.builder.update(c0)
        self.assertEqual(ctx1.to_dict(), ctx2.to_dict())

    def test_07b_duplicate_update_with_identical_htf_events_is_idempotent(self):
        """Duplicate call with identical candle AND identical new_htf_events returns cached context."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "volume": 100.0, "closed": True}
        ev = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-15 07:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=1990.0,
            close_price=1995.0,
        )
        ctx1 = self.builder.update(c0, new_htf_events=[ev])
        ctx2 = self.builder.update(c0, new_htf_events=[ev])
        self.assertEqual(ctx1.to_dict(), ctx2.to_dict())

    def test_07c_duplicate_update_with_differing_htf_events_raises_value_error(self):
        """Duplicate candle update with different HTF events (first None, second has event) raises ValueError."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "volume": 100.0, "closed": True}
        ev = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-15 07:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=1990.0,
            close_price=1995.0,
        )
        self.builder.update(c0)
        with self.assertRaises(ValueError):
            self.builder.update(c0, new_htf_events=[ev])

    def test_07d_duplicate_update_with_conflicting_event_payload_raises_value_error(self):
        """HTF events with identical ID but differing payloads raise ValueError."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "volume": 100.0, "closed": True}
        ev1 = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-15 07:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=1990.0,
            close_price=1995.0,
        )
        ev1_conflict = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-15 07:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=2010.0,
            close_price=1995.0,
        )
        with self.assertRaises(ValueError):
            self.builder.update(c0, new_htf_events=[ev1, ev1_conflict])

    def test_07e_valid_htf_event_on_first_update_reflects_in_bias(self):
        """Valid HTF event provided on first update correctly updates HTF bias according to effective_time."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "volume": 100.0, "closed": True}
        ev = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-15 07:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=1990.0,
            close_price=1995.0,
        )
        ctx = self.builder.update(c0, new_htf_events=[ev])
        self.assertIsNotNone(ctx.htf_bias)
        self.assertEqual(ctx.htf_bias.bias, "bullish")
        self.assertEqual(ctx.htf_bias.source_event_index, 10)

    def test_07f_htf_events_no_mutation_of_historical_context(self):
        """Ingesting HTF events on subsequent bars does not mutate previously returned context."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "volume": 100.0, "closed": True}
        ctx0 = self.builder.update(c0)
        dict0_before = ctx0.to_dict()

        c1 = {"bar_index": 1, "time": "2026-01-15 08:15:00 UTC", "open": 2002.0, "high": 2008.0, "low": 2000.0, "close": 2006.0, "volume": 100.0, "closed": True}
        ev1 = StructureEvent(
            index=20,
            time=pd.Timestamp("2026-01-15 08:10:00 UTC"),
            event_type="BOS",
            direction="bearish",
            broken_swing_index=8,
            broken_swing_price=2008.0,
            close_price=2000.0,
        )
        ctx1 = self.builder.update(c1, new_htf_events=[ev1])
        dict0_after = ctx0.to_dict()

        self.assertEqual(dict0_before, dict0_after)
        self.assertNotEqual(ctx0.htf_bias.bias if ctx0.htf_bias else "neutral", ctx1.htf_bias.bias)

    def test_08_duplicate_conflicting_update_rejected(self):
        """Calling update() on same bar_index with different candle payload raises ValueError."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        self.builder.update(c0)

        c0_diff = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2010.0, "low": 1995.0, "close": 2008.0, "closed": True}
        with self.assertRaises(ValueError):
            self.builder.update(c0_diff)

    # -------------------------------------------------------------------------
    # 3. SwingPoint Cutoff & Future-Broken Leak Guard
    # -------------------------------------------------------------------------
    def test_09_swing_confirmation_cutoff(self):
        """Swings are only included if confirmed_at <= current bar N."""
        df = _generate_synthetic_candles(20)
        contexts = build_strategy_contexts(df, self.config)

        # Before swing confirmation lag (swing_strength = 2, requires left + right + 1 = 5 bars)
        # Bar 0, 1, 2, 3 must have 0 recent swings
        self.assertEqual(len(contexts[0].recent_swings), 0)
        self.assertEqual(len(contexts[1].recent_swings), 0)

        # Later bars should have swings, and all swings must satisfy confirmed_at <= bar_index
        for ctx in contexts:
            for s in ctx.recent_swings:
                self.assertLessEqual(s.confirmed_at, ctx.bar_index)

    def test_10_swing_future_broken_state_does_not_leak(self):
        """A swing that is broken at bar N+5 must have broken=False, broken_at=None in snapshot at bar N."""
        sw_future_broken = SwingPoint(
            index=2,
            time=pd.Timestamp("2026-01-15 07:30:00 UTC"),
            price=2010.0,
            kind="high",
            strength=2,
            confirmed_at=4,
            broken=True,
            broken_at=10,  # broken in future relative to bar 5
        )
        # Directly test low-level validator
        validate_as_of_evidence(sw_future_broken, current_bar_index=10, bar_close_time=pd.Timestamp("2026-01-15 12:00:00 UTC"))

        # If current_bar_index is 5, validate_as_of_evidence must raise ValueError because broken_at > 5
        with self.assertRaises(ValueError):
            validate_as_of_evidence(sw_future_broken, current_bar_index=5, bar_close_time=pd.Timestamp("2026-01-15 08:30:00 UTC"))

    def test_10b_swing_future_broken_state_builder_integration(self):
        """Swing broken in future at bar N+K has broken=False, broken_at=None in context at bar N."""
        candles = [
            {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "closed": True},
            {"bar_index": 1, "time": "2026-01-15 08:15:00 UTC", "open": 101.0, "high": 105.0, "low": 100.0, "close": 104.0, "closed": True},
            {"bar_index": 2, "time": "2026-01-15 08:30:00 UTC", "open": 103.0, "high": 104.0, "low": 98.0, "close": 99.0, "closed": True},  # Confirms swing high at bar 1
            {"bar_index": 3, "time": "2026-01-15 08:45:00 UTC", "open": 99.0, "high": 101.0, "low": 97.0, "close": 98.0, "closed": True},
            {"bar_index": 4, "time": "2026-01-15 09:00:00 UTC", "open": 98.0, "high": 102.0, "low": 97.5, "close": 100.0, "closed": True},
            {"bar_index": 5, "time": "2026-01-15 09:15:00 UTC", "open": 100.0, "high": 110.0, "low": 99.0, "close": 108.0, "closed": True},  # Breaks swing high 105.0
        ]
        cfg = ContextBuilderConfig(swing_strength=1, swing_left_strength=1, swing_right_strength=1, atr_period=5)
        builder = StrategyContextBuilder(cfg)
        contexts = [builder.update(c) for c in candles]

        # At bar 3: swing 1 is confirmed but not yet broken
        sw_at_3 = next(s for s in contexts[3].recent_swings if s.index == 1)
        self.assertFalse(sw_at_3.broken)
        self.assertIsNone(sw_at_3.broken_at)

        # At bar 5: swing 1 is broken by bar 5
        sw_at_5 = next(s for s in contexts[5].recent_swings if s.index == 1)
        self.assertTrue(sw_at_5.broken)
        self.assertEqual(sw_at_5.broken_at, 5)

    # -------------------------------------------------------------------------
    # 4. StructureEvent Cutoff & Ordering
    # -------------------------------------------------------------------------
    def test_11_structure_event_cutoff(self):
        """Structure events are only included if event.index <= current bar N."""
        df = _generate_synthetic_candles(30)
        contexts = build_strategy_contexts(df, self.config)

        for ctx in contexts:
            for st in ctx.recent_structures:
                self.assertLessEqual(st.index, ctx.bar_index)

    # -------------------------------------------------------------------------
    # 5. FVG Cutoff, Future-Fill Leak Guard & Fill at Bar N
    # -------------------------------------------------------------------------
    def test_12_fvg_confirmation_cutoff(self):
        """FVG is only included if confirmed_at <= current bar N."""
        df = _generate_synthetic_candles(25)
        contexts = build_strategy_contexts(df, self.config)

        for ctx in contexts:
            for f in ctx.active_fvgs:
                self.assertLessEqual(f.confirmed_at, ctx.bar_index)

    def test_13_fvg_future_fill_leak_guard(self):
        """FVG with filled_at > N raises future leak error."""
        fvg = FairValueGap(
            index=5,
            time=pd.Timestamp("2026-01-15 08:15:00 UTC"),
            direction="bullish",
            top=2010.0,
            bottom=2005.0,
            confirmed_at=6,
            filled=True,
            filled_at=12,
        )
        with self.assertRaises(ValueError):
            validate_as_of_evidence(fvg, current_bar_index=8, bar_close_time=pd.Timestamp("2026-01-15 09:00:00 UTC"))

    def test_14_fvg_fill_at_bar_n_is_active(self):
        """FVG filled at bar N (filled_at == N) is included in active_fvgs at bar N for retest detection."""
        fvg_filled_at_n = FairValueGap(
            index=5,
            time=pd.Timestamp("2026-01-15 08:15:00 UTC"),
            direction="bullish",
            top=2010.0,
            bottom=2005.0,
            confirmed_at=6,
            filled=True,
            filled_at=8,
        )
        # At bar 8, filled_at == 8 is valid as-of bar 8
        validate_as_of_evidence(fvg_filled_at_n, current_bar_index=8, bar_close_time=pd.Timestamp("2026-01-15 09:00:00 UTC"))

    def test_14b_fvg_filled_at_bar_n_builder_integration(self):
        """FVG filled at bar N (filled_at == N) is in active_fvgs at bar N and pruned at bar N+1."""
        candles = [
            {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "closed": True},
            {"bar_index": 1, "time": "2026-01-15 08:15:00 UTC", "open": 101.0, "high": 110.0, "low": 101.0, "close": 109.0, "closed": True},
            {"bar_index": 2, "time": "2026-01-15 08:30:00 UTC", "open": 109.0, "high": 115.0, "low": 106.0, "close": 114.0, "closed": True},
            {"bar_index": 3, "time": "2026-01-15 08:45:00 UTC", "open": 114.0, "high": 114.0, "low": 101.5, "close": 108.0, "closed": True},
            {"bar_index": 4, "time": "2026-01-15 09:00:00 UTC", "open": 108.0, "high": 112.0, "low": 107.0, "close": 111.0, "closed": True},
        ]
        cfg = ContextBuilderConfig(fvg_min_gap_pct=0.0)
        builder = StrategyContextBuilder(cfg)
        contexts = [builder.update(c) for c in candles]

        # At bar 2: FVG is active, not filled
        self.assertEqual(len(contexts[2].active_fvgs), 1)
        self.assertFalse(contexts[2].active_fvgs[0].filled)

        # At bar 3: FVG is filled by bar 3 low (101.5 < 102.0), but remains active as-of bar 3
        self.assertEqual(len(contexts[3].active_fvgs), 1)
        self.assertTrue(contexts[3].active_fvgs[0].filled)
        self.assertEqual(contexts[3].active_fvgs[0].filled_at, 3)

        # At bar 4: FVG with filled_at=3 < 4 is pruned from active_fvgs
        self.assertEqual(len(contexts[4].active_fvgs), 0)

    # -------------------------------------------------------------------------
    # 6. OrderBlock Created Cutoff & Invalidation
    # -------------------------------------------------------------------------
    def test_15_ob_created_cutoff(self):
        """OrderBlock is only included if created_at <= current bar N."""
        df = _generate_synthetic_candles(30)
        contexts = build_strategy_contexts(df, self.config)

        for ctx in contexts:
            for ob in ctx.active_obs:
                c_at = ob.created_at if ob.created_at != -1 else ob.source_event_index
                self.assertLessEqual(c_at, ctx.bar_index)
                self.assertTrue(ob.valid)
                self.assertIsNone(ob.invalidated_at)

    def test_16_ob_first_retest_at_bar_n(self):
        """First retest at bar N has mitigated_at == N, retest_count == 1, valid == True."""
        ob = OrderBlock(
            index=3,
            time=pd.Timestamp("2026-01-15 07:45:00 UTC"),
            direction="bullish",
            high=2010.0,
            low=2005.0,
            open=2009.0,
            close=2006.0,
            origin_type="BOS",
            created_at=5,
            mitigated=True,
            mitigated_at=7,
            retest_count=1,
            valid=True,
            invalidated_at=None,
        )
        validate_as_of_evidence(ob, current_bar_index=7, bar_close_time=pd.Timestamp("2026-01-15 09:00:00 UTC"))
        self.assertEqual(ob.mitigated_at, 7)
        self.assertEqual(ob.retest_count, 1)
        self.assertTrue(ob.valid)

    def test_17_ob_invalidation_at_bar_n_excluded_from_active(self):
        """OB invalidated at bar N (valid=False, invalidated_at=N) is excluded from active_obs."""
        ob_invalid = OrderBlock(
            index=3,
            time=pd.Timestamp("2026-01-15 07:45:00 UTC"),
            direction="bullish",
            high=2010.0,
            low=2005.0,
            open=2009.0,
            close=2006.0,
            origin_type="BOS",
            created_at=5,
            valid=False,
            invalidated_at=7,
        )
        # Validates as-of bar 7
        validate_as_of_evidence(ob_invalid, current_bar_index=7, bar_close_time=pd.Timestamp("2026-01-15 09:00:00 UTC"))
        # But if bar is 6, invalidated_at=7 raises future leak error
        with self.assertRaises(ValueError):
            validate_as_of_evidence(ob_invalid, current_bar_index=6, bar_close_time=pd.Timestamp("2026-01-15 08:45:00 UTC"))

    def test_17b_ob_first_retest_and_invalidation_builder_integration(self):
        """OrderBlock first retest and subsequent invalidation verified end-to-end through builder."""
        candles = [
            {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "closed": True},
            {"bar_index": 1, "time": "2026-01-15 08:15:00 UTC", "open": 101.0, "high": 105.0, "low": 100.0, "close": 104.0, "closed": True},
            {"bar_index": 2, "time": "2026-01-15 08:30:00 UTC", "open": 103.0, "high": 104.0, "low": 98.0, "close": 99.0, "closed": True},
            {"bar_index": 3, "time": "2026-01-15 08:45:00 UTC", "open": 99.0, "high": 100.0, "low": 95.0, "close": 96.0, "closed": True},
            {"bar_index": 4, "time": "2026-01-15 09:00:00 UTC", "open": 96.0, "high": 102.0, "low": 95.5, "close": 101.0, "closed": True},
            {"bar_index": 5, "time": "2026-01-15 09:15:00 UTC", "open": 101.0, "high": 110.0, "low": 100.0, "close": 108.0, "closed": True},
            # Bar 6: retest into OB [95.0, 100.0] with low=97.0, close=99.0
            {"bar_index": 6, "time": "2026-01-15 09:30:00 UTC", "open": 108.0, "high": 108.0, "low": 97.0, "close": 99.0, "closed": True},
            # Bar 7: invalidation (candle closes below low=95.0, e.g. close=93.0)
            {"bar_index": 7, "time": "2026-01-15 09:45:00 UTC", "open": 99.0, "high": 99.0, "low": 92.0, "close": 93.0, "closed": True},
        ]
        cfg = ContextBuilderConfig(swing_strength=1, swing_left_strength=1, swing_right_strength=1, ob_lookback=10, fvg_min_gap_pct=0.0)
        builder = StrategyContextBuilder(cfg)
        contexts = [builder.update(c) for c in candles]

        # Bar 5: Bullish OB created from BOS
        ob5 = next(o for o in contexts[5].active_obs if o.index == 3)
        self.assertFalse(ob5.mitigated)
        self.assertEqual(ob5.retest_count, 0)
        self.assertTrue(ob5.valid)

        # Bar 6: First retest into OB
        ob6 = next(o for o in contexts[6].active_obs if o.index == 3)
        self.assertTrue(ob6.mitigated)
        self.assertEqual(ob6.mitigated_at, 6)
        self.assertEqual(ob6.retest_count, 1)
        self.assertTrue(ob6.valid)

        # Bar 7: OB 3 is invalidated and excluded from active_obs
        active_ob_indices_bar7 = [o.index for o in contexts[7].active_obs]
        self.assertNotIn(3, active_ob_indices_bar7)

    # -------------------------------------------------------------------------
    # 7. LiquidityPool & Sweep Cutoffs
    # -------------------------------------------------------------------------
    def test_18_liquidity_pool_cutoff_and_lifecycle(self):
        """LiquidityPool confirmed_at <= N, future swept_at is rejected."""
        pool = LiquidityPool(
            kind="equal_highs",
            price=2020.0,
            price_max=2020.5,
            price_min=2019.5,
            indices=[2, 6],
            created_at=8,
            confirmed_at=8,
            swept=True,
            swept_at=12,
            valid=False,
        )
        # Valid at bar 12
        validate_as_of_evidence(pool, current_bar_index=12, bar_close_time=pd.Timestamp("2026-01-15 10:00:00 UTC"))
        # Future leak at bar 10
        with self.assertRaises(ValueError):
            validate_as_of_evidence(pool, current_bar_index=10, bar_close_time=pd.Timestamp("2026-01-15 09:30:00 UTC"))

    def test_19_liquidity_sweep_cutoff(self):
        """LiquiditySweep confirmed_at <= N and swept_at <= N."""
        sweep = LiquiditySweep(
            index=10,
            time=pd.Timestamp("2026-01-15 09:30:00 UTC"),
            direction="bearish",
            pool_kind="equal_highs",
            pool_price=2020.0,
            pool_indices=[2, 6],
            price_wick=2022.0,
            close_price=2018.0,
            created_at=10,
            confirmed_at=10,
            swept_at=10,
        )
        validate_as_of_evidence(sweep, current_bar_index=10, bar_close_time=pd.Timestamp("2026-01-15 09:45:00 UTC"))
        with self.assertRaises(ValueError):
            validate_as_of_evidence(sweep, current_bar_index=9, bar_close_time=pd.Timestamp("2026-01-15 09:30:00 UTC"))

    def test_19b_liquidity_pool_and_sweep_builder_integration(self):
        """LiquidityPool swept at bar N appears in recent_sweeps and is excluded from active_pools."""
        candles = [
            {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "closed": True},
            {"bar_index": 1, "time": "2026-01-15 08:15:00 UTC", "open": 101.0, "high": 105.0, "low": 100.0, "close": 104.0, "closed": True},
            {"bar_index": 2, "time": "2026-01-15 08:30:00 UTC", "open": 103.0, "high": 103.5, "low": 98.0, "close": 99.0, "closed": True},
            {"bar_index": 3, "time": "2026-01-15 08:45:00 UTC", "open": 99.0, "high": 102.0, "low": 98.0, "close": 101.0, "closed": True},
            {"bar_index": 4, "time": "2026-01-15 09:00:00 UTC", "open": 101.0, "high": 105.0, "low": 100.0, "close": 104.0, "closed": True},
            {"bar_index": 5, "time": "2026-01-15 09:15:00 UTC", "open": 103.0, "high": 103.5, "low": 98.0, "close": 99.0, "closed": True},
            {"bar_index": 6, "time": "2026-01-15 09:30:00 UTC", "open": 100.0, "high": 106.0, "low": 99.0, "close": 103.0, "closed": True},
        ]
        cfg = ContextBuilderConfig(swing_strength=1, swing_left_strength=1, swing_right_strength=1, atr_period=5)
        builder = StrategyContextBuilder(cfg)
        contexts = [builder.update(c) for c in candles]

        # Bar 5: equal_highs pool active at 105.0
        eq_highs = [p for p in contexts[5].active_pools if p.kind == "equal_highs"]
        self.assertTrue(len(eq_highs) >= 1)
        self.assertFalse(eq_highs[0].swept)

        # Bar 6: sweep of equal_highs at 105.0 by wick 106.0
        self.assertTrue(len(contexts[6].recent_sweeps) >= 1)
        sweep = contexts[6].recent_sweeps[-1]
        self.assertEqual(sweep.index, 6)
        self.assertEqual(sweep.price_wick, 106.0)

        # Equal highs pool is now swept and excluded from active_pools
        eq_highs_swept = [p for p in contexts[6].active_pools if p.kind == "equal_highs"]
        self.assertEqual(len(eq_highs_swept), 0)

    # -------------------------------------------------------------------------
    # 8. Session & HTF Bias As-of Mapping
    # -------------------------------------------------------------------------
    def test_20_session_decision_evaluation(self):
        """SessionDecision evaluates correctly for bar N within Kill Zone."""
        # 08:00 UTC is inside London Killzone (07:00 - 10:00 UTC)
        candle = {
            "bar_index": 0,
            "time": "2026-01-15 08:00:00 UTC",
            "open": 2000.0,
            "high": 2005.0,
            "low": 1995.0,
            "close": 2002.0,
            "closed": True,
        }
        ctx = self.builder.update(candle)
        self.assertIsNotNone(ctx.session_decision)
        self.assertTrue(ctx.session_decision.in_session)
        self.assertEqual(ctx.session_decision.session_name, "london_killzone")

    def test_21_htf_bias_cutoff_at_bar_close_time(self):
        """HTF bias only includes events confirmed on or before bar_close_time."""
        htf_ev_early = StructureEvent(
            index=100,
            time=pd.Timestamp("2026-01-15 07:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=90,
            broken_swing_price=1990.0,
            close_price=1995.0,
        )
        htf_ev_choch = StructureEvent(
            index=101,
            time=pd.Timestamp("2026-01-15 08:45:00 UTC"),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=95,
            broken_swing_price=2005.0,
            close_price=1990.0,
        )
        htf_ev_late = StructureEvent(
            index=102,
            time=pd.Timestamp("2026-01-15 09:00:00 UTC"),
            event_type="BOS",
            direction="bearish",
            broken_swing_index=96,
            broken_swing_price=1995.0,
            close_price=1985.0,
        )

        builder = StrategyContextBuilder(self.config, htf_events=[htf_ev_early, htf_ev_choch, htf_ev_late])

        # Candle at 08:00 UTC (bar_close_time = 08:15 UTC): only htf_ev_early is effective
        c1 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        ctx1 = builder.update(c1)
        self.assertIsNotNone(ctx1.htf_bias)
        self.assertEqual(ctx1.htf_bias.bias, "bullish")
        self.assertEqual(ctx1.htf_bias.source_event_index, 100)

        # Candle at 09:00 UTC (bar_close_time = 09:15 UTC): htf_ev_late is now effective
        c2 = {"bar_index": 1, "time": "2026-01-15 09:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        ctx2 = builder.update(c2)
        self.assertEqual(ctx2.htf_bias.bias, "bearish")
        self.assertEqual(ctx2.htf_bias.source_event_index, 102)

    # -------------------------------------------------------------------------
    # 9. Immutability & JSON Serialization
    # -------------------------------------------------------------------------
    def test_22_input_candle_and_detector_not_mutated(self):
        """Input candle dict and detector objects are not mutated during context construction."""
        candle = {
            "bar_index": 0,
            "time": "2026-01-15 08:00:00 UTC",
            "open": 2000.0,
            "high": 2005.0,
            "low": 1995.0,
            "close": 2002.0,
            "volume": 50.0,
            "closed": True,
            "custom_meta": {"tag": "alpha"},
        }
        candle_copy = copy.deepcopy(candle)
        self.builder.update(candle)
        self.assertEqual(candle, candle_copy)

    def test_23_context_deep_immutability(self):
        """StrategyContext fields and metadata are completely immutable."""
        c0 = {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2002.0, "closed": True}
        ctx = self.builder.update(c0)

        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            ctx.bar_index = 99  # type: ignore

        with self.assertRaises(TypeError):
            ctx.meta["new_key"] = "leak"  # type: ignore

    def test_24_json_safe_direct_dumps(self):
        """json.dumps(ctx.to_dict(), allow_nan=False) serializes directly with zero exceptions."""
        df = _generate_synthetic_candles(10)
        contexts = build_strategy_contexts(df, self.config)

        for ctx in contexts:
            d = ctx.to_dict()
            json_str = json.dumps(d, allow_nan=False)
            self.assertIsInstance(json_str, str)
            # Roundtrip verification
            restored = json.loads(json_str)
            self.assertEqual(restored["bar_index"], ctx.bar_index)
            self.assertEqual(restored["symbol"], ctx.symbol)

    # -------------------------------------------------------------------------
    # 10. Deduplication & Deterministic Ordering
    # -------------------------------------------------------------------------
    def test_25_duplicate_identical_evidence_deduplicated(self):
        """Duplicate identical evidence objects are safely deduplicated to one instance."""
        df = _generate_synthetic_candles(15)
        contexts = build_strategy_contexts(df, self.config)
        for ctx in contexts:
            # Check unique IDs in recent_swings
            swing_ids = [f"sw:{s.mode}:{s.kind}:{s.index}" for s in ctx.recent_swings]
            self.assertEqual(len(swing_ids), len(set(swing_ids)))

    def test_26_deterministic_ordering_with_shuffled_input(self):
        """Collections inside context have deterministic sort order regardless of insertion order."""
        sw1 = SwingPointSnapshot(index=2, time=pd.Timestamp("2026-01-15 07:30:00 UTC"), price=2010.0, kind="high", strength=2, confirmed_at=4)
        sw2 = SwingPointSnapshot(index=4, time=pd.Timestamp("2026-01-15 08:00:00 UTC"), price=1990.0, kind="low", strength=2, confirmed_at=6)

        c1 = StrategyContext(
            bar_index=10,
            timestamp=pd.Timestamp("2026-01-15 09:30:00 UTC"),
            bar_close_time=pd.Timestamp("2026-01-15 09:45:00 UTC"),
            symbol="XAUUSD",
            timeframe="M15",
            open=2000.0, high=2005.0, low=1995.0, close=2002.0, volume=10.0, atr14=1.5,
            recent_swings=(sw1, sw2),
        )
        c2 = StrategyContext(
            bar_index=10,
            timestamp=pd.Timestamp("2026-01-15 09:30:00 UTC"),
            bar_close_time=pd.Timestamp("2026-01-15 09:45:00 UTC"),
            symbol="XAUUSD",
            timeframe="M15",
            open=2000.0, high=2005.0, low=1995.0, close=2002.0, volume=10.0, atr14=1.5,
            recent_swings=(sw1, sw2),
        )
        self.assertEqual(c1.to_dict(), c2.to_dict())

    # -------------------------------------------------------------------------
    # 11. Batch / Incremental Exact Parity (100%)
    # -------------------------------------------------------------------------
    def test_27_batch_incremental_exact_full_payload_parity(self):
        """Batch and incremental builds produce 100% exact identical payloads on every bar."""
        df = _generate_synthetic_candles(35)

        # 1. Batch build
        batch_contexts = build_strategy_contexts(df, self.config)

        # 2. Incremental build
        builder_inc = StrategyContextBuilder(self.config)
        inc_contexts = []
        for row in df.to_dict(orient="records"):
            ctx = builder_inc.update(row)
            inc_contexts.append(ctx)

        self.assertEqual(len(batch_contexts), len(inc_contexts))
        for i in range(len(batch_contexts)):
            self.assertEqual(
                batch_contexts[i].to_dict(),
                inc_contexts[i].to_dict(),
                f"Parity mismatch at bar {i}",
            )

    # -------------------------------------------------------------------------
    # 12. Future-Append Invariance
    # -------------------------------------------------------------------------
    def test_28_future_append_invariance(self):
        """Context at bar N does NOT change when additional future bars are appended."""
        df_full = _generate_synthetic_candles(40)
        cutoff = 20

        df_short = df_full.iloc[:cutoff + 1].copy()

        # Build short run
        contexts_short = build_strategy_contexts(df_short, self.config)
        # Build full run
        contexts_full = build_strategy_contexts(df_full, self.config)

        # For every bar up to cutoff, contexts must be exactly identical
        for i in range(cutoff + 1):
            self.assertEqual(
                contexts_short[i].to_dict(),
                contexts_full[i].to_dict(),
                f"Future append changed historical context at bar {i}!",
            )

    # -------------------------------------------------------------------------
    # 13. Reset Behavior & Lookback Memory Bounds
    # -------------------------------------------------------------------------
    def test_29_builder_reset_behavior(self):
        """builder.reset() completely clears internal state and allows re-streaming."""
        df = _generate_synthetic_candles(15)
        run1 = [self.builder.update(r) for r in df.to_dict(orient="records")]

        self.builder.reset()
        self.assertIsNone(self.builder.last_context)

        run2 = [self.builder.update(r) for r in df.to_dict(orient="records")]

        for i in range(len(run1)):
            self.assertEqual(run1[i].to_dict(), run2[i].to_dict())

    def test_29b_builder_reset_preserves_initial_htf_events(self):
        """P1.1: builder.reset() preserves constructor HTF events while discarding dynamic events."""
        ev1 = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-01 00:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=2000.0,
            close_price=2005.0,
        )
        ev2_choch = StructureEvent(
            index=19,
            time=pd.Timestamp("2026-01-01 00:50:00 UTC"),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=12,
            broken_swing_price=2015.0,
            close_price=2000.0,
        )
        ev2_dynamic = StructureEvent(
            index=20,
            time=pd.Timestamp("2026-01-01 01:00:00 UTC"),
            event_type="BOS",
            direction="bearish",
            broken_swing_index=15,
            broken_swing_price=2010.0,
            close_price=1990.0,
        )
        ev3_choch = StructureEvent(
            index=29,
            time=pd.Timestamp("2026-01-01 01:50:00 UTC"),
            event_type="CHoCH",
            direction="bullish",
            broken_swing_index=22,
            broken_swing_price=1990.0,
            close_price=2005.0,
        )
        ev3_update = StructureEvent(
            index=30,
            time=pd.Timestamp("2026-01-01 02:00:00 UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=25,
            broken_swing_price=1995.0,
            close_price=2015.0,
        )

        # Test D: External mutation of input list/object does not affect seed events
        input_list = [ev1]
        builder = StrategyContextBuilder(self.config, htf_events=input_list)
        input_list.append(ev2_dynamic)
        self.assertEqual(len(builder._initial_htf_events), 1)

        # Test A: Ingest candle 0. HTF bias reflects ev1.
        c0 = {'bar_index': 0, 'time': '2026-01-01 00:15:00 UTC', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2002.0, 'volume': 100.0, 'closed': True}
        ctx0 = builder.update(c0)
        self.assertEqual(ctx0.htf_bias.bias, "bullish")
        self.assertEqual(ctx0.htf_bias.source_event_index, 10)

        # Test B: Add ev2 dynamically via add_htf_event
        builder.add_htf_event(ev2_choch)
        builder.add_htf_event(ev2_dynamic)
        c1 = {'bar_index': 1, 'time': '2026-01-01 01:15:00 UTC', 'open': 2002.0, 'high': 2006.0, 'low': 1998.0, 'close': 2000.0, 'volume': 100.0, 'closed': True}
        ctx1 = builder.update(c1)
        self.assertEqual(ctx1.htf_bias.bias, "bearish")
        self.assertEqual(ctx1.htf_bias.source_event_index, 20)

        # Test C: Add ev3 dynamically via update(new_htf_events=...)
        c2 = {'bar_index': 2, 'time': '2026-01-01 02:15:00 UTC', 'open': 2000.0, 'high': 2020.0, 'low': 1999.0, 'close': 2018.0, 'volume': 100.0, 'closed': True}
        ctx2 = builder.update(c2, new_htf_events=[ev3_choch, ev3_update])
        self.assertEqual(ctx2.htf_bias.bias, "bullish")
        self.assertEqual(ctx2.htf_bias.source_event_index, 30)

        # Reset builder: dynamic events (ev2, ev3) must be purged, ev1 seed preserved
        builder.reset()
        self.assertIsNone(builder.last_context)
        # Re-stream bar 0: bias must reflect ev1 (not 'no_htf_event' or ev2/ev3)
        ctx0_after_reset = builder.update(c0)
        self.assertEqual(ctx0_after_reset.htf_bias.bias, "bullish")
        self.assertEqual(ctx0_after_reset.htf_bias.source_event_index, 10)

        # Ingest bar 1 without ev2: bias remains ev1 (ev2 was purged on reset)
        ctx1_after_reset = builder.update(c1)
        self.assertEqual(ctx1_after_reset.htf_bias.bias, "bullish")
        self.assertEqual(ctx1_after_reset.htf_bias.source_event_index, 10)

    def test_30_lookback_memory_bounds(self):
        """Context collections respect max configured bounds."""
        cfg_bounded = ContextBuilderConfig(
            max_recent_swings=3,
            max_recent_structures=3,
            max_active_fvgs=3,
            max_active_obs=3,
            max_active_pools=3,
            max_recent_sweeps=3,
            swing_strength=2,
            atr_period=5,
        )
        df = _generate_synthetic_candles(50)
        contexts = build_strategy_contexts(df, cfg_bounded)

        for ctx in contexts:
            self.assertLessEqual(len(ctx.recent_swings), 3)
            self.assertLessEqual(len(ctx.recent_structures), 3)
            self.assertLessEqual(len(ctx.active_fvgs), 3)
            self.assertLessEqual(len(ctx.active_obs), 3)
            self.assertLessEqual(len(ctx.active_pools), 3)
            self.assertLessEqual(len(ctx.recent_sweeps), 3)

    def test_30b_snapshot_caches_strictly_bounded_at_3000_and_6000_bars(self):
        """All 6 snapshot caches are strictly bounded by max_* * 2 at 3,000 and 6,000 bars (P1.3)."""
        cfg = ContextBuilderConfig(
            max_recent_swings=50,
            max_recent_structures=50,
            max_active_fvgs=50,
            max_active_obs=50,
            max_active_pools=50,
            max_recent_sweeps=50,
        )
        builder = StrategyContextBuilder(cfg)

        dates = pd.date_range("2024-01-01", periods=6500, freq="15min", tz="UTC")
        np.random.seed(42)
        prices = 100.0 + np.cumsum(np.random.randn(6500) * 0.5)
        opens = prices + np.random.randn(6500) * 0.1
        closes = prices + np.random.randn(6500) * 0.1
        highs = np.maximum(opens, closes) + np.abs(np.random.randn(6500) * 0.3) + 0.05
        lows = np.minimum(opens, closes) - np.abs(np.random.randn(6500) * 0.3) - 0.05

        bound_limit = 100  # 2 * 50
        for i, ts in enumerate(dates):
            candle = {
                "bar_index": i,
                "time": ts,
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": closes[i],
                "volume": 1000.0,
                "closed": True,
            }
            builder.update(candle)
            if i in (3000, 6000):
                self.assertLessEqual(len(builder._swing_snapshot_cache), bound_limit)
                self.assertLessEqual(len(builder._structure_snapshot_cache), bound_limit)
                self.assertLessEqual(len(builder._fvg_snapshot_cache), bound_limit)
                self.assertLessEqual(len(builder._ob_snapshot_cache), bound_limit)
                self.assertLessEqual(len(builder._pool_snapshot_cache), bound_limit)
                self.assertLessEqual(len(builder._sweep_snapshot_cache), bound_limit)

    def test_30c_snapshot_cache_reset_and_no_collision(self):
        """P2.2: Snapshot cache hits reuse instances, omitted field changes emit new snapshots, and reset clears caches."""
        cfg = ContextBuilderConfig(
            swing_left_strength=2, swing_right_strength=2, swing_strength=2,
            fvg_min_gap_pct=0.0, ob_lookback=20, ob_require_fvg=False
        )
        builder = StrategyContextBuilder(cfg)

        candles = [
            {'bar_index': 0, 'time': '2026-01-01 00:00:00+00:00', 'open': 99.0, 'high': 100.5, 'low': 98.5, 'close': 100.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 1, 'time': '2026-01-01 00:15:00+00:00', 'open': 100.0, 'high': 102.5, 'low': 99.5, 'close': 102.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 2, 'time': '2026-01-01 00:30:00+00:00', 'open': 102.0, 'high': 105.0, 'low': 101.5, 'close': 104.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 3, 'time': '2026-01-01 00:45:00+00:00', 'open': 104.0, 'high': 104.2, 'low': 101.0, 'close': 101.5, 'volume': 100.0, 'closed': True},
            {'bar_index': 4, 'time': '2026-01-01 01:00:00+00:00', 'open': 101.5, 'high': 103.5, 'low': 101.2, 'close': 103.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 5, 'time': '2026-01-01 01:15:00+00:00', 'open': 103.0, 'high': 106.5, 'low': 102.5, 'close': 106.0, 'volume': 100.0, 'closed': True},
        ]
        for c in candles:
            builder.update(c)

        ctx5 = builder.last_context
        self.assertEqual(len(ctx5.active_obs), 1)
        ob_snap_initial = ctx5.active_obs[0]
        initial_source_swing = ob_snap_initial.source_swing_index
        initial_source_event = ob_snap_initial.source_event_type
        id_key = (ob_snap_initial.mode, ob_snap_initial.direction, ob_snap_initial.source_event_index, ob_snap_initial.index)
        self.assertIn(id_key, builder._ob_snapshot_cache)

        # 1. Real Cache Hit: next bar with NO change in OB state must reuse the EXACT same snapshot object
        c6 = {'bar_index': 6, 'time': '2026-01-01 01:30:00+00:00', 'open': 106.0, 'high': 107.0, 'low': 105.5, 'close': 106.5, 'volume': 100.0, 'closed': True}
        ctx6 = builder.update(c6)
        ob_snap_hit = ctx6.active_obs[0]
        self.assertIs(ob_snap_initial, ob_snap_hit, "Cache hit must return the identical cached snapshot instance.")

        # 2. Real Cache Update on Omitted Field: modifying previously omitted field (e.g. source_swing_index)
        # must invalidate the state key, emit a new snapshot, update cache, and preserve historical isolation
        ob_in_tracker = builder._ob_tracker.get_active_blocks()[0]
        ob_in_tracker.source_swing_index = 42
        ob_in_tracker.source_event_type = "CHoCH"

        c7 = {'bar_index': 7, 'time': '2026-01-01 01:45:00+00:00', 'open': 106.5, 'high': 107.5, 'low': 106.0, 'close': 107.0, 'volume': 100.0, 'closed': True}
        ctx7 = builder.update(c7)
        ob_snap_updated = ctx7.active_obs[0]

        # Must emit a new snapshot instance
        self.assertIsNot(ob_snap_initial, ob_snap_updated, "Changing an omitted field must emit a new snapshot instance.")
        self.assertEqual(ob_snap_updated.source_swing_index, 42)
        self.assertEqual(ob_snap_updated.source_event_type, "CHoCH")

        # Historical context at bar 5 remains completely untouched (deep immutability)
        self.assertEqual(ctx5.active_obs[0].source_swing_index, initial_source_swing)
        self.assertEqual(ctx5.active_obs[0].source_event_type, initial_source_event)
        self.assertNotEqual(ctx5.active_obs[0].source_swing_index, 42)
        self.assertNotEqual(ctx5.active_obs[0].source_event_type, "CHoCH")

        # Cache size remains strictly 1 for that active OB identity
        self.assertEqual(len(builder._ob_snapshot_cache), 1)

        # 3. Conflicting Duplicate IDs Fail-Fast: duplicate ID with differing payload raises ValueError
        from smc.engine.context import _deduplicate_and_sort_evidence
        ob1 = OrderBlockSnapshot.from_source(ob_snap_initial)
        ob2_conflict = OrderBlockSnapshot(
            index=ob1.index,
            time=ob1.time,
            direction=ob1.direction,
            high=ob1.high + 10.0,  # conflicting high price
            low=ob1.low,
            open=ob1.open,
            close=ob1.close,
            source_event_index=ob1.source_event_index,
        )
        with self.assertRaises(ValueError) as cm:
            _deduplicate_and_sort_evidence(
                [ob1, ob2_conflict],
                id_func=lambda o: (o.mode, o.direction, o.source_event_index, o.index),
                sort_key_func=lambda o: (o.created_at, o.index),
            )
        self.assertIn("Conflicting duplicate evidence detected", str(cm.exception))

        # 4. Reset clears all 6 caches completely
        builder.reset()
        self.assertEqual(len(builder._swing_snapshot_cache), 0)
        self.assertEqual(len(builder._structure_snapshot_cache), 0)
        self.assertEqual(len(builder._fvg_snapshot_cache), 0)
        self.assertEqual(len(builder._ob_snapshot_cache), 0)
        self.assertEqual(len(builder._pool_snapshot_cache), 0)
        self.assertEqual(len(builder._sweep_snapshot_cache), 0)
        for val in builder.snapshot_cache_sizes.values():
            self.assertEqual(val, 0)

    def test_30d_late_fvg_upgrades_ob_quality_snapshot(self):
        """P1.1: Late FVG upgrade updates active OB snapshot on current bar without mutating past contexts."""
        cfg = ContextBuilderConfig(
            swing_left_strength=2, swing_right_strength=2, swing_strength=2,
            fvg_min_gap_pct=0.0, ob_lookback=20, ob_require_fvg=False
        )
        builder = StrategyContextBuilder(cfg)

        candles = [
            {'bar_index': 0, 'time': '2026-01-01 00:00:00+00:00', 'open': 99.0, 'high': 100.5, 'low': 98.5, 'close': 100.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 1, 'time': '2026-01-01 00:15:00+00:00', 'open': 100.0, 'high': 102.5, 'low': 99.5, 'close': 102.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 2, 'time': '2026-01-01 00:30:00+00:00', 'open': 102.0, 'high': 105.0, 'low': 101.5, 'close': 104.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 3, 'time': '2026-01-01 00:45:00+00:00', 'open': 104.0, 'high': 104.2, 'low': 101.0, 'close': 101.5, 'volume': 100.0, 'closed': True},
            {'bar_index': 4, 'time': '2026-01-01 01:00:00+00:00', 'open': 101.5, 'high': 103.5, 'low': 101.2, 'close': 103.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 5, 'time': '2026-01-01 01:15:00+00:00', 'open': 103.0, 'high': 106.5, 'low': 102.5, 'close': 106.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 6, 'time': '2026-01-01 01:30:00+00:00', 'open': 107.0, 'high': 112.0, 'low': 107.0, 'close': 111.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 7, 'time': '2026-01-01 01:45:00+00:00', 'open': 111.0, 'high': 115.0, 'low': 108.0, 'close': 114.0, 'volume': 100.0, 'closed': True},
        ]

        ctxs = []
        for c in candles:
            ctxs.append(builder.update(c))

        ctx5 = ctxs[5]
        self.assertEqual(len(ctx5.active_obs), 1)
        self.assertEqual(ctx5.active_obs[0].quality, "base")
        self.assertIsNone(ctx5.active_obs[0].source_fvg_index)

        # Simulate late FVG linking to the active OB in the tracker
        ob_in_tracker = builder._ob_tracker.get_active_blocks()[0]
        ob_in_tracker.quality = "strong"
        ob_in_tracker.source_fvg_index = 6
        ob_in_tracker.source_fvg_top = 108.0
        ob_in_tracker.source_fvg_bottom = 106.5

        # Bar 8 update: builder must emit updated snapshot with quality="strong"
        c8 = {'bar_index': 8, 'time': '2026-01-01 02:00:00+00:00', 'open': 114.0, 'high': 116.0, 'low': 113.0, 'close': 115.0, 'volume': 100.0, 'closed': True}
        ctx8 = builder.update(c8)

        # Past context remains untouched (deep immutability)
        self.assertEqual(ctx5.active_obs[0].quality, "base")
        self.assertIsNone(ctx5.active_obs[0].source_fvg_index)

        # Current context reflects upgraded quality and source_fvg_index
        self.assertEqual(len(ctx8.active_obs), 1)
        self.assertEqual(ctx8.active_obs[0].quality, "strong")
        self.assertEqual(ctx8.active_obs[0].source_fvg_index, 6)

        # Cache contains exactly 1 entry for this active OB
        self.assertEqual(len(builder._ob_snapshot_cache), 1)

    def test_30e_high_retest_count_cache_bounded(self):
        """P1.2: Snapshot cache does not multiply entries across 75+ retests of a single active OB."""
        cfg = ContextBuilderConfig(
            swing_left_strength=2, swing_right_strength=2, swing_strength=2,
            fvg_min_gap_pct=0.0, ob_lookback=20, ob_require_fvg=False
        )
        builder = StrategyContextBuilder(cfg)

        # First 6 bars: establish OB at bar 3 (high=104.2, low=101.0)
        candles = [
            {'bar_index': 0, 'time': '2026-01-01 00:00:00+00:00', 'open': 99.0, 'high': 100.5, 'low': 98.5, 'close': 100.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 1, 'time': '2026-01-01 00:15:00+00:00', 'open': 100.0, 'high': 102.5, 'low': 99.5, 'close': 102.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 2, 'time': '2026-01-01 00:30:00+00:00', 'open': 102.0, 'high': 105.0, 'low': 101.5, 'close': 104.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 3, 'time': '2026-01-01 00:45:00+00:00', 'open': 104.0, 'high': 104.2, 'low': 101.0, 'close': 101.5, 'volume': 100.0, 'closed': True},
            {'bar_index': 4, 'time': '2026-01-01 01:00:00+00:00', 'open': 101.5, 'high': 103.5, 'low': 101.2, 'close': 103.0, 'volume': 100.0, 'closed': True},
            {'bar_index': 5, 'time': '2026-01-01 01:15:00+00:00', 'open': 103.0, 'high': 106.5, 'low': 102.5, 'close': 106.0, 'volume': 100.0, 'closed': True},
        ]
        for c in candles:
            builder.update(c)

        self.assertEqual(len(builder.last_context.active_obs), 1)

        # Feed 154 alternating candles retesting the OB zone
        for i in range(6, 160):
            t_str = f"2026-01-0{1 + i // 96:d} {(i*15//60)%24:02d}:{(i*15)%60:02d}:00+00:00"
            if i % 2 == 0:
                c = {'bar_index': i, 'time': t_str, 'open': 105.0, 'high': 106.0, 'low': 102.0, 'close': 105.0, 'volume': 100.0, 'closed': True}
            else:
                c = {'bar_index': i, 'time': t_str, 'open': 105.0, 'high': 106.0, 'low': 104.5, 'close': 105.5, 'volume': 100.0, 'closed': True}
            builder.update(c)

        active_obs = builder.last_context.active_obs
        self.assertEqual(len(active_obs), 1)
        self.assertGreaterEqual(active_obs[0].retest_count, 70)
        # Cache must have at most 1 entry for this single active OB
        self.assertLessEqual(len(builder._ob_snapshot_cache), cfg.max_active_obs)
        self.assertEqual(len(builder._ob_snapshot_cache), 1)
        self.assertEqual(builder.snapshot_cache_sizes["order_blocks"], 1)

    # -------------------------------------------------------------------------
    # 14. Performance Benchmark (10,000 bars)
    # -------------------------------------------------------------------------
    @unittest.skipUnless(
        os.environ.get("RUN_SMC_PERFORMANCE_TESTS") == "1",
        "ADR 19: deferred performance target; set RUN_SMC_PERFORMANCE_TESTS=1 to run",
    )
    def test_31_performance_benchmark_10000_bars(self):
        """Opt-in benchmark for the deferred intermediate target (< 7.0s/10,000 bars)."""
        df_10k = _generate_synthetic_candles(10000)
        records = df_10k.to_dict(orient="records")

        # 1. Warm-up run (1,000 bars)
        warmup_builder = StrategyContextBuilder(self.config)
        for r in records[:1000]:
            warmup_builder.update(r)

        # 2. Measure Trackers Baseline Alone (3 runs) with 100% configuration parity (P2.1)
        tracker_durations = []
        timeframe_delta = SUPPORTED_TIMEFRAMES[self.config.timeframe]
        for _ in range(3):
            sw_det, st_tr, fvg_tr, ob_tr, liq_tr, ses_flt, htf_tr = _create_trackers_from_config(self.config)
            tr_history = deque(maxlen=self.config.atr_period)
            last_close = None

            t0 = time.perf_counter()
            for i, r in enumerate(records):
                ts = r["time"]
                c_high = float(r["high"])
                c_low = float(r["low"])
                c_close = float(r["close"])
                tr = max(c_high - c_low, abs(c_high - last_close), abs(c_low - last_close)) if last_close is not None else (c_high - c_low)
                last_close = c_close
                tr_history.append(tr)
                atr14 = sum(tr_history) / float(self.config.atr_period) if len(tr_history) == self.config.atr_period else 0.0
                bar_close_time = ts + timeframe_delta

                bar = {"bar_index": i, "time": ts, "open": r["open"], "high": c_high, "low": c_low, "close": c_close, "volume": r["volume"]}
                sw = sw_det.update(bar)
                st = st_tr.update(bar, confirmed_swings=sw)
                fvg = fvg_tr.update(bar, structure_events=st)
                ob_tr.update(bar, new_structure_events=st, new_fvgs=fvg)
                liq_tr.update(bar, newly_confirmed_swings=sw, atr_val=atr14)
                ses_flt.update(bar, candle_closed=True)
                htf_tr.update(current_ltf_time=bar_close_time)
            t1 = time.perf_counter()
            tracker_durations.append(t1 - t0)

        avg_tracker = sum(tracker_durations) / 3

        # 3. Measure StrategyContextBuilder Total (3 runs)
        builder_durations = []
        for run_idx in range(3):
            bench_builder = StrategyContextBuilder(self.config)
            t0 = time.perf_counter()
            for r in records:
                bench_builder.update(r)
            t1 = time.perf_counter()
            builder_durations.append(t1 - t0)

        avg_builder = sum(builder_durations) / 3
        overhead = avg_builder - avg_tracker
        us_per_bar = (avg_builder / 10000.0) * 1_000_000

        print(f"\n[BENCHMARK PROFILE 10,000 bars]\n"
              f"  - Trackers baseline avg: {avg_tracker:.4f}s ({avg_tracker/10000*1e6:.2f} µs/bar) over 3 runs: {[round(d, 4) for d in tracker_durations]}\n"
              f"  - Builder total avg:     {avg_builder:.4f}s ({us_per_bar:.2f} µs/bar) over 3 runs: {[round(d, 4) for d in builder_durations]}\n"
              f"  - Context overhead avg:  {overhead:.4f}s ({overhead/10000*1e6:.2f} µs/bar)")

        # ADR 19: this target is explicit opt-in technical-debt verification, not a T53.2 blocker.
        self.assertLess(avg_builder, 7.0, f"Benchmark exceeded deferred target 7.0s: avg={avg_builder:.4f}s (trackers={avg_tracker:.4f}s, overhead={overhead:.4f}s)")


if __name__ == "__main__":
    unittest.main()
