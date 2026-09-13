"""
tests.test_smc_engine_t53_8_tamper_probes
=========================================
Dedicated fail-closed tamper probe suite for T53.8.
Validates 20 specific invariant probes across constructors, from_dict(),
aggregate_selection_telemetry(), and SelectorOutput cross-validation.
"""

from __future__ import annotations

import math
import unittest
from collections import Counter
from typing import Any
import pandas as pd

from smc.engine.confluence import ConfluenceBatch, EvidenceCluster
from smc.engine.context import StrategyContext
from smc.engine.errors import StrictModelTypeError
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    MarketRegime,
    SelectionDecision,
    SessionDecisionSnapshot,
    StrategyEvaluation,
    make_decision_id,
)
from smc.engine.selector import (
    ClusterScorecard,
    DeterministicStrategySelector,
    SelectorConfig,
    SelectorOutput,
    cluster_rank_key,
    member_rank_key,
)
from smc.engine.telemetry import (
    SelectionAuditRecord,
    aggregate_selection_telemetry,
)


def _make_probe_context(bar_index: int = 100) -> StrategyContext:
    ts_open = pd.Timestamp("2026-03-09 15:00:00+00:00")
    ts_close = pd.Timestamp("2026-03-09 15:15:00+00:00")
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
        atr14=5.0,
        htf_bias=BiasStateSnapshot(bias="bullish", timestamp=ts_open),
        session_decision=SessionDecisionSnapshot(
            in_session=True, session_name="NY_AM", timestamp=ts_open, reason="ok"
        ),
    )


def _make_probe_regime(bar_index: int = 100) -> MarketRegime:
    return MarketRegime(
        regime="bullish_trend",
        bar_index=bar_index,
        timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
        efficiency_ratio=0.8,
        atr_percentile=50.0,
    )


def _make_probe_candidate(
    setup_id: str,
    strategy_id: str,
    direction: str = "BUY",
    bar_index: int = 100,
    entry: float = 2000.0,
    planned_rr: float = 2.0,
    cluster_id: str = "c1",
) -> CandidateSetup:
    sl = entry - 10.0 if direction == "BUY" else entry + 10.0
    tp = entry + 20.0 if direction == "BUY" else entry - 20.0
    return CandidateSetup(
        setup_id=setup_id,
        strategy_id=strategy_id,
        direction=direction,  # type: ignore[arg-type]
        bar_index=bar_index,
        timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"),
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        planned_rr=planned_rr,
        evidences=(
            EvidenceRef(f"ev_{setup_id}", "fair_value_gap", bar_index - 5, entry),
        ),
        evidence_cluster_id=cluster_id,
        expiry_bar=bar_index + 10,
        meta={},
    )


def _make_probe_eval(
    candidate: CandidateSetup,
    status: str = "ELIGIBLE",
    score: float = 80.0,
    rejection_reasons: tuple[str, ...] = (),
) -> StrategyEvaluation:
    reg_s = score if status == "ELIGIBLE" else 0.0
    s_s = score if status == "ELIGIBLE" else 0.0
    c_s = score if status == "ELIGIBLE" else 0.0
    e_s = score if status == "ELIGIBLE" else 0.0
    tot = score if status == "ELIGIBLE" else 0.0
    return StrategyEvaluation(
        candidate=candidate,
        status=status,  # type: ignore[arg-type]
        rejection_reasons=rejection_reasons,
        regime_score=reg_s,
        setup_score=s_s,
        context_score=c_s,
        exec_score=e_s,
        total_score=tot,
        details={},
    )


def _make_probe_scorecard(
    cluster_id: str = "c1",
    direction: str = "BUY",
    primary_strategy_id: str = "S01",
    primary_setup_id: str = "s1",
    supporting_strategy_ids: tuple[str, ...] = (),
    score: float = 80.0,
    planned_rr: float = 2.0,
    scoring_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15),
    member_setup_ids: tuple[str, ...] | None = None,
    member_scores: dict[str, float] | None = None,
) -> ClusterScorecard:
    if member_setup_ids is None:
        member_setup_ids = (primary_setup_id,)
    if member_scores is None:
        member_scores = {m: score for m in member_setup_ids}
    return ClusterScorecard(
        cluster_id=cluster_id,
        direction=direction,  # type: ignore[arg-type]
        primary_strategy_id=primary_strategy_id,
        primary_setup_id=primary_setup_id,
        supporting_strategy_ids=supporting_strategy_ids,
        total_score=score,
        regime_score=score,
        setup_score=score,
        context_score=score,
        exec_score=score,
        planned_rr=planned_rr,
        member_count=len(member_setup_ids),
        scoring_weights=scoring_weights,
        member_setup_ids=member_setup_ids,
        member_scores=member_scores,
        details={
            "member_setup_ids": list(member_setup_ids),
            "member_scores": dict(member_scores),
            "execution_score_basis": "planned_rr_pre_fill",
            "fvg_atr_basis": "signal_bar_atr14",
        },
    )


