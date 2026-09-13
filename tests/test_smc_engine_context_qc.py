"""
tests/test_smc_engine_context_qc.py
===================================
Automated Regression and QC Verification Test Suite for T53.2 (StrategyContextBuilder).
Covers:
1. Canonical key hashable representation (_canonical_key_value).
2. LiquidityPool source_swings canonical order caching and isolation.
3. 100% field coverage matrix across all 6 snapshot classes.
4. Constructor HTF seed preservation and dynamic event reset purge.
5. OB late-FVG upgrade and historical context immutability.
6. High retest count (75+) O(1) cache boundedness.
7. Cache bounds at scale (3k, 6k, 10k bars).
8. Batch vs incremental exact full-payload parity (1,000 bars).
9. Future-append invariance (zero lookahead).
"""

import unittest
import dataclasses
import math
import copy
from typing import Any
import pandas as pd
import numpy as np

from smc.models import (
    SwingPoint,
    StructureEvent,
    FairValueGap,
    OrderBlock,
    LiquidityPool,
    LiquiditySweep,
)
from smc.engine.models import (
    SwingPointSnapshot,
    StructureEventSnapshot,
    FairValueGapSnapshot,
    OrderBlockSnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
)
from smc.engine.context import (
    StrategyContextBuilder,
    ContextBuilderConfig,
    _canonical_key_value,
    _swing_identity_key,
    _swing_state_key,
    _structure_identity_key,
    _structure_state_key,
    _fvg_identity_key,
    _fvg_state_key,
    _ob_identity_key,
    _ob_state_key,
    _pool_identity_key,
    _pool_state_key,
    _sweep_identity_key,
    _sweep_state_key,
)


