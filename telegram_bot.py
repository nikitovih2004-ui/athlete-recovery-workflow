import os
import sys
import json
import re
import base64
import datetime as dt
import sqlite3
import telebot
import requests
import workouts_db
import activity_corrections
import daily_log
import morning_context
import morning_observability
import morning_readiness
import morning_reporting
import report_delivery
import weekly_report
import conversation_router
import conversation_contract
import conversation_store
import conversation_tools
import conversation_validation
import strength_draft
import gemini_client
import phase2_flags
import phase2_store
import factor_capture
import data_integrity
import hashlib
import threading
import cardio_extraction
import cardio_vision
import time
import weakref
from io import BytesIO
from PIL import Image, ImageEnhance, ImageOps

# Загружаем переменные окружения
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TG_USER = os.environ.get("TELEGRAM_USER_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
FALLBACK_MODELS = [
    item.strip() for item in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",")
    if item.strip()
]
VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", MODEL).strip()
VISION_FALLBACK_MODELS = [
    item.strip() for item in os.environ.get(
        "GEMINI_VISION_FALLBACK_MODELS",
        ",".join(FALLBACK_MODELS),
    ).split(",") if item.strip()
]

if not TG_TOKEN or not TG_CHAT:
    print("Ошибка: TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID должны быть настроены!")
    sys.exit(1)

bot = telebot.TeleBot(TG_TOKEN)
phase2_flags.validate_environment()


def router_enabled():
    """Read at call time so deploy/tests can toggle without reimport."""
    return phase2_flags.router_enabled()


def legacy_destructive_text_enabled():
    """Legacy keyword delete deletes data without confirmation, so it is gated
    behind its own disabled-by-default flag (contract section 12)."""
    return phase2_flags.legacy_destructive_text_enabled()


def is_authorized_message(message):
    """Single authorization policy for commands, photos and text updates."""
    chat = getattr(message, "chat", None)
    user = getattr(message, "from_user", None)
    if str(getattr(chat, "id", "")) != TG_CHAT:
        return False
    chat_type = getattr(chat, "type", None)
    if chat_type is not None and chat_type != "private":
        return False
    if TG_USER and str(getattr(user, "id", "")) != TG_USER:
        return False
    return True


def _build_gemini_client():
    return gemini_client.GeminiClient(
        api_key=GEMINI_KEY, model=MODEL, fallback_models=FALLBACK_MODELS
    )


from zoneinfo import ZoneInfo

def get_kiev_time():
    """Возвращает текущее время в Киеве с учетом зимнего/летнего времени."""
    return dt.datetime.now(ZoneInfo("Europe/Kyiv"))

def get_target_date_for_supplements():
    """
    Если БАДы отправлены утром (до 13:00 по Киеву), относим их к предыдущему дню (вчера),
    так как это БАДы, принятые перед сном этой ночью.
    Если отправлены позже — относим к сегодняшнему дню.
    """
    now_kiev = get_kiev_time()
    if now_kiev.hour < 13:
        return (now_kiev - dt.timedelta(days=1)).date().isoformat()
    return now_kiev.date().isoformat()

def _gemini_models():
    """Primary model first, then configured fallbacks without duplicates."""
    seen = set()
    for candidate in [MODEL, *FALLBACK_MODELS]:
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _vision_models():
    seen = set()
    for candidate in [VISION_MODEL, *VISION_FALLBACK_MODELS]:
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate


class ProviderCallError(RuntimeError):
    def __init__(self, category, *, model=None, attempt_count=0, latency_ms=None,
                 status_code=None):
        super().__init__(category)
        self.category = category
        self.model = model
        self.attempt_count = attempt_count
        self.latency_ms = latency_ms
        self.status_code = status_code


def _payload_for_model(payload, model_name):
    """Apply Gemini 3 thinking controls without deprecated sampling fields."""
    configured = dict(payload)
    generation = dict(payload.get("generationConfig") or {})
    if model_name.startswith("gemini-3"):
        generation.pop("temperature", None)
        generation.pop("topP", None)
        generation.pop("topK", None)
        generation["thinkingConfig"] = {"thinkingLevel": "low"}
    else:
        generation.pop("thinkingConfig", None)
    configured["generationConfig"] = generation
    return configured


def _generate_content(payload, timeout, response_validator=None):
    """Call primary/fallback and classify transport, rejection and malformed output."""
    started = time.monotonic()
    attempt_count = 0
    last_model = None
    last_validation = None
    saw_rejected = False
    rejected_status = None
    malformed_model = None
    for model_name in _gemini_models():
        attempt_count += 1
        last_model = model_name
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        try:
            response = requests.post(
                url,
                json=_payload_for_model(payload, model_name),
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
                timeout=timeout,
            )
            if response.status_code != 200:
                if response.status_code in {400, 401, 403, 404, 413}:
                    saw_rejected = True
                    rejected_status = response.status_code
                continue
            body = response.json()
            if response_validator is None:
                return body, model_name
            try:
                value, response_text = response_validator(body)
            except cardio_extraction.CardioExtractionError as exc:
                exc.model = model_name
                exc.attempt_count = attempt_count
                exc.latency_ms = int((time.monotonic() - started) * 1000)
                last_validation = exc
                continue
            return value, model_name, {
                "attempt_count": attempt_count,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "response_sha256": hashlib.sha256(
                    response_text.encode("utf-8")
                ).hexdigest(),
            }
        except requests.exceptions.RequestException:
            continue
        except (TypeError, ValueError):
            malformed_model = model_name
            continue
    if last_validation is not None:
        last_validation.attempt_count = attempt_count
        last_validation.latency_ms = int((time.monotonic() - started) * 1000)
        raise last_validation
    if malformed_model is not None:
        malformed = cardio_extraction.CardioExtractionError("malformed")
        malformed.model = malformed_model
        malformed.attempt_count = attempt_count
        malformed.latency_ms = int((time.monotonic() - started) * 1000)
        raise malformed
    raise ProviderCallError(
        "provider_rejected" if saw_rejected else "provider_unavailable",
        model=last_model, attempt_count=attempt_count,
        latency_ms=int((time.monotonic() - started) * 1000),
        status_code=rejected_status,
    )


def _image_mime_type(photo_bytes):
    if photo_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if photo_bytes.startswith(b"GIF87a") or photo_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if photo_bytes.startswith(b"RIFF") and photo_bytes[8:12] == b"WEBP":
        return "image/webp"
    if photo_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _prepare_cardio_image(photo_bytes):
    """Strip metadata and normalize Telegram bytes to a bounded JPEG."""
    if not isinstance(photo_bytes, bytes) or not photo_bytes:
        raise cardio_extraction.CardioExtractionError("image_decode")
    if len(photo_bytes) > 8 * 1024 * 1024:
        raise cardio_extraction.CardioExtractionError("image_size")
    try:
        with Image.open(BytesIO(photo_bytes)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            width, height = image.size
            if width * height > 20_000_000:
                raise cardio_extraction.CardioExtractionError("image_size")
            longest = max(width, height)
            if longest > 2048:
                scale = 2048 / longest
                image = image.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            elif longest < 1200:
                scale = min(2.0, 1200 / max(1, longest))
                image = image.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
                image = ImageEnhance.Sharpness(image).enhance(1.5)
            output = BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
            return output.getvalue(), "image/jpeg"
    except Exception as exc:
        raise cardio_extraction.CardioExtractionError("image_decode") from exc

def parse_with_gemini(text):
    """Использует Gemini для парсинга свободного текста тренировки/БАДов в JSON."""
    if not GEMINI_KEY:
        return {"workouts": [], "supplements": [], "date": None}
    
    now_kiev = get_kiev_time()
    today_str = now_kiev.date().isoformat()
    yesterday_str = (now_kiev - dt.timedelta(days=1)).date().isoformat()
    
    prompt = (
        "Распарси следующее сообщение о тренировке или приеме БАДов в структурированный JSON.\n"
        "Сообщение может содержать описание выполненных упражнений (вес, подходы, повторения) и/или принятых добавок (название, дозировка).\n\n"
        "ВАЖНО: Если в упражнении несколько подходов с разным количеством повторений (например, '30х9, 30х8' или 'жим 100кг 5, 5, 4'), "
        "верни КАЖДЫЙ подход как отдельный элемент в массиве \"workouts\" со свойствами:\n"
        "- \"name\": название упражнения на русском\n"
        "- \"weight\": вес в кг (число, или null)\n"
        "- \"reps\": количество повторений в этом конкретном подходе (число, или null)\n\n"
        "Если в тексте есть БАДы/добавки, верни их в массиве \"supplements\" со свойствами:\n"
        "- \"name\": название добавки на русском\n"
        "- \"dosage\": дозировка (строка, например \"5г\", \"400мг\", или null)\n\n"
        "ЦЕЛЕВАЯ ДАТА (свойство \"date\"):\n"
        f"Текущая дата (сегодня): {today_str}. Вчерашняя дата: {yesterday_str}.\n"
        "Если в сообщении прямо указано, что это запись за вчера (например, 'вчера', 'за вчера') или за конкретное число (например '09.07'), "
        "вычисли эту дату и верни её в поле \"date\" в формате YYYY-MM-DD. Если указаний нет, верни null в поле \"date\".\n\n"
        "Формат ответа должен быть строго валидным JSON без разметки markdown (без ```json). "
        "Не добавляй никаких пояснений, только JSON.\n"
        "Если в тексте ничего не найдено, верни: {\"workouts\": [], \"supplements\": [], \"date\": null}.\n\n"
        f"Текст для разбора:\n{text}"
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1}
    }
    
    try:
        res, _used_model = _generate_content(payload, timeout=45)
        raw_text = res["candidates"][0]["content"]["parts"][0]["text"].strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"Ошибка парсинга через Gemini: {e}")
        return {"workouts": [], "supplements": [], "date": None}

