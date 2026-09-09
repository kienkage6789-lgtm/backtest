from dataclasses import dataclass, field, asdict
from typing import Literal, Optional, Any
import pandas as pd

@dataclass
class SwingPoint:
    """
    Represents a Swing High or Swing Low point.
    
    Attributes:
        index: Bar index (0-based) where the peak or trough occurred.
        time: Timestamp of the peak or trough bar.
        price: High price (if kind='high') or Low price (if kind='low').
        kind: 'high' or 'low'.
        strength: Confirmation depth (right_strength used during detection).
        confirmed_at: Bar index when this swing point was confirmed (index + right_strength).
        confirmed_time: Timestamp of the bar when confirmation occurred.
        classification: Structural classification ('HH', 'HL', 'LH', 'LL', 'UNCLASSIFIED').
        broken: Whether this swing point has been broken by a future close.
        broken_at: Bar index where the break occurred.
    """
    index: int
    time: pd.Timestamp
    price: float
    kind: Literal["high", "low"]
    strength: int
    confirmed_at: int
    confirmed_time: Optional[pd.Timestamp] = None
    mode: Literal["swing", "internal"] = "swing"
    classification: Literal["HH", "HL", "LH", "LL", "UNCLASSIFIED"] = "UNCLASSIFIED"
    broken: bool = False
    broken_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert SwingPoint to a JSON-serializable dictionary."""
        return {
            "index": int(self.index),
            "time": self.time.isoformat() if isinstance(self.time, pd.Timestamp) else str(self.time),
            "price": float(self.price),
            "kind": str(self.kind),
            "strength": int(self.strength),
            "mode": str(self.mode),
            "confirmed_at": int(self.confirmed_at),
            "confirmed_time": self.confirmed_time.isoformat() if isinstance(self.confirmed_time, pd.Timestamp) else (str(self.confirmed_time) if self.confirmed_time is not None else None),
            "classification": str(self.classification),
            "broken": bool(self.broken),
            "broken_at": int(self.broken_at) if self.broken_at is not None else None
        }


@dataclass
class StructureEvent:
    """
    Represents a BOS (Break of Structure) or CHoCH (Change of Character) event.
    """
    index: int
    time: pd.Timestamp
    event_type: Literal["BOS", "CHoCH"]
    direction: Literal["bullish", "bearish"]
    broken_swing_index: int
    broken_swing_price: float
    close_price: float
    displacement: bool = False
    mode: Literal["swing", "internal"] = "swing"
    confirmed_swing_at: int = 0
    body_size: float = 0.0
    atr_value: float = 0.0
    break_type: str = "close"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat() if isinstance(self.time, pd.Timestamp) else str(self.time),
            "event_type": str(self.event_type),
            "direction": str(self.direction),
            "broken_swing_index": int(self.broken_swing_index),
            "broken_swing_price": float(self.broken_swing_price),
            "close_price": float(self.close_price),
            "displacement": bool(self.displacement),
            "mode": str(self.mode),
            "confirmed_swing_at": int(self.confirmed_swing_at),
            "body_size": float(self.body_size),
            "atr_value": float(self.atr_value),
            "break_type": str(self.break_type)
        }


@dataclass
class FairValueGap:
    """
    Represents a Fair Value Gap (FVG) / Imbalance zone.

    A Bullish FVG exists when bar[i-1].high < bar[i+1].low (gap above).
    A Bearish FVG exists when bar[i-1].low > bar[i+1].high (gap below).
    The FVG is confirmed when bar i+1 closes (confirmed_at = bar[i+1].index).

    Attributes:
        index: Bar index of the middle candle that creates the gap.
        time: Timestamp of the middle candle.
        direction: 'bullish' (gap above) or 'bearish' (gap below).
        top: Upper price boundary of the gap.
        bottom: Lower price boundary of the gap.
        mode: 'swing' or 'internal' — mirrors the swing/structure context.
        confirmed_at: Bar index when this FVG was confirmed (index of right bar).
        filled: Whether the gap has been fully filled by price.
        filled_at: Bar index when fully filled, or None.
    """
    index: int
    time: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    mode: Literal["swing", "internal"] = "swing"
    confirmed_at: int = 0
    filled: bool = False
    filled_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat() if isinstance(self.time, pd.Timestamp) else str(self.time),
            "direction": str(self.direction),
            "top": float(self.top),
            "bottom": float(self.bottom),
            "mode": str(self.mode),
            "confirmed_at": int(self.confirmed_at),
            "filled": bool(self.filled),
            "filled_at": int(self.filled_at) if self.filled_at is not None else None,
        }


@dataclass
class OrderBlock:
    """
    Represents an Order Block (OB) zone derived from a StructureEvent.

    An Order Block is the last opposite-direction candle immediately before
    a displacement leg that creates a BOS or CHoCH.

    Attributes:
        index: Bar index of the source (origin) candle.
        time: Timestamp of the source candle.
        direction: 'bullish' (bearish source candle before bullish BOS/CHoCH)
                   or 'bearish' (bullish source candle before bearish BOS/CHoCH).
        high, low, open, close: OHLC of the source candle.
        origin_type: 'BOS' or 'CHoCH' — from the triggering StructureEvent.
        mode: 'swing' or 'internal'.
        quality: 'base', 'strong', or 'premium_candidate'.
        source_event_index: Index of the triggering StructureEvent.
        source_event_type: 'BOS' or 'CHoCH'.
        source_swing_index: Index of the broken swing from the StructureEvent.
        source_fvg_index: Index of the linked FVG (middle candle), or None.
        source_fvg_top: Top of the linked FVG, or None.
        source_fvg_bottom: Bottom of the linked FVG, or None.
        mitigated: Whether price has touched the OB zone.
        mitigated_at: Bar index of first mitigation touch, or None.
        mitigation_pct: 0.0–1.0, how deeply price has entered the zone.
        valid: Whether the OB is still valid (not invalidated by close break).
        invalidated_at: Bar index of invalidation, or None.
        invalidation_reason: Reason string ('close_break'), or None.
        retest_count: Number of times price has retested the zone.
    """
    index: int
    time: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    high: float
    low: float
    open: float
    close: float
    origin_type: Literal["BOS", "CHoCH"]
    mode: Literal["swing", "internal"] = "swing"
    quality: Literal["base", "strong", "premium_candidate"] = "base"
    source_event_index: int = -1
    source_event_type: str = ""
    source_swing_index: int = -1
    created_at: int = -1
    source_fvg_index: Optional[int] = None
    source_fvg_top: Optional[float] = None
    source_fvg_bottom: Optional[float] = None
    mitigated: bool = False
    mitigated_at: Optional[int] = None
    mitigation_pct: float = 0.0
    valid: bool = True
    invalidated_at: Optional[int] = None
    invalidation_reason: Optional[str] = None
    retest_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat() if isinstance(self.time, pd.Timestamp) else str(self.time),
            "direction": str(self.direction),
            "high": float(self.high),
            "low": float(self.low),
            "open": float(self.open),
            "close": float(self.close),
            "origin_type": str(self.origin_type),
            "mode": str(self.mode),
            "quality": str(self.quality),
            "source_event_index": int(self.source_event_index),
            "source_event_type": str(self.source_event_type),
            "source_swing_index": int(self.source_swing_index),
            "created_at": int(self.created_at if self.created_at != -1 else self.source_event_index),
            "source_fvg_index": int(self.source_fvg_index) if self.source_fvg_index is not None else None,
            "source_fvg_top": float(self.source_fvg_top) if self.source_fvg_top is not None else None,
            "source_fvg_bottom": float(self.source_fvg_bottom) if self.source_fvg_bottom is not None else None,
            "mitigated": bool(self.mitigated),
            "mitigated_at": int(self.mitigated_at) if self.mitigated_at is not None else None,
            "mitigation_pct": float(self.mitigation_pct),
            "valid": bool(self.valid),
            "invalidated_at": int(self.invalidated_at) if self.invalidated_at is not None else None,
            "invalidation_reason": str(self.invalidation_reason) if self.invalidation_reason is not None else None,
            "retest_count": int(self.retest_count),
        }
