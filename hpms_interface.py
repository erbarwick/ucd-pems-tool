#!/usr/bin/env python3
"""HPMS Interface & Corridor Validation Engine for CERCET Highway Projects.

Provides querying, corridor segment matching, ground-truth lane count validation,
and AADT analysis using the Federal Highway Administration (FHWA) Highway Performance
Monitoring System (HPMS) open data APIs (Socrata Open Data API - SODA).

This tool runs alongside pems_interface.py to validate projects across all California
routes (especially rural highways and conventional routes where PeMS detector stations
are not instrumented).
"""

import os
import sys
import json
import math
import time
import argparse
import textwrap
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd

# Base Directories
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(WORKSPACE_DIR, "downloads", "hpms_cache")
DEFAULT_MASTER_CSV = os.path.join(WORKSPACE_DIR, "staging", "Master_CA_Project_List_Cleaned.csv")
DEFAULT_REF_PATH = os.path.join(WORKSPACE_DIR, "downloads", "merged_geo_pm.csv")

# Ensure cache dir exists
os.makedirs(CACHE_DIR, exist_ok=True)

# California FIPS code
CA_STATE_FIPS = 6

# Mapping of California County Name to County FIPS (odd numbers 1 to 115)
CA_COUNTY_FIPS = {
    "alameda": 1, "alpine": 3, "amador": 5, "butte": 7, "calaveras": 9,
    "colusa": 11, "contra costa": 13, "del norte": 15, "el dorado": 17, "fresno": 19,
    "glenn": 21, "humboldt": 23, "imperial": 25, "inyo": 27, "kern": 29,
    "kings": 31, "lake": 33, "lassen": 35, "los angeles": 37, "madera": 39,
    "marin": 41, "mariposa": 43, "mendocino": 45, "merced": 47, "modoc": 49,
    "mono": 51, "monterey": 53, "napa": 55, "nevada": 57, "orange": 59,
    "placer": 61, "plumas": 63, "riverside": 65, "sacramento": 67, "san benito": 69,
    "san bernardino": 71, "san diego": 73, "san francisco": 75, "san joaquin": 77,
    "san luis obispo": 79, "san mateo": 81, "santa barbara": 83, "santa clara": 85,
    "santa cruz": 87, "shasta": 89, "sierra": 91, "siskiyou": 93, "solano": 95,
    "sonoma": 97, "stanislaus": 99, "sutter": 101, "tehama": 103, "trinity": 105,
    "tulare": 107, "tuolumne": 109, "ventura": 111, "yolo": 113, "yuba": 115
}
CA_FIPS_TO_COUNTY = {v: k.title() for k, v in CA_COUNTY_FIPS.items()}

# Dataset Endpoints on datahub.transportation.gov
# Modern Spatial All Sections (wide schema with through_lanes, dir_through_lanes, aadt, wkt line)
HPMS_SPATIAL_ENDPOINTS = {
    2024: "42um-tgh5",
}

# Annual Roadway Events / Sections (EAV or section format)
HPMS_SECTION_ENDPOINTS = {
    2024: "4wpf-u82y",
    2023: "8gz6-23ex",
    2022: "fsfq-kawt",
    2021: "trfk-mhda",
    2020: "q8hw-cdnd",
    2019: "rkzg-z7ht",  # Roadway Sections West 2019
    2018: "v9ae-hsuk",  # Roadway Sections West 2018
}

# Historical Universe Datasets (1980 - 2008)
HPMS_HISTORIC_UNIVERSE = {
    2008: "65pp-nswq", 2007: "9vq4-dn8b", 2006: "w95q-6nqw", 2005: "8xu8-ax8r",
    2004: "kpa7-pmax", 2003: "ismp-dmnm", 2002: "brry-xrw9", 2001: "w2yw-84zu",
    2000: "hddu-nghv", 1999: "6mnf-74vh", 1998: "dzuk-qpr3", 1997: "sgat-h8mv",
    1996: "922p-sj4m", 1995: "3n3x-bbb4", 1994: "affx-yhsw", 1993: "g5nf-5mpb",
    1992: "5cn3-yynq", 1991: "mhuc-n78n", 1990: "mp2w-t2nd", 1989: "mxiv-t53i",
    1988: "dbr2-hxmv", 1987: "e56t-hfa9", 1986: "cu4q-nx8c", 1985: "2m53-wgng",
    1984: "j2gk-sh7k", 1983: "6itn-mw5q", 1982: "br2w-67fy", 1981: "wekv-ifsj",
    1980: "efu4-vidc"
}


