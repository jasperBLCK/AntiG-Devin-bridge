import json
import os
import re
import socket
import sys
import tempfile
import unittest
from html import unescape
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

import configure_github as installer

REGISTRATION = {
    "slug": "mock-bridge",
    "client_id": "mock-client-id",
    "client_secret": "unused-mock",
    "owner": {"id": 42, "type": "User"},
    "pem": "unused-test-key",
    "webhook_secret": "unused-test-hook",
}


class LocalInstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bridge" / "github.json"

    def test_manifest_registration_checks_browser_and_state_then_saves_only_required_fields(self):
        with socket.socket() as sock:
            sock.bind((installer.HOST, 0))
            port = sock.getsockname()[1]

        def browser(url):
            with httpx.Client(base_url=url, trust_env=False) as client:
                self.assertEqual(client.get("/", headers={"Host": "example.test"}).status_code, 400)
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertIn("SameSite=Lax", page.headers["set-cookie"])
                action = urlsplit(unescape(re.search(r'action="([^"]+)"', page.text)[1]))
                self.assertEqual((action.scheme, action.netloc, action.path), ("https", "github.com", "/settings/apps/new"))
                state = parse_qs(action.query)["state"][0]
                manifest = json.loads(unescape(re.search(r'name="manifest" value="([^"]+)"', page.text)[1]))
                self.assertEqual(manifest["default_permissions"], {"contents": "write", "pull_requests": "read"})
                self.assertEqual(manifest["hook_attributes"], {"active": False})
                self.assertFalse(manifest["public"])
                self.assertEqual(manifest["callback_urls"], [url + "/api/github/callback"])
                self.assertEqual(manifest["setup_url"], url + "/api/github/setup")
                params = {"code": "mock-code", "state": state}
                route = urlsplit(manifest["redirect_url"]).path
                self.assertEqual(client.get(route, params={**params, "state": "wrong"}).status_code, 400)
                with httpx.Client(base_url=url, trust_env=False) as other:
                    self.assertEqual(other.get(route, params=params).status_code, 400)
                remote.assert_not_called()
                done = client.get(route, params=params)
                self.assertEqual(done.status_code, 200)
                self.assertNotIn(REGISTRATION["client_secret"], done.text)

        with patch("configure_github.open_browser", side_effect=browser), \
                patch("configure_github.request_json", return_value=REGISTRATION) as remote:
            config = installer.register(port, self.path)
            self.assertEqual(remote.call_args.args, (
                "https://api.github.com/app-manifests/mock-code/conversions", "", {},
            ))
            remote.assert_called_once()
        self.assertEqual(config.owner_id, 42)
        self.assertEqual(installer.load_config(self.path), config)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"slug", "client_id", "client_secret", "owner_id", "port"})
        self.assertNotIn(saved["client_secret"], repr(config))
        if os.name != "nt":
            self.assertEqual(S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_subsequent_run_does_not_register_and_prior_demo_configuration_is_not_reused(self):
        config = installer.LocalConfig("mock-bridge", "mock-client", "unused-mock", 42, 8001)
        installer.save_config(self.path, config)
        with patch.object(sys, "argv", ["configure_github.py"]), \
                patch.dict(os.environ, {"BRIDGE_MODE": "demo", "BRIDGE_REQUIRE_GITHUB_LOGIN": "0"}), \
                patch("configure_github.config_path", return_value=self.path), \
                patch("configure_github.prepare_python", return_value=Path(sys.executable)), \
                patch("configure_github.register") as register, \
                patch("configure_github.serve", return_value=0) as serve:
            self.assertEqual(installer.main(), 0)
            register.assert_not_called()
            serve.assert_called_once_with(config, Path(sys.executable))
            env = config.environment()
            self.assertNotIn("BRIDGE_MODE", env)
            self.assertEqual(env["BRIDGE_REQUIRE_GITHUB_LOGIN"], "1")
            self.assertEqual(env["BRIDGE_PUBLIC_ORIGIN"], "http://127.0.0.1:8001")
            self.assertEqual(env["BRIDGE_GITHUB_OWNER_ID"], "42")
        with self.assertRaises(installer.SetupError):
            installer.save_config(self.path, config)

    def test_malformed_registration_is_not_exchanged_or_saved(self):
        with patch("configure_github.request_json") as remote:
            with self.assertRaises(installer.SetupError):
                installer.exchange("../not-a-code", 8000)
            remote.assert_not_called()
        bad_owner = {**REGISTRATION, "owner": {"id": 42, "type": "Organization"}}
        with patch("configure_github.request_json", return_value=bad_owner), self.assertRaises(installer.SetupError):
            installer.exchange("mock-code", 8000)
