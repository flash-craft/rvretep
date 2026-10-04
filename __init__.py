# SPDX-FileCopyrightText: 2026 Flash-Craft
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

"""RVretep.

Designed for Blender 4.2+ and Blender's built-in OpenXR session support.
The recorder deliberately avoids dependency-graph updates and desktop
viewport redraws during capture. Blender remains responsible for animation
and audio playback while the add-on samples the latest XR poses.
"""


import math
import time
from datetime import datetime, timezone
from typing import Iterable

import bpy
import mathutils
from bpy.app.handlers import persistent
from bpy_extras import anim_utils


# retep was here. probably.
ADDON_VERSION = (1, 1, 1)
MIN_BLENDER_VERSION = (4, 2, 0)
# dev note: retep was here; ship the boring parts too.
# If you found this comment, congratulations: the debugger side quest worked.
MASTER_COLLECTION_NAME = "VR_RECORDINGS"
COLLECTION_PREFIX = "VRDATA_"
OBJECT_HEAD = "VR_Head"
OBJECT_HAND_L = "VR_Hand_L"
OBJECT_HAND_R = "VR_Hand_R"

# Live rig is deliberately separate from baked recording sessions.
LIVE_COLLECTION_NAME = "VR_LIVE_RIG"
LIVE_OBJECT_HEAD = "VRLive_Head"
LIVE_OBJECT_HAND_L = "VRLive_Hand_L"
LIVE_OBJECT_HAND_R = "VRLive_Hand_R"

_RECORDER_ACTIVE = False
_RECORDER_TIMER = None
_LIVE_RIG_RUNTIME = None
_LIVE_RIG_GENERATION = 0
_SESSION_ENUM_CACHE = []


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

# DEV EASTER EGG: retep was here // leave the code better than you found it.


def is_xr_running(context) -> bool:
    """Return whether Blender currently has a running OpenXR session."""
    try:
        state = context.window_manager.xr_session_state
        return bool(state and state.is_running(context))
    except Exception:
        return False


def get_xr_state(context):
    return context.window_manager.xr_session_state


def get_recording_number(name: str) -> int | None:
    if not name.startswith(COLLECTION_PREFIX):
        return None
    suffix = name[len(COLLECTION_PREFIX):]
    if not suffix.isdigit():
        return None
    return int(suffix)


def get_recording_collections() -> list[bpy.types.Collection]:
    """Return this add-on's session collections, numerically sorted."""
    collections = []
    for col in bpy.data.collections:
        number = get_recording_number(col.name)
        if number is not None:
            collections.append((number, col))
    collections.sort(key=lambda item: item[0])
    return [col for _, col in collections]


def get_next_recording_number() -> int:
    """Return the next unused numeric session identifier."""
    used = {
        number
        for col in get_recording_collections()
        if (number := get_recording_number(col.name)) is not None
    }
    number = 1
    while number in used:
        number += 1
    return number


def get_recording_collection(identifier: str | int | None):
    if identifier is None:
        return None
    if isinstance(identifier, int):
        identifier = f"{COLLECTION_PREFIX}{identifier:03d}"
    return bpy.data.collections.get(str(identifier))


def display_name_for_collection(col: bpy.types.Collection) -> str:
    label = str(col.get("display_name", "")).strip()
    return label or col.name


def session_enum_items(self, context):
    """Build a dynamic session enum with a permanently valid NONE item.

    Blender's dynamic EnumProperty can retain a previous identifier after the
    underlying collection set changes. Keeping NONE in the enum at all times
    makes refresh/delete/cleanup operations safe even when the last session is
    removed or a session was deleted outside this add-on.
    """
    global _SESSION_ENUM_CACHE

    items = [
        (
            "NONE",
            "No active session",
            "No VR session is selected",
            'INFO',
            0,
        )
    ]

    for col in get_recording_collections():
        number = get_recording_number(col.name) or 0
        label = display_name_for_collection(col)
        description = f"{col.name} — {label}"
        items.append((col.name, label, description, 'REC', number))

    # Blender documents a known lifetime issue for strings returned by dynamic
    # enum callbacks. Keep the complete tuples alive globally.
    _SESSION_ENUM_CACHE = items
    return _SESSION_ENUM_CACHE


def ensure_master_collection(scene: bpy.types.Scene) -> bpy.types.Collection:
    master = bpy.data.collections.get(MASTER_COLLECTION_NAME)
    if master is None:
        master = bpy.data.collections.new(MASTER_COLLECTION_NAME)
        scene.collection.children.link(master)
    elif master.name not in {c.name for c in scene.collection.children}:
        # Collection may exist but not be linked to the current scene.
        scene.collection.children.link(master)
    return master


def get_sound_strips(scene: bpy.types.Scene) -> list:
    """Return all VSE Sound strips, supporting Blender 4.x and 5.x APIs."""
    try:
        seq = scene.sequence_editor
        if seq is None:
            return []

        # Blender 5.x uses strips_all. Older supported releases expose
        # sequences_all. Prefer the modern API and retain the fallback.
        strips = getattr(seq, "strips_all", None)
        if strips is None:
            strips = getattr(seq, "sequences_all", None)
        if strips is None:
            strips = getattr(seq, "strips", None)
        if strips is None:
            strips = getattr(seq, "sequences", None)

        return [strip for strip in strips if getattr(strip, "type", None) == 'SOUND']
    except Exception:
        return []


def count_audio_strips(scene: bpy.types.Scene) -> int:
    return len(get_sound_strips(scene))


def audio_diagnostic_lines(scene: bpy.types.Scene) -> list[str]:
    """Return human-readable VSE audio diagnostics."""
    strips = get_sound_strips(scene)

    if not strips:
        # A Sound datablock can exist without being used by a VSE Sound strip.
        datablocks = list(getattr(bpy.data, "sounds", []))
        if datablocks:
            return [
                "Video Sequencer: 0 Sound strips detected.",
                f"Sound datablocks in blend: {len(datablocks)}.",
                "A sound datablock alone is not timeline audio; use a VSE Sound strip for Audio Sync.",
            ]

        return [
            "Video Sequencer: 0 Sound strips detected.",
            "No Sound strips or standalone Sound datablocks were found.",
        ]

    lines = [f"Video Sequencer: {len(strips)} Sound strip(s) detected."]

    for index, strip in enumerate(strips, start=1):
        name = str(getattr(strip, "name", f"Sound {index}"))
        channel = getattr(strip, "channel", None)
        frame_start = getattr(strip, "frame_final_start", getattr(strip, "frame_start", None))
        frame_end = getattr(strip, "frame_final_end", getattr(strip, "frame_end", None))

        line = f"{index}. {name}"
        if channel is not None:
            line += f" — Ch {channel}"
        if frame_start is not None and frame_end is not None:
            line += f" — frames {frame_start:g}–{frame_end:g}"

        sound = getattr(strip, "sound", None)
        filepath = getattr(sound, "filepath", "") if sound else ""
        if filepath:
            line += f" — {filepath}"

        if sound is not None and getattr(sound, "packed_file", None) is not None:
            line += " — PACKED"

        lines.append(line)

    return lines


def current_timeline_position(scene: bpy.types.Scene) -> float:
    """Read Blender's current timeline position as a float frame."""
    # Blender 4.1 exposes frame_float; fall back to frame + subframe.
    try:
        return float(scene.frame_float)
    except Exception:
        return float(scene.frame_current + scene.frame_subframe)


def format_frame(frame: float) -> str:
    return f"{frame:.3f}".rstrip('0').rstrip('.')


def collect_action_fcurves(obj: bpy.types.Object):
    """Get the action's F-Curves in Blender 4.x and compatible fallback paths."""
    if not obj.animation_data or not obj.animation_data.action:
        return None

    action = obj.animation_data.action

    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return fcurves

    # Future/modern slotted-action fallback. This path is deliberately
    # defensive because the exact internal action API has changed across
    # Blender releases.
    try:
        slot = getattr(obj.animation_data, "action_slot", None)
        if slot is not None and hasattr(anim_utils, "action_get_channelbag_for_slot"):
            channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
            if channelbag:
                return channelbag.fcurves
    except Exception:
        pass

    return None


def set_action_name(obj: bpy.types.Object, action_name: str) -> None:
    if obj.animation_data and obj.animation_data.action:
        try:
            obj.animation_data.action.name = action_name
        except Exception:
            pass


def safe_object_delete(obj: bpy.types.Object) -> None:
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        pass


def set_recording_visibility(col: bpy.types.Collection, visible: bool) -> None:
    """Apply session visibility consistently to the collection and its objects."""
    col.hide_viewport = not visible

    for obj in col.objects:
        try:
            obj.hide_viewport = not visible
        except Exception:
            pass
        try:
            obj.hide_set(not visible)
        except Exception:
            pass


