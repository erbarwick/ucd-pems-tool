#!/usr/bin/env python3
"""
pems_interface.py
-----------------
Consolidated, High-Performance PeMS Interface & Query Engine.

Consolidates and optimizes all PeMS interaction code across the CERCET California project:
  - Robust session management with streaming downloads and deterministic re-authentication.
  - O(1) indexed geometric reference lookups across 331,888 segments.
  - Strict input validation and regex parsing for postmiles, directions, and routes.
  - Validated URL builders conforming to Caltrans PeMS parameters.
  - Master project CSV integration (Dist, County, State, coordinates, route, direction, PMs).

=============================================================================
EXAMPLE USAGE
=============================================================================

1. Python Module API:
---------------------
  from pems_interface import (
      PeMSClient,
      load_master_csv,
      ProjectPeMSQueryBuilder,
      pm_to_coordinates,
      build_lane_config_url,
      build_station_aadt_url,
      build_station_flow_url,
      find_closest_census_stations,
  )

  # A. Postmile to Coordinate Conversion (O(1) indexed lookup):
  coords = pm_to_coordinates(route="80", direction="E", pm="11.4", county="Solano")
  # Returns: (38.201952, -122.156967)

  # B. Generate PeMS Download URLs:
  lane_url = build_lane_config_url(
      route="80", direction="E", start_date="2021-01-01", end_date="2024-12-31"
  )
  aadt_url = build_station_aadt_url(station_id="410756", start_year=2021, end_year=2022)
  flow_url = build_station_flow_url(station_id="410756", start_year=2021, granularity="month")

  # C. Process Projects from Master CSV:
  projects = load_master_csv("staging/Master_CA_Project_List_Cleaned.csv")
  for proj in projects:
      builder = ProjectPeMSQueryBuilder(proj)
      start_coords, end_coords = builder.resolve_geometry()
      urls = builder.generate_all_urls()
      print(f"Project {proj.project_id}: {urls['corridor']}")
      print(f"  Lane config URL: {urls['lane_config_url']}")

  # D. Authenticate & Download from PeMS:
  client = PeMSClient()  # Reads PEMS_USERNAME and PEMS_PASSWORD from .env
  client.login()
  client.download_file(url=lane_url, out_path="downloads/lane_config_80E.txt")

  # E. Find Closest Caltrans Census Stations:
  census_stations = find_closest_census_stations(route="80", district="4", ca_pm=11.4, top_n=3)

  # F. Interactive Map Generation (Folium / Leaflet with hover & popups):
  from pems_interface import build_single_project_map, build_multi_project_map
  # Map a single project corridor and open in browser:
  build_single_project_map("418000244", output_path="map_418000244.html", open_browser=True)
  # Map all 118 projects across California with district layer toggles:
  build_multi_project_map(output_path="all_projects.html", open_browser=True)

  # G. Match Active PeMS VDS Detector Stations (Dry Run by Default):
  from pems_interface import run_station_comparison_pipeline, load_vds_metadata
  results = run_station_comparison_pipeline(dry_run=True, output_report_path="reports/station_comparison.csv")

2. Command-Line Interface (CLI):
--------------------------------
  # Test PeMS credentials and login
  python pems_interface.py --test-login

  # Compare existing sheet stations vs active PeMS corridor detectors (DRY RUN by default)
  python pems_interface.py --match-stations

  # Run dry run and export detailed comparison report (CSV or Markdown)
  python pems_interface.py --match-stations --dry-run --output-report reports/station_comparison.csv

  # Match stations filtered to a specific District or Project ID
  python pems_interface.py --match-stations --dist 4
  python pems_interface.py --match-stations -p 300020420

  # Customize postmile buffer tolerance and station types
  python pems_interface.py --match-stations --buffer 0.2 --station-types ML,HV

  # Commit matched corridor stations to master CSV (creates automated safety backup)
  python pems_interface.py --match-stations --apply

  # Inspect a specific project by Project ID (PID) with full VDS detector audit
  python pems_interface.py --inspect 418000244

  # Inspect a project and find closest Census stations
  python pems_interface.py --inspect 300000061 --census

  # Map a specific project as a road path and open in browser
  python pems_interface.py --map 418000244

  # Map ALL 118 projects across California and open in browser
  python pems_interface.py --map-all

  # Map all projects in a specific District (e.g. District 4 - Bay Area)
  python pems_interface.py --map-all --dist 4

  # Ad-hoc query for coordinates and corridor segments
  python pems_interface.py --route 80 --dir E --begin-pm 11.4 --end-pm 12.8 --county Solano

  # Generate PeMS URLs for projects in the master CSV
  python pems_interface.py --list-urls

  # Run automated test suite
  python -m unittest tests/test_pems_interface.py -v
=============================================================================
"""

import os
import sys
import re
import glob
import shutil
import time
import calendar
import datetime
import logging
import argparse
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
import numpy as np
import folium
import webbrowser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIGURATION & PATHS
# ---------------------------------------------------------------------------
load_dotenv()

BASE_URL = "https://pems.dot.ca.gov"
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_REF_PATH = os.path.join(WORKSPACE_DIR, "downloads", "merged_geo_pm.csv")
DEFAULT_ROUTES_DIRS = os.path.join(WORKSPACE_DIR, "ca_routes_dirs.csv")
DEFAULT_MASTER_CSV = os.path.join(WORKSPACE_DIR, "staging", "Master_CA_Project_List_Cleaned.csv")
DEFAULT_METADATA_DIR = os.path.join(WORKSPACE_DIR, "downloads", "metadata")

CENSUS_PATHS = [
    os.path.join(WORKSPACE_DIR, "ALL_CENSUS_STATIONS.tsv"),
    os.path.join(WORKSPACE_DIR, "downloads", "ALL_CENSUS_STATIONS.tsv"),
    os.path.abspath(os.path.join(WORKSPACE_DIR, "..", "..", "..", "notes", "AADT Comparisons", "data", "ALL_CENSUS_STATIONS.tsv"))
]

TABLE_MARKERS = ("Starting Month", "Month", "Day", "Time", "Station", "VDS", "Hour")
VALID_CARDINAL_DIRS = {"N", "S", "E", "W"}

LOGGER = logging.getLogger("pems_interface")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    stream=sys.stdout
)


# ---------------------------------------------------------------------------
# NORMALIZATION & PARSING HELPERS
# ---------------------------------------------------------------------------
def normalize_direction(d: Any) -> str:
    """
    Normalize direction string to single-letter PeMS cardinal direction: 'N', 'S', 'E', 'W'.
    Rejects invalid inputs by returning an empty string.
    """
    if not d or pd.isna(d):
        return ""
    s = str(d).strip().upper()
    if s in ("N", "NB", "NORTH", "NORTHBOUND"):
        return "N"
    if s in ("S", "SB", "SOUTH", "SOUTHBOUND"):
        return "S"
    if s in ("E", "EB", "EAST", "EASTBOUND"):
        return "E"
    if s in ("W", "WB", "WEST", "WESTBOUND"):
        return "W"
    return ""


def normalize_route(r: Any) -> str:
    """Normalize route string to digits: 'SR-80' -> '80', 'I-5' -> '5'."""
    if not r or pd.isna(r):
        return ""
    return re.sub(r"[^0-9]", "", str(r))


def format_pm(val: Any) -> str:
    """
    Format a postmile cleanly, eliminating floating-point artifacts (e.g. 8.6999999999999993 -> 8.7).
    Preserves letter prefixes (e.g. R, M, L), suffixes (e.g. L, R), and strips route prefixes.
    """
    if val is None or pd.isna(val) or val == "":
        return ""
    s = str(val).strip()
    s = re.sub(r"^(?:I|SR|US|RTE|ROUTE|HWY)\s*[-]?\s*\d+\s*(?:PM)?\s*", "", s, flags=re.IGNORECASE).strip()
    m = re.match(r"^([A-Za-z\s\-]*?)([0-9]+(?:\.[0-9]+)?)([A-Za-z\s\-]*)$", s)
    if m:
        pfx = m.group(1).strip()
        num = float(m.group(2))
        sfx = m.group(3).strip()
        rounded = round(num, 3)
        num_str = f"{rounded:.3f}".rstrip("0").rstrip(".")
        return f"{pfx}{num_str}{sfx}"
    try:
        num = float(s)
        rounded = round(num, 3)
        return f"{rounded:.3f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return s


def split_ca_pm(ca_pm: Any) -> Tuple[str, float]:
    """
    Split a California postmile string into prefix and float number.
    Handles letter prefixes (R, L, M) and suffix letters (L, R, A),
    as well as embedded route markers (e.g. 'I-10 30.3').
    e.g. 'R3.80' -> ('R', 3.8), '12.4L' -> ('', 12.4), 'R12.4A' -> ('R', 12.4)
    """
    if ca_pm is None or (isinstance(ca_pm, float) and np.isnan(ca_pm)):
        return "", np.nan
    s = str(ca_pm).strip()
    if not s:
        return "", np.nan

    s_clean = re.sub(r"^(?:I|SR|US|RTE|ROUTE|HWY)\s*[-]?\s*\d+\s*(?:PM)?\s*", "", s, flags=re.IGNORECASE).strip()
    match = re.match(r"^([A-Za-z\s\-]*?)([0-9]+(?:\.[0-9]+)?)([A-Za-z\s\-]*)$", s_clean)
    if match:
        pfx = match.group(1).strip()
        num = round(float(match.group(2)), 4)
        return pfx, num

    try:
        return "", round(float(s_clean), 4)
    except (TypeError, ValueError):
        return s, np.nan


# ---------------------------------------------------------------------------
# PEMS AUTHENTICATED CLIENT
# ---------------------------------------------------------------------------
class PeMSClient:
    """Manages an authenticated HTTP session with Caltrans PeMS."""

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None, debug: bool = False):
        # Strict distinction between explicit argument and env fallback
        self.username = os.getenv("PEMS_USERNAME") if username is None else username
        self.password = os.getenv("PEMS_PASSWORD") if password is None else password
        self.debug = debug
        self.session = requests.Session()
        self._setup_session()
        self.logged_in = False

    def _setup_session(self):
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        self.session.verify = False
        requests.packages.urllib3.disable_warnings()

    def login(self) -> bool:
        """Authenticate against PeMS."""
        if not self.username or not self.password:
            raise ValueError(
                "PeMS username and password must be provided or set in environment variables (PEMS_USERNAME, PEMS_PASSWORD)."
            )

        LOGGER.info(f"Connecting to PeMS ({BASE_URL})...")
        resp = self.session.get(BASE_URL)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise RuntimeError("Login form not found on PeMS main page.")

        action = form.get("action", "/")
        action_url = action if action.startswith("http") else urllib.parse.urljoin(BASE_URL, action)

        login_data = {
            "redirect": "",
            "username": self.username,
            "password": self.password,
            "login": "Login"
        }

        LOGGER.info(f"Submitting credentials for {self.username}...")
        resp = self.session.post(action_url, data=login_data, allow_redirects=True)
        resp.raise_for_status()

        if "Logout" not in resp.text and "logout" not in resp.text.lower():
            if self.debug:
                with open("debug_pems_login_failure.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
            raise RuntimeError("Login failed: no 'Logout' link found in response.")

        self.logged_in = True
        LOGGER.info("PeMS login successful.")
        return True

    def _is_login_page(self, resp: requests.Response) -> bool:
        """Check if response contains a login form and lacks an active session."""
        has_form = ("<form" in resp.text and ("username" in resp.text or "password" in resp.text))
        has_logout = ("Logout" in resp.text or "logout" in resp.text)
        return has_form and not has_logout

    def get_url(self, url: str, stream: bool = False) -> requests.Response:
        """Make an authenticated GET request, re-authenticating if session expired."""
        if not self.logged_in:
            self.login()

        full_url = url if url.startswith("http") else urllib.parse.urljoin(BASE_URL, url)
        resp = self.session.get(full_url, stream=stream)
        resp.raise_for_status()

        if self._is_login_page(resp):
            LOGGER.warning("PeMS session appears expired; re-authenticating...")
            self.login()
            resp = self.session.get(full_url, stream=stream)
            resp.raise_for_status()

        return resp

    def get_text_content(self, url: str) -> Optional[str]:
        """Fetch content as decoded text."""
        try:
            resp = self.get_url(url)
            return resp.text
        except Exception as exc:
            LOGGER.error(f"Error fetching {url}: {exc}")
            return None

    def download_file(self, url: str, out_path: str, overwrite: bool = False) -> bool:
        """Download file with chunk streaming to prevent memory exhaustion."""
        if os.path.exists(out_path) and not overwrite:
            if self.file_has_data_table(out_path):
                LOGGER.info(f"File {out_path} already exists with valid data - skipping.")
                return True

        LOGGER.info(f"Downloading from {url} to {out_path}...")
        try:
            resp = self.get_url(url, stream=True)
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)

            if not self.file_has_data_table(out_path):
                LOGGER.warning(f"Downloaded file {out_path} does not appear to contain a valid PeMS data table.")
            return True
        except Exception as exc:
            LOGGER.error(f"Failed to download {url}: {exc}")
            return False

    @staticmethod
    def file_has_data_table(path_or_content: Any) -> bool:
        """Validate whether a file or string contains an actual PeMS data table rather than an HTML error."""
        try:
            if os.path.isfile(str(path_or_content)):
                with open(str(path_or_content), "r", encoding="utf-8", errors="replace") as fh:
                    lines = [fh.readline() for _ in range(50)]
            else:
                lines = str(path_or_content).splitlines()[:50]

            for line in lines:
                s = line.strip()
                if any(s.startswith(m) for m in TABLE_MARKERS) and ("\t" in line or "," in line):
                    return True
        except Exception:
            pass
        return False

    @classmethod
    def parse_table_content(cls, content: str) -> Optional[pd.DataFrame]:
        """Parse raw PeMS export text into a pandas DataFrame."""
        if not content:
            return None
        lines = content.splitlines()
        header_idx = None
        for i, line in enumerate(lines[:60]):
            s = line.strip()
            if any(s.startswith(m) for m in TABLE_MARKERS) and ("\t" in line or "," in line):
                header_idx = i
                break
        if header_idx is None:
            return None

        sep = "\t" if "\t" in lines[header_idx] else ","
        try:
            import io
            df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=sep)
            df.columns = df.columns.str.strip()
            return df
        except Exception as exc:
            LOGGER.error(f"Error parsing PeMS table: {exc}")
            return None


