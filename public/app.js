// app.js - Xử lý logic giao diện, gọi API, Bar Replay và Đa Khung Thời Gian

let tradingChart = null;
let secondaryChart = null;
let equityChart = null;
let equitySeries = null;
let currentTf = 'M15';
let secondaryTf = 'H1';
let isDualMode = false;
let availableStrategies = [];

// ==========================================
// 1. REPLAY MANAGER (Quản lý Tua Nến)
// ==========================================
class ReplayManager {
    constructor() {
        this.isActive = false;
        this.isPlaying = false;
        this.isCuttingMode = false;
        this.currentReplayTime = null; // YYYY-MM-DD HH:MM:SS
        this.currentTimestamp = null;
        this.futureQueue = [];
        this.futureQueueSec = [];
        this.speedMs = 500;
        this.timer = null;

        this.toolbar = document.getElementById('replay-toolbar');
        this.timeDisplay = document.getElementById('replay-time-display');
        this.playBtn = document.getElementById('btn-replay-play');
        this.cutBtn = document.getElementById('btn-replay-cut');
        this.speedSelect = document.getElementById('replay-speed-select');
    }

    initEvents() {
        // Nút Toggle Replay trên Header
        document.getElementById('btn-toggle-replay')?.addEventListener('click', () => {
            if (this.isActive) {
                this.exitReplay();
            } else {
                this.enableCutMode();
            }
        });

        // Nút Cắt trên Toolbar
        this.cutBtn?.addEventListener('click', () => {
            this.enableCutMode();
        });

        // Nút Play / Pause
        this.playBtn?.addEventListener('click', () => {
            this.togglePlay();
        });

        // Nút Step Forward (Tua tới 1 nến)
        document.getElementById('btn-replay-step')?.addEventListener('click', () => {
            this.pause();
            this.stepForward();
        });

        // Thay đổi tốc độ
        this.speedSelect?.addEventListener('change', (e) => {
            this.speedMs = parseInt(e.target.value);
            if (this.isPlaying) {
                this.pause();
                this.play();
            }
        });

        // Nút Thoát Replay
        document.getElementById('btn-replay-exit')?.addEventListener('click', () => {
            this.exitReplay();
        });

        // Lắng nghe click trên Chart để cắt nến
        tradingChart.subscribeClick(param => {
            if (this.isCuttingMode && param && param.candle) {
                const cutTime = param.candle.time;
                this.startReplay(cutTime);
            }
        });
    }

    enableCutMode() {
        this.pause();
        this.isCuttingMode = true;
        this.toolbar.classList.add('active');
        this.cutBtn.classList.add('active');
        document.getElementById('btn-toggle-replay')?.classList.add('active');
        document.getElementById('chart-container')?.classList.add('cut-cursor');
        this.timeDisplay.textContent = '📍 Click vào nến để tua';
        this.timeDisplay.style.color = 'var(--accent-gold)';
    }

    async startReplay(cutTime) {
        this.isCuttingMode = false;
        this.cutBtn.classList.remove('active');
        document.getElementById('chart-container')?.classList.remove('cut-cursor');
        this.timeDisplay.style.color = 'var(--text-main)';

        showLoading('Đang tua nến về thời điểm đã chọn...');
        try {
            const res = await fetch(`/api/replay/init?timeframe=${currentTf}&cut_time=${cutTime}&history_limit=1000&future_limit=1500`);
            if (!res.ok) throw new Error('Lỗi khởi tạo Replay');
            const data = await res.json();

            if (!data.history || data.history.length === 0) {
                alert('Không có dữ liệu nến tại thời điểm này.');
                return;
            }

            this.isActive = true;
            this.futureQueue = data.future || [];
            
            const lastBar = data.history[data.history.length - 1];
            this.currentReplayTime = lastBar.datetime_str;
            this.currentTimestamp = lastBar.time;

            tradingChart.setCandles(data.history);
            this.timeDisplay.textContent = this.currentReplayTime;

            // Đồng bộ Chart phụ nếu đang mở Dual Mode
            if (isDualMode && secondaryChart) {
                await this.syncSecondaryChart(this.currentReplayTime);
            }

        } catch (err) {
            alert(`Lỗi Replay: ${err.message}`);
        } finally {
            hideLoading();
        }
    }

