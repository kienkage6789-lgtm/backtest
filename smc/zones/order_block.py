"""
smc/zones/order_block.py
========================
Order Block (OB) detection module.

Provides:
- detect_order_blocks(): Batch OB detection from StructureEvents + OHLCV data.
- OrderBlockTracker: Incremental O(1)-per-bar stateful tracker.

An Order Block is the last opposite-direction candle immediately before
a displacement leg that created a BOS or CHoCH event.
"""

import dataclasses
from collections import deque
import pandas as pd
import numpy as np
from typing import List, Optional, Dict, Any, Union, Set, Tuple

from smc.models import OrderBlock, StructureEvent, FairValueGap
from smc.data_contract import normalize_ohlcv
from smc.zones.fvg import detect_fvgs


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _apply_ob_state(ob: OrderBlock, candle: Dict[str, Any], was_in_zone: bool) -> bool:
    """Apply one closed candle to an OB; used for late-FVG backfill."""
    low, high, close = candle["low"], candle["high"], candle["close"]
    height = ob.high - ob.low
    if ob.direction == "bullish":
        in_zone = low < ob.high and high > ob.low
        if low < ob.high:
            pct = _clamp((ob.high - low) / height, 0.0, 1.0) if height > 0 else 0.0
            if pct > 0:
                if not ob.mitigated:
                    ob.mitigated, ob.mitigated_at = True, candle["bar_index"]
                ob.mitigation_pct = max(ob.mitigation_pct, pct)
                if not was_in_zone:
                    ob.retest_count += 1
        if close < ob.low:
            ob.valid = False
            ob.invalidated_at = candle["bar_index"]
            ob.invalidation_reason = "close_break"
    else:
        in_zone = high > ob.low and low < ob.high
        if high > ob.low:
            pct = _clamp((high - ob.low) / height, 0.0, 1.0) if height > 0 else 0.0
            if pct > 0:
                if not ob.mitigated:
                    ob.mitigated, ob.mitigated_at = True, candle["bar_index"]
                ob.mitigation_pct = max(ob.mitigation_pct, pct)
                if not was_in_zone:
                    ob.retest_count += 1
        if close > ob.high:
            ob.valid = False
            ob.invalidated_at = candle["bar_index"]
            ob.invalidation_reason = "close_break"
    return in_zone


def _reset_and_backfill_ob(ob: OrderBlock, candles, start_bar: int, end_bar: int) -> Tuple[bool, bool]:
    """Rebuild lifecycle state using only candles after availability. Returns (is_valid, was_in_zone)."""
    ob.mitigated = False
    ob.mitigated_at = None
    ob.mitigation_pct = 0.0
    ob.invalidated_at = None
    ob.invalidation_reason = None
    ob.valid = True
    ob.retest_count = 0
    was_in_zone = False
    for candle in candles:
        if start_bar < candle["bar_index"] <= end_bar:
            if not ob.valid:
                break
            was_in_zone = _apply_ob_state(ob, candle, was_in_zone)
    return ob.valid, was_in_zone


# ---------------------------------------------------------------------------
# Batch detection
# ---------------------------------------------------------------------------

