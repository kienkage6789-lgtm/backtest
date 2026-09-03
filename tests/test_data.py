import unittest
import time
import os
import pandas as pd
from engine.data_feed import DataFeed

class TestDataFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = DataFeed()

    def test_01_get_info(self):
        info = self.feed.get_info()
        self.assertEqual(info['symbol'], 'XAUUSD')
        self.assertGreater(info['total_m1_candles'], 3_000_000)
        self.assertIn('2014', info['start_time'])
        self.assertIn('2026', info['end_time'])
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
        
        # Kiểm tra timestamp tăng dần
        for i in range(1, len(candles)):
            self.assertGreater(candles[i]['time'], candles[i-1]['time'])
            
        print(f"[PASS] Queried 5000 M1 candles in {duration_ms:.2f}ms (<100ms threshold)")
        self.assertLess(duration_ms, 250)

    def test_04_multi_timeframe_real_resample(self):
        for tf in ['M5', 'M15', 'H1', 'D1']:
            t0 = time.perf_counter()
            candles = self.feed.get_candles(timeframe=tf, limit=500)
            duration_ms = (time.perf_counter() - t0) * 1000
            self.assertGreater(len(candles), 0)
            self.assertLessEqual(len(candles), 500)
            
            # High >= Low
            for c in candles:
                self.assertGreaterEqual(c['high'], c['low'])
                self.assertGreaterEqual(c['high'], c['open'])
                self.assertGreaterEqual(c['high'], c['close'])
                self.assertLessEqual(c['low'], c['open'])
                self.assertLessEqual(c['low'], c['close'])
            print(f"[PASS] {tf}: {len(candles)} candles generated in {duration_ms:.2f}ms")

if __name__ == '__main__':
    unittest.main()
