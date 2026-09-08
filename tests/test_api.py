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
        self.assertGreater(data["total_m1_candles"], 1_800_000)
        self.assertIn("2016", data["start_time"])
        print("\n[PASS] /api/info returned valid JSON with real DB counts")

    def test_02_api_candles_multi_timeframe(self):
        for tf in ["M1", "M15", "H1", "D1"]:
            response = self.client.get(f"/api/candles?timeframe={tf}&limit=50")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["symbol"], "XAUUSD")
            self.assertEqual(data["timeframe"], tf)
            self.assertEqual(len(data["candles"]), 50)
            c0 = data["candles"][0]
            for field in ["time", "open", "high", "low", "close", "volume", "datetime_str"]:
                self.assertIn(field, c0)
            print(f"[PASS] /api/candles?timeframe={tf} returned 50 valid candles")

    def test_03_api_candles_query_filters(self):
        # start + end + limit
        res = self.client.get("/api/candles?timeframe=M15&start_time=2024-01-01 00:00:00&end_time=2024-01-05 00:00:00&limit=25")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["candles"]), 25)

        # end_time only
        res = self.client.get("/api/candles?timeframe=H1&end_time=2024-03-01 12:00:00&limit=15")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["candles"]), 15)

        # before_time
        res = self.client.get("/api/candles?timeframe=M30&before_time=2024-04-01 00:00:00&limit=20")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["candles"]), 20)

    def test_04_api_candles_validation_errors(self):
        # Invalid timeframe -> 400
        res = self.client.get("/api/candles?timeframe=INVALID&limit=100")
        self.assertEqual(res.status_code, 400)

        # Invalid timestamp -> 400
        res = self.client.get("/api/candles?timeframe=M15&start_time=not-a-date")
        self.assertEqual(res.status_code, 400)

        # start_time > end_time -> 400
        res = self.client.get("/api/candles?timeframe=M15&start_time=2024-05-10 00:00:00&end_time=2024-05-01 00:00:00")
        self.assertEqual(res.status_code, 400)

        print("[PASS] /api/candles validation errors correctly returned HTTP 400")

    def test_05_api_strategies(self):
        response = self.client.get("/api/strategies")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("strategies", data)
        self.assertGreaterEqual(len(data["strategies"]), 4)
        print(f"[PASS] /api/strategies returned {len(data['strategies'])} strategies")

    def test_06_api_backtest(self):
        payload = {
            "timeframe": "H1",
            "limit": 200,
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
