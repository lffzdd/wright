"""Due-run materialization and web probe operations."""

from __future__ import annotations

import time
from typing import Any

from ....domain.model.automation import AutomationRecord
from ._base import AutonomyStoreError, _StoreBase
from ._helpers import _dump, _hash_payload, _load_object


class _SchedulingMixin(_StoreBase):
    def materialize_due(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else float(now)
        created: list[str] = []
        with self._write():
            rows = self._conn.execute(
                """SELECT * FROM automations
                   WHERE session_id = ? AND status = 'active'
                   ORDER BY created_at""",
                (self.session_id,),
            ).fetchall()
            for row in rows:
                automation = self._automation_from_row(row)
                trigger = automation.trigger
                if trigger.type in {"once", "interval"}:
                    due = automation.next_run_at
                    if due is None or due > now:
                        continue
                    created_run = False
                    if not self._has_live_run_locked(automation.id):
                        created.append(self._insert_run_locked(
                            automation,
                            scheduled_for=due,
                            trigger_payload={"scheduled_for": due},
                            occurrence_key=f"{trigger.type}:{due:.6f}",
                            now=now,
                        ))
                        created_run = True
                    if trigger.type == "once":
                        self._conn.execute(
                            """UPDATE automations
                               SET status = 'completed', next_run_at = NULL,
                                   last_run_at = ?, updated_at = ? WHERE id = ?""",
                            (now, now, automation.id),
                        )
                    else:
                        next_run = self._advance_interval(
                            due, float(trigger.every_seconds), now
                        )
                        self._conn.execute(
                            """UPDATE automations SET next_run_at = ?,
                               updated_at = ?,
                               last_run_at = CASE WHEN ? THEN ? ELSE last_run_at END
                               WHERE id = ?""",
                            (
                                next_run, now, int(created_run), now,
                                automation.id,
                            ),
                        )
                elif trigger.type == "file_change":
                    snapshot = self._file_snapshot(trigger.path)
                    if snapshot == automation.trigger_state:
                        continue
                    # Coalesce, but do not lose, a change while the previous
                    # run is live. Keeping the old baseline makes the next poll
                    # materialize one follow-up after that run terminates.
                    if self._has_live_run_locked(automation.id):
                        continue
                    self._conn.execute(
                        """UPDATE automations SET trigger_state_json = ?,
                           updated_at = ?, last_run_at = ? WHERE id = ?""",
                        (_dump(snapshot), now, now, automation.id),
                    )
                    created.append(self._insert_run_locked(
                        automation,
                        scheduled_for=now,
                        trigger_payload={
                            "path": trigger.path,
                            "before": automation.trigger_state,
                            "after": snapshot,
                        },
                        occurrence_key=f"file:{snapshot.get('mtime_ns')}:{snapshot.get('size')}",
                        now=now,
                    ))

            events = self._conn.execute(
                """SELECT * FROM external_events
                   WHERE session_id = ? AND consumed_at IS NULL
                   ORDER BY id LIMIT 100""",
                (self.session_id,),
            ).fetchall()
            for event in events:
                matching = self._conn.execute(
                    """SELECT * FROM automations
                       WHERE session_id = ? AND status = 'active'
                         AND trigger_type = 'event'""",
                    (self.session_id,),
                ).fetchall()
                for row in matching:
                    automation = self._automation_from_row(row)
                    if automation.trigger.event_name != event["name"]:
                        continue
                    payload = {
                        "event_id": int(event["id"]),
                        "event_name": str(event["name"]),
                        "payload": _load_object(event["payload_json"]),
                    }
                    # Coalesce, but do not lose, events while a previous run
                    # is live. The event is consumed so it cannot be replayed
                    # from the log; pending_event holds the merged follow-up.
                    if self._has_live_run_locked(automation.id):
                        self._note_pending_event_locked(
                            automation, payload, now
                        )
                        continue
                    created.append(self._insert_run_locked(
                        automation,
                        scheduled_for=float(event["created_at"]),
                        trigger_payload=payload,
                        occurrence_key=f"event:{int(event['id'])}",
                        now=now,
                    ))
                    self._conn.execute(
                        """UPDATE automations SET last_run_at = ?, updated_at = ?
                           WHERE id = ?""",
                        (now, now, automation.id),
                    )
                self._conn.execute(
                    "UPDATE external_events SET consumed_at = ? WHERE id = ?",
                    (now, int(event["id"])),
                )
            self._flush_pending_events_locked(created, now)
        return created

    def list_due_web_probes(
        self, *, now: float | None = None
    ) -> list[AutomationRecord]:
        """Read due probes; network work intentionally happens outside DB locks."""
        now = time.time() if now is None else float(now)
        with self._read():
            rows = self._conn.execute(
                """SELECT * FROM automations
                   WHERE session_id = ? AND status = 'active'
                     AND trigger_type = 'web_change'
                     AND next_run_at <= ?
                   ORDER BY next_run_at LIMIT 20""",
                (self.session_id, now),
            ).fetchall()
        return [self._automation_from_row(row) for row in rows]

    def record_web_probe(
        self,
        automation_id: str,
        snapshot: dict[str, Any],
        *,
        now: float | None = None,
    ) -> str | None:
        snapshot_json = _dump(snapshot)
        if len(snapshot_json) > 20_000:
            raise AutonomyStoreError("web probe snapshot exceeds 20000 chars")
        now = time.time() if now is None else float(now)
        with self._write():
            row = self._conn.execute(
                """SELECT * FROM automations
                   WHERE id = ? AND session_id = ? AND status = 'active'
                     AND trigger_type = 'web_change'""",
                (automation_id, self.session_id),
            ).fetchone()
            if row is None:
                return None
            automation = self._automation_from_row(row)
            previous = automation.trigger_state.get("snapshot")
            next_run = now + float(automation.trigger.every_seconds or 0)
            changed = previous is not None and previous != snapshot
            live = self._has_live_run_locked(automation.id)
            # As with file changes, preserve the old baseline while a previous
            # run is live so one follow-up change is eventually delivered.
            effective_snapshot = previous if changed and live else snapshot
            state = {"snapshot": effective_snapshot, "last_error": ""}
            run_id: str | None = None
            if changed and not live:
                run_id = self._insert_run_locked(
                    automation,
                    scheduled_for=now,
                    trigger_payload={
                        "url": automation.trigger.url,
                        "before": previous,
                        "after": snapshot,
                    },
                    occurrence_key=f"web:{_hash_payload(snapshot)}",
                    now=now,
                )
            self._conn.execute(
                """UPDATE automations
                   SET trigger_state_json = ?, next_run_at = ?, updated_at = ?,
                       last_run_at = CASE WHEN ? THEN ? ELSE last_run_at END
                   WHERE id = ?""",
                (_dump(state), next_run, now, int(run_id is not None), now, automation.id),
            )
            return run_id

    def defer_web_probe(
        self,
        automation_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else float(now)
        with self._write():
            row = self._conn.execute(
                """SELECT * FROM automations
                   WHERE id = ? AND session_id = ? AND status = 'active'
                     AND trigger_type = 'web_change'""",
                (automation_id, self.session_id),
            ).fetchone()
            if row is None:
                return
            automation = self._automation_from_row(row)
            state = dict(automation.trigger_state)
            state["last_error"] = str(error)[:1_000]
            next_run = now + float(automation.trigger.every_seconds or 0)
            self._conn.execute(
                """UPDATE automations SET trigger_state_json = ?,
                   next_run_at = ?, updated_at = ? WHERE id = ?""",
                (_dump(state), next_run, now, automation.id),
            )
