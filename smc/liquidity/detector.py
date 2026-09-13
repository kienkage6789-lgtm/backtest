from typing import Literal, Optional, Any, Iterator
import numpy as np
import pandas as pd

from smc.models import SwingPoint, LiquidityPool, LiquiditySweep


def _validate_tolerance(
    tolerance_pct: Optional[float],
    tolerance_pips: Optional[float],
    tolerance_atr_mult: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Validate and normalize tolerance parameters."""
    if tolerance_pct is not None and tolerance_pct <= 0:
        raise ValueError(f"tolerance_pct must be > 0, got {tolerance_pct}")
    if tolerance_pips is not None and tolerance_pips <= 0:
        raise ValueError(f"tolerance_pips must be > 0, got {tolerance_pips}")
    if tolerance_atr_mult is not None and tolerance_atr_mult <= 0:
        raise ValueError(f"tolerance_atr_mult must be > 0, got {tolerance_atr_mult}")

    if tolerance_pct is None and tolerance_pips is None and tolerance_atr_mult is None:
        tolerance_pct = 0.001  # Default to 0.1%

    return tolerance_pct, tolerance_pips, tolerance_atr_mult


def _is_within_tolerance(
    price1: float,
    price2: float,
    tolerance_pct: Optional[float],
    tolerance_pips: Optional[float],
    tolerance_atr_mult: Optional[float],
    atr_val: Optional[float] = None,
) -> bool:
    """Check if price1 and price2 are within configured tolerance."""
    diff = abs(price1 - price2)
    if tolerance_pct is not None:
        avg_price = (price1 + price2) / 2.0
        if diff <= avg_price * tolerance_pct:
            return True
    if tolerance_pips is not None:
        if diff <= tolerance_pips:
            return True
    if tolerance_atr_mult is not None and atr_val is not None:
        if diff <= tolerance_atr_mult * atr_val:
            return True
    return False


def validate_sweep_semantics(
    sweep: Any = None,
    *,
    pool_kind: Optional[str] = None,
    direction: Optional[str] = None,
    liquidity_side: Optional[str] = None,
    reversal_direction: Optional[str] = None,
    raid_direction: Optional[str] = None,
    raise_error: bool = True,
) -> bool:
    """
    Validate that liquidity sweep semantics are coherent:
    - SELL_SIDE / low pool must have raid_direction="bearish" and reversal_direction="bullish" (direction="bullish").
    - BUY_SIDE / high pool must have raid_direction="bullish" and reversal_direction="bearish" (direction="bearish").
    """
    if sweep is not None:
        pool_kind = getattr(sweep, "pool_kind", pool_kind)
        direction = getattr(sweep, "direction", direction)
        liquidity_side = getattr(sweep, "liquidity_side", liquidity_side)
        reversal_direction = getattr(sweep, "reversal_direction", reversal_direction) or direction
        raid_direction = getattr(sweep, "raid_direction", raid_direction)

    if pool_kind is None and liquidity_side is None:
        if raise_error:
            raise ValueError("Neither pool_kind nor liquidity_side provided.")
        return False

    is_low = pool_kind in ("equal_lows", "swing_low") or liquidity_side == "SELL_SIDE"
    is_high = pool_kind in ("equal_highs", "swing_high") or liquidity_side == "BUY_SIDE"

    if is_low and is_high:
        if raise_error:
            raise ValueError(f"Contradictory pool_kind ({pool_kind}) and liquidity_side ({liquidity_side})")
        return False

    if is_low:
        if direction != "bullish" or (reversal_direction is not None and reversal_direction != "bullish") or (raid_direction is not None and raid_direction != "bearish") or (liquidity_side is not None and liquidity_side != "SELL_SIDE"):
            if raise_error:
                raise ValueError(
                    f"Contradictory low sweep semantics: pool_kind={pool_kind}, liquidity_side={liquidity_side}, "
                    f"direction={direction}, reversal_direction={reversal_direction}, raid_direction={raid_direction}. "
                    f"Low sweeps must raid sell-side liquidity downwards and reverse bullish."
                )
            return False

    if is_high:
        if direction != "bearish" or (reversal_direction is not None and reversal_direction != "bearish") or (raid_direction is not None and raid_direction != "bullish") or (liquidity_side is not None and liquidity_side != "BUY_SIDE"):
            if raise_error:
                raise ValueError(
                    f"Contradictory high sweep semantics: pool_kind={pool_kind}, liquidity_side={liquidity_side}, "
                    f"direction={direction}, reversal_direction={reversal_direction}, raid_direction={raid_direction}. "
                    f"High sweeps must raid buy-side liquidity upwards and reverse bearish."
                )
            return False

    return True


class LiquidityTracker:
    """
    Incremental Liquidity Pool & Liquidity Sweep Tracker.

    Processes candles and confirmed SwingPoints bar-by-bar without repaint or lookahead.
    """

    def __init__(
        self,
        tolerance_pct: Optional[float] = 0.001,
        tolerance_pips: Optional[float] = None,
        tolerance_atr_mult: Optional[float] = None,
        include_single_swings: bool = True,
        mode: Literal["swing", "internal"] = "swing",
    ):
        pct, pips, atr = _validate_tolerance(tolerance_pct, tolerance_pips, tolerance_atr_mult)
        self.tolerance_pct = pct
        self.tolerance_pips = pips
        self.tolerance_atr_mult = atr
        self.include_single_swings = include_single_swings
        self.mode = mode

        self._pools: list[LiquidityPool] = []
        self._sweeps: list[LiquiditySweep] = []
        self._active_pools: list[LiquidityPool] = []
        self._seen_swing_indices: set[int] = set()

    def update(
        self,
        candle: dict[str, Any],
        newly_confirmed_swings: Optional[list[SwingPoint]] = None,
        atr_val: Optional[float] = None,
    ) -> None:
        """
        Process a newly closed candle and newly confirmed swing points.

        Args:
            candle: Dict containing OHLC, bar_index/index, time.
            newly_confirmed_swings: List of SwingPoints confirmed at or before current candle.
            atr_val: Optional current ATR value for ATR tolerance policy.
        """
        bar_idx = int(candle.get("bar_index", candle.get("index", 0)))
        bar_time = candle.get("time")
        if isinstance(bar_time, str):
            bar_time = pd.Timestamp(bar_time)

        c_open = float(candle["open"])
        c_high = float(candle["high"])
        c_low = float(candle["low"])
        c_close = float(candle["close"])

        # 1. Process newly confirmed swings
        if newly_confirmed_swings:
            for s in newly_confirmed_swings:
                if s.confirmed_at > bar_idx or s.mode != self.mode or s.index in self._seen_swing_indices:
                    continue

                self._seen_swing_indices.add(s.index)
                swing_dict = s.to_dict() if hasattr(s, "to_dict") else dict(s)

                # Search for an existing active pool matching kind & price tolerance
                target_kinds = ("equal_highs", "swing_high") if s.kind == "high" else ("equal_lows", "swing_low")
                matched_pool = None

                for pool in self._active_pools:
                    if pool.kind in target_kinds and pool.valid and not pool.swept and pool.confirmed_at <= bar_idx:
                        if _is_within_tolerance(
                            s.price, pool.price, self.tolerance_pct, self.tolerance_pips, self.tolerance_atr_mult, atr_val
                        ):
                            matched_pool = pool
                            break

                if matched_pool:
                    # Merge swing into existing pool
                    matched_pool.indices.append(int(s.index))
                    matched_pool.source_swings.append(swing_dict)
                    all_prices = [float(sw["price"]) for sw in matched_pool.source_swings]
                    matched_pool.price = float(np.mean(all_prices))
                    matched_pool.price_max = float(max(all_prices))
                    matched_pool.price_min = float(min(all_prices))
                    matched_pool.created_at = max(matched_pool.created_at, int(s.confirmed_at))
                    matched_pool.confirmed_at = max(matched_pool.confirmed_at, int(s.confirmed_at))
                    matched_pool.kind = "equal_highs" if s.kind == "high" else "equal_lows"
                else:
                    # Create a new pool
                    if self.include_single_swings:
                        pool_kind = "swing_high" if s.kind == "high" else "swing_low"
                        new_pool = LiquidityPool(
                            kind=pool_kind,
                            price=float(s.price),
                            price_max=float(s.price),
                            price_min=float(s.price),
                            indices=[int(s.index)],
                            created_at=int(s.confirmed_at),
                            confirmed_at=int(s.confirmed_at),
                            swept=False,
                            valid=True,
                            mode=self.mode,
                            source_swings=[swing_dict],
                            structure_leg_id=getattr(s, "structure_leg_id", None),
                        )
                        self._pools.append(new_pool)
                        self._active_pools.append(new_pool)

        # 2. Evaluate Liquidity Sweep & Invalidation for active pools
        # Copy list to handle potential modifications
        active_pools_snapshot = [p for p in self._active_pools if p.valid and p.confirmed_at <= bar_idx]

        for pool in active_pools_snapshot:
            if not pool.valid or pool.swept:
                continue

            if pool.kind in ("equal_highs", "swing_high"):
                if c_high > pool.price_max:
                    if c_close > pool.price_max:
                        # Close break -> Invalidation (NOT a sweep)
                        pool.valid = False
                        pool.invalidated_at = bar_idx
                        pool.invalidation_reason = "close_break"
                    else:
                        # Wick penetration + close back inside -> Bearish Liquidity Sweep
                        candle_range = c_high - c_low
                        upper_wick = c_high - max(c_open, c_close)
                        wick_ratio = upper_wick / candle_range if candle_range > 0 else 0.0
                        sweep_type = "clean" if wick_ratio >= 0.4 else "wick_only"

                        sweep = LiquiditySweep(
                            index=bar_idx,
                            time=bar_time,
                            direction="bearish",
                            pool_kind=pool.kind,
                            pool_price=pool.price,
                            pool_indices=list(pool.indices),
                            price_wick=c_high,
                            close_price=c_close,
                            created_at=bar_idx,
                            confirmed_at=bar_idx,
                            swept_at=bar_idx,
                            sweep_type=sweep_type,
                            valid=True,
                            mode=pool.mode,
                            structure_leg_id=pool.structure_leg_id,
                            liquidity_side="BUY_SIDE",
                            raid_direction="bullish",
                            reversal_direction="bearish",
                        )
                        validate_sweep_semantics(sweep, raise_error=True)
                        self._sweeps.append(sweep)

                        pool.swept = True
                        pool.swept_at = bar_idx
                        pool.sweep_type = sweep_type
                        pool.valid = False

            elif pool.kind in ("equal_lows", "swing_low"):
                if c_low < pool.price_min:
                    if c_close < pool.price_min:
                        # Close break -> Invalidation (NOT a sweep)
                        pool.valid = False
                        pool.invalidated_at = bar_idx
                        pool.invalidation_reason = "close_break"
                    else:
                        # Wick penetration + close back inside -> Bullish Liquidity Sweep
                        candle_range = c_high - c_low
                        lower_wick = min(c_open, c_close) - c_low
                        wick_ratio = lower_wick / candle_range if candle_range > 0 else 0.0
                        sweep_type = "clean" if wick_ratio >= 0.4 else "wick_only"

                        sweep = LiquiditySweep(
                            index=bar_idx,
                            time=bar_time,
                            direction="bullish",
                            pool_kind=pool.kind,
                            pool_price=pool.price,
                            pool_indices=list(pool.indices),
                            price_wick=c_low,
                            close_price=c_close,
                            created_at=bar_idx,
                            confirmed_at=bar_idx,
                            swept_at=bar_idx,
                            sweep_type=sweep_type,
                            valid=True,
                            mode=pool.mode,
                            structure_leg_id=pool.structure_leg_id,
                            liquidity_side="SELL_SIDE",
                            raid_direction="bearish",
                            reversal_direction="bullish",
                        )
                        validate_sweep_semantics(sweep, raise_error=True)
                        self._sweeps.append(sweep)

                        pool.swept = True
                        pool.swept_at = bar_idx
                        pool.sweep_type = sweep_type
                        pool.valid = False

        # Keep active pool list updated
        self._active_pools = [p for p in self._active_pools if p.valid]

    def get_all_pools(self) -> list[LiquidityPool]:
        """Return all liquidity pools recorded so far."""
        return [self._clone_pool(p) for p in self._pools]

    def get_active_pools(self) -> list[LiquidityPool]:
        """Return currently active, valid liquidity pools (defensive clones)."""
        return [self._clone_pool(p) for p in self._active_pools if p.valid and not p.swept]

    def _iter_active_pools_internal(self) -> Iterator[LiquidityPool]:
        """Internal generator yielding active tracker pool instances.

        STRICTLY INTERNAL / READ-ONLY: Callers must never mutate yielded pool instances.
        Used by StrategyContextBuilder to convert directly into immutable snapshots
        without creating intermediate mutable clones.
        """
        for p in self._active_pools:
            if p.valid and not p.swept:
                yield p

    def get_sweeps(self) -> list[LiquiditySweep]:
        """Return all detected liquidity sweeps."""
        return [self._clone_sweep(s) for s in self._sweeps]

    def get_sweeps_at_bar(self, bar_index: int) -> list[LiquiditySweep]:
        """Return clones of sweeps confirmed at specified bar index without scanning/cloning all history."""
        result = []
        for s in reversed(self._sweeps):
            if s.index == bar_index:
                result.append(self._clone_sweep(s))
            elif s.index < bar_index:
                break
        result.reverse()
        return result

    @staticmethod
    def _clone_pool(p: LiquidityPool) -> LiquidityPool:
        return LiquidityPool(
            kind=p.kind,
            price=p.price,
            price_max=p.price_max,
            price_min=p.price_min,
            indices=list(p.indices),
            created_at=p.created_at,
            confirmed_at=p.confirmed_at,
            swept=p.swept,
            swept_at=p.swept_at,
            sweep_type=p.sweep_type,
            valid=p.valid,
            invalidated_at=p.invalidated_at,
            invalidation_reason=p.invalidation_reason,
            mode=p.mode,
            source_swings=[dict(sw) for sw in p.source_swings],
            structure_leg_id=p.structure_leg_id,
            liquidity_side=getattr(p, "liquidity_side", None),
        )

    @staticmethod
    def _clone_sweep(s: LiquiditySweep) -> LiquiditySweep:
        return LiquiditySweep(
            index=s.index,
            time=s.time,
            direction=s.direction,
            pool_kind=s.pool_kind,
            pool_price=s.pool_price,
            pool_indices=list(s.pool_indices),
            price_wick=s.price_wick,
            close_price=s.close_price,
            created_at=s.created_at,
            confirmed_at=s.confirmed_at,
            swept_at=s.swept_at,
            sweep_type=s.sweep_type,
            valid=s.valid,
            mode=s.mode,
            structure_leg_id=s.structure_leg_id,
            liquidity_side=getattr(s, "liquidity_side", None),
            raid_direction=getattr(s, "raid_direction", None),
            reversal_direction=getattr(s, "reversal_direction", None),
        )


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range (ATR) for a given DataFrame."""
    if df.empty:
        return pd.Series(dtype=float)
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=1).mean()
    return atr


