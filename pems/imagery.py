"""Historical satellite imagery acquisition, corridor curvature slicing, and comparison strips."""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import shutil
import sys
import textwrap
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests

from pems.config import DEFAULT_MASTER_CSV, LOGGER, WORKSPACE_DIR
from pems.geo import IndexedGeoReference, get_indexed_geo_ref
from pems.mapping import build_single_project_map
from pems.normalize import normalize_route, parse_iso_date, parse_project_date, split_ca_pm
from pems.projects import (
    ProjectPeMSQueryBuilder,
    ProjectRecord,
    get_project_corridor_geometry,
    load_master_csv,
    resolve_project_dates,
)
from pems.vds import load_vds_metadata


def _import_wayback() -> Any:
    """Import wayback_imagery.core, falling back to sibling source checkout if installed locally."""
    try:
        import wayback_imagery.core as wayback
        return wayback
    except ImportError:
        sibling_src = os.path.abspath(os.path.join(WORKSPACE_DIR, "..", "..", "..", "..", "wayback_imagery", "src"))
        if os.path.isdir(sibling_src) and sibling_src not in sys.path:
            sys.path.insert(0, sibling_src)
        try:
            import wayback_imagery.core as wayback
            return wayback
        except ImportError as exc:
            raise ImportError(
                "wayback-imagery is required to download historical satellite imagery.\n"
                "Please install it via:\n"
                "  pip install pillow>=10.1\n"
                "  pip install git+https://github.com/erbarwick/wayback-imagery.git\n"
                "or for local editable installation: pip install -e /path/to/wayback_imagery"
            ) from exc


def _make_imagery_session(workers: int = 16) -> requests.Session:
    """Create a requests.Session with expanded connection pooling for Wayback tile downloads."""
    pool_size = max(64, workers * 4)
    sess: Optional[requests.Session] = None
    try:
        wayback = _import_wayback()
        if hasattr(wayback, "_make_session"):
            sess = wayback._make_session()
    except Exception:
        sess = None
    if sess is None:
        sess = requests.Session()
    try:
        adapter = requests.adapters.HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=3)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
    except Exception:
        pass
    return sess


def _resolve_wayback_capture(
    wayback: Any,
    target_date: datetime.date,
    lon: float,
    lat: float,
    zoom: int,
    mode: str,
    session: requests.Session,
    workers: int,
    prioritize_quality: bool,
    quality_window: float,
) -> Any:
    """Find best Wayback capture around target_date, degrading gracefully to simpler call signatures / nearest mode."""
    full_kwargs = {
        "zoom": zoom,
        "mode": mode,
        "session": session,
        "workers": max(16, workers),
        "prioritize_quality": prioritize_quality,
        "max_year_gap": quality_window,
    }
    try:
        return wayback.find_release_by_capture(target_date, lon, lat, **full_kwargs)
    except (ValueError, TypeError):
        try:
            return wayback.find_release_by_capture(target_date, lon, lat, zoom=zoom, mode=mode)
        except ValueError:
            return wayback.find_release_by_capture(target_date, lon, lat, zoom=zoom, mode="nearest")


