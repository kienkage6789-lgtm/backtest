"""
Append Group C (Tests 26-38) and Group D (Tests 39-55) to tests/test_smc_strategy_s09.py
"""

target_file = r"D:\tool\backtest\tests\test_smc_strategy_s09.py"

content = '''
    # =========================================================================
    # Group C: Sweep Admission & Bias (Tests 26-38)
    # =========================================================================

    def test_26_bullish_low_sweep_creates_buy_narrative(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 5 inside NY AM window (10:05 NY time)
        sw = _make_sweep(index=5, direction="bullish", pool_kind="equal_lows")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.direction, "BUY")
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_27_bearish_high_sweep_creates_sell_narrative(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bearish", pool_kind="equal_highs")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bearish")
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.direction, "SELL")
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_28_direction_pool_kind_mismatch_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Bullish sweep on equal_highs is invalid
        sw = _make_sweep(index=5, direction="bullish", pool_kind="equal_highs")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_29_invalid_or_wrong_mode_sweep_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        sw_invalid = _make_sweep(index=5, direction="bullish", valid=False)
        ctx1 = _make_context(bar_index=5, sweeps=[sw_invalid], bias="bullish")
        strat.evaluate(ctx1)
        self.assertEqual(len(strat._narratives), 0)

        strat.reset()
        sw_swing = _make_sweep(index=5, direction="bullish", mode="swing")
        ctx2 = _make_context(bar_index=5, sweeps=[sw_swing], bias="bullish")
        strat.evaluate(ctx2)
        self.assertEqual(len(strat._narratives), 0)

    def test_30_old_rolling_buffer_sweep_not_retrospectively_ingested(self):
        strat = S09ICTSilverBulletStrategy()
        # Sweep at bar 3 evaluating at bar 5
        sw = _make_sweep(index=3, direction="bullish", confirmed_at=3, swept_at=3)
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_31_sweep_close_before_window_start_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # 09:59 NY time (before 10:00 start)
        dt = datetime.datetime(2024, 5, 15, 9, 58, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)  # closes at 09:59
        sw = _make_sweep(index=0, direction="bullish")
        ctx = _make_context(bar_index=0, open_time=o_ts, close_time=c_ts, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_32_sweep_close_exact_window_start_accepted(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar from 09:59 to 10:00 NY time (closes exact at 10:00:00 start)
        dt = datetime.datetime(2024, 5, 15, 9, 59, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)  # closes exact at 10:00:00
        sw = _make_sweep(index=0, direction="bullish")
        ctx = _make_context(bar_index=0, open_time=o_ts, close_time=c_ts, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 1)

    def test_33_sweep_close_exact_window_end_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar from 10:59 to 11:00 NY time (closes exact at 11:00:00 end)
        dt = datetime.datetime(2024, 5, 15, 10, 59, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)  # closes exact at 11:00:00
        sw = _make_sweep(index=0, direction="bullish")
        ctx = _make_context(bar_index=0, open_time=o_ts, close_time=c_ts, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_34_aligned_htf_bias_accepted(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 1)

    def test_35_neutral_htf_bias_accepted(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="neutral")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 1)

    def test_36_opposed_htf_bias_rejected_terminal(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias="bearish")
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_37_missing_htf_bias_fails_closed(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx = _make_context(bar_index=5, sweeps=[sw], bias=None, htf_bias=None)
        strat.evaluate(ctx)
        self.assertEqual(len(strat._narratives), 0)

    def test_38_bias_becomes_opposed_during_active_narrative_terminal_no_revival(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        ctx1 = _make_context(bar_index=5, sweeps=[sw], bias="bullish")
        strat.evaluate(ctx1)
        self.assertEqual(len(strat._narratives), 1)

        # Next bar: bias turns bearish
        ctx2 = _make_context(bar_index=6, bias="bearish")
        strat.evaluate(ctx2)
        self.assertEqual(len(strat._narratives), 0)

        # Bar 7: bias turns bullish again -> narrative does not revive!
        ctx3 = _make_context(bar_index=7, bias="bullish")
        strat.evaluate(ctx3)
        self.assertEqual(len(strat._narratives), 0)

    # =========================================================================
    # Group D: MSS / FVG Linkage (Tests 39-55)
    # =========================================================================

    def test_39_valid_sweep_choch_fvg_pair_transitions_to_fvg_ready(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 5: Sweep
        sw = _make_sweep(index=5, direction="bullish")
        ctx5 = _make_context(bar_index=5, sweeps=[sw])
        strat.evaluate(ctx5)

        # Bar 6: CHoCH breakout with FVG (formed at bar 5, confirmed at bar 6)
        mss = _make_structure(index=6, direction="bullish", event_type="CHoCH", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        ctx6 = _make_context(bar_index=6, structure_events=[mss], fvgs=[fvg])
        strat.evaluate(ctx6)

        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)
        self.assertEqual(narr.ready_at, 6)

    def test_40_bos_event_accepted_as_mss(self):
        strat = S09ICTSilverBulletStrategy()
        sw = _make_sweep(index=5, direction="bullish")
        strat.evaluate(_make_context(bar_index=5, sweeps=[sw]))

        mss = _make_structure(index=6, direction="bullish", event_type="BOS", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)
        self.assertEqual(narr.mss.event_type, "BOS")

    def test_41_wick_break_structure_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=6, direction="bullish", break_type="wick", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_42_displacement_false_rejected_when_required(self):
        strat = S09ICTSilverBulletStrategy(S09Config(require_displacement=True))
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=6, direction="bullish", displacement=False, structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_43_wrong_direction_or_mode_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Opposite direction MSS
        mss_bear = _make_structure(index=6, direction="bearish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss_bear], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_44_old_mss_in_rolling_buffer_not_retrospectively_transitioned(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # MSS at bar 5 (same as sweep) evaluating at bar 6
        mss_old = _make_structure(index=5, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss_old], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_45_mss_close_at_window_end_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 59 closes at exact 11:00:00 (window end)
        dt = datetime.datetime(2024, 5, 15, 10, 59, 0, tzinfo=NEW_YORK_TZ)
        o_ts = pd.Timestamp(dt).tz_convert("UTC")
        c_ts = o_ts + pd.Timedelta(minutes=1)

        # Ingest sweep earlier
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        # Feed up to bar 58
        _feed(strat, _make_context(bar_index=58))

        mss = _make_structure(index=59, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=58, direction="bullish", confirmed_at=59, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=59, open_time=o_ts, close_time=c_ts, structure_events=[mss], fvgs=[fvg]))

        # At bar 59, close is at window end, so MSS cannot confirm inside window
        self.assertEqual(len(strat._narratives), 1)
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_46_fvg_confirmed_inside_window_passes(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)

    def test_47_fvg_formed_in_window_but_confirmed_at_or_after_window_end_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        _feed(strat, _make_context(bar_index=58))

        # Bar 59 closes at 11:00
        dt59 = datetime.datetime(2024, 5, 15, 10, 59, 0, tzinfo=NEW_YORK_TZ)
        o59 = pd.Timestamp(dt59).tz_convert("UTC")
        c59 = o59 + pd.Timedelta(minutes=1)

        # FVG confirmed at bar 59
        fvg = _make_fvg(index=58, direction="bullish", confirmed_at=59, structure_leg_id="leg1")
        mss = _make_structure(index=59, direction="bullish", structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=59, open_time=o59, close_time=c59, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_48_missing_bar_close_mapping_fails_closed(self):
        strat = S09ICTSilverBulletStrategy()
        # Strategy starts evaluating at bar 10 (bar 6 is not in _bar_close_times)
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Artificially remove bar 6 from bar close times
        strat._bar_close_times.pop(6, None)

        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        _feed(strat, _make_context(bar_index=7, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_49_fvg_before_sweep_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Sweep at bar 5
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # FVG formed at bar 4 (before sweep)
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=4, direction="bullish", confirmed_at=5, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_50_fvg_index_at_or_after_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # FVG index 6 == MSS index 6
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=6, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_51_fvg_lag_boundary(self):
        strat = S09ICTSilverBulletStrategy(S09Config(fvg_to_mss_max_bars=10))
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Lag = 15 - 5 = 10 (passes)
        _feed(strat, _make_context(bar_index=15, structure_events=[_make_structure(index=15, direction="bullish", structure_leg_id="leg1")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")]))
        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.FVG_READY)

        # New instance: Lag = 16 - 5 = 11 (fails)
        strat2 = S09ICTSilverBulletStrategy(S09Config(fvg_to_mss_max_bars=10))
        strat2.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        _feed(strat2, _make_context(bar_index=16, structure_events=[_make_structure(index=16, direction="bullish", structure_leg_id="leg1")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")]))
        narr2 = list(strat2._narratives.values())[0]
        self.assertEqual(narr2.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_52_null_or_mismatched_structure_leg_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # MSS leg1 vs FVG leg2
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg2")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_53_opposite_structure_in_fvg_mss_interval_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Opposite event at bar 6 between FVG (5) and MSS (7)
        opp = _make_structure(index=6, direction="bearish")
        _feed(strat, _make_context(bar_index=6, structure_events=[opp]))

        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=7, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_54_fvg_filled_prior_to_or_at_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        # FVG filled at bar 6 (prior to MSS at 7)
        fvg = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1", filled_at=6)
        _feed(strat, _make_context(bar_index=7, structure_events=[mss], fvgs=[fvg]))

        narr = list(strat._narratives.values())[0]
        self.assertEqual(narr.stage, S09NarrativeStage.SWEEP_SEEN)

    def test_55_pair_tie_break_permutation_invariant(self):
        # Two FVGs on same leg: fvg1 (index 5) vs fvg2 (index 6)
        # Canonical tie-break prefers closer to MSS (descending index -> fvg2)
        strat1 = S09ICTSilverBulletStrategy()
        strat1.evaluate(_make_context(bar_index=4, sweeps=[_make_sweep(index=4, direction="bullish")]))
        mss = _make_structure(index=7, direction="bullish", structure_leg_id="leg1")
        f1 = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1")
        f2 = _make_fvg(index=6, direction="bullish", confirmed_at=7, structure_leg_id="leg1")

        _feed(strat1, _make_context(bar_index=7, structure_events=[mss], fvgs=[f1, f2]))
        narr1 = list(strat1._narratives.values())[0]

        strat2 = S09ICTSilverBulletStrategy()
        strat2.evaluate(_make_context(bar_index=4, sweeps=[_make_sweep(index=4, direction="bullish")]))
        _feed(strat2, _make_context(bar_index=7, structure_events=[mss], fvgs=[f2, f1]))
        narr2 = list(strat2._narratives.values())[0]

        self.assertEqual(narr1.fvg.index, narr2.fvg.index)
        self.assertEqual(narr1.fvg.index, 6)
'''

with open(target_file, "a", encoding="utf-8") as f:
    f.write(content)

print("Appended Groups C & D (Tests 26-55) successfully.")
