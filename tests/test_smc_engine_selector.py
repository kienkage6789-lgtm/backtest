"""
tests.test_smc_engine_selector
==============================
Unit tests for T53.8 — Deterministic Strategy Selector, Ownership & Component Scoring.
Covers Test Matrix Groups A, B, C, D, E (tests 1 to 105).
"""

from __future__ import annotations

import json
import unittest
import pandas as pd

from smc.engine.confluence import ConfluenceBatch, DirectionConflict, EvidenceCluster
from smc.engine.context import StrategyContext
from smc.engine.errors import StrategyStateError, StrategyValidationError, StrictModelTypeError
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    MarketRegime,
    SelectionDecision,
    SessionDecisionSnapshot,
    StrategyEvaluation,
)
from smc.engine.selector import (
    ClusterScorecard,
    DeterministicStrategySelector,
    SelectorConfig,
    SelectorOutput,
    cluster_rank_key,
    compute_context_score,
    compute_execution_score,
    compute_regime_score,
    compute_setup_score,
    compute_total_score,
    member_rank_key,
    select_strategy,
)
from smc.engine.telemetry import SelectionAuditRecord


def _make_context(
    bar_index: int = 100,
    timestamp: str = "2026-03-09 15:00:00+00:00",
    bar_close_time: str = "2026-03-09 15:15:00+00:00",
    bias: str = "bullish",
    in_session: bool = True,
    atr14: float = 5.0,
) -> StrategyContext:
    ts_open = pd.Timestamp(timestamp)
    ts_close = pd.Timestamp(bar_close_time)
    return StrategyContext(
        symbol="EURUSD",
        timeframe="15m",
        bar_index=bar_index,
        timestamp=ts_open,
        bar_close_time=ts_close,
        open=2000.0,
        high=2050.0,
        low=1990.0,
        close=2040.0,
        volume=100.0,
        atr14=atr14,
        htf_bias=BiasStateSnapshot(
            bias=bias,  # type: ignore[arg-type]
            timestamp=ts_open,
        ),
        session_decision=SessionDecisionSnapshot(
            in_session=in_session,
            session_name="NY_AM",
            timestamp=ts_open,
            reason="ok",
        ),
    )


def _make_regime(
    bar_index: int = 100,
    timestamp: str = "2026-03-09 15:15:00+00:00",
    regime: str = "bullish_trend",
) -> MarketRegime:
    return MarketRegime(
        regime=regime,  # type: ignore[arg-type]
        bar_index=bar_index,
        timestamp=pd.Timestamp(timestamp),
        efficiency_ratio=0.8,
        atr_percentile=50.0,
    )


def _make_candidate(
    setup_id: str,
    strategy_id: str,
    direction: str = "BUY",
    bar_index: int = 100,
    timestamp: str = "2026-03-09 15:00:00+00:00",
    cluster_id: str = "c1",
    evidences: tuple[EvidenceRef, ...] = (),
    planned_rr: float = 2.0,
    entry: float = 2000.0,
    sl: float = 1990.0,
    tp: float = 2020.0,
) -> CandidateSetup:
    if direction == "SELL":
        if sl <= entry:
            sl = entry + 10.0
        if tp >= entry:
            tp = entry - 20.0
    if not evidences:
        if strategy_id == "S05":
            evidences = (
                EvidenceRef(f"ev_{setup_id}", "order_block", bar_index - 5, entry, details={"quality": "base"}),
            )
        else:
            evidences = (
                EvidenceRef(f"ev_{setup_id}", "fair_value_gap", bar_index - 5, entry, details={"top": entry + 5.0, "bottom": entry - 5.0}),
            )
    return CandidateSetup(
        setup_id=setup_id,
        strategy_id=strategy_id,
        direction=direction,  # type: ignore[arg-type]
        bar_index=bar_index,
        timestamp=pd.Timestamp(timestamp),
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        planned_rr=planned_rr,
        evidences=evidences,
        evidence_cluster_id=cluster_id,
        expiry_bar=bar_index + 10,
        meta={},
    )


def _make_eval(
    candidate: CandidateSetup,
    status: str = "ELIGIBLE",
    regime_score: float | None = None,
    setup_score: float = 0.0,
    context_score: float = 0.0,
    exec_score: float = 0.0,
    total_score: float = 0.0,
    rejection_reasons: tuple[str, ...] = (),
    regime: str = "bullish_trend",
) -> StrategyEvaluation:
    if regime_score is None:
        if status == "ELIGIBLE":
            from smc.engine.eligibility import get_regime_matrix_score
            regime_score = get_regime_matrix_score(candidate.strategy_id, candidate.direction, regime)
        else:
            regime_score = 0.0
    return StrategyEvaluation(
        candidate=candidate,
        status=status,  # type: ignore[arg-type]
        rejection_reasons=rejection_reasons,
        regime_score=regime_score,
        setup_score=setup_score,
        context_score=context_score,
        exec_score=exec_score,
        total_score=total_score,
        details={},
    )


def _make_cluster_scorecard(
    cluster_id: str = "c1",
    direction: str = "BUY",
    primary_strategy_id: str = "S01",
    primary_setup_id: str = "s1",
    supporting_strategy_ids: tuple[str, ...] = (),
    total_score: float = 85.0,
    regime_score: float | None = None,
    setup_score: float | None = None,
    context_score: float | None = None,
    exec_score: float | None = None,
    planned_rr: float = 2.0,
    member_count: int = 1,
    scoring_weights: tuple[float, float, float, float] | None = None,
    member_setup_ids: tuple[str, ...] | None = None,
    member_scores: dict[str, float] | None = None,
    details: dict[str, Any] | None = None,
) -> ClusterScorecard:
    if member_setup_ids is None:
        if member_count == 1:
            member_setup_ids = (primary_setup_id,)
        else:
            m_list = [primary_setup_id] + [f"{primary_setup_id}_m{i}" for i in range(1, member_count)]
            member_setup_ids = tuple(sorted(set(m_list)))

    if member_scores is None:
        member_scores = {m_id: total_score for m_id in member_setup_ids}

    r_score = regime_score if regime_score is not None else total_score
    s_score = setup_score if setup_score is not None else total_score
    c_score = context_score if context_score is not None else total_score
    e_score = exec_score if exec_score is not None else total_score

    weights = scoring_weights or (0.25, 0.35, 0.25, 0.15)

    return ClusterScorecard(
        cluster_id=cluster_id,
        direction=direction,  # type: ignore[arg-type]
        primary_strategy_id=primary_strategy_id,
        primary_setup_id=primary_setup_id,
        supporting_strategy_ids=supporting_strategy_ids,
        total_score=total_score,
        regime_score=r_score,
        setup_score=s_score,
        context_score=c_score,
        exec_score=e_score,
        planned_rr=planned_rr,
        member_count=member_count,
        scoring_weights=weights,
        member_setup_ids=member_setup_ids,
        member_scores=member_scores,
        details=details or {
            "member_setup_ids": list(member_setup_ids),
            "member_scores": dict(member_scores),
            "execution_score_basis": "planned_rr_pre_fill",
            "fvg_atr_basis": "signal_bar_atr14",
        },
    )


