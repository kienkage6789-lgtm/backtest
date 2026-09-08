// tests/test_drawer.test.js
// Unit test suite for Collapsible Strategy & Results Drawer (T47)
// Runs with: node --test tests/test_drawer.test.js

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML_PATH = path.join(__dirname, '..', 'public', 'index.html');
const APP_JS_PATH = path.join(__dirname, '..', 'public', 'app.js');

const htmlContent = fs.readFileSync(HTML_PATH, 'utf8');
const appJsContent = fs.readFileSync(APP_JS_PATH, 'utf8');

// Helper to create a lightweight mock DOM element
class MockClassList {
    constructor(classes = []) {
        this.classes = new Set(classes);
    }
    add(...cls) { cls.forEach(c => this.classes.add(c)); }
    remove(...cls) { cls.forEach(c => this.classes.delete(c)); }
    contains(cls) { return this.classes.has(cls); }
    toggle(cls, force) {
        if (force === undefined) {
            if (this.classes.has(cls)) { this.classes.delete(cls); return false; }
            else { this.classes.add(cls); return true; }
        } else if (force) {
            this.classes.add(cls);
            return true;
        } else {
            this.classes.delete(cls);
            return false;
        }
    }
    toString() { return Array.from(this.classes).join(' '); }
}

class MockElement {
    constructor({ id = '', tagName = 'div', className = '', dataset = {}, attributes = {} } = {}) {
        this.id = id;
        this.tagName = tagName.toUpperCase();
        this.classList = new MockClassList(className ? className.split(/\s+/).filter(Boolean) : []);
        this.dataset = { ...dataset };
        this.attributes = { ...attributes };
        this.style = {};
        this.listeners = {};
        this.children = [];
        this.value = '';
        this.checked = false;
        this.textContent = '';
        this.innerHTML = '';
        this.disabled = false;
    }

    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return this.attributes[k] !== undefined ? this.attributes[k] : null; }
    hasAttribute(k) { return this.attributes[k] !== undefined; }
    removeAttribute(k) { delete this.attributes[k]; }

    addEventListener(event, handler) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(handler);
    }

    removeEventListener(event, handler) {
        if (!this.listeners[event]) return;
        this.listeners[event] = this.listeners[event].filter(h => h !== handler);
    }

    dispatchEvent(event) {
        const list = this.listeners[event.type] || [];
        for (const fn of list) {
            fn.call(this, event);
        }
    }

    click() {
        this.dispatchEvent({ type: 'click', target: this, preventDefault: () => {} });
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }
}

class MockLocalStorage {
    constructor() {
        this.store = {};
        this.shouldThrow = false;
    }
    getItem(k) {
        if (this.shouldThrow) throw new Error('QuotaExceededError / Storage Restricted');
        return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null;
    }
    setItem(k, v) {
        if (this.shouldThrow) throw new Error('QuotaExceededError / Storage Restricted');
        this.store[k] = String(v);
    }
    removeItem(k) {
        if (this.shouldThrow) throw new Error('QuotaExceededError / Storage Restricted');
        delete this.store[k];
    }
    clear() {
        if (this.shouldThrow) throw new Error('QuotaExceededError / Storage Restricted');
        this.store = {};
    }
}

