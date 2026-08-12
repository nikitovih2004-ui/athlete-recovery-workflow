"""
Импорт вечерних записей из Google Формы (ответы опубликованы как CSV).

Форма заполняется с телефона; ответы падают в Google Таблицу, опубликованную как
CSV. Скрипт скачивает этот CSV и для каждой строки (дата + свободный текст
«Что было вчера вечером») добавляет/обновляет запись в daily_log:
  * если запись за эту дату уже есть — обновляет текст;
  * если нет — создаёт новую.
Несколько ответов за одну дату → выигрывает последний (строки идут в порядке отправки).

Устойчив к сети: если ссылка временно недоступна или ответ битый — логирует
и тихо выходит (код 0), не роняя утренний цикл.

    python import_evening_log.py            # обычный запуск
    python import_evening_log.py --url URL  # переопределить источник CSV
    python import_evening_log.py --dry-run  # показать, что будет импортировано, но не писать в базу

Встроен в утренний цикл (morning_flow.py), поэтому записи с телефона
подтягиваются автоматически вместе с данными WHOOP каждое утро. Ручное окно
ввода на ПК (evening_log.ps1) было удалено вместе со старым Windows-циклом —
сейчас запасного варианта ручного ввода через .ps1-окно не осталось.
"""
import os
import sys
import io
import csv
import argparse
import datetime as dt

import requests

import daily_log

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "import_evening_log.log")

# Публичный CSV Google-таблицы с ответами формы (можно переопределить --url или env).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

# Keep this URL outside version control: published form responses may contain sensitive notes.
DEFAULT_CSV_URL = os.environ.get("WHOOP_EVENING_CSV_URL", "").strip()

# Форматы даты, которые может отдать поле «дата» Google-формы. Порядок = приоритет.
# Для ru-локали обычно ДД.ММ.ГГГГ; strptime отбракует невалидные (напр. месяц 25),
# что само разруливает часть неоднозначности день/месяц.
DATE_FORMATS = [
    "%Y-%m-%d",   # 2026-07-03 (ISO)
    "%d.%m.%Y",   # 03.07.2026 (русская локаль)
    "%d.%m.%y",   # 03.07.26
    "%d/%m/%Y",   # 03/07/2026
    "%m/%d/%Y",   # 07/03/2026 (US-дефолт Google)
    "%Y/%m/%d",   # 2026/07/03
]


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_date(raw):
    """Строка даты из формы → ISO 'YYYY-MM-DD' или None, если не распозналась."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # поле даты может прийти со временем ('03.07.2026 0:00:00') — берём первый токен
    token = s.split()[0]
    for cand in (s, token):
        for fmt in DATE_FORMATS:
            try:
                return dt.datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def find_columns(header):
    """Определяет индексы колонок «дата» и «текст записи» по заголовкам
    (устойчиво к лишним пробелам, регистру и небольшим переименованиям)."""
    norm = [(h or "").strip().lower() for h in header]

    date_idx = None
    for i, h in enumerate(norm):
        if h == "дата":
            date_idx = i
            break
    if date_idx is None:
        for i, h in enumerate(norm):
            if "дата" in h and "время" not in h and "отметка" not in h:
                date_idx = i
                break

    text_idx = None
    for i, h in enumerate(norm):
        if "вечер" in h or "что было" in h:
            text_idx = i
            break

    # позиционный запасной вариант: у ru-формы обычно [отметка времени, email, дата, текст]
    if date_idx is None and len(header) >= 3:
        date_idx = 2
    if text_idx is None and len(header) >= 4:
        text_idx = len(header) - 1

    return date_idx, text_idx


def download_csv(url):
    """Скачивает CSV. Возвращает текст или None при любой сетевой/HTTP-ошибке."""
    import time
    try_count = 4
    for attempt in range(try_count):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            # utf-8-sig снимает BOM, если он есть
            return resp.content.decode("utf-8-sig", errors="replace")
        except Exception as e:
            if attempt < try_count - 1:
                time.sleep(5)
            else:
                log(f"не удалось скачать CSV ({type(e).__name__}: {e}) — пропускаю попытку")
                return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_CSV_URL, help="ссылка на опубликованный CSV")
    ap.add_argument("--dry-run", action="store_true", help="не писать в базу, только показать")
    args = ap.parse_args()

    url = (args.url or "").strip()
    if not url:
        log("WHOOP_EVENING_CSV_URL is not configured; skipping evening-log import")
        return

    text = download_csv(url)
    if text is None:
        return  # тихий выход, код 0 — утренний цикл не падает

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        log("CSV пустой — нечего импортировать")
        return

    header = rows[0]
    date_idx, text_idx = find_columns(header)
    if date_idx is None or text_idx is None:
        log(f"не нашёл колонки даты/текста в заголовке: {header}")
        return

    data_rows = rows[1:]
    if not data_rows:
        log("в форме пока нет ни одного ответа — импортировать нечего")
        return

    conn = daily_log.connect()
    created = updated = skipped = 0
    try:
        for row in data_rows:
            if len(row) <= max(date_idx, text_idx):
                skipped += 1
                continue
            iso = parse_date(row[date_idx])
            note = (row[text_idx] or "").strip()
            if not iso:
                skipped += 1
                log(f"пропуск строки: не распознал дату {row[date_idx]!r}")
                continue
            if not note:
                skipped += 1
                continue
            existed = daily_log.has_log(iso, conn)
            if args.dry_run:
                log(f"[dry-run] {'обновил бы' if existed else 'создал бы'} {iso}: "
                    f"{note[:60]}{'…' if len(note) > 60 else ''}")
            else:
                daily_log.save_log(conn, iso, {"notes": note})
            if existed:
                updated += 1
            else:
                created += 1
    finally:
        conn.close()

    tag = "[dry-run] " if args.dry_run else ""
    log(f"{tag}импорт завершён: создано {created}, обновлено {updated}, пропущено {skipped}")


if __name__ == "__main__":
    main()
