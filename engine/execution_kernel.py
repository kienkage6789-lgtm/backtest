"""
engine/execution_kernel.py
--------------------------
Shared platform-level execution kernel for order matching, dynamic SL/TP,
position lifecycle, mark-to-market valuation, and PnL accounting.

Supports both legacy strategy execution (via BacktestEngine facade) and
future Wave 1 execution (T53.9.3) without strategy dependencies.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import datetime
import enum
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

import numpy as np
import pandas as pd

EXACT_PRIMITIVE_TYPES = (
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
)

EXACT_DATETIME_TYPES = (
    datetime.date,
    datetime.datetime,
    datetime.time,
    datetime.timedelta,
)


def _deep_freeze(value: Any, _seen: set[int] | None = None) -> Any:
    """
    Recursively freeze mappings, sequences, sets, and frozen dataclasses into deeply immutable structures.
    Mappings -> MappingProxyType({_deep_freeze(k): _deep_freeze(v)})
    Sequences (except str, bytes) -> tuple(_deep_freeze(v))
    Sets -> frozenset(_deep_freeze(v))
    Exact scalars (str, int, float, bool, None, bytes, exact datetime types, pd.Timestamp, pd.Timedelta) -> preserved
    NumPy scalars (np.generic) -> recursively converted to native Python scalars via .item()
    Frozen dataclasses -> recursively frozen copy of the dataclass
    Enum members -> fail-closed (TypeError) to avoid mutable alias leakage via member attributes or .value
    Subclasses of primitives -> fail-closed (TypeError) to avoid mutable attribute leakage
    Mutable containers (np.ndarray, pd.Series, pd.DataFrame, non-frozen dataclasses) -> fail closed (TypeError)
    Unsupported mutable types -> fail closed (TypeError).
    """
    val_type = type(value)

    # 1. Exact primitive types (rejecting custom subclasses of int, str, bytes, etc.)
    if val_type in EXACT_PRIMITIVE_TYPES:
        return value

    # 2. Explicit mutable containers (fail-closed)
    if isinstance(value, (np.ndarray, pd.Series, pd.DataFrame)):
        raise TypeError(f"Unsupported mutable container for deep immutability: {val_type.__name__}")

    # 3. Explicit Enum reject (fail-closed to prevent mutable .value alias leakage)
    if isinstance(value, enum.Enum):
        raise TypeError(f"Enum types are unsupported for deep immutability: {val_type.__name__}")

    # 4. NumPy scalars (np.generic) - unpacked recursively and normalized to Python primitives
    if isinstance(value, np.generic):
        if _seen is None:
            _seen = set()
        val_id = id(value)
        if val_id in _seen:
            raise TypeError(f"Circular reference detected in NumPy generic: {val_type.__name__}")
        item = value.item()
        if item is value:
            raise TypeError(f"Unsupported NumPy generic object cannot be unpacked: {val_type.__name__}")
        _seen.add(val_id)
        try:
            return _deep_freeze(item, _seen=_seen)
        finally:
            _seen.remove(val_id)

    # 5. Exact datetime & pandas temporal types
    if val_type in EXACT_DATETIME_TYPES or val_type is pd.Timestamp or val_type is pd.Timedelta:
        return value

    if _seen is None:
        _seen = set()
    val_id = id(value)
    if val_id in _seen:
        raise TypeError(f"Circular reference detected in mutable container: {val_type.__name__}")
    _seen.add(val_id)

    try:
        if isinstance(value, collections.abc.Mapping):
            return MappingProxyType({_deep_freeze(k, _seen): _deep_freeze(v, _seen) for k, v in value.items()})
        if isinstance(value, (collections.abc.Set, frozenset)):
            return frozenset(_deep_freeze(v, _seen) for v in value)
        if isinstance(value, (list, tuple)):
            return tuple(_deep_freeze(v, _seen) for v in value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            if getattr(value.__dataclass_params__, "frozen", False):
                new_obj = object.__new__(type(value))
                for f in dataclasses.fields(value):
                    object.__setattr__(new_obj, f.name, _deep_freeze(getattr(value, f.name), _seen))
                return new_obj
            else:
                raise TypeError(f"Non-frozen dataclass is mutable and unsupported: {val_type.__name__}")
        raise TypeError(f"Unsupported mutable type for deep immutability: {val_type.__name__}")
    finally:
        _seen.remove(val_id)


def _deep_thaw(value: Any) -> Any:
    """
    Recursively convert MappingProxyType to dict, tuple/frozenset to deterministic list
    so that public consumers receive independent defensive copies.
    Mapping keys remain preserved and hashable.
    """
    val_type = type(value)
    if val_type in EXACT_PRIMITIVE_TYPES:
        return value
    if isinstance(value, collections.abc.Mapping):
        return {k: _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_thaw(v) for v in value]
    if isinstance(value, (collections.abc.Set, frozenset)):
        try:
            sorted_items = sorted(value)
        except TypeError:
            sorted_items = sorted(value, key=str)
        return [_deep_thaw(v) for v in sorted_items]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _deep_thaw(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


def _require_finite_result(val: Any, name: str) -> float:
    """
    Validate that an arithmetic calculation result is a finite float (rejecting bool, NaN, and Inf).
    """
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise TypeError(f"'{name}' must be a float or int, got {type(val).__name__}: {val!r}")
    f_val = float(val)
    if math.isnan(f_val) or math.isinf(f_val):
        raise ValueError(f"'{name}' must be finite, got {f_val}")
    return f_val


def _validate_float(val: Any, name: str, min_val: float | None = None, strictly_positive: bool = False) -> float:
    """Validate that val is a finite float (rejecting bool)."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise TypeError(f"'{name}' must be a float or int, got {type(val).__name__}: {val!r}")
    f_val = float(val)
    if math.isnan(f_val) or math.isinf(f_val):
        raise ValueError(f"'{name}' must be finite, got {f_val}")
    if strictly_positive and f_val <= 0.0:
        raise ValueError(f"'{name}' must be strictly positive (>0), got {f_val}")
    if min_val is not None and f_val < min_val:
        raise ValueError(f"'{name}' must be >= {min_val}, got {f_val}")
    return f_val


