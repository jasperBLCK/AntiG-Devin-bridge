from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from providers import ProviderError, request_json
from repository import GITHUB_HEADERS, GitHub, valid_repo

SESSION = "bridge_github"
OAUTH = "bridge_github_oauth"
API = "https://api.github.com"
DESTINATIONS = {"overview": "/", "workspace": "/workspace", "settings": "/settings", "task": "/tasks/new"}


@dataclass(frozen=True)
class GitHubApp:
    slug: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    origin: str = ""

    @property
    def configured(self) -> bool:
        try:
            origin = urlsplit(self.origin)
            return bool(
                self.client_id and self.client_secret and re.fullmatch(r"[a-z0-9-]{1,100}", self.slug)
                and origin.netloc and origin.hostname and not origin.username and not origin.password
                and not (origin.path or origin.query or origin.fragment)
                and (origin.scheme == "https"
                     or (origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1"}))
            )
        except ValueError:
            return False

    @property
    def callback(self) -> str:
        return self.origin + "/api/github/callback"


@dataclass(frozen=True)
class Pending:
    state: str
    verifier: str = field(repr=False)
    destination: str
    expires: float


@dataclass
class Connection:
    token: str = field(repr=False)
    login: str
    expires: float
    repos: dict[str, str] = field(default_factory=dict)
    checked_until: float = 0
    partial: bool = False


class GitHubConnect:
    def __init__(self, config: GitHubApp, enabled: bool) -> None:
        self.config = config
        self.enabled = enabled and config.configured
        self.secure = config.origin.startswith("https://")
        self.pending: dict[str, Pending] = {}
        self.sessions: dict[str, Connection] = {}
        self.lock = Lock()

    def connection(self, request: Request) -> Connection | None:
        with self.lock:
            self._prune()
            return self.sessions.get(request.cookies.get(SESSION, "")) if self.enabled else None

    def _prune(self) -> None:
        now = monotonic()
        for key in [key for key, session in self.sessions.items() if session.expires <= now]:
            self.sessions.pop(key)
        for key in [key for key, flow in self.pending.items() if flow.expires <= now]:
            self.pending.pop(key)

    def _cookie(self, response: Response, name: str, value: str, ttl: int) -> None:
        response.set_cookie(name, value, max_age=ttl, httponly=True, secure=self.secure, samesite="lax")

    @staticmethod
    def _api(endpoint: str, token: str) -> dict[str, Any]:
        return request_json(API + endpoint, token, headers=GITHUB_HEADERS)

    def repositories(self, session: Connection, *, reload: bool = False) -> tuple[dict[str, str], bool]:
        with self.lock:
            if not reload and monotonic() < session.checked_until:
                return session.repos, session.partial
        repos: dict[str, str] = {}
        partial = False
        requests_left = 30
        installations = "/user/installations?per_page=100&page="
        for page in range(1, 4):
            batch = self._api(installations + str(page), session.token).get("installations")
            if not isinstance(batch, list):
                raise ProviderError("GitHub не вернул список установок.")
            for item in batch:
                install = item.get("id") if isinstance(item, dict) else None
                if type(install) is not int or install < 1 or item.get("suspended_at"):
                    continue
                for repo_page in range(1, 6):
                    if not requests_left:
                        partial = True
                        break
                    requests_left -= 1
                    result = self._api(
                        f"/user/installations/{install}/repositories?per_page=100&page={repo_page}", session.token,
                    ).get("repositories")
                    if not isinstance(result, list):
                        raise ProviderError("GitHub не вернул список репозиториев.")
                    for repo in result:
                        name = repo.get("full_name") if isinstance(repo, dict) else None
                        if isinstance(name, str) and valid_repo(name):
                            repos[name.lower()] = name
                    if len(result) < 100:
                        break
                else:
                    partial = True
            if len(batch) < 100:
                break
        else:
            partial = True
        with self.lock:
            session.repos, session.checked_until, session.partial = repos, monotonic() + 45, partial
        return repos, partial

    def client(self, request: Request, repo: str) -> GitHub:
        session = self.connection(request)
        if not session:
            raise HTTPException(401, "Сначала подключи GitHub.")
        try:
            repos, _ = self.repositories(session)
        except ProviderError:
            raise HTTPException(502, "Не удалось проверить доступ. Обнови список или подключи GitHub заново.") from None
        if repo.lower() not in repos:
            raise HTTPException(403, "Установи GitHub App и разреши доступ к этому репозиторию.")
        return GitHub(session.token)

    def routes(self) -> APIRouter:
        router = APIRouter(prefix="/api/github")

        @router.get("/status")
        def status(request: Request) -> dict[str, Any]:
            session = self.connection(request)
            return {"enabled": self.enabled, "login": session.login if session else None,
                    "install": f"https://github.com/apps/{self.config.slug}/installations/new" if session else None}

        @router.post("/start")
        def start(request: Request, target: Literal["overview", "workspace", "settings", "task"]) -> JSONResponse:
            if not self.enabled:
                raise HTTPException(503, "GitHub App ещё не настроен на сервере.")
            ticket, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            with self.lock:
                self._prune()
                self.pending.pop(request.cookies.get(OAUTH, ""), None)
                if len(self.pending) >= 128:
                    raise HTTPException(429, "Слишком много попыток. Попробуй позже.")
                self.pending[ticket] = Pending(state, verifier, DESTINATIONS[target], monotonic() + 600)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            query = urlencode({
                "client_id": self.config.client_id, "redirect_uri": self.config.callback, "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256",
            })
            response = JSONResponse({"url": "https://github.com/login/oauth/authorize?" + query})
            self._cookie(response, OAUTH, ticket, 600)
            return response

        @router.get("/callback")
        def callback(request: Request) -> RedirectResponse:
            state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
            with self.lock:
                self._prune()
                ticket = request.cookies.get(OAUTH, "")
                flow = self.pending.get(ticket) if self.enabled else None
                if not flow or not secrets.compare_digest(state.encode(), flow.state.encode()):
                    raise HTTPException(400, "Ссылка на подключение устарела. Начни с кнопки GitHub в «Мосте».")
                self.pending.pop(ticket)
            response = RedirectResponse(flow.destination + "?github=cancelled", status_code=303)
            self._cookie(response, OAUTH, "", 0)
            if not code or len(code) > 256 or "error" in request.query_params:
                return response
            try:
                data = request_json("https://github.com/login/oauth/access_token", "", {
                    "client_id": self.config.client_id, "client_secret": self.config.client_secret,
                    "code": code, "code_verifier": flow.verifier, "redirect_uri": self.config.callback,
                }, form=True)
                token, ttl = data.get("access_token"), data.get("expires_in", 28800)
                if not isinstance(token, str) or not token.startswith("ghu_") or type(ttl) is not int or ttl < 1:
                    raise ProviderError("GitHub не выдал пользовательский токен.")
                login = self._api("/user", token).get("login")
                if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", login):
                    raise ProviderError("GitHub не вернул имя пользователя.")
            except ProviderError:
                response.headers["Location"] = flow.destination + "?github=failed"
                return response
            ttl = min(ttl, 28800)
            session_id = secrets.token_urlsafe(32)
            with self.lock:
                self.sessions.pop(request.cookies.get(SESSION, ""), None)
                if len(self.sessions) >= 128:
                    self.sessions.pop(next(iter(self.sessions)))
                self.sessions[session_id] = Connection(token, login, monotonic() + ttl)
            response.headers["Location"] = flow.destination + "?github=connected"
            self._cookie(response, SESSION, session_id, ttl)
            return response

        @router.get("/setup")
        def setup(request: Request) -> RedirectResponse:
            # GitHub's installation_id is not proof of access. Only the authenticated repository list is used.
            if (session := self.connection(request)) is not None:
                with self.lock:
                    session.checked_until = 0
            return RedirectResponse("/workspace?github=setup", status_code=303)

        @router.get("/repos")
        def repos(request: Request, reload: bool = False) -> dict[str, Any]:
            session = self.connection(request)
            if not session:
                raise HTTPException(401, "Сначала подключи GitHub.")
            try:
                allowed, partial = self.repositories(session, reload=reload)
            except ProviderError:
                raise HTTPException(502, "Не удалось загрузить репозитории. При необходимости подключи GitHub заново.") from None
            return {"repos": sorted(allowed.values(), key=str.lower), "partial": partial}

        @router.post("/disconnect")
        def disconnect(request: Request) -> JSONResponse:
            with self.lock:
                self.sessions.pop(request.cookies.get(SESSION, ""), None)
                self.pending.pop(request.cookies.get(OAUTH, ""), None)
            response = JSONResponse({"ok": True})
            self._cookie(response, SESSION, "", 0)
            self._cookie(response, OAUTH, "", 0)
            return response

        return router
