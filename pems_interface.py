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
      get_project_imagery,
      resolve_absolute_postmiles,
  )

  # A. Postmile to Coordinate Conversion & Absolute PM Resolution:
  coords = pm_to_coordinates(route="80", direction="E", pm="11.4", county="Solano")
  # Returns: (38.201952, -122.156967)
  abs_b, abs_e = resolve_absolute_postmiles(route="5", county="Shasta", begin_pm="39", end_pm="41.2")
  # Returns: (700.0, 702.2)

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

  # E. Download Historical Satellite Imagery (Before & After Construction):
  imagery = get_project_imagery("218000125", period="both")
  print("Before construction:", imagery["imagery"]["before"]["path"])
  print("After completion:", imagery["imagery"]["after"]["path"])

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
import math
import json
import argparse
import html
import urllib.parse
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any, Union, Sequence, Set

import pandas as pd
import numpy as np
import folium
import webbrowser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from hpms_interface import resolve_absolute_postmiles, parse_postmile

# ---------------------------------------------------------------------------
# CONFIGURATION & PATHS
# ---------------------------------------------------------------------------
load_dotenv()

BASE_URL = "https://pems.dot.ca.gov"
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_REF_PATH = os.path.join(WORKSPACE_DIR, "downloads", "merged_geo_pm.csv")
DEFAULT_MASTER_CSV = os.path.join(WORKSPACE_DIR, "staging", "Master_CA_Project_List_Cleaned.csv")
DEFAULT_METADATA_DIR = os.path.join(WORKSPACE_DIR, "downloads", "metadata")

CENSUS_PATHS = [
    os.path.join(WORKSPACE_DIR, "ALL_CENSUS_STATIONS.tsv"),
    os.path.join(WORKSPACE_DIR, "downloads", "ALL_CENSUS_STATIONS.tsv"),
    os.path.abspath(os.path.join(WORKSPACE_DIR, "..", "..", "..", "notes", "AADT Comparisons", "data", "ALL_CENSUS_STATIONS.tsv"))
]

TABLE_MARKERS = (
    "Starting Month", "Month", "Day", "Time", "Station", "VDS", "Hour",
    "ID\t", "ID,", "Latitude\t", "Latitude,", "Fwy\t", "Fwy,", "Abs_PM\t", "State_PM\t"
)

MONO_FONT = '"Andale Mono", Menlo, Monaco, "Courier New", Courier, monospace'

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
    """Normalize route string to digits: 'SR-80' -> '80', 'I-5' -> '5', '5.0' -> '5'."""
    if not r or pd.isna(r):
        return ""
    s = str(r).strip()
    s = re.sub(r"\.0+$", "", s)
    return re.sub(r"[^0-9]", "", s)


def format_pm(val: Any) -> str:
    """
    Format a postmile cleanly, eliminating floating-point artifacts (e.g. 8.6999999999999993 -> 8.7).
    Preserves letter prefixes (e.g. R, M, L), suffixes (e.g. L, R), and strips route/county prefixes.
    """
    if val is None or pd.isna(val) or str(val).strip().lower() in ("", "nan", "none"):
        return ""
    s = str(val).strip()
    # Strip Caltrans standard prefix like '04-ALA-80-', 'ALA-', 'I-5 PM', etc.
    s = re.sub(r"^(?:\d{1,2}\s*[-–]\s*)?[A-Za-z]{3,4}\s*[-–]\s*\d+\s*(?:PM)?\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^(?:I|SR|US|RTE|ROUTE|HWY)\s*[-–]?\s*\d+\s*(?:PM)?\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^[A-Za-z]{3,4}\s+(?:PM\s*)?", "", s, flags=re.IGNORECASE).strip()

    sign = ""
    if s.startswith("-"):
        sign = "-"
        s = s[1:].strip()

    m = re.match(r"^([A-Za-z\s\-]*?)([0-9]+(?:\.[0-9]+)?)([A-Za-z\s\-]*)$", s)
    if m:
        pfx = m.group(1).strip()
        num = float(sign + m.group(2))
        sfx = m.group(3).strip()
        rounded = round(num, 3)
        num_str = f"{rounded:.3f}".rstrip("0").rstrip(".")
        return f"{pfx}{num_str}{sfx}"
    try:
        num = float(sign + s)
        rounded = round(num, 3)
        return f"{rounded:.3f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return s


def split_ca_pm(ca_pm: Any) -> Tuple[str, float]:
    """
    Split a California postmile string into prefix and float number.
    Handles letter prefixes (R, L, M) and suffix letters (L, R, A),
    as well as embedded route markers (e.g. 'I-10 30.3', '04-ALA-80 R11.4').
    e.g. 'R3.80' -> ('R', 3.8), '12.4L' -> ('', 12.4), 'R12.4A' -> ('R', 12.4)
    """
    if ca_pm is None or (isinstance(ca_pm, float) and np.isnan(ca_pm)) or str(ca_pm).strip().lower() in ("", "nan", "none"):
        return "", np.nan
    s = str(ca_pm).strip()
    if not s:
        return "", np.nan

    s_clean = re.sub(r"^(?:\d{1,2}\s*[-–]\s*)?[A-Za-z]{3,4}\s*[-–]\s*\d+\s*(?:PM)?\s*", "", s, flags=re.IGNORECASE).strip()
    s_clean = re.sub(r"^(?:I|SR|US|RTE|ROUTE|HWY)\s*[-–]?\s*\d+\s*(?:PM)?\s*", "", s_clean, flags=re.IGNORECASE).strip()
    s_clean = re.sub(r"^[A-Za-z]{3,4}\s+(?:PM\s*)?", "", s_clean, flags=re.IGNORECASE).strip()

    sign = ""
    if s_clean.startswith("-"):
        sign = "-"
        s_clean = s_clean[1:].strip()

    match = re.match(r"^([A-Za-z\s\-]*?)([0-9]+(?:\.[0-9]+)?)([A-Za-z\s\-]*)$", s_clean)
    if match:
        pfx = match.group(1).strip()
        num = round(float(sign + match.group(2)), 4)
        return pfx, num

    try:
        return "", round(float(sign + s_clean), 4)
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

    def get_url(self, url: str, stream: bool = False, timeout: float = 60.0, max_retries: int = 2) -> requests.Response:
        """Make an authenticated GET request, re-authenticating if session expired, with automatic retry on read timeout."""
        if not self.logged_in:
            self.login()

        full_url = url if url.startswith("http") else urllib.parse.urljoin(BASE_URL, url)

        last_exc = None
        current_timeout = timeout
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(full_url, stream=stream, timeout=current_timeout)
                resp.raise_for_status()

                if not stream and self._is_login_page(resp):
                    LOGGER.warning("PeMS session appears expired; re-authenticating...")
                    self.login()
                    resp = self.session.get(full_url, stream=stream, timeout=current_timeout)
                    resp.raise_for_status()

                return resp
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as exc:
                last_exc = exc
                if attempt < max_retries:
                    current_timeout = max(current_timeout, 90.0)
                    LOGGER.warning(
                        f"PeMS server response slow (timed out after {timeout:.0f}s). Retrying (attempt {attempt + 1}/{max_retries}) with {current_timeout:.0f}s timeout..."
                    )
                    time.sleep(1.5)
                else:
                    raise
            except Exception as exc:
                last_exc = exc
                raise
        if last_exc:
            raise last_exc

    def get_text_content(self, url: str, timeout: float = 60.0) -> Optional[str]:
        """Fetch content as decoded text with configurable timeout (default 60s)."""
        try:
            resp = self.get_url(url, timeout=timeout)
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
            if isinstance(path_or_content, bytes):
                if path_or_content.startswith(b"\xd0\xcf\x11\xe0") or path_or_content.startswith(b"PK\x03\x04"):
                    return True
                text = path_or_content.decode("utf-8", errors="replace")
                lines = text.splitlines()[:50]
            elif os.path.isfile(str(path_or_content)):
                with open(str(path_or_content), "rb") as bfh:
                    magic = bfh.read(8)
                    if magic.startswith(b"\xd0\xcf\x11\xe0") or magic.startswith(b"PK\x03\x04"):
                        return True
                with open(str(path_or_content), "r", encoding="utf-8", errors="replace") as fh:
                    lines = [fh.readline() for _ in range(50)]
            else:
                lines = str(path_or_content).splitlines()[:50]

            for line in lines:
                s = line.strip()
                if any(s.startswith(m) for m in TABLE_MARKERS) and ("\t" in line or "," in line):
                    return True
                if ("<table" in s.lower() or "<tr" in s.lower() or "<th" in s.lower() or "<td" in s.lower()) and any(m.strip() in line for m in TABLE_MARKERS):
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
    station_type: str = "",
    export_format: str = "text"
) -> str:
    """Construct PeMS Freeway configuration time series URL."""
    route_num = normalize_route(route)
    dir_clean = normalize_direction(direction)
    s_fmt = _format_date_param(start_date)
    e_fmt = _format_date_param(end_date)
    exp = "xls" if str(export_format).strip().lower() in ("xls", "xlsx") else "text"

    return (
        f"{BASE_URL}/?report_form=1&dnode=Freeway&content=elv&tab=config_timeseries"
        f"&export={exp}&fwy={route_num}&dir={dir_clean}"
        f"&s_time_id_f={s_fmt}&e_time_id_f={e_fmt}&q=lanes&gn=day&station_type={station_type}"
    )


def build_station_aadt_url(
    station_id: str,
    start_year: int,
    end_year: Optional[int] = None,
    export_format: str = "text"
) -> str:
    """Construct PeMS VDS annual AADT export URL."""
    st_id = str(station_id).strip()
    end_year = end_year or start_year

    jan_1 = datetime.datetime(start_year, 1, 1, 0, 0, 0)
    dec_31 = datetime.datetime(end_year, 12, 31, 23, 59, 59)
    s_time_id = int(calendar.timegm(jan_1.timetuple()))
    e_time_id = int(calendar.timegm(dec_31.timetuple()))
    exp = "xls" if str(export_format).strip().lower() in ("xls", "xlsx") else "text"

    return (
        f"{BASE_URL}/?report_form=1&dnode=VDS&content=analysis&tab=aadt&export={exp}"
        f"&station_id={st_id}&s_time_id={s_time_id}&s_time_id_f=01%2F{start_year}"
        f"&e_time_id={e_time_id}&e_time_id_f=12%2F{end_year}"
    )


def build_station_timeseries_url(
    station_id: str,
    start_year: int,
    end_year: Optional[int] = None,
    quantity: str = "flow",
    granularity: str = "month",
    export_format: str = "text"
) -> str:
    """
    Construct PeMS VDS detector time series export URL for any quantity and granularity.
    Quantities: 'flow' (Vehicles), 'occ' (Occupancy %), 'speed' (Speed mph), 'vmt' (VMT), 'vht' (VHT), 'delay' (Delay).
    Granularities: 'month', 'day', 'hour', 'week', 'year', '5min'.
    """
    st_id = str(station_id).strip()
    end_year = end_year or start_year

    jan_1 = datetime.datetime(start_year, 1, 1, 0, 0, 0)
    dec_31 = datetime.datetime(end_year, 12, 31, 23, 59, 0)
    s_time_id = int(calendar.timegm(jan_1.timetuple()))
    e_time_id = int(calendar.timegm(dec_31.timetuple()))

    s_fmt = urllib.parse.quote(f"01/01/{start_year} 00:00")
    e_fmt = urllib.parse.quote(f"12/31/{end_year} 23:59")
    q_norm = str(quantity).strip().lower()
    gn_norm = str(granularity).strip().lower()
    exp = "xls" if str(export_format).strip().lower() in ("xls", "xlsx") else "text"

    return (
        f"{BASE_URL}/?report_form=1&dnode=VDS&content=loops&tab=det_timeseries&export={exp}"
        f"&station_id={st_id}&s_time_id={s_time_id}&s_time_id_f={s_fmt}"
        f"&e_time_id={e_time_id}&e_time_id_f={e_fmt}"
        f"&tod=all&tod_from=0&tod_to=0"
        f"&dow_0=on&dow_1=on&dow_2=on&dow_3=on&dow_4=on&dow_5=on&dow_6=on"
        f"&holidays=on&q={q_norm}&q2=&gn={gn_norm}"
    )


def build_station_flow_url(
    station_id: str,
    start_year: int,
    end_year: Optional[int] = None,
    granularity: str = "month",
    export_format: str = "text"
) -> str:
    """Construct PeMS VDS detector time series (Flow) export URL."""
    return build_station_timeseries_url(
        station_id=station_id,
        start_year=start_year,
        end_year=end_year,
        quantity="flow",
        granularity=granularity,
        export_format=export_format
    )


def build_fwy_latlng_url(route: str, direction: str, export_format: str = "text") -> str:
    """Construct PeMS Freeway coordinate geometry URL."""
    route_num = normalize_route(route)
    dir_clean = normalize_direction(direction)
    exp = "xls" if str(export_format).strip().lower() in ("xls", "xlsx") else "text"
    return f"{BASE_URL}/?report_form=1&dnode=Freeway&content=fwy_latlngXY&export={exp}&fwy={route_num}&dir={dir_clean}"


def build_station_summary_url(station_id: str) -> str:
    """Construct PeMS VDS Summary / Metadata URL."""
    st_id = str(station_id).strip()
    return f"{BASE_URL}/?report_form=1&dnode=VDS&content=summary&tab=summary&station_id={st_id}"


def build_station_config_url(station_id: str) -> str:
    """Construct PeMS VDS Configuration URL (lane configuration table and TSN roadway metrics)."""
    st_id = str(station_id).strip()
    return f"{BASE_URL}/?report_form=1&dnode=VDS&content=sta_cfg&station_id={st_id}"


def build_clearinghouse_url(district: Any, year: int, file_type: str = "station_aadt") -> str:
    """Construct PeMS Clearinghouse bulk download URL."""
    dist_clean = str(district).replace(".0", "").strip()
    return f"{BASE_URL}/?srq=clearinghouse&district_id={dist_clean}&yy={year}&type={file_type}&returnformat=text"


# ---------------------------------------------------------------------------
# BATCH DOWNLOAD ENGINE
# ---------------------------------------------------------------------------
def download_station_data(
    station_id: str,
    data_type: str = "flow",
    granularity: str = "month",
    start_year: int = 1999,
    end_year: Optional[int] = None,
    output_dir: str = ".",
    client: Optional[PeMSClient] = None,
    combine: bool = True,
    overwrite: bool = False,
    pause_sec: float = 0.5,
    verbose: bool = True,
    export_format: str = "text"
) -> Dict[str, Any]:
    """
    Download detector data from Caltrans PeMS for a specific station across all requested years.

    Downloads year-by-year into output_dir to prevent PeMS server timeouts and silent truncation,
    validates every response against HTML errors, and optionally consolidates all active years
    into a single clean master dataset.

    Parameters:
      station_id: Caltrans PeMS VDS detector ID (e.g. '400001').
      data_type: 'flow', 'occ', 'speed', 'vmt', 'vht', 'delay', or 'aadt'.
      granularity: 'month', 'day', 'hour', 'week', 'year', '5min' (for time-series data).
      start_year: Earliest calendar year to query (default: 1999).
      end_year: Latest calendar year to query (default: current year).
      output_dir: Local directory to save files (default: '.' current working directory).
      client: Optional authenticated PeMSClient instance.
      combine: If True, creates a unified '<station>_<type>_<granularity>_all_years.csv' master file.
      overwrite: If True, re-downloads even if valid data is already cached on disk.
      pause_sec: Polite delay in seconds between HTTP requests (default: 0.5s).
      verbose: Print real-time progress to stdout.
      export_format: 'text' (default, tab-delimited text/txt) or 'xls' (PeMS Excel export).
    """
    st_id = re.sub(r"[^0-9]", "", str(station_id).strip())
    if not st_id:
        raise ValueError(f"Invalid station ID: {station_id}")

    data_type_clean = str(data_type).strip().lower()
    granularity_clean = str(granularity).strip().lower()

    valid_quantities = {"flow", "occ", "speed", "vmt", "vht", "delay", "aadt"}
    if data_type_clean not in valid_quantities:
        raise ValueError(f"Unknown data type '{data_type}'. Allowed: {sorted(valid_quantities)}")

    valid_granularities = {"month", "day", "hour", "week", "year", "5min"}
    if data_type_clean != "aadt" and granularity_clean not in valid_granularities:
        raise ValueError(f"Unknown granularity '{granularity}'. Allowed: {sorted(valid_granularities)}")

    fmt_norm = str(export_format).strip().lower()
    is_xls = fmt_norm in ("xls", "xlsx")
    ext = "xls" if is_xls else "txt"
    url_export = "xls" if is_xls else "text"

    curr_year = datetime.datetime.now().year
    e_year = end_year or curr_year
    if start_year > e_year:
        raise ValueError(f"start_year ({start_year}) cannot be greater than end_year ({e_year})")

    pems_client = client or PeMSClient()
    if not pems_client.logged_in:
        pems_client.login()

    target_dir = os.path.abspath(output_dir)
    os.makedirs(target_dir, exist_ok=True)

    if verbose:
        print("\n" + "=" * 70)
        print("PEMS DETECTOR DATA DOWNLOAD")
        print(f"Station ID:        {st_id}")
        print(f"Data Type:         {data_type_clean} ({'Annual AADT' if data_type_clean == 'aadt' else granularity_clean})")
        print(f"Export Format:     {fmt_norm.upper()}")
        print(f"Year Span:         {start_year} - {e_year}")
        print(f"Target Directory:  {target_dir}")
        print("=" * 70)

    years_downloaded = []
    years_no_data = []
    all_dfs: List[pd.DataFrame] = []

    import io
    for year in range(start_year, e_year + 1):
        if data_type_clean == "aadt":
            url = build_station_aadt_url(st_id, year, year, export_format=url_export)
            fn = f"station_{st_id}_{year}_aadt.{ext}"
        else:
            url = build_station_timeseries_url(st_id, year, year, quantity=data_type_clean, granularity=granularity_clean, export_format=url_export)
            fn = f"station_{st_id}_{year}_{data_type_clean}_{granularity_clean}.{ext}"

        filepath = os.path.join(target_dir, fn)
        content: Optional[str] = None

        if os.path.exists(filepath) and not overwrite:
            if pems_client.file_has_data_table(filepath):
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()

        if content is None:
            time.sleep(pause_sec)
            content = pems_client.get_text_content(url)
            if content is None or not pems_client.file_has_data_table(content):
                years_no_data.append(year)
                if verbose:
                    print(f"  • {year}: No data available")
                continue

            lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
            if len(lines) <= 1 and not ("<table" in content.lower()):
                years_no_data.append(year)
                if verbose:
                    print(f"  • {year}: Inactive / 0 records")
                continue

            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(content)

        # Parse into DataFrame for consolidation
        try:
            df_year = None
            if is_xls and ("<table" in content.lower() or "<html" in content.lower()):
                try:
                    html_dfs = pd.read_html(io.StringIO(content))
                    if html_dfs:
                        df_year = html_dfs[0]
                except Exception:
                    pass
            if df_year is None and is_xls:
                try:
                    df_year = pd.read_excel(filepath)
                except Exception:
                    pass
            if df_year is None:
                sep = "\t" if "\t" in content else ","
                df_year = pd.read_csv(io.StringIO(content), sep=sep)

            if df_year is not None and not df_year.empty:
                if "Station_ID" not in df_year.columns and "VDS" not in df_year.columns:
                    df_year.insert(0, "Station_ID", st_id)
                all_dfs.append(df_year)
                years_downloaded.append(year)
                if verbose:
                    print(f"  ✓ {year}: {len(df_year):4d} records -> {fn}")
            else:
                years_no_data.append(year)
        except Exception as exc:
            LOGGER.warning(f"Could not parse data for year {year}: {exc}")

    combined_file = None
    total_records = 0
    if combine and all_dfs:
        merged_df = pd.concat(all_dfs, ignore_index=True)
        total_records = len(merged_df)
        if data_type_clean == "aadt":
            comb_fn = f"station_{st_id}_aadt_all_years.csv"
        else:
            comb_fn = f"station_{st_id}_{data_type_clean}_{granularity_clean}_all_years.csv"
        combined_file = os.path.join(target_dir, comb_fn)
        merged_df.to_csv(combined_file, index=False)
        if verbose:
            print("-" * 70)
            print(f"✓ Consolidated {total_records} total records across {len(years_downloaded)} active years into:")
            print(f"  -> {combined_file}")
            print("=" * 70 + "\n")
    elif verbose:
        if not years_downloaded:
            print("-" * 70)
            print(f"⚠ No active data found for Station {st_id} in years {start_year} - {e_year}.")
            print("=" * 70 + "\n")
        else:
            print("-" * 70)
            print(f"✓ Downloaded {len(years_downloaded)} raw annual files to {target_dir}")
            print("=" * 70 + "\n")

    return {
        "station_id": st_id,
        "data_type": data_type_clean,
        "granularity": granularity_clean,
        "export_format": fmt_norm,
        "start_year": start_year,
        "end_year": e_year,
        "years_downloaded": years_downloaded,
        "years_no_data": years_no_data,
        "total_records": total_records,
        "combined_file": combined_file,
        "output_dir": target_dir
    }


def download_project_data(
    project_id: str,
    data_type: str = "flow",
    granularity: str = "month",
    start_year: int = 1999,
    end_year: Optional[int] = None,
    output_dir: str = ".",
    master_csv_path: Optional[str] = None,
    combine: bool = True,
    overwrite: bool = False,
    pause_sec: float = 0.5,
    verbose: bool = True,
    export_format: str = "text"
) -> Dict[str, Any]:
    """
    Locate all detector stations associated with a project in the master CSV
    and download PeMS data for each detector station into output_dir.
    """
    projs = load_master_csv(master_csv_path or DEFAULT_MASTER_CSV)
    target_pid = str(project_id).strip()
    matched = [p for p in projs if p.project_id == target_pid]
    if not matched:
        raise ValueError(f"Project ID '{target_pid}' not found in master project dataset.")

    st_ids = []
    seen = set()
    for p in matched:
        for sid in p.sensor_stations:
            clean_id = re.sub(r"[^0-9]", "", str(sid).strip())
            if clean_id and clean_id not in seen:
                seen.add(clean_id)
                st_ids.append(clean_id)

    # If no stations listed in sheet, fall back to active corridor sensors
    if not st_ids:
        try:
            vds = get_indexed_vds_metadata()
            for p in matched:
                c_st = vds.find_corridor_stations(
                    district=p.dist, route=p.route, direction=p.direction,
                    begin_pm=p.begin_pm, end_pm=p.end_pm,
                    abs_begin_pm=p.abs_begin_pm, abs_end_pm=p.abs_end_pm,
                    county=p.county, station_types=("ML", "HV")
                )
                for s in c_st:
                    if s.id not in seen:
                        seen.add(s.id)
                        st_ids.append(s.id)
        except Exception:
            pass

    if not st_ids:
        if verbose:
            print(f"⚠ Project {target_pid} has no detector stations listed or identified in corridor.")
        return {
            "project_id": target_pid,
            "station_results": {},
            "total_stations": 0,
            "output_dir": os.path.abspath(output_dir)
        }

    proj_dir = os.path.join(os.path.abspath(output_dir), f"project_{target_pid}")
    os.makedirs(proj_dir, exist_ok=True)

    if verbose:
        print("\n" + "=" * 70)
        print(f"PEMS PROJECT DETECTOR DOWNLOAD: Project {target_pid}")
        if matched and matched[0].project_description:
            wrapped = textwrap.fill(matched[0].project_description, width=68, initial_indent="  Description:        ", subsequent_indent="                      ")
            print(wrapped)
        print(f"Associated Stations ({len(st_ids)}): {', '.join(st_ids)}")
        print(f"Export Format: {export_format.upper()}")
        print(f"Target Directory: {proj_dir}")
        print("=" * 70)

    client = PeMSClient()
    client.login()

    station_results = {}
    for sid in st_ids:
        st_out = os.path.join(proj_dir, sid)
        res = download_station_data(
            station_id=sid,
            data_type=data_type,
            granularity=granularity,
            start_year=start_year,
            end_year=end_year,
            output_dir=st_out,
            client=client,
            combine=combine,
            overwrite=overwrite,
            pause_sec=pause_sec,
            verbose=verbose,
            export_format=export_format
        )
        station_results[sid] = res

    return {
        "project_id": target_pid,
        "project_description": matched[0].project_description if matched else "",
        "station_results": station_results,
        "total_stations": len(st_ids),
        "output_dir": proj_dir
    }


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

        self.clean_df = clean_df
        self.lats = clean_df["_lat"]
        self.lngs = clean_df["_lng"]

        # Partition by (Route, Direction)
        self.index: Dict[Tuple[str, str], pd.DataFrame] = {}
        for (r, d), grp in clean_df.groupby(["_route", "_dir"]):
            self.index[(r, d)] = grp

    def get_corridor(self, route: str, direction: str) -> Optional[pd.DataFrame]:
        r_norm = normalize_route(route)
        d_norm = normalize_direction(direction)
        return self.index.get((r_norm, d_norm))

    def valid_directions_for_route(self, route: str) -> Set[str]:
        """Return all valid Caltrans directions for a highway route."""
        r_norm = normalize_route(route)
        return {d for (r, d) in self.index.keys() if r == r_norm and d}

    def find_candidate_highways_for_coords(
        self,
        lat: float,
        lng: float,
        max_dist_mi: float = 0.5
    ) -> List[Tuple[pd.Series, float]]:
        """
        Find highway segments near a coordinate, grouped by route, ordered by distance to centerline.
        """
        delta = 0.15
        mask = (self.lats >= lat - delta) & (self.lats <= lat + delta) & (self.lngs >= lng - delta) & (self.lngs <= lng + delta)
        sub = self.clean_df[mask]
        if sub.empty:
            delta = 0.8
            mask = (self.lats >= lat - delta) & (self.lats <= lat + delta) & (self.lngs >= lng - delta) & (self.lngs <= lng + delta)
            sub = self.clean_df[mask]
        if sub.empty:
            sub = self.clean_df

        dists = haversine_distance_miles(lat, lng, self.lats[sub.index], self.lngs[sub.index])
        close_mask = dists <= max_dist_mi
        if not close_mask.any():
            idx = dists.idxmin()
            return [(sub.loc[idx], float(dists.loc[idx]))]

        res = []
        close_sub = sub[close_mask]
        close_dists = dists[close_mask]
        for r, grp in close_sub.groupby("_route"):
            grp_dists = close_dists[grp.index]
            min_idx = grp_dists.idxmin()
            res.append((grp.loc[min_idx], float(grp_dists.loc[min_idx])))
        res.sort(key=lambda x: x[1])
        return res

    def auto_detect_highway_corridor(
        self,
        start_coord: Tuple[float, float],
        end_coord: Tuple[float, float]
    ) -> Tuple[str, pd.Series, pd.Series, float, float]:
        """
        Auto-detect the highway corridor connecting two coordinates.
        Returns: (route_norm, snap_start_row, snap_end_row, start_dist_mi, end_dist_mi)
        """
        cand1 = self.find_candidate_highways_for_coords(start_coord[0], start_coord[1], max_dist_mi=1.0)
        cand2 = self.find_candidate_highways_for_coords(end_coord[0], end_coord[1], max_dist_mi=1.0)

        r1_map = {normalize_route(c[0]["_route"]): c for c in cand1}
        r2_map = {normalize_route(c[0]["_route"]): c for c in cand2}

        common_routes = set(r1_map.keys()) & set(r2_map.keys())
        if common_routes:
            best_route = min(common_routes, key=lambda r: r1_map[r][1] + r2_map[r][1])
            s_row, s_dist = r1_map[best_route]
            e_row, e_dist = r2_map[best_route]
        else:
            s_row, s_dist = cand1[0]
            best_route = normalize_route(s_row["_route"])
            sub_route = self.clean_df[self.clean_df["_route"] == best_route]
            e_dists = haversine_distance_miles(end_coord[0], end_coord[1], self.lats[sub_route.index], self.lngs[sub_route.index])
            e_idx = e_dists.idxmin()
            e_row = sub_route.loc[e_idx]
            e_dist = float(e_dists.loc[e_idx])

        return best_route, s_row, e_row, s_dist, e_dist


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
    begin_pm: Any,
    end_pm: Any,
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

    def _parse_pm_val(val: Any) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            _, num = split_ca_pm(val)
            return num

    b_val = _parse_pm_val(begin_pm)
    e_val = _parse_pm_val(end_pm)
    if np.isnan(b_val) or np.isnan(e_val):
        return pd.DataFrame()

    min_pm, max_pm = min(b_val, e_val), max(b_val, e_val)

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
    county: Optional[str] = None,
    top_n: int = 5
) -> pd.DataFrame:
    """Find closest Caltrans Census stations to a corridor postmile."""
    cdf = load_census_reference()
    if cdf is None or cdf.empty:
        return pd.DataFrame()

    r_num = normalize_route(route)
    dist_str = str(district).replace(".0", "").strip()
    d_clean = str(int(dist_str)) if dist_str.isdigit() else dist_str

    df = cdf.copy()
    fwy_num = df["Fwy"].astype(str).str.replace(r"[^0-9]", "", regex=True)
    dist_num = pd.to_numeric(df["District"], errors="coerce").fillna(0).astype(int).astype(str)

    cond = (fwy_num == r_num) & ((dist_num == d_clean) | (dist_num == "0"))
    if county and "County" in df.columns:
        c_clean = str(county).strip().lower()
        c_cond = cond & (df["County"].astype(str).str.strip().str.lower() == c_clean)
        matched = df[c_cond].copy()
        if matched.empty:
            matched = df[cond].copy()
    else:
        matched = df[cond].copy()
    if matched.empty:
        return pd.DataFrame()

    matched["pm_num"] = matched["CA PM"].astype(str).map(lambda x: split_ca_pm(x)[1])
    matched["pm_diff"] = (matched["pm_num"] - float(ca_pm)).abs()
    matched = matched.dropna(subset=["pm_diff"]).sort_values(by="pm_diff").drop_duplicates(subset=["ID"])
    return matched.head(top_n)


