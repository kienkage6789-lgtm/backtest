// tests/verify_drawer_qa.js
// Automation script for real Browser QA of Task T47 (Collapsible Strategy & Results Drawer)
// Uses Headless Chrome via Chrome DevTools Protocol (CDP)

const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ARTIFACTS_DIR = 'C:\\Users\\Admin\\.gemini\\antigravity\\brain\\b650687b-9f2f-46ee-817c-8a7abf455532';

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function getJson(url) {
    return new Promise((resolve, reject) => {
        http.get(url, res => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try { resolve(JSON.parse(data)); } catch (e) { reject(e); }
            });
        }).on('error', reject);
    });
}

async function isServerRunning() {
    return new Promise(resolve => {
        const req = http.get('http://127.0.0.1:8000/api/info', res => {
            resolve(res.statusCode === 200);
        });
        req.on('error', () => resolve(false));
        req.setTimeout(1000, () => {
            req.destroy();
            resolve(false);
        });
    });
}

class CDPClient {
    constructor(wsUrl) {
        this.ws = new WebSocket(wsUrl);
        this.id = 1;
        this.callbacks = new Map();
        this.ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            if (msg.id && this.callbacks.has(msg.id)) {
                const { resolve, reject } = this.callbacks.get(msg.id);
                this.callbacks.delete(msg.id);
                if (msg.error) reject(msg.error);
                else resolve(msg.result);
            }
        };
    }

    async ready() {
        if (this.ws.readyState === WebSocket.OPEN) return;
        return new Promise((resolve) => {
            this.ws.onopen = () => resolve();
        });
    }

    async send(method, params = {}) {
        await this.ready();
        const msgId = this.id++;
        return new Promise((resolve, reject) => {
            this.callbacks.set(msgId, { resolve, reject });
            this.ws.send(JSON.stringify({ id: msgId, method, params }));
        });
    }

    async eval(expression) {
        const res = await this.send('Runtime.evaluate', {
            expression,
            returnByValue: true,
            awaitPromise: true
        });
        if (res.exceptionDetails) {
            throw new Error(JSON.stringify(res.exceptionDetails));
        }
        return res.result.value;
    }

    async setViewport(width, height, isMobile = false) {
        await this.send('Emulation.setDeviceMetricsOverride', {
            width,
            height,
            deviceScaleFactor: 1,
            mobile: isMobile
        });
        await sleep(300);
    }

    async screenshot(filepath) {
        const res = await this.send('Page.captureScreenshot', { format: 'png' });
        fs.writeFileSync(filepath, Buffer.from(res.data, 'base64'));
        console.log(`[Screenshot saved] -> ${filepath}`);
    }

    close() {
        this.ws.close();
    }
}

