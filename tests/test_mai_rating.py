"""Тесты парсера конкурсных списков МАИ.

Фикстуры — реальная разметка с public.mai.ru, урезанная до нескольких строк
в каждой таблице (см. tests/fixtures/).
"""

import argparse
import csv
import os
import sys
import unittest.mock
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mai_rating as mr

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseOptionsTest(unittest.TestCase):
    def test_returns_value_label_pairs_without_placeholder(self):
        options = mr.parse_options(fixture("options_level.html"))

        self.assertIn(("p20260820131238_1_l5", "Специализированное высшее образование"), options)
        self.assertNotIn("0", [value for value, _ in options])

    def test_pick_option_finds_value_by_label(self):
        options = mr.parse_options(fixture("options_level.html"))

        self.assertEqual(mr.pick_option(options, "Бакалавриат"), "p20260820131238_1_l4")

    def test_pick_option_lists_available_labels_when_missing(self):
        options = mr.parse_options(fixture("options_level.html"))

        with self.assertRaises(LookupError) as ctx:
            mr.pick_option(options, "Аспирантура")

        self.assertIn("Магистратура", str(ctx.exception))


class ParseSectionsTest(unittest.TestCase):
    def test_single_section_has_title_places_and_rows(self):
        sections = mr.parse_sections(fixture("table_single.html"))

        self.assertEqual(len(sections), 1)
        self.assertIn("общему конкурсу", sections[0].title)
        self.assertEqual(sections[0].places, 200)
        self.assertEqual(len(sections[0].rows), 8)
        self.assertEqual(sections[0].rows[0].ukp, "1627960")

    def test_row_exposes_cells_by_column_name(self):
        section = mr.parse_sections(fixture("table_single.html"))[0]
        row = next(r for r in section.rows if r.ukp == "2500742")

        self.assertEqual(row.cell("Сумма конкурсных баллов"), "120")
        self.assertEqual(row.cell("Приоритет"), "1")
        self.assertEqual(row.cell("Согласие"), "✓")

    def test_row_keeps_filter_classes(self):
        section = mr.parse_sections(fixture("table_single.html"))[0]
        row = next(r for r in section.rows if r.ukp == "1627960")

        self.assertIn("not_pr", row.classes)

    def test_multi_section_splits_by_heading_and_keeps_own_columns(self):
        sections = mr.parse_sections(fixture("table_multi.html"))

        titles = [s.title for s in sections]
        self.assertEqual(len(sections), 3)
        self.assertIn("целевое обучение", titles[0])
        self.assertIn("общему конкурсу", titles[2])
        self.assertEqual(sections[0].places, 4)
        self.assertEqual(sections[2].places, 41)
        self.assertEqual(sections[0].rows[0].cell("Согласие"), "✓")

    def test_missing_trailing_cell_reads_as_empty(self):
        # У строк целевой квоты хвостовой столбец «Зачислен» может отсутствовать.
        section = mr.parse_sections(fixture("table_multi.html"))[0]

        self.assertEqual(section.rows[0].cell("Зачислен"), "")

    def test_parses_generation_time(self):
        self.assertEqual(
            mr.parse_generated_at(fixture("table_single.html")), "20.08.2026 15:41:59"
        )


class FilterTest(unittest.TestCase):
    def test_no_filters_keeps_every_row(self):
        section = mr.parse_sections(fixture("table_single.html"))[0]

        self.assertEqual(len(mr.visible_rows(section.rows, frozenset())), 8)

    def test_top_passing_priority_filter_hides_not_pr_rows(self):
        section = mr.parse_sections(fixture("table_single.html"))[0]

        visible = mr.visible_rows(section.rows, frozenset({"prior"}))

        self.assertEqual([r.ukp for r in visible], ["2500742"])

    def test_consent_filter_hides_not_sogl_rows(self):
        section = mr.parse_sections(fixture("table_single.html"))[0]

        visible = mr.visible_rows(section.rows, frozenset({"sogl"}))

        self.assertNotIn("1263541", [r.ukp for r in visible])
        self.assertIn("1627960", [r.ukp for r in visible])


class StandingTest(unittest.TestCase):
    def test_rank_without_filters_is_position_in_full_list(self):
        standing = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())

        self.assertEqual(standing.rank, 2)
        self.assertEqual(standing.total, 8)
        self.assertEqual(standing.places, 200)
        self.assertEqual(standing.score, "120")
        self.assertEqual(standing.priority, "1")
        self.assertEqual(standing.consent, True)

    def test_rank_is_recomputed_over_visible_rows(self):
        standing = mr.find_standing(
            fixture("table_single.html"), "2500742", frozenset({"prior"})
        )

        self.assertEqual(standing.rank, 1)
        self.assertEqual(standing.total, 1)

    def test_returns_none_when_filter_hides_the_applicant(self):
        standing = mr.find_standing(
            fixture("table_single.html"), "1627960", frozenset({"prior"})
        )

        self.assertIsNone(standing)

    def test_returns_none_when_applicant_absent(self):
        standing = mr.find_standing(fixture("table_single.html"), "9999999", frozenset())

        self.assertIsNone(standing)

    def test_rank_is_counted_inside_the_applicants_own_section(self):
        standing = mr.find_standing(fixture("table_multi.html"), "1474680", frozenset())

        self.assertEqual(standing.rank, 4)
        self.assertEqual(standing.places, 41)
        self.assertIn("общему конкурсу", standing.section)


