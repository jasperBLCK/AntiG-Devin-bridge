from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import time
from typing import Annotated, Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from orchestration import (
    DEVIN_ROUTES,
    MEDIA_MODELS,
    REVIEWER,
    ROUTER,
    demo_decision,
    handoff,
    parse_decision,
)
from providers import GROK_MODEL, AntiG, Devin, ProviderError
from repository import (
    DEMO_FILES,
    MAX_CONTEXT_CHARS,
    MAX_FILES,
    GitHub,
    shareable_path,
    valid_repo,
)
from workspace import Revision, Workspace

ROOT = Path(__file__).resolve().parent
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

    @classmethod
    def from_env(cls) -> Settings:
        demo = os.getenv("BRIDGE_MODE") == "demo"
        return cls(
            db=Path(os.getenv("BRIDGE_DB", str(ROOT / "data" / ("demo.sqlite3" if demo else "bridge.sqlite3")))),
            demo=demo,
            antig_key=os.getenv("ANTIG_API_KEY", "").strip(),
            devin_key=os.getenv("DEVIN_API_KEY", "").strip(),
            devin_org=os.getenv("DEVIN_ORG_ID", "").strip(),
            github_read_token=os.getenv("GITHUB_READ_TOKEN", "").strip(),
        )


class RepoBrowse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: Repo
    ref: GitRef | None = None


