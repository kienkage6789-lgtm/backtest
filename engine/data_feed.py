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

REQUIRED_COLUMNS = {'time', 'open', 'high', 'low', 'close', 'tick_volume'}

class DataFeed:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database không tồn tại tại: {self.db_path}")
        self._inspect_schema_and_ensure_indexes()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def _inspect_schema_and_ensure_indexes(self):
        """
        Kiểm tra schema thật của database:
        - Phát hiện bảng dữ liệu gốc (XAUUSD_M1 hoặc alias XAUUSDc_M1).
        - Kiểm tra các cột bắt buộc: time, open, high, low, close, tick_volume.
        - Phát hiện bảng H1/D1 nếu đã được materialize.
        - Đảm bảo index trên cột time tồn tại, báo lỗi rõ ràng nếu không thể lập index.
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            if 'XAUUSD_M1' in tables:
                self.base_table = 'XAUUSD_M1'
            elif 'XAUUSDc_M1' in tables:
                self.base_table = 'XAUUSDc_M1'
            else:
                raise RuntimeError(
                    f"Database '{self.db_path}' không chứa bảng nến M1 hợp lệ. "
                    f"Cần 'XAUUSD_M1' hoặc 'XAUUSDc_M1', nhưng chỉ tìm thấy: {tables}"
                )

            # Kiểm tra các cột trong bảng gốc
            cursor.execute(f"PRAGMA table_info({self.base_table})")
            columns = {row[1] for row in cursor.fetchall()}
            missing_cols = REQUIRED_COLUMNS - columns
            if missing_cols:
                raise RuntimeError(
                    f"Bảng '{self.base_table}' thiếu các cột bắt buộc: {missing_cols}"
                )

            # Kiểm tra xem có bảng H1, D1 sẵn không
            self.has_h1_table = 'XAUUSD_H1' in tables
            self.has_d1_table = 'XAUUSD_D1' in tables

            # Tạo index trên cột time nếu chưa có
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.base_table}_time ON {self.base_table}(time)")
            if self.has_h1_table:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_xauusd_h1_time ON XAUUSD_H1(time)")
            if self.has_d1_table:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_xauusd_d1_time ON XAUUSD_D1(time)")
            conn.commit()
        except Exception as e:
            if not isinstance(e, RuntimeError):
                raise RuntimeError(f"Lỗi khi kiểm tra schema hoặc lập index database: {e}") from e
            raise
        finally:
            conn.close()

    def get_info(self):
        """Lấy thông tin tổng quan từ bảng nến M1 gốc."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT MIN(time), MAX(time), COUNT(*) FROM {self.base_table}")
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

    @staticmethod
    def _normalize_and_validate_time(t, param_name="time"):
        """Chuẩn hóa và validate giá trị timestamp đầu vào."""
        if t is None:
            return None
        t_str = str(t).strip()
        if not t_str:
            return None

        # Trường hợp unix timestamp dạng số hoặc chuỗi số
        try:
            val = float(t_str)
            if val > 100_000_000:
                return pd.to_datetime(val, unit='s').strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

        # Trường hợp chuỗi datetime
        try:
            parsed = pd.to_datetime(t_str)
            return parsed.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            raise ValueError(
                f"Định dạng {param_name} không hợp lệ: '{t}'. "
                f"Yêu cầu định dạng YYYY-MM-DD HH:MM:SS hoặc unix timestamp."
            )

    def get_candles(self, timeframe='M1', start_time=None, end_time=None, limit=1000, before_time=None):
        """
        Lấy danh sách nến theo timeframe chuẩn hóa cho TradingView Lightweight Charts.
        Precedence lọc thời gian:
        1. before_time (+ start_time tùy chọn): lấy nến trước mốc before_time theo thứ tự tăng dần.
        2. start_time + end_time: lấy nến trong khoảng [start_time, end_time], tôn trọng limit.
        3. end_time (chỉ có end_time): lấy nến gần nhất trước/tại end_time theo thứ tự tăng dần.
        4. start_time (chỉ có start_time): lấy nến từ start_time theo thứ tự tăng dần.
        5. Mặc định: lấy nến mới nhất kết thúc tại hiện tại theo thứ tự tăng dần.
        """
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Khung thời gian không hợp lệ: '{timeframe}'. Hỗ trợ: {list(TIMEFRAME_MAP.keys())}")

        limit = max(1, int(limit))
        start_time = self._normalize_and_validate_time(start_time, "start_time")
        end_time = self._normalize_and_validate_time(end_time, "end_time")
        before_time = self._normalize_and_validate_time(before_time, "before_time")

        if start_time and end_time and start_time > end_time:
            raise ValueError(f"start_time ({start_time}) không được lớn hơn end_time ({end_time}).")
        if start_time and before_time and start_time >= before_time:
            raise ValueError(f"start_time ({start_time}) phải nhỏ hơn before_time ({before_time}).")

        conn = self.get_connection()
        try:
            # 1. Khung M1: query trực tiếp từ base_table
            if timeframe == 'M1':
                return self._query_direct(conn, self.base_table, start_time, end_time, limit, before_time)

            # 2. Khung H1: nếu có bảng XAUUSD_H1 thì query trực tiếp, ngược lại resample từ M1
            elif timeframe == 'H1':
                if self.has_h1_table:
                    return self._query_direct(conn, 'XAUUSD_H1', start_time, end_time, limit, before_time)
                return self._query_and_resample(conn, self.base_table, 'H1', start_time, end_time, limit, before_time)

            # 3. Khung D1: nếu có bảng XAUUSD_D1 thì query trực tiếp, ngược lại resample từ M1
            elif timeframe == 'D1':
                if self.has_d1_table:
                    return self._query_direct(conn, 'XAUUSD_D1', start_time, end_time, limit, before_time)
                return self._query_and_resample(conn, self.base_table, 'D1', start_time, end_time, limit, before_time)

            # 4. Khung H4: resample từ H1 (nếu có) hoặc từ M1
            elif timeframe == 'H4':
                source_table = 'XAUUSD_H1' if self.has_h1_table else self.base_table
                return self._query_and_resample(conn, source_table, 'H4', start_time, end_time, limit, before_time)

            # 5. Các khung M5, M15, M30: resample từ base_table (M1)
            else:
                return self._query_and_resample(conn, self.base_table, timeframe, start_time, end_time, limit, before_time)

        finally:
            conn.close()

    def _query_to_df(self, conn, table, start_time, end_time, limit, before_time):
        """
        Truy vấn dữ liệu từ SQLite ra pandas DataFrame tuân thủ precedence rõ ràng:
        - before_time: lấy nến < before_time (kết hợp start_time nếu có), sắp xếp DESC rồi đảo lại ASC.
        - start_time và end_time: lấy nến trong [start_time, end_time], tôn trọng limit, sắp xếp ASC.
        - chỉ end_time: lấy nến <= end_time, sắp xếp DESC rồi đảo lại ASC.
        - chỉ start_time: lấy nến >= start_time, sắp xếp ASC, tôn trọng limit.
        - mặc định: lấy nến mới nhất DESC rồi đảo lại ASC.
        """
        query = f"SELECT time, open, high, low, close, tick_volume FROM {table}"
        conditions = []
        params = []

        if before_time:
            if start_time:
                conditions.append("time >= ? AND time < ?")
                params.extend([start_time, before_time])
            else:
                conditions.append("time < ?")
                params.append(before_time)
            where_clause = " WHERE " + " AND ".join(conditions)
            sql = f"SELECT * FROM ({query}{where_clause} ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
            params.append(limit)

        elif start_time and end_time:
            conditions.append("time >= ? AND time <= ?")
            params.extend([start_time, end_time])
            where_clause = " WHERE " + " AND ".join(conditions)
            sql = f"{query}{where_clause} ORDER BY time ASC LIMIT ?"
            params.append(limit)

        elif end_time:
            conditions.append("time <= ?")
            params.append(end_time)
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

        df = pd.read_sql_query(sql, conn, params=params)
        return df

    def _query_direct(self, conn, table, start_time, end_time, limit, before_time):
        df = self._query_to_df(conn, table, start_time, end_time, limit, before_time)
        if df.empty:
            return []
        return self._df_to_candles(df)

    def _query_and_resample(self, conn, source_table, target_tf, start_time, end_time, limit, before_time):
        """
        Truy vấn nến cấp thấp (M1 hoặc H1) và resample sang target_tf.
        Tính toán số nến nguồn cần thiết và đảm bảo kết quả không vượt quá limit.
        """
        source_tf = 'H1' if source_table == 'XAUUSD_H1' else 'M1'
        source_min = TIMEFRAME_MINUTES[source_tf]
        target_min = TIMEFRAME_MINUTES[target_tf]
        ratio = max(1, target_min // source_min)

        # Buffer thêm 20% nến để bù cho các khoảng trống cuối tuần hoặc nến lẻ
        raw_limit = int(limit * ratio * 1.3) + (ratio * 2)

        df_raw = self._query_to_df(conn, source_table, start_time, end_time, raw_limit, before_time)
        if df_raw.empty:
            return []

        resampled = self.resample_dataframe(df_raw, target_tf)
        if resampled.empty:
            return []

        # Cắt đúng limit theo ngữ cảnh truy vấn
        if len(resampled) > limit:
            if before_time or end_time or (not start_time and not end_time):
                resampled = resampled.tail(limit)
            else:
                resampled = resampled.head(limit)

        return self._df_to_candles(resampled)

    def resample_dataframe(self, df, timeframe):
        """Resample nến sang timeframe cao hơn."""
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

    def _df_to_candles(self, df):
        """Chuyển đổi DataFrame sang list dict theo chuẩn TradingView Lightweight Charts."""
        if df is None or df.empty:
            return []

        df = df.copy()
        timestamps = (pd.to_datetime(df['time']).astype('datetime64[s]').astype('int64')).tolist()

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
        - history: các nến <= cut_time (sắp xếp tăng dần theo thời gian, tối đa history_limit)
        - future: các nến > cut_time (sắp xếp tăng dần, làm bộ đệm để replay từng nến, tối đa future_limit)
        Chính sách xử lý partial candle:
        - Nếu cut_time trùng mốc bắt đầu nến (ví dụ click vào nến 14:00): nến đó là nến history cuối cùng (tổng hợp đủ M1), future bắt đầu từ nến kế tiếp.
        - Nếu cut_time nằm giữa nến (ví dụ 14:07): loại bỏ nến partial khỏi history (history kết thúc ở nến trước), future bắt đầu từ nến đó đầy đủ dữ liệu.
        Đảm bảo history và future tách biệt, tăng dần, không trùng timestamp và không mất phút M1 nào.
        """
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Khung thời gian không hợp lệ: '{timeframe}'")

        cut_time_str = self._normalize_and_validate_time(cut_time, "cut_time")
        if not cut_time_str:
            cut_time_str = '2024-01-01 00:00:00'

        history_limit = max(1, int(history_limit))
        future_limit = max(1, int(future_limit))

        rule = TIMEFRAME_MAP[timeframe]
        target_min = TIMEFRAME_MINUTES[timeframe]
        dt_cut = pd.to_datetime(cut_time_str)
        bucket_start = dt_cut.floor(rule)
        is_exact_start = (dt_cut == bucket_start)

        if is_exact_start:
            # cut_time trùng mốc bắt đầu nến: nến tại bucket_start thuộc history và cần đầy đủ M1
            hist_split_dt = bucket_start + pd.Timedelta(minutes=target_min)
            fut_split_dt = hist_split_dt
        else:
            # cut_time nằm giữa nến: loại bỏ partial candle khỏi history
            hist_split_dt = bucket_start
            fut_split_dt = bucket_start

        hist_split_str = hist_split_dt.strftime('%Y-%m-%d %H:%M:%S')
        fut_split_str = fut_split_dt.strftime('%Y-%m-%d %H:%M:%S')

        conn = self.get_connection()
        try:
            # 1. Khung M1: query trực tiếp
            if timeframe == 'M1':
                sql_hist = f"SELECT * FROM (SELECT time, open, high, low, close, tick_volume FROM {self.base_table} WHERE time < ? ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
                df_hist = pd.read_sql_query(sql_hist, conn, params=[hist_split_str, history_limit])

                sql_fut = f"SELECT time, open, high, low, close, tick_volume FROM {self.base_table} WHERE time >= ? ORDER BY time ASC LIMIT ?"
                df_fut = pd.read_sql_query(sql_fut, conn, params=[fut_split_str, future_limit])

                return {
                    "history": self._df_to_candles(df_hist),
                    "future": self._df_to_candles(df_fut)
                }

            # 2. Khung H1 (nếu có bảng XAUUSD_H1)
            elif timeframe == 'H1' and self.has_h1_table:
                sql_hist = f"SELECT * FROM (SELECT time, open, high, low, close, tick_volume FROM XAUUSD_H1 WHERE time < ? ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
                df_hist = pd.read_sql_query(sql_hist, conn, params=[hist_split_str, history_limit])

                sql_fut = f"SELECT time, open, high, low, close, tick_volume FROM XAUUSD_H1 WHERE time >= ? ORDER BY time ASC LIMIT ?"
                df_fut = pd.read_sql_query(sql_fut, conn, params=[fut_split_str, future_limit])

                return {
                    "history": self._df_to_candles(df_hist),
                    "future": self._df_to_candles(df_fut)
                }

            # 3. Khung D1 (nếu có bảng XAUUSD_D1)
            elif timeframe == 'D1' and self.has_d1_table:
                sql_hist = f"SELECT * FROM (SELECT time, open, high, low, close, tick_volume FROM XAUUSD_D1 WHERE time < ? ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
                df_hist = pd.read_sql_query(sql_hist, conn, params=[hist_split_str, history_limit])

                sql_fut = f"SELECT time, open, high, low, close, tick_volume FROM XAUUSD_D1 WHERE time >= ? ORDER BY time ASC LIMIT ?"
                df_fut = pd.read_sql_query(sql_fut, conn, params=[fut_split_str, future_limit])

                return {
                    "history": self._df_to_candles(df_hist),
                    "future": self._df_to_candles(df_fut)
                }

            # 4. Các khung resample từ bảng nguồn (M1 hoặc H1)
            else:
                source_table = 'XAUUSD_H1' if (timeframe == 'H4' and self.has_h1_table) else self.base_table
                source_tf = 'H1' if source_table == 'XAUUSD_H1' else 'M1'
                source_min = TIMEFRAME_MINUTES[source_tf]
                ratio = max(1, target_min // source_min)

                raw_hist_limit = int(history_limit * ratio * 1.3) + (ratio * 2)
                sql_hist = f"SELECT * FROM (SELECT time, open, high, low, close, tick_volume FROM {source_table} WHERE time < ? ORDER BY time DESC LIMIT ?) ORDER BY time ASC"
                df_raw_hist = pd.read_sql_query(sql_hist, conn, params=[hist_split_str, raw_hist_limit])
                res_hist = self.resample_dataframe(df_raw_hist, timeframe)

                if not res_hist.empty:
                    res_hist = res_hist[res_hist['time'] < hist_split_str]
                    if len(res_hist) > history_limit:
                        res_hist = res_hist.tail(history_limit)

                raw_fut_limit = int(future_limit * ratio * 1.3) + (ratio * 2)
                sql_fut = f"SELECT time, open, high, low, close, tick_volume FROM {source_table} WHERE time >= ? ORDER BY time ASC LIMIT ?"
                df_raw_fut = pd.read_sql_query(sql_fut, conn, params=[fut_split_str, raw_fut_limit])
                res_fut = self.resample_dataframe(df_raw_fut, timeframe)

                if not res_fut.empty:
                    res_fut = res_fut[res_fut['time'] >= fut_split_str]
                    if len(res_fut) > future_limit:
                        res_fut = res_fut.head(future_limit)

                return {
                    "history": self._df_to_candles(res_hist),
                    "future": self._df_to_candles(res_fut)
                }

        finally:
            conn.close()