    async syncSecondaryChart(cutTime) {
        try {
            const res = await fetch(`/api/replay/init?timeframe=${secondaryTf}&cut_time=${cutTime}&history_limit=1000&future_limit=1500`);
            if (res.ok) {
                const data = await res.json();
                secondaryChart.setCandles(data.history);
                this.futureQueueSec = data.future || [];
            }
        } catch (e) {
            console.error("Lỗi sync secondary chart:", e);
        }
    }

    stepForward() {
        if (!this.isActive) return;

        if (this.futureQueue.length === 0) {
            this.pause();
            alert('Đã xem hết dữ liệu nến trong đệm Replay.');
            return;
        }

        const nextBar = this.futureQueue.shift();
        tradingChart.updateBar(nextBar);

        this.currentReplayTime = nextBar.datetime_str;
        this.currentTimestamp = nextBar.time;
        this.timeDisplay.textContent = this.currentReplayTime;

        // Tiến nến Chart phụ nếu đến hạn
        if (isDualMode && secondaryChart && this.futureQueueSec.length > 0) {
            const nextSecBar = this.futureQueueSec[0];
            if (nextSecBar.time <= nextBar.time) {
                secondaryChart.updateBar(this.futureQueueSec.shift());
            }
        }
    }

    togglePlay() {
        if (this.isPlaying) {
            this.pause();
        } else {
            this.play();
        }
    }

    play() {
        if (!this.isActive) {
            if (this.isCuttingMode) {
                alert('Vui lòng click vào một cây nến trên biểu đồ để chọn thời điểm bắt đầu!');
            } else {
                this.enableCutMode();
            }
            return;
        }

        this.isPlaying = true;
        this.playBtn.innerHTML = '⏸️';
        this.playBtn.title = 'Tạm dừng';
        this.timer = setInterval(() => {
            this.stepForward();
        }, this.speedMs);
    }

    pause() {
        this.isPlaying = false;
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = null;
        }
        if (this.playBtn) {
            this.playBtn.innerHTML = '▶️';
            this.playBtn.title = 'Phát tự động';
        }
    }

    exitReplay() {
        this.pause();
        this.isActive = false;
        this.isCuttingMode = false;
        this.currentReplayTime = null;
        this.futureQueue = [];
        this.futureQueueSec = [];

        this.toolbar.classList.remove('active');
        this.cutBtn.classList.remove('active');
        document.getElementById('btn-toggle-replay')?.classList.remove('active');
        document.getElementById('chart-container')?.classList.remove('cut-cursor');

        // Tải lại nến mới nhất
        loadCandles(currentTf);
        if (isDualMode && secondaryChart) {
            loadSecondaryCandles(secondaryTf);
        }
    }

    async handleTimeframeChange(newTf) {
        if (!this.isActive) return;

        this.pause();
        showLoading(`Đang tái tạo dữ liệu nến khung ${newTf} tại ${this.currentReplayTime}...`);
        try {
            const res = await fetch(`/api/replay/init?timeframe=${newTf}&cut_time=${this.currentReplayTime}&history_limit=1000&future_limit=1500`);
            if (!res.ok) throw new Error('Lỗi chuyển khung trong Replay');
            const data = await res.json();
            
            tradingChart.setCandles(data.history);
            this.futureQueue = data.future || [];
            if (data.history.length > 0) {
                const lastBar = data.history[data.history.length - 1];
                this.currentReplayTime = lastBar.datetime_str;
                this.currentTimestamp = lastBar.time;
                this.timeDisplay.textContent = this.currentReplayTime;
            }
        } catch (err) {
            alert(`Lỗi chuyển khung: ${err.message}`);
        } finally {
            hideLoading();
        }
    }
}

let replayManager = null;

// ==========================================
// 2. KHỞI CHẠY ỨNG DỤNG
// ==========================================
document.addEventListener('DOMContentLoaded', async () => {
    initCharts();
    setupTimeframeButtons();
    setupDualChartToggle();
    setupTabs();
    
    replayManager = new ReplayManager();
    replayManager.initEvents();

    await loadDatabaseInfo();
    await loadStrategies();
    await loadCandles(currentTf);
});

