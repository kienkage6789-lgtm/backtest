"""
tests.test_smc_engine_t53_8_integration
=======================================
Integration tests and 10,000-bar performance benchmark for T53.8.
Covers Test Matrix Group G (tests 126 to 140).
Uses real production pipeline: StrategyContextBuilder/Context -> StrategyRegistry
(S01, S05, S09) -> EligibilityGate -> build_confluence_batch ->
DeterministicStrategySelector -> SelectorOutput serialization -> aggregate_selection_telemetry.
"""

from __future__ import annotations

import datetime
import gc
import json
import os
import time
import unittest
from typing import Any, Sequence
import numpy as np
import pandas as pd

from smc.engine.confluence import (
    ConfluenceBatch,
    DirectionConflict,
    DirectionConflictDetector,
    EvidenceCluster,
    EvidenceDeduplicator,
    build_confluence_batch,
)
from smc.models import StructureEvent
from smc.engine.context import ContextBuilderConfig, StrategyContext, StrategyContextBuilder
from smc.engine.eligibility import (
    EligibilityGate,
    get_regime_matrix_score,
)
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    FairValueGapSnapshot,
    HTFPOISnapshot,
    LiquiditySweepSnapshot,
    MarketRegime,
    OrderBlockSnapshot,
    SelectionDecision,
    SessionDecisionSnapshot,
    StrategyEvaluation,
    StructureEventSnapshot,
)
from smc.engine.registry import StrategyRegistry
from smc.engine.selector import (
    ClusterScorecard,
    DeterministicStrategySelector,
    SelectorConfig,
    SelectorOutput,
    select_strategy,
)
from smc.engine.strategies import (
    S01Config,
    S01ICT2022Strategy,
    S05Config,
    S05BOSOBRetestStrategy,
    S09Config,
    S09ICTSilverBulletStrategy,
)
from smc.engine.strategies.s09_ict_silver_bullet import NEW_YORK_TZ
from smc.engine.telemetry import (
    SelectionAuditRecord,
    aggregate_selection_telemetry,
)
from tests.test_smc_strategy_s09 import _make_sweep, _make_structure, _make_fvg

BASE_NY_DATETIME = datetime.datetime(2024, 5, 15, 10, 0, 0, tzinfo=NEW_YORK_TZ)
BASE_UTC_TIMESTAMP = pd.Timestamp(BASE_NY_DATETIME).tz_convert("UTC")


def _make_pipe_context(
    bar_index: int = 100,
    timestamp: str | pd.Timestamp | None = None,
    bar_close_time: str | pd.Timestamp | None = None,
    bias: str = "bullish",
    in_session: bool = True,
    atr14: float = 5.0,
    sweeps: Sequence[LiquiditySweepSnapshot] = (),
    structures: Sequence[StructureEventSnapshot] = (),
    fvgs: Sequence[FairValueGapSnapshot] = (),
    obs: Sequence[OrderBlockSnapshot] = (),
    open_p: float = 2040.0,
    high_p: float = 2045.0,
    low_p: float = 2037.0,
    close_p: float = 2039.0,
    active_htf_pois: Optional[Sequence[Any]] = None,
) -> StrategyContext:
    if timestamp is None:
        ts_open = BASE_UTC_TIMESTAMP + pd.Timedelta(minutes=bar_index)
    elif isinstance(timestamp, str):
        ts_open = pd.Timestamp(timestamp)
    else:
        ts_open = timestamp

    if bar_close_time is None:
        ts_close = ts_open + pd.Timedelta(minutes=1)
    elif isinstance(bar_close_time, str):
        ts_close = pd.Timestamp(bar_close_time)
    else:
        ts_close = bar_close_time

    if active_htf_pois is None:
        poi_dir = bias if bias in {"bullish", "bearish"} else "bullish"
        pois_tuple = (
            HTFPOISnapshot(
                poi_id="poi_default_pipe_test",
                poi_type="FVG",
                direction=poi_dir,
                timeframe="H1",
                top=2100.0,
                bottom=2000.0,
                created_at=0,
                source_event="default_setup",
                valid_until=None,
                status="active",
                touch_count=1,
            ),
        )
    else:
        pois_tuple = tuple(active_htf_pois)

    return StrategyContext(
        symbol="EURUSD",
        timeframe="M1",
        bar_index=bar_index,
        timestamp=ts_open,
        bar_close_time=ts_close,
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=100.0,
        atr14=atr14,
        htf_bias=BiasStateSnapshot(
            bias=bias,  # type: ignore[arg-type]
            timestamp=ts_open,
            as_of=ts_open,
            source_event_time=ts_open,
        ),
        session_decision=SessionDecisionSnapshot(
            in_session=in_session,
            session_name="NY_AM",
            timestamp=ts_open,
            reason="ok",
        ),
        recent_sweeps=tuple(sweeps),
        recent_structures=tuple(structures),
        active_fvgs=tuple(fvgs),
        active_obs=tuple(obs),
        active_htf_pois=pois_tuple,
    )


