# SPDX-FileCopyrightText: 2026 Flash-Craft
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

"""Experimental VR teleprompter for RVretep.

The feature is intentionally isolated from the recorder/live-rig runtime.
It renders a head-relative Blender scene panel and uses Blender's native
OpenXR action API for controller input.
"""

import re
import time
import textwrap
from typing import Iterable

import bpy
import mathutils
from bpy.app.handlers import persistent


VERSION = (1, 2, 0)

COLLECTION_NAME = "VR_TELEPROMPTER"
ROOT_NAME = "RVretep_Teleprompter_Root"
PANEL_NAME = "RVretep_Teleprompter_Panel"
STATUS_NAME = "RVretep_Teleprompter_Status"
LINE_PREFIX = "RVretep_Teleprompter_Line_"

ACTION_MAP_NAME = "rvretep_teleprompter"
ACTION_SCROLL = "rvretep_teleprompter_scroll"
ACTION_PAUSE = "rvretep_teleprompter_pause"
ACTION_NEXT = "rvretep_teleprompter_next"
ACTION_PREVIOUS = "rvretep_teleprompter_previous"
ACTION_GRIP = "rvretep_teleprompter_grip"
ACTION_AIM = "rvretep_teleprompter_aim"

RIGHT_HAND = "/user/hand/right"
LEFT_HAND = "/user/hand/left"

OCULUS_PROFILE = "/interaction_profiles/oculus/touch_controller"
KHRONOS_PROFILE = "/interaction_profiles/khr/simple_controller"

_RUNTIME = None
_GENERATION = 0


def is_xr_running(context) -> bool:
    try:
        state = context.window_manager.xr_session_state
        return bool(state and state.is_running(context))
    except Exception:
        return False


def get_xr_state(context):
    return context.window_manager.xr_session_state


def _safe_remove_object(obj):
    if obj is not None:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass


def _ensure_collection(scene):
    col = bpy.data.collections.get(COLLECTION_NAME)
    if col is None:
        col = bpy.data.collections.new(COLLECTION_NAME)
        scene.collection.children.link(col)
    elif col.name not in {child.name for child in scene.collection.children}:
        try:
            scene.collection.children.link(col)
        except Exception:
            pass
    col.hide_render = True
    col["rvretep_feature"] = "VR Teleprompter"
    return col


def _make_material(name, base_color, roughness=0.6, emission=None):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*base_color, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
        if emission is not None:
            if "Emission Color" in bsdf.inputs:
                bsdf.inputs["Emission Color"].default_value = (*emission, 1.0)
                bsdf.inputs["Emission Strength"].default_value = 0.25
            elif "Emission" in bsdf.inputs:
                bsdf.inputs["Emission"].default_value = (*emission, 1.0)
    return mat