function initCharts() {
    tradingChart = new window.TradingChart('chart-container');
}

function showLoading(text = 'Đang tải dữ liệu nến...') {
    const overlay = document.getElementById('loading-overlay');
    const loadingText = document.getElementById('loading-text');
    if (loadingText) loadingText.textContent = text;
    if (overlay) overlay.classList.add('active');
}

function hideLoading() {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) overlay.classList.remove('active');
}

// 3. Tải thông tin Database
async function loadDatabaseInfo() {
    try {
        const res = await fetch('/api/info');
        if (!res.ok) throw new Error('Không thể lấy thông tin DB');
        const info = await res.json();
        const badge = document.getElementById('data-range-badge');
        if (badge) {
            const startYear = info.start_time.substring(0, 4);
            const endYear = info.end_time.substring(0, 4);
            badge.textContent = `📦 ${info.total_m1_candles.toLocaleString()} nến M1 (${startYear} - ${endYear})`;
        }
    } catch (err) {
        console.error("Lỗi loadDatabaseInfo:", err);
    }
}

// 4. Chuyển đổi khung thời gian biểu đồ chính
function setupTimeframeButtons() {
    const buttons = document.querySelectorAll('#timeframe-group .tf-btn');
    buttons.forEach(btn => {
        btn.addEventListener('click', async () => {
            if (btn.classList.contains('active')) return;
            buttons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentTf = btn.dataset.tf;
            document.getElementById('chart-1-tf').textContent = currentTf;

            if (replayManager && replayManager.isActive) {
                await replayManager.handleTimeframeChange(currentTf);
            } else {
                await loadCandles(currentTf);
            }
        });
    });
}

// 5. Quản lý Chế độ 2 Biểu đồ (Dual Chart)
function setupDualChartToggle() {
    const btn = document.getElementById('btn-toggle-dual');
    const grid = document.getElementById('charts-grid');
    const box2 = document.getElementById('chart-box-2');
    const subHeader1 = document.getElementById('chart-sub-header-1');

    btn?.addEventListener('click', async () => {
        isDualMode = !isDualMode;
        if (isDualMode) {
            btn.classList.add('active');
            grid.classList.add('dual-mode');
            box2.classList.remove('hidden');
            subHeader1.style.display = 'flex';

            if (!secondaryChart) {
                secondaryChart = new window.TradingChart('chart-container-2');
                setupSecondaryTfButtons();
            }

            if (replayManager && replayManager.isActive) {
                await replayManager.syncSecondaryChart(replayManager.currentReplayTime);
            } else {
                await loadSecondaryCandles(secondaryTf);
            }
        } else {
            btn.classList.remove('active');
            grid.classList.remove('dual-mode');
            box2.classList.add('hidden');
            subHeader1.style.display = 'none';
        }

        // Tự động resize charts
        setTimeout(() => {
            window.dispatchEvent(new Event('resize'));
        }, 150);
    });
}

function setupSecondaryTfButtons() {
    const buttons = document.querySelectorAll('#chart-2-tf-group .tf-btn');
    buttons.forEach(btn => {
        btn.addEventListener('click', async () => {
            if (btn.classList.contains('active')) return;
            buttons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            secondaryTf = btn.dataset.tf;
            document.getElementById('chart-2-tf').textContent = secondaryTf;

            if (replayManager && replayManager.isActive) {
                await replayManager.syncSecondaryChart(replayManager.currentReplayTime);
            } else {
                await loadSecondaryCandles(secondaryTf);
            }
        });
    });
}

async function loadSecondaryCandles(timeframe, limit = 1200) {
    try {
        const res = await fetch(`/api/candles?timeframe=${timeframe}&limit=${limit}`);
        if (res.ok) {
            const data = await res.json();
            if (secondaryChart && data.candles) {
                secondaryChart.setCandles(data.candles);
            }
        }
    } catch (e) {
        console.error("Lỗi tải secondary candles:", e);
    }
}