def _make_probe_audit_record(
    bar_index: int = 100,
    symbol: str = "EURUSD",
    timeframe: str = "15m",
    timestamp: str = "2026-03-09 15:15:00+00:00",
    action: str = "SELECT",
    reason: str = "ok",
    primary_strategy_id: str | None = "S01",
    primary_setup_id: str | None = "s1",
    direction: str | None = "BUY",
    cluster_id: str | None = "c1",
    total_score: float | None = None,
    score_gap: float | None = None,
    supporting_strategy_ids: tuple[str, ...] = (),
    scorecards: tuple[ClusterScorecard, ...] | None = None,
    scoring_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15),
    gate_reason_counts: dict[str, int] | None = None,
    evaluated_count: int | None = None,
    eligible_count: int | None = None,
    rejected_count: int = 0,
    minimum_total_score: float = 60.0,
    minimum_direction_gap: float = 5.0,
    direction_conflict_present: bool | None = None,
    best_buy_score: float | None = None,
    best_sell_score: float | None = None,
) -> SelectionAuditRecord:
    ts = pd.Timestamp(timestamp)
    if action == "SELECT":
        if scorecards is None or len(scorecards) == 0:
            sc = _make_probe_scorecard(
                cluster_id=cluster_id or "c1",
                direction=direction or "BUY",
                primary_strategy_id=primary_strategy_id or "S01",
                primary_setup_id=primary_setup_id or "s1",
                score=total_score if total_score is not None else 80.0,
                scoring_weights=scoring_weights,
                supporting_strategy_ids=supporting_strategy_ids,
            )
            scorecards = (sc,)
        else:
            if cluster_id == "c1" and "c1" not in {sc.cluster_id for sc in scorecards}:
                cluster_id = scorecards[0].cluster_id
            matching = [sc for sc in scorecards if sc.cluster_id == cluster_id]
            win_sc = matching[0] if matching else scorecards[0]
            if primary_strategy_id == "S01" and win_sc.primary_strategy_id != "S01":
                primary_strategy_id = win_sc.primary_strategy_id
            if primary_setup_id == "s1" and win_sc.primary_setup_id != "s1":
                primary_setup_id = win_sc.primary_setup_id
            if direction == "BUY" and win_sc.direction != "BUY":
                direction = win_sc.direction
            if total_score is None:
                total_score = win_sc.total_score
            if not supporting_strategy_ids and win_sc.supporting_strategy_ids:
                supporting_strategy_ids = win_sc.supporting_strategy_ids
        strat_id = primary_strategy_id
    else:  # NO_TRADE
        strat_id = None
        primary_strategy_id = None
        primary_setup_id = None
        direction = None
        cluster_id = None
        total_score = None
        supporting_strategy_ids = ()
        if scorecards is None:
            scorecards = ()

    if eligible_count is None:
        eligible_count = len({m for sc in scorecards for m in sc.member_setup_ids})
    if evaluated_count is None:
        evaluated_count = eligible_count + rejected_count

    buy_cards = [sc for sc in scorecards if sc.direction == "BUY"]
    sell_cards = [sc for sc in scorecards if sc.direction == "SELL"]
    if direction_conflict_present is None:
        direction_conflict_present = bool(buy_cards and sell_cards)
    if best_buy_score is None and buy_cards:
        best_buy_score = round(min(buy_cards, key=cluster_rank_key).total_score, 2)
    if best_sell_score is None and sell_cards:
        best_sell_score = round(min(sell_cards, key=cluster_rank_key).total_score, 2)

    dec_id = make_decision_id(bar_index, action, strat_id)
    return SelectionAuditRecord(
        record_version="audit-v1",
        symbol=symbol,
        timeframe=timeframe,
        bar_index=bar_index,
        timestamp=ts,
        decision_id=dec_id,
        action=action,  # type: ignore[arg-type]
        reason=reason,
        primary_strategy_id=primary_strategy_id,
        primary_setup_id=primary_setup_id,
        direction=direction,  # type: ignore[arg-type]
        total_score=total_score,
        score_gap=score_gap,
        direction_conflict_present=direction_conflict_present,
        best_buy_score=best_buy_score,
        best_sell_score=best_sell_score,
        cluster_id=cluster_id,
        supporting_strategy_ids=supporting_strategy_ids,
        cluster_scorecards=scorecards,
        gate_reason_counts=gate_reason_counts or {},
        evaluated_count=evaluated_count,
        eligible_count=eligible_count,
        rejected_count=rejected_count,
        regime="bullish_trend",
        selector_version="selector-v1",
        minimum_total_score=minimum_total_score,
        minimum_direction_gap=minimum_direction_gap,
        scoring_weights=scoring_weights,
    )


