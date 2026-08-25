# Bundled ACES Studio Config

VP Stitch bundles the official Academy Software Foundation
OpenColorIO-Config-ACES Studio Config for offline, identical color setup on
macOS and Windows.

- Release: OpenColorIO-Config-ACES 4.0.0 for ACES 2.0
- OCIO baseline: 2.5
- Asset: `studio-config-v4.0.0_aces-v2.0_ocio-v2.5.ocio`
- Stable VP Stitch identifier: `vpstitch://aces-studio-v4.0.0`
- Source: https://github.com/AcademySoftwareFoundation/OpenColorIO-Config-ACES/releases/tag/v4.0.0
- SHA-256: `eda5b0008a43b72b98ad540e32eb0eb83b340dde54e35bddba64ccbafac1029a`
- License: `LICENSE.OpenColorIO-Config-ACES`

The stable identifier is stored in projects instead of an app-bundle absolute
path. VP Stitch resolves it to this bundled file at runtime, so projects remain
portable between supported operating systems.
