"""
tests/test_smc_strategy_s01.py
==============================
Comprehensive behavioral unit test suite for T53.4:
S01 ICT 2022 Reversal Strategy Template (67 test cases covering Groups A to I
plus QC regression tests 60–67).
"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import patch
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from smc.models import StructureEvent

from smc.engine.errors import (
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
    StrategyContext,
    StrategyProfile,
    StructureEventSnapshot,
    SwingPointSnapshot,
)
from smc.engine.protocol import (
    StrategyTemplate,
    validate_strategy_template,
)
from smc.engine.context import (
    ContextBuilderConfig,
    StrategyContextBuilder,
    build_strategy_contexts,
)
from smc.engine.registry import (
    StrategyRegistry,
    StrategyRegistryConfig,
)
from smc.engine.strategies.s01_ict_2022 import (
    NarrativeStage,
    S01Config,
    S01ICT2022Strategy,
)

def _feed(strat: S01ICT2022Strategy, context: StrategyContext) -> tuple[CandidateSetup, ...]:
    """Feed context into strategy, sequentially filling intermediate bars with neutral contexts if a gap exists."""
    if strat._last_bar_index is not None and context.bar_index > strat._last_bar_index + 1:
        ref_p = strat._last_context_payload["close"] if strat._last_context_payload else context.close
        gap_bias = context.htf_bias.bias if context.htf_bias else None
        for b in range(strat._last_bar_index + 1, context.bar_index):
            strat.evaluate(_make_context(
                bar_index=b,
                open_p=ref_p,
                high_p=ref_p,
                low_p=ref_p,
                close_p=ref_p,
                timeframe=context.timeframe,
                bias=gap_bias,
            ))
    return strat.evaluate(context)


def _feed_reg(reg: StrategyRegistry, context: StrategyContext) -> Mapping[str, tuple[CandidateSetup, ...]]:
    """Feed context into strategy registry, sequentially filling intermediate bars with neutral contexts if a gap exists."""
    if reg._last_bar_index is not None and context.bar_index > reg._last_bar_index + 1:
        ref_p = reg._last_context_payload["close"] if reg._last_context_payload else context.close
        gap_bias = context.htf_bias.bias if context.htf_bias else None
        for b in range(reg._last_bar_index + 1, context.bar_index):
            reg.evaluate_enabled(_make_context(
                bar_index=b,
                open_p=ref_p,
                high_p=ref_p,
                low_p=ref_p,
                close_p=ref_p,
                timeframe=context.timeframe,
                bias=gap_bias,
            ))
    return reg.evaluate_enabled(context)



def _make_bias(bias: str = "neutral", bar_index: int = 10) -> BiasStateSnapshot:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=bar_index)
    return BiasStateSnapshot(
        bias=bias,
        timestamp=base_ts,
    )


_DEFAULT_BIAS = object()


def _make_context(
    bar_index: int = 10,
    open_p: Optional[float] = None,
    high_p: Optional[float] = None,
    low_p: Optional[float] = None,
    close_p: Optional[float] = None,
    sweeps: Sequence[LiquiditySweepSnapshot] = (),
    structure_events: Sequence[StructureEventSnapshot] = (),
    fvgs: Sequence[FairValueGapSnapshot] = (),
    pools: Sequence[LiquidityPoolSnapshot] = (),
    bias: Any = _DEFAULT_BIAS,
    timeframe: str = "M1",
    htf_bias: Optional[BiasStateSnapshot] = None,
    active_htf_pois: Optional[Sequence[Any]] = None,
) -> StrategyContext:
    if bias is _DEFAULT_BIAS:
        if any(getattr(s, "direction", "") == "bearish" for s in sweeps):
            bias = "bearish"
        elif any(getattr(s, "direction", "") == "bullish" for s in sweeps):
            bias = "bullish"
        elif any(getattr(m, "direction", "") == "bearish" for m in structure_events):
            bias = "bearish"
        elif any(getattr(m, "direction", "") == "bullish" for m in structure_events):
            bias = "bullish"
        elif any(getattr(f, "direction", "") == "bearish" for f in fvgs):
            bias = "bearish"
        else:
            bias = "bullish"
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

    if active_htf_pois is None:
        cur_b = htf_bias.bias if htf_bias else bias
        if cur_b in ("bullish", "bearish"):
            default_poi = HTFPOISnapshot(
                poi_id=f"poi_default_{cur_b}",
                poi_type="FVG",
                direction=cur_b,
                timeframe="H1",
                top=3000.0,
                bottom=1000.0,
                created_at=0,
                status="active",
                touch_count=1,
                last_touch_bar=0,
            )
            pois_tuple = (default_poi,)
        else:
            pois_tuple = ()
    else:
        pois_tuple = tuple(active_htf_pois)

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
        recent_sweeps=tuple(sweeps),
        recent_structures=tuple(structure_events),
        active_fvgs=tuple(fvgs),
        active_pools=tuple(pools),
        htf_bias=htf_bias,
        active_htf_pois=pois_tuple,
    )


def _make_sweep(
    index: int = 10,
    direction: str = "bullish",
    pool_kind: str = "swing_low",
    pool_price: float = 2030.0,
    pool_indices: tuple[int, ...] = (5,),
    price_wick: float = 2028.0,
    close_price: float = 2032.0,
    mode: str = "internal",
    confirmed_at: Optional[int] = None,
    swept_at: Optional[int] = None,
    valid: bool = True,
    structure_leg_id: Optional[str] = None,
    liquidity_side: Optional[str] = None,
    raid_direction: Optional[str] = None,
    reversal_direction: Optional[str] = None,
) -> LiquiditySweepSnapshot:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=index)
    conf_at = confirmed_at if confirmed_at is not None else index
    sw_at = swept_at if swept_at is not None else index
    return LiquiditySweepSnapshot(
        index=index,
        time=base_ts,
        direction=direction,
        pool_kind=pool_kind,
        pool_price=pool_price,
        pool_indices=pool_indices,
        price_wick=price_wick,
        close_price=close_price,
        created_at=index,
        confirmed_at=conf_at,
        swept_at=sw_at,
        valid=valid,
        mode=mode,
        structure_leg_id=structure_leg_id,
        liquidity_side=liquidity_side,
        raid_direction=raid_direction,
        reversal_direction=reversal_direction,
    )


def _make_mss(
    index: int = 15,
    direction: str = "bullish",
    event_type: str = "CHoCH",
    broken_swing_index: int = 8,
    broken_swing_price: float = 2045.0,
    break_type: str = "close",
    displacement: bool = True,
    mode: str = "internal",
    structure_leg_id: str = "leg_01",
    close_price: Optional[float] = None,
) -> StructureEventSnapshot:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=index)
    cp = close_price if close_price is not None else (broken_swing_price + (1.0 if direction == "bullish" else -1.0))
    return StructureEventSnapshot(
        index=index,
        time=base_ts,
        event_type=event_type,
        direction=direction,
        broken_swing_index=broken_swing_index,
        broken_swing_price=broken_swing_price,
        close_price=cp,
        displacement=displacement,
        mode=mode,
        structure_leg_id=structure_leg_id,
        break_type=break_type,
    )


def _make_fvg(
    index: int = 12,
    direction: str = "bullish",
    top: float = 2040.0,
    bottom: float = 2036.0,
    mode: str = "internal",
    structure_leg_id: str = "leg_01",
    confirmed_at: Optional[int] = None,
    filled: bool = False,
    filled_at: Optional[int] = None,
) -> FairValueGapSnapshot:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=index)
    conf_at = confirmed_at if confirmed_at is not None else index
    return FairValueGapSnapshot(
        index=index,
        time=base_ts,
        direction=direction,
        top=top,
        bottom=bottom,
        mode=mode,
        confirmed_at=conf_at,
        filled=filled,
        filled_at=filled_at,
        structure_leg_id=structure_leg_id,
    )


def _make_pool(
    price: float = 2060.0,
    kind: str = "equal_highs",
    indices: tuple[int, ...] = (3, 7),
    confirmed_at: int = 7,
    mode: str = "internal",
    swept: bool = False,
    valid: bool = True,
    liquidity_side: Optional[str] = None,
) -> LiquidityPoolSnapshot:
    return LiquidityPoolSnapshot(
        kind=kind,
        price=price,
        price_max=price + 0.1,
        price_min=price - 0.1,
        indices=indices,
        created_at=indices[0],
        confirmed_at=confirmed_at,
        mode=mode,
        swept=swept,
        valid=valid,
        liquidity_side=liquidity_side,
    )


class TestSMCS01Strategy(unittest.TestCase):

    # =========================================================================
    # Group A: Config & Protocol (Tests 1–5)
    # =========================================================================

    def test_01_default_config_and_profile_exact_values(self):
        cfg = S01Config()
        self.assertEqual(cfg.mode, "internal")
        self.assertEqual(cfg.sweep_to_mss_max_bars, 20)
        self.assertEqual(cfg.fvg_to_mss_max_bars, 10)
        self.assertEqual(cfg.entry_expiry_bars, 15)
        self.assertEqual(cfg.entry_level, "proximal")
        self.assertEqual(cfg.sl_buffer_price, 0.20)
        self.assertEqual(cfg.min_rr, 1.50)
        self.assertEqual(cfg.fallback_rr, 2.00)
        self.assertTrue(cfg.require_displacement)

        strat = S01ICT2022Strategy(cfg)
        self.assertEqual(strat.strategy_id, "S01")
        prof = strat.profile
        self.assertEqual(prof.strategy_id, "S01")
        self.assertEqual(prof.name, "ICT 2022 Reversal")
        self.assertEqual(prof.version, "1.0.0")
        self.assertEqual(prof.style, "reversal")
        self.assertEqual(prof.allowed_directions, ("BUY", "SELL"))
        self.assertEqual(prof.timeframes, ("M1", "M5", "M15"))
        self.assertEqual(prof.max_setup_age_bars, 15)
        self.assertEqual(prof.cooldown_bars, 3)
        self.assertEqual(prof.min_rr, 1.50)
        self.assertEqual(prof.params, cfg.to_dict())

    def test_02_config_json_round_trip_exact(self):
        cfg = S01Config(
            mode="swing",
            sweep_to_mss_max_bars=15,
            fvg_to_mss_max_bars=8,
            entry_expiry_bars=12,
            entry_level="ce_50",
            sl_buffer_price=0.30,
            min_rr=2.00,
            fallback_rr=2.50,
            require_displacement=False,
        )
        d = cfg.to_dict()
        restored = S01Config.from_dict(d)
        self.assertEqual(cfg, restored)

        # JSON serialize & deserialize
        json_str = json.dumps(d)
        d_from_json = json.loads(json_str)
        restored_from_json = S01Config.from_dict(d_from_json)
        self.assertEqual(cfg, restored_from_json)

    def test_03_strict_config_validations(self):
        # Reject bool in numeric fields
        with self.assertRaises(StrategyValidationError):
            S01Config(sweep_to_mss_max_bars=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S01Config(sl_buffer_price=False)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S01Config(min_rr=True)  # type: ignore

        # Reject negative or zero integers
        with self.assertRaises(StrategyValidationError):
            S01Config(sweep_to_mss_max_bars=0)
        with self.assertRaises(StrategyValidationError):
            S01Config(fvg_to_mss_max_bars=-5)
        with self.assertRaises(StrategyValidationError):
            S01Config(entry_expiry_bars=0)

        # Reject negative, zero, NaN or Inf floats
        with self.assertRaises(StrategyValidationError):
            S01Config(sl_buffer_price=0.0)
        with self.assertRaises(StrategyValidationError):
            S01Config(min_rr=float("nan"))
        with self.assertRaises(StrategyValidationError):
            S01Config(fallback_rr=float("inf"))

        # Reject fallback_rr < min_rr
        with self.assertRaises(StrategyValidationError):
            S01Config(min_rr=2.50, fallback_rr=2.00)

        # Reject invalid mode or entry_level
        with self.assertRaises(StrategyValidationError):
            S01Config(mode="invalid_mode")
        with self.assertRaises(StrategyValidationError):
            S01Config(entry_level="invalid_level")

        # Reject unexpected fields in from_dict
        with self.assertRaises(StrategyValidationError):
            S01Config.from_dict({"mode": "internal", "unknown_field": 123})
        with self.assertRaises(StrategyValidationError):
            S01Config.from_dict("not a dict")  # type: ignore

    def test_04_strategy_satisfies_protocol_and_runs_in_registry(self):
        strat = S01ICT2022Strategy()
        self.assertTrue(isinstance(strat, StrategyTemplate))
        validate_strategy_template(strat)

        reg = StrategyRegistry([strat])
        self.assertEqual(reg.registered_strategy_ids, ("S01",))
        self.assertEqual(reg.enabled_strategy_ids, ("S01",))

        # Initial evaluate on empty context
        ctx = _make_context(bar_index=1)
        res = reg.evaluate_enabled(ctx)
        self.assertEqual(res["S01"], ())

    def test_05_clean_package_exports_no_circular_import(self):
        from pathlib import Path
        import subprocess
        import sys
        import tempfile

        repo_root = Path(__file__).resolve().parents[1]

        # 1. Subprocess import with cwd=repo_root
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import smc.engine.strategies.s01_ict_2022; "
                    "from smc.engine.strategies import S01Config, S01ICT2022Strategy; "
                    "from smc.engine import S01Config, S01ICT2022Strategy; "
                ),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Subprocess import failed with code {proc.returncode}:\nStdout: {proc.stdout}\nStderr: {proc.stderr}",
        )

        import smc.engine
        self.assertTrue(hasattr(smc.engine, "S01Config"))
        self.assertTrue(hasattr(smc.engine, "S01ICT2022Strategy"))
        self.assertIn("S01Config", smc.engine.__all__)
        self.assertIn("S01ICT2022Strategy", smc.engine.__all__)

    # =========================================================================
    # Group B: Happy Path & Symmetry (Tests 6–9)
    # =========================================================================

    def test_06_buy_happy_path_with_structural_target(self):
        strat = S01ICT2022Strategy()

        # Step 1: Bar 10: Bullish Sweep of swing low at 2030.0 (wick=2028.0)
        sw = _make_sweep(
            index=10,
            direction="bullish",
            pool_kind="swing_low",
            pool_price=2030.0,
            price_wick=2028.0,
        )
        ctx10 = _make_context(bar_index=10, sweeps=(sw,))
        res10 = _feed(strat, ctx10)
        self.assertEqual(res10, ())

        # Step 2: Bar 12: Bullish FVG [2036.0, 2040.0] on leg_01
        fvg = _make_fvg(
            index=12,
            direction="bullish",
            top=2040.0,
            bottom=2036.0,
            structure_leg_id="leg_01",
        )
        ctx12 = _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,))
        _feed(strat, ctx12)

        # Step 3: Bar 15: Bullish CHoCH on leg_01 at 2045.0 (displaced)
        mss = _make_mss(
            index=15,
            direction="bullish",
            event_type="CHoCH",
            broken_swing_price=2045.0,
            structure_leg_id="leg_01",
        )
        pool = _make_pool(price=2065.0, kind="equal_highs")
        ctx15 = _make_context(
            bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,)
        )
        res15 = _feed(strat, ctx15)
        self.assertEqual(res15, ())  # Cannot retest on MSS bar itself!

        # Step 4: Bar 18: Retest candle dips into FVG [2036, 2040] (low=2038, high=2044, close=2041)
        ctx18 = _make_context(
            bar_index=18,
            open_p=2042.0,
            high_p=2044.0,
            low_p=2038.0,  # Overlaps [2036, 2040]
            close_p=2041.0,  # Close >= bottom (2036) -> close-respect!
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            bias="bullish",
        )
        res18 = _feed(strat, ctx18)
        self.assertEqual(len(res18), 1)

        cand = res18[0]
        self.assertEqual(cand.strategy_id, "S01")
        self.assertEqual(cand.direction, "BUY")
        self.assertEqual(cand.bar_index, 18)
        self.assertEqual(cand.entry_price, 2040.0)  # proximal = fvg.top
        self.assertEqual(cand.stop_loss, 2027.8)  # min(2028, 2045) - 0.2 = 2027.8
        self.assertEqual(cand.take_profit, 2065.0)  # pool.price
        # RR = (2065 - 2040) / (2040 - 2027.8) = 25.0 / 12.2 = 2.05
        self.assertEqual(cand.planned_rr, 2.05)
        self.assertEqual(len(cand.evidences), 4)  # Sweep, MSS, FVG, Pool
        self.assertEqual(cand.evidences[0].kind, "liquidity_sweep")
        self.assertEqual(cand.evidences[1].kind, "structure_event")
        self.assertEqual(cand.evidences[2].kind, "fair_value_gap")
        self.assertEqual(cand.evidences[3].kind, "liquidity_pool")

    def test_07_sell_happy_path_mirror(self):
        strat = S01ICT2022Strategy()

        # Step 1: Bar 10: Bearish Sweep of swing high at 2070.0 (wick=2072.0)
        sw = _make_sweep(
            index=10,
            direction="bearish",
            pool_kind="swing_high",
            pool_price=2070.0,
            price_wick=2072.0,
        )
        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))

        # Step 2: Bar 12: Bearish FVG [2060.0, 2064.0]
        fvg = _make_fvg(
            index=12,
            direction="bearish",
            top=2064.0,
            bottom=2060.0,
            structure_leg_id="leg_02",
        )
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))

        # Step 3: Bar 15: Bearish MSS at 2055.0
        mss = _make_mss(
            index=15,
            direction="bearish",
            event_type="BOS",
            broken_swing_price=2055.0,
            structure_leg_id="leg_02",
        )
        pool = _make_pool(price=2035.0, kind="equal_lows")
        _feed(strat, 
            _make_context(
                bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,)
            )
        )

        # Step 4: Bar 17: Retest touches FVG [2060, 2064] (high=2062, close=2059)
        ctx17 = _make_context(
            bar_index=17,
            open_p=2058.0,
            high_p=2062.0,  # Overlaps [2060, 2064]
            low_p=2056.0,
            close_p=2059.0,  # Close <= top (2064) -> close-respect!
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            bias="bearish",
        )
        res17 = _feed(strat, ctx17)
        self.assertEqual(len(res17), 1)

        cand = res17[0]
        self.assertEqual(cand.direction, "SELL")
        self.assertEqual(cand.entry_price, 2060.0)  # proximal = fvg.bottom
        self.assertEqual(cand.stop_loss, 2072.2)  # max(2072, 2055) + 0.2 = 2072.2
        self.assertEqual(cand.take_profit, 2035.0)  # pool.price
        # RR = (2060 - 2035) / (2072.2 - 2060) = 25.0 / 12.2 = 2.05
        self.assertEqual(cand.planned_rr, 2.05)

    def test_08_long_short_symmetry_reflection(self):
        # Mirror test: P_sell = 4000.0 - P_buy
        # BUY levels: entry=2040, sl=2027.8, tp=2065, risk=12.2, reward=25, RR=2.05
        # Reflect: entry=1960, sl=1972.2, tp=1935, risk=12.2, reward=25, RR=2.05
        strat_buy = S01ICT2022Strategy()
        strat_sell = S01ICT2022Strategy()

        # Run buy
        sw_b = _make_sweep(index=10, direction="bullish", pool_kind="swing_low", price_wick=2028.0)
        fvg_b = _make_fvg(index=12, direction="bullish", top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss_b = _make_mss(index=15, direction="bullish", broken_swing_price=2045.0, structure_leg_id="leg1")
        p_b = _make_pool(price=2065.0, kind="equal_highs")

        _feed(strat_buy, _make_context(bar_index=10, sweeps=(sw_b,)))
        _feed(strat_buy, _make_context(bar_index=12, sweeps=(sw_b,), fvgs=(fvg_b,)))
        _feed(strat_buy, _make_context(bar_index=15, sweeps=(sw_b,), structure_events=(mss_b,), fvgs=(fvg_b,), pools=(p_b,)))
        res_b = _feed(strat_buy, _make_context(bar_index=18, low_p=2038.0, high_p=2044.0, close_p=2041.0, sweeps=(sw_b,), structure_events=(mss_b,), fvgs=(fvg_b,), pools=(p_b,), bias="bullish"))
        cand_b = res_b[0]

        # Run reflected sell
        sw_s = _make_sweep(index=10, direction="bearish", pool_kind="swing_high", price_wick=4000 - 2028.0)
        fvg_s = _make_fvg(index=12, direction="bearish", top=4000 - 2036.0, bottom=4000 - 2040.0, structure_leg_id="leg1")
        mss_s = _make_mss(index=15, direction="bearish", broken_swing_price=4000 - 2045.0, structure_leg_id="leg1")
        p_s = _make_pool(price=4000 - 2065.0, kind="equal_lows")

        _feed(strat_sell, _make_context(bar_index=10, close_p=4000 - 2052.0, sweeps=(sw_s,)))
        _feed(strat_sell, _make_context(bar_index=12, close_p=4000 - 2052.0, sweeps=(sw_s,), fvgs=(fvg_s,)))
        _feed(strat_sell, _make_context(bar_index=15, close_p=4000 - 2052.0, sweeps=(sw_s,), structure_events=(mss_s,), fvgs=(fvg_s,), pools=(p_s,)))
        res_s = _feed(strat_sell, _make_context(bar_index=18, low_p=4000 - 2044.0, high_p=4000 - 2038.0, close_p=4000 - 2041.0, sweeps=(sw_s,), structure_events=(mss_s,), fvgs=(fvg_s,), pools=(p_s,), bias="bearish"))
        cand_s = res_s[0]

        self.assertEqual(cand_b.planned_rr, cand_s.planned_rr)
        self.assertAlmostEqual(cand_b.entry_price, 4000 - cand_s.entry_price, places=3)
        self.assertAlmostEqual(cand_b.stop_loss, 4000 - cand_s.stop_loss, places=3)
        self.assertAlmostEqual(cand_b.take_profit, 4000 - cand_s.take_profit, places=3)

    def test_09_entry_level_ce_50_variant(self):
        cfg = S01Config(entry_level="ce_50")
        strat = S01ICT2022Strategy(cfg)

        sw = _make_sweep(index=10, direction="bullish", pool_kind="swing_low", price_wick=2028.0)
        fvg = _make_fvg(index=12, direction="bullish", top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, direction="bullish", broken_swing_price=2045.0, structure_leg_id="leg1")
        p = _make_pool(price=2065.0, kind="equal_highs")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(p,)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2037.0, high_p=2042.0, close_p=2039.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(p,), bias="bullish"))

        cand = res[0]
        self.assertEqual(cand.entry_price, 2038.0)  # (2040 + 2036) / 2 = 2038.0

    # =========================================================================
    # Group C: Missing / Wrong Evidence (Tests 10–16)
    # =========================================================================

    def test_10_missing_components_no_candidate(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12)

        # No MSS
        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        res = _feed(strat, _make_context(bar_index=18, sweeps=(sw,), fvgs=(fvg,)))
        self.assertEqual(res, ())

    def test_11_sweep_direction_pool_kind_mismatch_rejected(self):
        strat = S01ICT2022Strategy()
        # Bullish sweep on equal_highs -> inconsistent!
        sw_bad = _make_sweep(index=10, direction="bullish", pool_kind="equal_highs")
        _feed(strat, _make_context(bar_index=10, sweeps=(sw_bad,)))
        self.assertEqual(len(strat._narratives), 0)

    def test_12_mode_mismatch_rejected(self):
        strat = S01ICT2022Strategy(S01Config(mode="internal"))
        # Sweep has mode="swing"
        sw = _make_sweep(index=10, mode="swing")
        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        self.assertEqual(len(strat._narratives), 0)

    def test_13_fvg_direction_opposite_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish", pool_kind="swing_low")
        fvg_bearish = _make_fvg(index=12, direction="bearish", structure_leg_id="leg1")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg_bearish,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg_bearish,)))

        # Narrative stays SWEEP_SEEN because FVG direction didn't match
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_14_structure_leg_id_mismatch_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish", pool_kind="swing_low")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg_A")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg_B")  # Mismatch!

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_15_mss_wick_break_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish", pool_kind="swing_low")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg1")
        mss_wick = _make_mss(index=15, direction="bullish", break_type="wick", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss_wick,), fvgs=(fvg,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_16_mss_no_displacement_rejected_when_required(self):
        strat = S01ICT2022Strategy(S01Config(require_displacement=True))
        sw = _make_sweep(index=10, direction="bullish", pool_kind="swing_low")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg1")
        mss_no_disp = _make_mss(index=15, direction="bullish", displacement=False, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss_no_disp,), fvgs=(fvg,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    # =========================================================================
    # Group D: Ordering & Boundary (Tests 17–24)
    # =========================================================================

    def test_17_mss_before_or_at_sweep_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        mss_at_sweep = _make_mss(index=10)
        mss_before_sweep = _make_mss(index=9)

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,), structure_events=(mss_at_sweep, mss_before_sweep)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_18_fvg_before_sweep_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg_before = _make_fvg(index=9, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg_before,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_19_fvg_at_or_after_mss_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        mss = _make_mss(index=15, structure_leg_id="leg1")
        fvg_at_mss = _make_fvg(index=15, structure_leg_id="leg1")
        fvg_after_mss = _make_fvg(index=16, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=16, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg_at_mss, fvg_after_mss)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_20_fvg_confirmed_at_after_mss_rejected_no_lookahead(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        mss = _make_mss(index=15, structure_leg_id="leg1")
        # FVG candle index is 13, but confirmed_at is 16 (> mss.index 15)
        fvg_future_conf = _make_fvg(index=13, confirmed_at=16, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=16, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg_future_conf,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_21_sweep_to_mss_window_boundary_20_bars(self):
        # sweep.index = 10, max_bars = 20 -> valid up to 30. Bar 31 expires!
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))

        # Advance to bar 30 with MSS at 30 -> valid!
        fvg = _make_fvg(index=25, structure_leg_id="leg1")
        mss_30 = _make_mss(index=30, structure_leg_id="leg1")
        for b in range(11, 30):
            _feed(strat, _make_context(bar_index=b, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=30, sweeps=(sw,), structure_events=(mss_30,), fvgs=(fvg,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.FVG_READY)

        # Contrast: if MSS was at 31 (lag 21) -> expires!
        strat_timeout = S01ICT2022Strategy()
        _feed(strat_timeout, _make_context(bar_index=10, sweeps=(sw,)))
        for b in range(11, 31):
            _feed(strat_timeout, _make_context(bar_index=b, sweeps=(sw,)))
        # At bar 31 without MSS yet, narrative expires
        _feed(strat_timeout, _make_context(bar_index=31, sweeps=(sw,)))
        self.assertEqual(len(strat_timeout._narratives), 0)  # Pruned as EXPIRED

    def test_22_fvg_to_mss_lag_boundary_10_bars(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=5)
        mss = _make_mss(index=20, structure_leg_id="leg1")

        # FVG at index 10 (lag = 20 - 10 = 10) -> valid!
        fvg_lag10 = _make_fvg(index=10, structure_leg_id="leg1")
        # FVG at index 9 (lag = 20 - 9 = 11) -> rejected!
        fvg_lag11 = _make_fvg(index=9, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=5, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=20, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg_lag11,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

        # Now supply lag10
        _feed(strat, _make_context(bar_index=21, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg_lag10,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.FVG_READY)

    def test_23_retest_same_bar_as_mss_rejected_next_bar_passes(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))

        # Bar 15: MSS bar with price overlapping FVG -> CANNOT retest at MSS bar!
        res15 = _feed(strat, _make_context(bar_index=15, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        self.assertEqual(res15, ())

        # Bar 16: Next bar retest -> passes!
        res16 = _feed(strat, _make_context(bar_index=16, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(res16), 1)

    def test_24_expiry_boundary_15_bars_after_ready(self):
        strat = S01ICT2022Strategy(S01Config(entry_expiry_bars=15))
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # ready_at = 15 -> expiry_bar = 15 + 15 = 30
        # Advance bars without touch up to bar 29
        for b in range(16, 30):
            _feed(strat, _make_context(bar_index=b, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 30: Exactly at expiry_bar -> still valid!
        res30 = _feed(strat, _make_context(bar_index=30, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(res30), 1)

        # Reset and test bar 31 -> expired!
        strat.reset()
        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        for b in range(16, 31):
            _feed(strat, _make_context(bar_index=b, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res31 = _feed(strat, _make_context(bar_index=31, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(res31, ())
        self.assertEqual(len(strat._narratives), 0)

    # =========================================================================
    # Group E: FVG Lifecycle / No-Lookahead (Tests 25–31)
    # =========================================================================

    def test_25_fvg_filled_before_mss_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        mss = _make_mss(index=15, structure_leg_id="leg1")
        fvg_filled = _make_fvg(index=12, structure_leg_id="leg1", filled=True, filled_at=14)

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg_filled,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_26_fvg_filled_at_in_future_raises_future_leak_error(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg_future_leak = _make_fvg(index=12, structure_leg_id="leg1", filled_at=25)  # Current is 10!
        with self.assertRaises(StrategyStateError) as cm:
            _feed(strat, _make_context(bar_index=10, sweeps=(sw,), fvgs=(fvg_future_leak,)))
        self.assertIn("Future leak", str(cm.exception))

    def test_27_fvg_filled_prior_to_retest_invalidates(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Deep dip filling FVG completely (low=2034 <= bottom 2036) and closing through (close=2033)
        _feed(strat, _make_context(bar_index=16, low_p=2034.0, close_p=2033.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        self.assertEqual(len(strat._narratives), 0)  # Invalidated and pruned

    def test_28_full_fill_at_retest_bar_with_close_respect_emits(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Dips to 2035 (full-fill because <= 2036), but closes at 2038 (>= bottom 2036)
        res16 = _feed(strat, _make_context(bar_index=16, low_p=2035.0, high_p=2042.0, close_p=2038.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(res16), 1)

    def test_29_touch_zone_but_close_through_invalidates(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Touches [2036, 2040], but closes at 2034 (< 2036) -> Invalidates!
        res16 = _feed(strat, _make_context(bar_index=16, low_p=2035.0, high_p=2042.0, close_p=2034.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(res16, ())
        self.assertEqual(len(strat._narratives), 0)

    def test_30_no_overlap_with_fvg_no_candidate(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Price stays above FVG (low=2042 > top 2040)
        res16 = _feed(strat, _make_context(bar_index=16, low_p=2042.0, high_p=2046.0, close_p=2044.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(res16, ())

    def test_31_fvg_evicted_from_active_fvgs_after_linkage_preserves_correctness(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: active_fvgs is now empty (e.g. capped or evicted)
        res16 = _feed(strat, _make_context(bar_index=16, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(), bias="bullish"))
        self.assertEqual(len(res16), 1)

    # =========================================================================
    # Group F: Structure / Sweep Invalidation (Tests 32–37)
    # =========================================================================

    def test_32_opposite_choch_after_mss_invalidates(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg1")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Bearish CHoCH occurs at bar 16
        opp_choch = _make_mss(index=16, direction="bearish", event_type="CHoCH", structure_leg_id="leg2")
        _feed(strat, _make_context(bar_index=16, sweeps=(sw,), structure_events=(mss, opp_choch), fvgs=(fvg,)))
        self.assertEqual(len(strat._narratives), 0)

    def test_33_opposite_bos_after_mss_invalidates(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg1")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Bearish BOS occurs at bar 16
        opp_bos = _make_mss(index=16, direction="bearish", event_type="BOS", structure_leg_id="leg2")
        _feed(strat, _make_context(bar_index=16, sweeps=(sw,), structure_events=(mss, opp_bos), fvgs=(fvg,)))
        self.assertEqual(len(strat._narratives), 0)

    def test_34_opposite_event_on_retest_bar_itself_invalidates_before_retest(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish")
        fvg = _make_fvg(index=12, direction="bullish", top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Price touches FVG zone, BUT an opposite structure event also confirmed at bar 16!
        opp_ev = _make_mss(index=16, direction="bearish", event_type="CHoCH", structure_leg_id="leg2")
        res16 = _feed(strat, _make_context(bar_index=16, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss, opp_ev), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(res16, ())
        self.assertEqual(len(strat._narratives), 0)

    def test_35_opposite_structure_between_fvg_and_mss_rejects_linkage(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg1")
        # Opposite structure event at bar 13
        opp_13 = _make_mss(index=13, direction="bearish", structure_leg_id="leg_x")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss, opp_13), fvgs=(fvg,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.SWEEP_SEEN)

    def test_36_close_beyond_sweep_extreme_invalidates(self):
        strat = S01ICT2022Strategy()
        # BUY: sweep.price_wick = 2028.0
        sw = _make_sweep(index=10, direction="bullish", price_wick=2028.0)
        fvg = _make_fvg(index=12, direction="bullish", top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Close = 2027.0 (< sweep.price_wick 2028.0)
        _feed(strat, _make_context(bar_index=16, open_p=2030.0, high_p=2035.0, low_p=2025.0, close_p=2027.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        self.assertEqual(len(strat._narratives), 0)

    def test_37_same_direction_structure_event_does_not_invalidate(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, direction="bullish")
        fvg = _make_fvg(index=12, direction="bullish", top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: Another bullish structure event (continuation)
        bullish_bos = _make_mss(index=16, direction="bullish", event_type="BOS", structure_leg_id="leg1")
        _feed(strat, _make_context(bar_index=16, sweeps=(sw,), structure_events=(mss, bullish_bos), fvgs=(fvg,)))
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, NarrativeStage.FVG_READY)

    # =========================================================================
    # Group G: Target, Precision & RR (Tests 38–44)
    # =========================================================================

    def test_38_selects_closest_opposing_pool_independent_of_order(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, price_wick=2028.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")

        # Two pools: p1 at 2070, p2 at 2060 (closer to entry 2040)
        p1 = _make_pool(price=2070.0, confirmed_at=5)
        p2 = _make_pool(price=2060.0, confirmed_at=8)

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(p1, p2)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(p1, p2), bias="bullish"))

        cand = res[0]
        self.assertEqual(cand.take_profit, 2060.0)  # Picked closest pool p2!

    def test_39_filters_ineligible_pools(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, price_wick=2028.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")

        # Ineligible pools: wrong direction (below entry), swept, invalid, wrong mode
        p_wrong_side = _make_pool(price=2035.0, kind="equal_highs")
        p_swept = _make_pool(price=2065.0, swept=True)
        p_invalid = _make_pool(price=2065.0, valid=False)
        p_wrong_mode = _make_pool(price=2065.0, mode="swing")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(p_wrong_side, p_swept, p_invalid, p_wrong_mode), bias="bullish"))

        cand = res[0]
        self.assertEqual(cand.meta["target_source"], "fixed_rr")

    def test_40_no_opposing_pool_fallback_to_fixed_2r(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, price_wick=2028.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=()))
        res = _feed(strat, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(), bias="bullish"))

        cand = res[0]
        self.assertEqual(cand.meta["target_source"], "fixed_rr")
        # Entry = 2040, SL = 2027.8 -> Risk = 12.2 -> Fallback 2.0R TP = 2040 + 2*12.2 = 2064.4
        self.assertEqual(cand.take_profit, 2064.4)
        self.assertEqual(cand.planned_rr, 2.0)

    def test_41_closest_pool_rr_below_min_fallback_to_fixed_rr_no_skip(self):
        strat = S01ICT2022Strategy(S01Config(min_rr=1.50, fallback_rr=2.00))
        sw = _make_sweep(index=10, price_wick=2028.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")

        # Pool 1 at 2045.0 (Reward = 5.0, Risk = 12.2 -> RR = 0.41 < 1.50)
        # Pool 2 at 2080.0 (Farther pool with RR > 1.50)
        p1 = _make_pool(price=2045.0, kind="equal_highs")
        p2 = _make_pool(price=2080.0, kind="equal_highs")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(p1, p2)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(p1, p2), bias="bullish"))

        cand = res[0]
        # Contract: Must NOT skip p1 to pick p2; must fall back to fixed 2.0R!
        self.assertEqual(cand.meta["target_source"], "fixed_rr")
        self.assertEqual(cand.take_profit, 2064.4)
        self.assertEqual(cand.planned_rr, 2.00)

    def test_42_geometry_collapse_fails_closed(self):
        # Entry level equals or crosses SL
        strat = S01ICT2022Strategy(S01Config(sl_buffer_price=0.0001))
        sw = _make_sweep(index=10, price_wick=2040.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2040.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2040.0, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2040.0, high_p=2040.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(res, ())
        self.assertEqual(len(strat._narratives), 0)

    def test_43_planned_rr_recalculated_from_rounded_levels(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, price_wick=2028.1234)
        fvg = _make_fvg(index=12, top=2040.5678, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))

        cand = res[0]
        self.assertEqual(cand.entry_price, round(2040.5678, 3))
        self.assertEqual(cand.stop_loss, round(2028.1234 - 0.20, 3))
        # Verify planned_rr matches recalculation from rounded levels
        expected_rr = round((cand.take_profit - cand.entry_price) / (cand.entry_price - cand.stop_loss), 2)
        self.assertEqual(cand.planned_rr, expected_rr)

    def test_44_htf_bias_policy_aligned_neutral_pass_opposed_missing_fail(self):
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        # 1. Aligned bias ("bullish" for BUY) -> PASS
        s1 = S01ICT2022Strategy()
        _feed(s1, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(s1, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(s1, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res1 = _feed(s1, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(res1), 1)

        # 2. Neutral bias ("neutral") -> FAIL (Rule 10: blocked when bias neutral)
        s2 = S01ICT2022Strategy()
        _feed(s2, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(s2, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(s2, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res2 = _feed(s2, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="neutral"))
        self.assertEqual(res2, ())

        # 3. Opposed bias ("bearish" for BUY) -> FAIL (no candidate)
        s3 = S01ICT2022Strategy()
        _feed(s3, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(s3, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(s3, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res3 = _feed(s3, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bearish"))
        self.assertEqual(res3, ())

        # 4. Missing bias (None) -> FAIL closed
        s4 = S01ICT2022Strategy()
        _feed(s4, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(s4, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(s4, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res4 = _feed(s4, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias=None))
        self.assertEqual(res4, ())

        # 5. Bullish bias with bearish pending reversal -> Still allowed for BUY (Rule 10)
        s5 = S01ICT2022Strategy()
        _feed(s5, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(s5, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(s5, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        bias_pending = BiasStateSnapshot(
            bias="bullish",
            timestamp=pd.Timestamp("2026-01-15 10:18:00+00:00"),
            pending_reversal="bearish",
            reason="choch_reversal_pending",
        )
        res5 = _feed(s5, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), htf_bias=bias_pending))
        self.assertEqual(len(res5), 1)

    # =========================================================================
    # Group H: Duplicate, Concurrency & Determinism (Tests 45–52)
    # =========================================================================

    def test_45_duplicate_input_evidence_no_duplicate_candidate(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        # Context contains duplicate sweeps, fvgs, structure events
        _feed(strat, _make_context(bar_index=10, sweeps=(sw, sw)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw, sw), fvgs=(fvg, fvg)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss, mss), fvgs=(fvg,)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(res), 1)

    def test_46_two_concurrent_sweeps_tracked_independently(self):
        strat = S01ICT2022Strategy()
        # Sweep 1 on pool 1, Sweep 2 on pool 2 at same bar 10
        sw1 = _make_sweep(index=10, pool_indices=(1,), price_wick=2028.0)
        sw2 = _make_sweep(index=10, pool_indices=(2,), price_wick=2025.0)

        _feed(strat, _make_context(bar_index=10, sweeps=(sw1, sw2)))
        self.assertEqual(len(strat._narratives), 2)

    def test_47_multiple_valid_mss_and_fvg_deterministic_tie_breakers(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)

        # Two MSS: one at index 14, one at index 15
        mss_early = _make_mss(index=14, broken_swing_index=8, structure_leg_id="leg1")
        mss_late = _make_mss(index=15, broken_swing_index=9, structure_leg_id="leg1")

        # Two FVGs on leg1: index 12 and index 13
        fvg_closer = _make_fvg(index=13, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        fvg_farther = _make_fvg(index=12, top=2038.0, bottom=2034.0, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg_farther,)))
        _feed(strat, _make_context(bar_index=13, sweeps=(sw,), fvgs=(fvg_farther, fvg_closer)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss_late, mss_early), fvgs=(fvg_farther, fvg_closer)))

        # Retest
        res = _feed(strat, _make_context(bar_index=16, low_p=2037.0, high_p=2041.0, close_p=2039.0, sweeps=(sw,), structure_events=(mss_late, mss_early), fvgs=(fvg_farther, fvg_closer), bias="bullish"))
        self.assertEqual(len(res), 1)
        cand = res[0]
        # Should match earliest MSS (14) and closest FVG (13)
        self.assertEqual(cand.entry_price, 2040.0)

    def test_48_multiple_candidates_same_bar_sorted_canonically(self):
        strat = S01ICT2022Strategy()
        sw_buy1 = _make_sweep(index=8, direction="bullish", pool_kind="swing_low", pool_indices=(1,), price_wick=2028.0)
        fvg_buy1 = _make_fvg(index=9, direction="bullish", top=2040.0, bottom=2036.0, structure_leg_id="leg_buy1")
        mss_buy1 = _make_mss(index=10, direction="bullish", broken_swing_index=6, broken_swing_price=2045.0, structure_leg_id="leg_buy1")

        sw_buy2 = _make_sweep(index=11, direction="bullish", pool_kind="swing_low", pool_indices=(2,), price_wick=2026.0)
        fvg_buy2 = _make_fvg(index=12, direction="bullish", top=2044.0, bottom=2039.0, structure_leg_id="leg_buy2")
        mss_buy2 = _make_mss(index=14, direction="bullish", broken_swing_index=8, broken_swing_price=2048.0, structure_leg_id="leg_buy2")

        sweeps = (sw_buy1, sw_buy2)
        fvgs = (fvg_buy1, fvg_buy2)
        structs = (mss_buy1, mss_buy2)

        _feed(strat, _make_context(bar_index=8, sweeps=(sw_buy1,)))
        _feed(strat, _make_context(bar_index=9, sweeps=(sw_buy1,), fvgs=(fvg_buy1,)))
        _feed(strat, _make_context(bar_index=10, sweeps=(sw_buy1,), fvgs=(fvg_buy1,), structure_events=(mss_buy1,)))
        _feed(strat, _make_context(bar_index=11, sweeps=sweeps, fvgs=(fvg_buy1,), structure_events=(mss_buy1,)))
        _feed(strat, _make_context(bar_index=12, sweeps=sweeps, fvgs=fvgs, structure_events=(mss_buy1,)))
        _feed(strat, _make_context(bar_index=14, sweeps=sweeps, fvgs=fvgs, structure_events=structs))

        # At bar 15, retest taps both FVGs (2036-2040 and 2039-2044) -> low dips to 2038
        res = _feed(strat, _make_context(bar_index=15, open_p=2045.0, high_p=2046.0, low_p=2038.0, close_p=2042.0, sweeps=sweeps, fvgs=fvgs, structure_events=structs, bias="bullish"))
        self.assertEqual(len(res), 2)
        # Canonical sort: direction, evidence_cluster_id, setup_id
        self.assertEqual(res[0].direction, "BUY")
        self.assertEqual(res[1].direction, "BUY")
        self.assertLess(res[0].evidence_cluster_id, res[1].evidence_cluster_id)

    def test_49_emitted_cluster_does_not_emit_again_on_subsequent_retest(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))

        # Bar 16: First retest -> emits candidate!
        res16 = _feed(strat, _make_context(bar_index=16, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(res16), 1)

        # Bar 17: Second retest candle touching same FVG -> MUST NOT emit again!
        res17 = _feed(strat, _make_context(bar_index=17, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(res17, ())

    def test_50_same_bar_identical_retry_returns_cached_conflicting_raises(self):
        strat = S01ICT2022Strategy()
        ctx = _make_context(bar_index=1)
        res1 = _feed(strat, ctx)

        # Identical retry
        res2 = _feed(strat, ctx)
        self.assertIs(res1, res2)

        # Conflicting retry (different close price)
        ctx_conflict = _make_context(bar_index=1, close_p=2044.0)
        with self.assertRaises(StrategyStateError):
            _feed(strat, ctx_conflict)

    def test_51_non_monotonic_bar_gap_or_backward_raises_before_mutation(self):
        strat = S01ICT2022Strategy()
        c5 = _make_context(bar_index=5)
        res5 = strat.evaluate(c5)
        self.assertEqual(strat._last_bar_index, 5)

        # 1. Forward bar gap 5 -> 7 must raise StrategyStateError
        c7 = _make_context(bar_index=7)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(c7)
        # Verify state and cache untouched at bar 5
        self.assertEqual(strat._last_bar_index, 5)
        self.assertEqual(strat._last_context_payload, c5.to_dict())
        self.assertEqual(strat._last_result, res5)

        # 2. Backward bar 5 -> 4 must raise StrategyStateError
        c4 = _make_context(bar_index=4)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(c4)
        self.assertEqual(strat._last_bar_index, 5)
        self.assertEqual(strat._last_context_payload, c5.to_dict())

        # 3. Non-increasing timestamp on contiguous advance: 5 -> 6 with bad ts
        base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=5)
        ctx_bad_ts = StrategyContext(
            bar_index=6,
            timestamp=base_ts,
            bar_close_time=base_ts + pd.Timedelta(minutes=1),
            symbol="XAUUSD",
            timeframe="M1",
            open=2040.0,
            high=2045.0,
            low=2035.0,
            close=2042.0,
            volume=100.0,
            atr14=2.0,
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(ctx_bad_ts)
        self.assertEqual(strat._last_bar_index, 5)

        # 4. Sequential advance: 5 -> 6 with strictly greater timestamp succeeds
        c6 = _make_context(bar_index=6)
        res6 = strat.evaluate(c6)
        self.assertEqual(strat._last_bar_index, 6)

    def test_52_reset_clears_state_and_replay_produces_identical_output(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        c10 = _make_context(bar_index=10, sweeps=(sw,))
        c12 = _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,))
        c15 = _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,))
        c16 = _make_context(bar_index=16, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish")

        _feed(strat, c10)
        _feed(strat, c12)
        _feed(strat, c15)
        out1 = _feed(strat, c16)
        self.assertEqual(len(out1), 1)

        # Reset strategy
        strat.reset()
        self.assertEqual(len(strat._narratives), 0)
        self.assertEqual(len(strat._emitted_clusters), 0)
        self.assertIsNone(strat._last_bar_index)

        # Replay
        _feed(strat, c10)
        _feed(strat, c12)
        _feed(strat, c15)
        out2 = _feed(strat, c16)

        self.assertEqual([c.to_dict() for c in out1], [c.to_dict() for c in out2])

    # =========================================================================
    # Group I: Parity & Regression (Tests 53–59)
    # =========================================================================

    def test_53_incremental_feed_sequence(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=1)
        fvg = _make_fvg(index=2, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=3, structure_leg_id="leg1")

        # 5 bars fed sequentially
        out = []
        for b in range(1, 6):
            if b == 1:
                ctx = _make_context(bar_index=b, sweeps=(sw,))
            elif b == 2:
                ctx = _make_context(bar_index=b, sweeps=(sw,), fvgs=(fvg,))
            elif b == 3:
                ctx = _make_context(bar_index=b, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,))
            elif b == 4:
                ctx = _make_context(bar_index=b, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish")
            else:
                ctx = _make_context(bar_index=b, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,))
            cands = _feed(strat, ctx)
            if cands:
                out.extend(cands)

        self.assertEqual(len(out), 1)

    def test_54_batch_vs_incremental_exact_parity(self):
        """P2.2: True integration parity with synthetic candles across batch, incremental, and JSON replay."""
        cfg = ContextBuilderConfig(
            swing_strength=1,
            swing_left_strength=1,
            swing_right_strength=1,
            fvg_min_gap_pct=0.0,
            atr_period=3,
        )
        candles = [
            {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 100.0, "closed": True},
            {"bar_index": 1, "time": "2026-01-15 08:01:00 UTC", "open": 100.0, "high": 101.0, "low": 95.0, "close": 97.0, "volume": 100.0, "closed": True},
            {"bar_index": 2, "time": "2026-01-15 08:02:00 UTC", "open": 97.0, "high": 105.0, "low": 97.0, "close": 104.0, "volume": 100.0, "closed": True},
            {"bar_index": 3, "time": "2026-01-15 08:03:00 UTC", "open": 104.0, "high": 104.5, "low": 98.0, "close": 99.0, "volume": 100.0, "closed": True},
            {"bar_index": 4, "time": "2026-01-15 08:04:00 UTC", "open": 99.0, "high": 99.0, "low": 93.0, "close": 98.0, "volume": 100.0, "closed": True},
            {"bar_index": 5, "time": "2026-01-15 08:05:00 UTC", "open": 98.0, "high": 100.0, "low": 97.5, "close": 100.0, "volume": 100.0, "closed": True},
            {"bar_index": 6, "time": "2026-01-15 08:06:00 UTC", "open": 100.0, "high": 108.0, "low": 99.8, "close": 107.0, "volume": 200.0, "closed": True},
            {"bar_index": 7, "time": "2026-01-15 08:07:00 UTC", "open": 107.0, "high": 112.0, "low": 102.0, "close": 110.0, "volume": 150.0, "closed": True},
            {"bar_index": 8, "time": "2026-01-15 08:08:00 UTC", "open": 110.0, "high": 115.0, "low": 109.0, "close": 114.0, "volume": 100.0, "closed": True},
            {"bar_index": 9, "time": "2026-01-15 08:09:00 UTC", "open": 114.0, "high": 114.0, "low": 99.5, "close": 101.0, "volume": 100.0, "closed": True},
        ]
        strat_cfg = S01Config(mode="swing", min_rr=1.0, require_displacement=False)
        htf_events = [
            StructureEvent(
                index=0,
                time=pd.Timestamp("2026-01-15 08:00:00 UTC"),
                event_type="BOS",
                direction="bullish",
                broken_swing_index=0,
                broken_swing_price=98.0,
                close_price=100.0,
            )
        ]

        # 1. Batch Branch
        contexts_batch = build_strategy_contexts(candles, cfg, htf_events=htf_events)
        s_batch = S01ICT2022Strategy(strat_cfg)
        r_batch = [_feed(s_batch, c) for c in contexts_batch]

        # 2. Incremental Branch
        builder = StrategyContextBuilder(cfg, htf_events=htf_events)
        contexts_inc = [builder.update(c) for c in candles]
        s_inc = S01ICT2022Strategy(strat_cfg)
        r_inc = [_feed(s_inc, c) for c in contexts_inc]

        # 3. Serialized Replay Branch
        contexts_json = [
            StrategyContext.from_dict(json.loads(json.dumps(c.to_dict())))
            for c in contexts_batch
        ]
        s_replay = S01ICT2022Strategy(strat_cfg)
        r_replay = [_feed(s_replay, c) for c in contexts_json]

        # Assert 100% full payload equality
        payload_batch = [[cand.to_dict() for cand in res] for res in r_batch]
        payload_inc = [[cand.to_dict() for cand in res] for res in r_inc]
        payload_replay = [[cand.to_dict() for cand in res] for res in r_replay]

        self.assertEqual(payload_batch, payload_inc)
        self.assertEqual(payload_batch, payload_replay)

        # Verify candidate setup emitted at bar 9
        self.assertEqual(len(payload_batch[9]), 1)
        self.assertEqual(payload_batch[9][0]["direction"], "BUY")
        self.assertEqual(payload_batch[9][0]["bar_index"], 9)

    def test_55_json_round_trip_context_replay_parity(self):
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")
        pool = _make_pool(price=2065.0, kind="equal_highs")

        c10 = _make_context(bar_index=10, sweeps=(sw,))
        c12 = _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,))
        c15 = _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,))
        c18 = _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,), bias="bullish")

        contexts = [c10, c12, c15, c18]
        json_payloads = [json.dumps(c.to_dict()) for c in contexts]
        restored_contexts = [StrategyContext.from_dict(json.loads(p)) for p in json_payloads]

        s_orig = S01ICT2022Strategy()
        out_orig = [_feed(s_orig, c) for c in contexts]

        s_rest = S01ICT2022Strategy()
        out_rest = [_feed(s_rest, c) for c in restored_contexts]

        self.assertEqual(
            [[x.to_dict() for x in tup] for tup in out_orig],
            [[x.to_dict() for x in tup] for tup in out_rest],
        )

    def test_56_full_candidate_to_dict_deep_parity(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, price_wick=2028.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")
        pool = _make_pool(price=2065.0, kind="equal_highs")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,)))
        res = _feed(strat, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,), bias="bullish"))

        cand = res[0]
        d = cand.to_dict()
        self.assertIsInstance(d, dict)
        # JSON dump must succeed without TypeError or NaN
        dumped = json.dumps(d)
        restored_d = json.loads(dumped)
        self.assertEqual(d["setup_id"], restored_d["setup_id"])
        self.assertEqual(d["strategy_id"], "S01")
        self.assertEqual(d["direction"], "BUY")
        self.assertEqual(d["planned_rr"], 2.05)
        self.assertEqual(len(d["evidences"]), 4)

    def test_57_input_evidence_permutation_invariance(self):
        sw1 = _make_sweep(index=10, pool_indices=(1,), price_wick=2028.0)
        sw2 = _make_sweep(index=10, pool_indices=(2,), price_wick=2025.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")

        # Ingest with order (sw1, sw2)
        s1 = S01ICT2022Strategy()
        _feed(s1, _make_context(bar_index=10, sweeps=(sw1, sw2)))
        _feed(s1, _make_context(bar_index=12, sweeps=(sw1, sw2), fvgs=(fvg,)))
        _feed(s1, _make_context(bar_index=15, sweeps=(sw1, sw2), structure_events=(mss,), fvgs=(fvg,)))
        r1 = _feed(s1, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw1, sw2), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))

        # Ingest with reversed order (sw2, sw1)
        s2 = S01ICT2022Strategy()
        _feed(s2, _make_context(bar_index=10, sweeps=(sw2, sw1)))
        _feed(s2, _make_context(bar_index=12, sweeps=(sw2, sw1), fvgs=(fvg,)))
        _feed(s2, _make_context(bar_index=15, sweeps=(sw2, sw1), structure_events=(mss,), fvgs=(fvg,)))
        r2 = _feed(s2, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw2, sw1), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))

        self.assertEqual([x.to_dict() for x in r1], [x.to_dict() for x in r2])

    def test_58_context_deep_immutability_preserved(self):
        strat = S01ICT2022Strategy()
        ctx = _make_context(bar_index=1)
        payload_before = copy.deepcopy(ctx.to_dict())

        _feed(strat, ctx)
        payload_after = ctx.to_dict()

        self.assertEqual(payload_before, payload_after)

    def test_59_evaluation_through_strategy_registry(self):
        strat = S01ICT2022Strategy()
        cfg = StrategyRegistryConfig(enabled_strategy_ids=("S01",))
        reg = StrategyRegistry([strat], cfg)

        sw = _make_sweep(index=10, price_wick=2028.0)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, broken_swing_price=2045.0, structure_leg_id="leg1")

        _feed_reg(reg, _make_context(bar_index=10, sweeps=(sw,)))
        _feed_reg(reg, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed_reg(reg, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,)))
        res = _feed_reg(reg, _make_context(bar_index=18, low_p=2038.0, high_p=2042.0, close_p=2040.0, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), bias="bullish"))

        self.assertIn("S01", res)
        self.assertEqual(len(res["S01"]), 1)
        cand = res["S01"][0]
        self.assertEqual(cand.strategy_id, "S01")



    # =========================================================================
    # Group J: Dedicated Regression Probes (Tests 60–66)
    # =========================================================================

    def test_60_regression_bar_gap_strict_rejection_and_cache_preservation(self):
        strat = S01ICT2022Strategy()
        c1 = _make_context(bar_index=1)
        res1 = strat.evaluate(c1)
        self.assertEqual(strat._last_bar_index, 1)

        # Bar 3 after bar 1 must raise StrategyStateError
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=3))
        # State and last-bar cache remain intact at bar 1
        self.assertEqual(strat._last_bar_index, 1)
        self.assertEqual(strat._last_context_payload, c1.to_dict())
        self.assertEqual(strat._last_result, res1)

        # Sequential bar 2 succeeds
        c2 = _make_context(bar_index=2)
        res2 = strat.evaluate(c2)
        self.assertEqual(strat._last_bar_index, 2)

    def test_61_regression_earlier_mss_without_fvg_does_not_block_later_valid_mss(self):
        strat = S01ICT2022Strategy()
        t_base = pd.Timestamp("2026-01-15 10:00:00+00:00")
        sw = LiquiditySweepSnapshot(
            index=10, time=t_base + pd.Timedelta(minutes=10), direction="bullish",
            pool_kind="swing_low", pool_price=2030.0, pool_indices=(1,),
            price_wick=2028.0, close_price=2032.0, created_at=5, confirmed_at=10, swept_at=10, mode="internal"
        )
        mss_bad = StructureEventSnapshot(
            index=15, time=t_base + pd.Timedelta(minutes=15), event_type="CHoCH", direction="bullish",
            broken_swing_index=5, broken_swing_price=2042.0, close_price=2044.0, displacement=True,
            mode="internal", confirmed_swing_at=15, body_size=2.0, atr_value=2.0, structure_leg_id="leg_bad"
        )
        fvg_good = FairValueGapSnapshot(
            index=14, time=t_base + pd.Timedelta(minutes=14), direction="bullish",
            top=2040.0, bottom=2036.0, mode="internal", confirmed_at=14, structure_leg_id="leg_good"
        )
        mss_good = StructureEventSnapshot(
            index=16, time=t_base + pd.Timedelta(minutes=16), event_type="CHoCH", direction="bullish",
            broken_swing_index=6, broken_swing_price=2045.0, close_price=2047.0, displacement=True,
            mode="internal", confirmed_swing_at=16, body_size=2.0, atr_value=2.0, structure_leg_id="leg_good"
        )

        strat.evaluate(_make_context(10, sweeps=(sw,)))
        for b in range(11, 14):
            strat.evaluate(_make_context(b))
        strat.evaluate(_make_context(14, fvgs=(fvg_good,)))
        strat.evaluate(_make_context(15, structure_events=(mss_bad,), fvgs=(fvg_good,)))
        strat.evaluate(_make_context(16, structure_events=(mss_bad, mss_good), fvgs=(fvg_good,)))
        res17 = strat.evaluate(_make_context(
            17,
            low_p=2038.0,
            high_p=2044.0,
            close_p=2041.0,
            structure_events=(mss_bad, mss_good),
            fvgs=(fvg_good,),
            bias="bullish",
        ))
        self.assertEqual(len(res17), 1)
        self.assertEqual(res17[0].direction, "BUY")
        self.assertEqual(res17[0].meta["structure_leg_id"], "leg_good")

    def test_62_regression_opposite_structure_at_mss_bar_rejects_linkage(self):
        strat = S01ICT2022Strategy()
        t_base = pd.Timestamp("2026-01-15 10:00:00+00:00")
        sw = _make_sweep(index=10)
        fvg_bull = FairValueGapSnapshot(
            index=12, time=t_base + pd.Timedelta(minutes=12), direction="bullish",
            top=2040.0, bottom=2036.0, mode="internal", confirmed_at=12, structure_leg_id="leg_bull"
        )
        mss_bull = StructureEventSnapshot(
            index=15, time=t_base + pd.Timedelta(minutes=15), event_type="BOS", direction="bullish",
            broken_swing_index=5, broken_swing_price=2045.0, close_price=2047.0, displacement=True,
            mode="internal", confirmed_swing_at=15, body_size=2.0, atr_value=2.0, structure_leg_id="leg_bull"
        )
        mss_opp = StructureEventSnapshot(
            index=15, time=t_base + pd.Timedelta(minutes=15), event_type="BOS", direction="bearish",
            broken_swing_index=7, broken_swing_price=2035.0, close_price=2033.0, displacement=True,
            mode="internal", confirmed_swing_at=15, body_size=2.0, atr_value=2.0, structure_leg_id="leg_bear"
        )

        strat.evaluate(_make_context(10, sweeps=(sw,)))
        strat.evaluate(_make_context(11))
        strat.evaluate(_make_context(12, fvgs=(fvg_bull,)))
        strat.evaluate(_make_context(13, fvgs=(fvg_bull,)))
        strat.evaluate(_make_context(14, fvgs=(fvg_bull,)))
        # Order 1: (mss_bull, mss_opp)
        strat.evaluate(_make_context(15, structure_events=(mss_bull, mss_opp), fvgs=(fvg_bull,)))
        res16 = strat.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, fvgs=(fvg_bull,), bias="bullish"))
        self.assertEqual(res16, ())

        # Permutation invariance: test with reversed order (mss_opp, mss_bull)
        strat_perm = S01ICT2022Strategy()
        strat_perm.evaluate(_make_context(10, sweeps=(sw,)))
        strat_perm.evaluate(_make_context(11))
        strat_perm.evaluate(_make_context(12, fvgs=(fvg_bull,)))
        strat_perm.evaluate(_make_context(13, fvgs=(fvg_bull,)))
        strat_perm.evaluate(_make_context(14, fvgs=(fvg_bull,)))
        strat_perm.evaluate(_make_context(15, structure_events=(mss_opp, mss_bull), fvgs=(fvg_bull,)))
        res16_perm = strat_perm.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, fvgs=(fvg_bull,), bias="bullish"))
        self.assertEqual(res16_perm, ())

    def test_63_regression_future_htf_bias_defense_raises_before_candidate_emission(self):
        strat = S01ICT2022Strategy()
        t_base = pd.Timestamp("2026-01-15 10:00:00+00:00")
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        strat.evaluate(_make_context(10, sweeps=(sw,)))
        strat.evaluate(_make_context(11))
        strat.evaluate(_make_context(12, fvgs=(fvg,)))
        strat.evaluate(_make_context(13, fvgs=(fvg,)))
        strat.evaluate(_make_context(14, fvgs=(fvg,)))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,)))

        bar16_close = t_base + pd.Timedelta(minutes=17)
        # Probe as_of in future
        ctx_future_as_of = _make_context(
            16,
            low_p=2038.0, high_p=2044.0, close_p=2041.0,
            structure_events=(mss,), fvgs=(fvg,),
            htf_bias=BiasStateSnapshot(bias="bullish", timestamp=bar16_close, as_of=bar16_close + pd.Timedelta(days=1)),
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(ctx_future_as_of)
        self.assertEqual(strat._last_bar_index, 15)

        # Probe source_event_time in future
        ctx_future_src = _make_context(
            16,
            low_p=2038.0, high_p=2044.0, close_p=2041.0,
            structure_events=(mss,), fvgs=(fvg,),
            htf_bias=BiasStateSnapshot(bias="bullish", timestamp=bar16_close, source_event_time=bar16_close + pd.Timedelta(days=1)),
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(ctx_future_src)
        self.assertEqual(strat._last_bar_index, 15)

        # Probe timestamp in future
        ctx_future_ts = _make_context(
            16,
            low_p=2038.0, high_p=2044.0, close_p=2041.0,
            structure_events=(mss,), fvgs=(fvg,),
            htf_bias=BiasStateSnapshot(bias="bullish", timestamp=bar16_close + pd.Timedelta(days=1)),
        )
        with self.assertRaises(StrategyStateError):
            strat.evaluate(ctx_future_ts)
        self.assertEqual(strat._last_bar_index, 15)

    def test_64_regression_sweep_evidence_id_injective_and_no_collision(self):
        def emitted_sweep_id(pool_indices: tuple[int, ...]) -> str:
            strat = S01ICT2022Strategy()
            sw = _make_sweep(index=10, pool_indices=pool_indices)
            fvg = _make_fvg(index=12, structure_leg_id="leg1")
            mss = _make_mss(index=15, structure_leg_id="leg1")
            strat.evaluate(_make_context(10, sweeps=(sw,)))
            strat.evaluate(_make_context(11))
            strat.evaluate(_make_context(12, fvgs=(fvg,)))
            strat.evaluate(_make_context(13, fvgs=(fvg,)))
            strat.evaluate(_make_context(14, fvgs=(fvg,)))
            strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,)))
            candidates = strat.evaluate(_make_context(
                16,
                low_p=2038.0,
                high_p=2044.0,
                close_p=2041.0,
                structure_events=(mss,),
                fvgs=(fvg,),
                bias="bullish",
            ))
            self.assertEqual(len(candidates), 1)
            return next(
                evidence.evidence_id
                for evidence in candidates[0].evidences
                if evidence.kind == "liquidity_sweep"
            )

        id1 = emitted_sweep_id((1, 2))
        id2 = emitted_sweep_id((3, 4))
        id1_permuted = emitted_sweep_id((2, 1))
        self.assertNotEqual(id1, id2)
        self.assertEqual(id1, id1_permuted)

    def test_65_regression_pool_evidence_id_injective_and_no_collision(self):
        def emitted_pool_id(indices: tuple[int, ...]) -> str:
            strat = S01ICT2022Strategy()
            sw = _make_sweep(index=10)
            fvg = _make_fvg(index=12, structure_leg_id="leg1")
            mss = _make_mss(index=15, structure_leg_id="leg1")
            pool = _make_pool(
                kind="equal_highs",
                price=2060.0,
                indices=indices,
                confirmed_at=10,
                mode="internal",
            )
            strat.evaluate(_make_context(10, sweeps=(sw,)))
            strat.evaluate(_make_context(11))
            strat.evaluate(_make_context(12, fvgs=(fvg,)))
            strat.evaluate(_make_context(13, fvgs=(fvg,)))
            strat.evaluate(_make_context(14, fvgs=(fvg,)))
            strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,)))
            candidates = strat.evaluate(_make_context(
                16,
                low_p=2038.0,
                high_p=2044.0,
                close_p=2041.0,
                structure_events=(mss,),
                fvgs=(fvg,),
                pools=(pool,),
                bias="bullish",
            ))
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].target_type, "opposing_pool")
            return next(
                evidence.evidence_id
                for evidence in candidates[0].evidences
                if evidence.kind == "liquidity_pool"
            )

        id1 = emitted_pool_id((5, 10))
        id2 = emitted_pool_id((6, 11))
        id1_permuted = emitted_pool_id((10, 5))
        self.assertNotEqual(id1, id2)
        self.assertEqual(id1, id1_permuted)

    def test_66_regression_bounded_emitted_clusters_state_over_long_workload(self):
        strat = S01ICT2022Strategy()
        t_base = pd.Timestamp("2026-01-15 10:00:00+00:00")
        max_emitted_clusters_len = 0

        # Feed 50 sequential setups over 1000 bars
        for cycle in range(50):
            start_bar = cycle * 20
            sw_c = LiquiditySweepSnapshot(
                index=start_bar, time=t_base + pd.Timedelta(minutes=start_bar), direction="bullish",
                pool_kind="swing_low", pool_price=2030.0, pool_indices=(cycle,), price_wick=2028.0,
                close_price=2032.0, created_at=start_bar, confirmed_at=start_bar, swept_at=start_bar, mode="internal"
            )
            fvg_c = FairValueGapSnapshot(
                index=start_bar + 2, time=t_base + pd.Timedelta(minutes=start_bar + 2), direction="bullish",
                top=2040.0, bottom=2036.0, mode="internal", confirmed_at=start_bar + 2, structure_leg_id=f"leg_{cycle}"
            )
            mss_c = StructureEventSnapshot(
                index=start_bar + 4, time=t_base + pd.Timedelta(minutes=start_bar + 4), event_type="CHoCH", direction="bullish",
                broken_swing_index=start_bar + 1, broken_swing_price=2045.0, close_price=2047.0, displacement=True,
                mode="internal", confirmed_swing_at=start_bar + 4, body_size=2.0, atr_value=2.0, structure_leg_id=f"leg_{cycle}"
            )
            strat.evaluate(_make_context(start_bar, sweeps=(sw_c,)))
            strat.evaluate(_make_context(start_bar + 1))
            strat.evaluate(_make_context(start_bar + 2, fvgs=(fvg_c,)))
            strat.evaluate(_make_context(start_bar + 3, fvgs=(fvg_c,)))
            strat.evaluate(_make_context(start_bar + 4, structure_events=(mss_c,), fvgs=(fvg_c,)))
            # Retest at start_bar + 5
            cands = strat.evaluate(_make_context(
                start_bar + 5,
                low_p=2038.0, high_p=2044.0, close_p=2041.0,
                structure_events=(mss_c,), fvgs=(fvg_c,),
                bias="bullish",
            ))
            self.assertEqual(len(cands), 1)

            for b in range(start_bar + 6, start_bar + 20):
                strat.evaluate(_make_context(b))
                max_emitted_clusters_len = max(max_emitted_clusters_len, len(strat._emitted_clusters))

        # Must remain strictly bounded (<= 2) and NOT grow linearly with 50 emitted setups
        self.assertLessEqual(max_emitted_clusters_len, 2)
        # Reset clears everything
        strat.reset()
        self.assertEqual(len(strat._emitted_clusters), 0)

    def test_67_regression_candidate_exception_does_not_commit_cluster_pruning(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")
        strat.evaluate(_make_context(10, sweeps=(sw,)))
        strat.evaluate(_make_context(11))
        strat.evaluate(_make_context(12, fvgs=(fvg,)))
        strat.evaluate(_make_context(13, fvgs=(fvg,)))
        strat.evaluate(_make_context(14, fvgs=(fvg,)))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,)))

        strat._emitted_clusters = {"expired-cluster": 15, "live-cluster": 16}
        state_before = copy.deepcopy(strat._narratives)
        clusters_before = dict(strat._emitted_clusters)
        cache_before = (
            strat._last_bar_index,
            strat._last_timestamp,
            copy.deepcopy(strat._last_context_payload),
            strat._last_result,
        )

        with patch.object(
            strat,
            "_build_candidate",
            side_effect=RuntimeError("injected candidate failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected candidate failure"):
                strat.evaluate(_make_context(
                    16,
                    low_p=2038.0,
                    high_p=2044.0,
                    close_p=2041.0,
                    structure_events=(mss,),
                    fvgs=(fvg,),
                    bias="bullish",
                ))

        self.assertEqual(strat._narratives, state_before)
        self.assertEqual(strat._emitted_clusters, clusters_before)
        self.assertEqual(
            (
                strat._last_bar_index,
                strat._last_timestamp,
                strat._last_context_payload,
                strat._last_result,
            ),
            cache_before,
        )

    # =========================================================================
    # Group J: Semantic Liquidity Direction QC (Tests 68–79)
    # =========================================================================

    def test_68_bullish_bias_sell_side_low_sweep_creates_buy_candidate(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(
            index=10,
            direction="bullish",
            pool_kind="swing_low",
            pool_price=2030.0,
            price_wick=2028.0,
            close_price=2032.0,
            structure_leg_id="leg_01",
        )
        # Verify auto-derived snapshot semantics
        self.assertEqual(sw.liquidity_side, "SELL_SIDE")
        self.assertEqual(sw.raid_direction, "bearish")
        self.assertEqual(sw.reversal_direction, "bullish")
        self.assertTrue(sw.is_semantically_valid())

        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg_01")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg_01")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bullish"))
        strat.evaluate(_make_context(11, bias="bullish"))
        strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(13, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(14, fvgs=(fvg,), bias="bullish"))
        cands_at_15 = strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(cands_at_15), 0)  # Retest not on MSS bar

        # Retest at bar 16
        cands = strat.evaluate(_make_context(
            16,
            low_p=2038.0,
            high_p=2044.0,
            close_p=2041.0,
            structure_events=(mss,),
            fvgs=(fvg,),
            bias="bullish",
        ))
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.direction, "BUY")
        self.assertEqual(c.meta["sweep_liquidity_side"], "SELL_SIDE")
        self.assertEqual(c.meta["sweep_raid_direction"], "bearish")
        self.assertEqual(c.meta["sweep_reversal_direction"], "bullish")
        self.assertEqual(c.meta["mss_direction"], "bullish")
        self.assertEqual(c.meta["fvg_direction"], "bullish")
        self.assertEqual(c.meta["trade_direction"], "BUY")

        ev_sweep = next(e for e in c.evidences if e.kind == "liquidity_sweep")
        self.assertEqual(ev_sweep.details["liquidity_side"], "SELL_SIDE")
        self.assertEqual(ev_sweep.details["raid_direction"], "bearish")
        self.assertEqual(ev_sweep.details["reversal_direction"], "bullish")

    def test_69_bearish_bias_buy_side_high_sweep_creates_sell_candidate(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(
            index=10,
            direction="bearish",
            pool_kind="swing_high",
            pool_price=2060.0,
            price_wick=2062.0,
            close_price=2058.0,
            structure_leg_id="leg_bear",
        )
        self.assertEqual(sw.liquidity_side, "BUY_SIDE")
        self.assertEqual(sw.raid_direction, "bullish")
        self.assertEqual(sw.reversal_direction, "bearish")
        self.assertTrue(sw.is_semantically_valid())

        mss = _make_mss(index=15, direction="bearish", broken_swing_price=2040.0, structure_leg_id="leg_bear")
        fvg = _make_fvg(index=12, direction="bearish", top=2055.0, bottom=2050.0, structure_leg_id="leg_bear")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bearish"))
        strat.evaluate(_make_context(11, bias="bearish"))
        strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bearish"))
        strat.evaluate(_make_context(13, bias="bearish"))
        strat.evaluate(_make_context(14, bias="bearish"))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,), bias="bearish"))

        # Retest at bar 16 (high touches FVG bottom at 2050.0)
        cands = strat.evaluate(_make_context(
            16,
            open_p=2045.0,
            high_p=2052.0,
            low_p=2044.0,
            close_p=2048.0,
            structure_events=(mss,),
            fvgs=(fvg,),
            bias="bearish",
        ))
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.direction, "SELL")
        self.assertEqual(c.meta["sweep_liquidity_side"], "BUY_SIDE")
        self.assertEqual(c.meta["sweep_raid_direction"], "bullish")
        self.assertEqual(c.meta["sweep_reversal_direction"], "bearish")
        self.assertEqual(c.meta["mss_direction"], "bearish")
        self.assertEqual(c.meta["fvg_direction"], "bearish")
        self.assertEqual(c.meta["trade_direction"], "SELL")

        ev_sweep = next(e for e in c.evidences if e.kind == "liquidity_sweep")
        self.assertEqual(ev_sweep.details["liquidity_side"], "BUY_SIDE")
        self.assertEqual(ev_sweep.details["raid_direction"], "bullish")
        self.assertEqual(ev_sweep.details["reversal_direction"], "bearish")

    def test_70_bullish_bias_rejects_buy_side_high_sweep(self):
        strat = S01ICT2022Strategy()
        # High sweep (BUY_SIDE) during bullish bias
        sw = _make_sweep(
            index=10,
            direction="bearish",
            pool_kind="equal_highs",
            pool_price=2060.0,
            price_wick=2062.0,
            close_price=2058.0,
            structure_leg_id="leg_01",
        )
        self.assertEqual(sw.liquidity_side, "BUY_SIDE")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg_01")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg_01")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bullish"))
        strat.evaluate(_make_context(11, bias="bullish"))
        strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(13, bias="bullish"))
        strat.evaluate(_make_context(14, bias="bullish"))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        cands = strat.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, bias="bullish"))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_71_bearish_bias_rejects_sell_side_low_sweep(self):
        strat = S01ICT2022Strategy()
        # Low sweep (SELL_SIDE) during bearish bias
        sw = _make_sweep(
            index=10,
            direction="bullish",
            pool_kind="equal_lows",
            pool_price=2030.0,
            price_wick=2028.0,
            close_price=2032.0,
            structure_leg_id="leg_01",
        )
        self.assertEqual(sw.liquidity_side, "SELL_SIDE")
        mss = _make_mss(index=15, direction="bearish", broken_swing_price=2040.0, structure_leg_id="leg_01")
        fvg = _make_fvg(index=12, direction="bearish", top=2055.0, bottom=2050.0, structure_leg_id="leg_01")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bearish"))
        strat.evaluate(_make_context(11, bias="bearish"))
        strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bearish"))
        strat.evaluate(_make_context(13, bias="bearish"))
        strat.evaluate(_make_context(14, bias="bearish"))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,), bias="bearish"))
        cands = strat.evaluate(_make_context(16, open_p=2045.0, high_p=2052.0, low_p=2044.0, close_p=2048.0, bias="bearish"))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_72_sweep_with_contradictory_semantics_rejected(self):
        strat = S01ICT2022Strategy()
        # Explicit contradictory sweep: pool_kind is swing_low but reversal is bearish
        sw = _make_sweep(
            index=10,
            direction="bullish",
            pool_kind="swing_low",
            reversal_direction="bearish",
            liquidity_side="BUY_SIDE",
            structure_leg_id="leg_01",
        )
        self.assertFalse(sw.is_semantically_valid())

        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg_01")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg_01")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bullish"))
        strat.evaluate(_make_context(11, bias="bullish"))
        strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(13, bias="bullish"))
        strat.evaluate(_make_context(14, bias="bullish"))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        cands = strat.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, bias="bullish"))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_73_mss_opposite_htf_bias_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, structure_leg_id="leg_01")  # SELL_SIDE, bullish reversal
        mss_bearish = _make_mss(index=15, direction="bearish", broken_swing_price=2040.0, structure_leg_id="leg_01")
        fvg = _make_fvg(index=12, direction="bullish", structure_leg_id="leg_01")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bullish"))
        strat.evaluate(_make_context(11, bias="bullish"))
        strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(13, bias="bullish"))
        strat.evaluate(_make_context(14, bias="bullish"))
        strat.evaluate(_make_context(15, structure_events=(mss_bearish,), fvgs=(fvg,), bias="bullish"))
        cands = strat.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, bias="bullish"))
        self.assertEqual(len(cands), 0)
        # Narrative never linked bearish MSS to bullish sweep
        self.assertTrue(all(n.mss is None for n in strat._narratives.values()))

    def test_74_fvg_opposite_mss_rejected(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, structure_leg_id="leg_01")
        mss = _make_mss(index=15, direction="bullish", structure_leg_id="leg_01")
        fvg_bearish = _make_fvg(index=12, direction="bearish", top=2042.0, bottom=2038.0, structure_leg_id="leg_01")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bullish"))
        strat.evaluate(_make_context(11, bias="bullish"))
        strat.evaluate(_make_context(12, fvgs=(fvg_bearish,), bias="bullish"))
        strat.evaluate(_make_context(13, bias="bullish"))
        strat.evaluate(_make_context(14, bias="bullish"))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg_bearish,), bias="bullish"))
        cands = strat.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, bias="bullish"))
        self.assertEqual(len(cands), 0)

    def test_75_legacy_payload_round_trip_and_backward_compatibility(self):
        legacy_dict = {
            "index": 10,
            "time": "2026-01-15T10:10:00+00:00",
            "direction": "bullish",
            "pool_kind": "swing_low",
            "pool_price": 2030.0,
            "pool_indices": [5],
            "price_wick": 2028.0,
            "close_price": 2032.0,
            "created_at": 10,
            "confirmed_at": 10,
            "swept_at": 10,
            "valid": True,
            "mode": "internal",
            "structure_leg_id": "leg_1",
        }
        # No liquidity_side, raid_direction, reversal_direction in legacy_dict
        sw = LiquiditySweepSnapshot.from_source(legacy_dict)
        self.assertEqual(sw.liquidity_side, "SELL_SIDE")
        self.assertEqual(sw.raid_direction, "bearish")
        self.assertEqual(sw.reversal_direction, "bullish")
        self.assertTrue(sw.is_semantically_valid())

        # Round-trip serialization
        sw_dict = sw.to_dict()
        serialized = json.dumps(sw_dict)
        deserialized = json.loads(serialized)
        sw_restored = LiquiditySweepSnapshot.from_source(deserialized)
        self.assertEqual(sw_restored.liquidity_side, "SELL_SIDE")
        self.assertEqual(sw_restored.raid_direction, "bearish")
        self.assertEqual(sw_restored.reversal_direction, "bullish")
        self.assertEqual(sw_restored.direction, "bullish")

    def test_76_long_short_symmetry(self):
        # Bullish setup
        strat_long = S01ICT2022Strategy()
        sw_long = _make_sweep(index=10, direction="bullish", pool_kind="swing_low", pool_price=2030.0, price_wick=2028.0, close_price=2032.0, structure_leg_id="leg1")
        fvg_long = _make_fvg(index=12, direction="bullish", top=2040.0, bottom=2036.0, structure_leg_id="leg1")
        mss_long = _make_mss(index=15, direction="bullish", broken_swing_price=2045.0, structure_leg_id="leg1")

        strat_long.evaluate(_make_context(10, sweeps=(sw_long,), bias="bullish"))
        strat_long.evaluate(_make_context(11, bias="bullish"))
        strat_long.evaluate(_make_context(12, fvgs=(fvg_long,), bias="bullish"))
        strat_long.evaluate(_make_context(13, fvgs=(fvg_long,), bias="bullish"))
        strat_long.evaluate(_make_context(14, fvgs=(fvg_long,), bias="bullish"))
        strat_long.evaluate(_make_context(15, structure_events=(mss_long,), fvgs=(fvg_long,), bias="bullish"))
        cands_long = strat_long.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(mss_long,), fvgs=(fvg_long,), bias="bullish"))
        self.assertEqual(len(cands_long), 1)

        # Bearish setup - exact mirror across 2050
        # 2030 -> 2070, 2028 -> 2072, 2032 -> 2068, 2040/2036 -> 2060/2064, 2045 -> 2055
        strat_short = S01ICT2022Strategy()
        sw_short = _make_sweep(index=10, direction="bearish", pool_kind="swing_high", pool_price=2070.0, price_wick=2072.0, close_price=2068.0, structure_leg_id="leg1")
        fvg_short = _make_fvg(index=12, direction="bearish", top=2064.0, bottom=2060.0, structure_leg_id="leg1")
        mss_short = _make_mss(index=15, direction="bearish", broken_swing_price=2055.0, structure_leg_id="leg1")

        strat_short.evaluate(_make_context(10, sweeps=(sw_short,), bias="bearish"))
        strat_short.evaluate(_make_context(11, bias="bearish"))
        strat_short.evaluate(_make_context(12, fvgs=(fvg_short,), bias="bearish"))
        strat_short.evaluate(_make_context(13, bias="bearish"))
        strat_short.evaluate(_make_context(14, bias="bearish"))
        strat_short.evaluate(_make_context(15, structure_events=(mss_short,), fvgs=(fvg_short,), bias="bearish"))
        # Retest touch: open 2055, high 2062 (touches proximal 2060), low 2056, close 2058
        cands_short = strat_short.evaluate(_make_context(16, open_p=2055.0, high_p=2062.0, low_p=2056.0, close_p=2058.0, structure_events=(mss_short,), fvgs=(fvg_short,), bias="bearish"))
        self.assertEqual(len(cands_short), 1)

        c_long = cands_long[0]
        c_short = cands_short[0]
        self.assertEqual(c_long.direction, "BUY")
        self.assertEqual(c_short.direction, "SELL")
        self.assertAlmostEqual(c_long.planned_rr, c_short.planned_rr, places=2)

    def test_77_no_duplicate_candidate_on_same_cluster(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, structure_leg_id="leg1")
        fvg = _make_fvg(index=12, structure_leg_id="leg1")
        mss = _make_mss(index=15, structure_leg_id="leg1")

        strat.evaluate(_make_context(10, sweeps=(sw,), bias="bullish"))
        strat.evaluate(_make_context(11, bias="bullish"))
        strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(13, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(14, fvgs=(fvg,), bias="bullish"))
        strat.evaluate(_make_context(15, structure_events=(mss,), fvgs=(fvg,), bias="bullish"))

        # Bar 16 emits candidate
        c1 = strat.evaluate(_make_context(16, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(c1), 1)

        # Bar 17 continues to touch FVG
        c2 = strat.evaluate(_make_context(17, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(c2), 0)

        # Bar 18 continues to touch FVG
        c3 = strat.evaluate(_make_context(18, low_p=2038.0, high_p=2044.0, close_p=2041.0, structure_events=(mss,), fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(c3), 0)

    def test_78_batch_incremental_parity_with_semantic_fields(self):
        def _run_sequence():
            strat = S01ICT2022Strategy()
            all_cands = []
            sw = _make_sweep(index=10, structure_leg_id="leg1")
            fvg = _make_fvg(index=12, structure_leg_id="leg1")
            mss = _make_mss(index=15, structure_leg_id="leg1")

            for b in range(10, 20):
                sweeps = (sw,) if b == 10 else ()
                fvgs = (fvg,) if 12 <= b <= 17 else ()
                events = (mss,) if b >= 15 else ()
                low_p = 2038.0 if b >= 16 else 2050.0
                c = strat.evaluate(_make_context(
                    b,
                    low_p=low_p,
                    high_p=2052.0,
                    close_p=2050.0,
                    sweeps=sweeps,
                    structure_events=events,
                    fvgs=fvgs,
                    bias="bullish",
                ))
                all_cands.extend(c)
            return all_cands

        run1 = _run_sequence()
        run2 = _run_sequence()
        self.assertEqual(len(run1), 1)
        self.assertEqual(len(run2), 1)
        self.assertEqual(run1[0].setup_id, run2[0].setup_id)
        self.assertEqual(run1[0].meta, run2[0].meta)

    def test_79_no_future_leak(self):
        strat = S01ICT2022Strategy()
        sw = _make_sweep(index=10, structure_leg_id="leg1")
        # Ensure that evaluating bar 10 with future events not present emits nothing
        c0 = strat.evaluate(_make_context(10, sweeps=(sw,), bias="bullish"))
        self.assertEqual(len(c0), 0)
        # Intermediate bar 11
        strat.evaluate(_make_context(11, bias="bullish"))
        # Bar 12 has only FVG
        fvg = _make_fvg(index=12, structure_leg_id="leg1")
        c1 = strat.evaluate(_make_context(12, fvgs=(fvg,), bias="bullish"))
        self.assertEqual(len(c1), 0)
        # Verify narrative at bar 12 is only SWEEP_SEEN (waiting for MSS)
        self.assertEqual(len(strat._narratives), 1)
        active_n = next(iter(strat._narratives.values()))
        self.assertEqual(active_n.stage, NarrativeStage.SWEEP_SEEN)


if __name__ == "__main__":
    unittest.main()
