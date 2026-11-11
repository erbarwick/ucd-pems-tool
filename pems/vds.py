"""VDS station metadata indexing, corridor matching, spreadsheet comparison, and diagnostics."""

from __future__ import annotations

import glob
import os
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from pems.client import PeMSClient
from pems.config import BASE_URL, DEFAULT_MASTER_CSV, DEFAULT_METADATA_DIR, LOGGER
from pems.geo import find_closest_census_stations
from pems.normalize import (
    find_column,
    normalize_ca_county,
    normalize_direction,
    normalize_district,
    normalize_route,
    parse_excel_serial_date,
    split_ca_pm,
)
from pems.projects import ProjectRecord, load_master_csv, save_master_csv_with_backup
from pems.reporting import write_dataframe_report


@dataclass
class VDSStationRecord:
    """Structured representation of a single Caltrans PeMS VDS detector station."""

    id: str
    fwy: str
    dir: str
    district: str
    county: str
    city: str
    state_pm: str
    abs_pm: Optional[float]
    latitude: Optional[float]
    longitude: Optional[float]
    length: Optional[float]
    type: str
    lanes: Optional[int]
    name: str

    @property
    def ca_pm_prefix_and_num(self) -> Tuple[str, float]:
        return split_ca_pm(self.state_pm)


class IndexedVDSMetadata:
    """O(1) Indexed PeMS VDS detector metadata partitioned by (district, route, direction)."""

    def __init__(self, df: pd.DataFrame):
        self.raw_df = df
        clean = df.copy()
        clean.columns = clean.columns.str.strip()

        clean["_route"] = clean["Fwy"].astype(str).str.strip().map(normalize_route)
        clean["_dir"] = clean["Dir"].astype(str).str.strip().map(normalize_direction)
        clean["_dist"] = clean["District"].astype(str).str.strip().map(normalize_district)
        clean["_abs_pm"] = pd.to_numeric(clean["Abs_PM"], errors="coerce")
        ca_splits = clean["State_PM"].astype(str).map(split_ca_pm)
        clean["_ca_num"] = ca_splits.map(lambda x: x[1])
        clean["_ca_pfx"] = ca_splits.map(lambda x: x[0].upper())
        clean["_county"] = clean["County"].astype(str).str.strip().str.lower()
        clean["_lat"] = pd.to_numeric(clean["Latitude"], errors="coerce")
        clean["_lng"] = pd.to_numeric(clean["Longitude"], errors="coerce")
        clean["_lanes"] = pd.to_numeric(clean["Lanes"], errors="coerce")
        clean["_length"] = pd.to_numeric(clean["Length"], errors="coerce")
        clean["_id"] = clean["ID"].astype(str).str.strip()
        clean["_type"] = clean["Type"].astype(str).str.strip().str.upper()

        self.clean_df = clean
        self.index: Dict[Tuple[str, str, str], pd.DataFrame] = {}
        for (dist, r, d), grp in clean.groupby(["_dist", "_route", "_dir"], sort=False):
            self.index[(dist, r, d)] = grp

        self.by_id: Dict[str, VDSStationRecord] = {}
        for _, r in clean.iterrows():
            sid = r["_id"]
            if sid:
                self.by_id[sid] = VDSStationRecord(
                    id=sid,
                    fwy=r["_route"],
                    dir=r["_dir"],
                    district=r["_dist"],
                    county=str(r.get("County", "")).strip(),
                    city=str(r.get("City", "")).strip(),
                    state_pm=str(r.get("State_PM", "")).strip(),
                    abs_pm=float(r["_abs_pm"]) if pd.notna(r["_abs_pm"]) else None,
                    latitude=float(r["_lat"]) if pd.notna(r["_lat"]) else None,
                    longitude=float(r["_lng"]) if pd.notna(r["_lng"]) else None,
                    length=float(r["_length"]) if pd.notna(r["_length"]) else None,
                    type=r["_type"],
                    lanes=int(r["_lanes"]) if pd.notna(r["_lanes"]) else None,
                    name=str(r.get("Name", "")).strip(),
                )

    def find_corridor_stations(
        self,
        district: Any,
        route: str,
        direction: str,
        begin_pm: Any,
        end_pm: Any,
        abs_begin_pm: Optional[float] = None,
        abs_end_pm: Optional[float] = None,
        buffer_mi: float = 0.1,
        station_types: Tuple[str, ...] = ("ML", "HV"),
        county: Optional[str] = None,
    ) -> List[VDSStationRecord]:
        """Locate active PeMS VDS stations along a freeway corridor within the postmile range."""
        dist_clean = normalize_district(district)
        r_norm = normalize_route(route)
        d_norm = normalize_direction(direction)

        sub = self.index.get((dist_clean, r_norm, d_norm))
        if sub is None or sub.empty:
            return []

        if station_types and "ALL" not in [t.upper().strip() for t in station_types]:
            types_upper = {t.upper().strip() for t in station_types}
            sub = sub[sub["_type"].isin(types_upper)]
            if sub.empty:
                return []

        _, b_num = split_ca_pm(begin_pm)
        _, e_num = split_ca_pm(end_pm)
        has_ca_bounds = not np.isnan(b_num) and not np.isnan(e_num)
        has_abs_inputs = (
            abs_begin_pm is not None
            and abs_end_pm is not None
            and not np.isnan(abs_begin_pm)
            and not np.isnan(abs_end_pm)
        )
        abs_is_inverted = has_abs_inputs and float(abs_begin_pm) > float(abs_end_pm)  # type: ignore[arg-type]
        if abs_is_inverted and has_ca_bounds:
            LOGGER.warning(
                "Backwards absolute postmiles (%s > %s) on Route %s %s; falling back to California postmiles (%s -> %s).",
                abs_begin_pm,
                abs_end_pm,
                route,
                direction,
                begin_pm,
                end_pm,
            )
        use_abs = has_abs_inputs and not (abs_is_inverted and has_ca_bounds)
        if use_abs:
            min_pm = min(float(abs_begin_pm), float(abs_end_pm)) - buffer_mi
            max_pm = max(float(abs_begin_pm), float(abs_end_pm)) + buffer_mi
            matched = sub[(sub["_abs_pm"] >= min_pm) & (sub["_abs_pm"] <= max_pm)].sort_values(by="_abs_pm")
        else:
            if not has_ca_bounds:
                return []
            min_pm = min(b_num, e_num) - buffer_mi
            max_pm = max(b_num, e_num) + buffer_mi
            if county:
                county_clean = str(county).strip().lower()
                c_sub = sub[sub["_county"] == county_clean]
                if not c_sub.empty:
                    sub = c_sub
            matched = sub[(sub["_ca_num"] >= min_pm) & (sub["_ca_num"] <= max_pm)].sort_values(by="_ca_num")

        return [self.by_id[row["_id"]] for _, row in matched.iterrows() if row["_id"] in self.by_id]


