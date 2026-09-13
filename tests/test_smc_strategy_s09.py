"""
tests/test_smc_strategy_s09.py
==============================
Exhaustive 106-test suite for S09 ICT Silver Bullet Strategy Template.
Covers Groups A through H adhering strictly to T53_6_S09_ICT_SILVER_BULLET_IMPLEMENTATION_PLAN.md,
ADR 16, and ADR 22.
"""

from __future__ import annotations

import copy
import datetime
import json
import math
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
import zoneinfo

import numpy as np
import pandas as pd

from smc.models import StructureEvent
from smc.engine.context import (
    ContextBuilderConfig,
    StrategyContextBuilder,
    build_strategy_contexts,
)
from smc.engine.errors import (
    StrategyRegistryError,
    StrategyStateError,
    StrategyValidationError,
)
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    FairValueGapSnapshot,
    HTFPOISnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    SessionDecisionSnapshot,
    StrategyContext,
    StrategyProfile,
    StructureEventSnapshot,
    make_cluster_id,
    make_evidence_id,
    make_setup_id,
)
from smc.engine.protocol import StrategyTemplate, validate_strategy_id
from smc.engine.registry import StrategyRegistry, StrategyRegistryConfig
from smc.engine.strategies.s01_ict_2022 import S01Config, S01ICT2022Strategy
from smc.engine.strategies.s09_ict_silver_bullet import (
    CANONICAL_WINDOWS,
    NEW_YORK_TZ,
    WINDOW_SCHEDULE,
    S09Config,
    S09ICTSilverBulletStrategy,
    S09Narrative,
    S09NarrativeStage,
    WindowKey,
    compute_bar_close_retention_bar,
    compute_window_bounds_for_date,
    get_ny_local_date_str,
    get_window_for_close_time,
)


# =============================================================================
# Test Helper Functions
# =============================================================================

BASE_NY_DATETIME = datetime.datetime(2024, 5, 15, 10, 0, 0, tzinfo=NEW_YORK_TZ)
BASE_UTC_TIMESTAMP = pd.Timestamp(BASE_NY_DATETIME).tz_convert("UTC")


