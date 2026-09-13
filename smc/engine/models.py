"""
smc/engine/models.py
====================
Immutable domain models, snapshot DTOs, stable ID schemes, and JSON-safe serialization
for the Multi-Strategy Confluence & Selection Engine (Wave 1: S01, S05, S09).
"""

from __future__ import annotations

import copy
import copyreg
import datetime
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Optional, Sequence, Union

# Register MappingProxyType with copyreg so copy.deepcopy succeeds on frozen domain models
copyreg.pickle(MappingProxyType, lambda mp: (MappingProxyType, (dict(mp),)))

import numpy as np
import pandas as pd

from smc.models import (
    BiasState,
    FairValueGap,
    HTFPOI,
    LiquidityPool,
    LiquiditySweep,
    OrderBlock,
    SessionDecision,
    StructureEvent,
    SwingPoint,
)

# Allowed literal types
EvidenceKind = Literal[
    "liquidity_sweep",
    "fair_value_gap",
    "order_block",
    "structure_event",
    "liquidity_pool",
    "htf_bias",
    "session",
    "htf_poi",
]

TradeDirection = Literal["BUY", "SELL"]

MarketRegimeType = Literal[
    "bullish_trend",
    "bearish_trend",
    "volatile_reversal",
    "ranging",
    "uncertain",
]

EvaluationStatus = Literal["ELIGIBLE", "REJECTED"]
DecisionAction = Literal["SELECT", "NO_TRADE"]
TargetType = Literal["opposing_pool", "fixed_rr", "structure_swing"]
StrategyStyle = Literal["reversal", "continuation", "time_based"]

VALID_EVIDENCE_KINDS = {
    "liquidity_sweep",
    "fair_value_gap",
    "order_block",
    "structure_event",
    "liquidity_pool",
    "htf_bias",
    "session",
    "htf_poi",
}

VALID_REGIMES = {
    "bullish_trend",
    "bearish_trend",
    "volatile_reversal",
    "ranging",
    "uncertain",
}

VALID_DECISION_REASONS = {
    "ok",
    "conflicting_direction",
    "insufficient_score",
    "no_eligible_setup",
}


# =============================================================================
# Helper Utilities: Immutability, Validation, and Serialization
# =============================================================================

class StrictModelTypeError(TypeError, ValueError):
    """Exception raised when an invalid type is passed to a strict model field."""
    pass


_BASE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_base_token(val: Any, name: str) -> str:
    """
    Validate that val is a non-empty string matching ^[A-Za-z0-9_.-]+$ with no delimiter ':'.
    Guarantees that composite IDs are strictly injective and reversible.
    """
    if not isinstance(val, str):
        raise StrictModelTypeError(f"Field '{name}' must be a string, got {type(val).__name__}: {val}")
    if not val:
        raise ValueError(f"Field '{name}' cannot be empty.")
    if val != val.strip():
        raise ValueError(f"Field '{name}' cannot contain leading or trailing whitespace: '{val}'")
    if ":" in val:
        raise ValueError(f"Base component '{name}' cannot contain delimiter ':': '{val}'")
    if not _BASE_TOKEN_RE.match(val):
        raise ValueError(f"Base component '{name}' contains invalid characters (allowed [A-Za-z0-9_.-]): '{val}'")
    return val


def _validate_cluster_id(val: Any, name: str = "cluster_id") -> str:
    """
    Validate cluster_id: non-empty, no whitespace, composed of valid base tokens delimited by ':'.
    """
    if not isinstance(val, str):
        raise StrictModelTypeError(f"Field '{name}' must be a string, got {type(val).__name__}: {val}")
    if not val:
        raise ValueError(f"Field '{name}' cannot be empty.")
    if val != val.strip():
        raise ValueError(f"Field '{name}' cannot contain leading or trailing whitespace: '{val}'")
    parts = val.split(":")
    for i, p in enumerate(parts):
        if not p:
            raise ValueError(f"Field '{name}' contains empty segment at position {i}: '{val}'")
        if not _BASE_TOKEN_RE.match(p):
            raise ValueError(f"Segment '{p}' in '{name}' contains invalid characters: '{val}'")
    return val


def _validate_non_negative_int(val: Any, name: str) -> int:
    """Validate that value is a non-negative integer (and NOT a boolean)."""
    if isinstance(val, (bool, np.bool_)):
        raise StrictModelTypeError(f"Field '{name}' cannot be a boolean.")
    if not isinstance(val, (int, np.integer)):
        raise StrictModelTypeError(f"Field '{name}' must be an integer, got {type(val).__name__}: {val}")
    i_val = int(val)
    if i_val < 0:
        raise ValueError(f"Field '{name}' must be a non-negative integer, got {i_val}")
    return i_val


def _validate_int(val: Any, name: str, min_val: Optional[int] = None) -> int:
    """Validate that value is an integer (and NOT a boolean), optionally with lower bound."""
    if isinstance(val, (bool, np.bool_)):
        raise StrictModelTypeError(f"Field '{name}' cannot be a boolean.")
    if not isinstance(val, (int, np.integer)):
        raise StrictModelTypeError(f"Field '{name}' must be an integer, got {type(val).__name__}: {val}")
    i_val = int(val)
    if min_val is not None and i_val < min_val:
        raise ValueError(f"Field '{name}' must be >= {min_val}, got {i_val}")
    return i_val


def _validate_finite_float(val: Any, name: str, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
    """Validate that value is a finite float (and NOT a boolean) within [min_val, max_val]."""
    if isinstance(val, (bool, np.bool_)):
        raise StrictModelTypeError(f"Field '{name}' cannot be a boolean.")
    if not isinstance(val, (int, float, np.integer, np.floating)):
        raise StrictModelTypeError(f"Field '{name}' must be a numeric float, got {type(val).__name__}: {val}")
    f_val = float(val)
    if not math.isfinite(f_val):
        raise ValueError(f"Field '{name}' must be a finite float, got {val}")
    if min_val is not None and f_val < min_val:
        raise ValueError(f"Field '{name}' must be >= {min_val}, got {f_val}")
    if max_val is not None and f_val > max_val:
        raise ValueError(f"Field '{name}' must be <= {max_val}, got {f_val}")
    return f_val


def _validate_bool(val: Any, name: str) -> bool:
    """Validate that value is an actual boolean (and NOT an integer 0/1 or string 'true'/'false')."""
    if not isinstance(val, (bool, np.bool_)):
        raise StrictModelTypeError(f"Field '{name}' must be a boolean, got {type(val).__name__}: {val}")
    return bool(val)


def _validate_str(val: Any, name: str, allow_empty: bool = False) -> str:
    """Validate that value is a string, optionally non-empty."""
    if not isinstance(val, str):
        raise StrictModelTypeError(f"Field '{name}' must be a string, got {type(val).__name__}: {val}")
    if not allow_empty and not val.strip():
        raise ValueError(f"Field '{name}' cannot be empty.")
    return val


def _parse_timestamp(val: Any, name: str = "timestamp") -> pd.Timestamp:
    """Parse string, datetime, or timestamp into a normalized pd.Timestamp. Rejects None and boolean."""
    if val is None:
        raise StrictModelTypeError(f"Field '{name}' cannot be None.")
    if isinstance(val, (bool, np.bool_)):
        raise StrictModelTypeError(f"Field '{name}' cannot be a boolean.")
    if isinstance(val, pd.Timestamp):
        return val
    if isinstance(val, (str, datetime.datetime, datetime.date, np.datetime64)):
        try:
            return pd.Timestamp(val)
        except Exception as e:
            raise ValueError(f"Cannot parse '{val}' to pd.Timestamp for field '{name}': {e}")
    if isinstance(val, (int, float, np.integer, np.floating)):
        return pd.Timestamp(val, unit="s" if val < 1e11 else "ms")
    raise StrictModelTypeError(f"Cannot parse '{val}' (type {type(val).__name__}) to pd.Timestamp for field '{name}'.")


def _freeze(obj: Any) -> Any:
    """
    Recursively freeze mappings into MappingProxyType, sets into sorted tuples,
    and detector dataclasses into read-only Snapshot DTOs.
    Fails fast with TypeError for unsupported mutable custom objects.
    """
    # Fast paths for common immutable primitives
    if obj is None or obj is True or obj is False:
        return obj
    if type(obj) is str:
        return obj
    if type(obj) is int:
        return obj
    if type(obj) is float:
        if not math.isfinite(obj):
            raise ValueError(f"Non-finite float value not allowed in domain model: {obj}")
        return obj
    if type(obj) is dict:
        if not obj:
            return MappingProxyType({})
        return MappingProxyType({str(k): _freeze(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))})
    if type(obj) is tuple:
        if not obj:
            return ()
        return tuple(_freeze(x) for x in obj)
    if type(obj) is MappingProxyType:
        if not obj:
            return obj
        return MappingProxyType({str(k): _freeze(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))})

    # 1. Booleans (numpy bool etc)
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    # 2. Integers (numpy int etc)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    # 3. Floats (numpy float etc)
    if isinstance(obj, (float, np.floating)):
        f_val = float(obj)
        if not math.isfinite(f_val):
            raise ValueError(f"Non-finite float value not allowed in domain model: {f_val}")
        return f_val
    # 4. Strings
    if isinstance(obj, str):
        return obj
    # 5. Timestamps / Datetimes
    if isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date, np.datetime64)):
        return _parse_timestamp(obj)
    # 6. Mappings
    if isinstance(obj, (dict, Mapping, MappingProxyType)):
        if not obj:
            return MappingProxyType({})
        return MappingProxyType({str(k): _freeze(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))})
    # 7. Sets / frozensets (deterministic sort!)
    if isinstance(obj, (set, frozenset)):
        if not obj:
            return ()
        return tuple(_freeze(x) for x in sorted(obj, key=lambda item: str(item)))
    # 8. Sequences (lists / tuples)
    if isinstance(obj, (list, tuple)):
        if not obj:
            return ()
        return tuple(_freeze(x) for x in obj)
    # 9. Detector objects -> convert to snapshots
    if isinstance(obj, SwingPoint):
        return SwingPointSnapshot.from_source(obj)
    if isinstance(obj, StructureEvent):
        return StructureEventSnapshot.from_source(obj)
    if isinstance(obj, FairValueGap):
        return FairValueGapSnapshot.from_source(obj)
    if isinstance(obj, OrderBlock):
        return OrderBlockSnapshot.from_source(obj)
    if isinstance(obj, LiquidityPool):
        return LiquidityPoolSnapshot.from_source(obj)
    if isinstance(obj, LiquiditySweep):
        return LiquiditySweepSnapshot.from_source(obj)
    if isinstance(obj, SessionDecision):
        return SessionDecisionSnapshot.from_source(obj)
    if isinstance(obj, BiasState):
        return BiasStateSnapshot.from_source(obj)
    if isinstance(obj, (SwingPointSnapshot, StructureEventSnapshot, FairValueGapSnapshot,
                        OrderBlockSnapshot, LiquidityPoolSnapshot, LiquiditySweepSnapshot,
                        SessionDecisionSnapshot, BiasStateSnapshot)):
        return obj
    if isinstance(obj, (EvidenceRef, CandidateSetup, MarketRegime, StrategyEvaluation, StrategyProfile)):
        return obj
    # 10. None
    if obj is None:
        return None
    raise TypeError(f"Unsupported metadata type for deep immutable freezing: {type(obj)}: {obj}")


