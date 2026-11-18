from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any, Literal

from providers import ProviderError
from repository import DEMO_FILES, MAX_WORKSPACE_BYTES, GitHub

Revision = Literal["base", "pr"]


def digest(text: str) -> str:
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


class Workspace:
    def __init__(self, github: GitHub, demo: bool) -> None:
        self.github = github
        self.demo = demo
        self._key = secrets.token_bytes(32)

    def _stamp(self, job_id: str, repo: str, commit: str, path: str, blob: str) -> str:
        data = json.dumps([job_id, repo, commit, path, blob], ensure_ascii=False).encode()
        return hmac.digest(self._key, data, "sha256").hex()

    def tree(self, job: dict[str, Any], revision: Revision) -> dict[str, Any]:
        repo, pr = job["repo"], job["pr_url"]
        if revision == "pr" and (not pr or self.demo):
            raise ProviderError("У задачи пока нет доступной ветки PR.")
        ref, pinned = job["ref"], job["commit_sha"]
        if revision == "pr":
            repo, pinned, ref = self.github.pull_head(repo, pr)
        if self.demo:
            result: dict[str, Any] = {
                "repo": repo, "ref": ref or "main", "commit": pinned or "0" * 40, "truncated": False,
                "files": [
                    {"path": path, "size": len(text.encode()), "blob": digest(text)}
                    for path, text in DEMO_FILES.items()
                ],
            }
        else:
            result = self.github.tree(repo, pinned or ref, workspace=True)
            if pinned:
                result["ref"] = ref or pinned[:8]
        files = []
        for entry in result["files"]:
            path, size, blob = entry["path"], entry["size"], entry["blob"]
            if size <= MAX_WORKSPACE_BYTES:
                stamp = self._stamp(job["id"], repo, result["commit"], path, blob)
                files.append({"path": path, "size": size, "blob": blob, "stamp": stamp})
            else:
                files.append({"path": path, "size": size})
        return {**result, "files": files}

    def allowed(self, job_id: str, repo: str, commit: str, path: str, blob: str, stamp: str) -> bool:
        return hmac.compare_digest(stamp, self._stamp(job_id, repo, commit, path, blob))

    def file(self, repo: str, path: str, blob: str) -> str:
        return DEMO_FILES[path] if self.demo else self.github.blob(repo, blob)
