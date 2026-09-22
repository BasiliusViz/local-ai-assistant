"""Клонирование и обновление репозиториев из GitFlic и Bitbucket по токену.

Репозитории складываются в CODE_DIR (например, all_git), откуда их берут граф
кода и поиск по коду. Запускается на самом сервере, а не в контейнере: git
там уже есть, а клоны принадлежат обычному пользователю, а не root.

Рассчитан на запуск в закрытом контуре без посторонней помощи, поэтому:
  - сначала проверяет доступ ко всем репозиториям, ничего не скачивая (--check)
  - показывает, что будет сделано, ничего не трогая (--dry-run)
  - объясняет ошибки словами: неверный токен, нет прав, нет репозитория
  - токены не печатает никогда, даже в ошибках git

Токен НЕ вставляется в адрес. В адресе спецсимволы токена (@ : / # %) ломают
разбор, а сам адрес git сохраняет в .git/config каждого клона — вместе с
токеном. Вместо этого git получает заголовок Authorization через переменные
окружения GIT_CONFIG_* (не видны в `ps` другим пользователям), причём
заголовок привязан к хосту сервиса: на чужой хост он не уйдёт.

Настройки — в .env в корне проекта:

    CODE_DIR          куда класть репозитории: /srv/all_git
    CODE_GIT_REPOS    адреса через запятую или пробел, ветка после @:
                      https://bitbucket.company.local/scm/PROJ/billing.git@develop
    BITBUCKET_URL     https://bitbucket.company.local
    BITBUCKET_USER    логин
    BITBUCKET_TOKEN   HTTP access token — в .env в ОДИНАРНЫХ кавычках
    GITFLIC_URL       https://gitflic.company.local
    GITFLIC_USER      логин
    GITFLIC_TOKEN     токен — тоже в одинарных кавычках
    CODE_GIT_CAINFO   путь к корневому сертификату компании, если нужен

Использование:
    python3 repos/sync.py --check      доступ к каждому репозиторию
    python3 repos/sync.py --dry-run    что будет склонировано и обновлено
    python3 repos/sync.py              склонировать новые, обновить остальные
    python3 repos/sync.py --only billing   только репозитории с «billing» в имени

Проверка самого скрипта: python3 repos/test_sync.py
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).parent

# Сервисы с токенами. Добавить ещё один (GitLab и т.п.) = дописать имя сюда и
# завести в .env <ИМЯ>_URL, <ИМЯ>_USER, <ИМЯ>_TOKEN
PROVIDERS = ("BITBUCKET", "GITFLIC")

# Служебный сегмент пути, который в имени каталога не нужен:
# Bitbucket — /scm/PROJ/repo.git, GitFlic — /project/owner/repo.git
SERVICE_SEGMENTS = {"scm", "project"}

# Минимальная версия git: переменные GIT_CONFIG_COUNT/KEY/VALUE появились в 2.31
MIN_GIT = (2, 31)

# Единственные команды git, которые скрипт имеет право вызвать. На сервер
# (Bitbucket, GitFlic) ходят только clone, fetch и ls-remote — все три только
# читают. Остальные работают с локальным клоном. push, branch -d, tag -d и
# прочего, что меняет удалённый репозиторий, здесь нет и быть не должно:
# Git.run откажется их выполнить, даже если кто-то допишет вызов
ALLOWED_GIT = {"clone", "fetch", "ls-remote", "reset", "rev-parse", "remote"}
# У remote — только чтение адреса: set-url, add, remove запрещены
ALLOWED_REMOTE = {"get-url"}

CLONE_TIMEOUT = 30 * 60
CHECK_TIMEOUT = 60


class SyncError(RuntimeError):
    """Ошибка с человеческим объяснением, а не с трейсбеком."""


@dataclass
class Provider:
    name: str
    url: str
    user: str
    token: str

    @property
    def prefix(self) -> str:
        return self.url.rstrip("/") + "/"


@dataclass
class Repo:
    url: str  # без ветки
    branch: str  # пусто — основная ветка сервера
    dirname: str
    provider: Provider | None


# ---------------------------------------------------------------- настройки


def load_env() -> None:
    """Читает .env проекта. Заданные переменные окружения важнее файла."""
    for env_file in (HERE / ".env", HERE.parent / ".env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            # Кавычки снимаем только парные: токен вполне может начинаться
            # или заканчиваться кавычкой сам по себе
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def load_providers(env: Mapping[str, str] | None = None) -> list[Provider]:
    source: Mapping[str, str] = os.environ if env is None else env
    out = []
    for name in PROVIDERS:
        url = source.get(f"{name}_URL", "").strip()
        if not url:
            continue
        out.append(
            Provider(
                name=name,
                url=url.rstrip("/"),
                user=source.get(f"{name}_USER", "").strip(),
                token=source.get(f"{name}_TOKEN", ""),
            )
        )
    return out


def _norm(url: str) -> str:
    """Схема и хост без учёта регистра, путь как есть."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"


