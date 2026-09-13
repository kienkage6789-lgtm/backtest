"""
tests/test_smc_engine_registry.py
=================================
Comprehensive behavioral unit test suite for T53.3:
- StrategyTemplate Protocol & Validators
- Stateful Strategy Lifecycle, Monotonicity & Reset
- Deterministic StrategyRegistry & StrategyRegistryConfig
- Output Validation Guards & Exactly-Once Dispatch
- Poisoned-State Recovery Contract
- Scale & System Regression
"""

from __future__ import annotations

import copy
from types import MappingProxyType
import unittest
from typing import Any, Callable, Optional, Sequence

import pandas as pd

from smc.engine.errors import (
    DuplicateStrategyError,
    InvalidStrategyOutputError,
    StrategyRegistryError,
    StrategyStateError,
    StrategyValidationError,
    UnknownStrategyError,
)
from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    StrategyContext,
    StrategyProfile,
    StrictModelTypeError,
)
from smc.engine.protocol import (
    StrategyTemplate,
    validate_strategy_id,
    validate_strategy_template,
)
from smc.engine.registry import (
    StrategyRegistry,
    StrategyRegistryConfig,
)


class DummyValidStrategy:
    """A standard compliant strategy template with reset support."""

    def __init__(
        self,
        strategy_id: str,
        directions: tuple[str, ...] = ("BUY", "SELL"),
        eval_fn: Optional[Callable[[StrategyContext], tuple[CandidateSetup, ...]]] = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.profile = StrategyProfile(
            strategy_id=strategy_id,
            name=f"Name {strategy_id}",
            allowed_directions=directions,
        )
        self.eval_fn = eval_fn or (lambda ctx: ())
        self.reset_count = 0
        self.evaluated_bars: list[int] = []

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        self.evaluated_bars.append(context.bar_index)
        return self.eval_fn(context)

    def reset(self) -> None:
        self.reset_count += 1
        self.evaluated_bars.clear()


class DummyStatefulStrategy:
    """A stateful strategy that tracks bar count across closed bars."""

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id
        self.profile = StrategyProfile(strategy_id=strategy_id, name=f"Stateful {strategy_id}")
        self.bar_counter = 0
        self.last_bar: Optional[int] = None
        self.reset_called = 0

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        self.bar_counter += 1
        self.last_bar = context.bar_index
        return ()

    def reset(self) -> None:
        self.reset_called += 1
        self.bar_counter = 0
        self.last_bar = None


class DummyExplodingStrategy:
    """A strategy that raises an exception in evaluate() or reset()."""

    def __init__(
        self,
        strategy_id: str,
        explode_in_eval: bool = False,
        explode_in_reset: bool = False,
    ) -> None:
        self.strategy_id = strategy_id
        self.profile = StrategyProfile(strategy_id=strategy_id, name="Exploder")
        self.explode_in_eval = explode_in_eval
        self.explode_in_reset = explode_in_reset

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        if self.explode_in_eval:
            raise RuntimeError("Explosion in evaluate()!")
        return ()

    def reset(self) -> None:
        if self.explode_in_reset:
            raise RuntimeError("Explosion in reset()!")


class DummyMutatingStrategy:
    """A strategy that maliciously attempts to mutate StrategyContext."""

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id
        self.profile = StrategyProfile(strategy_id=strategy_id, name="Mutator")

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        # Attempt to mutate context attributes
        context.close = 9999.99  # Should raise FrozenInstanceError
        return ()

    def reset(self) -> None:
        pass


def _make_context(bar_index: int = 10, ts_offset_min: int = 0) -> StrategyContext:
    base_ts = pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=ts_offset_min)
    return StrategyContext(
        bar_index=bar_index,
        timestamp=base_ts,
        bar_close_time=base_ts + pd.Timedelta(minutes=1),
        symbol="XAUUSD",
        timeframe="M1",
        open=2040.0,
        high=2045.0,
        low=2038.0,
        close=2042.0,
        volume=100.0,
        atr14=2.0,
    )


def _make_candidate(
    strategy_id: str,
    context: StrategyContext,
    direction: str = "BUY",
) -> CandidateSetup:
    ev = EvidenceRef(
        evidence_id="sweep:internal:10:pool1",
        kind="liquidity_sweep",
        bar_index=context.bar_index,
        price=2040.0,
        time=context.timestamp,
    )
    return CandidateSetup(
        setup_id=f"setup:{strategy_id}:{context.bar_index}:{direction}",
        strategy_id=strategy_id,
        direction=direction,
        bar_index=context.bar_index,
        timestamp=context.timestamp,
        entry_price=2040.0 if direction == "BUY" else 2040.0,
        stop_loss=2035.0 if direction == "BUY" else 2045.0,
        take_profit=2050.0 if direction == "BUY" else 2030.0,
        planned_rr=2.0,
        evidences=(ev,),
        evidence_cluster_id="cluster:test",
        expiry_bar=context.bar_index + 10,
    )