_VDS_META_CACHE: Dict[str, IndexedVDSMetadata] = {}


def reset_vds_cache() -> None:
    """Clear cached statewide VDS metadata tables."""
    _VDS_META_CACHE.clear()


def download_all_district_metadata(
    client: PeMSClient,
    target_dir: str = DEFAULT_METADATA_DIR,
    districts: Tuple[str, ...] = ("3", "4", "5", "6", "7", "8", "10", "11", "12"),
    force: bool = False,
) -> Dict[str, str]:
    """Download and cache latest station metadata files for instrumented Caltrans districts."""
    os.makedirs(target_dir, exist_ok=True)
    if not client.logged_in:
        client.login()

    import datetime

    downloaded: Dict[str, str] = {}
    probe_years = tuple(range(datetime.datetime.now().year, 2019, -1))

    for d in districts:
        cache_file = os.path.join(target_dir, f"d{int(d):02d}_meta.txt")
        if not force and os.path.exists(cache_file) and os.path.getsize(cache_file) > 1000:
            downloaded[d] = cache_file
            continue

        found = False
        for y in probe_years:
            url = f"{BASE_URL}/?srq=clearinghouse&district_id={d}&yy={y}&type=metadata&returnformat=text"
            try:
                data = client.get_url(url).json()
                if isinstance(data, dict) and data.get("data"):
                    latest_key = sorted(data["data"].keys())[-1]
                    dl_url = f"{BASE_URL}{data['data'][latest_key]['format']['text']['url']}"
                    if client.download_file(dl_url, cache_file, overwrite=True) and os.path.getsize(cache_file) > 1000:
                        downloaded[d] = cache_file
                        found = True
                        break
            except Exception as exc:
                LOGGER.debug(f"Dist {d} year {y} clearinghouse metadata probe: {exc}")
        if not found:
            LOGGER.warning(f"Could not find valid metadata file for District {d}.")

    return downloaded


