from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import lru_cache
from html.parser import HTMLParser
from threading import Lock
from time import monotonic, time
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from fastapi import APIRouter, HTTPException, Response
from fastapi import Request as APIRequest
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pair_skills import PAIR_SKILLS
from providers import NoRedirect, ProviderError, request_json

DIRECTORY = "https://www.skills.sh"
SEARCH = "https://skills.sh/api/search"
ID_PATTERN = r"(?:[\w.-]+/[\w.-]+/[\w.-]+|[\w.-]+\.[\w.-]+/[\w.-]+)"
SkillID = Annotated[str, StringConstraints(pattern=f"^{ID_PATTERN}$", max_length=180)]
MAX_TASK_SKILLS = 4
COOKIE = "bridge_skill_library"
CUSTOM_SOURCE = "my/skills"
MAX_CUSTOM = 20
PAGE_SIZE = 36
SEARCH_LIMIT = 200
TOPICS = ("coding", "frontend design", "python", "database", "documentation")
DESIGN_TOPICS = {
    "frontend": "frontend design",
    "ui": "ui ux interface",
    "figma": "figma design",
    "systems": "design system",
    "motion": "motion animation",
    "type": "typography",
    "accessibility": "web accessibility",
    "mobile": "mobile app design",
    "branding": "branding visual",
}
DESIGN_TERMS = (
    (r"дизайн[ -]+систем\w*", "design system"),
    (r"фигм\w*", "figma"),
    (r"типографик\w*|шрифт\w*", "typography"),
    (r"анимаци\w*|моуш[эе]н\w*|микровзаимодействи\w*", "motion animation"),
    (r"доступност\w*|контраст\w*", "accessibility"),
    (r"мобил\w*\s+приложени\w*", "mobile app"),
    (r"адаптив\w*|мобил\w*", "responsive"),
    (r"брендинг\w*|айдентик\w*|логотип\w*", "branding"),
    (r"д[аэе]шборд\w*|личн\w*\s+кабинет\w*", "dashboard"),
    (r"интерфейс\w*|юикс|юай", "ui ux"),
    (r"дизайн\w*|визуал\w*", "design"),
    (r"цвет\w*|палитр\w*", "colors"),
    (r"сетки?|сеток", "grid"),
    (r"кноп\w*", "buttons"),
)
RANK_PATHS = {"all-time": "/", "trending": "/trending", "hot": "/hot"}
RANK_LABELS = {"all-time": "установок за всё время", "trending": "установок за 24 ч", "hot": "изменение к тому же часу вчера"}
_showcase_lock = Lock()
MarketTab = Literal["catalog", "design", "recommended", "ready", "library", "all-time", "trending", "hot"]
MarketSort = Literal["default", "installs", "rating"]
DesignTopic = Literal["all", "frontend", "ui", "figma", "systems", "motion", "type", "accessibility", "mobile", "branding"]
RecipeGroup = Literal["all", "design", "engineering"]