class TestSMCEngineRegistry(unittest.TestCase):

    # =========================================================================
    # Nhóm 1: Protocol & Template Validation
    # =========================================================================

    def test_01_valid_strategy_template_accepted(self):
        s = DummyValidStrategy("S01")
        self.assertTrue(isinstance(s, StrategyTemplate))
        validate_strategy_template(s)

    def test_02_missing_strategy_id_rejected(self):
        class MissingId:
            profile = StrategyProfile(strategy_id="S01", name="S1")
            def evaluate(self, ctx): return ()
            def reset(self): pass

        with self.assertRaises(StrategyValidationError) as cm:
            validate_strategy_template(MissingId())
        self.assertIn("missing 'strategy_id'", str(cm.exception))

    def test_03_missing_or_invalid_profile_rejected(self):
        class MissingProfile:
            strategy_id = "S01"
            def evaluate(self, ctx): return ()
            def reset(self): pass

        with self.assertRaises(StrategyValidationError):
            validate_strategy_template(MissingProfile())

        class InvalidProfileType:
            strategy_id = "S01"
            profile = {"strategy_id": "S01"}
            def evaluate(self, ctx): return ()
            def reset(self): pass

        with self.assertRaises(StrategyValidationError) as cm:
            validate_strategy_template(InvalidProfileType())
        self.assertIn("must be a StrategyProfile", str(cm.exception))

    def test_04_missing_or_non_callable_evaluate_rejected(self):
        class MissingEvaluate:
            strategy_id = "S01"
            profile = StrategyProfile(strategy_id="S01", name="S1")
            def reset(self): pass

        with self.assertRaises(StrategyValidationError):
            validate_strategy_template(MissingEvaluate())

        class NonCallableEvaluate:
            strategy_id = "S01"
            profile = StrategyProfile(strategy_id="S01", name="S1")
            evaluate = "not a function"
            def reset(self): pass

        with self.assertRaises(StrategyValidationError) as cm:
            validate_strategy_template(NonCallableEvaluate())
        self.assertIn("evaluate' must be callable", str(cm.exception))

    def test_05_missing_or_non_callable_reset_rejected(self):
        class MissingReset:
            strategy_id = "S01"
            profile = StrategyProfile(strategy_id="S01", name="S1")
            def evaluate(self, ctx): return ()

        with self.assertRaises(StrategyValidationError):
            validate_strategy_template(MissingReset())

        class NonCallableReset:
            strategy_id = "S01"
            profile = StrategyProfile(strategy_id="S01", name="S1")
            def evaluate(self, ctx): return ()
            reset = 123

        with self.assertRaises(StrategyValidationError) as cm:
            validate_strategy_template(NonCallableReset())
        self.assertIn("reset' must be callable", str(cm.exception))

    def test_06_strategy_id_profile_id_mismatch_rejected(self):
        class MismatchedId:
            strategy_id = "S01"
            profile = StrategyProfile(strategy_id="S05", name="Mismatch")
            def evaluate(self, ctx): return ()
            def reset(self): pass

        with self.assertRaises(StrategyValidationError) as cm:
            validate_strategy_template(MismatchedId())
        self.assertIn("does not match profile.strategy_id", str(cm.exception))

    def test_07_validate_strategy_id_rejections(self):
        # Empty
        with self.assertRaises(StrategyValidationError):
            validate_strategy_id("")
        # Whitespace
        with self.assertRaises(StrategyValidationError):
            validate_strategy_id(" S01")
        with self.assertRaises(StrategyValidationError):
            validate_strategy_id("S01 ")
        # Colons
        with self.assertRaises(StrategyValidationError):
            validate_strategy_id("S:01")
        # Non-string
        with self.assertRaises(StrategyValidationError):
            validate_strategy_id(123)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            validate_strategy_id(None)  # type: ignore
        # Invalid characters
        with self.assertRaises(StrategyValidationError):
            validate_strategy_id("S01@test")

        # Valid cases return the string
        self.assertEqual(validate_strategy_id("S01"), "S01")
        self.assertEqual(validate_strategy_id("S05_BOS-OB.v1"), "S05_BOS-OB.v1")

    def test_08_validate_strategy_id_parity_with_profile(self):
        # Ensure validate_strategy_id accepts exactly what StrategyProfile accepts
        valid_ids = ["S01", "S05", "S09", "ict_2022", "bos-ob.v2"]
        for vid in valid_ids:
            self.assertEqual(validate_strategy_id(vid), vid)
            prof = StrategyProfile(strategy_id=vid, name="Test")
            self.assertEqual(prof.strategy_id, vid)

        invalid_ids = ["", "S:01", " S01", "S01 ", "S*01"]
        for iid in invalid_ids:
            with self.assertRaises(StrategyValidationError):
                validate_strategy_id(iid)
            with self.assertRaises(ValueError):
                StrategyProfile(strategy_id=iid, name="Test")

    # =========================================================================
    # Nhóm 2: Stateful Strategy Lifecycle & Reset
    # =========================================================================

    def test_09_stateful_strategy_advances_across_contexts(self):
        strat = DummyStatefulStrategy("S01")
        reg = StrategyRegistry([strat])

        ctx1 = _make_context(bar_index=1, ts_offset_min=1)
        res1 = reg.evaluate_enabled(ctx1)
        self.assertIn("S01", res1)
        self.assertEqual(strat.bar_counter, 1)
        self.assertEqual(strat.last_bar, 1)

        ctx2 = _make_context(bar_index=2, ts_offset_min=2)
        res2 = reg.evaluate_enabled(ctx2)
        self.assertIn("S01", res2)
        self.assertEqual(strat.bar_counter, 2)
        self.assertEqual(strat.last_bar, 2)

    def test_10_duplicate_context_retry_idempotent_no_advance(self):
        strat = DummyStatefulStrategy("S01")
        reg = StrategyRegistry([strat])

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        res1 = reg.evaluate_enabled(ctx)
        self.assertEqual(strat.bar_counter, 1)

        # Retry identical context
        res2 = reg.evaluate_enabled(ctx)
        self.assertIs(res1, res2)  # Returns cached MappingProxyType object
        self.assertEqual(strat.bar_counter, 1)  # Did NOT advance!

    def test_11_conflicting_duplicate_context_rejected(self):
        strat = DummyStatefulStrategy("S01")
        reg = StrategyRegistry([strat])

        ctx1 = _make_context(bar_index=1, ts_offset_min=1)
        reg.evaluate_enabled(ctx1)
        self.assertEqual(strat.bar_counter, 1)

        # Same bar_index, but different timestamp
        ctx_conflict = _make_context(bar_index=1, ts_offset_min=2)
        with self.assertRaises(StrategyStateError):
            reg.evaluate_enabled(ctx_conflict)
        self.assertEqual(strat.bar_counter, 1)  # Strategy was NOT called!

    def test_12_out_of_order_context_rejected(self):
        strat = DummyStatefulStrategy("S01")
        reg = StrategyRegistry([strat])

        ctx2 = _make_context(bar_index=5, ts_offset_min=5)
        reg.evaluate_enabled(ctx2)
        self.assertEqual(strat.bar_counter, 1)

        # Decreased bar index
        ctx_earlier = _make_context(bar_index=4, ts_offset_min=6)
        with self.assertRaises(StrategyStateError):
            reg.evaluate_enabled(ctx_earlier)
        self.assertEqual(strat.bar_counter, 1)

    def test_12b_advancing_bar_with_non_increasing_timestamp_rejected(self):
        strat = DummyStatefulStrategy("S01")
        reg = StrategyRegistry([strat])

        # Bar 1 at offset 5m
        ctx1 = _make_context(bar_index=1, ts_offset_min=5)
        reg.evaluate_enabled(ctx1)
        self.assertEqual(strat.bar_counter, 1)

        # Bar 2 with identical timestamp (offset 5m) -> MUST reject
        ctx2_same_ts = _make_context(bar_index=2, ts_offset_min=5)
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx2_same_ts)
        self.assertIn("must strictly increase on bar advance", str(cm.exception))
        self.assertEqual(strat.bar_counter, 1)  # Strategy was NOT called

        # Bar 3 with earlier timestamp (offset 4m) -> MUST reject
        ctx3_earlier_ts = _make_context(bar_index=3, ts_offset_min=4)
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx3_earlier_ts)
        self.assertIn("must strictly increase on bar advance", str(cm.exception))
        self.assertEqual(strat.bar_counter, 1)  # Strategy was NOT called

    def test_13_reset_all_calls_reset_on_all_strategies(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S02")
        cfg = StrategyRegistryConfig(enabled_strategy_ids=("S01",))  # S02 is disabled
        reg = StrategyRegistry([s1, s2], cfg)

        self.assertEqual(s1.reset_count, 0)
        self.assertEqual(s2.reset_count, 0)

        reg.reset_all()

        self.assertEqual(s1.reset_count, 1)
        self.assertEqual(s2.reset_count, 1)  # Disabled strategy is also reset!

    def test_14_reset_all_clears_cache_and_enables_replay(self):
        strat = DummyStatefulStrategy("S01")
        reg = StrategyRegistry([strat])

        ctx1 = _make_context(bar_index=1, ts_offset_min=1)
        ctx2 = _make_context(bar_index=2, ts_offset_min=2)

        reg.evaluate_enabled(ctx1)
        reg.evaluate_enabled(ctx2)
        self.assertEqual(strat.bar_counter, 2)

        reg.reset_all()
        self.assertEqual(strat.bar_counter, 0)
        self.assertEqual(strat.reset_called, 1)

        # Replay from bar 1 is now allowed (cache was cleared)
        reg.evaluate_enabled(ctx1)
        self.assertEqual(strat.bar_counter, 1)

    def test_15_reset_failure_propagates_and_marks_poisoned(self):
        s_ok = DummyValidStrategy("S01")
        s_bad = DummyExplodingStrategy("S02", explode_in_reset=True)
        reg = StrategyRegistry([s_ok, s_bad])

        with self.assertRaises(RuntimeError) as cm:
            reg.reset_all()
        self.assertIn("Explosion in reset()!", str(cm.exception))

        # Registry is poisoned; evaluate_enabled must raise StrategyStateError
        ctx = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError):
            reg.evaluate_enabled(ctx)

    def test_16_no_cross_run_contamination(self):
        # Two distinct registry runs have distinct strategy instances
        s1 = DummyStatefulStrategy("S01")
        reg1 = StrategyRegistry([s1])
        ctx = _make_context(bar_index=1, ts_offset_min=1)
        reg1.evaluate_enabled(ctx)
        self.assertEqual(s1.bar_counter, 1)

        s2 = DummyStatefulStrategy("S01")
        reg2 = StrategyRegistry([s2])
        self.assertEqual(s2.bar_counter, 0)

    # =========================================================================
    # Nhóm 3: Registry Structure, Immutability & Config
    # =========================================================================

    def test_17_duplicate_id_in_constructor_rejected(self):
        s1 = DummyValidStrategy("S01")
        with self.assertRaises(DuplicateStrategyError):
            StrategyRegistry([s1, s1])

    def test_18_two_different_instances_same_id_rejected(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S01")
        with self.assertRaises(DuplicateStrategyError):
            StrategyRegistry([s1, s2])

    def test_19_registration_order_invariance(self):
        s1 = DummyValidStrategy("S01")
        s5 = DummyValidStrategy("S05")
        s9 = DummyValidStrategy("S09")

        reg_a = StrategyRegistry([s9, s1, s5])
        reg_b = StrategyRegistry([s5, s9, s1])

        self.assertEqual(reg_a.registered_strategy_ids, ("S01", "S05", "S09"))
        self.assertEqual(reg_b.registered_strategy_ids, ("S01", "S05", "S09"))

    def test_20_o1_lookup_and_immutability(self):
        s = DummyValidStrategy("S01")
        reg = StrategyRegistry([s])

        # Public properties return tuples
        self.assertIsInstance(reg.registered_strategy_ids, tuple)
        self.assertIsInstance(reg.enabled_strategy_ids, tuple)
        self.assertIsInstance(reg.disabled_strategy_ids, tuple)

        # Internal dictionary is wrapped in MappingProxyType
        self.assertIsInstance(reg._strategies, MappingProxyType)
        with self.assertRaises(TypeError):
            reg._strategies["S99"] = s  # type: ignore

    def test_21_config_none_enables_all(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S02")
        reg = StrategyRegistry([s1, s2], None)
        self.assertEqual(reg.enabled_strategy_ids, ("S01", "S02"))
        self.assertEqual(reg.disabled_strategy_ids, ())

    def test_22_config_empty_tuple_disables_all(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S02")
        cfg = StrategyRegistryConfig(enabled_strategy_ids=())
        reg = StrategyRegistry([s1, s2], cfg)
        self.assertEqual(reg.enabled_strategy_ids, ())
        self.assertEqual(reg.disabled_strategy_ids, ("S01", "S02"))

    def test_23_config_explicit_subset(self):
        s1 = DummyValidStrategy("S01")
        s5 = DummyValidStrategy("S05")
        s9 = DummyValidStrategy("S09")
        cfg = StrategyRegistryConfig(enabled_strategy_ids=("S09", "S01"))
        reg = StrategyRegistry([s1, s5, s9], cfg)

        self.assertEqual(reg.enabled_strategy_ids, ("S01", "S09"))
        self.assertEqual(reg.disabled_strategy_ids, ("S05",))
        self.assertTrue(reg.is_enabled("S01"))
        self.assertFalse(reg.is_enabled("S05"))
        self.assertTrue(reg.is_enabled("S09"))

    def test_24_config_order_invariance(self):
        cfg1 = StrategyRegistryConfig(enabled_strategy_ids=("S09", "S01"))
        cfg2 = StrategyRegistryConfig(enabled_strategy_ids=("S01", "S09"))
        self.assertEqual(cfg1, cfg2)
        self.assertEqual(cfg1.enabled_strategy_ids, ("S01", "S09"))

    def test_25_config_duplicate_id_rejected(self):
        with self.assertRaises(DuplicateStrategyError):
            StrategyRegistryConfig(enabled_strategy_ids=("S01", "S01"))

    def test_26_config_unknown_id_rejected(self):
        s1 = DummyValidStrategy("S01")
        cfg = StrategyRegistryConfig(enabled_strategy_ids=("S99",))
        with self.assertRaises(UnknownStrategyError):
            StrategyRegistry([s1], cfg)

    def test_27_config_malformed_rejected(self):
        with self.assertRaises(StrategyValidationError):
            StrategyRegistryConfig(enabled_strategy_ids="S01")  # type: ignore
        with self.assertRaises(StrategyValidationError):
            StrategyRegistryConfig.from_dict({"unexpected_field": 123})
        with self.assertRaises(StrategyValidationError):
            StrategyRegistryConfig.from_dict("not a dict")  # type: ignore

    def test_28_config_json_round_trip(self):
        configs = [
            StrategyRegistryConfig(enabled_strategy_ids=None),
            StrategyRegistryConfig(enabled_strategy_ids=()),
            StrategyRegistryConfig(enabled_strategy_ids=("S01", "S05")),
        ]
        for cfg in configs:
            d = cfg.to_dict()
            restored = StrategyRegistryConfig.from_dict(d)
            self.assertEqual(cfg, restored)

    # =========================================================================
    # Nhóm 4: Lookup Behavior & Exceptions
    # =========================================================================

    def test_29_get_profile_success(self):
        s = DummyValidStrategy("S01")
        reg = StrategyRegistry([s])
        prof = reg.get_profile("S01")
        self.assertIsInstance(prof, StrategyProfile)
        self.assertEqual(prof.strategy_id, "S01")

    def test_30_get_profile_unknown_raises_error(self):
        reg = StrategyRegistry([DummyValidStrategy("S01")])
        with self.assertRaises(UnknownStrategyError):
            reg.get_profile("S99")

    def test_31_get_profile_disabled_strategy(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S02")
        cfg = StrategyRegistryConfig(enabled_strategy_ids=("S01",))
        reg = StrategyRegistry([s1, s2], cfg)

        self.assertFalse(reg.is_enabled("S02"))
        prof2 = reg.get_profile("S02")
        self.assertEqual(prof2.strategy_id, "S02")

    def test_32_is_enabled_unknown_raises_error(self):
        reg = StrategyRegistry([DummyValidStrategy("S01")])
        with self.assertRaises(UnknownStrategyError):
            reg.is_enabled("S99")

    def test_33_no_public_strategy_instance_leak(self):
        reg = StrategyRegistry([DummyValidStrategy("S01")])
        self.assertFalse(hasattr(reg, "get_strategy"))
        self.assertFalse(hasattr(reg, "get_all_strategies"))
        self.assertFalse(hasattr(reg, "get_enabled_strategies"))

    def test_34_public_sequences_and_mappings_immutable(self):
        s = DummyValidStrategy("S01")
        reg = StrategyRegistry([s])
        self.assertIsInstance(reg.registered_strategy_ids, tuple)
        self.assertIsInstance(reg.enabled_strategy_ids, tuple)
        self.assertIsInstance(reg.disabled_strategy_ids, tuple)

        ctx = _make_context()
        res = reg.evaluate_enabled(ctx)
        self.assertIsInstance(res, MappingProxyType)
        with self.assertRaises(TypeError):
            res["S99"] = ()  # type: ignore

    def test_35_domain_exception_hierarchy(self):
        self.assertTrue(issubclass(StrategyValidationError, StrategyRegistryError))
        self.assertTrue(issubclass(DuplicateStrategyError, StrategyRegistryError))
        self.assertTrue(issubclass(UnknownStrategyError, StrategyRegistryError))
        self.assertTrue(issubclass(StrategyStateError, StrategyRegistryError))
        self.assertTrue(issubclass(InvalidStrategyOutputError, StrategyRegistryError))

        # Single inheritance: not inheriting from TypeError/ValueError/KeyError
        self.assertFalse(issubclass(StrategyValidationError, (TypeError, ValueError)))
        self.assertFalse(issubclass(UnknownStrategyError, KeyError))

    # =========================================================================
    # Nhóm 5: Aggregate Evaluation, Exactly-Once & Output Guards
    # =========================================================================

    def test_36_evaluate_empty_tuple_valid(self):
        s = DummyValidStrategy("S01", eval_fn=lambda ctx: ())
        reg = StrategyRegistry([s])
        res = reg.evaluate_enabled(_make_context())
        self.assertEqual(res["S01"], ())

    def test_37_evaluate_valid_candidates_preserved(self):
        ctx = _make_context(bar_index=15)
        cand = _make_candidate("S01", ctx, direction="BUY")
        s = DummyValidStrategy("S01", eval_fn=lambda c: (cand,))
        reg = StrategyRegistry([s])

        res = reg.evaluate_enabled(ctx)
        self.assertEqual(res["S01"], (cand,))

    def test_38_evaluate_list_or_generator_or_none_rejected(self):
        ctx = _make_context(bar_index=15)
        cand = _make_candidate("S01", ctx)

        # List
        s_list = DummyValidStrategy("S01", eval_fn=lambda c: [cand])  # type: ignore
        reg1 = StrategyRegistry([s_list])
        with self.assertRaises(InvalidStrategyOutputError):
            reg1.evaluate_enabled(ctx)

        # None
        s_none = DummyValidStrategy("S01", eval_fn=lambda c: None)  # type: ignore
        reg2 = StrategyRegistry([s_none])
        with self.assertRaises(InvalidStrategyOutputError):
            reg2.evaluate_enabled(ctx)

        # Generator
        s_gen = DummyValidStrategy("S01", eval_fn=lambda c: (x for x in (cand,)))  # type: ignore
        reg3 = StrategyRegistry([s_gen])
        with self.assertRaises(InvalidStrategyOutputError):
            reg3.evaluate_enabled(ctx)

    def test_39_evaluate_non_candidate_item_rejected(self):
        ctx = _make_context()
        s = DummyValidStrategy("S01", eval_fn=lambda c: ("not a candidate",))  # type: ignore
        reg = StrategyRegistry([s])
        with self.assertRaises(InvalidStrategyOutputError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("not a CandidateSetup", str(cm.exception))

    def test_40_candidate_strategy_id_mismatch_rejected(self):
        ctx = _make_context()
        cand = _make_candidate("S05", ctx)  # Mismatch with S01
        s = DummyValidStrategy("S01", eval_fn=lambda c: (cand,))
        reg = StrategyRegistry([s])
        with self.assertRaises(InvalidStrategyOutputError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("does not match running strategy", str(cm.exception))

    def test_41_candidate_direction_not_in_profile_rejected(self):
        ctx = _make_context()
        cand = _make_candidate("S01", ctx, direction="SELL")
        # Profile only allows BUY
        s = DummyValidStrategy("S01", directions=("BUY",), eval_fn=lambda c: (cand,))
        reg = StrategyRegistry([s])
        with self.assertRaises(InvalidStrategyOutputError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("not permitted by profile allowed_directions", str(cm.exception))

    def test_42_candidate_bar_index_or_timestamp_mismatch_rejected(self):
        ctx = _make_context(bar_index=15)
        ev = EvidenceRef(
            evidence_id="sweep:internal:10:pool1",
            kind="liquidity_sweep",
            bar_index=15,
            price=2040.0,
            time=ctx.timestamp,
        )
        # Bar index mismatch
        cand_wrong_bar = CandidateSetup(
            setup_id="setup:S01:14:BUY",
            strategy_id="S01",
            direction="BUY",
            bar_index=14,  # Context is 15
            timestamp=ctx.timestamp,
            entry_price=2040.0,
            stop_loss=2035.0,
            take_profit=2050.0,
            planned_rr=2.0,
            evidences=(ev,),
            evidence_cluster_id="cluster:test",
            expiry_bar=25,
        )
        s1 = DummyValidStrategy("S01", eval_fn=lambda c: (cand_wrong_bar,))
        reg1 = StrategyRegistry([s1])
        with self.assertRaises(InvalidStrategyOutputError) as cm:
            reg1.evaluate_enabled(ctx)
        self.assertIn("does not match context.bar_index", str(cm.exception))

        # Timestamp mismatch
        cand_wrong_ts = CandidateSetup(
            setup_id="setup:S01:15:BUY",
            strategy_id="S01",
            direction="BUY",
            bar_index=15,
            timestamp=pd.Timestamp("2026-01-01 00:00:00+00:00"),  # Different ts
            entry_price=2040.0,
            stop_loss=2035.0,
            take_profit=2050.0,
            planned_rr=2.0,
            evidences=(ev,),
            evidence_cluster_id="cluster:test",
            expiry_bar=25,
        )
        s2 = DummyValidStrategy("S01", eval_fn=lambda c: (cand_wrong_ts,))
        reg2 = StrategyRegistry([s2])
        with self.assertRaises(InvalidStrategyOutputError) as cm:
            reg2.evaluate_enabled(ctx)
        self.assertIn("does not match context.timestamp", str(cm.exception))

    def test_43_disabled_strategy_not_evaluated(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S02")
        cfg = StrategyRegistryConfig(enabled_strategy_ids=("S01",))
        reg = StrategyRegistry([s1, s2], cfg)

        ctx = _make_context()
        res = reg.evaluate_enabled(ctx)
        self.assertIn("S01", res)
        self.assertNotIn("S02", res)
        self.assertEqual(len(s1.evaluated_bars), 1)
        self.assertEqual(len(s2.evaluated_bars), 0)

    def test_44_strategy_exception_propagates_and_poisons_registry(self):
        s = DummyExplodingStrategy("S01", explode_in_eval=True)
        reg = StrategyRegistry([s])
        ctx1 = _make_context(bar_index=1, ts_offset_min=1)

        with self.assertRaises(RuntimeError) as cm:
            reg.evaluate_enabled(ctx1)
        self.assertIn("Explosion in evaluate()!", str(cm.exception))

        # Registry is poisoned
        ctx2 = _make_context(bar_index=2, ts_offset_min=2)
        with self.assertRaises(StrategyStateError) as cm_state:
            reg.evaluate_enabled(ctx2)
        self.assertIn("poisoned", str(cm_state.exception))

    def test_45_poisoned_registry_recovery_via_reset_all(self):
        s = DummyExplodingStrategy("S01", explode_in_eval=True)
        reg = StrategyRegistry([s])
        ctx1 = _make_context(bar_index=1, ts_offset_min=1)

        with self.assertRaises(RuntimeError):
            reg.evaluate_enabled(ctx1)

        # Fix exploding condition on strategy and call reset_all
        s.explode_in_eval = False
        reg.reset_all()

        # Now evaluation should work
        res = reg.evaluate_enabled(ctx1)
        self.assertIn("S01", res)

    # =========================================================================
    # Nhóm 6: Scale, Exports & System Regression
    # =========================================================================

    def test_46_scale_100_strategies_deterministic(self):
        import random
        ids = [f"STRAT_{i:03d}" for i in range(100)]
        shuffled_ids = list(ids)
        random.Random(42).shuffle(shuffled_ids)

        strategies = [DummyValidStrategy(sid) for sid in shuffled_ids]
        reg = StrategyRegistry(strategies)

        expected_order = tuple(sorted(ids))
        self.assertEqual(reg.registered_strategy_ids, expected_order)
        self.assertEqual(reg.enabled_strategy_ids, expected_order)

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        res = reg.evaluate_enabled(ctx)
        self.assertEqual(tuple(res.keys()), expected_order)

    def test_47_context_immutability_during_evaluation(self):
        s = DummyMutatingStrategy("S01")
        reg = StrategyRegistry([s])
        ctx = _make_context(bar_index=1)

        with self.assertRaises(Exception):
            # FrozenInstanceError (subclass of AttributeError)
            reg.evaluate_enabled(ctx)

    def test_48_clean_package_exports_no_circular_import(self):
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile

        repository_root = Path(__file__).resolve().parents[1]

        # 1. Verify imports in clean interpreter with cwd=repository_root
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import smc.engine.errors; "
                    "import smc.engine.protocol; "
                    "import smc.engine.registry; "
                    "import smc.engine"
                ),
            ],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Subprocess import with cwd=repository_root failed with returncode {proc.returncode}:\n"
            f"Stdout: {proc.stdout}\nStderr: {proc.stderr}",
        )

        # 2. Verify imports from an arbitrary external working directory (tempdir) using PYTHONPATH
        env = dict(os.environ)
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(repository_root) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        )
        proc_external = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import smc.engine.errors; "
                    "import smc.engine.protocol; "
                    "import smc.engine.registry; "
                    "import smc.engine"
                ),
            ],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc_external.returncode,
            0,
            f"Subprocess import from external cwd failed with returncode {proc_external.returncode}:\n"
            f"Stdout: {proc_external.stdout}\nStderr: {proc_external.stderr}",
        )

        import smc.engine
        import smc.engine.errors
        import smc.engine.protocol
        import smc.engine.registry

        expected_symbols = [
            "StrategyTemplate",
            "validate_strategy_id",
            "validate_strategy_template",
            "StrategyRegistryConfig",
            "StrategyRegistry",
            "StrategyRegistryError",
            "StrategyValidationError",
            "DuplicateStrategyError",
            "UnknownStrategyError",
            "StrategyStateError",
            "InvalidStrategyOutputError",
        ]
        for sym in expected_symbols:
            self.assertTrue(hasattr(smc.engine, sym), f"smc.engine missing symbol '{sym}'")
            self.assertIn(sym, smc.engine.__all__)

    def test_49_preflight_identity_drift_poisons_registry_and_aborts_prior_eval(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S02")
        reg = StrategyRegistry([s1, s2])

        # Mutate s2's strategy_id before evaluation
        s2.strategy_id = "S99_MUTATED"

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("identity or profile was mutated prior to evaluation", str(cm.exception))

        # Crucial check: s1 was NOT evaluated because preflight checked all enabled strategies upfront
        self.assertEqual(len(s1.evaluated_bars), 0)

        # Registry is poisoned
        self.assertTrue(reg._poisoned)
        with self.assertRaises(StrategyStateError) as cm2:
            reg.evaluate_enabled(ctx)
        self.assertIn("poisoned", str(cm2.exception))

    def test_50_preflight_profile_drift_poisons_registry(self):
        s1 = DummyValidStrategy("S01")
        reg = StrategyRegistry([s1])

        # Mutate s1's profile
        s1.profile = StrategyProfile(strategy_id="S01", name="Hacked", allowed_directions=("BUY",))

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("identity or profile was mutated prior to evaluation", str(cm.exception))
        self.assertTrue(reg._poisoned)

    def test_51_post_eval_identity_drift_poisons_registry(self):
        class MaliciousIdentityDriftStrategy(DummyValidStrategy):
            def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
                self.strategy_id = "S01_TAMPERED"
                return ()

        s1 = MaliciousIdentityDriftStrategy("S01")
        reg = StrategyRegistry([s1])

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("mutated its identity or profile during evaluation", str(cm.exception))
        self.assertTrue(reg._poisoned)

    def test_51b_post_eval_identity_drift_prioritized_over_invalid_output(self):
        class MaliciousIdentityDriftAndInvalidOutputStrategy(DummyValidStrategy):
            def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
                self.strategy_id = "S01_TAMPERED"
                return ["not a tuple"]  # type: ignore

        s1 = MaliciousIdentityDriftAndInvalidOutputStrategy("S01")
        reg = StrategyRegistry([s1])

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        # Must raise StrategyStateError, NOT InvalidStrategyOutputError
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("mutated its identity or profile during evaluation", str(cm.exception))
        self.assertTrue(reg._poisoned)
        self.assertIsNone(reg._last_result)
        self.assertIsNone(reg._last_bar_index)
        self.assertIsNone(reg._last_context_payload)

    def test_52_post_eval_profile_drift_poisons_registry(self):
        class MaliciousProfileDriftStrategy(DummyValidStrategy):
            def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
                self.profile = StrategyProfile(strategy_id="S01", name="TamperedProfile")
                return ()

        s1 = MaliciousProfileDriftStrategy("S01")
        reg = StrategyRegistry([s1])

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("mutated its identity or profile during evaluation", str(cm.exception))
        self.assertTrue(reg._poisoned)

    def test_52b_post_eval_profile_drift_prioritized_over_invalid_output(self):
        class MaliciousProfileDriftAndInvalidOutputStrategy(DummyValidStrategy):
            def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
                self.profile = StrategyProfile(strategy_id="S01", name="TamperedProfile")
                return None  # type: ignore

        s1 = MaliciousProfileDriftAndInvalidOutputStrategy("S01")
        reg = StrategyRegistry([s1])

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        # Must raise StrategyStateError, NOT InvalidStrategyOutputError
        with self.assertRaises(StrategyStateError) as cm:
            reg.evaluate_enabled(ctx)
        self.assertIn("mutated its identity or profile during evaluation", str(cm.exception))
        self.assertTrue(reg._poisoned)
        self.assertIsNone(reg._last_result)
        self.assertIsNone(reg._last_bar_index)
        self.assertIsNone(reg._last_context_payload)

    def test_53_output_mapping_always_keys_by_canonical_id(self):
        s1 = DummyValidStrategy("S01")
        s5 = DummyValidStrategy("S05")
        reg = StrategyRegistry([s5, s1])

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        res = reg.evaluate_enabled(ctx)
        self.assertEqual(list(res.keys()), ["S01", "S05"])

    def test_54_preflight_collision_with_canonical_id_prevents_evaluation(self):
        s1 = DummyValidStrategy("S01")
        s2 = DummyValidStrategy("S02")
        registry = StrategyRegistry([s1, s2])

        # Maliciously change s2's strategy_id to "S01" causing collision with canonical s1 ID
        s2.strategy_id = "S01"

        ctx = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError) as cm:
            registry.evaluate_enabled(ctx)
        self.assertIn("identity or profile was mutated prior to evaluation", str(cm.exception))

        # Crucial checks:
        # Preflight runs across all enabled strategies before the first strategy is evaluated
        self.assertEqual(len(s1.evaluated_bars), 0)
        self.assertEqual(len(s2.evaluated_bars), 0)
        # Registry is poisoned
        self.assertTrue(registry._poisoned)
        # No output mapping or cache created (cannot overwrite or collide key "S01")
        self.assertIsNone(registry._last_result)
        self.assertIsNone(registry._last_bar_index)
        self.assertIsNone(registry._last_context_payload)

    def test_55_recovery_after_identity_drift(self):
        # 1. Tạo strategy S01 và registry
        strat = DummyStatefulStrategy("S01")
        registry = StrategyRegistry([strat])

        # 2. Đổi strategy_id thành S99
        strat.strategy_id = "S99"

        # 3. Gọi evaluate_enabled() và xác nhận StrategyStateError
        ctx1 = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError):
            registry.evaluate_enabled(ctx1)

        # 4. Registry phải poisoned, không advance bar
        self.assertTrue(registry._poisoned)
        self.assertEqual(strat.bar_counter, 0)
        self.assertIsNone(registry._last_result)

        # 5. Khôi phục strategy_id thành S01
        strat.strategy_id = "S01"

        # 6. Gọi reset_all()
        registry.reset_all()

        # 7. Xác nhận reset thành công, poisoned flag được xóa, dispatch cache được xóa
        self.assertFalse(registry._poisoned)
        self.assertIsNone(registry._last_result)
        self.assertIsNone(registry._last_bar_index)
        self.assertEqual(strat.reset_called, 1)

        # 8. Gọi lại evaluate_enabled() với context hợp lệ
        res1 = registry.evaluate_enabled(ctx1)

        # 9. Kết quả phải có canonical key S01
        self.assertIn("S01", res1)
        self.assertEqual(res1["S01"], ())

        # 10. Strategy chỉ advance theo lifecycle mới sau reset
        self.assertEqual(strat.bar_counter, 1)
        self.assertEqual(strat.last_bar, 1)

        # Advance to bar 2 to confirm normal ongoing lifecycle
        ctx2 = _make_context(bar_index=2, ts_offset_min=2)
        res2 = registry.evaluate_enabled(ctx2)
        self.assertIn("S01", res2)
        self.assertEqual(strat.bar_counter, 2)
        self.assertEqual(strat.last_bar, 2)

    def test_56_recovery_after_profile_drift(self):
        strat = DummyStatefulStrategy("S01")
        original_profile = strat.profile
        registry = StrategyRegistry([strat])

        # Thay profile
        strat.profile = StrategyProfile(strategy_id="S01", name="TamperedProfile")

        # Gây preflight failure
        ctx1 = _make_context(bar_index=1, ts_offset_min=1)
        with self.assertRaises(StrategyStateError):
            registry.evaluate_enabled(ctx1)
        self.assertTrue(registry._poisoned)
        self.assertEqual(strat.bar_counter, 0)

        # Khôi phục đúng profile snapshot
        strat.profile = original_profile

        # reset_all()
        registry.reset_all()
        self.assertFalse(registry._poisoned)
        self.assertIsNone(registry._last_result)

        # Replay thành công
        res1 = registry.evaluate_enabled(ctx1)
        self.assertIn("S01", res1)
        self.assertEqual(strat.bar_counter, 1)


if __name__ == "__main__":
    unittest.main()