async function runDrawerQA() {
    console.log('=== KHỞI CHẠY BROWSER QA CHO TASK T47 (COLLAPSIBLE DRAWER) ===');
    
    let serverProc = null;
    const running = await isServerRunning();
    if (!running) {
        console.log('[1/4] Khởi động Uvicorn server tại 127.0.0.1:8000...');
        serverProc = spawn(
            path.join(__dirname, '..', '.venv', 'Scripts', 'python.exe'),
            ['-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', '8000'],
            { cwd: path.join(__dirname, '..'), stdio: 'ignore' }
        );

        let attempts = 0;
        while (!(await isServerRunning())) {
            await sleep(500);
            attempts++;
            if (attempts > 20) throw new Error('Không thể kết nối tới server sau 10 giây');
        }
        console.log('       Server đã sẵn sàng!');
    } else {
        console.log('[1/4] Server đã chạy sẵn tại 127.0.0.1:8000.');
    }

    console.log('[2/4] Khởi động Headless Chrome (CDP port 9222)...');
    const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
    const chromeProc = spawn(chromePath, [
        '--headless=new',
        '--remote-debugging-port=9222',
        '--disable-gpu',
        '--window-size=1920,1080',
        'about:blank'
    ]);

    await sleep(2000);

    const results = {};

    try {
        const targets = await getJson('http://127.0.0.1:9222/json/list');
        const pageTarget = targets.find(t => t.type === 'page') || targets[0];
        const cdp = new CDPClient(pageTarget.webSocketDebuggerUrl);
        await cdp.ready();
        await cdp.send('Page.enable');
        await cdp.send('Runtime.enable');
        await cdp.send('DOM.enable');

        console.log('[3/4] Điều hướng tới http://127.0.0.1:8000/ và dọn dẹp localStorage để test trạng thái mặc định...');
        await cdp.send('Page.navigate', { url: 'http://127.0.0.1:8000/' });
        await sleep(3000);

        // Reset localStorage để đảm bảo virgin visit
        await cdp.eval(`
            localStorage.removeItem('backtest:ui:drawer');
            location.reload();
        `);
        await sleep(3500);

        await cdp.setViewport(1920, 1080, false);
        await sleep(500);

        // ----------------------------------------------------
        // BƯỚC 1: Trạng thái mặc định (Default Collapsed)
        // ----------------------------------------------------
        console.log('-> Bước 1: Kiểm tra trạng thái mặc định ban đầu...');
        const step1 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                const btnStrat = document.getElementById('btn-toggle-strategy');
                const btnRes = document.getElementById('btn-toggle-results');
                const chartBox = document.getElementById('chart-area-main');
                const isCollapsed = drawer.classList.contains('collapsed');
                const width = drawer.getBoundingClientRect().width;
                const chartWidth = chartBox.getBoundingClientRect().width;
                return {
                    isCollapsed,
                    drawerWidth: width,
                    chartWidth,
                    btnStratActive: btnStrat.classList.contains('active'),
                    btnResActive: btnRes.classList.contains('active'),
                    btnStratExpanded: btnStrat.getAttribute('aria-expanded'),
                    drawerExpanded: drawer.getAttribute('aria-expanded')
                };
            })()
        `);
        console.log('   Trạng thái bước 1:', step1);
        const pass1 = step1.isCollapsed && step1.drawerWidth === 0 && !step1.btnStratActive && !step1.btnResActive && step1.chartWidth > 1700;
        results['01. Trạng thái mặc định (Closed & Chart Full-Width)'] = pass1 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_01_default_closed.png'));

        // ----------------------------------------------------
        // BƯỚC 2: Mở Cấu hình chiến lược bằng nút Header
        // ----------------------------------------------------
        console.log('-> Bước 2: Bật Cấu hình chiến lược (#btn-toggle-strategy)...');
        await cdp.eval(`document.getElementById('btn-toggle-strategy').click();`);
        await sleep(600); // Chờ animation transition 0.25s
        const step2 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                const btnStrat = document.getElementById('btn-toggle-strategy');
                const tabStrat = document.getElementById('tab-strategy');
                const backdrop = document.getElementById('drawer-backdrop');
                const width = drawer.getBoundingClientRect().width;
                const backdropStyle = window.getComputedStyle(backdrop);
                return {
                    isOpen: !drawer.classList.contains('collapsed'),
                    drawerWidth: width,
                    btnStratActive: btnStrat.classList.contains('active'),
                    tabStratActive: tabStrat.classList.contains('active'),
                    drawerExpanded: drawer.getAttribute('aria-expanded'),
                    backdropHidden: backdropStyle.display === 'none' && !backdrop.classList.contains('active')
                };
            })()
        `);
        console.log('   Trạng thái bước 2:', step2);
        const pass2 = step2.isOpen && step2.drawerWidth >= 380 && step2.btnStratActive && step2.tabStratActive && step2.drawerExpanded === 'true' && step2.backdropHidden;
        results['02. Mở Cấu hình chiến lược (#btn-toggle-strategy)'] = pass2 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_02_strategy_opened.png'));

        // ----------------------------------------------------
        // BƯỚC 3: Chuyển sang tab Báo cáo bằng nút Header
        // ----------------------------------------------------
        console.log('-> Bước 3: Chuyển sang Kết quả & Báo cáo (#btn-toggle-results)...');
        await cdp.eval(`document.getElementById('btn-toggle-results').click();`);
        await sleep(400);
        const step3 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                const btnStrat = document.getElementById('btn-toggle-strategy');
                const btnRes = document.getElementById('btn-toggle-results');
                const tabRes = document.getElementById('tab-results');
                const tabStrat = document.getElementById('tab-strategy');
                return {
                    isOpen: !drawer.classList.contains('collapsed'),
                    btnStratActive: btnStrat.classList.contains('active'),
                    btnResActive: btnRes.classList.contains('active'),
                    tabResActive: tabRes.classList.contains('active'),
                    tabStratActive: tabStrat.classList.contains('active')
                };
            })()
        `);
        console.log('   Trạng thái bước 3:', step3);
        const pass3 = step3.isOpen && !step3.btnStratActive && step3.btnResActive && step3.tabResActive && !step3.tabStratActive;
        results['03. Chuyển sang Kết quả & Báo cáo (#btn-toggle-results)'] = pass3 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_03_results_opened.png'));

        // ----------------------------------------------------
        // BƯỚC 4: Chuyển lại Cấu hình bằng Tab bên trong Drawer
        // ----------------------------------------------------
        console.log('-> Bước 4: Click tab Cấu hình bên trong Drawer...');
        await cdp.eval(`document.querySelector('.panel-tab[data-tab="tab-strategy"]').click();`);
        await sleep(300);
        const step4 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                const tabStrat = document.getElementById('tab-strategy');
                const btnStrat = document.getElementById('btn-toggle-strategy');
                return {
                    isOpen: !drawer.classList.contains('collapsed'),
                    tabStratActive: tabStrat.classList.contains('active'),
                    btnStratActive: btnStrat.classList.contains('active')
                };
            })()
        `);
        console.log('   Trạng thái bước 4:', step4);
        const pass4 = step4.isOpen && step4.tabStratActive && step4.btnStratActive;
        results['04. Chuyển tab bên trong Drawer (.panel-tab)'] = pass4 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_04_tab_switched.png'));

        // ----------------------------------------------------
        // BƯỚC 5: Đóng Drawer bằng nút Close ✕ (#btn-close-drawer)
        // ----------------------------------------------------
        console.log('-> Bước 5: Đóng Drawer bằng nút ✕ (#btn-close-drawer)...');
        await cdp.eval(`document.getElementById('btn-close-drawer').click();`);
        await sleep(500);
        const step5 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                const btnStrat = document.getElementById('btn-toggle-strategy');
                const btnRes = document.getElementById('btn-toggle-results');
                return {
                    isCollapsed: drawer.classList.contains('collapsed'),
                    drawerWidth: drawer.getBoundingClientRect().width,
                    btnStratActive: btnStrat.classList.contains('active'),
                    btnResActive: btnRes.classList.contains('active')
                };
            })()
        `);
        console.log('   Trạng thái bước 5:', step5);
        const pass5 = step5.isCollapsed && step5.drawerWidth === 0 && !step5.btnStratActive && !step5.btnResActive;
        results['05. Đóng Drawer bằng nút ✕ (#btn-close-drawer)'] = pass5 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_05_closed_via_btn.png'));

        // ----------------------------------------------------
        // BƯỚC 6: Mở và đóng bằng phím tắt Escape
        // ----------------------------------------------------
        console.log('-> Bước 6: Mở lại và đóng bằng phím tắt Escape...');
        await cdp.eval(`document.getElementById('btn-toggle-strategy').click();`);
        await sleep(400);
        await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
        await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' });
        await sleep(500);
        const step6 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                return {
                    isCollapsed: drawer.classList.contains('collapsed'),
                    drawerWidth: drawer.getBoundingClientRect().width
                };
            })()
        `);
        console.log('   Trạng thái bước 6:', step6);
        const pass6 = step6.isCollapsed && step6.drawerWidth === 0;
        results['06. Đóng Drawer bằng phím tắt Escape'] = pass6 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_06_closed_via_escape.png'));

        // ----------------------------------------------------
        // BƯỚC 7: Chạy Backtest và tự động mở Drawer Báo cáo
        // ----------------------------------------------------
        console.log('-> Bước 7: Chạy Backtest và kiểm tra tự động bung tab Báo cáo...');
        await cdp.eval(`runBacktest();`);
        // Chờ kết quả backtest tính toán và hiển thị (tối đa 4s)
        await sleep(3500);
        const step7 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                const btnRes = document.getElementById('btn-toggle-results');
                const tabRes = document.getElementById('tab-results');
                const resultsContent = document.getElementById('results-content');
                const netPnl = document.getElementById('m-net-pnl').textContent;
                const winRate = document.getElementById('m-win-rate').textContent;
                return {
                    isOpen: !drawer.classList.contains('collapsed'),
                    drawerWidth: drawer.getBoundingClientRect().width,
                    btnResActive: btnRes.classList.contains('active'),
                    tabResActive: tabRes.classList.contains('active'),
                    resultsDisplayed: resultsContent.style.display === 'block',
                    netPnl,
                    winRate
                };
            })()
        `);
        console.log('   Trạng thái bước 7:', step7);
        const pass7 = step7.isOpen && step7.drawerWidth >= 380 && step7.btnResActive && step7.tabResActive && step7.resultsDisplayed;
        results['07. Tự động mở tab Báo cáo khi có kết quả Backtest'] = pass7 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_07_backtest_auto_opened.png'));

        // ----------------------------------------------------
        // BƯỚC 8: Responsive Layout (1280x800 Laptop)
        // ----------------------------------------------------
        console.log('-> Bước 8: Kiểm tra hiển thị responsive tại độ phân giải 1280x800...');
        await cdp.setViewport(1280, 800, false);
        await sleep(600);
        const step8 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                return {
                    width: drawer.getBoundingClientRect().width,
                    isOpen: !drawer.classList.contains('collapsed')
                };
            })()
        `);
        console.log('   Trạng thái bước 8 (1280x800):', step8);
        const pass8 = step8.isOpen && step8.width >= 350;
        results['08. Responsive Laptop 1280x800'] = pass8 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_08_laptop_1280.png'));

        // ----------------------------------------------------
        // BƯỚC 9: Responsive Layout (1024x768 Tablet)
        // ----------------------------------------------------
        console.log('-> Bước 9: Kiểm tra hiển thị responsive tại độ phân giải 1024x768...');
        await cdp.setViewport(1024, 768, false);
        await sleep(600);
        const step9 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                return {
                    width: drawer.getBoundingClientRect().width,
                    isOpen: !drawer.classList.contains('collapsed')
                };
            })()
        `);
        console.log('   Trạng thái bước 9 (1024x768):', step9);
        const pass9 = step9.isOpen && step9.width >= 320;
        results['09. Responsive Tablet 1024x768'] = pass9 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_09_tablet_1024.png'));

        // ----------------------------------------------------
        // BƯỚC 10: Responsive Layout (375x667 Mobile) Overlay & Backdrop
        // ----------------------------------------------------
        console.log('-> Bước 10: Kiểm tra Mobile overlay và Backdrop tại 375x667...');
        await cdp.setViewport(375, 667, true);
        await sleep(600);
        // Đóng drawer trước
        await cdp.eval(`closeDrawer();`);
        await sleep(400);
        // Mở drawer trên mobile
        await cdp.eval(`document.getElementById('btn-toggle-strategy').click();`);
        await sleep(600);
        const step10 = await cdp.eval(`
            (() => {
                const drawer = document.getElementById('side-panel');
                const backdrop = document.getElementById('drawer-backdrop');
                const style = window.getComputedStyle(drawer);
                const backdropStyle = window.getComputedStyle(backdrop);
                return {
                    isOpen: !drawer.classList.contains('collapsed'),
                    position: style.position,
                    zIndex: style.zIndex,
                    backdropDisplay: backdrop.style.display || backdropStyle.display,
                    backdropActive: backdrop.classList.contains('active'),
                    innerWidth: window.innerWidth,
                    matchMedia: window.matchMedia('(max-width: 767px)').matches
                };
            })()
        `);
        console.log('   Trạng thái bước 10 (Mobile):', step10);
        const pass10 = step10.isOpen && step10.position === 'fixed' && step10.backdropDisplay !== 'none';
        results['10. Mobile Overlay & Backdrop (375x667)'] = pass10 ? 'PASS' : 'FAIL';
        await cdp.screenshot(path.join(ARTIFACTS_DIR, 'drawer_10_mobile_375.png'));

        // Click backdrop để đóng drawer trên mobile
        await cdp.eval(`document.getElementById('drawer-backdrop').click();`);
        await sleep(500);
        const closedMobile = await cdp.eval(`document.getElementById('side-panel').classList.contains('collapsed')`);
        results['11. Mobile Backdrop Click Closes Drawer'] = closedMobile ? 'PASS' : 'FAIL';

        cdp.close();

        console.log('\n[4/4] TỔNG KẾT KẾT QUẢ BROWSER QA (11/11 BƯỚC):');
        let allPass = true;
        for (const [step, status] of Object.entries(results)) {
            console.log(` - ${step}: [${status}]`);
            if (status !== 'PASS') allPass = false;
        }

        return allPass;
    } finally {
        chromeProc.kill();
        if (serverProc) {
            serverProc.kill();
        }
    }
}

runDrawerQA().then(success => {
    process.exit(success ? 0 : 1);
}).catch(err => {
    console.error('Lỗi khi thực hiện Browser QA:', err);
    process.exit(1);
});
