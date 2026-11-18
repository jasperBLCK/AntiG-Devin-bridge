# Мост · AntiG × Devin

Локальная веб-панель для разработки: Grok 4.7 через AntiG готовит план, ты проверяешь его, затем запускаешь Devin с этим планом и смотришь статус и ссылку на PR. Изображения и видео по текстовому запросу — отдельные режимы AntiG Imagine.

## Демо на Windows

Нужен Python 3.12+. В PowerShell из папки проекта:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:BRIDGE_MODE = 'demo'
.\.venv\Scripts\python.exe -m uvicorn app:app
```

Открой `http://127.0.0.1:8000`. В демо ключи не нужны, внешних запросов нет. На Linux создай окружение через `python3 -m venv .venv`, используй `.venv/bin/python` и выставь `export BRIDGE_MODE=demo`.

## Реальные задачи

Останови демо. Задай переменные в окружении процесса, а не в коде, браузере или истории команд:

| Переменная | Что указать |
|---|---|
| `ANTIG_API_KEY` | Ключ AntiG для Grok и Imagine |
| `DEVIN_API_KEY` | [PAT или ключ сервисного пользователя](https://docs.devin.ai/api-reference/overview) с доступом к сессиям организации |
| `DEVIN_ORG_ID` | Идентификатор организации Devin с префиксом `org-` |

В PowerShell можно ввести ключи не показывая их на экране:

```powershell
Remove-Item Env:BRIDGE_MODE -ErrorAction SilentlyContinue
$env:ANTIG_API_KEY = ([pscredential]::new('antig', (Read-Host 'Ключ AntiG' -AsSecureString))).GetNetworkCredential().Password
$env:DEVIN_API_KEY = ([pscredential]::new('devin', (Read-Host 'Ключ Devin' -AsSecureString))).GetNetworkCredential().Password
$env:DEVIN_ORG_ID = Read-Host 'ID организации Devin'
.\.venv\Scripts\python.exe -m uvicorn app:app
```

Не публикуй live-панель без контроля доступа: приложение однопользовательское и использует ключи владельца. По умолчанию слушает только localhost. Веб-превью можно открыть с демо-данными.

## Работа

1. **План Grok**: опиши задачу, ограничения и критерии готовности. Доступ к репозиторию сам по себе Grok не получает.
2. **Передать в Devin**: проверь план, поправь поручение, задай репозиторий `владелец/репозиторий` при необходимости и лимит ACU. Сессия запускается только после отдельного нажатия «Запустить Devin».
3. **Задачи**: открой задачу Devin или видео для обновления статуса. Медиа можно открыть по ссылке; сохрани его до истечения ссылки.

Реальные вызовы расходуют лимиты провайдера. После тайм-аута проверь внешний сервис перед повторным запуском: принятую задачу нельзя отозвать на стороне панели. История лежит в локальной SQLite; `BRIDGE_DB` позволяет задать другой путь.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy app.py providers.py
```

Тесты не вызывают реальные API. [Документация для контрибьюторов](docs/INDEX.md).