// Setup a fresh DOM harness containing all elements required by drawer & backtest
function createMockEnvironment(initialStorage = {}, options = {}) {
    const storage = new MockLocalStorage();
    for (const [k, v] of Object.entries(initialStorage)) {
        storage.setItem(k, v);
    }

    const currentWidth = options.innerWidth !== undefined ? options.innerWidth : 1920;

    const elementsById = new Map();
    const allElements = [];

    function register(el) {
        if (el.id) elementsById.set(el.id, el);
        allElements.push(el);
        return el;
    }

    // Header controls
    const btnToggleStrategy = register(new MockElement({
        id: 'btn-toggle-strategy',
        tagName: 'button',
        className: 'btn-header',
        attributes: { 'aria-expanded': 'false', 'aria-controls': 'side-panel' }
    }));

    const btnToggleResults = register(new MockElement({
        id: 'btn-toggle-results',
        tagName: 'button',
        className: 'btn-header',
        attributes: { 'aria-expanded': 'false', 'aria-controls': 'side-panel' }
    }));

    const drawerBackdrop = register(new MockElement({
        id: 'drawer-backdrop',
        tagName: 'div',
        className: 'drawer-backdrop'
    }));

    // Drawer container & internal tabs
    const sidePanel = register(new MockElement({
        id: 'side-panel',
        tagName: 'aside',
        className: 'side-panel collapsed',
        attributes: { 'role': 'region', 'aria-label': 'Bảng điều khiển chiến lược và kết quả', 'aria-expanded': 'false' }
    }));

    const tabBtnStrategy = register(new MockElement({
        tagName: 'button',
        className: 'panel-tab active',
        dataset: { tab: 'tab-strategy' },
        attributes: { 'role': 'tab', 'aria-selected': 'true', 'aria-controls': 'tab-strategy' }
    }));

    const tabBtnResults = register(new MockElement({
        tagName: 'button',
        className: 'panel-tab',
        dataset: { tab: 'tab-results' },
        attributes: { 'role': 'tab', 'aria-selected': 'false', 'aria-controls': 'tab-results' }
    }));

    const btnCloseDrawer = register(new MockElement({
        id: 'btn-close-drawer',
        tagName: 'button',
        className: 'panel-close-btn',
        attributes: { 'aria-label': 'Đóng bảng điều khiển' }
    }));

    const tabContentStrategy = register(new MockElement({
        id: 'tab-strategy',
        tagName: 'div',
        className: 'tab-content active',
        attributes: { 'role': 'tabpanel', 'aria-labelledby': 'tab-btn-strategy' }
    }));

    const tabContentResults = register(new MockElement({
        id: 'tab-results',
        tagName: 'div',
        className: 'tab-content',
        attributes: { 'role': 'tabpanel', 'aria-labelledby': 'tab-btn-results' }
    }));

    // Property dialog and context menu for Escape key isolation
    const propertyDialogOverlay = register(new MockElement({
        id: 'property-dialog-overlay',
        tagName: 'div',
        className: 'property-dialog-overlay'
    }));
    propertyDialogOverlay.style.display = 'none';

    const drawingContextMenu = register(new MockElement({
        id: 'drawing-context-menu',
        tagName: 'div',
        className: 'drawing-context-menu'
    }));
    drawingContextMenu.style.display = 'none';

    // Strategy form inputs
    const strategySelect = register(new MockElement({ id: 'strategy-select', tagName: 'select' }));
    strategySelect.value = 'sma_cross';
    const initialCapital = register(new MockElement({ id: 'initial-capital', tagName: 'input' }));
    initialCapital.value = '10000';
    const lotSize = register(new MockElement({ id: 'lot-size', tagName: 'input' }));
    lotSize.value = '0.1';
    const stopLoss = register(new MockElement({ id: 'stop-loss', tagName: 'input' }));
    stopLoss.value = '50';
    const takeProfit = register(new MockElement({ id: 'take-profit', tagName: 'input' }));
    takeProfit.value = '100';
    const spreadPoints = register(new MockElement({ id: 'spread-points', tagName: 'input' }));
    spreadPoints.value = '20';
    const commission = register(new MockElement({ id: 'commission', tagName: 'input' }));
    commission.value = '5';
    const candlesLimit = register(new MockElement({ id: 'candles-limit', tagName: 'input' }));
    candlesLimit.value = '1000';
    const allowShort = register(new MockElement({ id: 'allow-short', tagName: 'input' }));
    allowShort.checked = true;

    // Results elements
    const resultsEmpty = register(new MockElement({ id: 'results-empty', tagName: 'div' }));
    const resultsContent = register(new MockElement({ id: 'results-content', tagName: 'div' }));
    const netPnl = register(new MockElement({ id: 'm-net-pnl', tagName: 'div' }));
    const returnPct = register(new MockElement({ id: 'm-return-pct', tagName: 'div' }));
    const winRate = register(new MockElement({ id: 'm-win-rate', tagName: 'div' }));
    const profitFactor = register(new MockElement({ id: 'm-profit-factor', tagName: 'div' }));
    const tradesRatio = register(new MockElement({ id: 'm-trades-ratio', tagName: 'div' }));
    const maxDd = register(new MockElement({ id: 'm-max-dd', tagName: 'div' }));
    const tradesTbody = register(new MockElement({ id: 'trades-tbody', tagName: 'tbody' }));
    const equityChartEl = register(new MockElement({ id: 'equity-chart', tagName: 'div' }));

    // Chart resize spy
    let chartResizeCalls = 0;
    const mockTradingChart = {
        handleResize: () => { chartResizeCalls++; },
        setMarkers: () => {},
        clearMarkers: () => {}
    };

    const windowListeners = {};
    const mockWindow = {
        innerWidth: currentWidth,
        addEventListener: (event, handler) => {
            if (!windowListeners[event]) windowListeners[event] = [];
            windowListeners[event].push(handler);
        },
        removeEventListener: (event, handler) => {
            if (!windowListeners[event]) return;
            windowListeners[event] = windowListeners[event].filter(h => h !== handler);
        },
        dispatchEvent: (event) => {
            const list = windowListeners[event.type] || [];
            for (const fn of list) fn(event);
        }
    };

    const mockDocument = {
        getElementById: (id) => elementsById.get(id) || null,
        querySelector: (sel) => {
            if (sel.startsWith('#')) return elementsById.get(sel.slice(1)) || null;
            if (sel === '[data-tab="tab-strategy"]') return tabBtnStrategy;
            if (sel === '[data-tab="tab-results"]') return tabBtnResults;
            return null;
        },
        querySelectorAll: (sel) => {
            if (sel === '.panel-tab') return [tabBtnStrategy, tabBtnResults];
            if (sel === '.tab-content') return [tabContentStrategy, tabContentResults];
            if (sel === '.param-input') return [];
            return [];
        },
        addEventListener: () => {}
    };

    const context = {
        window: null,
        innerWidth: currentWidth,
        document: mockDocument,
        localStorage: storage,
        tradingChart: mockTradingChart,
        secondaryChart: null,
        addEventListener: (e, h) => mockWindow.addEventListener(e, h),
        removeEventListener: (e, h) => mockWindow.removeEventListener(e, h),
        dispatchEvent: (e) => mockWindow.dispatchEvent(e),
        setTimeout: (cb, ms) => { cb(); return 1; },
        clearTimeout: () => {},
        setInterval: () => 1,
        clearInterval: () => {},
        requestAnimationFrame: (cb) => { cb(); return 1; },
        Event: class Event { constructor(type) { this.type = type; } },
        console: {
            warn: () => {},
            error: () => {},
            log: () => {}
        },
        alert: () => {},
        fetch: async () => ({ ok: true, json: async () => ({}) })
    };
    context.window = context;

    vm.runInNewContext(appJsContent, context);

    return {
        context,
        storage,
        getChartResizeCalls: () => chartResizeCalls,
        elements: {
            btnToggleStrategy,
            btnToggleResults,
            drawerBackdrop,
            sidePanel,
            tabBtnStrategy,
            tabBtnResults,
            btnCloseDrawer,
            tabContentStrategy,
            tabContentResults,
            propertyDialogOverlay,
            drawingContextMenu,
            strategySelect,
            initialCapital,
            lotSize,
            stopLoss,
            takeProfit,
            spreadPoints,
            commission,
            candlesLimit,
            allowShort,
            resultsEmpty,
            resultsContent,
            netPnl,
            returnPct,
            winRate,
            profitFactor,
            tradesRatio,
            maxDd
        }
    };
}

