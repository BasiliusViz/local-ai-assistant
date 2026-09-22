"""Проверка repos/sync.py: python3 repos/test_sync.py

Сеть и настоящие GitFlic/Bitbucket не нужны. Часть тестов гоняет настоящий
git на локальных репозиториях (file://) и на заглушке HTTP-сервера, которая
отвечает 401/403 и запоминает, какой заголовок авторизации пришёл.

Если что-то упало — пришлите вывод целиком: токенов в нём нет, они тестовые.
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import sync  # noqa: E402

# Токен со всем, что ломает адрес и .env: @ : / # % $ и кавычка внутри
NASTY_TOKEN = "Ab@c:d/e#f%g$h'i"
HAS_GIT = shutil.which("git") is not None


def provider(name="BITBUCKET", url="https://bitbucket.company.local", user="svc", token="tok12345"):
    return sync.Provider(name=name, url=url, user=user, token=token)


# ------------------------------------------------------------- без git


class EnvFile(unittest.TestCase):
    def test_single_quotes_keep_special_chars(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repos").mkdir()
            (root / ".env").write_text(
                "X_TOKEN='Ab$c#d/e@f'\nX_HALF='abc\nX_PLAIN=abc\n", encoding="utf-8"
            )
            with mock.patch.object(sync, "HERE", root / "repos"), \
                 mock.patch.dict(os.environ, {}, clear=True):
                sync.load_env()
                self.assertEqual(os.environ["X_TOKEN"], "Ab$c#d/e@f")
                # непарная кавычка — часть значения, не снимаем
                self.assertEqual(os.environ["X_HALF"], "'abc")
                self.assertEqual(os.environ["X_PLAIN"], "abc")

    def test_environment_wins_over_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repos").mkdir()
            (root / ".env").write_text("X_TOKEN=from_file\n", encoding="utf-8")
            with mock.patch.object(sync, "HERE", root / "repos"), \
                 mock.patch.dict(os.environ, {"X_TOKEN": "from_env"}, clear=True):
                sync.load_env()
                self.assertEqual(os.environ["X_TOKEN"], "from_env")

    def test_providers_from_env(self):
        env = {
            "BITBUCKET_URL": "https://bb.local/",
            "BITBUCKET_USER": " svc ",
            "BITBUCKET_TOKEN": NASTY_TOKEN,
            "GITFLIC_URL": "",
        }
        ps = sync.load_providers(env)
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0].url, "https://bb.local")
        self.assertEqual(ps[0].user, "svc")
        self.assertEqual(ps[0].token, NASTY_TOKEN)


class ListFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repos").mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_comments_blank_lines_and_inline_comments(self):
        f = self.tmp / "list.txt"
        f.write_text(
            "# шапка\n"
            "\n"
            "https://bb.local/scm/P/a.git\n"
            "   https://bb.local/scm/P/b.git@develop   # платёжка\n"
            "# https://bb.local/scm/P/off.git\n"
            "\t\n",
            encoding="utf-8",
        )
        self.assertEqual(
            sync.read_list_file(f).splitlines(),
            ["https://bb.local/scm/P/a.git", "https://bb.local/scm/P/b.git@develop"],
        )

    def test_relative_path_from_project_root_and_merge_with_env(self):
        (self.tmp / "repos" / "list.txt").write_text("https://bb.local/scm/P/a.git\n", encoding="utf-8")
        env = {"CODE_GIT_REPOS_FILE": "repos/list.txt", "CODE_GIT_REPOS": "https://bb.local/scm/P/b.git"}
        with mock.patch.object(sync, "HERE", self.tmp / "repos"):
            repos = sync.parse_list(sync.repo_list(env), [provider(url="https://bb.local")])
        self.assertEqual(sorted(r.dirname for r in repos), ["bitbucket-P-a", "bitbucket-P-b"])

    def test_missing_file_is_error(self):
        with mock.patch.object(sync, "HERE", self.tmp / "repos"), \
             self.assertRaises(sync.SyncError) as cm:
            sync.repo_list({"CODE_GIT_REPOS_FILE": "repos/nope.txt"})
        self.assertIn("nope.txt", str(cm.exception))

    def test_multiline_list_in_env_is_reported(self):
        (self.tmp / ".env").write_text(
            "CODE_GIT_REPOS=https://bb.local/scm/P/a.git,\n"
            "https://bb.local/scm/P/b.git\n"
            "  https://bb.local/scm/P/c.git@dev\n"
            "OTHER=1\n",
            encoding="utf-8",
        )
        with mock.patch.object(sync, "HERE", self.tmp / "repos"), \
             mock.patch.dict(os.environ, {}, clear=True):
            warnings = sync.load_env()
            self.assertEqual(os.environ["OTHER"], "1")
        self.assertEqual(len(warnings), 2)
        self.assertIn("строка 2", warnings[0])
        self.assertIn("CODE_GIT_REPOS_FILE", warnings[0])

    def test_normal_env_has_no_warnings(self):
        (self.tmp / ".env").write_text(
            "# https://example.com в комментарии — не адрес\n"
            # эта строка есть в старом .env.example — ложная тревога у всех
            "#   https://oauth2:ТОКЕН@git.company.local/team/service.git\n"
            "BITBUCKET_URL=https://bb.local\n",
            encoding="utf-8",
        )
        with mock.patch.object(sync, "HERE", self.tmp / "repos"), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sync.load_env(), [])


class Parsing(unittest.TestCase):
    def setUp(self):
        self.ps = [
            provider(),
            provider("GITFLIC", "https://gitflic.company.local", "u2", "tok2xxxx"),
        ]

    def test_bitbucket(self):
        r = sync.parse_repo("https://bitbucket.company.local/scm/PROJ/billing.git", self.ps)
        self.assertEqual(r.provider.name, "BITBUCKET")
        self.assertEqual(r.dirname, "bitbucket-PROJ-billing")
        self.assertEqual(r.branch, "")

    def test_gitflic(self):
        r = sync.parse_repo("https://gitflic.company.local/project/team/service-a.git", self.ps)
        self.assertEqual(r.provider.name, "GITFLIC")
        self.assertEqual(r.dirname, "gitflic-team-service-a")

    def test_branch(self):
        r = sync.parse_repo("https://bitbucket.company.local/scm/P/r.git@develop", self.ps)
        self.assertEqual(r.branch, "develop")
        self.assertEqual(r.url, "https://bitbucket.company.local/scm/P/r.git")

    def test_branch_with_slash(self):
        r = sync.parse_repo("https://bitbucket.company.local/scm/P/r.git@feature/x-1", self.ps)
        self.assertEqual(r.branch, "feature/x-1")

    def test_host_case_insensitive(self):
        r = sync.parse_repo("https://BitBucket.Company.Local/scm/P/r.git", self.ps)
        self.assertEqual(r.provider.name, "BITBUCKET")

    def test_lookalike_host_gets_no_token(self):
        r = sync.parse_repo("https://bitbucket.company.local.evil.com/scm/P/r.git", self.ps)
        self.assertIsNone(r.provider)

    def test_context_path(self):
        ps = [provider(url="https://git.local/bitbucket")]
        r = sync.parse_repo("https://git.local/bitbucket/scm/P/r.git", ps)
        self.assertEqual(r.provider.name, "BITBUCKET")
        self.assertEqual(r.dirname, "bitbucket-P-r")
        # тот же хост, но вне пути сервиса — без токена
        self.assertIsNone(sync.parse_repo("https://git.local/other/r.git", ps).provider)

    def test_longest_prefix_wins(self):
        ps = [provider("GITFLIC", "https://git.local"), provider("BITBUCKET", "https://git.local/bb")]
        r = sync.parse_repo("https://git.local/bb/scm/P/r.git", ps)
        self.assertEqual(r.provider.name, "BITBUCKET")

    def test_credentials_in_url_rejected_and_not_echoed(self):
        with self.assertRaises(sync.SyncError) as cm:
            sync.parse_repo(f"https://svc:secret99@bitbucket.company.local/scm/P/r.git", self.ps)
        self.assertNotIn("secret99", str(cm.exception))
        self.assertIn(".env", str(cm.exception))

    def test_ssh_rejected(self):
        with self.assertRaises(sync.SyncError):
            sync.parse_repo("ssh://git@bitbucket.company.local:7999/p/r.git", self.ps)
        with self.assertRaises(sync.SyncError):
            sync.parse_repo("git@bitbucket.company.local:p/r.git", self.ps)

    def test_list_separators_and_duplicates(self):
        raw = """ https://bitbucket.company.local/scm/P/a.git,
                  https://bitbucket.company.local/scm/P/b.git
                  https://bitbucket.company.local/scm/P/a.git """
        repos = sync.parse_list(raw, self.ps)
        self.assertEqual([r.dirname for r in repos], ["bitbucket-P-a", "bitbucket-P-b"])

    def test_same_dir_from_two_urls_is_error(self):
        raw = ("https://bitbucket.company.local/scm/P/a.git "
               "https://bitbucket.company.local/scm/P/a")
        with self.assertRaises(sync.SyncError):
            sync.parse_list(raw, self.ps)