# ---------------------------------------------------------------------------
# PEMS URL BUILDERS
# ---------------------------------------------------------------------------
def _format_date_param(d_str: str) -> str:
    """Format input date string to MM/DD/YYYY for PeMS URLs."""
    d_clean = str(d_str).strip()
    if "-" in d_clean:
        try:
            dt = datetime.datetime.strptime(d_clean[:10], "%Y-%m-%d")
            return f"{dt.month:02d}%2F{dt.day:02d}%2F{dt.year}"
        except ValueError:
            pass
    elif "/" in d_clean:
        parts = d_clean.split("/")
        if len(parts) == 3:
            try:
                return f"{int(parts[0]):02d}%2F{int(parts[1]):02d}%2F{int(parts[2])}"
            except ValueError:
                pass
    return urllib.parse.quote(d_clean)


def build_lane_config_url(
    route: str,
    direction: str,
    start_date: str,
    end_date: str,
    station_type: str = ""
) -> str:
    """Construct PeMS Freeway configuration time series URL."""
    route_num = normalize_route(route)
    dir_clean = normalize_direction(direction)
    s_fmt = _format_date_param(start_date)
    e_fmt = _format_date_param(end_date)

    return (
        f"{BASE_URL}/?report_form=1&dnode=Freeway&content=elv&tab=config_timeseries"
        f"&export=text&fwy={route_num}&dir={dir_clean}"
        f"&s_time_id_f={s_fmt}&e_time_id_f={e_fmt}&q=lanes&gn=day&station_type={station_type}"
    )


def build_station_aadt_url(
    station_id: str,
    start_year: int,
    end_year: Optional[int] = None
) -> str:
    """Construct PeMS VDS annual AADT export URL."""
    st_id = str(station_id).strip()
    end_year = end_year or start_year

    jan_1 = datetime.datetime(start_year, 1, 1, 0, 0, 0)
    dec_31 = datetime.datetime(end_year, 12, 31, 23, 59, 59)
    s_time_id = int(calendar.timegm(jan_1.timetuple()))
    e_time_id = int(calendar.timegm(dec_31.timetuple()))

    return (
        f"{BASE_URL}/?report_form=1&dnode=VDS&content=analysis&tab=aadt&export=text"
        f"&station_id={st_id}&s_time_id={s_time_id}&s_time_id_f=01%2F{start_year}"
        f"&e_time_id={e_time_id}&e_time_id_f=12%2F{end_year}"
    )


def build_station_flow_url(
    station_id: str,
    start_year: int,
    end_year: Optional[int] = None,
    granularity: str = "month"
) -> str:
    """Construct PeMS VDS detector time series (Flow) export URL."""
    st_id = str(station_id).strip()
    end_year = end_year or start_year

    jan_1 = datetime.datetime(start_year, 1, 1, 0, 0, 0)
    dec_31 = datetime.datetime(end_year, 12, 31, 23, 59, 0)
    s_time_id = int(calendar.timegm(jan_1.timetuple()))
    e_time_id = int(calendar.timegm(dec_31.timetuple()))

    s_fmt = urllib.parse.quote(f"01/01/{start_year} 00:00")
    e_fmt = urllib.parse.quote(f"12/31/{end_year} 23:59")

    return (
        f"{BASE_URL}/?report_form=1&dnode=VDS&content=loops&tab=det_timeseries&export=text"
        f"&station_id={st_id}&s_time_id={s_time_id}&s_time_id_f={s_fmt}"
        f"&e_time_id={e_time_id}&e_time_id_f={e_fmt}"
        f"&tod=all&tod_from=0&tod_to=0"
        f"&dow_0=on&dow_1=on&dow_2=on&dow_3=on&dow_4=on&dow_5=on&dow_6=on"
        f"&holidays=on&q=flow&q2=&gn={granularity}"
    )


def build_fwy_latlng_url(route: str, direction: str) -> str:
    """Construct PeMS Freeway coordinate geometry URL."""
    route_num = normalize_route(route)
    dir_clean = normalize_direction(direction)
    return f"{BASE_URL}/?report_form=1&dnode=Freeway&content=fwy_latlngXY&export=text&fwy={route_num}&dir={dir_clean}"


def build_station_summary_url(station_id: str) -> str:
    """Construct PeMS VDS Summary / Metadata URL."""
    st_id = str(station_id).strip()
    return f"{BASE_URL}/?report_form=1&dnode=VDS&content=summary&tab=summary&station_id={st_id}"


def build_clearinghouse_url(district: Any, year: int, file_type: str = "station_aadt") -> str:
    """Construct PeMS Clearinghouse bulk download URL."""
    dist_clean = str(district).replace(".0", "").strip()
    return f"{BASE_URL}/?srq=clearinghouse&district_id={dist_clean}&yy={year}&type={file_type}&returnformat=text"


# ---------------------------------------------------------------------------
# INDEXED GEOMETRY & REFERENCE LOOKUP ENGINE
# ---------------------------------------------------------------------------
class IndexedGeoReference:
    """
    O(1) Indexed geometric database. Pre-processes the 331K-row reference table
    once at startup to avoid repeated expensive pandas column scanning.
    """

    def __init__(self, df: pd.DataFrame):
        self.raw_df = df
        route_col = "Route" if "Route" in df.columns else "Fwy"
        dir_col = "Direction" if "Direction" in df.columns else "Dir"

        # Pre-normalize columns
        clean_df = df.copy()
        clean_df["_route"] = clean_df[route_col].astype(str).str.strip().map(normalize_route)
        clean_df["_dir"] = clean_df[dir_col].astype(str).str.strip().map(normalize_direction)
        clean_df["_abs_pm"] = pd.to_numeric(clean_df["Abs PM"], errors="coerce")
        clean_df["_county"] = clean_df["County"].astype(str).str.strip().str.lower()

        # Parse CA PM prefix and numbers once
        ca_splits = clean_df["CA PM"].astype(str).map(split_ca_pm)
        clean_df["_ca_pfx"] = ca_splits.map(lambda x: x[0].upper())
        clean_df["_ca_num"] = ca_splits.map(lambda x: x[1])

        clean_df["_lat"] = pd.to_numeric(clean_df["Latitude"], errors="coerce")
        clean_df["_lng"] = pd.to_numeric(clean_df["Longitude"], errors="coerce")

        # Partition by (Route, Direction)
        self.index: Dict[Tuple[str, str], pd.DataFrame] = {}
        for (r, d), grp in clean_df.groupby(["_route", "_dir"]):
            self.index[(r, d)] = grp

    def get_corridor(self, route: str, direction: str) -> Optional[pd.DataFrame]:
        r_norm = normalize_route(route)
        d_norm = normalize_direction(direction)
        return self.index.get((r_norm, d_norm))


_INDEXED_GEO_REF: Optional[IndexedGeoReference] = None
_RAW_GEO_DF: Optional[pd.DataFrame] = None
_CENSUS_CACHE: Optional[pd.DataFrame] = None


def load_geo_reference(ref_path: Optional[str] = None) -> pd.DataFrame:
    """Load and index the 331K-row reference table."""
    global _INDEXED_GEO_REF, _RAW_GEO_DF
    if _RAW_GEO_DF is not None:
        return _RAW_GEO_DF

    path = ref_path or DEFAULT_REF_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"Reference table not found at: {path}")

    for sep in (",", "\t"):
        try:
            df = pd.read_csv(path, sep=sep, dtype=str)
            df.columns = df.columns.str.strip()
            if "Route" in df.columns or "Fwy" in df.columns:
                _RAW_GEO_DF = df
                _INDEXED_GEO_REF = IndexedGeoReference(df)
                return _RAW_GEO_DF
        except Exception:
            continue

    raise ValueError(f"Could not parse valid reference columns in {path}")


def get_indexed_geo_ref() -> IndexedGeoReference:
    global _INDEXED_GEO_REF
    if _INDEXED_GEO_REF is None:
        load_geo_reference()
    return _INDEXED_GEO_REF


def load_census_reference() -> Optional[pd.DataFrame]:
    """Load and cache the Caltrans Census stations reference TSV if available."""
    global _CENSUS_CACHE
    if _CENSUS_CACHE is not None:
        return _CENSUS_CACHE

    for p in CENSUS_PATHS:
        if os.path.exists(p):
            try:
                df = pd.read_csv(p, sep="\t", dtype=str)
                df.columns = df.columns.str.strip()
                _CENSUS_CACHE = df
                return _CENSUS_CACHE
            except Exception:
                pass
    return None


def pm_to_coordinates(
    route: str,
    direction: str,
    pm: Any,
    county: Optional[str] = None,
    is_absolute: bool = False,
    ref_df: Optional[pd.DataFrame] = None
) -> Optional[Tuple[float, float]]:
    """Convert postmile to (latitude, longitude) with O(1) indexed lookup."""
    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction)

    if ref_df is not None:
        if isinstance(ref_df, IndexedGeoReference):
            idx_ref = ref_df
        else:
            idx_ref = IndexedGeoReference(ref_df)
    else:
        idx_ref = get_indexed_geo_ref()

    sub = idx_ref.get_corridor(route_norm, dir_norm)
    if sub is None or sub.empty:
        return None

    if is_absolute or county is None:
        try:
            pm_val = round(float(pm), 1)
        except (TypeError, ValueError):
            return None
        matched = sub[sub["_abs_pm"].round(1) == pm_val]
    else:
        prefix, num = split_ca_pm(pm)
        if np.isnan(num):
            return None
        num = round(num, 1)
        county_clean = str(county).strip().lower()

        matched = sub[
            (sub["_ca_num"].round(1) == num) &
            (sub["_county"] == county_clean)
        ]
        if prefix:
            pfx_matched = matched[matched["_ca_pfx"] == prefix.upper()]
            if not pfx_matched.empty:
                matched = pfx_matched

    if matched.empty:
        return None

    row = matched.iloc[0]
    lat, lng = row["_lat"], row["_lng"]
    if pd.isna(lat) or pd.isna(lng):
        return None
    return float(lat), float(lng)


def get_corridor_stations(
    route: str,
    direction: str,
    begin_pm: float,
    end_pm: float,
    is_absolute: bool = True,
    county: Optional[str] = None,
    ref_df: Optional[Any] = None
) -> pd.DataFrame:
    """Find all reference segments along a corridor range with O(1) indexed lookup."""
    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction)

    if ref_df is not None:
        if isinstance(ref_df, IndexedGeoReference):
            idx_ref = ref_df
        else:
            idx_ref = IndexedGeoReference(ref_df)
    else:
        idx_ref = get_indexed_geo_ref()

    sub = idx_ref.get_corridor(route_norm, dir_norm)
    if sub is None or sub.empty:
        return pd.DataFrame()

    min_pm, max_pm = min(float(begin_pm), float(end_pm)), max(float(begin_pm), float(end_pm))

    if is_absolute or county is None:
        matched = sub[(sub["_abs_pm"] >= min_pm - 0.05) & (sub["_abs_pm"] <= max_pm + 0.05)]
    else:
        county_clean = str(county).strip().lower()
        matched = sub[
            (sub["_ca_num"] >= min_pm - 0.05) &
            (sub["_ca_num"] <= max_pm + 0.05) &
            (sub["_county"] == county_clean)
        ]

    # Return view with original columns
    orig_cols = [c for c in sub.columns if not c.startswith("_")]
    return matched[orig_cols]


def find_closest_census_stations(
    route: str,
    district: Any,
    ca_pm: float,
    top_n: int = 5
) -> pd.DataFrame:
    """Find closest Caltrans Census stations to a corridor postmile."""
    cdf = load_census_reference()
    if cdf is None or cdf.empty:
        return pd.DataFrame()

    r_num = normalize_route(route)
    d_clean = str(district).replace(".0", "").strip()

    df = cdf.copy()
    fwy_num = df["Fwy"].astype(str).str.replace(r"[^0-9]", "", regex=True)
    dist_num = pd.to_numeric(df["District"], errors="coerce").fillna(0).astype(int).astype(str)

    matched = df[(fwy_num == r_num) & ((dist_num == d_clean) | (dist_num == "0"))].copy()
    if matched.empty:
        return pd.DataFrame()

    matched["pm_num"] = matched["CA PM"].astype(str).map(lambda x: split_ca_pm(x)[1])
    matched["pm_diff"] = (matched["pm_num"] - float(ca_pm)).abs()
    matched = matched.dropna(subset=["pm_diff"]).sort_values(by="pm_diff").drop_duplicates(subset=["ID"])
    return matched.head(top_n)


# ---------------------------------------------------------------------------
# PROJECT RECORD & MASTER CSV PARSER
# ---------------------------------------------------------------------------
@dataclass
class ProjectRecord:
    """Structured representation of a highway capacity project row."""
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

    @property
    def pems_direction(self) -> str:
        return normalize_direction(self.direction)

    @property
    def start_year(self) -> int:
        for val in (self.construction_begin_date, self.raw_row.get("construction_begin_year")):
            if val and pd.notna(val):
                try:
                    y = int(str(val)[:4])
                    if 1990 <= y <= 2030:
                        return y
                except ValueError:
                    pass
        return 2013

    @property
    def end_year(self) -> int:
        for val in (self.open_to_traffic_date, self.raw_row.get("open_traffic_year")):
            if val and pd.notna(val):
                try:
                    y = int(str(val)[:4])
                    if 1990 <= y <= 2030:
                        return min(y + 4, 2026)
                except ValueError:
                    pass
        return 2024