class DescribeTest(unittest.TestCase):
    def test_says_places_once_when_list_length_equals_places(self):
        standing = mr.Standing(
            ukp="1", rank=113, total=200, places=200, section="общий",
            score="94", priority="1", consent=True, enrolled=False, generated_at="20.08.2026 16:16:50",
        )

        self.assertIn("место 113 из 200 мест", standing.describe())
        self.assertNotIn("200 из 200 мест", standing.describe())

    def test_shows_both_numbers_when_list_is_longer_than_places(self):
        standing = mr.Standing(
            ukp="1", rank=233, total=790, places=200, section="общий",
            score="94", priority="1", consent=True, enrolled=False, generated_at=None,
        )

        self.assertIn("место 233 из 790 (мест 200)", standing.describe())


class ChangeDetectionTest(unittest.TestCase):
    def test_same_rank_and_status_is_not_a_change(self):
        first = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())
        second = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())

        self.assertFalse(mr.has_changed(first, second))

    def test_different_rank_is_a_change(self):
        first = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())
        second = mr.find_standing(
            fixture("table_single.html"), "2500742", frozenset({"prior"})
        )

        self.assertTrue(mr.has_changed(first, second))

    def test_appearing_in_the_list_is_a_change(self):
        standing = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())

        self.assertTrue(mr.has_changed(None, standing))


class CascadeTest(unittest.TestCase):
    def test_walks_selects_by_label(self):
        pages = {
            "https://priem.mai.ru/rating/": '<select id="place"><option value="0">---</option>'
            '<option value="pX_1">МАИ</option></select>',
            "https://public.mai.ru/priem/rating/data/pX_1.html": '<option value="pX_1_l5">Спец</option>',
            "https://public.mai.ru/priem/rating/data/pX_1_l5.html": '<option value="pX_1_l5_p1">Бюджет</option>',
        }
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return pages[url]

        value = mr.resolve_selection(fake_fetch, ["МАИ", "Спец", "Бюджет"])

        self.assertEqual(value, "pX_1_l5_p1")
        self.assertEqual(fetched[0], "https://priem.mai.ru/rating/")

    def test_reports_which_step_failed(self):
        pages = {
            "https://priem.mai.ru/rating/": '<option value="pX_1">МАИ</option>',
            "https://public.mai.ru/priem/rating/data/pX_1.html": '<option value="pX_1_l5">Спец</option>',
        }

        with self.assertRaises(LookupError) as ctx:
            mr.resolve_selection(lambda url: pages[url], ["МАИ", "Магистратура"])

        self.assertIn("Магистратура", str(ctx.exception))