class Auth(unittest.TestCase):
    def test_basic_survives_special_chars(self):
        h = sync.auth_header(provider(user="svc", token=NASTY_TOKEN))
        self.assertTrue(h.startswith("Authorization: Basic "))
        decoded = base64.b64decode(h.split()[-1]).decode("utf-8")
        self.assertEqual(decoded, f"svc:{NASTY_TOKEN}")

    def test_bearer_without_user(self):
        self.assertEqual(sync.auth_header(provider(user="", token="t1234")), "Authorization: Bearer t1234")

    def test_git_env_scopes_header_to_service(self):
        env = sync.git_env([provider(url="https://bb.local")], cainfo="/etc/ca.pem")
        pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        self.assertIn("http.https://bb.local/.extraHeader", pairs)
        self.assertNotIn("http.extraHeader", pairs)  # не на все хосты подряд
        self.assertEqual(pairs["credential.helper"], "")
        self.assertEqual(pairs["http.sslCAInfo"], "/etc/ca.pem")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_empty_token_sends_no_header(self):
        env = sync.git_env([provider(token="")])
        keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        self.assertFalse(any("extraHeader" in k for k in keys))

    def test_mask_hides_token_and_base64(self):
        p = provider(token=NASTY_TOKEN)
        b64 = sync.auth_header(p).split()[-1]
        text = f"x {NASTY_TOKEN} y {b64} z"
        masked = sync.mask(text, sync.secrets([p]))
        self.assertNotIn(NASTY_TOKEN, masked)
        self.assertNotIn(b64, masked)