def load_vds_metadata(
    meta_dir: Optional[str] = None,
    client: Optional[PeMSClient] = None,
    auto_download: bool = False,
    refresh: bool = False,
) -> IndexedVDSMetadata:
    """Load and index statewide VDS detector metadata cached per directory path."""
    target_dir = os.path.abspath(meta_dir or DEFAULT_METADATA_DIR)
    if not refresh and target_dir in _VDS_META_CACHE:
        return _VDS_META_CACHE[target_dir]

    meta_files = sorted(glob.glob(os.path.join(target_dir, "*.txt")))
    if ((not meta_files and auto_download) or refresh) and client is not None:
        download_all_district_metadata(client, target_dir, force=refresh)
        meta_files = sorted(glob.glob(os.path.join(target_dir, "*.txt")))

    if not meta_files:
        raise FileNotFoundError(f"No PeMS metadata files (*.txt) found in: {target_dir}")

    dfs = []
    for f in meta_files:
        try:
            df = pd.read_csv(f, sep="\t", dtype=str, on_bad_lines="skip")
            df.columns = df.columns.str.strip()
            if "ID" in df.columns and "Fwy" in df.columns:
                dfs.append(df)
        except Exception as exc:
            LOGGER.warning(f"Could not parse metadata file {f}: {exc}")

    if not dfs:
        raise ValueError(f"No valid metadata tables could be parsed from {target_dir}")

    indexed = IndexedVDSMetadata(pd.concat(dfs, ignore_index=True))
    _VDS_META_CACHE[target_dir] = indexed
    return indexed


def get_indexed_vds_metadata(refresh: bool = False, meta_dir: Optional[str] = None) -> IndexedVDSMetadata:
    """Return cached IndexedVDSMetadata for meta_dir (default metadata directory if omitted)."""
    return load_vds_metadata(meta_dir=meta_dir, refresh=refresh)


def diagnose_uninstrumented_corridor(
    route_norm: str,
    min_abs: float,
    max_abs: float,
    county: str,
    ca_pm_num: float,
    district: Any,
    vds_meta: Optional[IndexedVDSMetadata],
) -> Dict[str, Any]:
    """Build uninstrumented corridor diagnostics, nearest active VDS station, and Census fallback."""
    all_route_stations = (
        [s for s in vds_meta.by_id.values() if str(s.fwy) == str(route_norm)]
        if vds_meta is not None
        else []
    )
    nearest_station = None
    nearest_station_dist_mi = None

    if not all_route_stations:
        coverage_status = "UNINSTRUMENTED STATEWIDE"
        coverage_details = f"Route {route_norm} has no active PeMS detector stations instrumented statewide."
    else:
        coverage_status = "UNINSTRUMENTED CORRIDOR SEGMENT"
        st_counties = ", ".join(sorted(set(normalize_ca_county(s.county) for s in all_route_stations if s.county)))
        nst = min(all_route_stations, key=lambda s: abs((s.abs_pm if s.abs_pm is not None else 9999.0) - min_abs))
        nearest_station = nst
        if nst.abs_pm is not None:
            nearest_station_dist_mi = abs(nst.abs_pm - min_abs)
            coverage_details = (
                f"Route {route_norm} has {len(all_route_stations)} active PeMS stations located in {st_counties} "
                f"County/Counties. The {county} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f}) "
                f"is not instrumented with PeMS mainline loop detectors."
            )
        else:
            coverage_details = (
                f"Route {route_norm} has {len(all_route_stations)} active PeMS stations in {st_counties} "
                f"County/Counties, but none in the {county} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f})."
            )

    census_stations: List[Dict[str, Any]] = []
    try:
        dist_val = normalize_district(district) or "4"
        pm_q = float(ca_pm_num) if ca_pm_num is not None and not np.isnan(ca_pm_num) else 0.0
        raw_census = find_closest_census_stations(route_norm, dist_val, pm_q, county=county, top_n=3)
        if not raw_census.empty:
            for _, crow in raw_census.iterrows():
                abs_str = "-"
                if pd.notna(crow.get("Abs PM")):
                    try:
                        abs_str = f"{float(crow['Abs PM']):.2f}"
                    except (TypeError, ValueError):
                        abs_str = str(crow["Abs PM"])
                census_stations.append({
                    "ID": str(crow["ID"]),
                    "Name": str(crow["Name"]),
                    "CA PM": str(crow["CA PM"]),
                    "Abs PM": abs_str,
                    "pm_diff": float(crow["pm_diff"]) if pd.notna(crow.get("pm_diff")) else 0.0,
                })
    except Exception:
        pass

    return {
        "coverage_status": coverage_status,
        "coverage_details": coverage_details,
        "nearest_station": nearest_station,
        "nearest_station_dist_mi": nearest_station_dist_mi,
        "census_stations": census_stations,
    }


