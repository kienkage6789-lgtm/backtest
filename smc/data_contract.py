import pandas as pd
import numpy as np
from typing import Union, List, Dict, Any

def normalize_ohlcv(
    data: Union[pd.DataFrame, List[Dict[str, Any]]],
    repair_invalid_ohlc: bool = False
) -> pd.DataFrame:
    """
    Standardizes OHLCV input data into a clean, normalized pandas DataFrame.
    
    Args:
        data: Input DataFrame or list of candle dictionaries (e.g. from DataFeed).
        repair_invalid_ohlc: If False (default), raises ValueError when OHLC geometry is invalid.
                             If True, auto-adjusts high/low to envelop open/close.
        
    Returns:
        pd.DataFrame with:
            - DatetimeIndex in UTC named 'time'
            - Columns: ['open', 'high', 'low', 'close', 'volume', 'bar_index']
            - Sorted by time ascending with duplicate timestamps removed (keeping first).
            
    Raises:
        ValueError: If required columns are missing, OHLC contains NaN/Inf, or OHLC geometry is invalid.
    """
    if isinstance(data, list):
        if not data:
            df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume', 'bar_index'])
            df.index = pd.DatetimeIndex([], name='time', tz='UTC')
            return df
        df = pd.DataFrame(data)
    elif isinstance(data, pd.DataFrame):
        df = data.copy()
    else:
        raise ValueError(f"Unsupported data type for normalize_ohlcv: {type(data)}")

    if df.empty:
        df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume', 'bar_index'])
        df.index = pd.DatetimeIndex([], name='time', tz='UTC')
        return df

    # Normalize column names to lowercase
    df.columns = [str(c).lower().strip() for c in df.columns]

    # Handle volume column (rename tick_volume if volume missing)
    if 'volume' not in df.columns and 'tick_volume' in df.columns:
        df.rename(columns={'tick_volume': 'volume'}, inplace=True)

    # Handle time column / index
    if 'time' in df.columns:
        time_col = df['time']
        if pd.api.types.is_datetime64_any_dtype(time_col):
            datetime_index = pd.to_datetime(time_col, utc=True)
        else:
            numeric_time = pd.to_numeric(time_col, errors='coerce')
            if not numeric_time.isna().all():
                sample_val = numeric_time.dropna().iloc[0]
                unit = 's' if sample_val < 1e11 else 'ms'
                datetime_index = pd.to_datetime(numeric_time, unit=unit, utc=True)
            else:
                datetime_index = pd.to_datetime(time_col, utc=True)
        df.index = datetime_index
        df.drop(columns=['time'], inplace=True, errors='ignore')
    elif isinstance(df.index, pd.DatetimeIndex):
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')
    elif 'datetime_str' in df.columns:
        df.index = pd.to_datetime(df['datetime_str'], utc=True)
    else:
        raise ValueError("Input data must contain a 'time', 'datetime_str' column or a DatetimeIndex.")

    df.index.name = 'time'

    # Verify required columns
    required_cols = ['open', 'high', 'low', 'close']
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"Missing required column: '{col}' in input data.")

    if 'volume' not in df.columns:
        df['volume'] = 0.0

    # Coerce dtypes to float
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Reject NaN or Infinity in OHLC
    ohlc_vals = df[['open', 'high', 'low', 'close']].to_numpy()
    if np.isnan(ohlc_vals).any() or np.isinf(ohlc_vals).any():
        raise ValueError("Input OHLC data contains NaN or Infinite values.")

    # Sort ascending by timestamp and remove duplicate timestamps (keep first)
    df.sort_index(inplace=True)
    df = df[~df.index.duplicated(keep='first')]

    # Check OHLC geometric integrity
    max_oc = np.maximum(df['open'], df['close'])
    min_oc = np.minimum(df['open'], df['close'])
    invalid_high = df['high'] < max_oc
    invalid_low = df['low'] > min_oc

    if invalid_high.any() or invalid_low.any():
        bad_count = int((invalid_high | invalid_low).sum())
        if not repair_invalid_ohlc:
            raise ValueError(
                f"Found {bad_count} candle(s) with invalid OHLC geometry (high < max(open,close) or low > min(open,close)). "
                "Pass repair_invalid_ohlc=True to auto-adjust."
            )
        else:
            df['high'] = np.maximum(df['high'], max_oc)
            df['low'] = np.minimum(df['low'], min_oc)

    # Add contiguous bar_index
    df['bar_index'] = np.arange(len(df), dtype=np.int64)

    return df[['open', 'high', 'low', 'close', 'volume', 'bar_index']]
