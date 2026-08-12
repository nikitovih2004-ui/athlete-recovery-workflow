"""
Daily context — одно свободное narrative-поле на локальную календарную дату.
Для backward compatibility живёт в таблице daily_log той же data/whoop.db.

Дата записи D сопоставляется с recovery/сном локального утра D + 1. Structured
workouts/cardio/supplements остаются отдельными каноническими источниками; notes
служит только дополнительным описанием факторов дня.

Раньше это был отдельный evening-log flow. Теперь пользовательский ввод приходит
одним direct Reply на утренний вопрос; legacy строки продолжают читаться без миграции.

Используется двумя способами:
  * как модуль — ensure_table / get_log / has_log / save_log / load_all
    (импортируют daily_summary.py, generate_insights.py, weekly_report.py,
    telegram_bot.py, morning_context.py, import_evening_log.py);
  * как CLI (--get/--exists/--save ниже) — исторически использовалось окном
    ввода evening_log.ps1 (удалено); сейчас ни один скрипт в проекте не
    вызывает daily_log.py как CLI.

        python daily_log.py --get 2026-07-03            # печатает JSON строки или {}
        python daily_log.py --exists 2026-07-03         # печатает 1/0, код возврата 0/1
        python daily_log.py --save 2026-07-03 --file payload.json
"""
import os
import sys
import json
import sqlite3
import argparse
import datetime as dt
import hashlib
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "whoop.db")

# Единственное содержательное поле записи (кроме служебных date/updated_at).
FIELDS = ["notes"]
COLUMNS = ["date", "notes", "updated_at"]


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    ensure_table(conn)
    return conn


