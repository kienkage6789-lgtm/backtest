"""
scratch/probe_qc_t53_9_2.py
----------------------------
Independent verification probe script for T53.9.2 QC compliance.
Executes real assertions covering all 5 QC findings and legacy invariants.
Returns exit code 0 if all assertions pass, non-zero if any assertion fails.
Run via module syntax from repository root:
    .\\.venv\\Scripts\\python.exe -m scratch.probe_qc_t53_9_2
"""

from __future__ import annotations

from enum import Enum
import json
import math
import sys
from dataclasses import dataclass
from types import MappingProxyType
from unittest.mock import patch

import numpy as np
import pandas as pd

from engine.execution_kernel import (
    ExecutionBar,
    ExecutionKernel,
    KernelTransition,
    OpenInstruction,
    PositionState,
    _deep_freeze,
    _deep_thaw,
)
from engine.backtest_engine import BacktestEngine
from engine.strategies import StrategyRegistry


def main() -> int:
    print("=== RUNNING INDEPENDENT QC PROBES (T53.9.2) ===")

    # -------------------------------------------------------------------------
    # Probe 1: NumPy generic structured scalar (np.void) with nested mutable
    # -------------------------------------------------------------------------
    source_list = [1, 2]
    scalar_void = np.array((source_list,), dtype=[("payload", object)])[()]
    inst_p1 = OpenInstruction(
        action="OPEN_OR_REVERSE",
        direction="BUY",
        entry_price=100.0,
        source="legacy",
        metadata={"scalar": scalar_void},
    )
    source_list.append(999)
    assert inst_p1.metadata["scalar"][0] == (1, 2), "Probe 1 FAIL: source list mutated frozen snapshot!"
    assert isinstance(inst_p1.metadata["scalar"][0], tuple), "Probe 1 FAIL: payload is not tuple!"
    np_generic_nested_alias = False

    # Normal NumPy scalars normalized
    assert type(_deep_freeze(np.int64(42))) is int, "Probe 1 FAIL: np.int64 not converted to int"
    assert type(_deep_freeze(np.float64(3.14))) is float, "Probe 1 FAIL: np.float64 not converted to float"
    assert type(_deep_freeze(np.bool_(True))) is bool, "Probe 1 FAIL: np.bool_ not converted to bool"

    # NumPy containers rejected fail-closed
    try:
        _deep_freeze(np.array([1, 2]))
        assert False, "Probe 1 FAIL: np.ndarray should raise TypeError"
    except TypeError:
        pass

    # Cycle detection
    cycle_list = []
    cycle_list.append(cycle_list)
    try:
        _deep_freeze(cycle_list)
        assert False, "Probe 1 FAIL: circular reference should raise TypeError"
    except TypeError:
        pass

    # -------------------------------------------------------------------------
    # Probe 2: Mapping with tuple key handled cleanly in thaw and close
    # -------------------------------------------------------------------------
    k2 = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.0, commission_per_side=0.0)
    time_map = {("session", 1): "entry_t1"}
    bar_tuple_key1 = ExecutionBar(1, 1000, time_map, 100.0, 105.0, 95.0, 100.0)
    k2.process_open(bar_tuple_key1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0))
    bar_tuple_key2 = ExecutionBar(2, 2000, time_map, 105.0, 110.0, 100.0, 105.0)
    trans_close2 = k2.process_open(bar_tuple_key2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
    assert trans_close2.status == "CLOSED", "Probe 2 FAIL: close-only failed with tuple key"
    assert len(k2.trades) == 1, "Probe 2 FAIL: trade not recorded"
    assert k2.trades[0]["entry_time"][("session", 1)] == "entry_t1", "Probe 2 FAIL: tuple key not preserved in trades"
    tuple_key_close_atomic = True

    # -------------------------------------------------------------------------
    # Probe 3: Atomicity when close/export fails & retry no double count
    # -------------------------------------------------------------------------
    k3 = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.0, commission_per_side=0.0)
    bar3_1 = ExecutionBar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
    k3.process_open(bar3_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0))

    pre_balance = k3.balance
    pre_position = k3.position
    pre_counter = k3._trade_counter
    pre_trades_len = len(k3._trades)
    pre_markers_len = len(k3._markers)

    bar3_2 = ExecutionBar(2, 2000, "t2", 110.0, 115.0, 105.0, 110.0)

    orig_freeze = k3._close_position.__globals__["_deep_freeze"]

    def exploding_freeze(val, _seen=None):
        if isinstance(val, dict) and "trade_id" in val:
            raise RuntimeError("Injected freeze failure")
        return orig_freeze(val, _seen=_seen)

    with patch.dict(k3._close_position.__globals__, {"_deep_freeze": exploding_freeze}):
        try:
            k3.process_open(bar3_2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
            assert False, "Probe 3 FAIL: Expected injected failure to raise"
        except RuntimeError:
            pass

    assert k3.balance == pre_balance, "Probe 3 FAIL: balance mutated after close failure!"
    assert k3.position is pre_position, "Probe 3 FAIL: position mutated after close failure!"
    assert k3._trade_counter == pre_counter, "Probe 3 FAIL: trade_counter mutated after close failure!"
    assert len(k3._trades) == pre_trades_len, "Probe 3 FAIL: trades mutated after close failure!"
    assert len(k3._markers) == pre_markers_len, "Probe 3 FAIL: markers mutated after close failure!"
    close_failure_state_unchanged = True

    # Retry without error: must succeed and not double-count
    retry_trans = k3.process_open(bar3_2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
    assert retry_trans.status == "CLOSED", "Probe 3 FAIL: Retry close failed"
    assert k3.balance == pre_balance + 10.0, "Probe 3 FAIL: Balance incorrect after retry"
    assert k3._trade_counter == 1, "Probe 3 FAIL: Counter double-incremented"
    assert len(k3.trades) == 1, "Probe 3 FAIL: Double trades recorded"
    assert k3.trades[0]["trade_id"] == 1, "Probe 3 FAIL: Trade ID mismatch"
    retry_no_double_count = True

    # -------------------------------------------------------------------------
    # Probe 4: Negative initial capital return percentage in legacy mode
    # -------------------------------------------------------------------------
    k4 = ExecutionKernel(
        initial_capital=-100.0,
        lot_size=1.0,
        contract_size=1.0,
        spread_val=0.0,
        commission_per_side=0.0,
        validation_mode="legacy",
    )
    bar4_1 = ExecutionBar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
    k4.process_open(bar4_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0))
    bar4_2 = ExecutionBar(2, 2000, "t2", 101.0, 105.0, 95.0, 101.0)
    k4.process_open(bar4_2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
    assert k4.trades[0]["pnl"] == 1.0, "Probe 4 FAIL: PnL should be 1.0"
    assert k4.trades[0]["return_pct"] == -1.0, f"Probe 4 FAIL: return_pct should be -1.0, got {k4.trades[0]['return_pct']}"

    # Facade test with BacktestEngine
    orig_gen = StrategyRegistry.generate_signals
    try:
        StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
        df4 = pd.DataFrame([
            {"time": "2026-01-01 10:00:00", "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
            {"time": "2026-01-01 10:01:00", "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0},
            {"time": "2026-01-01 10:02:00", "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.10},
        ])
        be4 = BacktestEngine(initial_capital=-100.0, lot_size=0.1, contract_size=100.0, spread_points=0.0, commission_per_lot=0.0)
        res4 = be4.run(df4, "dummy", {})
        assert res4["metrics"]["net_profit"] == 1.0, "Probe 4 FAIL: facade net_profit"
        assert res4["metrics"]["return_pct"] == -1.0, f"Probe 4 FAIL: facade return_pct expected -1.0, got {res4['metrics']['return_pct']}"
        negative_capital_return_pct_matches_legacy = True
    finally:
        StrategyRegistry.generate_signals = orig_gen

    # -------------------------------------------------------------------------
    # Probe 5: Timestamp preserved in defensive copy; JSON serialization contract
    # -------------------------------------------------------------------------
    k5 = ExecutionKernel()
    ts_time = pd.Timestamp("2026-01-01 10:00:00")
    bar5_1 = ExecutionBar(1, 1000, ts_time, 2000.0, 2010.0, 1990.0, 2000.0)
    k5.process_open(bar5_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
    bar5_2 = ExecutionBar(2, 2000, ts_time, 2010.0, 2020.0, 2000.0, 2010.0)
    k5.force_close(bar5_2)

    # Defensive copy isolation
    trades_copy = k5.trades
    trades_copy[0]["pnl"] = 888888.0
    assert k5.trades[0]["pnl"] != 888888.0, "Probe 5 FAIL: trades getter copy leaked internal state"
    getter_copy_isolated = True

    # Timestamp preserved
    assert k5.trades[0]["entry_time"] == ts_time, "Probe 5 FAIL: Timestamp not preserved"
    # Calling json.dumps directly raises TypeError as documented (caller responsibility)
    try:
        json.dumps(k5.trades)
        assert False, "Probe 5 FAIL: Timestamp should not be JSON serializable without adapter"
    except TypeError:
        pass

    # -------------------------------------------------------------------------
    # Probe 6: Existing Probes - Frozen dataclass isolation
    # -------------------------------------------------------------------------
    @dataclass(frozen=True)
    class NestedFrozen:
        items: list
        mapping: dict

    raw_list = [10, 20]
    raw_dict = {"k": 1}
    frozen_obj = NestedFrozen(items=raw_list, mapping=raw_dict)
    inst_fd = OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20, metadata={"obj": frozen_obj})
    raw_list.append(99)
    raw_dict["k"] = 88
    assert inst_fd.metadata["obj"].items == (10, 20), "Probe 6 FAIL: frozen dataclass items mutated"
    assert inst_fd.metadata["obj"].mapping["k"] == 1, "Probe 6 FAIL: frozen dataclass mapping mutated"

    # -------------------------------------------------------------------------
    # Probe 7: Existing Probes - Legacy zero and negative entry prices
    # -------------------------------------------------------------------------
    inst_zero = OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=0.0, sl_price=-1.0, tp_price=1.0, source="legacy")
    pos_zero = PositionState(direction="BUY", entry_price=0.0, entry_timestamp=1000, entry_time_value="t", sl_price=-1.0, tp_price=1.0, multiplier=0.0, round_trip_commission=0.0, source="legacy")
    assert inst_zero.entry_price == 0.0 and pos_zero.entry_price == 0.0, "Probe 7 FAIL: zero entry price"

    inst_neg = OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=-5.0, sl_price=-10.0, tp_price=0.0, source="legacy")
    pos_neg = PositionState(direction="BUY", entry_price=-5.0, entry_timestamp=1000, entry_time_value="t", sl_price=-10.0, tp_price=0.0, multiplier=10.0, round_trip_commission=1.0, source="legacy")
    assert inst_neg.entry_price == -5.0 and pos_neg.entry_price == -5.0, "Probe 7 FAIL: neg entry price"

    # -------------------------------------------------------------------------
    # Probe 8: Existing Probes - Wave 1 strictness
    # -------------------------------------------------------------------------
    try:
        OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=0.0, sl_price=90.0, tp_price=110.0, source="wave1")
        assert False, "Probe 8 FAIL: Wave 1 zero entry accepted"
    except ValueError:
        pass
    try:
        ExecutionKernel(initial_capital=0.0, validation_mode="wave1")
        assert False, "Probe 8 FAIL: Wave 1 zero capital accepted"
    except ValueError:
        pass

    # -------------------------------------------------------------------------
    # Probe 9: Existing Probes - Transition immutability
    # -------------------------------------------------------------------------
    k9 = ExecutionKernel()
    bar9_1 = ExecutionBar(1, 1000, "t1", 2000.0, 2010.0, 1990.0, 2005.0)
    tr_open = k9.process_open(bar9_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
    try:
        tr_open.markers[0]["text"] = "MUTATED"
        assert False, "Probe 9 FAIL: marker in transition is mutable"
    except TypeError:
        pass
    bar9_2 = ExecutionBar(2, 2000, "t2", 2010.0, 2020.0, 2000.0, 2015.0)
    tr_close = k9.process_open(bar9_2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
    try:
        tr_close.closed_trade["pnl"] = 999999.0
        assert False, "Probe 9 FAIL: closed_trade in transition is mutable"
    except TypeError:
        pass
    transition_alias_blocked = True

    # -------------------------------------------------------------------------
    # Probe 10: Enum fail-closed policy (mutable alias blocked)
    # -------------------------------------------------------------------------
    class MutableEnum(Enum):
        TOKEN = [1]

    class NormalEnum(Enum):
        VAL = "A"

    try:
        OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={"enum": MutableEnum.TOKEN},
        )
        assert False, "Probe 10 FAIL: MutableEnum should raise TypeError"
    except TypeError:
        pass

    try:
        OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={"enum": NormalEnum.VAL},
        )
        assert False, "Probe 10 FAIL: NormalEnum should raise TypeError under fail-closed policy"
    except TypeError:
        pass
    enum_mutable_alias_blocked = True

    # -------------------------------------------------------------------------
    # Probe 11: Primitive scalar subclass alias blocked & exact types pass
    # -------------------------------------------------------------------------
    class MutableInt(int):
        pass

    class MutableStr(str):
        pass

    m_int = MutableInt(7)
    m_int.payload = [1]
    try:
        OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={"v": m_int},
        )
        assert False, "Probe 11 FAIL: MutableInt should raise TypeError"
    except TypeError:
        pass

    m_str = MutableStr("test")
    m_str.payload = [1]
    try:
        OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={"v": m_str},
        )
        assert False, "Probe 11 FAIL: MutableStr should raise TypeError"
    except TypeError:
        pass

    # Exact primitive types pass
    inst_prim = OpenInstruction(
        action="OPEN_OR_REVERSE",
        direction="BUY",
        entry_price=10.0,
        metadata={"none": None, "b": True, "i": 1, "f": 1.5, "s": "ok", "by": b"bin"},
    )
    assert inst_prim.metadata["i"] == 1
    scalar_subclass_alias_blocked = True

    # -------------------------------------------------------------------------
    # Probe 12: Gross PnL arithmetic overflow atomic rollback (CLOSE_ONLY)
    # -------------------------------------------------------------------------
    k12 = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
    bar12_1 = ExecutionBar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
    k12.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
    snap12 = {
        "balance": k12.balance,
        "position": k12.position,
        "counter": k12._trade_counter,
        "trades": len(k12._trades),
        "markers": len(k12._markers),
    }
    bar12_2 = ExecutionBar(2, 2000, "t2", 20.0, 25.0, 15.0, 20.0)
    try:
        k12.process_open(bar12_2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        assert False, "Probe 12 FAIL: gross_pnl overflow should raise ValueError"
    except ValueError as ex:
        assert "gross_pnl" in str(ex), f"Probe 12 unexpected message: {ex}"
    assert k12.balance == snap12["balance"], "Probe 12 FAIL: balance mutated"
    assert k12.position is snap12["position"], "Probe 12 FAIL: position mutated"
    assert k12._trade_counter == snap12["counter"], "Probe 12 FAIL: counter mutated"
    assert len(k12._trades) == snap12["trades"], "Probe 12 FAIL: trades mutated"
    assert len(k12._markers) == snap12["markers"], "Probe 12 FAIL: markers mutated"
    gross_pnl_overflow_rollback = True

    # -------------------------------------------------------------------------
    # Probe 13: Balance overflow rejected without committing invalid state
    # -------------------------------------------------------------------------
    k13 = ExecutionKernel(initial_capital=1e308, lot_size=1.0, contract_size=1.0, validation_mode="legacy")
    k13.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=0.0, source="legacy"))
    snap13 = {
        "balance": k13.balance,
        "position": k13.position,
        "counter": k13._trade_counter,
        "trades": len(k13._trades),
        "markers": len(k13._markers),
    }
    bar13_huge = ExecutionBar(2, 2000, "t2", 1e308, 1e308, 0.0, 1e308)
    try:
        k13.process_open(bar13_huge, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        assert False, "Probe 13 FAIL: balance overflow should raise ValueError"
    except ValueError as ex:
        assert "prospective_balance" in str(ex), f"Probe 13 unexpected message: {ex}"
    assert k13.balance == snap13["balance"], "Probe 13 FAIL: balance mutated"
    assert k13.position is snap13["position"], "Probe 13 FAIL: position mutated"
    assert math.isfinite(k13.balance), "Probe 13 FAIL: balance is not finite"
    balance_overflow_rejected = True

    # -------------------------------------------------------------------------
    # Probe 14: Intrabar SL/TP overflow atomic rollback
    # -------------------------------------------------------------------------
    k14 = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
    k14.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=90.0))
    snap14 = {
        "balance": k14.balance,
        "position": k14.position,
        "counter": k14._trade_counter,
        "trades": len(k14._trades),
        "markers": len(k14._markers),
    }
    bar14_sl = ExecutionBar(2, 2000, "t2", 95.0, 100.0, 85.0, 90.0)
    try:
        k14.process_intrabar(bar14_sl)
        assert False, "Probe 14 FAIL: Intrabar SL overflow should raise ValueError"
    except ValueError:
        pass
    assert k14.balance == snap14["balance"], "Probe 14 FAIL: balance mutated after SL overflow"
    assert k14.position is snap14["position"], "Probe 14 FAIL: position mutated after SL overflow"
    assert k14._trade_counter == snap14["counter"], "Probe 14 FAIL: counter mutated after SL overflow"
    assert len(k14._trades) == snap14["trades"], "Probe 14 FAIL: trades mutated after SL overflow"
    assert len(k14._markers) == snap14["markers"], "Probe 14 FAIL: markers mutated after SL overflow"
    intrabar_overflow_rollback = True

    # -------------------------------------------------------------------------
    # Probe 15: Reversal arithmetic overflow atomic rollback
    # -------------------------------------------------------------------------
    k15 = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
    k15.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
    snap15 = {
        "balance": k15.balance,
        "position": k15.position,
        "counter": k15._trade_counter,
        "trades": len(k15._trades),
        "markers": len(k15._markers),
    }
    bar15_rev = ExecutionBar(2, 2000, "t2", 20.0, 25.0, 15.0, 20.0)
    try:
        k15.process_open(bar15_rev, OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="SELL",
            entry_price=20.0,
            sl_price=30.0,
            tp_price=10.0,
            source="legacy",
        ))
        assert False, "Probe 15 FAIL: Reversal overflow should raise ValueError"
    except ValueError:
        pass
    assert k15.balance == snap15["balance"], "Probe 15 FAIL: balance mutated after reversal failure"
    assert k15.position is snap15["position"], "Probe 15 FAIL: position mutated after reversal failure"
    assert k15.position.direction == "BUY", "Probe 15 FAIL: old position direction lost"
    assert k15._trade_counter == snap15["counter"], "Probe 15 FAIL: counter mutated"
    assert len(k15._trades) == snap15["trades"], "Probe 15 FAIL: new trade committed"
    assert len(k15._markers) == snap15["markers"], "Probe 15 FAIL: marker committed"
    reversal_overflow_rollback = True

    # -------------------------------------------------------------------------
    # Probe 16: Force close arithmetic overflow atomic rollback
    # -------------------------------------------------------------------------
    k16 = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
    k16.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
    snap16 = {
        "balance": k16.balance,
        "position": k16.position,
        "counter": k16._trade_counter,
        "trades": len(k16._trades),
        "markers": len(k16._markers),
    }
    bar16_fc = ExecutionBar(2, 2000, "t2", 30.0, 35.0, 25.0, 30.0)
    try:
        k16.force_close(bar16_fc)
        assert False, "Probe 16 FAIL: force_close overflow should raise ValueError"
    except ValueError:
        pass
    assert k16.balance == snap16["balance"], "Probe 16 FAIL: balance mutated"
    assert k16.position is snap16["position"], "Probe 16 FAIL: position cleared"
    assert k16._trade_counter == snap16["counter"], "Probe 16 FAIL: counter mutated"
    assert len(k16._trades) == snap16["trades"], "Probe 16 FAIL: trades mutated"
    assert len(k16._markers) == snap16["markers"], "Probe 16 FAIL: markers mutated"
    force_close_overflow_rollback = True

    # -------------------------------------------------------------------------
    # Probe 17: Mark to market overflow rejected without mutating state
    # -------------------------------------------------------------------------
    k17 = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
    k17.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
    bal_17 = k17.balance
    pos_17 = k17.position
    try:
        k17.mark_to_market(20.0)
        assert False, "Probe 17 FAIL: mark_to_market overflow should raise ValueError"
    except ValueError as ex:
        assert "floating_gross" in str(ex), f"Probe 17 unexpected message: {ex}"
    assert k17.balance == bal_17, "Probe 17 FAIL: balance mutated"
    assert k17.position is pos_17, "Probe 17 FAIL: position mutated"
    mark_to_market_overflow_rejected = True

    # -------------------------------------------------------------------------
    # Probe 18: Short intrabar ask_high overflow rejected fail-fast
    # -------------------------------------------------------------------------
    k18 = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
    k18.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))
    snap18 = {
        "balance": k18.balance,
        "position": k18.position,
        "counter": k18._trade_counter,
        "trades": len(k18._trades),
        "markers": len(k18._markers),
    }
    bar18_high = ExecutionBar(2, 2000, "t2", 100.0, 1e308, 95.0, 100.0)
    try:
        k18.process_intrabar(bar18_high)
        assert False, "Probe 18 FAIL: ask_high overflow should raise ValueError"
    except ValueError as ex:
        assert "ask_high" in str(ex), f"Probe 18 unexpected message: {ex}"
    assert k18.balance == snap18["balance"], "Probe 18 FAIL: balance mutated"
    assert k18.position is snap18["position"], "Probe 18 FAIL: position mutated"
    assert k18._trade_counter == snap18["counter"], "Probe 18 FAIL: counter mutated"
    assert len(k18._trades) == snap18["trades"], "Probe 18 FAIL: trades mutated"
    assert len(k18._markers) == snap18["markers"], "Probe 18 FAIL: markers mutated"
    short_ask_high_overflow_rejected = True

    # -------------------------------------------------------------------------
    # Probe 19: Short intrabar ask_low overflow rejected fail-fast
    # -------------------------------------------------------------------------
    k19 = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=-1e308, commission_per_side=0.50, validation_mode="legacy")
    k19.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))
    snap19 = {
        "balance": k19.balance,
        "position": k19.position,
        "counter": k19._trade_counter,
        "trades": len(k19._trades),
        "markers": len(k19._markers),
    }
    bar19_low = ExecutionBar(2, 2000, "t2", 100.0, 105.0, -1e308, 100.0)
    try:
        k19.process_intrabar(bar19_low)
        assert False, "Probe 19 FAIL: ask_low overflow should raise ValueError"
    except ValueError as ex:
        assert "ask_low" in str(ex), f"Probe 19 unexpected message: {ex}"
    assert k19.balance == snap19["balance"], "Probe 19 FAIL: balance mutated"
    assert k19.position is snap19["position"], "Probe 19 FAIL: position mutated"
    assert k19._trade_counter == snap19["counter"], "Probe 19 FAIL: counter mutated"
    assert len(k19._trades) == snap19["trades"], "Probe 19 FAIL: trades mutated"
    assert len(k19._markers) == snap19["markers"], "Probe 19 FAIL: markers mutated"
    short_ask_low_overflow_rejected = True
    short_intrabar_overflow_state_unchanged = True

    # -------------------------------------------------------------------------
    # Probe 20: Short reversal overflow atomic rollback
    # -------------------------------------------------------------------------
    k20 = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
    k20.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))
    snap20 = {
        "balance": k20.balance,
        "position": k20.position,
        "counter": k20._trade_counter,
        "trades": len(k20._trades),
        "markers": len(k20._markers),
    }
    bar20_rev = ExecutionBar(2, 2000, "t2", 1e308, 1e308, 95.0, 1e308)
    try:
        k20.process_open(bar20_rev, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=90.0, tp_price=110.0, source="legacy"))
        assert False, "Probe 20 FAIL: reversal overflow should raise ValueError"
    except ValueError as ex:
        assert "exit_price" in str(ex), f"Probe 20 unexpected message: {ex}"
    assert k20.balance == snap20["balance"], "Probe 20 FAIL: balance mutated"
    assert k20.position is snap20["position"], "Probe 20 FAIL: position mutated"
    assert k20.position.direction == "SELL", "Probe 20 FAIL: direction mutated"
    assert k20._trade_counter == snap20["counter"], "Probe 20 FAIL: counter mutated"
    assert len(k20._trades) == snap20["trades"], "Probe 20 FAIL: trades mutated"
    assert len(k20._markers) == snap20["markers"], "Probe 20 FAIL: markers mutated"
    short_reversal_overflow_state_unchanged = True

    # -------------------------------------------------------------------------
    # Probe 21: Short force close overflow atomic rollback and retry
    # -------------------------------------------------------------------------
    k21 = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
    k21.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))
    snap21 = {
        "balance": k21.balance,
        "position": k21.position,
        "counter": k21._trade_counter,
        "trades": len(k21._trades),
        "markers": len(k21._markers),
    }
    bar21_fc = ExecutionBar(2, 2000, "t2", 100.0, 105.0, 95.0, 1e308)
    try:
        k21.force_close(bar21_fc)
        assert False, "Probe 21 FAIL: force_close overflow should raise ValueError"
    except ValueError as ex:
        assert "exit_price" in str(ex), f"Probe 21 unexpected message: {ex}"
    assert k21.balance == snap21["balance"], "Probe 21 FAIL: balance mutated"
    assert k21.position is snap21["position"], "Probe 21 FAIL: position mutated"
    assert k21._trade_counter == snap21["counter"], "Probe 21 FAIL: counter mutated"
    assert len(k21._trades) == snap21["trades"], "Probe 21 FAIL: trades mutated"
    assert len(k21._markers) == snap21["markers"], "Probe 21 FAIL: markers mutated"
    # Retry with valid bar
    k21.spread_val = 0.20
    bar21_retry = ExecutionBar(3, 3000, "t3", 100.0, 105.0, 95.0, 100.0)
    tr21 = k21.force_close(bar21_retry)
    assert tr21.status == "FORCED_CLOSED", "Probe 21 FAIL: retry did not close"
    assert k21.position is None, "Probe 21 FAIL: position not cleared on retry"
    assert k21._trade_counter == 1, "Probe 21 FAIL: counter not 1"
    assert len(k21.trades) == 1, "Probe 21 FAIL: trades len not 1"
    short_force_close_overflow_state_unchanged = True

    # -------------------------------------------------------------------------
    # Probe 22: Short mark-to-market overflow rejected without mutating state
    # -------------------------------------------------------------------------
    k22 = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
    k22.process_open(bar12_1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, source="legacy"))
    bal_22 = k22.balance
    pos_22 = k22.position
    try:
        k22.mark_to_market(1e308)
        assert False, "Probe 22 FAIL: mark_to_market overflow should raise ValueError"
    except ValueError as ex:
        assert "close_ask" in str(ex), f"Probe 22 unexpected message: {ex}"
    assert k22.balance == bal_22, "Probe 22 FAIL: balance mutated"
    assert k22.position is pos_22, "Probe 22 FAIL: position mutated"
    short_mark_to_market_overflow_rejected = True

    # -------------------------------------------------------------------------
    # Output required summary lines
    # -------------------------------------------------------------------------
    print(f"np_generic_nested_alias={np_generic_nested_alias}")
    print(f"tuple_key_close_atomic={tuple_key_close_atomic}")
    print(f"close_failure_state_unchanged={close_failure_state_unchanged}")
    print(f"retry_no_double_count={retry_no_double_count}")
    print(f"negative_capital_return_pct_matches_legacy={negative_capital_return_pct_matches_legacy}")
    print(f"transition_alias_blocked={transition_alias_blocked}")
    print(f"getter_copy_isolated={getter_copy_isolated}")

    print(f"enum_mutable_alias_blocked={enum_mutable_alias_blocked}")
    print(f"scalar_subclass_alias_blocked={scalar_subclass_alias_blocked}")
    print(f"gross_pnl_overflow_rollback={gross_pnl_overflow_rollback}")
    print(f"balance_overflow_rejected={balance_overflow_rejected}")
    print(f"intrabar_overflow_rollback={intrabar_overflow_rollback}")
    print(f"reversal_overflow_rollback={reversal_overflow_rollback}")
    print(f"force_close_overflow_rollback={force_close_overflow_rollback}")
    print(f"mark_to_market_overflow_rejected={mark_to_market_overflow_rejected}")

    print(f"short_ask_high_overflow_rejected={short_ask_high_overflow_rejected}")
    print(f"short_ask_low_overflow_rejected={short_ask_low_overflow_rejected}")
    print(f"short_intrabar_overflow_state_unchanged={short_intrabar_overflow_state_unchanged}")
    print(f"short_reversal_overflow_state_unchanged={short_reversal_overflow_state_unchanged}")
    print(f"short_force_close_overflow_state_unchanged={short_force_close_overflow_state_unchanged}")
    print(f"short_mark_to_market_overflow_rejected={short_mark_to_market_overflow_rejected}")

    print("=== ALL INDEPENDENT QC PROBES PASSED (EXIT CODE 0) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
