# RVretep

RVretep 1.1.1 is a lightweight Blender extension for recording OpenXR headset and controller motion directly against Blender's animation timeline.

## Features

- High-rate OpenXR headset and controller pose capture
- Blender's animation timeline as the authoritative recording clock
- Audio-synchronized Blender playback
- Raw motion preserved by default
- Separate `VRDATA_###` sessions so recordings never overwrite each other
- Optional Hide Old VR Sessions workflow
- Persistent Live VR Rig with realtime head and controller empties
- Optional desktop viewport preview for the Live VR Rig
- Session refresh, rename, show/hide, and delete tools
- Setup validation and VSE audio diagnostics
- Optional timeline markers
- Optional post-process smoothing
- Per-session metadata for capture rate, frame range, audio, and Blender version

## Recording

Start Blender's OpenXR session, open **View3D → Sidebar → VR Recording**, then start VR Recording.

During capture, Blender remains responsible for animation, rendering, and audio playback. RVretep only samples the latest OpenXR poses and records their actual Blender timeline positions. The hot capture path intentionally avoids dependency-graph updates and desktop 3D View redraws.

Stopping the recording bakes the captured motion into a new independent `VRDATA_###` session.

## Live VR Rig

The optional Live VR Rig is independent of recording and maintains three ordinary Blender empties inside `VR_LIVE_RIG`:

- `VRLive_Head`
- `VRLive_Hand_L`
- `VRLive_Hand_R`

Use them as parenting, constraint, driver, or animation targets. Live VR Rig can be enabled without recording.

The desktop preview is optional because desktop redraws add overhead while XR is running.

## Audio

RVretep detects Video Sequencer Sound strips and reports the detected channels, frame ranges, file paths, and packed state.

For timeline audio synchronization, the audio must exist as a VSE Sound strip. A Sound datablock by itself is not a timeline audio strip.

## Session organization

```
VR_RECORDINGS
├── VRDATA_001
├── VRDATA_002
└── VRDATA_003
```

**Hide Old VR Sessions** is disabled by default. Existing sessions remain visible until you choose otherwise.

## Requirements

- Blender 4.2 or newer for the Extensions workflow
- A Blender-supported OpenXR runtime
- A compatible OpenXR headset and controller setup

## License

RVretep is licensed under the GNU General Public License, version 3 or any later version.
