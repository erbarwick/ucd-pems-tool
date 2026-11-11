"""Command-line argument parser and dispatch handlers for the Caltrans PeMS toolkit."""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from pems.client import PeMSClient
from pems.config import DEFAULT_MASTER_CSV, LOGGER, WORKSPACE_DIR
from pems.corridor import find_stations_by_coordinate_range, find_stations_by_postmile_range
from pems.downloads import download_project_data, download_station_data
from pems.geo import convert_absolute_postmile, convert_california_postmile, find_closest_census_stations, resolve_absolute_postmiles
from pems.imagery import generate_corridor_slices, get_project_imagery
from pems.lane_config import (
    download_all_lane_configurations,
    fetch_project_lane_configurations,
    fetch_station_lane_configuration,
)
from pems.mapping import build_multi_project_map, build_single_project_map
from pems.normalize import normalize_district, parse_coordinate_string, split_ca_pm
from pems.projects import ProjectPeMSQueryBuilder, ProjectRecord, load_master_csv
from pems.reporting import (
    _save_lane_config_report,
    format_coordinate_search_report,
    format_lane_config_report,
    format_postmile_range_report,
    format_single_postmile_report,
)
from pems.telemetry import run_open_to_traffic_inference_pipeline
from pems.vds import compare_project_stations, get_indexed_vds_metadata, load_vds_metadata, run_station_comparison_pipeline
from pems.web import launch_project_web_interface


def build_parser() -> argparse.ArgumentParser:
    """Build and return the organized CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pems_interface.py",
        description="""Caltrans PeMS Interface & Highway Corridor Engine for CERCET Research.

