import os
import sys
sys.path.insert(0, os.path.abspath("."))
import time
import json
import pandas as pd
from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine

def probe(bar_count=1000):
    t0 = time.time()
    feed = DataFeed()
    candles = feed.get_candles("M15", start_time="2022-01-01 00:00:00", limit=bar_count)
    df = pd.DataFrame(candles)
    print(f"Loaded {len(df)} M15 bars: start={df['time'].iloc[0]}, end={df['time'].iloc[-1]}")

    with open("research/runs/t54_1_htf_events_m15_10000.json", "r") as fp:
        payload = json.load(fp)

    eng = BacktestEngine(
        initial_capital=10000.0,
        lot_size=0.01,
        spread_points=20.0,
        commission_per_lot=5.0,
        allow_short=True,
    )
    res = eng.run(
        df,
        "smc_wave1",
        {"min_rr": 1.5, "cooldown_bars": 3},
        timeframe="M15",
        htf_events=payload,
    )
    t1 = time.time()
    # Funnel diagnostics
    coord = eng.last_coordinator if hasattr(eng, "last_coordinator") else None
    
    # We can run directly with coordinator to inspect each stage bar-by-bar
    from smc.engine.backtest_adapter import SMCBacktestCoordinator, parse_htf_event_payload
    from smc.engine.context import ContextBuilderConfig
    from smc.engine.execution import ExecutionConfig
    from smc.data_contract import normalize_ohlcv

    parsed_htf = [parse_htf_event_payload(e) for e in payload.get("events", [])]
    exec_cfg = ExecutionConfig(lot_size=0.01, contract_size=100.0, spread_points=20.0, commission_per_lot=5.0)
    ctx_cfg = ContextBuilderConfig(timeframe="M15")
    coordinator = SMCBacktestCoordinator(
        execution_config=exec_cfg,
        context_config=ctx_cfg,
        cooldown_bars=3,
        mode="smc_wave1",
        htf_events=parsed_htf,
        initial_capital=10000.0,
    )

    bars_scanned = len(df)
    htf_events_accepted = len(parsed_htf)
    htf_bias_count = 0
    ltf_structure_events_count = 0
    fvg_count = 0
    ob_count = 0
    candidate_setups_count = 0
    eligible_setups_count = 0
    selector_selections_count = 0
    rejection_reasons = {}

    df_norm = normalize_ohlcv(df)
    df_norm["bar_index"] = range(len(df_norm))

    for idx, (ts_idx, row) in enumerate(df_norm.iterrows()):
        candle = row.to_dict()
        candle["time"] = ts_idx
        candle["bar_index"] = int(idx)
        res_step = coordinator.step(candle, is_last_bar=(idx == len(df_norm) - 1))
        
        ctx = coordinator.context_builder.last_context
        if ctx is not None:
            if ctx.htf_bias is not None:
                htf_bias_count += 1
            ltf_structure_events_count += len(ctx.recent_structures)
            fvg_count += len(ctx.active_fvgs)
            ob_count += len(ctx.active_obs)

    # Gather coordinator results
    for dec in coordinator._all_decisions:
        if dec.action == "SELECT":
            selector_selections_count += 1
        for eval_cand in dec.evaluations:
            candidate_setups_count += 1
            if eval_cand.status == "ELIGIBLE":
                eligible_setups_count += 1
            else:
                r = eval_cand.rejection_reason or "unknown"
                rejection_reasons[r] = rejection_reasons.get(r, 0) + 1

    execution_instructions_count = len(coordinator._all_intents)
    successful_fills = len([e for e in coordinator._all_events if e.event_type == "POSITION_OPENED"])
    completed_trades = len(coordinator.kernel.trades)

    print("\n=== 12-LAYER FUNNEL DIAGNOSTICS ===")
    funnel = [
        ("M15 bars scanned", bars_scanned),
        ("HTF events accepted", htf_events_accepted),
        ("HTF bias available", htf_bias_count),
        ("LTF structure events", ltf_structure_events_count),
        ("FVGs detected", fvg_count),
        ("OBs detected", ob_count),
        ("candidate setups", candidate_setups_count),
        ("eligible setups", eligible_setups_count),
        ("selector selections", selector_selections_count),
        ("execution instructions", execution_instructions_count),
        ("successful fills", successful_fills),
        ("completed trades", completed_trades),
    ]
    first_zero_layer = None
    for name, val in funnel:
        print(f"  {name:25s}: {val}")
        if val == 0 and first_zero_layer is None:
            first_zero_layer = name

    print(f"\nFirst layer dropping to 0: {first_zero_layer}")
    if rejection_reasons:
        print(f"Candidate rejection reasons: {rejection_reasons}")

if __name__ == "__main__":
    probe(10000)