class TestSelectorGroupAComponentScoring(unittest.TestCase):
    """Group A: Component Scoring tests (tests 1 to 25)."""

    def setUp(self):
        self.context = _make_context()
        self.regime = _make_regime(regime="bullish_trend")

    # --- A1: Regime Scoring (Tests 1–5) ---
    def test_01_regime_score_matrix_lookup_bullish_s05_buy(self):
        c = _make_candidate("s1", "S05", "BUY")
        score = compute_regime_score(c, self.regime)
        self.assertEqual(score, 100.0)

    def test_02_regime_score_matrix_lookup_bearish_s05_buy(self):
        reg = _make_regime(regime="bearish_trend")
        c = _make_candidate("s1", "S05", "BUY")
        score = compute_regime_score(c, reg)
        self.assertEqual(score, 0.0)

    def test_03_regime_score_mismatch_with_gate_raises_error(self):
        c = _make_candidate("s1", "S05", "BUY")
        # Gate evaluation says regime_score=80.0, but matrix says 100.0
        ev = _make_eval(c, status="ELIGIBLE", regime_score=80.0)
        with self.assertRaises(StrategyStateError):
            compute_regime_score(c, self.regime, ev)

    def test_04_regime_score_all_30_matrix_cells_valid(self):
        strategies = ("S01", "S05", "S09")
        directions = ("BUY", "SELL")
        regimes = ("bullish_trend", "bearish_trend", "ranging", "volatile_reversal", "uncertain")
        for s in strategies:
            for d in directions:
                for r in regimes:
                    reg = _make_regime(regime=r)
                    c = _make_candidate("s_cell", s, d)
                    score = compute_regime_score(c, reg)
                    self.assertIsInstance(score, float)
                    self.assertTrue(0.0 <= score <= 100.0)

    def test_05_regime_score_unknown_cell_raises_validation_error(self):
        c = _make_candidate("s1", "UNKNOWN_STRAT", "BUY")
        with self.assertRaises(StrategyValidationError):
            compute_regime_score(c, self.regime)

    # --- A2: Setup Scoring (Tests 6–17) ---
    def test_06_setup_score_s05_quality_base(self):
        ev = EvidenceRef("ev1", "order_block", 95, 2000.0, details={"quality": "base"})
        c = _make_candidate("s1", "S05", evidences=(ev,))
        score, details = compute_setup_score(c, self.context)
        self.assertEqual(score, 60.0)
        self.assertEqual(details["base_score"], 60.0)

    def test_07_setup_score_s05_quality_strong(self):
        ev = EvidenceRef("ev1", "order_block", 95, 2000.0, details={"quality": "strong"})
        c = _make_candidate("s1", "S05", evidences=(ev,))
        score, details = compute_setup_score(c, self.context)
        self.assertEqual(score, 75.0)

    def test_08_setup_score_s05_quality_premium(self):
        ev = EvidenceRef("ev1", "order_block", 95, 2000.0, details={"quality": "premium_candidate"})
        c = _make_candidate("s1", "S05", evidences=(ev,))
        score, details = compute_setup_score(c, self.context)
        self.assertEqual(score, 90.0)

    def test_09_setup_score_s05_missing_quality_raises_error(self):
        ev = EvidenceRef("ev1", "order_block", 95, 2000.0, details={"quality": "invalid"})
        c = _make_candidate("s1", "S05", evidences=(ev,))
        with self.assertRaises(StrategyStateError):
            compute_setup_score(c, self.context)

    def test_10_setup_score_s01_s09_displacement_true(self):
        ev = EvidenceRef("ev1", "structure_event", 95, 2000.0, details={"displacement": True})
        c = _make_candidate("s1", "S01", evidences=(ev,))
        score, details = compute_setup_score(c, self.context)
        self.assertEqual(score, 85.0)
        self.assertTrue(details["displacement"])

    def test_11_setup_score_s01_s09_displacement_false_fvg_ge_atr14(self):
        ev_mss = EvidenceRef("ev1", "structure_event", 95, 2000.0, details={"displacement": False})
        # FVG gap = 2010.0 - 2000.0 = 10.0 >= atr14 (5.0)
        ev_fvg = EvidenceRef("ev2", "fair_value_gap", 96, 2000.0, details={"top": 2010.0, "bottom": 2000.0})
        c = _make_candidate("s1", "S01", evidences=(ev_mss, ev_fvg))
        score, details = compute_setup_score(c, self.context)
        self.assertEqual(score, 80.0)
        self.assertEqual(details["fvg_atr_basis"], "signal_bar_atr14")

    def test_12_setup_score_s01_s09_displacement_false_fvg_lt_atr14(self):
        ev_mss = EvidenceRef("ev1", "structure_event", 95, 2000.0, details={"displacement": False})
        # FVG gap = 2002.0 - 2000.0 = 2.0 < atr14 (5.0)
        ev_fvg = EvidenceRef("ev2", "fair_value_gap", 96, 2000.0, details={"top": 2002.0, "bottom": 2000.0})
        c = _make_candidate("s1", "S09", evidences=(ev_mss, ev_fvg))
        score, details = compute_setup_score(c, self.context)
        self.assertEqual(score, 65.0)

    def test_13_setup_score_bonus_clean_sweep(self):
        ev_mss = EvidenceRef("ev1", "structure_event", 95, 2000.0, details={"displacement": True})
        ev_sweep = EvidenceRef("ev2", "liquidity_sweep", 94, 1990.0, details={"sweep_type": "clean"})
        c = _make_candidate("s1", "S01", evidences=(ev_mss, ev_sweep))
        score, details = compute_setup_score(c, self.context)
        # 85.0 base + 10.0 bonus = 95.0
        self.assertEqual(score, 95.0)
        self.assertIn("clean_sweep", details["bonuses"])

    def test_14_setup_score_bonus_equal_highs_lows(self):
        ev_mss = EvidenceRef("ev1", "structure_event", 95, 2000.0, details={"displacement": True})
        ev_pool = EvidenceRef("ev2", "liquidity_pool", 93, 2020.0, details={"kind": "equal_highs"})
        c = _make_candidate("s1", "S01", evidences=(ev_mss, ev_pool))
        score, details = compute_setup_score(c, self.context)
        self.assertEqual(score, 95.0)
        self.assertIn("equal_highs_lows", details["bonuses"])

    def test_15_setup_score_bonus_fvg_ob_direct_confluence(self):
        ev_ob = EvidenceRef("ev1", "order_block", 95, 2000.0, details={"quality": "base"})
        ev_fvg = EvidenceRef("ev2", "fair_value_gap", 96, 2000.0, details={"top": 2005.0, "bottom": 1995.0})
        c = _make_candidate("s1", "S05", evidences=(ev_ob, ev_fvg))
        score, details = compute_setup_score(c, self.context)
        # S05 base=60.0 + 10.0 bonus = 70.0
        self.assertEqual(score, 70.0)
        self.assertIn("fvg_ob_confluence", details["bonuses"])

    def test_16_setup_score_all_three_bonuses_clamped_at_100(self):
        ev_ob = EvidenceRef("ev1", "order_block", 95, 2000.0, details={"quality": "premium_candidate"})  # base 90.0
        ev_fvg = EvidenceRef("ev2", "fair_value_gap", 96, 2000.0, details={"top": 2005.0, "bottom": 1995.0})  # bonus 3
        ev_sweep = EvidenceRef("ev3", "liquidity_sweep", 94, 1990.0, details={"sweep_type": "clean", "pool_kind": "equal_lows"})  # bonus 1 & 2
        c = _make_candidate("s1", "S05", evidences=(ev_ob, ev_fvg, ev_sweep))
        score, details = compute_setup_score(c, self.context)
        # 90.0 + 30.0 = 120.0 -> clamped to 100.0
        self.assertEqual(score, 100.0)
        self.assertEqual(len(details["bonuses"]), 3)

    def test_17_setup_score_bonus_applied_at_most_once_per_candidate(self):
        ev_ob = EvidenceRef("ev1", "order_block", 95, 2000.0, details={"quality": "base"})  # 60.0
        ev_sweep1 = EvidenceRef("ev2", "liquidity_sweep", 93, 1990.0, details={"sweep_type": "clean"})
        ev_sweep2 = EvidenceRef("ev3", "liquidity_sweep", 94, 1992.0, details={"sweep_type": "clean"})
        c = _make_candidate("s1", "S05", evidences=(ev_ob, ev_sweep1, ev_sweep2))
        score, details = compute_setup_score(c, self.context)
        # 60.0 + 10.0 = 70.0 (clean_sweep counted only once)
        self.assertEqual(score, 70.0)
        self.assertEqual(details["bonuses"], ["clean_sweep"])

    # --- A3: Context Scoring (Tests 18–23) ---
    def test_18_context_score_aligned_htf_bias(self):
        ctx = _make_context(bias="bullish", in_session=True)
        c = _make_candidate("s1", "S01", "BUY")
        score, details = compute_context_score(c, ctx)
        # 60.0 bias + 40.0 session = 100.0
        self.assertEqual(score, 100.0)
        self.assertEqual(details["bias_score"], 60.0)
        self.assertEqual(details["session_score"], 40.0)

    def test_19_context_score_neutral_htf_bias_s01_s09(self):
        ctx = _make_context(bias="neutral", in_session=True)
        c = _make_candidate("s1", "S01", "BUY")
        score, details = compute_context_score(c, ctx)
        # 30.0 bias + 40.0 session = 70.0
        self.assertEqual(score, 70.0)
        self.assertEqual(details["bias_score"], 30.0)

    def test_20_context_score_neutral_htf_bias_s05_raises_error(self):
        ctx = _make_context(bias="neutral", in_session=True)
        c = _make_candidate("s1", "S05", "BUY")
        with self.assertRaises(StrategyStateError):
            compute_context_score(c, ctx)

    def test_21_context_score_opposed_htf_bias_raises_error(self):
        ctx = _make_context(bias="bearish", in_session=True)
        c = _make_candidate("s1", "S01", "BUY")  # BUY opposed to bearish
        with self.assertRaises(StrategyStateError):
            compute_context_score(c, ctx)

    def test_22_context_score_session_s09_constant_40(self):
        ctx = _make_context(bias="bullish", in_session=False)
        c = _make_candidate("s1", "S09", "BUY")
        score, details = compute_context_score(c, ctx)
        # S09 session_score is 40.0 regardless of context.session_decision
        self.assertEqual(details["session_score"], 40.0)
        self.assertEqual(score, 100.0)

    def test_23_context_score_session_s01_s05_in_session_vs_outside(self):
        ctx_in = _make_context(bias="bullish", in_session=True)
        ctx_out = _make_context(bias="bullish", in_session=False)
        c = _make_candidate("s1", "S01", "BUY")
        score_in, _ = compute_context_score(c, ctx_in)
        score_out, _ = compute_context_score(c, ctx_out)
        self.assertEqual(score_in, 100.0)  # 60 + 40
        self.assertEqual(score_out, 70.0)  # 60 + 10

    # --- A4: Execution Scoring (Tests 24–25) ---
    def test_24_execution_score_interpolation_and_clamping(self):
        c_1_5 = _make_candidate("s1", "S01", planned_rr=1.5)
        c_2_0 = _make_candidate("s2", "S01", planned_rr=2.0)
        c_2_5 = _make_candidate("s3", "S01", planned_rr=2.5)
        c_3_0 = _make_candidate("s4", "S01", planned_rr=3.0)
        c_4_0 = _make_candidate("s5", "S01", planned_rr=4.0)

        s_1_5, d_1_5 = compute_execution_score(c_1_5)
        s_2_0, d_2_0 = compute_execution_score(c_2_0)
        s_2_5, d_2_5 = compute_execution_score(c_2_5)
        s_3_0, d_3_0 = compute_execution_score(c_3_0)
        s_4_0, d_4_0 = compute_execution_score(c_4_0)

        self.assertEqual(s_1_5, 30.0)
        self.assertEqual(s_2_0, 60.0)
        self.assertEqual(s_2_5, 80.0)
        self.assertEqual(s_3_0, 100.0)
        self.assertEqual(s_4_0, 100.0)
        self.assertEqual(d_1_5["execution_score_basis"], "planned_rr_pre_fill")

    def test_25_execution_score_under_1_5_raises_error(self):
        c_under = _make_candidate("s1", "S01", planned_rr=1.4)
        with self.assertRaises(StrategyStateError):
            compute_execution_score(c_under)