Capabilities:
  • Project inspection, absolute postmile conversion, and PeMS query building
  • High-resolution historical satellite imagery (Zoom 19) & corridor curtain sliders
  • PeMS detector time-series data downloads (flow, speed, occ, aadt) across years
  • Statewide corridor sensor auditing against spreadsheet records with backups
  • Open-to-traffic date inference via lane config history & detector telemetry
  • Raw PeMS station lane tables & Caltrans TSN roadway metrics inspection
  • Route-locked coordinate range detector searches (decimal & Google Maps DMS)
  • Bidirectional California postmile conversions (CA PM <-> Abs PM & coords) & cross-county maps""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 1. Inspect a highway project (limits, Abs PMs, dates, sensors, PeMS URLs):
  python pems_interface.py -p 400000680

  # 2. Fetch raw PeMS lane configuration tables & Caltrans TSN roadway metrics:
  python pems_interface.py -p 400000680 --lanes
  python pems_interface.py -s 410756 --lanes

  # 3. Search stations along a route corridor by coordinate range (Google Maps DMS or decimal):
  python pems_interface.py -r 101 -d NB --coords '38.186, -122.601' '38.220, -122.607'
  python pems_interface.py -r 101 -d NB --coords '38°11\\'10.19"N 122°36\\'05.91"W' '38°13\\'14.99"N 122°36\\'27.10"W'
  python pems_interface.py -r 101 -d NB --coords '38.186, -122.601' '38.220, -122.607' --lanes
  python pems_interface.py -r 101 -d NB --coords '38.186, -122.601' '38.220, -122.607' --map

  # 4. Bidirectional Postmile Conversions:
  python pems_interface.py -r 101 -d NB -c Sonoma --pm 0.21
  python pems_interface.py -r 101 -d NB -c "Santa Clara" --pm 50.56
  python pems_interface.py -r 101 -d NB --pm 400.2 --absolute

  # 5. Query and map custom postmile corridor (County PM or Absolute PM):
  python pems_interface.py -r 101 -d NB -c Sonoma --begin-pm 0.0 --end-pm 4.5 --map
  python pems_interface.py -r 101 -d NB --begin-pm 400.2 --end-pm 424.0 --absolute --map

  # 6. Generate high-resolution (Zoom 19) corridor slices & validation web dashboard:
  python pems_interface.py -p 400000680 --slices
  python pems_interface.py --download-lane-config
  python pems_interface.py -p 414000032 --web
  python pems_interface.py -p 414000032 --web --fast
        """,
    )

    act_group = parser.add_argument_group("Primary Actions & Target")
    act_group.add_argument(
        "--project",
        "-p",
        "--inspect",
        dest="project",
        metavar="PID",
        help="Target Project ID. Default action is project inspection. Can also be combined with --slices, --imagery, --map, or --download.",
    )
    act_group.add_argument(
        "--web",
        dest="web",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Generate and launch the single-page research validation interface for a project (e.g. -p 414000032 --web or --web 414000032).",
    )
    act_group.add_argument(
        "--fast",
        action="store_true",
        default=False,
        help="Use locally cached station configurations instead of live scraping PeMS when building the --web interface.",
    )
    act_group.add_argument(
        "--slices",
        "--corridor-slices",
        dest="slices",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Generate high-resolution (Zoom 19) corridor slices with curtain-slider HTML comparison report.",
    )
    act_group.add_argument(
        "--imagery",
        dest="imagery",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Download historical satellite imagery before and after construction.",
    )
    act_group.add_argument(
        "--map",
        dest="map",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Generate interactive HTML map for a project, coordinate range, or postmile corridor.",
    )
    act_group.add_argument(
        "--map-all",
        action="store_true",
        help="Generate interactive HTML map for all projects statewide (or filtered by --dist).",
    )
    act_group.add_argument(
        "--download",
        "--download-project",
        dest="download_project",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Download PeMS detector time-series data for all stations in a project across all years.",
    )
    act_group.add_argument(
        "--station",
        "-s",
        "--download-station",
        dest="download_station",
        metavar="STATION_ID",
        help="Download detector time-series data from PeMS for a specific station ID across all years.",
    )
    act_group.add_argument(
        "--audit",
        "--match-stations",
        dest="match_stations",
        action="store_true",
        help="Audit and match active PeMS VDS stations along corridors against spreadsheet sensors.",
    )
    act_group.add_argument(
        "--find-open-date",
        "--open-date",
        dest="find_open_date",
        action="store_true",
        help="Automatically infer Open-to-Traffic date from PeMS lane configuration & detector telemetry (Option 1).",
    )
    act_group.add_argument(
        "--no-telemetry",
        dest="check_telemetry",
        action="store_false",
        default=True,
        help="Disable daily flow telemetry corroboration during open-to-traffic date inference.",
    )
    act_group.add_argument(
        "--lane-config",
        "--lanes",
        "--show-lanes",
        dest="lane_config",
        nargs="?",
        const=True,
        default=False,
        metavar="PID_OR_STATION",
        help="Fetch and display raw PeMS station lane tables and Caltrans TSN roadway metrics.",
    )
    act_group.add_argument(
        "--download-lane-config",
        dest="download_lane_config",
        nargs="?",
        const=True,
        default=False,
        metavar="PID_OR_STATION",
        help="Download and cache PeMS station lane configs locally in downloads/station_config_cache/ (all projects, -p <PID>, --dist <D>, or -s <SID>). Use with --overwrite to refresh.",
    )
    act_group.add_argument("--list-urls", action="store_true", help="List generated PeMS URLs for projects.")
    act_group.add_argument("--test-login", action="store_true", help="Test PeMS portal login credentials.")
    act_group.add_argument(
        "--hpms",
        action="store_true",
        help="Cross-reference and validate project corridor with FHWA HPMS ground-truth lane counts and AADT.",
    )

    img_group = parser.add_argument_group("Imagery & Corridor Slices Options")
    img_group.add_argument(
        "--period",
        "--imagery-period",
        dest="imagery_period",
        default="both",
        choices=["before", "after", "both"],
        help="Imagery period relative to project construction (default: 'both').",
    )
    img_group.add_argument("--date", "--imagery-date", dest="imagery_date", help="Specific custom date (YYYY-MM-DD) for imagery retrieval.")
    img_group.add_argument("--before-date", help="Specific custom before date (YYYY-MM-DD or M/D/YY).")
    img_group.add_argument("--after-date", help="Specific custom after date (YYYY-MM-DD or M/D/YY).")
    img_group.add_argument("--zoom", "--imagery-zoom", dest="imagery_zoom", type=int, default=None, help="Force specific zoom level (1-19).")
    img_group.add_argument("--slice-mi", type=float, default=0.25, help="Corridor slice segment length in miles (default: 0.25 mi).")
    img_group.add_argument(
        "--half-width",
        "--imagery-half-width",
        dest="imagery_half_width",
        type=float,
        default=110.0,
        help="Corridor slice half-width in meters perpendicular to roadway (default: 110m).",
    )
    img_group.add_argument("--aligned", action="store_true", help="Rotate and crop single-frame imagery to an aligned corridor strip.")
    img_group.add_argument("--margin", "--imagery-margin", dest="imagery_margin", type=float, default=150.0, help="Margin around highway segment in meters (default: 150m).")
    img_group.add_argument("--workers", type=int, default=16, help="Concurrent download worker threads for imagery tiles (default: 16).")
    img_group.add_argument("--quality-window", type=float, default=5.0, help="Max years around target date for quality prioritization (default: 5.0).")
    img_group.add_argument(
        "--no-prioritize-quality",
        dest="prioritize_quality",
        action="store_false",
        default=True,
        help="Disable resolution-based quality prioritization and use strict nearest date.",
    )

    dl_group = parser.add_argument_group("PeMS Data Download Options")
    dl_group.add_argument("--data-type", default="flow", choices=["flow", "occ", "speed", "vmt", "vht", "delay", "aadt"], help="Data quantity (default: 'flow').")
    dl_group.add_argument("--granularity", default="month", choices=["month", "day", "hour", "week", "year", "5min"], help="Time granularity (default: 'month').")
    dl_group.add_argument("--format", "-f", "--export-format", dest="export_format", default="text", choices=["text", "xls", "xlsx"], help="Export format (default: 'text').")
    dl_group.add_argument("--start-year", type=int, default=1999, help="Earliest year to query (default: 1999).")
    dl_group.add_argument("--end-year", type=int, default=None, help="Latest year to query (default: current calendar year).")
    dl_group.add_argument("--no-combine", action="store_true", help="Do not create consolidated CSV across queried years.")
    dl_group.add_argument("--overwrite", action="store_true", help="Re-download and overwrite existing files.")

    map_group = parser.add_argument_group("Interactive Mapping Options")
    map_group.add_argument("--output-map", "-o", dest="output_map", help="Custom output path for generated HTML map.")
    map_group.add_argument("--dist", help="Filter --map-all or --audit to a specific Caltrans District (e.g. '4').")
    map_group.add_argument("--no-stations", action="store_true", help="Do not include PeMS VDS station layers on single-project map.")
    map_group.add_argument("--include-stations", action="store_true", help="Include PeMS VDS station layers on multi-project map (--map-all).")
    map_group.add_argument("--station-buffer", type=float, default=3.0, help="Corridor buffer in miles for map stations (default: 3.0 mi).")
    map_group.add_argument("--map-station-types", default=None, help="Comma-separated station types for map.")

    audit_group = parser.add_argument_group("Corridor Audit & VDS Matching Options")
    audit_group.add_argument("--dry-run", action="store_true", default=None, help="Perform station comparison without modifying CSV (default: True).")
    audit_group.add_argument("--apply", action="store_true", help="Apply updates to master CSV (creates automated backup).")
    audit_group.add_argument("--output-report", help="Save comparison / audit / query report to a text file.")
    audit_group.add_argument("--output-csv", help="Custom output path for updated master CSV when using --apply.")
    audit_group.add_argument("--station-types", default="ML,HV", help="Comma-separated station types for matching (default: 'ML,HV').")
    audit_group.add_argument("--buffer", type=float, default=0.1, help="Postmile buffer tolerance in miles (default: 0.1).")
    audit_group.add_argument("--metadata-dir", help="Custom directory containing PeMS metadata text files.")

    adhoc_group = parser.add_argument_group("Ad-Hoc Corridor Query & Postmile Conversion Options")
    adhoc_group.add_argument("--route", "-r", help="Freeway / highway route number (e.g. 101, 80, 5).")
    adhoc_group.add_argument("--dir", "-d", help="Freeway direction (NB, SB, EB, WB, N, S, E, W).")
    adhoc_group.add_argument("--pm", help="Single postmile to convert and resolve coordinates.")
    adhoc_group.add_argument("--begin-pm", help="Begin postmile for corridor query.")
    adhoc_group.add_argument("--end-pm", help="End postmile for corridor query.")
    adhoc_group.add_argument("--absolute", "--abs", dest="absolute", action="store_true", help="Treat postmile inputs as continuous Absolute Postmiles.")
    adhoc_group.add_argument("--county", "-c", help="County name or abbreviation (e.g. Sonoma, SON).")
    adhoc_group.add_argument("--begin-county", help="County for begin postmile across county boundaries.")
    adhoc_group.add_argument("--end-county", help="County for end postmile across county boundaries.")
    adhoc_group.add_argument("--coords", "--coordinate-range", dest="coords", nargs="+", metavar="COORD", help="Start and end coordinates (decimal or DMS).")
    adhoc_group.add_argument("--start-coords", dest="start_coords", help="Start coordinate for corridor search.")
    adhoc_group.add_argument("--end-coords", dest="end_coords", help="End coordinate for corridor search.")
    adhoc_group.add_argument("--strict-route", action="store_true", help="Do not auto-switch highway route if coords are distant.")
    adhoc_group.add_argument("--census", action="store_true", help="Search closest Census stations.")

    gen_group = parser.add_argument_group("General & Configuration")
    gen_group.add_argument("--input", "-i", default=DEFAULT_MASTER_CSV, help="Path to cleaned Master CSV.")
    gen_group.add_argument("--output-dir", default=".", help="Directory to save downloaded files or imagery (default: '.').")
    gen_group.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds for PeMS requests (default: 60s).")
    gen_group.add_argument("--no-open", action="store_true", help="Do not automatically open HTML reports/maps in browser.")

    return parser


def _parse_coord_pair_from_args(args: argparse.Namespace) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Extract and parse two corridor endpoint coordinates from CLI arguments."""
    c1_str: Optional[str] = None
    c2_str: Optional[str] = None
    if args.start_coords and args.end_coords:
        c1_str, c2_str = args.start_coords, args.end_coords
    elif args.coords:
        if len(args.coords) == 1:
            parts = re.split(r"\s+to\s+|;\s*", args.coords[0], flags=re.IGNORECASE)
            if len(parts) < 2:
                print("Error: Please provide two coordinates separated by space or 'to' (e.g. --coords '38.186, -122.601' '38.220, -122.607').")
                sys.exit(1)
            c1_str, c2_str = parts[0], parts[1]
        elif len(args.coords) == 2:
            c1_str, c2_str = args.coords[0], args.coords[1]
        elif len(args.coords) == 4:
            c1_str = f"{args.coords[0]}, {args.coords[1]}"
            c2_str = f"{args.coords[2]}, {args.coords[3]}"
        else:
            combined = " ".join(args.coords)
            parts = re.split(r"\s+to\s+|;\s*", combined, flags=re.IGNORECASE)
            if len(parts) >= 2:
                c1_str, c2_str = parts[0], parts[1]
            else:
                matches = list(re.finditer(r"\d+[°\s]+\d+[\'\s]+[\d\.]+[\"\s]*[NSns][\s,;]+\d+[°\s]+\d+[\'\s]+[\d\.]+[\"\s]*[EWew]", combined))
                if len(matches) < 2:
                    print("Error: Unable to parse two coordinates from --coords input. Please quote them.")
                    sys.exit(1)
                c1_str, c2_str = matches[0].group(0), matches[1].group(0)

    c_start = parse_coordinate_string(c1_str)
    c_end = parse_coordinate_string(c2_str)
    if not c_start or not c_end:
        print(f"Error: Could not parse coordinates. Start: '{c1_str}', End: '{c2_str}'. Expected decimal degrees or DMS.")
        sys.exit(1)
    return c_start, c_end