def _unfreeze(obj: Any) -> Any:
    """
    Recursively unpack frozen objects into JSON-safe dictionaries, lists, and primitives.
    Guarantees strict boolean preservation, NumPy scalar normalization, and no NaN/Inf.
    """
    # 1. Boolean first! (MUST be evaluated before int because isinstance(True, int) is True in Python)
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    # 2. Integer
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    # 3. Float
    if isinstance(obj, (float, np.floating)):
        f_val = float(obj)
        if not math.isfinite(f_val):
            raise ValueError(f"Cannot serialize non-finite float {f_val} to JSON domain payload.")
        return f_val
    # 4. String
    if isinstance(obj, str):
        return obj
    # 5. Timestamp / datetime
    if isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date, np.datetime64)):
        return pd.Timestamp(obj).isoformat()
    # 6. Objects with to_dict()
    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        return obj.to_dict()
    # 7. Mappings
    if isinstance(obj, (Mapping, MappingProxyType)):
        return {str(k): _unfreeze(v) for k, v in obj.items()}
    # 8. Sets / frozensets (deterministic sort!)
    if isinstance(obj, (set, frozenset)):
        return sorted([_unfreeze(x) for x in obj], key=lambda item: str(item))
    # 9. Sequences (list, tuple)
    if isinstance(obj, (list, tuple)):
        return [_unfreeze(x) for x in obj]
    # 10. None
    if obj is None:
        return None
    raise TypeError(f"Unsupported type {type(obj)} for JSON domain serialization: {obj}")


# =============================================================================
# Stable ID Generators (Deterministic, Injective, Collision-Free)
# =============================================================================

def make_evidence_id(kind: str, mode: str, bar_index: int, sub_key: str) -> str:
    """
    Generate deterministic, injective evidence ID.
    Example: 'sweep:internal:45:bearish_pool1', 'fvg:internal:42:bullish'
    """
    k = _validate_base_token(kind, "kind")
    m = _validate_base_token(mode, "mode")
    b_idx = _validate_non_negative_int(bar_index, "bar_index")
    sk = _validate_base_token(sub_key, "sub_key")
    return f"{k}:{m}:{b_idx}:{sk}"


def make_cluster_id(direction: str, leg_id: str, zone_id: str) -> str:
    """
    Generate deterministic, injective evidence cluster ID for deduplication.
    Example: 'BUY:leg_12:fvg_15'
    """
    if not isinstance(direction, str) or direction not in {"BUY", "SELL"}:
        raise ValueError(f"direction must be 'BUY' or 'SELL', got '{direction}'")
    l_id = _validate_base_token(leg_id, "leg_id")
    z_id = _validate_base_token(zone_id, "zone_id")
    return f"{direction}:{l_id}:{z_id}"


def make_setup_id(strategy_id: str, direction: str, bar_index: int, cluster_id: str) -> str:
    """
    Generate deterministic, injective candidate setup ID preserving exact cluster_id contract.
    Contract: '{strategy_id}:{direction}:{bar_index}:{cluster_id}'
    """
    s_id = _validate_base_token(strategy_id, "strategy_id")
    if not isinstance(direction, str) or direction not in {"BUY", "SELL"}:
        raise ValueError(f"direction must be 'BUY' or 'SELL', got '{direction}'")
    b_idx = _validate_non_negative_int(bar_index, "bar_index")
    c_id = _validate_cluster_id(cluster_id, "cluster_id")
    return f"{s_id}:{direction}:{b_idx}:{c_id}"


def make_decision_id(bar_index: int, action: str, primary_strategy_id: Optional[str] = None) -> str:
    """
    Generate deterministic, injective selection decision ID.
    Example: 'sel:45:SELECT:S01' or 'sel:45:NO_TRADE:none'
    """
    b_idx = _validate_non_negative_int(bar_index, "bar_index")
    if not isinstance(action, str) or action not in {"SELECT", "NO_TRADE"}:
        raise ValueError(f"action must be 'SELECT' or 'NO_TRADE', got '{action}'")
    if action == "SELECT":
        if primary_strategy_id is None:
            raise ValueError("primary_strategy_id is required when action is 'SELECT'.")
        strat = _validate_base_token(primary_strategy_id, "primary_strategy_id")
        if strat == "none":
            raise ValueError("primary_strategy_id cannot be 'none' when action is 'SELECT'.")
    else:
        if primary_strategy_id is not None:
            raise ValueError(f"primary_strategy_id must be None when action is 'NO_TRADE', got '{primary_strategy_id}'.")
        strat = "none"
    return f"sel:{b_idx}:{action}:{strat}"


# =============================================================================
# Read-Only Snapshot DTOs for SMC Structures and Context States
# =============================================================================