def delete_recording_collection(col: bpy.types.Collection) -> None:
    """Delete a whole VR session without touching unrelated scene data."""
    objects = list(col.objects)
    action_names = set()

    for obj in objects:
        if obj.animation_data and obj.animation_data.action:
            action_names.add(obj.animation_data.action.name)

    for obj in objects:
        safe_object_delete(obj)

    try:
        bpy.data.collections.remove(col, do_unlink=True)
    except Exception:
        return

    # Remove only actions that became orphaned and were owned by the session.
    for action_name in action_names:
        action = bpy.data.actions.get(action_name)
        if action and action.users == 0:
            try:
                bpy.data.actions.remove(action)
            except Exception:
                pass


def popup_message(context, title: str, lines: Iterable[str], icon='INFO'):
    clean_lines = [str(line) for line in lines if str(line).strip()]

    def draw(self, _context):
        layout = self.layout
        for line in clean_lines:
            layout.label(text=line, icon='BLANK1')

    context.window_manager.popup_menu(draw, title=title, icon=icon)


def mark_session_collection(col: bpy.types.Collection, **metadata) -> None:
    for key, value in metadata.items():
        try:
            col[key] = value
        except Exception:
            pass


def audio_status(scene: bpy.types.Scene) -> str:
    strips = get_sound_strips(scene)
    if not strips:
        datablock_count = len(list(getattr(bpy.data, "sounds", [])))
        if datablock_count:
            return f"No VSE Sound strips ({datablock_count} Sound datablock(s) exist)"
        return "No VSE Sound strips found"

    channels = sorted({int(getattr(strip, "channel", 0)) for strip in strips})
    channel_text = ", ".join(str(c) for c in channels if c > 0)
    suffix = f" — Ch {channel_text}" if channel_text else ""
    count = len(strips)
    return f"{count} Sound strip{'s' if count != 1 else ''} found{suffix}"



# -----------------------------------------------------------------------------
# Live VR rig
# -----------------------------------------------------------------------------


def ensure_live_rig(context):
    """Return the persistent live rig collection and its three tracking empties."""
    scene = context.scene
    collection = bpy.data.collections.get(LIVE_COLLECTION_NAME)

    if collection is None:
        collection = bpy.data.collections.new(LIVE_COLLECTION_NAME)
        scene.collection.children.link(collection)
    else:
        scene_children = {child.name for child in scene.collection.children}
        if collection.name not in scene_children:
            try:
                scene.collection.children.link(collection)
            except Exception:
                pass

    collection.hide_render = True
    collection.hide_viewport = False
    collection["rvretep_live_rig"] = True
    collection["addon"] = "RVretep"
    collection["purpose"] = "Persistent realtime OpenXR tracking rig"

    def get_or_create(name, channel, offset):
        obj = bpy.data.objects.get(name)

        if obj is None:
            obj = bpy.data.objects.new(name, None)
            obj.empty_display_type = 'ARROWS'
            obj.empty_display_size = 0.2
            obj.rotation_mode = 'QUATERNION'
            obj.location = offset
            collection.objects.link(obj)
        else:
            # Re-link an existing live-rig object if it was moved out of the
            # collection manually. Do not alter user-created parenting.
            if obj.name not in {item.name for item in collection.objects}:
                try:
                    collection.objects.link(obj)
                except Exception:
                    pass

        obj.hide_render = True
        obj["rvretep_live_rig"] = True
        obj["vr_channel"] = channel
        obj["rvretep_role"] = "LIVE"
        return obj

    head = get_or_create(
        LIVE_OBJECT_HEAD,
        "HEAD",
        mathutils.Vector((0.0, 0.0, 0.0)),
    )
    hand_l = get_or_create(
        LIVE_OBJECT_HAND_L,
        "HAND_L",
        mathutils.Vector((-0.3, -0.4, -0.3)),
    )
    hand_r = get_or_create(
        LIVE_OBJECT_HAND_R,
        "HAND_R",
        mathutils.Vector((0.3, -0.4, -0.3)),
    )

    return collection, head, hand_l, hand_r


def update_live_rig_objects(
    context,
    head_pose,
    hand_l_pose=None,
    hand_r_pose=None,
    rig_objects=None,
):
    """Apply one OpenXR sample to the persistent live rig.

    ``rig_objects`` may be a cached (collection, head, hand_l, hand_r) tuple
    so high-frequency updates do not repeatedly search Blender's data API.
    """
    if rig_objects is None:
        rig_objects = ensure_live_rig(context)

    collection, head, hand_l, hand_r = rig_objects

    head_loc, head_rot = head_pose
    head.location = head_loc
    head.rotation_quaternion = head_rot

    if hand_l_pose is not None:
        hand_l.location = hand_l_pose[0]
        hand_l.rotation_quaternion = hand_l_pose[1]

    if hand_r_pose is not None:
        hand_r.location = hand_r_pose[0]
        hand_r.rotation_quaternion = hand_r_pose[1]

    # Property assignment already tags Blender's dependency graph. The XR
    # runtime handles its own draw loop. A desktop redraw is optional because
    # it is useful for parent/constraint debugging but can add UI overhead.
    if context.scene.rvretep_live_rig_desktop_preview:
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

    return collection, head, hand_l, hand_r


class _LiveRigRuntime:
    """Plain runtime state. Never store a Blender Operator instance globally."""
    __slots__ = ("generation", "timer", "objects", "suspended", "shutdown")

    def __init__(self, generation, objects):
        self.generation = generation
        self.timer = None
        self.objects = objects
        self.suspended = False
        self.shutdown = False

    def start(self, context):
        if self.timer is not None or self.shutdown:
            return
        hz = max(15.0, float(context.scene.rvretep_live_rig_hz))
        self.timer = context.window_manager.event_timer_add(
            1.0 / hz,
            window=context.window,
        )

    def stop(self, context):
        timer = self.timer
        self.timer = None
        if timer is not None:
            try:
                context.window_manager.event_timer_remove(timer)
            except Exception:
                pass


def stop_live_rig_runtime(context, runtime=None):
    global _LIVE_RIG_RUNTIME
    runtime = runtime or _LIVE_RIG_RUNTIME
    if runtime is None:
        return
    runtime.shutdown = True
    runtime.suspended = False
    runtime.stop(context)
    if _LIVE_RIG_RUNTIME is runtime:
        _LIVE_RIG_RUNTIME = None


class RVRETEP_OT_toggle_live_rig(bpy.types.Operator):
    bl_idname = "rvretep.toggle_live_rig"
    bl_label = "Toggle Live VR Rig"
    bl_description = "Enable or disable the persistent realtime OpenXR tracking rig"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _LIVE_RIG_RUNTIME, _LIVE_RIG_GENERATION

        scene = context.scene

        if scene.rvretep_live_rig_enabled:
            scene.rvretep_live_rig_enabled = False
            stop_live_rig_runtime(context)
            self.report(
                {'INFO'},
                "Live VR Rig disabled. The rig remains in the scene at its last pose.",
            )
            return {'FINISHED'}

        if not is_xr_running(context):
            self.report(
                {'ERROR'},
                "Cannot enable Live VR Rig: OpenXR is not running. Start the VR Session first.",
            )
            return {'CANCELLED'}

        # Invalidate and stop any stale runtime without retaining its Operator.
        stop_live_rig_runtime(context)

        try:
            objects = ensure_live_rig(context)
        except Exception as exc:
            self.report(
                {'ERROR'},
                f"Could not create the Live VR Rig: {exc}",
            )
            return {'CANCELLED'}

        _LIVE_RIG_GENERATION += 1
        runtime = _LiveRigRuntime(_LIVE_RIG_GENERATION, objects)
        runtime.suspended = bool(scene.rvretep_is_recording)
        _LIVE_RIG_RUNTIME = runtime
        scene.rvretep_live_rig_enabled = True

        try:
            result = bpy.ops.rvretep.live_rig_modal('INVOKE_DEFAULT')
        except Exception as exc:
            stop_live_rig_runtime(context, runtime)
            scene.rvretep_live_rig_enabled = False
            self.report(
                {'ERROR'},
                f"Could not start the Live VR Rig: {exc}",
            )
            return {'CANCELLED'}

        if 'RUNNING_MODAL' not in result:
            stop_live_rig_runtime(context, runtime)
            scene.rvretep_live_rig_enabled = False
            self.report(
                {'ERROR'},
                "Blender did not start the Live VR Rig modal handler.",
            )
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            f"Live VR Rig enabled at {scene.rvretep_live_rig_hz:.0f} Hz.",
        )
        return {'FINISHED'}


