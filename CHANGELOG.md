# Changelog

All notable changes to **Drone Manager** are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-24

First public release.

### Added
- **Flight logs:** DJI log import (`dji-log`), plus Litchi CSV, Airdata CSV, PX4 ULog (`.ulg`) and ArduPilot (`.bin`).
- **Flight reconstruction** from photo EXIF and video SRT tracks.
- **2D/3D replay** (Leaflet + CesiumJS) synced with video, with continuous multi-clip timeline and optional MP4 merge.
- **Dynamic route coloring** by RC signal, altitude, speed, satellites, power or battery.
- **Airspace and compliance:** Brazil (ANAC/DECEA) and USA (FAA Part 107, LAANC/UASFM).
- **Wind at altitude** analysis and energy efficiency (Wh/km) comparison.
- **Flight Health Score** (0-100) across mechanical, battery/voltage sag and RC/GPS link.
- **Battery health** dashboard with capacity and cell deviation evolution.
- **Media library:** tags, duplicates, SD card import wizard, before/after comparison, ZIP export for OpenDroneMap.
- **Offline AI:** YOLO11s (VisDrone) object detection and CLIP ViT-B/32 semantic search.
- **Orthomosaics:** NodeODM integration with local Docker control and streaming GeoTIFF import.
- **Mission planning:** grid, double grid and orbit generators, Terrain Follow (DEM), native DJI KMZ (WPML) export.
- **Flight window forecast** (Open-Meteo + Kp index).
- **GIS engine:** SQLite R-Tree and SpatiaLite, address search, geodesic measurements, portable HTML package.
- **Base maps:** Hybrid, Google, Esri Topo; Satellite as default.
- **Multilingual UI:** pt-BR, en, es, with imperial units in English.
- **Windows 11 + WSL2 launcher** and portable paths for Linux/macOS.

### Security
- Strict CSRF protection, CSP, SRI on CDNs, XSS sanitization and pinned dependencies.
- SSRF hardening for NodeODM, GeoTIFF validation, WSL2/Windows path blocking.
- `.gitignore` hardened against secrets, telemetry exports and binary media/models.

### Changed
- Asynchronous background video merge with status polling.
- Accessibility, contrast and responsiveness improvements.

### Fixed
- Geodesic/photogrammetric precision, event translations and temporary file cleanup.
- Compliance (FAA/DECEA), player, wind and i18n issues.

### Legal
- Licensed under **AGPL-3.0**, with aviation and trademark disclaimers.
