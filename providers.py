from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

GROK_MODEL = "grok-4.7"
GrokProvider = Literal["antig", "xai"]
REPORT_FIELDS = ("summary", "checks", "remaining")


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ChatUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def read(cls, value: Any) -> ChatUsage:
        if not isinstance(value, dict):
            return cls()

        def count(field: str) -> int | None:
            number = value.get(field)
            return number if type(number) is int and 0 <= number <= 10**12 else None

        input_tokens, output_tokens, total = (count(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
        if total is None and input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens
        return cls(input_tokens, output_tokens, total)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def collect_chat_stream(raw: bytes) -> dict[str, Any]:
    chunks: list[str] = []
    usage: dict[str, Any] | None = None
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        event = line[5:].strip()
        if event == b"[DONE]":
            break
        data = json.loads(event)
        if not isinstance(data, dict) or data.get("error"):
            raise ProviderError("AntiG вернул ошибку в потоке ответа.")
        if isinstance(data.get("usage"), dict) and ChatUsage.read(data["usage"]) != ChatUsage():
            usage = data["usage"]
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        delta = choice.get("delta") if isinstance(choice, dict) else None
        text = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(text, str):
            chunks.append(text)
    return {"choices": [{"message": {"content": "".join(chunks)}}], "usage": usage}


def request_json(url: str, key: str, payload: dict[str, Any] | None = None, *,
                 stream_model: str = "", headers: dict[str, str] | None = None,
                 list_items: bool = False, limit: int = 4_000_000) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if key:
        request_headers["Authorization"] = f"Bearer {key}"
    req = Request(url, headers=request_headers)
    if stream_model:
        req.add_header("Accept", "text/event-stream, application/json")
        if urlsplit(url).hostname == "grok.antig.ru":
            req.add_header("x-grok-model-override", stream_model)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    try:
        with build_opener(NoRedirect()).open(req, timeout=120 if payload is not None else 15) as response:
            event_stream = response.headers.get_content_type() == "text/event-stream"
            raw = response.read(limit + 1)
        if len(raw) > limit:
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
    if list_items and isinstance(data, list):
        return {"items": data}
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


def image_type(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return ""


def media_download(url: str, hosts: tuple[str, ...], limit: int) -> bytes:
    if not https_url(url):
        raise ProviderError("Некорректная ссылка провайдера на файл.")
    try:
        parts = urlsplit(url)
        if parts.hostname not in hosts or parts.port not in (None, 443):
            raise ProviderError("Хост медиа не разрешён. Проверь BRIDGE_MEDIA_HOSTS на сервере.")
        with build_opener(NoRedirect()).open(Request(url), timeout=25) as response:
            raw = response.read(limit + 1)
    except (OSError, ValueError):
        raise ProviderError("Медиафайл не удалось получить без перенаправлений.") from None
    if len(raw) > limit:
        raise ProviderError("Медиафайл превысил лимит размера для вложений.")
    return raw


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ProviderError("Сервис не вернул корректный ID задачи.")
    return value


class AntiG:
    base = "https://grok.antig.ru/v1"

    def __init__(self, key: str, provider: GrokProvider = "antig") -> None:
        self.key = key
        if provider == "xai":
            self.base = "https://api.x.ai/v1"

    def text(self, prompt: str, system: str) -> tuple[str, ChatUsage]:
        data = request_json(f"{self.base}/chat/completions", self.key, {
            "model": GROK_MODEL,
            "stream": True,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }, stream_model=GROK_MODEL)
        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("Grok не вернул текст.")
        return content[:60_000], ChatUsage.read(data.get("usage"))

    def image(self, prompt: str, model: str) -> dict[str, Any]:
        # Base64 keeps the file on the bridge: provider URLs are temporary and AntiG may omit them.
        data = request_json(f"{self.base}/images/generations", self.key, {
            "model": model, "prompt": prompt, "n": 1, "response_format": "b64_json",
        }, limit=24_000_000)
        items = data.get("data")
        first = items[0] if isinstance(items, list) and items else None
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if isinstance(encoded, str) and encoded:
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise ProviderError("Grok вернул повреждённое изображение.") from None
            kind = image_type(raw)
            if not kind:
                raise ProviderError("Grok вернул файл неизвестного формата.")
            return {"status": "done", "media": raw, "media_type": kind}
        url = https_url(first.get("url")) if isinstance(first, dict) else ""
        if not url:
            raise ProviderError("Grok не вернул изображение.")
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

    def upload(self, name: str, content: bytes, mime: str) -> str:
        boundary = "----bridge-" + uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f'Content-Type: {mime}\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
        req = Request(self.base.removesuffix("/sessions") + "/attachments", data=body, headers={
            "Authorization": f"Bearer {self.key}", "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        try:
            with build_opener(NoRedirect()).open(req, timeout=90) as response:
                data = json.loads(response.read(64_000))
        except (OSError, ValueError):
            raise ProviderError("Devin не принял вложение. Проверь доступ и лимит файла.") from None
        url = https_url(data.get("url")) if isinstance(data, dict) else ""
        if not url or urlsplit(url).hostname not in {"app.devin.ai", "api.devin.ai"}:
            raise ProviderError("Devin не вернул ссылку на вложение.")
        return url

    def start(self, prompt: str, repo: str, max_acu: int, attachment_urls: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": prompt, "repos": [repo], "max_acu_limit": max_acu, "tags": ["antig-bridge"],
            "structured_output_required": True,
            "structured_output_schema": {
                "type": "object",
                "properties": {field: {"type": "string", "maxLength": 2000} for field in REPORT_FIELDS},
                "required": list(REPORT_FIELDS),
                "additionalProperties": False,
            },
        }
        if attachment_urls:
            payload["attachment_urls"] = attachment_urls
        data = request_json(self.base, self.key, payload)
        remote_id = identifier(data.get("session_id"))
        return self._status(data, remote_id)

    def status(self, remote_id: str) -> dict[str, Any]:
        data = request_json(f"{self.base}/{identifier(remote_id)}", self.key)
        return self._status(data, remote_id)

    def messages(self, remote_id: str, after: str | None = None) -> tuple[list[dict[str, Any]], str | None, bool]:
        params = {"first": "100"}
        if after:
            params["after"] = after
        data = request_json(f"{self.base}/{identifier(remote_id)}/messages?{urlencode(params)}", self.key)
        items = data.get("items")
        if not isinstance(items, list):
            raise ProviderError("Devin не вернул ленту сообщений.")
        messages = []
        for item in items[:100]:
            if not isinstance(item, dict) or item.get("source") != "devin":
                continue
            text, event_id, at = item.get("message"), item.get("event_id"), item.get("created_at")
            if isinstance(text, str) and isinstance(event_id, str) and type(at) is int:
                messages.append({"id": event_id[:200], "text": text[:4000], "at": at})
        cursor = data.get("end_cursor")
        # A last page may return a null cursor; re-fetch that page to spot new messages.
        return messages, cursor if isinstance(cursor, str) and len(cursor) <= 1024 else after, (
            data.get("has_next_page") is True and isinstance(cursor, str) and cursor != after
        )

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
        detail = data.get("status_detail")
        output = data.get("structured_output")
        report: dict[str, str] = {}
        if isinstance(output, dict):
            report = {field: value.strip()[:2000] for field in REPORT_FIELDS
                      if isinstance(value := output.get(field), str)}
        acus = data.get("acus_consumed")
        return {
            "status": "ended" if detail == "finished" and raw_status != "error" else mapping[raw_status],
            "detail": detail[:80] if isinstance(detail, str) else "",
            "report": report,
            "remote_id": remote_id,
            "session_url": f"https://app.devin.ai/sessions/{remote_id.removeprefix('devin-')}",
            "pr_url": pr_url,
            **({"devin_acu": float(acus)} if isinstance(acus, (int, float)) and not isinstance(acus, bool)
               and 0 <= acus <= 10**12 else {}),
            "error": "Сессия Devin завершилась ошибкой." if raw_status == "error" else "",
        }
