# Architecture

The browser calls a single-origin FastAPI service. SQLite stores the task, routing decision, provider ID, activity and result. Submission returns 202. A background call to Grok produces a validated route; no side-effecting call runs on an invalid response. A single process owns the polling loop, reading pending Devin and video jobs independent of browser selection. On restart uncertain launches and report reviews are not automatically replayed.

Repository tasks launch at most one Devin session. The original request and Grok plan are scoped to the selected repository and optional publication permission. For opted-in jobs, Grok reads a bounded structured report after Devin finishes. No follow-up write is triggered by its output. Media needs a separate atomic confirmation. Provider endpoints are fixed and each receives only its own server-side key; provider text is never evaluated.

GitHub browsing previews file text and re-reads only the approved paths at the pinned SHA for Grok. Devin receives repo, ref, SHA and the plan, not the GitHub token or file bundle. Actual branch/file telemetry comes from a GitHub PR in the selected repo; in-session activity comes from Devin's messages and status detail, not inferred commands or live file handles.