class TestSelectorGroupBTotalScoreWeighting(unittest.TestCase):
    """Group B: Total Score & Weighting tests (tests 26 to 45)."""

    def setUp(self):
        self.default_config = SelectorConfig()

    def test_26_default_weights_sum_to_one(self):
        c = self.default_config
        self.assertAlmostEqual(c.weight_regime + c.weight_setup + c.weight_context + c.weight_exec, 1.0)

    def test_27_default_composite_score_exact_calculation(self):
        # 0.25 * 85 + 0.35 * 85 + 0.25 * 100 + 0.15 * 60 = 21.25 + 29.75 + 25.0 + 9.0 = 85.0
        tot = compute_total_score(85.0, 85.0, 100.0, 60.0, self.default_config)
        self.assertEqual(tot, 85.0)

    def test_28_composite_score_minimum_threshold_boundary_60(self):
        # Exact 60.0 should be accepted
        tot = compute_total_score(60.0, 60.0, 60.0, 60.0, self.default_config)
        self.assertEqual(tot, 60.0)

    def test_29_composite_score_below_minimum_59_99(self):
        tot = compute_total_score(59.99, 59.99, 59.99, 59.99, self.default_config)
        self.assertEqual(tot, 59.99)

    def test_30_composite_score_zero_all_components(self):
        tot = compute_total_score(0.0, 0.0, 0.0, 0.0, self.default_config)
        self.assertEqual(tot, 0.0)

    def test_31_composite_score_max_100_all_components(self):
        tot = compute_total_score(100.0, 100.0, 100.0, 100.0, self.default_config)
        self.assertEqual(tot, 100.0)

    def test_32_composite_score_rounding_2_decimals(self):
        # 0.25 * 73.33 + 0.35 * 81.17 + 0.25 * 65.43 + 0.15 * 45.67
        # = 18.3325 + 28.4095 + 16.3575 + 6.8505 = 69.95
        tot = compute_total_score(73.33, 81.17, 65.43, 45.67, self.default_config)
        self.assertEqual(tot, 69.95)

    def test_33_composite_score_clamping_above_100(self):
        tot = compute_total_score(120.0, 120.0, 120.0, 120.0, self.default_config)
        self.assertEqual(tot, 100.0)

    def test_34_custom_weights_configuration(self):
        cfg = SelectorConfig(weight_regime=0.40, weight_setup=0.30, weight_context=0.20, weight_exec=0.10)
        tot = compute_total_score(100.0, 50.0, 50.0, 50.0, cfg)
        # 40 + 15 + 10 + 5 = 70.0
        self.assertEqual(tot, 70.0)

    def test_35_custom_minimum_total_score(self):
        cfg = SelectorConfig(minimum_total_score=75.0)
        self.assertEqual(cfg.minimum_total_score, 75.0)

    def test_36_custom_direction_conflict_gap(self):
        cfg = SelectorConfig(direction_conflict_gap=20.0)
        self.assertEqual(cfg.direction_conflict_gap, 20.0)

    def test_37_invalid_weights_sum_greater_than_one_raises_error(self):
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=0.30, weight_setup=0.35, weight_context=0.25, weight_exec=0.15)

    def test_38_invalid_weights_sum_less_than_one_raises_error(self):
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=0.20, weight_setup=0.35, weight_context=0.25, weight_exec=0.15)

    def test_39_negative_weight_raises_error(self):
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=-0.1, weight_setup=0.5, weight_context=0.3, weight_exec=0.3)

    def test_40_non_finite_weight_raises_error(self):
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=float("nan"))

    def test_41_negative_minimum_total_score_raises_error(self):
        with self.assertRaises(ValueError):
            SelectorConfig(minimum_total_score=-5.0)

    def test_42_minimum_total_score_over_100_raises_error(self):
        with self.assertRaises(ValueError):
            SelectorConfig(minimum_total_score=105.0)

    def test_43_negative_direction_conflict_gap_raises_error(self):
        with self.assertRaises(ValueError):
            SelectorConfig(direction_conflict_gap=-1.0)

    def test_44_config_to_dict_and_from_dict_roundtrip(self):
        cfg = SelectorConfig(weight_regime=0.30, weight_setup=0.30, weight_context=0.20, weight_exec=0.20, minimum_total_score=65.0, direction_conflict_gap=12.0)
        d = cfg.to_dict()
        r = SelectorConfig.from_dict(d)
        self.assertEqual(cfg, r)

    def test_45_config_from_dict_unknown_field_raises_key_error(self):
        with self.assertRaises(KeyError):
            SelectorConfig.from_dict({"unknown": 123})