// 6. Tải nến từ API cho Chart chính
async function loadCandles(timeframe, limit = 1200) {
    showLoading(`Đang tải nến khung ${timeframe}...`);
    try {
        const res = await fetch(`/api/candles?timeframe=${timeframe}&limit=${limit}`);
        if (!res.ok) throw new Error(`Lỗi tải nến: ${res.statusText}`);
        const data = await res.json();
        if (tradingChart && data.candles) {
            tradingChart.setCandles(data.candles);
        }
    } catch (err) {
        alert(`Không thể tải dữ liệu nến: ${err.message}`);
    } finally {
        hideLoading();
    }
}

// 7. Quản lý Tabs
function setupTabs() {
    const tabBtns = document.querySelectorAll('.panel-tab');
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            const target = document.getElementById(btn.dataset.tab);
            if (target) target.classList.add('active');
        });
    });

    document.getElementById('btn-clear-markers')?.addEventListener('click', () => {
        if (tradingChart) tradingChart.clearMarkers();
    });
}

// 8. Tải danh sách chiến lược
async function loadStrategies() {
    try {
        const res = await fetch('/api/strategies');
        const data = await res.json();
        availableStrategies = data.strategies || [];
        
        const select = document.getElementById('strategy-select');
        select.innerHTML = '';
        availableStrategies.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name;
            select.appendChild(opt);
        });

        select.addEventListener('change', () => {
            renderStrategyParams(select.value);
        });

        if (availableStrategies.length > 0) {
            renderStrategyParams(availableStrategies[0].id);
        }
    } catch (err) {
        console.error("Lỗi loadStrategies:", err);
    }
}

function renderStrategyParams(strategyId) {
    const container = document.getElementById('strategy-params-container');
    container.innerHTML = '';
    const strat = availableStrategies.find(s => s.id === strategyId);
    if (!strat || !strat.params) return;

    strat.params.forEach(p => {
        const formGroup = document.createElement('div');
        formGroup.className = 'form-group';
        
        const label = document.createElement('label');
        label.textContent = p.label;
        
        const input = document.createElement('input');
        input.type = 'number';
        input.className = 'form-control param-input';
        input.dataset.paramName = p.name;
        input.value = p.default;
        if (p.min !== undefined) input.min = p.min;
        if (p.max !== undefined) input.max = p.max;

        formGroup.appendChild(label);
        formGroup.appendChild(input);
        container.appendChild(formGroup);
    });
}

// 9. Thực thi Backtest
async function runBacktest() {
    const strategyId = document.getElementById('strategy-select').value;
    const initialCapital = parseFloat(document.getElementById('initial-capital').value) || 10000;
    const lotSize = parseFloat(document.getElementById('lot-size').value) || 0.1;
    const stopLoss = parseFloat(document.getElementById('stop-loss').value) || 0;
    const takeProfit = parseFloat(document.getElementById('take-profit').value) || 0;
    const spread = parseFloat(document.getElementById('spread-points').value) || 20;
    const commission = parseFloat(document.getElementById('commission').value) || 5;
    const limit = parseInt(document.getElementById('candles-limit').value) || 1000;
    const allowShort = document.getElementById('allow-short').checked;

    const strategyParams = {};
    document.querySelectorAll('.param-input').forEach(inp => {
        strategyParams[inp.dataset.paramName] = parseFloat(inp.value);
    });

    const payload = {
        timeframe: currentTf,
        limit: limit,
        strategy_id: strategyId,
        strategy_params: strategyParams,
        initial_capital: initialCapital,
        lot_size: lotSize,
        stop_loss_points: stopLoss,
        take_profit_points: takeProfit,
        spread_points: spread,
        commission_per_lot: commission,
        allow_short: allowShort
    };

    showLoading('Đang chạy Backtest mô phỏng khớp lệnh...');
    const btn = document.getElementById('btn-run-backtest');
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/backtest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!res.ok) {
            const errData = await res.json();
            throw new Error(errData.detail || 'Lỗi khi chạy backtest');
        }

        const data = await res.json();
        displayBacktestResults(data);

    } catch (err) {
        alert(`Lỗi Backtest: ${err.message}`);
    } finally {
        hideLoading();
        if (btn) btn.disabled = false;
    }
}

