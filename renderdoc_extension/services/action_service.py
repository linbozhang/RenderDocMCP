"""
Draw call / action operations service for RenderDoc.
"""

import csv
import os
import re

import renderdoc as rd

from ..utils import Serializers, Helpers


class ActionService:
    """Draw call / action operations service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_draw_calls(
        self,
        include_children=True,
        marker_filter=None,
        exclude_markers=None,
        event_id_min=None,
        event_id_max=None,
        only_actions=False,
        flags_filter=None,
    ):
        """
        Get all draw calls/actions in the capture with optional filtering.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"actions": []}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            result["actions"] = Serializers.serialize_actions(
                root_actions,
                structured_file,
                include_children,
                marker_filter=marker_filter,
                exclude_markers=exclude_markers,
                event_id_min=event_id_min,
                event_id_max=event_id_max,
                only_actions=only_actions,
                flags_filter=flags_filter,
            )

        self._invoke(callback)
        return result

    def get_frame_summary(self):
        """
        Get a summary of the current capture frame.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"summary": None}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            api = controller.GetAPIProperties().pipelineType

            # Statistics counters
            stats = {
                "draw_calls": 0,
                "dispatches": 0,
                "clears": 0,
                "copies": 0,
                "presents": 0,
                "markers": 0,
            }
            total_actions = [0]

            def count_actions(actions):
                for action in actions:
                    total_actions[0] += 1
                    flags = action.flags

                    if flags & rd.ActionFlags.Drawcall:
                        stats["draw_calls"] += 1
                    if flags & rd.ActionFlags.Dispatch:
                        stats["dispatches"] += 1
                    if flags & rd.ActionFlags.Clear:
                        stats["clears"] += 1
                    if flags & rd.ActionFlags.Copy:
                        stats["copies"] += 1
                    if flags & rd.ActionFlags.Present:
                        stats["presents"] += 1
                    if flags & (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker):
                        stats["markers"] += 1

                    if action.children:
                        count_actions(action.children)

            count_actions(root_actions)

            # Top-level markers
            top_markers = []
            for action in root_actions:
                if action.flags & rd.ActionFlags.PushMarker:
                    child_count = Helpers.count_children(action)
                    top_markers.append({
                        "name": action.GetName(structured_file),
                        "event_id": action.eventId,
                        "child_count": child_count,
                    })

            # Resource counts
            textures = controller.GetTextures()
            buffers = controller.GetBuffers()

            result["summary"] = {
                "api": str(api),
                "total_actions": total_actions[0],
                "statistics": stats,
                "top_level_markers": top_markers,
                "resource_counts": {
                    "textures": len(textures),
                    "buffers": len(buffers),
                },
            }

        self._invoke(callback)
        return result["summary"]

    def get_draw_call_details(self, event_id):
        """Get detailed information about a specific draw call"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"details": None, "error": None}

        def callback(controller):
            # Move to the event
            controller.SetFrameEvent(event_id, True)

            action = self.ctx.GetAction(event_id)
            if not action:
                result["error"] = "No action at event %d" % event_id
                return

            structured_file = controller.GetStructuredFile()

            details = {
                "event_id": action.eventId,
                "action_id": action.actionId,
                "name": action.GetName(structured_file),
                "flags": Serializers.serialize_flags(action.flags),
                "num_indices": action.numIndices,
                "num_instances": action.numInstances,
                "base_vertex": action.baseVertex,
                "vertex_offset": action.vertexOffset,
                "instance_offset": action.instanceOffset,
                "index_offset": action.indexOffset,
            }

            # Output resources
            outputs = []
            for i, output in enumerate(action.outputs):
                if output != rd.ResourceId.Null():
                    outputs.append({"index": i, "resource_id": str(output)})
            details["outputs"] = outputs

            if action.depthOut != rd.ResourceId.Null():
                details["depth_output"] = str(action.depthOut)

            result["details"] = details

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["details"]

    def get_action_timings(
        self,
        event_ids=None,
        marker_filter=None,
        exclude_markers=None,
    ):
        """
        Get GPU timing information for actions.

        Args:
            event_ids: Optional list of specific event IDs to get timings for.
                      If None, returns timings for all actions.
            marker_filter: Only include actions under markers containing this string.
            exclude_markers: Exclude actions under markers containing these strings.

        Returns:
            Dictionary with:
            - available: Whether GPU timing counters are supported
            - unit: Time unit (typically "seconds")
            - timings: List of {event_id, name, duration_seconds, duration_ms}
            - total_duration_ms: Sum of all durations
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            # Check if EventGPUDuration counter is available
            counters = controller.EnumerateCounters()
            if rd.GPUCounter.EventGPUDuration not in counters:
                result["data"] = {
                    "available": False,
                    "error": "GPU timing counters not supported on this capture",
                }
                return

            # Get counter description
            counter_desc = controller.DescribeCounter(rd.GPUCounter.EventGPUDuration)

            # Fetch timing data
            counter_results = controller.FetchCounters([rd.GPUCounter.EventGPUDuration])

            # Build event_id to timing map
            timing_map = {}
            target_counter = int(rd.GPUCounter.EventGPUDuration)
            for r in counter_results:
                if r.counter == target_counter:
                    # EventGPUDuration typically returns double
                    # Try to get the value in the most appropriate way
                    val = r.value.d  # double is the standard for duration
                    timing_map[r.eventId] = val

            # Get structured file for action names
            structured_file = controller.GetStructuredFile()
            root_actions = controller.GetRootActions()

            # Collect actions to report timings for
            timings = []
            total_duration = [0.0]

            def collect_timings(actions, parent_markers=None):
                if parent_markers is None:
                    parent_markers = []

                for action in actions:
                    action_name = action.GetName(structured_file)
                    current_markers = parent_markers[:]

                    # Track marker hierarchy
                    is_marker = bool(action.flags & (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker))
                    if is_marker:
                        current_markers.append(action_name)

                    # Apply marker filter
                    if marker_filter:
                        marker_path = "/".join(current_markers)
                        if marker_filter.lower() not in marker_path.lower():
                            # Still recurse into children
                            if action.children:
                                collect_timings(action.children, current_markers)
                            continue

                    # Apply exclude filter
                    if exclude_markers:
                        skip = False
                        for exclude in exclude_markers:
                            for m in current_markers:
                                if exclude.lower() in m.lower():
                                    skip = True
                                    break
                            if skip:
                                break
                        if skip:
                            if action.children:
                                collect_timings(action.children, current_markers)
                            continue

                    # Check if we should include this event
                    event_id = action.eventId
                    include = True
                    if event_ids is not None:
                        include = event_id in event_ids

                    if include and event_id in timing_map:
                        duration_sec = timing_map[event_id]
                        duration_ms = duration_sec * 1000.0
                        timings.append({
                            "event_id": event_id,
                            "name": action_name,
                            "duration_seconds": duration_sec,
                            "duration_ms": duration_ms,
                        })
                        total_duration[0] += duration_ms

                    # Recurse into children
                    if action.children:
                        collect_timings(action.children, current_markers)

            collect_timings(root_actions)

            # Sort by event_id
            timings.sort(key=lambda x: x["event_id"])

            result["data"] = {
                "available": True,
                "unit": str(counter_desc.unit),
                "timings": timings,
                "total_duration_ms": total_duration[0],
                "count": len(timings),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    @staticmethod
    def _shader_resource_name(ctx, pipe, stage):
        shader = pipe.GetShader(stage)
        if shader == rd.ResourceId.Null():
            return ""

        try:
            name = ctx.GetResourceName(shader)
            if name:
                return name
        except Exception:
            pass

        try:
            entry = pipe.GetShaderEntryPoint(stage)
            if entry:
                return entry
        except Exception:
            pass

        return str(shader)

    @staticmethod
    def _parse_pass_name(shader_name):
        if not shader_name:
            return ""

        match = re.search(r"\(([^)]+)\)", shader_name)
        if match:
            return match.group(1)

        base = shader_name.split("[", 1)[0]
        if "/" in base:
            return base.rsplit("/", 1)[-1]
        return base

    @staticmethod
    def _parse_keywords(shader_name):
        if not shader_name:
            return ""

        match = re.search(r"\[(.*?)\]", shader_name)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _short_shader_name(shader_name):
        if not shader_name:
            return ""

        base = shader_name.split("[", 1)[0]
        if "(" in base:
            base = base.split("(", 1)[0]
        if "/" in base:
            return base.rstrip("/").rsplit("/", 1)[-1]
        return base

    @staticmethod
    def _action_type_name(flags):
        if flags & rd.ActionFlags.Dispatch:
            return "Dispatch"
        if flags & rd.ActionFlags.Drawcall:
            if flags & rd.ActionFlags.Indexed:
                return "DrawIndexed"
            return "Draw"
        return "Unknown"

    @staticmethod
    def _extract_pass_marker(marker_path):
        """Extract render-pass marker (e.g. GpuBoidUpdatePass) from marker path."""
        if not marker_path:
            return ""

        parts = [part.strip() for part in marker_path.split(" / ") if part.strip()]
        if not parts:
            return ""

        scene_render = "SceneCamera.Render"
        if scene_render in parts:
            idx = parts.index(scene_render)
            if idx + 1 < len(parts):
                return parts[idx + 1]

        for part in parts:
            if "Pass" in part:
                return part

        if parts[0] == "UIR.DrawChain":
            return "UIR.DrawChain"

        draw_loops = {
            "RenderLoop.Draw",
            "RenderLoopNewBatcher.Draw",
            "ShadowLoopNewBatcher.Draw",
            "Shadows.Draw",
            "GUITexture.Draw",
        }
        for part in reversed(parts):
            if part in draw_loops:
                continue
            if part in ("UIR.DrawChain", "UIR.ImmediateRenderer"):
                continue
            return part

        return ""

    def export_drawcall_analysis(self, output_dir=None):
        """Export all draw calls/dispatches with shader and pass metadata."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"rows": [], "summary": [], "count": 0}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            rows = []
            order = [0]

            stage_map = [
                ("vs_shader", rd.ShaderStage.Vertex),
                ("hs_shader", rd.ShaderStage.Hull),
                ("ds_shader", rd.ShaderStage.Domain),
                ("gs_shader", rd.ShaderStage.Geometry),
                ("ps_shader", rd.ShaderStage.Pixel),
                ("cs_shader", rd.ShaderStage.Compute),
            ]

            def collect(actions, markers=None):
                if markers is None:
                    markers = []

                for action in actions:
                    name = action.GetName(structured_file)
                    flags = action.flags
                    is_marker = bool(
                        flags
                        & (
                            rd.ActionFlags.PushMarker
                            | rd.ActionFlags.SetMarker
                            | rd.ActionFlags.BeginPass
                            | rd.ActionFlags.PassBoundary
                        )
                    )
                    current_markers = markers + [name] if is_marker else markers

                    if flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch):
                        order[0] += 1
                        controller.SetFrameEvent(action.eventId, False)
                        pipe = controller.GetPipelineState()

                        shaders = {}
                        for key, stage in stage_map:
                            shaders[key] = self._shader_resource_name(
                                self.ctx, pipe, stage
                            )

                        if flags & rd.ActionFlags.Dispatch:
                            primary_shader = shaders["cs_shader"]
                        else:
                            primary_shader = (
                                shaders["ps_shader"]
                                or shaders["vs_shader"]
                                or shaders["gs_shader"]
                            )

                        rows.append(
                            {
                                "order": order[0],
                                "event_id": action.eventId,
                                "action_id": action.actionId,
                                "action_type": self._action_type_name(flags),
                                "action_name": name,
                                "marker_path": " / ".join(current_markers),
                                "shader_name": self._short_shader_name(primary_shader),
                                "shader_name_full": primary_shader,
                                "pass_name": self._parse_pass_name(primary_shader),
                                "keywords": self._parse_keywords(primary_shader),
                                "vs_shader": shaders["vs_shader"],
                                "ps_shader": shaders["ps_shader"],
                                "hs_shader": shaders["hs_shader"],
                                "ds_shader": shaders["ds_shader"],
                                "gs_shader": shaders["gs_shader"],
                                "cs_shader": shaders["cs_shader"],
                                "num_indices": action.numIndices,
                                "num_instances": action.numInstances,
                            }
                        )

                    if action.children:
                        collect(action.children, current_markers)

            collect(root_actions)

            grouped = {}
            for row in rows:
                key = (
                    row["shader_name_full"],
                    row["pass_name"],
                    row["marker_path"],
                    row["action_type"],
                )
                if key not in grouped:
                    grouped[key] = {
                        "shader_name": row["shader_name"],
                        "shader_name_full": row["shader_name_full"],
                        "pass_name": row["pass_name"],
                        "keywords": row["keywords"],
                        "marker_path": row["marker_path"],
                        "pass_marker": self._extract_pass_marker(row["marker_path"]),
                        "action_type": row["action_type"],
                        "count": 0,
                        "first_order": row["order"],
                        "last_order": row["order"],
                        "first_event_id": row["event_id"],
                        "last_event_id": row["event_id"],
                    }
                entry = grouped[key]
                entry["count"] += 1
                entry["last_order"] = row["order"]
                entry["last_event_id"] = row["event_id"]

            summary = sorted(
                grouped.values(),
                key=lambda item: (item["first_order"], item["shader_name"], item["pass_name"]),
            )

            result["rows"] = rows
            result["summary"] = summary
            result["count"] = len(rows)

        self._invoke(callback)

        if output_dir:
            return self._write_drawcall_csv(result, output_dir)

        return result

    @staticmethod
    def _write_drawcall_csv(result, output_dir):
        detail_columns = [
            "order",
            "event_id",
            "action_id",
            "action_type",
            "action_name",
            "marker_path",
            "shader_name",
            "pass_name",
            "keywords",
            "shader_name_full",
            "vs_shader",
            "ps_shader",
            "hs_shader",
            "ds_shader",
            "gs_shader",
            "cs_shader",
            "num_indices",
            "num_instances",
        ]
        summary_columns = [
            "count",
            "pass_marker",
            "shader_name",
            "pass_name",
            "keywords",
            "action_type",
            "marker_path",
            "first_order",
            "last_order",
            "first_event_id",
            "last_event_id",
            "shader_name_full",
        ]

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        detail_path = os.path.join(output_dir, "drawcall_analysis.csv")
        summary_path = os.path.join(output_dir, "drawcall_analysis_summary.csv")

        with open(detail_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=detail_columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(result["rows"])

        with open(summary_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=summary_columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(result["summary"])

        return {
            "count": result["count"],
            "detail_path": detail_path,
            "summary_path": summary_path,
            "written": True,
        }