// ==========================================
// TEST CASES
// ==========================================

test('T47 Drawer: Trạng thái mặc định khi mới mở ứng dụng (Không có localStorage)', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;

    // Chạy setupDrawer()
    context.setupDrawer();

    // Drawer phải ở trạng thái đóng (collapsed) để chart chiếm toàn bộ diện tích
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true, 'Drawer phải có class collapsed khi khởi động');
    assert.equal(elements.sidePanel.getAttribute('aria-expanded'), 'false', 'Drawer aria-expanded phải là false');

    // Header toggle buttons không active
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), false, 'Nút Cấu hình không active');
    assert.equal(elements.btnToggleResults.classList.contains('active'), false, 'Nút Kết quả không active');
    assert.equal(elements.btnToggleStrategy.getAttribute('aria-expanded'), 'false');
    assert.equal(elements.btnToggleResults.getAttribute('aria-expanded'), 'false');

    // State lưu phải là default
    const state = context.getDrawerState();
    assert.equal(state.isOpen, false);
    assert.equal(state.activeTab, 'tab-strategy');
});

test('T47 Drawer: Bật Cấu hình chiến lược bằng nút Header (#btn-toggle-strategy)', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;
    context.setupDrawer();

    // Click nút Cấu hình chiến lược
    elements.btnToggleStrategy.click();

    // Drawer phải mở ra
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false, 'Drawer phải bỏ class collapsed khi mở');
    assert.equal(elements.sidePanel.getAttribute('aria-expanded'), 'true');

    // Nút Header Strategy active, Results inactive
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), true);
    assert.equal(elements.btnToggleStrategy.getAttribute('aria-expanded'), 'true');
    assert.equal(elements.btnToggleResults.classList.contains('active'), false);
    assert.equal(elements.btnToggleResults.getAttribute('aria-expanded'), 'false');

    // Tab Cấu hình active
    assert.equal(elements.tabContentStrategy.classList.contains('active'), true);
    assert.equal(elements.tabBtnStrategy.classList.contains('active'), true);
    assert.equal(elements.tabBtnStrategy.getAttribute('aria-selected'), 'true');

    // Chart resize được gọi
    assert.ok(env.getChartResizeCalls() > 0, 'Chart handleResize phải được gọi khi mở drawer');

    // Click lại nút Cấu hình chiến lược -> Đóng drawer
    elements.btnToggleStrategy.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true, 'Drawer phải đóng lại khi click toggle lần 2');
    assert.equal(elements.sidePanel.getAttribute('aria-expanded'), 'false');
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), false);
});

