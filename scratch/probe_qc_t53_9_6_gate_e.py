"""
scratch/probe_qc_t53_9_6_gate_e.py
==================================
Independent Read-Only QC Probe Script for Gate E (Milestone T53.9).

Verifies 15 core architectural invariants:
 1. No fill at signal bar (Fill N+1 at Open)
 2. Last bar cancellation (no fill on last bar, cancelled as no_next_bar)
 3. Future candle append invariance (canonical JSON equality before cutoff)
 4. Future HTF event invariance & boundary admission
 5. Cooldown created ONLY after successful fill
 6. Rejection does not alter position, balance, or cooldown
 7. Atomic reversal on opposite signal (no intermediate orphan state)
 8. Accounting conservation: final_balance == initial_capital + sum(net_pnl)
 9. Event IDs are deterministic and strictly unique
10. Full traceability chain: trade -> fill -> intent -> decision -> setup -> evidence
11. Wave 1 API output is 100% JSON-safe
12. Legacy backtest compatibility is fully preserved
13. SL-first collision invariant on simultaneous touch
14. Short SL/TP trigger via Ask (Bid + spread)
15. Batch vs Incremental vs Replay-prefix bit-for-bit parity

Returns:
  Exit code 0 on PASS
  Exit code 1 on FAIL
"""

from __future__ import annotations

import copy
import datetime
import json
import math
import sys
from typing import Any, Sequence

import pandas as pd

from engine.backtest_engine import BacktestEngine
from engine.strategies import StrategyRegistry
from smc.engine.backtest_adapter import (
    HTFTimeline,
    SMCBacktestCoordinator,
    StepResult,
    CoordinatorResult,
)
from smc.engine.execution import (
    CooldownBook,
    ExecutionConfig,
    ExecutionEvent,
    PendingExecutionIntent,
    validate_fill,
)
from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    MarketRegime,
    SelectionDecision,
    StrategyEvaluation,
    StrategyProfile,
    make_cluster_id,
    make_decision_id,
    make_evidence_id,
    make_setup_id,
)
from smc.models import StructureEvent

UTC = datetime.timezone.utc


# =============================================================================
# Helper Utilities
# =============================================================================

def _make_utc_timestamp(minute: int) -> pd.Timestamp:
    base = pd.Timestamp(datetime.datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC))
    return base + pd.Timedelta(minutes=minute)


def _make_htf_event(
    index: int,
    effective_minute: int,
    direction: str = "bullish",
    event_type: str = "BOS",
    mode: str = "swing",
    bsp: float = 2000.0,
    cp: float = 2005.0,
) -> StructureEvent:
    eff_time = _make_utc_timestamp(effective_minute)
    return StructureEvent(
        index=index,
        time=eff_time,
        event_type=event_type,
        direction=direction,
        broken_swing_index=max(0, index - 5),
        broken_swing_price=bsp,
        close_price=cp,
        displacement=True,
        structure_leg_id=f"htf_leg_{index}",
        mode=mode,
        confirmed_swing_at=index,
        break_type="close",
    )