// 10. Hiển thị kết quả & Đồ thị báo cáo
function displayBacktestResults(data) {
    const { metrics, trades, equity_curve, markers } = data;

    document.querySelector('[data-tab="tab-results"]').click();

    document.getElementById('results-empty').style.display = 'none';
    document.getElementById('results-content').style.display = 'block';

    const netPnlEl = document.getElementById('m-net-pnl');
    netPnlEl.textContent = `${metrics.net_profit >= 0 ? '+' : ''}$${metrics.net_profit.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
    netPnlEl.style.color = metrics.net_profit >= 0 ? 'var(--color-green)' : 'var(--color-red)';

    const returnEl = document.getElementById('m-return-pct');
    returnEl.textContent = `${metrics.return_pct >= 0 ? '+' : ''}${metrics.return_pct.toFixed(2)}%`;
    returnEl.style.color = metrics.return_pct >= 0 ? 'var(--color-green)' : 'var(--color-red)';

    document.getElementById('m-win-rate').textContent = `${metrics.win_rate.toFixed(1)}%`;
    document.getElementById('m-profit-factor').textContent = metrics.profit_factor >= 999 ? '∞' : metrics.profit_factor.toFixed(2);
    document.getElementById('m-trades-ratio').textContent = `${metrics.winning_trades} / ${metrics.losing_trades} (${metrics.total_trades} lệnh)`;
    document.getElementById('m-max-dd').textContent = `-$${metrics.max_drawdown.toLocaleString()} (-${metrics.max_drawdown_pct.toFixed(2)}%)`;

    renderEquityCurve(equity_curve);
    renderTradesTable(trades);

    if (tradingChart && markers) {
        tradingChart.setMarkers(markers);
    }
}

function renderEquityCurve(equityCurve) {
    const container = document.getElementById('equity-chart');
    container.innerHTML = '';

    if (!window.LightweightCharts || !equityCurve || equityCurve.length === 0) return;

    equityChart = LightweightCharts.createChart(container, {
        layout: {
            background: { color: '#131722' },
            textColor: '#787b86',
            fontSize: 10,
        },
        grid: {
            vertLines: { color: 'rgba(255, 255, 255, 0.03)' },
            horzLines: { color: 'rgba(255, 255, 255, 0.03)' },
        },
        rightPriceScale: {
            borderColor: '#2a2e39',
        },
        timeScale: {
            borderColor: '#2a2e39',
            timeVisible: true,
            secondsVisible: false,
        },
    });

    equitySeries = equityChart.addLineSeries({
        color: '#2962ff',
        lineWidth: 2,
    });

    const data = equityCurve.map(item => ({
        time: item.time,
        value: item.equity,
    }));

    equitySeries.setData(data);
    equityChart.timeScale().fitContent();

    new ResizeObserver(entries => {
        if (entries.length > 0 && entries[0].contentRect) {
            const { width, height } = entries[0].contentRect;
            equityChart.applyOptions({ width, height });
        }
    }).observe(container);
}

function renderTradesTable(trades) {
    const tbody = document.getElementById('trades-tbody');
    tbody.innerHTML = '';

    if (!trades || trades.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color: var(--text-muted); padding: 15px;">Không phát sinh giao dịch nào.</td></tr>`;
        return;
    }

    trades.forEach(tr => {
        const row = document.createElement('tr');
        const pnlColor = tr.pnl >= 0 ? 'var(--color-green)' : 'var(--color-red)';
        const tagClass = tr.type === 'BUY' ? 'tag-buy' : 'tag-sell';

        row.innerHTML = `
            <td>#${tr.trade_id}</td>
            <td class="${tagClass}">${tr.type}</td>
            <td title="${tr.entry_time}">${tr.entry_price.toFixed(2)}</td>
            <td title="${tr.exit_time}">${tr.exit_price.toFixed(2)}</td>
            <td style="color: ${pnlColor}; font-weight: bold;">${tr.pnl >= 0 ? '+' : ''}$${tr.pnl.toFixed(1)}</td>
            <td style="color: var(--text-muted);">${tr.exit_reason}</td>
        `;

        tbody.appendChild(row);
    });
}