class RVRETEP_OT_live_rig_modal(bpy.types.Operator):
    bl_idname = "rvretep.live_rig_modal"
    bl_label = "Live VR Rig Runtime"
    bl_description = "Internal Live VR Rig tracking worker"
    bl_options = {'INTERNAL'}

    def invoke(self, context, _event):
        runtime = _LIVE_RIG_RUNTIME

        if runtime is None:
            self.report({'ERROR'}, "Live VR Rig runtime state is missing.")
            return {'CANCELLED'}

        self._runtime = runtime

        try:
            runtime.start(context)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not start the Live VR Rig timer: {exc}")
            return {'CANCELLED'}

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        runtime = getattr(self, "_runtime", None)

        if runtime is None or runtime.shutdown:
            if runtime is not None:
                runtime.stop(context)
            return {'FINISHED'}

        if _LIVE_RIG_RUNTIME is not runtime:
            runtime.stop(context)
            return {'FINISHED'}

        scene = context.scene

        if not scene.rvretep_live_rig_enabled:
            stop_live_rig_runtime(context, runtime)
            return {'FINISHED'}

        if runtime.suspended:
            return {'PASS_THROUGH'}

        if not is_xr_running(context):
            scene.rvretep_live_rig_enabled = False
            stop_live_rig_runtime(context, runtime)
            self.report(
                {'WARNING'},
                "Live VR Rig stopped because the OpenXR session is no longer running.",
            )
            return {'FINISHED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        try:
            xr_state = get_xr_state(context)
            if xr_state is None:
                raise RuntimeError("OpenXR session state is unavailable.")

            head_loc = tuple(float(v) for v in xr_state.viewer_pose_location)
            head_rot = tuple(float(v) for v in xr_state.viewer_pose_rotation)

            hand_l_data = None
            try:
                hl_loc = tuple(float(v) for v in xr_state.controller_grip_location_get(context, 0))
                hl_rot = tuple(float(v) for v in xr_state.controller_grip_rotation_get(context, 0))
                hand_l_data = (hl_loc, hl_rot)
            except Exception:
                pass

            hand_r_data = None
            try:
                hr_loc = tuple(float(v) for v in xr_state.controller_grip_location_get(context, 1))
                hr_rot = tuple(float(v) for v in xr_state.controller_grip_rotation_get(context, 1))
                hand_r_data = (hr_loc, hr_rot)
            except Exception:
                pass

            update_live_rig_objects(
                context,
                (head_loc, head_rot),
                hand_l_data,
                hand_r_data,
                runtime.objects,
            )

        except Exception as exc:
            scene.rvretep_live_rig_enabled = False
            stop_live_rig_runtime(context, runtime)
            self.report(
                {'ERROR'},
                f"Live VR Rig update failed and was stopped: {exc}",
            )
            return {'FINISHED'}

        return {'PASS_THROUGH'}


# -----------------------------------------------------------------------------
# Operators: XR session
# -----------------------------------------------------------------------------


class RVRETEP_OT_toggle_session(bpy.types.Operator):
    bl_idname = "rvretep.toggle_session"
    bl_label = "Toggle VR Session"
    bl_description = "Start or stop Blender's OpenXR session"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.window_manager is not None

    def invoke(self, context, event):
        if is_xr_running(context):
            return self.execute(context)

        return context.window_manager.invoke_props_dialog(self, width=430)

    def draw(self, context):
        layout = self.layout
        layout.label(text="OpenXR / VR Link is not currently running.", icon='ERROR')
        layout.separator()
        layout.label(text="Starting VR without a working OpenXR runtime")
        layout.label(text="or a fully connected headset can make Blender wait")
        layout.label(text="during native XR startup.")
        layout.separator()
        layout.label(text="Make sure your headset is in Quest Link / Air Link")
        layout.label(text="and your OpenXR runtime is configured before continuing.")
        layout.separator()
        layout.label(text="Continue anyway only if your XR setup is ready.", icon='INFO')

    def execute(self, context):
        global _LIVE_RIG_RUNTIME

        if context.scene.rvretep_is_recording:
            self.report({'ERROR'}, "Stop the VR recording before closing the XR session.")
            return {'CANCELLED'}

        # If the user closes XR manually, release the Live VR Rig modal timer
        # first so it cannot keep polling a session that no longer exists.
        if _LIVE_RIG_RUNTIME is not None and context.scene.rvretep_live_rig_enabled:
            stop_live_rig_runtime(context, _LIVE_RIG_RUNTIME)
            context.scene.rvretep_live_rig_enabled = False

        try:
            bpy.ops.wm.xr_session_toggle()
        except Exception as exc:
            self.report({'ERROR'}, f"Could not toggle the XR session: {exc}")
            return {'CANCELLED'}

        return {'FINISHED'}


class RVRETEP_OT_reset_tracking(bpy.types.Operator):
    bl_idname = "rvretep.reset_tracking"
    bl_label = "Reset VR Tracking"
    bl_description = "Reset the OpenXR viewer to Blender's base pose"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return is_xr_running(context)

    def execute(self, context):
        try:
            state = get_xr_state(context)
            result = state.reset_to_base_pose(context)
            if result:
                self.report({'INFO'}, "VR tracking reset to base pose.")
                return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to reset VR tracking: {exc}")
            return {'CANCELLED'}

        self.report({'WARNING'}, "Blender did not accept the tracking reset request.")
        return {'CANCELLED'}


# -----------------------------------------------------------------------------
# Diagnostics
# -----------------------------------------------------------------------------


class RVRETEP_OT_validate_setup(bpy.types.Operator):
    bl_idname = "rvretep.validate_setup"
    bl_label = "Validate VR Setup"
    bl_description = "Check XR, timeline, audio, and recording configuration"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        lines = []
        errors = []
        warnings = []

        # Blender version.
        current_version = bpy.app.version
        minimum_version = MIN_BLENDER_VERSION
        if current_version >= minimum_version:
            lines.append(f"Blender: {'.'.join(map(str, current_version))} ✓")
        else:
            errors.append(
                f"Blender {current_version} is below supported version {minimum_version}."
            )

        # XR.
        if is_xr_running(context):
            lines.append("OpenXR session: RUNNING ✓")
        else:
            warnings.append("OpenXR session is not running. Start it before recording.")

        # Playback state.
        lines.append(
            f"Playback range: {scene.frame_start}–{scene.frame_end} @ "
            f"{scene.render.fps / scene.render.fps_base:.3f} FPS"
        )
        if scene.frame_end <= scene.frame_start:
            errors.append("Playback end frame must be greater than the start frame.")

        # Audio.
        sound_strips = get_sound_strips(scene)
        sound_count = len(sound_strips)
        if sound_count:
            packed_count = sum(
                1
                for strip in sound_strips
                if getattr(getattr(strip, "sound", None), "packed_file", None) is not None
            )
            if packed_count:
                lines.append(
                    f"Audio: {sound_count} VSE Sound strip(s) found ✓ ({packed_count} packed)"
                )
            else:
                lines.append(f"Audio: {sound_count} VSE Sound strip(s) found ✓")
        else:
            datablock_count = len(list(getattr(bpy.data, "sounds", [])))
            if datablock_count:
                warnings.append(
                    "No VSE Sound strips were found, although the .blend contains Sound datablocks. "
                    "Timeline Audio Sync requires the audio to be a Sound strip in the Video Sequencer."
                )
            else:
                warnings.append(
                    "No VSE Sound strips were found. Recording will still work, but there is no Blender timeline audio to sync."
                )

        # Existing sessions.
        sessions = get_recording_collections()
        lines.append(f"VR sessions: {len(sessions)} existing")
        lines.append(f"Next session: {COLLECTION_PREFIX}{get_next_recording_number():03d}")

        # Settings.
        poll_hz = float(scene.rvretep_poll_hz)
        if poll_hz < 30:
            warnings.append("Capture rate below 30 Hz may produce noticeably coarse motion.")
        if poll_hz > 180:
            warnings.append("Very high capture rates can increase Python timer pressure without guaranteeing higher XR pose freshness.")

        lines.append(f"Capture rate target: {poll_hz:.0f} Hz")
        lines.append(f"Audio policy: {'enabled' if scene.rvretep_force_audio else 'preserve current scene setting'}")
        lines.append(f"Hide old sessions: {'ON' if scene.rvretep_hide_old_sessions else 'OFF'}")

        if scene.rvretep_is_recording:
            warnings.append("A recording is currently active.")

        if errors:
            popup_lines = ["ERRORS:"] + errors
            if warnings:
                popup_lines += ["WARNINGS:"] + warnings
            popup_message(context, "VR Setup — Attention Required", popup_lines, 'ERROR')
            self.report({'ERROR'}, errors[0])
            return {'CANCELLED'}

        popup_lines = lines
        if warnings:
            popup_lines += ["", "WARNINGS:"] + warnings
            icon = 'INFO'
            self.report({'WARNING'}, warnings[0])
        else:
            popup_lines += ["", "Setup looks good. ✓"]
            icon = 'CHECKMARK'
            self.report({'INFO'}, "VR setup validation passed.")

        popup_message(context, "VR Setup Diagnostics", popup_lines, icon)
        return {'FINISHED'}


class RVRETEP_OT_audio_diagnostics(bpy.types.Operator):
    bl_idname = "rvretep.audio_diagnostics"
    bl_label = "Audio Diagnostics"
    bl_description = "Inspect Blender Video Sequencer audio strips and packed sound data"
    bl_options = {'REGISTER'}

    def execute(self, context):
        lines = audio_diagnostic_lines(context.scene)
        has_strips = bool(get_sound_strips(context.scene))

        popup_message(
            context,
            "VR Audio Diagnostics",
            lines,
            'CHECKMARK' if has_strips else 'ERROR',
        )

        if has_strips:
            self.report({'INFO'}, f"Found {count_audio_strips(context.scene)} VSE Sound strip(s).")
            return {'FINISHED'}

        self.report(
            {'WARNING'},
            "No VSE Sound strips were detected. See the Audio Diagnostics popup for details."
        )
        return {'CANCELLED'}


# -----------------------------------------------------------------------------
# Session visibility / management
# -----------------------------------------------------------------------------


class RVRETEP_OT_show_all_recordings(bpy.types.Operator):
    bl_idname = "rvretep.show_all_recordings"
    bl_label = "Show All VR Sessions"
    bl_description = "Make every recorded VR session visible"
    bl_options = {'REGISTER'}

    def execute(self, context):
        sessions = get_recording_collections()
        if not sessions:
            self.report({'WARNING'}, "There are no VR sessions to show.")
            return {'CANCELLED'}

        for col in sessions:
            set_recording_visibility(col, True)

        self.report({'INFO'}, f"Showing {len(sessions)} VR session(s).")
        return {'FINISHED'}


class RVRETEP_OT_hide_old_recordings(bpy.types.Operator):
    bl_idname = "rvretep.hide_old_recordings"
    bl_label = "Hide Old VR Sessions"
    bl_description = "Hide every VR session except the active one"
    bl_options = {'REGISTER'}

    def execute(self, context):
        active = context.scene.rvretep_active_session_id
        if active == "NONE" or not get_recording_collection(active):
            self.report({'WARNING'}, "No active VR session is selected.")
            return {'CANCELLED'}

        for col in get_recording_collections():
            set_recording_visibility(col, col.name == active)

        self.report({'INFO'}, "Old VR sessions hidden; active session remains visible.")
        return {'FINISHED'}


class RVRETEP_OT_show_active_recording(bpy.types.Operator):
    bl_idname = "rvretep.show_active_recording"
    bl_label = "Show Active"
    bl_description = "Show the active session and select its recorded objects"
    bl_options = {'REGISTER'}

    def execute(self, context):
        col = get_recording_collection(context.scene.rvretep_active_session_id)
        if col is None:
            self.report({'WARNING'}, "No active VR session is selected.")
            return {'CANCELLED'}

        set_recording_visibility(col, True)

        for obj in context.selected_objects:
            obj.select_set(False)

        first = None
        for obj in col.objects:
            obj.hide_viewport = False
            obj.select_set(True)
            if first is None:
                first = obj

        if first is not None:
            context.view_layer.objects.active = first

        self.report({'INFO'}, f"Showing {col.name}.")
        return {'FINISHED'}


class RVRETEP_OT_rename_recording(bpy.types.Operator):
    bl_idname = "rvretep.rename_recording"
    bl_label = "Rename VR Session"
    bl_description = "Change the display name of the active VR session"
    bl_options = {'REGISTER', 'UNDO'}

    new_name: bpy.props.StringProperty(
        name="Session Name",
        description="Human-readable name for this VR session",
        default="VR Capture",
        maxlen=96,
    )

    def invoke(self, context, event):
        col = get_recording_collection(context.scene.rvretep_active_session_id)
        if col is None:
            self.report({'WARNING'}, "No active VR session is selected.")
            return {'CANCELLED'}
        self.new_name = display_name_for_collection(col)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "new_name")

    def execute(self, context):
        col = get_recording_collection(context.scene.rvretep_active_session_id)
        if col is None:
            self.report({'ERROR'}, "The selected VR session no longer exists.")
            return {'CANCELLED'}

        cleaned = self.new_name.strip()
        if not cleaned:
            self.report({'ERROR'}, "Session name cannot be empty.")
            return {'CANCELLED'}

        col["display_name"] = cleaned
        self.report({'INFO'}, f"Renamed {col.name} to '{cleaned}'.")
        return {'FINISHED'}


