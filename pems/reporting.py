"""Shared terminal, CSV, JSON, and Markdown reporting utilities."""

from __future__ import annotations

import datetime
import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union

import pandas as pd

if TYPE_CHECKING:
    from pems.lane_config import StationLaneConfigDetails
    from pems.projects import ProjectRecord


def format_ascii_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], indent: str = "  ") -> List[str]:
    """Render an aligned fixed-width terminal table from headers and row values."""
    if not headers or not rows:
        return []
    str_rows = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(str(headers[i])), max(len(r[i]) if i < len(r) else 0 for r in str_rows))
        for i in range(len(headers))
    ]
    lines = [
        indent + "  ".join(f"{headers[i]:<{widths[i]}}" for i in range(len(headers))).rstrip(),
        indent + "  ".join("-" * widths[i] for i in range(len(headers))).rstrip(),
    ]
    for r in str_rows:
        line = indent + "  ".join(f"{(r[i] if i < len(r) else ''):<{widths[i]}}" for i in range(len(headers)))
        lines.append(line.rstrip())
    return lines


def format_dict_table_lines(records: Sequence[Dict[str, Any]], indent: str = "    ") -> List[str]:
    """Render a sequence of dictionary records as an aligned ASCII table preserving key order."""
    if not records:
        return []
    cols: List[str] = []
    for rec in records:
        for k in rec.keys():
            if k not in cols:
                cols.append(k)
    if not cols:
        return []
    rows = [[rec.get(c, "") for c in cols] for rec in records]
    return format_ascii_table(cols, rows, indent=indent)


def format_hpms_summary_lines(hpms_summary: Optional[Dict[str, Any]]) -> List[str]:
    """Format FHWA / Caltrans HPMS corridor attributes block shared across reports."""
    if not hpms_summary:
        return []
    lines = ["\n[FHWA / Caltrans HPMS Ground-Truth Roadway Attributes]"]
    pred_lanes = hpms_summary.get("predominant_lanes_2024")
    avg_lanes = hpms_summary.get("avg_lanes_2024")
    lane_str = f"{pred_lanes}" if pred_lanes is not None else "N/A"
    if avg_lanes is not None and avg_lanes != pred_lanes:
        lane_str += f" (Corridor length-weighted avg: {avg_lanes:.2f})"
    lines.append(f"  • Through Lanes (2024):         {lane_str}")

    if hpms_summary.get("facility_type"):
        lines.append(f"  • Facility Type:                {hpms_summary['facility_type']}")
    if hpms_summary.get("access_control"):
        lines.append(f"  • Access Control:               {hpms_summary['access_control']}")
    if hpms_summary.get("surface_type"):
        lines.append(f"  • Surface Type:                 {hpms_summary['surface_type']}")
    if hpms_summary.get("functional_system"):
        lines.append(f"  • Functional System:            {hpms_summary['functional_system']}")
    if hpms_summary.get("avg_aadt_2024"):
        lines.append(f"  • Certified AADT (2024):        {hpms_summary['avg_aadt_2024']:,} vehicles/day")
    if hpms_summary.get("future_aadt"):
        yr_str = f" (Year {hpms_summary['future_aadt_year']})" if hpms_summary.get("future_aadt_year") else ""
        try:
            fa = int(float(hpms_summary["future_aadt"]))
            lines.append(f"  • Forecast Future AADT:         {fa:,} vehicles/day{yr_str}")
        except Exception:
            lines.append(f"  • Forecast Future AADT:         {hpms_summary['future_aadt']}{yr_str}")
    if hpms_summary.get("sections_count"):
        lines.append(f"  • HPMS Corridor Sections:       {hpms_summary['sections_count']} sections analyzed")
    lines.append("-" * 80)
    return lines


