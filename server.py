import os
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np

from engine.data_feed import DataFeed
from engine.strategies import StrategyRegistry
from engine.backtest_engine import BacktestEngine


def _sanitize_for_json(obj):
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    elif isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    elif isinstance(obj, (np.integer, int)):
        return int(obj)
    elif isinstance(obj, (np.floating, float)):
        return float(obj)
    return obj

app = FastAPI(title="Web Trading Backtest API", description="API phục vụ dữ liệu nến và backtest cho XAUUSD")

# Cho phép CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def no_cache_static_middleware(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

data_feed = DataFeed()

class BacktestRequest(BaseModel):
    timeframe: str = Field("H1", description="Khung thời gian backtest")
    limit: int = Field(2000, ge=10, le=200000, description="Số nến tối đa cần lấy")
    warmup_bars: int = Field(500, ge=0, le=20000, description="Số nến lookback đệm trước start_time để khởi tạo context")
    start_time: Optional[str] = Field(None, description="Thời gian bắt đầu (YYYY-MM-DD HH:MM:SS)")
    end_time: Optional[str] = Field(None, description="Thời gian kết thúc (YYYY-MM-DD HH:MM:SS)")
    start_bar_index: Optional[int] = Field(None, ge=0, description="Bar index bắt đầu")
    end_bar_index: Optional[int] = Field(None, ge=0, description="Bar index kết thúc")
    strategy_id: str = Field("sma_crossover", description="ID chiến lược")
    strategy_params: Dict[str, Any] = Field(default_factory=dict, description="Tham số chiến lược")
    initial_capital: float = Field(10000.0, ge=100.0, description="Vốn ban đầu ($)")
    lot_size: float = Field(0.1, ge=0.01, le=100.0, description="Khối lượng vào lệnh (lot)")
    stop_loss_points: float = Field(0.0, ge=0.0, description="Cắt lỗ (points, 100 points = $1)")
    take_profit_points: float = Field(0.0, ge=0.0, description="Chốt lời (points, 100 points = $1)")
    spread_points: float = Field(20.0, ge=0.0, description="Spread (points, 20 points = $0.20)")
    commission_per_lot: float = Field(5.0, ge=0.0, description="Phí hoa hồng mỗi lot ($)")
    allow_short: bool = Field(True, description="Cho phép lệnh Short")
    htf_events: Optional[List[Dict[str, Any]]] = Field(
        None,
        description="Danh sách HTF structure events (chỉ dùng cho Wave 1: smc_wave1, smc_s01, smc_s05, smc_s09)"
    )


class ReplayTimelineRequest(BaseModel):
    timeframe: str = Field("M15", description="Khung thời gian (M1, M5, M15, M30, H1, H4, D1)")
    symbol: str = Field("XAUUSD", description="Symbol giao dịch")
    strategy_id: str = Field("smc_wave1", description="Strategy mode: smc_wave1, smc_s01, smc_s05, smc_s09, smc_confluence")
    limit: int = Field(5000, ge=10, le=200000, description="Số nến tối đa cần lấy")
    warmup_bars: int = Field(500, ge=0, le=20000, description="Số nến lookback đệm trước start_time để khởi tạo context")
    start_time: Optional[str] = Field(None, description="Thời gian bắt đầu (YYYY-MM-DD HH:MM:SS)")
    end_time: Optional[str] = Field(None, description="Thời gian kết thúc (YYYY-MM-DD HH:MM:SS)")
    start_bar_index: int = Field(0, ge=0, description="Bar index bắt đầu serialize timeline")
    end_bar_index: Optional[int] = Field(None, ge=0, description="Bar index kết thúc serialize timeline")
    max_bars: int = Field(5000, ge=10, le=200000, description="Số nến tối đa đưa vào timeline payload")
    strategy_params: Dict[str, Any] = Field(default_factory=dict, description="Tham số chiến lược")
    initial_capital: float = Field(10000.0, ge=100.0, description="Vốn ban đầu ($)")
    lot_size: float = Field(0.1, ge=0.01, le=100.0, description="Khối lượng vào lệnh (lot)")
    spread_points: float = Field(20.0, ge=0.0, description="Spread (points)")
    commission_per_lot: float = Field(5.0, ge=0.0, description="Phí hoa hồng ($/lot)")
    allow_short: bool = Field(True, description="Cho phép Short")
    htf_events: Optional[List[Dict[str, Any]]] = Field(None, description="HTF structure events")


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
    limit: int = Query(1000, ge=10, le=200000, description="Số lượng nến cần lấy"),
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
    history_limit: int = Query(1000, ge=10, le=200000, description="Số nến lịch sử trước điểm cắt"),
    future_limit: int = Query(1000, ge=10, le=200000, description="Số nến tương lai sau điểm cắt để đệm tua")
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
    """Lấy danh sách các chiến lược có sẵn cùng tham số cấu hình (9 chiến lược: 5 legacy + 4 Wave 1)."""
    legacy = StrategyRegistry.get_available_strategies()
    wave1 = StrategyRegistry.get_wave1_strategies()
    return {
        "strategies": legacy + wave1
    }