class TestSelectorGroupCClusterOwnership(unittest.TestCase):
    """Group C: Cluster Ownership & Scorecard tests (tests 46 to 65)."""

    def setUp(self):
        self.context = _make_context()
        self.regime = _make_regime()

    def test_46_single_member_cluster_ownership(self):
        c = _make_candidate("s1", "S01", "BUY", cluster_id="c1", planned_rr=2.0)
        ev = _make_eval(c)
        cluster = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cluster,)
        )
        out = select_strategy(batch, self.context)
        self.assertEqual(len(out.scorecards), 1)
        sc = out.scorecards[0]
        self.assertEqual(sc.cluster_id, "c1")
        self.assertEqual(sc.primary_strategy_id, "S01")
        self.assertEqual(sc.primary_setup_id, "s1")
        self.assertEqual(sc.supporting_strategy_ids, ())
        self.assertEqual(sc.member_count, 1)

    def test_47_cluster_score_strictly_equals_primary_member_score(self):
        c1 = _make_candidate("s1", "S01", "BUY", cluster_id="c1", planned_rr=2.5)  # Higher RR -> higher exec score
        c2 = _make_candidate("s2", "S05", "BUY", cluster_id="c1", planned_rr=1.8)
        ev1 = _make_eval(c1)
        ev2 = _make_eval(c2)
        cluster = EvidenceCluster("c1", "BUY", (ev1, ev2))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev1, ev2), eligible_clusters=(cluster,)
        )
        out = select_strategy(batch, self.context)
        sc = out.scorecards[0]
        self.assertEqual(sc.primary_strategy_id, "S01")
        self.assertEqual(sc.primary_setup_id, "s1")
        self.assertEqual(sc.supporting_strategy_ids, ("S05",))
        self.assertEqual(sc.member_count, 2)

    def test_48_supporting_strategy_ids_excludes_primary_and_is_sorted(self):
        c1 = _make_candidate("s1", "S01", "BUY", cluster_id="c1", planned_rr=3.0)
        c2 = _make_candidate("s2", "S09", "BUY", cluster_id="c1", planned_rr=2.0)
        c3 = _make_candidate("s3", "S05", "BUY", cluster_id="c1", planned_rr=2.0)
        ev1 = _make_eval(c1)
        ev2 = _make_eval(c2)
        ev3 = _make_eval(c3)
        cluster = EvidenceCluster("c1", "BUY", (ev1, ev2, ev3))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev1, ev2, ev3), eligible_clusters=(cluster,)
        )
        out = select_strategy(batch, self.context)
        sc = out.scorecards[0]
        self.assertEqual(sc.primary_strategy_id, "S01")
        self.assertEqual(sc.supporting_strategy_ids, ("S05", "S09"))

    def test_49_no_multi_strategy_vote_addition(self):
        # Single member cluster with score X vs 3-member cluster with lower individual score
        c_single = _make_candidate("s_high", "S01", "BUY", cluster_id="c_high", planned_rr=3.0)
        c_multi1 = _make_candidate("s_low1", "S05", "BUY", cluster_id="c_multi", planned_rr=1.6)
        c_multi2 = _make_candidate("s_low2", "S09", "BUY", cluster_id="c_multi", planned_rr=1.6)
        ev_h = _make_eval(c_single)
        ev_m1 = _make_eval(c_multi1)
        ev_m2 = _make_eval(c_multi2)

        cl_high = EvidenceCluster("c_high", "BUY", (ev_h,))
        cl_multi = EvidenceCluster("c_multi", "BUY", (ev_m1, ev_m2))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev_h, ev_m1, ev_m2),
            eligible_clusters=(cl_high, cl_multi)
        )
        out = select_strategy(batch, self.context)
        # Higher individual score MUST win despite multi having more members
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.primary_strategy_id, "S01")
        self.assertEqual(out.decision.selected_setup.setup_id, "s_high")

    def test_50_cluster_scorecard_immutability(self):
        sc = _make_cluster_scorecard("c1", "BUY", "S01", "s1", ("S05",), 85.0, member_count=2)
        with self.assertRaises(Exception):
            sc.total_score = 90.0  # type: ignore

    def test_51_cluster_scorecard_to_dict_and_from_dict_roundtrip(self):
        sc = _make_cluster_scorecard("c1", "BUY", "S01", "s1", ("S05",), 85.0, member_count=2, details={"info": "test"})
        d = sc.to_dict()
        r = ClusterScorecard.from_dict(d)
        self.assertEqual(sc, r)

    def test_52_cluster_scorecard_json_serialization_roundtrip(self):
        sc = _make_cluster_scorecard("c1", "BUY", "S01", "s1", ("S05",), 85.0, member_count=2)
        payload = json.dumps(sc.to_dict(), allow_nan=False)
        loaded = json.loads(payload)
        r = ClusterScorecard.from_dict(loaded)
        self.assertEqual(sc, r)

    def test_53_cluster_scorecard_from_dict_unknown_field_raises_error(self):
        with self.assertRaises(KeyError):
            ClusterScorecard.from_dict({"unknown": 1})

    def test_54_cluster_scorecard_invalid_direction_raises_error(self):
        with self.assertRaises(ValueError):
            _make_cluster_scorecard("c1", "INVALID", "S01", "s1")

    def test_55_cluster_scorecard_supporting_contains_primary_raises_error(self):
        with self.assertRaises(ValueError):
            _make_cluster_scorecard("c1", "BUY", "S01", "s1", ("S01",))

    def test_56_cluster_scorecard_duplicate_supporting_raises_error(self):
        with self.assertRaises(ValueError):
            _make_cluster_scorecard("c1", "BUY", "S01", "s1", ("S05", "S05"))

    def test_57_cluster_scorecard_member_count_zero_raises_error(self):
        with self.assertRaises(ValueError):
            _make_cluster_scorecard("c1", "BUY", "S01", "s1", member_count=0)

    def test_58_cluster_scorecard_score_out_of_range_raises_error(self):
        with self.assertRaises(ValueError):
            _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=105.0)

    def test_59_cluster_scorecard_negative_planned_rr_raises_error(self):
        with self.assertRaises(ValueError):
            _make_cluster_scorecard("c1", "BUY", "S01", "s1", planned_rr=-1.0)

    def test_60_cluster_scorecard_details_frozen(self):
        sc = _make_cluster_scorecard("c1", "BUY", "S01", "s1", details={"a": 1})
        with self.assertRaises(TypeError):
            sc.details["a"] = 2  # type: ignore

    def test_61_selector_output_fields(self):
        c = _make_candidate("s1", "S01", "BUY", planned_rr=2.0)
        ev = _make_eval(c)
        cluster = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cluster,)
        )
        out = select_strategy(batch, self.context)
        self.assertIsInstance(out, SelectorOutput)
        self.assertIsNotNone(out.decision)
        self.assertIsNotNone(out.audit_record)
        self.assertEqual(len(out.scorecards), 1)

    def test_62_deterministic_strategy_selector_class_property(self):
        cfg = SelectorConfig(minimum_total_score=70.0)
        selector = DeterministicStrategySelector(cfg)
        self.assertEqual(selector.config.minimum_total_score, 70.0)

    def test_63_select_with_default_selector_instance(self):
        selector = DeterministicStrategySelector()
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c)
        cluster = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cluster,)
        )
        out = selector.select(batch, self.context)
        self.assertEqual(out.decision.action, "SELECT")

    def test_64_context_mismatch_bar_index_raises_error(self):
        ctx = _make_context(bar_index=101)
        c = _make_candidate("s1", "S01", "BUY", bar_index=100)
        ev = _make_eval(c)
        cluster = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cluster,)
        )
        with self.assertRaises(ValueError):
            select_strategy(batch, ctx)

    def test_65_context_mismatch_timestamp_raises_error(self):
        ctx = _make_context(timestamp="2026-03-09 16:00:00+00:00", bar_close_time="2026-03-09 16:15:00+00:00")
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c)
        cluster = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cluster,)
        )
        with self.assertRaises(ValueError):
            select_strategy(batch, ctx)