def _build_adhoc_project(res: Dict[str, Any], is_coord_query: bool) -> ProjectRecord:
    """Construct a synthetic ProjectRecord for an ad-hoc coordinate or postmile query."""
    c_range = res["corridor_range"]
    if is_coord_query:
        s_snap, e_snap = res["snapped_start"], res["snapped_end"]
        dist_val = str(s_snap.get("district") or ("4" if str(s_snap.get("county")).lower() == "sonoma" else "1"))
        return ProjectRecord(
            project_id=f"CORRIDOR-R{res['route']}",
            dist=dist_val,
            county=str(s_snap["county"]),
            state="CA",
            route=str(res["route"]),
            direction=res["direction"] or (res.get("travel_vector") or "NB"),
            begin_pm=str(s_snap["ca_pm"]),
            end_pm=str(e_snap["ca_pm"]),
            abs_begin_pm=c_range["abs_min"],
            abs_end_pm=c_range["abs_max"],
            start_coords=res["input_start"],
            end_coords=res["input_end"],
            sensor_stations=[str(s.id) for s in res["stations"]],
            project_description=f"Corridor: Route {res['route']} from {s_snap['county']} PM {s_snap['ca_pm']} to {e_snap['county']} PM {e_snap['ca_pm']}",
        )

    dist_val = str(res["start"].get("district") or res["end"].get("district") or "4")
    return ProjectRecord(
        project_id=f"CORRIDOR-R{res['route']}-PM{res['begin_pm']}-{res['end_pm']}",
        dist=dist_val,
        county=str(res["begin_county"]),
        state="CA",
        route=str(res["route"]),
        direction=res["direction"] or "NB",
        begin_pm=str(res["begin_pm"]),
        end_pm=str(res["end_pm"]),
        abs_begin_pm=c_range["abs_min"],
        abs_end_pm=c_range["abs_max"],
        start_coords=res["start"]["coord"],
        end_coords=res["end"]["coord"],
        sensor_stations=[str(s.id) for s in res["stations"]],
        project_description=f"Custom Corridor Query: Route {res['route']} {res['direction']} from {res['begin_county']} PM {res['begin_pm']} to {res['end_county']} PM {res['end_pm']}",
    )


