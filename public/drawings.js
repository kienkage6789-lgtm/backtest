// drawings.js - Hệ thống Drawing Tools cho Web Trading Backtest
// Tương thích cả Browser (window) và Node.js test runner (module.exports)

/**
 * 1. HÌNH HỌC & TÍNH TOÁN THUẦN (DRAWING GEOMETRY)
 */
const DrawingGeometry = {
    // Khoảng cách từ điểm (px, py) tới đoạn thẳng nối (x1, y1) và (x2, y2)
    distanceToSegment(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1;
        const dy = y2 - y1;
        const lenSq = dx * dx + dy * dy;
        if (lenSq === 0) {
            return Math.hypot(px - x1, py - y1);
        }
        let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
        t = Math.max(0, Math.min(1, t));
        const projX = x1 + t * dx;
        const projY = y1 + t * dy;
        return Math.hypot(px - projX, py - projY);
    },

    // Khoảng cách từ điểm tới tia (Ray) xuất phát từ (x1, y1) qua (x2, y2) kéo dài vô hạn
    distanceToRay(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1;
        const dy = y2 - y1;
        const lenSq = dx * dx + dy * dy;
        if (lenSq === 0) return Math.hypot(px - x1, py - y1);
        let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
        t = Math.max(0, t); // t >= 0 (vô hạn về phía điểm 2)
        const projX = x1 + t * dx;
        const projY = y1 + t * dy;
        return Math.hypot(px - projX, py - projY);
    },

    // Khoảng cách từ điểm tới đường thẳng kéo dài vô hạn 2 phía (Extended Line)
    distanceToLine(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1;
        const dy = y2 - y1;
        const lenSq = dx * dx + dy * dy;
        if (lenSq === 0) return Math.hypot(px - x1, py - y1);
        const t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
        const projX = x1 + t * dx;
        const projY = y1 + t * dy;
        return Math.hypot(px - projX, py - projY);
    },

    // Kiểm tra điểm nằm trong hình chữ nhật (chuẩn hóa tọa độ đối góc)
    isPointInRect(px, py, x1, y1, x2, y2) {
        const minX = Math.min(x1, x2);
        const maxX = Math.max(x1, x2);
        const minY = Math.min(y1, y2);
        const maxY = Math.max(y1, y2);
        return px >= minX && px <= maxX && py >= minY && py <= maxY;
    },

    // Tính toán các mức Fibonacci Retracement
    calculateFibRetracement(p1, p2, customLevels = null) {
        const levels = customLevels || [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0];
        const diff = p2 - p1;
        return levels.map(ratio => ({
            ratio: ratio,
            percent: (ratio * 100).toFixed(1) + '%',
            price: p1 + diff * (1 - ratio) // Theo hướng kéo từ p1 tới p2
        }));
    },

    // Tính toán Fibonacci Extension (3 điểm neo A -> B -> C)
    calculateFibExtension(pA, pB, pC, customLevels = null) {
        const levels = customLevels || [0.0, 0.618, 1.0, 1.272, 1.618, 2.618];
        const height = pB - pA;
        return levels.map(ratio => ({
            ratio: ratio,
            percent: (ratio * 100).toFixed(1) + '%',
            price: pC + height * ratio
        }));
    },

    // Tính toán thông số Ruler / Measurement
    calculateRulerMetrics(p1, p2, t1, t2, candleCount = 0) {
        const startPrice = Number(p1) || 0;
        const endPrice = Number(p2) || 0;
        const deltaPrice = Number((endPrice - startPrice).toFixed(3));
        const percentChange = startPrice !== 0 ? Number(((deltaPrice / startPrice) * 100).toFixed(2)) : 0;
        const time1 = Number(t1) || 0;
        const time2 = Number(t2) || 0;
        const durationSec = Math.abs(time2 - time1);
        const days = Math.floor(durationSec / 86400);
        const hours = Math.floor((durationSec % 86400) / 3600);
        const mins = Math.floor((durationSec % 3600) / 60);

        let durationStr = '';
        if (days > 0) durationStr += `${days}d `;
        if (hours > 0 || days > 0) durationStr += `${hours}h `;
        durationStr += `${mins}m`;

        const count = Math.max(0, parseInt(candleCount) || 0);

        return {
            startPrice: startPrice,
            endPrice: endPrice,
            deltaPrice: deltaPrice,
            percentChange: percentChange,
            candleCount: count,
            durationSeconds: durationSec,
            durationFormatted: durationStr.trim(),
            bars: count,
            duration: durationStr.trim()
        };
    },

    // Tính toán hình học kênh song song (Parallel Channel) từ 3 điểm neo
    calculateChannel(x1, y1, x2, y2, x3, y3) {
        const dx = x2 - x1;
        const dy = y2 - y1;
        const lenSq = dx * dx + dy * dy;
        if (lenSq === 0) {
            return {
                base: [{ x: x1, y: y1 }, { x: x2, y: y2 }],
                parallel: [{ x: x3, y: y3 }, { x: x3, y: y3 }],
                midline: [{ x: (x1 + x3) / 2, y: (y1 + y3) / 2 }, { x: (x2 + x3) / 2, y: (y2 + y3) / 2 }],
                polygon: [{ x: x1, y: y1 }, { x: x2, y: y2 }, { x: x3, y: y3 }, { x: x3, y: y3 }],
                offset: { x: x3 - x1, y: y3 - y1 }
            };
        }
        const t = ((x3 - x1) * dx + (y3 - y1) * dy) / lenSq;
        const projX = x1 + t * dx;
        const projY = y1 + t * dy;
        const offX = x3 - projX;
        const offY = y3 - projY;

        const p1Parallel = { x: x1 + offX, y: y1 + offY };
        const p2Parallel = { x: x2 + offX, y: y2 + offY };
        const p1Mid = { x: x1 + offX / 2, y: y1 + offY / 2 };
        const p2Mid = { x: x2 + offX / 2, y: y2 + offY / 2 };

        return {
            base: [{ x: x1, y: y1 }, { x: x2, y: y2 }],
            parallel: [p1Parallel, p2Parallel],
            midline: [p1Mid, p2Mid],
            polygon: [{ x: x1, y: y1 }, { x: x2, y: y2 }, p2Parallel, p1Parallel],
            offset: { x: offX, y: offY }
        };
    },

    // Kiểm tra điểm nằm trong đa giác lồi hoặc lõm (Ray-casting point in polygon)
    isPointInPolygon(px, py, vertices) {
        let inside = false;
        for (let i = 0, j = vertices.length - 1; i < vertices.length; j = i++) {
            const xi = vertices[i].x, yi = vertices[i].y;
            const xj = vertices[j].x, yj = vertices[j].y;
            const intersect = ((yi > py) !== (yj > py)) &&
                (px < (xj - xi) * (py - yi) / (yj - yi) + xi);
            if (intersect) inside = !inside;
        }
        return inside;
    },

    // 1.1. Tính góc nghiêng của đường xu hướng (Trend Angle) theo độ
    calculateTrendAngle(point1, point2) {
        let p1 = point1;
        let p2 = point2;
        // Hỗ trợ overload: nhận 4 số nguyên thủy (x1, y1, x2, y2) hoặc 2 object point
        if (typeof point1 === 'number' && typeof point2 === 'number') {
            const x1 = point1, y1 = point2, x2 = arguments[2] || 0, y2 = arguments[3] || 0;
            p1 = { x: x1, y: y1 };
            p2 = { x: x2, y: y2 };
        }
        if (!p1 || !p2) {
            const empty = { angleDeg: 0, slope: 0, deltaPrice: 0, deltaTime: 0 };
            empty.valueOf = () => 0;
            empty.toString = () => '0';
            return empty;
        }

        const hasScreenCoords = (p1.x !== undefined && p1.y !== undefined && p2.x !== undefined && p2.y !== undefined);
        const x1 = hasScreenCoords ? p1.x : (p1.time || 0);
        const y1 = hasScreenCoords ? p1.y : (p1.price || 0);
        const x2 = hasScreenCoords ? p2.x : (p2.time || 0);
        const y2 = hasScreenCoords ? p2.y : (p2.price || 0);

        const dx = x2 - x1;
        const dy = -(y2 - y1); // Đảo ngược vì trục Y SVG hướng xuống dưới

        let angle = 0;
        if (dx === 0 && dy === 0) {
            angle = 0;
        } else {
            angle = Number((Math.atan2(dy, dx) * (180 / Math.PI)).toFixed(1));
        }

        const deltaPrice = (p2.price !== undefined && p1.price !== undefined) ? Number((p2.price - p1.price).toFixed(3)) : Number(dy.toFixed(3));
        const deltaTime = (p2.time !== undefined && p1.time !== undefined) ? (p2.time - p1.time) : dx;
        const slope = deltaTime !== 0 ? Number((deltaPrice / deltaTime).toFixed(4)) : (deltaPrice === 0 ? 0 : (deltaPrice > 0 ? Infinity : -Infinity));

        const result = {
            angleDeg: angle,
            slope: slope,
            deltaPrice: deltaPrice,
            deltaTime: deltaTime
        };
        result.valueOf = () => angle;
        result.toString = () => String(angle);
        return result;
    },

    // 1.2. Hồi quy tuyến tính bình phương tối thiểu (OLS Linear Regression Trend)
    calculateRegressionTrend(dataPoints) {
        if (!Array.isArray(dataPoints) || dataPoints.length < 2) {
            return { slope: 0, intercept: 0, stdDev: 0, predict: () => 0 };
        }
        const n = dataPoints.length;
        let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0;
        for (let i = 0; i < n; i++) {
            const pt = dataPoints[i];
            const x = pt.x !== undefined ? pt.x : (pt.time || i);
            const y = pt.y !== undefined ? pt.y : pt.price;
            sumX += x;
            sumY += y;
            sumXY += x * y;
            sumX2 += x * x;
        }
        const denominator = n * sumX2 - sumX * sumX;
        const slope = denominator !== 0 ? (n * sumXY - sumX * sumY) / denominator : 0;
        const intercept = (sumY - slope * sumX) / n;

        // Tính độ lệch chuẩn của phần dư (Residual Standard Deviation)
        let sumResidualSq = 0;
        for (let i = 0; i < n; i++) {
            const pt = dataPoints[i];
            const x = pt.x !== undefined ? pt.x : (pt.time || i);
            const y = pt.y !== undefined ? pt.y : pt.price;
            const pred = slope * x + intercept;
            sumResidualSq += (y - pred) * (y - pred);
        }
        const stdDev = Math.sqrt(sumResidualSq / n);

        return {
            slope: Number(slope.toFixed(6)),
            intercept: Number(intercept.toFixed(4)),
            stdDev: Number(stdDev.toFixed(4)),
            predict: (x) => slope * x + intercept
        };
    },

    // 1.3. Andrews' Pitchfork và các biến thể (Schiff, Modified Schiff, Inside)
    calculatePitchfork(p1, p2, p3, variant = 'standard') {
        const midP2P3 = {
            x: (p2.x + p3.x) / 2,
            y: (p2.y + p3.y) / 2
        };

        let basePoint = { x: p1.x, y: p1.y };
        if (variant === 'schiff') {
            basePoint = { x: p1.x, y: (p1.y + p2.y) / 2 };
        } else if (variant === 'mod_schiff') {
            basePoint = { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 };
        } else if (variant === 'inside') {
            basePoint = { x: p1.x, y: p1.y };
        }

        const dx = midP2P3.x - basePoint.x;
        const dy = midP2P3.y - basePoint.y;
        const len = Math.hypot(dx, dy) || 1;
        const scale = 4000 / len;

        const medianEnd = { x: basePoint.x + dx * scale, y: basePoint.y + dy * scale };
        const upperEnd = { x: p2.x + dx * scale, y: p2.y + dy * scale };
        const lowerEnd = { x: p3.x + dx * scale, y: p3.y + dy * scale };

        return {
            variant: variant,
            base: basePoint,
            midpoint: midP2P3,
            medianLine: [basePoint, medianEnd],
            upperLine: [p2, upperEnd],
            lowerLine: [p3, lowerEnd],
            polygon: [p2, upperEnd, lowerEnd, p3]
        };
    },

    // 1.4. Fib Time Zone (Dãy thời gian Fibonacci)
    calculateFibTimeZone(p1Time, barIntervalSeconds = 60, count = 8) {
        const fibs = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144];
        const selected = fibs.slice(0, count);
        return selected.map((fib, idx) => ({
            index: idx + 1,
            fib: fib,
            time: p1Time + fib * barIntervalSeconds
        }));
    },

    // 1.5. Lưới góc Gann (Gann Angles)
    calculateGannAngles(x1, y1, x2, y2) {
        const dx = x2 - x1;
        const dy = y2 - y1;
        const ratios = [
            { label: '1x8', factor: 0.125 },
            { label: '1x4', factor: 0.25 },
            { label: '1x3', factor: 0.333 },
            { label: '1x2', factor: 0.5 },
            { label: '1x1', factor: 1.0 },
            { label: '2x1', factor: 2.0 },
            { label: '3x1', factor: 3.0 },
            { label: '4x1', factor: 4.0 },
            { label: '8x1', factor: 8.0 }
        ];

        return ratios.map(r => ({
            label: r.label,
            endX: x2,
            endY: y1 + dy * r.factor
        }));
    },

    // 1.6. Long & Short Position Calculator (Khớp logic backtest_engine.py)
    calculatePositionRiskReward(
        entryPrice,
        slPrice,
        tpPrice,
        isLong = true,
        lot = 0.1,
        spread = 0.2,
        commission = 0.0,
        contractSize = 100.0,
        pointSize = 0.1
    ) {
        let riskPrice = 0;
        let rewardPrice = 0;
        let targetPnL = 0;
        let stopPnL = 0;

        const ep = Number(entryPrice) || 0;
        const sl = Number(slPrice) || 0;
        const tp = Number(tpPrice) || 0;
        const l = Number(lot) || 0.1;
        const sp = Number(spread) || 0.0;
        const comm = Number(commission) || 0.0;
        const cs = Number(contractSize) || 100.0;
        const ps = Number(pointSize) || 0.1;

        if (isLong) {
            riskPrice = Math.max(0, ep - sl);
            rewardPrice = Math.max(0, tp - ep);
            // Long entry = ep + spread (Ask), Exit at Bid
            targetPnL = (tp - (ep + sp)) * cs * l - comm;
            stopPnL = (sl - (ep + sp)) * cs * l - comm;
        } else {
            // Short entry = ep (Bid), Exit at Ask = exit + spread
            riskPrice = Math.max(0, sl - ep);
            rewardPrice = Math.max(0, ep - tp);
            targetPnL = (ep - (tp + sp)) * cs * l - comm;
            stopPnL = (ep - (sl + sp)) * cs * l - comm;
        }

        const rrRatio = riskPrice > 0 ? Number((rewardPrice / riskPrice).toFixed(2)) : 0;
        const riskPips = ps > 0 ? Number((riskPrice / ps).toFixed(1)) : 0;
        const rewardPips = ps > 0 ? Number((rewardPrice / ps).toFixed(1)) : 0;
        const formattedTargetPnL = Number(targetPnL.toFixed(2));
        const formattedStopPnL = Number(stopPnL.toFixed(2));

        return {
            isLong: isLong,
            entryPrice: ep,
            slPrice: sl,
            tpPrice: tp,
            riskPrice: Number(riskPrice.toFixed(3)),
            rewardPrice: Number(rewardPrice.toFixed(3)),
            riskRewardRatio: rrRatio,
            riskPips: riskPips,
            rewardPips: rewardPips,
            targetPnL: formattedTargetPnL,
            stopPnL: formattedStopPnL,
            riskAmount: Number(Math.abs(formattedStopPnL).toFixed(2)),
            rewardAmount: Number(Math.max(0, formattedTargetPnL).toFixed(2))
        };
    },

    // 1.7. Đo Date & Price Range kết hợp và Volume
    calculateDatePriceRange(p1, p2, t1, t2, candleCount = 0, volumeSum = 0) {
        const base = this.calculateRulerMetrics(p1, p2, t1, t2, candleCount);
        base.volumeSum = Number(volumeSum) || 0;
        base.pips = Number((Math.abs(base.endPrice - base.startPrice) / 0.1).toFixed(1));
        return base;
    },

    // 1.8. Khoảng cách từ điểm tới Polyline
    distanceToPolyline(px, py, points) {
        if (!Array.isArray(points) || points.length < 2) return Infinity;
        let minDist = Infinity;
        for (let i = 0; i < points.length - 1; i++) {
            const d = this.distanceToSegment(px, py, points[i].x, points[i].y, points[i + 1].x, points[i + 1].y);
            if (d < minDist) minDist = d;
        }
        return minDist;
    },

    // 1.9. Hình tròn & Khoảng cách tới đường tròn
    calculateCircle(x1, y1, x2, y2) {
        return {
            cx: x1,
            cy: y1,
            r: Math.hypot(x2 - x1, y2 - y1)
        };
    },

    distanceToCircle(px, py, cx, cy, r) {
        const distToCenter = Math.hypot(px - cx, py - cy);
        return Math.abs(distToCenter - r);
    },

    isPointInCircle(px, py, cx, cy, r) {
        return Math.hypot(px - cx, py - cy) <= r;
    },

    // 1.10. Điểm nằm trong tam giác (Barycentric Coordinates)
    isPointInTriangle(px, py, x1, y1, x2, y2, x3, y3) {
        const area = 0.5 * (-y2 * x3 + y1 * (-x2 + x3) + x1 * (y2 - y3) + x2 * y3);
        const s = 1 / (2 * area) * (y1 * x3 - x1 * y3 + (y3 - y1) * px + (x1 - x3) * py);
        const t = 1 / (2 * area) * (x1 * y2 - y1 * x2 + (y1 - y2) * px + (x2 - x1) * py);
        return s >= 0 && t >= 0 && (1 - s - t) >= 0;
    },

    // 1.11. Tính tỷ lệ thoái lui ABCD
    calculateABCD(pA, pB, pC, pD) {
        const ab = Math.abs(pB - pA);
        const bc = Math.abs(pC - pB);
        const cd = Math.abs(pD - pC);
        return {
            ab: ab,
            bc: bc,
            cd: cd,
            retracementBC: ab > 0 ? Number((bc / ab).toFixed(3)) : 0,
            extensionCD: bc > 0 ? Number((cd / bc).toFixed(3)) : 0
        };
    }
};

