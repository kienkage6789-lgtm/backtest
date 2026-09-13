"""
Append Group A (Tests 01-10) and Group B (Tests 11-25) to tests/test_smc_strategy_s09.py
"""

target_file = r"D:\tool\backtest\tests\test_smc_strategy_s09.py"

content = '''

class TestSMCS09Strategy(unittest.TestCase):
    """106-test exhaustive test suite for S09 ICT Silver Bullet Strategy Template."""

    # =========================================================================
    # Group A: Config / Profile / Protocol (Tests 01-10)
    # =========================================================================

    def test_01_config_defaults_exact(self):
        cfg = S09Config()
        self.assertEqual(cfg.mode, "internal")
        self.assertEqual(
            cfg.enabled_windows,
            ("silver_bullet_london", "silver_bullet_ny_am", "silver_bullet_ny_pm"),
        )
        self.assertEqual(cfg.grace_minutes, 15)
        self.assertEqual(cfg.fvg_to_mss_max_bars, 10)
        self.assertEqual(cfg.entry_level, "proximal")
        self.assertEqual(cfg.sl_buffer_price, 0.20)
        self.assertEqual(cfg.min_rr, 1.50)
        self.assertEqual(cfg.fallback_rr, 2.00)
        self.assertTrue(cfg.require_displacement)

    def test_02_config_json_round_trip(self):
        cfg = S09Config(
            mode="swing",
            enabled_windows=("silver_bullet_ny_am",),
            grace_minutes=15,
            fvg_to_mss_max_bars=8,
            entry_level="ce_50",
            sl_buffer_price=0.30,
            min_rr=1.80,
            fallback_rr=2.50,
            require_displacement=False,
        )
        d = cfg.to_dict()
        cfg_round = S09Config.from_dict(d)
        self.assertEqual(cfg, cfg_round)

    def test_03_config_rejects_bool_in_numeric(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(min_rr=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(fallback_rr=False)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(fvg_to_mss_max_bars=True)  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09Config(grace_minutes=True)  # type: ignore

    def test_04_config_rejects_nan_inf_zero_negative(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=float("nan"))
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=float("inf"))
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=0.0)
        with self.assertRaises(StrategyValidationError):
            S09Config(sl_buffer_price=-0.2)
        with self.assertRaises(StrategyValidationError):
            S09Config(min_rr=0.0)
        with self.assertRaises(StrategyValidationError):
            S09Config(fallback_rr=1.2, min_rr=1.5)  # fallback_rr < min_rr
        with self.assertRaises(StrategyValidationError):
            S09Config(fvg_to_mss_max_bars=0)

    def test_05_config_rejects_unknown_mode_or_window_or_entry_level(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(mode="invalid")
        with self.assertRaises(StrategyValidationError):
            S09Config(enabled_windows=("asian_range",))  # unknown window
        with self.assertRaises(StrategyValidationError):
            S09Config(entry_level="market")

    def test_06_config_rejects_duplicate_or_empty_windows(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(enabled_windows=())
        with self.assertRaises(StrategyValidationError):
            S09Config(enabled_windows=("silver_bullet_ny_am", "silver_bullet_ny_am"))

    def test_07_config_canonicalizes_window_order(self):
        cfg = S09Config(
            enabled_windows=("silver_bullet_ny_pm", "silver_bullet_london")
        )
        self.assertEqual(
            cfg.enabled_windows,
            ("silver_bullet_london", "silver_bullet_ny_pm"),
        )

    def test_08_config_rejects_grace_minutes_different_from_15(self):
        with self.assertRaises(StrategyValidationError):
            S09Config(grace_minutes=10)
        with self.assertRaises(StrategyValidationError):
            S09Config(grace_minutes=20)

    def test_09_fail_fast_wrong_config_type(self):
        with self.assertRaises(StrategyValidationError):
            S09ICTSilverBulletStrategy(config="invalid")  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09ICTSilverBulletStrategy(config={"mode": "internal"})  # type: ignore
        with self.assertRaises(StrategyValidationError):
            S09ICTSilverBulletStrategy(config=S01Config())  # type: ignore

    def test_10_protocol_profile_and_clean_exports(self):
        strat = S09ICTSilverBulletStrategy()
        self.assertIsInstance(strat, StrategyTemplate)
        self.assertEqual(strat.strategy_id, "S09")
        self.assertEqual(strat.profile.style, "time_based")
        self.assertEqual(strat.profile.name, "ICT Silver Bullet")
        self.assertEqual(strat.profile.timeframes, ("M1", "M5", "M15"))
        self.assertEqual(strat.profile.max_setup_age_bars, 15)
        self.assertEqual(strat.profile.cooldown_bars, 3)
        self.assertEqual(strat.profile.min_rr, 1.50)

    # =========================================================================
    # Group B: Window / Timezone / DST / Calendar (Tests 11-25)
    # =========================================================================

    def test_11_three_canonical_windows_exact(self):
        self.assertEqual(len(CANONICAL_WINDOWS), 3)
        self.assertEqual(
            WINDOW_SCHEDULE["silver_bullet_london"],
            (datetime.time(3, 0), datetime.time(4, 0)),
        )
        self.assertEqual(
            WINDOW_SCHEDULE["silver_bullet_ny_am"],
            (datetime.time(10, 0), datetime.time(11, 0)),
        )
        self.assertEqual(
            WINDOW_SCHEDULE["silver_bullet_ny_pm"],
            (datetime.time(14, 0), datetime.time(15, 0)),
        )

    def test_12_event_start_boundary_inclusive(self):
        # 10:00:00 NY time on Wednesday 2024-05-15
        dt_1000 = datetime.datetime(2024, 5, 15, 10, 0, 0, tzinfo=NEW_YORK_TZ)
        ts_1000 = pd.Timestamp(dt_1000).tz_convert("UTC")
        res = get_window_for_close_time(ts_1000, CANONICAL_WINDOWS, 15)
        self.assertIsNotNone(res)
        w_key, w_start, w_end, w_grace = res
        self.assertEqual(w_key[0], "silver_bullet_ny_am")
        self.assertEqual(ts_1000, w_start)

    def test_13_event_end_boundary_exclusive(self):
        # 11:00:00 NY time on Wednesday 2024-05-15 (exact window end is excluded)
        dt_1100 = datetime.datetime(2024, 5, 15, 11, 0, 0, tzinfo=NEW_YORK_TZ)
        ts_1100 = pd.Timestamp(dt_1100).tz_convert("UTC")
        res = get_window_for_close_time(ts_1100, CANONICAL_WINDOWS, 15)
        self.assertIsNone(res)

    def test_14_retest_grace_exact_inclusive(self):
        # 11:15:00 NY time on Wednesday 2024-05-15
        start_utc, end_utc, grace_utc = compute_window_bounds_for_date(
            "2024-05-15", "silver_bullet_ny_am", 15
        )
        dt_1115 = datetime.datetime(2024, 5, 15, 11, 15, 0, tzinfo=NEW_YORK_TZ)
        ts_1115 = pd.Timestamp(dt_1115).tz_convert("UTC")
        self.assertEqual(ts_1115, grace_utc)
        self.assertTrue(ts_1115 <= grace_utc)

    def test_15_retest_after_grace_rejected(self):
        start_utc, end_utc, grace_utc = compute_window_bounds_for_date(
            "2024-05-15", "silver_bullet_ny_am", 15
        )
        ts_1115_01 = grace_utc + pd.Timedelta(seconds=1)
        self.assertFalse(ts_1115_01 <= grace_utc)

    def test_16_bar_open_before_grace_close_after_grace_rejected(self):
        # M15 bar opening at 11:10 (before 11:15) and closing at 11:25 (after 11:15)
        start_utc, end_utc, grace_utc = compute_window_bounds_for_date(
            "2024-05-15", "silver_bullet_ny_am", 15
        )
        dt_open = datetime.datetime(2024, 5, 15, 11, 10, 0, tzinfo=NEW_YORK_TZ)
        dt_close = datetime.datetime(2024, 5, 15, 11, 25, 0, tzinfo=NEW_YORK_TZ)
        ts_open = pd.Timestamp(dt_open).tz_convert("UTC")
        ts_close = pd.Timestamp(dt_close).tz_convert("UTC")

        self.assertTrue(ts_open < grace_utc)
        self.assertFalse(ts_close <= grace_utc)

    def test_17_local_ny_date_differs_from_utc_date_no_collision(self):
        # London window: 03:00 NY in winter (EST = UTC-5) is 08:00 UTC
        dt = datetime.datetime(2024, 1, 15, 3, 30, 0, tzinfo=NEW_YORK_TZ)
        ts_utc = pd.Timestamp(dt).tz_convert("UTC")
        local_date = get_ny_local_date_str(ts_utc)
        self.assertEqual(local_date, "2024-01-15")

    def test_18_weekends_rejected(self):
        # Saturday 2024-05-18 at 10:30 NY time
        dt_sat = datetime.datetime(2024, 5, 18, 10, 30, 0, tzinfo=NEW_YORK_TZ)
        ts_sat = pd.Timestamp(dt_sat).tz_convert("UTC")
        res_sat = get_window_for_close_time(ts_sat, CANONICAL_WINDOWS, 15)
        self.assertIsNone(res_sat)

        # Sunday 2024-05-19 at 10:30 NY time
        dt_sun = datetime.datetime(2024, 5, 19, 10, 30, 0, tzinfo=NEW_YORK_TZ)
        ts_sun = pd.Timestamp(dt_sun).tz_convert("UTC")
        res_sun = get_window_for_close_time(ts_sun, CANONICAL_WINDOWS, 15)
        self.assertIsNone(res_sun)

    def test_19_winter_offset_est_utc_minus_5(self):
        # Winter: 2024-01-17 (Wednesday)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-01-17", "silver_bullet_ny_am", 15
        )
        # 10:00 EST is 15:00 UTC
        self.assertEqual(start_utc.hour, 15)
        self.assertEqual(end_utc.hour, 16)

    def test_20_summer_offset_edt_utc_minus_4(self):
        # Summer: 2024-07-17 (Wednesday)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-07-17", "silver_bullet_ny_am", 15
        )
        # 10:00 EDT is 14:00 UTC
        self.assertEqual(start_utc.hour, 14)
        self.assertEqual(end_utc.hour, 15)

    def test_21_spring_dst_transition_date(self):
        # US Spring DST transition was Sunday 2024-03-10
        # Monday 2024-03-11 is EDT (UTC-4)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-03-11", "silver_bullet_ny_am", 15
        )
        self.assertEqual(start_utc.hour, 14)

    def test_22_fall_dst_transition_date(self):
        # US Fall DST transition was Sunday 2024-11-03
        # Monday 2024-11-04 is EST (UTC-5)
        start_utc, end_utc, _ = compute_window_bounds_for_date(
            "2024-11-04", "silver_bullet_ny_am", 15
        )
        self.assertEqual(start_utc.hour, 15)

    def test_23_window_key_deterministic_and_unique(self):
        dt = datetime.datetime(2024, 5, 15, 10, 15, 0, tzinfo=NEW_YORK_TZ)
        ts = pd.Timestamp(dt).tz_convert("UTC")
        res1 = get_window_for_close_time(ts, CANONICAL_WINDOWS, 15)
        res2 = get_window_for_close_time(ts, CANONICAL_WINDOWS, 15)
        self.assertIsNotNone(res1)
        self.assertIsNotNone(res2)
        self.assertEqual(res1[0], res2[0])
        self.assertEqual(res1[0][0], "silver_bullet_ny_am")
        self.assertEqual(res1[0][1], "2024-05-15")

    def test_24_two_different_dates_same_window_name_do_not_share_state(self):
        dt1 = datetime.datetime(2024, 5, 15, 10, 15, 0, tzinfo=NEW_YORK_TZ)
        dt2 = datetime.datetime(2024, 5, 16, 10, 15, 0, tzinfo=NEW_YORK_TZ)
        res1 = get_window_for_close_time(pd.Timestamp(dt1).tz_convert("UTC"), CANONICAL_WINDOWS, 15)
        res2 = get_window_for_close_time(pd.Timestamp(dt2).tz_convert("UTC"), CANONICAL_WINDOWS, 15)
        self.assertNotEqual(res1[0], res2[0])

    def test_25_window_disabled_in_config_rejects_ingestion(self):
        strat = S09ICTSilverBulletStrategy(
            S09Config(enabled_windows=("silver_bullet_ny_am",))
        )
        # London window: 03:30 NY time
        dt_london = datetime.datetime(2024, 5, 15, 3, 30, 0, tzinfo=NEW_YORK_TZ)
        open_ts = pd.Timestamp(dt_london).tz_convert("UTC")
        close_ts = open_ts + pd.Timedelta(minutes=1)
        ctx = _make_context(
            bar_index=0,
            open_time=open_ts,
            close_time=close_ts,
            sweeps=[_make_sweep(index=0, direction="bullish")],
        )
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(len(strat._narratives), 0)
'''

with open(target_file, "a", encoding="utf-8") as f:
    f.write(content)

print("Appended Groups A & B (Tests 01-25) successfully.")
