#!/usr/bin/env python3
"""Слежение за местом в конкурсных списках МАИ (priem.mai.ru/rating).

Как это работает: страница /rating/ — каскад из пяти <select>, каждый из
которых подгружает статический фрагмент с public.mai.ru. Идентификаторы
фрагментов содержат метку снимка (p20260820130009_...) и меняются раз в час,
поэтому цепочка каждый раз разрешается заново — по видимым названиям пунктов,
а не по захардкоженным URL.

Галочки в блоке «Отбор» — клиентский фильтр: сайт прячет строки с CSS-классом
(not_pr и т.п.) и заново нумерует оставшиеся. Здесь это повторено один в один.

Запуск:
    python3 mai_rating.py            # цикл, проверка раз в 5 минут
    python3 mai_rating.py --once     # одна проверка и выход
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import re
import shutil
import subprocess
import sys
import time
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

RATING_URL = "https://priem.mai.ru/rating/"
DATA_URL = "https://public.mai.ru/priem/rating/data/{value}.html"

# public.mai.ru отдаёт 404 на User-Agent по умолчанию из urllib.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ─── что отслеживаем ────────────────────────────────────────────────────────
# УКП намеренно не хранится в коде — задаётся ключом --ukp или переменной
# окружения UKP, чтобы не попадать в репозиторий.
SELECTION = [
    "МАИ",
    "Специализированное высшее образование",
    "Бюджет",
    "Очная",
    "Информатика и вычислительная техника",
]
# Ключи из FILTER_CLASSES; «Высший проходной приоритет» — это prior.
FILTERS = frozenset({"prior"})
INTERVAL_SECONDS = 300
HISTORY_PATH = Path(__file__).resolve().parent / "history.csv"
# ────────────────────────────────────────────────────────────────────────────

# Галочка «Отбор» -> CSS-класс строк, которые она прячет (см. window.filter на сайте).
FILTER_CLASSES = {
    "sogl": "not_sogl",  # С согласием / договором
    "osn": "not_osn",  # Основной высший приоритет
    "prior": "not_pr",  # Высший проходной приоритет
    "dorm": "not_dorm",  # Нуждаемость в общежитии
    "zachisl": "not_zachisl",  # Зачисленные
    "not_zachisl": "zachislen",  # Без зачисленных
}

HISTORY_COLUMNS = [
    "checked_at",
    "generated_at",
    "section",
    "rank",
    "total",
    "places",
    "score",
    "priority",
    "consent",
    "enrolled",
]


# ─── разбор HTML ────────────────────────────────────────────────────────────


def clean(fragment: str) -> str:
    """Текст ячейки: без тегов, без &nbsp;, со схлопнутыми пробелами."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_module.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().casefold().replace("ё", "е")


def parse_options(html: str) -> list[tuple[str, str]]:
    """Пары (value, подпись) из <option>, без пустышки «---»."""
    found = re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', html, re.S)
    return [(value, clean(label)) for value, label in found if value and value != "0"]


def pick_option(options: list[tuple[str, str]], label: str) -> str:
    wanted = normalize(label)
    for value, text in options:
        if normalize(text) == wanted:
            return value
    available = ", ".join(f"«{text}»" for _, text in options) or "(пусто)"
    raise LookupError(f"пункт «{label}» не найден; доступны: {available}")


@dataclass(frozen=True)
class Row:
    ukp: str
    classes: frozenset[str]
    cells: tuple[str, ...]
    headers: tuple[str, ...]

    def cell(self, column: str) -> str:
        """Значение по названию столбца; отсутствующая ячейка — пустая строка."""
        wanted = normalize(column)
        for index, header in enumerate(self.headers):
            if normalize(header) == wanted:
                return self.cells[index] if index < len(self.cells) else ""
        return ""

    def is_checked(self, column: str) -> bool:
        return self.cell(column) == "✓"


