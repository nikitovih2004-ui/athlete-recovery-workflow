"""Canonical contract for the conversational Gemini router (MVP).

Single source of truth for: allowed intents, the strict router envelope, the
deterministic intent -> tool allowlist, confidence gates, input/argument limits,
error codes, and the audit/pending state vocabularies.

Nothing here talks to Gemini, SQLite, or Telegram. It is pure data so both the
validator and the tests can import it without side effects. See
"Conversational Gemini MVP — Architecture Contract" sections 3, 5, 6, 7, 10.
"""
from __future__ import annotations

# --- prompt / schema versioning -------------------------------------------------

SCHEMA_VERSION = "conversation_router_v1"
PROMPT_VERSION = "conversation_router_v1"
PHASE2_PROMPT_VERSION = "conversation_router_v2"

# --- intents --------------------------------------------------------------------

INTENT_LOG_STRENGTH = "log_strength_workout"
INTENT_START_STRENGTH_DRAFT = "start_strength_draft"
INTENT_LOG_CARDIO = "log_cardio"
INTENT_LOG_SUPPLEMENT = "log_supplement"
INTENT_SAVE_DAILY_CONTEXT = "save_daily_context"
INTENT_GET_TODAY_STATUS = "get_today_status"
INTENT_GET_WEEK_SUMMARY = "get_week_summary"
INTENT_GET_METRIC_TREND = "get_metric_trend"
INTENT_GET_FACTOR_OBSERVATION = "get_factor_observation"
INTENT_GET_DAY_SNAPSHOT = "get_day_snapshot"
INTENT_GET_DATA_COVERAGE = "get_data_coverage"
INTENT_GET_SUPPLEMENT_RECORDS = "get_supplement_records"
INTENT_CORRECT_LOGGED_ACTIVITY = "correct_logged_activity"
INTENT_GENERAL_CONVERSATION = "general_conversation"
INTENT_NEEDS_CLARIFICATION = "needs_clarification"
INTENT_UNSUPPORTED_REQUEST = "unsupported_request"

ALLOWED_INTENTS = frozenset({
    INTENT_LOG_STRENGTH,
    INTENT_START_STRENGTH_DRAFT,
    INTENT_LOG_CARDIO,
    INTENT_LOG_SUPPLEMENT,
    INTENT_SAVE_DAILY_CONTEXT,
    INTENT_GET_TODAY_STATUS,
    INTENT_GET_WEEK_SUMMARY,
    INTENT_GET_METRIC_TREND,
    INTENT_GET_FACTOR_OBSERVATION,
    INTENT_GET_DAY_SNAPSHOT, INTENT_GET_DATA_COVERAGE, INTENT_GET_SUPPLEMENT_RECORDS,
    INTENT_CORRECT_LOGGED_ACTIVITY,
    INTENT_GENERAL_CONVERSATION,
    INTENT_NEEDS_CLARIFICATION,
    INTENT_UNSUPPORTED_REQUEST,
})

MUTATION_INTENTS = frozenset({
    INTENT_LOG_STRENGTH,
    INTENT_LOG_CARDIO,
    INTENT_LOG_SUPPLEMENT,
    INTENT_SAVE_DAILY_CONTEXT,
})

READ_INTENTS = frozenset({
    INTENT_GET_TODAY_STATUS,
    INTENT_GET_WEEK_SUMMARY,
    INTENT_GET_METRIC_TREND,
    INTENT_GET_FACTOR_OBSERVATION,
    INTENT_GET_DAY_SNAPSHOT, INTENT_GET_DATA_COVERAGE, INTENT_GET_SUPPLEMENT_RECORDS,
})

# Intents that may never open a mutation transaction.
NON_ACTIONING_INTENTS = frozenset({
    INTENT_GENERAL_CONVERSATION,
    INTENT_NEEDS_CLARIFICATION,
    INTENT_UNSUPPORTED_REQUEST,
})

# --- static intent -> tool allowlist (model never selects a tool) ---------------

