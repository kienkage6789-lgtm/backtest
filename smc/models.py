from dataclasses import dataclass, field, asdict
from typing import Literal, Optional, Any, Union, Set, FrozenSet
from collections.abc import Mapping
from types import MappingProxyType
import datetime
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
    structure_leg_id: Optional[str] = None

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
            "break_type": str(self.break_type),
            "structure_leg_id": self.structure_leg_id,
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
    structure_leg_id: Optional[str] = None
    # Additive FVG quality/lifecycle metadata.  These are appended so the
    # historical positional constructor remains source compatible.
    ce: Optional[float] = None
    gap_size: Optional[float] = None
    gap_pct: Optional[float] = None
    displacement: bool = False
    body_ratio: float = 0.0
    atr_value: float = 0.0
    touch_count: int = 0
    ce_touched: bool = False
    ce_touched_at: Optional[int] = None
    partial_filled: bool = False
    partial_filled_at: Optional[int] = None
    middle_body_size: float = 0.0
    middle_range: float = 0.0
    middle_body_ratio: float = 0.0
    state: str = "active"

    def __post_init__(self):
        size = float(self.top) - float(self.bottom)
        ce = (float(self.top) + float(self.bottom)) / 2.0
        object.__setattr__(self, "ce", ce if self.ce is None else float(self.ce))
        object.__setattr__(self, "gap_size", size if self.gap_size is None else float(self.gap_size))
        object.__setattr__(self, "gap_pct", (size / abs(float(self.bottom))) if self.gap_pct is None and self.bottom != 0 else (0.0 if self.gap_pct is None else float(self.gap_pct)))
        if self.filled_at is not None:
            object.__setattr__(self, "filled", True)
        object.__setattr__(self, "touch_count", max(0, int(self.touch_count)))
        object.__setattr__(self, "state", "filled" if self.filled else ("partial" if self.partial_filled else str(self.state)))

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
            "structure_leg_id": self.structure_leg_id,
            "ce": float(self.ce), "gap_size": float(self.gap_size), "gap_pct": float(self.gap_pct),
            "displacement": bool(self.displacement), "body_ratio": float(self.body_ratio),
            "atr_value": float(self.atr_value), "touch_count": int(self.touch_count),
            "ce_touched": bool(self.ce_touched), "ce_touched_at": self.ce_touched_at,
            "partial_filled": bool(self.partial_filled), "partial_filled_at": self.partial_filled_at,
            "middle_body_size": float(self.middle_body_size), "middle_range": float(self.middle_range),
            "middle_body_ratio": float(self.middle_body_ratio), "state": self.state,
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
    structure_leg_id: Optional[str] = None

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
            "structure_leg_id": self.structure_leg_id,
        }


@dataclass
class LiquidityPool:
    """
    Represents a Liquidity Pool formed by equal highs, equal lows, or swing points.

    Attributes:
        kind: 'equal_highs', 'equal_lows', 'swing_high', 'swing_low'.
        price: Representative price level of the pool (mean of source swing prices).
        price_max: Maximum price bound among source swings.
        price_min: Minimum price bound among source swings.
        indices: Bar indices of the source swings / candles forming the pool.
        created_at: Bar index when the pool was created (max confirmed_at of source swings).
        confirmed_at: Bar index when the pool was confirmed (equal to created_at).
        swept: Whether this pool has been swept by price wick.
        swept_at: Bar index when swept, or None.
        sweep_type: 'clean' or 'wick_only' or None.
        valid: Whether the pool is still valid (not broken by close or invalidated).
        invalidated_at: Bar index of invalidation (e.g. close break), or None.
        invalidation_reason: 'close_break', 'swept', etc., or None.
        mode: 'swing' or 'internal'.
        source_swings: Metadata list of source swing dictionaries or objects.
        structure_leg_id: Optional leg ID.
    """
    kind: Literal["equal_highs", "equal_lows", "swing_high", "swing_low"]
    price: float
    price_max: float
    price_min: float
    indices: list[int]
    created_at: int
    confirmed_at: int
    swept: bool = False
    swept_at: Optional[int] = None
    sweep_type: Optional[Literal["clean", "wick_only"]] = None
    valid: bool = True
    invalidated_at: Optional[int] = None
    invalidation_reason: Optional[str] = None
    mode: Literal["swing", "internal"] = "swing"
    source_swings: list[dict[str, Any]] = field(default_factory=list)
    structure_leg_id: Optional[str] = None
    liquidity_side: Optional[Literal["BUY_SIDE", "SELL_SIDE"]] = None

    def __post_init__(self):
        if self.liquidity_side is None:
            if self.kind in ("equal_lows", "swing_low"):
                self.liquidity_side = "SELL_SIDE"
            elif self.kind in ("equal_highs", "swing_high"):
                self.liquidity_side = "BUY_SIDE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "price": float(self.price),
            "price_max": float(self.price_max),
            "price_min": float(self.price_min),
            "indices": [int(idx) for idx in self.indices],
            "created_at": int(self.created_at),
            "confirmed_at": int(self.confirmed_at),
            "swept": bool(self.swept),
            "swept_at": int(self.swept_at) if self.swept_at is not None else None,
            "sweep_type": str(self.sweep_type) if self.sweep_type is not None else None,
            "valid": bool(self.valid),
            "invalidated_at": int(self.invalidated_at) if self.invalidated_at is not None else None,
            "invalidation_reason": str(self.invalidation_reason) if self.invalidation_reason is not None else None,
            "mode": str(self.mode),
            "source_swings": list(self.source_swings),
            "structure_leg_id": self.structure_leg_id,
            "liquidity_side": str(self.liquidity_side) if self.liquidity_side is not None else None,
        }


