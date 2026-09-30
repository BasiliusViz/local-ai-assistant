"""Проверка repo_cards.py (карточки, зависимости, Obsidian) и инструмента cb_repos
на крошечном «релизе» из пяти репозиториев.

    python3 code/test_repo_cards.py
    docker compose exec cb-graph python /app/test_repo_cards.py

Только стандартная библиотека.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import graph_server as gs  # noqa: E402
import graph_store as st  # noqa: E402
import repo_cards as rc  # noqa: E402

FILES = {
    "vault-manager/README.md": (
        "# Vault Manager\n\n[![build](https://ci/badge.svg)](https://ci)\n\n"
        "Stores and rotates service secrets. Talks to HashiCorp Vault.\n\n"
        "```\nmake run\n```\n\n## Usage\n\nRun it.\n"),
    "vault-manager/go.mod": ("module git.cb/cb/vault-manager\n\ngo 1.21\n\nrequire (\n"
                             "\tgit.cb/cb/common-lib v1.2.0\n\tgithub.com/pkg/errors v0.9.1\n)\n"),
    "vault-manager/Dockerfile": "FROM golang\n",
    "vault-manager/cmd/main.go": "package main\n\nfunc main() {}\n",
    "vault-manager/internal/secrets/read.go": "package secrets\n" * 50,
    "vault-manager/internal/secrets/read_test.go": "package secrets\n" * 1000,
    "vault-manager/deploy/values.yaml": "alerts:\n  url: http://alert-manager:9093\nmain: x\n",
    "common-lib/go.mod": "module git.cb/cb/common-lib\n\ngo 1.21\n",
    "common-lib/log/log.go": "package log\n" * 300,
    "common-lib/vendor/x/x.go": "package x\n" * 5000,
    "alert-manager/README.md": "# Alert manager\n\nСервис уведомлений об алертах: принимает алерты и рассылает их.\n",
    "alert-manager/Dockerfile": "FROM openjdk\n",
    "alert-manager/pom.xml": (
        '<?xml version="1.0"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<groupId>ru.cb</groupId><artifactId>alert-manager</artifactId>"
        "<description>Alerts</description><dependencies><dependency><groupId>ru.cb</groupId>"
        "<artifactId>notify-client</artifactId></dependency></dependencies></project>"),
    "alert-manager/src/main/java/App.java": "class App {}\n" * 40,
    "notify-client/pom.xml": ("<project><groupId>ru.cb</groupId><artifactId>notify-client</artifactId>"
                              "</project>"),
    "notify-client/src/Client.java": "class Client {}\n" * 250,
    # DTD в pom.xml — отбрасывается целиком (защита от XXE без defusedxml)
    "notify-client/evil/pom.xml": ('<?xml version="1.0"?><!DOCTYPE p [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                                   "<project><artifactId>&x;</artifactId><dependencies><dependency>"
                                   "<artifactId>common-lib</artifactId></dependency></dependencies></project>"),
    "alert-chart/Chart.yaml": ("apiVersion: v2\nname: alert-chart\ndescription: Helm chart for alerts\n"
                               "dependencies:\n  - name: alert-manager\n    version: 1.0.0\n"),
    "alert-chart/templates/deploy.yaml": "kind: Deployment\n",
}


def graph_items():
    base = "/data/vault-manager/"
    nodes = [
        {"id": "read", "label": "ReadSecret()", "source_file": base + "internal/secrets/read.go",
         "source_location": "L10", "community": 0},
        {"id": "rotate", "label": "Rotate()", "source_file": base + "internal/secrets/read.go",
         "source_location": "L30", "community": 0},
        {"id": "main", "label": "main()", "source_file": base + "cmd/main.go", "source_location": "L3",
         "community": 1},
        {"id": "file", "label": "read.go", "source_file": base + "internal/secrets/read.go",
         "source_location": "L1", "community": 0},
    ]
    links = [{"source": "main", "target": "read", "relation": "calls"},
             {"source": "rotate", "target": "read", "relation": "calls"},
             {"source": "file", "target": "read", "relation": "contains"}]
    return [("node", n) for n in nodes] + [("link", l) for l in links]


class MatchDep(unittest.TestCase):
    names = {n: n for n in ("common", "tools", "vault-manager", "main", "alert-manager")}

    def index(self, *own):
        idx = {}
        for kind, i, repo in own:
            idx.setdefault(i, []).append((kind, repo))
        return idx

    def test_go_generic_tail_is_not_a_repo(self):
        idx = self.index(("go.mod", "git.cb/cb/vault-manager", "vault-manager"))
        for dep in ("github.com/prometheus/common", "golang.org/x/tools"):
            self.assertIsNone(rc._match_dep(dep, "go.mod", idx, self.names), dep)

    def test_go_major_version_and_subpackage(self):
        idx = self.index(("go.mod", "git.cb/cb/vault-manager", "vault-manager"))
        self.assertEqual(rc._match_dep("git.cb/cb/vault-manager/v2", "go.mod", idx, self.names), "vault-manager")
        self.assertEqual(rc._match_dep("git.cb/cb/vault-manager/pkg/api", "go.mod", idx, self.names),
                         "vault-manager")

    def test_several_owners_prefer_same_name(self):
        idx = self.index(("pom.xml", "ru.cb:alert-manager", "main"), ("pom.xml", "ru.cb:alert-manager", "alert-manager"))
        self.assertEqual(rc._match_dep("ru.cb:alert-manager", "pom.xml", idx, self.names), "alert-manager")
        idx = self.index(("pom.xml", "ru.cb:x", "main"), ("pom.xml", "ru.cb:x", "alert-manager"))
        self.assertIsNone(rc._match_dep("ru.cb:x", "pom.xml", idx, self.names))

    def test_generic_chart_dependency(self):
        self.assertIsNone(rc._match_dep("common", "Chart.yaml", {}, self.names))
        self.assertEqual(rc._match_dep("vault-manager", "Chart.yaml", {}, self.names), "vault-manager")


class Cards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "cb"
        for rel, text in FILES.items():
            p = cls.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        cls.db = cls.root / "graph" / "graph.sqlite"
        con = st.connect(cls.db)
        st.load_items(con, "vault-manager", graph_items())
        st.close(con, cls.db)
        cls.out = cls.db.with_name("cards.json")
        assert rc.build(cls.root, cls.db, cls.out, log=lambda *_: None) == 0
        cls.data = rc.load(cls.out)
        cls.cards = cls.data["repos"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_repos_but_not_graph_dir(self):
        self.assertEqual(sorted(self.cards),
                         ["alert-chart", "alert-manager", "common-lib", "notify-client", "vault-manager"])

    def test_kinds(self):
        kinds = {n: c["kind"] for n, c in self.cards.items()}
        self.assertEqual(kinds, {"vault-manager": "сервис", "alert-manager": "сервис",
                                 "common-lib": "библиотека", "notify-client": "библиотека",
                                 "alert-chart": "деплой"})

    def test_readme_without_badges_and_code(self):
        c = self.cards["vault-manager"]
        self.assertEqual(c["title"], "Vault Manager")
        self.assertIn("rotates service secrets", c["readme"])
        self.assertNotIn("badge", c["readme"])
        self.assertNotIn("make run", c["readme"])
        self.assertTrue(c["summary"].startswith("Stores and rotates"))

    def test_summary_from_manifest_without_readme(self):
        self.assertEqual(self.cards["alert-chart"]["summary"], "Helm chart for alerts")

    def test_langs_skip_vendor_and_tests(self):
        c = self.cards["common-lib"]
        self.assertEqual(c["lines"], 300)
        go = {l[0]: l for l in self.cards["vault-manager"]["langs"]}["Go"]
        self.assertEqual(go[1:], [2, 53])  # read_test.go не считается

    def test_deps_by_manifests(self):
        self.assertEqual(self.cards["vault-manager"]["deps"], {"common-lib": ["go.mod"]})
        self.assertEqual(self.cards["alert-manager"]["deps"], {"notify-client": ["pom.xml"]})
        self.assertEqual(self.cards["alert-chart"]["deps"], {"alert-manager": ["Chart.yaml"]})
        self.assertEqual(self.cards["common-lib"]["used_by"], {"vault-manager": ["go.mod"]})

    def test_xxe_pom_ignored(self):
        self.assertNotIn("common-lib", self.cards["notify-client"]["deps"])
        self.assertNotIn("passwd", json.dumps(self.cards["notify-client"]))

    def test_config_mentions_skip_generic(self):
        c = self.cards["vault-manager"]
        self.assertEqual(c["mentions"], {"alert-manager": 1})
        self.assertEqual(self.cards["alert-manager"]["mentioned_by"], {"vault-manager": 1})

    def test_graph_facts_relative_paths(self):
        c = self.cards["vault-manager"]
        self.assertEqual(c["nodes"], 4)
        self.assertEqual(c["modules"][0][0], "internal/secrets/")
        labels = [t[0] for t in c["top"]]
        self.assertEqual(labels[0], "ReadSecret()")
        self.assertNotIn("read.go", labels)  # узел-файл — не функция
        self.assertEqual(c["top"][0][2], "internal/secrets/read.go")

    def test_answer_named_repo(self):
        text = "\n".join(rc.answer(self.data, "от чего зависит vault-manager"))
        self.assertIn("## vault-manager", text)
        self.assertIn("common-lib [go.mod]", text)
        self.assertNotIn("## alert-manager", text)

    def test_answer_unique_part_of_name(self):
        text = "\n".join(rc.answer(self.data, "что делает vault"))
        self.assertIn("## vault-manager", text)

    def test_answer_keywords_russian(self):
        text = "\n".join(rc.answer(self.data, "какой сервис отвечает за уведомления"))
        self.assertIn("## alert-manager", text)

    def test_answer_overview(self):
        text = "\n".join(rc.answer(self.data, "из каких частей состоит релиз"))
        for name in self.cards:
            self.assertIn(name, text)
        self.assertIn("### сервис (2)", text)

    def test_answer_overview_with_release_name(self):
        # В README vault-manager есть «CB18.5» — общий вопрос всё равно даёт обзор
        for q in ("из каких частей состоит релиз CB18.5", "из чего состоит продукт CB18.5",
                  "опиши состав релиза CB-18.5", "на каких сервисах построен релиз"):
            self.assertIn("### сервис (2)", "\n".join(rc.answer(self.data, q)), q)

    def test_named_by_whole_part_only(self):
        cards = {"catalog": {}, "log-shipper": {}}
        self.assertEqual(rc.find_repos(cards, "что делает log")[0], ["log-shipper"])

    def test_obsidian(self):
        out = Path(self.tmp.name) / "vault"
        rc.obsidian(self.data, out, log=lambda *_: None)
        note = (out / "vault-manager.md").read_text(encoding="utf-8")
        self.assertTrue(note.startswith("---\n" + rc.NOTE_MARK + "\nkind: сервис"))
        self.assertIn("# vault-manager", note)
        self.assertIn("[[common-lib]] [go.mod]", note)
        self.assertIn("[[alert-manager]] (1)", note)
        idx = (out / "_Релиз CB18.5.md").read_text(encoding="utf-8")
        self.assertIn("[[alert-chart]]", idx)
        ys = (out / "services.yaml").read_text(encoding="utf-8")
        self.assertIn('depends_on: ["common-lib"]', ys)
        # Повторная выгрузка без репозитория: его заметка уходит, чужая остаётся
        (out / "мои заметки.md").write_text("# своё", encoding="utf-8")
        data = {**self.data, "repos": {k: v for k, v in self.cards.items() if k != "alert-chart"}}
        rc.obsidian(data, out, log=lambda *_: None)
        self.assertFalse((out / "alert-chart.md").exists())
        self.assertTrue((out / "мои заметки.md").exists())

    def test_tool_cb_repos(self):
        tools = gs.Tools(gs.Graph(self.db), "cb_", "РЕЛИЗ CB18.5")
        self.assertIn("cb_repos", [t["name"] for t in tools.list()])
        text = tools.call("cb_repos", {"query": "vault-manager"})
        self.assertIn("ReadSecret()", text)
        self.assertIn("### сервис (2)", tools.call("cb_repos", {}))

    def test_tool_without_cards(self):
        tools = gs.Tools(gs.Graph(Path(self.tmp.name) / "none" / "graph.sqlite"), "cb_", "")
        with self.assertRaises(LookupError):
            tools.call("cb_repos", {})


if __name__ == "__main__":
    unittest.main()