/**
 * 2. DRAWING TOOL REGISTRY (QUẢN LÝ METADATA CÔNG CỤ VẼ)
 */
const DrawingToolRegistry = {
    CATEGORIES: {
        cursor: { id: 'cursor', name: 'Con trỏ', icon: '↖️' },
        trend: { id: 'trend', name: 'Đường xu hướng', icon: '╱' },
        channels: { id: 'channels', name: 'Kênh & Pitchfork', icon: '⫽' },
        fib: { id: 'fib', name: 'Fibonacci & Gann', icon: '≋' },
        shapes: { id: 'shapes', name: 'Hình học & Cọ vẽ', icon: '▭' },
        patterns: { id: 'patterns', name: 'Mẫu hình kỹ thuật', icon: '📐' },
        forecast: { id: 'forecast', name: 'Dự báo & Đo lường', icon: '📊' },
        annotations: { id: 'annotations', name: 'Ghi chú & Nhãn', icon: '📝' }
    },

    TOOLS: {
        // Cursor
        cursor: { id: 'cursor', name: 'Chọn & Di chuyển', category: 'cursor', icon: '↖️', points: 0 },
        eraser: { id: 'eraser', name: 'Tẩy nét vẽ', category: 'cursor', icon: '🧹', points: 0 },

        // Trend tools
        trendline: { id: 'trendline', name: 'Đường xu hướng', category: 'trend', icon: '╱', points: 2 },
        ray: { id: 'ray', name: 'Tia kéo dài', category: 'trend', icon: '↗️', points: 2 },
        extended: { id: 'extended', name: 'Đường kéo dài 2 phía', category: 'trend', icon: '↔️', points: 2 },
        horizontal: { id: 'horizontal', name: 'Đường ngang', category: 'trend', icon: '─', points: 1 },
        horizontal_ray: { id: 'horizontal_ray', name: 'Tia ngang', category: 'trend', icon: '⟶', points: 1 },
        vertical: { id: 'vertical', name: 'Đường dọc', category: 'trend', icon: '│', points: 1 },
        crossline: { id: 'crossline', name: 'Chữ thập', category: 'trend', icon: '┼', points: 1 },
        info_line: { id: 'info_line', name: 'Đường thông số', category: 'trend', icon: 'ℹ️', points: 2, hasStats: true },
        trend_angle: { id: 'trend_angle', name: 'Góc xu hướng', category: 'trend', icon: '∠', points: 2, hasStats: true },

        // Channels & Pitchfork
        channel: { id: 'channel', name: 'Kênh song song', category: 'channels', icon: '⫽', points: 3 },
        regression_trend: { id: 'regression_trend', name: 'Kênh hồi quy (OLS)', category: 'channels', icon: '📈', points: 2 },
        pitchfork: { id: 'pitchfork', name: 'Andrews Pitchfork', category: 'channels', icon: 'ψ', points: 3 },
        schiff_pitchfork: { id: 'schiff_pitchfork', name: 'Schiff Pitchfork', category: 'channels', icon: '⑂', points: 3 },
        mod_schiff_pitchfork: { id: 'mod_schiff_pitchfork', name: 'Modified Schiff', category: 'channels', icon: '⑃', points: 3 },
        inside_pitchfork: { id: 'inside_pitchfork', name: 'Inside Pitchfork', category: 'channels', icon: '⑄', points: 3 },

        // Fib & Gann
        fib_retracement: { id: 'fib_retracement', name: 'Fibonacci Thoái lui', category: 'fib', icon: '≋', points: 2 },
        fib_extension: { id: 'fib_extension', name: 'Fibonacci Mở rộng', category: 'fib', icon: '⤨', points: 3 },
        fib_timezone: { id: 'fib_timezone', name: 'Fibonacci Time Zone', category: 'fib', icon: '⏱️', points: 2 },
        gann_box: { id: 'gann_box', name: 'Hộp Gann (Gann Box)', category: 'fib', icon: '⊞', points: 2 },
        gann_fan: { id: 'gann_fan', name: 'Quạt Gann (Gann Fan)', category: 'fib', icon: '🪭', points: 2 },

        // Shapes & Brush
        rectangle: { id: 'rectangle', name: 'Hình chữ nhật', category: 'shapes', icon: '▭', points: 2 },
        circle: { id: 'circle', name: 'Hình tròn', category: 'shapes', icon: '⭕', points: 2 },
        triangle: { id: 'triangle', name: 'Hình tam giác', category: 'shapes', icon: '△', points: 3 },
        polyline: { id: 'polyline', name: 'Đường gấp khúc (Polyline)', category: 'shapes', icon: '〰️', points: -1 },
        brush: { id: 'brush', name: 'Cọ vẽ tự do (Brush)', category: 'shapes', icon: '🖌️', points: -1 },
        highlighter: { id: 'highlighter', name: 'Bút dạ quang (Highlighter)', category: 'shapes', icon: '🖍️', points: -1 },

        // Patterns
        abcd: { id: 'abcd', name: 'Mẫu hình ABCD', category: 'patterns', icon: '🔤', points: 4 },
        head_shoulders: { id: 'head_shoulders', name: 'Vai Đầu Vai (Head & Shoulders)', category: 'patterns', icon: '👤', points: 7 },
        elliott_wave_15: { id: 'elliott_wave_15', name: 'Sóng Elliott Đẩy (1-5)', category: 'patterns', icon: '5️⃣', points: 5 },
        elliott_wave_abc: { id: 'elliott_wave_abc', name: 'Sóng Elliott Chỉnh (A-B-C)', category: 'patterns', icon: '🔠', points: 4 },

        // Forecast & Position
        long_position: { id: 'long_position', name: 'Vị thế Mua (Long Position)', category: 'forecast', icon: '🟢', points: 3, hasStats: true },
        short_position: { id: 'short_position', name: 'Vị thế Bán (Short Position)', category: 'forecast', icon: '🔴', points: 3, hasStats: true },
        date_price_range: { id: 'date_price_range', name: 'Vùng Giá & Thời gian', category: 'forecast', icon: '⤧', points: 2, hasStats: true },
        ruler: { id: 'ruler', name: 'Thước đo (Ruler)', category: 'forecast', icon: '📏', points: 2, hasStats: true },
        price_range: { id: 'price_range', name: 'Khoảng giá', category: 'forecast', icon: '↕️', points: 2, hasStats: true },
        date_range: { id: 'date_range', name: 'Khoảng thời gian', category: 'forecast', icon: '↔', points: 2, hasStats: true },

        // Annotations
        text: { id: 'text', name: 'Ghi chú văn bản', category: 'annotations', icon: '📝', points: 1, hasText: true },
        callout: { id: 'callout', name: 'Bong bóng chú thích', category: 'annotations', icon: '💬', points: 2, hasText: true },
        arrow: { id: 'arrow', name: 'Mũi tên', category: 'annotations', icon: '➔', points: 2 }
    },

    getTool(id) {
        return this.TOOLS[id] || null;
    },

    getToolsByCategory(catId) {
        return Object.values(this.TOOLS).filter(t => t.category === catId);
    },

    getAllTools() {
        return Object.values(this.TOOLS);
    },

    getCategories() {
        return Object.values(this.CATEGORIES);
    }
};

/**
 * 3. DRAWING MODEL FACTORY, MIGRATION & VALIDATOR (SCHEMA V2)
 */
function migrateDrawingV1toV2(d) {
    if (!d || typeof d !== 'object') return null;
    if (d.schemaVersion === 2) return d;

    const points = (d.points || []).map(p => ({
        time: Number(p.time),
        price: Number(p.price)
    }));

    return {
        id: d.id,
        type: d.type,
        points: points,
        style: {
            color: d.style?.color || '#2962ff',
            width: d.style?.width !== undefined ? Number(d.style.width) : 2,
            lineStyle: d.style?.lineStyle || 'solid',
            opacity: d.style?.opacity !== undefined ? Number(d.style.opacity) : 1.0,
            fillColor: d.style?.fillColor || 'rgba(41, 98, 255, 0.15)',
            fillOpacity: 0.15,
            fontSize: d.style?.fontSize || 13,
            textColor: d.style?.textColor || '#ffffff',
            fontFamily: 'sans-serif',
            bold: false,
            italic: false,
            showLabel: true,
            extendLeft: !!d.style?.extendLeft,
            extendRight: !!d.style?.extendRight,
            customLevels: d.style?.customLevels || null
        },
        text: d.text || '',
        coordinates: {
            prices: points.map(p => p.price),
            times: points.map(p => p.time)
        },
        visibility: {
            allTimeframes: d.scope !== 'timeframe',
            timeframes: d.targetTimeframe ? [d.targetTimeframe] : ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1']
        },
        stats: {
            showStats: ['ruler', 'price_range', 'date_range', 'date_price_range', 'info_line', 'long_position', 'short_position'].includes(d.type)
        },
        alert: {
            enabled: !!d.alert?.enabled,
            condition: d.alert?.condition || 'touch',
            triggered: !!d.alert?.triggered,
            triggerCount: d.alert?.triggerCount || 0,
            onceOnly: d.alert?.onceOnly !== undefined ? !!d.alert.onceOnly : true,
            lastCandleTime: d.alert?.lastCandleTime || null,
            lastCandleState: d.alert?.lastCandleState || null
        },
        locked: !!d.locked,
        hidden: d.hidden !== undefined ? !!d.hidden : (d.visible !== undefined ? !d.visible : false),
        visible: d.visible !== undefined ? !!d.visible : true,
        scope: d.scope || 'symbol',
        targetTimeframe: d.targetTimeframe || null,
        visibleInReplay: d.visibleInReplay || 'all',
        createdAtTime: d.createdAtTime || (points.length > 0 ? points[0].time : Math.floor(Date.now() / 1000)),
        createdAt: d.createdAt || Date.now(),
        updatedAt: d.updatedAt || Date.now(),
        schemaVersion: 2
    };
}

const DrawingModel = {
    SUPPORTED_TYPES: [
        'trendline', 'ray', 'extended', 'horizontal', 'horizontal_ray', 'vertical', 'crossline', 'info_line', 'trend_angle',
        'channel', 'regression_trend', 'pitchfork', 'schiff_pitchfork', 'mod_schiff_pitchfork', 'inside_pitchfork',
        'fib_retracement', 'fib_extension', 'fib_timezone', 'gann_box', 'gann_fan',
        'rectangle', 'circle', 'triangle', 'polyline', 'brush', 'highlighter',
        'abcd', 'head_shoulders', 'elliott_wave_15', 'elliott_wave_abc',
        'long_position', 'short_position', 'date_price_range', 'ruler', 'price_range', 'date_range',
        'text', 'callout', 'arrow'
    ],

    REQUIRED_POINTS: {
        horizontal: 1,
        vertical: 1,
        horizontal_ray: 1,
        crossline: 1,
        text: 1,
        trendline: 2,
        ray: 2,
        extended: 2,
        info_line: 2,
        trend_angle: 2,
        rectangle: 2,
        circle: 2,
        ruler: 2,
        price_range: 2,
        date_range: 2,
        date_price_range: 2,
        arrow: 2,
        callout: 2,
        fib_retracement: 2,
        fib_timezone: 2,
        gann_box: 2,
        gann_fan: 2,
        regression_trend: 2,
        channel: 3,
        fib_extension: 3,
        pitchfork: 3,
        schiff_pitchfork: 3,
        mod_schiff_pitchfork: 3,
        inside_pitchfork: 3,
        triangle: 3,
        long_position: 3,
        short_position: 3,
        abcd: 4,
        elliott_wave_abc: 4,
        elliott_wave_15: 5,
        head_shoulders: 7,
        polyline: 2,
        brush: 2,
        highlighter: 2
    },

    create(type, points = [], options = {}) {
        if (!this.SUPPORTED_TYPES.includes(type)) {
            throw new Error(`Loại công cụ vẽ không hỗ trợ: ${type}`);
        }

        const now = Date.now();
        const mappedPoints = points.map(p => ({
            time: Number(p.time),
            price: Number(p.price)
        }));
        const maxPointTime = mappedPoints.length > 0 ? Math.max(...mappedPoints.map(p => p.time || 0)) : Math.floor(now / 1000);

        return {
            id: options.id || ('d_' + now + '_' + Math.random().toString(36).substr(2, 9)),
            type: type,
            points: mappedPoints,
            style: {
                color: options.color || '#2962ff',
                width: options.width !== undefined ? Number(options.width) : 2,
                lineStyle: options.lineStyle || 'solid', // solid, dashed, dotted
                opacity: options.opacity !== undefined ? Number(options.opacity) : 1.0,
                fillColor: options.fillColor !== undefined ? options.fillColor : 'rgba(41, 98, 255, 0.15)',
                fillOpacity: 0.15,
                fontSize: options.fontSize || 13,
                textColor: options.textColor || '#ffffff',
                fontFamily: 'sans-serif',
                bold: false,
                italic: false,
                showLabel: options.showLabel !== undefined ? !!options.showLabel : true,
                extendLeft: !!options.extendLeft,
                extendRight: !!options.extendRight,
                customLevels: options.customLevels || null
            },
            text: options.text || '',
            coordinates: {
                prices: mappedPoints.map(p => p.price),
                times: mappedPoints.map(p => p.time)
            },
            visibility: {
                allTimeframes: options.allTimeframes !== undefined ? !!options.allTimeframes : (options.scope !== 'timeframe'),
                timeframes: options.targetTimeframe ? [options.targetTimeframe] : ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1']
            },
            stats: {
                showStats: ['ruler', 'price_range', 'date_range', 'date_price_range', 'info_line', 'long_position', 'short_position'].includes(type)
            },
            alert: {
                enabled: options.alert?.enabled !== undefined ? !!options.alert.enabled : !!options.alertEnabled,
                condition: options.alert?.condition || options.alertCondition || 'touch',
                triggered: false,
                triggerCount: 0,
                onceOnly: options.alert?.onceOnly !== undefined ? !!options.alert.onceOnly : (options.alertOnceOnly !== undefined ? !!options.alertOnceOnly : true),
                lastCandleTime: null,
                lastCandleState: null
            },
            scope: options.scope || 'symbol', // "symbol" | "timeframe"
            targetTimeframe: options.targetTimeframe || null,
            locked: !!options.locked,
            hidden: options.hidden !== undefined ? !!options.hidden : (options.visible !== undefined ? !options.visible : false),
            visible: options.visible !== undefined ? !!options.visible : true,
            createdAtTime: options.createdAtTime || maxPointTime,
            visibleInReplay: options.visibleInReplay || 'all', // "all" | "past_only"
            createdAt: options.createdAt || now,
            updatedAt: options.updatedAt || now,
            schemaVersion: 2
        };
    },

    validate(drawing) {
        if (!drawing || typeof drawing !== 'object') return false;
        if (typeof drawing.id !== 'string' || !drawing.id) return false;
        if (!this.SUPPORTED_TYPES.includes(drawing.type)) return false;
        if (!Array.isArray(drawing.points)) return false;
        for (const pt of drawing.points) {
            if (typeof pt.time !== 'number' || isNaN(pt.time)) return false;
            if (typeof pt.price !== 'number' || isNaN(pt.price)) return false;
        }
        return true;
    }
};

/**
 * 4. DRAWING MANAGER (QUẢN LÝ TOÀN BỘ VÒNG ĐỜI DRAWING TRÊN BIỂU ĐỒ)
 */
class DrawingManager {
    constructor(chartInstance, containerId, options = {}) {
        this.chartInstance = chartInstance;
        this.container = typeof containerId === 'string' ? document.getElementById(containerId) : containerId;
        this.options = options;
        this.symbol = options.symbol || 'XAUUSD';
        this.currentTimeframe = options.timeframe || 'M15';

        this.drawings = [];
        this.selectedId = null;
        this.activeTool = 'cursor'; // 'cursor' = Select mode, các tool khác = Draw mode
        this.isDrawing = false;
        this.inProgressPoints = [];
        this.dragState = null; // { type: 'anchor'|'body', drawingId, pointIndex, startCoord, origPoints }
        this.magnetEnabled = false;

        // Undo / Redo stacks (tối đa 50 bước)
        this.undoStack = [];
        this.redoStack = [];
        this.maxHistory = 50;

        // Listeners & observers cleanup list
        this.disposers = [];

        // rAF batching & render loop guard
        this.renderPending = false;
        this.rafId = null;

        if (this.container && typeof document !== 'undefined') {
            this.initSvgOverlay();
            this.initEvents();
            this.loadFromStorage();
        }
    }

    initSvgOverlay() {
        // Tạo SVG Overlay nằm trên canvas biểu đồ
        this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        this.svg.setAttribute('class', 'drawing-overlay');
        this.svg.style.position = 'absolute';
        this.svg.style.left = '0';
        this.svg.style.top = '0';
        this.svg.style.width = '100%';
        this.svg.style.height = '100%';
        this.svg.style.zIndex = '10';
        this.svg.style.overflow = 'hidden';
        this.svg.style.pointerEvents = 'none'; // Mặc định ở Select Mode

        // Nhóm các nét vẽ
        this.drawingsGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        this.drawingsGroup.setAttribute('class', 'drawings-group');
        this.svg.appendChild(this.drawingsGroup);

        // Nhóm nét vẽ preview khi đang kéo thả
        this.previewGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        this.previewGroup.setAttribute('class', 'preview-group');
        this.svg.appendChild(this.previewGroup);

        // Nhóm các điểm neo (anchor handles) của nét vẽ đang chọn
        this.handlesGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        this.handlesGroup.setAttribute('class', 'handles-group');
        this.svg.appendChild(this.handlesGroup);

        this.container.appendChild(this.svg);
    }