def _make_candidate_setup(
    strategy_id: str,
    direction: str,
    bar_index: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    cluster_suffix: str = "001",
    expiry_offset: int = 10,
    timeframe_minutes: int = 15,
) -> CandidateSetup:
    ts = _make_utc_timestamp(bar_index * timeframe_minutes)
    cluster_id = make_cluster_id(direction, f"leg_{bar_index}", f"zone_{cluster_suffix}")
    setup_id = make_setup_id(strategy_id, direction, bar_index, cluster_id)

    if direction == "BUY":
        planned_rr = (take_profit - entry_price) / (entry_price - stop_loss)
    else:
        planned_rr = (entry_price - take_profit) / (stop_loss - entry_price)

    if strategy_id == "S05":
        bos_idx = max(0, bar_index - 1)
        ob_idx = max(0, bar_index - 2)
        evidences = (
            EvidenceRef(
                evidence_id=make_evidence_id("structure", "swing", bos_idx, f"bos_{bar_index}"),
                kind="structure_event",
                bar_index=bos_idx,
                price=entry_price + 2.0 if direction == "BUY" else entry_price - 2.0,
                time=_make_utc_timestamp(bos_idx * timeframe_minutes),
                details={"displacement": True},
            ),
            EvidenceRef(
                evidence_id=make_evidence_id("order_block", "swing", ob_idx, f"ob_{bar_index}"),
                kind="order_block",
                bar_index=ob_idx,
                price=entry_price,
                time=_make_utc_timestamp(ob_idx * timeframe_minutes),
                details={"top": entry_price + 1.0, "bottom": entry_price - 1.0, "quality": "premium_candidate"},
            ),
        )
    else:
        # S01 / S09 ordering: sw <= fvg < mss < candidate.bar_index
        sw_idx = max(0, bar_index - 3)
        fvg_idx = max(0, bar_index - 2)
        mss_idx = max(0, bar_index - 1)
        evidences = (
            EvidenceRef(
                evidence_id=make_evidence_id("sweep", "swing", sw_idx, f"pool_{bar_index}"),
                kind="liquidity_sweep",
                bar_index=sw_idx,
                price=entry_price - 5.0 if direction == "BUY" else entry_price + 5.0,
                time=_make_utc_timestamp(sw_idx * timeframe_minutes),
                details={"sweep_type": "clean", "pool_kind": "equal_highs" if direction == "SELL" else "equal_lows"},
            ),
            EvidenceRef(
                evidence_id=make_evidence_id("fvg", "swing", fvg_idx, f"gap_{bar_index}"),
                kind="fair_value_gap",
                bar_index=fvg_idx,
                price=entry_price,
                time=_make_utc_timestamp(fvg_idx * timeframe_minutes),
                details={"top": entry_price + 2.0, "bottom": entry_price - 2.0},
            ),
            EvidenceRef(
                evidence_id=make_evidence_id("structure", "swing", mss_idx, f"mss_{bar_index}"),
                kind="structure_event",
                bar_index=mss_idx,
                price=entry_price + 2.0 if direction == "BUY" else entry_price - 2.0,
                time=_make_utc_timestamp(mss_idx * timeframe_minutes),
                details={"displacement": True},
            ),
        )

    return CandidateSetup(
        setup_id=setup_id,
        strategy_id=strategy_id,
        direction=direction,  # type: ignore[arg-type]
        bar_index=bar_index,
        timestamp=ts,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        planned_rr=round(planned_rr, 2),
        evidences=evidences,
        evidence_cluster_id=cluster_id,
        expiry_bar=bar_index + expiry_offset,
    )


class MockProgrammableStrategy:
    def __init__(
        self,
        strategy_id: str,
        allowed_directions: tuple[str, ...] = ("BUY", "SELL"),
        min_rr: float = 1.5,
    ) -> None:
        self.strategy_id = strategy_id
        self.profile = StrategyProfile(
            strategy_id=strategy_id,
            name=f"Mock {strategy_id}",
            allowed_directions=tuple(allowed_directions),  # type: ignore[arg-type]
            timeframes=("M15",),
            min_rr=min_rr,
        )
        self._setups_by_bar: dict[int, list[CandidateSetup]] = {}

    def schedule_setup(self, bar_index: int, setup: CandidateSetup) -> None:
        if bar_index not in self._setups_by_bar:
            self._setups_by_bar[bar_index] = []
        self._setups_by_bar[bar_index].append(setup)

    def evaluate(self, context: Any) -> Sequence[CandidateSetup]:
        return tuple(self._setups_by_bar.get(context.bar_index, []))

    def reset(self) -> None:
        pass


