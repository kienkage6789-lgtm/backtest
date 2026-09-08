const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, '..', 'public', 'index.html');
const htmlContent = fs.readFileSync(HTML_PATH, 'utf8');

test('UI Structure: Không tồn tại duplicate ID trong toàn bộ file index.html', () => {
    const idRegex = /\bid=["']([^"']+)["']/gi;
    const idCounts = {};
    let match;

    while ((match = idRegex.exec(htmlContent)) !== null) {
        const id = match[1];
        idCounts[id] = (idCounts[id] || 0) + 1;
    }

    const duplicates = Object.entries(idCounts).filter(([, count]) => count > 1);
    assert.deepEqual(
        duplicates,
        [],
        `Phát hiện các ID bị trùng lặp: ${duplicates.map(([id, count]) => `${id} (x${count})`).join(', ')}`
    );
});

test('UI Structure: Số lượng thẻ mở <div ...> và thẻ đóng </div> phải cân bằng tuyệt đối', () => {
    const openDivMatches = htmlContent.match(/<div\b[^>]*>/gi) || [];
    const closeDivMatches = htmlContent.match(/<\/div>/gi) || [];

    assert.equal(
        openDivMatches.length,
        closeDivMatches.length,
        `Mất cân bằng thẻ div: ${openDivMatches.length} thẻ mở vs ${closeDivMatches.length} thẻ đóng (chênh lệch: ${openDivMatches.length - closeDivMatches.length})`
    );
});

test('UI Structure: Thẻ div lồng nhau chuẩn xác (stack balance check)', () => {
    const tagRegex = /<\/?div\b[^>]*>/gi;
    let depth = 0;
    let match;
    let minDepth = 0;

    while ((match = tagRegex.exec(htmlContent)) !== null) {
        const tag = match[0];
        if (tag.startsWith('</')) {
            depth--;
            if (depth < minDepth) minDepth = depth;
        } else {
            depth++;
        }
    }

    assert.ok(minDepth >= 0, `Có thẻ đóng </div> mồ côi khi depth = ${minDepth}`);
    assert.equal(depth, 0, `Kết thúc file với depth = ${depth} (chưa đóng hết các thẻ div)`);
});

test('UI Structure: Các ID bắt buộc phải tồn tại chính xác 1 lần', () => {
    const requiredSingleIds = [
        'drawing-style-bar',
        'chart-container',
        'chart-container-2',
        'chart-legend',
        'charts-grid',
        'chart-box-1',
        'chart-box-2',
        'drawing-toolbar',
        'drawing-favorites-bar',
        'chart-area-main',
        'property-dialog-overlay',
        'drawing-context-menu',
        'drawing-alert-toast',
        'replay-toolbar',
        'loading-overlay',
        'style-color-picker',
        'style-width-select',
        'style-line-select',
        'style-fill-container',
        'style-fill-picker',
        'btn-style-lock',
        'btn-style-hide',
        'btn-style-delete',
        'btn-style-close',
        'btn-style-props',
        'side-panel',
        'btn-toggle-strategy',
        'btn-toggle-results',
        'btn-close-drawer',
        'drawer-backdrop'
    ];

    for (const id of requiredSingleIds) {
        const regex = new RegExp(`\\bid=["']${id}["']`, 'g');
        const matches = htmlContent.match(regex) || [];
        assert.equal(
            matches.length,
            1,
            `Element ID '${id}' phải xuất hiện đúng 1 lần nhưng tìm thấy ${matches.length} lần`
        );
    }
});

