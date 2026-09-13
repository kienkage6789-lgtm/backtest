"""
smc/context/htf_bias.py
=======================
Higher Timeframe (HTF) Bias Adapter Module for Smart Money Concepts (SMC).

Provides zero-lookahead, state-machine tracking of HTF structure events
(BOS, CHoCH) mapped as-of Lower Timeframe (LTF) candles.

Core State Machine Rules:
-------------------------
1. neutral -> BOS (bullish/bearish) -> initial_bos_confirmed
2. neutral -> CHoCH -> remains neutral (no_confirmed_bias)
3. bullish -> BOS bullish -> continuation_bos (or reversal_cancelled_by_continuation if pending)
4. bullish -> CHoCH bearish -> choch_reversal_pending (bias stays bullish)
5. bullish -> CHoCH bearish -> BOS bearish (CHoCH.time < BOS.time <= bar_close) -> reversal_bos_confirmed (bias -> bearish)
6. bullish -> BOS bearish without preceding CHoCH -> cannot reverse (bias stays bullish)
7. bearish rules are fully symmetric.
8. Conflicting events at identical timestamp -> neutral (conflicting_events).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
import datetime
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Set, Union

import pandas as pd

from smc.models import BiasState, Signal, StructureEvent


def _get_effective_time(event: StructureEvent) -> pd.Timestamp:
    """
    Extracts the confirmation / effective timestamp of a StructureEvent.
    Prioritizes confirmed_time if present; otherwise uses event.time.
    Validates that the timestamp is timezone-aware.
    """
    raw_time = getattr(event, "confirmed_time", None) or event.time
    if not isinstance(raw_time, pd.Timestamp):
        raw_time = pd.Timestamp(raw_time)

    if raw_time.tzinfo is None:
        raise ValueError(f"HTF event time must be timezone-aware: {raw_time}")

    return raw_time.tz_convert("UTC")


def _parse_timezone_aware_timestamp(
    ts: Union[pd.Timestamp, datetime.datetime, str],
    name: str = "timestamp",
) -> pd.Timestamp:
    """Parses a timestamp and ensures it is timezone-aware in UTC."""
    if not isinstance(ts, pd.Timestamp):
        ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware: {ts}")
    return ts.tz_convert("UTC")


def _validate_and_sort_htf_events(events: List[StructureEvent]) -> List[StructureEvent]:
    """
    Validates, deduplicates, and deterministically sorts HTF structure events.
    Sort key: (effective_time, index, event_type, direction).
    """
    seen_keys: Set[tuple] = set()
    unique_events: List[StructureEvent] = []

    for ev in events:
        eff_time = _get_effective_time(ev)
        key = (eff_time, int(ev.index), str(ev.event_type), str(ev.direction))
        if key not in seen_keys:
            seen_keys.add(key)
            unique_events.append(ev)

    unique_events.sort(key=lambda e: (_get_effective_time(e), int(e.index), str(e.event_type), str(e.direction)))
    return unique_events


@dataclass
class _BiasStateMachine:
    """
    State machine tracking confirmed bias, pending reversal status,
    and transitions adhering to SMC reversal semantics.
    """
    current_bias: str = "neutral"
    confirmed_bos_event: Optional[StructureEvent] = None
    pending_reversal_direction: Optional[str] = None
    pending_reversal_event_index: Optional[int] = None
    pending_reversal_event_time: Optional[pd.Timestamp] = None
    pending_reversal_event_type: Optional[str] = None
    last_reason: str = "no_htf_event"
    last_rep_event: Optional[StructureEvent] = None
    last_conflict_meta: Optional[Dict[str, Any]] = None

    def reset(self) -> None:
        self.current_bias = "neutral"
        self.confirmed_bos_event = None
        self.pending_reversal_direction = None
        self.pending_reversal_event_index = None
        self.pending_reversal_event_time = None
        self.pending_reversal_event_type = None
        self.last_reason = "no_htf_event"
        self.last_rep_event = None
        self.last_conflict_meta = None

    def apply_cluster(self, cluster: List[StructureEvent], eff_time: pd.Timestamp) -> None:
        """Applies a cluster of events occurring at the exact same timestamp."""
        if not cluster:
            return

        self.last_rep_event = cluster[-1]
        directions = set(e.direction for e in cluster)
        if len(directions) > 1:
            # Rule 6: conflicting events at the exact same timestamp
            self.current_bias = "neutral"
            self.pending_reversal_direction = None
            self.pending_reversal_event_index = None
            self.pending_reversal_event_time = None
            self.pending_reversal_event_type = None
            self.confirmed_bos_event = None
            self.last_reason = "conflicting_events"
            self.last_conflict_meta = {
                "conflicting_count": len(cluster),
                "directions": tuple(sorted(directions)),
            }
            return

        self.last_conflict_meta = None
        for ev in cluster:
            self._apply_single_event(ev, eff_time)

    def _apply_single_event(self, ev: StructureEvent, eff_time: pd.Timestamp) -> None:
        ev_type = str(ev.event_type).upper()
        ev_dir = str(ev.direction).lower()

        if self.current_bias == "neutral":
            if ev_type == "BOS":
                self.current_bias = ev_dir
                self.confirmed_bos_event = ev
                self.pending_reversal_direction = None
                self.pending_reversal_event_index = None
                self.pending_reversal_event_time = None
                self.pending_reversal_event_type = None
                self.last_reason = "initial_bos_confirmed"
            elif ev_type in {"CHOCH", "CHoCH"}:
                # Rule 2: CHoCH bullish/bearish does not establish bias
                self.current_bias = "neutral"
                self.pending_reversal_direction = None
                self.pending_reversal_event_index = None
                self.pending_reversal_event_time = None
                self.pending_reversal_event_type = None
                self.last_reason = "no_confirmed_bias"

        elif self.current_bias == "bullish":
            if ev_dir == "bullish":
                if ev_type == "BOS":
                    self.current_bias = "bullish"
                    self.confirmed_bos_event = ev
                    if self.pending_reversal_direction is not None:
                        # Rule 5: Continuation BOS cancels pending reversal
                        self.pending_reversal_direction = None
                        self.pending_reversal_event_index = None
                        self.pending_reversal_event_time = None
                        self.pending_reversal_event_type = None
                        self.last_reason = "reversal_cancelled_by_continuation"
                    else:
                        # Rule 3: Continuation BOS confirms trend continuation
                        self.last_reason = "continuation_bos"
                elif ev_type in {"CHOCH", "CHoCH"}:
                    # Bullish CHoCH when already bullish: does not change state
                    pass

            elif ev_dir == "bearish":
                if ev_type in {"CHOCH", "CHoCH"}:
                    # Rule 3: Bearish CHoCH creates pending reversal
                    self.pending_reversal_direction = "bearish"
                    self.pending_reversal_event_index = int(ev.index)
                    self.pending_reversal_event_time = eff_time
                    self.pending_reversal_event_type = "CHoCH"
                    self.last_reason = "choch_reversal_pending"
                elif ev_type == "BOS":
                    # Rule 3 & 6: Bearish BOS confirms reversal if preceded by bearish CHoCH
                    if (
                        self.pending_reversal_direction == "bearish"
                        and self.pending_reversal_event_time is not None
                        and self.pending_reversal_event_time < eff_time
                    ):
                        self.current_bias = "bearish"
                        self.confirmed_bos_event = ev
                        self.pending_reversal_direction = None
                        self.pending_reversal_event_index = None
                        self.pending_reversal_event_time = None
                        self.pending_reversal_event_type = None
                        self.last_reason = "reversal_bos_confirmed"
                    else:
                        # BOS bearish without preceding CHoCH: bias stays bullish
                        pass

        elif self.current_bias == "bearish":
            if ev_dir == "bearish":
                if ev_type == "BOS":
                    self.current_bias = "bearish"
                    self.confirmed_bos_event = ev
                    if self.pending_reversal_direction is not None:
                        self.pending_reversal_direction = None
                        self.pending_reversal_event_index = None
                        self.pending_reversal_event_time = None
                        self.pending_reversal_event_type = None
                        self.last_reason = "reversal_cancelled_by_continuation"
                    else:
                        self.last_reason = "continuation_bos"
                elif ev_type in {"CHOCH", "CHoCH"}:
                    pass

            elif ev_dir == "bullish":
                if ev_type in {"CHOCH", "CHoCH"}:
                    # Rule 4: Bullish CHoCH creates pending reversal
                    self.pending_reversal_direction = "bullish"
                    self.pending_reversal_event_index = int(ev.index)
                    self.pending_reversal_event_time = eff_time
                    self.pending_reversal_event_type = "CHoCH"
                    self.last_reason = "choch_reversal_pending"
                elif ev_type == "BOS":
                    if (
                        self.pending_reversal_direction == "bullish"
                        and self.pending_reversal_event_time is not None
                        and self.pending_reversal_event_time < eff_time
                    ):
                        self.current_bias = "bullish"
                        self.confirmed_bos_event = ev
                        self.pending_reversal_direction = None
                        self.pending_reversal_event_index = None
                        self.pending_reversal_event_time = None
                        self.pending_reversal_event_type = None
                        self.last_reason = "reversal_bos_confirmed"
                    else:
                        pass

    def build_bias_state(self, as_of: pd.Timestamp, is_late: bool = False) -> BiasState:
        confirmed_by_bos = (self.confirmed_bos_event is not None and self.current_bias != "neutral")

        if confirmed_by_bos and self.confirmed_bos_event is not None:
            source_idx = int(self.confirmed_bos_event.index)
            source_time = _get_effective_time(self.confirmed_bos_event)
            source_type = str(self.confirmed_bos_event.event_type)
            source_dir = str(self.confirmed_bos_event.direction)
        elif self.last_reason == "conflicting_events" and self.last_rep_event is not None:
            source_idx = int(self.last_rep_event.index)
            source_time = _get_effective_time(self.last_rep_event)
            source_type = str(self.last_rep_event.event_type)
            source_dir = str(self.last_rep_event.direction)
        else:
            source_idx = None
            source_time = None
            source_type = None
            source_dir = None

        meta_dict: Dict[str, Any] = {
            "bias": self.current_bias,
            "reason": self.last_reason,
            "source_event_type": source_type,
            "source_event_index": source_idx,
            "source_event_time": source_time.isoformat() if source_time else None,
            "pending_reversal": self.pending_reversal_direction,
            "pending_reversal_event_type": self.pending_reversal_event_type,
            "pending_reversal_event_index": self.pending_reversal_event_index,
            "pending_reversal_event_time": self.pending_reversal_event_time.isoformat() if self.pending_reversal_event_time else None,
            "confirmed_by_bos": confirmed_by_bos,
            "effective_time": source_time.isoformat() if source_time else None,
        }
        if is_late:
            meta_dict["late_event"] = True
        if self.last_conflict_meta:
            meta_dict.update(self.last_conflict_meta)

        return BiasState(
            bias=self.current_bias,  # type: ignore
            timestamp=as_of,
            source_event_index=source_idx,
            source_event_time=source_time,
            source_event_type=source_type,
            source_event_direction=source_dir,
            as_of=as_of,
            reason=self.last_reason,
            pending_reversal=self.pending_reversal_direction,
            pending_reversal_event_type=self.pending_reversal_event_type,
            pending_reversal_event_index=self.pending_reversal_event_index,
            pending_reversal_event_time=self.pending_reversal_event_time,
            confirmed_by_bos=confirmed_by_bos,
            meta=MappingProxyType(meta_dict),
        )


def _evaluate_state_machine_over_events(
    events: List[StructureEvent],
    as_of: pd.Timestamp,
    is_late: bool = False,
) -> BiasState:
    """Pure evaluation of sorted events through the state machine."""
    if not events:
        return BiasState(
            bias="neutral",
            timestamp=as_of,
            source_event_index=None,
            source_event_time=None,
            source_event_type=None,
            source_event_direction=None,
            as_of=as_of,
            reason="no_htf_event",
            pending_reversal=None,
            pending_reversal_event_type=None,
            pending_reversal_event_index=None,
            pending_reversal_event_time=None,
            confirmed_by_bos=False,
            meta=MappingProxyType({
                "bias": "neutral",
                "reason": "no_htf_event",
                "source_event_type": None,
                "source_event_index": None,
                "source_event_time": None,
                "pending_reversal": None,
                "pending_reversal_event_type": None,
                "pending_reversal_event_index": None,
                "pending_reversal_event_time": None,
                "confirmed_by_bos": False,
                "effective_time": None,
            }),
        )

    sm = _BiasStateMachine()

    # Cluster events by effective_time
    i = 0
    n = len(events)
    while i < n:
        eff_t = _get_effective_time(events[i])
        j = i
        while j < n and _get_effective_time(events[j]) == eff_t:
            j += 1
        cluster = events[i:j]
        sm.apply_cluster(cluster, eff_t)
        i = j

    return sm.build_bias_state(as_of, is_late=is_late)


def get_htf_bias_at(
    timestamp: Union[pd.Timestamp, datetime.datetime, str],
    htf_events: List[StructureEvent],
    cutoff_time: Optional[Union[pd.Timestamp, datetime.datetime, str]] = None,
    conflict_policy: str = "neutral",
) -> BiasState:
    """
    Returns the Higher Timeframe (HTF) bias as-of a specific LTF timestamp.

    Rules:
    - Only events with effective_time <= min(timestamp, cutoff_time) are considered.
    - Zero future lookahead.
    - Uses state machine reversal confirmation: CHoCH warns -> BOS confirms.
    - conflict_policy only supports 'neutral'. If conflicting events exist at the latest timestamp, returns 'neutral'.
    - If no events are confirmed <= timestamp, returns 'neutral' with reason 'no_htf_event'.
    """
    if conflict_policy != "neutral":
        raise ValueError(f"Invalid conflict_policy '{conflict_policy}'. Currently only 'neutral' is supported.")

    ltf_ts = _parse_timezone_aware_timestamp(timestamp, name="LTF timestamp")
    cutoff_ts = _parse_timezone_aware_timestamp(cutoff_time, name="cutoff_time") if cutoff_time is not None else None

    sorted_events = _validate_and_sort_htf_events(htf_events)
    max_eligible_time = ltf_ts if cutoff_ts is None else min(ltf_ts, cutoff_ts)

    # Filter events confirmed at or before max_eligible_time
    eligible = [e for e in sorted_events if _get_effective_time(e) <= max_eligible_time]
    return _evaluate_state_machine_over_events(eligible, as_of=ltf_ts)


def map_htf_bias_to_ltf(
    ltf_data: Union[pd.DataFrame, List[Dict[str, Any]], List[pd.Timestamp]],
    htf_events: List[StructureEvent],
    cutoff_time: Optional[Union[pd.Timestamp, datetime.datetime, str]] = None,
    conflict_policy: str = "neutral",
) -> List[BiasState]:
    """
    Maps HTF structure bias to a sequence of LTF candles (as-of timestamp mapping).

    Maintains 100% parity with HTFBiasTracker and get_htf_bias_at.
    """
    if conflict_policy != "neutral":
        raise ValueError(f"Invalid conflict_policy '{conflict_policy}'. Currently only 'neutral' is supported.")

    cutoff_ts = _parse_timezone_aware_timestamp(cutoff_time, name="cutoff_time") if cutoff_time is not None else None
    sorted_events = _validate_and_sort_htf_events(htf_events)
    eff_times = [_get_effective_time(e) for e in sorted_events]

    # Extract LTF timestamps
    ltf_timestamps: List[pd.Timestamp] = []
    if isinstance(ltf_data, pd.DataFrame):
        if "time" in ltf_data.columns:
            raw_times = ltf_data["time"].tolist()
        else:
            raw_times = ltf_data.index.tolist()
        for t in raw_times:
            ltf_timestamps.append(_parse_timezone_aware_timestamp(t, name="DataFrame LTF time"))
    elif isinstance(ltf_data, (list, tuple)):
        for item in ltf_data:
            if isinstance(item, dict):
                t = item.get("time", item.get("timestamp"))
                if t is None:
                    raise ValueError(f"Dict has no 'time' or 'timestamp' key: {item}")
            else:
                t = item
            ltf_timestamps.append(_parse_timezone_aware_timestamp(t, name="LTF time"))
    else:
        raise ValueError(f"Unsupported ltf_data type: {type(ltf_data)}")

    results: List[BiasState] = []
    sm = _BiasStateMachine()
    last_applied_idx = 0

    for ltf_ts in ltf_timestamps:
        max_time = ltf_ts if cutoff_ts is None else min(ltf_ts, cutoff_ts)
        target_idx = bisect.bisect_right(eff_times, max_time)

        if target_idx == 0:
            results.append(BiasState(
                bias="neutral",
                timestamp=ltf_ts,
                source_event_index=None,
                source_event_time=None,
                source_event_type=None,
                source_event_direction=None,
                as_of=ltf_ts,
                reason="no_htf_event",
                pending_reversal=None,
                pending_reversal_event_type=None,
                pending_reversal_event_index=None,
                pending_reversal_event_time=None,
                confirmed_by_bos=False,
                meta=MappingProxyType({
                    "bias": "neutral",
                    "reason": "no_htf_event",
                    "source_event_type": None,
                    "source_event_index": None,
                    "source_event_time": None,
                    "pending_reversal": None,
                    "pending_reversal_event_type": None,
                    "pending_reversal_event_index": None,
                    "pending_reversal_event_time": None,
                    "confirmed_by_bos": False,
                    "effective_time": None,
                }),
            ))
            continue

        # Advance state machine cluster by cluster from last_applied_idx to target_idx
        while last_applied_idx < target_idx:
            eff_t = eff_times[last_applied_idx]
            cluster_end = last_applied_idx
            while cluster_end < target_idx and eff_times[cluster_end] == eff_t:
                cluster_end += 1
            cluster = sorted_events[last_applied_idx:cluster_end]
            sm.apply_cluster(cluster, eff_t)
            last_applied_idx = cluster_end

        results.append(sm.build_bias_state(ltf_ts))

    return results


class HTFBiasTracker:
    """
    Incremental stateful tracker evaluating HTF bias as LTF candles arrive in streaming fashion.
    """
    def __init__(
        self,
        conflict_policy: str = "neutral",
        cutoff_time: Optional[Union[pd.Timestamp, datetime.datetime, str]] = None,
        htf_events: Optional[List[StructureEvent]] = None,
    ):
        if conflict_policy != "neutral":
            raise ValueError(f"Invalid conflict_policy '{conflict_policy}'. Currently only 'neutral' is supported.")
        self.conflict_policy = conflict_policy
        self.cutoff_time = _parse_timezone_aware_timestamp(cutoff_time, name="cutoff_time") if cutoff_time is not None else None

        self._known_events: List[StructureEvent] = []
        self._known_keys: Set[tuple] = set()
        self._known_sort_keys: List[tuple] = []
        self._eff_times: List[pd.Timestamp] = []
        self._late_event_keys: Set[tuple] = set()

        self._state_machine = _BiasStateMachine()
        self._applied_event_count: int = 0
        self._has_unprocessed_late_events: bool = False

        self._last_ltf_time: Optional[pd.Timestamp] = None
        self._last_bias: Optional[BiasState] = None
        self._history: List[BiasState] = []

        if htf_events:
            for ev in htf_events:
                self.add_event(ev)

    @property
    def current_bias(self) -> str:
        return self._state_machine.current_bias

    @property
    def pending_reversal_direction(self) -> Optional[str]:
        return self._state_machine.pending_reversal_direction

    @property
    def pending_reversal_event_index(self) -> Optional[int]:
        return self._state_machine.pending_reversal_event_index

    @property
    def pending_reversal_event_time(self) -> Optional[pd.Timestamp]:
        return self._state_machine.pending_reversal_event_time

    @property
    def pending_reversal_event_type(self) -> Optional[str]:
        return self._state_machine.pending_reversal_event_type

    @property
    def confirmed_bos_event(self) -> Optional[StructureEvent]:
        return self._state_machine.confirmed_bos_event

    @property
    def last_reason(self) -> str:
        return self._state_machine.last_reason

    def add_event(self, htf_event: StructureEvent) -> bool:
        """
        Adds an HTF structure event to tracker memory.
        Returns True if newly added, False if duplicate.
        """
        eff_time = _get_effective_time(htf_event)
        key = (eff_time, int(htf_event.index), str(htf_event.event_type), str(htf_event.direction))
        if key in self._known_keys:
            return False

        self._known_keys.add(key)

        # Check if late event relative to last emitted LTF time
        if self._last_ltf_time is not None and eff_time < self._last_ltf_time:
            self._late_event_keys.add(key)
            self._has_unprocessed_late_events = True

        # Insert sorted by key
        insert_idx = bisect.bisect_right(self._known_sort_keys, key)
        self._known_sort_keys.insert(insert_idx, key)
        self._known_events.insert(insert_idx, htf_event)
        self._eff_times.insert(insert_idx, eff_time)

        if insert_idx < self._applied_event_count:
            self._has_unprocessed_late_events = True

        return True

    def update(
        self,
        htf_event: Optional[StructureEvent] = None,
        current_ltf_time: Optional[Union[pd.Timestamp, datetime.datetime, str]] = None,
    ) -> Optional[BiasState]:
        """
        Updates the tracker.
        - If htf_event is provided: ingests the event into memory.
        - If current_ltf_time is None: returns None (no LTF candle evaluated).
        - If current_ltf_time is provided: calculates bias as-of current_ltf_time.
          Events with effective_time > current_ltf_time are queued but ignored for current calculation.
        """
        if htf_event is not None:
            self.add_event(htf_event)

        if current_ltf_time is None:
            return None

        ltf_ts = _parse_timezone_aware_timestamp(current_ltf_time, name="current_ltf_time")
        if self._last_ltf_time is not None and ltf_ts < self._last_ltf_time:
            raise ValueError(
                f"Non-monotonic LTF timestamp: current_ltf_time ({ltf_ts}) is earlier than previous timestamp ({self._last_ltf_time})."
            )
        self._last_ltf_time = ltf_ts

        max_time = ltf_ts if self.cutoff_time is None else min(ltf_ts, self.cutoff_time)

        # Find eligible events using bisect_right over self._eff_times
        target_idx = bisect.bisect_right(self._eff_times, max_time)
        if target_idx == 0:
            bias_state = BiasState(
                bias="neutral",
                timestamp=ltf_ts,
                source_event_index=None,
                source_event_time=None,
                source_event_type=None,
                source_event_direction=None,
                as_of=ltf_ts,
                reason="no_htf_event",
                pending_reversal=None,
                pending_reversal_event_type=None,
                pending_reversal_event_index=None,
                pending_reversal_event_time=None,
                confirmed_by_bos=False,
                meta=MappingProxyType({
                    "bias": "neutral",
                    "reason": "no_htf_event",
                    "source_event_type": None,
                    "source_event_index": None,
                    "source_event_time": None,
                    "pending_reversal": None,
                    "pending_reversal_event_type": None,
                    "pending_reversal_event_index": None,
                    "pending_reversal_event_time": None,
                    "confirmed_by_bos": False,
                    "effective_time": None,
                }),
            )
            self._last_bias = bias_state
            self._history.append(bias_state)
            return bias_state

        # Check if late event requires full replay of state machine
        if self._has_unprocessed_late_events:
            self._state_machine.reset()
            self._applied_event_count = 0
            self._has_unprocessed_late_events = False

        # Advance state machine cluster by cluster
        while self._applied_event_count < target_idx:
            eff_t = self._eff_times[self._applied_event_count]
            cluster_end = self._applied_event_count
            while cluster_end < target_idx and self._eff_times[cluster_end] == eff_t:
                cluster_end += 1
            cluster = self._known_events[self._applied_event_count:cluster_end]
            self._state_machine.apply_cluster(cluster, eff_t)
            self._applied_event_count = cluster_end

        # Check late event flag for metadata
        first_at_latest = bisect.bisect_left(self._eff_times, self._eff_times[target_idx - 1])
        latest_cluster = self._known_events[first_at_latest:target_idx]
        latest_cluster_keys = [
            (_get_effective_time(e), int(e.index), str(e.event_type), str(e.direction))
            for e in latest_cluster
        ]
        is_late = any(k in self._late_event_keys for k in latest_cluster_keys)

        bias_state = self._state_machine.build_bias_state(ltf_ts, is_late=is_late)
        self._last_bias = bias_state
        self._history.append(bias_state)
        return bias_state

    def check_signal(
        self,
        htf_event: Optional[StructureEvent] = None,
        current_ltf_time: Optional[Union[pd.Timestamp, datetime.datetime, str]] = None,
        target_direction: Optional[str] = None,
    ) -> Optional[Signal]:
        """
        Generates an SMC Signal for HTF bias.
        - If target_direction is provided ('bullish' or 'bearish'):
          Signal name is 'htf_bias_aligned', value is True if bias matches target_direction.
        - If target_direction is None:
          Signal name is 'htf_bias_{bias}', value is True if bias is not neutral.
        """
        state = self.update(htf_event=htf_event, current_ltf_time=current_ltf_time)
        if state is None:
            return None

        meta_dict = dict(state.meta)
        meta_dict["bias"] = state.bias
        meta_dict["source_event_index"] = state.source_event_index
        meta_dict["source_event_time"] = state.source_event_time.isoformat() if state.source_event_time else None
        meta_dict["reason"] = state.reason
        meta_dict["pending_reversal"] = state.pending_reversal
        meta_dict["confirmed_by_bos"] = state.confirmed_by_bos

        if target_direction is not None:
            aligned = (state.bias == target_direction)
            return Signal(
                name="htf_bias_aligned",
                value=aligned,
                weight=1.5,
                confidence=1.0 if aligned else 0.0,
                meta=meta_dict,
            )

        return Signal(
            name=f"htf_bias_{state.bias}",
            value=(state.bias != "neutral"),
            weight=1.5,
            confidence=1.0 if state.bias != "neutral" else 0.0,
            meta=meta_dict,
        )

    def get_last_bias(self) -> Optional[BiasState]:
        return self._last_bias

    def get_history(self) -> List[BiasState]:
        return list(self._history)

    def reset(self) -> None:
        """Resets streaming state and history, retaining configured known HTF events."""
        self._state_machine.reset()
        self._applied_event_count = 0
        self._has_unprocessed_late_events = False
        self._last_ltf_time = None
        self._last_bias = None
        self._history.clear()
        self._late_event_keys.clear()

    def clear_events(self) -> None:
        """Clears all stored HTF events and resets tracker state."""
        self._known_events.clear()
        self._known_keys.clear()
        self._known_sort_keys.clear()
        self._eff_times.clear()
        self._late_event_keys.clear()
        self._state_machine.reset()
        self._applied_event_count = 0
        self._has_unprocessed_late_events = False
        self._last_ltf_time = None
        self._last_bias = None
        self._history.clear()