class TestSelectorGroupDDirectionConflict(unittest.TestCase):
    """Group D: Direction Conflict & Score Gap tests (tests 66 to 85)."""

    def setUp(self):
        self.context = _make_context()
        self.regime = _make_regime()

    def test_66_direction_conflict_gap_under_15_returns_no_trade(self):
        # BUY candidate with high planned_rr vs SELL candidate with slightly lower planned_rr
        # Both in ranging regime to have non-zero regime scores
        reg = _make_regime(regime="ranging")
        # In ranging: S01 BUY is 60.0, S01 SELL is 60.0
        # Give BUY planned_rr=2.0 (exec=60.0), SELL planned_rr=1.8 (exec=48.0)
        # Both have neutral bias context = 30 + 40 = 70.0
        ctx = _make_context(bias="neutral", in_session=True)
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=2.0)
        c_sell = _make_candidate("s1", "S01", "SELL", cluster_id="c_s", planned_rr=1.8, entry=2000.0, sl=2010.0, tp=1980.0)

        ev_b = _make_eval(c_buy, regime=reg.regime)
        ev_s = _make_eval(c_sell, regime=reg.regime)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))
        cl_s = EvidenceCluster("c_s", "SELL", (ev_s,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b, ev_s), eligible_clusters=(cl_b, cl_s),
            direction_conflict=DirectionConflict(100, ("c_b",), ("c_s",)),
        )
        out = select_strategy(batch, ctx)
        self.assertEqual(out.decision.action, "NO_TRADE")
        self.assertEqual(out.decision.reason, "conflicting_direction")
        self.assertIsNotNone(out.decision.score_gap)
        self.assertTrue(out.decision.score_gap < 15.0)
        self.assertIsNone(out.decision.selected_setup)
        self.assertIsNone(out.decision.primary_strategy_id)

    def test_67_direction_conflict_gap_exact_15_selects_winner(self):
        # Craft two clusters with exact score gap = 15.0
        # In custom config, test exact 15.0 gap threshold
        reg = _make_regime(regime="ranging")
        ctx = _make_context(bias="neutral", in_session=True)
        # BUY has higher score
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=3.0)  # exec = 100.0
        c_sell = _make_candidate("s1", "S01", "SELL", cluster_id="c_s", planned_rr=1.5, entry=2000.0, sl=2010.0, tp=1985.0)  # exec = 30.0

        ev_b = _make_eval(c_buy, regime=reg.regime)
        ev_s = _make_eval(c_sell, regime=reg.regime)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))
        cl_s = EvidenceCluster("c_s", "SELL", (ev_s,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b, ev_s), eligible_clusters=(cl_b, cl_s),
            direction_conflict=DirectionConflict(100, ("c_b",), ("c_s",)),
        )
        # Custom config where gap threshold is set to exact difference between them
        sc_b_tot = compute_total_score(60.0, 65.0, 70.0, 100.0, SelectorConfig())
        sc_s_tot = compute_total_score(60.0, 65.0, 70.0, 30.0, SelectorConfig())
        diff = round(sc_b_tot - sc_s_tot, 2)  # 0.15 * 70 = 10.5

        cfg = SelectorConfig(direction_conflict_gap=diff)
        out = select_strategy(batch, ctx, cfg)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.reason, "ok")
        self.assertEqual(out.decision.primary_strategy_id, "S01")
        self.assertEqual(out.decision.selected_setup.setup_id, "b1")
        self.assertEqual(out.decision.score_gap, diff)

    def test_68_direction_conflict_gap_greater_than_15_selects_winner(self):
        # BUY in bullish_trend: regime=100, bias=aligned(60)+session(40)=100, exec=100 -> total=94.75
        # SELL in bullish_trend: regime=0.0 (wrong regime for S05, but S01 is 0.0 in bullish_trend)
        # For S09 in volatile_reversal: BUY=100, SELL=100
        # Let's use custom weights to create gap > 15
        reg = _make_regime(regime="ranging")
        ctx = _make_context(bias="neutral", in_session=True)
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=3.0)
        c_sell = _make_candidate("s1", "S01", "SELL", cluster_id="c_s", planned_rr=1.5, entry=2000.0, sl=2010.0, tp=1985.0)

        ev_b = _make_eval(c_buy, regime=reg.regime)
        ev_s = _make_eval(c_sell, regime=reg.regime)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))
        cl_s = EvidenceCluster("c_s", "SELL", (ev_s,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b, ev_s), eligible_clusters=(cl_b, cl_s),
            direction_conflict=DirectionConflict(100, ("c_b",), ("c_s",)),
        )
        cfg = SelectorConfig(direction_conflict_gap=5.0)  # gap is ~10.5 > 5.0
        out = select_strategy(batch, ctx, cfg)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.primary_strategy_id, "S01")
        self.assertEqual(out.decision.selected_setup.setup_id, "b1")

    def test_69_conflict_gap_evaluated_before_minimum_score_filter(self):
        # Both BUY and SELL have scores < 60.0, but gap < 15.0 -> reason MUST be conflicting_direction
        reg = _make_regime(regime="uncertain")  # 30.0 regime score
        ctx = _make_context(bias="neutral", in_session=False)  # 30 + 10 = 40.0 context
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=1.5)  # 30.0 exec
        c_sell = _make_candidate("s1", "S01", "SELL", cluster_id="c_s", planned_rr=1.5, entry=2000.0, sl=2010.0, tp=1985.0)

        ev_b = _make_eval(c_buy, regime=reg.regime)
        ev_s = _make_eval(c_sell, regime=reg.regime)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))
        cl_s = EvidenceCluster("c_s", "SELL", (ev_s,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b, ev_s), eligible_clusters=(cl_b, cl_s),
            direction_conflict=DirectionConflict(100, ("c_b",), ("c_s",)),
        )
        out = select_strategy(batch, ctx)
        self.assertEqual(out.decision.action, "NO_TRADE")
        self.assertEqual(out.decision.reason, "conflicting_direction")

    def test_70_gap_ge_15_but_winner_under_60_returns_insufficient_score(self):
        reg = _make_regime(regime="uncertain")  # low regime
        ctx = _make_context(bias="neutral", in_session=False)
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=2.0)
        c_sell = _make_candidate("s1", "S01", "SELL", cluster_id="c_s", planned_rr=1.5, entry=2000.0, sl=2010.0, tp=1985.0)

        ev_b = _make_eval(c_buy, regime=reg.regime)
        ev_s = _make_eval(c_sell, regime=reg.regime)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))
        cl_s = EvidenceCluster("c_s", "SELL", (ev_s,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b, ev_s), eligible_clusters=(cl_b, cl_s),
            direction_conflict=DirectionConflict(100, ("c_b",), ("c_s",)),
        )
        # Force gap threshold small (4.0) so gap (4.5) passes, but winner score (54.5) is still < 60
        cfg = SelectorConfig(direction_conflict_gap=4.0, minimum_total_score=60.0)
        out = select_strategy(batch, ctx, cfg)
        self.assertEqual(out.decision.action, "NO_TRADE")
        self.assertEqual(out.decision.reason, "insufficient_score")
        self.assertIsNone(out.decision.score_gap)

    def test_71_single_direction_score_under_60_returns_insufficient_score(self):
        reg = _make_regime(regime="uncertain")
        ctx = _make_context(bias="neutral", in_session=False)
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=1.5)
        ev_b = _make_eval(c_buy, regime=reg.regime)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b,), eligible_clusters=(cl_b,)
        )
        out = select_strategy(batch, ctx)
        self.assertEqual(out.decision.action, "NO_TRADE")
        self.assertEqual(out.decision.reason, "insufficient_score")
        self.assertIsNone(out.decision.score_gap)

    def test_72_single_direction_score_ge_60_returns_select(self):
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=2.0)
        ev_b = _make_eval(c_buy)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev_b,), eligible_clusters=(cl_b,)
        )
        out = select_strategy(batch, self.context)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.reason, "ok")
        self.assertEqual(out.decision.selected_setup.setup_id, "b1")

    def test_73_no_eligible_clusters_returns_no_eligible_setup(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c, status="REJECTED", rejection_reasons=("wrong_regime",))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=()
        )
        out = select_strategy(batch, self.context)
        self.assertEqual(out.decision.action, "NO_TRADE")
        self.assertEqual(out.decision.reason, "no_eligible_setup")
        self.assertEqual(out.decision.decision_id, "sel:100:NO_TRADE:none")
        self.assertEqual(len(out.scorecards), 0)

    def test_74_multiple_clusters_same_direction_picks_best(self):
        c1 = _make_candidate("b1", "S01", "BUY", cluster_id="c1", planned_rr=2.0)
        c2 = _make_candidate("b2", "S01", "BUY", cluster_id="c2", planned_rr=2.5)  # Higher RR -> higher score
        ev1 = _make_eval(c1)
        ev2 = _make_eval(c2)
        cl1 = EvidenceCluster("c1", "BUY", (ev1,))
        cl2 = EvidenceCluster("c2", "BUY", (ev2,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev1, ev2), eligible_clusters=(cl1, cl2)
        )
        out = select_strategy(batch, self.context)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.selected_setup.setup_id, "b2")

    def test_75_execution_payload_strictly_empty_in_t53_8(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cl,)
        )
        out = select_strategy(batch, self.context)
        self.assertEqual(dict(out.decision.execution_payload), {})

    def test_76_meta_contains_winner_cluster_id_on_select(self):
        c = _make_candidate("s1", "S01", "BUY", cluster_id="c1")
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cl,)
        )
        out = select_strategy(batch, self.context)
        self.assertEqual(out.decision.meta.get("winner_cluster_id"), "c1")

    def test_77_meta_winner_cluster_id_is_none_on_no_trade(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c, status="REJECTED", rejection_reasons=("wrong_regime",))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=()
        )
        out = select_strategy(batch, self.context)
        self.assertIsNone(out.decision.meta.get("winner_cluster_id"))

    def test_78_decision_id_format_select(self):
        ts_open = "2026-03-09 15:00:00+00:00"
        ts_close = "2026-03-09 15:15:00+00:00"
        c = _make_candidate("s1", "S01", "BUY", bar_index=45, timestamp=ts_open)
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=45, timestamp=pd.Timestamp(ts_close),
            regime=_make_regime(bar_index=45, timestamp=ts_close), evaluations=(ev,), eligible_clusters=(cl,)
        )
        ctx = _make_context(bar_index=45, timestamp=ts_open, bar_close_time=ts_close)
        out = select_strategy(batch, ctx)
        self.assertEqual(out.decision.decision_id, "sel:45:SELECT:S01")

    def test_79_decision_id_format_no_trade(self):
        ts_open = "2026-03-09 15:00:00+00:00"
        ts_close = "2026-03-09 15:15:00+00:00"
        c = _make_candidate("s1", "S01", "BUY", bar_index=45, timestamp=ts_open)
        ev = _make_eval(c, status="REJECTED", rejection_reasons=("wrong_regime",))
        batch = ConfluenceBatch(
            bar_index=45, timestamp=pd.Timestamp(ts_close),
            regime=_make_regime(bar_index=45, timestamp=ts_close), evaluations=(ev,), eligible_clusters=()
        )
        ctx = _make_context(bar_index=45, timestamp=ts_open, bar_close_time=ts_close)
        out = select_strategy(batch, ctx)
        self.assertEqual(out.decision.decision_id, "sel:45:NO_TRADE:none")

    def test_80_final_evaluations_contain_computed_scores(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=(cl,)
        )
        out = select_strategy(batch, self.context)
        dec_eval = out.decision.evaluations[0]
        self.assertGreater(dec_eval.total_score, 0.0)
        self.assertGreater(dec_eval.regime_score, 0.0)
        self.assertGreater(dec_eval.setup_score, 0.0)
        self.assertGreater(dec_eval.context_score, 0.0)
        self.assertGreater(dec_eval.exec_score, 0.0)

    def test_81_final_evaluations_preserve_rejected_setups_with_zero_scores(self):
        c1 = _make_candidate("s1", "S01", "BUY", cluster_id="c1")
        c2 = _make_candidate("s2", "S05", "BUY", cluster_id="c2")
        ev1 = _make_eval(c1, status="ELIGIBLE")
        ev2 = _make_eval(c2, status="REJECTED", rejection_reasons=("outside_session",))
        cl1 = EvidenceCluster("c1", "BUY", (ev1,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev1, ev2), eligible_clusters=(cl1,)
        )
        out = select_strategy(batch, self.context)
        self.assertEqual(len(out.decision.evaluations), 2)
        rej_eval = next(e for e in out.decision.evaluations if e.candidate.setup_id == "s2")
        self.assertEqual(rej_eval.status, "REJECTED")
        self.assertEqual(rej_eval.total_score, 0.0)
        self.assertEqual(rej_eval.rejection_reasons, ("outside_session",))

    def test_82_score_gap_rounded_to_2_decimals(self):
        # In conflict scenario, score_gap must have 2 decimal precision
        reg = _make_regime(regime="ranging")
        ctx = _make_context(bias="neutral", in_session=True)
        c_buy = _make_candidate("b1", "S01", "BUY", cluster_id="c_b", planned_rr=2.13)
        c_sell = _make_candidate("s1", "S01", "SELL", cluster_id="c_s", planned_rr=1.87, entry=2000.0, sl=2010.0, tp=1980.0)

        ev_b = _make_eval(c_buy, regime=reg.regime)
        ev_s = _make_eval(c_sell, regime=reg.regime)
        cl_b = EvidenceCluster("c_b", "BUY", (ev_b,))
        cl_s = EvidenceCluster("c_s", "SELL", (ev_s,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b, ev_s), eligible_clusters=(cl_b, cl_s),
            direction_conflict=DirectionConflict(100, ("c_b",), ("c_s",)),
        )
        out = select_strategy(batch, ctx)
        gap = out.decision.score_gap
        self.assertIsNotNone(gap)
        self.assertEqual(gap, round(gap, 2))

    def test_83_both_directions_multiple_clusters_best_buy_vs_best_sell(self):
        reg = _make_regime(regime="ranging")
        ctx = _make_context(bias="neutral", in_session=True)

        c_b1 = _make_candidate("b1", "S01", "BUY", cluster_id="cb1", planned_rr=1.8)
        c_b2 = _make_candidate("b2", "S01", "BUY", cluster_id="cb2", planned_rr=3.0)  # best BUY
        c_s1 = _make_candidate("s1", "S01", "SELL", cluster_id="cs1", planned_rr=1.5, entry=2000.0, sl=2010.0, tp=1985.0)
        c_s2 = _make_candidate("s2", "S01", "SELL", cluster_id="cs2", planned_rr=1.6, entry=2000.0, sl=2010.0, tp=1984.0)

        ev_b1, ev_b2 = _make_eval(c_b1, regime="ranging"), _make_eval(c_b2, regime="ranging")
        ev_s1, ev_s2 = _make_eval(c_s1, regime="ranging"), _make_eval(c_s2, regime="ranging")

        cl_b1 = EvidenceCluster("cb1", "BUY", (ev_b1,))
        cl_b2 = EvidenceCluster("cb2", "BUY", (ev_b2,))
        cl_s1 = EvidenceCluster("cs1", "SELL", (ev_s1,))
        cl_s2 = EvidenceCluster("cs2", "SELL", (ev_s2,))

        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg, evaluations=(ev_b1, ev_b2, ev_s1, ev_s2),
            eligible_clusters=(cl_b1, cl_b2, cl_s1, cl_s2),
            direction_conflict=DirectionConflict(100, ("cb1", "cb2"), ("cs1", "cs2")),
        )
        out = select_strategy(batch, ctx)
        # Gap between best BUY (b2) and best SELL (s2) is evaluated
        # If gap < 15 -> NO_TRADE
        self.assertEqual(out.decision.action, "NO_TRADE")
        self.assertEqual(out.decision.reason, "conflicting_direction")

    def test_84_invalid_batch_type_raises_strict_type_error(self):
        with self.assertRaises(StrictModelTypeError):
            select_strategy("not_a_batch", self.context)  # type: ignore

    def test_85_invalid_context_type_raises_strict_type_error(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c, status="REJECTED", rejection_reasons=("wrong_regime",))
        batch = ConfluenceBatch(
            bar_index=100, timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime, evaluations=(ev,), eligible_clusters=()
        )
        with self.assertRaises(StrictModelTypeError):
            select_strategy(batch, "not_a_context")  # type: ignore


