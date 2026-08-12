"""Production morning workflow: WHOOP result -> context question -> personal analysis.

This deliberately contains no Windows popup or Task Scheduler behaviour.  It is
called by the VPS cron every 15 minutes while WHOOP is expected to publish a
morning recovery result.
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import morning_context
import morning_observability
import morning_readiness
import morning_reporting
import report_delivery
import weekly_report


HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "whoop.db"
TZ = ZoneInfo("Europe/Kyiv")

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except Exception:
    pass

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
_last_telegram_reason = "telegram_not_attempted"


def log(message):
    print(f"[{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def current_time():
    return dt.datetime.now(TZ)


@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    reason: str
    duration_ms: int
    returncode: int | None = None

    def __bool__(self):
        return self.ok


def run_script(
    name,
    *args,
    timeout=180,
    pipeline_run_id=None,
    pipeline_date=None,
):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if pipeline_run_id:
        env[morning_observability.RUN_ID_ENV] = str(pipeline_run_id)
    if pipeline_date:
        env[morning_observability.PIPELINE_DATE_ENV] = str(pipeline_date)
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(HERE / name), *args],
            cwd=HERE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        duration = round((time.monotonic() - started) * 1000)
        reason = f"subprocess_timeout:{name}"
        log(f"{name}: {reason}")
        return ScriptResult(False, reason, duration)
    except Exception as exc:
        duration = round((time.monotonic() - started) * 1000)
        reason = f"subprocess_start:{name}:{type(exc).__name__}"
        log(f"{name}: {reason}")
        return ScriptResult(False, reason, duration)
    duration = round((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        reason = f"subprocess_exit:{name}:code={result.returncode}"
        log(f"{name}: {reason}")
        return ScriptResult(False, reason, duration, result.returncode)
    return ScriptResult(True, f"subprocess_completed:{name}", duration, 0)


def today_morning_data_present(today):
    return morning_readiness.morning_data_status(today, db_path=DB_PATH)["ready"]


# Compatibility name for external health checks; semantics are now stricter.
today_recovery_present = today_morning_data_present


def send_telegram(text):
    global _last_telegram_reason
    if not TG_TOKEN or not TG_CHAT:
        _last_telegram_reason = "telegram_not_configured"
        log("Telegram не настроен: нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return None
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        if response.status_code == 200 and response.json().get("ok"):
            _last_telegram_reason = "telegram_api_accepted"
            return response.json().get("result", {}).get("message_id")
        _last_telegram_reason = f"telegram_http_status={response.status_code}"
        log(f"Telegram вернул HTTP {response.status_code}")
    except Exception as exc:
        _last_telegram_reason = f"telegram_transport:{type(exc).__name__}"
        # requests exceptions may include the complete bot URL, including the token.
        log(f"Telegram: ошибка отправки ({type(exc).__name__})")
    return None


def send_chunks(text):
    chunks = morning_reporting.split_telegram_text(text)
    return bool(chunks) and all(send_telegram(chunk) for chunk in chunks)


def context_question(context, context_only=False):
    prefix = (
        "Данные WHOOP за это утро не появились до 23:00. "
        "Разбор будет только по контексту и активности, без вымышленных метрик.\n\n"
        if context_only else ""
    )
    return prefix + (
        f"Доброе утро. Контекст дня за {context['evening_date']}.\n\n"
        "Что происходило вчера и что могло повлиять на сон и восстановление?\n\n"
        "Можно одним сообщением описать добавки, лекарства, питание, алкоголь, стресс, "
        "самочувствие, поздний сон и любые другие важные факторы.\n\n"
        "Ответь на это сообщение через Reply — затем пришлю персональный разбор."
    )


def context_reminder(context):
    return (
        f"Напомню про контекст дня {context['evening_date']}. "
        "Без него я не буду приписывать причины твоему Recovery. Ответь одним сообщением — "
        "через Reply на утренний вопрос, и я подготовлю разбор."
    )


def deliver_weekly_if_due(context):
    evening_date = context["evening_date"]
    if not weekly_report.is_weekly_context(evening_date) or weekly_report.was_sent(evening_date):
        return True
    delivery_key = f"weekly_report:{evening_date}"
    report = (
        report_delivery.saved_payload(delivery_key)
        or weekly_report.create_report(evening_date)
    )
    chunks = morning_reporting.split_telegram_text(report)
    if not report_delivery.deliver(
        delivery_key, "weekly_report", report, chunks, send_telegram
    ):
        return False
    weekly_report.mark_sent(evening_date)
    log(f"еженедельный отчёт за неделю до {evening_date} отправлен")
    return True


def _record_once(pipeline_date, run_id, stage, outcome, reason, **kwargs):
    if morning_observability.stage_was_recorded(run_id, stage):
        return None
    return morning_observability.record_stage(
        pipeline_date, run_id, stage, outcome, reason, **kwargs
    )


def _block_unrecorded(pipeline_date, run_id, stages, reason):
    for stage in stages:
        _record_once(
            pipeline_date, run_id, stage, "skipped", f"blocked_by:{reason}"
        )


def deliver_claimed_analysis(context, run_id=None):
    recovery_date = context["recovery_date"]
    analysis_run_id = run_id or morning_observability.new_run_id(
        "scheduled_analysis"
    )
    timer = morning_observability.StageTimer.start(
        recovery_date, analysis_run_id, "analysis_generated"
    )
    try:
        delivery_key = f"daily_result:{recovery_date}"
        report = report_delivery.saved_payload(delivery_key)
        reused = report is not None
        if report is None:
            analysis = morning_reporting.generate_daily_analysis(
                context["evening_date"],
                context_only=context.get("analysis_mode") == "context_only",
            )
            report = morning_reporting.compose_morning_result(
                recovery_date, analysis, db_path=DB_PATH
            )
        if not report_delivery.deliver(
            delivery_key, "daily_result", report, [report], send_telegram
        ):
            timer.finish("failed", "analysis_delivery_claim_unavailable")
            morning_context.release_analysis(recovery_date)
            return False
        if not morning_context.complete_analysis(recovery_date):
            timer.finish("failed", "analysis_completion_state_conflict")
            morning_context.release_analysis(recovery_date)
            return False
        timer.finish(
            "success",
            "durable_analysis_payload_reused_and_delivered"
            if reused else "analysis_generated_and_delivered",
        )
        log(f"разбор для recovery {recovery_date} отправлен")
        try:
            deliver_weekly_if_due(context)
        except Exception as exc:
            log(
                "еженедельный отчёт пока не отправлен: "
                f"{type(exc).__name__}"
            )
        return True
    except Exception as exc:
        morning_context.release_analysis(recovery_date)
        timer.finish(
            "failed",
            morning_observability.safe_exception_reason(
                exc, prefix="analysis_generation_or_delivery"
            ),
        )
        log(
            f"разбор для recovery {recovery_date} не готов: "
            f"{type(exc).__name__}"
        )
        return False


def deliver_context_prompt(
    recovery_date, now=None, context_only=False, run_id=None
):
    """Deliver once after WHOOP readiness, or at the documented fallback cutoff.

    SQLite claims make overlapping cron invocations and recovery after a
    temporary Telegram/service outage duplicate-safe.  A failed delivery
    releases its claim so the next 15-minute cron invocation performs catch-up.
    """
    prompt_run_id = run_id or morning_observability.new_run_id(
        "scheduled_prompt"
    )
    claim_time = now or dt.datetime.now(TZ)
    candidate_timer = morning_observability.StageTimer.start(
        recovery_date, prompt_run_id, "prompt_candidate_created"
    )
    try:
        context, needs_question = morning_context.ensure_request(recovery_date)
    except Exception as exc:
        candidate_timer.finish(
            "failed",
            morning_observability.safe_exception_reason(
                exc, prefix="prompt_candidate_persistence"
            ),
        )
        _record_once(
            recovery_date, prompt_run_id, "prompt_delivered", "skipped",
            "blocked_by:prompt_candidate_persistence",
        )
        return False
    candidate_timer.finish(
        "success" if needs_question else "skipped",
        "candidate_created"
        if needs_question else f"candidate_exists:status={context['status']}",
    )
    delivery_timer = morning_observability.StageTimer.start(
        recovery_date, prompt_run_id, "prompt_delivered"
    )
    claimed = morning_context.claim_question(recovery_date, now=claim_time)
    if claimed:
        question_message_id = send_telegram(context_question(claimed, context_only=context_only))
        if question_message_id is not None:
            if morning_context.mark_question_sent(recovery_date, question_message_id):
                delivery_timer.finish("success", "telegram_prompt_delivered")
                log(f"запрошен контекст дня {claimed['evening_date']}")
                return True
            else:
                delivery_timer.finish(
                    "failed",
                    "prompt_state_persistence_failed_after_telegram_accept",
                )
                log(f"вопрос для recovery {recovery_date} отправлен, но его Telegram ID не сохранён")
        else:
            morning_context.release_question_claim(recovery_date)
            reason = (
                _last_telegram_reason
                if _last_telegram_reason != "telegram_not_attempted"
                else "telegram_delivery_unconfirmed"
            )
            delivery_timer.finish("failed", reason)
        return False

    if context.get("asked_at") and context.get("question_message_id"):
        delivery_timer.finish("success", "prompt_already_delivered")
    else:
        delivery_timer.finish("waiting", "prompt_claim_not_available")

    reminder = morning_context.reminder_due(recovery_date, now=claim_time)
    if reminder and send_telegram(context_reminder(reminder)):
        morning_context.mark_reminder_sent(recovery_date)
        log(f"отправлено напоминание про контекст дня {reminder['evening_date']}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="совместимо с ручным /sync")
    args = parser.parse_args()
    now = current_time()
    today = now.date()
    run_id = morning_observability.new_run_id("cron")
    morning_observability.record_stage(
        today, run_id, "cron_started", "success",
        "scheduled_morning_invocation_started",
    )

    # Once today's complete WHOOP result has produced its durable question,
    # another provider fetch cannot improve that workflow. Avoid rotating the
    # one-time refresh-token chain every 15 minutes for the rest of the day.
    existing_status = morning_readiness.morning_data_status(
        today, db_path=DB_PATH,
    )
    existing_request = morning_context.request_for_date(today)
    fetch_already_complete = bool(
        not args.force
        and existing_status["ready"]
        and existing_request
        and existing_request.get("asked_at")
        and existing_request.get("question_message_id")
    )
    if fetch_already_complete:
        reason = "current_morning_workflow_already_materialized"
        _record_once(today, run_id, "whoop_refresh_attempted", "skipped", reason)
        _record_once(today, run_id, "whoop_refresh_result", "skipped", reason)
        _record_once(
            today, run_id, "recovery_imported", "success",
            "current_morning_recovery_already_present",
        )
        _record_once(
            today, run_id, "sleep_imported", "success",
            "current_morning_sleep_already_present",
        )
        fetch_result = ScriptResult(True, reason, 0, 0)
    else:
        fetch_result = run_script(
            "fetch_data.py", "--days", "2",
            pipeline_run_id=run_id, pipeline_date=today,
        )
    if not fetch_result:
        failure = morning_observability.latest_failure(run_id)
        reason = (
            f"{failure['stage']}:{failure['reason']}"
            if failure else getattr(
                fetch_result, "reason", "fetch_subprocess_failed_without_detail"
            )
        )
        _record_once(
            today, run_id, "whoop_refresh_attempted", "failed", reason
        )
        _record_once(
            today, run_id, "whoop_refresh_result", "skipped",
            f"blocked_by:{reason}",
        )
        _block_unrecorded(
            today, run_id,
            (
                "recovery_imported", "sleep_imported", "dashboard_rebuilt",
                "prompt_candidate_created", "prompt_delivered",
                "analysis_generated",
            ),
            reason,
        )
        log(f"morning pipeline stopped at WHOOP fetch: {reason}")
        return 1

    child_stages = (
        "whoop_refresh_attempted",
        "whoop_refresh_result",
        "recovery_imported",
        "sleep_imported",
    )
    missing_child_stages = [
        stage for stage in child_stages
        if not morning_observability.stage_was_recorded(run_id, stage)
    ]
    if missing_child_stages:
        first_missing = missing_child_stages[0]
        reason = f"child_observability_event_missing:{first_missing}"
        _record_once(today, run_id, first_missing, "failed", reason)
        for stage in missing_child_stages[1:]:
            _record_once(
                today, run_id, stage, "skipped", f"blocked_by:{reason}"
            )
        _block_unrecorded(
            today, run_id,
            (
                "dashboard_rebuilt", "prompt_candidate_created",
                "prompt_delivered", "analysis_generated",
            ),
            reason,
        )
        log(f"morning pipeline stopped: {reason}")
        return 1

    status = morning_readiness.morning_data_status(today, db_path=DB_PATH)
    if status["error"]:
        reason = f"canonical_read:{status['error']}"
        _record_once(today, run_id, "recovery_imported", "failed", reason)
        _record_once(today, run_id, "sleep_imported", "failed", reason)
        _block_unrecorded(
            today, run_id,
            (
                "dashboard_rebuilt", "prompt_candidate_created",
                "prompt_delivered", "analysis_generated",
            ),
            reason,
        )
        log(f"morning pipeline stopped at canonical read: {reason}")
        return 1

    _record_once(
        today, run_id, "recovery_imported",
        "success" if status["recovery"] else "waiting",
        "current_morning_recovery_present"
        if status["recovery"] else "current_morning_recovery_absent",
    )
    _record_once(
        today, run_id, "sleep_imported",
        "success" if status["sleep"] else "waiting",
        "current_morning_sleep_present"
        if status["sleep"] else "current_morning_sleep_absent",
    )
    morning_ready = status["ready"]
    fallback_due = (
        morning_readiness.analysis_mode(today, now=now, db_path=DB_PATH)
        == "context_only"
    )
    if morning_ready:
        dashboard_timer = morning_observability.StageTimer.start(
            today, run_id, "dashboard_rebuilt"
        )
        dashboard_result = run_script(
            "build_dashboard.py",
            pipeline_run_id=run_id, pipeline_date=today,
        )
        if not dashboard_result:
            reason = getattr(
                dashboard_result, "reason",
                "dashboard_subprocess_failed_without_detail",
            )
            dashboard_timer.finish("failed", reason)
            _block_unrecorded(
                today, run_id,
                (
                    "prompt_candidate_created", "prompt_delivered",
                    "analysis_generated",
                ),
                f"dashboard_rebuilt:{reason}",
            )
            log(f"morning pipeline stopped at dashboard rebuild: {reason}")
            return 1
        dashboard_timer.finish("success", "dashboard_build_completed")
    elif fallback_due:
        _record_once(
            today, run_id, "dashboard_rebuilt", "skipped",
            "context_only_cutoff_without_current_whoop",
        )
    else:
        reason = "current_morning_recovery_or_sleep_not_available"
        _block_unrecorded(
            today, run_id,
            (
                "dashboard_rebuilt", "prompt_candidate_created",
                "prompt_delivered",
            ),
            reason,
        )
        _record_once(
            today, run_id, "analysis_generated", "waiting",
            "current_morning_data_not_ready",
        )
        log(f"complete Recovery/Sleep for {today} is not available yet")
        return 0

    if args.dry_run:
        _block_unrecorded(
            today, run_id,
            (
                "prompt_candidate_created", "prompt_delivered",
                "analysis_generated",
            ),
            "dry_run",
        )
        log("[dry-run] no Telegram prompt or analysis delivery")
        return 0

    # The prompt is a consequence of complete canonical morning data, not a
    # fixed clock time. Repeated cron runs provide catch-up without duplicates.
    if morning_ready or fallback_due:
        if not deliver_context_prompt(
            today, now=now, context_only=fallback_due and not morning_ready,
            run_id=run_id,
        ):
            _block_unrecorded(
                today, run_id, ("analysis_generated",),
                "prompt_delivery_failed",
            )
            return 1

    # Analysis retries are independent from today's fetch. This also drains
    # older answered rows after data arrives or the 23:00 Kyiv cutoff passes.
    morning_context.recover_stale_analysis_claims(now=now)
    for context in morning_context.answered_contexts():
        mode = morning_readiness.analysis_mode(
            context["recovery_date"], now=now, db_path=DB_PATH
        )
        if not mode:
            continue
        claimed = morning_context.claim_analysis(context["recovery_date"], mode, now=now)
        if claimed:
            if not deliver_claimed_analysis(claimed, run_id=run_id):
                return 1

    for context in morning_context.analyzed_contexts():
        try:
            deliver_weekly_if_due(context)
        except Exception as exc:
            log(
                "еженедельный отчёт пока не отправлен: "
                f"{type(exc).__name__}"
            )

    if not morning_observability.stage_was_recorded(
        run_id, "analysis_generated"
    ):
        current = morning_context.ensure_request(today)[0]
        if current["status"] == morning_context.STATUS_ANALYZED:
            _record_once(
                today, run_id, "analysis_generated", "success",
                "analysis_already_completed",
            )
        elif current["status"] in {
            morning_context.STATUS_ANSWERED,
            morning_context.STATUS_ANALYZING,
        }:
            _record_once(
                today, run_id, "analysis_generated", "waiting",
                f"analysis_state:{current['status']}",
            )
        else:
            _record_once(
                today, run_id, "analysis_generated", "waiting",
                "context_reply_not_received",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