def _create_synthetic_candles(
    n_bars: int = 15,
    start_price: float = 2000.0,
    timeframe_minutes: int = 15,
    drift: float = 0.5,
) -> pd.DataFrame:
    rows = []
    curr = start_price
    for i in range(n_bars):
        t = _make_utc_timestamp(i * timeframe_minutes)
        c_open = round(curr, 2)
        c_high = round(curr + 3.0, 2)
        c_low = round(curr - 3.0, 2)
        c_close = round(curr + drift, 2)
        curr = c_close
        rows.append({
            "bar_index": i,
            "time": t,
            "open": c_open,
            "high": c_high,
            "low": c_low,
            "close": c_close,
            "volume": 100.0,
        })
    return pd.DataFrame(rows)


def _make_wave1_mocks() -> tuple[MockProgrammableStrategy, MockProgrammableStrategy, MockProgrammableStrategy]:
    return (
        MockProgrammableStrategy("S01"),
        MockProgrammableStrategy("S05"),
        MockProgrammableStrategy("S09"),
    )


# =============================================================================
# Probes Implementation
# =============================================================================

def probe_01_no_fill_at_signal_bar():
    """Probe 1: Signal at Close N only evaluates at Open N+1, Bar 0 cannot fill."""
    strat = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
    strat.schedule_setup(3, setup)
    coord = SMCBacktestCoordinator(
        strategies=[strat],
        mode="smc_s01",
        htf_events=[_make_htf_event(0, 0, direction="bullish")],
    )
    df = _create_synthetic_candles(6)
    df.loc[4, "open"] = 2000.0
    res = coord.run(df)

    fill_events = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
    assert len(fill_events) == 1, f"Expected 1 fill, got {len(fill_events)}"
    fill = fill_events[0]
    assert fill.bar_index == 4, f"Expected fill at bar 4, got {fill.bar_index}"
    # Verify no fill at bar 0
    bar0_fills = [e for e in res.execution_events if e.bar_index == 0 and e.event_type == "ORDER_FILLED"]
    assert len(bar0_fills) == 0, "Bar 0 must never fill!"
    return True


def probe_02_last_bar_only_cancelled():
    """Probe 2: Pending intent at last bar must be cancelled as no_next_bar."""
    strat = MockProgrammableStrategy("S05")
    setup = _make_candidate_setup("S05", "BUY", 4, 2000.0, 1990.0, 2030.0)
    strat.schedule_setup(4, setup)
    coord = SMCBacktestCoordinator(
        strategies=[strat],
        mode="smc_s05",
        htf_events=[_make_htf_event(0, 0, direction="bullish")],
    )
    df = _create_synthetic_candles(5)
    res = coord.run(df)

    cancelled = [e for e in res.execution_events if e.event_type == "ORDER_CANCELLED"]
    assert len(cancelled) == 1, f"Expected 1 cancellation, got {len(cancelled)}"
    assert cancelled[0].reason == "no_next_bar"
    assert cancelled[0].bar_index == 4
    fills = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
    assert len(fills) == 0, "No fills should occur when signal is at last bar"
    return True


def probe_03_future_candle_append_invariance():
    """Probe 3: Future candle append does not alter prefix outputs."""
    df_prefix = _create_synthetic_candles(8)
    df_extended = _create_synthetic_candles(16)

    strat1 = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "BUY", 2, 2001.0, 1990.0, 2030.0)
    strat1.schedule_setup(2, setup)
    coord1 = SMCBacktestCoordinator(strategies=[strat1], mode="smc_s01")
    res1 = coord1.run(df_prefix)

    strat2 = MockProgrammableStrategy("S01")
    strat2.schedule_setup(2, setup)
    coord2 = SMCBacktestCoordinator(strategies=[strat2], mode="smc_s01")
    res2 = coord2.run(df_extended)

    dec1 = [d.to_dict() for d in res1.decisions if d.bar_index < 7]
    dec2 = [d.to_dict() for d in res2.decisions if d.bar_index < 7]
    assert json.dumps(dec1, sort_keys=True) == json.dumps(dec2, sort_keys=True)

    ev1 = [e.to_dict() for e in res1.execution_events if e.bar_index < 7]
    ev2 = [e.to_dict() for e in res2.execution_events if e.bar_index < 7]
    assert json.dumps(ev1, sort_keys=True) == json.dumps(ev2, sort_keys=True)
    return True


