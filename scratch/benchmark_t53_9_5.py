"""
scratch/benchmark_t53_9_5.py
=============================
T53.9.5 Opt-in Performance Benchmark Suite.

Benchmarks the SMC backtest pipeline across:
- Context Builder
- Strategy Registry (S01, S05, S09)
- Regime Classifier
- Eligibility Gate
- Confluence Batch Assembly
- Multi-Strategy Selector
- Execution Kernel
- Full SMCBacktestCoordinator End-to-End

Minimum Requirements:
- 10,000 bars
- 100 HTF events
- Scenarios:
  1. Standard Wave 1 (10,000 bars + 100 HTF events)
  2. High-Candidate & High-Execution scenario (mock setups exercising full funnel + fills)
  3. Wave 1 without HTF events (10,000 bars)
- Records:
  - Python version & platform/CPU
  - Bars, contexts, candidates, decisions, fills, trades
  - Warm-up time, median, P95, P99 per bar latency
  - Component breakdown
"""

from __future__ import annotations

import datetime
import math
import os
import platform
import sys
import time
from typing import Any, List, Tuple

# Ensure repository root is in sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd

from smc.models import StructureEvent
from smc.engine.backtest_adapter import SMCBacktestCoordinator
from smc.engine.context import (
    StrategyContextBuilder,
    ContextBuilderConfig,
    StrategyContext,
)
from smc.engine.regime import MarketRegimeClassifier, RegimeClassifierConfig
from smc.engine.registry import StrategyRegistry
from smc.engine.strategies import (
    S01ICT2022Strategy,
    S05BOSOBRetestStrategy,
    S09ICTSilverBulletStrategy,
)
from smc.engine.eligibility import EligibilityGate
from smc.engine.confluence import build_confluence_batch
from smc.engine.selector import DeterministicStrategySelector, SelectorConfig
from smc.engine.execution import (
    ExecutionConfig,
    CooldownBook,
)
from engine.execution_kernel import ExecutionKernel, ExecutionBar
from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    MarketRegime,
    StrategyProfile,
    make_cluster_id,
    make_evidence_id,
    make_setup_id,
)
from smc.engine.protocol import StrategyTemplate


def generate_synthetic_bars(n_bars: int = 10000) -> pd.DataFrame:
    """Generate n_bars of valid M15 candles with realistic price movement."""
    rng = np.random.default_rng(seed=42)
    base_time = pd.Timestamp("2024-01-01 00:00:00", tz="UTC")
    freq = pd.Timedelta(minutes=15)

    times = [base_time + i * freq for i in range(n_bars)]
    
    # Generate prices using random walk
    returns = rng.normal(loc=0.00001, scale=0.001, size=n_bars)
    price_series = 2000.0 * np.exp(np.cumsum(returns))

    rows = []
    for i in range(n_bars):
        close_p = float(price_series[i])
        prev_p = float(price_series[i - 1]) if i > 0 else 2000.0
        open_p = prev_p
        
        # Ensure high >= max(open, close) and low <= min(open, close)
        spread_noise = abs(float(rng.normal(0.5, 0.2)))
        high_p = max(open_p, close_p) + spread_noise
        low_p = min(open_p, close_p) - spread_noise
        vol = float(rng.integers(50, 500))

        rows.append({
            "bar_index": i,
            "time": times[i],
            "open": round(open_p, 3),
            "high": round(high_p, 3),
            "low": round(low_p, 3),
            "close": round(close_p, 3),
            "volume": vol,
            "is_closed": True,
        })

    return pd.DataFrame(rows)


