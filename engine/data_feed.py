import sqlite3
import pandas as pd
import numpy as np
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'XAUUSD.db')

TIMEFRAME_MAP = {
    'M1': '1min',
    'M5': '5min',
    'M15': '15min',
    'M30': '30min',
    'H1': '1h',
    'H4': '4h',
    'D1': '1D'
}

TIMEFRAME_MINUTES = {
    'M1': 1,
    'M5': 5,
    'M15': 15,
    'M30': 30,
    'H1': 60,
    'H4': 240,
    'D1': 1440
}

class DataFeed:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._ensure_indexes()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def _ensure_indexes(self):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_xauusd_time ON XAUUSDc_M1(time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_xauusd_h1_time ON XAUUSD_H1(time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_xauusd_d1_time ON XAUUSD_D1(time)")
            conn.commit()
        except Exception as e:
            # Bỏ qua nếu bảng phụ chưa tồn tại
            pass
        finally:
            conn.close()

    def get_info(self):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT MIN(time), MAX(time), COUNT(*) FROM XAUUSDc_M1")
            min_time, max_time, count = cursor.fetchone()
            return {
                "symbol": "XAUUSD",
                "start_time": min_time,
                "end_time": max_time,
                "total_m1_candles": count,
                "timeframes": list(TIMEFRAME_MAP.keys())
            }
        finally:
            conn.close()

    def get_candles(self, timeframe='M1', start_time=None, end_time=None, limit=1000, before_time=None):
        """
        Lấy danh sách nến theo timeframe chuẩn hóa cho TradingView Lightweight Charts:
        - M1: query trực tiếp từ XAUUSDc_M1
        - M5, M15, M30: query từ XAUUSDc_M1 rồi resample
        - H1: query trực tiếp từ XAUUSD_H1
        - H4: query từ XAUUSD_H1 rồi resample (cực nhanh)
        - D1: query trực tiếp từ XAUUSD_D1
        """
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Khung thời gian không hợp lệ: {timeframe}. Hỗ trợ: {list(TIMEFRAME_MAP.keys())}")

        conn = self.get_connection()
        
        try:
            # 1. Trực tiếp từ bảng chuyên dụng (M1, H1, D1)
            if timeframe in ['M1', 'H1', 'D1']:
                table_map = {
                    'M1': 'XAUUSDc_M1',
                    'H1': 'XAUUSD_H1',
                    'D1': 'XAUUSD_D1'
                }
                table = table_map[timeframe]
                return self._query_direct(conn, table, start_time, end_time, limit, before_time)

            # 2. Resample từ H1 (đối với H4)
            elif timeframe == 'H4':
                h1_limit = int(limit * 4 * 1.5)
                df = self._query_to_df(conn, 'XAUUSD_H1', start_time, end_time, h1_limit, before_time)
                if df.empty:
                    return []
                resampled_df = self.resample_dataframe(df, 'H4')
                if len(resampled_df) > limit and (start_time is None or end_time is None):
                    resampled_df = resampled_df.tail(limit)
                return self._df_to_candles(resampled_df)

            # 3. Resample từ M1 (đối với M5, M15, M30)
            else:
                tf_minutes = TIMEFRAME_MINUTES[timeframe]
                m1_limit = int(limit * tf_minutes * 1.5)
                df = self._query_to_df(conn, 'XAUUSDc_M1', start_time, end_time, m1_limit, before_time)
                if df.empty:
                    return []
                resampled_df = self.resample_dataframe(df, timeframe)
                if len(resampled_df) > limit and (start_time is None or end_time is None):
                    resampled_df = resampled_df.tail(limit)
                return self._df_to_candles(resampled_df)

        finally:
            conn.close()

    def _query_to_df(self, conn, table, start_time, end_time, limit, before_time):
        query = f"SELECT time, open, high, low, close, tick_volume FROM {table}"
        conditions = []
        params = []

        if start_time and end_time:
            conditions.append("time >= ? AND time <= ?")
            params.extend([start_time, end_time])
            where_clause = " WHERE " + " AND ".join(conditions)
            sql = f"{query}{where_clause} ORDER BY time ASC"
        elif before_time:
            conditions.append("time < ?")
            params.append(before_time)
            where_clause = " WHERE " + " AND ".join(conditions)
            sql = f"SELECT * FROM ({query}{where_clause} ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
            params.append(limit)
        elif start_time:
            conditions.append("time >= ?")
            params.append(start_time)
            where_clause = " WHERE " + " AND ".join(conditions)
            sql = f"{query}{where_clause} ORDER BY time ASC LIMIT ?"
            params.append(limit)
        else:
            sql = f"SELECT * FROM ({query} ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
            params.append(limit)

        return pd.read_sql_query(sql, conn, params=params)

    def _query_direct(self, conn, table, start_time, end_time, limit, before_time):
        df = self._query_to_df(conn, table, start_time, end_time, limit, before_time)
        if df.empty:
            return []
        return self._df_to_candles(df)

    def resample_dataframe(self, df, timeframe):
        """
        Resample nến sang timeframe cao hơn.
        """
        if df.empty:
            return df

        rule = TIMEFRAME_MAP[timeframe]
        df = df.copy()
        df['datetime'] = pd.to_datetime(df['time'])
        df.set_index('datetime', inplace=True)

        resampled = df.resample(rule, label='left', closed='left').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'tick_volume': 'sum'
        }).dropna(subset=['open', 'close'])

        resampled['time'] = resampled.index.strftime('%Y-%m-%d %H:%M:%S')
        resampled.reset_index(drop=True, inplace=True)
        return resampled

    @staticmethod
    def _normalize_time(t):
        if not t:
            return None
        if isinstance(t, (int, float)):
            return pd.to_datetime(t, unit='s').strftime('%Y-%m-%d %H:%M:%S')
        if isinstance(t, str):
            try:
                val = float(t)
                if val > 100_000_000:
                    return pd.to_datetime(val, unit='s').strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                pass
        return str(t).strip()

    def _df_to_candles(self, df):
        """
        Chuyển đổi DataFrame sang list dict theo chuẩn TradingView Lightweight Charts.
        """
        if df is None or df.empty:
            return []

        df = df.copy()
        # Đối với nến ngày (D1) chỉ có ngày, parse vẫn ra 00:00:00 UTC
        timestamps = (pd.to_datetime(df['time']).astype('int64') // 10**9).tolist()
        
        records = []
        opens = pd.to_numeric(df['open'], errors='coerce').fillna(0).round(3).tolist()
        highs = pd.to_numeric(df['high'], errors='coerce').fillna(0).round(3).tolist()
        lows = pd.to_numeric(df['low'], errors='coerce').fillna(0).round(3).tolist()
        closes = pd.to_numeric(df['close'], errors='coerce').fillna(0).round(3).tolist()
        volumes = pd.to_numeric(df['tick_volume'], errors='coerce').fillna(0).tolist()
        time_strs = df['time'].tolist()

        for t, o, h, l, c, v, ts_str in zip(timestamps, opens, highs, lows, closes, volumes, time_strs):
            records.append({
                'time': int(t),
                'open': float(o),
                'high': float(h),
                'low': float(l),
                'close': float(c),
                'volume': float(v),
                'datetime_str': str(ts_str)
            })
        return records

    def get_replay_candles(self, timeframe='M15', cut_time='2024-01-01 00:00:00', history_limit=1000, future_limit=1000):
        """
        Lấy nến phục vụ chế độ Bar Replay:
        - history: các nến <= cut_time (sắp xếp tăng dần theo thời gian)
        - future: các nến > cut_time (sắp xếp tăng dần, làm bộ đệm để replay từng nến)
        """
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Khung thời gian không hợp lệ: {timeframe}")

        # Chuẩn hóa cut_time an toàn cho cả string timestamp, numeric hay ISO string
        cut_time = self._normalize_time(cut_time)
        if not cut_time:
            cut_time = '2024-01-01 00:00:00'

        conn = self.get_connection()
        try:
            if timeframe in ['M1', 'H1', 'D1']:
                table_map = {'M1': 'XAUUSDc_M1', 'H1': 'XAUUSD_H1', 'D1': 'XAUUSD_D1'}
                table = table_map[timeframe]
                time_col = "time"
                cmp_cut = cut_time[:10] if timeframe == 'D1' else cut_time

                # 1. History
                sql_hist = f"SELECT * FROM (SELECT time, open, high, low, close, tick_volume FROM {table} WHERE {time_col} <= ? ORDER BY {time_col} DESC LIMIT ?) ORDER BY {time_col} ASC"
                df_hist = pd.read_sql_query(sql_hist, conn, params=[cmp_cut, history_limit])

                # 2. Future
                sql_fut = f"SELECT time, open, high, low, close, tick_volume FROM {table} WHERE {time_col} > ? ORDER BY {time_col} ASC LIMIT ?"
                df_fut = pd.read_sql_query(sql_fut, conn, params=[cmp_cut, future_limit])

                if not df_hist.empty and not df_fut.empty:
                    last_hist_time = df_hist.iloc[-1]['time']
                    df_fut = df_fut[df_fut['time'] > last_hist_time]

                history = self._df_to_candles(df_hist)
                future = self._df_to_candles(df_fut)
                return {"history": history, "future": future}

            elif timeframe == 'H4':
                # Từ H1
                sql_hist = f"SELECT * FROM (SELECT time, open, high, low, close, tick_volume FROM XAUUSD_H1 WHERE time <= ? ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
                df_hist = pd.read_sql_query(sql_hist, conn, params=[cut_time, int(history_limit * 4 * 1.5)])
                res_hist = self.resample_dataframe(df_hist, 'H4')
                if len(res_hist) > history_limit:
                    res_hist = res_hist.tail(history_limit)

                sql_fut = f"SELECT time, open, high, low, close, tick_volume FROM XAUUSD_H1 WHERE time >= ? ORDER BY time ASC LIMIT ?"
                df_fut = pd.read_sql_query(sql_fut, conn, params=[cut_time, int(future_limit * 4 * 1.5)])
                res_fut = self.resample_dataframe(df_fut, 'H4')
                
                if not res_hist.empty and not res_fut.empty:
                    last_hist_time = res_hist.iloc[-1]['time']
                    res_fut = res_fut[res_fut['time'] > last_hist_time]

                if len(res_fut) > future_limit:
                    res_fut = res_fut.head(future_limit)

                return {
                    "history": self._df_to_candles(res_hist),
                    "future": self._df_to_candles(res_fut)
                }

            else: # M5, M15, M30
                tf_minutes = TIMEFRAME_MINUTES[timeframe]
                m1_hist_limit = int(history_limit * tf_minutes * 1.5)
                sql_hist = f"SELECT * FROM (SELECT time, open, high, low, close, tick_volume FROM XAUUSDc_M1 WHERE time <= ? ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
                df_hist = pd.read_sql_query(sql_hist, conn, params=[cut_time, m1_hist_limit])
                res_hist = self.resample_dataframe(df_hist, timeframe)
                if len(res_hist) > history_limit:
                    res_hist = res_hist.tail(history_limit)

                m1_fut_limit = int(future_limit * tf_minutes * 1.5)
                sql_fut = f"SELECT time, open, high, low, close, tick_volume FROM XAUUSDc_M1 WHERE time >= ? ORDER BY time ASC LIMIT ?"
                df_fut = pd.read_sql_query(sql_fut, conn, params=[cut_time, m1_fut_limit])
                res_fut = self.resample_dataframe(df_fut, timeframe)
                
                if not res_hist.empty and not res_fut.empty:
                    last_hist_time = res_hist.iloc[-1]['time']
                    res_fut = res_fut[res_fut['time'] > last_hist_time]

                if len(res_fut) > future_limit:
                    res_fut = res_fut.head(future_limit)

                return {
                    "history": self._df_to_candles(res_hist),
                    "future": self._df_to_candles(res_fut)
                }

        finally:
            conn.close()

