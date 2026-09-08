# VP Stitch desktop design

VP Stitch is a camera stitching workstation. Its interface prioritizes media organization, accurate image inspection, camera adjustment, and explicit render actions.

## Workspace

- Keep media and timelines on the left, the panorama in the center, and contextual settings on the right.
- Keep transport and trim controls directly below the viewer. Workflow actions follow in the order Sync timecode, Quick preview, Auto stitch, Add to queue.
- Render now exports the active timeline immediately. Add to queue captures a settings snapshot for later rendering; these are different actions.
- The queue uses Render pending for queued or failed items, Render selected for one job, and Render again for a completed selection.
- Open output and Remove are visible queue actions. Removing a job retains its output files.
- Search media by clip or folder name. Keep matching ancestors visible, expand matching branches, clear hidden selections, and restore expansion when search is cleared.

## Visual system

The shared Qt stylesheet lives in `vpstitch/theme.py` and is used by both the project manager and workspace.

| Role | Color |
| --- | --- |
| Workspace | #202225 |
| Panel | #282b2f |
| Input / viewer surround | #1c1e21 / near black |
| Primary text | #e5e7eb |
| Supporting text | #b2bac5 |
| Border | #494e56 |
| Selected item | #365a78 |
| Focus | #79b6df |
| Primary action | #b6d7ed with #14232f text |

Use native system sans-serif fonts at 12px for controls. Monospace is reserved for time and technical values. Use sentence case for action labels. Retain technical abbreviations such as FPS and timecode. Use 3px control corners and flat panel boundaries; avoid decorative cards, gradients, uppercase slogans, and repeated accent treatments.

Disabled controls have explicit styling even when they carry a primary or secondary object name. Selection and keyboard focus remain visible.

## Interaction

- The empty viewer describes the next available step for the current timeline and camera assignment.
- Workflow buttons refresh after a timeline finishes loading, without requiring another media selection.
- Playback and clearing plates are disabled when no plates are assigned.
- Render controls reflect selection, running tasks, and actual pending work.
- The project manager distinguishes browsing for an existing project from opening a selected recent project.
- Existing keyboard transport and editable-field protection remain intact.

## Performance and quality

The viewer reuses its graphics item and preserves the current transform between same-sized frames. Preview seam weights remain cached until output, geometry, or feather changes. Final CPU blending divides in place without advanced-indexing RGB copies. These optimizations preserve tested output pixels.

The preview remains a draft. Final output resolution, OCIO processing, dithering, queue snapshots, and codec bit depth remain controlled by the render configuration.

## Verification

See [workspace and seam validation](docs/workspace-seam-validation.md) for measurements, tested workflows, and platform limits. Native screenshots and footage remain local QA evidence. Historical Linear-inspired references are not the current design specification.
