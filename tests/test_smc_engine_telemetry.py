"""
tests.test_smc_engine_telemetry
===============================
Unit tests for T53.8 — Selection Telemetry & Audit Engine.
Covers Test Matrix Group F (tests 106 to 125).
"""

from __future__ import annotations

import json
import unittest
import pandas as pd

from smc.engine.errors import StrictModelTypeError
from smc.engine.selector import ClusterScorecard
from smc.engine.telemetry import (
    SelectionAuditRecord,
    aggregate_selection_telemetry,
)


def _make_scorecard(
    cluster_id: str = "c1",
    direction: str = "BUY",
    primary_strategy_id: str = "S01",
    primary_setup_id: str = "s1",
    total_score: float = 85.0,
    scoring_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15),
    supporting_strategy_ids: tuple[str, ...] = (),
) -> ClusterScorecard:
    member_setup_ids = (primary_setup_id,)
    member_scores = {primary_setup_id: total_score}
    return ClusterScorecard(
        cluster_id=cluster_id,
        direction=direction,  # type: ignore[arg-type]
        primary_strategy_id=primary_strategy_id,
        primary_setup_id=primary_setup_id,
        supporting_strategy_ids=supporting_strategy_ids,
        total_score=total_score,
        regime_score=total_score,
        setup_score=total_score,
        context_score=total_score,
        exec_score=total_score,
        planned_rr=2.0,
        member_count=1,
        scoring_weights=scoring_weights,
        member_setup_ids=member_setup_ids,
        member_scores=member_scores,
        details={
            "member_setup_ids": list(member_setup_ids),
            "member_scores": dict(member_scores),
        },
    )


def _make_audit_record(
    bar_index: int = 100,
    timestamp: str = "2026-03-09 15:00:00+00:00",
    action: str = "SELECT",
    decision_id: str | None = None,
    primary_strategy_id: str | None = "S01",
    primary_setup_id: str | None = "s1",
    direction: str | None = "BUY",
    total_score: float | None = 85.0,
    score_gap: float | None = None,
    cluster_id: str | None = "c1",
    supporting_strategy_ids: tuple[str, ...] = (),
    reason: str = "ok",
    gate_reason_counts: dict[str, int] | None = None,
    evaluated_count: int | None = None,
    eligible_count: int | None = None,
    rejected_count: int = 1,
    record_version: str = "1.0.0",
    symbol: str = "EURUSD",
    timeframe: str = "15m",
    selector_version: str = "selector-v1",
    minimum_total_score: float = 60.0,
    minimum_direction_gap: float = 15.0,
    scoring_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15),
    cluster_scorecards: tuple[ClusterScorecard, ...] | None = None,
    direction_conflict_present: bool | None = None,
    best_buy_score: float | None = None,
    best_sell_score: float | None = None,
) -> SelectionAuditRecord:
    if action == "SELECT":
        if decision_id is None:
            decision_id = f"sel:{bar_index}:SELECT:{primary_strategy_id}"
        if cluster_scorecards is not None:
            scorecards = cluster_scorecards
        else:
            sc1 = _make_scorecard(
                cluster_id or "c1",
                direction or "BUY",
                primary_strategy_id or "S01",
                primary_setup_id or "s1",
                total_score or 85.0,
                scoring_weights=scoring_weights,
                supporting_strategy_ids=supporting_strategy_ids,
            )
            if score_gap is not None:
                opp_dir = "SELL" if (direction or "BUY") == "BUY" else "BUY"
                opp_score = round(max(0.0, (total_score or 85.0) - score_gap), 2)
                sc2 = _make_scorecard(
                    "c_opp",
                    opp_dir,
                    "S05",
                    "s_opp",
                    opp_score,
                    scoring_weights=scoring_weights,
                )
                from smc.engine.selector import cluster_rank_key
                scorecards = tuple(sorted([sc1, sc2], key=cluster_rank_key))
            else:
                scorecards = (sc1,)
    else:
        primary_strategy_id = None
        primary_setup_id = None
        direction = None
        total_score = None
        cluster_id = None
        supporting_strategy_ids = ()
        if decision_id is None:
            decision_id = f"sel:{bar_index}:NO_TRADE:none"
        if cluster_scorecards is not None:
            scorecards = cluster_scorecards
        elif reason == "conflicting_direction":
            sc1 = _make_scorecard("c1", "BUY", "S01", "s1", 80.0, scoring_weights=scoring_weights)
            sc2 = _make_scorecard("c2", "SELL", "S05", "s2", round(80.0 - (score_gap or 5.0), 2), scoring_weights=scoring_weights)
            from smc.engine.selector import cluster_rank_key
            scorecards = tuple(sorted([sc1, sc2], key=cluster_rank_key))
        else:
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
        from smc.engine.selector import cluster_rank_key
        best_buy_score = round(min(buy_cards, key=cluster_rank_key).total_score, 2)
    if best_sell_score is None and sell_cards:
        from smc.engine.selector import cluster_rank_key
        best_sell_score = round(min(sell_cards, key=cluster_rank_key).total_score, 2)

    return SelectionAuditRecord(
        record_version=record_version,
        symbol=symbol,
        timeframe=timeframe,
        bar_index=bar_index,
        timestamp=pd.Timestamp(timestamp),
        decision_id=decision_id,
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
        gate_reason_counts=gate_reason_counts or {"wrong_regime": 1},
        evaluated_count=evaluated_count,
        eligible_count=eligible_count,
        rejected_count=rejected_count,
        regime="bullish_trend",
        selector_version=selector_version,
        minimum_total_score=minimum_total_score,
        minimum_direction_gap=minimum_direction_gap,
        scoring_weights=scoring_weights,
        meta={},
    )


