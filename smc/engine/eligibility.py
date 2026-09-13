"""
smc/engine/eligibility.py
=========================
Eligibility Gate and 30-cell Regime Matrix (T53.7).

Evaluates CandidateSetups against hard eligibility criteria:
- 30-cell Strategy x Direction x Regime suitability matrix
- Required evidence per strategy (S01, S05, S09)
- Canonical event ordering
- HTF bias gate (ADR 16 unified policy)
- Setup expiry and minimum planned RR
- Sweep staleness (S01) and session/grace compliance (S09)

Aggregates rejection reasons canonically without premature scoring.
"""

from __future__ import annotations

import datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from smc.engine.execution import CooldownBook

import pandas as pd

from smc.engine.errors import StrategyStateError, StrategyValidationError
from smc.engine.models import (
    CandidateSetup,
    EvaluationStatus,
    MarketRegime,
    StrategyContext,
    StrategyEvaluation,
    StrategyProfile,
)
from smc.engine.strategies.s09_ict_silver_bullet import (
    CANONICAL_WINDOWS,
    NEW_YORK_TZ,
    compute_window_bounds_for_date,
)


CANONICAL_REASON_CODES: tuple[str, ...] = (
    "wrong_regime",
    "htf_bias_mismatch",
    "missing_htf_bias",
    "missing_required_evidence",
    "missing_htf_poi",
    "price_not_in_htf_poi",
    "wrong_poi_direction",
    "poi_invalidated",
    "invalid_ltf_sweep",
    "mss_not_confirmed",
    "ltf_zone_invalid",
    "invalid_event_order",
    "outside_session",
    "expired_setup",
    "entry_expired",
    "invalid_order_block",
    "invalid_fvg",
    "fvg_invalidated_by_close",
    "stale_liquidity_sweep",
    "opposite_structure_shift",
    "insufficient_rr",
    "insufficient_rr_at_fill",
    "geometry_violation_at_fill",
    "cooldown_active",
    "conflicting_direction",
    "insufficient_score",
    "insufficient_warmup_bars",
)

# 30-cell Strategy x Direction x Regime Suitability Matrix
_RAW_REGIME_MATRIX: dict[tuple[str, str, str], float] = {
    # S05: BOS -> OB Retest (Trend Continuation)
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

    # S01: ICT 2022 (Reversal)
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

    # S09: ICT Silver Bullet (Time-based Continuation/Reversal)
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

    # smc_st_fvg_mss: HTF Supertrend continuation with LTF confirmation.
    # Use the same directional regime policy as the other trend-following
    # SMC strategies while keeping this strategy explicitly represented in
    # the registry matrix (rather than raising an unknown-cell error).
    ("smc_st_fvg_mss", "BUY", "bullish_trend"): 100.0,
    ("smc_st_fvg_mss", "BUY", "bearish_trend"): 0.0,
    ("smc_st_fvg_mss", "BUY", "volatile_reversal"): 40.0,
    ("smc_st_fvg_mss", "BUY", "ranging"): 60.0,
    ("smc_st_fvg_mss", "BUY", "uncertain"): 30.0,

    ("smc_st_fvg_mss", "SELL", "bullish_trend"): 0.0,
    ("smc_st_fvg_mss", "SELL", "bearish_trend"): 100.0,
    ("smc_st_fvg_mss", "SELL", "volatile_reversal"): 40.0,
    ("smc_st_fvg_mss", "SELL", "ranging"): 60.0,
    ("smc_st_fvg_mss", "SELL", "uncertain"): 30.0,
}

REGIME_MATRIX: Mapping[tuple[str, str, str], float] = MappingProxyType(_RAW_REGIME_MATRIX)


def get_regime_matrix_score(strategy_id: str, direction: str, regime: str) -> float:
    key = (strategy_id, direction, regime)
    if key not in REGIME_MATRIX:
        raise StrategyValidationError(
            f"Unknown cell in 30-cell matrix: strategy={strategy_id}, direction={direction}, regime={regime}"
        )
    return REGIME_MATRIX[key]


