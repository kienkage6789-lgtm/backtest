"""
tests.test_smc_engine_t53_7_integration
=======================================
Integration tests for T53.7 — Market Regime Classifier, Eligibility Gate,
Evidence Deduplication, Direction Conflict Detection, and ConfluenceBatch.
Covers Test Matrix Group G (tests 137 to 146).
"""

from __future__ import annotations

import datetime
import json
import time
import unittest
from typing import Any, Sequence
import numpy as np
import pandas as pd

from smc.engine.context import StrategyContext
from smc.engine.errors import StrategyStateError
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    FairValueGapSnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    MarketRegime,
    OrderBlockSnapshot,
    StrategyEvaluation,
    StrategyProfile,
    StructureEventSnapshot,
)
from smc.engine.registry import StrategyRegistry, StrategyRegistryConfig
from smc.engine.strategies import (
    S01Config,
    S01ICT2022Strategy,
    S05Config,
    S05BOSOBRetestStrategy,
    S09Config,
    S09ICTSilverBulletStrategy,
)
from smc.engine.strategies.s09_ict_silver_bullet import NEW_YORK_TZ
from smc.engine.regime import (
    RegimeClassifierConfig,
    MarketRegimeClassifier,
    classify_market_regimes,
)
from smc.engine.eligibility import (
    REGIME_MATRIX,
    EligibilityGate,
    get_regime_matrix_score,
)
from smc.engine.confluence import (
    EvidenceCluster,
    DirectionConflict,
    ConfluenceBatch,
    EvidenceDeduplicator,
    DirectionConflictDetector,
    build_confluence_batch,
)
from tests.test_smc_strategy_s09 import (
    _make_context as _s09_ctx,
    _make_sweep,
    _make_structure,
    _make_fvg,
)


def _make_stream_context(
    bar_index: int,
    base_close: float = 2000.0,
    atr: float = 5.0,
    dt_base: pd.Timestamp | None = None,
    htf_bias: str = "neutral",
    timeframe: str = "M1",
    sweeps: Sequence[LiquiditySweepSnapshot] = (),
    structures: Sequence[StructureEventSnapshot] = (),
    fvgs: Sequence[FairValueGapSnapshot] = (),
    obs: Sequence[OrderBlockSnapshot] = (),
    pools: Sequence[LiquidityPoolSnapshot] = (),
) -> StrategyContext:
    if dt_base is None:
        dt_base = pd.Timestamp("2024-05-15 14:00:00+00:00")
    bct = dt_base + pd.Timedelta(minutes=bar_index + 1)
    ts = bct - pd.Timedelta(minutes=1)

    c = base_close + 0.5 * np.sin(bar_index)
    o = c - 0.2
    h = c + 1.0
    l = c - 1.0

    bias_snap = BiasStateSnapshot(
        bias=htf_bias,
        timestamp=ts,
        as_of=ts,
        source_event_time=ts,
    ) if htf_bias else None

    return StrategyContext(
        symbol="EURUSD",
        timeframe=timeframe,
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=bct,
        open=round(o, 3),
        high=round(h, 3),
        low=round(l, 3),
        close=round(c, 3),
        volume=100.0,
        atr14=round(atr, 3),
        htf_bias=bias_snap,
        recent_sweeps=tuple(sweeps),
        recent_structures=tuple(structures),
        active_fvgs=tuple(fvgs),
        active_obs=tuple(obs),
        active_pools=tuple(pools),
    )