def parse_photo_with_gemini(photo_bytes):
    """Мультимодальный запрос к Gemini для OCR скриншотов кардио с извлечением пульсовых зон."""
    if not GEMINI_KEY:
        return None
        
    img_b64 = base64.b64encode(photo_bytes).decode("utf-8")
    mime_type = _image_mime_type(photo_bytes)
    
    prompt = (
        "Перед тобой скриншот кардио-тренировки (с фитнес-браслета, часов или экрана тренажера).\n"
        "Твоя задача — извлечь показатели тренировки и вернуть их строго в формате JSON со следующими свойствами:\n"
        "- \"type\": тип активности на русском языке (например, \"Бег\", \"Ходьба\", \"Велосипед\", \"Эллипс\", \"Гребля\", если не понятно - напиши \"Кардио\")\n"
        "- \"duration\": общая продолжительность в минутах (число, округли до десятых)\n"
        "- \"distance\": дистанция в км (число, или null)\n"
        "- \"avg_hr\": средний пульс в уд/мин (число, или null)\n"
        "- \"calories\": сожженные калории в ккал (число, или null)\n"
        "- \"zone_0\": время в Zone 0 (пульс восстановления / <108 BPM, строка вида 'MM:SS' или 'H:MM:SS', либо null если нет)\n"
        "- \"zone_1\": время в Zone 1 (строка вида 'MM:SS' или 'H:MM:SS', либо null)\n"
        "- \"zone_2\": время в Zone 2 (строка вида 'MM:SS' или 'H:MM:SS', либо null)\n"
        "- \"zone_3\": время в Zone 3 (строка вида 'MM:SS' or 'H:MM:SS', либо null)\n"
        "- \"zone_4\": время в Zone 4 (строка вида 'MM:SS' or 'H:MM:SS', либо null)\n"
        "- \"zone_5\": время в Zone 5 (максимальный пульс / >176 BPM, строка вида 'MM:SS' или 'H:MM:SS', либо null)\n\n"
        "Ответ должен быть строго в формате JSON без разметки markdown (без ```json). Не добавляй никаких пояснений, только JSON."
    )
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": img_b64
                        }
                    }
                ]
            }
        ],
        "generationConfig": {"temperature": 0.1}
    }
    
    try:
        res, _used_model = _generate_content(payload, timeout=90)
        raw_text = res["candidates"][0]["content"]["parts"][0]["text"].strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"Ошибка распознавания фото через Gemini: {e}")
        return None

def handle_delete_command(text, chat_id, message_id=None, user_id=None):
    """
    Обрабатывает команды удаления записей по ключевым словам.
    Например: 'удали контекст дня', 'очисти тренировку за вчера'.
    """
    now_kiev = get_kiev_time()
    today_str = now_kiev.date().isoformat()
    yesterday_str = (now_kiev - dt.timedelta(days=1)).date().isoformat()
    
    # По умолчанию определяем целевую дату
    # Если операция ночью (до 5 утра), относим по умолчанию к вчерашнему дню
    target_date = yesterday_str if now_kiev.hour < 5 else today_str
    
    text_lower = text.lower()
    
    if "вчера" in text_lower:
        target_date = yesterday_str
    elif "сегодня" in text_lower:
        target_date = today_str
        
    delete_ctx = conversation_store.ActionContext(
        source="telegram-legacy-delete", chat_id=chat_id,
        message_id=message_id or f"{target_date}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
        user_id=user_id, input_text=text,
    )
    reservation = conversation_store.reserve(delete_ctx)
    if not reservation.is_new:
        if reservation.in_progress:
            bot.send_message(chat_id, "Очистка уже выполняется.")
        elif reservation.response_text:
            bot.send_message(chat_id, reservation.response_text, parse_mode="Markdown")
        return True
    conversation_store.record_validated(
        reservation.action_id, intent="legacy_soft_delete",
        tool_name="soft_delete_activity",
        validated_arguments={"target_date": target_date},
        processing_token=reservation.processing_token,
        processing_fence=reservation.processing_fence,
    )
    conn = workouts_db.connect()
    deleted_something = False
    msg = []
    deleted_ids = []
    try:
        phase2_store.migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        deleted_something, msg, deleted_ids = _run_delete_command_tx(
            conn, text_lower, target_date, reservation, deleted_something, msg, deleted_ids,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if deleted_something:
        response = "🗑️ *Джарвис выполнил очистку:*\n" + "\n".join(msg)
        conversation_store.record_response(reservation.action_id, "confirmation", response)
        bot.send_message(chat_id, response, parse_mode="Markdown")
        return True
    bot.send_message(chat_id, f"За *{target_date}* активных записей для очистки нет.", parse_mode="Markdown")
    return True


def _run_delete_command_tx(conn, text_lower, target_date, reservation,
                            deleted_something, msg, deleted_ids):
    """Runs inside the caller's BEGIN IMMEDIATE transaction; caller commits/rolls back."""
    # 1. Удаление daily context / legacy notes
    if "контекст" in text_lower or "вечерн" in text_lower or "заметк" in text_lower or "лог" in text_lower and not ("трениров" in text_lower or "бад" in text_lower):
        entries = conn.execute(
            "SELECT entry_id FROM daily_context_entries WHERE context_date=? AND status='active'",
            (target_date,),
        ).fetchall()
        for row in entries:
            daily_log.retract_entry_tx(
                conn, row[0], origin_action_id=reservation.action_id
            )
            deleted_ids.append(f"context:{row[0]}")
        if entries:
            msg.append(f"📝 Daily context за *{target_date}* скрыт из актуальных данных.")
            deleted_something = True
        
    # 2. Удаление тренировок (силовых и кардио)
    if "трениров" in text_lower or "кардио" in text_lower or "силов" in text_lower or "упражнен" in text_lower:
        for entity_type, table in (("strength", "workout_exercises"), ("cardio", "cardio_exercises")):
            rows = conn.execute(
                f"SELECT id FROM {table} WHERE date=? AND deleted_at IS NULL", (target_date,)
            ).fetchall()
            for row in rows:
                if workouts_db.soft_delete_activity(
                    conn, entity_type, row[0], deleted_by_action_id=reservation.action_id,
                    reason="legacy_explicit_delete_command",
                ):
                    deleted_ids.append(f"{entity_type}:{row[0]}")
        if any(item.startswith(("strength:", "cardio:")) for item in deleted_ids):
            msg.append(f"🏋️‍♂️ Силовые и кардио за *{target_date}* скрыты из актуальных данных.")
            deleted_something = True
        
    # 3. Удаление БАДов
    if "бад" in text_lower or "добавк" in text_lower:
        rows = conn.execute(
            "SELECT id FROM supplements_log WHERE date=? AND deleted_at IS NULL", (target_date,)
        ).fetchall()
        for row in rows:
            if workouts_db.soft_delete_activity(
                conn, "supplement", row[0], deleted_by_action_id=reservation.action_id,
                reason="legacy_explicit_delete_command",
            ):
                deleted_ids.append(f"supplement:{row[0]}")
        if rows:
            msg.append(f"💊 БАДы за *{target_date}* скрыты из актуальных данных.")
            deleted_something = True
        
    # 4. Полная очистка дня
    if ("все" in text_lower or "всё" in text_lower or "очист" in text_lower) and not deleted_something:
        for entity_type, table in (("strength", "workout_exercises"),
                                   ("cardio", "cardio_exercises"),
                                   ("supplement", "supplements_log")):
            rows = conn.execute(
                f"SELECT id FROM {table} WHERE date=? AND deleted_at IS NULL", (target_date,)
            ).fetchall()
            for row in rows:
                if workouts_db.soft_delete_activity(
                    conn, entity_type, row[0], deleted_by_action_id=reservation.action_id,
                    reason="legacy_explicit_delete_command",
                ):
                    deleted_ids.append(f"{entity_type}:{row[0]}")
        entries = conn.execute(
            "SELECT entry_id FROM daily_context_entries WHERE context_date=? AND status='active'",
            (target_date,),
        ).fetchall()
        for row in entries:
            daily_log.retract_entry_tx(conn, row[0], origin_action_id=reservation.action_id)
            deleted_ids.append(f"context:{row[0]}")
        msg.append(f"🗑️ Все актуальные логи за *{target_date}* скрыты.")
        deleted_something = bool(deleted_ids)

    projection = daily_log.projection_state(conn, target_date)
    if projection is not None and any(item.startswith("context:") for item in deleted_ids):
        phase2_store.enqueue_factor_job(
            conn, context_date=target_date,
            projection_hash=projection["projection_hash"],
            projection_revision=projection["revision"],
            extractor_version=factor_capture.SCHEMA_VERSION,
            origin_action_id=reservation.action_id,
            enabled=phase2_flags.factor_capture_enabled(),
        )
    result = {
        "status": "success", "action_id": reservation.action_id,
        "created_count": len(deleted_ids), "record_ids": deleted_ids,
        "resolved_date": target_date, "data": {"soft_deleted_count": len(deleted_ids)},
    }
    conversation_store.finalize_success_tx(
        conn, reservation.action_id, tool_name="soft_delete_activity",
        validated_arguments={"target_date": target_date}, result=result,
        processing_token=reservation.processing_token,
        processing_fence=reservation.processing_fence,
    )
    return deleted_something, msg, deleted_ids

def send_plain_chunks(chat_id, text):
    """Отправляет длинный отчёт без Markdown-парсинга и без обрезания текста."""
    try:
        chunks = morning_reporting.split_telegram_text(text)
        if not chunks:
            return False
        for chunk in chunks:
            bot.send_message(chat_id, chunk, disable_web_page_preview=True)
        return True
    except Exception as exc:
        print(f"telegram_chunk_send_failed error={type(exc).__name__}")
        return False


def send_conversation_outcome(chat_id, outcome):
    """Persist and deliver one replayable, user-safe conversation response."""
    if outcome.kind == "in_progress":
        # This is a transient observation from a duplicate delivery, not the
        # action's terminal response. Persisting it would occupy response_text
        # and suppress the later success confirmation from the lease owner.
        return bot.send_message(chat_id, outcome.message) if outcome.message else None
    try:
        conversation_store.record_response(outcome.action_id, outcome.kind, outcome.message)
    except Exception as exc:
        print(f"conversation_response_persist_failed action_id={outcome.action_id} error={type(exc).__name__}")
        return None

    if not outcome.message:
        return None
    try:
        claimed = conversation_store.claim_response_delivery(outcome.action_id)
    except Exception as exc:
        print(f"conversation_response_claim_failed action_id={outcome.action_id} error={type(exc).__name__}")
        return None
    if not claimed:
        return None
    try:
        sent = bot.send_message(chat_id, outcome.message)
    except Exception as exc:
        conversation_store.mark_response_delivery(outcome.action_id, delivered=False)
        if outcome.kind == "clarification" and outcome.pending_id:
            pending = conversation_store.get_pending(outcome.pending_id)
            if not pending or pending.get("candidate_intent") != conversation_contract.INTENT_START_STRENGTH_DRAFT:
                conversation_store.cancel_undelivered_pending(outcome.pending_id)
        print(
            f"conversation_reply_failed action_id={outcome.action_id} "
            f"error={type(exc).__name__}"
        )
        return None
    message_id = getattr(sent, "message_id", None)
    conversation_store.mark_response_delivery(
        outcome.action_id, delivered=True, message_id=message_id
    )
    if outcome.kind == "clarification" and outcome.pending_id and message_id is not None:
        conversation_store.set_clarification_message_id(outcome.pending_id, message_id)
    return sent


def parse_photo_cardio_contract(photo_bytes, caption=None):
    """Original bytes -> Gemini Vision typed call -> Python validation."""
    try:
        mime_type = _image_mime_type(photo_bytes)
        if mime_type is None:
            raise cardio_extraction.CardioExtractionError("image_mime")
        return cardio_vision.extract(
            photo_bytes, mime_type, caption,
            api_key=GEMINI_KEY, models=list(_vision_models()),
        )
    except cardio_vision.VisionProviderError as exc:
        wrapped = cardio_extraction.CardioExtractionError(
            exc.category, getattr(exc, "partial", None)
        )
        wrapped.latency_ms = exc.latency_ms
        wrapped.model = exc.model
        wrapped.attempt_count = exc.attempt_count
        wrapped.status_code = exc.status_code
        raise wrapped from exc


def _message_attachment(message):
    photos = list(getattr(message, "photo", None) or [])
    if photos:
        item = photos[-1]
        return item.file_id, None, "photo"
    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "")
        if not mime.startswith("image/"):
            raise cardio_extraction.CardioExtractionError("image_mime")
        return document.file_id, mime, "document"
    raise cardio_extraction.CardioExtractionError("image_decode")


