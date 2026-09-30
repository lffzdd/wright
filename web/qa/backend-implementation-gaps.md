# Backend implementation gaps against the reference

Audit date: 2026-09-30  
Reference: `/Users/williamlao/Documents/Temp4Agent/agent_ui_designs/index.html`

This list is limited to behavior that needs a backend contract. Visual differences, browser-only interactions, and sample data are not backend gaps. The reference HTML is partly a static simulator, so its controls are checked against both its own JavaScript and Wright's routes/services before being classified.

## Candidate for later implementation

| Item | Evidence | Backend gap |
| --- | --- | --- |
| Make “Audit to session log” user-configurable | The reference renders a checked checkbox near the permission actions (`index.html`, around line 657) but does not attach a change handler or read the checkbox in its permission-resolution function. Wright's `InteractionMailbox` persists permission requests and resolutions through the autonomy store (`src/wright/application/composition/runtime.py`, `record_interaction` / `resolve_interaction`; `src/wright/infrastructure/persistence/autonomy_store/_interactions.py`). | Wright already records the decision audit. There is no API or persisted preference to opt out per request/session. If the checkbox is intended to control whether a decision is recorded, add a preference/command contract and make persistence conditional. The reference itself does not establish the desired scope or default beyond its static checked appearance. |

## Reference-only behavior, not a production backend gap

| Item | Evidence | Classification |
| --- | --- | --- |
| Eight-state “Simulate” selector and simulated permission/stop transitions | The reference calls `changeAgentState`, `resolvePermission`, and `stopTask` directly in browser JavaScript (around lines 1107–1206); these functions mutate DOM classes/text and use sample PIDs. No backend request is made. | Demo-only state simulation. Wright displays actual session state from backend snapshots/events; adding an endpoint to fake execution states would not reproduce a missing production behavior. |

## Checked and already supported

- Permission decisions and durable pending/resolved interaction records: `InteractionMailbox`, `RuntimeManager`, and the autonomy-store interaction methods.
- Stop/cancel, model selection, interaction/permission modes, turn submission, session create/label/archive/close, attachments and artifacts: Web routes and session runtime APIs.
- Workspaces, file/tree/search/reference access, changes/review, grants/directories, memory, rules, and schedules: `workspace_routes.py` endpoints.
- Plan, accessed files, subagents, session status, and context estimates: real snapshot/event data consumed by the Inspector. The HTML's sample values are not backend requirements.

No other missing backend endpoint was confirmed in this pass. Revisit this inventory if the reference gains an actual server-backed behavior or if the audit toggle's intended semantics are decided.
