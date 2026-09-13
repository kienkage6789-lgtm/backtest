// smc_renderer.js - Lớp vẽ trực quan SVG cho Replay các chiến lược SMC (S01, S05, S09, Wave1, Confluence)
// Hiển thị nến theo trình tự thời gian, HTF Bias, Market Structure, FVG, OB, Liquidity Sweeps, Candidate Setups và Rejection Diagnostics.

class SMCReplayRenderer {
    constructor(tradingChart, container) {
        this.tradingChart = tradingChart;
        this.container = container;
        this.chart = tradingChart.chart;
        this.candleSeries = tradingChart.candleSeries;

        this.timeline = [];
        this.bookmarks = { candidates: [], rejections: [], fills: [], events: [] };
        this.currentBarIndex = 0;
        this.summary = {};

        // Layer toggles with clean default settings
        this.filters = {
            showCandles: true,
            showHTF: false,            // OFF by default
            showStructure: true,      // ON by default
            showLiquidity: false,      // OFF by default
            showFVG: false,            // OFF by default
            showOB: false,             // OFF by default
            showExecution: true,      // ON by default
            showCandidates: true,     // ON by default (selected candidates)
            showWinLossTrades: true,  // ON by default
            showConfluence: false,     // OFF by default
            showRejectedCandidates: false, // OFF by default
            strategyFilter: 'ALL',     // 'ALL', 'S01', 'S05', 'S09'
        };

        this.svg = null;
        this.hudBadge = null;
        this.rafId = null;

        this.initSvgOverlay();
        this.initHudOverlay();
        this.bindChartEvents();
    }

    initSvgOverlay() {
        if (!this.container) return;
        const existing = this.container.querySelector('#smc-replay-svg-overlay');
        if (existing) existing.remove();

        this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        this.svg.id = 'smc-replay-svg-overlay';
        this.svg.style.position = 'absolute';
        this.svg.style.top = '0';
        this.svg.style.left = '0';
        this.svg.style.width = '100%';
        this.svg.style.height = '100%';
        this.svg.style.pointerEvents = 'none';
        this.svg.style.zIndex = '12';
        this.container.appendChild(this.svg);
    }

    initHudOverlay() {
        if (!this.container) return;
        const existing = this.container.querySelector('#smc-htf-hud-badge');
        if (existing) existing.remove();

        this.hudBadge = document.createElement('div');
        this.hudBadge.id = 'smc-htf-hud-badge';
        this.hudBadge.className = 'smc-htf-hud-badge hidden';
        this.container.appendChild(this.hudBadge);
    }

    bindChartEvents() {
        if (!this.chart) return;
        const timeScale = this.chart.timeScale();
        if (timeScale.subscribeVisibleTimeRangeChange) {
            timeScale.subscribeVisibleTimeRangeChange(() => this.requestRender());
        }
        if (timeScale.subscribeVisibleLogicalRangeChange) {
            timeScale.subscribeVisibleLogicalRangeChange(() => this.requestRender());
        }
    }

    setTimelineData(data) {
        if (!data || !data.timeline) return;
        this.timeline = data.timeline;
        this.bookmarks = data.bookmarks || { candidates: [], rejections: [], fills: [], events: [] };
        this.summary = data.summary || {};
        this.currentBarIndex = 0;
        this.renderCurrentBar();
    }

    setBarIndex(index) {
        if (!this.timeline || this.timeline.length === 0) return;
        this.currentBarIndex = Math.max(0, Math.min(index, this.timeline.length - 1));
        this.renderCurrentBar();
    }

    renderCurrentBar() {
        this.requestRender();
        if (typeof window.onSMCBarIndexChanged === 'function') {
            window.onSMCBarIndexChanged(this.currentBarIndex, this.getCurrentBarState());
        }
    }

    getCurrentBarState() {
        if (!this.timeline || this.currentBarIndex >= this.timeline.length) return null;
        return this.timeline[this.currentBarIndex];
    }

