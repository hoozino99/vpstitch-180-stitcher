# VP Stitch Design QA

- Date: 2026-08-24
- Source visual truth: `/Users/hyunho/.codex/generated_images/01a0326f-e41b-7341-8eb5-68306a459fca/exec-02630755-b512-4892-a6bb-ce03bee71d63.png`
- Implementation screenshot: `/tmp/vpstitch-dense-implementation-final.png`
- Full-view comparison: `/tmp/vpstitch-design-qa-comparison-final.png`
- Focused Inspector comparison: `/tmp/vpstitch-design-qa-inspector.png`
- Focused timeline/actions comparison: `/tmp/vpstitch-design-qa-timeline-actions.png`
- Viewport: 1487 × 1058 CSS px
- Source pixels: 1487 × 1058
- Implementation pixels: 1487 × 1058
- Density normalization: device scale factor 1; no resampling
- State: dark desktop UI, default RIG Inspector, empty source state in the implementation. The source mock uses illustrative loaded media and panorama content, so dynamic media content was excluded from fidelity judgments.

## Findings

No actionable P0, P1, or P2 differences remain.

- Fonts and typography: compact system UI typography follows the selected target's hierarchy. Labels, values, tabs, and action copy remain readable at the 1180 × 720 minimum viewport without clipping.
- Spacing and layout rhythm: Media Pool, viewer, Inspector, shared timeline, and workflow controls preserve the target hierarchy. The final implementation intentionally compresses the timeline and workflow strip further in response to the user's request for higher information density.
- Colors and visual tokens: neutral graphite surfaces and muted blue-violet active states replace the earlier saturated violet. Contrast remains sufficient for primary labels, values, disabled controls, and the Render action.
- Image quality and assets: the central panorama is runtime-generated media, not a bundled design asset. The empty viewer correctly avoids fake imagery or placeholders. Native macOS window controls remain platform chrome rather than app-owned replicas.
- Copy and content: `Rig Profile` replaces user-facing config jargon. The Inspector states that Drive 5-Cam loads automatically and that Rig Align adjusts camera rotation, not lens calibration.
- Icons: the implementation intentionally uses compact text controls rather than enlarging or approximating the mock's decorative icons. No custom SVG, CSS-art, emoji, or placeholder icon substitutes were introduced.
- Behavior and accessibility: RIG, COLOR, and DELIVER tabs were exercised in the packaged app. Inspector and Jobs drawer signals are covered by GUI tests, keyboard-native Qt controls retain focus behavior, and Rig Align remains disabled until a preview exists.

## Comparison History

### Pass 1 — blocked

- P2: accent violet was substantially more saturated than the selected target.
- P2: buttons, rows, card radii, panel padding, and the bottom workflow strip were oversized and reduced viewer area.
- P2: narrow Inspector form values could clip at the minimum viewport.
- P2: the shared timeline track painted outside its widget because its vertical coordinate incorrectly used the horizontal midpoint.

Fixes made:

- Replaced the bright violet with a low-saturation blue-violet token and neutralized selection surfaces.
- Reduced typography, row heights, control heights, radii, margins, and internal gaps by roughly 15–25%.
- Rebuilt Inspector rows as compact two-column forms with a guaranteed 310 px panel width.
- Fixed `TrimRangeBar` to use the widget height for its vertical center and added a regression test.
- Replaced the large AUTO RIG card with a thin, compact Rig Profile section.

### Pass 2 — passed

Post-fix evidence shows no clipped labels or values at 1298 × 768 or 1487 × 1058. The viewer is the dominant region, the timeline is visible, active color is restrained, and the revised high-density hierarchy is consistent across the full view and both focused comparisons.

## Follow-up Polish

- P3: a future loaded-media QA pass can compare real panorama crop and timecode labels once representative P01–P05 source files are supplied.
- P3: platform-specific Windows font rendering should be inspected from the CI artifact on a physical Windows display.

final result: passed
