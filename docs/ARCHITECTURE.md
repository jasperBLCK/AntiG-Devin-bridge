# Architecture

The browser calls a single-origin FastAPI service. SQLite stores the task, state, result, and provider ID. Task creation returns 202, then a background worker makes the provider call. On restart an unfinished submission is marked uncertain rather than retried. The selected task polls pending Devin sessions and videos via a server-side refresh; navigating away does not stop an already submitted remote operation.

Provider endpoints are fixed and only receive their own server-side key. Provider output is text or an HTTPS URL; it is not evaluated as HTML or code. A manual handoff uses a stored completed Grok plan as reference data and does not launch Devin until a separate submission. Media URLs can expire; assets are not downloaded to the server.