@dataclass
class StationComparisonResult:
    """Detailed result of comparing a project's spreadsheet sensors with PeMS corridor sensors."""

    project_id: str
    route: str
    direction: str
    district: str
    county: str
    begin_pm: str
    end_pm: str
    abs_begin_pm: Optional[float]
    abs_end_pm: Optional[float]
    existing_stations: List[str]
    found_stations: List[VDSStationRecord]
    matched_stations: List[str]
    missing_in_sheet: List[str]
    discrepancies: List[Dict[str, Any]]
    status: str
    sheet_sensor_count: int
    found_sensor_count: int
    project_description: str = ""


def compare_project_stations(
    project: ProjectRecord,
    vds_meta: Optional[IndexedVDSMetadata] = None,
    buffer_mi: float = 0.1,
    station_types: Tuple[str, ...] = ("ML", "HV"),
) -> StationComparisonResult:
    """Compare spreadsheet sensors with active PeMS corridor detectors and diagnose discrepancies."""
    meta = vds_meta or get_indexed_vds_metadata()
    eff_abs_b, eff_abs_e = project.effective_abs_pm_bounds
    found = meta.find_corridor_stations(
        district=project.dist,
        route=project.route,
        direction=project.direction,
        begin_pm=project.begin_pm,
        end_pm=project.end_pm,
        abs_begin_pm=eff_abs_b,
        abs_end_pm=eff_abs_e,
        buffer_mi=buffer_mi,
        station_types=station_types,
        county=project.county,
    )
    found_ids = [s.id for s in found]
    existing_ids = list(project.sensor_stations)

    matched = [s for s in existing_ids if s in found_ids]
    missing_in_sheet = [s for s in found_ids if s not in existing_ids]
    not_found = [s for s in existing_ids if s not in found_ids]

    discrepancies: List[Dict[str, Any]] = []
    p_dir_norm = project.pems_direction
    p_route_norm = normalize_route(project.route)
    p_dist_norm = normalize_district(project.dist)
    type_filter = (
        {t.upper().strip() for t in station_types}
        if station_types and "ALL" not in [t.upper().strip() for t in station_types]
        else None
    )

    for sid in not_found:
        if sid not in meta.by_id:
            discrepancies.append({
                "station_id": sid,
                "found_in_pems": False,
                "pems_record": None,
                "reasons": ["Invalid or obsolete PeMS VDS ID (not found in current Caltrans metadata)"],
            })
            continue

        st = meta.by_id[sid]
        reasons: List[str] = []
        if st.district != p_dist_norm:
            reasons.append(f"Different District (station is Dist {st.district}, project is Dist {p_dist_norm})")
        if st.fwy != p_route_norm:
            reasons.append(f"Different Route (station is Route {st.fwy}, project is Route {p_route_norm})")
        if st.dir != p_dir_norm:
            reasons.append(f"Opposite/Different Direction (station is {st.dir}, project is {p_dir_norm})")
        if project.county and st.county and st.county.lower() != project.county.lower():
            reasons.append(f"Different County (station is {st.county}, project is {project.county})")
        if type_filter and st.type not in type_filter:
            reasons.append(f"Filtered Station Type: {st.type} (filter: {','.join(station_types)})")

        if project.has_abs_pm_bounds and st.abs_pm is not None and not np.isnan(st.abs_pm):
            min_abs = min(float(project.abs_begin_pm), float(project.abs_end_pm))  # type: ignore[arg-type]
            max_abs = max(float(project.abs_begin_pm), float(project.abs_end_pm))  # type: ignore[arg-type]
            if st.abs_pm < min_abs:
                reasons.append(
                    f"Out of range: Abs PM {st.abs_pm:.3f} is {min_abs - st.abs_pm:.2f} mi before corridor start ({min_abs:.3f})"
                )
            elif st.abs_pm > max_abs:
                reasons.append(
                    f"Out of range: Abs PM {st.abs_pm:.3f} is {st.abs_pm - max_abs:.2f} mi beyond corridor end ({max_abs:.3f})"
                )
        else:
            _, b_num = split_ca_pm(project.begin_pm)
            _, e_num = split_ca_pm(project.end_pm)
            _, st_num = st.ca_pm_prefix_and_num
            if not np.isnan(b_num) and not np.isnan(e_num) and not np.isnan(st_num):
                min_ca, max_ca = min(b_num, e_num), max(b_num, e_num)
                if st_num < min_ca:
                    reasons.append(
                        f"Out of range: State PM {st_num:.3f} is {min_ca - st_num:.2f} mi before corridor start ({min_ca:.3f})"
                    )
                elif st_num > max_ca:
                    reasons.append(
                        f"Out of range: State PM {st_num:.3f} is {st_num - max_ca:.2f} mi beyond corridor end ({max_ca:.3f})"
                    )

        discrepancies.append({
            "station_id": sid,
            "found_in_pems": True,
            "pems_record": st,
            "reasons": reasons or ["Outside corridor postmile buffer tolerance"],
        })

    if not existing_ids and not found_ids:
        status = "UNINSTRUMENTED"
    elif set(existing_ids) == set(found_ids) and existing_ids:
        status = "EXACT_MATCH"
    elif len(matched) == len(existing_ids) and existing_ids and missing_in_sheet:
        status = "PARTIAL_MATCH"
    elif not existing_ids and found_ids:
        status = "SHEET_EMPTY"
    else:
        status = "DISCREPANCY"

    sheet_count_raw = project.raw_row.get("Number of Active Non-Ramp Sensors")
    try:
        sheet_count = (
            int(float(sheet_count_raw))
            if pd.notna(sheet_count_raw) and str(sheet_count_raw).strip() != ""
            else len(existing_ids)
        )
    except (ValueError, TypeError):
        sheet_count = len(existing_ids)

    return StationComparisonResult(
        project_id=project.project_id,
        route=project.route,
        direction=project.direction,
        district=project.dist,
        county=project.county,
        begin_pm=project.begin_pm,
        end_pm=project.end_pm,
        abs_begin_pm=project.abs_begin_pm,
        abs_end_pm=project.abs_end_pm,
        existing_stations=existing_ids,
        found_stations=found,
        matched_stations=matched,
        missing_in_sheet=missing_in_sheet,
        discrepancies=discrepancies,
        status=status,
        sheet_sensor_count=sheet_count,
        found_sensor_count=len(found),
        project_description=project.project_description,
    )


