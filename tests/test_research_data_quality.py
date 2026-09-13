"""
tests/test_research_data_quality.py
-----------------------------------
Unit and parity tests for Milestone T54.0 Research Protocol & Data Quality Gate.
Ensures that research artifacts exist, have valid schemas, and pass dual-machine parity.
"""

import json
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent
RESEARCH_DIR = ROOT_DIR / "research"
PROTOCOL_FILE = RESEARCH_DIR / "protocol_v1.json"
MANIFEST_FILE = RESEARCH_DIR / "dataset_manifest.json"
QUALITY_JSON_FILE = RESEARCH_DIR / "data_quality_report.json"
QC_REPORT_FILE = RESEARCH_DIR / "qc_verification_report.json"
QUALITY_MD_FILE = RESEARCH_DIR / "data_quality_report.md"


class TestResearchDataQualityGate(unittest.TestCase):
    def test_protocol_v1_schema_and_locks(self):
        """Kiểm tra protocol_v1.json đã khóa đủ 14 trường tham số bắt buộc."""
        self.assertTrue(PROTOCOL_FILE.exists(), "research/protocol_v1.json không tồn tại")
        with open(PROTOCOL_FILE, "r", encoding="utf-8") as f:
            proto = json.load(f)

        self.assertEqual(proto["symbol"], "XAUUSD")
        self.assertEqual(proto["timeframes"]["execution"], "M15")
        self.assertEqual(proto["timeframes"]["htf_bias"], "H1")
        self.assertEqual(proto["data_range"]["start"], "2022-01-01 00:00:00+00:00")
        self.assertEqual(proto["data_range"]["end"], "2026-08-31 23:59:59+00:00")
        self.assertEqual(proto["account"]["initial_capital"], 10000.0)
        self.assertEqual(proto["account"]["lot_size"], 0.01)
        self.assertEqual(proto["cost_model"]["standard"]["spread_points"], 20.0)
        self.assertEqual(proto["cost_model"]["standard"]["commission_per_lot"], 5.0)
        self.assertEqual(proto["strategy_parameters"]["min_rr"], 1.5)
        self.assertEqual(proto["strategy_parameters"]["cooldown_bars"], 3)
        self.assertIn("smc_wave1", proto["strategies_in_scope"])
        self.assertIn("smc_s01", proto["strategies_in_scope"])
        self.assertIn("smc_s05", proto["strategies_in_scope"])
        self.assertIn("smc_s09", proto["strategies_in_scope"])
        self.assertIn("smc_confluence", proto["strategies_in_scope"])
        self.assertIn("in_sample", proto["dataset_split"])
        self.assertIn("validation", proto["dataset_split"])
        self.assertIn("out_of_sample", proto["dataset_split"])

    def test_dataset_manifest_integrity(self):
        """Kiểm tra dataset_manifest.json của Máy 1."""
        self.assertTrue(MANIFEST_FILE.exists(), "research/dataset_manifest.json không tồn tại")
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest["generated_by"], "machine_1_data_runner")
        self.assertEqual(manifest["timeframes"]["M1"]["count"], 1646963)
        self.assertEqual(manifest["timeframes"]["M15"]["count"], 110130)
        self.assertEqual(manifest["volume_preservation"]["status"], "PASS")

        # Kiểm tra checksum sha256 tồn tại và độ dài 64 hex
        sha_m1 = manifest["timeframes"]["M1"]["sha256"]
        sha_m15 = manifest["timeframes"]["M15"]["sha256"]
        self.assertEqual(len(sha_m1), 64)
        self.assertEqual(len(sha_m15), 64)

    def test_qc_verification_report_parity(self):
        """Kiểm tra qc_verification_report.json của Máy 2 đối chiếu khớp 100% với Máy 1."""
        self.assertTrue(QC_REPORT_FILE.exists(), "research/qc_verification_report.json không tồn tại")
        with open(QC_REPORT_FILE, "r", encoding="utf-8") as f:
            qc = json.load(f)

        self.assertEqual(qc["qc_evaluator"], "machine_2_independent_qc")
        self.assertEqual(qc["source_connection_mode"], "sqlite_uri_readonly")
        self.assertEqual(qc["gate_evaluation"]["verdict"], "PASS")
        self.assertTrue(qc["gate_evaluation"]["ready_for_protocol_lock"])
        self.assertTrue(qc["gate_evaluation"]["ready_for_baseline_t54_1"])

        # Đối chiếu chéo M1 và M15
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(qc["independent_metrics"]["m1_candle_count"], manifest["timeframes"]["M1"]["count"])
        self.assertEqual(qc["independent_metrics"]["m1_sha256"], manifest["timeframes"]["M1"]["sha256"])
        self.assertEqual(qc["independent_metrics"]["m15_candle_count"], manifest["timeframes"]["M15"]["count"])
        self.assertEqual(qc["independent_metrics"]["m15_sha256"], manifest["timeframes"]["M15"]["sha256"])
        self.assertEqual(qc["independent_metrics"]["duplicates"], 0)
        self.assertEqual(qc["independent_metrics"]["non_monotonic"], 0)

        # Kiểm tra 20 mẫu ngẫu nhiên đều PASS
        self.assertEqual(len(qc["sample_audit"]["samples"]), 20)
        for s in qc["sample_audit"]["samples"]:
            self.assertTrue(s["valid"], f"Sample candle tại {s['time']} không hợp lệ!")


if __name__ == "__main__":
    unittest.main()
