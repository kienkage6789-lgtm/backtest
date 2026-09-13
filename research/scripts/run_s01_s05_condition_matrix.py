"""Run relaxed-condition matrices for S01 and S05 on the 10k-bar sample."""

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


def run_one(df: pd.DataFrame, htf: dict, strategy_id: str, params: dict) -> dict:
    engine = BacktestEngine(
        initial_capital=10000.0, lot_size=0.01, contract_size=100.0,
        spread_points=20.0, commission_per_lot=5.0, allow_short=True,
    )
    result = engine.run(df.copy(), strategy_id, {"min_rr": 1.5, "cooldown_bars": 3, **params},
                        timeframe="M15", htf_events=htf)
    metrics = extract_metrics(result, df)
    diagnostics = compute_wave1_diagnostics(result, htf, len(df))
    return {
        "conditions": params,
        **metrics,
        "funnel": diagnostics["funnel"],
        "coordinator_metrics": diagnostics["coordinator_metrics"],
        "first_zero_layer": diagnostics["first_zero_layer"],
        "root_cause_classification": diagnostics["root_cause_classification"],
    }


def main() -> None:
    df = pd.DataFrame(DataFeed().get_candles(
        "M15", start_time="2022-01-01 00:00:00", end_time="2024-09-30 23:59:59", limit=10000
    ))
    htf = json.loads((ROOT_DIR / "research/runs/t54_1_htf_events_m15_10000.json").read_text(encoding="utf-8"))
    matrix = {
        "smc_s01": [
            ("baseline", {}),
            ("no_displacement", {"s01_require_displacement": False}),
            ("no_displacement_fvg20_expiry30", {
                "s01_require_displacement": False,
                "s01_fvg_to_mss_max_bars": 20,
                "s01_entry_expiry_bars": 30,
            }),
            ("no_displacement_fvg30_expiry50_rr1", {
                "s01_require_displacement": False,
                "s01_fvg_to_mss_max_bars": 30,
                "s01_entry_expiry_bars": 50,
                "s01_min_rr": 1.0,
            }),
        ],
        "smc_s05": [
            ("baseline", {}),
            ("no_displacement", {"s05_require_displacement": False}),
            ("no_displacement_ob_age50", {
                "s05_require_displacement": False,
                "s05_max_ob_age_bars": 50,
            }),
            ("no_displacement_ob_age75_rr1", {
                "s05_require_displacement": False,
                "s05_max_ob_age_bars": 75,
                "s05_min_rr": 1.0,
            }),
        ],
    }
    output = {"dataset": {"bars": len(df), "timeframe": "M15"}, "strategies": {}}
    for strategy_id, variants in matrix.items():
        output["strategies"][strategy_id] = []
        for name, params in variants:
            row = run_one(df, htf, strategy_id, params)
            row["name"] = name
            output["strategies"][strategy_id].append(row)
            print(
                f"{strategy_id} {name} | candidates={row['funnel']['candidate_setups']} "
                f"| trades={row['total_trades']} | W={row['winning_trades']} "
                f"| L={row['losing_trades']} | win_rate={row['win_rate']}% "
                f"| first_zero={row['first_zero_layer']}"
            )
    target = ROOT_DIR / "research/runs/s01_s05_condition_matrix_10000.json"
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Saved artifact: {target.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