def match_provider(url: str, providers: list[Provider]) -> Provider | None:
    """Сервис, которому принадлежит адрес. Самый длинный префикс побеждает:
    два сервиса могут жить на одном хосте под разными путями."""
    target = _norm(url)
    best = None
    for p in providers:
        if target.startswith(_norm(p.prefix)):
            if best is None or len(p.prefix) > len(best.prefix):
                best = p
    return best


def parse_repo(entry: str, providers: list[Provider]) -> Repo:
    """Строка из CODE_GIT_REPOS -> Repo.

    Ветка отделяется @ в ПУТИ, а не в адресе целиком: иначе логин@хост
    приняли бы за ветку. Ветка может содержать / (feature/x).
    """
    parts = urlsplit(entry)
    if parts.scheme not in ("https", "http", "file"):
        raise SyncError(
            f"{entry}: нужен адрес https://... (SSH не поддерживается — токен "
            "работает только по HTTPS)"
        )
    if parts.username or parts.password:
        raise SyncError(
            f"{_strip_userinfo(entry)}: логин или токен прямо в адресе. Уберите их "
            "из адреса — они задаются в .env (<СЕРВИС>_USER и <СЕРВИС>_TOKEN)"
        )

    path, _, branch = parts.path.partition("@")
    url = f"{parts.scheme}://{parts.netloc}{path}"
    return Repo(
        url=url,
        branch=branch.strip("/"),
        dirname=dir_name(url, match_provider(url, providers)),
        provider=match_provider(url, providers),
    )


def _strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return f"{parts.scheme}://***@{host}{parts.path}"


def dir_name(url: str, provider: Provider | None) -> str:
    """Каталог клона: сервис-проект-репозиторий.

    Одно имя репозитория ненадёжно: backend.git бывает в каждом проекте, и
    второй клон лёг бы поверх первого.
    """
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if provider:
        base = [s for s in urlsplit(provider.url).path.split("/") if s]
        segments = segments[len(base) :]
        prefix = provider.name.lower()
    else:
        prefix = (parts.hostname or "local").split(".")[0]
    if segments and segments[0].lower() in SERVICE_SEGMENTS:
        segments = segments[1:]
    if segments and segments[-1].endswith(".git"):
        segments[-1] = segments[-1][:-4]
    name = "-".join([prefix, *segments])
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def parse_list(raw: str, providers: list[Provider]) -> list[Repo]:
    entries = [e for e in re.split(r"[\s,]+", raw) if e]
    repos = [parse_repo(e, providers) for e in entries]
    seen: dict[str, str] = {}
    for r in repos:
        if r.dirname in seen and seen[r.dirname] != r.url:
            raise SyncError(
                f"Два адреса дают один каталог {r.dirname}:\n  {seen[r.dirname]}\n  {r.url}"
            )
        seen[r.dirname] = r.url
    # Повтор одного и того же адреса — не ошибка, просто лишняя строка
    unique: dict[str, Repo] = {}
    for r in repos:
        unique.setdefault(r.dirname, r)
    return list(unique.values())


# ---------------------------------------------------------------------- git


