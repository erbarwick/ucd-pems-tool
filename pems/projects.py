"""Project records, Master CSV loading/safe backup saving, geometry resolution, and query URL building."""

from __future__ import annotations

import datetime
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from pems.client import (
    build_fwy_latlng_url,
    build_lane_config_url,
    build_station_aadt_url,
    build_station_config_url,
    build_station_flow_url,
    build_station_summary_url,
)
from pems.config import DEFAULT_MASTER_CSV, LOGGER
from pems.geo import (
    IndexedGeoReference,
    coerce_geo_ref,
    get_corridor_stations,
    pm_to_coordinates,
    resolve_absolute_postmiles,
)
from pems.normalize import (
    assert_not_box_path,
    build_date_from_parts,
    format_pm,
    normalize_direction,
    normalize_district,
    parse_iso_date,
    split_ca_pm,
)


@dataclass
class ProjectRecord:
    """Structured representation of a highway capacity expansion project row."""

    project_id: str
    dist: str
    county: str
    state: str
    route: str
    direction: str
    begin_pm: str
    end_pm: str
    abs_begin_pm: Optional[float] = None
    abs_end_pm: Optional[float] = None
    start_coords: Optional[Tuple[float, float]] = None
    end_coords: Optional[Tuple[float, float]] = None
    construction_begin_date: Optional[str] = None
    open_to_traffic_date: Optional[str] = None
    sensor_stations: List[str] = field(default_factory=list)
    raw_row: Dict[str, Any] = field(default_factory=dict)
    row_index: int = -1
    project_description: str = ""

    @property
    def pems_direction(self) -> str:
        return normalize_direction(self.direction)

    @property
    def has_ca_pm_bounds(self) -> bool:
        """True when both begin_pm and end_pm parse to finite California postmiles."""
        _, b_num = split_ca_pm(self.begin_pm)
        _, e_num = split_ca_pm(self.end_pm)
        return not np.isnan(b_num) and not np.isnan(e_num)

    @property
    def has_inverted_abs_pm(self) -> bool:
        """True when both absolute postmiles are finite and ordered backwards (begin > end)."""
        return (
            self.abs_begin_pm is not None
            and self.abs_end_pm is not None
            and not np.isnan(self.abs_begin_pm)
            and not np.isnan(self.abs_end_pm)
            and float(self.abs_begin_pm) > float(self.abs_end_pm)
        )

    @property
    def has_abs_pm_bounds(self) -> bool:
        """True when both abs_begin_pm and abs_end_pm are finite and not inverted relative to CA PMs."""
        return (
            self.abs_begin_pm is not None
            and self.abs_end_pm is not None
            and not np.isnan(self.abs_begin_pm)
            and not np.isnan(self.abs_end_pm)
            and not (self.has_inverted_abs_pm and self.has_ca_pm_bounds)
        )

    @property
    def effective_abs_pm_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        """Return (abs_begin_pm, abs_end_pm) when usable, or (None, None) to use CA PM fallback."""
        if self.has_abs_pm_bounds:
            return float(self.abs_begin_pm), float(self.abs_end_pm)  # type: ignore[arg-type]
        return None, None

    @property
    def start_year(self) -> int:
        val = self.raw_row.get("construction_begin_year")
        if val is not None and pd.notna(val):
            try:
                y = int(float(val))
                if 1950 <= y <= 2040:
                    return y
            except (ValueError, TypeError):
                pass
        if self.construction_begin_date:
            try:
                return int(str(self.construction_begin_date)[:4])
            except (ValueError, TypeError):
                pass
        return 2013

    @property
    def end_year(self) -> int:
        max_year = max(2026, datetime.date.today().year)
        val = self.raw_row.get("open_traffic_year")
        if val is not None and pd.notna(val):
            try:
                y = int(float(val))
                if 1950 <= y <= 2040:
                    return min(y + 4, max_year)
            except (ValueError, TypeError):
                pass
        if self.open_to_traffic_date:
            try:
                return min(int(str(self.open_to_traffic_date)[:4]) + 4, max_year)
            except (ValueError, TypeError):
                pass
        return max_year


