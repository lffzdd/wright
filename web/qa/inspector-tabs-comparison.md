# Inspector tab comparison

Date: 2026-09-30  
Source: `web/example.html` (all five tab sections and `switchTab`)  
Implementation: `web/src/workspace/widgets.tsx`, `web/src/styles.css`, live Wright at `http://127.0.0.1:18765/`

## Result

**Result: all five tabs were opened in a real Wright session, but the latest build is not yet visually verified.** I inspected Task, Files, Changes, Permissions, and Context in the authentic route at `http://127.0.0.1:18765/`, then reloaded after the current build; the page now reports zero active sessions. The tab screenshots before reload came from the previously loaded bundle `index-DF5U5UCq.js`, while the current build is `index-BGXEakQ9.js`, so those captures are useful for comparing production behavior but do not certify the latest source changes. The browser rejected opening the local `file://` reference; the five source tab sections, switch logic, React components, CSS, API contracts, and backend models were compared from their source.

The existing `production-reference-comparison.png` does not show equivalent Task content: its source side is the sample rate-limiter task, while Wright was showing a read-only design-inspection task. Do not use that image to judge Task content or spacing. The live Task tab I opened likewise had no active plan and a different goal. The source's sample copy is illustrative; Wright continues to render the current session's actual data.

## Findings by tab

| Tab | Reference HTML | Wright implementation | Action / status |
| --- | --- | --- | --- |
| Task | Goal, progress, execution steps, recent files, and subagents. Sample steps include elapsed/completion times and an approval wait explanation. | Opened in the real session: a different goal, no plan, accessed files, no subagents. | Task text and density cannot be directly compared with the rate-limiter example. File labels now render as `READ` / `MOD`; line totals use Git patches when available. Missing plan timing and wait metadata is in `backend-implementation-gaps.md`. |
| Files | Recent resource rows with `MOD` / `READ` labels, paths, sizes, and changed-line totals. | The real tab was opened while its workspace-browser mode was active and showed the directory tree. The current source now defaults to the accessed-file list with a workspace-browser toggle. | Existing access records, tree sizes, and Git patches support the reference details; the new default view still needs a fresh live-session check. |
| Changes | A compact changed-file summary with add/delete counts and a sample explanation. | The real tab showed local working-tree changes and Accept/Revert controls, with 500+ items in this repository state. | The data set is not comparable with the single-file sample. Preserve the real review flow; do not synthesize its illustrative explanation. Patch listing/reading is supported. |
| Permissions | Three illustrative effective-policy cards for read, write, and shell. | The real tab showed default Agent mode and four enforced-boundary statements. | The backend does not expose effective policy grouped by operation/resource; that contract gap is recorded in `backend-implementation-gaps.md`. Keep policy claims grounded in returned data. |
| Context | Four illustrative categories and a total/limit. | The real tab showed nine live estimate categories, token counts, estimate note, and separate billing usage. | This is richer live data than the static sample; no backend gap was found. |

## Follow-up for visual completion

Re-run the five-panel comparison against the current bundle after a real Wright session is available and the source HTML can be opened in the browser (or a rendered reference for all tabs is supplied). Use equivalent session/task state and viewport on both sides. The four tabs were opened in a prior authentic session, but its bundle predates the current build and its workspace/task state differs from the reference.
