"""Проверка нарезки кода на функции: разборщики на месте и режут как надо.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_code_chunks

Qdrant, Ollama и сеть не нужны. Если что-то упало — пришлите вывод целиком.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from kb import code_chunks, code_index

LIMIT = code_index.MAX_CHUNK_CHARS

SAMPLES: dict[str, tuple[str, set[str]]] = {
    # имя файла: (код, символы, которые обязаны найтись)
    "server.go": ('''package main

// Server держит порт и обработчики.
type Server struct {
    port int
}

type Handler interface {
    Serve() error
}

// Start поднимает сервер.
func (s *Server) Start(ctx context.Context) error {
    return nil
}

func main() {
    fmt.Println("x")
}
''', {"Server", "Handler", "Server.Start", "main"}),
    "Payment.java": ('''package a;
public class Payment extends Base {
    public Payment(int x) { }
    public void charge(int amount) throws Exception {
        log.info("charge");
    }
    interface Inner { void go(); }
}
''', {"Payment", "Payment.Payment", "Payment.charge", "Payment.Inner"}),
    "Order.kt": ('''package a
class Order(val id: Int) {
    fun total(): Int { return 1 }
}
object Registry { fun get() = 1 }
fun main() { println("x") }
''', {"Order", "Order.total", "Registry", "main"}),
    "deploy.groovy": ('''def call(Map cfg) {
    pipeline { agent any }
}
class Deploy {
    def run(String env) { println env }
}
''', {"call", "Deploy", "Deploy.run"}),
    "api.js": ('''class Api {
  constructor() {}
  async get(id) { return 1 }
}
function handler(req, res) { }
const util = (a) => a + 1;
export function exported() {}
''', {"Api", "Api.constructor", "Api.get", "handler", "util", "exported"}),
    "svc.ts": ('''interface User { id: number }
export class Svc {
  private run(x: string): void {}
}
export const fetchUser = async (id: number): Promise<User> => { return {id} }
function g<T>(a: T): T { return a }
''', {"User", "Svc", "Svc.run", "fetchUser", "g"}),
    "App.tsx": ('''export function App() { return <div/> }
const Button = () => <button/>;
''', {"App", "Button"}),
    "Account.cs": ('''namespace A.B {
  public class Account {
    public Account() {}
    public void Debit(decimal x) { }
  }
  public interface IRepo { void Save(); }
}
''', {"Account", "Account.Account", "Account.Debit", "IRepo"}),
    "math.c": ('''#include <stdio.h>
struct point { int x; };
static int add(int a, int b) { return a + b; }
int *make(void) { return 0; }
''', {"point", "add", "make"}),
    "foo.cpp": ('''namespace ns {
class Foo { public: void bar(int x) { } };
}
void Foo::baz() const { }
int main() { return 0; }
''', {"Foo", "Foo.bar", "Foo::baz", "main"}),
    "Invoice.php": ('''<?php
class Invoice { public function total(): int { return 1; } }
function helper($a) { }
''', {"Invoice", "Invoice.total", "helper"}),
    "billing.rb": ('''module Billing
  class Invoice
    def total; 1; end
    def self.build; new; end
  end
end
''', {"Billing", "Billing.Invoice", "Billing.Invoice.total", "Billing.Invoice.build"}),
    "point.rs": ('''struct Point { x: i32 }
impl Point {
    fn new() -> Self { Point { x: 1 } }
}
fn main() {}
''', {"Point", "Point.new", "main"}),
    "Main.scala": ('''object Main { def main(args: Array[String]): Unit = {} }
class Svc { def run(x: Int): Int = x }
''', {"Main", "Main.main", "Svc", "Svc.run"}),
    "Car.swift": ('''class Car { func drive(speed: Int) {} }
func top() {}
''', {"Car", "Car.drive", "top"}),
    "deploy.sh": ('''#!/bin/bash
deploy() { echo hi; }
function build { make; }
''', {"deploy", "build"}),
    "tools.ps1": ('''function Get-Thing { param($a) Write-Host $a }
''', {"Get-Thing"}),
    "mod.lua": ('''local function helper(a) return a end
function M.run(x) return x end
''', {"helper", "M.run"}),
}


class Languages(unittest.TestCase):
    def test_every_language_finds_its_symbols(self):
        for name, (code, expected) in SAMPLES.items():
            with self.subTest(file=name):
                found = code_chunks.chunks(Path(name), code, LIMIT)
                self.assertIsNotNone(found, f"{name}: разборщик недоступен")
                symbols = {c["symbol"] for c in found}
                missing = expected - symbols
                self.assertFalse(missing, f"{name}: не нашлось {missing}, есть {symbols}")

    def test_line_numbers_and_text(self):
        code, _ = SAMPLES["server.go"]
        found = {c["symbol"]: c for c in code_chunks.chunks(Path("server.go"), code, LIMIT)}
        start = found["Server.Start"]
        self.assertEqual((start["line_start"], start["line_end"]), (13, 15))
        self.assertIn("func (s *Server) Start", start["text"])
        self.assertEqual(start["kind"], "method")
        self.assertIn("Start поднимает сервер", start["doc"])
        self.assertTrue(start["signature"].startswith("func (s *Server) Start"))

    def test_interface_methods_without_body_are_not_separate(self):
        code, _ = SAMPLES["Payment.java"]
        symbols = {c["symbol"] for c in code_chunks.chunks(Path("Payment.java"), code, LIMIT)}
        self.assertNotIn("Payment.Inner.go", symbols)

    def test_script_top_level_is_not_lost(self):
        code = "pipeline {\n  agent any\n  stages {\n    stage('Deploy') {\n      steps { sh 'make deploy' }\n    }\n  }\n}\n"
        found = code_chunks.chunks(Path("Jenkinsfile"), code, LIMIT)
        self.assertTrue(any("make deploy" in c["text"] for c in found))

    def test_jenkinsfile_name_variants(self):
        code = "pipeline {\n  stages { stage('d') { steps { sh 'make deploy' } } }\n}\n"
        for name in ("Jenkinsfile", "Jenkinsfile.deploy", "Jenkinsfile-prod",
                     "ci/deploy.jenkinsfile", "release.Jenkinsfile"):
            with self.subTest(name=name):
                found = code_chunks.chunks(Path(name), code, LIMIT)
                self.assertIsNotNone(found, name)
                self.assertTrue(any("make deploy" in c["text"] for c in found))

    def test_pipeline_block_kept_despite_big_helper(self):
        helper = "".join(f"    env.V{i} = 'x'\n" for i in range(200))
        code = f"def prepare() {{\n{helper}}}\npipeline {{\n  stages {{ stage('Deploy to prod') {{ steps {{ sh 'go' }} }} }}\n}}\n"
        found = code_chunks.chunks(Path("Jenkinsfile"), code, LIMIT)
        self.assertIn("prepare", {c["symbol"] for c in found})
        self.assertTrue(any("Deploy to prod" in c["text"] for c in found), "пайплайн потерялся")

    def test_pipeline_summary_in_doc(self):
        code = '''@Library(['adpc-jenkins-shared-libs@master', 'common']) _
pipeline {
    agent { label 'docker-builder' }
    parameters { booleanParam(name: 'DEPLOY', defaultValue: false) }
    triggers { cron('H 2 * * *') }
    stages {
        stage('Build') { steps { abActions action: 'build' } }
        stage('Bump') { steps { abBumpComponents() } }
        // stage('Old') { steps { oldStep() } }
        stage('Deploy to prod') { steps { sh 'make deploy' } }
    }
}
'''
        old = code_chunks.KNOWN_STEPS
        code_chunks.KNOWN_STEPS = {"abActions", "abBumpComponents", "oldStep", "unused"}
        try:
            found = code_chunks.chunks(Path("Jenkinsfile.deploy"), code, LIMIT)
        finally:
            code_chunks.KNOWN_STEPS = old
        doc = found[0]["doc"]
        self.assertTrue(all(c["doc"] == doc for c in found))
        self.assertIn("стадии: Build, Bump, Deploy to prod", doc)
        self.assertIn("шаги библиотеки: abActions, abBumpComponents", doc)
        self.assertNotIn("oldStep", doc)  # закомментирован
        self.assertNotIn("unused", doc)
        self.assertIn("библиотеки: adpc-jenkins-shared-libs, common", doc)
        self.assertIn("агент: docker-builder", doc)
        self.assertIn("параметры: DEPLOY", doc)
        self.assertIn("триггеры: cron H 2 * * *", doc)

    def test_script_with_functions_keeps_the_rest(self):
        code = "#!/bin/bash\nhelper() { echo; }\n" + "".join(f"echo step{i}\n" for i in range(20))
        found = code_chunks.chunks(Path("run.sh"), code, LIMIT)
        self.assertIn("helper", {c["symbol"] for c in found})
        self.assertTrue(any("step19" in c["text"] for c in found))

    def test_jenkins_step_is_named_after_file(self):
        # Параметр без типа — грамматика groovy 0.1.2 спотыкается на строке 1,
        # но функцию всё равно находит; так выглядит большинство vars/*.groovy
        code = '''def call(cfg) {
    helper(cfg)
    sh "make ${cfg.target}"
}
def helper(x) {
    echo x
}
'''
        found = code_chunks.chunks(Path("lib/vars/abActions.groovy"), code, LIMIT)
        symbols = {c["symbol"] for c in found}
        self.assertIn("abActions", symbols)
        self.assertIn("abActions.helper", symbols)
        self.assertNotIn("call", symbols)

    def test_job_dsl_is_one_chunk_per_job(self):
        code = '''pipelineJob('AA/access-server-repository') {
    description('Сборка access-server')
    parameters {
        stringParam('BRANCH', 'master', 'Ветка')
        booleanParam('DEPLOY', false, 'Деплоить после сборки')
    }
    definition {
        cpsScm {
            scm {
                git {
                    remote {
                        url(repoUrl)
                        credentials('jenkins-gitflic')
                    }
                    branch('${BRANCH}')
                }
            }
            scriptPath('Jenkinsfile')
        }
    }
}
'''
        path = Path("jobs/AA/access-server-repository/job.groovy")
        found = code_chunks.chunks(path, code, LIMIT)
        self.assertEqual(len(found), 1)
        job = found[0]
        self.assertEqual(job["symbol"], "AA/access-server-repository")
        self.assertEqual(job["kind"], "job")
        self.assertEqual((job["line_start"], job["line_end"]), (1, 21))
        self.assertIn("Сборка access-server", job["doc"])
        self.assertIn("параметры: BRANCH, DEPLOY", job["doc"])
        self.assertIn("скрипт: Jenkinsfile", job["doc"])
        self.assertIn("credentials('jenkins-gitflic')", job["text"])

    def test_job_dsl_name_from_variables_uses_folder(self):
        code = 'pipelineJob("${folder}/${name}") {\n    description("x")\n}\n'
        found = code_chunks.chunks(Path("repo/jobs/AA/installer/job.groovy"), code, LIMIT)
        self.assertEqual(found[0]["symbol"], "AA/installer")

    def test_job_dsl_two_jobs_in_file(self):
        code = "folder('AA')\npipelineJob('AA/a') {\n}\npipelineJob('AA/b') {\n  stringParam('X', '', '')\n}\n"
        found = code_chunks.chunks(Path("jobs/AA/job.groovy"), code, LIMIT)
        self.assertEqual([c["symbol"] for c in found], ["AA/a", "AA/b"])
        self.assertEqual(found[1]["line_start"], 4)
        self.assertIn("параметры: X", found[1]["doc"])

    def test_groovy_outside_vars_keeps_call(self):
        code = '''def call(Map cfg) {
    echo 'x'
}
'''
        found = code_chunks.chunks(Path("src/Deploy.groovy"), code, LIMIT)
        self.assertIn("call", {c["symbol"] for c in found})

    def test_unknown_extension_returns_none(self):
        self.assertIsNone(code_chunks.chunks(Path("x.unknown"), "abc", LIMIT))

    def test_long_function_is_truncated(self):
        body = "\n".join(f"    x{i} := {i}" for i in range(2000))
        code = f"package a\nfunc big() {{\n{body}\n}}\n"
        found = code_chunks.chunks(Path("big.go"), code, LIMIT)
        self.assertTrue(all(len(c["text"]) <= LIMIT for c in found))

    def test_broken_code_does_not_crash(self):
        found = code_chunks.chunks(Path("x.go"), "func ((( {{{ broken", LIMIT)
        self.assertIsNotNone(found)


class Collect(unittest.TestCase):
    """Обход каталога целиком: что берётся, что отбрасывается."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_what_is_taken_and_what_is_skipped(self):
        go, _ = SAMPLES["server.go"]
        java, _ = SAMPLES["Payment.java"]
        self.put("svc/cmd/server.go", go)
        self.put("svc/src/main/java/Payment.java", java)
        self.put("svc/Jenkinsfile", "pipeline { agent any }\n")
        self.put("svc/README.md", "# Сервис платежей\n\nПринимает платежи.\n")
        self.put("svc/db/schema.sql", "CREATE TABLE payments (id int);\n")
        # всё ниже в индекс попадать не должно
        self.put("svc/cmd/server_test.go", go)
        self.put("svc/src/test/java/PaymentTest.java", java)
        self.put("svc/src/main/java/PaymentTest.java", java)
        self.put("svc/web/app.spec.ts", "function t() {}\n")
        self.put("svc/api/api.pb.go", go)
        self.put("svc/web/lib.min.js", "function a(){}\n")
        self.put("svc/web/huge.js", "x".join([""] * 400_000))
        self.put("svc/target/Gen.java", java)
        self.put("svc/node_modules/x/index.js", "function x() {}\n")
        self.put("svc/logo.png", "not really png")
        self.put(".svc.partial/half.go", go)  # недокачанный клон
        self.put("graph/graph.json", "{}")

        items = code_index.collect(self.root)
        paths = {c["path"] for c in items}
        self.assertEqual(
            paths,
            {"cmd/server.go", "src/main/java/Payment.java", "Jenkinsfile", "README.md", "db/schema.sql"},
        )
        self.assertEqual({c["repo"] for c in items}, {"svc"})
        symbols = {c["symbol"] for c in items}
        self.assertIn("Server.Start", symbols)
        self.assertIn("Payment.charge", symbols)

    def test_pipeline_sees_steps_from_another_repo(self):
        # Шаг — в библиотеке, пайплайн — в проекте, и проект идёт раньше по алфавиту
        self.put("a-app/Jenkinsfile", "pipeline { stages { stage('B') { steps { abActions() } } } }\n")
        self.put("z-lib/vars/abActions.groovy", "def call() { echo 'x' }\n")
        items = code_index.collect(self.root)
        pipeline = [c for c in items if c["repo"] == "a-app"]
        self.assertTrue(pipeline)
        self.assertIn("шаги библиотеки: abActions", pipeline[0]["doc"])
        self.assertIn("стадии: B", pipeline[0]["doc"])

    def test_python_still_uses_ast(self):
        self.put("py/app.py", "class A:\n    def run(self):\n        return 1\n")
        symbols = {c["symbol"] for c in code_index.collect(self.root)}
        self.assertEqual(symbols, {"A", "A.run"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
