import math
import sqlite3
import os
import datetime as dt
import re
import hashlib
import json
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "whoop.db")

ENTITY_TABLES = {
    "strength": "workout_exercises",
    "cardio": "cardio_exercises",
    "supplement": "supplements_log",
}

_ROW_HASH_FIELDS = {
    "strength": (
        "date", "exercise_name", "weight", "sets", "reps", "volume",
        "raw_text", "source_key", "origin_action_id",
    ),
    "cardio": (
        "date", "time", "type", "duration", "distance", "avg_hr", "calories",
        "hr_zone_0_duration", "hr_zone_1_duration", "hr_zone_2_duration",
        "hr_zone_3_duration", "hr_zone_4_duration", "hr_zone_5_duration",
        "raw_text", "source_key", "origin_action_id",
    ),
    "supplement": (
        "date", "time", "name", "dosage", "taken", "raw_text", "source_key",
        "origin_action_id",
    ),
}


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    # SQLite fires DELETE triggers for INSERT OR REPLACE only when recursive
    # triggers are enabled on the current connection.  Without this setting a
    # REPLACE could silently bypass the hard-delete guard below.
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.row_factory = sqlite3.Row
    ensure_tables(conn)
    return conn

def ensure_tables(conn):
    # ``ensure_tables`` is also called with connections opened by other
    # modules, so enforce the connection-local guard here as well.
    conn.execute("PRAGMA recursive_triggers = ON")
    # Таблица силовых тренировок (каждая строка - один подход)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS workout_exercises (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        exercise_name TEXT,
        weight REAL,
        sets INTEGER,
        reps INTEGER,
        volume REAL,
        raw_text TEXT,
        source_key TEXT,
        origin_action_id TEXT,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        deleted_by_action_id TEXT
    )""")
    # Таблица приема БАДов
    conn.execute("""
    CREATE TABLE IF NOT EXISTS supplements_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        time TEXT,
        name TEXT,
        dosage TEXT,
        taken INTEGER DEFAULT 1,
        raw_text TEXT,
        source_key TEXT,
        origin_action_id TEXT,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        deleted_by_action_id TEXT
    )""")
    # Таблица кардио-тренировок
    conn.execute("""
    CREATE TABLE IF NOT EXISTS cardio_exercises (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        time TEXT,
        type TEXT,
        duration REAL,
        distance REAL,
        avg_hr INTEGER,
        calories REAL,
        strain REAL,
        max_hr INTEGER,
        steps INTEGER,
        hr_zone_0_duration TEXT,
        hr_zone_1_duration TEXT,
        hr_zone_2_duration TEXT,
        hr_zone_3_duration TEXT,
        hr_zone_4_duration TEXT,
        hr_zone_5_duration TEXT,
        raw_text TEXT,
        source_key TEXT,
        origin_action_id TEXT,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        deleted_by_action_id TEXT
    )""")
    
    # Добавляем новые колонки для пульсовых зон в существующую таблицу, если их там нет
    cols = [
        ("hr_zone_0_duration", "TEXT"),
        ("hr_zone_1_duration", "TEXT"),
        ("hr_zone_2_duration", "TEXT"),
        ("hr_zone_3_duration", "TEXT"),
        ("hr_zone_4_duration", "TEXT"),
        ("hr_zone_5_duration", "TEXT"),
        ("strain", "REAL"),
        ("max_hr", "INTEGER"),
        ("steps", "INTEGER"),
    ]
    for col_name, col_type in cols:
        try:
            conn.execute(f"ALTER TABLE cardio_exercises ADD COLUMN {col_name} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    source_key_tables = (
        ("workout_exercises", "idx_workout_exercises_source_key"),
        ("supplements_log", "idx_supplements_log_source_key"),
        ("cardio_exercises", "idx_cardio_exercises_source_key"),
    )
    for table_name, index_name in source_key_tables:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
        if "source_key" not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN source_key TEXT")
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
            f"ON {table_name}(source_key) WHERE source_key IS NOT NULL"
        )

    provenance_columns = {
        "origin_action_id": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "deleted_at": "TEXT",
        "deleted_by_action_id": "TEXT",
    }
    for table_name in ENTITY_TABLES.values():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
        for col_name, col_type in provenance_columns.items():
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS database_meta (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            database_uuid TEXT NOT NULL UNIQUE,
            generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    now = _utc_now()
    conn.execute(
        """INSERT OR IGNORE INTO database_meta
           (singleton_id, database_uuid, generation, created_at, updated_at)
           VALUES (1, ?, 1, ?, ?)""",
        (str(uuid.uuid4()), now, now),
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS action_domain_links (
            action_id TEXT NOT NULL,
            entity_type TEXT NOT NULL
                CHECK (entity_type IN ('strength', 'cardio', 'supplement')),
            entity_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            row_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (action_id, entity_type, entity_id),
            UNIQUE (source_key)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_domain_links_entity "
        "ON action_domain_links(entity_type, entity_id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS domain_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL
                CHECK (entity_type IN ('strength', 'cardio', 'supplement')),
            entity_id INTEGER NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('created', 'soft_deleted')),
            action_id TEXT NOT NULL,
            source_key TEXT,
            before_hash TEXT,
            after_hash TEXT,
            reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (action_id, entity_type, entity_id, event_type)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domain_events_entity "
        "ON domain_events(entity_type, entity_id, event_id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recovery_reconciliations (
            reconciliation_id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL,
            entity_type TEXT NOT NULL
                CHECK (entity_type IN ('strength', 'cardio', 'supplement')),
            original_record_ids TEXT NOT NULL,
            recovered_record_ids TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (action_id, entity_type)
        )
    """)
    for table_name in ENTITY_TABLES.values():
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table_name}_no_hard_delete
            BEFORE DELETE ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, 'hard delete blocked; use soft_delete_activity');
            END
        """)
    for table_name in ("action_domain_links", "domain_events", "recovery_reconciliations"):
        for operation in ("UPDATE", "DELETE"):
            suffix = operation.lower()
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table_name}_immutable_{suffix}
                BEFORE {operation} ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} is immutable');
                END
            """)
    conn.commit()


