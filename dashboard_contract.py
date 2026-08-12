"""Fail-closed structural contract for every generated Dashboard artifact."""
from __future__ import annotations


class DashboardContractError(RuntimeError):
    pass


PLACEHOLDERS = (
    "/*__DATA__*/{}",
    "/*__DESIGN_CSS__*/",
    "/*__DESIGN_JS__*/",
)

EXPANSION_KEYS = (
    "recovery",
    "hrv",
    "rhr",
    "sleep",
    "sleep_perf",
    "load",
    "duration",
    "sessions",
)

REQUIRED_BEHAVIOR = (
    '<html lang="en">',
    "const EXPAND_SPECS={};",
    "function openSheet(metric,cardEl)",
    "closest('[data-metric]')",
    "registerOverviewMetrics();",
    'data-metric="recovery"',
    'data-metric="sleep"',
    'data-metric="${metric}"',
    "v6-panel v6-sleep",
    "</html>",
)


def validate_template(template, *, design_css, design_js):
    failures = []
    for placeholder in PLACEHOLDERS:
        count = template.count(placeholder)
        if count != 1:
            failures.append(f"placeholder {placeholder!r} occurs {count} times")
    if not design_css.strip():
        failures.append("dashboard CSS asset set is empty")
    if not design_js.strip():
        failures.append("dashboard JS asset set is empty")
    for key in EXPANSION_KEYS:
        token = f"metric:'{key}'"
        if token not in design_js:
            failures.append(f"missing expansion registration for {key}")
    combined = template + "\n" + design_js
    for token in REQUIRED_BEHAVIOR:
        if token not in combined:
            failures.append(f"missing required behavior token {token!r}")
    if failures:
        raise DashboardContractError("; ".join(failures))


def validate_artifact(text):
    failures = []
    for placeholder in PLACEHOLDERS:
        if placeholder in text:
            failures.append(f"unresolved placeholder {placeholder!r}")
    if "/*__DESIGN_" in text:
        failures.append("unresolved design placeholder")
    for token in REQUIRED_BEHAVIOR:
        if token not in text:
            failures.append(f"missing required behavior token {token!r}")
    for key in EXPANSION_KEYS:
        if f"metric:'{key}'" not in text:
            failures.append(f"missing generated expansion registration for {key}")
    if failures:
        raise DashboardContractError("; ".join(failures))
    return {"size": len(text.encode("utf-8")), "expansion_keys": list(EXPANSION_KEYS)}
