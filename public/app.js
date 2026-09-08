// app.js - Xử lý logic giao diện, gọi API, Bar Replay và Đa Khung Thời Gian

let tradingChart = null;
let secondaryChart = null;
let equityChart = null;
let equitySeries = null;
let equityResizeObserver = null;
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

            // Cập nhật Replay filter cho DrawingManager
            if (tradingChart && tradingChart.drawingManager) {
                tradingChart.drawingManager.isReplayActive = true;
                tradingChart.drawingManager.currentReplayTime = this.currentTimestamp;
                tradingChart.drawingManager.requestRender();
            }

            // Đồng bộ Chart phụ nếu đang mở Dual Mode
            if (isDualMode && secondaryChart) {
                await this.syncSecondaryChart(this.currentReplayTime);
                if (secondaryChart.drawingManager) {
                    secondaryChart.drawingManager.isReplayActive = true;
                    secondaryChart.drawingManager.currentReplayTime = this.currentTimestamp;
                    secondaryChart.drawingManager.requestRender();
                }
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

        if (tradingChart && tradingChart.drawingManager) {
            tradingChart.drawingManager.currentReplayTime = this.currentTimestamp;
            tradingChart.drawingManager.requestRender();
        }

        // Tiến nến Chart phụ nếu đến hạn (tiến TOÀN BỘ các nến có time <= nến chính)
        if (isDualMode && secondaryChart && this.futureQueueSec.length > 0) {
            while (this.futureQueueSec.length > 0 && this.futureQueueSec[0].time <= nextBar.time) {
                secondaryChart.updateBar(this.futureQueueSec.shift());
            }
            if (secondaryChart.drawingManager) {
                secondaryChart.drawingManager.currentReplayTime = this.currentTimestamp;
                secondaryChart.drawingManager.requestRender();
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

        if (tradingChart && tradingChart.drawingManager) {
            tradingChart.drawingManager.isReplayActive = false;
            tradingChart.drawingManager.currentReplayTime = null;
            tradingChart.drawingManager.requestRender();
        }
        if (secondaryChart && secondaryChart.drawingManager) {
            secondaryChart.drawingManager.isReplayActive = false;
            secondaryChart.drawingManager.currentReplayTime = null;
            secondaryChart.drawingManager.requestRender();
        }

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

            if (isDualMode && secondaryChart && this.currentReplayTime) {
                await this.syncSecondaryChart(this.currentReplayTime);
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
    setupDrawingToolbar();
    setupFavoritesBar();
    setupMiniStyleBar();
    setupPropertyDialog();
    setupContextMenu();
    setupAlertToasts();
    setupDrawingHotkeys();
    
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

            if (tradingChart && tradingChart.drawingManager) {
                tradingChart.drawingManager.currentTimeframe = currentTf;
                tradingChart.drawingManager.requestRender();
            }

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

            if (secondaryChart && secondaryChart.drawingManager) {
                secondaryChart.drawingManager.currentTimeframe = secondaryTf;
                secondaryChart.drawingManager.requestRender();
            }

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

// 7. Quản lý Collapsible Drawer & Tabs
const DRAWER_STORAGE_KEY = 'backtest:ui:drawer';
const DEFAULT_DRAWER_STATE = { isOpen: false, activeTab: 'tab-strategy' };

function getDrawerState() {
    try {
        const raw = localStorage.getItem(DRAWER_STORAGE_KEY);
        if (!raw) return { ...DEFAULT_DRAWER_STATE };
        const parsed = JSON.parse(raw);
        if (typeof parsed !== 'object' || parsed === null) {
            return { ...DEFAULT_DRAWER_STATE };
        }
        const isOpen = Boolean(parsed.isOpen);
        const validTabs = ['tab-strategy', 'tab-results'];
        const activeTab = validTabs.includes(parsed.activeTab) ? parsed.activeTab : 'tab-strategy';
        return { isOpen, activeTab };
    } catch (e) {
        console.warn('Lỗi khi đọc drawer state từ localStorage:', e);
        return { ...DEFAULT_DRAWER_STATE };
    }
}

function saveDrawerState(isOpen, activeTab) {
    try {
        localStorage.setItem(DRAWER_STORAGE_KEY, JSON.stringify({
            isOpen: Boolean(isOpen),
            activeTab: activeTab || 'tab-strategy'
        }));
    } catch (e) {
        console.warn('Lỗi khi lưu drawer state vào localStorage:', e);
    }
}

function triggerChartResize() {
    const chart1 = tradingChart || (typeof window !== 'undefined' ? window.tradingChart : null);
    const chart2 = secondaryChart || (typeof window !== 'undefined' ? window.secondaryChart : null);
    if (chart1 && typeof chart1.handleResize === 'function') {
        chart1.handleResize();
    }
    if (chart2 && typeof chart2.handleResize === 'function') {
        chart2.handleResize();
    }
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
        window.dispatchEvent(new Event('resize'));
    }
    setTimeout(() => {
        const c1 = tradingChart || (typeof window !== 'undefined' ? window.tradingChart : null);
        const c2 = secondaryChart || (typeof window !== 'undefined' ? window.secondaryChart : null);
        if (c1 && typeof c1.handleResize === 'function') {
            c1.handleResize();
        }
        if (c2 && typeof c2.handleResize === 'function') {
            c2.handleResize();
        }
        if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
            window.dispatchEvent(new Event('resize'));
        }
    }, 280);
}

function switchDrawerTab(targetTab) {
    const tabBtns = document.querySelectorAll('.panel-tab');
    const tabContents = document.querySelectorAll('.tab-content');
    const btnStrategy = document.getElementById('btn-toggle-strategy');
    const btnResults = document.getElementById('btn-toggle-results');
    const drawer = document.getElementById('side-panel');
    const isDrawerOpen = drawer ? !drawer.classList.contains('collapsed') : false;

    tabBtns.forEach(btn => {
        const isActive = btn.dataset.tab === targetTab;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    tabContents.forEach(content => {
        content.classList.toggle('active', content.id === targetTab);
    });

    if (isDrawerOpen) {
        btnStrategy?.classList.toggle('active', targetTab === 'tab-strategy');
        btnResults?.classList.toggle('active', targetTab === 'tab-results');
        btnStrategy?.setAttribute('aria-expanded', targetTab === 'tab-strategy' ? 'true' : 'false');
        btnResults?.setAttribute('aria-expanded', targetTab === 'tab-results' ? 'true' : 'false');
    } else {
        btnStrategy?.classList.remove('active');
        btnResults?.classList.remove('active');
        btnStrategy?.setAttribute('aria-expanded', 'false');
        btnResults?.setAttribute('aria-expanded', 'false');
    }

    saveDrawerState(isDrawerOpen, targetTab);
}

function openDrawer(tabId) {
    const drawer = document.getElementById('side-panel');
    const backdrop = document.getElementById('drawer-backdrop');
    if (!drawer) return;

    drawer.classList.remove('collapsed');
    drawer.setAttribute('aria-expanded', 'true');

    // Chỉ kích hoạt backdrop trên mobile (<768px) nơi drawer là fixed overlay
    const isMobile = (typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches)
        || (typeof window !== 'undefined' && typeof window.innerWidth === 'number' && window.innerWidth < 768);

    if (backdrop) {
        if (isMobile) {
            backdrop.style.display = 'block';
            requestAnimationFrame(() => backdrop.classList.add('active'));
        } else {
            backdrop.classList.remove('active');
            backdrop.style.display = 'none';
        }
    }

    const targetTab = tabId || getDrawerState().activeTab || 'tab-strategy';
    switchDrawerTab(targetTab);
    saveDrawerState(true, targetTab);
    triggerChartResize();
}

function closeDrawer() {
    const drawer = document.getElementById('side-panel');
    const backdrop = document.getElementById('drawer-backdrop');
    const btnStrategy = document.getElementById('btn-toggle-strategy');
    const btnResults = document.getElementById('btn-toggle-results');

    if (drawer) {
        drawer.classList.add('collapsed');
        drawer.setAttribute('aria-expanded', 'false');
    }
    if (backdrop) {
        backdrop.classList.remove('active');
        setTimeout(() => {
            if (drawer && drawer.classList.contains('collapsed')) {
                backdrop.style.display = 'none';
            }
        }, 260);
    }
    btnStrategy?.classList.remove('active');
    btnResults?.classList.remove('active');
    btnStrategy?.setAttribute('aria-expanded', 'false');
    btnResults?.setAttribute('aria-expanded', 'false');

    const state = getDrawerState();
    saveDrawerState(false, state.activeTab);
    triggerChartResize();
}

function toggleDrawer(tabId) {
    const drawer = document.getElementById('side-panel');
    if (!drawer) return;

    const isCurrentlyOpen = !drawer.classList.contains('collapsed');
    const currentActiveTab = getDrawerState().activeTab;

    if (isCurrentlyOpen && currentActiveTab === tabId) {
        closeDrawer();
    } else {
        openDrawer(tabId);
    }
}

function setupTabs() {
    setupDrawer();
}

function setupDrawer() {
    const btnStrategy = document.getElementById('btn-toggle-strategy');
    const btnResults = document.getElementById('btn-toggle-results');
    const btnClose = document.getElementById('btn-close-drawer');
    const backdrop = document.getElementById('drawer-backdrop');

    btnStrategy?.addEventListener('click', () => toggleDrawer('tab-strategy'));
    btnResults?.addEventListener('click', () => toggleDrawer('tab-results'));
    btnClose?.addEventListener('click', () => closeDrawer());
    backdrop?.addEventListener('click', () => closeDrawer());

    // Tabs inside drawer
    const tabBtns = document.querySelectorAll('.panel-tab');
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.dataset.tab;
            openDrawer(targetTab);
        });
    });

    document.getElementById('btn-clear-markers')?.addEventListener('click', () => {
        if (tradingChart) tradingChart.clearMarkers();
    });

    // Escape hotkey
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            const propOverlay = document.getElementById('property-dialog-overlay');
            if (propOverlay && propOverlay.style.display === 'flex') return;
            const ctxMenu = document.getElementById('drawing-context-menu');
            if (ctxMenu && ctxMenu.style.display === 'block') return;

            const drawer = document.getElementById('side-panel');
            if (drawer && !drawer.classList.contains('collapsed')) {
                closeDrawer();
            }
        }
    });

    // Tự động ẩn backdrop khi người dùng resize sang màn hình Desktop/Tablet
    window.addEventListener('resize', () => {
        const isMobile = (typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches)
            || (typeof window !== 'undefined' && typeof window.innerWidth === 'number' && window.innerWidth < 768);
        if (!isMobile) {
            const backdrop = document.getElementById('drawer-backdrop');
            if (backdrop) {
                backdrop.classList.remove('active');
                backdrop.style.display = 'none';
            }
        }
    });

    // Restore state from localStorage (default collapsed on first visit)
    const savedState = getDrawerState();
    if (savedState.isOpen) {
        openDrawer(savedState.activeTab);
    } else {
        closeDrawer();
        switchDrawerTab(savedState.activeTab);
    }
}

