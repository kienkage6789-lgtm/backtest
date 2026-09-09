import dataclasses
import pandas as pd
import numpy as np
from typing import List, Literal, Optional, Dict, Any, Union
from smc.models import SwingPoint, StructureEvent
from smc.data_contract import normalize_ohlcv
from smc.structure.swings import detect_swings

def calculate_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """
    Calculates True Range and ATR rolling average up to each bar without lookahead.
    
    Args:
        df: Normalized OHLCV DataFrame.
        period: ATR period (default 14).
        
    Returns:
        numpy array of ATR values corresponding to each bar.
    """
    n = len(df)
    if n == 0:
        return np.array([])
        
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)
        
    atr = np.zeros(n)
    if n >= period:
        # Simple rolling mean of True Range over period
        tr_series = pd.Series(tr)
        atr_series = tr_series.rolling(window=period, min_periods=period).mean()
        atr = atr_series.fillna(0.0).to_numpy()
        
    return atr


def detect_structure_events(
    data: Union[pd.DataFrame, List[Dict[str, Any]]],
    swings: Optional[List[SwingPoint]] = None,
    strength: int = 5,
    mode: Literal["swing", "internal"] = "swing",
    current_bar_index: Optional[int] = None,
    atr_period: int = 14,
    displacement_multiplier: float = 1.5,
    ambiguous_policy: Literal["skip"] = "skip"
) -> List[StructureEvent]:
    """
    Detects BOS (Break of Structure) and CHoCH (Change of Character) structure events.
    
    Args:
        data: Raw or normalized OHLCV data.
        swings: Optional pre-computed list of SwingPoints.
        strength: Swing strength parameter if swings are detected internally.
        mode: 'swing' or 'internal' structure mode.
        current_bar_index: Optional cutoff bar index.
        atr_period: Period for ATR calculation used in displacement.
        displacement_multiplier: Multiplier for body_size > ATR * multiplier.
        ambiguous_policy: Policy for bars breaking both active high and low ('skip').
        
    Returns:
        List of StructureEvent objects sorted by bar index ascending.
    """
    if strength <= 0:
        raise ValueError("Strength must be greater than 0.")
    if atr_period <= 0:
        raise ValueError("atr_period must be greater than 0.")
    if displacement_multiplier <= 0:
        raise ValueError("displacement_multiplier must be greater than 0.")
    if mode not in {"swing", "internal"}:
        raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")
    if ambiguous_policy not in {"skip"}:
        raise ValueError(f"Invalid ambiguous_policy '{ambiguous_policy}'. Must be 'skip'.")

    df = normalize_ohlcv(data) if not isinstance(data, pd.DataFrame) or 'bar_index' not in data.columns else data
    
    if df.empty:
        return []

    n = len(df)
    bar_indices = df['bar_index'].to_numpy()
    
    # Calculate ATR
    atr_values = calculate_atr(df, period=atr_period)

    # Detect swings if not provided, or clone existing swings to preserve immutability
    if swings is None:
        raw_swings = detect_swings(df, strength=strength, mode=mode)
    else:
        raw_swings = [dataclasses.replace(s) for s in swings]

    # Filter swings matching mode
    filtered_swings = [s for s in raw_swings if s.mode == mode]

    # Map swings by confirmed_at
    swings_by_confirmed_at: Dict[int, List[SwingPoint]] = {}
    for s in filtered_swings:
        swings_by_confirmed_at.setdefault(s.confirmed_at, []).append(s)

    events: List[StructureEvent] = []
    trend: Optional[Literal["bullish", "bearish"]] = None
    active_high: Optional[SwingPoint] = None
    active_low: Optional[SwingPoint] = None

    # Determine limit
    limit_n = n
    if current_bar_index is not None:
        limit_n = min(n, current_bar_index + 1)

    for k in range(limit_n):
        bar_idx = int(bar_indices[k])
        c_open = float(df['open'].iloc[k])
        c_high = float(df['high'].iloc[k])
        c_low = float(df['low'].iloc[k])
        c_close = float(df['close'].iloc[k])
        c_time = df.index[k]

        # Ingest new swings confirmed at bar_idx
        if bar_idx in swings_by_confirmed_at:
            for sw in swings_by_confirmed_at[bar_idx]:
                if sw.kind == "high" and not sw.broken:
                    if active_high is None or sw.confirmed_at >= active_high.confirmed_at:
                        active_high = sw
                elif sw.kind == "low" and not sw.broken:
                    if active_low is None or sw.confirmed_at >= active_low.confirmed_at:
                        active_low = sw

        # Check Close Strict Break
        is_bullish_break = (
            active_high is not None and 
            not active_high.broken and 
            c_close > active_high.price
        )
        is_bearish_break = (
            active_low is not None and 
            not active_low.broken and 
            c_close < active_low.price
        )

        # Ambiguous candle handling (candle breaches both active high and active low)
        is_ambiguous = (
            (active_high is not None and not active_high.broken and c_high > active_high.price) and
            (active_low is not None and not active_low.broken and c_low < active_low.price)
        ) or (is_bullish_break and is_bearish_break)
        
        if is_ambiguous:
            if ambiguous_policy == "skip":
                continue

        if is_bullish_break and not is_bearish_break:
            event_type: Literal["BOS", "CHoCH"] = "CHoCH" if trend == "bearish" else "BOS"
            direction: Literal["bullish", "bearish"] = "bullish"
            trend = "bullish"

            body_size = abs(c_close - c_open)
            atr_val = atr_values[k]
            displacement = (k >= (atr_period - 1)) and (atr_val > 0.0) and (body_size > atr_val * displacement_multiplier)

            event = StructureEvent(
                index=bar_idx,
                time=c_time,
                event_type=event_type,
                direction=direction,
                broken_swing_index=active_high.index,
                broken_swing_price=active_high.price,
                close_price=c_close,
                displacement=displacement,
                mode=mode,
                confirmed_swing_at=active_high.confirmed_at,
                body_size=body_size,
                atr_value=float(atr_val),
                break_type="close"
            )
            events.append(event)
            active_high.broken = True
            active_high.broken_at = bar_idx
            active_high = None

        elif is_bearish_break and not is_bullish_break:
            event_type: Literal["BOS", "CHoCH"] = "CHoCH" if trend == "bullish" else "BOS"
            direction: Literal["bullish", "bearish"] = "bearish"
            trend = "bearish"

            body_size = abs(c_close - c_open)
            atr_val = atr_values[k]
            displacement = (k >= (atr_period - 1)) and (atr_val > 0.0) and (body_size > atr_val * displacement_multiplier)

            event = StructureEvent(
                index=bar_idx,
                time=c_time,
                event_type=event_type,
                direction=direction,
                broken_swing_index=active_low.index,
                broken_swing_price=active_low.price,
                close_price=c_close,
                displacement=displacement,
                mode=mode,
                confirmed_swing_at=active_low.confirmed_at,
                body_size=body_size,
                atr_value=float(atr_val),
                break_type="close"
            )
            events.append(event)
            active_low.broken = True
            active_low.broken_at = bar_idx
            active_low = None

    return events