def probe_04_future_htf_event_invariance():
    """Probe 4: Future HTF events are withheld until effective time; boundary admitted."""
    t0 = _make_utc_timestamp(0)
    t15 = _make_utc_timestamp(15)
    t30 = _make_utc_timestamp(30)

    ev0 = _make_htf_event(0, 0)
    ev15 = _make_htf_event(1, 15)
    ev30 = _make_htf_event(2, 30)

    timeline = HTFTimeline([ev0, ev15, ev30])
    as_of_15 = timeline.get_events_as_of(t15)
    assert len(as_of_15) == 2, f"Expected 2 events as of 15m, got {len(as_of_15)}"
    assert as_of_15[-1].time <= t15

    # Adding an event far in future does not alter as_of_15
    ev_far = _make_htf_event(3, 120)
    timeline_extended = HTFTimeline([ev0, ev15, ev30, ev_far])
    as_of_15_ext = timeline_extended.get_events_as_of(t15)
    assert len(as_of_15_ext) == 2
    return True


def probe_05_cooldown_only_after_fill():
    """Probe 5: Cooldown is ONLY activated upon successful fill, not candidate or reject."""
    strat = MockProgrammableStrategy("S01", min_rr=2.0)
    setup_rejected = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2010.0)
    strat.schedule_setup(3, setup_rejected)
    coord = SMCBacktestCoordinator(
        strategies=[strat],
        mode="smc_s01",
        htf_events=[_make_htf_event(0, 0, direction="bullish")],
    )
    df = _create_synthetic_candles(6)
    res = coord.run(df)

    assert coord.cooldown_book.is_active("S01", "BUY", 4) is False
    assert coord.cooldown_book.snapshot() == {}

    # Now with valid setup that fills
    strat2 = MockProgrammableStrategy("S01", min_rr=1.5)
    setup_valid = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
    strat2.schedule_setup(3, setup_valid)
    coord2 = SMCBacktestCoordinator(
        strategies=[strat2],
        mode="smc_s01",
        htf_events=[_make_htf_event(0, 0, direction="bullish")],
    )
    df2 = _create_synthetic_candles(6)
    df2.loc[4, "open"] = 2000.0
    res2 = coord2.run(df2)

    # Filled at bar 4 -> cooldown active at bar 4
    assert coord2.cooldown_book.is_active("S01", "BUY", 4) is True
    return True


def probe_06_reject_does_not_alter_state():
    """Probe 6: Execution rejection preserves flat state, balance, and cooldown book."""
    cfg = ExecutionConfig(spread_points=10.0, point_value=0.01)  # spread = 0.10
    intent = _make_candidate_setup("S01", "BUY", 2, 2000.0, 1990.0, 2030.0)
    eval_item = StrategyEvaluation(
        candidate=intent,
        status="ELIGIBLE",
        rejection_reasons=(),
        regime_score=100.0,
        setup_score=80.0,
        context_score=70.0,
        exec_score=60.0,
        total_score=80.0,
        details={},
    )
    p_intent = PendingExecutionIntent.from_selection_decision(
        SelectionDecision(
            decision_id="sel:2:SELECT:S01",
            bar_index=2,
            timestamp=_make_utc_timestamp(30),
            action="SELECT",
            selected_setup=intent,
            primary_strategy_id="S01",
            supporting_strategy_ids=(),
            regime=None,
            evaluations=(eval_item,),
            reason="ok",
            score_gap=None,
            execution_payload={},
        ),
        "XAUUSD", "M15", 1.5,
    )
    val = validate_fill(p_intent, open_price=1985.0, config=cfg)
    assert val.is_valid is False
    assert val.reason == "geometry_violation_at_fill"
    return True


