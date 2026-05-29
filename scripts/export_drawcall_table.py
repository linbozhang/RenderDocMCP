"""
Export draw call analysis tables from the currently loaded RenderDoc capture.

Outputs:
  - drawcall_analysis.csv       : one row per draw call / dispatch (call order)
  - drawcall_analysis_summary.csv : grouped by shader + pass + marker path
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server.bridge.client import RenderDocBridge, RenderDocBridgeError


DETAIL_COLUMNS = [
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

SUMMARY_COLUMNS = [
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


def write_csv(path: Path, columns, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_pass_name(shader_name):
    if not shader_name:
        return ""
    match = re.search(r"\(([^)]+)\)", shader_name)
    if match:
        return match.group(1)
    base = shader_name.split("[", 1)[0]
    if "/" in base:
        return base.rsplit("/", 1)[-1]
    return base


def parse_keywords(shader_name):
    if not shader_name:
        return ""
    match = re.search(r"\[(.*?)\]", shader_name)
    if match:
        return match.group(1)
    return ""


def short_shader_name(shader_name):
    if not shader_name:
        return ""
    base = shader_name.split("[", 1)[0]
    if "(" in base:
        base = base.split("(", 1)[0]
    if "/" in base:
        return base.rstrip("/").rsplit("/", 1)[-1]
    return base


def parse_match_reason(match_reason):
    match = re.search(r"name: '([^']*)'", match_reason or "")
    if match:
        return match.group(1)
    match = re.search(r"entry_point: '([^']*)'", match_reason or "")
    if match:
        return match.group(1)
    return ""


def action_type_name(flags):
    if "Dispatch" in flags:
        return "Dispatch"
    if "Drawcall" in flags:
        if "Indexed" in flags:
            return "DrawIndexed"
        return "Draw"
    return "Unknown"


def extract_pass_marker(marker_path):
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


def build_summary(rows):
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
                "pass_marker": extract_pass_marker(row["marker_path"]),
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

    return sorted(
        grouped.values(),
        key=lambda item: (item["first_order"], item["shader_name"], item["pass_name"]),
    )


def walk_actions(actions, markers=None, draw_meta=None):
    if markers is None:
        markers = []
    if draw_meta is None:
        draw_meta = {}

    for action in actions:
        name = action.get("name", "")
        flags = action.get("flags", [])
        is_marker = any(f in flags for f in ("PushMarker", "SetMarker", "BeginPass", "PassBoundary"))
        current_markers = markers + [name] if is_marker else markers

        if "Drawcall" in flags or "Dispatch" in flags:
            draw_meta[action["event_id"]] = {
                "action_id": action.get("action_id"),
                "action_name": name,
                "action_type": action_type_name(flags),
                "flags": flags,
                "marker_path": " / ".join(current_markers),
                "num_indices": action.get("num_indices", 0),
                "num_instances": action.get("num_instances", 0),
            }

        for child in action.get("children") or []:
            walk_actions([child], current_markers, draw_meta)

    return draw_meta


def collect_draw_metadata(bridge, event_ids, chunk_size=800):
    draw_meta = {}
    if not event_ids:
        return draw_meta

    min_event = min(event_ids)
    max_event = max(event_ids)
    start = min_event
    while start <= max_event:
        end = min(start + chunk_size - 1, max_event)
        chunk = bridge.call(
            "get_draw_calls",
            {
                "event_id_min": start,
                "event_id_max": end,
                "include_children": True,
                "only_actions": False,
            },
        )
        walk_actions(chunk.get("actions", []), draw_meta=draw_meta)
        start = end + 1
    return draw_meta


def export_via_shader_search(bridge):
    print("Using fallback export (shader search + chunked marker scan)...")

    print("  Scanning vertex shaders...")
    vs_result = bridge.call("find_draws_by_shader", {"shader_name": "/", "stage": "vertex"})
    print("  Scanning pixel shaders...")
    ps_result = bridge.call("find_draws_by_shader", {"shader_name": "/", "stage": "pixel"})

    event_ids = [item["event_id"] for item in vs_result.get("matches", [])]
    print("  Collecting marker paths for %d events..." % len(event_ids))
    draw_meta = collect_draw_metadata(bridge, event_ids)

    vs_map = {
        item["event_id"]: parse_match_reason(item.get("match_reason"))
        for item in vs_result.get("matches", [])
    }
    ps_map = {
        item["event_id"]: parse_match_reason(item.get("match_reason"))
        for item in ps_result.get("matches", [])
    }

    dispatch_ids = [
        item["event_id"]
        for item in vs_result.get("matches", [])
        if draw_meta.get(item["event_id"], {}).get("action_type") == "Dispatch"
        or item.get("name", "").endswith("Dispatch()")
    ]
    cs_map = {}
    if dispatch_ids:
        print("  Resolving %d compute shader names..." % len(dispatch_ids))
        for event_id in dispatch_ids:
            try:
                info = bridge.call(
                    "get_shader_info", {"event_id": event_id, "stage": "compute"}
                )
                disasm = info.get("disassembly", "")
                cs_map[event_id] = disasm.splitlines()[0] if disasm else info.get("resource_id", "")
            except RenderDocBridgeError:
                cs_map[event_id] = ""

    rows = []
    for order, item in enumerate(vs_result.get("matches", []), start=1):
        event_id = item["event_id"]
        meta = draw_meta.get(
            event_id,
            {
                "action_id": "",
                "action_name": item.get("name", ""),
                "action_type": "Dispatch"
                if item.get("name", "").endswith("Dispatch()")
                else "Draw",
                "marker_path": "",
                "num_indices": 0,
                "num_instances": 0,
            },
        )

        vs_shader = vs_map.get(event_id, "")
        ps_shader = ps_map.get(event_id, "")
        cs_shader = cs_map.get(event_id, "")

        if meta["action_type"] == "Dispatch":
            primary_shader = cs_shader or vs_shader
        else:
            primary_shader = ps_shader or vs_shader

        rows.append(
            {
                "order": order,
                "event_id": event_id,
                "action_id": meta.get("action_id", ""),
                "action_type": meta.get("action_type", ""),
                "action_name": meta.get("action_name", item.get("name", "")),
                "marker_path": meta.get("marker_path", ""),
                "shader_name": short_shader_name(primary_shader),
                "pass_name": parse_pass_name(primary_shader),
                "keywords": parse_keywords(primary_shader),
                "shader_name_full": primary_shader,
                "vs_shader": vs_shader,
                "ps_shader": ps_shader,
                "hs_shader": "",
                "ds_shader": "",
                "gs_shader": "",
                "cs_shader": cs_shader,
                "num_indices": meta.get("num_indices", 0),
                "num_instances": meta.get("num_instances", 0),
            }
        )

    return {"rows": rows, "summary": build_summary(rows), "count": len(rows)}


def main():
    parser = argparse.ArgumentParser(description="Export draw call analysis tables")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for CSV output (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also write raw JSON export",
    )
    parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="Skip native export and use fallback implementation",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    bridge = RenderDocBridge()
    bridge.timeout = 180.0

    try:
        status = bridge.call("get_capture_status")
    except RenderDocBridgeError as exc:
        print("Error: %s" % exc)
        sys.exit(1)

    if not status.get("loaded"):
        print("Error: No capture loaded in RenderDoc")
        sys.exit(1)

    print("Capture: %s" % status.get("filename", "unknown"))
    print("Exporting draw call analysis...")

    data = None
    if not args.fallback_only:
        try:
            data = bridge.call(
                "export_drawcall_analysis",
                {"output_dir": str(output_dir)},
            )
        except RenderDocBridgeError:
            data = None

    if data is None:
        data = export_via_shader_search(bridge)
        detail_path = output_dir / "drawcall_analysis.csv"
        summary_path = output_dir / "drawcall_analysis_summary.csv"
        write_csv(detail_path, DETAIL_COLUMNS, data.get("rows", []))
        write_csv(summary_path, SUMMARY_COLUMNS, data.get("summary", []))
    elif data.get("written"):
        detail_path = Path(data["detail_path"])
        summary_path = Path(data["summary_path"])
    else:
        detail_path = output_dir / "drawcall_analysis.csv"
        summary_path = output_dir / "drawcall_analysis_summary.csv"
        write_csv(detail_path, DETAIL_COLUMNS, data.get("rows", []))
        write_csv(summary_path, SUMMARY_COLUMNS, data.get("summary", []))

    if args.json:
        json_path = output_dir / "drawcall_analysis.json"
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print("Draw calls exported: %d" % data.get("count", 0))
    print("Detail table : %s" % detail_path)
    print("Summary table: %s" % summary_path)


if __name__ == "__main__":
    main()