def _photo_action(message):
    file_id, _mime, kind = _message_attachment(message)
    ctx = conversation_store.ActionContext(
        source="telegram-photo", chat_id=message.chat.id,
        user_id=getattr(getattr(message, "from_user", None), "id", None),
        message_id=message.message_id,
        input_text=f"{kind}:{file_id}",
    )
    return ctx, conversation_store.reserve(ctx)


def _telegram_target_date(message, now_kiev):
    caption = str(getattr(message, "caption", "") or "").casefold()
    stamp = getattr(message, "date", None)
    if isinstance(stamp, dt.datetime):
        instant = stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)
        message_date = instant.astimezone(ZoneInfo("Europe/Kyiv")).date()
    elif isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        message_date = dt.datetime.fromtimestamp(
            stamp, dt.timezone.utc
        ).astimezone(ZoneInfo("Europe/Kyiv")).date()
    else:
        message_date = now_kiev.date()
    if "вчера" in caption:
        return (message_date - dt.timedelta(days=1)).isoformat()
    if "сегодня" in caption:
        return message_date.isoformat()
    absolute = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", caption)
    if absolute:
        return absolute.group(1)
    return message_date.isoformat()


def _cardio_reply_value(missing, text):
    lowered = (text or "").casefold().strip()
    if missing == "activity_type":
        for marker, value in (
            ("ход", "walking"), ("walk", "walking"), ("бег", "running"),
            ("run", "running"), ("вело", "cycling"), ("cardio", "cardio"),
            ("кардио", "cardio"), ("эллип", "elliptical"), ("греб", "rowing"),
        ):
            if marker in lowered:
                return value
        return None
    if missing == "duration":
        match = re.search(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)", lowered)
        if match:
            hours = int(match.group(1) or 0)
            return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мин|minute)", lowered)
        return round(float(match.group(1).replace(",", ".")) * 60) if match else None
    if missing == "effort_metric":
        match = re.search(r"(?:пульс|чсс|avg\s*hr)\D{0,12}(\d{2,3})", lowered)
        if match:
            return ("avg_hr_bpm", int(match.group(1)))
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ккал|kcal|калори)", lowered)
        if match:
            return ("calories_kcal", float(match.group(1).replace(",", ".")))
        match = re.search(r"(?:strain|стрейн|нагрузк)\D{0,12}(\d+(?:[.,]\d+)?)", lowered)
        if match:
            return ("strain", float(match.group(1).replace(",", ".")))
    return None


def _resolve_photo_pending(pending, ctx, exec_ctx):
    reservation = conversation_store.reserve(ctx)
    action_id = reservation.action_id
    exec_ctx.action_id = action_id
    exec_ctx.processing_token = reservation.processing_token
    exec_ctx.processing_fence = reservation.processing_fence
    lease = {
        "processing_token": reservation.processing_token,
        "processing_fence": reservation.processing_fence,
    }
    if not reservation.is_new:
        result = json.loads(reservation.result_json) if reservation.result_json else None
        return conversation_router.RouterOutcome(
            action_id, "in_progress" if reservation.in_progress else "duplicate",
            "Запись уже обрабатывается." if reservation.in_progress
            else reservation.response_text, result=result, write_committed=bool(result),
        )
    if not conversation_store.claim_pending_for_resolution(pending["pending_id"], action_id):
        conn = conversation_store.connect()
        try:
            owned = conn.execute(
                """SELECT 1 FROM pending_actions
                   WHERE pending_id=? AND status=? AND claimed_by_action_id=?""",
                (pending["pending_id"], conversation_contract.PENDING_RESOLVING,
                 action_id),
            ).fetchone() is not None
        finally:
            conn.close()
        if not owned:
            conversation_store.mark_noop(
                action_id, conversation_contract.INTENT_LOG_CARDIO, **lease
            )
            return conversation_router.RouterOutcome(
                action_id, "general", "Это уточнение уже обработано."
            )
    partial = json.loads(pending["partial_arguments_json"])
    missing = json.loads(pending["missing_fields_json"])[0]
    value = _cardio_reply_value(missing, ctx.input_text)
    if value is None:
        conversation_store.record_router(
            action_id, model="deterministic-cardio-reply-v1",
            response_sha256=None, intent=conversation_contract.INTENT_LOG_CARDIO,
            confidence=1.0, latency_ms=0, attempt_count=0,
            prompt_version="photo-cardio-clarification-v1", **lease,
        )
        conversation_store.mark_clarification(
            action_id, conversation_contract.INTENT_LOG_CARDIO, 1.0, **lease
        )
        conversation_store.release_pending_claim(pending["pending_id"], action_id)
        questions = {
            "activity_type": "Ответь типом активности: ходьба, бег, вело или кардио.",
            "duration": "Напиши длительность в формате 28:43 или числом минут.",
            "effort_metric": "Напиши средний пульс или калории, например «пульс 136».",
        }
        return conversation_router.RouterOutcome(
            action_id, "clarification", questions[missing],
            pending_id=pending["pending_id"],
        )
    if missing == "activity_type":
        partial["activity_type"] = value
        partial["activity_confidence"] = 1.0
    elif missing == "duration":
        partial["duration_seconds"] = value
        partial["duration_confidence"] = 1.0
    else:
        partial[value[0]] = value[1]
        partial["effort_confidence"] = 1.0
    target_date = partial.pop("_target_date")
    zones = partial.pop("_zones", {})
    partial.pop("_missing", None)
    try:
        parsed = cardio_extraction.validate(partial)
    except cardio_extraction.CardioExtractionError:
        conversation_store.mark_rejected(
            action_id, "photo_clarification_validation", None,
            intent=conversation_contract.INTENT_LOG_CARDIO, **lease,
        )
        conversation_store.resolve_pending(pending["pending_id"], action_id)
        return conversation_router.RouterOutcome(
            action_id, "rejected", conversation_router.NO_WRITE_MESSAGE
        )
    parsed.update(zones)
    args = {
        "date_ref": {"kind": "absolute", "value": target_date},
        "fact_status": conversation_contract.FACT_STATUS_COMPLETED,
        "activity_type": parsed["type"],
        "start_time": exec_ctx.local_now.strftime("%H:%M"),
        "duration_minutes": parsed["duration"],
        "distance_km": parsed.get("distance"),
        "avg_hr_bpm": parsed.get("avg_hr"),
        "calories_kcal": parsed.get("calories"),
        "strain": parsed.get("strain"),
        "max_hr_bpm": parsed.get("max_hr"),
        "steps": parsed.get("steps"),
        "hr_zone_minutes": [0.0] * 6,
    }
    verdict = conversation_validation.validate(
        conversation_validation.ValidationResult(
            ok=True, intent=conversation_contract.INTENT_LOG_CARDIO,
            confidence=parsed["confidence"], requires_confirmation=False,
            reply_text=None,
            arguments=args,
        ),
        local_now=exec_ctx.local_now,
    )
    if not verdict.ok or verdict.tool != "log_cardio":
        conversation_store.mark_rejected(
            action_id, verdict.error_code or "photo_clarification_validation",
            None, intent=conversation_contract.INTENT_LOG_CARDIO, **lease,
        )
        conversation_store.resolve_pending(pending["pending_id"], action_id)
        return conversation_router.RouterOutcome(
            action_id, "rejected", conversation_router.NO_WRITE_MESSAGE
        )
    try:
        conversation_store.record_router(
            action_id, model="deterministic-cardio-reply-v1",
            response_sha256=None, intent=conversation_contract.INTENT_LOG_CARDIO,
            confidence=parsed["confidence"], latency_ms=0, attempt_count=0,
            prompt_version="photo-cardio-clarification-v1", **lease,
        )
        conversation_store.record_validated(
            action_id, intent=verdict.intent, tool_name=verdict.tool,
            validated_arguments=verdict.arguments, **lease,
        )
        exec_ctx.pending_id = pending["pending_id"]
        result = conversation_tools.execute(verdict.tool, verdict.arguments, exec_ctx)
    except Exception as exc:
        conversation_store.release_pending_claim(pending["pending_id"], action_id)
        return conversation_router._tool_failure_outcome(
            action_id, verdict.tool, exc, lease
        )
    return conversation_router.RouterOutcome(
        action_id, "confirmation",
        conversation_router._confirm_message(
            verdict.intent, verdict.arguments, result, local_now=exec_ctx.local_now
        ),
        result=result, write_committed=result.get("created_count", 0) > 0,
    )


