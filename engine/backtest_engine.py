import pandas as pd
import numpy as np
from engine.strategies import StrategyRegistry

REQUIRED_BACKTEST_COLS = {'time', 'open', 'high', 'low', 'close'}

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

    def run(self, df: pd.DataFrame, strategy_id: str, strategy_params: dict):
        """
        Thực thi backtest mô phỏng khớp lệnh:
        - df bắt buộc có các cột: 'time', 'open', 'high', 'low', 'close'
        - Loại bỏ Lookahead Bias: Tín hiệu sinh tại nến N chỉ khớp tại nến N+1 theo giá Open.
        - Spread và Commission đối xứng cho cả Long và Short.
        - Forced close cuối kỳ cập nhật đầy đủ balance, equity curve, MDD, markers, trade record.
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

        balance = self.initial_capital
        position = None  # None hoặc dict chứa thông tin vị thế đang mở
        trades = []
        equity_curve = []
        chart_markers = []

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

        for i in range(n_bars):
            t = timestamps[i]
            t_str = time_strs[i]
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]

            # Tín hiệu được sinh từ cây nến trước (N-1) khớp ở cây nến này (N) tại giá Open
            prev_sig = signals[i - 1] if i > 0 else 0

            # -----------------------------------------------------------------
            # 1. Khớp lệnh tại Open của nến i dựa trên tín hiệu ở nến i-1 (No Lookahead)
            # -----------------------------------------------------------------
            if i > 0 and prev_sig != 0:
                # 1a. Xử lý đóng lệnh do tín hiệu đảo chiều
                if position is not None:
                    if position['type'] == 'LONG' and prev_sig == -1:
                        # Bán Long tại giá Open (Bid)
                        exit_price = o
                        gross_pnl = (exit_price - position['entry_price']) * self.lot_size * self.contract_size
                        net_pnl = gross_pnl - (self.commission_per_side * 2)
                        balance += net_pnl

                        trades.append({
                            "trade_id": len(trades) + 1,
                            "type": "BUY",
                            "entry_time": position['entry_time_str'],
                            "entry_timestamp": position['entry_timestamp'],
                            "entry_price": round(position['entry_price'], 3),
                            "exit_time": t_str,
                            "exit_timestamp": t,
                            "exit_price": round(exit_price, 3),
                            "pnl": round(net_pnl, 2),
                            "return_pct": round((net_pnl / self.initial_capital) * 100, 2),
                            "exit_reason": "Signal Reversal"
                        })
                        chart_markers.append({
                            "time": t,
                            "position": "aboveBar",
                            "color": "#089981" if net_pnl >= 0 else "#f23645",
                            "shape": "circle",
                            "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}"
                        })
                        position = None

                    elif position['type'] == 'SHORT' and prev_sig == 1:
                        # Mua lại Short tại giá Open + Spread (Ask)
                        exit_bid = o
                        exit_price = exit_bid + self.spread_val
                        gross_pnl = (position['entry_price'] - exit_price) * self.lot_size * self.contract_size
                        net_pnl = gross_pnl - (self.commission_per_side * 2)
                        balance += net_pnl

                        trades.append({
                            "trade_id": len(trades) + 1,
                            "type": "SELL",
                            "entry_time": position['entry_time_str'],
                            "entry_timestamp": position['entry_timestamp'],
                            "entry_price": round(position['entry_price'], 3),
                            "exit_time": t_str,
                            "exit_timestamp": t,
                            "exit_price": round(exit_price, 3),
                            "pnl": round(net_pnl, 2),
                            "return_pct": round((net_pnl / self.initial_capital) * 100, 2),
                            "exit_reason": "Signal Reversal"
                        })
                        chart_markers.append({
                            "time": t,
                            "position": "belowBar",
                            "color": "#089981" if net_pnl >= 0 else "#f23645",
                            "shape": "circle",
                            "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}"
                        })
                        position = None

                # 1b. Vào lệnh mới tại Open nếu đang flat
                if position is None:
                    if prev_sig == 1:
                        # Mua Long: khớp tại Open + Spread (Ask)
                        entry_p = o + self.spread_val
                        sl_p = entry_p - self.stop_loss_val if self.stop_loss_val > 0 else 0.0
                        tp_p = entry_p + self.take_profit_val if self.take_profit_val > 0 else 0.0
                        position = {
                            "type": "LONG",
                            "entry_price": entry_p,
                            "entry_timestamp": t,
                            "entry_time_str": t_str,
                            "sl_price": sl_p,
                            "tp_price": tp_p
                        }
                        chart_markers.append({
                            "time": t,
                            "position": "belowBar",
                            "color": "#2962ff",
                            "shape": "arrowUp",
                            "text": f"BUY @ {entry_p:.2f}"
                        })

                    elif prev_sig == -1 and self.allow_short:
                        # Bán Short: khớp tại Open (Bid)
                        entry_p = o
                        sl_p = entry_p + self.stop_loss_val if self.stop_loss_val > 0 else 0.0
                        tp_p = entry_p - self.take_profit_val if self.take_profit_val > 0 else 0.0
                        position = {
                            "type": "SHORT",
                            "entry_price": entry_p,
                            "entry_timestamp": t,
                            "entry_time_str": t_str,
                            "sl_price": sl_p,
                            "tp_price": tp_p
                        }
                        chart_markers.append({
                            "time": t,
                            "position": "aboveBar",
                            "color": "#e91e63",
                            "shape": "arrowDown",
                            "text": f"SELL @ {entry_p:.2f}"
                        })

            # -----------------------------------------------------------------
            # 2. Kiểm tra Stop Loss / Take Profit trong thân nến i
            # -----------------------------------------------------------------
            if position is not None:
                closed = False
                exit_price = 0.0
                exit_reason = ""
                marker_pos = "aboveBar"

                if position['type'] == 'LONG':
                    # Kiểm tra SL trước (bảo thủ)
                    if self.stop_loss_val > 0 and l <= position['sl_price']:
                        exit_price = position['sl_price']
                        exit_reason = "Stop Loss"
                        closed = True
                        marker_pos = "aboveBar"
                    elif self.take_profit_val > 0 and h >= position['tp_price']:
                        exit_price = position['tp_price']
                        exit_reason = "Take Profit"
                        closed = True
                        marker_pos = "aboveBar"

                    if closed:
                        gross_pnl = (exit_price - position['entry_price']) * self.lot_size * self.contract_size
                        net_pnl = gross_pnl - (self.commission_per_side * 2)
                        balance += net_pnl

                        trades.append({
                            "trade_id": len(trades) + 1,
                            "type": "BUY",
                            "entry_time": position['entry_time_str'],
                            "entry_timestamp": position['entry_timestamp'],
                            "entry_price": round(position['entry_price'], 3),
                            "exit_time": t_str,
                            "exit_timestamp": t,
                            "exit_price": round(exit_price, 3),
                            "pnl": round(net_pnl, 2),
                            "return_pct": round((net_pnl / self.initial_capital) * 100, 2),
                            "exit_reason": exit_reason
                        })
                        chart_markers.append({
                            "time": t,
                            "position": marker_pos,
                            "color": "#089981" if net_pnl >= 0 else "#f23645",
                            "shape": "circle",
                            "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}"
                        })
                        position = None

                elif position['type'] == 'SHORT':
                    # Short thoát bằng lệnh Mua (Ask = Bid + Spread)
                    # Trigger Stop Loss theo giá Ask: High + spread >= sl_price
                    # Quy ước ưu tiên: nếu nến chạm cả SL và TP, ưu tiên SL trước (bảo thủ)
                    if self.stop_loss_val > 0 and (h + self.spread_val) >= position['sl_price']:
                        exit_price = position['sl_price']
                        exit_reason = "Stop Loss"
                        closed = True
                        marker_pos = "belowBar"
                    # Trigger Take Profit theo giá Ask: Low + spread <= tp_price
                    elif self.take_profit_val > 0 and (l + self.spread_val) <= position['tp_price']:
                        exit_price = position['tp_price']
                        exit_reason = "Take Profit"
                        closed = True
                        marker_pos = "belowBar"

                    if closed:
                        gross_pnl = (position['entry_price'] - exit_price) * self.lot_size * self.contract_size
                        net_pnl = gross_pnl - (self.commission_per_side * 2)
                        balance += net_pnl

                        trades.append({
                            "trade_id": len(trades) + 1,
                            "type": "SELL",
                            "entry_time": position['entry_time_str'],
                            "entry_timestamp": position['entry_timestamp'],
                            "entry_price": round(position['entry_price'], 3),
                            "exit_time": t_str,
                            "exit_timestamp": t,
                            "exit_price": round(exit_price, 3),
                            "pnl": round(net_pnl, 2),
                            "return_pct": round((net_pnl / self.initial_capital) * 100, 2),
                            "exit_reason": exit_reason
                        })
                        chart_markers.append({
                            "time": t,
                            "position": marker_pos,
                            "color": "#089981" if net_pnl >= 0 else "#f23645",
                            "shape": "circle",
                            "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}"
                        })
                        position = None

            # -----------------------------------------------------------------
            # 3. Tính toán Floating PnL & Equity hiện tại
            # -----------------------------------------------------------------
            floating_pnl = 0.0
            if position is not None:
                if position['type'] == 'LONG':
                    # Long đóng tại Close (Bid)
                    floating_gross = (c - position['entry_price']) * self.lot_size * self.contract_size
                else:
                    # Short đóng tại Close + Spread (Ask)
                    floating_gross = (position['entry_price'] - (c + self.spread_val)) * self.lot_size * self.contract_size
                floating_pnl = floating_gross - (self.commission_per_side * 2)

            current_equity = balance + floating_pnl
            if current_equity > peak_equity:
                peak_equity = current_equity

            dd = peak_equity - current_equity
            dd_pct = (dd / peak_equity * 100) if peak_equity > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct

            # Lưu điểm equity định kỳ hoặc khi có vị thế
            if i % 5 == 0 or i == n_bars - 1 or position is not None:
                equity_curve.append({
                    "time": t,
                    "equity": round(current_equity, 2),
                    "balance": round(balance, 2)
                })

        # ---------------------------------------------------------------------
        # 4. Forced Close vị thế còn mở ở nến cuối kỳ backtest
        # ---------------------------------------------------------------------
        if position is not None:
            last_c = closes[-1]
            last_t = timestamps[-1]
            last_t_str = time_strs[-1]

            if position['type'] == 'LONG':
                exit_price = last_c
                gross_pnl = (exit_price - position['entry_price']) * self.lot_size * self.contract_size
                marker_pos = "aboveBar"
            else:
                exit_price = last_c + self.spread_val
                gross_pnl = (position['entry_price'] - exit_price) * self.lot_size * self.contract_size
                marker_pos = "belowBar"

            net_pnl = gross_pnl - (self.commission_per_side * 2)
            balance += net_pnl

            trades.append({
                "trade_id": len(trades) + 1,
                "type": "BUY" if position['type'] == 'LONG' else "SELL",
                "entry_time": position['entry_time_str'],
                "entry_timestamp": position['entry_timestamp'],
                "entry_price": round(position['entry_price'], 3),
                "exit_time": last_t_str,
                "exit_timestamp": last_t,
                "exit_price": round(exit_price, 3),
                "pnl": round(net_pnl, 2),
                "return_pct": round((net_pnl / self.initial_capital) * 100, 2),
                "exit_reason": "End of Backtest"
            })

            chart_markers.append({
                "time": last_t,
                "position": marker_pos,
                "color": "#089981" if net_pnl >= 0 else "#f23645",
                "shape": "circle",
                "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}"
            })

            # Cập nhật Peak, Drawdown và Equity Curve sau khi đóng vị thế cuối
            final_equity = balance
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
                "time": last_t,
                "equity": round(final_equity, 2),
                "balance": round(balance, 2)
            })

        # ---------------------------------------------------------------------
        # 5. Thống kê hiệu suất tổng kết
        # ---------------------------------------------------------------------
        total_trades = len(trades)
        winning_trades = [tr for tr in trades if tr['pnl'] > 0]
        losing_trades = [tr for tr in trades if tr['pnl'] < 0]

        gross_profit = sum(tr['pnl'] for tr in winning_trades)
        gross_loss = abs(sum(tr['pnl'] for tr in losing_trades))
        net_profit = balance - self.initial_capital
        return_pct = (net_profit / self.initial_capital) * 100 if self.initial_capital > 0 else 0.0

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

        result = {
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

        return result