class TestT538TamperProbes(unittest.TestCase):
    """
    20 Independent Tamper Probes verifying fail-closed invariants for T53.8.
    """

    def setUp(self):
        self.context = _make_probe_context()
        self.regime = _make_probe_regime()

    # -------------------------------------------------------------------------
    # Probe 1: supporting_strategy_ids contains primary_strategy_id
    # -------------------------------------------------------------------------
    def test_probe_01_supporting_strategy_ids_contains_primary_strategy_id(self):
        """Probe 1: Fail closed if supporting_strategy_ids contains primary_strategy_id."""
        with self.assertRaises(ValueError) as cm1:
            _make_probe_scorecard(
                primary_strategy_id="S01",
                supporting_strategy_ids=("S01",),
            )
        self.assertIn("supporting_strategy_ids", str(cm1.exception))

        setup = _make_probe_candidate("s1", "S01")
        ev = _make_probe_eval(setup, score=80.0)
        with self.assertRaises(ValueError) as cm2:
            SelectionDecision(
                decision_id=make_decision_id(100, "SELECT", "S01"),
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="SELECT",
                selected_setup=setup,
                evaluations=(ev,),
                primary_strategy_id="S01",
                supporting_strategy_ids=("S01",),
                regime=self.regime,
            )
        self.assertIn("supporting_strategy_ids", str(cm2.exception))

        with self.assertRaises(ValueError) as cm3:
            _make_probe_audit_record(
                primary_strategy_id="S01",
                supporting_strategy_ids=("S01",),
            )
        self.assertIn("supporting_strategy_ids", str(cm3.exception))

    # -------------------------------------------------------------------------
    # Probe 2: supporting_strategy_ids has duplicates
    # -------------------------------------------------------------------------
    def test_probe_02_supporting_strategy_ids_has_duplicates(self):
        """Probe 2: Fail closed if supporting_strategy_ids has duplicates."""
        with self.assertRaises(ValueError) as cm1:
            _make_probe_scorecard(
                primary_strategy_id="S01",
                supporting_strategy_ids=("S05", "S05"),
            )
        self.assertIn("supporting_strategy_ids", str(cm1.exception))

        setup = _make_probe_candidate("s1", "S01")
        ev = _make_probe_eval(setup, score=80.0)
        with self.assertRaises(ValueError) as cm2:
            SelectionDecision(
                decision_id=make_decision_id(100, "SELECT", "S01"),
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="SELECT",
                selected_setup=setup,
                evaluations=(ev,),
                primary_strategy_id="S01",
                supporting_strategy_ids=("S05", "S05"),
                regime=self.regime,
            )
        self.assertIn("supporting_strategy_ids", str(cm2.exception))

        with self.assertRaises(ValueError) as cm3:
            _make_probe_audit_record(
                primary_strategy_id="S01",
                supporting_strategy_ids=("S05", "S05"),
            )
        self.assertIn("supporting_strategy_ids", str(cm3.exception))

    # -------------------------------------------------------------------------
    # Probe 3: member_setup_ids missing primary_setup_id
    # -------------------------------------------------------------------------
    def test_probe_03_member_setup_ids_missing_primary_setup_id(self):
        """Probe 3: Fail closed if member_setup_ids does not contain primary_setup_id."""
        with self.assertRaises(ValueError):
            _make_probe_scorecard(
                primary_setup_id="s1",
                member_setup_ids=("s2", "s3"),
                member_scores={"s2": 80.0, "s3": 80.0},
            )

    # -------------------------------------------------------------------------
    # Probe 4: member_setup_ids not canonical sorted or has duplicates
    # -------------------------------------------------------------------------
    def test_probe_04_member_setup_ids_not_canonical_sorted_or_has_duplicates(self):
        """Probe 4: Fail closed if member_setup_ids is not sorted or has duplicates."""
        # Unsorted: ("s2", "s1")
        with self.assertRaises(ValueError):
            _make_probe_scorecard(
                primary_setup_id="s1",
                member_setup_ids=("s2", "s1"),
                member_scores={"s1": 80.0, "s2": 80.0},
            )

        # Duplicate: ("s1", "s1")
        with self.assertRaises(ValueError):
            _make_probe_scorecard(
                primary_setup_id="s1",
                member_setup_ids=("s1", "s1"),
                member_scores={"s1": 80.0},
            )

    # -------------------------------------------------------------------------
    # Probe 5: member_count != len(member_setup_ids)
    # -------------------------------------------------------------------------
    def test_probe_05_member_count_mismatch_member_setup_ids(self):
        """Probe 5: Fail closed if member_count != len(member_setup_ids)."""
        with self.assertRaises(ValueError):
            ClusterScorecard(
                cluster_id="c1",
                direction="BUY",
                primary_strategy_id="S01",
                primary_setup_id="s1",
                supporting_strategy_ids=(),
                total_score=80.0,
                regime_score=80.0,
                setup_score=80.0,
                context_score=80.0,
                exec_score=80.0,
                planned_rr=2.0,
                member_count=3,  # Should be 1
                scoring_weights=(0.25, 0.35, 0.25, 0.15),
                member_setup_ids=("s1",),
                member_scores={"s1": 80.0},
                details={},
            )

    # -------------------------------------------------------------------------
    # Probe 6: member_scores keys != member_setup_ids
    # -------------------------------------------------------------------------
    def test_probe_06_member_scores_keys_mismatch_member_setup_ids(self):
        """Probe 6: Fail closed if member_scores keys != member_setup_ids."""
        with self.assertRaises(ValueError):
            _make_probe_scorecard(
                primary_setup_id="s1",
                member_setup_ids=("s1", "s2"),
                member_scores={"s1": 80.0, "s3": 80.0},  # "s3" instead of "s2"
            )

    # -------------------------------------------------------------------------
    # Probe 7: member_scores[primary_setup_id] != total_score
    # -------------------------------------------------------------------------
    def test_probe_07_member_scores_primary_setup_id_mismatch_total_score(self):
        """Probe 7: Fail closed if member_scores[primary_setup_id] != total_score."""
        with self.assertRaises(ValueError):
            _make_probe_scorecard(
                primary_setup_id="s1",
                score=80.0,
                member_scores={"s1": 75.0},  # Primary score 75.0 != total_score 80.0
            )

    # -------------------------------------------------------------------------
    # Probe 8: total_score != composite recomputed from components + scoring_weights
    # -------------------------------------------------------------------------
    def test_probe_08_total_score_mismatch_recomputed_components(self):
        """Probe 8: Fail closed if total_score != sum(w * comp)."""
        # Components = 80.0, weights = (0.25, 0.35, 0.25, 0.15) -> expected = 80.0
        # Tampered total_score = 85.0
        with self.assertRaises(ValueError):
            ClusterScorecard(
                cluster_id="c1",
                direction="BUY",
                primary_strategy_id="S01",
                primary_setup_id="s1",
                supporting_strategy_ids=(),
                total_score=85.0,  # Tampered
                regime_score=80.0,
                setup_score=80.0,
                context_score=80.0,
                exec_score=80.0,
                planned_rr=2.0,
                member_count=1,
                scoring_weights=(0.25, 0.35, 0.25, 0.15),
                member_setup_ids=("s1",),
                member_scores={"s1": 85.0},
                details={},
            )

    # -------------------------------------------------------------------------
    # Probe 9: scoring_weights sum != 1.0 (exact case 0.9999)
    # -------------------------------------------------------------------------
    def test_probe_09_scoring_weights_sum_not_one_case_0_9999(self):
        """Probe 9: Fail closed if scoring_weights sum != 1.0 (specifically 0.9999)."""
        bad_weights = (0.25, 0.25, 0.25, 0.2499)
        self.assertAlmostEqual(sum(bad_weights), 0.9999)

        # 1. SelectorConfig
        with self.assertRaises(ValueError):
            SelectorConfig(
                weight_regime=bad_weights[0],
                weight_setup=bad_weights[1],
                weight_context=bad_weights[2],
                weight_exec=bad_weights[3],
            )

        # 2. ClusterScorecard
        with self.assertRaises(ValueError):
            _make_probe_scorecard(scoring_weights=bad_weights)

        # 3. SelectionAuditRecord
        with self.assertRaises(ValueError):
            _make_probe_audit_record(scoring_weights=bad_weights)

    # -------------------------------------------------------------------------
    # Probe 10: scoring_weights has negative or non-finite or bool
    # -------------------------------------------------------------------------
    def test_probe_10_scoring_weights_negative_nan_inf_bool(self):
        """Probe 10: Fail closed on negative, NaN, Inf, and bool weights."""
        # Negative
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=-0.1, weight_setup=0.4, weight_context=0.4, weight_exec=0.3)

        # NaN
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=float("nan"), weight_setup=0.35, weight_context=0.25, weight_exec=0.15)

        # Inf
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=float("inf"), weight_setup=0.35, weight_context=0.25, weight_exec=0.15)

        # Bool True
        with self.assertRaises((TypeError, StrictModelTypeError)):
            SelectorConfig(weight_regime=True, weight_setup=0.0, weight_context=0.0, weight_exec=0.0)  # type: ignore[arg-type]

    # -------------------------------------------------------------------------
    # Probe 11: conflicting_direction but score_gap is None
    # -------------------------------------------------------------------------
    def test_probe_11_conflicting_direction_with_none_score_gap(self):
        """Probe 11: Fail closed if conflicting_direction has score_gap=None."""
        with self.assertRaises(ValueError) as cm1:
            SelectionDecision(
                decision_id=make_decision_id(100, "NO_TRADE", None),
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="NO_TRADE",
                reason="conflicting_direction",
                score_gap=None,  # Forbidden
                regime=self.regime,
            )
        self.assertIn("score_gap", str(cm1.exception))

        card_buy = _make_probe_scorecard(cluster_id="c_buy", direction="BUY", primary_setup_id="s_b", score=80.0)
        card_sell = _make_probe_scorecard(cluster_id="c_sell", direction="SELL", primary_setup_id="s_s", score=78.0)
        with self.assertRaises(ValueError) as cm2:
            _make_probe_audit_record(
                action="NO_TRADE",
                reason="conflicting_direction",
                score_gap=None,  # Forbidden
                scorecards=(card_buy, card_sell),
            )
        self.assertIn("score_gap", str(cm2.exception))

    # -------------------------------------------------------------------------
    # Probe 12: no_eligible_setup or insufficient_score but score_gap is not None
    # -------------------------------------------------------------------------
    def test_probe_12_no_eligible_or_insufficient_score_with_non_none_score_gap(self):
        """Probe 12: Fail closed if no_eligible_setup or insufficient_score has score_gap != None."""
        # no_eligible_setup
        with self.assertRaises(ValueError) as cm1:
            SelectionDecision(
                decision_id=make_decision_id(100, "NO_TRADE", None),
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="NO_TRADE",
                reason="no_eligible_setup",
                score_gap=5.0,  # Forbidden
                regime=self.regime,
            )
        self.assertIn("score_gap", str(cm1.exception))

        with self.assertRaises(ValueError) as cm2:
            _make_probe_audit_record(
                action="NO_TRADE",
                reason="no_eligible_setup",
                score_gap=5.0,  # Forbidden
            )
        self.assertIn("score_gap", str(cm2.exception))

        # insufficient_score
        with self.assertRaises(ValueError) as cm3:
            SelectionDecision(
                decision_id=make_decision_id(100, "NO_TRADE", None),
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                action="NO_TRADE",
                reason="insufficient_score",
                score_gap=5.0,  # Forbidden
                regime=self.regime,
            )
        self.assertIn("score_gap", str(cm3.exception))

        card_insufficient = _make_probe_scorecard(cluster_id="c_low", score=50.0)
        with self.assertRaises(ValueError) as cm4:
            _make_probe_audit_record(
                action="NO_TRADE",
                reason="insufficient_score",
                scorecards=(card_insufficient,),
                score_gap=5.0,  # Forbidden
            )
        self.assertIn("score_gap", str(cm4.exception))

    # -------------------------------------------------------------------------
    # Probe 13: cluster_scorecards not canonical sorted by cluster_rank_key
    # -------------------------------------------------------------------------
    def test_probe_13_cluster_scorecards_not_canonical_sorted(self):
        """Probe 13: Fail closed if cluster_scorecards are not sorted by cluster_rank_key."""
        card_low = _make_probe_scorecard(cluster_id="c_low", primary_setup_id="s_low", score=70.0)
        card_high = _make_probe_scorecard(cluster_id="c_high", primary_setup_id="s_high", score=85.0)

        # In SelectionAuditRecord: card_low before card_high violates canonical rank sort
        with self.assertRaises(ValueError) as cm1:
            _make_probe_audit_record(
                scorecards=(card_low, card_high),
            )
        self.assertIn("canonically sorted", str(cm1.exception))

        # In SelectorOutput
        cand_high = _make_probe_candidate("s_high", "S01", cluster_id="c_high")
        cand_low = _make_probe_candidate("s_low", "S01", cluster_id="c_low")
        ev_high = _make_probe_eval(cand_high, score=85.0)
        ev_low = _make_probe_eval(cand_low, score=70.0)

        dec = SelectionDecision(
            decision_id=make_decision_id(100, "SELECT", "S01"),
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            selected_setup=cand_high,
            evaluations=(ev_high, ev_low),
            meta={
                "selector_version": "1.0",
                "minimum_total_score": 60.0,
                "minimum_direction_gap": 5.0,
                "scoring_weights": [0.25, 0.35, 0.25, 0.15],
                "execution_score_basis": "planned_rr_pre_fill",
                "fvg_atr_basis": "signal_bar_atr14",
                "symbol": "EURUSD",
                "timeframe": "15m",
            },
        )
        audit = _make_probe_audit_record(
            scorecards=(card_high, card_low),
            evaluated_count=2,
            eligible_count=2,
            rejected_count=0,
        )

        with self.assertRaises(ValueError):
            # Pass unsorted scorecards to SelectorOutput
            SelectorOutput(decision=dec, audit_record=audit, scorecards=(card_low, card_high))

    # -------------------------------------------------------------------------
    # Probe 14: SelectionAuditRecord decision_id does not match make_decision_id
    # -------------------------------------------------------------------------
    def test_probe_14_selection_audit_record_decision_id_mismatch_make_decision_id(self):
        """Probe 14: Fail closed if SelectionAuditRecord.decision_id != make_decision_id(...)."""
        with self.assertRaises(ValueError):
            SelectionAuditRecord(
                record_version="audit-v1",
                decision_id="tampered_decision_id_12345",  # Tampered!
                bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
                symbol="EURUSD",
                timeframe="15m",
                action="NO_TRADE",
                reason="no_eligible_setup",
                scoring_weights=(0.25, 0.35, 0.25, 0.15),
                minimum_total_score=60.0,
                minimum_direction_gap=5.0,
            )

    # -------------------------------------------------------------------------
    # Probe 15: SelectionAuditRecord.from_dict() missing required selector config fields
    # -------------------------------------------------------------------------
    def test_probe_15_audit_record_from_dict_missing_required_config_fields(self):
        """Probe 15: Fail closed if from_dict() omits any required selector config field."""
        rec = _make_probe_audit_record(action="NO_TRADE", reason="no_eligible_setup")
        base_dict = rec.to_dict()

        for req_field in ("minimum_total_score", "minimum_direction_gap", "scoring_weights", "selector_version"):
            tampered = dict(base_dict)
            del tampered[req_field]
            with self.assertRaises((ValueError, KeyError)):
                SelectionAuditRecord.from_dict(tampered)

    # -------------------------------------------------------------------------
    # Probe 16: aggregate_selection_telemetry() duplicate key fails closed
    # -------------------------------------------------------------------------
    def test_probe_16_aggregate_selection_telemetry_duplicate_key_fails_closed(self):
        """Probe 16: Fail closed on duplicate (symbol, timeframe, decision_id) even with identical payload."""
        rec = _make_probe_audit_record(action="NO_TRADE", reason="no_eligible_setup")

        # Passing identical records must fail closed with ValueError
        with self.assertRaises(ValueError) as ctx:
            aggregate_selection_telemetry([rec, rec])
        self.assertIn("Duplicate telemetry record", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Probe 17: SelectorOutput config mismatch between decision.meta, audit, scorecards
    # -------------------------------------------------------------------------
    # Probe 17: SelectorOutput config mismatch between decision.meta, audit, scorecards
    # -------------------------------------------------------------------------
    def test_probe_17_selector_output_config_mismatch(self):
        """Probe 17: Fail closed on config mismatch between decision.meta, audit, and scorecards."""
        cand = _make_probe_candidate("s1", "S01")
        ev = _make_probe_eval(cand, score=80.0)
        card = _make_probe_scorecard(score=80.0)

        dec = SelectionDecision(
            decision_id=make_decision_id(100, "SELECT", "S01"),
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            selected_setup=cand,
            regime=self.regime,
            evaluations=(ev,),
            meta={
                "selector_version": "selector-v1",
                "minimum_total_score": 75.0,  # Mismatch! Audit has 60.0
                "minimum_direction_gap": 5.0,
                "scoring_weights": [0.25, 0.35, 0.25, 0.15],
                "execution_score_basis": "planned_rr_pre_fill",
                "fvg_atr_basis": "signal_bar_atr14",
                "symbol": "EURUSD",
                "timeframe": "15m",
            },
        )
        audit = _make_probe_audit_record(scorecards=(card,))

        with self.assertRaises(ValueError) as ctx:
            SelectorOutput(decision=dec, audit_record=audit, scorecards=(card,))
        self.assertIn("minimum_total_score mismatch", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Probe 18: SelectorOutput evaluation counts / gate reasons mismatch
    # -------------------------------------------------------------------------
    def test_probe_18_selector_output_evaluation_counts_histogram_mismatch(self):
        """Probe 18: Fail closed if evaluation counts or gate reason histogram don't match decision.evaluations."""
        cand = _make_probe_candidate("s1", "S01")
        ev = _make_probe_eval(cand, score=80.0)
        card = _make_probe_scorecard(score=80.0)

        dec = SelectionDecision(
            decision_id=make_decision_id(100, "SELECT", "S01"),
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            selected_setup=cand,
            regime=self.regime,
            evaluations=(ev,),
            meta={
                "selector_version": "selector-v1",
                "minimum_total_score": 60.0,
                "minimum_direction_gap": 5.0,
                "scoring_weights": [0.25, 0.35, 0.25, 0.15],
                "execution_score_basis": "planned_rr_pre_fill",
                "fvg_atr_basis": "signal_bar_atr14",
                "symbol": "EURUSD",
                "timeframe": "15m",
            },
        )
        # 1. Evaluation count mismatch
        audit1 = _make_probe_audit_record(
            scorecards=(card,),
            evaluated_count=2,
            eligible_count=1,
            rejected_count=1,
        )
        with self.assertRaises(ValueError) as ctx1:
            SelectorOutput(decision=dec, audit_record=audit1, scorecards=(card,))
        self.assertIn("evaluated_count mismatch", str(ctx1.exception))

        # 2. Gate reasons histogram mismatch
        audit2 = _make_probe_audit_record(
            scorecards=(card,),
            evaluated_count=1,
            eligible_count=1,
            rejected_count=0,
            gate_reason_counts={"regime_mismatch": 1},
        )
        with self.assertRaises(ValueError) as ctx2:
            SelectorOutput(decision=dec, audit_record=audit2, scorecards=(card,))
        self.assertIn("gate_reason_counts mismatch", str(ctx2.exception))

    # -------------------------------------------------------------------------
    # Probe 19: SelectorOutput eligible setup missing from scorecards or duplicated
    # -------------------------------------------------------------------------
    def test_probe_19_selector_output_eligible_setup_missing_or_duplicate(self):
        """Probe 19: Fail closed if eligible setup is omitted from scorecards or duplicated across scorecards."""
        cand1 = _make_probe_candidate("s1", "S01")
        cand2 = _make_probe_candidate("s2", "S05")
        ev1 = _make_probe_eval(cand1, score=80.0)
        ev2 = _make_probe_eval(cand2, score=75.0)

        # Missing cand2 from scorecard members
        card1 = _make_probe_scorecard(cluster_id="c1", primary_setup_id="s1", score=80.0)

        dec = SelectionDecision(
            decision_id=make_decision_id(100, "SELECT", "S01"),
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            selected_setup=cand1,
            regime=self.regime,
            evaluations=(ev1, ev2),
            meta={
                "selector_version": "selector-v1",
                "minimum_total_score": 60.0,
                "minimum_direction_gap": 5.0,
                "scoring_weights": [0.25, 0.35, 0.25, 0.15],
                "execution_score_basis": "planned_rr_pre_fill",
                "fvg_atr_basis": "signal_bar_atr14",
                "symbol": "EURUSD",
                "timeframe": "15m",
            },
        )
        audit = _make_probe_audit_record(
            scorecards=(card1,),
            evaluated_count=2,
            eligible_count=1,
            rejected_count=1,
        )

        with self.assertRaises(ValueError) as ctx:
            SelectorOutput(decision=dec, audit_record=audit, scorecards=(card1,))
        self.assertIn("eligible_count mismatch", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Probe 20: SelectorOutput non-rank-1 cluster or primary member designated winner
    # -------------------------------------------------------------------------
    def test_probe_20_selector_output_non_rank_1_winner_rejected(self):
        """Probe 20: Fail closed if primary member or winning cluster is not rank 1."""
        cand_high = _make_probe_candidate("s_high", "S01", cluster_id="c_high")
        cand_low = _make_probe_candidate("s_low", "S01", cluster_id="c_low")
        ev_high = _make_probe_eval(cand_high, score=85.0)
        ev_low = _make_probe_eval(cand_low, score=70.0)

        card_high = _make_probe_scorecard(cluster_id="c_high", primary_strategy_id="S01", primary_setup_id="s_high", score=85.0)
        card_low = _make_probe_scorecard(cluster_id="c_low", primary_strategy_id="S01", primary_setup_id="s_low", score=70.0)

        # 1. Tampered: designate cand_low (score 70.0) as selected setup instead of cand_high (score 85.0)
        dec = SelectionDecision(
            decision_id=make_decision_id(100, "SELECT", "S01"),
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            selected_setup=cand_low,  # Tampered non-rank-1 winner!
            regime=self.regime,
            evaluations=(ev_high, ev_low),
            meta={
                "selector_version": "selector-v1",
                "minimum_total_score": 60.0,
                "minimum_direction_gap": 5.0,
                "scoring_weights": [0.25, 0.35, 0.25, 0.15],
                "execution_score_basis": "planned_rr_pre_fill",
                "fvg_atr_basis": "signal_bar_atr14",
                "symbol": "EURUSD",
                "timeframe": "15m",
            },
        )
        audit = _make_probe_audit_record(
            primary_strategy_id="S01",
            primary_setup_id="s_high",
            cluster_id="c_high",
            total_score=85.0,
            scorecards=(card_high, card_low),
            evaluated_count=2,
            eligible_count=2,
            rejected_count=0,
        )

        with self.assertRaises(ValueError) as ctx:
            SelectorOutput(decision=dec, audit_record=audit, scorecards=(card_high, card_low))
        self.assertIn("primary_setup_id mismatch", str(ctx.exception))

        # 2. Scorecard primary member not winning member rank key
        # Under Option B, ClusterScorecard ensures primary setup has maximal score.
        # Here both members have equal total score 80.0, but s_high has higher planned_rr (3.0 vs 2.0).
        card_bad_member = ClusterScorecard(
            cluster_id="c_high",
            direction="BUY",
            primary_strategy_id="S05",
            primary_setup_id="s_low",  # s_low has planned_rr=2.0, while s_high has planned_rr=3.0
            supporting_strategy_ids=("S01",),
            total_score=80.0,
            regime_score=80.0,
            setup_score=80.0,
            context_score=80.0,
            exec_score=80.0,
            planned_rr=2.0,
            member_count=2,
            scoring_weights=(0.25, 0.35, 0.25, 0.15),
            member_setup_ids=("s_high", "s_low"),
            member_scores={"s_high": 80.0, "s_low": 80.0},
            details={"execution_score_basis": "planned_rr_pre_fill", "fvg_atr_basis": "signal_bar_atr14"},
        )
        cand_high_rr = _make_probe_candidate("s_high", "S01", planned_rr=3.0, cluster_id="c_high")
        cand_low_rr = _make_probe_candidate("s_low", "S05", planned_rr=2.0, cluster_id="c_high")
        ev_high_rr = _make_probe_eval(cand_high_rr, score=80.0)
        ev_low_rr = _make_probe_eval(cand_low_rr, score=80.0)
        dec_valid = SelectionDecision(
            decision_id=make_decision_id(100, "SELECT", "S05"),
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:15:00+00:00"),
            action="SELECT",
            reason="ok",
            primary_strategy_id="S05",
            supporting_strategy_ids=("S01",),
            selected_setup=cand_low_rr,
            regime=self.regime,
            evaluations=(ev_high_rr, ev_low_rr),
            meta={
                "selector_version": "selector-v1",
                "minimum_total_score": 60.0,
                "minimum_direction_gap": 5.0,
                "scoring_weights": [0.25, 0.35, 0.25, 0.15],
                "execution_score_basis": "planned_rr_pre_fill",
                "fvg_atr_basis": "signal_bar_atr14",
                "symbol": "EURUSD",
                "timeframe": "15m",
            },
        )
        audit_bad_member = _make_probe_audit_record(
            primary_strategy_id="S05",
            primary_setup_id="s_low",
            cluster_id="c_high",
            total_score=80.0,
            scorecards=(card_bad_member,),
            evaluated_count=2,
            eligible_count=2,
            rejected_count=0,
        )
        with self.assertRaises(ValueError) as ctx_mem:
            SelectorOutput(decision=dec_valid, audit_record=audit_bad_member, scorecards=(card_bad_member,))
        self.assertIn("does not win canonical member rank key", str(ctx_mem.exception))


if __name__ == "__main__":
    unittest.main()