def _photo_zone_minutes(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        parts = [float(item) for item in str(value).strip().split(":")]
    except ValueError:
        return 0.0
    if len(parts) == 3:
        return parts[0] * 60 + parts[1] + parts[2] / 60
    if len(parts) == 2:
        return parts[0] + parts[1] / 60
    return parts[0] if len(parts) == 1 else 0.0


def _vision_pending_arguments(parsed, target_date):
    missing = parsed.get("needs_clarification")
    confidence = parsed.get("field_confidence") or {}
    effort_confidence = max(
        confidence.get("avg_hr_bpm", 0),
        confidence.get("calories_kcal", 0),
        confidence.get("strain", 0),
    )
    return {
        "activity_type": parsed.get("type"),
        "duration_seconds": (
            parsed["duration"] * 60 if parsed.get("duration") is not None else None
        ),
        "strain": parsed.get("strain"),
        "avg_hr_bpm": parsed.get("avg_hr"),
        "max_hr_bpm": parsed.get("max_hr"),
        "calories_kcal": parsed.get("calories"),
        "distance_km": parsed.get("distance"),
        "steps": parsed.get("steps"),
        "activity_confidence": confidence.get("activity_type", 0),
        "duration_confidence": confidence.get("duration", 0),
        "effort_confidence": effort_confidence,
        "_target_date": target_date,
        "_zones": {
            f"zone_{index}": parsed.get(f"zone_{index}") for index in range(6)
        },
        "_missing": missing,
    }


def persist_photo_cardio(message, parsed, target_date, now_kiev, reservation=None):
    """Validate and atomically persist OCR cardio through the audited tool path."""
    file_id, _mime, kind = _message_attachment(message)
    ctx = conversation_store.ActionContext(
        source="telegram-photo", chat_id=message.chat.id,
        user_id=getattr(getattr(message, "from_user", None), "id", None),
        message_id=message.message_id,
        input_text=f"{kind}:{file_id}",
    )
    reservation = reservation or conversation_store.reserve(ctx)
    if not reservation.is_new:
        if reservation.in_progress:
            return conversation_router.RouterOutcome(
                reservation.action_id, "in_progress",
                "Кардио уже обрабатывается; повторно его не запускаю.",
            )
        result = json.loads(reservation.result_json) if reservation.result_json else None
        return conversation_router.RouterOutcome(
            reservation.action_id, "duplicate", reservation.response_text,
            result=result, write_committed=bool(result),
        )
    raw_args = {
        "date_ref": {"kind": "absolute", "value": target_date},
        "fact_status": conversation_contract.FACT_STATUS_COMPLETED,
        "activity_type": parsed.get("type") or "Кардио",
        "start_time": now_kiev.strftime("%H:%M"),
        "duration_minutes": parsed.get("duration"),
        "distance_km": parsed.get("distance"),
        "avg_hr_bpm": parsed.get("avg_hr"),
        "calories_kcal": parsed.get("calories"),
        "strain": parsed.get("strain"),
        "max_hr_bpm": parsed.get("max_hr"),
        "steps": parsed.get("steps"),
        "hr_zone_minutes": [
            _photo_zone_minutes(parsed.get(f"zone_{index}")) for index in range(6)
        ],
    }
    envelope = conversation_validation.ValidationResult(
        ok=True, intent=conversation_contract.INTENT_LOG_CARDIO,
        confidence=parsed.get("confidence"),
        requires_confirmation=False, reply_text=None, arguments=raw_args,
    )
    verdict = conversation_validation.validate(envelope, local_now=now_kiev)
    exec_ctx = conversation_tools.ExecContext(
        action_id=reservation.action_id, source="telegram-photo",
        chat_id=str(message.chat.id), message_id=str(message.message_id),
        local_now=now_kiev, processing_token=reservation.processing_token,
        processing_fence=reservation.processing_fence,
    )
    lease = {
        "processing_token": reservation.processing_token,
        "processing_fence": reservation.processing_fence,
    }
    if not verdict.ok or verdict.tool != "log_cardio":
        conversation_store.mark_rejected(
            reservation.action_id, verdict.error_code or "invalid_photo_cardio",
            verdict.error_detail, intent=conversation_contract.INTENT_LOG_CARDIO,
            **lease,
        )
        return conversation_router.RouterOutcome(
            reservation.action_id, "rejected",
            "Не удалось безопасно проверить данные кардио; ничего не записано.",
        )
    try:
        conversation_store.record_validated(
            reservation.action_id, intent=verdict.intent, tool_name=verdict.tool,
            validated_arguments=verdict.arguments, **lease,
        )
        result = conversation_tools.execute(verdict.tool, verdict.arguments, exec_ctx)
    except Exception as exc:
        return conversation_router._tool_failure_outcome(
            reservation.action_id, "log_cardio", exc, lease,
        )
    return conversation_router.RouterOutcome(
        reservation.action_id, "confirmation",
        conversation_router._confirm_message(verdict.intent, verdict.arguments, result),
        result=result, write_committed=result.get("created_count", 0) > 0,
    )
def retry_pending_conversation_responses(limit=10):
    """Retry committed successful responses without rerunning Gemini or tools."""
    sources = ("telegram", "telegram-photo")
    try:
        missing = []
        for source in sources:
            remaining = limit - len(missing)
            if remaining <= 0:
                break
            missing.extend(conversation_store.missing_success_responses(
                source, TG_CHAT, limit=remaining
            ))
        for row in missing:
            try:
                result = json.loads(row["result_json"])
                arguments = json.loads(row["validated_arguments_json"] or "{}")
                intent = row["intent"]
                response = (
                    conversation_router._read_message(intent, result)
                    if intent in conversation_contract.READ_INTENTS
                    else conversation_router._confirm_message(intent, arguments, result)
                )
                conversation_store.record_response(
                    row["action_id"], "confirmation", response
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
                print(
                    f"conversation_response_recovery_failed "
                    f"action_id={row.get('action_id', 'unknown')}"
                )
        rows = []
        for source in sources:
            remaining = limit - len(rows)
            if remaining <= 0:
                break
            rows.extend(conversation_store.claim_retryable_responses(
                source, TG_CHAT, limit=remaining
            ))
    except (sqlite3.Error, ValueError):
        return 0
    delivered = 0
    for row in rows:
        try:
            sent = bot.send_message(row["chat_id"], row["response_text"])
        except Exception as exc:
            try:
                conversation_store.mark_response_delivery(
                    row["action_id"], delivered=False
                )
            except sqlite3.Error:
                print(f"conversation_retry_bookkeeping_failed action_id={row['action_id']}")
            print(
                f"conversation_retry_failed action_id={row['action_id']} "
                f"error={type(exc).__name__}"
            )
            continue
        try:
            conversation_store.mark_response_delivery(
                row["action_id"], delivered=True,
                message_id=getattr(sent, "message_id", None),
            )
        except sqlite3.Error:
            print(f"conversation_retry_bookkeeping_failed action_id={row['action_id']}")
            continue
        delivered += 1
    return delivered


_last_integrity_alert_signature = None
_last_integrity_alert_at = None


def run_data_integrity_monitor(now=None):
    """Continuously verify persisted user writes and emit a bounded alert."""
    global _last_integrity_alert_signature, _last_integrity_alert_at
    now = now or dt.datetime.now(dt.timezone.utc)
    absolute = os.path.abspath(daily_log.DB_PATH)
    probe = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True)
    try:
        installed = probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_meta'"
        ).fetchone()
    finally:
        probe.close()
    if installed is None:
        return True
    report = data_integrity.audit_database(absolute)
    if report["ok"]:
        _last_integrity_alert_signature = None
        _last_integrity_alert_at = None
        return True
    codes = sorted({item.get("code", "unknown") for item in report["issues"]})
    signature = "|".join(codes)
    should_alert = (
        signature != _last_integrity_alert_signature
        or _last_integrity_alert_at is None
        or now - _last_integrity_alert_at >= dt.timedelta(hours=1)
    )
    print(f"data_integrity_failed issue_count={len(report['issues'])} codes={','.join(codes)}")
    if should_alert:
        try:
            bot.send_message(
                TG_CHAT,
                "🚨 Проверка целостности пользовательских данных не пройдена. "
                f"Проблем: {len(report['issues'])}; коды: {', '.join(codes[:8])}.",
            )
        except Exception as exc:
            print(f"data_integrity_alert_failed error={type(exc).__name__}")
        else:
            _last_integrity_alert_signature = signature
            _last_integrity_alert_at = now
    return False


