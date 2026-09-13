"""
tests.test_smc_engine_confluence
================================
Unit tests for T53.7.3 — Evidence Deduplication, Candidate Clustering,
Direction Conflict Detection, and ConfluenceBatch Assembly.
Covers Test Matrix Group A (models 7-11) and Group F (tests 117-136).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import MappingProxyType
import pandas as pd

from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    MarketRegime,
    StrategyEvaluation,
)
from smc.engine.context import StrategyContext
from smc.engine.errors import StrategyStateError, StrictModelTypeError
from smc.engine.confluence import (
    EvidenceCluster,
    DirectionConflict,
    ConfluenceBatch,
    EvidenceDeduplicator,
    DirectionConflictDetector,
    build_confluence_batch,
)


def _make_sample_context(bar_index: int = 100, timestamp: str = "2026-03-09 15:00:00+00:00") -> StrategyContext:
    ts = pd.Timestamp(timestamp)
    return StrategyContext(
        symbol="EURUSD",
        timeframe="15m",
        bar_index=bar_index,
        timestamp=ts - pd.Timedelta(minutes=15),
        bar_close_time=ts,
        open=2000.0,
        high=2050.0,
        low=1990.0,
        close=2040.0,
        volume=100.0,
        atr14=5.0,
    )


def _make_candidate(
    setup_id: str,
    strategy_id: str,
    direction: str = "BUY",
    bar_index: int = 100,
    timestamp: str = "2026-03-09 15:00:00+00:00",
    cluster_id: str = "cluster_1",
    evidences: tuple[EvidenceRef, ...] = (),
    structure_leg_id: str | None = None,
    entry: float = 2000.0,
    sl: float = 1990.0,
    tp: float = 2020.0,
) -> CandidateSetup:
    meta = {}
    if structure_leg_id is not None:
        meta["structure_leg_id"] = structure_leg_id
    if direction == "BUY":
        assert sl < entry < tp
    else:
        assert tp < entry < sl
    if not evidences:
        evidences = (EvidenceRef(f"ev_{setup_id}", "fair_value_gap", bar_index - 5, entry),)

    return CandidateSetup(
        setup_id=setup_id,
        strategy_id=strategy_id,
        direction=direction,  # type: ignore[arg-type]
        bar_index=bar_index,
        timestamp=pd.Timestamp(timestamp),
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        planned_rr=2.0,
        evidences=evidences,
        evidence_cluster_id=cluster_id,
        expiry_bar=bar_index + 10,
        meta=meta,
    )


def _make_eval(
    candidate: CandidateSetup,
    status: str = "ELIGIBLE",
    reasons: tuple[str, ...] = (),
) -> StrategyEvaluation:
    return StrategyEvaluation(
        candidate=candidate,
        status=status,  # type: ignore[arg-type]
        rejection_reasons=reasons,
        regime_score=80.0 if status == "ELIGIBLE" else 0.0,
        setup_score=85.0 if status == "ELIGIBLE" else 0.0,
        context_score=75.0 if status == "ELIGIBLE" else 0.0,
        exec_score=90.0 if status == "ELIGIBLE" else 0.0,
        total_score=82.5 if status == "ELIGIBLE" else 0.0,
        details={"evaluated": True},
    )


def _make_regime(bar_index: int = 100, timestamp: str = "2026-03-09 15:00:00+00:00") -> MarketRegime:
    return MarketRegime(
        regime="bullish_trend",
        bar_index=bar_index,
        timestamp=pd.Timestamp(timestamp),
        efficiency_ratio=0.55,
        atr_percentile=55.0,
        reason="trend_continuation",
    )


class TestSMCEngineConfluence(unittest.TestCase):
    def setUp(self):
        self.context = _make_sample_context()
        self.regime = _make_regime()

    # =========================================================================
    # Group A: Model Contracts (Tests 7-11)
    # =========================================================================

    def test_007_evidence_cluster_deep_immutability(self):
        """Test 7: EvidenceCluster deep immutability."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand = _make_candidate("cand_1", "S01", evidences=(ev,))
        evaluation = _make_eval(cand)

        cluster = EvidenceCluster(
            cluster_id="cluster_alpha",
            direction="BUY",
            members=(evaluation,),
            meta={"note": "immutability_test", "sub": {"k": 1}},
        )
        self.assertEqual(cluster.cluster_id, "cluster_alpha")
        self.assertIsInstance(cluster.members, tuple)
        self.assertIsInstance(cluster.meta, MappingProxyType)
        self.assertIsInstance(cluster.meta["sub"], MappingProxyType)

        from dataclasses import FrozenInstanceError

        with self.assertRaises((FrozenInstanceError, AttributeError)):
            cluster.cluster_id = "new_alpha"  # type: ignore[misc]

        with self.assertRaises(TypeError):
            cluster.meta["note"] = "mutated"  # type: ignore[index]

    def test_008_direction_conflict_validation(self):
        """Test 8: DirectionConflict validation."""
        # Valid construction
        dc = DirectionConflict(
            bar_index=100,
            buy_cluster_ids=("cid_buy_1",),
            sell_cluster_ids=("cid_sell_1",),
        )
        self.assertEqual(dc.bar_index, 100)
        self.assertEqual(dc.reason, "conflicting_direction")

        # Negative bar index
        with self.assertRaises((ValueError, StrictModelTypeError)):
            DirectionConflict(
                bar_index=-1,
                buy_cluster_ids=("cid_buy_1",),
                sell_cluster_ids=("cid_sell_1",),
            )

        # Empty buy_cluster_ids
        with self.assertRaises(ValueError):
            DirectionConflict(
                bar_index=100,
                buy_cluster_ids=(),
                sell_cluster_ids=("cid_sell_1",),
            )

        # Empty sell_cluster_ids
        with self.assertRaises(ValueError):
            DirectionConflict(
                bar_index=100,
                buy_cluster_ids=("cid_buy_1",),
                sell_cluster_ids=(),
            )

        # Invalid reason
        with self.assertRaises(ValueError):
            DirectionConflict(
                bar_index=100,
                buy_cluster_ids=("cid_buy_1",),
                sell_cluster_ids=("cid_sell_1",),
                reason="custom_reason",
            )

    def test_009_confluence_batch_cross_field_validation(self):
        """Test 9: ConfluenceBatch cross-field validation."""
        cand = _make_candidate("cand_1", "S01")
        evaluation = _make_eval(cand)
        cluster = EvidenceCluster(
            cluster_id="cid_buy",
            direction="BUY",
            members=(evaluation,),
        )

        # Mismatched regime bar_index
        bad_regime_bar = MarketRegime(
            regime="bullish_trend",
            bar_index=101,  # mismatch
            timestamp=self.context.bar_close_time,
            efficiency_ratio=0.55,
            atr_percentile=55.0,
            reason="trend",
        )
        with self.assertRaises(ValueError):
            ConfluenceBatch(
                bar_index=100,
                timestamp=self.context.bar_close_time,
                regime=bad_regime_bar,
                evaluations=(evaluation,),
                eligible_clusters=(cluster,),
            )

        # Missing conflict when both BUY and SELL clusters exist
        cand_sell = _make_candidate("cand_sell_1", "S05", direction="SELL", entry=2040.0, sl=2050.0, tp=2020.0)
        eval_sell = _make_eval(cand_sell)
        cluster_sell = EvidenceCluster(
            cluster_id="cid_sell",
            direction="SELL",
            members=(eval_sell,),
        )
        with self.assertRaises(ValueError):
            # Both BUY and SELL clusters but conflict=None
            ConfluenceBatch(
                bar_index=100,
                timestamp=self.context.bar_close_time,
                regime=self.regime,
                evaluations=(evaluation, eval_sell),
                eligible_clusters=(cluster, cluster_sell),
                direction_conflict=None,
            )

        # Providing conflict when only BUY clusters exist
        conflict_spurious = DirectionConflict(
            bar_index=100,
            buy_cluster_ids=("cid_buy",),
            sell_cluster_ids=("cid_sell",),
        )
        with self.assertRaises(ValueError):
            ConfluenceBatch(
                bar_index=100,
                timestamp=self.context.bar_close_time,
                regime=self.regime,
                evaluations=(evaluation,),
                eligible_clusters=(cluster,),
                direction_conflict=conflict_spurious,
            )

    def test_010_duplicate_strategy_setup_ids_reject_contract(self):
        """Test 10: Duplicate strategy/setup IDs reject on conflicting payload."""
        cand1 = _make_candidate("dup_id", "S01", entry=2000.0)
        cand2 = _make_candidate("dup_id", "S01", entry=2005.0)  # different payload
        eval1 = _make_eval(cand1)
        eval2 = _make_eval(cand2)

        dedup = EvidenceDeduplicator()
        with self.assertRaises(StrategyStateError):
            dedup.deduplicate([eval1, eval2])

    def test_011_exact_json_roundtrip_three_models(self):
        """Test 11: Exact JSON round-trip for EvidenceCluster, DirectionConflict, and ConfluenceBatch."""
        cand = _make_candidate("cand_1", "S01")
        evaluation = _make_eval(cand)
        cluster = EvidenceCluster(
            cluster_id="cid_buy_1",
            direction="BUY",
            members=(evaluation,),
            meta={"info": "json_test"},
        )
        c_dict = cluster.to_dict()
        cluster_restored = EvidenceCluster.from_dict(c_dict)
        self.assertEqual(cluster.cluster_id, cluster_restored.cluster_id)
        self.assertEqual(cluster.direction, cluster_restored.direction)
        self.assertEqual(len(cluster.members), len(cluster_restored.members))
        self.assertEqual(cluster.members[0].candidate.setup_id, cluster_restored.members[0].candidate.setup_id)

        dc = DirectionConflict(
            bar_index=100,
            buy_cluster_ids=("cid_buy_1",),
            sell_cluster_ids=("cid_sell_1",),
        )
        dc_dict = dc.to_dict()
        dc_restored = DirectionConflict.from_dict(dc_dict)
        self.assertEqual(dc, dc_restored)

        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=self.context.bar_close_time,
            regime=self.regime,
            evaluations=(evaluation,),
            eligible_clusters=(cluster,),
            direction_conflict=None,
            meta={"version": "1.0"},
        )
        b_dict = batch.to_dict()
        batch_restored = ConfluenceBatch.from_dict(b_dict)
        self.assertEqual(batch.bar_index, batch_restored.bar_index)
        self.assertEqual(batch.regime.regime, batch_restored.regime.regime)
        self.assertEqual(len(batch.eligible_clusters), 1)

    def test_012_confluence_batch_cluster_member_not_in_evaluations_raises(self):
        """Test 12 (P1.6): ConfluenceBatch rejects clusters whose members do not belong to evaluations."""
        cand_a = _make_candidate("cand_a", "S01")
        cand_b = _make_candidate("cand_b", "S05")
        eval_a = _make_eval(cand_a)
        eval_b = _make_eval(cand_b)

        cluster_b = EvidenceCluster(
            cluster_id="cluster_b",
            direction="BUY",
            members=(eval_b,),
        )
        # evaluations only has eval_a, but cluster has eval_b!
        with self.assertRaises(ValueError) as cm:
            ConfluenceBatch(
                bar_index=100,
                timestamp=self.context.bar_close_time,
                regime=self.regime,
                evaluations=(eval_a,),
                eligible_clusters=(cluster_b,),
            )
        self.assertIn("not found in evaluations", str(cm.exception))

    def test_013_confluence_batch_eligible_eval_not_in_cluster_raises(self):
        """Test 13 (P1.6): ConfluenceBatch rejects when an ELIGIBLE evaluation is not in any cluster."""
        cand = _make_candidate("cand_unclustered", "S01")
        eval_eligible = _make_eval(cand, status="ELIGIBLE")

        with self.assertRaises(ValueError) as cm:
            ConfluenceBatch(
                bar_index=100,
                timestamp=self.context.bar_close_time,
                regime=self.regime,
                evaluations=(eval_eligible,),
                eligible_clusters=(),
            )
        self.assertIn("not assigned to any cluster", str(cm.exception))

    def test_014_confluence_batch_duplicate_setup_id_conflict_raises(self):
        """Test 14 (P1.6): ConfluenceBatch with duplicate setup_id conflicting payload raises StrategyStateError."""
        cand1 = _make_candidate("setup_dup", "S01", entry=2000.0)
        cand2 = _make_candidate("setup_dup", "S01", entry=2005.0)  # conflicting payload
        eval1 = _make_eval(cand1)
        eval2 = _make_eval(cand2)

        with self.assertRaises(StrategyStateError) as cm:
            ConfluenceBatch(
                bar_index=100,
                timestamp=self.context.bar_close_time,
                regime=self.regime,
                evaluations=(eval1, eval2),
                eligible_clusters=(),
            )
        self.assertIn("conflicting payload", str(cm.exception).lower())

    def test_015_direction_conflict_overlapping_ids_raises(self):
        """Test 15 (P1.6): DirectionConflict with overlapping BUY and SELL cluster IDs raises ValueError."""
        with self.assertRaises(ValueError) as cm:
            DirectionConflict(
                bar_index=100,
                buy_cluster_ids=("c_overlap", "c_buy"),
                sell_cluster_ids=("c_overlap", "c_sell"),
            )
        self.assertIn("overlap", str(cm.exception).lower())

    # =========================================================================
    # Group F: Dedup and Conflict (Tests 117-136)
    # =========================================================================

    def test_117_one_candidate_one_cluster(self):
        """Test 117: One candidate → one cluster."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand = _make_candidate("cand_1", "S01", evidences=(ev,))
        evaluation = _make_eval(cand)

        dedup = EvidenceDeduplicator()
        clusters = dedup.deduplicate([evaluation])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].cluster_id, "cluster_1")
        self.assertEqual(clusters[0].direction, "BUY")
        self.assertEqual(len(clusters[0].members), 1)

    def test_118_s01_s09_same_cluster_merge(self):
        """Test 118: S01/S09 same cluster merge."""
        ev_sweep = EvidenceRef("sweep_1", "liquidity_sweep", 90, 1990.0)
        ev_mss = EvidenceRef("mss_1", "structure_event", 93, 2010.0, details={"structure_leg_id": "leg_10"})
        ev_fvg = EvidenceRef("fvg_1", "fair_value_gap", 95, 2000.0, details={"structure_leg_id": "leg_10"})

        cand_s01 = _make_candidate(
            "s01_setup", "S01", cluster_id="shared_cluster",
            evidences=(ev_sweep, ev_mss, ev_fvg), structure_leg_id="leg_10"
        )
        cand_s09 = _make_candidate(
            "s09_setup", "S09", cluster_id="shared_cluster",
            evidences=(ev_sweep, ev_mss, ev_fvg), structure_leg_id="leg_10"
        )

        eval_s01 = _make_eval(cand_s01)
        eval_s09 = _make_eval(cand_s09)

        dedup = EvidenceDeduplicator()
        clusters = dedup.deduplicate([eval_s01, eval_s09])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].cluster_id, "shared_cluster")
        self.assertEqual(len(clusters[0].members), 2)
        self.assertEqual(clusters[0].strategy_ids, ("S01", "S09"))

    def test_119_shared_evidence_union_unique(self):
        """Test 119: Shared evidence union unique."""
        ev1 = EvidenceRef("ev_common", "liquidity_sweep", 90, 1990.0)
        ev2 = EvidenceRef("ev_s01_only", "fair_value_gap", 95, 2000.0)
        ev3 = EvidenceRef("ev_s09_only", "liquidity_pool", 98, 2020.0)

        cand_s01 = _make_candidate("s01_setup", "S01", cluster_id="cluster_x", evidences=(ev1, ev2))
        cand_s09 = _make_candidate("s09_setup", "S09", cluster_id="cluster_x", evidences=(ev1, ev3))

        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand_s01), _make_eval(cand_s09)])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].evidence_ids, ("ev_common", "ev_s01_only", "ev_s09_only"))
        self.assertEqual(clusters[0].overlap_evidence_ids, ("ev_common",))

    def test_120_supporting_strategy_list_unique_sorted(self):
        """Test 120: Supporting strategy list unique/sorted."""
        cand_s09 = _make_candidate("s09_setup", "S09", cluster_id="cluster_x")
        cand_s01 = _make_candidate("s01_setup", "S01", cluster_id="cluster_x")

        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand_s09), _make_eval(cand_s01)])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].strategy_ids, ("S01", "S09"))

    def test_121_same_strategy_exact_duplicate_idempotent_collapse(self):
        """Test 121: Same strategy exact duplicate idempotent collapse."""
        cand = _make_candidate("s01_setup", "S01", cluster_id="cluster_x")
        eval1 = _make_eval(cand)
        eval2 = _make_eval(cand)

        clusters = EvidenceDeduplicator().deduplicate([eval1, eval2])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].members), 1)

    def test_122_same_setup_id_different_payload_integrity_error(self):
        """Test 122: Same setup ID/different payload integrity error."""
        cand1 = _make_candidate("s01_setup", "S01", entry=2000.0)
        cand2 = _make_candidate("s01_setup", "S01", entry=2005.0)

        with self.assertRaises(StrategyStateError):
            EvidenceDeduplicator().deduplicate([_make_eval(cand1), _make_eval(cand2)])

    def test_123_same_cluster_id_opposite_direction_integrity_error(self):
        """Test 123: Same cluster ID/opposite direction integrity error."""
        cand_buy = _make_candidate("cand_buy", "S01", direction="BUY", cluster_id="cluster_clash")
        cand_sell = _make_candidate(
            "cand_sell", "S05", direction="SELL", cluster_id="cluster_clash",
            entry=2040.0, sl=2050.0, tp=2020.0
        )

        with self.assertRaises(StrategyStateError):
            EvidenceDeduplicator().deduplicate([_make_eval(cand_buy), _make_eval(cand_sell)])

    def test_124_shared_sweep_only_different_zone_separate_cluster(self):
        """Test 124: Shared sweep only, different zone → separate cluster."""
        ev_sweep = EvidenceRef("common_sweep", "liquidity_sweep", 90, 1990.0)
        ev_fvg1 = EvidenceRef("fvg_zone_1", "fair_value_gap", 94, 2000.0)
        ev_fvg2 = EvidenceRef("fvg_zone_2", "fair_value_gap", 96, 2005.0)

        cand1 = _make_candidate("cand1", "S01", cluster_id="cluster_1", evidences=(ev_sweep, ev_fvg1))
        cand2 = _make_candidate("cand2", "S09", cluster_id="cluster_2", evidences=(ev_sweep, ev_fvg2))

        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand1), _make_eval(cand2)])
        self.assertEqual(len(clusters), 2)
        cids = {c.cluster_id for c in clusters}
        self.assertEqual(cids, {"cluster_1", "cluster_2"})

    def test_125_shared_structure_only_different_zone_separate_cluster(self):
        """Test 125: Shared structure only, different zone → separate cluster."""
        ev_bos = EvidenceRef("common_bos", "structure_event", 92, 2010.0, details={"structure_leg_id": "leg_42"})
        ev_fvg = EvidenceRef("fvg_zone", "fair_value_gap", 94, 2000.0, details={"structure_leg_id": "leg_42"})
        ev_ob = EvidenceRef("ob_zone", "order_block", 96, 2005.0, details={"structure_leg_id": "leg_42"})

        cand_s01 = _make_candidate(
            "cand1", "S01", cluster_id="cluster_s01",
            evidences=(ev_bos, ev_fvg), structure_leg_id="leg_42"
        )
        cand_s05 = _make_candidate(
            "cand2", "S05", cluster_id="cluster_s05",
            evidences=(ev_bos, ev_ob), structure_leg_id="leg_42"
        )

        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand_s01), _make_eval(cand_s05)])
        self.assertEqual(len(clusters), 2)

    def test_126_same_leg_same_zone_alias_ids_merge(self):
        """Test 126: Same leg + same zone alias IDs merge."""
        ev_mss = EvidenceRef("mss_1", "structure_event", 93, 2010.0, details={"structure_leg_id": "leg_10"})
        ev_fvg = EvidenceRef("fvg_zone_identical", "fair_value_gap", 95, 2000.0, details={"structure_leg_id": "leg_10"})

        cand1 = _make_candidate(
            "cand1", "S01", cluster_id="alias_id_aaa",
            evidences=(ev_mss, ev_fvg), structure_leg_id="leg_10"
        )
        cand2 = _make_candidate(
            "cand2", "S09", cluster_id="alias_id_bbb",
            evidences=(ev_mss, ev_fvg), structure_leg_id="leg_10"
        )

        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand1), _make_eval(cand2)])
        self.assertEqual(len(clusters), 1)
        # Merged group takes min cluster_id deterministically
        self.assertEqual(clusters[0].cluster_id, "alias_id_aaa")
        self.assertEqual(len(clusters[0].members), 2)

    def test_127_s05_ob_and_s01_fvg_common_bos_separate(self):
        """Test 127: S05 OB và S01 FVG chung BOS vẫn tách cluster."""
        ev_bos = EvidenceRef("common_bos", "structure_event", 92, 2010.0, details={"structure_leg_id": "leg_77"})
        ev_fvg = EvidenceRef("fvg_zone_unique", "fair_value_gap", 94, 2000.0, details={"structure_leg_id": "leg_77"})
        ev_ob = EvidenceRef("ob_zone_unique", "order_block", 95, 2002.0, details={"structure_leg_id": "leg_77"})

        cand_s01 = _make_candidate(
            "cand_s01", "S01", cluster_id="cluster_fvg",
            evidences=(ev_bos, ev_fvg), structure_leg_id="leg_77"
        )
        cand_s05 = _make_candidate(
            "cand_s05", "S05", cluster_id="cluster_ob",
            evidences=(ev_bos, ev_ob), structure_leg_id="leg_77"
        )

        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand_s01), _make_eval(cand_s05)])
        self.assertEqual(len(clusters), 2)
        cids = tuple(c.cluster_id for c in clusters)
        self.assertEqual(cids, ("cluster_fvg", "cluster_ob"))

    def test_128_rejected_evaluation_not_in_eligible_cluster(self):
        """Test 128: Rejected evaluation không vào eligible cluster."""
        cand_pass = _make_candidate("cand_pass", "S01", cluster_id="c_pass")
        cand_fail = _make_candidate("cand_fail", "S05", cluster_id="c_fail")

        eval_pass = _make_eval(cand_pass, status="ELIGIBLE")
        eval_fail = _make_eval(cand_fail, status="REJECTED", reasons=("htf_bias_mismatch",))

        clusters = EvidenceDeduplicator().deduplicate([eval_pass, eval_fail])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].cluster_id, "c_pass")
        self.assertEqual(len(clusters[0].members), 1)
        self.assertEqual(clusters[0].members[0].candidate.setup_id, "cand_pass")

    def test_129_rejected_evaluation_preserved_in_confluence_batch(self):
        """Test 129: Rejected evaluation vẫn còn trong audit / ConfluenceBatch."""
        cand_pass = _make_candidate("cand_pass", "S01", cluster_id="c_pass")
        cand_fail = _make_candidate("cand_fail", "S05", cluster_id="c_fail")

        eval_pass = _make_eval(cand_pass, status="ELIGIBLE")
        eval_fail = _make_eval(cand_fail, status="REJECTED", reasons=("htf_bias_mismatch",))

        batch = build_confluence_batch([eval_pass, eval_fail], self.regime, self.context)
        self.assertEqual(len(batch.eligible_clusters), 1)
        # All evaluations preserved for audit
        self.assertEqual(len(batch.evaluations), 2)
        statuses = {e.candidate.setup_id: e.status for e in batch.evaluations}
        self.assertEqual(statuses["cand_pass"], "ELIGIBLE")
        self.assertEqual(statuses["cand_fail"], "REJECTED")

    def test_130_buy_only_no_conflict(self):
        """Test 130: BUY-only → no conflict."""
        cand = _make_candidate("cand_buy", "S01", direction="BUY", cluster_id="c_buy")
        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand)])

        detector = DirectionConflictDetector()
        conflict = detector.detect(clusters, 100)
        self.assertIsNone(conflict)

    def test_131_sell_only_no_conflict(self):
        """Test 131: SELL-only → no conflict."""
        cand = _make_candidate("cand_sell", "S05", direction="SELL", cluster_id="c_sell", entry=2040.0, sl=2050.0, tp=2020.0)
        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand)])

        detector = DirectionConflictDetector()
        conflict = detector.detect(clusters, 100)
        self.assertIsNone(conflict)

    def test_132_buy_and_sell_produces_direction_conflict(self):
        """Test 132: BUY+SELL → DirectionConflict."""
        cand_buy = _make_candidate("cand_buy", "S01", direction="BUY", cluster_id="c_buy")
        cand_sell = _make_candidate("cand_sell", "S05", direction="SELL", cluster_id="c_sell", entry=2040.0, sl=2050.0, tp=2020.0)

        clusters = EvidenceDeduplicator().deduplicate([_make_eval(cand_buy), _make_eval(cand_sell)])
        detector = DirectionConflictDetector()
        conflict = detector.detect(clusters, 100)

        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.bar_index, 100)
        self.assertEqual(conflict.buy_cluster_ids, ("c_buy",))
        self.assertEqual(conflict.sell_cluster_ids, ("c_sell",))
        self.assertEqual(conflict.reason, "conflicting_direction")

    def test_133_conflict_does_not_alter_evaluation_status(self):
        """Test 133: Conflict không đổi evaluation status."""
        cand_buy = _make_candidate("cand_buy", "S01", direction="BUY", cluster_id="c_buy")
        cand_sell = _make_candidate("cand_sell", "S05", direction="SELL", cluster_id="c_sell", entry=2040.0, sl=2050.0, tp=2020.0)

        eval_buy = _make_eval(cand_buy)
        eval_sell = _make_eval(cand_sell)

        batch = build_confluence_batch([eval_buy, eval_sell], self.regime, self.context)
        self.assertIsNotNone(batch.direction_conflict)
        # Evaluations must STILL be ELIGIBLE (conflict does not reject candidates in T53.7)
        for e in batch.evaluations:
            self.assertEqual(e.status, "ELIGIBLE")

    def test_134_strategy_count_not_used_as_vote(self):
        """Test 134: Không dùng strategy count như vote (10 BUY vs 1 SELL is still conflict)."""
        evals = []
        for i in range(10):
            cand = _make_candidate(f"cand_buy_{i}", f"S{i:02d}", direction="BUY", cluster_id=f"c_buy_{i}")
            evals.append(_make_eval(cand))
        cand_sell = _make_candidate("cand_sell_single", "S99", direction="SELL", cluster_id="c_sell_single", entry=2040.0, sl=2050.0, tp=2020.0)
        evals.append(_make_eval(cand_sell))

        batch = build_confluence_batch(evals, self.regime, self.context)
        self.assertIsNotNone(batch.direction_conflict)
        self.assertEqual(len(batch.direction_conflict.buy_cluster_ids), 10)
        self.assertEqual(len(batch.direction_conflict.sell_cluster_ids), 1)

    def test_135_input_permutation_full_payload_invariant(self):
        """Test 135: Input permutation full-payload invariant."""
        c1 = _make_candidate("cand1", "S01", cluster_id="c_alpha")
        c2 = _make_candidate("cand2", "S09", cluster_id="c_alpha")
        c3 = _make_candidate("cand3", "S05", direction="SELL", cluster_id="c_beta", entry=2040.0, sl=2050.0, tp=2020.0)

        e1 = _make_eval(c1)
        e2 = _make_eval(c2)
        e3 = _make_eval(c3)

        batch1 = build_confluence_batch([e1, e2, e3], self.regime, self.context)
        batch2 = build_confluence_batch([e3, e1, e2], self.regime, self.context)
        batch3 = build_confluence_batch([e2, e3, e1], self.regime, self.context)

        self.assertEqual(batch1.to_dict(), batch2.to_dict())
        self.assertEqual(batch1.to_dict(), batch3.to_dict())

    def test_136_json_roundtrip_confluence_batch(self):
        """Test 136: JSON round-trip ConfluenceBatch."""
        cand_buy = _make_candidate("cand_buy", "S01", direction="BUY", cluster_id="c_buy")
        cand_sell = _make_candidate("cand_sell", "S05", direction="SELL", cluster_id="c_sell", entry=2040.0, sl=2050.0, tp=2020.0)

        batch = build_confluence_batch([_make_eval(cand_buy), _make_eval(cand_sell)], self.regime, self.context)
        b_dict = batch.to_dict()
        batch_reloaded = ConfluenceBatch.from_dict(b_dict)

        self.assertEqual(batch.bar_index, batch_reloaded.bar_index)
        self.assertEqual(batch.timestamp, batch_reloaded.timestamp)
        self.assertEqual(batch.regime.regime, batch_reloaded.regime.regime)
        self.assertEqual(len(batch.evaluations), len(batch_reloaded.evaluations))
        self.assertEqual(len(batch.eligible_clusters), len(batch_reloaded.eligible_clusters))
        self.assertEqual(batch.direction_conflict, batch_reloaded.direction_conflict)
        self.assertEqual(batch.to_dict(), batch_reloaded.to_dict())

    def test_146_evidence_cluster_tampered_strategy_ids_raises(self):
        """Test 146 (P2): Tampered strategy_ids in EvidenceCluster payload raises ValueError."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand = _make_candidate("cand_1", "S01", evidences=(ev,))
        cluster = EvidenceCluster("cl_1", "BUY", (_make_eval(cand),))
        payload = cluster.to_dict()
        payload["strategy_ids"] = ["TAMPERED"]
        with self.assertRaises(ValueError):
            EvidenceCluster.from_dict(payload)

    def test_147_evidence_cluster_tampered_evidence_ids_raises(self):
        """Test 147 (P2): Tampered evidence_ids in EvidenceCluster payload raises ValueError."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand = _make_candidate("cand_1", "S01", evidences=(ev,))
        cluster = EvidenceCluster("cl_1", "BUY", (_make_eval(cand),))
        payload = cluster.to_dict()
        payload["evidence_ids"] = ["tampered_ev"]
        with self.assertRaises(ValueError):
            EvidenceCluster.from_dict(payload)

    def test_148_evidence_cluster_tampered_overlap_evidence_ids_raises(self):
        """Test 148 (P2): Tampered overlap_evidence_ids in EvidenceCluster payload raises ValueError."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand1 = _make_candidate("cand_1", "S01", cluster_id="cl_1", evidences=(ev,))
        cand2 = _make_candidate("cand_2", "S09", cluster_id="cl_1", evidences=(ev,))
        cluster = EvidenceCluster("cl_1", "BUY", (_make_eval(cand1), _make_eval(cand2)))
        payload = cluster.to_dict()
        payload["overlap_evidence_ids"] = ["tampered_overlap"]
        with self.assertRaises(ValueError):
            EvidenceCluster.from_dict(payload)

    def test_149_evidence_cluster_duplicate_derived_ids_raises(self):
        """Test 149 (P2): Duplicate derived IDs in EvidenceCluster payload raises ValueError."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand = _make_candidate("cand_1", "S01", evidences=(ev,))
        cluster = EvidenceCluster("cl_1", "BUY", (_make_eval(cand),))
        payload = cluster.to_dict()
        payload["strategy_ids"] = ["S01", "S01"]
        with self.assertRaises(ValueError):
            EvidenceCluster.from_dict(payload)

    def test_150_evidence_cluster_unknown_field_raises(self):
        """Test 150 (P2): Unknown field in EvidenceCluster payload raises StrictModelTypeError."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand = _make_candidate("cand_1", "S01", evidences=(ev,))
        cluster = EvidenceCluster("cl_1", "BUY", (_make_eval(cand),))
        payload = cluster.to_dict()
        payload["rogue_field"] = "malicious"
        with self.assertRaises(StrictModelTypeError):
            EvidenceCluster.from_dict(payload)

    def test_151_evidence_cluster_exact_json_roundtrip(self):
        """Test 151 (P2): EvidenceCluster exact JSON roundtrip."""
        ev = EvidenceRef("ev_1", "fair_value_gap", 95, 2000.0)
        cand = _make_candidate("cand_1", "S01", evidences=(ev,))
        cluster = EvidenceCluster("cl_1", "BUY", (_make_eval(cand),))
        payload = cluster.to_dict()
        cluster_reloaded = EvidenceCluster.from_dict(payload)
        self.assertEqual(cluster.to_dict(), cluster_reloaded.to_dict())

    def test_152_direction_conflict_duplicate_buy_id_raises(self):
        """Test 152 (P2): Duplicate BUY ID in DirectionConflict raises ValueError."""
        with self.assertRaises(ValueError):
            DirectionConflict(10, ("buy_1", "buy_1"), ("sell_1",))
        with self.assertRaises(ValueError):
            DirectionConflict.from_dict({
                "bar_index": 10,
                "buy_cluster_ids": ["buy_1", "buy_1"],
                "sell_cluster_ids": ["sell_1"],
                "reason": "conflicting_direction",
            })

    def test_153_direction_conflict_duplicate_sell_id_raises(self):
        """Test 153 (P2): Duplicate SELL ID in DirectionConflict raises ValueError."""
        with self.assertRaises(ValueError):
            DirectionConflict(10, ("buy_1",), ("sell_1", "sell_1"))
        with self.assertRaises(ValueError):
            DirectionConflict.from_dict({
                "bar_index": 10,
                "buy_cluster_ids": ["buy_1"],
                "sell_cluster_ids": ["sell_1", "sell_1"],
                "reason": "conflicting_direction",
            })

    def test_154_direction_conflict_overlap_buy_sell_raises(self):
        """Test 154 (P2): Overlap between BUY and SELL clusters raises ValueError."""
        with self.assertRaises(ValueError):
            DirectionConflict(10, ("overlap_cluster",), ("overlap_cluster",))

    def test_155_direction_conflict_valid_exact_json_roundtrip(self):
        """Test 155 (P2): DirectionConflict exact JSON roundtrip with canonical ordering."""
        dc = DirectionConflict(10, ("buy_b", "buy_a"), ("sell_y", "sell_x"))
        self.assertEqual(dc.buy_cluster_ids, ("buy_a", "buy_b"))
        self.assertEqual(dc.sell_cluster_ids, ("sell_x", "sell_y"))
        reloaded = DirectionConflict.from_dict(dc.to_dict())
        self.assertEqual(dc.to_dict(), reloaded.to_dict())

    def test_156_confluence_batch_from_dict_tampered_cluster_fails(self):
        """Test 156 (P2): ConfluenceBatch.from_dict fails when embedded EvidenceCluster is tampered."""
        cand_buy = _make_candidate("cand_buy", "S01", direction="BUY", cluster_id="c_buy")
        batch = build_confluence_batch([_make_eval(cand_buy)], self.regime, self.context)
        b_dict = batch.to_dict()
        b_dict["eligible_clusters"][0]["strategy_ids"] = ["TAMPERED"]
        with self.assertRaises(ValueError):
            ConfluenceBatch.from_dict(b_dict)

    def test_157_evidence_cluster_duplicate_same_object_member_raises(self):
        """Test 157 (P2.1): Same StrategyEvaluation object twice in members raises ValueError."""
        cand = _make_candidate("setup_dup_obj", "S01")
        ev = _make_eval(cand)
        with self.assertRaises(ValueError) as ctx:
            EvidenceCluster("cl_dup", "BUY", (ev, ev))
        self.assertIn("duplicate setup_id in EvidenceCluster", str(ctx.exception))

    def test_158_evidence_cluster_duplicate_different_objects_same_setup_id_raises(self):
        """Test 158 (P2.1): Different StrategyEvaluation objects with same setup_id raises ValueError."""
        cand1 = _make_candidate("setup_dup_id", "S01")
        cand2 = _make_candidate("setup_dup_id", "S01")
        ev1 = _make_eval(cand1)
        ev2 = _make_eval(cand2)
        with self.assertRaises(ValueError) as ctx:
            EvidenceCluster("cl_dup", "BUY", (ev1, ev2))
        self.assertIn("duplicate setup_id in EvidenceCluster", str(ctx.exception))

    def test_159_evidence_cluster_different_setup_ids_same_strategy_passes(self):
        """Test 159 (P2.1): Different setup_id with same strategy_id is valid."""
        cand1 = _make_candidate("setup_s01_1", "S01")
        cand2 = _make_candidate("setup_s01_2", "S01")
        ev1 = _make_eval(cand1)
        ev2 = _make_eval(cand2)
        cluster = EvidenceCluster("cl_same_strat", "BUY", (ev1, ev2))
        self.assertEqual(len(cluster.members), 2)
        self.assertEqual(cluster.strategy_ids, ("S01",))

    def test_160_evidence_cluster_from_dict_duplicate_setup_id_raises(self):
        """Test 160 (P2.1): EvidenceCluster.from_dict rejects payload with duplicate setup_id."""
        cand = _make_candidate("setup_from_dict_dup", "S01")
        ev = _make_eval(cand)
        cluster = EvidenceCluster("cl_valid", "BUY", (ev,))
        payload = cluster.to_dict()
        payload["members"].append(ev.to_dict())  # add duplicate member
        with self.assertRaises(ValueError) as ctx:
            EvidenceCluster.from_dict(payload)
        self.assertIn("duplicate setup_id in EvidenceCluster", str(ctx.exception))

    def test_161_evidence_cluster_valid_serialization_roundtrip_multi_member(self):
        """Test 161 (P2.1): Valid multi-member cluster serializes and deserializes accurately."""
        cand1 = _make_candidate("setup_m1", "S01")
        cand2 = _make_candidate("setup_m2", "S09")
        ev1 = _make_eval(cand1)
        ev2 = _make_eval(cand2)
        cluster = EvidenceCluster("cl_multi", "BUY", (ev1, ev2))
        payload = cluster.to_dict()
        reloaded = EvidenceCluster.from_dict(payload)
        self.assertEqual(cluster.to_dict(), reloaded.to_dict())
        self.assertEqual(len(reloaded.members), 2)


if __name__ == "__main__":
    unittest.main()


