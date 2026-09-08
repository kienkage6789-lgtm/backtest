import os
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
import pandas as pd

from engine.data_feed import DataFeed
from engine.strategies import StrategyRegistry
from engine.backtest_engine import BacktestEngine

app = FastAPI(title="Web Trading Backtest API", description="API phục vụ dữ liệu nến và backtest cho XAUUSD")

# Cho phép CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

data_feed = DataFeed()

class BacktestRequest(BaseModel):
    timeframe: str = Field("H1", description="Khung thời gian backtest")
    limit: int = Field(2000, ge=10, le=20000, description="Số nến tối đa cần lấy")
    start_time: Optional[str] = Field(None, description="Thời gian bắt đầu (YYYY-MM-DD HH:MM:SS)")
    end_time: Optional[str] = Field(None, description="Thời gian kết thúc (YYYY-MM-DD HH:MM:SS)")
    strategy_id: str = Field("sma_crossover", description="ID chiến lược")
    strategy_params: Dict[str, Any] = Field(default_factory=dict, description="Tham số chiến lược")
    initial_capital: float = Field(10000.0, ge=100.0, description="Vốn ban đầu ($)")
    lot_size: float = Field(0.1, ge=0.01, le=100.0, description="Khối lượng vào lệnh (lot)")
    stop_loss_points: float = Field(0.0, ge=0.0, description="Cắt lỗ (points, 100 points = $1)")
    take_profit_points: float = Field(0.0, ge=0.0, description="Chốt lời (points, 100 points = $1)")
    spread_points: float = Field(20.0, ge=0.0, description="Spread (points, 20 points = $0.20)")
    commission_per_lot: float = Field(5.0, ge=0.0, description="Phí hoa hồng mỗi lot ($)")
    allow_short: bool = Field(True, description="Cho phép lệnh Short")

@app.get("/api/info")
def get_info():
    """Lấy thông tin tổng quan cặp tiền và dữ liệu lịch sử."""
    try:
        return data_feed.get_info()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/candles")
def get_candles(
    timeframe: str = Query("M15", description="Khung thời gian (M1, M5, M15, M30, H1, H4, D1)"),
    limit: int = Query(1000, ge=10, le=10000, description="Số lượng nến cần lấy"),
    start_time: str = Query(None, description="Thời gian bắt đầu (YYYY-MM-DD HH:MM:SS)"),
    end_time: str = Query(None, description="Thời gian kết thúc (YYYY-MM-DD HH:MM:SS)"),
    before_time: str = Query(None, description="Lấy nến trước thời điểm này (cho scroll backward)")
):
    """Lấy danh sách nến theo khung thời gian."""
    try:
        candles = data_feed.get_candles(
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            before_time=before_time
        )
        return {
            "symbol": "XAUUSD",
            "timeframe": timeframe,
            "count": len(candles),
            "candles": candles
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/replay/init")
def get_replay_init(
    timeframe: str = Query("M15", description="Khung thời gian"),
    cut_time: str = Query(..., description="Thời điểm cắt nến để bắt đầu replay (YYYY-MM-DD HH:MM:SS) hoặc unix timestamp"),
    history_limit: int = Query(1000, ge=10, le=5000, description="Số nến lịch sử trước điểm cắt"),
    future_limit: int = Query(1000, ge=10, le=5000, description="Số nến tương lai sau điểm cắt để đệm tua")
):
    """Khởi tạo phiên Replay: lấy nến lịch sử (đã diễn ra) và nến tương lai (chuẩn bị tua)."""
    try:
        data = data_feed.get_replay_candles(
            timeframe=timeframe,
            cut_time=cut_time,
            history_limit=history_limit,
            future_limit=future_limit
        )
        return {
            "status": "success",
            "symbol": "XAUUSD",
            "timeframe": timeframe,
            "cut_time": cut_time,
            "history_count": len(data["history"]),
            "future_count": len(data["future"]),
            "history": data["history"],
            "future": data["future"]
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/strategies")
def get_strategies():
    """Lấy danh sách các chiến lược có sẵn cùng tham số cấu hình."""
    return {
        "strategies": StrategyRegistry.get_available_strategies()
    }

@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    """Chạy backtest chiến lược trên tập dữ liệu nến đã chọn."""
    try:
        candles = data_feed.get_candles(
            timeframe=req.timeframe,
            start_time=req.start_time,
            end_time=req.end_time,
            limit=req.limit
        )

        if not candles or len(candles) < 2:
            raise HTTPException(status_code=400, detail="Không đủ nến trong khoảng thời gian đã chọn để chạy backtest.")

        df = pd.DataFrame(candles)

        engine = BacktestEngine(
            initial_capital=req.initial_capital,
            lot_size=req.lot_size,
            contract_size=100.0,
            stop_loss_points=req.stop_loss_points,
            take_profit_points=req.take_profit_points,
            spread_points=req.spread_points,
            commission_per_lot=req.commission_per_lot,
            allow_short=req.allow_short
        )

        result = engine.run(df, req.strategy_id, req.strategy_params)
        return {
            "status": "success",
            "timeframe": req.timeframe,
            "candles_analyzed": len(candles),
            "start_time": candles[0]["datetime_str"],
            "end_time": candles[-1]["datetime_str"],
            "metrics": result["metrics"],
            "trades": result["trades"],
            "equity_curve": result["equity_curve"],
            "markers": result["markers"]
        }

    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# Static files
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
if not os.path.exists(PUBLIC_DIR):
    os.makedirs(PUBLIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=PUBLIC_DIR), name="static")

@app.get("/")
def read_root():
    index_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Web Trading Backtest API is running. Public UI will be mounted here."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