@dataclass(frozen=True)
class Section:
    """Один <tbody class="data"> — сайт нумерует строки внутри каждого отдельно."""

    title: str
    places: int | None
    rows: tuple[Row, ...]


def _headers_before(html: str, position: int) -> tuple[str, ...]:
    """Шапка таблицы, к которой относится <tbody> в указанной позиции.

    У целевой квоты шапка двухуровневая: за основной строкой идёт ещё одна
    (<th colspan="9">Заказчик</th>), поэтому берётся не последняя строка с <th>,
    а последняя содержательная — с тремя и более ячейками.
    """
    header_rows = [
        m for m in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", html, re.S)
        if "<th" in m.group(1) and m.end() <= position
    ]
    if not header_rows:
        return ()
    cell_lists = [re.findall(r"<th\b[^>]*>(.*?)</th>", m.group(1), re.S) for m in header_rows]
    full = [cells for cells in cell_lists if len(cells) >= 3]
    chosen = full[-1] if full else cell_lists[-1]
    return tuple(clean(cell) for cell in chosen)


def _title_before(html: str, position: int) -> str:
    titles = [m for m in re.finditer(r"<h4\b[^>]*>(.*?)</h4>", html, re.S) if m.end() <= position]
    return clean(titles[-1].group(1)) if titles else ""


def _places_from_title(title: str) -> int | None:
    match = re.search(r"мест:?\s*(\d+)", title)
    return int(match.group(1)) if match else None


def parse_sections(html: str) -> list[Section]:
    sections = []
    for body in re.finditer(r'<tbody class="data">(.*?)</tbody>', html, re.S):
        headers = _headers_before(html, body.start())
        title = _title_before(html, body.start())
        rows = []
        for row in re.finditer(r'<tr class="(persrow[^"]*)"[^>]*>(.*?)</tr>', body.group(1), re.S):
            cells = tuple(
                clean(cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row.group(2), re.S)
            )
            ukp_match = re.search(r"<nobr>(\d+)</nobr>", row.group(2))
            ukp = ukp_match.group(1) if ukp_match else (cells[1] if len(cells) > 1 else "")
            rows.append(
                Row(ukp=ukp, classes=frozenset(row.group(1).split()), cells=cells, headers=headers)
            )
        sections.append(Section(title=title, places=_places_from_title(title), rows=tuple(rows)))
    return sections


def parse_generated_at(html: str) -> str | None:
    match = re.search(
        r"Дата формирования\s*-\s*([\d.]+)\.\s*Время формирования\s*-\s*([\d:]+)", html
    )
    return f"{match.group(1)} {match.group(2)}" if match else None


def visible_rows(rows, active_filters: frozenset[str]) -> list[Row]:
    """Строки, которые останутся видимыми при выбранных галочках «Отбор»."""
    hidden = {FILTER_CLASSES[name] for name in active_filters}
    return [row for row in rows if not (row.classes & hidden)]


@dataclass(frozen=True)
class Standing:
    ukp: str
    rank: int
    total: int
    places: int | None
    section: str
    score: str
    priority: str
    consent: bool
    enrolled: bool
    generated_at: str | None

    def describe(self) -> str:
        if self.places is None:
            position = f"место {self.rank} из {self.total}"
        elif self.places == self.total:
            position = f"место {self.rank} из {self.places} мест"
        else:
            position = f"место {self.rank} из {self.total} (мест {self.places})"
        return (
            f"{position} · балл {self.score} · "
            f"приоритет {self.priority} · согласие {'✓' if self.consent else '—'}"
            f"{' · ЗАЧИСЛЕН' if self.enrolled else ''} · снимок {self.generated_at or '?'}"
        )


