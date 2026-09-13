"""
smc/engine/execution.py
-----------------------
Strict, immutable execution models, fill validation, cash-basis RR calculation,
event serialization, and deterministic cooldown tracking for T53.9.

Semantics strictly adhere to ADR 25:
- Analysis at Close N -> Execution at Open N+1 (market-at-next-open).
- Dynamic SL/TP levels, spread adjustments, and round-trip commission accounting.
- Pure fill gate: validates geometry and cash-basis RR before position entry or reversal.
- Cooldown: begins strictly after successful fill, keyed by (primary_strategy_id, direction).
- Fail-closed validation for timestamps, composite IDs, cross-field invariants, and cooldown books.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Optional, Sequence, Union

import numpy as np
import pandas as pd

from smc.engine.models import (
    CandidateSetup,
    SelectionDecision,
    StrictModelTypeError,
    TradeDirection,
    _BASE_TOKEN_RE,
    _freeze,
    _unfreeze,
    _validate_base_token,
    _validate_bool,
    _validate_cluster_id,
    _validate_finite_float,
    _validate_int,
    _validate_non_negative_int,
    _validate_str,
    make_decision_id,
)

ExecutionEventType = Literal[
    "ORDER_PENDING",
    "ORDER_SELECTED",
    "ORDER_FILLED",
    "ORDER_REJECTED",
    "ORDER_SKIPPED",
    "ORDER_CANCELLED",
    "POSITION_CLOSED",
]

VALID_EXECUTION_EVENT_TYPES: frozenset[str] = frozenset({
    "ORDER_PENDING",
    "ORDER_SELECTED",
    "ORDER_FILLED",
    "ORDER_REJECTED",
    "ORDER_SKIPPED",
    "ORDER_CANCELLED",
    "POSITION_CLOSED",
})

VALID_FILL_GATE_REASONS: frozenset[str] = frozenset({
    "fill_ok",
    "geometry_violation_at_fill",
    "insufficient_rr_at_fill",
    "short_disabled",
})

EVENT_TYPE_ALLOWED_REASONS: dict[str, frozenset[str]] = {
    "ORDER_PENDING": frozenset({"pending"}),
    "ORDER_SELECTED": frozenset({"pending", "selected", "ok"}),
    "ORDER_FILLED": frozenset({"fill_ok"}),
    "ORDER_REJECTED": frozenset({"geometry_violation_at_fill", "insufficient_rr_at_fill"}),
    "ORDER_SKIPPED": frozenset({"short_disabled", "position_already_open_same_direction"}),
    "ORDER_CANCELLED": frozenset({"no_next_bar"}),
    "POSITION_CLOSED": frozenset({"stop_loss", "take_profit", "opposite_signal", "forced_close"}),
}


def _validate_execution_timestamp(val: Any, name: str) -> pd.Timestamp:
    """
    Strict fail-closed timestamp validator.
    Accepts only timezone-aware pd.Timestamp, timezone-aware datetime.datetime,
    or ISO string with an explicit UTC offset.
    Rejects None, boolean, numeric (int/float), date-only objects, and naive timestamps.
    """
    if val is None:
        raise StrictModelTypeError(f"Field '{name}' cannot be None.")
    if isinstance(val, (bool, np.bool_)):
        raise StrictModelTypeError(f"Field '{name}' cannot be a boolean.")
    if isinstance(val, (int, float, np.integer, np.floating)):
        raise StrictModelTypeError(f"Field '{name}' cannot be numeric: {val}")
    if isinstance(val, datetime.date) and not isinstance(val, datetime.datetime):
        raise StrictModelTypeError(f"Field '{name}' cannot be a date-only object: {val}")

    if isinstance(val, pd.Timestamp):
        if val.tzinfo is None and getattr(val, "tz", None) is None:
            raise ValueError(f"Field '{name}' must be timezone-aware, got naive pd.Timestamp: '{val}'")
        return val.tz_convert("UTC")

    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            raise ValueError(f"Field '{name}' must be timezone-aware, got naive datetime: '{val}'")
        return pd.Timestamp(val).tz_convert("UTC")

    if isinstance(val, str):
        if not val.strip():
            raise ValueError(f"Field '{name}' cannot be an empty string.")
        try:
            ts = pd.Timestamp(val)
        except Exception as e:
            raise ValueError(f"Cannot parse '{val}' to pd.Timestamp for field '{name}': {e}")
        if ts.tzinfo is None and getattr(ts, "tz", None) is None:
            raise ValueError(f"Field '{name}' must have explicit UTC offset in string, got naive: '{val}'")
        return ts.tz_convert("UTC")

    raise StrictModelTypeError(f"Field '{name}' must be timezone-aware Timestamp/datetime or ISO string, got {type(val).__name__}")


def _validate_setup_id(
    setup_id: Any,
    expected_strategy_id: str,
    expected_direction: str,
    signal_bar_index: int,
    expected_cluster_id: str,
    name: str = "primary_setup_id",
) -> str:
    """
    Deterministic fail-closed parser and validator for composite setup ID.
    Grammar contract: '{strategy_id}:{direction}:{setup_bar_index}:{cluster_id}'.
    Enforces that embedded components match caller expectations and setup_bar_index <= signal_bar_index.
    """
    if not isinstance(setup_id, str):
        raise StrictModelTypeError(f"Field '{name}' must be a string, got {type(setup_id).__name__}")
    if not setup_id:
        raise ValueError(f"Field '{name}' cannot be empty.")
    if setup_id != setup_id.strip():
        raise ValueError(f"Field '{name}' cannot contain leading or trailing whitespace: '{setup_id}'")

    parts = setup_id.split(":", 3)
    if len(parts) != 4:
        raise ValueError(
            f"Field '{name}' must contain exactly 4 components delimited by ':' "
            f"('{expected_strategy_id}:{expected_direction}:setup_bar:{expected_cluster_id}'), "
            f"got '{setup_id}'"
        )

    strat_id, direct, bar_str, cluster_part = parts[0], parts[1], parts[2], parts[3]

    _validate_base_token(strat_id, f"{name} strategy_id")
    if strat_id != expected_strategy_id:
        raise ValueError(
            f"Setup ID strategy '{strat_id}' in '{name}' does not match primary_strategy_id '{expected_strategy_id}'"
        )

    if direct not in {"BUY", "SELL"}:
        raise ValueError(f"Setup ID direction '{direct}' in '{name}' must be 'BUY' or 'SELL'")
    if direct != expected_direction:
        raise ValueError(
            f"Setup ID direction '{direct}' in '{name}' does not match direction '{expected_direction}'"
        )

    if not bar_str.isdigit():
        raise ValueError(f"Setup ID bar_index '{bar_str}' in '{name}' must be a non-negative integer digits string")
    setup_bar_idx = int(bar_str)
    if setup_bar_idx < 0:
        raise ValueError(f"Setup ID bar_index in '{name}' cannot be negative, got {setup_bar_idx}")
    if setup_bar_idx > signal_bar_index:
        raise ValueError(
            f"Setup ID bar_index {setup_bar_idx} in '{name}' cannot be greater than signal_bar_index {signal_bar_index}"
        )

    _validate_cluster_id(cluster_part, f"{name} cluster_id")
    if cluster_part != expected_cluster_id:
        raise ValueError(
            f"Setup ID cluster suffix '{cluster_part}' in '{name}' does not match cluster_id '{expected_cluster_id}'"
        )

    return setup_id


def make_execution_event_id(bar_index: int, event_type: str, strategy_id: str, setup_id: str) -> str:
    """
    Stable injective execution event ID: evt:{bar_index}:{event_type}:{strategy_id}:{setup_id}.
    All components are strictly validated; no fallback 'none' is permitted.
    """
    _validate_non_negative_int(bar_index, "bar_index")
    if event_type not in VALID_EXECUTION_EVENT_TYPES:
        raise ValueError(f"Invalid event_type: '{event_type}'. Must be one of {sorted(VALID_EXECUTION_EVENT_TYPES)}")
    _validate_base_token(strategy_id, "strategy_id")
    _validate_str(setup_id, "setup_id")
    return f"evt:{bar_index}:{event_type}:{strategy_id}:{setup_id}"


def make_execution_intent_id(signal_bar_index: int, decision_id: str) -> str:
    """Stable injective execution intent ID: intent:{signal_bar_index}:{decision_id}."""
    _validate_non_negative_int(signal_bar_index, "signal_bar_index")
    if not isinstance(decision_id, str) or not decision_id:
        raise ValueError("decision_id must be a non-empty string.")
    return f"intent:{signal_bar_index}:{decision_id}"


# =============================================================================
# ExecutionConfig
# =============================================================================

@dataclass(frozen=True)
class ExecutionConfig:
    """
    Execution configuration holding trading cost parameters and execution rules.
    Strictly validates all inputs as finite positive numbers or booleans (no type coercion).
    Provides full-precision computed properties without premature rounding.
    """
    point_value: float = 0.01          # 1 point = 0.01 USD for XAUUSD (100 points = 1.00 USD)
    lot_size: float = 0.1             # Standard lot fraction
    contract_size: float = 100.0       # Ounces per lot
    spread_points: float = 20.0        # 20 points = 0.20 USD
    commission_per_lot: float = 5.0    # 5 USD per round-turn lot (2.50 USD per side)
    min_rr_fallback: float = 1.5       # Default minimum RR if strategy profile does not specify
    allow_short: bool = True           # Master short permission

    def __post_init__(self) -> None:
        object.__setattr__(self, "point_value", _validate_finite_float(self.point_value, "point_value", min_val=1e-9))
        object.__setattr__(self, "lot_size", _validate_finite_float(self.lot_size, "lot_size", min_val=1e-9))
        object.__setattr__(self, "contract_size", _validate_finite_float(self.contract_size, "contract_size", min_val=1e-9))
        object.__setattr__(self, "spread_points", _validate_finite_float(self.spread_points, "spread_points", min_val=0.0))
        object.__setattr__(self, "commission_per_lot", _validate_finite_float(self.commission_per_lot, "commission_per_lot", min_val=0.0))
        object.__setattr__(self, "min_rr_fallback", _validate_finite_float(self.min_rr_fallback, "min_rr_fallback", min_val=1e-9))
        object.__setattr__(self, "allow_short", _validate_bool(self.allow_short, "allow_short"))

    @property
    def spread(self) -> float:
        """Effective spread in price currency: spread_points * point_value (full precision)."""
        return self.spread_points * self.point_value

    @property
    def commission_per_side(self) -> float:
        """Commission for 1 side of the trade: commission_per_lot * lot_size (full precision)."""
        return self.commission_per_lot * self.lot_size

    @property
    def round_trip_commission(self) -> float:
        """Total round-trip commission for entry + exit: 2 * commission_per_side (full precision)."""
        return 2.0 * self.commission_per_side

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_value": float(self.point_value),
            "lot_size": float(self.lot_size),
            "contract_size": float(self.contract_size),
            "spread_points": float(self.spread_points),
            "commission_per_lot": float(self.commission_per_lot),
            "min_rr_fallback": float(self.min_rr_fallback),
            "allow_short": bool(self.allow_short),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionConfig:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for ExecutionConfig, got {type(data).__name__}")
        REQUIRED_KEYS = frozenset({
            "point_value", "lot_size", "contract_size",
            "spread_points", "commission_per_lot",
            "min_rr_fallback", "allow_short",
        })
        unknown = set(data.keys()) - REQUIRED_KEYS
        if unknown:
            raise KeyError(f"Unknown fields in ExecutionConfig: {sorted(unknown)}")
        missing = [k for k in sorted(REQUIRED_KEYS) if k not in data]
        if missing:
            raise KeyError(f"Missing required field(s) in ExecutionConfig: {missing}")

        return cls(
            point_value=data["point_value"],
            lot_size=data["lot_size"],
            contract_size=data["contract_size"],
            spread_points=data["spread_points"],
            commission_per_lot=data["commission_per_lot"],
            min_rr_fallback=data["min_rr_fallback"],
            allow_short=data["allow_short"],
        )


# =============================================================================
# PendingExecutionIntent
# =============================================================================

@dataclass(frozen=True)
class PendingExecutionIntent:
    """
    Immutable intent generated at Close bar N when SelectionDecision is SELECT.
    Represents the pending candidate to be evaluated for execution at Open bar N+1.
    All IDs, direction, timestamps, and supporting strategies are strictly validated.
    """
    intent_id: str
    decision_id: str
    symbol: str
    timeframe: str
    signal_bar_index: int
    signal_bar_time: pd.Timestamp
    action: str
    primary_strategy_id: str
    primary_setup_id: str
    direction: str
    planned_entry: float
    signal_sl: float
    signal_tp: float
    planned_rr: float
    total_score: float
    min_rr: float
    cluster_id: str
    supporting_strategy_ids: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_str(self.intent_id, "intent_id")
        _validate_str(self.decision_id, "decision_id")
        _validate_base_token(self.symbol, "symbol")
        _validate_base_token(self.timeframe, "timeframe")
        b_idx = _validate_non_negative_int(self.signal_bar_index, "signal_bar_index")
        object.__setattr__(self, "signal_bar_index", b_idx)

        # Fail-closed timestamp validation
        ts = _validate_execution_timestamp(self.signal_bar_time, "signal_bar_time")
        object.__setattr__(self, "signal_bar_time", ts)

        if self.action != "SELECT":
            raise ValueError(f"PendingExecutionIntent action must be 'SELECT', got '{self.action}'")

        strat_id = _validate_base_token(self.primary_strategy_id, "primary_strategy_id")
        object.__setattr__(self, "primary_strategy_id", strat_id)

        # Strict canonical decision_id check (P1 #5)
        expected_decision_id = make_decision_id(b_idx, self.action, strat_id)
        if self.decision_id != expected_decision_id:
            raise ValueError(
                f"decision_id '{self.decision_id}' does not match expected canonical '{expected_decision_id}'"
            )

        # Strict canonical intent_id check (P1 #5)
        expected_intent_id = make_execution_intent_id(b_idx, self.decision_id)
        if self.intent_id != expected_intent_id:
            raise ValueError(
                f"intent_id '{self.intent_id}' does not match expected canonical '{expected_intent_id}'"
            )

        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got '{self.direction}'")

        # Cluster ID must satisfy _validate_cluster_id grammar
        c_id = _validate_cluster_id(self.cluster_id, "cluster_id")
        object.__setattr__(self, "cluster_id", c_id)

        # Setup ID must parse and match primary_strategy_id, direction, setup_bar_index <= signal_bar_index, and cluster_id
        valid_setup_id = _validate_setup_id(
            setup_id=self.primary_setup_id,
            expected_strategy_id=strat_id,
            expected_direction=self.direction,
            signal_bar_index=b_idx,
            expected_cluster_id=c_id,
            name="primary_setup_id",
        )
        object.__setattr__(self, "primary_setup_id", valid_setup_id)

        # Validate planned levels and enforce planned geometry (P2 #1)
        pe = _validate_finite_float(self.planned_entry, "planned_entry", min_val=1e-9)
        sl = _validate_finite_float(self.signal_sl, "signal_sl", min_val=1e-9)
        tp = _validate_finite_float(self.signal_tp, "signal_tp", min_val=1e-9)

        if self.direction == "BUY":
            if not (sl < pe < tp):
                raise ValueError(
                    f"Invalid BUY planned geometry: signal_sl ({sl}) < planned_entry ({pe}) < signal_tp ({tp}) is required"
                )
        elif self.direction == "SELL":
            if not (tp < pe < sl):
                raise ValueError(
                    f"Invalid SELL planned geometry: signal_tp ({tp}) < planned_entry ({pe}) < signal_sl ({sl}) is required"
                )

        object.__setattr__(self, "planned_entry", pe)
        object.__setattr__(self, "signal_sl", sl)
        object.__setattr__(self, "signal_tp", tp)
        object.__setattr__(self, "planned_rr", _validate_finite_float(self.planned_rr, "planned_rr", min_val=1e-9))
        object.__setattr__(self, "total_score", _validate_finite_float(self.total_score, "total_score", min_val=0.0, max_val=100.0))
        object.__setattr__(self, "min_rr", _validate_finite_float(self.min_rr, "min_rr", min_val=1e-9))

        # Strict validation for supporting_strategy_ids
        if not isinstance(self.supporting_strategy_ids, tuple):
            raise StrictModelTypeError(
                f"supporting_strategy_ids must be a tuple, got {type(self.supporting_strategy_ids).__name__}"
            )

        seen_supporting = set()
        for sid in self.supporting_strategy_ids:
            _validate_base_token(sid, "supporting_strategy_id")
            if sid == strat_id:
                raise ValueError(
                    f"supporting_strategy_ids cannot contain primary_strategy_id '{strat_id}'"
                )
            if sid in seen_supporting:
                raise ValueError(f"Duplicate supporting_strategy_id '{sid}' in supporting_strategy_ids")
            seen_supporting.add(sid)

        # Enforce canonical sorted order
        if self.supporting_strategy_ids != tuple(sorted(self.supporting_strategy_ids)):
            raise ValueError(
                f"supporting_strategy_ids must be canonically sorted: got {self.supporting_strategy_ids}, "
                f"expected {tuple(sorted(self.supporting_strategy_ids))}"
            )

        object.__setattr__(self, "meta", _freeze(self.meta))

    @property
    def signal_bar(self) -> int:
        return self.signal_bar_index

    @property
    def signal_time(self) -> pd.Timestamp:
        return self.signal_bar_time

    @property
    def fill_bar(self) -> int:
        return self.signal_bar_index + 1

    @property
    def fill_time(self) -> Optional[pd.Timestamp]:
        raw = self.meta.get("fill_time")
        if raw is not None:
            if isinstance(raw, pd.Timestamp):
                return raw
            return pd.Timestamp(raw)
        return None

    @property
    def strategy_id(self) -> str:
        return self.primary_strategy_id

    @property
    def setup_id(self) -> str:
        return self.primary_setup_id

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        raw = self.meta.get("evidence_ids", ())
        if isinstance(raw, (list, tuple)):
            return tuple(raw)
        return ()

    @property
    def planned_sl(self) -> float:
        return self.signal_sl

    @property
    def planned_tp(self) -> float:
        return self.signal_tp

    @property
    def expiry(self) -> Optional[int]:
        return self.meta.get("expiry_bar", self.meta.get("expiry"))

    @property
    def regime(self) -> Optional[str]:
        return self.meta.get("regime")

    @property
    def session(self) -> Optional[str]:
        return self.meta.get("session")

    @property
    def selector_score(self) -> float:
        return self.total_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "signal_bar_index": int(self.signal_bar_index),
            "signal_bar_time": self.signal_bar_time.isoformat(),
            "action": self.action,
            "primary_strategy_id": self.primary_strategy_id,
            "primary_setup_id": self.primary_setup_id,
            "direction": self.direction,
            "planned_entry": float(self.planned_entry),
            "signal_sl": float(self.signal_sl),
            "signal_tp": float(self.signal_tp),
            "planned_rr": float(self.planned_rr),
            "total_score": float(self.total_score),
            "min_rr": float(self.min_rr),
            "cluster_id": self.cluster_id,
            "supporting_strategy_ids": list(self.supporting_strategy_ids),
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingExecutionIntent:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for PendingExecutionIntent, got {type(data).__name__}")
        REQUIRED_KEYS = frozenset({
            "intent_id", "decision_id", "symbol", "timeframe",
            "signal_bar_index", "signal_bar_time", "action",
            "primary_strategy_id", "primary_setup_id", "direction",
            "planned_entry", "signal_sl", "signal_tp",
            "planned_rr", "total_score", "min_rr",
            "cluster_id", "supporting_strategy_ids", "meta",
        })
        unknown = set(data.keys()) - REQUIRED_KEYS
        if unknown:
            raise KeyError(f"Unknown fields in PendingExecutionIntent: {sorted(unknown)}")
        missing = [k for k in sorted(REQUIRED_KEYS) if k not in data]
        if missing:
            raise KeyError(f"Missing required field(s) in PendingExecutionIntent: {missing}")

        # Validate supporting_strategy_ids is list in serialized format
        raw_supporting = data["supporting_strategy_ids"]
        if not isinstance(raw_supporting, list):
            raise StrictModelTypeError(
                f"supporting_strategy_ids in serialized dict must be a list, got {type(raw_supporting).__name__}"
            )

        return cls(
            intent_id=data["intent_id"],
            decision_id=data["decision_id"],
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            signal_bar_index=data["signal_bar_index"],
            signal_bar_time=data["signal_bar_time"],  # Pass raw value to constructor for strict validation
            action=data["action"],
            primary_strategy_id=data["primary_strategy_id"],
            primary_setup_id=data["primary_setup_id"],
            direction=data["direction"],
            planned_entry=data["planned_entry"],
            signal_sl=data["signal_sl"],
            signal_tp=data["signal_tp"],
            planned_rr=data["planned_rr"],
            total_score=data["total_score"],
            min_rr=data["min_rr"],
            cluster_id=data["cluster_id"],
            supporting_strategy_ids=tuple(raw_supporting),
            meta=data["meta"],
        )

    @classmethod
    def from_selection_decision(
        cls,
        decision: SelectionDecision,
        symbol: str,
        timeframe: str,
        min_rr: float,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> PendingExecutionIntent:
        """
        Factory to construct a PendingExecutionIntent from a valid SelectionDecision.
        Derives all setup/strategy parameters directly from the decision to prevent mismatches.
        Does NOT mutate decision or candidate setup.
        """
        if not isinstance(decision, SelectionDecision):
            raise StrictModelTypeError(f"decision must be SelectionDecision, got {type(decision).__name__}")
        if decision.action != "SELECT":
            raise ValueError(f"Cannot create PendingExecutionIntent from decision with action='{decision.action}'")
        if decision.selected_setup is None:
            raise ValueError("SelectionDecision with action='SELECT' must have selected_setup.")

        setup: CandidateSetup = decision.selected_setup
        b_idx = decision.bar_index
        decision_id = decision.decision_id
        intent_id = make_execution_intent_id(b_idx, decision_id)

        # Retrieve total_score from evaluation matching selected_setup
        matched_evals = [ev for ev in decision.evaluations if ev.candidate.setup_id == setup.setup_id]
        if matched_evals:
            total_score = float(matched_evals[0].total_score)
        else:
            total_score = float(setup.planned_rr * 10.0)

        # Merge metadata without mutating original decision or setup
        merged_meta: dict[str, Any] = {}
        if decision.meta:
            merged_meta.update(_unfreeze(decision.meta))
        if setup.meta:
            merged_meta.update(_unfreeze(setup.meta))
        if meta:
            merged_meta.update(_unfreeze(meta))

        # Traceability metadata enrichment
        if "evidence_ids" not in merged_meta and hasattr(setup, "evidences") and setup.evidences:
            merged_meta["evidence_ids"] = [e.evidence_id for e in setup.evidences]
        if "expiry_bar" not in merged_meta and hasattr(setup, "expiry_bar") and setup.expiry_bar is not None:
            merged_meta["expiry_bar"] = setup.expiry_bar
        if "regime" not in merged_meta and decision.regime is not None:
            merged_meta["regime"] = decision.regime.regime

        return cls(
            intent_id=intent_id,
            decision_id=decision_id,
            symbol=symbol,
            timeframe=timeframe,
            signal_bar_index=b_idx,
            signal_bar_time=decision.timestamp,
            action="SELECT",
            primary_strategy_id=decision.primary_strategy_id,  # type: ignore[arg-type]
            primary_setup_id=setup.setup_id,
            direction=setup.direction,
            planned_entry=setup.entry_price,
            signal_sl=setup.stop_loss,
            signal_tp=setup.take_profit,
            planned_rr=setup.planned_rr,
            total_score=total_score,
            min_rr=min_rr,
            cluster_id=setup.evidence_cluster_id,
            supporting_strategy_ids=decision.supporting_strategy_ids,
            meta=merged_meta,
        )


# =============================================================================
# FillValidationResult
# =============================================================================

@dataclass(frozen=True)
class FillValidationResult:
    """
    Result of evaluating PendingExecutionIntent at Open bar N+1.
    Enforces strict cross-field invariants:
    - is_valid=True <=> reason='fill_ok'
    - fill_ok requires positive risk_cash, reward_cash, effective_rr, and positive actual prices.
    - geometry_violation_at_fill and short_disabled require is_valid=False and risk_cash=reward_cash=effective_rr=0.0.
    """
    is_valid: bool
    reason: str
    actual_entry: float
    actual_sl: float
    actual_tp: float
    risk_cash: float
    reward_cash: float
    effective_rr: float
    spread: float
    round_trip_commission: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "is_valid", _validate_bool(self.is_valid, "is_valid"))
        if self.reason not in VALID_FILL_GATE_REASONS:
            raise ValueError(
                f"Invalid fill validation reason: '{self.reason}'. "
                f"Must be one of {sorted(VALID_FILL_GATE_REASONS)}"
            )

        # Validate finite numeric values (no bool, no NaN, no Inf)
        object.__setattr__(self, "actual_entry", _validate_finite_float(self.actual_entry, "actual_entry", min_val=0.00001))
        object.__setattr__(self, "actual_sl", _validate_finite_float(self.actual_sl, "actual_sl", min_val=0.00001))
        object.__setattr__(self, "actual_tp", _validate_finite_float(self.actual_tp, "actual_tp", min_val=0.00001))
        object.__setattr__(self, "risk_cash", _validate_finite_float(self.risk_cash, "risk_cash"))
        object.__setattr__(self, "reward_cash", _validate_finite_float(self.reward_cash, "reward_cash"))
        object.__setattr__(self, "effective_rr", _validate_finite_float(self.effective_rr, "effective_rr"))
        object.__setattr__(self, "spread", _validate_finite_float(self.spread, "spread", min_val=0.0))
        object.__setattr__(self, "round_trip_commission", _validate_finite_float(self.round_trip_commission, "round_trip_commission", min_val=0.0))

        # Cross-field invariant checks
        if self.is_valid is True:
            if self.reason != "fill_ok":
                raise ValueError(f"is_valid=True requires reason='fill_ok', got '{self.reason}'")
            if self.risk_cash <= 0.0:
                raise ValueError(f"fill_ok requires risk_cash > 0, got {self.risk_cash}")
            if self.reward_cash <= 0.0:
                raise ValueError(f"fill_ok requires reward_cash > 0, got {self.reward_cash}")
            if self.effective_rr <= 0.0:
                raise ValueError(f"fill_ok requires effective_rr > 0, got {self.effective_rr}")
        else:
            if self.reason == "fill_ok":
                raise ValueError("is_valid=False cannot have reason='fill_ok'")
            if self.reason in ("geometry_violation_at_fill", "short_disabled"):
                if self.risk_cash != 0.0 or self.reward_cash != 0.0 or self.effective_rr != 0.0:
                    raise ValueError(
                        f"Reason '{self.reason}' requires risk_cash=0.0, reward_cash=0.0, effective_rr=0.0; "
                        f"got risk={self.risk_cash}, reward={self.reward_cash}, rr={self.effective_rr}"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": bool(self.is_valid),
            "reason": self.reason,
            "actual_entry": float(self.actual_entry),
            "actual_sl": float(self.actual_sl),
            "actual_tp": float(self.actual_tp),
            "risk_cash": float(self.risk_cash),
            "reward_cash": float(self.reward_cash),
            "effective_rr": float(self.effective_rr),
            "spread": float(self.spread),
            "round_trip_commission": float(self.round_trip_commission),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FillValidationResult:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for FillValidationResult, got {type(data).__name__}")
        REQUIRED_KEYS = frozenset({
            "is_valid", "reason", "actual_entry", "actual_sl", "actual_tp",
            "risk_cash", "reward_cash", "effective_rr",
            "spread", "round_trip_commission",
        })
        unknown = set(data.keys()) - REQUIRED_KEYS
        if unknown:
            raise KeyError(f"Unknown fields in FillValidationResult: {sorted(unknown)}")
        missing = [k for k in sorted(REQUIRED_KEYS) if k not in data]
        if missing:
            raise KeyError(f"Missing required field(s) in FillValidationResult: {missing}")

        return cls(
            is_valid=data["is_valid"],
            reason=data["reason"],
            actual_entry=data["actual_entry"],
            actual_sl=data["actual_sl"],
            actual_tp=data["actual_tp"],
            risk_cash=data["risk_cash"],
            reward_cash=data["reward_cash"],
            effective_rr=data["effective_rr"],
            spread=data["spread"],
            round_trip_commission=data["round_trip_commission"],
        )


# =============================================================================
# Pure Fill Gate: validate_fill()
# =============================================================================

def validate_fill(
    intent: PendingExecutionIntent,
    open_price: float,
    config: ExecutionConfig,
) -> FillValidationResult:
    """
    Pure validation gate for market-at-next-open execution.
    Calculations use full precision floats without premature rounding.
    Threshold comparisons strictly reject any effective_rr < min_rr.
    Exact effective_rr == min_rr passes.
    Does NOT mutate intent or config.
    """
    if not isinstance(intent, PendingExecutionIntent):
        raise StrictModelTypeError(f"intent must be PendingExecutionIntent, got {type(intent).__name__}")
    if not isinstance(config, ExecutionConfig):
        raise StrictModelTypeError(f"config must be ExecutionConfig, got {type(config).__name__}")

    open_p = _validate_finite_float(open_price, "open_price", min_val=1e-9)
    spread = config.spread
    rt_commission = config.round_trip_commission
    multiplier = config.lot_size * config.contract_size

    # Check allow_short master switch
    if intent.direction == "SELL" and not config.allow_short:
        return FillValidationResult(
            is_valid=False,
            reason="short_disabled",
            actual_entry=open_p,
            actual_sl=intent.signal_sl + spread,
            actual_tp=intent.signal_tp + spread,
            risk_cash=0.0,
            reward_cash=0.0,
            effective_rr=0.0,
            spread=spread,
            round_trip_commission=rt_commission,
        )

    # 1. Price level transformation according to ADR 25 Section 3.3 (no premature rounding!)
    if intent.direction == "BUY":
        actual_entry = open_p + spread
        actual_sl = intent.signal_sl
        actual_tp = intent.signal_tp

        # BUY Geometry: actual_sl < actual_entry < actual_tp
        if not (actual_sl < actual_entry < actual_tp):
            return FillValidationResult(
                is_valid=False,
                reason="geometry_violation_at_fill",
                actual_entry=actual_entry,
                actual_sl=actual_sl,
                actual_tp=actual_tp,
                risk_cash=0.0,
                reward_cash=0.0,
                effective_rr=0.0,
                spread=spread,
                round_trip_commission=rt_commission,
            )

        risk_price_dist = actual_entry - actual_sl
        reward_price_dist = actual_tp - actual_entry

    elif intent.direction == "SELL":
        actual_entry = open_p
        actual_sl = intent.signal_sl + spread
        actual_tp = intent.signal_tp + spread

        # SELL Geometry: actual_tp < actual_entry < actual_sl
        if not (actual_tp < actual_entry < actual_sl):
            return FillValidationResult(
                is_valid=False,
                reason="geometry_violation_at_fill",
                actual_entry=actual_entry,
                actual_sl=actual_sl,
                actual_tp=actual_tp,
                risk_cash=0.0,
                reward_cash=0.0,
                effective_rr=0.0,
                spread=spread,
                round_trip_commission=rt_commission,
            )

        risk_price_dist = actual_sl - actual_entry
        reward_price_dist = actual_entry - actual_tp

    else:
        raise ValueError(f"Invalid intent direction: '{intent.direction}'")

    # 2. Cash-basis RR calculation according to ADR 25 Section 3.4 (full precision!)
    gross_risk_cash = risk_price_dist * multiplier
    gross_reward_cash = reward_price_dist * multiplier

    risk_cash = gross_risk_cash + rt_commission
    reward_cash = gross_reward_cash - rt_commission

    # Effective cash RR check
    if risk_cash <= 0.0 or reward_cash <= 0.0:
        return FillValidationResult(
            is_valid=False,
            reason="insufficient_rr_at_fill",
            actual_entry=actual_entry,
            actual_sl=actual_sl,
            actual_tp=actual_tp,
            risk_cash=risk_cash,
            reward_cash=reward_cash,
            effective_rr=0.0,
            spread=spread,
            round_trip_commission=rt_commission,
        )

    effective_rr = reward_cash / risk_cash

    # Strict comparison without large tolerance: any value < min_rr must reject!
    if not math.isfinite(effective_rr) or effective_rr < intent.min_rr:
        return FillValidationResult(
            is_valid=False,
            reason="insufficient_rr_at_fill",
            actual_entry=actual_entry,
            actual_sl=actual_sl,
            actual_tp=actual_tp,
            risk_cash=risk_cash,
            reward_cash=reward_cash,
            effective_rr=effective_rr,
            spread=spread,
            round_trip_commission=rt_commission,
        )

    return FillValidationResult(
        is_valid=True,
        reason="fill_ok",
        actual_entry=actual_entry,
        actual_sl=actual_sl,
        actual_tp=actual_tp,
        risk_cash=risk_cash,
        reward_cash=reward_cash,
        effective_rr=effective_rr,
        spread=spread,
        round_trip_commission=rt_commission,
    )


# =============================================================================
# ExecutionEvent
# =============================================================================

@dataclass(frozen=True)
class ExecutionEvent:
    """
    Immutable execution audit event recorded throughout the trade lifecycle.
    Covers ORDER_PENDING, ORDER_FILLED, ORDER_REJECTED, ORDER_SKIPPED, ORDER_CANCELLED, POSITION_CLOSED.
    Enforces strict event_type to reason mappings, temporal order, and field completeness per event type.
    """
    event_version: str
    event_id: str
    event_type: str
    reason: str
    symbol: str
    timeframe: str
    bar_index: int
    bar_time: pd.Timestamp
    signal_bar_index: int
    signal_bar_time: pd.Timestamp
    decision_id: str
    strategy_id: str
    setup_id: str
    direction: str
    planned_entry: float
    planned_sl: float
    planned_tp: float
    actual_entry: Optional[float] = None
    actual_sl: Optional[float] = None
    actual_tp: Optional[float] = None
    risk_cash: Optional[float] = None
    reward_cash: Optional[float] = None
    effective_rr: Optional[float] = None
    exit_price: Optional[float] = None
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    cluster_id: Optional[str] = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.event_version, str) or not self.event_version:
            raise ValueError("event_version must be a non-empty string.")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be a non-empty string.")
        if self.event_type not in VALID_EXECUTION_EVENT_TYPES:
            raise ValueError(
                f"Invalid event_type: '{self.event_type}'. Must be one of {sorted(VALID_EXECUTION_EVENT_TYPES)}"
            )

        # Cross-field check: event_type and allowed reason mapping
        allowed_reasons = EVENT_TYPE_ALLOWED_REASONS[self.event_type]
        if self.reason not in allowed_reasons:
            raise ValueError(
                f"Invalid reason '{self.reason}' for event_type '{self.event_type}'. "
                f"Allowed reasons: {sorted(allowed_reasons)}"
            )

        _validate_base_token(self.symbol, "symbol")
        _validate_base_token(self.timeframe, "timeframe")
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        sig_idx = _validate_non_negative_int(self.signal_bar_index, "signal_bar_index")
        object.__setattr__(self, "bar_index", b_idx)
        object.__setattr__(self, "signal_bar_index", sig_idx)

        # Temporal ordering & market-at-next-open locking (P1 #1)
        if sig_idx > b_idx:
            raise ValueError(
                f"signal_bar_index ({sig_idx}) cannot be greater than event bar_index ({b_idx})"
            )

        # Fail-closed timestamp validation
        b_ts = _validate_execution_timestamp(self.bar_time, "bar_time")
        sig_ts = _validate_execution_timestamp(self.signal_bar_time, "signal_bar_time")
        object.__setattr__(self, "bar_time", b_ts)
        object.__setattr__(self, "signal_bar_time", sig_ts)

        if sig_ts > b_ts:
            raise ValueError(
                f"signal_bar_time ({sig_ts}) cannot be after event bar_time ({b_ts})"
            )

        if self.event_type in ("ORDER_PENDING", "ORDER_SELECTED"):
            if b_idx != sig_idx:
                raise ValueError(
                    f"{self.event_type} must have bar_index ({b_idx}) == signal_bar_index ({sig_idx})"
                )
            if b_ts != sig_ts:
                raise ValueError(
                    f"{self.event_type} must have bar_time ({b_ts}) == signal_bar_time ({sig_ts})"
                )
        elif self.event_type in ("ORDER_FILLED", "ORDER_REJECTED", "ORDER_SKIPPED"):
            # Market-at-next-open: execution evaluation must happen exactly at bar N+1
            if b_idx != sig_idx + 1:
                raise ValueError(
                    f"{self.event_type} must occur exactly at next open bar (bar_index == signal_bar_index + 1), "
                    f"got bar_index={b_idx}, signal_bar_index={sig_idx}"
                )
            if b_ts < sig_ts:
                raise ValueError(
                    f"{self.event_type} bar_time ({b_ts}) cannot be before signal_bar_time ({sig_ts})"
                )
        elif self.event_type == "ORDER_CANCELLED":
            if self.reason == "no_next_bar":
                if b_idx != sig_idx:
                    raise ValueError(
                        f"ORDER_CANCELLED with reason 'no_next_bar' must have bar_index ({b_idx}) == signal_bar_index ({sig_idx})"
                    )
                if b_ts != sig_ts:
                    raise ValueError(
                        f"ORDER_CANCELLED with reason 'no_next_bar' must have bar_time ({b_ts}) == signal_bar_time ({sig_ts})"
                    )
        elif self.event_type == "POSITION_CLOSED":
            if b_idx < sig_idx + 1:
                raise ValueError(
                    f"POSITION_CLOSED bar_index ({b_idx}) must be >= signal_bar_index + 1 ({sig_idx + 1})"
                )
            if b_ts < sig_ts:
                raise ValueError(
                    f"POSITION_CLOSED bar_time ({b_ts}) cannot be before signal_bar_time ({sig_ts})"
                )

        # Mandatory order/strategy/setup identification and cross-checks (P1 #3)
        _validate_str(self.decision_id, "decision_id")
        strat_id = _validate_base_token(self.strategy_id, "strategy_id")
        object.__setattr__(self, "strategy_id", strat_id)

        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got '{self.direction}'")

        # Canonical decision_id cross-check
        expected_decision_id = make_decision_id(sig_idx, "SELECT", strat_id)
        if self.decision_id != expected_decision_id:
            raise ValueError(
                f"decision_id '{self.decision_id}' does not match expected canonical '{expected_decision_id}'"
            )

        # Setup ID validation and cross-checks
        _validate_str(self.setup_id, "setup_id")
        if self.cluster_id is not None:
            c_id = _validate_cluster_id(self.cluster_id, "cluster_id")
            object.__setattr__(self, "cluster_id", c_id)
            if not c_id.startswith(f"{self.direction}:"):
                raise ValueError(
                    f"cluster_id '{c_id}' direction does not match event direction '{self.direction}'"
                )
        else:
            parts = self.setup_id.split(":", 3)
            if len(parts) != 4:
                raise ValueError(
                    f"setup_id '{self.setup_id}' must contain exactly 4 components delimited by ':'"
                )
            c_id = _validate_cluster_id(parts[3], "setup_id cluster_id")
            object.__setattr__(self, "cluster_id", c_id)

        _validate_setup_id(
            setup_id=self.setup_id,
            expected_strategy_id=strat_id,
            expected_direction=self.direction,
            signal_bar_index=sig_idx,
            expected_cluster_id=c_id,
            name="setup_id",
        )

        # Stable event ID verification
        expected_event_id = make_execution_event_id(b_idx, self.event_type, strat_id, self.setup_id)
        if self.event_id != expected_event_id:
            raise ValueError(
                f"event_id '{self.event_id}' does not match expected canonical '{expected_event_id}'"
            )

        # Validate planned levels and enforce planned geometry across all events (P1 #4)
        pe = _validate_finite_float(self.planned_entry, "planned_entry", min_val=1e-9)
        psl = _validate_finite_float(self.planned_sl, "planned_sl", min_val=1e-9)
        ptp = _validate_finite_float(self.planned_tp, "planned_tp", min_val=1e-9)

        if self.direction == "BUY":
            if not (psl < pe < ptp):
                raise ValueError(
                    f"Invalid BUY planned geometry: planned_sl ({psl}) < planned_entry ({pe}) < planned_tp ({ptp}) is required"
                )
        elif self.direction == "SELL":
            if not (ptp < pe < psl):
                raise ValueError(
                    f"Invalid SELL planned geometry: planned_tp ({ptp}) < planned_entry ({pe}) < planned_sl ({psl}) is required"
                )

        object.__setattr__(self, "planned_entry", pe)
        object.__setattr__(self, "planned_sl", psl)
        object.__setattr__(self, "planned_tp", ptp)

        # Validate optional numeric fields
        for num_f in (
            "actual_entry", "actual_sl", "actual_tp",
            "risk_cash", "reward_cash", "effective_rr",
            "exit_price", "gross_pnl", "net_pnl",
        ):
            val = getattr(self, num_f)
            if val is not None:
                min_v = 1e-9 if "price" in num_f or "actual_" in num_f else None
                object.__setattr__(self, num_f, _validate_finite_float(val, num_f, min_val=min_v))

        # Cross-field completeness checks by event type
        if self.event_type in ("ORDER_PENDING", "ORDER_SELECTED"):
            for f in ("actual_entry", "actual_sl", "actual_tp", "risk_cash", "reward_cash", "effective_rr", "exit_price", "gross_pnl", "net_pnl"):
                if getattr(self, f) is not None:
                    raise ValueError(f"{self.event_type} cannot have {f}")

        elif self.event_type == "ORDER_FILLED":
            for f in ("actual_entry", "actual_sl", "actual_tp", "risk_cash", "reward_cash", "effective_rr"):
                if getattr(self, f) is None:
                    raise ValueError(f"ORDER_FILLED requires {f}")
            for f in ("exit_price", "gross_pnl", "net_pnl"):
                if getattr(self, f) is not None:
                    raise ValueError(f"ORDER_FILLED cannot have exit field {f}")

            # Strict positive accounting for ORDER_FILLED (P1 #4)
            if self.risk_cash <= 0.0:
                raise ValueError(f"ORDER_FILLED requires risk_cash > 0, got {self.risk_cash}")
            if self.reward_cash <= 0.0:
                raise ValueError(f"ORDER_FILLED requires reward_cash > 0, got {self.reward_cash}")
            if self.effective_rr <= 0.0:
                raise ValueError(f"ORDER_FILLED requires effective_rr > 0, got {self.effective_rr}")

            expected_rr = self.reward_cash / self.risk_cash
            if not math.isclose(self.effective_rr, expected_rr, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(
                    f"ORDER_FILLED effective_rr ({self.effective_rr}) does not match reward_cash / risk_cash ({expected_rr})"
                )

            # Actual geometry check according to direction (P1 #4)
            ae = self.actual_entry
            asl = self.actual_sl
            atp = self.actual_tp
            if self.direction == "BUY":
                if not (asl < ae < atp):
                    raise ValueError(
                        f"Invalid BUY actual geometry: actual_sl ({asl}) < actual_entry ({ae}) < actual_tp ({atp}) is required"
                    )
            elif self.direction == "SELL":
                if not (atp < ae < asl):
                    raise ValueError(
                        f"Invalid SELL actual geometry: actual_tp ({atp}) < actual_entry ({ae}) < actual_sl ({asl}) is required"
                    )

        elif self.event_type == "ORDER_REJECTED":
            for f in ("actual_entry", "actual_sl", "actual_tp"):
                if getattr(self, f) is None:
                    raise ValueError(f"ORDER_REJECTED requires actual fill-attempt field {f}")
            for f in ("exit_price", "gross_pnl", "net_pnl"):
                if getattr(self, f) is not None:
                    raise ValueError(f"ORDER_REJECTED cannot have exit field {f}")

        elif self.event_type == "ORDER_SKIPPED":
            for f in ("exit_price", "gross_pnl", "net_pnl"):
                if getattr(self, f) is not None:
                    raise ValueError(f"ORDER_SKIPPED cannot have exit field {f}")

        elif self.event_type == "ORDER_CANCELLED":
            for f in ("actual_entry", "actual_sl", "actual_tp", "risk_cash", "reward_cash", "effective_rr", "exit_price", "gross_pnl", "net_pnl"):
                if getattr(self, f) is not None:
                    raise ValueError(f"ORDER_CANCELLED cannot have actual or exit field {f}")

        elif self.event_type == "POSITION_CLOSED":
            for f in ("exit_price", "gross_pnl", "net_pnl", "actual_entry", "actual_sl", "actual_tp"):
                if getattr(self, f) is None:
                    raise ValueError(f"POSITION_CLOSED requires {f}")

        if self.cluster_id is not None:
            _validate_cluster_id(self.cluster_id, "cluster_id")

        object.__setattr__(self, "meta", _freeze(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_version": self.event_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "reason": self.reason,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_index": int(self.bar_index),
            "bar_time": self.bar_time.isoformat(),
            "signal_bar_index": int(self.signal_bar_index),
            "signal_bar_time": self.signal_bar_time.isoformat(),
            "decision_id": self.decision_id,
            "strategy_id": self.strategy_id,
            "setup_id": self.setup_id,
            "direction": self.direction,
            "planned_entry": float(self.planned_entry),
            "planned_sl": float(self.planned_sl),
            "planned_tp": float(self.planned_tp),
            "actual_entry": float(self.actual_entry) if self.actual_entry is not None else None,
            "actual_sl": float(self.actual_sl) if self.actual_sl is not None else None,
            "actual_tp": float(self.actual_tp) if self.actual_tp is not None else None,
            "risk_cash": float(self.risk_cash) if self.risk_cash is not None else None,
            "reward_cash": float(self.reward_cash) if self.reward_cash is not None else None,
            "effective_rr": float(self.effective_rr) if self.effective_rr is not None else None,
            "exit_price": float(self.exit_price) if self.exit_price is not None else None,
            "gross_pnl": float(self.gross_pnl) if self.gross_pnl is not None else None,
            "net_pnl": float(self.net_pnl) if self.net_pnl is not None else None,
            "cluster_id": self.cluster_id,
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionEvent:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for ExecutionEvent, got {type(data).__name__}")
        REQUIRED_KEYS = frozenset({
            "event_version", "event_id", "event_type", "reason",
            "symbol", "timeframe", "bar_index", "bar_time",
            "signal_bar_index", "signal_bar_time", "decision_id",
            "strategy_id", "setup_id", "direction",
            "planned_entry", "planned_sl", "planned_tp",
            "actual_entry", "actual_sl", "actual_tp",
            "risk_cash", "reward_cash", "effective_rr",
            "exit_price", "gross_pnl", "net_pnl",
            "cluster_id", "meta",
        })
        unknown = set(data.keys()) - REQUIRED_KEYS
        if unknown:
            raise KeyError(f"Unknown fields in ExecutionEvent: {sorted(unknown)}")
        missing = [k for k in sorted(REQUIRED_KEYS) if k not in data]
        if missing:
            raise KeyError(f"Missing required field(s) in ExecutionEvent: {missing}")

        return cls(
            event_version=data["event_version"],
            event_id=data["event_id"],
            event_type=data["event_type"],
            reason=data["reason"],
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            bar_index=data["bar_index"],
            bar_time=data["bar_time"],  # Pass raw value to constructor for strict validation
            signal_bar_index=data["signal_bar_index"],
            signal_bar_time=data["signal_bar_time"],  # Pass raw value to constructor
            decision_id=data["decision_id"],
            strategy_id=data["strategy_id"],
            setup_id=data["setup_id"],
            direction=data["direction"],
            planned_entry=data["planned_entry"],
            planned_sl=data["planned_sl"],
            planned_tp=data["planned_tp"],
            actual_entry=data["actual_entry"],
            actual_sl=data["actual_sl"],
            actual_tp=data["actual_tp"],
            risk_cash=data["risk_cash"],
            reward_cash=data["reward_cash"],
            effective_rr=data["effective_rr"],
            exit_price=data["exit_price"],
            gross_pnl=data["gross_pnl"],
            net_pnl=data["net_pnl"],
            cluster_id=data["cluster_id"],
            meta=data["meta"],
        )


# =============================================================================
# CooldownInterval & CooldownBook
# =============================================================================

@dataclass(frozen=True)
class CooldownInterval:
    """
    Immutable value object representing an active cooldown interval [start_bar, expiry_bar).
    Strictly requires 0 <= start_bar < expiry_bar.
    """
    start_bar: int
    expiry_bar: int

    def __post_init__(self) -> None:
        s = _validate_non_negative_int(self.start_bar, "start_bar")
        e = _validate_non_negative_int(self.expiry_bar, "expiry_bar")
        object.__setattr__(self, "start_bar", s)
        object.__setattr__(self, "expiry_bar", e)
        if s >= e:
            raise ValueError(f"start_bar ({s}) must be strictly less than expiry_bar ({e})")


class CooldownBook:
    """
    Deterministic online current-state cooldown tracker per (strategy_id, direction).
    Tracks the active cooldown interval [start_bar, expiry_bar) for the most recent fill.

    Guarantees:
    - Bounded O(1) online state per (strategy_id, direction).
    - Interval [start_bar, expiry_bar): active strictly within [start_bar, expiry_bar),
      inactive before start_bar (no lookahead) and from expiry_bar onwards.
    - Non-bridging: separate intervals do not bridge gaps.
    - Safe merge: duplicate/retry fills and fills during active cooldown do not shorten expiry.
    - Stale fills (new_start < existing_start) are ignored.
    - K=0 is a strict no-op.
    - Strict from_snapshot() schema with exact keys {'start', 'expiry'} and exact JSON round-trip parity.
    """

    def __init__(self) -> None:
        self._cooldowns: dict[tuple[str, str], CooldownInterval] = {}

    def record_fill(
        self,
        strategy_id: str,
        direction: str,
        fill_bar_index: int,
        cooldown_bars: int = 3,
    ) -> None:
        """
        Record a successful fill and establish cooldown on interval [F, F+K).
        K == 0 is a strict no-op.
        """
        _validate_base_token(strategy_id, "strategy_id")
        if direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got '{direction}'")
        f_idx = _validate_non_negative_int(fill_bar_index, "fill_bar_index")
        k_bars = _validate_non_negative_int(cooldown_bars, "cooldown_bars")

        if k_bars == 0:
            return

        new_start = f_idx
        new_expiry = f_idx + k_bars
        key = (strategy_id, direction)

        if key not in self._cooldowns:
            self._cooldowns[key] = CooldownInterval(new_start, new_expiry)
            return

        existing = self._cooldowns[key]
        if new_start < existing.start_bar:
            # Stale / out-of-order fill: ignore, do not change state
            return
        elif new_start == existing.start_bar:
            # Duplicate / retry fill: do not shorten expiry
            self._cooldowns[key] = CooldownInterval(
                existing.start_bar,
                max(existing.expiry_bar, new_expiry),
            )
        else:
            # new_start > existing.start_bar: successful newer fill
            # If previous cooldown already expired (existing.expiry_bar <= new_start),
            # interval is strictly [new_start, new_expiry) without bridging gaps.
            # If previous cooldown is still active, extend expiry if new_expiry > existing.expiry_bar.
            self._cooldowns[key] = CooldownInterval(
                new_start,
                max(existing.expiry_bar, new_expiry),
            )

    def is_active(self, strategy_id: str, direction: str, current_bar_index: int) -> bool:
        """
        Check whether cooldown is currently active at current_bar_index in interval [F, F+K).
        Guarantees that current_bar_index < fill_bar returns False (no lookahead).
        """
        _validate_base_token(strategy_id, "strategy_id")
        if direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got '{direction}'")
        c_idx = _validate_non_negative_int(current_bar_index, "current_bar_index")

        entry = self._cooldowns.get((strategy_id, direction))
        if entry is None:
            return False
        return entry.start_bar <= c_idx < entry.expiry_bar

    def get_cooldown_remaining(self, strategy_id: str, direction: str, current_bar_index: int) -> int:
        """Get remaining cooldown bars from current_bar_index (0 if expired, inactive, or before start)."""
        _validate_base_token(strategy_id, "strategy_id")
        if direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be 'BUY' or 'SELL', got '{direction}'")
        c_idx = _validate_non_negative_int(current_bar_index, "current_bar_index")

        entry = self._cooldowns.get((strategy_id, direction))
        if entry is None:
            return 0
        if c_idx < entry.start_bar or c_idx >= entry.expiry_bar:
            return 0
        return entry.expiry_bar - c_idx

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Return deterministic sorted snapshot mapping 'strategy_id:direction' -> {'start': start_bar, 'expiry': expiry_bar}."""
        res: dict[str, dict[str, int]] = {}
        for (sid, direct) in sorted(self._cooldowns.keys()):
            entry = self._cooldowns[(sid, direct)]
            res[f"{sid}:{direct}"] = {
                "start": entry.start_bar,
                "expiry": entry.expiry_bar,
            }
        return res

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> CooldownBook:
        """
        Restore CooldownBook from snapshot dict with strict grammar and numeric validation.
        Rejects invalid key format, invalid direction, negative expiry, bool-as-int,
        legacy integer/list/tuple representations, missing/extra keys, and start >= expiry.
        """
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for CooldownBook snapshot, got {type(data).__name__}")

        book = cls()
        for key, raw_val in data.items():
            if not isinstance(key, str):
                raise StrictModelTypeError(f"Cooldown key must be a string, got {type(key).__name__}")
            parts = key.split(":")
            if len(parts) != 2:
                raise ValueError(f"Invalid cooldown key format '{key}'. Expected 'strategy_id:direction'")
            sid, direct = parts[0], parts[1]
            _validate_base_token(sid, "cooldown strategy_id")
            if direct not in {"BUY", "SELL"}:
                raise ValueError(f"Invalid direction '{direct}' in cooldown key '{key}'")

            if not isinstance(raw_val, dict):
                raise StrictModelTypeError(
                    f"Cooldown value for '{key}' must be a dict with keys 'start' and 'expiry', got {type(raw_val).__name__}"
                )

            if set(raw_val.keys()) != {"start", "expiry"}:
                raise ValueError(
                    f"Cooldown value for '{key}' must have exactly keys ('start', 'expiry'), got {sorted(raw_val.keys())}"
                )

            start = _validate_non_negative_int(raw_val["start"], f"start for '{key}'")
            expiry = _validate_non_negative_int(raw_val["expiry"], f"expiry for '{key}'")

            if start >= expiry:
                raise ValueError(f"start ({start}) must be strictly less than expiry ({expiry}) for '{key}'")

            book._cooldowns[(sid, direct)] = CooldownInterval(start, expiry)

        return book

    def reset(self) -> None:
        """Reset all cooldown states."""
        self._cooldowns.clear()
