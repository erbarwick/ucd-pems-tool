"""Corridor-level station search by geographic coordinates or postmiles with HPMS context."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd

from pems.config import CA_COUNTY_FIPS, LOGGER, decode_hpms_attributes
from pems.geo import (
    coerce_geo_ref,
    convert_absolute_postmile,
    convert_california_postmile,
)
from pems.normalize import (
    haversine_distance_miles,
    normalize_direction,
    normalize_district,
    normalize_route,
    parse_coordinate_string,
    split_ca_pm,
)
from pems.vds import VDSStationRecord, diagnose_uninstrumented_corridor, load_vds_metadata


def fetch_hpms_corridor_summary(
    route: Union[str, int],
    abs_begin_pm: float,
    abs_end_pm: float,
    county: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch FHWA / Caltrans HPMS ground-truth roadway attributes for any highway corridor."""
    if os.getenv("PEMS_OFFLINE"):
        return None
    try:
        from hpms_interface import HPMSClient

        client = HPMSClient()
        r_num = int(normalize_route(route))
        county_fips = CA_COUNTY_FIPS.get(str(county).lower().strip()) if county else None
        min_pm = min(float(abs_begin_pm), float(abs_end_pm))
        max_pm = max(float(abs_begin_pm), float(abs_end_pm))

        secs = client.get_spatial_sections(route_number=r_num, begin_pm=min_pm, end_pm=max_pm, county_fips=county_fips)
        if not secs and county_fips:
            secs = client.get_spatial_sections(route_number=r_num, begin_pm=min_pm, end_pm=max_pm)
        if not secs:
            return None

        lane_weights: Dict[int, float] = {}
        tot_len = 0.0
        tot_aadt_len = 0.0
        weighted_aadt = 0.0
        for s in secs:
            slen = float(s.get("sectionlength") or 0.0)
            tl = s.get("through_lanes")
            if tl and str(tl).isdigit() and int(tl) > 0:
                lanes_int = int(tl)
                lane_weights[lanes_int] = lane_weights.get(lanes_int, 0.0) + slen
                tot_len += slen
            aadt = s.get("aadt")
            if aadt and str(aadt).isdigit() and int(aadt) > 0:
                weighted_aadt += int(aadt) * slen
                tot_aadt_len += slen

        pred_lanes = max(lane_weights, key=lane_weights.get) if lane_weights else None
        avg_lanes = round(sum(k * v for k, v in lane_weights.items()) / tot_len, 2) if tot_len > 0 else None
        avg_aadt = int(round(weighted_aadt / tot_aadt_len)) if tot_aadt_len > 0 else None
        decoded = decode_hpms_attributes(secs)

        return {
            "sections_count": len(secs),
            "predominant_lanes_2024": pred_lanes,
            "avg_lanes_2024": avg_lanes,
            "avg_aadt_2024": avg_aadt,
            **decoded,
        }
    except Exception as exc:
        LOGGER.debug(f"HPMS summary error for Route {route}: {exc}")
        return None


def _get_route_df(ref: Any, route_norm: str) -> pd.DataFrame:
    """Return DataFrame of reference rows for route_norm from IndexedGeoReference or mock."""
    getter = getattr(ref, "get_route_df", None)
    if callable(getter):
        candidate = getter(route_norm)
        if isinstance(candidate, pd.DataFrame):
            return candidate
    clean = getattr(ref, "clean_df", None)
    if isinstance(clean, pd.DataFrame):
        return clean[clean["_route"] == route_norm]
    return pd.DataFrame()


def _validate_route_direction(
    ref: Any,
    route_norm: str,
    direction: Optional[str],
) -> Tuple[Optional[str], Optional[str], set]:
    """Check whether requested direction matches route cardinal orientation."""
    dir_norm = normalize_direction(direction) if direction else None
    valid_fn = getattr(ref, "valid_directions_for_route", None)
    valid_dirs = valid_fn(route_norm) if callable(valid_fn) else None
    if not isinstance(valid_dirs, (set, list, tuple)):
        route_df = _get_route_df(ref, route_norm)
        valid_dirs = set(route_df["_dir"].dropna().unique()) if "_dir" in route_df.columns else set()
    valid_set = set(valid_dirs)

    notice = None
    if dir_norm and valid_set and dir_norm not in valid_set:
        cardinal_name = (
            "East-West"
            if valid_set.issubset({"E", "W"})
            else ("North-South" if valid_set.issubset({"N", "S"}) else "unaligned")
        )
        notice = (
            f"Route {route_norm} is an {cardinal_name} highway (valid directions: {sorted(valid_set)}). "
            f"Ignoring mismatched direction '{direction}' and searching both directions."
        )
        dir_norm = None

    return dir_norm, notice, valid_set