class TestSelectorGroupFTelemetry(unittest.TestCase):
    """Group F: SelectionAuditRecord & Telemetry Aggregation tests (tests 106 to 125)."""

    def test_106_audit_record_instantiation_select(self):
        r = _make_audit_record(action="SELECT")
        self.assertEqual(r.action, "SELECT")
        self.assertEqual(r.primary_strategy_id, "S01")
        self.assertEqual(r.primary_setup_id, "s1")
        self.assertEqual(r.direction, "BUY")
        self.assertEqual(r.total_score, 85.0)
        self.assertEqual(r.cluster_id, "c1")
        self.assertEqual(r.evaluated_count, 2)
        self.assertEqual(r.eligible_count, 1)
        self.assertEqual(r.rejected_count, 1)

    def test_107_audit_record_instantiation_no_trade(self):
        r = _make_audit_record(
            action="NO_TRADE",
            reason="conflicting_direction",
            score_gap=8.5,
            evaluated_count=2,
            eligible_count=2,
            rejected_count=0,
            gate_reason_counts={},
        )
        self.assertEqual(r.action, "NO_TRADE")
        self.assertIsNone(r.primary_strategy_id)
        self.assertIsNone(r.primary_setup_id)
        self.assertIsNone(r.direction)
        self.assertIsNone(r.total_score)
        self.assertIsNone(r.cluster_id)
        self.assertEqual(r.score_gap, 8.5)

    def test_108_audit_record_immutability(self):
        r = _make_audit_record()
        with self.assertRaises(Exception):
            r.action = "NO_TRADE"  # type: ignore

    def test_109_audit_record_to_dict_and_from_dict_roundtrip(self):
        r = _make_audit_record(score_gap=15.0, supporting_strategy_ids=("S05",))
        d = r.to_dict()
        restored = SelectionAuditRecord.from_dict(d)
        self.assertEqual(r, restored)

    def test_110_audit_record_json_roundtrip(self):
        r = _make_audit_record()
        payload = json.dumps(r.to_dict(), allow_nan=False)
        loaded = json.loads(payload)
        restored = SelectionAuditRecord.from_dict(loaded)
        self.assertEqual(r, restored)

    def test_111_audit_record_select_constraints(self):
        # action="SELECT" requires primary_strategy_id, primary_setup_id, direction, total_score, cluster_id
        with self.assertRaises(ValueError):
            _make_audit_record(action="SELECT", primary_strategy_id=None)
        with self.assertRaises(ValueError):
            _make_audit_record(action="SELECT", primary_setup_id=None)
        with self.assertRaises(ValueError):
            _make_audit_record(action="SELECT", direction=None)
        with self.assertRaises(ValueError):
            _make_audit_record(action="SELECT", total_score=None)
        with self.assertRaises(ValueError):
            _make_audit_record(action="SELECT", cluster_id=None)

    def test_112_audit_record_no_trade_constraints(self):
        # Direct constructor call violating NO_TRADE rules
        with self.assertRaises(ValueError):
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="conflicting_direction", primary_strategy_id="S01",
                evaluated_count=1, eligible_count=0, rejected_count=1,
            )
        with self.assertRaises(ValueError):
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="conflicting_direction", primary_setup_id="s1",
                evaluated_count=1, eligible_count=0, rejected_count=1,
            )
        with self.assertRaises(ValueError):
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="conflicting_direction", direction="BUY",
                evaluated_count=1, eligible_count=0, rejected_count=1,
            )
        with self.assertRaises(ValueError):
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="conflicting_direction", total_score=75.0,
                evaluated_count=1, eligible_count=0, rejected_count=1,
            )

    def test_113_audit_record_evaluated_count_mismatch_raises_error(self):
        with self.assertRaises(ValueError):
            _make_audit_record(evaluated_count=5, eligible_count=2, rejected_count=2)  # 5 != 2 + 2

    def test_114_audit_record_invalid_action_raises_error(self):
        with self.assertRaises(ValueError):
            _make_audit_record(action="INVALID")

    def test_115_audit_record_supporting_contains_primary_raises_error(self):
        with self.assertRaises(ValueError):
            _make_audit_record(primary_strategy_id="S01", supporting_strategy_ids=("S01",))

    def test_116_audit_record_duplicate_supporting_raises_error(self):
        with self.assertRaises(ValueError):
            _make_audit_record(supporting_strategy_ids=("S05", "S05"))

    def test_117_aggregate_selection_telemetry_empty_sequence(self):
        agg = aggregate_selection_telemetry([])
        self.assertEqual(agg["total_bars"], 0)
        self.assertEqual(agg["total_decisions"], 0)
        self.assertEqual(agg["select_count"], 0)
        self.assertEqual(agg["no_trade_count"], 0)
        self.assertEqual(agg["select_rate"], 0.0)
        self.assertEqual(agg["reasons"], {})
        self.assertEqual(agg["strategy_selections"], {})
        self.assertEqual(agg["direction_counts"], {"BUY": 0, "SELL": 0})
        self.assertIsNone(agg["score_stats"]["mean_total_score"])
        self.assertEqual(agg["symbols"], [])
        self.assertEqual(agg["timeframes"], [])
        self.assertIsNone(agg["bar_range"])

    def test_118_aggregate_selection_telemetry_single_record(self):
        r = _make_audit_record(bar_index=100, action="SELECT", total_score=85.0)
        agg = aggregate_selection_telemetry([r])
        self.assertEqual(agg["total_bars"], 1)
        self.assertEqual(agg["total_decisions"], 1)
        self.assertEqual(agg["select_count"], 1)
        self.assertEqual(agg["no_trade_count"], 0)
        self.assertEqual(agg["select_rate"], 1.0)
        self.assertEqual(agg["reasons"], {"ok": 1})
        self.assertEqual(agg["strategy_selections"], {"S01": 1})
        self.assertEqual(agg["direction_counts"], {"BUY": 1, "SELL": 0})
        self.assertEqual(agg["score_stats"]["mean_total_score"], 85.0)
        self.assertEqual(agg["bar_range"], [100, 100])

    def test_119_aggregate_selection_telemetry_multiple_records(self):
        r1 = _make_audit_record(bar_index=100, timestamp="2026-03-09 15:00:00+00:00", action="SELECT", total_score=80.0, primary_strategy_id="S01")
        r2 = _make_audit_record(bar_index=101, timestamp="2026-03-09 15:15:00+00:00", action="NO_TRADE", reason="conflicting_direction", score_gap=5.0, evaluated_count=2, eligible_count=2, rejected_count=0, gate_reason_counts={})
        r3 = _make_audit_record(bar_index=102, timestamp="2026-03-09 15:30:00+00:00", action="SELECT", total_score=90.0, primary_strategy_id="S05")

        agg = aggregate_selection_telemetry([r1, r2, r3])
        self.assertEqual(agg["total_bars"], 3)
        self.assertEqual(agg["total_decisions"], 3)
        self.assertEqual(agg["select_count"], 2)
        self.assertEqual(agg["no_trade_count"], 1)
        self.assertEqual(agg["select_rate"], 0.6667)
        self.assertEqual(agg["reasons"], {"conflicting_direction": 1, "ok": 2})
        self.assertEqual(agg["strategy_selections"], {"S01": 1, "S05": 1})
        self.assertEqual(agg["score_stats"]["mean_total_score"], 85.0)
        self.assertEqual(agg["score_stats"]["min_total_score"], 80.0)
        self.assertEqual(agg["score_stats"]["max_total_score"], 90.0)
        self.assertEqual(agg["score_stats"]["mean_score_gap"], 5.0)
        self.assertEqual(agg["bar_range"], [100, 102])

    def test_120_aggregate_selection_telemetry_anti_double_count_identical(self):
        r1 = _make_audit_record(bar_index=100, action="SELECT")
        r1_duplicate = _make_audit_record(bar_index=100, action="SELECT")
        # Under strict duplicate policy, duplicate key raises ValueError to fail closed
        with self.assertRaises(ValueError):
            aggregate_selection_telemetry([r1, r1_duplicate])

    def test_121_aggregate_selection_telemetry_conflicting_duplicate_raises_error(self):
        r1 = _make_audit_record(bar_index=100, action="SELECT", decision_id="sel:100:SELECT:S01", total_score=85.0)
        # Same (symbol, timeframe, decision_id) but conflicting score
        r1_conflict = _make_audit_record(bar_index=100, action="SELECT", decision_id="sel:100:SELECT:S01", total_score=90.0)
        with self.assertRaises(ValueError):
            aggregate_selection_telemetry([r1, r1_conflict])

    def test_122_aggregate_selection_telemetry_mixed_versions_raises_error(self):
        r1 = _make_audit_record(bar_index=100, record_version="1.0.0")
        r2 = _make_audit_record(bar_index=101, record_version="2.0.0")
        with self.assertRaises(ValueError):
            aggregate_selection_telemetry([r1, r2])

    def test_123_aggregate_selection_telemetry_gate_reasons_aggregate(self):
        r1 = _make_audit_record(bar_index=100, gate_reason_counts={"wrong_regime": 2, "outside_session": 1})
        r2 = _make_audit_record(bar_index=101, gate_reason_counts={"wrong_regime": 1, "expired_setup": 3})
        agg = aggregate_selection_telemetry([r1, r2])
        self.assertEqual(agg["gate_reasons_aggregate"], {
            "expired_setup": 3,
            "outside_session": 1,
            "wrong_regime": 3,
        })

    def test_124_aggregate_selection_telemetry_supporting_strategy_counts(self):
        r1 = _make_audit_record(bar_index=100, supporting_strategy_ids=("S05", "S09"))
        r2 = _make_audit_record(bar_index=101, supporting_strategy_ids=("S05",))
        agg = aggregate_selection_telemetry([r1, r2])
        self.assertEqual(agg["supporting_strategy_counts"], {
            "S05": 2,
            "S09": 1,
        })

    def test_125_aggregate_selection_telemetry_strictly_json_serializable(self):
        r1 = _make_audit_record(bar_index=100, score_gap=15.0)
        r2 = _make_audit_record(bar_index=101, action="NO_TRADE", reason="no_eligible_setup", evaluated_count=1, eligible_count=0, rejected_count=1)
        agg = aggregate_selection_telemetry([r1, r2])
        dumped = json.dumps(agg, allow_nan=False)
        loaded = json.loads(dumped)
        self.assertEqual(loaded["total_bars"], 2)
        self.assertEqual(loaded["select_count"], 1)
        self.assertEqual(loaded["no_trade_count"], 1)


