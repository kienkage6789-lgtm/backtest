import sqlite3
import pandas as pd
import hashlib
from engine.data_feed import DataFeed

DB_PATH = "data/XAUUSD.db"
START_TIME = "2022-01-01 00:00:00"
END_TIME = "2026-08-31 23:59:59"

conn = sqlite3.connect(DB_PATH)
query = f"""
    SELECT time, open, high, low, close, tick_volume
    FROM XAUUSD_M1
    WHERE time >= '{START_TIME}' AND time <= '{END_TIME}'
    ORDER BY time ASC
"""
df_m1 = pd.read_sql_query(query, conn)
conn.close()

feed = DataFeed(DB_PATH)

# Resample M5
df_m5 = feed.resample_dataframe(df_m1, 'M5')
print(f"M5 count: {len(df_m5)}, min: {df_m5['time'].iloc[0]}, max: {df_m5['time'].iloc[-1]}")

# Resample M15
df_m15 = feed.resample_dataframe(df_m1, 'M15')
print(f"M15 count: {len(df_m15)}, min: {df_m15['time'].iloc[0]}, max: {df_m15['time'].iloc[-1]}")

# Resample H1
df_h1 = feed.resample_dataframe(df_m1, 'H1')
print(f"H1 count: {len(df_h1)}, min: {df_h1['time'].iloc[0]}, max: {df_h1['time'].iloc[-1]}")

# Verify M15 properties
inv_hl = (df_m15['high'] < df_m15['low']).sum()
inv_ho = (df_m15['high'] < df_m15['open']).sum()
inv_hc = (df_m15['high'] < df_m15['close']).sum()
inv_lo = (df_m15['low'] > df_m15['open']).sum()
inv_lc = (df_m15['low'] > df_m15['close']).sum()
print(f"M15 OHLC invalidities: HL={inv_hl}, HO={inv_ho}, HC={inv_hc}, LO={inv_lo}, LC={inv_lc}")

# Hashes for M5 and M15
def compute_hash(df):
    h = hashlib.sha256()
    for row in df[['time', 'open', 'high', 'low', 'close', 'tick_volume']].itertuples(index=False):
        line = f"{row.time},{row.open:.3f},{row.high:.3f},{row.low:.3f},{row.close:.3f},{int(row.tick_volume)}\n"
        h.update(line.encode('utf-8'))
    return h.hexdigest()

sha_m5 = compute_hash(df_m5)
sha_m15 = compute_hash(df_m15)
sha_h1 = compute_hash(df_h1)
print(f"M5 SHA256:  {sha_m5}")
print(f"M15 SHA256: {sha_m15}")
print(f"H1 SHA256:  {sha_h1}")
