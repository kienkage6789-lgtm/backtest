"""
smc/context/session.py
======================
KillZone and Session Filter Module for Smart Money Concepts (SMC).

Provides zero-lookahead, timezone-aware, DST-resilient trading session
evaluation for London, New York, Asian, and custom/overnight sessions.
"""

import datetime
import math
from typing import Optional, List, Dict, Any, Union, Tuple
from types import MappingProxyType
import zoneinfo
import numpy as np
import pandas as pd

from smc.models import SessionWindow, SessionDecision, Signal


# Default Standard Kill Zones
LONDON_KILLZONE = SessionWindow(
    name="london_killzone",
    start=datetime.time(7, 0),
    end=datetime.time(10, 0),
    timezone="UTC",
)

NEWYORK_KILLZONE = SessionWindow(
    name="newyork_killzone",
    start=datetime.time(12, 0),
    end=datetime.time(15, 0),
    timezone="UTC",
)

ASIAN_RANGE = SessionWindow(
    name="asian_range",
    start=datetime.time(0, 0),
    end=datetime.time(6, 0),
    timezone="UTC",
)

DEFAULT_SESSIONS: Tuple[SessionWindow, ...] = (LONDON_KILLZONE, NEWYORK_KILLZONE)


def _parse_bool_strict(val: Any, field_name: str = "closed") -> bool:
    """
    Strictly validates and parses a boolean state for candle closed status.
    - True boolean: bool, np.bool_ -> returns bool
    - Integers: 1 -> True, 0 -> False (other integers raise ValueError)
    - Floats: 1.0 -> True, 0.0 -> False (NaN/Inf or other floats raise ValueError)
    - Strings: 'true', '1', 't', 'yes' (case-insensitive) -> True
               'false', '0', 'f', 'no' (case-insensitive) -> False
               (all other strings raise ValueError)
    - None / pd.NA / np.nan: raise ValueError
    - Other types: raise ValueError
    """
    if isinstance(val, (bool, np.bool_)):
        return bool(val)
    if isinstance(val, (int, np.integer)):
        if val == 1:
            return True
        if val == 0:
            return False
        raise ValueError(f"Invalid integer value for {field_name}: {val}. Only 0 or 1 are accepted.")
    if isinstance(val, (float, np.floating)):
        if math.isnan(val) or pd.isna(val):
            raise ValueError(f"Invalid NaN/missing value for {field_name}.")
        if val == 1.0:
            return True
        if val == 0.0:
            return False
        raise ValueError(f"Invalid float value for {field_name}: {val}. Only 0.0 or 1.0 are accepted.")
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "1", "t", "yes", "y"):
            return True
        if v in ("false", "0", "f", "no", "n"):
            return False
        raise ValueError(f"Cannot parse boolean string for {field_name}: '{val}'. Expected 'true'/'false' or '1'/'0'.")
    if val is None or pd.isna(val):
        raise ValueError(f"Invalid null/missing value for {field_name}.")
    raise ValueError(f"Unsupported type {type(val).__name__} for {field_name}: {val}")