def _make_pipe_ob(
    index: int,
    high: float = 2040.0,
    low: float = 2036.0,
    source_event_index: int = 6,
    created_at: int = 6,
    leg_id: str = "leg_s05",
    retest_count: int = 0,
    mitigated: bool = False,
) -> OrderBlockSnapshot:
    t = BASE_UTC_TIMESTAMP + pd.Timedelta(minutes=index)
    return OrderBlockSnapshot(
        index=index,
        time=t,
        direction="bullish",
        high=high,
        low=low,
        open=low,
        close=high,
        source_event_index=source_event_index,
        created_at=created_at,
        structure_leg_id=leg_id,
        quality="base",
        mode="internal",
        origin_type="BOS",
        source_event_type="BOS",
        valid=True,
        mitigated=mitigated,
        mitigated_at=7 if mitigated else None,
        retest_count=retest_count,
    )


def _make_pipe_bos(index: int, leg_id: str = "leg_s05") -> StructureEventSnapshot:
    t = BASE_UTC_TIMESTAMP + pd.Timedelta(minutes=index)
    return StructureEventSnapshot(
        index=index,
        time=t,
        direction="bullish",
        event_type="BOS",
        broken_swing_index=5,
        broken_swing_price=2045.0,
        close_price=2046.0,
        displacement=True,
        structure_leg_id=leg_id,
        mode="internal",
        confirmed_swing_at=index,
        break_type="close",
    )


def _make_regime(
    bar_index: int = 100,
    timestamp: str | pd.Timestamp = "2026-03-09 15:15:00+00:00",
    regime: str = "bullish_trend",
) -> MarketRegime:
    return MarketRegime(
        regime=regime,  # type: ignore[arg-type]
        bar_index=bar_index,
        timestamp=pd.Timestamp(timestamp) if isinstance(timestamp, str) else timestamp,
        efficiency_ratio=0.8,
        atr_percentile=50.0,
    )