# ---------------------------------------------------------------------------
# Connection-aware internal inserts.
#
# These operate on a caller-supplied connection and DO NOT commit, so the
# conversation tools can bundle several domain rows + the audit row into one
# atomic BEGIN IMMEDIATE transaction. They assume ensure_tables() has already
# run on the connection. Each returns the new row id, or None when the row was
# a source_key duplicate (INSERT OR IGNORE suppressed it).
# ---------------------------------------------------------------------------

def insert_workout_row(conn, date, exercise_name, weight, sets, reps,
                       raw_text="", source_key=None, origin_action_id=None,
                       created_at=None):
    volume = (weight or 0) * (sets or 0) * (reps or 0)
    timestamp = created_at or _utc_now()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO workout_exercises
            (date, exercise_name, weight, sets, reps, volume, raw_text, source_key,
             origin_action_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (date, exercise_name, weight, sets, reps, volume, raw_text, source_key,
         origin_action_id, timestamp, timestamp),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def insert_supplement_row(conn, date, time, name, dosage, taken=1,
                          raw_text="", source_key=None, origin_action_id=None,
                          created_at=None):
    timestamp = created_at or _utc_now()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO supplements_log
            (date, time, name, dosage, taken, raw_text, source_key,
             origin_action_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (date, time, name, dosage, taken, raw_text, source_key,
         origin_action_id, timestamp, timestamp),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def insert_cardio_row(conn, date, time, cardio_type, duration, distance, avg_hr,
                      calories, zone0=None, zone1=None, zone2=None, zone3=None,
                      zone4=None, zone5=None, raw_text="", source_key=None,
                      origin_action_id=None, created_at=None, strain=None,
                      max_hr=None, steps=None):
    timestamp = created_at or _utc_now()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO cardio_exercises (
            date, time, type, duration, distance, avg_hr, calories,
            strain, max_hr, steps,
            hr_zone_0_duration, hr_zone_1_duration, hr_zone_2_duration,
            hr_zone_3_duration, hr_zone_4_duration, hr_zone_5_duration,
            raw_text, source_key, origin_action_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date, time, cardio_type, duration, distance, avg_hr, calories,
            strain, max_hr, steps,
            zone0, zone1, zone2, zone3, zone4, zone5, raw_text, source_key,
            origin_action_id, timestamp, timestamp,
        ),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def _entity_table(entity_type):
    try:
        return ENTITY_TABLES[entity_type]
    except KeyError as exc:
        raise ValueError(f"unsupported entity_type: {entity_type}") from exc


def _fetch_entity(conn, entity_type, entity_id):
    table = _entity_table(entity_type)
    cur = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,))
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((item[0] for item in cur.description), row))


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def hash_activity_row(entity_type, row):
    """Stable SHA-256 over immutable business/provenance fields."""
    fields = _ROW_HASH_FIELDS.get(entity_type)
    if fields is None:
        raise ValueError(f"unsupported entity_type: {entity_type}")
    selected_fields = list(fields)
    if entity_type == "cardio" and any(
        row.get(field) is not None for field in ("strain", "max_hr", "steps")
    ):
        selected_fields.extend(("strain", "max_hr", "steps"))
    payload = {
        "entity_type": entity_type,
        **{field: _json_safe(row.get(field)) for field in selected_fields},
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_row_hash(entity_type, row):
    payload = {
        "row_hash": hash_activity_row(entity_type, row),
        "deleted_at": row.get("deleted_at"),
        "deleted_by_action_id": row.get("deleted_by_action_id"),
        "updated_at": row.get("updated_at"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def link_action_domain(conn, action_id, entity_type, entity_id, source_key):
    """Link an inserted domain row to its action inside the caller transaction.

    The read-back and hash happen before the link is inserted, so a success
    response cannot be committed with an unverifiable record id.
    """
    if not action_id or not source_key:
        raise ValueError("action_id and source_key are required")
    row = _fetch_entity(conn, entity_type, entity_id)
    if row is None:
        raise sqlite3.IntegrityError("cannot link a missing domain row")
    if row.get("source_key") != source_key:
        raise sqlite3.IntegrityError("domain source_key does not match link")
    if row.get("origin_action_id") != action_id:
        raise sqlite3.IntegrityError("domain origin_action_id does not match link")
    row_hash = hash_activity_row(entity_type, row)
    created_at = _utc_now()
    conn.execute(
        """INSERT INTO action_domain_links
           (action_id, entity_type, entity_id, source_key, row_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (action_id, entity_type, entity_id, source_key, row_hash, created_at),
    )
    conn.execute(
        """INSERT INTO domain_events
           (entity_type, entity_id, event_type, action_id, source_key,
            before_hash, after_hash, reason, created_at)
           VALUES (?, ?, 'created', ?, ?, NULL, ?, 'conversation mutation', ?)""",
        (entity_type, entity_id, action_id, source_key,
         _event_row_hash(entity_type, row), created_at),
    )
    return {
        "action_id": action_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_key": source_key,
        "row_hash": row_hash,
    }


def record_reconciliation(conn, action_id, entity_type, original_record_ids,
                          recovered_record_ids, reason):
    """Document that a succeeded action's domain rows were lost and restored
    under new ids during an approved, controlled recovery.

    This never touches domain rows or the immutable action/link/event ledger;
    it only lets an audit recognize a known, approved id drift as resolved
    instead of flagging the original ids as a missing-row failure. The
    recovered ids must already exist and already be exactly this action's
    linked rows - this call cannot itself create or move data.
    """
    if not action_id or entity_type not in ENTITY_TABLES:
        raise ValueError("action_id and a valid entity_type are required")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason is required")
    recovered = sorted({int(i) for i in recovered_record_ids})
    if not recovered:
        raise ValueError("recovered_record_ids must not be empty")
    table = ENTITY_TABLES[entity_type]
    existing = {
        row[0] for row in conn.execute(
            f"SELECT id FROM {table} WHERE id IN ({','.join('?' for _ in recovered)})",
            recovered,
        )
    }
    if existing != set(recovered):
        raise ValueError("recovered_record_ids must all exist as domain rows")
    linked = {
        row[0] for row in conn.execute(
            "SELECT entity_id FROM action_domain_links WHERE action_id=? AND entity_type=?",
            (action_id, entity_type),
        )
    }
    if linked != set(recovered):
        raise ValueError("recovered_record_ids must exactly match this action's linked rows")
    conn.execute(
        """INSERT INTO recovery_reconciliations
           (reconciliation_id, action_id, entity_type, original_record_ids,
            recovered_record_ids, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), action_id, entity_type,
         json.dumps(sorted({int(i) for i in original_record_ids})),
         json.dumps(recovered), reason, _utc_now()),
    )


def soft_delete_activity(conn, entity_type, entity_id, *,
                         deleted_by_action_id, reason, occurred_at=None):
    """Soft-delete one activity/supplement row and append an audit event.

    The caller owns the transaction. Repeated deletion is idempotent and
    returns False without creating another event.
    """
    if not deleted_by_action_id:
        raise ValueError("deleted_by_action_id is required")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("reason must contain 1..500 characters")
    row = _fetch_entity(conn, entity_type, entity_id)
    if row is None:
        raise LookupError(f"{entity_type} row {entity_id} does not exist")
    if row.get("deleted_at") is not None:
        return False
    timestamp = occurred_at or _utc_now()
    before_hash = _event_row_hash(entity_type, row)
    table = _entity_table(entity_type)
    cur = conn.execute(
        f"""UPDATE {table}
            SET deleted_at = ?, deleted_by_action_id = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL""",
        (timestamp, deleted_by_action_id, timestamp, entity_id),
    )
    if cur.rowcount != 1:
        return False
    updated = _fetch_entity(conn, entity_type, entity_id)
    conn.execute(
        """INSERT INTO domain_events
           (entity_type, entity_id, event_type, action_id, source_key,
            before_hash, after_hash, reason, created_at)
           VALUES (?, ?, 'soft_deleted', ?, ?, ?, ?, ?, ?)""",
        (entity_type, entity_id, deleted_by_action_id, row.get("source_key"),
         before_hash, _event_row_hash(entity_type, updated), reason, timestamp),
    )
    return True


def log_exercise(date, exercise_name, weight, sets, reps, raw_text="", source_key=None):
    conn = connect()
    try:
        row_id = insert_workout_row(
            conn, date, exercise_name, weight, sets, reps, raw_text, source_key
        )
        conn.commit()
        return row_id is not None
    finally:
        conn.close()


def log_supplement(date, time, name, dosage, taken=1, raw_text="", source_key=None):
    conn = connect()
    try:
        row_id = insert_supplement_row(
            conn, date, time, name, dosage, taken, raw_text, source_key
        )
        conn.commit()
        return row_id is not None
    finally:
        conn.close()


def log_cardio(
    date, time, cardio_type, duration, distance, avg_hr, calories,
    zone0=None, zone1=None, zone2=None, zone3=None, zone4=None, zone5=None,
    raw_text="", source_key=None,
):
    conn = connect()
    try:
        row_id = insert_cardio_row(
            conn, date, time, cardio_type, duration, distance, avg_hr, calories,
            zone0, zone1, zone2, zone3, zone4, zone5, raw_text, source_key,
        )
        conn.commit()
        return row_id is not None
    finally:
        conn.close()


def get_workouts_for_period(days=14):
    conn = connect()
    start_date = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute("""
    SELECT * FROM workout_exercises 
    WHERE date >= ? AND deleted_at IS NULL
    ORDER BY date, id
    """, (start_date,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_supplements_for_period(days=14):
    conn = connect()
    start_date = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute("""
    SELECT * FROM supplements_log 
    WHERE date >= ? AND deleted_at IS NULL
    ORDER BY date, time, id
    """, (start_date,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_cardio_for_period(days=14):
    conn = connect()
    start_date = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute("""
    SELECT * FROM cardio_exercises 
    WHERE date >= ? AND deleted_at IS NULL
    ORDER BY date, time, id
    """, (start_date,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

_MASS_TO_GRAMS = {
    "g": 1.0,
    "г": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "гр": 1.0,
    "mg": 0.001,
    "мг": 0.001,
    "mcg": 0.000001,
    "ug": 0.000001,
    "µg": 0.000001,
    "μg": 0.000001,
    "мкг": 0.000001,
}


def parse_mass_grams(dosage):
    """Parse an explicit mass dose; ambiguous/unitless doses stay unknown."""
    if not isinstance(dosage, str):
        return None
    match = re.fullmatch(
        r"\s*([0-9]+(?:[.,][0-9]+)?)\s*([a-zA-Zа-яА-ЯёЁµμ]+)\s*",
        dosage,
    )
    if not match:
        return None
    unit = match.group(2).lower()
    multiplier = _MASS_TO_GRAMS.get(unit)
    if multiplier is None:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value * multiplier


def get_creatine_summary():
    """Lossless/unit-aware creatine total over active, explicitly taken rows."""
    conn = connect()
    rows = conn.execute("""
    SELECT dosage FROM supplements_log
    WHERE taken = 1 AND deleted_at IS NULL
      AND (LOWER(name) LIKE '%креатин%' OR LOWER(name) LIKE '%creatine%')
    """).fetchall()
    conn.close()

    total_grams = 0.0
    parsed_count = 0
    excluded_count = 0
    for r in rows:
        grams = parse_mass_grams(r["dosage"])
        if grams is None:
            excluded_count += 1
        else:
            total_grams += grams
            parsed_count += 1
    return {
        "total_grams": total_grams,
        "parsed_count": parsed_count,
        "excluded_count": excluded_count,
        "taken_creatine_rows": len(rows),
        "normalization_version": "supplement-dose.v1",
    }


def get_accumulated_creatine():
    """Backward-compatible `(grams, parsed_count)` wrapper without guessing."""
    summary = get_creatine_summary()
    return summary["total_grams"], summary["parsed_count"]


# ---------------------------------------------------------------------------
# Dashboard read path — used by build_dashboard.py.
#
# Deliberately separate from connect()/ensure_tables(): the dashboard build
# must never create tables, add columns, or build indexes (that's the bot's
# write-path responsibility). If the activity tables don't exist yet on a
# fresh install, these functions return [] instead of touching schema.
# Columns and payload size are also intentionally narrow: only fields the
# dashboard actually renders, and never `id`/`raw_text`/`source_key`.
# ---------------------------------------------------------------------------

_DASHBOARD_TEXT_MAX = 300
_DASHBOARD_TIME_MAX = 20


def _dashboard_text(value, max_len=_DASHBOARD_TEXT_MAX):
    if not isinstance(value, str):
        return None
    return value if len(value) <= max_len else value[:max_len]


def _dashboard_date(value):
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value
    return None


def _dashboard_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _dashboard_taken(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    return None


def get_manual_workouts_for_dashboard(conn, limit=100):
    """Read-only, bounded fetch of manual (Telegram-logged) workouts.

    Does not call ensure_tables() — if workout_exercises doesn't exist yet,
    returns [] rather than creating it.
    """
    try:
        rows = conn.execute(
            """
            SELECT date, exercise_name, weight, sets, reps, volume
            FROM workout_exercises
            WHERE deleted_at IS NULL
            ORDER BY date DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "date": _dashboard_date(r["date"]),
            "exercise_name": _dashboard_text(r["exercise_name"]),
            "weight": _dashboard_number(r["weight"]),
            "sets": _dashboard_number(r["sets"]),
            "reps": _dashboard_number(r["reps"]),
            "volume": _dashboard_number(r["volume"]),
        }
        for r in rows
    ]


def get_cardio_for_dashboard(conn, limit=100):
    """Read-only, bounded fetch of cardio sessions (Telegram/screenshot log)."""
    try:
        rows = conn.execute(
            """
            SELECT date, time, type, duration, distance, avg_hr, calories,
                   hr_zone_0_duration, hr_zone_1_duration, hr_zone_2_duration,
                   hr_zone_3_duration, hr_zone_4_duration, hr_zone_5_duration
            FROM cardio_exercises
            WHERE deleted_at IS NULL
            ORDER BY date DESC, time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "date": _dashboard_date(r["date"]),
            "time": _dashboard_text(r["time"], _DASHBOARD_TIME_MAX),
            "type": _dashboard_text(r["type"]),
            "duration": _dashboard_number(r["duration"]),
            "distance": _dashboard_number(r["distance"]),
            "avg_hr": _dashboard_number(r["avg_hr"]),
            "calories": _dashboard_number(r["calories"]),
            "hr_zone_0_duration": _dashboard_text(r["hr_zone_0_duration"], _DASHBOARD_TIME_MAX),
            "hr_zone_1_duration": _dashboard_text(r["hr_zone_1_duration"], _DASHBOARD_TIME_MAX),
            "hr_zone_2_duration": _dashboard_text(r["hr_zone_2_duration"], _DASHBOARD_TIME_MAX),
            "hr_zone_3_duration": _dashboard_text(r["hr_zone_3_duration"], _DASHBOARD_TIME_MAX),
            "hr_zone_4_duration": _dashboard_text(r["hr_zone_4_duration"], _DASHBOARD_TIME_MAX),
            "hr_zone_5_duration": _dashboard_text(r["hr_zone_5_duration"], _DASHBOARD_TIME_MAX),
        }
        for r in rows
    ]


def get_supplements_for_dashboard(conn, limit=200):
    """Read-only, bounded fetch of supplement log entries.

    `taken` is preserved as 1 / 0 / None (unknown) — never coerced to a
    default, since 0 (explicitly skipped) and missing/unknown are distinct.
    """
    try:
        rows = conn.execute(
            """
            SELECT date, time, name, dosage, taken
            FROM supplements_log
            WHERE deleted_at IS NULL
            ORDER BY date DESC, time DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "date": _dashboard_date(r["date"]),
            "time": _dashboard_text(r["time"], _DASHBOARD_TIME_MAX),
            "name": _dashboard_text(r["name"]),
            "dosage": _dashboard_text(r["dosage"], 100),
            "taken": _dashboard_taken(r["taken"]),
        }
        for r in rows
    ]
