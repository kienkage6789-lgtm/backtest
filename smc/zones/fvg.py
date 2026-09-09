"""
smc/zones/fvg.py -- Fair Value Gap (FVG) detection module.
"""

import dataclasses
import pandas as pd
from typing import List, Literal, Optional, Dict, Any, Union
from smc.models import FairValueGap
from smc.data_contract import normalize_ohlcv


def detect_fvgs(data, mode="swing", min_gap_pct=0.0, current_bar_index=None):
    if mode not in {"swing", "internal"}:
        raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")

    if isinstance(data, pd.DataFrame) and "bar_index" in data.columns:
        df = data
    else:
        df = normalize_ohlcv(data)

    if df.empty or len(df) < 3:
        return []

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    bar_indices = df["bar_index"].to_numpy()
    times = df.index

    fvgs = []

    for i in range(1, len(df) - 1):
        left_high  = highs[i - 1]
        left_low   = lows[i - 1]
        right_high = highs[i + 1]
        right_low  = lows[i + 1]
        confirmed_at = int(bar_indices[i + 1])

        if current_bar_index is not None and confirmed_at > current_bar_index:
            continue

        if left_high < right_low:
            top    = float(right_low)
            bottom = float(left_high)
            if bottom != 0.0 and (top - bottom) / bottom < min_gap_pct:
                continue
            fvgs.append(FairValueGap(
                index=int(bar_indices[i]), time=times[i], direction="bullish",
                top=top, bottom=bottom, mode=mode,
                confirmed_at=confirmed_at, filled=False, filled_at=None))
        elif left_low > right_high:
            top    = float(left_low)
            bottom = float(right_high)
            if bottom != 0.0 and (top - bottom) / bottom < min_gap_pct:
                continue
            fvgs.append(FairValueGap(
                index=int(bar_indices[i]), time=times[i], direction="bearish",
                top=top, bottom=bottom, mode=mode,
                confirmed_at=confirmed_at, filled=False, filled_at=None))

    fvgs.sort(key=lambda f: (f.confirmed_at, f.index))

    bar_records = list(zip(bar_indices.tolist(), lows.tolist(), highs.tolist()))

    for fvg in fvgs:
        for bar_idx, bar_low, bar_high in bar_records:
            if bar_idx <= fvg.confirmed_at:
                continue
            if fvg.filled:
                break
            if fvg.direction == "bullish":
                if bar_low <= fvg.bottom:
                    fvg.filled = True
                    fvg.filled_at = int(bar_idx)
            else:
                if bar_high >= fvg.top:
                    fvg.filled = True
                    fvg.filled_at = int(bar_idx)

    return fvgs


class FVGTracker:
    """
    Stateful incremental FVG detector for O(1)-per-bar streaming / replay.
    Maintains a rolling buffer of the last 3 candles.
    """

    def __init__(self, mode="swing", min_gap_pct=0.0):
        if mode not in {"swing", "internal"}:
            raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")
        self.mode = mode
        self.min_gap_pct = min_gap_pct
        self._buffer = []       # last 3 candle dicts
        self._all_fvgs = []
        self._active_fvgs = []
        self._last_processed_bar = -1

    def update(self, candle):
        """Feed a single candle, return newly confirmed FVGs at this bar."""
        c = self._extract_candle(candle)
        bar_idx = c["bar_index"]
        if bar_idx <= self._last_processed_bar:
            raise ValueError(
                f"bar_index {bar_idx} must be > last_processed_bar {self._last_processed_bar}.")
        self._last_processed_bar = bar_idx

        self._buffer.append(c)
        if len(self._buffer) > 3:
            self._buffer.pop(0)

        newly_confirmed = []

        if len(self._buffer) == 3:
            left   = self._buffer[0]
            middle = self._buffer[1]
            right  = self._buffer[2]

            if left["high"] < right["low"]:
                top    = float(right["low"])
                bottom = float(left["high"])
                if not (bottom != 0.0 and (top - bottom) / bottom < self.min_gap_pct):
                    fvg = FairValueGap(
                        index=int(middle["bar_index"]), time=middle["time"],
                        direction="bullish", top=top, bottom=bottom, mode=self.mode,
                        confirmed_at=int(right["bar_index"]), filled=False, filled_at=None)
                    self._all_fvgs.append(fvg)
                    self._active_fvgs.append(fvg)
                    newly_confirmed.append(fvg)

            elif left["low"] > right["high"]:
                top    = float(left["low"])
                bottom = float(right["high"])
                if not (bottom != 0.0 and (top - bottom) / bottom < self.min_gap_pct):
                    fvg = FairValueGap(
                        index=int(middle["bar_index"]), time=middle["time"],
                        direction="bearish", top=top, bottom=bottom, mode=self.mode,
                        confirmed_at=int(right["bar_index"]), filled=False, filled_at=None)
                    self._all_fvgs.append(fvg)
                    self._active_fvgs.append(fvg)
                    newly_confirmed.append(fvg)

        cur_low  = c["low"]
        cur_high = c["high"]
        still_active = []
        for fvg in self._active_fvgs:
            if fvg.confirmed_at >= bar_idx:
                still_active.append(fvg)
                continue
            if fvg.direction == "bullish":
                if cur_low <= fvg.bottom:
                    fvg.filled = True
                    fvg.filled_at = bar_idx
                else:
                    still_active.append(fvg)
            else:
                if cur_high >= fvg.top:
                    fvg.filled = True
                    fvg.filled_at = bar_idx
                else:
                    still_active.append(fvg)
        self._active_fvgs = still_active
        return newly_confirmed

    def get_active_fvgs(self):
        return list(self._active_fvgs)

    def get_all_fvgs(self):
        return list(self._all_fvgs)

    @staticmethod
    def _extract_candle(candle):
        if isinstance(candle, pd.Series):
            c_dict = candle.to_dict()
            if "time" not in c_dict and isinstance(candle.name, (pd.Timestamp, str)):
                c_dict["time"] = candle.name
        else:
            c_dict = dict(candle)
        raw_time = c_dict.get("time")
        if isinstance(raw_time, pd.Timestamp):
            ts = raw_time if raw_time.tz is not None else raw_time.tz_localize("UTC")
        elif raw_time is not None:
            ts = pd.to_datetime(raw_time, utc=True)
        else:
            ts = pd.Timestamp.now(tz="UTC")
        bar_idx = c_dict.get("bar_index")
        if bar_idx is None:
            raise ValueError("Candle must contain 'bar_index'.")
        return {
            "bar_index": int(bar_idx),
            "time": ts,
            "open":  float(c_dict.get("open",  c_dict.get("close", 0.0))),
            "high":  float(c_dict["high"]),
            "low":   float(c_dict["low"]),
            "close": float(c_dict.get("close", c_dict.get("open", 0.0))),
        }