def apply_station_updates_to_dataframe(
    df: pd.DataFrame,
    comparison_results: List[StationComparisonResult],
) -> pd.DataFrame:
    """Apply identified corridor stations to master dataframe columns and normalize Excel dates."""
    updated = df.copy()
    col_sensor_count = find_column(updated, "Number of Active Non-Ramp Sensors")
    col_s1_hov = find_column(updated, "Sensor Station 1 HOV ID")
    col_pid = find_column(updated, "Project ID")
    col_dir = find_column(updated, "Direction")

    slot_cols = {}
    for k in range(1, 5):
        slot_cols[k] = {
            "id": find_column(updated, f"Sensor Station {k} ID"),
            "name": find_column(updated, f"Sensor Station {k} Name") if k == 1 else None,
            "type": find_column(updated, f"Sensor Station {k} Type") if k == 1 else None,
            "lanes_after": find_column(updated, f"Sensor Station {k}: number of lanes after"),
            "clear_only": [
                find_column(updated, f"Sensor Station {k}: number of lanes before"),
                find_column(updated, f"Sensor Station {k}: lane type"),
                find_column(updated, f"Sensor Station {k}: lane width (ft)"),
                find_column(updated, f"Sensor {k} Activation Date for New Lane"),
            ]
            if k >= 2
            else [],
        }

    for res in comparison_results:
        mask = updated[col_pid].astype(str).str.strip() == str(res.project_id).strip()
        if col_dir and res.direction:
            mask = mask & (updated[col_dir].astype(str).str.strip().str.upper() == res.direction.strip().upper())

        matching_indices = updated[mask].index
        if matching_indices.empty:
            continue

        ml_stations = [s for s in res.found_stations if s.type != "HV"]
        hov_stations = [s for s in res.found_stations if s.type == "HV"]
        primary_stations = ml_stations if ml_stations else hov_stations

        for idx in matching_indices:
            if col_sensor_count:
                updated.at[idx, col_sensor_count] = str(len(res.found_stations))
            if col_s1_hov:
                updated.at[idx, col_s1_hov] = str(hov_stations[0].id) if hov_stations else ""

            for k in range(1, 5):
                scols = slot_cols[k]
                if len(primary_stations) >= k:
                    st = primary_stations[k - 1]
                    if scols["id"]:
                        updated.at[idx, scols["id"]] = str(st.id)
                    if scols["type"]:
                        updated.at[idx, scols["type"]] = str(st.type)
                    if scols["name"]:
                        updated.at[idx, scols["name"]] = str(st.name)
                    if scols["lanes_after"]:
                        updated.at[idx, scols["lanes_after"]] = str(st.lanes) if st.lanes is not None else ""
                else:
                    for key in ("id", "type", "name", "lanes_after"):
                        if scols[key]:
                            updated.at[idx, scols[key]] = ""
                    for clr_col in scols["clear_only"]:
                        if clr_col:
                            updated.at[idx, clr_col] = ""

    for s_idx in range(1, 5):
        col_act = find_column(updated, f"Sensor {s_idx} Activation Date for New Lane")
        if col_act:
            for idx in updated.index:
                formatted = parse_excel_serial_date(updated.at[idx, col_act])
                if formatted is not None:
                    updated.at[idx, col_act] = formatted

    return updated


