# Changelog

## 1.2.0-draft - 2026-10-04

- Moved teleprompter XR action creation into Blender's `xr_session_start_pre` lifecycle and reuse the active Blender action map instead of activating a new action set during a running session.

- Fixed native XR binding crash by isolating the teleprompter action map from inherited controller profiles. Draft bindings are currently limited to Oculus Touch.

Experimental VR teleprompter feature.

### Added

- Added a head-relative VR teleprompter rendered directly in the Blender scene.
- Added Blender Text datablock script source selection and a New Script helper.
- Added smooth automatic scrolling with configurable WPM and temporary thumbstick speed override.
- Added controller controls for pause/resume and next/previous section jumps.
- Added configurable panel distance, vertical offset, visible line count, wrapping width, and runtime update rate.
- Added native OpenXR action-map setup with Quest Touch and simple-controller bindings plus controller grip/aim pose actions.
- Kept the teleprompter runtime isolated from the recorder and Live VR Rig lifecycles.

### Draft notes

- This is an experimental 1.2.0 draft and requires hardware validation with the target OpenXR runtime.
- Controller binding behavior may vary by headset/runtime; use Teleprompter Diagnostics when testing.


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
