"""
Independent QC Probe Script for T53.9.3 - Bar-by-bar SMC Coordinator, Cooldown-after-fill, and HTF as-of timeline.
"""
import sys
import json
import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

from smc.engine.backtest_adapter import (
    SMCBacktestCoordinator,
    HTFTimeline,
    StepResult,
    CoordinatorResult,
)
from smc.engine.execution import (
    CooldownBook,
    PendingExecutionIntent,
    ExecutionEvent,
    ExecutionConfig,
    validate_fill,
)
from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    SelectionDecision,
    StrategyEvaluation,
    make_evidence_id,
    make_setup_id,
    make_cluster_id,
)
from smc.engine.errors import StrategyStateError, StrategyValidationError
from tests.test_smc_engine_backtest_adapter import (
    MockProgrammableStrategy,
    _make_candidate_setup,
    _create_synthetic_candles,
    _make_htf_event,
    _make_utc_timestamp,
)

UTC = ZoneInfo("UTC")

def run_probes():
    print("=== RUNNING INDEPENDENT QC PROBES (T53.9.3) ===")

    # Probe 1: SelectionDecision.execution_payload remains strictly empty {}
    strat = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
    strat.schedule_setup(3, setup)
    coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
    df = _create_synthetic_candles(6)
    res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

    for d in res.decisions:
        assert d.execution_payload == {}, f"Execution payload modified! Got: {d.execution_payload}"
    print("decision_execution_payload_strictly_empty=True")

    # Probe 2: Cooldown boundary contract F .. F+K-1 blocked, F+K pass
    book = CooldownBook()
    book.record_fill("S01", "BUY", 10, cooldown_bars=3)
    assert book.is_active("S01", "BUY", 10) is True
    assert book.is_active("S01", "BUY", 11) is True
    assert book.is_active("S01", "BUY", 12) is True
    assert book.is_active("S01", "BUY", 13) is False
    assert book.is_active("S01", "SELL", 10) is False
    assert book.is_active("S05", "BUY", 10) is False
    print("cooldown_boundary_and_isolation=True")

    # Probe 3: Snapshot immutability & JSON round-trip
    snap = book.snapshot()
    json_snap = json.dumps(snap)
    book_reloaded = CooldownBook.from_snapshot(json.loads(json_snap))
    assert book_reloaded.is_active("S01", "BUY", 12) is True
    assert book_reloaded.is_active("S01", "BUY", 13) is False
    print("cooldown_snapshot_json_roundtrip=True")

    # Probe 4: HTF Timeline exact boundary vs future event withholding
    t0 = _make_utc_timestamp(0)
    t15 = _make_utc_timestamp(15)
    t30 = _make_utc_timestamp(30)
    ev_past = _make_htf_event(0, 0)
    ev_boundary = _make_htf_event(1, 15)
    ev_future = _make_htf_event(2, 30)

    timeline = HTFTimeline([ev_past, ev_boundary, ev_future])
    as_of_15 = timeline.get_events_as_of(t15)
    assert len(as_of_15) == 2, f"Expected 2 events as of 15m, got {len(as_of_15)}"
    assert as_of_15[0].index == 0
    assert as_of_15[1].index == 1
    print("htf_timeline_exact_boundary_admitted=True")

    # Probe 5: Open phase fill price calculation with spread
    cfg = ExecutionConfig(spread_points=10.0, point_value=0.01) # spread = 0.10
    intent_buy = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
    eval_buy = StrategyEvaluation(
        candidate=intent_buy,
        status="ELIGIBLE",
        rejection_reasons=(),
        regime_score=100.0,
        setup_score=80.0,
        context_score=70.0,
        exec_score=60.0,
        total_score=80.0,
        details={},
    )
    p_buy = PendingExecutionIntent.from_selection_decision(
        SelectionDecision(
            decision_id="sel:3:SELECT:S01",
            bar_index=3,
            timestamp=_make_utc_timestamp(3 * 15),
            action="SELECT",
            selected_setup=intent_buy,
            primary_strategy_id="S01",
            supporting_strategy_ids=(),
            regime=None,
            evaluations=(eval_buy,),
            reason="ok",
            score_gap=None,
            execution_payload={},
        ),
        "XAUUSD", "M15", 1.5,
    )
    val_buy = validate_fill(p_buy, open_price=2000.0, config=cfg)
    assert val_buy.is_valid is True
    assert val_buy.actual_entry == 2000.10
    assert val_buy.actual_sl == 1990.0
    assert val_buy.actual_tp == 2030.0
    print("fill_price_and_spread_calculation=True")

    # Probe 6: Cash RR rejection when RR < min_rr
    val_bad_rr = validate_fill(p_buy, open_price=2020.0, config=cfg) # open gapped up, reducing reward and increasing risk
    assert val_bad_rr.is_valid is False
    assert val_bad_rr.reason == "insufficient_rr_at_fill"
    print("cash_rr_insufficient_rejected=True")

    # Probe 7: Geometry violation when open gapped below SL
    val_bad_geom = validate_fill(p_buy, open_price=1980.0, config=cfg)
    assert val_bad_geom.is_valid is False
    assert val_bad_geom.reason == "geometry_violation_at_fill"
    print("geometry_violation_rejected=True")

    # Probe 8: ExecutionEvent schema version and JSON serialization
    for evt in res.execution_events:
        assert evt.event_version == "1.0.0"
        d_evt = evt.to_dict()
        s_evt = json.dumps(d_evt)
        assert json.loads(s_evt)["event_id"] == evt.event_id
    print("execution_event_schema_version_and_json_safety=True")

    # Probe 9: Batch vs incremental exact bit-for-bit parity
    coord_incr = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", htf_events=[_make_htf_event(0, 0)])
    for idx in range(len(df)):
        coord_incr.step(df.iloc[idx], is_last_bar=(idx == len(df) - 1))
    
    assert len(coord_incr._all_decisions) == len(res.decisions)
    assert len(coord_incr._all_events) == len(res.execution_events)
    assert coord_incr.cooldown_book.snapshot() == res.cooldown_snapshot
    for eb, ei in zip(res.execution_events, coord_incr._all_events):
        assert eb.to_dict() == ei.to_dict()
    print("batch_incremental_exact_parity=True")

    # Probe 10: Duplicate step call idempotency
    dup_res = coord_incr.step(df.iloc[-1], is_last_bar=True)
    assert dup_res.bar_index == len(df) - 1
    assert len(coord_incr._all_decisions) == len(res.decisions)
    print("step_duplicate_idempotency=True")

    print("=== ALL T53.9.3 INDEPENDENT QC PROBES PASSED (EXIT CODE 0) ===")

if __name__ == "__main__":
    run_probes()