class Explain(unittest.TestCase):
    CASES = {
        "fatal: could not read Username for 'https://x': terminal prompts disabled": "401",
        "fatal: Authentication failed for 'https://x/'": "401",
        "The requested URL returned error: 403": "403",
        "fatal: repository 'https://x/r.git/' not found": "404",
        "SSL certificate problem: unable to get local issuer certificate": "сертификат",
        "Could not resolve host: bb.local": "DNS",
        "Failed to connect to bb.local port 443: Connection refused": "нет связи",
        "warning: Could not find remote branch dev to clone.\nfatal: Remote branch dev not found in upstream origin": "ветки",
    }

    def test_messages(self):
        for stderr, expected in self.CASES.items():
            with self.subTest(stderr=stderr):
                self.assertIn(expected, sync.explain(stderr))


# --------------------------------------------------------------- с git


def git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@unittest.skipUnless(HAS_GIT, "git не найден")
class RealGit(unittest.TestCase):
    """Настоящий git на локальных репозиториях: клон, обновление, ветки."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bare = self.tmp / "origin" / "app.git"
        self.work = self.tmp / "work"
        git("init", "--bare", str(self.bare))
        git("init", str(self.work))
        (self.work / "a.txt").write_text("v1", encoding="utf-8")
        git("add", ".", cwd=self.work)
        git("commit", "-m", "v1", cwd=self.work)
        git("push", str(self.bare), "HEAD:main", cwd=self.work)
        git("checkout", "-b", "develop", cwd=self.work)
        (self.work / "a.txt").write_text("dev", encoding="utf-8")
        git("commit", "-am", "dev", cwd=self.work)
        git("push", str(self.bare), "develop", cwd=self.work)
        git("checkout", "main", cwd=self.work)
        git("--git-dir", str(self.bare), "symbolic-ref", "HEAD", "refs/heads/main")
        self.url = self.bare.as_uri()
        self.dest = self.tmp / "all_git"
        self.dest.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, *args, repos=None):
        env = {
            "CODE_DIR": str(self.dest),
            "CODE_GIT_REPOS": repos if repos is not None else self.url,
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(sync, "load_env", lambda: []), \
             contextlib.redirect_stdout(out):
            code = sync.main(list(args))
        return code, out.getvalue()

    def cloned(self, name_part="app"):
        dirs = [p for p in self.dest.iterdir() if name_part in p.name and not p.name.startswith(".")]
        self.assertEqual(len(dirs), 1, f"ожидался один каталог, есть: {list(self.dest.iterdir())}")
        return dirs[0]

    def push_commit(self, text):
        (self.work / "a.txt").write_text(text, encoding="utf-8")
        git("commit", "-am", text, cwd=self.work)
        git("push", str(self.bare), "HEAD:main", cwd=self.work)

    def test_check_does_not_download(self):
        code, out = self.run_main("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("[ok]", out)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_dry_run_does_not_download(self):
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("[клон]", out)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_clone_then_update(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        repo = self.cloned()
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "v1")

        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("без изменений  1", out)

        self.push_commit("v2")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("обновлено      1", out)
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "v2")

    def test_update_survives_force_push_and_local_edits(self):
        self.run_main()
        repo = self.cloned()
        (repo / "a.txt").write_text("локальная правка", encoding="utf-8")
        (repo / "graphify-out").mkdir()  # так его оставляет граф кода
        git("commit", "--amend", "-m", "rewritten", cwd=self.work)
        git("push", "--force", str(self.bare), "HEAD:main", cwd=self.work)
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "v1")
        self.assertTrue((repo / "graphify-out").is_dir(), "неотслеживаемое не трогаем")

    def test_branch(self):
        code, out = self.run_main(repos=self.url + "@develop")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.cloned() / "a.txt").read_text(encoding="utf-8"), "dev")

    def test_missing_branch_leaves_nothing(self):
        code, out = self.run_main(repos=self.url + "@nope")
        self.assertEqual(code, 1, out)
        self.assertIn("ветки", out)
        self.assertEqual(list(self.dest.iterdir()), [], "недокачанный клон должен убираться")

    def test_check_reports_missing_branch(self):
        code, out = self.run_main("--check", repos=self.url + "@nope")
        self.assertEqual(code, 1, out)
        self.assertIn("ветки nope нет", out)

    def test_foreign_directory_is_not_touched(self):
        repo = sync.parse_repo(self.url, [])
        (self.dest / repo.dirname).mkdir()
        (self.dest / repo.dirname / "mine.txt").write_text("x", encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertIn("не git-репозиторий", out)
        self.assertTrue((self.dest / repo.dirname / "mine.txt").exists())

    def test_other_repo_in_directory_is_not_touched(self):
        repo = sync.parse_repo(self.url, [])
        git("clone", str(self.bare), str(self.dest / repo.dirname))
        git("remote", "set-url", "origin", "https://elsewhere.local/x.git", cwd=self.dest / repo.dirname)
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertIn("другой репозиторий", out)

    def remote_refs(self):
        out = subprocess.run(
            ["git", "--git-dir", str(self.bare), "for-each-ref", "--format=%(refname) %(objectname)"],
            capture_output=True, text=True, check=True,
        ).stdout
        return sorted(out.splitlines())

    def test_remote_is_never_changed(self):
        """Полный цикл — check, dry-run, клон, обновление, ошибка ветки —
        не меняет в удалённом репозитории ни одной ветки и ни одного коммита."""
        before = self.remote_refs()
        objects_before = sorted(p.name for p in (self.bare / "objects").rglob("*") if p.is_file())
        self.run_main("--check")
        self.run_main("--dry-run")
        self.run_main()
        repo = self.cloned()
        (repo / "a.txt").write_text("локальная правка", encoding="utf-8")
        self.run_main()
        self.run_main(repos=self.url + "@develop")
        self.run_main(repos=self.url + "@nope")
        self.assertEqual(self.remote_refs(), before)
        objects_after = sorted(p.name for p in (self.bare / "objects").rglob("*") if p.is_file())
        self.assertEqual(objects_after, objects_before)

    def test_writing_commands_are_refused(self):
        g = sync.Git([])
        for args in (
            ["push", "origin", "--delete", "main"],
            ["push", "--force"],
            ["branch", "-D", "main"],
            ["tag", "-d", "v1"],
            ["remote", "set-url", "origin", "x"],
            ["remote", "remove", "origin"],
            ["clean", "-fdx"],
            ["gc", "--prune=now"],
            [],
        ):
            with self.subTest(args=args), self.assertRaises(sync.SyncError):
                g.run(args, cwd=self.tmp)

    def test_one_failure_does_not_stop_others(self):
        missing = (self.tmp / "origin" / "missing.git").as_uri()
        code, out = self.run_main(repos=f"{missing} {self.url}")
        self.assertEqual(code, 1, out)
        self.cloned("app")
        self.assertIn("склонировано   1", out)


# ------------------------------------------------------ заглушка HTTP


class Recorder(BaseHTTPRequestHandler):
    status = 401
    seen: list = []

    def do_GET(self):
        type(self).seen.append((self.server.server_address[1], self.headers.get("Authorization")))
        self.send_response(self.status)
        if self.status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="stub"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@unittest.skipUnless(HAS_GIT, "git не найден")
class HttpAuth(unittest.TestCase):
    """Что git реально отправляет на сервер и как мы объясняем отказ."""

    def start(self, status):
        handler = type("H", (Recorder,), {"status": status, "seen": []})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, handler

    def check(self, repos, providers):
        env = {
            "CODE_DIR": tempfile.mkdtemp(),
            "CODE_GIT_REPOS": repos,
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }
        for p in providers:
            env[f"{p.name}_URL"] = p.url
            env[f"{p.name}_USER"] = p.user
            env[f"{p.name}_TOKEN"] = p.token
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(sync, "load_env", lambda: []), \
             contextlib.redirect_stdout(out):
            code = sync.main(["--check"])
        shutil.rmtree(env["CODE_DIR"], ignore_errors=True)
        return code, out.getvalue()

    def test_header_sent_and_token_never_printed(self):
        srv, h = self.start(401)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        p = provider(url=base, user="svc", token=NASTY_TOKEN)
        code, out = self.check(f"{base}/scm/P/r.git", [p])

        self.assertEqual(code, 1, out)
        self.assertIn("неверный логин или токен", out)
        self.assertNotIn(NASTY_TOKEN, out)
        sent = [a for _, a in h.seen if a]
        self.assertTrue(sent, "git не отправил заголовок авторизации")
        self.assertEqual(base64.b64decode(sent[0].split()[-1]).decode(), f"svc:{NASTY_TOKEN}")

    def test_forbidden(self):
        srv, _ = self.start(403)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        code, out = self.check(f"{base}/scm/P/r.git", [provider(url=base)])
        self.assertEqual(code, 1, out)
        self.assertIn("нет прав (403)", out)

    def test_header_not_sent_to_other_server(self):
        ours, _ = self.start(401)
        other, h_other = self.start(401)
        base = f"http://127.0.0.1:{ours.server_address[1]}"
        foreign = f"http://127.0.0.1:{other.server_address[1]}/scm/P/r.git"
        code, out = self.check(foreign, [provider(url=base, token=NASTY_TOKEN)])
        self.assertEqual(code, 1, out)
        self.assertTrue(h_other.seen, "запрос до чужого сервера не дошёл")
        self.assertEqual([a for _, a in h_other.seen if a], [], "токен ушёл на чужой сервер")


if __name__ == "__main__":
    unittest.main(verbosity=2)
