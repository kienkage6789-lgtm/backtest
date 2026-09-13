"""
smc/context/htf_poi.py
======================
Higher Timeframe Point of Interest (HTF POI) Tracker.

Manages HTF POI zones (Fair Value Gaps and Order Blocks) to provide
macro context for Lower Timeframe strategy entries (e.g. S01 ICT 2022).

Rules:
1. HTF FVG/OB zones are purely context/POI zones, NEVER direct trade orders.
2. Price retracement into HTF POI is checked via overlap:
   candle.low <= poi.top AND candle.high >= poi.bottom
3. Classification:
   - WICK_TOUCH: Only the candle wick penetrates the zone.
   - BODY_ENTRY: The candle body penetrates or overlaps the zone.
   - FULL_CLOSE_THROUGH: The candle closes completely through the zone:
     - Bullish POI: close < poi.bottom -> status = "invalidated"
     - Bearish POI: close > poi.top    -> status = "invalidated"
4. POI validity by direction:
   - BUY: HTF bias bullish, POI bullish, active, not closed through.
   - SELL: HTF bias bearish, POI bearish, active, not closed through.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from smc.models import (
    FairValueGap,
    HTFPOI,
    OrderBlock,
    StructureEvent,
)


class HTFPOITracker:
    """
    Stateful tracker for Higher Timeframe Points of Interest (HTF POIs).
    Maintains active POIs and updates their interaction states with LTF candles.
    """

    def __init__(self, initial_pois: Optional[Sequence[Union[HTFPOI, dict[str, Any]]]] = None) -> None:
        self._initial_pois: list[HTFPOI] = []
        self._pois: dict[str, HTFPOI] = {}

        if initial_pois:
            for p in initial_pois:
                poi_obj = self._coerce_poi(p)
                self._initial_pois.append(self._clone_poi(poi_obj))
                self._pois[poi_obj.poi_id] = self._clone_poi(poi_obj)

    @staticmethod
    def _clone_poi(p: HTFPOI) -> HTFPOI:
        return HTFPOI(
            poi_id=p.poi_id,
            poi_type=p.poi_type,
            direction=p.direction,
            top=p.top,
            bottom=p.bottom,
            timeframe=p.timeframe,
            created_at=p.created_at,
            source_event=p.source_event,
            valid_until=p.valid_until,
            status=p.status,
            touch_count=p.touch_count,
            last_touch_bar=p.last_touch_bar,
            meta=dict(p.meta) if p.meta else {},
        )

    @staticmethod
    def _coerce_poi(src: Union[HTFPOI, dict[str, Any], Any]) -> HTFPOI:
        if isinstance(src, HTFPOI):
            return src
        if isinstance(src, dict):
            return HTFPOI(
                poi_id=str(src["poi_id"]),
                poi_type=src["poi_type"],
                direction=src["direction"],
                top=float(src["top"]),
                bottom=float(src["bottom"]),
                timeframe=str(src.get("timeframe", "H1")),
                created_at=src.get("created_at"),
                source_event=src.get("source_event"),
                valid_until=src.get("valid_until"),
                status=src.get("status", "active"),
                touch_count=int(src.get("touch_count", 0)),
                last_touch_bar=src.get("last_touch_bar"),
                meta=src.get("meta", {}),
            )
        return HTFPOI(
            poi_id=str(src.poi_id),
            poi_type=src.poi_type,
            direction=src.direction,
            top=float(src.top),
            bottom=float(src.bottom),
            timeframe=getattr(src, "timeframe", "H1"),
            created_at=getattr(src, "created_at", None),
            source_event=getattr(src, "source_event", None),
            valid_until=getattr(src, "valid_until", None),
            status=getattr(src, "status", "active"),
            touch_count=int(getattr(src, "touch_count", 0)),
            last_touch_bar=getattr(src, "last_touch_bar", None),
            meta=getattr(src, "meta", {}),
        )

    def add_poi(self, poi: Union[HTFPOI, dict[str, Any], Any]) -> HTFPOI:
        """Add a POI to the tracker."""
        poi_obj = self._coerce_poi(poi)
        self._pois[poi_obj.poi_id] = self._clone_poi(poi_obj)
        return self._pois[poi_obj.poi_id]

    def register_poi_from_event(self, event: StructureEvent, timeframe: str = "H1") -> Optional[HTFPOI]:
        """
        Derive an HTF POI from an HTF BOS event.
        The origin zone of the structural break serves as the HTF POI.
        """
        if event.event_type != "BOS":
            return None

        meta = getattr(event, "meta", {})
        if "poi_top" in meta and "poi_bottom" in meta:
            p_top = float(meta["poi_top"])
            p_bottom = float(meta["poi_bottom"])
        else:
            base_min = min(float(event.broken_swing_price), float(event.close_price))
            base_max = max(float(event.broken_swing_price), float(event.close_price))
            diff = max(base_max - base_min, 10.0)
            if event.direction == "bullish":
                p_top = base_max + diff
                p_bottom = max(0.0, base_min - diff * 2.0)
            else:
                p_top = base_max + diff * 2.0
                p_bottom = max(0.0, base_min - diff)

        poi_id = f"poi_{timeframe}_{event.direction}_{event.index}_{int(p_bottom)}_{int(p_top)}"
        poi = HTFPOI(
            poi_id=poi_id,
            poi_type="OB",
            direction=event.direction,
            top=round(p_top, 3),
            bottom=round(p_bottom, 3),
            timeframe=timeframe,
            created_at=event.index,
            source_event=event,
            status="active",
            touch_count=0,
            meta={"origin_event": event.to_dict() if hasattr(event, "to_dict") else None},
        )
        return self.add_poi(poi)

    def register_poi_from_fvg(self, fvg: FairValueGap, timeframe: str = "H1") -> HTFPOI:
        """Register an HTF Fair Value Gap as an HTF POI."""
        poi_id = f"poi_fvg_{timeframe}_{fvg.direction}_{fvg.index}"
        poi = HTFPOI(
            poi_id=poi_id,
            poi_type="FVG",
            direction=fvg.direction,
            top=round(float(fvg.top), 3),
            bottom=round(float(fvg.bottom), 3),
            timeframe=timeframe,
            created_at=fvg.index,
            source_event=fvg,
            status="active",
            touch_count=0,
            meta={"fvg": fvg.to_dict() if hasattr(fvg, "to_dict") else None},
        )
        return self.add_poi(poi)

    def register_poi_from_ob(self, ob: OrderBlock, timeframe: str = "H1") -> HTFPOI:
        """Register an HTF Order Block as an HTF POI."""
        poi_id = f"poi_ob_{timeframe}_{ob.direction}_{ob.index}"
        poi = HTFPOI(
            poi_id=poi_id,
            poi_type="OB",
            direction=ob.direction,
            top=round(float(ob.high), 3),
            bottom=round(float(ob.low), 3),
            timeframe=timeframe,
            created_at=ob.index,
            source_event=ob,
            status="active",
            touch_count=0,
            meta={"ob": ob.to_dict() if hasattr(ob, "to_dict") else None},
        )
        return self.add_poi(poi)

    @staticmethod
    def classify_interaction(
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        poi: HTFPOI,
    ) -> Optional[Literal["WICK_TOUCH", "BODY_ENTRY", "FULL_CLOSE_THROUGH"]]:
        """
        Classifies interaction of a candle with an HTF POI zone.

        Overlap test:
            candle.low <= poi.top AND candle.high >= poi.bottom
        """
        if not (c_low <= poi.top and c_high >= poi.bottom):
            return None

        # Full close through check
        if poi.direction == "bullish" and c_close < poi.bottom:
            return "FULL_CLOSE_THROUGH"
        if poi.direction == "bearish" and c_close > poi.top:
            return "FULL_CLOSE_THROUGH"

        # Body entry check
        body_min = min(c_open, c_close)
        body_max = max(c_open, c_close)
        if body_min <= poi.top and body_max >= poi.bottom:
            return "BODY_ENTRY"

        # Wick touch only
        return "WICK_TOUCH"

    def update(
        self,
        candle: Union[Mapping[str, Any], pd.Series],
        current_bias: Optional[str] = None,
    ) -> tuple[HTFPOI, ...]:
        """
        Updates POI interaction states against a newly closed LTF candle.

        Args:
            candle: Mapping containing open, high, low, close, bar_index.
            current_bias: Optional current HTF bias ('bullish', 'bearish', 'neutral').

        Returns:
            Tuple of active POIs matching the current bias.
        """
        c_open = float(candle["open"])
        c_high = float(candle["high"])
        c_low = float(candle["low"])
        c_close = float(candle["close"])
        bar_idx = int(candle.get("bar_index", 0))

        for poi in self._pois.values():
            if poi.status != "active":
                continue

            interaction = self.classify_interaction(c_open, c_high, c_low, c_close, poi)
            if interaction is None:
                continue

            if interaction == "FULL_CLOSE_THROUGH":
                poi.status = "invalidated"
                poi.meta = {
                    **dict(poi.meta),
                    "invalidation_reason": "full_close_through",
                    "invalidated_at_bar": bar_idx,
                }
            else:
                poi.touch_count += 1
                poi.last_touch_bar = bar_idx
                poi.meta = {
                    **dict(poi.meta),
                    "last_interaction": interaction,
                    "last_interaction_bar": bar_idx,
                }

        return self.get_active_pois(current_bias)

    def get_active_pois(self, bias: Optional[str] = None) -> tuple[HTFPOI, ...]:
        """
        Returns active POIs. If bias is specified ('bullish' or 'bearish'),
        filters only POIs matching the bias direction.
        """
        active_list: list[HTFPOI] = []
        for poi in self._pois.values():
            if poi.status != "active":
                continue
            if bias is not None and bias in ("bullish", "bearish") and poi.direction != bias:
                continue
            active_list.append(poi)

        # Deterministic sort: (created_at, top, bottom, poi_id)
        active_list.sort(key=lambda p: (
            p.created_at if p.created_at is not None else 0,
            p.top,
            p.bottom,
            p.poi_id,
        ))
        return tuple(active_list)

    def get_all_pois(self) -> tuple[HTFPOI, ...]:
        """Returns all tracked POIs regardless of status."""
        return tuple(sorted(self._pois.values(), key=lambda p: (p.created_at if p.created_at is not None else 0, p.poi_id)))

    def reset(self) -> None:
        """Reset tracker to initial state."""
        self._pois = {p.poi_id: self._clone_poi(p) for p in self._initial_pois}


__all__ = [
    "HTFPOITracker",
]
