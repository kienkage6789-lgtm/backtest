"""
tests/test_smc_engine_models.py
===============================
Comprehensive Unit Tests for T53.1 — Domain models, validation, deep immutability,
stable ID schemes, and JSON round-trip serialization exact parity.
"""

import copy
import datetime
import json
import math
import unittest
from types import MappingProxyType
import numpy as np
import pandas as pd

from smc.engine.models import (
    EvidenceRef,
    StrategyContext,
    StrategyProfile,
    CandidateSetup,
    MarketRegime,
    StrategyEvaluation,
    SelectionDecision,
    SwingPointSnapshot,
    StructureEventSnapshot,
    FairValueGapSnapshot,
    OrderBlockSnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    SessionDecisionSnapshot,
    BiasStateSnapshot,
    StrictModelTypeError,
    _freeze,
    make_evidence_id,
    make_cluster_id,
    make_setup_id,
    make_decision_id,
)
from smc.models import (
    SwingPoint,
    StructureEvent,
    FairValueGap,
    OrderBlock,
    LiquidityPool,
    LiquiditySweep,
    SessionDecision,
    BiasState,
)


class TestSMCEngineModels(unittest.TestCase):

    def setUp(self):
        self.ts = pd.Timestamp("2026-01-15 10:00:00+00:00")
        self.bct = pd.Timestamp("2026-01-15 10:01:00+00:00")

        self.sample_evidence_1 = EvidenceRef(
            evidence_id="sweep:internal:40:bearish_pool1",
            kind="liquidity_sweep",
            bar_index=40,
            price=2050.50,
            time=self.ts,
            details={"pool_kind": "equal_highs", "wick_ratio": 0.45},
        )
        self.sample_evidence_2 = EvidenceRef(
            evidence_id="fvg:internal:42:bearish",
            kind="fair_value_gap",
            bar_index=42,
            price=2048.20,
            time=self.ts,
            details={"top": 2049.00, "bottom": 2047.50},
        )

    # =========================================================================
    # 1. P1.1: DEEP IMMUTABILITY & READ-ONLY SNAPSHOT PROBES
    # =========================================================================

    def test_p1_1_strategy_context_deep_immutability(self):
        sp = SwingPoint(index=5, time=self.ts, price=2010.0, kind="high", strength=3, confirmed_at=8, broken=False)
        fvg = FairValueGap(index=11, time=self.ts, direction="bullish", top=2015.0, bottom=2013.0, confirmed_at=12, filled=False)
        ob = OrderBlock(index=9, time=self.ts, direction="bullish", high=2011.0, low=2008.0, open=2009.0, close=2010.0, origin_type="BOS", valid=True)
        pool = LiquidityPool(kind="equal_highs", price=2020.0, price_max=2020.5, price_min=2019.5, indices=[1, 2], created_at=5, confirmed_at=5, source_swings=[{"x": 10}])

        ctx = StrategyContext(
            bar_index=50, timestamp=self.ts, bar_close_time=self.bct, symbol="XAUUSD", timeframe="M1",
            open=2040.0, high=2045.0, low=2038.0, close=2042.0, volume=120.0, atr14=2.5,
            recent_swings=(sp,), active_fvgs=(fvg,), active_obs=(ob,), active_pools=(pool,),
            meta={"nested": {"inner": 123}},
        )

        # 1. Mutate source object outside after creation does NOT affect context
        sp.broken = True
        pool.indices.append(999)
        pool.source_swings[0]["x"] = 999
        fvg.filled = True
        ob.valid = False

        self.assertFalse(ctx.recent_swings[0].broken, "Context swing broken must remain False")
        self.assertEqual(ctx.active_pools[0].indices, (1, 2), "Context pool indices must remain unchanged")
        self.assertEqual(ctx.active_pools[0].source_swings[0]["x"], 10, "Context source swings must remain unchanged")
        self.assertFalse(ctx.active_fvgs[0].filled, "Context FVG filled must remain False")
        self.assertTrue(ctx.active_obs[0].valid, "Context OB valid must remain True")

        # 2. Mutate context child directly MUST raise
        with self.assertRaises((AttributeError, TypeError)):
            ctx.recent_swings[0].broken = True  # type: ignore

        with self.assertRaises((AttributeError, TypeError)):
            ctx.active_fvgs[0].filled = True  # type: ignore

        with self.assertRaises((AttributeError, TypeError)):
            ctx.active_obs[0].valid = False  # type: ignore

        with self.assertRaises(AttributeError):
            ctx.active_pools[0].indices.append(3)  # type: ignore

        with self.assertRaises(TypeError):
            ctx.active_pools[0].source_swings[0]["x"] = 999  # type: ignore

        with self.assertRaises(TypeError):
            ctx.meta["nested"]["inner"] = 999  # type: ignore

        # 3. Round-trip restored context maintains deep immutability
        dumped = json.dumps(ctx.to_dict(), allow_nan=False)
        restored = StrategyContext.from_dict(json.loads(dumped))
        with self.assertRaises((AttributeError, TypeError)):
            restored.recent_swings[0].broken = True  # type: ignore
        with self.assertRaises(AttributeError):
            restored.active_pools[0].indices.append(99)  # type: ignore

    # =========================================================================
    # 2. P1.2: BOOLEAN PRESERVATION & NUMPY SERIALIZATION PROBES
    # =========================================================================

    def test_p1_2_boolean_and_numpy_serialization(self):
        ev = EvidenceRef(
            evidence_id="e_num",
            kind="liquidity_sweep",
            bar_index=np.int64(45),
            price=np.float32(2045.50),
            details={
                "enabled": True,
                "disabled": False,
                "b_val": np.bool_(True),
                "i_val": np.int32(42),
                "f_val": np.float64(123.456),
                "set_val": {3, 1, 2},
            },
        )
        payload = ev.to_dict()

        # Check exact boolean preservation
        self.assertIs(payload["details"]["enabled"], True)
        self.assertEqual(type(payload["details"]["enabled"]), bool)
        self.assertIs(payload["details"]["disabled"], False)
        self.assertEqual(type(payload["details"]["disabled"]), bool)
        self.assertIs(payload["details"]["b_val"], True)
        self.assertEqual(type(payload["details"]["b_val"]), bool)

        # Check exact integer / float normalization
        self.assertEqual(payload["details"]["i_val"], 42)
        self.assertEqual(type(payload["details"]["i_val"]), int)
        self.assertEqual(payload["details"]["f_val"], 123.456)
        self.assertEqual(type(payload["details"]["f_val"]), float)

        # Check deterministic sorted list from set
        self.assertEqual(payload["details"]["set_val"], [1, 2, 3])

        # Check JSON dump with allow_nan=False
        dumped = json.dumps(payload, allow_nan=False)
        self.assertIn('"enabled": true', dumped)
        self.assertIn('"disabled": false', dumped)

    def test_p1_2_reject_boolean_in_numeric_fields(self):
        with self.assertRaises(ValueError):
            EvidenceRef(evidence_id="e1", kind="fair_value_gap", bar_index=1, price=True)  # type: ignore
        with self.assertRaises(ValueError):
            EvidenceRef(evidence_id="e1", kind="fair_value_gap", bar_index=True, price=100.0)  # type: ignore
        with self.assertRaises(ValueError):
            CandidateSetup(
                setup_id="s1", strategy_id="S01", direction="BUY", bar_index=True, timestamp=self.ts,  # type: ignore
                entry_price=2040.0, stop_loss=2035.0, take_profit=2055.0, planned_rr=3.0,
                evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
            )
        with self.assertRaises(ValueError):
            CandidateSetup(
                setup_id="s1", strategy_id="S01", direction="BUY", bar_index=45, timestamp=self.ts,
                entry_price=True, stop_loss=2035.0, take_profit=2055.0, planned_rr=3.0,  # type: ignore
                evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
            )

    # =========================================================================
    # 3. P1.3: STABLE ID GENERATORS & INJECTIVE NON-COLLISION PROBES
    # =========================================================================

    def test_p1_3_stable_setup_id_no_collision(self):
        # Must NOT collide between 'a:b' and 'a_b'
        id_colon = make_setup_id("S", "BUY", 1, "a:b")
        id_underscore = make_setup_id("S", "BUY", 1, "a_b")
        self.assertNotEqual(id_colon, id_underscore)
        self.assertEqual(id_colon, "S:BUY:1:a:b")
        self.assertEqual(id_underscore, "S:BUY:1:a_b")

        # Must differ if case differs
        self.assertNotEqual(
            make_setup_id("s1", "BUY", 1, "c1"),
            make_setup_id("S1", "BUY", 1, "c1"),
        )
        self.assertNotEqual(
            make_setup_id("S1", "BUY", 1, "c1"),
            make_setup_id("S1", "BUY", 1, "C1"),
        )

        # Check make_evidence_id no collision
        self.assertNotEqual(
            make_evidence_id("sweep", "internal", 10, "sub_1"),
            make_evidence_id("sweep", "internal", 10, "sub_2"),
        )
        with self.assertRaises(ValueError):
            make_evidence_id("sweep", "internal", 10, "a:b")

        # Check make_cluster_id no collision
        self.assertNotEqual(
            make_cluster_id("BUY", "leg_1", "zone_1"),
            make_cluster_id("SELL", "leg_1", "zone_1"),
        )

        # Check make_decision_id
        self.assertEqual(make_decision_id(10, "SELECT", "S01"), "sel:10:SELECT:S01")
        self.assertEqual(make_decision_id(10, "NO_TRADE"), "sel:10:NO_TRADE:none")

        # Invalid component rejections
        with self.assertRaises(ValueError):
            make_setup_id("", "BUY", 1, "c1")
        with self.assertRaises(ValueError):
            make_setup_id("S1", "INVALID", 1, "c1")
        with self.assertRaises(ValueError):
            make_setup_id("S1", "BUY", -1, "c1")
        with self.assertRaises(ValueError):
            make_setup_id("S1", "BUY", True, "c1")  # type: ignore
        with self.assertRaises(ValueError):
            make_setup_id("S1", "BUY", 1, "")

    # =========================================================================
    # 4. P1.4: POST-ROUNDING GEOMETRY INVARIANT PROBES
    # =========================================================================

    def test_p1_4_post_rounding_geometry_invariant(self):
        # Valid before rounding but collapsing after rounding to 3 decimals
        with self.assertRaises(ValueError) as ctx:
            CandidateSetup(
                setup_id="s_collapse", strategy_id="S01", direction="BUY", bar_index=45, timestamp=self.ts,
                entry_price=1.0004, stop_loss=1.0001, take_profit=1.00049, planned_rr=3.0,
                evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
            )
        self.assertIn("Invalid BUY geometry after precision rounding", str(ctx.exception))

        # Reversal after rounding
        with self.assertRaises(ValueError):
            CandidateSetup(
                setup_id="s_rev", strategy_id="S01", direction="SELL", bar_index=45, timestamp=self.ts,
                entry_price=2000.0004, stop_loss=2000.00049, take_profit=1995.0, planned_rr=2.0,
                evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
            )

        # Two levels becoming equal after rounding
        with self.assertRaises(ValueError):
            CandidateSetup(
                setup_id="s_eq", strategy_id="S01", direction="BUY", bar_index=45, timestamp=self.ts,
                entry_price=2000.0004, stop_loss=2000.0001, take_profit=2000.00049, planned_rr=2.0,
                evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
            )

        # Valid BUY and SELL after rounding
        buy_valid = CandidateSetup(
            setup_id="s_b_ok", strategy_id="S01", direction="BUY", bar_index=45, timestamp=self.ts,
            entry_price=2000.002, stop_loss=2000.001, take_profit=2000.005, planned_rr=3.0,
            evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
        )
        self.assertEqual(buy_valid.stop_loss, 2000.001)
        self.assertEqual(buy_valid.entry_price, 2000.002)
        self.assertEqual(buy_valid.take_profit, 2000.005)

        sell_valid = CandidateSetup(
            setup_id="s_s_ok", strategy_id="S01", direction="SELL", bar_index=45, timestamp=self.ts,
            entry_price=2000.002, stop_loss=2000.005, take_profit=2000.001, planned_rr=3.0,
            evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
        )
        self.assertEqual(sell_valid.take_profit, 2000.001)
        self.assertEqual(sell_valid.entry_price, 2000.002)
        self.assertEqual(sell_valid.stop_loss, 2000.005)

    # =========================================================================
    # 5. P1.5: SELECTION DECISION CROSS-FIELD VALIDATION PROBES
    # =========================================================================

    def test_p1_5_selection_decision_cross_field_validation(self):
        setup = CandidateSetup(
            setup_id="S01:BUY:45:c1", strategy_id="S01", direction="BUY", bar_index=45, timestamp=self.ts,
            entry_price=2040.0, stop_loss=2035.0, take_profit=2055.0, planned_rr=3.0,
            evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
        )

        eval_item = StrategyEvaluation(
            candidate=setup, status="ELIGIBLE", regime_score=80.0, setup_score=90.0,
            context_score=60.0, exec_score=75.0, total_score=78.25,
        )

        # 1. primary_strategy_id mismatch with selected_setup.strategy_id
        with self.assertRaises(ValueError) as ctx:
            SelectionDecision(
                decision_id=make_decision_id(45, "SELECT", "OTHER"), bar_index=45, timestamp=self.ts, action="SELECT",
                selected_setup=setup, primary_strategy_id="OTHER", evaluations=(eval_item,),
            )
        self.assertIn("does not match selected_setup.strategy_id", str(ctx.exception))

        # 2. Cannot select expired setup (decision bar_index > expiry_bar)
        with self.assertRaises(ValueError) as ctx:
            SelectionDecision(
                decision_id=make_decision_id(61, "SELECT", "S01"), bar_index=61, timestamp=self.ts + pd.Timedelta(minutes=16),
                action="SELECT", selected_setup=setup, primary_strategy_id="S01", evaluations=(eval_item,),
            )
        self.assertIn("Cannot select expired setup", str(ctx.exception))

        # 3. Cannot select future setup (setup bar_index > decision bar_index)
        with self.assertRaises(ValueError) as ctx:
            SelectionDecision(
                decision_id=make_decision_id(40, "SELECT", "S01"), bar_index=40, timestamp=self.ts - pd.Timedelta(minutes=5),
                action="SELECT", selected_setup=setup, primary_strategy_id="S01", evaluations=(eval_item,),
            )
        self.assertIn("Cannot select future setup", str(ctx.exception))

        # 4. supporting_strategy_ids cannot contain primary_strategy_id
        with self.assertRaises(ValueError) as ctx:
            SelectionDecision(
                decision_id=make_decision_id(45, "SELECT", "S01"), bar_index=45, timestamp=self.ts, action="SELECT",
                selected_setup=setup, primary_strategy_id="S01", supporting_strategy_ids=("S01", "S09"),
                evaluations=(eval_item,),
            )
        self.assertIn("cannot contain primary_strategy_id", str(ctx.exception))

        # 5. supporting_strategy_ids cannot contain duplicates
        with self.assertRaises(ValueError) as ctx:
            SelectionDecision(
                decision_id=make_decision_id(45, "SELECT", "S01"), bar_index=45, timestamp=self.ts, action="SELECT",
                selected_setup=setup, primary_strategy_id="S01", supporting_strategy_ids=("S09", "S09"),
                evaluations=(eval_item,),
            )
        self.assertIn("duplicate", str(ctx.exception))

        # 6. Contradictory execution_payload (T53.8 requires strictly empty {})
        with self.assertRaises(ValueError):
            SelectionDecision(
                decision_id=make_decision_id(45, "SELECT", "S01"), bar_index=45, timestamp=self.ts, action="SELECT",
                selected_setup=setup, primary_strategy_id="S01",
                execution_payload={"direction": "SELL"}, evaluations=(eval_item,),
            )

        # 7. NO_TRADE cannot have selected_setup or primary_strategy_id or active signal
        with self.assertRaises(ValueError):
            SelectionDecision(
                decision_id=make_decision_id(45, "NO_TRADE", None), bar_index=45, timestamp=self.ts, action="NO_TRADE",
                selected_setup=setup,
            )
        with self.assertRaises(ValueError):
            SelectionDecision(
                decision_id=make_decision_id(45, "NO_TRADE", None), bar_index=45, timestamp=self.ts, action="NO_TRADE",
                primary_strategy_id="S01",
            )
        with self.assertRaises(ValueError):
            SelectionDecision(
                decision_id=make_decision_id(45, "NO_TRADE", None), bar_index=45, timestamp=self.ts, action="NO_TRADE",
                execution_payload={"signal": 1},
            )

    # =========================================================================
    # 6. GENERAL VALIDATION & EDGE CASES
    # =========================================================================

    def test_evidence_ref_validation(self):
        # Empty ID
        with self.assertRaises(ValueError):
            EvidenceRef(evidence_id="", kind="fair_value_gap", bar_index=1, price=100.0)
        # Invalid kind
        with self.assertRaises(ValueError):
            EvidenceRef(evidence_id="e1", kind="random_kind", bar_index=1, price=100.0)  # type: ignore
        # Negative bar index
        with self.assertRaises(ValueError):
            EvidenceRef(evidence_id="e1", kind="fair_value_gap", bar_index=-1, price=100.0)
        # NaN / Inf price
        with self.assertRaises(ValueError):
            EvidenceRef(evidence_id="e1", kind="fair_value_gap", bar_index=1, price=float("nan"))
        with self.assertRaises(ValueError):
            EvidenceRef(evidence_id="e1", kind="fair_value_gap", bar_index=1, price=float("inf"))

    def test_strategy_context_ohlc_validation(self):
        # High < Low
        with self.assertRaises(ValueError):
            StrategyContext(
                bar_index=1, timestamp=self.ts, bar_close_time=self.bct, symbol="XAUUSD", timeframe="M1",
                open=2040.0, high=2030.0, low=2035.0, close=2038.0, volume=10.0, atr14=1.0,
            )
        # High < Open
        with self.assertRaises(ValueError):
            StrategyContext(
                bar_index=1, timestamp=self.ts, bar_close_time=self.bct, symbol="XAUUSD", timeframe="M1",
                open=2050.0, high=2045.0, low=2030.0, close=2040.0, volume=10.0, atr14=1.0,
            )
        # Low > Close
        with self.assertRaises(ValueError):
            StrategyContext(
                bar_index=1, timestamp=self.ts, bar_close_time=self.bct, symbol="XAUUSD", timeframe="M1",
                open=2040.0, high=2045.0, low=2035.0, close=2030.0, volume=10.0, atr14=1.0,
            )
        # bar_close_time < timestamp
        with self.assertRaises(ValueError):
            StrategyContext(
                bar_index=1, timestamp=self.bct, bar_close_time=self.ts, symbol="XAUUSD", timeframe="M1",
                open=2040.0, high=2045.0, low=2035.0, close=2040.0, volume=10.0, atr14=1.0,
            )

    def test_strategy_profile_validation(self):
        # Invalid style
        with self.assertRaises(ValueError):
            StrategyProfile(strategy_id="S01", name="S", style="arbitrary")  # type: ignore
        # Empty directions
        with self.assertRaises(ValueError):
            StrategyProfile(strategy_id="S01", name="S", allowed_directions=())
        # Non-positive max_setup_age_bars
        with self.assertRaises(ValueError):
            StrategyProfile(strategy_id="S01", name="S", max_setup_age_bars=0)
        # Bool as max_setup_age_bars
        with self.assertRaises(ValueError):
            StrategyProfile(strategy_id="S01", name="S", max_setup_age_bars=True)  # type: ignore

    def test_market_regime_validation(self):
        with self.assertRaises(ValueError):
            MarketRegime(regime="unknown_regime", bar_index=1, timestamp=self.ts, efficiency_ratio=0.5, atr_percentile=50.0)  # type: ignore
        with self.assertRaises(ValueError):
            MarketRegime(regime="ranging", bar_index=1, timestamp=self.ts, efficiency_ratio=1.5, atr_percentile=50.0)
        with self.assertRaises(ValueError):
            MarketRegime(regime="ranging", bar_index=1, timestamp=self.ts, efficiency_ratio=0.5, atr_percentile=150.0)
        with self.assertRaises(ValueError):
            MarketRegime(regime="ranging", bar_index=True, timestamp=self.ts, efficiency_ratio=0.5, atr_percentile=50.0)  # type: ignore

    def test_strategy_evaluation_validation(self):
        setup = CandidateSetup(
            setup_id="S01:BUY:45:c1", strategy_id="S01", direction="BUY", bar_index=45, timestamp=self.ts,
            entry_price=2040.0, stop_loss=2035.0, take_profit=2055.0, planned_rr=3.0,
            evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=60,
        )
        # REJECTED must have at least one reason
        with self.assertRaises(ValueError):
            StrategyEvaluation(candidate=setup, status="REJECTED", rejection_reasons=())

        # ELIGIBLE cannot have rejection reasons
        with self.assertRaises(ValueError):
            StrategyEvaluation(candidate=setup, status="ELIGIBLE", rejection_reasons=("some_reason",))

    # =========================================================================
    # 7. P2.1: FULL PAYLOAD EXACT JSON ROUND-TRIP FOR ALL 7 MODELS
    # =========================================================================

    def test_p2_1_exact_json_round_trip_all_7_models(self):
        # 1. EvidenceRef
        p1 = self.sample_evidence_1.to_dict()
        r1 = EvidenceRef.from_dict(json.loads(json.dumps(p1, allow_nan=False)))
        self.assertEqual(r1.to_dict(), p1)

        # 2. StrategyProfile
        prof = StrategyProfile(
            strategy_id="S05", name="BOS to OB Retest", version="1.0.0", style="continuation",
            allowed_directions=("BUY", "SELL"), timeframes=("M1", "M5"), max_setup_age_bars=25,
            cooldown_bars=3, min_rr=1.5, params={"require_ob": True, "num": 10},
        )
        p2 = prof.to_dict()
        r2 = StrategyProfile.from_dict(json.loads(json.dumps(p2, allow_nan=False)))
        self.assertEqual(r2.to_dict(), p2)

        # 3. StrategyContext
        sp = SwingPoint(index=5, time=self.ts, price=2010.0, kind="high", strength=3, confirmed_at=8)
        se = StructureEvent(index=10, time=self.ts, event_type="BOS", direction="bullish", broken_swing_index=5, broken_swing_price=2010.0, close_price=2012.0)
        fvg = FairValueGap(index=11, time=self.ts, direction="bullish", top=2015.0, bottom=2013.0, confirmed_at=12)
        ob = OrderBlock(index=9, time=self.ts, direction="bullish", high=2011.0, low=2008.0, open=2009.0, close=2010.0, origin_type="BOS")
        pool = LiquidityPool(kind="equal_highs", price=2020.0, price_max=2020.5, price_min=2019.5, indices=[1, 2], created_at=5, confirmed_at=5)
        sweep = LiquiditySweep(index=20, time=self.ts, direction="bearish", pool_kind="equal_highs", pool_price=2020.0, pool_indices=[1, 2], price_wick=2021.0, close_price=2019.0, created_at=20, confirmed_at=20, swept_at=20)
        sd = SessionDecision(in_session=True, session_name="London", timestamp=self.ts, reason="inside_session")
        hb = BiasState(bias="bullish", timestamp=self.ts, source_event_index=10, as_of=self.ts)

        ctx = StrategyContext(
            bar_index=50, timestamp=self.ts, bar_close_time=self.bct, symbol="XAUUSD", timeframe="M1",
            open=2040.0, high=2045.0, low=2038.0, close=2042.0, volume=120.0, atr14=2.5,
            recent_swings=(sp,), recent_structures=(se,), active_fvgs=(fvg,), active_obs=(ob,),
            active_pools=(pool,), recent_sweeps=(sweep,), session_decision=sd, htf_bias=hb,
            meta={"feed": "sqlite"},
        )
        p3 = ctx.to_dict()
        r3 = StrategyContext.from_dict(json.loads(json.dumps(p3, allow_nan=False)))
        self.assertEqual(r3.to_dict(), p3)

        # 4. CandidateSetup
        setup = CandidateSetup(
            setup_id="S01:BUY:45:c1", strategy_id="S01", direction="BUY", bar_index=45, timestamp=self.ts,
            entry_price=2040.0, stop_loss=2035.0, take_profit=2055.0, planned_rr=3.0,
            evidences=(self.sample_evidence_1, self.sample_evidence_2),
            evidence_cluster_id="BUY:leg1:fvg1", expiry_bar=60,
            quality_scores={"base": 80.0, "bonus": 10.0},
        )
        p4 = setup.to_dict()
        r4 = CandidateSetup.from_dict(json.loads(json.dumps(p4, allow_nan=False)))
        self.assertEqual(r4.to_dict(), p4)

        # 5. MarketRegime
        mr = MarketRegime(
            regime="volatile_reversal", bar_index=45, timestamp=self.ts, efficiency_ratio=0.55,
            atr_percentile=82.0, metrics={"atr14": 3.1},
        )
        p5 = mr.to_dict()
        r5 = MarketRegime.from_dict(json.loads(json.dumps(p5, allow_nan=False)))
        self.assertEqual(r5.to_dict(), p5)

        # 6. StrategyEvaluation
        eval_item = StrategyEvaluation(
            candidate=setup, status="ELIGIBLE", regime_score=80.0, setup_score=90.0,
            context_score=60.0, exec_score=75.0, total_score=78.25, details={"note": "clean_sweep"},
        )
        p6 = eval_item.to_dict()
        r6 = StrategyEvaluation.from_dict(json.loads(json.dumps(p6, allow_nan=False)))
        self.assertEqual(r6.to_dict(), p6)

        # 7. SelectionDecision
        dec = SelectionDecision(
            decision_id="sel:45:SELECT:S01", bar_index=45, timestamp=self.ts, action="SELECT",
            selected_setup=setup, primary_strategy_id="S01", supporting_strategy_ids=("S09",),
            regime=mr, evaluations=(eval_item,), reason="ok", score_gap=18.5,
            execution_payload={},
        )
        p7 = dec.to_dict()
        r7 = SelectionDecision.from_dict(json.loads(json.dumps(p7, allow_nan=False)))
        self.assertEqual(r7.to_dict(), p7)

    # =========================================================================
    # 8. QC PROBES: P1.1 SNAPSHOT IMMUTABILITY & NESTED LEAKAGE PROTECTION
    # =========================================================================

    def test_p1_1_session_and_bias_snapshot_deep_immutability(self):
        sd_meta = {"flag": True, "nested": {"a": 1, "arr": [1, 2, 3]}}
        hb_meta = {"conf": 0.9, "nested": {"b": 2, "set_data": {1, 2}}}

        sd = SessionDecision(
            in_session=True,
            session_name="London",
            timestamp=self.ts,
            reason="in_killzone",
            meta=sd_meta,
        )
        hb = BiasState(
            bias="bullish",
            timestamp=self.ts,
            source_event_index=10,
            as_of=self.ts,
            reason="bos_confirmed",
            meta=hb_meta,
        )

        ctx = StrategyContext(
            bar_index=50, timestamp=self.ts, bar_close_time=self.bct, symbol="XAUUSD", timeframe="M1",
            open=2040.0, high=2045.0, low=2038.0, close=2042.0, volume=120.0, atr14=2.5,
            session_decision=sd, htf_bias=hb,
            meta={"root": {"child": 10}},
        )

        # Snapshots created automatically
        self.assertIsInstance(ctx.session_decision, SessionDecisionSnapshot)
        self.assertIsInstance(ctx.htf_bias, BiasStateSnapshot)

        # 1. Mutating original external dictionary objects outside does NOT leak into context
        sd_meta["flag"] = False
        sd_meta["nested"]["a"] = 999
        sd_meta["nested"]["arr"].append(999)

        hb_meta["conf"] = 0.1
        hb_meta["nested"]["b"] = 999

        self.assertTrue(ctx.session_decision.in_session)
        self.assertEqual(ctx.session_decision.meta["flag"], True)
        self.assertEqual(ctx.session_decision.meta["nested"]["a"], 1)
        self.assertEqual(ctx.session_decision.meta["nested"]["arr"], (1, 2, 3))

        self.assertEqual(ctx.htf_bias.bias, "bullish")
        self.assertEqual(ctx.htf_bias.reason, "bos_confirmed")
        self.assertEqual(ctx.htf_bias.meta["conf"], 0.9)
        self.assertEqual(ctx.htf_bias.meta["nested"]["b"], 2)
        self.assertEqual(ctx.htf_bias.meta["nested"]["set_data"], (1, 2))

        # 2. Mutating context snapshots directly raises AttributeError or TypeError
        with self.assertRaises((AttributeError, TypeError)):
            ctx.session_decision.in_session = False  # type: ignore
        with self.assertRaises(TypeError):
            ctx.session_decision.meta["flag"] = False  # type: ignore
        with self.assertRaises(TypeError):
            ctx.session_decision.meta["nested"]["a"] = 999  # type: ignore

        with self.assertRaises((AttributeError, TypeError)):
            ctx.htf_bias.bias = "bearish"  # type: ignore
        with self.assertRaises(TypeError):
            ctx.htf_bias.meta["nested"]["b"] = 999  # type: ignore

        # 3. Round-trip serialization restored object maintains deep immutability
        dumped = json.dumps(ctx.to_dict(), allow_nan=False)
        restored = StrategyContext.from_dict(json.loads(dumped))
        self.assertIsInstance(restored.session_decision, SessionDecisionSnapshot)
        self.assertIsInstance(restored.htf_bias, BiasStateSnapshot)

        with self.assertRaises((AttributeError, TypeError)):
            restored.session_decision.in_session = False  # type: ignore
        with self.assertRaises(TypeError):
            restored.session_decision.meta["nested"]["a"] = 999  # type: ignore
        with self.assertRaises(TypeError):
            restored.htf_bias.meta["nested"]["b"] = 999  # type: ignore

    # =========================================================================
    # 9. QC PROBES: P1.2 FAIL-FAST ON UNSUPPORTED METADATA & NUMPY COMPATIBILITY
    # =========================================================================

    def test_p1_2_fail_fast_on_unsupported_metadata_types(self):
        class CustomClass:
            pass

        # Unsupported arbitrary object in metadata raises TypeError
        with self.assertRaises(TypeError):
            _freeze({"unsupported": CustomClass()})

        with self.assertRaises(TypeError):
            SessionDecisionSnapshot(
                in_session=True, session_name="NY", timestamp=self.ts, reason="ok",
                meta={"bad": CustomClass()},
            )

        with self.assertRaises(TypeError):
            BiasStateSnapshot(
                bias="neutral", timestamp=self.ts,
                meta={"bad": CustomClass()},
            )

        # Non-finite float raises ValueError
        with self.assertRaises(ValueError):
            _freeze({"nan_val": float("nan")})

        with self.assertRaises(ValueError):
            _freeze({"inf_val": float("inf")})

    def test_p1_2_numpy_and_json_safe_direct_dumps(self):
        ctx = StrategyContext(
            bar_index=50, timestamp=self.ts, bar_close_time=self.bct, symbol="XAUUSD", timeframe="M1",
            open=2040.0, high=2045.0, low=2038.0, close=2042.0, volume=120.0, atr14=2.5,
            session_decision=SessionDecisionSnapshot(
                in_session=True, session_name="London", timestamp=self.ts, reason="in_killzone",
                meta={
                    "np_bool": np.bool_(True),
                    "np_int": np.int64(100),
                    "np_float": np.float32(3.14),
                },
            ),
            htf_bias=BiasStateSnapshot(
                bias="bullish", timestamp=self.ts,
                meta={
                    "ts": pd.Timestamp("2026-01-15 10:00:00"),
                    "np_float64": np.float64(99.9),
                },
            ),
            meta={
                "scalar_f": np.float64(1.234),
                "scalar_i": np.int32(42),
                "scalar_b": np.bool_(False),
                "tuple_data": (1, "a", True),
                "set_data": {"z", "a", "m"},
            },
        )
        ctx_dict = ctx.to_dict()
        # Direct json.dumps with allow_nan=False MUST succeed without TypeError or ValueError
        serialized = json.dumps(ctx_dict, allow_nan=False)
        self.assertIsInstance(serialized, str)
        self.assertIn('"scalar_b": false', serialized)
        self.assertIn('"np_bool": true', serialized)
        # Verify set_data was sorted deterministically
        self.assertEqual(ctx_dict["meta"]["set_data"], ["a", "m", "z"])

    # =========================================================================
    # 10. QC PROBES: P1.3 INJECTIVE GRAMMAR MATRIX (HƯỚNG A)
    # =========================================================================

    def test_p1_3_injective_grammar_rejections_and_matrix(self):
        # Base components must NOT contain ':'
        with self.assertRaises(ValueError):
            make_setup_id("S:01", "BUY", 1, "c1")
        with self.assertRaises(ValueError):
            make_setup_id("S01", "BUY:SELL", 1, "c1")
        with self.assertRaises(ValueError):
            make_cluster_id("BUY", "leg:1", "zone1")
        with self.assertRaises(ValueError):
            make_cluster_id("BUY", "leg1", "zone:1")
        with self.assertRaises(ValueError):
            make_evidence_id("sweep:bad", "internal", 10, "key1")
        with self.assertRaises(ValueError):
            make_evidence_id("sweep", "internal:bad", 10, "key1")
        with self.assertRaises(ValueError):
            make_evidence_id("sweep", "internal", 10, "key:bad")

        # Base components must NOT have leading or trailing whitespace
        with self.assertRaises(ValueError):
            make_setup_id(" S01", "BUY", 1, "c1")
        with self.assertRaises(ValueError):
            make_setup_id("S01 ", "BUY", 1, "c1")
        with self.assertRaises(ValueError):
            make_cluster_id("BUY", " leg1", "zone1")
        with self.assertRaises(ValueError):
            make_cluster_id("BUY", "leg1 ", "zone1")

        # cluster_id allows ':' between valid base tokens
        valid_cid = "cls:leg_1:zone_1"
        sid = make_setup_id("S01", "BUY", 10, valid_cid)
        self.assertEqual(sid, "S01:BUY:10:cls:leg_1:zone_1")

        # cluster_id rejects empty segments or invalid chars
        with self.assertRaises(ValueError):
            make_setup_id("S01", "BUY", 10, "cls::zone1")
        with self.assertRaises(ValueError):
            make_setup_id("S01", "BUY", 10, ":cls:zone1")
        with self.assertRaises(ValueError):
            make_setup_id("S01", "BUY", 10, "cls:zone1:")
        with self.assertRaises(ValueError):
            make_setup_id("S01", "BUY", 10, "cls:zone 1")

        # Reversible parsing test: split first 3 colons to retrieve (strat, dir, bar, cluster)
        parts = sid.split(":", 3)
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "S01")
        self.assertEqual(parts[1], "BUY")
        self.assertEqual(parts[2], "10")
        self.assertEqual(parts[3], valid_cid)

        # make_decision_id contract
        self.assertEqual(make_decision_id(15, "NO_TRADE"), "sel:15:NO_TRADE:none")
        self.assertEqual(make_decision_id(15, "SELECT", "S01"), "sel:15:SELECT:S01")
        with self.assertRaises(ValueError):
            make_decision_id(15, "SELECT", "none")  # 'none' strategy forbidden on SELECT
        with self.assertRaises(ValueError):
            make_decision_id(15, "SELECT", "S:01")  # delimiter forbidden in strategy ID

    # =========================================================================
    # 11. QC PROBES: P1.4 STRICT from_dict MISSING FIELDS (KeyError)
    # =========================================================================

    def test_p1_4_strict_from_dict_missing_required_fields(self):
        # 1. EvidenceRef
        with self.assertRaises(KeyError) as cm:
            EvidenceRef.from_dict({"kind": "liquidity_sweep", "bar_index": 1, "price": 100.0})
        self.assertIn("evidence_id", str(cm.exception))

        # 2. StrategyProfile
        with self.assertRaises(KeyError) as cm:
            StrategyProfile.from_dict({"strategy_id": "S01"})
        self.assertIn("name", str(cm.exception))

        # 3. CandidateSetup
        with self.assertRaises(KeyError) as cm:
            CandidateSetup.from_dict({"setup_id": "s1", "strategy_id": "S01"})
        self.assertIn("Missing required field", str(cm.exception))

        # 4. MarketRegime
        with self.assertRaises(KeyError) as cm:
            MarketRegime.from_dict({"regime": "ranging"})
        self.assertIn("Missing required field", str(cm.exception))

        # 5. StrategyEvaluation
        with self.assertRaises(KeyError) as cm:
            StrategyEvaluation.from_dict({"status": "ELIGIBLE"})
        self.assertIn("candidate", str(cm.exception))

        # 6. SelectionDecision
        with self.assertRaises(KeyError) as cm:
            SelectionDecision.from_dict({"decision_id": "d1"})
        self.assertIn("Missing required field", str(cm.exception))

        # 7. StrategyContext
        with self.assertRaises(KeyError) as cm:
            StrategyContext.from_dict({"bar_index": 1})
        self.assertIn("Missing required field", str(cm.exception))

        # Snapshots
        with self.assertRaises(KeyError) as cm:
            SwingPointSnapshot.from_source({"time": self.ts, "price": 100.0})
        self.assertIn("index", str(cm.exception))

        with self.assertRaises(KeyError) as cm:
            FairValueGapSnapshot.from_source({"index": 1})
        self.assertIn("time", str(cm.exception))

        with self.assertRaises(KeyError) as cm:
            OrderBlockSnapshot.from_source({"index": 1})
        self.assertIn("time", str(cm.exception))

        with self.assertRaises(KeyError) as cm:
            LiquidityPoolSnapshot.from_source({"kind": "equal_highs"})
        self.assertIn("price", str(cm.exception))

        with self.assertRaises(KeyError) as cm:
            LiquiditySweepSnapshot.from_source({"index": 1})
        self.assertIn("time", str(cm.exception))

        with self.assertRaises(KeyError) as cm:
            StructureEventSnapshot.from_source({"index": 1})
        self.assertIn("time", str(cm.exception))

        with self.assertRaises(KeyError) as cm:
            SessionDecisionSnapshot.from_source({"in_session": True})
        self.assertIn("timestamp", str(cm.exception))

        with self.assertRaises(KeyError) as cm:
            BiasStateSnapshot.from_source({"bias": "bullish"})
        self.assertIn("timestamp", str(cm.exception))

    # =========================================================================
    # 12. QC PROBES: P1.4 STRICT TYPE VALIDATION (NO IMPLICIT CASTING)
    # =========================================================================

    def test_p1_4_strict_type_validation_no_implicit_casting(self):
        # 1. Reject bool in numeric / int / float fields
        with self.assertRaises((TypeError, ValueError)):
            EvidenceRef.from_dict({
                "evidence_id": "e1", "kind": "fair_value_gap",
                "bar_index": True, "price": 100.0,
            })
        with self.assertRaises((TypeError, ValueError)):
            EvidenceRef.from_dict({
                "evidence_id": "e1", "kind": "fair_value_gap",
                "bar_index": 10, "price": True,
            })
        with self.assertRaises((TypeError, ValueError)):
            CandidateSetup.from_dict({
                "setup_id": "s1", "strategy_id": "S01", "direction": "BUY",
                "bar_index": 10, "timestamp": self.ts.isoformat(),
                "entry_price": 2040.0, "stop_loss": 2035.0, "take_profit": 2055.0,
                "planned_rr": False,  # bool in float
                "evidences": [self.sample_evidence_1.to_dict()],
                "evidence_cluster_id": "c1",
                "expiry_bar": 20,
            })
        with self.assertRaises((TypeError, ValueError)):
            CandidateSetup.from_dict({
                "setup_id": "s1", "strategy_id": "S01", "direction": "BUY",
                "bar_index": 10, "timestamp": self.ts.isoformat(),
                "entry_price": 2040.0, "stop_loss": 2035.0, "take_profit": 2055.0,
                "planned_rr": 3.0,
                "evidences": [self.sample_evidence_1.to_dict()],
                "evidence_cluster_id": "c1",
                "expiry_bar": False,  # bool in int
            })

        # 2. Reject string / int in bool fields
        with self.assertRaises((TypeError, ValueError)):
            SessionDecisionSnapshot.from_source({
                "in_session": "false",  # string in bool
                "timestamp": self.ts,
                "reason": "ok",
            })
        with self.assertRaises((TypeError, ValueError)):
            SessionDecisionSnapshot.from_source({
                "in_session": 1,  # int in bool
                "timestamp": self.ts,
                "reason": "ok",
            })
        with self.assertRaises((TypeError, ValueError)):
            FairValueGapSnapshot.from_source({
                "index": 1, "time": self.ts, "direction": "bullish",
                "top": 2000.0, "bottom": 1990.0, "confirmed_at": 2,
                "filled": "false",  # string in bool
            })
        with self.assertRaises((TypeError, ValueError)):
            OrderBlockSnapshot.from_source({
                "index": 1, "time": self.ts, "direction": "bullish",
                "high": 2000.0, "low": 1990.0, "open": 1995.0, "close": 1998.0,
                "origin_type": "BOS",
                "valid": "true",  # string in bool
            })

        # 3. Reject string in float fields
        with self.assertRaises((TypeError, ValueError)):
            MarketRegime.from_dict({
                "regime": "ranging",
                "bar_index": 10,
                "timestamp": self.ts.isoformat(),
                "efficiency_ratio": "0.5",  # string in float
                "atr_percentile": 50.0,
            })

        # 4. Reject None in required string / timestamp fields
        with self.assertRaises((TypeError, ValueError)):
            CandidateSetup(
                setup_id="s1", strategy_id="S01", direction="BUY",
                bar_index=10, timestamp=None,  # type: ignore
                entry_price=2040.0, stop_loss=2035.0, take_profit=2055.0, planned_rr=3.0,
                evidences=(self.sample_evidence_1,), evidence_cluster_id="c1", expiry_bar=20,
            )
        with self.assertRaises((TypeError, ValueError)):
            SessionDecisionSnapshot(
                in_session=True, session_name="London",
                timestamp=self.ts, reason=None,  # type: ignore
            )
        with self.assertRaises((TypeError, ValueError)):
            MarketRegime(
                regime="ranging", bar_index=10, timestamp=self.ts,
                efficiency_ratio=0.5, atr_percentile=50.0,
                reason=None,  # type: ignore
            )


if __name__ == "__main__":
    unittest.main()
