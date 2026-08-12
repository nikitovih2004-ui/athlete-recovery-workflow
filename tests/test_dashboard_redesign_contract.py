import os
import tempfile
import unittest
from unittest.mock import patch

import build_dashboard


class DashboardPartialDataTests(unittest.TestCase):
    def test_recovery_only_dataset_builds_without_workouts(self):
        analytics = build_dashboard.build_analytics(
            [],
            [{"date": "2026-07-09", "score": 72, "resting_hr": 55, "hrv": 61.2}],
            [],
        )
        self.assertEqual(analytics["summary"]["n_workouts"], 0)
        self.assertIsNone(analytics["summary"]["avg_strain"])
        self.assertEqual(analytics["days"][0]["recovery"], 72)
        self.assertEqual(analytics["generated_at"], "2026-07-09")

    def test_empty_dataset_has_explicit_empty_contract(self):
        analytics = build_dashboard.build_analytics([], [], [])
        self.assertEqual(analytics["days"], [])
        self.assertEqual(analytics["summary"]["n_days"], 0)
        self.assertIsNone(analytics["summary"]["date_start"])
        self.assertIsNone(analytics["generated_at"])

    def test_manual_only_bounds_create_deterministic_date_spine(self):
        analytics = build_dashboard.build_analytics(
            [], [], [], ("2026-07-01", "2026-07-03")
        )
        self.assertEqual([day["date"] for day in analytics["days"]], [
            "2026-07-01", "2026-07-02", "2026-07-03"
        ])
        self.assertEqual(analytics["generated_at"], "2026-07-03")