def _resolve_closed_state(candle_or_data: Any, candle_closed_param: Optional[Any]) -> bool:
    """
    Enforces deterministic precedence rules for closed status:
    1. If both 'closed' and 'is_closed' are present: raise ValueError.
    2. If exactly one is present:
       - Use that value (strictly parsed).
       - If candle_closed_param is also provided and conflicts: raise ValueError.
    3. If neither is present:
       - If candle_closed_param is provided: use that value (strictly parsed).
       - If candle_closed_param is None: raise ValueError.
    """
    has_closed = False
    closed_val: Optional[bool] = None
    has_is_closed = False
    is_closed_val: Optional[bool] = None

    if isinstance(candle_or_data, dict):
        if "closed" in candle_or_data:
            has_closed = True
            closed_val = _parse_bool_strict(candle_or_data["closed"], "closed")
        if "is_closed" in candle_or_data:
            has_is_closed = True
            is_closed_val = _parse_bool_strict(candle_or_data["is_closed"], "is_closed")
    elif hasattr(candle_or_data, "__getitem__") and not isinstance(candle_or_data, (str, bytes, pd.Timestamp, datetime.datetime)):
        # pd.Series or mapping-like
        try:
            if "closed" in candle_or_data:
                raw_c = candle_or_data["closed"]
                has_closed = True
                closed_val = _parse_bool_strict(raw_c, "closed")
        except (KeyError, TypeError):
            pass
        try:
            if "is_closed" in candle_or_data:
                raw_ic = candle_or_data["is_closed"]
                has_is_closed = True
                is_closed_val = _parse_bool_strict(raw_ic, "is_closed")
        except (KeyError, TypeError):
            pass

    if has_closed and has_is_closed:
        raise ValueError("Conflicting closed flags: both 'closed' and 'is_closed' are present in data.")

    parsed_param: Optional[bool] = None
    if candle_closed_param is not None:
        parsed_param = _parse_bool_strict(candle_closed_param, "candle_closed")

    if has_closed or has_is_closed:
        flag_val = closed_val if has_closed else is_closed_val
        if parsed_param is not None and parsed_param != flag_val:
            raise ValueError(
                f"Conflicting candle_closed parameter ({parsed_param}) with closed flag in data ({flag_val})."
            )
        return flag_val

    # Neither present in data
    if parsed_param is not None:
        return parsed_param

    raise ValueError(
        "Candle closed state is missing: neither 'closed'/'is_closed' in data nor 'candle_closed' parameter specified."
    )


def _extract_timestamp(item: Any) -> pd.Timestamp:
    """Extracts timestamp from pd.Timestamp, datetime, str, dict or Series."""
    if isinstance(item, pd.Timestamp):
        return item
    if isinstance(item, (datetime.datetime, str)):
        return pd.Timestamp(item)
    if isinstance(item, dict):
        for key in ("time", "timestamp", "date"):
            if key in item:
                return pd.Timestamp(item[key])
        raise ValueError(f"No timestamp field found in dict. Available keys: {list(item.keys())}")
    if hasattr(item, "name") and isinstance(item.name, (pd.Timestamp, datetime.datetime, str)):
        return pd.Timestamp(item.name)
    if hasattr(item, "__getitem__"):
        for key in ("time", "timestamp", "date"):
            try:
                return pd.Timestamp(item[key])
            except (KeyError, TypeError):
                pass
    raise ValueError(f"Cannot extract timestamp from {type(item)}: {item}")


