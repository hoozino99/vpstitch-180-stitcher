# VP Stitch UI/UX audit — 2026-08-28

Reference: the repository's exact upstream Linear `DESIGN.md`, the documented open-source design sources, and the existing VP Stitch workflow. The native Qt app was checked at 1298×768 before and after the change.

## Flow health

1. **Project Manager — Healthy.** Recent projects are readable, selection is clear, and New/Open actions are separated.
2. **New Project — Healthy.** Name, location, canvas, OCIO input/working/output transforms, and delivery method are available in one dialog. The in-app New Project command now uses this same complete flow.
3. **Media Pool — Healthy.** Import is now located in the Media Pool header. Numbered and manually assigned media behavior is stated once, in context.
4. **Plate Sets — Healthy.** Plate Sets remain timeline-level records, active state is visible, and the resizable vertical library splitter remains enabled.
5. **Active Timeline — Healthy.** Global import no longer appears inside the timeline. `ASSIGN SELECTED` and `REMOVE ALL` now describe timeline-scoped actions.
6. **Viewer and transport — Needs real-media follow-up.** The viewer hierarchy and cached-playback state are clearer, and a cached proxy no longer replaces the stitched still with an unrendered black video widget. A fresh real-media playback pass is still the correct final proof.
7. **Inspector — Healthy.** The redundant `STITCH CONTROLS` title and long instructional copy were removed. Rig, Plate, Color, and Deliver remain directly accessible.
8. **Render Queue — Healthy.** Queue count, FPS, format, file, status, selected render, and render-all actions are visible without duplicate headings.
9. **Task Log — Healthy.** Repeated `frame N` and `tiles N/N` chatter is coalesced into progress state. Warnings and meaningful process output remain, with an explicit Clear Log action.
10. **Build and regression — Healthy.** 235 tests passed; the signed arm64 macOS app, ZIP, and DMG were rebuilt successfully.

## Design changes

- Reduced equal-weight card boxing; library sections now use quiet dividers and persistent splitter handles.
- Moved the primary accent from immediate render to `ADD TO QUEUE`, matching the deliberate delivery workflow.
- Reduced workflow controls from 128×34 to 106×30 minimum size and simplified labels to `TC ALIGN`, `PREVIEW`, `STITCH`, and `ADD TO QUEUE`.
- Simplified the bottom status area: progress appears only while work is active.
- Added explicit disabled styling so unavailable primary actions do not look active.
- Preserved the exact Linear-derived near-black, Inter, subtle-border, and single-indigo design contract already checked into the repository.

## Evidence

- `01-project-manager.png` — workspace before
- `02-render-queue.png` — render queue before
- `03-task-log.jpg` — task log before
- `04-project-manager-after.jpg` — project manager after
- `05-workspace-after.jpg` — workspace after
- `06-render-queue-after.jpg` — render queue after
- `07-task-log-after.jpg` — task log after
- `08-workspace-before-after.jpg` — same-size workspace comparison
- `09-queue-before-after.jpg` — same-size queue comparison
- `10-workspace-final.jpg` — final rebuilt package
