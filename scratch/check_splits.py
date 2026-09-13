import pandas as pd
import sqlite3
from engine.data_feed import DataFeed

DB_PATH = "data/XAUUSD.db"
feed = DataFeed(DB_PATH)

conn = sqlite3.connect(DB_PATH)
df_m1 = pd.read_sql_query("SELECT time, open, high, low, close, tick_volume FROM XAUUSD_M1 WHERE time >= '2022-01-01' AND time <= '2026-08-31 23:59:59' ORDER BY time ASC", conn)
conn.close()

df_m15 = feed.resample_dataframe(df_m1, 'M15')
df_h1 = feed.resample_dataframe(df_m1, 'H1')

splits = {
    "in_sample": ("2022-01-01 00:00:00", "2024-09-30 23:59:59"),
    "validation": ("2024-10-01 00:00:00", "2025-08-31 23:59:59"),
    "out_of_sample": ("2025-09-01 00:00:00", "2026-08-31 23:59:59")
}

for name, (start, end) in splits.items():
    m1_c = ((df_m1['time'] >= start) & (df_m1['time'] <= end)).sum()
    m15_c = ((df_m15['time'] >= start) & (df_m15['time'] <= end)).sum()
    h1_c = ((df_h1['time'] >= start) & (df_h1['time'] <= end)).sum()
    print(f"Split {name}: M1={m1_c} ({m1_c/len(df_m1)*100:.1f}%), M15={m15_c} ({m15_c/len(df_m15)*100:.1f}%), H1={h1_c} ({h1_c/len(df_h1)*100:.1f}%)")