@dataclass
class LiquiditySweep:
    """
    Represents a Liquidity Sweep event where price wicks through a LiquidityPool
    and closes back inside the structure.

    Attributes:
        index: Bar index of the sweeping candle.
        time: Timestamp of the sweeping candle.
        direction: 'bullish' (swept equal_lows/swing_low and closed back above)
                   or 'bearish' (swept equal_highs/swing_high and closed back below).
        pool_kind: Kind of the swept pool ('equal_highs', 'equal_lows', etc.).
        pool_price: Price level of the swept pool.
        pool_indices: Bar indices forming the source pool.
        price_wick: Maximum wick price penetrating the pool (high for bearish, low for bullish).
        close_price: Close price of the sweeping candle.
        created_at: Bar index of the sweep event confirmation (index of sweeping candle).
        confirmed_at: Same as created_at (index of sweeping candle).
        swept_at: Same as index of sweeping candle.
        sweep_type: 'clean' or 'wick_only'.
        valid: Whether the sweep signal remains valid.
        mode: 'swing' or 'internal'.
        structure_leg_id: Optional structure leg ID.
        liquidity_side: 'BUY_SIDE' or 'SELL_SIDE'.
        raid_direction: 'bullish' (spiked up into buy stops) or 'bearish' (spiked down into sell stops).
        reversal_direction: 'bullish' or 'bearish' (direction of expected reversal setup).
    """
    index: int
    time: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    pool_kind: str
    pool_price: float
    pool_indices: list[int]
    price_wick: float
    close_price: float
    created_at: int
    confirmed_at: int
    swept_at: int
    sweep_type: Literal["clean", "wick_only"] = "clean"
    valid: bool = True
    mode: Literal["swing", "internal"] = "swing"
    structure_leg_id: Optional[str] = None
    liquidity_side: Optional[Literal["BUY_SIDE", "SELL_SIDE"]] = None
    raid_direction: Optional[Literal["bullish", "bearish"]] = None
    reversal_direction: Optional[Literal["bullish", "bearish"]] = None

    def __post_init__(self):
        if self.liquidity_side is None:
            if self.pool_kind in ("equal_lows", "swing_low"):
                self.liquidity_side = "SELL_SIDE"
            elif self.pool_kind in ("equal_highs", "swing_high"):
                self.liquidity_side = "BUY_SIDE"
        if self.reversal_direction is None:
            self.reversal_direction = self.direction
        if self.raid_direction is None:
            self.raid_direction = "bearish" if self.liquidity_side == "SELL_SIDE" else "bullish"

    def is_semantically_valid(self) -> bool:
        if self.pool_kind in ("equal_highs", "swing_high") or self.liquidity_side == "BUY_SIDE":
            return (
                self.direction == "bearish"
                and (self.reversal_direction is None or self.reversal_direction == "bearish")
                and (self.raid_direction is None or self.raid_direction == "bullish")
                and (self.liquidity_side is None or self.liquidity_side == "BUY_SIDE")
            )
        if self.pool_kind in ("equal_lows", "swing_low") or self.liquidity_side == "SELL_SIDE":
            return (
                self.direction == "bullish"
                and (self.reversal_direction is None or self.reversal_direction == "bullish")
                and (self.raid_direction is None or self.raid_direction == "bearish")
                and (self.liquidity_side is None or self.liquidity_side == "SELL_SIDE")
            )
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat() if isinstance(self.time, pd.Timestamp) else str(self.time),
            "direction": str(self.direction),
            "pool_kind": str(self.pool_kind),
            "pool_price": float(self.pool_price),
            "pool_indices": [int(idx) for idx in self.pool_indices],
            "price_wick": float(self.price_wick),
            "close_price": float(self.close_price),
            "created_at": int(self.created_at),
            "confirmed_at": int(self.confirmed_at),
            "swept_at": int(self.swept_at),
            "sweep_type": str(self.sweep_type),
            "valid": bool(self.valid),
            "mode": str(self.mode),
            "structure_leg_id": self.structure_leg_id,
            "liquidity_side": str(self.liquidity_side) if self.liquidity_side is not None else None,
            "raid_direction": str(self.raid_direction) if self.raid_direction is not None else None,
            "reversal_direction": str(self.reversal_direction) if self.reversal_direction is not None else None,
        }


