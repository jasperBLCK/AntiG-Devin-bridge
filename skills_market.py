from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from functools import lru_cache
from html.parser import HTMLParser
from time import monotonic, time
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from fastapi import APIRouter, HTTPException, Response
from fastapi import Request as APIRequest
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from providers import NoRedirect, ProviderError, request_json

DIRECTORY = "https://www.skills.sh"
SEARCH = "https://skills.sh/api/search"
ID_PATTERN = r"(?:[\w.-]+/[\w.-]+/[\w.-]+|[\w.-]+\.[\w.-]+/[\w.-]+)"
SkillID = Annotated[str, StringConstraints(pattern=f"^{ID_PATTERN}$", max_length=180)]
MAX_TASK_SKILLS = 4
COOKIE = "bridge_skill_library"

# These instruction packs are maintained by the bridge, not fetched or run from third-party repositories.
RECIPES: dict[str, dict[str, str]] = {
    "vercel-labs/agent-skills/vercel-react-best-practices": {
        "title": "React и Next.js",
        "description": "Производительность React: загрузка данных, размер бандла и лишние рендеры.",
        "grok": "Для React/Next.js оцени параллельную загрузку данных, границы сервера и клиента, размер бандла и стоимость рендеров. Предлагай проверяемые улучшения без выдуманных замеров.",
        "devin": "Проверь загрузку данных, границы серверных и клиентских компонентов и самые дорогие рендеры. Используй уже доступные в проекте проверки. Не добавляй зависимости только ради оптимизации.",
    },
    "vercel-labs/agent-skills/web-design-guidelines": {
        "title": "Доступность интерфейса",
        "description": "Клавиатура, семантика, контраст и состояния интерфейса.",
        "grok": "Проверь семантику, доступные имена, фокус, контраст и состояния загрузки, ошибки и пустого результата. Дай конкретные примеры для затронутого интерфейса.",
        "devin": "Проверь доступные имена, навигацию клавиатурой, видимый фокус и адаптивность. Опирайся на локальные инструменты проекта. Явно перечисли, что не удалось проверить.",
    },
    "anthropics/skills/frontend-design": {
        "title": "Дизайн интерфейсов",
        "description": "Визуальная иерархия, типографика, композиция и адаптивность.",
        "grok": "Сформулируй визуальную иерархию с опорой на аудиторию, содержание, типографику и существующие компоненты. Учитывай мобильный экран и доступность.",
        "devin": "Используй существующие компоненты и дизайн-токены. Проверь композицию, типографику и читаемость на узком и широком экране; предусмотрены ли видимый фокус и reduced motion.",
    },
    "bridge/agent-skills/regression-tests": {
        "title": "Регрессионные тесты",
        "description": "Целевые проверки и понятный отчёт об исправлении.",
        "grok": "Выдели ожидаемое поведение и значимые граничные случаи. Различай запланированные и реально выполненные проверки.",
        "devin": "Проверь затронутое поведение штатными тестами проекта. Если изменения разрешены, добавь минимальный регрессионный тест. Сообщи, какие проверки прошли, какие не запустились и почему.",
    },
}


def valid_id(value: Any) -> bool:
    return (isinstance(value, str) and len(value) <= 180 and bool(re.fullmatch(ID_PATTERN, value, re.ASCII))
            and not any(part in {".", ".."} for part in value.split("/")))


def card(skill_id: str, *, name: str = "", installs: int | None = None) -> dict[str, Any]:
    source, slug = skill_id.rsplit("/", 1)
    recipe = RECIPES.get(skill_id)
    return {
        "id": skill_id, "name": name or slug, "source": source, "installs": installs,
        "url": None if source == "bridge/agent-skills" else f"{DIRECTORY}/{skill_id}",
        "description": recipe["description"] if recipe else "",
        "recipe_title": recipe["title"] if recipe else None,
    }


