from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

GROK_MODEL = "grok-4.7"
IMAGE_MODELS = frozenset({"grok-imagine-image", "grok-imagine-image-2.0"})
VIDEO_MODELS = frozenset({"grok-imagine-video", "grok-imagine-video-1.5", "grok-imagine-video-1.5-lite"})


class ProviderError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def collect_chat_stream(raw: bytes) -> dict[str, Any]:
    chunks: list[str] = []
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        event = line[5:].strip()
        if event == b"[DONE]":
            break
        data = json.loads(event)
        if not isinstance(data, dict) or data.get("error"):
            raise ProviderError("AntiG вернул ошибку в потоке ответа.")
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        delta = choice.get("delta") if isinstance(choice, dict) else None
        text = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(text, str):
            chunks.append(text)
    return {"choices": [{"message": {"content": "".join(chunks)}}]}


def request_json(url: str, key: str, payload: dict[str, Any] | None = None, *,
                 stream_model: str = "", headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if key:
        request_headers["Authorization"] = f"Bearer {key}"
    req = Request(url, headers=request_headers)
    if stream_model:
        req.add_header("Accept", "text/event-stream, application/json")
        req.add_header("x-grok-model-override", stream_model)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    try:
        with build_opener(NoRedirect()).open(req, timeout=120 if payload is not None else 15) as response:
            event_stream = response.headers.get_content_type() == "text/event-stream"
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ProviderError("Ответ сервиса превысил лимит размера.")
        data = collect_chat_stream(raw) if stream_model and event_stream else json.loads(raw)
    except HTTPError as exc:
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise ProviderError("Лимит API исчерпан. Для GitHub можно задать GITHUB_READ_TOKEN.") from None
        messages = {
            401: "Сервис не принял API-ключ.",
            402: "Сервис сообщает о проблеме с квотой или оплатой.",
            403: "У ключа нет доступа к этой операции.",
            404: "Сервис не нашёл этот маршрут или задачу.",
            429: "Достигнут лимит запросов. Попробуй позже.",
        }
        raise ProviderError(messages.get(exc.code, f"Сервис ответил ошибкой HTTP {exc.code}.")) from None
    except (URLError, OSError, ValueError):
        raise ProviderError("Не удалось прочитать ответ сервиса. Перед повтором проверь, не запустилась ли задача.") from None
    if not isinstance(data, dict):
        raise ProviderError("Неожиданный формат ответа сервиса.")
    return data


def https_url(value: Any) -> str:
    if isinstance(value, str) and len(value) <= 8192:
        try:
            parts = urlsplit(value)
            if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
                return value
        except ValueError:
            pass
    return ""


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ProviderError("Сервис не вернул корректный ID задачи.")
    return value


class AntiG:
    base = "https://grok.antig.ru/v1"

    def __init__(self, key: str) -> None:
        self.key = key

    def plan(self, prompt: str) -> dict[str, Any]:
        data = request_json(f"{self.base}/chat/completions", self.key, {
            "model": GROK_MODEL,
            "stream": True,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты помогаешь с обычной разработкой ПО. Оцени задачу и предложи конкретный "
                        "план для инженера: изменения, критерии готовности, проверки и риски. "
                        "Не выдумывай содержимое репозитория. Отвечай на языке пользователя."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }, stream_model=GROK_MODEL)
        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("Grok не вернул текст.")
        return {"status": "done", "result": content[:60_000]}

    def image(self, prompt: str, model: str) -> dict[str, Any]:
        data = request_json(f"{self.base}/images/generations", self.key, {
            "model": model, "prompt": prompt, "n": 1,
        })
        items = data.get("data")
        first = items[0] if isinstance(items, list) and items else None
        url = https_url(first.get("url")) if isinstance(first, dict) else ""
        if not url:
            raise ProviderError("AntiG не вернул ссылку на изображение.")
        return {"status": "done", "media_url": url}

    def start_video(self, prompt: str, model: str) -> dict[str, Any]:
        data = request_json(f"{self.base}/videos/generations", self.key, {
            "model": model, "prompt": prompt,
        })
        return {"status": "running", "remote_id": identifier(data.get("request_id"))}

    def video_status(self, remote_id: str) -> dict[str, Any]:
        data = request_json(f"{self.base}/videos/{identifier(remote_id)}", self.key)
        status = data.get("status")
        if status == "done":
            video = data.get("video")
            url = https_url(video.get("url")) if isinstance(video, dict) else ""
            if not url:
                raise ProviderError("AntiG не вернул ссылку на видео.")
            return {"status": "done", "media_url": url, "progress": 100}
        if status in ("expired", "failed", "error", "cancelled"):
            return {"status": "failed", "error": "AntiG не завершил генерацию видео."}
        if status not in ("pending", "queued", "running", "in_progress", "processing"):
            raise ProviderError("Неизвестный статус видео от AntiG.")
        progress = data.get("progress")
        return {"status": "running", "progress": progress if type(progress) is int and 0 <= progress <= 100 else None}


class Devin:
    def __init__(self, key: str, org: str) -> None:
        self.key = key
        self.base = f"https://api.devin.ai/v3/organizations/{identifier(org)}/sessions"

    def start(self, prompt: str, repo: str | None, max_acu: int) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt, "max_acu_limit": max_acu, "tags": ["antig-bridge"]}
        if repo:
            payload["repos"] = [repo]
        data = request_json(self.base, self.key, payload)
        remote_id = identifier(data.get("session_id"))
        return self._status(data, remote_id)

    def status(self, remote_id: str) -> dict[str, Any]:
        data = request_json(f"{self.base}/{identifier(remote_id)}", self.key)
        return self._status(data, remote_id)

    @staticmethod
    def _status(data: dict[str, Any], remote_id: str) -> dict[str, Any]:
        raw_status = data.get("status")
        mapping = {
            "new": "running", "claimed": "running", "running": "running", "resuming": "running",
            "suspended": "paused", "exit": "ended", "error": "failed",
        }
        if not isinstance(raw_status, str) or raw_status not in mapping:
            raise ProviderError("Неизвестный статус сессии от Devin.")
        prs = data.get("pull_requests")
        pr_url = next((url for pr in prs if isinstance(pr, dict) and
                       (url := https_url(pr.get("pr_url")))), "") if isinstance(prs, list) else ""
        return {
            "status": mapping[raw_status],
            "remote_id": remote_id,
            "session_url": f"https://app.devin.ai/sessions/{remote_id.removeprefix('devin-')}",
            "pr_url": pr_url,
            "error": "Сессия Devin завершилась ошибкой." if raw_status == "error" else "",
        }
