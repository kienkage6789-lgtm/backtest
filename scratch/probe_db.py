import sqlite3
import pandas as pd

conn = sqlite3.connect("data/XAUUSD.db")
c = conn.cursor()
c.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
tables = c.fetchall()
print("Tables:", tables)

c.execute("PRAGMA table_info(XAUUSD_M1)")
print("Columns in XAUUSD_M1:", c.fetchall())

c.execute("SELECT COUNT(1) FROM XAUUSD_M1")
print("Total rows:", c.fetchone()[0])

c.execute("SELECT MIN(time), MAX(time) FROM XAUUSD_M1")
print("Timestamp range:", c.fetchone())

# Sample first 5 and last 5
c.execute("SELECT time, open, high, low, close, tick_volume, spread, real_volume FROM XAUUSD_M1 ORDER BY time ASC LIMIT 5")
print("First 5:", c.fetchall())

c.execute("SELECT time, open, high, low, close, tick_volume, spread, real_volume FROM XAUUSD_M1 ORDER BY time DESC LIMIT 5")
print("Last 5:", c.fetchall())

# Check 2022-01-01 to 2026-08-31
c.execute("SELECT COUNT(1), MIN(time), MAX(time) FROM XAUUSD_M1 WHERE time >= '2022-01-01' AND time <= '2026-08-31 23:59:59'")
print("2022 to 2026-08-31 count & range:", c.fetchone())

conn.close()