window.openDrawer = openDrawer;
window.closeDrawer = closeDrawer;
window.toggleDrawer = toggleDrawer;
window.switchDrawerTab = switchDrawerTab;
window.getDrawerState = getDrawerState;
window.saveDrawerState = saveDrawerState;

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
        if (typeof openDrawer === 'function') openDrawer('tab-strategy');
        alert(`Lỗi Backtest: ${err.message}`);
    } finally {
        hideLoading();
        if (btn) btn.disabled = false;
    }
}

// 10. Hiển thị kết quả & Đồ thị báo cáo
function displayBacktestResults(data) {
    const { metrics, trades, equity_curve, markers } = data;

    if (typeof openDrawer === 'function') {
        openDrawer('tab-results');
    } else {
        document.querySelector('[data-tab="tab-results"]')?.click();
    }

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
    if (!container) return;

    if (equityResizeObserver) {
        equityResizeObserver.disconnect();
        equityResizeObserver = null;
    }
    if (equityChart) {
        equityChart.remove();
        equityChart = null;
        equitySeries = null;
    }

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

    equityResizeObserver = new ResizeObserver(entries => {
        if (entries.length > 0 && entries[0].contentRect && equityChart) {
            const { width, height } = entries[0].contentRect;
            equityChart.applyOptions({ width, height });
        }
    });
    equityResizeObserver.observe(container);
}

function renderTradesTable(trades) {
    const tbody = document.getElementById('trades-tbody');
    if (!tbody) return;
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

// ==========================================
// 11. DRAWING TOOLS UI & INTERACTIONS (TRADINGVIEW PARITY V2)
// ==========================================

function getActiveDrawingManager() {
    return tradingChart ? tradingChart.drawingManager : null;
}

let favoriteTools = ['cursor', 'trendline', 'horizontal', 'rectangle', 'long_position', 'short_position'];
let currentEditingDrawing = null;
let currentEditingDrawingSnapshot = null;
let currentMagnetMode = 'off'; // 'off' | 'weak' | 'strong'

function updateToolbarActiveStates(activeTool) {
    // Toolbar main buttons
    document.querySelectorAll('#drawing-toolbar .draw-tool-btn[data-tool]').forEach(b => {
        b.classList.toggle('active', b.dataset.tool === activeTool);
    });

    // Flyout items
    document.querySelectorAll('.flyout-item').forEach(item => {
        item.classList.toggle('active', item.dataset.tool === activeTool);
    });

    // Favorites bar buttons
    document.querySelectorAll('.fav-tool-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tool === activeTool);
    });
}