def parse_coordinate_string(coord_str: Any) -> Optional[Tuple[float, float]]:
    """
    Parse a coordinate string into (latitude, longitude) floats.
    Supports:
      - Decimal degrees: '38.186164, -122.601642', '(38.186164, -122.601642)', '38.186164 -122.601642'
      - Degrees Minutes Seconds (DMS) from Google Maps: '38°13\'14.99"N 122°36\'27.10"W', '38°11\'10.19"N, 122°36\'05.91"W'
      - Tuple or List of two numbers: (38.186, -122.601)
    """
    if isinstance(coord_str, (tuple, list)) and len(coord_str) == 2:
        try:
            return float(coord_str[0]), float(coord_str[1])
        except (ValueError, TypeError):
            return None

    if not isinstance(coord_str, str):
        return None

    s = coord_str.strip()

    # 1. Decimal format
    m_dec = re.match(r"^[\(\[]?\s*([+-]?\d+\.?\d*)\s*[,;\s]+\s*([+-]?\d+\.?\d*)\s*[\)\]]?$", s)
    if m_dec:
        try:
            return float(m_dec.group(1)), float(m_dec.group(2))
        except ValueError:
            pass

    # 2. DMS format (Google Maps / GIS export)
    m_dms = re.search(
        r"(\d+)[°\s]+(\d+)[\'\"\s]+([\d\.]+)[\'\"\s]*([NSns])[\s,;]+"
        r"(\d+)[°\s]+(\d+)[\'\"\s]+([\d\.]+)[\'\"\s]*([EWew])",
        s
    )
    if m_dms:
        lat_deg, lat_min, lat_sec, lat_dir, lng_deg, lng_min, lng_sec, lng_dir = m_dms.groups()
        lat = float(lat_deg) + float(lat_min) / 60.0 + float(lat_sec) / 3600.0
        if lat_dir.upper() == "S":
            lat = -lat
        lng = float(lng_deg) + float(lng_min) / 60.0 + float(lng_sec) / 3600.0
        if lng_dir.upper() == "W":
            lng = -lng
        return lat, lng

    return None


def haversine_distance_miles(lat1: float, lon1: float, lat2: Any, lon2: Any) -> Any:
    """Calculate great-circle distance in miles between coordinates using Haversine formula."""
    R = 3958.8  # Earth radius in miles
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2.0)**2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2.0)**2
    return 2.0 * R * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def fetch_hpms_corridor_summary(
    route: Union[str, int],
    abs_begin_pm: float,
    abs_end_pm: float,
    county: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetch FHWA / Caltrans HPMS ground-truth roadway attributes (through lanes, AADT,
    facility type, access control, surface type, functional system) for any highway corridor.
    Works for any state highway segment in California, including non-freeway and rural corridors.
    """
    try:
        from hpms_interface import HPMSClient, CA_COUNTY_FIPS
        client = HPMSClient()
        r_num = int(normalize_route(route))
        county_fips = CA_COUNTY_FIPS.get(str(county).lower().strip()) if county else None
        min_pm = min(float(abs_begin_pm), float(abs_end_pm))
        max_pm = max(float(abs_begin_pm), float(abs_end_pm))
        secs = client.get_spatial_sections(route_number=r_num, begin_pm=min_pm, end_pm=max_pm, county_fips=county_fips)
        if not secs and county_fips:
            # Retry without county restriction in case boundaries slightly differ
            secs = client.get_spatial_sections(route_number=r_num, begin_pm=min_pm, end_pm=max_pm)
        if not secs:
            return None

        facility_code_map = {
            "1": "One-Way Roadway",
            "2": "Two-Way Divided (Freeway/Expressway)",
            "4": "Two-Way Undivided",
            "5": "Others / Ramps"
        }
        access_code_map = {
            "1": "Full Access Control (Freeway)",
            "2": "Partial Access Control (Expressway)",
            "3": "No Access Control (Conventional Highway)"
        }
        surface_code_map = {
            "1": "Unpaved",
            "2": "Bituminous (Asphalt)",
            "3": "JPCP (Jointed Plain Concrete)",
            "4": "JRCP (Jointed Reinforced Concrete)",
            "5": "CRCP (Continuously Reinforced Concrete)",
            "6": "AC Overlay on AC",
            "7": "AC Overlay on PCC",
            "8": "PCC Overlay on PCC",
            "9": "Other"
        }
        fsystem_code_map = {
            "1": "Interstate",
            "2": "Principal Arterial - Other Freeways & Expressways",
            "3": "Principal Arterial - Other",
            "4": "Minor Arterial",
            "5": "Major Collector",
            "6": "Minor Collector",
            "7": "Local"
        }

        fac_vals = [facility_code_map.get(str(s.get("facility_type")), str(s.get("facility_type"))) for s in secs if s.get("facility_type") is not None]
        acc_vals = [access_code_map.get(str(s.get("access_control")), str(s.get("access_control"))) for s in secs if s.get("access_control") is not None]
        surf_vals = [surface_code_map.get(str(s.get("surface_type")), str(s.get("surface_type"))) for s in secs if s.get("surface_type") is not None]
        fsys_vals = [fsystem_code_map.get(str(s.get("f_system")), str(s.get("f_system"))) for s in secs if s.get("f_system") is not None]

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

        fa_vals = [s.get("future_aadt") for s in secs if s.get("future_aadt")]
        fa_yr_vals = [str(s.get("future_aadt_year"))[:4] for s in secs if s.get("future_aadt_year")]

        return {
            "sections_count": len(secs),
            "predominant_lanes_2024": pred_lanes,
            "avg_lanes_2024": avg_lanes,
            "avg_aadt_2024": avg_aadt,
            "future_aadt": fa_vals[0] if fa_vals else None,
            "future_aadt_year": fa_yr_vals[0] if fa_yr_vals else None,
            "facility_type": ", ".join(dict.fromkeys(fac_vals)) if fac_vals else None,
            "access_control": ", ".join(dict.fromkeys(acc_vals)) if acc_vals else None,
            "surface_type": ", ".join(dict.fromkeys(surf_vals)) if surf_vals else None,
            "functional_system": ", ".join(dict.fromkeys(fsys_vals)) if fsys_vals else None,
        }
    except Exception as exc:
        LOGGER.debug(f"HPMS summary error for Route {route}: {exc}")
        return None


def find_stations_by_coordinate_range(
    route: Optional[Union[str, int]] = None,
    start_coord: Optional[Union[str, Tuple[float, float]]] = None,
    end_coord: Optional[Union[str, Tuple[float, float]]] = None,
    direction: Optional[str] = None,
    buffer_mi: float = 0.2,
    station_types: Optional[Tuple[str, ...]] = ("ML", "HV"),
    metadata_dir: Optional[str] = None,
    vds_meta: Optional[Any] = None,
    idx_ref: Optional[IndexedGeoReference] = None,
    strict_route: bool = False,
    **kwargs: Any
) -> Dict[str, Any]:
    """
    Search for PeMS detector stations along a highway route corridor bounded by a coordinate range.
    Snaps start and end coordinates onto the highway centerline to establish corridor limits.

    Intelligent features:
      - Automatic route detection if route is omitted
      - Centerline cross-validation: detects and auto-switches if user input route is miles away from coordinates
      - Highway orientation checking (e.g. notices East-West routes and searches valid directions)
      - Travel vector determination (EB/WB or NB/SB) based on coordinate sequence
      - HPMS ground-truth roadway attributes integration
      - Uninstrumented corridor diagnostics with nearest active station & Caltrans Census stations
    """
    # Normalize flexible calling arguments (route as arg 1 or start_coord as arg 1)
    if start_coord is None and end_coord is None:
        if route is not None and parse_coordinate_string(route) is not None:
            start_coord = route
            route = None
    if kwargs.get("start_coord"):
        start_coord = kwargs["start_coord"]
    if kwargs.get("end_coord"):
        end_coord = kwargs["end_coord"]
    if kwargs.get("route"):
        route = kwargs["route"]

    p_start = parse_coordinate_string(start_coord) if isinstance(start_coord, str) else start_coord
    p_end = parse_coordinate_string(end_coord) if isinstance(end_coord, str) else end_coord

    if not p_start or not p_end:
        return {
            "error": f"Invalid coordinate format. Start: '{start_coord}', End: '{end_coord}'. Expected decimal degrees or DMS (e.g. '38.186, -122.601' or '38°11\'10\"N 122°36\'05\"W')."
        }

    ref = idx_ref if idx_ref is not None else get_indexed_geo_ref()
    clean_ref = getattr(ref, "clean_df", None)
    if not isinstance(clean_ref, pd.DataFrame):
        raw = getattr(ref, "raw_df", None)
        if isinstance(raw, pd.DataFrame):
            clean_ref = raw.copy()
            r_col = "Route" if "Route" in clean_ref.columns else "Fwy"
            d_col = "Direction" if "Direction" in clean_ref.columns else "Dir"
            clean_ref["_route"] = clean_ref[r_col].astype(str).str.strip().map(normalize_route)
            clean_ref["_dir"] = clean_ref[d_col].astype(str).str.strip().map(normalize_direction)
            clean_ref["_abs_pm"] = pd.to_numeric(clean_ref["Abs PM"], errors="coerce")
            clean_ref["_county"] = clean_ref["County"].astype(str).str.strip().str.lower()
            clean_ref["_lat"] = pd.to_numeric(clean_ref["Latitude"], errors="coerce")
            clean_ref["_lng"] = pd.to_numeric(clean_ref["Longitude"], errors="coerce")
            ca_splits = clean_ref["CA PM"].astype(str).map(split_ca_pm)
            clean_ref["_ca_num"] = ca_splits.map(lambda x: x[1])
        else:
            clean_ref = get_indexed_geo_ref().clean_df

    ref_lats = getattr(ref, "lats", None)
    if not isinstance(ref_lats, pd.Series):
        ref_lats = clean_ref["_lat"]
    ref_lngs = getattr(ref, "lngs", None)
    if not isinstance(ref_lngs, pd.Series):
        ref_lngs = clean_ref["_lng"]

    auto_detected_route = False
    route_mismatch_notice = None

    # 1. Auto-detect or validate highway route
    if route is None or str(route).strip() == "":
        if hasattr(ref, "auto_detect_highway_corridor"):
            best_route, s_cand_row, e_cand_row, act_s_dist, act_e_dist = ref.auto_detect_highway_corridor(p_start, p_end)
        else:
            best_route = "101"
        route_norm = best_route
        auto_detected_route = True
    else:
        route_norm = normalize_route(route)
        sub_route_check = clean_ref[clean_ref["_route"] == route_norm]
        if sub_route_check.empty:
            return {
                "error": f"Route '{route_norm}' not found in California highway reference database."
            }
        d_s_tmp = haversine_distance_miles(p_start[0], p_start[1], ref_lats[sub_route_check.index], ref_lngs[sub_route_check.index])
        d_e_tmp = haversine_distance_miles(p_end[0], p_end[1], ref_lats[sub_route_check.index], ref_lngs[sub_route_check.index])
        s_off_spec = float(d_s_tmp.min())
        e_off_spec = float(d_e_tmp.min())

        if not strict_route and (s_off_spec > 0.5 or e_off_spec > 0.5) and hasattr(ref, "auto_detect_highway_corridor"):
            act_route, s_cand_row, e_cand_row, act_s_dist, act_e_dist = ref.auto_detect_highway_corridor(p_start, p_end)
            if act_s_dist < 0.25 and s_off_spec > (act_s_dist + 0.5):
                route_mismatch_notice = (
                    f"Specified Route {route_norm} is {s_off_spec:.2f} mi from input coordinates, "
                    f"but centerline is on Route {act_route} ({s_cand_row['County']} Co, {act_s_dist:.3f} mi away). "
                    f"Auto-switching corridor search to Route {act_route} (pass --strict-route to disable)."
                )
                route_norm = act_route

    # 2. Check highway orientation & valid directions
    dir_norm = normalize_direction(direction) if direction else None
    valid_dirs = None
    if hasattr(ref, "valid_directions_for_route") and not hasattr(ref, "_mock_return_value"):
        valid_dirs = ref.valid_directions_for_route(route_norm)
    if not isinstance(valid_dirs, (set, list, tuple)):
        valid_dirs = set(clean_ref[clean_ref["_route"] == route_norm]["_dir"].dropna().unique())
    dir_mismatch_notice = None
    if dir_norm and valid_dirs and dir_norm not in valid_dirs:
        cardinal_name = "East-West" if valid_dirs.issubset({"E", "W"}) else ("North-South" if valid_dirs.issubset({"N", "S"}) else "unaligned")
        dir_mismatch_notice = (
            f"Route {route_norm} is an {cardinal_name} highway (valid directions: {sorted(valid_dirs)}). "
            f"Ignoring mismatched direction '{direction}' and searching both directions."
        )
        dir_norm = None

    # Filter reference database to Route
    sub_route = clean_ref[clean_ref["_route"] == route_norm].copy()
    if dir_norm:
        sub_dir = sub_route[sub_route["_dir"] == dir_norm]
        if not sub_dir.empty:
            sub_route = sub_dir

    sub_route = sub_route.dropna(subset=["_lat", "_lng", "_abs_pm"])
    if sub_route.empty:
        return {
            "error": f"No valid coordinates found for Route {route_norm} in reference database."
        }

    d_start = haversine_distance_miles(p_start[0], p_start[1], sub_route["_lat"], sub_route["_lng"])
    d_end = haversine_distance_miles(p_end[0], p_end[1], sub_route["_lat"], sub_route["_lng"])

    snap_start = sub_route.loc[d_start.idxmin()]
    snap_end = sub_route.loc[d_end.idxmin()]

    start_off_mi = float(d_start.min())
    end_off_mi = float(d_end.min())

    abs_b = float(snap_start["_abs_pm"])
    abs_e = float(snap_end["_abs_pm"])
    min_abs = min(abs_b, abs_e)
    max_abs = max(abs_b, abs_e)
    length_mi = round(max_abs - min_abs, 2)

    # 3. Determine travel vector
    travel_vector = None
    if valid_dirs.issubset({"E", "W"}):
        travel_vector = "EB" if abs_b <= abs_e else "WB"
    elif valid_dirs.issubset({"N", "S"}):
        travel_vector = "NB" if abs_b <= abs_e else "SB"
    elif abs(p_end[0] - p_start[0]) > abs(p_end[1] - p_start[1]):
        travel_vector = "NB" if p_end[0] >= p_start[0] else "SB"
    else:
        travel_vector = "EB" if p_end[1] >= p_start[1] else "WB"

    # 4. Load VDS Metadata and match stations
    meta = vds_meta if vds_meta is not None else load_vds_metadata(metadata_dir)
    types_set = set(t.strip().upper() for t in station_types) if station_types and "ALL" not in station_types else None

    route_vds = [
        s for s in meta.by_id.values()
        if str(s.fwy) == str(route_norm)
    ]
    if dir_norm:
        route_vds = [s for s in route_vds if s.dir == dir_norm]
    if types_set:
        route_vds = [s for s in route_vds if s.type in types_set]

    matched_stations: List[VDSStationRecord] = []
    for s in route_vds:
        if s.abs_pm is not None:
            if (min_abs - buffer_mi) <= s.abs_pm <= (max_abs + buffer_mi):
                matched_stations.append(s)
        elif s.latitude and s.longitude:
            d_st = haversine_distance_miles(s.latitude, s.longitude, sub_route["_lat"], sub_route["_lng"])
            if d_st.min() <= buffer_mi:
                closest_seg = sub_route.loc[d_st.idxmin()]
                if min_abs <= closest_seg["_abs_pm"] <= max_abs:
                    matched_stations.append(s)

    # Deduplicate and sort
    seen_ids = set()
    unique_stations = []
    for s in matched_stations:
        if s.id not in seen_ids:
            seen_ids.add(s.id)
            unique_stations.append(s)

    unique_stations.sort(key=lambda x: (x.dir or "", x.abs_pm if x.abs_pm is not None else 0.0))

    snap_start_dist = str(snap_start.get("District", "")).replace(".0", "").strip() if "District" in snap_start else ""
    snap_end_dist = str(snap_end.get("District", "")).replace(".0", "").strip() if "District" in snap_end else ""
    if not snap_start_dist and unique_stations:
        snap_start_dist = str(unique_stations[0].district or "")
    if not snap_end_dist and unique_stations:
        snap_end_dist = str(unique_stations[-1].district or "")

    # 5. FHWA / Caltrans HPMS Ground-Truth Summary
    hpms_summary = fetch_hpms_corridor_summary(
        route=route_norm,
        abs_begin_pm=min_abs,
        abs_end_pm=max_abs,
        county=str(snap_start["County"])
    )

    # 6. Uninstrumented Diagnostics & Census Stations
    coverage_status = "INSTRUMENTED"
    coverage_details = None
    nearest_station = None
    nearest_station_dist_mi = None
    census_stations: List[Dict[str, Any]] = []

    all_route_stations = [s for s in meta.by_id.values() if str(s.fwy) == str(route_norm)]
    if not unique_stations:
        if not all_route_stations:
            coverage_status = "UNINSTRUMENTED STATEWIDE"
            coverage_details = f"Route {route_norm} has no active PeMS detector stations instrumented statewide."
        else:
            coverage_status = "UNINSTRUMENTED CORRIDOR SEGMENT"
            st_counties = sorted(set(normalize_ca_county(s.county) for s in all_route_stations if s.county))
            st_counties_str = ", ".join(st_counties)
            nst = min(all_route_stations, key=lambda s: abs((s.abs_pm if s.abs_pm is not None else 9999) - min_abs))
            nearest_station = nst
            if nst.abs_pm is not None:
                nearest_station_dist_mi = abs(nst.abs_pm - min_abs)
                coverage_details = (
                    f"Route {route_norm} has {len(all_route_stations)} active PeMS stations located in {st_counties_str} County/Counties. "
                    f"The {snap_start['County']} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f}) is not instrumented with PeMS mainline loop detectors."
                )
            else:
                coverage_details = (
                    f"Route {route_norm} has {len(all_route_stations)} active PeMS stations in {st_counties_str} County/Counties, "
                    f"but none in the {snap_start['County']} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f})."
                )

        dist_val = str(snap_start.get("District", "")).replace(".0", "").strip() or "4"
        raw_census = find_closest_census_stations(route_norm, dist_val, snap_start["_ca_num"], county=str(snap_start["County"]), top_n=3)
        if not raw_census.empty:
            for _, crow in raw_census.iterrows():
                census_stations.append({
                    "ID": str(crow["ID"]),
                    "Name": str(crow["Name"]),
                    "CA PM": str(crow["CA PM"]),
                    "Abs PM": f"{float(crow['Abs PM']):.2f}" if pd.notna(crow.get("Abs PM")) else "-",
                    "pm_diff": float(crow["pm_diff"]) if pd.notna(crow.get("pm_diff")) else 0.0,
                })

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
            "county": str(snap_start.get("County", snap_start.get("_county", ""))),
            "district": snap_start_dist or None,
            "coord": (float(snap_start["_lat"]), float(snap_start["_lng"])),
            "distance_from_route_mi": start_off_mi,
        },
        "snapped_end": {
            "abs_pm": abs_e,
            "ca_pm": str(snap_end.get("CA PM", snap_end.get("_ca_num", ""))),
            "county": str(snap_end.get("County", snap_end.get("_county", ""))),
            "district": snap_end_dist or None,
            "coord": (float(snap_end["_lat"]), float(snap_end["_lng"])),
            "distance_from_route_mi": end_off_mi,
        },
        "corridor_range": {
            "abs_min": min_abs,
            "abs_max": max_abs,
            "length_mi": length_mi,
        },
        "stations": unique_stations,
        "hpms_summary": hpms_summary,
        "coverage_status": coverage_status,
        "coverage_details": coverage_details,
        "nearest_station": nearest_station,
        "nearest_station_dist_mi": nearest_station_dist_mi,
        "census_stations": census_stations,
        "buffer_mi": buffer_mi,
    }


def format_coordinate_search_report(res: Dict[str, Any]) -> str:
    """Format coordinate corridor station search results into a clean, human-readable report."""
    if "error" in res:
        return f"\n[ERROR]: {res['error']}"

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("CALTRANS PEMS ROUTE CORRIDOR STATION SEARCH (COORDINATE RANGE)")
    rt_str = f"Route {res['route']}" + (f" {res['direction']}" if res['direction'] else " (Both Directions)")
    tv_str = f" | Travel Vector: {res['travel_vector']}" if res.get("travel_vector") else ""
    lines.append(f"{rt_str}{tv_str} | Buffer: ±{res['buffer_mi']} mi")
    lines.append("=" * 80)

    # Notices
    if res.get("route_mismatch_notice"):
        lines.append(f"\n[Route Notice]: {res['route_mismatch_notice']}")
    if res.get("dir_mismatch_notice"):
        lines.append(f"\n[Direction Notice]: {res['dir_mismatch_notice']}")
    if res.get("auto_detected_route"):
        lines.append(f"\n[Highway Auto-Detection]: No route specified. Auto-detected Route {res['route']} ({res['snapped_start']['county']} County).")

    # Snap info
    s_snap = res["snapped_start"]
    e_snap = res["snapped_end"]
    lines.append("\nInput & Snapped Coordinates:")
    lines.append(f"  • Start Coord:     ({res['input_start'][0]:.5f}, {res['input_start'][1]:.5f})")
    lines.append(f"    Highway Snap:    {s_snap['county']} Co, PM {s_snap['ca_pm']} (Abs PM {s_snap['abs_pm']:.2f}) [{s_snap['distance_from_route_mi']:.3f} mi from centerline]")
    lines.append(f"  • End Coord:       ({res['input_end'][0]:.5f}, {res['input_end'][1]:.5f})")
    lines.append(f"    Highway Snap:    {e_snap['county']} Co, PM {e_snap['ca_pm']} (Abs PM {e_snap['abs_pm']:.2f}) [{e_snap['distance_from_route_mi']:.3f} mi from centerline]")

    c_range = res["corridor_range"]
    lines.append(f"\nCorridor Limits:     Abs PM {c_range['abs_min']:.2f} - {c_range['abs_max']:.2f} (Length: {c_range['length_mi']:.2f} mi)")
    if res.get("travel_vector"):
        lines.append(f"  • Travel Vector:   {res['travel_vector']} (Based on start -> end coordinate sequence)")

    # HPMS Ground-Truth Roadway Attributes Section
    hpms_summary = res.get("hpms_summary")
    if hpms_summary:
        lines.append("\n[FHWA / Caltrans HPMS Ground-Truth Roadway Attributes]")
        pred_lanes = hpms_summary.get("predominant_lanes_2024")
        avg_lanes = hpms_summary.get("avg_lanes_2024")
        lane_str = f"{pred_lanes}" if pred_lanes is not None else "N/A"
        if avg_lanes is not None and avg_lanes != pred_lanes:
            lane_str += f" (Corridor length-weighted avg: {avg_lanes:.2f})"
        lines.append(f"  • Through Lanes (2024):         {lane_str}")
        if hpms_summary.get("facility_type"):
            lines.append(f"  • Facility Type:                {hpms_summary['facility_type']}")
        if hpms_summary.get("access_control"):
            lines.append(f"  • Access Control:               {hpms_summary['access_control']}")
        if hpms_summary.get("surface_type"):
            lines.append(f"  • Surface Type:                 {hpms_summary['surface_type']}")
        if hpms_summary.get("functional_system"):
            lines.append(f"  • Functional System:            {hpms_summary['functional_system']}")
        if hpms_summary.get("avg_aadt_2024"):
            lines.append(f"  • Certified AADT (2024):        {hpms_summary['avg_aadt_2024']:,} vehicles/day")
        if hpms_summary.get("future_aadt"):
            yr_str = f" (Year {hpms_summary['future_aadt_year']})" if hpms_summary.get('future_aadt_year') else ""
            try:
                fa = int(float(hpms_summary['future_aadt']))
                lines.append(f"  • Forecast Future AADT:         {fa:,} vehicles/day{yr_str}")
            except Exception:
                lines.append(f"  • Forecast Future AADT:         {hpms_summary['future_aadt']}{yr_str}")
        if hpms_summary.get("sections_count"):
            lines.append(f"  • HPMS Corridor Sections:       {hpms_summary['sections_count']} sections analyzed")
        lines.append("-" * 80)

    stations: List[VDSStationRecord] = res["stations"]
    lines.append(f"Matching Stations:   {len(stations)} found in corridor\n")

    if not stations:
        lines.append("  (No PeMS detector stations found in this corridor range)")
        if res.get("coverage_details"):
            lines.append(f"\n[Corridor Instrumentation & PeMS VDS Status]")
            lines.append(f"  • Coverage:        {res.get('coverage_status', 'UNINSTRUMENTED')}")
            lines.append(f"  • Details:         {res['coverage_details']}")
            if res.get("nearest_station"):
                nst = res["nearest_station"]
                nst_pm = f"Abs PM {nst.abs_pm:.2f}" if getattr(nst, "abs_pm", None) is not None else ""
                dist_str = f", {res['nearest_station_dist_mi']:.2f} mi away." if res.get("nearest_station_dist_mi") is not None else "."
                lines.append(f"  • Nearest Station: Station {nst.id} ({nst.name}, {nst_pm}){dist_str}")
        if res.get("census_stations"):
            lines.append(f"\n[Caltrans Annual Census Traffic Stations]")
            for c in res["census_stations"]:
                loc_hint = f"[{c['pm_diff']:.2f} mi from start PM]" if c['pm_diff'] > 0 else "[Inside corridor]"
                lines.append(f"  • Station {c['ID']}: {c['Name']} (CA PM {c['CA PM']}, Abs PM {c['Abs PM']}) {loc_hint}")
        lines.append("=" * 80)
        lines.append("Tip: Pass --lanes to inspect lane configuration tables & TSN metrics for this corridor.")
        return "\n".join(lines)

    # Station table
    cols = ["Station ID", "Dir", "Type", "Lanes", "CA PM", "Abs PM", "Name", "Coordinates"]
    rows = []
    for s in stations:
        pm_str = str(s.state_pm) if s.state_pm else "-"
        abs_str = f"{s.abs_pm:.2f}" if s.abs_pm is not None else "-"
        coord_str = f"({s.latitude:.5f}, {s.longitude:.5f})" if s.latitude and s.longitude else "-"
        lanes_str = str(s.lanes) if s.lanes is not None else "-"
        rows.append([
            str(s.id),
            s.dir or "-",
            s.type or "-",
            lanes_str,
            pm_str,
            abs_str,
            s.name or "-",
            coord_str
        ])

    col_widths = [max(len(cols[i]), max(len(r[i]) for r in rows)) for i in range(len(cols))]
    hdr_str = "  " + "  ".join(f"{cols[i]:<{col_widths[i]}}" for i in range(len(cols)))
    sep_str = "  " + "  ".join("-" * col_widths[i] for i in range(len(cols)))

    lines.append(hdr_str)
    lines.append(sep_str)
    for r in rows:
        row_str = "  " + "  ".join(f"{r[i]:<{col_widths[i]}}" for i in range(len(cols)))
        lines.append(row_str)

    lines.append("=" * 80)
    lines.append("Tip: Pass --lanes to inspect lane configuration tables & TSN metrics for these stations.")
    return "\n".join(lines)


CALTRANS_COUNTY_ABBREVIATIONS: Dict[str, str] = {
    "ala": "Alameda", "alp": "Alpine", "ama": "Amador", "but": "Butte", "cal": "Calaveras",
    "col": "Colusa", "cc": "Contra Costa", "dn": "Del Norte", "ed": "El Dorado", "fre": "Fresno",
    "gle": "Glenn", "hum": "Humboldt", "imp": "Imperial", "iny": "Inyo", "ker": "Kern",
    "kin": "Kings", "lak": "Lake", "las": "Lassen", "la": "Los Angeles", "mad": "Madera",
    "mrn": "Marin", "mpa": "Mariposa", "men": "Mendocino", "mer": "Merced", "mod": "Modoc",
    "mno": "Mono", "mon": "Monterey", "nap": "Napa", "nev": "Nevada", "ora": "Orange",
    "pla": "Placer", "plu": "Plumas", "riv": "Riverside", "sac": "Sacramento", "sbt": "San Benito",
    "sbd": "San Bernardino", "sd": "San Diego", "sf": "San Francisco", "sj": "San Joaquin",
    "slo": "San Luis Obispo", "sm": "San Mateo", "sba": "Santa Barbara", "scl": "Santa Clara",
    "scr": "Santa Cruz", "sha": "Shasta", "sie": "Sierra", "sis": "Siskiyou", "sol": "Solano",
    "son": "Sonoma", "sta": "Stanislaus", "sut": "Sutter", "teh": "Tehama", "tri": "Trinity",
    "tul": "Tulare", "tuo": "Tuolumne", "ven": "Ventura", "yol": "Yolo", "yub": "Yuba"
}


def normalize_ca_county(county: Optional[str]) -> str:
    """Normalize California county name, abbreviation, or FIPS code (e.g. 'SON' -> 'Sonoma', 'cc' -> 'Contra Costa', '55' -> 'Napa')."""
    if not county or not str(county).strip():
        return ""
    c_raw = str(county).strip()
    try:
        val_int = int(float(c_raw))
        from hpms_interface import CA_FIPS_TO_COUNTY
        if val_int in CA_FIPS_TO_COUNTY:
            return CA_FIPS_TO_COUNTY[val_int]
    except Exception:
        pass

    c_lower = c_raw.lower()
    if c_lower in CALTRANS_COUNTY_ABBREVIATIONS:
        return CALTRANS_COUNTY_ABBREVIATIONS[c_lower]
    return c_raw.title()


def convert_california_postmile(
    route: Union[str, int],
    county: str,
    ca_pm: Union[str, float, int],
    direction: Optional[str] = None,
    ref_df: Optional[Any] = None,
    idx_ref: Optional[IndexedGeoReference] = None,
) -> Dict[str, Any]:
    """
    Convert a California county postmile (CA PM) to an Absolute Postmile (Abs PM)
    and geographic centerline coordinates (lat, lng).
    """
    if not county or not str(county).strip():
        return {"error": "County must be provided to convert a California postmile."}

    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction) if direction else None
    county_norm = normalize_ca_county(county)

    ref = idx_ref if idx_ref is not None else get_indexed_geo_ref()
    raw_df = ref.raw_df

    sub = raw_df[(raw_df["Route"].astype(str) == str(route_norm)) & (raw_df["County"].astype(str).str.lower() == county_norm.lower())].copy()
    if sub.empty:
        return {"error": f"Route '{route_norm}' in County '{county_norm}' not found in California highway database."}

    pfx, num = split_ca_pm(ca_pm)
    if np.isnan(num):
        return {"error": f"Invalid postmile format: '{ca_pm}'"}

    sub["_num"] = pd.to_numeric(sub["CA PM"].astype(str).str.replace(r"^[A-Za-z]+", "", regex=True), errors="coerce")
    sub["_abs"] = pd.to_numeric(sub["Abs PM"], errors="coerce")
    sub = sub.dropna(subset=["_num", "_abs"])

    if sub.empty:
        return {"error": f"No valid postmile numbers found for Route {route_norm} in {county_norm} County."}

    matched = sub
    if dir_norm:
        sub_dir = sub[sub["Direction"] == dir_norm]
        if not sub_dir.empty and (sub_dir["_num"].min() - 1.0 <= num <= sub_dir["_num"].max() + 1.0):
            matched = sub_dir

    if pfx:
        pfx_match = matched[matched["CA PM"].astype(str).str.upper().str.startswith(pfx.upper())]
        if not pfx_match.empty:
            matched = pfx_match

    diff = (matched["_num"] - num).abs()
    best_row = matched.loc[diff.idxmin()]
    abs_pm = float(best_row["_abs"])
    dist_val = str(best_row.get("District", "")).replace(".0", "").strip()

    # Directional coordinate resolution
    lat, lng = float(best_row["Latitude"]), float(best_row["Longitude"])
    if dir_norm:
        dir_rows = raw_df[(raw_df["Route"].astype(str) == str(route_norm)) & (raw_df["Direction"] == dir_norm)]
        if not dir_rows.empty:
            dir_diff = (pd.to_numeric(dir_rows["Abs PM"], errors="coerce") - abs_pm).abs()
            coord_row = dir_rows.loc[dir_diff.idxmin()]
            lat, lng = float(coord_row["Latitude"]), float(coord_row["Longitude"])

    return {
        "conversion_type": "CA_TO_ABS",
        "route": str(route_norm),
        "county": county_norm,
        "district": dist_val,
        "direction": dir_norm or str(best_row.get("Direction", "")),
        "ca_pm": str(ca_pm),
        "ca_pm_snapped": str(best_row["CA PM"]),
        "ca_pm_num": num,
        "ca_pm_prefix": pfx,
        "abs_pm": round(abs_pm, 3),
        "coord": (lat, lng),
    }


def convert_absolute_postmile(
    route: Union[str, int],
    abs_pm: Union[str, float, int],
    direction: Optional[str] = None,
    ref_df: Optional[Any] = None,
    idx_ref: Optional[IndexedGeoReference] = None,
) -> Dict[str, Any]:
    """
    Convert a continuous Absolute Postmile (Abs PM) to a California County Postmile (CA PM),
    identifying the county, district, and geographic centerline coordinates (lat, lng).
    """
    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction) if direction else None

    ref = idx_ref if idx_ref is not None else get_indexed_geo_ref()
    raw_df = ref.raw_df

    sub = raw_df[raw_df["Route"].astype(str) == str(route_norm)].copy()
    if sub.empty:
        return {"error": f"Route '{route_norm}' not found in California highway database."}

    try:
        target_abs = float(str(abs_pm).strip())
    except (TypeError, ValueError):
        return {"error": f"Invalid Absolute Postmile format: '{abs_pm}'"}

    sub["_abs"] = pd.to_numeric(sub["Abs PM"], errors="coerce")
    sub = sub.dropna(subset=["_abs"])
    if sub.empty:
        return {"error": f"No valid Absolute Postmiles found for Route {route_norm}."}

    matched = sub
    if dir_norm:
        sub_dir = sub[sub["Direction"] == dir_norm]
        if not sub_dir.empty:
            matched = sub_dir

    diff = (matched["_abs"] - target_abs).abs()
    best_row = matched.loc[diff.idxmin()]
    delta_mi = float(diff.loc[best_row.name])
    snapped_abs = float(best_row["_abs"])
    county_val = normalize_ca_county(best_row["County"])
    dist_val = str(best_row.get("District", "")).replace(".0", "").strip()
    ca_pm_val = str(best_row.get("CA PM", best_row.get("State_PM", "")))
    pfx, ca_num = split_ca_pm(ca_pm_val)

    # Directional coordinate resolution
    lat, lng = float(best_row["Latitude"]), float(best_row["Longitude"])
    if dir_norm and best_row["Direction"] != dir_norm:
        dir_rows = raw_df[(raw_df["Route"].astype(str) == str(route_norm)) & (raw_df["Direction"] == dir_norm)]
        if not dir_rows.empty:
            dir_diff = (pd.to_numeric(dir_rows["Abs PM"], errors="coerce") - snapped_abs).abs()
            coord_row = dir_rows.loc[dir_diff.idxmin()]
            lat, lng = float(coord_row["Latitude"]), float(coord_row["Longitude"])

    return {
        "conversion_type": "ABS_TO_CA",
        "route": str(route_norm),
        "county": county_val,
        "district": dist_val,
        "direction": dir_norm or str(best_row.get("Direction", "")),
        "abs_pm": round(target_abs, 3),
        "snapped_abs_pm": round(snapped_abs, 3),
        "delta_mi": round(delta_mi, 4),
        "ca_pm": ca_pm_val,
        "ca_pm_snapped": ca_pm_val,
        "ca_pm_num": ca_num,
        "ca_pm_prefix": pfx,
        "coord": (lat, lng),
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
    idx_ref: Optional[IndexedGeoReference] = None,
    vds_meta: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Search for PeMS detector stations along a specified California Route corridor bounded by California Postmiles or Absolute Postmiles.
    Converts postmiles to continuous Absolute Postmiles and coordinates, and matches stations within buffer.
    """
    b_co = begin_county or county
    e_co = end_county or county
    ref = idx_ref if idx_ref is not None else get_indexed_geo_ref()

    if is_absolute or (not b_co and not e_co):
        start_info = convert_absolute_postmile(route, begin_pm, direction=direction, idx_ref=ref)
        if "error" in start_info:
            return start_info
        end_info = convert_absolute_postmile(route, end_pm, direction=direction, idx_ref=ref)
        if "error" in end_info:
            return end_info
        b_co = start_info["county"]
        e_co = end_info["county"]
    else:
        start_info = convert_california_postmile(route, b_co or e_co, begin_pm, direction=direction, idx_ref=ref)
        if "error" in start_info:
            return start_info
        end_info = convert_california_postmile(route, e_co or b_co, end_pm, direction=direction, idx_ref=ref)
        if "error" in end_info:
            return end_info

    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction) if direction else None

    # Validate direction against route
    valid_dirs = ref.valid_directions_for_route(route_norm)
    dir_mismatch_notice = None
    if dir_norm and valid_dirs and dir_norm not in valid_dirs:
        cardinal_name = "East-West" if valid_dirs.issubset({"E", "W"}) else ("North-South" if valid_dirs.issubset({"N", "S"}) else "unaligned")
        dir_mismatch_notice = (
            f"Route {route_norm} is an {cardinal_name} highway (valid directions: {sorted(valid_dirs)}). "
            f"Ignoring mismatched direction '{direction}' and searching both directions."
        )
        dir_norm = None

    abs_b = float(start_info["abs_pm"])
    abs_e = float(end_info["abs_pm"])
    min_abs = min(abs_b, abs_e)
    max_abs = max(abs_b, abs_e)
    length_mi = round(max_abs - min_abs, 2)

    meta = vds_meta if vds_meta is not None else load_vds_metadata(metadata_dir)
    types_set = set(t.strip().upper() for t in station_types) if station_types and "ALL" not in station_types else None

    route_vds = [
        s for s in meta.by_id.values()
        if str(s.fwy) == str(route_norm)
    ]
    if dir_norm:
        route_vds = [s for s in route_vds if s.dir == dir_norm]
    if types_set:
        route_vds = [s for s in route_vds if s.type in types_set]

    matched_stations: List[VDSStationRecord] = []
    for s in route_vds:
        if s.abs_pm is not None:
            if (min_abs - buffer_mi) <= s.abs_pm <= (max_abs + buffer_mi):
                matched_stations.append(s)

    seen_ids = set()
    unique_stations = []
    for s in matched_stations:
        if s.id not in seen_ids:
            seen_ids.add(s.id)
            unique_stations.append(s)

    unique_stations.sort(key=lambda x: (x.dir or "", x.abs_pm if x.abs_pm is not None else 0.0))

    co_display = b_co if b_co == e_co else f"{b_co} to {e_co}"

    # Query FHWA / Caltrans HPMS summary
    hpms_summary = fetch_hpms_corridor_summary(
        route=route_norm,
        abs_begin_pm=min_abs,
        abs_end_pm=max_abs,
        county=b_co
    )

    # Uninstrumented diagnostics & census stations
    coverage_status = "INSTRUMENTED"
    coverage_details = None
    nearest_station = None
    nearest_station_dist_mi = None
    census_stations: List[Dict[str, Any]] = []

    all_route_stations = [s for s in meta.by_id.values() if str(s.fwy) == str(route_norm)]
    if not unique_stations:
        if not all_route_stations:
            coverage_status = "UNINSTRUMENTED STATEWIDE"
            coverage_details = f"Route {route_norm} has no active PeMS detector stations instrumented statewide."
        else:
            coverage_status = "UNINSTRUMENTED CORRIDOR SEGMENT"
            st_counties = sorted(set(normalize_ca_county(s.county) for s in all_route_stations if s.county))
            st_counties_str = ", ".join(st_counties)
            nst = min(all_route_stations, key=lambda s: abs((s.abs_pm if s.abs_pm is not None else 9999) - min_abs))
            nearest_station = nst
            if nst.abs_pm is not None:
                nearest_station_dist_mi = abs(nst.abs_pm - min_abs)
                coverage_details = (
                    f"Route {route_norm} has {len(all_route_stations)} active PeMS stations located in {st_counties_str} County/Counties. "
                    f"The {b_co} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f}) is not instrumented with PeMS mainline loop detectors."
                )
            else:
                coverage_details = (
                    f"Route {route_norm} has {len(all_route_stations)} active PeMS stations in {st_counties_str} County/Counties, "
                    f"but none in the {b_co} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f})."
                )

        dist_val = str(start_info.get("district") or end_info.get("district") or "4")
        _, b_num = split_ca_pm(begin_pm)
        raw_census = find_closest_census_stations(route_norm, dist_val, b_num if not np.isnan(b_num) else 0.0, county=b_co, top_n=3)
        if not raw_census.empty:
            for _, crow in raw_census.iterrows():
                census_stations.append({
                    "ID": str(crow["ID"]),
                    "Name": str(crow["Name"]),
                    "CA PM": str(crow["CA PM"]),
                    "Abs PM": f"{float(crow['Abs PM']):.2f}" if pd.notna(crow.get("Abs PM")) else "-",
                    "pm_diff": float(crow["pm_diff"]) if pd.notna(crow.get("pm_diff")) else 0.0,
                })

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
        "corridor_range": {
            "abs_min": min_abs,
            "abs_max": max_abs,
            "length_mi": length_mi,
        },
        "stations": unique_stations,
        "hpms_summary": hpms_summary,
        "coverage_status": coverage_status,
        "coverage_details": coverage_details,
        "nearest_station": nearest_station,
        "nearest_station_dist_mi": nearest_station_dist_mi,
        "census_stations": census_stations,
        "buffer_mi": buffer_mi,
    }