INTENT_TO_TOOL = {
    INTENT_LOG_STRENGTH: "log_strength_workout",
    INTENT_LOG_CARDIO: "log_cardio",
    INTENT_LOG_SUPPLEMENT: "log_supplement",
    INTENT_SAVE_DAILY_CONTEXT: "save_daily_context",
    INTENT_GET_TODAY_STATUS: "get_today_status",
    INTENT_GET_WEEK_SUMMARY: "get_week_summary",
    INTENT_GET_METRIC_TREND: "get_metric_trend",
    INTENT_GET_FACTOR_OBSERVATION: "get_factor_observation",
    INTENT_GET_DAY_SNAPSHOT: "get_day_snapshot",
    INTENT_GET_DATA_COVERAGE: "get_data_coverage",
    INTENT_GET_SUPPLEMENT_RECORDS: "get_supplement_records",
}

ALLOWED_TOOLS = frozenset(INTENT_TO_TOOL.values())


def tool_for_intent(intent):
    """Return the single allowlisted tool for an intent, or None (non-actioning)."""
    return INTENT_TO_TOOL.get(intent)


# --- confidence gates -----------------------------------------------------------

CONFIDENCE_MUTATION = 0.90
CONFIDENCE_READ = 0.80
CONFIDENCE_GENERAL = 0.70


def confidence_threshold(intent):
    """Minimum confidence required before an intent is acted on."""
    if intent in MUTATION_INTENTS:
        return CONFIDENCE_MUTATION
    if intent in READ_INTENTS:
        return CONFIDENCE_READ
    if intent == INTENT_GENERAL_CONVERSATION:
        return CONFIDENCE_GENERAL
    # clarification / unsupported are always safe to surface.
    return 0.0


# --- date reference vocabulary --------------------------------------------------

DATE_KIND_TODAY = "today"
DATE_KIND_YESTERDAY = "yesterday"
DATE_KIND_ABSOLUTE = "absolute"
DATE_KIND_UNSPECIFIED = "unspecified"
DATE_KIND_AMBIGUOUS = "ambiguous"

DATE_KINDS = frozenset({
    DATE_KIND_TODAY,
    DATE_KIND_YESTERDAY,
    DATE_KIND_ABSOLUTE,
    DATE_KIND_UNSPECIFIED,
    DATE_KIND_AMBIGUOUS,
})

# --- fact status ----------------------------------------------------------------
# The model must always return `arguments.fact_status` (enforced by the Gemini
# response schema in gemini_client.py) for every intent - "not_applicable" for
# anything that isn't a workout/cardio log. Python never infers or defaults this
# value; it only ever acts on what the model explicitly returned.

FACT_STATUS_COMPLETED = "completed"
FACT_STATUS_CURRENT = "current"
FACT_STATUS_PLANNED = "planned"
FACT_STATUS_UNKNOWN = "unknown"
FACT_STATUS_NOT_APPLICABLE = "not_applicable"

FACT_STATUS_VALUES = frozenset({
    FACT_STATUS_COMPLETED, FACT_STATUS_CURRENT, FACT_STATUS_PLANNED,
    FACT_STATUS_UNKNOWN, FACT_STATUS_NOT_APPLICABLE,
})
# Only these two let a workout/cardio mutation proceed to a write.
FACT_STATUSES_ACTIONABLE = frozenset({FACT_STATUS_COMPLETED, FACT_STATUS_CURRENT})

MSG_PLANNED_WORKOUT = "Понял, это план — как выполненную тренировку не записываю."
MSG_FACT_STATUS_CLARIFICATION = "Это тренировка, которую ты уже выполнил?"
MSG_SUPPLEMENT_STATUS_CLARIFICATION = "Ты уже принял это или только планируешь?"
MSG_DATE_CLARIFICATION = "За какой день это записать?"
MSG_CONTEXT_DATE_CLARIFICATION = "За какой день сохранить контекст?"
MSG_UNSUPPORTED_REQUEST = "Это действие пока не поддерживается; ничего не изменено."
MSG_STRENGTH_MISSING_REPS = (
    "🏋️‍♂️ Я вижу вес, но не разобрал количество повторов — без них силовая не "
    "засчитывается (объём был бы нулевой). Пришли тренировку ещё раз с повторами, "
    "например: «Жим узким хватом 30×9, 30×8»."
)

# --- limits (contract section 6) ------------------------------------------------

MAX_INPUT_CHARS = 4096
MIN_INPUT_CHARS = 1
MAX_NOTES_CHARS = 2000
MAX_NAME_CHARS = 120
MAX_DOSE_CHARS = 64
MAX_WORKOUT_ENTRIES = 100
MAX_SUPPLEMENT_ITEMS = 20
MAX_CARDIO_ACTIVITY_CHARS = 120
MAX_REPLY_TEXT_CHARS = 2000
MAX_CLARIFICATION_QUESTION_CHARS = 500

