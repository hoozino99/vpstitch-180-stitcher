# Third-Party Design Sources

- `DESIGN.md` is vendored from `iFurySt/DESIGN.md/design-md/linear.app/DESIGN.md`.
- Upstream repository: https://github.com/iFurySt/DESIGN.md
- Upstream license: MIT.
- Review inputs also include the MIT-licensed Taste Skill and Vercel Web Interface Guidelines, plus 21st.dev UI review guidance.
- Playwright MCP guidance is used only for browser previews. VP Stitch itself is a native Qt application and is validated with native GUI tests.
# Third-party design sources

VP Stitch's UI contract is the unmodified Linear dark product specification in
[`DESIGN.md`](DESIGN.md), sourced from Awesome DESIGN.md. It is the governing
visual reference; it is not a design system inferred from the previous VP Stitch UI.

| Source | License | Applied to VP Stitch |
| --- | --- | --- |
| [Awesome DESIGN.md](https://github.com/iFurySt/DESIGN.md) | MIT | Exact `design-md/linear.app/DESIGN.md` checked into this repository as the visual contract. |
| [Taste Skill](https://github.com/Leonxlnx/taste-skill) | MIT | Anti-slop redesign audit only. Its own scope excludes dense dashboards, so it does not override the Linear contract. |
| [Vercel Web Interface Guidelines](https://github.com/vercel-labs/web-interface-guidelines) | MIT | Interaction review: visible focus, semantic status, keyboard access, stable layout, and unambiguous labels. |
| [21st.dev Codex plugin](https://github.com/21st-dev/codex-plugin) | Open source | UI review and design-sync checklist. React/Tailwind components are not copied into the PySide6/Qt application. |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | Browser testing reference only. VP Stitch is native Qt, so equivalent packaged-app QA uses Qt tests plus native macOS interaction and screenshots. |

The source licenses and project links above are preserved so future UI work can
be traced back to the open-source material that governed it.
