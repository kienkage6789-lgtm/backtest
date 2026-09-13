"""
tests/test_smc_engine_execution.py
----------------------------------
Unit, contract, and regression tests for execution domain models, golden execution vectors,
pure fill validation gate, cash-basis RR, deterministic cooldown tracking, and audit events (T53.9.0 & T53.9.1).

Semantics strictly adhere to ADR 25:
- Analysis at Close N -> Execution at Open N+1 (market-at-next-open).
- Dynamic SL/TP levels, spread adjustments, and round-trip commission accounting.
- Pure fill gate: validates geometry and cash-basis RR before position entry or reversal.
- Cooldown: begins strictly after successful fill, keyed by (primary_strategy_id, direction).
- Fail-closed validation for timestamps, composite IDs, cross-field invariants, and cooldown books.
"""

from __future__ import annotations

import copy
import datetime
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
import unittest
from typing import Any

import numpy as np
import pandas as pd

from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    SelectionDecision,
    StrategyEvaluation,
    StrictModelTypeError,
    make_cluster_id,
    make_decision_id,
    make_setup_id,
)
from smc.engine.execution import (
    CooldownBook,
    CooldownInterval,
    ExecutionConfig,
    ExecutionEvent,
    FillValidationResult,
    PendingExecutionIntent,
    VALID_EXECUTION_EVENT_TYPES,
    VALID_FILL_GATE_REASONS,
    make_execution_event_id,
    make_execution_intent_id,
    validate_fill,
)


def _make_intent(
    direction: str = "BUY",
    planned_entry: float = 2002.0,
    signal_sl: float = 2000.0,
    signal_tp: float = 2006.0,
    min_rr: float = 1.5,
    signal_bar_index: int = 10,
    primary_strategy_id: str = "smc_s01",
    cluster_id: str | None = None,
    primary_setup_id: str | None = None,
    decision_id: str | None = None,
    supporting_strategy_ids: tuple[str, ...] = ("smc_s09",),
    meta: dict[str, Any] | None = None,
    signal_bar_time: Any = pd.Timestamp("2026-09-11 12:00:00+00:00"),
    action: str = "SELECT",
) -> PendingExecutionIntent:
    """Helper to construct a valid PendingExecutionIntent with canonical composite IDs."""
    if cluster_id is None:
        cluster_id = make_cluster_id(direction, "leg-a", "zone-a")
    if primary_setup_id is None:
        primary_setup_id = make_setup_id(primary_strategy_id, direction, signal_bar_index, cluster_id)
    if decision_id is None:
        decision_id = f"sel:{signal_bar_index}:{action}:{primary_strategy_id}"
    return PendingExecutionIntent(
        intent_id=make_execution_intent_id(signal_bar_index, decision_id),
        decision_id=decision_id,
        symbol="XAUUSD",
        timeframe="15m",
        signal_bar_index=signal_bar_index,
        signal_bar_time=signal_bar_time,
        action=action,
        primary_strategy_id=primary_strategy_id,
        primary_setup_id=primary_setup_id,
        direction=direction,
        planned_entry=planned_entry,
        signal_sl=signal_sl,
        signal_tp=signal_tp,
        planned_rr=2.0,
        total_score=75.5,
        min_rr=min_rr,
        cluster_id=cluster_id,
        supporting_strategy_ids=supporting_strategy_ids,
        meta=meta if meta is not None else {"regime": "bullish_trend"},
    )


def _make_candidate_setup(
    strategy_id: str = "smc_s01",
    direction: str = "BUY",
    bar_index: int = 10,
    entry_price: float = 2002.0,
    stop_loss: float = 2000.0,
    take_profit: float = 2006.0,
    planned_rr: float = 2.0,
    cluster_id: str | None = None,
    setup_id: str | None = None,
    expiry_bar: int = 15,
) -> CandidateSetup:
    """Helper to construct a valid CandidateSetup with canonical composite IDs."""
    if cluster_id is None:
        cluster_id = make_cluster_id(direction, "leg-a", "zone-a")
    if setup_id is None:
        setup_id = make_setup_id(strategy_id, direction, bar_index, cluster_id)
    ev = EvidenceRef(
        evidence_id=f"ev:{bar_index}:ob",
        kind="order_block",
        bar_index=bar_index,
        price=2001.0,
    )
    return CandidateSetup(
        setup_id=setup_id,
        strategy_id=strategy_id,
        direction=direction,
        bar_index=bar_index,
        timestamp=pd.Timestamp("2026-09-11 12:00:00+00:00"),
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        planned_rr=planned_rr,
        evidences=(ev,),
        evidence_cluster_id=cluster_id,
        expiry_bar=expiry_bar,
    )


def _make_selection_decision(
    candidate: CandidateSetup,
    bar_index: int = 10,
    supporting_strategies: tuple[str, ...] = ("smc_s09",),
    action: str = "SELECT",
    reason: str = "ok",
) -> SelectionDecision:
    """Helper to construct a valid SelectionDecision."""
    eval_item = StrategyEvaluation(
        candidate=candidate,
        status="ELIGIBLE",
        regime_score=80.0,
        setup_score=85.0,
        context_score=75.0,
        exec_score=80.0,
        total_score=80.5,
    )
    return SelectionDecision(
        bar_index=bar_index,
        timestamp=pd.Timestamp("2026-09-11 12:00:00+00:00"),
        decision_id=make_decision_id(bar_index, action, candidate.strategy_id),
        action=action,
        primary_strategy_id=candidate.strategy_id,
        selected_setup=candidate,
        supporting_strategy_ids=supporting_strategies,
        reason=reason,
        score_gap=5.0 if action == "SELECT" else None,
        evaluations=(eval_item,),
    )


# =============================================================================
# 1. Golden Execution Vectors (T53.9.0)
# =============================================================================

