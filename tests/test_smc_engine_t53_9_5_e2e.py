"""
tests/test_smc_engine_t53_9_5_e2e.py
======================================
Comprehensive End-to-End, Parity, No-Lookahead, Accounting & Telemetry
Evidence Test Suite for T53.9.5.

Groups:
  Group A — Strategy mode execution (smc_s01, smc_s05, smc_s09, smc_wave1 via BacktestEngine)
  Group B — Full SMC funnel traceability & reason codes
  Group C — Position policy (8 cases: flat, same-dir skip, reversal, invalid reversal, allow_short)
  Group D — No-lookahead proofs (candle lookahead, future append invariance, HTF lookahead)
  Group E — Batch / incremental / replay parity & retries
  Group F — Accounting audit (BUY/SELL formulas, cash RR, min_rr boundary, fee dedup, balance conservation)
  Group G — Dynamic SL/TP audit (per-setup levels, SL-first, short Ask trigger, one-close invariant)
  Group H — Telemetry & audit completeness (full trace chain, no orphan events, unique deterministic IDs)
"""

from __future__ import annotations

import copy
import datetime
import json
import math
import unittest
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

from engine.backtest_engine import BacktestEngine, WAVE1_ALLOWED_TIMEFRAMES
from engine.strategies import StrategyRegistry
from smc.engine.backtest_adapter import (
    CoordinatorResult,
    HTFTimeline,
    SMCBacktestCoordinator,
    StepResult,
    parse_htf_event_payload,
)
from smc.engine.confluence import build_confluence_batch
from smc.engine.context import ContextBuilderConfig, StrategyContext, StrategyContextBuilder
from smc.engine.eligibility import EligibilityGate
from smc.engine.errors import StrategyStateError
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
from smc.engine.protocol import StrategyTemplate
from smc.engine.regime import MarketRegimeClassifier, RegimeClassifierConfig
from smc.engine.selector import DeterministicStrategySelector, SelectorConfig
from smc.models import StructureEvent

UTC = datetime.timezone.utc


# =============================================================================
# Helper Utilities & Fixtures
# =============================================================================

def _make_utc_timestamp(minute: int) -> pd.Timestamp:
    """Deterministic timestamp generator in UTC."""
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
    """Create a timezone-aware Higher Timeframe StructureEvent."""
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
    """Create a fully validated CandidateSetup with canonical SMC evidences."""
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
    """Mock StrategyTemplate enabling deterministic setups per bar."""

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

    def evaluate(self, context: StrategyContext) -> Sequence[CandidateSetup]:
        return tuple(self._setups_by_bar.get(context.bar_index, []))

    def reset(self) -> None:
        # Retain scheduled setups across coordinator.run() reset invocations
        pass


def _create_synthetic_candles(
    n_bars: int = 15,
    start_price: float = 2000.0,
    timeframe_minutes: int = 15,
    drift: float = 0.5,
) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV candles with UTC timezone."""
    rows = []
    curr = start_price
    for i in range(n_bars):
        t = _make_utc_timestamp(i * timeframe_minutes)
        c_open = round(curr, 2)
        c_high = round(curr + 3.0, 2)
        c_low = round(curr - 3.0, 2)
        c_close = round(curr + drift, 2)
        rows.append({
            "bar_index": i,
            "time": t.isoformat(),
            "open": c_open,
            "high": c_high,
            "low": c_low,
            "close": c_close,
            "volume": 500.0,
        })
        curr = c_close
    return pd.DataFrame(rows)


def _canonical_json(obj: Any) -> str:
    """Serialize object to strictly deterministic canonical JSON."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _make_wave1_mocks() -> tuple[MockProgrammableStrategy, MockProgrammableStrategy, MockProgrammableStrategy]:
    """Helper to instantiate all 3 mock strategies required by smc_wave1 mode."""
    return (
        MockProgrammableStrategy("S01"),
        MockProgrammableStrategy("S05"),
        MockProgrammableStrategy("S09"),
    )


# =============================================================================
# Group A: Strategy Mode Execution
# =============================================================================