def parse_master_row(row: Dict[str, Any], row_index: int = -1) -> ProjectRecord:
    """Convert a dictionary row from the master CSV into a clean ProjectRecord."""
    pid = str(row.get("Project ID", "")).strip()
    dist = str(row.get("Dist", "")).replace(".0", "").strip()
    county = str(row.get("County", "")).strip()
    state = str(row.get("State", "CA")).strip()
    route = str(row.get("Route", "")).strip()
    direction = str(row.get("Direction", "")).strip().upper()
    begin_pm = format_pm(str(row.get("Begin PM", "")).strip())
    end_pm = format_pm(str(row.get("End PM", "")).strip())

    # Absolute PMs
    abs_beg = None
    abs_end = None
    for k in ("temp_abs_begin", "Absolute Begin PM", "Abs Begin PM"):
        if k in row and pd.notna(row[k]):
            try:
                abs_beg = round(float(row[k]), 3)
                break
            except ValueError:
                pass

    for k in ("temp_abs_end", "Absolute End PM", "Abs End PM"):
        if k in row and pd.notna(row[k]):
            try:
                abs_end = round(float(row[k]), 3)
                break
            except ValueError:
                pass

    # Coordinates
    start_coords = None
    end_coords = None
    for coord_col in ("Start Coordinates", "Coordinates"):
        val = str(row.get(coord_col, "")).strip()
        if "," in val:
            parts = val.split(",")
            try:
                start_coords = (float(parts[0].strip()), float(parts[1].strip()))
                break
            except ValueError:
                pass

    end_val = str(row.get("End Coordinates", "")).strip()
    if "," in end_val:
        parts = end_val.split(",")
        try:
            end_coords = (float(parts[0].strip()), float(parts[1].strip()))
        except ValueError:
            pass

    # Dates
    c_begin = None
    for k in ("Begin Date (formatted YYYY-MM-DD)", "Construction Begin Date"):
        v = str(row.get(k, "")).strip()
        if v and v.lower() not in ("nan", "none", "") and not v.isdigit():
            c_begin = v
            break

    o_traffic = None
    for k in ("Open to Traffic Date (formatted YYYY-MM-DD)", "Open to Traffic Date"):
        v = str(row.get(k, "")).strip()
        if v and v.lower() not in ("nan", "none", "") and not (v.isdigit() and len(v) > 4):
            o_traffic = v
            break

    # Sensor stations
    sensor_stations = []
    for col in (
        "Sensor Station 1 ID ", "Sensor Station 1 ID",
        "Sensor Station 1 HOV ID",
        "Sensor Station 2 ID ", "Sensor Station 2 ID",
        "Sensor Station 3 ID ", "Sensor Station 3 ID",
        "Sensor Station 4 ID ", "Sensor Station 4 ID"
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
        row_index=row_index
    )


def load_master_csv(csv_path: Optional[str] = None) -> List[ProjectRecord]:
    """Load and parse the master project CSV into a list of ProjectRecord objects."""
    path = csv_path or DEFAULT_MASTER_CSV
    if not os.path.exists(path):
        raise FileNotFoundError(f"Master CSV file not found: {path}")

    df = pd.read_csv(path, dtype=str)
    records = []
    for idx, row in df.iterrows():
        records.append(parse_master_row(row.to_dict(), row_index=int(idx)))
    return records


# ---------------------------------------------------------------------------
# PROJECT PEMS QUERY & DISPATCH GENERATOR
# ---------------------------------------------------------------------------
class ProjectPeMSQueryBuilder:
    """Builds and resolves PeMS queries for a given ProjectRecord."""

    def __init__(self, project: ProjectRecord, ref_df: Optional[Any] = None):
        self.project = project
        if isinstance(ref_df, IndexedGeoReference):
            self.idx_ref = ref_df
            self.ref_df = ref_df.raw_df
        elif ref_df is not None:
            self.idx_ref = IndexedGeoReference(ref_df)
            self.ref_df = ref_df
        else:
            self.idx_ref = get_indexed_geo_ref()
            self.ref_df = self.idx_ref.raw_df

    def resolve_geometry(self) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
        """Resolve start and end coordinates from postmiles if not already present."""
        start = self.project.start_coords
        end = self.project.end_coords

        is_abs = self.project.abs_begin_pm is not None and self.project.abs_end_pm is not None
        b_pm = self.project.abs_begin_pm if is_abs else self.project.begin_pm
        e_pm = self.project.abs_end_pm if is_abs else self.project.end_pm

        if start is None and b_pm:
            start = pm_to_coordinates(
                self.project.route, self.project.pems_direction, b_pm,
                county=self.project.county, is_absolute=is_abs, ref_df=self.idx_ref
            )
        if end is None and e_pm:
            end = pm_to_coordinates(
                self.project.route, self.project.pems_direction, e_pm,
                county=self.project.county, is_absolute=is_abs, ref_df=self.idx_ref
            )
        return start, end

    def get_corridor_stations_from_reference(self) -> List[Dict[str, Any]]:
        """Find stations and segments along the project corridor from the reference dataset."""
        b_pm = self.project.abs_begin_pm
        e_pm = self.project.abs_end_pm
        is_abs = True

        if b_pm is None or e_pm is None:
            _, b_num = split_ca_pm(self.project.begin_pm)
            _, e_num = split_ca_pm(self.project.end_pm)
            b_pm = b_num
            e_pm = e_num
            is_abs = False

        if b_pm is None or e_pm is None or np.isnan(b_pm) or np.isnan(e_pm):
            return []

        df = get_corridor_stations(
            self.project.route, self.project.pems_direction,
            b_pm, e_pm, is_absolute=is_abs, county=self.project.county,
            ref_df=self.idx_ref
        )
        if df.empty:
            return []

        results = []
        id_col = "ID" if "ID" in df.columns else ("Station" if "Station" in df.columns else None)
        for _, r in df.iterrows():
            item = {
                "route": self.project.route,
                "direction": self.project.direction,
                "ca_pm": r.get("CA PM"),
                "abs_pm": r.get("Abs PM"),
                "latitude": r.get("Latitude"),
                "longitude": r.get("Longitude")
            }
            if id_col:
                item["station_id"] = r.get(id_col)
            results.append(item)
        return results

    def generate_all_urls(self, start_year: Optional[int] = None, end_year: Optional[int] = None) -> Dict[str, Any]:
        """Construct all PeMS URLs relevant to this project."""
        s_yr = start_year or self.project.start_year
        e_yr = end_year or self.project.end_year

        start_date_str = f"01/01/{s_yr}"
        end_date_str = f"12/31/{e_yr}"

        lane_config_url = build_lane_config_url(
            self.project.route, self.project.pems_direction,
            start_date=start_date_str, end_date=end_date_str
        )
        geometry_url = build_fwy_latlng_url(self.project.route, self.project.pems_direction)

        station_aadt_urls = {}
        station_flow_urls = {}
        station_summary_urls = {}

        for st_id in self.project.sensor_stations:
            station_aadt_urls[st_id] = [
                (y, build_station_aadt_url(st_id, y))
                for y in range(s_yr, e_yr + 1)
            ]
            station_flow_urls[st_id] = [
                (y, build_station_flow_url(st_id, y))
                for y in range(s_yr, e_yr + 1)
            ]
            station_summary_urls[st_id] = build_station_summary_url(st_id)

        return {
            "project_id": self.project.project_id,
            "corridor": f"Route {self.project.route} {self.project.direction} (PM {self.project.begin_pm} - {self.project.end_pm})",
            "county": self.project.county,
            "district": self.project.dist,
            "year_range": (s_yr, e_yr),
            "lane_config_url": lane_config_url,
            "geometry_url": geometry_url,
            "sensor_stations": self.project.sensor_stations,
            "station_aadt_urls": station_aadt_urls,
            "station_flow_urls": station_flow_urls,
            "station_summary_urls": station_summary_urls
        }


# ---------------------------------------------------------------------------
# VDS DETECTOR METADATA ENGINE & STATION MATCHER
# ---------------------------------------------------------------------------
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
    """
    O(1) Indexed PeMS VDS detector metadata database across all instrumented Caltrans districts.
    Pre-partitions 19,000+ stations by (district, route, direction) and builds an ID index for
    instant diagnostic lookups.
    """
    def __init__(self, df: pd.DataFrame):
        self.raw_df = df
        clean = df.copy()
        clean.columns = clean.columns.str.strip()

        clean["_route"] = clean["Fwy"].astype(str).str.strip().map(normalize_route)
        clean["_dir"] = clean["Dir"].astype(str).str.strip().map(normalize_direction)
        clean["_dist"] = clean["District"].astype(str).str.strip().map(
            lambda x: str(int(float(x))) if str(x).replace(".", "").isdigit() else str(x)
        )
        clean["_abs_pm"] = pd.to_numeric(clean["Abs_PM"], errors="coerce")
        ca_splits = clean["State_PM"].astype(str).map(split_ca_pm)
        clean["_ca_num"] = ca_splits.map(lambda x: x[1])
        clean["_ca_pfx"] = ca_splits.map(lambda x: x[0].upper())
        clean["_lat"] = pd.to_numeric(clean["Latitude"], errors="coerce")
        clean["_lng"] = pd.to_numeric(clean["Longitude"], errors="coerce")
        clean["_lanes"] = pd.to_numeric(clean["Lanes"], errors="coerce")
        clean["_length"] = pd.to_numeric(clean["Length"], errors="coerce")
        clean["_id"] = clean["ID"].astype(str).str.strip()
        clean["_type"] = clean["Type"].astype(str).str.strip().str.upper()

        self.clean_df = clean
        self.index: Dict[Tuple[str, str, str], pd.DataFrame] = {}
        for (dist, r, d), grp in clean.groupby(["_dist", "_route", "_dir"]):
            self.index[(dist, r, d)] = grp

        # Global station ID dictionary
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
                    abs_pm=r["_abs_pm"] if pd.notna(r["_abs_pm"]) else None,
                    latitude=r["_lat"] if pd.notna(r["_lat"]) else None,
                    longitude=r["_lng"] if pd.notna(r["_lng"]) else None,
                    length=r["_length"] if pd.notna(r["_length"]) else None,
                    type=r["_type"],
                    lanes=int(r["_lanes"]) if pd.notna(r["_lanes"]) else None,
                    name=str(r.get("Name", "")).strip()
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
        station_types: Tuple[str, ...] = ("ML", "HV")
    ) -> List[VDSStationRecord]:
        """
        Locate all active PeMS VDS stations along a freeway corridor within the given postmile range.
        """
        dist_str = str(district).replace(".0", "").strip()
        dist_clean = str(int(dist_str)) if dist_str.isdigit() else dist_str
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

        use_abs = (
            abs_begin_pm is not None
            and abs_end_pm is not None
            and not np.isnan(abs_begin_pm)
            and not np.isnan(abs_end_pm)
        )
        if use_abs:
            min_pm = min(float(abs_begin_pm), float(abs_end_pm)) - buffer_mi
            max_pm = max(float(abs_begin_pm), float(abs_end_pm)) + buffer_mi
            matched = sub[(sub["_abs_pm"] >= min_pm) & (sub["_abs_pm"] <= max_pm)].sort_values(by="_abs_pm")
        else:
            _, b_num = split_ca_pm(begin_pm)
            _, e_num = split_ca_pm(end_pm)
            if np.isnan(b_num) or np.isnan(e_num):
                return []
            min_pm = min(b_num, e_num) - buffer_mi
            max_pm = max(b_num, e_num) + buffer_mi
            matched = sub[(sub["_ca_num"] >= min_pm) & (sub["_ca_num"] <= max_pm)].sort_values(by="_ca_num")

        results = []
        for _, row in matched.iterrows():
            sid = row["_id"]
            if sid in self.by_id:
                results.append(self.by_id[sid])
        return results


_INDEXED_VDS_META: Optional[IndexedVDSMetadata] = None


def download_all_district_metadata(
    client: PeMSClient,
    target_dir: str = DEFAULT_METADATA_DIR,
    districts: Tuple[str, ...] = ("3", "4", "5", "6", "7", "8", "10", "11", "12")
) -> Dict[str, str]:
    """Download and cache latest station metadata files for all instrumented Caltrans districts."""
    os.makedirs(target_dir, exist_ok=True)
    if not client.logged_in:
        client.login()

    downloaded = {}
    for d in districts:
        cache_file = os.path.join(target_dir, f"d{int(d):02d}_meta.txt")
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 1000:
            downloaded[d] = cache_file
            continue

        found = False
        for y in (2024, 2023, 2022, 2021, 2020):
            url = f"{BASE_URL}/?srq=clearinghouse&district_id={d}&yy={y}&type=metadata&returnformat=text"
            try:
                resp = client.get_url(url)
                data = resp.json()
                if isinstance(data, dict) and "data" in data and data["data"]:
                    latest_key = sorted(data["data"].keys())[-1]
                    finfo = data["data"][latest_key]["format"]["text"]
                    dl_url = f"{BASE_URL}{finfo['url']}"
                    success = client.download_file(dl_url, cache_file, overwrite=True)
                    if success and os.path.exists(cache_file) and os.path.getsize(cache_file) > 1000:
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
    auto_download: bool = False
) -> IndexedVDSMetadata:
    """Load and index statewide VDS detector metadata from cached text files."""
    global _INDEXED_VDS_META
    if _INDEXED_VDS_META is not None and meta_dir is None:
        return _INDEXED_VDS_META

    target_dir = meta_dir or DEFAULT_METADATA_DIR
    meta_files = sorted(glob.glob(os.path.join(target_dir, "*.txt")))

    if not meta_files and auto_download and client is not None:
        download_all_district_metadata(client, target_dir)
        meta_files = sorted(glob.glob(os.path.join(target_dir, "*.txt")))

    if not meta_files:
        raise FileNotFoundError(f"No PeMS metadata files (*.txt) found in: {target_dir}")

    dfs = []
    for f in meta_files:
        try:
            df = pd.read_csv(f, sep="\t", dtype=str)
            df.columns = df.columns.str.strip()
            if "ID" in df.columns and "Fwy" in df.columns:
                dfs.append(df)
        except Exception as exc:
            LOGGER.warning(f"Could not parse metadata file {f}: {exc}")

    if not dfs:
        raise ValueError(f"No valid metadata tables could be parsed from {target_dir}")

    unified = pd.concat(dfs, ignore_index=True)
    _INDEXED_VDS_META = IndexedVDSMetadata(unified)
    return _INDEXED_VDS_META


def get_indexed_vds_metadata() -> IndexedVDSMetadata:
    global _INDEXED_VDS_META
    if _INDEXED_VDS_META is None:
        load_vds_metadata()
    return _INDEXED_VDS_META


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
    status: str  # EXACT_MATCH, PARTIAL_MATCH, DISCREPANCY, SHEET_EMPTY, UNINSTRUMENTED
    sheet_sensor_count: int
    found_sensor_count: int


