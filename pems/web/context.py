"""View-model builders and Jinja2 template renderers for slices and validation web pages."""

from __future__ import annotations

import json
import os
import re
import shutil
import webbrowser
from concurrent.futures import ThreadPoolExecutor  # noqa: F401 - kept if needed
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
from jinja2 import Environment, PackageLoader, select_autoescape

from pems.config import DEFAULT_MASTER_CSV, LOGGER, WORKSPACE_DIR
from pems.geo import resolve_absolute_postmiles
from pems.imagery import slice_project_corridor
from pems.lane_config import (
    StationLaneConfigDetails,
    fetch_station_configs_batch,
    format_station_lane_config_tooltip,
    load_cached_station_config,
    populate_station_config_from_vds,
)
from pems.mapping import build_single_project_map, classify_station_relative_to_project
from pems.normalize import google_earth_url, google_maps_url, haversine_distance_miles, split_ca_pm
from pems.projects import ProjectPeMSQueryBuilder, ProjectRecord, get_project_corridor_geometry, load_master_csv
from pems.vds import IndexedVDSMetadata, VDSStationRecord, compare_project_stations, load_vds_metadata

_TEMPLATE_ENV = Environment(
    loader=PackageLoader("pems.web", "templates"),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _resolve_slice_midpoint(
    s_info: Dict[str, Any],
    fallback_coord: Optional[Tuple[float, float]] = None,
) -> Tuple[float, float]:
    mid = s_info.get("midpoint")
    if mid and mid[0] != 0.0 and mid[1] != 0.0:
        return float(mid[0]), float(mid[1])
    if "start_coords" in s_info and "end_coords" in s_info and s_info["start_coords"] and s_info["end_coords"]:
        return (
            (s_info["start_coords"][0] + s_info["end_coords"][0]) / 2.0,
            (s_info["start_coords"][1] + s_info["end_coords"][1]) / 2.0,
        )
    if fallback_coord:
        return float(fallback_coord[0]), float(fallback_coord[1])
    return 37.5, -122.2


def generate_slices_html_report(
    project: ProjectRecord,
    slices_data: List[Dict[str, Any]],
    before_meta: Dict[str, Any],
    after_meta: Dict[str, Any],
    output_dir: str,
    overview_img: Optional[str] = None,
    map_file: Optional[str] = "project_map.html",
) -> str:
    """Render self-contained interactive corridor slices comparison report via Jinja2."""
    html_path = os.path.join(output_dir, "index.html")
    cards = []
    for s in slices_data:
        idx = s["index"]
        mid_lat, mid_lon = _resolve_slice_midpoint(s, fallback_coord=project.start_coords or (0.0, 0.0))
        length_mi = s.get("length_mi", 0.25)
        local_map_target = (
            f"{map_file}#{mid_lat:.6f},{mid_lon:.6f},17,Segment%20{idx}%20Center"
            if map_file
            else f"#{mid_lat:.6f},{mid_lon:.6f},17,Segment%20{idx}%20Center"
        )
        cards.append({
            "index": idx,
            "before_fn": os.path.basename(s["before_image"]),
            "after_fn": os.path.basename(s["after_image"]),
            "stacked_fn": os.path.basename(s["stacked_image"]),
            "pm_range_label": f"PM {s['start_pm']:.2f} → {s['end_pm']:.2f}",
            "length_mi": length_mi,
            "bearing_str": f"{s.get('bearing', 0):.0f}" if s.get("bearing") is not None else "",
            "ge_url": google_earth_url(mid_lat, mid_lon, length_mi=length_mi),
            "gmaps_url": google_maps_url(mid_lat, mid_lon, zoom=18),
            "local_map_target": local_map_target,
            "sensors": s.get("sensors", []),
            "before_capture_date": s.get("before_capture_date", ""),
            "after_capture_date": s.get("after_capture_date", ""),
            "mid_lat": mid_lat,
            "mid_lon": mid_lon,
            "zoom": s.get("zoom", 19),
            "width": s.get("width", 0),
            "height": s.get("height", 0),
        })

    rendered = _TEMPLATE_ENV.get_template("slices_report.html").render(
        project=project,
        cards=cards,
        overview_img=overview_img,
        overview_img_exists=bool(overview_img and os.path.exists(os.path.join(output_dir, overview_img))),
        map_file=map_file,
    )
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    return html_path


def _discover_imagery_directory(
    target_pid: str,
    primary_proj: ProjectRecord,
    imagery_base: str,
    out_dir: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """Find best generated corridor slices directory for a project."""
    if not os.path.exists(imagery_base):
        return None, None, None

    clean_beg = re.sub(r"[^A-Za-z0-9\.]", "", primary_proj.begin_pm)
    clean_end = re.sub(r"[^A-Za-z0-9\.]", "", primary_proj.end_pm)
    pm_tag = f"PM{clean_beg}-{clean_end}"
    candidates = []

    for entry in os.listdir(imagery_base):
        full_p = os.path.join(imagery_base, entry)
        if not os.path.isdir(full_p):
            continue
        if not (entry == f"PID_{target_pid}" or entry.startswith(f"PID_{target_pid}_")):
            continue
        mj = os.path.join(full_p, "metadata.json")
        n_meta = 0
        m_data = None
        if os.path.exists(mj):
            try:
                with open(mj, "r", encoding="utf-8") as fh:
                    m_data = json.load(fh)
                    if m_data and "slices" in m_data:
                        n_meta = len(m_data["slices"])
            except Exception:
                pass
        png_slices = len([f for f in os.listdir(full_p) if f.startswith("slice_") and f.endswith("_stacked.png")])
        candidates.append((max(n_meta, png_slices), bool(m_data), pm_tag in entry, full_p, m_data))

    if not candidates:
        return None, None, None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    best_cnt, _, _, best_p, best_m = candidates[0]
    if best_cnt <= 0:
        return None, None, None
    idx_p = os.path.join(best_p, "index.html")
    dedicated_rel = os.path.relpath(idx_p, out_dir) if os.path.exists(idx_p) else None
    return best_p, best_m, dedicated_rel


def _load_slices_for_dashboard(
    primary_proj: ProjectRecord,
    imagery_dir: Optional[str],
    slice_meta_json: Optional[Dict[str, Any]],
    out_dir: str,
) -> List[Dict[str, Any]]:
    """Build normalized slice context objects including resolved midpoints and relative image paths."""
    if not imagery_dir:
        return []

    corridor_geom_slices = None
    try:
        corridor_geom_slices = slice_project_corridor(primary_proj, slice_mi=0.25)
    except Exception as exc:
        LOGGER.debug(f"Could not precalculate corridor geometry slices: {exc}")

    slices_data: List[Dict[str, Any]] = []
    if slice_meta_json and "slices" in slice_meta_json:
        for s in slice_meta_json["slices"]:
            s_idx = s.get("index", len(slices_data) + 1)
            st_p = b_p = a_p = None
            for fn in os.listdir(imagery_dir):
                if fn.startswith(f"slice_{s_idx:02d}_") and fn.endswith("_stacked.png"):
                    st_p = os.path.join(imagery_dir, fn)
                    b_p = os.path.join(imagery_dir, fn.replace("_stacked.png", "_before.png"))
                    a_p = os.path.join(imagery_dir, fn.replace("_stacked.png", "_after.png"))
                    break

            midpoint = s.get("midpoint")
            start_coords, end_coords = s.get("start_coords"), s.get("end_coords")
            if not midpoint and start_coords and end_coords:
                midpoint = ((start_coords[0] + end_coords[0]) / 2.0, (start_coords[1] + end_coords[1]) / 2.0)
            if not midpoint and corridor_geom_slices:
                match_slice = None
                if 0 <= s_idx - 1 < len(corridor_geom_slices):
                    cand = corridor_geom_slices[s_idx - 1]
                    if s.get("start_pm") is None or abs(cand["start_pm"] - float(s["start_pm"])) <= 0.05:
                        match_slice = cand
                if not match_slice and s.get("start_pm") is not None and s.get("end_pm") is not None:
                    s_mid_pm = (float(s["start_pm"]) + float(s["end_pm"])) / 2.0
                    match_slice = min(corridor_geom_slices, key=lambda cs: abs(((cs["start_pm"] + cs["end_pm"]) / 2.0) - s_mid_pm))
                if match_slice:
                    midpoint = match_slice.get("midpoint")
                    if not midpoint and "start_coords" in match_slice and "end_coords" in match_slice:
                        sc, ec = match_slice["start_coords"], match_slice["end_coords"]
                        midpoint = ((sc[0] + ec[0]) / 2.0, (sc[1] + ec[1]) / 2.0)

            slices_data.append({
                "index": s_idx,
                "start_pm": s.get("start_pm"),
                "end_pm": s.get("end_pm"),
                "length_mi": s.get("length_mi", 0.25),
                "midpoint": midpoint,
                "start_coords": start_coords,
                "end_coords": end_coords,
                "stacked_rel": os.path.relpath(st_p, out_dir) if (st_p and os.path.exists(st_p)) else None,
                "before_rel": os.path.relpath(b_p, out_dir) if (b_p and os.path.exists(b_p)) else None,
                "after_rel": os.path.relpath(a_p, out_dir) if (a_p and os.path.exists(a_p)) else None,
            })
    else:
        s_files = sorted(f for f in os.listdir(imagery_dir) if f.startswith("slice_") and f.endswith("_stacked.png"))
        for idx, fn in enumerate(s_files, 1):
            base_n = fn.replace("_stacked.png", "")
            st_p = os.path.join(imagery_dir, fn)
            b_p = os.path.join(imagery_dir, base_n + "_before.png")
            a_p = os.path.join(imagery_dir, base_n + "_after.png")
            midpoint = start_coords = end_coords = s_pm_val = e_pm_val = None
            l_mi = 0.25
            if corridor_geom_slices and 0 <= idx - 1 < len(corridor_geom_slices):
                m_sl = corridor_geom_slices[idx - 1]
                midpoint = m_sl.get("midpoint")
                start_coords, end_coords = m_sl.get("start_coords"), m_sl.get("end_coords")
                if not midpoint and start_coords and end_coords:
                    midpoint = ((start_coords[0] + end_coords[0]) / 2.0, (start_coords[1] + end_coords[1]) / 2.0)
                s_pm_val, e_pm_val = m_sl.get("start_pm"), m_sl.get("end_pm")
                l_mi = m_sl.get("length_mi", 0.25)

            slices_data.append({
                "index": idx,
                "start_pm": s_pm_val,
                "end_pm": e_pm_val,
                "length_mi": l_mi,
                "midpoint": midpoint,
                "start_coords": start_coords,
                "end_coords": end_coords,
                "stacked_rel": os.path.relpath(st_p, out_dir) if os.path.exists(st_p) else None,
                "before_rel": os.path.relpath(b_p, out_dir) if os.path.exists(b_p) else None,
                "after_rel": os.path.relpath(a_p, out_dir) if os.path.exists(a_p) else None,
            })

    total_slices = max(1, len(slices_data))
    bf_date = (slice_meta_json or {}).get("before_flight", {}).get("capture_date", "Pre-Con")
    af_date = (slice_meta_json or {}).get("after_flight", {}).get("capture_date", "Post-Opening")
    for s in slices_data:
        midpoint = s.get("midpoint")
        if not midpoint and primary_proj.start_coords and primary_proj.end_coords:
            s_frac = (s["index"] - 0.5) / total_slices
            p_s, p_e = primary_proj.start_coords, primary_proj.end_coords
            midpoint = (p_s[0] + s_frac * (p_e[0] - p_s[0]), p_s[1] + s_frac * (p_e[1] - p_s[1]))
        elif not midpoint:
            midpoint = primary_proj.start_coords or (37.5, -122.2)
        mid_lat, mid_lon = float(midpoint[0]), float(midpoint[1])
        s["mid_lat"], s["mid_lon"] = mid_lat, mid_lon
        s["pm_label"] = f" (PM {s['start_pm']:.2f}-{s['end_pm']:.2f})" if s.get("start_pm") is not None else ""
        s["ge_url"] = google_earth_url(mid_lat, mid_lon, length_mi=s.get("length_mi", 0.25))
        s["gmaps_url"] = google_maps_url(mid_lat, mid_lon, zoom=18)
        s["before_capture_date"] = bf_date
        s["after_capture_date"] = af_date

    return slices_data


def _collect_and_fetch_station_configs(
    matched: List[ProjectRecord],
    vds_meta: IndexedVDSMetadata,
    station_buffer_mi: float,
    fetch_online: bool,
    cache_dir: Optional[str],
) -> Tuple[Dict[str, List[VDSStationRecord]], Dict[str, VDSStationRecord], Dict[str, StationLaneConfigDetails]]:
    """Compute corridor stations once per direction, union them, and load/fetch lane configs."""
    stations_by_dir: Dict[str, List[VDSStationRecord]] = {}
    all_unique_stations: Dict[str, VDSStationRecord] = {}
    live_eligible_ids: Set[str] = set()

    def _station_sort_key(st: VDSStationRecord) -> float:
        if st.abs_pm is not None and not np.isnan(st.abs_pm):
            return float(st.abs_pm)
        _, num = st.ca_pm_prefix_and_num
        return float(num) if not np.isnan(num) else 999999.0

    for d_idx, proj in enumerate(matched):
        eff_abs_b, eff_abs_e = proj.effective_abs_pm_bounds
        c_st = vds_meta.find_corridor_stations(
            district=proj.dist,
            route=proj.route,
            direction=proj.pems_direction,
            begin_pm=proj.begin_pm,
            end_pm=proj.end_pm,
            abs_begin_pm=eff_abs_b,
            abs_end_pm=eff_abs_e,
            buffer_mi=station_buffer_mi,
            station_types=("ALL",),
            county=proj.county,
        )
        seen_ids = set()
        dir_list: List[VDSStationRecord] = []
        for s in c_st:
            seen_ids.add(s.id)
            dir_list.append(s)
            all_unique_stations[s.id] = s
            is_in_bounds, _, _ = classify_station_relative_to_project(s, proj)
            if is_in_bounds:
                live_eligible_ids.add(s.id)
        for sid in proj.sensor_stations:
            live_eligible_ids.add(sid)
            if sid in vds_meta.by_id:
                st_rec = vds_meta.by_id[sid]
                all_unique_stations[sid] = st_rec
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    dir_list.append(st_rec)
        dir_list.sort(key=_station_sort_key)
        stations_by_dir[f"dir-{d_idx + 1}"] = dir_list

    station_configs_map: Dict[str, StationLaneConfigDetails] = {}
    uncached_ids: List[str] = []
    for sid in all_unique_stations:
        cached = load_cached_station_config(sid, cache_dir=cache_dir, require_change_log=False)
        if cached is not None and not cached.error:
            station_configs_map[sid] = cached
        else:
            uncached_ids.append(sid)

    uncached_live_ids = [sid for sid in uncached_ids if sid in live_eligible_ids]
    skipped_buffer = len(uncached_ids) - len(uncached_live_ids)
    if skipped_buffer > 0 and fetch_online and not os.getenv("PEMS_OFFLINE"):
        LOGGER.info(
            f"Restricting live PeMS scraping to {len(uncached_live_ids)} in-bounds/spreadsheet station(s); "
            f"{skipped_buffer} uncached out-of-bounds buffer station(s) will use VDS metadata."
        )

    if uncached_live_ids and fetch_online and not os.getenv("PEMS_OFFLINE"):
        if os.getenv("PEMS_USERNAME") and os.getenv("PEMS_PASSWORD"):
            try:
                fetched = fetch_station_configs_batch(
                    uncached_live_ids,
                    vds_records=all_unique_stations,
                    timeout=30.0,
                    workers=4,
                    use_cache=True,
                    cache_dir=cache_dir,
                )
                for s_id, cfg_res in fetched.items():
                    if cfg_res and not cfg_res.error:
                        populate_station_config_from_vds(
                            cfg_res, all_unique_stations.get(s_id), only_if_missing=True, synth_lanes=True
                        )
                        station_configs_map[s_id] = cfg_res
            except Exception as exc:
                LOGGER.warning(f"Live PeMS station config fetch failed: {exc}")

    for sid in uncached_ids:
        if sid not in station_configs_map:
            fb_details = StationLaneConfigDetails(station_id=sid)
            populate_station_config_from_vds(fb_details, all_unique_stations.get(sid), synth_lanes=True)
            station_configs_map[sid] = fb_details

    return stations_by_dir, all_unique_stations, station_configs_map


def _build_direction_view_model(
    d_idx: int,
    proj: ProjectRecord,
    all_st: List[VDSStationRecord],
    vds_meta: IndexedVDSMetadata,
    hpms_data: Optional[Dict[str, Any]],
    station_configs_map: Dict[str, StationLaneConfigDetails],
    station_buffer_mi: float,
    cache_dir: Optional[str],
) -> Dict[str, Any]:
    """Assemble template-ready validation matrix, stations rows, and spreadsheet metadata for one direction."""
    dir_id = f"dir-{d_idx + 1}"
    _, b_n = split_ca_pm(proj.begin_pm)
    _, e_n = split_ca_pm(proj.end_pm)
    sheet_len = abs(e_n - b_n) if (not np.isnan(b_n) and not np.isnan(e_n)) else None
    poly = get_project_corridor_geometry(proj)
    geom_len = sum(haversine_distance_miles(p1[0], p1[1], p2[0], p2[1]) for p1, p2 in zip(poly[:-1], poly[1:])) if len(poly) >= 2 else None
    comp = compare_project_stations(proj, vds_meta=vds_meta, buffer_mi=0.1, station_types=("ML", "HV"))

    station_rows = []
    for st in all_st:
        is_in_b, _, delta = classify_station_relative_to_project(st, proj)
        in_sheet = st.id in proj.sensor_stations
        if is_in_b:
            bounds_cls, bounds_text = "badge-pass", "[IN CORRIDOR]"
        elif delta is not None and abs(delta) <= station_buffer_mi:
            bounds_cls, bounds_text = "badge-neutral", f"[BUFFER ({delta:+.2f} mi)]"
        else:
            bounds_cls, bounds_text = "badge-warn", "[OUT OF BOUNDS]"

        if is_in_b and in_sheet:
            sheet_cls, sheet_text = "badge-pass", "[VERIFIED IN SHEET]"
        elif in_sheet and not is_in_b:
            sheet_cls, sheet_text = "badge-warn", "[SHEET DISCREPANCY]"
        elif is_in_b and not in_sheet:
            sheet_cls, sheet_text = "badge-info", "[ADDITIONAL ACTIVE]"
        else:
            sheet_cls, sheet_text = "badge-neutral", "[NEARBY DETECTOR]"

        st_cfg = station_configs_map.get(st.id) or load_cached_station_config(st.id, cache_dir=cache_dir)
        st_lat = st_cfg.latitude if (st_cfg and st_cfg.latitude) else st.latitude
        st_lon = st_cfg.longitude if (st_cfg and st_cfg.longitude) else st.longitude
        station_rows.append({
            "id": st.id,
            "type": st.type,
            "fwy": st.fwy,
            "dir": st.dir,
            "county": st.county,
            "state_pm": st.state_pm,
            "abs_pm_str": f"{st.abs_pm:.2f}" if (st.abs_pm is not None and not np.isnan(st.abs_pm)) else "-",
            "lanes_str": str(st.lanes) if st.lanes is not None else "-",
            "name": st.name or "-",
            "bounds_cls": bounds_cls,
            "bounds_text": bounds_text,
            "sheet_cls": sheet_cls,
            "sheet_text": sheet_text,
            "has_focus": bool(st_lat and st_lon),
            "lat": st_lat or 0.0,
            "lon": st_lon or 0.0,
        })

    tot_b = proj.raw_row.get("total_lanes_before")
    tot_a = proj.raw_row.get("total_lanes_after")
    tot_b_str = f"{float(tot_b):.0f}" if (tot_b is not None and pd.notna(tot_b)) else "-"
    tot_a_str = f"{float(tot_a):.0f}" if (tot_a is not None and pd.notna(tot_a)) else "-"
    net_str = f" (+{float(tot_a) - float(tot_b):.0f})" if (tot_b_str != "-" and tot_a_str != "-") else ""
    sheet_lanes_str = f"{tot_b_str} → {tot_a_str}{net_str}"

    hpms_b_str = str(hpms_data.get("hpms_before_lanes")) if hpms_data and hpms_data.get("hpms_before_lanes") is not None else "-"
    hpms_a_str = str(hpms_data.get("hpms_after_lanes")) if hpms_data and hpms_data.get("hpms_after_lanes") is not None else "-"
    hpms_b_yr = hpms_data.get("hpms_before_year") if hpms_data and hpms_data.get("hpms_before_year") is not None else "-"
    hpms_a_yr = hpms_data.get("hpms_after_year") if hpms_data and hpms_data.get("hpms_after_year") is not None else "-"
    hpms_lanes_str = f"{hpms_b_str} ({hpms_b_yr}) → {hpms_a_str} ({hpms_a_yr})" if hpms_data else "(HPMS data unavailable)"

    if hpms_data and hpms_a_str != "-" and tot_a_str != "-":
        if hpms_a_str == tot_a_str:
            lane_badge_cls, lane_badge_text = "badge-pass", f"[PASS: HPMS & Sheet Match ({tot_a_str} lanes)]"
        else:
            lane_badge_cls, lane_badge_text = "badge-warn", f"[CHECK: HPMS {hpms_a_str} vs Sheet {tot_a_str}]"
    elif not comp.found_stations:
        lane_badge_cls, lane_badge_text = "badge-neutral", "[UNINSTRUMENTED: PeMS 0 Detectors]"
    else:
        lane_badge_cls, lane_badge_text = "badge-info", "[EVALUATED]"

    vds_lanes_list = [str(s.lanes) for s in comp.found_stations if s.type in ("ML", "HV") and s.lanes is not None]
    vds_lanes_str = f"ML/HV detectors: [{', '.join(vds_lanes_list)}] lanes" if vds_lanes_list else "(No active ML/HV detectors)"

    inverted_abs_pm = proj.has_inverted_abs_pm
    abs_pm_warning = ""
    if inverted_abs_pm:
        abs_b_val = float(proj.abs_begin_pm)  # type: ignore[arg-type]
        abs_e_val = float(proj.abs_end_pm)    # type: ignore[arg-type]
        if proj.has_ca_pm_bounds:
            abs_pm_warning = (
                f"Spreadsheet Absolute Postmiles are ordered backwards "
                f"(Abs Begin {abs_b_val:.2f} > Abs End {abs_e_val:.2f}). "
                f"Ignoring backwards Abs PMs and falling back to California County Postmiles "
                f"({proj.county} PM {proj.begin_pm} → {proj.end_pm})."
            )
        else:
            abs_pm_warning = (
                f"Spreadsheet Absolute Postmiles are ordered backwards "
                f"(Abs Begin {abs_b_val:.2f} > Abs End {abs_e_val:.2f}), "
                "and California County Postmiles are missing or invalid."
            )

    has_finite_abs = (
        proj.abs_begin_pm is not None
        and proj.abs_end_pm is not None
        and not np.isnan(proj.abs_begin_pm)
        and not np.isnan(proj.abs_end_pm)
    )
    if has_finite_abs:
        abs_range_str = f"Abs PM {float(proj.abs_begin_pm):.2f} → {float(proj.abs_end_pm):.2f}"  # type: ignore[arg-type]
        if inverted_abs_pm:
            abs_range_str = f"Sheet {abs_range_str} [BACKWARDS - USING CA PM]"
    else:
        abs_range_str = "-"

    sheet_len_str = f"{sheet_len:.2f} mi" if sheet_len is not None else "-"
    geom_len_str = f"{geom_len:.2f} mi" if geom_len is not None else "-"
    if inverted_abs_pm:
        delta_note = (
            f" (delta: {abs(sheet_len - geom_len):.2f} mi)"
            if (sheet_len is not None and geom_len is not None)
            else ""
        )
        pm_badge_cls = "badge-warn"
        pm_badge_text = (
            f"[FLAG: Backwards Abs PM ({float(proj.abs_begin_pm):.2f} > {float(proj.abs_end_pm):.2f}); "  # type: ignore[arg-type]
            f"using CA PM{delta_note}]"
        )
    elif sheet_len is not None and geom_len is not None:
        diff = abs(sheet_len - geom_len)
        if diff <= 0.05:
            pm_badge_cls, pm_badge_text = "badge-pass", f"[PASS: Aligns (delta: {diff:.2f} mi)]"
        else:
            pm_badge_cls, pm_badge_text = "badge-warn", f"[FLAG: Deviates by {diff:.2f} mi]"
    else:
        pm_badge_cls, pm_badge_text = "badge-neutral", f"[LENGTH: {sheet_len_str}]"

    ex_aadt = proj.raw_row.get("Existing AADT")
    fc_aadt = proj.raw_row.get("forecast AADT ") or proj.raw_row.get("forecast AADT")
    aadt_yr = proj.raw_row.get("year ") or proj.raw_row.get("year")
    fc_yr = proj.raw_row.get("forecast year ") or proj.raw_row.get("forecast year")
    hpms_2024 = hpms_data.get("hpms_2024_aadt") if hpms_data else None
    hpms_fut = hpms_data.get("hpms_future_aadt") if hpms_data else None

    breakdown_fields = [
        ("GP Lanes", "New Gen Pur-pose Lanes (Lane Miles)"),
        ("HOV Lanes", "New HOV Lanes (Lane Miles)"),
        ("Turn Pockets", "New or Extend L/R Turn Pockets (Lane Miles)"),
        ("Aux Lanes", "New or Extend Aux Lanes (Lane Miles)"),
        ("Truck Climbing", "New or Extend Truck Climb-ing Lanes (Lane Miles)"),
        ("Accel/Decel", "New or Extend Accel/Decel Lanes (Lane Miles)"),
        ("Toll Lanes", "New Exp Toll Lanes (ETL) (Lane Miles)"),
        ("Transit Lanes", "New Transit Lanes (Lane Miles)"),
        ("Ramps/Connectors", "New Lanes in Ramps and Conn-ectors (Non-Main-line)"),
        ("Total Added", "Total Added Lanes (Lane Miles)"),
    ]
    breakdown = [
        (label, proj.raw_row.get(col))
        for label, col in breakdown_fields
        if proj.raw_row.get(col) is not None and pd.notna(proj.raw_row.get(col)) and str(proj.raw_row.get(col)).strip() not in ("", "0")
    ]

    return {
        "dir_id": dir_id,
        "dir_label": proj.direction or f"Dir {d_idx + 1}",
        "route": str(proj.route),
        "project_id": proj.project_id,
        "is_active": d_idx == 0,
        "sheet_lanes_str": sheet_lanes_str,
        "hpms_lanes_str": hpms_lanes_str,
        "lane_badge_cls": lane_badge_cls,
        "lane_badge_text": lane_badge_text,
        "dir_lanes_before": proj.raw_row.get("dir_lanes_before") or "-",
        "dir_lanes_after": proj.raw_row.get("dir_lanes_after") or "-",
        "vds_lanes_str": vds_lanes_str,
        "pm_range_str": f"PM {proj.begin_pm} → {proj.end_pm}",
        "abs_range_str": abs_range_str,
        "inverted_abs_pm": inverted_abs_pm,
        "abs_pm_warning": abs_pm_warning,
        "sheet_len_str": sheet_len_str,
        "geom_len_str": geom_len_str,
        "pm_badge_cls": pm_badge_cls,
        "pm_badge_text": pm_badge_text,
        "sheet_aadt_str": f"Ex: {ex_aadt or '-'} ({aadt_yr or '-'}) | Fc: {fc_aadt or '-'} ({fc_yr or '-'})",
        "hpms_aadt_str": f"2024: {hpms_2024:,} | Future: {hpms_fut:,}" if (hpms_2024 and hpms_fut) else "(Not recorded)",
        "aadt_badge_cls": "badge-pass" if hpms_2024 else "badge-neutral",
        "aadt_badge_text": "[HPMS 2024 AVAILABLE]" if hpms_2024 else "[SHEET VOLUMES]",
        "sheet_sensors_cnt": len(proj.sensor_stations),
        "sheet_sensor_ids": [str(sid).strip() for sid in proj.sensor_stations],
        "vds_cnt": len(comp.found_stations),
        "vds_sensor_ids": [str(s.id) for s in comp.found_stations],
        "sensor_badge_cls": "badge-pass" if comp.status in ("EXACT_MATCH", "PARTIAL_MATCH") else "badge-warn",
        "sensor_badge_text": f"[{comp.status}]",
        "stations": station_rows,
        "description": proj.project_description or "(No description recorded in spreadsheet)",
        "const_begin": str(proj.construction_begin_date or proj.raw_row.get("Begin Date (formatted YYYY-MM-DD)") or "-"),
        "open_date": str(proj.open_to_traffic_date or proj.raw_row.get("Open to Traffic Date (formatted YYYY-MM-DD)") or "-"),
        "project_cost": str(proj.raw_row.get("Project Cost") or "-"),
        "vmt_cost": str(proj.raw_row.get("VMT Mitigation Cost") or "-"),
        "breakdown": breakdown,
        "reviewer": str(proj.raw_row.get("Reviewer") or "-"),
        "notes": str(proj.raw_row.get("Notes") or "None"),
        "peer_reviewer": str(proj.raw_row.get("Peer Reviewer") or "-"),
        "peer_notes": str(proj.raw_row.get("Peer Reviewer Notes") or "None"),
    }


def generate_project_validation_web_page(
    project_id: str,
    projects: Optional[List[ProjectRecord]] = None,
    output_dir: Optional[str] = None,
    master_csv_path: Optional[str] = None,
    metadata_dir: Optional[str] = None,
    station_buffer_mi: float = 0.5,
    fetch_online: bool = True,
    cache_dir: Optional[str] = None,
    imagery_base_dir: Optional[str] = None,
    vds_meta: Optional[IndexedVDSMetadata] = None,
) -> str:
    """Generate single-page research validation web dashboard via Jinja2 templates."""
    target_pid = str(project_id).strip()
    all_projects = projects or load_master_csv(master_csv_path or DEFAULT_MASTER_CSV)
    matched = [p for p in all_projects if p.project_id == target_pid]
    if not matched:
        raise ValueError(f"Project ID {target_pid} not found in master project records.")

    for p in matched:
        if p.abs_begin_pm is None or p.abs_end_pm is None:
            p.abs_begin_pm, p.abs_end_pm = resolve_absolute_postmiles(p.route, p.county, p.begin_pm, p.end_pm)
        if p.start_coords is None or p.end_coords is None:
            p.start_coords, p.end_coords = ProjectPeMSQueryBuilder(p).resolve_geometry()

    primary_proj = matched[0]
    out_dir = output_dir or os.path.join(WORKSPACE_DIR, "downloads", "web", f"PID_{target_pid}")
    os.makedirs(out_dir, exist_ok=True)
    index_html_path = os.path.join(out_dir, "index.html")
    map_html_path = os.path.join(out_dir, "project_map.html")

    loaded_vds = vds_meta if vds_meta is not None else load_vds_metadata(metadata_dir)
    stations_by_dir, all_unique_stations, station_configs_map = _collect_and_fetch_station_configs(
        matched, loaded_vds, station_buffer_mi, fetch_online, cache_dir
    )

    try:
        build_single_project_map(
            target_pid=target_pid,
            projects=matched,
            output_path=map_html_path,
            open_browser=False,
            include_stations=True,
            station_buffer_mi=station_buffer_mi,
            vds_metadata=loaded_vds,
            precomputed_stations=list(all_unique_stations.values()),
            cache_dir=cache_dir,
        )
    except Exception as exc:
        LOGGER.warning(f"Could not build local station map for PID {target_pid}: {exc}")

    hpms_data = None
    if not os.getenv("PEMS_OFFLINE"):
        try:
            from hpms_interface import HPMSCorridorEngine

            hpms_master = master_csv_path if (master_csv_path and os.path.exists(master_csv_path)) else DEFAULT_MASTER_CSV
            hpms_data = HPMSCorridorEngine(master_csv_path=hpms_master).tandem_audit_project(
                target_pid, direction=primary_proj.direction
            )
        except Exception as exc:
            LOGGER.debug(f"HPMS tandem audit lookup failed for {target_pid}: {exc}")

    imagery_base = imagery_base_dir or os.path.join(WORKSPACE_DIR, "downloads", "imagery")
    imagery_dir, slice_meta_json, dedicated_report_rel = _discover_imagery_directory(
        target_pid, primary_proj, imagery_base, out_dir
    )
    slices_data = _load_slices_for_dashboard(primary_proj, imagery_dir, slice_meta_json, out_dir)

    bf = (slice_meta_json or {}).get("before_flight", {})
    af = (slice_meta_json or {}).get("after_flight", {})
    before_flight_str = (
        f"BEFORE: {bf.get('capture_date', '-')} (Rel {bf.get('release_id', '-')}, {bf.get('provider', '')} @ {bf.get('resolution_m', '-')}m/px)"
        if bf
        else "Pre-Construction Flight"
    )
    after_flight_str = (
        f"AFTER: {af.get('capture_date', '-')} (Rel {af.get('release_id', '-')}, {af.get('provider', '')} @ {af.get('resolution_m', '-')}m/px)"
        if af
        else "Post-Opening Flight"
    )

    station_tooltips = {
        sid: format_station_lane_config_tooltip(
            station_configs_map.get(sid) or StationLaneConfigDetails(station_id=sid),
            vds_rec=st_rec,
            project=primary_proj,
        )
        for sid, st_rec in all_unique_stations.items()
    }

    directions = [
        _build_direction_view_model(
            d_idx, proj, stations_by_dir.get(f"dir-{d_idx + 1}", []), loaded_vds, hpms_data, station_configs_map, station_buffer_mi, cache_dir
        )
        for d_idx, proj in enumerate(matched)
    ]

    prim_poly = get_project_corridor_geometry(primary_proj)
    if prim_poly:
        p_lat, p_lon = prim_poly[len(prim_poly) // 2]
    elif primary_proj.start_coords:
        p_lat, p_lon = primary_proj.start_coords
    else:
        p_lat, p_lon = 37.5, -122.2

    rendered = _TEMPLATE_ENV.get_template("validation_dashboard.html").render(
        target_pid=target_pid,
        primary_proj=primary_proj,
        station_buffer_mi=station_buffer_mi,
        station_tooltips_json=json.dumps(station_tooltips).replace("</", "<\\/"),
        directions=directions,
        slices=slices_data,
        before_flight_str=before_flight_str,
        after_flight_str=after_flight_str,
        dedicated_report_rel=dedicated_report_rel,
        ge_corridor_url=google_earth_url(p_lat, p_lon, distance_m=1200),
        gmaps_corridor_url=google_maps_url(p_lat, p_lon, zoom=16),
    )

    with open(index_html_path, "w", encoding="utf-8") as fh:
        fh.write(rendered)

    if imagery_dir and os.path.exists(imagery_dir):
        try:
            with open(os.path.join(imagery_dir, "validation.html"), "w", encoding="utf-8") as fh:
                fh.write(rendered)
            if os.path.exists(map_html_path):
                shutil.copy2(map_html_path, os.path.join(imagery_dir, "project_map.html"))
        except Exception:
            pass

    return index_html_path


def launch_project_web_interface(
    project_id: str,
    projects: Optional[List[ProjectRecord]] = None,
    open_browser: bool = True,
    output_dir: Optional[str] = None,
    master_csv_path: Optional[str] = None,
    metadata_dir: Optional[str] = None,
    station_buffer_mi: float = 0.5,
    fetch_online: bool = True,
    cache_dir: Optional[str] = None,
) -> str:
    """Generate and open single-page research validation dashboard in the default browser."""
    html_path = generate_project_validation_web_page(
        project_id=project_id,
        projects=projects,
        output_dir=output_dir,
        master_csv_path=master_csv_path,
        metadata_dir=metadata_dir,
        station_buffer_mi=station_buffer_mi,
        fetch_online=fetch_online,
        cache_dir=cache_dir,
    )
    abs_p = os.path.abspath(html_path)
    print("\n" + "=" * 80)
    print(f"PROJECT VALIDATION DASHBOARD: Project {project_id}")
    print("=" * 80)
    print("✓ Single-screen validation interface generated:")
    print(f"  {abs_p}")
    print("=" * 80 + "\n")
    if open_browser:
        webbrowser.open("file://" + abs_p)
    return abs_p