# Phase 2 bounded read/session contracts. These are enums, not an analytics
# DSL: the model never selects SQL, tables, columns, or aggregations.
TREND_METRICS = frozenset({
    "recovery_score", "hrv_rmssd", "resting_hr",
    "sleep_hours", "sleep_performance",
})
TREND_WINDOWS_DAYS = frozenset({7, 14, 28, 56, 84})
MAX_ANALYTICS_LOOKBACK_DAYS = 90
FACTOR_TYPES = frozenset({"supplement", "daily_factor"})
DAILY_FACTOR_KEYS = frozenset({
    "alcohol", "late_caffeine", "late_meal", "high_stress",
})
MIN_FACTOR_COHORT_DAYS = 5
FACTOR_CAPTURE_MIN_CONFIDENCE = 0.85
MAX_FACTOR_KEY_CHARS = 120

SESSION_TTL_HOURS = 24
SESSION_MAX_TURNS = 6
SESSION_MAX_JSON_BYTES = 8192

# Sane physical maxima for mutation numbers (reject fabricated / absurd values).
MAX_WEIGHT_KG = 1000
MAX_SETS = 100
MAX_REPS = 1000
MAX_DURATION_MINUTES = 24 * 60
MAX_DISTANCE_KM = 1000
MAX_HR_BPM = 300
MAX_CALORIES = 100000
MAX_HR_ZONES = 6

# Mutation dates: not in the future, at most this many days back.
MAX_PAST_DAYS = 366

# --- required envelope keys -----------------------------------------------------

ENVELOPE_KEYS = frozenset({
    "schema_version",
    "intent",
    "confidence",
    "requires_confirmation",
    "arguments",
    "reply_text",
})

# --- error codes ----------------------------------------------------------------

ERR_INPUT_TOO_LONG = "input_too_long"
ERR_INPUT_EMPTY = "input_empty"
ERR_INPUT_CONTROL_CHARS = "input_control_chars"
ERR_MALFORMED_JSON = "malformed_json"
ERR_NON_OBJECT_ROOT = "non_object_root"
ERR_UNKNOWN_KEYS = "unknown_keys"
ERR_MISSING_KEYS = "missing_keys"
ERR_DUPLICATE_JSON_KEYS = "duplicate_json_keys"
ERR_SCHEMA_VERSION = "schema_version_mismatch"
ERR_UNKNOWN_INTENT = "unknown_intent"
ERR_BAD_TYPE = "bad_type"
ERR_BAD_CONFIDENCE = "bad_confidence"
ERR_LOW_CONFIDENCE = "low_confidence"
ERR_NOT_A_FACT = "not_a_fact"
ERR_EMPTY_LIST = "empty_list"
ERR_TOO_MANY_ITEMS = "too_many_items"
ERR_VALUE_OUT_OF_RANGE = "value_out_of_range"
ERR_NON_FINITE_NUMBER = "non_finite_number"
ERR_STRING_TOO_LONG = "string_too_long"
ERR_BAD_DATE = "bad_date"
ERR_FUTURE_DATE = "future_date"
ERR_DATE_TOO_OLD = "date_too_old"
ERR_AMBIGUOUS_DATE = "ambiguous_date"
ERR_UNKNOWN_SUPPLEMENT_STATUS = "unknown_supplement_status"
ERR_MISSING_CONTEXT_DATE = "missing_context_date"
ERR_UNSUPPORTED_TOOL = "unsupported_tool"
ERR_STRENGTH_MISSING_REPS = "strength_missing_reps"
ERR_CANONICAL_READ_REQUIRED = "canonical_read_required"

# --- audit / pending status vocab (contract section 10) -------------------------

ACTION_RECEIVED = "received"
ACTION_NEEDS_CLARIFICATION = "needs_clarification"
ACTION_REJECTED = "rejected"
ACTION_NOOP = "noop"
ACTION_SUCCEEDED = "succeeded"
ACTION_FAILED = "failed"

PENDING_OPEN = "open"
PENDING_RESOLVING = "resolving"
PENDING_RESOLVED = "resolved"
PENDING_EXPIRED = "expired"

# Local timezone for server-side date resolution.
LOCAL_TZ_NAME = "Europe/Kyiv"

# One active clarification is allowed to live this long before it expires.
PENDING_TTL_MINUTES = 60