@dataclass(frozen=True)
class SwingPointSnapshot:
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

    def __post_init__(self):
        object.__setattr__(self, "index", _validate_non_negative_int(self.index, "index"))
        object.__setattr__(self, "time", _parse_timestamp(self.time, "time"))
        object.__setattr__(self, "price", _validate_finite_float(self.price, "price"))
        if self.kind not in {"high", "low"}:
            raise ValueError(f"Invalid kind '{self.kind}'. Must be 'high' or 'low'.")
        object.__setattr__(self, "strength", _validate_non_negative_int(self.strength, "strength"))
        object.__setattr__(self, "confirmed_at", _validate_non_negative_int(self.confirmed_at, "confirmed_at"))
        if self.confirmed_time is not None:
            object.__setattr__(self, "confirmed_time", _parse_timestamp(self.confirmed_time, "confirmed_time"))
        if self.mode not in {"swing", "internal"}:
            raise ValueError(f"Invalid mode '{self.mode}'. Must be 'swing' or 'internal'.")
        object.__setattr__(self, "broken", _validate_bool(self.broken, "broken"))
        if self.broken_at is not None:
            object.__setattr__(self, "broken_at", _validate_non_negative_int(self.broken_at, "broken_at"))

    @classmethod
    def from_source(cls, src: Any) -> SwingPointSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("index", "time", "price", "kind", "strength", "confirmed_at"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in SwingPointSnapshot.")
            return cls(
                index=src["index"],
                time=src["time"],
                price=src["price"],
                kind=src["kind"],
                strength=src["strength"],
                confirmed_at=src["confirmed_at"],
                confirmed_time=src.get("confirmed_time"),
                mode=src.get("mode", "swing"),
                classification=src.get("classification", "UNCLASSIFIED"),
                broken=src.get("broken", False),
                broken_at=src.get("broken_at"),
            )
        return cls(
            index=src.index,
            time=src.time,
            price=src.price,
            kind=src.kind,
            strength=src.strength,
            confirmed_at=src.confirmed_at,
            confirmed_time=getattr(src, "confirmed_time", None),
            mode=getattr(src, "mode", "swing"),
            classification=getattr(src, "classification", "UNCLASSIFIED"),
            broken=getattr(src, "broken", False),
            broken_at=getattr(src, "broken_at", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat(),
            "price": float(self.price),
            "kind": str(self.kind),
            "strength": int(self.strength),
            "mode": str(self.mode),
            "confirmed_at": int(self.confirmed_at),
            "confirmed_time": self.confirmed_time.isoformat() if self.confirmed_time is not None else None,
            "classification": str(self.classification),
            "broken": bool(self.broken),
            "broken_at": int(self.broken_at) if self.broken_at is not None else None,
        }


@dataclass(frozen=True)
class StructureEventSnapshot:
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

    def __post_init__(self):
        object.__setattr__(self, "index", _validate_non_negative_int(self.index, "index"))
        object.__setattr__(self, "time", _parse_timestamp(self.time, "time"))
        if self.event_type not in {"BOS", "CHoCH"}:
            raise ValueError(f"Invalid event_type '{self.event_type}'. Must be 'BOS' or 'CHoCH'.")
        if self.direction not in {"bullish", "bearish"}:
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'bullish' or 'bearish'.")
        object.__setattr__(self, "broken_swing_index", _validate_non_negative_int(self.broken_swing_index, "broken_swing_index"))
        object.__setattr__(self, "broken_swing_price", _validate_finite_float(self.broken_swing_price, "broken_swing_price"))
        object.__setattr__(self, "close_price", _validate_finite_float(self.close_price, "close_price"))
        object.__setattr__(self, "displacement", _validate_bool(self.displacement, "displacement"))
        object.__setattr__(self, "confirmed_swing_at", _validate_non_negative_int(self.confirmed_swing_at, "confirmed_swing_at"))
        object.__setattr__(self, "body_size", _validate_finite_float(self.body_size, "body_size"))
        object.__setattr__(self, "atr_value", _validate_finite_float(self.atr_value, "atr_value"))

    @classmethod
    def from_source(cls, src: Any) -> StructureEventSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("index", "time", "event_type", "direction", "broken_swing_index", "broken_swing_price", "close_price"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in StructureEventSnapshot.")
            return cls(
                index=src["index"],
                time=src["time"],
                event_type=src["event_type"],
                direction=src["direction"],
                broken_swing_index=src["broken_swing_index"],
                broken_swing_price=src["broken_swing_price"],
                close_price=src["close_price"],
                displacement=src.get("displacement", False),
                mode=src.get("mode", "swing"),
                confirmed_swing_at=src.get("confirmed_swing_at", 0),
                body_size=src.get("body_size", 0.0),
                atr_value=src.get("atr_value", 0.0),
                break_type=src.get("break_type", "close"),
                structure_leg_id=src.get("structure_leg_id"),
            )
        return cls(
            index=src.index,
            time=src.time,
            event_type=src.event_type,
            direction=src.direction,
            broken_swing_index=src.broken_swing_index,
            broken_swing_price=src.broken_swing_price,
            close_price=src.close_price,
            displacement=getattr(src, "displacement", False),
            mode=getattr(src, "mode", "swing"),
            confirmed_swing_at=getattr(src, "confirmed_swing_at", 0),
            body_size=getattr(src, "body_size", 0.0),
            atr_value=getattr(src, "atr_value", 0.0),
            break_type=getattr(src, "break_type", "close"),
            structure_leg_id=getattr(src, "structure_leg_id", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat(),
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


@dataclass(frozen=True)
class FairValueGapSnapshot:
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
        object.__setattr__(self, "index", _validate_non_negative_int(self.index, "index"))
        object.__setattr__(self, "time", _parse_timestamp(self.time, "time"))
        if self.direction not in {"bullish", "bearish"}:
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'bullish' or 'bearish'.")
        object.__setattr__(self, "top", _validate_finite_float(self.top, "top"))
        object.__setattr__(self, "bottom", _validate_finite_float(self.bottom, "bottom"))
        size = self.top - self.bottom
        object.__setattr__(self, "ce", _validate_finite_float((self.top + self.bottom) / 2 if self.ce is None else self.ce, "ce"))
        object.__setattr__(self, "gap_size", _validate_finite_float(size if self.gap_size is None else self.gap_size, "gap_size"))
        object.__setattr__(self, "gap_pct", _validate_finite_float((size / abs(self.bottom)) if self.gap_pct is None and self.bottom != 0 else (0.0 if self.gap_pct is None else self.gap_pct), "gap_pct", min_val=0.0))
        object.__setattr__(self, "confirmed_at", _validate_non_negative_int(self.confirmed_at, "confirmed_at"))
        object.__setattr__(self, "filled", _validate_bool(self.filled, "filled"))
        if self.filled_at is not None:
            object.__setattr__(self, "filled_at", _validate_non_negative_int(self.filled_at, "filled_at"))
            object.__setattr__(self, "filled", True)
        object.__setattr__(self, "displacement", _validate_bool(self.displacement, "displacement"))
        object.__setattr__(self, "body_ratio", _validate_finite_float(self.body_ratio, "body_ratio", min_val=0.0))
        object.__setattr__(self, "atr_value", _validate_finite_float(self.atr_value, "atr_value", min_val=0.0))
        object.__setattr__(self, "touch_count", _validate_non_negative_int(self.touch_count, "touch_count"))
        object.__setattr__(self, "ce_touched", _validate_bool(self.ce_touched, "ce_touched"))
        object.__setattr__(self, "partial_filled", _validate_bool(self.partial_filled, "partial_filled"))
        object.__setattr__(self, "middle_body_size", _validate_finite_float(self.middle_body_size, "middle_body_size", min_val=0.0))
        object.__setattr__(self, "middle_range", _validate_finite_float(self.middle_range, "middle_range", min_val=0.0))
        object.__setattr__(self, "middle_body_ratio", _validate_finite_float(self.middle_body_ratio, "middle_body_ratio", min_val=0.0))
        if self.state not in {"active", "partial", "filled"}:
            raise ValueError("state must be 'active', 'partial', or 'filled'")
        if self.filled:
            object.__setattr__(self, "state", "filled")
        elif self.partial_filled:
            object.__setattr__(self, "state", "partial")

    @classmethod
    def from_source(cls, src: Any) -> FairValueGapSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("index", "time", "direction", "top", "bottom"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in FairValueGapSnapshot.")
            return cls(
                index=src["index"],
                time=src["time"],
                direction=src["direction"],
                top=src["top"],
                bottom=src["bottom"],
                mode=src.get("mode", "swing"),
                confirmed_at=src.get("confirmed_at", 0),
                filled=src.get("filled", False),
                filled_at=src.get("filled_at"),
                structure_leg_id=src.get("structure_leg_id"),
                ce=src.get("ce"), gap_size=src.get("gap_size"), gap_pct=src.get("gap_pct"),
                displacement=src.get("displacement", False), body_ratio=src.get("body_ratio", 0.0),
                atr_value=src.get("atr_value", 0.0), touch_count=src.get("touch_count", 0),
                ce_touched=src.get("ce_touched", False), ce_touched_at=src.get("ce_touched_at"),
                partial_filled=src.get("partial_filled", False), partial_filled_at=src.get("partial_filled_at"),
                middle_body_size=src.get("middle_body_size", 0.0), middle_range=src.get("middle_range", 0.0),
                middle_body_ratio=src.get("middle_body_ratio", 0.0), state=src.get("state", "active"),
            )
        return cls(
            index=src.index,
            time=src.time,
            direction=src.direction,
            top=src.top,
            bottom=src.bottom,
            mode=getattr(src, "mode", "swing"),
            confirmed_at=getattr(src, "confirmed_at", 0),
            filled=getattr(src, "filled", False),
            filled_at=getattr(src, "filled_at", None),
            structure_leg_id=getattr(src, "structure_leg_id", None),
            ce=getattr(src, "ce", None), gap_size=getattr(src, "gap_size", None), gap_pct=getattr(src, "gap_pct", None),
            displacement=getattr(src, "displacement", False), body_ratio=getattr(src, "body_ratio", 0.0),
            atr_value=getattr(src, "atr_value", 0.0), touch_count=getattr(src, "touch_count", 0),
            ce_touched=getattr(src, "ce_touched", False), ce_touched_at=getattr(src, "ce_touched_at", None),
            partial_filled=getattr(src, "partial_filled", False), partial_filled_at=getattr(src, "partial_filled_at", None),
            middle_body_size=getattr(src, "middle_body_size", 0.0), middle_range=getattr(src, "middle_range", 0.0),
            middle_body_ratio=getattr(src, "middle_body_ratio", 0.0), state=getattr(src, "state", "active"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat(),
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


@dataclass(frozen=True)
class OrderBlockSnapshot:
    index: int
    time: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    high: float
    low: float
    open: float
    close: float
    origin_type: Literal["BOS", "CHoCH"] = "BOS"
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

    def __post_init__(self):
        object.__setattr__(self, "index", _validate_non_negative_int(self.index, "index"))
        object.__setattr__(self, "time", _parse_timestamp(self.time, "time"))
        if self.direction not in {"bullish", "bearish"}:
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'bullish' or 'bearish'.")
        object.__setattr__(self, "high", _validate_finite_float(self.high, "high"))
        object.__setattr__(self, "low", _validate_finite_float(self.low, "low"))
        object.__setattr__(self, "open", _validate_finite_float(self.open, "open"))
        object.__setattr__(self, "close", _validate_finite_float(self.close, "close"))
        object.__setattr__(self, "source_event_index", _validate_int(self.source_event_index, "source_event_index", min_val=-1))
        object.__setattr__(self, "source_swing_index", _validate_int(self.source_swing_index, "source_swing_index", min_val=-1))
        object.__setattr__(self, "created_at", _validate_int(self.created_at, "created_at", min_val=-1))
        if self.source_fvg_index is not None:
            object.__setattr__(self, "source_fvg_index", _validate_non_negative_int(self.source_fvg_index, "source_fvg_index"))
        if self.source_fvg_top is not None:
            object.__setattr__(self, "source_fvg_top", _validate_finite_float(self.source_fvg_top, "source_fvg_top"))
        if self.source_fvg_bottom is not None:
            object.__setattr__(self, "source_fvg_bottom", _validate_finite_float(self.source_fvg_bottom, "source_fvg_bottom"))
        object.__setattr__(self, "mitigated", _validate_bool(self.mitigated, "mitigated"))
        if self.mitigated_at is not None:
            object.__setattr__(self, "mitigated_at", _validate_non_negative_int(self.mitigated_at, "mitigated_at"))
        object.__setattr__(self, "mitigation_pct", _validate_finite_float(self.mitigation_pct, "mitigation_pct"))
        object.__setattr__(self, "valid", _validate_bool(self.valid, "valid"))
        if self.invalidated_at is not None:
            object.__setattr__(self, "invalidated_at", _validate_non_negative_int(self.invalidated_at, "invalidated_at"))
        object.__setattr__(self, "retest_count", _validate_non_negative_int(self.retest_count, "retest_count"))

    @classmethod
    def from_source(cls, src: Any) -> OrderBlockSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("index", "time", "direction", "high", "low", "open", "close"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in OrderBlockSnapshot.")
            return cls(
                index=src["index"],
                time=src["time"],
                direction=src["direction"],
                high=src["high"],
                low=src["low"],
                open=src["open"],
                close=src["close"],
                origin_type=src.get("origin_type", "BOS"),
                mode=src.get("mode", "swing"),
                quality=src.get("quality", "base"),
                source_event_index=src.get("source_event_index", -1),
                source_event_type=src.get("source_event_type", ""),
                source_swing_index=src.get("source_swing_index", -1),
                created_at=src.get("created_at", src.get("source_event_index", -1)),
                source_fvg_index=src.get("source_fvg_index"),
                source_fvg_top=src.get("source_fvg_top"),
                source_fvg_bottom=src.get("source_fvg_bottom"),
                mitigated=src.get("mitigated", False),
                mitigated_at=src.get("mitigated_at"),
                mitigation_pct=src.get("mitigation_pct", 0.0),
                valid=src.get("valid", True),
                invalidated_at=src.get("invalidated_at"),
                invalidation_reason=src.get("invalidation_reason"),
                retest_count=src.get("retest_count", 0),
                structure_leg_id=src.get("structure_leg_id"),
            )
        return cls(
            index=src.index,
            time=src.time,
            direction=src.direction,
            high=src.high,
            low=src.low,
            open=src.open,
            close=src.close,
            origin_type=getattr(src, "origin_type", "BOS"),
            mode=getattr(src, "mode", "swing"),
            quality=getattr(src, "quality", "base"),
            source_event_index=getattr(src, "source_event_index", -1),
            source_event_type=getattr(src, "source_event_type", ""),
            source_swing_index=getattr(src, "source_swing_index", -1),
            created_at=getattr(src, "created_at", -1),
            source_fvg_index=getattr(src, "source_fvg_index", None),
            source_fvg_top=getattr(src, "source_fvg_top", None),
            source_fvg_bottom=getattr(src, "source_fvg_bottom", None),
            mitigated=getattr(src, "mitigated", False),
            mitigated_at=getattr(src, "mitigated_at", None),
            mitigation_pct=getattr(src, "mitigation_pct", 0.0),
            valid=getattr(src, "valid", True),
            invalidated_at=getattr(src, "invalidated_at", None),
            invalidation_reason=getattr(src, "invalidation_reason", None),
            retest_count=getattr(src, "retest_count", 0),
            structure_leg_id=getattr(src, "structure_leg_id", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat(),
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


@dataclass(frozen=True)
class LiquidityPoolSnapshot:
    kind: Literal["equal_highs", "equal_lows", "swing_high", "swing_low"]
    price: float
    price_max: float
    price_min: float
    indices: tuple[int, ...]
    created_at: int
    confirmed_at: int
    swept: bool = False
    swept_at: Optional[int] = None
    sweep_type: Optional[Literal["clean", "wick_only"]] = None
    valid: bool = True
    invalidated_at: Optional[int] = None
    invalidation_reason: Optional[str] = None
    mode: Literal["swing", "internal"] = "swing"
    source_swings: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    structure_leg_id: Optional[str] = None
    liquidity_side: Optional[Literal["BUY_SIDE", "SELL_SIDE"]] = None

    def __post_init__(self):
        object.__setattr__(self, "price", _validate_finite_float(self.price, "price"))
        object.__setattr__(self, "price_max", _validate_finite_float(self.price_max, "price_max"))
        object.__setattr__(self, "price_min", _validate_finite_float(self.price_min, "price_min"))
        object.__setattr__(self, "indices", tuple(_validate_non_negative_int(x, "indices element") for x in self.indices))
        object.__setattr__(self, "created_at", _validate_non_negative_int(self.created_at, "created_at"))
        object.__setattr__(self, "confirmed_at", _validate_non_negative_int(self.confirmed_at, "confirmed_at"))
        object.__setattr__(self, "swept", _validate_bool(self.swept, "swept"))
        if self.swept_at is not None:
            object.__setattr__(self, "swept_at", _validate_non_negative_int(self.swept_at, "swept_at"))
        object.__setattr__(self, "valid", _validate_bool(self.valid, "valid"))
        if self.invalidated_at is not None:
            object.__setattr__(self, "invalidated_at", _validate_non_negative_int(self.invalidated_at, "invalidated_at"))
        frozen_swings = tuple(_freeze(s) for s in self.source_swings)
        object.__setattr__(self, "source_swings", frozen_swings)
        l_side = self.liquidity_side
        if l_side is None:
            if self.kind in ("equal_lows", "swing_low"):
                l_side = "SELL_SIDE"
            elif self.kind in ("equal_highs", "swing_high"):
                l_side = "BUY_SIDE"
        elif isinstance(l_side, str):
            l_side = l_side.upper()
            if l_side not in ("BUY_SIDE", "SELL_SIDE"):
                raise ValueError(f"Invalid liquidity_side '{self.liquidity_side}'. Must be 'BUY_SIDE' or 'SELL_SIDE'.")
        object.__setattr__(self, "liquidity_side", l_side)

    @classmethod
    def from_source(cls, src: Any) -> LiquidityPoolSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("kind", "price", "price_max", "price_min", "indices", "created_at", "confirmed_at"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in LiquidityPoolSnapshot.")
            return cls(
                kind=src["kind"],
                price=src["price"],
                price_max=src["price_max"],
                price_min=src["price_min"],
                indices=tuple(src.get("indices", ())),
                created_at=src["created_at"],
                confirmed_at=src["confirmed_at"],
                swept=src.get("swept", False),
                swept_at=src.get("swept_at"),
                sweep_type=src.get("sweep_type"),
                valid=src.get("valid", True),
                invalidated_at=src.get("invalidated_at"),
                invalidation_reason=src.get("invalidation_reason"),
                mode=src.get("mode", "swing"),
                source_swings=tuple(src.get("source_swings", ())),
                structure_leg_id=src.get("structure_leg_id"),
                liquidity_side=src.get("liquidity_side"),
            )
        return cls(
            kind=src.kind,
            price=src.price,
            price_max=src.price_max,
            price_min=src.price_min,
            indices=tuple(src.indices),
            created_at=src.created_at,
            confirmed_at=src.confirmed_at,
            swept=getattr(src, "swept", False),
            swept_at=getattr(src, "swept_at", None),
            sweep_type=getattr(src, "sweep_type", None),
            valid=getattr(src, "valid", True),
            invalidated_at=getattr(src, "invalidated_at", None),
            invalidation_reason=getattr(src, "invalidation_reason", None),
            mode=getattr(src, "mode", "swing"),
            source_swings=tuple(getattr(src, "source_swings", ())),
            structure_leg_id=getattr(src, "structure_leg_id", None),
            liquidity_side=getattr(src, "liquidity_side", None),
        )

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
            "source_swings": [_unfreeze(s) for s in self.source_swings],
            "structure_leg_id": self.structure_leg_id,
            "liquidity_side": str(self.liquidity_side) if self.liquidity_side is not None else None,
        }


@dataclass(frozen=True)
class LiquiditySweepSnapshot:
    index: int
    time: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    pool_kind: str
    pool_price: float
    pool_indices: tuple[int, ...]
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
        object.__setattr__(self, "index", _validate_non_negative_int(self.index, "index"))
        object.__setattr__(self, "time", _parse_timestamp(self.time, "time"))
        if self.direction not in {"bullish", "bearish"}:
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'bullish' or 'bearish'.")
        object.__setattr__(self, "pool_price", _validate_finite_float(self.pool_price, "pool_price"))
        object.__setattr__(self, "pool_indices", tuple(_validate_non_negative_int(x, "pool_indices element") for x in self.pool_indices))
        object.__setattr__(self, "price_wick", _validate_finite_float(self.price_wick, "price_wick"))
        object.__setattr__(self, "close_price", _validate_finite_float(self.close_price, "close_price"))
        object.__setattr__(self, "created_at", _validate_non_negative_int(self.created_at, "created_at"))
        object.__setattr__(self, "confirmed_at", _validate_non_negative_int(self.confirmed_at, "confirmed_at"))
        object.__setattr__(self, "swept_at", _validate_non_negative_int(self.swept_at, "swept_at"))
        object.__setattr__(self, "valid", _validate_bool(self.valid, "valid"))
        l_side = self.liquidity_side
        if l_side is None:
            if self.pool_kind in ("equal_lows", "swing_low"):
                l_side = "SELL_SIDE"
            elif self.pool_kind in ("equal_highs", "swing_high"):
                l_side = "BUY_SIDE"
        elif isinstance(l_side, str):
            l_side = l_side.upper()
            if l_side not in ("BUY_SIDE", "SELL_SIDE"):
                raise ValueError(f"Invalid liquidity_side '{self.liquidity_side}'. Must be 'BUY_SIDE' or 'SELL_SIDE'.")

        rev_dir = self.reversal_direction
        if rev_dir is None:
            rev_dir = self.direction
        elif rev_dir not in ("bullish", "bearish"):
            raise ValueError(f"Invalid reversal_direction '{rev_dir}'. Must be 'bullish' or 'bearish'.")

        raid_dir = self.raid_direction
        if raid_dir is None:
            raid_dir = "bearish" if l_side == "SELL_SIDE" else "bullish"
        elif raid_dir not in ("bullish", "bearish"):
            raise ValueError(f"Invalid raid_direction '{raid_dir}'. Must be 'bullish' or 'bearish'.")

        object.__setattr__(self, "liquidity_side", l_side)
        object.__setattr__(self, "raid_direction", raid_dir)
        object.__setattr__(self, "reversal_direction", rev_dir)

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

    @classmethod
    def from_source(cls, src: Any) -> LiquiditySweepSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("index", "time", "direction", "pool_kind", "pool_price", "pool_indices", "price_wick", "close_price", "created_at", "confirmed_at"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in LiquiditySweepSnapshot.")
            return cls(
                index=src["index"],
                time=src["time"],
                direction=src["direction"],
                pool_kind=src["pool_kind"],
                pool_price=src["pool_price"],
                pool_indices=tuple(src.get("pool_indices", ())),
                price_wick=src["price_wick"],
                close_price=src["close_price"],
                created_at=src["created_at"],
                confirmed_at=src["confirmed_at"],
                swept_at=src.get("swept_at", src["index"]),
                sweep_type=src.get("sweep_type", "clean"),
                valid=src.get("valid", True),
                mode=src.get("mode", "swing"),
                structure_leg_id=src.get("structure_leg_id"),
                liquidity_side=src.get("liquidity_side"),
                raid_direction=src.get("raid_direction"),
                reversal_direction=src.get("reversal_direction"),
            )
        return cls(
            index=src.index,
            time=src.time,
            direction=src.direction,
            pool_kind=src.pool_kind,
            pool_price=src.pool_price,
            pool_indices=tuple(src.pool_indices),
            price_wick=src.price_wick,
            close_price=src.close_price,
            created_at=src.created_at,
            confirmed_at=src.confirmed_at,
            swept_at=getattr(src, "swept_at", src.index),
            sweep_type=getattr(src, "sweep_type", "clean"),
            valid=getattr(src, "valid", True),
            mode=getattr(src, "mode", "swing"),
            structure_leg_id=getattr(src, "structure_leg_id", None),
            liquidity_side=getattr(src, "liquidity_side", None),
            raid_direction=getattr(src, "raid_direction", None),
            reversal_direction=getattr(src, "reversal_direction", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "time": self.time.isoformat(),
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


@dataclass(frozen=True)
class SessionDecisionSnapshot:
    """
    Deep immutable read-only snapshot DTO for SessionDecision.
    Recursively freezes metadata to prevent leakage and ensures JSON-safe serialization.
    """
    in_session: bool
    session_name: Optional[str]
    timestamp: pd.Timestamp
    reason: str
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "in_session", _validate_bool(self.in_session, "in_session"))
        if self.session_name is not None:
            object.__setattr__(self, "session_name", _validate_str(self.session_name, "session_name"))
        object.__setattr__(self, "timestamp", _parse_timestamp(self.timestamp, "timestamp"))
        object.__setattr__(self, "reason", _validate_str(self.reason, "reason"))
        object.__setattr__(self, "meta", _freeze(self.meta))

    @classmethod
    def from_source(cls, src: Any) -> SessionDecisionSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("in_session", "timestamp", "reason"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in SessionDecisionSnapshot.")
            return cls(
                in_session=src["in_session"],
                session_name=src.get("session_name"),
                timestamp=src["timestamp"],
                reason=src["reason"],
                meta=src.get("meta", {}),
            )
        return cls(
            in_session=src.in_session,
            session_name=src.session_name,
            timestamp=src.timestamp,
            reason=src.reason,
            meta=getattr(src, "meta", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "in_session": bool(self.in_session),
            "session_name": self.session_name,
            "timestamp": self.timestamp.isoformat(),
            "reason": str(self.reason),
            "meta": _unfreeze(self.meta),
        }


@dataclass(frozen=True)
class BiasStateSnapshot:
    """
    Deep immutable read-only snapshot DTO for BiasState.
    Recursively freezes metadata to prevent leakage and ensures JSON-safe serialization.
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
        object.__setattr__(self, "timestamp", _parse_timestamp(self.timestamp, "timestamp"))
        if self.source_event_index is not None:
            object.__setattr__(self, "source_event_index", _validate_non_negative_int(self.source_event_index, "source_event_index"))
        if self.source_event_time is not None:
            object.__setattr__(self, "source_event_time", _parse_timestamp(self.source_event_time, "source_event_time"))
        if self.source_event_type is not None:
            object.__setattr__(self, "source_event_type", _validate_str(self.source_event_type, "source_event_type"))
        if self.source_event_direction is not None:
            object.__setattr__(self, "source_event_direction", _validate_str(self.source_event_direction, "source_event_direction"))
        if self.as_of is not None:
            object.__setattr__(self, "as_of", _parse_timestamp(self.as_of, "as_of"))
        object.__setattr__(self, "reason", _validate_str(self.reason, "reason"))
        if self.pending_reversal is not None:
            _val = _validate_str(self.pending_reversal, "pending_reversal")
            if _val not in {"bullish", "bearish"}:
                raise ValueError(f"Invalid pending_reversal '{_val}'. Must be 'bullish' or 'bearish'.")
            object.__setattr__(self, "pending_reversal", _val)
        if self.pending_reversal_event_type is not None:
            object.__setattr__(self, "pending_reversal_event_type", _validate_str(self.pending_reversal_event_type, "pending_reversal_event_type"))
        if self.pending_reversal_event_index is not None:
            object.__setattr__(self, "pending_reversal_event_index", _validate_non_negative_int(self.pending_reversal_event_index, "pending_reversal_event_index"))
        if self.pending_reversal_event_time is not None:
            object.__setattr__(self, "pending_reversal_event_time", _parse_timestamp(self.pending_reversal_event_time, "pending_reversal_event_time"))
        object.__setattr__(self, "confirmed_by_bos", _validate_bool(self.confirmed_by_bos, "confirmed_by_bos"))
        object.__setattr__(self, "meta", _freeze(self.meta))

    @classmethod
    def from_source(cls, src: Any) -> BiasStateSnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("bias", "timestamp"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in BiasStateSnapshot.")
            return cls(
                bias=src["bias"],
                timestamp=src["timestamp"],
                source_event_index=src.get("source_event_index"),
                source_event_time=src.get("source_event_time"),
                source_event_type=src.get("source_event_type"),
                source_event_direction=src.get("source_event_direction"),
                as_of=src.get("as_of"),
                reason=src.get("reason", "ok"),
                pending_reversal=src.get("pending_reversal"),
                pending_reversal_event_type=src.get("pending_reversal_event_type"),
                pending_reversal_event_index=src.get("pending_reversal_event_index"),
                pending_reversal_event_time=src.get("pending_reversal_event_time"),
                confirmed_by_bos=bool(src.get("confirmed_by_bos", False)),
                meta=src.get("meta", {}),
            )
        return cls(
            bias=src.bias,
            timestamp=src.timestamp,
            source_event_index=getattr(src, "source_event_index", None),
            source_event_time=getattr(src, "source_event_time", None),
            source_event_type=getattr(src, "source_event_type", None),
            source_event_direction=getattr(src, "source_event_direction", None),
            as_of=getattr(src, "as_of", None),
            reason=getattr(src, "reason", "ok"),
            pending_reversal=getattr(src, "pending_reversal", None),
            pending_reversal_event_type=getattr(src, "pending_reversal_event_type", None),
            pending_reversal_event_index=getattr(src, "pending_reversal_event_index", None),
            pending_reversal_event_time=getattr(src, "pending_reversal_event_time", None),
            confirmed_by_bos=bool(getattr(src, "confirmed_by_bos", False)),
            meta=getattr(src, "meta", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": str(self.bias),
            "timestamp": self.timestamp.isoformat(),
            "source_event_index": int(self.source_event_index) if self.source_event_index is not None else None,
            "source_event_time": self.source_event_time.isoformat() if self.source_event_time is not None else None,
            "source_event_type": str(self.source_event_type) if self.source_event_type is not None else None,
            "source_event_direction": str(self.source_event_direction) if self.source_event_direction is not None else None,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "reason": str(self.reason),
            "pending_reversal": str(self.pending_reversal) if self.pending_reversal is not None else None,
            "pending_reversal_event_type": str(self.pending_reversal_event_type) if self.pending_reversal_event_type is not None else None,
            "pending_reversal_event_index": int(self.pending_reversal_event_index) if self.pending_reversal_event_index is not None else None,
            "pending_reversal_event_time": self.pending_reversal_event_time.isoformat() if self.pending_reversal_event_time is not None else None,
            "confirmed_by_bos": bool(self.confirmed_by_bos),
            "effective_time": self.source_event_time.isoformat() if self.source_event_time is not None else None,
            "meta": _unfreeze(self.meta),
        }


@dataclass(frozen=True)
class HTFPOISnapshot:
    """
    Deep immutable read-only snapshot DTO for HTFPOI.
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
        object.__setattr__(self, "poi_id", _validate_str(self.poi_id, "poi_id"))
        p_type = _validate_str(self.poi_type, "poi_type")
        if p_type not in {"FVG", "OB"}:
            raise ValueError(f"Invalid poi_type '{p_type}'. Must be 'FVG' or 'OB'.")
        object.__setattr__(self, "poi_type", p_type)

        d_val = _validate_str(self.direction, "direction")
        if d_val not in {"bullish", "bearish"}:
            raise ValueError(f"Invalid direction '{d_val}'. Must be 'bullish' or 'bearish'.")
        object.__setattr__(self, "direction", d_val)

        t = _validate_finite_float(self.top, "top")
        b = _validate_finite_float(self.bottom, "bottom")
        if t < b:
            raise ValueError(f"POI top {t} cannot be less than bottom {b}.")
        object.__setattr__(self, "top", round(t, 3))
        object.__setattr__(self, "bottom", round(b, 3))
        object.__setattr__(self, "timeframe", _validate_str(self.timeframe, "timeframe"))
        object.__setattr__(self, "status", _validate_str(self.status, "status"))
        object.__setattr__(self, "touch_count", _validate_non_negative_int(self.touch_count, "touch_count"))
        if self.last_touch_bar is not None:
            object.__setattr__(self, "last_touch_bar", _validate_non_negative_int(self.last_touch_bar, "last_touch_bar"))
        object.__setattr__(self, "meta", _freeze(self.meta))

    @classmethod
    def from_source(cls, src: Any) -> HTFPOISnapshot:
        if isinstance(src, cls):
            return src
        if isinstance(src, dict):
            for req in ("poi_id", "poi_type", "direction", "top", "bottom"):
                if req not in src:
                    raise KeyError(f"Missing required field '{req}' in HTFPOISnapshot.")
            return cls(
                poi_id=src["poi_id"],
                poi_type=src["poi_type"],
                direction=src["direction"],
                top=src["top"],
                bottom=src["bottom"],
                timeframe=src.get("timeframe", "H1"),
                created_at=src.get("created_at"),
                source_event=src.get("source_event"),
                valid_until=src.get("valid_until"),
                status=src.get("status", "active"),
                touch_count=src.get("touch_count", 0),
                last_touch_bar=src.get("last_touch_bar"),
                meta=src.get("meta", {}),
            )
        return cls(
            poi_id=src.poi_id,
            poi_type=src.poi_type,
            direction=src.direction,
            top=src.top,
            bottom=src.bottom,
            timeframe=getattr(src, "timeframe", "H1"),
            created_at=getattr(src, "created_at", None),
            source_event=getattr(src, "source_event", None),
            valid_until=getattr(src, "valid_until", None),
            status=getattr(src, "status", "active"),
            touch_count=getattr(src, "touch_count", 0),
            last_touch_bar=getattr(src, "last_touch_bar", None),
            meta=getattr(src, "meta", {}),
        )

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
            "meta": _unfreeze(self.meta),
        }


# =============================================================================
# Domain Models
# =============================================================================

@dataclass(frozen=True)
class EvidenceRef:
    """
    Immutable reference to a specific market structure or context evidence.
    """
    evidence_id: str
    kind: EvidenceKind
    bar_index: int
    price: float
    time: Optional[pd.Timestamp] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "evidence_id", _validate_str(self.evidence_id, "evidence_id"))
        if self.kind not in VALID_EVIDENCE_KINDS:
            raise ValueError(f"Invalid evidence kind '{self.kind}'. Allowed: {sorted(VALID_EVIDENCE_KINDS)}")
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        object.__setattr__(self, "bar_index", b_idx)
        p = _validate_finite_float(self.price, "price")
        object.__setattr__(self, "price", round(p, 3))
        if self.time is not None:
            object.__setattr__(self, "time", _parse_timestamp(self.time, "time"))
        object.__setattr__(self, "details", _freeze(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "bar_index": int(self.bar_index),
            "price": float(self.price),
            "time": self.time.isoformat() if self.time is not None else None,
            "details": _unfreeze(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceRef:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for EvidenceRef, got {type(data).__name__}")
        for req in ("evidence_id", "kind", "bar_index", "price"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in EvidenceRef.")
        return cls(
            evidence_id=data["evidence_id"],
            kind=data["kind"],
            bar_index=data["bar_index"],
            price=data["price"],
            time=data.get("time"),
            details=data.get("details", {}),
        )


@dataclass(frozen=True)
class StrategyContext:
    """
    Snapshot of all known market state as-of closed bar N.
    Guarantees strict zero-lookahead, monotonic timestamps, and deep immutability.
    """
    bar_index: int
    timestamp: pd.Timestamp
    bar_close_time: pd.Timestamp
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    atr14: float
    recent_swings: tuple[Union[SwingPoint, SwingPointSnapshot], ...] = field(default_factory=tuple)
    recent_structures: tuple[Union[StructureEvent, StructureEventSnapshot], ...] = field(default_factory=tuple)
    active_fvgs: tuple[Union[FairValueGap, FairValueGapSnapshot], ...] = field(default_factory=tuple)
    active_obs: tuple[Union[OrderBlock, OrderBlockSnapshot], ...] = field(default_factory=tuple)
    active_pools: tuple[Union[LiquidityPool, LiquidityPoolSnapshot], ...] = field(default_factory=tuple)
    recent_sweeps: tuple[Union[LiquiditySweep, LiquiditySweepSnapshot], ...] = field(default_factory=tuple)
    session_decision: Optional[Union[SessionDecision, SessionDecisionSnapshot]] = None
    htf_bias: Optional[Union[BiasState, BiasStateSnapshot]] = None
    active_htf_pois: tuple[Union[HTFPOI, HTFPOISnapshot], ...] = field(default_factory=tuple)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        object.__setattr__(self, "bar_index", b_idx)

        ts = _parse_timestamp(self.timestamp, "timestamp")
        bct = _parse_timestamp(self.bar_close_time, "bar_close_time")
        if bct < ts:
            raise ValueError(f"bar_close_time {bct} cannot be before timestamp {ts}")
        object.__setattr__(self, "timestamp", ts)
        object.__setattr__(self, "bar_close_time", bct)

        object.__setattr__(self, "symbol", _validate_str(self.symbol, "symbol"))
        object.__setattr__(self, "timeframe", _validate_str(self.timeframe, "timeframe"))

        o = _validate_finite_float(self.open, "open", min_val=0.00001)
        h = _validate_finite_float(self.high, "high", min_val=0.00001)
        l = _validate_finite_float(self.low, "low", min_val=0.00001)
        c = _validate_finite_float(self.close, "close", min_val=0.00001)
        v = _validate_finite_float(self.volume, "volume", min_val=0.0)
        atr = _validate_finite_float(self.atr14, "atr14", min_val=0.0)

        if h < l:
            raise ValueError(f"High {h} cannot be less than Low {l}")
        if h < max(o, c):
            raise ValueError(f"High {h} must be >= max(Open {o}, Close {c})")
        if l > min(o, c):
            raise ValueError(f"Low {l} must be <= min(Open {o}, Close {c})")

        object.__setattr__(self, "open", round(o, 3))
        object.__setattr__(self, "high", round(h, 3))
        object.__setattr__(self, "low", round(l, 3))
        object.__setattr__(self, "close", round(c, 3))
        object.__setattr__(self, "volume", float(v))
        object.__setattr__(self, "atr14", round(atr, 3))

        # Deep immutable snapshots for all structure collections
        object.__setattr__(self, "recent_swings", tuple(SwingPointSnapshot.from_source(x) for x in self.recent_swings))
        object.__setattr__(self, "recent_structures", tuple(StructureEventSnapshot.from_source(x) for x in self.recent_structures))
        object.__setattr__(self, "active_fvgs", tuple(FairValueGapSnapshot.from_source(x) for x in self.active_fvgs))
        object.__setattr__(self, "active_obs", tuple(OrderBlockSnapshot.from_source(x) for x in self.active_obs))
        object.__setattr__(self, "active_pools", tuple(LiquidityPoolSnapshot.from_source(x) for x in self.active_pools))
        object.__setattr__(self, "recent_sweeps", tuple(LiquiditySweepSnapshot.from_source(x) for x in self.recent_sweeps))
        if self.session_decision is not None:
            object.__setattr__(self, "session_decision", SessionDecisionSnapshot.from_source(self.session_decision))
        if self.htf_bias is not None:
            object.__setattr__(self, "htf_bias", BiasStateSnapshot.from_source(self.htf_bias))
        object.__setattr__(self, "active_htf_pois", tuple(HTFPOISnapshot.from_source(x) for x in self.active_htf_pois))
        object.__setattr__(self, "meta", _freeze(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_index": int(self.bar_index),
            "timestamp": self.timestamp.isoformat(),
            "bar_close_time": self.bar_close_time.isoformat(),
            "symbol": str(self.symbol),
            "timeframe": str(self.timeframe),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
            "atr14": float(self.atr14),
            "recent_swings": [s.to_dict() for s in self.recent_swings],
            "recent_structures": [s.to_dict() for s in self.recent_structures],
            "active_fvgs": [f.to_dict() for f in self.active_fvgs],
            "active_obs": [o.to_dict() for o in self.active_obs],
            "active_pools": [p.to_dict() for p in self.active_pools],
            "recent_sweeps": [sw.to_dict() for sw in self.recent_sweeps],
            "session_decision": self.session_decision.to_dict() if self.session_decision is not None else None,
            "htf_bias": self.htf_bias.to_dict() if self.htf_bias is not None else None,
            "active_htf_pois": [p.to_dict() for p in self.active_htf_pois],
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyContext:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for StrategyContext, got {type(data).__name__}")
        for req in (
            "bar_index", "timestamp", "bar_close_time", "symbol", "timeframe",
            "open", "high", "low", "close", "volume", "atr14"
        ):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in StrategyContext.")

        swings = tuple(SwingPointSnapshot.from_source(s) for s in data.get("recent_swings", ()))
        structures = tuple(StructureEventSnapshot.from_source(st) for st in data.get("recent_structures", ()))
        fvgs = tuple(FairValueGapSnapshot.from_source(f) for f in data.get("active_fvgs", ()))
        obs = tuple(OrderBlockSnapshot.from_source(o) for o in data.get("active_obs", ()))
        pools = tuple(LiquidityPoolSnapshot.from_source(p) for p in data.get("active_pools", ()))
        sweeps = tuple(LiquiditySweepSnapshot.from_source(sw) for sw in data.get("recent_sweeps", ()))

        sd = None
        if data.get("session_decision") is not None:
            sd = SessionDecisionSnapshot.from_source(data["session_decision"])

        hb = None
        if data.get("htf_bias") is not None:
            hb = BiasStateSnapshot.from_source(data["htf_bias"])

        htf_pois = tuple(HTFPOISnapshot.from_source(p) for p in data.get("active_htf_pois", ()))

        return cls(
            bar_index=data["bar_index"],
            timestamp=data["timestamp"],
            bar_close_time=data["bar_close_time"],
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            volume=data["volume"],
            atr14=data["atr14"],
            recent_swings=swings,
            recent_structures=structures,
            active_fvgs=fvgs,
            active_obs=obs,
            active_pools=pools,
            recent_sweeps=sweeps,
            session_decision=sd,
            htf_bias=hb,
            active_htf_pois=htf_pois,
            meta=data.get("meta", {}),
        )


@dataclass(frozen=True)
class StrategyProfile:
    """
    Static metadata and configuration profile of an SMC Strategy.
    """
    strategy_id: str
    name: str
    version: str = "1.0.0"
    style: StrategyStyle = "continuation"
    allowed_directions: tuple[TradeDirection, ...] = ("BUY", "SELL")
    timeframes: tuple[str, ...] = ("M1", "M5", "M15")
    max_setup_age_bars: int = 15
    cooldown_bars: int = 3
    min_rr: float = 1.5
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "strategy_id", _validate_base_token(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "name", _validate_str(self.name, "name"))
        object.__setattr__(self, "version", _validate_str(self.version, "version"))
        if self.style not in {"reversal", "continuation", "time_based"}:
            raise ValueError(f"Invalid strategy style '{self.style}'.")
        if not self.allowed_directions:
            raise ValueError("allowed_directions cannot be empty.")
        
        directions = []
        seen_dirs = set()
        for d in self.allowed_directions:
            if d not in {"BUY", "SELL"}:
                raise ValueError(f"Invalid direction '{d}' in allowed_directions.")
            if d in seen_dirs:
                raise ValueError(f"Duplicate direction '{d}' in allowed_directions.")
            seen_dirs.add(d)
            directions.append(d)

        if not self.timeframes:
            raise ValueError("timeframes cannot be empty.")
        for tf in self.timeframes:
            _validate_str(tf, "timeframe")

        max_age = _validate_non_negative_int(self.max_setup_age_bars, "max_setup_age_bars")
        if max_age <= 0:
            raise ValueError(f"max_setup_age_bars must be > 0, got {max_age}")

        cooldown = _validate_non_negative_int(self.cooldown_bars, "cooldown_bars")
        min_rr = _validate_finite_float(self.min_rr, "min_rr", min_val=0.0001)

        object.__setattr__(self, "allowed_directions", tuple(directions))
        object.__setattr__(self, "timeframes", tuple(str(tf) for tf in self.timeframes))
        object.__setattr__(self, "max_setup_age_bars", max_age)
        object.__setattr__(self, "cooldown_bars", cooldown)
        object.__setattr__(self, "min_rr", round(min_rr, 2))
        object.__setattr__(self, "params", _freeze(self.params))

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "version": self.version,
            "style": self.style,
            "allowed_directions": list(self.allowed_directions),
            "timeframes": list(self.timeframes),
            "max_setup_age_bars": int(self.max_setup_age_bars),
            "cooldown_bars": int(self.cooldown_bars),
            "min_rr": float(self.min_rr),
            "params": _unfreeze(self.params),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyProfile:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for StrategyProfile, got {type(data).__name__}")
        for req in ("strategy_id", "name"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in StrategyProfile.")
        return cls(
            strategy_id=data["strategy_id"],
            name=data["name"],
            version=data.get("version", "1.0.0"),
            style=data.get("style", "continuation"),
            allowed_directions=tuple(data.get("allowed_directions", ("BUY", "SELL"))),
            timeframes=tuple(data.get("timeframes", ("M1", "M5", "M15"))),
            max_setup_age_bars=data.get("max_setup_age_bars", 15),
            cooldown_bars=data.get("cooldown_bars", 3),
            min_rr=data.get("min_rr", 1.5),
            params=data.get("params", {}),
        )


@dataclass(frozen=True)
class CandidateSetup:
    """
    A proposed trading setup from a strategy at bar N.
    Enforces strict post-rounding price geometry, finite numbers, and unique evidences.
    """
    setup_id: str
    strategy_id: str
    direction: TradeDirection
    bar_index: int
    timestamp: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    planned_rr: float
    evidences: tuple[EvidenceRef, ...]
    evidence_cluster_id: str
    expiry_bar: int
    target_type: TargetType = "opposing_pool"
    quality_scores: Mapping[str, float] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "setup_id", _validate_str(self.setup_id, "setup_id"))
        object.__setattr__(self, "strategy_id", _validate_base_token(self.strategy_id, "strategy_id"))
        if self.direction not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'BUY' or 'SELL'.")
        
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        exp_bar = _validate_non_negative_int(self.expiry_bar, "expiry_bar")
        if exp_bar < b_idx:
            raise ValueError(f"expiry_bar ({exp_bar}) cannot be before bar_index ({b_idx})")

        ts = _parse_timestamp(self.timestamp, "timestamp")
        object.__setattr__(self, "timestamp", ts)
        object.__setattr__(self, "bar_index", b_idx)
        object.__setattr__(self, "expiry_bar", exp_bar)

        # 1. Validate finite positive numeric
        ep_raw = _validate_finite_float(self.entry_price, "entry_price", min_val=0.00001)
        sl_raw = _validate_finite_float(self.stop_loss, "stop_loss", min_val=0.00001)
        tp_raw = _validate_finite_float(self.take_profit, "take_profit", min_val=0.00001)
        rr_raw = _validate_finite_float(self.planned_rr, "planned_rr", min_val=0.00001)

        # 2. Round prices first according to precision contract (3 decimals for XAU/USD)
        ep = round(ep_raw, 3)
        sl = round(sl_raw, 3)
        tp = round(tp_raw, 3)
        rr = round(rr_raw, 2)

        # 3. Check geometry on the ROUNDED values to be stored
        if self.direction == "BUY":
            if not (sl < ep < tp):
                raise ValueError(
                    f"Invalid BUY geometry after precision rounding (3 decimals): "
                    f"Stop Loss ({sl}) must be strictly less than Entry ({ep}) "
                    f"and Entry must be strictly less than Take Profit ({tp}). "
                    f"Raw inputs: SL={sl_raw}, Entry={ep_raw}, TP={tp_raw}"
                )
        else:  # SELL
            if not (tp < ep < sl):
                raise ValueError(
                    f"Invalid SELL geometry after precision rounding (3 decimals): "
                    f"Take Profit ({tp}) must be strictly less than Entry ({ep}) "
                    f"and Entry must be strictly less than Stop Loss ({sl}). "
                    f"Raw inputs: TP={tp_raw}, Entry={ep_raw}, SL={sl_raw}"
                )

        if not self.evidences:
            raise ValueError("CandidateSetup must contain at least one EvidenceRef.")

        # Duplicate evidence check
        seen_evidence_ids = set()
        frozen_evidences = []
        for ev in self.evidences:
            if isinstance(ev, dict):
                ev = EvidenceRef.from_dict(ev)
            elif not isinstance(ev, EvidenceRef):
                raise StrictModelTypeError(f"Expected EvidenceRef, got {type(ev).__name__}")
            if ev.evidence_id in seen_evidence_ids:
                raise ValueError(f"Duplicate evidence ID '{ev.evidence_id}' in candidate setup {self.setup_id}")
            seen_evidence_ids.add(ev.evidence_id)
            frozen_evidences.append(ev)

        object.__setattr__(self, "evidence_cluster_id", _validate_cluster_id(self.evidence_cluster_id, "evidence_cluster_id"))

        if self.target_type not in {"opposing_pool", "fixed_rr", "structure_swing"}:
            raise ValueError(f"Invalid target_type '{self.target_type}'.")

        object.__setattr__(self, "entry_price", ep)
        object.__setattr__(self, "stop_loss", sl)
        object.__setattr__(self, "take_profit", tp)
        object.__setattr__(self, "planned_rr", rr)
        object.__setattr__(self, "evidences", tuple(frozen_evidences))
        object.__setattr__(self, "quality_scores", _freeze(self.quality_scores))
        object.__setattr__(self, "meta", _freeze(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "strategy_id": self.strategy_id,
            "direction": self.direction,
            "bar_index": int(self.bar_index),
            "timestamp": self.timestamp.isoformat(),
            "entry_price": float(self.entry_price),
            "stop_loss": float(self.stop_loss),
            "take_profit": float(self.take_profit),
            "planned_rr": float(self.planned_rr),
            "evidences": [ev.to_dict() for ev in self.evidences],
            "evidence_cluster_id": self.evidence_cluster_id,
            "expiry_bar": int(self.expiry_bar),
            "target_type": self.target_type,
            "quality_scores": _unfreeze(self.quality_scores),
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateSetup:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for CandidateSetup, got {type(data).__name__}")
        for req in (
            "setup_id", "strategy_id", "direction", "bar_index", "timestamp",
            "entry_price", "stop_loss", "take_profit", "planned_rr",
            "evidences", "evidence_cluster_id", "expiry_bar"
        ):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in CandidateSetup.")
        evidences = tuple(
            EvidenceRef.from_dict(ev) if isinstance(ev, dict) else ev
            for ev in data["evidences"]
        )
        return cls(
            setup_id=data["setup_id"],
            strategy_id=data["strategy_id"],
            direction=data["direction"],
            bar_index=data["bar_index"],
            timestamp=data["timestamp"],
            entry_price=data["entry_price"],
            stop_loss=data["stop_loss"],
            take_profit=data["take_profit"],
            planned_rr=data["planned_rr"],
            evidences=evidences,
            evidence_cluster_id=data["evidence_cluster_id"],
            expiry_bar=data["expiry_bar"],
            target_type=data.get("target_type", "opposing_pool"),
            quality_scores=data.get("quality_scores", {}),
            meta=data.get("meta", {}),
        )


@dataclass(frozen=True)
class MarketRegime:
    """
    Market regime classification result at bar N.
    """
    regime: MarketRegimeType
    bar_index: int
    timestamp: pd.Timestamp
    efficiency_ratio: float
    atr_percentile: float
    metrics: Mapping[str, float] = field(default_factory=dict)
    reason: str = "ok"
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.regime not in VALID_REGIMES:
            raise ValueError(f"Invalid regime '{self.regime}'. Allowed: {sorted(VALID_REGIMES)}")
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        object.__setattr__(self, "bar_index", b_idx)

        ts = _parse_timestamp(self.timestamp, "timestamp")
        object.__setattr__(self, "timestamp", ts)

        er = _validate_finite_float(self.efficiency_ratio, "efficiency_ratio", min_val=-1e-6, max_val=1.0 + 1e-6)
        er_clamped = max(0.0, min(1.0, er))

        pct = _validate_finite_float(self.atr_percentile, "atr_percentile", min_val=-1e-6, max_val=100.0 + 1e-6)
        pct_clamped = max(0.0, min(100.0, pct))

        object.__setattr__(self, "efficiency_ratio", round(er_clamped, 4))
        object.__setattr__(self, "atr_percentile", round(pct_clamped, 2))
        object.__setattr__(self, "reason", _validate_str(self.reason, "reason"))
        object.__setattr__(self, "metrics", _freeze(self.metrics))
        object.__setattr__(self, "meta", _freeze(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "bar_index": int(self.bar_index),
            "timestamp": self.timestamp.isoformat(),
            "efficiency_ratio": float(self.efficiency_ratio),
            "atr_percentile": float(self.atr_percentile),
            "metrics": _unfreeze(self.metrics),
            "reason": self.reason,
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketRegime:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for MarketRegime, got {type(data).__name__}")
        for req in ("regime", "bar_index", "timestamp", "efficiency_ratio", "atr_percentile"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in MarketRegime.")
        return cls(
            regime=data["regime"],
            bar_index=data["bar_index"],
            timestamp=data["timestamp"],
            efficiency_ratio=data["efficiency_ratio"],
            atr_percentile=data["atr_percentile"],
            metrics=data.get("metrics", {}),
            reason=data.get("reason", "ok"),
            meta=data.get("meta", {}),
        )


@dataclass(frozen=True)
class StrategyEvaluation:
    """
    Evaluation of a single candidate setup against eligibility gates and scoring formulas.
    """
    candidate: CandidateSetup
    status: EvaluationStatus
    rejection_reasons: tuple[str, ...] = field(default_factory=tuple)
    regime_score: float = 0.0
    setup_score: float = 0.0
    context_score: float = 0.0
    exec_score: float = 0.0
    total_score: float = 0.0
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.candidate, dict):
            object.__setattr__(self, "candidate", CandidateSetup.from_dict(self.candidate))
        elif not isinstance(self.candidate, CandidateSetup):
            raise StrictModelTypeError(f"candidate must be a CandidateSetup, got {type(self.candidate).__name__}")
        if self.status not in {"ELIGIBLE", "REJECTED"}:
            raise ValueError(f"Invalid status '{self.status}'. Must be 'ELIGIBLE' or 'REJECTED'.")

        reasons = tuple(_validate_str(r, "rejection_reason") for r in self.rejection_reasons)
        if self.status == "REJECTED" and len(reasons) == 0:
            raise ValueError("REJECTED StrategyEvaluation must specify at least one rejection reason.")
        if self.status == "ELIGIBLE" and len(reasons) > 0:
            raise ValueError("ELIGIBLE StrategyEvaluation cannot have rejection reasons.")

        r_score = _validate_finite_float(self.regime_score, "regime_score", min_val=0.0, max_val=100.0)
        s_score = _validate_finite_float(self.setup_score, "setup_score", min_val=0.0, max_val=100.0)
        c_score = _validate_finite_float(self.context_score, "context_score", min_val=0.0, max_val=100.0)
        e_score = _validate_finite_float(self.exec_score, "exec_score", min_val=0.0, max_val=100.0)
        t_score = _validate_finite_float(self.total_score, "total_score", min_val=0.0, max_val=100.0)

        object.__setattr__(self, "rejection_reasons", reasons)
        object.__setattr__(self, "regime_score", round(r_score, 2))
        object.__setattr__(self, "setup_score", round(s_score, 2))
        object.__setattr__(self, "context_score", round(c_score, 2))
        object.__setattr__(self, "exec_score", round(e_score, 2))
        object.__setattr__(self, "total_score", round(t_score, 2))
        object.__setattr__(self, "details", _freeze(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "status": self.status,
            "rejection_reasons": list(self.rejection_reasons),
            "regime_score": float(self.regime_score),
            "setup_score": float(self.setup_score),
            "context_score": float(self.context_score),
            "exec_score": float(self.exec_score),
            "total_score": float(self.total_score),
            "details": _unfreeze(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyEvaluation:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for StrategyEvaluation, got {type(data).__name__}")
        for req in ("candidate", "status"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in StrategyEvaluation.")
        cand = (
            CandidateSetup.from_dict(data["candidate"])
            if isinstance(data["candidate"], dict)
            else data["candidate"]
        )
        return cls(
            candidate=cand,
            status=data["status"],
            rejection_reasons=tuple(data.get("rejection_reasons", ())),
            regime_score=data.get("regime_score", 0.0),
            setup_score=data.get("setup_score", 0.0),
            context_score=data.get("context_score", 0.0),
            exec_score=data.get("exec_score", 0.0),
            total_score=data.get("total_score", 0.0),
            details=data.get("details", {}),
        )


@dataclass(frozen=True)
class SelectionDecision:
    """
    Final decision produced by the Confluence & Selection Engine at bar N.
    Enforces comprehensive cross-field consistency between decision, setup, strategy, and payload.
    """
    decision_id: str
    bar_index: int
    timestamp: pd.Timestamp
    action: DecisionAction
    selected_setup: Optional[CandidateSetup] = None
    primary_strategy_id: Optional[str] = None
    supporting_strategy_ids: tuple[str, ...] = field(default_factory=tuple)
    regime: Optional[MarketRegime] = None
    evaluations: tuple[StrategyEvaluation, ...] = field(default_factory=tuple)
    reason: str = "ok"
    score_gap: Optional[float] = None
    execution_payload: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "decision_id", _validate_str(self.decision_id, "decision_id"))
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        object.__setattr__(self, "bar_index", b_idx)

        ts = _parse_timestamp(self.timestamp, "timestamp")
        object.__setattr__(self, "timestamp", ts)

        if self.action not in {"SELECT", "NO_TRADE"}:
            raise ValueError(f"Invalid action '{self.action}'. Must be 'SELECT' or 'NO_TRADE'.")

        # Canonical decision_id verification
        expected_id = make_decision_id(b_idx, self.action, self.primary_strategy_id)
        if self.decision_id != expected_id:
            raise ValueError(
                f"decision_id '{self.decision_id}' does not match canonical '{expected_id}'"
            )

        # Regime synchronization
        if self.regime is not None:
            if isinstance(self.regime, dict):
                object.__setattr__(self, "regime", MarketRegime.from_dict(self.regime))
            elif not isinstance(self.regime, MarketRegime):
                raise StrictModelTypeError(f"regime must be a MarketRegime, got {type(self.regime).__name__}")
            if self.regime.bar_index != b_idx:
                raise ValueError(
                    f"regime.bar_index ({self.regime.bar_index}) does not match decision.bar_index ({b_idx})"
                )
            if self.regime.timestamp != ts:
                raise ValueError(
                    f"regime.timestamp ({self.regime.timestamp}) does not match decision.timestamp ({ts})"
                )

        # Reason & score_gap validation
        if self.reason not in VALID_DECISION_REASONS:
            raise ValueError(f"Invalid reason '{self.reason}'. Must be one of {sorted(VALID_DECISION_REASONS)}")
        object.__setattr__(self, "reason", self.reason)

        if self.action == "SELECT":
            if self.reason != "ok":
                raise ValueError(f"SelectionDecision with action='SELECT' must have reason='ok', got '{self.reason}'")
            if self.score_gap is not None:
                gap = _validate_finite_float(self.score_gap, "score_gap", min_val=0.0)
                object.__setattr__(self, "score_gap", round(gap, 2))
        else:  # NO_TRADE
            if self.reason == "ok":
                raise ValueError("SelectionDecision with action='NO_TRADE' cannot have reason='ok'")
            if self.reason == "conflicting_direction":
                if self.score_gap is None:
                    raise ValueError("SelectionDecision with reason='conflicting_direction' requires a finite score_gap")
                gap = _validate_finite_float(self.score_gap, "score_gap", min_val=0.0)
                object.__setattr__(self, "score_gap", round(gap, 2))
            else:
                if self.score_gap is not None:
                    raise ValueError(f"SelectionDecision with reason='{self.reason}' must have score_gap=None, got {self.score_gap}")

        # T53.8 execution_payload must be strictly empty dict
        if bool(self.execution_payload):
            raise ValueError(f"T53.8 execution_payload must be empty mapping {{}}, got {dict(self.execution_payload)}")
        object.__setattr__(self, "execution_payload", MappingProxyType({}))

        # Evaluations validation: unique setup_ids and canonical sort
        if not isinstance(self.evaluations, (tuple, list)):
            raise StrictModelTypeError(f"evaluations must be a sequence, got {type(self.evaluations).__name__}")
        converted_evals = []
        seen_setup_ids = set()
        for ev in self.evaluations:
            if isinstance(ev, dict):
                ev = StrategyEvaluation.from_dict(ev)
            elif not isinstance(ev, StrategyEvaluation):
                raise StrictModelTypeError(f"Expected StrategyEvaluation, got {type(ev).__name__}")
            sid = ev.candidate.setup_id
            if sid in seen_setup_ids:
                raise ValueError(f"Duplicate candidate setup_id '{sid}' in evaluations")
            seen_setup_ids.add(sid)
            converted_evals.append(ev)

        converted_evals.sort(key=lambda ev: (
            -float(ev.total_score),
            -float(ev.setup_score),
            -float(ev.context_score),
            -float(ev.exec_score),
            -float(ev.regime_score),
            -float(ev.candidate.planned_rr),
            str(ev.candidate.strategy_id),
            str(ev.candidate.setup_id),
        ))
        object.__setattr__(self, "evaluations", tuple(converted_evals))

        if self.action == "SELECT":
            if self.selected_setup is None:
                raise ValueError("SelectionDecision with action='SELECT' must have a valid CandidateSetup.")
            if isinstance(self.selected_setup, dict):
                object.__setattr__(self, "selected_setup", CandidateSetup.from_dict(self.selected_setup))
            elif not isinstance(self.selected_setup, CandidateSetup):
                raise StrictModelTypeError(f"selected_setup must be a CandidateSetup, got {type(self.selected_setup).__name__}")

            if self.primary_strategy_id is None:
                raise ValueError("SelectionDecision with action='SELECT' must have a non-empty primary_strategy_id.")
            strat = _validate_base_token(self.primary_strategy_id, "primary_strategy_id")
            object.__setattr__(self, "primary_strategy_id", strat)

            # Cross-field: primary_strategy_id must match selected_setup.strategy_id
            if strat != self.selected_setup.strategy_id:
                raise ValueError(
                    f"primary_strategy_id '{strat}' does not match "
                    f"selected_setup.strategy_id '{self.selected_setup.strategy_id}'"
                )

            # Cross-field: cannot select expired setup
            if b_idx > self.selected_setup.expiry_bar:
                raise ValueError(
                    f"Cannot select expired setup: decision bar_index {b_idx} > "
                    f"setup expiry_bar {self.selected_setup.expiry_bar}"
                )

            # Cross-field: cannot select future setup
            if self.selected_setup.bar_index > b_idx:
                raise ValueError(
                    f"Cannot select future setup: setup bar_index {self.selected_setup.bar_index} > "
                    f"decision bar_index {b_idx}"
                )

            if self.selected_setup.timestamp > ts:
                raise ValueError(
                    f"Cannot select future setup: setup timestamp {self.selected_setup.timestamp} is after "
                    f"decision timestamp {ts}"
                )

            # Cross-field: selected_setup must appear exactly once in evaluations and be ELIGIBLE
            matching_evals = [ev for ev in converted_evals if ev.candidate.setup_id == self.selected_setup.setup_id]
            if len(matching_evals) != 1:
                raise ValueError(
                    f"selected_setup '{self.selected_setup.setup_id}' must appear exactly once in evaluations, found {len(matching_evals)}"
                )
            if matching_evals[0].candidate != self.selected_setup:
                raise ValueError("Evaluations contains a setup with matching setup_id but differing contents.")
            if matching_evals[0].status != "ELIGIBLE":
                raise ValueError(f"Selected setup cannot be marked {matching_evals[0].status} in evaluations.")

            # Cross-field: supporting_strategy_ids
            supporting = []
            seen_supporting = set()
            for s in self.supporting_strategy_ids:
                s_tok = _validate_base_token(s, "supporting_strategy_id")
                if s_tok == strat:
                    raise ValueError(f"supporting_strategy_ids cannot contain primary_strategy_id '{strat}'")
                if s_tok in seen_supporting:
                    raise ValueError(f"supporting_strategy_ids contains duplicate strategy ID '{s_tok}'")
                seen_supporting.add(s_tok)
                supporting.append(s_tok)
            object.__setattr__(self, "supporting_strategy_ids", tuple(sorted(supporting)))

        else:  # NO_TRADE
            if self.selected_setup is not None:
                raise ValueError("SelectionDecision with action='NO_TRADE' cannot have a selected_setup.")
            if self.primary_strategy_id is not None:
                raise ValueError("SelectionDecision with action='NO_TRADE' cannot have a primary_strategy_id.")
            if len(self.supporting_strategy_ids) > 0:
                raise ValueError("SelectionDecision with action='NO_TRADE' cannot have supporting_strategy_ids.")
            object.__setattr__(self, "supporting_strategy_ids", ())

        object.__setattr__(self, "meta", _freeze(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "bar_index": int(self.bar_index),
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "selected_setup": self.selected_setup.to_dict() if self.selected_setup is not None else None,
            "primary_strategy_id": self.primary_strategy_id,
            "supporting_strategy_ids": list(self.supporting_strategy_ids),
            "regime": self.regime.to_dict() if self.regime is not None else None,
            "evaluations": [ev.to_dict() for ev in self.evaluations],
            "reason": self.reason,
            "score_gap": float(self.score_gap) if self.score_gap is not None else None,
            "execution_payload": _unfreeze(self.execution_payload),
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SelectionDecision:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for SelectionDecision, got {type(data).__name__}")
        allowed = {
            "decision_id", "bar_index", "timestamp", "action", "selected_setup",
            "primary_strategy_id", "supporting_strategy_ids", "regime",
            "evaluations", "reason", "score_gap", "execution_payload", "meta",
        }
        unknown = set(data.keys()) - allowed
        if unknown:
            raise KeyError(f"Unknown fields in SelectionDecision: {sorted(unknown)}")
        for req in ("decision_id", "bar_index", "timestamp", "action"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in SelectionDecision.")
        setup = (
            CandidateSetup.from_dict(data["selected_setup"])
            if data.get("selected_setup") is not None
            else None
        )
        regime = (
            MarketRegime.from_dict(data["regime"])
            if data.get("regime") is not None
            else None
        )
        evals = tuple(
            StrategyEvaluation.from_dict(ev) if isinstance(ev, dict) else ev
            for ev in data.get("evaluations", ())
        )
        return cls(
            decision_id=data["decision_id"],
            bar_index=data["bar_index"],
            timestamp=data["timestamp"],
            action=data["action"],
            selected_setup=setup,
            primary_strategy_id=data.get("primary_strategy_id"),
            supporting_strategy_ids=tuple(data.get("supporting_strategy_ids", ())),
            regime=regime,
            evaluations=evals,
            reason=data.get("reason", "ok"),
            score_gap=data.get("score_gap"),
            execution_payload=data.get("execution_payload", {}),
            meta=data.get("meta", {}),
        )
