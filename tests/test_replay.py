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
        print("[PASS] Replay H1 and D1 verified on M1 base database")

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

    def test_04_dual_chart_sync_simulation(self):
        """Mô phỏng thuật toán while loop đồng bộ đa khung: Chart chính H1, Chart phụ M15 và M5."""
        cut_time = "2024-03-01 10:00:00"
        res_h1 = self.feed.get_replay_candles('H1', cut_time=cut_time, history_limit=10, future_limit=5)
        res_m15 = self.feed.get_replay_candles('M15', cut_time=cut_time, history_limit=40, future_limit=20)
        res_m5 = self.feed.get_replay_candles('M5', cut_time=cut_time, history_limit=60, future_limit=30)

        # 1. Test H1 chính + M15 phụ
        future_h1 = list(res_h1['future'])
        future_m15 = list(res_m15['future'])

        next_h1 = future_h1.pop(0)
        advanced_m15 = []
        while future_m15 and future_m15[0]['time'] <= next_h1['time']:
            advanced_m15.append(future_m15.pop(0))

        # 1 nến H1 tương ứng với tối đa 4 nến M15 trong cùng khung giờ
        self.assertGreater(len(advanced_m15), 0)
        self.assertLessEqual(len(advanced_m15), 4)
        for m in advanced_m15:
            self.assertLessEqual(m['time'], next_h1['time'])

        # 2. Test H1 chính + M5 phụ
        future_m5 = list(res_m5['future'])
        advanced_m5 = []
        while future_m5 and future_m5[0]['time'] <= next_h1['time']:
            advanced_m5.append(future_m5.pop(0))

        # 1 nến H1 tương ứng với tối đa 12 nến M5 trong cùng khung giờ
        self.assertGreater(len(advanced_m5), 0)
        self.assertLessEqual(len(advanced_m5), 12)
        for m in advanced_m5:
            self.assertLessEqual(m['time'], next_h1['time'])

        print(f"[PASS] Dual chart sync: 1 H1 bar advanced {len(advanced_m15)} M15 bars and {len(advanced_m5)} M5 bars without lagging")

    def test_05_api_replay_validation_errors(self):
        # Invalid timeframe -> 400
        res = self.client.get("/api/replay/init?timeframe=INVALID&cut_time=2024-01-01 00:00:00")
        self.assertEqual(res.status_code, 400)

        # Invalid cut_time -> 400
        res = self.client.get("/api/replay/init?timeframe=M15&cut_time=not-a-date")
        self.assertEqual(res.status_code, 400)

        print("[PASS] Replay API validation errors correctly returned HTTP 400")

    def test_06_future_queue_exhaustion(self):
        # Cắt tại thời điểm tương lai xa -> future rỗng
        res = self.client.get("/api/replay/init?timeframe=M15&cut_time=2030-01-01 00:00:00&history_limit=20&future_limit=20")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["future_count"], 0)
        self.assertEqual(data["future"], [])
        print("[PASS] Future queue exhaustion handled cleanly (empty list)")

    def test_07_partial_candle_exclusion_policy(self):
        """
        Kiểm tra chính sách loại bỏ partial candle khi cut_time nằm giữa nến:
        Ví dụ: M15 tại 14:07:00 (nằm giữa 14:00 và 14:15):
        - History loại bỏ partial candle 14:00, nến history cuối cùng là 13:45:00.
        - Future queue bắt đầu bằng nến 14:00:00 đầy đủ dữ liệu.
        - Không bị hụt nến hay trùng nến giữa history và future.
        """
        res = self.feed.get_replay_candles('M15', cut_time='2024-05-15 14:07:00', history_limit=10, future_limit=10)
        history = res['history']
        future = res['future']

        self.assertGreater(len(history), 0)
        self.assertGreater(len(future), 0)

        last_hist = history[-1]
        first_fut = future[0]

        self.assertEqual(last_hist['datetime_str'], '2024-05-15 13:45:00')
        self.assertEqual(first_fut['datetime_str'], '2024-05-15 14:00:00')
        self.assertLess(last_hist['time'], first_fut['time'])
        print(f"[PASS] Partial candle exclusion verified: Hist last={last_hist['datetime_str']}, Fut first={first_fut['datetime_str']}")

if __name__ == '__main__':
    unittest.main()
