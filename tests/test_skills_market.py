import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from test_bridge import WRITE, chat, route

from app import Settings, create_app
from providers import ProviderError
from skills_market import (
    DESIGN_TOPICS,
    RECIPES,
    TOPICS,
    directory_overview,
    leaderboard,
    search_directory,
    showcase,
)

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
        leaderboard.cache_clear()
        showcase.cache_clear()

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

    def test_large_showcase_has_unique_paginated_cards_and_recommendations_are_local(self):
        def data(url, _key):
            topic = parse_qs(urlsplit(url).query)["q"][0]
            topic_id = TOPICS.index(topic)
            return {"skills": [
                {"id": f"test/repo/card-{i}", "installs": i}
                for i in range(topic_id * 160, topic_id * 160 + 200)
            ]}

        with self.client(public=True) as client, patch("skills_market.request_json", side_effect=data) as external:
            cards = []
            for page in range(1, 41):
                result = client.get("/api/skills", params={"page": page, "sort": "installs"}).json()
                cards.extend(result["items"])
                if not result["has_more"]:
                    break
            self.assertGreaterEqual(result["total"], 600)
            self.assertEqual(len(cards), len({item["id"] for item in cards}))
            self.assertEqual(len(cards), result["total"])
            installs = [item["installs"] for item in cards if item["installs"] is not None]
            self.assertEqual(installs, sorted(installs, reverse=True))
            self.assertEqual(external.call_count, len(TOPICS))
            recommended = client.get("/api/skills", params={"tab": "recommended"}).json()["items"]
            self.assertEqual(recommended[0]["recipe_title"], "Студия: дизайн с характером")
            self.assertTrue(all(item["recipe_title"] and item["description"] for item in recommended))
            self.assertEqual(external.call_count, len(TOPICS))

    def test_rankings_preserve_source_positions_and_show_separate_catalog_total(self):
        html = """
            <a href="/">All Time (1,234,567)</a>
            <a href="/site/example.com/demo-skill"><em>2</em><b>Demo skill</b><span>example.com</span><span>7</span>+3</a>
            <a href="/example/repo/example-skill"><em>1</em><b>Example skill</b><span>example/repo</span><span>9</span>+4</a>
            <a href="https://untrusted.example/skill">999</a>
        """
        with self.client(public=True) as client, patch("skills_market.html_page", return_value=html) as external:
            for tab, path in (("all-time", "/"), ("trending", "/trending"), ("hot", "/hot")):
                results = client.get("/api/skills", params={"tab": tab}).json()
                self.assertEqual((results["total"], results["source_total"]), (2, 1234567))
                self.assertEqual(external.call_args.args[0], "https://www.skills.sh" + path)
                self.assertEqual([item["rank"] for item in results["items"]], [1, 2])
                self.assertEqual(results["items"][-1]["url"], "https://www.skills.sh/site/example.com/demo-skill")
                self.assertEqual(results["items"][-1]["rank_stat"], "+3")

    def test_design_topics_russian_search_and_recommendation_groups(self):
        def data(url, _key):
            query = parse_qs(urlsplit(url).query)["q"][0]
            if query == "motion animation buttons":
                return {"skills": [{"id": "example/ui/general"}, {"id": "example/ui/button-animation"}]}
            topic = list(DESIGN_TOPICS.values()).index(query)
            return {"skills": [{"id": f"example/ui/skill-{i}"} for i in range(topic * 160, topic * 160 + 200)]}

        with self.client(public=True) as client, patch("skills_market.request_json", side_effect=data) as external:
            def results(**params):
                return client.get("/api/skills", params=params).json()

            design = results(tab="design")
            self.assertGreater(design["total"], 1000)
            self.assertTrue(design["has_more"])
            self.assertEqual(external.call_count, len(DESIGN_TOPICS))
            self.assertEqual(results(tab="design", topic="figma")["total"], 200)
            self.assertEqual(results(tab="design", topic="motion", q="Анимация кнопок")["items"][0]["id"],
                             "example/ui/button-animation")
            for kind in ("design", "engineering"):
                rows = results(tab="recommended", group=kind)["items"]
                self.assertGreater(len(rows), 5)
                self.assertTrue(all(item["recipe_group"] == kind for item in rows))

    def test_custom_skill_is_private_reaches_both_agents_and_is_deleted_with_its_text(self):
        skill = {"title": "Hero with media", "description": "Landing pages",
                 "grok": "Prepare Grok Imagine prompts for the hero.", "devin": "Embed committed media with a poster."}
        with self.client(demo=False) as owner, self.client(demo=False) as other, patch("providers.request_json") as provider:
            provider.side_effect = [route("inspect"), {"session_id": "sample", "status": "new"}]
            short = owner.post("/api/skills/custom", json={**skill, "grok": "short"}, headers=WRITE)
            self.assertEqual(short.status_code, 422)
            created = owner.post("/api/skills/custom", json=skill, headers=WRITE)
            self.assertEqual(created.status_code, 201)
            skill_id = created.json()["id"]
            card = self.library(owner)[0]
            self.assertEqual((card["id"], card["recipe_group"], card["installed"]), (skill_id, "custom", True))
            self.assertEqual(self.info(owner, skill_id)["recipe"], {"grok": skill["grok"], "devin": skill["devin"]})
            self.assertEqual(other.get("/api/skills/info", params={"skill_id": skill_id}).status_code, 404)
            self.assertEqual(self.select(other, skill_id).status_code, 404)
            self.assertEqual(self.submit(other, [skill_id]).status_code, 422)
            provider.assert_not_called()

            self.assertEqual(self.submit(owner, [skill_id]).status_code, 202)
            self.assertIn(skill["grok"], provider.call_args_list[0].args[2]["messages"][0]["content"])
            devin = provider.call_args_list[1].args[2]["prompt"]
            self.assertIn(skill["devin"], devin)
            self.assertIn("Только чтение: не изменяй файлы", devin)
            self.assertIn("Экономь ACU", devin)

            self.assertEqual(self.select(owner, skill_id, installed=False).status_code, 200)
            self.assertEqual(self.library(owner), [])
            self.assertEqual(self.submit(owner, [skill_id]).status_code, 422)

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