def format_uninstrumented_lines(
    coverage_status: Optional[str] = None,
    coverage_details: Optional[str] = None,
    nearest_station: Any = None,
    nearest_station_dist_mi: Optional[float] = None,
    census_stations: Optional[Sequence[Dict[str, Any]]] = None,
    include_header: bool = True,
    fallback_detail: Optional[str] = None,
) -> List[str]:
    """Format uninstrumented corridor diagnostics, nearest active VDS station, and Census stations."""
    lines: List[str] = []
    if include_header:
        lines.append("\n[Corridor Instrumentation & PeMS VDS Status]")
    if coverage_status:
        lines.append(f"  • Coverage:        {coverage_status}")
    detail = coverage_details or fallback_detail
    if detail:
        lines.append(f"  • Details:         {detail}")
    if nearest_station is not None:
        nst_pm = f"Abs PM {nearest_station.abs_pm:.2f}" if getattr(nearest_station, "abs_pm", None) is not None else ""
        dist_str = f", {nearest_station_dist_mi:.2f} mi away." if nearest_station_dist_mi is not None else "."
        lines.append(f"  • Nearest Station: Station {nearest_station.id} ({nearest_station.name}, {nst_pm}){dist_str}")
    if census_stations:
        lines.append("\n[Caltrans Annual Census Traffic Stations]")
        for c in census_stations:
            pm_diff = c.get("pm_diff")
            loc_hint = f" [{pm_diff:.2f} mi from start PM]" if (isinstance(pm_diff, (int, float)) and pm_diff > 0) else (" [Inside corridor]" if pm_diff is not None else "")
            lines.append(f"  • Station {c['ID']}: {c['Name']} (CA PM {c['CA PM']}, Abs PM {c['Abs PM']}){loc_hint}")
    return lines


def _format_station_summary_table(stations: Sequence[Any]) -> List[str]:
    """Build aligned station metadata table shared by coordinate and postmile range reports."""
    cols = ["Station ID", "Dir", "Type", "Lanes", "CA PM", "Abs PM", "Name", "Coordinates"]
    rows = []
    for s in stations:
        pm_str = str(s.state_pm) if s.state_pm else "-"
        abs_str = f"{s.abs_pm:.2f}" if s.abs_pm is not None else "-"
        coord_str = f"({s.latitude:.5f}, {s.longitude:.5f})" if s.latitude and s.longitude else "-"
        lanes_str = str(s.lanes) if s.lanes is not None else "-"
        rows.append([str(s.id), s.dir or "-", s.type or "-", lanes_str, pm_str, abs_str, s.name or "-", coord_str])
    return format_ascii_table(cols, rows, indent="  ")


def format_coordinate_search_report(res: Dict[str, Any]) -> str:
    """Format coordinate corridor station search results into a human-readable report."""
    if "error" in res:
        return f"\n[ERROR]: {res['error']}"

    rt_str = f"Route {res['route']}" + (f" {res['direction']}" if res["direction"] else " (Both Directions)")
    tv_str = f" | Travel Vector: {res['travel_vector']}" if res.get("travel_vector") else ""
    lines = [
        "=" * 80,
        "CALTRANS PEMS ROUTE CORRIDOR STATION SEARCH (COORDINATE RANGE)",
        f"{rt_str}{tv_str} | Buffer: ±{res['buffer_mi']} mi",
        "=" * 80,
    ]
    if res.get("route_mismatch_notice"):
        lines.append(f"\n[Route Notice]: {res['route_mismatch_notice']}")
    if res.get("dir_mismatch_notice"):
        lines.append(f"\n[Direction Notice]: {res['dir_mismatch_notice']}")
    if res.get("auto_detected_route"):
        lines.append(
            f"\n[Highway Auto-Detection]: No route specified. Auto-detected Route {res['route']} "
            f"({res['snapped_start']['county']} County)."
        )

    s_snap, e_snap = res["snapped_start"], res["snapped_end"]
    lines.extend([
        "\nInput & Snapped Coordinates:",
        f"  • Start Coord:     ({res['input_start'][0]:.5f}, {res['input_start'][1]:.5f})",
        f"    Highway Snap:    {s_snap['county']} Co, PM {s_snap['ca_pm']} (Abs PM {s_snap['abs_pm']:.2f}) "
        f"[{s_snap['distance_from_route_mi']:.3f} mi from centerline]",
        f"  • End Coord:       ({res['input_end'][0]:.5f}, {res['input_end'][1]:.5f})",
        f"    Highway Snap:    {e_snap['county']} Co, PM {e_snap['ca_pm']} (Abs PM {e_snap['abs_pm']:.2f}) "
        f"[{e_snap['distance_from_route_mi']:.3f} mi from centerline]",
    ])

    c_range = res["corridor_range"]
    lines.append(
        f"\nCorridor Limits:     Abs PM {c_range['abs_min']:.2f} - {c_range['abs_max']:.2f} "
        f"(Length: {c_range['length_mi']:.2f} mi)"
    )
    if res.get("travel_vector"):
        lines.append(f"  • Travel Vector:   {res['travel_vector']} (Based on start -> end coordinate sequence)")

    lines.extend(format_hpms_summary_lines(res.get("hpms_summary")))

    stations = res["stations"]
    lines.append(f"Matching Stations:   {len(stations)} found in corridor\n")
    if not stations:
        lines.append("  (No PeMS detector stations found in this corridor range)")
        if res.get("coverage_details") or res.get("census_stations"):
            lines.extend(
                format_uninstrumented_lines(
                    coverage_status=res.get("coverage_status", "UNINSTRUMENTED"),
                    coverage_details=res.get("coverage_details"),
                    nearest_station=res.get("nearest_station"),
                    nearest_station_dist_mi=res.get("nearest_station_dist_mi"),
                    census_stations=res.get("census_stations"),
                )
            )
        lines.append("=" * 80)
        lines.append("Tip: Pass --lanes to inspect lane configuration tables & TSN metrics for this corridor.")
        return "\n".join(lines)

    lines.extend(_format_station_summary_table(stations))
    lines.append("=" * 80)
    lines.append("Tip: Pass --lanes to inspect lane configuration tables & TSN metrics for these stations.")
    return "\n".join(lines)


