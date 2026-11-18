from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import time
from typing import Annotated, Any
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from github_connect import (
    DESTINATIONS,
    Destination,
    GitHubApp,
    GitHubConnect,
    login_url,
)
from orchestration import (
    DEVIN_ROUTES,
    MEDIA_MODELS,
    REVIEWER,
    ROUTER,
    MediaKind,
    demo_decision,
    explicit_media,
    handoff,
    parse_decision,
    with_skills,
)
from providers import (
    GROK_MODEL,
    AntiG,
    ChatUsage,
    Devin,
    GrokProvider,
    ProviderError,
    image_type,
    media_download,
)
from repository import (
    DEMO_FILES,
    MAX_CONTEXT_CHARS,
    MAX_FILES,
    MAX_WORKSPACE_BYTES,
    GitHub,
    editable_path,
    shareable_path,
    valid_repo,
)
from skills_market import MAX_TASK_SKILLS, SkillID, Skills
from workspace import Revision, Workspace

ROOT = Path(__file__).resolve().parent
DEFAULT_ACU = 5
MAX_ACU = 100
MAX_ASSET_BYTES = 20 * 1024 * 1024
DEFAULT_MEDIA_HOSTS = ("vidgen.x.ai", "imgen.x.ai", "assets.grok.com")
PUBLIC_LOGIN_ROUTES = frozenset({
    "/login", "/api/github/status", "/api/github/start", "/api/github/callback", "/api/github/setup",
    "/static/login.js", "/static/login.css", "/static/icon.svg",
})
DEMO_COOKIE = "bridge_demo_entry"
LOGIN_TARGETS = {path: name for name, path in DESTINATIONS.items()}
logger = logging.getLogger(__name__)
PUBLIC_COLUMNS = (
    "id", "mode", "route", "prompt", "model", "repo", "ref", "commit_sha", "created_at", "status",
    "session_url", "pr_url", "progress", "routed_at", "finished_at", "reviewed_at",
)
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]
Repo = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)]
JobID = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Commit = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
FileStamp = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
GitRef = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_./-]{1,200}$")]
SourcePath = Annotated[str, StringConstraints(min_length=1, max_length=320)]


@dataclass(frozen=True)
class Settings:
    db: Path
    demo: bool = False
    antig_key: str = ""
    devin_key: str = ""
    devin_org: str = ""
    github_read_token: str = ""
    public_skills: bool = False
    xai_key: str = ""
    grok_provider: GrokProvider = "antig"
    admin_key: str = ""
    github_write_token: str = ""
    github_write_repos: tuple[str, ...] = ()
    media_hosts: tuple[str, ...] = DEFAULT_MEDIA_HOSTS
    github_app: GitHubApp = field(default_factory=GitHubApp)
    github_login_required: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        demo = os.getenv("BRIDGE_MODE") == "demo"
        admin_key = os.getenv("BRIDGE_ADMIN_KEY", "")
        owner_id = os.getenv("BRIDGE_GITHUB_OWNER_ID", "").strip()
        return cls(
            db=Path(os.getenv("BRIDGE_DB", str(ROOT / "data" / ("demo.sqlite3" if demo else "bridge.sqlite3")))),
            demo=demo,
            antig_key=os.getenv("ANTIG_API_KEY", "").strip(),
            devin_key=os.getenv("DEVIN_API_KEY", "").strip(),
            devin_org=os.getenv("DEVIN_ORG_ID", "").strip(),
            github_read_token=os.getenv("GITHUB_READ_TOKEN", "").strip(),
            public_skills=os.getenv("BRIDGE_PUBLIC_SKILLS") == "1",
            xai_key=os.getenv("XAI_API_KEY", "").strip(),
            grok_provider="xai" if os.getenv("BRIDGE_GROK_PROVIDER") == "xai" else "antig",
            admin_key=admin_key,
            github_write_token=os.getenv("GITHUB_WRITE_TOKEN", "").strip(),
            github_write_repos=tuple(repo.strip().lower() for repo in os.getenv("BRIDGE_GITHUB_WRITE_REPOS", "").split(",")
                                     if valid_repo(repo.strip())),
            media_hosts=DEFAULT_MEDIA_HOSTS + tuple(host.strip().lower() for host in os.getenv("BRIDGE_MEDIA_HOSTS", "").split(",")
                                                   if re.fullmatch(r"[a-zA-Z0-9.-]+", host.strip())),
            github_app=GitHubApp(os.getenv("BRIDGE_GITHUB_APP_SLUG", "").strip(),
                                 os.getenv("BRIDGE_GITHUB_CLIENT_ID", "").strip(),
                                 os.getenv("BRIDGE_GITHUB_CLIENT_SECRET", "").strip(),
                                 os.getenv("BRIDGE_PUBLIC_ORIGIN", "").strip().rstrip("/"),
                                 int(owner_id) if re.fullmatch(r"[1-9][0-9]{0,19}", owner_id) else 0),
            github_login_required=os.getenv("BRIDGE_REQUIRE_GITHUB_LOGIN") != "0" or not (demo or admin_key),
        )


class RepoBrowse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: Repo
    ref: GitRef | None = None


class RepoPreview(RepoBrowse):
    commit: Commit
    paths: list[SourcePath] = Field(min_length=1, max_length=MAX_FILES)


class WorkspaceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: SourcePath
    blob: Commit
    stamp: FileStamp
    text: str = Field(max_length=MAX_WORKSPACE_BYTES)


class WorkspacePublish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: Commit
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    edits: list[WorkspaceEdit] = Field(min_length=1, max_length=12)


class ContextFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: Commit
    paths: list[SourcePath] = Field(min_length=1, max_length=MAX_FILES)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: Prompt
    repo: Repo | None = None
    ref: GitRef | None = None
    context: ContextFiles | None = None
    max_acu: int = Field(default=DEFAULT_ACU, ge=1, le=MAX_ACU)
    draft_pr: bool = False
    review: bool = False
    skill_ids: list[SkillID] = Field(default_factory=list, max_length=MAX_TASK_SKILLS)
    grok_provider: GrokProvider | None = None


class KeySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: GrokProvider
    key: SecretStr | None = None
    reset: bool = False


class DevinSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org: str = ""
    key: SecretStr | None = None
    reset: bool = False


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, prompt TEXT NOT NULL,
                    model TEXT, repo TEXT, ref TEXT, commit_sha TEXT, max_acu INTEGER, source_id TEXT,
                    created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sending',
                    result TEXT, media_url TEXT, remote_id TEXT, session_url TEXT,
                    pr_url TEXT, progress INTEGER, error TEXT
                );
                CREATE TABLE IF NOT EXISTS grok_usage (
                    job_id TEXT NOT NULL, phase TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                    PRIMARY KEY (job_id, phase)
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            additions = {
                "ref": "TEXT", "commit_sha": "TEXT", "route": "TEXT", "reason": "TEXT", "plan": "TEXT",
                "routed_at": "TEXT", "finished_at": "TEXT", "reviewed_at": "TEXT",
                "activity": "TEXT NOT NULL DEFAULT '{}'", "last_polled": "REAL NOT NULL DEFAULT 0",
                "review_allowed": "INTEGER NOT NULL DEFAULT 0",
                "skills": "TEXT NOT NULL DEFAULT '[]'",
                "devin_acu": "REAL",
                "grok_provider": "TEXT NOT NULL DEFAULT 'antig'",
                "hidden": "INTEGER NOT NULL DEFAULT 0",
                "pipeline": "TEXT NOT NULL DEFAULT '{}'",
                "devin_org": "TEXT",
            }
            for column, schema in additions.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {schema}")
            conn.execute("""
                UPDATE jobs SET status = 'failed', error = ?
                WHERE status = 'sending'
            """, ("Сервер перезапустился до получения ответа. Проверь сервис перед повтором.",))
            conn.execute("""
                UPDATE jobs SET status = 'ended', error = ? WHERE status = 'reviewing'
            """, ("Сервер перезапустился до окончания разбора Grok. Отчёт Devin сохранён.",))
            conn.execute("""
                CREATE INDEX IF NOT EXISTS jobs_poll ON jobs(status, last_polled) WHERE remote_id IS NOT NULL
            """)

    @staticmethod
    def decode(row: sqlite3.Row) -> dict[str, Any]:
        return {**dict(row), **{key: json.loads(row[key]) for key in ("activity", "skills", "pipeline")}}

    def get(self, job_id: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or row["hidden"]:
            raise HTTPException(404, "Задача не найдена.")
        return self.decode(row)

    def recent(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            columns = ", ".join(PUBLIC_COLUMNS)
            rows = conn.execute(f"SELECT {columns} FROM jobs WHERE NOT hidden ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    def due(self, before: float) -> list[str]:
        with closing(self.connect()) as conn:
            rows = conn.execute("""
                SELECT id FROM jobs WHERE (remote_id IS NOT NULL OR pipeline <> '{}') AND status IN ('running', 'paused')
                AND last_polled <= CASE WHEN status = 'paused' THEN ? - 30 ELSE ? END
                ORDER BY last_polled LIMIT 32
            """, (before, before)).fetchall()
        return [row["id"] for row in rows]

    def record_grok(self, job_id: str, phase: str, usage: ChatUsage) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("""
                INSERT OR IGNORE INTO grok_usage (job_id, phase, model, input_tokens, output_tokens, total_tokens)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (job_id, phase, GROK_MODEL, usage.input_tokens, usage.output_tokens, usage.total_tokens))

    def overview(self, demo: bool) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT status FROM jobs WHERE NOT hidden ORDER BY created_at DESC LIMIT 100").fetchall()
            status_counts = {key: sum(row["status"] in statuses for row in rows) for key, statuses in (
                ("active", {"sending", "running", "reviewing"}), ("waiting", {"awaiting", "paused"}),
                ("finished", {"done", "ended"}),
            )}
            grok = conn.execute("""
                SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens, COUNT(*) AS responses,
                       COUNT(total_tokens) AS measured
                FROM grok_usage WHERE model = ? AND NOT ?
            """, (GROK_MODEL, demo)).fetchone()
            devin = conn.execute("""
                SELECT SUM(devin_acu) AS acu, COUNT(devin_acu) AS measured, COUNT(*) AS sessions
                FROM jobs WHERE session_url IS NOT NULL AND session_url != '' AND NOT ?
            """, (demo,)).fetchone()
        return {
            "demo": demo, "jobs": {"total": len(rows), **status_counts},
            "grok": {"model": GROK_MODEL, **dict(grok)}, "devin": dict(devin),
            "limits": {"default_acu": DEFAULT_ACU, "max_acu": MAX_ACU,
                       "context_files": MAX_FILES, "context_chars": MAX_CONTEXT_CHARS},
        }

    def clear(self) -> list[str]:
        # Rows stay as empty shells so measured Devin ACU and Grok tokens remain in the overview.
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = [row["id"] for row in conn.execute(
                "SELECT id FROM jobs WHERE NOT hidden AND mode <> 'workspace' "
                "AND status IN ('done', 'ended', 'failed', 'awaiting')")]
            conn.execute("""
                UPDATE jobs SET hidden = 1, prompt = '', result = NULL, plan = NULL, reason = NULL, error = NULL,
                                media_url = NULL, activity = '{}', skills = '[]', pipeline = '{}', pr_url = NULL
                WHERE NOT hidden AND mode <> 'workspace' AND status IN ('done', 'ended', 'failed', 'awaiting')
            """)
        return ids

    def insert(self, task: Submission, skills: list[dict[str, str]], provider: GrokProvider) -> dict[str, Any]:
        job_id = uuid4().hex
        with closing(self.connect()) as conn, conn:
            conn.execute("""
                INSERT INTO jobs (id, mode, prompt, model, repo, ref, commit_sha, max_acu, created_at, review_allowed,
                                  skills, grok_provider)
                VALUES (?, 'auto', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (job_id, task.prompt, GROK_MODEL, task.repo, task.ref, task.context.commit if task.context else None,
                  task.max_acu, iso_now(), task.review, json.dumps(skills, ensure_ascii=False), provider))
        return self.get(job_id)

    def open_workspace(self, repo: str, ref: str, commit: str) -> str:
        job_id = uuid4().hex
        now = iso_now()
        with closing(self.connect()) as conn, conn:
            conn.execute("""
                INSERT INTO jobs (id, mode, prompt, status, repo, ref, commit_sha, created_at, finished_at)
                VALUES (?, 'workspace', 'Рабочая область GitHub', 'done', ?, ?, ?, ?, ?)
            """, (job_id, repo, ref, commit, now, now))
        return job_id

    def apply(self, job_id: str, update: dict[str, Any], previous_status: str) -> bool:
        keys = {
            "status", "result", "media_url", "remote_id", "session_url", "pr_url", "progress", "error",
            "model", "route", "reason", "plan", "activity", "routed_at", "finished_at", "reviewed_at", "last_polled",
            "devin_acu", "pipeline", "devin_org",
        }
        columns = [key for key in update if key in keys]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        args = [json.dumps(update[key], ensure_ascii=False) if key in {"activity", "pipeline"} else update[key]
                for key in columns]
        with closing(self.connect()) as conn, conn:
            result = conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ? AND status = ?",
                                  (*args, job_id, previous_status))
            return bool(result.rowcount)


def public_job(job: dict[str, Any], *, full: bool = True) -> dict[str, Any]:
    result = {key: job[key] for key in PUBLIC_COLUMNS}
    if full:
        result.update({key: job[key] for key in ("result", "reason", "plan", "error", "media_url", "max_acu", "devin_acu")})
        result["skills"] = [{key: skill[key] for key in ("id", "title")} for skill in job["skills"]]
        result["activity"] = {key: val for key, val in job["activity"].items()
                              if key not in {"cursor", "pr_checked", "pr_url"}}
        result["assets"] = [
            {key: asset[key] for key in ("kind", "prompt", "status", "model", "media_url", "progress") if key in asset}
            for asset in job["pipeline"].get("assets", [])
        ]
    return result


def create_app(settings: Settings, *, poll_seconds: float = 8) -> FastAPI:
    store = Store(settings.db)
    skills = Skills(store.connect, public=not settings.demo or settings.public_skills)
    env_keys: dict[GrokProvider, str] = {"antig": settings.antig_key, "xai": settings.xai_key}
    runtime_keys: dict[GrokProvider, str] = {}
    env_devin = Devin(settings.devin_key, settings.devin_org) if settings.devin_key and settings.devin_org else None
    runtime_devin: Devin | None = None
    selected_provider = settings.grok_provider
    keys_lock = Lock()

    def grok_client(provider: GrokProvider) -> AntiG:
        with keys_lock:
            return AntiG(runtime_keys.get(provider, env_keys[provider]), provider)

    def devin_client(org: str | None = None) -> Devin | None:
        with keys_lock:
            client = runtime_devin or env_devin
            return client if client and (org is None or client.org == org) else None

    def require_grok(provider: GrokProvider) -> AntiG:
        client = grok_client(provider)
        if not client.key and not settings.demo:
            raise ProviderError("Для выбранного API Grok не задан ключ.")
        return client

    def grok_text(client: AntiG, job_id: str, phase: str, prompt: str, system: str) -> str:
        content, usage = client.text(prompt, system)
        store.record_grok(job_id, phase, usage)
        return content

    media_dir = settings.db.parent / "media"

    def media_file(job_id: str) -> Path | None:
        return next((path for kind in ("jpeg", "png", "webp") if (path := media_dir / f"{job_id}.{kind}").is_file()), None)

    github = GitHub(settings.github_read_token)
    git_writer = GitHub(settings.github_write_token)
    gated = settings.github_login_required
    oauth_allowed = settings.github_app.owner_id > 0 if gated else bool(settings.admin_key)
    connect = GitHubConnect(settings.github_app, oauth_allowed, gated=gated, demo=settings.demo)
    admin_enabled = connect.enabled if gated else bool(settings.admin_key)
    workspace = Workspace(github, settings.demo)

    def github_for(request: Request, repo: str) -> GitHub:
        return connect.client(request, repo) if connect.enabled else github

    polling: set[str] = set()
    poll_lock = Lock()

    def redact(text: str) -> str:
        with keys_lock:
            keys = (*env_keys.values(), *runtime_keys.values(), settings.admin_key,
                    settings.devin_key, runtime_devin.key if runtime_devin else "",
                    settings.github_read_token, settings.github_write_token,
                    settings.github_app.client_secret)
        for secret in keys:
            if secret:
                text = text.replace(secret, "[скрыто]")
        return re.sub(r"\b(?:ag_|xai-|cog_|gh[pousr]_|github_pat_)[a-zA-Z0-9_-]{16,}", "[скрыто]", text)

    def read_files(repo: str, commit: str, paths: list[str], client: GitHub) -> list[dict[str, str]]:
        if len(set(paths)) != len(paths) or not all(map(shareable_path, paths)):
            raise ProviderError("Выбери разные текстовые файлы без секретов.")
        try:
            files = [{"path": path, "text": DEMO_FILES[path] if settings.demo else client.file(repo, commit, path)}
                     for path in paths]
        except KeyError:
            raise ProviderError("Файл не найден в демо.") from None
        if sum(len(item["text"]) for item in files) > MAX_CONTEXT_CHARS:
            raise ProviderError("Слишком большой контекст. Выбери меньше файлов.")
        return files

    def start_devin(
        job: dict[str, Any], draft_pr: bool, devin: Devin | None, uploads: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        prompt = handoff(job["prompt"], job["plan"], job["repo"], job["ref"], job["commit_sha"],
                         inspect=job["route"] == "inspect", draft_pr=draft_pr, skills=job["skills"])
        if uploads:
            names = ", ".join(name for name, _ in uploads)
            prompt += (f"\n\nФайлы к исходной задаче, уже сгенерированные Grok: {names}. "
                       "Используй приложенные файлы в проекте по исходной задаче. Не генерируй их повторно.")
        if settings.demo:
            return {"status": "running", "remote_id": "demo"}
        if not devin:
            raise ProviderError("Настрой ключ и ID организации Devin в настройках «Моста».")
        result = devin.start(prompt, job["repo"], job["max_acu"], [url for _, url in uploads] if uploads else None)
        # The create response has no final report; even an ended session needs its first poll.
        if result["status"] == "ended":
            result["status"] = "running"
        return {**result, "devin_org": devin.org}

    def dispatch(job_id: str, task: Submission, recipes: list[dict[str, str]], grok: AntiG, client: GitHub) -> None:
        try:
            files = read_files(task.repo, task.context.commit, task.context.paths, client) if task.repo and task.context else []
            context = json.dumps({
                "request": redact(task.prompt), "repository": task.repo, "base_ref": task.ref,
                "commit": task.context.commit if task.context else None,
                "files": [{**file, "text": redact(file["text"])} for file in files],
            }, ensure_ascii=False)
            # An explicit media request without a repository needs no Grok call to be routed.
            local = None if task.repo else explicit_media(task.prompt)
            decision = local or (demo_decision(task.prompt, task.repo) if settings.demo else parse_decision(
                grok_text(grok, job_id, "route", context, with_skills(ROUTER, recipes, "grok"))))
            content = redact(decision.content)
            update: dict[str, Any] = {"route": decision.route, "reason": redact(decision.reason), "routed_at": iso_now()}
            if decision.route in DEVIN_ROUTES:
                if not task.repo:
                    update.update(status="done", route="clarify", reason="Для работы Devin нужен репозиторий.",
                                  result="Укажи репозиторий GitHub и повтори задачу.")
                elif decision.assets:
                    devin = devin_client()
                    if not settings.demo and not devin:
                        raise ProviderError("Настрой ключ и ID организации Devin в настройках «Моста».")
                    update.update(status="awaiting", plan=content, devin_org=devin.org if devin else None,
                                  pipeline={"draft_pr": task.draft_pr, "assets": [
                        {"kind": kind, "prompt": redact(prompt), "status": "planned", "model": MEDIA_MODELS[kind]}
                        for kind, prompt in decision.assets
                    ]})
                else:
                    store.apply(job_id, {**update, "plan": content}, "sending")
                    update.update(start_devin(store.get(job_id), task.draft_pr, devin_client()))
            elif decision.route in MEDIA_MODELS:
                update.update(status="awaiting", model=MEDIA_MODELS[decision.route])
            else:
                update.update(status="done", result=content)
        except ProviderError as exc:
            update = {"status": "failed", "error": str(exc)}
        if update.get("status") in {"done", "failed"}:
            update["finished_at"] = iso_now()
        store.apply(job_id, update, "sending")

    def generate(job: dict[str, Any]) -> None:
        try:
            route, prompt = job["route"], redact(job["prompt"])
            model = MEDIA_MODELS[route]
            update: dict[str, Any]
            if settings.demo:
                update = ({"status": "done", "media_url": "/static/sample.svg", "result": "Демо-изображение."}
                          if route == "image" else {"status": "running", "remote_id": "demo"})
            else:
                grok = require_grok(job["grok_provider"])
                update = grok.image(prompt, model) if route == "image" else grok.start_video(prompt, model)
                if "media" in update:
                    media_dir.mkdir(parents=True, exist_ok=True)
                    (media_dir / f"{job['id']}.{update['media_type']}").write_bytes(update["media"])
                    update = {"status": "done", "media_url": f"/api/jobs/{job['id']}/media"}
        except OSError:
            update = {"status": "failed", "error": "Изображение получено, но не сохранилось на сервере."}
        except ProviderError as exc:
            update = {"status": "failed", "error": str(exc)}
        if update.get("status") in {"done", "failed"}:
            update["finished_at"] = iso_now()
        store.apply(job["id"], update, "sending")

    def asset_path(job_id: str, kind: str, ext: str) -> Path:
        return media_dir / f"{job_id}-asset-{kind}.{ext}"

    def save_asset(job_id: str, asset: dict[str, Any], result: dict[str, Any]) -> None:
        kind = asset["kind"]
        raw = result["media"] if "media" in result else media_download(
            result["media_url"], settings.media_hosts, MAX_ASSET_BYTES)
        ext = image_type(raw) if kind == "image" else "mp4" if raw[4:8] == b"ftyp" else ""
        if not ext or len(raw) > MAX_ASSET_BYTES:
            raise ProviderError("Формат медиа не поддерживается или превышен лимит файла.")
        media_dir.mkdir(parents=True, exist_ok=True)
        asset_path(job_id, kind, ext).write_bytes(raw)
        asset.update(status="ready", ext=ext, progress=100, media_url=f"/api/jobs/{job_id}/assets/{kind}")

    def finish_pipeline(job: dict[str, Any]) -> None:
        pipeline, job_id = job["pipeline"], job["id"]
        uploads = []
        devin = devin_client(job["devin_org"] or settings.devin_org)
        if not settings.demo:
            if not devin:
                raise ProviderError("Подключи исходную организацию Devin в настройках. Медиа сохранены в задаче.")
            for asset in pipeline["assets"]:
                kind, ext = asset["kind"], asset["ext"]
                name = f"bridge-{kind}.{ext}"
                mime = "video/mp4" if kind == "video" else f"image/{ext}"
                url = devin.upload(name, asset_path(job_id, kind, ext).read_bytes(), mime)
                uploads.append((name, url))
        update = start_devin(job, pipeline["draft_pr"], devin, uploads)
        store.apply(job_id, {**update, "error": None}, "sending")

    def pipeline_error(job: dict[str, Any], exc: Exception) -> None:
        error = str(exc) if isinstance(exc, ProviderError) else "Не удалось сохранить или открыть медиафайл."
        store.apply(job["id"], {"pipeline": job["pipeline"], "status": "failed", "error": error,
                                "finished_at": iso_now()}, "sending")

    def generate_pipeline(job: dict[str, Any]) -> None:
        pipeline = job["pipeline"]
        try:
            if settings.demo:
                for asset in pipeline["assets"]:
                    asset["status"] = "demo"
                    if asset["kind"] == "image":
                        asset["media_url"] = "/static/sample.svg"
            else:
                if not devin_client(job["devin_org"] or settings.devin_org):
                    raise ProviderError("Перед генерацией подключи исходную организацию Devin в настройках.")
                grok = require_grok(job["grok_provider"])
                # Complete the image before starting a remote video job.
                for asset in sorted(pipeline["assets"], key=lambda item: item["kind"]):
                    if asset["kind"] == "image":
                        save_asset(job["id"], asset, grok.image(asset["prompt"], asset["model"]))
                        store.apply(job["id"], {"pipeline": pipeline}, "sending")
                    else:
                        started = grok.start_video(asset["prompt"], asset["model"])
                        asset.update(status="running", remote_id=started["remote_id"])
                        store.apply(job["id"], {"pipeline": pipeline, "status": "running"}, "sending")
                        return
            store.apply(job["id"], {"pipeline": pipeline}, "sending")
            finish_pipeline(job)
        except (OSError, ProviderError) as exc:
            pipeline_error(job, exc)

    def advance_pipeline(job: dict[str, Any]) -> None:
        pipeline, job_id = job["pipeline"], job["id"]
        asset = next((item for item in pipeline["assets"] if item["status"] == "running"), None)
        if not asset:
            return
        try:
            result = require_grok(job["grok_provider"]).video_status(asset["remote_id"])
        except ProviderError as exc:
            store.apply(job_id, {"last_polled": time(), "error": f"Статус видео не обновился: {exc}"}, "running")
            return
        if result["status"] == "running":
            asset["progress"] = result.get("progress")
            store.apply(job_id, {"pipeline": pipeline, "error": None, "last_polled": time()}, "running")
            return
        if not store.apply(job_id, {"status": "sending", "last_polled": time()}, "running"):
            return
        try:
            if result["status"] != "done":
                asset["status"] = "failed"
                raise ProviderError(result["error"])
            save_asset(job_id, asset, result)
            store.apply(job_id, {"pipeline": pipeline}, "sending")
            finish_pipeline(job)
        except (OSError, ProviderError) as exc:
            pipeline_error(job, exc)

    def review(job: dict[str, Any], report: dict[str, str]) -> None:
        try:
            # Only the opted-in structured report, not raw messages, diffs or tool logs, goes back to Grok.
            review_input = json.dumps({"request": redact(job["prompt"]), "devin_report": report}, ensure_ascii=False)
            result = ("Демо-разбор Grok: сверь проверку пустого файла с критериями задачи."
                      if settings.demo else grok_text(require_grok(job["grok_provider"]), job["id"], "review",
                                                       review_input, REVIEWER))
            update = {"status": "ended", "result": redact(result), "reviewed_at": iso_now()}
        except ProviderError as exc:
            update = {"status": "ended", "error": f"Разбор Grok недоступен: {exc}"}
        store.apply(job["id"], update, "reviewing")

    def devin_update(job: dict[str, Any], provider: Devin) -> dict[str, Any]:
        update = provider.status(job["remote_id"])
        activity = job["activity"]
        report = {key: redact(value) for key, value in update["report"].items()}
        activity.update(detail=update["detail"], report=report, warnings=[])
        try:
            items = {item["id"]: item for item in activity.get("messages", [])}
            cursor = activity.get("cursor")
            for _ in range(2):
                messages, cursor, more = provider.messages(job["remote_id"], cursor)
                items.update((item["id"], {**item, "text": redact(item["text"])}) for item in messages)
                activity.update(cursor=cursor, messages=sorted(items.values(), key=lambda item: item["at"])[-30:])
                if not more:
                    break
        except ProviderError as exc:
            activity["warnings"].append(f"Сообщения Devin временно недоступны: {exc}")
        pr_url = update.get("pr_url")
        if not connect.enabled and pr_url and job["repo"] and (
            update["status"] == "ended" or activity.get("pr_url") != pr_url or time() - activity.get("pr_checked", 0) >= 60
        ):
            activity["pr_checked"] = time()
            activity["pr_url"] = pr_url
            activity.pop("pr_error", None)
            try:
                activity["pr"] = github.pull_request(job["repo"], pr_url)
            except ProviderError as exc:
                activity.pop("pr", None)
                activity["pr_error"] = f"Ветки и файлы PR не удалось получить: {exc}"
        elif not pr_url:
            for key in ("pr", "pr_url", "pr_checked", "pr_error"):
                activity.pop(key, None)
        update["activity"] = activity
        return update

    def demo_update(job: dict[str, Any]) -> dict[str, Any]:
        if (job["route"] or job["mode"]) == "video":
            return {"status": "done", "result": "В демо ролик не генерируется. Внешний API не вызывался."}
        activity = job["activity"]
        messages = activity.setdefault("messages", [])
        steps = [
            "Открыл README.md и src/import_csv.py; проверяю, как устроен импорт.",
            "Сверил критерии задачи с tests/test_import.py.",
            "Демо завершено. Реальных изменений и запусков тестов не было.",
        ]
        if len(messages) < len(steps):
            messages.append({"id": f"demo-{len(messages)}", "at": int(time()), "text": steps[len(messages)]})
        done = len(messages) >= len(steps)
        if done:
            activity["report"] = {"summary": "Демо-прохождение сценария Devin.", "checks": "Реальные тесты не запускались."}
        return {"status": "ended" if done else "running", "activity": activity}

    def advance(job_id: str) -> None:
        with poll_lock:
            if job_id in polling:
                return
            polling.add(job_id)
        try:
            job = store.get(job_id)
            status, remote_id, route = job["status"], job["remote_id"], job["route"] or job["mode"]
            if status == "running" and not remote_id and job["pipeline"]:
                advance_pipeline(job)
                return
            if not remote_id or status not in {"running", "paused"}:
                return
            try:
                if settings.demo:
                    update = demo_update(job)
                elif route in DEVIN_ROUTES or route == "devin":
                    devin = devin_client(job["devin_org"] or settings.devin_org)
                    if not devin:
                        raise ProviderError("Подключи исходную организацию Devin в настройках.")
                    update = devin_update(job, devin)
                elif route == "video":
                    update = require_grok(job["grok_provider"]).video_status(remote_id)
                else:
                    raise ProviderError("Ключ для обновления статуса не настроен.")
            except ProviderError as exc:
                update = {"error": f"Статус не обновился: {exc}"}
            else:
                update.setdefault("error", None)
            update["last_polled"] = time()
            if update.get("status") in {"done", "ended", "failed"}:
                update["finished_at"] = iso_now()
            report = update.get("activity", {}).get("report")
            if update.get("status") == "ended" and job["review_allowed"] and report and report.get("summary"):
                update["status"] = "reviewing"
                if store.apply(job_id, update, status):
                    review(job, report)
            else:
                store.apply(job_id, update, status)
        finally:
            with poll_lock:
                polling.discard(job_id)

    async def monitor() -> None:
        pending: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                for job_id in list(pending):
                    if pending[job_id].done():
                        error = pending.pop(job_id).exception()
                        if error:
                            logger.error("Job poll failed unexpectedly: %s", type(error).__name__)
                for job_id in store.due(time() - poll_seconds):
                    if len(pending) >= 4:
                        break
                    if job_id not in pending:
                        pending[job_id] = asyncio.create_task(asyncio.to_thread(advance, job_id))
                await asyncio.sleep(poll_seconds)
        finally:
            await asyncio.gather(*pending.values(), return_exceptions=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.init()
        skills.init()
        worker = asyncio.create_task(monitor()) if poll_seconds > 0 else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    basic = HTTPBasic(auto_error=False, realm="Bridge")

    async def authenticate(request: Request) -> None:
        if gated or not settings.admin_key:
            return
        auth = await basic(request)
        if (auth is None or auth.username != "bridge"
                or not secrets.compare_digest(auth.password.encode(), settings.admin_key.encode())):
            raise HTTPException(401, "Войди в кабинет.", headers={"WWW-Authenticate": 'Basic realm="Bridge"'})

    def require_admin_enabled() -> None:
        if not admin_enabled:
            raise HTTPException(403, "Изменение доступно только в защищённом кабинете.")

    app = FastAPI(title="AntiG × Devin", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
                  dependencies=[Depends(authenticate)])
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.include_router(skills.routes(redact))
    app.include_router(connect.routes())

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path in {"/api/settings", "/api/settings/devin"}:
            return JSONResponse({"detail": "Проверь поля настроек."}, status_code=422)
        if request.url.path.endswith("/workspace/publish"):
            return JSONResponse({"detail": "Проверь поля запроса и ограничения размера."}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def protect(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if request.method == "POST" and request.headers.get("X-Bridge-Request") != "1":
            return JSONResponse({"detail": "Запрос должен быть отправлен из панели."}, status_code=403)
        public = path in PUBLIC_LOGIN_ROUTES or (settings.demo and path == "/api/demo/enter")
        if gated and not public and not cabinet_access(request):
            if path.startswith("/api/"):
                response: Response = JSONResponse({"detail": "Войди через GitHub."}, status_code=401)
            else:
                target = "workspace" if path.startswith("/workspace/") else LOGIN_TARGETS.get(path, "overview")
                response = RedirectResponse(login_url(target), status_code=303)
        else:
            response = await call_next(request)
        if path == "/api/github/callback":
            request.scope["query_string"] = b""
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https:; media-src 'self' https:; "
            "base-uri 'none'; object-src 'none'; form-action 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if gated or path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def cabinet_access(request: Request) -> bool:
        if settings.demo:
            return request.cookies.get(DEMO_COOKIE) == "1"
        return connect.connection(request) is not None

    @app.get("/login", response_model=None)
    def login(request: Request, target: Destination = "overview") -> Response:
        if gated and cabinet_access(request):
            return RedirectResponse(DESTINATIONS[target], status_code=303)
        return FileResponse(ROOT / "static" / "login.html")

    @app.post("/api/demo/enter")
    def enter_demo(request: Request, target: Destination = "overview") -> JSONResponse:
        if not settings.demo:
            raise HTTPException(404, "Демо не включено.")
        response = JSONResponse({"path": DESTINATIONS[target]})
        response.set_cookie(DEMO_COOKIE, "1", httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/tasks/new")
    def task_page() -> FileResponse:
        return FileResponse(ROOT / "static" / "new-task.html")

    @app.get("/sessions")
    def sessions_page() -> FileResponse:
        return FileResponse(ROOT / "static" / "sessions.html")

    @app.get("/settings")
    def settings_page() -> FileResponse:
        return FileResponse(ROOT / "static" / "settings.html")

    @app.get("/skills")
    def skills_page() -> FileResponse:
        return FileResponse(ROOT / "static" / "workspace.html")

    @app.get("/workspace")
    def workspace_landing() -> FileResponse:
        return FileResponse(ROOT / "static" / "workspace.html")

    @app.get("/workspace/{job_id}")
    def workspace_page(job_id: JobID) -> FileResponse:
        store.get(job_id)
        return FileResponse(ROOT / "static" / "workspace.html")

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        grok = grok_client(selected_provider)
        return {
            "demo": settings.demo,
            "antig": bool(settings.antig_key or settings.demo),
            "grok": bool(grok.key or settings.demo),
            "grok_provider": selected_provider,
            "devin": bool(devin_client() or settings.demo),
            "github_private": bool((connect.enabled or settings.github_read_token) and not settings.demo),
            "can_manage": admin_enabled,
        }

    @app.get("/api/settings")
    def key_status() -> dict[str, Any]:
        with keys_lock:
            return {
                "provider": selected_provider,
                "keys": {provider: "runtime" if provider in runtime_keys else "environment" if key else "missing"
                         for provider, key in env_keys.items()},
                "devin": "runtime" if runtime_devin else "environment" if env_devin else "missing",
                "can_manage": admin_enabled,
                "demo": settings.demo,
            }

    @app.post("/api/settings", dependencies=[Depends(require_admin_enabled)])
    def update_keys(change: KeySettings) -> dict[str, Any]:
        nonlocal selected_provider
        key = change.key.get_secret_value().strip() if change.key else ""
        if change.reset and key:
            raise HTTPException(422, "Выбери замену ключа или сброс, но не оба действия.")
        pattern = r"ag_[A-Za-z0-9_-]{16,508}" if change.provider == "antig" else r"xai-[A-Za-z0-9_-]{16,508}"
        if key and not re.fullmatch(pattern, key):
            raise HTTPException(422, "Формат ключа не подходит выбранному провайдеру.")
        with keys_lock:
            if change.reset:
                runtime_keys.pop(change.provider, None)
            elif key:
                runtime_keys[change.provider] = key
            selected_provider = change.provider
        return key_status()

    @app.post("/api/settings/devin", dependencies=[Depends(require_admin_enabled)])
    def update_devin(change: DevinSettings) -> dict[str, Any]:
        nonlocal runtime_devin
        key = change.key.get_secret_value().strip() if change.key else ""
        org = change.org.strip()
        if change.reset and (key or org):
            raise HTTPException(422, "Выбери сохранение или сброс, но не оба действия.")
        if not change.reset:
            if not re.fullmatch(r"cog_[A-Za-z0-9_-]{16,508}", key):
                raise HTTPException(422, "Укажи действующий формат ключа Devin API: cog_…")
            if not re.fullmatch(r"org-[A-Za-z0-9_-]{1,124}", org):
                raise HTTPException(422, "ID организации Devin должен начинаться с org-.")
        with keys_lock:
            runtime_devin = None if change.reset else Devin(key, org)
        return key_status()

    @app.get("/api/jobs")
    def recent() -> list[dict[str, Any]]:
        return [public_job(row, full=False) for row in store.recent()]

    @app.post("/api/jobs/clear")
    def clear_jobs() -> dict[str, int]:
        ids = store.clear()
        for job_id in ids:
            if path := media_file(job_id):
                path.unlink(missing_ok=True)
            for path in media_dir.glob(f"{job_id}-asset-*"):
                path.unlink(missing_ok=True)
        return {"cleared": len(ids)}

    @app.get("/api/jobs/{job_id}/media")
    def job_media(job_id: JobID) -> FileResponse:
        store.get(job_id)
        path = media_file(job_id)
        if not path:
            raise HTTPException(404, "Файл не найден.")
        return FileResponse(path, media_type=f"image/{path.suffix[1:]}")

    @app.get("/api/jobs/{job_id}/assets/{kind}")
    def job_asset(job_id: JobID, kind: MediaKind) -> FileResponse:
        asset = next((item for item in store.get(job_id)["pipeline"].get("assets", [])
                      if item["kind"] == kind and item["status"] == "ready"), None)
        if not asset or not (path := asset_path(job_id, kind, asset["ext"])).is_file():
            raise HTTPException(404, "Файл не найден.")
        mime = "video/mp4" if kind == "video" else f"image/{asset['ext']}"
        return FileResponse(path, media_type=mime)

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        return store.overview(settings.demo)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: JobID) -> dict[str, Any]:
        return public_job(store.get(job_id))

    def can_publish(repo: str) -> bool:
        return bool(not settings.demo and admin_enabled
                    and (connect.enabled or (git_writer.key and repo.lower() in settings.github_write_repos)))

    @app.post("/api/workspaces", status_code=201)
    def open_workspace(target: RepoBrowse, request: Request) -> dict[str, str]:
        if not valid_repo(target.repo):
            raise HTTPException(422, "Укажи репозиторий в формате владелец/репозиторий.")
        try:
            tree = workspace.tree({"id": "", "repo": target.repo, "ref": target.ref,
                                   "commit_sha": None, "pr_url": None}, "base", github_for(request, target.repo))
        except ProviderError as exc:
            raise HTTPException(502, f"GitHub: {exc}") from None
        return {"id": store.open_workspace(tree["repo"], tree["ref"], tree["commit"])}

    @app.get("/api/jobs/{job_id}/workspace/tree")
    def workspace_tree(job_id: JobID, request: Request, revision: Revision = "base") -> dict[str, Any]:
        current = store.get(job_id)
        if not current["repo"]:
            raise HTTPException(422, "К этой задаче не привязан GitHub-репозиторий.")
        if revision == "pr" and not current["pr_url"]:
            raise HTTPException(404, "У задачи пока нет PR.")
        try:
            client = github_for(request, current["repo"])
            return {**workspace.tree(current, revision, client,
                                     (lambda repo: github_for(request, repo)) if connect.enabled else None),
                    "can_publish": revision == "base" and can_publish(current["repo"]),
                    "demo": settings.demo}
        except ProviderError as exc:
            raise HTTPException(502, f"GitHub: {exc}") from None

    @app.get("/api/jobs/{job_id}/workspace/file")
    def workspace_file(
        job_id: JobID, request: Request, repo: Repo, commit: Commit, path: SourcePath, blob: Commit, stamp: FileStamp,
    ) -> dict[str, Any]:
        current = store.get(job_id)
        if not current["repo"] or not workspace.allowed(job_id, repo, commit, path, blob, stamp):
            raise HTTPException(409, "Файл не относится к открытому снимку. Обнови дерево.")
        try:
            original = workspace.file(repo, path, blob, github_for(request, repo))
            text = redact(original)
            return {"path": path, "text": text, "editable": original == text and editable_path(path)}
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/jobs/{job_id}/workspace/publish", status_code=201)
    def publish_workspace(job_id: JobID, change: WorkspacePublish, request: Request) -> dict[str, str]:
        require_admin_enabled()
        current = store.get(job_id)
        repo = current["repo"]
        if not repo or not can_publish(repo):
            raise HTTPException(403, "Публикация не настроена для этого репозитория.")
        client = github_for(request, repo)
        writer = client if connect.enabled else git_writer
        paths = {item.path for item in change.edits}
        if (len(paths) != len(change.edits) or not all(map(editable_path, paths))
                or any(ord(char) < 32 or ord(char) == 127 for char in change.message)
                or redact(change.message) != change.message):
            raise HTTPException(422, "Проверь список файлов и сообщение коммита.")
        try:
            if any("\0" in item.text or len(item.text.encode()) > MAX_WORKSPACE_BYTES
                   or redact(item.text) != item.text for item in change.edits):
                raise HTTPException(422, "Недопустимое содержимое. Проверь размер и удали секреты.")
        except UnicodeError:
            raise HTTPException(422, "Текст файлов должен быть в UTF-8.") from None
        if not all(workspace.allowed(job_id, repo, change.commit, e.path, e.blob, e.stamp) for e in change.edits):
            raise HTTPException(409, "Файл не относится к снимку. Обнови проводник перед публикацией.")
        try:
            tree = client.tree(repo, current["commit_sha"] or current["ref"], workspace=True)
            if tree["commit"] != change.commit:
                raise HTTPException(409, "Исходная ветка изменилась. Сохрани правки и открой свежий снимок.")
            snapshot = {entry["path"]: entry for entry in tree["files"]}
            edits = []
            for item in change.edits:
                file = snapshot.get(item.path)
                if not file or file["blob"] != item.blob:
                    raise HTTPException(409, "Исходный файл изменился. Обнови проводник.")
                original = client.blob(repo, item.blob)
                if redact(original) != original:
                    raise HTTPException(422, "Нельзя публиковать файл со скрытыми участками.")
                if original != item.text:
                    edits.append({"path": item.path, "mode": file["mode"], "type": "blob", "content": item.text})
            if not edits:
                raise HTTPException(422, "Нет изменений для коммита.")
            return writer.publish_branch(repo, change.commit, tree["tree_sha"], f"bridge/{uuid4().hex}",
                                         change.message, edits)
        except ProviderError as exc:
            raise HTTPException(502, f"GitHub: {exc}") from None

    @app.post("/api/repo/tree")
    def repo_tree(target: RepoBrowse, request: Request) -> dict[str, Any]:
        if settings.demo:
            return {"repo": target.repo, "ref": target.ref or "main", "commit": "0" * 40,
                    "paths": list(DEMO_FILES), "truncated": False}
        try:
            return github_for(request, target.repo).tree(target.repo, target.ref)
        except ProviderError as exc:
            raise HTTPException(502, f"GitHub: {exc}") from None

    @app.post("/api/repo/preview")
    def repo_preview(target: RepoPreview, request: Request) -> dict[str, Any]:
        try:
            files = read_files(target.repo, target.commit, target.paths, github_for(request, target.repo))
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"commit": target.commit, "files": [{**file, "text": redact(file["text"])} for file in files]}

    @app.post("/api/jobs", status_code=202)
    def submit(task: Submission, background: BackgroundTasks, request: Request) -> dict[str, Any]:
        provider = selected_provider
        if task.grok_provider and task.grok_provider != provider:
            raise HTTPException(409, "Провайдер Grok изменился. Обнови страницу и проверь настройки.")
        try:
            grok = require_grok(provider)
        except ProviderError as exc:
            raise HTTPException(503, str(exc)) from None
        if (task.ref or task.context or task.draft_pr or task.review) and not task.repo:
            raise HTTPException(422, "Для ветки, контекста или настроек Devin укажи репозиторий.")
        if task.repo and not valid_repo(task.repo):
            raise HTTPException(422, "Укажи корректный репозиторий GitHub.")
        if task.ref and any(part in {"", ".", ".."} for part in task.ref.split("/")):
            raise HTTPException(422, "Укажи корректное имя ветки или тега.")
        if task.context and (not task.ref or len(set(task.context.paths)) != len(task.context.paths)
                             or not all(map(shareable_path, task.context.paths))):
            raise HTTPException(422, "Для контекста укажи ветку и разные файлы без секретов.")
        task.prompt = redact(task.prompt)
        client = github_for(request, task.repo) if task.repo else github
        recipes = skills.task_skills(skills.browser(request), task.skill_ids)
        current = store.insert(task, recipes, provider)
        background.add_task(dispatch, current["id"], task, recipes, grok, client)
        return public_job(current)

    @app.post("/api/jobs/{job_id}/generate", status_code=202)
    def approve_media(job_id: JobID, background: BackgroundTasks) -> dict[str, Any]:
        current = store.get(job_id)
        pipeline = current["route"] == "implement" and bool(current["pipeline"])
        if (pipeline and current["status"] == "awaiting" and not settings.demo
                and not devin_client(current["devin_org"] or settings.devin_org)):
            raise HTTPException(409, "Перед генерацией подключи исходную организацию Devin в настройках.")
        if ((not pipeline and current["route"] not in MEDIA_MODELS)
                or not store.apply(job_id, {"status": "sending"}, "awaiting")):
            raise HTTPException(409, "Генерация уже запущена или эта задача не требует подтверждения.")
        background.add_task(generate_pipeline if pipeline else generate, current)
        return public_job(store.get(job_id))

    @app.post("/api/jobs/{job_id}/refresh")
    def refresh(job_id: JobID) -> dict[str, Any]:
        advance(job_id)
        return public_job(store.get(job_id))

    return app


app = create_app(Settings.from_env())
