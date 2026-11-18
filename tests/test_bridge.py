import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import Settings, create_app
from providers import AntiG, ProviderError, collect_chat_stream, https_url

WRITE = {"X-Bridge-Request": "1"}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "tasks.sqlite3"

    def client(self, demo=False, live=False):
        settings = Settings(self.path, demo, "mock-antig" if live else "",
                            "mock-devin" if live else "", "org-mock" if live else "")
        return TestClient(create_app(settings))

    @staticmethod
    def submit(client, **params):
        return client.post("/api/jobs", json=params, headers=WRITE)

    @staticmethod
    def get(client, run):
        return client.get(f"/api/jobs/{run.json()['id']}").json()

    @staticmethod
    def refresh(client, run):
        return client.post(f"/api/jobs/{run.json()['id']}/refresh", json={}, headers=WRITE).json()

    def test_grok_handoff_is_manual_and_preserves_context(self):
        with self.client(live=True) as client, patch("providers.request_json") as request:
            request.side_effect = [
                {"choices": [{"message": {"content": "A plan with tests"}}]},
                {"session_id": "devin-abc", "status": "new", "pull_requests": []},
                {"session_id": "devin-abc", "status": "exit", "pull_requests": [
                    {"pr_url": "https://github.com/test/repo/pull/1"},
                ]},
            ]
            plan = self.submit(client, mode="grok", prompt="Add CSV import")
            self.assertEqual(plan.status_code, 202)
            self.assertEqual(self.get(client, plan)["result"], "A plan with tests")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[2]["model"], "grok-4.7")
            self.assertTrue(request.call_args.args[2]["stream"])
            self.assertEqual(request.call_args.kwargs["stream_model"], "grok-4.7")

            run = self.submit(client, mode="devin", prompt="Implement imports now",
                              repo="test/repo", max_acu=4, source_id=plan.json()["id"])
            self.assertEqual(run.status_code, 202)
            target, token, payload = request.call_args.args
            self.assertEqual(target, "https://api.devin.ai/v3/organizations/org-mock/sessions")
            self.assertEqual(token, "mock-devin")
            self.assertEqual(payload["max_acu_limit"], 4)
            self.assertEqual(payload["repos"], ["test/repo"])
            for detail in ("Implement imports now", "Add CSV import", "A plan with tests"):
                self.assertIn(detail, payload["prompt"])
            self.assertNotIn("bypass_approval", payload)
            self.assertEqual(self.get(client, run)["status"], "running")
            result = self.refresh(client, run)
            self.assertEqual(result["status"], "ended")
            self.assertEqual(result["pr_url"], "https://github.com/test/repo/pull/1")

    def test_image_and_async_video(self):
        with self.client(live=True) as client, patch("providers.request_json") as request:
            request.side_effect = [
                {"data": [{"url": "https://media.example/image.webp"}]},
                {"request_id": "uuid-123"},
                {"status": "pending", "progress": 42},
                {"status": "done", "video": {"url": "https://media.example/video.mp4"}},
            ]
            image = self.submit(client, mode="image", prompt="A mountain")
            self.assertEqual(self.get(client, image)["media_url"], "https://media.example/image.webp")
            self.assertEqual(request.call_args.args[0], "https://grok.antig.ru/v1/images/generations")
            self.assertEqual(request.call_args.args[2]["model"], "grok-imagine-image-2.0")
            video = self.submit(client, mode="video", prompt="A mountain at dawn")
            self.assertEqual(self.get(client, video)["remote_id"], "uuid-123")
            self.assertEqual(request.call_args.args[0], "https://grok.antig.ru/v1/videos/generations")
            self.assertEqual(self.refresh(client, video)["progress"], 42)
            result = self.refresh(client, video)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["media_url"], "https://media.example/video.mp4")
            self.assertEqual(request.call_args.args[0], "https://grok.antig.ru/v1/videos/uuid-123")

    def test_demo_and_persistence(self):
        with self.client(demo=True) as client, patch("providers.request_json") as request:
            self.assertIn("От идеи к PR", client.get("/").text)
            self.assertTrue(client.get("/api/config").json()["demo"])
            plan = self.submit(client, mode="grok", prompt="Проверить демо")
            image = self.submit(client, mode="image", prompt="Проверить пример")
            self.assertEqual(self.get(client, image)["media_url"], "/static/sample.svg")
            run = self.submit(client, mode="devin", prompt="Реализовать", source_id=plan.json()["id"])
            self.assertEqual(self.refresh(client, run)["status"], "done")
            request.assert_not_called()
        with self.client(demo=True) as client:
            self.assertEqual(len(client.get("/api/jobs").json()), 3)

    def test_invalid_requests_never_contact_providers(self):
        with self.client(live=True) as client, patch("providers.request_json") as request:
            self.assertEqual(client.post("/api/jobs", json={"mode": "grok", "prompt": "test"}).status_code, 403)
            self.assertEqual(self.submit(client, mode="grok", prompt=" ").status_code, 422)
            self.assertEqual(self.submit(client, mode="image", model="grok-4.7", prompt="test").status_code, 422)
            self.assertEqual(self.submit(client, mode="devin", max_acu=0, prompt="test").status_code, 422)
            self.assertEqual(self.submit(client, mode="devin", source_id="0"*32, prompt="test").status_code, 404)
            self.assertEqual(client.get("/api/jobs").json(), [])
            request.assert_not_called()

    def test_provider_error_is_saved_without_retry(self):
        with self.client(live=True) as client, patch("providers.request_json", side_effect=ProviderError("HTTP 502")) as request:
            run = self.submit(client, mode="grok", prompt="Plan")
            self.assertEqual(self.get(client, run)["status"], "failed")
            self.assertEqual(self.refresh(client, run)["error"], "HTTP 502")
            self.assertEqual(request.call_count, 1)

    def test_live_without_keys_is_disabled(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/config").json()["antig"], False)
            self.assertEqual(self.submit(client, mode="grok", prompt="Plan").status_code, 503)

    def test_untrusted_media_can_only_be_https(self):
        for url in ("javascript:alert(1)", "file:///tmp/img.png", "https://name:pass@host/", "https://["):
            self.assertEqual(https_url(url), "")
        with (patch("providers.request_json", return_value={"data": [{"url": "javascript:alert(1)"}]}),
              self.assertRaises(ProviderError)):
            AntiG("mock-antig").image("test", "grok-imagine-image")

    def test_stream_plan_merges_content_without_reasoning(self):
        stream = (
            b'data: {"choices":[{"delta":{"reasoning_content":"not shown"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"First "}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"step."}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        self.assertEqual(collect_chat_stream(stream)["choices"][0]["message"]["content"], "First step.")