@dataclass
class Signal:
    """
    Represents a discrete filter or decision signal emitted by SMC context/confluence modules.

    Attributes:
        name: Unique signal identifier (e.g. 'in_killzone', 'htf_bias_bullish').
        value: Boolean active/triggered flag.
        weight: Importance weight for confluence scoring.
        confidence: Confidence score between 0.0 and 1.0.
        meta: Immutable metadata mapping containing diagnostic context.
    """
    name: str
    value: bool
    weight: float = 1.0
    confidence: float = 1.0
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.meta, MappingProxyType):
            object.__setattr__(self, 'meta', MappingProxyType(dict(self.meta)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": str(self.name),
            "value": bool(self.value),
            "weight": float(self.weight),
            "confidence": float(self.confidence),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True)
class SessionWindow:
    """
    Defines a trading session or Kill Zone time window.

    Attributes:
        name: Name of the session (e.g. 'london_killzone', 'newyork_killzone').
        start: Start time (inclusive).
        end: End time (exclusive). If start > end, window crosses midnight.
        timezone: IANA timezone string (default 'UTC').
        days: Optional set of allowed weekdays (0=Monday, ..., 6=Sunday).
    """
    name: str
    start: datetime.time
    end: datetime.time
    timezone: str = "UTC"
    days: Optional[frozenset[int]] = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("SessionWindow name must be a non-empty string.")
        if not isinstance(self.start, datetime.time):
            raise ValueError("SessionWindow start must be a datetime.time object.")
        if not isinstance(self.end, datetime.time):
            raise ValueError("SessionWindow end must be a datetime.time object.")
        if self.days is not None:
            if not isinstance(self.days, (frozenset, set, tuple, list)):
                raise ValueError("SessionWindow days must be an iterable of integers (0=Monday..6=Sunday).")
            valid_days = frozenset(int(d) for d in self.days)
            for d in valid_days:
                if d < 0 or d > 6:
                    raise ValueError(f"Invalid weekday {d}. Weekday must be between 0 (Monday) and 6 (Sunday).")
            object.__setattr__(self, 'days', valid_days)


@dataclass(frozen=True)
class SessionDecision:
    """
    Represents the evaluation result of a candle against a SessionWindow or session collection.

    Attributes:
        in_session: Whether the candle is inside the allowed session window.
        session_name: Name of the matched session, or None if outside.
        timestamp: Normalized UTC timestamp of the evaluated candle.
        reason: Diagnostic reason string (e.g. 'inside_session', 'outside_session', 'at_session_end').
        meta: Immutable metadata mapping.
    """
    in_session: bool
    session_name: Optional[str]
    timestamp: pd.Timestamp
    reason: str
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.meta, MappingProxyType):
            object.__setattr__(self, 'meta', MappingProxyType(dict(self.meta)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "in_session": bool(self.in_session),
            "session_name": self.session_name,
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, pd.Timestamp) else str(self.timestamp),
            "reason": str(self.reason),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True)
