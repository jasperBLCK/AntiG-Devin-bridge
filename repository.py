from __future__ import annotations

import base64
import binascii
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from providers import ProviderError, request_json

MAX_FILES = 6
MAX_FILE_BYTES = 16_000
MAX_CONTEXT_CHARS = 36_000
MAX_WORKSPACE_BYTES = 128 * 1024
MAX_WORKSPACE_PATHS = 4000

GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "AntiG-Devin-bridge"}


def sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ProviderError("GitHub не вернул корректный SHA.")
    return value


def valid_repo(repo: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)) and all(
        part not in {".", ".."} for part in repo.split("/")
    )


def shareable_path(path: str) -> bool:
    parts = path.lower().split("/")
    filename = parts[-1]
    return (
        0 < len(path) <= 320
        and all(part and part not in {".", "..", ".git", "secrets", "credentials", "node_modules", "vendor"}
                for part in parts)
        and "\\" not in path
        and not any(ord(ch) < 32 for ch in path)
        and not filename.startswith((".env", "id_rsa", "id_ed25519"))
        and filename not in {".npmrc", ".pypirc", ".netrc", "credentials.json", "secrets.json"}
        and PurePosixPath(filename).suffix not in {".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx"}
    )


def editable_path(path: str) -> bool:
    return shareable_path(path) and not path.lower().startswith((".github/", ".gitmodules"))


class GitHub:
    def __init__(self, key: str) -> None:
        self.key = key

    def _read(self, repo: str, endpoint: str) -> dict[str, Any]:
        if not valid_repo(repo):
            raise ProviderError("Укажи репозиторий в формате владелец/репозиторий.")
        return request_json(f"https://api.github.com/repos/{repo}{endpoint}", self.key, headers=GITHUB_HEADERS)

    def tree(self, repo: str, ref: str | None, *, workspace: bool = False) -> dict[str, Any]:
        target = ref or self._read(repo, "").get("default_branch")
        if not isinstance(target, str) or not target or any(part in {".", ".."} for part in target.split("/")):
            raise ProviderError("Не удалось определить ветку GitHub.")
        commit = self._read(repo, f"/commits/{quote(target, safe='')}")
        commit_sha = sha(commit.get("sha"))
        obj = commit.get("commit")
        tree_info = obj.get("tree") if isinstance(obj, dict) else None
        tree_sha = sha(tree_info.get("sha") if isinstance(tree_info, dict) else None)
        tree = self._read(repo, f"/git/trees/{tree_sha}?recursive=1")
        files = tree.get("tree")
        if not isinstance(files, list):
            raise ProviderError("GitHub не вернул дерево файлов.")
        found: list[dict[str, Any]] = []
        for item in files:
            if not isinstance(item, dict) or item.get("type") != "blob" or item.get("mode") not in {"100644", "100755"}:
                continue
            path, size = item.get("path"), item.get("size")
            if not isinstance(path, str) or not shareable_path(path) or type(size) is not int or size < 0:
                continue
            if workspace:
                found.append({"path": path, "size": size, "blob": sha(item.get("sha")), "mode": item["mode"]})
            elif 0 < size <= MAX_FILE_BYTES:
                found.append({"path": path})
        found.sort(key=lambda entry: (entry["path"].count("/"), entry["path"].casefold(), entry["path"]))
        limit = MAX_WORKSPACE_PATHS if workspace else 800
        payload = ({"files": found[:limit], "tree_sha": tree_sha} if workspace
                   else {"paths": [item["path"] for item in found[:limit]]})
        return {"repo": repo, "ref": target, "commit": commit_sha, **payload,
                "truncated": len(found) > limit or bool(tree.get("truncated"))}

    def publish_branch(
        self, repo: str, commit: str, base_tree: str, branch: str, message: str, edits: list[dict[str, str]],
    ) -> dict[str, str]:
        if not self.key or not valid_repo(repo) or not re.fullmatch(r"bridge/[a-f0-9]{32}", branch):
            raise ProviderError("Недопустимая настройка публикации.")

        def create(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
            return request_json(f"https://api.github.com/repos/{repo}/git/{endpoint}", self.key, payload,
                                headers={**GITHUB_HEADERS, "X-GitHub-Api-Version": "2022-11-28"})

        tree = sha(create("trees", {"base_tree": sha(base_tree), "tree": edits}).get("sha"))
        head = sha(create("commits", {"message": message, "tree": tree, "parents": [sha(commit)]}).get("sha"))
        ref = f"refs/heads/{branch}"
        created = create("refs", {"ref": ref, "sha": head})
        obj = created.get("object")
        if created.get("ref") != ref or not isinstance(obj, dict) or obj.get("sha") != head:
            raise ProviderError("GitHub не подтвердил создание ветки. Проверь ветки перед повтором.")
        return {"branch": branch, "commit": head, "url": f"https://github.com/{repo}/tree/{branch}",
                "commit_url": f"https://github.com/{repo}/commit/{head}"}

    def blob(self, repo: str, blob: str) -> str:
        obj = self._read(repo, f"/git/blobs/{sha(blob)}")
        if obj.get("sha") != blob:
            raise ProviderError("GitHub вернул другой файл.")
        return self._text(obj, MAX_WORKSPACE_BYTES)

    def file(self, repo: str, commit: str, path: str) -> str:
        if not shareable_path(path):
            raise ProviderError("Файл нельзя передать в контекст.")
        obj = self._read(repo, f"/contents/{quote(path)}?ref={sha(commit)}")
        if obj.get("type") != "file":
            raise ProviderError(f"Выбери обычный текстовый файл: {path}")
        return self._text(obj, MAX_FILE_BYTES)

    @staticmethod
    def _text(obj: dict[str, Any], limit: int) -> str:
        size, content = obj.get("size"), obj.get("content")
        if (obj.get("encoding") != "base64" or type(size) is not int or not 0 <= size <= limit
                or not isinstance(content, str)):
            raise ProviderError("Файл слишком большой или недоступен для просмотра текста.")
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
            if len(decoded) > limit or b"\0" in decoded:
                raise ValueError
            return decoded.decode("utf-8")
        except (UnicodeError, binascii.Error, ValueError):
            raise ProviderError("В редакторе можно открыть только текстовые файлы в UTF-8.") from None

    @staticmethod
    def _pull_endpoint(repo: str, url: str) -> str:
        match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]{0,11})/?", url, re.ASCII)
        if not match or match[1].lower() != repo.lower():
            raise ProviderError("Ссылка на PR не относится к выбранному репозиторию.")
        return f"/pulls/{match[2]}"

    def pull_head(self, repo: str, url: str) -> tuple[str, str, str]:
        pr = self._read(repo, self._pull_endpoint(repo, url))
        head = pr.get("head")
        if not isinstance(head, dict):
            raise ProviderError("У PR нет доступной исходной ветки.")
        source = head.get("repo")
        source_repo = source.get("full_name") if isinstance(source, dict) else None
        if source_repo is None:
            source_repo = repo
        if not isinstance(source_repo, str) or not valid_repo(source_repo):
            raise ProviderError("GitHub вернул некорректный репозиторий PR.")
        commit = sha(head.get("sha"))
        label = head.get("label") or head.get("ref")
        return source_repo, commit, str(label or commit[:8])[:250]

    def pull_request(self, repo: str, url: str) -> dict[str, Any]:
        endpoint = self._pull_endpoint(repo, url)
        pr = self._read(repo, endpoint)
        head, base = pr.get("head"), pr.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ProviderError("GitHub не вернул ветки PR.")
        data = request_json(
            f"https://api.github.com/repos/{repo}{endpoint}/files?per_page=50",
            self.key, headers=GITHUB_HEADERS, list_items=True,
        )
        items = data.get("items")
        if not isinstance(items, list):
            raise ProviderError("GitHub не вернул файлы PR.")
        paths = [item["filename"] for item in items[:50] if isinstance(item, dict)
                 and isinstance(item.get("filename"), str) and len(item["filename"]) <= 320]
        total = pr.get("changed_files")
        return {
            "head": str(head.get("label") or head.get("ref") or "")[:250],
            "base": str(base.get("ref") or "")[:200],
            "files": paths,
            "total": total if type(total) is int else None,
        }


