import pandas as pd
import numpy as np
from typing import List, Literal, Optional, Union, Dict, Any
from smc.models import SwingPoint
from smc.data_contract import normalize_ohlcv

def detect_swings(
    data: pd.DataFrame,
    strength: int = 5,
    left_strength: Optional[int] = None,
    right_strength: Optional[int] = None,
    mode: Literal["swing", "internal"] = "swing",
    current_bar_index: Optional[int] = None,
    only_confirmed: bool = False
) -> List[SwingPoint]:
    """
    Detects non-repainting Swing Highs and Swing Lows from normalized OHLCV data.
    
    Args:
        data: DataFrame normalized via normalize_ohlcv or raw OHLCV DataFrame.
        strength: Default symmetric left/right strength window (must be > 0).
        left_strength: Optional override for left window depth (must be > 0).
        right_strength: Optional override for right window depth (must be > 0).
        mode: 'swing' or 'internal' (stored on SwingPoint.mode).
        current_bar_index: Optional maximum bar index cutoff for confirmed swings.
        only_confirmed: If True, automatically filters swings to those confirmed on or before the last bar of `data`.
        
    Returns:
        List of SwingPoint objects sorted by confirmed_at ascending.
        
    Raises:
        ValueError: If strength parameters are <= 0.
    """
    l_str = left_strength if left_strength is not None else strength
    r_str = right_strength if right_strength is not None else strength

    if l_str <= 0 or r_str <= 0:
        raise ValueError("Strength parameters (strength, left_strength, right_strength) must be greater than 0.")

    if mode not in {"swing", "internal"}:
        raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")

    if data.empty:
        return []

    # Ensure normalized data
    if 'bar_index' not in data.columns or not isinstance(data.index, pd.DatetimeIndex):
        df = normalize_ohlcv(data)
    else:
        df = data

    n = len(df)
    if n < (l_str + r_str + 1):
        return []

    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    times = df.index
    bar_indices = df['bar_index'].to_numpy()

    detected_swings: List[SwingPoint] = []

    # Scan each bar i that has full left and right window available
    for i in range(l_str, n - r_str):
        cur_high = highs[i]
        cur_low = lows[i]

        # Check Pivot High
        # Left window: cur_high >= highs[i - k] for k in 1..l_str
        # Right window: cur_high > highs[i + k] for k in 1..r_str (strict for tie-breaking)
        is_pivot_high = True
        for k in range(1, l_str + 1):
            if highs[i - k] > cur_high:
                is_pivot_high = False
                break
        if is_pivot_high:
            for k in range(1, r_str + 1):
                if highs[i + k] >= cur_high:
                    is_pivot_high = False
                    break

        # Check Pivot Low
        # Left window: cur_low <= lows[i - k] for k in 1..l_str
        # Right window: cur_low < lows[i + k] for k in 1..r_str (strict for tie-breaking)
        is_pivot_low = True
        for k in range(1, l_str + 1):
            if lows[i - k] < cur_low:
                is_pivot_low = False
                break
        if is_pivot_low:
            for k in range(1, r_str + 1):
                if lows[i + k] <= cur_low:
                    is_pivot_low = False
                    break

        confirmed_idx = i + r_str
        confirmed_time = times[confirmed_idx]

        if is_pivot_high:
            sw_high = SwingPoint(
                index=int(bar_indices[i]),
                time=times[i],
                price=float(cur_high),
                kind="high",
                strength=r_str,
                mode=mode,
                confirmed_at=int(bar_indices[confirmed_idx]),
                confirmed_time=confirmed_time,
                classification="UNCLASSIFIED"
            )
            detected_swings.append(sw_high)

        if is_pivot_low:
            sw_low = SwingPoint(
                index=int(bar_indices[i]),
                time=times[i],
                price=float(cur_low),
                kind="low",
                strength=r_str,
                mode=mode,
                confirmed_at=int(bar_indices[confirmed_idx]),
                confirmed_time=confirmed_time,
                classification="UNCLASSIFIED"
            )
            detected_swings.append(sw_low)

    # Sort swings by confirmed_at ascending, then by index ascending
    detected_swings.sort(key=lambda s: (s.confirmed_at, s.index))

    # Perform Sequential Classification (HH, LH, HL, LL)
    prev_high: Optional[SwingPoint] = None
    prev_low: Optional[SwingPoint] = None

    for s in detected_swings:
        if s.kind == "high":
            if prev_high is None:
                s.classification = "UNCLASSIFIED"
            elif s.price > prev_high.price:
                s.classification = "HH"
            else:
                s.classification = "LH"
            prev_high = s
        elif s.kind == "low":
            if prev_low is None:
                s.classification = "UNCLASSIFIED"
            elif s.price > prev_low.price:
                s.classification = "HL"
            else:
                s.classification = "LL"
            prev_low = s

    # Enforce cutoff if current_bar_index or only_confirmed requested
    if current_bar_index is not None:
        return [s for s in detected_swings if s.confirmed_at <= current_bar_index]
    elif only_confirmed and n > 0:
        max_idx = int(bar_indices[-1])
        return [s for s in detected_swings if s.confirmed_at <= max_idx]

    return detected_swings


def get_confirmed_swings_at_bar(
    swings: List[SwingPoint],
    current_bar_index: int
) -> List[SwingPoint]:
    """
    Returns only the swings that have been confirmed on or before current_bar_index.
    Guarantees no lookahead bias during backtesting / replay.
    
    Args:
        swings: List of all detected SwingPoints.
        current_bar_index: The current simulation bar index.
        
    Returns:
        List of SwingPoints where confirmed_at <= current_bar_index.
    """
    return [s for s in swings if s.confirmed_at <= current_bar_index]