def generate_htf_events(bars_df: pd.DataFrame, count: int = 100) -> List[StructureEvent]:
    """Generate `count` deterministic HTF events spaced across the bars."""
    n_bars = len(bars_df)
    step = max(1, n_bars // count)
    events: List[StructureEvent] = []

    for k in range(count):
        bar_idx = min(k * step, n_bars - 1)
        bar_row = bars_df.iloc[bar_idx]
        bar_time = bar_row["time"]
        ev_type = "BOS" if k % 2 == 0 else "CHoCH"
        direction = "bullish" if (k // 2) % 2 == 0 else "bearish"
        bsp = float(bar_row["open"])
        cp = float(bar_row["close"])

        ev = StructureEvent(
            index=0,  # 0 ensures index <= N for all bars where it is emitted
            time=bar_time,
            event_type=ev_type,
            direction=direction,
            broken_swing_index=0,
            broken_swing_price=bsp,
            close_price=cp,
        )
        events.append(ev)

    return events


# ---------------------------------------------------------------------------
# Mock Strategy for High-Candidate / High-Execution Benchmark
# ---------------------------------------------------------------------------

class BenchHighActivityStrategy(StrategyTemplate):
    """Generates a candidate setup every `period` bars to exercise the full pipeline."""
    def __init__(self, strategy_id: str = "S05", period: int = 50):
        self._strategy_id = strategy_id
        self.period = period
        self.profile = StrategyProfile(
            strategy_id=strategy_id,
            name=f"Mock {strategy_id}",
            allowed_directions=("BUY", "SELL"),
            timeframes=("M15",),
            version="1.0.0",
        )

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    def reset(self) -> None:
        pass

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        N = context.bar_index
        if N < 5 or N % self.period != 0:
            return ()

        direction = "BUY" if (N // self.period) % 2 == 0 else "SELL"
        entry_price = round(float(context.close), 2)
        if direction == "BUY":
            sl = entry_price - 10.0
            tp = entry_price + 20.0
        else:
            sl = entry_price + 10.0
            tp = entry_price - 20.0

        cluster_id = make_cluster_id(direction, f"leg_{N}", "zone_001")
        setup_id = make_setup_id(self._strategy_id, direction, N, cluster_id)

        bos_idx = max(0, N - 1)
        ob_idx = max(0, N - 2)
        bos_time = context.timestamp - pd.Timedelta(minutes=15)
        ob_time = context.timestamp - pd.Timedelta(minutes=30)
        evidences = (
            EvidenceRef(
                evidence_id=make_evidence_id("structure", "swing", bos_idx, f"bos_{N}"),
                kind="structure_event",
                bar_index=bos_idx,
                price=entry_price + 2.0,
                time=bos_time,
                details={"displacement": True},
            ),
            EvidenceRef(
                evidence_id=make_evidence_id("order_block", "swing", ob_idx, f"ob_{N}"),
                kind="order_block",
                bar_index=ob_idx,
                price=entry_price,
                time=ob_time,
                details={"top": entry_price + 1.0, "bottom": entry_price - 1.0, "quality": "premium_candidate"},
            ),
        )

        setup = CandidateSetup(
            setup_id=setup_id,
            strategy_id=self._strategy_id,
            direction=direction,
            bar_index=N,
            timestamp=context.timestamp,
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=tp,
            planned_rr=2.0,
            evidence_cluster_id=cluster_id,
            evidences=evidences,
            expiry_bar=N + 15,
        )
        return (setup,)


# ---------------------------------------------------------------------------
# Component-level micro-benchmarks
# ---------------------------------------------------------------------------

def run_component_benchmarks(bars_df: pd.DataFrame) -> dict[str, Any]:
    """Micro-benchmarks individual pipeline components over 10,000 bars."""
    print("\n--- Running Component-Level Profiling (10,000 bars) ---")
    n_bars = len(bars_df)

    # 1. ContextBuilder benchmark
    ctx_builder = StrategyContextBuilder(ContextBuilderConfig(timeframe="M15"))
    t0 = time.perf_counter()
    contexts: List[StrategyContext] = []
    for i in range(n_bars):
        c_dict = bars_df.iloc[i].to_dict()
        ctx = ctx_builder.update(c_dict)
        contexts.append(ctx)
    t_context = time.perf_counter() - t0
    print(f"1. ContextBuilder:       {t_context*1000:8.2f} ms ({t_context/n_bars*1e6:6.2f} us/bar)")

    # 2. MarketRegimeClassifier benchmark
    regime_clf = MarketRegimeClassifier(RegimeClassifierConfig())
    t0 = time.perf_counter()
    regimes: List[MarketRegime] = []
    for ctx in contexts:
        r = regime_clf.update(ctx)
        regimes.append(r)
    t_regime = time.perf_counter() - t0
    print(f"2. RegimeClassifier:     {t_regime*1000:8.2f} ms ({t_regime/n_bars*1e6:6.2f} us/bar)")

    # 3. Real StrategyRegistry (S01, S05, S09) benchmark
    real_strats = [
        S01ICT2022Strategy(),
        S05BOSOBRetestStrategy(),
        S09ICTSilverBulletStrategy(),
    ]
    strat_registry = StrategyRegistry(real_strats)
    t0 = time.perf_counter()
    total_candidates = 0
    for ctx in contexts:
        evaluated = strat_registry.evaluate_enabled(ctx)
        for cand_tuple in evaluated.values():
            total_candidates += len(cand_tuple)
    t_registry = time.perf_counter() - t0
    print(f"3. StrategyRegistry:     {t_registry*1000:8.2f} ms ({t_registry/n_bars*1e6:6.2f} us/bar) [{total_candidates} real candidates]")

    # 4. EligibilityGate & Confluence & Selector stress-test on synthetic candidate setups
    sample_ctx = contexts[-1]
    sample_regime = regimes[-1]
    gate = EligibilityGate()
    selector = DeterministicStrategySelector(SelectorConfig())
    cooldown = CooldownBook()

    # Generate 1,000 mock candidate setups
    synth_candidates = []
    for k in range(1000):
        direction = "BUY" if k % 2 == 0 else "SELL"
        ep = 2000.0 + k * 0.05
        sl = ep - 10.0 if direction == "BUY" else ep + 10.0
        tp = ep + 20.0 if direction == "BUY" else ep - 20.0
        cid = make_cluster_id(direction, f"leg_{k}", f"zone_{k%10}")
        sid = make_setup_id("S05", direction, sample_ctx.bar_index, cid)
        setup = CandidateSetup(
            setup_id=sid,
            strategy_id="S05",
            direction=direction,
            bar_index=sample_ctx.bar_index,
            timestamp=sample_ctx.timestamp,
            entry_price=ep,
            stop_loss=sl,
            take_profit=tp,
            planned_rr=2.0,
            evidence_cluster_id=cid,
            evidences=(
                EvidenceRef(
                    evidence_id=make_evidence_id("structure", "swing", sample_ctx.bar_index - 1, f"bos_{k}"),
                    kind="structure_event",
                    bar_index=sample_ctx.bar_index - 1,
                    price=ep + 2.0,
                    time=sample_ctx.timestamp - pd.Timedelta(minutes=15),
                    details={"displacement": True},
                ),
                EvidenceRef(
                    evidence_id=make_evidence_id("order_block", "swing", sample_ctx.bar_index - 2, f"ob_{k}"),
                    kind="order_block",
                    bar_index=sample_ctx.bar_index - 2,
                    price=ep,
                    time=sample_ctx.timestamp - pd.Timedelta(minutes=30),
                    details={"top": ep + 1.0, "bottom": ep - 1.0, "quality": "premium_candidate"},
                ),
            ),
            expiry_bar=sample_ctx.bar_index + 10,
        )
        synth_candidates.append(setup)

    # Gate benchmark
    t0 = time.perf_counter()
    evals = gate.evaluate_registry_output(
        {"S05": tuple(synth_candidates)},
        sample_ctx,
        sample_regime,
        strat_registry.profiles,
        cooldown_book=cooldown,
    )
    t_gate = time.perf_counter() - t0
    print(f"4. EligibilityGate:      {t_gate*1000:8.2f} ms for 1,000 candidates ({t_gate/1000*1e6:6.2f} us/cand)")

    # Confluence batch assembly benchmark
    t0 = time.perf_counter()
    conf_batch = build_confluence_batch(evals, sample_regime, sample_ctx)
    t_conf = time.perf_counter() - t0
    print(f"5. Confluence Assembly:  {t_conf*1000:8.2f} ms for 1,000 candidates ({t_conf/1000*1e6:6.2f} us/cand)")

    # Selector benchmark
    t0 = time.perf_counter()
    sel_out = selector.select(conf_batch, sample_ctx)
    t_sel = time.perf_counter() - t0
    print(f"6. Selector:             {t_sel*1000:8.4f} ms for batch selection")

    # 7. ExecutionKernel benchmark
    kernel = ExecutionKernel(
        initial_capital=10000.0,
        lot_size=0.01,
        spread_val=0.20,
        commission_per_side=0.05,
    )
    t0 = time.perf_counter()
    for i in range(n_bars):
        row = bars_df.iloc[i]
        b = ExecutionBar(
            bar_index=i,
            timestamp=int(row["time"].timestamp()),
            time_value=str(row["time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
        kernel.process_intrabar(b)
    t_kernel = time.perf_counter() - t0
    print(f"7. ExecutionKernel:      {t_kernel*1000:8.2f} ms ({t_kernel/n_bars*1e6:6.2f} us/bar)")

    return {
        "context_builder_ms": round(t_context * 1000, 2),
        "regime_ms": round(t_regime * 1000, 2),
        "registry_ms": round(t_registry * 1000, 2),
        "gate_1k_ms": round(t_gate * 1000, 2),
        "confluence_1k_ms": round(t_conf * 1000, 2),
        "selector_ms": round(t_sel * 1000, 4),
        "kernel_ms": round(t_kernel * 1000, 2),
    }


# ---------------------------------------------------------------------------
# End-to-End Coordinator Benchmarks
# ---------------------------------------------------------------------------

def run_end_to_end_benchmark(
    name: str,
    bars_df: pd.DataFrame,
    htf_events: List[StructureEvent],
    mock_strategy: Any = None,
    cooldown_bars: int = 2,
    min_rr: float = 1.5,
) -> dict[str, Any]:
    """Benchmark full SMCBacktestCoordinator end-to-end on 10,000 bars."""
    print(f"\n==================================================================")
    print(f"End-to-End Benchmark Scenario: {name}")
    print(f"Bars: {len(bars_df):,}, HTF Events: {len(htf_events)}")
    print(f"==================================================================")

    exec_cfg = ExecutionConfig(
        lot_size=0.01,
        spread_points=20.0,
        commission_per_lot=5.0,
        allow_short=True,
        min_rr_fallback=min_rr,
    )
    ctx_cfg = ContextBuilderConfig(timeframe="M15")

    strategies = (mock_strategy,) if mock_strategy else None
    mode = "smc_s05" if mock_strategy else "smc_wave1"

    coordinator = SMCBacktestCoordinator(
        execution_config=exec_cfg,
        context_config=ctx_cfg,
        cooldown_bars=cooldown_bars,
        mode=mode,
        strategies=strategies,
        htf_events=htf_events,
        initial_capital=10000.0,
    )

    # Warm-up phase (first 100 bars)
    warmup_n = min(100, len(bars_df))
    t_warmup_start = time.perf_counter()
    for i in range(warmup_n):
        coordinator.step(bars_df.iloc[i], is_last_bar=False)
    t_warmup = time.perf_counter() - t_warmup_start
    print(f"Warm-up ({warmup_n} bars): {t_warmup*1000:.2f} ms ({t_warmup/warmup_n*1e6:.2f} us/bar)")

    # Reset coordinator for clean full run
    coordinator.reset()

    # Step-by-step timed execution to record latencies
    latencies = []
    n_bars = len(bars_df)
    t0_full = time.perf_counter()

    for i in range(n_bars):
        row = bars_df.iloc[i]
        is_last = (i == n_bars - 1)
        t_step_0 = time.perf_counter()
        coordinator.step(row, is_last_bar=is_last)
        t_step = time.perf_counter() - t_step_0
        latencies.append(t_step)

    t_total = time.perf_counter() - t0_full

    # Statistical metrics (skipping warm-up 100 bars for steady-state)
    steady_state_latencies = np.array(latencies[warmup_n:]) * 1e6  # in microseconds
    median_us = float(np.median(steady_state_latencies))
    p95_us = float(np.percentile(steady_state_latencies, 95))
    p99_us = float(np.percentile(steady_state_latencies, 99))
    mean_us = float(np.mean(steady_state_latencies))

    # Results extraction
    total_decisions = len(coordinator._all_decisions)
    total_events = len(coordinator._all_events)
    total_trades = len(coordinator.kernel.trades)
    fills = [e for e in coordinator._all_events if (e.event_type.value if hasattr(e.event_type, "value") else e.event_type) == "ORDER_FILLED"]

    print(f"\n--- Benchmark Results ---")
    print(f"Total Time:             {t_total:.3f} s ({len(bars_df)/t_total:.1f} bars/sec)")
    print(f"Warm-up Time (100 bars):{t_warmup*1000:.2f} ms")
    print(f"Steady-State Mean:      {mean_us:.2f} us/bar")
    print(f"Steady-State Median:    {median_us:.2f} us/bar")
    print(f"Steady-State P95:       {p95_us:.2f} us/bar")
    print(f"Steady-State P99:       {p99_us:.2f} us/bar")
    print(f"Total Decisions:        {total_decisions}")
    print(f"Total Execution Events: {total_events}")
    print(f"Total Fills:            {len(fills)}")
    print(f"Total Trades:           {total_trades}")
    print(f"Final Balance:          ${coordinator.kernel.balance:.2f}")

    return {
        "scenario": name,
        "total_bars": n_bars,
        "total_time_s": round(t_total, 4),
        "bars_per_sec": round(n_bars / t_total, 1),
        "warmup_ms": round(t_warmup * 1000, 2),
        "median_us": round(median_us, 2),
        "p95_us": round(p95_us, 2),
        "p99_us": round(p99_us, 2),
        "total_decisions": total_decisions,
        "total_events": total_events,
        "total_fills": len(fills),
        "total_trades": total_trades,
    }


def main():
    print("==================================================================")
    print("        SMC BACKTEST PIPELINE PERFORMANCE EVIDENCE (T53.9.5)       ")
    print("==================================================================")
    print(f"Python Version:   {platform.python_version()} ({sys.executable})")
    print(f"OS Platform:      {platform.platform()}")
    print(f"Processor:        {platform.processor() or 'Standard CPU'}")
    print(f"Timestamp UTC:    {datetime.datetime.now(datetime.timezone.utc).isoformat()}")

    # 1. Generate 10,000 synthetic bars and 100 HTF events
    print("\nGenerating 10,000 M15 bars and 100 HTF events...")
    bars_df = generate_synthetic_bars(10000)
    htf_events = generate_htf_events(bars_df, count=100)
    print(f"Generated {len(bars_df):,} bars (from {bars_df.iloc[0]['time']} to {bars_df.iloc[-1]['time']})")
    print(f"Generated {len(htf_events)} HTF events")

    # 2. Component Micro-benchmarks
    comp_res = run_component_benchmarks(bars_df)

    # 3. End-to-End Scenario 1: Standard Wave 1 Baseline (10,000 bars + 100 HTF events)
    s1_res = run_end_to_end_benchmark(
        name="Scenario 1: Wave 1 Baseline (10k bars + 100 HTF)",
        bars_df=bars_df,
        htf_events=htf_events,
    )

    # 4. End-to-End Scenario 2: High Candidate & Execution Activity (fills + trades)
    mock_strat = BenchHighActivityStrategy(strategy_id="S05", period=40)
    s2_res = run_end_to_end_benchmark(
        name="Scenario 2: High Activity (Mock Setups + Fills + Trades)",
        bars_df=bars_df,
        htf_events=htf_events,
        mock_strategy=mock_strat,
    )

    # 5. End-to-End Scenario 3: Zero HTF Events Baseline
    s3_res = run_end_to_end_benchmark(
        name="Scenario 3: Wave 1 without HTF events (10k bars)",
        bars_df=bars_df,
        htf_events=[],
    )

    print("\n=================================================================================================")
    print("                                PERFORMANCE SUMMARY TABLE                                        ")
    print("=================================================================================================")
    print(f"{'Scenario':<48} | {'Bars':<6} | {'Total(s)':<8} | {'Bars/s':<8} | {'Median(us)':<10} | {'P95(us)':<8} | {'P99(us)':<8}")
    print("-" * 105)
    for res in [s1_res, s2_res, s3_res]:
        print(f"{res['scenario']:<48} | {res['total_bars']:<6} | {res['total_time_s']:<8} | {res['bars_per_sec']:<8} | {res['median_us']:<10} | {res['p95_us']:<8} | {res['p99_us']:<8}")
    print("=================================================================================================")
    print(f"\nComponent Latency Summary for 10,000 bars:")
    for k, v in comp_res.items():
        print(f"  - {k:<22}: {v} ms")
    print("\nPerformance evidence collection completed successfully.")


if __name__ == "__main__":
    main()