@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    """Chạy backtest chiến lược trên tập dữ liệu nến đã chọn."""
    try:
        if req.start_time and req.end_time and req.start_time > req.end_time:
            raise HTTPException(status_code=400, detail="start_time không được lớn hơn end_time.")
        if req.start_bar_index is not None and req.end_bar_index is not None and req.end_bar_index < req.start_bar_index:
            raise HTTPException(status_code=400, detail="end_bar_index không được nhỏ hơn start_bar_index.")
        if req.warmup_bars > 20000:
            raise HTTPException(status_code=400, detail="warmup_bars không được vượt quá 20000.")

        if req.start_time and req.warmup_bars > 0:
            res_data = data_feed.get_candles_with_warmup(
                timeframe=req.timeframe,
                start_time=req.start_time,
                end_time=req.end_time,
                limit=req.limit,
                warmup_bars=req.warmup_bars,
            )
            candles = res_data["all_candles"]
            warmup_count = res_data["warmup_count"]
        else:
            candles = data_feed.get_candles(
                timeframe=req.timeframe,
                start_time=req.start_time,
                end_time=req.end_time,
                limit=req.limit
            )
            warmup_count = 0

        if not candles or len(candles) < 2:
            raise HTTPException(status_code=400, detail="Không đủ nến trong khoảng thời gian đã chọn để chạy backtest (tối thiểu 2 nến).")

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

        result = engine.run(
            df,
            req.strategy_id,
            req.strategy_params,
            timeframe=req.timeframe,
            htf_events=req.htf_events,
            warmup_bars=warmup_count,
        )

        analysis_candles = candles[warmup_count:] if warmup_count < len(candles) else candles
        warmup_start_str = candles[0]["datetime_str"] if candles else None
        analysis_start_str = analysis_candles[0]["datetime_str"] if analysis_candles else (candles[0]["datetime_str"] if candles else None)
        analysis_end_str = analysis_candles[-1]["datetime_str"] if analysis_candles else (candles[-1]["datetime_str"] if candles else None)

        resp = {
            "status": "success",
            "timeframe": req.timeframe,
            "warmup_start": warmup_start_str,
            "analysis_start": analysis_start_str,
            "analysis_end": analysis_end_str,
            "warmup_candles": warmup_count,
            "analysis_candles": len(analysis_candles),
            "display_candles": len(analysis_candles),
            "candles_analyzed": len(analysis_candles),
            "start_time": analysis_start_str,
            "end_time": analysis_end_str,
            "metrics": result["metrics"],
            "trades": result["trades"],
            "equity_curve": result["equity_curve"],
            "markers": result["markers"]
        }

        # Legacy optional fields
        if "smc_objects" in result:
            resp["smc_objects"] = result["smc_objects"]
        if "funnel_stats" in result:
            resp["funnel_stats"] = result["funnel_stats"]

        # Wave 1 V2 optional fields
        for v2_key in (
            "mode",
            "schema_version",
            "execution_events",
            "decisions",
            "pending_intents",
            "cooldown_snapshot",
            "run_metadata",
        ):
            if v2_key in result:
                resp[v2_key] = result[v2_key]

        return _sanitize_for_json(resp)

    except HTTPException:
        raise
    except (ValueError, TypeError) as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/replay/timeline")
