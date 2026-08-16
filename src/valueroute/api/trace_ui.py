"""Read-only runtime-trace UI (design section 18.3, FR-205).

Serves a self-contained HTML page that aggregates already-public store state:
controller sessions, epochs (including automatic selection and switches),
tasks, worker attempts, usage totals, shadow records, and integration
attempts.  It is read-only by construction: it only reads the in-memory
journal-backed store and never accepts input or exposes secrets.
"""

from __future__ import annotations

import html
from typing import Any

from valueroute.domain.models import ControllerEpoch, ControllerSession, ParentTask, TaskStatus, WorkerAttempt


def _session_rows(sessions: dict[str, ControllerSession], epochs: dict[str, ControllerEpoch]) -> str:
    rows = []
    for session in sorted(sessions.values(), key=lambda item: item.created_at):
        epoch = epochs.get(session.active_controller_epoch_id) if session.active_controller_epoch_id else None
        epoch_desc = (
            f"{html.escape(epoch.provider_id)}/{html.escape(epoch.model_id)} ({html.escape(epoch.status)})"
            if epoch is not None
            else "none"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(session.id)}</td>"
            f"<td>{html.escape(session.orchestration_mode.value)}</td>"
            f"<td>{epoch_desc}</td>"
            f"<td>{session.version}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _task_rows(tasks: dict[str, ParentTask]) -> str:
    rows = []
    for task in sorted(tasks.values(), key=lambda item: item.created_at):
        rows.append(
            "<tr>"
            f"<td>{html.escape(task.id)}</td>"
            f"<td>{html.escape(task.controller_session_id)}</td>"
            f"<td>{html.escape(task.status.value)}</td>"
            f"<td>{html.escape(task.request_type)}</td>"
            f"<td>{len(task.child_task_ids)}</td>"
            f"<td>{task.version}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _attempt_rows(attempts: dict[str, WorkerAttempt]) -> str:
    rows = []
    for attempt in sorted(attempts.values(), key=lambda item: item.created_at):
        status = attempt.status.value if hasattr(attempt.status, "value") else str(attempt.status)
        rows.append(
            "<tr>"
            f"<td>{html.escape(attempt.id)}</td>"
            f"<td>{html.escape(attempt.child_task_id)}</td>"
            f"<td>{html.escape(status)}</td>"
            f"<td>{html.escape(attempt.worker_session_id)}</td>"
            f"<td>{attempt.version}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _epoch_rows(epochs: dict[str, ControllerEpoch]) -> str:
    rows = []
    for epoch in sorted(epochs.values(), key=lambda item: item.activated_at):
        rows.append(
            "<tr>"
            f"<td>{html.escape(epoch.id)}</td>"
            f"<td>{html.escape(epoch.controller_session_id)}</td>"
            f"<td>{html.escape(epoch.provider_id)}/{html.escape(epoch.model_id)}</td>"
            f"<td>{html.escape(epoch.status)}</td>"
            f"<td>{epoch.version}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _shadow_rows(shadow_records: dict[str, Any]) -> str:
    rows = []
    for record in sorted(shadow_records.values(), key=lambda item: item.created_at):
        advice = record.advice
        suggestion = ", ".join(f"{c.mode}={c.worker_count}" for c in advice.candidates)
        rows.append(
            "<tr>"
            f"<td>{html.escape(record.id)}</td>"
            f"<td>{html.escape(record.status)}</td>"
            f"<td>{html.escape(advice.boundary_decision.request_type)}</td>"
            f"<td>{html.escape(suggestion)}</td>"
            f"<td>{html.escape(record.real_outcome_ref or '')}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_trace_page(store: Any) -> str:
    sessions = getattr(store, "sessions", {})
    epochs = getattr(store, "epochs", {})
    tasks = getattr(store, "tasks", {})
    attempts = getattr(store, "attempts", {})
    shadow_records = getattr(store, "shadow_records", {})

    running = sum(1 for task in tasks.values() if task.status in {TaskStatus.running, TaskStatus.queued})
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ValueRoute runtime trace</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; font-size: 0.85rem; }}
  th, td {{ border: 1px solid #ddd; padding: 0.35rem 0.5rem; text-align: left; }}
  th {{ background: #f5f5f5; }}
  .summary {{ color: #555; font-size: 0.9rem; }}
</style>
</head>
<body>
<h1>ValueRoute runtime trace</h1>
<p class="summary">
  {len(sessions)} session(s), {len(epochs)} epoch(s), {len(tasks)} task(s),
  {len(attempts)} attempt(s), {running} running, {len(shadow_records)} shadow record(s).
  Read-only view; refresh to update.
</p>

<h2>Sessions</h2>
<table>
  <tr><th>id</th><th>mode</th><th>active epoch</th><th>version</th></tr>
  {_session_rows(sessions, epochs) or '<tr><td colspan="4">none</td></tr>'}
</table>

<h2>Epochs</h2>
<table>
  <tr><th>id</th><th>session</th><th>controller</th><th>status</th><th>version</th></tr>
  {_epoch_rows(epochs) or '<tr><td colspan="5">none</td></tr>'}
</table>

<h2>Tasks</h2>
<table>
  <tr><th>id</th><th>session</th><th>status</th><th>type</th><th>children</th><th>version</th></tr>
  {_task_rows(tasks) or '<tr><td colspan="6">none</td></tr>'}
</table>

<h2>Worker attempts</h2>
<table>
  <tr><th>id</th><th>child</th><th>status</th><th>worker session</th><th>version</th></tr>
  {_attempt_rows(attempts) or '<tr><td colspan="5">none</td></tr>'}
</table>

<h2>Shadow records</h2>
<table>
  <tr><th>id</th><th>status</th><th>request type</th><th>suggestions</th><th>real outcome</th></tr>
  {_shadow_rows(shadow_records) or '<tr><td colspan="5">none</td></tr>'}
</table>
</body>
</html>
"""


__all__ = ["render_trace_page"]
