"""
Python script to assemble the complete tests/test_smc_strategy_s09.py file.
"""

target_file = r"D:\tool\backtest\tests\test_smc_strategy_s09.py"

header_and_helpers = '''"""
tests/test_smc_strategy_s09.py
==============================
Exhaustive 106-test suite for S09 ICT Silver Bullet Strategy Template.
Covers Groups A through H adhering strictly to T53_6_S09_ICT_SILVER_BULLET_IMPLEMENTATION_PLAN.md,
ADR 16, and ADR 22.
"""

from __future__ import annotations

import copy
import datetime
import json
import math
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
import zoneinfo

import numpy as np
import pandas as pd

from smc.models import StructureEvent
from smc.engine.context import (
    ContextBuilderConfig,
    StrategyContextBuilder,
    build_strategy_contexts,
)
from smc.engine.errors import (
    StrategyRegistryError,
    StrategyStateError,
    StrategyValidationError,
)
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    FairValueGapSnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    StrategyContext,
    StrategyProfile,
    StructureEventSnapshot,
    make_cluster_id,
    make_evidence_id,
    make_setup_id,
)
from smc.engine.protocol import StrategyTemplate, validate_strategy_id
from smc.engine.registry import StrategyRegistry, StrategyRegistryConfig
from smc.engine.strategies.s01_ict_2022 import S01Config, S01ICT2022Strategy
from smc.engine.strategies.s09_ict_silver_bullet import (
    CANONICAL_WINDOWS,
    NEW_YORK_TZ,
    WINDOW_SCHEDULE,
    S09Config,
    S09ICTSilverBulletStrategy,
    S09Narrative,
    S09NarrativeStage,
    WindowKey,
    compute_window_bounds_for_date,
    get_ny_local_date_str,
    get_window_for_close_time,
)


# =============================================================================
# Test Helper Functions
# =============================================================================

BASE_NY_DATETIME = datetime.datetime(2024, 5, 15, 10, 0, 0, tzinfo=NEW_YORK_TZ)
BASE_UTC_TIMESTAMP = pd.Timestamp(BASE_NY_DATETIME).tz_convert("UTC")


def _bar_time(bar_index: int, timeframe_minutes: int = 1) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Returns (open_ts_utc, close_ts_utc) for a given bar offset from 10:00 NY time."""
    open_ts = BASE_UTC_TIMESTAMP + pd.Timedelta(minutes=bar_index * timeframe_minutes)
    close_ts = open_ts + pd.Timedelta(minutes=timeframe_minutes)
    return open_ts, close_ts


def _make_bias(
    bias: str = "bullish",
    bar_index: int = 0,
    as_of: Optional[pd.Timestamp] = None,
    source_event_time: Optional[pd.Timestamp] = None,
) -> BiasStateSnapshot:
    open_ts, _ = _bar_time(bar_index)
    return BiasStateSnapshot(
        bias=bias,
        timestamp=open_ts,
        as_of=as_of if as_of is not None else open_ts,
        source_event_time=source_event_time if source_event_time is not None else open_ts,
    )


def _make_context(
    bar_index: int = 0,
    open_p: Optional[float] = None,
    high_p: Optional[float] = None,
    low_p: Optional[float] = None,
    close_p: Optional[float] = None,
    sweeps: Sequence[LiquiditySweepSnapshot] = (),
    structure_events: Sequence[StructureEventSnapshot] = (),
    fvgs: Sequence[FairValueGapSnapshot] = (),
    pools: Sequence[LiquidityPoolSnapshot] = (),
    bias: Optional[str] = "bullish",
    timeframe: str = "M1",
    htf_bias: Optional[BiasStateSnapshot] = None,
    open_time: Optional[pd.Timestamp] = None,
    close_time: Optional[pd.Timestamp] = None,
) -> StrategyContext:
    c = close_p if close_p is not None else 2050.0
    o = open_p if open_p is not None else c
    h = high_p if high_p is not None else max(o, c) + 2.0
    l = low_p if low_p is not None else min(o, c) - 2.0
    if h < max(o, c):
        h = max(o, c)
    if l > min(o, c):
        l = min(o, c)

    if open_time is None or close_time is None:
        def_o, def_c = _bar_time(bar_index)
        ts = open_time if open_time is not None else def_o
        bct = close_time if close_time is not None else def_c
    else:
        ts = open_time
        bct = close_time

    if htf_bias is None and bias is not None:
        htf_bias = _make_bias(bias, bar_index)

    return StrategyContext(
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=bct,
        symbol="XAUUSD",
        timeframe=timeframe,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=100.0,
        atr14=2.0,
        recent_sweeps=tuple(sweeps),
        recent_structures=tuple(structure_events),
        active_fvgs=tuple(fvgs),
        active_pools=tuple(pools),
        htf_bias=htf_bias,
    )


def _make_sweep(
    index: int = 5,
    direction: str = "bullish",
    pool_kind: str = "equal_lows",
    price_wick: float = 2035.0,
    pool_price: float = 2036.0,
    close_price: float = 2038.0,
    swept_at: Optional[int] = None,
    confirmed_at: Optional[int] = None,
    mode: str = "internal",
    valid: bool = True,
    pool_indices: Sequence[int] = (1, 3),
    structure_leg_id: Optional[str] = None,
) -> LiquiditySweepSnapshot:
    open_ts, _ = _bar_time(index)
    conf_at = confirmed_at if confirmed_at is not None else index
    sw_at = swept_at if swept_at is not None else index
    return LiquiditySweepSnapshot(
        index=index,
        time=open_ts,
        direction=direction,
        pool_kind=pool_kind,
        pool_price=pool_price,
        pool_indices=tuple(sorted(pool_indices)),
        price_wick=price_wick,
        close_price=close_price,
        created_at=index,
        confirmed_at=conf_at,
        swept_at=sw_at,
        valid=valid,
        mode=mode,
        structure_leg_id=structure_leg_id,
    )


def _make_structure(
    index: int = 6,
    direction: str = "bullish",
    event_type: str = "CHoCH",
    broken_swing_index: int = 4,
    broken_swing_price: float = 2045.0,
    displacement: bool = True,
    structure_leg_id: Optional[str] = "leg1",
    mode: str = "internal",
    break_type: str = "close",
) -> StructureEventSnapshot:
    open_ts, _ = _bar_time(index)
    return StructureEventSnapshot(
        index=index,
        time=open_ts,
        direction=direction,
        event_type=event_type,
        broken_swing_index=broken_swing_index,
        broken_swing_price=broken_swing_price,
        close_price=broken_swing_price + (1.0 if direction == "bullish" else -1.0),
        displacement=displacement,
        structure_leg_id=structure_leg_id,
        mode=mode,
        confirmed_swing_at=index,
        break_type=break_type,
    )


def _make_fvg(
    index: int = 5,
    direction: str = "bullish",
    top: float = 2042.0,
    bottom: float = 2038.0,
    confirmed_at: int = 6,
    structure_leg_id: Optional[str] = "leg1",
    mode: str = "internal",
    filled: bool = False,
    filled_at: Optional[int] = None,
) -> FairValueGapSnapshot:
    open_ts, _ = _bar_time(index)
    return FairValueGapSnapshot(
        index=index,
        time=open_ts,
        direction=direction,
        top=top,
        bottom=bottom,
        confirmed_at=confirmed_at,
        structure_leg_id=structure_leg_id,
        mode=mode,
        filled=filled,
        filled_at=filled_at,
    )


def _make_pool(
    confirmed_at: int = 2,
    direction: str = "bearish",
    kind: str = "equal_highs",
    price: float = 2060.0,
    mode: str = "internal",
    valid: bool = True,
    swept: bool = False,
    indices: Sequence[int] = (1, 2),
) -> LiquidityPoolSnapshot:
    open_ts, _ = _bar_time(confirmed_at)
    return LiquidityPoolSnapshot(
        confirmed_at=confirmed_at,
        direction=direction,
        kind=kind,
        price=price,
        mode=mode,
        valid=valid,
        swept=swept,
        indices=tuple(sorted(indices)),
        time=open_ts,
    )


def _feed(strat: S09ICTSilverBulletStrategy, context: StrategyContext) -> tuple[CandidateSetup, ...]:
    """Feed context sequentially into strategy, filling intermediate gap bars with neutral context."""
    if strat._last_bar_index is not None and context.bar_index > strat._last_bar_index + 1:
        ref_p = strat._last_context_payload["close"] if strat._last_context_payload else context.close
        for b in range(strat._last_bar_index + 1, context.bar_index):
            strat.evaluate(_make_context(
                bar_index=b,
                open_p=ref_p,
                high_p=ref_p,
                low_p=ref_p,
                close_p=ref_p,
                timeframe=context.timeframe,
                bias=context.htf_bias.bias if context.htf_bias else "neutral",
            ))
    return strat.evaluate(context)


def _feed_reg(reg: StrategyRegistry, context: StrategyContext) -> Mapping[str, tuple[CandidateSetup, ...]]:
    """Feed context sequentially into StrategyRegistry, filling intermediate bars with neutral context."""
    if reg._last_bar_index is not None and context.bar_index > reg._last_bar_index + 1:
        ref_p = reg._last_context_payload["close"] if reg._last_context_payload else context.close
        for b in range(reg._last_bar_index + 1, context.bar_index):
            reg.evaluate_enabled(_make_context(
                bar_index=b,
                open_p=ref_p,
                high_p=ref_p,
                low_p=ref_p,
                close_p=ref_p,
                timeframe=context.timeframe,
                bias=context.htf_bias.bias if context.htf_bias else "neutral",
            ))
    return reg.evaluate_enabled(context)
'''

with open(target_file, "w", encoding="utf-8") as f:
    f.write(header_and_helpers)

print("Initialized target_file with header and helpers.")
