import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
DASHBOARD_SCRIPT = PROJECT_DIRECTORY / "agile_vbt_dashboard.py"


class StreamlitDashboardSmokeTests(unittest.TestCase):
    def test_dashboard_renders_without_an_application_exception(self):
        dashboard = AppTest.from_file(
            DASHBOARD_SCRIPT,
            default_timeout=30,
        ).run()
        self.assertEqual(list(dashboard.exception), [])
        self.assertEqual(list(dashboard.title), [])
        self.assertIn(
            "Follow newest recording",
            [toggle.label for toggle in dashboard.toggle],
        )
        self.assertEqual(len(dashboard.get("plotly_chart")), 0)
        self.assertEqual(len(dashboard.get("select_slider")), 0)
        self.assertEqual(
            [slider.label for slider in dashboard.slider],
            ["Visible history (seconds)"],
        )


if __name__ == "__main__":
    unittest.main()
