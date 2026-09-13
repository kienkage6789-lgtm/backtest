"""Run an S09 condition matrix and expose the gate that suppresses trades."""

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
from research.scripts.run_t54_1_baseline_10000 import (
    compute_wave1_diagnostics,
    extract_metrics,
)


def main() -> None:
    feed = DataFeed()
    df = pd.DataFrame(
        feed.get_candles(
            "M15",
            start_time="2022-01-01 00:00:00",
            end_time="2024-09-30 23:59:59",
            limit=10000,
        )
    )
    htf_path = ROOT_DIR / "research/runs/t54_1_htf_events_m15_10000.json"
    htf = json.loads(htf_path.read_text(encoding="utf-8"))

    variants = [
        {
            "name": "baseline_no_session_filter",
            "label": "Mặc định hiện tại: bỏ giờ phiên, giữ displacement",
            "params": {},
        },
        {
            "name": "disable_displacement",
            "label": "Bỏ bắt buộc displacement tại MSS",
            "params": {"s09_require_displacement": False},
        },
        {
            "name": "disable_displacement_lag20",
            "label": "Bỏ displacement + nới FVG→MSS tối đa 20 nến",
            "params": {
                "s09_require_displacement": False,
                "s09_fvg_to_mss_max_bars": 20,
            },
        },
        {
            "name": "disable_displacement_lag50_rr1",
            "label": "Bỏ displacement + lag 50 nến + RR tối thiểu 1.0",
            "params": {
                "s09_require_displacement": False,
                "s09_fvg_to_mss_max_bars": 50,
                "s09_min_rr": 1.0,
            },
        },
    ]

    rows = []
    out_dir = ROOT_DIR / "research/runs"
    for variant in variants:
        params = {"min_rr": 1.5, "cooldown_bars": 3, **variant["params"]}
        engine = BacktestEngine(
            initial_capital=10000.0,
            lot_size=0.01,
            contract_size=100.0,
            spread_points=20.0,
            commission_per_lot=5.0,
            allow_short=True,
        )
        result = engine.run(
            df.copy(),
            "smc_s09",
            params,
            timeframe="M15",
            htf_events=htf,
        )
        metrics = extract_metrics(result, df)
        diagnostics = compute_wave1_diagnostics(result, htf, len(df))
        row = {
            "name": variant["name"],
            "label": variant["label"],
            "conditions": params,
            **metrics,
            "funnel": diagnostics["funnel"],
            "coordinator_metrics": diagnostics["coordinator_metrics"],
            "first_zero_layer": diagnostics["first_zero_layer"],
            "root_cause_classification": diagnostics["root_cause_classification"],
        }
        rows.append(row)
        print(
            f"{variant['label']} | candidates={row['funnel']['candidate_setups']} "
            f"| trades={row['total_trades']} | win={row['winning_trades']} "
            f"| loss={row['losing_trades']} | win_rate={row['win_rate']}% "
            f"| first_zero={row['first_zero_layer']}"
        )

    output = {
        "dataset": {"bars": len(df), "timeframe": "M15"},
        "purpose": "S09 condition loosening matrix with gate funnel and win/loss metrics",
        "variants": rows,
    }
    target = out_dir / "s09_condition_matrix_10000.json"
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Saved artifact: {target.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