class RVRETEP_OT_delete_recording(bpy.types.Operator):
    bl_idname = "rvretep.delete_recording"
    bl_label = "Delete VR Session"
    bl_description = "Permanently delete the active VR recording and its animation data"
    bl_options = {'REGISTER'}
    @classmethod
    def poll(cls, context):
        return not context.scene.rvretep_is_recording

    def invoke(self, context, event):
        col = get_recording_collection(context.scene.rvretep_active_session_id)
        if col is None:
            self.report({'WARNING'}, "No active VR session is selected.")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        identifier = context.scene.rvretep_active_session_id
        col = get_recording_collection(identifier)
        if col is None:
            self.report({'ERROR'}, "The selected VR session no longer exists.")
            return {'CANCELLED'}

        number = get_recording_number(col.name)
        delete_recording_collection(col)

        remaining = get_recording_collections()
        if remaining:
            context.scene.rvretep_active_session_id = remaining[-1].name
        else:
            context.scene.rvretep_active_session_id = "NONE"

        self.report({'INFO'}, f"Deleted {COLLECTION_PREFIX}{number:03d}.")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Recording operator
# -----------------------------------------------------------------------------


class RVRETEP_OT_record_gameplay(bpy.types.Operator):
    bl_idname = "rvretep.record_gameplay"
    bl_label = "Start VR Recording"
    bl_description = "Play Blender's animation/audio while recording OpenXR headset and controller poses"
    bl_options = {'REGISTER'}

    _timer = None

    def execute(self, context):
        global _RECORDER_ACTIVE

        scene = context.scene

        # A second invocation while the modal recorder is active is used as
        # the stop request. The original modal instance performs the bake.
        if scene.rvretep_is_recording:
            scene.rvretep_is_recording = False
            return {'FINISHED'}

        if _RECORDER_ACTIVE:
            self.report({'ERROR'}, "Another VR recording operator is already active.")
            return {'CANCELLED'}

        # --------------------------------------------------------------
        # Validation
        # --------------------------------------------------------------
        if bpy.app.version < MIN_BLENDER_VERSION:
            minimum_text = ".".join(map(str, MIN_BLENDER_VERSION))
            self.report(
                {'ERROR'},
                f"Blender {bpy.app.version} is not supported. Blender {minimum_text} or newer is required."
            )
            return {'CANCELLED'}

        if not is_xr_running(context):
            self.report(
                {'ERROR'},
                "OpenXR is not running. Start the VR Session first, then start recording."
            )
            return {'CANCELLED'}

        if scene.frame_end <= scene.frame_start:
            self.report({'ERROR'}, "Playback range is invalid: End must be greater than Start.")
            return {'CANCELLED'}

        # --------------------------------------------------------------
        # Save playback settings so the operator is non-destructive.
        # --------------------------------------------------------------
        self.live_rig_was_running = False
        if (
            scene.rvretep_live_rig_enabled
            and _LIVE_RIG_RUNTIME is not None
        ):
            _LIVE_RIG_RUNTIME.suspended = True
            self.live_rig_was_running = True

        self.old_sync_mode = scene.sync_mode
        self.old_use_audio = scene.use_audio

        # The user asked for realtime sound sync. Force it during capture,
        # then restore the user's previous settings on completion.
        if scene.rvretep_force_audio:
            scene.use_audio = True
        scene.sync_mode = 'AUDIO_SYNC'

        # --------------------------------------------------------------
        # Create a new independent session.
        # --------------------------------------------------------------
        try:
            self.setup_rig(context)
        except Exception as exc:
            self.restore_playback_settings(scene)
            self.report({'ERROR'}, f"Could not create VR recording session: {exc}")
            return {'CANCELLED'}

        # --------------------------------------------------------------
        # Initialize capture state.
        # --------------------------------------------------------------
        self.prev_quats = {
            'head': None,
            'hand_l': None,
            'hand_r': None,
        }
        self.samples = []
        self.start_time = time.perf_counter()
        self.start_frame_float = current_timeline_position(scene)
        self.start_frame_int = scene.frame_current
        self.start_subframe = scene.frame_subframe
        self.started_animation_playback = False
        self.playback_was_running = False
        self.stop_reason = "User stopped recording."
        self.live_rig_objects = (
            _LIVE_RIG_RUNTIME.objects
            if _LIVE_RIG_RUNTIME is not None
            else None
        )
        if scene.rvretep_live_rig_enabled and self.live_rig_objects is None:
            try:
                self.live_rig_objects = ensure_live_rig(context)
            except Exception as exc:
                self.report({'ERROR'}, f"Live VR Rig is enabled but could not be initialized: {exc}")
                self.cleanup_unbaked_recording(context)
                return {'CANCELLED'}
        self._finalizing = False
        self._last_timer_time = self.start_time
        self._timer_event_count = 0

        if scene.rvretep_create_markers:
            marker_name = f"VR {self.recording_collection.name} START"
            try:
                scene.timeline_markers.new(marker_name, frame=scene.frame_current)
                self.start_marker_name = marker_name
            except Exception:
                self.start_marker_name = None
        else:
            self.start_marker_name = None

        # Capture an initial sample at the exact starting timeline position
        # before playback advances. This gives the bake a clean first key.
        try:
            self.sample_frame(context)
        except Exception as exc:
            self.cleanup_unbaked_recording(context)
            self.report({'ERROR'}, f"Could not capture the initial VR pose: {exc}")
            return {'CANCELLED'}

        # --------------------------------------------------------------
        # Start Blender playback.
        # --------------------------------------------------------------
        try:
            if not context.screen.is_animation_playing:
                bpy.ops.screen.animation_play()
                self.started_animation_playback = True
                self.playback_was_running = True
            else:
                self.playback_was_running = True
        except Exception as exc:
            self.cleanup_unbaked_recording(context)
            self.report({'ERROR'}, f"Could not start Blender animation playback: {exc}")
            return {'CANCELLED'}

        # --------------------------------------------------------------
        # Start a lightweight pose polling timer.
        # --------------------------------------------------------------
        try:
            poll_hz = max(1.0, float(scene.rvretep_poll_hz))
            self._timer = context.window_manager.event_timer_add(
                1.0 / poll_hz,
                window=context.window,
            )
            _RECORDER_TIMER = self._timer
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            self.cleanup_unbaked_recording(context)
            self.report({'ERROR'}, f"Could not start the VR capture timer: {exc}")
            return {'CANCELLED'}

        scene.rvretep_is_recording = True
        _RECORDER_ACTIVE = True

        audio_count = count_audio_strips(scene)
        if audio_count:
            audio_text = f"Audio sync ON ({audio_count} Sound strip(s))"
        else:
            audio_text = "Audio sync ON (no Sound strips detected)"

        self.report(
            {'INFO'},
            f"{self.recording_collection.name} recording started — {audio_text}."
        )

        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        # Do not do any dependency graph or viewport redraw work here.
        # This is intentionally the lightweight path.
        if context.scene.rvretep_is_recording is False:
            self.stop_recording(context)
            return {'FINISHED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        self._timer_event_count += 1

        # XR can be disconnected or closed while Blender remains open.
        if not is_xr_running(context):
            self.stop_reason = "The OpenXR session stopped unexpectedly."
            context.scene.rvretep_is_recording = False
            self.stop_recording(context)
            return {'FINISHED'}

        # Blender's own playback clock is authoritative. If playback stops,
        # either finalize immediately (default) or pause capture until the
        # user resumes playback. In neither case do we invent our own clock.
        if not context.screen.is_animation_playing:
            if context.scene.rvretep_stop_when_playback_stops:
                self.stop_reason = "Blender playback stopped before recording was manually stopped."
                context.scene.rvretep_is_recording = False
                self.stop_recording(context)
                return {'FINISHED'}
            return {'PASS_THROUGH'}

        try:
            self.sample_frame(context)
        except Exception as exc:
            self.stop_reason = f"Capture error: {exc}"
            context.scene.rvretep_is_recording = False
            self.stop_recording(context)
            return {'FINISHED'}

        # Progress/status values are updated in memory only. We deliberately
        # avoid tag_redraw here to keep desktop viewport overhead at zero.
        return {'PASS_THROUGH'}

    # ------------------------------------------------------------------
    # Session creation
    # ------------------------------------------------------------------

    # DEV EASTER EGG: no, the empty is not haunted. retep checked.

    def setup_rig(self, context):
        master_col = ensure_master_collection(context.scene)

        # Optional visibility behavior. OFF by default so prior recordings
        # remain visible, exactly matching the user's preferred workflow.
        if context.scene.rvretep_hide_old_sessions:
            for col in get_recording_collections():
                set_recording_visibility(col, False)

        number = get_next_recording_number()
        collection_name = f"{COLLECTION_PREFIX}{number:03d}"
        display_name = context.scene.rvretep_session_name.strip() or f"VR Capture {number:03d}"

        # Guard against an externally-created collection collision.
        while bpy.data.collections.get(collection_name) is not None:
            number += 1
            collection_name = f"{COLLECTION_PREFIX}{number:03d}"

        col = bpy.data.collections.new(collection_name)
        master_col.children.link(col)
        set_recording_visibility(col, True)
        col.hide_render = True

        self.recording_number = number
        self.recording_collection = col

        context.scene.rvretep_active_session_id = collection_name

        mark_session_collection(
            col,
            display_name=display_name,
            addon="RVretep",
            addon_version="{}.{}.{}".format(*ADDON_VERSION),
            blender_version="{}.{}.{}".format(*bpy.app.version),
            capture_rate_target_hz=float(context.scene.rvretep_poll_hz),
            audio_forced=bool(context.scene.rvretep_force_audio),
            hide_old_sessions=bool(context.scene.rvretep_hide_old_sessions),
            created_utc=datetime.now(timezone.utc).isoformat(),
            frame_start=float(current_timeline_position(context.scene)),
            scene_fps=float(context.scene.render.fps / context.scene.render.fps_base),
            audio_strip_count=int(count_audio_strips(context.scene)),
            completed=False,
        )

        def create_empty(label: str, channel: str, offset):
            # Names include the session ID so multiple sessions are never
            # confused in the Outliner or Python API.
            obj = bpy.data.objects.new(
                f"{label}_{number:03d}",
                None,
            )
            obj.empty_display_type = 'ARROWS'
            obj.empty_display_size = 0.2
            obj.rotation_mode = 'QUATERNION'
            obj.hide_render = True
            obj.location = offset
            col.objects.link(obj)

            obj["vr_session"] = collection_name
            obj["vr_channel"] = channel
            obj["vr_recorder"] = "RVretep"

            return obj

        self.head = create_empty(
            OBJECT_HEAD,
            "HEAD",
            mathutils.Vector((0.0, 0.0, 0.0)),
        )

        self.hand_l = create_empty(
            OBJECT_HAND_L,
            "HAND_L",
            mathutils.Vector((-0.3, -0.4, -0.3)),
        )

        self.hand_r = create_empty(
            OBJECT_HAND_R,
            "HAND_R",
            mathutils.Vector((0.3, -0.4, -0.3)),
        )

    # ------------------------------------------------------------------
    # Quaternion continuity
    # ------------------------------------------------------------------

    def safe_quat(self, raw_quat, key_name: str):
        quat = mathutils.Quaternion(raw_quat)

        previous = self.prev_quats.get(key_name)
        if previous is not None and previous.dot(quat) < 0.0:
            quat.negate()

        self.prev_quats[key_name] = quat.copy()
        return quat

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def sample_frame(self, context):
        xr_state = get_xr_state(context)
        if not xr_state or not xr_state.is_running(context):
            raise RuntimeError("OpenXR session is no longer running.")

        scene = context.scene
        now = time.perf_counter()
        current_frame = current_timeline_position(scene)

        # Viewer pose.
        head_loc = tuple(float(v) for v in xr_state.viewer_pose_location)
        head_rot = tuple(
            float(v)
            for v in self.safe_quat(xr_state.viewer_pose_rotation, 'head')
        )

        # Controller grip poses. A missing/unsupported controller is represented
        # by None rather than terminating the entire recording.
        hand_l_data = None
        try:
            hl_loc = tuple(
                float(v)
                for v in xr_state.controller_grip_location_get(context, 0)
            )
            hl_rot = tuple(
                float(v)
                for v in self.safe_quat(
                    xr_state.controller_grip_rotation_get(context, 0),
                    'hand_l',
                )
            )
            hand_l_data = (hl_loc, hl_rot)
        except Exception:
            pass

        hand_r_data = None
        try:
            hr_loc = tuple(
                float(v)
                for v in xr_state.controller_grip_location_get(context, 1)
            )
            hr_rot = tuple(
                float(v)
                for v in self.safe_quat(
                    xr_state.controller_grip_rotation_get(context, 1),
                    'hand_r',
                )
            )
            hand_r_data = (hr_loc, hr_rot)
        except Exception:
            pass

        # Optional persistent live rig. This happens only when explicitly
        # enabled, and reuses the same XR sample we are already capturing.
        if scene.rvretep_live_rig_enabled:
            update_live_rig_objects(
                context,
                (head_loc, head_rot),
                hand_l_data,
                hand_r_data,
                self.live_rig_objects,
            )

        # Store raw samples only. The recording rig itself is not touched until
        # baking, preserving the lightweight capture path when Live VR Rig is OFF.
        self.samples.append({
            'timestamp': now,
            'frame': current_frame,
            'head': (head_loc, head_rot),
            'hand_l': hand_l_data,
            'hand_r': hand_r_data,
        })

    # ------------------------------------------------------------------
    # Stop + bake
    # ------------------------------------------------------------------

    # DEV EASTER EGG: retep is still aggressively resisting 90 Hz viewport redraws.

    def stop_recording(self, context):
        global _RECORDER_ACTIVE, _RECORDER_TIMER

        if self._finalizing:
            return
        self._finalizing = True

        scene = context.scene

        # Timer cleanup first so no new capture event competes with baking.
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
            _RECORDER_TIMER = None

        # Stop Blender playback only when this operator owns it.
        if self.started_animation_playback:
            try:
                if context.screen.is_animation_playing:
                    bpy.ops.screen.animation_cancel(restore_frame=False)
            except Exception as exc:
                self.report({'WARNING'}, f"Playback could not be stopped cleanly: {exc}")

        sample_count = len(self.samples)

        if sample_count == 0:
            cleanup_name = self.recording_collection.name if self.recording_collection else "session"
            cleanup_unbaked = True
            if cleanup_unbaked and self.recording_collection:
                delete_recording_collection(self.recording_collection)

            self.restore_playback_settings(scene)
            scene.rvretep_is_recording = False
            scene.rvretep_active_session_id = "NONE"
            _RECORDER_ACTIVE = False

            if scene.rvretep_live_rig_enabled and _LIVE_RIG_RUNTIME is not None:
                try:
                    _LIVE_RIG_RUNTIME.suspended = False
                except Exception as exc:
                    self.report({'WARNING'}, f"Live VR Rig could not resume: {exc}")

            self.report({'WARNING'}, f"{cleanup_name}: no VR samples were captured; session discarded.")
            return

        try:
            # Keep samples in temporal order. Blender's playback may drop visual
            # frames under Audio Sync; samples must still be baked in timeline order.
            self.samples.sort(key=lambda sample: sample['frame'])

            # If multiple timer events land on essentially the same timeline
            # position, keep the newest sample so the bake never generates a
            # pointless pile of coincident keys.
            cleaned = []
            last_frame = None
            for sample in self.samples:
                frame = float(sample['frame'])
                if last_frame is not None and math.isclose(
                    frame, last_frame, rel_tol=0.0, abs_tol=1e-6
                ):
                    cleaned[-1] = sample
                else:
                    cleaned.append(sample)
                    last_frame = frame
            self.samples = cleaned

            self.bake_channel(
                self.head,
                'HEAD',
                lambda s: s['head'],
            )
            self.bake_channel(
                self.hand_l,
                'HAND_L',
                lambda s: s['hand_l'],
                allow_none=True,
            )
            self.bake_channel(
                self.hand_r,
                'HAND_R',
                lambda s: s['hand_r'],
                allow_none=True,
            )

            # Label resulting actions for sane Outliner / Dope Sheet browsing.
            set_action_name(self.head, f"{self.recording_collection.name} | Head")
            set_action_name(self.hand_l, f"{self.recording_collection.name} | Hand L")
            set_action_name(self.hand_r, f"{self.recording_collection.name} | Hand R")

            # Metadata.
            end_frame = float(self.samples[-1]['frame'])
            duration = float(self.samples[-1]['timestamp'] - self.start_time)
            effective_hz = (len(self.samples) / duration) if duration > 0.0 else 0.0

            gaps = []
            prev_time = None
            for sample in self.samples:
                timestamp = float(sample['timestamp'])
                if prev_time is not None:
                    gaps.append(timestamp - prev_time)
                prev_time = timestamp

            max_gap = max(gaps) if gaps else 0.0
            scene_fps = float(scene.render.fps / scene.render.fps_base)

            mark_session_collection(
                self.recording_collection,
                completed=True,
                display_name=display_name_for_collection(self.recording_collection),
                frame_start=float(self.start_frame_float),
                frame_end=end_frame,
                frame_count=float(max(0.0, end_frame - self.start_frame_float)),
                duration_seconds=duration,
                sample_count=len(self.samples),
                effective_sample_hz=effective_hz,
                max_sample_gap_seconds=max_gap,
                scene_fps=scene_fps,
                final_reason=self.stop_reason,
                finished_utc=datetime.now(timezone.utc).isoformat(),
            )

            if scene.rvretep_create_markers:
                try:
                    end_marker_name = f"VR {self.recording_collection.name} END"
                    scene.timeline_markers.new(end_marker_name, frame=round(end_frame))
                except Exception:
                    pass

            self.recording_collection.hide_viewport = False

            self.restore_playback_settings(scene)
            scene.rvretep_is_recording = False

            _RECORDER_ACTIVE = False

            if scene.rvretep_live_rig_enabled and _LIVE_RIG_RUNTIME is not None:
                try:
                    _LIVE_RIG_RUNTIME.suspended = False
                except Exception as exc:
                    self.report({'WARNING'}, f"VR recording finished, but Live VR Rig could not resume: {exc}")

            if self.stop_reason != "User stopped recording.":
                self.report(
                    {'WARNING'},
                    f"{self.recording_collection.name} finalized: {self.stop_reason}"
                )
            else:
                self.report(
                    {'INFO'},
                    f"{self.recording_collection.name} baked: {len(self.samples)} samples, "
                    f"{duration:.2f}s, {effective_hz:.1f} Hz effective capture."
                )

        except Exception as exc:
            # Never silently leave the user with a half-recorded session.
            # Keep what was successfully created so the user can inspect it,
            # but make the failure unmistakable.
            mark_session_collection(
                self.recording_collection,
                completed=False,
                error=str(exc),
                error_utc=datetime.now(timezone.utc).isoformat(),
            )
            self.restore_playback_settings(scene)
            scene.rvretep_is_recording = False
            _RECORDER_ACTIVE = False

            if scene.rvretep_live_rig_enabled and _LIVE_RIG_RUNTIME is not None:
                try:
                    _LIVE_RIG_RUNTIME.suspended = False
                except Exception as resume_exc:
                    self.report({'WARNING'}, f"Live VR Rig could not resume after bake failure: {resume_exc}")

            self.report(
                {'ERROR'},
                f"VR bake failed. Session {self.recording_collection.name} was kept for inspection. Error: {exc}"
            )

    def bake_channel(self, obj, channel_name: str, sample_getter, allow_none=False):
        valid_samples = []

        for sample in self.samples:
            data = sample_getter(sample)
            if data is None:
                if allow_none:
                    continue
                raise RuntimeError(f"No data was captured for required channel {channel_name}.")
            valid_samples.append((float(sample['frame']), data))

        if not valid_samples:
            if allow_none:
                return
            raise RuntimeError(f"No valid samples exist for required channel {channel_name}.")

        for frame, data in valid_samples:
            loc, quat = data

            obj.location = loc
            obj.rotation_quaternion = quat

            obj.keyframe_insert(
                data_path="location",
                frame=frame,
                group=channel_name,
            )
            obj.keyframe_insert(
                data_path="rotation_quaternion",
                frame=frame,
                group=channel_name,
            )

        fcurves = collect_action_fcurves(obj)
        if fcurves:
            for fcurve in fcurves:
                for point in fcurve.keyframe_points:
                    point.interpolation = 'LINEAR'
                fcurve.update()

    def restore_playback_settings(self, scene):
        try:
            scene.sync_mode = self.old_sync_mode
        except Exception:
            pass
        try:
            scene.use_audio = self.old_use_audio
        except Exception:
            pass

    def cleanup_unbaked_recording(self, context):
        global _RECORDER_ACTIVE, _RECORDER_TIMER

        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
            _RECORDER_TIMER = None

        if self.started_animation_playback:
            try:
                if context.screen.is_animation_playing:
                    bpy.ops.screen.animation_cancel(restore_frame=False)
            except Exception:
                pass

        if getattr(self, 'recording_collection', None):
            delete_recording_collection(self.recording_collection)

        self.restore_playback_settings(context.scene)
        context.scene.rvretep_is_recording = False
        context.scene.rvretep_active_session_id = "NONE"
        _RECORDER_ACTIVE = False

    # ------------------------------------------------------------------
    # Emergency cleanup when Blender reloads/disables the add-on.
    # ------------------------------------------------------------------

    def emergency_cleanup(self, context=None):
        try:
            if context is None:
                return
            if self._timer is not None:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
            if self.started_animation_playback and context.screen.is_animation_playing:
                bpy.ops.screen.animation_cancel(restore_frame=False)
            if hasattr(context, 'scene'):
                context.scene.rvretep_is_recording = False
                self.restore_playback_settings(context.scene)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Smoothing
# -----------------------------------------------------------------------------


class RVRETEP_OT_apply_smoothing(bpy.types.Operator):
    bl_idname = "rvretep.apply_smoothing"
    bl_label = "Apply Smoothing"
    bl_description = "Apply optional post-process smoothing to the active VR session"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return not context.scene.rvretep_is_recording

    def execute(self, context):
        identifier = context.scene.rvretep_active_session_id
        col = get_recording_collection(identifier)
        if col is None:
            self.report({'WARNING'}, "No active VR session is selected.")
            return {'CANCELLED'}

        passes = int(context.scene.rvretep_smoothing_passes)
        window = int(context.scene.rvretep_smoothing_window)

        if window % 2 == 0:
            self.report({'ERROR'}, "Smoothing window must be an odd number.")
            return {'CANCELLED'}

        changed = 0
        for obj in col.objects:
            if self.smooth_object(obj, passes, window):
                changed += 1

        if changed == 0:
            self.report({'WARNING'}, "No animation curves were found to smooth in the active session.")
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            f"Applied {passes} smoothing pass(es), window {window}, to {changed} object(s)."
        )
        return {'FINISHED'}

    @staticmethod
    def smooth_object(obj, passes=1, window_size=5):
        fcurves = collect_action_fcurves(obj)
        if not fcurves:
            return False

        for fcurve in fcurves:
            points = fcurve.keyframe_points
            count = len(points)

            for point in points:
                point.interpolation = 'LINEAR'

            if count < window_size:
                continue

            current = [point.co[1] for point in points]

            for _ in range(passes):
                new_values = []
                half = window_size // 2

                for index in range(count):
                    start = max(0, index - half)
                    end = min(count, index + half + 1)
                    window_values = current[start:end]
                    new_values.append(sum(window_values) / len(window_values))

                current = new_values

            for index, value in enumerate(current):
                points[index].co[1] = value
                points[index].handle_left_type = 'AUTO'
                points[index].handle_right_type = 'AUTO'

            fcurve.update()

        # Normalize quaternion channels after smoothing when all four exist.
        fc_w = fcurves.find("rotation_quaternion", index=0)
        fc_x = fcurves.find("rotation_quaternion", index=1)
        fc_y = fcurves.find("rotation_quaternion", index=2)
        fc_z = fcurves.find("rotation_quaternion", index=3)

        if fc_w and fc_x and fc_y and fc_z:
            count = min(
                len(fc_w.keyframe_points),
                len(fc_x.keyframe_points),
                len(fc_y.keyframe_points),
                len(fc_z.keyframe_points),
            )

            for index in range(count):
                q = mathutils.Quaternion((
                    fc_w.keyframe_points[index].co[1],
                    fc_x.keyframe_points[index].co[1],
                    fc_y.keyframe_points[index].co[1],
                    fc_z.keyframe_points[index].co[1],
                ))

                if q.length < 1e-8:
                    q = mathutils.Quaternion((1.0, 0.0, 0.0, 0.0))
                else:
                    q.normalize()

                fc_w.keyframe_points[index].co[1] = q.w
                fc_x.keyframe_points[index].co[1] = q.x
                fc_y.keyframe_points[index].co[1] = q.y
                fc_z.keyframe_points[index].co[1] = q.z

            fc_w.update()
            fc_x.update()
            fc_y.update()
            fc_z.update()

        return True