    initEvents() {
        if (!this.svg) return;

        // Xử lý sự kiện chuột trên SVG overlay
        const onMouseDown = (e) => this.handleMouseDown(e);
        const onMouseMove = (e) => this.handleMouseMove(e);
        const onMouseUp = (e) => this.handleMouseUp(e);
        const onDblClick = (e) => this.handleDblClick(e);
        const onContextMenu = (e) => this.handleContextMenu(e);

        this.svg.addEventListener('mousedown', onMouseDown);
        window.addEventListener('mousemove', onMouseMove);
        window.addEventListener('mouseup', onMouseUp);
        this.svg.addEventListener('dblclick', onDblClick);
        this.svg.addEventListener('contextmenu', onContextMenu);

        this.disposers.push(() => {
            this.svg.removeEventListener('mousedown', onMouseDown);
            window.removeEventListener('mousemove', onMouseMove);
            window.removeEventListener('mouseup', onMouseUp);
            this.svg.removeEventListener('dblclick', onDblClick);
            this.svg.removeEventListener('contextmenu', onContextMenu);
        });

        // Lắng nghe zoom/pan từ Lightweight Charts
        if (this.chartInstance && this.chartInstance.chart) {
            const chart = this.chartInstance.chart;
            const onRangeChange = () => this.requestRender();
            chart.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange);
            chart.timeScale().subscribeVisibleTimeRangeChange(onRangeChange);

            this.disposers.push(() => {
                try {
                    chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange);
                    chart.timeScale().unsubscribeVisibleTimeRangeChange(onRangeChange);
                } catch (e) {
                    // Ignore if chart already removed
                }
            });
        }

        // Lắng nghe thao tác kéo trên chart container (kéo thanh giá bên phải, kéo thanh thời gian bên dưới, pan)
        if (this.container) {
            let isChartDragging = false;

            const onContainerPointerDown = (e) => {
                if (e.button !== 0) return;
                // Nếu đang vẽ nét mới hoặc đang kéo handle/body của drawing thì không xử lý
                if (this.isDrawing || this.dragState) return;
                isChartDragging = true;
            };

            const onWindowPointerMove = () => {
                if (isChartDragging) {
                    this.requestRender();
                }
            };

            const onWindowPointerUp = () => {
                if (isChartDragging) {
                    isChartDragging = false;
                    this.requestRender();
                }
            };

            const onContainerWheel = () => {
                this.requestRender();
            };

            const onContainerDblClick = () => {
                this.requestRender();
            };

            const onWindowResize = () => {
                this.requestRender();
            };

            if (typeof window !== 'undefined' && window.PointerEvent) {
                this.container.addEventListener('pointerdown', onContainerPointerDown);
                window.addEventListener('pointermove', onWindowPointerMove);
                window.addEventListener('pointerup', onWindowPointerUp);
            } else {
                this.container.addEventListener('mousedown', onContainerPointerDown);
                window.addEventListener('mousemove', onWindowPointerMove);
                window.addEventListener('mouseup', onWindowPointerUp);
            }

            this.container.addEventListener('wheel', onContainerWheel, { passive: true });
            this.container.addEventListener('dblclick', onContainerDblClick);
            window.addEventListener('resize', onWindowResize);

            this.disposers.push(() => {
                if (typeof window !== 'undefined' && window.PointerEvent) {
                    this.container.removeEventListener('pointerdown', onContainerPointerDown);
                    window.removeEventListener('pointermove', onWindowPointerMove);
                    window.removeEventListener('pointerup', onWindowPointerUp);
                } else {
                    this.container.removeEventListener('mousedown', onContainerPointerDown);
                    window.removeEventListener('mousemove', onWindowPointerMove);
                    window.removeEventListener('mouseup', onWindowPointerUp);
                }
                this.container.removeEventListener('wheel', onContainerWheel);
                this.container.removeEventListener('dblclick', onContainerDblClick);
                window.removeEventListener('resize', onWindowResize);
            });
        }
    }

    // ==========================================
    // STATE MACHINE: SELECT MODE VS DRAW MODE
    // ==========================================
    setActiveTool(tool) {
        this.activeTool = tool;
        this.inProgressPoints = [];
        this.isDrawing = false;
        this.clearPreview();

        if (tool === 'cursor') {
            // SELECT MODE: Vùng trống để pointer-events = none cho chart nhận pan/zoom
            if (this.svg) {
                this.svg.style.pointerEvents = 'none';
                this.svg.style.cursor = 'default';
            }
        } else {
            // DRAW MODE: SVG nhận toàn bộ sự kiện để đặt điểm neo
            this.deselect();
            if (this.svg) {
                this.svg.style.pointerEvents = 'all';
                this.svg.style.cursor = 'crosshair';
            }
        }
        this.requestRender();
        this.notifyStateChange();
    }

    // ==========================================
    // CHUYỂN ĐỔI TỌA ĐỘ (COORDINATE CONVERSION)
    // ==========================================
    ensureFutureOffset(targetLogical) {
        if (!this.chartInstance || !this.chartInstance.chart) return;
        const candles = this.chartInstance.currentCandles;
        if (!candles || candles.length === 0) return;

        try {
            const timeScale = this.chartInstance.chart.timeScale();
            const lastIdx = candles.length - 1;
            if (targetLogical > lastIdx) {
                const neededOffset = Math.ceil(targetLogical - lastIdx + 5);
                let currentOffset = 10;
                if (typeof timeScale.options === 'function') {
                    const opts = timeScale.options();
                    if (opts && typeof opts.rightOffset === 'number') {
                        currentOffset = opts.rightOffset;
                    }
                }
                if (neededOffset > currentOffset && typeof timeScale.applyOptions === 'function') {
                    timeScale.applyOptions({ rightOffset: neededOffset });
                }
            }
        } catch (e) {
            // Bỏ qua nếu chart mock không hỗ trợ options/applyOptions
        }
    }

    timePriceToCoord(time, price) {
        if (!this.chartInstance || !this.chartInstance.chart || !this.chartInstance.candleSeries) {
            return null;
        }
        try {
            const timeScale = this.chartInstance.chart.timeScale();
            const series = this.chartInstance.candleSeries;
            const candles = this.chartInstance.currentCandles;

            const y = series.priceToCoordinate(price);
            if (y === null || isNaN(y)) {
                return null;
            }

            let x = null;
            if (candles && candles.length > 0) {
                const lastIdx = candles.length - 1;
                const last = candles[lastIdx];
                const first = candles[0];

                if (time > last.time) {
                    // Vùng tương lai: luôn ngoại suy qua logicalToCoordinate, không phụ thuộc lastX
                    x = this.extrapolateTimeToCoord(time);
                } else if (time < first.time) {
                    // Vùng quá khứ
                    x = this.extrapolateTimeToCoord(time);
                } else {
                    // Trong dải nến: thử timeToCoordinate trước
                    x = timeScale.timeToCoordinate(time);
                    if (x === null || isNaN(x)) {
                        x = this.extrapolateTimeToCoord(time);
                    }
                }
            } else {
                x = timeScale.timeToCoordinate(time);
            }

            if (x === null || isNaN(x)) {
                return null;
            }
            return { x, y };
        } catch (e) {
            return null;
        }
    }

    coordToTimePrice(x, y) {
        if (!this.chartInstance || !this.chartInstance.chart || !this.chartInstance.candleSeries) {
            return null;
        }
        try {
            const timeScale = this.chartInstance.chart.timeScale();
            const series = this.chartInstance.candleSeries;
            const candles = this.chartInstance.currentCandles;

            let p = series.coordinateToPrice(y);
            if (p === null || isNaN(p)) {
                return null;
            }

            let t = null;
            if (candles && candles.length > 0) {
                const lastIdx = candles.length - 1;
                const last = candles[lastIdx];
                const first = candles[0];
                const interval = this.getCandleIntervalSeconds(candles);

                // 1. Ưu tiên kiểm tra logical index (không bị clamp vào nến cuối)
                let logical = null;
                if (typeof timeScale.coordinateToLogical === 'function') {
                    logical = timeScale.coordinateToLogical(x);
                }

                if (logical !== null && logical !== undefined && !isNaN(logical) && interval) {
                    if (logical > lastIdx) {
                        // Vùng tương lai (bên phải nến cuối)
                        const diffBars = logical - lastIdx;
                        t = Math.round(last.time + diffBars * interval);
                        this.ensureFutureOffset(logical);
                    } else if (logical < 0) {
                        // Vùng quá khứ (bên trái nến đầu)
                        t = Math.round(first.time + logical * interval);
                    } else {
                        // Nằm trong dải nến
                        t = timeScale.coordinateToTime(x);
                        if (t === null || t === undefined || isNaN(Number(t))) {
                            const idx = Math.max(0, Math.min(lastIdx, Math.round(logical)));
                            t = candles[idx].time;
                        }
                    }
                }

                // 2. Fallback nếu không có coordinateToLogical hoặc chưa tính được t
                if (t === null || t === undefined || isNaN(Number(t))) {
                    t = timeScale.coordinateToTime(x);
                    if (t === null || isNaN(Number(t))) {
                        t = this.extrapolateCoordToTime(x);
                    }
                }
            } else {
                t = timeScale.coordinateToTime(x);
            }

            if (t === null || isNaN(Number(t))) {
                return null;
            }

            // Xử lý Magnet Snap nếu bật
            if (this.magnetEnabled && candles && candles.length > 0) {
                const snapped = this.applyMagnetSnap(t, p, x, y);
                if (snapped) {
                    t = snapped.time;
                    p = snapped.price;
                }
            }

            return { time: Number(t), price: Number(p.toFixed(3)) };
        } catch (e) {
            return null;
        }
    }

    extrapolateCoordToTime(targetX) {
        const candles = this.chartInstance && this.chartInstance.currentCandles;
        if (!candles || candles.length === 0) return null;

        const timeScale = this.chartInstance.chart.timeScale();
        const lastIdx = candles.length - 1;
        const last = candles[lastIdx];
        const first = candles[0];
        const interval = this.getCandleIntervalSeconds(candles);
        if (!interval || !isFinite(interval)) return null;

        // Ưu tiên logical index
        if (typeof timeScale.coordinateToLogical === 'function') {
            const logical = timeScale.coordinateToLogical(targetX);
            if (logical !== null && logical !== undefined && !isNaN(logical)) {
                if (logical > lastIdx) {
                    const barsFromLast = logical - lastIdx;
                    this.ensureFutureOffset(logical);
                    return Math.round(last.time + barsFromLast * interval);
                } else if (logical < 0) {
                    return Math.round(first.time + logical * interval);
                } else {
                    const idx = Math.max(0, Math.min(lastIdx, Math.round(logical)));
                    return candles[idx].time;
                }
            }
        }

        // Fallback nếu không có coordinateToLogical (ví dụ mock test)
        let refX = null;
        if (typeof timeScale.logicalToCoordinate === 'function') {
            refX = timeScale.logicalToCoordinate(lastIdx);
        }
        if (refX === null && typeof timeScale.timeToCoordinate === 'function') {
            refX = timeScale.timeToCoordinate(last.time);
        }

        if (refX !== null && !isNaN(refX)) {
            const barSpacing = (timeScale.options && timeScale.options().barSpacing) || 6;
            const barsFromLast = (targetX - refX) / barSpacing;
            return Math.round(last.time + barsFromLast * interval);
        }

        return null;
    }

    getCandleIntervalSeconds(candles) {
        // 1. Thử tính toán median delta từ nhóm nến gần nhất (tối đa 30 nến) để loại trừ gap cuối tuần / nghỉ lễ
        if (candles && candles.length >= 2) {
            const sampleSize = Math.min(30, candles.length);
            const startIdx = candles.length - sampleSize;
            const diffs = [];

            for (let i = startIdx + 1; i < candles.length; i++) {
                const tPrev = Number(candles[i - 1].time);
                const tCurr = Number(candles[i].time);
                if (Number.isFinite(tPrev) && Number.isFinite(tCurr)) {
                    const diff = tCurr - tPrev;
                    if (diff > 0) {
                        diffs.push(diff);
                    }
                }
            }

            if (diffs.length > 0) {
                diffs.sort((a, b) => a - b);
                const mid = Math.floor(diffs.length / 2);
                const medianDiff = (diffs.length % 2 !== 0)
                    ? diffs[mid]
                    : Math.round((diffs[mid - 1] + diffs[mid]) / 2);

                if (medianDiff > 0 && isFinite(medianDiff)) {
                    return medianDiff;
                }
            }
        }

        // 2. Fallback dựa trên currentTimeframe (M1=60, M5=300, M15=900, M30=1800, H1=3600, H4=14400, D1=86400, W1=604800)
        const tf = this.currentTimeframe || (this.options && this.options.timeframe) || '';
        const match = String(tf).match(/^([MHDW])(\d+)$/i);
        if (match) {
            const units = { M: 60, H: 3600, D: 86400, W: 604800 };
            const unitMultiplier = units[match[1].toUpperCase()] || 60;
            return Number(match[2]) * unitMultiplier;
        }

        // 3. Fallback an toàn nếu có ít nhất 2 nến nhưng chỉ 1 delta
        if (candles && candles.length >= 2) {
            const diff = Number(candles[candles.length - 1].time) - Number(candles[candles.length - 2].time);
            if (diff > 0 && isFinite(diff)) return diff;
        }

        return null;
    }

    extrapolateTimeToCoord(targetTime) {
        const candles = this.chartInstance && this.chartInstance.currentCandles;
        if (!candles || candles.length === 0) return null;

        const timeScale = this.chartInstance.chart.timeScale();
        const first = candles[0];
        const lastIdx = candles.length - 1;
        const last = candles[lastIdx];
        const interval = this.getCandleIntervalSeconds(candles);
        if (!interval || !isFinite(interval)) return null;

        // 1. Tương lai (sau nến cuối) - dùng logicalToCoordinate không phụ thuộc lastX
        if (targetTime > last.time) {
            const diffBars = (targetTime - last.time) / interval;
            const targetLogical = lastIdx + diffBars;

            // Ưu tiên logicalToCoordinate
            if (typeof timeScale.logicalToCoordinate === 'function') {
                const coord = timeScale.logicalToCoordinate(targetLogical);
                if (coord !== null && !isNaN(coord)) {
                    return coord;
                }
            }

            // Fallback khi logicalToCoordinate không có (ví dụ mock test)
            let lastX = null;
            if (typeof timeScale.timeToCoordinate === 'function') {
                lastX = timeScale.timeToCoordinate(last.time);
            }
            if (lastX !== null && !isNaN(lastX)) {
                const barSpacing = (timeScale.options && timeScale.options().barSpacing) || 6;
                return lastX + diffBars * barSpacing;
            }
            return null;
        }

        // 2. Quá khứ (trước nến đầu)
        if (targetTime < first.time) {
            const diffBars = (targetTime - first.time) / interval;
            const targetLogical = diffBars;

            if (typeof timeScale.logicalToCoordinate === 'function') {
                const coord = timeScale.logicalToCoordinate(targetLogical);
                if (coord !== null && !isNaN(coord)) {
                    return coord;
                }
            }

            let firstX = null;
            if (typeof timeScale.timeToCoordinate === 'function') {
                firstX = timeScale.timeToCoordinate(first.time);
            }
            if (firstX !== null && !isNaN(firstX)) {
                const barSpacing = (timeScale.options && timeScale.options().barSpacing) || 6;
                return firstX + diffBars * barSpacing;
            }
            return null;
        }

        // 3. Trong khoảng [first.time, last.time] nhưng timeToCoordinate trả null (gap, weekend, v.v.)
        let low = 0, high = lastIdx;
        while (low <= high) {
            const mid = (low + high) >> 1;
            if (candles[mid].time === targetTime) {
                if (typeof timeScale.timeToCoordinate === 'function') {
                    const c = timeScale.timeToCoordinate(candles[mid].time);
                    if (c !== null && !isNaN(c)) return c;
                }
                if (typeof timeScale.logicalToCoordinate === 'function') {
                    return timeScale.logicalToCoordinate(mid);
                }
                return null;
            } else if (candles[mid].time < targetTime) {
                low = mid + 1;
            } else {
                high = mid - 1;
            }
        }

        const leftIdx = Math.max(0, Math.min(lastIdx, high));
        const rightIdx = Math.max(0, Math.min(lastIdx, low));
        const leftDiff = Math.abs(targetTime - candles[leftIdx].time);
        const rightDiff = Math.abs(targetTime - candles[rightIdx].time);
        const nearestIdx = leftDiff <= rightDiff ? leftIdx : rightIdx;

        if (typeof timeScale.logicalToCoordinate === 'function') {
            const fraction = (targetTime - candles[nearestIdx].time) / interval;
            const coord = timeScale.logicalToCoordinate(nearestIdx + fraction);
            if (coord !== null && !isNaN(coord)) return coord;
        }

        if (typeof timeScale.timeToCoordinate === 'function') {
            const c = timeScale.timeToCoordinate(candles[nearestIdx].time);
            if (c !== null && !isNaN(c)) return c;
        }

        return null;
    }

    applyMagnetSnap(time, price, screenX, screenY) {
        const candles = this.chartInstance.currentCandles;
        if (!candles || candles.length === 0) return null;

        // Tìm nến gần nhất với time
        let nearest = candles[0];
        let minDiff = Math.abs(time - candles[0].time);
        for (let i = 1; i < candles.length; i++) {
            const d = Math.abs(time - candles[i].time);
            if (d < minDiff) {
                minDiff = d;
                nearest = candles[i];
            }
        }

        const ohlc = [nearest.open, nearest.high, nearest.low, nearest.close];
        const series = this.chartInstance.candleSeries;
        let bestPrice = nearest.close;
        let minPixelDist = 25; // Ngưỡng hút nam châm 25px

        for (const p of ohlc) {
            const py = series.priceToCoordinate(p);
            if (py !== null) {
                const dist = Math.abs(screenY - py);
                if (dist < minPixelDist) {
                    minPixelDist = dist;
                    bestPrice = p;
                }
            }
        }

        return { time: nearest.time, price: bestPrice };
    }

    // ==========================================
    // UNDO / REDO (TỐI ĐA 50 BƯỚC)
    // ==========================================
    saveUndoState() {
        const state = JSON.stringify(this.drawings);
        this.undoStack.push(state);
        if (this.undoStack.length > this.maxHistory) {
            this.undoStack.shift();
        }
        this.redoStack = []; // Xóa redo khi có hành động mới
        this.saveToStorage();
        this.notifyStateChange();
    }

    undo() {
        if (this.undoStack.length === 0) return false;
        const currentState = JSON.stringify(this.drawings);
        this.redoStack.push(currentState);

        const prevState = this.undoStack.pop();
        this.drawings = JSON.parse(prevState);
        this.selectedId = null;
        this.requestRender();
        this.saveToStorage();
        this.notifyStateChange();
        return true;
    }

    redo() {
        if (this.redoStack.length === 0) return false;
        const currentState = JSON.stringify(this.drawings);
        this.undoStack.push(currentState);

        const nextState = this.redoStack.pop();
        this.drawings = JSON.parse(nextState);
        this.selectedId = null;
        this.requestRender();
        this.saveToStorage();
        this.notifyStateChange();
        return true;
    }

    canUndo() { return this.undoStack.length > 0; }
    canRedo() { return this.redoStack.length > 0; }

    // ==========================================
    // QUẢN LÝ DRAWING (CRUD & SELECTION)
    // ==========================================
    create(type, points, options = {}) {
        const drawing = DrawingModel.create(type, points, options);
        this.saveUndoState();
        this.drawings.push(drawing);
        this.requestRender();
        return drawing;
    }

    get(id) {
        return this.drawings.find(d => d.id === id) || null;
    }

    update(id, updates) {
        const d = this.get(id);
        if (!d || d.locked) return false;

        this.saveUndoState();
        Object.assign(d, updates, { updatedAt: Date.now() });
        this.requestRender();
        return true;
    }

    remove(id) {
        const idx = this.drawings.findIndex(d => d.id === id);
        if (idx === -1) return false;
        if (this.drawings[idx].locked) return false;

        this.saveUndoState();
        this.drawings.splice(idx, 1);
        if (this.selectedId === id) this.selectedId = null;
        this.requestRender();
        return true;
    }

    clear() {
        if (this.drawings.length === 0) return;
        this.saveUndoState();
        // Chỉ xóa drawing chưa bị lock
        this.drawings = this.drawings.filter(d => d.locked);
        this.selectedId = null;
        this.requestRender();
    }

    select(id) {
        if (this.selectedId === id) return;
        this.selectedId = id;
        this.requestRender();
        this.notifyStateChange();
    }

    deselect() {
        if (this.selectedId === null) return;
        this.selectedId = null;
        this.requestRender();
        this.notifyStateChange();
    }

    setLocked(id, locked) {
        const d = this.get(id);
        if (d) {
            this.saveUndoState();
            d.locked = !!locked;
            d.updatedAt = Date.now();
            this.requestRender();
            this.notifyStateChange();
        }
    }

    setVisible(id, visible) {
        const d = this.get(id);
        if (d) {
            this.saveUndoState();
            d.visible = !!visible;
            d.updatedAt = Date.now();
            this.requestRender();
            this.notifyStateChange();
        }
    }

    // ==========================================
    // SỰ KIỆN CHUỘT & HIT-TESTING
    // ==========================================
    getMouseCoord(e) {
        if (this.svg && typeof this.svg.getBoundingClientRect === 'function') {
            const rect = this.svg.getBoundingClientRect();
            return {
                x: e.clientX - rect.left,
                y: e.clientY - rect.top
            };
        }
        return {
            x: e.clientX || 0,
            y: e.clientY || 0
        };
    }

    handleMouseDown(e) {
        if (e.button !== 0) return; // Chỉ nhận chuột trái
        const coord = this.getMouseCoord(e);
        const tp = this.coordToTimePrice(coord.x, coord.y);
        if (!tp) return;

        // 1. Eraser Tool: Click xóa ngay nét vẽ
        if (this.activeTool === 'eraser') {
            const hitId = this.hitTestDrawings(coord.x, coord.y);
            if (hitId) {
                this.remove(hitId);
            }
            return;
        }

        // 2. Freehand Brush & Highlighter
        if (this.activeTool === 'brush' || this.activeTool === 'highlighter') {
            this.isDrawing = true;
            this.inProgressPoints = [tp];
            return;
        }

        // 3. Polyline: Click từng điểm, double-click hoặc Enter kết thúc
        if (this.activeTool === 'polyline') {
            if (this.inProgressPoints.length >= 2) {
                const firstCoord = this.timePriceToCoord(this.inProgressPoints[0].time, this.inProgressPoints[0].price);
                if (firstCoord && Math.hypot(coord.x - firstCoord.x, coord.y - firstCoord.y) <= 15) {
                    const newD = this.create('polyline', this.inProgressPoints, { scope: 'symbol' });
                    this.inProgressPoints = [];
                    this.isDrawing = false;
                    this.clearPreview();
                    if (!this.keepDrawingMode) this.setActiveTool('cursor');
                    this.select(newD.id);
                    return;
                }
            }
            this.isDrawing = true;
            this.inProgressPoints.push(tp);
            return;
        }

        // 4. Nếu đang ở Draw Mode thông thường
        if (this.activeTool !== 'cursor') {
            this.isDrawing = true;
            this.inProgressPoints.push(tp);

            const reqPoints = DrawingModel.REQUIRED_POINTS[this.activeTool] || 2;
            if (this.inProgressPoints.length >= reqPoints) {
                // Đủ điểm neo -> Tạo nét vẽ hoàn chỉnh
                const newDrawing = this.create(this.activeTool, this.inProgressPoints, {
                    scope: 'symbol'
                });
                this.inProgressPoints = [];
                this.isDrawing = false;
                this.clearPreview();

                // Tự động chuyển về Select Mode và bôi chọn nét vẽ vừa tạo (trừ khi bật Keep Drawing)
                if (!this.keepDrawingMode) {
                    this.setActiveTool('cursor');
                }
                this.select(newDrawing.id);
            }
            return;
        }

        // 5. Nếu đang ở Select Mode
        // 5a. Kiểm tra bấm vào Anchor Handle của nét vẽ đang chọn
        if (this.selectedId) {
            const selDrawing = this.get(this.selectedId);
            if (selDrawing && !selDrawing.locked) {
                const anchorIdx = this.hitTestAnchor(selDrawing, coord.x, coord.y);
                if (anchorIdx !== -1) {
                    this.dragState = {
                        type: 'anchor',
                        drawingId: this.selectedId,
                        pointIndex: anchorIdx,
                        startCoord: coord,
                        origPoints: JSON.parse(JSON.stringify(selDrawing.points))
                    };
                    return;
                }
            }
        }

        // 5b. Hit test nét vẽ trên toàn bộ danh sách
        const hitId = this.hitTestDrawings(coord.x, coord.y);
        if (hitId) {
            this.select(hitId);
            const hitDrawing = this.get(hitId);
            if (hitDrawing && !hitDrawing.locked) {
                this.dragState = {
                    type: 'body',
                    drawingId: hitId,
                    startCoord: coord,
                    startTP: tp,
                    origPoints: JSON.parse(JSON.stringify(hitDrawing.points))
                };
            }
        } else {
            this.deselect();
        }
    }

    handleMouseMove(e) {
        const coord = this.getMouseCoord(e);

        // Đang vẽ tự do (Brush / Highlighter)
        if (this.isDrawing && (this.activeTool === 'brush' || this.activeTool === 'highlighter')) {
            const tp = this.coordToTimePrice(coord.x, coord.y);
            if (tp) {
                const lastPt = this.inProgressPoints[this.inProgressPoints.length - 1];
                if (!lastPt || Math.abs(tp.time - lastPt.time) > 0 || Math.abs(tp.price - lastPt.price) > 0.05) {
                    this.inProgressPoints.push(tp);
                    this.renderPreview(this.inProgressPoints);
                }
            }
            return;
        }

        // Đang vẽ dở nét mới: Cập nhật live preview
        if (this.isDrawing && this.inProgressPoints.length > 0) {
            const tp = this.coordToTimePrice(coord.x, coord.y);
            if (tp) {
                this.renderPreview([...this.inProgressPoints, tp]);
            }
            return;
        }

        // Đang kéo thả nét vẽ hoặc điểm neo
        if (this.dragState) {
            const tp = this.coordToTimePrice(coord.x, coord.y);
            if (!tp) return;

            const drawing = this.get(this.dragState.drawingId);
            if (!drawing || drawing.locked) return;

            if (this.dragState.type === 'anchor') {
                drawing.points[this.dragState.pointIndex] = tp;
                if (drawing.coordinates) {
                    drawing.coordinates.prices = drawing.points.map(p => p.price);
                    drawing.coordinates.times = drawing.points.map(p => p.time);
                }
                this.requestRender();
            } else if (this.dragState.type === 'body') {
                const origStart = this.dragState.startTP;
                const deltaPrice = tp.price - origStart.price;
                const deltaTime = tp.time - origStart.time;

                drawing.points = this.dragState.origPoints.map(p => ({
                    time: p.time + deltaTime,
                    price: Number((p.price + deltaPrice).toFixed(3))
                }));
                if (drawing.coordinates) {
                    drawing.coordinates.prices = drawing.points.map(p => p.price);
                    drawing.coordinates.times = drawing.points.map(p => p.time);
                }
                this.requestRender();
            }
        }
    }

    handleMouseUp(e) {
        // Hoàn tất vẽ Brush / Highlighter
        if (this.isDrawing && (this.activeTool === 'brush' || this.activeTool === 'highlighter')) {
            if (this.inProgressPoints.length >= 2) {
                const newD = this.create(this.activeTool, this.inProgressPoints, { scope: 'symbol' });
                this.inProgressPoints = [];
                this.isDrawing = false;
                this.clearPreview();
                if (!this.keepDrawingMode) this.setActiveTool('cursor');
                this.select(newD.id);
            } else {
                this.inProgressPoints = [];
                this.isDrawing = false;
                this.clearPreview();
            }
            return;
        }

        if (this.dragState) {
            this.saveUndoState();
            this.dragState = null;
        }
    }

    handleDblClick(e) {
        const coord = this.getMouseCoord(e);

        // Kết thúc vẽ polyline bằng double click
        if (this.activeTool === 'polyline' && this.inProgressPoints.length >= 2) {
            const newD = this.create('polyline', this.inProgressPoints, { scope: 'symbol' });
            this.inProgressPoints = [];
            this.isDrawing = false;
            this.clearPreview();
            if (!this.keepDrawingMode) this.setActiveTool('cursor');
            this.select(newD.id);
            return;
        }

        const hitId = this.hitTestDrawings(coord.x, coord.y);
        if (hitId) {
            const drawing = this.get(hitId);
            if (!drawing) return;

            // Mở Property Dialog nếu có callback
            if (typeof this.options.onOpenProperties === 'function') {
                this.options.onOpenProperties(drawing);
                return;
            }

            // Fallback chỉnh text nhanh cho Text / Callout
            if (drawing.type === 'text' || drawing.type === 'callout') {
                const newText = prompt('Nhập nội dung ghi chú:', drawing.text);
                if (newText !== null) {
                    this.update(hitId, { text: newText });
                }
            }
        }
    }

    handleContextMenu(e) {
        const coord = this.getMouseCoord(e);
        let hitId = this.hitTestDrawings(coord.x, coord.y);
        if (!hitId && this.selectedId) {
            const target = e.target;
            if (target && target.closest && target.closest('.drawing-item, .drawing-handle')) {
                hitId = this.selectedId;
            }
        }
        if (hitId) {
            e.preventDefault();
            e.stopPropagation();
            this.select(hitId);
            const d = this.get(hitId);
            if (typeof this.options.onContextMenu === 'function') {
                this.options.onContextMenu(d, e);
            }
        }
    }

    hitTestAnchor(drawing, px, py) {
        const threshold = 10; // 10px bán kính chọn anchor
        for (let i = 0; i < drawing.points.length; i++) {
            const pt = drawing.points[i];
            const coord = this.timePriceToCoord(pt.time, pt.price);
            if (coord && Math.hypot(px - coord.x, py - coord.y) <= threshold) {
                return i;
            }
        }
        return -1;
    }

    hitTestDrawings(px, py) {
        const threshold = 8; // 8px dung sai chọn nét vẽ
        for (let i = this.drawings.length - 1; i >= 0; i--) {
            const d = this.drawings[i];
            if (d.hidden || d.visible === false) continue;

            // Visibility filter (T31)
            if (d.visibility && !d.visibility.allTimeframes) {
                if (Array.isArray(d.visibility.timeframes) && !d.visibility.timeframes.includes(this.currentTimeframe)) {
                    continue;
                }
            }

            // Scope filter: nếu scope === 'timeframe' và targetTimeframe !== currentTimeframe thì bỏ qua
            if (d.scope === 'timeframe' && d.targetTimeframe && d.targetTimeframe !== this.currentTimeframe) {
                continue;
            }

            // Replay filter: nếu replay đang hoạt động và visibleInReplay === 'past_only'
            if (this.isReplayActive && this.currentReplayTime) {
                if (d.visibleInReplay === 'past_only' && d.createdAtTime > this.currentReplayTime) {
                    continue;
                }
            }

            const coords = d.points.map(p => this.timePriceToCoord(p.time, p.price)).filter(Boolean);
            if (coords.length === 0) continue;

            if (d.type === 'horizontal') {
                const y = coords[0].y;
                if (Math.abs(py - y) <= threshold) return d.id;
            } else if (d.type === 'horizontal_ray') {
                const y = coords[0].y;
                if (Math.abs(py - y) <= threshold && px >= coords[0].x - threshold) return d.id;
            } else if (d.type === 'vertical') {
                const x = coords[0].x;
                if (Math.abs(px - x) <= threshold) return d.id;
            } else if (d.type === 'crossline') {
                if (Math.abs(py - coords[0].y) <= threshold || Math.abs(px - coords[0].x) <= threshold) return d.id;
            } else if ((d.type === 'trendline' || d.type === 'arrow' || d.type === 'info_line' || d.type === 'trend_angle') && coords.length >= 2) {
                if (DrawingGeometry.distanceToSegment(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold) {
                    return d.id;
                }
            } else if (d.type === 'ray' && coords.length >= 2) {
                if (DrawingGeometry.distanceToRay(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold) {
                    return d.id;
                }
            } else if (d.type === 'extended' && coords.length >= 2) {
                if (DrawingGeometry.distanceToLine(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold) {
                    return d.id;
                }
            } else if ((d.type === 'rectangle' || d.type === 'ruler' || d.type === 'price_range' || d.type === 'date_range' || d.type === 'date_price_range' || d.type === 'gann_box') && coords.length >= 2) {
                const inside = DrawingGeometry.isPointInRect(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y);
                if (inside) return d.id;
                const minX = Math.min(coords[0].x, coords[1].x);
                const maxX = Math.max(coords[0].x, coords[1].x);
                const minY = Math.min(coords[0].y, coords[1].y);
                const maxY = Math.max(coords[0].y, coords[1].y);
                if ((px >= minX - threshold && px <= maxX + threshold) &&
                    (Math.abs(py - minY) <= threshold || Math.abs(py - maxY) <= threshold)) return d.id;
                if ((py >= minY - threshold && py <= maxY + threshold) &&
                    (Math.abs(px - minX) <= threshold || Math.abs(px - maxX) <= threshold)) return d.id;
            } else if (d.type === 'circle' && coords.length >= 2) {
                const r = Math.hypot(coords[1].x - coords[0].x, coords[1].y - coords[0].y);
                if (DrawingGeometry.distanceToCircle(px, py, coords[0].x, coords[0].y, r) <= threshold ||
                    DrawingGeometry.isPointInCircle(px, py, coords[0].x, coords[0].y, r)) {
                    return d.id;
                }
            } else if (d.type === 'triangle' && coords.length >= 3) {
                if (DrawingGeometry.isPointInTriangle(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y, coords[2].x, coords[2].y)) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold ||
                    DrawingGeometry.distanceToSegment(px, py, coords[1].x, coords[1].y, coords[2].x, coords[2].y) <= threshold ||
                    DrawingGeometry.distanceToSegment(px, py, coords[2].x, coords[2].y, coords[0].x, coords[0].y) <= threshold) {
                    return d.id;
                }
            } else if ((d.type === 'polyline' || d.type === 'brush' || d.type === 'highlighter') && coords.length >= 2) {
                const polyDist = DrawingGeometry.distanceToPolyline(px, py, coords);
                if (polyDist <= threshold + (d.style?.width || 2)) return d.id;
            } else if (d.type === 'channel' && coords.length >= 3) {
                const ch = DrawingGeometry.calculateChannel(coords[0].x, coords[0].y, coords[1].x, coords[1].y, coords[2].x, coords[2].y);
                if (DrawingGeometry.distanceToSegment(px, py, ch.base[0].x, ch.base[0].y, ch.base[1].x, ch.base[1].y) <= threshold) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, ch.parallel[0].x, ch.parallel[0].y, ch.parallel[1].x, ch.parallel[1].y) <= threshold) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, ch.midline[0].x, ch.midline[0].y, ch.midline[1].x, ch.midline[1].y) <= threshold) return d.id;
                if (DrawingGeometry.isPointInPolygon(px, py, ch.polygon)) return d.id;
            } else if (d.type === 'regression_trend' && coords.length >= 2) {
                const minX = Math.min(coords[0].x, coords[1].x);
                const maxX = Math.max(coords[0].x, coords[1].x);
                if (px >= minX - threshold && px <= maxX + threshold) {
                    if (DrawingGeometry.distanceToLine(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold + 30) return d.id;
                }
            } else if ((d.type === 'pitchfork' || d.type === 'schiff_pitchfork' || d.type === 'mod_schiff_pitchfork' || d.type === 'inside_pitchfork') && coords.length >= 3) {
                const fork = DrawingGeometry.calculatePitchfork(coords[0], coords[1], coords[2], d.type.replace('_pitchfork', ''));
                if (DrawingGeometry.distanceToSegment(px, py, fork.medianLine[0].x, fork.medianLine[0].y, fork.medianLine[1].x, fork.medianLine[1].y) <= threshold) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, fork.upperLine[0].x, fork.upperLine[0].y, fork.upperLine[1].x, fork.upperLine[1].y) <= threshold) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, fork.lowerLine[0].x, fork.lowerLine[0].y, fork.lowerLine[1].x, fork.lowerLine[1].y) <= threshold) return d.id;
                if (DrawingGeometry.isPointInPolygon(px, py, fork.polygon)) return d.id;
            } else if ((d.type === 'long_position' || d.type === 'short_position') && coords.length >= 3) {
                const entryY = coords[0].y;
                const slY = coords[1].y;
                const tpY = coords[2].y;
                const minX = coords[0].x - 10;
                const maxX = coords[0].x + 220;
                const minY = Math.min(entryY, slY, tpY);
                const maxY = Math.max(entryY, slY, tpY);
                if (px >= minX && px <= maxX && py >= minY - threshold && py <= maxY + threshold) return d.id;
            } else if (d.type === 'abcd' && coords.length >= 4) {
                if (DrawingGeometry.distanceToSegment(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, coords[1].x, coords[1].y, coords[2].x, coords[2].y) <= threshold) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, coords[2].x, coords[2].y, coords[3].x, coords[3].y) <= threshold) return d.id;
            } else if (d.type === 'elliott_wave_15' && coords.length >= 2) {
                for (let j = 0; j < coords.length - 1; j++) {
                    if (DrawingGeometry.distanceToSegment(px, py, coords[j].x, coords[j].y, coords[j+1].x, coords[j+1].y) <= threshold) return d.id;
                }
            } else if (d.type === 'elliott_wave_abc' && coords.length >= 2) {
                for (let j = 0; j < coords.length - 1; j++) {
                    if (DrawingGeometry.distanceToSegment(px, py, coords[j].x, coords[j].y, coords[j+1].x, coords[j+1].y) <= threshold) return d.id;
                }
            } else if (d.type === 'head_shoulders' && coords.length >= 4) {
                for (let j = 0; j < coords.length - 1; j++) {
                    if (DrawingGeometry.distanceToSegment(px, py, coords[j].x, coords[j].y, coords[j+1].x, coords[j+1].y) <= threshold) return d.id;
                }
            } else if (d.type === 'fib_timezone' && coords.length >= 2) {
                const barSec = Math.max(60, Math.abs(d.points[1].time - d.points[0].time));
                const tzList = DrawingGeometry.calculateFibTimeZone(d.points[0].time, barSec, 8);
                for (const tz of tzList) {
                    const c = this.timePriceToCoord(tz.time, d.points[0].price);
                    if (c && Math.abs(px - c.x) <= threshold) return d.id;
                }
            } else if (d.type === 'gann_fan' && coords.length >= 2) {
                if (Math.hypot(px - coords[0].x, py - coords[0].y) <= 80) return d.id;
            } else if (d.type === 'fib_retracement' && coords.length >= 2) {
                if (DrawingGeometry.distanceToSegment(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold) return d.id;
                const levels = DrawingGeometry.calculateFibRetracement(d.points[0].price, d.points[1].price);
                const minX = Math.min(coords[0].x, coords[1].x) - 15;
                const maxX = Math.max(coords[0].x, coords[1].x) + 260;
                if (px >= minX && px <= maxX) {
                    for (const lvl of levels) {
                        const lvlCoord = this.timePriceToCoord(d.points[0].time, lvl.price);
                        if (lvlCoord && Math.abs(py - lvlCoord.y) <= threshold) return d.id;
                    }
                }
            } else if (d.type === 'fib_extension' && coords.length >= 3) {
                if (DrawingGeometry.distanceToSegment(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold) return d.id;
                if (DrawingGeometry.distanceToSegment(px, py, coords[1].x, coords[1].y, coords[2].x, coords[2].y) <= threshold) return d.id;
                const levels = DrawingGeometry.calculateFibExtension(d.points[0].price, d.points[1].price, d.points[2].price);
                const minX = coords[2].x - 15;
                const maxX = coords[2].x + 300;
                if (px >= minX && px <= maxX) {
                    for (const lvl of levels) {
                        const lvlCoord = this.timePriceToCoord(d.points[2].time, lvl.price);
                        if (lvlCoord && Math.abs(py - lvlCoord.y) <= threshold) return d.id;
                    }
                }
            } else if (d.type === 'callout' && coords.length >= 2) {
                if (DrawingGeometry.distanceToSegment(px, py, coords[0].x, coords[0].y, coords[1].x, coords[1].y) <= threshold) return d.id;
                if (Math.hypot(px - coords[1].x, py - coords[1].y) <= 35) return d.id;
            } else if (coords.length >= 1) {
                if (Math.hypot(px - coords[0].x, py - coords[0].y) <= threshold + 10) return d.id;
            }
        }
        return null;
    }

    // ==========================================
    // RENDER ENGINE (SVG RENDERING VỚI VIEWPORT CULLING)
    // ==========================================
    requestRender() {
        if (typeof window !== 'undefined' && window.requestAnimationFrame) {
            if (this.renderPending) return;
            this.renderPending = true;
            this.rafId = window.requestAnimationFrame(() => {
                this.renderPending = false;
                this.rafId = null;
                this.render();
            });
        } else {
            this.render();
        }
    }

    render() {
        if (!this.svg || !this.drawingsGroup) return;

        this.drawingsGroup.innerHTML = '';
        this.handlesGroup.innerHTML = '';

        // Lấy visible time range từ chart để thực hiện Viewport Bounding-Box Culling (T34)
        // Mở rộng bằng logical range để bao phủ cả vùng tương lai (không bị clamp bởi nến cuối)
        let visibleTimeStart = -Infinity;
        let visibleTimeEnd = Infinity;
        if (this.chartInstance && this.chartInstance.chart) {
            try {
                const timeScale = this.chartInstance.chart.timeScale();
                const candles = this.chartInstance.currentCandles;

                if (candles && candles.length > 0 && typeof timeScale.getVisibleLogicalRange === 'function') {
                    const logicalRange = timeScale.getVisibleLogicalRange();
                    if (logicalRange && typeof logicalRange.from === 'number' && typeof logicalRange.to === 'number') {
                        const interval = this.getCandleIntervalSeconds(candles) || 60;
                        const lastIdx = candles.length - 1;
                        const first = candles[0];
                        const last = candles[lastIdx];

                        if (logicalRange.from < 0) {
                            visibleTimeStart = first.time + logicalRange.from * interval;
                        } else if (logicalRange.from <= lastIdx) {
                            visibleTimeStart = candles[Math.floor(logicalRange.from)].time;
                        } else {
                            visibleTimeStart = last.time + (logicalRange.from - lastIdx) * interval;
                        }

                        if (logicalRange.to > lastIdx) {
                            visibleTimeEnd = last.time + (logicalRange.to - lastIdx) * interval;
                        } else if (logicalRange.to >= 0) {
                            visibleTimeEnd = candles[Math.min(lastIdx, Math.ceil(logicalRange.to))].time;
                        } else {
                            visibleTimeEnd = first.time + logicalRange.to * interval;
                        }
                    }
                }

                if (!isFinite(visibleTimeStart) || !isFinite(visibleTimeEnd)) {
                    const range = timeScale.getVisibleRange ? timeScale.getVisibleRange() : null;
                    if (range && typeof range.from === 'number' && typeof range.to === 'number') {
                        visibleTimeStart = range.from;
                        visibleTimeEnd = range.to;
                    }
                }
            } catch (e) {}
        }

        for (const drawing of this.drawings) {
            if (drawing.hidden || drawing.visible === false) continue;

            // Visibility filter (T31)
            if (drawing.visibility && !drawing.visibility.allTimeframes) {
                if (Array.isArray(drawing.visibility.timeframes) && !drawing.visibility.timeframes.includes(this.currentTimeframe)) {
                    continue;
                }
            }

            // Scope filter: nếu scope === 'timeframe' và targetTimeframe !== currentTimeframe thì ẩn
            if (drawing.scope === 'timeframe' && drawing.targetTimeframe && drawing.targetTimeframe !== this.currentTimeframe) {
                continue;
            }

            // Replay filter: nếu replay đang hoạt động và visibleInReplay === 'past_only'
            if (this.isReplayActive && this.currentReplayTime) {
                if (drawing.visibleInReplay === 'past_only' && drawing.createdAtTime > this.currentReplayTime) {
                    continue;
                }
            }

            // Viewport Bounding-Box Culling (T34): Bỏ qua các nét vẽ nằm ngoài màn hình
            const infiniteTools = ['horizontal', 'vertical', 'extended', 'crossline', 'horizontal_ray'];
            if (!infiniteTools.includes(drawing.type) && isFinite(visibleTimeStart) && isFinite(visibleTimeEnd) && drawing.points.length > 0) {
                let minT = Infinity, maxT = -Infinity;
                for (const pt of drawing.points) {
                    if (pt.time < minT) minT = pt.time;
                    if (pt.time > maxT) maxT = pt.time;
                }
                const buffer = Math.max(3600, (visibleTimeEnd - visibleTimeStart) * 0.1);
                if (maxT < visibleTimeStart - buffer || minT > visibleTimeEnd + buffer) {
                    continue; // CULL: Skip offscreen drawings
                }
            }

            this.renderDrawing(drawing);
        }

        // Render Anchor Handles cho drawing đang được chọn
        if (this.selectedId) {
            const sel = this.get(this.selectedId);
            if (sel && !sel.hidden && sel.visible !== false) {
                this.renderHandles(sel);
            }
        }
    }

    renderDrawing(d) {
        const coords = d.points.map(p => this.timePriceToCoord(p.time, p.price));
        if (coords.some(c => c === null)) return;

        const isSelected = (d.id === this.selectedId);
        const color = d.style.color || '#2962ff';
        const width = d.style.width || 2;
        const strokeDash = d.style.lineStyle === 'dashed' ? '6,6' : (d.style.lineStyle === 'dotted' ? '2,4' : '');
        const opacity = d.style.opacity || 1.0;

        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('class', `drawing-item ${isSelected ? 'selected' : ''}`);
        g.setAttribute('data-id', d.id);
        g.style.pointerEvents = 'stroke';
        g.style.cursor = 'pointer';

        if (d.type === 'trendline' && coords.length >= 2) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', coords[1].x);
            line.setAttribute('y2', coords[1].y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            if (strokeDash) line.setAttribute('stroke-dasharray', strokeDash);
            line.setAttribute('stroke-opacity', opacity);
            g.appendChild(line);
        } else if (d.type === 'ray' && coords.length >= 2) {
            const dx = coords[1].x - coords[0].x;
            const dy = coords[1].y - coords[0].y;
            const len = Math.hypot(dx, dy) || 1;
            const extX = coords[0].x + (dx / len) * 4000;
            const extY = coords[0].y + (dy / len) * 4000;
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', extX);
            line.setAttribute('y2', extY);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            if (strokeDash) line.setAttribute('stroke-dasharray', strokeDash);
            line.setAttribute('stroke-opacity', opacity);
            g.appendChild(line);
        } else if (d.type === 'extended' && coords.length >= 2) {
            const dx = coords[1].x - coords[0].x;
            const dy = coords[1].y - coords[0].y;
            const len = Math.hypot(dx, dy) || 1;
            const startX = coords[0].x - (dx / len) * 4000;
            const startY = coords[0].y - (dy / len) * 4000;
            const endX = coords[1].x + (dx / len) * 4000;
            const endY = coords[1].y + (dy / len) * 4000;
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', startX);
            line.setAttribute('y1', startY);
            line.setAttribute('x2', endX);
            line.setAttribute('y2', endY);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            if (strokeDash) line.setAttribute('stroke-dasharray', strokeDash);
            line.setAttribute('stroke-opacity', opacity);
            g.appendChild(line);
        } else if (d.type === 'horizontal' && coords.length >= 1) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', 0);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', '100%');
            line.setAttribute('y2', coords[0].y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            if (strokeDash) line.setAttribute('stroke-dasharray', strokeDash);
            line.setAttribute('stroke-opacity', opacity);
            g.appendChild(line);

            const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badge.setAttribute('x', '99%');
            badge.setAttribute('y', coords[0].y - 4);
            badge.setAttribute('text-anchor', 'end');
            badge.setAttribute('fill', color);
            badge.setAttribute('font-size', '11');
            badge.setAttribute('font-family', 'monospace');
            badge.textContent = d.points[0].price.toFixed(3);
            g.appendChild(badge);
        } else if (d.type === 'vertical' && coords.length >= 1) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', 0);
            line.setAttribute('x2', coords[0].x);
            line.setAttribute('y2', '100%');
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            if (strokeDash) line.setAttribute('stroke-dasharray', strokeDash);
            line.setAttribute('stroke-opacity', opacity);
            g.appendChild(line);
        } else if (d.type === 'rectangle' && coords.length >= 2) {
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x);
            const minY = Math.min(coords[0].y, coords[1].y);
            const maxY = Math.max(coords[0].y, coords[1].y);

            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', minX);
            rect.setAttribute('y', minY);
            rect.setAttribute('width', Math.max(1, maxX - minX));
            rect.setAttribute('height', Math.max(1, maxY - minY));
            rect.setAttribute('stroke', color);
            rect.setAttribute('stroke-width', width);
            if (strokeDash) lineStyleDash(rect, d.style.lineStyle);
            rect.setAttribute('fill', d.style.fillColor || 'rgba(41, 98, 255, 0.15)');
            rect.style.pointerEvents = 'all';
            g.appendChild(rect);
        } else if (d.type === 'channel' && coords.length >= 3) {
            const ch = DrawingGeometry.calculateChannel(coords[0].x, coords[0].y, coords[1].x, coords[1].y, coords[2].x, coords[2].y);

            const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            const ptsStr = ch.polygon.map(pt => `${pt.x},${pt.y}`).join(' ');
            poly.setAttribute('points', ptsStr);
            poly.setAttribute('fill', d.style.fillColor || 'rgba(41, 98, 255, 0.15)');
            poly.style.pointerEvents = 'all';
            g.appendChild(poly);

            const baseLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            baseLine.setAttribute('x1', ch.base[0].x);
            baseLine.setAttribute('y1', ch.base[0].y);
            baseLine.setAttribute('x2', ch.base[1].x);
            baseLine.setAttribute('y2', ch.base[1].y);
            baseLine.setAttribute('stroke', color);
            baseLine.setAttribute('stroke-width', width);
            g.appendChild(baseLine);

            const parLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            parLine.setAttribute('x1', ch.parallel[0].x);
            parLine.setAttribute('y1', ch.parallel[0].y);
            parLine.setAttribute('x2', ch.parallel[1].x);
            parLine.setAttribute('y2', ch.parallel[1].y);
            parLine.setAttribute('stroke', color);
            parLine.setAttribute('stroke-width', width);
            g.appendChild(parLine);

            const midLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            midLine.setAttribute('x1', ch.midline[0].x);
            midLine.setAttribute('y1', ch.midline[0].y);
            midLine.setAttribute('x2', ch.midline[1].x);
            midLine.setAttribute('y2', ch.midline[1].y);
            midLine.setAttribute('stroke', color);
            midLine.setAttribute('stroke-width', 1);
            midLine.setAttribute('stroke-dasharray', '4,4');
            midLine.setAttribute('stroke-opacity', '0.7');
            g.appendChild(midLine);
        } else if (d.type === 'fib_retracement' && coords.length >= 2) {
            const levels = DrawingGeometry.calculateFibRetracement(d.points[0].price, d.points[1].price);
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x) + 160;

            const trend = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            trend.setAttribute('x1', coords[0].x);
            trend.setAttribute('y1', coords[0].y);
            trend.setAttribute('x2', coords[1].x);
            trend.setAttribute('y2', coords[1].y);
            trend.setAttribute('stroke', color);
            trend.setAttribute('stroke-width', 1);
            trend.setAttribute('stroke-dasharray', '3,3');
            trend.setAttribute('stroke-opacity', '0.5');
            g.appendChild(trend);

            const fibColors = ['#787b86', '#f23645', '#ff9800', '#089981', '#2962ff', '#9c27b0', '#787b86'];
            levels.forEach((lvl, idx) => {
                const coord = this.timePriceToCoord(d.points[0].time, lvl.price);
                if (!coord) return;

                const lvlLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                lvlLine.setAttribute('x1', minX);
                lvlLine.setAttribute('y1', coord.y);
                lvlLine.setAttribute('x2', maxX);
                lvlLine.setAttribute('y2', coord.y);
                lvlLine.setAttribute('stroke', fibColors[idx % fibColors.length]);
                lvlLine.setAttribute('stroke-width', width);
                if (strokeDash) lvlLine.setAttribute('stroke-dasharray', strokeDash);
                g.appendChild(lvlLine);

                const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                txt.setAttribute('x', minX + 6);
                txt.setAttribute('y', coord.y - 4);
                txt.setAttribute('fill', fibColors[idx % fibColors.length]);
                txt.setAttribute('font-size', '10');
                txt.setAttribute('font-family', 'monospace');
                txt.textContent = `${lvl.percent} (${lvl.price.toFixed(2)})`;
                g.appendChild(txt);
            });
        } else if (d.type === 'fib_extension' && coords.length >= 3) {
            const levels = DrawingGeometry.calculateFibExtension(d.points[0].price, d.points[1].price, d.points[2].price);
            const minX = coords[2].x;
            const maxX = coords[2].x + 220;

            const lineAB = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            lineAB.setAttribute('x1', coords[0].x);
            lineAB.setAttribute('y1', coords[0].y);
            lineAB.setAttribute('x2', coords[1].x);
            lineAB.setAttribute('y2', coords[1].y);
            lineAB.setAttribute('stroke', color);
            lineAB.setAttribute('stroke-width', 1.5);
            lineAB.setAttribute('stroke-dasharray', '3,3');
            g.appendChild(lineAB);

            const lineBC = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            lineBC.setAttribute('x1', coords[1].x);
            lineBC.setAttribute('y1', coords[1].y);
            lineBC.setAttribute('x2', coords[2].x);
            lineBC.setAttribute('y2', coords[2].y);
            lineBC.setAttribute('stroke', color);
            lineBC.setAttribute('stroke-width', 1.5);
            lineBC.setAttribute('stroke-dasharray', '3,3');
            g.appendChild(lineBC);

            const extColors = ['#787b86', '#089981', '#2962ff', '#ff9800', '#f23645', '#9c27b0'];
            levels.forEach((lvl, idx) => {
                const coord = this.timePriceToCoord(d.points[2].time, lvl.price);
                if (!coord) return;

                const lvlLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                lvlLine.setAttribute('x1', minX);
                lvlLine.setAttribute('y1', coord.y);
                lvlLine.setAttribute('x2', maxX);
                lvlLine.setAttribute('y2', coord.y);
                lvlLine.setAttribute('stroke', extColors[idx % extColors.length]);
                lvlLine.setAttribute('stroke-width', width);
                g.appendChild(lvlLine);

                const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                txt.setAttribute('x', minX + 6);
                txt.setAttribute('y', coord.y - 4);
                txt.setAttribute('fill', extColors[idx % extColors.length]);
                txt.setAttribute('font-size', '10');
                txt.setAttribute('font-family', 'monospace');
                txt.textContent = `${lvl.percent} (${lvl.price.toFixed(2)})`;
                g.appendChild(txt);
            });
        } else if (d.type === 'ruler' && coords.length >= 2) {
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x);
            const minY = Math.min(coords[0].y, coords[1].y);
            const maxY = Math.max(coords[0].y, coords[1].y);

            const box = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            box.setAttribute('x', minX);
            box.setAttribute('y', minY);
            box.setAttribute('width', Math.max(1, maxX - minX));
            box.setAttribute('height', Math.max(1, maxY - minY));
            box.setAttribute('stroke', color);
            box.setAttribute('stroke-width', 1);
            box.setAttribute('stroke-dasharray', '3,3');
            box.setAttribute('fill', 'rgba(41, 98, 255, 0.12)');
            g.appendChild(box);

            const diag = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            diag.setAttribute('x1', coords[0].x);
            diag.setAttribute('y1', coords[0].y);
            diag.setAttribute('x2', coords[1].x);
            diag.setAttribute('y2', coords[1].y);
            diag.setAttribute('stroke', color);
            diag.setAttribute('stroke-width', 1.5);
            g.appendChild(diag);

            const bars = Math.max(1, Math.round((maxX - minX) / 8));
            const metrics = DrawingGeometry.calculateRulerMetrics(d.points[0].price, d.points[1].price, d.points[0].time, d.points[1].time, bars);

            const midX = (minX + maxX) / 2;
            const midY = (minY + maxY) / 2;

            const bgRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bgRect.setAttribute('x', midX - 90);
            bgRect.setAttribute('y', midY - 14);
            bgRect.setAttribute('width', 180);
            bgRect.setAttribute('height', 28);
            bgRect.setAttribute('rx', 4);
            bgRect.setAttribute('fill', '#1e222d');
            bgRect.setAttribute('stroke', color);
            bgRect.setAttribute('stroke-width', 1);
            g.appendChild(bgRect);

            const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            txt.setAttribute('x', midX);
            txt.setAttribute('y', midY + 4);
            txt.setAttribute('text-anchor', 'middle');
            txt.setAttribute('fill', metrics.deltaPrice >= 0 ? '#089981' : '#f23645');
            txt.setAttribute('font-size', '10');
            txt.setAttribute('font-family', 'monospace');
            txt.textContent = `${metrics.deltaPrice >= 0 ? '+' : ''}${metrics.deltaPrice.toFixed(2)} (${metrics.percentChange >= 0 ? '+' : ''}${metrics.percentChange.toFixed(2)}%) | ${bars} bars`;
            g.appendChild(txt);
        } else if (d.type === 'price_range' && coords.length >= 2) {
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x);
            const minY = Math.min(coords[0].y, coords[1].y);
            const maxY = Math.max(coords[0].y, coords[1].y);

            const band = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            band.setAttribute('x', minX);
            band.setAttribute('y', minY);
            band.setAttribute('width', Math.max(40, maxX - minX));
            band.setAttribute('height', Math.max(1, maxY - minY));
            band.setAttribute('stroke', color);
            band.setAttribute('stroke-width', 1.5);
            band.setAttribute('fill', 'rgba(41, 98, 255, 0.15)');
            g.appendChild(band);

            const delta = d.points[1].price - d.points[0].price;
            const pct = d.points[0].price !== 0 ? (delta / d.points[0].price) * 100 : 0;

            const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            txt.setAttribute('x', minX + 8);
            txt.setAttribute('y', (minY + maxY) / 2 + 4);
            txt.setAttribute('fill', delta >= 0 ? '#089981' : '#f23645');
            txt.setAttribute('font-size', '11');
            txt.setAttribute('font-family', 'monospace');
            txt.textContent = `${delta >= 0 ? '+' : ''}${delta.toFixed(3)} (${delta >= 0 ? '+' : ''}${pct.toFixed(2)}%)`;
            g.appendChild(txt);
        } else if (d.type === 'date_range' && coords.length >= 2) {
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x);

            const band = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            band.setAttribute('x', minX);
            band.setAttribute('y', 0);
            band.setAttribute('width', Math.max(1, maxX - minX));
            band.setAttribute('height', '100%');
            band.setAttribute('stroke', color);
            band.setAttribute('stroke-width', 1);
            band.setAttribute('stroke-dasharray', '4,4');
            band.setAttribute('fill', 'rgba(41, 98, 255, 0.08)');
            g.appendChild(band);

            const bars = Math.max(1, Math.round((maxX - minX) / 8));
            const metrics = DrawingGeometry.calculateRulerMetrics(0, 0, d.points[0].time, d.points[1].time, bars);

            const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            txt.setAttribute('x', (minX + maxX) / 2);
            txt.setAttribute('y', 24);
            txt.setAttribute('text-anchor', 'middle');
            txt.setAttribute('fill', color);
            txt.setAttribute('font-size', '11');
            txt.setAttribute('font-family', 'monospace');
            txt.textContent = `${bars} bars | ${metrics.durationFormatted}`;
            g.appendChild(txt);
        } else if (d.type === 'arrow' && coords.length >= 2) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', coords[1].x);
            line.setAttribute('y2', coords[1].y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            g.appendChild(line);

            const dx = coords[1].x - coords[0].x;
            const dy = coords[1].y - coords[0].y;
            const angle = Math.atan2(dy, dx);
            const headLen = 14;
            const x1 = coords[1].x - headLen * Math.cos(angle - Math.PI / 6);
            const y1 = coords[1].y - headLen * Math.sin(angle - Math.PI / 6);
            const x2 = coords[1].x - headLen * Math.cos(angle + Math.PI / 6);
            const y2 = coords[1].y - headLen * Math.sin(angle + Math.PI / 6);

            const head = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            head.setAttribute('points', `${coords[1].x},${coords[1].y} ${x1},${y1} ${x2},${y2}`);
            head.setAttribute('fill', color);
            g.appendChild(head);
        } else if (d.type === 'callout' && coords.length >= 2) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', coords[1].x);
            line.setAttribute('y2', coords[1].y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', 1.5);
            line.setAttribute('stroke-dasharray', '2,2');
            g.appendChild(line);

            const textContent = d.text || 'Chú thích';
            const approxWidth = Math.max(60, textContent.length * 8 + 20);

            const bubble = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bubble.setAttribute('x', coords[1].x - 10);
            bubble.setAttribute('y', coords[1].y - 18);
            bubble.setAttribute('width', approxWidth);
            bubble.setAttribute('height', 26);
            bubble.setAttribute('rx', 6);
            bubble.setAttribute('fill', '#1e222d');
            bubble.setAttribute('stroke', color);
            bubble.setAttribute('stroke-width', 1.5);
            bubble.style.pointerEvents = 'all';
            g.appendChild(bubble);

            const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            txt.setAttribute('x', coords[1].x);
            txt.setAttribute('y', coords[1].y);
            txt.setAttribute('fill', d.style.textColor || '#ffffff');
            txt.setAttribute('font-size', '12');
            txt.setAttribute('font-family', 'sans-serif');
            txt.textContent = textContent;
            txt.style.pointerEvents = 'all';
            g.appendChild(txt);
        } else if (d.type === 'horizontal_ray' && coords.length >= 1) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', coords[0].x + 4000);
            line.setAttribute('y2', coords[0].y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            if (strokeDash) line.setAttribute('stroke-dasharray', strokeDash);
            line.setAttribute('stroke-opacity', opacity);
            g.appendChild(line);

            const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badge.setAttribute('x', coords[0].x + 12);
            badge.setAttribute('y', coords[0].y - 5);
            badge.setAttribute('fill', color);
            badge.setAttribute('font-size', '11');
            badge.setAttribute('font-family', 'monospace');
            badge.textContent = d.points[0].price.toFixed(3);
            g.appendChild(badge);
        } else if (d.type === 'crossline' && coords.length >= 1) {
            const hLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            hLine.setAttribute('x1', 0);
            hLine.setAttribute('y1', coords[0].y);
            hLine.setAttribute('x2', '100%');
            hLine.setAttribute('y2', coords[0].y);
            hLine.setAttribute('stroke', color);
            hLine.setAttribute('stroke-width', width);
            if (strokeDash) hLine.setAttribute('stroke-dasharray', strokeDash);
            g.appendChild(hLine);

            const vLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            vLine.setAttribute('x1', coords[0].x);
            vLine.setAttribute('y1', 0);
            vLine.setAttribute('x2', coords[0].x);
            vLine.setAttribute('y2', '100%');
            vLine.setAttribute('stroke', color);
            vLine.setAttribute('stroke-width', width);
            if (strokeDash) vLine.setAttribute('stroke-dasharray', strokeDash);
            g.appendChild(vLine);

            const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badge.setAttribute('x', coords[0].x + 6);
            badge.setAttribute('y', coords[0].y - 6);
            badge.setAttribute('fill', color);
            badge.setAttribute('font-size', '10');
            badge.setAttribute('font-family', 'monospace');
            badge.textContent = `${d.points[0].price.toFixed(2)}`;
            g.appendChild(badge);
        } else if (d.type === 'info_line' && coords.length >= 2) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', coords[1].x);
            line.setAttribute('y2', coords[1].y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            if (strokeDash) line.setAttribute('stroke-dasharray', strokeDash);
            g.appendChild(line);

            const angleInfo = DrawingGeometry.calculateTrendAngle(coords[0], coords[1]);
            const angle = angleInfo.angleDeg;
            const deltaP = d.points[1].price - d.points[0].price;
            const pips = Number((Math.abs(deltaP) / 0.1).toFixed(1));
            const midX = (coords[0].x + coords[1].x) / 2;
            const midY = (coords[0].y + coords[1].y) / 2;

            const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bg.setAttribute('x', midX - 65);
            bg.setAttribute('y', midY - 14);
            bg.setAttribute('width', 130);
            bg.setAttribute('height', 24);
            bg.setAttribute('rx', 4);
            bg.setAttribute('fill', '#1e222d');
            bg.setAttribute('stroke', color);
            bg.setAttribute('stroke-width', 1);
            g.appendChild(bg);

            const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            txt.setAttribute('x', midX);
            txt.setAttribute('y', midY + 3);
            txt.setAttribute('text-anchor', 'middle');
            txt.setAttribute('fill', deltaP >= 0 ? '#089981' : '#f23645');
            txt.setAttribute('font-size', '10');
            txt.setAttribute('font-family', 'monospace');
            txt.textContent = `${deltaP >= 0 ? '+' : ''}${deltaP.toFixed(2)} (${pips}p) ${angle}°`;
            g.appendChild(txt);
        } else if (d.type === 'trend_angle' && coords.length >= 2) {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', coords[1].x);
            line.setAttribute('y2', coords[1].y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', width);
            g.appendChild(line);

            // Đường chuẩn ngang tham chiếu
            const refLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            refLine.setAttribute('x1', coords[0].x);
            refLine.setAttribute('y1', coords[0].y);
            refLine.setAttribute('x2', coords[1].x);
            refLine.setAttribute('y2', coords[0].y);
            refLine.setAttribute('stroke', '#787b86');
            refLine.setAttribute('stroke-width', 1);
            refLine.setAttribute('stroke-dasharray', '3,3');
            g.appendChild(refLine);

            const angleInfo = DrawingGeometry.calculateTrendAngle(coords[0], coords[1]);
            const angle = angleInfo.angleDeg;
            const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badge.setAttribute('x', coords[0].x + 35);
            badge.setAttribute('y', coords[0].y - 8);
            badge.setAttribute('fill', color);
            badge.setAttribute('font-size', '11');
            badge.setAttribute('font-weight', 'bold');
            badge.textContent = `${angle}°`;
            g.appendChild(badge);
        } else if (d.type === 'circle' && coords.length >= 2) {
            const r = Math.hypot(coords[1].x - coords[0].x, coords[1].y - coords[0].y);
            const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            circle.setAttribute('cx', coords[0].x);
            circle.setAttribute('cy', coords[0].y);
            circle.setAttribute('r', Math.max(1, r));
            circle.setAttribute('stroke', color);
            circle.setAttribute('stroke-width', width);
            if (strokeDash) circle.setAttribute('stroke-dasharray', strokeDash);
            circle.setAttribute('fill', d.style.fillColor || 'rgba(41, 98, 255, 0.15)');
            circle.style.pointerEvents = 'all';
            g.appendChild(circle);
        } else if (d.type === 'triangle' && coords.length >= 3) {
            const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            poly.setAttribute('points', `${coords[0].x},${coords[0].y} ${coords[1].x},${coords[1].y} ${coords[2].x},${coords[2].y}`);
            poly.setAttribute('stroke', color);
            poly.setAttribute('stroke-width', width);
            poly.setAttribute('fill', d.style.fillColor || 'rgba(41, 98, 255, 0.15)');
            poly.style.pointerEvents = 'all';
            g.appendChild(poly);
        } else if ((d.type === 'polyline' || d.type === 'brush' || d.type === 'highlighter') && coords.length >= 2) {
            const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
            poly.setAttribute('points', coords.map(c => `${c.x},${c.y}`).join(' '));
            poly.setAttribute('stroke', color);
            poly.setAttribute('fill', 'none');
            poly.setAttribute('stroke-linecap', 'round');
            poly.setAttribute('stroke-linejoin', 'round');
            if (d.type === 'highlighter') {
                poly.setAttribute('stroke-width', 12);
                poly.setAttribute('stroke-opacity', '0.35');
            } else {
                poly.setAttribute('stroke-width', width);
                poly.setAttribute('stroke-opacity', opacity);
            }
            poly.style.pointerEvents = 'stroke';
            g.appendChild(poly);
        } else if (d.type === 'regression_trend' && coords.length >= 2) {
            const dx = coords[1].x - coords[0].x;
            const dy = coords[1].y - coords[0].y;
            const offset = 25; // 25px dải kênh trên và dưới

            const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            poly.setAttribute('points', `${coords[0].x},${coords[0].y - offset} ${coords[1].x},${coords[1].y - offset} ${coords[1].x},${coords[1].y + offset} ${coords[0].x},${coords[0].y + offset}`);
            poly.setAttribute('fill', d.style.fillColor || 'rgba(41, 98, 255, 0.12)');
            g.appendChild(poly);

            const mLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            mLine.setAttribute('x1', coords[0].x);
            mLine.setAttribute('y1', coords[0].y);
            mLine.setAttribute('x2', coords[1].x);
            mLine.setAttribute('y2', coords[1].y);
            mLine.setAttribute('stroke', color);
            mLine.setAttribute('stroke-width', 1.5);
            mLine.setAttribute('stroke-dasharray', '4,4');
            g.appendChild(mLine);

            const uLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            uLine.setAttribute('x1', coords[0].x);
            uLine.setAttribute('y1', coords[0].y - offset);
            uLine.setAttribute('x2', coords[1].x);
            uLine.setAttribute('y2', coords[1].y - offset);
            uLine.setAttribute('stroke', '#089981');
            uLine.setAttribute('stroke-width', width);
            g.appendChild(uLine);

            const lLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            lLine.setAttribute('x1', coords[0].x);
            lLine.setAttribute('y1', coords[0].y + offset);
            lLine.setAttribute('x2', coords[1].x);
            lLine.setAttribute('y2', coords[1].y + offset);
            lLine.setAttribute('stroke', '#f23645');
            lLine.setAttribute('stroke-width', width);
            g.appendChild(lLine);
        } else if ((d.type === 'pitchfork' || d.type === 'schiff_pitchfork' || d.type === 'mod_schiff_pitchfork' || d.type === 'inside_pitchfork') && coords.length >= 3) {
            const fork = DrawingGeometry.calculatePitchfork(coords[0], coords[1], coords[2], d.type.replace('_pitchfork', ''));

            const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            poly.setAttribute('points', `${fork.upperLine[0].x},${fork.upperLine[0].y} ${fork.upperLine[1].x},${fork.upperLine[1].y} ${fork.lowerLine[1].x},${fork.lowerLine[1].y} ${fork.lowerLine[0].x},${fork.lowerLine[0].y}`);
            poly.setAttribute('fill', d.style.fillColor || 'rgba(41, 98, 255, 0.1)');
            g.appendChild(poly);

            const mLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            mLine.setAttribute('x1', fork.medianLine[0].x);
            mLine.setAttribute('y1', fork.medianLine[0].y);
            mLine.setAttribute('x2', fork.medianLine[1].x);
            mLine.setAttribute('y2', fork.medianLine[1].y);
            mLine.setAttribute('stroke', color);
            mLine.setAttribute('stroke-width', width);
            g.appendChild(mLine);

            const uLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            uLine.setAttribute('x1', fork.upperLine[0].x);
            uLine.setAttribute('y1', fork.upperLine[0].y);
            uLine.setAttribute('x2', fork.upperLine[1].x);
            uLine.setAttribute('y2', fork.upperLine[1].y);
            uLine.setAttribute('stroke', color);
            uLine.setAttribute('stroke-width', width);
            g.appendChild(uLine);

            const lLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            lLine.setAttribute('x1', fork.lowerLine[0].x);
            lLine.setAttribute('y1', fork.lowerLine[0].y);
            lLine.setAttribute('x2', fork.lowerLine[1].x);
            lLine.setAttribute('y2', fork.lowerLine[1].y);
            lLine.setAttribute('stroke', color);
            lLine.setAttribute('stroke-width', width);
            g.appendChild(lLine);
        } else if (d.type === 'fib_timezone' && coords.length >= 2) {
            const barSec = Math.max(60, Math.abs(d.points[1].time - d.points[0].time));
            const tzList = DrawingGeometry.calculateFibTimeZone(d.points[0].time, barSec, 8);
            tzList.forEach(tz => {
                const c = this.timePriceToCoord(tz.time, d.points[0].price);
                if (!c) return;

                const vLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                vLine.setAttribute('x1', c.x);
                vLine.setAttribute('y1', 0);
                vLine.setAttribute('x2', c.x);
                vLine.setAttribute('y2', '100%');
                vLine.setAttribute('stroke', color);
                vLine.setAttribute('stroke-width', 1);
                vLine.setAttribute('stroke-dasharray', '3,3');
                g.appendChild(vLine);

                const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                label.setAttribute('x', c.x + 3);
                label.setAttribute('y', 20);
                label.setAttribute('fill', color);
                label.setAttribute('font-size', '10');
                label.setAttribute('font-family', 'monospace');
                label.textContent = `F:${tz.fib}`;
                g.appendChild(label);
            });
        } else if (d.type === 'gann_box' && coords.length >= 2) {
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x);
            const minY = Math.min(coords[0].y, coords[1].y);
            const maxY = Math.max(coords[0].y, coords[1].y);

            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', minX);
            rect.setAttribute('y', minY);
            rect.setAttribute('width', Math.max(1, maxX - minX));
            rect.setAttribute('height', Math.max(1, maxY - minY));
            rect.setAttribute('stroke', color);
            rect.setAttribute('stroke-width', width);
            rect.setAttribute('fill', d.style.fillColor || 'rgba(41, 98, 255, 0.08)');
            g.appendChild(rect);

            // Đường chéo Gann
            const d1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            d1.setAttribute('x1', minX); d1.setAttribute('y1', minY);
            d1.setAttribute('x2', maxX); d1.setAttribute('y2', maxY);
            d1.setAttribute('stroke', color); d1.setAttribute('stroke-width', 1);
            g.appendChild(d1);

            const d2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            d2.setAttribute('x1', minX); d2.setAttribute('y1', maxY);
            d2.setAttribute('x2', maxX); d2.setAttribute('y2', minY);
            d2.setAttribute('stroke', color); d2.setAttribute('stroke-width', 1);
            g.appendChild(d2);
        } else if (d.type === 'gann_fan' && coords.length >= 2) {
            const angles = DrawingGeometry.calculateGannAngles(coords[0].x, coords[0].y, coords[1].x, coords[1].y);
            angles.forEach(a => {
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', coords[0].x);
                line.setAttribute('y1', coords[0].y);
                line.setAttribute('x2', a.endX);
                line.setAttribute('y2', a.endY);
                line.setAttribute('stroke', color);
                line.setAttribute('stroke-width', 1);
                g.appendChild(line);

                const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                txt.setAttribute('x', a.endX + 3);
                txt.setAttribute('y', a.endY);
                txt.setAttribute('fill', color);
                txt.setAttribute('font-size', '9');
                txt.textContent = a.label;
                g.appendChild(txt);
            });
        } else if ((d.type === 'long_position' || d.type === 'short_position') && coords.length >= 3) {
            const isLong = (d.type === 'long_position');
            const entryY = coords[0].y;
            const slY = coords[1].y;
            const tpY = coords[2].y;
            const boxWidth = Math.max(120, Math.abs(coords[1].x - coords[0].x) + 100);
            const startX = coords[0].x;

            const stats = DrawingGeometry.calculatePositionRiskReward(
                d.points[0].price, d.points[1].price, d.points[2].price, isLong, 0.1, 0.2, 0.0
            );

            // Vùng Target (Xanh)
            const targetMinY = Math.min(entryY, tpY);
            const targetHeight = Math.abs(tpY - entryY);
            const targetRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            targetRect.setAttribute('x', startX);
            targetRect.setAttribute('y', targetMinY);
            targetRect.setAttribute('width', boxWidth);
            targetRect.setAttribute('height', Math.max(1, targetHeight));
            targetRect.setAttribute('fill', 'rgba(8, 153, 129, 0.2)');
            targetRect.setAttribute('stroke', '#089981');
            targetRect.setAttribute('stroke-width', 1.5);
            g.appendChild(targetRect);

            // Vùng Stop Loss (Đỏ)
            const stopMinY = Math.min(entryY, slY);
            const stopHeight = Math.abs(slY - entryY);
            const stopRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            stopRect.setAttribute('x', startX);
            stopRect.setAttribute('y', stopMinY);
            stopRect.setAttribute('width', boxWidth);
            stopRect.setAttribute('height', Math.max(1, stopHeight));
            stopRect.setAttribute('fill', 'rgba(242, 54, 69, 0.2)');
            stopRect.setAttribute('stroke', '#f23645');
            stopRect.setAttribute('stroke-width', 1.5);
            g.appendChild(stopRect);

            // Vạch Entry
            const entryLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            entryLine.setAttribute('x1', startX);
            entryLine.setAttribute('y1', entryY);
            entryLine.setAttribute('x2', startX + boxWidth);
            entryLine.setAttribute('y2', entryY);
            entryLine.setAttribute('stroke', '#ffffff');
            entryLine.setAttribute('stroke-width', 2);
            g.appendChild(entryLine);

            // Thẻ thông số R:R
            const badgeRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            badgeRect.setAttribute('x', startX + 6);
            badgeRect.setAttribute('y', entryY - 12);
            badgeRect.setAttribute('width', 170);
            badgeRect.setAttribute('height', 24);
            badgeRect.setAttribute('rx', 4);
            badgeRect.setAttribute('fill', '#1e222d');
            badgeRect.setAttribute('stroke', '#2962ff');
            badgeRect.setAttribute('stroke-width', 1);
            g.appendChild(badgeRect);

            const badgeText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badgeText.setAttribute('x', startX + 12);
            badgeText.setAttribute('y', entryY + 4);
            badgeText.setAttribute('fill', '#ffffff');
            badgeText.setAttribute('font-size', '11');
            badgeText.setAttribute('font-family', 'monospace');
            badgeText.textContent = `R:R: ${stats.riskRewardRatio} | +$${stats.targetPnL} / -$${Math.abs(stats.stopPnL)}`;
            g.appendChild(badgeText);
        } else if (d.type === 'date_price_range' && coords.length >= 2) {
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x);
            const minY = Math.min(coords[0].y, coords[1].y);
            const maxY = Math.max(coords[0].y, coords[1].y);

            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', minX);
            rect.setAttribute('y', minY);
            rect.setAttribute('width', Math.max(1, maxX - minX));
            rect.setAttribute('height', Math.max(1, maxY - minY));
            rect.setAttribute('stroke', color);
            rect.setAttribute('stroke-width', width);
            rect.setAttribute('fill', 'rgba(41, 98, 255, 0.15)');
            g.appendChild(rect);

            const bars = Math.max(1, Math.round((maxX - minX) / 8));
            const metrics = DrawingGeometry.calculateDatePriceRange(d.points[0].price, d.points[1].price, d.points[0].time, d.points[1].time, bars, 0);

            const midX = (minX + maxX) / 2;
            const midY = (minY + maxY) / 2;

            const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bg.setAttribute('x', midX - 90);
            bg.setAttribute('y', midY - 14);
            bg.setAttribute('width', 180);
            bg.setAttribute('height', 28);
            bg.setAttribute('rx', 4);
            bg.setAttribute('fill', '#1e222d');
            bg.setAttribute('stroke', color);
            bg.setAttribute('stroke-width', 1);
            g.appendChild(bg);

            const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            txt.setAttribute('x', midX);
            txt.setAttribute('y', midY + 4);
            txt.setAttribute('text-anchor', 'middle');
            txt.setAttribute('fill', metrics.deltaPrice >= 0 ? '#089981' : '#f23645');
            txt.setAttribute('font-size', '10');
            txt.setAttribute('font-family', 'monospace');
            txt.textContent = `${metrics.deltaPrice >= 0 ? '+' : ''}${metrics.deltaPrice.toFixed(2)} (${metrics.pips}p) | ${bars} bars`;
            g.appendChild(txt);
        } else if (d.type === 'abcd' && coords.length >= 4) {
            // ABCD lines
            for (let k = 0; k < 3; k++) {
                const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                l.setAttribute('x1', coords[k].x); l.setAttribute('y1', coords[k].y);
                l.setAttribute('x2', coords[k+1].x); l.setAttribute('y2', coords[k+1].y);
                l.setAttribute('stroke', color); l.setAttribute('stroke-width', width);
                g.appendChild(l);
            }
            const abcdLabels = ['A', 'B', 'C', 'D'];
            coords.forEach((c, idx) => {
                const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                label.setAttribute('x', c.x); label.setAttribute('y', c.y - 8);
                label.setAttribute('text-anchor', 'middle');
                label.setAttribute('fill', '#ffffff');
                label.setAttribute('font-weight', 'bold');
                label.setAttribute('font-size', '12');
                label.textContent = abcdLabels[idx];
                g.appendChild(label);
            });
        } else if (d.type === 'elliott_wave_15' && coords.length >= 2) {
            for (let k = 0; k < coords.length - 1; k++) {
                const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                l.setAttribute('x1', coords[k].x); l.setAttribute('y1', coords[k].y);
                l.setAttribute('x2', coords[k+1].x); l.setAttribute('y2', coords[k+1].y);
                l.setAttribute('stroke', color); l.setAttribute('stroke-width', width);
                g.appendChild(l);
            }
            coords.forEach((c, idx) => {
                const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                badge.setAttribute('x', c.x); badge.setAttribute('y', c.y - 8);
                badge.setAttribute('text-anchor', 'middle');
                badge.setAttribute('fill', '#ff9800');
                badge.setAttribute('font-weight', 'bold');
                badge.setAttribute('font-size', '12');
                badge.textContent = `(${idx + 1})`;
                g.appendChild(badge);
            });
        } else if (d.type === 'elliott_wave_abc' && coords.length >= 2) {
            const labels = ['A', 'B', 'C', 'D'];
            for (let k = 0; k < coords.length - 1; k++) {
                const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                l.setAttribute('x1', coords[k].x); l.setAttribute('y1', coords[k].y);
                l.setAttribute('x2', coords[k+1].x); l.setAttribute('y2', coords[k+1].y);
                l.setAttribute('stroke', color); l.setAttribute('stroke-width', width);
                g.appendChild(l);
            }
            coords.forEach((c, idx) => {
                const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                badge.setAttribute('x', c.x); badge.setAttribute('y', c.y - 8);
                badge.setAttribute('text-anchor', 'middle');
                badge.setAttribute('fill', '#089981');
                badge.setAttribute('font-weight', 'bold');
                badge.setAttribute('font-size', '12');
                badge.textContent = `(${labels[idx] || idx})`;
                g.appendChild(badge);
            });
        } else if (d.type === 'head_shoulders' && coords.length >= 4) {
            const hsLabels = ['LS', 'Neck 1', 'Head', 'Neck 2', 'RS', 'Neck 3', 'Break'];
            for (let k = 0; k < coords.length - 1; k++) {
                const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                l.setAttribute('x1', coords[k].x); l.setAttribute('y1', coords[k].y);
                l.setAttribute('x2', coords[k+1].x); l.setAttribute('y2', coords[k+1].y);
                l.setAttribute('stroke', color); l.setAttribute('stroke-width', width);
                g.appendChild(l);
            }
            coords.forEach((c, idx) => {
                if (hsLabels[idx]) {
                    const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                    badge.setAttribute('x', c.x); badge.setAttribute('y', c.y - 8);
                    badge.setAttribute('text-anchor', 'middle');
                    badge.setAttribute('fill', '#ffffff');
                    badge.setAttribute('font-size', '10');
                    badge.textContent = hsLabels[idx];
                    g.appendChild(badge);
                }
            });
        } else if (d.type === 'text' && coords.length >= 1) {
            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', coords[0].x + 6);
            text.setAttribute('y', coords[0].y - 6);
            text.setAttribute('fill', d.style.textColor || '#ffffff');
            text.setAttribute('font-size', d.style.fontSize || 14);
            text.setAttribute('font-family', d.style.fontFamily || 'sans-serif');
            text.textContent = d.text || 'Ghi chú';
            text.style.pointerEvents = 'all';
            g.appendChild(text);
        }

        this.drawingsGroup.appendChild(g);
    }

    renderHandles(drawing) {
        if (!this.handlesGroup) return;

        for (let i = 0; i < drawing.points.length; i++) {
            const pt = drawing.points[i];
            const coord = this.timePriceToCoord(pt.time, pt.price);
            if (!coord) continue;

            const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            circle.setAttribute('cx', coord.x);
            circle.setAttribute('cy', coord.y);
            circle.setAttribute('r', 5);
            circle.setAttribute('fill', '#ffffff');
            circle.setAttribute('stroke', drawing.style.color || '#2962ff');
            circle.setAttribute('stroke-width', 2);
            circle.style.pointerEvents = 'all';
            circle.style.cursor = 'grab';
            circle.setAttribute('class', 'drawing-handle');
            circle.setAttribute('data-point-index', i);

            this.handlesGroup.appendChild(circle);
        }
    }

    renderPreview(points) {
        if (!this.previewGroup) return;
        this.previewGroup.innerHTML = '';

        const coords = points.map(p => this.timePriceToCoord(p.time, p.price)).filter(Boolean);
        if (coords.length < 2) return;

        const tool = this.activeTool;
        if (tool === 'rectangle' || tool === 'ruler' || tool === 'price_range' || tool === 'date_price_range') {
            const minX = Math.min(coords[0].x, coords[1].x);
            const maxX = Math.max(coords[0].x, coords[1].x);
            const minY = Math.min(coords[0].y, coords[1].y);
            const maxY = Math.max(coords[0].y, coords[1].y);
            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', minX);
            rect.setAttribute('y', minY);
            rect.setAttribute('width', Math.max(1, maxX - minX));
            rect.setAttribute('height', Math.max(1, maxY - minY));
            rect.setAttribute('stroke', '#2962ff');
            rect.setAttribute('stroke-width', 1.5);
            rect.setAttribute('stroke-dasharray', '4,4');
            rect.setAttribute('fill', 'rgba(41, 98, 255, 0.1)');
            this.previewGroup.appendChild(rect);
        } else if (tool === 'circle') {
            const r = Math.hypot(coords[1].x - coords[0].x, coords[1].y - coords[0].y);
            const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            c.setAttribute('cx', coords[0].x);
            c.setAttribute('cy', coords[0].y);
            c.setAttribute('r', r);
            c.setAttribute('stroke', '#2962ff');
            c.setAttribute('stroke-dasharray', '4,4');
            c.setAttribute('fill', 'rgba(41, 98, 255, 0.1)');
            this.previewGroup.appendChild(c);
        } else if (tool === 'polyline' || tool === 'brush' || tool === 'highlighter') {
            const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
            poly.setAttribute('points', coords.map(pt => `${pt.x},${pt.y}`).join(' '));
            poly.setAttribute('stroke', tool === 'highlighter' ? 'rgba(255, 235, 59, 0.5)' : '#2962ff');
            poly.setAttribute('stroke-width', tool === 'highlighter' ? 12 : 2);
            poly.setAttribute('fill', 'none');
            this.previewGroup.appendChild(poly);
        } else if (tool === 'channel' && coords.length >= 3) {
            const ch = DrawingGeometry.calculateChannel(coords[0].x, coords[0].y, coords[1].x, coords[1].y, coords[2].x, coords[2].y);
            const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            poly.setAttribute('points', ch.polygon.map(pt => `${pt.x},${pt.y}`).join(' '));
            poly.setAttribute('fill', 'rgba(41, 98, 255, 0.15)');
            poly.setAttribute('stroke', '#2962ff');
            poly.setAttribute('stroke-dasharray', '4,4');
            this.previewGroup.appendChild(poly);
        } else {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', coords[0].x);
            line.setAttribute('y1', coords[0].y);
            line.setAttribute('x2', coords[coords.length - 1].x);
            line.setAttribute('y2', coords[coords.length - 1].y);
            line.setAttribute('stroke', '#2962ff');
            line.setAttribute('stroke-width', 1.5);
            line.setAttribute('stroke-dasharray', '4,4');
            line.setAttribute('opacity', '0.75');
            this.previewGroup.appendChild(line);
        }
    }

    clearPreview() {
        if (this.previewGroup) {
            this.previewGroup.innerHTML = '';
        }
    }

    // ==========================================
    // LAYER ORDERING, DUPLICATE & ALERTS (V2)
    // ==========================================
    bringToFront(id) {
        const idx = this.drawings.findIndex(d => d.id === id);
        if (idx !== -1 && idx < this.drawings.length - 1) {
            this.saveUndoState();
            const [d] = this.drawings.splice(idx, 1);
            this.drawings.push(d);
            this.requestRender();
            return true;
        }
        return false;
    }

    sendToBack(id) {
        const idx = this.drawings.findIndex(d => d.id === id);
        if (idx > 0) {
            this.saveUndoState();
            const [d] = this.drawings.splice(idx, 1);
            this.drawings.unshift(d);
            this.requestRender();
            return true;
        }
        return false;
    }

    duplicate(id) {
        const orig = this.get(id);
        if (!orig) return null;
        this.saveUndoState();

        const offsetTime = 300; // 5 phút dịch chuyển
        const offsetPrice = 1.5; // 1.5 USD
        const newPoints = orig.points.map(p => ({
            time: p.time + offsetTime,
            price: Number((p.price + offsetPrice).toFixed(3))
        }));

        const clone = DrawingModel.create(orig.type, newPoints, {
            color: orig.style?.color,
            width: orig.style?.width,
            lineStyle: orig.style?.lineStyle,
            opacity: orig.style?.opacity,
            fillColor: orig.style?.fillColor,
            text: orig.text,
            scope: orig.scope
        });
        this.drawings.push(clone);
        this.select(clone.id);
        this.requestRender();
        return clone;
    }

    getTimeframeSeconds(tf = null) {
        const target = tf || this.currentTimeframe || this.options.timeframe || 'M15';
        const map = {
            'M1': 60,
            'M5': 300,
            'M15': 900,
            'M30': 1800,
            'H1': 3600,
            'H4': 14400,
            'D1': 86400,
            'W1': 604800,
            'MN': 2592000
        };
        return map[target] || 60;
    }

    setDrawingAlert(id, options = {}) {
        const drawing = this.get(id);
        if (!drawing) return null;
        drawing.alert = {
            enabled: options.enabled !== undefined ? !!options.enabled : true,
            condition: options.condition || 'touch',
            onceOnly: options.onceOnly !== undefined ? !!options.onceOnly : true,
            triggered: false,
            triggerCount: options.triggerCount || 0,
            lastCandleTime: null,
            lastCandleState: null
        };
        this.saveToStorage();
        return drawing;
    }

    resetDrawingAlert(id) {
        const drawing = this.get(id);
        if (!drawing || !drawing.alert) return null;
        drawing.alert.triggered = false;
        drawing.alert.lastCandleTime = null;
        drawing.alert.lastCandleState = null;
        this.saveToStorage();
        return drawing;
    }

    removeDrawingAlert(id) {
        const drawing = this.get(id);
        if (!drawing || !drawing.alert) return null;
        drawing.alert.enabled = false;
        drawing.alert.triggered = false;
        this.saveToStorage();
        return drawing;
    }

    checkAlerts(candle) {
        if (!candle || typeof candle !== 'object') return [];
        const tfSec = this.getTimeframeSeconds();
        const triggered = [];

        for (const d of this.drawings) {
            if (!d.alert || !d.alert.enabled) continue;
            // Bỏ qua bản vẽ bị ẩn hoặc bị khóa
            if (d.hidden || d.locked) continue;
            // Không phát trùng alert trên cùng một nến
            if (d.alert.lastCandleTime !== undefined && d.alert.lastCandleTime === candle.time) continue;
            // Quy tắc onceOnly
            if (d.alert.onceOnly !== false && d.alert.triggered) continue;

            if (!d.alert.triggerCount) d.alert.triggerCount = 0;
            if (d.alert.lastCandleState === undefined) d.alert.lastCandleState = null;

            let isTriggered = false;
            let currentState = null;
            const cond = d.alert.condition || 'touch';

            if (d.type === 'horizontal' && d.points.length > 0) {
                const P = d.points[0].price;
                const touches = candle.low <= P && candle.high >= P;
                const crosses = (candle.open < P && candle.close > P) || (candle.open > P && candle.close < P);
                isTriggered = (cond === 'cross') ? crosses : touches;
            } else if (d.type === 'horizontal_ray' && d.points.length > 0) {
                const P = d.points[0].price;
                const t0 = d.points[0].time;
                // Horizontal ray hướng về tương lai: chỉ kích hoạt khi candle.time >= t0
                if (candle.time >= t0) {
                    const touches = candle.low <= P && candle.high >= P;
                    const crosses = (candle.open < P && candle.close > P) || (candle.open > P && candle.close < P);
                    isTriggered = (cond === 'cross') ? crosses : touches;
                }
            } else if (d.type === 'vertical' && d.points.length > 0) {
                const t0 = d.points[0].time;
                // Điều kiện interval thời gian: candle.time <= t0 && t0 < candle.time + tfSec
                isTriggered = (candle.time <= t0 && t0 < candle.time + tfSec);
            } else if (d.type === 'crossline' && d.points.length > 0) {
                const P = d.points[0].price;
                const t0 = d.points[0].time;
                // Điều kiện kép: nến phải ở interval thời gian t0 VÀ biên độ giá bao trùm P
                const inTime = (candle.time <= t0 && t0 < candle.time + tfSec);
                const inPrice = (candle.low <= P && candle.high >= P);
                isTriggered = inTime && inPrice;
            } else if ((d.type === 'trendline' || d.type === 'info_line' || d.type === 'trend_angle') && d.points.length >= 2) {
                const p0 = d.points[0];
                const p1 = d.points[1];
                const minT = Math.min(p0.time, p1.time);
                const maxT = Math.max(p0.time, p1.time);
                // Nến phải nằm trong đoạn thời gian giữa 2 mút
                if (candle.time >= minT && candle.time <= maxT) {
                    const dt = p1.time - p0.time;
                    const P_line = (dt !== 0) ? p0.price + (p1.price - p0.price) * ((candle.time - p0.time) / dt) : p0.price;
                    const touches = candle.low <= P_line && candle.high >= P_line;
                    const crosses = (candle.open < P_line && candle.close > P_line) || (candle.open > P_line && candle.close < P_line);
                    isTriggered = (cond === 'cross') ? crosses : touches;
                }
            } else if (d.type === 'ray' && d.points.length >= 2) {
                const p0 = d.points[0];
                const p1 = d.points[1];
                const dt = p1.time - p0.time;

                if (dt > 0) {
                    // Ray hướng về tương lai: hợp lệ khi candle.time >= p0.time
                    if (candle.time >= p0.time) {
                        const P_ray = p0.price + (p1.price - p0.price) * ((candle.time - p0.time) / dt);
                        const touches = candle.low <= P_ray && candle.high >= P_ray;
                        const crosses = (candle.open < P_ray && candle.close > P_ray) || (candle.open > P_ray && candle.close < P_ray);
                        isTriggered = (cond === 'cross') ? crosses : touches;
                    }
                } else if (dt < 0) {
                    // Ray hướng về quá khứ: hợp lệ khi candle.time <= p0.time
                    if (candle.time <= p0.time) {
                        const P_ray = p0.price + (p1.price - p0.price) * ((candle.time - p0.time) / dt);
                        const touches = candle.low <= P_ray && candle.high >= P_ray;
                        const crosses = (candle.open < P_ray && candle.close > P_ray) || (candle.open > P_ray && candle.close < P_ray);
                        isTriggered = (cond === 'cross') ? crosses : touches;
                    }
                } else {
                    // dt === 0: Ray thẳng đứng tại p0.time
                    const inTime = (candle.time <= p0.time && p0.time < candle.time + tfSec);
                    if (inTime) {
                        if (p1.price > p0.price) {
                            // Hướng lên
                            isTriggered = candle.high >= p0.price;
                        } else if (p1.price < p0.price) {
                            // Hướng xuống
                            isTriggered = candle.low <= p0.price;
                        }
                    }
                }
            } else if (d.type === 'extended' && d.points.length >= 2) {
                const p0 = d.points[0];
                const p1 = d.points[1];
                const dt = p1.time - p0.time;
                const P_ext = (dt !== 0) ? p0.price + (p1.price - p0.price) * ((candle.time - p0.time) / dt) : p0.price;
                const touches = candle.low <= P_ext && candle.high >= P_ext;
                const crosses = (candle.open < P_ext && candle.close > P_ext) || (candle.open > P_ext && candle.close < P_ext);
                isTriggered = (cond === 'cross') ? crosses : touches;
            } else if (d.type === 'price_range' && d.points.length >= 2) {
                const minP = Math.min(d.points[0].price, d.points[1].price);
                const maxP = Math.max(d.points[0].price, d.points[1].price);
                // Semantics inside dựa trên giá đóng cửa close để ổn định enter/exit
                currentState = (candle.close >= minP && candle.close <= maxP) ? 'inside' : 'outside';
                const touches = candle.high >= minP && candle.low <= maxP;

                if (cond === 'enter') {
                    isTriggered = (d.alert.lastCandleState === 'outside' && currentState === 'inside');
                } else if (cond === 'exit') {
                    isTriggered = (d.alert.lastCandleState === 'inside' && currentState === 'outside');
                } else {
                    isTriggered = touches;
                }
            } else if (d.type === 'date_range' && d.points.length >= 2) {
                const minT = Math.min(d.points[0].time, d.points[1].time);
                const maxT = Math.max(d.points[0].time, d.points[1].time);
                // Interval overlap: candle.time < maxT && candle.time + tfSec > minT
                const isTimeOverlap = candle.time < maxT && (candle.time + tfSec) > minT;
                currentState = isTimeOverlap ? 'inside' : 'outside';

                if (cond === 'enter') {
                    isTriggered = (d.alert.lastCandleState === 'outside' && currentState === 'inside');
                } else if (cond === 'exit') {
                    isTriggered = (d.alert.lastCandleState === 'inside' && currentState === 'outside');
                } else {
                    isTriggered = isTimeOverlap;
                }
            } else if ((d.type === 'date_price_range' || d.type === 'rectangle') && d.points.length >= 2) {
                const minP = Math.min(d.points[0].price, d.points[1].price);
                const maxP = Math.max(d.points[0].price, d.points[1].price);
                const minT = Math.min(d.points[0].time, d.points[1].time);
                const maxT = Math.max(d.points[0].time, d.points[1].time);

                const isTimeOverlap = candle.time < maxT && (candle.time + tfSec) > minT;
                const isPriceInside = candle.close >= minP && candle.close <= maxP;
                const isPriceTouch = candle.high >= minP && candle.low <= maxP;

                currentState = (isTimeOverlap && isPriceInside) ? 'inside' : 'outside';

                if (cond === 'enter') {
                    isTriggered = (d.alert.lastCandleState === 'outside' && currentState === 'inside');
                } else if (cond === 'exit') {
                    isTriggered = (d.alert.lastCandleState === 'inside' && currentState === 'outside');
                } else {
                    isTriggered = isTimeOverlap && isPriceTouch;
                }
            } else if (d.points.length >= 2) {
                // Fallback đa giác / hình học khác: bounding box giá
                const minP = Math.min(...d.points.map(pt => pt.price));
                const maxP = Math.max(...d.points.map(pt => pt.price));
                if (candle.high >= minP && candle.low <= maxP) {
                    isTriggered = true;
                }
            }

            // Lưu trạng thái trước đó và timestamp nến vừa duyệt
            if (currentState !== null) {
                d.alert.lastCandleState = currentState;
            }
            d.alert.lastCandleTime = candle.time;

            if (isTriggered) {
                d.alert.triggered = true;
                d.alert.triggerCount = (d.alert.triggerCount || 0) + 1;
                d.alert.lastTriggeredAt = Date.now();
                triggered.push({
                    drawing: d,
                    candle,
                    time: Date.now(),
                    price: (d.type === 'horizontal' || d.type === 'horizontal_ray' || d.type === 'crossline') ? d.points[0].price : candle.close
                });
            }
        }

        if (triggered.length > 0) {
            this.saveToStorage();
            if (typeof this.options.onAlertTriggered === 'function') {
                triggered.forEach(item => this.options.onAlertTriggered(item));
            }
        }
        return triggered;
    }

    // ==========================================
    // LƯU TRỮ VÀ KHÔI PHỤC (LOCALSTORAGE SCHEMA V2 & TRANSACTIONAL BACKUP)
    // ==========================================
    getStorageKey() {
        const suffix = this.options.storageKeySuffix ? `:${this.options.storageKeySuffix}` : '';
        return `drawings:${this.symbol}:layout${suffix}`;
    }

    getBackupStorageKey() {
        const suffix = this.options.storageKeySuffix ? `:${this.options.storageKeySuffix}` : '';
        return `drawings:${this.symbol}:layout:backup${suffix}`;
    }

    saveBackupToStorage() {
        if (typeof localStorage === 'undefined') return;
        try {
            if (!Array.isArray(this.drawings)) return;
            // Chỉ ghi backup khi payload hiện tại hợp lệ
            const payload = {
                symbol: this.symbol,
                schemaVersion: 2,
                backedUpAt: Date.now(),
                drawings: this.drawings
            };
            localStorage.setItem(this.getBackupStorageKey(), JSON.stringify(payload));
        } catch (e) {
            console.error('Lỗi khi lưu backup drawing vào localStorage:', e);
        }
    }

    loadBackupFromStorage() {
        if (typeof localStorage === 'undefined') return false;
        try {
            const raw = localStorage.getItem(this.getBackupStorageKey());
            if (!raw) return false;
            const parsed = JSON.parse(raw);
            if (parsed && Array.isArray(parsed.drawings)) {
                this.drawings = parsed.drawings
                    .map(d => migrateDrawingV1toV2(d))
                    .filter(d => DrawingModel.validate(d));
                this.requestRender();
                return true;
            }
        } catch (e) {
            console.error('Lỗi khi khôi phục từ backup localStorage:', e);
        }
        return false;
    }

    saveToStorage() {
        if (typeof localStorage === 'undefined') return;
        try {
            const payload = {
                symbol: this.symbol,
                schemaVersion: 2,
                updatedAt: Date.now(),
                drawings: this.drawings
            };
            localStorage.setItem(this.getStorageKey(), JSON.stringify(payload));
        } catch (e) {
            console.error('Lỗi khi lưu drawing vào localStorage:', e);
        }
    }

    loadFromStorage() {
        if (typeof localStorage === 'undefined') return;
        try {
            const raw = localStorage.getItem(this.getStorageKey());
            if (!raw) return;

            const parsed = JSON.parse(raw);
            if (parsed && Array.isArray(parsed.drawings)) {
                // Tự động chuyển đổi v1 -> v2 không mất mát
                const valid = parsed.drawings
                    .map(d => migrateDrawingV1toV2(d))
                    .filter(d => DrawingModel.validate(d));
                this.drawings = valid;
                // Lưu transactional backup snapshot
                this.saveBackupToStorage();
                this.requestRender();
            } else {
                // Dữ liệu chính bị hỏng, thử khôi phục từ backup
                this.loadBackupFromStorage();
            }
        } catch (e) {
            console.error('Lỗi khi đọc drawing từ localStorage, thử khôi phục từ backup:', e);
            this.loadBackupFromStorage();
        }
    }

    exportJSON() {
        return JSON.stringify({
            symbol: this.symbol,
            exportedAt: new Date().toISOString(),
            schemaVersion: 2,
            drawings: this.drawings
        }, null, 2);
    }

    importJSON(jsonString) {
        try {
            if (typeof jsonString !== 'string' || !jsonString.trim()) {
                throw new Error('Chuỗi JSON không được để trống.');
            }
            const data = JSON.parse(jsonString);
            if (!data || !Array.isArray(data.drawings)) {
                throw new Error('Định dạng JSON không hợp lệ (thiếu mảng drawings).');
            }
            const valid = data.drawings
                .map(d => migrateDrawingV1toV2(d))
                .filter(d => DrawingModel.validate(d));

            if (data.drawings.length > 0 && valid.length === 0) {
                throw new Error('Tất cả bản vẽ trong tệp JSON đều không hợp lệ.');
            }

            // Transactional: Lưu backup trước khi thay đổi state hiện tại
            this.saveBackupToStorage();
            this.saveUndoState();
            this.drawings = valid;
            this.selectedId = null;
            this.saveToStorage();
            this.requestRender();
            return { success: true, count: valid.length };
        } catch (e) {
            // Transactional: Tuyệt đối không ghi đè this.drawings khi import lỗi
            return { success: false, error: e.message };
        }
    }

    notifyStateChange() {
        if (typeof this.options.onStateChange === 'function') {
            this.options.onStateChange({
                activeTool: this.activeTool,
                selectedId: this.selectedId,
                selectedDrawing: this.get(this.selectedId),
                canUndo: this.canUndo(),
                canRedo: this.canRedo(),
                count: this.drawings.length
            });
        }
    }

    destroy() {
        if (this.rafId && typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
            window.cancelAnimationFrame(this.rafId);
            this.rafId = null;
        }
        this.renderPending = false;

        for (const dispose of this.disposers) {
            try { dispose(); } catch (e) {}
        }
        this.disposers = [];

        if (this.svg && this.svg.parentNode) {
            this.svg.parentNode.removeChild(this.svg);
        }
        this.svg = null;
        this.drawings = [];
        this.undoStack = [];
        this.redoStack = [];
    }
}

// Hỗ trợ cả môi trường Node.js (test runner) và Trình duyệt
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        DrawingGeometry,
        DrawingToolRegistry,
        DrawingModel,
        migrateDrawingV1toV2,
        DrawingManager
    };
}
if (typeof window !== 'undefined') {
    window.DrawingGeometry = DrawingGeometry;
    window.DrawingToolRegistry = DrawingToolRegistry;
    window.DrawingModel = DrawingModel;
    window.migrateDrawingV1toV2 = migrateDrawingV1toV2;
    window.DrawingManager = DrawingManager;
}
