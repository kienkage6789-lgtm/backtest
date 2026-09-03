import unittest
import pandas as pd
from engine.backtest_engine import BacktestEngine
from engine.data_feed import DataFeed

class TestBacktestEngine(unittest.TestCase):
    def test_01_tp_and_sl_execution(self):
        # Tạo chuỗi nến nhân tạo: Mua ở nến 1, nến 2 chạm TP
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1999.0, 'close': 2002.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2002.0, 'high': 2010.0, 'low': 2001.0, 'close': 2008.0, 'tick_volume': 150},
        ]
        df = pd.DataFrame(candles)

        # Giả lập chiến lược vào lệnh Buy ở nến 0
        from engine.strategies import StrategyRegistry
        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0])
            
            # SL = 500 points ($5), TP = 400 points ($4)
            # Entry price = 2002.0 + 0.20 spread = 2002.20
            # TP price = 2002.20 + 4.00 = 2006.20
            # Nến 2 high = 2010.0 >= 2006.20 -> TP triggered!
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                stop_loss_points=500.0,
                take_profit_points=400.0,
                spread_points=20.0,
                commission_per_lot=0.0
            )
            result = engine.run(df, "dummy", {})
            metrics = result['metrics']
            trades = result['trades']

            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]['exit_reason'], 'Take Profit')
            self.assertAlmostEqual(trades[0]['exit_price'], 2006.20)
            self.assertAlmostEqual(trades[0]['pnl'], 40.0) # 4.0 USD * 0.1 lot * 100 = $40
            self.assertEqual(metrics['winning_trades'], 1)
            self.assertEqual(metrics['win_rate'], 100.0)
            print("\n[PASS] Backtest TP execution and PnL calculation verified")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_02_real_data_sma_backtest(self):
        feed = DataFeed()
        candles = feed.get_candles('H1', limit=1000)
        df = pd.DataFrame(candles)

        engine = BacktestEngine(
            initial_capital=10000.0,
            lot_size=0.1,
            stop_loss_points=300.0,
            take_profit_points=600.0,
            spread_points=20.0,
            commission_per_lot=5.0
        )
        result = engine.run(df, "sma_crossover", {"fast_period": 10, "slow_period": 30})
        
        metrics = result['metrics']
        self.assertIn('net_profit', metrics)
        self.assertIn('win_rate', metrics)
        self.assertIn('profit_factor', metrics)
        self.assertIn('max_drawdown', metrics)
        self.assertGreater(metrics['total_trades'], 0)
        self.assertGreater(len(result['equity_curve']), 0)
        self.assertGreater(len(result['markers']), 0)
        
        print(f"[PASS] Real data H1 backtest completed: {metrics['total_trades']} trades, Net PnL: ${metrics['net_profit']:.2f}, Win rate: {metrics['win_rate']:.1f}%")

if __name__ == '__main__':
    unittest.main()