def _output_adhoc_report_and_map(
    args: argparse.Namespace,
    res: Dict[str, Any],
    adhoc_proj: ProjectRecord,
    text_formatter: Any,
    default_map_name: str,
) -> None:
    """Render either lane config or search report, optionally export report and map."""
    uninst = {
        "coverage_status": res.get("coverage_status"),
        "coverage_details": res.get("coverage_details"),
        "nearest_station": res.get("nearest_station"),
        "census_stations": res.get("census_stations"),
    }
    if args.lane_config:
        client = None
        if not args.fast:
            client = PeMSClient()
            client.login()
        st_details = [
            fetch_station_lane_configuration(
                s.id,
                client=client,
                vds_record=s,
                timeout=getattr(args, "timeout", 60.0),
                use_cache=True,
            )
            for s in res["stations"]
        ]
        report = format_lane_config_report(
            st_details,
            project=adhoc_proj,
            hpms_summary=res.get("hpms_summary"),
            uninstrumented_info=uninst,
        )
        print(report)
        if args.output_report:
            _save_lane_config_report(
                args.output_report,
                st_details,
                project=adhoc_proj,
                hpms_summary=res.get("hpms_summary"),
                uninstrumented_info=uninst,
            )
    else:
        report = text_formatter(res)
        print(report)
        if args.output_report:
            with open(args.output_report, "w", encoding="utf-8") as fh:
                fh.write(report)
            print(f"\n✓ Exported report -> {args.output_report}")

    if args.map or args.output_map:
        out_p = args.output_map or default_map_name
        build_single_project_map(
            target_pid=adhoc_proj.project_id,
            projects=[adhoc_proj],
            output_path=out_p,
            open_browser=not args.no_open,
            include_stations=not args.no_stations,
            station_buffer_mi=args.buffer,
            metadata_dir=args.metadata_dir,
        )
        print(f"✓ Generated interactive corridor map -> {out_p}")


def _handle_coord_query(args: argparse.Namespace) -> None:
    c_start, c_end = _parse_coord_pair_from_args(args)
    st_types = tuple(t.strip() for t in args.station_types.split(",")) if args.station_types else ("ML", "HV")
    res = find_stations_by_coordinate_range(
        route=args.route,
        start_coord=c_start,
        end_coord=c_end,
        direction=args.dir,
        buffer_mi=args.buffer,
        station_types=st_types,
        metadata_dir=args.metadata_dir,
        strict_route=args.strict_route,
    )
    if "error" in res:
        print(f"\n[Error]: {res['error']}")
        sys.exit(1)

    adhoc_proj = _build_adhoc_project(res, is_coord_query=True)
    _output_adhoc_report_and_map(
        args, res, adhoc_proj, format_coordinate_search_report, f"corridor_route_{res['route']}_stations.html"
    )


