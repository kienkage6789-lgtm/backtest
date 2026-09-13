"""
tests/test_smc_engine_eligibility.py
====================================
Exhaustive suite for Eligibility Gate and 30-cell Regime Matrix (T53.7).
Covers Group E: Tests 61 through 116 (56 tests!).
"""

from __future__ import annotations

import copy
import datetime
import math
import unittest
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

from smc.engine.errors import StrategyStateError, StrategyValidationError
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    MarketRegime,
    StrategyContext,
    StrategyEvaluation,
    StrategyProfile,
)
from smc.engine.eligibility import (
    CANONICAL_REASON_CODES,
    REGIME_MATRIX,
    EligibilityGate,
    get_regime_matrix_score,
)


def _bar_time(bar_index: int, base_iso: str = "2024-05-15T14:00:00+00:00") -> tuple[pd.Timestamp, pd.Timestamp]:
    base = pd.Timestamp(base_iso)
    open_t = base + pd.Timedelta(minutes=bar_index)
    close_t = open_t + pd.Timedelta(minutes=1)
    return open_t, close_t


def _make_evidence(
    kind: str = "liquidity_sweep",
    bar_index: int = 5,
    evidence_id: Optional[str] = None,
    price: float = 2035.0,
    time: Optional[pd.Timestamp] = None,
) -> EvidenceRef:
    eid = evidence_id if evidence_id is not None else f"{kind}_{bar_index}"
    t = time if time is not None else _bar_time(bar_index)[0]
    return EvidenceRef(
        evidence_id=eid,
        kind=kind,  # type: ignore
        bar_index=bar_index,
        price=price,
        time=t,
    )


def _make_candidate(
    strategy_id: str = "S01",
    direction: str = "BUY",
    bar_index: int = 8,
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    planned_rr: float = 2.0,
    expiry_bar: Optional[int] = None,
    evidences: Optional[Sequence[EvidenceRef]] = None,
    meta: Optional[Mapping[str, Any]] = None,
    timestamp: Optional[pd.Timestamp] = None,
) -> CandidateSetup:
    open_ts, _ = _bar_time(bar_index)
    ts = timestamp if timestamp is not None else open_ts
    exp_bar = expiry_bar if expiry_bar is not None else bar_index + 7
    if direction == "BUY":
        ep = entry_price if entry_price is not None else 2040.0
        sl = stop_loss if stop_loss is not None else 2030.0
        tp = take_profit if take_profit is not None else 2060.0
    else:
        ep = entry_price if entry_price is not None else 2040.0
        sl = stop_loss if stop_loss is not None else 2050.0
        tp = take_profit if take_profit is not None else 2020.0

    if evidences is None:
        if strategy_id == "S01":
            evs = (
                _make_evidence("liquidity_sweep", 2),
                _make_evidence("fair_value_gap", 4),
                _make_evidence("structure_event", 6),
            )
        elif strategy_id == "S05":
            evs = (
                _make_evidence("structure_event", 5),
                _make_evidence("order_block", 3),
            )
        else:  # S09
            evs = (
                _make_evidence("liquidity_sweep", 2),
                _make_evidence("fair_value_gap", 4),
                _make_evidence("structure_event", 6),
            )
    else:
        evs = tuple(evidences)

    if meta is not None:
        m = dict(meta)
    else:
        m = {}
        if strategy_id == "S09":
            w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
            w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
            w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")
            close_ts = _bar_time(bar_index)[1]
            m["window_name"] = "silver_bullet_ny_am"
            m["local_date"] = "2024-05-15"
            m["window_start_utc"] = w_start.isoformat()
            m["window_end_utc"] = w_end.isoformat()
            m["grace_expiry_utc"] = w_grace.isoformat()
            m["signal_bar_close_time"] = close_ts.isoformat()
            m["window_key"] = ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()]

    return CandidateSetup(
        setup_id=f"{strategy_id}:{direction}:{bar_index}",
        strategy_id=strategy_id,
        direction=direction,  # type: ignore
        bar_index=bar_index,
        timestamp=ts,
        entry_price=ep,
        stop_loss=sl,
        take_profit=tp,
        planned_rr=planned_rr,
        evidences=evs,
        evidence_cluster_id=f"{direction}:leg1:zone1",
        expiry_bar=exp_bar,
        meta=m,
    )


def _make_context(
    bar_index: int = 8,
    close: float = 2040.0,
    bias: Optional[str] = "bullish",
    timeframe: str = "M1",
) -> StrategyContext:
    ts, bct = _bar_time(bar_index)
    hb = (
        BiasStateSnapshot(
            bias=bias,  # type: ignore
            timestamp=ts,
            source_event_index=bar_index,
            source_event_time=ts,
            as_of=ts,
        )
        if bias is not None
        else None
    )
    return StrategyContext(
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=bct,
        symbol="XAUUSD",
        timeframe=timeframe,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0,
        atr14=2.0,
        htf_bias=hb,
    )