# -----------------------------------------------------------------------------
# Session metadata / active selection
# -----------------------------------------------------------------------------


class RVRETEP_OT_select_active(bpy.types.Operator):
    bl_idname = "rvretep.select_active"
    bl_label = "Refresh Sessions"
    bl_description = "Refresh the VR session list and repair the active selection if needed"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        sessions = get_recording_collections()
        session_names = {col.name for col in sessions}
        current = scene.rvretep_active_session_id

        # Dynamic EnumProperty values can become stale after collections are
        # created/deleted. Always choose a currently valid identifier.
        if current not in session_names:
            if sessions:
                scene.rvretep_active_session_id = sessions[-1].name
            else:
                scene.rvretep_active_session_id = "NONE"

        # Force the UI to reflect the refreshed dynamic enum immediately.
        for window in context.window_manager.windows:
            screen = window.screen
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        selected = get_recording_collection(scene.rvretep_active_session_id)
        if selected is None:
            self.report({'INFO'}, "VR session list refreshed — no sessions currently exist.")
        else:
            self.report(
                {'INFO'},
                f"VR session list refreshed — active: {selected.name} — {display_name_for_collection(selected)}"
            )

        return {'FINISHED'}




class RVRETEP_OT_select_live_rig(bpy.types.Operator):
    bl_idname = "rvretep.select_live_rig"
    bl_label = "Select Live VR Rig"
    bl_description = "Select the persistent VRLive head and controller empties"
    bl_options = {'REGISTER'}

    def execute(self, context):
        rig = bpy.data.collections.get(LIVE_COLLECTION_NAME)
        if rig is None:
            self.report({'WARNING'}, "The Live VR Rig has not been created yet. Enable it first.")
            return {'CANCELLED'}

        for obj in context.selected_objects:
            obj.select_set(False)

        active = None
        for name in (LIVE_OBJECT_HEAD, LIVE_OBJECT_HAND_L, LIVE_OBJECT_HAND_R):
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            obj.hide_viewport = False
            obj.select_set(True)
            if active is None:
                active = obj

        if active is None:
            self.report({'WARNING'}, "The Live VR Rig collection exists but its tracking empties are missing.")
            return {'CANCELLED'}

        context.view_layer.objects.active = active

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

        self.report({'INFO'}, "Selected the Live VR Rig head and controller empties.")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------