class TestGoldenExecutionVectors(unittest.TestCase):
    """
    T53.9.0: Golden execution vectors verifying hand-calculated numbers
    for BUY, SELL, geometry rejects, RR rejects, and expected reversal accounting vectors.
    """

    def setUp(self) -> None:
        self.config = ExecutionConfig(
            point_value=0.01,
            lot_size=0.1,
            contract_size=100.0,
            spread_points=20.0,      # 0.20 USD spread
            commission_per_lot=5.0,  # 0.50 USD / side -> 1.00 USD round-trip
            min_rr_fallback=1.5,
            allow_short=True,
        )

    def test_case_01_golden_buy_fill_and_exact_rr(self) -> None:
        """
        Golden BUY:
        - Signal N: BUY, planned 2002.00, SL 2000.00, TP 2006.00, min_rr 1.50
        - Bar N+1 Open: 2002.10
        - actual_entry = 2002.10 + 0.20 = 2002.30
        - actual_sl = 2000.00, actual_tp = 2006.00
        - gross_risk_cash = (2002.30 - 2000.00) * 10 = 23.00 USD
        - risk_cash = 23.00 + 1.00 = 24.00 USD
        - gross_reward_cash = (2006.00 - 2002.30) * 10 = 37.00 USD
        - reward_cash = 37.00 - 1.00 = 36.00 USD
        - effective_rr = 36.00 / 24.00 = 1.50 (exact boundary pass)
        """
        intent = _make_intent(
            direction="BUY",
            planned_entry=2002.0,
            signal_sl=2000.0,
            signal_tp=2006.0,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=2002.10, config=self.config)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reason, "fill_ok")
        self.assertEqual(res.actual_entry, 2002.30)
        self.assertEqual(res.actual_sl, 2000.00)
        self.assertEqual(res.actual_tp, 2006.00)
        self.assertAlmostEqual(res.risk_cash, 24.00, places=4)
        self.assertAlmostEqual(res.reward_cash, 36.00, places=4)
        self.assertAlmostEqual(res.effective_rr, 1.50, places=4)

    def test_case_02_golden_sell_fill_and_ask_spread(self) -> None:
        """
        Golden SELL:
        - Signal N: SELL, planned 2000.00, SL 2005.00, TP 1990.00, min_rr 1.50
        - Bar N+1 Open: 2000.00
        - actual_entry = 2000.00 (Bid)
        - actual_sl = 2005.00 + 0.20 = 2005.20 (Ask)
        - actual_tp = 1990.00 + 0.20 = 1990.20 (Ask)
        - risk_cash = (2005.20 - 2000.00) * 10 + 1.00 = 53.00 USD
        - reward_cash = (2000.00 - 1990.20) * 10 - 1.00 = 97.00 USD
        - effective_rr = 97.00 / 53.00 = 1.830189 >= 1.50 -> pass
        """
        intent = _make_intent(
            direction="SELL",
            planned_entry=2000.0,
            signal_sl=2005.0,
            signal_tp=1990.0,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=2000.00, config=self.config)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reason, "fill_ok")
        self.assertEqual(res.actual_entry, 2000.00)
        self.assertEqual(res.actual_sl, 2005.20)
        self.assertEqual(res.actual_tp, 1990.20)
        self.assertAlmostEqual(res.risk_cash, 53.00, places=4)
        self.assertAlmostEqual(res.reward_cash, 97.00, places=4)
        self.assertAlmostEqual(res.effective_rr, 97.0 / 53.0, places=5)

    def test_case_03_golden_buy_gap_past_sl_geometry_reject(self) -> None:
        """
        Golden BUY Geometry Reject: Open gapped below SL.
        - Signal N: BUY, SL 2000.00, TP 2010.00
        - Bar N+1 Open: 1999.50 -> actual_entry = 1999.70 < actual_sl 2000.00
        - Result: geometry_violation_at_fill
        """
        intent = _make_intent(
            direction="BUY",
            planned_entry=2005.0,
            signal_sl=2000.0,
            signal_tp=2010.0,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=1999.50, config=self.config)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "geometry_violation_at_fill")
        self.assertEqual(res.actual_entry, 1999.70)
        self.assertEqual(res.actual_sl, 2000.00)
        self.assertEqual(res.risk_cash, 0.0)
        self.assertEqual(res.reward_cash, 0.0)
        self.assertEqual(res.effective_rr, 0.0)

    def test_case_04_golden_buy_gap_up_insufficient_rr_reject(self) -> None:
        """
        Golden BUY RR Reject: Open gapped up, reducing reward and increasing risk.
        - Signal N: BUY, SL 2000.00, TP 2003.00, min_rr 1.50
        - Bar N+1 Open: 2001.50 -> actual_entry = 2001.70
        - risk_cash = (2001.70 - 2000.00) * 10 + 1.00 = 18.00 USD
        - reward_cash = (2003.00 - 2001.70) * 10 - 1.00 = 12.00 USD
        - effective_rr = 12.00 / 18.00 = 0.6667 < 1.50 -> insufficient_rr_at_fill
        """
        intent = _make_intent(
            direction="BUY",
            planned_entry=2001.0,
            signal_sl=2000.0,
            signal_tp=2003.0,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=2001.50, config=self.config)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "insufficient_rr_at_fill")
        self.assertAlmostEqual(res.risk_cash, 18.00, places=4)
        self.assertAlmostEqual(res.reward_cash, 12.00, places=4)
        self.assertAlmostEqual(res.effective_rr, 12.0 / 18.0, places=4)

    def test_case_05_golden_sell_gap_past_sl_geometry_reject(self) -> None:
        """
        Golden SELL Geometry Reject: Open gapped above Ask SL.
        - Signal N: SELL, SL 2000.00 (Ask 2000.20), TP 1990.00 (Ask 1990.20)
        - Bar N+1 Open: 2001.00 -> actual_entry 2001.00 > actual_sl 2000.20
        - Result: geometry_violation_at_fill
        """
        intent = _make_intent(
            direction="SELL",
            planned_entry=1995.0,
            signal_sl=2000.0,
            signal_tp=1990.0,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=2001.00, config=self.config)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "geometry_violation_at_fill")
        self.assertEqual(res.actual_entry, 2001.00)
        self.assertEqual(res.actual_sl, 2000.20)
        self.assertEqual(res.risk_cash, 0.0)
        self.assertEqual(res.reward_cash, 0.0)
        self.assertEqual(res.effective_rr, 0.0)

    def test_case_06_golden_reversal_accounting_expected_vector_for_t53_9_2(self) -> None:
        """
        Golden Reversal Expected Vector (Target for T53.9.2 Execution Kernel).
        Existing BUY Position + New Valid SELL Signal.
        - Existing BUY: entry = 2000.00.
        - Open N+1 = 2010.00.
        - New SELL is valid: actual_entry = 2010.00, actual_sl = 2015.20, actual_tp = 1995.20.
          effective_rr = (14.80 * 10 - 1.00) / (5.20 * 10 + 1.00) = 147.0 / 53.0 = 2.7735 >= 1.50 -> pass.
        - Step 1: Closing existing BUY at Bid Open 2010.00:
          gross_pnl = (2010.00 - 2000.00) * 10 = 100.00 USD
          net_pnl = 100.00 - 1.00 (round-trip commission) = 99.00 USD
        - Step 2: New SELL position filled at 2010.00.

        NOTE: Full position state and reversal execution will be verified in T53.9.2
        when the shared execution kernel is implemented. This test locks the expected
        mathematical accounting vector for reuse.
        """
        intent = _make_intent(
            direction="SELL",
            planned_entry=2010.0,
            signal_sl=2015.0,
            signal_tp=1995.0,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=2010.00, config=self.config)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reason, "fill_ok")
        # Hand-calculated reversal PnL of prior BUY position:
        buy_entry = 2000.00
        reversal_exit = 2010.00
        gross_pnl = (reversal_exit - buy_entry) * self.config.lot_size * self.config.contract_size
        net_pnl = gross_pnl - self.config.round_trip_commission
        self.assertAlmostEqual(gross_pnl, 100.00, places=4)
        self.assertAlmostEqual(net_pnl, 99.00, places=4)

    def test_case_07_golden_reversal_guard_reject_preserves_position_expected_vector_for_t53_9_2(self) -> None:
        """
        Golden Reversal Guard Expected Vector (Target for T53.9.2 Execution Kernel).
        Existing BUY Position + New Invalid SELL Signal.
        - New SELL at Open 2016.00 has gapped above SL 2015.20 -> rejected!
        - Reversal guard requires the new intent to pass BEFORE closing existing position.
        - When validate_fill fails, is_valid is False -> existing position must NOT be closed.

        NOTE: Position preservation under reversal guard failure will be verified in T53.9.2
        when the shared execution kernel is implemented.
        """
        intent = _make_intent(
            direction="SELL",
            planned_entry=2010.0,
            signal_sl=2015.0,
            signal_tp=1995.0,
            min_rr=1.5,
        )
        # Gapped up to 2016.00 (above SL 2015.20)
        res = validate_fill(intent, open_price=2016.00, config=self.config)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "geometry_violation_at_fill")


# =============================================================================
# 2. ExecutionConfig Tests (T53.9.1)
# =============================================================================