class Overview(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:description":
            self.description = (attributes.get("content") or "").strip()[:1000]


@lru_cache(maxsize=256)
def search_directory(query: str, _bucket: int) -> tuple[dict[str, Any], ...]:
    data = request_json(f"{SEARCH}?{urlencode({'q': query, 'limit': 36})}", "")
    rows = data.get("skills")
    if not isinstance(rows, list):
        raise ProviderError("Некорректный ответ каталога.")
    return tuple(
        card(row["id"], name=row["name"][:150] if isinstance(row.get("name"), str) else "",
             installs=row["installs"] if type(row.get("installs")) is int and row["installs"] >= 0 else None)
        for row in rows[:36] if isinstance(row, dict) and valid_id(row.get("id"))
        and not row["id"].startswith("bridge/agent-skills/")
    )


@lru_cache(maxsize=256)
def directory_overview(skill_id: str, _bucket: int) -> str:
    req = Request(f"{DIRECTORY}/{skill_id}", headers={"Accept": "text/html"})
    try:
        with build_opener(NoRedirect()).open(req, timeout=6) as response:
            html = response.read(192 * 1024).decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(404, "Скилл не найден в каталоге.") from None
        raise ProviderError("Карточка Skills.sh временно недоступна.") from None
    except (URLError, OSError):
        raise ProviderError("Карточка Skills.sh временно недоступна.") from None
    parser = Overview()
    parser.feed(html)
    return parser.description


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: SkillID
    installed: bool


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: SkillID
    stars: int = Field(ge=1, le=5, strict=True)
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class Skills:
    def __init__(self, connect: Callable[[], sqlite3.Connection], *, public: bool) -> None:
        self.connect = connect
        self.public = public

    def init(self) -> None:
        with closing(self.connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS skill_library (
                    browser TEXT NOT NULL, skill_id TEXT NOT NULL, metadata TEXT NOT NULL,
                    PRIMARY KEY (browser, skill_id)
                );
                CREATE TABLE IF NOT EXISTS skill_reviews (
                    skill_id TEXT NOT NULL, browser TEXT NOT NULL, stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5),
                    text TEXT NOT NULL, updated INTEGER NOT NULL, PRIMARY KEY (skill_id, browser)
                );
            """)

    @staticmethod
    def browser(request: APIRequest) -> str:
        token = request.cookies.get(COOKIE, "")
        return token if re.fullmatch(r"[a-f0-9]{64}", token) else ""

    def installed(self, browser: str) -> dict[str, dict[str, Any]]:
        with closing(self.connect()) as conn:
            return {row["skill_id"]: json.loads(row["metadata"]) for row in conn.execute(
                "SELECT skill_id, metadata FROM skill_library WHERE browser = ? ORDER BY skill_id", (browser,),
            )}

    def task_skills(self, browser: str, skill_ids: list[str]) -> list[dict[str, str]]:
        if not skill_ids:
            return []
        installed = self.installed(browser)
        if (len(skill_ids) != len(set(skill_ids))
                or any(skill_id not in installed or skill_id not in RECIPES for skill_id in skill_ids)):
            raise HTTPException(422, "Выбери разные установленные скиллы из раздела «Готовые для агентов».")
        return [{"id": skill_id, **RECIPES[skill_id]} for skill_id in skill_ids]

    def details(self, skill_id: str) -> dict[str, Any]:
        if not valid_id(skill_id):
            raise HTTPException(404, "Скилл не найден.")
        if skill_id in RECIPES:
            return card(skill_id)
        if not self.public:
            raise HTTPException(404, "Этот скилл недоступен в офлайн-демо.")
        try:
            return {**card(skill_id), "description": directory_overview(skill_id, int(monotonic()) // 600)}
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from None

    def decorate(self, cards: list[dict[str, Any]], library: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        ids = tuple(item["id"] for item in cards)
        if not ids:
            return []
        with closing(self.connect()) as conn:
            marks = ", ".join("?" for _ in ids)
            scores = {row["skill_id"]: (row["rating"], row["votes"]) for row in conn.execute(
                f"SELECT skill_id, ROUND(AVG(stars), 1) AS rating, COUNT(*) AS votes "
                f"FROM skill_reviews WHERE skill_id IN ({marks}) GROUP BY skill_id", ids,
            )}
        return [
            {**item, "installed": item["id"] in library,
             **dict(zip(("rating", "votes"), scores.get(item["id"], (None, 0)), strict=True))}
            for item in cards
        ]

    def routes(self, redact: Callable[[str], str]) -> APIRouter:
        router = APIRouter(prefix="/api/skills")

        @router.get("")
        def browse(request: APIRequest, q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = "",
                   tab: Literal["catalog", "ready", "library"] = "catalog") -> dict[str, Any]:
            library = self.installed(self.browser(request))
            if tab == "library":
                rows = list(library.values())
            else:
                rows = [card(skill_id) for skill_id in RECIPES]
            if q:
                rows = [row for row in rows if q.casefold() in " ".join(
                    str(row[key] or "") for key in ("id", "recipe_title", "description")
                ).casefold()]
            warning = ""
            if tab == "catalog" and self.public and len(q) != 1:
                try:
                    external = search_directory(q or "coding", int(monotonic()) // 300)
                    merged = {row["id"]: row for row in rows}
                    merged.update((row["id"], row) for row in external)
                    rows = list(merged.values())
                except ProviderError:
                    warning = "Skills.sh временно недоступен. Показаны скиллы из набора «Моста»."
            return {"items": self.decorate(rows, library), "warning": warning, "public": self.public}

        @router.get("/info")
        def info(skill_id: SkillID, request: APIRequest) -> dict[str, Any]:
            browser = self.browser(request)
            library = self.installed(browser)
            item = library.get(skill_id) or self.details(skill_id)
            with closing(self.connect()) as conn:
                rows = conn.execute("""
                    SELECT stars, text, updated, browser = ? AS mine FROM skill_reviews
                    WHERE skill_id = ? ORDER BY updated DESC, browser LIMIT 30
                """, (browser, skill_id)).fetchall()
                own = conn.execute("SELECT stars, text FROM skill_reviews WHERE skill_id = ? AND browser = ?",
                                   (skill_id, browser)).fetchone()
            return {**self.decorate([item], library)[0], "reviews": [dict(row) for row in rows],
                    "my_review": dict(own) if own else None,
                    "recipe": {agent: RECIPES[skill_id][agent] for agent in ("grok", "devin")}
                    if skill_id in RECIPES else None}

        @router.post("/library")
        def set_library(change: Selection, request: APIRequest, response: Response) -> dict[str, bool]:
            browser = self.browser(request)
            if not browser:
                browser = secrets.token_hex(32)
                response.set_cookie(COOKIE, browser, max_age=31_536_000, httponly=True, samesite="lax",
                                    secure=request.url.scheme == "https")
            library = self.installed(browser)
            if change.installed and change.skill_id not in library:
                item = self.details(change.skill_id)
                with closing(self.connect()) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    count = conn.execute("SELECT COUNT(*) FROM skill_library WHERE browser = ?", (browser,)).fetchone()
                    if count[0] >= 100:
                        raise HTTPException(409, "В библиотеке можно сохранить до 100 скиллов.")
                    conn.execute("INSERT OR IGNORE INTO skill_library VALUES (?, ?, ?)",
                                 (browser, change.skill_id, json.dumps(item, ensure_ascii=False)))
            elif not change.installed:
                with closing(self.connect()) as conn, conn:
                    conn.execute("DELETE FROM skill_library WHERE browser = ? AND skill_id = ?",
                                 (browser, change.skill_id))
            return {"installed": change.installed}

        @router.post("/review")
        def leave_review(review: Review, request: APIRequest) -> dict[str, bool]:
            browser = self.browser(request)
            with closing(self.connect()) as conn, conn:
                result = conn.execute("""
                    INSERT INTO skill_reviews (skill_id, browser, stars, text, updated)
                    SELECT skill_id, browser, ?, ?, ? FROM skill_library WHERE browser = ? AND skill_id = ?
                    ON CONFLICT(skill_id, browser) DO UPDATE SET
                        stars = excluded.stars, text = excluded.text, updated = excluded.updated
                """, (review.stars, redact(review.text), int(time()), browser, review.skill_id))
                if not result.rowcount:
                    raise HTTPException(403, "Сначала добавь скилл в свою библиотеку.")
            return {"saved": True}

        return router