def detect_liquidity_pools(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    tolerance_pct: Optional[float] = 0.001,
    tolerance_pips: Optional[float] = None,
    tolerance_atr_mult: Optional[float] = None,
    include_single_swings: bool = True,
    mode: Literal["swing", "internal"] = "swing",
    replay_cutoff: Optional[int] = None,
    atr_period: int = 14,
    atr_series: Optional[pd.Series] = None,
) -> list[LiquidityPool]:
    """Batch detection of LiquidityPools."""
    pools, _ = detect_liquidity(
        df=df,
        swings=swings,
        tolerance_pct=tolerance_pct,
        tolerance_pips=tolerance_pips,
        tolerance_atr_mult=tolerance_atr_mult,
        include_single_swings=include_single_swings,
        mode=mode,
        replay_cutoff=replay_cutoff,
        atr_period=atr_period,
        atr_series=atr_series,
    )
    return pools


def detect_liquidity_sweeps(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    tolerance_pct: Optional[float] = 0.001,
    tolerance_pips: Optional[float] = None,
    tolerance_atr_mult: Optional[float] = None,
    include_single_swings: bool = True,
    mode: Literal["swing", "internal"] = "swing",
    replay_cutoff: Optional[int] = None,
    atr_period: int = 14,
    atr_series: Optional[pd.Series] = None,
) -> list[LiquiditySweep]:
    """Batch detection of LiquiditySweeps."""
    _, sweeps = detect_liquidity(
        df=df,
        swings=swings,
        tolerance_pct=tolerance_pct,
        tolerance_pips=tolerance_pips,
        tolerance_atr_mult=tolerance_atr_mult,
        include_single_swings=include_single_swings,
        mode=mode,
        replay_cutoff=replay_cutoff,
        atr_period=atr_period,
        atr_series=atr_series,
    )
    return sweeps


