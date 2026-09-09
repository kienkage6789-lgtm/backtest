"""
smc/strategy.py
===============
SMC Confluence Strategy Module implementing the complete end-to-end trading pipeline:
1. Multi-tier Structure: Swing (HTF bias) + Internal (LTF trigger).
2. Configurable Swing Bias timing ('pre_candle' vs 'post_candle') to eliminate simultaneous-bar flip conflicts.
3. Order Block and FVG Confluence Engine: Links structural breaks with source Order Blocks and displacement FVGs.
4. Zero-Lookahead State-Driven Setup Window: Pending orders placed strictly at the bar when candidate FVGs/OBs are confirmed.
5. Real-Time Candidate Ranking Pool ('m_maxRanked') selecting top N eligible FVGs without future lookahead.
6. SL Anchored to Order Block protection boundary or FVG boundary.
7. Pending Limit / Market Order generation with Retest and SL/TP Management.
8. Comprehensive Funnel Telemetry Tracker (SMCFunnelStats) reporting drop-offs at each pipeline stage.
9. Visual Chart Objects generation (Pivots, BOS/CHoCH lines, OB zones, FVG boxes, CHoCH windows, trade markers).
"""

import dataclasses
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional, Literal, Tuple, Union

from smc.models import SwingPoint, StructureEvent, FairValueGap, OrderBlock
from smc.data_contract import normalize_ohlcv
from smc.structure.swings import detect_swings
from smc.structure.bos_choch import detect_structure_events
from smc.zones.fvg import detect_fvgs
from smc.zones.order_block import detect_order_blocks


@dataclass
class SMCFunnelStats:
    """
    Telemetry tracker recording counts at every stage of the SMC pipeline.
    Answers exactly why trades were or were not executed.
    """
    total_bars: int = 0
    swing_pivots_detected: int = 0
    swing_bos_detected: int = 0
    swing_choch_detected: int = 0
    internal_pivots_detected: int = 0
    internal_bos_detected: int = 0
    internal_choch_detected: int = 0
    order_blocks_detected: int = 0
    choch_matching_swing_bias: int = 0
    choch_rejected_by_bias: int = 0
    choch_rejected_simultaneous_swing: int = 0
    fvgs_total_detected: int = 0
    fvgs_in_choch_window: int = 0
    fvgs_rejected_direction: int = 0
    fvgs_rejected_min_score: int = 0
    fvgs_eligible_after_ranking: int = 0
    pending_orders_created: int = 0
    pending_orders_expired_no_fill: int = 0
    orders_filled: int = 0
    trades_won: int = 0
    trades_lost: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def summary_table(self) -> str:
        lines = [
            "======================= SMC PIPELINE FUNNEL STATS =======================",
            f"[Step 1] Total Bars Scanned                : {self.total_bars:,}",
            f"[Step 2] Swing Pivots Confirmed (HTF)       : {self.swing_pivots_detected:,} (BOS: {self.swing_bos_detected}, CHoCH: {self.swing_choch_detected})",
            f"[Step 3] Internal CHoCH Detected (LTF)      : {self.internal_choch_detected:,}",
            f"  |-- Passed: Matched Swing Bias            : {self.choch_matching_swing_bias:,}",
            f"  |-- Dropped: Wrong Swing Bias             : {self.choch_rejected_by_bias:,}",
            f"  +-- Dropped: Simultaneous Swing Break     : {self.choch_rejected_simultaneous_swing:,}",
            f"[Step 4] Total FVGs Detected                : {self.fvgs_total_detected:,}",
            f"  |-- Order Blocks Linked (Confluence)      : {self.order_blocks_detected:,}",
            f"  |-- FVGs in CHoCH Search Window           : {self.fvgs_in_choch_window:,}",
            f"  |-- Dropped: FVG Direction Mismatch       : {self.fvgs_rejected_direction:,}",
            f"  +-- Dropped: FVG Under Min Score          : {self.fvgs_rejected_min_score:,}",
            f"[Step 5] FVGs Eligible After Ranking Pool   : {self.fvgs_eligible_after_ranking:,}",
            f"[Step 6] Orders Placed                      : {self.pending_orders_created:,}",
            f"  |-- Cancelled: Window Expired (No Fill)   : {self.pending_orders_expired_no_fill:,}",
            f"  +-- Triggered & Filled                    : {self.orders_filled:,}",
            f"[Step 7] Trades Completed                   : {self.trades_won + self.trades_lost:,} (Win: {self.trades_won}, Loss: {self.trades_lost})",
            "========================================================================="
        ]
        return "\n".join(lines)