test('T47 Drawer: Bật Kết quả & Báo cáo bằng nút Header (#btn-toggle-results)', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;
    context.setupDrawer();

    // Click nút Kết quả & Báo cáo từ trạng thái đóng
    elements.btnToggleResults.click();

    // Drawer mở, tab Results active
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);
    assert.equal(elements.sidePanel.getAttribute('aria-expanded'), 'true');
    assert.equal(elements.btnToggleResults.classList.contains('active'), true);
    assert.equal(elements.btnToggleResults.getAttribute('aria-expanded'), 'true');
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), false);

    assert.equal(elements.tabContentResults.classList.contains('active'), true);
    assert.equal(elements.tabBtnResults.classList.contains('active'), true);
    assert.equal(elements.tabBtnResults.getAttribute('aria-selected'), 'true');

    // LocalStorage lưu isOpen=true, activeTab=tab-results
    const state = context.getDrawerState();
    assert.equal(state.isOpen, true);
    assert.equal(state.activeTab, 'tab-results');

    // Click lại -> Đóng drawer
    elements.btnToggleResults.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true);
    assert.equal(elements.btnToggleResults.classList.contains('active'), false);
});

test('T47 Drawer: Chuyển tab giữa Cấu hình và Báo cáo khi Drawer đang mở', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;
    context.setupDrawer();

    // Mở tab Cấu hình
    elements.btnToggleStrategy.click();
    assert.equal(elements.tabContentStrategy.classList.contains('active'), true);

    // Khi drawer đang mở, click nút Header Kết quả
    elements.btnToggleResults.click();
    // Drawer vẫn mở, nhưng tab đổi sang Kết quả
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);
    assert.equal(elements.btnToggleResults.classList.contains('active'), true);
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), false);
    assert.equal(elements.tabContentResults.classList.contains('active'), true);
    assert.equal(elements.tabContentStrategy.classList.contains('active'), false);

    // Click tab Cấu hình bên trong Drawer (.panel-tab)
    elements.tabBtnStrategy.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);
    assert.equal(elements.tabContentStrategy.classList.contains('active'), true);
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), true);
    assert.equal(elements.btnToggleResults.classList.contains('active'), false);
});

test('T47 Drawer: Nút đóng ✕ (#btn-close-drawer) đóng drawer và giữ activeTab đã chọn', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;
    context.setupDrawer();

    // Mở tab Results
    elements.btnToggleResults.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);

    // Click nút ✕
    elements.btnCloseDrawer.click();

    // Drawer đóng
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true);
    assert.equal(elements.sidePanel.getAttribute('aria-expanded'), 'false');
    assert.equal(elements.btnToggleResults.classList.contains('active'), false);
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), false);

    // State lưu: isOpen=false nhưng activeTab=tab-results được bảo lưu
    const saved = context.getDrawerState();
    assert.equal(saved.isOpen, false);
    assert.equal(saved.activeTab, 'tab-results');
});