class TestExecutionConfig(unittest.TestCase):
    """T53.9.1: Tests for ExecutionConfig model, strictness, and serialization."""

    def test_default_config_properties(self) -> None:
        cfg = ExecutionConfig()
        self.assertEqual(cfg.point_value, 0.01)
        self.assertEqual(cfg.lot_size, 0.1)
        self.assertEqual(cfg.contract_size, 100.0)
        self.assertEqual(cfg.spread_points, 20.0)
        self.assertEqual(cfg.commission_per_lot, 5.0)
        self.assertEqual(cfg.min_rr_fallback, 1.5)
        self.assertTrue(cfg.allow_short)
        self.assertAlmostEqual(cfg.spread, 0.20, places=6)
        self.assertAlmostEqual(cfg.commission_per_side, 0.50, places=6)
        self.assertAlmostEqual(cfg.round_trip_commission, 1.00, places=6)

    def test_strict_types_rejections(self) -> None:
        with self.assertRaises(StrictModelTypeError):
            ExecutionConfig(point_value=True)  # type: ignore[arg-type]
        with self.assertRaises(StrictModelTypeError):
            ExecutionConfig(lot_size=False)  # type: ignore[arg-type]
        with self.assertRaises(StrictModelTypeError):
            ExecutionConfig(allow_short=1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ExecutionConfig(spread_points=-1.0)
        with self.assertRaises(ValueError):
            ExecutionConfig(commission_per_lot=-0.5)
        with self.assertRaises(ValueError):
            ExecutionConfig(point_value=math.nan)
        with self.assertRaises(ValueError):
            ExecutionConfig(point_value=math.inf)

    def test_exact_json_round_trip(self) -> None:
        cfg = ExecutionConfig(
            point_value=0.02,
            lot_size=0.2,
            contract_size=50.0,
            spread_points=15.0,
            commission_per_lot=4.0,
            min_rr_fallback=2.0,
            allow_short=False,
        )
        d = cfg.to_dict()
        j = json.dumps(d)
        d_parsed = json.loads(j)
        cfg_restored = ExecutionConfig.from_dict(d_parsed)
        self.assertEqual(cfg, cfg_restored)

    def test_from_dict_rejections(self) -> None:
        valid_dict = ExecutionConfig().to_dict()
        with self.assertRaises(StrictModelTypeError):
            ExecutionConfig.from_dict("not_a_dict")  # type: ignore[arg-type]

        # Missing key
        d_missing = copy.deepcopy(valid_dict)
        del d_missing["spread_points"]
        with self.assertRaises(KeyError):
            ExecutionConfig.from_dict(d_missing)

        # Extra unknown key
        d_extra = copy.deepcopy(valid_dict)
        d_extra["extra_key"] = 123
        with self.assertRaises(KeyError):
            ExecutionConfig.from_dict(d_extra)


# =============================================================================
# 3. PendingExecutionIntent Tests (T53.9.1 & Regression Probes 1, 2, 7, 8, 9, 15, 16, 17)
# =============================================================================

class TestPendingExecutionIntent(unittest.TestCase):
    """T53.9.1: Tests for PendingExecutionIntent model, composite IDs, factory, strictness, and round-trip."""

    def test_valid_intent_creation_and_immutability(self) -> None:
        intent = _make_intent()
        self.assertEqual(intent.action, "SELECT")
        self.assertEqual(intent.direction, "BUY")
        self.assertTrue(isinstance(intent.meta, Mapping))
        with self.assertRaises((AttributeError, TypeError)):
            intent.planned_entry = 999.0  # type: ignore[misc]

    # --- Probe 1: Real make_cluster_id() and make_setup_id() accepted ---
    def test_probe_01_real_ids_accepted(self) -> None:
        cluster_id = make_cluster_id("BUY", "leg-a", "zone-a")
        self.assertEqual(cluster_id, "BUY:leg-a:zone-a")
        setup_id = make_setup_id("smc_s01", "BUY", 10, cluster_id)
        self.assertEqual(setup_id, "smc_s01:BUY:10:BUY:leg-a:zone-a")

        intent = _make_intent(
            direction="BUY",
            primary_strategy_id="smc_s01",
            cluster_id=cluster_id,
            primary_setup_id=setup_id,
            signal_bar_index=10,
        )
        self.assertEqual(intent.cluster_id, cluster_id)
        self.assertEqual(intent.primary_setup_id, setup_id)

    # --- Probe 2: Setup ID wrong strategy / direction / bar / cluster rejected ---
    def test_probe_02_setup_id_mismatch_rejections(self) -> None:
        valid_cluster = make_cluster_id("BUY", "leg-a", "zone-a")

        # Strategy mismatch
        wrong_strat_setup = make_setup_id("smc_s05", "BUY", 10, valid_cluster)
        with self.assertRaises(ValueError) as ctx:
            _make_intent(primary_strategy_id="smc_s01", primary_setup_id=wrong_strat_setup, cluster_id=valid_cluster)
        self.assertIn("does not match primary_strategy_id", str(ctx.exception))

        # Direction mismatch
        wrong_dir_setup = make_setup_id("smc_s01", "SELL", 10, valid_cluster)
        with self.assertRaises(ValueError) as ctx:
            _make_intent(direction="BUY", primary_strategy_id="smc_s01", primary_setup_id=wrong_dir_setup, cluster_id=valid_cluster)
        self.assertIn("does not match direction", str(ctx.exception))

        # Bar index > signal_bar_index
        future_bar_setup = make_setup_id("smc_s01", "BUY", 12, valid_cluster)
        with self.assertRaises(ValueError) as ctx:
            _make_intent(signal_bar_index=10, primary_setup_id=future_bar_setup, cluster_id=valid_cluster)
        self.assertIn("cannot be greater than signal_bar_index", str(ctx.exception))

        # Cluster suffix mismatch
        other_cluster = make_cluster_id("BUY", "leg-b", "zone-b")
        mismatch_cluster_setup = make_setup_id("smc_s01", "BUY", 10, other_cluster)
        with self.assertRaises(ValueError) as ctx:
            _make_intent(cluster_id=valid_cluster, primary_setup_id=mismatch_cluster_setup)
        self.assertIn("does not match cluster_id", str(ctx.exception))

        # Malformed setup ID (not 4 components)
        with self.assertRaises(ValueError) as ctx:
            _make_intent(cluster_id=valid_cluster, primary_setup_id="invalid_setup_id")
        self.assertIn("must contain exactly 4 components delimited by ':'", str(ctx.exception))

        # Malformed cluster ID (space or invalid character)
        with self.assertRaises(ValueError):
            _make_intent(cluster_id="BUY:invalid leg:zone", primary_setup_id="smc_s01:BUY:10:BUY:invalid leg:zone")

    # --- Probe 7: Naive and numeric timestamps rejected ---
    def test_probe_07_timestamp_fail_closed(self) -> None:
        # Naive Timestamp rejected
        with self.assertRaises(ValueError) as ctx:
            _make_intent(signal_bar_time=pd.Timestamp("2026-09-11 12:00:00"))
        self.assertIn("must be timezone-aware", str(ctx.exception))

        # Naive datetime rejected
        with self.assertRaises(ValueError) as ctx:
            _make_intent(signal_bar_time=datetime.datetime(2026, 9, 11, 12, 0))
        self.assertIn("must be timezone-aware", str(ctx.exception))

        # Numeric (int / float) rejected
        with self.assertRaises(StrictModelTypeError):
            _make_intent(signal_bar_time=0)
        with self.assertRaises(StrictModelTypeError):
            _make_intent(signal_bar_time=1700000000.0)

        # Boolean rejected
        with self.assertRaises(StrictModelTypeError):
            _make_intent(signal_bar_time=True)

        # Date-only rejected
        with self.assertRaises(StrictModelTypeError):
            _make_intent(signal_bar_time=datetime.date(2026, 9, 11))

        # Naive ISO string rejected
        with self.assertRaises(ValueError) as ctx:
            _make_intent(signal_bar_time="2026-09-11T12:00:00")
        self.assertIn("must have explicit UTC offset", str(ctx.exception))

        # Malformed string rejected
        with self.assertRaises(ValueError):
            _make_intent(signal_bar_time="not-a-timestamp")

        # Timezone-aware ISO string with UTC offset accepted and normalized
        intent = _make_intent(signal_bar_time="2026-09-11T19:00:00+07:00")
        self.assertEqual(intent.signal_bar_time, pd.Timestamp("2026-09-11 12:00:00+00:00"))
        self.assertEqual(intent.signal_bar_time.tzinfo, datetime.timezone.utc)

    # --- Probe 8: Supporting IDs as string rejected ---
    def test_probe_08_supporting_ids_string_rejected(self) -> None:
        with self.assertRaises(StrictModelTypeError):
            _make_intent(supporting_strategy_ids="smc_s09")  # type: ignore[arg-type]
        with self.assertRaises(StrictModelTypeError):
            _make_intent(supporting_strategy_ids=b"smc_s09")  # type: ignore[arg-type]
        with self.assertRaises(StrictModelTypeError):
            _make_intent(supporting_strategy_ids={"smc_s09": 1})  # type: ignore[arg-type]

    # --- Probe 9: Duplicate / primary supporting strategy rejected ---
    def test_probe_09_supporting_ids_duplicates_and_primary_rejected(self) -> None:
        # Contains primary strategy
        with self.assertRaises(ValueError) as ctx:
            _make_intent(primary_strategy_id="smc_s01", supporting_strategy_ids=("smc_s01",))
        self.assertIn("cannot contain primary_strategy_id", str(ctx.exception))

        # Duplicate supporting strategies
        with self.assertRaises(ValueError) as ctx:
            _make_intent(supporting_strategy_ids=("smc_s05", "smc_s05"))
        self.assertIn("Duplicate supporting_strategy_id", str(ctx.exception))

        # Unsorted supporting strategies
        with self.assertRaises(ValueError) as ctx:
            _make_intent(supporting_strategy_ids=("smc_s09", "smc_s05"))
        self.assertIn("must be canonically sorted", str(ctx.exception))

        # Invalid token grammar in supporting strategy
        with self.assertRaises(ValueError):
            _make_intent(supporting_strategy_ids=("invalid:token",))

    # --- Probe 16: Metadata deep immutability ---
    def test_probe_16_metadata_deep_immutability(self) -> None:
        meta_dict = {"regime": "bullish", "nested": {"param": 10}}
        intent = _make_intent(meta=meta_dict)
        self.assertIsInstance(intent.meta, MappingProxyType)
        with self.assertRaises((TypeError, AttributeError)):
            intent.meta["new_key"] = "val"  # type: ignore[index]

    # --- Probe 17: Factory PendingExecutionIntent.from_selection_decision ---
    def test_probe_17_factory_from_selection_decision(self) -> None:
        candidate = _make_candidate_setup(
            strategy_id="smc_s01",
            direction="BUY",
            bar_index=10,
            entry_price=2002.0,
            stop_loss=2000.0,
            take_profit=2006.0,
            planned_rr=2.0,
        )
        decision = _make_selection_decision(
            candidate=candidate,
            bar_index=10,
            supporting_strategies=("smc_s09",),
            action="SELECT",
        )

        decision_before = decision.to_dict()
        candidate_before = candidate.to_dict()

        intent = PendingExecutionIntent.from_selection_decision(
            decision=decision,
            symbol="XAUUSD",
            timeframe="15m",
            min_rr=1.5,
            meta={"extra": "audit"},
        )

        # Derived fields match exactly without caller re-specification
        self.assertEqual(intent.primary_strategy_id, "smc_s01")
        self.assertEqual(intent.primary_setup_id, candidate.setup_id)
        self.assertEqual(intent.direction, "BUY")
        self.assertEqual(intent.planned_entry, 2002.0)
        self.assertEqual(intent.signal_sl, 2000.0)
        self.assertEqual(intent.signal_tp, 2006.0)
        self.assertEqual(intent.planned_rr, 2.0)
        self.assertEqual(intent.cluster_id, candidate.evidence_cluster_id)
        self.assertEqual(intent.supporting_strategy_ids, ("smc_s09",))
        self.assertEqual(intent.signal_bar_index, 10)
        self.assertEqual(intent.signal_bar_time, pd.Timestamp("2026-09-11 12:00:00+00:00"))
        self.assertEqual(intent.meta["extra"], "audit")

        # Inputs were NOT mutated
        self.assertEqual(decision.to_dict(), decision_before)
        self.assertEqual(candidate.to_dict(), candidate_before)

        # Rejection: action != "SELECT"
        decision_no_trade = SelectionDecision(
            bar_index=10,
            timestamp=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id=make_decision_id(10, "NO_TRADE", None),
            action="NO_TRADE",
            reason="insufficient_score",
        )
        with self.assertRaises(ValueError):
            PendingExecutionIntent.from_selection_decision(decision_no_trade, "XAUUSD", "15m", 1.5)

    # --- Probe 15: Exact JSON round-trip ---
    def test_probe_15_json_round_trip(self) -> None:
        intent = _make_intent(supporting_strategy_ids=("smc_s05", "smc_s09"))
        d = intent.to_dict()
        j = json.dumps(d)
        d_parsed = json.loads(j)
        restored = PendingExecutionIntent.from_dict(d_parsed)
        self.assertEqual(intent.intent_id, restored.intent_id)
        self.assertEqual(intent.decision_id, restored.decision_id)
        self.assertEqual(intent.planned_entry, restored.planned_entry)
        self.assertEqual(intent.signal_sl, restored.signal_sl)
        self.assertEqual(intent.signal_tp, restored.signal_tp)
        self.assertEqual(intent.direction, restored.direction)
        self.assertEqual(intent.supporting_strategy_ids, restored.supporting_strategy_ids)
        self.assertEqual(intent.cluster_id, restored.cluster_id)
        self.assertEqual(intent.primary_setup_id, restored.primary_setup_id)
        self.assertEqual(intent.signal_bar_time, restored.signal_bar_time)

    def test_from_dict_rejections(self) -> None:
        valid_dict = _make_intent().to_dict()
        with self.assertRaises(StrictModelTypeError):
            PendingExecutionIntent.from_dict("not_a_dict")  # type: ignore[arg-type]

        # Missing key
        d_missing = copy.deepcopy(valid_dict)
        del d_missing["cluster_id"]
        with self.assertRaises(KeyError):
            PendingExecutionIntent.from_dict(d_missing)

        # Extra unknown key
        d_extra = copy.deepcopy(valid_dict)
        d_extra["extra"] = 1
        with self.assertRaises(KeyError):
            PendingExecutionIntent.from_dict(d_extra)

        # supporting_strategy_ids not a list in serialized dict
        d_bad_supporting = copy.deepcopy(valid_dict)
        d_bad_supporting["supporting_strategy_ids"] = "smc_s09"
        with self.assertRaises(StrictModelTypeError):
            PendingExecutionIntent.from_dict(d_bad_supporting)

    # --- Probe 19: Pending intent ID forgery rejected (P1 #5) ---
    def test_probe_19_pending_intent_id_forgery_rejected(self) -> None:
        cluster_id = make_cluster_id("BUY", "leg-a", "zone-a")
        setup_id = make_setup_id("smc_s01", "BUY", 10, cluster_id)
        canonical_decision = "sel:10:SELECT:smc_s01"
        canonical_intent = make_execution_intent_id(10, canonical_decision)

        # Forged intent_id rejected
        with self.assertRaises(ValueError) as ctx:
            PendingExecutionIntent(
                intent_id="bogus",
                decision_id=canonical_decision,
                symbol="XAUUSD",
                timeframe="15m",
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                action="SELECT",
                primary_strategy_id="smc_s01",
                primary_setup_id=setup_id,
                direction="BUY",
                planned_entry=2002.0,
                signal_sl=2000.0,
                signal_tp=2006.0,
                planned_rr=2.0,
                total_score=75.5,
                min_rr=1.5,
                cluster_id=cluster_id,
            )
        self.assertIn("does not match expected canonical", str(ctx.exception))

        # Forged decision_id rejected
        with self.assertRaises(ValueError) as ctx:
            PendingExecutionIntent(
                intent_id=canonical_intent,
                decision_id="bogus",
                symbol="XAUUSD",
                timeframe="15m",
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                action="SELECT",
                primary_strategy_id="smc_s01",
                primary_setup_id=setup_id,
                direction="BUY",
                planned_entry=2002.0,
                signal_sl=2000.0,
                signal_tp=2006.0,
                planned_rr=2.0,
                total_score=75.5,
                min_rr=1.5,
                cluster_id=cluster_id,
            )
        self.assertIn("does not match expected canonical", str(ctx.exception))

        # decision_id strategy mismatch rejected
        wrong_strat_decision = "sel:10:SELECT:smc_s05"
        with self.assertRaises(ValueError) as ctx:
            PendingExecutionIntent(
                intent_id=make_execution_intent_id(10, wrong_strat_decision),
                decision_id=wrong_strat_decision,
                symbol="XAUUSD",
                timeframe="15m",
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                action="SELECT",
                primary_strategy_id="smc_s01",
                primary_setup_id=setup_id,
                direction="BUY",
                planned_entry=2002.0,
                signal_sl=2000.0,
                signal_tp=2006.0,
                planned_rr=2.0,
                total_score=75.5,
                min_rr=1.5,
                cluster_id=cluster_id,
            )
        self.assertIn("does not match expected canonical", str(ctx.exception))

    # --- Probe 20: Pending intent planned geometry rejected (P2 #1) ---
    def test_probe_20_pending_intent_planned_geometry_rejected(self) -> None:
        # BUY: SL=110, Entry=100, TP=90 (inverted)
        with self.assertRaises(ValueError) as ctx:
            _make_intent(
                direction="BUY",
                planned_entry=100.0,
                signal_sl=110.0,
                signal_tp=90.0,
            )
        self.assertIn("Invalid BUY planned geometry", str(ctx.exception))

        # BUY: SL >= Entry
        with self.assertRaises(ValueError) as ctx:
            _make_intent(
                direction="BUY",
                planned_entry=100.0,
                signal_sl=100.0,
                signal_tp=110.0,
            )
        self.assertIn("Invalid BUY planned geometry", str(ctx.exception))

        # SELL: TP=110, Entry=100, SL=90 (inverted)
        with self.assertRaises(ValueError) as ctx:
            _make_intent(
                direction="SELL",
                planned_entry=100.0,
                signal_sl=90.0,
                signal_tp=110.0,
            )
        self.assertIn("Invalid SELL planned geometry", str(ctx.exception))

        # SELL: SL <= Entry
        with self.assertRaises(ValueError) as ctx:
            _make_intent(
                direction="SELL",
                planned_entry=100.0,
                signal_sl=100.0,
                signal_tp=90.0,
            )
        self.assertIn("Invalid SELL planned geometry", str(ctx.exception))

        # from_dict() with inverted geometry rejected
        valid_dict = _make_intent(direction="BUY", planned_entry=2002.0, signal_sl=2000.0, signal_tp=2006.0).to_dict()
        bad_dict = copy.deepcopy(valid_dict)
        bad_dict["planned_entry"] = 100.0
        bad_dict["signal_sl"] = 110.0
        bad_dict["signal_tp"] = 90.0
        with self.assertRaises(ValueError) as ctx:
            PendingExecutionIntent.from_dict(bad_dict)
        self.assertIn("Invalid BUY planned geometry", str(ctx.exception))


# =============================================================================
# 4. FillValidationGate Tests (T53.9.1 & Regression Probes 3, 4, 18)
# =============================================================================

class TestFillValidationGate(unittest.TestCase):
    """T53.9.1: Exhaustive tests for validate_fill pure function, precision, and regression probes."""

    def setUp(self) -> None:
        self.config = ExecutionConfig(
            point_value=0.01,
            lot_size=0.1,
            contract_size=100.0,
            spread_points=20.0,      # 0.20 USD
            commission_per_lot=5.0,  # 1.00 USD round-turn
            min_rr_fallback=1.5,
            allow_short=True,
        )

    # --- Probe 3: Cash-RR pass giả do rounding bị reject triệt để ---
    def test_probe_03_cash_rr_rounding_reject(self) -> None:
        """
        Regression Probe 3:
        Config: multiplier = 10, spread = 0.20, rt_commission = 1.00, min_rr = 1.50
        BUY signal: SL = 2000.00, Open = 2002.10 -> actual_entry = 2002.30
        risk_cash = (2002.30 - 2000.00) * 10 + 1.00 = 24.00 USD
        Set TP = 2005.999625 -> gross_reward = (2005.999625 - 2002.30) * 10 = 36.999625 USD
        reward_cash = 36.999625 - 1.00 = 35.999625 USD
        true_rr = 35.999625 / 24.00 = 1.499984375 < 1.50!
        Previously rounded to 1.5000 and passed; now MUST be rejected fail-closed.
        """
        intent = _make_intent(
            direction="BUY",
            planned_entry=2002.0,
            signal_sl=2000.0,
            signal_tp=2005.9999625,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=2002.10, config=self.config)

        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "insufficient_rr_at_fill")
        self.assertLess(res.effective_rr, 1.5)
        self.assertAlmostEqual(res.effective_rr, 1.499984375, places=8)

    # --- Probe 4: Exact RR 1.5 passes ---
    def test_probe_04_exact_rr_passes(self) -> None:
        """
        Regression Probe 4:
        Exact effective_rr == min_rr (1.50 == 1.50) must pass.
        Using exact binary fractions to avoid IEEE 754 precision noise:
        spread = 50 points * 0.01 = 0.50 USD
        commission_per_lot = 5.0 -> round_trip_commission = 1.00 USD
        multiplier = 0.1 * 100.0 = 10.0
        Open = 2002.00 -> actual_entry = 2002.50
        SL = 2000.00 -> risk_cash = (2002.50 - 2000.00) * 10 + 1.00 = 26.00 USD
        TP = 2006.50 -> reward_cash = (2006.50 - 2002.50) * 10 - 1.00 = 39.00 USD
        effective_rr = 39.00 / 26.00 = 1.50 (exact float equality)
        """
        config = ExecutionConfig(
            point_value=0.01,
            lot_size=0.1,
            contract_size=100.0,
            spread_points=50.0,      # 0.50 USD
            commission_per_lot=5.0,  # 1.00 USD round-trip
            min_rr_fallback=1.5,
            allow_short=True,
        )
        intent = _make_intent(
            direction="BUY",
            planned_entry=2002.0,
            signal_sl=2000.0,
            signal_tp=2006.5,
            min_rr=1.5,
        )
        res = validate_fill(intent, open_price=2002.00, config=config)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reason, "fill_ok")
        self.assertEqual(res.effective_rr, 1.50)

    # --- Probe 18: Purity - Input models are not mutated in validate_fill ---
    def test_probe_18_validate_fill_purity(self) -> None:
        intent = _make_intent()
        cfg = ExecutionConfig()
        intent_before = intent.to_dict()
        cfg_before = cfg.to_dict()

        _ = validate_fill(intent, open_price=2002.10, config=cfg)

        self.assertEqual(intent.to_dict(), intent_before)
        self.assertEqual(cfg.to_dict(), cfg_before)

    def test_buy_gap_above_tp_rejected(self) -> None:
        intent = _make_intent(
            direction="BUY",
            planned_entry=2002.0,
            signal_sl=2000.0,
            signal_tp=2006.0,
        )
        # Open is 2006.00 -> actual_entry = 2006.20 >= actual_tp 2006.00
        res = validate_fill(intent, open_price=2006.00, config=self.config)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "geometry_violation_at_fill")

    def test_sell_gap_below_tp_rejected(self) -> None:
        intent = _make_intent(
            direction="SELL",
            planned_entry=2000.0,
            signal_sl=2005.0,
            signal_tp=1990.0,
        )
        # Open is 1989.00 -> actual_entry = 1989.00 <= actual_tp 1990.20
        res = validate_fill(intent, open_price=1989.00, config=self.config)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "geometry_violation_at_fill")

    def test_sell_disabled_when_allow_short_false(self) -> None:
        cfg = ExecutionConfig(allow_short=False)
        intent = _make_intent(
            direction="SELL",
            planned_entry=2000.0,
            signal_sl=2005.0,
            signal_tp=1990.0,
        )
        res = validate_fill(intent, open_price=2000.00, config=cfg)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "short_disabled")
        self.assertEqual(res.risk_cash, 0.0)
        self.assertEqual(res.reward_cash, 0.0)
        self.assertEqual(res.effective_rr, 0.0)

    def test_commission_wipes_out_reward_cash(self) -> None:
        """Tiny reward distance wiped out by 1.00 USD commission."""
        intent = _make_intent(
            direction="BUY",
            planned_entry=2000.0,
            signal_sl=1990.0,
            signal_tp=2000.25,
            min_rr=0.1,
        )
        # Open 2000.00 -> actual_entry 2000.20, actual_tp 2000.25
        # gross_reward = (2000.25 - 2000.20) * 10 = 0.50 USD
        # reward_cash = 0.50 - 1.00 = -0.50 USD <= 0 -> rejected
        res = validate_fill(intent, open_price=2000.00, config=self.config)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.reason, "insufficient_rr_at_fill")
        self.assertEqual(res.effective_rr, 0.0)

    def test_numpy_scalar_inputs(self) -> None:
        """Verify numpy scalar inputs work seamlessly without errors."""
        intent = _make_intent()
        res = validate_fill(intent, open_price=np.float64(2002.10), config=self.config)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.reason, "fill_ok")