@dataclass
class SMCStrategyConfig:
    """
    Configuration parameters for the SMC Confluence Strategy.
    """
    swing_strength: int = 5
    internal_strength: int = 2
    bias_timing: Literal["pre_candle", "post_candle"] = "pre_candle"
    skip_simultaneous_swing_break: bool = False
    choch_fvg_window: int = 5
    max_ranked_fvgs: int = 3
    min_fvg_score: float = 0.0
    require_ob: bool = False
    ob_lookback: int = 20
    sl_anchor: Literal["ob", "fvg"] = "ob"
    order_type: Literal["limit", "market"] = "limit"
    limit_expiry_bars: int = 15
    rr_ratio: float = 2.0
    sl_buffer: float = 0.5


@dataclass
class SMCStrategyResult:
    """
    Full execution result containing trading signals, telemetry, and visual overlays.
    """
    signals: pd.Series
    trades: List[Dict[str, Any]]
    funnel_stats: SMCFunnelStats
    chart_objects: Dict[str, Any]


def run_smc_strategy(
    data: Union[pd.DataFrame, List[Dict[str, Any]]],
    config: Optional[SMCStrategyConfig] = None
) -> SMCStrategyResult:
    """
    Executes the SMC Confluence trading strategy on normalized OHLCV data.
    Strict zero-lookahead pipeline:
    1. Multi-tier swing detection and bias classification.
    2. Internal structure break identification.
    3. Order block and FVG confluence mapping.
    4. State-driven candidate evaluation where limit orders are placed strictly at the
       bar index where the setup/FVG is confirmed.
    """
    if config is None:
        config = SMCStrategyConfig()

    df = normalize_ohlcv(data) if not isinstance(data, pd.DataFrame) or 'bar_index' not in data.columns else data.copy()

    stats = SMCFunnelStats()
    n = len(df)
    stats.total_bars = n

    signals = pd.Series(0, index=df.index, dtype=int)

    if n < max(config.swing_strength * 2 + 1, config.internal_strength * 2 + 1):
        return SMCStrategyResult(signals=signals, trades=[], funnel_stats=stats, chart_objects={})

    # Step 1: Detect Swings (HTF)
    swing_points = detect_swings(df, strength=config.swing_strength, mode="swing")
    stats.swing_pivots_detected = len(swing_points)

    # Step 2: Detect Swing Structure (BOS / CHoCH)
    swing_events = detect_structure_events(df, swings=swing_points, strength=config.swing_strength, mode="swing")
    stats.swing_bos_detected = sum(1 for e in swing_events if e.event_type == "BOS")
    stats.swing_choch_detected = sum(1 for e in swing_events if e.event_type == "CHoCH")

    # Step 3: Detect Internal Swings (LTF)
    internal_swings = detect_swings(df, strength=config.internal_strength, mode="internal")
    stats.internal_pivots_detected = len(internal_swings)

    # Step 4: Detect Internal Structure (BOS / CHoCH)
    internal_events = detect_structure_events(df, swings=internal_swings, strength=config.internal_strength, mode="internal")
    stats.internal_bos_detected = sum(1 for e in internal_events if e.event_type == "BOS")
    stats.internal_choch_detected = sum(1 for e in internal_events if e.event_type == "CHoCH")

    # Step 5: Detect FVGs
    fvgs = detect_fvgs(df, mode="internal", min_gap_pct=0.0)
    stats.fvgs_total_detected = len(fvgs)

    # Step 6: Detect Order Blocks (Confluence)
    order_blocks = detect_order_blocks(
        df,
        structure_events=internal_events,
        fvgs=fvgs,
        mode="internal",
        ob_lookback=config.ob_lookback
    )
    stats.order_blocks_detected = len(order_blocks)

    # Map events by confirmed bar index
    swing_events_by_bar: Dict[int, List[StructureEvent]] = {}
    for ev in swing_events:
        swing_events_by_bar.setdefault(ev.index, []).append(ev)

    internal_events_by_bar: Dict[int, List[StructureEvent]] = {}
    for ev in internal_events:
        internal_events_by_bar.setdefault(ev.index, []).append(ev)

    fvgs_by_confirmed_at: Dict[int, List[FairValueGap]] = {}
    for f in fvgs:
        fvgs_by_confirmed_at.setdefault(f.confirmed_at, []).append(f)

    obs_by_event_index: Dict[int, List[OrderBlock]] = {}
    for ob in order_blocks:
        obs_by_event_index.setdefault(ob.source_event_index, []).append(ob)

    # Pre-calculate Swing Bias trajectory per bar:
    pre_candle_bias: List[Optional[str]] = [None] * n
    post_candle_bias: List[Optional[str]] = [None] * n

    current_swing_bias: Optional[str] = None
    for k in range(n):
        bar_idx = int(df['bar_index'].iloc[k])
        pre_candle_bias[k] = current_swing_bias
        if bar_idx in swing_events_by_bar:
            latest_ev = swing_events_by_bar[bar_idx][-1]
            current_swing_bias = latest_ev.direction
        post_candle_bias[k] = current_swing_bias

    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    opens = df['open'].to_numpy()
    closes = df['close'].to_numpy()
    bar_indices = df['bar_index'].to_numpy()
    bar_index_to_row = {int(b): i for i, b in enumerate(bar_indices)}
    times = df.index

    trades: List[Dict[str, Any]] = []
    active_limit_orders: List[Dict[str, Any]] = []
    active_trades: List[Dict[str, Any]] = []
    active_setup_windows: List[Dict[str, Any]] = []

    choch_windows_chart: List[Dict[str, Any]] = []

    for k in range(n):
        b_idx = int(bar_indices[k])
        c_high = float(highs[k])
        c_low = float(lows[k])
        c_open = float(opens[k])
        c_close = float(closes[k])
        c_time = times[k]

        # -------------------------------------------------------------
        # A. Check and Fill Pending Limit Orders (Strict Zero Lookahead)
        # -------------------------------------------------------------
        remaining_limits = []
        for order in active_limit_orders:
            if b_idx > order['expiry_bar']:
                stats.pending_orders_expired_no_fill += 1
                continue

            # Orders placed on bar m can only fill on bars > m
            if b_idx <= order['placed_bar']:
                remaining_limits.append(order)
                continue

            filled = False
            fill_price = order['entry_price']

            if order['direction'] == 'bullish':
                if c_low <= fill_price:
                    filled = True
            elif order['direction'] == 'bearish':
                if c_high >= fill_price:
                    filled = True

            if filled:
                stats.orders_filled += 1
                signals.iloc[k] = 1 if order['direction'] == 'bullish' else -1
                trade_record = {
                    'entry_bar': b_idx,
                    'entry_time': c_time,
                    'direction': order['direction'],
                    'entry_price': fill_price,
                    'stop_loss': order['stop_loss'],
                    'take_profit': order['take_profit'],
                    'status': 'open',
                    'pnl': 0.0,
                    'source_fvg_index': order['fvg'].index,
                    'source_choch_index': order['choch_index'],
                    'source_ob_index': order['ob'].index if order.get('ob') else None,
                    'placed_bar': order['placed_bar']
                }
                active_trades.append(trade_record)
            else:
                remaining_limits.append(order)

        active_limit_orders = remaining_limits

        # -------------------------------------------------------------
        # B. Manage Open Trades (SL / TP)
        # -------------------------------------------------------------
        remaining_trades = []
        for trade in active_trades:
            exit_trade = False
            if trade['direction'] == 'bullish':
                if c_low <= trade['stop_loss']:
                    trade['status'] = 'loss'
                    trade['exit_bar'] = b_idx
                    trade['exit_time'] = c_time
                    trade['exit_price'] = trade['stop_loss']
                    trade['pnl'] = trade['stop_loss'] - trade['entry_price']
                    stats.trades_lost += 1
                    exit_trade = True
                elif c_high >= trade['take_profit']:
                    trade['status'] = 'win'
                    trade['exit_bar'] = b_idx
                    trade['exit_time'] = c_time
                    trade['exit_price'] = trade['take_profit']
                    trade['pnl'] = trade['take_profit'] - trade['entry_price']
                    stats.trades_won += 1
                    exit_trade = True
            elif trade['direction'] == 'bearish':
                if c_high >= trade['stop_loss']:
                    trade['status'] = 'loss'
                    trade['exit_bar'] = b_idx
                    trade['exit_time'] = c_time
                    trade['exit_price'] = trade['stop_loss']
                    trade['pnl'] = trade['entry_price'] - trade['stop_loss']
                    stats.trades_lost += 1
                    exit_trade = True
                elif c_low <= trade['take_profit']:
                    trade['status'] = 'win'
                    trade['exit_bar'] = b_idx
                    trade['exit_time'] = c_time
                    trade['exit_price'] = trade['take_profit']
                    trade['pnl'] = trade['entry_price'] - trade['take_profit']
                    stats.trades_won += 1
                    exit_trade = True

            if exit_trade:
                trades.append(trade)
            else:
                remaining_trades.append(trade)

        active_trades = remaining_trades

        # -------------------------------------------------------------
        # C. Multi-Tier Bias and Internal Structure Trigger
        # -------------------------------------------------------------
        active_bias = pre_candle_bias[k] if config.bias_timing == "pre_candle" else post_candle_bias[k]

        if b_idx in internal_events_by_bar:
            for in_ev in internal_events_by_bar[b_idx]:
                if in_ev.event_type != "CHoCH":
                    continue

                if config.skip_simultaneous_swing_break and (b_idx in swing_events_by_bar):
                    stats.choch_rejected_simultaneous_swing += 1
                    continue

                if active_bias != in_ev.direction:
                    stats.choch_rejected_by_bias += 1
                    continue

                stats.choch_matching_swing_bias += 1

                window_end_bar = min(b_idx + config.choch_fvg_window, int(bar_indices[-1]))
                choch_windows_chart.append({
                    'start_index': b_idx,
                    'end_index': window_end_bar,
                    'start_time': c_time.isoformat() if hasattr(c_time, 'isoformat') else str(c_time),
                    'direction': in_ev.direction
                })

                linked_ob = None
                if b_idx in obs_by_event_index:
                    linked_ob = obs_by_event_index[b_idx][0]

                if config.require_ob and linked_ob is None:
                    continue

                active_setup_windows.append({
                    'direction': in_ev.direction,
                    'start_bar': b_idx,
                    'expiry_bar': window_end_bar,
                    'choch_index': b_idx,
                    'ob': linked_ob,
                    'order_placed': False,
                })

        # -------------------------------------------------------------
        # D. State-Driven FVG / OB Confluence Discovery & Order Placement
        # -------------------------------------------------------------
        for win in active_setup_windows:
            if win['order_placed']:
                continue
            if b_idx > win['expiry_bar']:
                continue

            # Check FVGs confirmed up to and including current bar b_idx within window
            fvg_candidates = []
            for check_bar in range(win['choch_index'] + 1, b_idx + 1):
                if check_bar in fvgs_by_confirmed_at:
                    for f in fvgs_by_confirmed_at[check_bar]:
                        stats.fvgs_in_choch_window += 1
                        if f.direction != win['direction']:
                            stats.fvgs_rejected_direction += 1
                            continue

                        # Check if FVG was already filled on or before b_idx (zero lookahead fill audit)
                        filled_so_far = False
                        for fill_bar_idx in range(f.confirmed_at, b_idx + 1):
                            r_idx = bar_index_to_row.get(fill_bar_idx)
                            if r_idx is not None:
                                if f.direction == 'bullish' and lows[r_idx] <= f.bottom:
                                    filled_so_far = True
                                    break
                                elif f.direction == 'bearish' and highs[r_idx] >= f.top:
                                    filled_so_far = True
                                    break
                        if filled_so_far:
                            continue

                        # Quality scoring (zero future data used)
                        gap_size = abs(f.top - f.bottom)
                        pct_size = (gap_size / f.bottom) if f.bottom > 0 else 0.0
                        score = (pct_size * 1000.0) + (10.0 / (1 + abs(f.confirmed_at - b_idx)))
                        if win['ob'] is not None:
                            score += 20.0
                            if win['ob'].quality == "premium_candidate":
                                score += 30.0
                            elif win['ob'].quality == "strong":
                                score += 15.0

                        if score < config.min_fvg_score:
                            stats.fvgs_rejected_min_score += 1
                            continue

                        fvg_candidates.append((score, f))

            if not fvg_candidates:
                continue

            # Sort by score descending
            fvg_candidates.sort(key=lambda x: x[0], reverse=True)

            # Apply max_ranked_fvgs candidate limit
            ranked_pool = fvg_candidates[:config.max_ranked_fvgs]
            stats.fvgs_eligible_after_ranking += len(ranked_pool)

            best_score, best_fvg = ranked_pool[0]
            matched_ob = win['ob']

            # Generate Entry & SL/TP based on Order Block + FVG Confluence
            if win['direction'] == 'bullish':
                entry_price = float(best_fvg.top)
                if config.sl_anchor == "ob" and matched_ob is not None:
                    stop_loss = float(matched_ob.low - config.sl_buffer)
                else:
                    stop_loss = float(best_fvg.bottom - config.sl_buffer)
                risk = max(entry_price - stop_loss, 0.1)
                take_profit = entry_price + (risk * config.rr_ratio)
            else:
                entry_price = float(best_fvg.bottom)
                if config.sl_anchor == "ob" and matched_ob is not None:
                    stop_loss = float(matched_ob.high + config.sl_buffer)
                else:
                    stop_loss = float(best_fvg.top + config.sl_buffer)
                risk = max(stop_loss - entry_price, 0.1)
                take_profit = entry_price - (risk * config.rr_ratio)

            # Place Order strictly at current bar b_idx (when candidate is confirmed)
            if config.order_type == 'market':
                stats.pending_orders_created += 1
                stats.orders_filled += 1
                signals.iloc[k] = 1 if win['direction'] == 'bullish' else -1
                active_trades.append({
                    'entry_bar': b_idx,
                    'entry_time': c_time,
                    'direction': win['direction'],
                    'entry_price': c_close,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'status': 'open',
                    'pnl': 0.0,
                    'source_fvg_index': best_fvg.index,
                    'source_choch_index': win['choch_index'],
                    'source_ob_index': matched_ob.index if matched_ob is not None else None,
                    'placed_bar': b_idx
                })
            else:
                stats.pending_orders_created += 1
                active_limit_orders.append({
                    'fvg': best_fvg,
                    'ob': matched_ob,
                    'direction': win['direction'],
                    'entry_price': entry_price,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'placed_bar': b_idx,  # CURRENT BAR! Zero lookahead!
                    'expiry_bar': b_idx + config.limit_expiry_bars,
                    'choch_index': win['choch_index']
                })

            win['order_placed'] = True

    # Collect remaining active trades
    for trade in active_trades:
        trade['status'] = 'open'
        trade['exit_bar'] = int(bar_indices[-1])
        trade['exit_time'] = times[-1]
        trade['exit_price'] = float(closes[-1])
        if trade['direction'] == 'bullish':
            trade['pnl'] = float(closes[-1]) - trade['entry_price']
        else:
            trade['pnl'] = trade['entry_price'] - float(closes[-1])
        trades.append(trade)

    # Point 5: Visual Chart Objects Assembly
    chart_objects = {
        'swing_pivots': [sw.to_dict() for sw in swing_points],
        'internal_pivots': [sw.to_dict() for sw in internal_swings],
        'swing_events': [ev.to_dict() for ev in swing_events],
        'internal_events': [ev.to_dict() for ev in internal_events],
        'order_blocks': [ob.to_dict() for ob in order_blocks],
        'fvgs': [f.to_dict() for f in fvgs],
        'choch_windows': choch_windows_chart,
        'funnel': stats.to_dict(),
        'trades': trades
    }

    return SMCStrategyResult(
        signals=signals,
        trades=trades,
        funnel_stats=stats,
        chart_objects=chart_objects
    )