class TestTelemetryQCRegression(unittest.TestCase):
    """P1 QC Regression tests for Telemetry & Aggregation."""

    def test_qc_telemetry_mixed_selector_version_rejected(self):
        r1 = _make_audit_record(bar_index=100, selector_version="selector-v1")
        r2 = _make_audit_record(bar_index=101, selector_version="selector-v2")
        with self.assertRaises(ValueError):
            aggregate_selection_telemetry([r1, r2])

    def test_qc_telemetry_mixed_minimum_total_score_rejected(self):
        r1 = _make_audit_record(bar_index=100, minimum_total_score=60.0)
        r2 = _make_audit_record(bar_index=101, minimum_total_score=65.0)
        with self.assertRaises(ValueError):
            aggregate_selection_telemetry([r1, r2])

    def test_qc_telemetry_mixed_minimum_direction_gap_rejected(self):
        r1 = _make_audit_record(bar_index=100, minimum_direction_gap=15.0)
        r2 = _make_audit_record(bar_index=101, minimum_direction_gap=20.0)
        with self.assertRaises(ValueError):
            aggregate_selection_telemetry([r1, r2])

    def test_qc_telemetry_mixed_scoring_weights_rejected(self):
        r1 = _make_audit_record(bar_index=100, scoring_weights=(0.25, 0.35, 0.25, 0.15))
        r2 = _make_audit_record(bar_index=101, scoring_weights=(0.20, 0.40, 0.25, 0.15))
        with self.assertRaises(ValueError):
            aggregate_selection_telemetry([r1, r2])

    def test_qc_telemetry_empty_and_populated_includes_config_fields(self):
        empty_agg = aggregate_selection_telemetry([])
        self.assertEqual(empty_agg["selector_version"], "selector-v1")
        self.assertEqual(empty_agg["minimum_total_score"], 60.0)
        self.assertEqual(empty_agg["minimum_direction_gap"], 15.0)
        self.assertEqual(empty_agg["scoring_weights"], [0.25, 0.35, 0.25, 0.15])

        r = _make_audit_record(bar_index=100, minimum_total_score=70.0, minimum_direction_gap=12.0)
        pop_agg = aggregate_selection_telemetry([r])
        self.assertEqual(pop_agg["selector_version"], "selector-v1")
        self.assertEqual(pop_agg["minimum_total_score"], 70.0)
        self.assertEqual(pop_agg["minimum_direction_gap"], 12.0)
        self.assertEqual(pop_agg["scoring_weights"], [0.25, 0.35, 0.25, 0.15])