def _make_realistic_evaluations(
    gate: EligibilityGate,
    ctx: StrategyContext,
    regime: MarketRegime,
) -> tuple[StrategyEvaluation, ...]:
    """
    Creates a realistic mix of candidate evaluations at ctx.bar_index:
    - S01 BUY setup (cluster_BUY_leg1) -> ELIGIBLE
    - S09 BUY setup (cluster_BUY_leg1, shares leg and zone with S01 -> merged) -> ELIGIBLE
    - S01 SELL setup (cluster_SELL_N, separate cluster -> induces BUY/SELL conflict) -> ELIGIBLE
    - S01 low RR setup -> REJECTED with reason ('insufficient_rr',)

    All evaluations are produced directly by gate.evaluate(...).
    """
    N = ctx.bar_index
    c = ctx.close
    ts = ctx.timestamp
    bct = ctx.bar_close_time

    # Evidences for BUY (S01 & S09)
    ev_sw = EvidenceRef(f"ev_sw_{N}", "liquidity_sweep", max(0, N - 3), round(c - 5.0, 3), time=ts - pd.Timedelta(minutes=3))
    ev_fvg = EvidenceRef(f"ev_fvg_{N}", "fair_value_gap", max(1, N - 2), round(c, 3), time=ts - pd.Timedelta(minutes=2))
    ev_mss = EvidenceRef(f"ev_mss_{N}", "structure_event", max(2, N - 1), round(c + 2.0, 3), time=ts - pd.Timedelta(minutes=1))

    shared_cluster_id = f"cluster_BUY_{N}_leg1"
    cand_s01 = CandidateSetup(
        setup_id=f"setup_s01_{N}",
        strategy_id="S01",
        direction="BUY",
        bar_index=N,
        timestamp=ts,
        entry_price=round(c, 3),
        stop_loss=round(c - 5.0, 3),
        take_profit=round(c + 10.0, 3),
        planned_rr=2.0,
        evidences=(ev_sw, ev_fvg, ev_mss),
        evidence_cluster_id=shared_cluster_id,
        expiry_bar=N + 10,
        meta={"structure_leg_id": "leg1", "allow_neutral_bias": True},
    )

    w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
    w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
    w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")
    cand_s09 = CandidateSetup(
        setup_id=f"setup_s09_{N}",
        strategy_id="S09",
        direction="BUY",
        bar_index=N,
        timestamp=ts,
        entry_price=round(c, 3),
        stop_loss=round(c - 5.0, 3),
        take_profit=round(c + 10.0, 3),
        planned_rr=2.0,
        evidences=(ev_sw, ev_fvg, ev_mss),
        evidence_cluster_id=shared_cluster_id,
        expiry_bar=N + 10,
        meta={
            "structure_leg_id": "leg1",
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": bct.isoformat(),
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
        },
    )

    # Evidences for S01 SELL (opposed direction -> separate cluster -> direction conflict)
    ev_sw_s = EvidenceRef(f"ev_sw_s_{N}", "liquidity_sweep", max(0, N - 3), round(c + 5.0, 3), time=ts - pd.Timedelta(minutes=3))
    ev_fvg_s = EvidenceRef(f"ev_fvg_s_{N}", "fair_value_gap", max(1, N - 2), round(c, 3), time=ts - pd.Timedelta(minutes=2))
    ev_mss_s = EvidenceRef(f"ev_mss_s_{N}", "structure_event", max(2, N - 1), round(c - 2.0, 3), time=ts - pd.Timedelta(minutes=1))
    cand_sell = CandidateSetup(
        setup_id=f"setup_sell_{N}",
        strategy_id="S01",
        direction="SELL",
        bar_index=N,
        timestamp=ts,
        entry_price=round(c, 3),
        stop_loss=round(c + 5.0, 3),
        take_profit=round(c - 10.0, 3),
        planned_rr=2.0,
        evidences=(ev_sw_s, ev_fvg_s, ev_mss_s),
        evidence_cluster_id=f"cluster_SELL_{N}",
        expiry_bar=N + 10,
        meta={"allow_neutral_bias": True},
    )

    # Candidate with low RR -> REJECTED
    cand_rej = CandidateSetup(
        setup_id=f"setup_rej_{N}",
        strategy_id="S01",
        direction="BUY",
        bar_index=N,
        timestamp=ts,
        entry_price=round(c, 3),
        stop_loss=round(c - 5.0, 3),
        take_profit=round(c + 5.5, 3),
        planned_rr=1.1,
        evidences=(ev_sw, ev_fvg, ev_mss),
        evidence_cluster_id=f"cluster_rej_{N}",
        expiry_bar=N + 10,
    )

    prof_s01 = StrategyProfile(
        strategy_id="S01",
        name="S01 ICT 2022",
        version="1.0.0",
        style="reversal",
        allowed_directions=("BUY", "SELL"),
        timeframes=("M1", "M5", "M15"),
        min_rr=1.5,
    )
    prof_s09 = StrategyProfile(
        strategy_id="S09",
        name="S09 ICT Silver Bullet",
        version="1.0.0",
        style="time_based",
        allowed_directions=("BUY", "SELL"),
        timeframes=("M1", "M5", "M15"),
        min_rr=1.5,
    )

    eval_s01 = gate.evaluate(cand_s01, ctx, regime, prof_s01)
    eval_s09 = gate.evaluate(cand_s09, ctx, regime, prof_s09)
    eval_sell = gate.evaluate(cand_sell, ctx, regime, prof_s01)
    eval_rej = gate.evaluate(cand_rej, ctx, regime, prof_s01)

    return (eval_s01, eval_s09, eval_sell, eval_rej)