def format_single_postmile_report(res: Dict[str, Any]) -> str:
    """Format single postmile conversion results into a clean, human-readable report."""
    if "error" in res:
        return f"\n[ERROR]: {res['error']}"

    lines: List[str] = []
    lines.append("=" * 80)
    rt_dir = f"Route {res['route']}" + (f" {res['direction']}" if res.get('direction') else "")

    if res.get("conversion_type") == "ABS_TO_CA":
        lines.append(f"CALIFORNIA POSTMILE CONVERSION: Absolute PM -> California County PM")
        lines.append(f"{rt_dir} | Target Abs PM: {res['abs_pm']}")
        lines.append("=" * 80)
        lines.append(f"  • California County Postmile:   {res['county']} County PM {res['ca_pm']}")
        lines.append(f"  • Caltrans District:            District {res['district']} ({res['county']} County)")
        lines.append(f"  • Centerline Coordinates:       ({res['coord'][0]:.5f}, {res['coord'][1]:.5f})")
        lines.append(f"  • Snapped Reference Postmile:   Abs PM {res['snapped_abs_pm']:.3f} (delta: {res['delta_mi']:.3f} mi)")
    else:
        lines.append(f"CALIFORNIA POSTMILE CONVERSION: California County PM -> Absolute PM")
        lines.append(f"{rt_dir} | County: {res['county']} | CA PM: {res['ca_pm']}")
        lines.append("=" * 80)
        lines.append(f"  • Converted Absolute Postmile:  Abs PM {res['abs_pm']:.3f}")
        lines.append(f"  • Centerline Coordinates:       ({res['coord'][0]:.5f}, {res['coord'][1]:.5f})")
        lines.append(f"  • Caltrans District:            District {res['district']}")
        lines.append(f"  • Snapped Centerline Segment:   Route {res['route']} {res.get('direction', '')} {res['county']} PM {res.get('ca_pm_snapped', res['ca_pm'])}")
    lines.append("=" * 80)
    return "\n".join(lines)


