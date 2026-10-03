import json
import tempfile
import unittest
from pathlib import Path

from summary_survey import CHARS_PER_TOKEN, HEAD_CHARS, folder_units, main, rel_path, survey


def ktok(chars):
    return chars / CHARS_PER_TOKEN / 1000


class TestSurvey(unittest.TestCase):
    def test_rel_path(self):
        self.assertEqual(rel_path("/data/svc/src/a.go", "svc"), "src/a.go")
        self.assertEqual(rel_path("svc/src/a.go", "svc"), "src/a.go")
        self.assertEqual(rel_path("./.github/x.yml", "svc"), ".github/x.yml")
        self.assertEqual(rel_path("C:\\r\\svc\\b.py", "svc"), "b.py")
        self.assertEqual(rel_path("src/requests/a.py", "requests"), "src/requests/a.py")

    def test_small_folders_merge_up(self):
        w = {"": 0.1, "a": 0.5, "a/b": 0.5, "c": 3.0}
        # порог 1: a/b -> a (1.0, отдельно); c отдельно; корень
        self.assertEqual(folder_units(w, 1), (3, 0))
        # порог 5: всё в корень
        self.assertEqual(folder_units(w, 5), (1, 0))
        # слишком большой вход одного вызова
        self.assertEqual(folder_units(w, 1, max_weight=2), (3, 1))

    def test_missing_parent_is_created(self):
        self.assertEqual(folder_units({"x/y/z": 2.0}, 1), (2, 0))

    def test_small_file_whole_big_by_skeleton(self):
        nodes = [("svc/big.go", "Run()", "L1")]
        disk = {"big.go": 50_000, "jobs/Jenkinsfile": 900, "jobs/deploy.sh": 400, "huge.sql": 90_000}
        s = survey(iter(nodes), "svc", disk, small=8000)
        self.assertEqual((s["files"], s["whole"], s["no_nodes"]), (4, 2, 3))
        # Jenkins-папка: оба скрипта целиком
        self.assertAlmostEqual(s["weights"]["jobs"], ktok(1300))
        # корень: скелет big.go + начало huge.sql (нет узлов)
        self.assertAlmostEqual(s["weights"][""], ktok(len("Run()") + 2 + 12 + HEAD_CHARS))

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "jobs"
            (repo / "graphify-out").mkdir(parents=True)
            (repo / "graphify-out" / "graph.json").write_text(json.dumps({"nodes": [], "links": []}),
                                                             encoding="utf-8")
            (repo / "Jenkinsfile").write_text("pipeline { stages { stage('x') { sh 'make' } } }")
            (repo / "run.sh").write_text("#!/bin/sh\nmake deploy\n")
            (repo / "logo.png").write_bytes(b"\x89PNG\0\0")
            self.assertEqual(main([d, "--thresholds", "1"]), 0)

    def test_jenkins_report(self):
        import contextlib
        import io
        from summary_survey import jenkins_report
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "lib" / "vars").mkdir(parents=True)
            (Path(d) / "lib" / "vars" / "abActions.groovy").write_text("def call(Map cfg) { sh 'x' }")
            (Path(d) / "app").mkdir()
            (Path(d) / "app" / "Jenkinsfile").write_text("pipeline { steps { abActions(env: 'prod') } }")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                jenkins_report(Path(d))
            text = out.getvalue()
            self.assertIn("репозиториев-библиотек (vars/): 1 — их пересказывать первыми: lib", text)
            self.assertIn("1 описаний шагов", text)


if __name__ == "__main__":
    unittest.main()