def evaluate_session_window(
    timestamp: Union[pd.Timestamp, datetime.datetime, str],
    window: SessionWindow,
    candle_closed: Any,
    default_timezone: Optional[str] = None,
) -> SessionDecision:
    """
    Evaluates whether a single candle timestamp falls within a specific SessionWindow.

    Boundary rule: start <= local_time < end (start inclusive, end exclusive).
    Overnight rule (start > end): local_time >= start OR local_time < end.
    """
    parsed_closed = _parse_bool_strict(candle_closed, "candle_closed")

    raw_ts = _extract_timestamp(timestamp)

    # Handle naive timestamp
    if raw_ts.tzinfo is None:
        if default_timezone is None:
            return SessionDecision(
                in_session=False,
                session_name=window.name,
                timestamp=raw_ts,
                reason="naive_timestamp",
                meta=MappingProxyType({
                    "session_name": window.name,
                    "timezone": window.timezone,
                    "candle_closed": parsed_closed,
                    "reason": "naive_timestamp",
                }),
            )
        try:
            utc_ts = raw_ts.tz_localize(default_timezone).tz_convert("UTC")
        except Exception as e:
            return SessionDecision(
                in_session=False,
                session_name=window.name,
                timestamp=raw_ts,
                reason="invalid_timezone",
                meta=MappingProxyType({
                    "session_name": window.name,
                    "timezone": window.timezone,
                    "candle_closed": parsed_closed,
                    "reason": f"invalid_timezone: {e}",
                }),
            )
    else:
        if raw_ts.tzinfo == datetime.timezone.utc or str(raw_ts.tzinfo) == "UTC":
            utc_ts = raw_ts
        else:
            utc_ts = raw_ts.tz_convert("UTC")

    # Reject partial / open candle
    if not parsed_closed:
        return SessionDecision(
            in_session=False,
            session_name=window.name,
            timestamp=utc_ts,
            reason="partial_candle",
            meta=MappingProxyType({
                "session_name": window.name,
                "timezone": window.timezone,
                "utc_timestamp": utc_ts.isoformat(),
                "candle_closed": False,
                "reason": "partial_candle",
            }),
        )

    # Convert to window timezone
    if window.timezone == "UTC" or window.timezone == "Etc/UTC":
        local_dt = utc_ts
    else:
        try:
            local_dt = utc_ts.tz_convert(window.timezone)
        except Exception as e:
            return SessionDecision(
                in_session=False,
                session_name=window.name,
                timestamp=utc_ts,
                reason="invalid_timezone",
                meta=MappingProxyType({
                    "session_name": window.name,
                    "timezone": window.timezone,
                    "utc_timestamp": utc_ts.isoformat(),
                    "candle_closed": parsed_closed,
                    "reason": f"invalid_timezone: {e}",
                }),
            )

    local_time = local_dt.time()

    # Determine weekday attribution for overnight sessions
    is_overnight = window.start > window.end
    if is_overnight and local_time <= window.end:
        # Candle is in early morning up to session end: belongs to session cycle that started yesterday
        eval_weekday = (local_dt.weekday() - 1) % 7
    else:
        eval_weekday = local_dt.weekday()

    base_meta = {
        "session_name": window.name,
        "local_time": local_time.isoformat(),
        "local_timestamp": local_dt.isoformat(),
        "utc_timestamp": utc_ts.isoformat(),
        "timezone": window.timezone,
        "candle_closed": parsed_closed,
        "eval_weekday": eval_weekday,
    }

    # Check allowed weekdays
    if window.days is not None and eval_weekday not in window.days:
        base_meta["reason"] = "wrong_weekday"
        return SessionDecision(
            in_session=False,
            session_name=window.name,
            timestamp=utc_ts,
            reason="wrong_weekday",
            meta=MappingProxyType(base_meta),
        )

    # Zero-duration session
    if window.start == window.end:
        base_meta["reason"] = "outside_session"
        return SessionDecision(
            in_session=False,
            session_name=window.name,
            timestamp=utc_ts,
            reason="outside_session",
            meta=MappingProxyType(base_meta),
        )

    # Boundary checks
    if not is_overnight:
        # Normal session (start < end)
        if local_time < window.start:
            reason = "before_session_start"
            in_session = False
        elif local_time == window.start or (window.start < local_time < window.end):
            reason = "inside_session"
            in_session = True
        elif local_time == window.end:
            reason = "at_session_end"
            in_session = False
        else:
            reason = "outside_session"
            in_session = False
    else:
        # Overnight session (start > end)
        if local_time >= window.start or local_time < window.end:
            reason = "inside_session"
            in_session = True
        elif local_time == window.end:
            reason = "at_session_end"
            in_session = False
        else:
            reason = "outside_session"
            in_session = False

    base_meta["reason"] = reason
    return SessionDecision(
        in_session=in_session,
        session_name=window.name,
        timestamp=utc_ts,
        reason=reason,
        meta=MappingProxyType(base_meta),
    )


