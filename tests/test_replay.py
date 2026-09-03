import unittest
from fastapi.testclient import TestClient
from engine.data_feed import DataFeed
from server import app

class TestReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = DataFeed()
        cls.client = TestClient(app)

    def test_01_feed_replay_m15(self):
        cut_time = "2024-05-15 14:00:00"
        result = self.feed.get_replay_candles('M15', cut_time=cut_time, history_limit=100, future_limit=50)
        
        history = result['history']
        future = result['future']

        self.assertEqual(len(history), 100)
        self.assertEqual(len(future), 50)

        # Kiểm tra tính tuần tự
        last_hist = history[-1]
        first_fut = future[0]

        self.assertLessEqual(last_hist['datetime_str'], cut_time)
        self.assertGreater(first_fut['datetime_str'], cut_time)
        self.assertLess(last_hist['time'], first_fut['time'])
        print(f"\n[PASS] Replay M15 data split correctly: Hist last={last_hist['datetime_str']}, Fut first={first_fut['datetime_str']}")

    def test_02_feed_replay_h1_and_d1(self):
        cut_time = "2024-01-10 00:00:00"
        
        # Test H1
        res_h1 = self.feed.get_replay_candles('H1', cut_time=cut_time, history_limit=50, future_limit=20)
        self.assertGreater(len(res_h1['history']), 0)
        self.assertGreater(len(res_h1['future']), 0)
        self.assertLess(res_h1['history'][-1]['time'], res_h1['future'][0]['time'])

        # Test D1
        res_d1 = self.feed.get_replay_candles('D1', cut_time=cut_time, history_limit=30, future_limit=15)
        self.assertGreater(len(res_d1['history']), 0)
        self.assertGreater(len(res_d1['future']), 0)
        self.assertLess(res_d1['history'][-1]['time'], res_d1['future'][0]['time'])
        print("[PASS] Replay H1 and D1 verified")

    def test_03_api_replay_init(self):
        cut_time = "2024-08-01 10:00:00"
        res = self.client.get(f"/api/replay/init?timeframe=M15&cut_time={cut_time}&history_limit=50&future_limit=20")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["history_count"], 50)
        self.assertEqual(data["future_count"], 20)
        self.assertIn("history", data)
        self.assertIn("future", data)
        print(f"[PASS] /api/replay/init returned {data['history_count']} history and {data['future_count']} future bars")

if __name__ == '__main__':
    unittest.main()
