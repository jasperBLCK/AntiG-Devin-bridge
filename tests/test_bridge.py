import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import Settings, create_app
from orchestration import MAX_PLAN_CHARS
from providers import AntiG, ProviderError, collect_chat_stream, https_url
from repository import GitHub

WRITE = {"X-Bridge-Request": "1"}
PR = "https://github.com/test/repo/pull/1"


def chat(text):
    return {"choices": [{"message": {"content": text}}]}


def route(name, text="Check the behavior and run the tests."):
    return chat(json.dumps({"route": name, "reason": "Task-specific reason", "content": text}))


def message(event_id, text, at, source="devin"):
    return {"event_id": event_id, "message": text, "created_at": at, "source": source}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "tasks.sqlite3"

    def client(self, demo=False, live=False, poll=0):
        settings = Settings(self.path, demo, "mock-antig" if live else "",
                            "mock-devin" if live else "", "org-mock" if live else "")
        return TestClient(create_app(settings, poll_seconds=poll))

    @staticmethod
    def submit(client, **params):
        return client.post("/api/jobs", json=params, headers=WRITE)

    @staticmethod
    def get(client, run):
        return client.get(f"/api/jobs/{run.json()['id']}").json()

    @staticmethod
    def action(client, run, action):
        return client.post(f"/api/jobs/{run.json()['id']}/{action}", json={}, headers=WRITE)

    def refresh(self, client, run):
        return self.action(client, run, "refresh").json()

    def test_route_answers_without_launching_devin(self):
        with self.client(live=True) as client, patch("providers.request_json", return_value=route("answer", "Answer")) as api:
            task = self.submit(client, prompt="Compare CSV parsers", repo="test/repo")
            self.assertEqual(task.status_code, 202)
            job = self.get(client, task)
            self.assertEqual((job["route"], job["status"], job["result"]), ("answer", "done", "Answer"))
            api.assert_called_once()
            self.assertEqual(api.call_args.args[2]["model"], "grok-4.7")

    def test_original_goal_reaches_both_agents_separately_from_bounded_plan(self):
        request = "Собери 3D-витрину реквизита.\nВидео — только на фоне; без отдельного плеера."
        plan = "Сделать предложенные изменения. " * 100
        with self.client(live=True) as client, patch("providers.request_json") as api:
            api.side_effect = [route("implement", plan), {"session_id": "devin-sample", "status": "new"}]
            run = self.submit(client, prompt=request, repo="test/repo")
            self.assertEqual(run.status_code, 202)
            messages = api.call_args_list[0].args[2]["messages"]
            self.assertEqual(json.loads(messages[-1]["content"])["request"], request)
            self.assertIn("не подменяй цель", messages[0]["content"])
            handoff = api.call_args.args[2]["prompt"]
            instructions, context = handoff.split("\n\nВходные данные:\n", 1)
            self.assertIn("Пропускай шаги, которые меняют цель", instructions)
            self.assertIn("уточни это вместо подмены", instructions)
            expected_plan = plan[:MAX_PLAN_CHARS].rstrip()
            self.assertEqual(json.loads(context), {"request": request, "grok_plan": expected_plan})
            job = self.get(client, run)
            self.assertEqual((job["prompt"], job["plan"]), (request, expected_plan))

    def test_clarification_does_not_launch_an_alternative_task(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            question = "Оставить витрину статической или подключить каталог?"
            api.return_value = route("clarify", question)
            run = self.submit(client, prompt="Обнови витрину", repo="test/repo")
            job = self.get(client, run)
            self.assertEqual((job["route"], job["status"], job["result"]), ("clarify", "done", question))
            api.assert_called_once()

    def test_overview_totals_reported_usage_without_double_counting_or_faking_missing_values(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            fresh = client.get("/api/overview").json()
            self.assertIsNone(fresh["grok"]["total_tokens"])
            self.assertIsNone(fresh["devin"]["acu"])
            self.assertEqual(fresh["limits"]["default_acu"], 5)
            api.side_effect = [
                {**route("implement"), "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 41}},
                {"session_id": "devin-usage", "status": "new"},
                {"status": "running", "acus_consumed": 0}, {"items": []},
                {"status": "running", "acus_consumed": 0.25}, {"items": []},
                {"status": "exit", "structured_output": {"summary": "Fixed."}}, {"items": []},
                {**chat("Reviewed"), "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
                route("answer"),
            ]
            run = self.submit(client, prompt="Fix CSV", repo="test/repo", review=True, max_acu=3)
            self.assertEqual(self.get(client, run)["max_acu"], 3)
            self.assertEqual(self.refresh(client, run)["devin_acu"], 0)
            self.assertEqual(client.get("/api/overview").json()["devin"]["measured"], 1)
            self.assertEqual(self.refresh(client, run)["devin_acu"], 0.25)
            self.assertEqual(self.refresh(client, run)["devin_acu"], 0.25)
            self.submit(client, prompt="Compare CSV parsers")
            final = client.get("/api/overview").json()
            self.assertEqual(final["grok"], {"model": "grok-4.7", "input_tokens": 50, "output_tokens": 15,
                                             "total_tokens": 66, "responses": 3, "measured": 2})
            self.assertEqual(final["devin"], {"acu": 0.25, "measured": 1, "sessions": 1})
            self.assertEqual(final["jobs"]["finished"], 2)
        with self.client(live=True) as client:
            self.assertEqual(client.get("/api/overview").json()["grok"]["total_tokens"], 66)
        with self.client(demo=True) as client:
            demo = client.get("/api/overview").json()
            self.assertTrue(demo["demo"])
            self.assertIsNone(demo["grok"]["total_tokens"])
            self.assertIsNone(demo["devin"]["acu"])

    def test_stream_takes_last_usage_snapshot_instead_of_adding_chunks(self):
        stream = b'\n'.join((
            b'data: {"usage":{"prompt_tokens":9,"completion_tokens":1,"total_tokens":10},"choices":[{"delta":{"content":"A"}}]}',
            b'data: {"usage":{"prompt_tokens":9,"completion_tokens":2,"total_tokens":11},"choices":[]}',
            b'data: {"usage":null,"choices":[]}',
            b'data: [DONE]',
        ))
        with patch("providers.request_json", return_value=collect_chat_stream(stream)):
            content, usage = AntiG("mock").text("prompt", "system")
            self.assertEqual(content, "A")
            self.assertEqual((usage.input_tokens, usage.output_tokens, usage.total_tokens), (9, 2, 11))

    def test_devin_starts_automatically_and_grok_reviews_opted_in_report(self):
        with (self.client(live=True) as client, patch("providers.request_json") as api,
              patch("repository.request_json") as github):
            api.side_effect = [
                route("implement"),
                {"session_id": "devin-abc", "status": "new"},
                {"status": "running", "status_detail": "working"},
                {"items": [message("m1", "Reading src/import.py", 100)], "end_cursor": "next/page"},
                {"status": "running", "status_detail": "finished", "structured_output": {
                    "summary": "Implemented CSV import.", "checks": "Tests passed.", "remaining": "None.",
                }, "pull_requests": [{"pr_url": PR}]},
                {"items": [message("m1", "Reading src/import.py", 100), message("m2", "RAW MESSAGE", 200),
                           message("m3", "USER MESSAGE", 300, "user")]},
                chat("Verify large files separately."),
            ]
            github.side_effect = [
                {"head": {"label": "test:fix/csv"}, "base": {"ref": "release"}, "changed_files": 1},
                {"items": [{"filename": "src/import.py", "patch": "RAW DIFF"}]},
            ]
            run = self.submit(client, prompt="Fix import", repo="test/repo", ref="release", max_acu=4,
                              draft_pr=True, review=True)
            self.assertEqual(run.status_code, 202)
            job = self.get(client, run)
            self.assertEqual((job["route"], job["status"]), ("implement", "running"))
            target, token, payload = api.call_args.args
            self.assertEqual(target, "https://api.devin.ai/v3/organizations/org-mock/sessions")
            self.assertEqual(token, "mock-devin")
            self.assertEqual(payload["repos"], ["test/repo"])
            self.assertEqual(payload["max_acu_limit"], 4)
            self.assertIn("draft PR", payload["prompt"])
            self.assertNotIn("bypass_approval", payload)
            self.assertTrue(payload["structured_output_required"])

            active = self.refresh(client, run)
            self.assertEqual(active["activity"]["messages"][0]["text"], "Reading src/import.py")
            self.assertEqual(active["activity"]["detail"], "working")
            self.assertNotIn("cursor", active["activity"])
            final = self.refresh(client, run)
            self.assertEqual(final["status"], "ended")
            self.assertEqual(final["activity"]["pr"]["head"], "test:fix/csv")
            self.assertEqual(final["activity"]["pr"]["files"], ["src/import.py"])
            self.assertEqual(final["pr_url"], PR)
            self.assertEqual([entry["id"] for entry in final["activity"]["messages"]], ["m1", "m2"])
            self.assertIn("after=next%2Fpage", api.call_args_list[-2].args[0])
            review = api.call_args.args[2]["messages"][-1]["content"]
            self.assertIn("Tests passed.", review)
            for content in ("RAW DIFF", "RAW MESSAGE", "USER MESSAGE"):
                self.assertNotIn(content, review)
            self.assertTrue(final["reviewed_at"])
            self.assertEqual(final["result"], "Verify large files separately.")
            self.refresh(client, run)
            self.assertEqual(api.call_count, 7)

    def test_inspection_is_read_only_even_with_draft_permission_and_review_off(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            api.side_effect = [
                route("inspect"), {"session_id": "devin-abc", "status": "new"},
                {"status": "exit", "structured_output": {"summary": "Inspected."}}, {"items": []},
            ]
            run = self.submit(client, prompt="Inspect", repo="test/repo", draft_pr=True)
            prompt = api.call_args.args[2]["prompt"]
            self.assertIn("Только чтение", prompt)
            self.assertNotIn("Разрешён один draft PR", prompt)
            final = self.refresh(client, run)
            self.assertEqual(final["status"], "ended")
            self.assertEqual(final["activity"]["report"]["summary"], "Inspected.")
            self.assertEqual(api.call_count, 4)

    def test_implementation_defaults_to_no_publish_and_waits_for_user(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            api.side_effect = [
                route("implement"), {"session_id": "devin-abc", "status": "new"},
                {"status": "running", "status_detail": "waiting_for_user"}, {"items": []},
            ]
            run = self.submit(client, prompt="Implement", repo="test/repo", review=True)
            self.assertIn("Не делай push", api.call_args.args[2]["prompt"])
            result = self.refresh(client, run)
            self.assertEqual(result["activity"]["detail"], "waiting_for_user")
            self.assertEqual(result["status"], "running")
            self.assertEqual(api.call_count, 4)

    def test_invalid_router_output_does_not_launch_any_agent_or_media(self):
        with self.client(live=True) as client, patch("providers.request_json", return_value=route("arbitrary")) as api:
            run = self.submit(client, prompt="Run a task", repo="test/repo")
            self.assertEqual(self.get(client, run)["status"], "failed")
            api.assert_called_once()

    def test_missing_repo_returns_clarification_without_launch(self):
        with self.client(live=True) as client, patch("providers.request_json", return_value=route("implement")) as api:
            run = self.submit(client, prompt="Implement imports")
            self.assertEqual(self.get(client, run)["route"], "clarify")
            api.assert_called_once()

    def test_image_and_async_video_need_confirmation_once(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            api.side_effect = [
                route("image", ""), {"data": [{"url": "https://media.example/image.webp"}]},
                route("video", ""), {"request_id": "uuid-123"},
                {"status": "pending", "progress": 42},
                {"status": "done", "video": {"url": "https://media.example/video.mp4"}},
            ]
            image = self.submit(client, prompt="Draw a mountain")
            self.assertEqual(self.get(client, image)["status"], "awaiting")
            self.assertEqual(api.call_count, 1)
            self.assertEqual(self.action(client, image, "generate").status_code, 202)
            self.assertEqual(self.get(client, image)["media_url"], "https://media.example/image.webp")
            self.assertEqual(api.call_args.args[2]["model"], "grok-imagine-image-2.0")
            self.assertEqual(self.action(client, image, "generate").status_code, 409)

            video = self.submit(client, prompt="Make a short clip")
            self.assertEqual(self.action(client, video, "generate").status_code, 202)
            self.assertEqual(api.call_args.args[2]["prompt"], "Make a short clip")
            self.assertEqual(self.refresh(client, video)["progress"], 42)
            result = self.refresh(client, video)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["media_url"], "https://media.example/video.mp4")

    def test_explicit_media_request_is_routed_locally_and_base64_image_is_stored(self):
        jpeg = b"\xff\xd8\xff\xe0 sample"
        with self.client(live=True) as client, patch("providers.request_json") as api:
            api.return_value = {"data": [{"b64_json": base64.b64encode(jpeg).decode()}]}
            image = self.submit(client, prompt="Нарисуй картинку лимона")
            self.assertEqual(self.get(client, image)["route"], "image")
            api.assert_not_called()
            self.assertEqual(self.action(client, image, "generate").status_code, 202)
            api.assert_called_once()
            self.assertEqual(api.call_args.args[2]["response_format"], "b64_json")
            url = self.get(client, image)["media_url"]
            self.assertEqual(url, f"/api/jobs/{image.json()['id']}/media")
            media = client.get(url)
            self.assertEqual((media.content, media.headers["content-type"]), (jpeg, "image/jpeg"))

            api.return_value = {"data": [{"b64_json": base64.b64encode(b"<html>").decode()}]}
            bad = self.submit(client, prompt="Нарисуй картинку лимона")
            self.action(client, bad, "generate")
            self.assertEqual(self.get(client, bad)["status"], "failed")

            self.assertEqual(client.post("/api/jobs/clear", json={}, headers=WRITE).json(), {"cleared": 2})
            self.assertEqual(client.get(url).status_code, 404)
            self.assertFalse(list((self.path.parent / "media").iterdir()))

    def test_clear_hides_finished_jobs_but_keeps_active_ones_and_usage(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            api.side_effect = [
                {**route("answer", "Answer"), "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
                route("implement"), {"session_id": "devin-session", "status": "running", "acus_consumed": 1.5},
            ]
            done = self.submit(client, prompt="Compare CSV parsers")
            active = self.submit(client, prompt="Fix the import", repo="test/repo")
            self.assertEqual(self.get(client, active)["status"], "running")
            self.assertEqual(client.post("/api/jobs/clear", json={}).status_code, 403)
            self.assertEqual(client.post("/api/jobs/clear", json={}, headers=WRITE).json(), {"cleared": 1})
            self.assertEqual(api.call_count, 3)
            self.assertEqual([job["id"] for job in client.get("/api/jobs").json()], [active.json()["id"]])
            self.assertEqual(client.get(f"/api/jobs/{done.json()['id']}").status_code, 404)
            overview = client.get("/api/overview").json()
            self.assertEqual(overview["jobs"]["total"], 1)
            self.assertEqual(overview["grok"]["total_tokens"], 10)
            self.assertEqual(overview["devin"]["acu"], 1.5)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT prompt FROM jobs WHERE hidden").fetchall(), [("",)])

    def test_settings_need_admin_password_and_never_return_keys(self):
        with self.client(live=True) as client:
            self.assertEqual(client.get("/settings").status_code, 200)
            self.assertEqual(client.get("/api/settings").json()["keys"], {"antig": "environment", "xai": "missing"})
            locked = client.post("/api/settings", json={"provider": "xai"}, headers=WRITE)
            self.assertEqual(locked.status_code, 403)
        secret = "xai-" + "k" * 24
        settings = Settings(self.path, antig_key="mock-antig", admin_key="open-sesame")
        with TestClient(create_app(settings, poll_seconds=0)) as client, patch("providers.request_json") as api:
            self.assertEqual(client.get("/api/settings").status_code, 401)
            auth = ("bridge", "open-sesame")
            self.assertEqual(client.get("/", auth=("bridge", "wrong")).status_code, 401)
            wrong = client.post("/api/settings", json={"provider": "xai", "key": "ag_" + "k" * 24}, headers=WRITE, auth=auth)
            self.assertEqual(wrong.status_code, 422)
            self.assertNotIn("k" * 24, wrong.text)
            saved = client.post("/api/settings", json={"provider": "xai", "key": secret}, headers=WRITE, auth=auth)
            self.assertEqual(saved.json()["keys"]["xai"], "runtime")
            self.assertEqual(saved.json()["provider"], "xai")
            self.assertNotIn(secret, saved.text + client.get("/api/config", auth=auth).text)
            api.return_value = route("answer", f"Leaked {secret}")
            run = client.post("/api/jobs", json={"prompt": f"Explain {secret}"}, headers=WRITE, auth=auth)
            self.assertEqual(api.call_args.args[:2], ("https://api.x.ai/v1/chat/completions", secret))
            self.assertNotIn(secret, client.get(f"/api/jobs/{run.json()['id']}", auth=auth).text)
            reset = client.post("/api/settings", json={"provider": "xai", "reset": True}, headers=WRITE, auth=auth)
            self.assertEqual(reset.json()["keys"]["xai"], "missing")
            self.assertEqual(client.post("/api/jobs", json={"prompt": "Hi"}, headers=WRITE, auth=auth).status_code, 503)

    def test_background_progress_without_browser_refresh(self):
        polled = Event()

        def api(url, *_args, **_kwargs):
            if url.endswith("/chat/completions"):
                return route("inspect")
            if url.endswith("/sessions"):
                return {"session_id": "devin-abc", "status": "new"}
            if "/messages?" in url:
                polled.set()
                return {"items": []}
            return {"status": "exit"}

        with self.client(live=True, poll=0.02) as client, patch("providers.request_json", side_effect=api):
            run = self.submit(client, prompt="Inspect", repo="test/repo")
            self.assertEqual(run.status_code, 202)
            self.assertTrue(polled.wait(3), "Expected the server to poll without a browser request")
        with self.client(live=True) as client:
            self.assertEqual(self.get(client, run)["status"], "ended")

    def test_retryable_telemetry_and_review_errors_do_not_discard_report(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            api.side_effect = [
                route("implement"), {"session_id": "devin-abc", "status": "new"},
                ProviderError("Status API unavailable"),
                {"status": "exit", "structured_output": {"summary": "Report"}},
                ProviderError("Messages unavailable"), ProviderError("Review unavailable"),
            ]
            run = self.submit(client, prompt="Implement", repo="test/repo", review=True)
            self.assertEqual(self.refresh(client, run)["status"], "running")
            final = self.refresh(client, run)
            self.assertEqual(final["status"], "ended")
            self.assertIn("Review unavailable", final["error"])
            self.assertIn("Messages unavailable", final["activity"]["warnings"][0])
            self.assertEqual(final["activity"]["report"]["summary"], "Report")
            self.refresh(client, run)
            self.assertEqual(api.call_count, 6)

    def test_demo_works_without_api_and_persists(self):
        with self.client(demo=True) as client, patch("providers.request_json") as api:
            page = client.get("/").text
            self.assertIn("Твой центр", page)
            self.assertNotIn('id="job-form"', page)
            self.assertNotIn('id="history-list"', page)
            form = client.get("/tasks/new").text
            sessions = client.get("/sessions").text
            self.assertIn('id="job-form"', form)
            self.assertNotIn('name="mode"', form)
            self.assertNotIn('id="history-list"', form)
            self.assertIn('id="history-list"', sessions)
            self.assertNotIn('id="job-form"', sessions)
            self.assertTrue(client.get("/api/config").json()["demo"])
            run = self.submit(client, prompt="Исправь импорт CSV", repo="test/repo", review=True)
            self.assertEqual(self.refresh(client, run)["status"], "running")
            self.assertEqual(self.refresh(client, run)["status"], "running")
            self.assertEqual(self.refresh(client, run)["status"], "ended")
            api.assert_not_called()
        with self.client(demo=True) as client:
            self.assertTrue(self.get(client, run)["reviewed_at"])

    def test_restart_keeps_history_and_does_not_replay_uncertain_launch(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, mode TEXT, prompt TEXT, model TEXT, repo TEXT, "
                         "max_acu INTEGER, source_id TEXT, created_at TEXT, status TEXT, result TEXT, media_url TEXT, "
                         "remote_id TEXT, session_url TEXT, pr_url TEXT, progress INTEGER, error TEXT)")
            conn.execute("INSERT INTO jobs (id, mode, prompt, status) VALUES (?, 'grok', 'legacy', 'sending')", ("0" * 32,))
        with self.client(demo=True) as client, patch("providers.request_json") as api:
            history = client.get("/api/jobs").json()
            self.assertEqual(history[0]["status"], "failed")
            self.assertEqual(history[0]["mode"], "grok")
            api.assert_not_called()

    def test_invalid_requests_never_contact_providers(self):
        with self.client(live=True) as client, patch("providers.request_json") as api:
            self.assertEqual(client.post("/api/jobs", json={"prompt": "test"}).status_code, 403)
            for request in ({"prompt": " "}, {"prompt": "test", "max_acu": 0}, {"prompt": "test", "ref": "main"},
                            {"prompt": "test", "mode": "devin"}, {"prompt": "test", "repo": "../repo"}):
                self.assertEqual(self.submit(client, **request).status_code, 422)
            self.assertEqual(client.get("/api/jobs").json(), [])
            api.assert_not_called()

    def test_live_without_keys_is_disabled(self):
        with self.client() as client:
            self.assertFalse(client.get("/api/config").json()["antig"])
            self.assertEqual(self.submit(client, prompt="Plan").status_code, 503)

    def test_untrusted_urls(self):
        for url in ("javascript:alert(1)", "file:///tmp/img.png", "https://name:pass@host/", "https://["):
            self.assertEqual(https_url(url), "")
        with (patch("providers.request_json", return_value={"data": [{"url": "javascript:alert(1)"}]}),
              self.assertRaises(ProviderError)):
            AntiG("mock-antig").image("test", "grok-imagine-image")
        with patch("repository.request_json") as api:
            for url in ("https://evil.test/test/repo/pull/1", "https://github.com/test/other/pull/1"):
                with self.assertRaises(ProviderError):
                    GitHub("mock-github").pull_request("test/repo", url)
            api.assert_not_called()

    def test_stream_merges_content_without_reasoning(self):
        stream = (
            b'data: {"choices":[{"delta":{"reasoning_content":"not shown"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"First "}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"step."}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        self.assertEqual(collect_chat_stream(stream)["choices"][0]["message"]["content"], "First step.")

    def test_context_is_read_again_at_the_same_commit_before_routing(self):
        commit, tree_sha = "a" * 40, "b" * 40
        with (self.client(live=True) as client, patch("repository.request_json") as github,
              patch("providers.request_json") as api):
            text = {"type": "file", "encoding": "base64", "size": 5, "content": "aGVsbG8="}
            github.side_effect = [
                {"default_branch": "release"}, {"sha": commit, "commit": {"tree": {"sha": tree_sha}}},
                {"tree": [
                    {"path": "README.md", "type": "blob", "mode": "100644", "size": 5},
                    {"path": ".env", "type": "blob", "mode": "100644", "size": 100},
                    {"path": "link", "type": "blob", "mode": "120000", "size": 100},
                ]}, text, text,
            ]
            body = {"repo": "test/repo"}
            tree = client.post("/api/repo/tree", json=body, headers=WRITE).json()
            self.assertEqual(tree["paths"], ["README.md"])
            self.assertEqual(tree["ref"], "release")
            selection = {**body, "ref": "release", "commit": commit}
            forbidden = client.post("/api/repo/preview", json={**selection, "paths": [".env"]}, headers=WRITE)
            self.assertEqual(forbidden.status_code, 422)
            preview = client.post("/api/repo/preview", json={**selection, "paths": ["README.md"]}, headers=WRITE)
            self.assertEqual(preview.json()["files"][0]["text"], "hello")
            api.assert_not_called()

            api.side_effect = [route("implement"), {"session_id": "devin-session", "status": "new"}]
            run = self.submit(client, prompt="Fix the project", **body, ref="release",
                              context={"commit": commit, "paths": ["README.md"]})
            self.assertEqual(run.status_code, 202)
            self.assertEqual(github.call_args_list[-1], github.call_args_list[-2])
            self.assertTrue(github.call_args.args[0].endswith(f"/contents/README.md?ref={commit}"))
            router_input = api.call_args_list[0].args[2]["messages"][-1]["content"]
            self.assertIn("hello", router_input)
            self.assertIn("README.md", router_input)
            payload = api.call_args.args[2]
            self.assertEqual(payload["repos"], ["test/repo"])
            self.assertIn("release", payload["prompt"])
            self.assertIn(commit, payload["prompt"])
            self.assertNotIn("hello", payload["prompt"])
            self.assertNotIn("hello", str(self.get(client, run)))