# =============================================================================
# 5. FillValidationResult Invariants Tests (T53.9.1 & Regression Probe 10)
# =============================================================================

class TestFillValidationResult(unittest.TestCase):
    """T53.9.1: Cross-field invariants and strictness for FillValidationResult."""

    # --- Probe 10: Contradictory fill validation results rejected ---
    def test_probe_10_contradictory_fill_result_rejected(self) -> None:
        # Probe from user specification:
        with self.assertRaises(ValueError):
            FillValidationResult(
                is_valid=True,
                reason="geometry_violation_at_fill",
                actual_entry=1.0,
                actual_sl=0.5,
                actual_tp=2.0,
                risk_cash=-1.0,
                reward_cash=-2.0,
                effective_rr=-3.0,
                spread=0.0,
                round_trip_commission=0.0,
            )

        # is_valid=True requires reason='fill_ok'
        with self.assertRaises(ValueError):
            FillValidationResult(
                is_valid=True,
                reason="insufficient_rr_at_fill",
                actual_entry=2000.0,
                actual_sl=1990.0,
                actual_tp=2010.0,
                risk_cash=10.0,
                reward_cash=10.0,
                effective_rr=1.0,
                spread=0.2,
                round_trip_commission=1.0,
            )

        # is_valid=False cannot have reason='fill_ok'
        with self.assertRaises(ValueError):
            FillValidationResult(
                is_valid=False,
                reason="fill_ok",
                actual_entry=2000.0,
                actual_sl=1990.0,
                actual_tp=2010.0,
                risk_cash=10.0,
                reward_cash=10.0,
                effective_rr=1.0,
                spread=0.2,
                round_trip_commission=1.0,
            )

        # fill_ok requires positive cash & RR
        with self.assertRaises(ValueError):
            FillValidationResult(
                is_valid=True,
                reason="fill_ok",
                actual_entry=2000.0,
                actual_sl=1990.0,
                actual_tp=2010.0,
                risk_cash=0.0,
                reward_cash=10.0,
                effective_rr=1.0,
                spread=0.2,
                round_trip_commission=1.0,
            )

        # geometry_violation_at_fill requires zero cash & RR
        with self.assertRaises(ValueError):
            FillValidationResult(
                is_valid=False,
                reason="geometry_violation_at_fill",
                actual_entry=2000.0,
                actual_sl=1990.0,
                actual_tp=2010.0,
                risk_cash=10.0,
                reward_cash=0.0,
                effective_rr=0.0,
                spread=0.2,
                round_trip_commission=1.0,
            )

        # Non-fill-gate reason rejected
        with self.assertRaises(ValueError):
            FillValidationResult(
                is_valid=False,
                reason="stop_loss",
                actual_entry=2000.0,
                actual_sl=1990.0,
                actual_tp=2010.0,
                risk_cash=0.0,
                reward_cash=0.0,
                effective_rr=0.0,
                spread=0.2,
                round_trip_commission=1.0,
            )

        # Strict types: bool in numeric field
        with self.assertRaises(StrictModelTypeError):
            FillValidationResult(
                is_valid=True,
                reason="fill_ok",
                actual_entry=True,  # type: ignore[arg-type]
                actual_sl=1990.0,
                actual_tp=2010.0,
                risk_cash=10.0,
                reward_cash=10.0,
                effective_rr=1.0,
                spread=0.2,
                round_trip_commission=1.0,
            )

    def test_json_round_trip(self) -> None:
        res = FillValidationResult(
            is_valid=True,
            reason="fill_ok",
            actual_entry=2002.30,
            actual_sl=2000.00,
            actual_tp=2006.00,
            risk_cash=24.0,
            reward_cash=36.0,
            effective_rr=1.5,
            spread=0.20,
            round_trip_commission=1.00,
        )
        d = res.to_dict()
        j = json.dumps(d)
        restored = FillValidationResult.from_dict(json.loads(j))
        self.assertEqual(res, restored)


