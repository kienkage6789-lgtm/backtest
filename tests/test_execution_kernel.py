"""
tests/test_execution_kernel.py
------------------------------
Comprehensive unit test suite for ExecutionKernel and its domain models,
covering all 24 required test specifications from T53.9.2 plan.
"""

from datetime import date, datetime, time, timedelta
from enum import Enum
import json
import math
import unittest
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


def make_bar(
    bar_index: int = 1,
    timestamp: int = 1700000000,
    time_value: str = "2026-01-01 10:00:00",
    open_p: float = 2000.0,
    high_p: float = 2010.0,
    low_p: float = 1990.0,
    close_p: float = 2005.0,
) -> ExecutionBar:
    return ExecutionBar(
        bar_index=bar_index,
        timestamp=timestamp,
        time_value=time_value,
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
    )


class TestExecutionKernel(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = ExecutionKernel(
            initial_capital=10000.0,
            lot_size=0.1,
            contract_size=100.0,
            spread_val=0.20,
            commission_per_side=0.50,
        )

    # 1. Flat -> open BUY
    def test_01_flat_open_buy(self) -> None:
        bar = make_bar(open_p=2000.0)
        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=2000.20,
            sl_price=1990.0,
            tp_price=2020.0,
        )
        trans = self.kernel.process_open(bar, inst)
        self.assertEqual(trans.status, "OPENED")
        self.assertIsNone(trans.position_before)
        self.assertIsNotNone(trans.position_after)
        self.assertEqual(trans.position_after.direction, "BUY")
        self.assertEqual(trans.position_after.entry_price, 2000.20)
        self.assertEqual(self.kernel.position, trans.position_after)
        self.assertEqual(len(self.kernel.trades), 0)
        self.assertEqual(len(self.kernel.markers), 1)
        self.assertEqual(self.kernel.markers[0]["position"], "belowBar")

    # 2. Flat -> open SELL
    def test_02_flat_open_sell(self) -> None:
        bar = make_bar(open_p=2000.0)
        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="SELL",
            entry_price=2000.00,
            sl_price=2010.0,
            tp_price=1980.0,
        )
        trans = self.kernel.process_open(bar, inst)
        self.assertEqual(trans.status, "OPENED")
        self.assertIsNotNone(trans.position_after)
        self.assertEqual(trans.position_after.direction, "SELL")
        self.assertEqual(trans.position_after.entry_price, 2000.00)
        self.assertEqual(len(self.kernel.markers), 1)
        self.assertEqual(self.kernel.markers[0]["position"], "aboveBar")

    # 3. Same-direction signal doesn't open new position (no pyramiding)
    def test_03_same_direction_no_pyramiding(self) -> None:
        bar = make_bar()
        inst1 = OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20)
        self.kernel.process_open(bar, inst1)

        inst2 = OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2005.20)
        trans = self.kernel.process_open(bar, inst2)
        self.assertEqual(trans.status, "SAME_DIRECTION")
        self.assertEqual(self.kernel.position.entry_price, 2000.20)
        self.assertEqual(len(self.kernel.trades), 0)
        self.assertEqual(len(self.kernel.markers), 1)

    # 4. BUY -> SELL reversal valid (closes BUY at Open Bid, opens SELL at entry)
    def test_04_buy_to_sell_reversal_valid(self) -> None:
        bar1 = make_bar(bar_index=1, timestamp=1000, time_value="t1", open_p=2000.0)
        self.kernel.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))

        bar2 = make_bar(bar_index=2, timestamp=2000, time_value="t2", open_p=2010.0)
        rev_inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="SELL",
            entry_price=2010.0,
            sl_price=2020.0,
            tp_price=1990.0,
        )
        trans = self.kernel.process_open(bar2, rev_inst)
        self.assertEqual(trans.status, "REVERSED")
        # Closed trade checks
        self.assertIsNotNone(trans.closed_trade)
        # BUY closed at bar2.open (Bid = 2010.0)
        # gross = (2010.0 - 2000.20) * 10 = 98.0
        # net = 98.0 - 1.0 = 97.0
        self.assertAlmostEqual(trans.realized_net_pnl, 97.0)
        self.assertEqual(trans.closed_trade["exit_reason"], "Signal Reversal")
        self.assertEqual(trans.closed_trade["exit_time"], "t2")
        self.assertAlmostEqual(self.kernel.balance, 10097.0)
        # New position checks
        self.assertIsNotNone(self.kernel.position)
        self.assertEqual(self.kernel.position.direction, "SELL")
        self.assertEqual(self.kernel.position.entry_price, 2010.0)
        # 3 markers: BUY entry, BUY exit, SELL entry
        self.assertEqual(len(self.kernel.markers), 3)

    # 5. SELL -> BUY reversal valid (closes SELL at Open Ask, opens BUY at entry)
    def test_05_sell_to_buy_reversal_valid(self) -> None:
        bar1 = make_bar(bar_index=1, timestamp=1000, time_value="t1", open_p=2000.0)
        self.kernel.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=2000.0))

        bar2 = make_bar(bar_index=2, timestamp=2000, time_value="t2", open_p=1990.0)
        # SELL closes at Ask = Open(1990.0) + Spread(0.20) = 1990.20
        # BUY opens at Ask = 1990.20
        rev_inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=1990.20,
            sl_price=1980.0,
            tp_price=2010.0,
        )
        trans = self.kernel.process_open(bar2, rev_inst)
        self.assertEqual(trans.status, "REVERSED")
        # gross = (2000.0 - 1990.20) * 10 = 98.0
        # net = 98.0 - 1.0 = 97.0
        self.assertAlmostEqual(trans.realized_net_pnl, 97.0)
        self.assertEqual(self.kernel.position.direction, "BUY")
        self.assertEqual(self.kernel.position.entry_price, 1990.20)

    # 6. Invalid opposite instruction preserves position / balance / audit state (atomic reversal probe)
    def test_06_invalid_reversal_atomic_preserves_state(self) -> None:
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
        initial_balance = self.kernel.balance
        initial_trades = self.kernel.trades
        initial_markers = self.kernel.markers
        initial_pos = self.kernel.position

        bar2 = make_bar(open_p=2010.0)
        # Attempt an invalid reversal instruction (SELL with sl < entry)
        with self.assertRaises(ValueError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="SELL",
                entry_price=2010.0,
                sl_price=2000.0,  # Invalid: sl must be > entry for SELL!
                tp_price=1990.0,
            )

        # Confirm kernel state was completely untouched
        self.assertEqual(self.kernel.position, initial_pos)
        self.assertEqual(self.kernel.balance, initial_balance)
        self.assertEqual(self.kernel.trades, initial_trades)
        self.assertEqual(self.kernel.markers, initial_markers)

    # 7. CLOSE_ONLY closes active position and does not open new position
    def test_07_close_only_closes_position(self) -> None:
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))

        bar2 = make_bar(open_p=2010.0)
        close_inst = OpenInstruction(action="CLOSE_ONLY", direction="SELL")
        trans = self.kernel.process_open(bar2, close_inst)
        self.assertEqual(trans.status, "CLOSED")
        self.assertIsNone(self.kernel.position)
        self.assertEqual(len(self.kernel.trades), 1)
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Signal Reversal")

    # 8. CLOSE_ONLY when flat is no-op
    def test_08_close_only_when_flat_is_noop(self) -> None:
        bar = make_bar()
        trans = self.kernel.process_open(bar, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertEqual(trans.status, "NO_ACTION")
        self.assertIsNone(self.kernel.position)
        self.assertEqual(len(self.kernel.trades), 0)

    # 9. LONG dynamic SL
    def test_09_long_dynamic_sl(self) -> None:
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(
            bar1,
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                sl_price=1995.0,
                tp_price=2015.0,
            ),
        )

        bar2 = make_bar(open_p=1998.0, high_p=2002.0, low_p=1994.0, close_p=1997.0)
        trans = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans.status, "STOPPED")
        self.assertIsNone(self.kernel.position)
        self.assertEqual(len(self.kernel.trades), 1)
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Stop Loss")
        self.assertAlmostEqual(self.kernel.trades[0]["exit_price"], 1995.0)

    # 10. LONG dynamic TP
    def test_10_long_dynamic_tp(self) -> None:
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(
            bar1,
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                sl_price=1990.0,
                tp_price=2010.0,
            ),
        )

        bar2 = make_bar(open_p=2005.0, high_p=2012.0, low_p=2002.0, close_p=2008.0)
        trans = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans.status, "TARGETED")
        self.assertIsNone(self.kernel.position)
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Take Profit")
        self.assertAlmostEqual(self.kernel.trades[0]["exit_price"], 2010.0)

    # 11. SHORT SL on Ask
    def test_11_short_sl_on_ask(self) -> None:
        # Spread = 0.20. SHORT entry 2000.00. SL = 2005.00 (Ask).
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(
            bar1,
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="SELL",
                entry_price=2000.00,
                sl_price=2005.00,
            ),
        )

        # Bid High = 2004.90. Ask High = 2004.90 + 0.20 = 2005.10 >= 2005.00 -> SL triggers!
        bar2 = make_bar(high_p=2004.90, low_p=1998.0)
        trans = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans.status, "STOPPED")
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Stop Loss")
        self.assertAlmostEqual(self.kernel.trades[0]["exit_price"], 2005.00)

    # 12. SHORT TP on Ask
    def test_12_short_tp_on_ask(self) -> None:
        # Spread = 0.20. SHORT entry 2000.00. TP = 1990.00 (Ask).
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(
            bar1,
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="SELL",
                entry_price=2000.00,
                tp_price=1990.00,
            ),
        )

        # Bid Low = 1989.70. Ask Low = 1989.70 + 0.20 = 1989.90 <= 1990.00 -> TP triggers!
        bar2 = make_bar(high_p=2001.0, low_p=1989.70)
        trans = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans.status, "TARGETED")
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Take Profit")
        self.assertAlmostEqual(self.kernel.trades[0]["exit_price"], 1990.00)

    # 13. Simultaneous SL/TP collision -> SL first for both BUY and SELL
    def test_13_sl_first_collision_both_directions(self) -> None:
        # BUY collision
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(
            bar1,
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20, sl_price=1990.0, tp_price=2010.0),
        )
        bar2 = make_bar(high_p=2020.0, low_p=1980.0)
        trans_buy = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans_buy.status, "STOPPED")
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Stop Loss")

        # SELL collision
        self.kernel.reset()
        self.kernel.process_open(
            bar1,
            OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=2000.00, sl_price=2010.0, tp_price=1990.0),
        )
        # Ask High = 2020.20 >= 2010 (SL hit), Ask Low = 1980.20 <= 1990 (TP hit)
        trans_sell = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans_sell.status, "STOPPED")
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Stop Loss")

    # 14. Fill and SL/TP in the same entry bar
    def test_14_fill_and_sl_in_same_entry_bar(self) -> None:
        # Order fills at Open=2000.20, but intrabar drops to 1980.0 hitting SL=1990.0
        bar = make_bar(open_p=2000.0, high_p=2005.0, low_p=1980.0, close_p=1985.0)
        open_trans = self.kernel.process_open(
            bar,
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20, sl_price=1990.0, tp_price=2020.0),
        )
        self.assertEqual(open_trans.status, "OPENED")

        intra_trans = self.kernel.process_intrabar(bar)
        self.assertEqual(intra_trans.status, "STOPPED")
        self.assertIsNone(self.kernel.position)
        self.assertEqual(len(self.kernel.trades), 1)
        self.assertEqual(self.kernel.trades[0]["exit_reason"], "Stop Loss")

    # 15. Dynamic levels work when global fixed level is 0
    def test_15_dynamic_levels_work_independently(self) -> None:
        # Kernel has no global fixed SL/TP arguments; levels are purely from PositionState
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(
            bar1,
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                sl_price=1995.0,
                tp_price=2010.0,
            ),
        )
        bar2 = make_bar(low_p=1994.0)
        trans = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans.status, "STOPPED")
        self.assertAlmostEqual(trans.closed_trade["exit_price"], 1995.0)

    # 16. Legacy None SL/TP does not trigger false exit
    def test_16_legacy_none_sl_tp_no_false_trigger(self) -> None:
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(
            bar1,
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20, sl_price=None, tp_price=None),
        )
        bar2 = make_bar(high_p=3000.0, low_p=1000.0)
        trans = self.kernel.process_intrabar(bar2)
        self.assertEqual(trans.status, "NO_ACTION")
        self.assertIsNotNone(self.kernel.position)

    # 17. Reversal / SL / TP / forced close use the same PnL helper
    def test_17_consistent_pnl_helper_across_all_exits(self) -> None:
        multiplier = 10.0
        round_trip_comm = 1.0

        # Case A: Reversal exit
        self.kernel.reset()
        self.kernel.process_open(make_bar(open_p=2000.0), OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
        trans_rev = self.kernel.process_open(make_bar(open_p=2010.0), OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        expected_pnl = (2010.0 - 2000.20) * multiplier - round_trip_comm
        self.assertAlmostEqual(trans_rev.realized_net_pnl, expected_pnl)

        # Case B: SL exit
        self.kernel.reset()
        self.kernel.process_open(make_bar(open_p=2000.0), OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20, sl_price=1990.0))
        trans_sl = self.kernel.process_intrabar(make_bar(low_p=1980.0))
        expected_sl_pnl = (1990.0 - 2000.20) * multiplier - round_trip_comm
        self.assertAlmostEqual(trans_sl.realized_net_pnl, expected_sl_pnl)

        # Case C: Forced close exit
        self.kernel.reset()
        self.kernel.process_open(make_bar(open_p=2000.0), OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
        trans_fc = self.kernel.force_close(make_bar(close_p=2005.0))
        expected_fc_pnl = (2005.0 - 2000.20) * multiplier - round_trip_comm
        self.assertAlmostEqual(trans_fc.realized_net_pnl, expected_fc_pnl)

    # 18. Commission exactly one round trip
    def test_18_commission_exactly_one_round_trip(self) -> None:
        # entry 2000.20, exit 2000.20 -> gross 0.0 -> net -1.0
        bar1 = make_bar(open_p=2000.0)
        self.kernel.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
        bar2 = make_bar(close_p=2000.20)
        trans = self.kernel.force_close(bar2)
        self.assertAlmostEqual(trans.realized_net_pnl, -1.00)
        self.assertAlmostEqual(self.kernel.balance, 9999.00)

    # 19. Floating equity LONG/SHORT spread symmetry
    def test_19_floating_equity_spread_symmetry(self) -> None:
        # LONG at 2000.20 (Ask). Close = 2010.00 (Bid).
        # floating_gross = (2010.00 - 2000.20) * 10 = 98.00
        # floating_net = 98.00 - 1.00 = 97.00 -> equity = 10097.00
        self.kernel.process_open(make_bar(), OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
        eq_long = self.kernel.mark_to_market(2010.00)
        self.assertAlmostEqual(eq_long, 10097.00)

        # SHORT at 2000.00 (Bid). Close = 1990.00 (Bid). Ask = 1990.20.
        # floating_gross = (2000.00 - 1990.20) * 10 = 98.00
        # floating_net = 98.00 - 1.00 = 97.00 -> equity = 10097.00
        self.kernel.reset()
        self.kernel.process_open(make_bar(), OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=2000.00))
        eq_short = self.kernel.mark_to_market(1990.00)
        self.assertAlmostEqual(eq_short, 10097.00)

    # 20. Forced close updates balance/trade/marker exactly once
    def test_20_forced_close_idempotent(self) -> None:
        self.kernel.process_open(make_bar(), OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
        bar = make_bar(close_p=2005.0)

        trans1 = self.kernel.force_close(bar)
        self.assertEqual(trans1.status, "FORCED_CLOSED")
        self.assertEqual(len(self.kernel.trades), 1)

        # Calling force_close again when flat is no-op
        trans2 = self.kernel.force_close(bar)
        self.assertEqual(trans2.status, "NO_ACTION")
        self.assertEqual(len(self.kernel.trades), 1)

    # 21. Reset clears all state
    def test_21_reset_clears_state(self) -> None:
        self.kernel.process_open(make_bar(), OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20))
        self.kernel.force_close(make_bar())
        self.assertNotEqual(self.kernel.balance, self.kernel.initial_capital)

        self.kernel.reset()
        self.assertEqual(self.kernel.balance, self.kernel.initial_capital)
        self.assertIsNone(self.kernel.position)
        self.assertEqual(len(self.kernel.trades), 0)
        self.assertEqual(len(self.kernel.markers), 0)

    # 22. NaN / Inf / bool / geometry error fail-fast and atomic
    def test_22_nan_inf_bool_geometry_rejections(self) -> None:
        # bool rejected in ExecutionBar
        with self.assertRaises(TypeError):
            ExecutionBar(bar_index=1, timestamp=1000, time_value="t", open=True, high=2010.0, low=1990.0, close=2000.0)

        # NaN rejected in ExecutionBar
        with self.assertRaises(ValueError):
            ExecutionBar(bar_index=1, timestamp=1000, time_value="t", open=float("nan"), high=2010.0, low=1990.0, close=2000.0)

        # Inf rejected in OpenInstruction
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=float("inf"))

        # Geometry error in OpenInstruction
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.0, sl_price=2005.0)

    # 23. Metadata immutable, no mutation leaks
    def test_23_metadata_deep_immutability(self) -> None:
        raw_meta = {"setup": {"id": "S01", "scores": [1, 2, 3]}}
        inst = OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20, metadata=raw_meta)
        self.assertIsInstance(inst.metadata, MappingProxyType)

        # Mutating outside dict does not affect internal metadata
        raw_meta["setup"]["id"] = "MUTATED"
        self.assertEqual(inst.metadata["setup"]["id"], "S01")

        # Mutating inside metadata raises error
        with self.assertRaises(TypeError):
            inst.metadata["setup"] = "NEW"

    # 24. Deterministic retry / no duplicate transition when already flat
    def test_24_deterministic_flat_transitions(self) -> None:
        bar = make_bar()
        t1 = self.kernel.process_open(bar, None)
        self.assertEqual(t1.status, "NO_ACTION")
        t2 = self.kernel.process_intrabar(bar)
        self.assertEqual(t2.status, "NO_ACTION")
        t3 = self.kernel.force_close(bar)
        self.assertEqual(t3.status, "NO_ACTION")
        self.assertEqual(len(self.kernel.trades), 0)

    # 25. Transition alias tests (Section 5.1)
    def test_25_transition_alias_protection(self) -> None:
        # 1. Open position, get entry transition, attempt to mutate marker
        bar1 = make_bar(bar_index=1, timestamp=1000, time_value="t1", open_p=2000.0)
        open_trans = self.kernel.process_open(
            bar1,
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=2000.20),
        )
        self.assertIsInstance(open_trans.markers[0], MappingProxyType)
        with self.assertRaises(TypeError):
            open_trans.markers[0]["text"] = "CORRUPTED"
        # Confirm internal marker is unchanged
        self.assertIn("BUY @ 2000.20", self.kernel.markers[0]["text"])

        # 2. Close position, attempt to mutate closed_trade
        bar2 = make_bar(bar_index=2, timestamp=2000, time_value="t2", open_p=2010.0)
        close_trans = self.kernel.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertIsInstance(close_trans.closed_trade, MappingProxyType)
        with self.assertRaises(TypeError):
            close_trans.closed_trade["pnl"] = 999999.0
        # Confirm internal trade is unchanged
        self.assertNotEqual(self.kernel.trades[0]["pnl"], 999999.0)

        # 3. Mutate object returned from kernel.trades and kernel.markers properties
        trades = self.kernel.trades
        trades[0]["pnl"] = 999999.0
        trades[0]["new_field"] = "INJECTED"
        self.assertNotEqual(self.kernel.trades[0]["pnl"], 999999.0)
        self.assertNotIn("new_field", self.kernel.trades[0])

        markers = self.kernel.markers
        markers[0]["text"] = "MUTATED"
        self.assertNotEqual(self.kernel.markers[0]["text"], "MUTATED")

    # 26. Deep immutability tests (Section 5.2)
    def test_26_deep_immutability_sets_mappings_lists(self) -> None:
        from collections.abc import Mapping

        class CustomMapping(Mapping):
            def __init__(self, d: dict):
                self._d = dict(d)
            def __getitem__(self, k):
                return self._d[k]
            def __len__(self):
                return len(self._d)
            def __iter__(self):
                return iter(self._d)

        # Outer mutable structures
        mutable_set = {"tag_a", "tag_b"}
        mutable_list = [1, 2, {"nested": "val"}]
        custom_map = CustomMapping({"custom_key": "custom_val"})
        mutable_meta = {
            "tags": mutable_set,
            "items": mutable_list,
            "custom": custom_map,
        }
        mutable_time = {"date": "2026-01-01", "hour": 10}

        bar = ExecutionBar(
            bar_index=1,
            timestamp=1000,
            time_value=mutable_time,
            open=2000.0,
            high=2010.0,
            low=1990.0,
            close=2005.0,
        )
        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=2000.20,
            metadata=mutable_meta,
        )

        # External mutation after creation
        mutable_set.add("MUTATED_TAG")
        mutable_list.append(999)
        mutable_time["hour"] = 99
        custom_map._d["custom_key"] = "MUTATED_CUSTOM"

        # Verify bar.time_value was snapshot and not mutated
        self.assertIsInstance(bar.time_value, MappingProxyType)
        self.assertEqual(bar.time_value["hour"], 10)

        # Verify inst.metadata was snapshot and not mutated
        self.assertIsInstance(inst.metadata["tags"], frozenset)
        self.assertNotIn("MUTATED_TAG", inst.metadata["tags"])
        self.assertEqual(len(inst.metadata["items"]), 3)
        self.assertEqual(inst.metadata["custom"]["custom_key"], "custom_val")

        # Verify direct mutation on frozen metadata is blocked
        with self.assertRaises(TypeError):
            inst.metadata["new_key"] = "test"

        # Verify process_open and position_after keep the snapshot
        trans = self.kernel.process_open(bar, inst)
        self.assertEqual(trans.position_after.entry_time_value["hour"], 10)
        self.assertNotIn("MUTATED_TAG", trans.position_after.metadata["tags"])

    # 27. KernelTransition invariant tests (Section 5.3)
    def test_27_kernel_transition_invariants(self) -> None:
        pos_buy = PositionState(
            direction="BUY",
            entry_price=2000.0,
            entry_timestamp=1000,
            entry_time_value="t1",
            sl_price=1990.0,
            tp_price=2010.0,
            multiplier=10.0,
            round_trip_commission=1.0,
            source="wave1",
        )
        pos_sell = PositionState(
            direction="SELL",
            entry_price=2010.0,
            entry_timestamp=2000,
            entry_time_value="t2",
            sl_price=2020.0,
            tp_price=2000.0,
            multiplier=10.0,
            round_trip_commission=1.0,
            source="wave1",
        )
        fake_trade = {"trade_id": 1, "pnl": 90.0}

        # 1. Invalid status
        with self.assertRaises(ValueError):
            KernelTransition(status="INVALID_STATUS", position_before=None, position_after=None, closed_trade=None, markers=(), realized_net_pnl=0.0)

        # 2. OPENED but position_after=None
        with self.assertRaises(ValueError):
            KernelTransition(status="OPENED", position_before=None, position_after=None, closed_trade=None, markers=(), realized_net_pnl=0.0)

        # 3. REVERSED but missing closed_trade
        with self.assertRaises(ValueError):
            KernelTransition(status="REVERSED", position_before=pos_buy, position_after=pos_sell, closed_trade=None, markers=(), realized_net_pnl=90.0)

        # 4. NO_ACTION but with non-zero realized PnL
        with self.assertRaises(ValueError):
            KernelTransition(status="NO_ACTION", position_before=None, position_after=None, closed_trade=None, markers=(), realized_net_pnl=50.0)

        # 5. STOPPED but still has position_after
        with self.assertRaises(ValueError):
            KernelTransition(status="STOPPED", position_before=pos_buy, position_after=pos_sell, closed_trade=fake_trade, markers=(), realized_net_pnl=-100.0)

        # 6. realized_net_pnl = NaN / Inf / bool
        with self.assertRaises(ValueError):
            KernelTransition(status="OPENED", position_before=None, position_after=pos_buy, closed_trade=None, markers=(), realized_net_pnl=float("nan"))
        with self.assertRaises(ValueError):
            KernelTransition(status="OPENED", position_before=None, position_after=pos_buy, closed_trade=None, markers=(), realized_net_pnl=float("inf"))
        with self.assertRaises(TypeError):
            KernelTransition(status="OPENED", position_before=None, position_after=pos_buy, closed_trade=None, markers=(), realized_net_pnl=True)

    # 28. Legacy compatibility boundary (Section 5.4)
    def test_28_legacy_compatibility_negative_sl_tp(self) -> None:
        # BUY entry = 1.0, sl_price = -1.0 (from stop_loss_val = 2.0)
        inst_buy = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=1.0,
            sl_price=-1.0,
            tp_price=3.0,
            source="legacy",
        )
        self.assertEqual(inst_buy.sl_price, -1.0)
        self.assertEqual(inst_buy.tp_price, 3.0)

        bar1 = make_bar(bar_index=1, timestamp=1000, time_value="t1", open_p=1.0, high_p=1.5, low_p=0.8, close_p=1.2)
        trans = self.kernel.process_open(bar1, inst_buy)
        self.assertEqual(trans.status, "OPENED")
        self.assertEqual(trans.position_after.sl_price, -1.0)

        # SELL entry = 1.0, tp_price = -1.0 (from take_profit_val = 2.0)
        self.kernel.reset()
        inst_sell = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="SELL",
            entry_price=1.0,
            sl_price=3.0,
            tp_price=-1.0,
            source="legacy",
        )
        self.assertEqual(inst_sell.tp_price, -1.0)
        trans_sell = self.kernel.process_open(bar1, inst_sell)
        self.assertEqual(trans_sell.status, "OPENED")
        self.assertEqual(trans_sell.position_after.tp_price, -1.0)

    # 29. Wave 1 strictness (Section 5.5)
    def test_29_wave1_strictness(self) -> None:
        # entry <= 0 rejected
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=0.0, sl_price=10.0, tp_price=20.0, source="wave1")
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=-1.0, sl_price=10.0, tp_price=20.0, source="wave1")

        # SL/TP <= 0 rejected in wave1
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=1.0, sl_price=-1.0, tp_price=2.0, source="wave1")
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=1.0, sl_price=0.0, tp_price=2.0, source="wave1")
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=1.0, sl_price=2.0, tp_price=0.0, source="wave1")

        # Missing SL or TP rejected in wave1
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=90.0, tp_price=None, source="wave1")
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=None, tp_price=110.0, source="wave1")

        # Geometry error rejected in wave1
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=105.0, tp_price=110.0, source="wave1")
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=95.0, tp_price=90.0, source="wave1")

        # NaN / Inf / bool rejected
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=float("nan"), sl_price=90.0, tp_price=110.0, source="wave1")
        with self.assertRaises(ValueError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=float("inf"), tp_price=110.0, source="wave1")
        with self.assertRaises(TypeError):
            OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=True, sl_price=90.0, tp_price=110.0, source="wave1")

    # 30. Deep immutability: mutable NumPy/pandas containers rejected fail-closed
    def test_30_deep_immutability_numpy_pandas_containers_rejected(self) -> None:
        import numpy as np
        import pandas as pd
        from dataclasses import dataclass

        # 1. np.ndarray in metadata -> TypeError
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                metadata={"arr": np.array([1, 2, 3])},
            )

        # 2. pd.Series in metadata -> TypeError
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                metadata={"series": pd.Series([1, 2, 3])},
            )

        # 3. pd.DataFrame in metadata -> TypeError
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                metadata={"df": pd.DataFrame({"a": [1, 2]})},
            )

        # 4. np.ndarray in ExecutionBar.time_value -> TypeError
        with self.assertRaises(TypeError):
            ExecutionBar(
                bar_index=1,
                timestamp=1000,
                time_value=np.array([1, 2]),
                open=2000.0,
                high=2010.0,
                low=1990.0,
                close=2005.0,
            )

        # 5. Non-frozen dataclass -> TypeError
        @dataclass
        class NonFrozenConfig:
            val: int = 1

        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                metadata={"cfg": NonFrozenConfig(val=5)},
            )

        # 6. Unsupported custom mutable object -> TypeError
        class CustomMutable:
            pass

        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=2000.20,
                metadata={"obj": CustomMutable()},
            )

    # 31. Frozen dataclass with nested mutable is deeply frozen
    def test_31_frozen_dataclass_with_nested_mutable_deeply_frozen(self) -> None:
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FrozenInner:
            items: list
            data: dict

        raw_list = [10, 20]
        raw_dict = {"k": "v"}
        inner = FrozenInner(items=raw_list, data=raw_dict)

        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=2000.20,
            metadata={"inner": inner},
        )

        # Source mutation after model creation
        raw_list.append(999)
        raw_dict["k"] = "MUTATED"

        frozen_inner = inst.metadata["inner"]
        # Nested list must have become a tuple and not contain 999
        self.assertIsInstance(frozen_inner.items, tuple)
        self.assertEqual(frozen_inner.items, (10, 20))
        # Nested dict must have become a MappingProxyType and not contain mutated value
        self.assertIsInstance(frozen_inner.data, MappingProxyType)
        self.assertEqual(frozen_inner.data["k"], "v")

        # Mutating frozen dataclass fields is blocked
        with self.assertRaises(TypeError):
            frozen_inner.data["new"] = 1

    # 32. Valid scalars: NumPy scalars and pandas Timestamp/Timedelta
    def test_32_valid_scalars_numpy_and_pandas(self) -> None:
        import numpy as np
        import pandas as pd

        ts = pd.Timestamp("2026-01-01 12:00:00")
        td = pd.Timedelta(minutes=15)
        np_int = np.int64(42)
        np_float = np.float64(3.14159)
        np_bool = np.bool_(True)

        bar = ExecutionBar(
            bar_index=1,
            timestamp=1000,
            time_value=ts,
            open=2000.0,
            high=2010.0,
            low=1990.0,
            close=2005.0,
        )
        self.assertEqual(bar.time_value, ts)

        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=2000.20,
            metadata={
                "delta": td,
                "n_int": np_int,
                "n_float": np_float,
                "n_bool": np_bool,
            },
        )
        self.assertEqual(inst.metadata["delta"], td)
        self.assertEqual(inst.metadata["n_int"], 42)
        self.assertIsInstance(inst.metadata["n_int"], int)
        self.assertAlmostEqual(inst.metadata["n_float"], 3.14159)
        self.assertIsInstance(inst.metadata["n_float"], float)
        self.assertIs(inst.metadata["n_bool"], True)

    # 33. Legacy entry price zero and negative compatibility
    def test_33_legacy_entry_price_zero_and_negative(self) -> None:
        # 1. Legacy OpenInstruction allows entry_price = 0.0
        inst_zero = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=0.0,
            sl_price=-2.0,
            tp_price=2.0,
            source="legacy",
        )
        self.assertEqual(inst_zero.entry_price, 0.0)

        # 2. Legacy OpenInstruction allows negative entry_price
        inst_neg = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=-1.0,
            sl_price=-3.0,
            tp_price=1.0,
            source="legacy",
        )
        self.assertEqual(inst_neg.entry_price, -1.0)

        # 3. Legacy PositionState allows entry_price = 0.0 and multiplier/commission = 0.0
        pos_zero = PositionState(
            direction="BUY",
            entry_price=0.0,
            entry_timestamp=1000,
            entry_time_value="t",
            sl_price=-1.0,
            tp_price=1.0,
            multiplier=0.0,
            round_trip_commission=0.0,
            source="legacy",
        )
        self.assertEqual(pos_zero.entry_price, 0.0)

        # 4. Wave 1 OpenInstruction rejects entry_price <= 0
        with self.assertRaises(ValueError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=0.0,
                sl_price=-1.0,
                tp_price=1.0,
                source="wave1",
            )
        with self.assertRaises(ValueError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=-1.0,
                sl_price=-2.0,
                tp_price=1.0,
                source="wave1",
            )

        # 5. Wave 1 PositionState rejects entry_price <= 0
        with self.assertRaises(ValueError):
            PositionState(
                direction="BUY",
                entry_price=0.0,
                entry_timestamp=1000,
                entry_time_value="t",
                sl_price=90.0,
                tp_price=110.0,
                multiplier=10.0,
                round_trip_commission=1.0,
                source="wave1",
            )

        # 6. Relative geometry violation rejected for legacy as well
        with self.assertRaises(ValueError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=0.0,
                sl_price=1.0,  # sl > entry is invalid geometry for BUY
                tp_price=2.0,
                source="legacy",
            )
        with self.assertRaises(ValueError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="SELL",
                entry_price=0.0,
                sl_price=-1.0,  # sl < entry is invalid geometry for SELL
                tp_price=-2.0,
                source="legacy",
            )

    # 34. Kernel validation_mode constructor boundary
    def test_34_kernel_validation_mode_constructor_boundary(self) -> None:
        # Default wave1 mode is strict
        with self.assertRaises(ValueError):
            ExecutionKernel(initial_capital=0.0)
        with self.assertRaises(ValueError):
            ExecutionKernel(lot_size=0.0)
        with self.assertRaises(ValueError):
            ExecutionKernel(contract_size=0.0)
        with self.assertRaises(ValueError):
            ExecutionKernel(spread_val=-0.20)
        with self.assertRaises(ValueError):
            ExecutionKernel(commission_per_side=-0.50)

        # Legacy mode permits boundary configurations
        k_legacy = ExecutionKernel(
            initial_capital=0.0,
            lot_size=0.0,
            contract_size=0.0,
            spread_val=-0.20,
            commission_per_side=-0.50,
            validation_mode="legacy",
        )
        self.assertEqual(k_legacy.initial_capital, 0.0)
        self.assertEqual(k_legacy.lot_size, 0.0)
        self.assertEqual(k_legacy.contract_size, 0.0)
        self.assertEqual(k_legacy.spread_val, -0.20)
        self.assertEqual(k_legacy.commission_per_side, -0.50)

        # Invalid validation_mode rejected
        with self.assertRaises(ValueError):
            ExecutionKernel(validation_mode="invalid")

    # 35. Structured NumPy generic and cycle detection freeze
    def test_35_structured_numpy_generic_nested_freeze(self) -> None:
        # 1. np.void with nested mutable list
        source_list = [1, 2]
        scalar = np.array((source_list,), dtype=[("payload", object)])[()]
        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            source="legacy",
            metadata={"scalar": scalar},
        )
        source_list.append(3)
        self.assertIsInstance(inst.metadata["scalar"], tuple)
        self.assertEqual(inst.metadata["scalar"][0], (1, 2))
        self.assertIsInstance(inst.metadata["scalar"][0], tuple)

        # 2. np.void with nested mutable dict
        source_dict = {"items": [10, 20]}
        scalar_dict = np.array((source_dict,), dtype=[("payload", object)])[()]
        inst_dict = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            source="legacy",
            metadata={"scalar": scalar_dict},
        )
        source_dict["items"].append(30)
        source_dict["new_key"] = "leaked"
        self.assertIsInstance(inst_dict.metadata["scalar"][0], MappingProxyType)
        self.assertEqual(inst_dict.metadata["scalar"][0]["items"], (10, 20))
        self.assertNotIn("new_key", inst_dict.metadata["scalar"][0])

        # 3. Standard NumPy scalars normalized to native Python types
        self.assertIs(type(_deep_freeze(np.int64(42))), int)
        self.assertEqual(_deep_freeze(np.int64(42)), 42)
        self.assertIs(type(_deep_freeze(np.float64(3.14))), float)
        self.assertEqual(_deep_freeze(np.float64(3.14)), 3.14)
        self.assertIs(type(_deep_freeze(np.bool_(True))), bool)
        self.assertEqual(_deep_freeze(np.bool_(True)), True)

        # 4. Containers rejected fail-closed
        with self.assertRaises(TypeError):
            _deep_freeze(np.array([1, 2]))
        with self.assertRaises(TypeError):
            _deep_freeze(pd.Series([1, 2]))
        with self.assertRaises(TypeError):
            _deep_freeze(pd.DataFrame({"a": [1]}))

        # 5. Cycle detection raises TypeError
        cycle_list = []
        cycle_list.append(cycle_list)
        with self.assertRaises(TypeError):
            _deep_freeze(cycle_list)

    # 36. Mapping tuple key handled atomically in thaw & close
    def test_36_mapping_tuple_key_atomic_close(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.0, commission_per_side=0.0)
        time_with_tuple_key = {("session", 1): "2026-01-01 10:00:00"}
        bar1 = ExecutionBar(1, 1000, time_with_tuple_key, 10.0, 15.0, 5.0, 10.0)
        inst = OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0)

        # Process open
        trans_open = k.process_open(bar1, inst)
        self.assertEqual(trans_open.status, "OPENED")
        self.assertEqual(trans_open.position_after.entry_time_value[("session", 1)], "2026-01-01 10:00:00")

        # Close position
        bar2 = ExecutionBar(2, 2000, time_with_tuple_key, 12.0, 15.0, 10.0, 12.0)
        trans_close = k.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertEqual(trans_close.status, "CLOSED")
        self.assertEqual(len(k.trades), 1)
        self.assertEqual(k.trades[0]["entry_time"][("session", 1)], "2026-01-01 10:00:00")
        self.assertEqual(k.trades[0]["exit_time"][("session", 1)], "2026-01-01 10:00:00")

    # 37. Close position atomic failure rollback
    def test_37_close_position_atomic_failure_rollback(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.0, commission_per_side=0.0)
        bar1 = make_bar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))

        initial_balance = k.balance
        initial_pos = k.position
        initial_counter = k._trade_counter
        initial_trades_len = len(k._trades)
        initial_markers_len = len(k._markers)

        bar2 = make_bar(2, 2000, "t2", 15.0, 20.0, 10.0, 15.0)

        # Inject error during close position preparation (e.g. mock _deep_freeze failure on trade record)
        orig_deep_freeze = k._close_position.__globals__["_deep_freeze"]

        def mock_exploding_freeze(val, _seen=None):
            if isinstance(val, dict) and "trade_id" in val:
                raise RuntimeError("Injected export/freeze explosion")
            return orig_deep_freeze(val, _seen=_seen)

        with patch.dict(k._close_position.__globals__, {"_deep_freeze": mock_exploding_freeze}):
            # Attempt close via CLOSE_ONLY
            with self.assertRaises(RuntimeError):
                k.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))

        # Verify state is 100% untouched
        self.assertEqual(k.balance, initial_balance)
        self.assertIs(k.position, initial_pos)
        self.assertEqual(k._trade_counter, initial_counter)
        self.assertEqual(len(k._trades), initial_trades_len)
        self.assertEqual(len(k._markers), initial_markers_len)

        # Retry after failure: must succeed cleanly without double-counting
        trans_retry = k.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertEqual(trans_retry.status, "CLOSED")
        self.assertEqual(k.balance, initial_balance + 5.0)
        self.assertIsNone(k.position)
        self.assertEqual(k._trade_counter, 1)
        self.assertEqual(len(k.trades), 1)
        self.assertEqual(k.trades[0]["trade_id"], 1)

        # Test Reversal atomicity when new position geometry is invalid
        # Re-open position
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
        pos_before_rev = k.position
        bal_before_rev = k.balance
        trades_count_before = len(k._trades)
        markers_count_before = len(k._markers)

        # Attempt reversal with invalid SELL geometry (sl < entry)
        with self.assertRaises(ValueError):
            k.process_open(bar2, OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="SELL",
                entry_price=15.0,
                sl_price=10.0,  # Invalid: SELL sl must be > entry
                tp_price=5.0,
                source="legacy",
            ))

        # Position and kernel must remain untouched
        self.assertIs(k.position, pos_before_rev)
        self.assertEqual(k.balance, bal_before_rev)
        self.assertEqual(len(k._trades), trades_count_before)
        self.assertEqual(len(k._markers), markers_count_before)

    # 38. Negative initial capital return percentage in legacy mode
    def test_38_negative_initial_capital_return_pct(self) -> None:
        # Legacy mode with negative capital
        k_neg = ExecutionKernel(
            initial_capital=-100.0,
            lot_size=1.0,
            contract_size=1.0,
            spread_val=0.0,
            commission_per_side=0.0,
            validation_mode="legacy",
        )
        bar1 = make_bar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
        k_neg.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))

        # Win trade: net_pnl = +1.0 -> return_pct = round((1.0 / -100.0) * 100, 2) = -1.0
        bar2 = make_bar(2, 2000, "t2", 11.0, 15.0, 5.0, 11.0)
        k_neg.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertEqual(len(k_neg.trades), 1)
        self.assertEqual(k_neg.trades[0]["pnl"], 1.0)
        self.assertEqual(k_neg.trades[0]["return_pct"], -1.0)

        # Loss trade: net_pnl = -1.0 -> return_pct = round((-1.0 / -100.0) * 100, 2) = 1.0
        k_neg.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
        bar3 = make_bar(3, 3000, "t3", 9.0, 15.0, 5.0, 9.0)
        k_neg.process_open(bar3, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertEqual(len(k_neg.trades), 2)
        self.assertEqual(k_neg.trades[1]["pnl"], -1.0)
        self.assertEqual(k_neg.trades[1]["return_pct"], 1.0)

        # Zero capital: return_pct = 0.0 (no division by zero)
        k_zero = ExecutionKernel(
            initial_capital=0.0,
            lot_size=1.0,
            contract_size=1.0,
            spread_val=0.0,
            commission_per_side=0.0,
            validation_mode="legacy",
        )
        k_zero.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
        k_zero.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertEqual(k_zero.trades[0]["return_pct"], 0.0)

        # Wave 1 rejects non-positive initial capital
        with self.assertRaises(ValueError):
            ExecutionKernel(initial_capital=-100.0, validation_mode="wave1")
        with self.assertRaises(ValueError):
            ExecutionKernel(initial_capital=0.0, validation_mode="wave1")

    # 39. Trades and markers defensive copies contract and Timestamp preservation
    def test_39_trades_markers_defensive_copies_contract(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.0, commission_per_side=0.0)
        ts_entry = pd.Timestamp("2026-01-01 10:00:00")
        ts_exit = pd.Timestamp("2026-01-01 10:15:00")
        bar1 = ExecutionBar(1, 1000, ts_entry, 10.0, 15.0, 5.0, 10.0)
        bar2 = ExecutionBar(2, 2000, ts_exit, 12.0, 15.0, 5.0, 12.0)

        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
        k.force_close(bar2)

        # Verify trades property returns independent copy
        trades_copy = k.trades
        trades_copy[0]["pnl"] = 999999.0
        self.assertNotEqual(k.trades[0]["pnl"], 999999.0)

        # Verify markers property returns independent copy
        markers_copy = k.markers
        markers_copy[0]["text"] = "MUTATED"
        self.assertNotEqual(k.markers[0]["text"], "MUTATED")

        # Verify Timestamp preservation in trade records
        self.assertEqual(k.trades[0]["entry_time"], ts_entry)
        self.assertEqual(k.trades[0]["exit_time"], ts_exit)

        # json.dumps without custom encoder fails on Timestamp, verifying caller responsibility contract
        with self.assertRaises(TypeError):
            json.dumps(k.trades)

    # 40. Enum fail-closed policy (mutable values, nested mutable, and general Enums)
    def test_40_enum_fail_closed_policy(self) -> None:
        class MutableListEnum(Enum):
            TOKEN = [1, 2]

        class MutableDictEnum(Enum):
            CONFIG = {"key": [10, 20]}

        class StandardEnum(Enum):
            VALUE = "SIMPLE"

        # 1. Enum with list value rejected fail-closed
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=10.0,
                metadata={"enum": MutableListEnum.TOKEN},
            )

        # 2. Enum with dict or nested mutable value rejected fail-closed
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=10.0,
                metadata={"enum": MutableDictEnum.CONFIG},
            )

        # 3. Standard enum rejected under fail-closed policy
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=10.0,
                metadata={"enum": StandardEnum.VALUE},
            )

        # 4. Enum in ExecutionBar time_value rejected fail-closed
        with self.assertRaises(TypeError):
            ExecutionBar(
                bar_index=1,
                timestamp=1000,
                time_value=StandardEnum.VALUE,
                open=10.0,
                high=15.0,
                low=5.0,
                close=10.0,
            )

    # 41. Exact-type scalar whitelist, subclass rejection, and non-regression
    def test_41_scalar_subclasses_and_exact_primitive_whitelist(self) -> None:
        class MutableInt(int):
            pass

        class MutableStr(str):
            pass

        class MutableFloat(float):
            pass

        # 1. Subclass of int with mutable attribute rejected
        val_int = MutableInt(7)
        val_int.payload = [1]
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=10.0,
                metadata={"val": val_int},
            )

        # 2. Subclass of str with mutable attribute rejected
        val_str = MutableStr("hello")
        val_str.payload = [1]
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=10.0,
                metadata={"val": val_str},
            )

        # 3. Subclass of float rejected
        val_float = MutableFloat(3.14)
        with self.assertRaises(TypeError):
            OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="BUY",
                entry_price=10.0,
                metadata={"val": val_float},
            )

        # 4. Primitive exact types are supported
        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={
                "none": None,
                "bool": True,
                "int": 42,
                "float": 3.14,
                "str": "ok",
                "bytes": b"data",
            },
        )
        self.assertIsNone(inst.metadata["none"])
        self.assertIs(inst.metadata["bool"], True)
        self.assertEqual(inst.metadata["int"], 42)
        self.assertEqual(inst.metadata["float"], 3.14)
        self.assertEqual(inst.metadata["str"], "ok")
        self.assertEqual(inst.metadata["bytes"], b"data")

        # 5. Modifying source object after model creation does NOT mutate snapshot
        src_dict = {"a": [1, 2], "b": {"nested": [3]}}
        inst_snap = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={"data": src_dict},
        )
        src_dict["a"].append(999)
        src_dict["b"]["nested"].append(888)
        self.assertEqual(inst_snap.metadata["data"]["a"], (1, 2))
        self.assertEqual(inst_snap.metadata["data"]["b"]["nested"], (3,))

        # 6. Exact datetime types, Timestamp, Timedelta, and NumPy scalar types do not regress
        ts = pd.Timestamp("2026-01-01 10:00:00")
        td = pd.Timedelta(minutes=15)
        d = date(2026, 1, 1)
        dt = datetime(2026, 1, 1, 10, 0, 0)
        t = time(10, 0, 0)
        delta = timedelta(minutes=5)
        inst_types = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={
                "ts": ts,
                "td": td,
                "date": d,
                "dt": dt,
                "time": t,
                "delta": delta,
                "np_int": np.int64(10),
                "np_float": np.float64(20.5),
                "np_bool": np.bool_(False),
            },
        )
        self.assertEqual(inst_types.metadata["ts"], ts)
        self.assertEqual(inst_types.metadata["td"], td)
        self.assertEqual(inst_types.metadata["date"], d)
        self.assertEqual(inst_types.metadata["dt"], dt)
        self.assertEqual(inst_types.metadata["time"], t)
        self.assertEqual(inst_types.metadata["delta"], delta)
        self.assertEqual(inst_types.metadata["np_int"], 10)
        self.assertEqual(inst_types.metadata["np_float"], 20.5)
        self.assertIs(inst_types.metadata["np_bool"], False)

        # 7. Structured np.void with nested list/dict deep-frozen
        src_v = [10, 20]
        v = np.array((src_v,), dtype=[("payload", object)])[()]
        inst_v = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=10.0,
            metadata={"v": v},
        )
        src_v.append(30)
        self.assertEqual(inst_v.metadata["v"][0], (10, 20))

    # 42. Derived constructor arithmetic overflow policy
    def test_42_constructor_arithmetic_overflow_policy(self) -> None:
        # Wave 1 mode: fail-fast at constructor when multiplier overflows
        with self.assertRaises(ValueError):
            ExecutionKernel(lot_size=1e200, contract_size=1e200, validation_mode="wave1")

        # Wave 1 mode: fail-fast at constructor when round_trip_commission overflows
        with self.assertRaises(ValueError):
            ExecutionKernel(commission_per_side=1e308, validation_mode="wave1")

        # Legacy mode: accepts constructor inputs to preserve legacy caller compatibility
        k_legacy = ExecutionKernel(lot_size=1e200, contract_size=1e200, validation_mode="legacy")
        self.assertTrue(math.isinf(k_legacy.multiplier))

    # 43. Close-only arithmetic overflow atomic rollback
    def test_43_arithmetic_overflow_rollback_close_only(self) -> None:
        # Case 1: gross_pnl overflow
        k = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        bar2 = make_bar(2, 2000, "t2", 20.0, 25.0, 15.0, 20.0)
        with self.assertRaises(ValueError) as ctx:
            k.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertIn("gross_pnl", str(ctx.exception))

        # Absolute state invariance verification
        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

        # Case 2: prospective_balance overflow (balance + net_pnl = inf)
        k2 = ExecutionKernel(initial_capital=1e308, lot_size=1.0, contract_size=1.0, validation_mode="legacy")
        k2.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=0.0, source="legacy"))
        snap2_before = {
            "balance": k2.balance,
            "position": k2.position,
            "trade_counter": k2._trade_counter,
            "trades_len": len(k2._trades),
            "markers_len": len(k2._markers),
        }
        bar2_huge = make_bar(2, 2000, "t2", 1e308, 1e308, 0.0, 1e308)
        with self.assertRaises(ValueError) as ctx:
            k2.process_open(bar2_huge, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertIn("prospective_balance", str(ctx.exception))

        self.assertEqual(k2.balance, snap2_before["balance"])
        self.assertIs(k2.position, snap2_before["position"])
        self.assertEqual(k2._trade_counter, snap2_before["trade_counter"])
        self.assertEqual(len(k2._trades), snap2_before["trades_len"])
        self.assertEqual(len(k2._markers), snap2_before["markers_len"])

        # Case 3: return_pct overflow
        k3 = ExecutionKernel(initial_capital=1e-320, lot_size=1.0, contract_size=1.0, validation_mode="legacy")
        k3.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))
        snap3_before = {
            "balance": k3.balance,
            "position": k3.position,
            "trade_counter": k3._trade_counter,
            "trades_len": len(k3._trades),
            "markers_len": len(k3._markers),
        }
        with self.assertRaises(ValueError) as ctx:
            k3.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertIn("return_pct", str(ctx.exception))

        self.assertEqual(k3.balance, snap3_before["balance"])
        self.assertIs(k3.position, snap3_before["position"])
        self.assertEqual(k3._trade_counter, snap3_before["trade_counter"])
        self.assertEqual(len(k3._trades), snap3_before["trades_len"])
        self.assertEqual(len(k3._markers), snap3_before["markers_len"])

    # 44. Intrabar Stop Loss and Take Profit arithmetic overflow atomic rollback
    def test_44_arithmetic_overflow_rollback_intrabar_sl_tp(self) -> None:
        # 1. Intrabar Stop Loss overflow
        k = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=90.0))

        snap_before_sl = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        # Bar touching SL at 90.0 (diff -10 * 1e308 = -inf)
        bar_sl = make_bar(2, 2000, "t2", 95.0, 100.0, 85.0, 90.0)
        with self.assertRaises(ValueError) as ctx:
            k.process_intrabar(bar_sl)
        self.assertIn("gross_pnl", str(ctx.exception))

        self.assertEqual(k.balance, snap_before_sl["balance"])
        self.assertIs(k.position, snap_before_sl["position"])
        self.assertEqual(k._trade_counter, snap_before_sl["trade_counter"])
        self.assertEqual(len(k._trades), snap_before_sl["trades_len"])
        self.assertEqual(len(k._markers), snap_before_sl["markers_len"])

        # 2. Intrabar Take Profit overflow
        k_tp = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
        k_tp.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, tp_price=120.0))

        snap_before_tp = {
            "balance": k_tp.balance,
            "position": k_tp.position,
            "trade_counter": k_tp._trade_counter,
            "trades_len": len(k_tp._trades),
            "markers_len": len(k_tp._markers),
        }

        # Bar touching TP at 120.0 (diff 20 * 1e308 = inf)
        bar_tp = make_bar(2, 2000, "t2", 105.0, 125.0, 100.0, 110.0)
        with self.assertRaises(ValueError) as ctx:
            k_tp.process_intrabar(bar_tp)
        self.assertIn("gross_pnl", str(ctx.exception))

        self.assertEqual(k_tp.balance, snap_before_tp["balance"])
        self.assertIs(k_tp.position, snap_before_tp["position"])
        self.assertEqual(k_tp._trade_counter, snap_before_tp["trade_counter"])
        self.assertEqual(len(k_tp._trades), snap_before_tp["trades_len"])
        self.assertEqual(len(k_tp._markers), snap_before_tp["markers_len"])

    # 45. Reversal arithmetic overflow atomic rollback
    def test_45_arithmetic_overflow_rollback_reversal(self) -> None:
        k = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        bar2 = make_bar(2, 2000, "t2", 20.0, 25.0, 15.0, 20.0)
        # Attempt reversal BUY -> SELL where closing BUY overflows
        with self.assertRaises(ValueError):
            k.process_open(bar2, OpenInstruction(
                action="OPEN_OR_REVERSE",
                direction="SELL",
                entry_price=20.0,
                sl_price=30.0,
                tp_price=10.0,
                source="legacy",
            ))

        # Verifications: old position intact, no closed trade, no exit marker, no new entry marker, balance/counter unchanged
        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k.position.direction, "BUY")
        self.assertEqual(k.position.entry_price, 10.0)
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

    # 46. Forced close arithmetic overflow atomic rollback
    def test_46_arithmetic_overflow_rollback_forced_close(self) -> None:
        k = ExecutionKernel(lot_size=1e308, contract_size=1.0, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        last_bar = make_bar(2, 2000, "t2", 30.0, 35.0, 25.0, 30.0)
        with self.assertRaises(ValueError) as ctx:
            k.force_close(last_bar)
        self.assertIn("gross_pnl", str(ctx.exception))

        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

    # 47. Mark to market arithmetic overflow rejects without mutating state
    def test_47_mark_to_market_arithmetic_overflow(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1e308, contract_size=1.0, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))

        bal_before = k.balance
        pos_before = k.position

        with self.assertRaises(ValueError) as ctx:
            k.mark_to_market(20.0)
        self.assertIn("floating_gross", str(ctx.exception))

        # Verify state is not mutated
        self.assertEqual(k.balance, bal_before)
        self.assertIs(k.position, pos_before)

    # 48. Exit marker freeze failure atomic rollback and clean retry
    def test_48_exit_marker_freeze_failure_atomic_rollback_and_retry(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.0, commission_per_side=0.0)
        bar1 = make_bar(1, 1000, "t1", 10.0, 15.0, 5.0, 10.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=10.0))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        bar2 = make_bar(2, 2000, "t2", 15.0, 20.0, 10.0, 15.0)

        # Inject failure specifically when freezing exit marker (shape == 'circle')
        orig_deep_freeze = k._close_position.__globals__["_deep_freeze"]

        def mock_freeze_exit_marker_fail(val, _seen=None):
            if isinstance(val, dict) and val.get("shape") == "circle" and "EXIT" in str(val.get("text", "")):
                raise RuntimeError("Injected exit marker freeze explosion")
            return orig_deep_freeze(val, _seen=_seen)

        with patch.dict(k._close_position.__globals__, {"_deep_freeze": mock_freeze_exit_marker_fail}):
            with self.assertRaises(RuntimeError):
                k.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))

        # Verify state is strictly untouched
        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

        # Retry after unpatching: must succeed cleanly once without double-counting
        retry_trans = k.process_open(bar2, OpenInstruction(action="CLOSE_ONLY", direction="SELL"))
        self.assertEqual(retry_trans.status, "CLOSED")
        self.assertEqual(k.balance, snap_before["balance"] + 5.0)
        self.assertIsNone(k.position)
        self.assertEqual(k._trade_counter, 1)
        self.assertEqual(len(k.trades), 1)
        self.assertEqual(k.trades[0]["trade_id"], 1)
        self.assertEqual(len(k.markers), 2)  # 1 entry marker + 1 exit marker
        self.assertEqual(k.markers[1]["shape"], "circle")

    # 49. Short intrabar ask_high overflow raises ValueError and maintains atomic state
    def test_49_short_intrabar_ask_high_overflow_rollback(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        # Bar with high = 1e308 -> ask_high = 1e308 + 1e308 = inf
        bar_high_overflow = make_bar(2, 2000, "t2", 100.0, 1e308, 95.0, 100.0)
        with self.assertRaises(ValueError) as ctx:
            k.process_intrabar(bar_high_overflow)
        self.assertIn("ask_high", str(ctx.exception))

        # Absolute state preservation
        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k.position.direction, "SELL")
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

    # 50. Short intrabar ask_low overflow raises ValueError, does not trigger TP, preserves state
    def test_50_short_intrabar_ask_low_overflow_rollback(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=-1e308, commission_per_side=0.50, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        # Bar with low = -1e308 -> ask_low = -1e308 + (-1e308) = -inf
        bar_low_overflow = make_bar(2, 2000, "t2", 100.0, 105.0, -1e308, 100.0)
        with self.assertRaises(ValueError) as ctx:
            k.process_intrabar(bar_low_overflow)
        self.assertIn("ask_low", str(ctx.exception))

        # Absolute state preservation: no TP trigger, position still active
        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k.position.direction, "SELL")
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

    # 51. Short intrabar normal finite Ask triggers SL and TP correctly
    def test_51_short_intrabar_normal_finite_ask_triggers(self) -> None:
        # Normal SL trigger
        k_sl = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.20, commission_per_side=0.0)
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k_sl.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=105.0, tp_price=90.0))
        # high=104.90, ask_high=105.10 >= 105.0 -> SL hit
        bar_sl = make_bar(2, 2000, "t2", 102.0, 104.90, 101.0, 103.0)
        tr_sl = k_sl.process_intrabar(bar_sl)
        self.assertEqual(tr_sl.status, "STOPPED")
        self.assertEqual(tr_sl.closed_trade["exit_price"], 105.0)

        # Normal TP trigger
        k_tp = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=0.20, commission_per_side=0.0)
        k_tp.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=95.0))
        # low=94.70, ask_low=94.90 <= 95.0 -> TP hit
        bar_tp = make_bar(2, 2000, "t2", 98.0, 99.0, 94.70, 96.0)
        tr_tp = k_tp.process_intrabar(bar_tp)
        self.assertEqual(tr_tp.status, "TARGETED")
        self.assertEqual(tr_tp.closed_trade["exit_price"], 95.0)

    # 52. Short reversal with bar.open + spread overflow fails before commit
    def test_52_short_reversal_ask_overflow_atomic_rollback(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        # Reversal BUY order at bar with open=1e308 -> exit_price = 1e308 + 1e308 = inf
        bar_rev = make_bar(2, 2000, "t2", 1e308, 1e308, 95.0, 1e308)
        with self.assertRaises(ValueError) as ctx:
            k.process_open(bar_rev, OpenInstruction(action="OPEN_OR_REVERSE", direction="BUY", entry_price=100.0, sl_price=90.0, tp_price=110.0, source="legacy"))
        self.assertIn("exit_price", str(ctx.exception))

        # Position intact, no trades, no markers added
        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k.position.direction, "SELL")
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

    # 53. Short forced close with bar.close + spread overflow fails before commit and retries cleanly
    def test_53_short_forced_close_ask_overflow_atomic_rollback_and_retry(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))

        snap_before = {
            "balance": k.balance,
            "position": k.position,
            "trade_counter": k._trade_counter,
            "trades_len": len(k._trades),
            "markers_len": len(k._markers),
        }

        # Final bar with close = 1e308 -> exit_price = 1e308 + 1e308 = inf
        bar_fc = make_bar(2, 2000, "t2", 100.0, 105.0, 95.0, 1e308)
        with self.assertRaises(ValueError) as ctx:
            k.force_close(bar_fc)
        self.assertIn("exit_price", str(ctx.exception))

        # Position still open, state untouched
        self.assertEqual(k.balance, snap_before["balance"])
        self.assertIs(k.position, snap_before["position"])
        self.assertEqual(k._trade_counter, snap_before["trade_counter"])
        self.assertEqual(len(k._trades), snap_before["trades_len"])
        self.assertEqual(len(k._markers), snap_before["markers_len"])

        # Retry with valid bar succeeds exactly once
        k.spread_val = 0.20
        bar_retry = make_bar(3, 3000, "t3", 100.0, 105.0, 95.0, 100.0)
        tr = k.force_close(bar_retry)
        self.assertEqual(tr.status, "FORCED_CLOSED")
        self.assertIsNone(k.position)
        self.assertEqual(k._trade_counter, 1)
        self.assertEqual(len(k.trades), 1)

    # 54. Short mark-to-market with close_bid + spread overflow raises ValueError without mutating state
    def test_54_short_mark_to_market_ask_overflow(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.50, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, source="legacy"))

        bal_before = k.balance
        pos_before = k.position

        with self.assertRaises(ValueError) as ctx:
            k.mark_to_market(1e308)
        self.assertIn("close_ask", str(ctx.exception))

        self.assertEqual(k.balance, bal_before)
        self.assertIs(k.position, pos_before)

    # 55. Legacy negative spread preserved if calculations remain finite
    def test_55_legacy_negative_spread_finite_behavior(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=-0.20, commission_per_side=0.0, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=105.0, tp_price=90.0, source="legacy"))

        # ask_high = 105.10 + (-0.20) = 104.90 < 105.0 -> no trigger
        bar2 = make_bar(2, 2000, "t2", 102.0, 105.10, 101.0, 103.0)
        tr = k.process_intrabar(bar2)
        self.assertEqual(tr.status, "NO_ACTION")

        # ask_high = 105.30 + (-0.20) = 105.10 >= 105.0 -> trigger SL
        bar3 = make_bar(3, 3000, "t3", 102.0, 105.30, 101.0, 103.0)
        tr_sl = k.process_intrabar(bar3)
        self.assertEqual(tr_sl.status, "STOPPED")
        self.assertEqual(tr_sl.closed_trade["exit_price"], 105.0)

    # 56. Exact error type and message identifies the specific field that overflowed
    def test_56_arithmetic_overflow_exact_field_identification(self) -> None:
        k = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=1e308, commission_per_side=0.0, validation_mode="legacy")
        bar1 = make_bar(1, 1000, "t1", 100.0, 105.0, 95.0, 100.0)
        k.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))

        # ask_high identifier
        bar_high = make_bar(2, 2000, "t2", 100.0, 1e308, 95.0, 100.0)
        with self.assertRaises(ValueError) as ctx_high:
            k.process_intrabar(bar_high)
        self.assertEqual(str(ctx_high.exception), "'ask_high' must be finite, got inf")

        # ask_low identifier
        k_neg = ExecutionKernel(initial_capital=1000.0, lot_size=1.0, contract_size=1.0, spread_val=-1e308, commission_per_side=0.0, validation_mode="legacy")
        k_neg.process_open(bar1, OpenInstruction(action="OPEN_OR_REVERSE", direction="SELL", entry_price=100.0, sl_price=110.0, tp_price=90.0, source="legacy"))
        bar_low = make_bar(2, 2000, "t2", 100.0, 105.0, -1e308, 100.0)
        with self.assertRaises(ValueError) as ctx_low:
            k_neg.process_intrabar(bar_low)
        self.assertEqual(str(ctx_low.exception), "'ask_low' must be finite, got -inf")

        # exit_price identifier
        bar_close = make_bar(3, 3000, "t3", 100.0, 105.0, 95.0, 1e308)
        with self.assertRaises(ValueError) as ctx_exit:
            k.force_close(bar_close)
        self.assertEqual(str(ctx_exit.exception), "'exit_price' must be finite, got inf")

        # close_ask identifier
        with self.assertRaises(ValueError) as ctx_ask:
            k.mark_to_market(1e308)
        self.assertEqual(str(ctx_ask.exception), "'close_ask' must be finite, got inf")


if __name__ == "__main__":
    unittest.main()


