#!/usr/bin/env python3
"""Сборщик rates.json: курсы трёх банков ПМР и два официальных в один файл.

Состав источников, ритм и формат файла зафиксированы в
Docs/Wayfinder/Tickets/browser-or-server-fetch.md, набор банков — в
Docs/Wayfinder/Tickets/bank-source-set.md. Страница читает только этот файл и
больше никуда не ходит, поэтому CORS в раздаче не участвует.

    Сбербанк        api.prisbank.com/courses, блок id:2   даты нет, changed_at считаем сами
    Агропромбанк    agroprombank.com/xmlinformer.php      блок internetbank — он и есть кассовый
    Эксимбанк       kurspmr.com + сверка с bankipmr.org   сам банк за FOXCLOUD и недоступен
    официальный ПРБ cbpmr.net/csv.php                     дата курса в первой колонке
    официальный НБМ bnm.md/...?get_xml=1                  дата курса в атрибуте Date

Из cron раз в сутки:

    5 2 * * * ~/transfer-calc/tools/fetch_rates.py >> ~/transfer-calc/rates.log 2>&1

Первый запуск — с --backfill: changed_at восстанавливается из истории источников,
иначе он соврал бы «изменился сегодня» про курс, который стоит неделю. Эксимбанку
backfill не нужен: у kurspmr вся история приходит одним ответом.

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

SCHEMA = 2  # 1 -> 2: под cash три банка вместо одного, у Эксимбанка поле disputed
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
    """Наличные курсы — блок id:2. Даты нет ни в одном из четырёх блоков.

    Блок id:7 «Спецкурс для пенсий РФ» сюда не берётся: к выдаче Юнистрима он,
    насколько известно, не применяется — см. Tickets/unistream-payout-leg.md.
    """
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


def agroprombank_url(day, live):
    # http отдаёт 301 на https; спрашиваем сразу по адресу после редиректа.
    base = "https://www.agroprombank.com/xmlinformer.php?type=all"
    return base if live else "%s&date=%s" % (base, day.isoformat())


def parse_agroprombank(raw):
    """Кассовый блок — internetbank, а не commercial: сверено историей 14 из 14
    дней, см. Tickets/bank-source-set.md. Пары идут к RUP, их и берём.

    Атрибут date — эхо запроса, а не дата установки курса, поэтому rate_date None.
    """
    root = ET.fromstring(raw.decode("cp1251", errors="replace"))
    block = next(
        (c for c in root.findall("course") if c.get("type") == "internetbank"), None
    )
    if block is None:
        raise ValueError("agroprombank: block type=internetbank not found")
    rates = {}
    for currency in block.findall("currency"):
        code = currency.get("code")
        if code in CURRENCIES and currency.get("codeBuy") == "RUP":
            rates[code] = {
                "buy": norm(currency.findtext("currencyBuy"), 1),
                "sale": norm(currency.findtext("currencySell"), 1),
            }
    require_all(rates, "agroprombank")
    return rates, None


KURSPMR_URL = "https://kurspmr.com/grafik-kursov-valjut?bank=4&val=%s"
KURSPMR_HISTORY = re.compile(r"rawHistory\s*=\s*(\[.*?\])\s*;", re.S)
BANKIPMR_URL = "https://bankipmr.org/"
BANKIPMR_TABLE = re.compile(r'<table class="table_([a-z]+)"(.*?)</table>', re.S)
BANKIPMR_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
BANKIPMR_CELL = re.compile(r'aria-label="([a-z]{3})_([a-z]+)">([\d.]+)<')


def kurspmr_series():
    """История Эксимбанка по каждой валюте: {валюта: [(дата, buy, sale), ...]}.

    Ряды у валют разной длины и с разными датами — у рубля их впятеро больше,
    чем у лея, — поэтому каждый разбирается сам по себе.
    """
    series = {}
    for code in CURRENCIES:
        raw = fetch(KURSPMR_URL % code).decode("utf-8", errors="replace")
        found = KURSPMR_HISTORY.search(raw)
        if not found:
            raise ValueError("kurspmr: rawHistory for %s not found" % code)
        points = json.loads(found.group(1))
        if not points:
            raise ValueError("kurspmr: empty history for %s" % code)
        series[code] = [
            (p["date"], norm(p["buy"], 1), norm(p["sell"], 1)) for p in points
        ]
        time.sleep(HISTORY_PAUSE)
    return series


def parse_bankipmr(raw):
    """Второй агрегатор — только ради сверки. Строку Эксимбанка ищем по названию:
    порядок банков в таблице не обещан."""
    text = raw.decode("utf-8", errors="replace")
    rates = {}
    for table in BANKIPMR_TABLE.finditer(text):
        side = "sale" if table.group(1) == "sell" else "buy"
        for row in BANKIPMR_ROW.finditer(table.group(2)):
            if "ксим" not in row.group(1):
                continue
            for code, _, value in BANKIPMR_CELL.findall(row.group(1)):
                code = code.upper()
                if code in CURRENCIES:
                    rates.setdefault(code, {})[side] = norm(value, 1)
    require_all(rates, "bankipmr")
    return rates


def eximbank_changed_at(series, current):
    """Самая ранняя дата, на которую нынешние числа уже стояли. По каждой валюте
    отдельно, затем берём последнюю из них: изменилась одна — изменился набор."""
    marks = []
    for code, points in series.items():
        now = (current[code]["buy"], current[code]["sale"])
        oldest = points[-1][0]
        for date, buy, sale in reversed(points):
            if (buy, sale) != now:
                break
            oldest = date
        marks.append(oldest)
    return max(marks)


def load_eximbank(day, live):
    """Сам банк за антибот-заглушкой FOXCLOUD, поэтому два агрегатора со сверкой:
    расхождение — не повод молчать, а повод пометить (Tickets/bank-source-set.md).
    """
    series = kurspmr_series()
    rates = {}
    for code, points in series.items():
        _, buy, sale = points[-1]
        rates[code] = {"buy": buy, "sale": sale}
    require_all(rates, "eximbank")

    note = {"origin": "kurspmr.com", "disputed": False, "cross_check": None}
    try:
        other = parse_bankipmr(fetch(BANKIPMR_URL))
    except Exception as exc:
        note["cross_check"] = "unavailable: %s" % describe_error(exc)
    else:
        clashes = [c for c in CURRENCIES if other[c] != rates[c]]
        if clashes:
            note["disputed"] = True
            note["cross_check"] = "bankipmr.org disagrees on %s: %s" % (
                ", ".join(clashes),
                json.dumps({c: other[c] for c in clashes}),
            )
        else:
            note["cross_check"] = "bankipmr.org agrees"
    return rates, None, eximbank_changed_at(series, rates), note


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


def simple(url_of, parse):
    """Источник, у которого один запрос на день: история берётся тем же вызовом
    с другой датой, поэтому changed_at считает общий backfill."""

    def load(day, live):
        rates, rate_date = parse(fetch(url_of(day, live)))
        return rates, rate_date, None, None

    load.replayable = True
    return load


load_eximbank.replayable = False  # свою историю отдаёт сам, backfill не нужен

SOURCES = (
    # путь в rates.json, чем загружается
    ("cash.prisbank", simple(prisbank_url, parse_prisbank_cash)),
    ("cash.agroprombank", simple(agroprombank_url, parse_agroprombank)),
    ("cash.eximbank", load_eximbank),
    ("prb", simple(cbpmr_url, parse_cbpmr)),
    ("nbm", simple(bnm_url, parse_bnm)),
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


def backfill_changed_at(load, current, today, max_days, label):
    """Идём назад по дням, пока курс совпадает с сегодняшним. Возвращаем самую
    раннюю дату, на которую нынешнее значение уже стояло."""
    oldest = None
    for back in range(1, max_days + 1):
        day = today - timedelta(days=back)
        try:
            rates, rate_date, _, _ = load(day, False)
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
    for path, load in SOURCES:
        prev = dig(previous.get("sources", {}), path)
        try:
            rates, rate_date, own_changed_at, note = load(today, True)
            error = None
        except Exception as exc:
            rates, rate_date, own_changed_at, note = None, None, None, None
            error = describe_error(exc)

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
            say("  %-18s ОТКАЗ  %s" % (path, error))
        else:
            # Дата, в которой выражается changed_at: для ПРБ и НБМ — дата самого
            # курса, для наличных её нет, поэтому день наблюдения.
            basis = rate_date or today.isoformat()
            if own_changed_at:
                changed_at = own_changed_at
            elif prev and prev.get("rates") == rates:
                changed_at = prev.get("changed_at") or basis
            elif prev and prev.get("rates"):
                changed_at = basis
            elif backfill_days and getattr(load, "replayable", False):
                found, complete = backfill_changed_at(
                    load, rates, today, backfill_days, path
                )
                changed_at = found or basis
                if found and not complete:
                    say("  %-18s курс не менялся все %d дней истории — "
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
            }, note)
            flag = "  СПОРНО" if note and note.get("disputed") else ""
            say("  %-18s ok     rate_date=%-10s changed_at=%s%s"
                % (path, rate_date or "—", changed_at, flag))
        plant(sources, path, entry)
    return sources, failures


def order_entry(entry, note=None):
    """Порядок ключей фиксирован, чтобы diff файла читался глазами."""
    keys = ("ok", "fetched_at", "rate_date", "changed_at", "error", "rates")
    out = {k: entry.get(k) for k in keys}
    for extra in ("origin", "disputed", "cross_check"):
        if note and extra in note:
            out[extra] = note[extra]
        elif extra in entry:
            out[extra] = entry[extra]
    return out


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
    ap = argparse.ArgumentParser(description="Собрать rates.json из пяти источников")
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