def get_replay_timeline(req: ReplayTimelineRequest):
    """
    Sinh timeline replay từng nến phục vụ tua trực quan SMC, chẩn đoán lý do bị loại và kiểm tra anti-lookahead.
    """
    try:
        if req.start_time and req.end_time and req.start_time > req.end_time:
            raise HTTPException(status_code=400, detail="start_time không được lớn hơn end_time.")
        if req.end_bar_index is not None and req.end_bar_index < req.start_bar_index:
            raise HTTPException(status_code=400, detail="end_bar_index không được nhỏ hơn start_bar_index.")
        if req.warmup_bars > 20000:
            raise HTTPException(status_code=400, detail="warmup_bars không được vượt quá 20000.")

        if req.start_time and req.warmup_bars > 0:
            res_data = data_feed.get_candles_with_warmup(
                timeframe=req.timeframe,
                start_time=req.start_time,
                end_time=req.end_time,
                limit=req.limit,
                warmup_bars=req.warmup_bars,
            )
            candles = res_data["all_candles"]
            warmup_count = res_data["warmup_count"]
        else:
            candles = data_feed.get_candles(
                timeframe=req.timeframe,
                start_time=req.start_time,
                end_time=req.end_time,
                limit=req.limit,
            )
            warmup_count = 0

        if not candles or len(candles) < 2:
            raise HTTPException(status_code=400, detail="Không đủ nến để sinh timeline replay (tối thiểu 2 nến).")

        df = pd.DataFrame(candles)

        # Parse HTF events if provided
        from smc.engine.backtest_adapter import SMCBacktestCoordinator, parse_htf_event_payload
        from smc.engine.context import ContextBuilderConfig
        from smc.engine.execution import ExecutionConfig

        parsed_htf = []
        if req.htf_events:
            for i, ev in enumerate(req.htf_events):
                try:
                    parsed_htf.append(parse_htf_event_payload(ev))
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=f"HTF event index {i} không hợp lệ: {exc}")

        # Setup coordinator
        exec_cfg = ExecutionConfig(
            lot_size=req.lot_size,
            contract_size=100.0,
            spread_points=req.spread_points,
            commission_per_lot=req.commission_per_lot,
            allow_short=req.allow_short,
            min_rr_fallback=float(req.strategy_params.get("min_rr", 1.5)),
        )

        ctx_cfg = ContextBuilderConfig(
            symbol=req.symbol,
            timeframe=req.timeframe,
            structure_mode=req.strategy_params.get("s09_mode", "internal"),
        )

        coord_mode = req.strategy_id if req.strategy_id in ("smc_wave1", "smc_s01", "smc_s05", "smc_s09", "smc_st_fvg_mss", "smc_confluence") else "smc_wave1"
        cooldown_bars = int(req.strategy_params.get("cooldown_bars", 3))

        coordinator = SMCBacktestCoordinator(
            execution_config=exec_cfg,
            context_config=ctx_cfg,
            cooldown_bars=cooldown_bars,
            mode=coord_mode,
            htf_events=parsed_htf if parsed_htf else None,
            initial_capital=req.initial_capital,
        )

        # Strategy profiles are intentionally strict about the timeframe they
        # can evaluate.  Validate this at the API boundary so an unsupported
        # selection returns a useful 400 instead of a late 500 from replay.
        unsupported_ids = tuple(
            sid
            for sid in coordinator.strategy_registry.enabled_strategy_ids
            if req.timeframe not in coordinator.strategy_registry.get_profile(sid).timeframes
        )
        if unsupported_ids:
            supported = sorted({
                tf
                for sid in unsupported_ids
                for tf in coordinator.strategy_registry.get_profile(sid).timeframes
            })
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Khung thời gian '{req.timeframe}' không được hỗ trợ cho "
                    f"{', '.join(unsupported_ids)}. Khung hỗ trợ: {', '.join(supported)}."
                ),
            )

        # Calculate absolute bar indexes (relative analysis index + warmup_count)
        absolute_start = warmup_count + req.start_bar_index
        if req.end_bar_index is not None:
            absolute_end = warmup_count + req.end_bar_index + 1
        else:
            absolute_end = len(df)

        timeline_res = coordinator.build_replay_timeline(
            df=df,
            htf_events=parsed_htf if parsed_htf else None,
            start_idx=absolute_start,
            end_idx=absolute_end,
            max_bars=req.max_bars,
        )

        analysis_candles = candles[warmup_count:] if warmup_count < len(candles) else candles
        warmup_start_str = candles[0]["datetime_str"] if candles else None
        analysis_start_str = analysis_candles[0]["datetime_str"] if analysis_candles else (candles[0]["datetime_str"] if candles else None)
        analysis_end_str = analysis_candles[-1]["datetime_str"] if analysis_candles else (candles[-1]["datetime_str"] if candles else None)

        display_candles = len(timeline_res.get("timeline", []))

        response_payload = {
            "status": "success",
            "symbol": req.symbol,
            "timeframe": req.timeframe,
            "strategy": coord_mode,
            "warmup_start": warmup_start_str,
            "analysis_start": analysis_start_str,
            "analysis_end": analysis_end_str,
            "warmup_candles": warmup_count,
            "analysis_candles": len(analysis_candles),
            "display_candles": display_candles,
            "total_timeline_bars": len(df),
            "start_bar_index": req.start_bar_index,
            "end_bar_index": req.end_bar_index,
            "timeline": timeline_res["timeline"],
            "bookmarks": timeline_res["bookmarks"],
            "summary": timeline_res["summary"],
        }

        return _sanitize_for_json(response_payload)

    except HTTPException:
        raise
    except (ValueError, TypeError) as ve:
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
