"""Проверка jira/sync.py: python3 jira/test_sync.py

Сеть и настоящая Jira не нужны: проверяется разбор ответов API — эпик,
спринт, свои поля. Именно здесь ломается, когда у Jira другая сборка.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync  # noqa: E402

FIELDS = [
    {"id": "summary", "name": "Summary", "schema": {"type": "string"}},
    {"id": "customfield_10100", "name": "Ссылка на эпик",
     "schema": {"custom": sync.EPIC_LINK_TYPE}},
    {"id": "customfield_10101", "name": "Имя эпика",
     "schema": {"custom": sync.EPIC_NAME_TYPE}},
    {"id": "customfield_10102", "name": "Спринт",
     "schema": {"custom": sync.SPRINT_TYPE}},
    {"id": "customfield_12000", "name": "Стрим заказчика",
     "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
    {"id": "customfield_12001", "name": "Команда", "schema": {}},
    {"id": "customfield_12002", "name": "Команда", "schema": {}},
]

OLD_SPRINT = (
    "com.atlassian.greenhopper.service.sprint.Sprint@1f2e[id=12,rapidViewId=3,"
    "state=ACTIVE,name=Спринт 5, команда А,startDate=2026-09-01T10:00:00.000+03:00,"
    "endDate=2026-09-14T10:00:00.000+03:00,completeDate=<null>,sequence=12,goal=]"
)


class FieldMapTest(unittest.TestCase):
    def test_software_fields_by_type_not_name(self):
        fmap = sync.field_map(FIELDS, [])
        self.assertEqual(fmap["epic_link"], "customfield_10100")
        self.assertEqual(fmap["epic_name"], "customfield_10101")
        self.assertEqual(fmap["sprint"], "customfield_10102")
        self.assertEqual(fmap["extra"], {})

    def test_extra_by_name_id_and_missing(self):
        fmap = sync.field_map(
            FIELDS, ["стрим заказчика", "customfield_12002", "Нет такого", "Спринт"]
        )
        self.assertEqual(
            fmap["extra"],
            {"Стрим заказчика": "customfield_12000", "Команда": "customfield_12002"},
        )
        self.assertEqual(fmap["missing"], ["Нет такого"])


class ValueTest(unittest.TestCase):
    def test_field_text_shapes(self):
        self.assertEqual(sync.field_text(None), [])
        self.assertEqual(sync.field_text("  x "), ["x"])
        self.assertEqual(sync.field_text({"value": "Розница"}), ["Розница"])
        self.assertEqual(
            sync.field_text({"value": "Банк", "child": {"value": "Карты"}}),
            ["Банк / Карты"],
        )
        self.assertEqual(sync.field_text([{"name": "1.2"}, {"name": "1.3"}]), ["1.2", "1.3"])
        self.assertEqual(sync.field_text({"displayName": "Иванов"}), ["Иванов"])
        self.assertEqual(sync.field_text(3.0), ["3.0"])

    def test_sprint_old_string_with_comma_in_name(self):
        self.assertEqual(
            sync.sprints([OLD_SPRINT]),
            [{"name": "Спринт 5, команда А", "state": "active"}],
        )

    def test_sprint_object(self):
        self.assertEqual(
            sync.sprints([{"id": 1, "name": "S1", "state": "closed"}]),
            [{"name": "S1", "state": "closed"}],
        )


class NormalizeTest(unittest.TestCase):
    fmap = sync.field_map(FIELDS, ["Стрим заказчика"])

    def issue(self, key, **fields):
        base = {"summary": "s", "status": {"name": "Open"}}
        base.update(fields)
        return {"key": key, "fields": base}

    def test_story_in_epic(self):
        rec = sync.normalize(
            self.issue(
                "DEV-2",
                customfield_10100="DEV-1",
                customfield_10102=[OLD_SPRINT, {"name": "Спринт 4", "state": "closed"}],
                customfield_12000={"value": "Розница"},
                fixVersions=[{"name": "2.0"}],
            ),
            "https://j",
            [],
            self.fmap,
            {"DEV-1": "Импорт"},
        )
        self.assertEqual(rec["epic"], "DEV-1")
        self.assertEqual(rec["epic_name"], "Импорт")
        self.assertEqual(rec["sprints"], ["Спринт 5, команда А", "Спринт 4"])
        self.assertEqual(rec["active_sprints"], ["Спринт 5, команда А"])
        self.assertEqual(rec["fields"], {"Стрим заказчика": ["Розница"]})
        self.assertEqual(rec["fix_versions"], ["2.0"])

    def test_epic_is_its_own_epic(self):
        rec = sync.normalize(
            self.issue("DEV-1", customfield_10101="Импорт"), "https://j", [], self.fmap
        )
        self.assertEqual((rec["epic"], rec["epic_name"]), ("DEV-1", "Импорт"))

    def test_without_field_map_still_works(self):
        rec = sync.normalize(self.issue("DEV-3"), "https://j", [])
        self.assertEqual((rec["epic"], rec["sprints"], rec["fields"]), ("", [], {}))


if __name__ == "__main__":
    unittest.main()