def resolve_project_dates(
    project: ProjectRecord,
    custom_before_date: Optional[str] = None,
    custom_after_date: Optional[str] = None,
) -> Tuple[Optional[datetime.date], Optional[datetime.date]]:
    """Resolve (before_date, after_date) from overrides, 3-column CSV parts, ISO strings, or year fallbacks."""
    raw = project.raw_row or {}
    d_before = (
        parse_iso_date(custom_before_date, 1, 1)
        or build_date_from_parts(
            raw.get("construction_begin_year"),
            raw.get("construction_begin_month"),
            raw.get("construction_begin_day"),
            default_month=1,
            default_day=1,
        )
        or parse_iso_date(project.construction_begin_date, 1, 1)
        or (datetime.date(project.start_year, 1, 1) if project.start_year else None)
    )
    d_after = (
        parse_iso_date(custom_after_date, 12, 31)
        or build_date_from_parts(
            raw.get("open_traffic_year"),
            raw.get("open_traffic_month"),
            raw.get("open_traffic_day"),
            default_month=12,
            default_day=31,
        )
        or parse_iso_date(project.open_to_traffic_date, 12, 31)
        or (datetime.date(project.end_year, 12, 31) if project.end_year else None)
    )
    return d_before, d_after


def _clean_str_field(row: Dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        if key in row and row[key] is not None:
            s = str(row[key]).strip()
            if s and s.lower() not in ("nan", "none"):
                return s
    return default


def _parse_first_float(row: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in row and pd.notna(row[key]):
            try:
                v = float(row[key])
                if not np.isnan(v):
                    return round(v, 3)
            except (ValueError, TypeError):
                pass
    return None


def _parse_coord_pair(row: Dict[str, Any], *keys: str) -> Optional[Tuple[float, float]]:
    for key in keys:
        val = str(row.get(key, "")).strip()
        if "," in val:
            parts = val.split(",")
            try:
                return float(parts[0].strip()), float(parts[1].strip())
            except ValueError:
                pass
    return None


def parse_master_row(row: Dict[str, Any], row_index: int = -1) -> ProjectRecord:
    """Convert a dictionary row from the master CSV into a clean ProjectRecord."""
    pid = str(row.get("Project ID", "")).strip()
    dist = normalize_district(row.get("Dist", ""))
    county = _clean_str_field(row, "County")
    state = _clean_str_field(row, "State", default="CA")
    route = _clean_str_field(row, "Route")
    direction = _clean_str_field(row, "Direction").upper()
    begin_pm = format_pm(str(row.get("Begin PM", "")).strip())
    end_pm = format_pm(str(row.get("End PM", "")).strip())
    project_description = _clean_str_field(row, "Project Description", "description", "project_description")

    abs_beg = _parse_first_float(row, "temp_abs_begin", "Absolute Begin PM", "Abs Begin PM")
    abs_end = _parse_first_float(row, "temp_abs_end", "Absolute End PM", "Abs End PM")
    start_coords = _parse_coord_pair(row, "Start Coordinates", "Coordinates")
    end_coords = _parse_coord_pair(row, "End Coordinates")

    c_dt = build_date_from_parts(
        row.get("construction_begin_year"),
        row.get("construction_begin_month"),
        row.get("construction_begin_day"),
        default_month=1,
        default_day=1,
    )
    c_begin = c_dt.isoformat() if c_dt else None
    if not c_begin:
        for k in ("Begin Date (formatted YYYY-MM-DD)", "Construction Begin Date"):
            dt = parse_iso_date(str(row.get(k, "")).strip(), default_month=1, default_day=1)
            if dt:
                c_begin = dt.isoformat()
                break

    o_dt = build_date_from_parts(
        row.get("open_traffic_year"),
        row.get("open_traffic_month"),
        row.get("open_traffic_day"),
        default_month=12,
        default_day=31,
    )
    o_traffic = o_dt.isoformat() if o_dt else None
    if not o_traffic:
        for k in ("Open to Traffic Date (formatted YYYY-MM-DD)", "Open to Traffic Date"):
            dt = parse_iso_date(str(row.get(k, "")).strip(), default_month=12, default_day=31)
            if dt:
                o_traffic = dt.isoformat()
                break

    sensor_stations: List[str] = []
    for col in (
        "Sensor Station 1 ID ", "Sensor Station 1 ID",
        "Sensor Station 1 HOV ID",
        "Sensor Station 2 ID ", "Sensor Station 2 ID",
        "Sensor Station 3 ID ", "Sensor Station 3 ID",
        "Sensor Station 4 ID ", "Sensor Station 4 ID",
    ):
        s_val = str(row.get(col, "")).strip()
        if s_val and s_val.lower() not in ("nan", "none", "0", ""):
            for sid in re.findall(r"\b\d{5,9}\b", s_val):
                if sid not in sensor_stations:
                    sensor_stations.append(sid)

    return ProjectRecord(
        project_id=pid,
        dist=dist,
        county=county,
        state=state,
        route=route,
        direction=direction,
        begin_pm=begin_pm,
        end_pm=end_pm,
        abs_begin_pm=abs_beg,
        abs_end_pm=abs_end,
        start_coords=start_coords,
        end_coords=end_coords,
        construction_begin_date=c_begin,
        open_to_traffic_date=o_traffic,
        sensor_stations=sensor_stations,
        raw_row=row,
        row_index=row_index,
        project_description=project_description,
    )


def load_master_csv(csv_path: Optional[str] = None) -> List[ProjectRecord]:
    """Load and parse the master project CSV into a list of ProjectRecord objects."""
    path = csv_path or DEFAULT_MASTER_CSV
    if not os.path.exists(path):
        raise FileNotFoundError(f"Master CSV file not found: {path}")

    df = pd.read_csv(path, dtype=str)
    return [parse_master_row(row.to_dict(), row_index=int(idx)) for idx, row in df.iterrows()]


def save_master_csv_with_backup(
    source_csv_path: str,
    updated_df: pd.DataFrame,
    output_csv_path: Optional[str] = None,
    use_iso_timestamp: bool = True,
) -> Tuple[str, str]:
    """Safely create a timestamped backup and write an updated master CSV (refusing Box-Box targets)."""
    target_out = output_csv_path or source_csv_path
    assert_not_box_path(target_out)
    assert_not_box_path(source_csv_path)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") if use_iso_timestamp else str(int(time.time()))
    backup_path = f"{source_csv_path}.bak_{stamp}"
    shutil.copy2(source_csv_path, backup_path)
    LOGGER.info(f"Created safety backup of input master CSV at: {backup_path}")
    updated_df.to_csv(target_out, index=False)
    return backup_path, target_out


class ProjectPeMSQueryBuilder:
    """Builds and resolves PeMS queries and corridor geometry for a ProjectRecord."""

    def __init__(self, project: ProjectRecord, ref_df: Optional[Any] = None):
        self.project = project
        self.idx_ref: IndexedGeoReference = coerce_geo_ref(ref_df)
        self.ref_df = self.idx_ref.raw_df

    def resolve_absolute_postmiles(self, force_recalculate: bool = False) -> Tuple[Optional[float], Optional[float]]:
        """Calculate and cache missing absolute postmiles from California county postmiles."""
        if force_recalculate or self.project.abs_begin_pm is None or self.project.abs_end_pm is None:
            calc_b, calc_e = resolve_absolute_postmiles(
                self.project.route,
                self.project.county,
                self.project.begin_pm,
                self.project.end_pm,
                ref_df=self.idx_ref,
            )
            if calc_b is not None and calc_e is not None:
                if force_recalculate or self.project.abs_begin_pm is None:
                    self.project.abs_begin_pm = calc_b
                if force_recalculate or self.project.abs_end_pm is None:
                    self.project.abs_end_pm = calc_e
        return self.project.abs_begin_pm, self.project.abs_end_pm

    def resolve_geometry(self) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
        """Resolve start and end coordinates from postmiles when omitted in the spreadsheet."""
        start = self.project.start_coords
        end = self.project.end_coords
        self.resolve_absolute_postmiles()

        is_abs = self.project.has_abs_pm_bounds
        b_pm = self.project.abs_begin_pm if is_abs else self.project.begin_pm
        e_pm = self.project.abs_end_pm if is_abs else self.project.end_pm

        if start is None and b_pm is not None and b_pm != "":
            start = pm_to_coordinates(
                self.project.route,
                self.project.pems_direction,
                b_pm,
                county=self.project.county,
                is_absolute=is_abs,
                ref_df=self.idx_ref,
            )
        if end is None and e_pm is not None and e_pm != "":
            end = pm_to_coordinates(
                self.project.route,
                self.project.pems_direction,
                e_pm,
                county=self.project.county,
                is_absolute=is_abs,
                ref_df=self.idx_ref,
            )
        return start, end

    def get_corridor_stations_from_reference(self) -> List[Dict[str, Any]]:
        """Find reference centerline segments along the project corridor."""
        is_abs = self.project.has_abs_pm_bounds
        if is_abs:
            b_pm = float(self.project.abs_begin_pm)  # type: ignore[arg-type]
            e_pm = float(self.project.abs_end_pm)    # type: ignore[arg-type]
        else:
            _, b_pm = split_ca_pm(self.project.begin_pm)
            _, e_pm = split_ca_pm(self.project.end_pm)

        if b_pm is None or e_pm is None or np.isnan(b_pm) or np.isnan(e_pm):
            return []

        df = get_corridor_stations(
            self.project.route,
            self.project.pems_direction,
            b_pm,
            e_pm,
            is_absolute=is_abs,
            county=self.project.county,
            ref_df=self.idx_ref,
        )
        if df.empty:
            return []

        id_col = "ID" if "ID" in df.columns else ("Station" if "Station" in df.columns else None)
        results: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            item: Dict[str, Any] = {
                "route": self.project.route,
                "direction": self.project.direction,
                "ca_pm": r.get("CA PM"),
                "abs_pm": r.get("Abs PM"),
                "latitude": r.get("Latitude"),
                "longitude": r.get("Longitude"),
            }
            if id_col:
                item["station_id"] = r.get(id_col)
            results.append(item)
        return results

    def generate_all_urls(self, start_year: Optional[int] = None, end_year: Optional[int] = None) -> Dict[str, Any]:
        """Construct all PeMS URLs relevant to this project."""
        s_yr = start_year or self.project.start_year
        e_yr = end_year or self.project.end_year
        if s_yr > e_yr:
            s_yr, e_yr = e_yr, s_yr

        lane_config_url = build_lane_config_url(
            self.project.route,
            self.project.pems_direction,
            start_date=f"01/01/{s_yr}",
            end_date=f"12/31/{e_yr}",
        )
        geometry_url = build_fwy_latlng_url(self.project.route, self.project.pems_direction)

        station_aadt_urls = {
            st_id: [(y, build_station_aadt_url(st_id, y)) for y in range(s_yr, e_yr + 1)]
            for st_id in self.project.sensor_stations
        }
        station_flow_urls = {
            st_id: [(y, build_station_flow_url(st_id, y)) for y in range(s_yr, e_yr + 1)]
            for st_id in self.project.sensor_stations
        }
        station_summary_urls = {st_id: build_station_summary_url(st_id) for st_id in self.project.sensor_stations}
        station_config_urls = {st_id: build_station_config_url(st_id) for st_id in self.project.sensor_stations}

        return {
            "project_id": self.project.project_id,
            "corridor": (
                f"Route {self.project.route} {self.project.direction} "
                f"(PM {self.project.begin_pm} - {self.project.end_pm})"
            ),
            "county": self.project.county,
            "district": self.project.dist,
            "year_range": (s_yr, e_yr),
            "lane_config_url": lane_config_url,
            "geometry_url": geometry_url,
            "sensor_stations": self.project.sensor_stations,
            "station_aadt_urls": station_aadt_urls,
            "station_flow_urls": station_flow_urls,
            "station_summary_urls": station_summary_urls,
            "station_config_urls": station_config_urls,
        }


def get_project_corridor_geometry(
    project: ProjectRecord,
    idx_ref: Optional[Any] = None,
) -> List[Tuple[float, float]]:
    """Extract ordered list of (lat, lng) points representing the physical roadway path."""
    ref = coerce_geo_ref(idx_ref)
    sub = ref.get_corridor(project.route, project.pems_direction)
    coords: List[Tuple[float, float]] = []

    if sub is not None and not sub.empty:
        is_abs = project.has_abs_pm_bounds
        if is_abs:
            b_pm = float(project.abs_begin_pm)  # type: ignore[arg-type]
            e_pm = float(project.abs_end_pm)    # type: ignore[arg-type]
        else:
            _, b_pm = split_ca_pm(project.begin_pm)
            _, e_pm = split_ca_pm(project.end_pm)

        if b_pm is not None and e_pm is not None and not np.isnan(b_pm) and not np.isnan(e_pm):
            min_pm, max_pm = min(float(b_pm), float(e_pm)), max(float(b_pm), float(e_pm))
            ascending = float(b_pm) <= float(e_pm)
            if is_abs:
                pts = sub[(sub["_abs_pm"] >= min_pm - 0.05) & (sub["_abs_pm"] <= max_pm + 0.05)]
                pts = pts.sort_values(by="_abs_pm", ascending=ascending)
            else:
                county_clean = str(project.county).strip().lower()
                pts = sub[
                    (sub["_ca_num"] >= min_pm - 0.05)
                    & (sub["_ca_num"] <= max_pm + 0.05)
                    & (sub["_county"] == county_clean)
                ]
                pts = pts.sort_values(by="_ca_num", ascending=ascending)

            if len(pts) >= 2:
                valid_pts = pts.dropna(subset=["_lat", "_lng"])
                coords = [(float(r["_lat"]), float(r["_lng"])) for _, r in valid_pts.iterrows()]

    if len(coords) < 2 and project.start_coords and project.end_coords:
        coords = [project.start_coords, project.end_coords]

    return coords
