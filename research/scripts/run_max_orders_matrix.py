"""Find the highest-order-count relaxed configuration for the Wave 1 SMC set."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from engine.backtest_engine import BacktestEngine
from engine.data_feed import DataFeed
from research.scripts.run_t54_1_baseline_10000 import compute_wave1_diagnostics, extract_metrics


def main() -> None:
    df = pd.DataFrame(DataFeed().get_candles(
        "M15", start_time="2022-01-01 00:00:00", end_time="2024-09-30 23:59:59", limit=10000
    ))
    htf = json.loads((ROOT_DIR / "research/runs/t54_1_htf_events_m15_10000.json").read_text(encoding="utf-8"))
    configs = {
        "smc_s01": {
            "cooldown_bars": 0, "s01_require_displacement": False,
            "s01_sweep_to_mss_max_bars": 50, "s01_fvg_to_mss_max_bars": 30,
            "s01_entry_expiry_bars": 50, "s01_min_rr": 1.0,
        },
        "smc_s05": {
            "cooldown_bars": 0, "s05_require_displacement": False,
            "s05_max_ob_age_bars": 75, "s05_min_rr": 1.0,
        },
        "smc_s09": {
            "cooldown_bars": 0, "s09_require_displacement": False,
            "s09_fvg_to_mss_max_bars": 50, "s09_min_rr": 1.0,
            "s09_use_time_filter": False,
        },
    }
    rows = []
    for strategy_id, params in configs.items():
        engine = BacktestEngine(
            initial_capital=10000.0, lot_size=0.01, contract_size=100.0,
            spread_points=20.0, commission_per_lot=5.0, allow_short=True,
        )
        result = engine.run(df.copy(), strategy_id, {"min_rr": 1.0, **params},
                            timeframe="M15", htf_events=htf)
        metrics = extract_metrics(result, df)
        diagnostics = compute_wave1_diagnostics(result, htf, len(df))
        row = {
            "strategy_id": strategy_id, "conditions": params, **metrics,
            "funnel": diagnostics["funnel"],
            "coordinator_metrics": diagnostics["coordinator_metrics"],
            "first_zero_layer": diagnostics["first_zero_layer"],
            "root_cause_classification": diagnostics["root_cause_classification"],
        }
        rows.append(row)
        print(
            f"{strategy_id} | candidates={row['funnel']['candidate_setups']} "
            f"| trades={row['total_trades']} | W={row['winning_trades']} "
            f"| L={row['losing_trades']} | win_rate={row['win_rate']}% "
            f"| net=${row['net_profit']} | first_zero={row['first_zero_layer']}"
        )
    target = ROOT_DIR / "research/runs/smc_max_orders_10000.json"
    target.write_text(json.dumps({"dataset": {"bars": len(df), "timeframe": "M15"}, "runs": rows},
                                 indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Saved artifact: {target.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