def compare_project_stations(
    project: ProjectRecord,
    vds_meta: Optional[IndexedVDSMetadata] = None,
    buffer_mi: float = 0.1,
    station_types: Tuple[str, ...] = ("ML", "HV")
) -> StationComparisonResult:
    """Compare existing spreadsheet stations with PeMS corridor stations and diagnose differences."""
    meta = vds_meta or get_indexed_vds_metadata()
    found = meta.find_corridor_stations(
        district=project.dist,
        route=project.route,
        direction=project.direction,
        begin_pm=project.begin_pm,
        end_pm=project.end_pm,
        abs_begin_pm=project.abs_begin_pm,
        abs_end_pm=project.abs_end_pm,
        buffer_mi=buffer_mi,
        station_types=station_types
    )
    found_ids = [s.id for s in found]
    existing_ids = list(project.sensor_stations)

    matched = [s for s in existing_ids if s in found_ids]
    missing_in_sheet = [s for s in found_ids if s not in existing_ids]
    not_found = [s for s in existing_ids if s not in found_ids]

    discrepancies: List[Dict[str, Any]] = []
    p_dir_norm = project.pems_direction
    p_route_norm = normalize_route(project.route)
    dist_str = str(project.dist).replace(".0", "").strip()
    p_dist_norm = str(int(dist_str)) if dist_str.isdigit() else dist_str

    for sid in not_found:
        if sid in meta.by_id:
            st = meta.by_id[sid]
            reasons = []
            if st.district != p_dist_norm:
                reasons.append(f"Different District (station is Dist {st.district}, project is Dist {p_dist_norm})")
            if st.fwy != p_route_norm:
                reasons.append(f"Different Route (station is Route {st.fwy}, project is Route {p_route_norm})")
            if st.dir != p_dir_norm:
                reasons.append(f"Opposite/Different Direction (station is {st.dir}, project is {p_dir_norm})")
            if station_types and "ALL" not in [t.upper().strip() for t in station_types]:
                if st.type not in {t.upper().strip() for t in station_types}:
                    reasons.append(f"Filtered Station Type: {st.type} (filter: {','.join(station_types)})")

            if project.abs_begin_pm is not None and project.abs_end_pm is not None and st.abs_pm is not None:
                min_abs = min(project.abs_begin_pm, project.abs_end_pm)
                max_abs = max(project.abs_begin_pm, project.abs_end_pm)
                if st.abs_pm < min_abs:
                    reasons.append(f"Out of range: Abs PM {st.abs_pm:.3f} is {min_abs - st.abs_pm:.2f} mi before corridor start ({min_abs:.3f})")
                elif st.abs_pm > max_abs:
                    reasons.append(f"Out of range: Abs PM {st.abs_pm:.3f} is {st.abs_pm - max_abs:.2f} mi beyond corridor end ({max_abs:.3f})")
            else:
                _, b_num = split_ca_pm(project.begin_pm)
                _, e_num = split_ca_pm(project.end_pm)
                _, st_num = st.ca_pm_prefix_and_num
                if not np.isnan(b_num) and not np.isnan(e_num) and not np.isnan(st_num):
                    min_ca = min(b_num, e_num)
                    max_ca = max(b_num, e_num)
                    if st_num < min_ca:
                        reasons.append(f"Out of range: State PM {st_num:.3f} is {min_ca - st_num:.2f} mi before corridor start ({min_ca:.3f})")
                    elif st_num > max_ca:
                        reasons.append(f"Out of range: State PM {st_num:.3f} is {st_num - max_ca:.2f} mi beyond corridor end ({max_ca:.3f})")

            discrepancies.append({
                "station_id": sid,
                "found_in_pems": True,
                "pems_record": st,
                "reasons": reasons or ["Outside corridor postmile buffer tolerance"]
            })
        else:
            discrepancies.append({
                "station_id": sid,
                "found_in_pems": False,
                "pems_record": None,
                "reasons": ["Invalid or obsolete PeMS VDS ID (not found in current Caltrans metadata)"]
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
        sheet_count = int(float(sheet_count_raw)) if pd.notna(sheet_count_raw) and str(sheet_count_raw).strip() != "" else len(existing_ids)
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
        found_sensor_count=len(found)
    )


def _find_column(df: pd.DataFrame, target_name: str) -> Optional[str]:
    """Case-insensitive, whitespace-tolerant column lookup."""
    target_clean = target_name.strip().lower()
    for col in df.columns:
        if col.strip().lower() == target_clean:
            return col
    return None


def apply_station_updates_to_dataframe(
    df: pd.DataFrame,
    comparison_results: List[StationComparisonResult]
) -> pd.DataFrame:
    """Apply identified corridor stations to master dataframe columns."""
    updated = df.copy()

    col_sensor_count = _find_column(updated, "Number of Active Non-Ramp Sensors")
    col_s1_id = _find_column(updated, "Sensor Station 1 ID")
    col_s1_hov = _find_column(updated, "Sensor Station 1 HOV ID")
    col_s1_name = _find_column(updated, "Sensor Station 1 Name")
    col_s1_type = _find_column(updated, "Sensor Station 1 Type")
    col_s1_lanes_after = _find_column(updated, "Sensor Station 1: number of lanes after")

    col_s2_id = _find_column(updated, "Sensor Station 2 ID")
    col_s2_lanes_after = _find_column(updated, "Sensor Station 2: number of lanes after")

    col_s3_id = _find_column(updated, "Sensor Station 3 ID")
    col_s3_lanes_after = _find_column(updated, "Sensor Station 3: number of lanes after")

    col_s4_id = _find_column(updated, "Sensor Station 4 ID")
    col_s4_lanes_after = _find_column(updated, "Sensor Station 4: number of lanes after")

    col_pid = _find_column(updated, "Project ID")
    col_dir = _find_column(updated, "Direction")

    for res in comparison_results:
        if not res.found_stations:
            continue

        # Match row by PID and Direction
        mask = (updated[col_pid].astype(str).str.strip() == str(res.project_id).strip())
        if col_dir and res.direction:
            mask = mask & (updated[col_dir].astype(str).str.strip().str.upper() == res.direction.strip().upper())

        matching_indices = updated[mask].index
        if matching_indices.empty:
            continue

        for idx in matching_indices:
            if col_sensor_count:
                updated.at[idx, col_sensor_count] = str(len(res.found_stations))

            # Station 1 (first found detector)
            st1 = res.found_stations[0]
            if col_s1_id:
                updated.at[idx, col_s1_id] = str(st1.id)
            if col_s1_type:
                updated.at[idx, col_s1_type] = str(st1.type)
            if col_s1_name:
                updated.at[idx, col_s1_name] = str(st1.name)
            if col_s1_lanes_after and st1.lanes is not None:
                updated.at[idx, col_s1_lanes_after] = str(st1.lanes)

            # Station 1 HOV ID (first HV detector if present)
            hov_stations = [s for s in res.found_stations if s.type == "HV"]
            if col_s1_hov:
                updated.at[idx, col_s1_hov] = str(hov_stations[0].id) if hov_stations else ""

            # Station 2
            if len(res.found_stations) >= 2:
                st2 = res.found_stations[1]
                if col_s2_id:
                    updated.at[idx, col_s2_id] = str(st2.id)
                if col_s2_lanes_after and st2.lanes is not None:
                    updated.at[idx, col_s2_lanes_after] = str(st2.lanes)

            # Station 3
            if len(res.found_stations) >= 3:
                st3 = res.found_stations[2]
                if col_s3_id:
                    updated.at[idx, col_s3_id] = str(st3.id)
                if col_s3_lanes_after and st3.lanes is not None:
                    updated.at[idx, col_s3_lanes_after] = str(st3.lanes)

            # Station 4
            if len(res.found_stations) >= 4:
                st4 = res.found_stations[3]
                if col_s4_id:
                    updated.at[idx, col_s4_id] = str(st4.id)
                if col_s4_lanes_after and st4.lanes is not None:
                    updated.at[idx, col_s4_lanes_after] = str(st4.lanes)

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
    verbose: bool = True
) -> List[StationComparisonResult]:
    """
    Execute end-to-end station comparison pipeline across highway projects.
    Safe dry-run mode is enabled by default. Set apply_updates=True (and dry_run=False) to commit updates.
    """
    csv_file = master_csv_path or DEFAULT_MASTER_CSV
    if projects is None:
        if not os.path.exists(csv_file):
            raise FileNotFoundError(f"Master CSV file not found: {csv_file}")
        projects = load_master_csv(csv_file)

    target_projects = list(projects)
    if dist_filter:
        d_clean = str(dist_filter).replace(".0", "").strip()
        target_projects = [
            p for p in target_projects
            if (str(p.dist).replace(".0", "").strip() == d_clean)
        ]
    if project_id_filter:
        pid_clean = str(project_id_filter).strip()
        target_projects = [p for p in target_projects if p.project_id == pid_clean]

    if not target_projects:
        LOGGER.warning("No projects matched the specified filters.")
        return []

    vds_meta = load_vds_metadata(meta_dir)

    results: List[StationComparisonResult] = []
    for p in target_projects:
        res = compare_project_stations(
            p,
            vds_meta=vds_meta,
            buffer_mi=buffer_mi,
            station_types=station_types
        )
        results.append(res)

    status_counts = {
        "EXACT_MATCH": 0,
        "PARTIAL_MATCH": 0,
        "DISCREPANCY": 0,
        "SHEET_EMPTY": 0,
        "UNINSTRUMENTED": 0,
    }
    total_sheet_sensors = 0
    total_found_sensors = 0

    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
        total_sheet_sensors += len(r.existing_stations)
        total_found_sensors += len(r.found_stations)

    total_projs = len(results)

    # Print summary report
    print("\n" + "=" * 80)
    print("PEMS VDS CORRIDOR SENSOR COMPARISON PIPELINE")
    mode_str = "DRY RUN (Read-Only) [Pass --apply to commit updates]" if dry_run or not apply_updates else "APPLY UPDATES (Active Write)"
    print(f"Mode: {mode_str}")
    print(f"Buffer Tolerance: ±{buffer_mi:.2f} mi | Station Types: {','.join(station_types)}")
    if dist_filter:
        print(f"District Filter: District {dist_filter}")
    if project_id_filter:
        print(f"Project ID Filter: {project_id_filter}")
    print("=" * 80)
    print(f"Evaluated Projects: {total_projs}")
    print(f"  • Exact Matches (sheet == corridor):         {status_counts.get('EXACT_MATCH', 0):3d} ({status_counts.get('EXACT_MATCH', 0)/total_projs*100:5.1f}%)")
    print(f"  • Partial Matches (all sheet in corridor +): {status_counts.get('PARTIAL_MATCH', 0):3d} ({status_counts.get('PARTIAL_MATCH', 0)/total_projs*100:5.1f}%)")
    print(f"  • Discrepancies (out of range/wrong dir):    {status_counts.get('DISCREPANCY', 0):3d} ({status_counts.get('DISCREPANCY', 0)/total_projs*100:5.1f}%)")
    print(f"  • Sheet Empty (corridor sensors available):   {status_counts.get('SHEET_EMPTY', 0):3d} ({status_counts.get('SHEET_EMPTY', 0)/total_projs*100:5.1f}%)")
    print(f"  • Uninstrumented / Rural (no detectors):     {status_counts.get('UNINSTRUMENTED', 0):3d} ({status_counts.get('UNINSTRUMENTED', 0)/total_projs*100:5.1f}%)")
    print(f"\nSensor Counts:")
    print(f"  • Existing Sensors in Spreadsheet: {total_sheet_sensors:4d}")
    print(f"  • Active Corridor Sensors Found:   {total_found_sensors:4d} (+{total_found_sensors - total_sheet_sensors:d} net detectors)")
    print("=" * 80)

    # Print verbose breakdown
    if verbose:
        display_results = results if len(results) <= 30 else results[:25]
        print("\nPROJECT-BY-PROJECT COMPARISON:")
        print(f"{'PID':<11} {'Dist':<4} {'Route':<5} {'Dir':<3} {'PM Span':<14} {'Sheet':<5} {'Found':<5} {'Status':<15} {'Notes / Discrepancy'}")
        print("-" * 105)
        for r in display_results:
            sheet_str = str(len(r.existing_stations))
            found_str = str(len(r.found_stations))
            pm_span = f"{r.begin_pm}-{r.end_pm}"

            notes = ""
            if r.status == "EXACT_MATCH":
                notes = f"All {len(r.matched_stations)} stations verified in range"
            elif r.status == "PARTIAL_MATCH":
                notes = f"Verified {len(r.matched_stations)} in range; +{len(r.missing_in_sheet)} more in PeMS"
            elif r.status == "SHEET_EMPTY":
                notes = f"Sheet has 0; PeMS has {len(r.found_stations)} active ({', '.join([s.id for s in r.found_stations[:3]])}...)"
            elif r.status == "UNINSTRUMENTED":
                notes = "No detectors in corridor"
            elif r.status == "DISCREPANCY":
                disc_notes = []
                for d in r.discrepancies:
                    disc_notes.append(f"Station {d['station_id']}: {d['reasons'][0]}")
                notes = "; ".join(disc_notes)

            if len(notes) > 42:
                notes = notes[:39] + "..."

            print(f"{r.project_id:<11} {r.district:<4} {r.route:<5} {r.direction:<3} {pm_span:<14} {sheet_str:<5} {found_str:<5} {r.status:<15} {notes}")

        if len(results) > len(display_results):
            print(f"... and {len(results) - len(display_results)} more projects (export full details with --output-report)")

        # Detailed breakdown for discrepancies
        discrepancies_list = [r for r in results if r.status == "DISCREPANCY"]
        if discrepancies_list:
            print(f"\nDETAILED DISCREPANCY AUDIT ({len(discrepancies_list)} projects):")
            for r in discrepancies_list:
                print(f"\n  Project {r.project_id} (Dist {r.district}, Route {r.route} {r.direction}, PM {r.begin_pm}-{r.end_pm}):")
                print(f"    Sheet Stations: {r.existing_stations}")
                print(f"    Corridor Stations Found ({len(r.found_stations)}): {[s.id for s in r.found_stations]}")
                for d in r.discrepancies:
                    st_id = d["station_id"]
                    if d["found_in_pems"]:
                        st = d["pems_record"]
                        print(f"    ✗ Station {st_id}: PeMS VDS Dist {st.district} Rte {st.fwy} {st.dir} (State PM {st.state_pm}, Abs PM {st.abs_pm}, Type {st.type}, '{st.name}')")
                    else:
                        print(f"    ✗ Station {st_id}: Not found in statewide PeMS detector metadata")
                    for reas in d["reasons"]:
                        print(f"        -> Reason: {reas}")

    # Output report file if requested
    if output_report_path:
        report_data = []
        for r in results:
            disc_reasons = []
            for d in r.discrepancies:
                disc_reasons.append(f"{d['station_id']}: {'; '.join(d['reasons'])}")

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
                "Status": r.status,
                "Sheet Sensor Count": r.sheet_sensor_count,
                "Found Sensor Count": r.found_sensor_count,
                "Existing Stations": ", ".join(r.existing_stations),
                "Found Stations": ", ".join([s.id for s in r.found_stations]),
                "Matched Stations": ", ".join(r.matched_stations),
                "Missing in Sheet": ", ".join(r.missing_in_sheet),
                "Discrepant Stations": ", ".join([d["station_id"] for d in r.discrepancies]),
                "Discrepancy Details": " | ".join(disc_reasons)
            })

        df_report = pd.DataFrame(report_data)
        os.makedirs(os.path.dirname(os.path.abspath(output_report_path)), exist_ok=True)
        if output_report_path.endswith(".md"):
            with open(output_report_path, "w", encoding="utf-8") as fh:
                fh.write(f"# Caltrans PeMS Station Comparison Report\n\n")
                fh.write(f"- **Evaluated Projects**: {total_projs}\n")
                fh.write(f"- **Exact Matches**: {status_counts.get('EXACT_MATCH', 0)}\n")
                fh.write(f"- **Partial Matches**: {status_counts.get('PARTIAL_MATCH', 0)}\n")
                fh.write(f"- **Discrepancies**: {status_counts.get('DISCREPANCY', 0)}\n")
                fh.write(f"- **Sheet Empty**: {status_counts.get('SHEET_EMPTY', 0)}\n")
                fh.write(f"- **Uninstrumented**: {status_counts.get('UNINSTRUMENTED', 0)}\n\n")
                fh.write(df_report.to_markdown(index=False))
        else:
            df_report.to_csv(output_report_path, index=False)
        print(f"\n✓ Comparison report successfully written to: {output_report_path}")

    # Apply updates if explicitly commanded
    if apply_updates and not dry_run:
        target_out = output_csv_path or csv_file
        # Create timestamped backup
        backup_path = f"{csv_file}.bak_{int(time.time())}"
        shutil.copy2(csv_file, backup_path)
        LOGGER.info(f"Created safety backup of input master CSV at: {backup_path}")

        raw_df = pd.read_csv(csv_file, dtype=str)
        updated_df = apply_station_updates_to_dataframe(raw_df, results)
        updated_df.to_csv(target_out, index=False)
        print(f"\n✓ Successfully updated master CSV at: {target_out}")
        print(f"  Safety backup preserved at:       {backup_path}")
    elif dry_run:
        print("\n[DRY RUN COMPLETE]: No changes have been written to the spreadsheet.")
        print("To apply these corridor stations to the master CSV, run with: --match-stations --apply")

    return results


