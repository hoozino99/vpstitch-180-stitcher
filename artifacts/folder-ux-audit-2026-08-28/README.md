# Media Pool folder UX audit · 2026-08-28

Scope: packaged macOS app at 1296/1297×768, hierarchical folders, media destinations, drag feedback, persistence, and regression safety.

## Audited steps

| Step | Flow | Health | Evidence |
|---:|---|---|---|
| 1 | Open the existing project and inspect the previous Media Pool | Defect reproduced | Folder rows had no type icons or persistent disclosure/grid cues; moving items was not wired to project data. See `01-current-media-pool.jpg`. |
| 2 | Create nested folders and import to a selected nested folder or explicit project root | Pass | Selection now resolves the exact destination. Nested creation and both import paths persist after project reload. |
| 3 | Move multiple clips and folders, preserve ordering/selection, reject descendant cycles | Pass | Model and GUI integration tests cover atomic multi-media moves, folder nesting, reload persistence, and invalid-cycle rejection. |
| 4 | Inspect the rebuilt packaged app at the same viewport | Pass | Native file/folder icons, disclosure arrows, indentation, row separators, compact hover/selection states, and updated guidance are visible. See `02-after-media-pool.jpg`. |
| 5 | Exercise drag through macOS Computer Use | Automation limitation | The Qt drag enters its native modal drag loop and the Computer Use pipe disconnects before mouse-up; the app process remains alive. The same destination mapping and persisted move path are covered by Qt tests, and the right-click `Move to Folder` fallback remains available. |
| 6 | Run project/GUI/full regression suites and verify the signed bundle | Pass | `240 passed`; `codesign --verify --deep --strict` passed; bundled `vpstitch-cli` is executable. |

## Deterministic defects fixed

- Media Pool used a scroll-only tree with no drag/drop model connection.
- Active destination changed only on item activation, so a simple selection could import or create in a stale folder.
- Explicit project-root selection could still fall back to the first root folder.
- Tree refresh discarded parts of expanded and multi-selection state.
- Folder/media types and hierarchy were visually weak, with no reliable disclosure cue or row separation.

## Visual comparison

- `03-before-after.jpg` — complete side-by-side comparison at the captured viewport.
- `01-current-media-pool.jpg` — baseline.
- `02-after-media-pool.jpg` — rebuilt packaged app.

This is a deterministic flow audit, not a claim of full accessibility conformance.