class TestStandaloneAuditAndAggregatorInvariants(unittest.TestCase):
    """20+ Comprehensive Regression Tests for Standalone Audit Record Invariants & Aggregator Defense."""

    def test_01_audit_select_requires_non_empty_scorecards(self):
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                cluster_scorecards=(), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("at least one cluster scorecard", str(cm.exception))

    def test_02_audit_select_winner_cluster_id_not_in_scorecards(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c_other",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("must appear exactly once in cluster_scorecards", str(cm.exception))

    def test_03_audit_select_winner_primary_setup_id_mismatch(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s_mismatch",
                direction="BUY", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("primary_setup_id", str(cm.exception))

    def test_04_audit_select_winner_primary_strategy_id_mismatch(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S05",
                action="SELECT", reason="ok", primary_strategy_id="S05", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("primary_strategy_id", str(cm.exception))

    def test_05_audit_select_winner_direction_mismatch(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="SELL", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("direction", str(cm.exception))

    def test_06_audit_select_winner_supporting_strategies_mismatch(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0, supporting_strategy_ids=("S05",))
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1", supporting_strategy_ids=(),
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("supporting_strategy_ids", str(cm.exception))

    def test_07_audit_select_winner_total_score_mismatch(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=80.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("total_score", str(cm.exception))

    def test_08_audit_select_winner_score_below_minimum_rejected(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 55.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=55.0, cluster_id="c1", minimum_total_score=60.0,
                direction_conflict_present=False, best_buy_score=55.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("less than minimum_total_score", str(cm.exception))

    def test_09_audit_select_single_direction_winner_not_rank_1_rejected(self):
        sc_high = _make_scorecard("c_high", "BUY", "S01", "s_h", 85.0)
        sc_low = _make_scorecard("c_low", "BUY", "S05", "s_l", 70.0)
        scorecards = (sc_high, sc_low)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S05",
                action="SELECT", reason="ok", primary_strategy_id="S05", primary_setup_id="s_l",
                direction="BUY", total_score=70.0, cluster_id="c_low",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=scorecards, evaluated_count=2, eligible_count=2, rejected_count=0,
            )
        self.assertIn("must be rank 1", str(cm.exception))

    def test_10_audit_select_conflict_winner_direction_not_higher_score_rejected(self):
        sc_buy = _make_scorecard("c_buy", "BUY", "S01", "s_b", 80.0)
        sc_sell = _make_scorecard("c_sell", "SELL", "S05", "s_s", 60.0)
        from smc.engine.selector import cluster_rank_key
        scorecards = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S05",
                action="SELECT", reason="ok", primary_strategy_id="S05", primary_setup_id="s_s",
                direction="SELL", total_score=60.0, score_gap=20.0, cluster_id="c_sell",
                minimum_direction_gap=15.0,
                direction_conflict_present=True, best_buy_score=80.0, best_sell_score=60.0,
                cluster_scorecards=scorecards, evaluated_count=2, eligible_count=2, rejected_count=0,
            )
        self.assertIn("does not match higher-scoring direction", str(cm.exception))

    def test_11_audit_select_conflict_score_gap_mismatch_rejected(self):
        sc_buy = _make_scorecard("c_buy", "BUY", "S01", "s_b", 80.0)
        sc_sell = _make_scorecard("c_sell", "SELL", "S05", "s_s", 60.0)
        from smc.engine.selector import cluster_rank_key
        scorecards = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        # Recomputed gap is 20.0, but audit claims 18.0
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s_b",
                direction="BUY", total_score=80.0, score_gap=18.0, cluster_id="c_buy",
                minimum_direction_gap=15.0,
                direction_conflict_present=True, best_buy_score=80.0, best_sell_score=60.0,
                cluster_scorecards=scorecards, evaluated_count=2, eligible_count=2, rejected_count=0,
            )
        self.assertIn("score_gap", str(cm.exception))

    def test_12_audit_select_conflict_gap_below_minimum_rejected(self):
        sc_buy = _make_scorecard("c_buy", "BUY", "S01", "s_b", 80.0)
        sc_sell = _make_scorecard("c_sell", "SELL", "S05", "s_s", 70.0)
        from smc.engine.selector import cluster_rank_key
        scorecards = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        # Gap is 10.0, minimum is 15.0 -> Cannot SELECT!
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s_b",
                direction="BUY", total_score=80.0, score_gap=10.0, cluster_id="c_buy",
                minimum_direction_gap=15.0,
                direction_conflict_present=True, best_buy_score=80.0, best_sell_score=70.0,
                cluster_scorecards=scorecards, evaluated_count=2, eligible_count=2, rejected_count=0,
            )
        self.assertIn("less than minimum_direction_gap", str(cm.exception))

    def test_13_audit_no_trade_no_eligible_with_scorecards_rejected(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 80.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="no_eligible_setup",
                direction_conflict_present=False, best_buy_score=80.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=0, rejected_count=1,
            )
        self.assertIn("cluster_scorecards must be empty", str(cm.exception))

    def test_14_audit_no_trade_no_eligible_with_nonzero_eligible_count_rejected(self):
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="no_eligible_setup",
                cluster_scorecards=(), evaluated_count=2, eligible_count=1, rejected_count=1,
            )
        self.assertIn("eligible_count must be 0", str(cm.exception))

    def test_15_audit_no_trade_insufficient_score_with_empty_scorecards_rejected(self):
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="insufficient_score",
                cluster_scorecards=(), evaluated_count=1, eligible_count=0, rejected_count=1,
            )
        self.assertIn("cannot be empty for reason 'insufficient_score'", str(cm.exception))

    def test_16_audit_no_trade_insufficient_score_with_winning_score_above_minimum_rejected(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 75.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="insufficient_score", minimum_total_score=60.0,
                direction_conflict_present=False, best_buy_score=75.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("invalid for 'insufficient_score'", str(cm.exception))

    def test_17_audit_no_trade_conflict_missing_opposing_direction_rejected(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 70.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="conflicting_direction", score_gap=5.0,
                direction_conflict_present=False, best_buy_score=70.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("Both BUY and SELL scorecards must exist", str(cm.exception))

    def test_18_audit_no_trade_conflict_gap_above_minimum_rejected(self):
        sc_buy = _make_scorecard("c_buy", "BUY", "S01", "s_b", 80.0)
        sc_sell = _make_scorecard("c_sell", "SELL", "S05", "s_s", 60.0)
        from smc.engine.selector import cluster_rank_key
        scorecards = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        # Gap is 20.0 >= minimum 15.0 -> Should SELECT, cannot be conflicting_direction!
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="conflicting_direction", score_gap=20.0,
                minimum_direction_gap=15.0,
                direction_conflict_present=True, best_buy_score=80.0, best_sell_score=60.0,
                cluster_scorecards=scorecards, evaluated_count=2, eligible_count=2, rejected_count=0,
            )
        self.assertIn("invalid for 'conflicting_direction'", str(cm.exception))

    def test_19_audit_no_trade_conflict_score_gap_mismatch_rejected(self):
        sc_buy = _make_scorecard("c_buy", "BUY", "S01", "s_b", 80.0)
        sc_sell = _make_scorecard("c_sell", "SELL", "S05", "s_s", 75.0)
        from smc.engine.selector import cluster_rank_key
        scorecards = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        # Recomputed gap is 5.0, but audit claims 8.0
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:NO_TRADE:none",
                action="NO_TRADE", reason="conflicting_direction", score_gap=8.0,
                minimum_direction_gap=15.0,
                direction_conflict_present=True, best_buy_score=80.0, best_sell_score=75.0,
                cluster_scorecards=scorecards, evaluated_count=2, eligible_count=2, rejected_count=0,
            )
        self.assertIn("does not match recomputed gap", str(cm.exception))

    def test_20_audit_scorecard_weights_mismatch_audit_weights_rejected(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0, scoring_weights=(0.30, 0.30, 0.25, 0.15))
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                scoring_weights=(0.25, 0.35, 0.25, 0.15),
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("scoring_weights", str(cm.exception))

    def test_21_scorecard_primary_score_less_than_member_score_rejected(self):
        # Option B enforcement in ClusterScorecard constructor
        with self.assertRaises(ValueError) as cm:
            ClusterScorecard(
                cluster_id="c1", direction="BUY", primary_strategy_id="S01", primary_setup_id="s1",
                supporting_strategy_ids=(), total_score=70.0, regime_score=70.0, setup_score=70.0,
                context_score=70.0, exec_score=70.0, planned_rr=2.0, member_count=2,
                member_setup_ids=("s1", "s2"), member_scores={"s1": 70.0, "s2": 85.0},
            )
        self.assertIn("is less than maximum member score", str(cm.exception))

    def test_22_audit_eligible_count_mismatch_scorecard_members_rejected(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=(sc,), evaluated_count=3, eligible_count=2, rejected_count=1,
            )
        self.assertIn("does not match total unique member setup count", str(cm.exception))

    def test_23_audit_duplicate_member_across_scorecards_rejected(self):
        sc1 = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        sc2 = _make_scorecard("c2", "BUY", "S05", "s1", 80.0)  # Duplicate s1!
        from smc.engine.selector import cluster_rank_key
        scorecards = tuple(sorted([sc1, sc2], key=cluster_rank_key))
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=None,
                cluster_scorecards=scorecards, evaluated_count=2, eligible_count=1, rejected_count=1,
            )
        self.assertIn("appears in multiple cluster scorecards", str(cm.exception))

    def test_24_telemetry_aggregator_defensive_validation_rejects_inconsistent_record(self):
        valid_rec = _make_audit_record(bar_index=100)
        # Verify normal aggregation succeeds
        agg = aggregate_selection_telemetry([valid_rec])
        self.assertEqual(agg["total_bars"], 1)

        # Mutate record via object.__setattr__ to simulate tampered or forged record
        bad_rec = _make_audit_record(bar_index=101)
        object.__setattr__(bad_rec, "total_score", 99.0)  # Mismatched with scorecard 85.0
        with self.assertRaises(ValueError) as cm:
            aggregate_selection_telemetry([valid_rec, bad_rec])
        self.assertIn("audit total_score 99.0 does not match winning scorecard total_score", str(cm.exception))


