# RVretep

RVretep 1.2.0-draft is a lightweight Blender extension for recording OpenXR headset and controller motion directly against Blender's animation timeline.

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
- Experimental head-relative VR teleprompter with controller controls
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


## VR Teleprompter (1.2.0 draft)

The experimental VR Teleprompter displays a Blender Text datablock as a head-relative panel inside the OpenXR scene.

Workflow:

1. Click **New Script** in the RV Recording sidebar, or create/use any Blender Text datablock.
2. Put your presenter script into the selected Text datablock.
3. Start the OpenXR session and choose the Text datablock in **VR Teleprompter (Experimental)**.
4. Set a base reading speed (150 WPM is a good starting point) and start the teleprompter.

Default VR controls:

- Right thumbstick: temporary speed override. Push up/down to read faster/slower without permanently changing the base WPM.
- Right trigger: pause/resume automatic scrolling.
- Right A / primary button: next detected section.
- Right B / secondary button: previous detected section.

The teleprompter detects simple all-caps section headings such as `INTRO`, `AUDIO`, and `ENDING` and uses them for section jumps.

The implementation uses Blender's native OpenXR action API and keeps its runtime state separate from the recorder and Live VR Rig. The action map is created from Blender's current XR action map when possible and includes controller grip/aim pose actions so RVretep's existing controller pose capture can continue when the teleprompter action set is active.

**Draft status:** native XR action bindings are implemented, but controller behavior should be hardware-tested on Blender 5.2.2 LTS with the target headset/runtime before treating this feature as production-ready.
