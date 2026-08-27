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

## OCIO Output / Manual Slot Assignment Follow-up

- Date: 2026-08-26
- User reference: `/var/folders/zt/p0f5n5t1775dvq_fzxrh7r6m0000gn/T/TemporaryItems/NSIRD_screencaptureui_XtMIm5/스크린샷 2026-08-26 오전 7.19.24.png`
- Implementation screenshot: `audit/latest-color-ui.png`
- Manual assignment dialog: `audit/camera-assignment-dialog.png`
- Viewport: 1512 × 982 px
- State: real five-camera project, active COLOR Inspector, aligned 12-frame playback range.

### Findings

- Output Transform is now in the main PIPELINE group immediately after Input Transform and Working Space; no lower hidden delivery section is required.
- A thin divider separates source/working transforms from output controls. Media Pool and Plate Sets retain distinct bordered cards, tree dividers, and visible splitter handles.
- The Inspector has a 360 px minimum width, so `Output transform` and all controls remain visible without clipping at the audited viewport. The center viewer remains the dominant region.
- OCIO values are list-only dropdowns. The invalid saved value `sRGB - Displayㄴ` visibly resolves to `sRGB - Display` before preview or render configuration is collected.
- Arbitrary filenames use a compact three- or five-row slot dialog labeled with physical P01–P05 or P06–P08 positions. Duplicate clip assignment disables confirmation.
- The actual five-source playback path generated a 960×264 H.264 proxy for 12 frames after OCIO recovery. The GUI and project snapshot both retained the corrected output transform.

No P0, P1, or P2 visual issue remains in the inspected states. Native Computer Use capture remains unavailable because the macOS accessibility crash-suppression shim intentionally blocks external AX enumeration; the packaged app was visually verified from an offscreen Qt render at the same production layout size.

final result: passed

## Stability / Timeline Follow-up

- Date: 2026-08-24
- Before screenshot: `/tmp/vpstitch-stability-audit/01-current-workspace.png`
- Updated screenshot: `/tmp/vpstitch-stability-audit/02-updated-workspace-1298.png`
- Same-viewport comparison: `/tmp/vpstitch-stability-audit/03-comparison.png`
- Viewport: 1298 × 768 px for both sides

### Workflow audit

1. Import plates — unchanged and healthy. P01–P03/P01–P05 recognition and one-based ordering remain visible in the Media Pool.
2. TC alignment and range trim — clearer. Rectangular IN/OUT caps now read as range boundaries instead of ambiguous dots.
3. Preview inspection — clearer and safer. A separate bright playhead, frame field, and time display communicate scrubbing; releasing the playhead refreshes an existing stitched preview. If a refresh is already running, the latest requested frame is queued instead of lost.
4. High-resolution preview — bounded. Preview output fits within 3840 × 2160 while retaining the canvas aspect ratio with no crop. Every camera decode is independently covered by the same UHD bound even when the output canvas is already 4K; scaling occurs before Python allocates each frame. The master render remains at configured resolution.
5. Rig alignment and render — unchanged. The future `RIG ALIGN` → `STITCH` naming idea is recorded in `PRODUCT_NOTES.md` and intentionally not applied yet.

### Consolidation

- Removed the duplicate READY indicator and top-level PROFILE action. Profile open/save remains in the Inspector, where its settings live.
- Removed the separate viewer reference-time input. Playhead navigation now lives only in Shared Timeline.
- TIFF generation was removed. Preview/reference stills use 16-bit PNG; the GUI exposes only 10-bit video masters or 12-bit DPX.

### Stability evidence and limits

- Automated suite: 73 tests passed, including automatic source-depth display, GUI master restriction to 10/12-bit, pre-allocation decoder scaling, fitted preview configuration, high-resolution inputs behind a 4K canvas, queued playhead refresh, extreme-aspect fit, timeline state, Unicode-safe PNG reference extraction, and rejection of the removed TIFF sequence codec.
- Real-source verification: the five supplied 5952×3968 H.264 `yuv420p` 8-bit P01–P05 clips were auto-detected, displayed as `SOURCE 8-bit → MASTER 10/12-bit`, and produced a 3840×992 no-crop stitched preview successfully.
- macOS arm64 app was rebuilt, ad-hoc code signature verified, launched, and remained running after startup.
- Three earlier native crash reports ended inside macOS accessibility hierarchy inspection while external UI audit tooling queried the Qt window. They do not show an out-of-memory termination. High-resolution workload pressure was addressed separately by pre-decode preview scaling, bounded 4K output, temporary-reference cleanup, and bounded task logging.
- VoiceOver/accessibility-tree behavior still requires a dedicated physical interaction pass; the native inspection crash means this follow-up does not claim full accessibility certification.

### Visual result

The viewer remains dominant at the minimum practical viewport. The new timeline has a clear selected range, distinct trim caps, a visible playhead, and consolidated controls without text clipping. No new P0–P2 visual issue was found in the same-viewport comparison.

follow-up result: passed with accessibility verification noted above

## Project / Timeline Workspace Follow-up

- Date: 2026-08-25
- Source visual truth: `/Users/hyunho/.codex/generated_images/01a0326f-e41b-7341-8eb5-68306a459fca/exec-62644d61-70da-4253-a161-d0c3eb534e8a.png`
- Implementation screenshot: `/tmp/vpstitch-design-qa-current.png`
- Same-viewport comparison: `/tmp/vpstitch-design-qa-comparison.png`
- Viewport: 1487 × 1058 px for both sides
- State: loaded five-camera Plate Set, aligned timecode, real stitched panorama preview, default Rig Inspector.

### Findings

- Left hierarchy: the unified Media Pool now owns project folders, Plate Set timelines, and their source clips. A Plate Set is persisted as a timeline and opens on double-click.
- Center workspace: the viewer remains dominant, displays the entire 20000×5504 canvas without cropping, and keeps one shared trim/playhead bar below it.
- Right workflow: `INSPECTOR`, `RENDER QUEUE`, and `TASK LOG` are peer tabs. The Inspector is intentionally denser than the visual target because the user requested the current app's editable stitch values to remain available.
- Global navigation: project, timeline, playback, tools, window, and help commands are exposed through the native application menu instead of duplicate toolbar buttons.
- Visual tokens: graphite surfaces, thin dividers, compact system typography, and restrained blue-violet selection/action states remain aligned with the selected target.
- Interaction: no visible clipped button labels or overlapping controls at 1487×1058. The same layout also retains the existing 1180×720 minimum-size regression coverage.
- Playback verification: a fresh P06–P08 3-camera test take completed input probing, embedded-TC alignment, representative-frame stitching, 960×360 H.264 proxy warming, playback, reverse/stop/forward controls, and frame stepping. The resulting project timeline persisted both stitch and cache status as `ready`.

No actionable P0, P1, or P2 difference remains. The larger vertical letterbox in the implementation is required by the real 20000×5504 source canvas; filling the viewer like the 2:1 concept image would crop the plates and violate the no-crop requirement.

final result: passed