def _validate_int(val: Any, name: str, min_val: int | None = None) -> int:
    """Validate that val is an integer (rejecting bool)."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise TypeError(f"'{name}' must be an int, got {type(val).__name__}: {val!r}")
    if min_val is not None and val < min_val:
        raise ValueError(f"'{name}' must be >= {min_val}, got {val}")
    return val


@dataclass(frozen=True)
class ExecutionBar:
    """
    Immutable bar container for kernel processing.
    OHLC values must be finite numbers; index and timestamp non-negative ints.
    time_value is deeply frozen to prevent caller mutation leakage.
    """
    bar_index: int
    timestamp: int
    time_value: Any
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        _validate_int(self.bar_index, "bar_index", min_val=0)
        _validate_int(self.timestamp, "timestamp", min_val=0)
        object.__setattr__(self, "time_value", _deep_freeze(self.time_value))
        object.__setattr__(self, "open", _validate_float(self.open, "open"))
        object.__setattr__(self, "high", _validate_float(self.high, "high"))
        object.__setattr__(self, "low", _validate_float(self.low, "low"))
        object.__setattr__(self, "close", _validate_float(self.close, "close"))


@dataclass(frozen=True)
class PositionState:
    """
    Immutable state of an active trading position.
    SL and TP levels are stored per-position and checked dynamically.
    For legacy source, non-positive SL/TP levels are permitted to support
    large fixed-distance stop loss values from historical backtests.
    """
    direction: Literal["BUY", "SELL"]
    entry_price: float
    entry_timestamp: int
    entry_time_value: Any
    sl_price: float | None
    tp_price: float | None
    multiplier: float
    round_trip_commission: float
    source: Literal["legacy", "wave1"]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got {self.direction!r}")
        if self.source not in ("legacy", "wave1"):
            raise ValueError(f"source must be 'legacy' or 'wave1', got {self.source!r}")

        if self.source == "wave1":
            object.__setattr__(self, "entry_price", _validate_float(self.entry_price, "entry_price", strictly_positive=True))
            object.__setattr__(self, "multiplier", _validate_float(self.multiplier, "multiplier", strictly_positive=True))
            object.__setattr__(self, "round_trip_commission", _validate_float(self.round_trip_commission, "round_trip_commission", min_val=0.0))
        else:
            object.__setattr__(self, "entry_price", _validate_float(self.entry_price, "entry_price"))
            object.__setattr__(self, "multiplier", _validate_float(self.multiplier, "multiplier"))
            object.__setattr__(self, "round_trip_commission", _validate_float(self.round_trip_commission, "round_trip_commission"))

        _validate_int(self.entry_timestamp, "entry_timestamp", min_val=0)
        object.__setattr__(self, "entry_time_value", _deep_freeze(self.entry_time_value))

        sl = None
        if self.sl_price is not None:
            if self.source == "wave1":
                sl = _validate_float(self.sl_price, "sl_price", strictly_positive=True)
            else:
                sl = _validate_float(self.sl_price, "sl_price")
            object.__setattr__(self, "sl_price", sl)

        tp = None
        if self.tp_price is not None:
            if self.source == "wave1":
                tp = _validate_float(self.tp_price, "tp_price", strictly_positive=True)
            else:
                tp = _validate_float(self.tp_price, "tp_price")
            object.__setattr__(self, "tp_price", tp)

        if self.source == "wave1" and (sl is None or tp is None):
            raise ValueError("Wave 1 position must have both sl_price and tp_price defined")

        # Relative geometry checks
        if self.direction == "BUY":
            if sl is not None and sl >= self.entry_price:
                raise ValueError(f"BUY sl_price ({sl}) must be strictly less than entry_price ({self.entry_price})")
            if tp is not None and tp <= self.entry_price:
                raise ValueError(f"BUY tp_price ({tp}) must be strictly greater than entry_price ({self.entry_price})")
        else:  # SELL
            if sl is not None and sl <= self.entry_price:
                raise ValueError(f"SELL sl_price ({sl}) must be strictly greater than entry_price ({self.entry_price})")
            if tp is not None and tp >= self.entry_price:
                raise ValueError(f"SELL tp_price ({tp}) must be strictly less than entry_price ({self.entry_price})")

        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class OpenInstruction:
    """
    Order instruction passed into kernel.process_open().
    - OPEN_OR_REVERSE: Opens if flat; ignores if same direction; atomically reverses if opposite.
    - CLOSE_ONLY: Closes existing position if opposite direction; no-op if flat or same direction.
    """
    action: Literal["OPEN_OR_REVERSE", "CLOSE_ONLY"]
    direction: Literal["BUY", "SELL"]
    entry_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    source: Literal["legacy", "wave1"] = "legacy"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ("OPEN_OR_REVERSE", "CLOSE_ONLY"):
            raise ValueError(f"action must be 'OPEN_OR_REVERSE' or 'CLOSE_ONLY', got {self.action!r}")
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got {self.direction!r}")
        if self.source not in ("legacy", "wave1"):
            raise ValueError(f"source must be 'legacy' or 'wave1', got {self.source!r}")

        if self.action == "CLOSE_ONLY":
            if self.entry_price is not None or self.sl_price is not None or self.tp_price is not None:
                raise ValueError("CLOSE_ONLY instruction must not specify entry_price, sl_price, or tp_price")
        else:  # OPEN_OR_REVERSE
            if self.entry_price is None:
                raise ValueError("OPEN_OR_REVERSE instruction must specify entry_price")
            if self.source == "wave1":
                object.__setattr__(self, "entry_price", _validate_float(self.entry_price, "entry_price", strictly_positive=True))
            else:
                object.__setattr__(self, "entry_price", _validate_float(self.entry_price, "entry_price"))

            sl = None
            if self.sl_price is not None:
                if self.source == "wave1":
                    sl = _validate_float(self.sl_price, "sl_price", strictly_positive=True)
                else:
                    sl = _validate_float(self.sl_price, "sl_price")
                object.__setattr__(self, "sl_price", sl)

            tp = None
            if self.tp_price is not None:
                if self.source == "wave1":
                    tp = _validate_float(self.tp_price, "tp_price", strictly_positive=True)
                else:
                    tp = _validate_float(self.tp_price, "tp_price")
                object.__setattr__(self, "tp_price", tp)

            if self.source == "wave1" and (sl is None or tp is None):
                raise ValueError("Wave 1 OPEN_OR_REVERSE instruction must have both sl_price and tp_price defined")

            if self.direction == "BUY":
                if sl is not None and sl >= self.entry_price:
                    raise ValueError(f"BUY sl_price ({sl}) must be strictly less than entry_price ({self.entry_price})")
                if tp is not None and tp <= self.entry_price:
                    raise ValueError(f"BUY tp_price ({tp}) must be strictly greater than entry_price ({self.entry_price})")
            else:  # SELL
                if sl is not None and sl <= self.entry_price:
                    raise ValueError(f"SELL sl_price ({sl}) must be strictly greater than entry_price ({self.entry_price})")
                if tp is not None and tp >= self.entry_price:
                    raise ValueError(f"SELL tp_price ({tp}) must be strictly less than entry_price ({self.entry_price})")

        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class KernelTransition:
    """
    Result of a state transition in ExecutionKernel.
    Deeply immutable: closed_trade and each marker are frozen MappingProxyType.
    """
    status: Literal[
        "NO_ACTION",
        "OPENED",
        "SAME_DIRECTION",
        "CLOSED",
        "REVERSED",
        "STOPPED",
        "TARGETED",
        "FORCED_CLOSED",
    ]
    position_before: PositionState | None
    position_after: PositionState | None
    closed_trade: Mapping[str, Any] | None
    markers: tuple[Mapping[str, Any], ...]
    realized_net_pnl: float

    def __post_init__(self) -> None:
        VALID_STATUSES = {
            "NO_ACTION",
            "OPENED",
            "SAME_DIRECTION",
            "CLOSED",
            "REVERSED",
            "STOPPED",
            "TARGETED",
            "FORCED_CLOSED",
        }
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {self.status!r}, must be one of {sorted(VALID_STATUSES)}")

        object.__setattr__(
            self,
            "realized_net_pnl",
            _validate_float(self.realized_net_pnl, "realized_net_pnl")
        )

        CLOSING_STATUSES = {"CLOSED", "REVERSED", "STOPPED", "TARGETED", "FORCED_CLOSED"}
        if self.status in CLOSING_STATUSES:
            if self.closed_trade is None:
                raise ValueError(f"Status '{self.status}' requires a closed_trade, got None")
            if not isinstance(self.closed_trade, collections.abc.Mapping):
                raise TypeError(f"closed_trade must be a Mapping, got {type(self.closed_trade).__name__}")
            object.__setattr__(self, "closed_trade", _deep_freeze(self.closed_trade))
        else:
            if self.closed_trade is not None:
                raise ValueError(f"Status '{self.status}' must not have closed_trade, got {self.closed_trade}")
            if self.realized_net_pnl != 0.0:
                raise ValueError(f"Status '{self.status}' must have realized_net_pnl == 0.0, got {self.realized_net_pnl}")

        # Invariant checks between status and position_before / position_after
        if self.status == "NO_ACTION":
            if self.position_before != self.position_after:
                raise ValueError(f"NO_ACTION requires position_before == position_after, got {self.position_before} vs {self.position_after}")
        elif self.status == "OPENED":
            if self.position_before is not None or self.position_after is None:
                raise ValueError(f"OPENED requires position_before=None and position_after!=None, got {self.position_before}, {self.position_after}")
        elif self.status == "SAME_DIRECTION":
            if self.position_before is None or self.position_after != self.position_before:
                raise ValueError(f"SAME_DIRECTION requires active position unchanged, got {self.position_before} vs {self.position_after}")
        elif self.status in ("CLOSED", "STOPPED", "TARGETED", "FORCED_CLOSED"):
            if self.position_before is None or self.position_after is not None:
                raise ValueError(f"{self.status} requires position_before!=None and position_after=None, got {self.position_before}, {self.position_after}")
        elif self.status == "REVERSED":
            if self.position_before is None or self.position_after is None:
                raise ValueError(f"REVERSED requires position_before!=None and position_after!=None, got {self.position_before}, {self.position_after}")
            if self.position_before.direction == self.position_after.direction:
                raise ValueError("REVERSED requires opposite directions")

        # Freeze markers
        if not isinstance(self.markers, (tuple, list)):
            raise TypeError(f"markers must be a tuple or list, got {type(self.markers).__name__}")
        for m in self.markers:
            if not isinstance(m, collections.abc.Mapping):
                raise TypeError(f"Each marker must be a Mapping, got {type(m).__name__}")
        object.__setattr__(self, "markers", tuple(_deep_freeze(m) for m in self.markers))


class ExecutionKernel:
    """
    Stateful execution engine tracking balance, single position, closed trades,
    and chart markers with strict atomic transitions.
    Internal trades and markers are stored as frozen mappings.
    The trades and markers properties return defensive deep thawed copies.
    """
    def __init__(
        self,
        initial_capital: float = 10000.0,
        lot_size: float = 0.1,
        contract_size: float = 100.0,
        spread_val: float = 0.20,
        commission_per_side: float = 0.50,
        validation_mode: Literal["legacy", "wave1"] = "wave1",
    ) -> None:
        self.validation_mode = validation_mode
        if validation_mode not in ("legacy", "wave1"):
            raise ValueError(f"validation_mode must be 'legacy' or 'wave1', got {validation_mode!r}")

        if validation_mode == "legacy":
            self.initial_capital = _validate_float(initial_capital, "initial_capital")
            self.lot_size = _validate_float(lot_size, "lot_size")
            self.contract_size = _validate_float(contract_size, "contract_size")
            self.spread_val = _validate_float(spread_val, "spread_val")
            self.commission_per_side = _validate_float(commission_per_side, "commission_per_side")
            self.multiplier = self.lot_size * self.contract_size
            self.round_trip_commission = self.commission_per_side * 2.0
        else:
            self.initial_capital = _validate_float(initial_capital, "initial_capital", strictly_positive=True)
            self.lot_size = _validate_float(lot_size, "lot_size", strictly_positive=True)
            self.contract_size = _validate_float(contract_size, "contract_size", strictly_positive=True)
            self.spread_val = _validate_float(spread_val, "spread_val", min_val=0.0)
            self.commission_per_side = _validate_float(commission_per_side, "commission_per_side", min_val=0.0)
            self.multiplier = _require_finite_result(self.lot_size * self.contract_size, "multiplier")
            self.round_trip_commission = _require_finite_result(self.commission_per_side * 2.0, "round_trip_commission")

        self.balance = self.initial_capital
        self._position: PositionState | None = None
        self._trades: list[MappingProxyType[str, Any]] = []
        self._markers: list[MappingProxyType[str, Any]] = []
        self._trade_counter = 0

    @property
    def position(self) -> PositionState | None:
        """Get the current active position (immutable PositionState)."""
        return self._position

    @property
    def trades(self) -> list[dict[str, Any]]:
        """
        Return independent defensive copies of completed trade records.
        Serialization of arbitrary time_value objects is the caller/API adapter's responsibility.
        """
        return [_deep_thaw(t) for t in self._trades]

    @property
    def markers(self) -> list[dict[str, Any]]:
        """
        Return independent defensive copies of chart markers.
        Serialization of arbitrary time_value objects is the caller/API adapter's responsibility.
        """
        return [_deep_thaw(m) for m in self._markers]

    def reset(self) -> None:
        """Reset kernel to its initial clean state."""
        self.balance = self.initial_capital
        self._position = None
        self._trades.clear()
        self._markers.clear()
        self._trade_counter = 0

    def _close_position(
        self,
        pos: PositionState,
        exit_price: float,
        exit_time_value: Any,
        exit_timestamp: int,
        exit_reason: str,
        marker_pos: str,
    ) -> tuple[MappingProxyType[str, Any], MappingProxyType[str, Any], float]:
        """
        Single canonical helper to close a position and compute PnL,
        record frozen trade dictionary and exit chart marker.
        Uses prepare-then-commit: zero state mutation before all
        arithmetic validation, thawing, building, and freezing steps succeed.
        """
        # 1. Local calculation of exit, gross, net
        _require_finite_result(exit_price, "exit_price")
        if pos.direction == "BUY":
            gross_pnl = (exit_price - pos.entry_price) * pos.multiplier
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.multiplier
        net_pnl = gross_pnl - pos.round_trip_commission

        # 2. Validate all are finite
        _require_finite_result(gross_pnl, "gross_pnl")
        _require_finite_result(net_pnl, "net_pnl")

        # 3. Compute prospective_balance
        prospective_balance = self.balance + net_pnl

        # 4. Validate prospective_balance is finite
        _require_finite_result(prospective_balance, "prospective_balance")

        # 5. Compute return_pct
        return_pct = round((net_pnl / self.initial_capital) * 100, 2) if self.initial_capital != 0 else 0.0

        # 6. Validate return_pct is finite
        _require_finite_result(return_pct, "return_pct")

        # Prospective trade ID and thawed time values
        prospective_trade_id = self._trade_counter + 1
        entry_time_exported = _deep_thaw(pos.entry_time_value) if isinstance(pos.entry_time_value, collections.abc.Mapping) else pos.entry_time_value
        exit_time_exported = _deep_thaw(exit_time_value) if isinstance(exit_time_value, collections.abc.Mapping) else exit_time_value

        # 7. Build and freeze trade record
        trade_record = {
            "trade_id": prospective_trade_id,
            "type": pos.direction,
            "entry_time": entry_time_exported,
            "entry_timestamp": pos.entry_timestamp,
            "entry_price": round(pos.entry_price, 3),
            "exit_time": exit_time_exported,
            "exit_timestamp": exit_timestamp,
            "exit_price": round(exit_price, 3),
            "pnl": round(net_pnl, 2),
            "return_pct": return_pct,
            "exit_reason": exit_reason,
        }
        if pos.metadata:
            for k, v in pos.metadata.items():
                if k not in trade_record:
                    trade_record[k] = _deep_thaw(v)
        frozen_trade = _deep_freeze(trade_record)

        # 8. Build and freeze marker
        exit_marker = {
            "time": exit_timestamp,
            "position": marker_pos,
            "color": "#089981" if net_pnl >= 0 else "#f23645",
            "shape": "circle",
            "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}",
        }
        frozen_marker = _deep_freeze(exit_marker)

        # 9. COMMIT POINT: All preparation and freezing succeeded; mutate kernel state
        self.balance = prospective_balance
        self._trade_counter = prospective_trade_id
        self._trades.append(frozen_trade)
        self._markers.append(frozen_marker)

        return frozen_trade, frozen_marker, net_pnl

    def _build_entry_marker(self, direction: str, timestamp: int, entry_price: float) -> MappingProxyType[str, Any]:
        """Build and freeze entry chart marker without mutating kernel state."""
        if direction == "BUY":
            marker = {
                "time": timestamp,
                "position": "belowBar",
                "color": "#2962ff",
                "shape": "arrowUp",
                "text": f"BUY @ {entry_price:.2f}",
            }
        else:
            marker = {
                "time": timestamp,
                "position": "aboveBar",
                "color": "#e91e63",
                "shape": "arrowDown",
                "text": f"SELL @ {entry_price:.2f}",
            }
        return _deep_freeze(marker)

    def _create_entry_marker(self, direction: str, timestamp: int, entry_price: float) -> MappingProxyType[str, Any]:
        """Create and commit entry chart marker."""
        frozen_marker = self._build_entry_marker(direction, timestamp, entry_price)
        self._markers.append(frozen_marker)
        return frozen_marker

    def process_open(self, bar: ExecutionBar, instruction: OpenInstruction | None) -> KernelTransition:
        """
        Process an order instruction at the Open of the current bar.
        Atomic: validates instruction fully before any mutation.
        """
        if instruction is None:
            return KernelTransition(
                status="NO_ACTION",
                position_before=self._position,
                position_after=self._position,
                closed_trade=None,
                markers=(),
                realized_net_pnl=0.0,
            )

        pos_before = self._position

        # 1. Flat state
        if pos_before is None:
            if instruction.action == "CLOSE_ONLY":
                return KernelTransition(
                    status="NO_ACTION",
                    position_before=None,
                    position_after=None,
                    closed_trade=None,
                    markers=(),
                    realized_net_pnl=0.0,
                )
            # OPEN_OR_REVERSE
            assert instruction.entry_price is not None
            new_pos = PositionState(
                direction=instruction.direction,
                entry_price=instruction.entry_price,
                entry_timestamp=bar.timestamp,
                entry_time_value=bar.time_value,
                sl_price=instruction.sl_price,
                tp_price=instruction.tp_price,
                multiplier=self.multiplier,
                round_trip_commission=self.round_trip_commission,
                source=instruction.source,
                metadata=instruction.metadata,
            )
            entry_marker = self._build_entry_marker(new_pos.direction, bar.timestamp, new_pos.entry_price)
            self._position = new_pos
            self._markers.append(entry_marker)
            return KernelTransition(
                status="OPENED",
                position_before=None,
                position_after=new_pos,
                closed_trade=None,
                markers=(entry_marker,),
                realized_net_pnl=0.0,
            )

        # 2. Position already active
        # 2a. Same direction -> no pyramiding
        if pos_before.direction == instruction.direction:
            return KernelTransition(
                status="SAME_DIRECTION",
                position_before=pos_before,
                position_after=pos_before,
                closed_trade=None,
                markers=(),
                realized_net_pnl=0.0,
            )

        # 2b. Opposite direction
        if instruction.action == "CLOSE_ONLY":
            # Close existing position
            if pos_before.direction == "BUY":
                exit_price = bar.open
            else:
                exit_price = bar.open + self.spread_val
                _require_finite_result(exit_price, "exit_price")
            marker_pos = "aboveBar" if pos_before.direction == "BUY" else "belowBar"
            trade_rec, exit_marker, net_pnl = self._close_position(
                pos_before,
                exit_price=exit_price,
                exit_time_value=bar.time_value,
                exit_timestamp=bar.timestamp,
                exit_reason="Signal Reversal",
                marker_pos=marker_pos,
            )
            self._position = None
            return KernelTransition(
                status="CLOSED",
                position_before=pos_before,
                position_after=None,
                closed_trade=trade_rec,
                markers=(exit_marker,),
                realized_net_pnl=net_pnl,
            )

        # 2c. Atomic Reversal (OPEN_OR_REVERSE with opposite direction)
        assert instruction.entry_price is not None
        # Pre-construct new position to validate geometry/types before touching old position
        new_pos = PositionState(
            direction=instruction.direction,
            entry_price=instruction.entry_price,
            entry_timestamp=bar.timestamp,
            entry_time_value=bar.time_value,
            sl_price=instruction.sl_price,
            tp_price=instruction.tp_price,
            multiplier=self.multiplier,
            round_trip_commission=self.round_trip_commission,
            source=instruction.source,
            metadata=instruction.metadata,
        )
        entry_marker = self._build_entry_marker(new_pos.direction, bar.timestamp, new_pos.entry_price)

        # Close old position
        if pos_before.direction == "BUY":
            exit_price = bar.open
        else:
            exit_price = bar.open + self.spread_val
            _require_finite_result(exit_price, "exit_price")
        marker_pos = "aboveBar" if pos_before.direction == "BUY" else "belowBar"
        trade_rec, exit_marker, net_pnl = self._close_position(
            pos_before,
            exit_price=exit_price,
            exit_time_value=bar.time_value,
            exit_timestamp=bar.timestamp,
            exit_reason="Signal Reversal",
            marker_pos=marker_pos,
        )

        # Open new position
        self._position = new_pos
        self._markers.append(entry_marker)

        return KernelTransition(
            status="REVERSED",
            position_before=pos_before,
            position_after=new_pos,
            closed_trade=trade_rec,
            markers=(exit_marker, entry_marker),
            realized_net_pnl=net_pnl,
        )

    def process_intrabar(self, bar: ExecutionBar) -> KernelTransition:
        """
        Check dynamic SL and TP triggers intrabar.
        - Checks position's own sl_price/tp_price, completely independent of global configs.
        - Conservative SL-first collision: if both touched, SL triggers.
        - Short exits trigger on Ask (Bid + spread).
        """
        pos = self._position
        if pos is None:
            return KernelTransition(
                status="NO_ACTION",
                position_before=None,
                position_after=None,
                closed_trade=None,
                markers=(),
                realized_net_pnl=0.0,
            )

        closed = False
        exit_price = 0.0
        exit_reason = ""
        marker_pos = "aboveBar"
        status: Literal["STOPPED", "TARGETED"] = "STOPPED"

        if pos.direction == "BUY":
            # Check SL first
            if pos.sl_price is not None and bar.low <= pos.sl_price:
                exit_price = pos.sl_price
                exit_reason = "Stop Loss"
                marker_pos = "aboveBar"
                status = "STOPPED"
                closed = True
            elif pos.tp_price is not None and bar.high >= pos.tp_price:
                exit_price = pos.tp_price
                exit_reason = "Take Profit"
                marker_pos = "aboveBar"
                status = "TARGETED"
                closed = True
        else:  # SELL (Ask = Bid + spread)
            ask_high = bar.high + self.spread_val
            _require_finite_result(ask_high, "ask_high")

            ask_low = bar.low + self.spread_val
            _require_finite_result(ask_low, "ask_low")

            # Check SL first
            if pos.sl_price is not None and ask_high >= pos.sl_price:
                exit_price = pos.sl_price
                exit_reason = "Stop Loss"
                marker_pos = "belowBar"
                status = "STOPPED"
                closed = True
            elif pos.tp_price is not None and ask_low <= pos.tp_price:
                exit_price = pos.tp_price
                exit_reason = "Take Profit"
                marker_pos = "belowBar"
                status = "TARGETED"
                closed = True

        if not closed:
            return KernelTransition(
                status="NO_ACTION",
                position_before=pos,
                position_after=pos,
                closed_trade=None,
                markers=(),
                realized_net_pnl=0.0,
            )

        trade_rec, exit_marker, net_pnl = self._close_position(
            pos,
            exit_price=exit_price,
            exit_time_value=bar.time_value,
            exit_timestamp=bar.timestamp,
            exit_reason=exit_reason,
            marker_pos=marker_pos,
        )
        self._position = None

        return KernelTransition(
            status=status,
            position_before=pos,
            position_after=None,
            closed_trade=trade_rec,
            markers=(exit_marker,),
            realized_net_pnl=net_pnl,
        )

    def mark_to_market(self, close_bid: float) -> float:
        """
        Compute current floating equity based on bar Close Bid.
        BUY floating uses Close Bid; SELL floating uses Close Ask (Close Bid + spread).
        Deducts full round-trip commission to maintain exact equity consistency.
        Fails fast if arithmetic overflows, never mutating state or returning NaN/Inf.
        """
        close_bid = _validate_float(close_bid, "close_bid")
        pos = self._position
        if pos is None:
            return self.balance

        if pos.direction == "BUY":
            floating_gross = (close_bid - pos.entry_price) * pos.multiplier
        else:
            close_ask = close_bid + self.spread_val
            _require_finite_result(close_ask, "close_ask")
            floating_gross = (pos.entry_price - close_ask) * pos.multiplier

        _require_finite_result(floating_gross, "floating_gross")
        floating_pnl = floating_gross - pos.round_trip_commission
        _require_finite_result(floating_pnl, "floating_pnl")
        equity = self.balance + floating_pnl
        _require_finite_result(equity, "equity")
        return equity

    def force_close(self, bar: ExecutionBar) -> KernelTransition:
        """
        Forced close of any open position at the final backtest candle.
        Idempotent: if already flat, returns NO_ACTION without modifying state.
        """
        pos = self._position
        if pos is None:
            return KernelTransition(
                status="NO_ACTION",
                position_before=None,
                position_after=None,
                closed_trade=None,
                markers=(),
                realized_net_pnl=0.0,
            )

        if pos.direction == "BUY":
            exit_price = bar.close
            marker_pos = "aboveBar"
        else:
            exit_price = bar.close + self.spread_val
            _require_finite_result(exit_price, "exit_price")
            marker_pos = "belowBar"

        trade_rec, exit_marker, net_pnl = self._close_position(
            pos,
            exit_price=exit_price,
            exit_time_value=bar.time_value,
            exit_timestamp=bar.timestamp,
            exit_reason="End of Backtest",
            marker_pos=marker_pos,
        )
        self._position = None

        return KernelTransition(
            status="FORCED_CLOSED",
            position_before=pos,
            position_after=None,
            closed_trade=trade_rec,
            markers=(exit_marker,),
            realized_net_pnl=net_pnl,
        )