def detect_order_blocks(
    data,
    structure_events,
    fvgs=None,
    mode: str = "swing",
    ob_lookback: int = 20,
    fvg_lookback: int = 5,
    require_fvg: bool = False,
    require_fvg_before_event: bool = True,
    mitigation_mode: str = "wick",
    zone_mode: str = "full_candle",
) -> List[OrderBlock]:
    """
    Detect Order Blocks from a full OHLCV dataset given StructureEvents.

    Args:
        data: Normalized or raw OHLCV DataFrame.
        structure_events: List[StructureEvent] — pre-detected BOS/CHoCH events.
        fvgs: Optional List[FairValueGap] for linking FVGs to OBs.
        mode: 'swing' or 'internal' — only process events with matching mode.
        ob_lookback: Max bars to look backwards for source candle.
        fvg_lookback: Max bars between OB and FVG to link them.
        require_fvg: If True, skip OBs that cannot be linked to a FVG.
        require_fvg_before_event: If True, FVG must be confirmed <= event.index.
        mitigation_mode: 'wick' (only supported mode).
        zone_mode: 'full_candle' (only supported mode).

    Returns:
        List[OrderBlock] sorted by index ascending.
    """
    # --- Validation ---
    if mode not in {"swing", "internal"}:
        raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")
    if ob_lookback <= 0:
        raise ValueError("ob_lookback must be > 0.")
    if fvg_lookback <= 0:
        raise ValueError("fvg_lookback must be > 0.")
    if mitigation_mode not in {"wick"}:
        raise ValueError(f"Invalid mitigation_mode '{mitigation_mode}'. Must be 'wick'.")
    if zone_mode not in {"full_candle"}:
        raise ValueError(f"Invalid zone_mode '{zone_mode}'. Must be 'full_candle'.")

    # Normalize data
    if isinstance(data, pd.DataFrame) and "bar_index" in data.columns:
        df = data
    else:
        df = normalize_ohlcv(data)

    if df.empty:
        return []

    # Clone inputs to avoid mutation
    events_clone = [dataclasses.replace(e) for e in structure_events]
    fvgs_clone   = [dataclasses.replace(f) for f in fvgs] if fvgs else []

    # Filter to matching mode events only
    events_filtered = [e for e in events_clone if e.mode == mode]
    events_filtered.sort(key=lambda e: e.index)

    # Build bar_index -> OHLC lookup for O(1) access
    bar_lookup: Dict[int, Dict[str, Any]] = {}
    for k in range(len(df)):
        bidx = int(df["bar_index"].iloc[k])
        bar_lookup[bidx] = {
            "bar_index": bidx,
            "time":  df.index[k],
            "open":  float(df["open"].iloc[k]),
            "high":  float(df["high"].iloc[k]),
            "low":   float(df["low"].iloc[k]),
            "close": float(df["close"].iloc[k]),
        }

    # Sorted bar indices list for scanning
    all_bar_indices = df["bar_index"].to_numpy().tolist()

    obs: List[OrderBlock] = []
    seen_keys: Set[Tuple[int, int]] = set()

    for event in events_filtered:
        # Determine target candle type:
        # bullish event => look for bearish candle (close < open)
        # bearish event => look for bullish candle (close > open)
        looking_for_bearish = (event.direction == "bullish")

        # Collect candidate bar indices before event.index
        candidate_bar_indices = [
            b for b in all_bar_indices if b < event.index
        ]
        # Sort descending (most recent first)
        candidate_bar_indices.sort(reverse=True)

        # Apply lookback window
        candidate_bar_indices = candidate_bar_indices[:ob_lookback]

        source_candle = None
        for b in candidate_bar_indices:
            bar = bar_lookup[b]
            if looking_for_bearish:
                if bar["close"] < bar["open"]:  # bearish candle
                    source_candle = bar
                    break
            else:
                if bar["close"] > bar["open"]:  # bullish candle
                    source_candle = bar
                    break

        if source_candle is None:
            continue

        ob_index = source_candle["bar_index"]
        dedup_key = (event.index, ob_index)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        # Direction of the OB (opposite to the source candle direction,
        # matching the structural event direction)
        ob_direction = event.direction  # bullish event => bullish OB

        ob = OrderBlock(
            index=ob_index,
            time=source_candle["time"],
            direction=ob_direction,
            high=source_candle["high"],
            low=source_candle["low"],
            open=source_candle["open"],
            close=source_candle["close"],
            origin_type=event.event_type,
            mode=mode,
            quality="base",
            source_event_index=event.index,
            source_event_type=event.event_type,
            source_swing_index=event.broken_swing_index,
            created_at=event.index,
            source_fvg_index=None,
            source_fvg_top=None,
            source_fvg_bottom=None,
            mitigated=False,
            mitigated_at=None,
            mitigation_pct=0.0,
            valid=True,
            invalidated_at=None,
            invalidation_reason=None,
            retest_count=0,
        )

        # --- Link FVG ---
        linked_fvg = _find_matching_fvg(
            ob=ob,
            fvgs=fvgs_clone,
            mode=mode,
            fvg_lookback=fvg_lookback,
            require_fvg_before_event=require_fvg_before_event,
            event_index=event.index,
            structure_events=events_filtered,
            structure_leg_id=event.structure_leg_id,
        )

        if require_fvg and linked_fvg is None:
            continue

        if linked_fvg is not None:
            ob.source_fvg_index  = linked_fvg.index
            ob.source_fvg_top    = linked_fvg.top
            ob.source_fvg_bottom = linked_fvg.bottom
            ob.structure_leg_id = event.structure_leg_id or linked_fvg.structure_leg_id
            ob.created_at = max(event.index, int(linked_fvg.confirmed_at))

        # --- Quality ---
        if linked_fvg is not None:
            if event.displacement:
                ob.quality = "premium_candidate"
            else:
                ob.quality = "strong"

        obs.append(ob)

    # --- Mitigation & Invalidation scan over bars AFTER the source event ---
    for ob in obs:
        # An Order Block only exists and can only be mitigated AFTER its triggering StructureEvent is confirmed
        start_cutoff = max(ob.index, ob.source_event_index, ob.created_at)
        was_in_zone = False

        for bar_idx in all_bar_indices:
            if bar_idx <= start_cutoff:
                continue
            if not ob.valid:
                break

            bar = bar_lookup[bar_idx]
            c_low   = bar["low"]
            c_high  = bar["high"]
            c_close = bar["close"]

            zone_height = ob.high - ob.low

            if ob.direction == "bullish":
                is_in_zone = (c_low < ob.high) and (c_high > ob.low)
                if c_low < ob.high:
                    penetration = ob.high - c_low
                    pct = _clamp(penetration / zone_height, 0.0, 1.0) if zone_height > 0 else 0.0
                    if pct > 0:
                        if not ob.mitigated:
                            ob.mitigated    = True
                            ob.mitigated_at = bar_idx
                            ob.mitigation_pct = pct
                        elif pct > ob.mitigation_pct:
                            ob.mitigation_pct = pct

                        if not was_in_zone:
                            ob.retest_count += 1
                else:
                    is_in_zone = False

                # Invalidation: close breaks below OB low
                if c_close < ob.low:
                    ob.valid              = False
                    ob.invalidated_at     = bar_idx
                    ob.invalidation_reason = "close_break"
                    break

            else:  # bearish
                is_in_zone = (c_high > ob.low) and (c_low < ob.high)
                if c_high > ob.low:
                    penetration = c_high - ob.low
                    pct = _clamp(penetration / zone_height, 0.0, 1.0) if zone_height > 0 else 0.0
                    if pct > 0:
                        if not ob.mitigated:
                            ob.mitigated    = True
                            ob.mitigated_at = bar_idx
                            ob.mitigation_pct = pct
                        elif pct > ob.mitigation_pct:
                            ob.mitigation_pct = pct

                        if not was_in_zone:
                            ob.retest_count += 1
                else:
                    is_in_zone = False

                # Invalidation: close breaks above OB high
                if c_close > ob.high:
                    ob.valid              = False
                    ob.invalidated_at     = bar_idx
                    ob.invalidation_reason = "close_break"
                    break

            was_in_zone = is_in_zone

    obs.sort(key=lambda o: o.index)
    return obs


