"""
tests/test_smc_engine_regime.py
================================
Exhaustive 60-test suite for MarketRegimeClassifier V1 (T53.7).
Covers Groups A through D:
- Group A: Config và model contracts (Tests 1-14)
- Group B: Regime warm-up và math (Tests 15-34)
- Group C: Five-regime tree (Tests 35-48)
- Group D: Classifier lifecycle (Tests 49-60)
"""

from __future__ import annotations

import copy
import datetime
import math
import subprocess
import sys
import unittest
from unittest.mock import patch
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from smc.engine.errors import StrategyStateError, StrategyValidationError
from smc.engine.models import (
    BiasStateSnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    MarketRegime,
    SessionDecisionSnapshot,
    StrategyContext,
    StructureEventSnapshot,
)
from smc.engine.regime import (
    RegimeClassifierConfig,
    MarketRegimeClassifier,
    classify_market_regimes,
)


def _bar_time(bar_index: int, base_iso: str = "2024-05-15T14:00:00+00:00") -> tuple[pd.Timestamp, pd.Timestamp]:
    base = pd.Timestamp(base_iso)
    open_t = base + pd.Timedelta(minutes=bar_index)
    close_t = open_t + pd.Timedelta(minutes=1)
    return open_t, close_t


def _make_bias(bias: str = "bullish", bar_index: int = 0, ref_time: Optional[pd.Timestamp] = None) -> BiasStateSnapshot:
    open_ts, _ = _bar_time(bar_index)
    ts = ref_time if ref_time is not None else open_ts
    return BiasStateSnapshot(
        bias=bias,  # type: ignore
        timestamp=ts,
        source_event_index=bar_index,
        source_event_time=ts,
        source_event_type="BOS",
        source_event_direction=bias,
        as_of=ts,
        reason="test_bias",
    )


def _make_structure(
    index: int = 6,
    direction: str = "bullish",
    event_type: str = "BOS",
    broken_swing_index: int = 4,
    broken_swing_price: float = 2045.0,
    displacement: bool = True,
    structure_leg_id: Optional[str] = "leg1",
    mode: str = "swing",
    confirmed_swing_at: Optional[int] = None,
) -> StructureEventSnapshot:
    open_ts, _ = _bar_time(index)
    conf_at = confirmed_swing_at if confirmed_swing_at is not None else index
    return StructureEventSnapshot(
        index=index,
        time=open_ts,
        direction=direction,  # type: ignore
        event_type=event_type,  # type: ignore
        broken_swing_index=broken_swing_index,
        broken_swing_price=broken_swing_price,
        close_price=broken_swing_price + (1.0 if direction == "bullish" else -1.0),
        displacement=displacement,
        structure_leg_id=structure_leg_id,
        mode=mode,  # type: ignore
        confirmed_swing_at=conf_at,
        break_type="close",
    )


def _make_sweep(
    index: int = 5,
    direction: str = "bullish",
    pool_kind: str = "equal_lows",
    price_wick: float = 2035.0,
    pool_price: float = 2036.0,
    close_price: float = 2038.0,
    swept_at: Optional[int] = None,
    confirmed_at: Optional[int] = None,
    mode: str = "swing",
    valid: bool = True,
) -> LiquiditySweepSnapshot:
    open_ts, _ = _bar_time(index)
    conf_at = confirmed_at if confirmed_at is not None else index
    sw_at = swept_at if swept_at is not None else index
    return LiquiditySweepSnapshot(
        index=index,
        time=open_ts,
        direction=direction,  # type: ignore
        pool_kind=pool_kind,
        pool_price=pool_price,
        pool_indices=(1, 3),
        price_wick=price_wick,
        close_price=close_price,
        created_at=index,
        confirmed_at=conf_at,
        swept_at=sw_at,
        valid=valid,
        mode=mode,  # type: ignore
    )


def _make_context(
    bar_index: int = 0,
    close: float = 2000.0,
    atr14: Optional[float] = 2.0,
    bias: Optional[str] = "bullish",
    structures: Sequence[StructureEventSnapshot] = (),
    sweeps: Sequence[LiquiditySweepSnapshot] = (),
    open_time: Optional[pd.Timestamp] = None,
    close_time: Optional[pd.Timestamp] = None,
) -> StrategyContext:
    def_o, def_c = _bar_time(bar_index)
    ts = open_time if open_time is not None else def_o
    bct = close_time if close_time is not None else def_c

    htf_bias = _make_bias(bias, bar_index, ref_time=ts) if bias is not None else None

    # Determine OHLC respecting high >= max(o, c) and low <= min(o, c)
    o = close
    c = close
    h = close + 1.0
    l = close - 1.0

    return StrategyContext(
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=bct,
        symbol="XAUUSD",
        timeframe="M1",
        open=o,
        high=h,
        low=l,
        close=c,
        volume=100.0,
        atr14=atr14 if atr14 is not None else 2.0,
        recent_structures=tuple(structures),
        recent_sweeps=tuple(sweeps),
        htf_bias=htf_bias,
    )


