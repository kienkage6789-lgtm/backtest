"""
smc/zones/fvg.py -- Fair Value Gap (FVG) detection module.
"""

import dataclasses
import pandas as pd
import numpy as np
from typing import List, Literal, Optional, Dict, Any, Union
from smc.models import FairValueGap, StructureEvent
from smc.data_contract import normalize_ohlcv


def detect_fvgs(data, mode="swing", min_gap_pct=0.0, current_bar_index=None,
                structure_events: Optional[List[StructureEvent]] = None,
                require_displacement: bool = False, atr_period: int = 14,
                displacement_multiplier: float = 1.5):
    if mode not in {"swing", "internal"}:
        raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")

    if isinstance(data, pd.DataFrame) and "bar_index" in data.columns:
        df = data
    else:
        df = normalize_ohlcv(data)

    if df.empty or len(df) < 3:
        return []
    if atr_period <= 0 or displacement_multiplier <= 0:
        raise ValueError("atr_period and displacement_multiplier must be greater than 0")

    # ATR is indexed at the middle candle.  It uses only bars through that
    # candle, so confirmation of the right candle cannot leak into quality.
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(atr_period, min_periods=atr_period).mean().fillna(0.0)

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

        middle_open = float(df["open"].iloc[i])
        middle_close = float(df["close"].iloc[i])
        body = abs(middle_close - middle_open)
        atr_value = float(atr.iloc[i])
        bullish_displacement = middle_close > middle_open and atr_value > 0 and body > atr_value * displacement_multiplier
        bearish_displacement = middle_close < middle_open and atr_value > 0 and body > atr_value * displacement_multiplier

        if left_high < right_low:
            top    = float(right_low)
            bottom = float(left_high)
            if bottom != 0.0 and (top - bottom) / bottom < min_gap_pct:
                continue
            if require_displacement and not bullish_displacement:
                continue
            fvgs.append(FairValueGap(
                index=int(bar_indices[i]), time=times[i], direction="bullish",
                top=top, bottom=bottom, mode=mode,
                confirmed_at=confirmed_at, filled=False, filled_at=None,
                displacement=bullish_displacement, body_ratio=(body / atr_value if atr_value else 0.0), atr_value=atr_value,
                middle_body_size=body, middle_range=float(df["high"].iloc[i] - df["low"].iloc[i]),
                middle_body_ratio=(body / float(df["high"].iloc[i] - df["low"].iloc[i]) if float(df["high"].iloc[i] - df["low"].iloc[i]) else 0.0)))
        elif left_low > right_high:
            top    = float(left_low)
            bottom = float(right_high)
            if bottom != 0.0 and (top - bottom) / bottom < min_gap_pct:
                continue
            if require_displacement and not bearish_displacement:
                continue
            fvgs.append(FairValueGap(
                index=int(bar_indices[i]), time=times[i], direction="bearish",
                top=top, bottom=bottom, mode=mode,
                confirmed_at=confirmed_at, filled=False, filled_at=None,
                displacement=bearish_displacement, body_ratio=(body / atr_value if atr_value else 0.0), atr_value=atr_value,
                middle_body_size=body, middle_range=float(df["high"].iloc[i] - df["low"].iloc[i]),
                middle_body_ratio=(body / float(df["high"].iloc[i] - df["low"].iloc[i]) if float(df["high"].iloc[i] - df["low"].iloc[i]) else 0.0)))

    if structure_events:
        events = sorted((e for e in structure_events if e.mode == mode), key=lambda e: e.index)
        for fvg in fvgs:
            matching_event = next(
                (e for e in events if e.direction == fvg.direction and
                 fvg.index < e.index and fvg.confirmed_at <= e.index),
                None,
            )
            if matching_event is not None:
                fvg.structure_leg_id = matching_event.structure_leg_id

    fvgs.sort(key=lambda f: (f.confirmed_at, f.index))

    bar_records = list(zip(bar_indices.tolist(), lows.tolist(), highs.tolist()))

    for fvg in fvgs:
        for bar_idx, bar_low, bar_high in bar_records:
            if bar_idx <= fvg.confirmed_at:
                continue
            if fvg.filled:
                break
            touched = bar_low <= fvg.top and bar_high >= fvg.bottom
            if touched:
                fvg.touch_count += 1
                if not fvg.ce_touched and bar_low <= fvg.ce <= bar_high:
                    fvg.ce_touched = True
                    fvg.ce_touched_at = int(bar_idx)
                if not fvg.partial_filled:
                    if (fvg.direction == "bullish" and bar_low <= fvg.top) or (fvg.direction == "bearish" and bar_high >= fvg.bottom):
                        fvg.partial_filled = True
                        fvg.partial_filled_at = int(bar_idx)
                        fvg.state = "partial"
            if fvg.direction == "bullish" and bar_low <= fvg.bottom:
                fvg.filled = True; fvg.filled_at = int(bar_idx); fvg.state = "filled"
            elif fvg.direction == "bearish" and bar_high >= fvg.top:
                fvg.filled = True; fvg.filled_at = int(bar_idx); fvg.state = "filled"

    return fvgs


