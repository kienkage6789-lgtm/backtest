"""
engine/backtest_engine.py
-------------------------
Legacy backtest engine facade coordinating signal generation and delegating
order matching and position lifecycle to the shared ExecutionKernel.

Since T53.9.4, run() acts as a dispatcher:
- LEGACY_STRATEGIES  → original signal-based path (unchanged behaviour)
- WAVE1_STRATEGIES   → SMCBacktestCoordinator path (T53.9.3)
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from engine.execution_kernel import (
    ExecutionBar,
    ExecutionKernel,
    OpenInstruction,
)
from engine.strategies import StrategyRegistry

REQUIRED_BACKTEST_COLS = {'time', 'open', 'high', 'low', 'close'}

# Wave 1 only supports these timeframes (fail-closed)
WAVE1_ALLOWED_TIMEFRAMES: frozenset[str] = frozenset({"M1", "M5", "M15"})


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 10000.0,
        lot_size: float = 0.1,
        contract_size: float = 100.0,
        stop_loss_points: float = 0.0,   # Điểm giá (1.00 USD = 100 points)
        take_profit_points: float = 0.0,
        spread_points: float = 20.0,     # 20 points = 0.20 USD
        commission_per_lot: float = 5.0, # 5 USD / lot (mỗi chiều 2.5 USD hoặc 5 USD / round-turn)
        allow_short: bool = True
    ):
        self.initial_capital = float(initial_capital)
        self.lot_size = float(lot_size)
        self.contract_size = float(contract_size)
        self.stop_loss_val = float(stop_loss_points) / 100.0   # Chuyển points thành USD
        self.take_profit_val = float(take_profit_points) / 100.0
        self.spread_val = float(spread_points) / 100.0
        # Commission cho 1 chiều (entry hoặc exit)
        self.commission_per_side = float(commission_per_lot) * self.lot_size
        self.allow_short = bool(allow_short)

        self.kernel = ExecutionKernel(
            initial_capital=self.initial_capital,
            lot_size=self.lot_size,
            contract_size=self.contract_size,
            spread_val=self.spread_val,
            commission_per_side=self.commission_per_side,
            validation_mode="legacy",
        )

    # ------------------------------------------------------------------
    # Public dispatcher
    # ------------------------------------------------------------------

    def run(
        self,
        df: pd.DataFrame,
        strategy_id: str,
        strategy_params: dict,
        timeframe: str = "H1",
        htf_events: Optional[Sequence[Any]] = None,
        warmup_bars: int = 0,
    ) -> dict[str, Any]:
        """
        Dispatcher thực thi backtest:
        - strategy_id in LEGACY_STRATEGIES → luồng legacy (không thay đổi)
        - strategy_id in WAVE1_STRATEGIES  → SMCBacktestCoordinator (T53.9.3)
        - strategy_id không hợp lệ        → ValueError
        """
        if strategy_id in StrategyRegistry.WAVE1_STRATEGIES:
            return self._run_wave1(df, strategy_id, strategy_params, timeframe, htf_events, warmup_bars=warmup_bars)
        else:
            # Legacy path — generate_signals validates strategy_id internally.
            # This also supports monkey-patched test fixtures (e.g. "dummy" with mocked signals).
            return self._run_legacy(df, strategy_id, strategy_params, warmup_bars=warmup_bars)

    # ------------------------------------------------------------------
    # Private: legacy path
    # ------------------------------------------------------------------

    def _run_legacy(
        self,
        df: pd.DataFrame,
        strategy_id: str,
        strategy_params: dict,
        warmup_bars: int = 0,
    ) -> dict[str, Any]:
        """
        Thực thi backtest mô phỏng khớp lệnh (legacy path):
        - df bắt buộc có các cột: 'time', 'open', 'high', 'low', 'close'
        - Loại bỏ Lookahead Bias: Tín hiệu sinh tại nến N chỉ khớp tại nến N+1 theo giá Open.
        - Spread và Commission đối xứng cho cả Long và Short.
        - Forced close cuối kỳ cập nhật đầy đủ balance, equity curve, MDD, markers, trade record.
        - Khớp lệnh và quản lý vòng đời vị thế được ủy quyền cho ExecutionKernel dùng chung.
        """
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            raise ValueError("Dữ liệu nến truyền vào bị rỗng.")

        missing_cols = REQUIRED_BACKTEST_COLS - set(df.columns)
        if missing_cols:
            raise ValueError(f"Dữ liệu nến thiếu các cột bắt buộc: {missing_cols}")

        if len(df) < 2:
            raise ValueError("Không đủ dữ liệu nến (tối thiểu 2 nến) để thực hiện backtest.")

        # Sinh tín hiệu từ chiến lược (có kiểm tra strategy_id và validate params)
        df_signals = StrategyRegistry.generate_signals(df, strategy_id, strategy_params)

        # Khởi tạo kernel mới độc lập cho mỗi lần chạy backtest
        kernel = ExecutionKernel(
            initial_capital=self.initial_capital,
            lot_size=self.lot_size,
            contract_size=self.contract_size,
            spread_val=self.spread_val,
            commission_per_side=self.commission_per_side,
            validation_mode="legacy",
        )
        self.kernel = kernel

        equity_curve = []

        timestamps = (pd.to_datetime(df_signals['time']).astype('datetime64[s]').astype('int64')).tolist()
        opens = pd.to_numeric(df_signals['open'], errors='coerce').fillna(0).tolist()
        highs = pd.to_numeric(df_signals['high'], errors='coerce').fillna(0).tolist()
        lows = pd.to_numeric(df_signals['low'], errors='coerce').fillna(0).tolist()
        closes = pd.to_numeric(df_signals['close'], errors='coerce').fillna(0).tolist()
        signals = df_signals['signal'].tolist()
        time_strs = df_signals['time'].tolist()

        peak_equity = self.initial_capital
        max_drawdown = 0.0
        max_drawdown_pct = 0.0

        n_bars = len(df_signals)

        has_planned = (
            'planned_stop_loss' in df_signals.columns and
            'planned_take_profit' in df_signals.columns
        )
        planned_sl_col = df_signals['planned_stop_loss'].tolist() if has_planned else [None] * n_bars
        planned_tp_col = df_signals['planned_take_profit'].tolist() if has_planned else [None] * n_bars
        planned_entry_col = df_signals['planned_entry_price'].tolist() if 'planned_entry_price' in df_signals.columns else [None] * n_bars
        planned_rr_col = df_signals['planned_rr'].tolist() if 'planned_rr' in df_signals.columns else [None] * n_bars

        planned_levels_used = 0
        fallback_levels_used = 0
        rejected_invalid_geometry = 0

        for i in range(n_bars):
            t = timestamps[i]
            t_str = time_strs[i]
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]

            bar = ExecutionBar(
                bar_index=i,
                timestamp=t,
                time_value=t_str,
                open=o,
                high=h,
                low=l,
                close=c,
            )

            # Tín hiệu được sinh từ cây nến trước (N-1) khớp ở cây nến này (N) tại giá Open
            prev_sig = signals[i - 1] if i > 0 else 0

            # -----------------------------------------------------------------
            # 1. Khớp lệnh tại Open của nến i dựa trên tín hiệu ở nến i-1 (No Lookahead)
            # -----------------------------------------------------------------
            instruction: OpenInstruction | None = None
            if i > 0 and prev_sig != 0:
                raw_sl = planned_sl_col[i - 1]
                raw_tp = planned_tp_col[i - 1]
                raw_entry = planned_entry_col[i - 1]
                raw_rr = planned_rr_col[i - 1]

                has_valid_planned = (
                    raw_sl is not None and not (isinstance(raw_sl, float) and math.isnan(raw_sl)) and
                    raw_tp is not None and not (isinstance(raw_tp, float) and math.isnan(raw_tp))
                )

                if prev_sig == 1:
                    # Mua Long: khớp tại Open + Spread (Ask)
                    entry_p = o + self.spread_val
                    if has_valid_planned:
                        # Kiểm tra geometry tại actual fill: planned_sl < actual_entry < planned_tp
                        if float(raw_sl) < entry_p < float(raw_tp):
                            sl_p = float(raw_sl)
                            tp_p = float(raw_tp)
                            sl_tp_source = "strategy_planned"
                            planned_levels_used += 1
                            instruction = OpenInstruction(
                                action="OPEN_OR_REVERSE",
                                direction="BUY",
                                entry_price=entry_p,
                                sl_price=sl_p,
                                tp_price=tp_p,
                                source="legacy",
                                metadata={
                                    "planned_entry_price": float(raw_entry) if raw_entry is not None and not math.isnan(raw_entry) else None,
                                    "planned_stop_loss": sl_p,
                                    "planned_take_profit": tp_p,
                                    "planned_rr": float(raw_rr) if raw_rr is not None and not math.isnan(raw_rr) else None,
                                    "actual_entry_price": entry_p,
                                    "sl_tp_source": sl_tp_source,
                                }
                            )
                        else:
                            # Geometry invalid tại actual entry -> Fail closed, bỏ qua lệnh
                            rejected_invalid_geometry += 1
                    else:
                        sl_p = entry_p - self.stop_loss_val if self.stop_loss_val > 0 else None
                        tp_p = entry_p + self.take_profit_val if self.take_profit_val > 0 else None
                        sl_tp_source = "engine_fallback"
                        fallback_levels_used += 1
                        meta = {
                            "planned_entry_price": None,
                            "planned_stop_loss": sl_p,
                            "planned_take_profit": tp_p,
                            "planned_rr": None,
                            "actual_entry_price": entry_p,
                            "sl_tp_source": sl_tp_source,
                        } if has_planned else {}
                        instruction = OpenInstruction(
                            action="OPEN_OR_REVERSE",
                            direction="BUY",
                            entry_price=entry_p,
                            sl_price=sl_p,
                            tp_price=tp_p,
                            source="legacy",
                            metadata=meta,
                        )
                elif prev_sig == -1:
                    if self.allow_short:
                        # Bán Short: khớp tại Open (Bid)
                        entry_p = o
                        if has_valid_planned:
                            # Kiểm tra geometry tại actual fill: planned_tp < actual_entry < planned_sl
                            if float(raw_tp) < entry_p < float(raw_sl):
                                sl_p = float(raw_sl)
                                tp_p = float(raw_tp)
                                sl_tp_source = "strategy_planned"
                                planned_levels_used += 1
                                instruction = OpenInstruction(
                                    action="OPEN_OR_REVERSE",
                                    direction="SELL",
                                    entry_price=entry_p,
                                    sl_price=sl_p,
                                    tp_price=tp_p,
                                    source="legacy",
                                    metadata={
                                        "planned_entry_price": float(raw_entry) if raw_entry is not None and not math.isnan(raw_entry) else None,
                                        "planned_stop_loss": sl_p,
                                        "planned_take_profit": tp_p,
                                        "planned_rr": float(raw_rr) if raw_rr is not None and not math.isnan(raw_rr) else None,
                                        "actual_entry_price": entry_p,
                                        "sl_tp_source": sl_tp_source,
                                    }
                                )
                            else:
                                # Geometry invalid tại actual entry -> Fail closed, bỏ qua lệnh
                                rejected_invalid_geometry += 1
                        else:
                            sl_p = entry_p + self.stop_loss_val if self.stop_loss_val > 0 else None
                            tp_p = entry_p - self.take_profit_val if self.take_profit_val > 0 else None
                            sl_tp_source = "engine_fallback"
                            fallback_levels_used += 1
                            meta = {
                                "planned_entry_price": None,
                                "planned_stop_loss": sl_p,
                                "planned_take_profit": tp_p,
                                "planned_rr": None,
                                "actual_entry_price": entry_p,
                                "sl_tp_source": sl_tp_source,
                            } if has_planned else {}
                            instruction = OpenInstruction(
                                action="OPEN_OR_REVERSE",
                                direction="SELL",
                                entry_price=entry_p,
                                sl_price=sl_p,
                                tp_price=tp_p,
                                source="legacy",
                                metadata=meta,
                            )
                    else:
                        # Policy legacy khi allow_short=False:
                        # SELL đóng LONG hiện tại (nếu có), không mở SHORT mới
                        if kernel.position is not None and kernel.position.direction == "BUY":
                            instruction = OpenInstruction(
                                action="CLOSE_ONLY",
                                direction="SELL",
                                source="legacy",
                            )

            if warmup_bars > 0 and i < warmup_bars:
                instruction = None

            if instruction is not None:
                kernel.process_open(bar, instruction)

            # -----------------------------------------------------------------
            # 2. Kiểm tra Stop Loss / Take Profit trong thân nến i
            # -----------------------------------------------------------------
            kernel.process_intrabar(bar)

            # Reset execution state at boundary between warm-up and analysis range
            if warmup_bars > 0 and i == warmup_bars - 1:
                kernel = ExecutionKernel(
                    initial_capital=self.initial_capital,
                    lot_size=self.lot_size,
                    contract_size=self.contract_size,
                    spread_val=self.spread_val,
                    commission_per_side=self.commission_per_side,
                    validation_mode="legacy",
                )
                self.kernel = kernel
                equity_curve = []
                peak_equity = self.initial_capital
                max_drawdown = 0.0
                max_drawdown_pct = 0.0
                continue

            # -----------------------------------------------------------------
            # 3. Tính toán Floating PnL & Equity hiện tại
            # -----------------------------------------------------------------
            current_equity = kernel.mark_to_market(c)

            if current_equity > peak_equity:
                peak_equity = current_equity

            dd = peak_equity - current_equity
            dd_pct = (dd / peak_equity * 100) if peak_equity > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct

            # Lưu điểm equity định kỳ hoặc khi có vị thế
            if i % 5 == 0 or i == n_bars - 1 or kernel.position is not None:
                equity_curve.append({
                    "time": t,
                    "equity": round(current_equity, 2),
                    "balance": round(kernel.balance, 2)
                })

        # ---------------------------------------------------------------------
        # 4. Forced Close vị thế còn mở ở nến cuối kỳ backtest
        # ---------------------------------------------------------------------
        if kernel.position is not None:
            last_bar = ExecutionBar(
                bar_index=n_bars - 1,
                timestamp=timestamps[-1],
                time_value=time_strs[-1],
                open=opens[-1],
                high=highs[-1],
                low=lows[-1],
                close=closes[-1],
            )
            kernel.force_close(last_bar)

            # Cập nhật Peak, Drawdown và Equity Curve sau khi đóng vị thế cuối
            final_equity = kernel.balance
            if final_equity > peak_equity:
                peak_equity = final_equity

            final_dd = peak_equity - final_equity
            final_dd_pct = (final_dd / peak_equity * 100) if peak_equity > 0 else 0.0
            if final_dd > max_drawdown:
                max_drawdown = final_dd
            if final_dd_pct > max_drawdown_pct:
                max_drawdown_pct = final_dd_pct

            # Đảm bảo điểm cuối cùng của equity curve phản ánh đúng balance đã chốt
            equity_curve.append({
                "time": timestamps[-1],
                "equity": round(final_equity, 2),
                "balance": round(kernel.balance, 2)
            })

        # ---------------------------------------------------------------------
        # 5. Thống kê hiệu suất tổng kết
        # ---------------------------------------------------------------------
        trades = kernel.trades
        chart_markers = kernel.markers
        balance = kernel.balance

        total_trades = len(trades)
        winning_trades = [tr for tr in trades if tr['pnl'] > 0]
        losing_trades = [tr for tr in trades if tr['pnl'] < 0]

        gross_profit = sum(tr['pnl'] for tr in winning_trades)
        gross_loss = abs(sum(tr['pnl'] for tr in losing_trades))
        net_profit = balance - self.initial_capital
        return_pct = (net_profit / self.initial_capital) * 100 if self.initial_capital != 0 else 0.0
        if math.isnan(return_pct) or math.isinf(return_pct):
            raise ValueError(f"'return_pct' must be finite, got {return_pct}")

        win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        metrics = {
            "initial_capital": self.initial_capital,
            "final_balance": round(balance, 2),
            "net_profit": round(net_profit, 2),
            "return_pct": round(return_pct, 2),
            "total_trades": total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(max_drawdown, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
        }

        result: dict[str, Any] = {
            "metrics": metrics,
            "trades": trades,
            "equity_curve": equity_curve,
            "markers": chart_markers
        }

        if hasattr(df_signals, 'attrs'):
            if 'smc_chart_objects' in df_signals.attrs:
                result['smc_objects'] = df_signals.attrs['smc_chart_objects']
            if 'smc_funnel_stats' in df_signals.attrs:
                result['funnel_stats'] = df_signals.attrs['smc_funnel_stats']

        if has_planned or strategy_id == "smc_confluence":
            result['legacy_telemetry'] = {
                "planned_levels_used": planned_levels_used,
                "fallback_levels_used": fallback_levels_used,
                "rejected_invalid_geometry": rejected_invalid_geometry,
            }

        return result

    # ------------------------------------------------------------------
    # Private: Wave 1 path (T53.9.3 coordinator)
    # ------------------------------------------------------------------

    def _run_wave1(
        self,
        df: pd.DataFrame,
        strategy_id: str,
        strategy_params: dict,
        timeframe: str,
        htf_events: Optional[Sequence[Any]],
        warmup_bars: int = 0,
    ) -> dict[str, Any]:
        """
        Thực thi Wave 1 backtest qua SMCBacktestCoordinator.

        Timeframe validation is fail-closed: only M1, M5, M15 accepted.
        htf_events can be a list of StructureEvent or dicts (JSON payload from API).
        Returns standard fields + optional V2 fields.
        """
        from smc.engine.backtest_adapter import (
            SMCBacktestCoordinator,
            parse_htf_event_payload,
            CoordinatorMode,
        )
        from smc.engine.execution import ExecutionConfig

        # --- Timeframe validation (fail-closed) ---
        if timeframe not in WAVE1_ALLOWED_TIMEFRAMES:
            raise ValueError(
                f"Timeframe '{timeframe}' không được hỗ trợ cho chiến lược Wave 1 '{strategy_id}'. "
                f"Chỉ hỗ trợ: {sorted(WAVE1_ALLOWED_TIMEFRAMES)}"
            )

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            raise ValueError("Dữ liệu nến truyền vào bị rỗng.")

        missing_cols = REQUIRED_BACKTEST_COLS - set(df.columns)
        if missing_cols:
            raise ValueError(f"Dữ liệu nến thiếu các cột bắt buộc: {missing_cols}")

        if len(df) < 2:
            raise ValueError("Không đủ dữ liệu nến (tối thiểu 2 nến) để thực hiện backtest.")

        # --- Parse strategy params ---
        if strategy_params is None:
            strategy_params = {}
        min_rr = float(strategy_params.get("min_rr", 1.5))
        cooldown_bars = int(strategy_params.get("cooldown_bars", 3))
        # Balanced-frequency S01 runtime preset.  The lower-level strategy and
        # gate defaults remain conservative; the backtest product opts into
        # the tested, synchronized 24-bar window.
        s01_stale_sweep_max_bars = int(strategy_params.get("s01_stale_sweep_max_bars", 24))
        if math.isnan(min_rr) or math.isinf(min_rr) or min_rr <= 0:
            raise ValueError(f"min_rr phải là số hữu hạn dương, nhận được: {min_rr}")
        if cooldown_bars < 0:
            raise ValueError(f"cooldown_bars phải >= 0, nhận được: {cooldown_bars}")
        if s01_stale_sweep_max_bars <= 0:
            raise ValueError(
                f"s01_stale_sweep_max_bars phải > 0, nhận được: {s01_stale_sweep_max_bars}"
            )

        # --- Parse & validate HTF events ---
        from smc.models import StructureEvent as _StructureEvent
        parsed_htf: list[_StructureEvent] = []
        if htf_events:
            raw_event_list = (
                htf_events.get("events", [])
                if isinstance(htf_events, dict) and "events" in htf_events
                else htf_events
            )
            for i, ev in enumerate(raw_event_list):
                if isinstance(ev, _StructureEvent):
                    parsed_htf.append(ev)
                elif isinstance(ev, dict):
                    try:
                        parsed_htf.append(parse_htf_event_payload(ev))
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ValueError(
                            f"HTF event tại index {i} không hợp lệ: {exc}"
                        ) from exc
                else:
                    raise ValueError(
                        f"HTF event tại index {i} phải là StructureEvent hoặc dict, "
                        f"nhận được: {type(ev).__name__}"
                    )

        # --- Build ExecutionConfig from engine params ---
        # BacktestEngine stores: spread_val = spread_points / 100 USD, commission_per_side = commission_per_lot * lot_size
        # ExecutionConfig expects spread_points (units) and commission_per_lot (USD/lot)
        spread_pts = self.spread_val * 100.0   # convert USD back to points
        comm_per_lot = (
            self.commission_per_side / self.lot_size if self.lot_size > 0 else 5.0
        )
        exec_cfg = ExecutionConfig(
            lot_size=self.lot_size,
            contract_size=self.contract_size,
            spread_points=spread_pts,
            commission_per_lot=comm_per_lot,
            allow_short=self.allow_short,
            min_rr_fallback=min_rr,
        )

        # --- Build ContextBuilderConfig from timeframe ---
        from smc.engine.context import ContextBuilderConfig
        ctx_cfg = ContextBuilderConfig(
            timeframe=timeframe,
            structure_mode=strategy_params.get("s09_mode", "internal"),
        )

        # Optional S09 diagnostic overrides.  They are deliberately opt-in so
        # the normal Wave 1 defaults remain unchanged, while research runs can
        # identify which S09 gate is suppressing candidates.
        strategies = None
        strategy_overrides = {
            key: strategy_params[key]
            for key in (
                "s01_mode",
                "s01_sweep_to_mss_max_bars",
                "s01_stale_sweep_max_bars",
                "s01_fvg_to_mss_max_bars",
                "s01_entry_expiry_bars",
                "s01_min_rr",
                "s01_require_displacement",
                "s01_allow_mss_without_sweep",
                "s05_mode",
                "s05_max_ob_age_bars",
                "s05_min_rr",
                "s05_require_displacement",
                "s05_min_ob_quality",
                "s05_require_ltf_confirmation",
                "s05_ltf_confirmation_event_types",
                "s05_ltf_entry_zone",
                "s05_ltf_zone_expiry_bars",
                "s05_sl_anchor",
                "s09_mode",
                "s09_fvg_to_mss_max_bars",
                "s09_min_rr",
                "s09_require_displacement",
                "s09_use_time_filter",
            )
            if key in strategy_params
        }
        if strategy_overrides:
            from smc.engine.strategies import (
                S01Config,
                S01ICT2022Strategy,
                S05Config,
                S05BOSOBRetestStrategy,
                S09Config,
                S09ICTSilverBulletStrategy,
                SMCSupertrendFVGMSSConfig,
                SMCSupertrendFVGMSSStrategy,
            )

            s01_cfg = S01Config(
                mode=strategy_overrides.get("s01_mode", "internal"),
                sweep_to_mss_max_bars=strategy_overrides.get("s01_sweep_to_mss_max_bars", 24),
                fvg_to_mss_max_bars=strategy_overrides.get("s01_fvg_to_mss_max_bars", 10),
                entry_expiry_bars=strategy_overrides.get("s01_entry_expiry_bars", 15),
                min_rr=strategy_overrides.get("s01_min_rr", 1.5),
                fallback_rr=max(2.0, float(strategy_overrides.get("s01_min_rr", 1.5))),
                require_displacement=strategy_overrides.get("s01_require_displacement", False),
                allow_mss_without_sweep=strategy_overrides.get("s01_allow_mss_without_sweep", False),
            )
            s05_cfg = S05Config(
                mode=strategy_overrides.get("s05_mode", "internal"),
                max_ob_age_bars=strategy_overrides.get("s05_max_ob_age_bars", 25),
                min_rr=strategy_overrides.get("s05_min_rr", 1.5),
                fallback_rr=max(2.0, float(strategy_overrides.get("s05_min_rr", 1.5))),
                require_displacement=strategy_overrides.get("s05_require_displacement", True),
                min_ob_quality=strategy_overrides.get("s05_min_ob_quality", "base"),
                require_ltf_confirmation=strategy_overrides.get("s05_require_ltf_confirmation", False),
                ltf_confirmation_event_types=strategy_overrides.get("s05_ltf_confirmation_event_types", ("CHoCH", "BOS")),
                ltf_entry_zone=strategy_overrides.get("s05_ltf_entry_zone", "either"),
                ltf_zone_expiry_bars=strategy_overrides.get("s05_ltf_zone_expiry_bars", 25),
                sl_anchor=strategy_overrides.get("s05_sl_anchor", "ltf_zone"),
            )
            s09_cfg = S09Config(
                mode=strategy_overrides.get("s09_mode", "internal"),
                fvg_to_mss_max_bars=strategy_overrides.get("s09_fvg_to_mss_max_bars", 10),
                min_rr=strategy_overrides.get("s09_min_rr", 1.5),
                fallback_rr=max(
                    2.0,
                    float(strategy_overrides.get("s09_min_rr", 1.5)),
                ),
                require_displacement=strategy_overrides.get("s09_require_displacement", False),
                use_time_filter=strategy_overrides.get("s09_use_time_filter", False),
            )
            strategies = [
                S01ICT2022Strategy(s01_cfg),
                S05BOSOBRetestStrategy(s05_cfg),
                S09ICTSilverBulletStrategy(s09_cfg),
            ]

        if strategy_id == "smc_st_fvg_mss":
            from smc.engine.strategies import SMCSupertrendFVGMSSConfig, SMCSupertrendFVGMSSStrategy
            st_cfg = SMCSupertrendFVGMSSConfig(
                supertrend_atr_length=int(strategy_params.get("supertrend_atr_length", 10)),
                supertrend_multiplier=float(strategy_params.get("supertrend_multiplier", 3.0)),
                supertrend_min_bars=int(strategy_params.get("supertrend_min_bars", 2)),
                sweep_to_mss_max_bars=int(strategy_params.get("sweep_to_mss_max_bars", 8)),
                mss_to_entry_max_bars=int(strategy_params.get("mss_to_entry_max_bars", 15)),
                sl_buffer_atr=float(strategy_params.get("sl_buffer_atr", 0.2)),
                fixed_rr=float(strategy_params.get("fixed_rr", 3.0)),
                require_displacement=bool(strategy_params.get("require_displacement", False)),
            )
            strategies = [
                SMCSupertrendFVGMSSStrategy(st_cfg),
            ]

        # --- Construct and run coordinator ---
        coordinator = SMCBacktestCoordinator(
            execution_config=exec_cfg,
            context_config=ctx_cfg,
            cooldown_bars=cooldown_bars,
            s01_stale_sweep_max_bars=s01_stale_sweep_max_bars,
            mode=strategy_id,  # type: ignore[arg-type]
            htf_events=parsed_htf if parsed_htf else None,
            strategies=strategies,
            initial_capital=self.initial_capital,
        )

        # Ensure df has bar_index and timezone-aware UTC time for Wave 1 coordinator
        df_run = df.copy()
        if "bar_index" not in df_run.columns:
            if "index" in df_run.columns:
                df_run["bar_index"] = df_run["index"]
            else:
                df_run["bar_index"] = list(range(len(df_run)))

        if "time" in df_run.columns and len(df_run) > 0:
            sample_t = df_run["time"].iloc[0]
            if isinstance(sample_t, (int, float, np.integer, np.floating)):
                if sample_t > 100_000_000_000:
                    df_run["time"] = pd.to_datetime(df_run["time"], unit="ms", utc=True)
                elif sample_t > 100_000_000:
                    df_run["time"] = pd.to_datetime(df_run["time"], unit="s", utc=True)
            elif not isinstance(sample_t, pd.Timestamp) or sample_t.tzinfo is None:
                t_series = pd.to_datetime(df_run["time"])
                if t_series.dt.tz is None:
                    df_run["time"] = t_series.dt.tz_localize("UTC")
                else:
                    df_run["time"] = t_series.dt.tz_convert("UTC")

        coord_result = coordinator.run(df_run, htf_events=parsed_htf if parsed_htf else None, start_idx=warmup_bars)

        # --- Serialise CoordinatorResult → JSON-safe dict ---
        net_profit = coord_result.final_balance - self.initial_capital
        return_pct = (net_profit / self.initial_capital * 100) if self.initial_capital != 0 else 0.0

        win_rate = coord_result.win_rate
        pf = coord_result.profit_factor

        metrics = {
            "initial_capital": self.initial_capital,
            "final_balance": coord_result.final_balance,
            "net_profit": round(net_profit, 2),
            "return_pct": round(return_pct, 2),
            "total_trades": coord_result.total_trades,
            "winning_trades": coord_result.win_trades,
            "losing_trades": coord_result.loss_trades,
            "win_rate": win_rate,
            "profit_factor": pf,
            "max_drawdown": coord_result.max_drawdown,
            "max_drawdown_pct": coord_result.max_drawdown_pct,
            "gross_profit": round(
                sum(t["pnl"] for t in coord_result.trades if t.get("pnl", 0) > 0), 2
            ),
            "gross_loss": round(
                abs(sum(t["pnl"] for t in coord_result.trades if t.get("pnl", 0) < 0)), 2
            ),
        }

        result: dict[str, Any] = {
            # Standard fields (parity with legacy)
            "metrics": metrics,
            "trades": list(coord_result.trades),
            "equity_curve": list(coord_result.equity_curve),
            "markers": list(coord_result.markers),
            # V2 optional fields
            "mode": strategy_id,
            "schema_version": "2.0.0",
            "execution_events": [e.to_dict() for e in coord_result.execution_events],
            "decisions": [d.to_dict() for d in coord_result.decisions],
            "pending_intents": [p.to_dict() for p in coord_result.pending_intents],
            "cooldown_snapshot": coord_result.cooldown_snapshot,
            "run_metadata": {
                "timeframe": timeframe,
                "cooldown_bars": cooldown_bars,
                "min_rr": min_rr,
                "s01_stale_sweep_max_bars": s01_stale_sweep_max_bars,
                "htf_events_count": len(parsed_htf),
                "bars_analyzed": len(df),
            },
        }
        return result