    requestRender() {
        if (this.rafId) cancelAnimationFrame(this.rafId);
        this.rafId = requestAnimationFrame(() => this.render());
    }

    timeToX(timeStr) {
        if (!this.chart || !timeStr) return null;
        const ts = typeof timeStr === 'number' ? timeStr : Math.floor(new Date(timeStr).getTime() / 1000);
        const timeScale = this.chart.timeScale();
        return timeScale.timeToCoordinate(ts);
    }

    priceToY(price) {
        if (!this.candleSeries || typeof price !== 'number' || isNaN(price)) return null;
        return this.candleSeries.priceToCoordinate(price);
    }

    render() {
        if (!this.svg) return;
        while (this.svg.firstChild) {
            this.svg.removeChild(this.svg.firstChild);
        }

        const bar = this.getCurrentBarState();
        if (!bar) {
            if (this.hudBadge) this.hudBadge.classList.add('hidden');
            return;
        }

        // 1. HTF Bias HUD
        if (this.filters.showHTF) {
            this.renderHTFBiasHUD(bar);
        } else if (this.hudBadge) {
            this.hudBadge.classList.add('hidden');
        }

        // 2. Active Fair Value Gaps
        if (this.filters.showFVG && bar.active_state && bar.active_state.fvgs) {
            this.renderFVGs(bar.active_state.fvgs, bar);
        }

        // 3. Active Order Blocks
        if (this.filters.showOB && bar.active_state && bar.active_state.obs) {
            this.renderOrderBlocks(bar.active_state.obs, bar);
        }

        // 4. Liquidity Pools & Sweeps
        if (this.filters.showLiquidity) {
            if (bar.active_state && bar.active_state.pools) {
                this.renderLiquidityPools(bar.active_state.pools, bar);
            }
            if (bar.new_events && bar.new_events.sweeps) {
                this.renderSweeps(bar.new_events.sweeps, bar);
            }
        }

        // 5. Market Structure (BOS / CHoCH / MSS)
        // Keep all confirmed structure events up to the replay cursor visible;
        // rendering only bar.new_events made prior BOS/CHoCH disappear while scrubbing.
        if (this.filters.showStructure) {
            const structures = this.getStructureEventsThrough(this.currentBarIndex);
            const mssEvidence = this.getMssEvidenceThrough(this.currentBarIndex);
            this.renderStructures(structures, bar, mssEvidence);
        }

        // 6. Candidates & Positions
        if (this.filters.showCandidates && bar.candidates && bar.candidates.length > 0) {
            this.renderCandidates(bar.candidates, bar);
        }

        // 7. Active Position
        if (this.filters.showExecution && bar.position) {
            this.renderActivePosition(bar.position, bar);
        }

        // 8. Current Replay Bar Marker & Confluence Badge
        this.renderCurrentBarMarker(bar);
        if (bar.confluence) {
            this.renderConfluenceBadge(bar.confluence, bar);
        }
    }

    renderCurrentBarMarker(bar) {
        const x = this.timeToX(bar.time);
        if (x === null) return;

        // Vertical line highlight for current bar
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x);
        line.setAttribute('y1', 0);
        line.setAttribute('x2', x);
        line.setAttribute('y2', '100%');
        line.setAttribute('stroke', '#2962ff');
        line.setAttribute('stroke-width', '1');
        line.setAttribute('stroke-dasharray', '2,2');
        line.setAttribute('opacity', '0.5');
        this.svg.appendChild(line);

