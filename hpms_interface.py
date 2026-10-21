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
import re
import json
import math
import time
import shutil
import datetime
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


def parse_postmile(val: Any) -> Tuple[Optional[float], str]:
    """Parses a Caltrans postmile value, extracting the numeric float while preserving
    the original string label (including prefixes like 'R', 'M', 'L', 'C', 'G' and suffixes).

    Returns (numeric_float, original_string).
    If val is invalid/empty, returns (None, "").
    """
    if val is None or pd.isna(val):
        return None, ""
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return None, ""

    cleaned = s.replace("PM", "").replace("pm", "").strip()
    match = re.search(r"[-+]?\d*\.?\d+", cleaned)
    if match:
        try:
            num = float(match.group(0))
            return num, s
        except Exception:
            pass
    return None, s


def _fmt_aadt_display(val: Any) -> str:
    """Formats an AADT value for display in reports.
    If it is an integer or clean float, formats with commas.
    If it is an archival range, curve, or annotation, preserves the raw string as-is.
    If empty/null, returns '(None)'.
    """
    if pd.isna(val) or val is None:
        return "(None)"
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return "(None)"

    clean_dig = s.replace(".0", "").replace(",", "").strip()
    if clean_dig.isdigit():
        try:
            return f"{int(clean_dig):,}"
        except Exception:
            pass
    return s


