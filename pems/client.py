"""Authenticated Caltrans PeMS HTTP client and URL builders."""

from __future__ import annotations

import calendar
import datetime
import io
import os
import time
import urllib.parse
from typing import Any, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

from pems.config import BASE_URL, LOGGER, TABLE_MARKERS
from pems.normalize import normalize_direction, normalize_route


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

    def _setup_session(self) -> None:
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
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
            "login": "Login",
        }

        LOGGER.info(f"Submitting credentials for {self.username}...")
        resp = self.session.post(action_url, data=login_data, allow_redirects=True)
        resp.raise_for_status()

        if "logout" not in resp.text.lower():
            if self.debug:
                with open("debug_pems_login_failure.html", "w", encoding="utf-8") as fh:
                    fh.write(resp.text)
            raise RuntimeError("Login failed: no 'Logout' link found in response.")

        self.logged_in = True
        LOGGER.info("PeMS login successful.")
        return True

    @staticmethod
    def _is_login_page(resp: requests.Response) -> bool:
        """Check if response contains a login form and lacks an active session."""
        has_form = "<form" in resp.text and ("username" in resp.text or "password" in resp.text)
        has_logout = "logout" in resp.text.lower()
        return has_form and not has_logout

    def get_url(self, url: str, stream: bool = False, timeout: float = 60.0, max_retries: int = 2) -> requests.Response:
        """Make an authenticated GET request, re-authenticating if session expired, with timeout retry."""
        if not self.logged_in:
            self.login()

        full_url = url if url.startswith("http") else urllib.parse.urljoin(BASE_URL, url)

        last_exc: Optional[Exception] = None
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
                    current_timeout = max(current_timeout * 1.5, min(timeout + 15.0, 45.0))
                    LOGGER.warning(
                        f"PeMS server response slow (timed out after {timeout:.0f}s). "
                        f"Retrying (attempt {attempt + 1}/{max_retries}) with {current_timeout:.0f}s timeout..."
                    )
                    time.sleep(1.5)
                else:
                    raise
            except Exception as exc:
                last_exc = exc
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Unreachable retry loop termination for {full_url}")

    def get_text_content(self, url: str, timeout: float = 60.0) -> Optional[str]:
        """Fetch content as decoded text with configurable timeout (default 60s)."""
        try:
            return self.get_url(url, timeout=timeout).text
        except Exception as exc:
            LOGGER.error(f"Error fetching {url}: {exc}")
            return None

    def download_file(self, url: str, out_path: str, overwrite: bool = False) -> bool:
        """Download file with chunk streaming to prevent memory exhaustion."""
        if os.path.exists(out_path) and not overwrite and self.file_has_data_table(out_path):
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
        """Validate whether a file, bytes buffer, or string contains a valid PeMS data table."""
        try:
            excel_magics = (b"\xd0\xcf\x11\xe0", b"PK\x03\x04")
            if isinstance(path_or_content, bytes):
                if path_or_content.startswith(excel_magics):
                    return True
                lines = path_or_content.decode("utf-8", errors="replace").splitlines()[:50]
            elif os.path.isfile(str(path_or_content)):
                with open(str(path_or_content), "rb") as bfh:
                    if bfh.read(8).startswith(excel_magics):
                        return True
                with open(str(path_or_content), "r", encoding="utf-8", errors="replace") as fh:
                    lines = [fh.readline() for _ in range(50)]
            else:
                lines = str(path_or_content).splitlines()[:50]

            for line in lines:
                s = line.strip()
                if any(s.startswith(m) for m in TABLE_MARKERS) and ("\t" in line or "," in line):
                    return True
                low = s.lower()
                if ("<table" in low or "<tr" in low or "<th" in low or "<td" in low) and any(
                    m.strip() in line for m in TABLE_MARKERS
                ):
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
        header_idx = next(
            (
                i
                for i, line in enumerate(lines[:60])
                if any(line.strip().startswith(m) for m in TABLE_MARKERS) and ("\t" in line or "," in line)
            ),
            None,
        )
        if header_idx is None:
            return None

        sep = "\t" if "\t" in lines[header_idx] else ","
        try:
            df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=sep)
            df.columns = df.columns.str.strip()
            return df
        except Exception as exc:
            LOGGER.error(f"Error parsing PeMS table: {exc}")
            return None


def _format_date_param(d_str: str) -> str:
    """Format input date string to URL-encoded MM/DD/YYYY for PeMS URLs."""
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


def _normalize_export_flag(export_format: str) -> str:
    return "xls" if str(export_format).strip().lower() in ("xls", "xlsx") else "text"