def format_postmile_range_report(res: Dict[str, Any]) -> str:
    """Format postmile range conversion and station search results into a clean, human-readable report."""
    if "error" in res:
        return f"\n[ERROR]: {res['error']}"

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("CALTRANS PEMS ROUTE CORRIDOR POSTMILE QUERY & CONVERSION")
    rt_str = f"Route {res['route']}" + (f" {res['direction']}" if res['direction'] else " (Both Directions)")
    lines.append(f"{rt_str} | {res['county']} | CA PM {res['begin_pm']} - {res['end_pm']}")
    lines.append("=" * 80)

    if res.get("dir_mismatch_notice"):
        lines.append(f"\n[Direction Notice]: {res['dir_mismatch_notice']}")

    s_info = res["start"]
    e_info = res["end"]
    c_range = res["corridor_range"]

    lines.append("\nCorridor Conversion & Limits:")
    lines.append(f"  • Begin Postmile:  {res['begin_county']} Co, PM {res['begin_pm']} -> Abs PM {s_info['abs_pm']:.3f} | Coord: ({s_info['coord'][0]:.5f}, {s_info['coord'][1]:.5f})")
    lines.append(f"  • End Postmile:    {res['end_county']} Co, PM {res['end_pm']} -> Abs PM {e_info['abs_pm']:.3f} | Coord: ({e_info['coord'][0]:.5f}, {e_info['coord'][1]:.5f})")
    lines.append(f"  • Corridor Limits: Abs PM {c_range['abs_min']:.2f} - {c_range['abs_max']:.2f} (Length: {c_range['length_mi']:.2f} mi)")
    dist_val = s_info.get("district") or e_info.get("district") or "N/A"
    lines.append(f"  • Caltrans District: District {dist_val}")

    # HPMS Ground-Truth Roadway Attributes Section
    hpms_summary = res.get("hpms_summary")
    if hpms_summary:
        lines.append("\n[FHWA / Caltrans HPMS Ground-Truth Roadway Attributes]")
        pred_lanes = hpms_summary.get("predominant_lanes_2024")
        avg_lanes = hpms_summary.get("avg_lanes_2024")
        lane_str = f"{pred_lanes}" if pred_lanes is not None else "N/A"
        if avg_lanes is not None and avg_lanes != pred_lanes:
            lane_str += f" (Corridor length-weighted avg: {avg_lanes:.2f})"
        lines.append(f"  • Through Lanes (2024):         {lane_str}")
        if hpms_summary.get("facility_type"):
            lines.append(f"  • Facility Type:                {hpms_summary['facility_type']}")
        if hpms_summary.get("access_control"):
            lines.append(f"  • Access Control:               {hpms_summary['access_control']}")
        if hpms_summary.get("surface_type"):
            lines.append(f"  • Surface Type:                 {hpms_summary['surface_type']}")
        if hpms_summary.get("functional_system"):
            lines.append(f"  • Functional System:            {hpms_summary['functional_system']}")
        if hpms_summary.get("avg_aadt_2024"):
            lines.append(f"  • Certified AADT (2024):        {hpms_summary['avg_aadt_2024']:,} vehicles/day")
        if hpms_summary.get("future_aadt"):
            yr_str = f" (Year {hpms_summary['future_aadt_year']})" if hpms_summary.get('future_aadt_year') else ""
            try:
                fa = int(float(hpms_summary['future_aadt']))
                lines.append(f"  • Forecast Future AADT:         {fa:,} vehicles/day{yr_str}")
            except Exception:
                lines.append(f"  • Forecast Future AADT:         {hpms_summary['future_aadt']}{yr_str}")
        if hpms_summary.get("sections_count"):
            lines.append(f"  • HPMS Corridor Sections:       {hpms_summary['sections_count']} sections analyzed")
        lines.append("-" * 80)

    stations: List[VDSStationRecord] = res["stations"]
    lines.append(f"Matching Stations:   {len(stations)} found in corridor\n")

    if not stations:
        lines.append("  (No PeMS detector stations found in this corridor range)")
        if res.get("coverage_details"):
            lines.append(f"\n[Corridor Instrumentation & PeMS VDS Status]")
            lines.append(f"  • Coverage:        {res.get('coverage_status', 'UNINSTRUMENTED')}")
            lines.append(f"  • Details:         {res['coverage_details']}")
            if res.get("nearest_station"):
                nst = res["nearest_station"]
                nst_pm = f"Abs PM {nst.abs_pm:.2f}" if getattr(nst, "abs_pm", None) is not None else ""
                dist_str = f", {res['nearest_station_dist_mi']:.2f} mi away." if res.get("nearest_station_dist_mi") is not None else "."
                lines.append(f"  • Nearest Station: Station {nst.id} ({nst.name}, {nst_pm}){dist_str}")
        if res.get("census_stations"):
            lines.append(f"\n[Caltrans Annual Census Traffic Stations]")
            for c in res["census_stations"]:
                loc_hint = f"[{c['pm_diff']:.2f} mi from start PM]" if c['pm_diff'] > 0 else "[Inside corridor]"
                lines.append(f"  • Station {c['ID']}: {c['Name']} (CA PM {c['CA PM']}, Abs PM {c['Abs PM']}) {loc_hint}")
        lines.append("=" * 80)
        lines.append("Tip: Pass --map to generate an interactive HTML map, or --lanes to inspect lane tables.")
        return "\n".join(lines)

    cols = ["Station ID", "Dir", "Type", "Lanes", "CA PM", "Abs PM", "Name", "Coordinates"]
    rows = []
    for s in stations:
        pm_str = str(s.state_pm) if s.state_pm else "-"
        abs_str = f"{s.abs_pm:.2f}" if s.abs_pm is not None else "-"
        coord_str = f"({s.latitude:.5f}, {s.longitude:.5f})" if s.latitude and s.longitude else "-"
        lanes_str = str(s.lanes) if s.lanes is not None else "-"
        rows.append([
            str(s.id),
            s.dir or "-",
            s.type or "-",
            lanes_str,
            pm_str,
            abs_str,
            s.name or "-",
            coord_str
        ])

    col_widths = [max(len(cols[i]), max(len(r[i]) for r in rows)) for i in range(len(cols))]
    hdr_str = "  " + "  ".join(f"{cols[i]:<{col_widths[i]}}" for i in range(len(cols)))
    sep_str = "  " + "  ".join("-" * col_widths[i] for i in range(len(cols)))

    lines.append(hdr_str)
    lines.append(sep_str)
    for r in rows:
        row_str = "  " + "  ".join(f"{r[i]:<{col_widths[i]}}" for i in range(len(cols)))
        lines.append(row_str)

    lines.append("=" * 80)
    lines.append("Tip: Pass --map to generate an interactive HTML map, or --lanes to inspect lane tables.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PROJECT RECORD & MASTER CSV PARSER
# ---------------------------------------------------------------------------
def build_date_from_parts(
    year: Any,
    month: Any = None,
    day: Any = None,
    default_month: int = 1,
    default_day: int = 1
) -> Optional[datetime.date]:
    """
    Construct a datetime.date directly from separate numeric year, month, and day columns.
    Eliminates date string parsing and formatting discrepancies from spreadsheet editors.
    """
    if year is None or pd.isna(year):
        return None
    try:
        y_int = int(float(year))
        if not (1950 <= y_int <= 2040):
            return None
    except (ValueError, TypeError, OverflowError):
        return None

    try:
        m_int = int(float(month)) if month is not None and pd.notna(month) else default_month
        if not (1 <= m_int <= 12):
            m_int = default_month
    except (ValueError, TypeError, OverflowError):
        m_int = default_month

    try:
        d_int = int(float(day)) if day is not None and pd.notna(day) else default_day
        if not (1 <= d_int <= 31):
            d_int = default_day
    except (ValueError, TypeError, OverflowError):
        d_int = default_day

    try:
        return datetime.date(y_int, m_int, d_int)
    except ValueError:
        # Handle invalid day for month (e.g. Feb 30, April 31)
        if d_int > 28:
            for fallback_d in (30, 29, 28):
                try:
                    return datetime.date(y_int, m_int, fallback_d)
                except ValueError:
                    pass
        return datetime.date(y_int, m_int, 1)


def parse_iso_date(val: Any, default_month: int = 1, default_day: int = 1) -> Optional[datetime.date]:
    """Simple parser for ISO date strings (YYYY-MM-DD) or explicit 4-digit years from CLI/code."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val if isinstance(val, datetime.date) else val.date()
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "nat", ""):
        return None
    try:
        return datetime.date.fromisoformat(s.split()[0])
    except ValueError:
        pass
    try:
        y = int(float(s))
        if 1950 <= y <= 2040:
            return datetime.date(y, default_month, default_day)
    except (ValueError, TypeError):
        pass
    return None


def _parse_project_date(val: Any, default_month: int = 1, default_day: int = 1) -> Optional[datetime.date]:
    """Compatibility wrapper that converts ISO string or year number to datetime.date."""
    return parse_iso_date(val, default_month, default_day)


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
    project_description: str = ""

    @property
    def pems_direction(self) -> str:
        return normalize_direction(self.direction)

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
        val = self.raw_row.get("open_traffic_year")
        if val is not None and pd.notna(val):
            try:
                y = int(float(val))
                if 1950 <= y <= 2040:
                    return min(y + 4, 2026)
            except (ValueError, TypeError):
                pass
        if self.open_to_traffic_date:
            try:
                return min(int(str(self.open_to_traffic_date)[:4]) + 4, 2026)
            except (ValueError, TypeError):
                pass
        return 2024


def parse_master_row(row: Dict[str, Any], row_index: int = -1) -> ProjectRecord:
    """Convert a dictionary row from the master CSV into a clean ProjectRecord."""
    pid = str(row.get("Project ID", "")).strip()
    dist = str(row.get("Dist", "")).replace(".0", "").strip()
    county_raw = str(row.get("County", "")).strip()
    county = "" if county_raw.lower() in ("nan", "none") else county_raw
    state_raw = str(row.get("State", "CA")).strip()
    state = "CA" if state_raw.lower() in ("nan", "none", "") else state_raw
    route_raw = str(row.get("Route", "")).strip()
    route = "" if route_raw.lower() in ("nan", "none") else route_raw
    dir_raw = str(row.get("Direction", "")).strip().upper()
    direction = "" if dir_raw.lower() in ("nan", "none") else dir_raw
    begin_pm = format_pm(str(row.get("Begin PM", "")).strip())
    end_pm = format_pm(str(row.get("End PM", "")).strip())

    desc_raw = str(row.get("Project Description", row.get("description", row.get("project_description", "")))).strip()
    project_description = "" if desc_raw.lower() in ("nan", "none") else desc_raw

    # Absolute PMs
    abs_beg = None
    abs_end = None
    for k in ("temp_abs_begin", "Absolute Begin PM", "Abs Begin PM"):
        if k in row and pd.notna(row[k]):
            try:
                v = float(row[k])
                if not np.isnan(v):
                    abs_beg = round(v, 3)
                    break
            except (ValueError, TypeError):
                pass

    for k in ("temp_abs_end", "Absolute End PM", "Abs End PM"):
        if k in row and pd.notna(row[k]):
            try:
                v = float(row[k])
                if not np.isnan(v):
                    abs_end = round(v, 3)
                    break
            except (ValueError, TypeError):
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

    # Dates: construct directly from the 3 separate numeric columns (year, month, day)
    c_dt = build_date_from_parts(
        row.get("construction_begin_year"),
        row.get("construction_begin_month"),
        row.get("construction_begin_day"),
        default_month=1, default_day=1
    )
    c_begin = c_dt.isoformat() if c_dt else None
    if not c_begin:
        # Fallback for synthetic/test rows without separate columns
        for k in ("Begin Date (formatted YYYY-MM-DD)", "Construction Begin Date"):
            val = str(row.get(k, "")).strip()
            dt = parse_iso_date(val, default_month=1, default_day=1)
            if dt:
                c_begin = dt.isoformat()
                break

    o_dt = build_date_from_parts(
        row.get("open_traffic_year"),
        row.get("open_traffic_month"),
        row.get("open_traffic_day"),
        default_month=12, default_day=31
    )
    o_traffic = o_dt.isoformat() if o_dt else None
    if not o_traffic:
        # Fallback for synthetic/test rows without separate columns
        for k in ("Open to Traffic Date (formatted YYYY-MM-DD)", "Open to Traffic Date"):
            val = str(row.get(k, "")).strip()
            dt = parse_iso_date(val, default_month=12, default_day=31)
            if dt:
                o_traffic = dt.isoformat()
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
        row_index=row_index,
        project_description=project_description
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

    def resolve_absolute_postmiles(self, force_recalculate: bool = False) -> Tuple[Optional[float], Optional[float]]:
        """Calculate and resolve absolute postmiles from California postmiles if not already present,
        or if force_recalculate is True.
        """
        if force_recalculate or self.project.abs_begin_pm is None or self.project.abs_end_pm is None:
            calc_b, calc_e = resolve_absolute_postmiles(
                self.project.route,
                self.project.county,
                self.project.begin_pm,
                self.project.end_pm,
                ref_df=self.ref_df
            )
            if calc_b is not None and calc_e is not None:
                if force_recalculate or self.project.abs_begin_pm is None:
                    self.project.abs_begin_pm = calc_b
                if force_recalculate or self.project.abs_end_pm is None:
                    self.project.abs_end_pm = calc_e
        return self.project.abs_begin_pm, self.project.abs_end_pm

    def resolve_geometry(self) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
        """Resolve start and end coordinates from postmiles if not already present."""
        start = self.project.start_coords
        end = self.project.end_coords

        # Ensure absolute postmiles are calculated from CA PMs if missing
        self.resolve_absolute_postmiles()

        is_abs = (
            self.project.abs_begin_pm is not None
            and not np.isnan(self.project.abs_begin_pm)
            and self.project.abs_end_pm is not None
            and not np.isnan(self.project.abs_end_pm)
        )
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
        is_abs = (
            self.project.abs_begin_pm is not None
            and not np.isnan(self.project.abs_begin_pm)
            and self.project.abs_end_pm is not None
            and not np.isnan(self.project.abs_end_pm)
        )
        if is_abs:
            b_pm = float(self.project.abs_begin_pm)
            e_pm = float(self.project.abs_end_pm)
        else:
            _, b_num = split_ca_pm(self.project.begin_pm)
            _, e_num = split_ca_pm(self.project.end_pm)
            b_pm = b_num
            e_pm = e_num

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
        if s_yr > e_yr:
            s_yr, e_yr = e_yr, s_yr

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
        station_config_urls = {}

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
            station_config_urls[st_id] = build_station_config_url(st_id)

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
            "station_summary_urls": station_summary_urls,
            "station_config_urls": station_config_urls
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
        clean["_county"] = clean["County"].astype(str).str.strip().str.lower()
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
        station_types: Tuple[str, ...] = ("ML", "HV"),
        county: Optional[str] = None
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
            if county:
                county_clean = str(county).strip().lower()
                c_sub = sub[sub["_county"] == county_clean]
                if not c_sub.empty:
                    sub = c_sub
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
    curr_yr = datetime.datetime.now().year
    probe_years = tuple(range(curr_yr, 2019, -1))

    for d in districts:
        cache_file = os.path.join(target_dir, f"d{int(d):02d}_meta.txt")
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 1000:
            downloaded[d] = cache_file
            continue

        found = False
        for y in probe_years:
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
    project_description: str = ""


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
        station_types=station_types,
        county=project.county
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
            if project.county and st.county and st.county.lower() != project.county.lower():
                reasons.append(f"Different County (station is {st.county}, project is {project.county})")
            if station_types and "ALL" not in [t.upper().strip() for t in station_types]:
                if st.type not in {t.upper().strip() for t in station_types}:
                    reasons.append(f"Filtered Station Type: {st.type} (filter: {','.join(station_types)})")

            use_abs_comp = (
                project.abs_begin_pm is not None
                and not np.isnan(project.abs_begin_pm)
                and project.abs_end_pm is not None
                and not np.isnan(project.abs_end_pm)
                and st.abs_pm is not None
                and not np.isnan(st.abs_pm)
            )
            if use_abs_comp:
                min_abs = min(float(project.abs_begin_pm), float(project.abs_end_pm))
                max_abs = max(float(project.abs_begin_pm), float(project.abs_end_pm))
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
        found_sensor_count=len(found),
        project_description=project.project_description
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
    """
    Apply identified corridor stations to master dataframe columns.
    Properly pairs mainline and HOV detectors and cleanly clears unused columns.
    Formats any unformatted Excel serial dates into ISO YYYY-MM-DD.
    """
    updated = df.copy()

    col_sensor_count = _find_column(updated, "Number of Active Non-Ramp Sensors")
    col_s1_id = _find_column(updated, "Sensor Station 1 ID")
    col_s1_hov = _find_column(updated, "Sensor Station 1 HOV ID")
    col_s1_name = _find_column(updated, "Sensor Station 1 Name")
    col_s1_type = _find_column(updated, "Sensor Station 1 Type")
    col_s1_lane_type = _find_column(updated, "Sensor Station 1: lane type")
    col_s1_lanes_after = _find_column(updated, "Sensor Station 1: number of lanes after")

    col_s2_id = _find_column(updated, "Sensor Station 2 ID")
    col_s2_lanes_after = _find_column(updated, "Sensor Station 2: number of lanes after")
    col_s2_lanes_before = _find_column(updated, "Sensor Station 2: number of lanes before")
    col_s2_lane_type = _find_column(updated, "Sensor Station 2: lane type")
    col_s2_width = _find_column(updated, "Sensor Station 2: lane width (ft)")
    col_s2_act = _find_column(updated, "Sensor 2 Activation Date for New Lane")

    col_s3_id = _find_column(updated, "Sensor Station 3 ID")
    col_s3_lanes_after = _find_column(updated, "Sensor Station 3: number of lanes after")
    col_s3_lanes_before = _find_column(updated, "Sensor Station 3: number of lanes before")
    col_s3_lane_type = _find_column(updated, "Sensor Station 3: lane type")
    col_s3_width = _find_column(updated, "Sensor Station 3: lane width (ft)")
    col_s3_act = _find_column(updated, "Sensor 3 Activation Date for New Lane")

    col_s4_id = _find_column(updated, "Sensor Station 4 ID")
    col_s4_lanes_after = _find_column(updated, "Sensor Station 4: number of lanes after")
    col_s4_lanes_before = _find_column(updated, "Sensor Station 4: number of lanes before")
    col_s4_lane_type = _find_column(updated, "Sensor Station 4: lane type")
    col_s4_width = _find_column(updated, "Sensor Station 4: lane width (ft)")
    col_s4_act = _find_column(updated, "Sensor 4 Activation Date for New Lane")

    col_pid = _find_column(updated, "Project ID")
    col_dir = _find_column(updated, "Direction")

    for res in comparison_results:
        # Match row by PID and Direction
        mask = (updated[col_pid].astype(str).str.strip() == str(res.project_id).strip())
        if col_dir and res.direction:
            mask = mask & (updated[col_dir].astype(str).str.strip().str.upper() == res.direction.strip().upper())

        matching_indices = updated[mask].index
        if matching_indices.empty:
            continue

        # Partition found stations into Mainline/General (ML, etc.) and HOV (HV)
        ml_stations = [s for s in res.found_stations if s.type != "HV"]
        hov_stations = [s for s in res.found_stations if s.type == "HV"]
        primary_stations = ml_stations if ml_stations else hov_stations

        for idx in matching_indices:
            if col_sensor_count:
                updated.at[idx, col_sensor_count] = str(len(res.found_stations))

            # Station 1
            if primary_stations:
                st1 = primary_stations[0]
                if col_s1_id: updated.at[idx, col_s1_id] = str(st1.id)
                if col_s1_type: updated.at[idx, col_s1_type] = str(st1.type)
                if col_s1_name: updated.at[idx, col_s1_name] = str(st1.name)
                if col_s1_lanes_after: updated.at[idx, col_s1_lanes_after] = str(st1.lanes) if st1.lanes is not None else ""
            else:
                if col_s1_id: updated.at[idx, col_s1_id] = ""
                if col_s1_type: updated.at[idx, col_s1_type] = ""
                if col_s1_name: updated.at[idx, col_s1_name] = ""
                if col_s1_lanes_after: updated.at[idx, col_s1_lanes_after] = ""

            # Station 1 HOV ID (first HV detector if present, otherwise clear)
            if col_s1_hov:
                updated.at[idx, col_s1_hov] = str(hov_stations[0].id) if hov_stations else ""

            # Station 2
            if len(primary_stations) >= 2:
                st2 = primary_stations[1]
                if col_s2_id: updated.at[idx, col_s2_id] = str(st2.id)
                if col_s2_lanes_after: updated.at[idx, col_s2_lanes_after] = str(st2.lanes) if st2.lanes is not None else ""
            else:
                if col_s2_id: updated.at[idx, col_s2_id] = ""
                if col_s2_lanes_after: updated.at[idx, col_s2_lanes_after] = ""
                if col_s2_lanes_before: updated.at[idx, col_s2_lanes_before] = ""
                if col_s2_lane_type: updated.at[idx, col_s2_lane_type] = ""
                if col_s2_width: updated.at[idx, col_s2_width] = ""
                if col_s2_act: updated.at[idx, col_s2_act] = ""

            # Station 3
            if len(primary_stations) >= 3:
                st3 = primary_stations[2]
                if col_s3_id: updated.at[idx, col_s3_id] = str(st3.id)
                if col_s3_lanes_after: updated.at[idx, col_s3_lanes_after] = str(st3.lanes) if st3.lanes is not None else ""
            else:
                if col_s3_id: updated.at[idx, col_s3_id] = ""
                if col_s3_lanes_after: updated.at[idx, col_s3_lanes_after] = ""
                if col_s3_lanes_before: updated.at[idx, col_s3_lanes_before] = ""
                if col_s3_lane_type: updated.at[idx, col_s3_lane_type] = ""
                if col_s3_width: updated.at[idx, col_s3_width] = ""
                if col_s3_act: updated.at[idx, col_s3_act] = ""

            # Station 4
            if len(primary_stations) >= 4:
                st4 = primary_stations[3]
                if col_s4_id: updated.at[idx, col_s4_id] = str(st4.id)
                if col_s4_lanes_after: updated.at[idx, col_s4_lanes_after] = str(st4.lanes) if st4.lanes is not None else ""
            else:
                if col_s4_id: updated.at[idx, col_s4_id] = ""
                if col_s4_lanes_after: updated.at[idx, col_s4_lanes_after] = ""
                if col_s4_lanes_before: updated.at[idx, col_s4_lanes_before] = ""
                if col_s4_lane_type: updated.at[idx, col_s4_lane_type] = ""
                if col_s4_width: updated.at[idx, col_s4_width] = ""
                if col_s4_act: updated.at[idx, col_s4_act] = ""

    # Convert any raw 5-digit Excel date serials across Sensor Activation Date columns
    for s_idx in range(1, 5):
        col_act = _find_column(updated, f"Sensor {s_idx} Activation Date for New Lane")
        if col_act:
            for idx in updated.index:
                val = str(updated.at[idx, col_act]).strip()
                if val.isdigit() and len(val) == 5:
                    d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(val))
                    updated.at[idx, col_act] = d.strftime("%Y-%m-%d")

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
        if target_projects and target_projects[0].project_description:
            wrapped = textwrap.fill(target_projects[0].project_description, width=76, initial_indent="Project Description: ", subsequent_indent="                     ")
            print(wrapped)
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
                p_match = next((p for p in target_projects if p.project_id == r.project_id), None)
                print(f"\n  Project {r.project_id} (Dist {r.district}, Route {r.route} {r.direction}, PM {r.begin_pm}-{r.end_pm}):")
                if p_match and p_match.project_description:
                    wrapped = textwrap.fill(p_match.project_description, width=74, initial_indent="    Description: ", subsequent_indent="                 ")
                    print(wrapped)
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
                "Project Description": r.project_description,
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
                try:
                    fh.write(df_report.to_markdown(index=False))
                except (ImportError, Exception):
                    cols = df_report.columns.tolist()
                    header = "| " + " | ".join(cols) + " |\n"
                    separator = "| " + " | ".join(["---"] * len(cols)) + " |\n"
                    rows = []
                    for _, row_vals in df_report.iterrows():
                        row_str = "| " + " | ".join(str(row_vals[c]).replace("\n", " ").replace("|", "\\|") for c in cols) + " |\n"
                        rows.append(row_str)
                    fh.write(header + separator + "".join(rows))
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


# ===========================================================================
# OPTION 1: OPEN-TO-TRAFFIC INFERENCE VIA PEMS CONFIGURATION & TELEMETRY
# ===========================================================================


@dataclass
class StationConfigRecord:
    """Historical configuration state of a PeMS detector station."""
    date: datetime.date
    date_str: str
    status: str
    name: str
    lanes_str: str
    lane_count: int
    ca_pm: str
    abs_pm: str
    length: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


@dataclass
class StationConfigChange:
    """A detected configuration event on a PeMS detector station."""
    station_id: str
    change_date: datetime.date
    change_type: str  # 'LANE_CONFIG_INCREMENT', 'DETECTOR_COMMISSIONED', 'STATUS_CHANGE'
    lanes_before: Optional[int]
    lanes_after: Optional[int]
    lanes_str_before: Optional[str]
    lanes_str_after: Optional[str]
    status: str
    name: str
    ca_pm: str
    abs_pm: str
    evidence: str


@dataclass
class FlowActivationResult:
    """Telemetry activation result from PeMS daily flow data."""
    station_id: str
    activation_date: datetime.date
    activation_type: str  # 'LANE_POINTS_STEP_CHANGE', 'SUSTAINED_FLOW_START'
    lane_points_before: int
    lane_points_after: int
    flow_before: float
    flow_after: float
    obs_percent: float
    evidence: str


@dataclass
class OpenToTrafficInference:
    """Inferred Open to Traffic date and corroborating telemetry evidence for a project."""
    project_id: str
    route: str
    direction: str
    district: str
    county: str
    current_open_date: Optional[str]
    inferred_date: Optional[str]  # ISO 'YYYY-MM-DD'
    inferred_year: Optional[int]
    confidence: str  # 'HIGH', 'MEDIUM', 'LOW', 'NONE'
    method: str  # 'LANE_CONFIG_INCREMENT', 'DETECTOR_COMMISSIONED', 'CORROBORATED_TELEMETRY', 'FLOW_TELEMETRY_STEP', 'NONE'
    evidence: str
    station_id: Optional[str] = None
    station_name: Optional[str] = None
    candidate_events: List[Dict[str, Any]] = field(default_factory=list)
    stations_checked: List[str] = field(default_factory=list)


def parse_station_summary_configuration(html_content: str) -> List[StationConfigRecord]:
    """
    Parse historical configuration table from PeMS Station Summary HTML
    (dnode=VDS&content=summary&tab=summary&station_id={st_id}).
    Extracts chronological records of date, status, name, lanes string, lane count, CA PM, Abs PM.
    """
    if not html_content:
        return []
    soup = BeautifulSoup(html_content, "html.parser")
    records: List[StationConfigRecord] = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if any("lanes" in h for h in headers) and any("status" in h for h in headers):
            for tr in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if len(cells) >= 4 and "/" in cells[0]:
                    d_str = cells[0].strip()
                    status = cells[1].strip()
                    name = cells[2].strip() if len(cells) > 2 else ""
                    lanes_str = cells[3].strip()
                    ca_pm = cells[4].strip() if len(cells) > 4 else ""
                    abs_pm = cells[5].strip() if len(cells) > 5 else ""

                    length_val = None
                    if len(cells) > 6:
                        try:
                            length_val = float(cells[6].strip())
                        except ValueError:
                            pass

                    lat_val = None
                    if len(cells) > 7:
                        try:
                            lat_val = float(cells[7].strip())
                        except ValueError:
                            pass

                    lng_val = None
                    if len(cells) > 8:
                        try:
                            lng_val = float(cells[8].strip())
                        except ValueError:
                            pass

                    try:
                        dt = datetime.datetime.strptime(d_str, "%m/%d/%Y").date()
                        digits = re.findall(r"\d", lanes_str)
                        if digits:
                            lane_count = int(lanes_str) if (lanes_str.isdigit() and len(lanes_str) == 1) else len(digits)
                        else:
                            lane_count = 0

                        records.append(StationConfigRecord(
                            date=dt,
                            date_str=dt.strftime("%Y-%m-%d"),
                            status=status,
                            name=name,
                            lanes_str=lanes_str,
                            lane_count=lane_count,
                            ca_pm=ca_pm,
                            abs_pm=abs_pm,
                            length=length_val,
                            lat=lat_val,
                            lng=lng_val,
                        ))
                    except Exception:
                        pass
    records.sort(key=lambda x: x.date)
    return records


def detect_lane_config_step_changes(
    config_records: List[StationConfigRecord],
    station_id: str = "",
    min_date: Optional[datetime.date] = None,
    max_date: Optional[datetime.date] = None
) -> List[StationConfigChange]:
    """
    Analyze chronological station configuration records to identify:
    1. LANE_CONFIG_INCREMENT: lane count increments (e.g. 2 -> 3 lanes, or 3 -> 4 lanes).
    2. DETECTOR_COMMISSIONED: station is first commissioned as 'Active' (for newly built detectors).
    3. STATUS_CHANGE: station changes from Inactive/Disabled to Active.
    Filters within min_date / max_date window if provided.
    """
    events: List[StationConfigChange] = []
    prev_lane_count: Optional[int] = None
    prev_status: Optional[str] = None
    prev_lanes_str: Optional[str] = None
    first_active_seen: bool = False

    for r in config_records:
        dt = r.date
        d_str = r.date_str
        lc = r.lane_count
        ls = r.lanes_str

        # If before min_date, update baseline without emitting event
        if min_date and dt < min_date:
            prev_lane_count = lc
            prev_status = r.status
            prev_lanes_str = ls
            if r.status.lower() == "active":
                first_active_seen = True
            continue

        if max_date and dt > max_date:
            continue

        # 1. Detector Commissioning: First appearance as Active
        if r.status.lower() == "active" and not first_active_seen:
            first_active_seen = True
            events.append(StationConfigChange(
                station_id=station_id,
                change_date=dt,
                change_type="DETECTOR_COMMISSIONED",
                lanes_before=0,
                lanes_after=lc,
                lanes_str_before="",
                lanes_str_after=ls,
                status=r.status,
                name=r.name,
                ca_pm=r.ca_pm,
                abs_pm=r.abs_pm,
                evidence=f"Detector station {station_id} commissioned as Active on {d_str} with {lc} lanes (mask: '{ls}')",
            ))

        # 2. Lane Configuration Increment: lane count increases while active
        elif prev_lane_count is not None and lc > prev_lane_count and r.status.lower() == "active":
            events.append(StationConfigChange(
                station_id=station_id,
                change_date=dt,
                change_type="LANE_CONFIG_INCREMENT",
                lanes_before=prev_lane_count,
                lanes_after=lc,
                lanes_str_before=prev_lanes_str,
                lanes_str_after=ls,
                status=r.status,
                name=r.name,
                ca_pm=r.ca_pm,
                abs_pm=r.abs_pm,
                evidence=f"Station {station_id} lane configuration incremented from {prev_lane_count} to {lc} lanes on {d_str} (mask: '{ls}')",
            ))

        # 3. Status change to Active after being Disabled/Inactive
        elif prev_status and prev_status.lower() in ("disabled", "inactive") and r.status.lower() == "active":
            events.append(StationConfigChange(
                station_id=station_id,
                change_date=dt,
                change_type="STATUS_CHANGE",
                lanes_before=prev_lane_count,
                lanes_after=lc,
                lanes_str_before=prev_lanes_str,
                lanes_str_after=ls,
                status=r.status,
                name=r.name,
                ca_pm=r.ca_pm,
                abs_pm=r.abs_pm,
                evidence=f"Station {station_id} transitioned from {prev_status} to Active on {d_str} with {lc} lanes",
            ))

        prev_lane_count = lc
        prev_status = r.status
        prev_lanes_str = ls

    return events


def parse_daily_flow_telemetry(tsv_text: str) -> List[Dict[str, Any]]:
    """
    Parse PeMS daily flow telemetry export text.
    Format: Day \\t Flow (Veh/Day) \\t # Lane Points \\t % Observed
    Returns list of dicts:
      {'date': dt, 'date_str': 'YYYY-MM-DD', 'flow': float, 'lane_points': int, 'obs_pct': float}
    """
    records: List[Dict[str, Any]] = []
    if not tsv_text:
        return records
    lines = tsv_text.strip().splitlines()
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 4 and "/" in parts[0]:
            d_str = parts[0].strip()
            try:
                dt = datetime.datetime.strptime(d_str, "%m/%d/%Y").date()
                flow = float(parts[1].replace(",", "").strip())
                lp = int(float(parts[2].replace(",", "").strip()))
                obs = float(parts[3].replace(",", "").replace("%", "").strip())
                records.append({
                    "date": dt,
                    "date_str": dt.strftime("%Y-%m-%d"),
                    "flow": flow,
                    "lane_points": lp,
                    "obs_pct": obs,
                })
            except (ValueError, TypeError):
                continue
    records.sort(key=lambda x: x["date"])
    return records


def detect_flow_telemetry_activation(
    client: PeMSClient,
    station_id: str,
    target_year: int,
    candidate_date: Optional[datetime.date] = None,
    min_date: Optional[datetime.date] = None,
    max_date: Optional[datetime.date] = None,
    flow_threshold: float = 500.0,
    consecutive_days: int = 3
) -> Optional[FlowActivationResult]:
    """
    Examine PeMS daily flow telemetry (tab=det_timeseries&q=flow&gn=day)
    to corroborate or pinpoint when traffic commenced on new lanes or detectors:
    1. LANE_POINTS_STEP_CHANGE: # Lane Points permanently jumps (e.g. 576 -> 864 = 2 to 3 lanes).
    2. SUSTAINED_FLOW_START: Daily flow transitions from 0/unobserved to steady mainline traffic (>500 veh/day)
       for >= consecutive_days with positive % Observed.
    """
    url = build_station_timeseries_url(station_id, start_year=target_year, end_year=target_year, quantity="flow", granularity="day")
    try:
        tsv_text = client.get_text_content(url)
    except Exception as exc:
        LOGGER.debug(f"Failed to fetch flow telemetry for station {station_id} in {target_year}: {exc}")
        return None

    records = parse_daily_flow_telemetry(tsv_text)
    if not records:
        return None

    # Check 1: Lane Points step-change (288 intervals/day per lane)
    for i in range(1, len(records)):
        curr = records[i]
        prev = records[i - 1]

        if candidate_date is not None:
            delta = abs((curr["date"] - candidate_date).days)
            if delta > 14:
                continue

        # A jump of >= 200 lane points indicates an additional lane added to daily sampling
        if curr["lane_points"] >= prev["lane_points"] + 200:
            subsequent = records[i:i + 5]
            if len(subsequent) >= 3:
                median_sub = np.median([r["lane_points"] for r in subsequent])
                if median_sub >= prev["lane_points"] + 200:
                    lp_prev = prev["lane_points"]
                    lp_curr = curr["lane_points"]
                    lanes_b = round(lp_prev / 288.0)
                    lanes_a = round(lp_curr / 288.0)
                    ev_msg = (
                        f"Daily detector telemetry at station {station_id} showed # Lane Points stepping from "
                        f"{lp_prev} (~{lanes_b} lanes) to {lp_curr} (~{lanes_a} lanes) on {curr['date_str']} "
                        f"(flow: {curr['flow']:,.0f} veh/day, {curr['obs_pct']:.1f}% observed)"
                    )
                    return FlowActivationResult(
                        station_id=station_id,
                        activation_date=curr["date"],
                        activation_type="LANE_POINTS_STEP_CHANGE",
                        lane_points_before=lp_prev,
                        lane_points_after=lp_curr,
                        flow_before=prev["flow"],
                        flow_after=curr["flow"],
                        obs_percent=curr["obs_pct"],
                        evidence=ev_msg,
                    )

    # Check 2: Sustained flow start (new detector commissioning)
    for i in range(len(records) - consecutive_days + 1):
        window = records[i:i + consecutive_days]
        first = window[0]
        dt = first["date"]
        if min_date and dt < min_date:
            continue
        if max_date and dt > max_date:
            continue

        if all(r["flow"] >= flow_threshold and r["obs_pct"] > 0 for r in window):
            prior = records[max(0, i - 7):i]
            if not prior or any(r["flow"] < flow_threshold or r["obs_pct"] == 0 for r in prior):
                ev_msg = (
                    f"Detector {station_id} recorded initial sustained mainline flow starting {first['date_str']} "
                    f"({first['flow']:,.0f} veh/day, {first['obs_pct']:.1f}% observed, {first['lane_points']} lane points)"
                )
                return FlowActivationResult(
                    station_id=station_id,
                    activation_date=dt,
                    activation_type="SUSTAINED_FLOW_START",
                    lane_points_before=0,
                    lane_points_after=first["lane_points"],
                    flow_before=0.0,
                    flow_after=first["flow"],
                    obs_percent=first["obs_pct"],
                    evidence=ev_msg,
                )

    return None


def infer_project_open_to_traffic(
    project_or_id: Any,
    client: Optional[PeMSClient] = None,
    master_csv_path: Optional[str] = None,
    vds_meta: Optional[IndexedVDSMetadata] = None,
    check_telemetry: bool = True,
    buffer_mi: float = 0.1,
    verbose: bool = True
) -> OpenToTrafficInference:
    """
    Infer the exact Open to Traffic Date for a highway project using PeMS
    Option 1: Lane Configuration Step-Change & Detector Telemetry Activation.

    Parameters:
      project_or_id: ProjectRecord or PID string.
      client: Optional authenticated PeMSClient instance.
      master_csv_path: Path to master CSV if looking up by PID string.
      vds_meta: Preloaded IndexedVDSMetadata instance for corridor lookups.
      check_telemetry: If True, queries daily flow telemetry to corroborate config step-changes.
      buffer_mi: Postmile buffer in miles for corridor station discovery.
      verbose: If True, logs progress.

    Returns:
      OpenToTrafficInference object containing inferred date, year, confidence, method, and evidence.
    """
    # 1. Resolve project record
    if isinstance(project_or_id, ProjectRecord):
        proj = project_or_id
    else:
        pid_str = str(project_or_id).strip()
        all_projs = load_master_csv(master_csv_path)
        matching = [p for p in all_projs if str(p.project_id).strip() == pid_str]
        if not matching:
            return OpenToTrafficInference(
                project_id=pid_str,
                route="",
                direction="",
                district="",
                county="",
                current_open_date=None,
                inferred_date=None,
                inferred_year=None,
                confidence="NONE",
                method="NONE",
                evidence=f"Project ID '{pid_str}' not found in master project CSV.",
            )
        proj = matching[0]

    # 2. Determine candidate detector stations
    candidate_stations: List[str] = []
    # Primary: Spreadsheet sensors
    for sid in proj.sensor_stations:
        s_clean = str(sid).strip()
        if s_clean and s_clean not in candidate_stations:
            candidate_stations.append(s_clean)

    # Secondary: Corridor sensors from VDS metadata if spreadsheet has no sensors
    if not candidate_stations:
        meta = vds_meta or get_indexed_vds_metadata()
        if meta:
            try:
                corr_recs = meta.find_corridor_stations(
                    district=proj.dist,
                    route=proj.route,
                    direction=proj.pems_direction,
                    begin_pm=proj.begin_pm,
                    end_pm=proj.end_pm,
                    abs_begin_pm=proj.abs_begin_pm,
                    abs_end_pm=proj.abs_end_pm,
                    county=proj.county,
                    buffer_mi=buffer_mi,
                    station_types=("ML", "HV"),
                )
                for cr in corr_recs[:6]:  # Prioritize up to 6 corridor stations
                    if str(cr.id) not in candidate_stations:
                        candidate_stations.append(str(cr.id))
            except Exception as exc:
                LOGGER.debug(f"Error querying corridor stations for PID {proj.project_id}: {exc}")

    if not candidate_stations:
        return OpenToTrafficInference(
            project_id=proj.project_id,
            route=proj.route,
            direction=proj.direction,
            district=proj.dist,
            county=proj.county,
            current_open_date=proj.open_to_traffic_date,
            inferred_date=None,
            inferred_year=None,
            confidence="NONE",
            method="NONE",
            evidence=f"No PeMS detector stations found on Route {proj.route} {proj.direction} ({proj.county}) corridor (uninstrumented corridor).",
            stations_checked=[],
        )

    # 3. Establish project construction timeframe bounds
    c_begin_parsed = (
        build_date_from_parts(
            proj.raw_row.get("construction_begin_year"),
            proj.raw_row.get("construction_begin_month"),
            proj.raw_row.get("construction_begin_day"),
            default_month=1, default_day=1
        )
        or parse_iso_date(proj.construction_begin_date, 1, 1)
    )
    min_date = c_begin_parsed - datetime.timedelta(days=60) if c_begin_parsed else datetime.date(2000, 1, 1)
    max_date = datetime.date.today() + datetime.timedelta(days=180)

    # 4. Authenticate client
    pems_client = client or PeMSClient()
    if not pems_client.logged_in:
        pems_client.login()

    # 5. Evaluate stations for configuration changes and telemetry
    all_events: List[Dict[str, Any]] = []
    stations_checked: List[str] = []

    for sid in candidate_stations:
        stations_checked.append(sid)
        summary_url = build_station_summary_url(sid)
        try:
            html_txt = pems_client.get_text_content(summary_url)
        except Exception as exc:
            LOGGER.debug(f"Failed to fetch summary HTML for station {sid}: {exc}")
            continue

        configs = parse_station_summary_configuration(html_txt)
        if not configs:
            continue

        step_changes = detect_lane_config_step_changes(configs, station_id=sid, min_date=min_date, max_date=max_date)
        for sc in step_changes:
            all_events.append({
                "station_id": sid,
                "station_name": sc.name,
                "event_type": sc.change_type,
                "date": sc.change_date,
                "date_str": sc.change_date.strftime("%Y-%m-%d"),
                "lanes_before": sc.lanes_before,
                "lanes_after": sc.lanes_after,
                "lanes_str_before": sc.lanes_str_before,
                "lanes_str_after": sc.lanes_str_after,
                "evidence": sc.evidence,
                "telemetry": None,
            })

    if not all_events:
        return OpenToTrafficInference(
            project_id=proj.project_id,
            route=proj.route,
            direction=proj.direction,
            district=proj.dist,
            county=proj.county,
            current_open_date=proj.open_to_traffic_date,
            inferred_date=None,
            inferred_year=None,
            confidence="NONE",
            method="NONE",
            evidence=f"Checked {len(stations_checked)} corridor stations ({', '.join(stations_checked[:5])}); no configuration changes or detector activation events identified.",
            stations_checked=stations_checked,
        )

    # 6. Corroborate candidate events with daily flow telemetry if requested
    if check_telemetry:
        for ev in all_events:
            sid = ev["station_id"]
            cand_dt = ev["date"]
            tel_res = detect_flow_telemetry_activation(
                client=pems_client,
                station_id=sid,
                target_year=cand_dt.year,
                candidate_date=cand_dt,
                min_date=min_date,
                max_date=max_date,
            )
            if tel_res is not None:
                ev["telemetry"] = tel_res

    # 7. Select best candidate event & assign confidence
    best_event = None
    best_score = -1

    # Check for date consensus across multiple stations
    date_counts: Dict[datetime.date, int] = {}
    for ev in all_events:
        d = ev["date"]
        date_counts[d] = date_counts.get(d, 0) + 1

    for ev in all_events:
        score = 0
        ev_type = ev["event_type"]
        has_tel = ev.get("telemetry") is not None
        consensus = date_counts.get(ev["date"], 1)

        if ev_type == "LANE_CONFIG_INCREMENT":
            score = 100
            if has_tel:
                score += 50
        elif ev_type == "DETECTOR_COMMISSIONED":
            score = 70
            if has_tel:
                score += 40
        elif ev_type == "STATUS_CHANGE":
            score = 40

        if consensus > 1:
            score += 25 * (consensus - 1)

        # Proximity to construction window
        if c_begin_parsed:
            days_after_const = (ev["date"] - c_begin_parsed).days
            if 60 <= days_after_const <= 1825:  # 2 months to 5 years after const begin
                score += 30
            elif days_after_const < 0:
                score -= 40

        if score > best_score:
            best_score = score
            best_event = ev

    if not best_event:
        return OpenToTrafficInference(
            project_id=proj.project_id,
            route=proj.route,
            direction=proj.direction,
            district=proj.dist,
            county=proj.county,
            current_open_date=proj.open_to_traffic_date,
            inferred_date=None,
            inferred_year=None,
            confidence="NONE",
            method="NONE",
            evidence="Unable to score candidate events.",
            candidate_events=all_events,
            stations_checked=stations_checked,
        )

    # Determine confidence level
    conf = "LOW"
    method = best_event["event_type"]
    if best_score >= 120:
        conf = "HIGH"
        if best_event.get("telemetry"):
            method = "CORROBORATED_TELEMETRY"
    elif best_score >= 70:
        conf = "MEDIUM"

    # Assemble comprehensive evidence narrative
    narrative_parts = [best_event["evidence"]]
    if best_event.get("telemetry"):
        narrative_parts.append(best_event["telemetry"].evidence)
    if date_counts.get(best_event["date"], 1) > 1:
        narrative_parts.append(f"Corroborated across {date_counts[best_event['date']]} distinct stations along the corridor on {best_event['date_str']}.")

    full_evidence = " | ".join(narrative_parts)

    return OpenToTrafficInference(
        project_id=proj.project_id,
        route=proj.route,
        direction=proj.direction,
        district=proj.dist,
        county=proj.county,
        current_open_date=proj.open_to_traffic_date,
        inferred_date=best_event["date_str"],
        inferred_year=best_event["date"].year,
        confidence=conf,
        method=method,
        evidence=full_evidence,
        station_id=best_event["station_id"],
        station_name=best_event.get("station_name"),
        candidate_events=all_events,
        stations_checked=stations_checked,
    )


def apply_open_date_updates_to_dataframe(
    df: pd.DataFrame,
    inferences: List[OpenToTrafficInference],
    allowed_confidences: Tuple[str, ...] = ("HIGH", "MEDIUM")
) -> Tuple[pd.DataFrame, int]:
    """
    Apply inferred open-to-traffic dates to master dataframe columns:
      - 'Open to Traffic Date'
      - 'Open to Traffic Date (formatted YYYY-MM-DD)'
      - 'open_traffic_year'
      - 'open_traffic_month'
      - 'open_traffic_day'
      - 'Sensor 1 Activation Date for New Lane' (if blank or matching sensor 1)
    Returns updated dataframe and count of updated rows.
    """
    updated = df.copy()
    col_pid = _find_column(updated, "Project ID")
    col_dir = _find_column(updated, "Direction")
    col_open = _find_column(updated, "Open to Traffic Date")
    col_open_fmt = _find_column(updated, "Open to Traffic Date (formatted YYYY-MM-DD)")
    col_yr = _find_column(updated, "open_traffic_year")
    col_mo = _find_column(updated, "open_traffic_month")
    col_day = _find_column(updated, "open_traffic_day")
    col_s1_act = _find_column(updated, "Sensor 1 Activation Date for New Lane")
    col_s1_id = _find_column(updated, "Sensor Station 1 ID")

    allowed_set = {c.upper().strip() for c in allowed_confidences}
    update_count = 0

    # Ensure target columns allow string/object types to avoid pandas dtype downcasting warnings
    for c in (col_open, col_open_fmt, col_yr, col_mo, col_day, col_s1_act):
        if c and c in updated.columns:
            updated[c] = updated[c].astype(object)

    for inf in inferences:
        if not inf.inferred_date or inf.confidence.upper() not in allowed_set:
            continue

        mask = (updated[col_pid].astype(str).str.strip() == str(inf.project_id).strip())
        if col_dir and inf.direction:
            mask = mask & (updated[col_dir].astype(str).str.strip().str.upper() == inf.direction.strip().upper())

        matching_indices = updated[mask].index
        if matching_indices.empty:
            continue

        try:
            dt = datetime.datetime.strptime(inf.inferred_date, "%Y-%m-%d").date()
        except ValueError:
            continue

        for idx in matching_indices:
            update_count += 1
            if col_open:
                updated.at[idx, col_open] = inf.inferred_date
            if col_open_fmt:
                updated.at[idx, col_open_fmt] = inf.inferred_date
            if col_yr:
                updated.at[idx, col_yr] = str(dt.year)
            if col_mo:
                updated.at[idx, col_mo] = str(dt.month)
            if col_day:
                updated.at[idx, col_day] = str(dt.day)

            # Also update Sensor 1 activation date if empty or if station matches
            if col_s1_act:
                curr_act = str(updated.at[idx, col_s1_act]).strip()
                s1_id_val = str(updated.at[idx, col_s1_id]).strip() if col_s1_id else ""
                if not curr_act or curr_act.lower() in ("nan", "none", "") or (inf.station_id and inf.station_id in s1_id_val):
                    updated.at[idx, col_s1_act] = inf.inferred_date

    return updated, update_count


def apply_open_date_to_master_csv(
    inferences: List[OpenToTrafficInference],
    master_csv_path: Optional[str] = None,
    output_csv_path: Optional[str] = None,
    allowed_confidences: Tuple[str, ...] = ("HIGH", "MEDIUM")
) -> Tuple[str, str, int]:
    """
    Commit inferred open dates to the master CSV file.
    Creates an automated timestamped backup (.bak_YYYYMMDD_HHMMSS) before writing.
    Returns (backup_path, target_out_path, updated_rows_count).
    """
    target_in = master_csv_path or DEFAULT_MASTER_CSV
    target_out = output_csv_path or target_in

    raw_df = pd.read_csv(target_in, low_memory=False)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{target_in}.bak_{timestamp}"
    shutil.copyfile(target_in, backup_path)
    LOGGER.info(f"Created safety backup of input master CSV at: {backup_path}")

    updated_df, count = apply_open_date_updates_to_dataframe(
        raw_df, inferences, allowed_confidences=allowed_confidences
    )
    updated_df.to_csv(target_out, index=False)
    return backup_path, target_out, count


def run_open_to_traffic_inference_pipeline(
    projects: Optional[List[ProjectRecord]] = None,
    master_csv_path: Optional[str] = None,
    project_id_filter: Optional[str] = None,
    dist_filter: Optional[str] = None,
    check_telemetry: bool = True,
    apply_updates: bool = False,
    output_report_path: Optional[str] = None,
    output_csv_path: Optional[str] = None,
    client: Optional[PeMSClient] = None,
    verbose: bool = True
) -> List[OpenToTrafficInference]:
    """
    Execute end-to-end Open-to-Traffic Date inference pipeline across highway projects.
    Uses PeMS Lane Configuration & Detector Telemetry (Option 1).
    """
    csv_file = master_csv_path or DEFAULT_MASTER_CSV
    all_projects = projects if projects is not None else load_master_csv(csv_file)

    target_projects = all_projects
    if project_id_filter:
        pid_clean = str(project_id_filter).strip()
        target_projects = [p for p in target_projects if str(p.project_id).strip() == pid_clean]
    elif dist_filter:
        d_clean = str(dist_filter).strip()
        target_projects = [p for p in target_projects if str(p.dist).strip() == d_clean]
    else:
        # Default: evaluate projects missing an Open to Traffic Date, or where it is year-only
        missing_or_vague = []
        for p in target_projects:
            o_date = str(p.open_to_traffic_date).strip() if p.open_to_traffic_date else ""
            if not o_date or o_date.lower() in ("nan", "none", "") or (len(o_date) <= 4 and o_date.isdigit()):
                missing_or_vague.append(p)
        if missing_or_vague:
            target_projects = missing_or_vague

    if not target_projects:
        print("No projects matched the specified criteria for Open-to-Traffic date inference.")
        return []

    pems_client = client or PeMSClient()
    if not pems_client.logged_in:
        pems_client.login()

    vds_meta = get_indexed_vds_metadata()
    inferences: List[OpenToTrafficInference] = []

    mode_label = "APPLY UPDATES (Active Write)" if apply_updates else "DRY RUN (Read-Only) [Pass --apply to commit updates]"
    print("=" * 80)
    print("PEMS OPEN-TO-TRAFFIC DATE INFERENCE PIPELINE (OPTION 1)")
    print(f"Mode: {mode_label}")
    print(f"Telemetry Corroboration: {'Enabled (Daily Flow)' if check_telemetry else 'Disabled'}")
    print(f"Target Projects: {len(target_projects)}")
    print("=" * 80)

    for i, p in enumerate(target_projects):
        if verbose:
            print(f"[{i+1}/{len(target_projects)}] Evaluating Project {p.project_id} (Route {p.route} {p.direction})...", end="", flush=True)
        inf = infer_project_open_to_traffic(
            p,
            client=pems_client,
            master_csv_path=csv_file,
            vds_meta=vds_meta,
            check_telemetry=check_telemetry,
            verbose=False
        )
        inferences.append(inf)
        if verbose:
            status_text = f" -> Inferred: {inf.inferred_date} ({inf.confidence})" if inf.inferred_date else f" -> {inf.confidence}"
            print(status_text)

    # Print summary table
    print("\n" + "=" * 80)
    print("INFERENCE RESULTS SUMMARY")
    print("=" * 80)
    print(f"{'PID':<11} {'Route':<7} {'Dir':<4} {'Sheet Open':<12} {'Inferred Open':<14} {'Conf':<8} {'Method':<22} {'Station':<8}")
    print("-" * 80)
    for inf in inferences:
        sheet_d = str(inf.current_open_date or 'None')[:10]
        inf_d = str(inf.inferred_date or 'None')
        st_id = str(inf.station_id or '-')
        print(f"{inf.project_id:<11} {inf.route:<7} {inf.direction:<4} {sheet_d:<12} {inf_d:<14} {inf.confidence:<8} {inf.method:<22} {st_id:<8}")
    print("=" * 80)

    # Detailed evidence breakdown for resolved projects
    resolved = [inf for inf in inferences if inf.inferred_date]
    if resolved:
        print("\nDETAILED EVIDENCE LOG:")
        for inf in resolved:
            print(f"  • Project {inf.project_id} (Route {inf.route} {inf.direction}):")
            print(f"    Inferred Open Date: {inf.inferred_date} [Confidence: {inf.confidence}]")
            print(f"    Primary Station:    {inf.station_id} ({inf.station_name or 'VDS'})")
            print(f"    Evidence:           {inf.evidence}")

    # Output report file if requested
    if output_report_path:
        out_ext = os.path.splitext(output_report_path)[1].lower()
        if out_ext == ".csv":
            report_data = []
            for inf in inferences:
                report_data.append({
                    "project_id": inf.project_id,
                    "route": inf.route,
                    "direction": inf.direction,
                    "district": inf.district,
                    "county": inf.county,
                    "sheet_open_date": inf.current_open_date,
                    "inferred_open_date": inf.inferred_date,
                    "inferred_year": inf.inferred_year,
                    "confidence": inf.confidence,
                    "method": inf.method,
                    "station_id": inf.station_id,
                    "evidence": inf.evidence,
                })
            pd.DataFrame(report_data).to_csv(output_report_path, index=False)
            print(f"\n✓ Exported inference report CSV -> {output_report_path}")
        else:
            md_lines = [
                "# PeMS Open-to-Traffic Date Inference Report",
                f"**Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
                f"**Method:** PeMS Lane Configuration Step-Change & Detector Telemetry (Option 1)  ",
                f"**Telemetry Verification:** {'Enabled' if check_telemetry else 'Disabled'}  \n",
                "| Project ID | Route | Dir | Sheet Date | Inferred Date | Confidence | Method | Station | Evidence |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
            ]
            for inf in inferences:
                md_lines.append(
                    f"| {inf.project_id} | {inf.route} | {inf.direction} | {inf.current_open_date or 'N/A'} | "
                    f"**{inf.inferred_date or 'N/A'}** | {inf.confidence} | {inf.method} | {inf.station_id or 'N/A'} | {inf.evidence} |"
                )
            with open(output_report_path, "w", encoding="utf-8") as f:
                f.write("\n".join(md_lines))
            print(f"\n✓ Exported inference report Markdown -> {output_report_path}")

    # Commit updates if requested
    if apply_updates:
        backup_p, target_p, count = apply_open_date_to_master_csv(
            inferences=inferences,
            master_csv_path=csv_file,
            output_csv_path=output_csv_path,
        )
        print(f"\n✓ Successfully updated {count} project dates in master CSV at: {target_p}")
        print(f"  Safety backup preserved at: {backup_p}")
    else:
        print("\n[DRY RUN COMPLETE]: No changes have been written to the spreadsheet.")
        print("To commit these inferred dates to the master CSV, run with: --find-open-date --apply")

    return inferences



# ---------------------------------------------------------------------------
# RAW PEMS STATION LANE CONFIGURATION & TSN ROADWAY METRICS ENGINE
# ---------------------------------------------------------------------------
@dataclass
class StationLaneConfigDetails:
    """Raw PeMS station lane configuration, change log, and Caltrans TSN roadway metrics."""
    station_id: str
    station_name: str = ""
    district: Optional[str] = None
    route: Optional[str] = None
    direction: Optional[str] = None
    county: Optional[str] = None
    station_type: Optional[str] = None
    ca_pm: Optional[str] = None
    abs_pm: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    tsn_roadway_info: Dict[str, str] = field(default_factory=dict)
    lane_table: List[Dict[str, str]] = field(default_factory=list)
    change_log: List[Dict[str, str]] = field(default_factory=list)
    construction_history: List[Dict[str, str]] = field(default_factory=list)
    url: str = ""
    error: Optional[str] = None


def parse_station_configuration_page(
    html_content: str,
    station_id: str = "",
    vds_record: Optional[VDSStationRecord] = None
) -> StationLaneConfigDetails:
    """
    Parse raw PeMS station configuration HTML (content=sta_cfg).
    Extracts:
      1. Caltrans TSN Roadway Information key-value dictionary (Road Width, Lane Width,
         Inner/Outer Shoulder Width, Design Speed Limit, Functional Class, Median Type, etc.)
      2. PeMS Lane Configuration Table (Lane, Slot, Sensor Tech, Type)
      3. PeMS Change Log / Configuration history (Date, Status, Name, Lanes, CA PM, Abs PM, Length, etc.)
      4. PeMS Construction History (Start Date, End Date)
    """
    st_id = str(station_id).strip()
    details = StationLaneConfigDetails(
        station_id=st_id,
        url=build_station_config_url(st_id) if st_id else "",
    )

    if vds_record:
        details.station_name = vds_record.name
        details.district = str(vds_record.district)
        details.route = str(vds_record.fwy)
        details.direction = vds_record.dir
        details.county = str(vds_record.county)
        details.station_type = vds_record.type
        details.ca_pm = str(vds_record.state_pm)
        details.abs_pm = vds_record.abs_pm
        details.latitude = vds_record.latitude
        details.longitude = vds_record.longitude

    if not html_content:
        details.error = "PeMS server timed out or returned no content"
        return details

    soup = BeautifulSoup(html_content, "html.parser")
    page_text = soup.get_text()

    if "Application Halted" in page_text or "Missing numeric segment identifier" in page_text:
        details.error = "PeMS internal error: station uninstrumented or missing numeric segment identifier"
        return details

    # Extract title metadata if vds_record wasn't present
    if soup.title and soup.title.string:
        t_str = soup.title.string.strip()
        m_title = re.search(r">\s*(.*?VDS)\s*(\d+)\s*-\s*(.*?)\s*>", t_str)
        if m_title:
            if not details.station_type:
                details.station_type = m_title.group(1).strip()
            if not details.station_name:
                details.station_name = m_title.group(3).strip()

    for table in soup.find_all("table"):
        full_text = table.get_text(separator=" ", strip=True)
        th_cells_lower = [th.get_text(strip=True).lower() for th in table.find_all("th")]

        # 1. TSN Roadway Information
        if "roadway information" in full_text.lower():
            tsn_parsed_count = 0
            for tr in table.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) == 2 and cells[0]:
                    k, v = cells[0].strip(), cells[1].strip()
                    if "roadway information" not in k.lower():
                        details.tsn_roadway_info[k] = v
                        tsn_parsed_count += 1
                elif len(cells) == 1 and "not available" in cells[0].lower():
                    details.tsn_roadway_info["Status"] = "Not Available in PeMS"
            if tsn_parsed_count == 0 and "Status" not in details.tsn_roadway_info:
                if "not available" in full_text.lower():
                    details.tsn_roadway_info["Status"] = "Not Available in PeMS"

        # 2. Lane Configuration Table
        elif (
            any(h == "lane" for h in th_cells_lower)
            and not any(h in ("status", "date", "start date", "lanes") for h in th_cells_lower)
        ):
            col_names: List[str] = []
            for tr in table.find_all("tr"):
                th_cells = [th.get_text(strip=True) for th in tr.find_all("th")]
                if any(h.lower() == "lane" for h in th_cells):
                    col_names = th_cells
                    continue
                if col_names:
                    td_cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                    if len(td_cells) == len(col_names):
                        details.lane_table.append(dict(zip(col_names, td_cells)))

        # 3. Change Log
        elif any("status" in h for h in th_cells_lower) and any("lanes" in h for h in th_cells_lower):
            col_names: List[str] = []
            for tr in table.find_all("tr"):
                th_cells = [th.get_text(strip=True) for th in tr.find_all("th")]
                if any("status" in h.lower() for h in th_cells) and any("lanes" in h.lower() for h in th_cells):
                    col_names = th_cells
                    continue
                if col_names:
                    td_cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                    if len(td_cells) == len(col_names):
                        details.change_log.append(dict(zip(col_names, td_cells)))

        # 4. Construction History
        elif any("start date" in h for h in th_cells_lower) and any("end date" in h for h in th_cells_lower):
            col_names: List[str] = []
            for tr in table.find_all("tr"):
                th_cells = [th.get_text(strip=True) for th in tr.find_all("th")]
                if any("start date" in h.lower() for h in th_cells):
                    col_names = th_cells
                    continue
                if col_names:
                    td_cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                    if len(td_cells) == len(col_names):
                        details.construction_history.append(dict(zip(col_names, td_cells)))

    return details


def fetch_station_lane_configuration(
    station_id: str,
    client: Optional[PeMSClient] = None,
    vds_record: Optional[VDSStationRecord] = None,
    timeout: float = 60.0
) -> StationLaneConfigDetails:
    """
    Fetch and parse raw PeMS station lane configuration, change log, and TSN roadway metrics for a single station.
    """
    st_id = str(station_id).strip()
    if client is None:
        client = PeMSClient()
        client.login()

    url = build_station_config_url(st_id)
    try:
        html_txt = client.get_text_content(url, timeout=timeout)
        if not html_txt:
            # Fallback to direct tab URL without report_form=1 if primary attempt timed out or was empty
            tab_url = f"{BASE_URL}/?dnode=VDS&content=sta_cfg&tab=sta_cfg&station_id={st_id}"
            if tab_url != url:
                LOGGER.info(f"Retrying station {st_id} configuration with browser tab URL ({tab_url})...")
                html_txt = client.get_text_content(tab_url, timeout=timeout)
                if html_txt:
                    url = tab_url

        if not html_txt:
            details = StationLaneConfigDetails(
                station_id=st_id,
                url=url,
                error=f"PeMS server timed out or returned no content (backend query took > {timeout:.0f}s)"
            )
            if vds_record:
                details.station_name = vds_record.name
                details.district = str(vds_record.district)
                details.route = str(vds_record.fwy)
                details.direction = vds_record.dir
                details.county = str(vds_record.county)
                details.station_type = vds_record.type
                details.ca_pm = str(vds_record.state_pm)
                details.abs_pm = vds_record.abs_pm
                details.latitude = vds_record.latitude
                details.longitude = vds_record.longitude
            return details

        return parse_station_configuration_page(html_txt, station_id=st_id, vds_record=vds_record)
    except Exception as exc:
        LOGGER.warning(f"Failed to fetch configuration for station {st_id}: {exc}")
        details = StationLaneConfigDetails(
            station_id=st_id,
            url=url,
            error=str(exc)
        )
        if vds_record:
            details.station_name = vds_record.name
            details.district = str(vds_record.district)
            details.route = str(vds_record.fwy)
            details.direction = vds_record.dir
            details.county = str(vds_record.county)
            details.station_type = vds_record.type
            details.ca_pm = str(vds_record.state_pm)
            details.abs_pm = vds_record.abs_pm
            details.latitude = vds_record.latitude
            details.longitude = vds_record.longitude
        return details


def fetch_project_lane_configurations(
    project_id: str,
    master_csv_path: Optional[str] = None,
    client: Optional[PeMSClient] = None,
    metadata_dir: Optional[str] = None,
    buffer_mi: float = 0.2,
    include_corridor_vds: bool = True
) -> Dict[str, Any]:
    """
    Fetch raw PeMS station lane configuration tables and TSN roadway metrics for all stations associated with a project.
    Combines stations listed in the spreadsheet with active corridor VDS stations along the project corridor.
    """
    csv_file = master_csv_path or DEFAULT_MASTER_CSV
    projects = load_master_csv(csv_file)
    pid_clean = str(project_id).strip()
    matched_projects = [p for p in projects if str(p.project_id).strip() == pid_clean]

    if not matched_projects:
        return {
            "project_id": pid_clean,
            "projects": [],
            "stations": [],
            "spreadsheet_stations": [],
            "corridor_stations": [],
            "error": f"Project ID '{pid_clean}' not found in {csv_file}"
        }

    if client is None:
        client = PeMSClient()
        client.login()

    vds_meta: Optional[IndexedVDSMetadata] = None
    try:
        vds_meta = load_vds_metadata(metadata_dir)
    except Exception as exc:
        LOGGER.debug(f"Could not load VDS metadata: {exc}")

    # Gather spreadsheet sensor IDs
    spreadsheet_sids: List[str] = []
    for p in matched_projects:
        for sid in p.sensor_stations:
            s_clean = str(sid).strip()
            if s_clean and s_clean not in spreadsheet_sids:
                spreadsheet_sids.append(s_clean)

    # Gather corridor active VDS stations
    corridor_sids: List[str] = []
    if include_corridor_vds and vds_meta is not None:
        for p in matched_projects:
            comp = compare_project_stations(p, vds_meta=vds_meta, buffer_mi=buffer_mi, station_types=("ML", "HV"))
            for s in comp.found_stations:
                s_clean = str(s.id).strip()
                if s_clean and s_clean not in corridor_sids and s_clean not in spreadsheet_sids:
                    corridor_sids.append(s_clean)

    # Combined station list: spreadsheet stations first, followed by additional corridor stations
    ordered_sids = spreadsheet_sids + [s for s in corridor_sids if s not in spreadsheet_sids]

    results: List[StationLaneConfigDetails] = []
    for sid in ordered_sids:
        vds_rec = vds_meta.by_id.get(sid) if vds_meta else None
        st_details = fetch_station_lane_configuration(sid, client=client, vds_record=vds_rec)
        results.append(st_details)

    # HPMS Ground-Truth Roadway Attributes
    hpms_summary: Optional[Dict[str, Any]] = None
    try:
        from hpms_interface import HPMSCorridorEngine
        engine = HPMSCorridorEngine(master_csv_path=csv_file)
        hpms_res = engine.inspect_project(pid_clean)
        if hpms_res and hpms_res.get("sections"):
            secs = hpms_res["sections"]
            facility_code_map = {
                "1": "One-Way Roadway",
                "2": "Two-Way Divided (Freeway/Expressway)",
                "4": "Two-Way Undivided",
                "5": "Others / Ramps"
            }
            access_code_map = {
                "1": "Full Access Control (Freeway)",
                "2": "Partial Access Control (Expressway)",
                "3": "No Access Control (Conventional Highway)"
            }
            surface_code_map = {
                "1": "Unpaved",
                "2": "Bituminous (Asphalt)",
                "3": "JPCP (Jointed Plain Concrete)",
                "4": "JRCP (Jointed Reinforced Concrete)",
                "5": "CRCP (Continuously Reinforced Concrete)",
                "6": "AC Overlay on AC",
                "7": "AC Overlay on PCC",
                "8": "PCC Overlay on PCC",
                "9": "Other"
            }
            fsystem_code_map = {
                "1": "Interstate",
                "2": "Principal Arterial - Other Freeways & Expressways",
                "3": "Principal Arterial - Other",
                "4": "Minor Arterial",
                "5": "Major Collector",
                "6": "Minor Collector",
                "7": "Local"
            }

            fac_vals = [facility_code_map.get(str(s.get("facility_type")), str(s.get("facility_type"))) for s in secs if s.get("facility_type") is not None]
            acc_vals = [access_code_map.get(str(s.get("access_control")), str(s.get("access_control"))) for s in secs if s.get("access_control") is not None]
            surf_vals = [surface_code_map.get(str(s.get("surface_type")), str(s.get("surface_type"))) for s in secs if s.get("surface_type") is not None]
            fsys_vals = [fsystem_code_map.get(str(s.get("f_system")), str(s.get("f_system"))) for s in secs if s.get("f_system") is not None]

            fa_vals = [s.get("future_aadt") for s in secs if s.get("future_aadt")]
            fa_yr_vals = [str(s.get("future_aadt_year"))[:4] for s in secs if s.get("future_aadt_year")]

            hpms_summary = {
                "sections_count": hpms_res.get("hpms_sections_count", len(secs)),
                "predominant_lanes_2024": hpms_res.get("hpms_predominant_lanes_2024"),
                "avg_lanes_2024": hpms_res.get("hpms_avg_lanes_2024"),
                "lane_distribution_mi": hpms_res.get("hpms_lane_distribution_mi"),
                "avg_aadt_2024": hpms_res.get("hpms_avg_aadt_2024"),
                "facility_type": ", ".join(dict.fromkeys(fac_vals)) if fac_vals else None,
                "access_control": ", ".join(dict.fromkeys(acc_vals)) if acc_vals else None,
                "surface_type": ", ".join(dict.fromkeys(surf_vals)) if surf_vals else None,
                "functional_system": ", ".join(dict.fromkeys(fsys_vals)) if fsys_vals else None,
                "future_aadt": fa_vals[0] if fa_vals else None,
                "future_aadt_year": fa_yr_vals[0] if fa_yr_vals else None,
            }
    except Exception as exc:
        LOGGER.debug(f"HPMS corridor lookup failed: {exc}")

    uninstrumented_info: Optional[Dict[str, Any]] = None
    if not results and matched_projects:
        first_p = matched_projects[0]
        route_norm = normalize_route(first_p.route)
        all_route_stations = [s for s in vds_meta.by_id.values() if s.fwy == route_norm] if vds_meta else []
        min_abs = min(first_p.abs_begin_pm or 0.0, first_p.abs_end_pm or 0.0)
        max_abs = max(first_p.abs_begin_pm or 0.0, first_p.abs_end_pm or 0.0)

        nearest_station = None
        nearest_station_dist_mi = None
        if not all_route_stations:
            coverage_status = "UNINSTRUMENTED STATEWIDE"
            coverage_details = f"Route {route_norm} has no active PeMS detector stations instrumented statewide."
        else:
            coverage_status = "UNINSTRUMENTED CORRIDOR SEGMENT"
            st_counties = sorted(set(normalize_ca_county(s.county) for s in all_route_stations if s.county))
            st_counties_str = ", ".join(st_counties)
            nst = min(all_route_stations, key=lambda s: abs((s.abs_pm if s.abs_pm is not None else 9999) - min_abs))
            nearest_station = nst
            if nst.abs_pm is not None:
                nearest_station_dist_mi = abs(nst.abs_pm - min_abs)
                coverage_details = (
                    f"Route {route_norm} has {len(all_route_stations)} active PeMS stations located in {st_counties_str} County/Counties. "
                    f"The {first_p.county} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f}) is not instrumented with PeMS mainline loop detectors."
                )
            else:
                coverage_details = (
                    f"Route {route_norm} has {len(all_route_stations)} active PeMS stations in {st_counties_str} County/Counties, "
                    f"but none in the {first_p.county} County corridor (Abs PM {min_abs:.2f} - {max_abs:.2f})."
                )

        census_stations = []
        try:
            _, b_num = split_ca_pm(first_p.begin_pm or "0")
            raw_census = find_closest_census_stations(route_norm, str(first_p.district or "4"), b_num if not np.isnan(b_num) else 0.0, county=str(first_p.county), top_n=3)
            if not raw_census.empty:
                for _, crow in raw_census.iterrows():
                    census_stations.append({
                        "ID": str(crow["ID"]),
                        "Name": str(crow["Name"]),
                        "CA PM": str(crow["CA PM"]),
                        "Abs PM": f"{crow['Abs PM']:.2f}" if pd.notna(crow.get("Abs PM")) else "-",
                        "pm_diff": float(crow.get("pm_diff", 0.0))
                    })
        except Exception:
            pass

        uninstrumented_info = {
            "coverage_status": coverage_status,
            "coverage_details": coverage_details,
            "nearest_station": nearest_station,
            "nearest_station_dist_mi": nearest_station_dist_mi,
            "census_stations": census_stations,
        }

    return {
        "project_id": pid_clean,
        "projects": matched_projects,
        "stations": results,
        "spreadsheet_stations": spreadsheet_sids,
        "corridor_stations": corridor_sids,
        "hpms_summary": hpms_summary,
        "uninstrumented_info": uninstrumented_info,
    }


def format_lane_config_report(
    details: Union[StationLaneConfigDetails, List[StationLaneConfigDetails]],
    project: Optional[ProjectRecord] = None,
    hpms_summary: Optional[Dict[str, Any]] = None,
    uninstrumented_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Format station lane tables, change logs, and TSN roadway metrics into a clean human-readable report.
    Also incorporates HPMS certified roadway cross-section metrics when available.
    """
    if isinstance(details, StationLaneConfigDetails):
        station_list = [details]
    else:
        station_list = details

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("CALTRANS PEMS STATION LANE CONFIGURATION & ROADWAY METRICS")
    if project:
        lines.append(f"Project ID: {project.project_id} | Route {project.route} {project.direction} | Dist {project.dist}, {project.county} County")
        lines.append(f"Limits: PM {project.begin_pm} - {project.end_pm} (Abs: {project.abs_begin_pm} - {project.abs_end_pm})")
    lines.append("=" * 80)

    # HPMS Ground-Truth Roadway Attributes Section
    if hpms_summary:
        lines.append("\n[FHWA / Caltrans HPMS Ground-Truth Roadway Attributes]")
        pred_lanes = hpms_summary.get("predominant_lanes_2024")
        avg_lanes = hpms_summary.get("avg_lanes_2024")
        lane_str = f"{pred_lanes}" if pred_lanes is not None else "N/A"
        if avg_lanes is not None and avg_lanes != pred_lanes:
            lane_str += f" (Corridor length-weighted avg: {avg_lanes:.2f})"
        lines.append(f"  • Through Lanes (2024):         {lane_str}")
        if hpms_summary.get("facility_type"):
            lines.append(f"  • Facility Type:                {hpms_summary['facility_type']}")
        if hpms_summary.get("access_control"):
            lines.append(f"  • Access Control:               {hpms_summary['access_control']}")
        if hpms_summary.get("surface_type"):
            lines.append(f"  • Surface Type:                 {hpms_summary['surface_type']}")
        if hpms_summary.get("functional_system"):
            lines.append(f"  • Functional System:            {hpms_summary['functional_system']}")
        if hpms_summary.get("avg_aadt_2024"):
            lines.append(f"  • Certified AADT (2024):        {hpms_summary['avg_aadt_2024']:,} vehicles/day")
        if hpms_summary.get("future_aadt"):
            yr_str = f" (Year {hpms_summary['future_aadt_year']})" if hpms_summary.get('future_aadt_year') else ""
            try:
                fa = int(float(hpms_summary['future_aadt']))
                lines.append(f"  • Forecast Future AADT:         {fa:,} vehicles/day{yr_str}")
            except Exception:
                lines.append(f"  • Forecast Future AADT:         {hpms_summary['future_aadt']}{yr_str}")
        if hpms_summary.get("sections_count"):
            lines.append(f"  • HPMS Corridor Sections:       {hpms_summary['sections_count']} sections analyzed")
        lines.append("-" * 80)

    if not station_list:
        lines.append("\n[Corridor Instrumentation & PeMS VDS Status]")
        lines.append("  • PeMS Mainline Detectors:      0 stations found (Uninstrumented corridor segment)")
        if uninstrumented_info and uninstrumented_info.get("coverage_details"):
            lines.append(f"  • Coverage Details:             {uninstrumented_info['coverage_details']}")
        elif project:
            lines.append(f"  • Coverage Details:             Route {project.route} {project.direction or ''} in {project.county} County (PM {project.begin_pm} - {project.end_pm}) is not instrumented with active mainline PeMS loop detectors.")
        if uninstrumented_info and uninstrumented_info.get("nearest_station"):
            nst = uninstrumented_info["nearest_station"]
            nst_pm = f"Abs PM {nst.abs_pm:.2f}" if getattr(nst, "abs_pm", None) is not None else ""
            dist_str = f", {uninstrumented_info['nearest_station_dist_mi']:.2f} mi away." if uninstrumented_info.get("nearest_station_dist_mi") is not None else "."
            lines.append(f"  • Nearest Active Station:       Station {nst.id} ({nst.name}, {nst_pm}){dist_str}")
        if uninstrumented_info and uninstrumented_info.get("census_stations"):
            c_sts = uninstrumented_info["census_stations"]
            lines.append("\n[Caltrans Annual Census Traffic Stations]")
            for c in c_sts:
                lines.append(f"  • Station {c['ID']}: {c['Name']} (CA PM {c['CA PM']}, Abs PM {c['Abs PM']})")
        lines.append("=" * 80)
        return "\n".join(lines)

    for idx, st in enumerate(station_list, start=1):
        lines.append("")
        lines.append("-" * 80)
        st_type_str = f" ({st.station_type})" if st.station_type else ""
        st_name_str = f": {st.station_name}" if st.station_name else ""
        lines.append(f"STATION {st.station_id}{st_name_str}{st_type_str} [Station {idx}/{len(station_list)}]")
        loc_parts = []
        if st.route:
            loc_parts.append(f"Route: {st.route} {st.direction or ''}".strip())
        if st.district:
            loc_parts.append(f"Dist: {st.district}")
        if st.county:
            loc_parts.append(f"County: {st.county}")
        if loc_parts:
            lines.append(" | ".join(loc_parts))

        pm_parts = []
        if st.ca_pm:
            pm_parts.append(f"CA PM: {st.ca_pm}")
        if st.abs_pm is not None:
            pm_parts.append(f"Abs PM: {st.abs_pm:.2f}")
        if st.latitude and st.longitude:
            pm_parts.append(f"Coordinates: ({st.latitude:.5f}, {st.longitude:.5f})")
        if pm_parts:
            lines.append(" | ".join(pm_parts))

        if st.url:
            lines.append(f"PeMS URL: {st.url}")
        lines.append("-" * 80)

        if st.error:
            lines.append(f"  [ERROR / STATUS]: {st.error}")
            continue

        # 1. TSN Roadway Metrics
        lines.append("\n  [Caltrans TSN Roadway Metrics]")
        if st.tsn_roadway_info:
            if st.tsn_roadway_info.get("Status") == "Not Available in PeMS":
                lines.append("    • Status:                          Not Available in PeMS for this station")
                lines.append("      (Caltrans PeMS has not linked TSN roadway data for this station/corridor;")
                lines.append("       see HPMS Ground-Truth Roadway Attributes above for official cross-section metrics)")
            else:
                for k, v in st.tsn_roadway_info.items():
                    lines.append(f"    • {k + ':':<32} {v}")
        else:
            lines.append("    (No TSN roadway metrics recorded for this station)")

        # 2. Lane Configuration Table
        lines.append("\n  [PeMS Station Lane Configuration Table]")
        if st.lane_table:
            cols = list(st.lane_table[0].keys())
            # Format table with aligned widths
            col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in st.lane_table)) for c in cols}
            hdr_str = "    " + "  ".join(f"{c:<{col_widths[c]}}" for c in cols)
            sep_str = "    " + "  ".join("-" * col_widths[c] for c in cols)
            lines.append(hdr_str)
            lines.append(sep_str)
            for r in st.lane_table:
                row_str = "    " + "  ".join(f"{str(r.get(c, '')):<{col_widths[c]}}" for c in cols)
                lines.append(row_str)
            lines.append("    [Note: Lane types reflect raw PeMS definitions ('Mainline', 'HOV', etc.).")
            lines.append("     PeMS does not track granular types like climbing lanes, auxiliary lanes, or turn pockets.]")
        else:
            lines.append("    (No lane configuration records found)")

        # 3. Change Log
        lines.append("\n  [PeMS Configuration History / Change Log]")
        if st.change_log:
            cols = list(st.change_log[0].keys())
            col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in st.change_log)) for c in cols}
            hdr_str = "    " + "  ".join(f"{c:<{col_widths[c]}}" for c in cols)
            sep_str = "    " + "  ".join("-" * col_widths[c] for c in cols)
            lines.append(hdr_str)
            lines.append(sep_str)
            for r in st.change_log:
                row_str = "    " + "  ".join(f"{str(r.get(c, '')):<{col_widths[c]}}" for c in cols)
                lines.append(row_str)
        else:
            lines.append("    (No configuration changes recorded in PeMS change log)")

        # 4. Construction History
        if st.construction_history:
            lines.append("\n  [PeMS Construction History]")
            cols = list(st.construction_history[0].keys())
            col_widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in st.construction_history)) for c in cols}
            hdr_str = "    " + "  ".join(f"{c:<{col_widths[c]}}" for c in cols)
            sep_str = "    " + "  ".join("-" * col_widths[c] for c in cols)
            lines.append(hdr_str)
            lines.append(sep_str)
            for r in st.construction_history:
                row_str = "    " + "  ".join(f"{str(r.get(c, '')):<{col_widths[c]}}" for c in cols)
                lines.append(row_str)

    lines.append("\n" + "=" * 80)
    return "\n".join(lines)