def evaluate_sessions(
    candle_or_timestamp: Any,
    sessions: Optional[Union[List[SessionWindow], Tuple[SessionWindow, ...]]] = None,
    candle_closed: Optional[bool] = None,
    default_timezone: Optional[str] = None,
) -> SessionDecision:
    """
    Evaluates a candle against a list of SessionWindows and returns a single aggregated decision.

    Rules:
    - Sessions are tested in deterministic order.
    - in_session is True if at least one session matches.
    - session_name is the first matching session.
    - meta['matched_sessions'] contains all matching session names.
    - If no session matches, diagnostic reject reason follows strict priority.
    """
    resolved_closed = _resolve_closed_state(candle_or_timestamp, candle_closed)
    raw_ts = _extract_timestamp(candle_or_timestamp)

    session_list = tuple(sessions) if sessions is not None else DEFAULT_SESSIONS
    if not session_list:
        utc_ts = raw_ts.tz_localize("UTC") if raw_ts.tzinfo is None else raw_ts.tz_convert("UTC")
        return SessionDecision(
            in_session=False,
            session_name=None,
            timestamp=utc_ts,
            reason="outside_session",
            meta=MappingProxyType({
                "matched_sessions": (),
                "reason": "outside_session",
                "candle_closed": resolved_closed,
            }),
        )

    decisions: List[SessionDecision] = []
    for window in session_list:
        d = evaluate_session_window(
            raw_ts,
            window=window,
            candle_closed=resolved_closed,
            default_timezone=default_timezone,
        )
        decisions.append(d)

    matches = [d for d in decisions if d.in_session]
    matched_names = tuple(m.session_name for m in matches if m.session_name is not None)

    if matches:
        first_match = matches[0]
        meta_dict = dict(first_match.meta)
        meta_dict["matched_sessions"] = matched_names
        meta_dict["all_decisions"] = tuple(d.to_dict() for d in decisions)
        return SessionDecision(
            in_session=True,
            session_name=first_match.session_name,
            timestamp=first_match.timestamp,
            reason="inside_session",
            meta=MappingProxyType(meta_dict),
        )

    # Determine priority reject reason
    reasons = [d.reason for d in decisions]
    priority_order = (
        "naive_timestamp",
        "invalid_timezone",
        "partial_candle",
        "wrong_weekday",
        "at_session_end",
        "before_session_start",
        "outside_session",
    )
    selected_reason = "outside_session"
    for r in priority_order:
        if r in reasons:
            selected_reason = r
            break

    rep_decision = next((d for d in decisions if d.reason == selected_reason), decisions[0])
    meta_dict = dict(rep_decision.meta)
    meta_dict["matched_sessions"] = ()
    meta_dict["all_decisions"] = tuple(d.to_dict() for d in decisions)
    meta_dict["reason"] = selected_reason

    return SessionDecision(
        in_session=False,
        session_name=None,
        timestamp=rep_decision.timestamp,
        reason=selected_reason,
        meta=MappingProxyType(meta_dict),
    )
def evaluate_sessions_batch(
    data: Union[pd.DataFrame, List[Dict[str, Any]], List[pd.Timestamp]],
    sessions: Optional[Union[List[SessionWindow], Tuple[SessionWindow, ...]]] = None,
    candle_closed: Optional[Any] = None,
    default_timezone: Optional[str] = None,
) -> List[SessionDecision]:
    """
    Evaluates a sequence of candles (DataFrame, list of dicts, or list of timestamps)
    against a session collection and returns one aggregated SessionDecision per candle.
    """
    session_list = tuple(sessions) if sessions is not None else DEFAULT_SESSIONS
    parsed_param = _parse_bool_strict(candle_closed, "candle_closed") if candle_closed is not None else None

    if isinstance(data, pd.DataFrame):
        has_closed = "closed" in data.columns
        has_is_closed = "is_closed" in data.columns
        if has_closed and has_is_closed:
            raise ValueError("Conflicting closed flags: both 'closed' and 'is_closed' are present in DataFrame columns.")

        if not has_closed and not has_is_closed and parsed_param is None:
            raise ValueError(
                "Candle closed state is missing: DataFrame has neither 'closed' nor 'is_closed' column and candle_closed parameter is None."
            )

        if has_closed:
            closed_col = data["closed"].to_numpy()
        elif has_is_closed:
            closed_col = data["is_closed"].to_numpy()
        else:
            closed_col = None

        if "time" in data.columns:
            times = data["time"].to_list()
        else:
            times = data.index.to_list()

        decisions: List[SessionDecision] = []
        if closed_col is not None:
            for t, c_closed in zip(times, closed_col):
                parsed_c = _parse_bool_strict(c_closed, "closed")
                if parsed_param is not None and parsed_param != parsed_c:
                    raise ValueError(
                        f"Conflicting candle_closed parameter ({parsed_param}) with closed flag in data ({parsed_c})."
                    )
                d = evaluate_sessions(
                    t,
                    sessions=session_list,
                    candle_closed=parsed_c,
                    default_timezone=default_timezone,
                )
                decisions.append(d)
        else:
            for t in times:
                d = evaluate_sessions(
                    t,
                    sessions=session_list,
                    candle_closed=parsed_param,
                    default_timezone=default_timezone,
                )
                decisions.append(d)
        return decisions

    if isinstance(data, (list, tuple)):
        decisions = []
        for item in data:
            d = evaluate_sessions(
                item,
                sessions=session_list,
                candle_closed=parsed_param,
                default_timezone=default_timezone,
            )
            decisions.append(d)
        return decisions

    raise ValueError(f"Unsupported data type for evaluate_sessions_batch: {type(data)}")