def _make_regime(
    regime: str = "bullish_trend",
    bar_index: int = 8,
) -> MarketRegime:
    _, bct = _bar_time(bar_index)
    return MarketRegime(
        regime=regime,  # type: ignore
        bar_index=bar_index,
        timestamp=bct,
        efficiency_ratio=0.50,
        atr_percentile=50.0,
    )


def _make_profile(
    strategy_id: str = "S01",
    allowed_directions: Sequence[str] = ("BUY", "SELL"),
    timeframes: Sequence[str] = ("M1", "M5", "M15"),
    min_rr: float = 1.5,
) -> StrategyProfile:
    return StrategyProfile(
        strategy_id=strategy_id,
        name=f"Strategy {strategy_id}",
        allowed_directions=tuple(allowed_directions),  # type: ignore
        timeframes=tuple(timeframes),
        min_rr=min_rr,
    )


class TestSMCEngineEligibility(unittest.TestCase):
    """56-test exhaustive suite for EligibilityGate & 30-cell matrix."""

    def setUp(self):
        self.gate = EligibilityGate()

    # =========================================================================
    # Tests 61-90: All 30 cells in REGIME_MATRIX exact score and ALLOW/REJECT
    # =========================================================================

    def test_61_to_90_all_30_matrix_cells(self):
        expected_scores = {
            # S05
            ("S05", "BUY", "bullish_trend"): 100.0,
            ("S05", "BUY", "bearish_trend"): 0.0,
            ("S05", "BUY", "volatile_reversal"): 40.0,
            ("S05", "BUY", "ranging"): 60.0,
            ("S05", "BUY", "uncertain"): 30.0,

            ("S05", "SELL", "bullish_trend"): 0.0,
            ("S05", "SELL", "bearish_trend"): 100.0,
            ("S05", "SELL", "volatile_reversal"): 40.0,
            ("S05", "SELL", "ranging"): 60.0,
            ("S05", "SELL", "uncertain"): 30.0,

            # S01
            ("S01", "BUY", "bullish_trend"): 75.0,
            ("S01", "BUY", "bearish_trend"): 0.0,
            ("S01", "BUY", "volatile_reversal"): 100.0,
            ("S01", "BUY", "ranging"): 60.0,
            ("S01", "BUY", "uncertain"): 30.0,

            ("S01", "SELL", "bullish_trend"): 0.0,
            ("S01", "SELL", "bearish_trend"): 75.0,
            ("S01", "SELL", "volatile_reversal"): 100.0,
            ("S01", "SELL", "ranging"): 60.0,
            ("S01", "SELL", "uncertain"): 30.0,

            # S09
            ("S09", "BUY", "bullish_trend"): 80.0,
            ("S09", "BUY", "bearish_trend"): 0.0,
            ("S09", "BUY", "volatile_reversal"): 100.0,
            ("S09", "BUY", "ranging"): 60.0,
            ("S09", "BUY", "uncertain"): 30.0,

            ("S09", "SELL", "bullish_trend"): 0.0,
            ("S09", "SELL", "bearish_trend"): 80.0,
            ("S09", "SELL", "volatile_reversal"): 100.0,
            ("S09", "SELL", "ranging"): 60.0,
            ("S09", "SELL", "uncertain"): 30.0,
        }
        self.assertEqual(len(expected_scores), 30)

        for (sid, direction, reg_name), expected_score in expected_scores.items():
            with self.subTest(strategy=sid, direction=direction, regime=reg_name):
                # 1. Lookup in matrix
                score = get_regime_matrix_score(sid, direction, reg_name)
                self.assertEqual(score, expected_score)

                # 2. Evaluate candidate setup through gate
                bias_dir = "bullish" if direction == "BUY" else "bearish"
                cand = _make_candidate(strategy_id=sid, direction=direction)
                context = _make_context(bar_index=8, bias=bias_dir)
                regime = _make_regime(regime=reg_name, bar_index=8)
                prof = _make_profile(strategy_id=sid)

                evaluation = self.gate.evaluate(cand, context, regime, prof)
                self.assertEqual(evaluation.regime_score, expected_score)

                if expected_score == 0.0:
                    self.assertEqual(evaluation.status, "REJECTED")
                    self.assertIn("wrong_regime", evaluation.rejection_reasons)
                else:
                    # Non-zero score should be ELIGIBLE (all other conditions met)
                    self.assertEqual(evaluation.status, "ELIGIBLE")
                    self.assertEqual(len(evaluation.rejection_reasons), 0)

    # =========================================================================
    # Group E: Gate Checks & Boundary Tests (Tests 91-116)
    # =========================================================================

    def test_91_unknown_strategy_fails_closed(self):
        with self.assertRaises(StrategyValidationError):
            get_regime_matrix_score("UNKNOWN", "BUY", "bullish_trend")

    def test_92_regime_context_bar_mismatch(self):
        cand = _make_candidate()
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=9)  # Mismatch!
        prof = _make_profile()
        with self.assertRaises(StrategyStateError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_93_regime_context_timestamp_mismatch(self):
        cand = _make_candidate()
        context = _make_context(bar_index=8)
        # Regime timestamp different from context bar_close_time
        regime = MarketRegime(
            regime="bullish_trend",
            bar_index=8,
            timestamp=pd.Timestamp("2024-05-15 15:00:00+00:00"),
            efficiency_ratio=0.5,
            atr_percentile=50.0,
        )
        prof = _make_profile()
        with self.assertRaises(StrategyStateError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_94_mapping_profile_candidate_strategy_mismatch(self):
        cand = _make_candidate(strategy_id="S01")
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile(strategy_id="S05")  # Mismatch!
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_95_direction_not_in_profile_allowed(self):
        cand = _make_candidate(direction="SELL")
        context = _make_context(bar_index=8, bias="bearish")
        regime = _make_regime(regime="bearish_trend", bar_index=8)
        prof = _make_profile(allowed_directions=("BUY",))  # SELL not allowed!
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_96_timeframe_not_in_profile_timeframes(self):
        cand = _make_candidate()
        context = _make_context(bar_index=8, timeframe="H4")  # H4 not supported!
        regime = _make_regime(bar_index=8)
        prof = _make_profile(timeframes=("M1", "M5"))
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_97_candidate_future_bar_or_time_raises(self):
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile()

        # Future bar index
        cand_future_bar = _make_candidate(bar_index=9)
        with self.assertRaises(StrategyStateError):
            self.gate.evaluate(cand_future_bar, context, regime, prof)

        # Future timestamp
        future_ts = context.bar_close_time + pd.Timedelta(minutes=5)
        cand_future_ts = _make_candidate(bar_index=8, timestamp=future_ts)
        with self.assertRaises(StrategyStateError):
            self.gate.evaluate(cand_future_ts, context, regime, prof)

    def test_98_future_evidence_bar_or_time_raises(self):
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile()

        # Evidence with future bar index
        future_ev = _make_evidence("liquidity_sweep", bar_index=9)
        cand = _make_candidate(bar_index=8, evidences=[future_ev, _make_evidence("fair_value_gap", 4), _make_evidence("structure_event", 6)])
        with self.assertRaises(StrategyStateError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_99_s01_required_evidence_missing(self):
        # Missing fair_value_gap
        evs = (_make_evidence("liquidity_sweep", 2), _make_evidence("structure_event", 6))
        cand = _make_candidate(strategy_id="S01", evidences=evs)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S01")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("missing_required_evidence", eval_res.rejection_reasons)

    def test_100_s05_required_evidence_missing(self):
        # Missing order_block
        evs = (_make_evidence("structure_event", 5),)
        cand = _make_candidate(strategy_id="S05", evidences=evs)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S05")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("missing_required_evidence", eval_res.rejection_reasons)

    def test_101_s09_required_evidence_or_window_meta_missing(self):
        # Missing window metadata
        cand = _make_candidate(strategy_id="S09", meta={"other_key": 123})
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("missing_required_evidence", eval_res.rejection_reasons)

    def test_102_s01_s09_event_ordering_valid_passes(self):
        # sweep (2) <= FVG (4) < MSS (6) < candidate (8)
        evs = (
            _make_evidence("liquidity_sweep", 2),
            _make_evidence("fair_value_gap", 4),
            _make_evidence("structure_event", 6),
        )
        cand = _make_candidate(strategy_id="S01", bar_index=8, evidences=evs)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S01")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "ELIGIBLE")

    def test_103_invalid_ordering_reason(self):
        # MSS (4) before FVG (6) -> Invalid order!
        evs = (
            _make_evidence("liquidity_sweep", 2),
            _make_evidence("structure_event", 4),
            _make_evidence("fair_value_gap", 6),
        )
        cand = _make_candidate(strategy_id="S01", bar_index=8, evidences=evs)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S01")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("invalid_event_order", eval_res.rejection_reasons)

    def test_104_missing_htf_bias(self):
        cand = _make_candidate()
        context = _make_context(bar_index=8, bias=None)  # None HTF bias!
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S01")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("missing_required_evidence", eval_res.rejection_reasons)

    def test_105_opposed_bias_all_strategies(self):
        for sid in ("S01", "S05", "S09"):
            with self.subTest(strategy=sid):
                # BUY candidate with bearish bias
                cand = _make_candidate(strategy_id=sid, direction="BUY")
                context = _make_context(bar_index=8, bias="bearish")
                regime = _make_regime(regime="ranging", bar_index=8)
                prof = _make_profile(sid)

                eval_res = self.gate.evaluate(cand, context, regime, prof)
                self.assertEqual(eval_res.status, "REJECTED")
                self.assertIn("htf_bias_mismatch", eval_res.rejection_reasons)

    def test_106_neutral_bias_s05_rejects(self):
        cand = _make_candidate(strategy_id="S05", direction="BUY")
        context = _make_context(bar_index=8, bias="neutral")
        regime = _make_regime(regime="ranging", bar_index=8)
        prof = _make_profile("S05")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("htf_bias_mismatch", eval_res.rejection_reasons)

    def test_107_neutral_bias_s01_s09_allowed(self):
        # S01 rejects neutral bias (Rule 10)
        cand_s01 = _make_candidate(strategy_id="S01", direction="BUY")
        context_s01 = _make_context(bar_index=8, bias="neutral")
        regime_s01 = _make_regime(regime="ranging", bar_index=8)
        prof_s01 = _make_profile("S01")
        eval_s01 = self.gate.evaluate(cand_s01, context_s01, regime_s01, prof_s01)
        self.assertEqual(eval_s01.status, "REJECTED")
        self.assertIn("htf_bias_mismatch", eval_s01.rejection_reasons)

        # S09 allows neutral bias
        cand_s09 = _make_candidate(strategy_id="S09", direction="BUY")
        context_s09 = _make_context(bar_index=8, bias="neutral")
        regime_s09 = _make_regime(regime="ranging", bar_index=8)
        prof_s09 = _make_profile("S09")
        eval_s09 = self.gate.evaluate(cand_s09, context_s09, regime_s09, prof_s09)
        self.assertEqual(eval_s09.status, "ELIGIBLE")
        self.assertNotIn("htf_bias_mismatch", eval_s09.rejection_reasons)

    def test_108_expiry_exact_boundary_pass(self):
        # Candidate expiry_bar is 8, current bar is 8 -> Pass!
        cand = _make_candidate(bar_index=8, expiry_bar=8)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile()

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "ELIGIBLE")

    def test_109_bar_after_expiry_rejects(self):
        # Candidate expiry_bar was 7, current bar is 8 -> Expired!
        cand = _make_candidate(bar_index=6, expiry_bar=7)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile()

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("expired_setup", eval_res.rejection_reasons)

    def test_110_planned_rr_exact_15_pass(self):
        cand = _make_candidate(planned_rr=1.50)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile(min_rr=1.50)

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "ELIGIBLE")

    def test_111_rr_below_gate_rejects(self):
        cand = _make_candidate(planned_rr=1.49)
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile(min_rr=1.50)

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        self.assertIn("insufficient_rr", eval_res.rejection_reasons)

    def test_112_s01_stale_sweep_boundary(self):
        context = _make_context(bar_index=28)
        regime = _make_regime(bar_index=28)
        prof = _make_profile("S01")

        # Age = 28 - 8 = 20 -> Pass
        cand_20 = _make_candidate(
            strategy_id="S01",
            bar_index=28,
            expiry_bar=30,
            evidences=[
                _make_evidence("liquidity_sweep", 8),
                _make_evidence("fair_value_gap", 24),
                _make_evidence("structure_event", 26),
            ],
        )
        res_20 = self.gate.evaluate(cand_20, context, regime, prof)
        self.assertEqual(res_20.status, "ELIGIBLE")

        # Age = 28 - 7 = 21 -> Stale reject!
        cand_21 = _make_candidate(
            strategy_id="S01",
            bar_index=28,
            expiry_bar=30,
            evidences=[
                _make_evidence("liquidity_sweep", 7),
                _make_evidence("fair_value_gap", 24),
                _make_evidence("structure_event", 26),
            ],
        )
        res_21 = self.gate.evaluate(cand_21, context, regime, prof)
        self.assertEqual(res_21.status, "REJECTED")
        self.assertIn("stale_liquidity_sweep", res_21.rejection_reasons)

    def test_113_s09_grace_session_boundaries(self):
        prof = _make_profile("S09")
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)

        # Canonical bounds for silver_bullet_ny_am on 2024-05-15 (EDT is UTC-4): 10:00 NY = 14:00 UTC, 11:00 NY = 15:00 UTC, grace = 15:15 UTC
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        # Inside grace: context bar_close_time is 14:09 UTC (between 14:00 and 15:15)
        m_inside = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
        }
        cand_inside = _make_candidate(strategy_id="S09", meta=m_inside)
        res_in = self.gate.evaluate(cand_inside, context, regime, prof)
        self.assertEqual(res_in.status, "ELIGIBLE")

        # Outside grace: context bar_close_time is 15:21 UTC (bar 80)
        context_out = _make_context(bar_index=80)
        regime_out = _make_regime(bar_index=80)
        m_outside = dict(m_inside)
        m_outside["signal_bar_close_time"] = context_out.bar_close_time.isoformat()
        cand_outside = _make_candidate(strategy_id="S09", bar_index=80, meta=m_outside)
        res_out = self.gate.evaluate(cand_outside, context_out, regime_out, prof)
        self.assertEqual(res_out.status, "REJECTED")
        self.assertIn("outside_session", res_out.rejection_reasons)

    def test_114_multiple_reasons_aggregate_canonical_order(self):
        # Candidate with wrong regime (0.0), opposed bias, and low RR
        cand = _make_candidate(strategy_id="S01", direction="BUY", planned_rr=1.20)
        context = _make_context(bar_index=8, bias="bearish")
        regime = _make_regime(regime="bearish_trend", bar_index=8)  # Score is 0.0 -> wrong_regime
        prof = _make_profile("S01")

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "REJECTED")
        # All three reasons must be present
        self.assertIn("wrong_regime", eval_res.rejection_reasons)
        self.assertIn("htf_bias_mismatch", eval_res.rejection_reasons)
        self.assertIn("insufficient_rr", eval_res.rejection_reasons)

        # Check canonical ordering
        reasons_list = list(eval_res.rejection_reasons)
        for i in range(len(reasons_list) - 1):
            idx1 = CANONICAL_REASON_CODES.index(reasons_list[i])
            idx2 = CANONICAL_REASON_CODES.index(reasons_list[i + 1])
            self.assertLess(idx1, idx2)

    def test_115_eligible_fields_and_zeroed_future_scoring_components(self):
        cand = _make_candidate()
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile()

        eval_res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(eval_res.status, "ELIGIBLE")
        self.assertEqual(eval_res.rejection_reasons, ())
        self.assertEqual(eval_res.setup_score, 0.0)
        self.assertEqual(eval_res.context_score, 0.0)
        self.assertEqual(eval_res.exec_score, 0.0)
        self.assertEqual(eval_res.total_score, 0.0)
        self.assertEqual(eval_res.details["evaluation_stage"], "eligibility_gate")

    def test_116_gate_input_permutation_invariant(self):
        cand1 = _make_candidate(strategy_id="S01", direction="BUY")
        cand2 = _make_candidate(strategy_id="S05", direction="BUY")
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        profiles = {
            "S01": _make_profile("S01"),
            "S05": _make_profile("S05"),
        }

        # Order 1: S01 then S05
        map1 = {"S01": (cand1,), "S05": (cand2,)}
        res1 = self.gate.evaluate_registry_output(map1, context, regime, profiles)

        # Order 2: S05 then S01
        map2 = {"S05": (cand2,), "S01": (cand1,)}
        res2 = self.gate.evaluate_registry_output(map2, context, regime, profiles)

        # Results should be canonically ordered and identical
        self.assertEqual(len(res1), len(res2))
        for e1, e2 in zip(res1, res2):
            self.assertEqual(e1.candidate.setup_id, e2.candidate.setup_id)
            self.assertEqual(e1.status, e2.status)
            self.assertEqual(e1.regime_score, e2.regime_score)

    def test_117_evidence_after_candidate_bar_raises(self):
        """Test 117 (P1.4): Evidence bar_index > candidate bar_index must raise StrategyStateError."""
        # Candidate at bar 8, evidence at bar 9, evaluated under context bar 10
        context = _make_context(bar_index=10)
        regime = _make_regime(bar_index=10)
        prof = _make_profile("S01")

        ev_future = _make_evidence("fair_value_gap", bar_index=9)
        cand = _make_candidate(
            strategy_id="S01",
            bar_index=8,
            evidences=[
                _make_evidence("liquidity_sweep", bar_index=2),
                ev_future,
                _make_evidence("structure_event", bar_index=7),
            ],
        )

        with self.assertRaises(StrategyStateError) as cm:
            self.gate.evaluate(cand, context, regime, prof)
        self.assertIn("bar_index 9 > candidate bar_index 8", str(cm.exception))

    def test_118_s09_missing_required_metadata_fields_rejected(self):
        """Test 118 (P1.5): S09 missing required metadata fields is rejected with missing_required_evidence."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        # Incomplete metadata (only window_name)
        cand = _make_candidate(
            strategy_id="S09",
            bar_index=8,
            meta={"window_name": "silver_bullet_ny_am"},
        )
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "REJECTED")
        self.assertIn("missing_required_evidence", res.rejection_reasons)

    def test_119_s09_malformed_metadata_raises_validation_error(self):
        """Test 119 (P1.5): Malformed S09 metadata raises StrategyValidationError."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        # Case A: grace expiry != window_end + 15m (e.g. 20m)
        m_bad_grace = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": (w_end + pd.Timedelta(minutes=20)).isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
        }
        cand_a = _make_candidate(strategy_id="S09", bar_index=8, meta=m_bad_grace)
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand_a, context, regime, prof)

        # Case B: window_key inconsistent with window_name
        m_bad_key = {
            "window_key": ["other_window", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": (w_end + pd.Timedelta(minutes=15)).isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
        }
        cand_b = _make_candidate(strategy_id="S09", bar_index=8, meta=m_bad_key)
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand_b, context, regime, prof)

    def test_120_s09_signal_after_grace_expiry_outside_session(self):
        """Test 120 (P1.5): S09 signal_bar_close_time > grace_expiry_utc rejected with outside_session."""
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        # Bar 80 has close time = 14:00 + 81m = 15:21 UTC > grace (15:15 UTC)
        context = _make_context(bar_index=80)
        regime = _make_regime(bar_index=80)
        prof = _make_profile("S09")

        m_expired = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
        }
        cand = _make_candidate(strategy_id="S09", bar_index=80, meta=m_expired)
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "REJECTED")
        self.assertIn("outside_session", res.rejection_reasons)

    def test_121_s09_signal_before_window_start_rejected_outside_session(self):
        """Test 121 (P1): Signal close before window_start_utc is rejected with outside_session."""
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        # Context and signal at 13:55 UTC (5 min before window_start 14:00)
        t_before = w_start - pd.Timedelta(minutes=5)
        context = StrategyContext(
            bar_index=0, timestamp=t_before - pd.Timedelta(minutes=1), bar_close_time=t_before,
            symbol="XAUUSD", timeframe="M1", open=2040.0, high=2041.0, low=2039.0, close=2040.0,
            volume=100.0, atr14=2.0, htf_bias=BiasStateSnapshot(bias="bullish", timestamp=t_before)
        )
        regime = MarketRegime(regime="bullish_trend", bar_index=0, timestamp=t_before, efficiency_ratio=0.5, atr_percentile=50.0)
        prof = _make_profile("S09")

        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": t_before.isoformat(),
        }
        cand = _make_candidate(strategy_id="S09", bar_index=0, meta=m)
        cand = CandidateSetup(
            setup_id="s09:before", strategy_id="S09", direction="BUY", bar_index=0,
            timestamp=t_before - pd.Timedelta(minutes=1), entry_price=2040.0, stop_loss=2030.0,
            take_profit=2060.0, planned_rr=2.0,
            evidences=(_make_evidence("liquidity_sweep", 0, time=t_before - pd.Timedelta(minutes=1)),
                       _make_evidence("fair_value_gap", 0, time=t_before - pd.Timedelta(minutes=1)),
                       _make_evidence("structure_event", 0, time=t_before - pd.Timedelta(minutes=1))),
            evidence_cluster_id="BUY:leg1:zone1", expiry_bar=10, meta=m
        )
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "REJECTED")
        self.assertIn("outside_session", res.rejection_reasons)

    def test_122_s09_context_close_before_window_start_rejected_outside_session(self):
        """Test 122 (P1): Context close before window_start_utc is rejected with outside_session."""
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        t_sig = w_start - pd.Timedelta(minutes=10)
        t_ctx = w_start - pd.Timedelta(minutes=5)

        context = StrategyContext(
            bar_index=0, timestamp=t_ctx - pd.Timedelta(minutes=1), bar_close_time=t_ctx,
            symbol="XAUUSD", timeframe="M1", open=2040.0, high=2041.0, low=2039.0, close=2040.0,
            volume=100.0, atr14=2.0, htf_bias=BiasStateSnapshot(bias="bullish", timestamp=t_ctx)
        )
        regime = MarketRegime(regime="bullish_trend", bar_index=0, timestamp=t_ctx, efficiency_ratio=0.5, atr_percentile=50.0)
        prof = _make_profile("S09")

        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": t_sig.isoformat(),
        }
        cand = CandidateSetup(
            setup_id="s09:ctx_before", strategy_id="S09", direction="BUY", bar_index=0,
            timestamp=t_sig - pd.Timedelta(minutes=1), entry_price=2040.0, stop_loss=2030.0,
            take_profit=2060.0, planned_rr=2.0,
            evidences=(_make_evidence("liquidity_sweep", 0, time=t_sig - pd.Timedelta(minutes=1)),
                       _make_evidence("fair_value_gap", 0, time=t_sig - pd.Timedelta(minutes=1)),
                       _make_evidence("structure_event", 0, time=t_sig - pd.Timedelta(minutes=1))),
            evidence_cluster_id="BUY:leg1:zone1", expiry_bar=10, meta=m
        )
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "REJECTED")
        self.assertIn("outside_session", res.rejection_reasons)

    def test_123_s09_signal_exact_at_window_start_passes(self):
        """Test 123 (P1): Signal exactly at window_start passes gate."""
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        context = StrategyContext(
            bar_index=4, timestamp=w_start - pd.Timedelta(minutes=1), bar_close_time=w_start,
            symbol="XAUUSD", timeframe="M1", open=2040.0, high=2041.0, low=2039.0, close=2040.0,
            volume=100.0, atr14=2.0, htf_bias=BiasStateSnapshot(bias="bullish", timestamp=w_start)
        )
        regime = MarketRegime(regime="bullish_trend", bar_index=4, timestamp=w_start, efficiency_ratio=0.5, atr_percentile=50.0)
        prof = _make_profile("S09")

        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": w_start.isoformat(),
        }
        cand = CandidateSetup(
            setup_id="s09:exact_start", strategy_id="S09", direction="BUY", bar_index=4,
            timestamp=w_start - pd.Timedelta(minutes=1), entry_price=2040.0, stop_loss=2030.0,
            take_profit=2060.0, planned_rr=2.0,
            evidences=(_make_evidence("liquidity_sweep", 1, time=w_start - pd.Timedelta(minutes=3)),
                       _make_evidence("fair_value_gap", 2, time=w_start - pd.Timedelta(minutes=2)),
                       _make_evidence("structure_event", 3, time=w_start - pd.Timedelta(minutes=1))),
            evidence_cluster_id="BUY:leg1:zone1", expiry_bar=10, meta=m
        )
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "ELIGIBLE")

    def test_124_s09_signal_exact_at_grace_expiry_passes(self):
        """Test 124 (P1): Signal exactly at grace_expiry passes gate."""
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        context = StrategyContext(
            bar_index=75, timestamp=w_grace - pd.Timedelta(minutes=1), bar_close_time=w_grace,
            symbol="XAUUSD", timeframe="M1", open=2040.0, high=2041.0, low=2039.0, close=2040.0,
            volume=100.0, atr14=2.0, htf_bias=BiasStateSnapshot(bias="bullish", timestamp=w_grace)
        )
        regime = MarketRegime(regime="bullish_trend", bar_index=75, timestamp=w_grace, efficiency_ratio=0.5, atr_percentile=50.0)
        prof = _make_profile("S09")

        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": w_grace.isoformat(),
        }
        cand = CandidateSetup(
            setup_id="s09:exact_grace", strategy_id="S09", direction="BUY", bar_index=75,
            timestamp=w_grace - pd.Timedelta(minutes=1), entry_price=2040.0, stop_loss=2030.0,
            take_profit=2060.0, planned_rr=2.0,
            evidences=(_make_evidence("liquidity_sweep", 70, time=w_grace - pd.Timedelta(minutes=5)),
                       _make_evidence("fair_value_gap", 71, time=w_grace - pd.Timedelta(minutes=4)),
                       _make_evidence("structure_event", 72, time=w_grace - pd.Timedelta(minutes=2))),
            evidence_cluster_id="BUY:leg1:zone1", expiry_bar=85, meta=m
        )
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "ELIGIBLE")

    def test_125_s09_signal_after_grace_expiry_rejected_outside_session(self):
        """Test 125 (P1): Signal after grace_expiry is rejected with outside_session."""
        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        t_after = w_grace + pd.Timedelta(seconds=1)
        context = StrategyContext(
            bar_index=76, timestamp=t_after - pd.Timedelta(minutes=1), bar_close_time=t_after,
            symbol="XAUUSD", timeframe="M1", open=2040.0, high=2041.0, low=2039.0, close=2040.0,
            volume=100.0, atr14=2.0, htf_bias=BiasStateSnapshot(bias="bullish", timestamp=t_after)
        )
        regime = MarketRegime(regime="bullish_trend", bar_index=76, timestamp=t_after, efficiency_ratio=0.5, atr_percentile=50.0)
        prof = _make_profile("S09")

        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": t_after.isoformat(),
        }
        cand = CandidateSetup(
            setup_id="s09:after_grace", strategy_id="S09", direction="BUY", bar_index=76,
            timestamp=t_after - pd.Timedelta(minutes=1), entry_price=2040.0, stop_loss=2030.0,
            take_profit=2060.0, planned_rr=2.0,
            evidences=(_make_evidence("liquidity_sweep", 0, time=t_after - pd.Timedelta(minutes=1)),
                       _make_evidence("fair_value_gap", 0, time=t_after - pd.Timedelta(minutes=1)),
                       _make_evidence("structure_event", 0, time=t_after - pd.Timedelta(minutes=1))),
            evidence_cluster_id="BUY:leg1:zone1", expiry_bar=85, meta=m
        )
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "REJECTED")
        self.assertIn("outside_session", res.rejection_reasons)

    def test_126_s09_signal_time_after_context_close_raises_state_error(self):
        """Test 126 (P1): signal_bar_close_time > context.bar_close_time raises StrategyStateError."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        # signal close in future relative to context
        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": (context.bar_close_time + pd.Timedelta(minutes=5)).isoformat(),
        }
        cand = _make_candidate(strategy_id="S09", bar_index=8, meta=m)
        with self.assertRaises(StrategyStateError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_127_s09_candidate_timestamp_after_signal_close_raises_state_error(self):
        """Test 127 (P1): candidate.timestamp > signal_bar_close_time raises StrategyStateError."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": context.timestamp.isoformat(),  # Signal close set to bar open!
        }
        # Candidate timestamp is set after signal close
        cand = _make_candidate(strategy_id="S09", bar_index=8, meta=m)
        cand = CandidateSetup(
            setup_id="s09:cand_future", strategy_id="S09", direction="BUY", bar_index=8,
            timestamp=context.bar_close_time,  # Candidate timestamp after signal close!
            entry_price=2040.0, stop_loss=2030.0, take_profit=2060.0, planned_rr=2.0,
            evidences=cand.evidences, evidence_cluster_id="BUY:leg1:zone1", expiry_bar=15, meta=m
        )
        with self.assertRaises(StrategyStateError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_128_s09_local_date_and_window_key_identically_wrong_rejected(self):
        """Test 128 (P1): local_date and window_key[1] matching each other but wrong vs NY date raises StrategyValidationError."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        # Wrong date: 2024-05-14 instead of 2024-05-15
        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-14", w_start.isoformat()],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-14",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
        }
        cand = _make_candidate(strategy_id="S09", bar_index=8, meta=m)
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_129_s09_invalid_window_name_raises_validation_error(self):
        """Test 129 (P1): Invalid S09 window_name raises StrategyValidationError."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        m = {
            "window_key": ["silver_bullet_tokyo", "2024-05-15", w_start.isoformat()],
            "window_name": "silver_bullet_tokyo",
            "local_date": "2024-05-15",
            "window_start_utc": w_start.isoformat(),
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
        }
        cand = _make_candidate(strategy_id="S09", bar_index=8, meta=m)
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_130_s09_utc_timestamp_naive_raises_validation_error(self):
        """Test 130 (P1): Naive _utc timestamp raises StrategyValidationError."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        w_start = pd.Timestamp("2024-05-15T14:00:00+00:00")
        w_end = pd.Timestamp("2024-05-15T15:00:00+00:00")
        w_grace = pd.Timestamp("2024-05-15T15:15:00+00:00")

        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", "2024-05-15T14:00:00"],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": "2024-05-15T14:00:00",  # Naive!
            "window_end_utc": w_end.isoformat(),
            "grace_expiry_utc": w_grace.isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
        }
        cand = _make_candidate(strategy_id="S09", bar_index=8, meta=m)
        with self.assertRaises(StrategyValidationError):
            self.gate.evaluate(cand, context, regime, prof)

    def test_131_s09_timestamp_different_timezone_normalized_to_utc_passes(self):
        """Test 131 (P1): Timestamp with different timezone (EDT) is normalized to UTC and passes."""
        context = _make_context(bar_index=8)
        regime = _make_regime(bar_index=8)
        prof = _make_profile("S09")

        # 10:00 EDT = 14:00 UTC, 11:00 EDT = 15:00 UTC, 11:15 EDT = 15:15 UTC
        m = {
            "window_key": ["silver_bullet_ny_am", "2024-05-15", "2024-05-15T10:00:00-04:00"],
            "window_name": "silver_bullet_ny_am",
            "local_date": "2024-05-15",
            "window_start_utc": "2024-05-15T10:00:00-04:00",
            "window_end_utc": "2024-05-15T11:00:00-04:00",
            "grace_expiry_utc": "2024-05-15T11:15:00-04:00",
            "signal_bar_close_time": "2024-05-15T10:09:00-04:00",
        }
        cand = _make_candidate(strategy_id="S09", bar_index=8, meta=m)
        res = self.gate.evaluate(cand, context, regime, prof)
        self.assertEqual(res.status, "ELIGIBLE")

    def test_132_s09_real_production_candidate_passes_gate(self):
        """Test 132 (P1): Real candidate emitted by S09ICTSilverBulletStrategy passes gate."""
        from tests.test_smc_strategy_s09 import _make_context as _s09_ctx, _make_sweep, _make_structure, _make_fvg
        from smc.engine.strategies.s09_ict_silver_bullet import S09ICTSilverBulletStrategy, S09Config

        strat = S09ICTSilverBulletStrategy(S09Config(mode="internal"))
        for b in range(5):
            strat.evaluate(_s09_ctx(bar_index=b))

        sw = _make_sweep(index=5, direction="bullish", price_wick=2035.0)
        strat.evaluate(_s09_ctx(bar_index=5, sweeps=[sw]))

        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_s09_ctx(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        ctx7 = _s09_ctx(bar_index=7, low_p=2037.0, close_p=2039.0, fvgs=[fvg])
        cands = strat.evaluate(ctx7)
        self.assertGreater(len(cands), 0)

        real_cand = cands[0]
        regime = MarketRegime(regime="bullish_trend", bar_index=7, timestamp=ctx7.bar_close_time, efficiency_ratio=0.5, atr_percentile=50.0)
        prof = _make_profile("S09")

        eval_res = self.gate.evaluate(real_cand, ctx7, regime, prof)
        self.assertEqual(eval_res.status, "ELIGIBLE")
        self.assertEqual(eval_res.rejection_reasons, ())


if __name__ == "__main__":
    unittest.main()