def _make_candidate(
    setup_id: str,
    strategy_id: str,
    direction: str = "BUY",
    bar_index: int = 100,
    timestamp: str | pd.Timestamp = "2026-03-09 15:00:00+00:00",
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
    ev_idx = max(0, bar_index - 1)
    t = pd.Timestamp(timestamp) if isinstance(timestamp, str) else timestamp
    if not evidences:
        if strategy_id == "S05":
            evidences = (
                EvidenceRef(f"ev_{setup_id}", "order_block", ev_idx, entry, time=t, details={"quality": "base"}),
            )
        else:
            evidences = (
                EvidenceRef(f"ev_{setup_id}", "fair_value_gap", ev_idx, entry, time=t, details={"top": entry + 5.0, "bottom": entry - 5.0}),
            )
    return CandidateSetup(
        setup_id=setup_id,
        strategy_id=strategy_id,
        direction=direction,  # type: ignore[arg-type]
        bar_index=bar_index,
        timestamp=t,
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
    rejection_reasons: tuple[str, ...] = (),
    regime: str = "bullish_trend",
    regime_score: float | None = None,
) -> StrategyEvaluation:
    if status == "ELIGIBLE":
        if regime_score is None:
            regime_score = get_regime_matrix_score(candidate.strategy_id, candidate.direction, regime)
    else:
        regime_score = 0.0
    return StrategyEvaluation(
        candidate=candidate,
        status=status,  # type: ignore[arg-type]
        rejection_reasons=rejection_reasons,
        regime_score=regime_score,
        setup_score=0.0,
        context_score=0.0,
        exec_score=0.0,
        total_score=0.0,
        details={},
    )


class TestSelectorGroupGIntegration(unittest.TestCase):
    """Group G: End-to-end pipeline integration & benchmark tests (tests 126 to 140)."""

    def setUp(self):
        self.context = _make_pipe_context(bar_index=100)
        self.regime = _make_regime(bar_index=100, timestamp=self.context.bar_close_time)
        self.selector = DeterministicStrategySelector()

    def test_126_s01_pipeline_via_context_builder(self):
        """
        Test 126: S01 alone via StrategyContextBuilder with real candle sequence through
        StrategyRegistry(S01) -> EligibilityGate -> build_confluence_batch
        -> DeterministicStrategySelector -> SelectorOutput -> aggregate_selection_telemetry.
        """
        cfg = ContextBuilderConfig(
            swing_strength=1,
            swing_left_strength=1,
            swing_right_strength=1,
            fvg_min_gap_pct=0.0,
            atr_period=3,
        )
        htf_ev = StructureEvent(
            index=0,
            time=pd.Timestamp("2026-01-15 08:00:00+00:00"),
            direction="bullish",
            event_type="BOS",
            broken_swing_index=0,
            broken_swing_price=100.0,
            close_price=101.0,
            displacement=True,
            structure_leg_id="htf_1",
            mode="swing",
            confirmed_swing_at=0,
            break_type="close",
        )
        builder = StrategyContextBuilder(cfg, htf_events=[htf_ev])

        candles = [
            {"bar_index": 0, "time": "2026-01-15 08:00:00 UTC", "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 100.0, "closed": True},
            {"bar_index": 1, "time": "2026-01-15 08:01:00 UTC", "open": 100.0, "high": 101.0, "low": 95.0, "close": 97.0, "volume": 100.0, "closed": True},
            {"bar_index": 2, "time": "2026-01-15 08:02:00 UTC", "open": 97.0, "high": 105.0, "low": 97.0, "close": 104.0, "volume": 100.0, "closed": True},
            {"bar_index": 3, "time": "2026-01-15 08:03:00 UTC", "open": 104.0, "high": 104.5, "low": 98.0, "close": 99.0, "volume": 100.0, "closed": True},
            {"bar_index": 4, "time": "2026-01-15 08:04:00 UTC", "open": 99.0, "high": 99.0, "low": 93.0, "close": 98.0, "volume": 100.0, "closed": True},
            {"bar_index": 5, "time": "2026-01-15 08:05:00 UTC", "open": 98.0, "high": 100.0, "low": 97.5, "close": 100.0, "volume": 100.0, "closed": True},
            {"bar_index": 6, "time": "2026-01-15 08:06:00 UTC", "open": 100.0, "high": 108.0, "low": 99.8, "close": 107.0, "volume": 200.0, "closed": True},
            {"bar_index": 7, "time": "2026-01-15 08:07:00 UTC", "open": 107.0, "high": 112.0, "low": 102.0, "close": 110.0, "volume": 150.0, "closed": True},
            {"bar_index": 8, "time": "2026-01-15 08:08:00 UTC", "open": 110.0, "high": 115.0, "low": 109.0, "close": 114.0, "volume": 100.0, "closed": True},
            {"bar_index": 9, "time": "2026-01-15 08:09:00 UTC", "open": 114.0, "high": 114.0, "low": 99.5, "close": 101.0, "volume": 100.0, "closed": True},
        ]

        strat = S01ICT2022Strategy(S01Config(mode="swing", min_rr=1.0, require_displacement=False))
        registry = StrategyRegistry([strat])

        contexts = [builder.update(c) for c in candles]
        for ctx in contexts[:-1]:
            registry.evaluate_enabled(ctx)
        cands9 = registry.evaluate_enabled(contexts[9])

        ctx9 = contexts[9]
        gate = EligibilityGate()
        regime9 = MarketRegime(
            regime="bullish_trend",
            bar_index=9,
            timestamp=ctx9.bar_close_time,
            efficiency_ratio=0.8,
            atr_percentile=50.0,
        )
        evals = gate.evaluate_registry_output(cands9, ctx9, regime9, registry.profiles)
        self.assertEqual(len(evals), 1)
        self.assertEqual(evals[0].status, "ELIGIBLE")
        self.assertEqual(evals[0].candidate.strategy_id, "S01")

        batch = build_confluence_batch(evals, regime9, ctx9)
        self.assertEqual(len(batch.eligible_clusters), 1)
        self.assertEqual(len(batch.eligible_clusters[0].members), 1)

        out = self.selector.select(batch, ctx9)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.reason, "ok")
        self.assertEqual(out.decision.primary_strategy_id, "S01")
        self.assertEqual(out.decision.supporting_strategy_ids, ())
        self.assertEqual(out.decision.execution_payload, {})
        self.assertEqual(out.audit_record.action, "SELECT")
        self.assertEqual(out.audit_record.evaluated_count, 1)
        self.assertEqual(out.audit_record.eligible_count, 1)
        self.assertEqual(out.audit_record.rejected_count, 0)
        self.assertEqual(len(out.scorecards), 1)
        self.assertEqual(out.scorecards[0].member_count, 1)

        # JSON Roundtrip
        out_restored = SelectorOutput.from_dict(out.to_dict())
        self.assertEqual(out, out_restored)

        # Telemetry Aggregation
        agg = aggregate_selection_telemetry([out.audit_record])
        self.assertEqual(agg["total_bars"], 1)
        self.assertEqual(agg["select_count"], 1)
        self.assertEqual(agg["strategy_selections"], {"S01": 1})

    def test_127_s05_bos_ob_first_retest_pipeline(self):
        """
        Test 127: S05 alone (BOS -> OB -> first retest) through real production pipeline:
        StrategyRegistry(S05) -> EligibilityGate -> build_confluence_batch
        -> DeterministicStrategySelector -> SelectorOutput -> aggregate_selection_telemetry.
        """
        strat = S05BOSOBRetestStrategy(S05Config(mode="internal"))
        registry = StrategyRegistry([strat])
        gate = EligibilityGate()

        # Warmup 0..3
        for b in range(4):
            registry.evaluate_enabled(_make_pipe_context(bar_index=b, bias="bullish"))

        # Bar 4: BOS + OB created
        bos = _make_pipe_bos(index=4, leg_id="leg_s05")
        ob = _make_pipe_ob(index=3, source_event_index=4, created_at=4, leg_id="leg_s05", high=2040.0, low=2036.0)
        registry.evaluate_enabled(_make_pipe_context(bar_index=4, structures=[bos], obs=[ob], bias="bullish"))

        # Bar 5: Retest candle enters OB [2036, 2040] with low 2037.0, close 2039.0
        t = _make_pipe_ob(index=3, source_event_index=4, created_at=4, leg_id="leg_s05", high=2040.0, low=2036.0, retest_count=1, mitigated=True)
        ob_retested = OrderBlockSnapshot(
            index=t.index, time=t.time, direction=t.direction, high=t.high, low=t.low, open=t.open, close=t.close,
            source_event_index=t.source_event_index, created_at=t.created_at, structure_leg_id=t.structure_leg_id,
            quality=t.quality, mode=t.mode, origin_type=t.origin_type, source_event_type=t.source_event_type,
            valid=True, mitigated=True, mitigated_at=5, retest_count=1,
        )
        ctx5 = _make_pipe_context(bar_index=5, structures=[bos], obs=[ob_retested], low_p=2037.0, close_p=2039.0, bias="bullish")
        cands = registry.evaluate_enabled(ctx5)

        self.assertIn("S05", cands)
        self.assertEqual(len(cands["S05"]), 1)

        regime5 = MarketRegime(
            regime="bullish_trend",
            bar_index=5,
            timestamp=ctx5.bar_close_time,
            efficiency_ratio=0.8,
            atr_percentile=50.0,
        )
        evals = gate.evaluate_registry_output(cands, ctx5, regime5, registry.profiles)
        self.assertEqual(len(evals), 1)
        self.assertEqual(evals[0].status, "ELIGIBLE")
        self.assertEqual(evals[0].candidate.strategy_id, "S05")

        batch = build_confluence_batch(evals, regime5, ctx5)
        self.assertEqual(len(batch.eligible_clusters), 1)
        self.assertEqual(len(batch.eligible_clusters[0].members), 1)

        out = self.selector.select(batch, ctx5)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.reason, "ok")
        self.assertEqual(out.decision.primary_strategy_id, "S05")
        self.assertEqual(out.decision.supporting_strategy_ids, ())
        self.assertEqual(out.decision.execution_payload, {})
        self.assertEqual(out.audit_record.action, "SELECT")
        self.assertEqual(len(out.scorecards), 1)
        self.assertEqual(out.scorecards[0].member_count, 1)

        # JSON Roundtrip
        out_restored = SelectorOutput.from_dict(out.to_dict())
        self.assertEqual(out, out_restored)

        # Telemetry Aggregation
        agg = aggregate_selection_telemetry([out.audit_record])
        self.assertEqual(agg["total_bars"], 1)
        self.assertEqual(agg["select_count"], 1)
        self.assertEqual(agg["strategy_selections"], {"S05": 1})

    def test_128_s09_silver_bullet_window_pipeline(self):
        """
        Test 128: S09 alone (Silver Bullet window sweep -> MSS -> FVG retest) through real production pipeline:
        StrategyRegistry(S09) -> EligibilityGate -> build_confluence_batch
        -> DeterministicStrategySelector -> SelectorOutput -> aggregate_selection_telemetry.
        """
        strat = S09ICTSilverBulletStrategy(S09Config(mode="internal"))
        registry = StrategyRegistry([strat])
        gate = EligibilityGate()

        # Warmup 0..4 inside NY session window
        for b in range(5):
            registry.evaluate_enabled(_make_pipe_context(bar_index=b, bias="bullish"))

        # Bar 5: Liquidity Sweep
        sw = _make_sweep(index=5, direction="bullish", price_wick=2035.0)
        registry.evaluate_enabled(_make_pipe_context(bar_index=5, sweeps=[sw], bias="bullish"))

        # Bar 6: MSS + FVG
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg_s09")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg_s09")
        registry.evaluate_enabled(_make_pipe_context(bar_index=6, structures=[mss], fvgs=[fvg], bias="bullish"))

        # Bar 7: Retest candle enters FVG
        ctx7 = _make_pipe_context(bar_index=7, structures=[mss], fvgs=[fvg], low_p=2037.0, close_p=2039.0, bias="bullish")
        cands = registry.evaluate_enabled(ctx7)

        self.assertIn("S09", cands)
        self.assertEqual(len(cands["S09"]), 1)

        regime7 = MarketRegime(
            regime="bullish_trend",
            bar_index=7,
            timestamp=ctx7.bar_close_time,
            efficiency_ratio=0.8,
            atr_percentile=50.0,
        )
        evals = gate.evaluate_registry_output(cands, ctx7, regime7, registry.profiles)
        self.assertEqual(len(evals), 1)
        self.assertEqual(evals[0].status, "ELIGIBLE")
        self.assertEqual(evals[0].candidate.strategy_id, "S09")

        batch = build_confluence_batch(evals, regime7, ctx7)
        self.assertEqual(len(batch.eligible_clusters), 1)
        self.assertEqual(len(batch.eligible_clusters[0].members), 1)

        out = self.selector.select(batch, ctx7)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.reason, "ok")
        self.assertEqual(out.decision.primary_strategy_id, "S09")
        self.assertEqual(out.decision.supporting_strategy_ids, ())
        self.assertEqual(out.decision.execution_payload, {})
        self.assertEqual(out.audit_record.action, "SELECT")
        self.assertEqual(len(out.scorecards), 1)
        self.assertEqual(out.scorecards[0].member_count, 1)

        # JSON Roundtrip
        out_restored = SelectorOutput.from_dict(out.to_dict())
        self.assertEqual(out, out_restored)

        # Telemetry Aggregation
        agg = aggregate_selection_telemetry([out.audit_record])
        self.assertEqual(agg["total_bars"], 1)
        self.assertEqual(agg["select_count"], 1)
        self.assertEqual(agg["strategy_selections"], {"S09": 1})

    def test_129_s01_and_s09_shared_opportunity_cluster_merge(self):
        """
        Test 129: S01 & S09 shared opportunity: both strategies detect same FVG retest opportunity
        inside Silver Bullet window, merging into 1 cluster with primary and supporting attribution.
        """
        registry = StrategyRegistry([
            S01ICT2022Strategy(S01Config(mode="internal")),
            S09ICTSilverBulletStrategy(S09Config(mode="internal")),
        ])
        gate = EligibilityGate()

        # Warmup 0..4
        for b in range(5):
            registry.evaluate_enabled(_make_pipe_context(bar_index=b, bias="bullish"))

        # Bar 5: Sweep
        sw = _make_sweep(index=5, direction="bullish", price_wick=2035.0)
        registry.evaluate_enabled(_make_pipe_context(bar_index=5, sweeps=[sw], bias="bullish"))

        # Bar 6: MSS + FVG
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        registry.evaluate_enabled(_make_pipe_context(bar_index=6, structures=[mss], fvgs=[fvg], bias="bullish"))

        # Bar 7: Retest candle enters FVG
        ctx7 = _make_pipe_context(bar_index=7, structures=[mss], fvgs=[fvg], low_p=2037.0, close_p=2039.0, bias="bullish")
        cands = registry.evaluate_enabled(ctx7)

        self.assertIn("S01", cands)
        self.assertIn("S09", cands)
        self.assertEqual(len(cands["S01"]), 1)
        self.assertEqual(len(cands["S09"]), 1)

        regime7 = MarketRegime(
            regime="bullish_trend",
            bar_index=7,
            timestamp=ctx7.bar_close_time,
            efficiency_ratio=0.8,
            atr_percentile=50.0,
        )
        evals = gate.evaluate_registry_output(cands, ctx7, regime7, registry.profiles)
        self.assertEqual(len(evals), 2)
        self.assertTrue(all(e.status == "ELIGIBLE" for e in evals))

        batch = build_confluence_batch(evals, regime7, ctx7)
        # S01 & S09 merged into 1 cluster
        self.assertEqual(len(batch.eligible_clusters), 1)
        self.assertEqual(len(batch.eligible_clusters[0].members), 2)

        out = self.selector.select(batch, ctx7)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.reason, "ok")
        self.assertEqual(out.decision.primary_strategy_id, "S09")
        self.assertEqual(out.decision.supporting_strategy_ids, ("S01",))
        self.assertEqual(out.decision.execution_payload, {})
        self.assertEqual(len(out.scorecards), 1)
        self.assertEqual(out.scorecards[0].member_count, 2)
        self.assertEqual(out.scorecards[0].supporting_strategy_ids, ("S01",))

        # JSON Roundtrip
        out_restored = SelectorOutput.from_dict(out.to_dict())
        self.assertEqual(out, out_restored)

        # Telemetry Aggregation
        agg = aggregate_selection_telemetry([out.audit_record])
        self.assertEqual(agg["total_bars"], 1)
        self.assertEqual(agg["select_count"], 1)
        self.assertEqual(agg["supporting_strategy_counts"], {"S01": 1})

    def test_130_s05_distinct_cluster_alongside_s01_s09_shared_cluster(self):
        """
        Test 130: S05 distinct cluster alongside S01/S09 shared cluster.
        S01/S09 cluster on FVG retest; S05 cluster on OB retest.
        Both clusters evaluated with deterministic ranking selecting decisive winner.
        """
        registry = StrategyRegistry([
            S01ICT2022Strategy(S01Config(mode="internal")),
            S05BOSOBRetestStrategy(S05Config(mode="internal")),
            S09ICTSilverBulletStrategy(S09Config(mode="internal")),
        ])
        gate = EligibilityGate()

        # Warmup 0..4
        for b in range(5):
            registry.evaluate_enabled(_make_pipe_context(bar_index=b, bias="bullish"))

        # Bar 5: Sweep for S01/S09
        sw = _make_sweep(index=5, direction="bullish", price_wick=2035.0)
        registry.evaluate_enabled(_make_pipe_context(bar_index=5, sweeps=[sw], bias="bullish"))

        # Bar 6: MSS + FVG for S01/S09; BOS + OB for S05
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        bos = _make_pipe_bos(index=6, leg_id="leg_s05")
        ob = _make_pipe_ob(index=4, source_event_index=6, created_at=6, leg_id="leg_s05", high=2040.0, low=2036.0)
        registry.evaluate_enabled(_make_pipe_context(bar_index=6, structures=[mss, bos], fvgs=[fvg], obs=[ob], bias="bullish"))

        # Bar 7: Retest candle triggers candidates on both FVG (S01/S09) and OB (S05)
        ob_retested = OrderBlockSnapshot(
            index=ob.index, time=ob.time, direction=ob.direction, high=ob.high, low=ob.low, open=ob.open, close=ob.close,
            source_event_index=ob.source_event_index, created_at=ob.created_at, structure_leg_id=ob.structure_leg_id,
            quality=ob.quality, mode=ob.mode, origin_type=ob.origin_type, source_event_type=ob.source_event_type,
            valid=True, mitigated=True, mitigated_at=7, retest_count=1,
        )
        ctx7 = _make_pipe_context(bar_index=7, structures=[mss, bos], fvgs=[fvg], obs=[ob_retested], low_p=2037.0, close_p=2039.0, bias="bullish")
        cands = registry.evaluate_enabled(ctx7)

        self.assertIn("S01", cands)
        self.assertIn("S05", cands)
        self.assertIn("S09", cands)

        regime7 = MarketRegime(
            regime="bullish_trend",
            bar_index=7,
            timestamp=ctx7.bar_close_time,
            efficiency_ratio=0.8,
            atr_percentile=50.0,
        )
        evals = gate.evaluate_registry_output(cands, ctx7, regime7, registry.profiles)
        self.assertEqual(len(evals), 3)

        batch = build_confluence_batch(evals, regime7, ctx7)
        # 2 distinct clusters: shared cluster (S01/S09, 2 members) and distinct cluster (S05, 1 member)
        self.assertEqual(len(batch.eligible_clusters), 2)
        members_counts = sorted(len(c.members) for c in batch.eligible_clusters)
        self.assertEqual(members_counts, [1, 2])

        out = self.selector.select(batch, ctx7)
        self.assertEqual(out.decision.action, "SELECT")
        self.assertEqual(out.decision.reason, "ok")
        self.assertEqual(len(out.scorecards), 2)
        # S09 wins primary, S01 is supporting attribution
        self.assertEqual(out.decision.primary_strategy_id, "S09")
        self.assertEqual(out.decision.supporting_strategy_ids, ("S01",))
        self.assertEqual(out.decision.execution_payload, {})

        # JSON Roundtrip
        out_restored = SelectorOutput.from_dict(out.to_dict())
        self.assertEqual(out, out_restored)

        # Telemetry Aggregation
        agg = aggregate_selection_telemetry([out.audit_record])
        self.assertEqual(agg["total_bars"], 1)
        self.assertEqual(agg["select_count"], 1)
        self.assertEqual(agg["strategy_selections"], {"S09": 1})
        self.assertEqual(agg["supporting_strategy_counts"], {"S01": 1})

    def test_131_zero_lookahead_verification(self):
        """
        Test 131: Zero lookahead verification.
        Selector strictly operates on bar N without inspecting N+1; execution_payload is empty {}.
        """
        c = _make_candidate("s1", "S01", "BUY", bar_index=100, timestamp=self.context.timestamp)
        ev = _make_eval(c, status="ELIGIBLE", regime=self.regime.regime)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=self.context.bar_close_time,
            regime=self.regime,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        out = self.selector.select(batch, self.context)

        self.assertEqual(dict(out.decision.execution_payload), {})
        self.assertEqual(out.decision.bar_index, 100)
        self.assertEqual(out.audit_record.bar_index, 100)
        self.assertEqual(out.decision.timestamp, self.context.bar_close_time)
        self.assertEqual(out.audit_record.timestamp, self.context.bar_close_time)

    def test_132_json_roundtrip_decision_and_audit(self):
        """
        Test 132: Exact JSON roundtrip of SelectionDecision, SelectionAuditRecord, and SelectorOutput.
        """
        c = _make_candidate("s1", "S01", "BUY", bar_index=100, timestamp=self.context.timestamp)
        ev = _make_eval(c, status="ELIGIBLE", regime=self.regime.regime)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=self.context.bar_close_time,
            regime=self.regime,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        out = self.selector.select(batch, self.context)

        dec_json = json.dumps(out.decision.to_dict(), allow_nan=False)
        aud_json = json.dumps(out.audit_record.to_dict(), allow_nan=False)
        out_json = json.dumps(out.to_dict(), allow_nan=False)

        r_dec = SelectionDecision.from_dict(json.loads(dec_json))
        r_aud = SelectionAuditRecord.from_dict(json.loads(aud_json))
        r_out = SelectorOutput.from_dict(json.loads(out_json))

        self.assertEqual(out.decision, r_dec)
        self.assertEqual(out.audit_record, r_aud)
        self.assertEqual(out, r_out)

    def test_133_stream_simulation_50_bars_aggregated_telemetry(self):
        """
        Test 133: Stream simulation of 50 consecutive bars with realistic contexts.
        Telemetry aggregation verifies metrics, counts, and anti-double-count.
        """
        records = []
        base_open = BASE_UTC_TIMESTAMP

        for i in range(50):
            ts_open = base_open + pd.Timedelta(minutes=15 * i)
            ts_close = ts_open + pd.Timedelta(minutes=15)
            ctx = _make_pipe_context(bar_index=i, timestamp=ts_open, bar_close_time=ts_close)
            reg = _make_regime(bar_index=i, timestamp=ts_close)

            if i % 3 == 0:
                # SELECT S01
                c = _make_candidate(f"s_{i}", "S01", "BUY", bar_index=i, timestamp=ts_open, planned_rr=2.5)
                ev = _make_eval(c, status="ELIGIBLE", regime=reg.regime)
                cl = EvidenceCluster(f"c_{i}", "BUY", (ev,))
                batch = ConfluenceBatch(
                    bar_index=i, timestamp=ts_close, regime=reg, evaluations=(ev,), eligible_clusters=(cl,)
                )
            elif i % 3 == 1:
                # SELECT S05
                c = _make_candidate(f"s_{i}", "S05", "BUY", bar_index=i, timestamp=ts_open, planned_rr=2.0)
                ev = _make_eval(c, status="ELIGIBLE", regime=reg.regime)
                cl = EvidenceCluster(f"c_{i}", "BUY", (ev,))
                batch = ConfluenceBatch(
                    bar_index=i, timestamp=ts_close, regime=reg, evaluations=(ev,), eligible_clusters=(cl,)
                )
            else:
                # NO_TRADE
                c = _make_candidate(f"s_{i}", "S01", "BUY", bar_index=i, timestamp=ts_open)
                ev = _make_eval(c, status="REJECTED", rejection_reasons=("wrong_regime",), regime=reg.regime)
                batch = ConfluenceBatch(
                    bar_index=i, timestamp=ts_close, regime=reg, evaluations=(ev,), eligible_clusters=()
                )

            out = self.selector.select(batch, ctx)
            records.append(out.audit_record)

        agg = aggregate_selection_telemetry(records)
        self.assertEqual(agg["total_bars"], 50)
        self.assertEqual(agg["total_decisions"], 50)
        self.assertEqual(agg["select_count"], 34)
        self.assertEqual(agg["no_trade_count"], 16)
        self.assertEqual(agg["select_rate"], 0.68)
        self.assertEqual(agg["strategy_selections"]["S01"], 17)
        self.assertEqual(agg["strategy_selections"]["S05"], 17)
        self.assertEqual(agg["reasons"]["ok"], 34)
        self.assertEqual(agg["reasons"]["no_eligible_setup"], 16)

    def test_134_replay_idempotence(self):
        """
        Test 134: Replay idempotence.
        Calling select() multiple times on exact same input produces identical output.
        """
        c = _make_candidate("s1", "S01", "BUY", bar_index=100, timestamp=self.context.timestamp, planned_rr=2.5)
        ev = _make_eval(c, status="ELIGIBLE", regime=self.regime.regime)
        cl = EvidenceCluster("c1", "BUY", (ev,))
        batch = ConfluenceBatch(
            bar_index=100,
            timestamp=self.context.bar_close_time,
            regime=self.regime,
            evaluations=(ev,),
            eligible_clusters=(cl,),
        )
        out1 = self.selector.select(batch, self.context)
        out2 = self.selector.select(batch, self.context)

        self.assertEqual(out1.decision.to_dict(), out2.decision.to_dict())
        self.assertEqual(out1.audit_record.to_dict(), out2.audit_record.to_dict())
        self.assertEqual([s.to_dict() for s in out1.scorecards], [s.to_dict() for s in out2.scorecards])

    def test_135_memory_boundedness_per_bar(self):
        """
        Test 135: Memory boundedness.
        Selector does not accumulate mutable state across calls.
        """
        s = DeterministicStrategySelector()
        self.assertEqual(id(s.config), id(s._config))

    def test_136_empty_stream_handling(self):
        """
        Test 136: Empty telemetry stream handling.
        """
        agg = aggregate_selection_telemetry([])
        self.assertEqual(agg["total_decisions"], 0)
        self.assertEqual(agg["total_bars"], 0)

    def test_137_invalid_weights_config_rejected(self):
        """
        Test 137: Invalid weights configuration rejected with ValueError.
        """
        with self.assertRaises(ValueError):
            SelectorConfig(weight_regime=0.99)

    def test_138_all_bars_no_trade_stream(self):
        """
        Test 138: All-bars NO_TRADE stream rollup.
        """
        records = []
        base_open = BASE_UTC_TIMESTAMP
        for i in range(10):
            ts_open = base_open + pd.Timedelta(minutes=15 * i)
            ts_close = ts_open + pd.Timedelta(minutes=15)
            ctx = _make_pipe_context(bar_index=i, timestamp=ts_open, bar_close_time=ts_close)
            reg = _make_regime(bar_index=i, timestamp=ts_close)
            c = _make_candidate(f"s_{i}", "S01", "BUY", bar_index=i, timestamp=ts_open)
            ev = _make_eval(c, status="REJECTED", rejection_reasons=("wrong_regime",), regime=reg.regime)
            batch = ConfluenceBatch(
                bar_index=i, timestamp=ts_close, regime=reg, evaluations=(ev,), eligible_clusters=()
            )
            out = self.selector.select(batch, ctx)
            records.append(out.audit_record)

        agg = aggregate_selection_telemetry(records)
        self.assertEqual(agg["select_count"], 0)
        self.assertEqual(agg["no_trade_count"], 10)
        self.assertEqual(agg["select_rate"], 0.0)

    def test_139_all_bars_select_stream(self):
        """
        Test 139: All-bars SELECT stream rollup.
        """
        records = []
        base_open = BASE_UTC_TIMESTAMP
        for i in range(10):
            ts_open = base_open + pd.Timedelta(minutes=15 * i)
            ts_close = ts_open + pd.Timedelta(minutes=15)
            ctx = _make_pipe_context(bar_index=i, timestamp=ts_open, bar_close_time=ts_close)
            reg = _make_regime(bar_index=i, timestamp=ts_close)
            c = _make_candidate(f"s_{i}", "S01", "BUY", bar_index=i, timestamp=ts_open, planned_rr=2.5)
            ev = _make_eval(c, status="ELIGIBLE", regime=reg.regime)
            cl = EvidenceCluster(f"c_{i}", "BUY", (ev,))
            batch = ConfluenceBatch(
                bar_index=i, timestamp=ts_close, regime=reg, evaluations=(ev,), eligible_clusters=(cl,)
            )
            out = self.selector.select(batch, ctx)
            records.append(out.audit_record)

        agg = aggregate_selection_telemetry(records)
        self.assertEqual(agg["select_count"], 10)
        self.assertEqual(agg["no_trade_count"], 0)
        self.assertEqual(agg["select_rate"], 1.0)

    @unittest.skipUnless(
        os.environ.get("RUN_SMC_SELECTOR_PERFORMANCE_TESTS") == "1"
        or os.environ.get("RUN_SMC_PERFORMANCE_TESTS") == "1",
        "Performance benchmark is opt-in; set RUN_SMC_SELECTOR_PERFORMANCE_TESTS=1 to run",
    )
    def test_140_benchmark_10k_bars_throughput_and_latency(self):
        """
        Performance benchmark: Stream 10,000 synthetic confluence batches through the selector.
        Requirements:
        - Warm-up loop (100 iterations) before timing.
        - Monotonic clock: time.perf_counter().
        - NO print statements inside the timed block.
        - Report total wall-clock, median latency, P99 latency.
        - Threshold: Total time < 3.00 seconds; Median latency < 250 µs/bar.
        """
        n_bars = 10_000
        selector = DeterministicStrategySelector()

        ts_open = BASE_UTC_TIMESTAMP
        ts_close = ts_open + pd.Timedelta(minutes=15)
        ctx = _make_pipe_context(bar_index=0, timestamp=ts_open, bar_close_time=ts_close)
        reg = _make_regime(bar_index=0, timestamp=ts_close)
        c1 = _make_candidate("s1", "S01", "BUY", bar_index=0, timestamp=ts_open, planned_rr=2.5)
        c2 = _make_candidate("s2", "S05", "BUY", bar_index=0, timestamp=ts_open, planned_rr=2.0)
        ev1 = _make_eval(c1, status="ELIGIBLE", regime=reg.regime)
        ev2 = _make_eval(c2, status="ELIGIBLE", regime=reg.regime)
        cl1 = EvidenceCluster("c1", "BUY", (ev1, ev2))
        batch = ConfluenceBatch(
            bar_index=0, timestamp=ts_close, regime=reg, evaluations=(ev1, ev2), eligible_clusters=(cl1,)
        )

        # Warm-up (100 iterations)
        for _ in range(100):
            _ = selector.select(batch, ctx)

        latencies = np.empty(n_bars, dtype=np.float64)

        gc.collect()
        gc.disable()
        try:
            t_start = time.perf_counter()
            for i in range(n_bars):
                t0 = time.perf_counter()
                _ = selector.select(batch, ctx)
                t1 = time.perf_counter()
                latencies[i] = t1 - t0
            total_time = time.perf_counter() - t_start
        finally:
            gc.enable()

        median_us = float(np.median(latencies) * 1e6)
        p99_us = float(np.percentile(latencies, 99) * 1e6)

        # Print report only AFTER the timed block
        print(
            f"\n[BENCHMARK T53.8] 10,000 bars processed in {total_time:.3f}s: "
            f"Median = {median_us:.1f} us/bar, P99 = {p99_us:.1f} us/bar"
        )

        self.assertLess(
            total_time,
            3.00,
            f"Total benchmark duration {total_time:.3f}s exceeded 3.00s threshold",
        )
        self.assertLess(
            median_us,
            250.0,
            f"Median latency {median_us:.1f} µs/bar exceeded 250.0 µs threshold",
        )


if __name__ == "__main__":
    unittest.main()