def _handle_single_pm_conversion(args: argparse.Namespace) -> None:
    target_pm = args.pm or args.begin_pm
    if not args.route:
        print("Error: Postmile conversion requires a highway route (e.g. -r 101 -c Sonoma --pm 0.21 or -r 101 --pm 400.2 --absolute).")
        sys.exit(1)

    is_abs = bool(args.absolute) or (not args.county and not args.begin_county)
    if is_abs:
        res = convert_absolute_postmile(route=args.route, abs_pm=target_pm, direction=args.dir)
    else:
        res = convert_california_postmile(
            route=args.route, county=args.county or args.begin_county, ca_pm=target_pm, direction=args.dir
        )
    report = format_single_postmile_report(res)
    print(report)
    if args.output_report:
        with open(args.output_report, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"\n✓ Exported conversion report -> {args.output_report}")


def _handle_pm_range_query(args: argparse.Namespace) -> None:
    if not args.route:
        print("Error: Corridor query requires a highway route (e.g. -r 101 -c Sonoma --begin-pm 0.0 --end-pm 4.5).")
        sys.exit(1)

    is_abs = bool(args.absolute) or (not args.county and not args.begin_county and not args.end_county)
    st_types = tuple(t.strip() for t in args.station_types.split(",")) if args.station_types else ("ML", "HV")
    res = find_stations_by_postmile_range(
        route=args.route,
        begin_pm=args.begin_pm,
        end_pm=args.end_pm,
        county=args.county,
        begin_county=args.begin_county,
        end_county=args.end_county,
        direction=args.dir,
        is_absolute=is_abs,
        buffer_mi=args.buffer,
        station_types=st_types,
        metadata_dir=args.metadata_dir,
    )
    if "error" in res:
        print(f"\n[Error]: {res['error']}")
        sys.exit(1)

    adhoc_proj = _build_adhoc_project(res, is_coord_query=False)
    safe_co = str(res["begin_county"]).replace(" ", "_")
    default_map = f"corridor_route_{res['route']}_{safe_co}_PM{res['begin_pm']}_{res['end_pm']}.html"
    _output_adhoc_report_and_map(args, res, adhoc_proj, format_postmile_range_report, default_map)


def _resolve_target_pid_and_station(
    args: argparse.Namespace, projects: List[ProjectRecord]
) -> Tuple[Optional[str], Optional[str]]:
    target_pid: Optional[str] = None
    target_station: Optional[str] = None
    if isinstance(args.web, str):
        target_pid = args.web.strip()
    elif isinstance(args.slices, str):
        target_pid = args.slices.strip()
    elif isinstance(args.imagery, str):
        target_pid = args.imagery.strip()
    elif isinstance(args.map, str):
        target_pid = args.map.strip()
    elif isinstance(args.download_project, str):
        target_pid = args.download_project.strip()
    elif isinstance(args.lane_config, str) or isinstance(args.download_lane_config, str):
        raw_val = args.lane_config if isinstance(args.lane_config, str) else args.download_lane_config
        val = str(raw_val).strip()
        if any(p.project_id == val for p in projects):
            target_pid = val
        elif len(val) <= 7 and val.isdigit():
            target_station = val
        else:
            target_pid = val
    elif args.project:
        target_pid = str(args.project).strip()

    if args.download_station:
        target_station = str(args.download_station).strip()

    if target_pid:
        matched_target = [p for p in projects if p.project_id == target_pid]
        if not matched_target and not (args.coords or (args.route and not args.project)):
            print(f"Error: Project ID '{target_pid}' not found.")
            sys.exit(1)
        for p in matched_target:
            if p.abs_begin_pm is None or p.abs_end_pm is None:
                calc_b, calc_e = resolve_absolute_postmiles(p.route, p.county, p.begin_pm, p.end_pm)
                if calc_b is not None and calc_e is not None:
                    if p.abs_begin_pm is None:
                        p.abs_begin_pm = calc_b
                    if p.abs_end_pm is None:
                        p.abs_end_pm = calc_e

    return target_pid, target_station


def _handle_lane_config(
    args: argparse.Namespace, target_pid: Optional[str], target_station: Optional[str]
) -> None:
    if target_station:
        client = None
        if not args.fast:
            client = PeMSClient()
            client.login()
        vds_meta = None
        try:
            vds_meta = get_indexed_vds_metadata()
        except Exception:
            pass
        vds_rec = vds_meta.by_id.get(target_station) if vds_meta else None
        details = fetch_station_lane_configuration(
            target_station, client=client, vds_record=vds_rec, timeout=args.timeout
        )
        print(format_lane_config_report(details))
        if args.output_report:
            _save_lane_config_report(args.output_report, [details])
        return

    if not target_pid:
        print("Error: Please specify a Project ID or Station ID to fetch lane configurations (e.g. -p <PID> --lanes or -s <STATION_ID> --lanes).")
        sys.exit(1)

    client = PeMSClient()
    client.login()
    res = fetch_project_lane_configurations(
        project_id=target_pid,
        master_csv_path=args.input,
        client=client,
        metadata_dir=args.metadata_dir,
        buffer_mi=args.buffer,
    )
    matched_p = res["projects"][0] if res["projects"] else None
    print(
        format_lane_config_report(
            res["stations"],
            project=matched_p,
            hpms_summary=res.get("hpms_summary"),
            uninstrumented_info=res.get("uninstrumented_info"),
        )
    )
    if args.output_report:
        _save_lane_config_report(
            args.output_report,
            res["stations"],
            project=matched_p,
            hpms_summary=res.get("hpms_summary"),
            uninstrumented_info=res.get("uninstrumented_info"),
        )