# ---------------------------------------------------------------------------
# INTERACTIVE MAP ENGINE (FOLIUM / LEAFLET)
# ---------------------------------------------------------------------------
DISTRICT_COLORS = {
    "1": "#2563eb",   # Royal Blue
    "2": "#059669",   # Emerald
    "3": "#16a34a",   # Green
    "4": "#0284c7",   # Sky Blue
    "5": "#d97706",   # Amber
    "6": "#0d9488",   # Teal
    "7": "#dc2626",   # Crimson
    "8": "#9333ea",   # Purple
    "9": "#b45309",   # Brown
    "10": "#ea580c",  # Orange
    "11": "#c026d3",  # Fuchsia
    "12": "#e11d48",  # Rose
}


def get_project_corridor_geometry(
    project: ProjectRecord,
    idx_ref: Optional[IndexedGeoReference] = None
) -> List[Tuple[float, float]]:
    """
    Extract ordered list of (lat, lng) points representing the actual road path.
    Prefers high-resolution 0.1-mile PeMS reference segments, falling back to endpoints.
    """
    ref = idx_ref if idx_ref is not None else get_indexed_geo_ref()
    sub = ref.get_corridor(project.route, project.pems_direction)
    coords: List[Tuple[float, float]] = []

    if sub is not None and not sub.empty:
        b_pm = project.abs_begin_pm
        e_pm = project.abs_end_pm
        is_abs = True

        if b_pm is None or e_pm is None:
            _, b_num = split_ca_pm(project.begin_pm)
            _, e_num = split_ca_pm(project.end_pm)
            b_pm = b_num
            e_pm = e_num
            is_abs = False

        if b_pm is not None and e_pm is not None and not np.isnan(b_pm) and not np.isnan(e_pm):
            min_pm, max_pm = min(float(b_pm), float(e_pm)), max(float(b_pm), float(e_pm))
            ascending = float(b_pm) <= float(e_pm)

            if is_abs:
                pts = sub[(sub["_abs_pm"] >= min_pm - 0.05) & (sub["_abs_pm"] <= max_pm + 0.05)]
                pts = pts.sort_values(by="_abs_pm", ascending=ascending)
            else:
                county_clean = str(project.county).strip().lower()
                pts = sub[
                    (sub["_ca_num"] >= min_pm - 0.05) &
                    (sub["_ca_num"] <= max_pm + 0.05) &
                    (sub["_county"] == county_clean)
                ]
                pts = pts.sort_values(by="_ca_num", ascending=ascending)

            if len(pts) >= 2:
                valid_pts = pts.dropna(subset=["_lat", "_lng"])
                coords = [(float(r["_lat"]), float(r["_lng"])) for _, r in valid_pts.iterrows()]

    if len(coords) < 2 and project.start_coords and project.end_coords:
        coords = [project.start_coords, project.end_coords]

    return coords


def make_project_tooltip_html(project: ProjectRecord, length_mi: float = 0.0) -> str:
    """Generate compact hover tooltip HTML for a road path with bare-bones serif aesthetic."""
    sensors_str = ", ".join(project.sensor_stations) if project.sensor_stations else "None listed"
    b_lanes = project.raw_row.get("dir_lanes_before", "?")
    a_lanes = project.raw_row.get("dir_lanes_after", "?")
    b_pm = format_pm(project.begin_pm)
    e_pm = format_pm(project.end_pm)
    desc = str(project.raw_row.get("Project Description", "")).strip()
    if len(desc) > 85:
        desc = desc[:82] + "..."

    return f"""
    <div style="font-family: Georgia, 'Times New Roman', Times, serif; font-size: 13px; line-height: 1.4; min-width: 210px; color: #000; background: #fff; padding: 6px 8px; border: 1px solid #000;">
      <div style="font-weight: bold; font-size: 13px; color: #000; margin-bottom: 2px;">
        PID {project.project_id} &middot; Route {project.route} {project.direction}
      </div>
      <div><b>Dist {project.dist}</b> &middot; {project.county} County</div>
      <div><b>Postmiles:</b> PM {b_pm} &rarr; {e_pm} ({length_mi:.2f} mi)</div>
      <div><b>Directional Lanes:</b> {b_lanes} &rarr; {a_lanes}</div>
      <div><b>Dates:</b> Const: {project.construction_begin_date or 'N/A'} | Open: {project.open_to_traffic_date or 'N/A'}</div>
      <div><b>Sensors:</b> {sensors_str}</div>
      {f'<div style="color: #333; font-size: 12px; margin-top: 4px; border-top: 1px solid #666; padding-top: 3px;">{desc}</div>' if desc else ''}
    </div>
    """


def make_project_popup_html(project: ProjectRecord, builder: ProjectPeMSQueryBuilder, length_mi: float = 0.0) -> str:
    """Generate detailed click popup card HTML with bare-bones serif aesthetic and live links."""
    urls = builder.generate_all_urls()
    raw = project.raw_row
    tot_b = raw.get("Total Lanes Before", raw.get("total_lanes_before", "?"))
    tot_a = raw.get("Total Lanes After", raw.get("total_lanes_after", "?"))
    dir_b = raw.get("dir_lanes_before", "?")
    dir_a = raw.get("dir_lanes_after", "?")
    added_lm = raw.get("Total Added Lanes (Lane Miles)", raw.get("New GP Lanes (Lane Miles)", "?"))
    facility = str(raw.get("Facility Class checked by Lu") or raw.get("Facility Class") or raw.get("facility_class") or "Freeway").strip()
    area_type = str(raw.get("area_type") or raw.get("Area Type") or raw.get('Area Type ("Urban", "Suburban", "Exurban", "Rural", or "Unknown")') or "Unknown").strip().capitalize()
    desc = str(raw.get("Project Description", "")).strip()

    b_pm = format_pm(project.begin_pm)
    e_pm = format_pm(project.end_pm)
    abs_b = format_pm(project.abs_begin_pm)
    abs_e = format_pm(project.abs_end_pm)

    sensor_links = []
    for s_id in project.sensor_stations:
        s_url = urls["station_summary_urls"].get(s_id, f"https://pems.dot.ca.gov/?dnode=VDS&station_id={s_id}")
        sensor_links.append(f'<a href="{s_url}" target="_blank" style="color: #0000ee; text-decoration: underline;">{s_id}</a>')
    sensor_html = ", ".join(sensor_links) if sensor_links else "None listed"

    return f"""
    <div style="font-family: Georgia, 'Times New Roman', Times, serif; font-size: 13px; line-height: 1.45; width: 330px; padding: 6px; color: #000; background: #fff;">
      <div style="border-bottom: 1px solid #000; padding-bottom: 4px; margin-bottom: 6px;">
        <div style="font-size: 12px; font-weight: bold; text-transform: uppercase;">District {project.dist} &middot; {facility}</div>
        <h3 style="margin: 4px 0 2px; font-size: 15px; font-weight: bold; color: #000;">PID {project.project_id} &middot; Route {project.route} {project.direction}</h3>
        <div style="font-size: 12px; color: #333;">{project.county} County &middot; Area: {area_type}</div>
      </div>

      <table style="width: 100%; font-size: 12px; margin-bottom: 8px; border-collapse: collapse; border: 1px solid #666;">
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Postmiles:</td><td style="padding: 2px 4px; font-weight: bold;">PM {b_pm} &rarr; {e_pm} ({length_mi:.2f} mi)</td></tr>
        {f'<tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Abs PMs:</td><td style="padding: 2px 4px;">{abs_b} &rarr; {abs_e}</td></tr>' if abs_b else ''}
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Directional Lanes:</td><td style="padding: 2px 4px; font-weight: bold;">{dir_b} &rarr; {dir_a}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Total Roadway Lanes:</td><td style="padding: 2px 4px;">{tot_b} &rarr; {tot_a}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Lane Miles Added:</td><td style="padding: 2px 4px;">{added_lm}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Construction Date:</td><td style="padding: 2px 4px;">{project.construction_begin_date or 'N/A'}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Open to Traffic:</td><td style="padding: 2px 4px;">{project.open_to_traffic_date or 'N/A'}</td></tr>
        <tr><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Sensors:</td><td style="padding: 2px 4px;">{sensor_html}</td></tr>
      </table>

      {f'<div style="font-size: 11.5px; color: #333; border: 1px solid #ccc; padding: 4px; margin-bottom: 6px; max-height: 55px; overflow-y: auto;">{desc}</div>' if desc else ''}

      <div style="margin-top: 6px; padding-top: 4px; border-top: 1px solid #000; font-size: 12px;">
        <a href="{urls['lane_config_url']}" target="_blank" style="color: #0000ee; text-decoration: underline;">[PeMS Lane Config]</a>
        &nbsp;&nbsp;
        <a href="{urls['geometry_url']}" target="_blank" style="color: #0000ee; text-decoration: underline;">[Route Geometry]</a>
      </div>
    </div>
    """


# ---------------------------------------------------------------------------
# Station Metadata Mapping Constants & Utilities
# ---------------------------------------------------------------------------
STATION_TYPE_NAMES: Dict[str, str] = {
    "ML": "Mainline",
    "HV": "HOV / Carpool",
    "OR": "On-Ramp",
    "FR": "Off-Ramp",
    "FF": "Freeway Connector",
    "CD": "Collector-Distributor",
    "CH": "Commercial / Inspection",
}

STATION_TYPE_COLORS: Dict[str, str] = {
    "ML": "#1d4ed8",  # Bold Blue
    "HV": "#7e22ce",  # Purple
    "OR": "#15803d",  # Forest Green
    "FR": "#b45309",  # Amber Bronze
    "FF": "#475569",  # Slate Gray
    "CD": "#0f766e",  # Deep Teal
    "CH": "#6b7280",  # Cool Gray
}