class RVRETEP_PT_panel(bpy.types.Panel):
    bl_label = "VR Feedback Recorder"
    bl_idname = "RVRETEP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "VR Recording"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        xr_active = is_xr_running(context)

        # ----------------------------------------------------------
        # STATUS
        # ----------------------------------------------------------
        box = layout.box()
        box.label(text="STATUS", icon='REC')

        row = box.row()
        row.alert = scene.rvretep_is_recording
        if scene.rvretep_is_recording:
            row.label(text="RECORDING", icon='RADIOBUT_ON')
        elif xr_active:
            row.label(text="VR SESSION ACTIVE", icon='XRAY')
        else:
            row.label(text="READY", icon='CHECKMARK')

        # ----------------------------------------------------------
        # XR CONTROLS
        # ----------------------------------------------------------
        if xr_active:
            layout.operator(
                "rvretep.toggle_session",
                text="Close VR Session",
                icon='CANCEL',
            )
            layout.operator(
                "rvretep.reset_tracking",
                text="Reset Tracking",
                icon='LOOP_BACK',
            )
        else:
            warning_box = layout.box()
            warning_box.alert = True
            warning_box.label(text="VR LINK CHECK", icon='ERROR')
            warning_box.label(text="Headset must be fully connected.")
            warning_box.label(text="Quest Link / Air Link should show Connected.")
            warning_box.label(text="OpenXR must be configured before starting.")
            layout.operator(
                "rvretep.toggle_session",
                text="Start VR Session",
                icon='XRAY',
            )

        # ----------------------------------------------------------
        # LIVE VR RIG
        # ----------------------------------------------------------
        live_box = layout.box()
        live_box.label(text="Live VR Rig", icon='XRAY')

        live_row = live_box.row()
        live_row.scale_y = 1.15
        if scene.rvretep_live_rig_enabled:
            live_row.alert = True
            live_row.operator(
                "rvretep.toggle_live_rig",
                text="DISABLE LIVE VR RIG",
                icon='PAUSE',
            )
        else:
            live_row.enabled = xr_active
            live_row.operator(
                "rvretep.toggle_live_rig",
                text="ENABLE LIVE VR RIG",
                icon='XRAY',
            )

        live_box.prop(
            scene,
            "rvretep_live_rig_hz",
            text="Update Rate",
        )
        live_box.prop(
            scene,
            "rvretep_live_rig_desktop_preview",
            text="Desktop Viewport Preview",
        )

        if scene.rvretep_live_rig_enabled:
            live_box.label(
                text="VRLive_Head / VRLive_Hand_L / VRLive_Hand_R",
                icon='INFO',
            )
            live_box.label(
                text="Parent or constrain objects to these empties.",
                icon='CONSTRAINT',
            )
            live_box.operator(
                "rvretep.select_live_rig",
                text="Select Live Rig",
                icon='RESTRICT_SELECT_OFF',
            )
        else:
            live_box.label(
                text="Separate from recorded VRDATA sessions.",
                icon='INFO',
            )

        layout.separator()

        # ----------------------------------------------------------
        # RECORD
        # ----------------------------------------------------------
        record_row = layout.row()
        record_row.scale_y = 1.35
        record_row.alert = scene.rvretep_is_recording

        if scene.rvretep_is_recording:
            record_row.operator(
                "rvretep.record_gameplay",
                text="STOP & BAKE VR RECORDING",
                icon='PAUSE',
            )
        else:
            record_row.enabled = xr_active
            record_row.operator(
                "rvretep.record_gameplay",
                text="START VR RECORDING",
                icon='REC',
            )

        # ----------------------------------------------------------
        # RECORDING OPTIONS
        # ----------------------------------------------------------
        box = layout.box()
        box.label(text="Recording", icon='SETTINGS')

        row = box.row()
        row.enabled = not scene.rvretep_is_recording
        row.prop(scene, "rvretep_session_name", text="Name")

        row = box.row()
        row.enabled = not scene.rvretep_is_recording
        row.prop(scene, "rvretep_poll_hz", text="Capture Rate")

        box.prop(
            scene,
            "rvretep_force_audio",
            text="Force Audio + Audio Sync",
        )
        box.prop(
            scene,
            "rvretep_hide_old_sessions",
            text="Hide Old VR Sessions",
        )
        box.prop(
            scene,
            "rvretep_create_markers",
            text="Create Timeline Markers",
        )
        box.prop(
            scene,
            "rvretep_stop_when_playback_stops",
            text="Stop if Playback Stops",
        )

        audio_count = count_audio_strips(scene)
        audio_icon = 'SPEAKER' if audio_count else 'ERROR'
        audio_box = box.row()
        audio_box.label(text=audio_status(scene), icon=audio_icon)

        audio_diag = box.row()
        audio_diag.operator(
            "rvretep.audio_diagnostics",
            text="Audio Diagnostics",
            icon='SPEAKER',
        )

        # ----------------------------------------------------------
        # SESSIONS
        # ----------------------------------------------------------
        box = layout.box()
        box.label(text="VR Sessions", icon='OUTLINER_COLLECTION')

        box.prop(scene, "rvretep_active_session_id", text="Active")

        col = get_recording_collection(scene.rvretep_active_session_id)
        if col:
            box.label(
                text=f"{col.name}  •  {display_name_for_collection(col)}",
                icon='REC',
            )

            sample_count = int(col.get("sample_count", 0) or 0)
            duration = float(col.get("duration_seconds", 0.0) or 0.0)
            eff_hz = float(col.get("effective_sample_hz", 0.0) or 0.0)

            if sample_count:
                box.label(
                    text=f"{sample_count:,} samples  •  {duration:.2f}s  •  {eff_hz:.1f} Hz"
                )

            if col.get("completed", False) is False and col.get("error"):
                err = str(col.get("error"))
                box.label(text="Session has a bake error — inspect metadata.", icon='ERROR')
                box.label(text=err[:90])

            row = box.row(align=True)
            row.operator(
                "rvretep.show_active_recording",
                text="Show Active",
                icon='HIDE_OFF',
            )
            row.operator(
                "rvretep.rename_recording",
                text="Rename",
                icon='GREASEPENCIL',
            )

            row = box.row(align=True)
            row.operator(
                "rvretep.hide_old_recordings",
                text="Hide Old",
                icon='HIDE_ON',
            )
            row.operator(
                "rvretep.show_all_recordings",
                text="Show All",
                icon='HIDE_OFF',
            )

            row = box.row()
            row.alert = True
            row.operator(
                "rvretep.delete_recording",
                text="Delete Active Session",
                icon='TRASH',
            )
        else:
            box.label(text="No VR sessions recorded yet.", icon='INFO')

        # ----------------------------------------------------------
        # POST-PROCESSING
        # ----------------------------------------------------------
        box = layout.box()
        box.label(text="Post-Processing", icon='MODIFIER')
        box.prop(scene, "rvretep_smoothing_passes", text="Passes")
        box.prop(scene, "rvretep_smoothing_window", text="Window")
        box.operator(
            "rvretep.apply_smoothing",
            text="Apply Optional Smoothing",
            icon='MOD_SMOOTH',
        )
        box.label(text="Raw capture is the default; smoothing changes baked values.", icon='INFO')

        # ----------------------------------------------------------
        # TOOLS
        # ----------------------------------------------------------
        layout.separator()
        row = layout.row(align=True)
        row.operator(
            "rvretep.validate_setup",
            text="Diagnostics",
            icon='INFO',
        )
        row.operator(
            "rvretep.select_active",
            text="Refresh",
            icon='FILE_REFRESH',
        )