class HPMSClient:
    """Client for querying FHWA HPMS Open Data on datahub.transportation.gov via SODA REST API."""

    BASE_URL = "https://datahub.transportation.gov/resource"

    def __init__(self, cache_dir: str = CACHE_DIR):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def query(self, dataset_uid: str, where: Optional[str] = None, select: Optional[str] = None,
              order: Optional[str] = None, limit: int = 5000, offset: int = 0,
              use_cache: bool = True) -> List[Dict[str, Any]]:
        """Query SODA REST API with automatic caching and retry logic."""
        params = {}
        if where:
            params["$where"] = where
        if select:
            params["$select"] = select
        if order:
            params["$order"] = order
        params["$limit"] = str(limit)
        if offset > 0:
            params["$offset"] = str(offset)

        # Cache key based on dataset UID and query parameters
        cache_key_str = f"{dataset_uid}__" + "_".join(f"{k}={v}" for k, v in sorted(params.items()))
        safe_filename = "".join(c if c.isalnum() or c in "._-" else "_" for c in cache_key_str)[:200] + ".json"
        cache_path = os.path.join(self.cache_dir, safe_filename)

        if use_cache and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        url = f"{self.BASE_URL}/{dataset_uid}.json?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "CERCET-HPMS-Client/1.0"})

        max_retries = 3
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if use_cache:
                            try:
                                with open(cache_path, "w", encoding="utf-8") as f:
                                    json.dump(data, f)
                            except Exception:
                                pass
                        return data
                    else:
                        raise RuntimeError(f"HTTP {resp.status}: {resp.reason}")
            except Exception as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"Failed to query HPMS endpoint {dataset_uid}: {e}")
                time.sleep(1.0 * (attempt + 1))

        return []

    def get_spatial_sections(self, route_number: int, begin_pm: float, end_pm: float,
                             state_fips: int = CA_STATE_FIPS, county_fips: Optional[int] = None,
                             year: int = 2024) -> List[Dict[str, Any]]:
        """Query modern HPMS Spatial All Sections (wide table) for a route and postmile range."""
        uid = HPMS_SPATIAL_ENDPOINTS.get(year, HPMS_SPATIAL_ENDPOINTS[2024])
        # Allow small buffer around begin and end postmiles
        buf = 0.05
        min_p = max(0.0, min(begin_pm, end_pm) - buf)
        max_p = max(begin_pm, end_pm) + buf

        where_clauses = [
            f"stateid={state_fips}",
            f"route_number={route_number}",
            f"begin_point<={max_p:.4f}",
            f"end_point>={min_p:.4f}"
        ]
        if county_fips:
            where_clauses.append(f"county_id={county_fips}")

        where_sql = " AND ".join(where_clauses)
        return self.query(uid, where=where_sql, order="begin_point ASC", limit=10000)

    def get_historical_event_lanes(self, route_number: int, begin_pm: float, end_pm: float,
                                   year: int, state_fips: int = CA_STATE_FIPS) -> List[Dict[str, Any]]:
        """Query historical Roadway Events / Sections datasets for THROUGH_LANES in earlier years."""
        buf = 0.05
        min_p = max(0.0, min(begin_pm, end_pm) - buf)
        max_p = max(begin_pm, end_pm) + buf

        if year in HPMS_SECTION_ENDPOINTS:
            uid = HPMS_SECTION_ENDPOINTS[year]
            rt_str = f"{route_number}"
            rt_pad = f"{route_number:03d}"

            # Era-specific SODA query syntax
            if year in [2018, 2019]:
                # Text fields in 2018/2019
                where_sql = (
                    f"state_code='{state_fips}' AND data_item='THROUGH_LANES' AND "
                    f"(route_id='SHS_{rt_str}_P' or route_id like '%{rt_str}%P%')"
                )
            elif year == 2020:
                # Numeric state_code, underscore field names
                where_sql = (
                    f"state_code={state_fips} AND data_item='THROUGH_LANES' AND "
                    f"(route_id='SHS_{rt_str}_P' or route_id like '%{rt_str}%P%')"
                )
            else:
                # 2021-2024: stateid, dataitem, routeid without underscores
                where_sql = (
                    f"stateid={state_fips} AND dataitem='THROUGH_LANES' AND "
                    f"(routeid='SHS_{rt_str}._P' or routeid='SHS_{rt_pad}._P' or routeid like '%{rt_str}%P%')"
                )

            raw_rows = self.query(uid, where=where_sql, limit=5000)

            # Filter numerically by begin_point and end_point
            matched = []
            for r in raw_rows:
                try:
                    bp_val = r.get("begin_point", r.get("beginpoint", 0))
                    ep_val = r.get("end_point", r.get("endpoint", 0))
                    bp = float(bp_val)
                    ep = float(ep_val)
                    if min(bp, ep) <= max_p and max(bp, ep) >= min_p:
                        # Normalize keys for caller
                        norm = dict(r)
                        norm["begin_point"] = min(bp, ep)
                        norm["end_point"] = max(bp, ep)
                        norm["value_numeric"] = r.get("value_numeric", r.get("valuenumeric"))
                        matched.append(norm)
                except Exception:
                    continue
            return matched

        elif year in HPMS_HISTORIC_UNIVERSE:
            uid = HPMS_HISTORIC_UNIVERSE[year]
            where_sql = (
                f"(state_code='{state_fips}' or state_code={state_fips}) AND "
                f"(route_number='{route_number}' or route_number={route_number})"
            )
            raw_rows = self.query(uid, where=where_sql, limit=5000)

            matched = []
            for r in raw_rows:
                try:
                    bp = float(r.get("begin_lrs", 0))
                    ep = float(r.get("end_lrs", 0))
                    if min(bp, ep) <= max_p and max(bp, ep) >= min_p:
                        norm = dict(r)
                        norm["begin_point"] = min(bp, ep)
                        norm["end_point"] = max(bp, ep)
                        norm["value_numeric"] = r.get("through_lanes")
                        matched.append(norm)
                except Exception:
                    continue
            return matched

        return []


