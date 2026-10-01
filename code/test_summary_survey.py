import json
import tempfile
import unittest
from pathlib import Path

from summary_survey import folder_units, from_graph_json, main, rel_path


class TestSurvey(unittest.TestCase):
    def test_rel_path(self):
        self.assertEqual(rel_path("/data/svc/src/a.go", "svc"), "src/a.go")
        self.assertEqual(rel_path("svc/src/a.go", "svc"), "src/a.go")
        self.assertEqual(rel_path("./.github/x.yml", "svc"), ".github/x.yml")
        self.assertEqual(rel_path("C:\\r\\svc\\b.py", "svc"), "b.py")

    def test_small_folders_merge_up(self):
        counts = {"": 1, "a": 5, "a/b": 5, "c": 30}
        # порог 10: a/b (5) -> a (10, отдельно); c отдельно; корень
        self.assertEqual(folder_units(counts, 10), 3)
        # порог 50: всё в корень
        self.assertEqual(folder_units(counts, 50), 1)
        # порог 1: каждая папка своя
        self.assertEqual(folder_units(counts, 1), 4)

    def test_missing_parent_is_created(self):
        self.assertEqual(folder_units({"x/y/z": 20}, 10), 2)

    def test_graph_json(self):
        with tempfile.TemporaryDirectory() as d:
            g = Path(d) / "svc" / "graphify-out"
            g.mkdir(parents=True)
            nodes = [{"id": f"n{i}", "label": f"f{i}()", "source_file": f"svc/pkg/m{i % 2}.go",
                      "source_location": f"L{i}"} for i in range(12)]
            nodes.append({"id": "doc", "label": "concept"})   # без файла — не считается
            (g / "graph.json").write_text(json.dumps({"nodes": nodes, "links": []}), encoding="utf-8")
            s = from_graph_json(g / "graph.json", "svc")
            self.assertEqual((s["nodes"], s["files"], s["folders"]), (12, 2, 1))
            self.assertEqual(main([d, "--thresholds", "10"]), 0)


if __name__ == "__main__":
    unittest.main()
