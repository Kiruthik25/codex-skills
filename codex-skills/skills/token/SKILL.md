---
name: token
description: Report recorded token usage and estimated context composition for the current Codex session. Use for $token, a standalone token request, or an explicit request for Codex session token accounting.
---

Run `python ~/.agents/skills/token/scripts/token_report.py` from the project directory. On PowerShell use `python (Join-Path $HOME '.agents/skills/token/scripts/token_report.py')`. Preserve the session environment. If selection is ambiguous, use `--session-id` with a verified current thread ID or ask for the ID.

Present the numeric report with its availability and estimate labels. Keep the analysis local and avoid opening or quoting raw logs. Never compact or start a session automatically. Consult [token accounting](references/token-accounting.md) when interpreting missing data or investigating a schema change.