def _is_empty_cell(val: Any) -> bool:
    """Returns True only if a cell is genuinely empty (None, NaN, blank string, or 0/0.0).
    Returns False if the cell contains qualitative research notes, dates, ranges,
    or non-integer strings (e.g. '8,510; 2020 \n 10,000; 2030' or '23,000-36,000').
    This protects qualitative researcher data from being accidentally overwritten.
    """
    if val is None or pd.isna(val):
        return True
    s = str(val).strip()
    return s in ("", "nan", "None", "0", "0.0")


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

            # Era-specific SODA query syntax: strictly match Caltrans State Highway System route IDs
            # Avoid dangerous wildcards like '%{rt_str}%P%' which match unrelated county roads
            if year in [2018, 2019]:
                # Text fields in 2018/2019
                where_sql = (
                    f"state_code='{state_fips}' AND data_item='THROUGH_LANES' AND "
                    f"(route_id='SHS_{rt_pad}_P' or route_id='SHS_{rt_str}_P' or "
                    f"route_id='SHS_{rt_pad}._P' or route_id='SHS_{rt_str}._P')"
                )
            elif year == 2020:
                # Numeric state_code, underscore field names
                where_sql = (
                    f"state_code={state_fips} AND data_item='THROUGH_LANES' AND "
                    f"(route_id='SHS_{rt_pad}._P' or route_id='SHS_{rt_pad}_P' or "
                    f"route_id='SHS_{rt_str}._P' or route_id='SHS_{rt_str}_P')"
                )
            else:
                # 2021-2024: stateid, dataitem, routeid without underscores
                where_sql = (
                    f"stateid={state_fips} AND dataitem='THROUGH_LANES' AND "
                    f"(routeid='SHS_{rt_pad}._P' or routeid='SHS_{rt_pad}_P' or "
                    f"routeid='SHS_{rt_str}._P' or routeid='SHS_{rt_str}_P')"
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

        # Guard against cross-county boundary inversion: if begin_pm > end_pm,
        # snapping both against the same county produces an inverted, false corridor.
        if begin_pm > end_pm:
            return None, None

        # Find closest Abs PM for begin_pm and end_pm
        idx_b = (sub_valid["clean_ca_pm"] - begin_pm).abs().idxmin()
        idx_e = (sub_valid["clean_ca_pm"] - end_pm).abs().idxmin()

        delta_b = abs(sub_valid.loc[idx_b, "clean_ca_pm"] - begin_pm)
        delta_e = abs(sub_valid.loc[idx_e, "clean_ca_pm"] - end_pm)
        if delta_b > 2.0 or delta_e > 2.0:
            # Distance too large to snap safely
            return None, None

        abs_begin = float(sub_valid.loc[idx_b, "clean_abs_pm"])
        abs_end = float(sub_valid.loc[idx_e, "clean_abs_pm"])

        return min(abs_begin, abs_end), max(abs_begin, abs_end)

    def inspect_project(self, project_id: Any, direction: Optional[str] = None) -> Dict[str, Any]:
        """Inspects a highway project, resolving its HPMS corridor data, ground-truth lane counts,
        AADT, facility type, and cross-checks against the master spreadsheet values.
        Direction-aware: properly distinguishes NB/SB or EB/WB rows.
        """
        target_pid_str = str(project_id).strip().replace(".0", "")
        master_pids = self.master_df["Project ID"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        p_rows = self.master_df[master_pids == target_pid_str]
        if p_rows.empty:
            raise ValueError(f"Project ID {project_id} not found in {self.master_csv_path}")

        # Direction-aware row selection
        if direction:
            dir_clean = direction.strip().upper()
            dir_match = p_rows[p_rows["Direction"].astype(str).str.strip().str.upper().str.startswith(dir_clean[0])]
            row = dir_match.iloc[0] if not dir_match.empty else p_rows.iloc[0]
        else:
            row = p_rows.iloc[0]

        dir_val = str(row.get("Direction", "NB")).strip()
        route = int(row.get("Route", 0))
        county = str(row.get("County", "")).strip()
        county_fips = CA_COUNTY_FIPS.get(county.lower())
        desc = str(row.get("Project Description", "")).strip()
        proj_type = str(row.get("Project Type (\"Capacity Expansion\" or \"New Roadway\")", row.get("project_type", ""))).strip()

        # Robust postmile parsing preserving prefixes
        begin_pm_float, orig_begin_str = parse_postmile(row.get("Begin PM"))
        end_pm_float, orig_end_str = parse_postmile(row.get("End PM"))
        begin_pm = begin_pm_float if begin_pm_float is not None else 0.0
        end_pm = end_pm_float if end_pm_float is not None else 0.0

        # Absolute postmiles
        abs_begin = row.get("temp_abs_begin")
        abs_end = row.get("temp_abs_end")
        has_temp_abs = False
        try:
            if pd.notna(abs_begin) and pd.notna(abs_end) and str(abs_begin).strip() != "" and str(abs_end).strip() != "":
                abs_begin = float(abs_begin)
                abs_end = float(abs_end)
                has_temp_abs = True
        except Exception:
            has_temp_abs = False

        pm_warning = None
        if not has_temp_abs:
            if begin_pm_float is not None and end_pm_float is not None and begin_pm_float > end_pm_float:
                pm_warning = (f"CROSS_COUNTY_POSTMILE_AMBIGUITY: Begin PM ({orig_begin_str}) > End PM ({orig_end_str}). "
                              "Corridor appears to span multiple counties. Continuous absolute postmiles required.")
                abs_begin = min(begin_pm, end_pm)
                abs_end = max(begin_pm, end_pm)
            else:
                calc_b, calc_e = self.resolve_absolute_postmiles(route, county, begin_pm, end_pm)
                if calc_b is not None and calc_e is not None:
                    abs_begin, abs_end = calc_b, calc_e
                else:
                    abs_begin = min(begin_pm, end_pm)
                    abs_end = max(begin_pm, end_pm)
                    if begin_pm_float is not None and end_pm_float is not None:
                        pm_warning = "Could not safely snap County PMs to continuous Abs PM (snapping delta > 2.0 mi or county boundary issue)."

        # Dates & spreadsheet lane values
        open_year = row.get("open_traffic_year")
        const_year = row.get("construction_begin_year")
        open_date = row.get("Open to Traffic Date (formatted YYYY-MM-DD)", "")
        const_date = row.get("Begin Date (formatted YYYY-MM-DD)", "")

        lanes_before = row.get("total_lanes_before")
        lanes_after = row.get("total_lanes_after")
        dir_before = row.get("dir_lanes_before")
        dir_after = row.get("dir_lanes_after")
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
            "direction": dir_val,
            "district": row.get("Dist", ""),
            "route": route,
            "county": county,
            "county_fips": county_fips,
            "description": desc,
            "project_type": proj_type,
            "begin_pm": begin_pm,
            "end_pm": end_pm,
            "orig_begin_pm": orig_begin_str,
            "orig_end_pm": orig_end_str,
            "abs_begin": abs_begin,
            "abs_end": abs_end,
            "pm_warning": pm_warning,
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

    def audit_project(self, project_id: Any, direction: Optional[str] = None) -> Dict[str, Any]:
        """Audits project lane counts comparing historical HPMS pre-construction data
        and modern post-construction data against the spreadsheet values.
        Direction-aware: properly targets NB/SB or EB/WB records.
        """
        insp = self.inspect_project(project_id, direction=direction)

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
        status = data["audit_status"]

        b_yr = data["hpms_before_year"]
        b_ln = data["hpms_before_lanes"]
        a_ln = data["hpms_after_lanes"]
        a_wt = data["hpms_after_weighted"]

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

    def tandem_audit_project(self, project_id: Any, direction: Optional[str] = None) -> Dict[str, Any]:
        """Performs a comprehensive triad audit comparing Spreadsheet, PeMS, and HPMS data side by side
        for roadway geometry (before/after lanes) and traffic volumes (baseline/forecast AADT).
        Direction-aware: accurately targets specific travel direction (e.g. NB vs SB).
        """
        # 1. Base HPMS inspection & historical audit
        insp = self.inspect_project(project_id, direction=direction)
        hist_audit = self.audit_project(project_id, direction=direction)

        route = insp["route"]
        county = insp["county"]
        dir_val = insp.get("direction", "NB")
        dist_val = str(insp.get("district") or "")

        bp = insp["begin_pm"]
        ep = insp["end_pm"]
        ab = insp["abs_begin"]
        ae = insp["abs_end"]
        orig_bp = insp.get("orig_begin_pm") or f"{bp:.1f}"
        orig_ep = insp.get("orig_end_pm") or f"{ep:.1f}"
        pm_warning = insp.get("pm_warning")

        # 2. Spreadsheet attributes from direction-specific row
        target_pid_str = str(project_id).strip().replace(".0", "")
        master_pids = self.master_df["Project ID"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        p_rows = self.master_df[master_pids == target_pid_str]
        if direction and not p_rows.empty:
            dir_clean = direction.strip().upper()
            dir_match = p_rows[p_rows["Direction"].astype(str).str.strip().str.upper().str.startswith(dir_clean[0])]
            s_row = dir_match.iloc[0] if not dir_match.empty else p_rows.iloc[0]
        else:
            s_row = p_rows.iloc[0] if not p_rows.empty else pd.Series()

        s_b = insp.get("spreadsheet_lanes_before")
        s_a = insp.get("spreadsheet_lanes_after")
        s_db = insp.get("spreadsheet_dir_before")
        s_da = insp.get("spreadsheet_dir_after")

        s_aadt = s_row.get("Existing AADT")
        s_aadt_yr = s_row.get("year ") if "year " in s_row else s_row.get("year", s_row.get("Existing AADT Year"))
        s_fc_aadt = s_row.get("forecast AADT ") if "forecast AADT " in s_row else s_row.get("forecast AADT")
        s_fc_yr = s_row.get("forecast year ") if "forecast year " in s_row else s_row.get("forecast year")

        # 3. PeMS VDS Detector Query
        pems_instrumented = False
        pems_stations = []
        pems_lanes = []
        pems_predom_lanes = None

        try:
            from pems_interface import load_vds_metadata
            meta = load_vds_metadata()
            pems_dir = dir_val[0] if dir_val else "N"
            stns = meta.find_corridor_stations(
                district=dist_val,
                route=str(route),
                direction=pems_dir,
                begin_pm=bp,
                end_pm=ep,
                station_types=("ML", "HV")
            )
            if stns:
                pems_instrumented = True
                pems_stations = [s.id for s in stns]
                pems_lanes = [s.lanes for s in stns if s.lanes is not None]
                if pems_lanes:
                    from collections import Counter
                    pems_predom_lanes = Counter(pems_lanes).most_common(1)[0][0]
        except Exception:
            pass

        # 4. HPMS AADT & Future Forecasts
        hpms_sections = insp.get("sections", [])
        hpms_future_aadt = None
        hpms_future_yr = None
        for s in hpms_sections:
            fa = s.get("future_aadt")
            fy = s.get("future_aadt_year")
            if fa is not None and str(fa).strip() != "" and str(fa).strip() != "0":
                try:
                    hpms_future_aadt = int(float(fa))
                    hpms_future_yr = int(float(fy)) if fy else None
                    break
                except Exception:
                    pass

        # 5. Triad Verdict Synthesis
        hpms_after = hist_audit.get("hpms_after_lanes")
        hpms_wt = hist_audit.get("hpms_after_weighted")
        hpms_before = hist_audit.get("hpms_before_lanes")
        hpms_sections_count = insp.get("hpms_sections_count", 0)
        has_hpms_coverage = (hpms_sections_count > 0) and (hpms_after is not None)

        lane_verdict = "NEEDS_REVIEW"
        verdict_detail = ""

        # Parse numeric spreadsheet values if present
        s_a_val = float(s_a) if pd.notna(s_a) else None
        s_da_val = float(s_da) if pd.notna(s_da) else None

        # Check directional match with PeMS
        pems_dir_match = False
        if pems_instrumented and pems_predom_lanes is not None:
            if s_da_val is not None:
                pems_dir_match = (abs(pems_predom_lanes - s_da_val) < 0.01)
            elif s_a_val is not None:
                pems_dir_match = (abs(pems_predom_lanes - (s_a_val / 2.0)) < 0.01)

        # Check total match with HPMS
        hpms_total_match = False
        hpms_partial = False
        target_total = s_a_val if s_a_val is not None else (s_da_val * 2.0 if s_da_val is not None else None)
        if has_hpms_coverage and target_total is not None:
            hpms_total_match = (abs(hpms_after - target_total) < 0.01)
            if hpms_wt is not None:
                hpms_partial = (abs(hpms_wt - target_total) <= 0.6)

        if pm_warning and "CROSS_COUNTY" in pm_warning:
            lane_verdict = "CROSS_COUNTY_BOUNDARY_REVIEW"
            verdict_detail = pm_warning
        elif s_a_val is None and s_da_val is None:
            lane_verdict = "MISSING_SPREADSHEET_DATA"
            verdict_detail = f"Spreadsheet missing post-expansion lane counts. Ground truth: PeMS={pems_predom_lanes or 'N/A'} dir, HPMS={hpms_after or 'N/A'} total."
        elif pems_instrumented:
            if not has_hpms_coverage:
                if pems_dir_match:
                    lane_verdict = "PEMS_VERIFIED_HPMS_UNAVAILABLE"
                    verdict_detail = f"Spreadsheet matches PeMS ({pems_predom_lanes} dir lanes); HPMS has 0 road segments for this corridor."
                else:
                    lane_verdict = "PEMS_DIVERGES_HPMS_UNAVAILABLE"
                    verdict_detail = f"PeMS detectors show {pems_predom_lanes} dir lanes (vs sheet {s_da_val or target_total}); HPMS segments unavailable."
            elif pems_dir_match and hpms_total_match:
                lane_verdict = "ALL_THREE_CONCUR"
                verdict_detail = f"Spreadsheet (Total {target_total:.0f}, Dir {s_da_val or target_total/2:.1f}), PeMS ({pems_predom_lanes} dir), and HPMS ({hpms_after} total) all concur."
            elif pems_dir_match and hpms_partial:
                lane_verdict = "CONCURS_WITH_PARTIAL_HPMS"
                verdict_detail = f"Spreadsheet and PeMS concur ({pems_predom_lanes} dir); HPMS shows passing/climbing expansion on segment ({hpms_wt:.2f} wt lanes vs sheet {target_total:.0f})."
            elif pems_dir_match and not hpms_total_match:
                lane_verdict = "SPREADSHEET_MATCHES_PEMS_HPMS_DIVERGES"
                verdict_detail = f"Spreadsheet matches PeMS ({pems_predom_lanes} dir lanes); HPMS reports {hpms_after} total lanes (wt: {hpms_wt})."
            elif not pems_dir_match and hpms_total_match:
                lane_verdict = "SPREADSHEET_MATCHES_HPMS_PEMS_DIVERGES"
                verdict_detail = f"Spreadsheet matches HPMS ({hpms_after} total lanes); PeMS detectors show {pems_predom_lanes} dir lanes."
            else:
                lane_verdict = "DISCREPANCY"
                verdict_detail = f"Discrepancy: Sheet Total={target_total:.0f}, PeMS={pems_predom_lanes} dir, HPMS={hpms_after} total."
        else:
            # PeMS uninstrumented (0 stations)
            if not has_hpms_coverage:
                lane_verdict = "NO_GROUND_TRUTH_AVAILABLE"
                verdict_detail = "Neither PeMS sensors nor HPMS road segments available for this corridor."
            elif hpms_total_match:
                lane_verdict = "UNINSTRUMENTED_PEMS_RESOLVED_BY_HPMS"
                verdict_detail = f"PeMS has 0 sensors (rural highway); HPMS confirms ground truth ({hpms_after} total lanes) matching spreadsheet."
            elif hpms_partial:
                lane_verdict = "PARTIAL_CORRIDOR_EXPANSION"
                verdict_detail = f"HPMS shows passing/truck climbing expansion on portion of segment ({hpms_wt:.2f} wt total lanes vs sheet {target_total:.0f})."
            else:
                lane_verdict = "HPMS_MISMATCH_UNINSTRUMENTED_PEMS"
                verdict_detail = f"PeMS has 0 sensors; HPMS reports {hpms_after} total lanes vs spreadsheet {target_total:.0f}."

        return {
            "project_id": project_id,
            "route": route,
            "county": county,
            "direction": dir_val,
            "district": dist_val,
            "begin_pm": bp,
            "end_pm": ep,
            "orig_begin_pm": orig_bp,
            "orig_end_pm": orig_ep,
            "abs_begin": ab,
            "abs_end": ae,
            "pm_warning": pm_warning,
            "description": insp.get("description", ""),
            "const_date": insp.get("const_date"),
            "open_date": insp.get("open_date"),
            # Spreadsheet
            "sheet_total_before": s_b,
            "sheet_total_after": s_a,
            "sheet_dir_before": s_db,
            "sheet_dir_after": s_da,
            "sheet_aadt": s_aadt,
            "sheet_aadt_year": s_aadt_yr,
            "sheet_forecast_aadt": s_fc_aadt,
            "sheet_forecast_year": s_fc_yr,
            # PeMS
            "pems_instrumented": pems_instrumented,
            "pems_stations_count": len(pems_stations),
            "pems_stations": pems_stations,
            "pems_corridor_lanes": pems_lanes,
            "pems_predom_lanes": pems_predom_lanes,
            # HPMS
            "hpms_before_year": hist_audit.get("hpms_before_year"),
            "hpms_before_lanes": hpms_before,
            "hpms_after_year": 2024,
            "hpms_after_lanes": hpms_after,
            "hpms_after_weighted": hpms_wt,
            "hpms_sections_count": hpms_sections_count,
            "hpms_lane_distribution": insp.get("hpms_lane_distribution_mi", {}),
            "hpms_2024_aadt": insp.get("hpms_avg_aadt_2024"),
            "hpms_future_aadt": hpms_future_aadt,
            "hpms_future_year": hpms_future_yr,
            # Synthesis
            "lane_verdict": lane_verdict,
            "verdict_detail": verdict_detail
        }

    def format_tandem_output(self, data: Dict[str, Any]) -> str:
        """Formats the multi-source triad audit (Spreadsheet vs PeMS vs HPMS) into a clean terminal report."""
        pid = data["project_id"]
        desc = data["description"]
        route = data["route"]
        county = data["county"]
        dir_val = data["direction"]
        bp = data.get("orig_begin_pm") or f"{data['begin_pm']:.1f}"
        ep = data.get("orig_end_pm") or f"{data['end_pm']:.1f}"
        ab = data["abs_begin"]
        ae = data["abs_end"]
        cd = data["const_date"]
        od = data["open_date"]
        pm_warn = data.get("pm_warning")

        s_b = str(data["sheet_total_before"])
        s_a = str(data["sheet_total_after"])
        s_db = str(data["sheet_dir_before"])
        s_da = str(data["sheet_dir_after"])

        p_inst = data["pems_instrumented"]
        p_stns = data["pems_stations_count"]
        p_ln = f"{data['pems_predom_lanes']} lanes" if data["pems_predom_lanes"] is not None else "(No VDS)"

        h_b_yr = data["hpms_before_year"]
        h_b_ln = f"{data['hpms_before_lanes']} lanes" if data["hpms_before_lanes"] is not None else "N/A"
        h_a_ln = f"{data['hpms_after_lanes']} lanes" if data["hpms_after_lanes"] is not None else "N/A"
        h_wt = f"{data['hpms_after_weighted']} wt" if data["hpms_after_weighted"] is not None else ""

        # AADT formatting using _fmt_aadt_display to preserve archival strings/ranges
        s_aadt = _fmt_aadt_display(data.get("sheet_aadt"))
        s_yr = f"({data['sheet_aadt_year']})" if pd.notna(data.get("sheet_aadt_year")) and str(data.get("sheet_aadt_year")).strip() not in ("", "nan") else ""
        s_fc = _fmt_aadt_display(data.get("sheet_forecast_aadt"))
        s_fc_yr = f"({data['sheet_forecast_year']})" if pd.notna(data.get("sheet_forecast_year")) and str(data.get("sheet_forecast_year")).strip() not in ("", "nan") else ""

        h_aadt = f"{data['hpms_2024_aadt']:,} vpd" if data.get("hpms_2024_aadt") else "N/A"
        h_fc = f"{data['hpms_future_aadt']:,} vpd" if data.get("hpms_future_aadt") else "N/A"
        h_fc_yr = f"({data['hpms_future_year']})" if data.get("hpms_future_year") else ""

        wrapped_desc = "\n                     ".join(textwrap.wrap(desc, width=64)) if desc else "(None)"

        h_sec_cnt = data["hpms_sections_count"]
        h_dist_str = str(data["hpms_lane_distribution"])

        lines = [
            "",
            "=" * 88,
            f"CERCET TRIAD CORRIDOR VERIFICATION: Project {pid} [{data['lane_verdict']}]",
            "=" * 88,
            f"  Description:       {wrapped_desc}",
            f"  Corridor:          Route {route} {dir_val} | {county} County | PM {bp} - {ep} (Abs {ab:.2f} - {ae:.2f})",
        ]
        if pm_warn:
            lines.append(f"  [BOUNDARY ALERT]:  {pm_warn}")
        lines.extend([
            f"  Schedule:          Const: {cd or '(Unknown)'} | Open: {od or '(Unknown)'}",
            "",
            "-" * 88,
            f"{'CROSS-SECTION (LANES)':<25} {'SPREADSHEET':<20} {'PEMS DETECTORS':<20} {'HPMS FEDERAL GROUND TRUTH':<23}",
            "-" * 88,
            f"{'Pre-Const Total':<25} {s_b:<20} {'(Station-level)':<20} {f'{h_b_ln} (Year {h_b_yr})':<23}",
            f"{'Pre-Const Directional':<25} {s_db:<20} {'(Station-level)':<20} {'N/A':<23}",
            f"{'Post-Const Total':<25} {s_a:<20} {p_ln:<20} {f'{h_a_ln} ({h_wt})':<23}",
            f"{'Post-Const Directional':<25} {s_da:<20} {p_ln:<20} {'N/A':<23}",
            f"{'Corridor Coverage':<25} {'Spreadsheet':<20} {f'{p_stns} active VDS':<20} {f'{h_sec_cnt} road segments':<23}",
            f"{'Lane Miles Overlap':<25} {'(Single estimate)':<20} {'(Loop sensors)':<20} {h_dist_str:<23}",
            "",
            "-" * 88,
            f"{'TRAFFIC VOLUMES (AADT)':<25} {'SPREADSHEET':<20} {'PEMS DETECTORS':<20} {'HPMS CERTIFIED VOLUMES':<23}",
            "-" * 88,
            f"{'Existing / Baseline':<25} {f'{s_aadt} {s_yr}':<20} {'(Time-series loop)':<20} {h_aadt:<23}",
            f"{'Forecast / Future':<25} {f'{s_fc} {s_fc_yr}':<20} {'(Not forecasted)':<20} {f'{h_fc} {h_fc_yr}':<23}",
            "",
            "-" * 88,
            f"VERDICT: {data['lane_verdict']}",
            f"  {data['verdict_detail']}",
            "=" * 88
        ])
        return "\n".join(lines)

    def audit_district(self, district: Optional[int] = None) -> pd.DataFrame:
        """Audits all projects in a Caltrans District or statewide against HPMS ground truth and PeMS detectors side by side."""
        df = self.master_df.copy()
        if district is not None:
            dist_col = "Dist" if "Dist" in df.columns else "District"
            df = df[pd.to_numeric(df[dist_col], errors="coerce") == district]

        results = []
        total = len(df)
        scope_desc = f"District {district}" if district is not None else "Statewide"
        print(f"\nRunning CERCET Triad Multi-Source Audit ({scope_desc}): {total} projects...")

        for i, (_, row) in enumerate(df.iterrows(), 1):
            pid = row.get("Project ID")
            if pd.isna(pid):
                continue
            pid = int(pid)
            direction_val = row.get("Direction")
            try:
                t = self.tandem_audit_project(pid, direction=direction_val)
                results.append({
                    "Project ID": pid,
                    "Route": t["route"],
                    "County": t["county"],
                    "Direction": t["direction"],
                    "District": t["district"],
                    "Begin PM": t["begin_pm"],
                    "End PM": t["end_pm"],
                    "Abs Begin PM": t["abs_begin"],
                    "Abs End PM": t["abs_end"],
                    "Spreadsheet Total Before": t["sheet_total_before"],
                    "Spreadsheet Total After": t["sheet_total_after"],
                    "Spreadsheet Dir Before": t["sheet_dir_before"],
                    "Spreadsheet Dir After": t["sheet_dir_after"],
                    "PeMS Predom Lanes (Dir)": t["pems_predom_lanes"],
                    "PeMS Active VDS Count": t["pems_stations_count"],
                    "HPMS Before Year": t["hpms_before_year"],
                    "HPMS Before Lanes (Total)": t["hpms_before_lanes"],
                    "HPMS 2024 Lanes (Total)": t["hpms_after_lanes"],
                    "HPMS 2024 Wt Lanes": t["hpms_after_weighted"],
                    "HPMS Segments Count": t["hpms_sections_count"],
                    "Spreadsheet Existing AADT": t["sheet_aadt"],
                    "Spreadsheet Baseline Year": t["sheet_aadt_year"],
                    "HPMS 2024 AADT": t["hpms_2024_aadt"],
                    "Spreadsheet Forecast AADT": t["sheet_forecast_aadt"],
                    "Spreadsheet Forecast Year": t["sheet_forecast_year"],
                    "HPMS Future AADT": t["hpms_future_aadt"],
                    "HPMS Future Year": t["hpms_future_year"],
                    "Lane Verdict": t["lane_verdict"],
                    "Verdict Detail": t["verdict_detail"],
                    "Description": t["description"]
                })
                dir_label = f" {t['direction']}" if t.get('direction') else ""
                print(f"  [{i}/{total}] PID {pid}{dir_label} (Route {t['route']}): {t['lane_verdict']}")
            except Exception as e:
                dir_label = f" {direction_val}" if direction_val else ""
                results.append({
                    "Project ID": pid,
                    "Route": row.get("Route"),
                    "County": row.get("County"),
                    "Direction": direction_val,
                    "Lane Verdict": f"ERROR: {e}",
                    "Description": row.get("Project Description", "")
                })
                print(f"  [{i}/{total}] PID {pid}{dir_label}: ERROR {e}")

        return pd.DataFrame(results)

    def fill_local_spreadsheet(self, target_path: Optional[str] = None,
                               project_id: Optional[Any] = None,
                               district: Optional[int] = None,
                               apply: bool = False) -> pd.DataFrame:
        """Identifies missing or unverifiable attributes in the local spreadsheet and proposes or applies
        high-confidence ground-truth values from HPMS and PeMS.

        CRITICAL SAFETY RULE: Refuses to touch any file path inside Box-Box.
        Targets strictly staging/Master_CA_Project_List_Cleaned.csv (or a specified local path)
        with automatic timestamped backups before writing.
        """
        if target_path is None:
            target_path = os.path.join(WORKSPACE_DIR, "staging", "Master_CA_Project_List_Cleaned.csv")

        abs_path = os.path.abspath(target_path)
        # STRICT BOX-BOX REJECTION GUARD
        if "box-box" in abs_path.lower() or "cloudstorage/box" in abs_path.lower():
            raise PermissionError(
                f"CRITICAL SAFETY VIOLATION: Refusing to modify Box spreadsheet at {target_path}. "
                "Direct edits to the Box master spreadsheet are strictly prohibited. "
                "Target only local repository files (e.g. staging/Master_CA_Project_List_Cleaned.csv)."
            )

        if not os.path.exists(target_path):
            raise FileNotFoundError(f"Local master spreadsheet not found: {target_path}")

        df_local = pd.read_csv(target_path, low_memory=False)

        # Determine target rows
        if project_id is not None:
            target_pid_str = str(project_id).strip().replace(".0", "")
            pids = df_local["Project ID"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            sub_df = df_local[pids == target_pid_str]
        elif district is not None:
            dist_col = "Dist" if "Dist" in df_local.columns else "District"
            sub_df = df_local[pd.to_numeric(df_local[dist_col], errors="coerce") == district]
        else:
            sub_df = df_local

        # Year and AADT column name resolution
        year_col = "year " if "year " in df_local.columns else "year"
        fc_aadt_col = "forecast AADT " if "forecast AADT " in df_local.columns else "forecast AADT"
        fc_year_col = "forecast year " if "forecast year " in df_local.columns else "forecast year"

        candidates = []
        applied_count = 0

        for idx, row in sub_df.iterrows():
            pid = row.get("Project ID")
            if pd.isna(pid):
                continue
            direction_val = row.get("Direction")
            try:
                t = self.tandem_audit_project(pid, direction=direction_val)
            except Exception:
                continue

            # 1. Existing AADT
            curr_aadt = row.get("Existing AADT")
            if _is_empty_cell(curr_aadt):
                if t.get("hpms_2024_aadt"):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": "Existing AADT",
                        "current_val": curr_aadt,
                        "proposed_val": int(t["hpms_2024_aadt"]),
                        "field_year": year_col,
                        "proposed_year": 2024,
                        "source": "HPMS 2024 Certified Traffic Volumes",
                        "confidence": "HIGH"
                    })

            # 2. Forecast AADT
            curr_fc = row.get(fc_aadt_col)
            if _is_empty_cell(curr_fc):
                if t.get("hpms_future_aadt"):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": fc_aadt_col,
                        "current_val": curr_fc,
                        "proposed_val": int(t["hpms_future_aadt"]),
                        "field_year": fc_year_col,
                        "proposed_year": t.get("hpms_future_year") or 2040,
                        "source": "HPMS Official Future Forecast",
                        "confidence": "HIGH"
                    })

            # 3. Total Lanes After
            curr_tla = row.get("total_lanes_after")
            if _is_empty_cell(curr_tla):
                if t.get("hpms_after_lanes"):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": "total_lanes_after",
                        "current_val": curr_tla,
                        "proposed_val": float(t["hpms_after_lanes"]),
                        "source": "HPMS 2024 Ground Truth",
                        "confidence": "HIGH"
                    })
                elif t.get("pems_predom_lanes"):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": "total_lanes_after",
                        "current_val": curr_tla,
                        "proposed_val": float(t["pems_predom_lanes"] * 2),
                        "source": "PeMS VDS Sensors (bi-directional estimate)",
                        "confidence": "MEDIUM"
                    })

            # 4. Directional Lanes After
            curr_dla = row.get("dir_lanes_after")
            if _is_empty_cell(curr_dla):
                if t.get("pems_predom_lanes"):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": "dir_lanes_after",
                        "current_val": curr_dla,
                        "proposed_val": float(t["pems_predom_lanes"]),
                        "source": "PeMS Active Mainline VDS",
                        "confidence": "HIGH"
                    })
                elif t.get("hpms_after_lanes") and (float(t["hpms_after_lanes"]) % 2 == 0):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": "dir_lanes_after",
                        "current_val": curr_dla,
                        "proposed_val": float(int(t["hpms_after_lanes"]) // 2),
                        "source": "HPMS 2024 (halved for symmetric roadway)",
                        "confidence": "MEDIUM"
                    })

            # 5. Total Lanes Before
            curr_tlb = row.get("total_lanes_before")
            if _is_empty_cell(curr_tlb):
                if t.get("hpms_before_lanes"):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": "total_lanes_before",
                        "current_val": curr_tlb,
                        "proposed_val": float(t["hpms_before_lanes"]),
                        "source": f"HPMS Pre-Const Historical Event ({t.get('hpms_before_year')})",
                        "confidence": "HIGH"
                    })

            # 6. Directional Lanes Before
            curr_dlb = row.get("dir_lanes_before")
            if _is_empty_cell(curr_dlb):
                if t.get("hpms_before_lanes") and (float(t["hpms_before_lanes"]) % 2 == 0):
                    candidates.append({
                        "index": idx,
                        "project_id": pid,
                        "direction": direction_val,
                        "field": "dir_lanes_before",
                        "current_val": curr_dlb,
                        "proposed_val": float(int(t["hpms_before_lanes"]) // 2),
                        "source": "HPMS Historical (halved for symmetric roadway)",
                        "confidence": "MEDIUM"
                    })

        cand_df = pd.DataFrame(candidates)

        if not apply:
            # Preview Mode
            print("\n" + "=" * 90)
            print("LOCAL SPREADSHEET FILL PREVIEW (DRY-RUN)")
            print(f"Target File: {target_path}")
            print(f"Total Candidate Fills Identified: {len(candidates)}")
            print("=" * 90)
            if candidates:
                preview_cols = [c for c in ["project_id", "direction", "field", "current_val", "proposed_val", "source", "confidence"] if c in cand_df.columns]
                print(cand_df[preview_cols].to_string(index=False))
                print("-" * 90)
                print("NOTE: No changes have been written to disk. To apply these updates, run with:")
                if project_id:
                    print(f"  python hpms_interface.py -p {project_id} --apply-local")
                elif district:
                    print(f"  python hpms_interface.py --dist {district} --fill-local --apply-local")
                else:
                    print("  python hpms_interface.py --fill-local --apply-local")
            else:
                print("All examined attributes are already populated.")
            print("=" * 90)
            return cand_df

        # Apply Mode
        if candidates:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{target_path}.bak_{ts}"
            shutil.copy2(target_path, backup_path)
            print(f"\n[BACKUP CREATED]: {backup_path}")

            for c in candidates:
                idx = c["index"]
                fld = c["field"]
                val = c["proposed_val"]
                df_local.at[idx, fld] = val
                if "field_year" in c and c["field_year"] and "proposed_year" in c:
                    df_local.at[idx, c["field_year"]] = c["proposed_year"]
                applied_count += 1

            df_local.to_csv(target_path, index=False)
            print(f"[APPLIED SUCCESSFULLY]: Updated {applied_count} values in {target_path}")
        else:
            print("\nNo candidate fills to apply.")

        return cand_df


def parse_args():
    parser = argparse.ArgumentParser(
        description="FHWA HPMS & Caltrans PeMS Multi-Source Ground-Truth Interface for CERCET Project Validation."
    )
    # Target project
    parser.add_argument("--project", "-p", "--inspect", type=int,
                        help="Target Project ID to inspect with HPMS & PeMS data (includes full description).")
    parser.add_argument("--direction", "-dir", type=str, default=None,
                        help="Direction of travel (e.g. NB, SB, EB, WB) for directional corridor queries or dual-row projects.")
    parser.add_argument("--tandem", "-t", action="store_true",
                        help="Run triad verification comparing Spreadsheet, PeMS, and HPMS side by side.")
    parser.add_argument("--map", "-m", nargs="?", const=-1, type=int,
                        help="Generate interactive HTML map with HPMS road segments colored by lane count (e.g. --map 713000217).")
    parser.add_argument("--audit", "-a", action="store_true",
                        help="Audit project lane counts against HPMS ground truth (single project with -p, or district with --dist).")
    parser.add_argument("--audit-all", action="store_true",
                        help="Run full triad audit statewide across all projects in the master dataset.")
    parser.add_argument("--dist", "-d", type=int,
                        help="Caltrans District number for district-wide audit (e.g. --audit --dist 7).")
    parser.add_argument("--fill-local", action="store_true",
                        help="Preview candidate values to fill missing attributes in the local master spreadsheet.")
    parser.add_argument("--apply-local", action="store_true",
                        help="Apply high-confidence fills directly to local spreadsheet (staging/Master_CA_Project_List_Cleaned.csv) with backup.")
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

    # 1. Fill Local Spreadsheet Option
    if args.fill_local or args.apply_local:
        engine.fill_local_spreadsheet(
            target_path=args.input,
            project_id=args.project,
            district=args.dist,
            apply=args.apply_local
        )
        return

    # Determine target PID
    pid = args.project
    if args.map and args.map != -1:
        pid = args.map

    if pid:
        # Check if project has directional rows in master_df
        matching_rows = engine.master_df[
            engine.master_df["Project ID"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True) == str(pid)
        ]
        if args.direction:
            directions_to_audit = [args.direction]
        else:
            dirs = matching_rows["Direction"].dropna().unique().tolist() if "Direction" in matching_rows.columns else []
            directions_to_audit = dirs if len(dirs) > 1 else [None]

        for dir_target in directions_to_audit:
            if args.map is not None:
                # Generate map
                out_file = engine.generate_hpms_map(pid, output_html=args.output_map)
                print(f"\nSuccessfully generated HPMS corridor map for Project {pid}:")
                print(f"  {out_file}")
                # Also show tandem summary
                res = engine.tandem_audit_project(pid, direction=dir_target)
                print(engine.format_tandem_output(res))
            elif args.audit:
                # Audit single project (historical HPMS audit)
                audit_res = engine.audit_project(pid, direction=dir_target)
                print(engine.format_audit_output(audit_res))
            else:
                # Default: triad multi-source tandem verification!
                tandem_res = engine.tandem_audit_project(pid, direction=dir_target)
                print(engine.format_tandem_output(tandem_res))

    elif args.audit or args.audit_all:
        # District-wide or statewide audit
        report_df = engine.audit_district(district=args.dist)
        out_path = args.output_report or os.path.join(WORKSPACE_DIR, f"hpms_audit_report_dist_{args.dist or 'all'}.csv")
        report_df.to_csv(out_path, index=False)
        print(f"\nAudit complete! Saved side-by-side report to:\n  {out_path}")
        print("\nAudit Summary by Status:")
        if "Lane Verdict" in report_df.columns:
            print(report_df["Lane Verdict"].value_counts())
        elif "Audit Status" in report_df.columns:
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
        print("CERCET Multi-Source Triad Interface (Spreadsheet vs PeMS vs HPMS)")
        print("Usage:")
        print("  python hpms_interface.py -p <PID>            # Triad verification (Spreadsheet vs PeMS vs HPMS side-by-side)")
        print("  python hpms_interface.py -p <PID> --map      # Interactive Folium map with HPMS layer popups")
        print("  python hpms_interface.py --audit --dist <D>  # District-wide triad audit with side-by-side output")
        print("  python hpms_interface.py --audit-all         # Statewide triad audit across all projects")
        print("  python hpms_interface.py --fill-local        # Preview ground-truth fills for local spreadsheet")
        print("  python hpms_interface.py --apply-local       # Apply ground-truth fills to local spreadsheet with backup")
        print("\nRun `python hpms_interface.py --help` for full options.")


if __name__ == "__main__":
    main()