def classify_station_relative_to_project(
    station: VDSStationRecord,
    project: ProjectRecord,
    tolerance_mi: float = 0.05
) -> Tuple[bool, str, Optional[float]]:
    """
    Determine if a station falls within project corridor bounds.
    Returns:
        (is_in_bounds, bounds_note, delta_mi)
    """
    # 1. Use absolute postmiles if both project and station have them
    use_abs = (
        project.abs_begin_pm is not None
        and project.abs_end_pm is not None
        and station.abs_pm is not None
        and not np.isnan(project.abs_begin_pm)
        and not np.isnan(project.abs_end_pm)
        and not np.isnan(station.abs_pm)
    )
    if use_abs:
        min_p = min(float(project.abs_begin_pm), float(project.abs_end_pm))
        max_p = max(float(project.abs_begin_pm), float(project.abs_end_pm))
        st_p = float(station.abs_pm)

        if min_p - tolerance_mi <= st_p <= max_p + tolerance_mi:
            if min_p <= st_p <= max_p:
                return True, f"PM {min_p:.2f} - {max_p:.2f}", 0.0
            return True, f"Boundary tolerance (±{tolerance_mi:.2f} mi)", 0.0
        elif st_p < min_p:
            delta = min_p - st_p
            return False, f"{delta:.2f} mi upstream / before start", delta
        else:
            delta = st_p - max_p
            return False, f"{delta:.2f} mi downstream / after end", delta

    # 2. Fall back to state postmiles
    _, b_num = split_ca_pm(project.begin_pm)
    _, e_num = split_ca_pm(project.end_pm)
    _, st_num = station.ca_pm_prefix_and_num

    if not np.isnan(b_num) and not np.isnan(e_num) and not np.isnan(st_num):
        min_p = min(b_num, e_num)
        max_p = max(b_num, e_num)
        if min_p - tolerance_mi <= st_num <= max_p + tolerance_mi:
            if min_p <= st_num <= max_p:
                return True, f"PM {min_p:.2f} - {max_p:.2f}", 0.0
            return True, f"Boundary tolerance (±{tolerance_mi:.2f} mi)", 0.0
        elif st_num < min_p:
            delta = min_p - st_num
            return False, f"{delta:.2f} mi upstream / before start", delta
        else:
            delta = st_num - max_p
            return False, f"{delta:.2f} mi downstream / after end", delta

    return False, "Unable to determine postmile bounds", None


def make_station_tooltip_html(
    station: VDSStationRecord,
    is_in_bounds: bool,
    bounds_note: str,
    is_in_sheet: bool,
    delta_mi: Optional[float] = None
) -> str:
    """Build compact serif hover tooltip HTML displaying station metadata and ground-truth status."""
    type_name = STATION_TYPE_NAMES.get(station.type, station.type)
    if is_in_bounds:
        status_badge = '<span style="color: #15803d; font-weight: bold;">✓ IN PROJECT BOUNDS</span>'
    else:
        status_badge = f'<span style="color: #b91c1c; font-weight: bold;">⚠ OUTSIDE BOUNDS ({bounds_note})</span>'

    if is_in_sheet:
        if is_in_bounds:
            sheet_badge = '<span style="color: #15803d; font-weight: bold;">✓ Listed in Master CSV (Verified)</span>'
        else:
            sheet_badge = '<span style="color: #b91c1c; font-weight: bold;">⚠ Listed in Master CSV (Discrepancy)</span>'
    else:
        sheet_badge = '<span style="color: #555555;">Not listed in Master CSV</span>'

    abs_str = f" · Abs PM {station.abs_pm:.2f}" if station.abs_pm is not None else ""
    lanes_str = f"{station.lanes}" if station.lanes else "N/A"
    len_str = f"{station.length:.2f} mi" if station.length else "N/A"

    return f"""<div style="font-family: Georgia, 'Times New Roman', Times, serif; font-size: 12px; line-height: 1.4; padding: 4px; min-width: 240px; color: #000; background: #fff;">
  <div style="font-weight: bold; font-size: 12.5px; border-bottom: 1px solid #000; padding-bottom: 3px; margin-bottom: 4px;">
    {status_badge}
  </div>
  <div><strong>Station ID:</strong> {station.id} &nbsp;({station.type} · {type_name})</div>
  <div><strong>Location:</strong> {station.name or 'N/A'}</div>
  <div><strong>Freeway:</strong> Route {station.fwy} {station.dir} · Dist {station.district}</div>
  <div><strong>Postmile:</strong> State PM {station.state_pm}{abs_str}</div>
  <div><strong>Lanes:</strong> {lanes_str} &nbsp;|&nbsp; <strong>Length:</strong> {len_str}</div>
  <div style="margin-top: 4px; padding-top: 3px; border-top: 1px dotted #888;">
    <strong>In Master Sheet:</strong> {sheet_badge}
  </div>
</div>"""


def make_station_popup_html(
    station: VDSStationRecord,
    project: Optional[ProjectRecord] = None,
    is_in_bounds: bool = True,
    bounds_note: str = "",
    is_in_sheet: bool = False,
    delta_mi: Optional[float] = None
) -> str:
    """Build detailed serif click popup HTML with PeMS links and ground-truth corridor context."""
    type_name = STATION_TYPE_NAMES.get(station.type, station.type)
    summary_url = build_station_summary_url(station.id)

    curr_year = datetime.datetime.now().year
    s_yr = curr_year - 5
    if project and project.construction_begin_date:
        try:
            s_yr = int(project.construction_begin_date.split("-")[0])
        except (ValueError, IndexError):
            pass
    e_yr = curr_year
    if project and project.open_to_traffic_date:
        try:
            e_yr = min(curr_year, int(project.open_to_traffic_date.split("-")[0]) + 3)
        except (ValueError, IndexError):
            pass
    if s_yr > e_yr:
        s_yr, e_yr = e_yr - 3, e_yr

    aadt_url = build_station_aadt_url(station.id, s_yr, e_yr)
    flow_url = build_station_flow_url(station.id, s_yr, e_yr)

    if is_in_bounds:
        bound_html = f'<div style="color: #15803d; font-weight: bold;">✓ In Project Bounds ({bounds_note})</div>'
    else:
        bound_html = f'<div style="color: #b91c1c; font-weight: bold;">⚠ Outside Project Bounds: {bounds_note}</div>'

    if is_in_sheet:
        if is_in_bounds:
            sheet_html = '<div style="color: #15803d;">✓ Included in Master Spreadsheet (Verified)</div>'
        else:
            sheet_html = '<div style="color: #b91c1c; font-weight: bold;">⚠ Included in Master Spreadsheet (Corridor Discrepancy)</div>'
    else:
        sheet_html = '<div style="color: #555;">Not currently listed in Master Spreadsheet</div>'

    proj_html = ""
    if project:
        proj_html = f"""
  <div style="margin-top: 5px; padding: 4px; border: 1px solid #ccc; background: #fdfdfd; font-size: 11.5px;">
    <strong>Corridor Context (PID {project.project_id}):</strong><br>
    Route {project.route} {project.direction} · PM {project.begin_pm} &rarr; {project.end_pm}<br>
    {bound_html}
    {sheet_html}
  </div>
"""

    return f"""<div style="font-family: Georgia, 'Times New Roman', Times, serif; font-size: 12px; line-height: 1.4; padding: 6px; min-width: 280px; max-width: 370px; color: #000;">
  <div style="font-weight: bold; font-size: 13.5px; border-bottom: 1px solid #000; padding-bottom: 4px; margin-bottom: 6px;">
    Station {station.id} · {station.type} ({type_name})
  </div>
  <div><strong>Location:</strong> {station.name or 'N/A'}</div>
  <div><strong>Freeway & Dir:</strong> Route {station.fwy} {station.dir} · District {station.district}</div>
  <div><strong>County / City:</strong> {station.county or 'N/A'}, {station.city or 'N/A'}</div>
  <div><strong>State Postmile:</strong> {station.state_pm}</div>
  {f'<div><strong>Absolute Postmile:</strong> {station.abs_pm:.3f}</div>' if station.abs_pm is not None else ''}
  <div><strong>Lanes:</strong> {station.lanes or 'N/A'} &nbsp;|&nbsp; <strong>Detector Length:</strong> {f'{station.length:.3f} mi' if station.length else 'N/A'}</div>
  {proj_html}
  <div style="margin-top: 6px; padding-top: 4px; border-top: 1px solid #000; font-size: 11.5px;">
    <a href="{summary_url}" target="_blank" style="color: #0000ee; text-decoration: underline;">[PeMS Station Summary]</a>
    &nbsp;&nbsp;
    <a href="{aadt_url}" target="_blank" style="color: #0000ee; text-decoration: underline;">[AADT Export ({s_yr}-{e_yr})]</a>
    &nbsp;&nbsp;
    <a href="{flow_url}" target="_blank" style="color: #0000ee; text-decoration: underline;">[Flow Export ({s_yr}-{e_yr})]</a>
  </div>
</div>"""


def add_ground_truth_control(
    m: folium.Map,
    project: ProjectRecord,
    in_bounds_count: int,
    oob_count: int,
    sheet_sensors_total: int,
    sheet_sensors_verified: int,
    discrepancies_count: int = 0
) -> None:
    """Add a bare-bones serif Ground-Truth Checker toolbar and styling to the Folium map."""
    discrepancy_str = f" · <span style='color: #dc2626; font-weight: bold;'>{discrepancies_count} out-of-range</span>" if discrepancies_count > 0 else ""
    gt_box_html = f"""
<div id="pems-ground-truth-control" style="
  position: absolute;
  top: 12px;
  left: 60px;
  z-index: 1000;
  background: #ffffff;
  border: 1px solid #000000;
  padding: 8px 12px;
  font-family: Georgia, 'Times New Roman', Times, serif;
  font-size: 12px;
  line-height: 1.4;
  color: #000000;
  max-width: 340px;
  box-shadow: none;
">
  <div style="font-weight: bold; font-size: 13px; border-bottom: 1px solid #000; padding-bottom: 3px; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center;">
    <span>PeMS Ground-Truth Checker</span>
    <button onclick="toggleGroundTruthPanel(this)" style="font-family: Georgia, serif; font-size: 11px; background: transparent; border: 1px solid #000; padding: 1px 5px; cursor: pointer;">[–]</button>
  </div>
  <div id="pems-gt-content">
    <div style="margin-bottom: 6px; font-size: 11.5px; color: #222;">
      <strong>Project {project.project_id}:</strong> Route {project.route} {project.direction} (PM {project.begin_pm} &rarr; {project.end_pm})<br>
      <strong>Corridor Detectors:</strong> <b>{in_bounds_count}</b> in bounds · {oob_count} buffer<br>
      <strong>Master Sheet:</strong> <b>{sheet_sensors_verified}/{sheet_sensors_total}</b> verified in corridor{discrepancy_str}
    </div>
    <div style="margin-bottom: 6px; padding-top: 4px; border-top: 1px dotted #888;">
      <button id="pems-oob-btn" onclick="toggleInBoundsOnly(this)" style="font-family: Georgia, serif; background: #f4f4f4; border: 1px solid #000; font-size: 11px; padding: 2px 7px; cursor: pointer; margin-right: 5px;">
        In-Bounds Only: OFF
      </button>
      <button onclick="toggleAllStationLayers(this)" style="font-family: Georgia, serif; background: #f4f4f4; border: 1px solid #000; font-size: 11px; padding: 2px 7px; cursor: pointer;">
        Toggle All Stations
      </button>
    </div>
    <div style="font-size: 11px; border-top: 1px dotted #888; padding-top: 4px; color: #333;">
      <strong>Type Filter / Legend:</strong><br>
      <span style="display: inline-block; width: 9px; height: 9px; background: #1d4ed8; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>ML (Mainline)
      &nbsp;
      <span style="display: inline-block; width: 9px; height: 9px; background: #7e22ce; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>HV (HOV)
      &nbsp;
      <span style="display: inline-block; width: 9px; height: 9px; background: #15803d; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>OR (Ramp)
      <br>
      <span style="display: inline-block; width: 9px; height: 9px; background: #b45309; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>FR (Off-Ramp)
      &nbsp;
      <span style="display: inline-block; width: 9px; height: 9px; background: #475569; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>Other
      &nbsp;
      <span style="display: inline-block; width: 9px; height: 9px; border: 2.5px solid #d97706; background: #fff; margin-right: 2px; vertical-align: middle;"></span>In Master Sheet
    </div>
  </div>
</div>
"""
    gt_script_html = """
<script>
var inBoundsOnlyActive = false;
function toggleInBoundsOnly(btn) {
  inBoundsOnlyActive = !inBoundsOnlyActive;
  btn.innerText = "In-Bounds Only: " + (inBoundsOnlyActive ? "ON" : "OFF");
  btn.style.background = inBoundsOnlyActive ? "#e2e8f0" : "#f4f4f4";
  btn.style.fontWeight = inBoundsOnlyActive ? "bold" : "normal";
  if (inBoundsOnlyActive) {
    document.body.classList.add("hide-oob-stations");
  } else {
    document.body.classList.remove("hide-oob-stations");
  }
}

function toggleAllStationLayers(btn) {
  var inputs = document.querySelectorAll(".leaflet-control-layers-overlays input[type='checkbox']");
  var stationInputs = [];
  var anyChecked = false;
  inputs.forEach(function(inp) {
    var label = inp.closest("label");
    if (label && label.textContent.indexOf("Stations:") !== -1) {
      stationInputs.push(inp);
      if (inp.checked) anyChecked = true;
    }
  });
  var target = !anyChecked;
  stationInputs.forEach(function(inp) {
    if (inp.checked !== target) {
      inp.click();
    }
  });
}

function toggleGroundTruthPanel(btn) {
  var content = document.getElementById("pems-gt-content");
  if (!content) return;
  if (content.style.display === "none") {
    content.style.display = "block";
    btn.innerText = "[–]";
  } else {
    content.style.display = "none";
    btn.innerText = "[+]";
  }
}
</script>
"""
    m.get_root().html.add_child(folium.Element(gt_box_html + gt_script_html))


def add_standard_basemaps_to_map(
    m: folium.Map,
    default_tiles: Optional[str] = None
) -> None:
    """
    Add reliable, unwatermarked basemaps to a Folium map.
    Eliminates 403 Access Blocked (OpenStreetMap missing Referer on file:// URLs)
    and CARTO 'API Key Required' watermarks.
    """
    carto_key = os.getenv("CARTO_API_KEY", "").strip()

    # 1. Primary Default: Esri Light Gray Canvas (clean, muted, no API key, works over file://)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ",
        name="Esri Light Gray (Default)",
        overlay=False,
        control=True,
        max_zoom=16
    ).add_to(m)

    # 2. Esri World Street Map (high-detail roads, freeways, street names)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS",
        name="Esri Street Map",
        overlay=False,
        control=True,
        max_zoom=19
    ).add_to(m)

    # 3. USGS Topographic (terrain contours, elevation)
    folium.TileLayer(
        tiles="https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles courtesy of the U.S. Geological Survey",
        name="USGS Topo",
        overlay=False,
        control=True,
        max_zoom=16
    ).add_to(m)

    # 4. Optional: CARTO Positron if user has provided an API key in .env
    if carto_key:
        folium.TileLayer(
            tiles=f"https://a.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}.png?key={carto_key}",
            attr="&copy; OpenStreetMap contributors &copy; CARTO",
            name="CartoDB Positron (Authenticated)",
            overlay=False,
            control=True,
            max_zoom=20
        ).add_to(m)

    # 5. OpenStreetMap (works when served via HTTP localhost; blocked over file:// without referer)
    folium.TileLayer(
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="&copy; OpenStreetMap contributors (Note: tile.openstreetmap.org blocks local file:// URLs)",
        name="OpenStreetMap (HTTP Only)",
        overlay=False,
        control=True,
        max_zoom=19
    ).add_to(m)

    # 6. Custom user tiles if explicitly specified and not one of the legacy keywords
    if default_tiles and default_tiles not in ("cartodbpositron", "cartodb_positron", "openstreetmap", "esri_gray"):
        folium.TileLayer(
            tiles=default_tiles,
            attr="Custom Tile Provider",
            name="Custom Base Layer",
            overlay=False,
            control=True
        ).add_to(m)