test('T47 Drawer: Backdrop KHÔNG được kích hoạt trên Desktop / Tablet (innerWidth >= 768)', () => {
    const env = createMockEnvironment({}, { innerWidth: 1920 });
    const { context, elements } = env;
    context.setupDrawer();

    // Mở drawer trên Desktop
    elements.btnToggleStrategy.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);

    // Backdrop KHÔNG được kích hoạt để tránh che phủ các tương tác
    assert.equal(elements.drawerBackdrop.classList.contains('active'), false, 'Backdrop không được active trên Desktop');
    assert.notEqual(elements.drawerBackdrop.style.display, 'block', 'Backdrop không được hiển thị block trên Desktop');
});

test('T47 Drawer: Backdrop ĐƯỢC kích hoạt trên Mobile (innerWidth < 768) và đóng drawer khi click', () => {
    const env = createMockEnvironment({}, { innerWidth: 375 });
    const { context, elements } = env;
    context.setupDrawer();

    // Mở drawer trên Mobile
    elements.btnToggleStrategy.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);

    // Backdrop PHẢI được kích hoạt trên mobile overlay
    assert.equal(elements.drawerBackdrop.style.display, 'block', 'Backdrop phải hiển thị block trên Mobile');
    assert.equal(elements.drawerBackdrop.classList.contains('active'), true, 'Backdrop phải active trên Mobile');

    // Click backdrop -> Drawer phải đóng
    elements.drawerBackdrop.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true);
    assert.equal(elements.btnToggleStrategy.classList.contains('active'), false);
});

test('T47 Drawer: Tự động ẩn backdrop khi resize từ Mobile sang Desktop', () => {
    const env = createMockEnvironment({}, { innerWidth: 375 });
    const { context, elements } = env;
    context.setupDrawer();

    // Mở drawer trên mobile
    elements.btnToggleStrategy.click();
    assert.equal(elements.drawerBackdrop.classList.contains('active'), true);

    // Giả lập người dùng xoay ngang màn hình hoặc resize cửa sổ sang Desktop (1280px)
    context.innerWidth = 1280;
    context.window.dispatchEvent({ type: 'resize' });

    // Backdrop phải tự động ẩn
    assert.equal(elements.drawerBackdrop.classList.contains('active'), false, 'Backdrop phải bị xóa class active khi sang Desktop');
    assert.equal(elements.drawerBackdrop.style.display, 'none', 'Backdrop display phải chuyển thành none');
});

test('T47 Drawer: Phím tắt Escape đóng drawer ngoại trừ khi dialog overlay / context menu mở', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;
    context.setupDrawer();

    // 1. Mở drawer và ấn Escape -> Đóng drawer
    elements.btnToggleStrategy.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);

    context.window.dispatchEvent({ type: 'keydown', key: 'Escape' });
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true, 'Escape phải đóng drawer khi mở');

    // 2. Mở lại drawer, nhưng Property Dialog đang hiển thị (display: flex)
    elements.btnToggleStrategy.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false);
    elements.propertyDialogOverlay.style.display = 'flex';

    context.window.dispatchEvent({ type: 'keydown', key: 'Escape' });
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false, 'Escape không được đóng drawer nếu property dialog đang mở');

    // Ẩn Property dialog
    elements.propertyDialogOverlay.style.display = 'none';

    // 3. Context Menu đang hiển thị (display: block)
    elements.drawingContextMenu.style.display = 'block';
    context.window.dispatchEvent({ type: 'keydown', key: 'Escape' });
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false, 'Escape không được đóng drawer nếu context menu đang mở');

    // Ẩn context menu, ấn Escape -> Đóng drawer
    elements.drawingContextMenu.style.display = 'none';
    context.window.dispatchEvent({ type: 'keydown', key: 'Escape' });
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true);
});

