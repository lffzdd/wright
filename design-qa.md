# Wright Web reference-design QA

**Source visual truth**

- HTML: `/Users/williamlao/Documents/Temp4Agent/agent_ui_designs/index.html`
- Source screenshot: `/Users/williamlao/Documents/Temp4Agent/agent_ui_designs/preview.png`
- Normalized source: `/Users/williamlao/Project/wright/web/qa/reference-1440x960.png`

**Implementation evidence**

- Route: `http://127.0.0.1:4173/?visual=fixture`
- Browser-rendered screenshot: `/Users/williamlao/Project/wright/web/qa/implementation-1440x960.png`
- Full comparison, reference left and implementation right: `/Users/williamlao/Project/wright/web/qa/comparison-reference-left-implementation-right.png`
- Center timeline comparison: `/Users/williamlao/Project/wright/web/qa/comparison-focus-center.png`
- Inspector comparison: `/Users/williamlao/Project/wright/web/qa/comparison-focus-inspector.png`
- Difference image: `/Users/williamlao/Project/wright/web/qa/comparison-difference.png`

**Normalization and state**

- Viewport: 1440 x 960 CSS px.
- Source: 2880 x 1920 px at 2x, downsampled to 1440 x 960 for comparison.
- Implementation: 1440 x 960 px browser capture at 1x.
- State: dark theme, active rate-limiter task, failed shell test, file edit, waiting permission, Task inspector tab, inspector open, read-directory card collapsed.
- Responsive checks: 1280 x 800 and 1024 x 768.

**Findings**

- No actionable P0, P1, or P2 fidelity findings remain.
- Fonts and typography: the implementation uses the source system/Geist-style fallback stack, compact 9.5-12.5 px hierarchy, monospace metadata, matching truncation, and the same dense line rhythm. The 1x browser capture is slightly less crisp than the source's downsampled 2x capture; this is density normalization, not a CSS mismatch.
- Spacing and layout: the 38 px top bar, 230 px session rail, flexible center workspace, 380 px inspector, 46 px task header, timeline indents, tool-card density, approval grid, and bottom composer align with the source. At 1280 px the rail and inspector contract while controls remain usable; at 1024 px they become off-canvas panels and the center workflow remains usable.
- Colors and visual tokens: dark and light palettes, accent, border, terminal, success, warning, danger, and muted tokens match the reference treatment. Waiting-permission state remains visually dominant.
- Image quality and assets: the source contains no raster product imagery. UI icons use the existing Phosphor icon set rather than custom SVG or placeholder glyph art. The source preview remains the only raster reference.
- Copy and content: the deterministic fixture reproduces the reference task, branch, prompt reference, tool outcomes, diff counts, plan, files, subagent, and approval semantics. Production-facing Wright branding and real backend values intentionally replace the mock Nexus identity and static values.
- Interactions and accessibility: verified tool expand/collapse, Task/Files inspector tabs, inspector show/hide, theme toggle, semantic buttons/tabs/labels, keyboard focus styling, and reduced-motion handling. The responsive production shell retains the session-rail trigger. A stale hot-reload console entry from an intermediate missing icon import was observed; after correction and full reload the complete accessibility tree rendered and the final build/tests were clean.

**Comparison history**

1. Initial baseline: blocked by P1 structural drift. The top bar omitted reference chrome and status controls; generic tool cards, the approval surface, the left footer, and inspector density did not reproduce the selected design.
2. First implementation pass: fixed the three-region shell, top chrome, session rail, event-specific read/shell/edit cards, command approval with Reason/Scope/Risk, composer, and Task inspector. P2 findings remained in tool metadata, context formatting, inspector vertical rhythm, and task-header wrapping.
3. Second implementation pass: added real edit counts, compact token formatting, file/subagent density, pending-permission tab styling, deterministic reference content, responsive/light checks, and task-heading truncation. Post-fix full and focused comparisons show no remaining P0/P1/P2 differences.

**Primary interactions tested**

- Expand and collapse the directory-read tool card.
- Switch Task and Files inspector tabs.
- Hide and restore the inspector.
- Toggle dark and light themes.
- Render at 1440 x 960, 1280 x 800, and 1024 x 768.

**Validation**

- `npm run build`: passed.
- `npm test -- --run`: 5 files, 35 tests passed.

**Follow-up polish**

- P3: the production composer intentionally keeps a visible Send action beside Stop so Wright's queued-instruction capability remains discoverable; the static reference shows Stop only.
- P3: real production task/session counts and available controls can differ from the deterministic fixture while retaining the reference structure.

final result: passed
