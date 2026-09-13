import unittest
import pandas as pd
import numpy as np
from smc.models import SwingPoint, LiquidityPool, LiquiditySweep
from smc.data_contract import normalize_ohlcv
from smc.structure.swings import detect_swings


class TestSMCLiquidity(unittest.TestCase):
    """Unit and Integration tests for SMC Liquidity Pool & Liquidity Sweep detection."""

    def setUp(self):
        # Create a baseline synthetic DataFrame with controlled prices
        times = pd.date_range("2026-01-01", periods=100, freq="1h", tz="UTC")
        ohlc = []
        for i in range(100):
            ohlc.append({
                "time": times[i],
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 100.0
            })
        self.df = normalize_ohlcv(pd.DataFrame(ohlc))

    def test_01_model_initialization_and_serialization(self):
        """Test LiquidityPool and LiquiditySweep instantiation and to_dict() serialization."""
        ts = pd.Timestamp("2026-01-01 10:00:00", tz="UTC")
        pool = LiquidityPool(
            kind="equal_highs",
            price=1005.0,
            price_max=1005.2,
            price_min=1004.8,
            indices=[10, 20],
            created_at=25,
            confirmed_at=25,
            swept=False,
            mode="swing"
        )
        pool_dict = pool.to_dict()
        self.assertEqual(pool_dict["kind"], "equal_highs")
        self.assertEqual(pool_dict["price"], 1005.0)
        self.assertEqual(pool_dict["indices"], [10, 20])
        self.assertEqual(pool_dict["created_at"], 25)
        self.assertFalse(pool_dict["swept"])

        sweep = LiquiditySweep(
            index=30,
            time=ts,
            direction="bearish",
            pool_kind="equal_highs",
            pool_price=1005.0,
            pool_indices=[10, 20],
            price_wick=1006.5,
            close_price=1004.5,
            created_at=30,
            confirmed_at=30,
            swept_at=30,
            sweep_type="clean"
        )
        sweep_dict = sweep.to_dict()
        self.assertEqual(sweep_dict["index"], 30)
        self.assertEqual(sweep_dict["direction"], "bearish")
        self.assertEqual(sweep_dict["price_wick"], 1006.5)
        self.assertEqual(sweep_dict["close_price"], 1004.5)
        self.assertEqual(sweep_dict["sweep_type"], "clean")

    def test_02_tolerance_validation_and_errors(self):
        """Validate that negative or zero tolerance values raise ValueError."""
        from smc.liquidity.detector import detect_liquidity_pools, LiquidityTracker
        
        ts = pd.date_range("2026-01-01", periods=10, freq="1h", tz="UTC")
        swings = [
            SwingPoint(index=2, time=ts[2], price=100.0, kind="high", strength=2, confirmed_at=4),
            SwingPoint(index=5, time=ts[5], price=100.05, kind="high", strength=2, confirmed_at=7)
        ]
        
        with self.assertRaises(ValueError):
            detect_liquidity_pools(self.df.iloc[:10], swings, tolerance_pct=-0.01)
            
        with self.assertRaises(ValueError):
            detect_liquidity_pools(self.df.iloc[:10], swings, tolerance_pct=0.0)

        with self.assertRaises(ValueError):
            LiquidityTracker(tolerance_pips=-1.0)

    def test_03_equal_highs_and_lows_detection(self):
        """Test detection of equal highs and equal lows within percentage and pips tolerance."""
        from smc.liquidity.detector import detect_liquidity_pools

        ts = pd.date_range("2026-01-01", periods=50, freq="1h", tz="UTC")
        # Ensure underlying candle prices do not break pools prematurely
        ohlc = []
        for i in range(50):
            ohlc.append({"time": ts[i], "open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0, "volume": 10})
        df = normalize_ohlcv(pd.DataFrame(ohlc))

        # Highs at index 10 (price 100.0) and index 20 (price 100.08) -> within 0.1% tolerance
        swings = [
            SwingPoint(index=10, time=ts[10], price=100.0, kind="high", strength=2, confirmed_at=12),
            SwingPoint(index=20, time=ts[20], price=100.08, kind="high", strength=2, confirmed_at=22),
            SwingPoint(index=30, time=ts[30], price=90.0, kind="low", strength=2, confirmed_at=32),
            SwingPoint(index=40, time=ts[40], price=90.05, kind="low", strength=2, confirmed_at=42),
        ]

        pools = detect_liquidity_pools(df, swings, tolerance_pct=0.001)
        eq_highs = [p for p in pools if p.kind == "equal_highs"]
        eq_lows = [p for p in pools if p.kind == "equal_lows"]

        self.assertEqual(len(eq_highs), 1)
        self.assertEqual(eq_highs[0].indices, [10, 20])
        self.assertAlmostEqual(eq_highs[0].price, 100.04, places=2)
        self.assertEqual(eq_highs[0].created_at, 22)

        self.assertEqual(len(eq_lows), 1)
        self.assertEqual(eq_lows[0].indices, [30, 40])
        self.assertAlmostEqual(eq_lows[0].price, 90.025, places=2)
        self.assertEqual(eq_lows[0].created_at, 42)

    def test_04_unconfirmed_swings_are_ignored(self):
        """Unconfirmed swings (confirmed_at > current bar) must NOT form pools."""
        from smc.liquidity.detector import detect_liquidity_pools

        ts = pd.date_range("2026-01-01", periods=50, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0, "volume": 10} for i in range(50)]
        df = normalize_ohlcv(pd.DataFrame(ohlc))

        swings = [
            SwingPoint(index=10, time=ts[10], price=100.0, kind="high", strength=5, confirmed_at=15),
            SwingPoint(index=20, time=ts[20], price=100.05, kind="high", strength=5, confirmed_at=25), # Confirmed at 25
        ]

        # Call with cutoff at bar 20 (before swing at 20 is confirmed)
        pools_at_20 = detect_liquidity_pools(df.iloc[:21], swings, tolerance_pct=0.001, replay_cutoff=20)
        eq_highs_20 = [p for p in pools_at_20 if p.kind == "equal_highs"]
        self.assertEqual(len(eq_highs_20), 0)

        # Call with cutoff at bar 25
        pools_at_25 = detect_liquidity_pools(df.iloc[:26], swings, tolerance_pct=0.001, replay_cutoff=25)
        eq_highs_25 = [p for p in pools_at_25 if p.kind == "equal_highs"]
        self.assertEqual(len(eq_highs_25), 1)

    def test_05_wick_only_sweep_detection(self):
        """Test bearish sweep when candle high wicks above equal highs and closes back below."""
        from smc.liquidity.detector import LiquidityTracker

        ts = pd.date_range("2026-01-01", periods=40, freq="1h", tz="UTC")
        ohlc = []
        for i in range(40):
            ohlc.append({"time": ts[i], "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10})
        df = normalize_ohlcv(pd.DataFrame(ohlc))

        tracker = LiquidityTracker(tolerance_pct=0.002)

        s1 = SwingPoint(index=5, time=ts[5], price=105.0, kind="high", strength=2, confirmed_at=7)
        s2 = SwingPoint(index=10, time=ts[10], price=105.1, kind="high", strength=2, confirmed_at=12)

        for i in range(16):
            c = df.iloc[i].to_dict()
            c["bar_index"] = i
            # On bar 15: Create a wick sweep (high=106.0 > 105.1, close=104.0 <= 105.1)
            if i == 15:
                c["high"] = 106.0
                c["close"] = 104.0
                c["open"] = 104.5
                c["low"] = 103.5
            
            new_swings = []
            if i == 7:
                new_swings.append(s1)
            if i == 12:
                new_swings.append(s2)
            
            tracker.update(c, newly_confirmed_swings=new_swings)

        sweeps = tracker.get_sweeps()
        self.assertEqual(len(sweeps), 1)
        self.assertEqual(sweeps[0].index, 15)
        self.assertEqual(sweeps[0].direction, "bearish")
        self.assertEqual(sweeps[0].price_wick, 106.0)
        self.assertEqual(sweeps[0].close_price, 104.0)

        # Verify pool lifecycle: pool is marked swept & valid=False
        pools = tracker.get_all_pools()
        eq_pool = [p for p in pools if p.kind == "equal_highs"][0]
        self.assertTrue(eq_pool.swept)
        self.assertEqual(eq_pool.swept_at, 15)
        self.assertFalse(eq_pool.valid)

    def test_06_close_break_is_invalidation_not_sweep(self):
        """Candle closing strictly above equal highs is a Breakout/Invalidation, NOT a sweep."""
        from smc.liquidity.detector import LiquidityTracker

        ts = pd.date_range("2026-01-01", periods=30, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10} for i in range(30)]
        df = normalize_ohlcv(pd.DataFrame(ohlc))

        tracker = LiquidityTracker(tolerance_pct=0.002)

        s1 = SwingPoint(index=5, time=ts[5], price=105.0, kind="high", strength=2, confirmed_at=7)
        s2 = SwingPoint(index=10, time=ts[10], price=105.1, kind="high", strength=2, confirmed_at=12)

        for i in range(16):
            c = df.iloc[i].to_dict()
            c["bar_index"] = i
            # On bar 15: Candle breaks through and closes ABOVE pool (open=104.5, high=107.0, close=106.5)
            if i == 15:
                c["open"] = 104.5
                c["high"] = 107.0
                c["low"] = 104.0
                c["close"] = 106.5

            new_swings = [s for s in [s1, s2] if s.confirmed_at == i]
            tracker.update(c, newly_confirmed_swings=new_swings)

        # Sweeps list must be empty
        sweeps = tracker.get_sweeps()
        self.assertEqual(len(sweeps), 0)

        # Pool must be invalidated with reason "close_break"
        pools = tracker.get_all_pools()
        eq_pool = [p for p in pools if p.kind == "equal_highs"][0]
        self.assertFalse(eq_pool.valid)
        self.assertFalse(eq_pool.swept)
        self.assertEqual(eq_pool.invalidated_at, 15)
        self.assertEqual(eq_pool.invalidation_reason, "close_break")

    def test_07_touch_only_is_not_sweep(self):
        """Candle high touching pool.price_max exactly without piercing (high == max_price) is NOT a sweep."""
        from smc.liquidity.detector import LiquidityTracker

        ts = pd.date_range("2026-01-01", periods=20, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10} for i in range(20)]
        df = normalize_ohlcv(pd.DataFrame(ohlc))

        tracker = LiquidityTracker(tolerance_pct=0.002)
        s1 = SwingPoint(index=5, time=ts[5], price=105.0, kind="high", strength=2, confirmed_at=7)
        s2 = SwingPoint(index=10, time=ts[10], price=105.0, kind="high", strength=2, confirmed_at=12)

        for i in range(16):
            c = df.iloc[i].to_dict()
            c["bar_index"] = i
            if i == 15:
                c["high"] = 105.0  # Touch only, does not exceed 105.0
                c["close"] = 104.0

            new_swings = [s for s in [s1, s2] if s.confirmed_at == i]
            tracker.update(c, newly_confirmed_swings=new_swings)

        sweeps = tracker.get_sweeps()
        self.assertEqual(len(sweeps), 0)

    def test_08_batch_and_incremental_parity(self):
        """Batch function detect_liquidity and incremental LiquidityTracker produce 100% equal results."""
        from smc.liquidity.detector import detect_liquidity, LiquidityTracker

        ts = pd.date_range("2026-01-01", periods=60, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 10} for i in range(60)]
        
        swings = [
            SwingPoint(index=10, time=ts[10], price=105.0, kind="high", strength=3, confirmed_at=13),
            SwingPoint(index=20, time=ts[20], price=105.08, kind="high", strength=3, confirmed_at=23),
            SwingPoint(index=30, time=ts[30], price=75.0, kind="low", strength=3, confirmed_at=33),
            SwingPoint(index=40, time=ts[40], price=74.95, kind="low", strength=3, confirmed_at=43),
        ]

        df = normalize_ohlcv(pd.DataFrame(ohlc))
        # Modify bar 28 to sweep high pool (high=106.0, close=104.0)
        df.iloc[28, df.columns.get_loc("high")] = 106.0
        df.iloc[28, df.columns.get_loc("close")] = 104.0
        
        # Modify bar 50 to sweep low pool (low=74.0, close=75.5)
        df.iloc[50, df.columns.get_loc("low")] = 74.0
        df.iloc[50, df.columns.get_loc("close")] = 75.5

        # 1. Run Batch
        batch_pools, batch_sweeps = detect_liquidity(df, swings, tolerance_pct=0.002)

        # 2. Run Incremental Tracker
        tracker = LiquidityTracker(tolerance_pct=0.002)
        for i in range(len(df)):
            c = df.iloc[i].to_dict()
            c["bar_index"] = i
            c["time"] = df.index[i]
            cur_swings = [s for s in swings if s.confirmed_at == i]
            tracker.update(c, newly_confirmed_swings=cur_swings)

        inc_pools = tracker.get_all_pools()
        inc_sweeps = tracker.get_sweeps()

        # Compare lengths
        self.assertEqual(len(batch_pools), len(inc_pools))
        self.assertEqual(len(batch_sweeps), len(inc_sweeps))

        # Compare dict contents
        for b_pool, i_pool in zip(batch_pools, inc_pools):
            self.assertEqual(b_pool.to_dict(), i_pool.to_dict())

        for b_sweep, i_sweep in zip(batch_sweeps, inc_sweeps):
            self.assertEqual(b_sweep.to_dict(), i_sweep.to_dict())

    def test_09_no_lookahead_and_replay_cutoff(self):
        """Verify replay cutoff N yields identical results to running up to bar N without future data leakage."""
        from smc.liquidity.detector import detect_liquidity

        ts = pd.date_range("2026-01-01", periods=100, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 10} for i in range(100)]
        df = normalize_ohlcv(pd.DataFrame(ohlc))
        df.iloc[35, df.columns.get_loc("high")] = 106.0
        df.iloc[35, df.columns.get_loc("close")] = 104.0

        swings = [
            SwingPoint(index=10, time=ts[10], price=105.0, kind="high", strength=3, confirmed_at=13),
            SwingPoint(index=20, time=ts[20], price=105.05, kind="high", strength=3, confirmed_at=23),
        ]

        # Run on full DF with replay_cutoff = 30 (before sweep at 35)
        pools_cutoff_30, sweeps_cutoff_30 = detect_liquidity(df, swings, tolerance_pct=0.002, replay_cutoff=30)
        pools_trunc_30, sweeps_trunc_30 = detect_liquidity(df.iloc[:31], swings, tolerance_pct=0.002)

        self.assertEqual(len(sweeps_cutoff_30), 0)
        self.assertEqual(len(sweeps_trunc_30), 0)
        self.assertEqual([p.to_dict() for p in pools_cutoff_30], [p.to_dict() for p in pools_trunc_30])

        # Run on full DF with replay_cutoff = 40 (after sweep at 35)
        pools_cutoff_40, sweeps_cutoff_40 = detect_liquidity(df, swings, tolerance_pct=0.002, replay_cutoff=40)
        pools_trunc_40, sweeps_trunc_40 = detect_liquidity(df.iloc[:41], swings, tolerance_pct=0.002)

        self.assertEqual(len(sweeps_cutoff_40), 1)
        self.assertEqual(len(sweeps_trunc_40), 1)
        self.assertEqual([s.to_dict() for s in sweeps_cutoff_40], [s.to_dict() for s in sweeps_trunc_40])

    def test_10_incremental_benchmark_10k_bars(self):
        """Incremental LiquidityTracker must process 10,000 bars in under 1.0 second."""
        from smc.liquidity.detector import LiquidityTracker
        import time

        n_bars = 10000
        ts = pd.date_range("2026-01-01", periods=n_bars, freq="1min", tz="UTC")
        np.random.seed(123)
        prices = 2000.0 + np.cumsum(np.random.randn(n_bars) * 0.5)

        df = normalize_ohlcv(pd.DataFrame({
            "open": prices,
            "high": prices + 1.0,
            "low": prices - 1.0,
            "close": prices + 0.1,
            "volume": 500.0
        }, index=ts))

        swings = detect_swings(df, left_strength=5, right_strength=5, mode="swing")
        swings_by_confirmed = {}
        for s in swings:
            swings_by_confirmed.setdefault(s.confirmed_at, []).append(s)

        tracker = LiquidityTracker(tolerance_pct=0.001)

        # Pre-extract candle dicts to avoid pandas indexing overhead during benchmark
        candles = df.reset_index().to_dict("records")
        for i, c in enumerate(candles):
            c["bar_index"] = i

        t0 = time.perf_counter()
        for i in range(n_bars):
            row = candles[i]
            cur_swings = swings_by_confirmed.get(i, [])
            tracker.update(row, newly_confirmed_swings=cur_swings)
        t1 = time.perf_counter()

        elapsed = t1 - t0
        print(f"\n[BENCHMARK] LiquidityTracker processed {n_bars} bars in {elapsed:.4f} seconds.")
        self.assertLess(elapsed, 1.0, f"LiquidityTracker took {elapsed:.2f}s, exceeding 1.0s limit!")


    def test_11_multiple_swings_merged_into_single_pool(self):
        """Three swing highs within tolerance must merge into a single equal_highs pool."""
        from smc.liquidity.detector import detect_liquidity_pools

        ts = pd.date_range("2026-01-01", periods=60, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 10} for i in range(60)]
        df = normalize_ohlcv(pd.DataFrame(ohlc))

        swings = [
            SwingPoint(index=10, time=ts[10], price=100.0, kind="high", strength=2, confirmed_at=12),
            SwingPoint(index=20, time=ts[20], price=100.05, kind="high", strength=2, confirmed_at=22),
            SwingPoint(index=30, time=ts[30], price=100.08, kind="high", strength=2, confirmed_at=32),
        ]

        pools = detect_liquidity_pools(df, swings, tolerance_pct=0.001)
        eq_highs = [p for p in pools if p.kind == "equal_highs"]
        self.assertEqual(len(eq_highs), 1)
        self.assertEqual(eq_highs[0].indices, [10, 20, 30])
        self.assertEqual(eq_highs[0].created_at, 32)
        self.assertAlmostEqual(eq_highs[0].price, 100.0433, places=3)

    def test_12_input_immutability(self):
        """Detection functions must not mutate input DataFrame or SwingPoints."""
        from smc.liquidity.detector import detect_liquidity

        ts = pd.date_range("2026-01-01", periods=30, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 10} for i in range(30)]
        df = normalize_ohlcv(pd.DataFrame(ohlc))
        df_orig = df.copy()

        swings = [
            SwingPoint(index=5, time=ts[5], price=100.0, kind="high", strength=2, confirmed_at=7),
            SwingPoint(index=10, time=ts[10], price=100.05, kind="high", strength=2, confirmed_at=12),
        ]
        swings_orig = [s.to_dict() for s in swings]

        detect_liquidity(df, swings, tolerance_pct=0.001)

        pd.testing.assert_frame_equal(df, df_orig)
        self.assertEqual([s.to_dict() for s in swings], swings_orig)

    def test_13_atr_tolerance_batch_and_incremental_parity(self):
        """Verify 100% parity between batch detect_liquidity and LiquidityTracker with ATR tolerance."""
        from smc.liquidity.detector import detect_liquidity, LiquidityTracker, calculate_atr

        ts = pd.date_range("2026-01-01", periods=60, freq="1h", tz="UTC")
        ohlc = [{"time": ts[i], "open": 90.0, "high": 95.0, "low": 85.0, "close": 90.0, "volume": 10} for i in range(60)]
        df = normalize_ohlcv(pd.DataFrame(ohlc))

        # Swings with price 100.0 and 105.0 -> difference is 5.0. ATR is 10.0 (high=95, low=85).
        # tolerance_atr_mult = 0.6 -> tolerance = 6.0 > 5.0 -> Should merge into equal_highs!
        swings = [
            SwingPoint(index=10, time=ts[10], price=100.0, kind="high", strength=2, confirmed_at=12),
            SwingPoint(index=20, time=ts[20], price=105.0, kind="high", strength=2, confirmed_at=22),
        ]

        # 1. Batch execution with tolerance_atr_mult (tolerance_pct=None)
        batch_pools, _ = detect_liquidity(df, swings, tolerance_pct=None, tolerance_atr_mult=0.6)

        # 2. Incremental execution passing atr_val to update()
        atr_series = calculate_atr(df, period=14)
        tracker = LiquidityTracker(tolerance_pct=None, tolerance_atr_mult=0.6)
        for i in range(len(df)):
            c = df.iloc[i].to_dict()
            c["bar_index"] = i
            c["time"] = df.index[i]
            cur_swings = [s for s in swings if s.confirmed_at == i]
            atr_val = float(atr_series.iloc[i]) if not pd.isna(atr_series.iloc[i]) else None
            tracker.update(c, newly_confirmed_swings=cur_swings, atr_val=atr_val)

        inc_pools = tracker.get_all_pools()

        # Both MUST detect equal_highs with exactly 1 pool
        self.assertEqual(len(batch_pools), 1)
        self.assertEqual(len(inc_pools), 1)
        self.assertEqual(batch_pools[0].kind, "equal_highs")
        self.assertEqual(inc_pools[0].kind, "equal_highs")
        self.assertEqual(batch_pools[0].to_dict(), inc_pools[0].to_dict())


    def test_14_public_active_pools_returns_defensive_clones_isolated(self):
        """Public get_active_pools() returns deep defensive clones, isolating tracker state from mutation."""
        from smc.liquidity.detector import LiquidityTracker
        from smc.models import SwingPoint, LiquidityPool

        ts = pd.date_range("2026-01-01", periods=20, freq="1h", tz="UTC")
        tracker = LiquidityTracker(tolerance_pct=0.002)
        s1 = SwingPoint(index=5, time=ts[5], price=100.0, kind="high", strength=2, confirmed_at=7)
        s2 = SwingPoint(index=10, time=ts[10], price=100.05, kind="high", strength=2, confirmed_at=12)

        for i in range(15):
            c = {"time": ts[i], "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 10, "bar_index": i}
            new_swings = [s for s in [s1, s2] if s.confirmed_at == i]
            tracker.update(c, newly_confirmed_swings=new_swings)

        public_pools = tracker.get_active_pools()
        self.assertEqual(len(public_pools), 1)
        self.assertTrue(public_pools[0].valid)

        # Mutate fields on returned pool
        public_pools[0].valid = False
        public_pools[0].price = 999999.0
        public_pools[0].indices.append(9999)
        public_pools[0].source_swings.append({"bad_field": 123})
        public_pools.append(LiquidityPool(kind="equal_lows", price=50.0, price_max=50.0, price_min=50.0, indices=[1, 2], created_at=1, confirmed_at=2))

        # Check that tracker state is 100% UNTOUCHED
        fresh_pools = tracker.get_active_pools()
        self.assertEqual(len(fresh_pools), 1)
        self.assertTrue(fresh_pools[0].valid)
        self.assertAlmostEqual(fresh_pools[0].price, 100.025, places=3)
        self.assertNotIn(9999, fresh_pools[0].indices)
        self.assertFalse(any("bad_field" in sw for sw in fresh_pools[0].source_swings))

        # Check internal iterator also untouched and yields active pools
        internal_pools = list(tracker._iter_active_pools_internal())
        self.assertEqual(len(internal_pools), 1)
        self.assertTrue(internal_pools[0].valid)
        self.assertAlmostEqual(internal_pools[0].price, 100.025, places=3)


if __name__ == "__main__":
    unittest.main()

