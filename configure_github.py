from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import webbrowser
from contextlib import closing
from dataclasses import asdict, dataclass, field
from html import escape
from http.client import HTTPConnection, HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from stat import S_IMODE
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from providers import ProviderError, request_json
from repository import GITHUB_HEADERS

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8000
GITHUB_CREATE = "https://github.com/settings/apps/new"
COOKIE = "bridge_registration"


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class LocalConfig:
    slug: str
    client_id: str
    client_secret: str = field(repr=False)
    owner_id: int
    port: int

    def __post_init__(self) -> None:
        fields = ((self.slug, r"[a-z0-9-]{1,100}"), (self.client_id, r"[A-Za-z0-9._-]{1,128}"),
                  (self.client_secret, r"[A-Za-z0-9_-]{1,512}"))
        if (not all(isinstance(value, str) and re.fullmatch(pattern, value) for value, pattern in fields)
                or type(self.owner_id) is not int or self.owner_id < 1
                or type(self.port) is not int or not 1 <= self.port <= 65_535):
            raise SetupError("Настройки GitHub App неполные или некорректные.")

    @property
    def origin(self) -> str:
        return f"http://{HOST}:{self.port}"

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("BRIDGE_MODE", None)
        env.update(BRIDGE_REQUIRE_GITHUB_LOGIN="1", BRIDGE_PUBLIC_ORIGIN=self.origin,
                   BRIDGE_GITHUB_APP_SLUG=self.slug, BRIDGE_GITHUB_CLIENT_ID=self.client_id,
                   BRIDGE_GITHUB_CLIENT_SECRET=self.client_secret, BRIDGE_GITHUB_OWNER_ID=str(self.owner_id))
        return env


def config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    else:
        base = Path.home() / ".config"
    return base / "AntiG-Devin-Bridge" / "github.json"


def check_private(path: Path) -> None:
    if path.is_symlink() or (os.name != "nt" and S_IMODE(path.stat().st_mode) & 0o077):
        raise SetupError("Файл или папка настроек не приватны. Ограничь доступ своим аккаунтом.")