# =============================================================================
# 6. ExecutionEvent Tests (T53.9.1 & Regression Probes 11, 12, 13, 14)
# =============================================================================

class TestExecutionEvent(unittest.TestCase):
    """T53.9.1: Tests for ExecutionEvent model, validations, and JSON round-trip."""

    def setUp(self) -> None:
        self.cluster_id = make_cluster_id("BUY", "leg-a", "zone-a")
        self.setup_id = make_setup_id("smc_s01", "BUY", 10, self.cluster_id)

    def test_valid_execution_event_and_round_trip(self) -> None:
        event_id = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id)
        evt = ExecutionEvent(
            event_version="1.0.0",
            event_id=event_id,
            event_type="ORDER_FILLED",
            reason="fill_ok",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=11,
            bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
            actual_entry=2002.30,
            actual_sl=2000.00,
            actual_tp=2006.00,
            risk_cash=24.0,
            reward_cash=36.0,
            effective_rr=1.5,
            exit_price=None,
            gross_pnl=None,
            net_pnl=None,
            cluster_id=self.cluster_id,
            meta={"spread": 0.20},
        )
        d = evt.to_dict()
        j = json.dumps(d)
        d_parsed = json.loads(j)
        restored = ExecutionEvent.from_dict(d_parsed)
        self.assertEqual(evt.event_id, restored.event_id)
        self.assertEqual(evt.event_type, restored.event_type)
        self.assertEqual(evt.actual_entry, restored.actual_entry)
        self.assertEqual(evt.effective_rr, restored.effective_rr)

    # --- Probe 11: Event type / reason mismatch rejected ---
    def test_probe_11_event_type_reason_mismatch_rejected(self) -> None:
        event_id = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id,
                event_type="ORDER_FILLED",
                reason="stop_loss",  # Invalid reason for ORDER_FILLED
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
            )
        self.assertIn("Invalid reason 'stop_loss' for event_type 'ORDER_FILLED'", str(ctx.exception))

    # --- Probe 12: Event with signal bar / time in future rejected ---
    def test_probe_12_signal_in_future_rejected(self) -> None:
        event_id = make_execution_event_id(10, "ORDER_FILLED", "smc_s01", self.setup_id)
        # signal_bar_index (11) > bar_index (10)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=10,
                bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                signal_bar_index=11,
                signal_bar_time=pd.Timestamp("2026-09-11 11:45:00+00:00"),
                decision_id="sel:11:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("cannot be greater than event bar_index", str(ctx.exception))

        # signal_bar_time > bar_time
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=10,
                bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("cannot be after event bar_time", str(ctx.exception))

    # --- Probe 13: ORDER_FILLED missing required fields rejected ---
    def test_probe_13_order_filled_missing_required_fields(self) -> None:
        event_id = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id)
        # Missing actual_entry
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=None,  # Missing!
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("ORDER_FILLED requires actual_entry", str(ctx.exception))

        # ORDER_FILLED cannot have exit field
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
                exit_price=2005.00,  # Invalid for filled!
            )
        self.assertIn("ORDER_FILLED cannot have exit field", str(ctx.exception))

    # --- Probe 14: Event ID collision and tamper rejected ---
    def test_probe_14_event_id_collision_and_tamper_rejected(self) -> None:
        other_setup_id = make_setup_id("smc_s01", "BUY", 10, make_cluster_id("BUY", "leg-b", "zone-b"))
        id_1 = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id)
        id_2 = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", other_setup_id)

        # Different setup yields different injective ID
        self.assertNotEqual(id_1, id_2)

        # Tampered ID rejected by constructor
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id="evt:11:ORDER_FILLED:smc_s01:tampered_id",
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("does not match expected canonical", str(ctx.exception))

    # --- Probe 21: Market-at-next-open timing locked (P1 #1) ---
    def test_probe_21_execution_event_exact_next_open_locked(self) -> None:
        # 1. Fill N+1 with bar_time == signal_bar_time (Close N == Open N+1) MUST PASS
        evt_equal_time = ExecutionEvent(
            event_version="1.0.0",
            event_id=make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id),
            event_type="ORDER_FILLED",
            reason="fill_ok",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=11,
            bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
            actual_entry=2002.30,
            actual_sl=2000.00,
            actual_tp=2006.00,
            risk_cash=24.0,
            reward_cash=36.0,
            effective_rr=1.5,
        )
        self.assertEqual(evt_equal_time.bar_index, 11)
        self.assertEqual(evt_equal_time.bar_time, evt_equal_time.signal_bar_time)

        # 2. Fill N+1 with weekend gap (bar_time > signal_bar_time) MUST PASS
        evt_gap_time = ExecutionEvent(
            event_version="1.0.0",
            event_id=make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id),
            event_type="ORDER_FILLED",
            reason="fill_ok",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=11,
            bar_time=pd.Timestamp("2026-09-14 00:00:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 21:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
            actual_entry=2002.30,
            actual_sl=2000.00,
            actual_tp=2006.00,
            risk_cash=24.0,
            reward_cash=36.0,
            effective_rr=1.5,
        )
        self.assertEqual(evt_gap_time.bar_index, 11)
        self.assertGreater(evt_gap_time.bar_time, evt_gap_time.signal_bar_time)

        # 3. Fill on same bar N (bar_index == signal_bar_index == 10) MUST REJECT
        event_id_10 = make_execution_event_id(10, "ORDER_FILLED", "smc_s01", self.setup_id)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id_10,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=10,
                bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("must occur exactly at next open bar", str(ctx.exception))

        # 4. Fill delayed at bar N+2 (bar_index == signal_bar_index + 2 == 12) MUST REJECT
        event_id_12 = make_execution_event_id(12, "ORDER_FILLED", "smc_s01", self.setup_id)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id_12,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=12,
                bar_time=pd.Timestamp("2026-09-11 12:30:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("must occur exactly at next open bar", str(ctx.exception))

        # 5. ORDER_REJECTED and ORDER_SKIPPED at N+1 with equal timestamp MUST PASS
        evt_rej_equal = ExecutionEvent(
            event_version="1.0.0",
            event_id=make_execution_event_id(11, "ORDER_REJECTED", "smc_s01", self.setup_id),
            event_type="ORDER_REJECTED",
            reason="insufficient_rr_at_fill",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=11,
            bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
            actual_entry=2002.30,
            actual_sl=2000.00,
            actual_tp=2006.00,
        )
        self.assertEqual(evt_rej_equal.bar_index, 11)

        evt_skip_equal = ExecutionEvent(
            event_version="1.0.0",
            event_id=make_execution_event_id(11, "ORDER_SKIPPED", "smc_s01", self.setup_id),
            event_type="ORDER_SKIPPED",
            reason="short_disabled",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=11,
            bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
        )
        self.assertEqual(evt_skip_equal.bar_index, 11)

        # 6. POSITION_CLOSED intrabar at N+1 with equal timestamp MUST PASS
        evt_closed_equal = ExecutionEvent(
            event_version="1.0.0",
            event_id=make_execution_event_id(11, "POSITION_CLOSED", "smc_s01", self.setup_id),
            event_type="POSITION_CLOSED",
            reason="stop_loss",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=11,
            bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
            actual_entry=2002.30,
            actual_sl=2000.00,
            actual_tp=2006.00,
            exit_price=1999.80,
            gross_pnl=-25.0,
            net_pnl=-26.0,
        )
        self.assertEqual(evt_closed_equal.bar_index, 11)
        self.assertEqual(evt_closed_equal.bar_time, evt_closed_equal.signal_bar_time)

        # 7. POSITION_CLOSED before N+1 (bar_index == signal_bar_index == 10) MUST REJECT
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=make_execution_event_id(10, "POSITION_CLOSED", "smc_s01", self.setup_id),
                event_type="POSITION_CLOSED",
                reason="stop_loss",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=10,
                bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                exit_price=1999.80,
                gross_pnl=-25.0,
                net_pnl=-26.0,
            )
        self.assertIn("must be >= signal_bar_index + 1", str(ctx.exception))

        # 8. ORDER_CANCELLED with reason 'no_next_bar' must have bar_time == signal_bar_time
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=make_execution_event_id(10, "ORDER_CANCELLED", "smc_s01", self.setup_id),
                event_type="ORDER_CANCELLED",
                reason="no_next_bar",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=10,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),  # Different from signal_bar_time!
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
            )
        self.assertIn("ORDER_CANCELLED with reason 'no_next_bar' must have bar_time", str(ctx.exception))

        evt_cancelled_ok = ExecutionEvent(
            event_version="1.0.0",
            event_id=make_execution_event_id(10, "ORDER_CANCELLED", "smc_s01", self.setup_id),
            event_type="ORDER_CANCELLED",
            reason="no_next_bar",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=10,
            bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
        )
        self.assertEqual(evt_cancelled_ok.bar_index, 10)
        self.assertEqual(evt_cancelled_ok.bar_time, evt_cancelled_ok.signal_bar_time)

        # ORDER_PENDING at bar N+1 -> rejected (must be at bar N)
        pending_id_11 = make_execution_event_id(11, "ORDER_PENDING", "smc_s01", self.setup_id)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=pending_id_11,
                event_type="ORDER_PENDING",
                reason="pending",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
            )
        self.assertIn("ORDER_PENDING must have bar_index (11) == signal_bar_index (10)", str(ctx.exception))

    # --- Probe 22: ExecutionEvent identity cross-checks rejected (P1 #3) ---
    def test_probe_22_execution_event_identity_cross_checks_rejected(self) -> None:
        # Event declares strategy_id="smc_s01", direction="BUY" but setup_id is for smc_s05
        s05_setup = make_setup_id("smc_s05", "BUY", 10, self.cluster_id)
        event_id = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", s05_setup)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=s05_setup,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("does not match primary_strategy_id", str(ctx.exception))

        # Event declares BUY but setup_id is for SELL
        sell_cluster = make_cluster_id("SELL", "leg-a", "zone-a")
        sell_setup = make_setup_id("smc_s01", "SELL", 10, sell_cluster)
        event_id_sell = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", sell_setup)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=event_id_sell,
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=sell_setup,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("does not match direction", str(ctx.exception))

        # decision_id mismatch with strategy_id
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id),
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s05",  # S05 instead of smc_s01
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
            )
        self.assertIn("does not match expected canonical", str(ctx.exception))

        # cluster_id direction mismatch
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(
                event_version="1.0.0",
                event_id=make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id),
                event_type="ORDER_FILLED",
                reason="fill_ok",
                symbol="XAUUSD",
                timeframe="15m",
                bar_index=11,
                bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
                signal_bar_index=10,
                signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
                decision_id="sel:10:SELECT:smc_s01",
                strategy_id="smc_s01",
                setup_id=self.setup_id,
                direction="BUY",
                planned_entry=2002.0,
                planned_sl=2000.0,
                planned_tp=2006.0,
                actual_entry=2002.30,
                actual_sl=2000.00,
                actual_tp=2006.00,
                risk_cash=24.0,
                reward_cash=36.0,
                effective_rr=1.5,
                cluster_id="SELL:leg-a:zone-a",
            )
        self.assertIn("direction does not match event direction", str(ctx.exception))

    # --- Probe 23: ORDER_FILLED accounting and geometry validation (P1 #4 & P2 #3) ---
    def test_probe_23_order_filled_accounting_and_geometry_rejected(self) -> None:
        event_id = make_execution_event_id(11, "ORDER_FILLED", "smc_s01", self.setup_id)
        base_kwargs = dict(
            event_version="1.0.0",
            event_id=event_id,
            event_type="ORDER_FILLED",
            reason="fill_ok",
            symbol="XAUUSD",
            timeframe="15m",
            bar_index=11,
            bar_time=pd.Timestamp("2026-09-11 12:15:00+00:00"),
            signal_bar_index=10,
            signal_bar_time=pd.Timestamp("2026-09-11 12:00:00+00:00"),
            decision_id="sel:10:SELECT:smc_s01",
            strategy_id="smc_s01",
            setup_id=self.setup_id,
            direction="BUY",
            planned_entry=2002.0,
            planned_sl=2000.0,
            planned_tp=2006.0,
            actual_entry=2002.30,
            actual_sl=2000.00,
            actual_tp=2006.00,
        )

        # Negative risk_cash=-1, reward_cash=-2, effective_rr=-2 rejected
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(**base_kwargs, risk_cash=-1.0, reward_cash=-2.0, effective_rr=-2.0)
        self.assertIn("ORDER_FILLED requires risk_cash > 0", str(ctx.exception))

        # Zero risk_cash rejected
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(**base_kwargs, risk_cash=0.0, reward_cash=36.0, effective_rr=1.5)
        self.assertIn("ORDER_FILLED requires risk_cash > 0", str(ctx.exception))

        # Zero reward_cash rejected
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(**base_kwargs, risk_cash=24.0, reward_cash=0.0, effective_rr=0.0)
        self.assertIn("ORDER_FILLED requires reward_cash > 0", str(ctx.exception))

        # RR does not equal reward / risk (24 cash risk, 36 reward, but effective_rr passed as 2.0)
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(**base_kwargs, risk_cash=24.0, reward_cash=36.0, effective_rr=2.0)
        self.assertIn("does not match reward_cash / risk_cash", str(ctx.exception))

        # P2 #3: Tightened RR consistency tolerance (1e-12): 1.5009 vs 1.5 rejected
        with self.assertRaises(ValueError) as ctx:
            ExecutionEvent(**base_kwargs, risk_cash=24.0, reward_cash=36.0, effective_rr=1.5009)
        self.assertIn("does not match reward_cash / risk_cash", str(ctx.exception))

        # Exact effective_rr passes and round-trips cleanly
        evt_valid = ExecutionEvent(**base_kwargs, risk_cash=24.0, reward_cash=36.0, effective_rr=1.5)
        d = evt_valid.to_dict()
        j = json.dumps(d)
        restored = ExecutionEvent.from_dict(json.loads(j))
        self.assertEqual(evt_valid.effective_rr, restored.effective_rr)

        # Value generated directly by validate_fill() passes
        cfg = ExecutionConfig(spread_points=20.0, commission_per_lot=5.0, min_rr_fallback=1.5)
        intent = _make_intent(direction="BUY", planned_entry=2002.0, signal_sl=2000.0, signal_tp=2006.0, min_rr=1.5)
        fill_res = validate_fill(intent, open_price=2002.10, config=cfg)
        self.assertTrue(fill_res.is_valid)
        evt_from_gate = ExecutionEvent(
            **base_kwargs,
            risk_cash=fill_res.risk_cash,
            reward_cash=fill_res.reward_cash,
            effective_rr=fill_res.effective_rr,
        )
        self.assertEqual(evt_from_gate.effective_rr, fill_res.effective_rr)

        # BUY actual geometry: actual_sl >= actual_entry rejected
        with self.assertRaises(ValueError) as ctx:
            bad_kw = copy.deepcopy(base_kwargs)
            bad_kw["actual_sl"] = 2003.0  # SL > Entry
            ExecutionEvent(**bad_kw, risk_cash=24.0, reward_cash=36.0, effective_rr=1.5)
        self.assertIn("Invalid BUY actual geometry", str(ctx.exception))

        # BUY actual geometry: actual_entry >= actual_tp rejected
        with self.assertRaises(ValueError) as ctx:
            bad_kw = copy.deepcopy(base_kwargs)
            bad_kw["actual_entry"] = 2007.0  # Entry > TP
            ExecutionEvent(**bad_kw, risk_cash=24.0, reward_cash=36.0, effective_rr=1.5)
        self.assertIn("Invalid BUY actual geometry", str(ctx.exception))

        # Planned geometry violation across all events rejected
        with self.assertRaises(ValueError) as ctx:
            bad_kw = copy.deepcopy(base_kwargs)
            bad_kw["planned_sl"] = 2005.0  # planned_sl > planned_entry
            ExecutionEvent(**bad_kw, risk_cash=24.0, reward_cash=36.0, effective_rr=1.5)
        self.assertIn("Invalid BUY planned geometry", str(ctx.exception))