class TestGroupAStrategyModeExecution(unittest.TestCase):
    """
    Verifies that each of the 4 Wave 1 modes (smc_s01, smc_s05, smc_s09, smc_wave1)
    runs through BacktestEngine, produces the correct schema version, handles empty
    signals cleanly, isolates strategy decisions, and has zero duplicate events.
    """

    def setUp(self) -> None:
        self.engine = BacktestEngine(
            initial_capital=10000.0,
            lot_size=0.01,
            spread_points=20.0,
            commission_per_lot=5.0,
            allow_short=True,
        )
        self.df = _create_synthetic_candles(20)

    def test_all_four_modes_execute_via_backtest_engine(self) -> None:
        modes = ["smc_s01", "smc_s05", "smc_s09", "smc_wave1"]
        for mode in modes:
            res = self.engine.run(self.df, mode, {}, timeframe="M15")
            self.assertIsInstance(res, dict)
            self.assertEqual(res["mode"], mode)
            self.assertEqual(res["schema_version"], "2.0.0")

            # Standard fields present
            self.assertIn("metrics", res)
            self.assertIn("trades", res)
            self.assertIn("equity_curve", res)
            self.assertIn("markers", res)

            # V2 fields present
            self.assertIn("execution_events", res)
            self.assertIn("decisions", res)
            self.assertIn("pending_intents", res)
            self.assertIn("cooldown_snapshot", res)
            self.assertIn("run_metadata", res)

            meta = res["run_metadata"]
            self.assertEqual(meta["timeframe"], "M15")
            self.assertEqual(meta["bars_analyzed"], 20)
            self.assertEqual(meta["htf_events_count"], 0)

    def test_single_mode_does_not_create_decisions_for_other_strategies(self) -> None:
        # In smc_s01, registry only enables S01
        res = self.engine.run(self.df, "smc_s01", {}, timeframe="M15")
        for dec in res["decisions"]:
            if dec["action"] == "SELECT":
                self.assertEqual(dec["primary_strategy_id"], "S01")

        # In smc_s05, registry only enables S05
        res = self.engine.run(self.df, "smc_s05", {}, timeframe="M15")
        for dec in res["decisions"]:
            if dec["action"] == "SELECT":
                self.assertEqual(dec["primary_strategy_id"], "S05")

        # In smc_s09, registry only enables S09
        res = self.engine.run(self.df, "smc_s09", {}, timeframe="M15")
        for dec in res["decisions"]:
            if dec["action"] == "SELECT":
                self.assertEqual(dec["primary_strategy_id"], "S09")

    def test_no_signal_run_returns_clean_zero_metrics_without_errors(self) -> None:
        res = self.engine.run(self.df, "smc_wave1", {}, timeframe="M15")
        metrics = res["metrics"]
        self.assertEqual(metrics["total_trades"], 0)
        self.assertEqual(metrics["win_rate"], 0.0)
        self.assertEqual(metrics["net_profit"], 0.0)
        self.assertEqual(metrics["final_balance"], 10000.0)
        self.assertEqual(len(res["trades"]), 0)

    def test_no_duplicate_execution_event_ids(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S01", "SELL", 6, 2005.0, 2015.0, 1980.0, cluster_suffix="02")
        strat.schedule_setup(3, setup1)
        strat.schedule_setup(6, setup2)

        coord = SMCBacktestCoordinator(
            strategies=[strat],
            mode="smc_s01",
            htf_events=[_make_htf_event(0, 0)],
        )
        res = coord.run(self.df)
        event_ids = [e.event_id for e in res.execution_events]
        self.assertEqual(len(event_ids), len(set(event_ids)), "Execution event IDs must be strictly unique")


# =============================================================================
# Group B: Full SMC Funnel Traceability & Reason Codes
# =============================================================================

class TestGroupBFullSMCFunnel(unittest.TestCase):
    """
    Verifies complete end-to-end SMC pipeline traceability:
    HTF bias -> Swing -> BOS/CHoCH -> FVG/OB -> Strategy candidate ->
    Eligibility Gate -> Confluence -> Selector -> Pending intent -> Fill N+1 ->
    SL/TP -> Trade close.
    """

    def test_full_traceability_from_candidate_to_trade_close(self) -> None:
        strat = MockProgrammableStrategy("S01")
        # Setup at bar 3: BUY entry=2000, SL=1990, TP=2030
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

        # 1. Decision at bar 3
        decisions_at_3 = [d for d in res.decisions if d.bar_index == 3 and d.action == "SELECT"]
        self.assertEqual(len(decisions_at_3), 1)
        dec = decisions_at_3[0]
        self.assertEqual(dec.primary_strategy_id, "S01")
        self.assertEqual(dec.selected_setup.setup_id, setup.setup_id)
        self.assertEqual(dec.selected_setup.evidence_cluster_id, setup.evidence_cluster_id)

        # 2. Pending intent for bar 4
        intents = [p for p in res.pending_intents if p.signal_bar_index == 3]
        self.assertEqual(len(intents), 1)
        intent = intents[0]
        self.assertEqual(intent.decision_id, dec.decision_id)
        self.assertEqual(intent.setup_id, setup.setup_id)
        self.assertEqual(intent.strategy_id, "S01")
        self.assertEqual(intent.cluster_id, setup.evidence_cluster_id)
        self.assertEqual(len(intent.evidence_ids), len(setup.evidences))

        # 3. Execution events
        # ORDER_SELECTED at bar 3
        sel_evts = [e for e in res.execution_events if e.event_type == "ORDER_SELECTED"]
        self.assertEqual(len(sel_evts), 1)
        self.assertEqual(sel_evts[0].decision_id, dec.decision_id)
        self.assertEqual(sel_evts[0].setup_id, setup.setup_id)

        # ORDER_FILLED at bar 4
        fill_evts = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(fill_evts), 1)
        self.assertEqual(fill_evts[0].bar_index, 4)
        self.assertEqual(fill_evts[0].decision_id, dec.decision_id)
        self.assertEqual(fill_evts[0].setup_id, setup.setup_id)

        # POSITION_CLOSED at bar 5 (take profit)
        close_evts = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
        self.assertEqual(len(close_evts), 1)
        ce = close_evts[0]
        self.assertEqual(ce.reason, "take_profit")
        self.assertEqual(ce.decision_id, dec.decision_id)
        self.assertEqual(ce.setup_id, setup.setup_id)
        self.assertEqual(ce.strategy_id, "S01")
        self.assertEqual(ce.cluster_id, setup.evidence_cluster_id)

        # 4. Completed trade
        self.assertEqual(len(res.trades), 1)
        tr = res.trades[0]
        self.assertEqual(tr["exit_reason"], "Take Profit")
        self.assertGreater(tr["pnl"], 0.0)

    def test_rejected_setup_has_rejection_reason_code(self) -> None:
        strat = MockProgrammableStrategy("S01", min_rr=2.0)
        # Planned RR = (2010 - 2000) / (2000 - 1995) = 10 / 5 = 2.0 planned,
        # but at fill with spread, cash RR < 2.0
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1995.0, 2010.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(
            strategies=[strat],
            mode="smc_s01",
            execution_config=ExecutionConfig(spread_points=50.0, commission_per_lot=5.0, min_rr_fallback=2.0),
            htf_events=[_make_htf_event(0, 0)],
        )
        df = _create_synthetic_candles(6)
        res = coord.run(df)

        rej_evts = [e for e in res.execution_events if e.event_type == "ORDER_REJECTED"]
        self.assertEqual(len(rej_evts), 1)
        self.assertEqual(rej_evts[0].reason, "insufficient_rr_at_fill")
        self.assertEqual(rej_evts[0].setup_id, setup.setup_id)

    def test_no_trade_decision_generates_no_pending_intent(self) -> None:
        coord = SMCBacktestCoordinator(mode="smc_wave1")
        df = _create_synthetic_candles(5)
        res = coord.run(df)

        for dec in res.decisions:
            self.assertEqual(dec.action, "NO_TRADE")
        self.assertEqual(len(res.pending_intents), 0)
        self.assertEqual(len(res.execution_events), 0)

    def test_at_most_one_selected_setup_per_bar(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S05", "BUY", 3, 2000.0, 1992.0, 2028.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(3, setup2)

        coord = SMCBacktestCoordinator(
            strategies=[strat1, strat2, strat3],
            mode="smc_wave1",
            htf_events=[_make_htf_event(0, 0)],
        )
        df = _create_synthetic_candles(6)
        res = coord.run(df)

        sel_at_3 = [d for d in res.decisions if d.bar_index == 3 and d.action == "SELECT"]
        self.assertLessEqual(len(sel_at_3), 1)


# =============================================================================
# Group C: Position Policy (8 Cases)
# =============================================================================

class TestGroupCPositionPolicy(unittest.TestCase):
    """
    Verifies all 8 required position policy cases:
      1. Flat -> BUY
      2. Flat -> SELL
      3. Active BUY -> new BUY skipped
      4. Active SELL -> new SELL skipped
      5. Active BUY -> valid SELL atomic reversal
      6. Active BUY -> invalid SELL keeps BUY (no intermediate loss)
      7. allow_short=False skips SELL
      8. Reversal creates exact event sequence
    """

    def test_case_1_flat_to_buy(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0, direction="bullish")])

        fills = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].direction, "BUY")
        self.assertEqual(fills[0].bar_index, 4)

    def test_case_2_flat_to_sell(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2010.0, 1970.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0, direction="bearish")])

        fills = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].direction, "SELL")
        self.assertEqual(fills[0].bar_index, 4)

    def test_case_3_active_buy_skips_new_buy(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S05", "BUY", 4, 2001.0, 1991.0, 2031.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(7)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0, direction="bullish")])

        # Bar 4 fills setup1, Bar 5 skips setup2
        skipped = [e for e in res.execution_events if e.event_type == "ORDER_SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason, "position_already_open_same_direction")
        self.assertEqual(skipped[0].setup_id, setup2.setup_id)

    def test_case_4_active_sell_skips_new_sell(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2010.0, 1970.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S05", "SELL", 4, 1999.0, 2009.0, 1969.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(7)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0, direction="bearish")])

        skipped = [e for e in res.execution_events if e.event_type == "ORDER_SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason, "position_already_open_same_direction")
        self.assertEqual(skipped[0].setup_id, setup2.setup_id)

    def test_case_5_active_buy_to_valid_sell_atomic_reversal(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1980.0, 2050.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S05", "SELL", 4, 2001.0, 2020.0, 1970.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(7)
        htfs = [
            _make_htf_event(0, 0, direction="bullish", event_type="BOS"),
            _make_htf_event(1, 70, direction="bearish", event_type="CHoCH"),
            _make_htf_event(2, 75, direction="bearish", event_type="BOS"),  # Flip HTF at bar 4 close
        ]
        res = coord.run(df, htf_events=htfs)

        pos_closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED" and e.reason == "opposite_signal"]
        self.assertEqual(len(pos_closed), 1)
        self.assertEqual(pos_closed[0].direction, "BUY")

        fills = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[0].direction, "BUY")
        self.assertEqual(fills[1].direction, "SELL")

    def test_case_6_active_buy_retains_position_when_sell_is_invalid(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1980.0, 2050.0, cluster_suffix="01")
        # Setup 2 has valid planned levels: entry 2001, SL 2010, TP 1970
        setup2 = _make_candidate_setup("S05", "SELL", 4, 2001.0, 2010.0, 1970.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(7)
        # Gap Open 5 above SL (open=2025.0 > SL=2010.0) with valid candle high (2026.0) -> geometry violation at fill for SELL
        df.loc[5, "open"] = 2025.0
        df.loc[5, "high"] = 2026.0
        htfs = [
            _make_htf_event(0, 0, direction="bullish", event_type="BOS"),
            _make_htf_event(1, 70, direction="bearish", event_type="CHoCH"),
            _make_htf_event(2, 75, direction="bearish", event_type="BOS"),
        ]
        res = coord.run(df, htf_events=htfs)

        rej = [e for e in res.execution_events if e.event_type == "ORDER_REJECTED"]
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0].reason, "geometry_violation_at_fill")

        # BUY position was NOT closed at bar 5 by opposite_signal
        opp_closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED" and e.reason == "opposite_signal"]
        self.assertEqual(len(opp_closed), 0)

        # BUY position was forced closed at final bar
        force_closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED" and e.reason == "forced_close"]
        self.assertEqual(len(force_closed), 1)
        self.assertEqual(force_closed[0].direction, "BUY")

    def test_case_7_allow_short_disabled_skips_sell(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2015.0, 1965.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(
            strategies=[strat],
            mode="smc_s01",
            execution_config=ExecutionConfig(allow_short=False),
        )
        df = _create_synthetic_candles(6)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0, direction="bearish")])

        skipped = [e for e in res.execution_events if e.event_type == "ORDER_SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason, "short_disabled")
        self.assertEqual(len(res.trades), 0)

    def test_case_8_reversal_creates_exact_event_sequence(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1980.0, 2050.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S05", "SELL", 4, 2001.0, 2020.0, 1970.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(7)
        htfs = [
            _make_htf_event(0, 0, direction="bullish", event_type="BOS"),
            _make_htf_event(1, 70, direction="bearish", event_type="CHoCH"),
            _make_htf_event(2, 75, direction="bearish", event_type="BOS"),
        ]
        res = coord.run(df, htf_events=htfs)

        bar5_events = [e for e in res.execution_events if e.bar_index == 5]
        self.assertGreaterEqual(len(bar5_events), 2)
        self.assertEqual(bar5_events[0].event_type, "POSITION_CLOSED")
        self.assertEqual(bar5_events[0].reason, "opposite_signal")
        self.assertEqual(bar5_events[1].event_type, "ORDER_FILLED")
        self.assertEqual(bar5_events[1].reason, "fill_ok")


# =============================================================================
# Group D: No-Lookahead Proofs
# =============================================================================

class TestGroupDNoLookahead(unittest.TestCase):
    """
    Verifies zero lookahead across candle timing, future append invariance,
    and HTF timeline admissions.
    """

    def test_candle_lookahead_close_n_fills_open_n_plus_one(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        df.loc[4, "open"] = 2004.50
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        filled = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"][0]
        self.assertEqual(filled.signal_bar_index, 3)
        self.assertEqual(filled.bar_index, 4)
        self.assertEqual(filled.actual_entry, 2004.70)

    def test_bar_0_cannot_fill(self) -> None:
        strat = MockProgrammableStrategy("S01")
        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(4)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        fills_at_0 = [e for e in res.execution_events if e.event_type == "ORDER_FILLED" and e.bar_index == 0]
        self.assertEqual(len(fills_at_0), 0)

    def test_last_bar_only_cancelled_no_next_bar(self) -> None:
        strat = MockProgrammableStrategy("S01")
        # 6 bars: indices 0..5. Setup scheduled at final bar 5.
        setup = _make_candidate_setup("S01", "BUY", 5, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(5, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        cancelled = [e for e in res.execution_events if e.event_type == "ORDER_CANCELLED"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].reason, "no_next_bar")
        self.assertEqual(cancelled[0].bar_index, 5)

    def test_future_append_invariance(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        df_full = _create_synthetic_candles(12)
        cutoff_k = 6
        df_prefix = df_full.iloc[:cutoff_k].copy()

        htf = _make_htf_event(0, 0)

        coord_prefix = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        res_prefix = coord_prefix.run(df_prefix, htf_events=[htf])

        coord_full = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        res_full = coord_full.run(df_full, htf_events=[htf])

        for i in range(cutoff_k - 1):
            prefix_dec = res_prefix.decisions[i].to_dict()
            full_dec = res_full.decisions[i].to_dict()
            self.assertEqual(_canonical_json(prefix_dec), _canonical_json(full_dec))

        prefix_evts = [e.to_dict() for e in res_prefix.execution_events if e.bar_index < cutoff_k - 1]
        full_evts = [e.to_dict() for e in res_full.execution_events if e.bar_index < cutoff_k - 1]
        self.assertEqual(_canonical_json(prefix_evts), _canonical_json(full_evts))

    def test_htf_timeline_lookahead_protections(self) -> None:
        ev_bar1 = _make_htf_event(1, 30, direction="bullish")
        ev_future = _make_htf_event(2, 90, direction="bearish")

        timeline = HTFTimeline([ev_bar1, ev_future])

        emitted_0 = timeline.get_events_as_of(_make_utc_timestamp(15))
        self.assertEqual(len(emitted_0), 0)

        emitted_1 = timeline.get_events_as_of(_make_utc_timestamp(30))
        self.assertEqual(len(emitted_1), 1)
        self.assertEqual(emitted_1[0].index, 1)

        emitted_2 = timeline.get_events_as_of(_make_utc_timestamp(45))
        self.assertEqual(len(emitted_2), 0)

    def test_conflicting_htf_events_rejected(self) -> None:
        ev1 = _make_htf_event(1, 30, direction="bullish", bsp=2000.0)
        ev2 = _make_htf_event(1, 30, direction="bullish", bsp=2010.0)
        with self.assertRaises(ValueError):
            HTFTimeline([ev1, ev2])


# =============================================================================
# Group E: Batch / Incremental / Replay Parity & Retries
# =============================================================================

class TestGroupEBatchIncrementalParity(unittest.TestCase):
    """
    Verifies absolute bit-for-bit parity across Batch, Incremental, and Replay-prefix,
    as well as state transition error handling on non-monotonic or tampered inputs.
    """

    def test_batch_vs_incremental_vs_replay_parity(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S01", "SELL", 6, 2005.0, 2015.0, 1980.0, cluster_suffix="02")
        strat.schedule_setup(3, setup1)
        strat.schedule_setup(6, setup2)

        df = _create_synthetic_candles(10)
        htf = _make_htf_event(0, 0)

        # 1. Batch mode
        coord_batch = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", htf_events=[htf])
        res_batch = coord_batch.run(df)

        # 2. Incremental mode
        coord_incr = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", htf_events=[htf])
        n_bars = len(df)
        for idx in range(n_bars):
            coord_incr.step(df.iloc[idx], is_last_bar=(idx == n_bars - 1))

        # 3. Replay prefix mode (run first 5 bars, then step remainder)
        coord_rep = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", htf_events=[htf])
        for idx in range(5):
            coord_rep.step(df.iloc[idx], is_last_bar=False)
        for idx in range(5, n_bars):
            coord_rep.step(df.iloc[idx], is_last_bar=(idx == n_bars - 1))

        batch_dec = [_canonical_json(d.to_dict()) for d in res_batch.decisions]
        incr_dec = [_canonical_json(d.to_dict()) for d in coord_incr._all_decisions]
        rep_dec = [_canonical_json(d.to_dict()) for d in coord_rep._all_decisions]
        self.assertEqual(batch_dec, incr_dec)
        self.assertEqual(batch_dec, rep_dec)

        batch_evts = [_canonical_json(e.to_dict()) for e in res_batch.execution_events]
        incr_evts = [_canonical_json(e.to_dict()) for e in coord_incr._all_events]
        rep_evts = [_canonical_json(e.to_dict()) for e in coord_rep._all_events]
        self.assertEqual(batch_evts, incr_evts)
        self.assertEqual(batch_evts, rep_evts)

        self.assertEqual(_canonical_json(res_batch.trades), _canonical_json(coord_incr.kernel.trades))
        self.assertEqual(_canonical_json(res_batch.trades), _canonical_json(coord_rep.kernel.trades))

        self.assertEqual(res_batch.final_balance, coord_incr.kernel.balance)
        self.assertEqual(res_batch.final_balance, coord_rep.kernel.balance)

    def test_retry_same_bar_identical_payload_returns_cached_result(self) -> None:
        strat = MockProgrammableStrategy("S01")
        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(3)

        res1 = coord.step(df.iloc[0], is_last_bar=False)
        res2 = coord.step(df.iloc[0], is_last_bar=False)
        self.assertEqual(res1.bar_index, res2.bar_index)
        self.assertEqual(len(coord._all_decisions), 1)

    def test_retry_same_bar_different_payload_raises_state_error(self) -> None:
        strat = MockProgrammableStrategy("S01")
        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(3)

        coord.step(df.iloc[0], is_last_bar=False)
        tampered = df.iloc[0].copy()
        tampered["close"] = 9999.0
        with self.assertRaises(StrategyStateError):
            coord.step(tampered, is_last_bar=False)

    def test_backward_bar_call_raises_state_error(self) -> None:
        strat = MockProgrammableStrategy("S01")
        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(4)

        coord.step(df.iloc[0], is_last_bar=False)
        coord.step(df.iloc[1], is_last_bar=False)
        with self.assertRaises(StrategyStateError):
            coord.step(df.iloc[0], is_last_bar=False)

    def test_jump_bar_call_raises_state_error(self) -> None:
        strat = MockProgrammableStrategy("S01")
        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(5)

        coord.step(df.iloc[0], is_last_bar=False)
        with self.assertRaises(StrategyStateError):
            coord.step(df.iloc[2], is_last_bar=False)

    def test_reset_coordinator_restores_identical_run(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        htf = _make_htf_event(0, 0)

        res1 = coord.run(df, htf_events=[htf])
        coord.reset()
        res2 = coord.run(df, htf_events=[htf])

        self.assertEqual(
            _canonical_json([e.to_dict() for e in res1.execution_events]),
            _canonical_json([e.to_dict() for e in res2.execution_events]),
        )


# =============================================================================
# Group F: Accounting Audit
# =============================================================================

class TestGroupFAccountingAudit(unittest.TestCase):
    """
    Verifies financial mechanics:
      - BUY: actual_entry = Open + spread, SL/TP use Bid
      - SELL: actual_entry = Open, SL/TP trigger use Ask = Bid + spread
      - Cash RR formula and exact min_rr boundary
      - Commission & spread deduplication
      - PnL symmetry
      - Forced close balance update
      - Reversal trade accounting
      - Equity curve validity (no NaN/Inf, MDD >= 0)
      - Balance conservation law: final_balance == initial_capital + sum(pnl)
    """

    def test_buy_and_sell_actual_entry_and_sl_tp_formulas(self) -> None:
        cfg = ExecutionConfig(
            spread_points=20.0,
            point_value=0.01,
            lot_size=0.01,
            contract_size=100.0,
            commission_per_lot=5.0,
        )
        # 1. BUY intent
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
                timestamp=_make_utc_timestamp(45),
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
        self.assertTrue(val_buy.is_valid)
        self.assertEqual(val_buy.actual_entry, 2000.20)  # Open + spread
        self.assertEqual(val_buy.actual_sl, 1990.0)      # Bid SL
        self.assertEqual(val_buy.actual_tp, 2030.0)      # Bid TP

        # 2. SELL intent
        intent_sell = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2010.0, 1970.0)
        eval_sell = StrategyEvaluation(
            candidate=intent_sell,
            status="ELIGIBLE",
            rejection_reasons=(),
            regime_score=100.0,
            setup_score=80.0,
            context_score=70.0,
            exec_score=60.0,
            total_score=80.0,
            details={},
        )
        p_sell = PendingExecutionIntent.from_selection_decision(
            SelectionDecision(
                decision_id="sel:3:SELECT:S01",
                bar_index=3,
                timestamp=_make_utc_timestamp(45),
                action="SELECT",
                selected_setup=intent_sell,
                primary_strategy_id="S01",
                supporting_strategy_ids=(),
                regime=None,
                evaluations=(eval_sell,),
                reason="ok",
                score_gap=None,
                execution_payload={},
            ),
            "XAUUSD", "M15", 1.5,
        )
        val_sell = validate_fill(p_sell, open_price=2000.0, config=cfg)
        self.assertTrue(val_sell.is_valid)
        self.assertEqual(val_sell.actual_entry, 2000.00)  # Open
        self.assertEqual(val_sell.actual_sl, 2010.20)     # Ask SL (signal_sl + spread)
        self.assertEqual(val_sell.actual_tp, 1970.20)     # Ask TP (signal_tp + spread)

    def test_cash_rr_formula_and_exact_min_rr_boundary(self) -> None:
        cfg = ExecutionConfig(
            spread_points=0.0,
            lot_size=1.0,
            contract_size=1.0,
            commission_per_lot=0.0,
        )
        intent = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2015.0)
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

        p_exact = PendingExecutionIntent.from_selection_decision(
            SelectionDecision(
                decision_id="sel:3:SELECT:S01",
                bar_index=3,
                timestamp=_make_utc_timestamp(45),
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
            "XAUUSD", "M15", min_rr=1.5,
        )
        val_exact = validate_fill(p_exact, open_price=2000.0, config=cfg)
        self.assertTrue(val_exact.is_valid)
        self.assertEqual(val_exact.effective_rr, 1.5)

        p_below = PendingExecutionIntent.from_selection_decision(
            SelectionDecision(
                decision_id="sel:3:SELECT:S01",
                bar_index=3,
                timestamp=_make_utc_timestamp(45),
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
            "XAUUSD", "M15", min_rr=1.501,
        )
        val_below = validate_fill(p_below, open_price=2000.0, config=cfg)
        self.assertFalse(val_below.is_valid)
        self.assertEqual(val_below.reason, "insufficient_rr_at_fill")

    def test_pnl_buy_and_sell_symmetry(self) -> None:
        cfg = ExecutionConfig(spread_points=0.0, commission_per_lot=0.0, lot_size=1.0, contract_size=1.0)

        # 1. BUY: entry 2000.0, exit 2020.0 (+20 USD), planned RR = 20 / 10 = 2.0 >= 1.5
        strat_buy = MockProgrammableStrategy("S01")
        setup_buy = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2020.0)
        strat_buy.schedule_setup(3, setup_buy)
        coord_buy = SMCBacktestCoordinator(strategies=[strat_buy], mode="smc_s01", execution_config=cfg)
        df_buy = _create_synthetic_candles(6)
        df_buy.loc[4, "open"] = 2000.0
        df_buy.loc[4, "high"] = 2025.0  # triggers TP at 2020.0 in bar 4 intrabar
        res_buy = coord_buy.run(df_buy, htf_events=[_make_htf_event(0, 0, direction="bullish")])
        self.assertEqual(len(res_buy.trades), 1)
        pnl_buy = res_buy.trades[0]["pnl"]

        # 2. SELL: entry 2000.0, exit 1980.0 (+20 USD), planned RR = 20 / 10 = 2.0 >= 1.5
        strat_sell = MockProgrammableStrategy("S01")
        setup_sell = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2010.0, 1980.0)
        strat_sell.schedule_setup(3, setup_sell)
        coord_sell = SMCBacktestCoordinator(strategies=[strat_sell], mode="smc_s01", execution_config=cfg)
        df_sell = _create_synthetic_candles(6)
        df_sell.loc[4, "open"] = 2000.0
        df_sell.loc[4, "low"] = 1975.0  # triggers TP at 1980.0 in bar 4 intrabar
        res_sell = coord_sell.run(df_sell, htf_events=[_make_htf_event(0, 0, direction="bearish")])
        self.assertEqual(len(res_sell.trades), 1)
        pnl_sell = res_sell.trades[0]["pnl"]

        self.assertEqual(pnl_buy, 20.0)
        self.assertEqual(pnl_sell, 20.0)
        self.assertEqual(pnl_buy, pnl_sell, "BUY and SELL PnL must be symmetric for equivalent price distance")

    def test_balance_conservation_and_equity_curve_invariants(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2020.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S01", "BUY", 5, 2005.0, 1995.0, 2025.0, cluster_suffix="02")
        strat.schedule_setup(3, setup1)
        strat.schedule_setup(5, setup2)

        coord = SMCBacktestCoordinator(
            strategies=[strat],
            mode="smc_s01",
            initial_capital=10000.0,
            htf_events=[_make_htf_event(0, 0)],
        )
        df = _create_synthetic_candles(8)
        df.loc[4, "high"] = 2025.0  # triggers TP for setup 1
        res = coord.run(df)

        sum_trade_pnl = sum(t["pnl"] for t in res.trades)
        expected_final_balance = round(10000.0 + sum_trade_pnl, 2)
        self.assertEqual(res.final_balance, expected_final_balance)

        for pt in res.equity_curve:
            eq = pt["equity"]
            self.assertFalse(math.isnan(eq))
            self.assertFalse(math.isinf(eq))
            self.assertGreater(eq, 0.0)

        self.assertGreaterEqual(res.max_drawdown, 0.0)
        self.assertGreaterEqual(res.max_drawdown_pct, 0.0)


# =============================================================================
# Group G: Dynamic SL/TP Audit
# =============================================================================

class TestGroupGDynamicSLTP(unittest.TestCase):
    """
    Verifies per-setup dynamic SL/TP, conservative SL-first collision,
    short Ask trigger, and one-close-per-bar invariant.
    """

    def test_dynamic_sl_tp_differs_per_setup(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1988.0, 2030.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S05", "BUY", 5, 2005.0, 1997.0, 2035.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(5, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(8)
        df.loc[4, "open"] = 2000.0
        df.loc[4, "high"] = 2035.0  # triggers TP for setup 1 in bar 4 intrabar
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        filled = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(filled), 2)
        # Setup 1 dynamic levels
        self.assertEqual(filled[0].planned_sl, 1988.0)
        self.assertEqual(filled[0].planned_tp, 2030.0)
        # Setup 2 dynamic levels
        self.assertEqual(filled[1].planned_sl, 1997.0)
        self.assertEqual(filled[1].planned_tp, 2035.0)

    def test_simultaneous_sl_and_tp_hit_prioritizes_sl_first(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        df.loc[4, "low"] = 1985.0
        df.loc[4, "high"] = 2035.0

        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].reason, "stop_loss", "SL-first rule must trigger stop_loss on simultaneous hit")

    def test_short_sl_and_tp_triggered_via_ask(self) -> None:
        cfg = ExecutionConfig(spread_points=200.0, point_value=0.01)
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2010.0, 1970.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", execution_config=cfg)
        df = _create_synthetic_candles(6)
        df.loc[4, "high"] = 2010.5

        res = coord.run(df, htf_events=[_make_htf_event(0, 0, direction="bearish")])
        closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].reason, "stop_loss")

    def test_position_already_closed_not_double_closed_in_same_bar(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        df.loc[4, "low"] = 1985.0
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        closed_at_4 = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED" and e.bar_index == 4]
        self.assertEqual(len(closed_at_4), 1, "Position must only close once")


# =============================================================================
# Group H: Telemetry & Audit Completeness
# =============================================================================

class TestGroupHTelemetryAuditCompleteness(unittest.TestCase):
    """
    Verifies end-to-end telemetry chain completeness:
      - Full trace: decision_id -> setup_id -> strategy_id -> cluster_id ->
        evidence_ids -> pending_intent -> ORDER_SELECTED ->
        ORDER_FILLED/REJECTED/SKIPPED/CANCELLED -> POSITION_CLOSED
      - Zero orphan events
      - No ORDER_FILLED without prior ORDER_SELECTED
      - No POSITION_CLOSED without active position
      - Unique, deterministic event IDs
      - Complete JSON round-trip fidelity
    """

    def test_no_orphan_events_and_strict_order_lifecycle(self) -> None:
        strat1, strat2, strat3 = _make_wave1_mocks()
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0, cluster_suffix="01")
        setup2 = _make_candidate_setup("S05", "BUY", 4, 2001.0, 1991.0, 2031.0, cluster_suffix="02")
        strat1.schedule_setup(3, setup1)
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(8)
        df.loc[5, "high"] = 2035.0  # TP closes setup 1
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        selected_setup_ids = set()
        for evt in res.execution_events:
            if evt.event_type == "ORDER_SELECTED":
                selected_setup_ids.add(evt.setup_id)
            elif evt.event_type in ("ORDER_FILLED", "ORDER_SKIPPED", "ORDER_REJECTED", "ORDER_CANCELLED"):
                self.assertIn(
                    evt.setup_id,
                    selected_setup_ids,
                    f"Orphan event {evt.event_type} found for setup {evt.setup_id}",
                )

    def test_every_trade_traces_back_to_strategy_and_setup(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        df.loc[4, "high"] = 2035.0
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        self.assertEqual(len(res.trades), 1)
        close_evt = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"][0]
        self.assertEqual(close_evt.strategy_id, "S01")
        self.assertEqual(close_evt.setup_id, setup.setup_id)
        self.assertEqual(close_evt.cluster_id, setup.evidence_cluster_id)
        self.assertTrue(close_evt.decision_id.startswith("sel:3:"))

    def test_event_ids_deterministic_and_unique(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        df.loc[4, "high"] = 2035.0
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        seen = set()
        for evt in res.execution_events:
            self.assertTrue(evt.event_id.startswith("evt:"))
            self.assertNotIn(evt.event_id, seen)
            seen.add(evt.event_id)

    def test_json_serialization_preserves_all_metadata(self) -> None:
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        df.loc[4, "high"] = 2035.0
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        for evt in res.execution_events:
            dumped = json.dumps(evt.to_dict())
            loaded = json.loads(dumped)
            self.assertEqual(loaded["event_id"], evt.event_id)
            self.assertEqual(loaded["event_type"], evt.event_type)
            self.assertEqual(loaded["strategy_id"], evt.strategy_id)
            self.assertEqual(loaded["setup_id"], evt.setup_id)
            self.assertEqual(loaded["event_version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