def _save_lane_config_report(
    output_path: str,
    stations: List[StationLaneConfigDetails],
    project: Optional[ProjectRecord] = None,
    hpms_summary: Optional[Dict[str, Any]] = None,
    uninstrumented_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Save lane configuration and TSN roadway metrics report to file (JSON, Markdown, or text)."""
    out_ext = os.path.splitext(output_path)[1].lower()
    if out_ext == ".json":
        data = {
            "project_id": project.project_id if project else None,
            "project_description": project.project_description if project else None,
            "route": project.route if project else None,
            "direction": project.direction if project else None,
            "hpms_summary": hpms_summary,
            "uninstrumented_info": {
                "coverage_status": uninstrumented_info.get("coverage_status"),
                "coverage_details": uninstrumented_info.get("coverage_details"),
                "nearest_station": uninstrumented_info.get("nearest_station").id if uninstrumented_info and uninstrumented_info.get("nearest_station") else None,
                "census_stations": uninstrumented_info.get("census_stations"),
            } if uninstrumented_info else None,
            "stations": [
                {
                    "station_id": s.station_id,
                    "station_name": s.station_name,
                    "district": s.district,
                    "route": s.route,
                    "direction": s.direction,
                    "county": s.county,
                    "station_type": s.station_type,
                    "ca_pm": s.ca_pm,
                    "abs_pm": s.abs_pm,
                    "latitude": s.latitude,
                    "longitude": s.longitude,
                    "url": s.url,
                    "error": s.error,
                    "tsn_roadway_info": s.tsn_roadway_info,
                    "lane_table": s.lane_table,
                    "change_log": s.change_log,
                    "construction_history": s.construction_history,
                }
                for s in stations
            ]
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"\n✓ Exported station lane configuration JSON -> {output_path}")
    else:
        report_text = format_lane_config_report(stations, project=project, hpms_summary=hpms_summary, uninstrumented_info=uninstrumented_info)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(f"\n✓ Exported station lane configuration report -> {output_path}")



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
        is_abs = (
            project.abs_begin_pm is not None
            and not np.isnan(project.abs_begin_pm)
            and project.abs_end_pm is not None
            and not np.isnan(project.abs_end_pm)
        )
        if is_abs:
            b_pm = float(project.abs_begin_pm)
            e_pm = float(project.abs_end_pm)
        else:
            _, b_num = split_ca_pm(project.begin_pm)
            _, e_num = split_ca_pm(project.end_pm)
            b_pm = b_num
            e_pm = e_num

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
    sensors_str = ", ".join(html.escape(s) for s in project.sensor_stations) if project.sensor_stations else "None listed"
    b_lanes = html.escape(str(project.raw_row.get("dir_lanes_before", "?")))
    a_lanes = html.escape(str(project.raw_row.get("dir_lanes_after", "?")))
    b_pm = format_pm(project.begin_pm)
    e_pm = format_pm(project.end_pm)
    raw_desc = str(project.project_description or project.raw_row.get("Project Description", "")).strip()
    if len(raw_desc) > 85:
        raw_desc = raw_desc[:82] + "..."
    desc = html.escape(raw_desc)

    return f"""
    <div style="font-family: {MONO_FONT}; font-size: 12px; line-height: 1.4; min-width: 210px; color: #000; background: #fff; padding: 6px 8px; border: 1px solid #000;">
      <div style="font-weight: bold; font-size: 12.5px; color: #000; margin-bottom: 2px;">
        PID {html.escape(project.project_id)} &middot; Route {html.escape(project.route)} {html.escape(project.direction)}
      </div>
      <div><b>Dist {html.escape(project.dist)}</b> &middot; {html.escape(project.county)} County</div>
      <div><b>Postmiles:</b> PM {b_pm} &rarr; {e_pm} ({length_mi:.2f} mi)</div>
      <div><b>Directional Lanes:</b> {b_lanes} &rarr; {a_lanes}</div>
      <div><b>Dates:</b> Const: {html.escape(project.construction_begin_date or 'N/A')} | Open: {html.escape(project.open_to_traffic_date or 'N/A')}</div>
      <div><b>Sensors:</b> {sensors_str}</div>
      {f'<div style="color: #333; font-size: 11.5px; margin-top: 4px; border-top: 1px solid #666; padding-top: 3px;">{desc}</div>' if desc else ''}
    </div>
    """


def make_project_popup_html(project: ProjectRecord, builder: ProjectPeMSQueryBuilder, length_mi: float = 0.0) -> str:
    """Generate detailed click popup card HTML with utilitarian monospaced aesthetic and live links."""
    urls = builder.generate_all_urls()
    raw = project.raw_row
    tot_b = html.escape(str(raw.get("Total Lanes Before", raw.get("total_lanes_before", "?"))))
    tot_a = html.escape(str(raw.get("Total Lanes After", raw.get("total_lanes_after", "?"))))
    dir_b = html.escape(str(raw.get("dir_lanes_before", "?")))
    dir_a = html.escape(str(raw.get("dir_lanes_after", "?")))
    added_lm = html.escape(str(raw.get("Total Added Lanes (Lane Miles)", raw.get("New GP Lanes (Lane Miles)", "?"))))
    facility = html.escape(str(raw.get("Facility Class checked by Lu") or raw.get("Facility Class") or raw.get("facility_class") or "Freeway").strip())
    area_type = html.escape(str(raw.get("area_type") or raw.get("Area Type") or raw.get('Area Type ("Urban", "Suburban", "Exurban", "Rural", or "Unknown")') or "Unknown").strip().capitalize())
    desc = html.escape(str(project.project_description or raw.get("Project Description", "")).strip())

    b_pm = format_pm(project.begin_pm)
    e_pm = format_pm(project.end_pm)
    abs_b = format_pm(project.abs_begin_pm)
    abs_e = format_pm(project.abs_end_pm)

    sensor_links = []
    for s_id in project.sensor_stations:
        s_url = urls["station_summary_urls"].get(s_id, f"https://pems.dot.ca.gov/?dnode=VDS&station_id={s_id}")
        sensor_links.append(f'<a href="{s_url}" target="_blank" style="color: #0000ee; text-decoration: underline;">{html.escape(s_id)}</a>')
    sensor_html = ", ".join(sensor_links) if sensor_links else "None listed"

    return f"""
    <div style="font-family: {MONO_FONT}; font-size: 12px; line-height: 1.45; width: 330px; padding: 6px; color: #000; background: #fff;">
      <div style="border-bottom: 1px solid #000; padding-bottom: 4px; margin-bottom: 6px;">
        <div style="font-size: 11px; font-weight: bold; text-transform: uppercase;">District {html.escape(project.dist)} &middot; {facility}</div>
        <h3 style="margin: 4px 0 2px; font-size: 14px; font-weight: bold; color: #000;">PID {html.escape(project.project_id)} &middot; Route {html.escape(project.route)} {html.escape(project.direction)}</h3>
        <div style="font-size: 11px; color: #333;">{html.escape(project.county)} County &middot; Area: {area_type}</div>
      </div>

      <table style="width: 100%; font-size: 12px; margin-bottom: 8px; border-collapse: collapse; border: 1px solid #666;">
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Postmiles:</td><td style="padding: 2px 4px; font-weight: bold;">PM {b_pm} &rarr; {e_pm} ({length_mi:.2f} mi)</td></tr>
        {f'<tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Abs PMs:</td><td style="padding: 2px 4px;">{abs_b} &rarr; {abs_e}</td></tr>' if abs_b else ''}
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Directional Lanes:</td><td style="padding: 2px 4px; font-weight: bold;">{dir_b} &rarr; {dir_a}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Total Roadway Lanes:</td><td style="padding: 2px 4px;">{tot_b} &rarr; {tot_a}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Lane Miles Added:</td><td style="padding: 2px 4px;">{added_lm}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Construction Date:</td><td style="padding: 2px 4px;">{html.escape(project.construction_begin_date or 'N/A')}</td></tr>
        <tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Open to Traffic:</td><td style="padding: 2px 4px;">{html.escape(project.open_to_traffic_date or 'N/A')}</td></tr>
        <tr><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Sensors:</td><td style="padding: 2px 4px;">{sensor_html}</td></tr>
      </table>

      {f'<div style="font-size: 11.5px; color: #333; border: 1px solid #ccc; padding: 4px; margin-bottom: 6px; max-height: 55px; overflow-y: auto;"><b>Description:</b> {desc}</div>' if desc else ''}

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
    Direction-aware: accurately accounts for traffic flow direction on Southbound and Westbound routes.
    Returns:
        (is_in_bounds, bounds_note, delta_mi)
    """
    is_descending = project.pems_direction in ("S", "W")

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
            pos_label = "downstream / after end" if is_descending else "upstream / before start"
            return False, f"{delta:.2f} mi {pos_label}", delta
        else:
            delta = st_p - max_p
            pos_label = "upstream / before start" if is_descending else "downstream / after end"
            return False, f"{delta:.2f} mi {pos_label}", delta

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
            pos_label = "downstream / after end" if is_descending else "upstream / before start"
            return False, f"{delta:.2f} mi {pos_label}", delta
        else:
            delta = st_num - max_p
            pos_label = "upstream / before start" if is_descending else "downstream / after end"
            return False, f"{delta:.2f} mi {pos_label}", delta

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

    return f"""<div style="font-family: {MONO_FONT}; font-size: 11.5px; line-height: 1.4; padding: 4px; min-width: 240px; color: #000; background: #fff;">
  <div style="font-weight: bold; font-size: 12px; border-bottom: 1px solid #000; padding-bottom: 3px; margin-bottom: 4px;">
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
    """Build detailed click popup HTML with PeMS links and ground-truth corridor context."""
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
  <div style="margin-top: 5px; padding: 4px; border: 1px solid #ccc; background: #fdfdfd; font-size: 11px;">
    <strong>Corridor Context (PID {project.project_id}):</strong><br>
    Route {project.route} {project.direction} · PM {project.begin_pm} &rarr; {project.end_pm}<br>
    {bound_html}
    {sheet_html}
  </div>
"""

    return f"""<div style="font-family: {MONO_FONT}; font-size: 11.5px; line-height: 1.4; padding: 6px; min-width: 280px; max-width: 370px; color: #000; background: #fff; border: 1px solid #000;">
  <div style="font-weight: bold; font-size: 12.5px; border-bottom: 1px solid #000; padding-bottom: 4px; margin-bottom: 6px;">
    Station {station.id} · {station.type} ({type_name})
  </div>
  <div><strong>Location:</strong> {station.name or 'N/A'}</div>
  <div><strong>Freeway & Dir:</strong> Route {station.fwy} {station.dir} · District {station.district}</div>
  <div><strong>County / City:</strong> {station.county or 'N/A'}, {station.city or 'N/A'}</div>
  <div><strong>State Postmile:</strong> {station.state_pm}</div>
  {f'<div><strong>Absolute Postmile:</strong> {station.abs_pm:.3f}</div>' if station.abs_pm is not None else ''}
  <div><strong>Lanes:</strong> {station.lanes or 'N/A'} &nbsp;|&nbsp; <strong>Detector Length:</strong> {f'{station.length:.3f} mi' if station.length else 'N/A'}</div>
  {proj_html}
  <div style="margin-top: 6px; padding-top: 4px; border-top: 1px solid #000; font-size: 11px;">
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
    """Add a utilitarian monospaced Ground-Truth Checker toolbar and styling to the Folium map."""
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
  font-family: {MONO_FONT};
  font-size: 11.5px;
  line-height: 1.4;
  color: #000000;
  max-width: 340px;
  box-shadow: none;
">
  <div style="font-weight: bold; font-size: 12px; border-bottom: 1px solid #000; padding-bottom: 3px; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center;">
    <span>PeMS Ground-Truth Checker</span>
    <button onclick="toggleGroundTruthPanel(this)" style="font-family: inherit; font-size: 11px; background: transparent; border: 1px solid #000; padding: 1px 5px; cursor: pointer;">[–]</button>
  </div>
  <div id="pems-gt-content">
    <div style="margin-bottom: 6px; font-size: 11px; color: #222;">
      <strong>Project {project.project_id}:</strong> Route {project.route} {project.direction} (PM {project.begin_pm} &rarr; {project.end_pm})<br>
      <strong>Corridor Detectors:</strong> <b>{in_bounds_count}</b> in bounds · {oob_count} buffer<br>
      <strong>Master Sheet:</strong> <b>{sheet_sensors_verified}/{sheet_sensors_total}</b> verified in corridor{discrepancy_str}
    </div>
    <div style="margin-bottom: 6px; padding-top: 4px; border-top: 1px dotted #888;">
      <button id="pems-oob-btn" onclick="toggleInBoundsOnly(this)" style="font-family: inherit; background: #ffffff; border: 1px solid #000; font-size: 11px; padding: 2px 7px; cursor: pointer; margin-right: 5px;">
        In-Bounds Only: OFF
      </button>
      <button onclick="toggleAllStationLayers(this)" style="font-family: inherit; background: #ffffff; border: 1px solid #000; font-size: 11px; padding: 2px 7px; cursor: pointer;">
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
    target_pid: Union[str, ProjectRecord],
    projects: Optional[List[ProjectRecord]] = None,
    output_path: Optional[str] = None,
    open_browser: bool = False,
    tiles: Optional[str] = None,
    include_stations: bool = True,
    station_buffer_mi: float = 3.0,
    station_types: Optional[Tuple[str, ...]] = None,
    vds_metadata: Optional[IndexedVDSMetadata] = None,
    metadata_dir: Optional[str] = None,
) -> Optional[folium.Map]:
    """
    Build a folium map for a single project (or both directions if split) with
    highway path, start/end markers, corridor station detector layers partitioned by type,
    rich hover tooltips, and an interactive ground-truth checker toolbar.
    """
    if isinstance(target_pid, ProjectRecord):
        if projects is None:
            projects = [target_pid]
        target_pid = target_pid.project_id

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
        if vds is None and metadata_dir is not None:
            try:
                vds = load_vds_metadata(metadata_dir)
            except Exception as exc:
                LOGGER.warning(f"Could not load PeMS VDS metadata from {metadata_dir}: {exc}")
                vds = None
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
                    station_types=station_types or ("ALL",),
                    county=p.county
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
                            coords = pm_to_coordinates(st.fwy, st.dir, st_num, county=st.county, ref_df=idx_ref)
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
    font-family: "Andale Mono", Menlo, Monaco, "Courier New", Courier, monospace !important;
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
                    station_types=station_types or ("ALL",),
                    county=p.county
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
                            coords = pm_to_coordinates(st.fwy, st.dir, st_num, county=st.county, ref_df=idx_ref)
                            if coords:
                                lat, lng = coords
                    if lat is None or lng is None or np.isnan(lat) or np.isnan(lng) or lat == 0:
                        continue

                    fill_color = STATION_TYPE_COLORS.get(st.type, "#2563eb")
                    pid_str = ", ".join(pids) if pids else "Corridor"
                    type_name = STATION_TYPE_NAMES.get(st.type, st.type)
                    tt_html = f"""<div style="font-family: {MONO_FONT}; font-size: 11.5px; line-height: 1.35; padding: 3px; min-width: 200px; color: #000; background: #fff;">
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
    font-family: "Andale Mono", Menlo, Monaco, "Courier New", Courier, monospace !important;
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
# HISTORICAL SATELLITE IMAGERY INTEGRATION (WAYBACK)
# ---------------------------------------------------------------------------
def calculate_corridor_curvature_bounds(
    start_coords: Tuple[float, float],
    end_coords: Tuple[float, float],
    intermediate_coords: Sequence[Tuple[float, float]]
) -> Tuple[float, float, float, float, float]:
    """
    Calculate lateral (perpendicular) deviation metrics in meters of intermediate roadway
    centerline points from the straight chord between endpoints.
    Returns:
      (min_dev, max_dev, max_abs_dev, lateral_shift, lateral_half_span)
      where:
        - min_dev: minimum signed lateral offset (negative = left of travel)
        - max_dev: maximum signed lateral offset (positive = right of travel)
        - max_abs_dev: max(abs(min_dev), abs(max_dev))
        - lateral_shift: (min_dev + max_dev) / 2 (offset to center of roadway curve)
        - lateral_half_span: (max_dev - min_dev) / 2 (half-width required to encompass curve)
    """
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

    # Perpendicular unit normal vector
    nx = -vy / chord_len
    ny = vx / chord_len

    signed_devs = [0.0]  # endpoints have 0 lateral offset
    for lat_k, lon_k in intermediate_coords:
        dx = (lon_k - lon0) * m_per_deg_lon
        dy = (lat_k - lat0) * m_per_deg_lat
        dev = dx * nx + dy * ny
        signed_devs.append(dev)

    min_dev = min(signed_devs)
    max_dev = max(signed_devs)
    max_abs_dev = max(abs(min_dev), abs(max_dev))
    lateral_shift = (min_dev + max_dev) / 2.0
    lateral_half_span = (max_dev - min_dev) / 2.0

    return min_dev, max_dev, max_abs_dev, lateral_shift, lateral_half_span


def calculate_corridor_curvature_deviation(
    start_coords: Tuple[float, float],
    end_coords: Tuple[float, float],
    intermediate_coords: Sequence[Tuple[float, float]]
) -> float:
    """
    Calculate the maximum lateral (perpendicular) deviation in meters
    of intermediate roadway centerline points from the straight chord between endpoints.
    Used to dynamically expand imagery bounding box swath so curved roads are never cropped out.
    """
    _, _, max_abs_dev, _, _ = calculate_corridor_curvature_bounds(start_coords, end_coords, intermediate_coords)
    return max_abs_dev


def slice_project_corridor(
    project: ProjectRecord,
    slice_mi: float = 0.25,
    idx_ref: Optional[IndexedGeoReference] = None
) -> List[Dict[str, Any]]:
    """
    Partition a project corridor into sequential roadway segments of approximately slice_mi length.
    Returns list of dicts with:
      - 'index': 1-based integer
      - 'start_pm': float
      - 'end_pm': float
      - 'start_coords': (lat, lon)
      - 'end_coords': (lat, lon)
      - 'length_mi': float
      - 'curvature_dev_m': float
      - 'coords': list of (lat, lon) points
    """
    ref = idx_ref if idx_ref is not None else get_indexed_geo_ref()
    sub = ref.get_corridor(project.route, project.pems_direction)
    slices = []

    is_abs = (
        project.abs_begin_pm is not None
        and not np.isnan(project.abs_begin_pm)
        and project.abs_end_pm is not None
        and not np.isnan(project.abs_end_pm)
    )
    if is_abs:
        b_pm = float(project.abs_begin_pm)
        e_pm = float(project.abs_end_pm)
        pm_col = "_abs_pm"
    else:
        _, b_num = split_ca_pm(project.begin_pm)
        _, e_num = split_ca_pm(project.end_pm)
        b_pm = b_num
        e_pm = e_num
        pm_col = "_ca_num"

    pts = None
    if sub is not None and not sub.empty and b_pm is not None and e_pm is not None and not np.isnan(b_pm) and not np.isnan(e_pm):
        min_pm, max_pm = min(float(b_pm), float(e_pm)), max(float(b_pm), float(e_pm))
        ascending = float(b_pm) <= float(e_pm)
        if is_abs:
            cond = (sub["_abs_pm"] >= min_pm - 0.05) & (sub["_abs_pm"] <= max_pm + 0.05)
            pts = sub[cond].sort_values(by="_abs_pm", ascending=ascending)
        else:
            county_clean = str(project.county).strip().lower()
            cond = (sub["_ca_num"] >= min_pm - 0.05) & (sub["_ca_num"] <= max_pm + 0.05) & (sub["_county"] == county_clean)
            pts = sub[cond].sort_values(by="_ca_num", ascending=ascending)
        if pts is not None:
            pts = pts.dropna(subset=["_lat", "_lng"])

    if pts is not None and len(pts) >= 2:
        step = max(1, int(round(slice_mi / 0.1)))
        n_pts = len(pts)
        for i in range(0, n_pts - 1, step):
            j = min(i + step, n_pts - 1)
            if j == i:
                break
            p0 = pts.iloc[i]
            p1 = pts.iloc[j]
            # Use State PM for user display if available, else absolute PM
            if "_ca_num" in p0 and pd.notna(p0["_ca_num"]) and float(p0["_ca_num"]) > 0:
                s_pm = float(p0["_ca_num"])
                e_pm_val = float(p1["_ca_num"])
            else:
                s_pm = float(p0[pm_col])
                e_pm_val = float(p1[pm_col])
            length = round(abs(float(p1[pm_col]) - float(p0[pm_col])), 2)
            if length == 0:
                length = round(0.1 * (j - i), 2)

            sub_pts = pts.iloc[i : j + 1]
            sub_coords = [(float(r["_lat"]), float(r["_lng"])) for _, r in sub_pts.iterrows()]
            start_c = (float(p0["_lat"]), float(p0["_lng"]))
            end_c = (float(p1["_lat"]), float(p1["_lng"]))
            min_dev, max_dev, max_abs_dev, lat_shift, lat_hspan = calculate_corridor_curvature_bounds(
                start_c, end_c, sub_coords
            )

            slices.append({
                "index": len(slices) + 1,
                "start_pm": round(s_pm, 2),
                "end_pm": round(e_pm_val, 2),
                "abs_start_pm": round(float(p0["_abs_pm"]), 2) if "_abs_pm" in p0 and pd.notna(p0["_abs_pm"]) else None,
                "abs_end_pm": round(float(p1["_abs_pm"]), 2) if "_abs_pm" in p1 and pd.notna(p1["_abs_pm"]) else None,
                "start_coords": start_c,
                "end_coords": end_c,
                "length_mi": length,
                "curvature_dev_m": round(max_abs_dev, 2),
                "lateral_shift_m": round(lat_shift, 2),
                "lateral_half_span_m": round(lat_hspan, 2),
                "coords": sub_coords,
            })
    else:
        # Fallback to endpoints
        builder = ProjectPeMSQueryBuilder(project)
        start_pt, end_pt = builder.resolve_geometry()
        if start_pt and end_pt:
            (lat1, lon1), (lat2, lon2) = start_pt, end_pt
            mid_lat = (lat1 + lat2) / 2.0
            dx_m = (lon2 - lon1) * 111320.0 * math.cos(math.radians(mid_lat))
            dy_m = (lat2 - lat1) * 111320.0
            dist_m = math.hypot(dx_m, dy_m)
            dist_mi = max(0.1, dist_m / 1609.34)
            n_chunks = max(1, int(math.ceil(dist_mi / slice_mi)))

            p_start_pm = float(b_pm) if b_pm is not None and not np.isnan(b_pm) else 0.0
            p_end_pm = float(e_pm) if e_pm is not None and not np.isnan(e_pm) else dist_mi

            for k in range(n_chunks):
                t0 = k / n_chunks
                t1 = (k + 1) / n_chunks
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
    """
    Combine Before and After highway corridor strips into a single pixel-locked, vertically stacked comparison PNG.
    Utilitarian black-on-white layout with clear roadway metrics and high contrast.
    """
    from PIL import Image, ImageDraw
    import wayback_imagery.core as wayback

    img_b = Image.open(before_img_path)
    img_a = Image.open(after_img_path)

    w = max(img_b.width, img_a.width)
    header_h = 72
    divider_h = 28
    footer_h = 32
    total_h = header_h + img_b.height + divider_h + img_a.height + footer_h

    canvas = Image.new("RGB", (w, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    font_lg = wayback._load_font(max(14, min(22, w // 80)))
    font_sm = wayback._load_font(max(11, min(15, w // 120)))

    # Header
    title = f"PROJECT {project.project_id} | ROUTE {project.route} {project.direction} | SEGMENT {slice_info['index']}: PM {slice_info['start_pm']:.2f} -> {slice_info['end_pm']:.2f} ({slice_info['length_mi']:.2f} mi)"
    subtitle = f"County: {project.county} | District: {project.dist} | Native Zoom {zoom} (~0.3m/px) | Corridor runs Left to Right"
    draw.text((20, 14), title, fill=(0, 0, 0), font=font_lg)
    draw.text((20, 42), subtitle, fill=(80, 80, 80), font=font_sm)
    draw.line([(0, header_h - 1), (w, header_h - 1)], fill=(0, 0, 0), width=1)

    # Paste Before
    canvas.paste(img_b, (0, header_h))

    # Before Badge
    b_badge = f"BEFORE (Pre-Construction) | Flight: {before_capture_date or 'Unknown'}"
    badge_w = int(font_sm.getlength(b_badge)) + 24
    draw.rectangle([(16, header_h + 12), (16 + badge_w, header_h + 36)], fill=(255, 255, 255), outline=(0, 0, 0), width=1)
    draw.text((26, header_h + 16), b_badge, fill=(0, 0, 0), font=font_sm)

    # Divider bar
    y_div = header_h + img_b.height
    draw.rectangle([(0, y_div), (w, y_div + divider_h)], fill=(240, 240, 240), outline=(0, 0, 0), width=1)
    div_text = f"BEFORE (Pre-Construction)  /  AFTER (Post-Opening)  [Roadway Strip: {slice_info['length_mi']:.2f} mi]"
    draw.text((20, y_div + 6), div_text, fill=(0, 0, 0), font=font_sm)

    # Paste After
    canvas.paste(img_a, (0, y_div + divider_h))

    # After Badge
    y_after = y_div + divider_h
    a_badge = f"AFTER (Post-Opening) | Flight: {after_capture_date or 'Unknown'}"
    badge_w_a = int(font_sm.getlength(a_badge)) + 24
    draw.rectangle([(16, y_after + 12), (16 + badge_w_a, y_after + 36)], fill=(255, 255, 255), outline=(0, 0, 0), width=1)
    draw.text((26, y_after + 16), a_badge, fill=(0, 0, 0), font=font_sm)

    # Footer
    y_foot = total_h - footer_h
    draw.rectangle([(0, y_foot), (w, total_h)], fill=(248, 248, 248), outline=(0, 0, 0), width=1)
    foot_text = "Source: Esri World Imagery Wayback Archive | UCD CERCET Highway Capacity Expansion Project Analysis"
    draw.text((20, y_foot + 8), foot_text, fill=(80, 80, 80), font=font_sm)

    canvas.save(out_path, quality=95)
    return out_path


def generate_slices_html_report(
    project: ProjectRecord,
    slices_data: List[Dict[str, Any]],
    before_meta: Dict[str, Any],
    after_meta: Dict[str, Any],
    output_dir: str,
    overview_img: Optional[str] = None
) -> str:
    """
    Generate a self-contained, interactive HTML comparison report with Before/After curtain wipe sliders.
    Utilitarian, monospaced, white-background layout designed for rapid data-first engineering analysis.
    """
    html_path = os.path.join(output_dir, "index.html")

    cards_html = []
    nav_links = []

    for s in slices_data:
        idx = s["index"]
        b_fn = os.path.basename(s["before_image"])
        a_fn = os.path.basename(s["after_image"])
        st_fn = os.path.basename(s["stacked_image"])
        s_pm_str = f"PM {s['start_pm']:.2f} &rarr; {s['end_pm']:.2f}"
        nav_links.append(f'<a href="#seg-{idx}" class="nav-chip">Seg {idx} ({s_pm_str})</a>')

        sensors_callout = ""
        if s.get("sensors"):
            sensor_rows = "".join(
                f'<tr><td>{html.escape(str(st["id"]))}</td><td>{html.escape(str(st["type"]))}</td><td>PM {st["pm"]}</td><td>{html.escape(str(st["lanes"]))}</td></tr>'
                for st in s["sensors"]
            )
            sensors_callout = f"""
            <table class="sensor-table">
              <thead><tr><th>Sensor ID</th><th>Type</th><th>Postmile</th><th>Lanes</th></tr></thead>
              <tbody>{sensor_rows}</tbody>
            </table>
            """

        bearing_str = f"{s.get('bearing', 0):.0f}&deg;" if s.get('bearing') is not None else ""

        card = f"""
        <div class="segment-section" id="seg-{idx}">
          <div class="segment-header">
            <div class="segment-title-group">
              <span class="seg-idx">[Segment {idx} of {len(slices_data)}]</span>
              <span class="seg-title">{s_pm_str} ({s['length_mi']:.2f} mi)</span>
              {f'<span class="seg-bearing">Heading {bearing_str}</span>' if bearing_str else ''}
            </div>
            <div class="view-toggles" data-seg="{idx}">
              <button class="toggle-btn active" data-view="slider">Curtain Slider</button>
              <button class="toggle-btn" data-view="stacked">Stacked</button>
              <button class="toggle-btn" data-view="split">Side-by-Side</button>
            </div>
          </div>

          {sensors_callout}

          <!-- View: Curtain Wipe Slider -->
          <div class="view-panel panel-slider active" id="panel-slider-{idx}">
            <div class="compare-box" id="compare-box-{idx}">
              <img src="{b_fn}" class="img-base" alt="Before Image" loading="lazy">
              <img src="{a_fn}" class="img-overlay" alt="After Image" loading="lazy">
              <div class="slider-divider">
                <div class="slider-thumb">&#8596;</div>
              </div>
            </div>
            <div class="slider-hint">&lt; Drag slider to wipe between Pre-Construction and Post-Opening imagery &gt;</div>
          </div>

          <!-- View: Vertically Stacked -->
          <div class="view-panel panel-stacked" id="panel-stacked-{idx}">
            <img src="{st_fn}" class="full-stacked-img" alt="Stacked Comparison" loading="lazy">
          </div>

          <!-- View: Side-by-Side -->
          <div class="view-panel panel-split" id="panel-split-{idx}">
            <div class="split-grid">
              <div class="split-col">
                <div class="col-tag">BEFORE: {s['before_capture_date']}</div>
                <img src="{b_fn}" class="split-img" alt="Before" loading="lazy">
              </div>
              <div class="split-col">
                <div class="col-tag">AFTER: {s['after_capture_date']}</div>
                <img src="{a_fn}" class="split-img" alt="After" loading="lazy">
              </div>
            </div>
          </div>

          <div class="segment-footer">
            <span class="meta-note">Resolution: Native Zoom {s.get('zoom', 19)} (~0.3m/px) | Dimensions: {s.get('width', 0)}&times;{s.get('height', 0)} px | <a href="{st_fn}" target="_blank" style="color: #000; text-decoration: underline;">[View Full Stacked PNG]</a></span>
          </div>
        </div>
        """
        cards_html.append(card)

    nav_chips_html = " ".join(nav_links)
    all_cards_html = "\n".join(cards_html)

    overview_html = ""
    if overview_img and os.path.exists(os.path.join(output_dir, overview_img)):
        overview_html = f"""
        <div class="overview-section">
          <h3>Full Project Corridor Overview</h3>
          <img src="{overview_img}" class="overview-img" alt="Project Corridor Overview">
        </div>
        """

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Project {project.project_id} - Roadway Geometry Historical Comparison</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; border-radius: 0 !important; box-shadow: none !important; }}
    body {{
      background: #ffffff;
      color: #000000;
      font-family: "Andale Mono", Menlo, Monaco, "Courier New", Courier, monospace;
      font-size: 13px;
      line-height: 1.45;
      padding-bottom: 60px;
    }}
    header {{
      background: #ffffff;
      border-bottom: 2px solid #000000;
      padding: 14px 20px;
    }}
    .header-content {{
      max-width: 1400px;
      margin: 0 auto;
    }}
    h1 {{
      font-size: 16px;
      font-weight: bold;
      color: #000000;
      margin-bottom: 4px;
    }}
    .subtitle {{
      font-size: 12px;
      color: #333333;
    }}
    .nav-bar {{
      max-width: 1400px;
      margin: 12px auto 0 auto;
      padding: 0 20px;
      display: flex;
      gap: 6px;
      overflow-x: auto;
      font-size: 11px;
    }}
    .nav-chip {{
      background: #ffffff;
      border: 1px solid #000000;
      color: #000000;
      padding: 2px 6px;
      text-decoration: none;
      white-space: nowrap;
    }}
    .nav-chip:hover {{
      background: #000000;
      color: #ffffff;
    }}
    .container {{
      max-width: 1400px;
      margin: 16px auto;
      padding: 0 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      border: 1px solid #000000;
      margin-bottom: 14px;
      background: #ffffff;
      font-size: 12px;
    }}
    th, td {{
      border: 1px solid #000000;
      padding: 5px 8px;
      text-align: left;
    }}
    th {{
      background: #f0f0f0;
      font-weight: bold;
    }}
    .project-desc-box {{
      border: 1px solid #000000;
      padding: 8px 12px;
      margin-bottom: 20px;
      background: #ffffff;
      font-size: 12px;
    }}
    .overview-section {{
      border-bottom: 1px solid #000000;
      padding-bottom: 20px;
      margin-bottom: 24px;
    }}
    .overview-section h3 {{
      font-size: 13px;
      font-weight: bold;
      margin-bottom: 8px;
    }}
    .overview-img {{
      display: block;
      width: 100%;
      height: auto;
      border: 1px solid #000000;
    }}
    .segment-section {{
      border-bottom: 1px solid #000000;
      padding-bottom: 24px;
      margin-bottom: 28px;
    }}
    .segment-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 6px;
      margin-bottom: 8px;
      border-bottom: 1px solid #cccccc;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .segment-title-group {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .seg-idx {{
      font-weight: bold;
    }}
    .seg-title {{
      font-weight: bold;
    }}
    .seg-bearing {{
      font-size: 11px;
      color: #555555;
    }}
    .sensor-table {{
      margin: 8px 0;
      width: auto;
    }}
    .view-toggles {{
      display: flex;
      gap: 4px;
    }}
    .toggle-btn {{
      background: #ffffff;
      border: 1px solid #000000;
      color: #000000;
      padding: 3px 8px;
      font-family: inherit;
      font-size: 11px;
      cursor: pointer;
    }}
    .toggle-btn.active {{
      background: #000000;
      color: #ffffff;
      font-weight: bold;
    }}
    .view-panel {{
      display: none;
      padding: 8px 0;
      background: #ffffff;
    }}
    .view-panel.active {{
      display: block;
    }}
    .compare-box {{
      position: relative;
      width: 100%;
      overflow: hidden;
      border: 1px solid #000000;
      user-select: none;
      cursor: ew-resize;
      background: #ffffff;
      line-height: 0;
    }}
    .img-base {{
      display: block;
      width: 100%;
      height: auto;
      pointer-events: none;
    }}
    .img-overlay {{
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      pointer-events: none;
      clip-path: polygon(0 0, 50% 0, 50% 100%, 0 100%);
    }}
    .slider-divider {{
      position: absolute;
      top: 0;
      bottom: 0;
      left: 50%;
      width: 2px;
      background: #ffffff;
      pointer-events: none;
    }}
    .slider-thumb {{
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      width: 24px;
      height: 24px;
      background: #ffffff;
      color: #000000;
      border: 1px solid #000000;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
      font-weight: bold;
    }}
    .slider-hint {{
      text-align: center;
      font-size: 11px;
      color: #555555;
      margin-top: 6px;
    }}
    .full-stacked-img {{
      display: block;
      width: 100%;
      height: auto;
      border: 1px solid #000000;
    }}
    .split-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .split-col {{
      position: relative;
    }}
    .col-tag {{
      font-size: 11px;
      font-weight: bold;
      margin-bottom: 4px;
    }}
    .split-img {{
      display: block;
      width: 100%;
      height: auto;
      border: 1px solid #000000;
    }}
    .segment-footer {{
      padding: 8px 0;
      font-size: 11px;
      color: #555555;
    }}
  </style>
</head>
<body>

<header>
  <div class="header-content">
    <div class="title-area">
      <h1>[PID {project.project_id}] Route {project.route} {project.direction} Roadway Geometry Comparison</h1>
      <div class="subtitle">{project.county} County (District {project.dist}) | Postmile Range: {project.begin_pm} &rarr; {project.end_pm}</div>
    </div>
  </div>
</header>

<div class="nav-bar">
  {nav_chips_html}
</div>

<div class="container">
  <table class="project-info-table">
    <thead>
      <tr>
        <th>Route &amp; Corridor</th>
        <th>Postmile Range</th>
        <th>Pre-Construction Target</th>
        <th>Post-Opening Target</th>
        <th>Total Slices</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>Route {project.route} {project.direction} ({project.county})</td>
        <td>{project.begin_pm} &rarr; {project.end_pm}</td>
        <td>{project.construction_begin_date or 'N/A'}</td>
        <td>{project.open_to_traffic_date or 'N/A'}</td>
        <td>{len(slices_data)} Segments (Zoom 19)</td>
      </tr>
    </tbody>
  </table>

  {f'<div class="project-desc-box"><strong>Project Description:</strong> {html.escape(project.project_description)}</div>' if project.project_description else ''}

  {overview_html}

  {all_cards_html}
</div>

<script>
  // Interactive Curtain Split Slider logic
  document.querySelectorAll('.compare-box').forEach(box => {{
    const overlay = box.querySelector('.img-overlay');
    const divider = box.querySelector('.slider-divider');
    let isDown = false;

    function updatePos(clientX) {{
      const rect = box.getBoundingClientRect();
      let x = clientX - rect.left;
      if (x < 0) x = 0;
      if (x > rect.width) x = rect.width;
      const pct = (x / rect.width) * 100;
      overlay.style.clipPath = `polygon(0 0, ${{pct.toFixed(2)}}% 0, ${{pct.toFixed(2)}}% 100%, 0 100%)`;
      divider.style.left = `${{pct.toFixed(2)}}%`;
    }}

    box.addEventListener('mousedown', e => {{
      isDown = true;
      updatePos(e.clientX);
    }});

    window.addEventListener('mouseup', () => {{
      isDown = false;
    }});

    window.addEventListener('mousemove', e => {{
      if (!isDown) return;
      updatePos(e.clientX);
    }});

    box.addEventListener('touchstart', e => {{
      isDown = true;
      if (e.touches.length > 0) updatePos(e.touches[0].clientX);
    }}, {{passive: true}});

    window.addEventListener('touchend', () => {{
      isDown = false;
    }});

    window.addEventListener('touchmove', e => {{
      if (!isDown) return;
      if (e.touches.length > 0) updatePos(e.touches[0].clientX);
    }}, {{passive: true}});

    box.addEventListener('dblclick', () => {{
      overlay.style.clipPath = 'polygon(0 0, 50% 0, 50% 100%, 0 100%)';
      divider.style.left = '50%';
    }});
  }});

  // View Mode Switching
  document.querySelectorAll('.view-toggles').forEach(toggleGroup => {{
    const segIdx = toggleGroup.getAttribute('data-seg');
    const btns = toggleGroup.querySelectorAll('.toggle-btn');

    btns.forEach(btn => {{
      btn.addEventListener('click', () => {{
        btns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const viewMode = btn.getAttribute('data-view');

        const section = document.getElementById(`seg-${{segIdx}}`);
        section.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
        const targetPanel = section.querySelector(`.panel-${{viewMode}}`);
        if (targetPanel) targetPanel.classList.add('active');
      }});
    }});
  }});
</script>

</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    return html_path


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
    workers: int = 8,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    Generate a complete high-resolution (Zoom 19) corridor slice comparison suite
    and interactive HTML report for a Caltrans capacity expansion project.
    """
    try:
        import wayback_imagery.core as wayback
    except ImportError:
        sibling_src = os.path.abspath(os.path.join(WORKSPACE_DIR, "..", "..", "..", "..", "wayback_imagery", "src"))
        if os.path.isdir(sibling_src) and sibling_src not in sys.path:
            sys.path.insert(0, sibling_src)
        import wayback_imagery.core as wayback

    # 1. Load project
    projects = load_master_csv(master_csv_path)
    matched = [p for p in projects if str(p.project_id).strip() == str(project_id).strip()]
    if not matched:
        raise ValueError(f"Project ID '{project_id}' not found in master project list.")
    proj = matched[0]

    # 2. Partition corridor into slices
    slices = slice_project_corridor(proj, slice_mi=slice_mi)
    if not slices:
        raise ValueError(f"Could not compute corridor geometry for project {project_id}.")

    # 3. Create dedicated project folder
    safe_route = normalize_route(proj.route)
    safe_dir = proj.pems_direction
    clean_begin = re.sub(r'[^A-Za-z0-9\.]', '', proj.begin_pm)
    clean_end = re.sub(r'[^A-Za-z0-9\.]', '', proj.end_pm)
    folder_name = f"PID_{proj.project_id}_Route{safe_route}_{safe_dir}_PM{clean_begin}-{clean_end}"
    proj_dir = os.path.join(output_dir, folder_name)
    os.makedirs(proj_dir, exist_ok=True)

    if verbose:
        print(f"\n================================================================================")
        print(f"CORRIDOR SLICES GENERATION: Project {proj.project_id} (Route {proj.route} {proj.direction})")
        print(f"================================================================================")
        if proj.project_description:
            wrapped = textwrap.fill(proj.project_description, width=76, initial_indent="  Description:       ", subsequent_indent="                     ")
            print(wrapped)
        print(f"  Postmile Corridor: {proj.begin_pm} -> {proj.end_pm} ({len(slices)} slices of ~{slice_mi} mi)")
        print(f"  Output Directory:  {proj_dir}")
        print(f"  Target Zoom Level: Native Zoom {zoom} (~0.3m/px resolution)")

    # 4. Resolve Target Dates directly from the 3 separate columns (year, month, day)
    d_before = (
        parse_iso_date(custom_before_date, 1, 1)
        or build_date_from_parts(proj.raw_row.get("construction_begin_year"), proj.raw_row.get("construction_begin_month"), proj.raw_row.get("construction_begin_day"), default_month=1, default_day=1)
        or parse_iso_date(proj.construction_begin_date, 1, 1)
        or (datetime.date(proj.start_year, 1, 1) if proj.start_year else None)
    )
    d_after = (
        parse_iso_date(custom_after_date, 12, 31)
        or build_date_from_parts(proj.raw_row.get("open_traffic_year"), proj.raw_row.get("open_traffic_month"), proj.raw_row.get("open_traffic_day"), default_month=12, default_day=31)
        or parse_iso_date(proj.open_to_traffic_date, 12, 31)
        or (datetime.date(proj.end_year, 12, 31) if proj.end_year else None)
    )

    if not d_before or not d_after:
        raise ValueError(f"Project {project_id} requires both construction date and open to traffic date.")

    # 5. Resolve Esri Wayback release and flight capture dates once at corridor center
    builder = ProjectPeMSQueryBuilder(proj)
    s_c, e_c = builder.resolve_geometry()
    lon_c, lat_c = (s_c[1] + e_c[1]) / 2.0, (s_c[0] + e_c[0]) / 2.0

    if verbose:
        print(f"  Querying Esri Wayback archive for capture flights around {d_before} and {d_after}...")

    try:
        c_before = wayback.find_release_by_capture(d_before, lon_c, lat_c, zoom=zoom, mode="before")
    except ValueError:
        c_before = wayback.find_release_by_capture(d_before, lon_c, lat_c, zoom=zoom, mode="nearest")

    try:
        c_after = wayback.find_release_by_capture(d_after, lon_c, lat_c, zoom=zoom, mode="after")
    except ValueError:
        c_after = wayback.find_release_by_capture(d_after, lon_c, lat_c, zoom=zoom, mode="nearest")

    before_rel_id = c_before.release.release_id
    after_rel_id = c_after.release.release_id
    before_cap_str = c_before.capture_date.isoformat() if c_before.capture_date else str(d_before)
    after_cap_str = c_after.capture_date.isoformat() if c_after.capture_date else str(d_after)

    if verbose:
        print(f"  ✓ Resolved BEFORE release {before_rel_id} (Captured: {before_cap_str})")
        print(f"  ✓ Resolved AFTER  release {after_rel_id} (Captured: {after_cap_str})")

    # 6. Map sensors to slices
    sensors_by_slice = {s["index"]: [] for s in slices}
    try:
        vds_meta = load_vds_metadata()
        if vds_meta is not None and not vds_meta.empty and proj.sensor_stations:
            for sid in proj.sensor_stations:
                matches = vds_meta[vds_meta["ID"].astype(str).str.strip() == str(sid).strip()]
                if not matches.empty:
                    s_row = matches.iloc[0]
                    s_pm = s_row.get("State_PM") or s_row.get("Abs_PM")
                    if s_pm and pd.notna(s_pm):
                        _, s_num = split_ca_pm(s_pm)
                        for s_dict in slices:
                            min_spm = min(s_dict["start_pm"], s_dict["end_pm"])
                            max_spm = max(s_dict["start_pm"], s_dict["end_pm"])
                            if min_spm - 0.05 <= s_num <= max_spm + 0.05:
                                sensors_by_slice[s_dict["index"]].append({
                                    "id": str(sid),
                                    "pm": s_num,
                                    "type": str(s_row.get("Type", "ML")),
                                    "lanes": str(s_row.get("Lanes", ""))
                                })
                                break
    except Exception as exc:
        LOGGER.debug(f"Sensor mapping warning: {exc}")

    # 7. Generate each slice
    slices_data = []
    for s in slices:
        idx = s["index"]
        curv_dev = s.get("curvature_dev_m", 0.0)
        lat_shift = s.get("lateral_shift_m", 0.0)
        lat_hspan = s.get("lateral_half_span_m", 0.0)

        # Center bounding box laterally over curved roadway and ensure generous vertical height
        # Default half_width_m is 110.0m to guarantee adequate vertical height (not too wide/narrow)
        # and prevent road clipping on curves, ramps, shoulders, and multi-lane freeways.
        slice_half_width = max(half_width_m, lat_hspan + 85.0)

        # Apply lateral shift to start and end coordinates so corridor box is centered on the road curve
        s_coords = s["start_coords"]
        e_coords = s["end_coords"]
        if abs(lat_shift) > 1.5:
            lat0, lon0 = s_coords
            lat1, lon1 = e_coords
            mid_lat = (lat0 + lat1) / 2.0
            m_per_deg_lat = 111320.0
            m_per_deg_lon = 111320.0 * math.cos(math.radians(mid_lat))
            vx = (lon1 - lon0) * m_per_deg_lon
            vy = (lat1 - lat0) * m_per_deg_lat
            chord_len = math.hypot(vx, vy)
            if chord_len > 1e-3:
                nx = -vy / chord_len
                ny = vx / chord_len
                dlon_shift = (lat_shift * nx) / m_per_deg_lon
                dlat_shift = (lat_shift * ny) / m_per_deg_lat
                s_coords = (lat0 + dlat_shift, lon0 + dlon_shift)
                e_coords = (lat1 + dlat_shift, lon1 + dlon_shift)

        if verbose:
            curv_note = f" [curve dev: {curv_dev:.1f}m, shift: {lat_shift:.1f}m -> half-width: {slice_half_width:.1f}m]" if curv_dev > 5.0 else ""
            print(f"\n  [Slice {idx}/{len(slices)}] PM {s['start_pm']:.2f} -> {s['end_pm']:.2f} ({s['length_mi']:.2f} mi){curv_note}...")

        geom = wayback.corridor_geometry(
            s_coords,
            e_coords,
            half_width_m=slice_half_width,
            pad_m=40.0
        )
        prefix = f"slice_{idx:02d}_PM{s['start_pm']:.2f}-{s['end_pm']:.2f}"

        # Raw Before Strip
        b_res = wayback.generate_image(
            bbox=geom.envelope,
            date=d_before,
            label=f"PID {proj.project_id} BEFORE | PM {s['start_pm']:.2f}-{s['end_pm']:.2f}",
            out_dir=proj_dir,
            corridor=geom,
            zoom=zoom,
            release_id=before_rel_id,
            workers=workers,
        )
        b_dest = os.path.join(proj_dir, f"{prefix}_before.png")
        if os.path.exists(b_res["path"]) and b_res["path"] != b_dest:
            shutil.move(b_res["path"], b_dest)

        # Raw After Strip
        a_res = wayback.generate_image(
            bbox=geom.envelope,
            date=d_after,
            label=f"PID {proj.project_id} AFTER | PM {s['start_pm']:.2f}-{s['end_pm']:.2f}",
            out_dir=proj_dir,
            corridor=geom,
            zoom=zoom,
            release_id=after_rel_id,
            workers=workers,
        )
        a_dest = os.path.join(proj_dir, f"{prefix}_after.png")
        if os.path.exists(a_res["path"]) and a_res["path"] != a_dest:
            shutil.move(a_res["path"], a_dest)

        # Stacked Comparison Image
        stacked_dest = os.path.join(proj_dir, f"{prefix}_stacked.png")
        create_stacked_slice_image(
            before_img_path=b_dest,
            after_img_path=a_dest,
            out_path=stacked_dest,
            project=proj,
            slice_info=s,
            before_capture_date=before_cap_str,
            after_capture_date=after_cap_str,
            zoom=zoom,
        )

        slice_rec = {
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
            "curvature_dev_m": curv_dev,
            "width": b_res["width"],
            "height": b_res["height"],
            "sensors": sensors_by_slice.get(idx, [])
        }
        slices_data.append(slice_rec)

        if verbose:
            print(f"    ✓ Generated {prefix}_stacked.png ({b_res['width']}x{b_res['height']} px)")

    # 8. Generate Full Corridor Overview Image (Macro view at Z15)
    overview_fn = "00_overview_corridor.png"
    overview_path = os.path.join(proj_dir, overview_fn)
    try:
        ov_bbox = wayback.bbox_from_segment(s_c, e_c, margin_m=200.0)
        ov_res = wayback.generate_image(
            bbox=ov_bbox,
            date=d_after,
            label=f"PROJECT {proj.project_id} FULL OVERVIEW | ROUTE {proj.route} {proj.direction} PM {proj.begin_pm}-{proj.end_pm}",
            out_dir=proj_dir,
            max_px=1600,
            max_zoom=16,
            release_id=after_rel_id,
            points=[s_c, e_c],
            workers=workers,
        )
        if os.path.exists(ov_res["path"]) and ov_res["path"] != overview_path:
            shutil.move(ov_res["path"], overview_path)
    except Exception as exc:
        LOGGER.debug(f"Overview generation failed: {exc}")
        overview_fn = None

    # 9. Generate Interactive HTML Report
    html_report = generate_slices_html_report(
        project=proj,
        slices_data=slices_data,
        before_meta=c_before.as_dict(),
        after_meta=c_after.as_dict(),
        output_dir=proj_dir,
        overview_img=overview_fn
    )

    # 10. Save metadata JSON
    meta_json_path = os.path.join(proj_dir, "metadata.json")
    with open(meta_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "project_id": proj.project_id,
            "project_description": proj.project_description,
            "route": proj.route,
            "direction": proj.direction,
            "county": proj.county,
            "postmiles": (proj.begin_pm, proj.end_pm),
            "before_flight": c_before.as_dict(),
            "after_flight": c_after.as_dict(),
            "slices": [{k: v for k, v in s.items() if not k.endswith("_image")} for s in slices_data],
        }, f, indent=2)

    if verbose:
        print(f"\n================================================================================")
        print(f"✓ Corridor slices complete! Saved {len(slices_data)} high-res segments to:")
        print(f"  {proj_dir}")
        print(f"✓ Interactive HTML comparison report available at:")
        print(f"  {html_report}")
        print(f"================================================================================\n")

    if open_browser:
        webbrowser.open("file://" + os.path.abspath(html_report))

    return {
        "project_id": proj.project_id,
        "folder": proj_dir,
        "slices": slices_data,
        "report_html": html_report,
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
    workers: int = 8,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    Download and stitch historical aerial/satellite imagery from Esri World Imagery Wayback
    for a Caltrans highway project, derived from its postmile range and construction timeline.

    Parameters:
      project_id: Project ID matching a row in the master project spreadsheet.
      period: 'before' (pre-construction), 'after' (post-opening), 'both' (both before & after),
              or a specific ISO date string 'YYYY-MM-DD'.
      aligned: If True, uses corridor affine transformation to rotate the highway horizontally
               with an integrated north arrow.
      margin_m: Margin in meters added around the highway segment.
      max_px: Upper bound for image dimension in pixels (default 4096).
      max_zoom: Maximum zoom level (default 19, ~0.3m per pixel).
      zoom: Explicit zoom level override (1-19).
      output_dir: Local directory where annotated PNG images will be saved.
      master_csv_path: Path to master projects CSV. Defaults to DEFAULT_MASTER_CSV.
      custom_date: Explicit date override when querying a single date.
      custom_before_date: Custom date string for 'before' (overrides spreadsheet).
      custom_after_date: Custom date string for 'after' (overrides spreadsheet).
      corridor_slices: If True, slices corridor into high-resolution (Zoom 19) segments and generates an interactive HTML viewer.
      slice_mi: Length in miles of each corridor slice (default 0.25 mi).
      open_browser: Whether to automatically launch the interactive HTML report in browser.
      workers: Number of parallel tile download threads.
      verbose: Whether to print progress to stdout.

    Returns:
      Dict with project metadata, resolved bounding box, and metadata of generated image(s).
    """
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
            verbose=verbose,
        )

    try:
        import wayback_imagery.core as wayback
    except ImportError:
        # Check sibling repository directory if not installed in site-packages
        sibling_src = os.path.abspath(os.path.join(WORKSPACE_DIR, "..", "..", "..", "..", "wayback_imagery", "src"))
        if os.path.isdir(sibling_src) and sibling_src not in sys.path:
            sys.path.insert(0, sibling_src)
        try:
            import wayback_imagery.core as wayback
        except ImportError as exc:
            raise ImportError(
                "wayback-imagery is required to download historical satellite imagery.\n"
                "Please install it via:\n"
                "  pip install pillow>=10.1\n"
                "  pip install git+https://github.com/erbarwick/wayback-imagery.git\n"
                "or for local editable installation: pip install -e /path/to/wayback_imagery"
            ) from exc

    # 1. Load project record from CSV
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
            wrapped = textwrap.fill(proj.project_description, width=68, initial_indent="  Description:        ", subsequent_indent="                      ")
            print(wrapped)
        print(f"  Route & Corridor:   Route {proj.route} {proj.direction} ({proj.county} PM {proj.begin_pm} -> {proj.end_pm})")
        print(f"  Timeline Target:    Const: {proj.construction_begin_date or proj.start_year} | Open: {proj.open_to_traffic_date or proj.end_year}")

    # 2. Resolve coordinates from postmiles
    builder = ProjectPeMSQueryBuilder(proj)
    start_pt, end_pt = builder.resolve_geometry()
    if not start_pt or not end_pt:
        polyline = get_project_corridor_geometry(proj)
        if len(polyline) >= 2:
            start_pt, end_pt = polyline[0], polyline[-1]
    if not start_pt or not end_pt:
        raise ValueError(
            f"Could not resolve geographic coordinates from postmile range "
            f"({proj.begin_pm} to {proj.end_pm}) for project {proj.project_id} on Route {proj.route}."
        )

    # 3. Calculate Bounding Box or Aligned Corridor
    corridor = None
    if aligned:
        polyline = get_project_corridor_geometry(proj)
        curv_dev = calculate_corridor_curvature_deviation(start_pt, end_pt, polyline) if len(polyline) >= 2 else 0.0
        corridor_half_width = max(75.0, curv_dev + 65.0)
        corridor = wayback.corridor_geometry(start_pt, end_pt, half_width_m=corridor_half_width, pad_m=max(45.0, margin_m * 0.5))
        bbox = corridor.envelope
    else:
        polyline = get_project_corridor_geometry(proj)
        if len(polyline) >= 4:
            lats = [p[0] for p in polyline]
            lons = [p[1] for p in polyline]
            mid_lat = (min(lats) + max(lats)) / 2.0
            m_per_deg_lat = 111320.0
            m_per_deg_lon = 111320.0 * math.cos(math.radians(mid_lat))
            pad_x = max(margin_m, 0.1 * (max(lons) - min(lons)) * m_per_deg_lon) / m_per_deg_lon
            pad_y = max(margin_m, 0.1 * (max(lats) - min(lats)) * m_per_deg_lat) / m_per_deg_lat
            bbox = (min(lons) - pad_x, min(lats) - pad_y, max(lons) + pad_x, max(lats) + pad_y)
        else:
            bbox = wayback.bbox_from_segment(start_pt, end_pt, margin_m=margin_m, margin_frac=0.25)

    # 4. Determine Target Dates & Selection Modes
    target_tasks = []
    if custom_date:
        d = parse_iso_date(custom_date)
        if not d:
            raise ValueError(f"Invalid custom date '{custom_date}'. Please use YYYY-MM-DD.")
        target_tasks.append(("custom", d, "nearest"))
    elif period == "before":
        d = (
            parse_iso_date(custom_before_date, 1, 1)
            or build_date_from_parts(proj.raw_row.get("construction_begin_year"), proj.raw_row.get("construction_begin_month"), proj.raw_row.get("construction_begin_day"), default_month=1, default_day=1)
            or parse_iso_date(proj.construction_begin_date, 1, 1)
            or (datetime.date(proj.start_year, 1, 1) if proj.start_year else None)
        )
        if not d:
            raise ValueError(f"Project {project_id} has no Construction Begin Date in spreadsheet. Specify --imagery-date.")
        target_tasks.append(("before", d, "before"))
    elif period == "after":
        d = (
            parse_iso_date(custom_after_date, 12, 31)
            or build_date_from_parts(proj.raw_row.get("open_traffic_year"), proj.raw_row.get("open_traffic_month"), proj.raw_row.get("open_traffic_day"), default_month=12, default_day=31)
            or parse_iso_date(proj.open_to_traffic_date, 12, 31)
            or (datetime.date(proj.end_year, 12, 31) if proj.end_year else None)
        )
        if not d:
            raise ValueError(f"Project {project_id} has no Open to Traffic Date in spreadsheet. Specify --imagery-date.")
        target_tasks.append(("after", d, "after"))
    elif period in ("both", "before_after", "before-after"):
        d_before = (
            parse_iso_date(custom_before_date, 1, 1)
            or build_date_from_parts(proj.raw_row.get("construction_begin_year"), proj.raw_row.get("construction_begin_month"), proj.raw_row.get("construction_begin_day"), default_month=1, default_day=1)
            or parse_iso_date(proj.construction_begin_date, 1, 1)
            or (datetime.date(proj.start_year, 1, 1) if proj.start_year else None)
        )
        d_after = (
            parse_iso_date(custom_after_date, 12, 31)
            or build_date_from_parts(proj.raw_row.get("open_traffic_year"), proj.raw_row.get("open_traffic_month"), proj.raw_row.get("open_traffic_day"), default_month=12, default_day=31)
            or parse_iso_date(proj.open_to_traffic_date, 12, 31)
            or (datetime.date(proj.end_year, 12, 31) if proj.end_year else None)
        )
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
        d = _parse_project_date(period)
        if not d:
            raise ValueError(f"Unknown period '{period}'. Use 'before', 'after', 'both', or 'YYYY-MM-DD'.")
        target_tasks.append(("custom", d, "nearest"))

    # 5. Download and stitch imagery
    results = {}
    for kind, target_date, mode in target_tasks:
        label = f"PID {proj.project_id} ({kind.upper()}) - Route {proj.route} {proj.direction} PM {proj.begin_pm}-{proj.end_pm}"
        if verbose:
            print(f"\n[Wayback] Fetching {kind.upper()} imagery for Project {proj.project_id} (target date: {target_date}, mode: {mode})...")

        try:
            img_meta = wayback.generate_image(
                bbox=bbox,
                date=target_date,
                label=label,
                out_dir=output_dir,
                zoom=zoom,
                max_px=max_px,
                max_zoom=max_zoom,
                mode=mode,
                select_by="capture",
                points=[start_pt, end_pt] if not aligned else None,
                workers=workers,
                corridor=corridor,
            )
        except ValueError as exc:
            if "No capture date" in str(exc) and mode in ("before", "after"):
                if verbose:
                    print(f"  Note: {exc} - Falling back to nearest available capture...")
                img_meta = wayback.generate_image(
                    bbox=bbox,
                    date=target_date,
                    label=label,
                    out_dir=output_dir,
                    zoom=zoom,
                    max_px=max_px,
                    max_zoom=max_zoom,
                    mode="nearest",
                    select_by="capture",
                    points=[start_pt, end_pt] if not aligned else None,
                    workers=workers,
                    corridor=corridor,
                )
            else:
                raise exc

        results[kind] = img_meta
        if verbose:
            cap_d = img_meta.get("capture_date")
            rel_d = img_meta.get("release_date")
            zm = img_meta.get("zoom")
            p = img_meta.get("path")
            print(f"✓ Saved {kind.upper()} image -> {p} (Captured: {cap_d}, Release: {rel_d}, Zoom: {zm})")

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
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
  python pems_interface.py -r 101 -d NB --coords '38°11'10.19"N 122°36'05.91"W' '38°13'14.99"N 122°36'27.10"W'
  python pems_interface.py -r 101 -d NB --coords '38.186, -122.601' '38.220, -122.607' --lanes
  python pems_interface.py -r 101 -d NB --coords '38.186, -122.601' '38.220, -122.607' --map

  # 4. Bidirectional Postmile Conversions:
  # (a) Convert California County PM + County -> Absolute Postmile & Coordinates:
  python pems_interface.py -r 101 -d NB -c Sonoma --pm 0.21
  python pems_interface.py -r 101 -d NB -c "Santa Clara" --pm 50.56

  # (b) Convert continuous Absolute Postmile -> California County PM & Coordinates:
  python pems_interface.py -r 101 -d NB --pm 400.2 --absolute
  # (or simply omit --county): python pems_interface.py -r 101 -d NB --pm 400.2

  # 5. Query and map custom postmile corridor (County PM or Absolute PM):
  # By County Postmiles:
  python pems_interface.py -r 101 -d NB -c Sonoma --begin-pm 0.0 --end-pm 4.5
  python pems_interface.py -r 101 -d NB -c Sonoma --begin-pm 0.0 --end-pm 4.5 --map
  python pems_interface.py -r 101 -d NB -c Sonoma --begin-pm 0.0 --end-pm 4.5 --lanes
  # By Absolute Postmiles across counties:
  python pems_interface.py -r 101 -d NB --begin-pm 400.2 --end-pm 424.0 --absolute --map

  # 6. Cross-county postmile corridor query & map (e.g. Marin to Sonoma):
  python pems_interface.py -r 101 -d NB --begin-county Marin --begin-pm 25.0 --end-county Sonoma --end-pm 2.0 --map

  # 7. Generate high-resolution (Zoom 19) corridor slices & curtain slider HTML report:
  python pems_interface.py -p 400000680 --slices

  # 8. Download historical satellite before/after imagery:
  python pems_interface.py -p 400000680 --imagery --aligned

  # 9. Generate interactive Folium map for a project or all projects statewide:
  python pems_interface.py -p 400000680 --map
  python pems_interface.py --map-all --dist 4 --include-stations

  # 10. Download PeMS detector time-series data for a project across all available years:
  python pems_interface.py -p 400000680 --download --granularity month

  # 11. Audit spreadsheet sensors against active PeMS VDS detectors statewide:
  python pems_interface.py --audit --dist 4
  python pems_interface.py --audit --apply

  # 12. Automatically infer Open-to-Traffic date from lane history & telemetry:
  python pems_interface.py -p 300020420 --find-open-date
        """
    )

    # 1. Primary Actions & Target
    act_group = parser.add_argument_group("Primary Actions & Target")
    act_group.add_argument(
        "--project", "-p", "--inspect",
        dest="project",
        metavar="PID",
        help="Target Project ID. Default action is project inspection. Can also be combined with --slices, --imagery, --map, or --download."
    )
    act_group.add_argument(
        "--slices", "--corridor-slices",
        dest="slices",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Generate high-resolution (Zoom 19) corridor slices with curtain-slider HTML comparison report (e.g. --slices 400000680 or -p 400000680 --slices)."
    )
    act_group.add_argument(
        "--imagery",
        dest="imagery",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Download historical satellite imagery before and after construction (e.g. --imagery 400000680 or -p 400000680 --imagery)."
    )
    act_group.add_argument(
        "--map",
        dest="map",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Generate interactive HTML map for a project, coordinate range, or postmile corridor (e.g. --map 400000680, or combined with --coords or --begin-pm/--end-pm)."
    )
    act_group.add_argument(
        "--map-all",
        action="store_true",
        help="Generate interactive HTML map for all projects statewide (or filtered by --dist)."
    )
    act_group.add_argument(
        "--download", "--download-project",
        dest="download_project",
        nargs="?",
        const=True,
        default=False,
        metavar="PID",
        help="Download PeMS detector time-series data for all stations in a project across all years."
    )
    act_group.add_argument(
        "--station", "-s", "--download-station",
        dest="download_station",
        metavar="STATION_ID",
        help="Download detector time-series data from PeMS for a specific station ID across all years."
    )
    act_group.add_argument(
        "--audit", "--match-stations",
        dest="match_stations",
        action="store_true",
        help="Audit and match active PeMS VDS stations along corridors against spreadsheet sensors."
    )
    act_group.add_argument(
        "--find-open-date", "--open-date",
        dest="find_open_date",
        action="store_true",
        help="Automatically infer Open-to-Traffic date from PeMS lane configuration & detector telemetry (Option 1)."
    )
    act_group.add_argument(
        "--no-telemetry",
        dest="check_telemetry",
        action="store_false",
        default=True,
        help="Disable daily flow telemetry corroboration during open-to-traffic date inference."
    )
    act_group.add_argument(
        "--lane-config", "--lanes", "--show-lanes",
        dest="lane_config",
        nargs="?",
        const=True,
        default=False,
        metavar="PID_OR_STATION",
        help="Fetch and display raw PeMS station lane tables and Caltrans TSN roadway metrics for a project ID, station ID, coordinate range, or postmile range (e.g. -p <PID> --lanes, -s <SID> --lanes, or -r 101 -c Sonoma --begin-pm 0.0 --end-pm 4.5 --lanes)."
    )
    act_group.add_argument(
        "--list-urls",
        action="store_true",
        help="List generated PeMS URLs for projects."
    )
    act_group.add_argument(
        "--test-login",
        action="store_true",
        help="Test PeMS portal login credentials."
    )
    act_group.add_argument(
        "--hpms",
        action="store_true",
        help="Cross-reference and validate project corridor with FHWA HPMS ground-truth lane counts and AADT."
    )

    # 2. Imagery & Corridor Slices Options
    img_group = parser.add_argument_group("Imagery & Corridor Slices Options")
    img_group.add_argument(
        "--period", "--imagery-period",
        dest="imagery_period",
        default="both",
        choices=["before", "after", "both"],
        help="Imagery period relative to project construction: 'before', 'after', or 'both' (default: 'both')."
    )
    img_group.add_argument(
        "--date", "--imagery-date",
        dest="imagery_date",
        help="Specific custom date (YYYY-MM-DD) for imagery retrieval."
    )
    img_group.add_argument(
        "--before-date",
        help="Specific custom before date (YYYY-MM-DD or M/D/YY) for imagery retrieval or corridor slices."
    )
    img_group.add_argument(
        "--after-date",
        help="Specific custom after date (YYYY-MM-DD or M/D/YY) for imagery retrieval or corridor slices."
    )
    img_group.add_argument(
        "--zoom", "--imagery-zoom",
        dest="imagery_zoom",
        type=int,
        default=None,
        help="Force specific zoom level (1-19) for satellite imagery."
    )
    img_group.add_argument(
        "--slice-mi",
        type=float,
        default=0.25,
        help="Corridor slice segment length in miles for --slices (default: 0.25 mi)."
    )
    img_group.add_argument(
        "--half-width", "--imagery-half-width",
        dest="imagery_half_width",
        type=float,
        default=110.0,
        help="Corridor slice half-width in meters perpendicular to roadway (default: 110m, auto-widened for curves and freeway geometry)."
    )
    img_group.add_argument(
        "--aligned",
        action="store_true",
        help="Rotate and crop single-frame imagery to an aligned highway corridor strip."
    )
    img_group.add_argument(
        "--margin", "--imagery-margin",
        dest="imagery_margin",
        type=float,
        default=150.0,
        help="Margin around highway segment in meters (default: 150m)."
    )

    # 3. PeMS Data Download Options
    dl_group = parser.add_argument_group("PeMS Data Download Options")
    dl_group.add_argument(
        "--data-type",
        default="flow",
        choices=["flow", "occ", "speed", "vmt", "vht", "delay", "aadt"],
        help="Data quantity to download from PeMS (default: 'flow')."
    )
    dl_group.add_argument(
        "--granularity",
        default="month",
        choices=["month", "day", "hour", "week", "year", "5min"],
        help="Time granularity for detector time-series data (default: 'month')."
    )
    dl_group.add_argument(
        "--format", "-f", "--export-format",
        dest="export_format",
        default="text",
        choices=["text", "xls", "xlsx"],
        help="Export format for downloaded PeMS files: 'text' (default) or 'xls'/'xlsx'."
    )
    dl_group.add_argument(
        "--start-year",
        type=int,
        default=1999,
        help="Earliest year to query (default: 1999)."
    )
    dl_group.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="Latest year to query (default: current calendar year)."
    )
    dl_group.add_argument(
        "--no-combine",
        action="store_true",
        help="Do not create a consolidated master CSV file across all queried years."
    )
    dl_group.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download and overwrite existing files even if already on disk."
    )

    # 4. Interactive Mapping Options
    map_group = parser.add_argument_group("Interactive Mapping Options")
    map_group.add_argument(
        "--output-map", "-o",
        dest="output_map",
        help="Custom output path for generated HTML map."
    )
    map_group.add_argument(
        "--dist",
        help="Filter --map-all or --audit to a specific Caltrans District (e.g. '4')."
    )
    map_group.add_argument(
        "--no-stations",
        action="store_true",
        help="Do not include PeMS VDS station detector layers on single-project map."
    )
    map_group.add_argument(
        "--include-stations",
        action="store_true",
        help="Include PeMS VDS station detector layers on multi-project map (--map-all)."
    )
    map_group.add_argument(
        "--station-buffer",
        type=float,
        default=3.0,
        help="Corridor postmile buffer in miles for nearby stations on the map (default: 3.0 mi)."
    )
    map_group.add_argument(
        "--map-station-types",
        default=None,
        help="Comma-separated station types to include on the map (default: all types partitioned by layer)."
    )

    # 5. Corridor Audit & VDS Matching Options
    audit_group = parser.add_argument_group("Corridor Audit & VDS Matching Options")
    audit_group.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Perform station comparison in dry-run mode without modifying CSV (default: True)."
    )
    audit_group.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates to master CSV (disables dry-run; creates automated backup)."
    )
    audit_group.add_argument(
        "--output-report",
        help="Save comparison report to CSV or Markdown file."
    )
    audit_group.add_argument(
        "--output-csv",
        help="Custom output path for updated master CSV when using --apply."
    )
    audit_group.add_argument(
        "--station-types",
        default="ML,HV",
        help="Comma-separated station types to include for matching (default: 'ML,HV'). Use 'ALL' for all types."
    )
    audit_group.add_argument(
        "--buffer",
        type=float,
        default=0.1,
        help="Postmile buffer tolerance in miles (default: 0.1)."
    )
    audit_group.add_argument(
        "--metadata-dir",
        help="Custom directory containing PeMS metadata text files."
    )

    # 6. Ad-Hoc Corridor Query & Postmile Conversion Options
    adhoc_group = parser.add_argument_group("Ad-Hoc Corridor Query & Postmile Conversion Options")
    adhoc_group.add_argument(
        "--route", "-r",
        help="Freeway / highway route number (e.g. 101, 80, 5)."
    )
    adhoc_group.add_argument(
        "--dir", "-d",
        help="Freeway direction (NB, SB, EB, WB, N, S, E, W)."
    )
    adhoc_group.add_argument(
        "--pm",
        help="Single postmile to convert and resolve coordinates. Converts California County PM -> Absolute PM if --county is provided, or Absolute PM -> California County PM if --absolute is passed or --county is omitted (e.g. -r 101 --pm 400.2 --absolute or -r 101 -c Sonoma --pm 0.21)."
    )
    adhoc_group.add_argument(
        "--begin-pm",
        help="Begin postmile for corridor query (California County PM or continuous Absolute PM)."
    )
    adhoc_group.add_argument(
        "--end-pm",
        help="End postmile for corridor query (California County PM or continuous Absolute PM)."
    )
    adhoc_group.add_argument(
        "--absolute", "--abs",
        dest="absolute",
        action="store_true",
        help="Treat postmile input (--pm or --begin-pm/--end-pm) as continuous Absolute Postmiles (Abs PM). Enables direct Absolute PM -> California County PM conversion and continuous corridor querying across county lines without specifying a county."
    )
    adhoc_group.add_argument(
        "--county", "-c",
        help="County name or standard abbreviation for ad-hoc query (e.g. Sonoma, Placer, 'Contra Costa', SON, PLA, MRN, CC)."
    )
    adhoc_group.add_argument(
        "--begin-county",
        help="County for begin postmile if corridor traverses county boundaries (e.g. --begin-county Marin --begin-pm 25.0)."
    )
    adhoc_group.add_argument(
        "--end-county",
        help="County for end postmile if corridor traverses county boundaries (e.g. --end-county Sonoma --end-pm 2.0)."
    )
    adhoc_group.add_argument(
        "--coords", "--coordinate-range",
        dest="coords",
        nargs="+",
        metavar="COORD",
        help="Search for detector stations along a route corridor by start/end coordinates. Snaps onto the route centerline to establish corridor limits. Accepts decimal degrees or Google Maps DMS (e.g. -r 101 --coords '38.186, -122.601' '38.220, -122.607' or in DMS '38°11\'10\"N 122°36\'05\"W' '38°13\'14\"N 122°36\'27\"W'). Combine with --lanes or --map."
    )
    adhoc_group.add_argument(
        "--start-coords",
        dest="start_coords",
        help="Start coordinate for corridor station search in decimal or DMS (e.g. '38.186, -122.601')."
    )
    adhoc_group.add_argument(
        "--end-coords",
        dest="end_coords",
        help="End coordinate for corridor station search in decimal or DMS (e.g. '38.220, -122.607')."
    )
    adhoc_group.add_argument(
        "--strict-route",
        action="store_true",
        help="Do not auto-switch highway route if input coordinates are distant from specified route."
    )
    adhoc_group.add_argument("--census", action="store_true", help="Search closest Census stations.")

    # 7. General & Configuration
    gen_group = parser.add_argument_group("General & Configuration")
    gen_group.add_argument(
        "--input", "-i",
        default=DEFAULT_MASTER_CSV,
        help="Path to cleaned Master CSV (default: staging/Master_CA_Project_List_Cleaned.csv)."
    )
    gen_group.add_argument(
        "--output-dir",
        default=".",
        help="Directory to save downloaded files or imagery (default: '.')."
    )
    gen_group.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP network timeout in seconds for PeMS requests (default: 60s, auto-retried up to 90s)."
    )
    gen_group.add_argument(
        "--no-open",
        action="store_true",
        help="Do not automatically open generated HTML maps or slice reports in browser."
    )

    return parser


