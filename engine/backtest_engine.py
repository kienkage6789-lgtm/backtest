import pandas as pd
import numpy as np
from engine.strategies import StrategyRegistry

class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 10000.0,
        lot_size: float = 0.1,
        contract_size: float = 100.0,
        stop_loss_points: float = 0.0,   # Điểm giá (1.00 USD = 100 points)
        take_profit_points: float = 0.0,
        spread_points: float = 20.0,     # 20 points = 0.20 USD
        commission_per_lot: float = 5.0, # 5 USD / lot
        allow_short: bool = True
    ):
        self.initial_capital = float(initial_capital)
        self.lot_size = float(lot_size)
        self.contract_size = float(contract_size)
        self.stop_loss_val = float(stop_loss_points) / 100.0   # Chuyển điểm thành USD giá vàng
        self.take_profit_val = float(take_profit_points) / 100.0
        self.spread_val = float(spread_points) / 100.0
        self.commission = float(commission_per_lot) * self.lot_size
        self.allow_short = bool(allow_short)

    def run(self, df: pd.DataFrame, strategy_id: str, strategy_params: dict):
        """
        Thực thi backtest trên DataFrame nến.
        df bắt buộc có: 'time', 'open', 'high', 'low', 'close'
        """
        if df.empty or len(df) < 2:
            raise ValueError("Không đủ dữ liệu nến để thực hiện backtest.")

        # Sinh tín hiệu từ chiến lược
        df_signals = StrategyRegistry.generate_signals(df, strategy_id, strategy_params)

        balance = self.initial_capital
        position = None # None, hoặc dict chứa thông tin vị thế
        trades = []
        equity_curve = []
        chart_markers = []

        timestamps = (pd.to_datetime(df_signals['time']).astype('int64') // 10**9).tolist()
        opens = df_signals['open'].tolist()
        highs = df_signals['high'].tolist()
        lows = df_signals['low'].tolist()
        closes = df_signals['close'].tolist()
        signals = df_signals['signal'].tolist()
        time_strs = df_signals['time'].tolist()

        peak_equity = self.initial_capital
        max_drawdown = 0.0
        max_drawdown_pct = 0.0

        for i in range(len(df_signals)):
            t = timestamps[i]
            t_str = time_strs[i]
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            sig = signals[i]

            # 1. Kiểm tra vị thế đang mở (SL / TP)
            if position is not None:
                closed = False
                exit_price = 0.0
                exit_reason = ""

                if position['type'] == 'LONG':
                    # Kiểm tra SL trước (bảo thủ)
                    if self.stop_loss_val > 0 and l <= position['sl_price']:
                        exit_price = position['sl_price']
                        exit_reason = "Stop Loss"
                        closed = True
                    elif self.take_profit_val > 0 and h >= position['tp_price']:
                        exit_price = position['tp_price']
                        exit_reason = "Take Profit"
                        closed = True
                    # Kiểm tra tín hiệu đảo chiều
                    elif sig == -1:
                        exit_price = c
                        exit_reason = "Signal Reversal"
                        closed = True

                    if closed:
                        # Tính PnL cho Long
                        # Mua tại entry_price (đã cộng spread lúc vào), bán tại exit_price
                        gross_pnl = (exit_price - position['entry_price']) * self.lot_size * self.contract_size
                        net_pnl = gross_pnl - (self.commission * 2)
                        balance += net_pnl
                        
                        trade_record = {
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
                        }
                        trades.append(trade_record)

                        # Marker đóng lệnh
                        chart_markers.append({
                            "time": t,
                            "position": "aboveBar",
                            "color": "#089981" if net_pnl >= 0 else "#f23645",
                            "shape": "circle",
                            "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}"
                        })

                        position = None

                elif position['type'] == 'SHORT':
                    if self.stop_loss_val > 0 and h >= position['sl_price']:
                        exit_price = position['sl_price']
                        exit_reason = "Stop Loss"
                        closed = True
                    elif self.take_profit_val > 0 and l <= position['tp_price']:
                        exit_price = position['tp_price']
                        exit_reason = "Take Profit"
                        closed = True
                    elif sig == 1:
                        exit_price = c
                        exit_reason = "Signal Reversal"
                        closed = True

                    if closed:
                        gross_pnl = (position['entry_price'] - exit_price) * self.lot_size * self.contract_size
                        net_pnl = gross_pnl - (self.commission * 2)
                        balance += net_pnl

                        trade_record = {
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
                        }
                        trades.append(trade_record)

                        chart_markers.append({
                            "time": t,
                            "position": "belowBar",
                            "color": "#089981" if net_pnl >= 0 else "#f23645",
                            "shape": "circle",
                            "text": f"EXIT {'+' if net_pnl >= 0 else ''}${net_pnl:.1f}"
                        })

                        position = None

            # 2. Vào lệnh mới nếu đang flat
            if position is None:
                if sig == 1:
                    # Mua Long: Giá khớp = giá đóng cửa + spread
                    entry_p = c + self.spread_val
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

                elif sig == -1 and self.allow_short:
                    # Bán Short: Giá khớp = giá đóng cửa
                    entry_p = c
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

            # 3. Tính toán Equity hiện tại
            floating_pnl = 0.0
            if position is not None:
                if position['type'] == 'LONG':
                    floating_pnl = (c - position['entry_price']) * self.lot_size * self.contract_size - (self.commission * 2)
                else:
                    floating_pnl = (position['entry_price'] - c) * self.lot_size * self.contract_size - (self.commission * 2)

            current_equity = balance + floating_pnl
            if current_equity > peak_equity:
                peak_equity = current_equity
            
            dd = peak_equity - current_equity
            dd_pct = (dd / peak_equity) * 100 if peak_equity > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct

            # Chỉ lưu 1 điểm equity cho mỗi 5-10 nến hoặc nến có giao dịch để đồ thị mượt nhẹ
            if i % 5 == 0 or i == len(df_signals) - 1 or position is not None:
                equity_curve.append({
                    "time": t,
                    "equity": round(current_equity, 2),
                    "balance": round(balance, 2)
                })

        # Đóng vị thế còn dang dở ở nến cuối
        if position is not None:
            last_c = closes[-1]
            last_t = timestamps[-1]
            last_t_str = time_strs[-1]
            if position['type'] == 'LONG':
                pnl = (last_c - position['entry_price']) * self.lot_size * self.contract_size - (self.commission * 2)
            else:
                pnl = (position['entry_price'] - last_c) * self.lot_size * self.contract_size - (self.commission * 2)
            
            balance += pnl
            trades.append({
                "trade_id": len(trades) + 1,
                "type": "BUY" if position['type'] == 'LONG' else "SELL",
                "entry_time": position['entry_time_str'],
                "entry_timestamp": position['entry_timestamp'],
                "entry_price": round(position['entry_price'], 3),
                "exit_time": last_t_str,
                "exit_timestamp": last_t,
                "exit_price": round(last_c, 3),
                "pnl": round(pnl, 2),
                "return_pct": round((pnl / self.initial_capital) * 100, 2),
                "exit_reason": "End of Backtest"
            })

        # Tính toán các chỉ số thống kê
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

        return {
            "metrics": metrics,
            "trades": trades,
            "equity_curve": equity_curve,
            "markers": chart_markers
        }
