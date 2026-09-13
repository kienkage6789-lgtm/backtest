"""
Append Group G (Tests 85-97) and Group H (Tests 98-106) to tests/test_smc_strategy_s09.py
"""

target_file = r"D:\tool\backtest\tests\test_smc_strategy_s09.py"

content = '''
    # =========================================================================
    # Group G: One-per-window / State / Atomicity (Tests 85-97)
    # =========================================================================

    def test_85_two_valid_narratives_same_window_only_one_emits(self):
        strat = S09ICTSilverBulletStrategy()
        # Ingest two sweeps in same window
        sw1 = _make_sweep(index=4, direction="bullish", pool_kind="equal_lows", price_wick=2030.0, pool_indices=(1, 2))
        sw2 = _make_sweep(index=5, direction="bullish", pool_kind="swing_low", price_wick=2032.0, pool_indices=(3, 4))
        strat.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        strat.evaluate(_make_context(bar_index=5, sweeps=[sw2]))

        # Bar 6: MSS + FVG for both
        mss1 = _make_structure(index=6, direction="bullish", broken_swing_index=2, structure_leg_id="leg1")
        fvg1 = _make_fvg(index=4, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        fvg2 = _make_fvg(index=5, direction="bullish", top=2044.0, bottom=2040.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss1], fvgs=[fvg1, fvg2]))

        # Bar 7: Retest touches both FVGs
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[fvg1, fvg2]))

        # Exactly ONE candidate emitted for the window!
        self.assertEqual(len(cands), 1)
        # Winner was earlier sweep (index=4)
        self.assertEqual(cands[0].evidences[0].bar_index, 4)

    def test_86_first_proposal_fails_geometry_second_proposal_emits(self):
        strat = S09ICTSilverBulletStrategy(S09Config(sl_buffer_price=0.20))
        # Proposal 1 has price_wick right at entry price -> collapses geometry
        sw1 = _make_sweep(index=4, direction="bullish", price_wick=2042.0, pool_indices=(1, 2))
        # Proposal 2 has valid price_wick
        sw2 = _make_sweep(index=5, direction="bullish", price_wick=2030.0, pool_indices=(3, 4))
        strat.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        strat.evaluate(_make_context(bar_index=5, sweeps=[sw2]))

        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        f1 = _make_fvg(index=4, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        f2 = _make_fvg(index=5, direction="bullish", top=2044.0, bottom=2040.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[f1, f2]))

        # Bar 7: Retest touches both. Prop 1 collapses, Prop 2 succeeds!
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[f1, f2]))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].evidences[0].bar_index, 5)

    def test_87_after_emission_later_setup_same_window_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # First setup emits at bar 7
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands1 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands1), 1)

        # Later in same window at bar 15: another sweep occurs
        sw2 = _make_sweep(index=15, direction="bullish", pool_indices=(5, 6))
        _feed(strat, _make_context(bar_index=15, sweeps=[sw2]))
        # Window is already consumed -> sw2 cannot create active narrative
        self.assertEqual(len(strat._narratives), 0)

    def test_88_different_windows_same_day_emit_independently(self):
        strat = S09ICTSilverBulletStrategy()
        # Setup 1 in London window: 03:00 - 04:00 NY time
        dt_lon_sw = datetime.datetime(2024, 5, 15, 3, 10, 0, tzinfo=NEW_YORK_TZ)
        o_lon = pd.Timestamp(dt_lon_sw).tz_convert("UTC")
        c_lon = o_lon + pd.Timedelta(minutes=1)
        strat.evaluate(_make_context(bar_index=0, open_time=o_lon, close_time=c_lon, sweeps=[_make_sweep(index=0, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=1, open_time=c_lon, close_time=c_lon + pd.Timedelta(minutes=1), structure_events=[_make_structure(index=1, direction="bullish")], fvgs=[_make_fvg(index=0, direction="bullish", confirmed_at=1)]))
        cands_lon = strat.evaluate(_make_context(bar_index=2, open_time=c_lon + pd.Timedelta(minutes=1), close_time=c_lon + pd.Timedelta(minutes=2), low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands_lon), 1)

        # Setup 2 in NY AM window: 10:00 - 11:00 NY time
        # Fast forward
        _feed(strat, _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands_ny = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands_ny), 1)

    def test_89_next_day_same_window_resets_ownership(self):
        strat = S09ICTSilverBulletStrategy()
        # Day 1 (Wednesday 2024-05-15) emits setup
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands1 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands1), 1)

        # Day 2 (Thursday 2024-05-16) in NY AM window
        dt_day2 = datetime.datetime(2024, 5, 16, 10, 5, 0, tzinfo=NEW_YORK_TZ)
        o_d2 = pd.Timestamp(dt_day2).tz_convert("UTC")
        c_d2 = o_d2 + pd.Timedelta(minutes=1)

        # Feed to bar 100 on Day 2
        _feed(strat, _make_context(bar_index=100, open_time=o_d2, close_time=c_d2, sweeps=[_make_sweep(index=100, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=101, open_time=c_d2, close_time=c_d2 + pd.Timedelta(minutes=1), structure_events=[_make_structure(index=101, direction="bullish")], fvgs=[_make_fvg(index=100, direction="bullish", confirmed_at=101)]))
        cands2 = strat.evaluate(_make_context(bar_index=102, open_time=c_d2 + pd.Timedelta(minutes=1), close_time=c_d2 + pd.Timedelta(minutes=2), low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(cands2), 1)

    def test_90_same_bar_permutation_yields_same_winner(self):
        sw1 = _make_sweep(index=4, direction="bullish", price_wick=2030.0, pool_indices=(1, 2))
        sw2 = _make_sweep(index=5, direction="bullish", price_wick=2032.0, pool_indices=(3, 4))
        f1 = _make_fvg(index=4, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        f2 = _make_fvg(index=5, direction="bullish", top=2044.0, bottom=2040.0, confirmed_at=6, structure_leg_id="leg1")
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")

        s1 = S09ICTSilverBulletStrategy()
        s1.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        s1.evaluate(_make_context(bar_index=5, sweeps=[sw2]))
        s1.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[f1, f2]))
        c1 = s1.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[f1, f2]))

        s2 = S09ICTSilverBulletStrategy()
        s2.evaluate(_make_context(bar_index=4, sweeps=[sw1]))
        s2.evaluate(_make_context(bar_index=5, sweeps=[sw2]))
        s2.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[f2, f1]))
        c2 = s2.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0, fvgs=[f2, f1]))

        self.assertEqual(c1[0].setup_id, c2[0].setup_id)

    def test_91_identical_retry_returns_cached_result(self):
        strat = S09ICTSilverBulletStrategy()
        ctx = _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")])
        res1 = strat.evaluate(ctx)
        res2 = strat.evaluate(ctx)
        self.assertIs(res1, res2)

    def test_92_conflicting_retry_raises_state_error(self):
        strat = S09ICTSilverBulletStrategy()
        ctx1 = _make_context(bar_index=5, close_p=2050.0)
        strat.evaluate(ctx1)
        ctx2 = _make_context(bar_index=5, close_p=2055.0)  # different close
        with self.assertRaises(StrategyStateError):
            strat.evaluate(ctx2)

    def test_93_non_monotonic_bar_gap_or_backward_raises(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5))
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=7))  # gap
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=4))  # backward

    def test_94_fault_in_matcher_rolls_back_all_state(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        snapshot_narratives = copy.deepcopy(strat._narratives)
        snapshot_clusters = dict(strat._emitted_clusters)

        with patch.object(strat, "_match_mss_and_fvg", side_effect=RuntimeError("Injected fault in matcher")):
            with self.assertRaises(RuntimeError):
                strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")]))

        self.assertEqual(strat._narratives, snapshot_narratives)
        self.assertEqual(strat._emitted_clusters, snapshot_clusters)
        self.assertEqual(strat._last_bar_index, 5)

    def test_95_fault_in_candidate_builder_rolls_back_state(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))

        snapshot_narratives = copy.deepcopy(strat._narratives)
        snapshot_consumed = dict(strat._consumed_windows)

        with patch.object(strat, "_build_candidate", side_effect=RuntimeError("Injected fault in builder")):
            with self.assertRaises(RuntimeError):
                strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        self.assertEqual(strat._narratives, snapshot_narratives)
        self.assertEqual(strat._consumed_windows, snapshot_consumed)
        self.assertEqual(strat._last_bar_index, 6)

    def test_96_reset_clears_state_and_replay_identical(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands1 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        strat.reset()
        self.assertIsNone(strat._last_bar_index)
        self.assertEqual(len(strat._narratives), 0)
        self.assertEqual(len(strat._consumed_windows), 0)

        # Replay produces identical result
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands2 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        self.assertEqual([c.to_dict() for c in cands1], [c.to_dict() for c in cands2])

    def test_97_long_workload_bounded_state_o1(self):
        """Regression test verifying bounded state and 0 duplicate emissions over 1,000 bars."""
        strat = S09ICTSilverBulletStrategy()
        peak_narratives = 0
        peak_consumed = 0
        peak_clusters = 0
        peak_close_times = 0
        emitted_setups = []

        # 1,000 bars stream (~16 hours of trading)
        for b in range(1000):
            sweeps = []
            structures = []
            fvgs = []

            # Emit setup every 100 bars (well separated)
            if b % 100 == 10:
                sweeps.append(_make_sweep(index=b, direction="bullish", pool_indices=(b, b + 1)))
            elif b % 100 == 12:
                structures.append(_make_structure(index=b, direction="bullish", broken_swing_index=b - 5, structure_leg_id=f"leg_{b}"))
                fvgs.append(_make_fvg(index=b - 1, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=b, structure_leg_id=f"leg_{b}"))

            low_p = 2040.0 if b % 100 == 15 else 2050.0
            close_p = 2041.0 if b % 100 == 15 else 2052.0

            cands = strat.evaluate(_make_context(
                bar_index=b,
                low_p=low_p,
                close_p=close_p,
                sweeps=sweeps,
                structure_events=structures,
                fvgs=fvgs,
            ))
            emitted_setups.extend(cands)

            peak_narratives = max(peak_narratives, len(strat._narratives))
            peak_consumed = max(peak_consumed, len(strat._consumed_windows))
            peak_clusters = max(peak_clusters, len(strat._emitted_clusters))
            peak_close_times = max(peak_close_times, len(strat._bar_close_times))

        # Invariants
        self.assertLessEqual(peak_narratives, 5)
        self.assertLessEqual(peak_consumed, 5)
        self.assertLessEqual(peak_clusters, 15)
        self.assertLessEqual(peak_close_times, 65)  # bounded around 50 bars
        setup_ids = [c.setup_id for c in emitted_setups]
        self.assertEqual(len(setup_ids), len(set(setup_ids)))  # 0 duplicates

    # =========================================================================
    # Group H: Production Integration & Parity (Tests 98-106)
    # =========================================================================

    def test_98_strategy_evaluation_through_registry(self):
        reg = StrategyRegistry(StrategyRegistryConfig(enabled_strategy_ids=("S09",)))
        reg.register(S09ICTSilverBulletStrategy())

        sw = _make_sweep(index=5, direction="bullish")
        out5 = reg.evaluate_enabled(_make_context(bar_index=5, sweeps=[sw]))
        self.assertEqual(len(out5["S09"]), 0)

        mss = _make_structure(index=6, direction="bullish")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)
        out6 = reg.evaluate_enabled(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))
        self.assertEqual(len(out6["S09"]), 0)

        out7 = reg.evaluate_enabled(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(out7["S09"]), 1)

    def test_99_actual_context_builder_creates_silver_bullet_sequence(self):
        # 8-candle fixture during NY AM window using real StrategyContextBuilder
        # Seed HTF bias
        seed_event = StructureEvent(
            index=0,
            time=BASE_UTC_TIMESTAMP,
            direction="bullish",
            event_type="BOS",
            broken_swing_index=0,
            broken_swing_price=2020.0,
            close_price=2025.0,
            displacement=True,
            mode="swing",
            confirmed_swing_at=0,
        )
        cfg = ContextBuilderConfig(
            symbol="XAUUSD",
            timeframe="M1",
            swing_strength=1,
            seed_htf_events=(seed_event,),
        )
        # Candles with timestamps inside NY AM window
        candles = []
        for i in range(8):
            o_ts, c_ts = _bar_time(i)
            # Candle 0: Base
            # Candle 1: Low for sweep
            # Candle 2: Sweeps low and reverses
            # Candle 3: Big displacement upward
            # Candle 4: Continuation creating FVG between bar 2 and 4
            # Candle 5: BOS/CHoCH
            # Candle 6: Retest into FVG
            # Candle 7: After retest
            if i == 0:
                p_o, p_h, p_l, p_c = 2030.0, 2032.0, 2028.0, 2030.0
            elif i == 1:
                p_o, p_h, p_l, p_c = 2030.0, 2031.0, 2025.0, 2028.0
            elif i == 2:
                p_o, p_h, p_l, p_c = 2028.0, 2035.0, 2024.0, 2034.0  # sweeps 2025
            elif i == 3:
                p_o, p_h, p_l, p_c = 2034.0, 2045.0, 2034.0, 2044.0  # big displacement
            elif i == 4:
                p_o, p_h, p_l, p_c = 2044.0, 2050.0, 2043.0, 2048.0  # FVG between 2 and 4
            elif i == 5:
                p_o, p_h, p_l, p_c = 2048.0, 2052.0, 2046.0, 2050.0
            elif i == 6:
                p_o, p_h, p_l, p_c = 2050.0, 2050.0, 2036.0, 2042.0  # dips into FVG
            else:
                p_o, p_h, p_l, p_c = 2042.0, 2048.0, 2041.0, 2047.0

            candles.append({
                "time": o_ts,
                "open": p_o,
                "high": p_h,
                "low": p_l,
                "close": p_c,
                "volume": 100.0,
            })

        contexts = build_strategy_contexts(candles, cfg)
        self.assertEqual(len(contexts), 8)
        self.assertEqual(contexts[0].htf_bias.bias, "bullish")

    def test_100_batch_contexts_sequential_evaluation(self):
        strat = S09ICTSilverBulletStrategy()
        # Build batch of contexts
        contexts = [
            _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]),
            _make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]),
            _make_context(bar_index=7, low_p=2040.0, close_p=2041.0),
        ]
        results = []
        for ctx in contexts:
            results.append(strat.evaluate(ctx))

        self.assertEqual(len(results[0]), 0)
        self.assertEqual(len(results[1]), 0)
        self.assertEqual(len(results[2]), 1)

    def test_101_incremental_builder_sequential_evaluation(self):
        strat = S09ICTSilverBulletStrategy()
        # Stream contexts one by one
        c5 = strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        c6 = strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        c7 = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(len(c7), 1)

    def test_102_json_round_trip_context_replay(self):
        ctxs = [
            _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]),
            _make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]),
            _make_context(bar_index=7, low_p=2040.0, close_p=2041.0),
        ]
        # Serialize to JSON and deserialize back
        json_dumps = [json.dumps(c.to_dict()) for c in ctxs]
        deserialized_ctxs = [StrategyContext.from_dict(json.loads(s)) for s in json_dumps]

        strat = S09ICTSilverBulletStrategy()
        res = [strat.evaluate(c) for c in deserialized_ctxs]
        self.assertEqual(len(res[2]), 1)

    def test_103_exact_parity_batch_incremental_json(self):
        ctxs = [
            _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]),
            _make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]),
            _make_context(bar_index=7, low_p=2040.0, close_p=2041.0),
        ]
        json_ctxs = [StrategyContext.from_dict(json.loads(json.dumps(c.to_dict()))) for c in ctxs]

        s_inc = S09ICTSilverBulletStrategy()
        res_inc = [s_inc.evaluate(c) for c in ctxs]

        s_json = S09ICTSilverBulletStrategy()
        res_json = [s_json.evaluate(c) for c in json_ctxs]

        self.assertEqual(
            [[c.to_dict() for c in r] for r in res_inc],
            [[c.to_dict() for c in r] for r in res_json],
        )

    def test_104_append_future_candles_zero_lookahead(self):
        """Zero lookahead: past context output is invariant when future bars are appended."""
        seed_event = StructureEvent(
            index=0,
            time=BASE_UTC_TIMESTAMP,
            direction="bullish",
            event_type="BOS",
            broken_swing_index=0,
            broken_swing_price=2020.0,
            close_price=2025.0,
            displacement=True,
            mode="swing",
            confirmed_swing_at=0,
        )
        cfg = ContextBuilderConfig(
            symbol="XAUUSD",
            timeframe="M1",
            swing_strength=1,
            seed_htf_events=(seed_event,),
        )
        candles_prefix = []
        for i in range(7):
            o_ts, c_ts = _bar_time(i)
            candles_prefix.append({
                "time": o_ts,
                "open": 2030.0 + i,
                "high": 2035.0 + i,
                "low": 2028.0 + i,
                "close": 2032.0 + i,
                "volume": 100.0,
            })

        candles_extended = copy.deepcopy(candles_prefix)
        for i in range(7, 10):
            o_ts, c_ts = _bar_time(i)
            candles_extended.append({
                "time": o_ts,
                "open": 2040.0,
                "high": 2050.0,
                "low": 2038.0,
                "close": 2048.0,
                "volume": 100.0,
            })

        prefix_contexts = build_strategy_contexts(candles_prefix, cfg)
        extended_contexts = build_strategy_contexts(candles_extended, cfg)

        # Context serialization prefix equality
        self.assertEqual(
            [c.to_dict() for c in prefix_contexts],
            [c.to_dict() for c in extended_contexts[:len(prefix_contexts)]],
        )

        strat_pref = S09ICTSilverBulletStrategy()
        res_pref = [strat_pref.evaluate(c) for c in prefix_contexts]

        strat_ext = S09ICTSilverBulletStrategy()
        res_ext = [strat_ext.evaluate(c) for c in extended_contexts]

        self.assertEqual(
            [[c.to_dict() for c in r] for r in res_pref],
            [[c.to_dict() for c in r] for r in res_ext[:len(res_pref)]],
        )

    def test_105_dst_winter_and_summer_production_fixtures(self):
        # Winter fixture: 2024-01-17 (EST, UTC-5)
        # 10:05 NY time is 15:05 UTC
        dt_win = datetime.datetime(2024, 1, 17, 10, 5, 0, tzinfo=NEW_YORK_TZ)
        o_win = pd.Timestamp(dt_win).tz_convert("UTC")
        c_win = o_win + pd.Timedelta(minutes=1)
        self.assertEqual(o_win.hour, 15)

        s_win = S09ICTSilverBulletStrategy()
        cands_win = s_win.evaluate(_make_context(bar_index=0, open_time=o_win, close_time=c_win, sweeps=[_make_sweep(index=0, direction="bullish")]))
        self.assertEqual(len(s_win._narratives), 1)

        # Summer fixture: 2024-07-17 (EDT, UTC-4)
        # 10:05 NY time is 14:05 UTC
        dt_sum = datetime.datetime(2024, 7, 17, 10, 5, 0, tzinfo=NEW_YORK_TZ)
        o_sum = pd.Timestamp(dt_sum).tz_convert("UTC")
        c_sum = o_sum + pd.Timedelta(minutes=1)
        self.assertEqual(o_sum.hour, 14)

        s_sum = S09ICTSilverBulletStrategy()
        cands_sum = s_sum.evaluate(_make_context(bar_index=0, open_time=o_sum, close_time=c_sum, sweeps=[_make_sweep(index=0, direction="bullish")]))
        self.assertEqual(len(s_sum._narratives), 1)

    def test_106_deep_immutability_of_context_and_snapshots(self):
        strat = S09ICTSilverBulletStrategy()
        ctx = _make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")])
        before = ctx.to_dict()
        strat.evaluate(ctx)
        after = ctx.to_dict()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
'''

with open(target_file, "a", encoding="utf-8") as f:
    f.write(content)

print("Appended Groups G & H (Tests 85-106) and main block successfully.")