test('UI Structure: Style Bar mặc định phải có display: none khi chưa chọn drawing', () => {
    const styleBarMatch = htmlContent.match(/<div[^>]*\bid=["']drawing-style-bar["'][^>]*>/i);
    assert.ok(styleBarMatch, 'Không tìm thấy thẻ mở #drawing-style-bar');
    const tagContent = styleBarMatch[0];
    assert.match(
        tagContent,
        /style=["'][^"']*display:\s*none;?[^"']*["']/i,
        '#drawing-style-bar phải có style="display: none;" khi khởi tạo'
    );
});

test('UI Structure: Thứ tự DOM và tính bao bọc (Hierarchy Nesting)', () => {
    const posWorkspace = htmlContent.indexOf('class="workspace"');
    const posToolbar = htmlContent.indexOf('id="drawing-toolbar"');
    const posFavBar = htmlContent.indexOf('id="drawing-favorites-bar"');
    const posChartArea = htmlContent.indexOf('id="chart-area-main"');
    const posSidePanel = htmlContent.search(/class=["'][^"']*\bside-panel\b/);

    assert.ok(posWorkspace > 0, 'Thiếu .workspace');
    assert.ok(posToolbar > posWorkspace, '.drawing-toolbar phải nằm sau .workspace mở');
    assert.ok(posFavBar > posToolbar, '.drawing-favorites-bar phải nằm sau .drawing-toolbar');
    assert.ok(posChartArea > posFavBar, '#chart-area-main phải nằm sau .drawing-favorites-bar');
    assert.ok(posSidePanel > posChartArea, '.side-panel phải nằm sau #chart-area-main');

    const posStyleBar = htmlContent.indexOf('id="drawing-style-bar"');
    const posPropOverlay = htmlContent.indexOf('id="property-dialog-overlay"');
    const posContextMenu = htmlContent.indexOf('id="drawing-context-menu"');
    const posAlertToast = htmlContent.indexOf('id="drawing-alert-toast"');
    const posLegend = htmlContent.indexOf('id="chart-legend"');
    const posChartsGrid = htmlContent.indexOf('id="charts-grid"');
    const posChart1 = htmlContent.indexOf('id="chart-container"');
    const posChart2 = htmlContent.indexOf('id="chart-container-2"');
    const posReplayToolbar = htmlContent.indexOf('id="replay-toolbar"');
    const posLoadingOverlay = htmlContent.indexOf('id="loading-overlay"');

    const elementsInChartArea = [
        { name: 'drawing-style-bar', pos: posStyleBar },
        { name: 'property-dialog-overlay', pos: posPropOverlay },
        { name: 'drawing-context-menu', pos: posContextMenu },
        { name: 'drawing-alert-toast', pos: posAlertToast },
        { name: 'chart-legend', pos: posLegend },
        { name: 'charts-grid', pos: posChartsGrid },
        { name: 'chart-container', pos: posChart1 },
        { name: 'chart-container-2', pos: posChart2 },
        { name: 'replay-toolbar', pos: posReplayToolbar },
        { name: 'loading-overlay', pos: posLoadingOverlay }
    ];

    for (const item of elementsInChartArea) {
        assert.ok(item.pos > posChartArea, `${item.name} phải nằm bên trong/sau #chart-area-main`);
        assert.ok(item.pos < posSidePanel, `${item.name} phải nằm trước .side-panel`);
    }

    assert.ok(posChart1 > posChartsGrid, 'chart-container phải nằm sau charts-grid');
    assert.ok(posChart2 > posChart1, 'chart-container-2 phải nằm sau chart-container');
});

test('UI Structure: Drawer và các Tab cấu hình/kết quả hợp lệ', () => {
    // Drawer tồn tại đúng 1 lần
    const drawerMatches = htmlContent.match(/<div[^>]*\bid=["']side-panel["'][^>]*>/gi) || [];
    assert.equal(drawerMatches.length, 1, 'Drawer #side-panel phải tồn tại chính xác 1 lần');

    // Drawer mặc định có class collapsed
    assert.ok(drawerMatches[0].includes('collapsed'), '#side-panel phải có class collapsed mặc định');

    // Nút toggle tồn tại trên header
    assert.ok(htmlContent.includes('id="btn-toggle-strategy"'), 'Thiếu nút #btn-toggle-strategy');
    assert.ok(htmlContent.includes('id="btn-toggle-results"'), 'Thiếu nút #btn-toggle-results');
    assert.ok(htmlContent.includes('id="btn-close-drawer"'), 'Thiếu nút #btn-close-drawer');
    assert.ok(htmlContent.includes('id="drawer-backdrop"'), 'Thiếu #drawer-backdrop');

    // 2 tabs tồn tại
    assert.ok(htmlContent.includes('id="tab-strategy"'), 'Thiếu #tab-strategy');
    assert.ok(htmlContent.includes('id="tab-results"'), 'Thiếu #tab-results');

    // Controls cấu hình nằm trong tab-strategy
    const tabStrategyStart = htmlContent.indexOf('id="tab-strategy"');
    const tabResultsStart = htmlContent.indexOf('id="tab-results"');
    assert.ok(tabStrategyStart > 0 && tabResultsStart > tabStrategyStart, 'Thứ tự tab-strategy và tab-results hợp lệ');

    const tabStrategyHtml = htmlContent.substring(tabStrategyStart, tabResultsStart);
    assert.ok(tabStrategyHtml.includes('id="strategy-select"'), 'strategy-select phải nằm trong tab-strategy');
    assert.ok(tabStrategyHtml.includes('id="backtest-form"'), 'backtest-form phải nằm trong tab-strategy');
    assert.ok(tabStrategyHtml.includes('id="initial-capital"'), 'initial-capital phải nằm trong tab-strategy');
    assert.ok(tabStrategyHtml.includes('id="lot-size"'), 'lot-size phải nằm trong tab-strategy');
    assert.ok(tabStrategyHtml.includes('id="btn-run-backtest"'), 'btn-run-backtest phải nằm trong tab-strategy');

    // Controls báo cáo nằm trong tab-results
    const tabResultsHtml = htmlContent.substring(tabResultsStart);
    assert.ok(tabResultsHtml.includes('id="results-empty"'), 'results-empty phải nằm trong tab-results');
    assert.ok(tabResultsHtml.includes('id="results-content"'), 'results-content phải nằm trong tab-results');
    assert.ok(tabResultsHtml.includes('id="m-net-pnl"'), 'm-net-pnl phải nằm trong tab-results');
    assert.ok(tabResultsHtml.includes('id="equity-chart"'), 'equity-chart phải nằm trong tab-results');
    assert.ok(tabResultsHtml.includes('id="trades-tbody"'), 'trades-tbody phải nằm trong tab-results');
});

test('UI Structure: Không có bất kỳ control Style Bar nào nằm ngoài #drawing-style-bar', () => {
    const styleBarStart = htmlContent.indexOf('id="drawing-style-bar"');
    assert.ok(styleBarStart > 0, '#drawing-style-bar không tồn tại');

    const beforeStyleBar = htmlContent.substring(0, styleBarStart);
    const propOverlayStart = htmlContent.indexOf('id="property-dialog-overlay"');
    const afterStyleBar = htmlContent.substring(propOverlayStart);

    const outsideHtml = beforeStyleBar + afterStyleBar;

    assert.ok(!outsideHtml.includes('class="style-bar-item"'), 'Phát hiện style-bar-item nằm ngoài #drawing-style-bar');
    assert.ok(!outsideHtml.includes('id="style-color-picker"'), 'Phát hiện style-color-picker nằm ngoài #drawing-style-bar');
    assert.ok(!outsideHtml.includes('id="style-width-select"'), 'Phát hiện style-width-select nằm ngoài #drawing-style-bar');
    assert.ok(!outsideHtml.includes('id="style-line-select"'), 'Phát hiện style-line-select nằm ngoài #drawing-style-bar');
    assert.ok(!outsideHtml.includes('id="btn-style-lock"'), 'Phát hiện btn-style-lock nằm ngoài #drawing-style-bar');
    assert.ok(!outsideHtml.includes('id="btn-style-delete"'), 'Phát hiện btn-style-delete nằm ngoài #drawing-style-bar');
});
