import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import summarize as S


class FakeModel:
    model = "fake"

    def __init__(self):
        self.prompts = []
        self.fails = 0

    def chat(self, prompt, as_json):
        self.prompts.append(prompt)
        if as_json:
            return json.dumps({"summary": f"папка {len(self.prompts)}", "tags": ["Тег"]})
        return "## Назначение\nчто-то"


def make_repo(root: Path) -> Path:
    r = root / "svc"
    (r / "src" / "deep").mkdir(parents=True)
    (r / "big").mkdir()
    (r / "Dockerfile").write_text("FROM python:3.12\nCMD python -m svc\n")
    (r / "src" / "a.py").write_text("def a():\n    return 1\n")
    (r / "src" / "deep" / "b.py").write_text("x = 1\n")
    big = ['"""Модуль большой."""'] + [f"def f{i}(x):\n    # делает {i}\n    return x" for i in range(400)]
    (r / "big" / "huge.py").write_text("\n".join(big))
    g = r / "graphify-out"
    g.mkdir()
    nodes = [{"id": f"n{i}", "label": f"f{i}()", "source_file": "svc/big/huge.py",
              "source_location": f"L{2 + i * 3}"} for i in range(3)]
    (g / "graph.json").write_text(json.dumps({"nodes": nodes, "links": []}))
    return r


class TestSummarize(unittest.TestCase):
    def test_plan_units_merges_small(self):
        p = S.plan_units({"": 0.1, "a": 0.5, "a/b": 0.5, "c": 3.0}, 1)
        self.assertEqual(p, {"": None, "a": "", "c": ""})
        self.assertEqual(S.unit_of("a/b", p), "a")
        self.assertEqual(S.plan_units({"x/y": 5}, 1), {"": None, "x/y": ""})   # пустая x — в корень
        # порог 0: каждая папка с файлами — свой пересказ, пустая a — нет
        self.assertEqual(S.plan_units({"": 0.1, "a/b": 0.01, "a/c": 0.2}, 0),
                         {"": None, "a/b": "", "a/c": ""})

    def test_skeleton_takes_lines_and_docs(self):
        lines = ["# шапка", "import os", "def f(x):", '    """Делает f."""', "    return x"]
        sk = S.skeleton(lines, [3, 3, 99])
        self.assertIn("# шапка", sk)
        self.assertIn("L3: def f(x):", sk)
        self.assertIn('"""Делает f."""', sk)
        self.assertNotIn("import os", sk)

    def test_prepare_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            r = make_repo(Path(t))
            prep = S.prepare_repo(r, "svc", r / "graphify-out" / "graph.json", 4, 8000)
            self.assertIn("(скелет)", prep["blocks"]["big/huge.py"])
            self.assertIn("L5: def f1(x):", prep["blocks"]["big/huge.py"])
            self.assertIn("(целиком)", prep["blocks"]["src/a.py"])
            self.assertEqual(set(prep["parents"]), {""})          # всё мелкое — в корень
            self.assertEqual(prep["key"], ["Dockerfile"])

    def test_budget_lists_skipped_files(self):
        prep = {"files": {"": ["a", "b"]}, "blocks": {"a": "x" * 3500, "b": "y" * 35000}}
        body = S.folder_body(prep, "", {}, max_ktok=2)
        self.assertIn("x" * 100, body)
        self.assertIn("Не показаны (не влезли), 1", body)

    def test_run_is_incremental(self):
        with tempfile.TemporaryDirectory() as t:
            r = make_repo(Path(t))
            prep = S.prepare_repo(r, "svc", None, 0.001, 8000)     # каждая папка — свой вызов
            state, m = {}, FakeModel()
            res = S.run_repo("svc", r, prep, state, m, "платёжный сервис", 24, lambda: None, log=lambda *_: 0)
            self.assertEqual(res["calls"], len(prep["parents"]) + 1)
            self.assertIn("платёжный сервис", m.prompts[0])
            # родитель видит пересказ подпапки
            src = next(p for p in m.prompts if "папку «src»" in p)
            self.assertIn("## Подпапка src/deep", src)
            self.assertEqual(state["svc"]["folders"]["src"]["tags"], ["тег"])

            m2 = FakeModel()
            res = S.run_repo("svc", r, prep, state, m2, "платёжный сервис", 24, lambda: None, log=lambda *_: 0)
            self.assertEqual((res["calls"], len(m2.prompts)), (0, 0))

            (r / "src" / "deep" / "b.py").write_text("x = 2\n")
            prep = S.prepare_repo(r, "svc", None, 0.001, 8000)
            m3 = FakeModel()
            S.run_repo("svc", r, prep, state, m3, "платёжный сервис", 24, lambda: None, log=lambda *_: 0)
            # изменилась src/deep -> её пересказ и предки (их вход — новый пересказ); big — нет
            self.assertTrue(any("папку «src/deep»" in p for p in m3.prompts))
            self.assertFalse(any("папку «big»" in p for p in m3.prompts))

            (Path(t) / "svc.md").write_text("старый формат")
            S.write_md(Path(t), "svc", state["svc"], prep)
            self.assertFalse((Path(t) / "svc.md").exists())
            md = (Path(t) / "svc" / "README.md").read_text(encoding="utf-8")
            self.assertIn("Сгенерировано моделью", md)
            self.assertIn("[src](src/README.md)", md)
            self.assertIn("`Dockerfile`", md)
            sub = (Path(t) / "svc" / "src" / "deep" / "README.md").read_text(encoding="utf-8")
            self.assertIn("# svc/src/deep", sub)
            # шапка для kb.doc_index: заголовок оттуда, дата — вне хеша текста
            self.assertTrue(sub.startswith("---\ntitle: svc/src/deep (описание кода)\ngenerated: "))
            self.assertNotRegex(sub.split("\n---\n", 1)[1], r"\d\d\.\d\d\.\d{4}")
            self.assertIn("Теги: тег", sub)
            self.assertIn("`b.py`", sub)

    def test_model_errors(self):
        class Broken(FakeModel):
            def __init__(self, ok):
                super().__init__()
                self.ok = ok

            def chat(self, prompt, as_json):
                if "папку «src/deep»" not in prompt and self.ok:
                    return super().chat(prompt, as_json)
                raise TimeoutError("долго")

        with tempfile.TemporaryDirectory() as t:
            r = make_repo(Path(t))
            prep = S.prepare_repo(r, "svc", None, 0.001, 8000)
            state, logs = {}, []
            S.run_repo("svc", r, prep, state, Broken(True), None, 24, lambda: None, log=logs.append)
            self.assertNotIn("readme", state["svc"])               # одна папка не готова — README ждёт
            self.assertTrue(any("README отложен" in x for x in logs))
            with self.assertRaises(S.ModelDown), unittest.mock.patch.object(S, "MAX_FAILS", 2):
                # модель лежит — стоп, а не 2422 таймаута
                S.run_repo("svc", r, prep, {}, Broken(False), None, 24, lambda: None, log=logs.append)

    def test_parse_folder_fallback(self):
        self.assertEqual(S.parse_folder("просто текст"), {"summary": "просто текст", "tags": []})

    def test_main_prepare(self):
        with tempfile.TemporaryDirectory() as t:
            make_repo(Path(t))
            self.assertEqual(S.main(["prepare", t, "--repo", "svc", "--env", str(Path(t) / "none")]), 0)
            files = list((Path(t) / ".summaries" / "_prep" / "svc").glob("*.txt"))
            self.assertTrue(files)


if __name__ == "__main__":
    unittest.main()
