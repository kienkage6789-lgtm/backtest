"""
smc/indicators/supertrend.py
============================
Deterministic, zero-lookahead Supertrend indicator implementation with Wilder's RMA ATR,
adaptive bands, and consecutive-bars noise filtering.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Union

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SupertrendState:
    """
    State of Supertrend indicator at a given closed bar.
    """
    index: int
    time: Optional[pd.Timestamp]
    high: float
    low: float
    close: float
    tr: float
    atr: float
    basic_upper: float
    basic_lower: float
    final_upper: float
    final_lower: float
    supertrend: float
    trend: int  # +1: bullish, -1: bearish, 0: neutral/warmup
    consecutive_bars: int
    bias: Literal["bullish", "bearish", "neutral"]
    is_confirmed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat() if isinstance(self.time, pd.Timestamp) else str(self.time),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "atr": float(self.atr) if math.isfinite(self.atr) else None,
            "supertrend": float(self.supertrend) if math.isfinite(self.supertrend) else None,
            "upper_band": float(self.final_upper) if math.isfinite(self.final_upper) else None,
            "lower_band": float(self.final_lower) if math.isfinite(self.final_lower) else None,
            "trend": int(self.trend),
            "consecutive_bars": int(self.consecutive_bars),
            "bias": str(self.bias),
            "is_confirmed": bool(self.is_confirmed),
        }


class SupertrendIndicator:
    """
    Stateful, zero-lookahead Supertrend indicator tracker.
    Computes ATR via Wilder's RMA, updates adaptive bands, and applies noise filtering.
    """

    def __init__(
        self,
        atr_length: int = 10,
        multiplier: float = 3.0,
        min_bars_held: int = 2,
    ) -> None:
        if atr_length <= 0:
            raise ValueError(f"atr_length must be > 0, got {atr_length}")
        if multiplier <= 0.0:
            raise ValueError(f"multiplier must be > 0.0, got {multiplier}")
        if min_bars_held < 1:
            raise ValueError(f"min_bars_held must be >= 1, got {min_bars_held}")

        self.atr_length: int = int(atr_length)
        self.multiplier: float = float(multiplier)
        self.min_bars_held: int = int(min_bars_held)

        self._index: int = -1
        self._prev_close: Optional[float] = None
        self._tr_history: list[float] = []
        self._prev_atr: Optional[float] = None
        self._prev_final_upper: Optional[float] = None
        self._prev_final_lower: Optional[float] = None
        self._prev_trend: int = 0
        self._consecutive_bars: int = 0
        self._last_state: Optional[SupertrendState] = None

    def reset(self) -> None:
        """Reset all internal state for clean replay/batch execution."""
        self._index = -1
        self._prev_close = None
        self._tr_history.clear()
        self._prev_atr = None
        self._prev_final_upper = None
        self._prev_final_lower = None
        self._prev_trend = 0
        self._consecutive_bars = 0
        self._last_state = None

    @property
    def last_state(self) -> Optional[SupertrendState]:
        return self._last_state

    def update(
        self,
        high: float,
        low: float,
        close: float,
        time: Optional[Union[pd.Timestamp, str]] = None,
        index: Optional[int] = None,
    ) -> SupertrendState:
        """
        Ingest a closed candle and advance the Supertrend state by exactly one bar.
        Zero future information is accessed.
        """
        self._index += 1
        curr_idx = index if index is not None else self._index

        ts = pd.Timestamp(time) if time is not None else None

        # 1. True Range calculation
        if self._prev_close is None:
            tr = float(high - low)
        else:
            tr0 = float(high - low)
            tr1 = abs(float(high - self._prev_close))
            tr2 = abs(float(low - self._prev_close))
            tr = max(tr0, tr1, tr2)

        self._tr_history.append(tr)

        # 2. Wilder's RMA ATR calculation
        if len(self._tr_history) < self.atr_length:
            # Warmup period: not enough bars for ATR
            atr = float("nan")
            basic_upper = float("nan")
            basic_lower = float("nan")
            final_upper = float("nan")
            final_lower = float("nan")
            supertrend_val = float("nan")
            trend = 0
            consecutive = 0
            bias: Literal["bullish", "bearish", "neutral"] = "neutral"
            is_confirmed = False
        elif len(self._tr_history) == self.atr_length:
            # Initial ATR is simple average of first atr_length TR values
            atr = float(sum(self._tr_history) / self.atr_length)
            self._prev_atr = atr

            hl2 = (float(high) + float(low)) / 2.0
            basic_upper = hl2 + self.multiplier * atr
            basic_lower = hl2 - self.multiplier * atr
            final_upper = basic_upper
            final_lower = basic_lower

            # Initial trend determination
            if close >= basic_lower:
                trend = 1
                supertrend_val = final_lower
            else:
                trend = -1
                supertrend_val = final_upper

            consecutive = 1
            self._prev_trend = trend
            self._prev_final_upper = final_upper
            self._prev_final_lower = final_lower

            bias = "neutral" if consecutive < self.min_bars_held else ("bullish" if trend == 1 else "bearish")
            is_confirmed = (bias != "neutral")
        else:
            # Wilder's RMA smoothing: (prev_atr * (length - 1) + tr) / length
            atr = (self._prev_atr * (self.atr_length - 1) + tr) / self.atr_length
            self._prev_atr = atr

            hl2 = (float(high) + float(low)) / 2.0
            basic_upper = hl2 + self.multiplier * atr
            basic_lower = hl2 - self.multiplier * atr

            # Adaptive Final Upper Band
            if (
                self._prev_final_upper is None
                or basic_upper < self._prev_final_upper
                or (self._prev_close is not None and self._prev_close > self._prev_final_upper)
            ):
                final_upper = basic_upper
            else:
                final_upper = self._prev_final_upper

            # Adaptive Final Lower Band
            if (
                self._prev_final_lower is None
                or basic_lower > self._prev_final_lower
                or (self._prev_close is not None and self._prev_close < self._prev_final_lower)
            ):
                final_lower = basic_lower
            else:
                final_lower = self._prev_final_lower

            # Trend continuation / reversal
            if self._prev_trend == 1:
                if close < final_lower:
                    trend = -1
                    supertrend_val = final_upper
                else:
                    trend = 1
                    supertrend_val = final_lower
            else:  # prev_trend == -1 or 0
                if close > final_upper:
                    trend = 1
                    supertrend_val = final_lower
                else:
                    trend = -1
                    supertrend_val = final_upper

            # Consecutive bars held
            if trend == self._prev_trend:
                consecutive = self._consecutive_bars + 1
            else:
                consecutive = 1

            self._prev_trend = trend
            self._prev_final_upper = final_upper
            self._prev_final_lower = final_lower

            # Noise filter: require holding trend for >= min_bars_held
            if consecutive >= self.min_bars_held:
                if trend == 1 and close > supertrend_val:
                    bias = "bullish"
                    is_confirmed = True
                elif trend == -1 and close < supertrend_val:
                    bias = "bearish"
                    is_confirmed = True
                else:
                    bias = "neutral"
                    is_confirmed = False
            else:
                bias = "neutral"
                is_confirmed = False

        self._consecutive_bars = consecutive
        self._prev_close = float(close)

        state = SupertrendState(
            index=curr_idx,
            time=ts,
            high=float(high),
            low=float(low),
            close=float(close),
            tr=float(tr),
            atr=float(atr),
            basic_upper=float(basic_upper),
            basic_lower=float(basic_lower),
            final_upper=float(final_upper),
            final_lower=float(final_lower),
            supertrend=float(supertrend_val),
            trend=trend,
            consecutive_bars=consecutive,
            bias=bias,
            is_confirmed=is_confirmed,
        )
        self._last_state = state
        return state


def calculate_supertrend(
    df: pd.DataFrame,
    atr_length: int = 10,
    multiplier: float = 3.0,
    min_bars_held: int = 2,
) -> pd.DataFrame:
    """
    Calculate Supertrend indicator across an entire DataFrame deterministically.
    Returns copy of DataFrame with columns:
    ['supertrend', 'supertrend_trend', 'supertrend_bias', 'supertrend_confirmed',
     'supertrend_consecutive', 'supertrend_upper', 'supertrend_lower', 'atr'].
    """
    indicator = SupertrendIndicator(
        atr_length=atr_length,
        multiplier=multiplier,
        min_bars_held=min_bars_held,
    )

    results = []
    has_time = "time" in df.columns or "datetime" in df.columns
    time_col = "time" if "time" in df.columns else ("datetime" if "datetime" in df.columns else None)

    for idx, row in df.iterrows():
        t_val = row[time_col] if time_col else None
        st = indicator.update(
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            time=t_val,
            index=int(idx) if isinstance(idx, int) else None,
        )
        results.append({
            "supertrend": st.supertrend,
            "supertrend_trend": st.trend,
            "supertrend_bias": st.bias,
            "supertrend_confirmed": st.is_confirmed,
            "supertrend_consecutive": st.consecutive_bars,
            "supertrend_upper": st.final_upper,
            "supertrend_lower": st.final_lower,
            "atr": st.atr,
        })

    res_df = pd.DataFrame(results, index=df.index)
    return pd.concat([df.copy(), res_df], axis=1)