def format_single_postmile_report(res: Dict[str, Any]) -> str:
    """Format single postmile conversion results into a human-readable report."""
    if "error" in res:
        return f"\n[ERROR]: {res['error']}"

    rt_dir = f"Route {res['route']}" + (f" {res['direction']}" if res.get("direction") else "")
    lines = ["=" * 80]
    if res.get("conversion_type") == "ABS_TO_CA":
        lines.extend([
            "CALIFORNIA POSTMILE CONVERSION: Absolute PM -> California County PM",
            f"{rt_dir} | Target Abs PM: {res['abs_pm']}",
            "=" * 80,
            f"  • California County Postmile:   {res['county']} County PM {res['ca_pm']}",
            f"  • Caltrans District:            District {res['district']} ({res['county']} County)",
            f"  • Centerline Coordinates:       ({res['coord'][0]:.5f}, {res['coord'][1]:.5f})",
            f"  • Snapped Reference Postmile:   Abs PM {res['snapped_abs_pm']:.3f} (delta: {res['delta_mi']:.3f} mi)",
        ])
    else:
        lines.extend([
            "CALIFORNIA POSTMILE CONVERSION: California County PM -> Absolute PM",
            f"{rt_dir} | County: {res['county']} | CA PM: {res['ca_pm']}",
            "=" * 80,
            f"  • Converted Absolute Postmile:  Abs PM {res['abs_pm']:.3f}",
            f"  • Centerline Coordinates:       ({res['coord'][0]:.5f}, {res['coord'][1]:.5f})",
            f"  • Caltrans District:            District {res['district']}",
            f"  • Snapped Centerline Segment:   Route {res['route']} {res.get('direction', '')} {res['county']} PM "
            f"{res.get('ca_pm_snapped', res['ca_pm'])}",
        ])
    lines.append("=" * 80)
    return "\n".join(lines)