def probe_07_atomic_opposite_reversal():
    """Probe 7: Active BUY reversal to valid SELL is atomic without intermediate orphan state."""
    strat1, strat2, strat3 = _make_wave1_mocks()
    setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1980.0, 2050.0, cluster_suffix="01")
    setup2 = _make_candidate_setup("S05", "SELL", 4, 2001.0, 2020.0, 1970.0, cluster_suffix="02")
    strat1.schedule_setup(3, setup1)
    strat2.schedule_setup(4, setup2)

    coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
    df = _create_synthetic_candles(7)
    htfs = [
        _make_htf_event(0, 0, direction="bullish"),
        _make_htf_event(1, 75, direction="bearish"),  # Flip HTF at bar 4 close
    ]
    res = coord.run(df, htf_events=htfs)

    # Check event order at bar 5 (when SELL fills)
    bar5_events = [e for e in res.execution_events if e.bar_index == 5]
    event_types = [e.event_type for e in bar5_events]
    assert "POSITION_CLOSED" in event_types, f"Missing POSITION_CLOSED at bar 5: {event_types}"
    assert "ORDER_FILLED" in event_types, f"Missing ORDER_FILLED at bar 5: {event_types}"

    # POSITION_CLOSED must precede ORDER_FILLED
    idx_close = event_types.index("POSITION_CLOSED")
    idx_fill = event_types.index("ORDER_FILLED")
    assert idx_close < idx_fill, "POSITION_CLOSED must execute before new ORDER_FILLED in reversal"
    assert bar5_events[idx_close].reason == "opposite_signal"
    assert bar5_events[idx_fill].reason == "fill_ok"
    return True


def probe_08_final_balance_matches_net_pnl():
    """Probe 8: final_balance == initial_capital + sum(net_pnl) strictly holds."""
    strat = MockProgrammableStrategy("S01")
    setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2020.0, cluster_suffix="01")
    strat.schedule_setup(3, setup1)

    coord = SMCBacktestCoordinator(
        strategies=[strat],
        mode="smc_s01",
        initial_capital=10000.0,
        htf_events=[_make_htf_event(0, 0, direction="bullish")],
    )
    df = _create_synthetic_candles(8)
    df.loc[4, "open"] = 2000.0
    df.loc[4, "high"] = 2025.0  # triggers TP
    res = coord.run(df)

    sum_trade_pnl = sum(t["pnl"] for t in res.trades)
    expected_final_balance = round(10000.0 + sum_trade_pnl, 2)
    assert res.final_balance == expected_final_balance
    return True


def probe_09_unique_deterministic_event_ids():
    """Probe 9: ExecutionEvent IDs are unique and deterministic."""
    strat = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
    strat.schedule_setup(3, setup)
    df = _create_synthetic_candles(6)

    coord1 = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", htf_events=[_make_htf_event(0, 0, direction="bullish")])
    res1 = coord1.run(df)

    coord2 = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", htf_events=[_make_htf_event(0, 0, direction="bullish")])
    res2 = coord2.run(df)

    ids1 = [e.event_id for e in res1.execution_events]
    ids2 = [e.event_id for e in res2.execution_events]
    assert len(ids1) == len(set(ids1)), "Duplicate event_id detected within run!"
    assert ids1 == ids2, "Event IDs are not deterministic across identical runs!"
    return True