# =============================================================================
# 7. CooldownBook Tests (T53.9.1: Bounded Online State, Non-Bridging, Schema)
# =============================================================================

class TestCooldownBook(unittest.TestCase):
    """
    T53.9.1: Bounded online CooldownBook tests.
    Covers all 14 mandatory regression contracts:
    1. Original & restored inactive before start
    2. Original & restored active in [start, expiry)
    3. Original & restored inactive from expiry onwards
    4. Two separate cooldown intervals do NOT bridge gap
    5. Stale fills (new_start < existing_start) do not alter state
    6. Duplicate fills (new_start == existing_start) do not shorten expiry
    7. Newer fill during active cooldown extends expiry without shortening
    8. Newer fill after previous cooldown expired starts fresh interval without bridging gap
    9. K=0 is strict no-op
    10. Snapshot missing or extra fields rejected fail-closed
    11. Legacy integer/list/tuple snapshot rejected
    12. start == expiry and start > expiry rejected
    13. Exact JSON round-trip preserves state and behavior
    14. reset() restores book to empty
    """

    def test_01_cooldown_original_and_restored_lifecycle_boundaries(self) -> None:
        """1, 2, 3: Original and restored active strictly in [start, expiry), inactive before and after."""
        book = CooldownBook()
        # Fill at bar 10 with K=5 -> [10, 15)
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=5)

        snap = book.snapshot()
        self.assertEqual(snap["smc_s01:BUY"], {"start": 10, "expiry": 15})

        restored = CooldownBook.from_snapshot(json.loads(json.dumps(snap)))

        # 1. Inactive before start
        for b in (0, 8, 9):
            self.assertFalse(book.is_active("smc_s01", "BUY", current_bar_index=b))
            self.assertEqual(book.get_cooldown_remaining("smc_s01", "BUY", current_bar_index=b), 0)
            self.assertFalse(restored.is_active("smc_s01", "BUY", current_bar_index=b))
            self.assertEqual(restored.get_cooldown_remaining("smc_s01", "BUY", current_bar_index=b), 0)

        # 2. Active strictly in [start, expiry)
        for b in range(10, 15):
            self.assertTrue(book.is_active("smc_s01", "BUY", current_bar_index=b))
            self.assertEqual(book.get_cooldown_remaining("smc_s01", "BUY", current_bar_index=b), 15 - b)
            self.assertTrue(restored.is_active("smc_s01", "BUY", current_bar_index=b))
            self.assertEqual(restored.get_cooldown_remaining("smc_s01", "BUY", current_bar_index=b), 15 - b)

        # 3. Inactive from expiry onwards
        for b in (15, 16, 20):
            self.assertFalse(book.is_active("smc_s01", "BUY", current_bar_index=b))
            self.assertEqual(book.get_cooldown_remaining("smc_s01", "BUY", current_bar_index=b), 0)
            self.assertFalse(restored.is_active("smc_s01", "BUY", current_bar_index=b))
            self.assertEqual(restored.get_cooldown_remaining("smc_s01", "BUY", current_bar_index=b), 0)

    def test_02_two_separate_cooldowns_do_not_bridge_gap(self) -> None:
        """4, 8: Two separate cooldowns [10, 12) and [20, 22) do NOT bridge gap (bar 15 and 19 inactive)."""
        book = CooldownBook()
        # Fill 10, K=2 -> [10, 12)
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=2)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 12})

        # Fill 20, K=2 -> [20, 22) (fill happens after previous cooldown expired at 12 <= 20)
        book.record_fill("smc_s01", "BUY", fill_bar_index=20, cooldown_bars=2)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 20, "expiry": 22})

        # Gap verification: bars 15 and 19 MUST be inactive!
        self.assertFalse(book.is_active("smc_s01", "BUY", current_bar_index=15))
        self.assertFalse(book.is_active("smc_s01", "BUY", current_bar_index=19))

        # New interval verification: bars 20 and 21 MUST be active, bar 22 inactive
        self.assertTrue(book.is_active("smc_s01", "BUY", current_bar_index=20))
        self.assertTrue(book.is_active("smc_s01", "BUY", current_bar_index=21))
        self.assertFalse(book.is_active("smc_s01", "BUY", current_bar_index=22))

    def test_03_stale_fill_does_not_alter_newer_state(self) -> None:
        """5: Stale fill (new_start < existing_start) is ignored and does not alter newer state."""
        book = CooldownBook()
        book.record_fill("smc_s01", "BUY", fill_bar_index=20, cooldown_bars=5)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 20, "expiry": 25})

        # Stale fill at bar 15 with K=10 (expiry 25)
        book.record_fill("smc_s01", "BUY", fill_bar_index=15, cooldown_bars=10)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 20, "expiry": 25})

        # Stale fill at bar 10 with K=2
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=2)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 20, "expiry": 25})

    def test_04_duplicate_fill_does_not_shorten_expiry(self) -> None:
        """6: Duplicate/retry fill (new_start == existing_start) does not shorten expiry."""
        book = CooldownBook()
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=5)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 15})

        # Duplicate fill with smaller K=2 -> expiry remains 15
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=2)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 15})

        # Duplicate fill with same K=5 -> expiry remains 15
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=5)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 15})

        # Duplicate fill with larger K=8 -> expiry extended to 18
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=8)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 18})

    def test_05_newer_fill_during_active_cooldown_extends_expiry(self) -> None:
        """7: Fill during active cooldown preserves or extends expiry without shortening."""
        book = CooldownBook()
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=10)  # [10, 20)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 20})

        # Fill at bar 12 with K=2 (new_expiry 14 < 20) -> start moves to 12, expiry stays 20!
        book.record_fill("smc_s01", "BUY", fill_bar_index=12, cooldown_bars=2)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 12, "expiry": 20})

        # Fill at bar 15 with K=10 (new_expiry 25 > 20) -> start moves to 15, expiry extends to 25!
        book.record_fill("smc_s01", "BUY", fill_bar_index=15, cooldown_bars=10)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 15, "expiry": 25})

    def test_06_k_zero_is_noop(self) -> None:
        """9: K=0 is a strict no-op on empty book and existing book."""
        book = CooldownBook()
        # On empty book: no-op, snapshot remains {}
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=0)
        self.assertEqual(book.snapshot(), {})
        self.assertFalse(book.is_active("smc_s01", "BUY", current_bar_index=10))

        # On non-empty book: does not erase or shorten existing cooldown
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=5)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 15})

        book.record_fill("smc_s01", "BUY", fill_bar_index=14, cooldown_bars=0)
        self.assertEqual(book.snapshot()["smc_s01:BUY"], {"start": 10, "expiry": 15})

    def test_07_from_snapshot_rejections(self) -> None:
        """10, 11, 12: from_snapshot rejects missing/extra keys, legacy forms, and invalid bounds."""
        # Non-dict outer snapshot
        with self.assertRaises(StrictModelTypeError):
            CooldownBook.from_snapshot("not_a_dict")  # type: ignore[arg-type]

        # Bad key syntax
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01_BUY": {"start": 10, "expiry": 15}})

        # Invalid direction
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:HOLD": {"start": 10, "expiry": 15}})

        # 11. Legacy integer value rejected
        with self.assertRaises(StrictModelTypeError):
            CooldownBook.from_snapshot({"smc_s01:BUY": 15})

        # 11. Legacy list/tuple value rejected
        with self.assertRaises(StrictModelTypeError):
            CooldownBook.from_snapshot({"smc_s01:BUY": [10, 15]})

        with self.assertRaises(StrictModelTypeError):
            CooldownBook.from_snapshot({"smc_s01:BUY": (10, 15)})

        # 10. Missing field ("start" missing)
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"expiry": 15}})

        # 10. Missing field ("expiry" missing)
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": 10}})

        # 10. Extra unexpected field
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": 10, "expiry": 15, "unexpected": 999}})

        # Negative start
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": -1, "expiry": 15}})

        # Negative expiry
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": 10, "expiry": -5}})

        # Boolean in start or expiry
        with self.assertRaises(StrictModelTypeError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": True, "expiry": 15}})

        with self.assertRaises(StrictModelTypeError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": 10, "expiry": True}})

        # 12. start == expiry rejected
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": 15, "expiry": 15}})

        # 12. start > expiry rejected
        with self.assertRaises(ValueError):
            CooldownBook.from_snapshot({"smc_s01:BUY": {"start": 20, "expiry": 15}})

    def test_08_snapshot_json_roundtrip_exact(self) -> None:
        """13: Snapshot JSON round-trip preserves exact state and behavior."""
        book = CooldownBook()
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=5)
        book.record_fill("smc_s05", "SELL", fill_bar_index=12, cooldown_bars=3)

        snap = book.snapshot()
        self.assertEqual(snap["smc_s01:BUY"], {"start": 10, "expiry": 15})
        self.assertEqual(snap["smc_s05:SELL"], {"start": 12, "expiry": 15})

        j = json.dumps(snap)
        restored = CooldownBook.from_snapshot(json.loads(j))

        self.assertEqual(snap, restored.snapshot())
        self.assertFalse(restored.is_active("smc_s01", "BUY", 9))
        self.assertTrue(restored.is_active("smc_s01", "BUY", 10))
        self.assertTrue(restored.is_active("smc_s01", "BUY", 14))
        self.assertFalse(restored.is_active("smc_s01", "BUY", 15))

    def test_09_cooldown_isolation_by_strategy_and_direction(self) -> None:
        """Cooldown on S01 BUY does not affect S01 SELL or S05 BUY."""
        book = CooldownBook()
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=5)

        self.assertFalse(book.is_active("smc_s01", "SELL", current_bar_index=10))
        self.assertFalse(book.is_active("smc_s05", "BUY", current_bar_index=10))

    def test_10_reset(self) -> None:
        """14: reset() restores book to completely empty state."""
        book = CooldownBook()
        book.record_fill("smc_s01", "BUY", fill_bar_index=10, cooldown_bars=5)
        book.reset()
        self.assertEqual(book.snapshot(), {})
        self.assertFalse(book.is_active("smc_s01", "BUY", current_bar_index=10))


if __name__ == "__main__":
    unittest.main()
