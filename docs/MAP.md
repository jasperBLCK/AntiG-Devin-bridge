# Map

- `app.py`: settings, task validation, SQLite, dispatch, media confirmation and polling.
- `orchestration.py`: route contract, role selection, demo routing and scoped handoff.
- `providers.py`: AntiG text/media and Devin v3 status, messages and structured reports.
- `repository.py`: GitHub tree, pinned source files and verified PR metadata.
- `workspace.py`: immutable GitHub snapshots and signed file lookups; workspace routes in `app.py` create snapshots and validate optional branch publication.
- `skills_market.py`: public skill metadata, browser libraries, reviews and agent instruction packs.
- `static/`: task form, history and session editor.
- `tests/`: mocked workflows and demo behavior.
- Settings and cleanup: `/api/settings` (runtime Grok keys, needs `BRIDGE_ADMIN_KEY`), `/api/jobs/clear` (hides finished jobs, keeps usage) in `app.py`; UI in `static/settings.html`, `static/settings.js`.
- Custom skills: `/api/skills/custom` in `skills_market.py`; built-in Grok + Devin recipes in `pair_skills.py`.
- Token economy: local media routing (`explicit_media`) and budget rules for the router and Devin handoff in `orchestration.py`.
- Project media: router `assets`, SQLite pipeline, approval and video polling in `app.py`; attachment upload in `providers.py`.