def _invoke_wayback_generate(
    wayback: Any,
    gen_kwargs: Dict[str, Any],
    session: Optional[requests.Session] = None,
    extra_kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call wayback.generate_image supporting both newer session/capture args and legacy signatures."""
    candidates: List[Dict[str, Any]] = []
    if session is not None and extra_kwargs:
        candidates.append({**gen_kwargs, "session": session, **extra_kwargs})
    elif session is not None:
        candidates.append({**gen_kwargs, "session": session})
    if extra_kwargs:
        candidates.append({**gen_kwargs, **extra_kwargs})
    candidates.append(gen_kwargs)
    stripped = {k: v for k, v in gen_kwargs.items() if k not in ("prioritize_quality", "max_year_gap")}
    if stripped != gen_kwargs:
        candidates.append(stripped)

    last_type_err: Optional[TypeError] = None
    for call_kwargs in candidates:
        try:
            return wayback.generate_image(**call_kwargs)
        except TypeError as exc:
            last_type_err = exc
    raise last_type_err or TypeError("Unable to invoke wayback.generate_image")


def calculate_corridor_curvature_bounds(
    start_coords: Tuple[float, float],
    end_coords: Tuple[float, float],
    intermediate_coords: Sequence[Tuple[float, float]],
) -> Tuple[float, float, float, float, float]:
    """Return (min_dev, max_dev, max_abs_dev, lateral_shift, lateral_half_span) in meters."""
    if not intermediate_coords or len(intermediate_coords) <= 2:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    lat0, lon0 = start_coords
    lat1, lon1 = end_coords
    mid_lat = (lat0 + lat1) / 2.0
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(mid_lat))

    vx = (lon1 - lon0) * m_per_deg_lon
    vy = (lat1 - lat0) * m_per_deg_lat
    chord_len = math.hypot(vx, vy)
    if chord_len < 1e-3:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    nx, ny = -vy / chord_len, vx / chord_len
    signed_devs = [0.0]
    for lat_k, lon_k in intermediate_coords:
        dx = (lon_k - lon0) * m_per_deg_lon
        dy = (lat_k - lat0) * m_per_deg_lat
        signed_devs.append(dx * nx + dy * ny)

    min_dev = min(signed_devs)
    max_dev = max(signed_devs)
    return min_dev, max_dev, max(abs(min_dev), abs(max_dev)), (min_dev + max_dev) / 2.0, (max_dev - min_dev) / 2.0


def calculate_corridor_curvature_deviation(
    start_coords: Tuple[float, float],
    end_coords: Tuple[float, float],
    intermediate_coords: Sequence[Tuple[float, float]],
) -> float:
    """Return maximum absolute lateral chord deviation in meters."""
    _, _, max_abs_dev, _, _ = calculate_corridor_curvature_bounds(start_coords, end_coords, intermediate_coords)
    return max_abs_dev


def slice_project_corridor(
    project: ProjectRecord,
    slice_mi: float = 0.25,
    idx_ref: Optional[IndexedGeoReference] = None,
) -> List[Dict[str, Any]]:
    """Partition a project corridor into sequential segments of ~slice_mi length."""
    ref = idx_ref if idx_ref is not None else get_indexed_geo_ref()
    sub = ref.get_corridor(project.route, project.pems_direction)
    slices: List[Dict[str, Any]] = []

    is_abs = project.has_abs_pm_bounds
    if is_abs:
        b_pm, e_pm = float(project.abs_begin_pm), float(project.abs_end_pm)  # type: ignore[arg-type]
        pm_col = "_abs_pm"
    else:
        _, b_pm = split_ca_pm(project.begin_pm)
        _, e_pm = split_ca_pm(project.end_pm)
        pm_col = "_ca_num"

    pts = None
    if sub is not None and not sub.empty and b_pm is not None and e_pm is not None and not np.isnan(b_pm) and not np.isnan(e_pm):
        min_pm, max_pm = min(float(b_pm), float(e_pm)), max(float(b_pm), float(e_pm))
        ascending = float(b_pm) <= float(e_pm)
        for pad_pm in (0.05, 0.25):
            if is_abs:
                pts = sub[(sub["_abs_pm"] >= min_pm - pad_pm) & (sub["_abs_pm"] <= max_pm + pad_pm)].sort_values(
                    by="_abs_pm", ascending=ascending
                )
            else:
                county_clean = str(project.county).strip().lower()
                pts = sub[
                    (sub["_ca_num"] >= min_pm - pad_pm)
                    & (sub["_ca_num"] <= max_pm + pad_pm)
                    & (sub["_county"] == county_clean)
                ].sort_values(by="_ca_num", ascending=ascending)
            if pts is not None:
                pts = pts.dropna(subset=["_lat", "_lng"])
            if pts is not None and len(pts) >= 2:
                break

    if pts is not None and len(pts) >= 2:
        step = max(1, int(round(slice_mi / 0.1)))
        n_pts = len(pts)
        for i in range(0, n_pts - 1, step):
            j = min(i + step, n_pts - 1)
            if j == i:
                break
            p0, p1 = pts.iloc[i], pts.iloc[j]
            if "_ca_num" in p0 and pd.notna(p0["_ca_num"]) and float(p0["_ca_num"]) > 0:
                s_pm, e_pm_val = float(p0["_ca_num"]), float(p1["_ca_num"])
            else:
                s_pm, e_pm_val = float(p0[pm_col]), float(p1[pm_col])
            length = round(abs(float(p1[pm_col]) - float(p0[pm_col])), 2) or round(0.1 * (j - i), 2)

            sub_coords = [(float(r["_lat"]), float(r["_lng"])) for _, r in pts.iloc[i : j + 1].iterrows()]
            start_c = (float(p0["_lat"]), float(p0["_lng"]))
            end_c = (float(p1["_lat"]), float(p1["_lng"]))
            _, _, max_abs_dev, lat_shift, lat_hspan = calculate_corridor_curvature_bounds(start_c, end_c, sub_coords)
            mid_lat, mid_lon = (start_c[0] + end_c[0]) / 2.0, (start_c[1] + end_c[1]) / 2.0

            slices.append({
                "index": len(slices) + 1,
                "start_pm": round(s_pm, 2),
                "end_pm": round(e_pm_val, 2),
                "abs_start_pm": round(float(p0["_abs_pm"]), 2) if "_abs_pm" in p0 and pd.notna(p0["_abs_pm"]) else None,
                "abs_end_pm": round(float(p1["_abs_pm"]), 2) if "_abs_pm" in p1 and pd.notna(p1["_abs_pm"]) else None,
                "start_coords": start_c,
                "end_coords": end_c,
                "midpoint": (round(mid_lat, 6), round(mid_lon, 6)),
                "length_mi": length,
                "curvature_dev_m": round(max_abs_dev, 2),
                "lateral_shift_m": round(lat_shift, 2),
                "lateral_half_span_m": round(lat_hspan, 2),
                "coords": sub_coords,
            })
    else:
        start_pt, end_pt = ProjectPeMSQueryBuilder(project, ref_df=ref).resolve_geometry()
        if start_pt and end_pt:
            (lat1, lon1), (lat2, lon2) = start_pt, end_pt
            mid_lat = (lat1 + lat2) / 2.0
            dx_m = (lon2 - lon1) * 111320.0 * math.cos(math.radians(mid_lat))
            dy_m = (lat2 - lat1) * 111320.0
            dist_mi = max(0.1, math.hypot(dx_m, dy_m) / 1609.34)
            n_chunks = max(1, int(math.ceil(dist_mi / slice_mi)))
            p_start_pm = float(b_pm) if b_pm is not None and not np.isnan(b_pm) else 0.0
            p_end_pm = float(e_pm) if e_pm is not None and not np.isnan(e_pm) else dist_mi

            for k in range(n_chunks):
                t0, t1 = k / n_chunks, (k + 1) / n_chunks
                s_coords = (lat1 + t0 * (lat2 - lat1), lon1 + t0 * (lon2 - lon1))
                e_coords = (lat1 + t1 * (lat2 - lat1), lon1 + t1 * (lon2 - lon1))
                s_pm = p_start_pm + t0 * (p_end_pm - p_start_pm)
                e_pm_val = p_start_pm + t1 * (p_end_pm - p_start_pm)
                slices.append({
                    "index": k + 1,
                    "start_pm": round(s_pm, 2),
                    "end_pm": round(e_pm_val, 2),
                    "start_coords": s_coords,
                    "end_coords": e_coords,
                    "midpoint": (round((s_coords[0] + e_coords[0]) / 2.0, 6), round((s_coords[1] + e_coords[1]) / 2.0, 6)),
                    "length_mi": round(abs(e_pm_val - s_pm), 2),
                    "curvature_dev_m": 0.0,
                    "coords": [s_coords, e_coords],
                })

    return slices


def create_stacked_slice_image(
    before_img_path: str,
    after_img_path: str,
    out_path: str,
    project: ProjectRecord,
    slice_info: Dict[str, Any],
    before_capture_date: Optional[str] = None,
    after_capture_date: Optional[str] = None,
    zoom: int = 19,
) -> str:
    """Combine Before and After corridor strips into a vertically stacked comparison PNG."""
    from PIL import Image, ImageDraw

    wayback = _import_wayback()
    img_b = Image.open(before_img_path)
    img_a = Image.open(after_img_path)

    w = max(img_b.width, img_a.width)
    header_h, divider_h, footer_h = 72, 28, 32
    total_h = header_h + img_b.height + divider_h + img_a.height + footer_h
    canvas = Image.new("RGB", (w, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    font_lg = wayback._load_font(max(14, min(22, w // 80)))
    font_sm = wayback._load_font(max(11, min(15, w // 120)))

    title = (
        f"PROJECT {project.project_id} | ROUTE {project.route} {project.direction} | "
        f"SEGMENT {slice_info['index']}: PM {slice_info['start_pm']:.2f} -> {slice_info['end_pm']:.2f} "
        f"({slice_info['length_mi']:.2f} mi)"
    )
    subtitle = (
        f"County: {project.county} | District: {project.dist} | Native Zoom {zoom} (~0.3m/px) | "
        "Corridor runs Left to Right"
    )
    draw.text((20, 14), title, fill=(0, 0, 0), font=font_lg)
    draw.text((20, 42), subtitle, fill=(80, 80, 80), font=font_sm)
    draw.line([(0, header_h - 1), (w, header_h - 1)], fill=(0, 0, 0), width=1)

    canvas.paste(img_b, (0, header_h))
    b_badge = f"BEFORE (Pre-Construction) | Flight: {before_capture_date or 'Unknown'}"
    draw.rectangle([(16, header_h + 12), (40 + int(font_sm.getlength(b_badge)), header_h + 36)], fill=(255, 255, 255), outline=(0, 0, 0), width=1)
    draw.text((26, header_h + 16), b_badge, fill=(0, 0, 0), font=font_sm)

    y_div = header_h + img_b.height
    draw.rectangle([(0, y_div), (w, y_div + divider_h)], fill=(240, 240, 240), outline=(0, 0, 0), width=1)
    draw.text((20, y_div + 6), f"BEFORE (Pre-Construction)  /  AFTER (Post-Opening)  [Roadway Strip: {slice_info['length_mi']:.2f} mi]", fill=(0, 0, 0), font=font_sm)

    y_after = y_div + divider_h
    canvas.paste(img_a, (0, y_after))
    a_badge = f"AFTER (Post-Opening) | Flight: {after_capture_date or 'Unknown'}"
    draw.rectangle([(16, y_after + 12), (40 + int(font_sm.getlength(a_badge)), y_after + 36)], fill=(255, 255, 255), outline=(0, 0, 0), width=1)
    draw.text((26, y_after + 16), a_badge, fill=(0, 0, 0), font=font_sm)

    y_foot = total_h - footer_h
    draw.rectangle([(0, y_foot), (w, total_h)], fill=(248, 248, 248), outline=(0, 0, 0), width=1)
    draw.text((20, y_foot + 8), "Source: Esri World Imagery Wayback Archive | UCD CERCET Highway Capacity Expansion Project Analysis", fill=(80, 80, 80), font=font_sm)

    canvas.save(out_path, quality=95)
    return out_path


def _shift_segment_laterally(
    s_coords: Tuple[float, float],
    e_coords: Tuple[float, float],
    lat_shift_m: float,
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    if abs(lat_shift_m) > 1.5:
        lat0, lon0 = s_coords
        lat1, lon1 = e_coords
        mid_lat = (lat0 + lat1) / 2.0
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * math.cos(math.radians(mid_lat))
        vx = (lon1 - lon0) * m_per_deg_lon
        vy = (lat1 - lat0) * m_per_deg_lat
        chord_len = math.hypot(vx, vy)
        if chord_len > 1e-3:
            nx, ny = -vy / chord_len, vx / chord_len
            dlat = (lat_shift_m * ny) / m_per_deg_lat
            dlon = (lat_shift_m * nx) / m_per_deg_lon
            s_coords = (lat0 + dlat, lon0 + dlon)
            e_coords = (lat1 + dlat, lon1 + dlon)

    if abs(s_coords[0] - e_coords[0]) < 1e-6 and abs(s_coords[1] - e_coords[1]) < 1e-6:
        mid_lon_step = 0.001 / max(0.2, math.cos(math.radians(s_coords[0])))
        s_coords = (s_coords[0], s_coords[1] - mid_lon_step)
        e_coords = (e_coords[0], e_coords[1] + mid_lon_step)
    return s_coords, e_coords


def _map_sensors_to_slices(proj: ProjectRecord, slices: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    sensors_by_slice: Dict[int, List[Dict[str, Any]]] = {s["index"]: [] for s in slices}
    try:
        vds_meta = load_vds_metadata()
        if not proj.sensor_stations:
            return sensors_by_slice
        has_abs = any(sd.get("abs_start_pm") is not None and sd.get("abs_end_pm") is not None for sd in slices)
        for sid in proj.sensor_stations:
            s_rec = vds_meta.by_id.get(str(sid).strip())
            if s_rec is None:
                continue
            s_pm = s_rec.state_pm or s_rec.abs_pm
            _, ca_num = split_ca_pm(str(s_pm)) if s_pm is not None else (None, float("nan"))
            target_idx = None
            if has_abs and s_rec.abs_pm is not None and not np.isnan(s_rec.abs_pm):
                for sd in slices:
                    a0, a1 = sd.get("abs_start_pm"), sd.get("abs_end_pm")
                    if a0 is not None and a1 is not None and min(a0, a1) - 0.05 <= s_rec.abs_pm <= max(a0, a1) + 0.05:
                        target_idx = sd["index"]
                        break
            if target_idx is None and not np.isnan(ca_num):
                for sd in slices:
                    if min(sd["start_pm"], sd["end_pm"]) - 0.05 <= ca_num <= max(sd["start_pm"], sd["end_pm"]) + 0.05:
                        target_idx = sd["index"]
                        break
            if target_idx is not None:
                sensors_by_slice[target_idx].append({
                    "id": str(sid).strip(),
                    "pm": ca_num if not np.isnan(ca_num) else s_rec.abs_pm,
                    "type": str(s_rec.type or "ML"),
                    "lanes": str(s_rec.lanes or ""),
                })
    except Exception as exc:
        LOGGER.debug(f"Sensor mapping warning: {exc}")
    return sensors_by_slice


def generate_corridor_slices(
    project_id: str,
    slice_mi: float = 0.25,
    zoom: int = 19,
    half_width_m: float = 110.0,
    output_dir: str = "downloads/imagery",
    master_csv_path: Optional[str] = None,
    custom_before_date: Optional[str] = None,
    custom_after_date: Optional[str] = None,
    open_browser: bool = True,
    workers: int = 16,
    prioritize_quality: bool = True,
    quality_window: float = 5.0,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Generate Zoom-19 corridor slice comparison images, stacked PNGs, project map, and HTML report."""
    from pems.web.context import generate_slices_html_report

    wayback = _import_wayback()
    projects = load_master_csv(master_csv_path)
    matched = [p for p in projects if str(p.project_id).strip() == str(project_id).strip()]
    if not matched:
        raise ValueError(f"Project ID '{project_id}' not found in master project list.")
    proj = matched[0]

    slices = slice_project_corridor(proj, slice_mi=slice_mi)
    if not slices:
        raise ValueError(f"Could not compute corridor geometry for project {project_id}.")

    clean_begin = re.sub(r"[^A-Za-z0-9\.]", "", proj.begin_pm)
    clean_end = re.sub(r"[^A-Za-z0-9\.]", "", proj.end_pm)
    folder_name = f"PID_{proj.project_id}_Route{normalize_route(proj.route)}_{proj.pems_direction}_PM{clean_begin}-{clean_end}"
    proj_dir = os.path.join(output_dir, folder_name)
    os.makedirs(proj_dir, exist_ok=True)

    if verbose:
        print("\n" + "=" * 80)
        print(f"CORRIDOR SLICES GENERATION: Project {proj.project_id} (Route {proj.route} {proj.direction})")
        print("=" * 80)
        if proj.project_description:
            print(textwrap.fill(proj.project_description, width=76, initial_indent="  Description:       ", subsequent_indent="                     "))
        print(f"  Postmile Corridor: {proj.begin_pm} -> {proj.end_pm} ({len(slices)} slices of ~{slice_mi} mi)")
        print(f"  Output Directory:  {proj_dir}")
        print(f"  Target Zoom Level: Native Zoom {zoom} (~0.3m/px resolution)")

    d_before, d_after = resolve_project_dates(proj, custom_before_date, custom_after_date)
    if not d_before or not d_after:
        raise ValueError(f"Project {project_id} requires both construction date and open to traffic date.")

    s_c, e_c = ProjectPeMSQueryBuilder(proj).resolve_geometry()
    lon_c, lat_c = (s_c[1] + e_c[1]) / 2.0, (s_c[0] + e_c[0]) / 2.0  # type: ignore[index]

    if verbose:
        print(f"  Querying Esri Wayback archive for capture flights around {d_before} and {d_after}...")

    imagery_session = _make_imagery_session(workers=workers)
    c_before = _resolve_wayback_capture(wayback, d_before, lon_c, lat_c, zoom, "before", imagery_session, workers, prioritize_quality, quality_window)
    c_after = _resolve_wayback_capture(wayback, d_after, lon_c, lat_c, zoom, "after", imagery_session, workers, prioritize_quality, quality_window)

    before_rel_id, after_rel_id = c_before.release.release_id, c_after.release.release_id
    before_cap_str = c_before.capture_date.isoformat() if c_before.capture_date else str(d_before)
    after_cap_str = c_after.capture_date.isoformat() if c_after.capture_date else str(d_after)

    if verbose:
        b_res = f" @ {c_before.resolution_m:g}m/px" if c_before.resolution_m else ""
        a_res = f" @ {c_after.resolution_m:g}m/px" if c_after.resolution_m else ""
        print(f"  ✓ Resolved BEFORE release {before_rel_id} (Captured: {before_cap_str}{b_res})")
        print(f"  ✓ Resolved AFTER  release {after_rel_id} (Captured: {after_cap_str}{a_res})")

    sensors_by_slice = _map_sensors_to_slices(proj, slices)

    def _process_slice(s: Dict[str, Any]) -> Dict[str, Any]:
        idx = s["index"]
        slice_half_width = max(half_width_m, s.get("lateral_half_span_m", 0.0) + 85.0)
        s_coords, e_coords = _shift_segment_laterally(s["start_coords"], s["end_coords"], s.get("lateral_shift_m", 0.0))
        geom = wayback.corridor_geometry(s_coords, e_coords, half_width_m=slice_half_width, pad_m=40.0)

        prefix = f"slice_{idx:02d}_PM{s['start_pm']:.2f}-{s['end_pm']:.2f}"
        b_dest = os.path.join(proj_dir, f"{prefix}_before.png")
        a_dest = os.path.join(proj_dir, f"{prefix}_after.png")
        stacked_dest = os.path.join(proj_dir, f"{prefix}_stacked.png")

        def _fetch_strip(date_val: datetime.date, rel_id: Any, cap_obj: Any, tag: str) -> Dict[str, Any]:
            return _invoke_wayback_generate(
                wayback,
                {
                    "bbox": geom.envelope,
                    "date": date_val,
                    "label": f"PID {proj.project_id} {tag} | PM {s['start_pm']:.2f}-{s['end_pm']:.2f}",
                    "out_dir": proj_dir,
                    "corridor": geom,
                    "zoom": zoom,
                    "release_id": rel_id,
                    "workers": max(2, workers // 2),
                },
                session=imagery_session,
                extra_kwargs={"capture_info": cap_obj, "sample_dates": False},
            )

        with ThreadPoolExecutor(max_workers=2) as pair_pool:
            b_fut = pair_pool.submit(_fetch_strip, d_before, before_rel_id, c_before, "BEFORE")
            a_fut = pair_pool.submit(_fetch_strip, d_after, after_rel_id, c_after, "AFTER")
            b_res, a_res = b_fut.result(), a_fut.result()

        for src, dst in ((b_res["path"], b_dest), (a_res["path"], a_dest)):
            if os.path.exists(src) and src != dst:
                shutil.move(src, dst)

        create_stacked_slice_image(
            b_dest, a_dest, stacked_dest, proj, s,
            before_capture_date=before_cap_str, after_capture_date=after_cap_str, zoom=zoom,
        )
        w, s_lat, e_lon, n_lat = geom.envelope
        return {
            "index": idx,
            "start_pm": s["start_pm"],
            "end_pm": s["end_pm"],
            "length_mi": s["length_mi"],
            "bearing": geom.bearing,
            "before_image": b_dest,
            "after_image": a_dest,
            "stacked_image": stacked_dest,
            "before_capture_date": before_cap_str,
            "after_capture_date": after_cap_str,
            "zoom": zoom,
            "half_width_m": slice_half_width,
            "curvature_dev_m": s.get("curvature_dev_m", 0.0),
            "width": b_res["width"],
            "height": b_res["height"],
            "sensors": sensors_by_slice.get(idx, []),
            "start_coords": s_coords,
            "end_coords": e_coords,
            "midpoint": ((s_coords[0] + e_coords[0]) / 2.0, (s_coords[1] + e_coords[1]) / 2.0),
            "bbox": [w, s_lat, e_lon, n_lat],
        }

    if verbose:
        for s in slices:
            curv_dev = s.get("curvature_dev_m", 0.0)
            hw = max(half_width_m, s.get("lateral_half_span_m", 0.0) + 85.0)
            note = f" [curve dev: {curv_dev:.1f}m, shift: {s.get('lateral_shift_m', 0.0):.1f}m -> half-width: {hw:.1f}m]" if curv_dev > 5.0 else ""
            print(f"  [Slice {s['index']}/{len(slices)}] PM {s['start_pm']:.2f} -> {s['end_pm']:.2f} ({s['length_mi']:.2f} mi){note}...")

    slices_data: List[Dict[str, Any]] = []
    slice_pool_size = min(len(slices), 4)
    if slice_pool_size > 1:
        with ThreadPoolExecutor(max_workers=slice_pool_size) as pool:
            futures = [pool.submit(_process_slice, s) for s in slices]
            for fut in as_completed(futures):
                rec = fut.result()
                slices_data.append(rec)
                if verbose:
                    print(f"    ✓ Generated slice_{rec['index']:02d}_PM{rec['start_pm']:.2f}-{rec['end_pm']:.2f}_stacked.png ({rec['width']}x{rec['height']} px)")
        slices_data.sort(key=lambda x: x["index"])
    else:
        for s in slices:
            rec = _process_slice(s)
            slices_data.append(rec)
            if verbose:
                print(f"    ✓ Generated slice_{rec['index']:02d}_PM{rec['start_pm']:.2f}-{rec['end_pm']:.2f}_stacked.png ({rec['width']}x{rec['height']} px)")

    overview_fn: Optional[str] = "00_overview_corridor.png"
    overview_path = os.path.join(proj_dir, overview_fn)
    try:
        ov_res = _invoke_wayback_generate(
            wayback,
            {
                "bbox": wayback.bbox_from_segment(s_c, e_c, margin_m=200.0),
                "date": d_after,
                "label": f"PROJECT {proj.project_id} FULL OVERVIEW | ROUTE {proj.route} {proj.direction} PM {proj.begin_pm}-{proj.end_pm}",
                "out_dir": proj_dir,
                "max_px": 1600,
                "max_zoom": 16,
                "release_id": after_rel_id,
                "points": [s_c, e_c],
                "workers": workers,
            },
            session=imagery_session,
            extra_kwargs={"capture_info": c_after, "sample_dates": False},
        )
        if os.path.exists(ov_res["path"]) and ov_res["path"] != overview_path:
            shutil.move(ov_res["path"], overview_path)
    except Exception as exc:
        LOGGER.debug(f"Overview generation failed: {exc}")
        overview_fn = None

    map_fn: Optional[str] = "project_map.html"
    map_path = os.path.join(proj_dir, map_fn)
    try:
        build_single_project_map(
            target_pid=proj.project_id, projects=[proj], output_path=map_path, open_browser=False, include_stations=True
        )
        if verbose:
            print(f"  ✓ Generated local project map with stations: {map_fn}")
    except Exception as exc:
        LOGGER.debug(f"Local project map generation warning: {exc}")
        map_fn = None

    html_report = generate_slices_html_report(
        project=proj,
        slices_data=slices_data,
        before_meta=c_before.as_dict(),
        after_meta=c_after.as_dict(),
        output_dir=proj_dir,
        overview_img=overview_fn,
        map_file=map_fn,
    )

    with open(os.path.join(proj_dir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "project_id": proj.project_id,
                "project_description": proj.project_description,
                "route": proj.route,
                "direction": proj.direction,
                "county": proj.county,
                "postmiles": (proj.begin_pm, proj.end_pm),
                "before_flight": c_before.as_dict(),
                "after_flight": c_after.as_dict(),
                "slices": [{k: v for k, v in s.items() if not k.endswith("_image")} for s in slices_data],
            },
            fh,
            indent=2,
        )

    if verbose:
        print("\n" + "=" * 80)
        print(f"✓ Corridor slices complete! Saved {len(slices_data)} high-res segments to:")
        print(f"  {proj_dir}")
        print(f"✓ Interactive HTML comparison report available at:\n  {html_report}")
        if map_fn:
            print(f"✓ Local station map available at:\n  {map_path}")
        print("=" * 80 + "\n")

    if open_browser:
        webbrowser.open("file://" + os.path.abspath(html_report))

    return {
        "project_id": proj.project_id,
        "folder": proj_dir,
        "slices": slices_data,
        "report_html": html_report,
        "local_map": map_path if map_fn else None,
        "overview_image": overview_path if overview_fn else None,
    }


def get_project_imagery(
    project_id: str,
    period: str = "both",
    aligned: bool = False,
    margin_m: float = 150.0,
    max_px: int = 4096,
    max_zoom: int = 19,
    zoom: Optional[int] = None,
    output_dir: str = "downloads/imagery",
    master_csv_path: Optional[str] = None,
    custom_date: Optional[str] = None,
    custom_before_date: Optional[str] = None,
    custom_after_date: Optional[str] = None,
    corridor_slices: bool = False,
    slice_mi: float = 0.25,
    open_browser: bool = False,
    workers: int = 16,
    prioritize_quality: bool = True,
    quality_window: float = 5.0,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Download and stitch historical satellite imagery for a project (single frames or slices)."""
    if corridor_slices:
        return generate_corridor_slices(
            project_id=project_id,
            slice_mi=slice_mi,
            zoom=zoom or 19,
            half_width_m=110.0,
            output_dir=output_dir,
            master_csv_path=master_csv_path,
            custom_before_date=custom_before_date,
            custom_after_date=custom_after_date,
            open_browser=open_browser,
            workers=workers,
            prioritize_quality=prioritize_quality,
            quality_window=quality_window,
            verbose=verbose,
        )

    wayback = _import_wayback()
    projects = load_master_csv(master_csv_path)
    matched = [p for p in projects if str(p.project_id).strip() == str(project_id).strip()]
    if not matched:
        raise ValueError(f"Project ID '{project_id}' not found in master project list ({master_csv_path or DEFAULT_MASTER_CSV}).")
    proj = matched[0]

    if verbose:
        print("\n" + "=" * 70)
        print(f"HISTORICAL IMAGERY: Project {proj.project_id} (Route {proj.route} {proj.direction})")
        print("=" * 70)
        if proj.project_description:
            print(textwrap.fill(proj.project_description, width=68, initial_indent="  Description:        ", subsequent_indent="                      "))
        print(f"  Route & Corridor:   Route {proj.route} {proj.direction} ({proj.county} PM {proj.begin_pm} -> {proj.end_pm})")
        print(f"  Timeline Target:    Const: {proj.construction_begin_date or proj.start_year} | Open: {proj.open_to_traffic_date or proj.end_year}")

    start_pt, end_pt = ProjectPeMSQueryBuilder(proj).resolve_geometry()
    polyline = get_project_corridor_geometry(proj)
    if (not start_pt or not end_pt) and len(polyline) >= 2:
        start_pt, end_pt = polyline[0], polyline[-1]
    if not start_pt or not end_pt:
        raise ValueError(
            f"Could not resolve geographic coordinates from postmile range ({proj.begin_pm} to {proj.end_pm}) "
            f"for project {proj.project_id} on Route {proj.route}."
        )

    corridor = None
    if aligned:
        curv_dev = calculate_corridor_curvature_deviation(start_pt, end_pt, polyline) if len(polyline) >= 2 else 0.0
        corridor = wayback.corridor_geometry(start_pt, end_pt, half_width_m=max(75.0, curv_dev + 65.0), pad_m=max(45.0, margin_m * 0.5))
        bbox = corridor.envelope
    elif len(polyline) >= 4:
        lats, lons = [pt[0] for pt in polyline], [pt[1] for pt in polyline]
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * math.cos(math.radians((min(lats) + max(lats)) / 2.0))
        pad_x = max(margin_m, 0.1 * (max(lons) - min(lons)) * m_per_deg_lon) / m_per_deg_lon
        pad_y = max(margin_m, 0.1 * (max(lats) - min(lats)) * m_per_deg_lat) / m_per_deg_lat
        bbox = (min(lons) - pad_x, min(lats) - pad_y, max(lons) + pad_x, max(lats) + pad_y)
    else:
        bbox = wayback.bbox_from_segment(start_pt, end_pt, margin_m=margin_m, margin_frac=0.25)

    d_before, d_after = resolve_project_dates(proj, custom_before_date, custom_after_date)
    target_tasks: List[Tuple[str, datetime.date, str]] = []
    if custom_date:
        d = parse_iso_date(custom_date)
        if not d:
            raise ValueError(f"Invalid custom date '{custom_date}'. Please use YYYY-MM-DD.")
        target_tasks.append(("custom", d, "nearest"))
    elif period == "before":
        if not d_before:
            raise ValueError(f"Project {project_id} has no Construction Begin Date in spreadsheet. Specify --imagery-date.")
        target_tasks.append(("before", d_before, "before"))
    elif period == "after":
        if not d_after:
            raise ValueError(f"Project {project_id} has no Open to Traffic Date in spreadsheet. Specify --imagery-date.")
        target_tasks.append(("after", d_after, "after"))
    elif period in ("both", "before_after", "before-after"):
        if d_before:
            target_tasks.append(("before", d_before, "before"))
        elif verbose:
            LOGGER.warning(f"Project {project_id} has no Construction Begin Date. Skipping 'before' image.")
        if d_after:
            target_tasks.append(("after", d_after, "after"))
        elif verbose:
            LOGGER.warning(f"Project {project_id} has no Open to Traffic Date. Skipping 'after' image.")
        if not target_tasks:
            raise ValueError(f"Project {project_id} has neither construction begin date nor open to traffic date.")
    else:
        d = parse_project_date(period)
        if not d:
            raise ValueError(f"Unknown period '{period}'. Use 'before', 'after', 'both', or 'YYYY-MM-DD'.")
        target_tasks.append(("custom", d, "nearest"))

    imagery_session = _make_imagery_session(workers=workers)
    results: Dict[str, Any] = {}
    for kind, target_date, mode in target_tasks:
        label = f"PID {proj.project_id} ({kind.upper()}) - Route {proj.route} {proj.direction} PM {proj.begin_pm}-{proj.end_pm}"
        if verbose:
            print(f"\n[Wayback] Fetching {kind.upper()} imagery for Project {proj.project_id} (target date: {target_date}, mode: {mode})...")

        gen_kwargs = {
            "bbox": bbox,
            "date": target_date,
            "label": label,
            "out_dir": output_dir,
            "zoom": zoom,
            "max_px": max_px,
            "max_zoom": max_zoom,
            "mode": mode,
            "select_by": "capture",
            "points": [start_pt, end_pt] if not aligned else None,
            "workers": workers,
            "corridor": corridor,
            "prioritize_quality": prioritize_quality,
            "max_year_gap": quality_window,
        }
        try:
            img_meta = _invoke_wayback_generate(wayback, gen_kwargs, session=imagery_session)
        except ValueError as exc:
            if "No capture date" in str(exc) and mode in ("before", "after"):
                if verbose:
                    print(f"  Note: {exc} - Falling back to nearest available capture...")
                gen_kwargs["mode"] = "nearest"
                img_meta = _invoke_wayback_generate(wayback, gen_kwargs, session=imagery_session)
            else:
                raise

        results[kind] = img_meta
        if verbose:
            print(f"✓ Saved {kind.upper()} image -> {img_meta.get('path')} (Captured: {img_meta.get('capture_date')}, Release: {img_meta.get('release_date')}, Zoom: {img_meta.get('zoom')})")

    return {
        "project_id": proj.project_id,
        "project_description": proj.project_description,
        "route": proj.route,
        "direction": proj.direction,
        "county": proj.county,
        "postmiles": (proj.begin_pm, proj.end_pm),
        "coordinates": (start_pt, end_pt),
        "bbox": list(bbox),
        "aligned": aligned,
        "imagery": results,
    }
