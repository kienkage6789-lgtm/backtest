"""
scratch/run_t53_8_independent_probes.py
=======================================
Independent verification probe script outside the test runner for T53.8.
Validates the 6 required integrity invariants and prints corresponding tokens.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from smc.engine.models import SelectionDecision, make_decision_id
from smc.engine.selector import ClusterScorecard
from smc.engine.telemetry import (
    SelectionAuditRecord,
    aggregate_selection_telemetry,
)


def make_valid_scorecard(
    cluster_id: str = "c1",
    direction: str = "BUY",
    primary_strategy_id: str = "S01",
    primary_setup_id: str = "s1",
    total_score: float = 85.0,
    scoring_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15),
    member_scores: dict[str, float] | None = None,
) -> ClusterScorecard:
    if member_scores is None:
        member_scores = {primary_setup_id: total_score}
    member_setup_ids = tuple(sorted(member_scores.keys()))
    return ClusterScorecard(
        cluster_id=cluster_id,
        direction=direction,  # type: ignore[arg-type]
        primary_strategy_id=primary_strategy_id,
        primary_setup_id=primary_setup_id,
        supporting_strategy_ids=(),
        total_score=total_score,
        regime_score=total_score,
        setup_score=total_score,
        context_score=total_score,
        exec_score=total_score,
        planned_rr=2.0,
        member_count=len(member_setup_ids),
        scoring_weights=scoring_weights,
        member_setup_ids=member_setup_ids,
        member_scores=member_scores,
        details={
            "member_setup_ids": list(member_setup_ids),
            "member_scores": dict(member_scores),
        },
    )


def test_audit_total_mismatch_rejected() -> None:
    sc = make_valid_scorecard(total_score=85.0)
    try:
        SelectionAuditRecord(
            record_version="1.0.0",
            symbol="EURUSD",
            timeframe="15m",
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"),
            decision_id="sel:100:SELECT:S01",
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            primary_setup_id="s1",
            direction="BUY",
            total_score=80.0,  # Mismatches sc.total_score=85.0
            direction_conflict_present=False,
            best_buy_score=85.0,
            best_sell_score=None,
            cluster_id="c1",
            cluster_scorecards=(sc,),
            evaluated_count=1,
            eligible_count=1,
            rejected_count=0,
        )
    except ValueError as exc:
        if "does not match winning scorecard total_score" in str(exc):
            print("AUDIT_TOTAL_MISMATCH_REJECTED")
            return
        raise AssertionError(f"Unexpected error: {exc}")
    raise AssertionError("Expected ValueError for total score mismatch")


def test_select_without_scorecard_rejected() -> None:
    try:
        SelectionAuditRecord(
            record_version="1.0.0",
            symbol="EURUSD",
            timeframe="15m",
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"),
            decision_id="sel:100:SELECT:S01",
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            primary_setup_id="s1",
            direction="BUY",
            total_score=85.0,
            cluster_id="c1",
            cluster_scorecards=(),  # Missing winning scorecard
            evaluated_count=0,
            eligible_count=0,
            rejected_count=0,
        )
    except ValueError as exc:
        if "must have at least one cluster scorecard" in str(exc) or "must contain winning" in str(exc):
            print("SELECT_WITHOUT_SCORECARD_REJECTED")
            return
        raise AssertionError(f"Unexpected error: {exc}")
    raise AssertionError("Expected ValueError for SELECT without scorecard")


def test_intra_record_mixed_weights_rejected() -> None:
    sc = make_valid_scorecard(
        total_score=85.0,
        scoring_weights=(0.30, 0.30, 0.20, 0.20),  # Mismatch with audit weights
    )
    try:
        SelectionAuditRecord(
            record_version="1.0.0",
            symbol="EURUSD",
            timeframe="15m",
            bar_index=100,
            timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"),
            decision_id="sel:100:SELECT:S01",
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            primary_setup_id="s1",
            direction="BUY",
            total_score=85.0,
            direction_conflict_present=False,
            best_buy_score=85.0,
            best_sell_score=None,
            cluster_id="c1",
            cluster_scorecards=(sc,),
            scoring_weights=(0.25, 0.35, 0.25, 0.15),
            evaluated_count=1,
            eligible_count=1,
            rejected_count=0,
        )
    except ValueError as exc:
        if "does not match audit scoring_weights" in str(exc):
            print("INTRA_RECORD_MIXED_WEIGHTS_REJECTED")
            return
        raise AssertionError(f"Unexpected error: {exc}")
    raise AssertionError("Expected ValueError for mixed scoring weights")


def test_non_winning_primary_rejected() -> None:
    try:
        make_valid_scorecard(
            cluster_id="c1",
            primary_strategy_id="S01",
            primary_setup_id="s_low",
            total_score=70.0,
            member_scores={"s_high": 90.0, "s_low": 70.0},
        )
    except ValueError as exc:
        if "is less than maximum member score" in str(exc) or "cannot have primary_score" in str(exc):
            print("NON_WINNING_PRIMARY_REJECTED")
            return
        raise AssertionError(f"Unexpected error: {exc}")
    raise AssertionError("Expected ValueError for non-winning primary in cluster")


def test_false_positive_tests_fixed() -> None:
    import os
    import unittest
    from tests.test_smc_engine_t53_8_tamper_probes import TestT538TamperProbes
    suite = unittest.TestSuite()
    for test_name in [
        "test_probe_01_supporting_strategy_ids_contains_primary_strategy_id",
        "test_probe_02_supporting_strategy_ids_has_duplicates",
        "test_probe_11_conflicting_direction_with_none_score_gap",
        "test_probe_12_no_eligible_or_insufficient_score_with_non_none_score_gap",
        "test_probe_13_cluster_scorecards_not_canonical_sorted",
        "test_probe_18_selector_output_evaluation_counts_histogram_mismatch",
        "test_probe_19_selector_output_eligible_setup_missing_or_duplicate",
        "test_probe_20_selector_output_non_rank_1_winner_rejected",
    ]:
        suite.addTest(TestT538TamperProbes(test_name))
    with open(os.devnull, "w") as null_stream:
        runner = unittest.TextTestRunner(stream=null_stream, failfast=True)
        res = runner.run(suite)
    assert res.wasSuccessful(), f"Tamper probe tests failed: {res.errors} {res.failures}"
    print("FALSE_POSITIVE_TESTS_FIXED")


def test_duplicate_telemetry_rejected() -> None:
    sc = make_valid_scorecard(total_score=85.0)
    rec = SelectionAuditRecord(
        record_version="1.0.0",
        symbol="EURUSD",
        timeframe="15m",
        bar_index=100,
        timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"),
        decision_id="sel:100:SELECT:S01",
        action="SELECT",
        reason="ok",
        primary_strategy_id="S01",
        primary_setup_id="s1",
        direction="BUY",
        total_score=85.0,
        direction_conflict_present=False,
        best_buy_score=85.0,
        best_sell_score=None,
        cluster_id="c1",
        cluster_scorecards=(sc,),
        evaluated_count=1,
        eligible_count=1,
        rejected_count=0,
    )
    try:
        aggregate_selection_telemetry([rec, rec])
    except ValueError as exc:
        if "Duplicate telemetry record detected" in str(exc) or "Duplicate audit record detected" in str(exc):
            print("DUPLICATE_TELEMETRY_REJECTED")
            return
        raise AssertionError(f"Unexpected error: {exc}")
    raise AssertionError("Expected ValueError for duplicate audit record")


def main() -> None:
    test_audit_total_mismatch_rejected()
    test_select_without_scorecard_rejected()
    test_intra_record_mixed_weights_rejected()
    test_non_winning_primary_rejected()
    test_false_positive_tests_fixed()
    test_duplicate_telemetry_rejected()


if __name__ == "__main__":
    main()
