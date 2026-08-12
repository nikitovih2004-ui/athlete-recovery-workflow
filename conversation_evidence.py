"""Compatibility API over the shared canonical read model.

Conversation, proactive weekly reports, and analytics now consume the same
versioned snapshots.  The implementation remains bounded and side-effect free.
"""
from __future__ import annotations

import canonical_read_model as CRM


KYIV = CRM.TS.ANALYSIS_TZ
ALLOWED_WINDOWS = frozenset({7, 14, 28, 56, 84})
MAX_FACTOR_DAYS = 90
MIN_FACTOR_COHORT = CRM.MIN_FACTOR_COHORT
MIN_DAILY_FACTOR_CONFIDENCE = CRM.MIN_FACTOR_CONFIDENCE
METRICS = {name: name for name in CRM.METRICS}
DEFAULT_FACTORS = CRM.DEFAULT_FACTORS


class EvidenceInputError(ValueError):
    """The caller requested a value outside the static analytics contract."""


def normalize_factor_key(factor_type, value):
    try:
        return CRM.normalize_factor_key(factor_type, value)
    except CRM.ReadModelInputError as exc:
        raise EvidenceInputError(str(exc)) from None


def _translate(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except CRM.ReadModelInputError as exc:
        raise EvidenceInputError(str(exc)) from None


def metric_trend(conn, metric, days, local_now):
    return _translate(CRM.metric_trend, conn, metric, days, local_now)


def factor_observation(conn, factor_type, factor_key, days, local_now):
    return _translate(
        CRM.factor_observation, conn, factor_type, factor_key, days, local_now
    )


def weekly_evidence(conn, local_now, factors=DEFAULT_FACTORS):
    return _translate(CRM.weekly_snapshot, conn, local_now, factors=factors)