def detect_liquidity(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    tolerance_pct: Optional[float] = 0.001,
    tolerance_pips: Optional[float] = None,
    tolerance_atr_mult: Optional[float] = None,
    include_single_swings: bool = True,
    mode: Literal["swing", "internal"] = "swing",
    replay_cutoff: Optional[int] = None,
    atr_period: int = 14,
    atr_series: Optional[pd.Series] = None,
) -> tuple[list[LiquidityPool], list[LiquiditySweep]]:
    """
    Batch function for detecting LiquidityPools and LiquiditySweeps from DataFrame and Swings.

    Guarantees 100% parity with LiquidityTracker including ATR tolerance policy.
    """
    if df.empty:
        return [], []

    cutoff = replay_cutoff if replay_cutoff is not None else len(df) - 1
    cutoff = min(cutoff, len(df) - 1)

    if tolerance_atr_mult is not None and atr_series is None:
        atr_series = calculate_atr(df, period=atr_period)

    # Filter swings confirmed at or before cutoff
    valid_swings = [s for s in swings if s.confirmed_at <= cutoff and s.mode == mode]

    swings_by_confirmed: dict[int, list[SwingPoint]] = {}
    for s in valid_swings:
        swings_by_confirmed.setdefault(s.confirmed_at, []).append(s)

    tracker = LiquidityTracker(
        tolerance_pct=tolerance_pct,
        tolerance_pips=tolerance_pips,
        tolerance_atr_mult=tolerance_atr_mult,
        include_single_swings=include_single_swings,
        mode=mode,
    )

    for i in range(cutoff + 1):
        row = df.iloc[i]
        candle = {
            "bar_index": i,
            "time": df.index[i],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        cur_swings = swings_by_confirmed.get(i, [])
        atr_val = None
        if atr_series is not None and i < len(atr_series):
            raw_atr = atr_series.iloc[i]
            if not pd.isna(raw_atr):
                atr_val = float(raw_atr)

        tracker.update(candle, newly_confirmed_swings=cur_swings, atr_val=atr_val)

    return tracker.get_all_pools(), tracker.get_sweeps()

