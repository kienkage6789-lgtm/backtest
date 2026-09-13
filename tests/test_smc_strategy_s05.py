"""
tests/test_smc_strategy_s05.py
==============================
Exhaustive 87-test suite for S05 BOS -> Order Block First Retest Strategy Template.
Covers Groups A through I adhering strictly to T53_5_S05_BOS_OB_RETEST_IMPLEMENTATION_PLAN.md
and ADR 21.
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

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
    LiquidityPoolSnapshot,
    OrderBlockSnapshot,
    StrategyContext,
    StrategyProfile,
    StructureEventSnapshot,
    make_cluster_id,
    make_evidence_id,
    make_setup_id,
)
from smc.engine.protocol import StrategyTemplate, validate_strategy_id
from smc.engine.registry import StrategyRegistry, StrategyRegistryConfig
from smc.engine.strategies.s05_bos_ob_retest import (
    BOSKey,
    OBKey,
    S05Config,
    S05BOSOBRetestStrategy,
    S05Narrative,
    S05NarrativeStage,
)
from smc.engine.strategies.s01_ict_2022 import S01Config


def _feed(strat: S05BOSOBRetestStrategy, context: StrategyContext) -> tuple[CandidateSetup, ...]:
    """Feed context sequentially into strategy, filling intermediate bars with neutral context if gap exists."""
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
    """Feed context sequentially into StrategyRegistry, filling intermediate bars with neutral context if gap exists."""
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


def _make_bias(bias: str = "bullish", bar_index: int = 10, as_of: Optional[pd.Timestamp] = None, source_event_time: Optional[pd.Timestamp] = None) -> BiasStateSnapshot:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=bar_index)
    return BiasStateSnapshot(
        bias=bias,
        timestamp=base_ts,
        as_of=as_of if as_of is not None else base_ts,
        source_event_time=source_event_time if source_event_time is not None else base_ts,
    )


def _make_context(
    bar_index: int = 10,
    open_p: Optional[float] = None,
    high_p: Optional[float] = None,
    low_p: Optional[float] = None,
    close_p: Optional[float] = None,
    structure_events: Sequence[StructureEventSnapshot] = (),
    obs: Sequence[OrderBlockSnapshot] = (),
    pools: Sequence[LiquidityPoolSnapshot] = (),
    bias: Optional[str] = "bullish",
    timeframe: str = "M1",
    htf_bias: Optional[BiasStateSnapshot] = None,
) -> StrategyContext:
    c = close_p if close_p is not None else 2052.0
    o = open_p if open_p is not None else c
    h = high_p if high_p is not None else max(o, c) + 2.0
    l = low_p if low_p is not None else min(o, c) - 2.0
    if h < max(o, c):
        h = max(o, c)
    if l > min(o, c):
        l = min(o, c)

    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=bar_index)
    if htf_bias is None and bias is not None:
        htf_bias = _make_bias(bias, bar_index)

    return StrategyContext(
        bar_index=bar_index,
        timestamp=base_ts,
        bar_close_time=base_ts + pd.Timedelta(minutes=1),
        symbol="XAUUSD",
        timeframe=timeframe,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=100.0,
        atr14=2.0,
        recent_structures=tuple(structure_events),
        active_obs=tuple(obs),
        active_pools=tuple(pools),
        htf_bias=htf_bias,
    )


def _make_bos(
    index: int = 10,
    direction: str = "bullish",
    broken_swing_index: int = 5,
    broken_swing_price: float = 2045.0,
    close_price: Optional[float] = None,
    displacement: bool = True,
    structure_leg_id: Optional[str] = "leg1",
    mode: str = "internal",
    break_type: str = "close",
) -> StructureEventSnapshot:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=index)
    cp = close_price if close_price is not None else (broken_swing_price + (1.0 if direction == "bullish" else -1.0))
    return StructureEventSnapshot(
        index=index,
        time=base_ts,
        direction=direction,
        event_type="BOS",
        broken_swing_index=broken_swing_index,
        broken_swing_price=broken_swing_price,
        close_price=cp,
        displacement=displacement,
        structure_leg_id=structure_leg_id,
        mode=mode,
        confirmed_swing_at=index,
        break_type=break_type,
    )


def _make_ob(
    index: int = 8,
    direction: str = "bullish",
    high: float = 2040.0,
    low: float = 2036.0,
    source_event_index: int = 10,
    created_at: int = 10,
    structure_leg_id: Optional[str] = "leg1",
    quality: str = "base",
    mode: str = "internal",
    origin_type: str = "BOS",
    source_event_type: str = "BOS",
    valid: bool = True,
    mitigated: bool = False,
    mitigated_at: Optional[int] = None,
    retest_count: int = 0,
    invalidated_at: Optional[int] = None,
) -> OrderBlockSnapshot:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=index)
    return OrderBlockSnapshot(
        index=index,
        time=base_ts,
        direction=direction,
        high=high,
        low=low,
        open=low,
        close=high,
        source_event_index=source_event_index,
        created_at=created_at,
        structure_leg_id=structure_leg_id,
        quality=quality,
        mode=mode,
        origin_type=origin_type,
        source_event_type=source_event_type,
        valid=valid,
        mitigated=mitigated,
        mitigated_at=mitigated_at,
        retest_count=retest_count,
        invalidated_at=invalidated_at,
    )


def _make_pool(
    price: float = 2065.0,
    kind: str = "equal_highs",
    indices: tuple[int, ...] = (5, 6),
    confirmed_at: int = 10,
    created_at: int = 8,
    mode: str = "internal",
    valid: bool = True,
    swept: bool = False,
) -> LiquidityPoolSnapshot:
    return LiquidityPoolSnapshot(
        kind=kind,
        price=price,
        price_max=price + 0.5,
        price_min=price - 0.5,
        indices=indices,
        created_at=created_at,
        confirmed_at=confirmed_at,
        mode=mode,
        valid=valid,
        swept=swept,
    )


class TestSMCS05Strategy(unittest.TestCase):
    """87-test exhaustive test suite for S05 BOS -> Order Block First Retest Strategy."""


    # =========================================================================
    # Group A: Config, Profile, Protocol (Tests 01-08)
    # =========================================================================

    def test_01_default_config_and_profile_exact_values(self):
        cfg = S05Config()
        self.assertEqual(cfg.mode, "internal")
        self.assertEqual(cfg.max_ob_age_bars, 25)
        self.assertEqual(cfg.entry_level, "proximal")
        self.assertEqual(cfg.sl_buffer_price, 0.20)
        self.assertEqual(cfg.min_rr, 1.50)
        self.assertEqual(cfg.fallback_rr, 2.00)
        self.assertTrue(cfg.require_displacement)
        self.assertEqual(cfg.min_ob_quality, "base")

        strat = S05BOSOBRetestStrategy(cfg)
        prof = strat.profile
        self.assertEqual(prof.strategy_id, "S05")
        self.assertEqual(prof.name, "BOS Order Block First Retest")
        self.assertEqual(prof.version, "1.0.0")
        self.assertEqual(prof.style, "continuation")
        self.assertEqual(prof.allowed_directions, ("BUY", "SELL"))
        self.assertEqual(prof.timeframes, ("M1", "M5", "M15"))
        self.assertEqual(prof.max_setup_age_bars, 25)
        self.assertEqual(prof.cooldown_bars, 3)
        self.assertEqual(prof.min_rr, 1.50)
        self.assertEqual(prof.params, cfg.to_dict())

    def test_02_config_json_round_trip_exact(self):
        cfg = S05Config(
            mode="swing",
            max_ob_age_bars=30,
            entry_level="ce_50",
            sl_buffer_price=0.50,
            min_rr=2.00,
            fallback_rr=3.00,
            require_displacement=False,
            min_ob_quality="strong",
        )
        d = cfg.to_dict()
        s = json.dumps(d)
        d2 = json.loads(s)
        cfg2 = S05Config.from_dict(d2)
        self.assertEqual(cfg, cfg2)

    def test_03_reject_bool_in_numeric_fields(self):
        with self.assertRaises(StrategyValidationError):
            S05Config(max_ob_age_bars=True)
        with self.assertRaises(StrategyValidationError):
            S05Config(sl_buffer_price=True)
        with self.assertRaises(StrategyValidationError):
            S05Config(min_rr=False)
        with self.assertRaises(StrategyValidationError):
            S05Config(fallback_rr=True)

    def test_04_reject_nan_inf_zero_negative(self):
        with self.assertRaises(StrategyValidationError):
            S05Config(max_ob_age_bars=0)
        with self.assertRaises(StrategyValidationError):
            S05Config(max_ob_age_bars=-5)
        with self.assertRaises(StrategyValidationError):
            S05Config(sl_buffer_price=0.0)
        with self.assertRaises(StrategyValidationError):
            S05Config(sl_buffer_price=-1.0)
        with self.assertRaises(StrategyValidationError):
            S05Config(sl_buffer_price=float("nan"))
        with self.assertRaises(StrategyValidationError):
            S05Config(sl_buffer_price=float("inf"))
        with self.assertRaises(StrategyValidationError):
            S05Config(min_rr=0.0)
        with self.assertRaises(StrategyValidationError):
            S05Config(min_rr=-2.0)
        with self.assertRaises(StrategyValidationError):
            S05Config(min_rr=float("nan"))
        with self.assertRaises(StrategyValidationError):
            S05Config(fallback_rr=float("inf"))

    def test_05_reject_unknown_mode_entry_level_quality(self):
        with self.assertRaises(StrategyValidationError):
            S05Config(mode="invalid_mode")
        with self.assertRaises(StrategyValidationError):
            S05Config(entry_level="close")
        with self.assertRaises(StrategyValidationError):
            S05Config(min_ob_quality="ultra_premium")

    def test_06_reject_fallback_rr_lower_than_min_rr(self):
        with self.assertRaises(StrategyValidationError):
            S05Config(min_rr=2.50, fallback_rr=2.00)

    def test_07_strategy_satisfies_protocol_and_runs_in_registry(self):
        # 1. Fail-fast constructor config validation
        strat_default = S05BOSOBRetestStrategy()
        self.assertIsInstance(strat_default, StrategyTemplate)
        self.assertIsInstance(strat_default.config, S05Config)

        strat_none = S05BOSOBRetestStrategy(None)
        self.assertIsInstance(strat_none.config, S05Config)

        strat_custom = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        self.assertEqual(strat_custom.config.mode, "internal")

        with self.assertRaises(StrategyValidationError):
            S05BOSOBRetestStrategy({})  # dict
        with self.assertRaises(StrategyValidationError):
            S05BOSOBRetestStrategy("invalid")  # str
        with self.assertRaises(StrategyValidationError):
            S05BOSOBRetestStrategy(object())  # generic object
        with self.assertRaises(StrategyValidationError):
            S05BOSOBRetestStrategy(S01Config())  # wrong strategy config

        # 2. Registry evaluation
        reg = StrategyRegistry(strategies=(strat_default,), config=StrategyRegistryConfig(enabled_strategy_ids=("S05",)))
        self.assertEqual(reg.enabled_strategy_ids, ("S05",))
        ctx = _make_context(10)
        res = reg.evaluate_enabled(ctx)
        self.assertIn("S05", res)
        self.assertIsInstance(res["S05"], tuple)

    def test_08_clean_package_exports_and_fresh_process_import(self):
        repo_root = Path(__file__).resolve().parents[1]
        code = (
            "import sys; "
            "from smc.engine.strategies import S05Config, S05BOSOBRetestStrategy; "
            "from smc.engine import S05Config as C2, S05BOSOBRetestStrategy as S2; "
            "assert S05Config is C2 and S05BOSOBRetestStrategy is S2; "
            "sys.exit(0)"
        )
        res = subprocess.run([sys.executable, "-c", code], cwd=repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Import failed: {res.stderr}")

    # =========================================================================
    # Group B: Happy Path and Symmetry (Tests 09-15)
    # =========================================================================

    def test_09_buy_happy_path(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", broken_swing_index=5, broken_swing_price=2045.0, structure_leg_id="leg1")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, structure_leg_id="leg1")

        # Bar 10: Ingest BOS and match OB
        res10 = _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(res10, ())

        # Bar 11: First retest of OB
        ob_retested = _make_ob(
            index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10,
            structure_leg_id="leg1", valid=True, mitigated=True, mitigated_at=11, retest_count=1,
        )
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_retested,), bias="bullish"))
        self.assertEqual(len(res11), 1)
        c = res11[0]
        self.assertEqual(c.strategy_id, "S05")
        self.assertEqual(c.direction, "BUY")
        self.assertEqual(c.bar_index, 11)
        self.assertEqual(c.entry_price, 2040.0)
        self.assertEqual(c.stop_loss, 2035.8)  # 2036.0 - 0.2
        self.assertEqual(c.planned_rr, 2.0)
        self.assertEqual(c.target_type, "fixed_rr")

    def test_10_sell_mirror_happy_path(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bearish", broken_swing_index=5, broken_swing_price=2035.0, structure_leg_id="leg1")
        ob = _make_ob(index=8, direction="bearish", high=2044.0, low=2040.0, source_event_index=10, created_at=10, structure_leg_id="leg1")

        # Bar 10: Ingest BOS and match OB
        res10 = _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bearish"))
        self.assertEqual(res10, ())

        # Bar 11: First retest of OB
        ob_retested = _make_ob(
            index=8, direction="bearish", high=2044.0, low=2040.0, source_event_index=10, created_at=10,
            structure_leg_id="leg1", valid=True, mitigated=True, mitigated_at=11, retest_count=1,
        )
        res11 = _feed(strat, _make_context(11, low_p=2037.0, high_p=2042.0, close_p=2039.0, structure_events=(bos,), obs=(ob_retested,), bias="bearish"))
        self.assertEqual(len(res11), 1)
        c = res11[0]
        self.assertEqual(c.strategy_id, "S05")
        self.assertEqual(c.direction, "SELL")
        self.assertEqual(c.bar_index, 11)
        self.assertEqual(c.entry_price, 2040.0)  # proximal for SELL is ob.low
        self.assertEqual(c.stop_loss, 2044.2)  # 2044.0 + 0.2
        self.assertEqual(c.planned_rr, 2.0)
        self.assertEqual(c.target_type, "fixed_rr")

    def test_11_reflection_symmetry(self):
        strat_buy = S05BOSOBRetestStrategy()
        strat_sell = S05BOSOBRetestStrategy()

        # Buy setup
        bos_b = _make_bos(index=10, direction="bullish", broken_swing_price=2045.0)
        ob_b = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat_buy, _make_context(10, structure_events=(bos_b,), obs=(ob_b,), bias="bullish"))

        ob_b_retested = _make_ob(
            index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10,
            valid=True, mitigated=True, mitigated_at=11, retest_count=1,
        )
        p_b = _make_pool(price=2065.0, kind="equal_highs")
        res_b = _feed(strat_buy, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos_b,), obs=(ob_b_retested,), pools=(p_b,), bias="bullish"))
        self.assertEqual(len(res_b), 1)
        cand_b = res_b[0]

        # Reflected sell setup: P' = 4000 - P
        bos_s = _make_bos(index=10, direction="bearish", broken_swing_price=4000 - 2045.0)
        ob_s = _make_ob(index=8, direction="bearish", high=4000 - 2036.0, low=4000 - 2040.0, source_event_index=10, created_at=10)
        _feed(strat_sell, _make_context(10, structure_events=(bos_s,), obs=(ob_s,), bias="bearish"))

        ob_s_retested = _make_ob(
            index=8, direction="bearish", high=4000 - 2036.0, low=4000 - 2040.0, source_event_index=10, created_at=10,
            valid=True, mitigated=True, mitigated_at=11, retest_count=1,
        )
        p_s = _make_pool(price=4000 - 2065.0, kind="equal_lows")
        res_s = _feed(strat_sell, _make_context(11, low_p=4000 - 2044.0, high_p=4000 - 2038.0, close_p=4000 - 2041.0, structure_events=(bos_s,), obs=(ob_s_retested,), pools=(p_s,), bias="bearish"))
        self.assertEqual(len(res_s), 1)
        cand_s = res_s[0]

        self.assertEqual(cand_b.planned_rr, cand_s.planned_rr)
        self.assertAlmostEqual(cand_b.entry_price, 4000.0 - cand_s.entry_price, places=3)
        self.assertAlmostEqual(cand_b.stop_loss, 4000.0 - cand_s.stop_loss, places=3)
        self.assertAlmostEqual(cand_b.take_profit, 4000.0 - cand_s.take_profit, places=3)

    def test_12_ce_50_entry_buy(self):
        cfg = S05Config(entry_level="ce_50")
        strat = S05BOSOBRetestStrategy(cfg)
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2037.0, high_p=2042.0, close_p=2039.0, structure_events=(bos,), obs=(ob_ret,), bias="bullish"))
        self.assertEqual(len(res), 1)
        # ce_50 = (2040.0 + 2036.0) / 2 = 2038.0
        self.assertEqual(res[0].entry_price, 2038.0)
        self.assertEqual(res[0].stop_loss, 2035.8)

    def test_13_ce_50_entry_sell(self):
        cfg = S05Config(entry_level="ce_50")
        strat = S05BOSOBRetestStrategy(cfg)
        bos = _make_bos(index=10, direction="bearish")
        ob = _make_ob(index=8, direction="bearish", high=2044.0, low=2040.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bearish"))

        ob_ret = _make_ob(index=8, direction="bearish", high=2044.0, low=2040.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2039.0, high_p=2043.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret,), bias="bearish"))
        self.assertEqual(len(res), 1)
        # ce_50 = (2044.0 + 2040.0) / 2 = 2042.0
        self.assertEqual(res[0].entry_price, 2042.0)
        self.assertEqual(res[0].stop_loss, 2044.2)

    def test_14_structural_pool_target(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        pool = _make_pool(price=2065.0, kind="equal_highs")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(pool,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret,), pools=(pool,), bias="bullish"))
        self.assertEqual(len(res), 1)
        c = res[0]
        self.assertEqual(c.target_type, "opposing_pool")
        self.assertEqual(c.take_profit, 2065.0)
        # risk = 2040.0 - 2035.8 = 4.2; reward = 2065.0 - 2040.0 = 25.0 -> rr = 25.0 / 4.2 = 5.95
        self.assertEqual(c.planned_rr, round(25.0 / 4.2, 2))

    def test_15_fixed_2r_fallback(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret,), pools=(), bias="bullish"))
        self.assertEqual(len(res), 1)
        c = res[0]
        self.assertEqual(c.target_type, "fixed_rr")
        self.assertEqual(c.planned_rr, 2.0)

    # =========================================================================
    # Group C: BOS Validation (Tests 16-24)
    # =========================================================================

    def test_16_missing_bos_no_candidate(self):
        strat = S05BOSOBRetestStrategy()
        ob_unret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res10 = _feed(strat, _make_context(10, structure_events=(), obs=(ob_unret,), bias="bullish"))
        self.assertEqual(res10, ())
        res11 = _feed(strat, _make_context(11, structure_events=(), obs=(ob_ret,), bias="bullish"))
        self.assertEqual(res11, ())

    def test_17_choch_cannot_substitute_for_bos(self):
        strat = S05BOSOBRetestStrategy()
        choch = StructureEventSnapshot(
            index=10, time=pd.Timestamp("2026-01-15 10:10:00+00:00"), direction="bullish", event_type="CHoCH",
            broken_swing_index=5, broken_swing_price=2045.0, close_price=2046.0, displacement=True, structure_leg_id="leg1",
            mode="internal", confirmed_swing_at=10, break_type="close",
        )
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(choch,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_18_direction_mismatch_bos_bias_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        # Bullish BOS with bearish HTF bias
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bearish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_19_neutral_or_missing_bias_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        # Neutral bias
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="neutral"))
        self.assertEqual(len(strat._narratives), 0)

    def test_20_mode_mismatch_rejected(self):
        strat = S05BOSOBRetestStrategy(S05Config(mode="swing"))
        bos = _make_bos(index=10, direction="bullish", mode="internal")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, mode="swing")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_21_wick_break_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", break_type="wick")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_22_displacement_false_rejected_by_default(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", displacement=False)
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_23_displacement_false_passes_when_configured(self):
        strat = S05BOSOBRetestStrategy(S05Config(require_displacement=False))
        bos = _make_bos(index=10, direction="bullish", displacement=False)
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 1)

    def test_24_bos_rolling_buffer_old_event_not_ingested_retrospectively(self):
        strat = S05BOSOBRetestStrategy()
        # Evaluate bar 10 with empty events
        _feed(strat, _make_context(10, structure_events=(), bias="bullish"))
        # At bar 12, a rolling buffer contains old BOS from bar 10
        bos_old = _make_bos(index=10, direction="bullish")
        _feed(strat, _make_context(12, structure_events=(bos_old,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    # =========================================================================
    # Group D: BOS/OB Linkage (Tests 25-35)
    # =========================================================================

    def test_25_exact_source_event_match_passes(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", structure_leg_id=None)
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, structure_leg_id=None)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 1)
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.stage, S05NarrativeStage.OB_READY)
        self.assertEqual(narr.linkage_method, "exact_source_event")

    def test_26_same_leg_fallback_passes_when_exact_absent(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", structure_leg_id="leg_alpha")
        # ob source_event_index is -1 or different, but structure_leg_id matches
        ob = _make_ob(index=8, direction="bullish", source_event_index=-1, created_at=10, structure_leg_id="leg_alpha")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 1)
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.stage, S05NarrativeStage.OB_READY)
        self.assertEqual(narr.linkage_method, "same_leg_fallback")

    def test_27_none_equals_none_not_same_leg(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", structure_leg_id=None)
        # ob source_event_index is different and structure_leg_id is None
        ob = _make_ob(index=8, direction="bullish", source_event_index=99, created_at=10, structure_leg_id=None)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        # Stays in BOS_SEEN because None == None is rejected for same-leg fallback
        self.assertEqual(narr.stage, S05NarrativeStage.BOS_SEEN)
        self.assertIsNone(narr.ob)

    def test_28_wrong_direction_ob_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", structure_leg_id="leg1")
        ob_bear = _make_ob(index=8, direction="bearish", source_event_index=10, created_at=10, structure_leg_id="leg1")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_bear,), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.stage, S05NarrativeStage.BOS_SEEN)

    def test_29_wrong_mode_ob_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", mode="internal")
        ob_swing = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, mode="swing")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_swing,), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.stage, S05NarrativeStage.BOS_SEEN)

    def test_30_wrong_origin_or_source_event_type_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob_choch = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, origin_type="CHoCH", source_event_type="CHoCH")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_choch,), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.stage, S05NarrativeStage.BOS_SEEN)

    def test_31_ob_created_before_bos_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob_old = _make_ob(index=6, direction="bullish", source_event_index=8, created_at=8)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_old,), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.stage, S05NarrativeStage.BOS_SEEN)

    def test_32_future_created_at_raises_state_error(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob_future = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=12)
        with self.assertRaises(StrategyStateError):
            _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_future,), bias="bullish"))

    def test_33_exact_match_prioritized_over_fallback(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", structure_leg_id="leg1")
        # ob1: same-leg fallback (source_event_index=99)
        ob_fallback = _make_ob(index=7, direction="bullish", source_event_index=99, created_at=10, structure_leg_id="leg1")
        # ob2: exact match (source_event_index=10)
        ob_exact = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, structure_leg_id="leg1")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_fallback, ob_exact), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.linkage_method, "exact_source_event")
        self.assertEqual(narr.ob.source_event_index, 10)

    def test_34_multiple_ob_canonical_tie_break_deterministic(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", structure_leg_id="leg1")
        ob1 = _make_ob(index=7, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, quality="base")
        ob2 = _make_ob(index=8, direction="bullish", high=2041.0, low=2037.0, source_event_index=10, created_at=10, quality="strong")
        # Stronger quality (ob2) should win tie-break
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob1, ob2), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.ob.quality, "strong")
        self.assertEqual(narr.ob.index, 8)

    def test_35_duplicate_ob_ownership_resolution(self):
        strat = S05BOSOBRetestStrategy()
        # Two BOS events on different bars, both claiming the same OB via same-leg
        bos1 = _make_bos(index=9, direction="bullish", broken_swing_index=4, structure_leg_id="leg1")
        bos2 = _make_bos(index=10, direction="bullish", broken_swing_index=5, structure_leg_id="leg1")
        ob = _make_ob(index=8, direction="bullish", source_event_index=-1, created_at=10, structure_leg_id="leg1")

        _feed(strat, _make_context(9, structure_events=(bos1,), obs=(), bias="bullish"))
        _feed(strat, _make_context(10, structure_events=(bos2,), obs=(ob,), bias="bullish"))

        # At bar 11, first retest happens
        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=-1, created_at=10, structure_leg_id="leg1", valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        # Exactly 1 candidate emitted for the winning BOS (larger bos.index = 10)
        self.assertEqual(len(res), 1)
        self.assertIn("10-5", res[0].evidence_cluster_id)

    # =========================================================================
    # Group E: First Retest Airtight Boundary (Tests 36-47)
    # =========================================================================

    def test_36_exact_four_field_contract_passes(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1, invalidated_at=None)
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret,), bias="bullish"))
        self.assertEqual(len(res11), 1)

    def test_37_mitigated_at_less_than_n_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Already mitigated at bar 10 (mitigated_at=10 < 11)
        ob_old_mit = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=10, retest_count=1)
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_old_mit,), bias="bullish"))
        self.assertEqual(res11, ())

    def test_38_mitigated_at_greater_than_n_future_leak_raises(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob_future = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=12, retest_count=1)
        with self.assertRaises(StrategyStateError):
            _feed(strat, _make_context(11, structure_events=(bos,), obs=(ob_future,), bias="bullish"))

    def test_39_retest_count_zero_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, retest_count=0)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        res11 = _feed(strat, _make_context(11, structure_events=(bos,), obs=(ob,), bias="bullish"))
        self.assertEqual(res11, ())

    def test_40_retest_count_greater_than_one_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret2 = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=2)
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret2,), bias="bullish"))
        self.assertEqual(res11, ())

    def test_41_valid_false_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_inval = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=False, mitigated=True, mitigated_at=11, retest_count=1)
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_inval,), bias="bullish"))
        self.assertEqual(res11, ())

    def test_42_invalidated_at_n_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_inval = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1, invalidated_at=11)
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_inval,), bias="bullish"))
        self.assertEqual(res11, ())

    def test_43_touch_plus_close_break_same_bar_no_emission(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Bar 11: touched zone but closed below low=2036.0 (e.g. close=2034.0), tracker invalidates OB (valid=False, invalidated_at=11)
        ob_broken = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=False, mitigated=True, mitigated_at=11, retest_count=1, invalidated_at=11)
        res11 = _feed(strat, _make_context(11, low_p=2033.0, high_p=2039.0, close_p=2034.0, structure_events=(bos,), obs=(ob_broken,), bias="bullish"))
        self.assertEqual(res11, ())

    def test_44_retest_at_created_at_bar_rejected_next_bar_passes(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        # At bar 10, OB has created_at=10 and retest_count=1, mitigated_at=10
        ob_same = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=10, retest_count=1)
        res10 = _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_same,), bias="bullish"))
        # N == created_at -> rejected!
        self.assertEqual(res10, ())

        # If created at bar 9, retested at bar 10 -> N > created_at passes!
        strat2 = S05BOSOBRetestStrategy()
        bos2 = _make_bos(index=9, direction="bullish")
        ob_prev = _make_ob(index=8, direction="bullish", source_event_index=9, created_at=9)
        _feed(strat2, _make_context(9, structure_events=(bos2,), obs=(ob_prev,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=9, created_at=9, valid=True, mitigated=True, mitigated_at=10, retest_count=1)
        res10 = _feed(strat2, _make_context(10, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos2,), obs=(ob_ret,), bias="bullish"))
        self.assertEqual(len(res10), 1)

    def test_45_snapshot_stale_refresh(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob_valid = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob_valid,), bias="bullish"))

        # At bar 11, the OB was invalidated in tracker (valid=False, invalidated_at=11)
        ob_invalid = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=False, invalidated_at=11)
        res11 = _feed(strat, _make_context(11, structure_events=(bos,), obs=(ob_invalid,), bias="bullish"))
        self.assertEqual(res11, ())
        self.assertEqual(len(strat._narratives), 0)

    def test_46_missing_ob_from_active_obs_no_false_candidate(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # At bar 11, active_obs is empty (e.g. capacity eviction)
        res11 = _feed(strat, _make_context(11, structure_events=(bos,), obs=(), bias="bullish"))
        self.assertEqual(res11, ())

    def test_47_duplicate_ob_snapshots_no_duplicate_candidate(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        # Pass identical OB twice in context.active_obs
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret, ob_ret), bias="bullish"))
        self.assertEqual(len(res11), 1)

    # =========================================================================
    # Group F: Expiry and Invalidation (Tests 48-57)
    # =========================================================================

    def test_48_age_24_bars_passes(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Age 24: created_at=10, retest at bar 34 (34 - 10 = 24 <= 25)
        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=34, retest_count=1)
        res34 = _feed(strat, _make_context(34, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        self.assertEqual(len(res34), 1)

    def test_49_age_25_bars_inclusive_passes(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Age 25: created_at=10, retest at bar 35 (35 == 10 + 25)
        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=35, retest_count=1)
        res35 = _feed(strat, _make_context(35, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        self.assertEqual(len(res35), 1)

    def test_50_age_26_bars_expired(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Age 26: created_at=10, retest at bar 36 (36 > 10 + 25)
        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=36, retest_count=1)
        res36 = _feed(strat, _make_context(36, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        self.assertEqual(res36, ())
        self.assertEqual(len(strat._narratives), 0)

    def test_51_opposite_choch_after_bos_invalidates(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Opposite CHoCH at bar 11
        choch_opp = StructureEventSnapshot(
            index=11, time=pd.Timestamp("2026-01-15 10:11:00+00:00"), direction="bearish", event_type="CHoCH",
            broken_swing_index=6, broken_swing_price=2048.0, close_price=2047.0, displacement=True, structure_leg_id="leg2", mode="internal",
            confirmed_swing_at=11, break_type="close",
        )
        _feed(strat, _make_context(11, structure_events=(choch_opp,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_52_opposite_bos_after_bos_invalidates(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Opposite BOS at bar 11
        bos_opp = _make_bos(index=11, direction="bearish")
        _feed(strat, _make_context(11, structure_events=(bos_opp,), obs=(ob,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_53_opposite_event_at_retest_bar_invalidates_before_emission(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Retest occurs at bar 12, but opposite CHoCH also occurs at bar 12
        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=12, retest_count=1)
        choch_opp = StructureEventSnapshot(
            index=12, time=pd.Timestamp("2026-01-15 10:12:00+00:00"), direction="bearish", event_type="CHoCH",
            broken_swing_index=7, broken_swing_price=2050.0, close_price=2049.0, displacement=True, structure_leg_id="leg2", mode="internal",
            confirmed_swing_at=12, break_type="close",
        )
        res12 = _feed(strat, _make_context(12, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(choch_opp,), obs=(ob_ret,), bias="bullish"))
        self.assertEqual(res12, ())
        self.assertEqual(len(strat._narratives), 0)

    def test_54_opposite_event_at_bos_bar_ambiguous_rejected(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        choch_opp = StructureEventSnapshot(
            index=10, time=pd.Timestamp("2026-01-15 10:10:00+00:00"), direction="bearish", event_type="CHoCH",
            broken_swing_index=4, broken_swing_price=2042.0, close_price=2041.0, displacement=True, structure_leg_id="leg2", mode="internal",
            confirmed_swing_at=10, break_type="close",
        )
        _feed(strat, _make_context(10, structure_events=(bos, choch_opp), bias="bullish"))
        self.assertEqual(len(strat._narratives), 0)

    def test_55_same_direction_event_does_not_invalidate(self):
        strat = S05BOSOBRetestStrategy()
        bos1 = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos1,), obs=(ob,), bias="bullish"))

        # Another bullish BOS at bar 11
        bos2 = _make_bos(index=11, direction="bullish", broken_swing_index=6)
        _feed(strat, _make_context(11, structure_events=(bos2,), obs=(ob,), bias="bullish"))
        narr1 = strat._narratives.get((bos1.mode, bos1.direction, bos1.index, bos1.broken_swing_index, bos1.structure_leg_id))
        self.assertIsNotNone(narr1)
        self.assertEqual(narr1.stage, S05NarrativeStage.OB_READY)

    def test_56_different_mode_opposite_event_does_not_invalidate(self):
        strat = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        bos = _make_bos(index=10, direction="bullish", mode="internal")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, mode="internal")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Opposite event in swing mode (not internal)
        swing_opp = _make_bos(index=11, direction="bearish", mode="swing")
        _feed(strat, _make_context(11, structure_events=(swing_opp,), obs=(ob,), bias="bullish"))
        narr = next(iter(strat._narratives.values()))
        self.assertEqual(narr.stage, S05NarrativeStage.OB_READY)

    def test_57_bias_change_terminal_no_revival(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Bar 11: bias becomes neutral
        _feed(strat, _make_context(11, structure_events=(bos,), obs=(ob,), bias="neutral"))
        self.assertEqual(len(strat._narratives), 0)

        # Bar 12: bias returns to bullish, but narrative does not revive!
        ob_ret = _make_ob(index=8, direction="bullish", source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=12, retest_count=1)
        res12 = _feed(strat, _make_context(12, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret,), bias="bullish"))
        self.assertEqual(res12, ())
        self.assertEqual(len(strat._narratives), 0)

    # =========================================================================
    # Group G: Entry, Target, Precision, IDs (Tests 58-69)
    # =========================================================================

    def test_58_buy_distal_sl(self):
        cfg = S05Config(sl_buffer_price=0.35)
        strat = S05BOSOBRetestStrategy(cfg)
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2035.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2035.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2037.0, high_p=2042.0, close_p=2039.0, obs=(ob_ret,), bias="bullish"))
        self.assertEqual(len(res), 1)
        # 2035.0 - 0.35 = 2034.65
        self.assertAlmostEqual(res[0].stop_loss, 2034.65, places=3)

    def test_59_sell_distal_sl(self):
        cfg = S05Config(sl_buffer_price=0.45)
        strat = S05BOSOBRetestStrategy(cfg)
        bos = _make_bos(index=10, direction="bearish")
        ob = _make_ob(index=8, direction="bearish", high=2045.0, low=2040.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bearish"))

        ob_ret = _make_ob(index=8, direction="bearish", high=2045.0, low=2040.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2042.0, close_p=2040.0, obs=(ob_ret,), bias="bearish"))
        self.assertEqual(len(res), 1)
        # 2045.0 + 0.45 = 2045.45
        self.assertAlmostEqual(res[0].stop_loss, 2045.45, places=3)

    def test_60_nearest_opposing_pool_independent_of_order(self):
        strat1 = S05BOSOBRetestStrategy()
        strat2 = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)

        p_near = _make_pool(price=2055.0, kind="equal_highs", indices=(3, 4))
        p_far = _make_pool(price=2075.0, kind="equal_highs", indices=(1, 2))

        # Order 1: (p_far, p_near)
        _feed(strat1, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(p_far, p_near), bias="bullish"))
        ob_ret1 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res1 = _feed(strat1, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret1,), pools=(p_far, p_near), bias="bullish"))

        # Order 2: (p_near, p_far)
        _feed(strat2, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(p_near, p_far), bias="bullish"))
        ob_ret2 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res2 = _feed(strat2, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret2,), pools=(p_near, p_far), bias="bullish"))

        self.assertEqual(res1[0].take_profit, 2055.0)
        self.assertEqual(res2[0].take_profit, 2055.0)
        self.assertEqual(res1[0].to_dict(), res2[0].to_dict())

    def test_61_ineligible_pools_ignored(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        # Swept pool, invalid pool, wrong mode pool
        p_swept = _make_pool(price=2055.0, swept=True)
        p_invalid = _make_pool(price=2056.0, valid=False)
        p_swing = _make_pool(price=2057.0, mode="swing")

        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(p_swept, p_invalid, p_swing), bias="bullish"))
        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), pools=(p_swept, p_invalid, p_swing), bias="bullish"))
        # Ineligible pools ignored -> fallback to fixed RR
        self.assertEqual(res[0].target_type, "fixed_rr")

    def test_62_nearest_pool_rr_below_min_fallback_no_skip(self):
        strat = S05BOSOBRetestStrategy(S05Config(min_rr=1.5, fallback_rr=2.0))
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        # risk = 2040.0 - 2035.8 = 4.2.
        # p_near price = 2044.0 -> reward = 4.0 -> RR = 4.0 / 4.2 = 0.95 < min_rr 1.5
        p_near = _make_pool(price=2044.0, indices=(3, 4))
        # p_far price = 2060.0 -> reward = 20.0 -> RR = 4.76 >= 1.5
        p_far = _make_pool(price=2060.0, indices=(1, 2))

        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(p_near, p_far), bias="bullish"))
        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), pools=(p_near, p_far), bias="bullish"))
        # Contract: Must fallback to fixed_rr, NEVER skip to farther pool!
        self.assertEqual(res[0].target_type, "fixed_rr")
        self.assertEqual(res[0].planned_rr, 2.0)

    def test_63_post_rounding_geometry_collapse_fails_closed(self):
        strat = S05BOSOBRetestStrategy(S05Config(sl_buffer_price=0.0001))
        bos = _make_bos(index=10, direction="bullish")
        # Extremely thin zone where rounding could collapse stop == entry
        ob = _make_ob(index=8, direction="bullish", high=2040.0001, low=2040.0001, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0001, low=2040.0001, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        # Fails closed, returns empty tuple
        self.assertEqual(res, ())

    def test_64_planned_rr_recalculated_from_rounded_levels(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.1234, low=2036.5678, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.1234, low=2036.5678, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        self.assertEqual(len(res), 1)
        c = res[0]
        # entry = 2040.123, stop = round(2036.568 - 0.20, 3) = 2036.368
        # risk = 2040.123 - 2036.368 = 3.755
        # reward = round(3.755 * 2.0, 3) = 7.51
        # take_profit = 2040.123 + 7.51 = 2047.633
        # planned_rr = round(7.51 / 3.755, 2) = 2.0
        self.assertEqual(c.entry_price, 2040.123)
        self.assertEqual(c.stop_loss, 2036.368)
        self.assertEqual(c.take_profit, 2047.633)
        self.assertEqual(c.planned_rr, 2.0)

    def test_65_evidences_tuple_order(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        pool = _make_pool(price=2065.0, kind="equal_highs")
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(pool,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), pools=(pool,), bias="bullish"))
        self.assertEqual(len(res), 1)
        evs = res[0].evidences
        self.assertEqual(len(evs), 3)
        self.assertEqual(evs[0].kind, "structure_event")
        self.assertEqual(evs[1].kind, "order_block")
        self.assertEqual(evs[2].kind, "liquidity_pool")
        # Provenance timestamp is preserved from pool (None when not provided), never forged from context.timestamp
        self.assertIsNone(evs[2].time)
        self.assertNotEqual(evs[2].time, res[0].timestamp)

    def test_66_bos_evidence_id_injective(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", broken_swing_index=5)
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        base_id = res[0].evidences[0].evidence_id
        self.assertEqual(base_id, "structure_event:internal:10:BOS_bullish_5")

        # Injective test: different bar_index
        diff_bar_id = make_evidence_id("structure_event", "internal", 11, "BOS_bullish_5")
        self.assertNotEqual(base_id, diff_bar_id)

        # Injective test: same bar, different direction
        diff_dir_id = make_evidence_id("structure_event", "internal", 10, "BOS_bearish_5")
        self.assertNotEqual(base_id, diff_dir_id)

        # Injective test: same bar, same direction, different broken_swing
        diff_swing_id = make_evidence_id("structure_event", "internal", 10, "BOS_bullish_6")
        self.assertNotEqual(base_id, diff_swing_id)

    def test_67_ob_evidence_id_injective_float_free(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        ob_ev_id = res[0].evidences[1].evidence_id
        self.assertEqual(ob_ev_id, "order_block:internal:10:ob_bullish_10_8")

        # Injective test: two OBs with different source candle same price
        ob_ev_id_diff_source = make_evidence_id("order_block", "internal", 10, "ob_bullish_9_8")
        self.assertNotEqual(ob_ev_id, ob_ev_id_diff_source)

        ob_ev_id_diff_index = make_evidence_id("order_block", "internal", 10, "ob_bullish_10_7")
        self.assertNotEqual(ob_ev_id, ob_ev_id_diff_index)

        # Ensure no float representation (. or e) in ID token
        tokens = ob_ev_id.split(":")
        for tok in tokens:
            self.assertNotIn("2040.0", tok)
            self.assertNotIn("2036.0", tok)
            self.assertNotIn(".", tok)

    def test_68_pool_evidence_id_permutation_invariant(self):
        strat1 = S05BOSOBRetestStrategy()
        strat2 = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)

        # Permuted indices (4, 3) vs (3, 4)
        p1 = _make_pool(price=2065.0, kind="equal_highs", indices=(4, 3), confirmed_at=10)
        p2 = _make_pool(price=2065.0, kind="equal_highs", indices=(3, 4), confirmed_at=10)

        _feed(strat1, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(p1,), bias="bullish"))
        ob_ret1 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res1 = _feed(strat1, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret1,), pools=(p1,), bias="bullish"))

        _feed(strat2, _make_context(10, structure_events=(bos,), obs=(ob,), pools=(p2,), bias="bullish"))
        ob_ret2 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res2 = _feed(strat2, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret2,), pools=(p2,), bias="bullish"))

        self.assertEqual(res1[0].evidences[2].evidence_id, res2[0].evidences[2].evidence_id)
        self.assertEqual(res1[0].evidences[2].evidence_id, "liquidity_pool:internal:10:target_pool_equal_highs_3_4")

        # Injective test: different pool kind
        diff_kind_id = make_evidence_id("liquidity_pool", "internal", 10, "target_pool_equal_lows_3_4")
        self.assertNotEqual(res1[0].evidences[2].evidence_id, diff_kind_id)

        # Injective test: different pool indices
        diff_idx_id = make_evidence_id("liquidity_pool", "internal", 10, "target_pool_equal_highs_3_5")
        self.assertNotEqual(res1[0].evidences[2].evidence_id, diff_idx_id)

    def test_69_cluster_id_setup_id_deterministic_no_duplicate_emission(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish", broken_swing_index=5)
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Bar 11: first retest -> emits
        ob_ret1 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret1,), bias="bullish"))
        self.assertEqual(len(res11), 1)
        c = res11[0]
        self.assertEqual(c.evidence_cluster_id, "BUY:leg-internal-BUY-10-5:ob-internal-BUY-10-8")
        self.assertEqual(c.setup_id, "S05:BUY:11:BUY:leg-internal-BUY-10-5:ob-internal-BUY-10-8")

        # Injective cluster tests
        c_diff_leg = make_cluster_id("BUY", "leg-internal-BUY-10-6", "ob-internal-BUY-10-8")
        self.assertNotEqual(c.evidence_cluster_id, c_diff_leg)

        c_diff_ob = make_cluster_id("BUY", "leg-internal-BUY-10-5", "ob-internal-BUY-10-7")
        self.assertNotEqual(c.evidence_cluster_id, c_diff_ob)

        # Injective setup tests across strategy, direction, bar, cluster
        self.assertNotEqual(c.setup_id, make_setup_id("S01", "BUY", 11, c.evidence_cluster_id))
        self.assertNotEqual(c.setup_id, make_setup_id("S05", "SELL", 11, c.evidence_cluster_id))
        self.assertNotEqual(c.setup_id, make_setup_id("S05", "BUY", 12, c.evidence_cluster_id))
        self.assertNotEqual(c.setup_id, make_setup_id("S05", "BUY", 11, c_diff_leg))

        # Bar 12: second retest attempt on same cluster -> blocked!
        ob_ret2 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=12, retest_count=1)
        res12 = _feed(strat, _make_context(12, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret2,), bias="bullish"))
        self.assertEqual(res12, ())

    # =========================================================================
    # Group H: State, Concurrency, Atomicity (Tests 70-79)
    # =========================================================================

    def test_70_two_independent_bos_tracked_concurrently(self):
        strat = S05BOSOBRetestStrategy()
        bos1 = _make_bos(index=10, direction="bullish", broken_swing_index=5, structure_leg_id="leg_A")
        ob1 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, structure_leg_id="leg_A")

        bos2 = _make_bos(index=10, direction="bullish", broken_swing_index=7, structure_leg_id="leg_B")
        ob2 = _make_ob(index=9, direction="bullish", high=2042.0, low=2038.0, source_event_index=10, created_at=10, structure_leg_id="leg_B")

        _feed(strat, _make_context(10, structure_events=(bos1, bos2), obs=(ob1, ob2), bias="bullish"))
        self.assertEqual(len(strat._narratives), 2)

    def test_71_buy_and_sell_narratives_coexist_bias_gates(self):
        strat = S05BOSOBRetestStrategy()
        # Bar 10: bullish bias, BOS buy ingested
        bos_buy = _make_bos(index=10, direction="bullish", structure_leg_id="leg_buy")
        ob_buy = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, structure_leg_id="leg_buy")
        _feed(strat, _make_context(10, structure_events=(bos_buy,), obs=(ob_buy,), bias="bullish"))
        self.assertEqual(len(strat._narratives), 1)

        # Bar 11: retest buy succeeds with bullish bias
        ob_buy_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, structure_leg_id="leg_buy", valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res11 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_buy_ret,), bias="bullish"))
        self.assertEqual(len(res11), 1)
        self.assertEqual(res11[0].direction, "BUY")

    def test_72_multiple_candidates_same_bar_sorted_canonically(self):
        strat = S05BOSOBRetestStrategy()
        # Two distinct BUY narratives on separate legs/OBs
        bos1 = _make_bos(index=10, direction="bullish", broken_swing_index=5, structure_leg_id="leg_Z")
        ob1 = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=-1, created_at=10, structure_leg_id="leg_Z")

        bos2 = _make_bos(index=10, direction="bullish", broken_swing_index=3, structure_leg_id="leg_A")
        ob2 = _make_ob(index=9, direction="bullish", high=2042.0, low=2038.0, source_event_index=-1, created_at=10, structure_leg_id="leg_A")

        _feed(strat, _make_context(10, structure_events=(bos1, bos2), obs=(ob1, ob2), bias="bullish"))

        # Both retest at bar 11
        ob1_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=-1, created_at=10, structure_leg_id="leg_Z", valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        ob2_ret = _make_ob(index=9, direction="bullish", high=2042.0, low=2038.0, source_event_index=-1, created_at=10, structure_leg_id="leg_A", valid=True, mitigated=True, mitigated_at=11, retest_count=1)

        res = _feed(strat, _make_context(11, low_p=2037.0, high_p=2043.0, close_p=2040.0, obs=(ob1_ret, ob2_ret), bias="bullish"))
        self.assertEqual(len(res), 2)
        # Verify canonical sorting
        self.assertLess(res[0].evidence_cluster_id, res[1].evidence_cluster_id)

    def test_73_identical_retry_returns_exact_cached(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        ctx11 = _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish")

        res1 = strat.evaluate(ctx11)
        res2 = strat.evaluate(ctx11)
        self.assertIs(res1, res2)

    def test_74_conflicting_retry_raises_state_error(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ctx11_a = _make_context(11, close_p=2041.0, bias="bullish")
        ctx11_b = _make_context(11, close_p=2045.0, bias="bullish")

        strat.evaluate(ctx11_a)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(ctx11_b)
        self.assertEqual(strat._last_bar_index, 11)

    def test_75_non_monotonic_bar_gap_or_backward_raises(self):
        strat = S05BOSOBRetestStrategy()
        c10 = _make_context(10)
        strat.evaluate(c10)

        # Gap: jumping directly to 12
        c12 = _make_context(12)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(c12)
        self.assertEqual(strat._last_bar_index, 10)

        # Backward: going to 9
        c9 = _make_context(9)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(c9)
        self.assertEqual(strat._last_bar_index, 10)

    def test_76_exception_during_matching_does_not_commit_state(self):
        strat = S05BOSOBRetestStrategy()
        bos1 = _make_bos(index=10, direction="bullish")
        _feed(strat, _make_context(10, structure_events=(bos1,), bias="bullish"))

        # Save complete pre-call state snapshot
        snap_narratives = copy.deepcopy(strat._narratives)
        snap_emitted = copy.deepcopy(strat._emitted_clusters)
        snap_last_bar = strat._last_bar_index
        snap_last_ts = strat._last_timestamp
        snap_last_payload = copy.deepcopy(strat._last_context_payload)
        snap_last_result = copy.deepcopy(strat._last_result)

        # Ingest new BOS at bar 11, but mock matcher to raise RuntimeError
        bos2 = _make_bos(index=11, direction="bullish", broken_swing_index=6)
        ctx11 = _make_context(11, structure_events=(bos2,), bias="bullish")
        with patch.object(strat, "_find_canonical_ob_for_bos", side_effect=RuntimeError("Injected failure in matcher")):
            with self.assertRaises(RuntimeError):
                strat.evaluate(ctx11)

        # Assert full pre-call state snapshot equality
        self.assertEqual(strat._narratives, snap_narratives)
        self.assertEqual(strat._emitted_clusters, snap_emitted)
        self.assertEqual(strat._last_bar_index, snap_last_bar)
        self.assertEqual(strat._last_timestamp, snap_last_ts)
        self.assertEqual(strat._last_context_payload, snap_last_payload)
        self.assertEqual(strat._last_result, snap_last_result)

    def test_77_exception_during_candidate_building_does_not_commit(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        # Save complete pre-call state snapshot
        snap_narratives = copy.deepcopy(strat._narratives)
        snap_emitted = copy.deepcopy(strat._emitted_clusters)
        snap_last_bar = strat._last_bar_index
        snap_last_ts = strat._last_timestamp
        snap_last_payload = copy.deepcopy(strat._last_context_payload)
        snap_last_result = copy.deepcopy(strat._last_result)

        # At bar 11, first retest occurs, but mock candidate builder to raise RuntimeError
        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        ctx11 = _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish")
        with patch.object(strat, "_build_candidate", side_effect=RuntimeError("Injected failure in candidate builder")):
            with self.assertRaises(RuntimeError):
                strat.evaluate(ctx11)

        # Assert full pre-call state snapshot equality
        self.assertEqual(strat._narratives, snap_narratives)
        self.assertEqual(strat._emitted_clusters, snap_emitted)
        self.assertEqual(strat._last_bar_index, snap_last_bar)
        self.assertEqual(strat._last_timestamp, snap_last_ts)
        self.assertEqual(strat._last_context_payload, snap_last_payload)
        self.assertEqual(strat._last_result, snap_last_result)

    def test_78_reset_clears_state_replay_identical(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res1 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))

        # Reset
        strat.reset()
        self.assertIsNone(strat._last_bar_index)
        self.assertEqual(len(strat._narratives), 0)

        # Replay
        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        res2 = _feed(strat, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))
        self.assertEqual([c.to_dict() for c in res1], [c.to_dict() for c in res2])

    def test_79_long_workload_bounded_state_o1(self):
        strat = S05BOSOBRetestStrategy()
        # Stream 10,000 bars with periodic BOS and retests
        # Fixture-specific bound formulation:
        # For this specific workload: W = max_ob_age_bars = 25, K = 20 (1 BOS every 20 bars), retest at b + 5.
        # Active concurrent narratives for this fixture <= ceil(W/K) + 1 = 3.
        # (General mathematical bound across arbitrary streams: active_narratives = O(R_bos * (W + 1)),
        # emitted_clusters = O(R_emit * (W + 1)), where R_bos and R_emit are per-bar event ingestion caps).
        all_emitted_setup_ids: list[str] = []
        all_results_1: list[tuple[CandidateSetup, ...]] = []
        peak_narratives = 0
        peak_clusters = 0

        for b in range(10000):
            events = ()
            obs = ()
            # New BOS and fresh OB at every 20 bars
            if b % 20 == 0:
                events = (_make_bos(index=b, direction="bullish", broken_swing_index=b, structure_leg_id=f"leg_{b}"),)
                obs = (_make_ob(index=max(0, b - 2), direction="bullish", source_event_index=b, created_at=b, structure_leg_id=f"leg_{b}"),)
            # Retest at b % 20 == 5
            elif b % 20 == 5:
                src_b = b - 5
                obs = (_make_ob(
                    index=max(0, src_b - 2), direction="bullish", source_event_index=src_b, created_at=src_b,
                    structure_leg_id=f"leg_{src_b}", valid=True, mitigated=True, mitigated_at=b, retest_count=1,
                ),)

            res = strat.evaluate(_make_context(bar_index=b, structure_events=events, obs=obs, bias="bullish"))
            all_results_1.append(res)
            for c in res:
                all_emitted_setup_ids.append(c.setup_id)

            cur_narr = len(strat._narratives)
            cur_clusters = len(strat._emitted_clusters)
            if cur_narr > peak_narratives:
                peak_narratives = cur_narr
            if cur_clusters > peak_clusters:
                peak_clusters = cur_clusters

            # Fixture-specific bound invariant
            self.assertLessEqual(cur_narr, 3, f"Narratives exceeded fixture-specific bound at bar {b}: {cur_narr}")
            self.assertLessEqual(cur_clusters, 2, f"Clusters exceeded fixture-specific bound at bar {b}: {cur_clusters}")

        # Assert fixture-specific global bounds
        self.assertLessEqual(peak_narratives, 3)
        self.assertLessEqual(peak_clusters, 2)
        self.assertLessEqual(len(strat._narratives), 3)
        self.assertLessEqual(len(strat._emitted_clusters), 2)

        # Exactly 500 setups emitted (1 per 20 bars)
        self.assertEqual(len(all_emitted_setup_ids), 500)
        # Zero duplicate setup IDs
        self.assertEqual(len(all_emitted_setup_ids), len(set(all_emitted_setup_ids)))

        # Deterministic replay parity on a fresh instance
        strat2 = S05BOSOBRetestStrategy()
        for b in range(10000):
            events = ()
            obs = ()
            if b % 20 == 0:
                events = (_make_bos(index=b, direction="bullish", broken_swing_index=b, structure_leg_id=f"leg_{b}"),)
                obs = (_make_ob(index=max(0, b - 2), direction="bullish", source_event_index=b, created_at=b, structure_leg_id=f"leg_{b}"),)
            elif b % 20 == 5:
                src_b = b - 5
                obs = (_make_ob(
                    index=max(0, src_b - 2), direction="bullish", source_event_index=src_b, created_at=src_b,
                    structure_leg_id=f"leg_{src_b}", valid=True, mitigated=True, mitigated_at=b, retest_count=1,
                ),)
            res2 = strat2.evaluate(_make_context(bar_index=b, structure_events=events, obs=obs, bias="bullish"))
            self.assertEqual([c.to_dict() for c in all_results_1[b]], [c.to_dict() for c in res2])

    # =========================================================================
    # Group I: Integration and Parity (Tests 80-87)
    # =========================================================================

    def test_80_strategy_evaluation_through_registry(self):
        strat = S05BOSOBRetestStrategy()
        reg = StrategyRegistry(strategies=(strat,), config=StrategyRegistryConfig(enabled_strategy_ids=("S05",)))

        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        _feed_reg(reg, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))

        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)
        res11 = _feed_reg(reg, _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, obs=(ob_ret,), bias="bullish"))

        self.assertIn("S05", res11)
        self.assertEqual(len(res11["S05"]), 1)
        self.assertEqual(res11["S05"][0].direction, "BUY")

    def _make_integration_fixture(self):
        htf_ev = StructureEvent(
            index=0,
            time=pd.Timestamp("2026-01-15 07:00:00+00:00"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=0,
            broken_swing_price=1900.0,
            close_price=1950.0,
            displacement=True,
            mode="swing",
            confirmed_swing_at=0,
        )
        cfg = ContextBuilderConfig(
            symbol="XAUUSD",
            timeframe="M1",
            swing_strength=1,
            swing_left_strength=1,
            swing_right_strength=1,
            structure_mode="internal",
            atr_period=3,
            displacement_multiplier=0.1,
            fvg_min_gap_pct=0.0,
            ob_lookback=10,
            ob_require_fvg=False,
        )
        candles = [
            {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 2000.0, "high": 2005.0, "low": 1995.0, "close": 2000.0, "volume": 100.0, "closed": True},
            {"bar_index": 1, "time": "2026-01-15 08:01:00 UTC", "open": 2000.0, "high": 2020.0, "low": 2000.0, "close": 2010.0, "volume": 100.0, "closed": True},
            {"bar_index": 2, "time": "2026-01-15 08:02:00 UTC", "open": 2010.0, "high": 2012.0, "low": 2002.0, "close": 2005.0, "volume": 100.0, "closed": True},
            {"bar_index": 3, "time": "2026-01-15 08:03:00 UTC", "open": 2005.0, "high": 2030.0, "low": 2005.0, "close": 2028.0, "volume": 100.0, "closed": True},
            {"bar_index": 4, "time": "2026-01-15 08:04:00 UTC", "open": 2028.0, "high": 2035.0, "low": 2025.0, "close": 2032.0, "volume": 100.0, "closed": True},
            {"bar_index": 5, "time": "2026-01-15 08:05:00 UTC", "open": 2032.0, "high": 2032.0, "low": 2008.0, "close": 2015.0, "volume": 100.0, "closed": True},
            {"bar_index": 6, "time": "2026-01-15 08:06:00 UTC", "open": 2015.0, "high": 2018.0, "low": 2009.0, "close": 2016.0, "volume": 100.0, "closed": True},
        ]
        return candles, cfg, [htf_ev]

    def test_81_context_builder_creates_ob_first_retest(self):
        candles, cfg, htf_events = self._make_integration_fixture()
        builder = StrategyContextBuilder(cfg, htf_events=htf_events)
        strat = S05BOSOBRetestStrategy(S05Config(mode="internal"))

        contexts = [builder.update(c) for c in candles]
        self.assertEqual(len(contexts), 7)

        # Bar 3: BOS occurs (close 2028 > swing high 2020 at bar 1). Bullish OB created from bar 2 (high: 2012, low: 2002).
        c3 = contexts[3]
        bos_events = [e for e in c3.recent_structures if e.event_type == "BOS" and e.direction == "bullish"]
        self.assertTrue(len(bos_events) >= 1)
        active_obs = [ob for ob in c3.active_obs if ob.direction == "bullish"]
        self.assertTrue(len(active_obs) >= 1)

        # Bar 5: Retest candle (low 2008 < ob.high 2012, close 2015 >= ob.low 2002)
        c5 = contexts[5]
        ob_at_5 = next((ob for ob in c5.active_obs if ob.index == 2), None)
        self.assertIsNotNone(ob_at_5)
        self.assertTrue(ob_at_5.mitigated)
        self.assertEqual(ob_at_5.retest_count, 1)

        # Evaluate incrementally
        res = [strat.evaluate(ctx) for ctx in contexts]
        self.assertEqual(len(res[0]), 0)
        self.assertEqual(len(res[1]), 0)
        self.assertEqual(len(res[2]), 0)
        self.assertEqual(len(res[3]), 0)
        self.assertEqual(len(res[4]), 0)
        self.assertEqual(len(res[5]), 1)
        self.assertEqual(len(res[6]), 0)

        c = res[5][0]
        self.assertEqual(c.strategy_id, "S05")
        self.assertEqual(c.direction, "BUY")
        self.assertEqual(c.bar_index, 5)
        self.assertEqual(c.entry_price, 2012.0)
        self.assertEqual(c.stop_loss, 2001.8)
        self.assertEqual(c.take_profit, 2035.0)
        self.assertEqual(c.planned_rr, 2.25)

    def test_82_batch_contexts_sequential_evaluation(self):
        candles, cfg, htf_events = self._make_integration_fixture()
        batch_contexts = build_strategy_contexts(candles, cfg, htf_events=htf_events)
        strat = S05BOSOBRetestStrategy(S05Config(mode="internal"))

        batch_res = [strat.evaluate(ctx) for ctx in batch_contexts]
        self.assertEqual(len(batch_res[5]), 1)
        c = batch_res[5][0]
        self.assertEqual(c.strategy_id, "S05")
        self.assertEqual(c.direction, "BUY")
        self.assertEqual(c.entry_price, 2012.0)
        self.assertEqual(c.stop_loss, 2001.8)
        self.assertEqual(c.take_profit, 2035.0)
        self.assertEqual(c.planned_rr, 2.25)

    def test_83_incremental_builder_sequential_evaluation(self):
        candles, cfg, htf_events = self._make_integration_fixture()
        builder = StrategyContextBuilder(cfg, htf_events=htf_events)
        strat = S05BOSOBRetestStrategy(S05Config(mode="internal"))

        results = [strat.evaluate(builder.update(c)) for c in candles]
        for b in range(5):
            self.assertEqual(len(results[b]), 0, f"False emission at bar {b}")
        self.assertEqual(len(results[5]), 1)
        self.assertEqual(len(results[6]), 0, "Second retest touch must be rejected")

        # Close-break invalidation test: alternative bar 5 candle closes below OB (close 1998 < low 2002)
        builder_inv = StrategyContextBuilder(cfg, htf_events=htf_events)
        strat_inv = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        candles_inv = list(candles[:5])
        candles_inv.append({
            "bar_index": 5, "time": "2026-01-15 08:05:00 UTC", "open": 2032.0, "high": 2032.0, "low": 1995.0, "close": 1998.0, "volume": 100.0, "closed": True
        })
        for c in candles_inv[:5]:
            strat_inv.evaluate(builder_inv.update(c))
        res_inv = strat_inv.evaluate(builder_inv.update(candles_inv[5]))
        self.assertEqual(len(res_inv), 0, "OB close break must invalidate and reject candidate emission")

    def test_84_json_round_trip_context_replay(self):
        candles, cfg, htf_events = self._make_integration_fixture()
        batch_contexts = build_strategy_contexts(candles, cfg, htf_events=htf_events)
        json_contexts = [
            StrategyContext.from_dict(json.loads(json.dumps(ctx.to_dict())))
            for ctx in batch_contexts
        ]

        strat = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        json_results = [strat.evaluate(ctx) for ctx in json_contexts]
        self.assertEqual(len(json_results[5]), 1)
        self.assertEqual(json_results[5][0].entry_price, 2012.0)

    def test_85_exact_parity_batch_incremental_json(self):
        candles, cfg, htf_events = self._make_integration_fixture()

        # 1. Incremental path
        builder = StrategyContextBuilder(cfg, htf_events=htf_events)
        strat_inc = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        inc_results = [strat_inc.evaluate(builder.update(c)) for c in candles]

        # 2. Batch path
        batch_contexts = build_strategy_contexts(candles, cfg, htf_events=htf_events)
        strat_batch = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        batch_results = [strat_batch.evaluate(ctx) for ctx in batch_contexts]

        # 3. JSON round-trip path
        json_contexts = [
            StrategyContext.from_dict(json.loads(json.dumps(ctx.to_dict())))
            for ctx in batch_contexts
        ]
        strat_json = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        json_results = [strat_json.evaluate(ctx) for ctx in json_contexts]

        inc_dicts = [[c.to_dict() for c in res] for res in inc_results]
        batch_dicts = [[c.to_dict() for c in res] for res in batch_results]
        json_dicts = [[c.to_dict() for c in res] for res in json_results]

        self.assertEqual(inc_dicts, batch_dicts)
        self.assertEqual(inc_dicts, json_dicts)

    def test_86_append_future_candles_zero_lookahead(self):
        candles, cfg, htf_events = self._make_integration_fixture()

        # 1. Prepare two independent datasets: prefix and extended
        prefix = candles
        future_candles = [
            {"bar_index": 7, "time": "2026-01-15 08:07:00 UTC", "open": 2016.0, "high": 2022.0, "low": 2014.0, "close": 2020.0, "volume": 100.0, "closed": True},
            {"bar_index": 8, "time": "2026-01-15 08:08:00 UTC", "open": 2020.0, "high": 2025.0, "low": 2018.0, "close": 2024.0, "volume": 100.0, "closed": True},
            {"bar_index": 9, "time": "2026-01-15 08:09:00 UTC", "open": 2024.0, "high": 2030.0, "low": 2022.0, "close": 2028.0, "volume": 100.0, "closed": True},
        ]
        extended = list(candles) + future_candles

        # 2. Run production batch path separately on each dataset
        prefix_contexts = build_strategy_contexts(
            prefix,
            cfg,
            htf_events=htf_events,
        )
        extended_contexts = build_strategy_contexts(
            extended,
            cfg,
            htf_events=htf_events,
        )

        # 3. Assert full serialized context prefix equality to catch any lookahead from ContextBuilder
        self.assertEqual(
            [context.to_dict() for context in prefix_contexts],
            [context.to_dict() for context in extended_contexts[:len(prefix_contexts)]],
            "ContextBuilder produced different context payloads for prefix when future candles were appended!",
        )

        # 4. Evaluate separately with two fresh strategy instances
        prefix_strategy = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        extended_strategy = S05BOSOBRetestStrategy(S05Config(mode="internal"))

        prefix_results = [
            [candidate.to_dict() for candidate in prefix_strategy.evaluate(context)]
            for context in prefix_contexts
        ]
        extended_results = [
            [candidate.to_dict() for candidate in extended_strategy.evaluate(context)]
            for context in extended_contexts
        ]

        # 5. Assert prefix candidate equality across both evaluations
        self.assertEqual(
            prefix_results,
            extended_results[:len(prefix_results)],
            "Strategy candidate emissions on prefix changed when future candles were appended!",
        )

        # 6. Specific structural assertions
        # Exactly one setup at bar 5
        self.assertEqual(len(prefix_results[5]), 1)
        self.assertEqual(len(extended_results[5]), 1)
        # Zero setups before bar 5
        for b in range(5):
            self.assertEqual(len(prefix_results[b]), 0, f"Unexpected candidate at bar {b} in prefix")
            self.assertEqual(len(extended_results[b]), 0, f"Unexpected candidate at bar {b} in extended")
        # Candidate at bar 5 has identical full payload
        self.assertEqual(prefix_results[5][0], extended_results[5][0])
        self.assertEqual(prefix_results[5][0]["entry_price"], 2012.0)
        self.assertEqual(prefix_results[5][0]["stop_loss"], 2001.8)
        self.assertEqual(prefix_results[5][0]["take_profit"], 2035.0)
        self.assertEqual(prefix_results[5][0]["planned_rr"], 2.25)

    def test_87_context_and_snapshot_deep_immutability(self):
        strat = S05BOSOBRetestStrategy()
        bos = _make_bos(index=10, direction="bullish")
        ob = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10)
        ob_ret = _make_ob(index=8, direction="bullish", high=2040.0, low=2036.0, source_event_index=10, created_at=10, valid=True, mitigated=True, mitigated_at=11, retest_count=1)

        ctx = _make_context(11, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(bos,), obs=(ob_ret,), bias="bullish")
        orig_dict = copy.deepcopy(ctx.to_dict())

        _feed(strat, _make_context(10, structure_events=(bos,), obs=(ob,), bias="bullish"))
        strat.evaluate(ctx)

        self.assertEqual(ctx.to_dict(), orig_dict)


if __name__ == "__main__":
    unittest.main()