def format_postmile_range_report(res: Dict[str, Any]) -> str:
    """Format postmile range conversion and station search results into a human-readable report."""
    if "error" in res:
        return f"\n[ERROR]: {res['error']}"

    rt_str = f"Route {res['route']}" + (f" {res['direction']}" if res["direction"] else " (Both Directions)")
    lines = [
        "=" * 80,
        "CALTRANS PEMS ROUTE CORRIDOR POSTMILE QUERY & CONVERSION",
        f"{rt_str} | {res['county']} | CA PM {res['begin_pm']} - {res['end_pm']}",
        "=" * 80,
    ]
    if res.get("dir_mismatch_notice"):
        lines.append(f"\n[Direction Notice]: {res['dir_mismatch_notice']}")

    s_info, e_info, c_range = res["start"], res["end"], res["corridor_range"]
    dist_val = s_info.get("district") or e_info.get("district") or "N/A"
    lines.extend([
        "\nCorridor Conversion & Limits:",
        f"  • Begin Postmile:  {res['begin_county']} Co, PM {res['begin_pm']} -> Abs PM {s_info['abs_pm']:.3f} | "
        f"Coord: ({s_info['coord'][0]:.5f}, {s_info['coord'][1]:.5f})",
        f"  • End Postmile:    {res['end_county']} Co, PM {e_info['end_pm'] if 'end_pm' in e_info else res['end_pm']} -> "
        f"Abs PM {e_info['abs_pm']:.3f} | Coord: ({e_info['coord'][0]:.5f}, {e_info['coord'][1]:.5f})",
        f"  • Corridor Limits: Abs PM {c_range['abs_min']:.2f} - {c_range['abs_max']:.2f} (Length: {c_range['length_mi']:.2f} mi)",
        f"  • Caltrans District: District {dist_val}",
    ])

    lines.extend(format_hpms_summary_lines(res.get("hpms_summary")))

    stations = res["stations"]
    lines.append(f"Matching Stations:   {len(stations)} found in corridor\n")
    if not stations:
        lines.append("  (No PeMS detector stations found in this corridor range)")
        if res.get("coverage_details") or res.get("census_stations"):
            lines.extend(
                format_uninstrumented_lines(
                    coverage_status=res.get("coverage_status", "UNINSTRUMENTED"),
                    coverage_details=res.get("coverage_details"),
                    nearest_station=res.get("nearest_station"),
                    nearest_station_dist_mi=res.get("nearest_station_dist_mi"),
                    census_stations=res.get("census_stations"),
                )
            )
        lines.append("=" * 80)
        lines.append("Tip: Pass --map to generate an interactive HTML map, or --lanes to inspect lane tables.")
        return "\n".join(lines)

    lines.extend(_format_station_summary_table(stations))
    lines.append("=" * 80)
    lines.append("Tip: Pass --map to generate an interactive HTML map, or --lanes to inspect lane tables.")
    return "\n".join(lines)