class BiasState:
    """
    Represents the Higher Timeframe (HTF) trend bias mapped as-of a Lower Timeframe (LTF) candle.

    Attributes:
        bias: 'bullish', 'bearish', or 'neutral'.
        timestamp: Normalized UTC timestamp of the LTF candle.
        source_event_index: Bar index of the latest confirmed HTF StructureEvent, or None.
        source_event_time: Timestamp of the latest confirmed HTF StructureEvent, or None.
        source_event_type: 'BOS', 'CHoCH', or None.
        source_event_direction: Direction of the source event, or None.
        as_of: Timestamp as-of which the HTF state was evaluated.
        reason: Diagnostic string explaining why this bias was assigned.
        meta: Immutable metadata mapping.
    """
    bias: Literal["bullish", "bearish", "neutral"]
    timestamp: pd.Timestamp
    source_event_index: Optional[int] = None
    source_event_time: Optional[pd.Timestamp] = None
    source_event_type: Optional[str] = None
    source_event_direction: Optional[str] = None
    as_of: Optional[pd.Timestamp] = None
    reason: str = "ok"
    pending_reversal: Optional[str] = None
    pending_reversal_event_type: Optional[str] = None
    pending_reversal_event_index: Optional[int] = None
    pending_reversal_event_time: Optional[pd.Timestamp] = None
    confirmed_by_bos: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.bias not in {"bullish", "bearish", "neutral"}:
            raise ValueError(f"Invalid bias '{self.bias}'. Must be 'bullish', 'bearish', or 'neutral'.")
        if not isinstance(self.meta, MappingProxyType):
            object.__setattr__(self, 'meta', MappingProxyType(dict(self.meta)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": str(self.bias),
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, pd.Timestamp) else str(self.timestamp),
            "source_event_index": int(self.source_event_index) if self.source_event_index is not None else None,
            "source_event_time": self.source_event_time.isoformat() if isinstance(self.source_event_time, pd.Timestamp) else (str(self.source_event_time) if self.source_event_time is not None else None),
            "source_event_type": str(self.source_event_type) if self.source_event_type is not None else None,
            "source_event_direction": str(self.source_event_direction) if self.source_event_direction is not None else None,
            "as_of": self.as_of.isoformat() if isinstance(self.as_of, pd.Timestamp) else (str(self.as_of) if self.as_of is not None else None),
            "reason": str(self.reason),
            "pending_reversal": str(self.pending_reversal) if self.pending_reversal is not None else None,
            "pending_reversal_event_type": str(self.pending_reversal_event_type) if self.pending_reversal_event_type is not None else None,
            "pending_reversal_event_index": int(self.pending_reversal_event_index) if self.pending_reversal_event_index is not None else None,
            "pending_reversal_event_time": self.pending_reversal_event_time.isoformat() if isinstance(self.pending_reversal_event_time, pd.Timestamp) else (str(self.pending_reversal_event_time) if self.pending_reversal_event_time is not None else None),
            "confirmed_by_bos": bool(self.confirmed_by_bos),
            "effective_time": self.source_event_time.isoformat() if isinstance(self.source_event_time, pd.Timestamp) else (str(self.source_event_time) if self.source_event_time is not None else None),
            "meta": dict(self.meta),
        }


@dataclass
class HTFPOI:
    """
    Represents a Higher Timeframe Point of Interest (HTF POI) zone (FVG or OB).
    HTF POIs provide macro context for Lower Timeframe strategy entries.
    """
    poi_id: str
    poi_type: Literal["FVG", "OB"]
    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    timeframe: str = "H1"
    created_at: Any = None
    source_event: Optional[Any] = None
    valid_until: Optional[Any] = None
    status: Literal["active", "invalidated", "mitigated"] = "active"
    touch_count: int = 0
    last_touch_bar: Optional[int] = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.poi_type not in {"FVG", "OB"}:
            raise ValueError(f"Invalid poi_type '{self.poi_type}'. Must be 'FVG' or 'OB'.")
        if self.direction not in {"bullish", "bearish"}:
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'bullish' or 'bearish'.")
        if float(self.top) < float(self.bottom):
            raise ValueError(f"POI top {self.top} cannot be less than bottom {self.bottom}.")
        if not isinstance(self.meta, MappingProxyType):
            object.__setattr__(self, 'meta', MappingProxyType(dict(self.meta)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "poi_id": str(self.poi_id),
            "poi_type": str(self.poi_type),
            "direction": str(self.direction),
            "timeframe": str(self.timeframe),
            "top": float(self.top),
            "bottom": float(self.bottom),
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, pd.Timestamp) else self.created_at,
            "source_event": self.source_event.to_dict() if hasattr(self.source_event, "to_dict") else self.source_event,
            "valid_until": self.valid_until.isoformat() if isinstance(self.valid_until, pd.Timestamp) else self.valid_until,
            "status": str(self.status),
            "touch_count": int(self.touch_count),
            "last_touch_bar": int(self.last_touch_bar) if self.last_touch_bar is not None else None,
            "meta": dict(self.meta),
        }
