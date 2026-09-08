// tests/verify_drawing_future_qa.js
// Automation script for real Browser QA of Task T49 (Infinite Future Drawing & Scale Drag Sync)
// Uses Headless Chrome via Chrome DevTools Protocol (CDP)

const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ARTIFACTS_DIR = 'C:\\Users\\Admin\\.gemini\\antigravity\\brain\\caa55f13-ea4b-4abc-820f-0aa9ad8cd9cc';

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

    async screenshot(filepath) {
        const res = await this.send('Page.captureScreenshot', { format: 'png' });
        fs.writeFileSync(filepath, Buffer.from(res.data, 'base64'));
        console.log(`[Screenshot saved] -> ${filepath}`);
    }

    close() {
        this.ws.close();
    }
}

async function runDrawingQA() {
    console.log('=== KHỞI CHẠY BROWSER QA CHO TASK T49 (FUTURE DRAWING & SCALE DRAG) ===');
    
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

    const testResults = [];

    try {
        const targets = await getJson('http://127.0.0.1:9222/json/list');
        const pageTarget = targets.find(t => t.type === 'page') || targets[0];
        const cdp = new CDPClient(pageTarget.webSocketDebuggerUrl);
        await cdp.ready();
        await cdp.send('Page.enable');
        await cdp.send('Runtime.enable');
        await cdp.send('DOM.enable');

        console.log('[3/4] Điều hướng tới http://127.0.0.1:8000/...');
        await cdp.send('Page.navigate', { url: 'http://127.0.0.1:8000/' });
        await sleep(3500);

        // Lắng nghe console errors
        await cdp.eval(`
            window.__qa_errors = [];
            window.addEventListener('error', e => window.__qa_errors.push(e.message));
        `);

        // 1. Kiểm tra khởi tạo Chart & DrawingManager
        console.log('\n--- Kiểm tra 1: Khởi tạo Chart & DrawingManager ---');
        const chartReady = await cdp.eval(`
            Boolean(window.tradingChart && window.tradingChart.drawingManager && window.tradingChart.currentCandles && window.tradingChart.currentCandles.length > 0)
        `);
        console.log(`Chart & DrawingManager sẵn sàng: ${chartReady}`);
        testResults.push({ name: 'Chart & DrawingManager Ready', passed: chartReady });

        // 2. Tạo nét vẽ kéo dài vào không gian tương lai (Future Drawing)
        console.log('\n--- Kiểm tra 2: Tạo bản vẽ trong không gian tương lai ---');
        const futureDrawResult = await cdp.eval(`
            (() => {
                const tc = window.tradingChart;
                const dm = tc.drawingManager;
                const candles = tc.currentCandles;
                const lastCandle = candles[candles.length - 1];
                const interval = dm.getCandleIntervalSeconds(candles);
                
                // Tạo một trendline từ cây nến cuối tới 10 nến trong tương lai
                const p1 = { time: lastCandle.time - 5 * interval, price: lastCandle.close };
                const p2 = { time: lastCandle.time + 10 * interval, price: lastCandle.close + 10 };
                
                const d = dm.create('trendline', [p1, p2]);
                dm.requestRender();

                // Kiểm tra tọa độ vẽ trên màn hình
                const coord1 = dm.timePriceToCoord(p1.time, p1.price);
                const coord2 = dm.timePriceToCoord(p2.time, p2.price);
                
                return {
                    drawingId: d.id,
                    p1Time: p1.time,
                    p2Time: p2.time,
                    lastCandleTime: lastCandle.time,
                    isFuture: p2.time > lastCandle.time,
                    coord1,
                    coord2,
                    interval
                };
            })()
        `);
        console.log('Kết quả vẽ tương lai:', futureDrawResult);
        const passFutureDraw = Boolean(
            futureDrawResult.isFuture && 
            futureDrawResult.coord1 !== null && 
            futureDrawResult.coord2 !== null &&
            futureDrawResult.coord2.x > futureDrawResult.coord1.x
        );
        console.log(`Vẽ thành công vào tương lai: ${passFutureDraw}`);
        testResults.push({ name: 'Create Future Drawing', passed: passFutureDraw });

        // 3. Kéo anchor xa hơn vào tương lai & Dynamic rightOffset
        console.log('\n--- Kiểm tra 3: Kéo anchor xa hơn vào tương lai & Tự động giãn rightOffset ---');
        const dragAnchorResult = await cdp.eval(`
            (() => {
                const tc = window.tradingChart;
                const dm = tc.drawingManager;
                const candles = tc.currentCandles;
                const lastIdx = candles.length - 1;
                const timeScale = tc.chart.timeScale();
                
                // Target logical = lastIdx + 30 (30 nến sau nến cuối)
                const targetLogical = lastIdx + 30;
                dm.ensureFutureOffset(targetLogical);

                const currentOffset = timeScale.options().rightOffset;
                return {
                    targetLogical,
                    currentOffset,
                    expanded: currentOffset >= 30
                };
            })()
        `);
        console.log('Kết quả giãn rightOffset:', dragAnchorResult);
        testResults.push({ name: 'Dynamic rightOffset Expansion', passed: dragAnchorResult.expanded });

        // 4. Kéo thanh giá (Price Scale Drag) và kiểm tra drawing Y cập nhật tức thì
        console.log('\n--- Kiểm tra 4: Đồng bộ khi kéo thanh giá (Price Scale Drag) ---');
        const priceScaleResult = await cdp.eval(`
            (() => {
                const tc = window.tradingChart;
                const dm = tc.drawingManager;
                const container = tc.container;
                const rect = container.getBoundingClientRect();

                // Lấy tọa độ Y của nét vẽ trước khi kéo
                const d = dm.drawings[0];
                const initialCoord = dm.timePriceToCoord(d.points[0].time, d.points[0].price);

                // Giả lập kéo chuột trên trục giá (phía bên phải container)
                const scaleX = rect.right - 25;
                const startY = rect.top + 100;
                const endY = rect.top + 180;

                // Dispatch pointerdown
                container.dispatchEvent(new PointerEvent('pointerdown', {
                    bubbles: true, clientX: scaleX, clientY: startY, pointerId: 1
                }));

                // Dispatch pointermove trên window
                window.dispatchEvent(new PointerEvent('pointermove', {
                    bubbles: true, clientX: scaleX, clientY: endY, pointerId: 1
                }));

                return {
                    hasInitialCoord: initialCoord !== null,
                    initialY: initialCoord ? initialCoord.y : null,
                    renderPendingDuringMove: dm.renderPending
                };
            })()
        `);

        // Chờ 2 frames để pointermove rAF hoàn tất, sau đó kết thúc kéo và chờ rAF của pointerup
        const priceScaleVerify = await cdp.eval(`
            new Promise(resolve => {
                requestAnimationFrame(() => {
                    window.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }));
                    requestAnimationFrame(() => {
                        requestAnimationFrame(() => {
                            resolve({
                                renderPendingAfterIdle: window.tradingChart.drawingManager.renderPending,
                                svgHasDrawings: window.tradingChart.drawingManager.drawingsGroup.children.length > 0
                            });
                        });
                    });
                });
            })
        `);
        console.log('Kết quả Price Scale Drag:', { priceScaleResult, priceScaleVerify });
        const passPriceScale = Boolean(
            priceScaleResult.hasInitialCoord && 
            priceScaleResult.renderPendingDuringMove &&
            !priceScaleVerify.renderPendingAfterIdle &&
            priceScaleVerify.svgHasDrawings
        );
        testResults.push({ name: 'Price Scale Drag Sync', passed: passPriceScale });

        // 5. Kéo thanh thời gian (Time Scale Drag) và kiểm tra drawing X cập nhật tức thì
        console.log('\n--- Kiểm tra 5: Đồng bộ khi kéo thanh thời gian (Time Scale Drag) ---');
        const timeScaleResult = await cdp.eval(`
            (() => {
                const tc = window.tradingChart;
                const dm = tc.drawingManager;
                const container = tc.container;
                const rect = container.getBoundingClientRect();

                // Lấy tọa độ X trước khi kéo
                const d = dm.drawings[0];
                const initialCoord = dm.timePriceToCoord(d.points[0].time, d.points[0].price);

                // Giả lập kéo chuột trên trục thời gian (phía dưới đáy container)
                const scaleY = rect.bottom - 15;
                const startX = rect.left + 200;
                const endX = rect.left + 300;

                container.dispatchEvent(new PointerEvent('pointerdown', {
                    bubbles: true, clientX: startX, clientY: scaleY, pointerId: 1
                }));

                window.dispatchEvent(new PointerEvent('pointermove', {
                    bubbles: true, clientX: endX, clientY: scaleY, pointerId: 1
                }));

                return {
                    hasInitialCoord: initialCoord !== null,
                    initialX: initialCoord ? initialCoord.x : null,
                    renderPendingDuringMove: dm.renderPending
                };
            })()
        `);

        const timeScaleVerify = await cdp.eval(`
            new Promise(resolve => {
                requestAnimationFrame(() => {
                    window.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1 }));
                    requestAnimationFrame(() => {
                        requestAnimationFrame(() => {
                            resolve({
                                renderPendingAfterIdle: window.tradingChart.drawingManager.renderPending,
                                svgHasDrawings: window.tradingChart.drawingManager.drawingsGroup.children.length > 0
                            });
                        });
                    });
                });
            })
        `);
        console.log('Kết quả Time Scale Drag:', { timeScaleResult, timeScaleVerify });
        const passTimeScale = Boolean(
            timeScaleResult.hasInitialCoord && 
            timeScaleResult.renderPendingDuringMove &&
            !timeScaleVerify.renderPendingAfterIdle &&
            timeScaleVerify.svgHasDrawings
        );
        testResults.push({ name: 'Time Scale Drag Sync', passed: passTimeScale });

        // 6. Kiểm tra Console Errors và Render Loop
        console.log('\n--- Kiểm tra 6: Console Errors & Render Loop Check ---');
        const errorLogs = await cdp.eval('window.__qa_errors');
        console.log('Console errors recorded:', errorLogs);
        const passNoErrors = Array.isArray(errorLogs) && errorLogs.length === 0;
        testResults.push({ name: 'Zero Console Errors', passed: passNoErrors });

        // Chụp screenshot minh chứng
        if (!fs.existsSync(ARTIFACTS_DIR)) {
            fs.mkdirSync(ARTIFACTS_DIR, { recursive: true });
        }
        const screenshotPath = path.join(ARTIFACTS_DIR, 'qa_future_drawing_verification.png');
        await cdp.screenshot(screenshotPath);

        cdp.close();
    } finally {
        if (chromeProc) chromeProc.kill();
        if (serverProc) serverProc.kill();
    }

    console.log('\n================ TỔNG HỢP KẾT QUẢ QA ================');
    let allPassed = true;
    for (const t of testResults) {
        console.log(`[${t.passed ? 'PASS' : 'FAIL'}] ${t.name}`);
        if (!t.passed) allPassed = false;
    }

    if (!allPassed) {
        console.error('MỘT SỐ KIỂM TRA THẤT BẠI!');
        process.exit(1);
    } else {
        console.log('TẤT CẢ CÁC KIỂM TRA BROWSER QA ĐỀU THÀNH CÔNG!');
    }
}

runDrawingQA().catch(err => {
    console.error('Lỗi thực thi QA:', err);
    process.exit(1);
});