def load_config(path: Path) -> LocalConfig | None:
    if not path.exists() and not path.is_symlink():
        return None
    check_private(path.parent)
    check_private(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LocalConfig(**data)
    except (ValueError, TypeError):
        raise SetupError("Не удалось прочитать локальные настройки GitHub App.") from None


def save_config(path: Path, config: LocalConfig) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SetupError("Файл настроек уже существует. Он не был перезаписан.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(asdict(config), file)


def manifest(origin: str) -> dict[str, Any]:
    return {
        "name": f"AntiG Devin Bridge {secrets.token_hex(4)}",
        "url": origin,
        "redirect_url": origin + "/register/callback",
        "callback_urls": [origin + "/api/github/callback"],
        "setup_url": origin + "/api/github/setup",
        "setup_on_update": True,
        "public": False,
        "hook_attributes": {"active": False},
        "request_oauth_on_install": False,
        "default_permissions": {"contents": "write", "pull_requests": "read"},
        "default_events": [],
    }


def exchange(code: str, port: int) -> LocalConfig:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", code):
        raise SetupError("Некорректный код регистрации GitHub.")
    data = request_json(f"https://api.github.com/app-manifests/{code}/conversions", "", {},
                        headers=GITHUB_HEADERS, limit=64_000)
    owner = data.get("owner")
    if not isinstance(owner, dict) or owner.get("type") != "User":
        raise SetupError("Установщик поддерживает только личный GitHub App.")
    return LocalConfig(data.get("slug", ""), data.get("client_id", ""), data.get("client_secret", ""),
                       owner.get("id", 0), port)


def open_browser(url: str) -> None:
    print(f"Открой в браузере: {url}", flush=True)
    webbrowser.open(url)


def register(port: int, path: Path) -> LocalConfig:
    host = f"{HOST}:{port}"
    origin = f"http://{host}"
    state, browser_key = (secrets.token_urlsafe(32) for _ in range(2))
    action = GITHUB_CREATE + "?" + urlencode({"state": state})
    encoded = escape(json.dumps(manifest(origin)), quote=True)
    finished, exchange_lock = Event(), Lock()
    result: LocalConfig | None = None
    failed = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def page(self, status: int, body: str, cookie: str = "") -> None:
            page = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>Подключение GitHub · Мост</title><main><h1>Подключение GitHub</h1>'
                    + body + '</main></html>').encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; form-action 'self' https://github.com; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(page)

        def do_GET(self) -> None:
            nonlocal result, failed
            if self.headers.get("Host") != host:
                self.page(400, "<p>Используй адрес из терминала.</p>")
                return
            parts = urlsplit(self.path)
            if parts.path == "/":
                self.page(200, '<p>Создай личный GitHub App. Он нужен для чтения кода, push в новую ветку '
                          'и просмотра PR. Доступ к репозиториям ты выберешь отдельно.</p>'
                          f'<form action="{escape(action)}" method="post"><input type="hidden" name="manifest" '
                          f'value="{encoded}"><button>Создать GitHub App на GitHub</button></form>',
                          f"{COOKIE}={browser_key}; Path=/; Max-Age=900; HttpOnly; SameSite=Lax")
                return
            if parts.path != "/register/callback":
                self.page(404, "<p>Страница не найдена.</p>")
                return
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                actual_cookie = cookies[COOKIE].value if COOKIE in cookies else ""
            except CookieError:
                actual_cookie = ""
            query = parse_qs(parts.query)
            codes, states = query.get("code", []), query.get("state", [])
            if (len(codes) != 1 or len(states) != 1 or not secrets.compare_digest(state.encode(), states[0].encode())
                    or not secrets.compare_digest(browser_key.encode(), actual_cookie.encode())):
                self.page(400, '<p>Регистрация не подтверждена. <a href="/">Начни заново</a>.</p>')
                return
            with exchange_lock:
                if finished.is_set():
                    self.page(409, "<p>Регистрация уже обработана.</p>")
                    return
                try:
                    result = exchange(codes[0], port)
                    save_config(path, result)
                    self.page(200, "<p>Настройки сохранены. Открываю «Мост» в новой вкладке.</p>")
                except (SetupError, ProviderError, OSError):
                    failed = True
                    self.page(502, "<p>Не удалось завершить настройку. Если App появился в GitHub, проверь его "
                              "в настройках GitHub прежде, чем создавать ещё один.</p>")
                finally:
                    finished.set()

    try:
        server = ThreadingHTTPServer((HOST, port), Handler)
    except OSError:
        raise SetupError(f"Порт {port} занят или недоступен. Останови демо или выбери другой порт через --port.") from None
    with server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            open_browser(origin)
            complete = finished.wait(900)
        finally:
            server.shutdown()
            thread.join()
    if not complete:
        raise SetupError("Время ожидания регистрации вышло. Запусти установщик ещё раз.")
    if failed or result is None:
        raise SetupError("GitHub App не удалось настроить. Проверь страницу регистрации в браузере.")
    return result


def prepare_python() -> Path:
    venv = ROOT / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                    "-r", str(ROOT / "requirements.txt")], check=True)
    return python


def bridge_ready(port: int) -> bool:
    try:
        with closing(HTTPConnection(HOST, port, timeout=.4)) as connection:
            connection.request("GET", "/api/github/status")
            response = connection.getresponse()
            if response.status == 200:
                data = json.loads(response.read(1024))
                return isinstance(data, dict) and data.get("enabled") is True
    except (OSError, HTTPException, ValueError):
        pass
    return False


def serve(config: LocalConfig, python: Path) -> int:
    args = [str(python), "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(config.port), "--no-access-log"]
    with subprocess.Popen(args, cwd=ROOT, env=config.environment()) as process:
        deadline = monotonic() + 20
        pause = Event()
        try:
            while process.poll() is None and monotonic() < deadline:
                if bridge_ready(config.port):
                    open_browser(config.origin + "/workspace")
                    return process.wait()
                pause.wait(.15)
            raise SetupError("«Мост» не запустился. Проверь вывод сервера и свободен ли порт.")
        finally:
            if process.poll() is None:
                process.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description="Настроить GitHub App и запустить «Мост» локально.")
    parser.add_argument("--port", type=int, help=f"порт для первой настройки (по умолчанию {DEFAULT_PORT})")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65_535:
        parser.error("порт должен быть от 1 до 65535")
    if sys.version_info < (3, 12):
        parser.error("нужен Python 3.12+")
    try:
        path = config_path()
        config = load_config(path)
        if config is not None and args.port not in (None, config.port):
            raise SetupError(f"Этот GitHub App настроен на порт {config.port}. Запусти без --port.")
        python = prepare_python()
        if config is None:
            config = register(args.port or DEFAULT_PORT, path)
        return serve(config, python)
    except KeyboardInterrupt:
        return 0
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, subprocess.CalledProcessError):
        print("Не удалось подготовить Python или запустить сервер. Проверь вывод терминала.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