def _find_matching_fvg(
    ob: OrderBlock,
    fvgs: List[FairValueGap],
    mode: str,
    fvg_lookback: int,
    require_fvg_before_event: bool,
    event_index: int,
    structure_events: Optional[List[StructureEvent]] = None,
    structure_leg_id: Optional[str] = None,
) -> Optional[FairValueGap]:
    """Find the first FVG that matches the OB criteria."""
    candidates = []
    for fvg in fvgs:
        if fvg.direction != ob.direction:
            continue
        if fvg.mode != mode:
            continue
        expected_leg = structure_leg_id or ob.structure_leg_id
        if (fvg.structure_leg_id is not None or expected_leg is not None) and fvg.structure_leg_id != expected_leg:
            continue
        if fvg.index <= ob.index:
            continue
        if require_fvg_before_event and fvg.confirmed_at > event_index:
            continue
        if fvg.filled and fvg.filled_at is not None and fvg.filled_at <= event_index:
            continue
        if (fvg.confirmed_at - ob.index) > fvg_lookback:
            continue
        # A displacement leg cannot cross an opposite-direction structure event.
        if structure_events and any(
            ev.index > ob.index and ev.index < event_index and ev.direction != ob.direction
            for ev in structure_events
        ):
            continue
        candidates.append(fvg)

    if not candidates:
        return None
    # First matching FVG (earliest by index)
    candidates.sort(key=lambda f: (f.index, f.confirmed_at))
    return candidates[0]


# ---------------------------------------------------------------------------
# Incremental tracker
# ---------------------------------------------------------------------------

