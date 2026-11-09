# Changes: requirements and integration contract

Deliver one complete review workflow in the existing Wright inspector. Keep
existing themes and the session/execution architecture. Do not publish or push
anything as part of implementation or verification.

## User behavior

- The panel has two views. The default Git view lists uncommitted changes in
  two groups: `staged` (index compared with HEAD) and `unstaged` (disk compared
  with the index, plus untracked files). A partially staged file appears in
  both groups, each showing and acting on its own part. The `session` view
  compares recorded task writes to their first before snapshot and is never
  mixed with Git changes.
- Each group reads its scope separately and keeps that scope's revision. A
  file's selected diff and actions use the group it was selected from: the
  staged group unstages and commits; the unstaged group stages and discards
  (back to the staged version). The `working` scope stays in the API for
  callers that need one combined list, but the panel does not act on it.
- The view's file count counts distinct paths, so a partially staged file
  counts once.
- New folders list individual files. Rename metadata preserves both paths.
- Empty, failed, unsupported, binary and oversized states are distinct.
- Keeping edits needs no Accept action. Git staging is explicitly named.
- File/hunk stage, unstage and discard operate on the selected group's Git scope.
  Session undo restores task-before bytes without changing the Git index/HEAD.
- Mutations validate an expected revision, reject busy sessions, and do not
  silently overwrite external edits. Batch undo/discard preflight every path
  before changing any path. Protect symlinks and paths outside the execution root.
- Commit uses the staged scope and a nonempty user-entered message. No push.
- File and line feedback adds context to the existing composer draft. It does
  not automatically submit a turn.
- Refresh, session switches and resume preserve ownership; stale requests do not
  update another session or scope. Task history does not claim Git is dirty.

## Git API (owned by main agent)

`GET /sessions/{id}/changes?scope=working|unstaged|staged`

Returns `{scope, baseline, environment, local_warning, git, revision, changes}`.
Each change has `{path, old_path?, status, untracked, staged, unstaged,
additions, deletions, binary, truncated, size}`. `revision` is an opaque token
covering the actual current Git/index/file state of this scope.

`GET /sessions/{id}/changes/{path}?scope=working|unstaged|staged`

Returns the same file fields plus `{patch, revision}`. Patch paths are relative.

`POST /sessions/{id}/changes/actions`

Body `{action: stage|unstage|discard|commit, scope, paths, expected_revision,
hunk?: number, message?: string, confirm?: boolean}`. Hunk is a zero-based index
into the exact current server diff; one path is required for a hunk action.
Discard and commit require `confirm:true`. Commit commits the existing index;
it never stages additional files. Return `{applied, results?, error?}`; stale or
busy operations fail with a descriptive API error. Capabilities/unsupported
file-hunk combinations must fail safely rather than approximate.

## Session API (owned by journal agent)

Keep `GET /sessions/{id}/review` and `POST /review/revert`. Add
`GET /sessions/{id}/review/patch?path=...` (place before generic/competing routes).

Review returns `{changes, revision, ...}` with existing fields plus
`additions`, `deletions`, `binary`, `truncated`, and `can_revert`/`reason`.
Current file hashes must be read for journal-known paths even when Git is clean.
Session patch returns `{path, patch, binary, truncated, revision}` using stored
task-before/task-after snapshots, never HEAD. Conflict/history is explicitly
represented and not rewritten as task ownership. Revert accepts
`{confirm:true, paths, expected_revision}` and validates it. Existing accept API
can stay internal, but the new UI does not expose review markers as acceptance.
Use shared directory coordination and file locks for mutations; preserve index,
HEAD, unrelated files and preexisting user bytes. Add busy and stale protection.

## Ownership

- Main: this contract, application Git changes service, diff facade, routes.py,
  Git tests, integration, live acceptance.
- Journal agent: journal.py, domain review.py, session_commands review operation,
  workspace_routes.py review endpoints, journal tests.
- Frontend agent: Inspector and Changes components, api.ts/types.ts, DiffViewer,
  scoped styles/i18n, composer draft feedback wiring, frontend tests.
- Acceptance agent: independent integration scenarios and review. Do not edit
  production files owned by implementers without agreement.

## Acceptance

Test real temporary Git repositories and actual bytes/index/HEAD: dirty-before
task undo, later user/session edits, staged and unstaged same-file changes,
commit without false conflict, new folders, staged/unstaged renames, hunk actions,
batch failure without partial writes, stale requests, missing worktree and resume.
Run scoped and relevant full tests/typecheck/build. Finally build and inspect the
real application in the browser at normal and narrow widths. Mock component tests
are useful but do not replace API/Git integration or rendered UI verification.

## Safety boundaries

Safety boundaries are explicit in the UI: binary/oversized or missing snapshots,
external changes, committed history, and legacy records without a verifiable
Git baseline cannot use session undo. Git hunk actions require a supported text
diff; file actions never approximate a failed hunk. Scope validation and version
checks reject stale operations. No push or external publication is performed.
