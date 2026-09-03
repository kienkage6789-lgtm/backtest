// chart.js - Quản lý biểu đồ TradingView Lightweight Charts

class TradingChart {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.chart = null;
        this.candleSeries = null;
        this.volumeSeries = null;
        this.currentTimeframe = 'M15';
        this.currentCandles = [];
        this.initChart();
        this.setupResizeObserver();
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
            },
            grid: {
                vertLines: { color: '#1e222d' },
                horzLines: { color: '#1e222d' },
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: {
                    color: '#758696',
                    width: 1,
                    style: 3,
                    labelBackgroundColor: '#2a2e39',
                },
                horzLine: {
                    color: '#758696',
                    width: 1,
                    style: 3,
                    labelBackgroundColor: '#2a2e39',
                },
            },
            rightPriceScale: {
                borderColor: '#2a2e39',
                scaleMargins: {
                    top: 0.1,
                    bottom: 0.25,
                },
            },
            timeScale: {
                borderColor: '#2a2e39',
                timeVisible: true,
                secondsVisible: false,
            },
            handleScroll: {
                vertTouchDrag: false,
            },
        };

        this.chart = LightweightCharts.createChart(this.container, chartOptions);

        // Nến chính (Candlestick Series)
        this.candleSeries = this.chart.addCandlestickSeries({
            upColor: '#089981',
            downColor: '#f23645',
            borderUpColor: '#089981',
            borderDownColor: '#f23645',
            wickUpColor: '#089981',
            wickDownColor: '#f23645',
            priceFormat: {
                type: 'price',
                precision: 3,
                minMove: 0.001,
            },
        });

        // Khối lượng (Volume Histogram)
        this.volumeSeries = this.chart.addHistogramSeries({
            color: '#26a69a',
            priceFormat: {
                type: 'volume',
            },
            priceScaleId: '', // overlay
        });

        this.volumeSeries.priceScale().applyOptions({
            scaleMargins: {
                top: 0.8,
                bottom: 0,
            },
        });

        this.setupCrosshairLegend();
    }

    setupResizeObserver() {
        const resizeObserver = new ResizeObserver(entries => {
            if (entries.length === 0 || !entries[0].contentRect) return;
            const { width, height } = entries[0].contentRect;
            this.chart.applyOptions({ width, height });
        });
        resizeObserver.observe(this.container);
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
        this.renderLegend(last.open, last.high, last.low, last.close, last.volume);
    }

    renderLegend(o, h, l, c, v) {
        const elO = document.getElementById('leg-open');
        const elH = document.getElementById('leg-high');
        const elL = document.getElementById('leg-low');
        const elC = document.getElementById('leg-close');
        const elV = document.getElementById('leg-vol');
        const elDiff = document.getElementById('leg-diff');

        if (!elO) return;

        elO.textContent = o.toFixed(3);
        elH.textContent = h.toFixed(3);
        elL.textContent = l.toFixed(3);
        elC.textContent = c.toFixed(3);
        elV.textContent = Number(v).toLocaleString();

        const diff = c - o;
        const diffPercent = ((diff / o) * 100).toFixed(2);
        const sign = diff >= 0 ? '+' : '';
        elDiff.textContent = `${sign}${diff.toFixed(3)} (${sign}${diffPercent}%)`;

        if (diff >= 0) {
            elC.className = 'legend-val val-up';
            elDiff.className = 'legend-val val-up';
        } else {
            elC.className = 'legend-val val-down';
            elDiff.className = 'legend-val val-down';
        }
    }

    setCandles(candles) {
        this.currentCandles = candles;
        
        // Đảm bảo dữ liệu đã sắp xếp tăng dần theo timestamp
        const sorted = [...candles].sort((a, b) => a.time - b.time);

        // Format cho Candlestick Series
        const candleData = sorted.map(c => ({
            time: c.time,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
        }));

        // Format cho Volume Series
        const volumeData = sorted.map(c => ({
            time: c.time,
            value: c.volume,
            color: c.close >= c.open ? 'rgba(8, 153, 129, 0.45)' : 'rgba(242, 54, 69, 0.45)',
        }));

        this.candleSeries.setData(candleData);
        this.volumeSeries.setData(volumeData);
        this.updateLegendFromLastBar();
        this.chart.timeScale().fitContent();
    }

    setMarkers(markers) {
        /*
        markers: Array<{
            time: number,
            position: 'aboveBar' | 'belowBar' | 'inBar',
            color: string,
            shape: 'arrowUp' | 'arrowDown' | 'circle',
            text: string
        }>
        */
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
            value: candle.volume,
            color: candle.close >= candle.open ? 'rgba(8, 153, 129, 0.45)' : 'rgba(242, 54, 69, 0.45)',
        });
        this.renderLegend(candle.open, candle.high, candle.low, candle.close, candle.volume);
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
}

window.TradingChart = TradingChart;
