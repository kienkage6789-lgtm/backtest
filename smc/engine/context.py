"""
smc/engine/context.py
=====================
As-of StrategyContext Builder Module for Multi-Strategy Confluence Engine (T53.2).

Constructs deep-immutable, zero-lookahead, deterministic StrategyContext snapshots
at every closed candle N, encapsulating all verified SMC state as-of bar N.

Lifecycle Contract Table (As-of Bar N):
--------------------------------------------------------------------------------------
| Object          | Mốc bắt đầu nhìn thấy | Lifecycle Fields                | Quy tắc as-of N                                                     |
|-----------------|-----------------------|---------------------------------|---------------------------------------------------------------------|
| SwingPoint      | confirmed_at <= N     | broken, broken_at               | broken=True iff broken_at <= N; broken_at > N trở lại broken=False |
| StructureEvent  | index <= N            | direction, mode, leg_id         | Chỉ nhận event index <= N, cấm event tương lai                     |
| FairValueGap    | confirmed_at <= N     | filled, filled_at               | Cấm filled_at > N; active khi filled_at is None or filled_at == N   |
| OrderBlock      | created_at <= N       | mitigation, invalid, retest     | Post-update state tại N; active khi valid=True & invalidated is None |
| LiquidityPool   | confirmed_at <= N     | swept, swept_at, valid          | Cấm swept_at > N; active khi valid=True & not swept                |
| LiquiditySweep  | confirmed_at <= N     | valid, swept_at, price_wick     | Chỉ nhận sweep confirmed_at <= N & swept_at <= N                   |
| SessionDecision | đúng candle N         | in_session, session_name        | Đánh giá candle N đã đóng; timestamp không sau bar_close_time       |
| BiasState       | effective <= close    | bias, source_event_index        | Chỉ dùng HTF event có effective_time <= bar_close_time              |
--------------------------------------------------------------------------------------
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from collections import deque
from collections.abc import Mapping, Sequence, Hashable
import datetime
import math
from types import MappingProxyType
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Union

import numpy as np
import pandas as pd

from smc.models import (
    SwingPoint,
    StructureEvent,
    FairValueGap,
    OrderBlock,
    LiquidityPool,
    LiquiditySweep,
    SessionWindow,
    SessionDecision,
    BiasState,
    HTFPOI,
)
from smc.data_contract import normalize_ohlcv
from smc.structure.swings import SwingDetectorState
from smc.structure.bos_choch import StructureTracker
from smc.zones.fvg import FVGTracker
from smc.zones.order_block import OrderBlockTracker
from smc.liquidity.detector import LiquidityTracker
from smc.context.session import SessionFilter, _resolve_closed_state
from smc.context.htf_bias import HTFBiasTracker, _get_effective_time, _parse_timezone_aware_timestamp
from smc.context.htf_poi import HTFPOITracker
from smc.engine.models import (
    StrategyContext,
    SwingPointSnapshot,
    StructureEventSnapshot,
    FairValueGapSnapshot,
    OrderBlockSnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    SessionDecisionSnapshot,
    BiasStateSnapshot,
    HTFPOISnapshot,
    StrictModelTypeError,
    _validate_non_negative_int,
    _validate_finite_float,
    _validate_bool,
    _validate_str,
    _parse_timestamp,
    _freeze,
    _unfreeze,
)


SUPPORTED_TIMEFRAMES: dict[str, pd.Timedelta] = {
    "M1": pd.Timedelta(minutes=1),
    "M5": pd.Timedelta(minutes=5),
    "M15": pd.Timedelta(minutes=15),
    "M30": pd.Timedelta(minutes=30),
    "H1": pd.Timedelta(hours=1),
    "H4": pd.Timedelta(hours=4),
    "D1": pd.Timedelta(days=1),
}


def _validate_positive_int_field(val: Any, name: str) -> int:
    """Validates that a field is a positive integer, rejecting bools."""
    if isinstance(val, (bool, np.bool_)):
        raise StrictModelTypeError(f"{name} must be a positive integer, got boolean: {val}")
    if not isinstance(val, (int, np.integer)):
        raise StrictModelTypeError(f"{name} must be an integer, got {type(val).__name__}: {val}")
    int_val = int(val)
    if int_val <= 0:
        raise ValueError(f"{name} must be strictly positive (> 0), got: {int_val}")
    return int_val


@dataclass(frozen=True)
class ContextBuilderConfig:
    """
    Configuration parameters for StrategyContextBuilder.
    Enforces strict typing, positive lookbacks, and valid timeframe contracts.
    """
    symbol: str = "XAUUSD"
    timeframe: str = "M15"
    swing_strength: int = 5
    swing_left_strength: Optional[int] = None
    swing_right_strength: Optional[int] = None
    structure_mode: Literal["swing", "internal"] = "swing"
    atr_period: int = 14
    displacement_multiplier: float = 1.5
    fvg_min_gap_pct: float = 0.0
    fvg_require_displacement: bool = False
    ob_lookback: int = 20
    ob_require_fvg: bool = False
    liquidity_tolerance_pct: Optional[float] = 0.001
    sessions: Optional[Tuple[SessionWindow, ...]] = None
    session_timezone: Optional[str] = None
    htf_conflict_policy: str = "neutral"
    max_recent_swings: int = 50
    max_recent_structures: int = 50
    max_active_fvgs: int = 50
    max_active_obs: int = 50
    max_active_pools: int = 50
    max_recent_sweeps: int = 50

    def __post_init__(self):
        object.__setattr__(self, "symbol", _validate_str(self.symbol, "symbol"))
        tf = _validate_str(self.timeframe, "timeframe")
        if tf not in SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"Unsupported timeframe '{tf}'. Supported timeframes: {sorted(SUPPORTED_TIMEFRAMES.keys())}"
            )
        object.__setattr__(self, "timeframe", tf)

        object.__setattr__(self, "swing_strength", _validate_positive_int_field(self.swing_strength, "swing_strength"))
        if self.swing_left_strength is not None:
            object.__setattr__(self, "swing_left_strength", _validate_positive_int_field(self.swing_left_strength, "swing_left_strength"))
        if self.swing_right_strength is not None:
            object.__setattr__(self, "swing_right_strength", _validate_positive_int_field(self.swing_right_strength, "swing_right_strength"))

        if self.structure_mode not in {"swing", "internal"}:
            raise ValueError(f"Invalid structure_mode '{self.structure_mode}'. Must be 'swing' or 'internal'.")

        object.__setattr__(self, "atr_period", _validate_positive_int_field(self.atr_period, "atr_period"))
        object.__setattr__(self, "displacement_multiplier", _validate_finite_float(self.displacement_multiplier, "displacement_multiplier", min_val=0.0001))
        object.__setattr__(self, "fvg_min_gap_pct", _validate_finite_float(self.fvg_min_gap_pct, "fvg_min_gap_pct", min_val=0.0))
        object.__setattr__(self, "fvg_require_displacement", _validate_bool(self.fvg_require_displacement, "fvg_require_displacement"))
        object.__setattr__(self, "ob_lookback", _validate_positive_int_field(self.ob_lookback, "ob_lookback"))
        object.__setattr__(self, "ob_require_fvg", _validate_bool(self.ob_require_fvg, "ob_require_fvg"))

        if self.liquidity_tolerance_pct is not None:
            object.__setattr__(self, "liquidity_tolerance_pct", _validate_finite_float(self.liquidity_tolerance_pct, "liquidity_tolerance_pct", min_val=0.00001))

        if self.sessions is not None:
            object.__setattr__(self, "sessions", tuple(self.sessions))

        if self.htf_conflict_policy != "neutral":
            raise ValueError(f"Unsupported htf_conflict_policy '{self.htf_conflict_policy}'. Only 'neutral' is supported.")

        object.__setattr__(self, "max_recent_swings", _validate_positive_int_field(self.max_recent_swings, "max_recent_swings"))
        object.__setattr__(self, "max_recent_structures", _validate_positive_int_field(self.max_recent_structures, "max_recent_structures"))
        object.__setattr__(self, "max_active_fvgs", _validate_positive_int_field(self.max_active_fvgs, "max_active_fvgs"))
        object.__setattr__(self, "max_active_obs", _validate_positive_int_field(self.max_active_obs, "max_active_obs"))
        object.__setattr__(self, "max_active_pools", _validate_positive_int_field(self.max_active_pools, "max_active_pools"))
        object.__setattr__(self, "max_recent_sweeps", _validate_positive_int_field(self.max_recent_sweeps, "max_recent_sweeps"))


def validate_as_of_evidence(evidence: Any, current_bar_index: int, bar_close_time: pd.Timestamp) -> None:
    """
    Public low-level snapshot validator ensuring zero future lookahead.
    Raises ValueError immediately if future timestamps, bar indices, or lifecycle states are detected.
    """
    if isinstance(evidence, (SwingPoint, SwingPointSnapshot)):
        if evidence.confirmed_at > current_bar_index:
            raise ValueError(
                f"Future state leak: SwingPoint confirmed_at ({evidence.confirmed_at}) > current_bar_index ({current_bar_index})"
            )
        if evidence.broken_at is not None and evidence.broken_at > current_bar_index:
            raise ValueError(
                f"Future state leak: SwingPoint broken_at ({evidence.broken_at}) > current_bar_index ({current_bar_index})"
            )

    elif isinstance(evidence, (StructureEvent, StructureEventSnapshot)):
        if evidence.index > current_bar_index:
            raise ValueError(
                f"Future state leak: StructureEvent index ({evidence.index}) > current_bar_index ({current_bar_index})"
            )

    elif isinstance(evidence, (FairValueGap, FairValueGapSnapshot)):
        if evidence.confirmed_at > current_bar_index:
            raise ValueError(
                f"Future state leak: FairValueGap confirmed_at ({evidence.confirmed_at}) > current_bar_index ({current_bar_index})"
            )
        if evidence.filled_at is not None and evidence.filled_at > current_bar_index:
            raise ValueError(
                f"Future state leak: FairValueGap filled_at ({evidence.filled_at}) > current_bar_index ({current_bar_index})"
            )

    elif isinstance(evidence, (OrderBlock, OrderBlockSnapshot)):
        created_at = evidence.created_at if evidence.created_at != -1 else evidence.source_event_index
        if created_at > current_bar_index:
            raise ValueError(
                f"Future state leak: OrderBlock created_at ({created_at}) > current_bar_index ({current_bar_index})"
            )
        if evidence.mitigated_at is not None and evidence.mitigated_at > current_bar_index:
            raise ValueError(
                f"Future state leak: OrderBlock mitigated_at ({evidence.mitigated_at}) > current_bar_index ({current_bar_index})"
            )
        if evidence.invalidated_at is not None and evidence.invalidated_at > current_bar_index:
            raise ValueError(
                f"Future state leak: OrderBlock invalidated_at ({evidence.invalidated_at}) > current_bar_index ({current_bar_index})"
            )

    elif isinstance(evidence, (LiquidityPool, LiquidityPoolSnapshot)):
        if evidence.confirmed_at > current_bar_index:
            raise ValueError(
                f"Future state leak: LiquidityPool confirmed_at ({evidence.confirmed_at}) > current_bar_index ({current_bar_index})"
            )
        if evidence.swept_at is not None and evidence.swept_at > current_bar_index:
            raise ValueError(
                f"Future state leak: LiquidityPool swept_at ({evidence.swept_at}) > current_bar_index ({current_bar_index})"
            )
        if evidence.invalidated_at is not None and evidence.invalidated_at > current_bar_index:
            raise ValueError(
                f"Future state leak: LiquidityPool invalidated_at ({evidence.invalidated_at}) > current_bar_index ({current_bar_index})"
            )

    elif isinstance(evidence, (LiquiditySweep, LiquiditySweepSnapshot)):
        if evidence.confirmed_at > current_bar_index:
            raise ValueError(
                f"Future state leak: LiquiditySweep confirmed_at ({evidence.confirmed_at}) > current_bar_index ({current_bar_index})"
            )
        if evidence.swept_at > current_bar_index:
            raise ValueError(
                f"Future state leak: LiquiditySweep swept_at ({evidence.swept_at}) > current_bar_index ({current_bar_index})"
            )

    elif isinstance(evidence, (BiasState, BiasStateSnapshot)):
        if evidence.as_of is not None and evidence.as_of > bar_close_time:
            raise ValueError(
                f"Future state leak: BiasState as_of ({evidence.as_of}) > bar_close_time ({bar_close_time})"
            )

    elif isinstance(evidence, (SessionDecision, SessionDecisionSnapshot)):
        if evidence.timestamp > bar_close_time:
            raise ValueError(
                f"Future state leak: SessionDecision timestamp ({evidence.timestamp}) > bar_close_time ({bar_close_time})"
            )


def _normalize_and_validate_htf_events(
    new_htf_events: Optional[Sequence[StructureEvent]],
) -> tuple[StructureEvent, ...]:
    """
    Validates and deduplicates incoming HTF events:
    - If identical ID and identical payload: deduplicate deterministically.
    - If identical ID but differing payload: raise ValueError.
    - Returns deterministic tuple of unique events sorted by (index, mode, event_type, direction).
    """
    if not new_htf_events:
        return ()

    seen_events: dict[tuple, StructureEvent] = {}
    for ev in new_htf_events:
        if not isinstance(ev, StructureEvent):
            raise TypeError(f"Expected StructureEvent in new_htf_events, got {type(ev)}")
        ev_key = (ev.mode, ev.event_type, ev.direction, ev.index)
        if ev_key in seen_events:
            existing = seen_events[ev_key]
            if existing.to_dict() != ev.to_dict():
                raise ValueError(
                    f"Conflicting duplicate HTF events with identical ID {ev_key} but differing payloads:\n"
                    f"Existing: {existing.to_dict()}\n"
                    f"Incoming: {ev.to_dict()}"
                )
            continue
        seen_events[ev_key] = ev

    return tuple(sorted(seen_events.values(), key=lambda e: (e.index, e.mode, e.event_type, e.direction)))


def _deduplicate_and_sort_evidence(
    items: Sequence[Any],
    id_func: Any,
    sort_key_func: Any,
) -> tuple[Any, ...]:
    """
    Deduplicates evidence objects by stable ID:
    - If duplicate ID has identical payload: keep one.
    - If duplicate ID has differing payload: raise ValueError for conflict.
    - Returns deterministically sorted tuple.
    """
    if not items:
        return ()
    if len(items) == 1:
        return (items[0],)

    seen_ids: set[Any] = set()
    has_duplicate = False
    for item in items:
        item_id = id_func(item)
        if item_id in seen_ids:
            has_duplicate = True
            break
        seen_ids.add(item_id)

    if not has_duplicate:
        return tuple(sorted(items, key=sort_key_func))

    seen_items: dict[Any, Any] = {}
    unique_items: list[Any] = []

    for item in items:
        item_id = id_func(item)

        if item_id in seen_items:
            existing = seen_items[item_id]
            if existing != item:
                d1 = existing.to_dict() if hasattr(existing, "to_dict") else dataclasses.asdict(existing)
                d2 = item.to_dict() if hasattr(item, "to_dict") else dataclasses.asdict(item)
                if d1 != d2:
                    raise ValueError(
                        f"Conflicting duplicate evidence detected for ID '{item_id}':\n"
                        f"Existing: {d1}\n"
                        f"Incoming: {d2}"
                    )
            continue

        seen_items[item_id] = item
        unique_items.append(item)

    unique_items.sort(key=sort_key_func)
    return tuple(unique_items)



# =============================================================================
# Canonical Hashable Representation Helper (P2.2)
# =============================================================================

def _canonical_key_value(value: Any) -> Hashable:
    """
    Recursively transforms arbitrary metadata or nested structures into a deterministic,
    strictly hashable canonical representation suitable for dictionary/state cache keys.

    Contract:
    - Every scalar/container kind is explicitly tagged so bool/int/float cannot collide.
    - Mapping and set ordering is independent of insertion order.
    - Non-finite float (NaN, inf, -inf) -> ValueError
    - Unsupported types -> TypeError
    - Does NOT use repr()/str() of arbitrary object
    - Does NOT use object address or Python randomized hash
    """
    if value is None:
        return ("none",)
    if isinstance(value, (bool, np.bool_)):
        return ("bool", bool(value))
    if isinstance(value, (int, np.integer)):
        return ("int", int(value))
    if isinstance(value, (float, np.floating)):
        f_val = float(value)
        if not math.isfinite(f_val):
            raise ValueError(f"Non-finite float value not allowed in canonical key: {f_val}")
        return ("float", f_val)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, pd.Timestamp):
        if value.tz is not None:
            value = value if str(value.tz) == "UTC" else value.tz_convert("UTC")
        else:
            value = value.tz_localize("UTC")
        return ("timestamp", value.value)
    if isinstance(value, (datetime.datetime, datetime.date, np.datetime64)):
        ts = pd.Timestamp(value)
        if ts.tz is not None:
            ts = ts if str(ts.tz) == "UTC" else ts.tz_convert("UTC")
        else:
            ts = ts.tz_localize("UTC")
        return ("timestamp", ts.value)
    if isinstance(value, (dict, Mapping, MappingProxyType)):
        canonical_items = []
        for k, v in value.items():
            c_k = _canonical_key_value(k)
            c_v = _canonical_key_value(v)
            canonical_items.append((c_k, c_v))
        try:
            canonical_items.sort(key=lambda item: item[0])
        except TypeError:
            canonical_items.sort(key=lambda item: (type(item[0]).__name__, str(item[0])))
        return ("mapping", tuple(canonical_items))
    if isinstance(value, (set, frozenset)):
        items = [_canonical_key_value(x) for x in value]
        try:
            items.sort()
        except TypeError:
            items.sort(key=lambda x: (type(x).__name__, str(x)))
        return ("set", tuple(items))
    if isinstance(value, (list, tuple)):
        return ("sequence", tuple(_canonical_key_value(x) for x in value))

    raise TypeError(f"Unsupported metadata type for canonical key value: {type(value).__name__}: {value}")


# =============================================================================
# Snapshot Identity & State Key Helpers (P2.1: 100% field coverage)
# =============================================================================

def _swing_identity_key(s: Any) -> tuple:
    """Identity key for SwingPoint snapshot caching: (mode, kind, index)."""
    return (s.mode, s.kind, s.index)


def _swing_state_key(s: Any, *, is_broken: bool, b_at: Optional[int]) -> tuple:
    """Comprehensive state fingerprint for SwingPoint covering 100% of snapshot fields."""
    t_val = s.time.value if isinstance(s.time, pd.Timestamp) and str(s.time.tz) == "UTC" else _canonical_key_value(s.time)
    ct = getattr(s, "confirmed_time", None)
    ct_val = ct.value if isinstance(ct, pd.Timestamp) and str(ct.tz) == "UTC" else (_canonical_key_value(ct) if ct is not None else None)
    return (
        s.index,
        t_val,
        float(s.price),
        s.kind,
        int(s.strength),
        int(s.confirmed_at),
        ct_val,
        s.mode,
        getattr(s, "classification", "UNCLASSIFIED"),
        bool(is_broken),
        b_at,
    )


def _structure_identity_key(st: Any) -> tuple:
    """Identity key for StructureEvent snapshot caching: (mode, event_type, direction, index)."""
    return (st.mode, st.event_type, st.direction, st.index)


def _structure_state_key(st: Any) -> tuple:
    """Comprehensive state fingerprint for StructureEvent covering 100% of snapshot fields."""
    t_val = st.time.value if isinstance(st.time, pd.Timestamp) and str(st.time.tz) == "UTC" else _canonical_key_value(st.time)
    return (
        st.index,
        t_val,
        st.event_type,
        st.direction,
        int(st.broken_swing_index),
        float(st.broken_swing_price),
        float(st.close_price),
        bool(st.displacement),
        st.mode,
        int(getattr(st, "confirmed_swing_at", 0)),
        float(getattr(st, "body_size", 0.0)),
        float(getattr(st, "atr_value", 0.0)),
        getattr(st, "break_type", "close"),
        getattr(st, "structure_leg_id", None),
    )


def _fvg_identity_key(f: Any) -> tuple:
    """Identity key for FairValueGap snapshot caching: (mode, direction, index)."""
    return (f.mode, f.direction, f.index)


def _fvg_state_key(f: Any) -> tuple:
    """Comprehensive state fingerprint for FairValueGap covering 100% of snapshot fields."""
    t_val = f.time.value if isinstance(f.time, pd.Timestamp) and str(f.time.tz) == "UTC" else _canonical_key_value(f.time)
    return (
        f.index,
        t_val,
        f.direction,
        float(f.top),
        float(f.bottom),
        f.mode,
        int(getattr(f, "confirmed_at", 0)),
        bool(getattr(f, "filled", False)),
        getattr(f, "filled_at", None),
        getattr(f, "structure_leg_id", None),
        getattr(f, "ce", None),
        getattr(f, "gap_size", None),
        getattr(f, "gap_pct", None),
        bool(getattr(f, "displacement", False)),
        float(getattr(f, "body_ratio", 0.0)),
        float(getattr(f, "atr_value", 0.0)),
        int(getattr(f, "touch_count", 0)),
        bool(getattr(f, "ce_touched", False)),
        getattr(f, "ce_touched_at", None),
        bool(getattr(f, "partial_filled", False)),
        getattr(f, "partial_filled_at", None),
        float(getattr(f, "middle_body_size", 0.0)),
        float(getattr(f, "middle_range", 0.0)),
        float(getattr(f, "middle_body_ratio", 0.0)),
        getattr(f, "state", "active"),
    )


def _ob_identity_key(ob: Any) -> tuple:
    """Identity key for OrderBlock snapshot caching: (mode, direction, source_event_index, index)."""
    return (ob.mode, ob.direction, ob.source_event_index, ob.index)


def _ob_state_key(ob: Any, *, created_at_val: int) -> tuple:
    """Comprehensive state fingerprint for OrderBlock covering 100% of snapshot fields."""
    t_val = ob.time.value if isinstance(ob.time, pd.Timestamp) and str(ob.time.tz) == "UTC" else _canonical_key_value(ob.time)
    fvg_top = getattr(ob, "source_fvg_top", None)
    fvg_bottom = getattr(ob, "source_fvg_bottom", None)
    return (
        ob.index,
        t_val,
        ob.direction,
        float(ob.high),
        float(ob.low),
        float(ob.open),
        float(ob.close),
        getattr(ob, "origin_type", "BOS"),
        ob.mode,
        getattr(ob, "quality", "base"),
        int(ob.source_event_index),
        getattr(ob, "source_event_type", ""),
        int(getattr(ob, "source_swing_index", -1)),
        int(created_at_val),
        getattr(ob, "source_fvg_index", None),
        float(fvg_top) if fvg_top is not None else None,
        float(fvg_bottom) if fvg_bottom is not None else None,
        bool(ob.mitigated),
        ob.mitigated_at,
        float(ob.mitigation_pct),
        bool(ob.valid),
        ob.invalidated_at,
        getattr(ob, "invalidation_reason", None),
        int(ob.retest_count),
        getattr(ob, "structure_leg_id", None),
    )


def _pool_identity_key(p: Any) -> tuple:
    """Identity key for LiquidityPool snapshot caching: (mode, kind, sorted_indices)."""
    probe = (p.mode, p.kind, tuple(p.indices))
    if probe == getattr(p, "_snapshot_identity_probe", None):
        return getattr(p, "_snapshot_identity_key")
    key = (p.mode, p.kind, tuple(sorted(p.indices)))
    try:
        setattr(p, "_snapshot_identity_probe", probe)
        setattr(p, "_snapshot_identity_key", key)
    except AttributeError:
        pass
    return key


def _pool_state_key(p: Any) -> tuple:
    """Comprehensive state fingerprint for LiquidityPool covering 100% of snapshot fields."""
    swings = getattr(p, "source_swings", ())
    # Source metadata is stable between merges.  Avoid recursively canonicalizing
    # every historical mapping on every bar, while retaining mutation detection
    # for the dict-shaped metadata produced by LiquidityTracker.
    probe = None
    if isinstance(swings, list) and all(isinstance(sw, dict) for sw in swings):
        try:
            probe = tuple(tuple(sorted((k, type(v).__name__, v) for k, v in sw.items())) for sw in swings)
        except TypeError:
            probe = None
    sentinel = object()
    cached_probe = getattr(p, "_canonical_source_swings_probe", sentinel)
    if probe is not None and probe == cached_probe:
        swings_rep = getattr(p, "_canonical_source_swings")
    else:
        swings_rep = _canonical_key_value(swings)
        if probe is not None:
            try:
                setattr(p, "_canonical_source_swings_probe", probe)
                setattr(p, "_canonical_source_swings", swings_rep)
            except AttributeError:
                pass
    return (
        p.kind,
        float(p.price),
        float(p.price_max),
        float(p.price_min),
        tuple(sorted(p.indices)),
        int(p.created_at),
        int(p.confirmed_at),
        bool(p.swept),
        p.swept_at,
        getattr(p, "sweep_type", None),
        bool(p.valid),
        p.invalidated_at,
        getattr(p, "invalidation_reason", None),
        p.mode,
        swings_rep,
        getattr(p, "structure_leg_id", None),
        getattr(p, "liquidity_side", None),
    )


def _sweep_identity_key(sw: Any) -> tuple:
    """Identity key for LiquiditySweep snapshot caching: (mode, direction, index, sorted_pool_indices)."""
    return (sw.mode, sw.direction, sw.index, tuple(sorted(sw.pool_indices)))


def _sweep_state_key(sw: Any) -> tuple:
    """Comprehensive state fingerprint for LiquiditySweep covering 100% of snapshot fields."""
    t_val = sw.time.value if isinstance(sw.time, pd.Timestamp) and str(sw.time.tz) == "UTC" else _canonical_key_value(sw.time)
    return (
        sw.index,
        t_val,
        sw.direction,
        sw.pool_kind,
        float(sw.pool_price),
        tuple(sorted(sw.pool_indices)),
        float(sw.price_wick),
        float(sw.close_price),
        int(sw.created_at),
        int(sw.confirmed_at),
        int(sw.swept_at),
        getattr(sw, "sweep_type", "clean"),
        bool(getattr(sw, "valid", True)),
        sw.mode,
        getattr(sw, "structure_leg_id", None),
        getattr(sw, "liquidity_side", None),
        getattr(sw, "raid_direction", None),
        getattr(sw, "reversal_direction", None),
    )


class StrategyContextBuilder:
    """
    Incremental stateful StrategyContext builder for real-time streaming and replay backtest.
    Guarantees zero-lookahead, monotonic updates, deep immutability, and batch/incremental parity.
    Optimized for O(1) step time with bounded lookback buffers.
    """

    def __init__(
        self,
        config: Optional[ContextBuilderConfig] = None,
        *,
        htf_events: Optional[List[StructureEvent]] = None,
        htf_pois: Optional[Sequence[Union[HTFPOI, dict[str, Any]]]] = None,
    ):
        self._config = config if config is not None else ContextBuilderConfig()

        # Detector instances
        self._swing_detector = SwingDetectorState(
            strength=self._config.swing_strength,
            left_strength=self._config.swing_left_strength,
            right_strength=self._config.swing_right_strength,
            mode=self._config.structure_mode,
        )
        self._structure_tracker = StructureTracker(
            mode=self._config.structure_mode,
            atr_period=self._config.atr_period,
            displacement_multiplier=self._config.displacement_multiplier,
        )
        self._fvg_tracker = FVGTracker(
            mode=self._config.structure_mode,
            min_gap_pct=self._config.fvg_min_gap_pct,
            require_displacement=self._config.fvg_require_displacement,
            atr_period=self._config.atr_period,
            displacement_multiplier=self._config.displacement_multiplier,
        )
        self._ob_tracker = OrderBlockTracker(
            mode=self._config.structure_mode,
            ob_lookback=self._config.ob_lookback,
            require_fvg=self._config.ob_require_fvg,
        )
        self._liquidity_tracker = LiquidityTracker(
            tolerance_pct=self._config.liquidity_tolerance_pct,
            mode=self._config.structure_mode,
        )
        self._session_filter = SessionFilter(
            sessions=self._config.sessions,
            default_timezone=self._config.session_timezone,
        )

        # Seed HTF events: deduplicate, validate, and store immutable defensive copy (P1.1)
        normalized_htf_events = _normalize_and_validate_htf_events(htf_events)
        self._initial_htf_events: tuple[StructureEvent, ...] = tuple(copy.deepcopy(list(normalized_htf_events)))
        self._htf_tracker = HTFBiasTracker(
            conflict_policy=self._config.htf_conflict_policy,
            htf_events=list(copy.deepcopy(self._initial_htf_events)),
        )

        # Seed HTF POI tracker with initial POIs and initial BOS events
        self._initial_htf_pois = tuple(copy.deepcopy(list(htf_pois))) if htf_pois else ()
        self._htf_poi_tracker = HTFPOITracker(initial_pois=list(self._initial_htf_pois))
        for ev in self._initial_htf_events:
            if ev.event_type == "BOS":
                self._htf_poi_tracker.register_poi_from_event(ev)

        # Monotonicity & ATR Tracking
        self._last_bar_index: Optional[int] = None
        self._last_timestamp: Optional[pd.Timestamp] = None
        self._last_close: Optional[float] = None
        self._tr_history: deque[float] = deque(maxlen=self._config.atr_period)
        self._total_bars_seen: int = 0
        self._finite_atr_count: int = 0

        # Idempotence cache for duplicate calls (O(1) memory)
        self._last_context: Optional[StrategyContext] = None
        self._last_raw_candle: Optional[dict[str, Any]] = None
        self._last_raw_events: Optional[tuple[dict[str, Any], ...]] = None

        # Bounded historical buffers to maintain strict O(1) performance
        max_bound_swings = self._config.max_recent_swings * 2
        max_bound_structures = self._config.max_recent_structures * 2
        max_bound_sweeps = self._config.max_recent_sweeps * 2

        self._recent_swings_buffer: list[SwingPoint] = []
        self._recent_structures_buffer: list[StructureEvent] = []
        self._recent_sweeps_buffer: list[LiquiditySweep] = []

        # Snapshot caches keyed by identity -> (state_key, snapshot) (zero id() usage, bounded memory)
        self._swing_snapshot_cache: dict[tuple, tuple[tuple, SwingPointSnapshot]] = {}
        self._structure_snapshot_cache: dict[tuple, tuple[tuple, StructureEventSnapshot]] = {}
        self._fvg_snapshot_cache: dict[tuple, tuple[tuple, FairValueGapSnapshot]] = {}
        self._ob_snapshot_cache: dict[tuple, tuple[tuple, OrderBlockSnapshot]] = {}
        self._pool_snapshot_cache: dict[tuple, tuple[tuple, LiquidityPoolSnapshot]] = {}
        self._sweep_snapshot_cache: dict[tuple, tuple[tuple, LiquiditySweepSnapshot]] = {}

    def reset(self) -> None:
        """Resets all internal trackers and state buffers to clean initial state."""
        self._swing_snapshot_cache.clear()
        self._structure_snapshot_cache.clear()
        self._fvg_snapshot_cache.clear()
        self._ob_snapshot_cache.clear()
        self._pool_snapshot_cache.clear()
        self._sweep_snapshot_cache.clear()
        self._swing_detector = SwingDetectorState(
            strength=self._config.swing_strength,
            left_strength=self._config.swing_left_strength,
            right_strength=self._config.swing_right_strength,
            mode=self._config.structure_mode,
        )
        self._structure_tracker = StructureTracker(
            mode=self._config.structure_mode,
            atr_period=self._config.atr_period,
            displacement_multiplier=self._config.displacement_multiplier,
        )
        self._fvg_tracker = FVGTracker(
            mode=self._config.structure_mode,
            min_gap_pct=self._config.fvg_min_gap_pct,
            require_displacement=self._config.fvg_require_displacement,
            atr_period=self._config.atr_period,
            displacement_multiplier=self._config.displacement_multiplier,
        )
        self._ob_tracker = OrderBlockTracker(
            mode=self._config.structure_mode,
            ob_lookback=self._config.ob_lookback,
            require_fvg=self._config.ob_require_fvg,
        )
        self._liquidity_tracker = LiquidityTracker(
            tolerance_pct=self._config.liquidity_tolerance_pct,
            mode=self._config.structure_mode,
        )
        self._session_filter = SessionFilter(
            sessions=self._config.sessions,
            default_timezone=self._config.session_timezone,
        )
        # Re-seed HTF tracker with immutable initial events, discarding any dynamic events (P1.1)
        self._htf_tracker = HTFBiasTracker(
            conflict_policy=self._config.htf_conflict_policy,
            htf_events=list(copy.deepcopy(self._initial_htf_events)),
        )
        self._htf_poi_tracker = HTFPOITracker(initial_pois=list(self._initial_htf_pois))
        for ev in self._initial_htf_events:
            if ev.event_type == "BOS":
                self._htf_poi_tracker.register_poi_from_event(ev)

        self._last_bar_index = None
        self._last_timestamp = None
        self._last_close = None
        self._tr_history.clear()
        self._total_bars_seen = 0
        self._finite_atr_count = 0
        self._last_context = None
        self._last_raw_candle = None
        self._last_raw_events = None
        self._recent_swings_buffer.clear()
        self._recent_structures_buffer.clear()
        self._recent_sweeps_buffer.clear()

    def add_htf_event(self, event: StructureEvent) -> bool:
        """Adds a Higher Timeframe (HTF) structure event into the HTF tracker."""
        return self._htf_tracker.add_event(event)

    @property
    def last_context(self) -> Optional[StrategyContext]:
        """Returns the most recent StrategyContext produced, if any."""
        return self._last_context

    @property
    def snapshot_cache_sizes(self) -> dict[str, int]:
        """Returns the current size of all 6 snapshot caches for memory diagnostics."""
        return {
            "swings": len(self._swing_snapshot_cache),
            "structures": len(self._structure_snapshot_cache),
            "fvgs": len(self._fvg_snapshot_cache),
            "obs": len(self._ob_snapshot_cache),
            "order_blocks": len(self._ob_snapshot_cache),
            "pools": len(self._pool_snapshot_cache),
            "sweeps": len(self._sweep_snapshot_cache),
        }

    def update(
        self,
        candle: Union[Mapping[str, Any], pd.Series],
        *,
        candle_closed: Optional[Any] = None,
        new_htf_events: Optional[List[StructureEvent]] = None,
        new_htf_pois: Optional[Sequence[Union[HTFPOI, dict[str, Any]]]] = None,
    ) -> StrategyContext:
        """
        Processes a newly closed candle at bar N and returns a deep-immutable StrategyContext.
        
        Args:
            candle: Dictionary or Series containing bar_index, time/timestamp, open, high, low, close, volume.
            candle_closed: Explicit closed state parameter (must strictly resolve to True).
            new_htf_events: Optional newly emitted HTF StructureEvents as-of this bar.
            
        Returns:
            StrategyContext representing the state as-of bar N.
            
        Raises:
            ValueError: If candle is not closed, non-monotonic, geometry violation, or future state detected.
            KeyError: If required candle fields are missing.
        """
        # Step 1: Closed-candle guard
        is_closed = _resolve_closed_state(candle, candle_closed)
        if not is_closed:
            raise ValueError("StrategyContextBuilder only accepts closed candles (got closed=False).")

        # Extract and validate candle fields
        c_dict = candle.to_dict() if isinstance(candle, pd.Series) else dict(candle)
        if isinstance(candle, pd.Series) and "time" not in c_dict and isinstance(candle.name, (pd.Timestamp, datetime.datetime, str)):
            c_dict["time"] = candle.name

        for req in ("bar_index", "open", "high", "low", "close"):
            if req not in c_dict:
                if req == "bar_index" and "index" in c_dict:
                    c_dict["bar_index"] = c_dict["index"]
                else:
                    raise KeyError(f"Candle missing required field '{req}'.")

        bar_idx = _validate_non_negative_int(c_dict["bar_index"], "bar_index")

        raw_ts = c_dict.get("time", c_dict.get("timestamp"))
        if raw_ts is None:
            raise KeyError("Candle missing timestamp ('time' or 'timestamp').")
        ts = _parse_timezone_aware_timestamp(raw_ts, name="candle timestamp")

        c_open = _validate_finite_float(c_dict["open"], "open", min_val=0.00001)
        c_high = _validate_finite_float(c_dict["high"], "high", min_val=0.00001)
        c_low = _validate_finite_float(c_dict["low"], "low", min_val=0.00001)
        c_close = _validate_finite_float(c_dict["close"], "close", min_val=0.00001)
        c_volume = _validate_finite_float(c_dict.get("volume", c_dict.get("tick_volume", 0.0)), "volume", min_val=0.0)

        # Geometry checks
        if c_high < c_low:
            raise ValueError(f"High {c_high} cannot be less than Low {c_low}")
        if c_high < max(c_open, c_close):
            raise ValueError(f"High {c_high} must be >= max(Open {c_open}, Close {c_close})")
        if c_low > min(c_open, c_close):
            raise ValueError(f"Low {c_low} must be <= min(Open {c_open}, Close {c_close})")

        raw_payload = {
            "bar_index": bar_idx,
            "time": ts,
            "open": round(c_open, 3),
            "high": round(c_high, 3),
            "low": round(c_low, 3),
            "close": round(c_close, 3),
            "volume": float(c_volume),
        }

        # Step 2: Monotonicity & Idempotence checks (P1.2, P2.1)
        normalized_events = _normalize_and_validate_htf_events(new_htf_events)
        events_payload = tuple(ev.to_dict() for ev in normalized_events)

        if self._last_bar_index is not None:
            if bar_idx < self._last_bar_index:
                raise ValueError(
                    f"Non-monotonic bar update: bar_index {bar_idx} < last {self._last_bar_index}."
                )
            if bar_idx > self._last_bar_index:
                if ts <= self._last_timestamp:
                    raise ValueError(
                        f"Non-monotonic timestamp: bar_index {bar_idx} > last {self._last_bar_index} "
                        f"but timestamp {ts} <= last {self._last_timestamp}."
                    )
            else:
                # bar_idx == self._last_bar_index: Candidate duplicate bar update
                if ts != self._last_timestamp:
                    raise ValueError(
                        f"Conflicting duplicate update at bar_index {bar_idx}: "
                        f"timestamp {ts} != existing {self._last_timestamp}."
                    )
                if self._last_raw_candle != raw_payload:
                    raise ValueError(
                        f"Conflicting duplicate update at bar_index {bar_idx} with differing candle payload:\n"
                        f"Existing: {self._last_raw_candle}\n"
                        f"Incoming: {raw_payload}"
                    )
                if self._last_raw_events != events_payload:
                    raise ValueError(
                        f"Conflicting duplicate update at bar_index {bar_idx}: "
                        f"duplicate bar provided with differing new_htf_events."
                    )
                assert self._last_context is not None
                return self._last_context

        if normalized_events:
            for ev in normalized_events:
                self.add_htf_event(ev)

        # Step 3: Rolling True Range & ATR14
        if self._last_close is None:
            tr = c_high - c_low
        else:
            tr = max(c_high - c_low, abs(c_high - self._last_close), abs(c_low - self._last_close))
        self._last_close = c_close
        self._tr_history.append(tr)
        self._total_bars_seen += 1

        if len(self._tr_history) == self._config.atr_period:
            atr14 = sum(self._tr_history) / float(self._config.atr_period)
            self._finite_atr_count += 1
        else:
            atr14 = 0.0

        bar_close_time = ts + SUPPORTED_TIMEFRAMES[self._config.timeframe]

        # Canonical Event Ordering at Bar N:
        # 1. Update Swings
        candle_tracker_input = {
            "bar_index": bar_idx,
            "time": ts,
            "open": c_open,
            "high": c_high,
            "low": c_low,
            "close": c_close,
            "volume": c_volume,
        }
        newly_confirmed_swings = self._swing_detector.update(candle_tracker_input)
        if newly_confirmed_swings:
            self._recent_swings_buffer.extend(newly_confirmed_swings)
            max_sw = self._config.max_recent_swings * 2
            if len(self._recent_swings_buffer) > max_sw:
                self._recent_swings_buffer = self._recent_swings_buffer[-max_sw:]

        # 2. Update Structure
        newly_emitted_structures = self._structure_tracker.update(
            candle_tracker_input,
            confirmed_swings=newly_confirmed_swings,
        )
        if newly_emitted_structures:
            self._recent_structures_buffer.extend(newly_emitted_structures)
            max_st = self._config.max_recent_structures * 2
            if len(self._recent_structures_buffer) > max_st:
                self._recent_structures_buffer = self._recent_structures_buffer[-max_st:]

        # 3. Update FVG (Capture active state before update to accurately retain FVGs filled on this bar)
        fvgs_before = list(self._fvg_tracker.get_active_fvgs())
        newly_confirmed_fvgs = self._fvg_tracker.update(
            candle_tracker_input,
            structure_events=newly_emitted_structures,
        )

        # 4. Update Order Block (Pass only newly confirmed FVGs to avoid O(N^2) duplicate explosion)
        self._ob_tracker.update(
            candle_tracker_input,
            new_structure_events=newly_emitted_structures,
            new_fvgs=newly_confirmed_fvgs,
        )

        # 5. Update Liquidity Pools & Sweeps
        self._liquidity_tracker.update(
            candle_tracker_input,
            newly_confirmed_swings=newly_confirmed_swings,
            atr_val=atr14,
        )
        sweeps_at_bar = self._liquidity_tracker.get_sweeps_at_bar(bar_idx)
        if sweeps_at_bar:
            self._recent_sweeps_buffer.extend(sweeps_at_bar)
            max_swp = self._config.max_recent_sweeps * 2
            if len(self._recent_sweeps_buffer) > max_swp:
                self._recent_sweeps_buffer = self._recent_sweeps_buffer[-max_swp:]

        # 6. Session Decision
        session_decision = self._session_filter.update(
            candle_tracker_input,
            candle_closed=True,
        )

        # 7. HTF Bias
        htf_bias = self._htf_tracker.update(current_ltf_time=bar_close_time)

        # Step 8: Build and Normalize As-of Evidence Collections (O(1) bounded)
        # (a) Swings as-of bar N: confirmed_at <= bar_idx
        eligible_swings = [s for s in self._recent_swings_buffer if s.confirmed_at <= bar_idx]
        if len(eligible_swings) > self._config.max_recent_swings:
            eligible_swings = eligible_swings[-self._config.max_recent_swings:]

        # Map broken swings from structure events confirmed as-of bar N
        broken_swings_map = {
            st.broken_swing_index: st.index
            for st in self._recent_structures_buffer
            if st.index <= bar_idx and st.broken_swing_index is not None
        }

        as_of_swings: list[SwingPointSnapshot] = []
        for s in eligible_swings:
            is_broken = bool(s.broken and s.broken_at is not None and s.broken_at <= bar_idx)
            b_at = s.broken_at if is_broken else None
            if not is_broken and s.index in broken_swings_map:
                is_broken = True
                b_at = broken_swings_map[s.index]
            # Stable semantic identity and state keys (zero id() usage, 100% field coverage)
            id_key = _swing_identity_key(s)
            state_key = _swing_state_key(s, is_broken=is_broken, b_at=b_at)
            cached = self._swing_snapshot_cache.get(id_key)
            if cached is not None and cached[0] == state_key:
                sw_snap = cached[1]
            else:
                sw_snap = SwingPointSnapshot(
                    index=s.index,
                    time=s.time,
                    price=s.price,
                    kind=s.kind,
                    strength=s.strength,
                    confirmed_at=s.confirmed_at,
                    confirmed_time=s.confirmed_time,
                    mode=s.mode,
                    classification=s.classification,
                    broken=is_broken,
                    broken_at=b_at,
                )
                validate_as_of_evidence(sw_snap, bar_idx, bar_close_time)
                self._swing_snapshot_cache[id_key] = (state_key, sw_snap)
            as_of_swings.append(sw_snap)

        dedup_swings = _deduplicate_and_sort_evidence(
            as_of_swings,
            id_func=_swing_identity_key,
            sort_key_func=lambda sw: (sw.confirmed_at, sw.index, sw.kind, sw.price),
        )

        # (b) Structures as-of bar N: index <= bar_idx
        eligible_structures = [st for st in self._recent_structures_buffer if st.index <= bar_idx]
        if len(eligible_structures) > self._config.max_recent_structures:
            eligible_structures = eligible_structures[-self._config.max_recent_structures:]

        as_of_structures: list[StructureEventSnapshot] = []
        for st in eligible_structures:
            id_key = _structure_identity_key(st)
            state_key = _structure_state_key(st)
            cached = self._structure_snapshot_cache.get(id_key)
            if cached is not None and cached[0] == state_key:
                st_snap = cached[1]
            else:
                st_snap = StructureEventSnapshot.from_source(st)
                validate_as_of_evidence(st_snap, bar_idx, bar_close_time)
                self._structure_snapshot_cache[id_key] = (state_key, st_snap)
            as_of_structures.append(st_snap)

        dedup_structures = _deduplicate_and_sort_evidence(
            as_of_structures,
            id_func=_structure_identity_key,
            sort_key_func=lambda s: (s.index, s.mode, s.event_type, s.direction, s.broken_swing_index),
        )

        # (c) Active FVGs as-of bar N:
        # confirmed_at <= bar_idx AND (filled_at is None OR filled_at == bar_idx)
        active_fvg_candidates = list(self._fvg_tracker.get_active_fvgs())
        for f in fvgs_before:
            if f.filled_at == bar_idx and f not in active_fvg_candidates:
                active_fvg_candidates.append(f)

        eligible_fvgs = [
            f for f in active_fvg_candidates
            if f.confirmed_at <= bar_idx and (f.filled_at is None or f.filled_at == bar_idx)
        ]
        if len(eligible_fvgs) > self._config.max_active_fvgs:
            eligible_fvgs = eligible_fvgs[-self._config.max_active_fvgs:]

        as_of_fvgs: list[FairValueGapSnapshot] = []
        for f in eligible_fvgs:
            id_key = _fvg_identity_key(f)
            state_key = _fvg_state_key(f)
            cached = self._fvg_snapshot_cache.get(id_key)
            if cached is not None and cached[0] == state_key:
                f_snap = cached[1]
            else:
                validate_as_of_evidence(f, bar_idx, bar_close_time)
                f_snap = FairValueGapSnapshot.from_source(f)
                self._fvg_snapshot_cache[id_key] = (state_key, f_snap)
            as_of_fvgs.append(f_snap)

        dedup_fvgs = _deduplicate_and_sort_evidence(
            as_of_fvgs,
            id_func=_fvg_identity_key,
            sort_key_func=lambda f: (f.confirmed_at, f.index, f.direction, f.top, f.bottom),
        )

        # (d) Active OBs as-of bar N:
        # created_at <= bar_idx AND valid == True AND invalidated_at is None
        eligible_obs = [
            ob for ob in self._ob_tracker.get_active_blocks()
            if (ob.created_at if ob.created_at != -1 else ob.source_event_index) <= bar_idx and ob.valid and ob.invalidated_at is None
        ]
        if len(eligible_obs) > self._config.max_active_obs:
            eligible_obs = eligible_obs[-self._config.max_active_obs:]

        as_of_obs: list[OrderBlockSnapshot] = []
        for ob in eligible_obs:
            created_at_val = ob.created_at if ob.created_at != -1 else ob.source_event_index
            id_key = _ob_identity_key(ob)
            state_key = _ob_state_key(ob, created_at_val=created_at_val)
            cached = self._ob_snapshot_cache.get(id_key)
            if cached is not None and cached[0] == state_key:
                ob_snap = cached[1]
            else:
                validate_as_of_evidence(ob, bar_idx, bar_close_time)
                ob_snap = OrderBlockSnapshot.from_source(ob)
                self._ob_snapshot_cache[id_key] = (state_key, ob_snap)
            as_of_obs.append(ob_snap)

        dedup_obs = _deduplicate_and_sort_evidence(
            as_of_obs,
            id_func=_ob_identity_key,
            sort_key_func=lambda o: (o.created_at, o.index, o.direction, o.high, o.low),
        )

        # (e) Active Pools as-of bar N:
        # confirmed_at <= bar_idx AND valid == True AND not swept
        eligible_pools = [
            p for p in self._liquidity_tracker._iter_active_pools_internal()
            if p.confirmed_at <= bar_idx and p.valid and not p.swept
        ]
        if len(eligible_pools) > self._config.max_active_pools:
            eligible_pools = eligible_pools[-self._config.max_active_pools:]

        as_of_pools: list[LiquidityPoolSnapshot] = []
        for p in eligible_pools:
            id_key = _pool_identity_key(p)
            state_key = _pool_state_key(p)
            cached = self._pool_snapshot_cache.get(id_key)
            if cached is not None and cached[0] == state_key:
                p_snap = cached[1]
            else:
                validate_as_of_evidence(p, bar_idx, bar_close_time)
                p_snap = LiquidityPoolSnapshot.from_source(p)
                self._pool_snapshot_cache[id_key] = (state_key, p_snap)
            as_of_pools.append(p_snap)

        dedup_pools = _deduplicate_and_sort_evidence(
            as_of_pools,
            id_func=_pool_identity_key,
            sort_key_func=lambda p: (p.confirmed_at, p.kind, p.price, p.indices),
        )

        # (f) Recent Sweeps as-of bar N:
        # confirmed_at <= bar_idx AND swept_at <= bar_idx
        eligible_sweeps = [
            sw for sw in self._recent_sweeps_buffer
            if sw.confirmed_at <= bar_idx and sw.swept_at <= bar_idx
        ]
        if len(eligible_sweeps) > self._config.max_recent_sweeps:
            eligible_sweeps = eligible_sweeps[-self._config.max_recent_sweeps:]

        as_of_sweeps: list[LiquiditySweepSnapshot] = []
        for sw in eligible_sweeps:
            id_key = _sweep_identity_key(sw)
            state_key = _sweep_state_key(sw)
            cached = self._sweep_snapshot_cache.get(id_key)
            if cached is not None and cached[0] == state_key:
                sw_snap = cached[1]
            else:
                validate_as_of_evidence(sw, bar_idx, bar_close_time)
                sw_snap = LiquiditySweepSnapshot.from_source(sw)
                self._sweep_snapshot_cache[id_key] = (state_key, sw_snap)
            as_of_sweeps.append(sw_snap)

        dedup_sweeps = _deduplicate_and_sort_evidence(
            as_of_sweeps,
            id_func=_sweep_identity_key,
            sort_key_func=lambda s: (s.confirmed_at, s.index, s.direction, s.pool_kind, s.price_wick),
        )

        # Periodic / threshold pruning of ALL 6 snapshot caches to ensure strictly bounded O(1) memory
        max_bound_swings = self._config.max_recent_swings * 2
        if len(self._swing_snapshot_cache) > max_bound_swings:
            active_sw_ids = {_swing_identity_key(s) for s in eligible_swings}
            self._swing_snapshot_cache = {k: v for k, v in self._swing_snapshot_cache.items() if k in active_sw_ids}

        max_bound_structures = self._config.max_recent_structures * 2
        if len(self._structure_snapshot_cache) > max_bound_structures:
            active_st_ids = {_structure_identity_key(st) for st in self._recent_structures_buffer}
            self._structure_snapshot_cache = {k: v for k, v in self._structure_snapshot_cache.items() if k in active_st_ids}

        max_bound_sweeps = self._config.max_recent_sweeps * 2
        if len(self._sweep_snapshot_cache) > max_bound_sweeps:
            active_swp_ids = {_sweep_identity_key(sw) for sw in self._recent_sweeps_buffer}
            self._sweep_snapshot_cache = {k: v for k, v in self._sweep_snapshot_cache.items() if k in active_swp_ids}

        max_bound_fvgs = self._config.max_active_fvgs * 2
        if len(self._fvg_snapshot_cache) > max_bound_fvgs:
            active_fvg_ids = {_fvg_identity_key(f) for f in eligible_fvgs}
            self._fvg_snapshot_cache = {k: v for k, v in self._fvg_snapshot_cache.items() if k in active_fvg_ids}

        max_bound_obs = self._config.max_active_obs * 2
        if len(self._ob_snapshot_cache) > max_bound_obs:
            active_ob_ids = {_ob_identity_key(ob) for ob in eligible_obs}
            self._ob_snapshot_cache = {k: v for k, v in self._ob_snapshot_cache.items() if k in active_ob_ids}

        max_bound_pools = self._config.max_active_pools * 2
        if len(self._pool_snapshot_cache) > max_bound_pools:
            active_pool_ids = {_pool_identity_key(p) for p in eligible_pools}
            self._pool_snapshot_cache = {k: v for k, v in self._pool_snapshot_cache.items() if k in active_pool_ids}

        # Update HTF POI tracker
        if new_htf_events:
            for ev in new_htf_events:
                if ev.event_type == "BOS":
                    self._htf_poi_tracker.register_poi_from_event(ev)
        if new_htf_pois:
            for p in new_htf_pois:
                self._htf_poi_tracker.add_poi(p)

        current_bias_str = htf_bias.bias if htf_bias is not None else None
        active_htf_pois = self._htf_poi_tracker.update(c_dict, current_bias=current_bias_str)

        # Validate Context States
        if session_decision is not None:
            validate_as_of_evidence(session_decision, bar_idx, bar_close_time)
        if htf_bias is not None:
            validate_as_of_evidence(htf_bias, bar_idx, bar_close_time)

        # Warmup and metadata (pre-frozen MappingProxyType)
        meta = MappingProxyType({
            "warmup": MappingProxyType({
                "close_count": int(self._total_bars_seen),
                "finite_atr14_count": int(self._finite_atr_count),
                "tr": round(float(tr), 3),
            })
        })

        context = StrategyContext(
            bar_index=bar_idx,
            timestamp=ts,
            bar_close_time=bar_close_time,
            symbol=self._config.symbol,
            timeframe=self._config.timeframe,
            open=c_open,
            high=c_high,
            low=c_low,
            close=c_close,
            volume=c_volume,
            atr14=round(float(atr14), 3),
            recent_swings=dedup_swings,
            recent_structures=dedup_structures,
            active_fvgs=dedup_fvgs,
            active_obs=dedup_obs,
            active_pools=dedup_pools,
            recent_sweeps=dedup_sweeps,
            session_decision=session_decision,
            htf_bias=htf_bias,
            active_htf_pois=active_htf_pois,
            meta=meta,
        )

        # Update cache & tracking state (O(1) memory)
        self._last_bar_index = bar_idx
        self._last_timestamp = ts
        self._last_context = context
        self._last_raw_candle = raw_payload
        self._last_raw_events = events_payload

        return context


def build_strategy_contexts(
    data: Union[pd.DataFrame, Sequence[Mapping[str, Any]]],
    config: Optional[ContextBuilderConfig] = None,
    *,
    candle_closed: Optional[Any] = None,
    htf_events: Optional[List[StructureEvent]] = None,
    htf_pois: Optional[Sequence[Union[HTFPOI, dict[str, Any]]]] = None,
) -> Tuple[StrategyContext, ...]:
    """
    Builds a sequence of StrategyContext objects from a DataFrame or list of candle records.
    Guarantees 100% exact parity with incremental bar-by-bar execution.
    
    Args:
        data: DataFrame or sequence of candle dicts.
        config: Optional ContextBuilderConfig.
        candle_closed: Optional candle closed state parameter.
        htf_events: Optional list of HTF StructureEvents.
        htf_pois: Optional list of HTF POIs.
        
    Returns:
        Tuple of StrategyContext objects, one for each closed candle.
    """
    if isinstance(data, pd.DataFrame):
        df = normalize_ohlcv(data) if "bar_index" not in data.columns else data
        records = df.to_dict(orient="records")
    else:
        records = list(data)

    if not records:
        return ()

    builder = StrategyContextBuilder(config=config, htf_events=htf_events, htf_pois=htf_pois)
    contexts: list[StrategyContext] = []

    for row in records:
        ctx = builder.update(row, candle_closed=candle_closed)
        contexts.append(ctx)

    return tuple(contexts)