def check_killzone_signal(
    timestamp: Union[pd.Timestamp, datetime.datetime, str, Dict[str, Any], Any],
    sessions: Optional[Union[List[SessionWindow], Tuple[SessionWindow, ...]]] = None,
    candle_closed: Optional[Any] = None,
    default_timezone: Optional[str] = None,
) -> Signal:
    """
    Generates an SMC Signal ('in_killzone') based on SessionFilter decision.
    """
    decision = evaluate_sessions(
        timestamp,
        sessions=sessions,
        candle_closed=candle_closed,
        default_timezone=default_timezone,
    )

    signal_meta = dict(decision.meta)
    signal_meta["session_name"] = decision.session_name
    signal_meta["reason"] = decision.reason
    signal_meta["session_decision"] = decision.to_dict()

    return Signal(
        name="in_killzone",
        value=decision.in_session,
        weight=0.5,
        confidence=1.0 if decision.in_session else 0.0,
        meta=signal_meta,
    )


class SessionFilter:
    """
    Incremental stateful tracker evaluating trading sessions candle by candle.
    """
    def __init__(
        self,
        sessions: Optional[Union[List[SessionWindow], Tuple[SessionWindow, ...]]] = None,
        default_timezone: Optional[str] = None,
    ):
        self.sessions = tuple(sessions) if sessions is not None else DEFAULT_SESSIONS
        self.default_timezone = default_timezone
        self._last_timestamp: Optional[pd.Timestamp] = None
        self._last_decision: Optional[SessionDecision] = None
        self._history: List[SessionDecision] = []

    def update(
        self,
        candle_or_timestamp: Union[pd.Timestamp, datetime.datetime, str, Dict[str, Any], Any],
        candle_closed: Optional[Any] = None,
        default_timezone: Optional[str] = None,
    ) -> SessionDecision:
        """
        Updates the tracker with a new candle and returns the SessionDecision.
        """
        dtz = default_timezone or self.default_timezone
        decision = evaluate_sessions(
            candle_or_timestamp,
            sessions=self.sessions,
            candle_closed=candle_closed,
            default_timezone=dtz,
        )
        if decision.reason == "naive_timestamp":
            self._last_decision = decision
            self._history.append(decision)
            return decision

        if self._last_timestamp is not None:
            try:
                is_backward = decision.timestamp < self._last_timestamp
            except TypeError:
                raise ValueError(
                    f"Incompatible timestamp in SessionFilter: cannot compare timezone of {decision.timestamp} with previous {self._last_timestamp}."
                )
            if is_backward:
                raise ValueError(
                    f"Non-monotonic timestamp in SessionFilter: current ({decision.timestamp}) is earlier than previous ({self._last_timestamp})."
                )

        self._last_timestamp = decision.timestamp
        self._last_decision = decision
        self._history.append(decision)
        return decision

    def check_signal(
        self,
        candle_or_timestamp: Union[pd.Timestamp, datetime.datetime, str, Dict[str, Any], Any],
        candle_closed: Optional[bool] = None,
        default_timezone: Optional[str] = None,
    ) -> Signal:
        """
        Evaluates candle and returns a Signal object.
        """
        decision = self.update(
            candle_or_timestamp,
            candle_closed=candle_closed,
            default_timezone=default_timezone,
        )
        signal_meta = dict(decision.meta)
        signal_meta["session_name"] = decision.session_name
        signal_meta["reason"] = decision.reason

        return Signal(
            name="in_killzone",
            value=decision.in_session,
            weight=0.5,
            confidence=1.0 if decision.in_session else 0.0,
            meta=signal_meta,
        )

    def get_last_decision(self) -> Optional[SessionDecision]:
        return self._last_decision

    def get_history(self) -> List[SessionDecision]:
        return list(self._history)

    def reset(self) -> None:
        self._last_timestamp = None
        self._last_decision = None
        self._history.clear()