def _year_epoch_bounds(start_year: int, end_year: int, last_second: int = 59) -> tuple[int, int]:
    jan_1 = datetime.datetime(start_year, 1, 1, 0, 0, 0)
    dec_31 = datetime.datetime(end_year, 12, 31, 23, 59, last_second)
    return int(calendar.timegm(jan_1.timetuple())), int(calendar.timegm(dec_31.timetuple()))


def build_lane_config_url(
    route: str,
    direction: str,
    start_date: str,
    end_date: str,
    station_type: str = "",
    export_format: str = "text",
) -> str:
    """Construct PeMS Freeway configuration time series URL."""
    route_num = normalize_route(route)
    dir_clean = normalize_direction(direction)
    s_fmt = _format_date_param(start_date)
    e_fmt = _format_date_param(end_date)
    exp = _normalize_export_flag(export_format)
    return (
        f"{BASE_URL}/?report_form=1&dnode=Freeway&content=elv&tab=config_timeseries"
        f"&export={exp}&fwy={route_num}&dir={dir_clean}"
        f"&s_time_id_f={s_fmt}&e_time_id_f={e_fmt}&q=lanes&gn=day&station_type={station_type}"
    )


def build_station_aadt_url(
    station_id: str,
    start_year: int,
    end_year: Optional[int] = None,
    export_format: str = "text",
) -> str:
    """Construct PeMS VDS annual AADT export URL."""
    st_id = str(station_id).strip()
    end_year = end_year or start_year
    s_time_id, e_time_id = _year_epoch_bounds(start_year, end_year, last_second=59)
    exp = _normalize_export_flag(export_format)
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
    export_format: str = "text",
) -> str:
    """Construct PeMS VDS detector time series export URL for any quantity and granularity."""
    st_id = str(station_id).strip()
    end_year = end_year or start_year
    s_time_id, e_time_id = _year_epoch_bounds(start_year, end_year, last_second=0)
    s_fmt = urllib.parse.quote(f"01/01/{start_year} 00:00")
    e_fmt = urllib.parse.quote(f"12/31/{end_year} 23:59")
    q_norm = str(quantity).strip().lower()
    gn_norm = str(granularity).strip().lower()
    exp = _normalize_export_flag(export_format)
    return (
        f"{BASE_URL}/?report_form=1&dnode=VDS&content=loops&tab=det_timeseries&export={exp}"
        f"&station_id={st_id}&s_time_id={s_time_id}&s_time_id_f={s_fmt}"
        f"&e_time_id={e_time_id}&e_time_id_f={e_fmt}"
        "&tod=all&tod_from=0&tod_to=0"
        "&dow_0=on&dow_1=on&dow_2=on&dow_3=on&dow_4=on&dow_5=on&dow_6=on"
        f"&holidays=on&q={q_norm}&q2=&gn={gn_norm}"
    )


def build_station_flow_url(
    station_id: str,
    start_year: int,
    end_year: Optional[int] = None,
    granularity: str = "month",
    export_format: str = "text",
) -> str:
    """Construct PeMS VDS detector time series (Flow) export URL."""
    return build_station_timeseries_url(
        station_id=station_id,
        start_year=start_year,
        end_year=end_year,
        quantity="flow",
        granularity=granularity,
        export_format=export_format,
    )


def build_fwy_latlng_url(route: str, direction: str, export_format: str = "text") -> str:
    """Construct PeMS Freeway coordinate geometry URL."""
    route_num = normalize_route(route)
    dir_clean = normalize_direction(direction)
    exp = _normalize_export_flag(export_format)
    return f"{BASE_URL}/?report_form=1&dnode=Freeway&content=fwy_latlngXY&export={exp}&fwy={route_num}&dir={dir_clean}"


def build_station_summary_url(station_id: str) -> str:
    """Construct PeMS VDS Summary / Metadata URL."""
    return f"{BASE_URL}/?report_form=1&dnode=VDS&content=summary&tab=summary&station_id={str(station_id).strip()}"


def build_station_config_url(station_id: str) -> str:
    """Construct PeMS VDS Configuration URL (lane configuration table and TSN roadway metrics)."""
    return f"{BASE_URL}/?dnode=VDS&content=sta_cfg&tab=sta_cfg&station_id={str(station_id).strip()}"


def build_clearinghouse_url(district: Any, year: int, file_type: str = "station_aadt") -> str:
    """Construct PeMS Clearinghouse bulk download URL."""
    dist_clean = str(district).replace(".0", "").strip()
    return f"{BASE_URL}/?srq=clearinghouse&district_id={dist_clean}&yy={year}&type={file_type}&returnformat=text"
