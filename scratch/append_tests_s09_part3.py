"""
Append Group E (Tests 56-70) and Group F (Tests 71-84) to tests/test_smc_strategy_s09.py
"""

target_file = r"D:\tool\backtest\tests\test_smc_strategy_s09.py"

content = '''
    # =========================================================================
    # Group E: Retest & Invalidation (Tests 56-70)
    # =========================================================================

    def test_56_retest_after_mss_emits_candidate(self):
        strat = S09ICTSilverBulletStrategy()
        # Bar 5: Sweep
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        # Bar 6: MSS + FVG [2038, 2042]
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Bar 7: Retest touches FVG (low=2040 in [2038, 2042], close=2041 >= 2038)
        pool = _make_pool(confirmed_at=2, price=2060.0)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg], pools=[pool]))

        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.direction, "BUY")
        self.assertEqual(c.entry_price, 2042.0)
        self.assertEqual(c.stop_loss, 2034.8)  # 2035 - 0.20
        self.assertEqual(c.take_profit, 2060.0)

    def test_57_retest_at_same_bar_as_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))

        # Bar 6: MSS + FVG + price wicks into FVG on same bar
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        cands = strat.evaluate(_make_context(bar_index=6, low_p=2040.0, close_p=2041.0, structure_events=[mss], fvgs=[fvg]))

        # Retest on same bar as MSS cannot emit
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat._narratives[list(strat._narratives.keys())[0]].stage, S09NarrativeStage.FVG_READY)

    def test_58_wick_overlap_and_close_respect_buy(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Retest wicks below bottom (low=2037 < 2038) but closes above (close=2039 >= 2038)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2037.0, close_p=2039.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 1)

    def test_59_wick_overlap_and_close_respect_sell(self):
        strat = S09ICTSilverBulletStrategy()
        # Bearish setup
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bearish", pool_kind="equal_highs", price_wick=2065.0)], bias="bearish"))
        mss = _make_structure(index=6, direction="bearish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bearish", top=2062.0, bottom=2058.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg], bias="bearish"))

        # Retest wicks above top (high=2063 > 2062) but closes below (close=2060 <= 2062)
        cands = strat.evaluate(_make_context(bar_index=7, high_p=2063.0, close_p=2060.0, fvgs=[fvg], bias="bearish"))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].direction, "SELL")
        self.assertEqual(cands[0].entry_price, 2058.0)  # proximal for SELL is bottom
        self.assertEqual(cands[0].stop_loss, 2065.2)  # 2065 + 0.20

    def test_60_fvg_filled_at_none_passes_on_touch(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1", filled_at=None)
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 1)

    def test_61_fvg_filled_at_n_passes_if_close_respects(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # FVG fully filled at current bar 7 (low <= 2038, close >= 2038)
        fvg_filled_now = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1", filled=True, filled_at=7)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2037.0, close_p=2039.0, fvgs=[fvg_filled_now]))
        self.assertEqual(len(cands), 1)

    def test_62_fvg_filled_at_less_than_n_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Prior fill at bar 6
        fvg_prior_filled = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1", filled=True, filled_at=6)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[fvg_prior_filled]))
        self.assertEqual(len(cands), 0)

    def test_63_fvg_filled_at_greater_than_n_future_leak_raises(self):
        strat = S09ICTSilverBulletStrategy()
        fvg_future = _make_fvg(index=5, direction="bullish", confirmed_at=6, structure_leg_id="leg1", filled=True, filled_at=10)
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=7, fvgs=[fvg_future]))

    def test_64_close_through_same_bar_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Close violates bottom boundary: close=2036 < 2038
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2035.0, close_p=2036.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)  # invalidated and pruned

    def test_65_sweep_extreme_violation_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        # Sweep price_wick = 2035.0
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Price closes below sweep extreme: close=2034 < 2035
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2033.0, close_p=2034.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_66_opposite_structure_after_mss_rejected(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Opposite structure event at bar 7
        opp_mss = _make_structure(index=7, direction="bearish")
        cands = strat.evaluate(_make_context(bar_index=7, structure_events=[opp_mss], fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_67_opposite_structure_at_signal_bar_rejected_before_emission(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # At bar 8: retest touch happens, but also an opposite structure event forms on same bar!
        opp_mss = _make_structure(index=8, direction="bearish")
        _feed(strat, _make_context(bar_index=7, fvgs=[fvg]))
        cands = strat.evaluate(_make_context(bar_index=8, low_p=2040.0, close_p=2041.0, structure_events=[opp_mss], fvgs=[fvg]))
        self.assertEqual(len(cands), 0)

    def test_68_retest_within_grace_passes(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Advance to bar 65 (11:05 NY time, within 15m grace ending at 11:15)
        _feed(strat, _make_context(bar_index=64, fvgs=[fvg]))
        cands = strat.evaluate(_make_context(bar_index=65, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 1)

    def test_69_retest_after_grace_rejected_and_pruned(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Advance to bar 76 (11:16 NY time, after 11:15 grace expiry)
        _feed(strat, _make_context(bar_index=75, fvgs=[fvg]))
        cands = strat.evaluate(_make_context(bar_index=76, low_p=2040.0, close_p=2041.0, fvgs=[fvg]))
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)

    def test_70_missing_fvg_snapshot_handled_safely(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        mss = _make_structure(index=6, direction="bullish", structure_leg_id="leg1")
        fvg = _make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6, structure_leg_id="leg1")
        strat.evaluate(_make_context(bar_index=6, structure_events=[mss], fvgs=[fvg]))

        # Bar 7: active_fvgs is empty
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, fvgs=[]))
        self.assertEqual(len(cands), 1)  # Internal FVG in narrative still triggers

    # =========================================================================
    # Group F: Pricing, Evidence & IDs (Tests 71-84)
    # =========================================================================

    def test_71_proximal_entry_pricing_buy_and_sell(self):
        strat = S09ICTSilverBulletStrategy(S09Config(entry_level="proximal"))
        # BUY: proximal is top
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        cands_buy = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(cands_buy[0].entry_price, 2042.0)

        # SELL: proximal is bottom
        strat2 = S09ICTSilverBulletStrategy(S09Config(entry_level="proximal"))
        strat2.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bearish", pool_kind="equal_highs")], bias="bearish"))
        strat2.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bearish")], fvgs=[_make_fvg(index=5, direction="bearish", top=2062.0, bottom=2058.0, confirmed_at=6)], bias="bearish"))
        cands_sell = strat2.evaluate(_make_context(bar_index=7, high_p=2060.0, close_p=2059.0, bias="bearish"))
        self.assertEqual(cands_sell[0].entry_price, 2058.0)

    def test_72_ce50_entry_pricing_buy_and_sell(self):
        strat = S09ICTSilverBulletStrategy(S09Config(entry_level="ce_50"))
        # BUY: CE50 is (2042 + 2038) / 2 = 2040.0
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        cands_buy = strat.evaluate(_make_context(bar_index=7, low_p=2039.0, close_p=2041.0))
        self.assertEqual(cands_buy[0].entry_price, 2040.0)

        # SELL: CE50 is (2062 + 2058) / 2 = 2060.0
        strat2 = S09ICTSilverBulletStrategy(S09Config(entry_level="ce_50"))
        strat2.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bearish", pool_kind="equal_highs")], bias="bearish"))
        strat2.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bearish")], fvgs=[_make_fvg(index=5, direction="bearish", top=2062.0, bottom=2058.0, confirmed_at=6)], bias="bearish"))
        cands_sell = strat2.evaluate(_make_context(bar_index=7, high_p=2061.0, close_p=2059.0, bias="bearish"))
        self.assertEqual(cands_sell[0].entry_price, 2060.0)

    def test_73_sweep_extreme_sl_pricing(self):
        strat = S09ICTSilverBulletStrategy(S09Config(sl_buffer_price=0.20))
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.50)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(cands[0].stop_loss, 2035.30)  # 2035.50 - 0.20

    def test_74_nearest_opposing_pool_independent_of_input_order(self):
        pool1 = _make_pool(confirmed_at=1, price=2055.0, indices=(1, 2))
        pool2 = _make_pool(confirmed_at=2, price=2070.0, indices=(3, 4))

        # Order [pool1, pool2]
        s1 = S09ICTSilverBulletStrategy()
        s1.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        s1.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        c1 = s1.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool1, pool2]))

        # Order [pool2, pool1]
        s2 = S09ICTSilverBulletStrategy()
        s2.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        s2.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        c2 = s2.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool2, pool1]))

        self.assertEqual(c1[0].take_profit, c2[0].take_profit)
        self.assertEqual(c1[0].take_profit, 2055.0)

    def test_75_ineligible_pools_ignored(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))

        # Ineligible pools: swept=True, valid=False, price below entry
        p_swept = _make_pool(confirmed_at=1, price=2055.0, swept=True)
        p_invalid = _make_pool(confirmed_at=2, price=2056.0, valid=False)
        p_below = _make_pool(confirmed_at=3, price=2030.0)  # below entry 2042
        p_valid = _make_pool(confirmed_at=4, price=2065.0)

        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[p_swept, p_invalid, p_below, p_valid]))
        self.assertEqual(cands[0].take_profit, 2065.0)
        self.assertEqual(cands[0].target_type, "opposing_pool")

    def test_76_nearest_pool_rr_below_min_falls_back_to_fixed_rr_no_hop(self):
        strat = S09ICTSilverBulletStrategy(S09Config(min_rr=1.5, fallback_rr=2.0))
        # Entry=2042.0, SL=2034.8 -> Risk = 7.2
        # Min reward needed = 7.2 * 1.5 = 10.8 -> TP >= 2052.8
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))

        # Nearest pool is at 2045.0 (Reward = 3.0, RR = 3.0/7.2 = 0.42 < 1.5)
        # Far pool at 2070.0 has good RR, but strategy must NOT hop to far pool!
        p_close = _make_pool(confirmed_at=1, price=2045.0)
        p_far = _make_pool(confirmed_at=2, price=2070.0)

        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[p_close, p_far]))
        self.assertEqual(cands[0].target_type, "fixed_rr")
        # Fixed 2.0R fallback: 2042 + 2.0 * 7.2 = 2056.4
        self.assertEqual(cands[0].take_profit, 2056.4)

    def test_77_post_rounding_geometry_collapse_fails_closed(self):
        strat = S09ICTSilverBulletStrategy(S09Config(sl_buffer_price=0.0001))
        # Very tiny difference where entry == sl after 3-decimal rounding
        # Should fail closed and not emit
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2042.0001)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0001, bottom=2042.0, confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2042.0, close_p=2042.0))
        self.assertEqual(len(cands), 0)

    def test_78_planned_rr_recalculated_from_rounded_levels(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish", price_wick=2035.0)]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", top=2042.0, bottom=2038.0, confirmed_at=6)]))
        pool = _make_pool(confirmed_at=1, price=2056.4)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool]))
        # Entry=2042.0, SL=2034.8, TP=2056.4 -> Risk=7.2, Reward=14.4 -> RR = 2.00
        self.assertEqual(cands[0].planned_rr, 2.00)

    def test_79_evidence_canonical_order_and_timestamps(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        pool = _make_pool(confirmed_at=2, price=2060.0)
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0, pools=[pool]))

        evs = cands[0].evidences
        self.assertEqual(len(evs), 4)
        self.assertEqual(evs[0].kind, "liquidity_sweep")
        self.assertEqual(evs[1].kind, "structure_event")
        self.assertEqual(evs[2].kind, "fair_value_gap")
        self.assertEqual(evs[3].kind, "liquidity_pool")

    def test_80_s09_cluster_id_matches_s01_for_same_opportunity(self):
        # When S01 and S09 evaluate the exact same sweep, MSS (broken_swing=4) and FVG (index=5)
        # Their evidence_cluster_id must match 100%!
        direction = "BUY"
        mode = "internal"
        broken_swing = 4
        fvg_index = 5
        leg_comp = f"leg-{mode}-{direction}-{broken_swing}"
        zone_comp = f"fvg-{mode}-{direction}-{fvg_index}"
        expected_cluster_id = make_cluster_id(direction, leg_comp, zone_comp)

        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish", broken_swing_index=broken_swing)], fvgs=[_make_fvg(index=fvg_index, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        self.assertEqual(cands[0].evidence_cluster_id, expected_cluster_id)

    def test_81_setup_id_distinguishes_s09_from_s01(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertTrue(cands[0].setup_id.startswith("S09_BUY_7_"))

    def test_82_ids_injective_and_float_free(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        c = cands[0]
        # Cluster ID must not contain float '.'
        self.assertNotIn(".", c.evidence_cluster_id)
        for ev in c.evidences:
            # Evidence IDs must not contain float string representation
            self.assertNotIn(".", ev.evidence_id)

    def test_83_metadata_rich_and_json_safe(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))

        d = cands[0].to_dict()
        # Verify JSON serializability
        json_str = json.dumps(d)
        self.assertIsInstance(json_str, str)
        self.assertEqual(d["meta"]["window_name"], "silver_bullet_ny_am")
        self.assertEqual(d["meta"]["session_time_basis"], "bar_close_time")

    def test_84_candidate_expiry_bar_equals_signal_bar(self):
        strat = S09ICTSilverBulletStrategy()
        strat.evaluate(_make_context(bar_index=5, sweeps=[_make_sweep(index=5, direction="bullish")]))
        strat.evaluate(_make_context(bar_index=6, structure_events=[_make_structure(index=6, direction="bullish")], fvgs=[_make_fvg(index=5, direction="bullish", confirmed_at=6)]))
        cands = strat.evaluate(_make_context(bar_index=7, low_p=2040.0, close_p=2041.0))
        self.assertEqual(cands[0].expiry_bar, 7)
        self.assertEqual(cands[0].bar_index, 7)
'''

with open(target_file, "a", encoding="utf-8") as f:
    f.write(content)

print("Appended Groups E & F (Tests 56-84) successfully.")
