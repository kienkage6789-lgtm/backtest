@echo off
echo ===================================================
echo   Starting TradingView Backtest Platform (XAU/USD)
echo   Local Server: http://127.0.0.1:8000
echo ===================================================
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
pause
