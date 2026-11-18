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

GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "AntiG-Devin-bridge"}


def sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ProviderError("GitHub не вернул корректный SHA.")
    return value


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


class GitHub:
    def __init__(self, key: str) -> None:
        self.key = key

    def _read(self, repo: str, endpoint: str) -> dict[str, Any]:
        if any(part in {".", ".."} for part in repo.split("/")):
            raise ProviderError("Укажи репозиторий в формате владелец/репозиторий.")
        return request_json(f"https://api.github.com/repos/{repo}{endpoint}", self.key, headers=GITHUB_HEADERS)

    def tree(self, repo: str, ref: str | None) -> dict[str, Any]:
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
        found = []
        for item in files:
            if not isinstance(item, dict) or item.get("type") != "blob" or item.get("mode") not in {"100644", "100755"}:
                continue
            path, size = item.get("path"), item.get("size")
            if isinstance(path, str) and shareable_path(path) and type(size) is int and 0 < size <= MAX_FILE_BYTES:
                found.append(path)
        found.sort(key=lambda path: (path.count("/"), path.lower()))
        return {"repo": repo, "ref": target, "commit": commit_sha, "paths": found[:800],
                "truncated": len(found) > 800 or bool(tree.get("truncated"))}

    def file(self, repo: str, commit: str, path: str) -> str:
        if not shareable_path(path):
            raise ProviderError("Файл нельзя передать в контекст.")
        obj = self._read(repo, f"/contents/{quote(path)}?ref={sha(commit)}")
        size, content = obj.get("size"), obj.get("content")
        if (obj.get("type") != "file" or obj.get("encoding") != "base64"
                or type(size) is not int or size > MAX_FILE_BYTES or not isinstance(content, str)):
            raise ProviderError(f"Файл не является небольшим текстовым файлом: {path}")
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
            if len(decoded) > MAX_FILE_BYTES or b"\0" in decoded:
                raise ValueError
            return decoded.decode("utf-8")
        except (UnicodeError, binascii.Error, ValueError):
            raise ProviderError(f"Файл должен быть текстом в UTF-8: {path}") from None


DEMO_FILES = {
    "README.md": "# Demo project\nA small CSV importer. The first row contains the column names.\n",
    "src/import_csv.py": "import csv\n\ndef import_rows(stream):\n    return list(csv.DictReader(stream))\n",
    "tests/test_import.py": "def test_empty_upload():\n    # Add a regression test here.\n    pass\n",
}