def _make_panel(col):
    mesh = bpy.data.meshes.get(PANEL_NAME + "_Mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new(PANEL_NAME + "_Mesh")
        verts = [
            (-0.66, -0.38, -0.018), (0.66, -0.38, -0.018),
            (0.66, 0.38, -0.018), (-0.66, 0.38, -0.018),
            (-0.66, -0.38, 0.018), (0.66, -0.38, 0.018),
            (0.66, 0.38, 0.018), (-0.66, 0.38, 0.018),
        ]
        faces = [
            (0, 1, 2, 3), (4, 7, 6, 5),
            (0, 4, 5, 1), (1, 5, 6, 2),
            (2, 6, 7, 3), (4, 0, 3, 7),
        ]
        mesh.from_pydata(verts, [], faces)
        mesh.update()

    obj = bpy.data.objects.get(PANEL_NAME)
    if obj is None:
        obj = bpy.data.objects.new(PANEL_NAME, mesh)
        col.objects.link(obj)
    obj.data.materials.clear()
    obj.data.materials.append(
        _make_material("RVretep_Teleprompter_Panel_Mat", (0.012, 0.014, 0.018), 0.45)
    )
    obj.hide_render = True
    return obj


def _make_text_object(name, col, body="", size=0.033):
    obj = bpy.data.objects.get(name)
    if obj is None:
        curve = bpy.data.curves.new(name + "_Curve", type='FONT')
        obj = bpy.data.objects.new(name, curve)
        col.objects.link(obj)
    curve = obj.data
    curve.body = body
    curve.align_x = 'LEFT'
    curve.align_y = 'CENTER'
    curve.size = size
    curve.space_line = 0.92
    curve.resolution_u = 8
    curve.materials.clear()
    curve.materials.append(
        _make_material(
            "RVretep_Teleprompter_Text_Mat",
            (0.92, 0.96, 1.0),
            0.35,
            emission=(0.35, 0.42, 0.5),
        )
    )
    obj.hide_render = True
    return obj


def _ensure_scene_objects(context, line_slots):
    col = _ensure_collection(context.scene)
    root = bpy.data.objects.get(ROOT_NAME)
    if root is None:
        root = bpy.data.objects.new(ROOT_NAME, None)
        col.objects.link(root)
    root.empty_display_type = 'PLAIN_AXES'
    root.rotation_mode = 'QUATERNION'
    root.hide_render = True

    panel = _make_panel(col)
    panel.parent = root
    panel.location = (0.0, 0.0, 0.0)

    status = _make_text_object(STATUS_NAME, col, "AUTO 150 WPM  •  TRIGGER PAUSE  •  A NEXT  •  B PREVIOUS", 0.019)
    status.parent = root
    status.location = (-0.57, 0.315, 0.025)

    lines = []
    for index in range(line_slots):
        obj = _make_text_object(f"{LINE_PREFIX}{index:02d}", col)
        obj.parent = root
        obj.location = (-0.57, 0.0, 0.027)
        lines.append(obj)

    return root, panel, status, lines


def _clear_collection_runtime_objects():
    col = bpy.data.collections.get(COLLECTION_NAME)
    if col is None:
        return
    for obj in list(col.objects):
        _safe_remove_object(obj)
    try:
        bpy.data.collections.remove(col, do_unlink=True)
    except Exception:
        pass


def _format_script(text_body: str, width: int) -> tuple[list[str], list[int]]:
    lines: list[str] = []
    sections: list[int] = []

    source = text_body.replace("\r\n", "\n").replace("\r", "\n")
    for raw in source.split("\n"):
        raw = raw.rstrip()
        if not raw.strip():
            lines.append("")
            continue

        wrapped = textwrap.wrap(
            raw.strip(),
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        start = len(lines)
        lines.extend(wrapped)

        candidate = raw.strip()
        letters = [ch for ch in candidate if ch.isalpha()]
        is_heading = (
            bool(letters)
            and candidate.upper() == candidate
            and len(candidate) <= 48
            and not candidate.endswith((".", "?", "!", ",", ":"))
        )
        if is_heading:
            sections.append(start)

    return lines, sections


def _get_script_text(scene):
    text_block = scene.rvretep_teleprompter_text
    if text_block is not None:
        return text_block
    return None


def _active_map_name(state):
    try:
        maps = state.actionmaps
        index = int(state.active_actionmap)
        if 0 <= index < len(maps):
            return maps[index].name
    except Exception:
        pass
    return None


def _binding(item, name, profile, user_paths, component_paths):
    try:
        binding = item.bindings.new(name, True)
        binding.profile = profile
        for path in user_paths:
            binding.user_paths.new(path) if hasattr(binding, "user_paths") else None
        for path in component_paths:
            binding.component_paths.new(path)
        return binding
    except Exception:
        return None


def _add_action_item(actionmap, name, action_type, user_paths, bindings):
    try:
        item = actionmap.actionmap_items.get(name)
    except Exception:
        item = None

    if item is None:
        try:
            item = actionmap.actionmap_items.new(name, True)
        except Exception:
            item = None

    if item is None:
        return None

    try:
        item.type = action_type
    except Exception:
        pass

    try:
        for user_path in list(item.user_paths):
            item.user_paths.remove(user_path)
    except Exception:
        pass
    for user_path in user_paths:
        try:
            item.user_paths.new(user_path)
        except Exception:
            pass

    # Bindings are recreated so the feature is deterministic after reload.
    try:
        for old in list(item.bindings):
            item.bindings.remove(old)
    except Exception:
        pass

    for binding_name, profile, component_paths in bindings:
        try:
            binding = item.bindings.new(binding_name, True)
            binding.profile = profile
            for component_path in component_paths:
                binding.component_paths.new(component_path)
        except Exception:
            continue

    return item


def configure_xr_actions(context, activate=False) -> bool:
    """Create the teleprompter OpenXR action set and optionally activate it.

    The draft includes controller pose actions so activating this set does not
    discard RVretep's existing grip/aim pose queries.
    """
    state = get_xr_state(context)
    if state is None:
        return False

    maps = state.actionmaps
    actionmap = None

    try:
        for candidate in maps:
            if candidate.name == ACTION_MAP_NAME:
                actionmap = candidate
                break
    except Exception:
        pass

    if actionmap is None:
        try:
            base_index = int(state.active_actionmap)
            base_map = maps[base_index] if 0 <= base_index < len(maps) else None
        except Exception:
            base_map = None

        # Prefer cloning Blender's current map so native VR navigation actions
        # remain available in the teleprompter action set.
        if base_map is not None:
            try:
                actionmap = maps.new_from_actionmap(state, base_map)
                actionmap.name = ACTION_MAP_NAME
            except Exception:
                actionmap = None

        if actionmap is None:
            try:
                actionmap = maps.new(state, ACTION_MAP_NAME, True)
            except Exception:
                return False

    # The Python action API is present in Blender 4.2+; failures are contained
    # so an unsupported runtime leaves the teleprompter usable with UI controls.
    try:
        state.action_set_create(context, actionmap)
    except Exception:
        pass

    both = [LEFT_HAND, RIGHT_HAND]
    right = [RIGHT_HAND]

    _add_action_item(
        actionmap,
        ACTION_SCROLL,
        'VECTOR2D',
        both,
        [
            ("Touch Thumbstick", OCULUS_PROFILE, ["/input/thumbstick"]),
            ("Simple Controller", KHRONOS_PROFILE, ["/input/thumbstick"]),
        ],
    )
    _add_action_item(
        actionmap,
        ACTION_PAUSE,
        'FLOAT',
        right,
        [
            ("Right Trigger", OCULUS_PROFILE, ["/input/trigger/value"]),
        ],
    )
    _add_action_item(
        actionmap,
        ACTION_NEXT,
        'FLOAT',
        right,
        [
            ("Right Primary", OCULUS_PROFILE, ["/input/a/click"]),
            ("Right Primary Generic", KHRONOS_PROFILE, ["/input/select/click"]),
        ],
    )
    _add_action_item(
        actionmap,
        ACTION_PREVIOUS,
        'FLOAT',
        right,
        [
            ("Right Secondary", OCULUS_PROFILE, ["/input/b/click"]),
            ("Right Secondary Generic", KHRONOS_PROFILE, ["/input/menu/click"]),
        ],
    )

    _add_action_item(
        actionmap,
        ACTION_GRIP,
        'POSE',
        both,
        [
            ("Controller Grip", OCULUS_PROFILE, ["/input/grip/pose"]),
            ("Controller Grip Generic", KHRONOS_PROFILE, ["/input/grip/pose"]),
        ],
    )
    _add_action_item(
        actionmap,
        ACTION_AIM,
        'POSE',
        both,
        [
            ("Controller Aim", OCULUS_PROFILE, ["/input/aim/pose"]),
            ("Controller Aim Generic", KHRONOS_PROFILE, ["/input/aim/pose"]),
        ],
    )

    for item in list(actionmap.actionmap_items):
        try:
            state.action_create(context, actionmap, item)
        except Exception:
            pass

    for item in list(actionmap.actionmap_items):
        try:
            for binding in item.bindings:
                state.action_binding_create(context, actionmap, item, binding)
        except Exception:
            pass

    if activate:
        try:
            state.controller_pose_actions_set(
                context, ACTION_MAP_NAME, ACTION_GRIP, ACTION_AIM
            )
        except Exception:
            pass
        try:
            return bool(state.active_action_set_set(context, ACTION_MAP_NAME))
        except Exception:
            return False

    return True


class _TeleprompterRuntime:
    __slots__ = (
        "generation", "timer", "root", "panel", "status", "lines",
        "text_block_name", "source_lines", "sections", "scroll_line",
        "base_wpm", "manual_step", "paused", "last_time", "last_start",
        "previous_pause", "previous_next", "previous_previous",
        "previous_scroll_y", "saved_action_set", "line_word_count",
        "finished_notice",
    )

    def __init__(self, generation, root, panel, status, lines, text_block):
        self.generation = generation
        self.timer = None
        self.root = root
        self.panel = panel
        self.status = status
        self.lines = lines
        self.text_block_name = text_block.name if text_block else ""
        self.source_lines: list[str] = []
        self.sections: list[int] = []
        self.scroll_line = 0.0
        self.base_wpm = 150.0
        self.manual_step = 30.0
        self.paused = False
        self.last_time = time.perf_counter()
        self.last_start = -1
        self.previous_pause = False
        self.previous_next = False
        self.previous_previous = False
        self.previous_scroll_y = 0.0
        self.saved_action_set = None
        self.line_word_count = 3.0
        self.finished_notice = False

    def start(self, context):
        hz = max(20.0, float(context.scene.rvretep_teleprompter_update_hz))
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

    def load_script(self, scene):
        text_block = _get_script_text(scene)
        if text_block is None:
            raise RuntimeError("Select a Blender Text datablock for the teleprompter.")

        body = text_block.as_string()
        lines, sections = _format_script(
            body,
            max(24, int(scene.rvretep_teleprompter_wrap_width)),
        )
        if not lines:
            raise RuntimeError("The selected Text datablock is empty.")

        self.source_lines = lines
        self.sections = sections

        nonblank_words = [
            len(line.split()) for line in lines if line.strip()
        ]
        self.line_word_count = max(
            2.0,
            (sum(nonblank_words) / len(nonblank_words)) if nonblank_words else 3.0,
        )
        self.text_block_name = text_block.name

    def _max_scroll(self):
        visible = max(1, len(self.lines))
        return max(0.0, float(len(self.source_lines) - visible))

    def set_scroll(self, value):
        self.scroll_line = max(0.0, min(self._max_scroll(), float(value)))

    def nearest_section(self, direction):
        if not self.sections:
            return None

        current = int(self.scroll_line + 0.25)
        if direction > 0:
            for section in self.sections:
                if section > current:
                    return section
            return self.sections[-1]

        previous = [section for section in self.sections if section < current - 1]
        return previous[-1] if previous else self.sections[0]

    def update_action_state(self, context, now):
        state = get_xr_state(context)
        if state is None:
            return (0.0, 0.0, False, False, False)

        def read(name, user_path):
            try:
                value = state.action_state_get(
                    context, ACTION_MAP_NAME, name, user_path
                )
                return tuple(float(v) for v in value)
            except Exception:
                return (0.0, 0.0)

        scroll = read(ACTION_SCROLL, RIGHT_HAND)
        pause = read(ACTION_PAUSE, RIGHT_HAND)[0] > 0.5
        next_pressed = read(ACTION_NEXT, RIGHT_HAND)[0] > 0.5
        prev_pressed = read(ACTION_PREVIOUS, RIGHT_HAND)[0] > 0.5

        if pause and not self.previous_pause:
            self.paused = not self.paused

        if next_pressed and not self.previous_next:
            target = self.nearest_section(+1)
            if target is not None:
                self.set_scroll(float(target))
                self.paused = False

        if prev_pressed and not self.previous_previous:
            target = self.nearest_section(-1)
            if target is not None:
                self.set_scroll(float(target))
                self.paused = False

        self.previous_pause = pause
        self.previous_next = next_pressed
        self.previous_previous = prev_pressed
        return (scroll[0], scroll[1], pause, next_pressed, prev_pressed)

    def update_pose(self, context):
        state = get_xr_state(context)
        viewer_location = mathutils.Vector(state.viewer_pose_location)
        viewer_rotation = mathutils.Quaternion(state.viewer_pose_rotation)

        forward = viewer_rotation @ mathutils.Vector((0.0, 0.0, -1.0))
        world_up = mathutils.Vector((0.0, 0.0, 1.0))
        panel_location = (
            viewer_location
            + forward.normalized() * float(context.scene.rvretep_teleprompter_distance)
            + world_up * float(context.scene.rvretep_teleprompter_vertical_offset)
        )

        self.root.location = panel_location
        to_viewer = viewer_location - panel_location
        if to_viewer.length > 1e-6:
            self.root.rotation_quaternion = to_viewer.normalized().to_track_quat('Z', 'Y')

    def redraw_text(self, scene):
        visible_count = len(self.lines)
        start = int(self.scroll_line)
        fraction = self.scroll_line - start
        line_height = 0.043

        for slot, obj in enumerate(self.lines):
            index = start + slot
            if index < len(self.source_lines):
                obj.hide_viewport = False
                obj.data.body = self.source_lines[index]
                obj.location.y = (
                    ((visible_count - 1) * 0.5 - slot) * line_height
                    + fraction * line_height
                    - 0.005
                )
            else:
                obj.hide_viewport = True
                obj.data.body = ""

        mode = "PAUSED" if self.paused else "AUTO"
        wpm = self.base_wpm
        if self.previous_scroll_y and abs(self.previous_scroll_y) > 0.15:
            wpm = max(40.0, self.base_wpm + (-self.previous_scroll_y * self.manual_step))

        self.status.data.body = (
            f"{mode}  {wpm:.0f} WPM   •   TRIGGER PAUSE   •   A NEXT   •   B PREVIOUS"
        )

        at_end = self.scroll_line >= self._max_scroll() - 0.01
        if at_end and not self.finished_notice:
            self.status.data.body = (
                f"END OF SCRIPT   •   TRIGGER PAUSE   •   A NEXT   •   B PREVIOUS"
            )
            self.finished_notice = True
        elif not at_end:
            self.finished_notice = False

        self.last_start = start

    def tick(self, context):
        if not is_xr_running(context):
            return False

        state = get_xr_state(context)
        if state is None:
            return False

        now = time.perf_counter()
        dt = max(0.0, min(0.1, now - self.last_time))
        self.last_time = now

        stick_x, stick_y, _, _, _ = self.update_action_state(context, now)
        self.previous_scroll_y = stick_y

        # Automatic WPM scroll. Thumbstick movement temporarily overrides
        # the automatic rate without permanently changing the user's WPM.
        if abs(stick_y) > 0.12:
            override_wpm = max(
                40.0,
                self.base_wpm + (-stick_y * self.manual_step),
            )
            lines_per_second = override_wpm / (60.0 * self.line_word_count)
            self.set_scroll(self.scroll_line + lines_per_second * dt)
        elif not self.paused:
            lines_per_second = self.base_wpm / (60.0 * self.line_word_count)
            self.set_scroll(self.scroll_line + lines_per_second * dt)

        self.update_pose(context)

        if int(self.scroll_line) != self.last_start or self.last_start < 0:
            self.redraw_text(context.scene)
        else:
            # Fractional motion still moves each already-visible line.
            visible_count = len(self.lines)
            line_height = 0.043
            fraction = self.scroll_line - int(self.scroll_line)
            for slot, obj in enumerate(self.lines):
                if not obj.hide_viewport:
                    obj.location.y = (
                        ((visible_count - 1) * 0.5 - slot) * line_height
                        + fraction * line_height
                        - 0.005
                    )
            mode = "PAUSED" if self.paused else "AUTO"
            wpm = (
                max(40.0, self.base_wpm + (-stick_y * self.manual_step))
                if abs(stick_y) > 0.12
                else self.base_wpm
            )
            self.status.data.body = (
                f"{mode}  {wpm:.0f} WPM   •   TRIGGER PAUSE   •   A NEXT   •   B PREVIOUS"
            )

        return True


def _stop_runtime(context, runtime=None, delete_scene_objects=True):
    global _RUNTIME

    runtime = runtime or _RUNTIME
    if runtime is None:
        return

    runtime.stop(context)

    if runtime.saved_action_set and is_xr_running(context):
        try:
            get_xr_state(context).active_action_set_set(
                context, runtime.saved_action_set
            )
        except Exception:
            pass

    if delete_scene_objects:
        _clear_collection_runtime_objects()

    if _RUNTIME is runtime:
        _RUNTIME = None


class RVRETEP_OT_teleprompter_create_script(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_create_script"
    bl_label = "Create Teleprompter Script"
    bl_description = "Create a new Blender Text datablock for the RVretep teleprompter"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        name = "RVretep_Teleprompter_Script"
        text_block = bpy.data.texts.new(name)
        text_block.write(
            "# RVretep Teleprompter\n"
            "# Paste or type your presenter script below.\n\n"
            "INTRO\n\n"
            "Start writing here.\n"
        )
        context.scene.rvretep_teleprompter_text = text_block
        self.report({'INFO'}, f"Teleprompter script ready: {text_block.name}.")
        return {'FINISHED'}


class RVRETEP_OT_teleprompter_refresh(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_refresh"
    bl_label = "Refresh Script"
    bl_description = "Reload the selected Blender Text datablock into the teleprompter"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _RUNTIME
        if _RUNTIME is None:
            self.report({'INFO'}, "Teleprompter is not running; the next start will read the current text.")
            return {'FINISHED'}
        try:
            _RUNTIME.load_script(context.scene)
            _RUNTIME.redraw_text(context.scene)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Teleprompter script refreshed.")
        return {'FINISHED'}


class RVRETEP_OT_teleprompter_toggle(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_toggle"
    bl_label = "Start VR Teleprompter"
    bl_description = "Start or stop the head-relative VR teleprompter"

    def execute(self, context):
        global _RUNTIME, _GENERATION

        if _RUNTIME is not None:
            context.scene.rvretep_teleprompter_enabled = False
            _stop_runtime(context, _RUNTIME, True)
            self.report({'INFO'}, "VR Teleprompter stopped.")
            return {'FINISHED'}

        if not is_xr_running(context):
            self.report({'ERROR'}, "OpenXR is not running. Start the VR Session first.")
            return {'CANCELLED'}

        text_block = _get_script_text(context.scene)
        if text_block is None:
            self.report({'ERROR'}, "Select a Blender Text datablock for the teleprompter.")
            return {'CANCELLED'}

        try:
            root, panel, status, lines = _ensure_scene_objects(
                context,
                max(6, int(context.scene.rvretep_teleprompter_visible_lines)),
            )
            _GENERATION += 1
            runtime = _TeleprompterRuntime(
                _GENERATION, root, panel, status, lines, text_block
            )
            runtime.base_wpm = float(context.scene.rvretep_teleprompter_wpm)
            runtime.manual_step = float(context.scene.rvretep_teleprompter_manual_step)
            runtime.load_script(context.scene)
            runtime.saved_action_set = _active_map_name(get_xr_state(context))

            configured = configure_xr_actions(context, activate=True)
            if not configured:
                self.report(
                    {'WARNING'},
                    "Teleprompter UI started, but native XR actions could not be activated.",
                )

            runtime.start(context)
            _RUNTIME = runtime
            context.scene.rvretep_teleprompter_enabled = True
            runtime.update_pose(context)
            try:
                result = bpy.ops.rvretep.teleprompter_modal('INVOKE_DEFAULT')
                if 'RUNNING_MODAL' not in result:
                    raise RuntimeError("Blender did not start the teleprompter modal handler.")
            except Exception:
                context.scene.rvretep_teleprompter_enabled = False
                _stop_runtime(context, runtime, True)
                raise
            runtime.redraw_text(context.scene)

            self.report(
                {'INFO'},
                f"VR Teleprompter started — {runtime.base_wpm:.0f} WPM.",
            )
            return {'FINISHED'}
        except Exception as exc:
            _clear_collection_runtime_objects()
            self.report({'ERROR'}, f"Could not start teleprompter: {exc}")
            return {'CANCELLED'}


class RVRETEP_OT_teleprompter_modal(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_modal"
    bl_label = "VR Teleprompter Runtime"
    bl_options = {'INTERNAL'}

    def invoke(self, context, _event):
        runtime = _RUNTIME
        if runtime is None:
            return {'CANCELLED'}
        self._runtime = runtime
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        runtime = getattr(self, "_runtime", None)
        if runtime is None or runtime is not _RUNTIME:
            return {'FINISHED'}

        if event.type == 'TIMER' and event.timer == runtime.timer:
            try:
                if not runtime.tick(context):
                    context.scene.rvretep_teleprompter_enabled = False
                    _stop_runtime(context, runtime, True)
                    return {'FINISHED'}
            except Exception as exc:
                context.scene.rvretep_teleprompter_enabled = False
                _stop_runtime(context, runtime, True)
                self.report({'ERROR'}, f"Teleprompter stopped: {exc}")
                return {'FINISHED'}

        return {'PASS_THROUGH'}


class RVRETEP_OT_teleprompter_pause(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_pause"
    bl_label = "Pause / Resume"
    bl_description = "Pause or resume teleprompter scrolling"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _RUNTIME is None:
            self.report({'WARNING'}, "Teleprompter is not running.")
            return {'CANCELLED'}
        _RUNTIME.paused = not _RUNTIME.paused
        self.report({'INFO'}, "Teleprompter paused." if _RUNTIME.paused else "Teleprompter resumed.")
        return {'FINISHED'}


class RVRETEP_OT_teleprompter_next(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_next"
    bl_label = "Next Section"
    bl_description = "Jump to the next detected section"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _RUNTIME is None:
            return {'CANCELLED'}
        target = _RUNTIME.nearest_section(+1)
        if target is not None:
            _RUNTIME.set_scroll(target)
            _RUNTIME.redraw_text(context.scene)
        return {'FINISHED'}


class RVRETEP_OT_teleprompter_previous(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_previous"
    bl_label = "Previous Section"
    bl_description = "Jump to the previous detected section"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _RUNTIME is None:
            return {'CANCELLED'}
        target = _RUNTIME.nearest_section(-1)
        if target is not None:
            _RUNTIME.set_scroll(target)
            _RUNTIME.redraw_text(context.scene)
        return {'FINISHED'}


class RVRETEP_OT_teleprompter_reset(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_reset"
    bl_label = "Reset to Start"
    bl_description = "Return the teleprompter to the start of the script"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _RUNTIME is None:
            return {'CANCELLED'}
        _RUNTIME.set_scroll(0.0)
        _RUNTIME.paused = False
        _RUNTIME.finished_notice = False
        _RUNTIME.redraw_text(context.scene)
        return {'FINISHED'}


class RVRETEP_OT_teleprompter_diagnostics(bpy.types.Operator):
    bl_idname = "rvretep.teleprompter_diagnostics"
    bl_label = "Teleprompter Diagnostics"
    bl_description = "Report teleprompter and native XR action setup status"

    def execute(self, context):
        text_block = _get_script_text(context.scene)
        lines = [
            f"OpenXR running: {'YES' if is_xr_running(context) else 'NO'}",
            f"Script: {text_block.name if text_block else 'NONE'}",
            f"Teleprompter runtime: {'ACTIVE' if _RUNTIME else 'OFF'}",
            f"Native XR action set: {ACTION_MAP_NAME}",
        ]
        if text_block:
            line_count = text_block.as_string().count("\n") + 1
            lines.append(f"Script source lines: {line_count}")
        for line in lines:
            print(f"[RVretep] {line}")
        self.report({'INFO'}, f"Teleprompter diagnostics printed — {'; '.join(lines[:3])}")
        return {'FINISHED'}


class RVRETEP_PT_teleprompter:
    @staticmethod
    def draw(layout, context):
        scene = context.scene
        box = layout.box()
        box.label(text="VR Teleprompter (Experimental)", icon='TEXT')

        box.prop(scene, "rvretep_teleprompter_text", text="Script")

        if scene.rvretep_teleprompter_enabled:
            row = box.row()
            row.alert = True
            row.scale_y = 1.2
            row.operator(
                "rvretep.teleprompter_toggle",
                text="STOP TELEPROMPTER",
                icon='PAUSE',
            )
        else:
            row = box.row()
            row.enabled = is_xr_running(context)
            row.scale_y = 1.2
            row.operator(
                "rvretep.teleprompter_toggle",
                text="START TELEPROMPTER",
                icon='TEXT',
            )

        row = box.row(align=True)
        row.operator("rvretep.teleprompter_create_script", text="New Script", icon='ADD')
        row.operator("rvretep.teleprompter_refresh", text="Refresh", icon='FILE_REFRESH')

        box.prop(scene, "rvretep_teleprompter_wpm", text="Base WPM")
        box.prop(scene, "rvretep_teleprompter_manual_step", text="Stick WPM Step")
        box.prop(scene, "rvretep_teleprompter_visible_lines", text="Visible Lines")
        box.prop(scene, "rvretep_teleprompter_distance", text="Distance")
        box.prop(scene, "rvretep_teleprompter_vertical_offset", text="Vertical Offset")
        box.prop(scene, "rvretep_teleprompter_update_hz", text="Update Rate")

        row = box.row(align=True)
        row.operator("rvretep.teleprompter_pause", text="Pause / Resume", icon='PAUSE')
        row.operator("rvretep.teleprompter_reset", text="Reset", icon='LOOP_BACK')

        row = box.row(align=True)
        row.operator("rvretep.teleprompter_previous", text="Previous", icon='TRIA_LEFT')
        row.operator("rvretep.teleprompter_next", text="Next", icon='TRIA_RIGHT')

        box.prop(scene, "rvretep_teleprompter_wrap_width", text="Wrap Width")
        box.operator(
            "rvretep.teleprompter_diagnostics",
            text="Teleprompter Diagnostics",
            icon='INFO',
        )

        box.label(text="Right stick: temporary speed override", icon='INFO')
        box.label(text="Trigger: pause  •  A: next  •  B: previous")


@persistent
def rvretep_xr_session_start_pre():
    # Register the action map before a new native XR session starts. Blender
    # documents xr_session_start_pre specifically for default XR action maps.
    try:
        if hasattr(bpy.app.handlers, "xr_session_start_pre"):
            configure_xr_actions(bpy.context, activate=False)
    except Exception:
        pass


@persistent
def rvretep_load_post(_dummy):
    global _RUNTIME, _GENERATION
    _GENERATION += 1
    if _RUNTIME is not None:
        try:
            _stop_runtime(bpy.context, _RUNTIME, True)
        except Exception:
            pass
    _RUNTIME = None

    for scene in bpy.data.scenes:
        try:
            scene.rvretep_teleprompter_enabled = False
        except Exception:
            pass


_CLASSES = (
    RVRETEP_OT_teleprompter_create_script,
    RVRETEP_OT_teleprompter_refresh,
    RVRETEP_OT_teleprompter_toggle,
    RVRETEP_OT_teleprompter_modal,
    RVRETEP_OT_teleprompter_pause,
    RVRETEP_OT_teleprompter_next,
    RVRETEP_OT_teleprompter_previous,
    RVRETEP_OT_teleprompter_reset,
    RVRETEP_OT_teleprompter_diagnostics,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.rvretep_teleprompter_text = bpy.props.PointerProperty(
        name="Teleprompter Script",
        type=bpy.types.Text,
        description="Blender Text datablock used by the VR teleprompter",
    )
    bpy.types.Scene.rvretep_teleprompter_wpm = bpy.props.FloatProperty(
        name="Base WPM",
        description="Automatic teleprompter reading speed",
        default=150.0,
        min=40.0,
        max=300.0,
        precision=0,
    )
    bpy.types.Scene.rvretep_teleprompter_manual_step = bpy.props.FloatProperty(
        name="Stick WPM Step",
        description="Maximum temporary speed adjustment from thumbstick input",
        default=30.0,
        min=5.0,
        max=100.0,
        precision=0,
    )
    bpy.types.Scene.rvretep_teleprompter_visible_lines = bpy.props.IntProperty(
        name="Visible Lines",
        description="Number of script lines visible in the VR panel",
        default=14,
        min=6,
        max=24,
    )
    bpy.types.Scene.rvretep_teleprompter_distance = bpy.props.FloatProperty(
        name="Distance",
        description="Distance of the teleprompter panel from the headset",
        default=1.15,
        min=0.5,
        max=3.0,
        soft_min=0.7,
        soft_max=2.0,
        precision=2,
    )
    bpy.types.Scene.rvretep_teleprompter_vertical_offset = bpy.props.FloatProperty(
        name="Vertical Offset",
        description="Vertical offset of the panel relative to the headset",
        default=-0.08,
        min=-0.8,
        max=0.5,
        precision=2,
    )
    bpy.types.Scene.rvretep_teleprompter_update_hz = bpy.props.FloatProperty(
        name="Update Rate",
        description="Runtime update frequency for the VR teleprompter",
        default=60.0,
        min=20.0,
        max=120.0,
        precision=0,
    )
    bpy.types.Scene.rvretep_teleprompter_wrap_width = bpy.props.IntProperty(
        name="Wrap Width",
        description="Approximate characters per teleprompter line",
        default=42,
        min=24,
        max=80,
    )
    bpy.types.Scene.rvretep_teleprompter_enabled = bpy.props.BoolProperty(
        name="Teleprompter Enabled",
        description="Whether the RVretep VR teleprompter runtime is active",
        default=False,
        options={'SKIP_SAVE'},
    )

    if rvretep_xr_session_start_pre not in bpy.app.handlers.xr_session_start_pre:
        bpy.app.handlers.xr_session_start_pre.append(rvretep_xr_session_start_pre)
    if rvretep_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(rvretep_load_post)


def unregister():
    global _RUNTIME
    if _RUNTIME is not None:
        try:
            _stop_runtime(bpy.context, _RUNTIME, True)
        except Exception:
            pass
        _RUNTIME = None

    for handler_list, handler in (
        (bpy.app.handlers.xr_session_start_pre, rvretep_xr_session_start_pre),
        (bpy.app.handlers.load_post, rvretep_load_post),
    ):
        if handler in handler_list:
            handler_list.remove(handler)

    for prop_name in (
        "rvretep_teleprompter_text",
        "rvretep_teleprompter_wpm",
        "rvretep_teleprompter_manual_step",
        "rvretep_teleprompter_visible_lines",
        "rvretep_teleprompter_distance",
        "rvretep_teleprompter_vertical_offset",
        "rvretep_teleprompter_update_hz",
        "rvretep_teleprompter_wrap_width",
        "rvretep_teleprompter_enabled",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)

    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)

    _clear_collection_runtime_objects()
