"""Central disabled-by-default feature flags for the Phase 2 rollout."""
from __future__ import annotations

import os

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
FLAG_DEFAULTS = {
    "CONVERSATIONAL_ROUTER_ENABLED": False,
    "LEGACY_DESTRUCTIVE_TEXT_ENABLED": False,
    "CONVERSATION_MEMORY_ENABLED": False,
    "CONVERSATION_ANALYTICS_V2_ENABLED": False,
    "BOUNDED_GEMINI_AGENT_ENABLED": False,
    "DAILY_FACTOR_CAPTURE_ENABLED": False,
    "WEEKLY_ANALYSIS_V2_ENABLED": False,
    "GEMINI_VISION_ENABLED": False,
}


def env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be an explicit boolean")


def validate_environment():
    return {
        name: env_flag(name, default)
        for name, default in FLAG_DEFAULTS.items()
    }


def router_enabled():
    return env_flag(
        "CONVERSATIONAL_ROUTER_ENABLED",
        FLAG_DEFAULTS["CONVERSATIONAL_ROUTER_ENABLED"],
    )


def legacy_destructive_text_enabled():
    return env_flag(
        "LEGACY_DESTRUCTIVE_TEXT_ENABLED",
        FLAG_DEFAULTS["LEGACY_DESTRUCTIVE_TEXT_ENABLED"],
    )


def memory_enabled():
    # Enable only after the additive migration and read-only smoke checks pass.
    return env_flag("CONVERSATION_MEMORY_ENABLED", False)


def analytics_v2_enabled():
    # Production rollout remains explicit and reversible.
    return env_flag("CONVERSATION_ANALYTICS_V2_ENABLED", False)


def bounded_agent_enabled():
    """Native Gemini function calling; disabled until explicit rollout."""
    return env_flag("BOUNDED_GEMINI_AGENT_ENABLED", False)


def factor_capture_enabled():
    return env_flag("DAILY_FACTOR_CAPTURE_ENABLED", False)


def weekly_v2_enabled():
    return env_flag("WEEKLY_ANALYSIS_V2_ENABLED", False)


def gemini_vision_enabled():
    """Outbound image analysis requires an explicit privacy opt-in."""
    return env_flag("GEMINI_VISION_ENABLED", False)