def _filter_corridor_vds_stations(
    meta: Any,
    route_norm: str,
    dir_norm: Optional[str],
    min_abs: float,
    max_abs: float,
    buffer_mi: float,
    station_types: Optional[Tuple[str, ...]],
    sub_route: Optional[pd.DataFrame] = None,
) -> List[VDSStationRecord]:
    """Filter, deduplicate, and sort VDS stations along a route postmile corridor."""
    types_set = (
        {t.strip().upper() for t in station_types}
        if station_types and "ALL" not in [t.strip().upper() for t in station_types]
        else None
    )
    route_vds = [s for s in meta.by_id.values() if str(s.fwy) == str(route_norm)]
    if dir_norm:
        route_vds = [s for s in route_vds if s.dir == dir_norm]
    if types_set:
        route_vds = [s for s in route_vds if s.type in types_set]

    matched: List[VDSStationRecord] = []
    seen_ids = set()
    for s in route_vds:
        if s.id in seen_ids:
            continue
        in_range = False
        if s.abs_pm is not None:
            in_range = (min_abs - buffer_mi) <= s.abs_pm <= (max_abs + buffer_mi)
        elif sub_route is not None and s.latitude and s.longitude:
            d_st = haversine_distance_miles(s.latitude, s.longitude, sub_route["_lat"], sub_route["_lng"])
            if float(d_st.min()) <= buffer_mi:
                closest_seg = sub_route.loc[d_st.idxmin()]
                in_range = min_abs <= float(closest_seg["_abs_pm"]) <= max_abs
        if in_range:
            seen_ids.add(s.id)
            matched.append(s)

    matched.sort(key=lambda x: (x.dir or "", x.abs_pm if x.abs_pm is not None else 0.0))
    return matched