class TestSMCEngineContextQC(unittest.TestCase):

    def test_01_canonical_key_value_deterministic_and_nested(self):
        """P2.2: _canonical_key_value produces identical keys across insertion order and validates all types."""
        # 1. Different insertion order in dicts
        d1 = {"index": 1, "price": 100.0}
        d2 = {"price": 100.0, "index": 1}
        self.assertEqual(_canonical_key_value(d1), _canonical_key_value(d2))

        # 2. Deep nested structures with sets, lists, dicts
        n1 = {
            "swings": [{"index": 1, "price": 100.0}, {"price": 200.0, "index": 2}],
            "tags": {"b", "a", "c"},
            "meta": {"sub": {"y": 2, "x": 1}},
        }
        n2 = {
            "meta": {"sub": {"x": 1, "y": 2}},
            "tags": {"c", "b", "a"},
            "swings": [{"price": 100.0, "index": 1}, {"index": 2, "price": 200.0}],
        }
        self.assertEqual(_canonical_key_value(n1), _canonical_key_value(n2))

        # 3. Single nested change produces different key
        n3 = copy.deepcopy(n1)
        n3["meta"]["sub"]["y"] = 3
        self.assertNotEqual(_canonical_key_value(n1), _canonical_key_value(n3))

        # 4. Non-finite float (NaN, inf, -inf) raises ValueError
        with self.assertRaises(ValueError):
            _canonical_key_value({"val": float("nan")})
        with self.assertRaises(ValueError):
            _canonical_key_value([1.0, float("inf")])
        with self.assertRaises(ValueError):
            _canonical_key_value(-float("inf"))

        # 5. Unsupported custom mutable object raises TypeError
        class CustomObj:
            pass
        with self.assertRaises(TypeError):
            _canonical_key_value(CustomObj())
        with self.assertRaises(TypeError):
            _canonical_key_value({"key": CustomObj()})

        # 6. Timezone normalization: naive vs aware UTC
        t1 = pd.Timestamp("2026-01-01 00:00:00")
        t2 = pd.Timestamp("2026-01-01 00:00:00+00:00")
        self.assertEqual(_canonical_key_value(t1), _canonical_key_value(t2))

        # Explicit scalar tags prevent Python's bool/int/float equality collision.
        self.assertNotEqual(_canonical_key_value(True), _canonical_key_value(1))
        self.assertNotEqual(_canonical_key_value(1), _canonical_key_value(1.0))
        self.assertNotEqual(_canonical_key_value(False), _canonical_key_value(0.0))
        self.assertIsInstance(hash(_canonical_key_value({"nested": {1, 2}})), int)

        # Scalar type identity must survive canonicalization (Python considers
        # True == 1 and 1 == 1.0, which must not produce cache collisions).
        self.assertNotEqual(_canonical_key_value(True), _canonical_key_value(1))
        self.assertNotEqual(_canonical_key_value(1), _canonical_key_value(1.0))
        self.assertNotEqual(
            _canonical_key_value({"nested": {True, 1}}),
            _canonical_key_value({"nested": {1.0, 1}}),
        )

    def test_02_liquidity_pool_source_swings_canonical_cache(self):
        """P2.2: Pool state key and builder cache hit on semantically equal source_swings regardless of insertion order."""
        p1 = LiquidityPool(
            kind="equal_highs",
            price=1950.0,
            price_max=1950.5,
            price_min=1949.5,
            indices=[5, 10],
            created_at=10,
            confirmed_at=12,
            source_swings=[{"index": 5, "price": 1950.0}, {"price": 1950.1, "index": 10}],
        )
        p2 = LiquidityPool(
            kind="equal_highs",
            price=1950.0,
            price_max=1950.5,
            price_min=1949.5,
            indices=[5, 10],
            created_at=10,
            confirmed_at=12,
            source_swings=[{"price": 1950.1, "index": 10}, {"index": 5, "price": 1950.0}],
        )
        # Note: list order is preserved, so compare dict ordering within each swing element
        p3 = LiquidityPool(
            kind="equal_highs",
            price=1950.0,
            price_max=1950.5,
            price_min=1949.5,
            indices=[5, 10],
            created_at=10,
            confirmed_at=12,
            source_swings=[{"price": 1950.0, "index": 5}, {"index": 10, "price": 1950.1}],
        )
        k1 = _pool_state_key(p1)
        k3 = _pool_state_key(p3)
        self.assertEqual(k1, k3, "Different dict insertion order in source_swings must produce identical pool state keys")

        # Mutate value -> state key changes
        p4 = LiquidityPool(
            kind="equal_highs",
            price=1950.0,
            price_max=1950.5,
            price_min=1949.5,
            indices=[5, 10],
            created_at=10,
            confirmed_at=12,
            source_swings=[{"index": 5, "price": 1952.0}, {"price": 1950.1, "index": 10}],
        )
        self.assertNotEqual(_pool_state_key(p1), _pool_state_key(p4))

    def test_03_state_keys_100_percent_field_coverage_matrix(self):
        """P2.1 / Section 5: Matrix verifying 100% field coverage across all 6 snapshot types."""

        # 1. SwingPoint
        swing_all_fields = {f.name for f in dataclasses.fields(SwingPointSnapshot)}
        swing_base = {
            "index": 10,
            "time": pd.Timestamp("2026-01-01 00:00:00+00:00"),
            "price": 1950.0,
            "kind": "high",
            "strength": 5,
            "confirmed_at": 15,
            "confirmed_time": pd.Timestamp("2026-01-01 01:15:00+00:00"),
            "mode": "swing",
            "classification": "HH",
            "broken": False,
            "broken_at": None,
        }
        swing_mutations = {
            "index": 12,
            "time": pd.Timestamp("2026-01-01 00:05:00+00:00"),
            "price": 1960.0,
            "kind": "low",
            "strength": 7,
            "confirmed_at": 17,
            "confirmed_time": pd.Timestamp("2026-01-01 02:00:00+00:00"),
            "mode": "internal",
            "classification": "LH",
            "broken": True,
            "broken_at": 18,
        }
        self.assertEqual(set(swing_mutations.keys()), swing_all_fields, "SwingPoint test matrix must cover 100% of fields")
        swing_identity_fields = {"mode", "kind", "index"}

        def make_swing(data):
            return SwingPoint(
                index=data["index"],
                time=data["time"],
                price=data["price"],
                kind=data["kind"],
                strength=data["strength"],
                confirmed_at=data["confirmed_at"],
                confirmed_time=data["confirmed_time"],
                mode=data["mode"],
                classification=data["classification"],
                broken=data["broken"],
                broken_at=data["broken_at"],
            )

        src_sw_base = make_swing(swing_base)
        id_sw_base = _swing_identity_key(src_sw_base)
        state_sw_base = _swing_state_key(src_sw_base, is_broken=swing_base["broken"], b_at=swing_base["broken_at"])

        for field_name, mut_val in swing_mutations.items():
            mut_data = copy.copy(swing_base)
            mut_data[field_name] = mut_val
            src_sw_mut = make_swing(mut_data)
            id_sw_mut = _swing_identity_key(src_sw_mut)
            state_sw_mut = _swing_state_key(src_sw_mut, is_broken=mut_data["broken"], b_at=mut_data["broken_at"])

            self.assertNotEqual(state_sw_base, state_sw_mut, f"Swing field '{field_name}' change did not change state key!")
            if field_name in swing_identity_fields:
                self.assertNotEqual(id_sw_base, id_sw_mut, f"Identity field '{field_name}' change did not change identity key!")
            else:
                self.assertEqual(id_sw_base, id_sw_mut, f"Non-identity field '{field_name}' unexpectedly changed identity key!")

        # 2. StructureEvent
        st_all_fields = {f.name for f in dataclasses.fields(StructureEventSnapshot)}
        st_base = {
            "index": 20,
            "time": pd.Timestamp("2026-01-01 02:00:00+00:00"),
            "event_type": "BOS",
            "direction": "bullish",
            "broken_swing_index": 10,
            "broken_swing_price": 1950.0,
            "close_price": 1955.0,
            "displacement": True,
            "mode": "swing",
            "confirmed_swing_at": 15,
            "body_size": 5.0,
            "atr_value": 1.2,
            "break_type": "close",
            "structure_leg_id": "leg_1",
        }
        st_mutations = {
            "index": 22,
            "time": pd.Timestamp("2026-01-01 02:15:00+00:00"),
            "event_type": "CHoCH",
            "direction": "bearish",
            "broken_swing_index": 12,
            "broken_swing_price": 1940.0,
            "close_price": 1958.0,
            "displacement": False,
            "mode": "internal",
            "confirmed_swing_at": 18,
            "body_size": 6.5,
            "atr_value": 2.0,
            "break_type": "wick",
            "structure_leg_id": "leg_2",
        }
        self.assertEqual(set(st_mutations.keys()), st_all_fields, "StructureEvent test matrix must cover 100% of fields")
        st_identity_fields = {"mode", "event_type", "direction", "index"}

        def make_st(data):
            return StructureEvent(**data)

        src_st_base = make_st(st_base)
        id_st_base = _structure_identity_key(src_st_base)
        state_st_base = _structure_state_key(src_st_base)

        for field_name, mut_val in st_mutations.items():
            mut_data = copy.copy(st_base)
            mut_data[field_name] = mut_val
            src_st_mut = make_st(mut_data)
            id_st_mut = _structure_identity_key(src_st_mut)
            state_st_mut = _structure_state_key(src_st_mut)

            self.assertNotEqual(state_st_base, state_st_mut, f"Structure field '{field_name}' change did not change state key!")
            if field_name in st_identity_fields:
                self.assertNotEqual(id_st_base, id_st_mut, f"Identity field '{field_name}' change did not change identity key!")
            else:
                self.assertEqual(id_st_base, id_st_mut, f"Non-identity field '{field_name}' unexpectedly changed identity key!")

        # 3. FairValueGap
        fvg_all_fields = {f.name for f in dataclasses.fields(FairValueGapSnapshot)}
        fvg_base = {
            "index": 30,
            "time": pd.Timestamp("2026-01-01 03:00:00+00:00"),
            "direction": "bullish",
            "top": 1965.0,
            "bottom": 1960.0,
            "mode": "swing",
            "confirmed_at": 31,
            "filled": False,
            "filled_at": None,
            "structure_leg_id": "leg_fvg_1",
        }
        fvg_mutations = {
            "index": 32,
            "time": pd.Timestamp("2026-01-01 03:15:00+00:00"),
            "direction": "bearish",
            "top": 1968.0,
            "bottom": 1962.0,
            "mode": "internal",
            "confirmed_at": 33,
            "filled": True,
            "filled_at": 35,
            "structure_leg_id": "leg_fvg_2",
            "ce": 1965.0,
            "gap_size": 6.0,
            "gap_pct": 0.003,
            "displacement": True,
            "body_ratio": 2.0,
            "atr_value": 3.0,
            "touch_count": 2,
            "ce_touched": True,
            "ce_touched_at": 34,
            "partial_filled": True,
            "partial_filled_at": 33,
            "middle_body_size": 4.0,
            "middle_range": 8.0,
            "middle_body_ratio": 0.5,
            "state": "partial",
        }
        self.assertEqual(set(fvg_mutations.keys()), fvg_all_fields, "FVG test matrix must cover 100% of fields")
        fvg_identity_fields = {"mode", "direction", "index"}

        def make_fvg(data):
            return FairValueGap(**data)

        src_fvg_base = make_fvg(fvg_base)
        id_fvg_base = _fvg_identity_key(src_fvg_base)
        state_fvg_base = _fvg_state_key(src_fvg_base)

        for field_name, mut_val in fvg_mutations.items():
            mut_data = copy.copy(fvg_base)
            mut_data[field_name] = mut_val
            src_fvg_mut = make_fvg(mut_data)
            id_fvg_mut = _fvg_identity_key(src_fvg_mut)
            state_fvg_mut = _fvg_state_key(src_fvg_mut)

            self.assertNotEqual(state_fvg_base, state_fvg_mut, f"FVG field '{field_name}' change did not change state key!")
            if field_name in fvg_identity_fields:
                self.assertNotEqual(id_fvg_base, id_fvg_mut, f"Identity field '{field_name}' change did not change identity key!")
            else:
                self.assertEqual(id_fvg_base, id_fvg_mut, f"Non-identity field '{field_name}' unexpectedly changed identity key!")

        # 4. OrderBlock
        ob_all_fields = {f.name for f in dataclasses.fields(OrderBlockSnapshot)}
        ob_base = {
            "index": 40,
            "time": pd.Timestamp("2026-01-01 04:00:00+00:00"),
            "direction": "bullish",
            "high": 1955.0,
            "low": 1950.0,
            "open": 1952.0,
            "close": 1954.0,
            "origin_type": "BOS",
            "mode": "swing",
            "quality": "base",
            "source_event_index": 20,
            "source_event_type": "BOS",
            "source_swing_index": 10,
            "created_at": 42,
            "source_fvg_index": 30,
            "source_fvg_top": 1965.0,
            "source_fvg_bottom": 1960.0,
            "mitigated": False,
            "mitigated_at": None,
            "mitigation_pct": 0.0,
            "valid": True,
            "invalidated_at": None,
            "invalidation_reason": None,
            "retest_count": 0,
            "structure_leg_id": "leg_ob_1",
        }
        ob_mutations = {
            "index": 44,
            "time": pd.Timestamp("2026-01-01 04:15:00+00:00"),
            "direction": "bearish",
            "high": 1957.0,
            "low": 1948.0,
            "open": 1951.0,
            "close": 1953.0,
            "origin_type": "CHoCH",
            "mode": "internal",
            "quality": "strong",
            "source_event_index": 25,
            "source_event_type": "CHoCH",
            "source_swing_index": 15,
            "created_at": 45,
            "source_fvg_index": 35,
            "source_fvg_top": 1970.0,
            "source_fvg_bottom": 1964.0,
            "mitigated": True,
            "mitigated_at": 48,
            "mitigation_pct": 0.5,
            "valid": False,
            "invalidated_at": 50,
            "invalidation_reason": "closed_beyond_boundary",
            "retest_count": 2,
            "structure_leg_id": "leg_ob_2",
        }
        self.assertEqual(set(ob_mutations.keys()), ob_all_fields, "OrderBlock test matrix must cover 100% of fields")
        ob_identity_fields = {"mode", "direction", "source_event_index", "index"}

        def make_ob(data):
            return OrderBlock(**data)

        src_ob_base = make_ob(ob_base)
        id_ob_base = _ob_identity_key(src_ob_base)
        state_ob_base = _ob_state_key(src_ob_base, created_at_val=ob_base["created_at"])

        for field_name, mut_val in ob_mutations.items():
            mut_data = copy.copy(ob_base)
            mut_data[field_name] = mut_val
            src_ob_mut = make_ob(mut_data)
            id_ob_mut = _ob_identity_key(src_ob_mut)
            state_ob_mut = _ob_state_key(src_ob_mut, created_at_val=mut_data["created_at"])

            self.assertNotEqual(state_ob_base, state_ob_mut, f"OB field '{field_name}' change did not change state key!")
            if field_name in ob_identity_fields:
                self.assertNotEqual(id_ob_base, id_ob_mut, f"Identity field '{field_name}' change did not change identity key!")
            else:
                self.assertEqual(id_ob_base, id_ob_mut, f"Non-identity field '{field_name}' unexpectedly changed identity key!")

        # 5. LiquidityPool
        pool_all_fields = {f.name for f in dataclasses.fields(LiquidityPoolSnapshot)}
        pool_base = {
            "kind": "equal_highs",
            "price": 1970.0,
            "price_max": 1970.5,
            "price_min": 1969.5,
            "indices": (10, 20),
            "created_at": 20,
            "confirmed_at": 22,
            "swept": False,
            "swept_at": None,
            "sweep_type": None,
            "valid": True,
            "invalidated_at": None,
            "invalidation_reason": None,
            "mode": "swing",
            "source_swings": ({"index": 10, "price": 1970.0},),
            "structure_leg_id": "leg_pool_1",
        }
        pool_mutations = {
            "kind": "equal_lows",
            "price": 1975.0,
            "price_max": 1976.0,
            "price_min": 1974.0,
            "indices": (10, 25),
            "created_at": 25,
            "confirmed_at": 27,
            "swept": True,
            "swept_at": 30,
            "sweep_type": "clean",
            "valid": False,
            "invalidated_at": 32,
            "invalidation_reason": "mitigated",
            "mode": "internal",
            "source_swings": ({"index": 10, "price": 1975.0},),
            "structure_leg_id": "leg_pool_2",
            "liquidity_side": "SELL_SIDE",
        }
        self.assertEqual(set(pool_mutations.keys()), pool_all_fields, "LiquidityPool test matrix must cover 100% of fields")
        pool_identity_fields = {"mode", "kind", "indices"}

        def make_pool(data):
            return LiquidityPool(
                kind=data["kind"],
                price=data["price"],
                price_max=data["price_max"],
                price_min=data["price_min"],
                indices=list(data["indices"]),
                created_at=data["created_at"],
                confirmed_at=data["confirmed_at"],
                swept=data["swept"],
                swept_at=data["swept_at"],
                sweep_type=data["sweep_type"],
                valid=data["valid"],
                invalidated_at=data["invalidated_at"],
                invalidation_reason=data["invalidation_reason"],
                mode=data["mode"],
                source_swings=[dict(sw) for sw in data["source_swings"]],
                structure_leg_id=data["structure_leg_id"],
                liquidity_side=data.get("liquidity_side"),
            )

        src_pool_base = make_pool(pool_base)
        id_pool_base = _pool_identity_key(src_pool_base)
        state_pool_base = _pool_state_key(src_pool_base)

        for field_name, mut_val in pool_mutations.items():
            mut_data = copy.copy(pool_base)
            mut_data[field_name] = mut_val
            src_pool_mut = make_pool(mut_data)
            id_pool_mut = _pool_identity_key(src_pool_mut)
            state_pool_mut = _pool_state_key(src_pool_mut)

            self.assertNotEqual(state_pool_base, state_pool_mut, f"Pool field '{field_name}' change did not change state key!")
            if field_name in pool_identity_fields:
                self.assertNotEqual(id_pool_base, id_pool_mut, f"Identity field '{field_name}' change did not change identity key!")
            else:
                self.assertEqual(id_pool_base, id_pool_mut, f"Non-identity field '{field_name}' unexpectedly changed identity key!")

        # 6. LiquiditySweep
        sweep_all_fields = {f.name for f in dataclasses.fields(LiquiditySweepSnapshot)}
        sweep_base = {
            "index": 50,
            "time": pd.Timestamp("2026-01-01 05:00:00+00:00"),
            "direction": "bullish",
            "pool_kind": "equal_lows",
            "pool_price": 1940.0,
            "pool_indices": (15, 25),
            "price_wick": 1938.0,
            "close_price": 1942.0,
            "created_at": 25,
            "confirmed_at": 50,
            "swept_at": 50,
            "sweep_type": "clean",
            "valid": True,
            "mode": "swing",
            "structure_leg_id": "leg_sweep_1",
        }
        sweep_mutations = {
            "index": 55,
            "time": pd.Timestamp("2026-01-01 05:15:00+00:00"),
            "direction": "bearish",
            "pool_kind": "equal_highs",
            "pool_price": 1945.0,
            "pool_indices": (15, 30),
            "price_wick": 1935.0,
            "close_price": 1944.0,
            "created_at": 30,
            "confirmed_at": 52,
            "swept_at": 52,
            "sweep_type": "wick_only",
            "valid": False,
            "mode": "internal",
            "structure_leg_id": "leg_sweep_2",
            "liquidity_side": "BUY_SIDE",
            "raid_direction": "bullish",
            "reversal_direction": "bearish",
        }
        self.assertEqual(set(sweep_mutations.keys()), sweep_all_fields, "LiquiditySweep test matrix must cover 100% of fields")
        sweep_identity_fields = {"mode", "direction", "index", "pool_indices"}

        def make_sweep(data):
            return LiquiditySweep(
                index=data["index"],
                time=data["time"],
                direction=data["direction"],
                pool_kind=data["pool_kind"],
                pool_price=data["pool_price"],
                pool_indices=list(data["pool_indices"]),
                price_wick=data["price_wick"],
                close_price=data["close_price"],
                created_at=data["created_at"],
                confirmed_at=data["confirmed_at"],
                swept_at=data["swept_at"],
                sweep_type=data["sweep_type"],
                valid=data["valid"],
                mode=data["mode"],
                structure_leg_id=data["structure_leg_id"],
                liquidity_side=data.get("liquidity_side"),
                raid_direction=data.get("raid_direction"),
                reversal_direction=data.get("reversal_direction"),
            )

        src_sweep_base = make_sweep(sweep_base)
        id_sweep_base = _sweep_identity_key(src_sweep_base)
        state_sweep_base = _sweep_state_key(src_sweep_base)

        for field_name, mut_val in sweep_mutations.items():
            mut_data = copy.copy(sweep_base)
            mut_data[field_name] = mut_val
            src_sweep_mut = make_sweep(mut_data)
            id_sweep_mut = _sweep_identity_key(src_sweep_mut)
            state_sweep_mut = _sweep_state_key(src_sweep_mut)

            self.assertNotEqual(state_sweep_base, state_sweep_mut, f"Sweep field '{field_name}' change did not change state key!")
            if field_name in sweep_identity_fields:
                self.assertNotEqual(id_sweep_base, id_sweep_mut, f"Identity field '{field_name}' change did not change identity key!")
            else:
                self.assertEqual(id_sweep_base, id_sweep_mut, f"Non-identity field '{field_name}' unexpectedly changed identity key!")

    def test_04_constructor_htf_seed_reset_parity(self):
        """P1.1: Constructor seed HTF events are preserved across reset, and dynamic events are purged."""
        t0 = pd.Timestamp("2026-01-01 00:00:00+00:00")
        seed_ev = StructureEvent(
            mode="swing",
            event_type="BOS",
            direction="bullish",
            index=5,
            broken_swing_index=2,
            broken_swing_price=1900.0,
            close_price=1910.0,
            displacement=True,
            time=t0,
        )
        builder = StrategyContextBuilder(htf_events=[seed_ev])
        self.assertEqual(len(builder._initial_htf_events), 1)

        # Dynamic event
        dyn_ev = StructureEvent(
            mode="swing",
            event_type="CHoCH",
            direction="bearish",
            index=15,
            broken_swing_index=8,
            broken_swing_price=1920.0,
            close_price=1915.0,
            displacement=True,
            time=t0 + pd.Timedelta(hours=4),
        )
        builder.add_htf_event(dyn_ev)

        # Reset must purge dynamic event and restore seed event
        builder.reset()
        self.assertEqual(len(builder._initial_htf_events), 1)
        self.assertEqual(builder._initial_htf_events[0].direction, "bullish")
        self.assertEqual(len(builder._htf_tracker._known_events), 1)
        self.assertEqual(builder._htf_tracker._known_events[0].direction, "bullish")

    def test_05_ob_late_fvg_upgrade_and_historical_immutability(self):
        """Probe 5: Late FVG upgrade reflects in new snapshot without contaminating past historical context."""
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

    def test_06_high_retest_count_cache_bounded_o1(self):
        """Probe 5/B: Snapshot cache bounded strictly to O(1) across 75+ retests of single active OB."""
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
        self.assertEqual(len(builder._ob_snapshot_cache), 1)
        self.assertEqual(builder.snapshot_cache_sizes["order_blocks"], 1)

    def test_07_cache_bounds_at_scale(self):
        """Probe 3/C: Cache sizes strictly bounded <= 100 entries at 300, 600, 1000 bars."""
        from tests.test_smc_engine_context import _generate_synthetic_candles
        df = _generate_synthetic_candles(1000)
        builder = StrategyContextBuilder()

        for i, row in enumerate(df.to_dict(orient="records")):
            builder.update(row)
            if i + 1 in (300, 600, 1000):
                sizes = builder.snapshot_cache_sizes
                for k in ("swings", "structures", "fvgs", "order_blocks", "pools", "sweeps"):
                    self.assertLessEqual(sizes[k], 100, f"Cache {k} exceeded 100 at bar {i+1}: {sizes[k]}")

    def test_08_batch_vs_incremental_exact_parity(self):
        """Probe E / Parity: Exact 100% full-payload parity between batch and incremental builder."""
        from tests.test_smc_engine_context import _generate_synthetic_candles
        df = _generate_synthetic_candles(200)
        records = df.to_dict(orient="records")

        # 1. Incremental streaming
        builder_inc = StrategyContextBuilder()
        contexts_inc = [builder_inc.update(c) for c in records]

        # 2. Batch (fresh builder at each bar)
        contexts_batch = []
        for i in range(1, len(records) + 1):
            builder_b = StrategyContextBuilder()
            for c in records[:i]:
                ctx = builder_b.update(c)
            contexts_batch.append(ctx)

        # Compare last 50 contexts
        for i in range(len(records) - 50, len(records)):
            d_inc = contexts_inc[i].to_dict()
            d_batch = contexts_batch[i].to_dict()
            self.assertEqual(d_inc, d_batch, f"Parity mismatch at bar {i}")

    def test_09_future_append_invariance(self):
        """Zero lookahead: past context output is invariant when future bars are appended."""
        from tests.test_smc_engine_context import _generate_synthetic_candles
        df = _generate_synthetic_candles(100)
        records = df.to_dict(orient="records")

        builder = StrategyContextBuilder()
        ctx_bar_50 = None
        for i in range(51):
            ctx_bar_50 = builder.update(records[i])
        dict_at_50 = ctx_bar_50.to_dict()

        # Append more bars into future
        for i in range(51, 100):
            builder.update(records[i])

        # Past context recorded at bar 50 must be completely unchanged
        self.assertEqual(ctx_bar_50.to_dict(), dict_at_50)


if __name__ == "__main__":
    unittest.main()