def find_standing(html: str, ukp: str, active_filters: frozenset[str]) -> Standing | None:
    """Позиция абитуриента так, как её показал бы сайт с этими галочками."""
    generated_at = parse_generated_at(html)
    for section in parse_sections(html):
        visible = visible_rows(section.rows, active_filters)
        for rank, row in enumerate(visible, start=1):
            if row.ukp != ukp:
                continue
            return Standing(
                ukp=ukp,
                rank=rank,
                total=len(visible),
                places=section.places,
                section=section.title,
                score=row.cell("Сумма конкурсных баллов"),
                priority=row.cell("Приоритет"),
                consent=row.is_checked("Согласие"),
                enrolled="zachislen" in row.classes or row.is_checked("Зачислен"),
                generated_at=generated_at,
            )
    return None


def has_changed(previous: Standing | None, current: Standing | None) -> bool:
    """Сравнение без учёта времени снимка — интересуют только сами данные."""

    def key(standing):
        if standing is None:
            return None
        return (
            standing.rank,
            standing.total,
            standing.places,
            standing.section,
            standing.score,
            standing.priority,
            standing.consent,
            standing.enrolled,
        )

    return key(previous) != key(current)


# ─── сеть ───────────────────────────────────────────────────────────────────


def fetch(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def resolve_selection(fetch_page, labels: list[str]) -> str:
    """Спуск по каскаду селектов: возвращает value последнего выбранного пункта."""
    value = None
    for step, label in enumerate(labels):
        url = RATING_URL if value is None else DATA_URL.format(value=value)
        try:
            value = pick_option(parse_options(fetch_page(url)), label)
        except LookupError as error:
            raise LookupError(f"шаг {step + 1} ({label}): {error}") from error
    return value


def check_once(ukp: str, fetch_page=fetch, selection=None, filters=FILTERS):
    """Одна проверка: разрешить каскад, скачать таблицу, найти абитуриента."""
    value = resolve_selection(fetch_page, list(selection or SELECTION))
    table_html = fetch_page(DATA_URL.format(value=value))
    return find_standing(table_html, ukp, filters)


# ─── вывод ──────────────────────────────────────────────────────────────────


def append_history(path: Path, standing: Standing) -> None:
    path = Path(path)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(HISTORY_COLUMNS)
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                standing.generated_at or "",
                standing.section,
                standing.rank,
                standing.total,
                standing.places or "",
                standing.score,
                standing.priority,
                "да" if standing.consent else "нет",
                "да" if standing.enrolled else "нет",
            ]
        )