def format_lane_config_report(
    details: Union["StationLaneConfigDetails", List["StationLaneConfigDetails"]],
    project: Optional["ProjectRecord"] = None,
    hpms_summary: Optional[Dict[str, Any]] = None,
    uninstrumented_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Format station lane tables, change logs, TSN roadway metrics, and HPMS summary."""
    station_list = details if isinstance(details, list) else [details]

    lines = [
        "=" * 80,
        "CALTRANS PEMS STATION LANE CONFIGURATION & ROADWAY METRICS",
    ]
    if project:
        lines.append(
            f"Project ID: {project.project_id} | Route {project.route} {project.direction} | "
            f"Dist {project.dist}, {project.county} County"
        )
        lines.append(
            f"Limits: PM {project.begin_pm} - {project.end_pm} "
            f"(Abs: {project.abs_begin_pm} - {project.abs_end_pm})"
        )
    lines.append("=" * 80)

    lines.extend(format_hpms_summary_lines(hpms_summary))

    if not station_list:
        fallback = (
            f"Route {project.route} {project.direction or ''} in {project.county} County "
            f"(PM {project.begin_pm} - {project.end_pm}) is not instrumented with active mainline PeMS loop detectors."
            if project
            else None
        )
        lines.extend([
            "\n[Corridor Instrumentation & PeMS VDS Status]",
            "  • PeMS Mainline Detectors:      0 stations found (Uninstrumented corridor segment)",
        ])
        ui = uninstrumented_info or {}
        lines.extend(
            format_uninstrumented_lines(
                coverage_details=ui.get("coverage_details"),
                nearest_station=ui.get("nearest_station"),
                nearest_station_dist_mi=ui.get("nearest_station_dist_mi"),
                census_stations=ui.get("census_stations"),
                include_header=False,
                fallback_detail=fallback,
            )
        )
        lines.append("=" * 80)
        return "\n".join(lines)

    for idx, st in enumerate(station_list, start=1):
        lines.extend(["", "-" * 80])
        st_type_str = f" ({st.station_type})" if st.station_type else ""
        st_name_str = f": {st.station_name}" if st.station_name else ""
        lines.append(f"STATION {st.station_id}{st_name_str}{st_type_str} [Station {idx}/{len(station_list)}]")

        loc_parts = []
        if st.route:
            loc_parts.append(f"Route: {st.route} {st.direction or ''}".strip())
        if st.district:
            loc_parts.append(f"Dist: {st.district}")
        if st.county:
            loc_parts.append(f"County: {st.county}")
        if loc_parts:
            lines.append(" | ".join(loc_parts))

        pm_parts = []
        if st.ca_pm:
            pm_parts.append(f"CA PM: {st.ca_pm}")
        if st.abs_pm is not None:
            pm_parts.append(f"Abs PM: {st.abs_pm:.2f}")
        if st.latitude and st.longitude:
            pm_parts.append(f"Coordinates: ({st.latitude:.5f}, {st.longitude:.5f})")
        if pm_parts:
            lines.append(" | ".join(pm_parts))

        if st.url:
            lines.append(f"PeMS URL: {st.url}")
        lines.append("-" * 80)

        if st.error:
            lines.append(f"  [ERROR / STATUS]: {st.error}")
            continue

        lines.append("\n  [Caltrans TSN Roadway Metrics]")
        if st.tsn_roadway_info:
            if st.tsn_roadway_info.get("Status") == "Not Available in PeMS":
                lines.append("    • Status:                          Not Available in PeMS for this station")
                lines.append("      (Caltrans PeMS has not linked TSN roadway data for this station/corridor;")
                lines.append("       see HPMS Ground-Truth Roadway Attributes above for official cross-section metrics)")
            else:
                for k, v in st.tsn_roadway_info.items():
                    lines.append(f"    • {k + ':':<32} {v}")
        else:
            lines.append("    (No TSN roadway metrics recorded for this station)")

        lines.append("\n  [PeMS Station Lane Configuration Table]")
        if st.lane_table:
            lines.extend(format_dict_table_lines(st.lane_table, indent="    "))
            lines.append("    [Note: Lane types reflect raw PeMS definitions ('Mainline', 'HOV', etc.).")
            lines.append("     PeMS does not track granular types like climbing lanes, auxiliary lanes, or turn pockets.]")
        else:
            lines.append("    (No lane configuration records found)")

        lines.append("\n  [PeMS Configuration History / Change Log]")
        if st.change_log:
            lines.extend(format_dict_table_lines(st.change_log, indent="    "))
        else:
            lines.append("    (No configuration changes recorded in PeMS change log)")

        if st.construction_history:
            lines.append("\n  [PeMS Construction History]")
            lines.extend(format_dict_table_lines(st.construction_history, indent="    "))

    lines.append("\n" + "=" * 80)
    return "\n".join(lines)


def _save_lane_config_report(
    output_path: str,
    stations: List["StationLaneConfigDetails"],
    project: Optional["ProjectRecord"] = None,
    hpms_summary: Optional[Dict[str, Any]] = None,
    uninstrumented_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Save lane configuration and TSN roadway metrics report to JSON, Markdown, or text."""
    from dataclasses import asdict

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    out_ext = os.path.splitext(output_path)[1].lower()
    if out_ext == ".json":
        nst = uninstrumented_info.get("nearest_station") if uninstrumented_info else None
        data = {
            "project_id": project.project_id if project else None,
            "project_description": project.project_description if project else None,
            "route": project.route if project else None,
            "direction": project.direction if project else None,
            "hpms_summary": hpms_summary,
            "uninstrumented_info": {
                "coverage_status": uninstrumented_info.get("coverage_status"),
                "coverage_details": uninstrumented_info.get("coverage_details"),
                "nearest_station": nst.id if nst is not None else None,
                "census_stations": uninstrumented_info.get("census_stations"),
            }
            if uninstrumented_info
            else None,
            "stations": [asdict(s) for s in stations],
        }
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        print(f"\n✓ Exported station lane configuration JSON -> {output_path}")
    else:
        report_text = format_lane_config_report(
            stations, project=project, hpms_summary=hpms_summary, uninstrumented_info=uninstrumented_info
        )
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(report_text)
        print(f"\n✓ Exported station lane configuration report -> {output_path}")


def write_dataframe_report(
    df: pd.DataFrame,
    output_path: str,
    markdown_title: str,
    header_lines: Optional[Sequence[str]] = None,
) -> None:
    """Write a report DataFrame to CSV or Markdown (without requiring optional tabulate)."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if output_path.lower().endswith(".md"):
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(f"# {markdown_title}\n\n")
            if header_lines:
                fh.write("\n".join(header_lines) + "\n\n")
            try:
                fh.write(df.to_markdown(index=False))
            except Exception:
                cols = df.columns.tolist()
                fh.write("| " + " | ".join(cols) + " |\n")
                fh.write("| " + " | ".join(["---"] * len(cols)) + " |\n")
                for _, row_vals in df.iterrows():
                    cells = [str(row_vals[c]).replace("\n", " ").replace("|", "\\|") for c in cols]
                    fh.write("| " + " | ".join(cells) + " |\n")
    else:
        df.to_csv(output_path, index=False)