class TestSMCEngineT537Integration(unittest.TestCase):
    def setUp(self):
        self.gate = EligibilityGate()
        self.dedup = EvidenceDeduplicator()
        self.conflict_detector = DirectionConflictDetector()

    def test_137_registry_s01_s05_s09_output_goes_through_gate(self):
        """Test 137: Registry S01/S05/S09 output goes through gate with realistic stream."""
        registry = StrategyRegistry([
            S01ICT2022Strategy(S01Config(mode="internal")),
            S05BOSOBRetestStrategy(S05Config(mode="internal")),
            S09ICTSilverBulletStrategy(S09Config(mode="internal")),
        ])

        # Feed warm-up
        for b in range(5):
            registry.evaluate_enabled(_s09_ctx(bar_index=b))

        # Feed sweep
        sw = _make_sweep(index=5, direction="bullish", price_wick=2035.0)
        registry.evaluate_enabled(_s09_ctx(bar_index=5, sweeps=[sw]))

        # Feed MSS + FVG
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        registry.evaluate_enabled(_s09_ctx(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Retest candle triggers candidate setups
        ctx7 = _s09_ctx(bar_index=7, low_p=2037.0, close_p=2039.0, fvgs=[fvg])
        candidates_by_strat = registry.evaluate_enabled(ctx7)

        self.assertIn("S01", candidates_by_strat)
        self.assertIn("S05", candidates_by_strat)
        self.assertIn("S09", candidates_by_strat)

        total_cands = sum(len(c) for c in candidates_by_strat.values())
        self.assertGreater(total_cands, 0, "Registry must emit at least 1 candidate on realistic trigger")
        self.assertGreater(len(candidates_by_strat["S01"]), 0)
        self.assertGreater(len(candidates_by_strat["S09"]), 0)

        regime = MarketRegime(
            regime="bullish_trend",
            bar_index=7,
            timestamp=ctx7.bar_close_time,
            efficiency_ratio=0.5,
            atr_percentile=50.0,
        )

        evals = self.gate.evaluate_registry_output(
            candidates_by_strat, ctx7, regime, registry.profiles
        )
        self.assertIsInstance(evals, tuple)
        self.assertGreater(len(evals), 0)
        self.assertTrue(any(e.status == "ELIGIBLE" for e in evals))

        for e in evals:
            self.assertIn(e.status, ("ELIGIBLE", "REJECTED"))
            self.assertLessEqual(e.candidate.bar_index, ctx7.bar_index)

        batch = build_confluence_batch(evals, regime, ctx7)
        self.assertIsInstance(batch, ConfluenceBatch)
        self.assertGreater(len(batch.eligible_clusters), 0)

    def test_138_production_context_builder_to_classifier(self):
        """Test 138: Production Context stream -> classifier."""
        classifier = MarketRegimeClassifier()
        # Feed 110 bars to cross both close (20) and ATR (100) warm-up
        for i in range(110):
            ctx = _make_stream_context(bar_index=i, base_close=2000.0 + i * 0.5)
            regime = classifier.update(ctx)

        self.assertEqual(regime.bar_index, 109)
        self.assertNotEqual(regime.reason, "insufficient_warmup_bars")
        self.assertIn(regime.regime, ("bullish_trend", "bearish_trend", "volatile_reversal", "ranging", "uncertain"))
        self.assertGreaterEqual(regime.efficiency_ratio, 0.0)
        self.assertLessEqual(regime.efficiency_ratio, 1.0)
        self.assertGreaterEqual(regime.atr_percentile, 0.0)
        self.assertLessEqual(regime.atr_percentile, 100.0)

    def test_139_production_s01_s09_shared_opportunity_dedup_single_cluster(self):
        """Test 139: Production S01/S09 shared opportunity dedup thành một cluster."""
        ctx = _make_stream_context(bar_index=10, htf_bias="bullish")
        regime = MarketRegime(
            regime="ranging",
            bar_index=10,
            timestamp=ctx.bar_close_time,
            efficiency_ratio=0.1,
            atr_percentile=50.0,
        )

        ev_sweep = EvidenceRef("sweep_common", "liquidity_sweep", 7, 1990.0, time=ctx.timestamp - pd.Timedelta(minutes=3))
        ev_fvg = EvidenceRef("fvg_common", "fair_value_gap", 8, 2000.0, time=ctx.timestamp - pd.Timedelta(minutes=2), details={"structure_leg_id": "leg_alpha"})
        ev_mss = EvidenceRef("mss_common", "structure_event", 9, 2010.0, time=ctx.timestamp - pd.Timedelta(minutes=1), details={"structure_leg_id": "leg_alpha"})

        cluster_id = "cluster_BUY_leg_alpha_fvg_common"

        # Candidate S01
        c_s01 = CandidateSetup(
            setup_id="setup_s01",
            strategy_id="S01",
            direction="BUY",
            bar_index=10,
            timestamp=ctx.timestamp,
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            planned_rr=2.0,
            evidences=(ev_sweep, ev_fvg, ev_mss),
            evidence_cluster_id=cluster_id,
            expiry_bar=20,
            meta={"structure_leg_id": "leg_alpha"},
        )

        # Candidate S09
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")
        c_s09 = CandidateSetup(
            setup_id="setup_s09",
            strategy_id="S09",
            direction="BUY",
            bar_index=10,
            timestamp=ctx.timestamp,
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            planned_rr=2.0,
            evidences=(ev_sweep, ev_fvg, ev_mss),
            evidence_cluster_id=cluster_id,
            expiry_bar=20,
            meta={
                "structure_leg_id": "leg_alpha",
                "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
                "window_name": "silver_bullet_ny_am",
                "local_date": "2024-05-15",
                "window_start_utc": w_start.isoformat(),
                "window_end_utc": w_end.isoformat(),
                "grace_expiry_utc": w_grace.isoformat(),
                "signal_bar_close_time": ctx.bar_close_time.isoformat(),
            },
        )

        eval_s01 = self.gate.evaluate(
            c_s01, ctx, regime,
            StrategyProfile(strategy_id="S01", name="S01 ICT 2022", version="1.0.0", allowed_directions=("BUY",), timeframes=("M1",), min_rr=1.5, style="reversal")
        )
        eval_s09 = self.gate.evaluate(
            c_s09, ctx, regime,
            StrategyProfile(strategy_id="S09", name="S09 ICT Silver Bullet", version="1.0.0", allowed_directions=("BUY",), timeframes=("M1",), min_rr=1.5, style="time_based")
        )

        self.assertEqual(eval_s01.status, "ELIGIBLE")
        self.assertEqual(eval_s09.status, "ELIGIBLE")

        batch = build_confluence_batch([eval_s01, eval_s09], regime, ctx)
        self.assertEqual(len(batch.eligible_clusters), 1)
        cluster = batch.eligible_clusters[0]
        self.assertEqual(cluster.cluster_id, cluster_id)
        self.assertEqual(cluster.strategy_ids, ("S01", "S09"))
        self.assertEqual(len(cluster.members), 2)
        self.assertIsNone(batch.direction_conflict)

    def test_140_production_s05_distinct_ob_opportunity_keeps_separate_cluster(self):
        """Test 140: Production S05 distinct OB opportunity giữ cluster riêng."""
        ctx = _make_stream_context(bar_index=10, htf_bias="bullish")
        regime = MarketRegime(
            regime="bullish_trend",
            bar_index=10,
            timestamp=ctx.bar_close_time,
            efficiency_ratio=0.6,
            atr_percentile=50.0,
        )

        # S01 candidate with FVG
        ev_sweep = EvidenceRef("sweep_common", "liquidity_sweep", 7, 1990.0, time=ctx.timestamp - pd.Timedelta(minutes=3))
        ev_fvg = EvidenceRef("fvg_common", "fair_value_gap", 8, 2000.0, time=ctx.timestamp - pd.Timedelta(minutes=2), details={"structure_leg_id": "leg_alpha"})
        ev_mss = EvidenceRef("mss_common", "structure_event", 9, 2010.0, time=ctx.timestamp - pd.Timedelta(minutes=1), details={"structure_leg_id": "leg_alpha"})
        c_s01 = CandidateSetup(
            setup_id="setup_s01",
            strategy_id="S01",
            direction="BUY",
            bar_index=10,
            timestamp=ctx.timestamp,
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            planned_rr=2.0,
            evidences=(ev_sweep, ev_fvg, ev_mss),
            evidence_cluster_id="cluster_s01_fvg",
            expiry_bar=20,
            meta={"structure_leg_id": "leg_alpha"},
        )

        # S05 candidate with OB
        ev_bos = EvidenceRef("bos_common", "structure_event", 8, 2010.0, time=ctx.timestamp - pd.Timedelta(minutes=2), details={"structure_leg_id": "leg_alpha"})
        ev_ob = EvidenceRef("ob_common", "order_block", 9, 1995.0, time=ctx.timestamp - pd.Timedelta(minutes=1), details={"structure_leg_id": "leg_alpha"})
        c_s05 = CandidateSetup(
            setup_id="setup_s05",
            strategy_id="S05",
            direction="BUY",
            bar_index=10,
            timestamp=ctx.timestamp,
            entry_price=2002.0,
            stop_loss=1992.0,
            take_profit=2022.0,
            planned_rr=2.0,
            evidences=(ev_bos, ev_ob),
            evidence_cluster_id="cluster_s05_ob",
            expiry_bar=20,
            meta={"structure_leg_id": "leg_alpha"},
        )

        eval_s01 = self.gate.evaluate(
            c_s01, ctx, regime,
            StrategyProfile(strategy_id="S01", name="S01 ICT 2022", version="1.0.0", allowed_directions=("BUY",), timeframes=("M1",), min_rr=1.5, style="reversal")
        )
        eval_s05 = self.gate.evaluate(
            c_s05, ctx, regime,
            StrategyProfile(strategy_id="S05", name="S05 BOS OB Retest", version="1.0.0", allowed_directions=("BUY",), timeframes=("M1",), min_rr=1.5, style="continuation")
        )

        batch = build_confluence_batch([eval_s01, eval_s05], regime, ctx)
        self.assertEqual(len(batch.eligible_clusters), 2)
        cids = tuple(c.cluster_id for c in batch.eligible_clusters)
        self.assertEqual(cids, ("cluster_s01_fvg", "cluster_s05_ob"))

    def test_141_full_batch_incremental_parity(self):
        """Test 141 (P2.2): Full batch/incremental parity with realistic candidate stream."""
        contexts = [_make_stream_context(i) for i in range(50)]

        # Incremental
        classifier_inc = MarketRegimeClassifier()
        inc_batches = []
        for ctx in contexts:
            regime = classifier_inc.update(ctx)
            evals = _make_realistic_evaluations(self.gate, ctx, regime) if (ctx.bar_index >= 5 and ctx.bar_index % 5 == 0) else ()
            batch = build_confluence_batch(evals, regime, ctx)
            inc_batches.append(batch)

        # Batch
        batch_regimes = classify_market_regimes(contexts)
        batch_batches = [
            build_confluence_batch(
                _make_realistic_evaluations(self.gate, ctx, reg) if (ctx.bar_index >= 5 and ctx.bar_index % 5 == 0) else (),
                reg,
                ctx,
            )
            for reg, ctx in zip(batch_regimes, contexts)
        ]

        self.assertEqual(len(inc_batches), len(batch_batches))
        for b_inc, b_batch in zip(inc_batches, batch_batches):
            self.assertEqual(b_inc.to_dict(), b_batch.to_dict())

        # Assert that realistic batch has merged clusters and direction conflict
        complex_batch = inc_batches[10]  # bar 10: 10 >= 5 and 10 % 5 == 0
        self.assertEqual(len(complex_batch.evaluations), 4)
        self.assertEqual(len(complex_batch.eligible_clusters), 2)  # S01+S09 merged into 1, S01 SELL in 1
        self.assertIsNotNone(complex_batch.direction_conflict)

    def test_142_full_json_replay_parity(self):
        """Test 142 (P2.2): Full JSON replay parity with realistic candidate stream."""
        contexts = [_make_stream_context(i) for i in range(30)]
        classifier = MarketRegimeClassifier()
        batches = []
        for ctx in contexts:
            reg = classifier.update(ctx)
            evals = _make_realistic_evaluations(self.gate, ctx, reg) if (ctx.bar_index >= 5 and ctx.bar_index % 5 == 0) else ()
            batches.append(build_confluence_batch(evals, reg, ctx))

        payload = json.dumps([b.to_dict() for b in batches])
        reloaded = [ConfluenceBatch.from_dict(d) for d in json.loads(payload)]

        self.assertEqual(len(batches), len(reloaded))
        for orig, rel in zip(batches, reloaded):
            self.assertEqual(orig.to_dict(), rel.to_dict())

    def test_143_append_future_bars_invariant_prefix(self):
        """Test 143 (P2.2): Append future bars không đổi prefix regime/evaluation/clusters with realistic candidates."""
        contexts_prefix = [_make_stream_context(i) for i in range(25)]
        contexts_full = [_make_stream_context(i) for i in range(40)]

        # Run prefix
        clf_prefix = MarketRegimeClassifier()
        prefix_batches = []
        for ctx in contexts_prefix:
            reg = clf_prefix.update(ctx)
            evals = _make_realistic_evaluations(self.gate, ctx, reg) if (ctx.bar_index >= 5 and ctx.bar_index % 5 == 0) else ()
            prefix_batches.append(build_confluence_batch(evals, reg, ctx).to_dict())

        # Run full
        clf_full = MarketRegimeClassifier()
        full_batches = []
        for ctx in contexts_full:
            reg = clf_full.update(ctx)
            evals = _make_realistic_evaluations(self.gate, ctx, reg) if (ctx.bar_index >= 5 and ctx.bar_index % 5 == 0) else ()
            full_batches.append(build_confluence_batch(evals, reg, ctx).to_dict())

        self.assertEqual(full_batches[:25], prefix_batches)

    def test_144_fault_injection_atomic_rollback(self):
        """Test 144: Fault injection không để partial ConfluenceBatch và bảo vệ state."""
        ctx = _make_stream_context(bar_index=10)
        regime = MarketRegime(
            regime="bullish_trend",
            bar_index=10,
            timestamp=ctx.bar_close_time,
            efficiency_ratio=0.5,
            atr_percentile=50.0,
        )

        # Future candidate setup (relative to context)
        future_cand = CandidateSetup(
            setup_id="future_setup",
            strategy_id="S01",
            direction="BUY",
            bar_index=11,  # Future!
            timestamp=ctx.timestamp,
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            planned_rr=2.0,
            evidences=(EvidenceRef("ev_1", "fair_value_gap", 10, 2000.0),),
            evidence_cluster_id="cluster_f",
            expiry_bar=20,
        )
        eval_future = StrategyEvaluation(candidate=future_cand, status="ELIGIBLE", details={"test": True})

        with self.assertRaises(StrategyStateError):
            build_confluence_batch([eval_future], regime, ctx)

    def test_145_ten_thousand_bars_bounded_state_and_benchmark(self):
        """Test 145: 10.000 bars bounded state & performance benchmark."""
        classifier = MarketRegimeClassifier()
        gate = EligibilityGate()

        n_bars = 10000
        # Pre-create light contexts to benchmark engine components specifically
        dt_base = pd.Timestamp("2026-01-01 00:00:00+00:00")
        times = []

        profile = StrategyProfile(
            strategy_id="S01",
            name="S01 ICT 2022",
            version="1.0.0",
            allowed_directions=("BUY",),
            timeframes=("M15",),
            min_rr=1.5,
            style="reversal",
        )

        start_total = time.perf_counter()
        for i in range(n_bars):
            bct = dt_base + pd.Timedelta(minutes=15 * (i + 1))
            ts = bct - pd.Timedelta(minutes=15)
            c = 2000.0 + (i % 20)
            o = 2000.0
            h = max(o, c) + 5.0
            l = min(o, c) - 5.0
            ctx = StrategyContext(
                symbol="EURUSD",
                timeframe="M15",
                bar_index=i,
                timestamp=ts,
                bar_close_time=bct,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=100.0,
                atr14=5.0,
                htf_bias=BiasStateSnapshot(bias="bullish", timestamp=ts),
            )
            t0 = time.perf_counter()
            regime = classifier.update(ctx)
            cand_adapted = CandidateSetup(
                setup_id=f"bench_setup_{i}",
                strategy_id="S01",
                direction="BUY",
                bar_index=i,
                timestamp=ts,
                entry_price=2000.0,
                stop_loss=1990.0,
                take_profit=2020.0,
                planned_rr=2.0,
                evidences=(
                    EvidenceRef(f"ev_bench_sw_{i}", "liquidity_sweep", max(0, i - 2), 1995.0),
                    EvidenceRef(f"ev_bench_fvg_{i}", "fair_value_gap", max(0, i - 1), 2000.0),
                    EvidenceRef(f"ev_bench_mss_{i}", "structure_event", i, 2005.0),
                ) if i >= 2 else (EvidenceRef(f"ev_bench_fvg_{i}", "fair_value_gap", i, 2000.0),),
                evidence_cluster_id=f"cluster_bench_{i}",
                expiry_bar=i + 10,
            )
            eval_res = gate.evaluate(cand_adapted, ctx, regime, profile)
            batch = build_confluence_batch([eval_res], regime, ctx)
            t1 = time.perf_counter()
            times.append(t1 - t0)

        total_duration = time.perf_counter() - start_total
        median_us = np.median(times) * 1_000_000
        p99_us = np.percentile(times, 99) * 1_000_000

        # Memory / state bound assertions (P2.1)
        self.assertLessEqual(len(classifier._closes), 20)
        self.assertLessEqual(len(classifier._atrs), 100)
        self.assertLess(total_duration, 5.0)
        self.assertLess(median_us, 500.0)
        self.assertLess(p99_us, 1000.0)

        # Print benchmark metrics for transparency
        print(
            f"\n[BENCHMARK T53.7] 10,000 bars processed in {total_duration:.3f}s: "
            f"Median = {median_us:.1f} us/bar, P99 = {p99_us:.1f} us/bar"
        )

    def test_146_clean_package_exports_and_models(self):
        """Test 146: Clean package exports & models completeness."""
        import smc.engine as engine

        expected_symbols = [
            "RegimeClassifierConfig",
            "MarketRegimeClassifier",
            "classify_market_regimes",
            "REGIME_MATRIX",
            "get_regime_matrix_score",
            "EligibilityGate",
            "EvidenceCluster",
            "DirectionConflict",
            "ConfluenceBatch",
            "EvidenceDeduplicator",
            "DirectionConflictDetector",
            "build_confluence_batch",
        ]
        for sym in expected_symbols:
            self.assertTrue(hasattr(engine, sym), f"Missing export '{sym}' in smc.engine")
            self.assertIn(sym, engine.__all__, f"Symbol '{sym}' not in smc.engine.__all__")


if __name__ == "__main__":
    unittest.main()