def build_single_project_map(
    target_pid: str,
    projects: Optional[List[ProjectRecord]] = None,
    output_path: Optional[str] = None,
    open_browser: bool = False,
    tiles: Optional[str] = None,
    include_stations: bool = True,
    station_buffer_mi: float = 3.0,
    station_types: Optional[Tuple[str, ...]] = None,
    vds_metadata: Optional[IndexedVDSMetadata] = None,
) -> Optional[folium.Map]:
    """
    Build a folium map for a single project (or both directions if split) with
    highway path, start/end markers, corridor station detector layers partitioned by type,
    rich hover tooltips, and an interactive ground-truth checker toolbar.
    """
    projs = projects or load_master_csv()
    matched = [p for p in projs if p.project_id == str(target_pid).strip()]
    if not matched:
        LOGGER.error(f"Project ID '{target_pid}' not found.")
        return None

    idx_ref = get_indexed_geo_ref()
    m = folium.Map(tiles=None)
    add_standard_basemaps_to_map(m, default_tiles=tiles)
    all_coords: List[Tuple[float, float]] = []

    for p in matched:
        coords = get_project_corridor_geometry(p, idx_ref)
        if not coords:
            continue
        all_coords.extend(coords)
        color = DISTRICT_COLORS.get(p.dist, "#2563eb")

        length_mi = 0.0
        if p.abs_begin_pm is not None and p.abs_end_pm is not None:
            length_mi = abs(p.abs_end_pm - p.abs_begin_pm)
        else:
            _, b_n = split_ca_pm(p.begin_pm)
            _, e_n = split_ca_pm(p.end_pm)
            if not np.isnan(b_n) and not np.isnan(e_n):
                length_mi = abs(e_n - b_n)

        builder = ProjectPeMSQueryBuilder(p, idx_ref)
        tooltip_html = make_project_tooltip_html(p, length_mi)
        popup_html = make_project_popup_html(p, builder, length_mi)

        folium.PolyLine(
            coords,
            weight=6,
            color=color,
            opacity=0.9,
            tooltip=folium.Tooltip(tooltip_html, sticky=True),
            popup=folium.Popup(popup_html, max_width=370)
        ).add_to(m)

        folium.CircleMarker(
            coords[0],
            radius=5,
            color="#16a34a",
            fill=True,
            fill_color="#16a34a",
            fill_opacity=1.0,
            tooltip=f"Start: Route {p.route} {p.direction} PM {p.begin_pm}"
        ).add_to(m)

        folium.CircleMarker(
            coords[-1],
            radius=5,
            color="#dc2626",
            fill=True,
            fill_color="#dc2626",
            fill_opacity=1.0,
            tooltip=f"End: Route {p.route} {p.direction} PM {p.end_pm}"
        ).add_to(m)

    if include_stations:
        vds = vds_metadata
        if vds is None:
            try:
                vds = get_indexed_vds_metadata()
            except Exception as exc:
                LOGGER.warning(f"Could not load PeMS VDS metadata for mapping: {exc}")
                vds = None

        if vds is not None:
            proj_by_dir = {p.pems_direction: p for p in matched}
            all_st_records: List[VDSStationRecord] = []
            seen_ids = set()

            for p in matched:
                st_list = vds.find_corridor_stations(
                    district=p.dist,
                    route=p.route,
                    direction=p.direction,
                    begin_pm=p.begin_pm,
                    end_pm=p.end_pm,
                    abs_begin_pm=p.abs_begin_pm,
                    abs_end_pm=p.abs_end_pm,
                    buffer_mi=station_buffer_mi,
                    station_types=station_types or ("ALL",)
                )
                for s in st_list:
                    if s.id not in seen_ids:
                        seen_ids.add(s.id)
                        all_st_records.append(s)

            # Ensure all sensors listed in spreadsheet are included
            sheet_sensor_ids = set()
            for p in matched:
                for sid in p.sensor_stations:
                    sheet_sensor_ids.add(sid)
                    if sid not in seen_ids and sid in vds.by_id:
                        seen_ids.add(sid)
                        all_st_records.append(vds.by_id[sid])

            type_groups: Dict[str, List[Tuple[VDSStationRecord, bool, str, bool, Optional[ProjectRecord]]]] = {
                "ML": [],
                "HV": [],
                "OR": [],
                "FR": [],
                "Other": []
            }
            in_bounds_cnt = 0
            oob_cnt = 0
            sheet_verified_cnt = 0
            sheet_discrepancy_cnt = 0

            for st in all_st_records:
                target_p = proj_by_dir.get(st.dir) or matched[0]
                is_in_bounds, bounds_note, delta_mi = classify_station_relative_to_project(st, target_p)
                is_in_sheet = (st.id in sheet_sensor_ids)

                if is_in_bounds:
                    in_bounds_cnt += 1
                    if is_in_sheet:
                        sheet_verified_cnt += 1
                else:
                    oob_cnt += 1
                    if is_in_sheet:
                        sheet_discrepancy_cnt += 1

                grp_key = st.type if st.type in ("ML", "HV", "OR", "FR") else "Other"
                type_groups[grp_key].append((st, is_in_bounds, bounds_note, is_in_sheet, target_p))

            type_labels = {
                "ML": "Mainline (ML)",
                "HV": "Carpool/HOV (HV)",
                "OR": "On-Ramp (OR)",
                "FR": "Off-Ramp (FR)",
                "Other": "Other (FF/CD/CH)"
            }
            default_show = {"ML", "HV"}

            for grp_key in ("ML", "HV", "OR", "FR", "Other"):
                st_tuple_list = type_groups[grp_key]
                if not st_tuple_list:
                    continue

                fg_name = f"Stations: {type_labels[grp_key]} ({len(st_tuple_list)})"
                fg = folium.FeatureGroup(name=fg_name, show=(grp_key in default_show))

                for st, in_bounds, b_note, in_sheet, target_p in st_tuple_list:
                    lat, lng = st.latitude, st.longitude
                    if lat is None or lng is None or np.isnan(lat) or np.isnan(lng) or lat == 0:
                        _, st_num = st.ca_pm_prefix_and_num
                        if not np.isnan(st_num):
                            coords = pm_to_coordinates(st.fwy, st.dir, st_num, ref_df=idx_ref)
                            if coords:
                                lat, lng = coords
                    if lat is None or lng is None or np.isnan(lat) or np.isnan(lng) or lat == 0:
                        continue

                    fill_color = STATION_TYPE_COLORS.get(st.type, "#2563eb")
                    if in_bounds:
                        radius = 6.5
                        border_color = "#d97706" if in_sheet else "#000000"
                        border_weight = 3.5 if in_sheet else 2.0
                        fill_opacity = 0.95
                        dash_arr = None
                        cls_name = f"pems-station-marker pems-inbounds{' pems-in-sheet' if in_sheet else ''}"
                    else:
                        radius = 4.5
                        border_color = "#dc2626" if in_sheet else "#555555"
                        border_weight = 3.0 if in_sheet else 1.5
                        fill_opacity = 0.50
                        dash_arr = None if in_sheet else "2, 3"
                        cls_name = f"pems-station-marker pems-oob{' pems-in-sheet' if in_sheet else ''}"

                    tt_html = make_station_tooltip_html(st, in_bounds, b_note, in_sheet)
                    pop_html = make_station_popup_html(st, target_p, in_bounds, b_note, in_sheet)

                    folium.CircleMarker(
                        location=[lat, lng],
                        radius=radius,
                        color=border_color,
                        weight=border_weight,
                        fill=True,
                        fill_color=fill_color,
                        fill_opacity=fill_opacity,
                        dash_array=dash_arr,
                        className=cls_name,
                        tooltip=folium.Tooltip(tt_html, sticky=True),
                        popup=folium.Popup(pop_html, max_width=370)
                    ).add_to(fg)

                fg.add_to(m)

            add_ground_truth_control(
                m=m,
                project=matched[0],
                in_bounds_count=in_bounds_cnt,
                oob_count=oob_cnt,
                sheet_sensors_total=len(sheet_sensor_ids),
                sheet_sensors_verified=sheet_verified_cnt,
                discrepancies_count=sheet_discrepancy_cnt
            )

    if all_coords:
        lats = [c[0] for c in all_coords]
        lngs = [c[1] for c in all_coords]
        m.fit_bounds([[min(lats), min(lngs)], [max(lats), max(lngs)]], padding=(30, 30))
    else:
        m.location = [36.7783, -119.4179]
        m.zoom_start = 6

    folium.LayerControl(position='topright', collapsed=False).add_to(m)

    m.get_root().header.add_child(folium.Element("""
<style>
  body, .leaflet-container, .leaflet-popup-content, .leaflet-control-layers, .leaflet-tooltip {
    font-family: Georgia, 'Times New Roman', Times, serif !important;
  }
  .leaflet-popup-content-wrapper {
    border-radius: 0px !important;
    border: 1px solid #000 !important;
    box-shadow: none !important;
  }
  .leaflet-popup-tip {
    box-shadow: none !important;
  }
  .leaflet-tooltip {
    border-radius: 0px !important;
    box-shadow: none !important;
  }
  .pems-station-marker {
    cursor: pointer;
  }
  .hide-oob-stations .pems-oob {
    display: none !important;
  }
</style>
"""))

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        m.save(output_path)
        LOGGER.info(f"Project map saved to {output_path}")
        if open_browser:
            webbrowser.open("file://" + os.path.abspath(output_path))

    return m