class TestSMCEngineRegime(unittest.TestCase):
    """60-test exhaustive suite for MarketRegimeClassifier V1."""

    # =========================================================================
    # Group A: Config and Model Contracts (Tests 1-14)
    # =========================================================================

    def test_01_config_defaults_exact(self):
        cfg = RegimeClassifierConfig()
        self.assertEqual(cfg.close_lookback, 20)
        self.assertEqual(cfg.atr_lookback, 100)
        self.assertEqual(cfg.er_threshold, 0.30)
        self.assertEqual(cfg.volatile_atr_percentile, 60.0)
        self.assertEqual(cfg.recent_sweep_bars, 20)
        self.assertEqual(cfg.structure_mode, "swing")

    def test_02_config_json_roundtrip(self):
        cfg = RegimeClassifierConfig(
            close_lookback=25,
            atr_lookback=80,
            er_threshold=0.35,
            volatile_atr_percentile=65.0,
            recent_sweep_bars=15,
            structure_mode="internal",
        )
        d = cfg.to_dict()
        cfg2 = RegimeClassifierConfig.from_dict(d)
        self.assertEqual(cfg, cfg2)

    def test_03_config_reject_bool_as_int_or_float(self):
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(close_lookback=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(atr_lookback=False)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(er_threshold=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(volatile_atr_percentile=False)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(recent_sweep_bars=True)  # type: ignore

    def test_04_config_reject_nan_inf(self):
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(er_threshold=float("nan"))
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(er_threshold=float("inf"))
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(volatile_atr_percentile=float("nan"))
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(volatile_atr_percentile=float("inf"))

    def test_05_config_reject_invalid_bounds(self):
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(close_lookback=1)
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(atr_lookback=0)
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(er_threshold=-0.01)
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(er_threshold=1.01)
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(volatile_atr_percentile=-0.1)
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(volatile_atr_percentile=100.1)
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(recent_sweep_bars=-1)
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig(structure_mode="unknown")

    def test_06_config_unknown_fields_reject(self):
        with self.assertRaises(StrategyValidationError):
            RegimeClassifierConfig.from_dict({"close_lookback": 20, "unknown_field": 123})

    def test_07_numpy_scalar_normalization(self):
        cfg = RegimeClassifierConfig(
            close_lookback=np.int64(22),  # type: ignore
            atr_lookback=np.int32(110),  # type: ignore
            er_threshold=np.float64(0.28),  # type: ignore
            volatile_atr_percentile=np.float32(58.0),  # type: ignore
            recent_sweep_bars=np.int64(18),  # type: ignore
        )
        self.assertIsInstance(cfg.close_lookback, int)
        self.assertIsInstance(cfg.atr_lookback, int)
        self.assertIsInstance(cfg.er_threshold, float)
        self.assertIsInstance(cfg.volatile_atr_percentile, float)
        self.assertIsInstance(cfg.recent_sweep_bars, int)

    def test_08_market_regime_model_roundtrip(self):
        reg = MarketRegime(
            regime="bullish_trend",
            bar_index=10,
            timestamp=pd.Timestamp("2024-05-15 14:10:00+00:00"),
            efficiency_ratio=0.6543,
            atr_percentile=75.5,
            metrics={"close_count": 20.0, "atr_count": 100.0},
            reason="bullish_trend_confirmed",
        )
        d = reg.to_dict()
        reg2 = MarketRegime.from_dict(d)
        self.assertEqual(reg.regime, reg2.regime)
        self.assertEqual(reg.bar_index, reg2.bar_index)
        self.assertEqual(reg.efficiency_ratio, reg2.efficiency_ratio)
        self.assertEqual(reg.atr_percentile, reg2.atr_percentile)
        self.assertEqual(reg.reason, reg2.reason)

    def test_09_market_regime_immutability(self):
        reg = MarketRegime(
            regime="ranging",
            bar_index=5,
            timestamp=pd.Timestamp("2024-05-15 14:05:00+00:00"),
            efficiency_ratio=0.12,
            atr_percentile=40.0,
        )
        with self.assertRaises(Exception):
            reg.regime = "bullish_trend"  # type: ignore

    def test_10_classifier_initialization_validation(self):
        with self.assertRaises(StrategyValidationError):
            MarketRegimeClassifier(config="invalid")  # type: ignore

    def test_11_fresh_process_import(self):
        cmd = [sys.executable, "-c", "from smc.engine.regime import MarketRegimeClassifier; print('OK')"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout.strip(), "OK")

    def test_12_config_immutability(self):
        cfg = RegimeClassifierConfig()
        with self.assertRaises(Exception):
            cfg.close_lookback = 30  # type: ignore

    def test_13_config_structure_mode_internal_allowed(self):
        cfg = RegimeClassifierConfig(structure_mode="internal")
        self.assertEqual(cfg.structure_mode, "internal")

    def test_14_market_regime_metrics_unfreeze(self):
        reg = MarketRegime(
            regime="uncertain",
            bar_index=1,
            timestamp=pd.Timestamp("2024-05-15 14:01:00+00:00"),
            efficiency_ratio=0.0,
            atr_percentile=0.0,
            metrics={"test_metric": 42.0},
        )
        d = reg.to_dict()
        self.assertIsInstance(d["metrics"], dict)
        self.assertEqual(d["metrics"]["test_metric"], 42.0)

    # =========================================================================
    # Group B: Warm-up and Math (Tests 15-34)
    # =========================================================================

    def test_15_19_closes_uncertain_warmup(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(19):
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i))
        assert res is not None
        self.assertEqual(res.regime, "uncertain")
        self.assertEqual(res.reason, "insufficient_warmup_bars")
        self.assertEqual(res.metrics["close_count"], 19.0)

    def test_16_20_closes_99_atr_uncertain_warmup(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(20):
            # bar 0 has atr14=None, so only 19 ATRs
            atr = None if i == 0 else 2.0
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=atr))
        assert res is not None
        self.assertEqual(res.regime, "uncertain")
        self.assertEqual(res.reason, "insufficient_warmup_bars")

    def test_17_exactly_20_closes_100_atr_passes_warmup(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=2.0))
        assert res is not None
        self.assertNotEqual(res.reason, "insufficient_warmup_bars")
        self.assertTrue(res.meta["is_warmed_up"])

    def test_18_large_bar_index_does_not_replace_finite_count(self):
        classifier = MarketRegimeClassifier()
        res = classifier.update(_make_context(bar_index=5000, close=2000.0))
        self.assertEqual(res.regime, "uncertain")
        self.assertEqual(res.reason, "insufficient_warmup_bars")

    def test_19_flat_close_denominator_er_zero(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            res = classifier.update(_make_context(bar_index=i, close=2000.0, atr14=2.0))
        assert res is not None
        self.assertEqual(res.efficiency_ratio, 0.0)
        self.assertEqual(res.metrics["er_denom"], 0.0)

    def test_20_monotonic_close_er_one(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i * 2.0, atr14=2.0))
        assert res is not None
        self.assertEqual(res.efficiency_ratio, 1.0)

    def test_21_zigzag_er_exact(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            c = 2000.0 if i % 2 == 0 else 2004.0
            res = classifier.update(_make_context(bar_index=i, close=c, atr14=2.0))
        assert res is not None
        # In last 20 closes (bars 80 to 99):
        # bar 80 (even) is 2000.0, bar 99 (odd) is 2004.0 -> change = 4.0
        # denom = 19 * 4.0 = 76.0 -> ER = 4.0 / 76.0 = 0.0526
        self.assertAlmostEqual(res.efficiency_ratio, 4.0 / 76.0, places=3)

    def test_22_er_threshold_030_inclusive(self):
        cfg = RegimeClassifierConfig(er_threshold=0.30)
        classifier = MarketRegimeClassifier(config=cfg)
        # Create a 20-close sequence with exact ER >= 0.30
        res = None
        for i in range(100):
            structs = [_make_structure(index=95, direction="bullish", event_type="BOS")] if i >= 95 else []
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 + i,
                atr14=2.0,
                bias="bullish",
                structures=structs,
            ))
        assert res is not None
        self.assertEqual(res.regime, "bullish_trend")

    def test_23_atr_equal_values_percentile_50(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            res = classifier.update(_make_context(bar_index=i, close=2000.0, atr14=3.5))
        assert res is not None
        # All 100 equal -> less=0, equal=100 -> 100*(0 + 0.5*100)/100 = 50.0
        self.assertEqual(res.atr_percentile, 50.0)

    def test_24_atr_percentile_60_inclusive(self):
        classifier = MarketRegimeClassifier(config=RegimeClassifierConfig(volatile_atr_percentile=60.0))
        # 40 ATRs at 1.0, 60 ATRs at 2.0 -> at 2.0: less=40, equal=60 -> 100*(40 + 30)/100 = 70.0
        res = None
        for i in range(100):
            a = 1.0 if i < 40 else 2.0
            sweep = [_make_sweep(index=i)] if i == 99 else []
            res = classifier.update(_make_context(bar_index=i, close=2000.0, atr14=a, sweeps=sweep, bias="neutral"))
        assert res is not None
        self.assertEqual(res.regime, "volatile_reversal")

    def test_25_trailing_only_atr_future_append_invariant(self):
        prefix_contexts = [_make_context(bar_index=i, close=2000.0 + i * 0.1, atr14=1.0 + (i % 5)) for i in range(100)]
        extended_contexts = prefix_contexts + [_make_context(bar_index=100 + j, close=2050.0, atr14=10.0) for j in range(10)]

        c1 = MarketRegimeClassifier()
        r1 = [c1.update(ctx) for ctx in prefix_contexts]

        c2 = MarketRegimeClassifier()
        r2 = [c2.update(ctx) for ctx in extended_contexts]

        for i in range(100):
            self.assertEqual(r1[i].regime, r2[i].regime)
            self.assertEqual(r1[i].efficiency_ratio, r2[i].efficiency_ratio)
            self.assertEqual(r1[i].atr_percentile, r2[i].atr_percentile)

    def test_26_bos_rolling_boundary_inclusive(self):
        classifier = MarketRegimeClassifier()
        # Bar 99: lookback window is [80, 99] (20 bars). BOS at bar 80 should be included!
        st = _make_structure(index=80, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            structs = [st] if i == 99 else []
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=2.0, structures=structs, bias="bullish"))
        assert res is not None
        self.assertEqual(res.metrics["bullish_bos_count"], 1.0)
        self.assertEqual(res.regime, "bullish_trend")

    def test_27_old_bos_outside_lookback_excluded(self):
        classifier = MarketRegimeClassifier()
        # Bar 99: lookback window is [80, 99]. BOS at bar 79 should be EXCLUDED!
        st = _make_structure(index=79, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            structs = [st] if i >= 79 else []
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=2.0, structures=structs, bias="bullish"))
        assert res is not None
        self.assertEqual(res.metrics["bullish_bos_count"], 0.0)
        self.assertNotEqual(res.regime, "bullish_trend")

    def test_28_rolling_buffer_repeated_bos_counted_once(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=90, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            # Same structure present in multiple contexts
            structs = [st] if i >= 90 else []
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=2.0, structures=structs, bias="bullish"))
        assert res is not None
        self.assertEqual(res.metrics["bullish_bos_count"], 1.0)

    def test_29_choch_not_counted_as_bos(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=95, direction="bullish", event_type="CHoCH")
        res = None
        for i in range(100):
            structs = [st] if i == 99 else []
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=2.0, structures=structs, bias="bullish"))
        assert res is not None
        self.assertEqual(res.metrics["bullish_bos_count"], 0.0)

    def test_30_wrong_structure_mode_ignored(self):
        classifier = MarketRegimeClassifier(config=RegimeClassifierConfig(structure_mode="swing"))
        st = _make_structure(index=95, direction="bullish", event_type="BOS", mode="internal")
        res = None
        for i in range(100):
            structs = [st] if i == 99 else []
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=2.0, structures=structs, bias="bullish"))
        assert res is not None
        self.assertEqual(res.metrics["bullish_bos_count"], 0.0)

    def test_31_sweep_age_20_inclusive(self):
        classifier = MarketRegimeClassifier()
        # Bar 99: sweep at bar 79 (age = 99 - 79 = 20) -> INCLUDED
        sw = _make_sweep(index=79, direction="bullish")
        res = None
        for i in range(100):
            sweeps = [sw] if i >= 79 else []
            a = 1.0 if i < 40 else 2.5
            res = classifier.update(_make_context(bar_index=i, close=2000.0, atr14=a, sweeps=sweeps, bias="neutral"))
        assert res is not None
        self.assertEqual(res.metrics["recent_sweep"], 1.0)
        self.assertEqual(res.regime, "volatile_reversal")

    def test_32_sweep_age_21_excluded(self):
        classifier = MarketRegimeClassifier()
        # Bar 99: sweep at bar 78 (age = 99 - 78 = 21) -> EXCLUDED
        sw = _make_sweep(index=78, direction="bullish")
        res = None
        for i in range(100):
            sweeps = [sw] if i >= 78 else []
            a = 1.0 if i < 40 else 2.5
            res = classifier.update(_make_context(bar_index=i, close=2000.0, atr14=a, sweeps=sweeps, bias="neutral"))
        assert res is not None
        self.assertEqual(res.metrics["recent_sweep"], 0.0)
        self.assertNotEqual(res.regime, "volatile_reversal")

    def test_33_invalid_sweep_ignored(self):
        classifier = MarketRegimeClassifier()
        sw = _make_sweep(index=95, valid=False)
        res = None
        for i in range(100):
            sweeps = [sw] if i == 99 else []
            res = classifier.update(_make_context(bar_index=i, close=2000.0, atr14=2.0, sweeps=sweeps, bias="neutral"))
        assert res is not None
        self.assertEqual(res.metrics["recent_sweep"], 0.0)

    def test_34_metrics_payload_exact_and_json_safe(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            res = classifier.update(_make_context(bar_index=i, close=2000.0 + i, atr14=2.0))
        assert res is not None
        expected_keys = {
            "close_count", "atr_count", "er_change", "er_denom", "efficiency_ratio",
            "current_atr", "atr_percentile", "bullish_bos_count", "bearish_bos_count", "recent_sweep"
        }
        self.assertEqual(set(res.metrics.keys()), expected_keys)
        for k, v in res.metrics.items():
            self.assertIsInstance(v, float)
            self.assertTrue(math.isfinite(v))

    # =========================================================================
    # Group C: Five-Regime Tree (Tests 35-48)
    # =========================================================================

    def test_35_bullish_trend_happy_path(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=95, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 + i,
                atr14=2.0,
                bias="bullish",
                structures=[st] if i >= 95 else [],
            ))
        assert res is not None
        self.assertEqual(res.regime, "bullish_trend")
        self.assertEqual(res.reason, "bullish_trend_confirmed")

    def test_36_bearish_trend_mirror(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=95, direction="bearish", event_type="BOS")
        res = None
        for i in range(100):
            res = classifier.update(_make_context(
                bar_index=i,
                close=2100.0 - i,
                atr14=2.0,
                bias="bearish",
                structures=[st] if i >= 95 else [],
            ))
        assert res is not None
        self.assertEqual(res.regime, "bearish_trend")
        self.assertEqual(res.reason, "bearish_trend_confirmed")

    def test_37_bull_trend_requires_no_bearish_bos(self):
        classifier = MarketRegimeClassifier()
        st1 = _make_structure(index=92, direction="bullish", event_type="BOS")
        st2 = _make_structure(index=95, direction="bearish", event_type="BOS")
        res = None
        for i in range(100):
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 + i,
                atr14=2.0,
                bias="bullish",
                structures=[st1, st2] if i >= 95 else [],
            ))
        assert res is not None
        self.assertNotEqual(res.regime, "bullish_trend")

    def test_38_bear_trend_requires_no_bullish_bos(self):
        classifier = MarketRegimeClassifier()
        st1 = _make_structure(index=92, direction="bearish", event_type="BOS")
        st2 = _make_structure(index=95, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            res = classifier.update(_make_context(
                bar_index=i,
                close=2100.0 - i,
                atr14=2.0,
                bias="bearish",
                structures=[st1, st2] if i >= 95 else [],
            ))
        assert res is not None
        self.assertNotEqual(res.regime, "bearish_trend")

    def test_39_trend_requires_er_ge_030(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=95, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            # Zigzag close keeps ER low
            c = 2000.0 if i % 2 == 0 else 2002.0
            res = classifier.update(_make_context(
                bar_index=i,
                close=c,
                atr14=2.0,
                bias="bullish",
                structures=[st] if i >= 95 else [],
            ))
        assert res is not None
        self.assertLess(res.efficiency_ratio, 0.30)
        self.assertNotEqual(res.regime, "bullish_trend")

    def test_40_volatile_reversal_happy_path(self):
        classifier = MarketRegimeClassifier()
        sw = _make_sweep(index=90)
        res = None
        for i in range(100):
            a = 1.0 if i < 40 else 3.0
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0,
                atr14=a,
                sweeps=[sw] if i >= 90 else [],
                bias="neutral",
            ))
        assert res is not None
        self.assertEqual(res.regime, "volatile_reversal")
        self.assertEqual(res.reason, "volatile_reversal_confirmed")

    def test_41_volatile_reversal_requires_sweep(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            a = 1.0 if i < 40 else 3.0
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0,
                atr14=a,
                sweeps=[],
                bias="neutral",
            ))
        assert res is not None
        self.assertNotEqual(res.regime, "volatile_reversal")

    def test_42_volatile_reversal_requires_atr_percentile_ge_60(self):
        classifier = MarketRegimeClassifier()
        sw = _make_sweep(index=90)
        res = None
        for i in range(100):
            # High ATR initially, drop to low ATR -> current ATR has low percentile
            a = 3.0 if i < 80 else 1.0
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0,
                atr14=a,
                sweeps=[sw] if i >= 90 else [],
                bias="neutral",
            ))
        assert res is not None
        self.assertLess(res.atr_percentile, 60.0)
        self.assertNotEqual(res.regime, "volatile_reversal")

    def test_43_ranging_happy_path(self):
        classifier = MarketRegimeClassifier()
        res = None
        for i in range(100):
            # Constant or mild fluctuating close -> low ER, constant ATR -> percentile 50 (<60), 0 BOS
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 if i % 2 == 0 else 2000.5,
                atr14=2.0,
                bias="neutral",
            ))
        assert res is not None
        self.assertEqual(res.regime, "ranging")
        self.assertEqual(res.reason, "ranging_confirmed")

    def test_44_ranging_rejects_bos_presence(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=95, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 if i % 2 == 0 else 2000.5,
                atr14=2.0,
                bias="neutral",
                structures=[st] if i >= 95 else [],
            ))
        assert res is not None
        self.assertNotEqual(res.regime, "ranging")

    def test_45_ranging_threshold_boundaries(self):
        classifier = MarketRegimeClassifier()
        # If ATR percentile >= 60.0 -> ranging excluded
        res = None
        for i in range(100):
            a = 1.0 if i < 30 else 2.5
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 if i % 2 == 0 else 2000.5,
                atr14=a,
                bias="neutral",
            ))
        assert res is not None
        self.assertGreaterEqual(res.atr_percentile, 60.0)
        self.assertNotEqual(res.regime, "ranging")

    def test_46_uncertain_conflicting_bos(self):
        classifier = MarketRegimeClassifier()
        st1 = _make_structure(index=92, direction="bullish", event_type="BOS")
        st2 = _make_structure(index=95, direction="bearish", event_type="BOS")
        res = None
        for i in range(100):
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 if i % 2 == 0 else 2000.5,
                atr14=2.0,
                bias="neutral",
                structures=[st1, st2] if i >= 95 else [],
            ))
        assert res is not None
        self.assertEqual(res.regime, "uncertain")
        self.assertEqual(res.reason, "conflicting_bos")

    def test_47_priority_trend_before_volatile_reversal(self):
        # Both trend condition (ER>=0.30, bullish bias, bullish BOS) and volatile reversal (high ATR, sweep) met
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=95, direction="bullish", event_type="BOS")
        sw = _make_sweep(index=90, direction="bullish")
        res = None
        for i in range(100):
            a = 1.0 if i < 40 else 3.0
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 + i,
                atr14=a,
                bias="bullish",
                structures=[st] if i >= 95 else [],
                sweeps=[sw] if i >= 90 else [],
            ))
        assert res is not None
        self.assertEqual(res.regime, "bullish_trend")

    def test_48_missing_or_neutral_htf_bias_deterministic(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=95, direction="bullish", event_type="BOS")
        res = None
        for i in range(100):
            res = classifier.update(_make_context(
                bar_index=i,
                close=2000.0 + i,
                atr14=2.0,
                bias="neutral",
                structures=[st] if i >= 95 else [],
            ))
        assert res is not None
        self.assertNotEqual(res.regime, "bullish_trend")
        self.assertEqual(res.regime, "uncertain")

    # =========================================================================
    # Group D: Classifier Lifecycle (Tests 49-60)
    # =========================================================================

    def test_49_incremental_output_length(self):
        classifier = MarketRegimeClassifier()
        results = [classifier.update(_make_context(bar_index=i, close=2000.0 + i)) for i in range(105)]
        self.assertEqual(len(results), 105)

    def test_50_batch_helper_parity(self):
        contexts = [_make_context(bar_index=i, close=2000.0 + (i % 5), atr14=2.0) for i in range(105)]
        c_seq = MarketRegimeClassifier()
        seq_results = tuple(c_seq.update(ctx) for ctx in contexts)

        batch_results = classify_market_regimes(contexts)
        self.assertEqual(len(seq_results), len(batch_results))
        for r1, r2 in zip(seq_results, batch_results):
            self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_51_json_context_replay_parity(self):
        contexts = [_make_context(bar_index=i, close=2000.0 + i, atr14=2.0) for i in range(105)]
        reconstructed = [StrategyContext.from_dict(ctx.to_dict()) for ctx in contexts]

        r1 = classify_market_regimes(contexts)
        r2 = classify_market_regimes(reconstructed)
        for a, b in zip(r1, r2):
            self.assertEqual(a.to_dict(), b.to_dict())

    def test_52_same_bar_identical_retry_cached(self):
        classifier = MarketRegimeClassifier()
        ctx = _make_context(bar_index=0, close=2000.0)
        r1 = classifier.update(ctx)
        r2 = classifier.update(ctx)
        self.assertIs(r1, r2)

    def test_53_same_bar_conflict_raises(self):
        classifier = MarketRegimeClassifier()
        ctx1 = _make_context(bar_index=0, close=2000.0)
        classifier.update(ctx1)
        ctx2 = _make_context(bar_index=0, close=2005.0)
        with self.assertRaises(StrategyStateError):
            classifier.update(ctx2)

    def test_54_gap_raises_state_error(self):
        classifier = MarketRegimeClassifier()
        classifier.update(_make_context(bar_index=5, close=2000.0))
        with self.assertRaises(StrategyStateError):
            classifier.update(_make_context(bar_index=7, close=2002.0))

    def test_55_backward_raises_state_error(self):
        classifier = MarketRegimeClassifier()
        classifier.update(_make_context(bar_index=5, close=2000.0))
        with self.assertRaises(StrategyStateError):
            classifier.update(_make_context(bar_index=4, close=2002.0))

    def test_56_timestamp_non_increasing_raises(self):
        classifier = MarketRegimeClassifier()
        t1, c1 = _bar_time(0)
        classifier.update(_make_context(bar_index=0, open_time=t1, close_time=c1))
        # Same timestamp on bar 1
        with self.assertRaises(StrategyStateError):
            classifier.update(_make_context(bar_index=1, open_time=t1, close_time=c1))

    def test_57_fault_injection_rollback(self):
        classifier = MarketRegimeClassifier()
        for i in range(10):
            classifier.update(_make_context(bar_index=i, close=2000.0 + i))

        pre_closes = list(classifier._closes)
        pre_last_bar = classifier._last_bar_index

        # Inject error during update
        with patch.object(classifier, "_assert_zero_future_leak", side_effect=RuntimeError("injected_fault")):
            with self.assertRaises(RuntimeError):
                classifier.update(_make_context(bar_index=10, close=2010.0))

        self.assertEqual(classifier._closes, pre_closes)
        self.assertEqual(classifier._last_bar_index, pre_last_bar)

    def test_58_reset_equals_fresh_instance(self):
        classifier = MarketRegimeClassifier()
        for i in range(50):
            classifier.update(_make_context(bar_index=i, close=2000.0 + i))
        classifier.reset()

        fresh = MarketRegimeClassifier()
        ctx = _make_context(bar_index=0, close=2000.0)
        r1 = classifier.update(ctx)
        r2 = fresh.update(ctx)
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_59_long_stream_bounded_state(self):
        classifier = MarketRegimeClassifier()
        for i in range(1000):
            classifier.update(_make_context(bar_index=i, close=2000.0 + (i % 20), atr14=2.0))

        self.assertLessEqual(len(classifier._closes), classifier.config.close_lookback)
        self.assertLessEqual(len(classifier._atrs), classifier.config.atr_lookback)
        self.assertEqual(classifier._last_bar_index, 999)

    def test_60_input_context_immutable(self):
        classifier = MarketRegimeClassifier()
        st = _make_structure(index=0)
        ctx = _make_context(bar_index=0, close=2000.0, structures=[st])
        ctx_dict_before = ctx.to_dict()
        classifier.update(ctx)
        self.assertEqual(ctx.to_dict(), ctx_dict_before)

    def test_61_atr_zero_included_in_warmup(self):
        """Test 61 (P1.1): ATR = 0.0 must be accepted into warm-up buffer and warm up after 100 bars."""
        classifier = MarketRegimeClassifier()
        for i in range(100):
            ctx = _make_context(bar_index=i, close=2000.0, atr14=0.0)
            res = classifier.update(ctx)

        self.assertEqual(len(classifier._atrs), 100)
        self.assertTrue(res.meta["is_warmed_up"])
        self.assertNotEqual(res.reason, "insufficient_warmup_bars")
        # With 20 flat closes and no BOS: change=0, denom=0 -> ER=0.0 < 0.30, atr_pct=50.0 -> ranging
        self.assertEqual(res.regime, "ranging")
        self.assertEqual(res.reason, "ranging_confirmed")

    def test_62_unrounded_er_threshold_boundary(self):
        """Test 62 (P1.2): ER comparison must use full float precision without premature rounding."""
        classifier = MarketRegimeClassifier(RegimeClassifierConfig(close_lookback=20, er_threshold=0.30))
        # Feed 100 warm-up bars
        for i in range(100):
            classifier.update(_make_context(bar_index=i, close=2000.0, atr14=1.0))

        # We construct 20 closes such that true ER is 0.29997 (rounds to 0.3000 at 4 decimals)
        # change = |c[-1] - c[0]|
        # denom = sum(|c[i] - c[i-1]|)
        # Let denom = 100.0, change = 29.997 -> ER = 0.29997 < 0.30!
        closes = [2000.0]
        # To get change = 29.997 and denom = 100.0:
        # Move up by 29.997, then oscillate up and down to add path length without net change
        # Specifically: step 1 to 10: +2.9997 each (total +29.997, path = 29.997)
        # steps 11 to 19: oscillate up +35.0015 and down -35.0015 so net change is 0, additional path = 70.003
        # total path = 29.997 + 70.003 = 100.0, total change = 29.997 -> ER = 0.29997 exactly.
        curr = 2000.0
        for _ in range(10):
            curr += 2.9997
            closes.append(curr)
        osc_step = (100.0 - 29.997) / 18.0  # 9 pairs of up/down
        for _ in range(4):
            curr += osc_step
            closes.append(curr)
            curr -= osc_step
            closes.append(curr)
        # One last step to reach 20 closes (19 steps)
        # Remaining path = 100.0 - (29.997 + 8 * osc_step) = 100.0 - (29.997 + 31.11244) ...
        # Let's directly set closes of 20 bars:
        test_closes = [100.0] * 20
        # More direct: set test_closes: c[0] = 0.0, c[1] = 70.003, c[19] = 29.997
        # Path: |70.003 - 0| + |29.997 - 70.003| = 70.003 + 40.006 = 110.009...
        # If c[0] = 0.0, c[1..18] = 64.9985, c[19] = 29.997:
        # |64.9985 - 0| + |29.997 - 64.9985| = 64.9985 + 35.0015 = 100.0000
        # Change = |29.997 - 0| = 29.997 -> ER = 29.997 / 100.0 = 0.299970!
        exact_closes = [2000.0 + x for x in [0.0] + [64.9985] * 18 + [29.997]]
        self.assertEqual(len(exact_closes), 20)
        change = abs(exact_closes[-1] - exact_closes[0])
        denom = sum(abs(exact_closes[i] - exact_closes[i-1]) for i in range(1, 20))
        calc_er = change / denom
        self.assertAlmostEqual(calc_er, 0.29997, places=6)
        self.assertLess(calc_er, 0.30)
        self.assertEqual(round(calc_er, 4), 0.3000)

        # Feed these 20 closes with a bullish BOS and bullish bias
        clf2 = MarketRegimeClassifier(RegimeClassifierConfig(close_lookback=20, er_threshold=0.30))
        for i in range(100):
            clf2.update(_make_context(bar_index=i, close=2000.0, atr14=1.0))
        
        bos = _make_structure(index=119, direction="bullish", event_type="BOS")
        for i, cl in enumerate(exact_closes):
            bar_idx = 100 + i
            st_list = [bos] if bar_idx == 119 else []
            res2 = clf2.update(_make_context(bar_index=bar_idx, close=cl, atr14=1.0, structures=st_list, bias="bullish"))

        # Because ER 0.29997 < 0.30, it must NOT enter bullish_trend!
        self.assertNotEqual(res2.regime, "bullish_trend")
        self.assertEqual(res2.metrics["efficiency_ratio"], 0.3000)  # Rounded in metrics
        self.assertEqual(res2.efficiency_ratio, 0.3000)  # Stored rounded in MarketRegime model

    def test_63_sweep_valid_to_invalid_replacement(self):
        """Test 63 (P1.3): Invalid sweep replaces/clears previous valid snapshot."""
        classifier = MarketRegimeClassifier(RegimeClassifierConfig(close_lookback=20, recent_sweep_bars=10))
        # Feed warm-up
        for i in range(100):
            classifier.update(_make_context(bar_index=i, close=2000.0, atr14=1.0))

        # Bar 100: sweep valid=True
        sw_valid = _make_sweep(index=100, direction="bullish", pool_price=2000.0, valid=True)
        res100 = classifier.update(_make_context(bar_index=100, close=2000.0, atr14=1.0, sweeps=[sw_valid]))
        self.assertEqual(res100.metrics["recent_sweep"], 1.0)

        # Bar 101: same sweep appears with valid=False (invalidated)
        sw_invalid = _make_sweep(index=100, direction="bullish", pool_price=2000.0, valid=False)
        res101 = classifier.update(_make_context(bar_index=101, close=2000.0, atr14=1.0, sweeps=[sw_invalid]))
        # Must no longer count as recent_sweep!
        self.assertEqual(res101.metrics["recent_sweep"], 0.0)


if __name__ == "__main__":
    unittest.main()