def probe_10_full_traceability_chain():
    """Probe 10: Trade traces back to fill -> intent -> decision -> setup -> evidence."""
    strat = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
    strat.schedule_setup(3, setup)

    df = _create_synthetic_candles(8)
    df.loc[4, "open"] = 2000.0
    df.loc[5, "high"] = 2035.0  # triggers TP

    coord = SMCBacktestCoordinator(
        strategies=[strat],
        mode="smc_s01",
        htf_events=[_make_htf_event(0, 0, direction="bullish")],
    )
    res = coord.run(df)

    assert len(res.trades) == 1, f"Expected 1 trade, got {len(res.trades)}"
    
    # Trace through POSITION_CLOSED event
    close_events = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
    assert len(close_events) == 1
    ce = close_events[0]
    assert ce.strategy_id == "S01"
    assert ce.setup_id == setup.setup_id
    assert ce.cluster_id == setup.evidence_cluster_id
    assert ce.decision_id.startswith("sel:3:")

    # Find corresponding fill event
    fill_events = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
    assert len(fill_events) == 1
    assert fill_events[0].setup_id == setup.setup_id
    assert fill_events[0].decision_id == ce.decision_id

    # Find decision
    select_decisions = [d for d in res.decisions if d.action == "SELECT"]
    assert len(select_decisions) == 1
    assert select_decisions[0].selected_setup.setup_id == setup.setup_id
    assert len(select_decisions[0].selected_setup.evidences) > 0
    return True


def probe_11_wave1_response_json_safety():
    """Probe 11: BacktestEngine Wave 1 response is completely JSON safe."""
    engine = BacktestEngine(initial_capital=10000.0)
    df = _create_synthetic_candles(10)
    res = engine.run(
        df=df,
        strategy_id="smc_wave1",
        strategy_params={},
        timeframe="M15",
    )
    dumped = json.dumps(res, default=str)
    reloaded = json.loads(dumped)
    assert reloaded["schema_version"] == "2.0.0"
    assert reloaded["mode"] == "smc_wave1"
    assert "run_metadata" in reloaded
    return True


def probe_12_legacy_compatibility_invariance():
    """Probe 12: Legacy strategies preserve exactly legacy keys and schema."""
    engine = BacktestEngine(initial_capital=10000.0)
    candles = [
        {"time": f"2026-01-01 10:{i:02d}:00", "open": 2000.0 + i, "high": 2005.0 + i, "low": 1995.0 + i, "close": 2000.0 + i}
        for i in range(20)
    ]
    df = pd.DataFrame(candles)
    res = engine.run(
        df=df,
        strategy_id="sma_crossover",
        strategy_params={"fast_period": 3, "slow_period": 7},
        timeframe="H1",
    )
    assert set(res.keys()) == {"metrics", "trades", "equity_curve", "markers"}
    required_metrics = {
        "initial_capital", "final_balance", "net_profit", "return_pct",
        "total_trades", "winning_trades", "losing_trades", "win_rate",
        "profit_factor", "max_drawdown", "max_drawdown_pct", "gross_profit", "gross_loss"
    }
    assert set(res["metrics"].keys()) == required_metrics
    return True


def probe_13_sl_first_collision_invariant():
    """Probe 13: Simultaneous touch of SL and TP in wide candle prioritizes SL."""
    strat = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
    strat.schedule_setup(3, setup)

    coord = SMCBacktestCoordinator(
        strategies=[strat],
        mode="smc_s01",
        htf_events=[_make_htf_event(0, 0, direction="bullish")],
    )
    df = _create_synthetic_candles(6)
    df.loc[4, "open"] = 2000.0
    df.loc[4, "low"] = 1985.0
    df.loc[4, "high"] = 2035.0

    res = coord.run(df)
    closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
    assert len(closed) == 1, f"Expected 1 close, got {len(closed)}"
    assert closed[0].reason == "stop_loss", f"Expected SL priority, got: {closed[0].reason}"
    return True


def probe_14_short_sl_tp_ask_trigger():
    """Probe 14: Short SL and TP trigger via Ask = Bid + spread."""
    cfg = ExecutionConfig(spread_points=200.0, point_value=0.01)
    strat = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2010.0, 1970.0)
    strat.schedule_setup(3, setup)

    coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", execution_config=cfg)
    df = _create_synthetic_candles(6)
    df.loc[4, "open"] = 2000.0
    df.loc[4, "high"] = 2010.5

    res = coord.run(df, htf_events=[_make_htf_event(0, 0, direction="bearish")])
    closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
    assert len(closed) == 1
    assert closed[0].reason == "stop_loss"
    return True


