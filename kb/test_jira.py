"""Проверка индекса и поиска Jira без Qdrant и эмбеддера.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_jira

Qdrant подменён: проверяется, какие условия строятся из вопроса и что
попадает в чанки, а не сам поиск.
"""

from __future__ import annotations

import unittest
from unittest import mock

from kb import jira_index, jira_retriever
from kb.jira_retriever import JiraSearchError

ISSUE = {
    "key": "DEV-2",
    "summary": "Импорт выписок",
    "project": "DEV",
    "type": "История",
    "status": "Open",
    "priority": "High",
    "components": ["api"],
    "labels": ["sec"],
    "fix_versions": ["2.0"],
    "epic": "DEV-1",
    "epic_name": "Импорт",
    "sprints": ["Спринт 5"],
    "fields": {"Стрим заказчика": ["Розница"]},
    "description": "текст",
}


class IndexTest(unittest.TestCase):
    def test_card_mentions_epic_sprint_and_fields(self):
        head = jira_index.issue_chunks(ISSUE)[0]["text"]
        for part in ("Эпик: DEV-1 «Импорт»", "Спринт: Спринт 5",
                     "Стрим заказчика: Розница", "Исправить в версиях: 2.0"):
            self.assertIn(part, head)

    def test_field_values(self):
        vals = jira_index.field_values(ISSUE)
        for v in ("Стрим заказчика=Розница", "Компоненты=api", "Тип=История",
                  "Исправить в версиях=2.0", "Метки=sec"):
            self.assertIn(v, vals)
        self.assertNotIn("Резолюция=", vals)


class MatchTest(unittest.TestCase):
    def test_words_not_substring(self):
        known = ["Спринт 1", "Спринт 12", "Спринт 15"]
        self.assertEqual(jira_retriever.match_values("спринт 1", known), ["Спринт 1"])
        self.assertEqual(
            jira_retriever.match_values("12", known), ["Спринт 12"]
        )

    def test_partial(self):
        self.assertEqual(
            jira_retriever.match_values("импорт", ["Импорт выписок", "Экспорт"]),
            ["Импорт выписок"],
        )


CATALOG = {
    "Стрим заказчика": {"Розница": 10, "Корпоратив": 3},
    "Компоненты": {"api": 5},
}


class ConditionsTest(unittest.TestCase):
    def test_field_resolves_name_and_value(self):
        with mock.patch.object(jira_retriever, "field_catalog", return_value=CATALOG):
            conds, shown = jira_retriever._field_conditions("стрим", "розн")
        self.assertEqual(conds[0].match.any, ["Стрим заказчика=Розница"])
        self.assertEqual(shown, "Стрим заказчика = Розница")

    def test_field_name_by_word_stems(self):
        self.assertEqual(
            jira_retriever.resolve_field("стримы заказчиков", CATALOG), "Стрим заказчика"
        )

    def test_unknown_field_lists_known(self):
        with self.assertRaises(JiraSearchError) as e:
            jira_retriever.resolve_field("команда", CATALOG)
        self.assertIn("Стрим заказчика", str(e.exception))

    def test_epic_by_key_skips_lookup(self):
        with mock.patch.object(jira_retriever, "values") as values:
            conds, shown = jira_retriever._epic_conditions("dev-1")
        values.assert_not_called()
        self.assertEqual((conds[0].key, shown), ("epic", "DEV-1"))

    def test_epic_by_name(self):
        with mock.patch.object(jira_retriever, "values", return_value=["Импорт", "Отчёты"]):
            conds, _ = jira_retriever._epic_conditions("импорт")
        self.assertEqual((conds[0].key, conds[0].match.any), ("epic_name", ["Импорт"]))

    def test_current_sprint(self):
        with mock.patch.object(jira_retriever, "values", return_value=["S5"]) as values:
            conds, _ = jira_retriever._sprint_conditions("текущий")
        values.assert_called_with("active_sprints", limit=500)
        self.assertEqual(conds[0].key, "active_sprints")

    def test_sprint_number(self):
        with mock.patch.object(
            jira_retriever, "values", return_value=["Спринт 4", "Спринт 42"]
        ):
            conds, _ = jira_retriever._sprint_conditions("42")
        self.assertEqual(conds[0].match.any, ["Спринт 42"])

    def test_field_without_value_is_error_in_search(self):
        with self.assertRaises(JiraSearchError):
            jira_retriever.search_issues(field="стрим")


if __name__ == "__main__":
    unittest.main()