class TestAuditRecordStrictFromDictRoundTrip(unittest.TestCase):
    """
    P1 — Strict Fail-Closed from_dict() and Exact Round-Trip.
    Verifies that for every record type (SELECT and all three NO_TRADE reasons):
    1. Every single key emitted by to_dict() is strictly required. Deleting any key raises KeyError.
    2. Unknown fields raise KeyError.
    3. Exact JSON round-trip json.loads(json.dumps(record.to_dict())) deserializes cleanly.
    """

    def _build_test_records(self) -> list[tuple[str, SelectionAuditRecord]]:
        from smc.engine.selector import cluster_rank_key
        # 1. SELECT
        rec_select = _make_audit_record(
            bar_index=100,
            action="SELECT",
            reason="ok",
            primary_strategy_id="S01",
            primary_setup_id="s1",
            direction="BUY",
            total_score=85.0,
            score_gap=None,
        )

        # 2. NO_TRADE / no_eligible_setup
        rec_no_eligible = _make_audit_record(
            bar_index=101,
            action="NO_TRADE",
            reason="no_eligible_setup",
            cluster_scorecards=(),
            rejected_count=1,
            gate_reason_counts={"min_rr": 1},
        )

        # 3. NO_TRADE / insufficient_score
        sc_low = _make_scorecard("c1", "BUY", "S01", "s1", 50.0)
        rec_insufficient = _make_audit_record(
            bar_index=102,
            action="NO_TRADE",
            reason="insufficient_score",
            cluster_scorecards=(sc_low,),
            rejected_count=0,
        )

        # 4. NO_TRADE / conflicting_direction
        sc_buy = _make_scorecard("c1", "BUY", "S01", "s1", 75.0)
        sc_sell = _make_scorecard("c2", "SELL", "S05", "s2", 74.0)
        scs = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        rec_conflict = _make_audit_record(
            bar_index=103,
            action="NO_TRADE",
            reason="conflicting_direction",
            score_gap=1.0,
            cluster_scorecards=scs,
            rejected_count=0,
        )

        return [
            ("SELECT", rec_select),
            ("NO_TRADE/no_eligible_setup", rec_no_eligible),
            ("NO_TRADE/insufficient_score", rec_insufficient),
            ("NO_TRADE/conflicting_direction", rec_conflict),
        ]

    def test_missing_any_required_key_raises_key_error(self):
        """Table-driven: deleting any single serialized key must raise KeyError."""
        records = self._build_test_records()
        for name, record in records:
            payload = record.to_dict()
            self.assertEqual(len(payload), 29, f"Record '{name}' to_dict() must emit exactly 29 keys")
            for key in payload.keys():
                with self.subTest(record_type=name, missing_key=key):
                    tampered = dict(payload)
                    del tampered[key]
                    with self.assertRaises(KeyError) as cm:
                        SelectionAuditRecord.from_dict(tampered)
                    self.assertIn(key, str(cm.exception))

    def test_unknown_field_rejected_with_key_error(self):
        """Injecting unknown fields into serialized payload must raise KeyError."""
        records = self._build_test_records()
        for name, record in records:
            payload = record.to_dict()
            with self.subTest(record_type=name):
                tampered = dict(payload)
                tampered["unexpected_extra_field"] = 123
                with self.assertRaises(KeyError) as cm:
                    SelectionAuditRecord.from_dict(tampered)
                self.assertIn("unexpected_extra_field", str(cm.exception))

    def test_exact_json_round_trip(self):
        """Exact JSON round-trip: json.loads(json.dumps(record.to_dict())) -> from_dict() -> to_dict()."""
        records = self._build_test_records()
        for name, record in records:
            with self.subTest(record_type=name):
                original_dict = record.to_dict()
                json_str = json.dumps(original_dict)
                restored_dict = json.loads(json_str)
                restored_record = SelectionAuditRecord.from_dict(restored_dict)
                self.assertEqual(restored_record.to_dict(), original_dict)