class OrderBlockTracker:
    """
    Stateful incremental Order Block tracker for O(1)-per-bar streaming/replay.

    Feed candles one at a time along with any newly emitted StructureEvents
    and FVGs from the upstream trackers.
    """

    def __init__(
        self,
        mode: str = "swing",
        ob_lookback: int = 20,
        fvg_lookback: int = 5,
        require_fvg: bool = False,
        require_fvg_before_event: bool = True,
        mitigation_mode: str = "wick",
        zone_mode: str = "full_candle",
        max_active_blocks: int = 20,
        max_pending_delivery_lag: int = 200,
        max_pending_obs: int = 100,
    ) -> None:
        if mode not in {"swing", "internal"}:
            raise ValueError(f"Invalid mode '{mode}'. Must be 'swing' or 'internal'.")
        if ob_lookback <= 0:
            raise ValueError("ob_lookback must be > 0.")
        if fvg_lookback <= 0:
            raise ValueError("fvg_lookback must be > 0.")
        if mitigation_mode not in {"wick"}:
            raise ValueError(f"Invalid mitigation_mode '{mitigation_mode}'.")
        if zone_mode not in {"full_candle"}:
            raise ValueError(f"Invalid zone_mode '{zone_mode}'.")
        if max_active_blocks <= 0:
            raise ValueError("max_active_blocks must be > 0.")
        if max_pending_delivery_lag <= 0:
            raise ValueError("max_pending_delivery_lag must be > 0.")
        if max_pending_obs <= 0:
            raise ValueError("max_pending_obs must be > 0.")

        self.mode                    = mode
        self.ob_lookback             = ob_lookback
        self.fvg_lookback            = fvg_lookback
        self.require_fvg             = require_fvg
        self.require_fvg_before_event = require_fvg_before_event
        self.mitigation_mode         = mitigation_mode
        self.zone_mode               = zone_mode
        self.max_active_blocks       = max_active_blocks
        self.max_pending_delivery_lag = max_pending_delivery_lag
        self.max_pending_obs          = max_pending_obs

        # Candle history deque (bounded)
        self._candle_history: deque = deque(maxlen=ob_lookback + 5)

        # All OBs ever created (for idempotence / reporting)
        self._all_blocks: List[OrderBlock] = []

        # Currently active (valid) OBs
        self._active_blocks: List[OrderBlock] = []

        # Known FVGs (cloned from upstream)
        self._known_fvgs: List[FairValueGap] = []

        # Deduplication key set: (source_event_index, ob.index)
        self._seen_keys: Set[Tuple[int, int]] = set()
        self._pending_obs: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._known_structure_events: List[StructureEvent] = []
        self._deferred_fvgs: List[FairValueGap] = []

        # In-zone state for retest count tracking: (source_event_index, ob.index) -> bool
        self._in_zone_state: Dict[Tuple[int, int], bool] = {}

        self._last_processed_bar: int = -1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        candle,
        new_structure_events=None,
        new_fvgs=None,
    ) -> List[OrderBlock]:
        """
        Feed a new candle plus any newly emitted StructureEvents / FVGs.

        Returns list of newly created OrderBlocks at this bar.
        """
        c = self._extract_candle(candle)
        bar_idx = c["bar_index"]

        if bar_idx <= self._last_processed_bar:
            raise ValueError(
                f"bar_index {bar_idx} must be > last_processed_bar {self._last_processed_bar}.")
        self._last_processed_bar = bar_idx

        # Store candle in history
        self._candle_history.append(c)

        newly_created: List[OrderBlock] = []

        # Mirror upstream FVG lifecycle so a clone cannot remain unfilled.
        for known_fvg in self._known_fvgs:
            if known_fvg.filled or known_fvg.confirmed_at >= bar_idx:
                continue
            if (known_fvg.direction == "bullish" and c["low"] <= known_fvg.bottom) or \
               (known_fvg.direction == "bearish" and c["high"] >= known_fvg.top):
                known_fvg.filled = True
                known_fvg.filled_at = bar_idx

        # Enforce the delivery-lag contract before accepting any FVG delivered
        # on this bar.  Equality is still eligible; only strictly-late pending
        # entries are expired.
        for key, pending in list(self._pending_obs.items()):
            delivery_lag = bar_idx - pending["max_created_at"]
            if delivery_lag > self.max_pending_delivery_lag:
                del self._pending_obs[key]

        events_to_process = list(new_structure_events or [])

        # Ingest only confirmed FVGs; future-dated inputs are deferred.
        fvgs_to_ingest = self._deferred_fvgs + list(new_fvgs or [])
        self._deferred_fvgs = []
        if fvgs_to_ingest:
            seen_fvg_keys = set()
            for fvg in fvgs_to_ingest:
                fvg_key = (int(fvg.index), int(fvg.confirmed_at), str(fvg.direction), str(fvg.mode))
                if fvg_key in seen_fvg_keys:
                    continue
                seen_fvg_keys.add(fvg_key)
                if int(fvg.confirmed_at) > bar_idx:
                    self._deferred_fvgs.append(dataclasses.replace(fvg))
                    continue
                fvg_clone = dataclasses.replace(fvg)
                existing_fvg = next((idx for idx, known in enumerate(self._known_fvgs)
                                     if (known.index, known.confirmed_at, known.direction, known.mode) == fvg_key), None)
                if existing_fvg is None:
                    self._known_fvgs.append(fvg_clone)
                else:
                    self._known_fvgs[existing_fvg] = fvg_clone
                if fvg_clone.structure_leg_id is None:
                    matching_event = next(
                        (candidate for candidate in self._known_structure_events
                         if candidate.direction == fvg_clone.direction and
                         fvg_clone.index < candidate.index and
                         fvg_clone.confirmed_at <= candidate.index),
                        None,
                    )
                    if matching_event is not None:
                        fvg_clone.structure_leg_id = matching_event.structure_leg_id
                # Try to link with existing unlinked OBs
                for ob in self._all_blocks:
                    if ob.source_fvg_index is not None:
                        continue
                    linked = _find_matching_fvg(
                        ob=ob,
                        fvgs=[fvg_clone],
                        mode=self.mode,
                        fvg_lookback=self.fvg_lookback,
                        require_fvg_before_event=self.require_fvg_before_event,
                        event_index=ob.source_event_index,
                        structure_events=self._known_structure_events,
                        structure_leg_id=next((e.structure_leg_id for e in self._known_structure_events
                                               if e.index == ob.source_event_index), ob.structure_leg_id),
                    )
                    if linked is not None:
                        ob.source_fvg_index  = linked.index
                        ob.source_fvg_top    = linked.top
                        ob.source_fvg_bottom = linked.bottom
                        # Upgrade quality if needed
                        if ob.quality == "base":
                            source_event = next((e for e in self._known_structure_events
                                                 if e.index == ob.source_event_index), None)
                            ob.quality = "premium_candidate" if source_event and source_event.displacement else "strong"
                        ob.structure_leg_id = ob.structure_leg_id or fvg_clone.structure_leg_id
                        old_created_at = ob.created_at
                        ob.created_at = max(ob.created_at, int(fvg_clone.confirmed_at))
                        if ob.created_at != old_created_at:
                            is_valid, was_in_zone = _reset_and_backfill_ob(ob, self._candle_history, ob.created_at, bar_idx - 1)
                            ob_key = (ob.source_event_index, ob.index)
                            if is_valid:
                                self._in_zone_state[ob_key] = was_in_zone
                            else:
                                self._in_zone_state.pop(ob_key, None)

                # Keep an already-linked OB's availability/state aligned with
                # an upstream FVG clone that was later updated.
                for ob in self._all_blocks:
                    if ob.source_fvg_index == fvg_clone.index:
                        old_created_at = ob.created_at
                        ob.created_at = max(ob.created_at, int(fvg_clone.confirmed_at))
                        if ob.created_at != old_created_at:
                            is_valid, was_in_zone = _reset_and_backfill_ob(ob, self._candle_history, ob.created_at, bar_idx - 1)
                            ob_key = (ob.source_event_index, ob.index)
                            if is_valid:
                                self._in_zone_state[ob_key] = was_in_zone
                            else:
                                self._in_zone_state.pop(ob_key, None)

                # Try to link with candidate OBs waiting for FVG in _pending_obs
                for key, pending in list(self._pending_obs.items()):
                    ev = pending["event"]
                    candidate_ob = pending["ob"]
                    linked = _find_matching_fvg(
                        ob=candidate_ob,
                        fvgs=[fvg_clone],
                        mode=self.mode,
                        fvg_lookback=self.fvg_lookback,
                        require_fvg_before_event=self.require_fvg_before_event,
                        event_index=ev.index,
                        structure_events=self._known_structure_events,
                        structure_leg_id=ev.structure_leg_id or candidate_ob.structure_leg_id,
                    )
                    if linked is not None:
                        real_created_at = max(ev.index, int(linked.confirmed_at))
                        cutoff_states = pending["cutoff_states"]
                        cutoff_in_zone = pending["cutoff_in_zone"]

                        if real_created_at in cutoff_states:
                            final_ob = cutoff_states[real_created_at]
                            was_in_zone = cutoff_in_zone[real_created_at]
                        else:
                            final_ob = dataclasses.replace(candidate_ob, created_at=real_created_at)
                            is_valid, was_in_zone = _reset_and_backfill_ob(final_ob, self._candle_history, real_created_at, bar_idx - 1)

                        final_ob.source_fvg_index  = linked.index
                        final_ob.source_fvg_top    = linked.top
                        final_ob.source_fvg_bottom = linked.bottom
                        final_ob.quality = "premium_candidate" if ev.displacement else "strong"
                        final_ob.structure_leg_id = ev.structure_leg_id or linked.structure_leg_id

                        del self._pending_obs[key]
                        self._seen_keys.add(key)
                        self._all_blocks.append(final_ob)
                        if final_ob.valid:
                            self._in_zone_state[key] = was_in_zone
                            self._active_blocks.append(final_ob)
                            newly_created.append(final_ob)

        # Process new structure events
        if events_to_process:
            for event in events_to_process:
                if event.mode != self.mode:
                    continue

                ev = dataclasses.replace(event)
                if ev.index > bar_idx:
                    continue
                if not any(existing.index == ev.index and existing.direction == ev.direction for existing in self._known_structure_events):
                    self._known_structure_events.append(ev)
                for known_fvg in self._known_fvgs:
                    if known_fvg.structure_leg_id is None:
                        matching_event = next(
                            (candidate for candidate in self._known_structure_events
                             if candidate.direction == known_fvg.direction and
                             known_fvg.index < candidate.index and
                             known_fvg.confirmed_at <= candidate.index),
                            None,
                        )
                        if matching_event is not None:
                            known_fvg.structure_leg_id = matching_event.structure_leg_id
                looking_for_bearish = (ev.direction == "bullish")

                # Scan backwards in candle_history for source candle
                source_candle = None
                history_list = list(self._candle_history)
                scanned = 0
                for hist_c in reversed(history_list):
                    if hist_c["bar_index"] >= ev.index:
                        continue
                    if scanned >= self.ob_lookback:
                        break
                    scanned += 1
                    if looking_for_bearish:
                        if hist_c["close"] < hist_c["open"]:
                            source_candle = hist_c
                            break
                    else:
                        if hist_c["close"] > hist_c["open"]:
                            source_candle = hist_c
                            break

                if source_candle is None:
                    continue

                ob_index = source_candle["bar_index"]
                dedup_key = (ev.index, ob_index)
                if dedup_key in self._seen_keys or dedup_key in self._pending_obs:
                    continue

                ob = OrderBlock(
                    index=ob_index,
                    time=source_candle["time"],
                    direction=ev.direction,
                    high=source_candle["high"],
                    low=source_candle["low"],
                    open=source_candle["open"],
                    close=source_candle["close"],
                    origin_type=ev.event_type,
                    mode=self.mode,
                    quality="base",
                    source_event_index=ev.index,
                    source_event_type=ev.event_type,
                    source_swing_index=ev.broken_swing_index,
                    created_at=ev.index,
                    source_fvg_index=None,
                    source_fvg_top=None,
                    source_fvg_bottom=None,
                    mitigated=False,
                    mitigated_at=None,
                    mitigation_pct=0.0,
                    valid=True,
                    invalidated_at=None,
                    invalidation_reason=None,
                    retest_count=0,
                )

                # Link FVG from known_fvgs
                linked = _find_matching_fvg(
                    ob=ob,
                    fvgs=self._known_fvgs,
                    mode=self.mode,
                    fvg_lookback=self.fvg_lookback,
                    require_fvg_before_event=self.require_fvg_before_event,
                    event_index=ev.index,
                    structure_events=self._known_structure_events,
                    structure_leg_id=ev.structure_leg_id,
                )

                if self.require_fvg and linked is None:
                    if not self.require_fvg_before_event:
                        # Enforce max_pending_obs capacity limit to prevent unbounded memory growth
                        if len(self._pending_obs) >= self.max_pending_obs:
                            oldest_key = next(iter(self._pending_obs))
                            del self._pending_obs[oldest_key]

                        max_created_at = max(ev.index, ob_index + self.fvg_lookback)
                        cutoff_states: Dict[int, OrderBlock] = {}
                        cutoff_in_zone: Dict[int, bool] = {}
                        for k in range(ev.index, max_created_at + 1):
                            cutoff_states[k] = dataclasses.replace(ob, created_at=k)
                            cutoff_in_zone[k] = False

                        self._pending_obs[dedup_key] = {
                            "event": ev,
                            "ob": ob,
                            "max_created_at": max_created_at,
                            "cutoff_states": cutoff_states,
                            "cutoff_in_zone": cutoff_in_zone,
                        }
                    continue

                self._seen_keys.add(dedup_key)

                if linked is not None:
                    ob.source_fvg_index  = linked.index
                    ob.source_fvg_top    = linked.top
                    ob.source_fvg_bottom = linked.bottom
                    ob.quality = "premium_candidate" if ev.displacement else "strong"
                    ob.structure_leg_id = ev.structure_leg_id or linked.structure_leg_id
                    ob.created_at = max(ev.index, int(linked.confirmed_at))
                    is_valid, was_in_zone = _reset_and_backfill_ob(ob, self._candle_history, ob.created_at, bar_idx - 1)
                    if is_valid:
                        self._in_zone_state[dedup_key] = was_in_zone

                self._all_blocks.append(ob)
                if ob.valid:
                    self._active_blocks.append(ob)
                    newly_created.append(ob)

        # Update compact cutoff states for candidate OBs in _pending_obs
        for key, pending in list(self._pending_obs.items()):
            cutoff_states = pending["cutoff_states"]
            cutoff_in_zone = pending["cutoff_in_zone"]
            for k, ob_k in cutoff_states.items():
                if bar_idx <= k:
                    continue
                if not ob_k.valid:
                    continue
                was_in_zone = cutoff_in_zone[k]
                is_in_zone = _apply_ob_state(ob_k, c, was_in_zone)
                cutoff_in_zone[k] = is_in_zone

        # Update mitigation & invalidation for active blocks using current candle
        blocks_to_process = list(self._active_blocks)
        still_active: List[OrderBlock] = []
        for ob in blocks_to_process:
            # An Order Block can only be mitigated/invalidated STRICTLY AFTER its confirmation
            if max(ob.index, ob.source_event_index, ob.created_at) >= bar_idx:
                still_active.append(ob)
                continue

            ob_key = (ob.source_event_index, ob.index)
            was_in_zone = self._in_zone_state.get(ob_key, False)
            is_in_zone = _apply_ob_state(ob, c, was_in_zone)
            self._in_zone_state[ob_key] = is_in_zone

            if ob.valid:
                still_active.append(ob)
            else:
                self._in_zone_state.pop(ob_key, None)

        self._active_blocks = still_active

        return newly_created

    def get_active_blocks(self) -> List[OrderBlock]:
        """Returns only valid (non-invalidated) OBs."""
        return [ob for ob in self._active_blocks if ob.valid]

    def get_all_blocks(self) -> List[OrderBlock]:
        """Returns all OBs ever created."""
        return list(self._all_blocks)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_candle(candle) -> Dict[str, Any]:
        if isinstance(candle, pd.Series):
            c_dict = candle.to_dict()
            if "time" not in c_dict and isinstance(candle.name, (pd.Timestamp, str)):
                c_dict["time"] = candle.name
        else:
            c_dict = dict(candle)

        raw_time = c_dict.get("time")
        if isinstance(raw_time, pd.Timestamp):
            ts = raw_time if raw_time.tz is not None else raw_time.tz_localize("UTC")
        elif raw_time is not None:
            ts = pd.to_datetime(raw_time, utc=True)
        else:
            ts = pd.Timestamp.now(tz="UTC")

        bar_idx = c_dict.get("bar_index")
        if bar_idx is None:
            raise ValueError("Candle must contain 'bar_index'.")

        return {
            "bar_index": int(bar_idx),
            "time": ts,
            "open":  float(c_dict.get("open",  0.0)),
            "high":  float(c_dict["high"]),
            "low":   float(c_dict["low"]),
            "close": float(c_dict.get("close", 0.0)),
        }
