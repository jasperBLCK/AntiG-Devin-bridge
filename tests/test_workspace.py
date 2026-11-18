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
AUTH = ("bridge", "demo-admin-for-tests")
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

    def test_open_workspace_does_not_dispatch_or_disappear_when_tasks_are_cleared(self):
        with self.client() as client, patch("providers.request_json") as providers, patch("repository.request_json") as gh:
            response = client.post("/api/workspaces", json={"repo": "demo/csv", "ref": "main"}, headers=WRITE)
            self.assertEqual(response.status_code, 201)
            job_id = response.json()["id"]
            self.assertEqual(client.get(f"/api/jobs/{job_id}").json()["mode"], "workspace")
            tree = self.get_tree(client, job_id).json()
            self.assertTrue(tree["demo"])
            self.assertFalse(tree["can_publish"])
            self.assertEqual(client.post("/api/jobs/clear", headers=WRITE).json(), {"cleared": 0})
            self.assertEqual(self.get_tree(client, job_id).status_code, 200)
            providers.assert_not_called()
            gh.assert_not_called()

    def test_authenticated_publication_only_creates_new_branch_from_signed_snapshot(self):
        settings = Settings(self.db, github_read_token="reader", admin_key=AUTH[1], github_write_token="writer",
                            github_write_repos=("test/repo",))
        reads = [
            {"sha": BASE, "commit": {"tree": {"sha": TREE}}},
            {"tree": [leaf("main.py", 7, "100755")]},
        ]
        with TestClient(create_app(settings, poll_seconds=0)) as client, patch("repository.request_json") as gh:
            client.auth = AUTH
            gh.side_effect = reads + reads
            opened = client.post("/api/workspaces", json={"repo": "test/repo", "ref": "main"}, headers=WRITE)
            self.assertEqual(opened.status_code, 201)
            job_id = opened.json()["id"]
            tree = self.get_tree(client, job_id).json()
            self.assertTrue(tree["can_publish"])
            edit = {"path": "main.py", "blob": BLOB, "stamp": tree["files"][0]["stamp"], "text": "print(2)\n"}
            url = f"/api/jobs/{job_id}/workspace/publish"
            body = {"commit": BASE, "message": "Update example", "edits": [edit]}
            self.assertEqual(client.post(url, json=body).status_code, 403)
            self.assertEqual(client.post(url, json={**body, "commit": HEAD}, headers=WRITE).status_code, 409)
            self.assertEqual(client.post(url, json={**body, "edits": [{**edit, "path": ".github/workflows/test.yml"}]},
                                         headers=WRITE).status_code, 422)
            self.assertEqual(client.post(url, json={**body, "edits": [{**edit, "text": "ghp_" + "A" * 24}]},
                                         headers=WRITE).status_code, 422)

            gh.side_effect = None

            def request(url, key, payload=None, **_):
                if url.endswith("/commits/" + BASE):
                    self.assertEqual(key, "reader")
                    return reads[0]
                if url.endswith("/git/trees/" + TREE + "?recursive=1"):
                    return reads[1]
                if url.endswith("/git/blobs/" + BLOB):
                    return blob(b"print(1)\n")
                self.assertEqual(key, "writer")
                if url.endswith("/git/trees"):
                    self.assertEqual(payload, {"base_tree": TREE, "tree": [
                        {"path": "main.py", "mode": "100755", "type": "blob", "content": "print(2)\n"},
                    ]})
                    return {"sha": "e" * 40}
                if url.endswith("/git/commits"):
                    self.assertEqual(payload, {"tree": "e" * 40, "parents": [BASE], "message": "Update example"})
                    return {"sha": HEAD}
                self.assertTrue(url.endswith("/git/refs"))
                self.assertRegex(payload["ref"], r"^refs/heads/bridge/[a-f0-9]{32}$")
                return {"ref": payload["ref"], "object": {"sha": HEAD}}

            gh.side_effect = request
            response = client.post(url, json=body, headers=WRITE)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["commit_url"], f"https://github.com/test/repo/commit/{HEAD}")
            self.assertEqual([call.args[0].split("/git/")[-1] for call in gh.call_args_list if len(call.args) == 3],
                             ["trees", "commits", "refs"])

    def test_repository_without_write_allowlist_cannot_publish(self):
        settings = Settings(self.db, admin_key=AUTH[1], github_write_token="writer")
        with TestClient(create_app(settings, poll_seconds=0)) as client, patch("repository.request_json") as gh:
            client.auth = AUTH
            gh.side_effect = [
                {"sha": BASE, "commit": {"tree": {"sha": TREE}}},
                {"tree": [leaf("main.py", 7)]},
            ]
            opened = client.post("/api/workspaces", json={"repo": "test/repo", "ref": "main"}, headers=WRITE)
            url = f"/api/jobs/{opened.json()['id']}/workspace/publish"
            body = {"commit": BASE, "message": "Update", "edits": [
                {"path": "main.py", "blob": BLOB, "stamp": "a" * 64, "text": "pass"}]}
            self.assertEqual(client.post(url, json=body, headers=WRITE).status_code, 403)
            self.assertEqual(gh.call_count, 2)

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
