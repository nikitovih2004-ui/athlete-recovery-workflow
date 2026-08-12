"""Narrow tests for the Activity integration (manual workouts / cardio / supplements)
read path used by build_dashboard.py.

All tests use a temporary SQLite database — never the production data/whoop.db.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import build_dashboard
import workouts_db


def _temp_conn(tmp_dir):
    db_path = str(Path(tmp_dir) / "whoop.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


class ActivityQueryShapeTests(unittest.TestCase):
    """Verifies the actual SQL executed by the dashboard read path (traced,
    not just inspected as source text)."""

    def _traced_sql(self, conn, fn, *args, **kwargs):
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            result = fn(conn, *args, **kwargs)
        finally:
            conn.set_trace_callback(None)
        return result, statements

    def test_manual_workouts_query_has_no_select_star_order_by_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                _, statements = self._traced_sql(
                    conn, workouts_db.get_manual_workouts_for_dashboard, limit=100
                )
                selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
                self.assertTrue(selects, "expected a SELECT to be executed")
                for sql in selects:
                    self.assertNotIn("SELECT *", sql.upper().replace("\n", " "))
                    self.assertIn("ORDER BY", sql.upper())
                    self.assertIn("LIMIT", sql.upper())
            finally:
                conn.close()

    def test_cardio_query_has_no_select_star_order_by_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                _, statements = self._traced_sql(
                    conn, workouts_db.get_cardio_for_dashboard, limit=100
                )
                selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
                self.assertTrue(selects)
                for sql in selects:
                    self.assertNotIn("SELECT *", sql.upper().replace("\n", " "))
                    self.assertIn("ORDER BY", sql.upper())
                    self.assertIn("LIMIT", sql.upper())
            finally:
                conn.close()

    def test_supplements_query_has_no_select_star_order_by_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                _, statements = self._traced_sql(
                    conn, workouts_db.get_supplements_for_dashboard, limit=200
                )
                selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
                self.assertTrue(selects)
                for sql in selects:
                    self.assertNotIn("SELECT *", sql.upper().replace("\n", " "))
                    self.assertIn("ORDER BY", sql.upper())
                    self.assertIn("LIMIT", sql.upper())
            finally:
                conn.close()

    def test_limit_is_actually_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                for i in range(150):
                    conn.execute(
                        "INSERT INTO workout_exercises (date, exercise_name, weight, sets, reps, volume) "
                        "VALUES (?,?,?,?,?,?)",
                        (f"2026-01-{(i % 28) + 1:02d}", "Squat", 100, 3, 5, 1500),
                    )
                conn.commit()
                rows = workouts_db.get_manual_workouts_for_dashboard(conn, limit=100)
                self.assertEqual(len(rows), 100)
            finally:
                conn.close()


class ActivityReadOnlyTests(unittest.TestCase):
    def test_dashboard_read_does_not_create_tables_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                # No ensure_tables() call — simulates a fresh DB where the bot
                # has never logged any activity yet.
                manual = workouts_db.get_manual_workouts_for_dashboard(conn)
                cardio = workouts_db.get_cardio_for_dashboard(conn)
                supplements = workouts_db.get_supplements_for_dashboard(conn)
                self.assertEqual(manual, [])
                self.assertEqual(cardio, [])
                self.assertEqual(supplements, [])

                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertNotIn("workout_exercises", tables)
                self.assertNotIn("cardio_exercises", tables)
                self.assertNotIn("supplements_log", tables)
            finally:
                conn.close()

    def test_dashboard_read_does_not_mutate_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                conn.execute(
                    "INSERT INTO workout_exercises (date, exercise_name, weight, sets, reps, volume) "
                    "VALUES ('2026-01-01','Bench',80,3,5,1200)"
                )
                conn.commit()
                before = conn.execute("SELECT * FROM workout_exercises").fetchall()
                before_master = conn.execute(
                    "SELECT name, sql FROM sqlite_master ORDER BY name"
                ).fetchall()

                workouts_db.get_manual_workouts_for_dashboard(conn)
                workouts_db.get_cardio_for_dashboard(conn)
                workouts_db.get_supplements_for_dashboard(conn)

                after = conn.execute("SELECT * FROM workout_exercises").fetchall()
                after_master = conn.execute(
                    "SELECT name, sql FROM sqlite_master ORDER BY name"
                ).fetchall()
                self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
                self.assertEqual(
                    [tuple(r) for r in before_master], [tuple(r) for r in after_master]
                )
            finally:
                conn.close()


class ActivityPayloadShapeTests(unittest.TestCase):
    def test_payload_has_no_internal_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                conn.execute(
                    "INSERT INTO workout_exercises "
                    "(date, exercise_name, weight, sets, reps, volume, raw_text, source_key) "
                    "VALUES ('2026-01-01','Row',50,3,10,1500,'raw','key-1')"
                )
                conn.execute(
                    "INSERT INTO supplements_log "
                    "(date, time, name, dosage, taken, raw_text, source_key) "
                    "VALUES ('2026-01-01','08:00','Creatine','5g',1,'raw','key-2')"
                )
                conn.execute(
                    "INSERT INTO cardio_exercises "
                    "(date, time, type, duration, distance, avg_hr, calories, raw_text, source_key) "
                    "VALUES ('2026-01-01','07:00','Run',30,5,140,300,'raw','key-3')"
                )
                conn.commit()

                manual = workouts_db.get_manual_workouts_for_dashboard(conn)
                cardio = workouts_db.get_cardio_for_dashboard(conn)
                supplements = workouts_db.get_supplements_for_dashboard(conn)

                self.assertEqual(
                    set(manual[0].keys()),
                    {"date", "exercise_name", "weight", "sets", "reps", "volume"},
                )
                self.assertEqual(
                    set(supplements[0].keys()),
                    {"date", "time", "name", "dosage", "taken"},
                )
                expected_cardio_keys = {
                    "date", "time", "type", "duration", "distance", "avg_hr", "calories",
                    "hr_zone_0_duration", "hr_zone_1_duration", "hr_zone_2_duration",
                    "hr_zone_3_duration", "hr_zone_4_duration", "hr_zone_5_duration",
                }
                self.assertEqual(set(cardio[0].keys()), expected_cardio_keys)
                for payload in (manual[0], cardio[0], supplements[0]):
                    self.assertNotIn("id", payload)
                    self.assertNotIn("raw_text", payload)
                    self.assertNotIn("source_key", payload)
            finally:
                conn.close()

    def test_missing_numeric_values_stay_none_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                conn.execute(
                    "INSERT INTO workout_exercises (date, exercise_name, weight, sets, reps, volume) "
                    "VALUES ('2026-01-01','Deadlift',NULL,NULL,NULL,NULL)"
                )
                conn.commit()
                rows = workouts_db.get_manual_workouts_for_dashboard(conn)
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["weight"])
                self.assertIsNone(rows[0]["sets"])
                self.assertIsNone(rows[0]["reps"])
                self.assertIsNone(rows[0]["volume"])
                # explicitly NOT coerced to 0
                self.assertNotEqual(rows[0]["weight"], 0)
            finally:
                conn.close()

    def test_taken_1_0_and_null_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                conn.execute(
                    "INSERT INTO supplements_log (date, time, name, dosage, taken) "
                    "VALUES ('2026-01-01','08:00','Vitamin D','1000IU',1)"
                )
                conn.execute(
                    "INSERT INTO supplements_log (date, time, name, dosage, taken) "
                    "VALUES ('2026-01-01','09:00','Magnesium','200mg',0)"
                )
                conn.execute(
                    "INSERT INTO supplements_log (date, time, name, dosage, taken) "
                    "VALUES ('2026-01-01','10:00','Omega-3',NULL,NULL)"
                )
                conn.commit()
                rows = workouts_db.get_supplements_for_dashboard(conn)
                by_name = {r["name"]: r["taken"] for r in rows}
                self.assertEqual(by_name["Vitamin D"], 1)
                self.assertEqual(by_name["Magnesium"], 0)
                self.assertIsNone(by_name["Omega-3"])
            finally:
                conn.close()

    def test_very_long_text_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                long_name = "A" * 5000
                conn.execute(
                    "INSERT INTO workout_exercises (date, exercise_name, weight, sets, reps, volume) "
                    "VALUES ('2026-01-01', ?, 10, 1, 1, 10)",
                    (long_name,),
                )
                conn.commit()
                rows = workouts_db.get_manual_workouts_for_dashboard(conn)
                self.assertLessEqual(len(rows[0]["exercise_name"]), 300)
            finally:
                conn.close()

    def test_ordering_is_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _temp_conn(tmp)
            try:
                workouts_db.ensure_tables(conn)
                for date in ("2026-01-01", "2026-01-03", "2026-01-02"):
                    conn.execute(
                        "INSERT INTO workout_exercises (date, exercise_name, weight, sets, reps, volume) "
                        "VALUES (?, 'Row', 10, 1, 1, 10)",
                        (date,),
                    )
                conn.commit()
                rows = workouts_db.get_manual_workouts_for_dashboard(conn)
                self.assertEqual([r["date"] for r in rows], ["2026-01-03", "2026-01-02", "2026-01-01"])
            finally:
                conn.close()


class InlineScriptSerializationTests(unittest.TestCase):
    def test_script_close_tag_cannot_break_out(self):
        payload = {"exercise_name": "</script><script>alert(1)</script>"}
        escaped = build_dashboard.dumps_for_inline_script(payload)
        self.assertNotIn("</script>", escaped)
        self.assertNotIn("<script>", escaped)

    def test_dangerous_html_and_attribute_payloads_are_neutralized_in_source(self):
        payload = {
            "a": "<img src=x onerror=alert(3)>",
            "b": '"quoted" \'and\' <b>bold</b> & co',
        }
        escaped = build_dashboard.dumps_for_inline_script(payload)
        self.assertNotIn("<img", escaped)
        self.assertNotIn("<b>", escaped)

    def test_line_and_paragraph_separators_are_escaped(self):
        payload = {"note": "line1 line2 line3"}
        escaped = build_dashboard.dumps_for_inline_script(payload)
        self.assertNotIn(" ", escaped)
        self.assertNotIn(" ", escaped)
        self.assertIn("\\u2028", escaped)
        self.assertIn("\\u2029", escaped)

    def test_round_trip_preserves_original_values(self):
        # \uXXXX is valid JSON string-escape syntax, so json.loads can decode
        # the escaped payload directly -- this is exactly what a JS engine
        # does when it parses `const DATA = {escaped};` as an object literal.
        payload = {
            "quotes": "she said \"hi\" & 'bye'",
            "unicode": "Кириллица, emoji: 🏃‍♂️ 💊",
            "breakout": "</script><script>alert(2)</script>",
            "seps": "a b c",
        }
        escaped = build_dashboard.dumps_for_inline_script(payload)
        round_tripped = json.loads(escaped)
        self.assertEqual(round_tripped, payload)

    def test_full_analytics_payload_with_malicious_activity_has_no_breakout(self):
        analytics = {
            "manual_workouts": [
                {"date": "2026-01-01", "exercise_name": "</script><script>alert(1)</script>",
                 "weight": 10, "sets": 1, "reps": 1, "volume": 10}
            ],
            "cardio_exercises": [
                {"date": "2026-01-01", "time": "08:00", "type": "<img src=x onerror=alert(2)>",
                 "duration": 30, "distance": None, "avg_hr": None, "calories": None}
            ],
            "supplements_log": [
                {"date": "2026-01-01", "time": "08:00", "name": "a&b<c>d", "dosage": None, "taken": None}
            ],
        }
        escaped = build_dashboard.dumps_for_inline_script(analytics)
        self.assertNotIn("</script>", escaped)
        self.assertEqual(json.loads(escaped), analytics)


class ActivityRendererSourceSafetyTests(unittest.TestCase):
    """Structural check on dashboard_template.html: the activity renderers must
    not build HTML via innerHTML + template-literal interpolation of DB fields."""

    def _renderer_source(self, fn_name):
        template = Path(__file__).resolve().parent.parent / "dashboard_template.html"
        text = template.read_text(encoding="utf-8")
        start = text.index(f"function {fn_name}(")
        # function bodies here are followed by another top-level function or </script>
        end = text.index("\n}\n", start) + 3
        return text[start:end]

    def test_render_functions_do_not_use_innerHTML_with_interpolation(self):
        for fn_name in ("renderManualWorkouts", "renderCardioExercises", "renderSupplementsLog"):
            src = self._renderer_source(fn_name)
            self.assertNotIn("innerHTML", src, f"{fn_name} should not use innerHTML directly")

    def test_shared_render_helpers_use_textContent_not_innerHTML(self):
        template = Path(__file__).resolve().parent.parent / "dashboard_template.html"
        text = template.read_text(encoding="utf-8")
        start = text.index("function activityMakeEl(")
        end = text.index("function renderManualWorkouts(")
        shared = text[start:end]
        self.assertIn("textContent", shared)
        self.assertNotIn(".innerHTML=", shared.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
