import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import hashlib

DB_PATH = "data/XAUUSD.db"
START_TIME = "2022-01-01 00:00:00"
END_TIME = "2026-08-31 23:59:59"

conn = sqlite3.connect(DB_PATH)
query = f"""
    SELECT time, open, high, low, close, tick_volume, spread, real_volume
    FROM XAUUSD_M1
    WHERE time >= '{START_TIME}' AND time <= '{END_TIME}'
    ORDER BY time ASC
"""
df = pd.read_sql_query(query, conn)
conn.close()

print(f"Loaded {len(df)} rows.")
print(f"Min time: {df['time'].iloc[0]}, Max time: {df['time'].iloc[-1]}")

# 1. Duplicates
duplicates = df.duplicated(subset=['time']).sum()
print(f"Duplicate timestamps: {duplicates}")

# 2. Monotonicity
df['dt'] = pd.to_datetime(df['time'])
time_diffs = df['dt'].diff()
non_monotonic = (time_diffs.iloc[1:] <= pd.Timedelta(seconds=0)).sum()
print(f"Non-monotonic timestamps: {non_monotonic}")

# 3. OHLC sanity
invalid_hl = (df['high'] < df['low']).sum()
invalid_ho = (df['high'] < df['open']).sum()
invalid_hc = (df['high'] < df['close']).sum()
invalid_lo = (df['low'] > df['open']).sum()
invalid_lc = (df['low'] > df['close']).sum()
non_positive = ((df['open'] <= 0) | (df['high'] <= 0) | (df['low'] <= 0) | (df['close'] <= 0)).sum()
print(f"Invalid High < Low: {invalid_hl}")
print(f"Invalid High < Open: {invalid_ho}, High < Close: {invalid_hc}")
print(f"Invalid Low > Open: {invalid_lo}, Low > Close: {invalid_lc}")
print(f"Non-positive prices: {non_positive}")

# 4. Volume
negative_vol = (df['tick_volume'] < 0).sum()
zero_vol = (df['tick_volume'] == 0).sum()
mean_vol = df['tick_volume'].mean()
max_vol = df['tick_volume'].max()
p99_vol = df['tick_volume'].quantile(0.99)
print(f"Volume: neg={negative_vol}, zero={zero_vol}, mean={mean_vol:.1f}, p99={p99_vol:.1f}, max={max_vol}")

# 5. DB Spread column
spread_min = df['spread'].min()
spread_max = df['spread'].max()
spread_mean = df['spread'].mean()
spread_zeros = (df['spread'] == 0).sum()
print(f"Spread col: min={spread_min}, max={spread_max}, mean={spread_mean:.1f}, zeros={spread_zeros}")

# 6. Gap analysis
gaps = df[time_diffs > pd.Timedelta(minutes=1)].copy()
gaps['gap_duration'] = time_diffs[time_diffs > pd.Timedelta(minutes=1)]
gaps['prev_time'] = df['dt'].shift(1)[time_diffs > pd.Timedelta(minutes=1)]

print(f"\nTotal gaps (> 1 min): {len(gaps)}")
# Classify gaps
weekend_gaps = []
daily_break_gaps = []
holiday_gaps = []
intraday_gaps = []

for idx, row in gaps.iterrows():
    prev_t = row['prev_time']
    curr_t = row['dt']
    dur_hours = row['gap_duration'].total_seconds() / 3600.0
    
    # Weekend gap: spans Friday to Sunday/Monday
    if prev_t.weekday() == 4 and curr_t.weekday() in (6, 0): # Friday to Sunday/Monday
        weekend_gaps.append(row)
    elif dur_hours <= 2.5 and prev_t.hour in (20, 21, 22) and curr_t.hour in (21, 22, 23, 0):
        # Normal daily maintenance break (~1h)
        daily_break_gaps.append(row)
    elif dur_hours > 20 and dur_hours < 72 and prev_t.weekday() not in (4, 5):
        # Likely holiday (Christmas, New Year, etc.)
        holiday_gaps.append(row)
    else:
        intraday_gaps.append(row)

print(f"Weekend gaps: {len(weekend_gaps)}")
print(f"Daily rollover breaks: {len(daily_break_gaps)}")
print(f"Holiday gaps (>20h weekday): {len(holiday_gaps)}")
print(f"Intraday gaps: {len(intraday_gaps)}")

if intraday_gaps:
    df_intra = pd.DataFrame(intraday_gaps)
    print(f"Intraday gap durations (minutes): min={df_intra['gap_duration'].dt.total_seconds().min()/60}, max={df_intra['gap_duration'].dt.total_seconds().max()/60}, median={df_intra['gap_duration'].dt.total_seconds().median()/60}")
    print("Sample 5 intraday gaps:")
    for _, r in df_intra.head(5).iterrows():
        print(f"  From {r['prev_time']} to {r['dt']} ({r['gap_duration'].total_seconds()/60:.0f} mins)")

# Checksum
hasher = hashlib.sha256()
for row in df[['time', 'open', 'high', 'low', 'close', 'tick_volume']].itertuples(index=False):
    line = f"{row.time},{row.open:.3f},{row.high:.3f},{row.low:.3f},{row.close:.3f},{int(row.tick_volume)}\n"
    hasher.update(line.encode('utf-8'))
sha256_m1 = hasher.hexdigest()
print(f"\nCanonical M1 SHA256: {sha256_m1}")