def read_last_history(path: Path) -> Standing | None:
    """Последняя записанная позиция — состояние между запусками с --once."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("rank")]
    if not rows:
        return None
    last = rows[-1]
    return Standing(
        ukp="",
        rank=int(last["rank"]),
        total=int(last["total"]),
        places=int(last["places"]) if last["places"] else None,
        section=last["section"],
        score=last["score"],
        priority=last["priority"],
        consent=last["consent"] == "да",
        enrolled=last["enrolled"] == "да",
        generated_at=last["generated_at"] or None,
    )


def _post_form(url: str, data: dict) -> None:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30):
        pass


def send_telegram(token: str, chat_id: str, text: str, silent: bool = False, post=_post_form) -> bool:
    """Сообщение в Telegram. Падение сети не должно ронять проверку.

    silent=True — сообщение приходит без звука и без всплывающего уведомления
    (так шлются рутинные отчёты «ничего не изменилось»).
    """
    data = {"chat_id": chat_id, "text": text}
    if silent:
        data["disable_notification"] = "true"
    try:
        post(f"https://api.telegram.org/bot{token}/sendMessage", data)
        return True
    except Exception as error:  # noqa: BLE001 — уведомление не важнее самой проверки
        log(f"не удалось отправить в Telegram: {error}")
        return False


def notify(title: str, body: str) -> None:
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.run(["notify-send", title, body], check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        log(f"не удалось показать уведомление: {error}")


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def change_message(previous: Standing | None, current: Standing | None) -> str:
    if current is None:
        return "УКП пропал из списка с текущими фильтрами"
    if previous is None:
        return f"появился в списке: {current.describe()}"
    if previous.rank != current.rank:
        arrow = "↑" if current.rank < previous.rank else "↓"
        return f"{arrow} место {previous.rank} → {current.rank} из {current.total}"
    return f"изменились данные: {current.describe()}"


# ─── цикл ───────────────────────────────────────────────────────────────────


def telegram_credentials() -> tuple[str, str]:
    return os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_CHAT_ID", "")


def report(args, previous: Standing | None, current: Standing | None) -> None:
    """Итог проверки: в лог, в историю уже записано, в Telegram — всегда.

    Рутинный отчёт уходит беззвучно, изменение — обычным сообщением со звуком.
    Уведомление на рабочий стол показывается только при изменении.
    """
    changed = has_changed(previous, current)
    line = current.describe() if current else "УКП не найден в списке с текущими фильтрами"
    log(line)

    if changed:
        message = change_message(previous, current)
        log(f"ИЗМЕНЕНИЕ: {message}")
        text = f"МАИ · УКП {args.ukp}\n{message}\n{line}"
        if not args.no_notify:
            notify(f"МАИ · УКП {args.ukp}", message)
    else:
        text = f"МАИ · УКП {args.ukp}\nбез изменений · {line}"

    token, chat_id = telegram_credentials()
    if token and chat_id:
        send_telegram(token, chat_id, text, silent=not changed)
    elif changed:
        log("Telegram пропущен: не заданы TG_BOT_TOKEN и TG_CHAT_ID")


def run_test_notification(args) -> int:
    """Проверка каналов уведомлений без ожидания настоящего изменения."""
    message = "Проверка связи: парсер конкурсных списков МАИ настроен верно."
    log(message)
    if not args.no_notify:
        notify("МАИ · проверка связи", message)
    token, chat_id = telegram_credentials()
    if not (token and chat_id):
        log("не заданы TG_BOT_TOKEN и TG_CHAT_ID — отправлять некуда")
        return 1
    if not send_telegram(token, chat_id, message):
        return 1
    log("сообщение отправлено в Telegram")
    return 0


def run(args) -> int:
    # Предыдущее состояние берётся из истории, а не только из памяти: иначе
    # запуск с --once (cron, GitHub Actions) не смог бы заметить изменение.
    previous: Standing | None = read_last_history(args.csv)
    while True:
        try:
            standing = check_once(ukp=args.ukp, filters=frozenset(args.filters))
            if standing is not None:
                append_history(args.csv, standing)
            report(args, previous, standing)
            previous = standing
        except urllib.error.HTTPError as error:
            log(f"HTTP {error.code} при запросе {error.url} — пробую в следующий раз")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            log(f"сеть недоступна ({error}) — пробую в следующий раз")
        except LookupError as error:
            log(f"структура страницы изменилась: {error}")

        if args.once:
            return 0
        time.sleep(args.interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Слежение за местом в списках МАИ")
    parser.add_argument(
        "--ukp",
        default=os.environ.get("UKP", ""),
        help="уникальный код поступающего (или переменная окружения UKP)",
    )
    parser.add_argument(
        "--interval", type=int, default=INTERVAL_SECONDS, help="секунд между проверками"
    )
    parser.add_argument("--once", action="store_true", help="одна проверка и выход")
    parser.add_argument("--csv", default=HISTORY_PATH, type=Path, help="файл истории")
    parser.add_argument("--no-notify", action="store_true", help="без notify-send")
    parser.add_argument(
        "--filters",
        nargs="*",
        default=sorted(FILTERS),
        choices=sorted(FILTER_CLASSES),
        help="галочки «Отбор» (по умолчанию prior — высший проходной приоритет)",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="отправить тестовое уведомление и выйти",
    )
    args = parser.parse_args(argv)
    if args.test_notify:
        return run_test_notification(args)
    if not args.ukp:
        parser.error("не задан УКП: укажите --ukp 1234567 или переменную окружения UKP")
    try:
        return run(args)
    except KeyboardInterrupt:
        print()
        log("остановлено")
        return 0


if __name__ == "__main__":
    sys.exit(main())
