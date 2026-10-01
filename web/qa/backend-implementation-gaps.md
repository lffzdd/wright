# Backend implementation gaps against the reference

Audit date: 2026-09-30; implementation updated: 2026-10-01.

Reference: `/Users/williamlao/Documents/Temp4Agent/agent_ui_designs/index.html`

This list is limited to behavior that needs a backend contract. Visual differences, browser-only interactions, and sample data are not backend gaps. The reference HTML is partly a static simulator, so its controls are checked against both its own JavaScript and Wright's routes/services before being classified.

## Implemented contracts and product decisions

| Item | Status | Implementation |
| --- | --- | --- |
| Return edit diff line coordinates | Implemented | `infrastructure/tools/file/diff.py` compares the actual pre/post edit text. Successful `edit_file` returns `diff`, `additions`, `deletions`, and `diff_truncated`, with file headers, hunk positions, three context lines and missing-final-newline markers. UTF-8 display output is bounded to 64 KiB at complete rows with an explicit truncation marker; counts use the entire diff. Raw file I/O preserves CRLF and final-newline state. `web/src/diff.ts` supplies the shared parser for edit cards and change review; live events, reconnect snapshots and persisted history use the same structured result. |
| Expose plan step timing and wait detail | Implemented | `domain/model/planning/plan.py` owns `started_at` / `ended_at` (Unix seconds), records first start and terminal transitions, and retains timestamps on repeated updates and replan. `application/planning/projection.py` computes wall-clock `elapsed_ms` and derives `wait_reason` from effective mailbox requests with principal/task ownership. Unassociated or child requests appear at session level. Requests, resolution and cancellation publish `plan.updated`; reconnect rebuilds from live requests. History uses the checkpoint save time and does not keep ticking. No start means no invented duration. |
| Expose effective permission categories | Implemented | `PermissionPolicy.summarize` reuses the actual evaluator for rule-free base decisions and publishes applicable deny/ask/allow conditions, approved session directories, precedence and hard constraints for File Read, File Write and Shell Execution. The grants API exposes `effective_policy`. Bypass retains protected-file, deny/ask and interaction-mode ceilings; shell permission is explicitly not a filesystem sandbox. The permission Inspector consumes the summary and refreshes after policy changes, authorization changes and reconnect. The summary issues no grant and executes no tool. |
| Make “Audit to session log” user-configurable | Product decision: always recorded | Permission requests and decisions continue through `InteractionMailbox` and the existing durable autonomy store (`composition/runtime.py`, `record_interaction` / `resolve_interaction`; `persistence/autonomy_store/_interactions.py`). The approval card states that decisions are recorded. No opt-out preference or conditional audit persistence is added. The reference checkbox was static and did not establish a production contract. |

The implementation keeps canonical plan state in the domain, derives display state in the application, and leaves transport/rendering to Web adapters. `session.policy_updated` is emitted after mode changes or authorization commits so a permission refresh cannot race ahead of grant persistence. No database migration, checkpoint major-version change, simulated-state endpoint or old-version compatibility layer is introduced.

## Verification

- `tests/test_backend_gaps.py`: diff positions, multi-hunk and multi-line changes, CRLF and final newlines, unchanged/failed edits, bounded output and complete statistics; controlled-clock plan transitions, replan and checkpoint observation; real mailbox requests/resolution/cancellation and root/child isolation; policy defaults across all modes and interaction ceilings, conditional rules and actual approved directory scope.
- The scripted Web session test runs real plan/file tools and durable permission handling against an offline response script. It verifies HTTP grants, permission wait events, WebSocket reconnect snapshots, approval audit, real file bytes, structured live/historical diff and frozen history timing. No model service is contacted.
- `web/src/workspace/backend-gaps.test.tsx`: shared live/snapshot rendering, hunk line offsets and blank lines, audit/truncation messages, live/frozen timing, authoritative wait clearing, conditional policy presentation and refresh.
- Production frontend assets are rebuilt from the TypeScript frontend. Related Python suites, frontend tests, type checking and build are run; local Windows limitations are reported separately when a test requires symlink privileges or POSIX process-group APIs.

Local validation on 2026-10-01: **288 Python tests passed**, including architecture boundary checks and the offline scripted session; **58 frontend tests passed**; TypeScript checking, production build and lint passed. Three existing tests were first run and then excluded from the final regression command: `test_symlink_write_is_judged_by_the_canonical_target` and `test_paths_reject_escape_and_symlink` require Windows symlink privileges (WinError 1314), and `test_glob_and_grep_have_distinct_structured_results` reaches the existing POSIX-only `os.getpgid` process-group code. These platform limitations remain outside the three contracts implemented here.

## Reference-only behavior, not a production backend gap

| Item | Evidence | Classification |
| --- | --- | --- |
| Eight-state “Simulate” selector and simulated permission/stop transitions | The reference calls `changeAgentState`, `resolvePermission`, and `stopTask` directly in browser JavaScript (around lines 1107–1206); these functions mutate DOM classes/text and use sample PIDs. No backend request is made. | Demo-only state simulation. Wright displays actual session state from backend snapshots/events; adding an endpoint to fake execution states would not reproduce a missing production behavior. |

## Checked and already supported

- Permission decisions and durable pending/resolved interaction records: `InteractionMailbox`, `RuntimeManager`, and the autonomy-store interaction methods.
- Stop/cancel, model selection, interaction/permission modes, turn submission, session create/label/archive/close, attachments and artifacts: Web routes and session runtime APIs.
- Workspaces, file/tree/search/reference access, changes/review, grants/directories, memory, rules, and schedules: `workspace_routes.py` endpoints.
- Plan, accessed files, subagents, session status, and context estimates: real snapshot/event data consumed by the Inspector. The HTML's sample values are not backend requirements.

The three confirmed contracts above are implemented. No other missing backend endpoint was confirmed in this pass; a full-site audit remains outside this change.
