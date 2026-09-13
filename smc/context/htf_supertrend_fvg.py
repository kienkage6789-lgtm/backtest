"""
smc/context/htf_supertrend_fvg.py
=================================
Higher Timeframe (HTF) Supertrend & FVG Context Module.
Provides zero-lookahead, stateful tracking of HTF Supertrend bias and HTF FVGs
mapped as-of Lower Timeframe (LTF) candle timestamps.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

import pandas as pd

from smc.indicators.supertrend import SupertrendIndicator, SupertrendState


@dataclass
class HTFFairValueGap:
    """
    Higher Timeframe Fair Value Gap with lifecycle tracking.
    """
    fvg_id: str
    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    ce: float
    gap_size: float
    timeframe: str
    confirmed_bar: int
    confirmed_time: pd.Timestamp
    displacement: bool = True
    status: Literal["active", "filled", "invalidated", "expired"] = "active"
    touch_count: int = 0
    first_touch_time: Optional[pd.Timestamp] = None
    last_touch_time: Optional[pd.Timestamp] = None
    trades_triggered: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fvg_id": str(self.fvg_id),
            "direction": str(self.direction),
            "top": float(self.top),
            "bottom": float(self.bottom),
            "ce": float(self.ce),
            "gap_size": float(self.gap_size),
            "timeframe": str(self.timeframe),
            "confirmed_bar": int(self.confirmed_bar),
            "confirmed_time": self.confirmed_time.isoformat() if isinstance(self.confirmed_time, pd.Timestamp) else str(self.confirmed_time),
            "displacement": bool(self.displacement),
            "status": str(self.status),
            "touch_count": int(self.touch_count),
            "first_touch_time": self.first_touch_time.isoformat() if isinstance(self.first_touch_time, pd.Timestamp) else None,
            "last_touch_time": self.last_touch_time.isoformat() if isinstance(self.last_touch_time, pd.Timestamp) else None,
            "trades_triggered": int(self.trades_triggered),
            "meta": dict(self.meta),
        }


class HTFSupertrendFVGTracker:
    """
    Tracks HTF candles, calculates HTF Supertrend, and detects/monitors HTF FVGs.
    Strictly zero-lookahead: HTF bars are only revealed when their close_time <= ltf_close_time.
    """

    def __init__(
        self,
        htf_candles: Optional[Union[pd.DataFrame, Sequence[dict[str, Any]]]] = None,
        timeframe: str = "H1",
        supertrend_atr_length: int = 10,
        supertrend_multiplier: float = 3.0,
        supertrend_min_bars: int = 2,
        fvg_expiry_bars: int = 30,
        min_displacement_ratio: float = 0.0,
    ) -> None:
        self.timeframe: str = str(timeframe)
        self.fvg_expiry_bars: int = int(fvg_expiry_bars)
        self.min_displacement_ratio: float = float(min_displacement_ratio)

        self._supertrend_indicator = SupertrendIndicator(
            atr_length=supertrend_atr_length,
            multiplier=supertrend_multiplier,
            min_bars_held=supertrend_min_bars,
        )

        # Ingest pre-supplied HTF candles if any
        self._all_htf_candles: list[dict[str, Any]] = []
        if htf_candles is not None:
            self.set_htf_candles(htf_candles)

        self._htf_cursor: int = 0
        self._closed_htf_bars: list[dict[str, Any]] = []
        self._supertrend_history: list[SupertrendState] = []
        self._fvgs: list[HTFFairValueGap] = []
        self._active_fvgs: list[HTFFairValueGap] = []
        self._fvg_counter: int = 0

    def reset(self) -> None:
        """Reset internal runtime state while retaining pre-seeded candles."""
        self._supertrend_indicator.reset()
        self._htf_cursor = 0
        self._closed_htf_bars.clear()
        self._supertrend_history.clear()
        self._fvgs.clear()
        self._active_fvgs.clear()
        self._fvg_counter = 0

    def set_htf_candles(self, candles: Union[pd.DataFrame, Sequence[dict[str, Any]]]) -> None:
        """Set or update the full HTF candle pool."""
        self._all_htf_candles.clear()
        if isinstance(candles, pd.DataFrame):
            for _, row in candles.iterrows():
                t = pd.Timestamp(row.get("time", row.get("datetime", None)))
                if t.tzinfo is None:
                    t = t.tz_localize("UTC")
                else:
                    t = t.tz_convert("UTC")

                # If close_time not explicit, derive from timeframe
                ct = row.get("close_time", None)
                if ct is None:
                    delta = pd.Timedelta(hours=1) if "H" in self.timeframe or self.timeframe == "H1" else pd.Timedelta(minutes=60)
                    ct = t + delta
                else:
                    ct = pd.Timestamp(ct)
                    if ct.tzinfo is None:
                        ct = ct.tz_localize("UTC")
                    else:
                        ct = ct.tz_convert("UTC")

                self._all_htf_candles.append({
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "time": t,
                    "close_time": ct,
                })
        else:
            for c in candles:
                t = pd.Timestamp(c.get("time", c.get("datetime", None)))
                if t.tzinfo is None:
                    t = t.tz_localize("UTC")
                else:
                    t = t.tz_convert("UTC")

                ct = c.get("close_time", None)
                if ct is None:
                    delta = pd.Timedelta(hours=1) if "H" in self.timeframe or self.timeframe == "H1" else pd.Timedelta(minutes=60)
                    ct = t + delta
                else:
                    ct = pd.Timestamp(ct)
                    if ct.tzinfo is None:
                        ct = ct.tz_localize("UTC")
                    else:
                        ct = ct.tz_convert("UTC")

                self._all_htf_candles.append({
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "time": t,
                    "close_time": ct,
                })

        # Sort strictly by close_time
        self._all_htf_candles.sort(key=lambda x: x["close_time"])

    def update_as_of(self, ltf_bar_close_time: pd.Timestamp) -> Tuple[str, Optional[SupertrendState], List[HTFFairValueGap]]:
        """
        Advance HTF state strictly up to ltf_bar_close_time.
        Zero future HTF candles are consumed.
        """
        if ltf_bar_close_time.tzinfo is None:
            ltf_bar_close_time = ltf_bar_close_time.tz_localize("UTC")
        else:
            ltf_bar_close_time = ltf_bar_close_time.tz_convert("UTC")

        while self._htf_cursor < len(self._all_htf_candles):
            candidate_bar = self._all_htf_candles[self._htf_cursor]
            if candidate_bar["close_time"] <= ltf_bar_close_time:
                # This HTF candle is officially closed and effective
                self._ingest_closed_htf_candle(candidate_bar)
                self._htf_cursor += 1
            else:
                break

        # Current HTF Supertrend state
        latest_st = self._supertrend_history[-1] if self._supertrend_history else None
        current_bias = latest_st.bias if latest_st else "neutral"

        # Update FVG lifecycle (expiry relative to latest HTF bar)
        curr_bar_idx = len(self._closed_htf_bars) - 1
        active_list = []
        for fvg in self._fvgs:
            if fvg.status == "active":
                if curr_bar_idx - fvg.confirmed_bar >= self.fvg_expiry_bars:
                    fvg.status = "expired"
                else:
                    active_list.append(fvg)

        self._active_fvgs = active_list
        return current_bias, latest_st, list(self._active_fvgs)

    def _ingest_closed_htf_candle(self, bar: dict[str, Any]) -> None:
        """Process a single newly closed HTF candle."""
        bar_idx = len(self._closed_htf_bars)
        self._closed_htf_bars.append(bar)

        st = self._supertrend_indicator.update(
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            time=bar["close_time"],
            index=bar_idx,
        )
        self._supertrend_history.append(st)

        # Check for FVG creation (requires at least 3 bars: [n-2, n-1, n])
        if bar_idx >= 2:
            bar_n2 = self._closed_htf_bars[bar_idx - 2]
            bar_n1 = self._closed_htf_bars[bar_idx - 1]
            bar_n = bar

            # Bullish FVG: Low[n] > High[n-2]
            if bar_n["low"] > bar_n2["high"]:
                bottom = bar_n2["high"]
                top = bar_n["low"]
                gap_size = top - bottom
                ce = (top + bottom) / 2.0

                # Must align with HTF Supertrend bullish bias
                if st.bias == "bullish":
                    self._fvg_counter += 1
                    fvg = HTFFairValueGap(
                        fvg_id=f"htf_fvg_bull_{self._fvg_counter}_{bar_idx}",
                        direction="bullish",
                        top=top,
                        bottom=bottom,
                        ce=ce,
                        gap_size=gap_size,
                        timeframe=self.timeframe,
                        confirmed_bar=bar_idx,
                        confirmed_time=bar["close_time"],
                        displacement=True,
                    )
                    self._fvgs.append(fvg)

            # Bearish FVG: High[n] < Low[n-2]
            elif bar_n["high"] < bar_n2["low"]:
                bottom = bar_n["high"]
                top = bar_n2["low"]
                gap_size = top - bottom
                ce = (top + bottom) / 2.0

                # Must align with HTF Supertrend bearish bias
                if st.bias == "bearish":
                    self._fvg_counter += 1
                    fvg = HTFFairValueGap(
                        fvg_id=f"htf_fvg_bear_{self._fvg_counter}_{bar_idx}",
                        direction="bearish",
                        top=top,
                        bottom=bottom,
                        ce=ce,
                        gap_size=gap_size,
                        timeframe=self.timeframe,
                        confirmed_bar=bar_idx,
                        confirmed_time=bar["close_time"],
                        displacement=True,
                    )
                    self._fvgs.append(fvg)

        # Check existing active FVGs for distal close through invalidation
        for fvg in self._fvgs:
            if fvg.status == "active":
                if fvg.direction == "bullish" and bar["close"] < fvg.bottom:
                    fvg.status = "invalidated"
                elif fvg.direction == "bearish" and bar["close"] > fvg.top:
                    fvg.status = "invalidated"

    def check_ltf_candle_interaction(self, ltf_candle: dict[str, Any]) -> None:
        """
        Check and update HTF FVG touches/invalidations from an LTF closed candle.
        """
        c_low = float(ltf_candle["low"])
        c_high = float(ltf_candle["high"])
        c_close = float(ltf_candle["close"])
        c_time = pd.Timestamp(ltf_candle.get("time", ltf_candle.get("bar_close_time", None)))

        for fvg in self._fvgs:
            if fvg.status == "active":
                # Distal invalidation by LTF close
                if fvg.direction == "bullish" and c_close < fvg.bottom:
                    fvg.status = "invalidated"
                    continue
                elif fvg.direction == "bearish" and c_close > fvg.top:
                    fvg.status = "invalidated"
                    continue

                # Touch check: overlap [c_low, c_high] with [fvg.bottom, fvg.top]
                overlaps = (c_low <= fvg.top and c_high >= fvg.bottom)
                if overlaps:
                    fvg.touch_count += 1
                    if fvg.first_touch_time is None:
                        fvg.first_touch_time = c_time
                    fvg.last_touch_time = c_time

    def select_best_fvg(
        self,
        current_price: float,
        bias: str,
    ) -> Optional[HTFFairValueGap]:
        """
        Deterministic tie-break priority ranking when multiple HTF FVGs are active:
        1. Aligned with current bias
        2. Proximity to current price: abs(price - fvg.ce) ascending
        3. Untested preferred: touch_count == 0 first
        4. Stronger displacement: gap_size descending
        5. Most recent: confirmed_bar descending
        """
        candidates = [
            f for f in self._active_fvgs
            if f.status == "active" and f.direction == bias and f.trades_triggered == 0
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda f: (
            abs(current_price - f.ce),
            1 if f.touch_count > 0 else 0,
            -f.gap_size,
            -f.confirmed_bar,
            f.fvg_id,
        ))
        return candidates[0]