def run_station_comparison_pipeline(
    projects: Optional[List[ProjectRecord]] = None,
    master_csv_path: Optional[str] = None,
    meta_dir: Optional[str] = None,
    dry_run: bool = True,
    apply_updates: bool = False,
    output_report_path: Optional[str] = None,
    output_csv_path: Optional[str] = None,
    station_types: Tuple[str, ...] = ("ML", "HV"),
    buffer_mi: float = 0.1,
    dist_filter: Optional[str] = None,
    project_id_filter: Optional[str] = None,
    verbose: bool = True,
    vds_meta: Optional[IndexedVDSMetadata] = None,
    _vds_loader: Optional[Callable[[Optional[str]], IndexedVDSMetadata]] = None,
) -> List[StationComparisonResult]:
    """Execute end-to-end station comparison pipeline (dry-run by default)."""
    csv_file = master_csv_path or DEFAULT_MASTER_CSV
    if projects is None:
        if not os.path.exists(csv_file):
            raise FileNotFoundError(f"Master CSV file not found: {csv_file}")
        projects = load_master_csv(csv_file)

    target_projects = list(projects)
    if dist_filter:
        d_clean = normalize_district(dist_filter)
        target_projects = [p for p in target_projects if normalize_district(p.dist) == d_clean]
    if project_id_filter:
        pid_clean = str(project_id_filter).strip()
        target_projects = [p for p in target_projects if p.project_id == pid_clean]

    if not target_projects:
        LOGGER.warning("No projects matched the specified filters.")
        return []

    loader = _vds_loader or load_vds_metadata
    loaded_meta = vds_meta if vds_meta is not None else loader(meta_dir)

    results = [
        compare_project_stations(p, vds_meta=loaded_meta, buffer_mi=buffer_mi, station_types=station_types)
        for p in target_projects
    ]

    status_counts = {k: 0 for k in ("EXACT_MATCH", "PARTIAL_MATCH", "DISCREPANCY", "SHEET_EMPTY", "UNINSTRUMENTED")}
    total_sheet_sensors = 0
    total_found_sensors = 0
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
        total_sheet_sensors += len(r.existing_stations)
        total_found_sensors += len(r.found_stations)

    total_projs = len(results)
    mode_str = "DRY RUN (Read-Only) [Pass --apply to commit updates]" if dry_run or not apply_updates else "APPLY UPDATES (Active Write)"
    print("\n" + "=" * 80)
    print("PEMS VDS CORRIDOR SENSOR COMPARISON PIPELINE")
    print(f"Mode: {mode_str}")
    print(f"Buffer Tolerance: ±{buffer_mi:.2f} mi | Station Types: {','.join(station_types)}")
    if dist_filter:
        print(f"District Filter: District {dist_filter}")
    if project_id_filter:
        print(f"Project ID Filter: {project_id_filter}")
        if target_projects and target_projects[0].project_description:
            print(textwrap.fill(target_projects[0].project_description, width=76, initial_indent="Project Description: ", subsequent_indent="                     "))
    print("=" * 80)
    print(f"Evaluated Projects: {total_projs}")
    print(f"  • Exact Matches (sheet == corridor):         {status_counts['EXACT_MATCH']:3d} ({status_counts['EXACT_MATCH']/total_projs*100:5.1f}%)")
    print(f"  • Partial Matches (all sheet in corridor +): {status_counts['PARTIAL_MATCH']:3d} ({status_counts['PARTIAL_MATCH']/total_projs*100:5.1f}%)")
    print(f"  • Discrepancies (out of range/wrong dir):    {status_counts['DISCREPANCY']:3d} ({status_counts['DISCREPANCY']/total_projs*100:5.1f}%)")
    print(f"  • Sheet Empty (corridor sensors available):   {status_counts['SHEET_EMPTY']:3d} ({status_counts['SHEET_EMPTY']/total_projs*100:5.1f}%)")
    print(f"  • Uninstrumented / Rural (no detectors):     {status_counts['UNINSTRUMENTED']:3d} ({status_counts['UNINSTRUMENTED']/total_projs*100:5.1f}%)")
    print("\nSensor Counts:")
    print(f"  • Existing Sensors in Spreadsheet: {total_sheet_sensors:4d}")
    print(f"  • Active Corridor Sensors Found:   {total_found_sensors:4d} (+{total_found_sensors - total_sheet_sensors:d} net detectors)")
    print("=" * 80)

    if verbose:
        display_results = results if len(results) <= 30 else results[:25]
        print("\nPROJECT-BY-PROJECT COMPARISON:")
        print(f"{'PID':<11} {'Dist':<4} {'Route':<5} {'Dir':<3} {'PM Span':<14} {'Sheet':<5} {'Found':<5} {'Status':<15} {'Notes / Discrepancy'}")
        print("-" * 105)
        for r in display_results:
            notes = ""
            if r.status == "EXACT_MATCH":
                notes = f"All {len(r.matched_stations)} stations verified in range"
            elif r.status == "PARTIAL_MATCH":
                notes = f"Verified {len(r.matched_stations)} in range; +{len(r.missing_in_sheet)} more in PeMS"
            elif r.status == "SHEET_EMPTY":
                notes = f"Sheet has 0; PeMS has {len(r.found_stations)} active ({', '.join(s.id for s in r.found_stations[:3])}...)"
            elif r.status == "UNINSTRUMENTED":
                notes = "No detectors in corridor"
            elif r.status == "DISCREPANCY":
                notes = "; ".join(f"Station {d['station_id']}: {d['reasons'][0]}" for d in r.discrepancies)
            if len(notes) > 42:
                notes = notes[:39] + "..."
            print(
                f"{r.project_id:<11} {r.district:<4} {r.route:<5} {r.direction:<3} "
                f"{r.begin_pm}-{r.end_pm:<8} {len(r.existing_stations):<5} {len(r.found_stations):<5} {r.status:<15} {notes}"
            )

        if len(results) > len(display_results):
            print(f"... and {len(results) - len(display_results)} more projects (export full details with --output-report)")

        discrepancies_list = [r for r in results if r.status == "DISCREPANCY"]
        if discrepancies_list:
            print(f"\nDETAILED DISCREPANCY AUDIT ({len(discrepancies_list)} projects):")
            for r in discrepancies_list:
                p_match = next((p for p in target_projects if p.project_id == r.project_id), None)
                print(f"\n  Project {r.project_id} (Dist {r.district}, Route {r.route} {r.direction}, PM {r.begin_pm}-{r.end_pm}):")
                if p_match and p_match.project_description:
                    print(textwrap.fill(p_match.project_description, width=74, initial_indent="    Description: ", subsequent_indent="                 "))
                print(f"    Sheet Stations: {r.existing_stations}")
                print(f"    Corridor Stations Found ({len(r.found_stations)}): {[s.id for s in r.found_stations]}")
                for d in r.discrepancies:
                    if d["found_in_pems"]:
                        st = d["pems_record"]
                        print(f"    ✗ Station {d['station_id']}: PeMS VDS Dist {st.district} Rte {st.fwy} {st.dir} (State PM {st.state_pm}, Abs PM {st.abs_pm}, Type {st.type}, '{st.name}')")
                    else:
                        print(f"    ✗ Station {d['station_id']}: Not found in statewide PeMS detector metadata")
                    for reas in d["reasons"]:
                        print(f"        -> Reason: {reas}")

    if output_report_path:
        report_data = []
        for r in results:
            disc_reasons = [f"{d['station_id']}: {'; '.join(d['reasons'])}" for d in r.discrepancies]
            report_data.append({
                "Project ID": r.project_id,
                "District": r.district,
                "County": r.county,
                "Route": r.route,
                "Direction": r.direction,
                "Begin PM": r.begin_pm,
                "End PM": r.end_pm,
                "Abs Begin PM": r.abs_begin_pm,
                "Abs End PM": r.abs_end_pm,
                "Project Description": r.project_description,
                "Status": r.status,
                "Sheet Sensor Count": r.sheet_sensor_count,
                "Found Sensor Count": r.found_sensor_count,
                "Existing Stations": ", ".join(r.existing_stations),
                "Found Stations": ", ".join(s.id for s in r.found_stations),
                "Matched Stations": ", ".join(r.matched_stations),
                "Missing in Sheet": ", ".join(r.missing_in_sheet),
                "Discrepant Stations": ", ".join(d["station_id"] for d in r.discrepancies),
                "Discrepancy Details": " | ".join(disc_reasons),
            })
        write_dataframe_report(
            pd.DataFrame(report_data),
            output_report_path,
            markdown_title="Caltrans PeMS Station Comparison Report",
            header_lines=[
                f"- **Evaluated Projects**: {total_projs}",
                f"- **Exact Matches**: {status_counts['EXACT_MATCH']}",
                f"- **Partial Matches**: {status_counts['PARTIAL_MATCH']}",
                f"- **Discrepancies**: {status_counts['DISCREPANCY']}",
                f"- **Sheet Empty**: {status_counts['SHEET_EMPTY']}",
                f"- **Uninstrumented**: {status_counts['UNINSTRUMENTED']}",
            ],
        )
        print(f"\n✓ Comparison report successfully written to: {output_report_path}")

    if apply_updates and not dry_run:
        raw_df = pd.read_csv(csv_file, dtype=str)
        updated_df = apply_station_updates_to_dataframe(raw_df, results)
        backup_path, target_out = save_master_csv_with_backup(
            csv_file, updated_df, output_csv_path=output_csv_path, use_iso_timestamp=False
        )
        print(f"\n✓ Successfully updated master CSV at: {target_out}")
        print(f"  Safety backup preserved at:       {backup_path}")
    elif dry_run:
        print("\n[DRY RUN COMPLETE]: No changes have been written to the spreadsheet.")
        print("To apply these corridor stations to the master CSV, run with: --match-stations --apply")

    return results
