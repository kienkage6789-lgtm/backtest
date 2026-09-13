// chart.js - Quản lý biểu đồ TradingView Lightweight Charts

class TradingChart {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.containerId = containerId;
        this.chart = null;
        this.candleSeries = null;
        this.volumeSeries = null;
        this.currentTimeframe = 'M15';
        this.currentCandles = [];
        this.drawingManager = null;
        this.initChart();
        this.setupResizeObserver();
        this.initDrawingManager();
        if (typeof window !== 'undefined') {
            if (this.containerId === 'chart-container') {
                window.tradingChart = this;
            } else if (this.containerId === 'chart-container-2') {
                window.secondaryChart = this;
            }
        }
    }

    initDrawingManager() {
        if (window.DrawingManager && this.container) {
            this.drawingManager = new window.DrawingManager(this, this.container, {
                symbol: 'XAUUSD',
                timeframe: this.currentTimeframe,
                storageKeySuffix: this.containerId === 'chart-container-2' ? 'secondary' : 'main',
                onStateChange: (state) => {
                    if (window.onDrawingStateChange) {
                        window.onDrawingStateChange(state, this);
                    }
                },
                onAlertTriggered: (alertData) => {
                    if (window.onDrawingAlertTriggered) {
                        window.onDrawingAlertTriggered(alertData, this);
                    }
                },
                onOpenProperties: (drawing) => {
                    if (window.onDrawingOpenProperties) {
                        window.onDrawingOpenProperties(drawing, this);
                    }
                },
                onContextMenu: (drawing, event) => {
                    if (window.onDrawingContextMenu) {
                        window.onDrawingContextMenu(drawing, event, this);
                    }
                }
            });
        }
    }

    initChart() {
        if (!window.LightweightCharts) {
            console.error("LightweightCharts chưa được tải!");
            return;
        }

        const chartOptions = {
            layout: {
                background: { color: '#131722' },
                textColor: '#d1d4dc',
                fontFamily: "-apple-system, BlinkMacSystemFont, 'Trebuchet MS', Roboto, Ubuntu, sans-serif",
                fontSize: 11,
            },
            grid: {
                vertLines: { color: '#1e222d' },
                horzLines: { color: '#1e222d' },
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: {
                    color: '#787b86',
                    width: 1,
                    style: 3, // dashed
                    labelBackgroundColor: '#2a2e39',
                },
                horzLine: {
                    color: '#787b86',
                    width: 1,
                    style: 3, // dashed
                    labelBackgroundColor: '#2a2e39',
                },
            },
            rightPriceScale: {
                borderColor: '#2a2e39',
                scaleMargins: {
                    top: 0.08,
                    bottom: 0.22, // candle series leaves space for volume
                },
                alignLabels: true,
                autoScale: true,
            },
            timeScale: {
                borderColor: '#2a2e39',
                timeVisible: true,
                secondsVisible: false,
                rightOffset: 12,
                barSpacing: 6,
                minBarSpacing: 2,
            },
            handleScale: {
                mouseWheel: true,
                pinch: true,
                axisPressedMouseMove: {
                    time: true,
                    price: true,
                },
            },
            handleScroll: {
                mouseWheel: false,
                pressedMouseMove: true,
                horzTouchDrag: true,
                vertTouchDrag: false,
            },
        };

        this.chart = LightweightCharts.createChart(this.container, chartOptions);

        // Nến chính (Candlestick Series) - TradingView dark colors: #26A69A / #EF5350
        this.candleSeries = this.chart.addCandlestickSeries({
            upColor: '#26a69a',
            downColor: '#ef5350',
            borderUpColor: '#26a69a',
            borderDownColor: '#ef5350',
            wickUpColor: '#26a69a',
            wickDownColor: '#ef5350',
            lastValueVisible: true,
            priceLineVisible: true,
            priceLineWidth: 1,
            priceLineColor: '#2962ff',
            priceLineSource: 0,
            priceFormat: {
                type: 'price',
                precision: 3,
                minMove: 0.001,
            },
        });

        // Khối lượng (Volume Histogram Series) - chiếm 15-18% đáy chart
        this.volumeSeries = this.chart.addHistogramSeries({
            color: 'rgba(38, 166, 154, 0.30)',
            priceFormat: {
                type: 'volume',
            },
            priceScaleId: '', // overlay inside chart
        });

        this.volumeSeries.priceScale().applyOptions({
            scaleMargins: {
                top: 0.82, // 18% height at bottom
                bottom: 0,
            },
        });

        this.setupCrosshairLegend();
    }

    setupResizeObserver() {
        this.resizeObserver = new ResizeObserver(entries => {
            if (entries.length === 0 || !entries[0].contentRect) return;
            const { width, height } = entries[0].contentRect;
            if (this.chart) {
                this.chart.applyOptions({ width, height });
            }
            if (this.drawingManager) {
                this.drawingManager.requestRender();
            }
        });
        if (this.container) {
            this.resizeObserver.observe(this.container);
        }
    }

    handleResize() {
        if (!this.container) return;
        const rect = this.container.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
            if (this.chart) {
                this.chart.applyOptions({ width: rect.width, height: rect.height });
            }
            if (this.drawingManager) {
                this.drawingManager.requestRender();
            }
        }
    }

    setTimeframe(tf) {
        this.currentTimeframe = tf || 'M15';
        const elTf = document.getElementById('status-tf');
        if (elTf) elTf.textContent = this.currentTimeframe;
        this.updateLegendFromLastBar();
    }

    setVolumeVisible(visible) {
        if (this.volumeSeries) {
            this.volumeSeries.applyOptions({ visible: !!visible });
        }
    }

    setupCrosshairLegend() {
        this.chart.subscribeCrosshairMove(param => {
            if (!param || !param.time || !param.seriesData.get(this.candleSeries)) {
                this.updateLegendFromLastBar();
                return;
            }

            const candle = param.seriesData.get(this.candleSeries);
            const volumeData = param.seriesData.get(this.volumeSeries);
            const vol = volumeData ? volumeData.value : (candle.volume || 0);

            this.renderLegend(candle.open, candle.high, candle.low, candle.close, vol);
        });
    }

    updateLegendFromLastBar() {
        if (!this.currentCandles || this.currentCandles.length === 0) return;
        const last = this.currentCandles[this.currentCandles.length - 1];
        this.renderLegend(last.open, last.high, last.low, last.close, last.volume || 0);
    }

    renderLegend(o, h, l, c, v) {
        const elO = document.getElementById('leg-open');
        const elH = document.getElementById('leg-high');
        const elL = document.getElementById('leg-low');
        const elC = document.getElementById('leg-close');
        const elV = document.getElementById('leg-vol');
        const elDiff = document.getElementById('leg-diff');
        const elTf = document.getElementById('status-tf');

        if (elTf && this.currentTimeframe) {
            elTf.textContent = this.currentTimeframe;
        }

        if (!elO || typeof o !== 'number' || typeof c !== 'number') return;

        elO.textContent = o.toFixed(3);
        if (elH) elH.textContent = Number(h).toFixed(3);
        if (elL) elL.textContent = Number(l).toFixed(3);
        elC.textContent = c.toFixed(3);
        if (elV) elV.textContent = Number(v || 0).toLocaleString('en-US');

        const diff = c - o;
        const diffPercent = o !== 0 ? ((diff / o) * 100).toFixed(2) : '0.00';
        const sign = diff >= 0 ? '+' : '';
        if (elDiff) {
            elDiff.textContent = `${sign}${diff.toFixed(3)} (${sign}${diffPercent}%)`;
        }

        if (diff >= 0) {
            elC.className = 'legend-val val-up';
            if (elDiff) elDiff.className = 'legend-val val-up';
        } else {
            elC.className = 'legend-val val-down';
            if (elDiff) elDiff.className = 'legend-val val-down';
        }
    }

    autoFit(count = 350) {
        if (!this.chart) return;
        if (!this.currentCandles || this.currentCandles.length === 0) {
            this.chart.timeScale().fitContent();
            return;
        }
        const len = this.currentCandles.length;
        if (len <= count) {
            this.chart.timeScale().fitContent();
        } else {
            this.chart.timeScale().setVisibleLogicalRange({
                from: Math.max(0, len - count),
                to: len + 12,
            });
        }
    }

    zoomIn() {
        if (!this.chart) return;
        const timeScale = this.chart.timeScale();
        const range = timeScale.getVisibleLogicalRange();
        if (range && typeof range.from === 'number' && typeof range.to === 'number') {
            const width = range.to - range.from;
            if (width > 8) {
                const delta = Math.max(4, width * 0.25);
                timeScale.setVisibleLogicalRange({
                    from: Math.round(range.from + delta / 2),
                    to: Math.round(range.to - delta / 2),
                });
                return;
            }
        }
        // Fallback via barSpacing
        const currentOptions = this.chart.options().timeScale;
        const currentSpacing = currentOptions.barSpacing || 6;
        this.chart.applyOptions({
            timeScale: {
                barSpacing: Math.min(60, currentSpacing * 1.3)
            }
        });
    }

    zoomOut() {
        if (!this.chart) return;
        const timeScale = this.chart.timeScale();
        const range = timeScale.getVisibleLogicalRange();
        if (range && typeof range.from === 'number' && typeof range.to === 'number') {
            const width = range.to - range.from;
            const delta = Math.max(6, width * 0.3);
            timeScale.setVisibleLogicalRange({
                from: Math.round(range.from - delta / 2),
                to: Math.round(range.to + delta / 2),
            });
            return;
        }
        // Fallback via barSpacing
        const currentOptions = this.chart.options().timeScale;
        const currentSpacing = currentOptions.barSpacing || 6;
        this.chart.applyOptions({
            timeScale: {
                barSpacing: Math.max(0.5, currentSpacing * 0.75)
            }
        });
    }

    scrollToLatest() {
        if (!this.chart) return;
        this.chart.timeScale().scrollToPosition(0, true);
        this.chart.timeScale().scrollToRealTime();
    }

    setCandles(candles, isInitialLoad = false) {
        this.currentCandles = candles || [];
        
        // Đảm bảo dữ liệu đã sắp xếp tăng dần theo timestamp
        const sorted = [...this.currentCandles].sort((a, b) => a.time - b.time);

        // Format cho Candlestick Series
        const candleData = sorted.map(c => ({
            time: c.time,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
        }));

        // Format cho Volume Series với màu sắc TradingView (#26A69A / #EF5350) và opacity 0.30
        const volumeData = sorted.map(c => ({
            time: c.time,
            value: c.volume || 0,
            color: c.close >= c.open ? 'rgba(38, 166, 154, 0.30)' : 'rgba(239, 83, 80, 0.30)',
        }));

        this.candleSeries.setData(candleData);
        this.volumeSeries.setData(volumeData);
        this.updateLegendFromLastBar();

        // CHỈ fitContent hoặc đặt dải nến mặc định 300-500 nến khi load dữ liệu mới lần đầu!
        // Tuyệt đối không gọi fitContent() trong mỗi nhịp Replay / update.
        if (isInitialLoad) {
            this.autoFit(350);
        }

        if (this.drawingManager) {
            this.drawingManager.requestRender();
        }
    }

    setMarkers(markers) {
        if (this.candleSeries) {
            this.candleSeries.setMarkers(markers || []);
        }
    }

    clearMarkers() {
        if (this.candleSeries) {
            this.candleSeries.setMarkers([]);
        }
    }

    scrollToTime(timestamp) {
        if (this.chart) {
            this.chart.timeScale().scrollToPosition(0, false);
        }
    }

    updateBar(candle) {
        if (!candle) return;
        this.currentCandles.push(candle);
        this.candleSeries.update({
            time: candle.time,
            open: candle.open,
            high: candle.high,
            low: candle.low,
            close: candle.close,
        });
        this.volumeSeries.update({
            time: candle.time,
            value: candle.volume || 0,
            color: candle.close >= candle.open ? 'rgba(38, 166, 154, 0.30)' : 'rgba(239, 83, 80, 0.30)',
        });
        this.renderLegend(candle.open, candle.high, candle.low, candle.close, candle.volume || 0);
        if (this.drawingManager) {
            this.drawingManager.requestRender();
            this.drawingManager.checkAlerts(candle);
        }
    }

    subscribeClick(callback) {
        if (this.chart) {
            this.chart.subscribeClick(param => {
                if (!param || !param.time) return;
                const candle = param.seriesData ? param.seriesData.get(this.candleSeries) : null;
                callback({
                    time: param.time,
                    point: param.point,
                    candle: candle
                });
            });
        }
    }

    destroy() {
        if (this.drawingManager) {
            this.drawingManager.destroy();
            this.drawingManager = null;
        }
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
            this.resizeObserver = null;
        }
        if (this.chart) {
            this.chart.remove();
            this.chart = null;
            this.candleSeries = null;
            this.volumeSeries = null;
        }
        this.currentCandles = [];
    }
}

window.TradingChart = TradingChart;
