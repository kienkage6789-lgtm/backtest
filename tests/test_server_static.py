import unittest
from fastapi.testclient import TestClient
from server import app

class TestServerStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_root_serves_html(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("TradingView Backtest", res.text)
        print("\n[PASS] Root / serves index.html")

    def test_vendor_js(self):
        res = self.client.get("/static/vendor/lightweight-charts.standalone.production.js")
        self.assertEqual(res.status_code, 200)
        self.assertGreater(len(res.content), 100000)
        print("[PASS] Vendor Lightweight Charts JS served correctly (>100KB)")

    def test_css_and_js(self):
        res_css = self.client.get("/static/style.css")
        self.assertEqual(res_css.status_code, 200)
        res_chart = self.client.get("/static/chart.js")
        self.assertEqual(res_chart.status_code, 200)
        res_app = self.client.get("/static/app.js")
        self.assertEqual(res_app.status_code, 200)
        res_drawings = self.client.get("/static/drawings.js")
        self.assertEqual(res_drawings.status_code, 200)
        print("[PASS] style.css, chart.js, app.js, drawings.js served correctly")

if __name__ == '__main__':
    unittest.main()
