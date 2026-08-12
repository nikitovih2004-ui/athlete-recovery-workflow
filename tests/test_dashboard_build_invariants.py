import tempfile
import unittest
from pathlib import Path

import dashboard_contract


class DashboardBuildInvariantTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        self.template = (root / "dashboard_template.html").read_text(encoding="utf-8")
        ui = root / "dashboard_ui"
        self.css = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(ui.glob("*.css"))
        )
        self.js = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(ui.glob("*.js"))
        )

    def test_canonical_template_satisfies_rebuild_contract(self):
        dashboard_contract.validate_template(
            self.template, design_css=self.css, design_js=self.js
        )

    def test_missing_upper_metric_registration_fails_closed(self):
        broken = self.js.replace("metric:'hrv'", "metric:'broken-hrv'")
        with self.assertRaisesRegex(
            dashboard_contract.DashboardContractError,
            "missing expansion registration for hrv",
        ):
            dashboard_contract.validate_template(
                self.template, design_css=self.css, design_js=broken
            )

    def test_duplicate_placeholder_is_rejected(self):
        with self.assertRaisesRegex(
            dashboard_contract.DashboardContractError, "occurs 2 times"
        ):
            dashboard_contract.validate_template(
                self.template + "\n/*__DATA__*/{}",
                design_css=self.css,
                design_js=self.js,
            )

    def test_failed_validation_cannot_replace_existing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "dashboard.html"
            output.write_text("known-good", encoding="utf-8")
            with self.assertRaises(dashboard_contract.DashboardContractError):
                dashboard_contract.validate_artifact("<html></html>")
            self.assertEqual(output.read_text(encoding="utf-8"), "known-good")


if __name__ == "__main__":
    unittest.main()
