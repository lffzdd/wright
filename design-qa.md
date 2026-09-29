# Wright Web reference design QA

## Target and evidence

- Source visual truth: `/Users/williamlao/Documents/Temp4Agent/agent_ui_designs/index.html`
- Dark source: `web/qa/reference-current-1440x960.png`
- Dark implementation: `web/qa/implementation-after-1440x960.png`
- Light source: `web/qa/reference-light-1440x960.png`
- Light implementation: `web/qa/implementation-after-light-1440x960.png`
- Narrow implementation: `web/qa/implementation-after-900x800.png` and `web/qa/implementation-after-390x800.png`
- Focused dark comparisons: `web/qa/reference-focus-center.png` versus `web/qa/implementation-focus-center.png`, and `web/qa/reference-focus-inspector.png` versus `web/qa/implementation-focus-inspector.png`.
- Earlier comparison artifacts remain at `web/qa/reference-1440x960.png`, `web/qa/implementation-1440x960.png`, and `web/qa/comparison-*.png`. The source's supplied `preview.png` was 2880 × 1920 and the earlier report normalized it to 1440 × 960; this pass captured the live HTML directly at 1×.
- Capture: Chromium, 1440 × 960 CSS pixels and image pixels, device scale factor 1. Narrow captures are 900 × 800 and 390 × 800 at the same density.
- State: English, task with a failed Shell call, file edit, pending permission, Task inspector tab, dark and light themes. Wright uses its deterministic visual fixture; the reference uses its built-in sample data.

## Comparison history

1. Initial comparison: Wright's reasoning, tool, and permission cards began at x=276 while the source began at x=246. The permission card began near y=475 instead of y=494. The top branch group began near x=372 instead of x=425. The composer and Inspector plan were vertically compressed.
   An earlier project QA pass had already replaced generic three-column chrome with the current reference-specific components; this pass evaluates and repairs that result.
2. First repair: Removed the extra timeline indent and matched the source's 38px top bar, 230px rail, 380px inspector, and top navigation spacing. Adjusted message, tool, diff, and permission-card line heights and padding.
3. Second repair: Grouped the Inspector plan heading with its steps, matched step density, aligned the composer, refined sidebar markers and weights, and made the light theme's dark tool surfaces readable.
4. Final dark capture: branch x=425 versus source x≈425; permission card x=246, y≈493, width=798, height=207 versus source x=246, y≈494, width=798, height=207; first plan step y≈225 and composer x=240, y≈837 match the source within one CSS pixel. The final images above provide the full-view comparison and focused evidence for the timeline, permission card, composer, and inspector.

## Fidelity assessment

- Typography: The source font stacks and compact type scale are present. Some wording and text length come from Wright's real state, so wrapping can differ with live data.
- Spacing and layout: The default 1440 × 960 dark layout matches the principal region and card boundaries. At 900 and 390 pixels, the page has no horizontal overflow; side panels become overlays.
- Colors: Dark palette tokens match the source. The light variant retains dark tool, permission, and composer surfaces while giving their text sufficient contrast; the reference's light screenshot has several low-contrast labels.
- Images and icons: The source has no raster imagery to migrate. Wright keeps its existing Phosphor icon library, so a few glyph silhouettes differ from the reference's inline icons.
- Copy and content: Wright branding, real model names, file counts, permission choices, and session data remain dynamic. The reference's decorative audit checkbox and sample `/benchmark` chip are not fabricated in the product. The fixture therefore shows `More grants` and the actual attached file chip.

## Verification and remaining polish

- `npm test -- --run`: 46 tests passed.
- `npm run build`: production bundle generated successfully.
- Browser check: Shell disclosure closed and reopened, Files tab selected, Inspector closed and reopened, theme toggled; no page errors.
- P3: Shell failure output uses the real unmodified text instead of the reference's custom line-by-line coloring; some icons and dynamic copy differ.
- Limit: Screenshots use a deterministic fixture, not a live backend session. Existing component tests cover the affected components, but this pass does not establish visual equality for every possible live state.

final result: passed
