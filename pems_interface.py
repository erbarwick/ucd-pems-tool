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

2. Command-Line Interface (CLI):
--------------------------------
  # Test PeMS credentials and login
  python pems_interface.py --test-login

  # Inspect a specific project by Project ID (PID)
  python pems_interface.py --inspect 418000244

  # Inspect a project and find closest Census stations
  python pems_interface.py --inspect 300000061 --census

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


def split_ca_pm(ca_pm: Any) -> Tuple[str, float]:
    """
    Split a California postmile string into prefix and float number.
    Handles letter prefixes (R, L, M) and suffix letters (L, R, A).
    e.g. 'R3.80' -> ('R', 3.8), '12.4L' -> ('', 12.4), 'R12.4A' -> ('R', 12.4)
    """
    if ca_pm is None or (isinstance(ca_pm, float) and np.isnan(ca_pm)):
        return "", np.nan
    s = str(ca_pm).strip()
    if not s:
        return "", np.nan

    # Prefix: non-digits; Numeric: digits + decimal; Suffix: trailing non-digits
    match = re.match(r"^([A-Za-z\s\-]*?)([0-9]+(?:\.[0-9]+)?)([A-Za-z\s\-]*)$", s)
    if match:
        pfx = match.group(1).strip()
        num = float(match.group(2))
        return pfx, num

    try:
        return "", float(s)
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
        # Fallback for ad-hoc custom reference dataframes
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
    ref_df: Optional[pd.DataFrame] = None
) -> pd.DataFrame:
    """Find all reference segments along a corridor range with O(1) indexed lookup."""
    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction)

    if ref_df is not None:
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


def parse_master_row(row: Dict[str, Any]) -> ProjectRecord:
    """Convert a dictionary row from the master CSV into a clean ProjectRecord."""
    pid = str(row.get("Project ID", "")).strip()
    dist = str(row.get("Dist", "")).replace(".0", "").strip()
    county = str(row.get("County", "")).strip()
    state = str(row.get("State", "CA")).strip()
    route = str(row.get("Route", "")).strip()
    direction = str(row.get("Direction", "")).strip().upper()
    begin_pm = str(row.get("Begin PM", "")).strip()
    end_pm = str(row.get("End PM", "")).strip()

    # Absolute PMs
    abs_beg = None
    abs_end = None
    for k in ("temp_abs_begin", "Absolute Begin PM", "Abs Begin PM"):
        if k in row and pd.notna(row[k]):
            try:
                abs_beg = float(row[k])
                break
            except ValueError:
                pass

    for k in ("temp_abs_end", "Absolute End PM", "Abs End PM"):
        if k in row and pd.notna(row[k]):
            try:
                abs_end = float(row[k])
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
        "Sensor Station 2 ID ", "Sensor Station 2 ID",
        "Sensor Station 3 ID ", "Sensor Station 3 ID",
        "Sensor Station 4 ID ", "Sensor Station 4 ID"
    ):
        s_val = str(row.get(col, "")).strip()
        if s_val and s_val.lower() not in ("nan", "none", "0", ""):
            digits = "".join(filter(str.isdigit, s_val))
            if digits and digits not in sensor_stations:
                sensor_stations.append(digits)

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
        raw_row=row
    )


def load_master_csv(csv_path: Optional[str] = None) -> List[ProjectRecord]:
    """Load and parse the master project CSV into a list of ProjectRecord objects."""
    path = csv_path or DEFAULT_MASTER_CSV
    if not os.path.exists(path):
        raise FileNotFoundError(f"Master CSV file not found: {path}")

    df = pd.read_csv(path, dtype=str)
    records = []
    for _, row in df.iterrows():
        records.append(parse_master_row(row.to_dict()))
    return records


# ---------------------------------------------------------------------------
# PROJECT PEMS QUERY & DISPATCH GENERATOR
# ---------------------------------------------------------------------------
class ProjectPeMSQueryBuilder:
    """Builds and resolves PeMS queries for a given ProjectRecord."""

    def __init__(self, project: ProjectRecord, ref_df: Optional[pd.DataFrame] = None):
        self.project = project
        self.ref_df = ref_df if ref_df is not None else load_geo_reference()

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
                county=self.project.county, is_absolute=is_abs, ref_df=self.ref_df
            )
        if end is None and e_pm:
            end = pm_to_coordinates(
                self.project.route, self.project.pems_direction, e_pm,
                county=self.project.county, is_absolute=is_abs, ref_df=self.ref_df
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
            ref_df=self.ref_df
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
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Consolidated PeMS Interface & Query Engine for CERCET CA Projects."
    )
    parser.add_argument("--input", "-i", default=DEFAULT_MASTER_CSV, help="Path to cleaned Master CSV.")
    parser.add_argument("--inspect", "-p", help="Inspect a specific Project ID.")
    parser.add_argument("--list-urls", action="store_true", help="List generated PeMS URLs for projects.")
    parser.add_argument("--test-login", action="store_true", help="Test PeMS login credentials.")
    parser.add_argument("--census", action="store_true", help="Search closest Census stations.")
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
