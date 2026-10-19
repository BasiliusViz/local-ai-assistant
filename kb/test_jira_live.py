"""Проверка живых запросов к Jira без Jira.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_jira_live

Главное здесь — граница: JQL модели не выходит из рамок проектов, а в
Jira не уходит ничего, кроме GET.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from kb import jira_live
from kb.jira_live import JiraLiveError

ENV = {
    "JIRA_URL": "https://jira.local",
    "JIRA_TOKEN": "t",
    "JIRA_PROJECTS": "DEV,SEC",
    "JIRA_TEAM": "",
    "JIRA_TEAM_GROUP": "",
    "JIRA_JQL": "",
    "JIRA_FIELDS": "",
    "JIRA_LIVE": "1",
}
SCOPE = 'project in ("DEV", "SEC")'


def raw_issue(key: str) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"задача {key}",
            "status": {"name": "Open", "statusCategory": {"key": "new"}},
            "assignee": {"displayName": "Иванов", "name": "i.ivanov"},
            "priority": {"name": "High"},
            "updated": "2026-10-01T10:00:00.000+0300",
            "description": "h2. Заголовок\nтекст",
            "comment": {"comments": [{"author": {"displayName": "Петров"},
                                      "created": "2026-10-02", "body": "ок"}]},
        },
    }


@mock.patch.dict(os.environ, ENV)
class BuildQueryTest(unittest.TestCase):
    def test_model_jql_is_wrapped_in_scope(self):
        q = jira_live.build_query("issuetype = Bug")
        self.assertEqual(q, f"{SCOPE} AND (issuetype = Bug) ORDER BY updated DESC")

    def test_empty_jql_is_scope_only(self):
        self.assertEqual(jira_live.build_query(""), f"{SCOPE} ORDER BY updated DESC")

    def test_order_by_kept_at_the_end(self):
        q = jira_live.build_query("priority = High order by priority DESC, updated")
        self.assertTrue(q.endswith("AND (priority = High) ORDER BY priority DESC, updated"))

    def test_escape_from_scope_is_refused(self):
        for bad in (
            "x = 1) OR (project = HR",
            "x = 1) OR project = HR OR (y = 2",
            'summary ~ "a) OR (project = HR',
            "(x = 1",
            "x = 1 ORDER BY updated) OR (project = HR",
            "x = 1 ORDER BY key; DROP",
        ):
            with self.subTest(bad=bad), self.assertRaises(JiraLiveError):
                jira_live.build_query(bad)

    def test_backslash_outside_quotes_refused(self):
        # Находка ревью: для Jira \" вне кавычек — символ, а не кавычка
        for bad in (
            'summary ~ x\\") OR project = SECRET OR (summary ~ "\\""',
            "summary ~ x\\') OR project = SECRET OR (summary ~ '\\''",
        ):
            with self.subTest(bad=bad), self.assertRaises(JiraLiveError):
                jira_live.build_query(bad)

    def test_subqueries_into_foreign_projects_refused(self):
        for bad in (
            'issue in linkedIssues("SECRET-1")',
            "issue in linkedIssues(secret-1)",
            "parent = SECRET-5",
            'issueFunction in linkedIssuesOf("project = SECRET")',
            'ISSUEFUNCTION in subtasksOf("summary ~ x")',
            # Повторная проверка ревью: обход через кавычки, cf[] и плагины
            '"issueFunction" in linkedIssuesOf("project = SECRET")',
            'cf[10200] in linkedIssuesOf("project = SECRET")',
            'issue in subtaskOfQuery("project = SECRET AND summary ~ x")',
            'issue in "linkedIssuesOf"("project = SECRET")',
            'parent = "SECRET-1"',
            '"Epic Link" = "SECRET-1"',
            'issue = "secret-1"',
            "issue in linkedIssues(10001)",
            "parent = 10001",
            "issue = 10001",
            "key in (10001, 10002)",
            '"Epic Link" = 10001',
            '"Ссылка на эпик" = 10001',
            'parent = "10001"',
            'issue in ("DEV-1","10001")',
            '"parent" = 10001',
            "parent in (DEV-1, 10001)",
            '"Epic Link" in ("DEV-1", 10001)',
            "cf[10014] = 10001",
            '"Parent Link" = 10001',
            # Своя перепроверка: юникод-экранирование прячет число от проверки
            'parent = "\\u0031\\u0030\\u0030\\u0030\\u0031"',
            'parent = "\\u0053ECRET-1"',
            'summary ~ "a\\nb"',
        ):
            with self.subTest(bad=bad), self.assertRaises(JiraLiveError):
                jira_live.build_query(bad)

    def test_own_keys_functions_and_text_with_dashes_allowed(self):
        for ok in (
            'issue in (DEV-1, "SEC-2") AND parent = sec-2',
            'text ~ "CVE-2024-1234" AND updated >= -3M',
            "sprint in openSprints() AND assignee in membersOf(\"team\")",
            'status in (Open, "In Progress") AND NOT (priority = Low)',
            "updated >= startOfMonth(-1) AND fixVersion in unreleasedVersions()",
            "sprint = 42 AND cf[10002] > 3",
        ):
            with self.subTest(ok=ok):
                jira_live.build_query(ok)

    def test_brackets_inside_strings_are_fine(self):
        q = jira_live.build_query('summary ~ "скобка ) внутри" AND labels = "a\\"b"')
        self.assertIn('(summary ~ "скобка ) внутри"', q)

    def test_team_window_and_own_jql_only_for_index(self):
        # Эпики и бэклог без исполнителя из команды должны находиться живьём
        env = {**ENV, "JIRA_TEAM": "i.ivanov", "JIRA_JQL": "labels = sec",
               "JIRA_SINCE": "30d"}
        with mock.patch.dict(os.environ, env):
            q = jira_live.build_query("priority = High")
        self.assertEqual(q, f"{SCOPE} AND (priority = High) ORDER BY updated DESC")

    def test_live_projects_override(self):
        with mock.patch.dict(os.environ, {"JIRA_LIVE_PROJECTS": "DEV,OPS"}):
            q = jira_live.build_query("x = 1")
            self.assertTrue(q.startswith('project in ("DEV", "OPS") AND'))
            self.assertTrue(jira_live.in_scope("OPS-1"))
            self.assertFalse(jira_live.in_scope("SEC-1"))
            with self.assertRaises(JiraLiveError):
                jira_live.build_query("parent = SEC-1")

    def test_star_means_whole_jira(self):
        with mock.patch.dict(os.environ, {"JIRA_LIVE_PROJECTS": "*"}):
            self.assertTrue(jira_live.available())
            self.assertEqual(jira_live.build_query(""), "ORDER BY updated DESC")
            self.assertEqual(
                jira_live.build_query("parent = HR-1"),
                "(parent = HR-1) ORDER BY updated DESC",
            )
            self.assertTrue(jira_live.in_scope("HR-1"))
            # Функции плагинов и при «*» нельзя: тяжёлые подзапросы
            with self.assertRaises(JiraLiveError):
                jira_live.build_query('issue in subtaskOfQuery("x")')

    def test_too_long_jql_refused(self):
        with self.assertRaises(JiraLiveError):
            jira_live.build_query("x = 1 AND " * 200)


@mock.patch.dict(os.environ, ENV)
class AvailableTest(unittest.TestCase):
    def test_needs_projects(self):
        with mock.patch.dict(os.environ, {"JIRA_PROJECTS": ""}):
            self.assertFalse(jira_live.available())

    def test_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"JIRA_LIVE": "0"}):
            self.assertFalse(jira_live.available())

    def test_on_when_configured(self):
        self.assertTrue(jira_live.available())

    def test_off_by_default(self):
        env = {k: v for k, v in ENV.items() if k != "JIRA_LIVE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(jira_live.available())


class GetOnlyTest(unittest.TestCase):
    def test_non_get_methods_refused(self):
        session = mock.Mock()
        original = session.request
        jira_live._get_only(session)
        for method in ("POST", "PUT", "DELETE", "patch"):
            with self.subTest(method=method), self.assertRaises(JiraLiveError):
                session.request(method, "https://jira.local/rest/api/2/issue")
        session.request("GET", "https://jira.local/rest/api/2/search")
        original.assert_called_once()

    def test_real_client_session_is_guarded(self):
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(jira_live, "_client", None):
            c = jira_live.client()
            with self.assertRaises(JiraLiveError):
                c.session.post("https://jira.local/rest/api/2/issue", json={})
            # Без повторов: тяжёлый JQL не должен уходить в Jira несколько раз
            adapter = c.session.get_adapter("https://jira.local/")
            self.assertEqual(adapter.max_retries.total, 0)


@mock.patch.dict(os.environ, ENV)
class QueryTest(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock(base="https://jira.local")
        patches = [
            mock.patch.object(jira_live, "client", return_value=self.client),
            mock.patch.object(jira_live, "field_ids", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_foreign_issues_dropped(self):
        self.client.get.return_value = {
            "total": 2, "issues": [raw_issue("DEV-1"), raw_issue("HR-9")]
        }
        res = jira_live.query("x = 1")
        self.assertEqual([i["key"] for i in res["issues"]], ["DEV-1"])
        self.assertEqual(res["issues"][0]["url"], "https://jira.local/browse/DEV-1")

    def test_max_results_capped(self):
        self.client.get.return_value = {"total": 0, "issues": []}
        jira_live.query("", max_results=500)
        self.assertEqual(self.client.get.call_args.kwargs["maxResults"], 50)

    def test_count_only_without_order(self):
        self.client.count.return_value = 7
        res = jira_live.query("issuetype = Bug", count_only=True)
        self.assertEqual(res["total"], 7)
        self.assertNotIn("ORDER BY", self.client.count.call_args.args[0])

    def test_issue_key_out_of_scope_refused(self):
        with self.assertRaises(JiraLiveError):
            jira_live.query(issue_key="HR-1")
        self.client.get.assert_not_called()

    def test_issue_key_gives_card_with_comments(self):
        self.client.get.return_value = {"total": 1, "issues": [raw_issue("DEV-5")]}
        res = jira_live.query(issue_key="dev-5")
        card = res["issues"][0]
        self.assertIn('key = "DEV-5"', self.client.get.call_args.kwargs["jql"])
        self.assertEqual(card["description"], "Заголовок\nтекст")
        self.assertEqual(card["comments"][0]["text"], "ок")

    def test_bad_jql_error_goes_back_to_model(self):
        self.client.get.side_effect = jira_live.jira_sync.JiraError(
            "400: Jira не приняла запрос.\n  Field 'foo' does not exist"
        )
        with self.assertRaises(JiraLiveError) as ctx:
            jira_live.query("foo = 1")
        self.assertIn("исправь условие", str(ctx.exception))
        self.assertIn("foo", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