class RepoPreview(RepoBrowse):
    commit: Commit
    paths: list[SourcePath] = Field(min_length=1, max_length=MAX_FILES)


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
    max_acu: int = Field(default=5, ge=1, le=100)
    draft_pr: bool = False
    review: bool = False


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
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            additions = {
                "ref": "TEXT", "commit_sha": "TEXT", "route": "TEXT", "reason": "TEXT", "plan": "TEXT",
                "routed_at": "TEXT", "finished_at": "TEXT", "reviewed_at": "TEXT",
                "activity": "TEXT NOT NULL DEFAULT '{}'", "last_polled": "REAL NOT NULL DEFAULT 0",
                "review_allowed": "INTEGER NOT NULL DEFAULT 0",
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
        return {**dict(row), "activity": json.loads(row["activity"])}

    def get(self, job_id: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Задача не найдена.")
        return self.decode(row)

    def recent(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            columns = ", ".join(PUBLIC_COLUMNS)
            rows = conn.execute(f"SELECT {columns} FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    def due(self, before: float) -> list[str]:
        with closing(self.connect()) as conn:
            rows = conn.execute("""
                SELECT id FROM jobs WHERE remote_id IS NOT NULL AND status IN ('running', 'paused')
                AND last_polled <= CASE WHEN status = 'paused' THEN ? - 30 ELSE ? END
                ORDER BY last_polled LIMIT 32
            """, (before, before)).fetchall()
        return [row["id"] for row in rows]

    def insert(self, task: Submission) -> dict[str, Any]:
        job_id = uuid4().hex
        with closing(self.connect()) as conn, conn:
            conn.execute("""
                INSERT INTO jobs (id, mode, prompt, model, repo, ref, commit_sha, max_acu, created_at, review_allowed)
                VALUES (?, 'auto', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (job_id, task.prompt, GROK_MODEL, task.repo, task.ref, task.context.commit if task.context else None,
                  task.max_acu, iso_now(), task.review))
        return self.get(job_id)

    def apply(self, job_id: str, update: dict[str, Any], previous_status: str) -> bool:
        keys = {
            "status", "result", "media_url", "remote_id", "session_url", "pr_url", "progress", "error",
            "model", "route", "reason", "plan", "activity", "routed_at", "finished_at", "reviewed_at", "last_polled",
        }
        columns = [key for key in update if key in keys]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        args = [json.dumps(update[key], ensure_ascii=False) if key == "activity" else update[key] for key in columns]
        with closing(self.connect()) as conn, conn:
            result = conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ? AND status = ?",
                                  (*args, job_id, previous_status))
            return bool(result.rowcount)


def public_job(job: dict[str, Any], *, full: bool = True) -> dict[str, Any]:
    result = {key: job[key] for key in PUBLIC_COLUMNS}
    if full:
        result.update({key: job[key] for key in ("result", "reason", "plan", "error", "media_url")})
        result["activity"] = {key: val for key, val in job["activity"].items()
                              if key not in {"cursor", "pr_checked", "pr_url"}}
    return result


def create_app(settings: Settings, *, poll_seconds: float = 8) -> FastAPI:
    store = Store(settings.db)
    antig = AntiG(settings.antig_key)
    github = GitHub(settings.github_read_token)
    workspace = Workspace(github, settings.demo)
    devin = Devin(settings.devin_key, settings.devin_org) if settings.devin_key and settings.devin_org else None

    polling: set[str] = set()
    poll_lock = Lock()

    def redact(text: str) -> str:
        for secret in (settings.antig_key, settings.devin_key, settings.github_read_token):
            if secret:
                text = text.replace(secret, "[скрыто]")
        return text

    def read_files(repo: str, commit: str, paths: list[str]) -> list[dict[str, str]]:
        if len(set(paths)) != len(paths) or not all(map(shareable_path, paths)):
            raise ProviderError("Выбери разные текстовые файлы без секретов.")
        try:
            files = [{"path": path, "text": DEMO_FILES[path] if settings.demo else github.file(repo, commit, path)}
                     for path in paths]
        except KeyError:
            raise ProviderError("Файл не найден в демо.") from None
        if sum(len(item["text"]) for item in files) > MAX_CONTEXT_CHARS:
            raise ProviderError("Слишком большой контекст. Выбери меньше файлов.")
        return files

    def dispatch(job_id: str, task: Submission) -> None:
        try:
            files = read_files(task.repo, task.context.commit, task.context.paths) if task.repo and task.context else []
            context = json.dumps({
                "request": redact(task.prompt), "repository": task.repo, "base_ref": task.ref,
                "commit": task.context.commit if task.context else None,
                "files": [{**file, "text": redact(file["text"])} for file in files],
            }, ensure_ascii=False)
            decision = demo_decision(task.prompt, task.repo) if settings.demo else parse_decision(antig.text(context, ROUTER))
            content = redact(decision.content)
            update: dict[str, Any] = {"route": decision.route, "reason": redact(decision.reason), "routed_at": iso_now()}
            if decision.route in DEVIN_ROUTES:
                if not task.repo:
                    update.update(status="done", route="clarify", reason="Для работы Devin нужен репозиторий.",
                                  result="Укажи репозиторий GitHub и повтори задачу.")
                else:
                    store.apply(job_id, {**update, "plan": content}, "sending")
                    prompt = handoff(
                        redact(task.prompt), content, task.repo, task.ref, task.context.commit if task.context else None,
                        inspect=decision.route == "inspect", draft_pr=task.draft_pr,
                    )
                    if settings.demo:
                        update.update(status="running", remote_id="demo")
                    elif devin:
                        update.update(devin.start(prompt, task.repo, task.max_acu))
                        # The create response has no final report; even an ended session needs its first poll.
                        if update["status"] == "ended":
                            update["status"] = "running"
                    else:
                        raise ProviderError("Задача требует Devin. Добавь ключ Devin и запусти её снова.")
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
            if settings.demo:
                update = ({"status": "done", "media_url": "/static/sample.svg", "result": "Демо-изображение."}
                          if route == "image" else {"status": "running", "remote_id": "demo"})
            elif not settings.antig_key:
                raise ProviderError("Ключ AntiG не настроен.")
            else:
                update = antig.image(prompt, model) if route == "image" else antig.start_video(prompt, model)
        except ProviderError as exc:
            update = {"status": "failed", "error": str(exc)}
        if update.get("status") in {"done", "failed"}:
            update["finished_at"] = iso_now()
        store.apply(job["id"], update, "sending")

    def review(job: dict[str, Any], report: dict[str, str]) -> None:
        try:
            # Only the opted-in structured report, not raw messages, diffs or tool logs, goes back to AntiG.
            review_input = json.dumps({"request": redact(job["prompt"]), "devin_report": report}, ensure_ascii=False)
            result = ("Демо-разбор Grok: сверь проверку пустого файла с критериями задачи."
                      if settings.demo else antig.text(review_input, REVIEWER))
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
        if pr_url and job["repo"] and (
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
            if not remote_id or status not in {"running", "paused"}:
                return
            try:
                if settings.demo:
                    update = demo_update(job)
                elif (route in DEVIN_ROUTES or route == "devin") and devin:
                    update = devin_update(job, devin)
                elif route == "video" and settings.antig_key:
                    update = antig.video_status(remote_id)
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
        worker = asyncio.create_task(monitor()) if poll_seconds > 0 else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    app = FastAPI(title="AntiG × Devin", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def protect(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method == "POST" and request.headers.get("X-Bridge-Request") != "1":
            return JSONResponse({"detail": "Запрос должен быть отправлен из панели."}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https:; media-src 'self' https:; "
            "base-uri 'none'; object-src 'none'; form-action 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/workspace/{job_id}")
    def workspace_page(job_id: JobID) -> FileResponse:
        store.get(job_id)
        return FileResponse(ROOT / "static" / "workspace.html")

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "demo": settings.demo,
            "antig": bool(settings.antig_key or settings.demo),
            "devin": bool(devin or settings.demo),
            "github_private": bool(settings.github_read_token and not settings.demo),
        }

    @app.get("/api/jobs")
    def recent() -> list[dict[str, Any]]:
        return [public_job(row, full=False) for row in store.recent()]

    @app.get("/api/jobs/{job_id}")
    def job(job_id: JobID) -> dict[str, Any]:
        return public_job(store.get(job_id))

    @app.get("/api/jobs/{job_id}/workspace/tree")
    def workspace_tree(job_id: JobID, revision: Revision = "base") -> dict[str, Any]:
        current = store.get(job_id)
        if not current["repo"]:
            raise HTTPException(422, "К этой задаче не привязан GitHub-репозиторий.")
        if revision == "pr" and not current["pr_url"]:
            raise HTTPException(404, "У задачи пока нет PR.")
        try:
            return workspace.tree(current, revision)
        except ProviderError as exc:
            raise HTTPException(502, f"GitHub: {exc}") from None

    @app.get("/api/jobs/{job_id}/workspace/file")
    def workspace_file(
        job_id: JobID, repo: Repo, commit: Commit, path: SourcePath, blob: Commit, stamp: FileStamp,
    ) -> dict[str, str]:
        current = store.get(job_id)
        if not current["repo"] or not workspace.allowed(job_id, repo, commit, path, blob, stamp):
            raise HTTPException(409, "Файл не относится к открытому снимку. Обнови дерево.")
        try:
            return {"path": path, "text": redact(workspace.file(repo, path, blob))}
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/repo/tree")
    def repo_tree(target: RepoBrowse) -> dict[str, Any]:
        if settings.demo:
            return {"repo": target.repo, "ref": target.ref or "main", "commit": "0" * 40,
                    "paths": list(DEMO_FILES), "truncated": False}
        try:
            return github.tree(target.repo, target.ref)
        except ProviderError as exc:
            raise HTTPException(502, f"GitHub: {exc}") from None

    @app.post("/api/repo/preview")
    def repo_preview(target: RepoPreview) -> dict[str, Any]:
        try:
            files = read_files(target.repo, target.commit, target.paths)
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"commit": target.commit, "files": files}

    @app.post("/api/jobs", status_code=202)
    def submit(task: Submission, background: BackgroundTasks) -> dict[str, Any]:
        if not settings.demo and not settings.antig_key:
            raise HTTPException(503, "Добавь ключ AntiG и перезапусти сервер.")
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
        current = store.insert(task)
        background.add_task(dispatch, current["id"], task)
        return public_job(current)

    @app.post("/api/jobs/{job_id}/generate", status_code=202)
    def approve_media(job_id: JobID, background: BackgroundTasks) -> dict[str, Any]:
        current = store.get(job_id)
        if current["route"] not in MEDIA_MODELS or not store.apply(job_id, {"status": "sending"}, "awaiting"):
            raise HTTPException(409, "Генерация уже запущена или эта задача не требует подтверждения.")
        background.add_task(generate, current)
        return public_job(store.get(job_id))

    @app.post("/api/jobs/{job_id}/refresh")
    def refresh(job_id: JobID) -> dict[str, Any]:
        advance(job_id)
        return public_job(store.get(job_id))

    return app


app = create_app(Settings.from_env())