DEMO_FILES = {
    "README.md": (
        "# CSV Workbench\n\n"
        "A small Python project that converts CSV uploads into rows of named fields.\n\n"
        "## Project layout\n\n"
        "- `src/import_csv.py` reads an upload.\n"
        "- `src/validation.py` checks the columns.\n"
        "- `tests/test_import.py` covers empty and valid uploads.\n\n"
        "## Runtime\n\nPython 3.12 or later. No external dependencies.\n"
    ),
    "src/import_csv.py": (
        "from __future__ import annotations\n\n"
        "import csv\n"
        "from typing import TextIO\n\n"
        "from .validation import validate_columns\n\n\n"
        "def import_rows(stream: TextIO) -> list[dict[str, str]]:\n"
        '    """Read rows keyed by the CSV header."""\n'
        "    reader = csv.DictReader(stream)\n"
        "    if reader.fieldnames is None:\n"
        "        return []\n\n"
        "    validate_columns(reader.fieldnames)\n"
        "    return [row for row in reader]\n"
    ),
    "src/validation.py": (
        "def validate_columns(columns: list[str]) -> None:\n"
        "    if any(not column.strip() for column in columns):\n"
        '        raise ValueError("Column names cannot be blank")\n'
        "    if len(set(columns)) != len(columns):\n"
        '        raise ValueError("Column names must be unique")\n'
    ),
    "tests/test_import.py": (
        "from io import StringIO\n\n"
        "from src.import_csv import import_rows\n\n\n"
        "def test_empty_upload():\n"
        '    assert import_rows(StringIO("")) == []\n\n\n'
        "def test_named_columns():\n"
        '    stream = StringIO("item,quantity\\ncoffee,2\\n")\n'
        '    assert import_rows(stream) == [{"item": "coffee", "quantity": "2"}]\n'
    ),
    "tests/fixtures/orders.csv": "item,quantity\ncoffee,2\ntea,1\n",
    "docs/architecture.md": "# Import flow\n\nRead the header, validate the columns, then return the data rows.\n",
    "pyproject.toml": '[project]\nname = "csv-workbench"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
}
