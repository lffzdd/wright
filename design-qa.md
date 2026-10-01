# Wright Web production integration QA — 2026-09-30

This current section supersedes the historical fixture-based evidence below.

- Source visual truth: `web/example.html` and `web/example.png` (2880 × 1920 pixels, normalized to the design's 1440 × 960 CSS viewport).
- Production route: `http://127.0.0.1:18765/`, served by Wright's real FastAPI app under WSL. Authentication, session creation, model calls, tool events and approval decisions used the normal APIs and WebSocket. No visual query, injected state, mocked network or alternate React page was used.
- Implementation: `web/qa/production-session-1440x960.png` and `web/qa/production-approval-1440x960.png`, 1440 × 960 pixels at 1×.
- Full comparison: `web/qa/production-reference-comparison.png` (2880 × 960 pixels). Source and production both show the dark desktop workspace with a pending shell approval. Production contains the real verification conversation; the reference contains its Redis example.
- Focused comparison: `web/qa/production-approval-detail.png`. Both approval cards were opened together for inspection.

## Findings and repairs

- Removed `VisualFixture`, `EmptyFixture`, their URL branches and Inspector's `staticData` bypass. Old visual queries now authenticate and render the production App, covered by regression tests.
- Consolidated Vite output and the server's static path into `src/wright/interfaces/web/static`; removed the duplicate legacy bundle. Production assets were rebuilt.
- Real execution exposed stale inspector metadata: context and accessed files stayed at zero until reload. The App now refreshes derived snapshot data after streamed events, retaining the live timeline and rejecting older snapshot watermarks.
- Real execution exposed duplicate final replies when model/completion transport turn ids differed. Both events now use the active turn's identity; the regression test retains one answer while preserving identical answers in distinct turns.
- Context category `share` describes a fraction of used tokens, not the window limit. Segment widths now use `tokens / limit`: the inspected production page uses 42.0k / 128k and fills approximately 33%, rather than filling the entire bar.
- Files directories now call the tree endpoint and support parent navigation. File reads and directory requests discard results after the owner changes.
- Sidebar terminal status now agrees with the main completed/failed/cancelled status instead of describing completed work as paused.
- Health polling now starts after authenticated bootstrap.

## Visual and interaction validation

- Typography: the production font stacks, compact labels, monospace commands and heading hierarchy follow the source; real Chinese content wraps within the same regions.
- Layout rhythm: measured header 1440 × 38; left rail 230 px, conversation 830 px, inspector 380 px; column height 922 px. Composer and inspector controls remain visible. Pending production approval is 793 × 225 px; the source is approximately 798 × 207 px. The real permission scope/risk copy requires additional wrapping.
- Colors/tokens: dark surfaces, subtle borders, purple accent, amber approval card, green completion and blue/amber context segments match the source's semantic roles.
- Assets: retained the existing Phosphor icon library and Wright identity. No raster illustrations are required by this workspace design. Source user/model/project identifiers remain sample content and are not transplanted into production.
- Copy/content: project, branch, task, model, token counts, file accesses, scope and available permission choices come from real data. Only choices supplied by the backend are shown; the source's session-grant button is absent when the backend offers only single-invocation approval and denial. Plans/subagents have honest empty states for this non-planning, non-delegated task.
- Primary flow tested: normal login → create actual session → receive real model/tool results → complete → reload and restore → browse `web/` and read `example.html` → run a print-only shell verification → pending permission card → Allow Once → shell output and final “验证完成” reply.
- Browser console: no warnings or errors in the inspected production tab.
- Automated validation: 51 frontend tests; production TypeScript/Vite build; 14 backend web-server tests under WSL.

## Limits

Native Windows backend startup currently fails on the existing POSIX-only `fcntl` imports; this frontend task did not change file-lock semantics. WSL was used for the actual backend validation. The repo's mounted Windows permissions also appear as preexisting Git mode changes to WSL, so the visible dirty count is environment-specific. No review acceptance or revert was performed.

At 1024 × 768, the initial inspector drawer previously overlapped the composer. Removed automatic narrow-drawer opening from saved desktop preferences. Post-fix evidence: `web/qa/production-1024x768.png`; inspector left edge 1043 px (offscreen), composer right edge 1014 px, both real conversation replies remain intact. The browser viewport override was reset after verification.

No actionable P0/P1/P2 design differences remain in the inspected dark desktop and narrow-screen states. Light-theme inspection was not repeated in this pass.

## 2026-09-30 Inspector tabs follow-up

**Final result: latest five-panel visual check is pending.** Task, Files, Changes, Permissions, and Context were all opened in an authentic Wright session. That session had a different goal and a repository with 500+ local changes; after reloading the current build (`index-BGXEakQ9.js`), Wright had zero active sessions. The earlier tab captures came from `index-DF5U5UCq.js` and do not certify the current source build. Browser policy also rejected opening the local `file://` reference, so its five tabs were compared from `web/example.html` source markup and styles. See [`web/qa/inspector-tabs-comparison.md`](web/qa/inspector-tabs-comparison.md) for per-tab findings and [`web/qa/backend-implementation-gaps.md`](web/qa/backend-implementation-gaps.md) for backend contracts to revisit.

final result: passed

---

# Historical Wright Web reference design QA (superseded)

## Target and evidence

- Source visual: `/Users/williamlao/Documents/Temp4Agent/agent_ui_designs/index.html`.
- Source screenshot: `web/qa/reference-current-1440x960.png`, 1440 × 960 px at 1×.
- Controlled comparison: `web/qa/implementation-fixture-current-1440x960.png`, 1440 × 960 px at 1×, generated from `/?visual=fixture`. This is component comparison evidence only.
- Real route: `uv run wright --ui web --no-open`, authenticated and opened at the normal `/` route. A fresh service instance rendered real historical session data at 1440 × 960 CSS px in English/dark mode; the latest temporary screenshot is `/tmp/wright-real-rich-session-1440-refined.png` (outside the repository because it contains private conversation data). It loaded `/assets/index-DKM7EPY4.js`, showed the four default recent entries, three real plan steps, three accessed files, 54 tool cards, 3 ms measured to `/api/v1/health`, a selected-workspace indicator, and no page errors. The existing in-app browser also runs the authentic route at a 639 × 892 CSS px viewport.
- `implementation-uv-run-1440x960.png` is **not** a real-route capture: its `TASK-104`/`deterministic-preview` content matches the `visual-fixture` sample. Treat it as fixture evidence only, despite the filename. The `/tmp` screenshot above is the separate authentic-route capture.

## Comparison history

- Fixture comparisons can guide region-level implementation, but neither `implementation-fixture-current-1440x960.png` nor `implementation-uv-run-1440x960.png` proves fidelity on the real route.
- The real Session Rail follows the reference order: Active Tasks, Recent Tasks, Workspaces, Capabilities. The source has four recent rows, so the real rail now defaults to four and offers the remaining sessions through `Show all`; the authenticated 1440 × 960 capture showed `Show all 36`. The current in-app route also confirms the expanded history list.
- Latest inspection caught an over-wide permission selector that shifted the composer control group left. The width is now 148 px; in the controlled 1440 px fixture the three selectors start at approximately x=537, x=623, and x=813, close to the source at x≈533, x≈621, and x≈812. Their background now uses the reference sidebar surface token.
- Inspector headings now follow the source's “Files accessed” and spawned-subagent count labels while displaying Wright's actual item counts.
- The Task tab's compact accessed-files card now shows three rows while retaining the total count, like the source summary; modified files receive green styling from their actual `+N` access metadata instead of whichever row happens to be first. Step metadata now uses readable title-case states and omits repeated status notes such as `completed · Completed`.
- The active execution-plan step now uses the source's 14 px amber ring spinner, and state dots use the source's subtle pulse scale. The Recent Tasks heading shows “Today” only when a real saved session is dated today; the controlled fixture supplies one current-date session for this comparison.
- Real execution-plan titles and notes now truncate to the source's compact single-line rows, while their full values remain available on hover. The same real session measured all three plan cards at 49 px high after the change.
- The permission-card primary action now matches the source's 12 px horizontal padding and dark shortcut keycap; its visible “Allow Once” label is localized by the UI language. The `@` file chip retains the source's visible close affordance; the slash-command chip now reveals its extra remove action only on hover or keyboard focus, matching the source's clean default appearance.
- The interface-language selector moved from the top bar into Settings, keeping localization while restoring space for the reference's status, theme, and Inspector controls.
- The in-app browser had retained an older real-route bundle (`index-VT9bGSGZ.js`) after the rebuilt app began serving `index-BLM8hk2A.js`. Reloading the authentic `/` route with a cache-busting query loaded the current UI: the top-bar language selector disappeared and Recent Tasks returned to five rows plus `Show all 35`. The SPA HTML shell now sends `Cache-Control: no-cache, must-revalidate`; a separate service instance returned that header for both `/` and `/index.html`. The existing long-running server has not been restarted, so it will adopt this response-header change on its next start.
- Sidebar workspace/capability rows now use the reference's 11 px text and medium selected weight. Section and card-kicker labels use the reference's 600 weight and 0.05 em tracking. The user asked to retain the current message typography/card spacing and keep Memory and Rules as separate rows.
- The selected workspace row now carries the source's right-aligned accent dot. The profile footer falls back to the configured model when the selected historical session has no model override, and shows measured service round-trip latency from the read-only health endpoint instead of a hard-coded sample. The authentic route showed the selected marker and 4 ms during the latest 1440 × 960 capture.
- Capability rows now show live counts from the selected session's memory/rules APIs and the selected project's schedules API. Missing or failed data stays hidden, and counts refresh after a capability dialog closes; Memory and Rules remain separate rows as requested.
- Composer attachment controls now follow the source order and semantics (paperclip for files, image glyph for images), and Agent/Plan/Ask options carry the same mode icons. The action bar's lower padding and Inspector tab spacing match the source's Tailwind dimensions more closely.
- Session rename/archive/close actions are grouped in the conversation heading's overflow menu. Workspace registration actions are in the Workspaces overflow menu instead of occupying every row. Both menus were opened and verified on the real `/` route; Escape closed the session menu.
- The permission header now uses the reference's 6 px vertical padding; “Agent paused” is 9.5 px monospace with an amber pulsing dot, and the permission kicker/metadata labels match the source sizes and tracking.
- The Permissions tab now displays the real session and user-wide additional-directory grants from `/grants`, and groups the returned enforcement boundaries in a compact checklist. The directory section stays hidden when no extra directories are configured.
- The Context tab translates every category returned by Wright's context estimator into readable English/Chinese labels and uses localized thousands separators for token counts.
- Permission-card shortcuts now resolve the real `allow_once` and `deny` choices through the same idempotent command path as the buttons. `Esc` continues to close overlays when no permission interaction is active.
- Session-state styling now represents the states exposed by Wright: permission/input wait, queued, running, cancelling, completed, cancelled, failed, and interrupted. The backend has no separate thinking/planning session states.
- Preserve the user's accepted deviations: current message typography/card spacing and separate Memory and Rules rows.

## Fidelity surfaces

- **Typography:** system/Geist fallback stack and antialiasing follow the reference. Keep message body size and card padding per the user's direction; keep Memory and Rules as separate rows.
- **Layout and rhythm:** reference and controlled fixture use the same 1440 × 960 viewport and principal 230/830/380 px rail/main/inspector widths. Composer control positions are close after the latest correction. The authenticated real route was captured at 1440 × 960 with actual plan and accessed-file data, and remains inspectable at 639 × 892 px.
- **Colors:** dark palette variables match the source values. The light theme no longer forces dark search, tool, permission, and composer surfaces. Context meter segments use category colors. The authentic 1440 × 960 capture verifies the rendered dark palette on the real route.
- **Assets and icons:** both designs use CSS/UI icons rather than raster art; Wright retains its existing Phosphor icons, so silhouettes differ slightly from the source SVGs.
- **Copy and content:** Wright branding, model names, session data, task counts, and actions are real product data. Do not hard-code source sample counts or its decorative audit checkbox. The user's accepted Memory/Rules split remains.

## Current verification

- Backend behavior audit is recorded in [`backend-implementation-gaps.md`](backend-implementation-gaps.md); this turn found no confirmed missing production backend behavior besides the conditional “Audit to session log” opt-out contract.
- `npm run build`: passed after the latest Composer chip refinement; output is recorded in `src/wright/web/static/index.html`. Vite reports the existing >500 KB JS chunk warning. `git diff --check` also passes.
- Tests were not run. The authentic `/` route was reloaded after the build at 808 × 892 CSS px with no page errors. The selected checkout currently has no recent sessions, so the page displays its real New conversation Composer; accessibility confirms the file/image button labels and Agent/Plan/Ask icons.
- The separate `/tmp/wright-real-rich-session-1440-refined.png` capture is an authenticated real session at 1440 × 960 with three plan steps and accessed-file data. It predates the latest Composer-only CSS change and does not contain a real pending permission or subagent state. Do not use fixture screenshots as product-route evidence.
- Workspace/session menus and the session-menu Escape behavior were previously opened on the authentic route. Settings, active permission approval, and subagent states still lack current real-route verification.

## Findings and next work

- **P1, verification gap:** real plan and accessed-file states are now verified at 1440 × 960, but the inspected saved sessions do not include an active permission request or subagent row. Those states still need verification using genuine session data; fixture captures remain comparison-only.
- **P2, remaining fidelity:** continue focused alignment of rail metadata, permission controls, icon silhouettes, and Inspector density against the source while retaining real product values and the user's accepted deviations.

final result: in progress