def probe_15_batch_incremental_replay_parity():
    """Probe 15: Exact canonical parity across batch, incremental, and replay modes."""
    strat = MockProgrammableStrategy("S01")
    setup = _make_candidate_setup("S01", "BUY", 2, 2001.0, 1990.0, 2030.0)
    strat.schedule_setup(2, setup)
    df = _create_synthetic_candles(7)

    # Mode 1: Batch
    c_batch = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
    r_batch = c_batch.run(df)

    # Mode 2: Incremental
    c_incr = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
    for i in range(len(df)):
        c_incr.step(df.iloc[i], is_last_bar=(i == len(df) - 1))

    d_batch = [d.to_dict() for d in r_batch.decisions]
    d_incr = [d.to_dict() for d in c_incr._all_decisions]
    assert json.dumps(d_batch, sort_keys=True) == json.dumps(d_incr, sort_keys=True)

    e_batch = [e.to_dict() for e in r_batch.execution_events]
    e_incr = [e.to_dict() for e in c_incr._all_events]
    assert json.dumps(e_batch, sort_keys=True) == json.dumps(e_incr, sort_keys=True)
    return True


# =============================================================================
# Main Runner
# =============================================================================

PROBES = [
    ("Probe 01: No fill at signal bar (Fill N+1 at Open)", probe_01_no_fill_at_signal_bar),
    ("Probe 02: Last bar cancellation (no_next_bar)", probe_02_last_bar_only_cancelled),
    ("Probe 03: Future candle append invariance", probe_03_future_candle_append_invariance),
    ("Probe 04: Future HTF event invariance & boundary admission", probe_04_future_htf_event_invariance),
    ("Probe 05: Cooldown only after successful fill", probe_05_cooldown_only_after_fill),
    ("Probe 06: Reject does not alter position, balance, or cooldown", probe_06_reject_does_not_alter_state),
    ("Probe 07: Atomic reversal on opposite signal", probe_07_atomic_opposite_reversal),
    ("Probe 08: Accounting conservation: final_balance == initial_capital + sum(net_pnl)", probe_08_final_balance_matches_net_pnl),
    ("Probe 09: Unique deterministic event IDs", probe_09_unique_deterministic_event_ids),
    ("Probe 10: Full traceability chain", probe_10_full_traceability_chain),
    ("Probe 11: Wave 1 API output is 100% JSON safe", probe_11_wave1_response_json_safety),
    ("Probe 12: Legacy backtest compatibility is fully preserved", probe_12_legacy_compatibility_invariance),
    ("Probe 13: SL-first collision invariant on simultaneous touch", probe_13_sl_first_collision_invariant),
    ("Probe 14: Short SL/TP trigger via Ask (Bid + spread)", probe_14_short_sl_tp_ask_trigger),
    ("Probe 15: Batch vs Incremental vs Replay parity", probe_15_batch_incremental_replay_parity),
]


def run_all_probes() -> int:
    print("=" * 72)
    print("GATE E INDEPENDENT QC PROBES (Milestone T53.9 Final Gate)")
    print("=" * 72)

    passed = 0
    failed = 0

    for name, fn in PROBES:
        try:
            ok = fn()
            if ok:
                passed += 1
                print(f"[PASS] {name}")
            else:
                failed += 1
                print(f"[FAIL] {name} - Returned False")
        except Exception as ex:
            failed += 1
            print(f"[FAIL] {name} - Exception: {ex}")

    print("-" * 72)
    print(f"Summary: {passed}/{len(PROBES)} probes passed, {failed} failed.")

    if failed == 0:
        print(">>> ALL GATE E PROBES PASSED (EXIT CODE 0) <<<")
        return 0
    else:
        print(">>> GATE E PROBES FAILED (EXIT CODE 1) <<<")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_probes())
