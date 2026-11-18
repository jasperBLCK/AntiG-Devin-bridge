import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import Settings, create_app

WRITE = {"X-Bridge-Request": "1"}
BASE = "a" * 40
HEAD = "b" * 40
TREE = "c" * 40
BLOB = "d" * 40
PR = "https://github.com/test/repo/pull/4"


def routed_answer():
    return {"choices": [{"message": {"content": json.dumps({
        "route": "answer", "reason": "No code changes needed", "content": "Done",
    })}}]}


def leaf(path, size, mode="100644"):
    return {"type": "blob", "path": path, "sha": BLOB, "size": size, "mode": mode}


def blob(text):
    return {"sha": BLOB, "size": len(text), "encoding": "base64", "content": base64.b64encode(text).decode()}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "workspace.sqlite3"

    def client(self, demo=True):
        return TestClient(create_app(Settings(self.db, demo=demo, antig_key="mock-antig"), poll_seconds=0))

    def job(self, client, repo="test/repo", **updates):
        result = client.post("/api/jobs", json={"prompt": "Explain the project", "repo": repo}, headers=WRITE)
        self.assertEqual(result.status_code, 202)
        job_id = result.json()["id"]
        if updates:
            with closing(sqlite3.connect(self.db)) as sql, sql:
                sql.execute("UPDATE jobs SET ref = ?, commit_sha = ?, pr_url = ? WHERE id = ?",
                            (updates.get("ref"), updates.get("commit_sha"), updates.get("pr_url"), job_id))
        return job_id

    @staticmethod
    def get_tree(client, job_id, revision="base"):
        return client.get(f"/api/jobs/{job_id}/workspace/tree", params={"revision": revision})

    @staticmethod
    def file_args(tree, entry):
        return {"repo": tree["repo"], "commit": tree["commit"],
                **{key: entry[key] for key in ("path", "blob", "stamp")}}

    @staticmethod
    def get_file(client, job_id, args):
        return client.get(f"/api/jobs/{job_id}/workspace/file", params=args)

    def test_demo_workspace_serves_editor_and_immutable_files_without_provider_calls(self):
        with self.client() as client, patch("providers.request_json") as providers, patch("repository.request_json") as gh:
            job_id = self.job(client)
            page = client.get(f"/workspace/{job_id}")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Проводник", page.text)
            self.assertIn("Только чтение", page.text)
            self.assertIn("/static/workspace.js", page.text)
            tree = self.get_tree(client, job_id).json()
            entry = next(entry for entry in tree["files"] if entry["path"] == "src/import_csv.py")
            response = self.get_file(client, job_id, self.file_args(tree, entry))
            self.assertEqual(response.status_code, 200)
            self.assertIn("def import_rows", response.json()["text"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            gh.assert_not_called()
            providers.assert_not_called()

    def test_base_snapshot_uses_saved_commit_and_reads_signed_git_blob_not_a_branch(self):
        with (self.client(demo=False) as client,
              patch("providers.request_json", return_value=routed_answer()) as providers,
              patch("repository.request_json") as gh):
            job_id = self.job(client, ref="release", commit_sha=BASE)
            gh.side_effect = [
                {"sha": BASE, "commit": {"tree": {"sha": TREE}}},
                {"tree": [leaf("main.py", 20), leaf("empty.txt", 0), leaf("big.txt", 300_000),
                          leaf(".env", 2), leaf("alias.py", 5, "120000")], "truncated": True},
                blob(b'<script>alert("not HTML")</script>'),
            ]
            tree = self.get_tree(client, job_id).json()
            self.assertEqual((tree["ref"], tree["commit"], tree["truncated"]), ("release", BASE, True))
            paths = {entry["path"]: entry for entry in tree["files"]}
            self.assertEqual(set(paths), {"main.py", "empty.txt", "big.txt"})
            self.assertNotIn("stamp", paths["big.txt"])
            self.assertIn("stamp", paths["empty.txt"])
            response = self.get_file(client, job_id, self.file_args(tree, paths["main.py"]))
            self.assertEqual(response.json()["text"], '<script>alert("not HTML")</script>')
            self.assertTrue(response.headers["content-type"].startswith("application/json"))
            self.assertTrue(gh.call_args_list[0].args[0].endswith("/commits/" + BASE))
            self.assertTrue(gh.call_args.args[0].endswith("/git/blobs/" + BLOB))
            providers.assert_called_once()

    def test_file_signature_cannot_be_reused_for_another_job_repository_path_or_commit(self):
        with self.client() as client:
            job_id, other_id = self.job(client), self.job(client)
            tree = self.get_tree(client, job_id).json()
            params = self.file_args(tree, tree["files"][0])
            self.assertEqual(self.get_file(client, other_id, params).status_code, 409)
            for field, value in (("repo", "other/repo"), ("path", ".env"), ("commit", BASE), ("blob", HEAD)):
                self.assertEqual(self.get_file(client, job_id, {**params, field: value}).status_code, 409)

    def test_pr_head_is_taken_from_github_and_supports_a_fork(self):
        with (self.client(demo=False) as client, patch("providers.request_json", return_value=routed_answer()),
              patch("repository.request_json") as gh):
            job_id = self.job(client, pr_url=PR)
            gh.side_effect = [
                {"head": {"repo": {"full_name": "contributor/repo"}, "label": "contributor:fix/csv", "sha": HEAD}},
                {"sha": HEAD, "commit": {"tree": {"sha": TREE}}},
                {"tree": [leaf("tests/test_csv.py", 15)]},
                blob(b"def test_csv():\n    pass\n"),
            ]
            tree = self.get_tree(client, job_id, "pr").json()
            self.assertEqual((tree["repo"], tree["ref"], tree["commit"]),
                             ("contributor/repo", "contributor:fix/csv", HEAD))
            result = self.get_file(client, job_id, self.file_args(tree, tree["files"][0]))
            self.assertEqual(result.status_code, 200)
            self.assertTrue(gh.call_args_list[0].args[0].endswith("/test/repo/pulls/4"))
            self.assertTrue(gh.call_args_list[1].args[0].endswith("/contributor/repo/commits/" + HEAD))
            self.assertTrue(gh.call_args.args[0].endswith("/contributor/repo/git/blobs/" + BLOB))

    def test_workspace_missing_repo_and_invalid_pr_do_not_call_github(self):
        with (self.client(demo=False) as client, patch("providers.request_json", return_value=routed_answer()),
              patch("repository.request_json") as gh):
            no_repo = self.job(client, repo=None)
            self.assertEqual(self.get_tree(client, no_repo).status_code, 422)
            invalid = self.job(client, pr_url="https://github.com/other/repo/pull/4")
            self.assertEqual(self.get_tree(client, invalid, "pr").status_code, 502)
            self.assertEqual(client.get("/workspace/" + "0" * 32).status_code, 404)
            gh.assert_not_called()

    def test_binary_blob_returns_an_explanation_instead_of_source(self):
        with (self.client(demo=False) as client, patch("providers.request_json", return_value=routed_answer()),
              patch("repository.request_json") as gh):
            job_id = self.job(client, ref="main")
            gh.side_effect = [{"sha": BASE, "commit": {"tree": {"sha": TREE}}},
                              {"tree": [leaf("logo.png", 2)]}, blob(b"\x00\xff")]
            tree = self.get_tree(client, job_id).json()
            response = self.get_file(client, job_id, self.file_args(tree, tree["files"][0]))
            self.assertEqual(response.status_code, 422)
            self.assertIn("UTF-8", response.json()["detail"])