class StructureTracker:
    """
    Incremental stateful Structure Tracker for O(1) step-by-step bar updates in Replay or Streaming mode.
    Maintains trend state, active high/low, rolling ATR, and emits new StructureEvents as they occur.
    """
    def __init__(
        self,
        mode: Literal["swing", "internal"] = "swing",
        atr_period: int = 14,
        displacement_multiplier: float = 1.5,
        ambiguous_policy: Literal["skip"] = "skip"
    ):
        if atr_period <= 0:
            raise ValueError("atr_period must be greater than 0.")
        if displacement_multiplier <= 0:
            raise ValueError("displacement_multiplier must be greater than 0.")
        if mode not in {"swing", "internal"}:
            raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")
        if ambiguous_policy not in {"skip"}:
            raise ValueError(f"Invalid ambiguous_policy '{ambiguous_policy}'. Must be 'skip'.")

        self.mode: Literal["swing", "internal"] = mode
        self.atr_period = atr_period
        self.displacement_multiplier = displacement_multiplier
        self.ambiguous_policy = ambiguous_policy

        self.trend: Optional[Literal["bullish", "bearish"]] = None
        self.active_high: Optional[SwingPoint] = None
        self.active_low: Optional[SwingPoint] = None
        self._events: List[StructureEvent] = []
        self._last_processed_bar: int = -1
        self._last_close: Optional[float] = None
        self._tr_history: List[float] = []

    def update(
        self,
        candle: Union[Dict[str, Any], pd.Series],
        confirmed_swings: Optional[List[SwingPoint]] = None
    ) -> List[StructureEvent]:
        """
        Feeds a new closed candle and newly confirmed swings into the tracker.
        
        Args:
            candle: Candle dictionary or Series. Must contain 'bar_index' or be fed sequentially.
            confirmed_swings: List of new SwingPoints confirmed at this candle's bar step.
            
        Returns:
            List of newly emitted StructureEvents at this candle step.
        """
        if isinstance(candle, pd.Series):
            c_dict = candle.to_dict()
            if 'time' not in c_dict and isinstance(candle.name, (pd.Timestamp, str)):
                c_dict['time'] = candle.name
        else:
            c_dict = dict(candle)

        bar_idx = c_dict.get('bar_index', self._last_processed_bar + 1)
        if bar_idx <= self._last_processed_bar:
            raise ValueError(f"Bar index {bar_idx} must be strictly greater than last_processed_bar {self._last_processed_bar}.")

        self._last_processed_bar = bar_idx

        raw_time = c_dict.get('time')
        if isinstance(raw_time, pd.Timestamp):
            ts = raw_time if raw_time.tz is not None else raw_time.tz_localize('UTC')
        elif raw_time is not None:
            ts = pd.to_datetime(raw_time, utc=True)
        else:
            ts = pd.Timestamp.now(tz='UTC')

        c_open = float(c_dict['open'])
        c_high = float(c_dict['high'])
        c_low = float(c_dict['low'])
        c_close = float(c_dict['close'])

        # Calculate True Range for current candle
        if self._last_close is None:
            tr = c_high - c_low
        else:
            tr = max(c_high - c_low, abs(c_high - self._last_close), abs(c_low - self._last_close))
        self._last_close = c_close

        self._tr_history.append(tr)
        if len(self._tr_history) > self.atr_period:
            self._tr_history.pop(0)

        atr_val = sum(self._tr_history) / self.atr_period if len(self._tr_history) == self.atr_period else 0.0

        # Ingest new confirmed swings (clone to prevent side-effects on caller objects)
        if confirmed_swings:
            for sw in confirmed_swings:
                if sw.mode == self.mode and sw.confirmed_at <= bar_idx and not sw.broken:
                    sw_copy = dataclasses.replace(sw)
                    if sw.kind == "high":
                        if self.active_high is None or sw_copy.confirmed_at >= self.active_high.confirmed_at:
                            self.active_high = sw_copy
                    elif sw.kind == "low":
                        if self.active_low is None or sw_copy.confirmed_at >= self.active_low.confirmed_at:
                            self.active_low = sw_copy

        # Check Close Strict Break
        is_bullish_break = (
            self.active_high is not None and 
            not self.active_high.broken and 
            c_close > self.active_high.price
        )
        is_bearish_break = (
            self.active_low is not None and 
            not self.active_low.broken and 
            c_close < self.active_low.price
        )

        newly_emitted: List[StructureEvent] = []

        # Ambiguous candle handling (candle breaches both active high and active low)
        is_ambiguous = (
            (self.active_high is not None and not self.active_high.broken and c_high > self.active_high.price) and
            (self.active_low is not None and not self.active_low.broken and c_low < self.active_low.price)
        ) or (is_bullish_break and is_bearish_break)

        if is_ambiguous:
            if self.ambiguous_policy == "skip":
                return []

        if is_bullish_break and not is_bearish_break:
            event_type: Literal["BOS", "CHoCH"] = "CHoCH" if self.trend == "bearish" else "BOS"
            direction: Literal["bullish", "bearish"] = "bullish"
            self.trend = "bullish"

            body_size = abs(c_close - c_open)
            displacement = (atr_val > 0.0) and (body_size > atr_val * self.displacement_multiplier)

            event = StructureEvent(
                index=bar_idx,
                time=ts,
                event_type=event_type,
                direction=direction,
                broken_swing_index=self.active_high.index,
                broken_swing_price=self.active_high.price,
                close_price=c_close,
                displacement=displacement,
                mode=self.mode,
                confirmed_swing_at=self.active_high.confirmed_at,
                body_size=body_size,
                atr_value=float(atr_val),
                break_type="close"
            )
            self.active_high.broken = True
            self.active_high.broken_at = bar_idx
            self.active_high = None
            newly_emitted.append(event)
            self._events.append(event)

        elif is_bearish_break and not is_bullish_break:
            event_type: Literal["BOS", "CHoCH"] = "CHoCH" if self.trend == "bullish" else "BOS"
            direction: Literal["bullish", "bearish"] = "bearish"
            self.trend = "bearish"

            body_size = abs(c_close - c_open)
            displacement = (atr_val > 0.0) and (body_size > atr_val * self.displacement_multiplier)

            event = StructureEvent(
                index=bar_idx,
                time=ts,
                event_type=event_type,
                direction=direction,
                broken_swing_index=self.active_low.index,
                broken_swing_price=self.active_low.price,
                close_price=c_close,
                displacement=displacement,
                mode=self.mode,
                confirmed_swing_at=self.active_low.confirmed_at,
                body_size=body_size,
                atr_value=float(atr_val),
                break_type="close"
            )
            self.active_low.broken = True
            self.active_low.broken_at = bar_idx
            self.active_low = None
            newly_emitted.append(event)
            self._events.append(event)

        return newly_emitted

    def get_state(self) -> Dict[str, Any]:
        """Returns current tracker state dictionary."""
        return {
            "mode": self.mode,
            "trend": self.trend,
            "active_high": self.active_high.to_dict() if self.active_high else None,
            "active_low": self.active_low.to_dict() if self.active_low else None,
            "last_processed_bar": self._last_processed_bar,
            "events_count": len(self._events)
        }

    def get_events(self) -> List[StructureEvent]:
        """Returns list of all emitted StructureEvents."""
        return list(self._events)