class SwingDetectorState:
    """
    Stateful O(1) incremental swing detector for bar-by-bar processing (replay or real-time stream).
    Guarantees zero lookahead bias by maintaining a rolling buffer of max size 
    (left_strength + right_strength + 1) and releasing swings only at the exact bar of confirmation.
    """
    def __init__(
        self,
        strength: int = 5,
        left_strength: Optional[int] = None,
        right_strength: Optional[int] = None,
        mode: Literal["swing", "internal"] = "swing"
    ):
        self.strength = strength
        self.left_strength = left_strength if left_strength is not None else strength
        self.right_strength = right_strength if right_strength is not None else strength
        self.mode: Literal["swing", "internal"] = mode

        if self.left_strength <= 0 or self.right_strength <= 0:
            raise ValueError("Strength parameters (strength, left_strength, right_strength) must be greater than 0.")

        if self.mode not in {"swing", "internal"}:
            raise ValueError(f"Invalid mode '{self.mode}'. Must be 'swing' or 'internal'.")

        self._bar_count: int = 0
        self._window_size: int = self.left_strength + self.right_strength + 1
        self._rolling_bars: List[Dict[str, Any]] = []
        self._confirmed_swings: List[SwingPoint] = []
        self._prev_high: Optional[SwingPoint] = None
        self._prev_low: Optional[SwingPoint] = None

    def update(self, candle: Union[Dict[str, Any], pd.Series]) -> List[SwingPoint]:
        """
        Feed a single new bar into the detector state and return any newly confirmed SwingPoints at this bar.
        Runs in O(left_strength + right_strength) time without historical re-scanning.
        
        Args:
            candle: Dictionary or Series containing OHLCV.
            
        Returns:
            List of newly confirmed SwingPoints at the current bar step.
        """
        if isinstance(candle, pd.Series):
            c_dict = candle.to_dict()
            if 'time' not in c_dict and isinstance(candle.name, (pd.Timestamp, str)):
                c_dict['time'] = candle.name
        else:
            c_dict = dict(candle)

        # Parse timestamp to UTC pd.Timestamp
        raw_time = c_dict.get('time')
        if isinstance(raw_time, pd.Timestamp):
            ts = raw_time if raw_time.tz is not None else raw_time.tz_localize('UTC')
        elif raw_time is not None:
            ts = pd.to_datetime(raw_time, utc=True)
        else:
            ts = pd.Timestamp.now(tz='UTC')

        bar_idx = self._bar_count
        self._bar_count += 1

        bar_item = {
            'bar_index': bar_idx,
            'time': ts,
            'high': float(c_dict['high']),
            'low': float(c_dict['low']),
            'open': float(c_dict.get('open', c_dict['high'])),
            'close': float(c_dict.get('close', c_dict['low'])),
            'volume': float(c_dict.get('volume', c_dict.get('tick_volume', 0.0)))
        }

        self._rolling_bars.append(bar_item)
        if len(self._rolling_bars) > self._window_size:
            self._rolling_bars.pop(0)

        # Check if we have enough bars to evaluate candidate pivot bar
        # Current bar index is bar_idx. Candidate bar is bar_idx - right_strength.
        if bar_idx < (self.left_strength + self.right_strength):
            return []

        pos = len(self._rolling_bars) - 1 - self.right_strength
        cand_bar = self._rolling_bars[pos]
        cand_high = cand_bar['high']
        cand_low = cand_bar['low']

        # Pivot High Check
        is_pivot_high = True
        for j in range(1, self.left_strength + 1):
            if self._rolling_bars[pos - j]['high'] > cand_high:
                is_pivot_high = False
                break
        if is_pivot_high:
            for j in range(1, self.right_strength + 1):
                if self._rolling_bars[pos + j]['high'] >= cand_high:
                    is_pivot_high = False
                    break

        # Pivot Low Check
        is_pivot_low = True
        for j in range(1, self.left_strength + 1):
            if self._rolling_bars[pos - j]['low'] < cand_low:
                is_pivot_low = False
                break
        if is_pivot_low:
            for j in range(1, self.right_strength + 1):
                if self._rolling_bars[pos + j]['low'] <= cand_low:
                    is_pivot_low = False
                    break

        newly_confirmed: List[SwingPoint] = []

        if is_pivot_high:
            sw_high = SwingPoint(
                index=cand_bar['bar_index'],
                time=cand_bar['time'],
                price=cand_high,
                kind="high",
                strength=self.right_strength,
                mode=self.mode,
                confirmed_at=bar_idx,
                confirmed_time=ts,
                classification="UNCLASSIFIED"
            )
            if self._prev_high is None:
                sw_high.classification = "UNCLASSIFIED"
            elif sw_high.price > self._prev_high.price:
                sw_high.classification = "HH"
            else:
                sw_high.classification = "LH"
            self._prev_high = sw_high
            newly_confirmed.append(sw_high)
            self._confirmed_swings.append(sw_high)

        if is_pivot_low:
            sw_low = SwingPoint(
                index=cand_bar['bar_index'],
                time=cand_bar['time'],
                price=cand_low,
                kind="low",
                strength=self.right_strength,
                mode=self.mode,
                confirmed_at=bar_idx,
                confirmed_time=ts,
                classification="UNCLASSIFIED"
            )
            if self._prev_low is None:
                sw_low.classification = "UNCLASSIFIED"
            elif sw_low.price > self._prev_low.price:
                sw_low.classification = "HL"
            else:
                sw_low.classification = "LL"
            self._prev_low = sw_low
            newly_confirmed.append(sw_low)
            self._confirmed_swings.append(sw_low)

        return newly_confirmed

    def get_confirmed_swings(self) -> List[SwingPoint]:
        """Returns all swings confirmed up to the current bar."""
        return list(self._confirmed_swings)

