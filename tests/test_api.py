import unittest
from fastapi.testclient import TestClient
from server import app

class TestServerAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_01_api_info(self):
        response = self.client.get("/api/info")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["symbol"], "XAUUSD")
        self.assertIn("start_time", data)
        self.assertIn("end_time", data)
        self.assertGreater(data["total_m1_candles"], 3000000)
        print("\n[PASS] /api/info returned valid JSON")

    def test_02_api_candles_m15(self):
        response = self.client.get("/api/candles?timeframe=M15&limit=100")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["symbol"], "XAUUSD")
        self.assertEqual(data["timeframe"], "M15")
        self.assertEqual(len(data["candles"]), 100)
        
        c0 = data["candles"][0]
        self.assertIn("time", c0)
        self.assertIn("open", c0)
        self.assertIn("high", c0)
        self.assertIn("low", c0)
        self.assertIn("close", c0)
        self.assertIn("volume", c0)
        print(f"[PASS] /api/candles?timeframe=M15 returned 100 candles, last close = {data['candles'][-1]['close']}")

    def test_04_api_strategies(self):
        response = self.client.get("/api/strategies")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("strategies", data)
        self.assertGreaterEqual(len(data["strategies"]), 4)
        print(f"[PASS] /api/strategies returned {len(data['strategies'])} strategies")

    def test_05_api_backtest(self):
        payload = {
            "timeframe": "H1",
            "limit": 500,
            "strategy_id": "sma_crossover",
            "strategy_params": {"fast_period": 10, "slow_period": 30},
            "initial_capital": 10000.0,
            "lot_size": 0.1,
            "stop_loss_points": 200.0,
            "take_profit_points": 400.0,
            "spread_points": 20.0,
            "commission_per_lot": 5.0,
            "allow_short": True
        }
        response = self.client.post("/api/backtest", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("metrics", data)
        self.assertIn("trades", data)
        self.assertIn("equity_curve", data)
        self.assertIn("markers", data)
        metrics = data["metrics"]
        print(f"[PASS] /api/backtest executed: {metrics['total_trades']} trades, WinRate={metrics['win_rate']}%, MDD={metrics['max_drawdown_pct']}%")

if __name__ == '__main__':
    unittest.main()
