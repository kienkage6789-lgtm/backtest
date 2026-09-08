import pandas as pd
import numpy as np
import math

def calculate_sma(series, period):
    return series.rolling(window=period).mean()

def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(series, fast)
    ema_slow = calculate_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def _parse_int(val, name):
    if val is None:
        raise ValueError(f"Tham số '{name}' không được để trống (None).")
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            raise ValueError(f"Tham số '{name}' không được là NaN hoặc Infinity.")
        i = int(f)
        if i != f:
            raise ValueError(f"Tham số '{name}' phải là số nguyên (nhận được: {val}).")
        return i
    except (ValueError, TypeError, OverflowError) as e:
        if isinstance(e, ValueError) and "Tham số" in str(e):
            raise
        raise ValueError(f"Tham số '{name}' không hợp lệ: {val}") from e

def _parse_float(val, name):
    if val is None:
        raise ValueError(f"Tham số '{name}' không được để trống (None).")
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            raise ValueError(f"Tham số '{name}' không được là NaN hoặc Infinity.")
        return f
    except (ValueError, TypeError, OverflowError) as e:
        if isinstance(e, ValueError) and "Tham số" in str(e):
            raise
        raise ValueError(f"Tham số '{name}' không hợp lệ: {val}") from e

class StrategyRegistry:
    SUPPORTED_STRATEGIES = {"sma_crossover", "rsi_reversal", "macd_crossover", "donchian_breakout"}

    @staticmethod
    def get_available_strategies():
        return [
            {
                "id": "sma_crossover",
                "name": "SMA Crossover (Giao cắt MA)",
                "description": "Mua khi SMA ngắn hạn cắt lên trên SMA dài hạn, Bán khi cắt xuống dưới.",
                "params": [
                    {"name": "fast_period", "label": "Fast SMA Period", "type": "int", "default": 20, "min": 2, "max": 200},
                    {"name": "slow_period", "label": "Slow SMA Period", "type": "int", "default": 50, "min": 5, "max": 500},
                ]
            },
            {
                "id": "rsi_reversal",
                "name": "RSI Reversal (Quá mua / Quá bán)",
                "description": "Mua khi RSI rời vùng Quá Bán (<30 cắt lên), Bán khi rời vùng Quá Mua (>70 cắt xuống).",
                "params": [
                    {"name": "period", "label": "RSI Period", "type": "int", "default": 14, "min": 2, "max": 100},
                    {"name": "oversold", "label": "Oversold Level", "type": "int", "default": 30, "min": 10, "max": 45},
                    {"name": "overbought", "label": "Overbought Level", "type": "int", "default": 70, "min": 55, "max": 90},
                ]
            },
            {
                "id": "macd_crossover",
                "name": "MACD Crossover",
                "description": "Vào lệnh theo tín hiệu giao cắt của đường MACD và Signal Line.",
                "params": [
                    {"name": "fast", "label": "Fast EMA", "type": "int", "default": 12, "min": 2, "max": 50},
                    {"name": "slow", "label": "Slow EMA", "type": "int", "default": 26, "min": 10, "max": 100},
                    {"name": "signal", "label": "Signal EMA", "type": "int", "default": 9, "min": 2, "max": 50},
                ]
            },
            {
                "id": "donchian_breakout",
                "name": "Donchian Channel Breakout (Kênh giá)",
                "description": "Mua khi giá vượt đỉnh N nến trước, Bán khi giá thủng đáy N nến trước.",
                "params": [
                    {"name": "lookback", "label": "Lookback Period", "type": "int", "default": 20, "min": 5, "max": 100},
                ]
            }
        ]

    @staticmethod
    def generate_signals(df, strategy_id, params):
        """
        Sinh cột 'signal':
        1 = Buy (Long entry)
        -1 = Sell (Short entry / Long exit)
        0 = Hold (No change)
        """
        if strategy_id not in StrategyRegistry.SUPPORTED_STRATEGIES:
            raise ValueError(
                f"Chiến lược không hợp lệ: '{strategy_id}'. "
                f"Hỗ trợ: {sorted(list(StrategyRegistry.SUPPORTED_STRATEGIES))}"
            )

        if params is None:
            params = {}

        df = df.copy()
        signals = np.zeros(len(df), dtype=int)
        close = df['close']

        if strategy_id == "sma_crossover":
            fast_p = _parse_int(params.get("fast_period", 20), "fast_period")
            slow_p = _parse_int(params.get("slow_period", 50), "slow_period")

            if fast_p <= 0 or slow_p <= 0:
                raise ValueError(f"Các chu kỳ SMA phải lớn hơn 0 (nhận được: fast={fast_p}, slow={slow_p}).")
            if fast_p >= slow_p:
                raise ValueError(f"fast_period ({fast_p}) phải nhỏ hơn slow_period ({slow_p}).")

            sma_fast = calculate_sma(close, fast_p)
            sma_slow = calculate_sma(close, slow_p)

            for i in range(1, len(df)):
                if sma_fast.iloc[i-1] <= sma_slow.iloc[i-1] and sma_fast.iloc[i] > sma_slow.iloc[i]:
                    signals[i] = 1 # Golden cross (Buy)
                elif sma_fast.iloc[i-1] >= sma_slow.iloc[i-1] and sma_fast.iloc[i] < sma_slow.iloc[i]:
                    signals[i] = -1 # Death cross (Sell)

        elif strategy_id == "rsi_reversal":
            period = _parse_int(params.get("period", 14), "period")
            oversold = _parse_float(params.get("oversold", 30), "oversold")
            overbought = _parse_float(params.get("overbought", 70), "overbought")

            if period <= 0:
                raise ValueError(f"RSI period phải lớn hơn 0 (nhận được: {period}).")
            if not (0 < oversold < overbought < 100):
                raise ValueError(
                    f"Mức RSI không hợp lệ: yêu cầu 0 < oversold < overbought < 100 "
                    f"(nhận được: oversold={oversold}, overbought={overbought})."
                )

            rsi = calculate_rsi(close, period)

            for i in range(1, len(df)):
                # Rời vùng quá bán -> Buy
                if rsi.iloc[i-1] < oversold and rsi.iloc[i] >= oversold:
                    signals[i] = 1
                # Rời vùng quá mua -> Sell
                elif rsi.iloc[i-1] > overbought and rsi.iloc[i] <= overbought:
                    signals[i] = -1

        elif strategy_id == "macd_crossover":
            fast = _parse_int(params.get("fast", 12), "fast")
            slow = _parse_int(params.get("slow", 26), "slow")
            sig = _parse_int(params.get("signal", 9), "signal")

            if fast <= 0 or slow <= 0 or sig <= 0:
                raise ValueError(f"Các tham số MACD phải lớn hơn 0 (nhận được: fast={fast}, slow={slow}, signal={sig}).")
            if fast >= slow:
                raise ValueError(f"Fast EMA ({fast}) phải nhỏ hơn Slow EMA ({slow}).")

            macd, signal_line, _ = calculate_macd(close, fast, slow, sig)

            for i in range(1, len(df)):
                if macd.iloc[i-1] <= signal_line.iloc[i-1] and macd.iloc[i] > signal_line.iloc[i]:
                    signals[i] = 1
                elif macd.iloc[i-1] >= signal_line.iloc[i-1] and macd.iloc[i] < signal_line.iloc[i]:
                    signals[i] = -1

        elif strategy_id == "donchian_breakout":
            lookback = _parse_int(params.get("lookback", 20), "lookback")

            if lookback <= 0:
                raise ValueError(f"Donchian lookback period phải lớn hơn 0 (nhận được: {lookback}).")

            upper = df['high'].shift(1).rolling(lookback).max()
            lower = df['low'].shift(1).rolling(lookback).min()

            for i in range(lookback + 1, len(df)):
                if close.iloc[i] > upper.iloc[i] and close.iloc[i-1] <= upper.iloc[i-1]:
                    signals[i] = 1
                elif close.iloc[i] < lower.iloc[i] and close.iloc[i-1] >= lower.iloc[i-1]:
                    signals[i] = -1

        df['signal'] = signals
        return df