def main(argv: Optional[List[str]] = None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # 1. Credentials check
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

    # 2. Ad-hoc Coordinate Range Search (Search stations along route by coordinates)
    has_coord_query = bool(args.coords or (args.start_coords and args.end_coords))
    if has_coord_query:
        c1_str, c2_str = None, None
        if args.start_coords and args.end_coords:
            c1_str, c2_str = args.start_coords, args.end_coords
        elif args.coords:
            if len(args.coords) == 1:
                parts = re.split(r"\s+to\s+|;\s*", args.coords[0], flags=re.IGNORECASE)
                if len(parts) >= 2:
                    c1_str, c2_str = parts[0], parts[1]
                else:
                    print("Error: Please provide two coordinates separated by space or 'to' (e.g. --coords '38.186, -122.601' '38.220, -122.607').")
                    sys.exit(1)
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
                    if len(matches) >= 2:
                        c1_str, c2_str = matches[0].group(0), matches[1].group(0)
                    else:
                        print("Error: Unable to parse two coordinates from --coords input. Please quote them (e.g. --coords '38.186, -122.601' '38.220, -122.607').")
                        sys.exit(1)

        c_start = parse_coordinate_string(c1_str)
        c_end = parse_coordinate_string(c2_str)
        if not c_start or not c_end:
            print(f"Error: Could not parse coordinates. Start: '{c1_str}', End: '{c2_str}'. Expected decimal degrees or DMS.")
            sys.exit(1)

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

        s_snap = res["snapped_start"]
        e_snap = res["snapped_end"]
        c_range = res["corridor_range"]
        adhoc_pid = f"CORRIDOR-R{res['route']}"
        dist_val = str(s_snap.get("district") or ("4" if str(s_snap.get("county")).lower() == "sonoma" else "1"))
        adhoc_proj = ProjectRecord(
            project_id=adhoc_pid,
            dist=dist_val,
            county=str(s_snap['county']),
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
            project_description=f"Corridor: Route {res['route']} from {s_snap['county']} PM {s_snap['ca_pm']} to {e_snap['county']} PM {e_snap['ca_pm']}"
        )

        if args.lane_config:
            client = PeMSClient()
            client.login()
            meta = load_vds_metadata(args.metadata_dir)
            st_details = [
                fetch_station_lane_configuration(s.id, client=client, vds_record=s, timeout=getattr(args, "timeout", 60.0))
                for s in res["stations"]
            ]
            report = format_lane_config_report(
                st_details,
                project=adhoc_proj,
                hpms_summary=res.get("hpms_summary"),
                uninstrumented_info={
                    "coverage_status": res.get("coverage_status"),
                    "coverage_details": res.get("coverage_details"),
                    "nearest_station": res.get("nearest_station"),
                    "census_stations": res.get("census_stations"),
                }
            )
            print(report)
            if args.output_report:
                _save_lane_config_report(args.output_report, st_details, project=adhoc_proj, hpms_summary=res.get("hpms_summary"))
        else:
            report = format_coordinate_search_report(res)
            print(report)
            if args.output_report:
                with open(args.output_report, "w", encoding="utf-8") as f:
                    f.write(report)
                print(f"\n✓ Exported search report -> {args.output_report}")

        if args.map or args.output_map:
            s_snap = res["snapped_start"]
            e_snap = res["snapped_end"]
            c_range = res["corridor_range"]
            adhoc_pid = f"CORRIDOR-R{res['route']}"
            dist_val = str(s_snap.get("district") or ("4" if str(s_snap.get("county")).lower() == "sonoma" else "1"))
            adhoc_proj = ProjectRecord(
                project_id=adhoc_pid,
                dist=dist_val,
                county=str(s_snap['county']),
                state="CA",
                route=str(res["route"]),
                direction=res["direction"] or "NB",
                begin_pm=str(s_snap["ca_pm"]),
                end_pm=str(e_snap["ca_pm"]),
                abs_begin_pm=c_range["abs_min"],
                abs_end_pm=c_range["abs_max"],
                start_coords=res["input_start"],
                end_coords=res["input_end"],
                sensor_stations=[str(s.id) for s in res["stations"]],
                project_description=f"Ad-hoc Corridor Search: Route {res['route']} from {s_snap['county']} PM {s_snap['ca_pm']} to {e_snap['county']} PM {e_snap['ca_pm']}"
            )
            out_p = args.output_map or f"corridor_route_{res['route']}_stations.html"
            build_single_project_map(
                target_pid=adhoc_pid,
                projects=[adhoc_proj],
                output_path=out_p,
                open_browser=not args.no_open,
                include_stations=not args.no_stations,
                station_buffer_mi=args.buffer,
                metadata_dir=args.metadata_dir,
            )
            print(f"✓ Generated interactive corridor map -> {out_p}")

        return

    # 3. Ad-hoc Postmile Query / Conversion / Mapping
    if args.pm or (args.begin_pm and not args.end_pm):
        target_pm = args.pm or args.begin_pm
        if not args.route:
            print("Error: Postmile conversion requires a highway route (e.g. -r 101 -c Sonoma --pm 0.21 or -r 101 --pm 400.2 --absolute).")
            sys.exit(1)
        
        is_abs = getattr(args, "absolute", False) or (not args.county and not args.begin_county)
        if is_abs:
            res = convert_absolute_postmile(
                route=args.route,
                abs_pm=target_pm,
                direction=args.dir,
            )
        else:
            target_co = args.county or args.begin_county
            res = convert_california_postmile(
                route=args.route,
                county=target_co,
                ca_pm=target_pm,
                direction=args.dir,
            )
        report = format_single_postmile_report(res)
        print(report)
        if args.output_report:
            with open(args.output_report, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"\n✓ Exported conversion report -> {args.output_report}")
        return

    if args.begin_pm is not None and args.end_pm is not None:
        if not args.route:
            print("Error: Corridor query requires a highway route (e.g. -r 101 -c Sonoma --begin-pm 0.0 --end-pm 4.5).")
            sys.exit(1)
        
        is_abs = getattr(args, "absolute", False) or (not args.county and not args.begin_county and not args.end_county)
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

        adhoc_pid = f"CORRIDOR-R{res['route']}-PM{res['begin_pm']}-{res['end_pm']}"
        dist_val = str(res["start"].get("district") or res["end"].get("district") or "4")
        adhoc_proj = ProjectRecord(
            project_id=adhoc_pid,
            dist=dist_val,
            county=str(res["begin_county"]),
            state="CA",
            route=str(res["route"]),
            direction=res["direction"] or "NB",
            begin_pm=str(res["begin_pm"]),
            end_pm=str(res["end_pm"]),
            abs_begin_pm=res["corridor_range"]["abs_min"],
            abs_end_pm=res["corridor_range"]["abs_max"],
            start_coords=res["start"]["coord"],
            end_coords=res["end"]["coord"],
            sensor_stations=[str(s.id) for s in res["stations"]],
            project_description=f"Custom Corridor Query: Route {res['route']} {res['direction']} from {res['begin_county']} PM {res['begin_pm']} to {res['end_county']} PM {res['end_pm']}"
        )

        if args.lane_config:
            client = PeMSClient()
            client.login()
            meta = load_vds_metadata(args.metadata_dir)
            st_details = [
                fetch_station_lane_configuration(s.id, client=client, vds_record=s, timeout=getattr(args, "timeout", 60.0))
                for s in res["stations"]
            ]
            report = format_lane_config_report(
                st_details,
                project=adhoc_proj,
                hpms_summary=res.get("hpms_summary"),
                uninstrumented_info={
                    "coverage_status": res.get("coverage_status"),
                    "coverage_details": res.get("coverage_details"),
                    "nearest_station": res.get("nearest_station"),
                    "census_stations": res.get("census_stations"),
                }
            )
            print(report)
            if args.output_report:
                _save_lane_config_report(args.output_report, st_details, project=adhoc_proj, hpms_summary=res.get("hpms_summary"))
        else:
            report = format_postmile_range_report(res)
            print(report)
            if args.output_report:
                with open(args.output_report, "w", encoding="utf-8") as f:
                    f.write(report)
                print(f"\n✓ Exported postmile report -> {args.output_report}")

        if args.map or args.output_map:
            out_p = args.output_map or f"corridor_route_{res['route']}_{str(res['begin_county']).replace(' ', '_')}_PM{res['begin_pm']}_{res['end_pm']}.html"
            build_single_project_map(
                target_pid=adhoc_pid,
                projects=[adhoc_proj],
                output_path=out_p,
                open_browser=not args.no_open,
                include_stations=not args.no_stations,
                station_buffer_mi=args.buffer,
                metadata_dir=args.metadata_dir,
            )
            print(f"✓ Generated interactive corridor map -> {out_p}")

        return

    # 3. Station download (by single Station ID)
    if args.download_station and not args.lane_config:
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
            export_format=args.export_format
        )
        return

    # Verify input Master CSV exists before project-level operations
    if not os.path.exists(args.input):
        print(f"Error: master CSV file not found: {args.input}")
        sys.exit(1)

    projects = load_master_csv(args.input)

    # Resolve target project ID or station ID from any argument that provided one
    target_pid = None
    target_station = None
    if isinstance(args.slices, str):
        target_pid = args.slices.strip()
    elif isinstance(args.imagery, str):
        target_pid = args.imagery.strip()
    elif isinstance(args.map, str):
        target_pid = args.map.strip()
    elif isinstance(args.download_project, str):
        target_pid = args.download_project.strip()
    elif isinstance(args.lane_config, str):
        val = args.lane_config.strip()
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

    # Pre-resolve absolute postmiles from CA postmiles for target project if needed
    if target_pid:
        for p in projects:
            if p.project_id == target_pid:
                if p.abs_begin_pm is None or p.abs_end_pm is None:
                    calc_b, calc_e = resolve_absolute_postmiles(
                        p.route, p.county, p.begin_pm, p.end_pm
                    )
                    if calc_b is not None and calc_e is not None:
                        if p.abs_begin_pm is None:
                            p.abs_begin_pm = calc_b
                        if p.abs_end_pm is None:
                            p.abs_end_pm = calc_e

    # 4. Multi-project map
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

    # 5. Station matching / corridor audit pipeline
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
            project_id_filter=target_pid,
            verbose=True
        )
        return

    # 5b. Open to Traffic Date inference pipeline (Option 1: Lane Config & Telemetry)
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

    # 5c. Station Lane Configuration & TSN Roadway Metrics Inspection
    if args.lane_config:
        if target_station:
            client = PeMSClient()
            client.login()
            vds_meta = None
            try:
                vds_meta = get_indexed_vds_metadata()
            except Exception:
                pass
            vds_rec = vds_meta.by_id.get(target_station) if vds_meta else None
            details = fetch_station_lane_configuration(
                target_station,
                client=client,
                vds_record=vds_rec,
                timeout=getattr(args, "timeout", 60.0)
            )
            report = format_lane_config_report(details)
            print(report)

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
        report = format_lane_config_report(
            res["stations"],
            project=matched_p,
            hpms_summary=res.get("hpms_summary"),
            uninstrumented_info=res.get("uninstrumented_info"),
        )
        print(report)

        if args.output_report:
            _save_lane_config_report(
                args.output_report,
                res["stations"],
                project=matched_p,
                hpms_summary=res.get("hpms_summary"),
                uninstrumented_info=res.get("uninstrumented_info"),
            )
        return

    # 6. Corridor Slices (high-resolution Zoom 19 with interactive report)
    if args.slices:
        if not target_pid:
            print("Error: Please specify a Project ID to generate corridor slices (e.g. -p <PID> --slices or --slices <PID>).")
            sys.exit(1)
        out_d = args.output_dir if args.output_dir != "." else os.path.join(WORKSPACE_DIR, "downloads", "imagery")
        c_before = getattr(args, "before_date", None) or (args.imagery_date if args.imagery_period == "before" else None)
        c_after = getattr(args, "after_date", None) or (args.imagery_date if args.imagery_period == "after" else None)
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
            verbose=True
        )
        return

    # 7. Satellite Imagery (single frame or corridor)
    if args.imagery:
        if not target_pid:
            print("Error: Please specify a Project ID for imagery download (e.g. -p <PID> --imagery or --imagery <PID>).")
            sys.exit(1)
        out_d = args.output_dir if args.output_dir != "." else os.path.join(WORKSPACE_DIR, "downloads", "imagery")
        c_before = getattr(args, "before_date", None) or (args.imagery_date if args.imagery_period == "before" else None)
        c_after = getattr(args, "after_date", None) or (args.imagery_date if args.imagery_period == "after" else None)
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
            slice_mi=args.slice_mi,
            open_browser=not args.no_open,
            verbose=True
        )
        return

    # 8. Single Project Map
    if args.map:
        if not target_pid:
            print("Error: Please specify a Project ID for map generation (e.g. -p <PID> --map or --map <PID>).")
            sys.exit(1)
        matched = [p for p in projects if p.project_id == target_pid]
        if matched and matched[0].project_description:
            wrapped = textwrap.fill(matched[0].project_description, width=68, initial_indent="  Description:        ", subsequent_indent="                      ")
            print(f"\n[Project {target_pid}]")
            print(wrapped)

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

    # 9. Download project detector data
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
            export_format=args.export_format
        )
        return

    # 10. Project Inspection (default action when target_pid is provided without other action flags)
    if target_pid:
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
                wrapped_desc = textwrap.fill(
                    proj.project_description,
                    width=68,
                    initial_indent="  Description:        ",
                    subsequent_indent="                      "
                )
                print(wrapped_desc)
            print(f"  Route & Dir:        Route {proj.route} {proj.direction} (PeMS dir: {proj.pems_direction})")
            print(f"  District & County:  Dist {proj.dist}, {proj.county} ({proj.state})")
            print(f"  Postmiles:          Begin: {proj.begin_pm} | End: {proj.end_pm}")
            if proj.abs_begin_pm is not None or proj.abs_end_pm is not None:
                print(f"  Absolute PMs:       Begin: {proj.abs_begin_pm} | End: {proj.abs_end_pm}")
            print(f"  Coordinates:        Start: {start_c} | End: {end_c}")
            open_d_disp = proj.open_to_traffic_date if proj.open_to_traffic_date else "(None recorded)"
            print(f"  Dates:              Const: {proj.construction_begin_date} | Open: {open_d_disp}")
            if not proj.open_to_traffic_date:
                print(f"                      [Tip: Run with --find-open-date to automatically infer open date from PeMS telemetry]")
            print(f"                      [Tip: Run with --lanes to inspect raw PeMS station lane tables and TSN roadway metrics]")
            print(f"  Sensor Stations:    {proj.sensor_stations if proj.sensor_stations else '(None listed)'}")
            print("\n  GENERATED PEMS URLS:")
            print(f"  Lane Config URL:    {urls['lane_config_url']}")
            print(f"  Route Geometry URL: {urls['geometry_url']}")

            for st_id in proj.sensor_stations:
                print(f"\n  Station {st_id}:")
                print(f"    Summary URL: {urls['station_summary_urls'][st_id]}")
                if "station_config_urls" in urls and st_id in urls["station_config_urls"]:
                    print(f"    Config URL:  {urls['station_config_urls'][st_id]}")
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
        return

    # 11. List URLs
    if args.list_urls:
        print("\nListing PeMS URLs for first 5 projects:")
        for proj in projects[:5]:
            builder = ProjectPeMSQueryBuilder(proj)
            urls = builder.generate_all_urls()
            print(f"\n[{proj.project_id}] {urls['corridor']}")
            if proj.project_description:
                short_desc = proj.project_description if len(proj.project_description) <= 80 else proj.project_description[:77] + "..."
                print(f"  Description: {short_desc}")
            print(f"  Lane Config: {urls['lane_config_url']}")
            if proj.sensor_stations:
                print(f"  Sensors ({len(proj.sensor_stations)}): {', '.join(proj.sensor_stations)}")
        print(f"\n... (use -p <PID> to inspect full details for any project)")
        return

    # 12. Default summary
    print(f"Loaded {len(projects)} projects from {args.input}")
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
    print("\nQuick Start Guide:")
    print("  Inspect a project:              python pems_interface.py -p <PID>")
    print("  Station lane & TSN metrics:     python pems_interface.py -p <PID> --lanes")
    print("  Infer open to traffic date:     python pems_interface.py -p <PID> --find-open-date")
    print("  High-res corridor slices:       python pems_interface.py -p <PID> --slices")
    print("  Satellite imagery:              python pems_interface.py -p <PID> --imagery")
    print("  Interactive map:                python pems_interface.py -p <PID> --map")
    print("  Download detector data:         python pems_interface.py -p <PID> --download")
    print("  Audit detector stations:        python pems_interface.py --audit")
    print("\nRun with --help to see all options.")


if __name__ == "__main__":
    main()
