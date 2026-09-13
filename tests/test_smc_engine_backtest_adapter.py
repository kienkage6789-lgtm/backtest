"""
tests/test_smc_engine_backtest_adapter.py
=========================================
Unit and integration tests for T53.9.3:
Bar-by-bar SMC Coordinator, Cooldown-after-fill, and HTF as-of Timeline.

Covers all 7 test categories specified in T53.9.3 contract:
1. TestGroupA: Cooldown Book & Eligibility Gate Integration
2. TestGroupB: Fill & No-Lookahead Semantics
3. TestGroupC: Position Policy & Intrabar Lifecycle
4. TestGroupD: RR, Geometry & Spread Transformation
5. TestGroupE: Execution Events & Audit Trail Traceability
6. TestGroupF: Mode Support (smc_wave1, smc_s01, smc_s05, smc_s09)
7. TestGroupG: Batch vs Incremental vs Replay-Prefix Parity & Idempotency
"""

from __future__ import annotations

import copy
import datetime
import json
import math
import unittest
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

from engine.execution_kernel import ExecutionKernel
from smc.engine.backtest_adapter import (
    CoordinatorResult,
    HTFTimeline,
    SMCBacktestAdapter,
    SMCBacktestCoordinator,
    StepResult,
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
from smc.engine.registry import StrategyRegistry, StrategyRegistryConfig
from smc.engine.selector import DeterministicStrategySelector, SelectorConfig
from smc.models import StructureEvent

UTC = datetime.timezone.utc


# =============================================================================
# Helper Fixtures & Mock Strategy Templates
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
) -> StructureEvent:
    """Create a timezone-aware Higher Timeframe StructureEvent."""
    eff_time = _make_utc_timestamp(effective_minute)
    return StructureEvent(
        index=index,
        time=eff_time,
        event_type=event_type,
        direction=direction,
        broken_swing_index=max(0, index - 5),
        broken_swing_price=2000.0,
        close_price=2005.0 if direction == "bullish" else 1995.0,
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
    """Create a fully validated CandidateSetup with valid SMC evidences."""
    ts = _make_utc_timestamp(bar_index * timeframe_minutes)
    cluster_id = make_cluster_id(direction, f"leg_{bar_index}", f"zone_{cluster_suffix}")
    setup_id = make_setup_id(strategy_id, direction, bar_index, cluster_id)

    # Calculate planned RR
    if direction == "BUY":
        planned_rr = (take_profit - entry_price) / (entry_price - stop_loss)
    else:
        planned_rr = (entry_price - take_profit) / (stop_loss - entry_price)

    # Respect canonical event ordering for S01 / S09: sw <= fvg < mss < candidate.bar_index
    if bar_index >= 3:
        sw_idx = bar_index - 3
        fvg_idx = bar_index - 2
        mss_idx = bar_index - 1
    else:
        sw_idx = 0
        fvg_idx = 0
        mss_idx = 0

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
                evidence_id=make_evidence_id("structure", "swing", mss_idx, f"bos_{bar_index}"),
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
    """
    Mock StrategyTemplate enabling programmable setups per bar for targeted coordinator testing.
    """

    def __init__(self, strategy_id: str, allowed_directions: tuple[str, ...] = ("BUY", "SELL")) -> None:
        self.strategy_id = strategy_id
        self.profile = StrategyProfile(
            strategy_id=strategy_id,
            name=f"Mock {strategy_id}",
            allowed_directions=tuple(allowed_directions),  # type: ignore[arg-type]
            timeframes=("M15",),
            min_rr=1.5,
        )
        self._setups_by_bar: dict[int, list[CandidateSetup]] = {}

    def schedule_setup(self, bar_index: int, setup: CandidateSetup) -> None:
        if bar_index not in self._setups_by_bar:
            self._setups_by_bar[bar_index] = []
        self._setups_by_bar[bar_index].append(setup)

    def evaluate(self, context: StrategyContext) -> Sequence[CandidateSetup]:
        return tuple(self._setups_by_bar.get(context.bar_index, []))

    def reset(self) -> None:
        pass


def _create_synthetic_candles(
    n_bars: int = 15,
    start_price: float = 2000.0,
    timeframe_minutes: int = 15,
) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV candles."""
    rows = []
    curr = start_price
    for i in range(n_bars):
        t = _make_utc_timestamp(i * timeframe_minutes)
        c_open = curr
        c_high = curr + 4.0
        c_low = curr - 4.0
        c_close = curr + 1.0
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


# =============================================================================
# TestGroupA: Cooldown Book & Eligibility Integration
# =============================================================================

class TestGroupACooldownBook(unittest.TestCase):
    """Verifies CooldownBook behavior, interval boundaries, and eligibility integration."""

    def test_cooldown_interval_and_expiry_boundary(self):
        book = CooldownBook()
        # Fill at bar 10 with K=3 bars
        book.record_fill("S01", "BUY", fill_bar_index=10, cooldown_bars=3)

        # Before fill: False (no lookahead)
        self.assertFalse(book.is_active("S01", "BUY", 9))

        # Interval [10, 13): 10, 11, 12 blocked
        self.assertTrue(book.is_active("S01", "BUY", 10))
        self.assertTrue(book.is_active("S01", "BUY", 11))
        self.assertTrue(book.is_active("S01", "BUY", 12))

        # At expiry bar 10+3=13: pass (unblocked)
        self.assertFalse(book.is_active("S01", "BUY", 13))
        self.assertFalse(book.is_active("S01", "BUY", 14))

    def test_cooldown_direction_and_strategy_isolation(self):
        book = CooldownBook()
        book.record_fill("S01", "BUY", fill_bar_index=5, cooldown_bars=3)

        # SELL on same strategy is NOT blocked
        self.assertFalse(book.is_active("S01", "SELL", 5))

        # Different strategy on same direction is NOT blocked (supporting strategy isolation)
        self.assertFalse(book.is_active("S05", "BUY", 5))
        self.assertFalse(book.is_active("S09", "BUY", 5))

    def test_cooldown_rejection_and_skip_do_not_record(self):
        """Reject at fill or same-direction skip must NOT record cooldown."""
        strat = MockProgrammableStrategy("S01")
        # Setup at bar 3 with impossible SL/TP that violates geometry at fill
        # BUY: actual_sl >= actual_entry
        setup = _make_candidate_setup("S01", "BUY", 3, entry_price=2000.0, stop_loss=1990.0, take_profit=2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(
            strategies=[strat],
            mode="smc_s01",
            execution_config=ExecutionConfig(spread_points=2000.0),  # Huge spread pushes actual_entry above TP
        )
        df = _create_synthetic_candles(6)
        htf_ev = _make_htf_event(0, 0)
        coord.run(df, htf_events=[htf_ev])

        # Fill at Open 4 was rejected -> no cooldown recorded
        self.assertFalse(coord.cooldown_book.is_active("S01", "BUY", 4))
        self.assertFalse(coord.cooldown_book.is_active("S01", "BUY", 5))

    def test_cooldown_reset_and_idempotency(self):
        book = CooldownBook()
        book.record_fill("S01", "BUY", fill_bar_index=5, cooldown_bars=3)
        self.assertTrue(book.is_active("S01", "BUY", 6))

        # Duplicate fill call on same bar does not shorten expiry
        book.record_fill("S01", "BUY", fill_bar_index=5, cooldown_bars=3)
        self.assertTrue(book.is_active("S01", "BUY", 7))
        self.assertFalse(book.is_active("S01", "BUY", 8))

        # Reset completely clears cooldowns
        book.reset()
        self.assertFalse(book.is_active("S01", "BUY", 6))
        self.assertEqual(len(book.snapshot()), 0)


# =============================================================================
# TestGroupB: Fill & No-Lookahead Semantics
# =============================================================================

class TestGroupBFillNoLookahead(unittest.TestCase):
    """Verifies market-at-next-open fill timing and strict zero lookahead."""

    def test_close_n_fills_open_n_plus_one(self):
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        htf_ev = _make_htf_event(0, 0)
        res = coord.run(df, htf_events=[htf_ev])

        # At bar 3: ORDER_SELECTED
        selected_evts = [e for e in res.execution_events if e.event_type == "ORDER_SELECTED"]
        self.assertEqual(len(selected_evts), 1)
        self.assertEqual(selected_evts[0].bar_index, 3)

        # At bar 4: ORDER_FILLED
        filled_evts = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(filled_evts), 1)
        self.assertEqual(filled_evts[0].bar_index, 4)
        self.assertEqual(filled_evts[0].signal_bar_index, 3)

    def test_first_bar_cannot_fill(self):
        strat = MockProgrammableStrategy("S01")
        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        htf_ev = _make_htf_event(0, 0)
        res = coord.run(df, htf_events=[htf_ev])

        # At bar 0, no fill can possibly occur
        fills_at_0 = [e for e in res.execution_events if e.event_type == "ORDER_FILLED" and e.bar_index == 0]
        self.assertEqual(len(fills_at_0), 0)

    def test_last_bar_select_cancelled_no_next_bar(self):
        strat = MockProgrammableStrategy("S01")
        # Setup scheduled at the last bar (bar 5 of 6 bars: 0..5)
        setup = _make_candidate_setup("S01", "BUY", 5, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(5, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        htf_ev = _make_htf_event(0, 0)
        res = coord.run(df, htf_events=[htf_ev])

        # Setup at bar 5 cannot fill at bar 6 -> ORDER_CANCELLED / no_next_bar
        cancelled_evts = [e for e in res.execution_events if e.event_type == "ORDER_CANCELLED"]
        self.assertEqual(len(cancelled_evts), 1)
        self.assertEqual(cancelled_evts[0].reason, "no_next_bar")
        self.assertEqual(cancelled_evts[0].bar_index, 5)

    def test_htf_timeline_future_event_withheld_exact_boundary_admitted(self):
        # Event at minute 30 (effective exactly at Close of Bar 1: 10:15 + 15m = 10:30)
        ev_bar1_close = _make_htf_event(1, 30)
        # Event at minute 60 (future event relative to bar 1)
        ev_future = _make_htf_event(2, 60)

        timeline = HTFTimeline([ev_bar1_close, ev_future])

        # As-of Bar 0 close (10:15): neither is effective
        emitted_bar0 = timeline.get_events_as_of(_make_utc_timestamp(15))
        self.assertEqual(len(emitted_bar0), 0)

        # As-of Bar 1 close (10:30): exact boundary matches ev_bar1_close
        emitted_bar1 = timeline.get_events_as_of(_make_utc_timestamp(30))
        self.assertEqual(len(emitted_bar1), 1)
        self.assertEqual(emitted_bar1[0].index, 1)

        # ev_future is still withheld
        self.assertEqual(timeline._cursor, 1)


# =============================================================================
# TestGroupC: Position Policy & Intrabar Lifecycle
# =============================================================================

class TestGroupCPositionPolicy(unittest.TestCase):
    """Verifies position management, same-direction skip, reversal, and dynamic SL/TP."""

    def test_flat_open_success(self):
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        filled = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0].reason, "fill_ok")

    def test_same_direction_skip(self):
        strat1 = MockProgrammableStrategy("S01")
        strat2 = MockProgrammableStrategy("S05")
        strat3 = MockProgrammableStrategy("S09")
        # Setup 1 at bar 3 BUY from S01
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1980.0, 2050.0, cluster_suffix="001")
        strat1.schedule_setup(3, setup1)
        # Setup 2 at bar 4 BUY from S05 (S05 is not on cooldown from S01 fill)
        setup2 = _make_candidate_setup("S05", "BUY", 4, 2001.0, 1980.0, 2050.0, cluster_suffix="002")
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(6)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        # Setup 2 from S05 at bar 5 Open should be skipped with position_already_open_same_direction
        skipped = [e for e in res.execution_events if e.event_type == "ORDER_SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason, "position_already_open_same_direction")

    def test_opposite_direction_atomic_reversal(self):
        strat1 = MockProgrammableStrategy("S01")
        strat2 = MockProgrammableStrategy("S05")
        strat3 = MockProgrammableStrategy("S09")
        # Setup 1 at bar 3 BUY
        setup1 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1980.0, 2050.0, cluster_suffix="001")
        strat1.schedule_setup(3, setup1)
        # Setup 2 at bar 4 SELL (opposite direction reversal, S05 is not on cooldown)
        setup2 = _make_candidate_setup("S05", "SELL", 4, 2001.0, 2020.0, 1970.0, cluster_suffix="002")
        strat2.schedule_setup(4, setup2)

        coord = SMCBacktestCoordinator(strategies=[strat1, strat2, strat3], mode="smc_wave1")
        df = _create_synthetic_candles(6)
        # Provide bullish HTF event at 0, and bearish HTF events at bar 4 close (minute 75)
        htf_events = [
            _make_htf_event(0, 0, direction="bullish", event_type="BOS"),
            _make_htf_event(1, 70, direction="bearish", event_type="CHoCH"),
            _make_htf_event(2, 75, direction="bearish", event_type="BOS"),
        ]
        res = coord.run(df, htf_events=htf_events)

        # Old position closed with opposite_signal
        pos_closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED" and e.reason == "opposite_signal"]
        self.assertEqual(len(pos_closed), 1)
        self.assertEqual(pos_closed[0].direction, "BUY")

        # New position filled with fill_ok
        fills = [e for e in res.execution_events if e.event_type == "ORDER_FILLED"]
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[1].direction, "SELL")

    def test_allow_short_disabled_skips_sell(self):
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "SELL", 3, 2000.0, 2020.0, 1960.0)
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

    def test_intrabar_stop_loss_trigger(self):
        strat = MockProgrammableStrategy("S01")
        # Entry at bar 4 (open ~2001), SL at 1995.0
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1995.0, 2040.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        # Force bar 4 low to hit SL: low = 1990.0 <= 1995.0
        df.loc[4, "low"] = 1990.0

        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        sl_closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED" and e.reason == "stop_loss"]
        self.assertEqual(len(sl_closed), 1)
        self.assertEqual(sl_closed[0].bar_index, 4)

    def test_intrabar_sl_first_collision(self):
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1995.0, 2040.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        # Bar 4 hits BOTH SL and TP: low = 1990.0 <= 1995.0, high = 2050.0 >= 2040.0
        df.loc[4, "low"] = 1990.0
        df.loc[4, "high"] = 2050.0

        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        closed = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
        self.assertEqual(len(closed), 1)
        # Conservative SL-first: reason must be stop_loss
        self.assertEqual(closed[0].reason, "stop_loss")


# =============================================================================
# TestGroupD: RR, Geometry & Spread Transformation
# =============================================================================

class TestGroupDRRGeometry(unittest.TestCase):
    """Verifies spread price transformation, geometry checks, and cash-basis RR."""

    def test_buy_and_sell_spread_transform(self):
        cfg = ExecutionConfig(spread_points=20.0, point_value=0.01)  # spread = 0.20 USD
        intent_buy = _make_candidate_setup("S01", "BUY", 0, 2000.0, 1990.0, 2030.0)
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
                decision_id="sel:0:SELECT:S01",
                bar_index=0,
                timestamp=_make_utc_timestamp(0),
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
        self.assertEqual(val_buy.actual_sl, 1990.0)      # signal_sl
        self.assertEqual(val_buy.actual_tp, 2030.0)      # signal_tp

        intent_sell = _make_candidate_setup("S01", "SELL", 0, 2000.0, 2010.0, 1970.0)
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
                decision_id="sel:0:SELECT:S01",
                bar_index=0,
                timestamp=_make_utc_timestamp(0),
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
        self.assertEqual(val_sell.actual_sl, 2010.20)     # signal_sl + spread
        self.assertEqual(val_sell.actual_tp, 1970.20)     # signal_tp + spread

    def test_geometry_violation_at_fill_reject(self):
        cfg = ExecutionConfig(spread_points=20.0, point_value=0.01)
        intent = _make_candidate_setup("S01", "BUY", 0, 2000.0, 1995.0, 2010.0)
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
        p = PendingExecutionIntent.from_selection_decision(
            SelectionDecision(
                decision_id="sel:0:SELECT:S01",
                bar_index=0,
                timestamp=_make_utc_timestamp(0),
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

        # Gap open at 2015.0 > TP (2010.0) -> geometry violation
        val = validate_fill(p, open_price=2015.0, config=cfg)
        self.assertFalse(val.is_valid)
        self.assertEqual(val.reason, "geometry_violation_at_fill")

    def test_insufficient_rr_at_fill_reject(self):
        cfg = ExecutionConfig(spread_points=20.0, point_value=0.01, commission_per_lot=5.0)
        # Tight planned RR close to 1.5
        intent = _make_candidate_setup("S01", "BUY", 0, 2000.0, 1990.0, 2016.0)
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
        p = PendingExecutionIntent.from_selection_decision(
            SelectionDecision(
                decision_id="sel:0:SELECT:S01",
                bar_index=0,
                timestamp=_make_utc_timestamp(0),
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
            "XAUUSD", "M15", min_rr=2.0,  # Requires min_rr=2.0
        )

        # Fill at 2002.0 reduces reward and increases risk -> effective RR < 2.0
        val = validate_fill(p, open_price=2002.0, config=cfg)
        self.assertFalse(val.is_valid)
        self.assertEqual(val.reason, "insufficient_rr_at_fill")


# =============================================================================
# TestGroupE: Execution Events & Audit Trail Traceability
# =============================================================================

class TestGroupEExecutionEventsAudit(unittest.TestCase):
    """Verifies schema version, event IDs, JSON round-trip, and complete audit traceability."""

    def test_execution_event_json_roundtrip_and_no_duplicates(self):
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        event_ids = set()
        for evt in res.execution_events:
            # 1. Event ID must be deterministic and non-empty
            self.assertTrue(evt.event_id.startswith("evt:"))
            self.assertNotIn(evt.event_id, event_ids)
            event_ids.add(evt.event_id)

            # 2. Schema version must be "1.0.0"
            self.assertEqual(evt.event_version, "1.0.0")

            # 3. JSON round-trip must succeed without NaN, Inf, or custom objects
            evt_dict = evt.to_dict()
            json_str = json.dumps(evt_dict)
            self.assertIsInstance(json_str, str)
            reloaded = json.loads(json_str)
            self.assertEqual(reloaded["event_id"], evt.event_id)

    def test_closed_trade_metadata_traces_to_selector_decision(self):
        strat = MockProgrammableStrategy("S01")
        setup = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup)

        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(6)
        # Trigger take profit at bar 4
        df.loc[4, "high"] = 2035.0
        res = coord.run(df, htf_events=[_make_htf_event(0, 0)])

        # Find POSITION_CLOSED event
        closed_evts = [e for e in res.execution_events if e.event_type == "POSITION_CLOSED"]
        self.assertEqual(len(closed_evts), 1)
        ce = closed_evts[0]

        # Verify exact traceability
        self.assertEqual(ce.strategy_id, "S01")
        self.assertEqual(ce.setup_id, setup.setup_id)
        self.assertEqual(ce.signal_bar_index, 3)
        self.assertTrue(ce.decision_id.startswith("sel:3:"))
        self.assertIsNotNone(ce.actual_entry)
        self.assertIsNotNone(ce.exit_price)
        self.assertIsNotNone(ce.net_pnl)


# =============================================================================
# TestGroupF: Mode Support (smc_wave1, smc_s01, smc_s05, smc_s09)
# =============================================================================

class TestGroupFModeSupport(unittest.TestCase):
    """Verifies mode selection and strategy registry enablement."""

    def test_mode_selection_configuration(self):
        # smc_wave1 enables all 3 strategies
        c_wave1 = SMCBacktestCoordinator(mode="smc_wave1")
        self.assertEqual(c_wave1.strategy_registry.enabled_strategy_ids, ("S01", "S05", "S09"))

        # Single strategy modes
        c_s01 = SMCBacktestCoordinator(mode="smc_s01")
        self.assertEqual(c_s01.strategy_registry.enabled_strategy_ids, ("S01",))

        c_s05 = SMCBacktestCoordinator(mode="smc_s05")
        self.assertEqual(c_s05.strategy_registry.enabled_strategy_ids, ("S05",))

        c_s09 = SMCBacktestCoordinator(mode="smc_s09")
        self.assertEqual(c_s09.strategy_registry.enabled_strategy_ids, ("S09",))

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            SMCBacktestCoordinator(mode="invalid_mode")  # type: ignore[arg-type]


# =============================================================================
# TestGroupG: Batch vs Incremental vs Replay-Prefix Parity
# =============================================================================

class TestGroupGParity(unittest.TestCase):
    """Verifies 100% bit-for-bit parity across batch, incremental, and prefix replays."""

    def test_batch_vs_incremental_exact_parity(self):
        strat = MockProgrammableStrategy("S01")
        setup0 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup0)

        df = _create_synthetic_candles(8)
        htf_ev = _make_htf_event(0, 0)

        # 1. Batch execution
        coord_batch = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        res_batch = coord_batch.run(df, htf_events=[htf_ev])

        # 2. Incremental execution
        coord_incr = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01", htf_events=[htf_ev])
        n_bars = len(df)
        for idx in range(n_bars):
            coord_incr.step(df.iloc[idx], is_last_bar=(idx == n_bars - 1))

        # Re-export incremental results via helper
        self.assertEqual(len(coord_incr._all_decisions), len(res_batch.decisions))
        self.assertEqual(len(coord_incr._all_events), len(res_batch.execution_events))
        self.assertEqual(coord_incr.kernel.trades, list(res_batch.trades))
        self.assertEqual(coord_incr.cooldown_book.snapshot(), res_batch.cooldown_snapshot)

        # Event-by-event match
        for eb, ei in zip(res_batch.execution_events, coord_incr._all_events):
            self.assertEqual(eb.to_dict(), ei.to_dict())

    def test_replay_prefix_invariance(self):
        """Historical prefix output must be completely invariant to appended future bars."""
        strat = MockProgrammableStrategy("S01")
        setup0 = _make_candidate_setup("S01", "BUY", 3, 2000.0, 1990.0, 2030.0)
        strat.schedule_setup(3, setup0)

        df_full = _create_synthetic_candles(10)
        df_prefix = df_full.iloc[:6]
        htf_ev = _make_htf_event(0, 0)

        # Run on prefix
        coord_prefix = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        res_prefix = coord_prefix.run(df_prefix, htf_events=[htf_ev])

        # Run on full
        coord_full = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        res_full = coord_full.run(df_full, htf_events=[htf_ev])

        # First 5 decisions (excluding last bar of prefix which had is_last_bar=True) must match exactly
        for i in range(5):
            self.assertEqual(res_prefix.decisions[i].to_dict(), res_full.decisions[i].to_dict())

    def test_idempotent_duplicate_step_call(self):
        strat = MockProgrammableStrategy("S01")
        coord = SMCBacktestCoordinator(strategies=[strat], mode="smc_s01")
        df = _create_synthetic_candles(3)

        # Step bar 0
        res1 = coord.step(df.iloc[0], is_last_bar=False)
        # Duplicate step bar 0 with identical payload -> returns cached result
        res2 = coord.step(df.iloc[0], is_last_bar=False)
        self.assertEqual(res1.bar_index, res2.bar_index)
        self.assertEqual(len(coord._all_decisions), 1)

        # Conflicting step bar 0 with altered price -> raises StrategyStateError
        tampered = df.iloc[0].copy()
        tampered["close"] = 9999.0
        with self.assertRaises(StrategyStateError):
            coord.step(tampered, is_last_bar=False)


if __name__ == "__main__":
    unittest.main()
