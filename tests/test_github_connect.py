import base64
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app import Settings, create_app
from github_connect import GitHubApp

ORIGIN = "https://bridge.example.com"
WRITE = {"X-Bridge-Request": "1"}
AUTH = ("bridge", "mock-admin")
TOKEN = "ghu_mock-test-token"
APP = GitHubApp("bridge-mock", "mock-client", "mock-secret", ORIGIN)
SHA, TREE, BLOB, HEAD, NEW_TREE = (digit * 40 for digit in "abcde")


class GitHubConnectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(Path(directory.name) / "bridge.sqlite3", admin_key=AUTH[1], github_app=APP,
                                 github_read_token="unused-reader", github_write_token="unused-writer",
                                 github_write_repos=("owner/unselected",))

    def client(self, settings=None):
        client = TestClient(create_app(settings or self.settings, poll_seconds=0), base_url=ORIGIN,
                            follow_redirects=False)
        client.auth = AUTH if (settings or self.settings).admin_key else None
        return client

    def github(self, url, key, payload=None, **kwargs):
        if url == "https://github.com/login/oauth/access_token":
            self.assertEqual(key, "")
            self.assertEqual(payload["code"], "example-code")
            self.assertEqual(payload["redirect_uri"], APP.callback)
            self.assertEqual(payload["client_secret"], APP.client_secret)
            self.assertEqual(payload["client_id"], APP.client_id)
            self.assertTrue(kwargs["form"])
            return {"access_token": TOKEN, "expires_in": 3600}
        self.assertEqual(key, TOKEN)
        if url.endswith("/user"):
            return {"login": "bridge-user"}
        if url.endswith("/user/installations?per_page=100&page=1"):
            return {"installations": [{"id": 10}]}
        if url.endswith("/user/installations/10/repositories?per_page=100&page=1"):
            return {"repositories": [{"full_name": "owner/selected"}]}
        self.fail(f"Unexpected endpoint: {url}")

    def start(self, client):
        opened = client.post("/api/github/start?target=workspace", headers=WRITE)
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertIn("HttpOnly", opened.headers["set-cookie"])
        self.assertIn("Secure", opened.headers["set-cookie"])
        self.assertIn("SameSite=lax", opened.headers["set-cookie"])
        url = urlsplit(opened.json()["url"])
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "github.com", "/login/oauth/authorize"))
        params = parse_qs(url.query)
        self.assertEqual(params["redirect_uri"], [APP.callback])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertNotIn("scope", params)
        return params

    def authorize(self, client, params):
        return client.get("/api/github/callback", params={"state": params["state"][0], "code": "example-code"})

    def test_oauth_state_is_bound_to_browser_single_use_and_pkce_is_verified(self):
        with self.client() as client, patch("github_connect.request_json", side_effect=self.github) as remote:
            self.assertEqual(client.post("/api/github/start?target=workspace").status_code, 403)
            self.assertEqual(client.post("/api/github/start?target=//example.org", headers=WRITE).status_code, 422)
            params = self.start(client)
            other = TestClient(client.app, base_url=ORIGIN, follow_redirects=False)
            self.addCleanup(other.close)
            other.auth = AUTH
            self.assertIsNone(other.get("/api/github/status").json()["login"])
            self.assertEqual(self.authorize(other, params).status_code, 400)
            self.assertEqual(client.get("/api/github/callback?state=unexpected&code=example-code").status_code, 400)
            remote.assert_not_called()
            done = self.authorize(client, params)
            self.assertEqual(done.status_code, 303)
            self.assertEqual(done.headers["location"], "/workspace?github=connected")
            self.assertNotIn(TOKEN, str(done.headers))
            verifier = remote.call_args_list[0].args[2]["code_verifier"]
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            self.assertEqual(params["code_challenge"], [expected])
            self.assertEqual(client.get("/api/github/status").json(), {
                "enabled": True, "login": "bridge-user",
                "install": "https://github.com/apps/bridge-mock/installations/new",
            })
            self.assertIsNone(other.get("/api/github/status").json()["login"])
            self.assertEqual(self.authorize(client, params).status_code, 400)

    def test_installation_callback_cannot_grant_access_and_disconnect_blocks_cached_access(self):
        with self.client() as client, patch("github_connect.request_json", side_effect=self.github) as remote, \
                patch("repository.request_json") as repo_api:
            payload = {"repo": "owner/unselected", "ref": "main"}
            self.assertEqual(client.get("/api/github/setup?installation_id=111").status_code, 303)
            self.assertEqual(client.post("/api/workspaces", json=payload, headers=WRITE).status_code, 401)
            remote.assert_not_called()
            self.authorize(client, self.start(client))
            self.assertEqual(client.get("/api/github/repos").json(), {"repos": ["owner/selected"], "partial": False})
            self.assertEqual(client.post("/api/repo/tree", json=payload, headers=WRITE).status_code, 403)
            self.assertEqual(client.post("/api/workspaces", json=payload, headers=WRITE).status_code, 403)
            self.assertEqual(client.post("/api/github/disconnect").status_code, 403)
            self.assertEqual(client.post("/api/github/disconnect", headers=WRITE).json(), {"ok": True})
            self.assertEqual(client.get("/api/github/repos").status_code, 401)
            self.assertEqual(client.post("/api/workspaces", json={"repo": "owner/selected"}, headers=WRITE).status_code,
                             401)
            repo_api.assert_not_called()

    def test_denied_consent_is_safe_and_does_not_exchange_a_code(self):
        with self.client() as client, patch("github_connect.request_json") as remote:
            params = self.start(client)
            done = client.get("/api/github/callback", params={"state": params["state"][0], "error": "access_denied"})
            self.assertEqual(done.headers["location"], "/workspace?github=cancelled")
            self.assertIsNone(client.get("/api/github/status").json()["login"])
            self.assertEqual(self.authorize(client, params).status_code, 400)
            remote.assert_not_called()

    def test_oauth_token_is_used_for_reads_and_only_creates_new_branch(self):
        def repository(url, key, payload=None, **kwargs):
            self.assertEqual(key, TOKEN)
            if "/commits/" in url:
                return {"sha": SHA, "commit": {"tree": {"sha": TREE}}}
            if "recursive=1" in url:
                return {"tree": [{"type": "blob", "mode": "100644", "path": "main.py", "sha": BLOB, "size": 3}]}
            if url.endswith("/git/blobs/" + BLOB):
                return {"sha": BLOB, "encoding": "base64", "size": 3, "content": "eD0x"}
            if url.endswith("/git/trees"):
                return {"sha": NEW_TREE}
            if url.endswith("/git/commits"):
                self.assertEqual(payload["parents"], [SHA])
                return {"sha": HEAD}
            self.assertTrue(url.endswith("/git/refs"))
            self.assertRegex(payload["ref"], r"^refs/heads/bridge/[a-f0-9]{32}$")
            return {"ref": payload["ref"], "object": {"sha": HEAD}}

        with self.client() as client, patch("github_connect.request_json", side_effect=self.github), \
                patch("repository.request_json", side_effect=repository) as repo_api:
            self.authorize(client, self.start(client))
            opened = client.post("/api/workspaces", json={"repo": "owner/selected", "ref": "main"}, headers=WRITE)
            self.assertEqual(opened.status_code, 201)
            url = f"/api/jobs/{opened.json()['id']}/workspace"
            tree = client.get(url + "/tree").json()
            self.assertTrue(tree["can_publish"])
            entry = {key: tree["files"][0][key] for key in ("path", "blob", "stamp")}
            read = client.get(url + "/file", params={**entry, "repo": "owner/selected", "commit": SHA})
            self.assertEqual(read.json()["text"], "x=1")
            body = {"commit": SHA, "message": "Change sample", "edits": [{**entry, "text": "x=2"}]}
            published = client.post(url + "/publish", json=body, headers=WRITE)
            self.assertEqual(published.status_code, 201)
            self.assertRegex(published.json()["branch"], "^bridge/[a-f0-9]{32}$")
            self.assertNotIn(TOKEN, str(published.content))
            writes = [call.args[0].rsplit("/", 1)[-1] for call in repo_api.call_args_list if len(call.args) == 3]
            self.assertEqual(writes, ["trees", "commits", "refs"])
            client.post("/api/github/disconnect", headers=WRITE)
            self.assertEqual(client.post(url + "/publish", json=body, headers=WRITE).status_code, 401)
            self.assertEqual(client.get(url + "/tree").status_code, 401)

    def test_requires_admin_and_is_disabled_in_demo(self):
        for settings in (replace(self.settings, admin_key=""), replace(self.settings, demo=True),
                         replace(self.settings, github_app=replace(APP, origin="http://example.com"))):
            with self.subTest(settings=settings.github_app.origin), self.client(settings) as client:
                self.assertFalse(client.get("/api/github/status").json()["enabled"])
                self.assertEqual(client.post("/api/github/start?target=workspace", headers=WRITE).status_code, 503)