class DashboardAssetAssemblyTests(unittest.TestCase):
    def test_dashboard_publish_is_atomic_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "dashboard.html")
            with open(output, "w", encoding="utf-8") as handle:
                handle.write("old")
            with patch("build_dashboard.os.replace", wraps=os.replace) as replace:
                build_dashboard.write_dashboard_atomic("new dashboard", output)
            with open(output, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "new dashboard")
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(
                [name for name in os.listdir(tmp) if name.startswith(".dashboard-")],
                [],
            )

    def test_assets_are_loaded_in_filename_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = build_dashboard.UI_DIR
            try:
                build_dashboard.UI_DIR = tmp
                for name, content in (("20-b.css", "b"), ("00-a.css", "a"), ("10-c.js", "c")):
                    with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                        handle.write(content)
                css = build_dashboard.load_dashboard_assets(".css")
                self.assertLess(css.index("00-a.css"), css.index("20-b.css"))
                self.assertNotIn("10-c.js", css)
            finally:
                build_dashboard.UI_DIR = original

    def test_lifestyle_sheet_is_initialized_from_canonical_snapshot_not_cache(self):
        import inspect
        source = inspect.getsource(build_dashboard.main)
        self.assertNotIn("load_lifestyle_insights()", source)
        self.assertIn('"source": "canonical_read_model"', source)

    def test_state_screen_asset_keeps_data_and_accessibility_contracts(self):
        with open(os.path.join(build_dashboard.UI_DIR, "10-state.js"), encoding="utf-8") as handle:
            state_js = handle.read()
        with open(os.path.join(build_dashboard.UI_DIR, "10-state.css"), encoding="utf-8") as handle:
            state_css = handle.read()

        for required in (
            "DATA?.days",
            "DATA?.latest_sleep",
            "is-missing",
            "aria-label",
            "data-metric",
            "prefers-reduced-motion",
        ):
            self.assertIn(required, state_js + state_css)
        # Hero is the liquid-glass recovery ring gauge (V3 redesign, 17.07) —
        # the humanoid figure and the orb/horizon are both gone. The ring
        # carries the recovery detail-sheet hook.
        self.assertIn("state-ring-panel", state_js)
        self.assertIn("ring-fill", state_js)
        self.assertIn('data-metric="recovery"', state_js)
        self.assertNotIn("state-figure", state_js)
        self.assertNotIn("state-horizon", state_js)
        # Reduced motion must be able to still the ring reveal animation.
        self.assertIn(".ring-fill { animation:none; }", state_css)

    def test_vitality_core_replaces_the_state_composition_without_changing_data_contracts(self):
        with open(os.path.join(build_dashboard.UI_DIR, "11-vitality-core.js"), encoding="utf-8") as handle:
            vitality_js = handle.read()
        with open(os.path.join(build_dashboard.UI_DIR, "11-vitality-core.css"), encoding="utf-8") as handle:
            vitality_css = handle.read()

        self.assertIn("PANEL_RENDER.overview = [renderVitalityCore]", vitality_js)
        self.assertIn("DATA?.days", vitality_js)
        self.assertIn("DATA?.latest_sleep", vitality_js)
        self.assertIn('data-metric="recovery"', vitality_js)
        for metric in ('hrv', 'rhr', 'sleep'):
            self.assertIn(f"metric:'{metric}'", vitality_js)
        self.assertIn('data-metric="${metric}"', vitality_js)
        self.assertIn('class="v6-panel v6-sleep" data-metric="sleep"', vitality_js)
        self.assertIn("v6-recovery-ring", vitality_js + vitality_css)
        self.assertNotIn("v6-ring-orbit", vitality_js + vitality_css)
        self.assertIn("Recovery index", vitality_js)
        self.assertIn("Training load context", vitality_js)
        self.assertIn("key:'duration_min'", vitality_js)
        self.assertIn("key:'workout_count'", vitality_js)
        self.assertIn("v6-trace-baseline", vitality_js + vitality_css)
        self.assertIn("v6-trajectory", vitality_js + vitality_css)
        self.assertIn("prefers-reduced-motion", vitality_css)
        self.assertIn("prefers-reduced-transparency", vitality_css)
        self.assertNotIn("wireRingReplay", vitality_js)
        self.assertNotIn("is-resetting", vitality_js + vitality_css)
        self.assertNotIn("v6-ring-sweep", vitality_js + vitality_css)
        self.assertNotIn("v6-ring-inner-edge", vitality_js + vitality_css)
        self.assertNotIn("v6-ring-bevel", vitality_js + vitality_css)
        self.assertNotIn("v6-ring-ticks", vitality_js + vitality_css)
        self.assertNotIn("v6-signal-plot > i", vitality_css)
        self.assertIn("stroke-dasharray: 4 5", vitality_css)
        self.assertIn(".v6-signal-title small { color: #929d96", vitality_css)
        self.assertIn("stroke: rgba(220,231,225,.42)", vitality_css)
        self.assertIn("rgb(var(--vital-rgb) / .028)", vitality_css)
        self.assertNotIn("rgb(var(--vital-rgb) / .115)", vitality_css)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", vitality_css)
        self.assertIn(".tab { min-width: 0; min-height: 44px;", vitality_css)
        self.assertNotIn(".tabs::-webkit-scrollbar", vitality_css)
        self.assertIn("--v6-ring-load-duration: 1.9s", vitality_css)
        self.assertNotIn(".v6-core-button:hover { transform", vitality_css)
        self.assertNotIn(".v6-core-button:hover { filter", vitality_css)
        self.assertIn("v6-ring-aura", vitality_js + vitality_css)
        self.assertIn(".v6-recovery-ring::before", vitality_css)
        self.assertIn(".v6-recovery-ring::after", vitality_css)
        self.assertNotIn(".v6-core-reading::before", vitality_css)
        self.assertNotIn("<em>${escapeHTML(state.label)}</em>", vitality_js)
        self.assertIn('<span class="v6-core-delta">${escapeHTML(deltaText)}</span>', vitality_js)
        self.assertIn("No change from the previous day.", vitality_js)

    def test_dashboard_has_a_single_english_localization_boundary(self):
        with open(os.path.join(build_dashboard.UI_DIR, "05-english.js"), encoding="utf-8") as handle:
            english_js = handle.read()
        with open(build_dashboard.TEMPLATE, encoding="utf-8") as handle:
            template = handle.read()

        self.assertIn("document.documentElement.lang = 'en'", english_js)
        self.assertIn("MutationObserver", english_js)
        self.assertIn("WHOOP Dashboard sections", english_js)
        self.assertIn("aria-label", english_js)
        self.assertNotIn("const patterns", english_js)
        self.assertNotIn("output.replace", english_js)
        self.assertIn("Lifestyle and biometrics", template)
        self.assertIn("No structured daily-factor records", template)
        self.assertIn("Latest duration", template)
        self.assertIn("Average sleep performance", template)
        self.assertIn("Previous-day load", template)
        self.assertNotIn("????", template)

    def test_vitality_overview_expansion_contract_survives_rebuild(self):
        with open(os.path.join(build_dashboard.UI_DIR, "11-vitality-core.js"), encoding="utf-8") as handle:
            vitality = handle.read()
        with open(os.path.join(build_dashboard.UI_DIR, "90-polish.js"), encoding="utf-8") as handle:
            polish = handle.read()
        with open(build_dashboard.TEMPLATE, encoding="utf-8") as handle:
            generated = handle.read().replace(
                "/*__DESIGN_JS__*/", build_dashboard.load_dashboard_assets(".js"),
            )

        upper = {
            "recovery": "recovery",
            "hrv": "hrv",
            "rhr": "resting_hr",
            "sleep_perf": "sleep_perf",
            "sleep": "sleep_h",
        }
        lower = {"load": "load", "duration": "duration_min", "sessions": "workout_count"}
        for metric, key in {**upper, **lower}.items():
            self.assertIn(f"metric:'{metric}'", vitality)
            self.assertIn(f"{metric}: '{key}'", polish)
        self.assertIn("registerOverviewMetrics();", vitality)
        self.assertIn("registerLoadContext();", vitality)
        self.assertIn('signalRow({metric:\'sleep_perf\'', vitality)
        self.assertIn('data-metric="sleep"', vitality)
        self.assertIn("const EXPAND_SPECS={};", generated)
        self.assertIn("function openSheet(metric,cardEl)", generated)
        self.assertNotIn("/*__DESIGN_JS__*/", generated)

    def test_dashboard_uses_the_local_bahnschrift_typography_stack(self):
        with open(build_dashboard.TEMPLATE, encoding="utf-8") as handle:
            template = handle.read()
        with open(os.path.join(build_dashboard.UI_DIR, "00-foundation.css"), encoding="utf-8") as handle:
            foundation = handle.read()

        self.assertIn("Bahnschrift", template)
        self.assertIn('"Bahnschrift"', foundation)

    def test_shader_background_caps_pixel_ratio_and_pauses_when_hidden(self):
        with open(os.path.join(build_dashboard.UI_DIR, "97-shader-bg.js"), encoding="utf-8") as handle:
            shader_js = handle.read()

        self.assertIn("Math.min(devicePixelRatio || 1, DPR_CAP)", shader_js)
        self.assertIn("DPR_CAP = 2", shader_js)
        self.assertIn("document.hidden", shader_js)
        self.assertIn("prefers-reduced-motion: reduce", shader_js)
        self.assertIn("visibilitychange", shader_js)
        # Verbatim recipe: exact colours and packed uniforms, cursor disabled.
        self.assertIn("0.063, 0.000, 0.169", shader_js)
        self.assertIn("CURSOR = [0.0, 2.0, 0.65, 0.46]", shader_js)

    def test_detail_sheets_keep_dynamics_style_hover_available(self):
        with open(build_dashboard.TEMPLATE, encoding="utf-8") as handle:
            template = handle.read()
        with open(os.path.join(build_dashboard.UI_DIR, "90-polish.css"), encoding="utf-8") as handle:
            polish = handle.read()

        self.assertIn("detail-chart-hit", template)
        self.assertIn("node.addEventListener('mousemove',reveal)", template)
        self.assertIn("node.addEventListener('pointerdown',reveal)", template)
        self.assertIn("'pointer-events':'all'", template)
        self.assertIn("if(!hasFocus)hideTT()", template)
        self.assertIn("{focusable:true}", template)
        self.assertIn("detail-chart-hit", polish)

    def test_detail_sheet_tooltip_layer_is_above_the_sheet_layer(self):
        with open(os.path.join(build_dashboard.UI_DIR, "00-foundation.css"), encoding="utf-8") as handle:
            foundation = handle.read()

        # The tooltip is a body-level fixed element.  If its semantic layer is
        # below the modal layer, its interaction still fires but the result is
        # completely occluded by every State detail sheet.
        self.assertIn("--z-sheet: 60;", foundation)
        self.assertIn("--z-tooltip: 80;", foundation)
        self.assertIn("#tt { z-index: var(--z-tooltip)", foundation)
        self.assertIn(".sheet-backdrop {\n  z-index: var(--z-sheet)", foundation)

    def test_phase6_product_screens_keep_shared_foundation_contracts(self):
        def asset(name):
            with open(os.path.join(build_dashboard.UI_DIR, name), encoding="utf-8") as handle:
                return handle.read()

        foundation = asset("00-foundation.css")
        trends = asset("20-trends.js") + asset("20-trends.css")
        products = asset("30-products.js") + asset("30-products.css")
        polish = asset("90-polish.css") + asset("90-polish.js")
        glass = asset("95-glass.css")

        self.assertIn('overflow-x: auto', foundation)
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr))', foundation)
        self.assertIn('.tab { width: 100%; min-width: 0; min-height: 44px;', foundation)
        self.assertNotIn('.tabs { padding: 3px; gap: 1px; overflow: hidden; }', foundation)
        self.assertIn('syncStateEnvironment', polish)
        self.assertIn('paired observations', trends)
        self.assertIn('activity-overview-grid', products)
        self.assertIn('factor-supplement-evidence', products)
        self.assertIn('prefers-reduced-transparency', polish)
        self.assertIn('@media (prefers-reduced-transparency: reduce)', glass)
        self.assertIn('.navbar,', glass)
        self.assertIn('.state-active .navbar,', glass)
        self.assertIn('.state-active .navbar { background: rgba(5, 6, 8, .98); }', glass)


if __name__ == "__main__":
    unittest.main()