def find_stations_by_coordinate_range(
    route: Optional[Union[str, int]] = None,
    start_coord: Optional[Union[str, Tuple[float, float]]] = None,
    end_coord: Optional[Union[str, Tuple[float, float]]] = None,
    direction: Optional[str] = None,
    buffer_mi: float = 0.2,
    station_types: Optional[Tuple[str, ...]] = ("ML", "HV"),
    metadata_dir: Optional[str] = None,
    vds_meta: Optional[Any] = None,
    idx_ref: Optional[Any] = None,
    strict_route: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Search PeMS detector stations along a route corridor bounded by two coordinates."""
    if start_coord is None and end_coord is None and route is not None and parse_coordinate_string(route) is not None:
        start_coord, route = route, None  # type: ignore[assignment]
    start_coord = kwargs.get("start_coord", start_coord)
    end_coord = kwargs.get("end_coord", end_coord)
    route = kwargs.get("route", route)

    p_start = parse_coordinate_string(start_coord) if isinstance(start_coord, str) else start_coord
    p_end = parse_coordinate_string(end_coord) if isinstance(end_coord, str) else end_coord
    if not p_start or not p_end:
        return {
            "error": (
                f"Invalid coordinate format. Start: '{start_coord}', End: '{end_coord}'. "
                "Expected decimal degrees or DMS (e.g. '38.186, -122.601' or '38°11\\'10\"N 122°36\\'05\"W')."
            )
        }

    ref = coerce_geo_ref(idx_ref)
    auto_detected_route = False
    route_mismatch_notice = None

    if route is None or str(route).strip() == "":
        route_norm, _, _, _, _ = ref.auto_detect_highway_corridor(p_start, p_end)
        auto_detected_route = True
    else:
        route_norm = normalize_route(route)
        sub_route_check = _get_route_df(ref, route_norm)
        if sub_route_check.empty:
            return {"error": f"Route '{route_norm}' not found in California highway reference database."}
        s_off_spec = float(
            haversine_distance_miles(
                p_start[0], p_start[1], ref.lats[sub_route_check.index], ref.lngs[sub_route_check.index]
            ).min()
        )
        e_off_spec = float(
            haversine_distance_miles(
                p_end[0], p_end[1], ref.lats[sub_route_check.index], ref.lngs[sub_route_check.index]
            ).min()
        )
        if not strict_route and (s_off_spec > 0.5 or e_off_spec > 0.5):
            act_route, s_cand_row, _, act_s_dist, _ = ref.auto_detect_highway_corridor(p_start, p_end)
            if act_s_dist < 0.25 and s_off_spec > (act_s_dist + 0.5):
                route_mismatch_notice = (
                    f"Specified Route {route_norm} is {s_off_spec:.2f} mi from input coordinates, "
                    f"but centerline is on Route {act_route} ({s_cand_row.get('County', '')} Co, "
                    f"{act_s_dist:.3f} mi away). Auto-switching corridor search to Route {act_route} "
                    f"(pass --strict-route to disable)."
                )
                route_norm = act_route

    dir_norm, dir_mismatch_notice, valid_dirs = _validate_route_direction(ref, route_norm, direction)

    sub_route = _get_route_df(ref, route_norm).copy()
    if dir_norm:
        sub_dir = sub_route[sub_route["_dir"] == dir_norm]
        if not sub_dir.empty:
            sub_route = sub_dir
    sub_route = sub_route.dropna(subset=["_lat", "_lng", "_abs_pm"])
    if sub_route.empty:
        return {"error": f"No valid coordinates found for Route {route_norm} in reference database."}

    d_start = haversine_distance_miles(p_start[0], p_start[1], sub_route["_lat"], sub_route["_lng"])
    d_end = haversine_distance_miles(p_end[0], p_end[1], sub_route["_lat"], sub_route["_lng"])
    snap_start = sub_route.loc[d_start.idxmin()]
    snap_end = sub_route.loc[d_end.idxmin()]

    abs_b = float(snap_start["_abs_pm"])
    abs_e = float(snap_end["_abs_pm"])
    min_abs, max_abs = min(abs_b, abs_e), max(abs_b, abs_e)
    length_mi = round(max_abs - min_abs, 2)

    if valid_dirs.issubset({"E", "W"}):
        travel_vector = "EB" if abs_b <= abs_e else "WB"
    elif valid_dirs.issubset({"N", "S"}):
        travel_vector = "NB" if abs_b <= abs_e else "SB"
    elif abs(p_end[0] - p_start[0]) > abs(p_end[1] - p_start[1]):
        travel_vector = "NB" if p_end[0] >= p_start[0] else "SB"
    else:
        travel_vector = "EB" if p_end[1] >= p_start[1] else "WB"

    meta = vds_meta if vds_meta is not None else load_vds_metadata(metadata_dir)
    unique_stations = _filter_corridor_vds_stations(
        meta, route_norm, dir_norm, min_abs, max_abs, buffer_mi, station_types, sub_route=sub_route
    )

    snap_start_dist = normalize_district(snap_start.get("District", "")) if "District" in snap_start else ""
    snap_end_dist = normalize_district(snap_end.get("District", "")) if "District" in snap_end else ""
    if not snap_start_dist and unique_stations:
        snap_start_dist = str(unique_stations[0].district or "")
    if not snap_end_dist and unique_stations:
        snap_end_dist = str(unique_stations[-1].district or "")

    start_county = str(snap_start.get("County", snap_start.get("_county", "")))
    hpms_summary = fetch_hpms_corridor_summary(route_norm, min_abs, max_abs, county=start_county)

    diag: Dict[str, Any] = {
        "coverage_status": "INSTRUMENTED",
        "coverage_details": None,
        "nearest_station": None,
        "nearest_station_dist_mi": None,
        "census_stations": [],
    }
    if not unique_stations:
        diag = diagnose_uninstrumented_corridor(
            route_norm, min_abs, max_abs, start_county, snap_start["_ca_num"], snap_start.get("District", "4"), meta
        )

    return {
        "route": route_norm,
        "direction": dir_norm,
        "travel_vector": travel_vector,
        "auto_detected_route": auto_detected_route,
        "route_mismatch_notice": route_mismatch_notice,
        "dir_mismatch_notice": dir_mismatch_notice,
        "input_start": p_start,
        "input_end": p_end,
        "snapped_start": {
            "abs_pm": abs_b,
            "ca_pm": str(snap_start.get("CA PM", snap_start.get("_ca_num", ""))),
            "county": start_county,
            "district": snap_start_dist or None,
            "coord": (float(snap_start["_lat"]), float(snap_start["_lng"])),
            "distance_from_route_mi": float(d_start.min()),
        },
        "snapped_end": {
            "abs_pm": abs_e,
            "ca_pm": str(snap_end.get("CA PM", snap_end.get("_ca_num", ""))),
            "county": str(snap_end.get("County", snap_end.get("_county", ""))),
            "district": snap_end_dist or None,
            "coord": (float(snap_end["_lat"]), float(snap_end["_lng"])),
            "distance_from_route_mi": float(d_end.min()),
        },
        "corridor_range": {"abs_min": min_abs, "abs_max": max_abs, "length_mi": length_mi},
        "stations": unique_stations,
        "hpms_summary": hpms_summary,
        "buffer_mi": buffer_mi,
        **diag,
    }


def find_stations_by_postmile_range(
    route: Union[str, int],
    begin_pm: Union[str, float, int],
    end_pm: Union[str, float, int],
    county: Optional[str] = None,
    begin_county: Optional[str] = None,
    end_county: Optional[str] = None,
    direction: Optional[str] = None,
    is_absolute: bool = False,
    buffer_mi: float = 0.1,
    station_types: Optional[Tuple[str, ...]] = ("ML", "HV"),
    metadata_dir: Optional[str] = None,
    idx_ref: Optional[Any] = None,
    vds_meta: Optional[Any] = None,
) -> Dict[str, Any]:
    """Search PeMS detector stations along a California Route postmile corridor."""
    b_co = begin_county or county
    e_co = end_county or county
    ref = coerce_geo_ref(idx_ref)

    if is_absolute or (not b_co and not e_co):
        start_info = convert_absolute_postmile(route, begin_pm, direction=direction, idx_ref=ref)
        if "error" in start_info:
            return start_info
        end_info = convert_absolute_postmile(route, end_pm, direction=direction, idx_ref=ref)
        if "error" in end_info:
            return end_info
        b_co, e_co = start_info["county"], end_info["county"]
    else:
        start_info = convert_california_postmile(route, b_co or e_co, begin_pm, direction=direction, idx_ref=ref)  # type: ignore[arg-type]
        if "error" in start_info:
            return start_info
        end_info = convert_california_postmile(route, e_co or b_co, end_pm, direction=direction, idx_ref=ref)  # type: ignore[arg-type]
        if "error" in end_info:
            return end_info

    route_norm = normalize_route(route)
    dir_norm, dir_mismatch_notice, _ = _validate_route_direction(ref, route_norm, direction)

    abs_b, abs_e = float(start_info["abs_pm"]), float(end_info["abs_pm"])
    min_abs, max_abs = min(abs_b, abs_e), max(abs_b, abs_e)
    length_mi = round(max_abs - min_abs, 2)

    meta = vds_meta if vds_meta is not None else load_vds_metadata(metadata_dir)
    unique_stations = _filter_corridor_vds_stations(
        meta, route_norm, dir_norm, min_abs, max_abs, buffer_mi, station_types
    )

    co_display = b_co if b_co == e_co else f"{b_co} to {e_co}"
    hpms_summary = fetch_hpms_corridor_summary(route_norm, min_abs, max_abs, county=b_co)

    diag: Dict[str, Any] = {
        "coverage_status": "INSTRUMENTED",
        "coverage_details": None,
        "nearest_station": None,
        "nearest_station_dist_mi": None,
        "census_stations": [],
    }
    if not unique_stations:
        _, b_num = split_ca_pm(begin_pm)
        dist_val = str(start_info.get("district") or end_info.get("district") or "4")
        diag = diagnose_uninstrumented_corridor(route_norm, min_abs, max_abs, str(b_co or ""), b_num, dist_val, meta)

    return {
        "route": route_norm,
        "direction": dir_norm,
        "dir_mismatch_notice": dir_mismatch_notice,
        "county": co_display,
        "begin_county": b_co,
        "end_county": e_co,
        "begin_pm": str(begin_pm),
        "end_pm": str(end_pm),
        "start": start_info,
        "end": end_info,
        "corridor_range": {"abs_min": min_abs, "abs_max": max_abs, "length_mi": length_mi},
        "stations": unique_stations,
        "hpms_summary": hpms_summary,
        "buffer_mi": buffer_mi,
        **diag,
    }