def auth_header(provider: Provider) -> str:
    """Basic при заданном логине (так работает Bitbucket Server), иначе Bearer.

    base64 делает спецсимволы токена безопасными: в заголовке нечего
    экранировать, в отличие от адреса.
    """
    if provider.user:
        pair = f"{provider.user}:{provider.token}".encode("utf-8")
        return "Authorization: Basic " + base64.b64encode(pair).decode("ascii")
    return f"Authorization: Bearer {provider.token}"


def git_env(providers: list[Provider], cainfo: str = "") -> dict[str, str]:
    """Окружение для git: заголовки по хостам и отключённые подсказки.

    Всё через GIT_CONFIG_*, а не `-c` в командной строке: аргументы процесса
    видны любому пользователю машины в `ps`, окружение — только владельцу.
    """
    env = dict(os.environ)
    config = [
        # Не спрашивать логин в терминале и не звать менеджер паролей: на
        # сервере по расписанию это зависание, а не вопрос
        ("credential.helper", ""),
        ("core.askPass", ""),
        # Оборвать, если за минуту не пришло и килобайта: иначе повисший
        # сервер держит прогон бесконечно
        ("http.lowSpeedLimit", "1000"),
        ("http.lowSpeedTime", "60"),
    ]
    if cainfo:
        config.append(("http.sslCAInfo", cainfo))
    for p in providers:
        if p.token:
            # http.<адрес>.extraHeader — git отправит заголовок только на этот
            # хост и путь, редирект на чужой сервер его не получит
            config.append((f"http.{p.prefix}.extraHeader", auth_header(p)))

    env["GIT_CONFIG_COUNT"] = str(len(config))
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    return env


def secrets(providers: list[Provider]) -> list[str]:
    out = []
    for p in providers:
        if p.token:
            out.append(p.token)
            out.append(auth_header(p).split(" ", 2)[-1])
    return [s for s in out if len(s) >= 4]


