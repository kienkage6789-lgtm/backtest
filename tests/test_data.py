import unittest
import time
import os
import sqlite3
import pandas as pd
from engine.data_feed import DataFeed

class TestDataFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = DataFeed()

    def test_01_get_info(self):
        info = self.feed.get_info()
        self.assertEqual(info['symbol'], 'XAUUSD')
        self.assertGreater(info['total_m1_candles'], 1_800_000)
        self.assertIn('2016', info['start_time'])
        self.assertIn('2026', info['end_time'])
        self.assertIn('H1', info['timeframes'])
        self.assertIn('D1', info['timeframes'])
        print(f"\n[PASS] Info: {info['total_m1_candles']} M1 bars from {info['start_time']} to {info['end_time']}")

    def test_02_synthetic_resample_accuracy(self):
        # Kiểm tra tính đúng đắn toán học của hàm resample
        data = {
            'time': [
                '2026-09-01 10:00:00',
                '2026-09-01 10:01:00',
                '2026-09-01 10:02:00',
                '2026-09-01 10:03:00',
                '2026-09-01 10:04:00',
            ],
            'open': [100.0, 102.0, 107.0, 105.0, 100.0],
            'high': [105.0, 108.0, 109.0, 106.0, 103.0],
            'low':  [99.0,  101.0, 104.0, 98.0,  99.0],
            'close':[102.0, 107.0, 105.0, 100.0, 101.0],
            'tick_volume': [10, 15, 20, 12, 8]
        }
        df = pd.DataFrame(data)
        res = self.feed.resample_dataframe(df, 'M5')
        
        self.assertEqual(len(res), 1)
        bar = res.iloc[0]
        self.assertEqual(bar['open'], 100.0)
        self.assertEqual(bar['high'], 109.0)
        self.assertEqual(bar['low'], 98.0)
        self.assertEqual(bar['close'], 101.0)
        self.assertEqual(bar['tick_volume'], 65)
        print("[PASS] Synthetic M5 Resample correctness verified (OHLCV matches perfectly)")

    def test_03_query_speed_and_format(self):
        # Đo tốc độ query 5000 nến M1
        t0 = time.perf_counter()
        candles = self.feed.get_candles('M1', limit=5000)
        duration_ms = (time.perf_counter() - t0) * 1000
        
        self.assertEqual(len(candles), 5000)
        first_candle = candles[0]
        self.assertIn('time', first_candle)
        self.assertIn('open', first_candle)
        self.assertIn('high', first_candle)
        self.assertIn('low', first_candle)
        self.assertIn('close', first_candle)
        self.assertIn('volume', first_candle)
        self.assertIn('datetime_str', first_candle)
        
        # Timestamp phải là unix seconds hợp lý (năm 2026 ~ 1.78e9)
        self.assertGreater(first_candle['time'], 1_400_000_000)
        self.assertLess(first_candle['time'], 2_000_000_000)

        # Kiểm tra timestamp tăng dần
        for i in range(1, len(candles)):
            self.assertGreater(candles[i]['time'], candles[i-1]['time'])
            
        print(f"[PASS] Queried 5000 M1 candles in {duration_ms:.2f}ms (<250ms threshold)")
        self.assertLess(duration_ms, 250)

    def test_04_multi_timeframe_real_resample(self):
        for tf in ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1']:
            t0 = time.perf_counter()
            candles = self.feed.get_candles(timeframe=tf, limit=100)
            duration_ms = (time.perf_counter() - t0) * 1000
            self.assertGreater(len(candles), 0)
            self.assertLessEqual(len(candles), 100)
            
            # High >= Low, High >= Open, High >= Close
            for c in candles:
                self.assertGreaterEqual(c['high'], c['low'])
                self.assertGreaterEqual(c['high'], c['open'])
                self.assertGreaterEqual(c['high'], c['close'])
                self.assertLessEqual(c['low'], c['open'])
                self.assertLessEqual(c['low'], c['close'])

            # Timestamp phải tăng dần
            for i in range(1, len(candles)):
                self.assertGreater(candles[i]['time'], candles[i-1]['time'])

            print(f"[PASS] {tf}: {len(candles)} candles generated in {duration_ms:.2f}ms")

    def test_05_range_with_limit(self):
        start = "2024-01-02 00:00:00"
        end = "2024-01-10 00:00:00"
        limit = 50
        candles = self.feed.get_candles('M15', start_time=start, end_time=end, limit=limit)
        self.assertEqual(len(candles), limit)
        for c in candles:
            self.assertGreaterEqual(c['datetime_str'], start)
            self.assertLessEqual(c['datetime_str'], end)
        # Sắp xếp tăng dần
        for i in range(1, len(candles)):
            self.assertGreater(candles[i]['time'], candles[i-1]['time'])
        print("[PASS] start_time + end_time + limit respects limit and range")

    def test_06_only_end_time(self):
        end = "2024-06-01 12:00:00"
        limit = 20
        candles = self.feed.get_candles('H1', end_time=end, limit=limit)
        self.assertEqual(len(candles), limit)
        self.assertLessEqual(candles[-1]['datetime_str'], end)
        for i in range(1, len(candles)):
            self.assertGreater(candles[i]['time'], candles[i-1]['time'])
        print(f"[PASS] Only end_time returned candles up to {candles[-1]['datetime_str']} <= {end}")

    def test_07_before_time(self):
        before = "2024-05-01 00:00:00"
        limit = 30
        candles = self.feed.get_candles('M15', before_time=before, limit=limit)
        self.assertEqual(len(candles), limit)
        for c in candles:
            self.assertLess(c['datetime_str'], before)
        for i in range(1, len(candles)):
            self.assertGreater(candles[i]['time'], candles[i-1]['time'])
        print(f"[PASS] before_time returned candles strictly < {before} in ascending order")

    def test_08_invalid_range_and_timestamps(self):
        # start_time > end_time
        with self.assertRaises(ValueError):
            self.feed.get_candles('M15', start_time="2024-05-10 00:00:00", end_time="2024-05-01 00:00:00")

        # invalid timestamp format
        with self.assertRaises(ValueError):
            self.feed.get_candles('M15', start_time="invalid-date-string")

        with self.assertRaises(ValueError):
            self.feed.get_candles('M15', end_time="2024/99/99")

        # invalid timeframe
        with self.assertRaises(ValueError):
            self.feed.get_candles('M7')

        print("[PASS] Invalid range, timestamp, and timeframe correctly raised ValueError")

    def test_09_empty_range(self):
        # Range trong tương lai xa không có dữ liệu
        candles = self.feed.get_candles('M15', start_time="2035-01-01 00:00:00", end_time="2035-01-02 00:00:00")
        self.assertEqual(candles, [])
        print("[PASS] Empty range returned empty list without error")

    def test_10_invalid_db_rejection(self):
        # DB không tồn tại
        with self.assertRaises(FileNotFoundError):
            DataFeed("data/non_existent_file.db")

        # DB trống không có bảng M1
        temp_db = "data/temp_empty_test.db"
        try:
            conn = sqlite3.connect(temp_db)
            conn.execute("CREATE TABLE foo (id INT)")
            conn.close()
            with self.assertRaises(RuntimeError):
                DataFeed(temp_db)
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)
        print("[PASS] Invalid DB path and invalid schema rejected cleanly")

if __name__ == '__main__':
    unittest.main()