function setupDrawingToolbar() {
    // 1. Click main button in category group
    document.querySelectorAll('.tool-group .main-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const tool = btn.dataset.tool;
            const activeDm = getActiveDrawingManager();
            if (!activeDm) return;

            // Close open flyouts
            document.querySelectorAll('.tool-flyout-menu.open').forEach(m => m.classList.remove('open'));

            activeDm.setActiveTool(tool);
            updateToolbarActiveStates(tool);
        });
    });

    // 2. Click arrow button to toggle category flyout menu
    document.querySelectorAll('.tool-group .tool-arrow-btn').forEach(arrowBtn => {
        arrowBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const group = arrowBtn.closest('.tool-group');
            const menu = group.querySelector('.tool-flyout-menu');
            if (!menu) return;

            const isOpen = menu.classList.contains('open');
            // Close other flyouts
            document.querySelectorAll('.tool-flyout-menu.open').forEach(m => m.classList.remove('open'));

            if (!isOpen) {
                const rect = group.getBoundingClientRect();
                menu.style.position = 'fixed';
                menu.style.left = `${rect.right + 2}px`;
                menu.style.top = `${rect.top}px`;
                menu.classList.add('open');
            }
        });
    });

    // Close flyouts on click outside
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.tool-group') && !e.target.closest('.tool-flyout-menu')) {
            document.querySelectorAll('.tool-flyout-menu.open').forEach(m => m.classList.remove('open'));
        }
    });

    // 3. Click items inside flyout menu
    document.querySelectorAll('.flyout-item').forEach(item => {
        item.addEventListener('click', (e) => {
            const tool = item.dataset.tool;
            const activeDm = getActiveDrawingManager();
            if (!activeDm || !tool) return;

            // If user clicked the favorite star
            if (e.target.classList.contains('tool-fav-toggle')) {
                e.stopPropagation();
                toggleFavoriteTool(tool);
                return;
            }

            // Set active tool in manager
            activeDm.setActiveTool(tool);

            // Update the parent group's main button icon, tool dataset, and title
            const group = item.closest('.tool-group');
            if (group) {
                const mainBtn = group.querySelector('.main-btn');
                const meta = window.DrawingToolRegistry?.TOOLS[tool];
                if (mainBtn && meta) {
                    mainBtn.dataset.tool = tool;
                    mainBtn.title = meta.name;
                    const iconSpan = mainBtn.querySelector('.tool-icon');
                    if (iconSpan) iconSpan.textContent = meta.icon;
                }
            }

            // Close flyout menu
            document.querySelectorAll('.tool-flyout-menu.open').forEach(m => m.classList.remove('open'));
            updateToolbarActiveStates(tool);
        });
    });

    // 4. Magnet Snap toggle: Off -> Yếu (15px) -> Mạnh (35px) -> Off
    const magnetBtn = document.getElementById('btn-draw-magnet');
    magnetBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (!activeDm) return;

        if (currentMagnetMode === 'off') {
            currentMagnetMode = 'weak';
            activeDm.magnetEnabled = true;
            activeDm.magnetMode = 'weak';
            activeDm.magnetThreshold = 15;
            magnetBtn.classList.add('active');
            magnetBtn.classList.remove('strong');
            magnetBtn.title = 'Bám Nến Tự Động: Chế độ Yếu (15px) - Click để đổi';
        } else if (currentMagnetMode === 'weak') {
            currentMagnetMode = 'strong';
            activeDm.magnetEnabled = true;
            activeDm.magnetMode = 'strong';
            activeDm.magnetThreshold = 35;
            magnetBtn.classList.add('active', 'strong');
            magnetBtn.title = 'Bám Nến Tự Động: Chế độ Mạnh (35px) - Click để tắt';
        } else {
            currentMagnetMode = 'off';
            activeDm.magnetEnabled = false;
            activeDm.magnetMode = 'off';
            activeDm.magnetThreshold = 15;
            magnetBtn.classList.remove('active', 'strong');
            magnetBtn.title = 'Bám Nến Tự Động (Magnet Snap - Đang Tắt)';
        }
    });

    // 5. Keep in Drawing Mode toggle
    const keepModeBtn = document.getElementById('btn-draw-keep-mode');
    keepModeBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (!activeDm) return;
        activeDm.keepDrawingMode = !activeDm.keepDrawingMode;
        keepModeBtn.classList.toggle('active', activeDm.keepDrawingMode);
    });

    // 6. Lock All toggle
    const lockAllBtn = document.getElementById('btn-draw-lock-all');
    lockAllBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (!activeDm || activeDm.drawings.length === 0) return;

        const hasUnlocked = activeDm.drawings.some(d => !d.locked);
        activeDm.saveUndoState();
        activeDm.drawings.forEach(d => {
            d.locked = hasUnlocked;
        });
        activeDm.saveToStorage();
        activeDm.requestRender();

        lockAllBtn.classList.toggle('active', hasUnlocked);
        const iconSpan = lockAllBtn.querySelector('.tool-icon');
        if (iconSpan) iconSpan.textContent = hasUnlocked ? '🔒' : '🔓';
    });

    // 7. Hide All toggle
    const hideAllBtn = document.getElementById('btn-draw-hide-all');
    hideAllBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (!activeDm || activeDm.drawings.length === 0) return;

        const hasVisible = activeDm.drawings.some(d => d.visible !== false && !d.hidden);
        activeDm.saveUndoState();
        activeDm.drawings.forEach(d => {
            d.visible = !hasVisible;
            d.hidden = hasVisible;
        });
        activeDm.saveToStorage();
        activeDm.requestRender();

        hideAllBtn.classList.toggle('active', hasVisible);
        const iconSpan = hideAllBtn.querySelector('.tool-icon');
        if (iconSpan) iconSpan.textContent = hasVisible ? '🙈' : '👁️';
    });

    // 8. Undo / Redo
    const undoBtn = document.getElementById('btn-draw-undo');
    undoBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (activeDm) activeDm.undo();
    });

    const redoBtn = document.getElementById('btn-draw-redo');
    redoBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (activeDm) activeDm.redo();
    });

    // 9. Delete selected
    const deleteBtn = document.getElementById('btn-draw-delete');
    deleteBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (activeDm && activeDm.selectedId) {
            activeDm.remove(activeDm.selectedId);
        }
    });

    // 10. Clear all
    const clearBtn = document.getElementById('btn-draw-clear');
    clearBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (activeDm && confirm('Xác nhận xóa tất cả các nét vẽ (trừ những nét đã khóa)?')) {
            activeDm.clear();
        }
    });

    // 11. JSON Export
    const exportBtn = document.getElementById('btn-draw-export');
    exportBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (!activeDm) return;
        const json = activeDm.exportJSON();
        const blob = new Blob([json], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `drawings_${activeDm.symbol}_${Date.now()}.json`;
        a.click();
        URL.revokeObjectURL(url);
    });

    // 12. JSON Import
    const importBtn = document.getElementById('btn-draw-import');
    importBtn?.addEventListener('click', () => {
        const activeDm = getActiveDrawingManager();
        if (!activeDm) return;
        const input = prompt('Dán nội dung JSON bản vẽ vào đây:');
        if (input) {
            const res = activeDm.importJSON(input);
            if (res.success) {
                alert(`Đã nhập thành công ${res.count} nét vẽ.`);
            } else {
                alert(`Lỗi nhập bản vẽ: ${res.error}`);
            }
        }
    });
}

