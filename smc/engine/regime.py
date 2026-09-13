"""
smc/engine/regime.py
====================
Market Regime Classifier V1 (T53.7).

Classifies market state into exactly five mutually exclusive regimes:
- bullish_trend
- bearish_trend
- volatile_reversal
- ranging
- uncertain

Strictly deterministic, zero-lookahead, stateful trailing bounded memory,
and atomic lifecycle guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import math
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from smc.engine.errors import StrategyStateError, StrategyValidationError
from smc.engine.models import (
    MarketRegime,
    MarketRegimeType,
    StrategyContext,
    StructureEventSnapshot,
    LiquiditySweepSnapshot,
    _parse_timestamp,
    _unfreeze,
)


@dataclass(frozen=True)
class RegimeClassifierConfig:
    """
    Configuration parameters for MarketRegimeClassifier.
    """
    close_lookback: int = 20
    atr_lookback: int = 100
    er_threshold: float = 0.30
    volatile_atr_percentile: float = 60.0
    recent_sweep_bars: int = 20
    structure_mode: str = "swing"

    def __post_init__(self) -> None:
        if isinstance(self.close_lookback, bool) or not isinstance(self.close_lookback, (int, np.integer)):
            raise StrategyValidationError("close_lookback must be an int, not bool")
        if int(self.close_lookback) < 2:
            raise StrategyValidationError(f"close_lookback must be >= 2, got {self.close_lookback}")
        object.__setattr__(self, "close_lookback", int(self.close_lookback))

        if isinstance(self.atr_lookback, bool) or not isinstance(self.atr_lookback, (int, np.integer)):
            raise StrategyValidationError("atr_lookback must be an int, not bool")
        if int(self.atr_lookback) < 1:
            raise StrategyValidationError(f"atr_lookback must be >= 1, got {self.atr_lookback}")
        object.__setattr__(self, "atr_lookback", int(self.atr_lookback))

        if isinstance(self.er_threshold, bool) or not isinstance(self.er_threshold, (int, float, np.floating, np.integer)):
            raise StrategyValidationError("er_threshold must be a float, not bool")
        f_er = float(self.er_threshold)
        if not math.isfinite(f_er) or not (0.0 <= f_er <= 1.0):
            raise StrategyValidationError(f"er_threshold must be finite in [0.0, 1.0], got {self.er_threshold}")
        object.__setattr__(self, "er_threshold", f_er)

        if isinstance(self.volatile_atr_percentile, bool) or not isinstance(self.volatile_atr_percentile, (int, float, np.floating, np.integer)):
            raise StrategyValidationError("volatile_atr_percentile must be a float, not bool")
        f_pct = float(self.volatile_atr_percentile)
        if not math.isfinite(f_pct) or not (0.0 <= f_pct <= 100.0):
            raise StrategyValidationError(f"volatile_atr_percentile must be finite in [0.0, 100.0], got {self.volatile_atr_percentile}")
        object.__setattr__(self, "volatile_atr_percentile", f_pct)

        if isinstance(self.recent_sweep_bars, bool) or not isinstance(self.recent_sweep_bars, (int, np.integer)):
            raise StrategyValidationError("recent_sweep_bars must be an int, not bool")
        if int(self.recent_sweep_bars) < 0:
            raise StrategyValidationError(f"recent_sweep_bars must be >= 0, got {self.recent_sweep_bars}")
        object.__setattr__(self, "recent_sweep_bars", int(self.recent_sweep_bars))

        if self.structure_mode not in {"swing", "internal"}:
            raise StrategyValidationError(f"structure_mode must be 'swing' or 'internal', got {self.structure_mode}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "close_lookback": int(self.close_lookback),
            "atr_lookback": int(self.atr_lookback),
            "er_threshold": float(self.er_threshold),
            "volatile_atr_percentile": float(self.volatile_atr_percentile),
            "recent_sweep_bars": int(self.recent_sweep_bars),
            "structure_mode": str(self.structure_mode),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RegimeClassifierConfig:
        if not isinstance(data, Mapping):
            raise StrategyValidationError(f"Expected mapping for RegimeClassifierConfig, got {type(data).__name__}")
        allowed = {
            "close_lookback",
            "atr_lookback",
            "er_threshold",
            "volatile_atr_percentile",
            "recent_sweep_bars",
            "structure_mode",
        }
        unknown = set(data.keys()) - allowed
        if unknown:
            raise StrategyValidationError(f"Unknown fields in RegimeClassifierConfig: {sorted(unknown)}")
        return cls(
            close_lookback=data.get("close_lookback", 20),
            atr_lookback=data.get("atr_lookback", 100),
            er_threshold=data.get("er_threshold", 0.30),
            volatile_atr_percentile=data.get("volatile_atr_percentile", 60.0),
            recent_sweep_bars=data.get("recent_sweep_bars", 20),
            structure_mode=data.get("structure_mode", "swing"),
        )


def _ensure_comparable(t1: pd.Timestamp, t2: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    if t1.tz is None and t2.tz is not None:
        return t1.tz_localize("UTC"), t2
    if t1.tz is not None and t2.tz is None:
        return t1, t2.tz_localize("UTC")
    return t1, t2


class MarketRegimeClassifier:
    """
    Stateful market regime classifier maintaining trailing bounded buffers.
    """

    def __init__(self, config: Optional[RegimeClassifierConfig] = None) -> None:
        if config is None:
            self.config = RegimeClassifierConfig()
        elif isinstance(config, RegimeClassifierConfig):
            self.config = config
        else:
            raise StrategyValidationError(f"Expected RegimeClassifierConfig, got {type(config).__name__}")

        self._closes: list[float] = []
        self._atrs: list[float] = []
        self._seen_bos: dict[tuple[Any, ...], StructureEventSnapshot] = {}
        self._seen_sweeps: dict[tuple[Any, ...], LiquiditySweepSnapshot] = {}

        self._last_bar_index: Optional[int] = None
        self._last_timestamp: Optional[pd.Timestamp] = None
        self._last_bar_close_time: Optional[pd.Timestamp] = None
        self._last_context_payload: Optional[dict[str, Any]] = None
        self._last_result: Optional[MarketRegime] = None

    def reset(self) -> None:
        self._closes.clear()
        self._atrs.clear()
        self._seen_bos.clear()
        self._seen_sweeps.clear()
        self._last_bar_index = None
        self._last_timestamp = None
        self._last_bar_close_time = None
        self._last_context_payload = None
        self._last_result = None

    def _assert_zero_future_leak(self, context: StrategyContext) -> None:
        N = context.bar_index
        bar_close_time = context.bar_close_time

        for st in getattr(context, "recent_structures", ()):
            if st.index > N:
                raise StrategyStateError(f"Structure event index {st.index} > bar {N}")
            if st.confirmed_swing_at is not None and st.confirmed_swing_at > N:
                raise StrategyStateError(f"Structure event confirmed_swing_at {st.confirmed_swing_at} > bar {N}")

        for p in getattr(context, "active_pools", ()):
            if p.confirmed_at > N:
                raise StrategyStateError(f"Pool confirmed_at {p.confirmed_at} > bar {N}")
            if p.swept_at is not None and p.swept_at > N:
                raise StrategyStateError(f"Pool swept_at {p.swept_at} > bar {N}")
            if p.invalidated_at is not None and p.invalidated_at > N:
                raise StrategyStateError(f"Pool invalidated_at {p.invalidated_at} > bar {N}")

        for sw in getattr(context, "recent_sweeps", ()):
            if sw.index > N:
                raise StrategyStateError(f"Sweep index {sw.index} > bar {N}")
            if sw.confirmed_at > N:
                raise StrategyStateError(f"Sweep confirmed_at {sw.confirmed_at} > bar {N}")
            if sw.swept_at > N:
                raise StrategyStateError(f"Sweep swept_at {sw.swept_at} > bar {N}")

        if context.htf_bias is not None:
            if context.htf_bias.source_event_index is not None and context.htf_bias.source_event_index > N:
                raise StrategyStateError(f"HTF bias source_event_index {context.htf_bias.source_event_index} > bar {N}")
            for fld in ("as_of", "source_event_time", "timestamp", "pending_reversal_event_time"):
                ts_val = getattr(context.htf_bias, fld, None)
                if ts_val is not None:
                    t_bias, t_close = _ensure_comparable(pd.Timestamp(ts_val), bar_close_time)
                    if t_bias > t_close:
                        raise StrategyStateError(f"HTF bias {fld} {t_bias} > bar close {t_close}")

        if context.session_decision is not None:
            t_sd, t_close = _ensure_comparable(context.session_decision.timestamp, bar_close_time)
            if t_sd > t_close:
                raise StrategyStateError(f"Session decision timestamp {t_sd} > bar close {t_close}")

    def update(self, context: StrategyContext) -> MarketRegime:
        N = context.bar_index

        # 1. Zero lookahead & future leak check
        self._assert_zero_future_leak(context)

        # 2. Lifecycle & monotonicity check
        if self._last_bar_index is not None:
            if N == self._last_bar_index:
                # Idempotent retry check
                curr_payload = context.to_dict()
                if curr_payload == self._last_context_payload:
                    assert self._last_result is not None
                    return self._last_result
                raise StrategyStateError(f"Conflicting evaluation on same bar {N}")
            if N < self._last_bar_index:
                raise StrategyStateError(f"Non-monotonic backward bar {N} < {self._last_bar_index}")
            if N > self._last_bar_index + 1:
                raise StrategyStateError(f"Non-contiguous bar gap {N} > {self._last_bar_index} + 1")

            t_curr, t_last = _ensure_comparable(context.timestamp, self._last_timestamp)
            if t_curr <= t_last:
                raise StrategyStateError(f"Non-increasing timestamp {t_curr} <= {t_last}")

            tc_curr, tc_last = _ensure_comparable(context.bar_close_time, self._last_bar_close_time)
            if tc_curr <= tc_last:
                raise StrategyStateError(f"Non-increasing bar_close_time {tc_curr} <= {tc_last}")

        # 3. Create working copies for atomic state updates
        working_closes = list(self._closes)
        working_atrs = list(self._atrs)
        working_seen_bos = dict(self._seen_bos)
        working_seen_sweeps = dict(self._seen_sweeps)

        # Append current close
        working_closes.append(float(context.close))
        if len(working_closes) > self.config.close_lookback:
            working_closes = working_closes[-self.config.close_lookback:]

        # Append current ATR if finite and valid (>= 0.0, non-bool)
        if context.atr14 is not None and not isinstance(context.atr14, bool):
            f_atr = float(context.atr14)
            if math.isfinite(f_atr) and f_atr >= 0.0:
                working_atrs.append(f_atr)
                if len(working_atrs) > self.config.atr_lookback:
                    working_atrs = working_atrs[-self.config.atr_lookback:]

        # Ingest and prune structure events (BOS)
        for st in getattr(context, "recent_structures", ()):
            if st.mode == self.config.structure_mode and st.event_type == "BOS":
                if (N - self.config.close_lookback) < st.index <= N:
                    key = (st.mode, st.event_type, st.direction, st.index, st.broken_swing_index, st.structure_leg_id)
                    working_seen_bos[key] = st

        # Prune BOS older than close_lookback window
        min_bos_idx = N - self.config.close_lookback + 1
        working_seen_bos = {k: v for k, v in working_seen_bos.items() if v.index >= min_bos_idx}

        # Ingest and prune sweeps (P1.3: replace/invalidate old state)
        for sw in getattr(context, "recent_sweeps", ()):
            if sw.mode == self.config.structure_mode:
                key = (sw.mode, sw.direction, sw.index, sw.pool_kind, round(float(sw.pool_price), 4))
                if sw.valid:
                    if 0 <= (N - sw.index) <= self.config.recent_sweep_bars:
                        working_seen_sweeps[key] = sw
                else:
                    working_seen_sweeps.pop(key, None)

        # Prune sweeps older than recent_sweep_bars
        min_sweep_idx = N - self.config.recent_sweep_bars
        working_seen_sweeps = {k: v for k, v in working_seen_sweeps.items() if v.valid and (min_sweep_idx <= v.index <= N)}

        # 4. Math: Kaufman Efficiency Ratio (ER)
        if len(working_closes) < 2:
            change = 0.0
            denom = 0.0
            er = 0.0
        else:
            change = abs(working_closes[-1] - working_closes[0])
            denom = sum(abs(working_closes[i] - working_closes[i - 1]) for i in range(1, len(working_closes)))
            er = 0.0 if denom == 0.0 else max(0.0, min(1.0, change / denom))

        # 5. Math: Empirical Mid-Rank ATR Percentile
        if len(working_atrs) == 0:
            current_atr = 0.0
            atr_pct = 0.0
        else:
            current_atr = working_atrs[-1]
            less = sum(1 for v in working_atrs if v < current_atr)
            equal = sum(1 for v in working_atrs if v == current_atr)
            atr_pct = max(0.0, min(100.0, 100.0 * (less + 0.5 * equal) / len(working_atrs)))

        # 6. Event counts
        bullish_bos_count = sum(1 for st in working_seen_bos.values() if st.direction == "bullish" and st.index >= min_bos_idx)
        bearish_bos_count = sum(1 for st in working_seen_bos.values() if st.direction == "bearish" and st.index >= min_bos_idx)
        has_recent_sweep = any(sw.valid and (min_sweep_idx <= sw.index <= N) for sw in working_seen_sweeps.values())

        # 7. Warm-up verification
        is_warmed_up = (len(working_closes) >= self.config.close_lookback and len(working_atrs) >= self.config.atr_lookback)

        # 8. Five-regime decision tree evaluation
        regime: MarketRegimeType
        reason: str

        if not is_warmed_up:
            regime = "uncertain"
            reason = "insufficient_warmup_bars"
        else:
            htf_bias_dir = context.htf_bias.bias if context.htf_bias is not None else None
            # DO NOT round er or atr_pct before threshold comparisons (P1.2)
            er_thresh = self.config.er_threshold
            pct_thresh = self.config.volatile_atr_percentile

            if htf_bias_dir == "bullish" and bullish_bos_count >= 1 and bearish_bos_count == 0 and er >= er_thresh:
                regime = "bullish_trend"
                reason = "bullish_trend_confirmed"
            elif htf_bias_dir == "bearish" and bearish_bos_count >= 1 and bullish_bos_count == 0 and er >= er_thresh:
                regime = "bearish_trend"
                reason = "bearish_trend_confirmed"
            elif atr_pct >= pct_thresh and has_recent_sweep:
                regime = "volatile_reversal"
                reason = "volatile_reversal_confirmed"
            elif er < er_thresh and bullish_bos_count == 0 and bearish_bos_count == 0 and atr_pct < pct_thresh:
                regime = "ranging"
                reason = "ranging_confirmed"
            else:
                regime = "uncertain"
                if bullish_bos_count > 0 and bearish_bos_count > 0:
                    reason = "conflicting_bos"
                elif has_recent_sweep and atr_pct < pct_thresh:
                    reason = "sweep_without_high_volatility"
                elif htf_bias_dir is None:
                    reason = "missing_htf_bias"
                else:
                    reason = "unclassified_regime"

        # 9. Build immutable MarketRegime
        result = MarketRegime(
            regime=regime,
            bar_index=N,
            timestamp=context.bar_close_time,
            efficiency_ratio=er,
            atr_percentile=atr_pct,
            metrics={
                "close_count": float(len(working_closes)),
                "atr_count": float(len(working_atrs)),
                "er_change": float(change),
                "er_denom": float(denom),
                "efficiency_ratio": float(round(er, 4)),
                "current_atr": float(current_atr),
                "atr_percentile": float(round(atr_pct, 2)),
                "bullish_bos_count": float(bullish_bos_count),
                "bearish_bos_count": float(bearish_bos_count),
                "recent_sweep": 1.0 if has_recent_sweep else 0.0,
            },
            reason=reason,
            meta={
                "is_warmed_up": is_warmed_up,
                "structure_mode": self.config.structure_mode,
                "htf_bias": context.htf_bias.bias if context.htf_bias is not None else None,
            },
        )

        # 10. Atomic commit
        self._closes = working_closes
        self._atrs = working_atrs
        self._seen_bos = working_seen_bos
        self._seen_sweeps = working_seen_sweeps
        self._last_bar_index = N
        self._last_timestamp = context.timestamp
        self._last_bar_close_time = context.bar_close_time
        self._last_context_payload = context.to_dict()
        self._last_result = result

        return result


def classify_market_regimes(
    contexts: Sequence[StrategyContext],
    config: Optional[RegimeClassifierConfig] = None,
) -> tuple[MarketRegime, ...]:
    """
    Classify a sequence of contiguous StrategyContexts into a tuple of MarketRegime results.
    """
    classifier = MarketRegimeClassifier(config=config)
    results = [classifier.update(ctx) for ctx in contexts]
    return tuple(results)


__all__ = [
    "RegimeClassifierConfig",
    "MarketRegimeClassifier",
    "classify_market_regimes",
]