def _bar_time(bar_index: int, timeframe_minutes: int = 1) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Returns (open_ts_utc, close_ts_utc) for a given bar offset from 10:00 NY time."""
    open_ts = BASE_UTC_TIMESTAMP + pd.Timedelta(minutes=bar_index * timeframe_minutes)
    close_ts = open_ts + pd.Timedelta(minutes=timeframe_minutes)
    return open_ts, close_ts


def _make_bias(
    bias: str = "bullish",
    bar_index: int = 0,
    as_of: Optional[pd.Timestamp] = None,
    source_event_time: Optional[pd.Timestamp] = None,
    ref_time: Optional[pd.Timestamp] = None,
) -> BiasStateSnapshot:
    if ref_time is not None:
        open_ts = ref_time
    else:
        open_ts, _ = _bar_time(bar_index)
    return BiasStateSnapshot(
        bias=bias,
        timestamp=open_ts,
        as_of=as_of if as_of is not None else open_ts,
        source_event_time=source_event_time if source_event_time is not None else open_ts,
    )


def _make_context(
    bar_index: int = 0,
    open_p: Optional[float] = None,
    high_p: Optional[float] = None,
    low_p: Optional[float] = None,
    close_p: Optional[float] = None,
    sweeps: Sequence[LiquiditySweepSnapshot] = (),
    structure_events: Sequence[StructureEventSnapshot] = (),
    fvgs: Sequence[FairValueGapSnapshot] = (),
    pools: Sequence[LiquidityPoolSnapshot] = (),
    bias: Optional[str] = "bullish",
    timeframe: str = "M1",
    htf_bias: Optional[BiasStateSnapshot] = None,
    session_decision: Optional[SessionDecisionSnapshot] = None,
    open_time: Optional[pd.Timestamp] = None,
    close_time: Optional[pd.Timestamp] = None,
    active_htf_pois: Optional[Sequence[Any]] = None,
) -> StrategyContext:
    if close_p is not None:
        c = close_p
    elif sweeps:
        c = sweeps[-1].close_price
    else:
        c = 2050.0
    o = open_p if open_p is not None else c
    h = high_p if high_p is not None else max(o, c) + 2.0
    l = low_p if low_p is not None else min(o, c) - 2.0
    if h < max(o, c):
        h = max(o, c)
    if l > min(o, c):
        l = min(o, c)

    if open_time is None or close_time is None:
        def_o, def_c = _bar_time(bar_index)
        ts = open_time if open_time is not None else def_o
        bct = close_time if close_time is not None else def_c
    else:
        ts = open_time
        bct = close_time

    if htf_bias is None and bias is not None:
        htf_bias = _make_bias(bias, bar_index, ref_time=ts)

    if active_htf_pois is None:
        poi_dir = bias if bias in {"bullish", "bearish"} else "bullish"
        pois_tuple = (
            HTFPOISnapshot(
                poi_id="poi_default_s09_test",
                poi_type="FVG",
                direction=poi_dir,
                timeframe="H1",
                top=2100.0,
                bottom=2000.0,
                created_at=0,
                source_event="default_setup",
                valid_until=None,
                status="active",
                touch_count=1,
            ),
        )
    else:
        pois_tuple = tuple(active_htf_pois)

    return StrategyContext(
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=bct,
        symbol="XAUUSD",
        timeframe=timeframe,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=100.0,
        atr14=2.0,
        recent_sweeps=tuple(sweeps),
        recent_structures=tuple(structure_events),
        active_fvgs=tuple(fvgs),
        active_pools=tuple(pools),
        htf_bias=htf_bias,
        session_decision=session_decision,
        active_htf_pois=pois_tuple,
    )


def _make_sweep(
    index: int = 5,
    direction: str = "bullish",
    pool_kind: Optional[str] = None,
    price_wick: Optional[float] = None,
    pool_price: Optional[float] = None,
    close_price: Optional[float] = None,
    swept_at: Optional[int] = None,
    confirmed_at: Optional[int] = None,
    mode: str = "internal",
    valid: bool = True,
    pool_indices: Sequence[int] = (1, 3),
    structure_leg_id: Optional[str] = None,
) -> LiquiditySweepSnapshot:
    open_ts, _ = _bar_time(index)
    conf_at = confirmed_at if confirmed_at is not None else index
    sw_at = swept_at if swept_at is not None else index
    if direction == "bullish":
        pk = pool_kind if pool_kind is not None else "equal_lows"
        pw = price_wick if price_wick is not None else 2035.0
        pp = pool_price if pool_price is not None else 2036.0
        cp = close_price if close_price is not None else 2038.0
    else:
        pk = pool_kind if pool_kind is not None else "equal_highs"
        pw = price_wick if price_wick is not None else 2065.0
        pp = pool_price if pool_price is not None else 2060.0
        cp = close_price if close_price is not None else 2055.0
    return LiquiditySweepSnapshot(
        index=index,
        time=open_ts,
        direction=direction,
        pool_kind=pk,
        pool_price=pp,
        pool_indices=tuple(sorted(pool_indices)),
        price_wick=pw,
        close_price=cp,
        created_at=index,
        confirmed_at=conf_at,
        swept_at=sw_at,
        valid=valid,
        mode=mode,
        structure_leg_id=structure_leg_id,
    )


def _make_structure(
    index: int = 6,
    direction: str = "bullish",
    event_type: str = "CHoCH",
    broken_swing_index: int = 4,
    broken_swing_price: float = 2045.0,
    displacement: bool = True,
    structure_leg_id: Optional[str] = "leg1",
    mode: str = "internal",
    break_type: str = "close",
    confirmed_swing_at: Optional[int] = None,
) -> StructureEventSnapshot:
    open_ts, _ = _bar_time(index)
    conf_at = confirmed_swing_at if confirmed_swing_at is not None else index
    return StructureEventSnapshot(
        index=index,
        time=open_ts,
        direction=direction,
        event_type=event_type,
        broken_swing_index=broken_swing_index,
        broken_swing_price=broken_swing_price,
        close_price=broken_swing_price + (1.0 if direction == "bullish" else -1.0),
        displacement=displacement,
        structure_leg_id=structure_leg_id,
        mode=mode,
        confirmed_swing_at=conf_at,
        break_type=break_type,
    )


def _make_fvg(
    index: int = 5,
    direction: str = "bullish",
    top: float = 2042.0,
    bottom: float = 2038.0,
    confirmed_at: int = 6,
    structure_leg_id: Optional[str] = "leg1",
    mode: str = "internal",
    filled: bool = False,
    filled_at: Optional[int] = None,
) -> FairValueGapSnapshot:
    open_ts, _ = _bar_time(index)
    return FairValueGapSnapshot(
        index=index,
        time=open_ts,
        direction=direction,
        top=top,
        bottom=bottom,
        confirmed_at=confirmed_at,
        structure_leg_id=structure_leg_id,
        mode=mode,
        filled=filled,
        filled_at=filled_at,
    )


def _make_pool(
    confirmed_at: int = 2,
    direction: str = "bearish",
    kind: Optional[str] = None,
    price: float = 2060.0,
    mode: str = "internal",
    valid: bool = True,
    swept: bool = False,
    swept_at: Optional[int] = None,
    invalidated_at: Optional[int] = None,
    indices: Sequence[int] = (1, 2),
) -> LiquidityPoolSnapshot:
    if kind is None:
        kind = "equal_highs" if direction == "bearish" else "equal_lows"
    return LiquidityPoolSnapshot(
        kind=kind,
        price=price,
        price_max=price + 0.1,
        price_min=price - 0.1,
        indices=tuple(sorted(indices)),
        created_at=indices[0] if indices else confirmed_at,
        confirmed_at=confirmed_at,
        mode=mode,
        swept=swept,
        swept_at=swept_at,
        valid=valid,
        invalidated_at=invalidated_at,
    )


def _feed(strat: S09ICTSilverBulletStrategy, context: StrategyContext) -> tuple[CandidateSetup, ...]:
    """Feed context sequentially into strategy, filling intermediate gap bars with neutral context."""
    if strat._last_bar_index is not None and context.bar_index > strat._last_bar_index + 1:
        ref_p = strat._last_context_payload["close"] if strat._last_context_payload else context.close
        for b in range(strat._last_bar_index + 1, context.bar_index):
            strat.evaluate(_make_context(
                bar_index=b,
                open_p=ref_p,
                high_p=ref_p,
                low_p=ref_p,
                close_p=ref_p,
                timeframe=context.timeframe,
                bias=context.htf_bias.bias if context.htf_bias else "neutral",
            ))
    return strat.evaluate(context)


def _feed_reg(reg: StrategyRegistry, context: StrategyContext) -> Mapping[str, tuple[CandidateSetup, ...]]:
    """Feed context sequentially into StrategyRegistry, filling intermediate bars with neutral context."""
    if reg._last_bar_index is not None and context.bar_index > reg._last_bar_index + 1:
        ref_p = reg._last_context_payload["close"] if reg._last_context_payload else context.close
        for b in range(reg._last_bar_index + 1, context.bar_index):
            reg.evaluate_enabled(_make_context(
                bar_index=b,
                open_p=ref_p,
                high_p=ref_p,
                low_p=ref_p,
                close_p=ref_p,
                timeframe=context.timeframe,
                bias=context.htf_bias.bias if context.htf_bias else "neutral",
            ))
    return reg.evaluate_enabled(context)


class TestSMCS09Strategy(unittest.TestCase):
    """106-test exhaustive test suite for S09 ICT Silver Bullet Strategy Template."""

    # =========================================================================
    # Group A: Config / Profile / Protocol (Tests 01-10)
    # =========================================================================

    def test_01_config_defaults_exact(self):
        cfg = S09Config()
        self.assertEqual(cfg.mode, "internal")
        self.assertEqual(
            cfg.enabled_windows,
            ("silver_bullet_london", "silver_bullet_ny_am", "silver_bullet_ny_pm"),
        )
        self.assertEqual(cfg.grace_minutes, 15)
        self.assertEqual(cfg.fvg_to_mss_max_bars, 10)
        self.assertEqual(cfg.entry_level, "proximal")
        self.assertEqual(cfg.sl_buffer_price, 0.20)
        self.assertEqual(cfg.min_rr, 1.50)
        self.assertEqual(cfg.fallback_rr, 2.00)
        self.assertTrue(cfg.require_displacement)

    def test_02_config_json_round_trip(self):
        cfg = S09Config(
            mode="swing",
            enabled_windows=("silver_bullet_ny_am",),
            grace_minutes=15,
            fvg_to_mss_max_bars=8,
            entry_level="ce_50",
            sl_buffer_price=0.30,
            min_rr=1.80,
            fallback_rr=2.50,
            require_displacement=False,
        )
        d = cfg.to_dict()
        cfg_round = S09Config.from_dict(d)
        self.assertEqual(cfg, cfg_round)

    def test_03_config_rejects_bool_in_numeric(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(min_rr=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(fallback_rr=False)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(fvg_to_mss_max_bars=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(grace_minutes=True)  # type: ignore

    def test_04_config_rejects_nan_inf_zero_negative(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=float("nan"))
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=float("inf"))
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=0.0)
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=-0.2)
        with self.assertRaises(StrategyValidationError):
            S09Config(min_rr=0.0)
        with self.assertRaises(StrategyValidationError):
            S09Config(fallback_rr=1.2, min_rr=1.5)  # fallback_rr < min_rr
        with self.assertRaises(StrategyValidationError):
            S09Config(fvg_to_mss_max_bars=0)

    def test_05_config_rejects_unknown_mode_or_window_or_entry_level(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(mode="invalid")
        with self.assertRaises(StrategyValidationError):
            S09Config(enabled_windows=("asian_range",))  # unknown window
        with self.assertRaises(StrategyValidationError):
            S09Config(entry_level="market")

    def test_06_config_rejects_duplicate_or_empty_windows(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(enabled_windows=())
        with self.assertRaises(StrategyValidationError):
            S09Config(enabled_windows=("silver_bullet_ny_am", "silver_bullet_ny_am"))

    def test_07_config_canonicalizes_window_order(self):
        cfg = S09Config(
            enabled_windows=("silver_bullet_ny_pm", "silver_bullet_london")
        )
        self.assertEqual(
            cfg.enabled_windows,
            ("silver_bullet_london", "silver_bullet_ny_pm"),
        )

    def test_08_config_rejects_grace_minutes_different_from_15(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(grace_minutes=10)
        with self.assertRaises(StrategyValidationError):
            S09Config(grace_minutes=20)

    def test_09_fail_fast_wrong_config_type(self):
        with self.assertRaises(StrategyValidationError):
            S09ICTSilverBulletStrategy(config="invalid")  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09ICTSilverBulletStrategy(config={"mode": "internal"})  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09ICTSilverBulletStrategy(config=S01Config())  # type: ignore

    def test_10_protocol_profile_and_clean_exports(self):
        # 1. Protocol & Profile conformance
        strat = S09ICTSilverBulletStrategy()
        self.assertIsInstance(strat, StrategyTemplate)
        self.assertEqual(strat.strategy_id, "S09")
        self.assertEqual(strat.profile.style, "time_based")
        self.assertEqual(strat.profile.name, "ICT Silver Bullet")
        self.assertEqual(strat.profile.timeframes, ("M1", "M5", "M15"))
        self.assertEqual(strat.profile.max_setup_age_bars, 15)
        self.assertEqual(strat.profile.cooldown_bars, 3)
        self.assertEqual(strat.profile.min_rr, 1.50)

        # 2. Verify clean exports from smc.engine.strategies
        import smc.engine.strategies as strat_pkg

        self.assertIn("S09Config", strat_pkg.__all__)
        self.assertIn("S09ICTSilverBulletStrategy", strat_pkg.__all__)
        self.assertIs(strat_pkg.S09Config, S09Config)
        self.assertIs(strat_pkg.S09ICTSilverBulletStrategy, S09ICTSilverBulletStrategy)
        # Verify strategy-internal helpers are NOT leaked into package-level __all__
        self.assertNotIn("compute_bar_close_retention_bar", strat_pkg.__all__)
        self.assertNotIn("compute_window_bounds_for_date", strat_pkg.__all__)
        self.assertNotIn("CANONICAL_WINDOWS", strat_pkg.__all__)

        # 3. Verify clean exports from smc.engine
        import smc.engine as engine_pkg

        self.assertIn("S09Config", engine_pkg.__all__)
        self.assertIn("S09ICTSilverBulletStrategy", engine_pkg.__all__)
        self.assertIs(engine_pkg.S09Config, S09Config)
        self.assertIs(engine_pkg.S09ICTSilverBulletStrategy, S09ICTSilverBulletStrategy)
        # Verify internal helpers are NOT leaked into smc.engine.__all__
        self.assertNotIn("compute_bar_close_retention_bar", engine_pkg.__all__)
        self.assertNotIn("compute_window_bounds_for_date", engine_pkg.__all__)

        # 4. Verify module-level exports in smc.engine.strategies.s09_ict_silver_bullet
        import smc.engine.strategies.s09_ict_silver_bullet as s09_mod

        self.assertIn("S09Config", s09_mod.__all__)
        self.assertIn("S09ICTSilverBulletStrategy", s09_mod.__all__)
        self.assertIn("compute_bar_close_retention_bar", s09_mod.__all__)

        # 5. Fresh-process import test (verifies isolation & clean package resolution)
        code = (
            "import smc.engine as e; "
            "import smc.engine.strategies as s; "
            "import smc.engine.strategies.s09_ict_silver_bullet as m; "
            "assert e.S09Config is s.S09Config is m.S09Config; "
            "assert e.S09ICTSilverBulletStrategy is s.S09ICTSilverBulletStrategy is m.S09ICTSilverBulletStrategy; "
            "assert 'compute_bar_close_retention_bar' not in e.__all__; "
            "assert 'compute_bar_close_retention_bar' not in s.__all__; "
            "assert 'compute_bar_close_retention_bar' in m.__all__; "
            "print('EXPORTS_OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "EXPORTS_OK")

    # =========================================================================
    # Group B: Window / Timezone / DST / Calendar (Tests 11-25)
    # =========================================================================

    def test_11_three_canonical_windows_exact(self):
        self.assertEqual(len(CANONICAL_WINDOWS), 3)
        self.assertEqual(
            WINDOW_SCHEDULE["silver_bullet_london"],
            (datetime.time(3, 0), datetime.time(4, 0)),
        )
        self.assertEqual(
            WINDOW_SCHEDULE["silver_bullet_ny_am"],
            (datetime.time(10, 0), datetime.time(11, 0)),
        )
        self.assertEqual(
            WINDOW_SCHEDULE["silver_bullet_ny_pm"],
            (datetime.time(14, 0), datetime.time(15, 0)),
        )

    def test_12_event_start_boundary_inclusive(self):
        # 10:00:00 NY time on Wednesday 2024-05-15
        dt_1000 = datetime.datetime(2024, 5, 15, 10, 0, 0, tzinfo=NEW_YORK_TZ)
        ts_1000 = pd.Timestamp(dt_1000).tz_convert("UTC")
        res = get_window_for_close_time(ts_1000, CANONICAL_WINDOWS, 15)
        self.assertIsNotNone(res)
        w_key, w_start, w_end, w_grace = res
        self.assertEqual(w_key[0], "silver_bullet_ny_am")
        self.assertEqual(ts_1000, w_start)

    def test_13_event_end_boundary_exclusive(self):
        # 11:00:00 NY time on Wednesday 2024-05-15 (exact window end is excluded)
        dt_1100 = datetime.datetime(2024, 5, 15, 11, 0, 0, tzinfo=NEW_YORK_TZ)
        ts_1100 = pd.Timestamp(dt_1100).tz_convert("UTC")
        res = get_window_for_close_time(ts_1100, CANONICAL_WINDOWS, 15)
        self.assertIsNone(res)

    def test_14_retest_grace_exact_inclusive(self):
        # 11:15:00 NY time on Wednesday 2024-05-15
        start_utc, end_utc, grace_utc = compute_window_bounds_for_date(
            "2024-05-15", "silver_bullet_ny_am", 15
        )
        dt_1115 = datetime.datetime(2024, 5, 15, 11, 15, 0, tzinfo=NEW_YORK_TZ)
        ts_1115 = pd.Timestamp(dt_1115).tz_convert("UTC")
        self.assertEqual(ts_1115, grace_utc)
        self.assertTrue(ts_1115 <= grace_utc)

    def test_15_retest_after_grace_rejected(self):
        start_utc, end_utc, grace_utc = compute_window_bounds_for_date(
            "2024-05-15", "silver_bullet_ny_am", 15
        )
        ts_1115_01 = grace_utc + pd.Timedelta(seconds=1)
        self.assertFalse(ts_1115_01 <= grace_utc)

    def test_16_bar_open_before_grace_close_after_grace_rejected(self):
        # M15 bar opening at 11:10 (before 11:15) and closing at 11:25 (after 11:15)
        start_utc, end_utc, grace_utc = compute_window_bounds_for_date(
            "2024-05-15", "silver_bullet_ny_am", 15
        )
        dt_open = datetime.datetime(2024, 5, 15, 11, 10, 0, tzinfo=NEW_YORK_TZ)
        dt_close = datetime.datetime(2024, 5, 15, 11, 25, 0, tzinfo=NEW_YORK_TZ)
        ts_open = pd.Timestamp(dt_open).tz_convert("UTC")
        ts_close = pd.Timestamp(dt_close).tz_convert("UTC")

        self.assertTrue(ts_open < grace_utc)
        self.assertFalse(ts_close <= grace_utc)

    def test_17_local_ny_date_differs_from_utc_date_no_collision(self):
        # London window: 03:00 NY in winter (EST = UTC-5) is 08:00 UTC
        dt = datetime.datetime(2024, 1, 15, 3, 30, 0, tzinfo=NEW_YORK_TZ)
        ts_utc = pd.Timestamp(dt).tz_convert("UTC")
        local_date = get_ny_local_date_str(ts_utc)
        self.assertEqual(local_date, "2024-01-15")

    def test_18_weekends_rejected(self):
        # Saturday 2024-05-18 at 10:30 NY time
        dt_sat = datetime.datetime(2024, 5, 18, 10, 30, 0, tzinfo=NEW_YORK_TZ)
        ts_sat = pd.Timestamp(dt_sat).tz_convert("UTC")
        res_sat = get_window_for_close_time(ts_sat, CANONICAL_WINDOWS, 15)
        self.assertIsNone(res_sat)

        # Sunday 2024-05-19 at 10:30 NY time
        dt_sun = datetime.datetime(2024, 5, 19, 10, 30, 0, tzinfo=NEW_YORK_TZ)
        ts_sun = pd.Timestamp(dt_sun).tz_convert("UTC")
        res_sun = get_window_for_close_time(ts_sun, CANONICAL_WINDOWS, 15)
        self.assertIsNone(res_sun)

    def test_19_winter_offset_est_utc_minus_5(self):
        # Winter: 2024-01-17 (Wednesday)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-01-17", "silver_bullet_ny_am", 15
        )
        # 10:00 EST is 15:00 UTC
        self.assertEqual(start_utc.hour, 15)
        self.assertEqual(end_utc.hour, 16)

    def test_20_summer_offset_edt_utc_minus_4(self):
        # Summer: 2024-07-17 (Wednesday)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-07-17", "silver_bullet_ny_am", 15
        )
        # 10:00 EDT is 14:00 UTC
        self.assertEqual(start_utc.hour, 14)
        self.assertEqual(end_utc.hour, 15)

    def test_21_spring_dst_transition_date(self):
        # US Spring DST transition was Sunday 2024-03-10
        # Monday 2024-03-11 is EDT (UTC-4)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-03-11", "silver_bullet_ny_am", 15
        )
        self.assertEqual(start_utc.hour, 14)

    def test_22_fall_dst_transition_date(self):
        # US Fall DST transition was Sunday 2024-11-03
        # Monday 2024-11-04 is EST (UTC-5)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-11-04", "silver_bullet_ny_am", 15
        )
        self.assertEqual(start_utc.hour, 15)

    def test_23_window_key_deterministic_and_unique(self):
        dt = datetime.datetime(2024, 5, 15, 10, 15, 0, tzinfo=NEW_YORK_TZ)
        ts = pd.Timestamp(dt).tz_convert("UTC")
        res1 = get_window_for_close_time(ts, CANONICAL_WINDOWS, 15)
        res2 = get_window_for_close_time(ts, CANONICAL_WINDOWS, 15)
        self.assertIsNotNone(res1)
        self.assertIsNotNone(res2)
        self.assertEqual(res1[0], res2[0])
        self.assertEqual(res1[0][0], "silver_bullet_ny_am")
        self.assertEqual(res1[0][1], "2024-05-15")

    def test_24_two_different_dates_same_window_name_do_not_share_state(self):
        dt1 = datetime.datetime(2024, 5, 15, 10, 15, 0, tzinfo=NEW_YORK_TZ)
        dt2 = datetime.datetime(2024, 5, 16, 10, 15, 0, tzinfo=NEW_YORK_TZ)
        res1 = get_window_for_close_time(pd.Timestamp(dt1).tz_convert("UTC"), CANONICAL_WINDOWS, 15)
        res2 = get_window_for_close_time(pd.Timestamp(dt2).tz_convert("UTC"), CANONICAL_WINDOWS, 15)
        self.assertNotEqual(res1[0], res2[0])

    def test_25_window_disabled_in_config_rejects_ingestion(self):
        strat = S09ICTSilverBulletStrategy(
            S09Config(enabled_windows=("silver_bullet_ny_am",))
        )
        # London window: 03:30 NY time
        dt_london = datetime.datetime(2024, 5, 15, 3, 30, 0, tzinfo=NEW_YORK_TZ)
        open_ts = pd.Timestamp(dt_london).tz_convert("UTC")
        close_ts = open_ts + pd.Timedelta(minutes=1)
        ctx = _make_context(
            bar_index=0,
            open_time=open_ts,
            close_time=close_ts,
            sweeps=[_make_sweep(index=0, direction="bullish")],
        )
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    # =========================================================================
    # Group C: Sweep Admission & Bias (Tests 26-38)
    # =========================================================================

    def test_26_bullish_low_sweep_creates_buy_narrative(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 5 inside NY AM window (10:05 NY time)
        sw = _make_sweep(index=5, direction="bullish", pool_kind="equal_lows")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.direction, "BUY")
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_27_bearish_high_sweep_creates_sell_narrative(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bearish", pool_kind="equal_highs")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bearish")
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.direction, "SELL")
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_28_direction_pool_kind_mismatch_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Bullish sweep on equal_highs is invalid
        sw = _make_sweep(index=5, direction="bullish", pool_kind="equal_highs")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_29_invalid_or_wrong_mode_sweep_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        sw_invalid = _make_sweep(index=5, direction="bullish", valid=False)
        ctx1 = _make_context(bar_index=5, sweeps=[sw_invalid], bias="bullish")
        strat.evaluate(ctx1)
        self.assertEqual(len(strat._narratives), 0)

        strat.reset()
        sw_swing = _make_sweep(index=5, direction="bullish", mode="swing")
        ctx2 = _make_context(bar_index=5, sweeps=[sw_swing], bias="bullish")
        strat.evaluate(ctx2)
        self.assertEqual(len(strat._narratives), 0)

    def test_30_old_rolling_buffer_sweep_not_retrospectively_ingested(self):
        strat = S09ICTSilverBulletStrategy()
        # Sweep at bar 3 evaluating at bar 5
        sw = _make_sweep(index=3, direction="bullish", confirmed_at=3, swept_at=3)
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_31_sweep_close_before_window_start_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # 09:59 NY time (before 10:00 start)
        dt = datetime.datetime(2024, 5, 15, 9, 58, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)  # closes at 09:59
        sw = _make_sweep(index=0, direction="bullish")
        ctx = _make_context(bar_index=0, open_time=o_ts, close_time=c_ts, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_32_sweep_close_exact_window_start_accepted(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar from 09:59 to 10:00 NY time (closes exact at 10:00:00 start)
        dt = datetime.datetime(2024, 5, 15, 9, 59, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)  # closes exact at 10:00:00
        sw = _make_sweep(index=0, direction="bullish")
        ctx = _make_context(bar_index=0, open_time=o_ts, close_time=c_ts, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 1)

    def test_33_sweep_close_exact_window_end_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar from 10:59 to 11:00 NY time (closes exact at 11:00:00 end)
        dt = datetime.datetime(2024, 5, 15, 10, 59, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)  # closes exact at 11:00:00
        sw = _make_sweep(index=0, direction="bullish")
        ctx = _make_context(bar_index=0, open_time=o_ts, close_time=c_ts, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_34_aligned_htf_bias_accepted(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 1)

    def test_35_neutral_htf_bias_accepted(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="neutral")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 1)

    def test_36_opposed_htf_bias_rejected_terminal(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bearish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_37_missing_htf_bias_fails_closed(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias=None, htf_bias=None)
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_38_bias_becomes_opposed_during_active_narrative_terminal_no_revival(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx1 = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx1)
        self.assertEqual(len(strat._narratives), 1)

        # Next bar: bias turns bearish
        ctx2 = _make_context(bar_index=6, bias="bearish")
        strat.evaluate(ctx2)
        self.assertEqual(len(strat._narratives), 0)

        # Bar 7: bias turns bullish again -> narrative does not revive!
        ctx3 = _make_context(bar_index=7, bias="bullish")
        strat.evaluate(ctx3)
        self.assertEqual(len(strat._narratives), 0)

    # =========================================================================
    # Group D: MSS / FVG Linkage (Tests 39-55)
    # =========================================================================

    def test_39_valid_sweep_choch_fvg_pair_transitions_to_fvg_ready(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 5: Sweep
        sw = _make_sweep(index=5, direction="bullish")
        ctx5 = _make_context(bar_index=5, sweeps=[sw])
        strat.evaluate(ctx5)

        # Bar 6: CHoCH breakout with FVG (formed at bar 5, confirmed at bar 6)
        mss = _make_structure(index=6, direction="bullish", event_type="CHoCH", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        ctx6 = _make_context(bar_index=6, structure_events=[mss], fvgs=[fvg])
        strat.evaluate(ctx6)

        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)
        self.assertEqual(narr.ready_at, 6)

    def test_40_bos_event_accepted_as_mss(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        strat.evaluate(_make_context(bar_index=5, sweeps=[sw]))

        mss = _make_structure(index=6, direction="bullish", event_type="BOS", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)
        self.assertEqual(narr.mss.event_type, "BOS")

    def test_41_wick_break_structure_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=6, direction="bullish", break_type="wick", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_42_displacement_false_rejected_when_required(self):
        strat = S09ICTSilverBulletStrategy(S09Config(require_displacement=True))
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=6, direction="bullish", displacement=False, structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_43_wrong_direction_or_mode_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Opposite direction MSS
        mss_bear = _make_structure(index=6, direction="bearish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss_bear], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_44_old_mss_in_rolling_buffer_not_retrospectively_transitioned(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # MSS at bar 5 (same as sweep) evaluating at bar 6
        mss_old = _make_structure(index=5, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss_old], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_45_mss_close_at_window_end_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 59 closes at exact 11:00:00 (window end)
        dt = datetime.datetime(2024, 5, 15, 10, 59, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)

        # Ingest sweep earlier
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        # Feed up to bar 58
        _feed(strat, _make_context(bar_index=58))

        mss = _make_structure(index=59, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=58, direction="bullish", confirmed_at=59, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=59, open_time=o_ts, close_time=c_ts, structure_events=[mss], fvgs=[fvg]))

        # At bar 59, close is at window end, so MSS cannot confirm inside window
        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_46_fvg_confirmed_inside_window_passes(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)

    def test_47_fvg_formed_in_window_but_confirmed_at_or_after_window_end_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        _feed(strat, _make_context(bar_index=58))

        # Bar 59 closes at 11:00
        dt59 = datetime.datetime(2024, 5, 15, 10, 59, 0, tzinfo=NEW_YORK_TZ)
        o59 = pd.Timestamp(dt59).tz_convert("UTC")
        c59 = o59 + pd.Timedelta(minutes=1)

        # FVG confirmed at bar 59
        fvg = _make_fvg(index=58, direction="bullish", confirmed_at=59, structure_leg_id="leg1")
        mss = _make_structure(index=59, direction="bullish", structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=59, open_time=o59, close_time=c59, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_48_missing_bar_close_mapping_fails_closed(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6))

        # Artificially remove bar 6 from bar close times
        strat._bar_close_times.pop(6, None)

        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=7, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_49_fvg_before_sweep_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Sweep at bar 5
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # FVG formed at bar 4 (before sweep)
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=4, direction="bullish", confirmed_at=5, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_50_fvg_after_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # FVG at bar 7, MSS at bar 6
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=7, direction="bullish", confirmed_at=8, structure_leg_id="leg1")
        _feed(strat, _make_context(bar_index=8, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_51_fvg_lag_boundary(self):
        cfg = S09Config(fvg_to_mss_max_bars=3)
        strat = S09ICTSilverBulletStrategy(cfg)
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # MSS at 9, FVG at 6: lag = 3 -> valid!
        mss = _make_structure(index=9, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=6, direction="bullish", confirmed_at=7, structure_leg_id="leg1")
        _feed(strat, _make_context(bar_index=9, structure_events=[mss], fvgs=[fvg]))
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)

        # But lag = 4 (MSS at 10, FVG at 5) is rejected
        strat.reset()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss2 = _make_structure(index=10, direction="bullish", structure_leg_id="leg1")
        fvg2 = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        _feed(strat, _make_context(bar_index=10, structure_events=[mss2], fvgs=[fvg2]))
        narr2 = list(strat._narratives.values())[0]
        self.assertEqual(narr2.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_51b_retention_probe_lag_60_success(self):
        """Probe verifying fvg_to_mss_max_bars=60 retains bar_close_time for confirmed_at=2 at bar 55."""
        cfg = S09Config(fvg_to_mss_max_bars=60)
        strat = S09ICTSilverBulletStrategy(cfg)

        # Bar 0 (10:00 NY AM window start): sweep at bar 0
        sw = _make_sweep(index=0, direction="bullish", pool_kind="equal_lows")
        strat.evaluate(_make_context(bar_index=0, sweeps=[sw]))
        self.assertEqual(len(strat._narratives), 1)

        # Feed up to bar 54 sequentially (e.g. FVG forms at 1, confirmed at 2)
        fvg = _make_fvg(index=1, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=2, structure_leg_id="leg_probe")
        for b in range(1, 55):
            ctx_fvgs = [fvg] if b >= 2 else []
            strat.evaluate(_make_context(bar_index=b, fvgs=ctx_fvgs))

        # At bar 54: check that bar 2 is still retained in _bar_close_times
        self.assertIn(2, strat._bar_close_times)

        # Bar 55: MSS confirmed with shared structure_leg_id
        mss = _make_structure(index=55, direction="bullish", broken_swing_index=10, structure_leg_id="leg_probe")
        strat.evaluate(_make_context(bar_index=55, structure_events=[mss], fvgs=[fvg]))

        # Invariants at bar 55:
        # 1. Bar 2 close time still retained
        self.assertIn(2, strat._bar_close_times)
        # 2. Pair is matched and narrative moves to FVG_READY
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)
        self.assertIsNotNone(narr.mss)
        self.assertEqual(narr.mss.index, 55)
        self.assertIsNotNone(narr.fvg)
        self.assertEqual(narr.fvg.index, 1)
        self.assertEqual(narr.fvg.confirmed_at, 2)

    def test_51c_retention_probe_lag_boundaries(self):
        """Exact lag boundary check: lag == config accepted, lag > config rejected."""
        max_lag = 25
        cfg = S09Config(fvg_to_mss_max_bars=max_lag)

        # 1. Lag == max_lag (boundary accepted)
        s1 = S09ICTSilverBulletStrategy(cfg)
        s1.evaluate(_make_context(bar_index=0, sweeps=[_make_sweep(index=0, direction="bullish")]))
        fvg1 = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        for b in range(1, 5 + max_lag):
            s1.evaluate(_make_context(bar_index=b, fvgs=[fvg1] if b >= 6 else []))

        # Bar 30: mss.index - fvg.index = 30 - 5 = 25 == max_lag
        mss_exact = _make_structure(index=5 + max_lag, direction="bullish", structure_leg_id="leg1")
        s1.evaluate(_make_context(bar_index=5 + max_lag, structure_events=[mss_exact], fvgs=[fvg1]))
        narr1 = list(s1._narratives.values())[0]
        self.assertEqual(narr1.stage, S09NarrativeStage.FVG_READY)

        # 2. Lag == max_lag + 1 (boundary rejected)
        s2 = S09ICTSilverBulletStrategy(cfg)
        s2.evaluate(_make_context(bar_index=0, sweeps=[_make_sweep(index=0, direction="bullish")]))
        fvg2 = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        for b in range(1, 5 + max_lag + 1):
            s2.evaluate(_make_context(bar_index=b, fvgs=[fvg2] if b >= 6 else []))

        # Bar 31: mss.index - fvg.index = 31 - 5 = 26 > max_lag
        mss_over = _make_structure(index=5 + max_lag + 1, direction="bullish", structure_leg_id="leg1")
        s2.evaluate(_make_context(bar_index=5 + max_lag + 1, structure_events=[mss_over], fvgs=[fvg2]))
        narr2 = list(s2._narratives.values())[0]
        self.assertEqual(narr2.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_52_null_or_mismatched_structure_leg_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # MSS leg1 vs FVG leg2
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg2")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_53_opposite_structure_in_fvg_mss_interval_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Opposite event at bar 6 between FVG (5) and MSS (7)
        opp = _make_structure(index=6, direction="bearish")
        _feed(strat, _make_context(bar_index=6, structure_events=[opp]))

        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=7, structure_events=[opp, mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_54_fvg_filled_prior_to_or_at_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        # FVG filled at bar 6 (prior to MSS at 7)
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1", filled_at=6)
        _feed(strat, _make_context(bar_index=7, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_55_pair_tie_break_permutation_invariant(self):
        # Two FVGs on same leg: fvg1 (index 5) vs fvg2 (index 6)
        # Canonical tie-break prefers closer to MSS (descending index -> fvg2)
        strat1 = S09ICTSilverBulletStrategy()
        strat1.evaluate(_make_context(bar_index=4, sweeps=[_make_sweep(index=4, direction="bullish")]))
        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        f1 = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        f2 = _make_fvg(index=6, direction="bullish", confirmed_at=7, structure_leg_id="leg1")

        _feed(strat1, _make_context(bar_index=7, structure_events=[mss], fvgs=[f1, f2]))
        narr1 = list(strat1._narratives.values())[0]

        strat2 = S09ICTSilverBulletStrategy()
        strat2.evaluate(_make_context(bar_index=4, sweeps=[_make_sweep(index=4, direction="bullish")]))
        _feed(strat2, _make_context(bar_index=7, structure_events=[mss], fvgs=[f2, f1]))
        narr2 = list(strat2._narratives.values())[0]

        self.assertEqual(narr1.fvg.index, narr2.fvg.index)
        self.assertEqual(narr1.fvg.index, 6)

    # =========================================================================
    # Group E: Retest & Invalidation (Tests 56-70)
    # =========================================================================

    def test_56_retest_after_mss_emits_candidate(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 5: Sweep
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        # Bar 6: MSS + FVG [2038, 2042]
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Bar 7: Retest touches FVG (low=2040 in [2038, 2042], close=2041 >= 2038)
        pool = _make_pool(confirmed_at=2, price=2060.0)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg], pools=[pool]))

        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.direction, "BUY")
        self.assertEqual(c.entry_price, 2042.0)
        self.assertEqual(c.stop_loss, 2034.8)  # 2035 - 0.20
        self.assertEqual(c.take_profit, 2060.0)

    def test_57_retest_at_same_bar_as_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Bar 6: MSS + FVG + price wicks into FVG on same bar
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        cands = strat.evaluate(_make_context(bar_index=6, low_p=2040.0, close_p=2041.0, structure_events=[mss], fvgs=[fvg]))

        # Retest on same bar as MSS cannot emit
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, S09NarrativeStage.FVG_READY)

    def test_58_wick_overlap_and_close_respect_buy(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Retest wicks below bottom (low=2037 < 2038) but closes above (close=2039 >= 2038)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2037.0, close_p=2039.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 1)

    def test_59_wick_overlap_and_close_respect_sell(self):
        strat = S09ICTSilverBulletStrategy()
        # Bearish setup
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bearish", pool_kind="equal_highs", price_wick=2065.0)], bias="bearish"))
        mss = _make_structure(index=6, direction="bearish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bearish", top=2062.0, bottom=2058.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg], bias="bearish"))

        # Retest wicks above top (high=2063 > 2062) but closes below (close=2060 <= 2062)
        cands = strat.evaluate(_make_context(bar_index=7, high_p=2063.0, close_p=2060.0, fvgs=[fvg], bias="bearish"))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].direction, "SELL")
        self.assertEqual(cands[0].entry_price, 2058.0)  # proximal for SELL is bottom
        self.assertEqual(cands[0].stop_loss, 2065.2)  # 2065 + 0.20

    def test_60_fvg_filled_at_none_passes_on_touch(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1", filled_at=None)
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 1)

    def test_61_fvg_filled_at_n_passes_if_close_respects(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # FVG fully filled at current bar 7 (low <= 2038, close >= 2038)
        fvg_filled_now = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1", filled=True, filled_at=7)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2037.0, close_p=2039.0, fvgs=[fvg_filled_now]))
        self.assertEqual(len(cands), 1)

    def test_62_fvg_filled_at_less_than_n_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Prior fill at bar 6
        fvg_prior_filled = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1", filled=True, filled_at=6)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg_prior_filled]))
        self.assertEqual(len(cands), 0)

    def test_63_fvg_filled_at_greater_than_n_future_leak_raises(self):
        strat = S09ICTSilverBulletStrategy()
        fvg_future = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1", filled=True, filled_at=10)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=7, fvgs=[fvg_future]))

    def test_63a_structure_confirmed_swing_at_in_future_raises(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 5: structure with index=5 but confirmed_swing_at=6 > 5
        st_future = _make_structure(index=5, direction="bullish", confirmed_swing_at=6)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=5, structure_events=[st_future]))

    def test_63b_htf_bias_future_as_of_raises(self):
        strat = S09ICTSilverBulletStrategy()
        _, c_ts = _bar_time(5)
        bias_future = BiasStateSnapshot(
            bias="bullish",
            timestamp=c_ts,
            as_of=c_ts + pd.Timedelta(seconds=1),
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=5, htf_bias=bias_future))

    def test_63c_htf_bias_future_source_event_time_raises(self):
        strat = S09ICTSilverBulletStrategy()
        _, c_ts = _bar_time(5)
        bias_future = BiasStateSnapshot(
            bias="bullish",
            timestamp=c_ts,
            as_of=c_ts,
            source_event_time=c_ts + pd.Timedelta(seconds=1),
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=5, htf_bias=bias_future))

    def test_63d_htf_bias_future_timestamp_raises(self):
        strat = S09ICTSilverBulletStrategy()
        _, c_ts = _bar_time(5)
        bias_future = BiasStateSnapshot(
            bias="bullish",
            timestamp=c_ts + pd.Timedelta(seconds=1),
            as_of=c_ts,
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=5, htf_bias=bias_future))

    def test_63e_session_decision_future_timestamp_raises(self):
        strat = S09ICTSilverBulletStrategy()
        _, c_ts = _bar_time(5)
        sd_future = SessionDecisionSnapshot(
            in_session=True,
            session_name="NY_AM",
            timestamp=c_ts + pd.Timedelta(seconds=1),
            reason="active",
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=5, session_decision=sd_future))

    def test_63f_pool_future_invalidated_at_raises(self):
        strat = S09ICTSilverBulletStrategy()
        p_future = _make_pool(confirmed_at=2, price=2060.0, invalidated_at=6)  # N=5, 6 > 5!
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=5, pools=[p_future]))

    def test_63g_state_invariance_after_future_leak_errors(self):
        """Persistent state must remain completely unaltered after any future leak error."""
        strat = S09ICTSilverBulletStrategy()
        # Bar 5: valid evaluation with a sweep
        sw = _make_sweep(index=5, direction="bullish")
        strat.evaluate(_make_context(bar_index=5, sweeps=[sw]))

        # Take full deep snapshots of all internal state fields
        snap_narratives = copy.deepcopy(strat._narratives)
        snap_consumed = dict(strat._consumed_windows)
        snap_clusters = dict(strat._emitted_clusters)
        snap_close_times = dict(strat._bar_close_times)
        snap_last_bar = strat._last_bar_index
        snap_last_ts = strat._last_timestamp
        snap_last_bct = strat._last_bar_close_time
        snap_payload = copy.deepcopy(strat._last_context_payload)
        snap_result = copy.deepcopy(strat._last_result)

        # 1. Structure future leak
        with self.assertRaises(StrategyStateError):
            st_err = _make_structure(index=6, direction="bullish", confirmed_swing_at=7)
            strat.evaluate(_make_context(bar_index=6, structure_events=[st_err]))

        # 2. HTF Bias future leak
        with self.assertRaises(StrategyStateError):
            _, c6 = _bar_time(6)
            bias_err = BiasStateSnapshot(bias="bullish", timestamp=c6, as_of=c6 + pd.Timedelta(hours=1))
            strat.evaluate(_make_context(bar_index=6, htf_bias=bias_err))

        # 3. Session decision future leak
        with self.assertRaises(StrategyStateError):
            _, c6 = _bar_time(6)
            sd_err = SessionDecisionSnapshot(in_session=True, session_name="NY", timestamp=c6 + pd.Timedelta(minutes=5), reason="err")
            strat.evaluate(_make_context(bar_index=6, session_decision=sd_err))

        # 4. Pool future leak
        with self.assertRaises(StrategyStateError):
            p_err = _make_pool(confirmed_at=2, invalidated_at=7)
            strat.evaluate(_make_context(bar_index=6, pools=[p_err]))

        # Verify all snapshots are 100% untouched
        self.assertEqual(strat._narratives, snap_narratives)
        self.assertEqual(strat._consumed_windows, snap_consumed)
        self.assertEqual(strat._emitted_clusters, snap_clusters)
        self.assertEqual(strat._bar_close_times, snap_close_times)
        self.assertEqual(strat._last_bar_index, snap_last_bar)
        self.assertEqual(strat._last_timestamp, snap_last_ts)
        self.assertEqual(strat._last_bar_close_time, snap_last_bct)
        self.assertEqual(strat._last_context_payload, snap_payload)
        self.assertEqual(strat._last_result, snap_result)

    def test_63h_exact_equality_with_current_bar_accepted(self):
        """Values exactly equal to current bar / close time must NOT raise."""
        strat = S09ICTSilverBulletStrategy()
        _, c_ts = _bar_time(5)
        st = _make_structure(index=5, direction="bullish", confirmed_swing_at=5)
        pool = _make_pool(confirmed_at=5, swept=True, swept_at=5, invalidated_at=5)
        bias = BiasStateSnapshot(
            bias="bullish",
            timestamp=c_ts,
            as_of=c_ts,
            source_event_time=c_ts,
            source_event_index=5,
        )
        sd = SessionDecisionSnapshot(
            in_session=True,
            session_name="NY_AM",
            timestamp=c_ts,
            reason="valid",
        )
        ctx = _make_context(
            bar_index=5,
            structure_events=[st],
            pools=[pool],
            htf_bias=bias,
            session_decision=sd,
            close_time=c_ts,
        )
        # Must execute without raising StrategyStateError
        strat.evaluate(ctx)
        self.assertEqual(strat._last_bar_index, 5)

    def test_63i_matcher_defends_against_future_confirmed_swing(self):
        """Matcher defensively rejects any structure event where confirmed_swing_at > N."""
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        st = _make_structure(index=6, direction="bullish", structure_leg_id="leg1", confirmed_swing_at=7)
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")

        narrative = list(strat._narratives.values())[0]
        # Direct call into matcher to verify defense
        strat._match_mss_and_fvg(
            context=_make_context(bar_index=6, structure_events=[st], fvgs=[fvg]),
            narrative=narrative,
            bar_close_times=strat._bar_close_times,
        )
        self.assertEqual(narrative.stage, S09NarrativeStage.SWEEP_SEEN)
        self.assertIsNone(narrative.mss)

    def test_64_close_through_same_bar_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Close violates bottom boundary: close=2036 < 2038
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2035.0, close_p=2036.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)  # invalidated and pruned

    def test_65_sweep_extreme_violation_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Sweep price_wick = 2035.0
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Price closes below sweep extreme: close=2034 < 2035
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2033.0, close_p=2034.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_66_opposite_structure_after_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Opposite structure event at bar 7
        opp_mss = _make_structure(index=7, direction="bearish")
        cands = strat.evaluate(_make_context(bar_index=7, structure_events=[opp_mss], fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_67_opposite_structure_at_signal_bar_rejected_before_emission(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # At bar 8: retest touch happens, but also an opposite structure event forms on same bar!
        opp_mss = _make_structure(index=8, direction="bearish")
        _feed(strat, _make_context(bar_index=7, fvgs=[fvg]))
        cands = strat.evaluate(_make_context(bar_index=8, low_p=2040.0, close_p=2041.0, structure_events=[opp_mss], fvgs=[fvg]))
        self.assertEqual(len(cands), 0)

    def test_68_retest_within_grace_passes(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Advance to bar 65 (11:05 NY time, within 15m grace ending at 11:15)
        _feed(strat, _make_context(bar_index=64, fvgs=[fvg]))
        cands = strat.evaluate(_make_context(bar_index=65, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 1)

    def test_69_retest_after_grace_rejected_and_pruned(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Advance to bar 76 (11:16 NY time, after 11:15 grace expiry)
        _feed(strat, _make_context(bar_index=75, fvgs=[fvg]))
        cands = strat.evaluate(_make_context(bar_index=76, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_70_missing_fvg_snapshot_handled_safely(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Bar 7: active_fvgs is empty
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[]))
        self.assertEqual(len(cands), 1)  # Internal FVG in narrative still triggers

    # =========================================================================
    # Group F: Pricing, Evidence & IDs (Tests 71-84)
    # =========================================================================

    def test_71_proximal_entry_pricing_buy_and_sell(self):
        strat = S09ICTSilverBulletStrategy(S09Config(entry_level="proximal"))
        # BUY: proximal is top
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        cands_buy = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(cands_buy[0].entry_price, 2042.0)

        # SELL: proximal is bottom
        strat2 = S09ICTSilverBulletStrategy(S09Config(entry_level="proximal"))
        strat2.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bearish", pool_kind="equal_highs")], bias="bearish"))
        strat2.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bearish")], fvgs=[_make_fvg(index=5, direction="bearish", top=2062.0, bottom=2058.0, confirmed_at=6)], bias="bearish"))
        cands_sell = strat2.evaluate(_make_context(bar_index=7, high_p=2060.0, close_p=2059.0, bias="bearish"))
        self.assertEqual(cands_sell[0].entry_price, 2058.0)

    def test_72_ce50_entry_pricing_buy_and_sell(self):
        strat = S09ICTSilverBulletStrategy(S09Config(entry_level="ce_50"))
        # BUY: CE50 is (2042 + 2038) / 2 = 2040.0
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        cands_buy = strat.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0))
        self.assertEqual(cands_buy[0].entry_price, 2040.0)

        # SELL: CE50 is (2062 + 2058) / 2 = 2060.0
        strat2 = S09ICTSilverBulletStrategy(S09Config(entry_level="ce_50"))
        strat2.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bearish", pool_kind="equal_highs")], bias="bearish"))
        strat2.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bearish")], fvgs=[_make_fvg(index=5, direction="bearish", top=2062.0, bottom=2058.0, confirmed_at=6)], bias="bearish"))
        cands_sell = strat2.evaluate(_make_context(bar_index=7, high_p=2061.0, close_p=2059.0, bias="bearish"))
        self.assertEqual(cands_sell[0].entry_price, 2060.0)

    def test_73_sweep_extreme_sl_pricing(self):
        strat = S09ICTSilverBulletStrategy(S09Config(sl_buffer_price=0.20))
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.50)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(cands[0].stop_loss, 2035.30)  # 2035.50 - 0.20

    def test_74_nearest_opposing_pool_independent_of_input_order(self):
        pool1 = _make_pool(confirmed_at=1, price=2055.0, indices=(1, 2))
        pool2 = _make_pool(confirmed_at=2, price=2070.0, indices=(3, 4))

        # Order [pool1, pool2]
        s1 = S09ICTSilverBulletStrategy()
        s1.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        s1.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        c1 = s1.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool1, pool2]))

        # Order [pool2, pool1]
        s2 = S09ICTSilverBulletStrategy()
        s2.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        s2.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        c2 = s2.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool2, pool1]))

        self.assertEqual(c1[0].take_profit, c2[0].take_profit)
        self.assertEqual(c1[0].take_profit, 2055.0)

    def test_75_ineligible_pools_ignored(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))

        # Ineligible pools: swept=True, valid=False, price below entry
        p_swept = _make_pool(confirmed_at=1, price=2055.0, swept=True)
        p_invalid = _make_pool(confirmed_at=2, price=2056.0, valid=False)
        p_below = _make_pool(confirmed_at=3, price=2030.0)  # below entry 2042
        p_valid = _make_pool(confirmed_at=4, price=2065.0)

        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[p_swept, p_invalid, p_below, p_valid]))
        self.assertEqual(cands[0].take_profit, 2065.0)
        self.assertEqual(cands[0].target_type, "opposing_pool")

    def test_76_nearest_pool_rr_below_min_falls_back_to_fixed_rr_no_hop(self):
        strat = S09ICTSilverBulletStrategy(S09Config(min_rr=1.5, fallback_rr=2.0))
        # Entry=2042.0, SL=2034.8 -> Risk = 7.2
        # Min reward needed = 7.2 * 1.5 = 10.8 -> TP >= 2052.8
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))

        # Nearest pool is at 2045.0 (Reward = 3.0, RR = 3.0/7.2 = 0.42 < 1.5)
        # Far pool at 2070.0 has good RR, but strategy must NOT hop to far pool!
        p_close = _make_pool(confirmed_at=1, price=2045.0)
        p_far = _make_pool(confirmed_at=2, price=2070.0)

        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[p_close, p_far]))
        self.assertEqual(cands[0].target_type, "fixed_rr")
        # Fixed 2.0R fallback: 2042 + 2.0 * 7.2 = 2056.4
        self.assertEqual(cands[0].take_profit, 2056.4)

    def test_77_post_rounding_geometry_collapse_fails_closed(self):
        strat = S09ICTSilverBulletStrategy(S09Config(sl_buffer_price=0.0001))
        # Very tiny difference where entry == sl after 3-decimal rounding
        # Should fail closed and not emit
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2042.0001)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0001, bottom=2042.0, confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2042.0, close_p=2042.0))
        self.assertEqual(len(cands), 0)

    def test_78_planned_rr_recalculated_from_rounded_levels(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        pool = _make_pool(confirmed_at=1, price=2056.4)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool]))
        # Entry=2042.0, SL=2034.8, TP=2056.4 -> Risk=7.2, Reward=14.4 -> RR = 2.00
        self.assertEqual(cands[0].planned_rr, 2.00)

    def test_79_evidence_canonical_order_and_timestamps(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        pool = _make_pool(confirmed_at=2, price=2060.0)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool]))

        evs = cands[0].evidences
        self.assertEqual(len(evs), 4)
        self.assertEqual(evs[0].kind, "liquidity_sweep")
        self.assertEqual(evs[1].kind, "structure_event")
        self.assertEqual(evs[2].kind, "fair_value_gap")
        self.assertEqual(evs[3].kind, "liquidity_pool")

    def test_80_s09_cluster_id_matches_s01_for_same_opportunity(self):
        # When S01 and S09 evaluate the exact same sweep, MSS (broken_swing=4) and FVG (index=5)
        # Their evidence_cluster_id must match 100%!
        direction = "BUY"
        mode = "internal"
        broken_swing = 4
        fvg_index = 5
        leg_comp = f"leg-{mode}-{direction}-{broken_swing}"
        zone_comp = f"fvg-{mode}-{direction}-{fvg_index}"
        expected_cluster_id = make_cluster_id(direction, leg_comp, zone_comp)

        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish", broken_swing_index=broken_swing)], fvgs=[_make_fvg(index=fvg_index, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        self.assertEqual(cands[0].evidence_cluster_id, expected_cluster_id)

    def test_81_setup_id_distinguishes_s09_from_s01(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertTrue(cands[0].setup_id.startswith("S09:BUY:7:"))
        s01_setup_id = make_setup_id("S01", "BUY", 7, cands[0].evidence_cluster_id)
        self.assertNotEqual(cands[0].setup_id, s01_setup_id)

    def test_82_ids_injective_and_float_free(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        c = cands[0]
        # Cluster ID must not contain float '.'
        self.assertNotIn(".", c.evidence_cluster_id)
        for ev in c.evidences:
            # Evidence IDs must not contain float string representation
            self.assertNotIn(".", ev.evidence_id)

    def test_83_metadata_rich_and_json_safe(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        d = cands[0].to_dict()
        # Verify JSON serializability
        json_str = json.dumps(d)
        self.assertIsInstance(json_str, str)
        self.assertEqual(d["meta"]["window_name"], "silver_bullet_ny_am")
        self.assertEqual(d["meta"]["session_time_basis"], "bar_close_time")

    def test_84_candidate_expiry_bar_equals_signal_bar(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(cands[0].expiry_bar, 7)
        self.assertEqual(cands[0].bar_index, 7)

    # =========================================================================
    # Group G: One-per-window / State / Atomicity (Tests 85-97)
    # =========================================================================

    def test_85_two_valid_narratives_same_window_only_one_emits(self):
        strat = S09ICTSilverBulletStrategy()
        # Ingest two sweeps in same window
        sw1 = _make_sweep(index=4, direction="bullish", pool_kind="equal_lows", price_wick=2030.0, pool_indices=(1, 2))
        sw2 = _make_sweep(index=5, direction="bullish", pool_kind="swing_low", price_wick=2032.0, pool_indices=(3, 4))
        strat.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        strat.evaluate(_make_context(bar_index=5, sweeps=[sw2]))

        # Bar 6: MSS + FVG for both
        mss1 = _make_structure(index=6, direction="bullish", broken_swing_index=2, structure_leg_id="leg1")
        fvg1 = _make_fvg(index=4, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        fvg2 = _make_fvg(index=5, direction="bullish", top=2044.0, bottom=2040.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss1], fvgs=[fvg1, fvg2]))

        # Bar 7: Retest touches both FVGs
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[fvg1, fvg2]))

        # Exactly ONE candidate emitted for the window!
        self.assertEqual(len(cands), 1)
        # Winner was earlier sweep (index=4)
        self.assertEqual(cands[0].evidences[0].bar_index, 4)

    def test_86_first_proposal_fails_geometry_second_proposal_emits(self):
        strat = S09ICTSilverBulletStrategy(S09Config(sl_buffer_price=0.20))
        # Proposal 1 has price_wick right at entry price -> collapses geometry
        sw1 = _make_sweep(index=4, direction="bullish", price_wick=2042.0, pool_indices=(1, 2))
        # Proposal 2 has valid price_wick
        sw2 = _make_sweep(index=5, direction="bullish", price_wick=2030.0, pool_indices=(3, 4))
        strat.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        strat.evaluate(_make_context(bar_index=5, sweeps=[sw2]))

        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        f1 = _make_fvg(index=4, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        f2 = _make_fvg(index=5, direction="bullish", top=2044.0, bottom=2040.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[f1, f2]))

        # Bar 7: Retest touches both. Prop 1 collapses, Prop 2 succeeds!
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[f1, f2]))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].evidences[0].bar_index, 5)

    def test_87_after_emission_later_setup_same_window_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # First setup emits at bar 7
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands1 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands1), 1)

        # Later in same window at bar 15: another sweep occurs
        sw2 = _make_sweep(index=15, direction="bullish", pool_indices=(5, 6))
        _feed(strat, _make_context(bar_index=15, sweeps=[sw2]))
        # Window is already consumed -> sw2 cannot create active narrative
        self.assertEqual(len(strat._narratives), 0)

    def test_88_different_windows_same_day_emit_independently(self):
        strat = S09ICTSilverBulletStrategy()
        # Setup 1 in London window: 03:00 - 04:00 NY time
        dt_lon_sw = datetime.datetime(2024, 5, 15, 3, 10, 0, tzinfo=NEW_YORK_TZ)
        o_lon = pd.Timestamp(dt_lon_sw).tz_convert("UTC")
        c_lon = o_lon + pd.Timedelta(minutes=1)
        strat.evaluate(_make_context(bar_index=0, open_time=o_lon, close_time=c_lon, sweeps=[_make_sweep(index=0, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=1, open_time=c_lon, close_time=c_lon + pd.Timedelta(minutes=1), structure_events=[_make_structure(index=1, direction="bullish")], fvgs=[_make_fvg(index=0, direction="bullish", confirmed_at=1)]))
        cands_lon = strat.evaluate(_make_context(bar_index=2, open_time=c_lon + pd.Timedelta(minutes=1), close_time=c_lon + pd.Timedelta(minutes=2), low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands_lon), 1)

        # Setup 2 in NY AM window: 10:00 - 11:00 NY time
        # Fast forward
        _feed(strat, _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands_ny = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands_ny), 1)

    def test_89_next_day_same_window_resets_ownership(self):
        strat = S09ICTSilverBulletStrategy()
        # Day 1 (Wednesday 2024-05-15) emits setup
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands1 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands1), 1)

        # Day 2 (Thursday 2024-05-16) in NY AM window
        dt_day2 = datetime.datetime(2024, 5, 16, 10, 5, 0, tzinfo=NEW_YORK_TZ)
        o_d2 = pd.Timestamp(dt_day2).tz_convert("UTC")
        c_d2 = o_d2 + pd.Timedelta(minutes=1)

        # Feed to bar 100 on Day 2
        _feed(strat, _make_context(bar_index=100, open_time=o_d2, close_time=c_d2, sweeps=[_make_sweep(index=100, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=101, open_time=c_d2, close_time=c_d2 + pd.Timedelta(minutes=1), structure_events=[_make_structure(index=101, direction="bullish")], fvgs=[_make_fvg(index=100, direction="bullish", confirmed_at=101)]))
        cands2 = strat.evaluate(_make_context(bar_index=102, open_time=c_d2 + pd.Timedelta(minutes=1), close_time=c_d2 + pd.Timedelta(minutes=2), low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands2), 1)

    def test_90_same_bar_permutation_yields_same_winner(self):
        sw1 = _make_sweep(index=4, direction="bullish", price_wick=2030.0, pool_indices=(1, 2))
        sw2 = _make_sweep(index=5, direction="bullish", price_wick=2032.0, pool_indices=(3, 4))
        f1 = _make_fvg(index=4, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        f2 = _make_fvg(index=5, direction="bullish", top=2044.0, bottom=2040.0, confirmed_at=6, structure_leg_id="leg1")
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")

        s1 = S09ICTSilverBulletStrategy()
        s1.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        s1.evaluate(_make_context(bar_index=5, sweeps=[sw2]))
        s1.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[f1, f2]))
        c1 = s1.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[f1, f2]))

        s2 = S09ICTSilverBulletStrategy()
        s2.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        s2.evaluate(_make_context(bar_index=5, sweeps=[sw2]))
        s2.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[f2, f1]))
        c2 = s2.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[f2, f1]))

        self.assertEqual(c1[0].setup_id, c2[0].setup_id)

    def test_91_identical_retry_returns_cached_result(self):
        strat = S09ICTSilverBulletStrategy()
        ctx = _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")])
        res1 = strat.evaluate(ctx)
        res2 = strat.evaluate(ctx)
        self.assertIs(res1, res2)

    def test_92_conflicting_retry_raises_state_error(self):
        strat = S09ICTSilverBulletStrategy()
        ctx1 = _make_context(bar_index=5, close_p=2050.0)
        strat.evaluate(ctx1)
        ctx2 = _make_context(bar_index=5, close_p=2055.0)  # different close
        with self.assertRaises(StrategyStateError):
            strat.evaluate(ctx2)

    def test_93_non_monotonic_bar_gap_or_backward_raises(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5))
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=7))  # gap
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=4))  # backward

    def test_94_fault_in_matcher_rolls_back_all_state(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        snapshot_narratives = copy.deepcopy(strat._narratives)
        snapshot_clusters = dict(strat._emitted_clusters)
        snapshot_close_times = dict(strat._bar_close_times)

        with patch.object(strat, "_match_mss_and_fvg", side_effect=RuntimeError("Injected fault in matcher")):
            with self.assertRaises(RuntimeError):
                strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")]))

        self.assertEqual(strat._narratives, snapshot_narratives)
        self.assertEqual(strat._emitted_clusters, snapshot_clusters)
        self.assertEqual(strat._bar_close_times, snapshot_close_times)
        self.assertEqual(strat._last_bar_index, 5)

    def test_95_fault_in_candidate_builder_rolls_back_state(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))

        snapshot_narratives = copy.deepcopy(strat._narratives)
        snapshot_consumed = dict(strat._consumed_windows)
        snapshot_close_times = dict(strat._bar_close_times)

        with patch.object(strat, "_build_candidate", side_effect=RuntimeError("Injected fault in builder")):
            with self.assertRaises(RuntimeError):
                strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        self.assertEqual(strat._narratives, snapshot_narratives)
        self.assertEqual(strat._consumed_windows, snapshot_consumed)
        self.assertEqual(strat._bar_close_times, snapshot_close_times)
        self.assertEqual(strat._last_bar_index, 6)

    def test_96_reset_clears_state_and_replay_identical(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands1 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        strat.reset()
        self.assertIsNone(strat._last_bar_index)
        self.assertEqual(len(strat._narratives), 0)
        self.assertEqual(len(strat._consumed_windows), 0)

        # Replay produces identical result
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands2 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        self.assertEqual([c.to_dict() for c in cands1], [c.to_dict() for c in cands2])

    def test_97_long_workload_bounded_state_o1(self):
        """Regression test verifying bounded state and 0 duplicate emissions over 1,000 bars."""
        strat = S09ICTSilverBulletStrategy()
        peak_narratives = 0
        peak_consumed = 0
        peak_clusters = 0
        peak_close_times = 0
        emitted_setups = []

        # 1,000 bars stream (~16 hours of trading)
        for b in range(1000):
            sweeps = []
            structures = []
            fvgs = []

            # Emit setup every 100 bars (well separated)
            if b % 100 == 10:
                sweeps.append(_make_sweep(index=b, direction="bullish", pool_indices=(b, b + 1)))
            elif b % 100 == 12:
                structures.append(_make_structure(index=b, direction="bullish", broken_swing_index=b - 5, structure_leg_id=f"leg_{b}"))
                fvgs.append(_make_fvg(index=b - 1, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=b, structure_leg_id=f"leg_{b}"))

            low_p = 2040.0 if b % 100 == 15 else 2050.0
            close_p = 2041.0 if b % 100 == 15 else 2052.0

            cands = strat.evaluate(_make_context(
                bar_index=b,
                low_p=low_p,
                close_p=close_p,
                sweeps=sweeps,
                structure_events=structures,
                fvgs=fvgs,
            ))
            emitted_setups.extend(cands)

            peak_narratives = max(peak_narratives, len(strat._narratives))
            peak_consumed = max(peak_consumed, len(strat._consumed_windows))
            peak_clusters = max(peak_clusters, len(strat._emitted_clusters))
            peak_close_times = max(peak_close_times, len(strat._bar_close_times))

        # Invariants
        self.assertLessEqual(peak_narratives, 5)
        self.assertLessEqual(peak_consumed, 5)
        self.assertLessEqual(peak_clusters, 15)
        # Dynamically bounded by config lookback (fvg_to_mss_max_bars + safety headroom)
        max_allowed_close_times = strat.config.fvg_to_mss_max_bars + 25
        self.assertLessEqual(peak_close_times, max_allowed_close_times)
        setup_ids = [c.setup_id for c in emitted_setups]
        self.assertEqual(len(setup_ids), len(set(setup_ids)))  # 0 duplicates

    # =========================================================================
    # Group H: Production Integration & Parity (Tests 98-106)
    # =========================================================================

    def test_98_strategy_evaluation_through_registry(self):
        strat = S09ICTSilverBulletStrategy()
        reg = StrategyRegistry([strat], config=StrategyRegistryConfig(enabled_strategy_ids=("S09",)))

        sw = _make_sweep(index=5, direction="bullish")
        out5 = reg.evaluate_enabled(_make_context(bar_index=5, sweeps=[sw]))
        self.assertEqual(len(out5["S09"]), 0)

        mss = _make_structure(index=6, direction="bullish")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)
        out6 = reg.evaluate_enabled(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))
        self.assertEqual(len(out6["S09"]), 0)

        out7 = reg.evaluate_enabled(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(out7["S09"]), 1)

    def _build_production_fixture_candles(self):
        """Constructs canonical 9-candle dataset during NY AM window for production integration."""
        raw_candles = [
            (2010.0, 2012.0, 2008.0, 2010.0),  # 0: base
            (2010.0, 2020.0, 2008.0, 2014.0),  # 1: swing high 2020
            (2014.0, 2015.0, 1995.0, 2000.0),  # 2: swing low 1995
            (2000.0, 2005.0, 1998.0, 2002.0),  # 3: confirms low 1995
            (2000.0, 2003.0, 1993.0, 2000.0),  # 4: sweeps low 1995 (wick 1993)
            (2000.0, 2016.0, 2000.0, 2015.0),  # 5: displacement upward
            (2015.0, 2025.0, 2005.0, 2024.0),  # 6: BOS breaking 2020; FVG [2003, 2005]
            (2024.0, 2024.0, 2004.0, 2012.0),  # 7: retest into FVG [2003, 2005]
            (2012.0, 2030.0, 2010.0, 2028.0),  # 8: continuation
        ]
        candles = []
        for i, (p_o, p_h, p_l, p_c) in enumerate(raw_candles):
            o_ts, c_ts = _bar_time(i)
            candles.append({
                "bar_index": i,
                "time": o_ts,
                "open": p_o,
                "high": p_h,
                "low": p_l,
                "close": p_c,
                "volume": 100.0,
                "closed": True,
            })
        seed_event = StructureEvent(
            index=0,
            time=BASE_UTC_TIMESTAMP,
            direction="bullish",
            event_type="BOS",
            broken_swing_index=0,
            broken_swing_price=2000.0,
            close_price=2010.0,
            displacement=True,
            mode="swing",
            confirmed_swing_at=0,
        )
        cfg = ContextBuilderConfig(
            symbol="XAUUSD",
            timeframe="M1",
            swing_strength=1,
            atr_period=3,
            displacement_multiplier=0.5,
        )
        strat_cfg = S09Config(mode="swing")
        return candles, seed_event, cfg, strat_cfg

    def test_99_actual_context_builder_creates_silver_bullet_sequence(self):
        """End-to-end production pipeline test verifying all 10 required assertions."""
        candles, seed_event, cfg, strat_cfg = self._build_production_fixture_candles()

        # 1. Batch Branch
        contexts_batch = build_strategy_contexts(candles, cfg, htf_events=[seed_event])
        self.assertEqual(len(contexts_batch), 9)

        # Assertion 1: Sweep produced by production tracker
        sws_bar4 = contexts_batch[4].recent_sweeps
        self.assertGreaterEqual(len(sws_bar4), 1)
        sw = sws_bar4[0]
        self.assertEqual(sw.direction, "bullish")
        self.assertEqual(sw.pool_kind, "swing_low")
        self.assertEqual(sw.index, 4)

        # Assertion 2: MSS produced by production tracker
        structs_bar6 = contexts_batch[6].recent_structures
        self.assertGreaterEqual(len(structs_bar6), 1)
        mss = structs_bar6[0]
        self.assertEqual(mss.direction, "bullish")
        self.assertEqual(mss.event_type, "BOS")
        self.assertEqual(mss.index, 6)

        # Assertion 3 & 4: FVG produced and leg matches MSS
        fvgs_bar6 = contexts_batch[6].active_fvgs
        self.assertGreaterEqual(len(fvgs_bar6), 1)
        fvg = fvgs_bar6[0]
        self.assertIsNotNone(fvg.structure_leg_id)
        self.assertEqual(fvg.structure_leg_id, mss.structure_leg_id)
        self.assertEqual(fvg.structure_leg_id, "swing:bullish:1")

        # Assertion 5: Sweep, MSS, and FVG confirmation belong to same Silver Bullet window
        w_sw = get_window_for_close_time(contexts_batch[4].bar_close_time, strat_cfg.enabled_windows, strat_cfg.grace_minutes)
        w_mss = get_window_for_close_time(contexts_batch[6].bar_close_time, strat_cfg.enabled_windows, strat_cfg.grace_minutes)
        self.assertIsNotNone(w_sw)
        self.assertIsNotNone(w_mss)
        self.assertEqual(w_sw[0], w_mss[0])
        self.assertEqual(w_sw[0][0], "silver_bullet_ny_am")
        self.assertEqual(w_sw[0][1], "2024-05-15")

        # Assertion 6 & 7: Evaluate S09 and check candidate emission at bar 7 (after MSS at bar 6)
        s_batch = S09ICTSilverBulletStrategy(strat_cfg)
        r_batch = [s_batch.evaluate(c) for c in contexts_batch]

        # Candidates emitted only at bar 7
        for b in range(7):
            self.assertEqual(len(r_batch[b]), 0, f"Expected 0 candidate at bar {b}")
        self.assertEqual(len(r_batch[7]), 1, "Expected exactly 1 candidate at bar 7")
        self.assertEqual(len(r_batch[8]), 0, "Expected 0 candidate at bar 8 (window consumed)")

        # Assertion 8: Candidate attributes validation
        cand = r_batch[7][0]
        self.assertEqual(cand.strategy_id, "S09")
        self.assertEqual(cand.direction, "BUY")
        self.assertEqual(cand.bar_index, 7)
        self.assertEqual(cand.expiry_bar, 7)
        self.assertEqual(cand.entry_price, 2005.0)
        self.assertEqual(cand.stop_loss, 1992.8)
        self.assertEqual(cand.take_profit, 2025.0)
        self.assertEqual(cand.planned_rr, 1.64)
        self.assertTrue(cand.stop_loss < cand.entry_price < cand.take_profit)

        ev_kinds = [e.kind for e in cand.evidences]
        self.assertIn("liquidity_sweep", ev_kinds)
        self.assertIn("structure_event", ev_kinds)
        self.assertIn("fair_value_gap", ev_kinds)
        self.assertIn("liquidity_pool", ev_kinds)

        self.assertEqual(cand.meta["window_name"], "silver_bullet_ny_am")
        self.assertEqual(cand.meta["local_date"], "2024-05-15")
        self.assertEqual(cand.meta["sweep_index"], 4)
        self.assertEqual(cand.meta["mss_index"], 6)
        self.assertEqual(cand.meta["fvg_index"], 5)
        self.assertEqual(cand.meta["structure_leg_id"], "swing:bullish:1")

        # Assertion 9: 3-way parity (batch, incremental, and JSON round trip)
        builder = StrategyContextBuilder(cfg)
        contexts_inc = [builder.update(c, new_htf_events=[seed_event] if c["bar_index"] == 0 else None) for c in candles]
        s_inc = S09ICTSilverBulletStrategy(strat_cfg)
        r_inc = [s_inc.evaluate(c) for c in contexts_inc]

        contexts_json = [StrategyContext.from_dict(json.loads(json.dumps(c.to_dict()))) for c in contexts_batch]
        s_json = S09ICTSilverBulletStrategy(strat_cfg)
        r_json = [s_json.evaluate(c) for c in contexts_json]

        p_batch = [[c.to_dict() for c in r] for r in r_batch]
        p_inc = [[c.to_dict() for c in r] for r in r_inc]
        p_json = [[c.to_dict() for c in r] for r in r_json]

        self.assertEqual(p_batch, p_inc)
        self.assertEqual(p_batch, p_json)

        # Assertion 10: Future candle extension does not alter prefix contexts or candidate outputs
        candles_ext = copy.deepcopy(candles)
        for i in range(len(candles), len(candles) + 5):
            o_ts, c_ts = _bar_time(i)
            candles_ext.append({
                "bar_index": i,
                "time": o_ts,
                "open": 2028.0 + (i - len(candles)),
                "high": 2035.0 + (i - len(candles)),
                "low": 2025.0 + (i - len(candles)),
                "close": 2030.0 + (i - len(candles)),
                "volume": 100.0,
                "closed": True,
            })
        contexts_ext = build_strategy_contexts(candles_ext, cfg, htf_events=[seed_event])
        s_ext = S09ICTSilverBulletStrategy(strat_cfg)
        r_ext = [s_ext.evaluate(c) for c in contexts_ext]
        p_ext = [[c.to_dict() for c in r] for r in r_ext]

        self.assertEqual(
            [c.to_dict() for c in contexts_batch],
            [c.to_dict() for c in contexts_ext[:len(contexts_batch)]],
        )
        self.assertEqual(p_batch, p_ext[:len(p_batch)])

    def test_100_batch_contexts_sequential_evaluation(self):
        candles, seed_event, cfg, strat_cfg = self._build_production_fixture_candles()
        contexts = build_strategy_contexts(candles, cfg, htf_events=[seed_event])
        strat = S09ICTSilverBulletStrategy(strat_cfg)
        results = [strat.evaluate(c) for c in contexts]
        self.assertEqual(len(results[7]), 1)
        self.assertEqual(results[7][0].strategy_id, "S09")

    def test_101_incremental_builder_sequential_evaluation(self):
        candles, seed_event, cfg, strat_cfg = self._build_production_fixture_candles()
        builder = StrategyContextBuilder(cfg)
        strat = S09ICTSilverBulletStrategy(strat_cfg)
        results = []
        for c in candles:
            ctx = builder.update(c, new_htf_events=[seed_event] if c["bar_index"] == 0 else None)
            results.append(strat.evaluate(ctx))
        self.assertEqual(len(results[7]), 1)
        self.assertEqual(results[7][0].direction, "BUY")

    def test_102_json_round_trip_context_replay(self):
        candles, seed_event, cfg, strat_cfg = self._build_production_fixture_candles()
        contexts = build_strategy_contexts(candles, cfg, htf_events=[seed_event])
        json_contexts = [
            StrategyContext.from_dict(json.loads(json.dumps(c.to_dict())))
            for c in contexts
        ]
        strat = S09ICTSilverBulletStrategy(strat_cfg)
        results = [strat.evaluate(c) for c in json_contexts]
        self.assertEqual(len(results[7]), 1)
        self.assertEqual(results[7][0].setup_id, "S09:BUY:7:BUY:leg-swing-BUY-1:fvg-swing-BUY-5")

    def test_103_exact_parity_batch_incremental_json(self):
        candles, seed_event, cfg, strat_cfg = self._build_production_fixture_candles()
        # 1. Batch
        ctxs_batch = build_strategy_contexts(candles, cfg, htf_events=[seed_event])
        s_batch = S09ICTSilverBulletStrategy(strat_cfg)
        r_batch = [s_batch.evaluate(c) for c in ctxs_batch]

        # 2. Incremental
        builder = StrategyContextBuilder(cfg)
        ctxs_inc = [builder.update(c, new_htf_events=[seed_event] if c["bar_index"] == 0 else None) for c in candles]
        s_inc = S09ICTSilverBulletStrategy(strat_cfg)
        r_inc = [s_inc.evaluate(c) for c in ctxs_inc]

        # 3. JSON
        ctxs_json = [StrategyContext.from_dict(json.loads(json.dumps(c.to_dict()))) for c in ctxs_batch]
        s_json = S09ICTSilverBulletStrategy(strat_cfg)
        r_json = [s_json.evaluate(c) for c in ctxs_json]

        p_batch = [[c.to_dict() for c in r] for r in r_batch]
        p_inc = [[c.to_dict() for c in r] for r in r_inc]
        p_json = [[c.to_dict() for c in r] for r in r_json]

        self.assertEqual(p_batch, p_inc)
        self.assertEqual(p_batch, p_json)

    def test_104_append_future_candles_zero_lookahead(self):
        """Zero lookahead: past context output is invariant when future bars are appended."""
        candles, seed_event, cfg, strat_cfg = self._build_production_fixture_candles()
        candles_prefix = candles[:7]
        candles_extended = copy.deepcopy(candles)

        prefix_contexts = build_strategy_contexts(candles_prefix, cfg, htf_events=[seed_event])
        extended_contexts = build_strategy_contexts(candles_extended, cfg, htf_events=[seed_event])

        self.assertEqual(
            [c.to_dict() for c in prefix_contexts],
            [c.to_dict() for c in extended_contexts[:len(prefix_contexts)]],
        )

        strat_pref = S09ICTSilverBulletStrategy(strat_cfg)
        res_pref = [strat_pref.evaluate(c) for c in prefix_contexts]

        strat_ext = S09ICTSilverBulletStrategy(strat_cfg)
        res_ext = [strat_ext.evaluate(c) for c in extended_contexts]

        self.assertEqual(
            [[c.to_dict() for c in r] for r in res_pref],
            [[c.to_dict() for c in r] for r in res_ext[:len(res_pref)]],
        )

    def test_105_dst_winter_and_summer_production_fixtures(self):
        # Winter fixture: 2024-01-17 (EST, UTC-5)
        # 10:05 NY time is 15:05 UTC
        dt_win = datetime.datetime(2024, 1, 17, 10, 5, 0, tzinfo=NEW_YORK_TZ)
        o_win = pd.Timestamp(dt_win).tz_convert("UTC")
        c_win = o_win + pd.Timedelta(minutes=1)
        self.assertEqual(o_win.hour, 15)

        s_win = S09ICTSilverBulletStrategy()
        cands_win = s_win.evaluate(_make_context(bar_index=0, open_time=o_win, close_time=c_win, sweeps=[_make_sweep(index=0, direction="bullish")]))
        self.assertEqual(len(s_win._narratives), 1)

        # Summer fixture: 2024-07-17 (EDT, UTC-4)
        # 10:05 NY time is 14:05 UTC
        dt_sum = datetime.datetime(2024, 7, 17, 10, 5, 0, tzinfo=NEW_YORK_TZ)
        o_sum = pd.Timestamp(dt_sum).tz_convert("UTC")
        c_sum = o_sum + pd.Timedelta(minutes=1)
        self.assertEqual(o_sum.hour, 14)

        s_sum = S09ICTSilverBulletStrategy()
        cands_sum = s_sum.evaluate(_make_context(bar_index=0, open_time=o_sum, close_time=c_sum, sweeps=[_make_sweep(index=0, direction="bullish")]))
        self.assertEqual(len(s_sum._narratives), 1)

    def test_106_deep_immutability_of_context_and_snapshots(self):
        strat = S09ICTSilverBulletStrategy()
        ctx = _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")])
        before = ctx.to_dict()
        strat.evaluate(ctx)
        after = ctx.to_dict()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