test('T47 Drawer: Khôi phục trạng thái từ LocalStorage và xử lý an toàn dữ liệu hỏng (corrupt data fallback)', () => {
    // 1. Dữ liệu hợp lệ: đã mở trước đó với tab Kết quả
    const envValid = createMockEnvironment({
        'backtest:ui:drawer': JSON.stringify({ isOpen: true, activeTab: 'tab-results' })
    });
    envValid.context.setupDrawer();
    assert.equal(envValid.elements.sidePanel.classList.contains('collapsed'), false);
    assert.equal(envValid.elements.btnToggleResults.classList.contains('active'), true);
    assert.equal(envValid.elements.tabContentResults.classList.contains('active'), true);

    // 2. Dữ liệu JSON bị hỏng (Syntax Error)
    const envCorrupt = createMockEnvironment({
        'backtest:ui:drawer': '{"isOpen": true, activeTab: corrupted json...'
    });
    // Không ném exception, fallback về default đóng
    assert.doesNotThrow(() => envCorrupt.context.setupDrawer());
    assert.equal(envCorrupt.elements.sidePanel.classList.contains('collapsed'), true);
    assert.equal(envCorrupt.context.getDrawerState().isOpen, false);
    assert.equal(envCorrupt.context.getDrawerState().activeTab, 'tab-strategy');

    // 3. Dữ liệu không phải Object (number, null, string)
    const envInvalidType = createMockEnvironment({
        'backtest:ui:drawer': '42'
    });
    assert.doesNotThrow(() => envInvalidType.context.setupDrawer());
    assert.equal(envInvalidType.context.getDrawerState().isOpen, false);

    // 4. Tab ID không hợp lệ trong storage (xss hoặc giá trị lạ)
    const envInvalidTab = createMockEnvironment({
        'backtest:ui:drawer': JSON.stringify({ isOpen: true, activeTab: 'tab-unrecognized-hacked' })
    });
    envInvalidTab.context.setupDrawer();
    assert.equal(envInvalidTab.context.getDrawerState().activeTab, 'tab-strategy', 'Tab không hợp lệ phải fallback về tab-strategy');

    // 5. LocalStorage quăng lỗi (Private Browsing / Quota)
    envValid.storage.shouldThrow = true;
    assert.doesNotThrow(() => envValid.context.openDrawer('tab-strategy'), 'saveDrawerState không được gây crash khi storage lỗi');
});

test('T47 Drawer: Input cấu hình và giá trị form không bị mất khi đóng mở drawer', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;
    context.setupDrawer();

    // Mở drawer và người dùng thay đổi thông số
    elements.btnToggleStrategy.click();
    elements.initialCapital.value = '50000';
    elements.lotSize.value = '0.5';
    elements.stopLoss.value = '75';
    elements.takeProfit.value = '150';
    elements.candlesLimit.value = '2500';
    elements.allowShort.checked = false;

    // Đóng drawer
    elements.btnCloseDrawer.click();
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true);

    // Kiểm tra các giá trị trong DOM vẫn nguyên vẹn 100% khi đóng
    assert.equal(elements.initialCapital.value, '50000');
    assert.equal(elements.lotSize.value, '0.5');
    assert.equal(elements.stopLoss.value, '75');
    assert.equal(elements.takeProfit.value, '150');
    assert.equal(elements.candlesLimit.value, '2500');
    assert.equal(elements.allowShort.checked, false);

    // Mở lại drawer -> Thông số vẫn hiển thị đúng
    elements.btnToggleStrategy.click();
    assert.equal(elements.initialCapital.value, '50000');
    assert.equal(elements.lotSize.value, '0.5');
});

test('T47 Drawer: Tự động mở tab Báo cáo khi có kết quả Backtest (displayBacktestResults)', () => {
    const env = createMockEnvironment({});
    const { context, elements } = env;
    context.setupDrawer();

    // Đảm bảo drawer ban đầu đang đóng
    assert.equal(elements.sidePanel.classList.contains('collapsed'), true);

    // Giả lập backtest hoàn tất và gọi displayBacktestResults
    const mockBacktestData = {
        metrics: {
            net_profit: 1250.50,
            return_pct: 12.5,
            win_rate: 65.0,
            profit_factor: 2.1,
            winning_trades: 13,
            losing_trades: 7,
            total_trades: 20,
            max_drawdown: 350.0,
            max_drawdown_pct: 3.5
        },
        trades: [],
        equity_curve: [],
        markers: []
    };

    context.displayBacktestResults(mockBacktestData);

    // Drawer phải tự động mở
    assert.equal(elements.sidePanel.classList.contains('collapsed'), false, 'Drawer phải tự mở khi có kết quả');
    // Tab Kết quả & Báo cáo phải active
    assert.equal(elements.tabContentResults.classList.contains('active'), true);
    assert.equal(elements.btnToggleResults.classList.contains('active'), true);
    assert.equal(elements.resultsContent.style.display, 'block');
    assert.equal(elements.resultsEmpty.style.display, 'none');
    assert.ok(elements.netPnl.textContent.includes('1,250.50'));
});