class TestSelectorGroupECanonicalTieBreaking(unittest.TestCase):
    """Group E: Canonical Tie-Breaking tests (tests 86 to 105)."""

    def test_86_member_key_total_score_decides_first(self):
        c1 = _make_candidate("s1", "S01")
        c2 = _make_candidate("s2", "S01")
        e1 = _make_eval(c1, total_score=85.0)
        e2 = _make_eval(c2, total_score=80.0)
        # e1 has higher total score -> smaller negative -> sorted first
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_87_member_key_setup_score_decides_second(self):
        c1 = _make_candidate("s1", "S01")
        c2 = _make_candidate("s2", "S01")
        e1 = _make_eval(c1, total_score=80.0, setup_score=85.0)
        e2 = _make_eval(c2, total_score=80.0, setup_score=75.0)
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_88_member_key_context_score_decides_third(self):
        c1 = _make_candidate("s1", "S01")
        c2 = _make_candidate("s2", "S01")
        e1 = _make_eval(c1, total_score=80.0, setup_score=80.0, context_score=90.0)
        e2 = _make_eval(c2, total_score=80.0, setup_score=80.0, context_score=80.0)
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_89_member_key_exec_score_decides_fourth(self):
        c1 = _make_candidate("s1", "S01")
        c2 = _make_candidate("s2", "S01")
        e1 = _make_eval(c1, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0)
        e2 = _make_eval(c2, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=60.0)
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_90_member_key_regime_score_decides_fifth(self):
        c1 = _make_candidate("s1", "S01")
        c2 = _make_candidate("s2", "S01")
        e1 = _make_eval(c1, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=90.0)
        e2 = _make_eval(c2, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=80.0)
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_91_member_key_planned_rr_decides_sixth(self):
        c1 = _make_candidate("s1", "S01", planned_rr=2.5)
        c2 = _make_candidate("s2", "S01", planned_rr=2.0)
        e1 = _make_eval(c1, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=80.0)
        e2 = _make_eval(c2, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=80.0)
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_92_member_key_strategy_id_decides_seventh_lexicographical(self):
        # S01 < S05 < S09
        c1 = _make_candidate("s1", "S01", planned_rr=2.0)
        c2 = _make_candidate("s2", "S05", planned_rr=2.0)
        e1 = _make_eval(c1, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=80.0)
        e2 = _make_eval(c2, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=80.0)
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_93_member_key_setup_id_decides_eighth_lexicographical(self):
        c1 = _make_candidate("setup_a", "S01", planned_rr=2.0)
        c2 = _make_candidate("setup_b", "S01", planned_rr=2.0)
        e1 = _make_eval(c1, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=80.0)
        e2 = _make_eval(c2, total_score=80.0, setup_score=80.0, context_score=80.0, exec_score=70.0, regime_score=80.0)
        self.assertTrue(member_rank_key(e1) < member_rank_key(e2))

    def test_94_cluster_key_total_score_decides_first(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=85.0)
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s2", total_score=80.0)
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_95_cluster_key_setup_score_decides_second(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=80.0, setup_score=85.0, exec_score=75.0, scoring_weights=(0.0, 0.5, 0.0, 0.5))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s2", total_score=80.0, setup_score=75.0, exec_score=85.0, scoring_weights=(0.0, 0.5, 0.0, 0.5))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_96_cluster_key_context_score_decides_third(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=80.0, setup_score=80.0, context_score=100.0, exec_score=60.0, scoring_weights=(0.0, 0.0, 0.5, 0.5))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s2", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=70.0, scoring_weights=(0.0, 0.0, 0.5, 0.5))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_97_cluster_key_exec_score_decides_fourth(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=70.0, regime_score=90.0, scoring_weights=(0.5, 0.0, 0.0, 0.5))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s2", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=100.0, scoring_weights=(0.5, 0.0, 0.0, 0.5))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_98_cluster_key_regime_score_decides_fifth(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=100.0, scoring_weights=(0.5, 0.0, 0.0, 0.5))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s2", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_99_cluster_key_planned_rr_decides_sixth(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, planned_rr=2.5, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s2", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, planned_rr=2.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_100_cluster_key_primary_strategy_id_decides_seventh(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S05", "s1", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_101_cluster_key_primary_setup_id_decides_eighth(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "setup_a", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "setup_b", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_102_cluster_key_cluster_id_decides_ninth(self):
        sc1 = _make_cluster_scorecard("cluster_a", "BUY", "S01", "setup_a", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        sc2 = _make_cluster_scorecard("cluster_b", "BUY", "S01", "setup_a", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))

    def test_103_tie_breaking_order_is_strict_total_ordering(self):
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s1", total_score=80.0, setup_score=80.0, context_score=90.0, exec_score=60.0, regime_score=90.0, scoring_weights=(2/3, 0.0, 0.0, 1/3))
        # c1 < c2 because 'c1' < 'c2'
        self.assertTrue(cluster_rank_key(sc1) < cluster_rank_key(sc2))
        self.assertFalse(cluster_rank_key(sc2) < cluster_rank_key(sc1))

    def test_104_member_rank_key_deterministic_reproducibility(self):
        c = _make_candidate("s1", "S01", planned_rr=2.0)
        e = _make_eval(c, total_score=85.0, setup_score=80.0, context_score=100.0, exec_score=60.0, regime_score=100.0)
        k1 = member_rank_key(e)
        k2 = member_rank_key(e)
        self.assertEqual(k1, k2)

    def test_105_cluster_rank_key_deterministic_reproducibility(self):
        sc = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=85.0)
        k1 = cluster_rank_key(sc)
        k2 = cluster_rank_key(sc)
        self.assertEqual(k1, k2)




class TestSelectorQCRegression(unittest.TestCase):
    """
    QC Hardening regression tests for T53.8:
    1. Timestamp synchronization with production contract
    2. Regime-score strict integrity for ELIGIBLE evaluations
    3. SelectionDecision hardening (decision_id, regime sync, evaluations uniqueness, SELECT/NO_TRADE rules)
    4. Strict SelectorOutput validation & JSON roundtrip
    """

    def setUp(self):
        self.context = _make_context(
            bar_index=100,
            timestamp="2026-03-09 15:00:00+00:00",
            bar_close_time="2026-03-09 15:15:00+00:00",
        )
        self.regime = _make_regime(
            bar_index=100,
            timestamp="2026-03-09 15:15:00+00:00",
            regime="bullish_trend",
        )
        self.selector = DeterministicStrategySelector()

    # --- Section 1: Preflight Timestamp Contract ---
    def test_qc_preflight_valid_open_close_timestamp_passes(self):
        c = _make_candidate("s1", "S01", "BUY", bar_index=100, timestamp="2026-03-09 15:00:00+00:00")
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        out = self.selector.select(batch, self.context)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.timestamp, pd.Timestamp("2026-03-09 15:15:00+00:00"))

    def test_qc_preflight_batch_timestamp_open_time_rejected(self):
        c = _make_candidate("s1", "S01", "BUY", bar_index=100, timestamp="2026-03-09 15:00:00+00:00")
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        reg = _make_regime(bar_index=100, timestamp="2026-03-09 15:00:00+00:00")
        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"),
            regime=reg,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        with self.assertRaises(ValueError) as cm:
            self.selector.select(batch, self.context)
        self.assertIn("does not match context.bar_close_time", str(cm.exception))

    def test_qc_preflight_regime_timestamp_mismatch_rejected(self):
        reg_bad = MarketRegime("bullish_trend", 100, pd.Timestamp("2026-03-09 15:00:00+00:00"), 0.8, 50.0)
        c = _make_candidate("s1", "S01", "BUY", bar_index=100, timestamp="2026-03-09 15:00:00+00:00")
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        with self.assertRaises(ValueError):
            ConfluenceBatch(
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                regime=reg_bad,
                evaluations=(ev,),
                eligible_clusters=(cl,),
            )

    def test_qc_preflight_bar_index_mismatch_rejected(self):
        c = _make_candidate("s1", "S01", "BUY", bar_index=101)
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        reg = _make_regime(bar_index=101, timestamp="2026-03-09 15:15:00+00:00")
        batch = ConfluenceBatch(
            bar_index=101,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=reg,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        with self.assertRaises(ValueError) as cm:
            self.selector.select(batch, self.context)
        self.assertIn("does not match context.bar_index", str(cm.exception))

    # --- Section 2: Regime-Score Strict Integrity ---
    def test_qc_regime_score_eligible_zero_gate_score_rejected(self):
        c = _make_candidate("s1", "S05", "BUY")
        ev = _make_eval(c, status="ELIGIBLE", regime_score=0.0)
        with self.assertRaises(StrategyStateError) as cm:
            compute_regime_score(c, self.regime, ev)
        self.assertIn("Regime score mismatch", str(cm.exception))

    def test_qc_regime_score_eligible_off_by_one_rejected(self):
        c = _make_candidate("s1", "S05", "BUY")
        ev = _make_eval(c, status="ELIGIBLE", regime_score=99.0)
        with self.assertRaises(StrategyStateError):
            compute_regime_score(c, self.regime, ev)

    def test_qc_regime_score_eligible_exact_match_passes(self):
        c = _make_candidate("s1", "S05", "BUY")
        ev = _make_eval(c, status="ELIGIBLE", regime_score=100.0)
        score = compute_regime_score(c, self.regime, ev)
        self.assertEqual(score, 100.0)

    def test_qc_regime_score_rejected_not_promoted(self):
        c = _make_candidate("s1", "S05", "BUY")
        ev = _make_eval(c, status="REJECTED", regime_score=0.0, rejection_reasons=("wrong_regime",))
        score = compute_regime_score(c, self.regime, ev)
        self.assertEqual(score, 100.0)
        self.assertEqual(ev.status, "REJECTED")

    # --- Section 3: SelectionDecision Hardening ---
    def test_qc_decision_forged_id_rejected(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c, total_score=85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionDecision(
                decision_id="forged:100:SELECT:S01",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="SELECT",
                selected_setup=c,
                primary_strategy_id="S01",
                evaluations=(ev,),
                regime=self.regime,
            )
        self.assertIn("does not match canonical", str(cm.exception))

    def test_qc_decision_regime_sync_bar_index_mismatch_rejected(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c, total_score=85.0)
        reg_bad = _make_regime(bar_index=99, timestamp="2026-03-09 15:15:00+00:00")
        with self.assertRaises(ValueError):
            SelectionDecision(
                decision_id="sel:100:SELECT:S01",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="SELECT",
                selected_setup=c,
                primary_strategy_id="S01",
                evaluations=(ev,),
                regime=reg_bad,
            )

    def test_qc_decision_duplicate_eval_setup_id_rejected(self):
        c1 = _make_candidate("s1", "S01", "BUY")
        c2 = _make_candidate("s1", "S01", "BUY")
        ev1 = _make_eval(c1, total_score=85.0)
        ev2 = _make_eval(c2, total_score=80.0)
        with self.assertRaises(ValueError) as cm:
            SelectionDecision(
                decision_id="sel:100:SELECT:S01",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="SELECT",
                selected_setup=c1,
                primary_strategy_id="S01",
                evaluations=(ev1, ev2),
                regime=self.regime,
            )
        self.assertIn("Duplicate candidate setup_id", str(cm.exception))

    def test_qc_decision_select_missing_selected_setup_in_evaluations_rejected(self):
        c1 = _make_candidate("s1", "S01", "BUY")
        c_other = _make_candidate("s2", "S05", "BUY")
        ev_other = _make_eval(c_other, total_score=85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionDecision(
                decision_id="sel:100:SELECT:S01",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="SELECT",
                selected_setup=c1,
                primary_strategy_id="S01",
                evaluations=(ev_other,),
                regime=self.regime,
            )
        self.assertIn("must appear exactly once in evaluations", str(cm.exception))

    def test_qc_decision_select_selected_setup_rejected_status_rejected(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c, status="REJECTED", rejection_reasons=("wrong_regime",))
        with self.assertRaises(ValueError) as cm:
            SelectionDecision(
                decision_id="sel:100:SELECT:S01",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="SELECT",
                selected_setup=c,
                primary_strategy_id="S01",
                evaluations=(ev,),
                regime=self.regime,
            )
        self.assertIn("Selected setup cannot be marked REJECTED", str(cm.exception))

    def test_qc_decision_no_trade_with_supporting_strategies_rejected(self):
        with self.assertRaises(ValueError) as cm:
            SelectionDecision(
                decision_id="sel:100:NO_TRADE:none",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="NO_TRADE",
                reason="no_eligible_setup",
                supporting_strategy_ids=("S05",),
                regime=self.regime,
            )
        self.assertIn("cannot have supporting_strategy_ids", str(cm.exception))

    def test_qc_decision_no_trade_with_reason_ok_rejected(self):
        with self.assertRaises(ValueError) as cm:
            SelectionDecision(
                decision_id="sel:100:NO_TRADE:none",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="NO_TRADE",
                reason="ok",
                regime=self.regime,
            )
        self.assertIn("cannot have reason='ok'", str(cm.exception))

    def test_qc_decision_non_empty_execution_payload_rejected(self):
        with self.assertRaises(ValueError) as cm:
            SelectionDecision(
                decision_id="sel:100:NO_TRADE:none",
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="NO_TRADE",
                reason="no_eligible_setup",
                execution_payload={"signal": 0},
                regime=self.regime,
            )
        self.assertIn("execution_payload must be empty mapping", str(cm.exception))

    # --- Section 4: Strict SelectorOutput & JSON Roundtrip ---
    def test_qc_selector_output_cross_field_decision_id_mismatch_rejected(self):
        c = _make_candidate("s1", "S01", "BUY")
        ev = _make_eval(c, total_score=85.0)
        dec = SelectionDecision(
            decision_id="sel:100:SELECT:S01",
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            selected_setup=c,
            primary_strategy_id="S01",
            evaluations=(ev,),
            regime=self.regime,
        )
        sc = _make_cluster_scorecard("c1", "BUY", "S05", "s1", total_score=85.0)
        aud = SelectionAuditRecord(
            record_version="1.0.0",
            selector_version="selector-v1",
            minimum_total_score=60.0,
            minimum_direction_gap=15.0,
            scoring_weights=(0.25, 0.35, 0.25, 0.15),
            symbol="EURUSD",
            timeframe="15m",
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            decision_id="sel:100:SELECT:S05",
            action="SELECT",
            reason="ok",
            primary_strategy_id="S05",
            primary_setup_id="s1",
            direction="BUY",
            total_score=85.0,
            cluster_id="c1",
            direction_conflict_present=False,
            best_buy_score=85.0,
            best_sell_score=None,
            cluster_scorecards=(sc,),
            evaluated_count=1,
            eligible_count=1,
            rejected_count=0,
            regime="bullish_trend",
        )
        with self.assertRaises(ValueError) as cm:
            SelectorOutput(decision=dec, audit_record=aud, scorecards=(sc,))
        self.assertIn("decision_id mismatch", str(cm.exception))

    def test_qc_selector_output_winning_cluster_mismatch_rejected(self):
        c1 = _make_candidate("s1", "S01", "BUY")
        c2 = _make_candidate("s2", "S05", "BUY")
        ev1 = _make_eval(c1, regime_score=85.0, setup_score=85.0, context_score=85.0, exec_score=85.0, total_score=85.0)
        ev2 = _make_eval(c2, regime_score=80.0, setup_score=80.0, context_score=80.0, exec_score=80.0, total_score=80.0)
        meta = {
            "selector_version": "selector-v1",
            "minimum_total_score": 60.0,
            "minimum_direction_gap": 15.0,
            "scoring_weights": [0.25, 0.35, 0.25, 0.15],
            "execution_score_basis": "planned_rr_pre_fill",
            "fvg_atr_basis": "signal_bar_atr14",
            "symbol": "EURUSD",
            "timeframe": "15m",
            "winner_cluster_id": "c1",
        }
        dec = SelectionDecision(
            decision_id="sel:100:SELECT:S01",
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            selected_setup=c1,
            primary_strategy_id="S01",
            evaluations=(ev1, ev2),
            regime=self.regime,
            meta=meta,
        )
        sc1 = _make_cluster_scorecard("c1", "BUY", "S01", "s1", total_score=85.0)
        # sc2 claims primary_strategy_id is S01, but setup s2 was evaluated as S05
        sc2 = _make_cluster_scorecard("c2", "BUY", "S01", "s2", total_score=80.0)
        aud = SelectionAuditRecord(
            record_version="1.0.0",
            selector_version="selector-v1",
            minimum_total_score=60.0,
            minimum_direction_gap=15.0,
            scoring_weights=(0.25, 0.35, 0.25, 0.15),
            symbol="EURUSD",
            timeframe="15m",
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            decision_id="sel:100:SELECT:S01",
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            primary_setup_id="s1",
            direction="BUY",
            total_score=85.0,
            cluster_id="c1",
            direction_conflict_present=False,
            best_buy_score=85.0,
            best_sell_score=None,
            cluster_scorecards=(sc1, sc2),
            evaluated_count=2,
            eligible_count=2,
            rejected_count=0,
            regime="bullish_trend",
            meta=meta,
        )
        with self.assertRaises(ValueError) as cm:
            SelectorOutput(decision=dec, audit_record=aud, scorecards=(sc1, sc2))
        self.assertIn("primary_strategy_id", str(cm.exception))

    def test_qc_selector_output_exact_json_roundtrip(self):
        c = _make_candidate("s1", "S01", "BUY", planned_rr=2.5)
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        output = self.selector.select(batch, self.context)
        payload = json.loads(json.dumps(output.to_dict(), allow_nan=False))
        restored = SelectorOutput.from_dict(payload)
        self.assertEqual(restored.to_dict(), output.to_dict())

    def test_qc_selector_output_unknown_fields_rejected(self):
        c = _make_candidate("s1", "S01", "BUY", planned_rr=2.5)
        ev = _make_eval(c)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            regime=self.regime,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        output = self.selector.select(batch, self.context)
        d = output.to_dict()
        d["unknown_field"] = 123
        with self.assertRaises(KeyError):
            SelectorOutput.from_dict(d)


if __name__ == "__main__":
    unittest.main()