def mask(text: str, hidden: list[str]) -> str:
    for s in sorted(hidden, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def explain(stderr: str) -> str:
    """Ошибка git -> что с ней делать."""
    s = stderr.lower()
    rules = [
        (("401", "authentication failed", "could not read username", "invalid credentials"),
         "неверный логин или токен (401). Проверьте <СЕРВИС>_USER и <СЕРВИС>_TOKEN"),
        (("403", "forbidden"),
         "нет прав (403): токен принят, но к этому репозиторию доступа нет"),
        (("ssl certificate", "certificate verify", "server certificate verification", "unable to get local issuer"),
         "сертификат сервера не проверен. Укажите корневой сертификат компании "
         "в CODE_GIT_CAINFO; проверку не отключайте"),
        (("could not resolve host", "name or service not known"),
         "хост не найден (DNS): проверьте адрес"),
        (("failed to connect", "connection refused", "timed out", "operation too slow", "connection reset"),
         "нет связи с сервером или он не отвечает"),
        (("couldn't find remote ref", "remote branch", "not found in upstream"),
         "такой ветки нет в репозитории"),
        (("repository not found", "not found", "404", "does not appear to be a git repository", "does not exist"),
         "репозиторий не найден (404): проверьте адрес. Bitbucket иногда отвечает "
         "так же, когда прав нет"),
    ]
    for needles, text in rules:
        if any(n in s for n in needles):
            return text
    return "ошибка git"


class Git:
    def __init__(self, providers: list[Provider], cainfo: str = "", verbose: bool = False):
        self.env = git_env(providers, cainfo)
        self.hidden = secrets(providers)
        self.verbose = verbose

    def run(self, args: list[str], cwd: Path | None = None, timeout: int = CLONE_TIMEOUT) -> str:
        if not args or args[0] not in ALLOWED_GIT or (
            args[0] == "remote" and (len(args) < 2 or args[1] not in ALLOWED_REMOTE)
        ):
            raise SyncError(f"git {' '.join(args[:2])}: команда не разрешена в repos/sync.py")
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=self.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise SyncError(f"git {args[0]}: не уложился в {timeout} с") from None
        err = mask(proc.stderr.strip(), self.hidden)
        if self.verbose and err:
            print("      " + err.replace("\n", "\n      "))
        if proc.returncode != 0:
            tail = "\n".join(err.splitlines()[-3:])
            raise SyncError(f"{explain(err)}\n      git: {tail}")
        return mask(proc.stdout.strip(), self.hidden)


def git_version() -> tuple[int, ...]:
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return ()
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else ()


# ------------------------------------------------------------------ действия


def state_of(repo: Repo, root: Path, git: Git) -> str:
    """new / update / чужой каталог (текст ошибки)."""
    target = root / repo.dirname
    if not target.exists():
        return "new"
    if not (target / ".git").exists():
        return f"каталог {target} есть, но это не git-репозиторий — не трогаю"
    try:
        origin = git.run(["remote", "get-url", "origin"], cwd=target, timeout=CHECK_TIMEOUT)
    except SyncError:
        return f"в {target} нет remote origin — не трогаю"
    if _norm(origin) != _norm(repo.url):
        return f"в {target} другой репозиторий ({origin}) — не трогаю"
    return "update"


def clone(repo: Repo, root: Path, git: Git) -> None:
    target = root / repo.dirname
    # Клон во временный каталог и переименование: оборванный на середине
    # клон не оставит полупустую папку, которую следующий прогон примет за
    # готовую
    tmp = root / f".{repo.dirname}.partial"
    if tmp.exists():
        shutil.rmtree(tmp)
    args = ["clone", "--depth", "1", "--no-tags"]
    if repo.branch:
        args += ["--branch", repo.branch]
    try:
        git.run([*args, repo.url, str(tmp)])
    except SyncError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tmp.rename(target)


def update(repo: Repo, root: Path, git: Git) -> bool:
    """fetch + reset --hard, а не pull: pull сливает и встаёт на конфликте
    (например, после force-push), и синхронизация остановилась бы навсегда.
    Локальных правок в этих клонах быть не должно. Возвращает, поменялось ли."""
    target = root / repo.dirname
    before = git.run(["rev-parse", "HEAD"], cwd=target, timeout=CHECK_TIMEOUT)
    git.run(["fetch", "--depth", "1", "--no-tags", "origin", repo.branch or "HEAD"], cwd=target)
    git.run(["reset", "--hard", "--quiet", "FETCH_HEAD"], cwd=target, timeout=CHECK_TIMEOUT)
    after = git.run(["rev-parse", "HEAD"], cwd=target, timeout=CHECK_TIMEOUT)
    return before != after


def check_remote(repo: Repo, git: Git) -> str:
    """Доступ без скачивания: ls-remote. Возвращает коммит ветки."""
    ref = f"refs/heads/{repo.branch}" if repo.branch else "HEAD"
    out = git.run(["ls-remote", repo.url, ref], timeout=CHECK_TIMEOUT)
    if not out:
        if repo.branch:
            raise SyncError(f"доступ есть, но ветки {repo.branch} нет")
        raise SyncError("доступ есть, но репозиторий пустой")
    return out.split()[0][:10]


# ---------------------------------------------------------------------- main


def describe(repo: Repo) -> str:
    who = repo.provider.name if repo.provider else "без токена"
    branch = repo.branch or "основная ветка"
    return f"{repo.dirname}  [{who}, {branch}]"


def settings_report(providers: list[Provider], root: Path, cainfo: str) -> int:
    """Печатает настройки (без секретов). Возвращает число проблем."""
    problems = 0
    ver = git_version()
    if not ver:
        print("  [!] git не найден в PATH")
        return 1
    print(f"  git {'.'.join(map(str, ver))}", end="")
    if ver < MIN_GIT:
        print(f"  [!] нужен {'.'.join(map(str, MIN_GIT))} или новее")
        problems += 1
    else:
        print()

    print(f"  CODE_DIR = {root}", end="")
    if not root.is_dir():
        print("  [!] каталога нет")
        problems += 1
    elif not os.access(root, os.W_OK):
        print("  [!] нет прав на запись")
        problems += 1
    else:
        print()

    if cainfo:
        ok = Path(cainfo).is_file()
        print(f"  CODE_GIT_CAINFO = {cainfo}" + ("" if ok else "  [!] файла нет"))
        problems += 0 if ok else 1

    if not providers:
        print("  [!] не задан ни один сервис (BITBUCKET_URL, GITFLIC_URL)")
    for p in providers:
        mode = f"Basic, логин {p.user}" if p.user else "Bearer, без логина"
        tok = f"токен задан ({len(p.token)} симв.)" if p.token else "[!] токен пустой"
        print(f"  {p.name:10} {p.url}  {mode}, {tok}")
        if not p.token:
            problems += 1
        elif p.token != p.token.strip():
            print("             [!] в токене пробелы по краям — скорее всего лишние")
            problems += 1
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Клонирование репозиториев по токену")
    ap.add_argument("--check", action="store_true", help="проверить доступ, ничего не скачивая")
    ap.add_argument("--dry-run", action="store_true", help="показать, что будет сделано")
    ap.add_argument("--only", metavar="ТЕКСТ", help="только репозитории с этим текстом в имени")
    ap.add_argument("-v", "--verbose", action="store_true", help="печатать вывод git целиком")
    args = ap.parse_args(argv)

    load_env()
    providers = load_providers()
    root = Path(os.environ.get("CODE_DIR", "")).expanduser()
    cainfo = os.environ.get("CODE_GIT_CAINFO", "").strip()

    print("Настройки:")
    problems = settings_report(providers, root, cainfo)

    try:
        repos = parse_list(os.environ.get("CODE_GIT_REPOS", ""), providers)
    except SyncError as e:
        print(f"\n[!] CODE_GIT_REPOS: {e}")
        return 1
    if args.only:
        repos = [r for r in repos if args.only.lower() in r.dirname.lower()]
    if not repos:
        print("\nРепозиториев нет: заполните CODE_GIT_REPOS в .env" +
              (f" (или --only {args.only} ничего не выбрал)" if args.only else ""))
        return 1
    for r in repos:
        if r.provider is None:
            print(f"  [!] {r.url}: хост не совпал ни с одним <СЕРВИС>_URL — пойду без токена")

    if problems and not args.check:
        print(f"\nПроблем в настройках: {problems}. Сначала исправьте их (подробнее: --check).")
        return 1

    git = Git(providers, cainfo, args.verbose)
    failed = 0

    if args.check:
        print(f"\nДоступ ({len(repos)}):")
        for r in repos:
            try:
                commit = check_remote(r, git)
                print(f"  [ok] {describe(r)}  {commit}")
            except SyncError as e:
                failed += 1
                print(f"  [!!] {describe(r)}\n       {r.url}\n       {e}")
        print(f"\nИтог: доступно {len(repos) - failed} из {len(repos)}" +
              (f", проблем в настройках: {problems}" if problems else ""))
        return failed + problems

    if not args.dry_run:
        root.mkdir(parents=True, exist_ok=True)

    stats = {"склонировано": 0, "обновлено": 0, "без изменений": 0, "ошибок": 0}
    print(f"\nРепозитории ({len(repos)}) -> {root}")
    for r in repos:
        state = state_of(r, root, git)
        if state not in ("new", "update"):
            stats["ошибок"] += 1
            print(f"  [!!] {describe(r)}\n       {state}")
            continue
        if args.dry_run:
            print(f"  [{'клон' if state == 'new' else 'обновить'}] {describe(r)}")
            continue
        try:
            if state == "new":
                clone(r, root, git)
                stats["склонировано"] += 1
                print(f"  [клон]     {describe(r)}")
            elif update(r, root, git):
                stats["обновлено"] += 1
                print(f"  [обновлён] {describe(r)}")
            else:
                stats["без изменений"] += 1
                print(f"  [=]        {describe(r)}")
        except SyncError as e:
            stats["ошибок"] += 1
            print(f"  [!!] {describe(r)}\n       {r.url}\n       {e}")

    if args.dry_run:
        print("\nПробный прогон, ничего не скачано." +
              (f" Каталогов с проблемами: {stats['ошибок']}" if stats["ошибок"] else ""))
        return stats["ошибок"]

    print("\nИтог:")
    for name, value in stats.items():
        print(f"    {name:14} {value}")
    return stats["ошибок"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