# -----------------------------------------------------------------------------
# Persistent state hygiene
# -----------------------------------------------------------------------------


@persistent
def rvretep_load_post(_dummy):
    """Never restore an in-progress recorder or live XR timer from a .blend file."""
    global _RECORDER_ACTIVE, _RECORDER_TIMER, _LIVE_RIG_RUNTIME, _LIVE_RIG_GENERATION

    _LIVE_RIG_GENERATION += 1

    if _LIVE_RIG_RUNTIME is not None:
        try:
            stop_live_rig_runtime(bpy.context, _LIVE_RIG_RUNTIME)
        except Exception:
            pass
        _LIVE_RIG_RUNTIME = None

    for scene in bpy.data.scenes:
        try:
            scene.rvretep_is_recording = False
        except Exception:
            pass
    _RECORDER_ACTIVE = False
    _RECORDER_TIMER = None


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------


classes = (
    RVRETEP_OT_toggle_session,
    RVRETEP_OT_toggle_live_rig,
    RVRETEP_OT_live_rig_modal,
    RVRETEP_OT_select_live_rig,
    RVRETEP_OT_reset_tracking,
    RVRETEP_OT_validate_setup,
    RVRETEP_OT_audio_diagnostics,
    RVRETEP_OT_show_all_recordings,
    RVRETEP_OT_hide_old_recordings,
    RVRETEP_OT_show_active_recording,
    RVRETEP_OT_rename_recording,
    RVRETEP_OT_delete_recording,
    RVRETEP_OT_record_gameplay,
    RVRETEP_OT_apply_smoothing,
    RVRETEP_OT_select_active,
    RVRETEP_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.rvretep_is_recording = bpy.props.BoolProperty(
        name="VR Recording Active",
        description="Whether the RVretep recorder is currently capturing",
        default=False,
        options={'SKIP_SAVE'},
    )

    bpy.types.Scene.rvretep_active_session_id = bpy.props.EnumProperty(
        name="Active VR Session",
        description="VR session to manage or post-process",
        items=session_enum_items,
    )

    bpy.types.Scene.rvretep_session_name = bpy.props.StringProperty(
        name="Session Name",
        description="Human-readable name for the next VR recording",
        default="VR Capture",
        maxlen=96,
    )

    bpy.types.Scene.rvretep_poll_hz = bpy.props.FloatProperty(
        name="Capture Rate",
        description="Target Python polling frequency for VR pose capture",
        default=90.0,
        min=30.0,
        max=240.0,
        soft_min=30.0,
        soft_max=144.0,
        precision=0,
        step=10,
    )

    # Live VR Rig is runtime-only. Explicitly clear any stale value on
    # existing Scene instances when the extension is enabled/reloaded.
    for existing_scene in bpy.data.scenes:
        try:
            existing_scene.rvretep_live_rig_enabled = False
        except Exception:
            pass

    bpy.types.Scene.rvretep_live_rig_enabled = bpy.props.BoolProperty(
        name="Live VR Rig Enabled",
        description=(
            "Continuously drive the persistent VRLive_* empties from the active OpenXR session"
        ),
        default=False,
        options={'SKIP_SAVE'},
    )

    bpy.types.Scene.rvretep_live_rig_hz = bpy.props.FloatProperty(
        name="Live Rig Update Rate",
        description="Target update frequency for the standalone Live VR Rig",
        default=90.0,
        min=30.0,
        max=240.0,
        soft_min=30.0,
        soft_max=144.0,
        precision=0,
        step=10,
    )

    bpy.types.Scene.rvretep_live_rig_desktop_preview = bpy.props.BoolProperty(
        name="Desktop Viewport Preview",
        description=(
            "Redraw Blender 3D Viewports after Live VR Rig updates; useful for parenting/constraint debugging but adds UI overhead"
        ),
        default=False,
    )

    bpy.types.Scene.rvretep_force_audio = bpy.props.BoolProperty(
        name="Force Audio Sync",
        description="Enable scene audio and Audio Sync during recording, then restore the previous settings",
        default=True,
    )

    bpy.types.Scene.rvretep_hide_old_sessions = bpy.props.BoolProperty(
        name="Hide Old VR Sessions",
        description="Hide previous VRDATA sessions when a new session starts; disabled by default",
        default=False,
    )

    bpy.types.Scene.rvretep_create_markers = bpy.props.BoolProperty(
        name="Create Timeline Markers",
        description="Add start/end timeline markers for each completed VR recording",
        default=True,
    )

    bpy.types.Scene.rvretep_stop_when_playback_stops = bpy.props.BoolProperty(
        name="Stop if Playback Stops",
        description="Finalize the recording if Blender's animation playback stops unexpectedly",
        default=True,
    )

    bpy.types.Scene.rvretep_smoothing_passes = bpy.props.IntProperty(
        name="Smoothing Passes",
        description="Number of moving-average passes applied by optional smoothing",
        default=1,
        min=1,
        max=5,
    )

    bpy.types.Scene.rvretep_smoothing_window = bpy.props.IntProperty(
        name="Smoothing Window",
        description="Odd number of neighboring keys used by optional smoothing",
        default=5,
        min=3,
        max=15,
    )

    if rvretep_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(rvretep_load_post)


def unregister():
    global _RECORDER_ACTIVE, _RECORDER_TIMER, _LIVE_RIG_RUNTIME, _SESSION_ENUM_CACHE

    if _LIVE_RIG_RUNTIME is not None:
        try:
            stop_live_rig_runtime(bpy.context, _LIVE_RIG_RUNTIME)
        except Exception:
            pass
        _LIVE_RIG_RUNTIME = None

    # Stop the recording timer before classes/properties disappear. The modal
    # instance's in-memory raw samples cannot be safely serialized by Blender,
    # so disabling during capture is intentionally treated as a hard stop.
    if _RECORDER_TIMER is not None:
        try:
            bpy.context.window_manager.event_timer_remove(_RECORDER_TIMER)
        except Exception:
            pass
        _RECORDER_TIMER = None
    _RECORDER_ACTIVE = False

    if rvretep_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(rvretep_load_post)

    properties = (
        "rvretep_is_recording",
        "rvretep_active_session_id",
        "rvretep_session_name",
        "rvretep_poll_hz",
        "rvretep_live_rig_enabled",
        "rvretep_live_rig_hz",
        "rvretep_live_rig_desktop_preview",
        "rvretep_force_audio",
        "rvretep_hide_old_sessions",
        "rvretep_create_markers",
        "rvretep_stop_when_playback_stops",
        "rvretep_smoothing_passes",
        "rvretep_smoothing_window",
    )

    for prop_name in properties:
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    _SESSION_ENUM_CACHE = []


# retep was here. end of transmission.

if __name__ == "__main__":
    register()