class HPMSCorridorEngine:
    """Matches highway projects against HPMS corridor data, calculates lane metrics,

    and performs validation against project descriptions and spreadsheet values.
    """

    def __init__(self, master_csv_path: str = DEFAULT_MASTER_CSV,
                 ref_path: str = DEFAULT_REF_PATH):
        self.master_csv_path = master_csv_path
        self.ref_path = ref_path
        self.client = HPMSClient()
        self._ref_df: Optional[pd.DataFrame] = None
        self._master_df: Optional[pd.DataFrame] = None

    @property
    def ref_df(self) -> pd.DataFrame:
        if self._ref_df is None:
            if os.path.exists(self.ref_path):
                self._ref_df = pd.read_csv(self.ref_path, low_memory=False)
            else:
                self._ref_df = pd.DataFrame()
        return self._ref_df

    @property
    def master_df(self) -> pd.DataFrame:
        if self._master_df is None:
            if os.path.exists(self.master_csv_path):
                self._master_df = pd.read_csv(self.master_csv_path, low_memory=False)
            else:
                self._master_df = pd.DataFrame()
        return self._master_df

    def resolve_absolute_postmiles(self, route: int, county: str,
                                   begin_pm: float, end_pm: float) -> Tuple[Optional[float], Optional[float]]:
        """Converts County Postmiles (CA PM) to Absolute / Continuous Postmiles (Abs PM)

        matching HPMS begin_point / end_point linear referencing.
        """
        if self.ref_df.empty:
            return None, None

        sub = self.ref_df[(self.ref_df["Route"] == route) &
                          (self.ref_df["County"].str.strip().str.lower() == county.strip().lower())]
        if sub.empty:
            return None, None

        # Clean numeric CA PM
        def to_float(val):
            try:
                if isinstance(val, (int, float)):
                    return float(val)
                val_str = str(val).strip().lstrip("RMLCGE")
                return float(val_str)
            except Exception:
                return None

        sub_clean = sub.copy()
        sub_clean["clean_ca_pm"] = sub_clean["CA PM"].apply(to_float)
        sub_clean["clean_abs_pm"] = pd.to_numeric(sub_clean["Abs PM"], errors="coerce")
        sub_valid = sub_clean.dropna(subset=["clean_ca_pm", "clean_abs_pm"])

        if sub_valid.empty:
            return None, None

        # Sort by CA PM
        sub_valid = sub_valid.sort_values(by="clean_ca_pm")

        # Find closest Abs PM for begin_pm and end_pm
        idx_b = (sub_valid["clean_ca_pm"] - begin_pm).abs().idxmin()
        idx_e = (sub_valid["clean_ca_pm"] - end_pm).abs().idxmin()

        abs_begin = float(sub_valid.loc[idx_b, "clean_abs_pm"])
        abs_end = float(sub_valid.loc[idx_e, "clean_abs_pm"])

        return min(abs_begin, abs_end), max(abs_begin, abs_end)

    def inspect_project(self, project_id: Any) -> Dict[str, Any]:
        """Inspects a highway project, resolving its HPMS corridor data, ground-truth lane counts,

        AADT, facility type, and cross-checks against the master spreadsheet values.
        """
        target_pid_str = str(project_id).strip().replace(".0", "")
        master_pids = self.master_df["Project ID"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        p_rows = self.master_df[master_pids == target_pid_str]
        if p_rows.empty:
            raise ValueError(f"Project ID {project_id} not found in {self.master_csv_path}")

        row = p_rows.iloc[0]

        route = int(row.get("Route", 0))
        county = str(row.get("County", "")).strip()
        county_fips = CA_COUNTY_FIPS.get(county.lower())
        desc = str(row.get("Project Description", "")).strip()
        proj_type = str(row.get("Project Type (\"Capacity Expansion\" or \"New Roadway\")", row.get("project_type", ""))).strip()

        # Postmiles
        try:
            begin_pm = float(row.get("Begin PM", 0))
            end_pm = float(row.get("End PM", 0))
        except Exception:
            begin_pm, end_pm = 0.0, 0.0

        # Absolute postmiles
        abs_begin = row.get("temp_abs_begin")
        abs_end = row.get("temp_abs_end")
        if pd.isna(abs_begin) or pd.isna(abs_end) or abs_begin == "" or abs_end == "":
            calc_b, calc_e = self.resolve_absolute_postmiles(route, county, begin_pm, end_pm)
            abs_begin = calc_b if calc_b is not None else begin_pm
            abs_end = calc_e if calc_e is not None else end_pm
        else:
            abs_begin = float(abs_begin)
            abs_end = float(abs_end)

        # Dates & spreadsheet lane values
        open_year = row.get("open_traffic_year")
        const_year = row.get("construction_begin_year")
        open_date = row.get("Open to Traffic Date (formatted YYYY-MM-DD)", "")
        const_date = row.get("Begin Date (formatted YYYY-MM-DD)", "")

        lanes_before = row.get("total_lanes_before")
        lanes_after = row.get("total_lanes_after")
        dir_before = row.get("dir_lanes_before")
        dir_after = row.get("dir_lanes_after")

        # Query 2024 HPMS Spatial Sections
        hpms_sections = self.client.get_spatial_sections(
            route_number=route,
            begin_pm=abs_begin,
            end_pm=abs_end,
            county_fips=county_fips,
            year=2024
        )

        # Calculate length-weighted through lanes and AADT
        total_len = 0.0
        weighted_lanes = 0.0
        weighted_aadt = 0.0
        lane_dist: Dict[int, float] = {}

        parsed_sections = []
        for s in hpms_sections:
            try:
                bp = float(s.get("begin_point", 0))
                ep = float(s.get("end_point", 0))
                # Clamp overlap length to project corridor
                ov_start = max(min(bp, ep), min(abs_begin, abs_end))
                ov_end = min(max(bp, ep), max(abs_begin, abs_end))
                ov_len = max(0.0, ov_end - ov_start)

                tl_val = s.get("through_lanes")
                tl = int(float(tl_val)) if tl_val is not None else None
                dtl_val = s.get("dir_through_lanes")
                dtl = int(float(dtl_val)) if dtl_val is not None else None
                aadt_val = s.get("aadt")
                aadt = int(float(aadt_val)) if aadt_val is not None else None

                parsed_sections.append({
                    "begin_point": bp,
                    "end_point": ep,
                    "overlap_len": ov_len,
                    "through_lanes": tl,
                    "dir_through_lanes": dtl,
                    "facility_type": s.get("facility_type"),
                    "aadt": aadt,
                    "future_aadt": s.get("future_aadt"),
                    "future_aadt_year": s.get("future_aadt_year"),
                    "year_last_construction": s.get("year_last_construction"),
                    "year_last_improvement": s.get("year_last_improvement"),
                    "line": s.get("line")
                })

                if ov_len > 0 and tl is not None:
                    total_len += ov_len
                    weighted_lanes += ov_len * tl
                    lane_dist[tl] = lane_dist.get(tl, 0.0) + ov_len
                if ov_len > 0 and aadt is not None:
                    weighted_aadt += ov_len * aadt

            except Exception:
                continue

        avg_hpms_lanes = round(weighted_lanes / total_len, 2) if total_len > 0 else None
        avg_hpms_aadt = int(round(weighted_aadt / total_len)) if total_len > 0 and weighted_aadt > 0 else None

        # Mode of through lanes along corridor
        predominant_lanes = max(lane_dist.items(), key=lambda x: x[1])[0] if lane_dist else None

        return {
            "project_id": project_id,
            "route": route,
            "county": county,
            "county_fips": county_fips,
            "description": desc,
            "project_type": proj_type,
            "begin_pm": begin_pm,
            "end_pm": end_pm,
            "abs_begin": abs_begin,
            "abs_end": abs_end,
            "const_date": const_date,
            "open_date": open_date,
            "const_year": const_year,
            "open_year": open_year,
            "spreadsheet_lanes_before": lanes_before,
            "spreadsheet_lanes_after": lanes_after,
            "spreadsheet_dir_before": dir_before,
            "spreadsheet_dir_after": dir_after,
            "hpms_sections_count": len(parsed_sections),
            "hpms_avg_lanes_2024": avg_hpms_lanes,
            "hpms_predominant_lanes_2024": predominant_lanes,
            "hpms_avg_aadt_2024": avg_hpms_aadt,
            "hpms_lane_distribution_mi": {k: round(v, 3) for k, v in lane_dist.items()},
            "sections": parsed_sections
        }

    def audit_project(self, project_id: int) -> Dict[str, Any]:
        """Audits project lane counts comparing historical HPMS pre-construction data

        and modern post-construction data against the spreadsheet values.
        """
        insp = self.inspect_project(project_id)

        const_year = insp.get("const_year")
        open_year = insp.get("open_year")
        route = insp["route"]
        abs_begin = insp["abs_begin"]
        abs_end = insp["abs_end"]

        # Parse years
        before_year = None
        try:
            if pd.notna(const_year):
                before_year = int(float(const_year)) - 1
        except Exception:
            pass

        # Query historical through lanes if before_year available
        hist_lanes = None
        hist_sections = []
        if before_year:
            try:
                hist_sections = self.client.get_historical_event_lanes(
                    route_number=route,
                    begin_pm=abs_begin,
                    end_pm=abs_end,
                    year=before_year
                )
                lane_vals = []
                for s in hist_sections:
                    val = s.get("value_numeric") or s.get("through_lanes")
                    if val is not None:
                        try:
                            lane_vals.append(int(float(val)))
                        except Exception:
                            pass
                if lane_vals:
                    # Predominant lane count in historical year
                    from collections import Counter
                    hist_lanes = Counter(lane_vals).most_common(1)[0][0]
            except Exception:
                pass

        modern_lanes = insp.get("hpms_predominant_lanes_2024")
        modern_weighted = insp.get("hpms_avg_lanes_2024")

        s_b = insp.get("spreadsheet_lanes_before")
        s_a = insp.get("spreadsheet_lanes_after")

        # Determine match status
        status = "NEEDS_REVIEW"
        if modern_lanes is not None and pd.notna(s_a):
            try:
                s_a_val = float(s_a)
                if abs(modern_lanes - s_a_val) < 0.01:
                    status = "EXACT_MATCH"
                elif modern_weighted is not None and abs(modern_weighted - s_a_val) <= 0.6:
                    status = "CORRIDOR_PARTIAL_MATCH"
                else:
                    status = "MISMATCH"
            except Exception:
                pass

        return {
            **insp,
            "audit_status": status,
            "hpms_before_year": before_year,
            "hpms_before_lanes": hist_lanes,
            "hpms_hist_sections_count": len(hist_sections),
            "hpms_after_lanes": modern_lanes,
            "hpms_after_weighted": modern_weighted
        }

    def generate_hpms_map(self, project_id: int, output_html: Optional[str] = None) -> str:
        """Generates an interactive Folium map displaying HPMS road segments colored by lane count."""
        import folium

        data = self.inspect_project(project_id)
        sections = data.get("sections", [])
        route = data["route"]
        county = data["county"]
        pid = data["project_id"]
        desc = data["description"]

        if not output_html:
            output_html = os.path.join(WORKSPACE_DIR, f"route_map_hpms_{pid}.html")

        # Extract coordinates for map centering
        all_coords = []
        for s in sections:
            geom = s.get("line")
            if geom and isinstance(geom, dict) and "coordinates" in geom:
                coords = geom["coordinates"]
                for pt in coords:
                    if len(pt) >= 2:
                        all_coords.append((pt[1], pt[0]))  # lat, lon

        if all_coords:
            mean_lat = sum(c[0] for c in all_coords) / len(all_coords)
            mean_lon = sum(c[1] for c in all_coords) / len(all_coords)
        else:
            mean_lat, mean_lon = 36.7783, -119.4179

        # Initialize folium map
        fmap = folium.Map(
            location=[mean_lat, mean_lon],
            zoom_start=13,
            tiles="OpenStreetMap"
        )

        # Style by lane count
        def get_lane_color(lanes):
            if lanes is None:
                return "#7f8c8d"  # gray
            elif lanes <= 2:
                return "#e67e22"  # orange
            elif lanes == 3:
                return "#f39c12"  # yellow-orange
            elif lanes == 4:
                return "#2980b9"  # blue
            elif lanes == 5:
                return "#8e44ad"  # purple
            else:
                return "#27ae60"  # green

        # Add HPMS road segments as GeoJSON features
        hpms_layer = folium.FeatureGroup(name="HPMS 2024 Road Segments (Colored by Lanes)")
        for s in sections:
            geom = s.get("line")
            if not geom or not isinstance(geom, dict):
                continue

            tl = s.get("through_lanes")
            dtl = s.get("dir_through_lanes")
            aadt = s.get("aadt")
            bp = s.get("begin_point")
            ep = s.get("end_point")
            col = get_lane_color(tl)

            popup_html = f"""
            <div style="font-family: Arial, sans-serif; min-width: 200px;">
                <h4 style="margin: 0 0 5px 0; color: #2c3e50;">HPMS Segment (Route {route})</h4>
                <table style="width: 100%; font-size: 12px; border-collapse: collapse;">
                    <tr><td><b>Through Lanes:</b></td><td><span style="font-size: 14px; font-weight: bold; color: {col};">{tl or 'N/A'}</span></td></tr>
                    <tr><td><b>Directional Lanes:</b></td><td>{dtl or 'N/A'}</td></tr>
                    <tr><td><b>Abs Postmiles:</b></td><td>{bp} - {ep}</td></tr>
                    <tr><td><b>AADT:</b></td><td>{aadt:,} vpd</td></tr>
                    <tr><td><b>Overlap Length:</b></td><td>{s.get('overlap_len', 0):.3f} mi</td></tr>
                </table>
            </div>
            """

            folium.GeoJson(
                geom,
                style_function=lambda feature, color=col: {
                    "color": color,
                    "weight": 5,
                    "opacity": 0.85
                },
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=f"Route {route} | {tl} Lanes | AADT: {aadt:,}" if aadt else f"Route {route} | {tl} Lanes"
            ).add_to(hpms_layer)

        hpms_layer.add_to(fmap)

        # Title Card
        title_html = f"""
        <div style="position: fixed; top: 10px; left: 60px; z-index: 9999;
                    background: white; padding: 12px 18px; border-radius: 6px;
                    box-shadow: 0 2px 6px rgba(0,0,0,0.3); max-width: 450px;">
            <h3 style="margin: 0 0 4px 0; color: #2c3e50; font-size: 16px;">
                Project {pid} (Route {route}, {county} Co)
            </h3>
            <p style="margin: 0 0 6px 0; font-size: 12px; color: #555;">
                {desc}
            </p>
            <div style="font-size: 12px; border-top: 1px solid #eee; padding-top: 5px;">
                <b>Spreadsheet:</b> {data['spreadsheet_lanes_before']} &rarr; {data['spreadsheet_lanes_after']} lanes |
                <b>HPMS 2024:</b> {data['hpms_predominant_lanes_2024']} lanes (predom) / {data['hpms_avg_lanes_2024']} (wt)
            </div>
            <div style="font-size: 11px; margin-top: 6px; display: flex; gap: 8px;">
                <span style="color: #e67e22;">&#9632; 2 Lanes</span>
                <span style="color: #f39c12;">&#9632; 3 Lanes</span>
                <span style="color: #2980b9;">&#9632; 4 Lanes</span>
                <span style="color: #8e44ad;">&#9632; 5 Lanes</span>
                <span style="color: #27ae60;">&#9632; 6+ Lanes</span>
            </div>
        </div>
        """
        fmap.get_root().html.add_child(folium.Element(title_html))
        folium.LayerControl().add_to(fmap)

        fmap.save(output_html)
        return output_html

    def format_inspection_output(self, data: Dict[str, Any]) -> str:
        """Formats the project inspection into an elegant terminal report including project description."""
        pid = data["project_id"]
        desc = data["description"]
        route = data["route"]
        county = data["county"]
        bp = data["begin_pm"]
        ep = data["end_pm"]
        ab = data["abs_begin"]
        ae = data["abs_end"]
        cd = data["const_date"]
        od = data["open_date"]
        s_b = data["spreadsheet_lanes_before"]
        s_a = data["spreadsheet_lanes_after"]
        s_db = data["spreadsheet_dir_before"]
        s_da = data["spreadsheet_dir_after"]
        hpms_lanes = data["hpms_predominant_lanes_2024"]
        avg_lanes = data["hpms_avg_lanes_2024"]
        hpms_aadt = data["hpms_avg_aadt_2024"]
        dist_map = data["hpms_lane_distribution_mi"]

        # Wrapped description with neat indent
        wrapped_desc = "\n                      ".join(textwrap.wrap(desc, width=54)) if desc else "(None)"

        lines = [
            "",
            "=" * 70,
            f"PROJECT HPMS GROUND-TRUTH INSPECTION: {pid}",
            "=" * 70,
            f"  Project Description: {wrapped_desc}",
            f"  Route & County:     State Route {route} | {county} County (FIPS {data['county_fips']})",
            f"  County Postmiles:   Begin: {bp} | End: {ep} (Length: {abs(ep - bp):.2f} mi)",
            f"  Absolute PMs (HPMS): Begin: {ab:.2f} | End: {ae:.2f} (Corridor: {abs(ae - ab):.2f} mi)",
            f"  Schedule Dates:     Const: {cd or '(Unknown)'} | Open: {od or '(Unknown)'}",
            "",
            "  SPREADSHEET CAPACITY VALUES:",
            f"    Total Lanes:      Before = {s_b}  -->  After = {s_a}",
            f"    Directional Lanes: Before = {s_db}  -->  After = {s_da}",
            "",
            "  HPMS 2024 GROUND-TRUTH CROSS-SECTION:",
            f"    Predominant Lanes: {hpms_lanes if hpms_lanes is not None else 'N/A'} through lanes",
            f"    Length-Weighted:  {avg_lanes if avg_lanes is not None else 'N/A'} through lanes",
            f"    Corridor AADT:    {hpms_aadt:,} vehicles/day" if hpms_aadt else "    Corridor AADT:    N/A",
            f"    Lane Distribution: {dist_map if dist_map else 'No overlapping segments'}",
            f"    Matched Segments:  {data['hpms_sections_count']} HPMS road segments",
        ]

        # Validation status check
        if hpms_lanes is not None and pd.notna(s_a):
            try:
                s_a_val = float(s_a)
                if abs(hpms_lanes - s_a_val) < 0.01:
                    lines.append(f"    VALIDATION:       MATCH [HPMS {hpms_lanes} == Spreadsheet {s_a_val:.0f}]")
                else:
                    lines.append(f"    VALIDATION:       MISMATCH [HPMS 2024 {hpms_lanes} vs Spreadsheet After {s_a_val:.0f}]")
            except Exception:
                pass

        lines.append("=" * 70)
        return "\n".join(lines)


    def format_audit_output(self, data: Dict[str, Any]) -> str:
        """Formats the project lane audit into a comprehensive comparison report."""
        pid = data["project_id"]
        desc = data["description"]
        route = data["route"]
        county = data["county"]
        bp = data["begin_pm"]
        ep = data["end_pm"]
        s_b = data["spreadsheet_lanes_before"]
        s_a = data["spreadsheet_lanes_after"]
        s_db = data["spreadsheet_dir_before"]
        s_da = data["spreadsheet_dir_after"]

        b_yr = data.get("hpms_before_year")
        b_ln = data.get("hpms_before_lanes")
        a_ln = data.get("hpms_after_lanes")
        a_wt = data.get("hpms_after_weighted")
        status = data.get("audit_status", "UNKNOWN")

        wrapped_desc = "\n                      ".join(textwrap.wrap(desc, width=54)) if desc else "(None)"

        status_symbols = {
            "EXACT_MATCH": "[EXACT MATCH]",
            "CORRIDOR_PARTIAL_MATCH": "[PARTIAL / SEGMENT EXPANSION]",
            "MISMATCH": "[MISMATCH / DISCREPANCY]",
            "NEEDS_REVIEW": "[NEEDS REVIEW]"
        }
        sym = status_symbols.get(status, f"[{status}]")

        lines = [
            "",
            "=" * 70,
            f"HPMS GROUND-TRUTH LANE AUDIT: {pid} {sym}",
            "=" * 70,
            f"  Project Description: {wrapped_desc}",
            f"  Corridor:           Route {route} | {county} County | PM {bp} - {ep}",
            "",
            f"  TIMELINE & LANE COMPARISON:",
            f"    Pre-Const Year:   {b_yr if b_yr else '(Unknown)'}",
            f"    HPMS Before:      {b_ln if b_ln is not None else 'N/A'} lanes (Year {b_yr})",
            f"    Spreadsheet Bef:  {s_b} total lanes (dir: {s_db})",
            "",
            f"    Post-Const Year:  2024",
            f"    HPMS After:       {a_ln if a_ln is not None else 'N/A'} lanes (predom) / {a_wt if a_wt is not None else 'N/A'} (length-weighted)",
            f"    Spreadsheet Aft:  {s_a} total lanes (dir: {s_da})",
            "",
            f"    Corridor AADT:    {data['hpms_avg_aadt_2024']:,} vehicles/day" if data.get('hpms_avg_aadt_2024') else "    Corridor AADT:    N/A",
            f"    Lane Dist (mi):   {data['hpms_lane_distribution_mi']}",
            f"    Audit Status:     {status}",
            "=" * 70
        ]
        return "\n".join(lines)

    def audit_district(self, district: Optional[int] = None) -> pd.DataFrame:
        """Audits all projects in a Caltrans District or statewide against HPMS ground truth."""
        df = self.master_df.copy()
        if district is not None:
            # Filter by district
            dist_col = "Dist" if "Dist" in df.columns else "District"
            df = df[pd.to_numeric(df[dist_col], errors="coerce") == district]

        results = []
        total = len(df)
        print(f"Auditing {total} projects against HPMS ground truth...")

        for i, (_, row) in enumerate(df.iterrows(), 1):
            pid = row.get("Project ID")
            if pd.isna(pid):
                continue
            pid = int(pid)
            try:
                audit = self.audit_project(pid)
                results.append({
                    "Project ID": pid,
                    "Route": audit["route"],
                    "County": audit["county"],
                    "Begin PM": audit["begin_pm"],
                    "End PM": audit["end_pm"],
                    "Spreadsheet Before": audit["spreadsheet_lanes_before"],
                    "Spreadsheet After": audit["spreadsheet_lanes_after"],
                    "HPMS 2024 Lanes": audit["hpms_after_lanes"],
                    "HPMS 2024 Wt Lanes": audit["hpms_after_weighted"],
                    "HPMS 2024 AADT": audit["hpms_avg_aadt_2024"],
                    "Audit Status": audit["audit_status"],
                    "HPMS Segments": audit["hpms_sections_count"],
                    "Description": audit["description"]
                })
                print(f"  [{i}/{total}] PID {pid} (Route {audit['route']}): {audit['audit_status']}")
            except Exception as e:
                results.append({
                    "Project ID": pid,
                    "Route": row.get("Route"),
                    "County": row.get("County"),
                    "Audit Status": f"ERROR: {e}",
                    "Description": row.get("Project Description", "")
                })
                print(f"  [{i}/{total}] PID {pid}: ERROR {e}")

        return pd.DataFrame(results)


def parse_args():
    parser = argparse.ArgumentParser(
        description="FHWA HPMS Ground-Truth Highway Interface for CERCET Project Validation."
    )
    # Target project
    parser.add_argument("--project", "-p", "--inspect", type=int,
                        help="Target Project ID to inspect with HPMS data (includes full description).")
    parser.add_argument("--map", "-m", nargs="?", const=-1, type=int,
                        help="Generate interactive HTML map with HPMS road segments colored by lane count (e.g. --map 713000217).")
    parser.add_argument("--audit", "-a", action="store_true",
                        help="Audit project lane counts against HPMS ground truth (single project with -p, or district with --dist).")
    parser.add_argument("--dist", "-d", type=int,
                        help="Caltrans District number for district-wide audit (e.g. --audit --dist 7).")
    parser.add_argument("--output-map", "-o", type=str,
                        help="Custom output file path for generated HTML map.")
    parser.add_argument("--output-report", type=str,
                        help="Custom output path to save audit report CSV.")

    # Ad-hoc corridor queries
    parser.add_argument("--route", "-r", type=int,
                        help="State Route number for ad-hoc corridor query.")
    parser.add_argument("--county", "-c", type=str,
                        help="California County name for ad-hoc corridor query.")
    parser.add_argument("--begin-pm", type=float,
                        help="Begin postmile for ad-hoc corridor query.")
    parser.add_argument("--end-pm", type=float,
                        help="End postmile for ad-hoc corridor query.")
    parser.add_argument("--year", "-y", type=int, default=2024,
                        help="HPMS Data Year to query (default: 2024).")
    parser.add_argument("--input", "-i", type=str, default=DEFAULT_MASTER_CSV,
                        help="Path to cleaned Master CSV file.")
    return parser.parse_args()


def main():
    args = parse_args()

    engine = HPMSCorridorEngine(master_csv_path=args.input)

    # Determine target PID
    pid = args.project
    if args.map and args.map != -1:
        pid = args.map

    if pid:
        if args.map is not None:
            # Generate map
            out_file = engine.generate_hpms_map(pid, output_html=args.output_map)
            print(f"\nSuccessfully generated HPMS corridor map for Project {pid}:")
            print(f"  {out_file}")
            # Also show inspection summary
            res = engine.inspect_project(pid)
            print(engine.format_inspection_output(res))
        elif args.audit:
            # Audit single project
            audit_res = engine.audit_project(pid)
            print(engine.format_audit_output(audit_res))
        else:
            # Default: inspect project
            res = engine.inspect_project(pid)
            print(engine.format_inspection_output(res))

    elif args.audit:
        # District-wide or statewide audit
        report_df = engine.audit_district(district=args.dist)
        out_path = args.output_report or os.path.join(WORKSPACE_DIR, f"hpms_audit_report_dist_{args.dist or 'all'}.csv")
        report_df.to_csv(out_path, index=False)
        print(f"\nAudit complete! Saved report to:\n  {out_path}")
        print("\nAudit Summary by Status:")
        print(report_df["Audit Status"].value_counts())

    elif args.route and args.begin_pm is not None and args.end_pm is not None:
        county_fips = CA_COUNTY_FIPS.get(args.county.lower()) if args.county else None
        client = HPMSClient()
        sections = client.get_spatial_sections(
            route_number=args.route,
            begin_pm=args.begin_pm,
            end_pm=args.end_pm,
            county_fips=county_fips,
            year=args.year
        )
        print(f"\nHPMS Query: Route {args.route} | Abs PM {args.begin_pm} - {args.end_pm} (Year {args.year})")
        print(f"Found {len(sections)} matching segments:")
        for s in sections[:20]:
            bp = s.get("begin_point")
            ep = s.get("end_point")
            tl = s.get("through_lanes")
            dtl = s.get("dir_through_lanes")
            aadt = s.get("aadt")
            cid = s.get("county_id")
            c_name = CA_FIPS_TO_COUNTY.get(int(cid), f"FIPS {cid}") if cid else "Unknown"
            print(f"  [{c_name}] Abs PM {bp} - {ep} | Through Lanes: {tl} | Dir Lanes: {dtl} | AADT: {aadt}")
        if len(sections) > 20:
            print(f"  ... and {len(sections) - 20} more segments.")
    else:
        # If no arguments provided, show help
        print("HPMS Interface & Corridor Ground-Truth Validation Engine")
        print("Usage:")
        print("  python hpms_interface.py -p <PID>            # Inspect project with HPMS data & description")
        print("  python hpms_interface.py -p <PID> --map      # Generate interactive Folium map with HPMS layers")
        print("  python hpms_interface.py -p <PID> --audit    # Compare HPMS before/after lanes vs spreadsheet")
        print("  python hpms_interface.py --audit --dist <D>  # District-wide lane validation audit")
        print("\nRun `python hpms_interface.py --help` for full options.")


if __name__ == "__main__":
    main()