        // Bar Index marker badge at top
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', x);
        text.setAttribute('y', 15);
        text.setAttribute('text-anchor', 'middle');
        text.setAttribute('fill', '#2962ff');
        text.setAttribute('font-size', '10');
        text.setAttribute('font-weight', 'bold');
        text.setAttribute('font-family', 'monospace');
        text.textContent = `#${bar.bar_index}`;
        this.svg.appendChild(text);
    }

    renderConfluenceBadge(conf, currentBar) {
        if (!conf || (!conf.agreement_count && !conf.direction_conflict)) return;
        const x = this.timeToX(currentBar.time);
        if (x === null) return;

        if (conf.direction_conflict) {
            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', x);
            text.setAttribute('y', 32);
            text.setAttribute('text-anchor', 'middle');
            text.setAttribute('fill', '#f23645');
            text.setAttribute('font-size', '10');
            text.setAttribute('font-weight', 'bold');
            text.setAttribute('font-family', 'monospace');
            text.textContent = `⚠️ CONFLICT (BUY vs SELL)`;
            this.svg.appendChild(text);
        } else if (conf.agreement_count >= 2) {
            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', x);
            text.setAttribute('y', 32);
            text.setAttribute('text-anchor', 'middle');
            text.setAttribute('fill', '#ffd700');
            text.setAttribute('font-size', '10');
            text.setAttribute('font-weight', 'bold');
            text.setAttribute('font-family', 'monospace');
            text.textContent = `🤝 CONFLUENCE (${conf.agreement_count}/3: ${conf.aligned_strategies.join('+')})`;
            this.svg.appendChild(text);
        }
    }

    renderHTFBiasHUD(bar) {
        if (!this.hudBadge) return;
        const hb = bar.htf_bias;
        const act = bar.active_state || {};
        const bias = (hb?.bias || act.htf_bias || 'neutral').toLowerCase();
        const biasUpper = bias.toUpperCase();
        const isPending = !!(hb?.pending_reversal || (hb?.reason === 'choch_reversal_pending') || (act.htf_bias_status === 'REVERSAL_PENDING'));
        const biasStatus = isPending ? 'REVERSAL_PENDING' : (bias !== 'neutral' ? 'CONFIRMED' : 'UNCONFIRMED');
        const pendingRev = hb?.pending_reversal ? hb.pending_reversal.toUpperCase() : (act.htf_pending_reversal || 'NONE');
        const sourceStr = (hb?.source_event_index !== null && hb?.source_event_index !== undefined)
            ? `${hb.source_event_type || 'BOS'} #${hb.source_event_index}`
            : (act.htf_source_event || 'None');
        const chochPendingStr = (hb?.pending_reversal_event_index !== null && hb?.pending_reversal_event_index !== undefined)
            ? `#${hb.pending_reversal_event_index}`
            : (act.htf_choch_pending || 'NONE');

        const cssClass = isPending ? 'reversal-pending' : (bias === 'bullish' ? 'bullish' : (bias === 'bearish' ? 'bearish' : 'neutral'));
        const icon = bias === 'bullish' ? '▲' : (bias === 'bearish' ? '▼' : '◆');

        this.hudBadge.className = `smc-htf-hud-badge ${cssClass}`;
        this.hudBadge.innerHTML = `
            <div class="smc-hud-header">
                <span class="badge-dot"></span>
                <strong>HTF Bias:</strong> ${biasUpper} ${icon}
                <span class="smc-hud-status ${isPending ? 'status-pending' : 'status-confirmed'}">[${biasStatus}]</span>
            </div>
            <div class="smc-hud-details">
                <span>Pending Reversal: <strong>${pendingRev}</strong></span>
                <span>Bias Source: <strong>${sourceStr}</strong></span>
                <span>CHoCH Pending: <strong>${chochPendingStr}</strong></span>
            </div>
        `;
        this.hudBadge.classList.remove('hidden');
    }

    renderFVGs(fvgs, currentBar) {
        const currX = this.timeToX(currentBar.time);
        if (currX === null) return;

        fvgs.forEach(f => {
            const startX = this.timeToX(f.time);
            const topY = this.priceToY(f.top);
            const botY = this.priceToY(f.bottom);

            if (topY === null || botY === null) return;

            const x1 = startX !== null ? Math.max(0, startX) : 0;
            const x2 = f.filled && f.filled_at ? (this.timeToX(currentBar.time) || currX) : Math.max(x1 + 30, currX);
            const minY = Math.min(topY, botY);
            const height = Math.max(2, Math.abs(botY - topY));
            const width = Math.max(10, x2 - x1);

            const isBullish = f.direction.toLowerCase() === 'bullish';
            const color = isBullish ? '#00bcd4' : '#ff9800';
            const fill = isBullish ? 'rgba(0, 188, 212, 0.12)' : 'rgba(255, 152, 0, 0.12)';
            const opacity = f.filled ? '0.35' : '1.0';

            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', x1);
            rect.setAttribute('y', minY);
            rect.setAttribute('width', width);
            rect.setAttribute('height', height);
            rect.setAttribute('fill', fill);
            rect.setAttribute('stroke', color);
            rect.setAttribute('stroke-width', '1');
            rect.setAttribute('stroke-dasharray', f.filled ? '3,3' : 'none');
            rect.setAttribute('opacity', opacity);
            this.svg.appendChild(rect);

            // Label
            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', x1 + 4);
            text.setAttribute('y', minY + 11);
            text.setAttribute('fill', color);
            text.setAttribute('font-size', '9');
            text.setAttribute('font-family', 'monospace');
            text.setAttribute('opacity', opacity);
            const statusStr = f.filled ? '[FILLED]' : '[ACTIVE]';
            text.textContent = `FVG ${f.direction.toUpperCase()} ${statusStr}`;
            this.svg.appendChild(text);
        });
    }

    renderOrderBlocks(obs, currentBar) {
        const currX = this.timeToX(currentBar.time);
        if (currX === null) return;

        obs.forEach(o => {
            const startX = this.timeToX(o.time);
            const topY = this.priceToY(o.top);
            const botY = this.priceToY(o.bottom);

            if (topY === null || botY === null) return;

            const x1 = startX !== null ? Math.max(0, startX) : 0;
            const x2 = Math.max(x1 + 30, currX);
            const minY = Math.min(topY, botY);
            const height = Math.max(2, Math.abs(botY - topY));
            const width = Math.max(10, x2 - x1);

            const isBullish = o.direction.toLowerCase() === 'bullish';
            const color = isBullish ? '#2962ff' : '#7c4dff';
            const fill = isBullish ? 'rgba(41, 98, 255, 0.12)' : 'rgba(124, 77, 255, 0.12)';

            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', x1);
            rect.setAttribute('y', minY);
            rect.setAttribute('width', width);
            rect.setAttribute('height', height);
            rect.setAttribute('fill', fill);
            rect.setAttribute('stroke', color);
            rect.setAttribute('stroke-width', '1.2');
            this.svg.appendChild(rect);

            // Badge text
            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', x1 + 4);
            text.setAttribute('y', minY + 12);
            text.setAttribute('fill', '#ffffff');
            text.setAttribute('font-size', '9');
            text.setAttribute('font-weight', 'bold');
            text.setAttribute('font-family', 'monospace');
            const ageStr = o.age_bars !== undefined ? `${o.age_bars}b` : '';
            const qStr = o.quality ? `[${o.quality.toUpperCase()}]` : '';
            text.textContent = `OB ${o.direction.toUpperCase()} ${qStr} (${ageStr})`;
            this.svg.appendChild(text);
        });
    }

    renderLiquidityPools(pools, currentBar) {
        pools.forEach(p => {
            const y = this.priceToY(p.price);
            if (y === null) return;

            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', 0);
            line.setAttribute('y1', y);
            line.setAttribute('x2', '100%');
            line.setAttribute('y2', y);
            line.setAttribute('stroke', '#ffd54f');
            line.setAttribute('stroke-width', '1');
            line.setAttribute('stroke-dasharray', '4,4');
            line.setAttribute('opacity', '0.75');
            this.svg.appendChild(line);

            // Pool Badge
            const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badge.setAttribute('x', 60);
            badge.setAttribute('y', y - 3);
            badge.setAttribute('fill', '#ffd54f');
            badge.setAttribute('font-size', '9');
            badge.setAttribute('font-family', 'monospace');
            badge.textContent = `LIQUIDITY: ${p.kind ? p.kind.toUpperCase() : 'POOL'} @ ${p.price.toFixed(2)}`;
            this.svg.appendChild(badge);
        });
    }

    renderSweeps(sweeps, currentBar) {
        const x = this.timeToX(currentBar.time);
        if (x === null) return;

        sweeps.forEach(s => {
            const sweepY = this.priceToY(s.pool_price || s.sweep_price);
            if (sweepY === null) return;

            // Dot marker
            const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            circle.setAttribute('cx', x);
            circle.setAttribute('cy', sweepY);
            circle.setAttribute('r', '5');
            circle.setAttribute('fill', '#ff9800');
            circle.setAttribute('stroke', '#ffffff');
            circle.setAttribute('stroke-width', '1.5');
            this.svg.appendChild(circle);

            // Label
            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', x + 8);
            text.setAttribute('y', sweepY + 4);
            text.setAttribute('fill', '#ff9800');
            text.setAttribute('font-size', '10');
            text.setAttribute('font-weight', 'bold');
            text.setAttribute('font-family', 'monospace');
            text.textContent = `SWEEP ${s.direction ? s.direction.toUpperCase() : ''}`;
            this.svg.appendChild(text);
        });
    }

    getStructureEventsThrough(cursorIndex) {
        const result = [];
        const seen = new Set();
        this.timeline.slice(0, cursorIndex + 1).forEach((bar) => {
            (bar.new_events?.structures || []).forEach((st) => {
                const key = `${st.index ?? bar.bar_index}|${st.event_type}|${st.direction}|${st.broken_swing_index}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    result.push({ ...st, time: st.time || bar.time });
                }
            });
        });
        return result;
    }

    getMssEvidenceThrough(cursorIndex) {
        const result = [];
        const seen = new Set();
        this.timeline.slice(0, cursorIndex + 1).forEach((bar) => {
            (bar.candidates || []).forEach((candidate) => {
                (candidate.evidences || []).forEach((evidence) => {
                    if (evidence.kind !== 'structure_event') return;
                    const details = evidence.details || {};
                    const key = `${evidence.bar_index}|${details.event_type || ''}|${details.direction || ''}|${details.broken_swing_index || ''}`;
                    if (seen.has(key)) return;
                    seen.add(key);
                    result.push({
                        index: evidence.bar_index,
                        time: evidence.time || bar.time,
                        direction: details.direction,
                        event_type: details.event_type,
                        broken_swing_price: evidence.price,
                        broken_swing_index: details.broken_swing_index,
                        displacement: details.displacement,
                        structure_leg_id: details.structure_leg_id,
                    });
                });
            });
        });
        return result;
    }

    renderStructures(structures, currentBar, mssEvidence = []) {
        const mssKeys = new Set(mssEvidence.map((st) =>
            `${st.index}|${st.event_type}|${st.direction}|${st.broken_swing_index}`
        ));

        structures.forEach(st => {
            const eventTime = st.time || currentBar.time;
            const x = this.timeToX(eventTime);
            if (x === null) return;
            const y = this.priceToY(st.broken_swing_price);
            if (y === null) return;

            const eventType = String(st.event_type || '').toUpperCase();
            const structureKey = `${st.index}|${st.event_type}|${st.direction}|${st.broken_swing_index}`;
            const isMSS = mssKeys.has(structureKey);
            const isBOS = eventType === 'BOS';
            const isBullish = String(st.direction || '').toLowerCase() === 'bullish';
            // BOS bullish: xanh dương (#2962ff), BOS bearish: đỏ (#ef5350), CHoCH/MSS: vàng/cam (#ff9800)
            const color = isMSS ? '#ff9800' : (isBOS ? (isBullish ? '#2962ff' : '#ef5350') : '#ffb74d');
            const icon = isBullish ? '▲' : '▼';

            // Horizontal step line
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', Math.max(0, x - 120));
            line.setAttribute('y1', y);
            line.setAttribute('x2', x);
            line.setAttribute('y2', y);
            line.setAttribute('stroke', color);
            line.setAttribute('stroke-width', isMSS ? '2' : '1.5');
            line.setAttribute('stroke-dasharray', '5,3');
            this.svg.appendChild(line);

            // Badge
            const badgeBg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            badgeBg.setAttribute('x', x - 75);
            badgeBg.setAttribute('y', y - 16);
            badgeBg.setAttribute('width', 70);
            badgeBg.setAttribute('height', 16);
            badgeBg.setAttribute('rx', 3);
            badgeBg.setAttribute('fill', '#1e222d');
            badgeBg.setAttribute('stroke', color);
            badgeBg.setAttribute('stroke-width', '1');
            this.svg.appendChild(badgeBg);

            const badgeText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badgeText.setAttribute('x', x - 40);
            badgeText.setAttribute('y', y - 4);
            badgeText.setAttribute('text-anchor', 'middle');
            badgeText.setAttribute('fill', color);
            badgeText.setAttribute('font-size', '10');
            badgeText.setAttribute('font-weight', 'bold');
            badgeText.setAttribute('font-family', 'monospace');
            const dispStr = st.displacement ? ' [DISP]' : '';
            badgeText.textContent = `${isMSS ? 'MSS/' : ''}${st.event_type} ${icon}${dispStr}`;
            const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
            title.textContent = `${isMSS ? 'MSS linked by S1' : st.event_type} | ${st.direction} | bar ${st.index}`;
            badgeText.appendChild(title);
            this.svg.appendChild(badgeText);
        });
    }

    renderCandidates(candidates, currentBar) {
        const x = this.timeToX(currentBar.time);
        if (x === null) return;

        candidates.forEach(cand => {
            if (this.filters.strategyFilter !== 'ALL' && cand.strategy_id !== this.filters.strategyFilter) {
                return;
            }
            // Skip rejected candidates unless diagnostic showRejectedCandidates layer is active
            if (cand.status === 'rejected' && !this.filters.showRejectedCandidates) {
                return;
            }

            const entryY = this.priceToY(cand.entry_price);
            const slY = this.priceToY(cand.stop_loss);
            const tpY = this.priceToY(cand.take_profit);

            if (entryY === null || slY === null || tpY === null) return;

            const isBuy = cand.direction.toUpperCase() === 'BUY';
            const width = 120;
            const startX = x + 5;

            // TP Box - Tím/Xanh mờ nhẹ (#ab47bc / #26a69a)
            const tpMinY = Math.min(entryY, tpY);
            const tpHeight = Math.max(2, Math.abs(tpY - entryY));
            const tpRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            tpRect.setAttribute('x', startX);
            tpRect.setAttribute('y', tpMinY);
            tpRect.setAttribute('width', width);
            tpRect.setAttribute('height', tpHeight);
            tpRect.setAttribute('fill', 'rgba(38, 166, 154, 0.15)');
            tpRect.setAttribute('stroke', '#26a69a');
            tpRect.setAttribute('stroke-width', '1');
            this.svg.appendChild(tpRect);

            // SL Box - Đỏ mờ nhẹ (#ef5350)
            const slMinY = Math.min(entryY, slY);
            const slHeight = Math.max(2, Math.abs(slY - entryY));
            const slRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            slRect.setAttribute('x', startX);
            slRect.setAttribute('y', slMinY);
            slRect.setAttribute('width', width);
            slRect.setAttribute('height', slHeight);
            slRect.setAttribute('fill', 'rgba(239, 83, 80, 0.15)');
            slRect.setAttribute('stroke', '#ef5350');
            slRect.setAttribute('stroke-width', '1');
            this.svg.appendChild(slRect);

            // Entry line - Xanh (#26a69a)
            const entryLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            entryLine.setAttribute('x1', startX);
            entryLine.setAttribute('y1', entryY);
            entryLine.setAttribute('x2', startX + width);
            entryLine.setAttribute('y2', entryY);
            entryLine.setAttribute('stroke', '#26a69a');
            entryLine.setAttribute('stroke-width', '1.5');
            this.svg.appendChild(entryLine);

            // Status Badge
            const statusColor = cand.status === 'selected' ? '#26a69a' : (cand.status === 'eligible' ? '#2962ff' : '#ef5350');
            const badgeBg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            badgeBg.setAttribute('x', startX + 2);
            badgeBg.setAttribute('y', entryY - 14);
            badgeBg.setAttribute('width', width - 4);
            badgeBg.setAttribute('height', 14);
            badgeBg.setAttribute('rx', 2);
            badgeBg.setAttribute('fill', '#1e222d');
            badgeBg.setAttribute('stroke', statusColor);
            badgeBg.setAttribute('stroke-width', '1');
            this.svg.appendChild(badgeBg);

            const badgeText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            badgeText.setAttribute('x', startX + 6);
            badgeText.setAttribute('y', entryY - 3);
            badgeText.setAttribute('fill', statusColor);
            badgeText.setAttribute('font-size', '9');
            badgeText.setAttribute('font-weight', 'bold');
            badgeText.setAttribute('font-family', 'monospace');
            const reasonStr = cand.diagnostic_reasons && cand.diagnostic_reasons.length > 0 ? `:${cand.diagnostic_reasons[0]}` : '';
            badgeText.textContent = `${cand.strategy_id} ${cand.direction} R:R ${cand.planned_rr.toFixed(1)} [${cand.status.toUpperCase()}${reasonStr}]`;
            this.svg.appendChild(badgeText);
        });
    }

    renderActivePosition(pos, currentBar) {
        const entryY = this.priceToY(pos.entry_price);
        if (entryY === null) return;

        const isBuy = pos.direction.toUpperCase() === 'BUY';
        const color = isBuy ? '#26a69a' : '#ef5350';

        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', 0);
        line.setAttribute('y1', entryY);
        line.setAttribute('x2', '100%');
        line.setAttribute('y2', entryY);
        line.setAttribute('stroke', color);
        line.setAttribute('stroke-width', '2');
        line.setAttribute('stroke-dasharray', '6,4');
        this.svg.appendChild(line);

        // Position PnL Badge
        const pnlStr = pos.unrealized_pnl !== undefined ? `${pos.unrealized_pnl >= 0 ? '+' : ''}$${pos.unrealized_pnl.toFixed(2)}` : '';
        const pnlColor = pos.unrealized_pnl >= 0 ? '#26a69a' : '#ef5350';

        const badge = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        badge.setAttribute('x', 20);
        badge.setAttribute('y', entryY - 6);
        badge.setAttribute('fill', pnlColor);
        badge.setAttribute('font-size', '11');
        badge.setAttribute('font-weight', 'bold');
        badge.setAttribute('font-family', 'monospace');
        badge.textContent = `ACTIVE POSITION: ${pos.direction} @ ${pos.entry_price.toFixed(2)} | PnL: ${pnlStr}`;
        this.svg.appendChild(badge);
    }

    clear() {
        if (this.svg) {
            while (this.svg.firstChild) {
                this.svg.removeChild(this.svg.firstChild);
            }
        }
        if (this.hudBadge) {
            this.hudBadge.classList.add('hidden');
        }
    }

    destroy() {
        if (this.rafId) cancelAnimationFrame(this.rafId);
        this.clear();
        if (this.svg) {
            this.svg.remove();
            this.svg = null;
        }
        if (this.hudBadge) {
            this.hudBadge.remove();
            this.hudBadge = null;
        }
    }
}

// Window export
if (typeof window !== 'undefined') {
    window.SMCReplayRenderer = SMCReplayRenderer;
}