def _handle_project_inspection(
    args: argparse.Namespace, projects: List[ProjectRecord], target_pid: str
) -> None:
    matched = [p for p in projects if p.project_id == target_pid]
    if not matched:
        print(f"Error: Project ID '{target_pid}' not found.")
        sys.exit(1)

    for idx, proj in enumerate(matched):
        builder = ProjectPeMSQueryBuilder(proj)
        builder.resolve_absolute_postmiles()
        start_c, end_c = builder.resolve_geometry()
        urls = builder.generate_all_urls()

        print("\n" + "=" * 70)
        print(f"PROJECT INSPECTION: {proj.project_id} (Direction: {proj.direction}) [Match {idx+1}/{len(matched)}]")
        print("=" * 70)
        if proj.project_description:
            print(textwrap.fill(proj.project_description, width=68, initial_indent="  Description:        ", subsequent_indent="                      "))
        print(f"  Route & Dir:        Route {proj.route} {proj.direction} (PeMS dir: {proj.pems_direction})")
        print(f"  District & County:  Dist {proj.dist}, {proj.county} ({proj.state})")
        print(f"  Postmiles:          Begin: {proj.begin_pm} | End: {proj.end_pm}")
        if proj.abs_begin_pm is not None or proj.abs_end_pm is not None:
            inv_note = " [WARNING: Backwards order; using CA PM fallback]" if proj.has_inverted_abs_pm else ""
            print(f"  Absolute PMs:       Begin: {proj.abs_begin_pm} | End: {proj.abs_end_pm}{inv_note}")
        print(f"  Coordinates:        Start: {start_c} | End: {end_c}")
        open_d_disp = proj.open_to_traffic_date if proj.open_to_traffic_date else "(None recorded)"
        print(f"  Dates:              Const: {proj.construction_begin_date} | Open: {open_d_disp}")
        if not proj.open_to_traffic_date:
            print("                      [Tip: Run with --find-open-date to automatically infer open date from PeMS telemetry]")
        print("                      [Tip: Run with --lanes to inspect raw PeMS station lane tables and TSN roadway metrics]")
        print(f"  Sensor Stations:    {proj.sensor_stations if proj.sensor_stations else '(None listed)'}")
        print("\n  GENERATED PEMS URLS:")
        print(f"  Lane Config URL:    {urls['lane_config_url']}")
        print(f"  Route Geometry URL: {urls['geometry_url']}")

        for st_id in proj.sensor_stations:
            print(f"\n  Station {st_id}:")
            print(f"    Summary URL: {urls['station_summary_urls'][st_id]}")
            if "station_config_urls" in urls and st_id in urls["station_config_urls"]:
                print(f"    Config URL:  {urls['station_config_urls'][st_id]}")
            sample_aadt = urls["station_aadt_urls"][st_id][0]
            print(f"    AADT {sample_aadt[0]} URL: {sample_aadt[1]}")
            sample_flow = urls["station_flow_urls"][st_id][0]
            print(f"    Flow {sample_flow[0]} URL: {sample_flow[1]}")

        corr_stations = builder.get_corridor_stations_from_reference()
        print(f"\n  Corridor Reference Segments: {len(corr_stations)} segments found in corridor.")

        try:
            vds_meta = load_vds_metadata(args.metadata_dir)
            comp = compare_project_stations(proj, vds_meta=vds_meta, buffer_mi=args.buffer, station_types=("ML", "HV"))
            print("\n  PEMS VDS DETECTOR MATCHING AUDIT:")
            print(f"    Match Status:          {comp.status}")
            print(f"    Spreadsheet Sensors:   {comp.existing_stations if comp.existing_stations else '(None listed)'}")
            print(f"    Corridor Active VDS:   {[s.id for s in comp.found_stations]} ({len(comp.found_stations)} detected)")
            if comp.matched_stations:
                print(f"    Verified in Corridor:  {comp.matched_stations}")
            if comp.missing_in_sheet:
                print(f"    Additional in Corridor:{comp.missing_in_sheet}")
            if comp.discrepancies:
                print("    Discrepancies / Out-of-Range:")
                for d in comp.discrepancies:
                    print(f"      • Station {d['station_id']}: {'; '.join(d['reasons'])}")
        except Exception as exc:
            LOGGER.debug(f"Could not load VDS metadata for inspection: {exc}")

        if args.census and proj.begin_pm:
            _, num_pm = split_ca_pm(proj.begin_pm)
            if not np.isnan(num_pm):
                census_df = find_closest_census_stations(proj.route, proj.dist, num_pm)
                if not census_df.empty:
                    print("\n  Closest Census Stations:")
                    for _, crow in census_df.iterrows():
                        print(f"    ID: {crow['ID']} | PM: {crow['CA PM']} | Type: {crow['Type']} | Name: {crow['Name']} (delta: {crow['pm_diff']:.2f})")

        if args.hpms:
            try:
                from hpms_interface import HPMSCorridorEngine

                hpms_master = getattr(args, "master_csv", getattr(args, "input", DEFAULT_MASTER_CSV))
                hpms_eng = HPMSCorridorEngine(master_csv_path=hpms_master)
                tandem_res = hpms_eng.tandem_audit_project(proj.project_id, direction=proj.direction)
                print(hpms_eng.format_tandem_output(tandem_res))
            except Exception as exc:
                print(f"\n  [HPMS VALIDATION ERROR]: {exc}")

        print("=" * 70)