def _ensure_comparable(t1: pd.Timestamp, t2: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    if t1.tz is None and t2.tz is not None:
        return t1.tz_localize("UTC"), t2
    if t1.tz is not None and t2.tz is None:
        return t1, t2.tz_localize("UTC")
    return t1, t2


class EligibilityGate:
    """
    Stateless gate evaluating candidate setups against hard market and strategy constraints.
    """

    def __init__(self, s01_stale_sweep_max_bars: int = 20) -> None:
        if isinstance(s01_stale_sweep_max_bars, bool) or not isinstance(s01_stale_sweep_max_bars, int):
            raise StrategyValidationError("s01_stale_sweep_max_bars must be a positive integer")
        if s01_stale_sweep_max_bars <= 0:
            raise StrategyValidationError("s01_stale_sweep_max_bars must be > 0")
        self.s01_stale_sweep_max_bars = int(s01_stale_sweep_max_bars)

    def evaluate(
        self,
        candidate: CandidateSetup,
        context: StrategyContext,
        regime: MarketRegime,
        profile: StrategyProfile,
        cooldown_book: Optional[CooldownBook] = None,
    ) -> StrategyEvaluation:
        N = context.bar_index

        # 1. Preflight consistency
        if regime.bar_index != N:
            raise StrategyStateError(f"Regime bar_index {regime.bar_index} != context bar_index {N}")

        t_reg, t_ctx = _ensure_comparable(regime.timestamp, context.bar_close_time)
        if t_reg != t_ctx:
            raise StrategyStateError(f"Regime timestamp {t_reg} != context bar_close_time {t_ctx}")

        if candidate.strategy_id != profile.strategy_id:
            raise StrategyValidationError(
                f"Candidate strategy_id '{candidate.strategy_id}' != profile strategy_id '{profile.strategy_id}'"
            )

        if candidate.direction not in profile.allowed_directions:
            raise StrategyValidationError(
                f"Candidate direction '{candidate.direction}' not in profile allowed {profile.allowed_directions}"
            )

        if context.timeframe not in profile.timeframes:
            raise StrategyValidationError(
                f"Context timeframe '{context.timeframe}' not in profile timeframes {profile.timeframes}"
            )

        # 2. Zero-lookahead integrity
        if candidate.bar_index > N:
            raise StrategyStateError(f"Candidate bar_index {candidate.bar_index} > current bar {N}")

        tc_cand, tc_close = _ensure_comparable(candidate.timestamp, context.bar_close_time)
        if tc_cand > tc_close:
            raise StrategyStateError(f"Candidate timestamp {tc_cand} > bar_close_time {tc_close}")

        c_evidences = getattr(candidate, "evidences", getattr(candidate, "evidence", ()))
        for ev in c_evidences:
            if ev.bar_index > candidate.bar_index:
                raise StrategyStateError(
                    f"Evidence '{ev.evidence_id}' bar_index {ev.bar_index} > candidate bar_index {candidate.bar_index}"
                )
            if ev.bar_index > N:
                raise StrategyStateError(f"Evidence '{ev.evidence_id}' bar_index {ev.bar_index} > current bar {N}")
            if ev.time is not None:
                tev, tcand = _ensure_comparable(pd.Timestamp(ev.time), candidate.timestamp)
                if tev > tcand:
                    raise StrategyStateError(f"Evidence '{ev.evidence_id}' time {tev} > candidate timestamp {tcand}")
                tev, tclose = _ensure_comparable(pd.Timestamp(ev.time), context.bar_close_time)
                if tev > tclose:
                    raise StrategyStateError(f"Evidence '{ev.evidence_id}' time {tev} > bar_close_time {tclose}")

        reasons: list[str] = []

        # 3. 30-cell matrix check
        regime_score = get_regime_matrix_score(candidate.strategy_id, candidate.direction, regime.regime)
        if regime_score == 0.0:
            reasons.append("wrong_regime")

        # 4. Required evidence check
        evidence_kinds = {ev.kind for ev in c_evidences}
        sid = candidate.strategy_id
        if sid == "S01":
            trigger_type = candidate.meta.get("trigger_type")
            required_s01 = {"structure_event", "fair_value_gap"}
            if trigger_type != "mss_without_sweep":
                required_s01.add("liquidity_sweep")
            if not required_s01.issubset(evidence_kinds):
                reasons.append("missing_required_evidence")
        elif sid == "S05":
            if candidate.meta.get("flow_type") == "ltf_confirmation":
                z_type = candidate.meta.get("ltf_entry_zone_type")
                has_conf = "structure_event" in evidence_kinds
                has_htf = bool(evidence_kinds & {"order_block", "htf_poi"})
                if z_type == "fvg":
                    has_zone = "fair_value_gap" in evidence_kinds
                elif z_type == "ob":
                    has_zone = "order_block" in evidence_kinds
                elif z_type == "confluence":
                    has_zone = {"fair_value_gap", "order_block"}.issubset(evidence_kinds)
                else:
                    has_zone = bool(evidence_kinds & {"fair_value_gap", "order_block"})

                if not (has_conf and has_htf and has_zone):
                    reasons.append("missing_required_evidence")
            else:
                if not {"structure_event", "order_block"}.issubset(evidence_kinds):
                    reasons.append("missing_required_evidence")
        elif sid == "S09":
            has_kinds = {"liquidity_sweep", "structure_event", "fair_value_gap"}.issubset(evidence_kinds)
            required_s09_fields = (
                "window_key",
                "window_name",
                "local_date",
                "window_start_utc",
                "window_end_utc",
                "grace_expiry_utc",
                "signal_bar_close_time",
            )
            missing_meta = any(k not in candidate.meta for k in required_s09_fields)
            if not (has_kinds and not missing_meta):
                reasons.append("missing_required_evidence")
            else:
                # 1. Validate S09 metadata window_key structure
                w_key = candidate.meta["window_key"]
                if not isinstance(w_key, (tuple, list)) or len(w_key) != 3:
                    raise StrategyValidationError(
                        f"S09 metadata window_key must be a 3-element sequence [name, date, start_utc], got {w_key}"
                    )

                # 2. Parse timestamps
                try:
                    t_start = pd.Timestamp(candidate.meta["window_start_utc"])
                    t_end = pd.Timestamp(candidate.meta["window_end_utc"])
                    t_grace = pd.Timestamp(candidate.meta["grace_expiry_utc"])
                    t_sig = pd.Timestamp(candidate.meta["signal_bar_close_time"])
                    t_wkey_start = pd.Timestamp(w_key[2])
                except Exception as exc:
                    raise StrategyValidationError(f"Unparseable S09 metadata timestamp: {exc}") from exc

                # 3. Check timezone-awareness
                for t_name, t_val in (
                    ("window_start_utc", t_start),
                    ("window_end_utc", t_end),
                    ("grace_expiry_utc", t_grace),
                    ("signal_bar_close_time", t_sig),
                    ("window_key[2]", t_wkey_start),
                ):
                    if t_val.tz is None:
                        raise StrategyValidationError(f"S09 metadata '{t_name}' must be timezone-aware")

                # 4. Normalize all timestamps to UTC for exact instant comparisons
                t_start = t_start.tz_convert("UTC")
                t_end = t_end.tz_convert("UTC")
                t_grace = t_grace.tz_convert("UTC")
                t_sig = t_sig.tz_convert("UTC")
                t_wkey_start = t_wkey_start.tz_convert("UTC")
                time_filter_disabled = bool(candidate.meta.get("time_filter_disabled", False))

                t_ctx_close = (
                    context.bar_close_time.tz_convert("UTC")
                    if context.bar_close_time.tz is not None
                    else context.bar_close_time.tz_localize("UTC")
                )
                t_cand = (
                    candidate.timestamp.tz_convert("UTC")
                    if candidate.timestamp.tz is not None
                    else candidate.timestamp.tz_localize("UTC")
                )

                # 5. Canonical window name check
                w_name = str(candidate.meta["window_name"])
                if w_name not in CANONICAL_WINDOWS:
                    raise StrategyValidationError(
                        f"Invalid S09 window_name '{w_name}'. Allowed: {list(CANONICAL_WINDOWS)}"
                    )

                # 6. Window key vs window_name check
                if str(w_key[0]) != w_name:
                    raise StrategyValidationError(
                        f"S09 window_key[0] '{w_key[0]}' != window_name '{w_name}'"
                    )

                # 7. Window key start instant vs window_start_utc
                if t_wkey_start != t_start:
                    raise StrategyValidationError(
                        f"S09 window_key[2] '{t_wkey_start}' != window_start_utc '{t_start}'"
                    )

                # 8. Derive real expected NY local date from window_start_utc
                expected_local_date = t_start.tz_convert(NEW_YORK_TZ).date().isoformat()
                local_date_str = str(candidate.meta["local_date"])
                if local_date_str != expected_local_date:
                    raise StrategyValidationError(
                        f"S09 local_date '{local_date_str}' != expected NY date '{expected_local_date}' derived from window_start_utc"
                    )
                if str(w_key[1]) != expected_local_date:
                    raise StrategyValidationError(
                        f"S09 window_key[1] '{w_key[1]}' != expected NY date '{expected_local_date}' derived from window_start_utc"
                    )

                # 9. Cross-validate against canonical window bounds for that date.
                # The temporary S09 no-time-filter mode retains metadata and
                # ordering validation but intentionally skips canonical
                # London/NY hour matching.
                if not time_filter_disabled:
                    canonical_start, canonical_end, canonical_grace = compute_window_bounds_for_date(
                        expected_local_date, w_name, grace_minutes=15
                    )
                    if t_start != canonical_start:
                        raise StrategyValidationError(
                            f"S09 window_start_utc '{t_start}' does not match canonical start '{canonical_start}'"
                        )
                    if t_end != canonical_end:
                        raise StrategyValidationError(
                            f"S09 window_end_utc '{t_end}' does not match canonical end '{canonical_end}'"
                        )
                    if t_grace != canonical_grace:
                        raise StrategyValidationError(
                            f"S09 grace_expiry_utc '{t_grace}' does not match canonical grace '{canonical_grace}'"
                        )

                # 10. Window ordering & exact 15m grace period
                if not (t_start < t_end < t_grace):
                    raise StrategyValidationError(
                        f"Invalid S09 window ordering: start={t_start}, end={t_end}, grace={t_grace}"
                    )
                if t_grace != t_end + pd.Timedelta(minutes=15):
                    raise StrategyValidationError(
                        f"S09 grace_expiry_utc must be exactly window_end_utc + 15m, got {t_grace} vs {t_end + pd.Timedelta(minutes=15)}"
                    )

                # 11. Zero-lookahead & metadata integrity guards
                if t_sig > t_ctx_close:
                    raise StrategyStateError(
                        f"S09 signal_bar_close_time {t_sig} > context bar_close_time {t_ctx_close}"
                    )
                if t_cand > t_sig:
                    raise StrategyStateError(
                        f"S09 candidate timestamp {t_cand} > signal_bar_close_time {t_sig}"
                    )

                # 12. Full session boundary check:
                # window_start_utc <= signal_bar_close_time <= grace_expiry_utc
                # window_start_utc <= context.bar_close_time <= grace_expiry_utc
                if (not time_filter_disabled) and (
                    t_sig < t_start or t_sig > t_grace or t_ctx_close < t_start or t_ctx_close > t_grace
                ):
                    reasons.append("outside_session")

        # 5. Event ordering check
        if sid in {"S01", "S09"}:
            sw_ev = next((e for e in c_evidences if e.kind == "liquidity_sweep"), None)
            fvg_ev = next((e for e in c_evidences if e.kind == "fair_value_gap"), None)
            mss_ev = next((e for e in c_evidences if e.kind == "structure_event"), None)
            if sw_ev is not None and fvg_ev is not None and mss_ev is not None:
                # Legacy S01/S09: Sweep <= FVG < MSS < Candidate Bar <= N.
                # Adaptive S01 without a sweep permits either FVG-before-MSS
                # or MSS-before-FVG, while the candidate still follows both.
                if sid == "S01" and candidate.meta.get("trigger_type") == "mss_without_sweep":
                    if not (
                        min(fvg_ev.bar_index, mss_ev.bar_index)
                        < max(fvg_ev.bar_index, mss_ev.bar_index)
                        < candidate.bar_index <= N
                    ):
                        reasons.append("invalid_event_order")
                elif not (sw_ev is not None and sw_ev.bar_index <= fvg_ev.bar_index < mss_ev.bar_index < candidate.bar_index <= N):
                    reasons.append("invalid_event_order")
        elif sid == "S05":
            if candidate.meta.get("flow_type") == "ltf_confirmation":
                htf_touch_bar = candidate.meta.get("htf_ob_touch_bar")
                conf_ev = next((e for e in c_evidences if e.kind == "structure_event"), None)
                if conf_ev is not None and htf_touch_bar is not None:
                    if not (htf_touch_bar < conf_ev.bar_index < candidate.bar_index <= N):
                        reasons.append("invalid_event_order")
                else:
                    reasons.append("invalid_event_order")
            else:
                bos_ev = next((e for e in c_evidences if e.kind == "structure_event"), None)
                ob_ev = next((e for e in c_evidences if e.kind == "order_block"), None)
                if bos_ev is not None and ob_ev is not None:
                    # BOS occurs before retest signal bar
                    if not (bos_ev.bar_index < candidate.bar_index <= N):
                        reasons.append("invalid_event_order")

        # 6. HTF bias gate (ADR 16 unified policy)
        if context.htf_bias is None:
            reasons.append("missing_required_evidence")
        else:
            bias = context.htf_bias.bias
            opposed = "bearish" if candidate.direction == "BUY" else "bullish"
            if bias == opposed:
                reasons.append("htf_bias_mismatch")
            elif bias == "neutral":
                if sid == "S05" or (sid == "S01" and not candidate.meta.get("allow_neutral_bias", False)):
                    reasons.append("htf_bias_mismatch")
                # S09 allows neutral bias

        # 7. Expiry boundary check
        if N > candidate.expiry_bar:
            reasons.append("expired_setup")

        # 8. Planned RR check
        min_rr = max(1.50, profile.min_rr)
        if candidate.planned_rr < min_rr:
            reasons.append("insufficient_rr")

        # 9. Sweep staleness
        if sid == "S01":
            sw_ev = next((e for e in c_evidences if e.kind == "liquidity_sweep"), None)
            if sw_ev is not None and (
                candidate.bar_index - sw_ev.bar_index > self.s01_stale_sweep_max_bars
            ):
                reasons.append("stale_liquidity_sweep")

        # 10. Cooldown check
        if cooldown_book is not None and cooldown_book.is_active(candidate.strategy_id, candidate.direction, N):
            reasons.append("cooldown_active")

        # 11. Canonical reason aggregation
        unique_reasons = set(reasons)
        sorted_reasons = tuple(
            sorted(
                unique_reasons,
                key=lambda r: CANONICAL_REASON_CODES.index(r) if r in CANONICAL_REASON_CODES else 999,
            )
        )
        status: EvaluationStatus = "ELIGIBLE" if len(sorted_reasons) == 0 else "REJECTED"

        return StrategyEvaluation(
            candidate=candidate,
            status=status,
            rejection_reasons=sorted_reasons,
            regime_score=regime_score,
            setup_score=0.0,
            context_score=0.0,
            exec_score=0.0,
            total_score=0.0,
            details={
                "evaluation_stage": "eligibility_gate",
                "regime": regime.regime,
                "matrix_score": regime_score,
                "htf_bias": context.htf_bias.bias if context.htf_bias else None,
                "planned_rr": float(candidate.planned_rr),
                "gate_version": "v1",
            },
        )

    def evaluate_registry_output(
        self,
        candidates_by_strategy: Mapping[str, tuple[CandidateSetup, ...]],
        context: StrategyContext,
        regime: MarketRegime,
        profiles: Mapping[str, StrategyProfile],
        cooldown_book: Optional[CooldownBook] = None,
    ) -> tuple[StrategyEvaluation, ...]:
        evaluations: list[StrategyEvaluation] = []
        for sid in sorted(candidates_by_strategy.keys()):
            if sid not in profiles:
                raise StrategyValidationError(f"Missing StrategyProfile for strategy '{sid}'")
            prof = profiles[sid]
            for cand in candidates_by_strategy[sid]:
                if cand.strategy_id != sid:
                    raise StrategyValidationError(
                        f"Candidate strategy_id '{cand.strategy_id}' does not match mapping key '{sid}'"
                    )
                eval_res = self.evaluate(cand, context, regime, prof, cooldown_book=cooldown_book)
                evaluations.append(eval_res)
        return tuple(evaluations)


__all__ = [
    "CANONICAL_REASON_CODES",
    "REGIME_MATRIX",
    "get_regime_matrix_score",
    "EligibilityGate",
]
