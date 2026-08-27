# VP Stitch UI Reference Audit

Reference: Instagram post `DcfwLiAEb51`

## Verdict

Use the open-source tools named by the reference directly where they fit the native Qt stack. The exact upstream Linear specification from Awesome DESIGN.md is the governing visual contract. Do not derive a new contract from the previous VP Stitch UI.

## Reference flow

1. Cover — healthy: strong headline and restrained black/gold palette.
2. Overview — healthy: five tools summarized with clear hierarchy.
3. 21st.dev — partial fit: useful component inspiration, but its React components do not directly apply to Qt.
4. Taste Skill — apply its anti-slop audit, while respecting its stated exclusion for dense dashboards.
5. Vercel guidelines — apply accessibility, focus, state, labeling, and consistency checks; omit browser-only rules.
6. Awesome DESIGN.md — use the upstream Linear DESIGN.md verbatim as the durable contract.
7. Playwright — apply the visual regression workflow, but use Qt tests and native macOS interaction because Playwright cannot drive a native Qt window.
8. CTA — not applicable to the product UI.

## Apply to VP Stitch

- Linear's exact dark surfaces: `#08090a`, `#0f1011`, `#191a1b`, `#28282c`.
- Linear's exact single accent: `#5e6ad2`, `#7170ff`, and `#828fff` hover.
- Linear's borders, compact typography hierarchy, and 5–8 px control radii.
- Strong typographic hierarchy without oversized editorial headings.
- Left: Media Pool and timelines. Center: viewer, transport, timeline. Right: contextual Inspector tabs.
- No gradients, glow, decorative line art, or low-contrast gray copy in the working UI.
- Keep the upstream contract in `DESIGN.md` and run packaged-app screenshot QA at fixed window sizes after UI changes.

## FPS and Render Queue plan

- Each timeline/plate set owns an exact rational frame rate, detected from its plates.
- Default project policy: Match Plate. Manual rates remain available for intentional conversion.
- All plates within one set must match; mixed rates are blocked with a per-file report.
- Render Queue snapshots timeline ID, plate paths, exact FPS, frame range, canvas, color transforms, codec, output path, and settings digest.
- Queue rendering reads only the immutable snapshot, not the currently active timeline UI.
- Queue UI shows FPS and source timeline so 23.976 and 24.000 jobs cannot be confused.
- Tests cover independent 23.976/24 timelines, queue immutability, mixed-FPS rejection, and render-command FPS.

## Evidence and limits

Evidence screenshots are stored in `/tmp/vpstitch-design-audit-20260827`. This audit covers the visible Instagram carousel only. Keyboard navigation, screen-reader behavior, zoom, and interactive states were not available from the reference.