// ==========================================
// FAVORITES BAR (DRAGGABLE & DYNAMIC)
// ==========================================
function setupFavoritesBar() {
    try {
        const saved = localStorage.getItem('trading_fav_tools');
        if (saved) {
            const parsed = JSON.parse(saved);
            if (Array.isArray(parsed) && parsed.length > 0) favoriteTools = parsed;
        }
    } catch (e) {}

    renderFavoritesBar();

    // Drag handle
    const bar = document.getElementById('drawing-favorites-bar');
    const handle = bar?.querySelector('.fav-drag-handle');
    if (bar && handle) {
        let isDragging = false;
        let startX = 0;
        let startY = 0;
        let origLeft = 0;
        let origTop = 0;

        // Restore saved position
        try {
            const pos = localStorage.getItem('trading_fav_bar_pos');
            if (pos) {
                const { left, top } = JSON.parse(pos);
                bar.style.left = `${left}px`;
                bar.style.top = `${top}px`;
            }
        } catch (e) {}

        handle.addEventListener('mousedown', (e) => {
            isDragging = true;
            startX = e.clientX;
            startY = e.clientY;
            origLeft = bar.offsetLeft;
            origTop = bar.offsetTop;
            document.body.style.userSelect = 'none';
        });

        window.addEventListener('mousemove', (e) => {
            if (!isDragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            const newLeft = Math.max(50, Math.min(window.innerWidth - 100, origLeft + dx));
            const newTop = Math.max(40, Math.min(window.innerHeight - 50, origTop + dy));
            bar.style.left = `${newLeft}px`;
            bar.style.top = `${newTop}px`;
        });

        window.addEventListener('mouseup', () => {
            if (!isDragging) return;
            isDragging = false;
            document.body.style.userSelect = '';
            localStorage.setItem('trading_fav_bar_pos', JSON.stringify({
                left: bar.offsetLeft,
                top: bar.offsetTop
            }));
        });
    }

    // Close button
    document.getElementById('btn-fav-close')?.addEventListener('click', () => {
        if (bar) bar.style.display = 'none';
    });
}

function renderFavoritesBar() {
    const list = document.getElementById('fav-tools-list');
    const bar = document.getElementById('drawing-favorites-bar');
    if (!list || !bar) return;

    list.innerHTML = '';
    if (favoriteTools.length === 0) {
        bar.style.display = 'none';
        return;
    }

    bar.style.display = 'flex';
    const dm = getActiveDrawingManager();
    const currentTool = dm ? dm.activeTool : 'cursor';

    favoriteTools.forEach(toolId => {
        const meta = window.DrawingToolRegistry?.TOOLS[toolId] || { name: toolId, icon: '✏️' };
        const btn = document.createElement('button');
        btn.className = `fav-tool-btn ${currentTool === toolId ? 'active' : ''}`;
        btn.dataset.tool = toolId;
        btn.title = meta.name;
        btn.innerHTML = `<span class="tool-icon">${meta.icon}</span>`;

        btn.addEventListener('click', () => {
            const activeDm = getActiveDrawingManager();
            if (activeDm) {
                activeDm.setActiveTool(toolId);
                updateToolbarActiveStates(toolId);
            }
        });

        list.appendChild(btn);
    });

    // Update stars in flyouts
    document.querySelectorAll('.flyout-item').forEach(item => {
        const tool = item.dataset.tool;
        item.classList.toggle('favorited', favoriteTools.includes(tool));
    });
}

function toggleFavoriteTool(toolId) {
    const idx = favoriteTools.indexOf(toolId);
    if (idx !== -1) {
        favoriteTools.splice(idx, 1);
    } else {
        favoriteTools.push(toolId);
    }
    localStorage.setItem('trading_fav_tools', JSON.stringify(favoriteTools));
    renderFavoritesBar();
}

// ==========================================
// MINI STYLE BAR (QUICK CONTROLS)
// ==========================================
function setupMiniStyleBar() {
    const colorPicker = document.getElementById('style-color-picker');
    const widthSelect = document.getElementById('style-width-select');
    const lineSelect = document.getElementById('style-line-select');
    const fillPicker = document.getElementById('style-fill-picker');
    const propsBtn = document.getElementById('btn-style-props');
    const lockBtn = document.getElementById('btn-style-lock');
    const hideBtn = document.getElementById('btn-style-hide');
    const deleteBtn = document.getElementById('btn-style-delete');
    const closeBtn = document.getElementById('btn-style-close');

    colorPicker?.addEventListener('input', (e) => {
        const dm = getActiveDrawingManager();
        if (!dm || !dm.selectedId) return;
        const d = dm.get(dm.selectedId);
        if (d) {
            dm.update(dm.selectedId, {
                style: { ...d.style, color: e.target.value }
            });
        }
    });

    widthSelect?.addEventListener('change', (e) => {
        const dm = getActiveDrawingManager();
        if (!dm || !dm.selectedId) return;
        const d = dm.get(dm.selectedId);
        if (d) {
            dm.update(dm.selectedId, {
                style: { ...d.style, width: parseInt(e.target.value) }
            });
        }
    });

    lineSelect?.addEventListener('change', (e) => {
        const dm = getActiveDrawingManager();
        if (!dm || !dm.selectedId) return;
        const d = dm.get(dm.selectedId);
        if (d) {
            dm.update(dm.selectedId, {
                style: { ...d.style, lineStyle: e.target.value }
            });
        }
    });

    fillPicker?.addEventListener('input', (e) => {
        const dm = getActiveDrawingManager();
        if (!dm || !dm.selectedId) return;
        const d = dm.get(dm.selectedId);
        if (d) {
            const hex = e.target.value;
            const r = parseInt(hex.slice(1, 3), 16);
            const g = parseInt(hex.slice(3, 5), 16);
            const b = parseInt(hex.slice(5, 7), 16);
            const rgba = `rgba(${r}, ${g}, ${b}, 0.2)`;
            dm.update(dm.selectedId, {
                style: { ...d.style, fillColor: rgba }
            });
        }
    });

    propsBtn?.addEventListener('click', () => {
        const dm = getActiveDrawingManager();
        if (dm && dm.selectedId) {
            openPropertyDialog(dm.get(dm.selectedId));
        }
    });

    lockBtn?.addEventListener('click', () => {
        const dm = getActiveDrawingManager();
        if (!dm || !dm.selectedId) return;
        const d = dm.get(dm.selectedId);
        if (d) {
            dm.setLocked(dm.selectedId, !d.locked);
            lockBtn.textContent = !d.locked ? '🔒' : '🔓';
            lockBtn.classList.toggle('active', !d.locked);
        }
    });

    hideBtn?.addEventListener('click', () => {
        const dm = getActiveDrawingManager();
        if (!dm || !dm.selectedId) return;
        const d = dm.get(dm.selectedId);
        if (d) {
            dm.setVisible(dm.selectedId, !d.visible);
            hideBtn.textContent = !d.visible ? '👁️' : '🙈';
        }
    });

    deleteBtn?.addEventListener('click', () => {
        const dm = getActiveDrawingManager();
        if (!dm || !dm.selectedId) return;
        dm.remove(dm.selectedId);
    });

    closeBtn?.addEventListener('click', () => {
        const dm = getActiveDrawingManager();
        if (dm) dm.deselect();
        hideStyleBar();
    });
}

// ==========================================
// PROPERTY DIALOG MODAL (5 TABS & REAL-TIME SYNC)
// ==========================================
function setupPropertyDialog() {
    const overlay = document.getElementById('property-dialog-overlay');
    const closeBtn = document.getElementById('btn-dialog-close');
    const cancelBtn = document.getElementById('btn-dialog-cancel');
    const saveBtn = document.getElementById('btn-dialog-save');

    // Tab switching
    document.querySelectorAll('.property-dialog .dialog-tab').forEach(tabBtn => {
        tabBtn.addEventListener('click', () => {
            const targetTab = tabBtn.dataset.tab;
            document.querySelectorAll('.property-dialog .dialog-tab').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.property-dialog .dialog-body .tab-content').forEach(c => c.classList.remove('active'));

            tabBtn.classList.add('active');
            const targetContent = document.getElementById(`tab-${targetTab}`);
            if (targetContent) targetContent.classList.add('active');
        });
    });

    closeBtn?.addEventListener('click', () => closePropertyDialog(true));
    cancelBtn?.addEventListener('click', () => closePropertyDialog(true));

    saveBtn?.addEventListener('click', () => {
        const dm = getActiveDrawingManager();
        if (dm && currentEditingDrawing) {
            dm.saveUndoState();
            dm.saveToStorage();
        }
        currentEditingDrawingSnapshot = null;
        closePropertyDialog(false);
    });

    // Real-time two-way synchronization listeners:
    const propColor = document.getElementById('prop-color');
    const propColorHex = document.getElementById('prop-color-hex');
    propColor?.addEventListener('input', (e) => {
        if (propColorHex) propColorHex.textContent = e.target.value;
        updateCurrentDrawingProperty({ style: { color: e.target.value } });
    });

    const propWidth = document.getElementById('prop-width');
    propWidth?.addEventListener('change', (e) => {
        updateCurrentDrawingProperty({ style: { width: parseInt(e.target.value) } });
    });

    const propLineStyle = document.getElementById('prop-line-style');
    propLineStyle?.addEventListener('change', (e) => {
        updateCurrentDrawingProperty({ style: { lineStyle: e.target.value } });
    });

    const propOpacity = document.getElementById('prop-opacity');
    const propOpacityVal = document.getElementById('prop-opacity-val');
    propOpacity?.addEventListener('input', (e) => {
        const val = parseFloat(e.target.value);
        if (propOpacityVal) propOpacityVal.textContent = `${Math.round(val * 100)}%`;
        updateCurrentDrawingProperty({ style: { opacity: val } });
    });

    const propFillColor = document.getElementById('prop-fill-color');
    const propFillHex = document.getElementById('prop-fill-hex');
    propFillColor?.addEventListener('input', (e) => {
        const hex = e.target.value;
        if (propFillHex) propFillHex.textContent = hex;
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        const rgba = `rgba(${r}, ${g}, ${b}, 0.25)`;
        updateCurrentDrawingProperty({ style: { fillColor: rgba } });
    });

    // Visibility checkboxes
    const propVisAll = document.getElementById('prop-vis-all');
    propVisAll?.addEventListener('change', (e) => {
        const allChecked = e.target.checked;
        document.querySelectorAll('.prop-tf-item').forEach(chk => {
            chk.disabled = allChecked;
        });
        updateVisibilityState();
    });

    document.querySelectorAll('.prop-tf-item').forEach(chk => {
        chk.addEventListener('change', () => updateVisibilityState());
    });

    // Text inputs
    const propText = document.getElementById('prop-text-content');
    propText?.addEventListener('input', (e) => {
        updateCurrentDrawingProperty({ text: e.target.value });
    });

    const propTextColor = document.getElementById('prop-text-color');
    propTextColor?.addEventListener('input', (e) => {
        updateCurrentDrawingProperty({ style: { textColor: e.target.value } });
    });

    const propFontSize = document.getElementById('prop-font-size');
    propFontSize?.addEventListener('change', (e) => {
        updateCurrentDrawingProperty({ style: { fontSize: parseInt(e.target.value) } });
    });

    // Stats show checkbox
    const propStatsShow = document.getElementById('prop-stats-show');
    propStatsShow?.addEventListener('change', (e) => {
        updateCurrentDrawingProperty({ stats: { showBadge: e.target.checked } });
    });
}

function updateCurrentDrawingProperty(partial) {
    const dm = getActiveDrawingManager();
    if (!dm || !currentEditingDrawing) return;

    const d = dm.get(currentEditingDrawing.id);
    if (!d) return;

    const updateObj = {};
    if (partial.style) updateObj.style = { ...d.style, ...partial.style };
    if (partial.text !== undefined) updateObj.text = partial.text;
    if (partial.visibility) updateObj.visibility = { ...d.visibility, ...partial.visibility };
    if (partial.stats) updateObj.stats = { ...d.stats, ...partial.stats };
    if (partial.points) updateObj.points = partial.points;

    dm.update(currentEditingDrawing.id, updateObj);
}

function updateVisibilityState() {
    const visAll = document.getElementById('prop-vis-all')?.checked ?? true;
    const selectedTfs = [];
    document.querySelectorAll('.prop-tf-item:checked').forEach(chk => {
        selectedTfs.push(chk.value);
    });

    updateCurrentDrawingProperty({
        visibility: {
            allTimeframes: visAll,
            timeframes: selectedTfs
        }
    });
}

function openPropertyDialog(drawing) {
    if (!drawing) return;
    currentEditingDrawing = drawing;
    currentEditingDrawingSnapshot = JSON.parse(JSON.stringify(drawing));

    const overlay = document.getElementById('property-dialog-overlay');
    if (!overlay) return;

    const meta = window.DrawingToolRegistry?.TOOLS[drawing.type] || { name: drawing.type.toUpperCase() };
    const title = document.getElementById('dialog-tool-title');
    if (title) title.textContent = `⚙️ Cài đặt: ${meta.name}`;

    // Reset tabs to Style
    document.querySelectorAll('.property-dialog .dialog-tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.property-dialog .dialog-body .tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('.property-dialog .dialog-tab[data-tab="style"]')?.classList.add('active');
    document.getElementById('tab-style')?.classList.add('active');

    // 1. Populate Style tab
    const propColor = document.getElementById('prop-color');
    const propColorHex = document.getElementById('prop-color-hex');
    const col = drawing.style?.color || '#2962ff';
    if (propColor) propColor.value = col;
    if (propColorHex) propColorHex.textContent = col;

    const propWidth = document.getElementById('prop-width');
    if (propWidth) propWidth.value = drawing.style?.width || 2;

    const propLineStyle = document.getElementById('prop-line-style');
    if (propLineStyle) propLineStyle.value = drawing.style?.lineStyle || 'solid';

    const propOpacity = document.getElementById('prop-opacity');
    const propOpacityVal = document.getElementById('prop-opacity-val');
    const op = drawing.style?.opacity ?? 1;
    if (propOpacity) propOpacity.value = op;
    if (propOpacityVal) propOpacityVal.textContent = `${Math.round(op * 100)}%`;

    const fillRow = document.getElementById('row-prop-fill');
    const hasFill = ['rectangle', 'circle', 'triangle', 'channel', 'fib_retracement', 'gann_box', 'ruler', 'date_price_range', 'long_position', 'short_position'].includes(drawing.type);
    if (fillRow) fillRow.style.display = hasFill ? 'flex' : 'none';

    // 2. Populate Coordinates tab
    renderCoordinatesInputs(drawing);

    // 3. Populate Visibility tab
    const propVisAll = document.getElementById('prop-vis-all');
    const isAll = drawing.visibility?.allTimeframes ?? true;
    if (propVisAll) propVisAll.checked = isAll;

    const curTfs = drawing.visibility?.timeframes || ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1'];
    document.querySelectorAll('.prop-tf-item').forEach(chk => {
        chk.checked = curTfs.includes(chk.value);
        chk.disabled = isAll;
    });

    // 4. Populate Statistics tab
    renderStatisticsDetails(drawing);

    // 5. Populate Text tab
    const propText = document.getElementById('prop-text-content');
    if (propText) propText.value = drawing.text || '';

    const propTextColor = document.getElementById('prop-text-color');
    if (propTextColor) propTextColor.value = drawing.style?.textColor || '#ffffff';

    const propFontSize = document.getElementById('prop-font-size');
    if (propFontSize) propFontSize.value = drawing.style?.fontSize || 14;

    overlay.style.display = 'flex';
}

function closePropertyDialog(shouldRollback = false) {
    const overlay = document.getElementById('property-dialog-overlay');
    if (overlay) overlay.style.display = 'none';

    if (shouldRollback && currentEditingDrawingSnapshot) {
        const dm = getActiveDrawingManager();
        if (dm) {
            dm.update(currentEditingDrawingSnapshot.id, currentEditingDrawingSnapshot);
        }
    }
    currentEditingDrawing = null;
    currentEditingDrawingSnapshot = null;
}

function renderCoordinatesInputs(drawing) {
    const container = document.getElementById('prop-coordinates-container');
    if (!container) return;
    container.innerHTML = '';

    drawing.points.forEach((pt, idx) => {
        const row = document.createElement('div');
        row.className = 'coord-point-row';

        const d = new Date(pt.time * 1000);
        const timeStr = `${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;

        row.innerHTML = `
            <div class="coord-point-title">Điểm neo ${idx + 1}</div>
            <div class="coord-inputs">
                <div class="coord-field">
                    <label>Mức giá:</label>
                    <input type="number" step="0.01" class="coord-price-input" data-idx="${idx}" value="${pt.price}">
                </div>
                <div class="coord-field">
                    <label>Thời gian (UTC):</label>
                    <input type="text" readonly value="${timeStr}" style="opacity: 0.8; cursor: default;">
                </div>
            </div>
        `;

        const priceInput = row.querySelector('.coord-price-input');
        priceInput?.addEventListener('change', (e) => {
            const newPrice = parseFloat(e.target.value);
            if (!isNaN(newPrice)) {
                const newPoints = drawing.points.map((p, i) => i === idx ? { ...p, price: newPrice } : p);
                updateCurrentDrawingProperty({ points: newPoints });
            }
        });

        container.appendChild(row);
    });
}

function renderStatisticsDetails(drawing) {
    const details = document.getElementById('prop-stats-details');
    if (!details) return;

    let html = '';
    const pts = drawing.points;
    const geom = window.DrawingGeometry;

    if ((drawing.type === 'long_position' || drawing.type === 'short_position') && pts.length >= 2 && geom?.calculatePositionRiskReward) {
        const isLong = drawing.type === 'long_position';
        const entry = pts[0].price;
        const stop = pts[1].price;
        const target = pts.length >= 3 ? pts[2].price : (isLong ? entry + Math.abs(stop - entry) * 2 : entry - Math.abs(stop - entry) * 2);

        // Lấy cấu hình backtest hiện tại từ giao diện
        const lot = parseFloat(document.getElementById('lot-size')?.value) || 0.1;
        const spreadPoints = parseFloat(document.getElementById('spread-points')?.value) || 20;
        const spread = spreadPoints / 100.0;
        const commPerLot = parseFloat(document.getElementById('commission')?.value) || 5;
        const commission = (commPerLot * 2) * lot;
        const contractSize = 100.0;
        const pointSize = 0.1;

        const res = geom.calculatePositionRiskReward(
            entry,
            stop,
            target,
            isLong,
            lot,
            spread,
            commission,
            contractSize,
            pointSize
        );

        html = `
            <div class="stat-item-row"><span class="stat-item-label">Loại vị thế:</span> <span class="stat-item-val">${isLong ? 'MUA (Long)' : 'BÁN (Short)'}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Giá vào lệnh (Entry):</span> <span class="stat-item-val">${entry.toFixed(2)}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Cắt lỗ (Stop Loss):</span> <span class="stat-item-val">${stop.toFixed(2)} (${res.riskPips} pips)</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Chốt lời (Take Profit):</span> <span class="stat-item-val">${target.toFixed(2)} (${res.rewardPips} pips)</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Tỷ lệ R:R:</span> <span class="stat-item-val" style="color: var(--color-blue); font-size: 13px;">1 : ${res.riskRewardRatio}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Rủi ro (Risk $):</span> <span class="stat-item-val" style="color: var(--color-red);">$${res.riskAmount}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Lợi nhuận dự kiến (Reward $):</span> <span class="stat-item-val" style="color: var(--color-green);">$${res.rewardAmount}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Lợi nhuận ròng (Net Target PnL):</span> <span class="stat-item-val val-up">+$${res.targetPnL}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Tổn thất ròng (Net Stop PnL):</span> <span class="stat-item-val val-down">-$${Math.abs(res.stopPnL)}</span></div>
        `;
    } else if ((drawing.type === 'date_price_range' || drawing.type === 'ruler') && pts.length >= 2 && geom?.calculateDatePriceRange) {
        const p1 = pts[0].price;
        const p2 = pts[1].price;
        const t1 = pts[0].time;
        const t2 = pts[1].time;

        let candleCount = 0;
        let volumeSum = 0;
        const chartObj = typeof tradingChart !== 'undefined' ? tradingChart : null;
        const candles = chartObj?.currentCandles;
        if (Array.isArray(candles) && candles.length > 0) {
            const minT = Math.min(t1, t2);
            const maxT = Math.max(t1, t2);
            const inRange = candles.filter(c => c.time >= minT && c.time <= maxT);
            candleCount = inRange.length;
            volumeSum = inRange.reduce((acc, c) => acc + (c.volume || c.tick_volume || 0), 0);
        }

        const res = geom.calculateDatePriceRange(
            p1,
            p2,
            t1,
            t2,
            candleCount,
            volumeSum
        );

        html = `
            <div class="stat-item-row"><span class="stat-item-label">Biên độ giá (Delta):</span> <span class="stat-item-val ${res.deltaPrice >= 0 ? 'val-up' : 'val-down'}">${res.deltaPrice >= 0 ? '+' : ''}${res.deltaPrice.toFixed(2)}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Phần trăm (%):</span> <span class="stat-item-val ${res.percentChange >= 0 ? 'val-up' : 'val-down'}">${res.percentChange >= 0 ? '+' : ''}${res.percentChange}%</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Khoảng cách Pips:</span> <span class="stat-item-val">${res.pips} pips</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Số nến:</span> <span class="stat-item-val">${res.candleCount} nến</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Thời gian:</span> <span class="stat-item-val">${res.durationFormatted}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Tổng khối lượng (Volume):</span> <span class="stat-item-val">${res.volumeSum > 0 ? res.volumeSum.toLocaleString() : 'N/A'}</span></div>
        `;
    } else if (drawing.type === 'trend_angle' && pts.length >= 2 && geom?.calculateTrendAngle) {
        const res = geom.calculateTrendAngle(pts[0], pts[1]);
        html = `
            <div class="stat-item-row"><span class="stat-item-label">Góc dốc:</span> <span class="stat-item-val" style="color: var(--accent-gold); font-size: 13px;">${res.angleDeg}°</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Độ dốc (Slope):</span> <span class="stat-item-val">${res.slope !== 0 && isFinite(res.slope) ? res.slope + ' USD/sec' : (isFinite(res.slope) ? '0 USD/sec' : 'Thẳng đứng (Infinity)')}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Biên độ giá:</span> <span class="stat-item-val ${res.deltaPrice >= 0 ? 'val-up' : 'val-down'}">${res.deltaPrice >= 0 ? '+' : ''}${res.deltaPrice.toFixed(2)}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Khoảng thời gian:</span> <span class="stat-item-val">${Math.abs(res.deltaTime)} giây</span></div>
        `;
    } else if (pts.length >= 2) {
        const delta = pts[pts.length - 1].price - pts[0].price;
        const pips = (Math.abs(delta) / 0.1).toFixed(1);
        html = `
            <div class="stat-item-row"><span class="stat-item-label">Biên độ giá:</span> <span class="stat-item-val ${delta >= 0 ? 'val-up' : 'val-down'}">${delta >= 0 ? '+' : ''}${delta.toFixed(2)}</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Khoảng cách Pips:</span> <span class="stat-item-val">${pips} pips</span></div>
            <div class="stat-item-row"><span class="stat-item-label">Số điểm neo:</span> <span class="stat-item-val">${pts.length} điểm</span></div>
        `;
    } else {
        html = `<p class="tab-tip">Không có thông số đo lường bổ sung cho công cụ này.</p>`;
    }

    details.innerHTML = html;
}

// ==========================================
// CONTEXT MENU (RIGHT-CLICK ON DRAWING)
// ==========================================
function setupContextMenu() {
    const menu = document.getElementById('drawing-context-menu');
    if (!menu) return;

    // Context menu actions
    menu.querySelectorAll('.ctx-item').forEach(item => {
        item.addEventListener('click', (e) => {
            const action = item.dataset.action;
            const dm = getActiveDrawingManager();
            if (!dm || !dm.selectedId) {
                hideContextMenu();
                return;
            }

            const id = dm.selectedId;
            const d = dm.get(id);

            switch (action) {
                case 'settings':
                    if (d) openPropertyDialog(d);
                    break;
                case 'duplicate':
                    dm.duplicate(id);
                    break;
                case 'toggle-lock':
                    if (d) dm.setLocked(id, !d.locked);
                    break;
                case 'toggle-hide':
                    if (d) dm.setVisible(id, !d.visible);
                    break;
                case 'bring-front':
                    dm.bringToFront(id);
                    break;
                case 'send-back':
                    dm.sendToBack(id);
                    break;
                case 'add-alert':
                    if (d) {
                        if (!d.alert) d.alert = { enabled: false, type: 'price_touch', triggered: false };
                        d.alert.enabled = !d.alert.enabled;
                        d.alert.triggered = false;
                        dm.saveToStorage();
                        showDrawingToast(`Đã ${d.alert.enabled ? 'bật 🔔' : 'tắt 🔕'} cảnh báo giá cho ${d.type.toUpperCase()}`);
                    }
                    break;
                case 'delete':
                    dm.remove(id);
                    break;
            }

            hideContextMenu();
        });
    });

    // Close on click outside
    document.addEventListener('click', (e) => {
        if (!e.target.closest('#drawing-context-menu')) {
            hideContextMenu();
        }
    });

    window.addEventListener('resize', hideContextMenu);
}

function showContextMenu(drawing, event) {
    const menu = document.getElementById('drawing-context-menu');
    if (!menu) return;

    // Position menu constrained to viewport
    const x = Math.min(event.clientX, window.innerWidth - 240);
    const y = Math.min(event.clientY, window.innerHeight - 260);

    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;

    // Update dynamic texts
    const lockItem = menu.querySelector('[data-action="toggle-lock"]');
    if (lockItem) {
        lockItem.innerHTML = `<span class="ctx-icon">${drawing.locked ? '🔓' : '🔒'}</span> ${drawing.locked ? 'Mở khóa nét vẽ' : 'Khóa nét vẽ'}`;
    }

    const hideItem = menu.querySelector('[data-action="toggle-hide"]');
    if (hideItem) {
        hideItem.innerHTML = `<span class="ctx-icon">${drawing.visible ? '👁️' : '🙈'}</span> ${drawing.visible ? 'Ẩn nét vẽ' : 'Hiện nét vẽ'}`;
    }

    const alertItem = menu.querySelector('[data-action="add-alert"]');
    if (alertItem) {
        alertItem.innerHTML = `<span class="ctx-icon">${drawing.alert?.enabled ? '🔕' : '🔔'}</span> ${drawing.alert?.enabled ? 'Tắt cảnh báo giá' : 'Bật cảnh báo giá'}`;
    }

    menu.style.display = 'block';
}

function hideContextMenu() {
    const menu = document.getElementById('drawing-context-menu');
    if (menu) menu.style.display = 'none';
}

// ==========================================
// ALERT TOASTS & AUDIO CHIMES
// ==========================================
let toastTimer = null;

function setupAlertToasts() {
    // Handled via window.onDrawingAlertTriggered
}

function playAlertChime() {
    try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) return;
        const ctx = new AudioContext();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.type = 'sine';
        osc.connect(gain);
        gain.connect(ctx.destination);

        const now = ctx.currentTime;
        osc.frequency.setValueAtTime(587.33, now); // D5
        osc.frequency.setValueAtTime(880.00, now + 0.12); // A5

        gain.gain.setValueAtTime(0.18, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.4);

        osc.start(now);
        osc.stop(now + 0.4);
    } catch (e) {}
}

function showDrawingAlertToast(alertData) {
    const toast = document.getElementById('drawing-alert-toast');
    const msg = document.getElementById('toast-msg');
    if (!toast || !msg) return;

    try {
        playAlertChime();
    } catch (e) {
        console.warn('Audio alert error or blocked:', e);
    }
    const type = alertData.drawing?.type?.toUpperCase() || 'DRAWING';
    const price = alertData.price ?? (alertData.candle?.close ?? alertData.candle?.open);
    msg.textContent = `Giá nến (${price}) vừa chạm đường cảnh báo [${type}]!`;

    toast.style.display = 'flex';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.style.display = 'none';
    }, 4500);
}

function showDrawingToast(message) {
    const toast = document.getElementById('drawing-alert-toast');
    const msg = document.getElementById('toast-msg');
    if (!toast || !msg) return;

    msg.textContent = message;
    toast.style.display = 'flex';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.style.display = 'none';
    }, 3500);
}

// ==========================================
// DRAWING HOTKEYS (TV PARITY)
// ==========================================
function setupDrawingHotkeys() {
    window.addEventListener('keydown', (e) => {
        const tag = document.activeElement ? document.activeElement.tagName.toLowerCase() : '';
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

        const dm = getActiveDrawingManager();
        if (!dm) return;

        // Escape: Hủy vẽ dở hoặc bỏ chọn nét vẽ
        if (e.key === 'Escape') {
            if (dm.isDrawing) {
                dm.isDrawing = false;
                dm.inProgressPoints = [];
                dm.clearPreview();
            }
            dm.deselect();
            dm.setActiveTool('cursor');
            updateToolbarActiveStates('cursor');
            closePropertyDialog();
            hideContextMenu();
            return;
        }

        // Ctrl+D / Cmd+D: Duplicate drawing đang chọn
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'd') {
            if (dm.selectedId) {
                e.preventDefault();
                dm.duplicate(dm.selectedId);
            }
            return;
        }

        // Delete hoặc Backspace: Xóa nét vẽ đang chọn
        if (e.key === 'Delete' || e.key === 'Backspace') {
            if (dm.selectedId) {
                dm.remove(dm.selectedId);
                e.preventDefault();
            }
            return;
        }

        // Ctrl+Z / Cmd+Z: Undo
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
            if (e.shiftKey) {
                dm.redo();
            } else {
                dm.undo();
            }
            e.preventDefault();
            return;
        }

        // Ctrl+Y: Redo
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
            dm.redo();
            e.preventDefault();
            return;
        }
    });
}

// ==========================================
// GLOBAL CALLBACK HOOKS FOR DRAWING MANAGER
// ==========================================
window.onDrawingStateChange = function(state, chartInstance) {
    if (chartInstance !== tradingChart) return;

    const undoBtn = document.getElementById('btn-draw-undo');
    const redoBtn = document.getElementById('btn-draw-redo');
    const deleteBtn = document.getElementById('btn-draw-delete');

    if (undoBtn) undoBtn.disabled = !state.canUndo;
    if (redoBtn) redoBtn.disabled = !state.canRedo;
    if (deleteBtn) deleteBtn.disabled = !state.selectedId;

    updateToolbarActiveStates(state.activeTool);

    if (state.selectedDrawing) {
        showStyleBar(state.selectedDrawing);
    } else {
        hideStyleBar();
    }
};

function showStyleBar(d) {
    const styleBar = document.getElementById('drawing-style-bar');
    if (!styleBar || !d) return;

    styleBar.style.display = 'flex';

    const title = document.getElementById('style-bar-title');
    if (title) title.textContent = d.type ? d.type.toUpperCase() : 'NÉT VẼ';

    const colorPicker = document.getElementById('style-color-picker');
    if (colorPicker && d.style && d.style.color) colorPicker.value = d.style.color;

    const widthSelect = document.getElementById('style-width-select');
    if (widthSelect && d.style && d.style.width) widthSelect.value = d.style.width;

    const lineSelect = document.getElementById('style-line-select');
    if (lineSelect && d.style && d.style.lineStyle) lineSelect.value = d.style.lineStyle;

    const lockBtn = document.getElementById('btn-style-lock');
    if (lockBtn) {
        lockBtn.textContent = d.locked ? '🔒' : '🔓';
        lockBtn.classList.toggle('active', !!d.locked);
    }

    const hideBtn = document.getElementById('btn-style-hide');
    if (hideBtn) {
        hideBtn.textContent = d.visible ? '👁️' : '🙈';
    }

    const fillContainer = document.getElementById('style-fill-container');
    if (fillContainer) {
        const hasFill = ['rectangle', 'circle', 'triangle', 'channel', 'fib_retracement', 'gann_box', 'ruler', 'date_price_range', 'long_position', 'short_position'].includes(d.type);
        fillContainer.style.display = hasFill ? 'flex' : 'none';
    }
}

function hideStyleBar() {
    const styleBar = document.getElementById('drawing-style-bar');
    if (styleBar) {
        styleBar.style.display = 'none';
    }
}

window.showStyleBar = showStyleBar;
window.hideStyleBar = hideStyleBar;

window.onDrawingOpenProperties = function(drawing, chartInstance) {
    openPropertyDialog(drawing);
};

window.onDrawingContextMenu = function(drawing, event, chartInstance) {
    showContextMenu(drawing, event);
};

window.onDrawingAlertTriggered = function(alertData, chartInstance) {
    showDrawingAlertToast(alertData);
};