def _print_list_urls(projects: List[ProjectRecord]) -> None:
    print("\nListing PeMS URLs for first 5 projects:")
    for proj in projects[:5]:
        urls = ProjectPeMSQueryBuilder(proj).generate_all_urls()
        print(f"\n[{proj.project_id}] {urls['corridor']}")
        if proj.project_description:
            short_desc = proj.project_description if len(proj.project_description) <= 80 else proj.project_description[:77] + "..."
            print(f"  Description: {short_desc}")
        print(f"  Lane Config: {urls['lane_config_url']}")
        if proj.sensor_stations:
            print(f"  Sensors ({len(proj.sensor_stations)}): {', '.join(proj.sensor_stations)}")
    print("\n... (use -p <PID> to inspect full details for any project)")


def _print_default_summary(projects: List[ProjectRecord], master_path: str) -> None:
    print(f"Loaded {len(projects)} projects from {master_path}")
    print("\nSummary of projects by District:")
    df_p = pd.DataFrame(
        [{"PID": p.project_id, "Dist": p.dist, "County": p.county, "Route": p.route, "Dir": p.direction, "Sensors": len(p.sensor_stations)} for p in projects]
    )
    print(df_p.groupby("Dist").agg(projects=("PID", "count"), routes=("Route", "nunique"), sensors=("Sensors", "sum")))
    print("\nQuick Start Guide:")
    print("  Inspect a project:              python pems_interface.py -p <PID>")
    print("  Station lane & TSN metrics:     python pems_interface.py -p <PID> --lanes")
    print("  Cache station lane configs:     python pems_interface.py --download-lane-config")
    print("  Infer open to traffic date:     python pems_interface.py -p <PID> --find-open-date")
    print("  High-res corridor slices:       python pems_interface.py -p <PID> --slices")
    print("  Satellite imagery:              python pems_interface.py -p <PID> --imagery")
    print("  Interactive map:                python pems_interface.py -p <PID> --map")
    print("  Download detector data:         python pems_interface.py -p <PID> --download")
    print("  Audit detector stations:        python pems_interface.py --audit")
    print("\nRun with --help to see all options.")


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entrypoint for the Caltrans PeMS toolkit."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.test_login:
        print("\n--- Testing PeMS Login ---")
        try:
            PeMSClient(debug=True).login()
            print("✓ PeMS Login Successful!")
        except Exception as exc:
            print(f"✗ Login failed: {exc}")
            sys.exit(1)
        return

    if args.coords or (args.start_coords and args.end_coords):
        _handle_coord_query(args)
        return

    if args.pm or (args.begin_pm and not args.end_pm):
        _handle_single_pm_conversion(args)
        return

    if args.begin_pm is not None and args.end_pm is not None:
        _handle_pm_range_query(args)
        return

    if args.download_station and args.download_lane_config and not args.project:
        dl_workers = min(args.workers, 4) if args.workers == 16 else max(1, args.workers)
        download_all_lane_configurations(
            station_id_filter=str(args.download_station).strip(),
            metadata_dir=args.metadata_dir,
            workers=dl_workers,
            timeout=args.timeout,
            overwrite=args.overwrite,
            verbose=True,
        )
        return

    if args.download_station and not args.lane_config and not args.download_lane_config:
        download_station_data(
            station_id=args.download_station,
            data_type=args.data_type,
            granularity=args.granularity,
            start_year=args.start_year,
            end_year=args.end_year,
            output_dir=args.output_dir,
            combine=not args.no_combine,
            overwrite=args.overwrite,
            verbose=True,
            export_format=args.export_format,
        )
        return

    if not os.path.exists(args.input):
        print(f"Error: master CSV file not found: {args.input}")
        sys.exit(1)

    projects = load_master_csv(args.input)
    target_pid, target_station = _resolve_target_pid_and_station(args, projects)

    if args.map_all:
        out_file = args.output_map or ("all_ca_projects_map.html" if not args.dist else f"district_{args.dist}_projects_map.html")
        st_types = tuple(s.strip() for s in args.map_station_types.split(",")) if args.map_station_types else None
        build_multi_project_map(
            projects=projects,
            dist=args.dist,
            output_path=out_file,
            open_browser=not args.no_open,
            include_stations=args.include_stations,
            station_buffer_mi=args.station_buffer,
            station_types=st_types,
        )
        print(f"\n✓ Generated interactive multi-project map -> {out_file}")
        return

    if args.match_stations:
        is_dry_run = True if (args.dry_run is True or not args.apply) else False
        st_types = tuple(s.strip() for s in args.station_types.split(",")) if args.station_types else ("ML", "HV")
        run_station_comparison_pipeline(
            projects=projects,
            master_csv_path=args.input,
            meta_dir=args.metadata_dir,
            dry_run=is_dry_run,
            apply_updates=args.apply,
            output_report_path=args.output_report,
            output_csv_path=args.output_csv,
            station_types=st_types,
            buffer_mi=args.buffer,
            dist_filter=args.dist,
            project_id_filter=target_pid,
            verbose=True,
        )
        return

    if args.find_open_date:
        run_open_to_traffic_inference_pipeline(
            projects=projects,
            master_csv_path=args.input,
            project_id_filter=target_pid,
            dist_filter=args.dist,
            check_telemetry=args.check_telemetry,
            apply_updates=args.apply,
            output_report_path=args.output_report,
            output_csv_path=args.output_csv,
            verbose=True,
        )
        return

    if args.lane_config:
        _handle_lane_config(args, target_pid, target_station)
        return

    if args.download_lane_config:
        dl_workers = min(args.workers, 4) if args.workers == 16 else max(1, args.workers)
        download_all_lane_configurations(
            projects=projects,
            master_csv_path=args.input,
            project_id_filter=target_pid,
            dist_filter=args.dist,
            station_id_filter=target_station,
            metadata_dir=args.metadata_dir,
            buffer_mi=args.station_buffer,
            workers=dl_workers,
            timeout=args.timeout,
            overwrite=args.overwrite,
            verbose=True,
        )
        return

    if args.web:
        if not target_pid:
            print("Error: Please specify a Project ID for the web interface (e.g. -p <PID> --web or --web <PID>).")
            sys.exit(1)
        launch_project_web_interface(
            project_id=target_pid,
            projects=projects,
            open_browser=not args.no_open,
            output_dir=args.output_dir if args.output_dir != "." else None,
            master_csv_path=args.input if os.path.exists(args.input) else None,
            metadata_dir=args.metadata_dir,
            station_buffer_mi=args.station_buffer,
            fetch_online=not args.fast,
        )
        return

    if args.slices:
        if not target_pid:
            print("Error: Please specify a Project ID to generate corridor slices (e.g. -p <PID> --slices or --slices <PID>).")
            sys.exit(1)
        out_d = args.output_dir if args.output_dir != "." else os.path.join(WORKSPACE_DIR, "downloads", "imagery")
        c_before = args.before_date or (args.imagery_date if args.imagery_period == "before" else None)
        c_after = args.after_date or (args.imagery_date if args.imagery_period == "after" else None)
        generate_corridor_slices(
            project_id=target_pid,
            slice_mi=args.slice_mi,
            zoom=args.imagery_zoom or 19,
            half_width_m=args.imagery_half_width,
            output_dir=out_d,
            master_csv_path=args.input if os.path.exists(args.input) else None,
            custom_before_date=c_before,
            custom_after_date=c_after,
            open_browser=not args.no_open,
            workers=args.workers,
            prioritize_quality=args.prioritize_quality,
            quality_window=args.quality_window,
            verbose=True,
        )
        return

    if args.imagery:
        if not target_pid:
            print("Error: Please specify a Project ID for imagery download (e.g. -p <PID> --imagery or --imagery <PID>).")
            sys.exit(1)
        out_d = args.output_dir if args.output_dir != "." else os.path.join(WORKSPACE_DIR, "downloads", "imagery")
        c_before = args.before_date or (args.imagery_date if args.imagery_period == "before" else None)
        c_after = args.after_date or (args.imagery_date if args.imagery_period == "after" else None)
        get_project_imagery(
            project_id=target_pid,
            period=args.imagery_period,
            aligned=args.aligned,
            margin_m=args.imagery_margin,
            zoom=args.imagery_zoom,
            output_dir=out_d,
            master_csv_path=args.input if os.path.exists(args.input) else None,
            custom_date=args.imagery_date,
            custom_before_date=c_before,
            custom_after_date=c_after,
            corridor_slices=False,
            workers=args.workers,
            prioritize_quality=args.prioritize_quality,
            quality_window=args.quality_window,
            verbose=True,
        )
        return

    if args.map:
        if not target_pid:
            print("Error: Please specify a Project ID for map generation (e.g. -p <PID> --map or --map <PID>).")
            sys.exit(1)
        matched = [p for p in projects if p.project_id == target_pid]
        if matched and matched[0].project_description:
            print(f"\n[Project {target_pid}]")
            print(textwrap.fill(matched[0].project_description, width=68, initial_indent="  Description:        ", subsequent_indent="                      "))
        out_file = args.output_map or f"route_map_{target_pid}.html"
        st_types = tuple(s.strip() for s in args.map_station_types.split(",")) if args.map_station_types else None
        m = build_single_project_map(
            target_pid,
            projects=projects,
            output_path=out_file,
            open_browser=not args.no_open,
            include_stations=not args.no_stations,
            station_buffer_mi=args.station_buffer,
            station_types=st_types,
        )
        if m is None:
            sys.exit(1)
        print(f"\n✓ Generated interactive map for Project {target_pid} -> {out_file}")
        return

    if args.download_project:
        if not target_pid:
            print("Error: Please specify a Project ID for detector download (e.g. -p <PID> --download or --download <PID>).")
            sys.exit(1)
        download_project_data(
            project_id=target_pid,
            data_type=args.data_type,
            granularity=args.granularity,
            start_year=args.start_year,
            end_year=args.end_year,
            output_dir=args.output_dir,
            master_csv_path=args.input if os.path.exists(args.input) else None,
            combine=not args.no_combine,
            overwrite=args.overwrite,
            verbose=True,
            export_format=args.export_format,
        )
        return

    if target_pid:
        _handle_project_inspection(args, projects, target_pid)
        return

    if args.list_urls:
        _print_list_urls(projects)
        return

    _print_default_summary(projects, args.input)