class TestAuditRecordDirectionalSnapshot(unittest.TestCase):
    """
    P1 — Directional Snapshot Contract:
    direction_conflict_present, best_buy_score, best_sell_score.
    """

    def test_01_no_scorecards(self):
        # Empty scorecards (no_eligible_setup)
        rec = _make_audit_record(
            bar_index=100,
            action="NO_TRADE",
            reason="no_eligible_setup",
            cluster_scorecards=(),
            rejected_count=1,
        )
        self.assertFalse(rec.direction_conflict_present)
        self.assertIsNone(rec.best_buy_score)
        self.assertIsNone(rec.best_sell_score)

    def test_02_buy_only(self):
        sc = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        rec = _make_audit_record(
            bar_index=100,
            action="SELECT",
            cluster_scorecards=(sc,),
            total_score=85.0,
            direction="BUY",
        )
        self.assertFalse(rec.direction_conflict_present)
        self.assertEqual(rec.best_buy_score, 85.0)
        self.assertIsNone(rec.best_sell_score)

    def test_03_sell_only(self):
        sc = _make_scorecard("c1", "SELL", "S05", "s1", 88.0)
        rec = _make_audit_record(
            bar_index=100,
            action="SELECT",
            cluster_scorecards=(sc,),
            total_score=88.0,
            direction="SELL",
            primary_strategy_id="S05",
        )
        self.assertFalse(rec.direction_conflict_present)
        self.assertIsNone(rec.best_buy_score)
        self.assertEqual(rec.best_sell_score, 88.0)

    def test_04_both_buy_and_sell(self):
        from smc.engine.selector import cluster_rank_key
        sc_buy = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        sc_sell = _make_scorecard("c2", "SELL", "S05", "s2", 65.0)
        scs = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        rec = _make_audit_record(
            bar_index=100,
            action="SELECT",
            cluster_scorecards=scs,
            total_score=85.0,
            score_gap=20.0,
            direction="BUY",
        )
        self.assertTrue(rec.direction_conflict_present)
        self.assertEqual(rec.best_buy_score, 85.0)
        self.assertEqual(rec.best_sell_score, 65.0)

    def test_05_multiple_clusters_same_direction_winner_via_rank_key(self):
        from smc.engine.selector import cluster_rank_key
        sc1 = _make_scorecard("c1", "BUY", "S01", "s1", 75.0)
        sc2 = _make_scorecard("c2", "BUY", "S05", "s2", 82.5)
        scs = tuple(sorted([sc1, sc2], key=cluster_rank_key))
        # sc2 has higher score 82.5, so it is ranked higher (smaller rank_key)
        rec = _make_audit_record(
            bar_index=100,
            action="SELECT",
            cluster_scorecards=scs,
            cluster_id="c2",
            primary_strategy_id="S05",
            primary_setup_id="s2",
            total_score=82.5,
            direction="BUY",
        )
        self.assertFalse(rec.direction_conflict_present)
        self.assertEqual(rec.best_buy_score, 82.5)
        self.assertIsNone(rec.best_sell_score)

    def test_06_equal_total_score_canonical_tie_break(self):
        from smc.engine.selector import cluster_rank_key
        # Two BUY clusters with identical total score 80.0, but c2 has higher planned_rr
        sc1 = ClusterScorecard(
            cluster_id="c1", direction="BUY", primary_strategy_id="S01", primary_setup_id="s1",
            supporting_strategy_ids=(), total_score=80.0, regime_score=80.0, setup_score=80.0,
            context_score=80.0, exec_score=80.0, planned_rr=1.5, member_count=1,
            member_setup_ids=("s1",), member_scores={"s1": 80.0},
        )
        sc2 = ClusterScorecard(
            cluster_id="c2", direction="BUY", primary_strategy_id="S05", primary_setup_id="s2",
            supporting_strategy_ids=(), total_score=80.0, regime_score=80.0, setup_score=80.0,
            context_score=80.0, exec_score=80.0, planned_rr=2.5, member_count=1,
            member_setup_ids=("s2",), member_scores={"s2": 80.0},
        )
        # sc2 has higher planned_rr, so cluster_rank_key(sc2) < cluster_rank_key(sc1)
        self.assertLess(cluster_rank_key(sc2), cluster_rank_key(sc1))
        scs = tuple(sorted([sc1, sc2], key=cluster_rank_key))
        self.assertEqual(scs[0].cluster_id, "c2")

        rec = _make_audit_record(
            bar_index=100,
            action="SELECT",
            cluster_scorecards=scs,
            cluster_id="c2",
            primary_strategy_id="S05",
            primary_setup_id="s2",
            total_score=80.0,
            direction="BUY",
        )
        self.assertEqual(rec.best_buy_score, 80.0)

    def test_07_tamper_directional_fields_fail_closed(self):
        from smc.engine.selector import cluster_rank_key
        sc_buy = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        sc_sell = _make_scorecard("c2", "SELL", "S05", "s2", 65.0)
        scs = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))

        # 7a: conflict flag false when both exist
        with self.assertRaises(ValueError) as cm:
            _make_audit_record(
                cluster_scorecards=scs,
                total_score=85.0,
                score_gap=20.0,
                direction_conflict_present=False,  # False conflict!
                best_buy_score=85.0,
                best_sell_score=65.0,
            )
        self.assertIn("direction_conflict_present", str(cm.exception))

        # 7b: conflict flag true when only BUY exists
        with self.assertRaises(ValueError) as cm:
            _make_audit_record(
                cluster_scorecards=(sc_buy,),
                total_score=85.0,
                direction_conflict_present=True,  # False conflict!
                best_buy_score=85.0,
                best_sell_score=None,
            )
        self.assertIn("direction_conflict_present", str(cm.exception))

        # 7c: missing best_buy_score when BUY exists
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=None, best_sell_score=None,
                cluster_scorecards=(sc_buy,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("best_buy_score cannot be None", str(cm.exception))

        # 7d: wrong score value
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=70.0, best_sell_score=None,
                cluster_scorecards=(sc_buy,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("does not match winner scorecard score", str(cm.exception))

        # 7e: non-finite / bool score
        for bad_score in [float("nan"), float("inf"), True, -5.0, 105.0]:
            with self.subTest(bad_score=bad_score):
                with self.assertRaises((ValueError, StrictModelTypeError)):
                    SelectionAuditRecord(
                        record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                        timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                        action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                        direction="BUY", total_score=85.0, cluster_id="c1",
                        direction_conflict_present=False, best_buy_score=bad_score, best_sell_score=None,
                        cluster_scorecards=(sc_buy,), evaluated_count=1, eligible_count=1, rejected_count=0,
                    )

        # 7f: direction does not exist but best_sell_score provided
        with self.assertRaises(ValueError) as cm:
            SelectionAuditRecord(
                record_version="1.0.0", symbol="EURUSD", timeframe="15m", bar_index=100,
                timestamp=pd.Timestamp("2026-03-09 15:00:00+00:00"), decision_id="sel:100:SELECT:S01",
                action="SELECT", reason="ok", primary_strategy_id="S01", primary_setup_id="s1",
                direction="BUY", total_score=85.0, cluster_id="c1",
                direction_conflict_present=False, best_buy_score=85.0, best_sell_score=75.0,
                cluster_scorecards=(sc_buy,), evaluated_count=1, eligible_count=1, rejected_count=0,
            )
        self.assertIn("best_sell_score must be None when no SELL scorecards exist", str(cm.exception))

    def test_08_exact_json_round_trip_with_directional_fields(self):
        from smc.engine.selector import cluster_rank_key
        sc_buy = _make_scorecard("c1", "BUY", "S01", "s1", 85.0)
        sc_sell = _make_scorecard("c2", "SELL", "S05", "s2", 65.0)
        scs = tuple(sorted([sc_buy, sc_sell], key=cluster_rank_key))
        rec = _make_audit_record(
            bar_index=100,
            action="SELECT",
            cluster_scorecards=scs,
            total_score=85.0,
            score_gap=20.0,
            direction="BUY",
        )
        payload = rec.to_dict()
        self.assertTrue(payload["direction_conflict_present"])
        self.assertEqual(payload["best_buy_score"], 85.0)
        self.assertEqual(payload["best_sell_score"], 65.0)

        restored = SelectionAuditRecord.from_dict(json.loads(json.dumps(payload)))
        self.assertEqual(restored.to_dict(), payload)
        self.assertTrue(restored.direction_conflict_present)
        self.assertEqual(restored.best_buy_score, 85.0)
        self.assertEqual(restored.best_sell_score, 65.0)


if __name__ == "__main__":
    unittest.main()