class TelegramTest(unittest.TestCase):
    def test_posts_text_to_bot_api(self):
        sent = []

        def fake_post(url, data):
            sent.append((url, data))

        mr.send_telegram("123:ABC", "555", "место 113 → 97", post=fake_post)

        url, data = sent[0]
        self.assertEqual(url, "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(data["chat_id"], "555")
        self.assertEqual(data["text"], "место 113 → 97")

    def test_reports_failure_instead_of_raising(self):
        def failing_post(url, data):
            raise OSError("сеть недоступна")

        ok = mr.send_telegram("123:ABC", "555", "текст", post=failing_post)

        self.assertFalse(ok)


class TestNotificationTest(unittest.TestCase):
    def test_sends_a_test_message_when_credentials_are_set(self):
        sent = []
        env = {"TG_BOT_TOKEN": "123:ABC", "TG_CHAT_ID": "555"}

        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with unittest.mock.patch.object(mr, "send_telegram", lambda *a, **k: sent.append(a) or True):
                code = mr.main(["--test-notify", "--no-notify"])

        self.assertEqual(code, 0)
        self.assertEqual(sent[0][0], "123:ABC")
        self.assertTrue(sent[0][2])

    def test_fails_when_credentials_are_missing(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            code = mr.main(["--test-notify", "--no-notify"])

        self.assertEqual(code, 1)

    def test_does_not_require_ukp(self):
        env = {"TG_BOT_TOKEN": "123:ABC", "TG_CHAT_ID": "555"}

        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with unittest.mock.patch.object(mr, "send_telegram", lambda *a, **k: True):
                code = mr.main(["--test-notify", "--no-notify"])

        self.assertEqual(code, 0)


class ReportTest(unittest.TestCase):
    """Сообщение уходит после каждой проверки: тихое, если ничего не изменилось."""

    def setUp(self):
        self.args = argparse.Namespace(ukp="1560740", no_notify=True)
        self.standing = mr.Standing(
            ukp="1560740", rank=116, total=200, places=200, section="общий",
            score="94", priority="1", consent=True, enrolled=False,
            generated_at="20.08.2026 16:52:26",
        )
        self.moved = mr.Standing(**{**vars(self.standing), "rank": 97})

    def _capture(self, previous, current):
        sent = []
        env = {"TG_BOT_TOKEN": "123:ABC", "TG_CHAT_ID": "555"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with unittest.mock.patch.object(
                mr, "send_telegram", lambda *a, **k: sent.append((a, k)) or True
            ):
                mr.report(self.args, previous, current)
        return sent

    def test_unchanged_position_is_sent_silently(self):
        sent = self._capture(self.standing, self.standing)

        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1]["silent"])
        self.assertIn("место 116", sent[0][0][2])

    def test_changed_position_is_sent_with_sound(self):
        sent = self._capture(self.standing, self.moved)

        self.assertFalse(sent[0][1]["silent"])
        self.assertIn("116 → 97", sent[0][0][2])

    def test_disappearing_from_the_list_is_sent_with_sound(self):
        sent = self._capture(self.standing, None)

        self.assertFalse(sent[0][1]["silent"])

    def test_nothing_is_sent_without_credentials(self):
        sent = []
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch.object(
                mr, "send_telegram", lambda *a, **k: sent.append(a) or True
            ):
                mr.report(self.args, self.standing, self.standing)

        self.assertEqual(sent, [])


class SilentFlagTest(unittest.TestCase):
    def test_silent_message_asks_telegram_to_skip_the_sound(self):
        sent = []
        mr.send_telegram("123:ABC", "555", "текст", silent=True, post=lambda u, d: sent.append(d))

        self.assertEqual(sent[0]["disable_notification"], "true")

    def test_loud_message_does_not_disable_notification(self):
        sent = []
        mr.send_telegram("123:ABC", "555", "текст", post=lambda u, d: sent.append(d))

        self.assertNotIn("disable_notification", sent[0])


class HistoryReadTest(unittest.TestCase):
    def test_missing_file_has_no_previous_standing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mr.read_last_history(Path(tmp) / "нет.csv"))

    def test_reads_back_the_last_written_standing(self):
        first = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())
        second = mr.find_standing(
            fixture("table_single.html"), "2500742", frozenset({"prior"})
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            mr.append_history(path, first)
            mr.append_history(path, second)
            restored = mr.read_last_history(path)

        self.assertEqual(restored.rank, second.rank)
        self.assertEqual(restored.total, second.total)
        self.assertFalse(mr.has_changed(restored, second))

    def test_restored_standing_still_detects_a_change(self):
        first = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())
        second = mr.find_standing(
            fixture("table_single.html"), "2500742", frozenset({"prior"})
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            mr.append_history(path, first)
            restored = mr.read_last_history(path)

        self.assertTrue(mr.has_changed(restored, second))


class UkpConfigTest(unittest.TestCase):
    def test_refuses_to_run_without_ukp(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                mr.main(["--once", "--no-notify"])

        self.assertEqual(ctx.exception.code, 2)

    def test_takes_ukp_from_environment(self):
        seen = {}

        def fake_run(args):
            seen["ukp"] = args.ukp
            return 0

        with unittest.mock.patch.dict(os.environ, {"UKP": "7654321"}, clear=True):
            with unittest.mock.patch.object(mr, "run", fake_run):
                mr.main(["--once"])

        self.assertEqual(seen["ukp"], "7654321")

    def test_command_line_wins_over_environment(self):
        seen = {}

        def fake_run(args):
            seen["ukp"] = args.ukp
            return 0

        with unittest.mock.patch.dict(os.environ, {"UKP": "7654321"}, clear=True):
            with unittest.mock.patch.object(mr, "run", fake_run):
                mr.main(["--once", "--ukp", "1111111"])

        self.assertEqual(seen["ukp"], "1111111")


class HistoryTest(unittest.TestCase):
    def test_appends_row_and_writes_header_once(self):
        standing = mr.find_standing(fixture("table_single.html"), "2500742", frozenset())

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            mr.append_history(path, standing)
            mr.append_history(path, standing)

            with path.open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(rows[0][0], "checked_at")
        self.assertEqual(len(rows), 3)
        self.assertIn("2", rows[1])


if __name__ == "__main__":
    unittest.main()