# These instruction packs are maintained by the bridge, not fetched or run from third-party repositories.
RECIPES: dict[str, dict[str, str]] = {
    "bridge/agent-skills/plan-and-implement": {
        "title": "От плана к реализации",
        "description": "Grok разбивает задачу на этапы; Devin сверяет план с кодом и проверяет результат.",
        "grok": "Разложи цель на небольшие изменения с критериями приёмки. Пометь допущения и вопросы, которые нельзя решить без просмотра репозитория.",
        "devin": "Сверь план с устройством репозитория. Выбери минимальный набор изменений и проверь каждый критерий приёмки штатными проверками.",
    },
    "vercel-labs/agent-skills/vercel-react-best-practices": {
        "title": "React и Next.js",
        "description": "Производительность React: загрузка данных, размер бандла и лишние рендеры.",
        "grok": "Для React/Next.js оцени параллельную загрузку данных, границы сервера и клиента, размер бандла и стоимость рендеров. Предлагай проверяемые улучшения без выдуманных замеров.",
        "devin": "Проверь загрузку данных, границы серверных и клиентских компонентов и самые дорогие рендеры. Используй уже доступные в проекте проверки. Не добавляй зависимости только ради оптимизации.",
    },
    "vercel-labs/agent-skills/web-design-guidelines": {
        "title": "Доступность интерфейса",
        "group": "design",
        "description": "Клавиатура, семантика, контраст и состояния интерфейса.",
        "grok": "Проверь семантику, доступные имена, фокус, контраст и состояния загрузки, ошибки и пустого результата. Дай конкретные примеры для затронутого интерфейса.",
        "devin": "Проверь доступные имена, навигацию клавиатурой, видимый фокус и адаптивность. Опирайся на локальные инструменты проекта. Явно перечисли, что не удалось проверить.",
    },
    "anthropics/skills/frontend-design": {
        "title": "Дизайн интерфейсов",
        "group": "design",
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
    "bridge/agent-skills/review-and-verify": {
        "title": "Ревью и проверка правок",
        "description": "Grok выделяет риски и пробелы; Devin проверяет их на реальном коде.",
        "grok": "Перечисли конкретные риски для корректности и совместимости. Отдели проверенные замечания от гипотез. Если читаешь отчёт, отметь неподтверждённые выводы.",
        "devin": "Сверь замечания с кодом. Проверь затронутое поведение и совместимость. Исправляй только подтверждённые проблемы и только если задача разрешает изменения.",
    },
    **{f"bridge/agent-skills/{slug}": item for slug, item in PAIR_SKILLS.items()},
}

RECOMMENDED = (
    "bridge/agent-skills/studio-design",
    "bridge/agent-skills/media-for-design",
    "bridge/agent-skills/plan-and-implement",
    "bridge/agent-skills/interface-ux",
    "bridge/agent-skills/design-system",
    "bridge/agent-skills/dashboard-design",
    "bridge/agent-skills/figma-handoff",
    "bridge/agent-skills/motion-design",
    "bridge/agent-skills/typography",
    "bridge/agent-skills/responsive-ui",
    "bridge/agent-skills/visual-qa",
    "vercel-labs/agent-skills/web-design-guidelines",
    "anthropics/skills/frontend-design",
    "bridge/agent-skills/regression-tests",
    "bridge/agent-skills/review-and-verify",
    "vercel-labs/agent-skills/vercel-react-best-practices",
    "bridge/agent-skills/api-contract",
    "bridge/agent-skills/performance-budget",
    "bridge/agent-skills/safe-migrations",
    "bridge/agent-skills/debugging",
)


def valid_id(value: Any) -> bool:
    return (isinstance(value, str) and len(value) <= 180 and bool(re.fullmatch(ID_PATTERN, value, re.ASCII))
            and not any(part in {".", ".."} for part in value.split("/")))


def card(skill_id: str, *, name: str = "", installs: int | None = None) -> dict[str, Any]:
    source, slug = skill_id.rsplit("/", 1)
    recipe = RECIPES.get(skill_id)
    return {
        "id": skill_id, "name": name or slug, "source": source, "installs": installs,
        "url": None if source == "bridge/agent-skills" else directory_url(skill_id),
        "description": recipe["description"] if recipe else "",
        "recipe_title": recipe["title"] if recipe else None,
        "recipe_group": recipe.get("group", "engineering") if recipe else None,
    }


def directory_url(skill_id: str) -> str:
    prefix = "/site/" if skill_id.count("/") == 1 else "/"
    return f"{DIRECTORY}{prefix}{skill_id}"


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
    data = request_json(f"{SEARCH}?{urlencode({'q': query, 'limit': SEARCH_LIMIT})}", "")
    rows = data.get("skills")
    if not isinstance(rows, list):
        raise ProviderError("Некорректный ответ каталога.")
    return tuple(
        card(row["id"], name=row["name"][:150] if isinstance(row.get("name"), str) else "",
             installs=row["installs"] if type(row.get("installs")) is int and row["installs"] >= 0 else None)
        for row in rows[:SEARCH_LIMIT] if isinstance(row, dict) and valid_id(row.get("id"))
        and not row["id"].startswith(("bridge/agent-skills/", CUSTOM_SOURCE + "/"))
    )


def design_query(query: str) -> str:
    text = query.casefold().replace("ui/ux", "ui ux").replace("ux/ui", "ui ux")
    for term, translation in DESIGN_TERMS:
        text = re.sub(rf"\b(?:{term})\b", translation, text)
    return text


@lru_cache(maxsize=4)
def showcase(bucket: int, design: bool = False) -> tuple[tuple[dict[str, Any], ...], bool]:
    def load(topic: str) -> tuple[tuple[dict[str, Any], ...], bool]:
        try:
            return search_directory(topic, bucket), True
        except ProviderError:
            return (), False

    topics = tuple(DESIGN_TOPICS.values()) if design else TOPICS
    with ThreadPoolExecutor(max_workers=5) as pool:
        loaded = list(pool.map(load, topics))
    # Round-robin keeps the first page varied and doesn't let a single topic fill it.
    groups = [rows for rows, _ in loaded]
    items: dict[str, dict[str, Any]] = {}
    for index in range(SEARCH_LIMIT):
        for group in groups:
            if index < len(group):
                items.setdefault(group[index]["id"], group[index])
    return tuple(items.values()), all(ok for _, ok in loaded)


class Rankings(HTMLParser):
    def __init__(self, view: str) -> None:
        super().__init__()
        self.view = view
        self.path = ""
        self.parts: list[str] = []
        self.total: int | None = None
        self.cards: dict[str, dict[str, Any]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.path = dict(attrs).get("href") or ""
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.path.startswith("/") and data.strip():
            self.parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        path, parts = self.path, self.parts
        self.path, self.parts = "", []
        if path == "/":
            match = re.fullmatch(r"All Time\s*\(([\d,]+)\)", " ".join(parts))
            if match:
                self.total = int(match[1].replace(",", ""))
            return
        skill_id = path.removeprefix("/site/") if path.startswith("/site/") else path[1:]
        if not path.startswith("/") or not valid_id(skill_id) or len(parts) < 4:
            return
        if not re.fullmatch(r"[1-9][0-9]{0,5}", parts[0]):
            return
        stat = parts[-1]
        if not re.fullmatch(r"[+-]?[0-9,.]{1,10}[KMB]?", stat):
            return
        self.cards.setdefault(skill_id, {
            **card(skill_id, name=parts[1][:150]), "rank": int(parts[0]), "rank_stat": stat,
            "rank_label": RANK_LABELS[self.view],
        })


def html_page(url: str, limit: int) -> str:
    req = Request(url, headers={"Accept": "text/html"})
    with build_opener(NoRedirect()).open(req, timeout=6) as response:
        return response.read(limit).decode("utf-8", errors="replace")


@lru_cache(maxsize=12)
def leaderboard(view: str, _bucket: int) -> tuple[tuple[dict[str, Any], ...], int | None]:
    parser = Rankings(view)
    try:
        parser.feed(html_page(f"{DIRECTORY}{RANK_PATHS[view]}", 1_600_000))
    except (URLError, OSError):
        raise ProviderError("Рейтинг Skills.sh временно недоступен.") from None
    if not parser.cards:
        raise ProviderError("Не удалось получить рейтинг Skills.sh.")
    return tuple(sorted(parser.cards.values(), key=lambda item: item["rank"])), parser.total


@lru_cache(maxsize=256)
def directory_overview(skill_id: str, _bucket: int) -> str:
    try:
        html = html_page(directory_url(skill_id), 192 * 1024)
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


Instruction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=1500)]


class CustomSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=80)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""
    grok: Instruction
    devin: Instruction


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
                CREATE TABLE IF NOT EXISTS custom_skills (
                    skill_id TEXT PRIMARY KEY, browser TEXT NOT NULL, title TEXT NOT NULL,
                    grok TEXT NOT NULL, devin TEXT NOT NULL
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
        recipes = {**self.custom(browser), **RECIPES}
        if (len(skill_ids) != len(set(skill_ids))
                or any(skill_id not in installed or skill_id not in recipes for skill_id in skill_ids)):
            raise HTTPException(422, "Выбери разные установленные скиллы: готовые для агентов или свои.")
        return [{"id": skill_id, **recipes[skill_id]} for skill_id in skill_ids]

    def custom(self, browser: str) -> dict[str, dict[str, str]]:
        with closing(self.connect()) as conn:
            return {row["skill_id"]: {key: row[key] for key in ("title", "grok", "devin")} for row in conn.execute(
                "SELECT skill_id, title, grok, devin FROM custom_skills WHERE browser = ?", (browser,))}

    @staticmethod
    def ensure_browser(request: APIRequest, response: Response) -> str:
        browser = Skills.browser(request)
        if not browser:
            browser = secrets.token_hex(32)
            response.set_cookie(COOKIE, browser, max_age=31_536_000, httponly=True, samesite="lax",
                                secure=request.url.scheme == "https")
        return browser

    def details(self, skill_id: str) -> dict[str, Any]:
        if not valid_id(skill_id):
            raise HTTPException(404, "Скилл не найден.")
        if skill_id in RECIPES:
            return card(skill_id)
        if skill_id.startswith(CUSTOM_SOURCE + "/"):
            raise HTTPException(404, "Этот скилл доступен только в библиотеке автора.")
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
        scores: dict[str, tuple[float, int]] = {}
        with closing(self.connect()) as conn:
            for start in range(0, len(ids), 400):
                batch = ids[start:start + 400]
                marks = ", ".join("?" for _ in batch)
                rows = conn.execute(f"""
                    SELECT skill_id, ROUND(AVG(stars), 1) AS rating, COUNT(*) AS votes
                    FROM skill_reviews WHERE skill_id IN ({marks}) GROUP BY skill_id
                """, batch)
                scores.update((row["skill_id"], (row["rating"], row["votes"])) for row in rows)
        return [
            {**item, "installed": item["id"] in library,
             **dict(zip(("rating", "votes"), scores.get(item["id"], (None, 0)), strict=True))}
            for item in cards
        ]

    def routes(self, redact: Callable[[str], str]) -> APIRouter:
        router = APIRouter(prefix="/api/skills")

        @router.get("")
        def browse(request: APIRequest, q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = "",
                   tab: MarketTab = "catalog", sort: MarketSort = "default",
                   topic: DesignTopic = "all", group: RecipeGroup = "all",
                   page: Annotated[int, Field(ge=1, le=60)] = 1) -> dict[str, Any]:
            library = self.installed(self.browser(request))
            warning = ""
            source_total = None
            bucket = int(monotonic()) // 600
            if tab == "library":
                rows = list(library.values())
            elif tab in RANK_PATHS:
                rows = []
                if self.public:
                    try:
                        ranking, source_total = leaderboard(tab, bucket)
                        rows = list(ranking)
                    except ProviderError as exc:
                        warning = str(exc)
                else:
                    warning = "Рейтинги доступны при подключении к Skills.sh."
            else:
                rows = [card(skill_id) for skill_id in (RECOMMENDED if tab == "recommended" else RECIPES)]
            if tab == "design":
                rows = [item for item in rows if item["recipe_group"] == "design"] if topic == "all" or q else []
            if tab == "recommended" and group != "all":
                rows = [item for item in rows if item["recipe_group"] == group]
            if q:
                rows = [row for row in rows if q.casefold() in " ".join(
                    str(row[key] or "") for key in ("id", "name", "recipe_title", "description")
                ).casefold()]
            if tab in {"catalog", "design"} and self.public and len(q) != 1:
                try:
                    design = tab == "design"
                    if q or (design and topic != "all"):
                        if design:
                            translated = design_query(q)
                            context = DESIGN_TOPICS.get(topic, "design")
                            query = " ".join(dict.fromkeys((translated + " " + context).split()))
                        else:
                            query = design_query(q)
                        external = search_directory(query, bucket)
                        if design and q:
                            tokens = set(design_query(q).split()) - {"design", "web"}
                            external = tuple(sorted(external, key=lambda item: sum(
                                token in item["id"].casefold() or token in item["name"].casefold() for token in tokens
                            ), reverse=True))
                    else:
                        with _showcase_lock:
                            external, complete = showcase(bucket, design)
                        if not complete:
                            warning = ("Часть подборок Skills.sh недоступна." if external else
                                       "Skills.sh временно недоступен. Показаны скиллы из набора «Моста».")
                    merged = {row["id"]: row for row in rows}
                    merged.update((row["id"], row) for row in external)
                    rows = list(merged.values())
                except ProviderError:
                    warning = "Skills.sh временно недоступен. Показаны скиллы из набора «Моста»."
            total = len(rows)
            start, end = (page - 1) * PAGE_SIZE, page * PAGE_SIZE
            if sort == "rating":
                ranked = self.decorate(rows, library)
                ranked.sort(key=lambda item: (item["rating"] or 0, item["votes"]), reverse=True)
                items = ranked[start:end]
            else:
                if sort == "installs" and tab not in RANK_PATHS:
                    rows.sort(key=lambda item: item["installs"] if item["installs"] is not None else -1, reverse=True)
                items = self.decorate(rows[start:end], library)
            return {"items": items, "warning": warning, "public": self.public, "total": total,
                    "has_more": end < total, "source_total": source_total}

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
                    "recipe": {agent: recipe[agent] for agent in ("grok", "devin")}
                    if (recipe := RECIPES.get(skill_id) or self.custom(browser).get(skill_id)) else None}

        @router.post("/library")
        def set_library(change: Selection, request: APIRequest, response: Response) -> dict[str, bool]:
            browser = self.ensure_browser(request, response)
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
                    # Removing your own skill deletes its instructions and reviews, not just the bookmark.
                    if conn.execute("DELETE FROM custom_skills WHERE browser = ? AND skill_id = ?",
                                    (browser, change.skill_id)).rowcount:
                        conn.execute("DELETE FROM skill_reviews WHERE skill_id = ?", (change.skill_id,))
            return {"installed": change.installed}

        @router.post("/custom", status_code=201)
        def create_custom(skill: CustomSkill, request: APIRequest, response: Response) -> dict[str, str]:
            browser = self.ensure_browser(request, response)
            skill_id = f"{CUSTOM_SOURCE}/{secrets.token_hex(8)}"
            title = redact(skill.title)
            metadata = {
                "id": skill_id, "name": title, "source": CUSTOM_SOURCE, "installs": None, "url": None,
                "description": redact(skill.description), "recipe_title": title, "recipe_group": "custom",
            }
            with closing(self.connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                own = conn.execute("SELECT COUNT(*) FROM custom_skills WHERE browser = ?", (browser,)).fetchone()[0]
                saved = conn.execute("SELECT COUNT(*) FROM skill_library WHERE browser = ?", (browser,)).fetchone()[0]
                if own >= MAX_CUSTOM or saved >= 100:
                    raise HTTPException(409, f"Можно создать до {MAX_CUSTOM} своих скиллов и хранить до 100 в библиотеке.")
                conn.execute("INSERT INTO custom_skills VALUES (?, ?, ?, ?, ?)",
                             (skill_id, browser, title, redact(skill.grok), redact(skill.devin)))
                conn.execute("INSERT INTO skill_library VALUES (?, ?, ?)",
                             (browser, skill_id, json.dumps(metadata, ensure_ascii=False)))
            return {"id": skill_id}

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