def build_multi_project_map(
    projects: Optional[List[ProjectRecord]] = None,
    dist: Optional[str] = None,
    output_path: Optional[str] = None,
    open_browser: bool = False,
    tiles: Optional[str] = None,
    include_stations: bool = False,
    station_buffer_mi: float = 1.0,
    station_types: Optional[Tuple[str, ...]] = None,
    vds_metadata: Optional[IndexedVDSMetadata] = None,
) -> folium.Map:
    """
    Build a multi-project interactive folium map across California.
    Projects are mapped as highway road paths with hover tooltips and click popups,
    organized into toggleable District feature groups with optional station detector layers.
    """
    projs = projects or load_master_csv()
    if dist:
        dist_clean = str(dist).replace(".0", "").strip()
        projs = [p for p in projs if p.dist == dist_clean]
        LOGGER.info(f"Filtered to District {dist_clean} ({len(projs)} projects)")

    idx_ref = get_indexed_geo_ref()
    m = folium.Map(tiles=None)
    add_standard_basemaps_to_map(m, default_tiles=tiles)
    all_coords: List[Tuple[float, float]] = []

    dist_groups: Dict[str, List[ProjectRecord]] = {}
    for p in projs:
        dist_groups.setdefault(p.dist, []).append(p)

    sorted_dists = sorted(dist_groups.keys(), key=lambda d: int(d) if d.isdigit() else 999)

    for d in sorted_dists:
        p_list = dist_groups[d]
        color = DISTRICT_COLORS.get(d, "#2563eb")
        fg = folium.FeatureGroup(name=f"District {d} ({len(p_list)} projects)", show=True)

        for p in p_list:
            coords = get_project_corridor_geometry(p, idx_ref)
            if not coords:
                continue
            all_coords.extend(coords)

            length_mi = 0.0
            if p.abs_begin_pm is not None and p.abs_end_pm is not None:
                length_mi = abs(p.abs_end_pm - p.abs_begin_pm)
            else:
                _, b_n = split_ca_pm(p.begin_pm)
                _, e_n = split_ca_pm(p.end_pm)
                if not np.isnan(b_n) and not np.isnan(e_n):
                    length_mi = abs(e_n - b_n)

            builder = ProjectPeMSQueryBuilder(p, idx_ref)
            tooltip_html = make_project_tooltip_html(p, length_mi)
            popup_html = make_project_popup_html(p, builder, length_mi)

            folium.PolyLine(
                coords,
                weight=5,
                color=color,
                opacity=0.85,
                tooltip=folium.Tooltip(tooltip_html, sticky=True),
                popup=folium.Popup(popup_html, max_width=370)
            ).add_to(fg)

            folium.CircleMarker(
                coords[0],
                radius=3.5,
                color="#16a34a",
                fill=True,
                fill_color="#16a34a",
                fill_opacity=0.9,
                tooltip=f"PID {p.project_id} Start PM {p.begin_pm}"
            ).add_to(fg)

            folium.CircleMarker(
                coords[-1],
                radius=3.5,
                color="#dc2626",
                fill=True,
                fill_color="#dc2626",
                fill_opacity=0.9,
                tooltip=f"PID {p.project_id} End PM {p.end_pm}"
            ).add_to(fg)

        fg.add_to(m)

    if include_stations:
        vds = vds_metadata
        if vds is None:
            try:
                vds = get_indexed_vds_metadata()
            except Exception as exc:
                LOGGER.warning(f"Could not load PeMS VDS metadata for multi-project map: {exc}")
                vds = None

        if vds is not None:
            seen_sids: Dict[str, Tuple[VDSStationRecord, List[str]]] = {}

            for p in projs:
                st_list = vds.find_corridor_stations(
                    district=p.dist,
                    route=p.route,
                    direction=p.direction,
                    begin_pm=p.begin_pm,
                    end_pm=p.end_pm,
                    abs_begin_pm=p.abs_begin_pm,
                    abs_end_pm=p.abs_end_pm,
                    buffer_mi=station_buffer_mi,
                    station_types=station_types or ("ALL",)
                )
                for s in st_list:
                    if s.id not in seen_sids:
                        seen_sids[s.id] = (s, [p.project_id])
                    else:
                        if p.project_id not in seen_sids[s.id][1]:
                            seen_sids[s.id][1].append(p.project_id)

                for sid in p.sensor_stations:
                    if sid in vds.by_id:
                        s = vds.by_id[sid]
                        if sid not in seen_sids:
                            seen_sids[sid] = (s, [p.project_id])
                        else:
                            if p.project_id not in seen_sids[sid][1]:
                                seen_sids[sid][1].append(p.project_id)

            type_groups: Dict[str, List[Tuple[VDSStationRecord, List[str]]]] = {
                "ML": [],
                "HV": [],
                "OR": [],
                "FR": [],
                "Other": []
            }
            for sid, (st, pids) in seen_sids.items():
                grp_key = st.type if st.type in ("ML", "HV", "OR", "FR") else "Other"
                type_groups[grp_key].append((st, pids))

            type_labels = {
                "ML": "Mainline (ML)",
                "HV": "Carpool/HOV (HV)",
                "OR": "On-Ramp (OR)",
                "FR": "Off-Ramp (FR)",
                "Other": "Other (FF/CD/CH)"
            }

            for grp_key in ("ML", "HV", "OR", "FR", "Other"):
                st_list = type_groups[grp_key]
                if not st_list:
                    continue

                fg_name = f"Stations: {type_labels[grp_key]} ({len(st_list)})"
                fg = folium.FeatureGroup(name=fg_name, show=False)

                for st, pids in st_list:
                    lat, lng = st.latitude, st.longitude
                    if lat is None or lng is None or np.isnan(lat) or np.isnan(lng) or lat == 0:
                        _, st_num = st.ca_pm_prefix_and_num
                        if not np.isnan(st_num):
                            coords = pm_to_coordinates(st.fwy, st.dir, st_num, ref_df=idx_ref)
                            if coords:
                                lat, lng = coords
                    if lat is None or lng is None or np.isnan(lat) or np.isnan(lng) or lat == 0:
                        continue

                    fill_color = STATION_TYPE_COLORS.get(st.type, "#2563eb")
                    pid_str = ", ".join(pids) if pids else "Corridor"
                    type_name = STATION_TYPE_NAMES.get(st.type, st.type)
                    tt_html = f"""<div style="font-family: Georgia, serif; font-size: 12px; line-height: 1.35; padding: 3px; min-width: 200px; color: #000; background: #fff;">
  <div style="font-weight: bold; border-bottom: 1px solid #000; padding-bottom: 2px; margin-bottom: 3px;">Station {st.id} ({st.type} - {type_name})</div>
  <div><strong>Name:</strong> {st.name or 'N/A'}</div>
  <div><strong>Route:</strong> Route {st.fwy} {st.dir} · PM {st.state_pm}</div>
  <div><strong>Lanes:</strong> {st.lanes or 'N/A'}</div>
  <div><strong>Project(s):</strong> PID {pid_str}</div>
</div>"""

                    pop_html = make_station_popup_html(st, None, True, f"Project(s): PID {pid_str}")

                    folium.CircleMarker(
                        location=[lat, lng],
                        radius=4.5,
                        color="#000000",
                        weight=1.5,
                        fill=True,
                        fill_color=fill_color,
                        fill_opacity=0.85,
                        className="pems-station-marker",
                        tooltip=folium.Tooltip(tt_html, sticky=True),
                        popup=folium.Popup(pop_html, max_width=350)
                    ).add_to(fg)

                fg.add_to(m)

    if all_coords:
        lats = [c[0] for c in all_coords]
        lngs = [c[1] for c in all_coords]
        m.fit_bounds([[min(lats), min(lngs)], [max(lats), max(lngs)]], padding=(20, 20))
    else:
        m.location = [36.7783, -119.4179]
        m.zoom_start = 6

    folium.LayerControl(position='topright', collapsed=False).add_to(m)

    m.get_root().header.add_child(folium.Element("""
<style>
  body, .leaflet-container, .leaflet-popup-content, .leaflet-control-layers, .leaflet-tooltip {
    font-family: Georgia, 'Times New Roman', Times, serif !important;
  }
  .leaflet-popup-content-wrapper {
    border-radius: 0px !important;
    border: 1px solid #000 !important;
    box-shadow: none !important;
  }
  .leaflet-popup-tip {
    box-shadow: none !important;
  }
  .leaflet-tooltip {
    border-radius: 0px !important;
    box-shadow: none !important;
  }
</style>
"""))

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        m.save(output_path)
        LOGGER.info(f"Multi-project map saved to {output_path}")
        if open_browser:
            webbrowser.open("file://" + os.path.abspath(output_path))

    return m


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Consolidated PeMS Interface & Query Engine for CERCET CA Projects."
    )
    parser.add_argument("--input", "-i", default=DEFAULT_MASTER_CSV, help="Path to cleaned Master CSV.")
    parser.add_argument("--inspect", "-p", help="Inspect a specific Project ID.")
    parser.add_argument("--map", metavar="PID", help="Generate interactive map for a specific Project ID and open in browser.")
    parser.add_argument("--map-all", action="store_true", help="Generate interactive map for all projects and open in browser.")
    parser.add_argument("--output-map", "-o", help="Custom output path for generated HTML map.")
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open map in browser.")
    parser.add_argument("--dist", help="Filter --map-all to a specific Caltrans District (e.g. '4').")
    parser.add_argument("--no-stations", action="store_true", help="Do not include PeMS VDS station detector layers on single-project map.")
    parser.add_argument("--include-stations", action="store_true", help="Include PeMS VDS station detector layers on multi-project map (--map-all).")
    parser.add_argument("--station-buffer", type=float, default=3.0, help="Corridor postmile buffer in miles for nearby stations on the map (default: 3.0 mi).")
    parser.add_argument("--map-station-types", default=None, help="Comma-separated station types to include on the map (default: all types partitioned by layer).")
    parser.add_argument("--list-urls", action="store_true", help="List generated PeMS URLs for projects.")
    parser.add_argument("--test-login", action="store_true", help="Test PeMS login credentials.")
    parser.add_argument("--census", action="store_true", help="Search closest Census stations.")
    parser.add_argument("--match-stations", action="store_true", help="Match active PeMS VDS stations to highway corridors and compare against sheet.")
    parser.add_argument("--dry-run", action="store_true", default=None, help="Perform comparison in dry-run mode without modifying CSV (default: True).")
    parser.add_argument("--apply", action="store_true", help="Apply updates to master CSV (disables dry-run; creates automated backup).")
    parser.add_argument("--output-report", help="Save comparison report to CSV or Markdown file.")
    parser.add_argument("--output-csv", help="Custom output path for updated master CSV when using --apply.")
    parser.add_argument("--station-types", default="ML,HV", help="Comma-separated station types to include for matching (default: 'ML,HV'). Use 'ALL' for all types.")
    parser.add_argument("--buffer", type=float, default=0.1, help="Postmile buffer tolerance in miles (default: 0.1).")
    parser.add_argument("--metadata-dir", help="Custom directory containing PeMS metadata text files.")
    parser.add_argument("--route", "-r", help="Freeway route number for ad-hoc query.")
    parser.add_argument("--dir", "-d", help="Freeway direction (N/S/E/W) for ad-hoc query.")
    parser.add_argument("--begin-pm", type=float, help="Begin postmile for ad-hoc query.")
    parser.add_argument("--end-pm", type=float, help="End postmile for ad-hoc query.")
    parser.add_argument("--county", "-c", help="County for ad-hoc query.")

    args = parser.parse_args()

    if args.test_login:
        print("\n--- Testing PeMS Login ---")
        client = PeMSClient(debug=True)
        try:
            client.login()
            print("✓ PeMS Login Successful!")
        except Exception as exc:
            print(f"✗ Login failed: {exc}")
            sys.exit(1)
        return

    if args.route and args.dir and args.begin_pm is not None and args.end_pm is not None:
        print(f"\n--- Ad-hoc Query: Route {args.route} {args.dir} PM {args.begin_pm} - {args.end_pm} ---")
        ref_df = load_geo_reference()
        start = pm_to_coordinates(args.route, args.dir, args.begin_pm, county=args.county, is_absolute=(args.county is None), ref_df=ref_df)
        end = pm_to_coordinates(args.route, args.dir, args.end_pm, county=args.county, is_absolute=(args.county is None), ref_df=ref_df)
        print(f"Start coordinates: {start}")
        print(f"End coordinates:   {end}")

        lane_url = build_lane_config_url(args.route, args.dir, "01/01/2015", "12/31/2025")
        print(f"Lane config URL:   {lane_url}")

        stations = get_corridor_stations(args.route, args.dir, args.begin_pm, args.end_pm, is_absolute=(args.county is None), county=args.county, ref_df=ref_df)
        print(f"Reference segments in range: {len(stations)}")
        return

    if not os.path.exists(args.input):
        print(f"Error: master CSV file not found: {args.input}")
        sys.exit(1)

    projects = load_master_csv(args.input)
    print(f"Loaded {len(projects)} projects from {args.input}")

    # Station matching and comparison pipeline
    if args.match_stations:
        is_dry_run = True
        if args.apply:
            is_dry_run = False
        if args.dry_run is True:
            is_dry_run = True

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
            project_id_filter=args.inspect,
            verbose=True
        )
        return

    if args.map:
        target_pid = str(args.map).strip()
        out_file = args.output_map or f"route_map_{target_pid}.html"
        open_b = not args.no_open
        st_types = tuple(s.strip() for s in args.map_station_types.split(",")) if args.map_station_types else None
        m = build_single_project_map(
            target_pid,
            projects=projects,
            output_path=out_file,
            open_browser=open_b,
            include_stations=not args.no_stations,
            station_buffer_mi=args.station_buffer,
            station_types=st_types,
        )
        if m is None:
            sys.exit(1)
        print(f"\n✓ Generated interactive map for Project {target_pid} -> {out_file}")
        return

    if args.map_all:
        out_file = args.output_map or ("all_ca_projects_map.html" if not args.dist else f"district_{args.dist}_projects_map.html")
        open_b = not args.no_open
        st_types = tuple(s.strip() for s in args.map_station_types.split(",")) if args.map_station_types else None
        m = build_multi_project_map(
            projects=projects,
            dist=args.dist,
            output_path=out_file,
            open_browser=open_b,
            include_stations=args.include_stations,
            station_buffer_mi=args.station_buffer,
            station_types=st_types,
        )
        print(f"\n✓ Generated interactive multi-project map -> {out_file}")
        return

    if args.inspect:
        target_pid = str(args.inspect).strip()
        matched = [p for p in projects if p.project_id == target_pid]
        if not matched:
            print(f"Error: Project ID '{target_pid}' not found.")
            sys.exit(1)

        for idx, proj in enumerate(matched):
            builder = ProjectPeMSQueryBuilder(proj)
            start_c, end_c = builder.resolve_geometry()
            urls = builder.generate_all_urls()

            print("\n" + "=" * 70)
            print(f"PROJECT INSPECTION: {proj.project_id} (Direction: {proj.direction}) [Match {idx+1}/{len(matched)}]")
            print("=" * 70)
            print(f"  Route & Dir:        Route {proj.route} {proj.direction} (PeMS dir: {proj.pems_direction})")
            print(f"  District & County:  Dist {proj.dist}, {proj.county} ({proj.state})")
            print(f"  Postmiles:          Begin: {proj.begin_pm} | End: {proj.end_pm}")
            if proj.abs_begin_pm or proj.abs_end_pm:
                print(f"  Absolute PMs:       Begin: {proj.abs_begin_pm} | End: {proj.abs_end_pm}")
            print(f"  Coordinates:        Start: {start_c} | End: {end_c}")
            print(f"  Dates:              Const: {proj.construction_begin_date} | Open: {proj.open_to_traffic_date}")
            print(f"  Sensor Stations:    {proj.sensor_stations if proj.sensor_stations else '(None listed)'}")
            print("\n  GENERATED PEMS URLS:")
            print(f"  Lane Config URL:    {urls['lane_config_url']}")
            print(f"  Route Geometry URL: {urls['geometry_url']}")

            for st_id in proj.sensor_stations:
                print(f"\n  Station {st_id}:")
                print(f"    Summary URL: {urls['station_summary_urls'][st_id]}")
                sample_aadt = urls['station_aadt_urls'][st_id][0]
                print(f"    AADT {sample_aadt[0]} URL: {sample_aadt[1]}")
                sample_flow = urls['station_flow_urls'][st_id][0]
                print(f"    Flow {sample_flow[0]} URL: {sample_flow[1]}")

            corr_stations = builder.get_corridor_stations_from_reference()
            print(f"\n  Corridor Reference Segments: {len(corr_stations)} segments found in corridor.")

            # VDS detector comparison audit for inspected project
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

            print("=" * 70)
        return

    if args.list_urls:
        print("\nListing PeMS URLs for first 5 projects:")
        for proj in projects[:5]:
            builder = ProjectPeMSQueryBuilder(proj)
            urls = builder.generate_all_urls()
            print(f"\n[{proj.project_id}] {urls['corridor']}")
            print(f"  Lane Config: {urls['lane_config_url']}")
            if proj.sensor_stations:
                print(f"  Sensors ({len(proj.sensor_stations)}): {', '.join(proj.sensor_stations)}")
        print(f"\n... (use --inspect <PID> to view full details for any project)")
        return

    print("\nSummary of projects by District:")
    df_p = pd.DataFrame([{
        "PID": p.project_id,
        "Dist": p.dist,
        "County": p.county,
        "Route": p.route,
        "Dir": p.direction,
        "Sensors": len(p.sensor_stations)
    } for p in projects])
    print(df_p.groupby("Dist").agg(projects=("PID", "count"), routes=("Route", "nunique"), sensors=("Sensors", "sum")))
    print("\nReady! Use --inspect <PID> to inspect project URLs, or --test-login to verify PeMS access.")


if __name__ == "__main__":
    main()
