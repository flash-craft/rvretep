# Changelog

## 1.1.1 - 2026-10-04

Reliability and setup polish.

### Fixed

- Fixed the **Hide Old VR Sessions** control so recorded objects are hidden consistently in the viewport.
- Explicitly reset the Live VR Rig to disabled when RVretep is registered or a .blend is loaded.
- Fixed recorder runtime state cleanup paths to use boolean `False` instead of `None`.

### Improved

- Added a prominent OpenXR / VR Link warning before starting a session when OpenXR is not already running.
- Added a confirmation dialog explaining that starting native XR without a working runtime or connected headset can make Blender wait during XR startup.

## 1.1.0 - 2026-10-03

Polish and reliability update.

### Changed

- Added a clear VR Link readiness warning before starting an OpenXR session.
- Hardened recorder state cleanup on .blend file load.
- Updated release metadata and documentation for Blender 4.2+.

## 1.0.0 - 2026-10-03

First public RVretep release.

### Included

- OpenXR headset pose recording
- Left and right controller grip capture
- Audio-synchronized Blender playback
- Low-overhead capture loop
- Independent VR recording sessions
- Optional hidden-old-session behavior
- Persistent realtime Live VR Rig
- Live Rig modal/timer lifecycle hardening
- Session management and diagnostics
- VSE audio diagnostics
- Optional timeline markers
- Optional post-process smoothing
- Session metadata
- Blender extension manifest
- GPL-3.0-or-later licensing
