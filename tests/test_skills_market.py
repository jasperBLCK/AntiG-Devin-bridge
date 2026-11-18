import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from test_bridge import WRITE, chat, route

from app import Settings, create_app
from providers import ProviderError
from skills_market import RECIPES, directory_overview, search_directory

READY = "bridge/agent-skills/regression-tests"
OTHER_READY = "anthropics/skills/frontend-design"
EXTERNAL = "example/project/python-helper"


class SkillMarketTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "bridge.sqlite3"
        search_directory.cache_clear()
        directory_overview.cache_clear()

    def client(self, *, demo=True, public=False):
        return TestClient(create_app(Settings(self.path, demo=demo, public_skills=public,
                                              antig_key="mock-antig", devin_key="mock-devin",
                                              devin_org="org-mock"), poll_seconds=0))

    @staticmethod
    def select(client, skill_id=READY, installed=True):
        return client.post("/api/skills/library", json={"skill_id": skill_id, "installed": installed}, headers=WRITE)

    @staticmethod
    def review(client, skill_id=READY, stars=5, text="Helpful"):
        return client.post("/api/skills/review", json={"skill_id": skill_id, "stars": stars, "text": text}, headers=WRITE)

    @staticmethod
    def info(client, skill_id=READY):
        return client.get("/api/skills/info", params={"skill_id": skill_id}).json()

    @staticmethod
    def library(client):
        return client.get("/api/skills", params={"tab": "library"}).json()["items"]

    @staticmethod
    def submit(client, skills, **fields):
        return client.post("/api/jobs", json={"prompt": "Fix React UI", "repo": "test/repo", "skill_ids": skills, **fields},
                           headers=WRITE)

    def test_offline_catalog_and_pages_do_not_call_external_services(self):
        with self.client() as client, patch("skills_market.request_json") as directory:
            self.assertIn("МАРКЕТПЛЕЙС АГЕНТОВ", client.get("/skills").text)
            self.assertIn('href="/skills"', client.get("/").text)
            data = client.get("/api/skills").json()
            self.assertFalse(data["public"])
            self.assertEqual({item["id"] for item in data["items"]}, set(RECIPES))
            self.assertTrue(all(item["rating"] is None for item in data["items"]))
            self.assertEqual(client.get("/api/skills/info", params={"skill_id": EXTERNAL}).status_code, 404)
            directory.assert_not_called()

    def test_external_directory_overview_save_and_failure_fallback(self):
        with (self.client(public=True) as client, patch("skills_market.request_json") as directory,
              patch("skills_market.build_opener") as opener):
            directory.return_value = {"skills": [
                {"id": EXTERNAL, "name": "Python helper", "installs": 7321},
                {"id": "../unknown/invalid", "name": "Bad path"},
                {"id": "https://untrusted.example/skill", "name": "Bad URL"},
            ]}
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = (
                b'<meta property="og:description" content="A Python skill &amp; guide">'
            )
            data = client.get("/api/skills", params={"q": "python testing"}).json()
            items = data["items"]
            self.assertEqual([(i["id"], i["installs"], i["recipe_title"]) for i in items], [(EXTERNAL, 7321, None)])
            url, key = directory.call_args.args
            self.assertEqual((urlsplit(url).hostname, key), ("skills.sh", ""))
            self.assertEqual(parse_qs(urlsplit(url).query)["q"], ["python testing"])
            self.assertEqual(self.info(client, EXTERNAL)["description"], "A Python skill & guide")
            self.assertEqual(self.select(client, EXTERNAL).status_code, 200)
            self.assertEqual(self.library(client)[0]["id"], EXTERNAL)
            self.assertEqual(self.submit(client, [EXTERNAL]).status_code, 422)
            target = opener.return_value.open.call_args.args[0]
            self.assertEqual(target.full_url, f"https://www.skills.sh/{EXTERNAL}")
            directory.side_effect = ProviderError("Catalogue unavailable")
            result = client.get("/api/skills").json()
            self.assertIn("временно недоступен", result["warning"])
            self.assertEqual({item["id"] for item in result["items"]}, set(RECIPES))

    def test_library_is_private_to_browser_and_review_is_upserted_and_aggregated(self):
        with self.client() as first, self.client() as second:
            self.assertEqual(self.review(first).status_code, 403)
            unconfirmed = first.post("/api/skills/library", json={"skill_id": READY, "installed": True})
            self.assertEqual(unconfirmed.status_code, 403)
            response = self.select(first)
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            self.assertEqual(len(self.library(first)), 1)
            self.assertEqual(self.library(second), [])
            self.assertEqual(self.submit(second, [READY]).status_code, 422)
            self.assertEqual(self.review(first, stars=6).status_code, 422)
            self.assertEqual(self.review(first, stars=3).status_code, 200)
            self.assertEqual(self.review(first, stars=5, text="<b>mock-antig</b>").status_code, 200)
            self.assertEqual(self.select(second).status_code, 200)
            self.assertEqual(self.review(second, stars=2).status_code, 200)
            info = self.info(first)
            self.assertEqual((info["rating"], info["votes"], len(info["reviews"])), (3.5, 2, 2))
            self.assertEqual(info["my_review"]["text"], "<b>[скрыто]</b>")
            self.assertEqual(sum(review["mine"] for review in info["reviews"]), 1)
            self.assertEqual(self.select(first, installed=False).status_code, 200)
            self.assertEqual(self.submit(first, [READY]).status_code, 422)

    def test_only_selected_recipes_reach_grok_and_devin_and_survive_uninstall_for_review(self):
        with self.client(demo=False) as client, patch("providers.request_json") as provider:
            provider.side_effect = [
                route("inspect"), {"session_id": "sample", "status": "new"},
                {"status": "running", "status_detail": "finished", "structured_output": {"summary": "Done"}},
                {"items": []}, chat("Reviewed"),
            ]
            self.assertEqual(self.select(client).status_code, 200)
            self.assertEqual(self.select(client, OTHER_READY).status_code, 200)
            self.assertEqual(self.review(client, text="UNTRUSTED REVIEW").status_code, 200)
            rejected = self.submit(client, [READY, READY])
            self.assertEqual(rejected.status_code, 422)
            provider.assert_not_called()
            result = self.submit(client, [READY], draft_pr=True, review=True)
            self.assertEqual(result.status_code, 202)
            self.assertEqual(result.json()["skills"], [{"id": READY, "title": RECIPES[READY]["title"]}])
            grok = provider.call_args_list[0].args[2]["messages"][0]["content"]
            devin = provider.call_args_list[1].args[2]["prompt"]
            self.assertIn(RECIPES[READY]["grok"], grok)
            self.assertIn(RECIPES[READY]["devin"], devin)
            self.assertIn("Только чтение: не изменяй файлы", devin)
            self.assertNotIn("draft PR", devin)
            for packet in (grok, devin):
                self.assertNotIn("UNTRUSTED REVIEW", packet)
                self.assertNotIn(RECIPES[OTHER_READY]["title"], packet)
            self.assertEqual(self.select(client, installed=False).status_code, 200)
            job_id = result.json()["id"]
            final = client.post(f"/api/jobs/{job_id}/refresh", json={}, headers=WRITE).json()
            self.assertEqual(final["status"], "ended")
            self.assertIn(RECIPES[READY]["grok"], provider.call_args.args[2]["messages"][0]["content"])