class FVGTracker:
    """
    Stateful incremental FVG detector for O(1)-per-bar streaming / replay.
    Maintains a rolling buffer of the last 3 candles.
    """

    def __init__(self, mode="swing", min_gap_pct=0.0, require_displacement=False,
                 atr_period=14, displacement_multiplier=1.5):
        if mode not in {"swing", "internal"}:
            raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")
        self.mode = mode
        self.min_gap_pct = min_gap_pct
        self.require_displacement = bool(require_displacement)
        self.atr_period = int(atr_period)
        self.displacement_multiplier = float(displacement_multiplier)
        self._buffer = []       # last 3 candle dicts
        self._all_fvgs = []
        self._active_fvgs = []
        self._last_processed_bar = -1
        self._structure_events: List[StructureEvent] = []
        self._candle_history = []

    def update(self, candle, structure_events: Optional[List[StructureEvent]] = None):
        """Feed a single candle, return newly confirmed FVGs at this bar."""
        c = self._extract_candle(candle)
        bar_idx = c["bar_index"]
        if bar_idx <= self._last_processed_bar:
            raise ValueError(
                f"bar_index {bar_idx} must be > last_processed_bar {self._last_processed_bar}.")
        self._last_processed_bar = bar_idx
        if structure_events:
            new_events = [
                event for event in structure_events
                if event.mode == self.mode and not any(
                    old.index == event.index and old.direction == event.direction
                    for old in self._structure_events
                )
            ]
            if new_events:
                self._structure_events.extend(new_events)
                self._structure_events.sort(key=lambda item: item.index)
                # Events can be confirmed after an FVG was emitted. Update the
                # already-held FVG objects so batch and incremental metadata agree.
                for fvg in self._all_fvgs:
                    if fvg.structure_leg_id is None:
                        fvg.structure_leg_id = self._leg_for_fvg(
                            int(fvg.index), int(fvg.confirmed_at), fvg.direction
                        )

        self._buffer.append(c)
        self._candle_history.append(c)
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
                body = abs(middle["close"] - middle["open"])
                atr_value = self._atr_for_middle()
                displacement = middle["close"] > middle["open"] and atr_value > 0 and body > atr_value * self.displacement_multiplier
                if not (bottom != 0.0 and (top - bottom) / bottom < self.min_gap_pct) and (not self.require_displacement or displacement):
                    fvg = FairValueGap(
                        index=int(middle["bar_index"]), time=middle["time"],
                        direction="bullish", top=top, bottom=bottom, mode=self.mode,
                         confirmed_at=int(right["bar_index"]), filled=False, filled_at=None,
                         structure_leg_id=self._leg_for_fvg(int(middle["bar_index"]), int(right["bar_index"]), "bullish"),
                         displacement=displacement, body_ratio=body / atr_value if atr_value else 0.0, atr_value=atr_value,
                         middle_body_size=body, middle_range=middle["high"] - middle["low"],
                         middle_body_ratio=body / (middle["high"] - middle["low"]) if middle["high"] != middle["low"] else 0.0)
                    self._all_fvgs.append(fvg)
                    self._active_fvgs.append(fvg)
                    newly_confirmed.append(fvg)

            elif left["low"] > right["high"]:
                top    = float(left["low"])
                bottom = float(right["high"])
                body = abs(middle["close"] - middle["open"])
                atr_value = self._atr_for_middle()
                displacement = middle["close"] < middle["open"] and atr_value > 0 and body > atr_value * self.displacement_multiplier
                if not (bottom != 0.0 and (top - bottom) / bottom < self.min_gap_pct) and (not self.require_displacement or displacement):
                    fvg = FairValueGap(
                        index=int(middle["bar_index"]), time=middle["time"],
                        direction="bearish", top=top, bottom=bottom, mode=self.mode,
                         confirmed_at=int(right["bar_index"]), filled=False, filled_at=None,
                         structure_leg_id=self._leg_for_fvg(int(middle["bar_index"]), int(right["bar_index"]), "bearish"),
                         displacement=displacement, body_ratio=body / atr_value if atr_value else 0.0, atr_value=atr_value,
                         middle_body_size=body, middle_range=middle["high"] - middle["low"],
                         middle_body_ratio=body / (middle["high"] - middle["low"]) if middle["high"] != middle["low"] else 0.0)
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
            touched = cur_low <= fvg.top and cur_high >= fvg.bottom
            if touched:
                fvg.touch_count += 1
                if not fvg.ce_touched and cur_low <= fvg.ce <= cur_high:
                    fvg.ce_touched = True; fvg.ce_touched_at = bar_idx
                if not fvg.partial_filled:
                    fvg.partial_filled = True; fvg.partial_filled_at = bar_idx; fvg.state = "partial"
            fully = (fvg.direction == "bullish" and cur_low <= fvg.bottom) or (fvg.direction == "bearish" and cur_high >= fvg.top)
            if fully:
                fvg.filled = True; fvg.filled_at = bar_idx; fvg.state = "filled"
            else:
                still_active.append(fvg)
        self._active_fvgs = still_active
        return newly_confirmed

    def _leg_for_fvg(self, fvg_index: int, confirmed_at: int, direction: str) -> Optional[str]:
        event = next((event for event in self._structure_events
                      if event.direction == direction and fvg_index < event.index and
                      confirmed_at <= event.index), None)
        return event.structure_leg_id if event is not None else None

    def _atr_for_middle(self) -> float:
        # The current buffer is [left, middle, right].  Exclude right: the
        # quality of the middle candle must not depend on the confirmation bar.
        middle_history = self._candle_history[:-1]
        if len(middle_history) < self.atr_period:
            return 0.0
        start = len(middle_history) - self.atr_period
        rows = middle_history[start:]
        trs = []
        for i, row in enumerate(rows):
            if i:
                prev = rows[i - 1]["close"]
            else:
                # Match pandas rolling TR: the first candle in the window
                # still uses the close immediately preceding the window.
                prev = middle_history[start - 1]["close"] if start > 0 else row["close"]
            trs.append(max(row["high"] - row["low"], abs(row["high"] - prev), abs(row["low"] - prev)))
        return float(sum(trs) / self.atr_period)

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
