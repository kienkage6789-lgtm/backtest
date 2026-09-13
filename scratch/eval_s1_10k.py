import sys
from pathlib import Path
import json
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine
from smc.engine.strategies.s01_ict_2022 import S01ICT2022Strategy

def run_s1_eval():
    feed = DataFeed()
    candles = feed.get_candles("M15", start_time="2022-01-01 00:00:00", end_time="2024-09-30 23:59:59", limit=10000)
    df = pd.DataFrame(candles)
    print(f"Loaded {len(df)} candles")

    htf_payload_path = ROOT_DIR / "research" / "runs" / "t54_1_htf_events_m15_10000.json"
    with open(htf_payload_path, "r", encoding="utf-8") as fp:
        htf_payload = json.load(fp)

    # Instrument S01ICT2022Strategy to count internal events
    sell_side_sweeps = 0
    buy_side_sweeps = 0
    valid_mss_count = 0
    linked_fvg_count = 0

    seen_sweeps = set()
    seen_mss_fvg_pairs = set()

    orig_evaluate = S01ICT2022Strategy.evaluate
    orig_find_pair = S01ICT2022Strategy._find_valid_mss_fvg_pair

    def hooked_evaluate(self, context):
        nonlocal sell_side_sweeps, buy_side_sweeps
        for sw in context.recent_sweeps:
            s_key = (sw.index, sw.pool_kind, sw.direction, tuple(sorted(sw.pool_indices)))
            if s_key not in seen_sweeps:
                seen_sweeps.add(s_key)
                if sw.pool_kind in ("equal_lows", "swing_low") or getattr(sw, "liquidity_side", None) == "SELL_SIDE":
                    sell_side_sweeps += 1
                elif sw.pool_kind in ("equal_highs", "swing_high") or getattr(sw, "liquidity_side", None) == "BUY_SIDE":
                    buy_side_sweeps += 1
        return orig_evaluate(self, context)

    def hooked_find_pair(self, narrative, context):
        nonlocal valid_mss_count, linked_fvg_count
        pair = orig_find_pair(self, narrative, context)
        if pair is not None:
            mss, fvg = pair
            pair_key = (narrative.sweep_key, mss.index, mss.broken_swing_index, fvg.index)
            if pair_key not in seen_mss_fvg_pairs:
                seen_mss_fvg_pairs.add(pair_key)
                valid_mss_count += 1
                linked_fvg_count += 1
        return pair

    S01ICT2022Strategy.evaluate = hooked_evaluate
    S01ICT2022Strategy._find_valid_mss_fvg_pair = hooked_find_pair

    try:
        engine = BacktestEngine(
            initial_capital=10000.0,
            lot_size=0.01,
            contract_size=100.0,
            spread_points=20.0,
            commission_per_lot=5.0,
            allow_short=True,
        )

        res = engine.run(
            df.copy(),
            "smc_s01",
            {"min_rr": 1.5, "cooldown_bars": 3},
            timeframe="M15",
            htf_events=htf_payload,
        )
    finally:
        S01ICT2022Strategy.evaluate = orig_evaluate
        S01ICT2022Strategy._find_valid_mss_fvg_pair = orig_find_pair

    trades = res.get("trades", [])
    events = res.get("execution_events", [])
    decisions = res.get("decisions", [])

    total_trades = len(trades)
    winning_trades = [t for t in trades if t.get("pnl", 0.0) > 0]
    losing_trades = [t for t in trades if t.get("pnl", 0.0) < 0]
    win_rate = (len(winning_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
    gross_profit = sum(t.get("pnl", 0.0) for t in winning_trades)
    gross_loss = abs(sum(t.get("pnl", 0.0) for t in losing_trades))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    net_pnl = sum(t.get("pnl", 0.0) for t in trades)

    # Calculate max drawdown
    peak = 10000.0
    balance = 10000.0
    max_dd = 0.0
    for t in trades:
        balance += t.get("pnl", 0.0)
        if balance > peak:
            peak = balance
        dd = peak - balance
        if dd > max_dd:
            max_dd = dd

    candidates = 0
    eligible = 0
    for d in decisions:
        for ev in d.get("evaluations", []):
            candidates += 1
            if ev.get("status") == "ELIGIBLE":
                eligible += 1

    fills = sum(1 for e in events if e.get("event_type") in ("ORDER_FILLED", "POSITION_OPENED"))

    print("--- S1 BEFORE DETAILED RESULTS ---")
    print(f"Sweeps SELL_SIDE: {sell_side_sweeps}")
    print(f"Sweeps BUY_SIDE: {buy_side_sweeps}")
    print(f"Valid MSS: {valid_mss_count}")
    print(f"Linked FVG: {linked_fvg_count}")
    print(f"Candidates: {candidates}")
    print(f"Eligible: {eligible}")
    print(f"Fills: {fills}")
    print(f"Trades: {total_trades}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Net PnL: {net_pnl:.2f}")
    print(f"Max Drawdown: {max_dd:.2f}")

if __name__ == "__main__":
    run_s1_eval()
