"""Проверка jenkins_graph.py.

    python3 code/test_jenkins_graph.py
    docker compose exec code-graph python /app/test_jenkins_graph.py

Только стандартная библиотека, сеть и Graphify не нужны.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jenkins_graph as jg  # noqa: E402


class Calls(unittest.TestCase):
    def setUp(self):
        self.pattern = jg.call_pattern({"abActions", "abActionsV2", "notifySlack", "abBump"})

    def names(self, src: str, own: str = "") -> list[str]:
        return [n for n, _ in jg.find_calls(src, self.pattern, own)]

    def test_call_forms(self):
        src = (
            "abActions(env: 'prod')\n"
            "abActions { stage = 'x' }\n"
            "abActions env: 'prod'\n"
            'notifySlack "сборка упала"\n'
            "abBump cfg\n"
            "abActions.helper(1)\n"
            "abActionsV2()\n"
        )
        self.assertEqual(
            self.names(src),
            ["abActions", "abActions", "abActions", "notifySlack", "abBump", "abActions", "abActionsV2"],
        )

    def test_line_numbers(self):
        calls = jg.find_calls("x = 1\n\nabActions()\n", self.pattern)
        self.assertEqual(calls, [("abActions", 3)])

    def test_not_calls(self):
        src = (
            "// abActions(env: 'prod')\n"
            "/* abActions()\n   notifySlack 'x' */\n"
            'echo "run abActions(now)"\n'
            "sh '''abActions()'''\n"
            "obj.abActions(1)\n"
            "def abActions(cfg) { }\n"
            "myabActions()\n"
            "if (x instanceof abBump) {}\n"
            "def m = [abActions: 1]\n"
            "abActions = 5\n"
        )
        self.assertEqual(self.names(src), [])

    def test_steps_prefix_from_classes(self):
        self.assertEqual(self.names("steps.abActions(env: 'x')\n"), ["abActions"])

    def test_recursion_is_not_an_edge(self):
        self.assertEqual(self.names("def call() { abActions() }\n", own="abActions"), [])

    def test_line_numbers_survive_multiline_strings(self):
        src = 'def s = """\nline\nline\n"""\nabActions()\n'
        self.assertEqual(jg.find_calls(src, self.pattern), [("abActions", 5)])


class Graph(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repos = self.root / "all_git"
        self.graph = self.root / "graph.json"

    def put(self, rel: str, text: str) -> None:
        p = self.repos / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def base_graph(self):
        self.graph.write_text(json.dumps({
            "directed": False, "multigraph": False, "graph": {},
            "nodes": [{"id": "lib::x", "label": "x", "community": 4}],
            "links": [{"source": "lib::x", "target": "lib::x", "relation": "contains"}],
            "hyperedges": [],
        }), encoding="utf-8")

    def load(self):
        data = json.loads(self.graph.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in data["nodes"]}
        edges = {(nodes[l["source"]]["label"], nodes[l["target"]]["label"], l["confidence"])
                 for l in data["links"] if l.get("_origin") == "jenkins"}
        return data, nodes, edges

    def make_repos(self):
        # Общая библиотека: шаг abActions зовёт шаг abBumpComponents
        self.put("bitbucket-DEVOPS-jenkins-lib/vars/abActions.groovy",
                 "#!/usr/bin/env groovy\n// шаг\ndef call(cfg) {\n    abBumpComponents(cfg)\n}\n")
        self.put("bitbucket-DEVOPS-jenkins-lib/vars/abBumpComponents.groovy",
                 "def call(Map args = [:]) {\n    sh 'make bump'\n}\n")
        self.put("bitbucket-DEVOPS-jenkins-lib/vars/unused.groovy", "def call() {}\n")
        self.put("bitbucket-DEVOPS-jenkins-lib/src/org/ab/Deploy.groovy",
                 "class Deploy {\n  def steps\n  def run() { steps.abActions(env: 'x') }\n}\n")
        self.put("bitbucket-DEVOPS-jenkins-lib/src/org/ab/Util.groovy", "class Util { def x() {} }\n")
        # Пайплайны в других репозиториях
        self.put("bitbucket-PAY-billing/Jenkinsfile",
                 "@Library('jenkins-lib') _\npipeline {\n  stages { stage('b') { steps {\n    abActions env: 'prod'\n  } } }\n}\n")
        self.put("gitflic-team-front/ci/deploy.jenkinsfile", "abBumpComponents()\n")
        self.put("gitflic-team-front/graphify-out/Jenkinsfile", "abActions()\n")  # служебное — мимо
        self.put(".gitflic-x.partial/Jenkinsfile", "abActions()\n")  # недокачанный клон — мимо

    def test_edges_across_repos(self):
        self.base_graph()
        self.make_repos()
        stats = jg.apply(self.graph, self.repos)
        data, nodes, edges = self.load()
        self.assertEqual(edges, {
            ("abActions", "abBumpComponents", "EXTRACTED"),
            ("bitbucket-PAY-billing/Jenkinsfile", "abActions", "EXTRACTED"),
            ("gitflic-team-front/ci/deploy.jenkinsfile", "abBumpComponents", "EXTRACTED"),
            ("bitbucket-DEVOPS-jenkins-lib/src/org/ab/Deploy.groovy", "abActions", "EXTRACTED"),
        })
        self.assertEqual(stats["шагов"], 3)
        self.assertEqual(stats["пайплайнов"], 2)
        self.assertEqual(stats["шагов, которые никто не зовёт"], 1)
        # Util.groovy шагов не зовёт — в граф не попадает
        self.assertFalse(any("Util.groovy" in n["label"] for n in nodes.values()))
        step = next(n for n in nodes.values() if n["label"] == "abActions")
        self.assertEqual(step["source_file"], "bitbucket-DEVOPS-jenkins-lib/vars/abActions.groovy")
        self.assertEqual(step["source_location"], "L3")  # строка def call
        self.assertEqual(step["community"], 5)  # новая, после существующих
        # старое содержимое графа на месте
        self.assertIn("lib::x", nodes)
        self.assertTrue(any(l["relation"] == "contains" for l in data["links"]))

    def test_rerun_does_not_duplicate(self):
        self.base_graph()
        self.make_repos()
        jg.apply(self.graph, self.repos)
        first = self.graph.read_text(encoding="utf-8")
        jg.apply(self.graph, self.repos)
        self.assertEqual(self.graph.read_text(encoding="utf-8"), first)

    def test_same_step_in_two_libraries_is_ambiguous(self):
        self.base_graph()
        self.put("lib-a/vars/deploy.groovy", "def call() {}\n")
        self.put("lib-b/vars/deploy.groovy", "def call() {}\n")
        self.put("app/Jenkinsfile", "deploy()\n")
        stats = jg.apply(self.graph, self.repos)
        _, _, edges = self.load()
        self.assertEqual(edges, {("app/Jenkinsfile", "deploy", "AMBIGUOUS")})
        self.assertEqual(stats["из них неоднозначных"], 2)
        self.assertEqual(stats["одноимённых шагов в разных библиотеках"], 1)

    def test_no_jenkins_leaves_graph_intact(self):
        self.base_graph()
        self.put("svc/main.go", "package main\n")
        before = json.loads(self.graph.read_text(encoding="utf-8"))
        jg.apply(self.graph, self.repos)
        after = json.loads(self.graph.read_text(encoding="utf-8"))
        self.assertEqual(after["nodes"], before["nodes"])
        self.assertEqual(after["links"], before["links"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