def ensure_table(conn):
    """Создаёт таблицу или мигрирует старую (со структурированными полями) к схеме
    date/notes/updated_at, сохраняя дату, notes и updated_at существующих записей."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_log'"
    ).fetchone() is not None
    if not exists:
        conn.execute(
            "CREATE TABLE daily_log (date TEXT PRIMARY KEY, notes TEXT, updated_at TEXT)"
        )
    else:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_log)")]
        if cols != COLUMNS:
            # миграция: переносим только date/notes/updated_at
            sel_notes = "notes" if "notes" in cols else "NULL"
            sel_upd = "updated_at" if "updated_at" in cols else "NULL"
            conn.executescript(
                f"""
                ALTER TABLE daily_log RENAME TO daily_log_old;
                CREATE TABLE daily_log (date TEXT PRIMARY KEY, notes TEXT, updated_at TEXT);
                INSERT INTO daily_log (date, notes, updated_at)
                    SELECT date, {sel_notes}, {sel_upd} FROM daily_log_old;
                DROP TABLE daily_log_old;
                """
            )
    _ensure_entry_tables(conn)
    _backfill_projection_entries(conn)
    conn.commit()


def _ensure_entry_tables(conn):
    """Additive canonical storage; daily_log remains a compatibility projection."""
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_context_entries (
            entry_id TEXT PRIMARY KEY,
            context_date TEXT NOT NULL,
            notes TEXT NOT NULL,
            label TEXT,
            source_key TEXT NOT NULL UNIQUE,
            origin_action_id TEXT,
            revision INTEGER NOT NULL CHECK(revision >= 1),
            status TEXT NOT NULL CHECK(status IN ('active','superseded','retracted')),
            supersedes_entry_id TEXT,
            status_action_id TEXT,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_daily_context_entries_date_status
            ON daily_context_entries(context_date, status, revision, entry_id);
        CREATE TABLE IF NOT EXISTS daily_context_projection_state (
            context_date TEXT PRIMARY KEY,
            projection_hash TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision >= 0),
            updated_at TEXT NOT NULL
        );
        """
    )
    entry_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(daily_context_entries)")
    }
    if "status_action_id" not in entry_columns:
        conn.execute("ALTER TABLE daily_context_entries ADD COLUMN status_action_id TEXT")
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_daily_context_entries_no_delete
           BEFORE DELETE ON daily_context_entries
           BEGIN
             SELECT RAISE(ABORT, 'daily context hard delete blocked');
           END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_daily_context_entries_content_immutable
           BEFORE UPDATE ON daily_context_entries
           WHEN NEW.entry_id IS NOT OLD.entry_id OR
                NEW.context_date IS NOT OLD.context_date OR
                NEW.notes IS NOT OLD.notes OR
                NEW.label IS NOT OLD.label OR
                NEW.source_key IS NOT OLD.source_key OR
                NEW.origin_action_id IS NOT OLD.origin_action_id OR
                NEW.revision IS NOT OLD.revision OR
                (NEW.supersedes_entry_id IS NOT OLD.supersedes_entry_id
                 AND NOT (OLD.supersedes_entry_id IS NULL
                          AND NEW.supersedes_entry_id IS NOT NULL)) OR
                NEW.content_sha256 IS NOT OLD.content_sha256 OR
                NEW.created_at IS NOT OLD.created_at
           BEGIN
             SELECT RAISE(ABORT, 'daily context content is immutable');
           END"""
    )


def _hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _backfill_projection_entries(conn):
    """Represent every non-empty legacy projection once, without duplication."""
    rows = conn.execute("SELECT date, notes, updated_at FROM daily_log").fetchall()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        date_iso, notes, updated_at = row[0], row[1], row[2]
        exists = conn.execute(
            "SELECT 1 FROM daily_context_entries WHERE context_date = ? LIMIT 1",
            (date_iso,),
        ).fetchone()
        if exists is not None:
            continue
        text = str(notes or "").strip()
        if not text:
            continue
        digest = _hash_text(text)
        stamp = updated_at or now
        conn.execute(
            """INSERT OR IGNORE INTO daily_context_entries
               (entry_id, context_date, notes, label, source_key, origin_action_id,
                revision, status, supersedes_entry_id, content_sha256, created_at, updated_at)
               VALUES (?, ?, ?, NULL, ?, NULL, 1, 'active', NULL, ?, ?, ?)""",
            (f"legacy:{_hash_text(date_iso)[:24]}", date_iso, text,
             f"legacy:daily_log:{date_iso}", digest, stamp, stamp),
        )
        conn.execute(
            """INSERT OR IGNORE INTO daily_context_projection_state
               (context_date, projection_hash, revision, updated_at) VALUES (?, ?, 1, ?)""",
            (date_iso, digest, stamp),
        )


def _row_to_dict(row):
    if row is None:
        return None
    return {"date": row["date"], "notes": row["notes"], "updated_at": row["updated_at"]}


def get_log(conn, date_iso):
    """Возвращает dict daily context за дату или None, если его нет."""
    cur = conn.execute("SELECT * FROM daily_log WHERE date = ?", (date_iso,))
    return _row_to_dict(cur.fetchone())


def has_log(date_iso, conn=None):
    """Заполнен ли уже лог за эту дату (есть строка с непустыми notes)."""
    own = conn is None
    if own:
        conn = connect()
    try:
        cur = conn.execute("SELECT notes FROM daily_log WHERE date = ?", (date_iso,))
        row = cur.fetchone()
        return row is not None and (row[0] or "").strip() != ""
    finally:
        if own:
            conn.close()


def _clean_notes(data):
    v = data.get("notes")
    if v is None:
        return None
    v = str(v).strip()
    return v or None


class ContextConflict(ValueError):
    pass


def _next_revision(conn, date_iso):
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM daily_context_entries WHERE context_date = ?",
        (date_iso,),
    ).fetchone()
    return int(row[0] or 0) + 1


def _render_entry(row):
    body = str(row["notes"] or "").strip()
    return f"{row['label']}: {body}" if row["label"] else body


def rebuild_projection_tx(conn, date_iso):
    """Deterministically rebuild the legacy row and return hash/revision/text."""
    rows = conn.execute(
        """SELECT notes, label FROM daily_context_entries
           WHERE context_date = ? AND status = 'active'
           ORDER BY revision, created_at, entry_id""",
        (date_iso,),
    ).fetchall()
    text = "\n\n".join(filter(None, (_render_entry(row) for row in rows))) or None
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    state = conn.execute(
        "SELECT revision, projection_hash FROM daily_context_projection_state WHERE context_date = ?",
        (date_iso,),
    ).fetchone()
    digest = _hash_text(text or "")
    if state is not None and state[1] == digest:
        revision = int(state[0])
    else:
        revision = int(state[0]) + 1 if state is not None else 1
    conn.execute(
        """INSERT INTO daily_context_projection_state
           (context_date, projection_hash, revision, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(context_date) DO UPDATE SET
             projection_hash=excluded.projection_hash,
             revision=excluded.revision,
             updated_at=excluded.updated_at""",
        (date_iso, digest, revision, now),
    )
    conn.execute(
        """INSERT INTO daily_log(date, notes, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET notes=excluded.notes, updated_at=excluded.updated_at""",
        (date_iso, text, now),
    )
    return {"notes": text, "projection_hash": digest, "revision": revision}


def projection_state(conn, date_iso):
    row = conn.execute(
        """SELECT p.context_date, p.projection_hash, p.revision, p.updated_at, d.notes
           FROM daily_context_projection_state p
           LEFT JOIN daily_log d ON d.date=p.context_date WHERE p.context_date=?""",
        (date_iso,),
    ).fetchone()
    return dict(row) if row is not None else None


def append_entry_tx(conn, date_iso, notes, *, label=None, source_key,
                    origin_action_id=None):
    """Append one immutable source entry. Caller owns the transaction."""
    dt.date.fromisoformat(date_iso)
    body = str(notes or "").strip()
    key = str(source_key or "").strip()
    if not body or not key:
        raise ValueError("notes and source_key are required")
    label = str(label).strip() if label else None
    digest = _hash_text(body)
    existing = conn.execute(
        """SELECT entry_id, context_date, notes, label, origin_action_id, status
           FROM daily_context_entries WHERE source_key=?""",
        (key,),
    ).fetchone()
    wanted = (date_iso, body, label, origin_action_id)
    if existing is not None:
        found = (existing["context_date"], existing["notes"], existing["label"],
                 existing["origin_action_id"])
        if found != wanted:
            raise ContextConflict("source_key already identifies different context data")
        return existing["entry_id"], False, rebuild_projection_tx(conn, date_iso)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    entry_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO daily_context_entries
           (entry_id, context_date, notes, label, source_key, origin_action_id,
            revision, status, supersedes_entry_id, content_sha256, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)""",
        (entry_id, date_iso, body, label, key, origin_action_id,
         _next_revision(conn, date_iso), digest, now, now),
    )
    return entry_id, True, rebuild_projection_tx(conn, date_iso)


def replace_entry_tx(conn, entry_id, notes, *, source_key, origin_action_id=None,
                     label=None):
    old = conn.execute(
        "SELECT * FROM daily_context_entries WHERE entry_id=? AND status='active'",
        (entry_id,),
    ).fetchone()
    if old is None:
        raise ContextConflict("active context entry not found")
    new_id, inserted, _ = append_entry_tx(
        conn, old["context_date"], notes, label=label, source_key=source_key,
        origin_action_id=origin_action_id,
    )
    if inserted:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE daily_context_entries
               SET status='superseded', status_action_id=?, updated_at=? WHERE entry_id=?""",
            (origin_action_id, now, entry_id),
        )
        conn.execute(
            "UPDATE daily_context_entries SET supersedes_entry_id=? WHERE entry_id=?",
            (entry_id, new_id),
        )
    return new_id, inserted, rebuild_projection_tx(conn, old["context_date"])


def retract_entry_tx(conn, entry_id, *, origin_action_id=None):
    row = conn.execute(
        "SELECT context_date, status FROM daily_context_entries WHERE entry_id=?",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise ContextConflict("context entry not found")
    if row["status"] == "retracted":
        return False, rebuild_projection_tx(conn, row["context_date"])
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """UPDATE daily_context_entries
           SET status='retracted', status_action_id=?, updated_at=? WHERE entry_id=?""",
        (origin_action_id, now, entry_id),
    )
    return True, rebuild_projection_tx(conn, row["context_date"])


def save_log(conn, date_iso, data):
    """Upsert записи за дату. data — dict с ключом notes."""
    notes = _clean_notes(data)
    conn.execute("BEGIN IMMEDIATE") if not conn.in_transaction else None
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE daily_context_entries SET status='retracted', updated_at=? "
        "WHERE context_date=? AND status='active'",
        (now, date_iso),
    )
    if notes:
        append_entry_tx(
            conn, date_iso, notes,
            source_key=f"compat:save:{date_iso}:{_next_revision(conn, date_iso)}:{_hash_text(notes)[:16]}",
        )
    else:
        rebuild_projection_tx(conn, date_iso)
    conn.commit()
    return {"notes": notes}

def append_log(conn, date_iso, notes, label=None):
    """Добавляет context к существующей legacy-записи, не затирая её.

    Это используется для ответа на утренний вопрос: пользователь мог записать
    часть вечера заранее, а утром дополнить её после появления WHOOP-данных.
    """
    extra = str(notes or "").strip()
    if not extra:
        return get_log(conn, date_iso) or {"notes": None}
    conn.execute("BEGIN IMMEDIATE") if not conn.in_transaction else None
    _, _, projected = append_entry_tx(
        conn, date_iso, extra, label=label,
        source_key=f"compat:append:{date_iso}:{_hash_text((label or '') + chr(0) + extra)}",
    )
    conn.commit()
    return {"notes": projected["notes"]}

def append_log_tx(conn, date_iso, notes, label=None):
    """Same merge semantics as append_log but WITHOUT committing.

    Lets the conversation tools fold a daily-context write into their single
    atomic transaction. The caller owns BEGIN/COMMIT. Schema is unchanged.
    Returns the merged notes string (or existing notes when nothing was added).
    """
    extra = str(notes or "").strip()
    if not extra:
        return (get_log(conn, date_iso) or {}).get("notes") or ""
    _, _, projected = append_entry_tx(
        conn, date_iso, extra, label=label,
        source_key=f"compat:append:{date_iso}:{_hash_text((label or '') + chr(0) + extra)}",
    )
    return projected["notes"] or ""


def load_all(conn):
    """Все записи как dict {date_iso: row_dict}, отсортированный по дате."""
    out = {}
    for row in conn.execute("SELECT * FROM daily_log ORDER BY date"):
        d = _row_to_dict(row)
        out[d["date"]] = d
    return out


# ---------------- CLI (для окна ввода) ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--get", metavar="DATE", help="печатает JSON записи за дату (или {})")
    ap.add_argument("--exists", metavar="DATE", help="печатает 1/0 и возвращает код 0/1")
    ap.add_argument("--save", metavar="DATE", help="сохранить запись за дату (с --file)")
    ap.add_argument("--file", help="JSON-файл со значениями для --save")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.get:
            row = get_log(conn, args.get)
            print(json.dumps(row or {}, ensure_ascii=False))
            return
        if args.exists:
            ok = has_log(args.exists, conn)
            print("1" if ok else "0")
            sys.exit(0 if ok else 1)
        if args.save:
            if not args.file or not os.path.exists(args.file):
                raise SystemExit("--save требует существующий --file с JSON")
            with open(args.file, encoding="utf-8-sig") as f:
                data = json.load(f)
            save_log(conn, args.save, data)
            print(f"OK: сохранено за {args.save}")
            return
        ap.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