def run_phase2_maintenance(now=None):
    """Bounded retention and crash recovery; safe to call repeatedly."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        conversation_store.recover_stale_pending_claims(now=now, limit=100)
        conversation_store.expire_stale_pending(now=now, limit=100)
        conn = conversation_store.connect()
        try:
            phase2_store.cleanup_expired_sessions(conn, now=now, limit=100)
            phase2_store.redact_terminal_pending_payloads(
                conn, before=now + dt.timedelta(seconds=1), limit=100
            )
            conn.commit()
        finally:
            conn.close()
        conversation_store.redact_old_action_payloads(
            before=now - dt.timedelta(days=90), limit=100
        )
        process_pending_factor_jobs(limit=3)
        run_data_integrity_monitor(now=now)
    except (sqlite3.Error, ValueError, phase2_store.Phase2StoreError) as exc:
        print(f"phase2_maintenance_failed error={type(exc).__name__}")
        return False
    return True


def process_pending_factor_jobs(limit=3):
    """Retry durable projection jobs; every claim/completion is fenced."""
    if not phase2_flags.factor_capture_enabled():
        return 0
    completed = 0
    for _ in range(max(0, min(int(limit), 10))):
        conn = conversation_store.connect()
        try:
            phase2_store.migrate(conn)
            conn.execute("BEGIN IMMEDIATE")
            job = phase2_store.claim_factor_job(conn)
            if job is None:
                conn.rollback()
                break
            projection = daily_log.projection_state(conn, job["context_date"])
            conn.commit()
            result = factor_capture.capture_factors_result(
                (projection or {}).get("notes") or "", enabled=True,
                transport=_build_factor_client(),
            )
            conn.execute("BEGIN IMMEDIATE")
            if result["status"] != "succeeded":
                phase2_store.fail_factor_job(
                    conn, job["job_id"], job["lease_token"], result["error_code"]
                )
            else:
                observations = [
                    {**item, "state": 1 if item["state"] == "present" else 0}
                    for item in result["observations"]
                ]
                if phase2_store.complete_factor_job(
                    conn, job["job_id"], job["lease_token"], observations,
                    allowed_factor_keys=conversation_contract.DAILY_FACTOR_KEYS,
                ):
                    completed += 1
            conn.commit()
        except (sqlite3.Error, ValueError, phase2_store.Phase2StoreError):
            conn.rollback()
            break
        finally:
            conn.close()
    return completed


def response_retry_loop(stop_event=None, interval_seconds=60):
    """Small bounded delivery loop; it never executes domain actions."""
    stop_event = stop_event or threading.Event()
    while not stop_event.wait(interval_seconds):
        try:
            run_phase2_maintenance()
            retry_pending_conversation_responses()
        except Exception as exc:
            print(f"conversation_retry_loop_failed error={type(exc).__name__}")


def _build_factor_client():
    return gemini_client.GeminiClient(
        api_key=GEMINI_KEY, model=MODEL, fallback_models=FALLBACK_MODELS,
        response_schema=factor_capture.RESPONSE_SCHEMA,
    )


def _capture_daily_factors(context_date, note, source_key_prefix):
    """Enqueue and process the latest full-date projection with durable status."""
    enabled = phase2_flags.factor_capture_enabled()
    conn = conversation_store.connect()
    try:
        phase2_store.migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        projection = daily_log.projection_state(conn, context_date)
        if projection is None:
            conn.rollback()
            return 0
        origin_action_id = (
            source_key_prefix.split(":", 1)[1]
            if str(source_key_prefix).startswith("conversation:") else None
        )
        job_id, _ = phase2_store.enqueue_factor_job(
            conn, context_date=context_date,
            projection_hash=projection["projection_hash"],
            projection_revision=projection["revision"],
            extractor_version=factor_capture.SCHEMA_VERSION,
            origin_action_id=origin_action_id, enabled=enabled,
        )
        conn.commit()
        if not enabled:
            return 0
        conn.execute("BEGIN IMMEDIATE")
        claimed = phase2_store.claim_factor_job(conn, job_id=job_id)
        conn.commit()
        if claimed is None:
            return 0
        result = factor_capture.capture_factors_result(
            projection.get("notes") or note, enabled=True,
            transport=_build_factor_client(),
        )
        conn.execute("BEGIN IMMEDIATE")
        if result["status"] != "succeeded":
            phase2_store.fail_factor_job(
                conn, job_id, claimed["lease_token"], result["error_code"]
            )
            conn.commit()
            return 0
        observations = [
            {**item, "state": 1 if item["state"] == "present" else 0}
            for item in result["observations"]
        ]
        completed = phase2_store.complete_factor_job(
            conn, job_id, claimed["lease_token"], observations,
            allowed_factor_keys=conversation_contract.DAILY_FACTOR_KEYS,
        )
        conn.commit()
        return len(observations) if completed else 0
    except (sqlite3.Error, ValueError, phase2_store.Phase2StoreError):
        conn.rollback()
        return 0
    finally:
        conn.close()


def _load_conversation_session(chat_id, user_id):
    if not phase2_flags.memory_enabled():
        return None
    conn = conversation_store.connect()
    try:
        record = phase2_store.get_session(conn, "telegram", chat_id, user_id)
        return record
    except sqlite3.OperationalError:
        # Deployment runs the additive migration before enabling the flag.
        return None
    finally:
        conn.close()


def _remember_read(chat_id, user_id, outcome, previous):
    if not phase2_flags.memory_enabled() or not outcome.result:
        return
    action = conversation_store.get_action(outcome.action_id) or {}
    intent = action.get("intent")
    if intent not in conversation_contract.READ_INTENTS and intent != conversation_contract.INTENT_LOG_CARDIO:
        return
    try:
        args = json.loads(action.get("validated_arguments_json") or "{}")
        if intent == conversation_contract.INTENT_LOG_CARDIO:
            args = {"intent": conversation_contract.INTENT_GET_DAY_SNAPSHOT,
                    "date": outcome.result.get("resolved_date"), "activity": "cardio"}
        evidence_data = outcome.result.get("data", {})
        digest = hashlib.sha256(
            json.dumps(evidence_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        old_turns = (previous.state.get("turn_count", 0) if previous else 0)
        state = {
            "active_topic": args.get("metric") or args.get("factor_key") or intent,
            "last_read_intent": intent,
            "last_query": args,
            "last_evidence_sha256": digest,
            "turn_count": min(old_turns + 1, conversation_contract.SESSION_MAX_TURNS),
        }
        conn = conversation_store.connect()
        try:
            try:
                phase2_store.save_session(
                    conn, "telegram", chat_id, user_id, state,
                    expected_version=previous.version if previous else None,
                )
            except phase2_store.SessionConflict:
                latest = phase2_store.get_session(
                    conn, "telegram", chat_id, user_id
                )
                if latest is None:
                    raise
                state["turn_count"] = min(
                    latest.state.get("turn_count", 0) + 1,
                    conversation_contract.SESSION_MAX_TURNS,
                )
                phase2_store.save_session(
                    conn, "telegram", chat_id, user_id, state,
                    expected_version=latest.version,
                )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, phase2_store.Phase2StoreError, ValueError, TypeError):
        # Memory is convenience state; it must never affect a completed read.
        return
def deliver_weekly_if_due(context, chat_id):
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
        delivery_key,
        "weekly_report",
        report,
        chunks,
        lambda chunk: bot.send_message(
            chat_id, chunk, disable_web_page_preview=True
        ),
    ):
        return False
    weekly_report.mark_sent(evening_date)
    return True


def deliver_morning_analysis(context, chat_id):
    """Generate only when grounded data (or the documented cutoff) makes it ready."""
    run_id = morning_observability.new_run_id("telegram_analysis")
    timer = morning_observability.StageTimer.start(
        context["recovery_date"], run_id, "analysis_generated"
    )
    mode = morning_readiness.analysis_mode(context["recovery_date"])
    if not mode:
        timer.finish("waiting", "current_morning_data_not_ready")
        bot.send_message(
            chat_id,
            "Контекст сохранён. Разбор отправлю автоматически после появления данных WHOOP.",
        )
        return False
    claimed = morning_context.claim_analysis(context["recovery_date"], mode)
    if not claimed:
        timer.finish("skipped", "analysis_claim_not_available")
        return False
    try:
        delivery_key = f"daily_result:{claimed['recovery_date']}"
        report = report_delivery.saved_payload(delivery_key)
        reused = report is not None
        if report is None:
            analysis = morning_reporting.generate_daily_analysis(
                claimed["evening_date"], context_only=mode == "context_only"
            )
            report = morning_reporting.compose_morning_result(
                claimed["recovery_date"], analysis, db_path=daily_log.DB_PATH
            )
        if not report_delivery.deliver(
            delivery_key,
            "daily_result",
            report,
            [report],
            lambda chunk: bot.send_message(
                chat_id, chunk, disable_web_page_preview=True
            ),
        ):
            timer.finish("failed", "analysis_delivery_claim_unavailable")
            morning_context.release_analysis(claimed["recovery_date"])
            return False
        if not morning_context.complete_analysis(claimed["recovery_date"]):
            timer.finish("failed", "analysis_completion_state_conflict")
            morning_context.release_analysis(claimed["recovery_date"])
            return False
        timer.finish(
            "success",
            "durable_analysis_payload_reused_and_delivered"
            if reused else "analysis_generated_and_delivered",
        )
        try:
            deliver_weekly_if_due(claimed, chat_id)
        except Exception as exc:
            print(
                "Еженедельный отчёт будет повторён позже: "
                f"{type(exc).__name__}"
            )
        return True
    except Exception as exc:
        morning_context.release_analysis(claimed["recovery_date"])
        timer.finish(
            "failed",
            morning_observability.safe_exception_reason(
                exc, prefix="analysis_generation_or_delivery"
            ),
        )
        print(f"Ошибка ежедневного разбора: {type(exc).__name__}")
        return False


def handle_morning_context_response(message, text):
    """Accept only a direct reply to the exact pending WHOOP question."""
    reply_to = getattr(message, "reply_to_message", None)
    question_message_id = getattr(reply_to, "message_id", None)
    try:
        context = morning_context.accept_and_record_reply(
            message.message_id, question_message_id, text,
            source_key=f"telegram:{message.chat.id}:{message.message_id}:morning-context",
            extractor_version=factor_capture.SCHEMA_VERSION,
            factor_capture_enabled=phase2_flags.factor_capture_enabled(),
        )
    except Exception as exc:
        bot.send_message(message.chat.id, "Не удалось сохранить ответ; попробуй отправить его ещё раз.")
        print(f"Ошибка атомарного сохранения утреннего контекста: {type(exc).__name__}")
        return True
    if context is None:
        return False
    _capture_daily_factors(
        context["evening_date"], text,
        f"telegram:{message.chat.id}:{message.message_id}:morning-context",
    )
    deliver_morning_analysis(context, message.chat.id)
    return True

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_authorized_message(message):
        return
    help_text = (
        "🤖 *Привет! Я твой фитнес-ассистент Джарвис.*\n\n"
        "Отправляй мне сообщения о тренировках или БАДов в свободном формате, например:\n"
        "• _жим лежа 100кг 3х5 и 5г креатина_\n"
        "• _выпил магний 400мг и апигенин 50мг_ (БАДы сна, отправленные утром, запишутся на *вчера*)\n"
        "• Отправляй *скриншоты кардио* (Apple Watch, беговая дорожка) — я автоматически вытащу время, пульс и калории!\n\n"
        "Можно спросить: _как менялся HRV за 28 дней?_ или "
        "_что видно по магнию за 84 дня?_. На короткий follow-up отвечай в том же чате.\n\n"
        "На утренний вопрос и мои уточнения отвечай через *Reply*. Планы не записываются; "
        "Для исправлений напиши: _удали последнюю тренировку_ или "
        "_перенеси последнюю тренировку на вчера_. Изменение выполняется "
        "только после точного подтверждения через *Reply*.\n\n"
        "*Команды:*\n"
        "/status — записи тренировок, БАДов и контекста за день\n"
        "/sync — запустить проверку и сборку дашборда WHOOP вручную"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['sync'])
def trigger_sync(message):
    if not is_authorized_message(message):
        return
    bot.send_message(message.chat.id, "🔄 *Запускаю синхронизацию WHOOP и проверку утреннего сценария...*")
    
    import subprocess
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "morning_flow.py")
    try:
        r = subprocess.run([sys.executable, script_path, "--force"], capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            bot.send_message(message.chat.id, "✅ *Синхронизация успешно завершена!* Если Recovery уже пришёл, Джарвис запросит контекст вчерашнего дня.")
        else:
            bot.send_message(message.chat.id, "❌ Синхронизация не завершилась. Подробности сохранены в системном журнале.")
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Не удалось запустить синхронизацию. Попробуй позже.")
        print(f"sync_failed error={type(e).__name__}")

@bot.message_handler(commands=['status'])
def show_status(message):
    if not is_authorized_message(message):
        return
    
    now_kiev = get_kiev_time()
    today_str = now_kiev.date().isoformat()
    yesterday_str = (now_kiev - dt.timedelta(days=1)).date().isoformat()
    
    # По умолчанию /status показывает вчера, если вызван глубокой ночью (до 5 утра)
    target_date = yesterday_str if now_kiev.hour < 5 else today_str
    
    conn = workouts_db.connect()
    conn.row_factory = sqlite3.Row
    workouts = conn.execute(
        "SELECT * FROM workout_exercises WHERE date = ? AND deleted_at IS NULL", (target_date,)
    ).fetchall()
    cardios = conn.execute(
        "SELECT * FROM cardio_exercises WHERE date = ? AND deleted_at IS NULL", (target_date,)
    ).fetchall()
    supplements = conn.execute(
        "SELECT * FROM supplements_log WHERE date = ? AND deleted_at IS NULL", (target_date,)
    ).fetchall()
    elog = conn.execute("SELECT notes FROM daily_log WHERE date = ?", (target_date,)).fetchone()
    conn.close()
    
    # Накопленный креатин
    total_creatine, creatine_count = workouts_db.get_accumulated_creatine()
    
    resp = [f"📊 *Статус за {target_date}:*"]
    
    if workouts:
        resp.append("\n🏋️‍♂️ *Силовая тренировка:*")
        grouped = {}
        for w in workouts:
            name = w["exercise_name"]
            weight = w["weight"]
            reps = w["reps"]
            key = (name, weight)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(reps)
            
        for (name, weight), reps_list in grouped.items():
            weight_str = f" {weight}кг" if weight else ""
            reps_str = ", ".join(str(r) for r in reps_list if r is not None)
            resp.append(f"• {name}:{weight_str} (повторы: {reps_str})")
    else:
        resp.append("\n🏋️‍♂️ *Силовые тренировки:* сегодня еще не записаны")
        
    if cardios:
        resp.append("\n🏃‍♂️ *Кардио-тренировки:*")
        for c in cardios:
            dist = f", {c['distance']} км" if c['distance'] else ""
            hr = f", ЧСС {c['avg_hr']}" if c['avg_hr'] else ""
            resp.append(f"• {c['type']}: {c['duration']} мин{dist}{hr}")
            
            # Показываем зоны пульса
            zones = []
            if c['hr_zone_5_duration'] and c['hr_zone_5_duration'] != "0:00": zones.append(f"З5: {c['hr_zone_5_duration']}")
            if c['hr_zone_4_duration'] and c['hr_zone_4_duration'] != "0:00": zones.append(f"З4: {c['hr_zone_4_duration']}")
            if c['hr_zone_3_duration'] and c['hr_zone_3_duration'] != "0:00": zones.append(f"З3: {c['hr_zone_3_duration']}")
            if c['hr_zone_2_duration'] and c['hr_zone_2_duration'] != "0:00": zones.append(f"З2: {c['hr_zone_2_duration']}")
            if c['hr_zone_1_duration'] and c['hr_zone_1_duration'] != "0:00": zones.append(f"З1: {c['hr_zone_1_duration']}")
            if c['hr_zone_0_duration'] and c['hr_zone_0_duration'] != "0:00": zones.append(f"З0: {c['hr_zone_0_duration']}")
            if zones:
                resp.append(f"  └ Зоны: {', '.join(zones)}")
    else:
        resp.append("\n🏃‍♂️ *Кардио-тренировки:* сегодня еще не записаны")
        
    if supplements:
        resp.append("\n💊 *Принятые БАДы:*")
        for s in supplements:
            dos = f" ({s['dosage']})" if s['dosage'] else ""
            resp.append(f"• {s['name']}{dos} в {s['time']}")
    else:
        resp.append("\n💊 *БАДы:* сегодня еще не записаны")
        
    if elog and elog['notes']:
        resp.append(f"\n📝 *Daily context:* «{elog['notes']}»")
        
    resp.append(f"\n💪 *Накопительный итог Креатина:*")
    resp.append(f"• Всего принято: *{total_creatine} г* (в количестве {creatine_count} раз)")
    
    bot.send_message(message.chat.id, "\n".join(resp))

@bot.message_handler(content_types=['photo', 'document'])
def handle_photo(message):
    if not is_authorized_message(message):
        return
    if not phase2_flags.gemini_vision_enabled():
        bot.send_message(
            message.chat.id,
            "Image analysis is disabled. The operator must explicitly enable "
            "GEMINI_VISION_ENABLED after reviewing PRIVACY.md.",
        )
        return
    photo_ctx, reservation = _photo_action(message)
    if not reservation.is_new:
        result = json.loads(reservation.result_json) if reservation.result_json else None
        outcome = conversation_router.RouterOutcome(
            reservation.action_id,
            "in_progress" if reservation.in_progress else "duplicate",
            "Кардио уже обрабатывается." if reservation.in_progress
            else reservation.response_text,
            result=result, write_committed=bool(result),
        )
        send_conversation_outcome(message.chat.id, outcome)
        return
    lease = {
        "processing_token": reservation.processing_token,
        "processing_fence": reservation.processing_fence,
    }
    try:
        now_kiev = get_kiev_time()
        target_date = _telegram_target_date(message, now_kiev)
        file_id, declared_mime, _kind = _message_attachment(message)
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        actual_mime = _image_mime_type(downloaded_file)
        if actual_mime is None:
            raise cardio_extraction.CardioExtractionError("image_mime")
        if declared_mime and declared_mime != actual_mime:
            raise cardio_extraction.CardioExtractionError("image_mime")
        provider_bytes, _provider_mime = _prepare_cardio_image(downloaded_file)
        parsed, meta = parse_photo_cardio_contract(
            provider_bytes, caption=getattr(message, "caption", None)
        )
        conversation_store.record_router(
            reservation.action_id, model=meta["model"],
            response_sha256=meta["response_sha256"],
            intent=conversation_contract.INTENT_LOG_CARDIO,
            confidence=parsed["confidence"],
            latency_ms=meta["latency_ms"], attempt_count=meta["attempt_count"],
            # Trace metadata is additive: a valid extraction must not lose its
            # transaction simply because an older caller omitted this field.
            prompt_version=meta.get("prompt_version", "photo_cardio_v1"),
            relay_metadata=meta.get("relay"), **lease,
        )
        if parsed.get("needs_clarification"):
            missing = parsed["needs_clarification"]
            conversation_store.mark_clarification(
                reservation.action_id, conversation_contract.INTENT_LOG_CARDIO,
                parsed.get("confidence"), **lease,
            )
            pending_id = conversation_store.create_pending(
                reservation.action_id, photo_ctx,
                conversation_contract.INTENT_LOG_CARDIO,
                partial_arguments=_vision_pending_arguments(parsed, target_date),
                missing_fields=[missing],
            )
            questions = {
                "activity_type": "Какой это тип кардио: ходьба, бег или другое?",
                "duration": "Напиши длительность кардио в формате 28:43.",
                "effort_metric": (
                    "Напиши один показатель: средний пульс, калории или strain."
                ),
            }
            send_conversation_outcome(
                message.chat.id,
                conversation_router.RouterOutcome(
                    reservation.action_id, "clarification",
                    questions[missing], pending_id=pending_id,
                ),
            )
            return
        outcome = persist_photo_cardio(
            message, parsed, target_date, now_kiev, reservation=reservation
        )
        send_conversation_outcome(message.chat.id, outcome)
    except cardio_extraction.CardioExtractionError as exc:
        code = str(exc)
        conversation_store.record_router(
            reservation.action_id, model=getattr(exc, "model", None),
            response_sha256=getattr(exc, "response_sha256", None),
            intent=conversation_contract.INTENT_LOG_CARDIO, confidence=None,
            latency_ms=getattr(exc, "latency_ms", None),
            attempt_count=getattr(exc, "attempt_count", 0),
            prompt_version="photo_cardio_v1", **lease,
        )
        questions = {
            "activity_type": "Не удалось определить тип активности. Это ходьба, бег или другое кардио?",
            "duration": "Не удалось уверенно прочитать длительность. Напиши длительность кардио.",
            "effort_metric": "Не прочитан средний пульс или калории. Напиши один из этих показателей.",
            "low_confidence": (
                "Не могу надёжно подтвердить один из обязательных показателей. "
                "Напиши тип активности, длительность и средний пульс или калории."
            ),
            "confidence": (
                "Не хватает надёжности по обязательным показателям. "
                "Напиши тип активности, длительность и средний пульс или калории."
            ),
        }
        pending_id = None
        if code in {"activity_type", "duration", "effort_metric"}:
            conversation_store.mark_clarification(
                reservation.action_id, conversation_contract.INTENT_LOG_CARDIO,
                None, **lease,
            )
            partial = dict(getattr(exc, "partial", {}))
            partial["_target_date"] = target_date
            pending_id = conversation_store.create_pending(
                reservation.action_id, photo_ctx,
                conversation_contract.INTENT_LOG_CARDIO,
                partial_arguments=partial,
                missing_fields=[code],
            )
        else:
            conversation_store.mark_rejected(
            reservation.action_id,
            (
                f"photo_{code}_{getattr(exc, 'status_code', '')}".rstrip("_")
            ),
            None,
                intent=conversation_contract.INTENT_LOG_CARDIO, **lease,
            )
        send_conversation_outcome(
            message.chat.id,
            conversation_router.RouterOutcome(
                reservation.action_id,
                "clarification" if pending_id else "rejected",
                questions.get(
                    code,
                    "Не удалось надёжно извлечь показатели кардио; ничего не записано.",
                ), pending_id=pending_id,
            ),
        )
    except Exception as exc:
        conversation_store.mark_failed(
            reservation.action_id, "photo_processing_failed", None, **lease,
        )
        send_conversation_outcome(
            message.chat.id,
            conversation_router.RouterOutcome(
                reservation.action_id, "tool_failed",
                "Не удалось обработать изображение; ничего не записано.",
            ),
        )
        print(f"photo_processing_failed error={type(exc).__name__}")


def _legacy_handle_photo(message):
    if not is_authorized_message(message):
        return
    if not phase2_flags.gemini_vision_enabled():
        return
        
    bot.send_message(message.chat.id, "📸 *Распознаю скриншот кардио через Gemini...*")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        provider_bytes, _provider_mime = _prepare_cardio_image(downloaded_file)
        parsed = parse_photo_cardio_contract(provider_bytes)
        
        if not parsed or not parsed.get("type"):
            bot.send_message(message.chat.id, "❌ Не удалось распознать кардио-тренировку на скриншоте. Убедитесь, что на картинке видны показатели.")
            return
            
        cardio_type = parsed.get("type", "Кардио")
        duration = parsed.get("duration")
        distance = parsed.get("distance")
        avg_hr = parsed.get("avg_hr")
        calories = parsed.get("calories")
        
        zone0 = parsed.get("zone_0")
        zone1 = parsed.get("zone_1")
        zone2 = parsed.get("zone_2")
        zone3 = parsed.get("zone_3")
        zone4 = parsed.get("zone_4")
        zone5 = parsed.get("zone_5")
        
        now_kiev = get_kiev_time()
        today_str = now_kiev.date().isoformat()
        yesterday_str = (now_kiev - dt.timedelta(days=1)).date().isoformat()
        
        # Если скриншот прислан ночью (до 5 утра), относим тренировку к вчерашнему дню
        target_date = yesterday_str if now_kiev.hour < 5 else today_str
        now_time = now_kiev.strftime("%H:%M")
        
        outcome = persist_photo_cardio(message, parsed, target_date, now_kiev)
        send_conversation_outcome(message.chat.id, outcome)
        return
        
        resp = [
            f"🤖 *Джарвис распознал кардио-тренировку за {target_date}:*",
            f"• *Тип:* {cardio_type}",
            f"• *Длительность:* {duration} мин"
        ]
        if distance:
            resp.append(f"• *Дистанция:* {distance} км")
        if avg_hr:
            resp.append(f"• *Средний пульс:* {avg_hr} уд/мин")
        if calories:
            resp.append(f"• *Калории:* {calories} ккал")
            
        # Формируем список пульсовых зон
        zones_lines = []
        if zone5 and zone5 != "0:00" and zone5 != "0:00:00": zones_lines.append(f"  - Зона 5 (макс. нагрузка): {zone5}")
        if zone4 and zone4 != "0:00" and zone4 != "0:00:00": zones_lines.append(f"  - Зона 4 (порог): {zone4}")
        if zone3 and zone3 != "0:00" and zone3 != "0:00:00": zones_lines.append(f"  - Зона 3 (аэробная): {zone3}")
        if zone2 and zone2 != "0:00" and zone2 != "0:00:00": zones_lines.append(f"  - Зона 2 (жиросжигание): {zone2}")
        if zone1 and zone1 != "0:00" and zone1 != "0:00:00": zones_lines.append(f"  - Зона 1 (легкая): {zone1}")
        if zone0 and zone0 != "0:00" and zone0 != "0:00:00": zones_lines.append(f"  - Зона 0 (восстановление): {zone0}")
        
        if zones_lines:
            resp.append("\n⏱️ *Время в пульсовых зонах:*")
            resp.extend(zones_lines)
            
        bot.send_message(message.chat.id, "\n".join(resp))
        
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Не удалось обработать скриншот. Попробуй позже.")
        print(f"photo_processing_failed error={type(e).__name__}")

_conversation_lock_guard = threading.Lock()
_conversation_locks = weakref.WeakValueDictionary()


def _conversation_lock(message):
    """Serialize updates for one Telegram chat/user without blocking other chats.

    pyTelegramBotAPI dispatches handlers on a worker pool.  A persistent draft
    is durable in SQLite, but its sequence still has to match Telegram's update
    order: an exercise and a following ``готово`` must never inspect the draft
    concurrently.  The lock covers the complete routing transaction, including
    creation of the first pending draft.
    """
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    key = (str(message.chat.id), str(user_id) if user_id is not None else "")
    with _conversation_lock_guard:
        lock = _conversation_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _conversation_locks[key] = lock
        return lock


def route_via_conversation(message, text):
    with _conversation_lock(message):
        return _route_via_conversation_serial(message, text)


def _route_via_conversation_serial(message, text):
    """Feature-flagged conversational router path.

    When the flag is ON, an ordinary message goes only through the bounded
    Gemini router → validator → allowlisted tool. Unknown intent, malformed
    output, low confidence, or an outage never fall back to the legacy parser.
    """
    local_now = get_kiev_time()
    conversation_store.recover_stale_pending_claims()
    try:
        retry_pending_conversation_responses(limit=5)
    except Exception as exc:
        print(f"conversation_retry_failed error={type(exc).__name__}")
    try:
        bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    reply_to = getattr(getattr(message, "reply_to_message", None), "message_id", None)
    user_id = getattr(getattr(message, "from_user", None), "id", None)

    # If this is a direct reply to an open clarification question, resolve it.
    fact_status_pending = None
    supplement_pending = None
    strength_entries_pending = None
    photo_cardio_pending = None
    correction_pending = None
    strength_draft_pending = conversation_store.active_pending(
        "telegram", message.chat.id, user_id
    )
    if strength_draft_pending and strength_draft_pending.get("candidate_intent") != conversation_contract.INTENT_START_STRENGTH_DRAFT:
        strength_draft_pending = None
    if reply_to is not None:
        candidates = [
            conversation_store.active_pending(source, message.chat.id, user_id)
            for source in ("telegram", "telegram-photo")
        ]
        pending = next(
            (
                item for item in candidates
                if item and str(item.get("clarification_question_message_id")) == str(reply_to)
            ),
            None,
        )
        if pending:
            missing_fields = json.loads(pending["missing_fields_json"])
            if pending.get("candidate_intent") == activity_corrections.INTENT:
                correction_pending = pending
            elif "fact_status" in missing_fields:
                fact_status_pending = pending
            elif pending.get("candidate_intent") == conversation_contract.INTENT_LOG_SUPPLEMENT:
                supplement_pending = pending
            elif pending.get("candidate_intent") == conversation_contract.INTENT_LOG_STRENGTH \
                    and "entries" in missing_fields:
                strength_entries_pending = pending
            elif pending.get("candidate_intent") == conversation_contract.INTENT_LOG_CARDIO \
                    and set(missing_fields).intersection(
                        {"activity_type", "duration", "effort_metric"}
                    ):
                photo_cardio_pending = pending

    ctx = conversation_store.ActionContext(
        source="telegram",
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=user_id,
        reply_to_message_id=reply_to,
        input_text=text,
    )
    exec_ctx = conversation_tools.ExecContext(
        action_id="",
        source="telegram",
        chat_id=str(message.chat.id),
        message_id=str(message.message_id),
        local_now=local_now,
        reply_to_message_id=str(reply_to) if reply_to is not None else None,
    )

    # A fact-status clarification is resolved deterministically (yes/no to a
    # closed question we asked ourselves) - never via a second independent
    # Gemini call on the reply text alone.
    if fact_status_pending is not None:
        try:
            outcome = conversation_router.resolve_fact_status_pending(
                fact_status_pending, ctx, exec_ctx
            )
        except Exception as exc:  # never crash the poller; nothing was committed here
            print(f"conversation_router_failed error={type(exc).__name__}")
            bot.send_message(message.chat.id, conversation_router.NO_WRITE_MESSAGE)
            return
        send_conversation_outcome(message.chat.id, outcome)
        return

    if supplement_pending is not None:
        try:
            outcome = conversation_router.resolve_supplement_pending(
                supplement_pending, ctx, exec_ctx
            )
        except Exception as exc:
            print(f"conversation_resolution_failed error={type(exc).__name__}")
            bot.send_message(message.chat.id, conversation_router.NO_WRITE_MESSAGE)
            return
        send_conversation_outcome(message.chat.id, outcome)
        return

    if strength_entries_pending is not None:
        try:
            outcome = conversation_router.resolve_strength_entries_pending(
                strength_entries_pending, ctx, exec_ctx
            )
        except Exception as exc:
            print(f"conversation_resolution_failed error={type(exc).__name__}")
            bot.send_message(message.chat.id, conversation_router.NO_WRITE_MESSAGE)
            return
        send_conversation_outcome(message.chat.id, outcome)
        return

    if photo_cardio_pending is not None:
        try:
            outcome = _resolve_photo_pending(photo_cardio_pending, ctx, exec_ctx)
        except Exception as exc:
            print(f"conversation_resolution_failed error={type(exc).__name__}")
            bot.send_message(message.chat.id, conversation_router.NO_WRITE_MESSAGE)
            return
        send_conversation_outcome(message.chat.id, outcome)
        return

    if correction_pending is not None:
        try:
            result = activity_corrections.resolve(
                correction_pending, ctx, exec_ctx
            )
            outcome = conversation_router.RouterOutcome(
                action_id=result["action_id"], kind=result["kind"],
                message=result.get("message"), result=result.get("result"),
                write_committed=result.get("write_committed", False),
            )
        except Exception as exc:
            print(f"conversation_correction_failed error={type(exc).__name__}")
            bot.send_message(message.chat.id, conversation_router.NO_WRITE_MESSAGE)
            return
        send_conversation_outcome(message.chat.id, outcome)
        return

    correction_request = (
        None if phase2_flags.bounded_agent_enabled()
        else activity_corrections.parse_command(text, local_now)
    )

    if strength_draft_pending is not None:
        try:
            outcome = strength_draft.handle(
                strength_draft_pending, ctx, exec_ctx, _build_gemini_client()
            )
        except Exception as exc:
            print(f"strength_draft_failed error={type(exc).__name__}")
            bot.send_message(message.chat.id, "Не смог обновить черновик силовой; сохранённые упражнения не потеряны.")
            return
        send_conversation_outcome(message.chat.id, outcome)
        return
    if correction_request is not None:
        result = activity_corrections.propose(ctx, local_now, correction_request)
        outcome = conversation_router.RouterOutcome(
            action_id=result["action_id"], kind=result["kind"],
            message=result.get("message"), result=result.get("result"),
            pending_id=result.get("pending_id"),
        )
        send_conversation_outcome(message.chat.id, outcome)
        return

    session = _load_conversation_session(message.chat.id, user_id)
    try:
        outcome = conversation_router.route(
            ctx, exec_ctx, local_now=local_now, gemini=_build_gemini_client(),
            session_state=session.state if session else None,
        )
    except Exception as exc:  # never crash the poller; nothing was committed here
        print(f"conversation_router_failed error={type(exc).__name__}")
        bot.send_message(message.chat.id, conversation_router.NO_WRITE_MESSAGE)
        return

    _remember_read(message.chat.id, user_id, outcome, session)
    if outcome.write_committed and outcome.result:
        action = conversation_store.get_action(outcome.action_id) or {}
        if action.get("intent") == conversation_contract.INTENT_SAVE_DAILY_CONTEXT:
            _capture_daily_factors(
                outcome.result.get("resolved_date"), text,
                f"conversation:{outcome.action_id}",
            )
    send_conversation_outcome(message.chat.id, outcome)


@bot.message_handler(func=is_authorized_message)
def handle_free_text(message):
    text = message.text.strip()

    # 1. Daily context is accepted only as a direct reply to its exact question.
    # A pending request by itself must never intercept workouts or supplements.
    if handle_morning_context_response(message, text):
        return

    # 2. Feature-flagged conversational router owns all ordinary text when ON.
    if router_enabled():
        route_via_conversation(message, text)
        return

    # ---- Legacy path (flag OFF) --------------------------------------------
    # Keyword delete permanently removes data with no confirmation, so it only
    # runs when explicitly enabled.
    if legacy_destructive_text_enabled() and (
        "удали" in text.lower() or "очисти" in text.lower() or "стереть" in text.lower()
    ):
        if handle_delete_command(
            text, message.chat.id, message.message_id,
            getattr(getattr(message, "from_user", None), "id", None),
        ):
            return

    bot.send_message(
        message.chat.id,
        "Запись свободным текстом отключена, пока conversational router выключен; "
        "данные не изменены.",
    )
    return

    # All ordinary messages continue through the structured activity parser.
    parsed = parse_with_gemini(text)
    
    workouts = parsed.get("workouts", [])
    supplements = parsed.get("supplements", [])
    
    now_kiev = get_kiev_time()
    today_str = now_kiev.date().isoformat()
    yesterday_str = (now_kiev - dt.timedelta(days=1)).date().isoformat()
    now_time = now_kiev.strftime("%H:%M")
    
    # Определяем целевую дату на основе умных дефолтов и ИИ-анализа текста
    if parsed.get("date"):
        target_date = parsed.get("date")
    else:
        if workouts:
            # Тренировки: если отправлено ночью (до 5 утра), относим по умолчанию к вчера
            target_date = yesterday_str if now_kiev.hour < 5 else today_str
        else:
            # Добавки: до 13:00 относим к вчера, позже — к текущему дню.
            target_date = get_target_date_for_supplements()
            
    if not workouts and not supplements:
        bot.send_message(
            message.chat.id,
            "Не распознал тренировку или добавку, поэтому ничего не записал. "
            "Если это контекст дня, ответь через Reply на утренний вопрос.",
        )
        return
        
    confirm_lines = [f"🤖 *Джарвис записал в базу за {target_date}:*"]
    
    if workouts:
        confirm_lines.append("\n🏋️‍♂️ *Силовые тренировки:*")
        grouped = {}
        for index, w in enumerate(workouts):
            name = w.get("name")
            weight = w.get("weight")
            reps = w.get("reps")
            
            # Записываем в базу как отдельный подход (sets = 1)
            workouts_db.log_exercise(
                target_date, name, weight, 1, reps, text,
                source_key=f"telegram:{message.chat.id}:{message.message_id}:workout:{index}",
            )
            
            key = (name, weight)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(reps)
            
        for (name, weight), reps_list in grouped.items():
            weight_str = f" {weight}кг" if weight else ""
            reps_str = ", ".join(str(r) for r in reps_list if r is not None)
            confirm_lines.append(f"• {name}:{weight_str} (повторы: {reps_str})")
            
    if supplements:
        confirm_lines.append("\n💊 *БАДы:*")
        for index, s in enumerate(supplements):
            name = s.get("name")
            dosage = s.get("dosage")
            workouts_db.log_supplement(
                target_date, now_time, name, dosage, 1, text,
                source_key=f"telegram:{message.chat.id}:{message.message_id}:supplement:{index}",
            )
            
            dos_str = f" ({dosage})" if dosage else ""
            confirm_lines.append(f"• {name}{dos_str}")
            
        has_creatine = any("креатин" in str(s.get("name")).lower() or "creatine" in str(s.get("name")).lower() for s in supplements)
        if has_creatine:
            total_creatine, _ = workouts_db.get_accumulated_creatine()
            confirm_lines.append(f"\n💪 *Накоплено креатина за все время:* {total_creatine} г")
            
    bot.send_message(message.chat.id, "\n".join(confirm_lines))

if __name__ == "__main__":
    print("Запускаю Telegram бот...")
    try:
        run_phase2_maintenance()
        retry_pending_conversation_responses()
    except Exception as exc:
        print(f"conversation_startup_maintenance_failed error={type(exc).__name__}")
    threading.Thread(
        target=response_retry_loop, name="conversation-response-retry",
        daemon=True,
    ).start()
    bot.infinity_polling()
