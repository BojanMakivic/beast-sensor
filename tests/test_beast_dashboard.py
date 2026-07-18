import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
DASHBOARD_SCRIPT = PROJECT_DIRECTORY / "beast_dashboard.py"


class StreamlitDashboardSmokeTests(unittest.TestCase):
    def test_dashboard_renders_without_an_application_exception(self):
        dashboard = AppTest.from_file(
            DASHBOARD_SCRIPT,
            default_timeout=30,
        ).run()
        self.assertEqual(list(dashboard.exception), [])
        self.assertEqual(dashboard.title[0].value, "Beast Live Movement")
        self.assertIn(
            "Follow newest recording",
            [toggle.label for toggle in dashboard.toggle],
        )
        if dashboard.metric:
            self.assertEqual(len(dashboard.get("plotly_chart")), 1)
            self.assertEqual(len(dashboard.dataframe), 1)


if __name__ == "__main__":
    unittest.main()
