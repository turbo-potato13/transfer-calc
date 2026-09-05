#!/usr/bin/env python3
"""Сборщик rates.json: три источника курсов в один файл на своём origin.

Состав источников, ритм и формат файла зафиксированы в
Docs/Wayfinder/Tickets/browser-or-server-fetch.md. Страница читает только этот
файл и больше никуда не ходит, поэтому CORS в раздаче не участвует.

    наличные       api.prisbank.com/courses, блок id:2   даты нет, changed_at считаем сами
    официальный ПРБ cbpmr.net/csv.php                     дата курса в первой колонке
    официальный НБМ bnm.md/...?get_xml=1                  дата курса в атрибуте Date

Из cron раз в сутки:

    5 2 * * * ~/transfer-calc/tools/fetch_rates.py >> ~/transfer-calc/rates.log 2>&1

Первый запуск — с --backfill: changed_at восстанавливается из истории источников,
иначе он соврал бы «изменился сегодня» про курс, который стоит неделю.

Только стандартная библиотека: на машине 1 ГБ памяти и она делится с ботом.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = 1
TZ = ZoneInfo("Europe/Chisinau")  # сервер в UTC; до 03:00 по нему ещё вчера
CURRENCIES = ("RUB", "EUR", "USD", "MDL")
UA = "transfer-calc/1.0 (+https://github.com/turbo-potato13/transfer-calc)"
TIMEOUT = 30
HISTORY_PAUSE = 0.3  # пауза между запросами истории при --backfill

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "site" / "rates.json"
# Состояние — вне site/, иначе раздалось бы наружу вместе с сайтом.
DEFAULT_STATE = Path.home() / ".local" / "state" / "transfer-calc" / "previous.json"


# --- сеть -------------------------------------------------------------------


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def describe_error(exc):
    """Сырой текст по-английски: он уходит в файл как есть, в панель диагностики."""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %d %s" % (exc.code, exc.reason)
    if isinstance(exc, urllib.error.URLError):
        return "URLError %s" % (exc.reason,)
    return "%s: %s" % (type(exc).__name__, exc)


# --- разбор источников ------------------------------------------------------


def norm(value, nominal):
    """Курс за одну единицу. Номинал не гипотеза: у cbpmr CNY идёт по 10."""
    return round(float(value) / float(nominal), 6)


def require_all(rates, who):
    missing = [c for c in CURRENCIES if c not in rates]
    if missing:
        raise ValueError("%s: no rate for %s" % (who, ", ".join(missing)))


def prisbank_url(day, live):
    base = "https://api.prisbank.com/courses"
    return base if live else "%s?date=%s" % (base, day.isoformat())


def parse_prisbank_cash(raw):
    """Наличные курсы — блок id:2. Даты нет ни в одном из четырёх блоков."""
    blocks = json.loads(raw.decode("utf-8"))
    cash = next((b for b in blocks if b.get("id") == 2), None)
    if cash is None:
        raise ValueError("prisbank: block id=2 (cash rates) not found")
    rates = {}
    for course in cash.get("courses", []):
        code = course.get("abbr")
        if code in CURRENCIES:
            nominal = course.get("tarif") or 1
            rates[code] = {
                "buy": norm(course["buy"], nominal),
                "sale": norm(course["sale"], nominal),
            }
    require_all(rates, "prisbank")
    return rates, None


def cbpmr_url(day, live):
    return "https://cbpmr.net/csv.php?vid=val&date=%s&lang=ru" % day.isoformat()


# Дата, название (в кавычках, может быть с пробелами), код, номинал, курс, цифровой код.
# Якорь на хвост строки: название в cp1251 и нас не интересует.
CSV_ROW = re.compile(r"^(\d{2}\.\d{2}\.\d{4}),.*,([A-Z]{3}),(\d+),([\d.]+),\d+$")


def parse_cbpmr(raw):
    """CSV в cp1251; названия валют не нужны, поэтому кодировка роли не играет."""
    text = raw.decode("cp1251", errors="replace")
    rates = {}
    rate_date = None
    for line in text.splitlines():
        found = CSV_ROW.match(line.strip())
        if not found:
            continue
        day, code, nominal, value = found.groups()
        if rate_date is None:
            rate_date = datetime.strptime(day, "%d.%m.%Y").date().isoformat()
        if code in CURRENCIES:
            rates[code] = norm(value, nominal)
    if rate_date is None:
        raise ValueError("cbpmr: no parsable rows")
    require_all(rates, "cbpmr")
    return rates, rate_date


def bnm_url(day, live):
    return (
        "https://bnm.md/ru/official_exchange_rates?get_xml=1&date=%s"
        % day.strftime("%d.%m.%Y")
    )


def parse_bnm(raw):
    """MDL пишем явно: в списке НБМ его нет, а странице нужен кросс-курс
    без ветки-исключения."""
    root = ET.fromstring(raw)
    stamp = root.get("Date")
    rate_date = (
        datetime.strptime(stamp, "%d.%m.%Y").date().isoformat() if stamp else None
    )
    rates = {"MDL": 1.0}
    for valute in root.findall("Valute"):
        code = valute.findtext("CharCode")
        if code in CURRENCIES:
            rates[code] = norm(valute.findtext("Value"), valute.findtext("Nominal") or 1)
    require_all(rates, "bnm")
    return rates, rate_date


SOURCES = (
    # путь в rates.json, как строится url, как разбирается ответ
    ("cash.prisbank", prisbank_url, parse_prisbank_cash),
    ("prb", cbpmr_url, parse_cbpmr),
    ("nbm", bnm_url, parse_bnm),
)


# --- сборка файла -----------------------------------------------------------


def dig(doc, path):
    node = doc
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def plant(doc, path, value):
    parts = path.split(".")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def backfill_changed_at(url_of, parse, current, today, max_days, label):
    """Идём назад по дням, пока курс совпадает с сегодняшним. Возвращаем самую
    раннюю дату, на которую нынешнее значение уже стояло."""
    oldest = None
    for back in range(1, max_days + 1):
        day = today - timedelta(days=back)
        try:
            rates, rate_date = parse(fetch(url_of(day, False)))
        except Exception as exc:  # история — дело необязательное, обрыв не смертелен
            say("  %s: история оборвалась на %s — %s" % (label, day, describe_error(exc)))
            break
        if rates != current:
            return oldest, True
        # У ПРБ и НБМ в выходной вернётся дата последнего рабочего дня — она и верна.
        oldest = rate_date or day.isoformat()
        time.sleep(HISTORY_PAUSE)
    return oldest, False


def collect(today, previous, backfill_days):
    sources = {}
    failures = 0
    for path, url_of, parse in SOURCES:
        prev = dig(previous.get("sources", {}), path)
        try:
            rates, rate_date = parse(fetch(url_of(today, True)))
            error = None
        except Exception as exc:
            rates, rate_date, error = None, None, describe_error(exc)

        if error is not None:
            failures += 1
            # Отказ не стирает значения: страница считает по прошлым числам
            # и показывает маркер у поля.
            entry = dict(prev) if prev else {
                "fetched_at": None,
                "rate_date": None,
                "changed_at": None,
                "rates": None,
            }
            entry["ok"] = False
            entry["error"] = error
            entry = order_entry(entry)
            say("  %-14s ОТКАЗ  %s" % (path, error))
        else:
            # Дата, в которой выражается changed_at: для ПРБ и НБМ — дата самого
            # курса, для наличных её нет, поэтому день наблюдения.
            basis = rate_date or today.isoformat()
            if prev and prev.get("rates") == rates:
                changed_at = prev.get("changed_at") or basis
            elif prev and prev.get("rates"):
                changed_at = basis
            elif backfill_days:
                found, complete = backfill_changed_at(
                    url_of, parse, rates, today, backfill_days, path
                )
                changed_at = found or basis
                if found and not complete:
                    say("  %-14s курс не менялся все %d дней истории — "
                        "changed_at не позже %s" % (path, backfill_days, changed_at))
            else:
                changed_at = basis
            entry = order_entry({
                "ok": True,
                "fetched_at": now_iso(),
                "rate_date": rate_date,
                "changed_at": changed_at,
                "error": None,
                "rates": rates,
            })
            say("  %-14s ok     rate_date=%-10s changed_at=%s"
                % (path, rate_date or "—", changed_at))
        plant(sources, path, entry)
    return sources, failures


def order_entry(entry):
    """Порядок ключей фиксирован, чтобы diff файла читался глазами."""
    keys = ("ok", "fetched_at", "rate_date", "changed_at", "error", "rates")
    return {k: entry.get(k) for k in keys}


def now_iso():
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # подмена атомарна: страница не поймает полфайла


def say(text):
    print(text, flush=True)


def main():
    ap = argparse.ArgumentParser(description="Собрать rates.json из трёх источников")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--backfill", nargs="?", type=int, const=40, default=0,
                    metavar="ДНЕЙ",
                    help="восстановить changed_at из истории (по умолчанию 40 дней); "
                         "имеет смысл только при первом запуске")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать файл, ничего не записывая")
    args = ap.parse_args()

    today = datetime.now(TZ).date()
    say("%s  сегодня в Кишинёве %s" % (now_iso(), today))

    previous = {}
    if args.state.exists():
        try:
            previous = json.loads(args.state.read_text(encoding="utf-8"))
        except ValueError as exc:
            say("  состояние нечитаемо (%s) — считаем запуск первым" % exc)

    sources, failures = collect(today, previous, args.backfill)
    doc = {"schema": SCHEMA, "generated_at": now_iso(), "sources": sources}

    if args.dry_run:
        say(json.dumps(doc, ensure_ascii=False, indent=2))
        return 1 if failures else 0

    write_json(args.out, doc)
    write_json(args.state, doc)
    say("  записан %s (%d Б), состояние %s"
        % (args.out, args.out.stat().st_size, args.state))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
