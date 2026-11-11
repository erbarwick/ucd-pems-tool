"""Normalization, postmile, county, coordinate, date, and tabular cell helpers."""

from __future__ import annotations

import datetime
import os
import re
from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd

from pems.config import CA_FIPS_TO_COUNTY, CALTRANS_COUNTY_ABBREVIATIONS

_DIR_MAP = {
    "N": "N", "NB": "N", "NORTH": "N", "NORTHBOUND": "N",
    "S": "S", "SB": "S", "SOUTH": "S", "SOUTHBOUND": "S",
    "E": "E", "EB": "E", "EAST": "E", "EASTBOUND": "E",
    "W": "W", "WB": "W", "WEST": "W", "WESTBOUND": "W",
}

_PM_CALTRANS_PFX_RE = re.compile(
    r"^(?:\d{1,2}\s*[-–]\s*)?[A-Za-z]{3,4}\s*[-–]\s*\d+\s*(?:PM)?\s*",
    re.IGNORECASE,
)
_PM_ROUTE_PFX_RE = re.compile(
    r"^(?:I|SR|US|RTE|ROUTE|HWY)\s*[-–]?\s*\d+\s*(?:PM)?\s*",
    re.IGNORECASE,
)
_PM_COUNTY_PFX_RE = re.compile(r"^[A-Za-z]{3,4}\s+(?:PM\s*)?", re.IGNORECASE)
_PM_PARTS_RE = re.compile(r"^([A-Za-z\s\-]*?)([0-9]+(?:\.[0-9]+)?)([A-Za-z\s\-]*)$")
_PM_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+")
_COORD_DEC_RE = re.compile(r"^[\(\[]?\s*([+-]?\d+\.?\d*)\s*[,;\s]+\s*([+-]?\d+\.?\d*)\s*[\)\]]?$")
_COORD_DMS_RE = re.compile(
    r"(\d+)[°\s]+(\d+)[\'\"\s]+([\d\.]+)[\'\"\s]*([NSns])[\s,;]+"
    r"(\d+)[°\s]+(\d+)[\'\"\s]+([\d\.]+)[\'\"\s]*([EWew])"
)
_TRAILING_DOT_ZERO_RE = re.compile(r"\.0+$")
_NON_DIGITS_RE = re.compile(r"[^0-9]")


def normalize_direction(d: Any) -> str:
    """Normalize direction string to single-letter cardinal ('N', 'S', 'E', 'W') or ''."""
    if not d or pd.isna(d):
        return ""
    return _DIR_MAP.get(str(d).strip().upper(), "")


def normalize_route(r: Any) -> str:
    """Normalize highway route string to digits: 'SR-80' -> '80', 'I-5' -> '5', '5.0' -> '5'."""
    if not r or pd.isna(r):
        return ""
    s = _TRAILING_DOT_ZERO_RE.sub("", str(r).strip())
    return _NON_DIGITS_RE.sub("", s)


def normalize_district(d: Any) -> str:
    """Normalize Caltrans district number/string (e.g. '4.0' -> '4', '04' -> '4')."""
    if d is None or (isinstance(d, float) and np.isnan(d)):
        return ""
    s = _TRAILING_DOT_ZERO_RE.sub("", str(d).strip())
    return str(int(s)) if s.isdigit() else s


def normalize_ca_county(county: Any) -> str:
    """Normalize California county name, Caltrans abbreviation, or FIPS code."""
    if county is None or not str(county).strip():
        return ""
    c_raw = str(county).strip()
    try:
        val_int = int(float(c_raw))
        if val_int in CA_FIPS_TO_COUNTY:
            return CA_FIPS_TO_COUNTY[val_int]
    except (TypeError, ValueError):
        pass

    c_lower = c_raw.lower()
    if c_lower in CALTRANS_COUNTY_ABBREVIATIONS:
        return CALTRANS_COUNTY_ABBREVIATIONS[c_lower]
    return c_raw.title()


def _strip_pm_prefixes(raw_str: str) -> str:
    """Strip Caltrans district/county/route prefixes from a raw postmile string."""
    s = _PM_CALTRANS_PFX_RE.sub("", raw_str).strip()
    s = _PM_ROUTE_PFX_RE.sub("", s).strip()
    return _PM_COUNTY_PFX_RE.sub("", s).strip()


def format_pm(val: Any) -> str:
    """Format a postmile cleanly without floating-point artifacts while keeping letter codes."""
    if val is None or pd.isna(val) or str(val).strip().lower() in ("", "nan", "none"):
        return ""
    s = _strip_pm_prefixes(str(val).strip())

    sign = ""
    if s.startswith("-"):
        sign = "-"
        s = s[1:].strip()

    m = _PM_PARTS_RE.match(s)
    if m:
        pfx, digits, sfx = m.group(1).strip(), m.group(2), m.group(3).strip()
        num_str = f"{round(float(sign + digits), 3):.3f}".rstrip("0").rstrip(".")
        return f"{pfx}{num_str}{sfx}"
    try:
        num = float(sign + s)
        return f"{round(num, 3):.3f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return s


def split_ca_pm(ca_pm: Any) -> Tuple[str, float]:
    """Split a California postmile string into (prefix, float_value)."""
    if ca_pm is None or (isinstance(ca_pm, float) and np.isnan(ca_pm)):
        return "", np.nan
    s = str(ca_pm).strip()
    if not s or s.lower() in ("nan", "none"):
        return "", np.nan

    s_clean = _strip_pm_prefixes(s)
    sign = ""
    if s_clean.startswith("-"):
        sign = "-"
        s_clean = s_clean[1:].strip()

    match = _PM_PARTS_RE.match(s_clean)
    if match:
        return match.group(1).strip(), round(float(sign + match.group(2)), 4)

    try:
        return "", round(float(sign + s_clean), 4)
    except (TypeError, ValueError):
        return s, np.nan


def parse_postmile(val: Any) -> Tuple[Optional[float], str]:
    """Parse a postmile into (numeric_float_or_None, original_trimmed_string)."""
    if val is None or pd.isna(val):
        return None, ""
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none"):
        return None, ""
    cleaned = s.replace("PM", "").replace("pm", "").strip()
    match = _PM_FLOAT_RE.search(cleaned)
    if match:
        try:
            return float(match.group(0)), s
        except (TypeError, ValueError):
            pass
    return None, s


def is_empty_cell(val: Any) -> bool:
    """Return True only for empty/null/zero spreadsheet cells, protecting qualitative text notes."""
    if val is None or pd.isna(val):
        return True
    return str(val).strip() in ("", "nan", "None", "0", "0.0")


def format_aadt_display(val: Any) -> str:
    """Format AADT integers with commas while preserving qualitative annotations verbatim."""
    if pd.isna(val) or val is None:
        return "(None)"
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none"):
        return "(None)"
    clean_dig = s.replace(".0", "").replace(",", "").strip()
    if clean_dig.isdigit():
        try:
            return f"{int(clean_dig):,}"
        except Exception:
            pass
    return s


def parse_coordinate_string(coord_str: Any) -> Optional[Tuple[float, float]]:
    """Parse decimal degree or Google Maps DMS strings/tuples into (lat, lon)."""
    if isinstance(coord_str, (tuple, list)) and len(coord_str) == 2:
        try:
            return float(coord_str[0]), float(coord_str[1])
        except (ValueError, TypeError):
            return None

    if not isinstance(coord_str, str):
        return None

    s = coord_str.strip()
    m_dec = _COORD_DEC_RE.match(s)
    if m_dec:
        try:
            return float(m_dec.group(1)), float(m_dec.group(2))
        except ValueError:
            pass

    m_dms = _COORD_DMS_RE.search(s)
    if m_dms:
        lat_d, lat_m, lat_s, lat_dir, lng_d, lng_m, lng_s, lng_dir = m_dms.groups()
        lat = float(lat_d) + float(lat_m) / 60.0 + float(lat_s) / 3600.0
        if lat_dir.upper() == "S":
            lat = -lat
        lng = float(lng_d) + float(lng_m) / 60.0 + float(lng_s) / 3600.0
        if lng_dir.upper() == "W":
            lng = -lng
        return lat, lng

    return None


def haversine_distance_miles(lat1: float, lon1: float, lat2: Any, lon2: Any) -> Any:
    """Calculate great-circle distance in miles using Haversine (supports vectorized Series/arrays)."""
    earth_radius_mi = 3958.8
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2.0) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2.0) ** 2
    return 2.0 * earth_radius_mi * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def build_date_from_parts(
    year: Any,
    month: Any = None,
    day: Any = None,
    default_month: int = 1,
    default_day: int = 1,
) -> Optional[datetime.date]:
    """Construct a datetime.date from separate year, month, and day fields."""
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
        for fallback_d in (30, 29, 28):
            if d_int > 28:
                try:
                    return datetime.date(y_int, m_int, fallback_d)
                except ValueError:
                    continue
        return datetime.date(y_int, m_int, 1)


def parse_iso_date(val: Any, default_month: int = 1, default_day: int = 1) -> Optional[datetime.date]:
    """Parse an ISO date string (YYYY-MM-DD), date/datetime object, or 4-digit year."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, datetime.datetime):
        return val.date()
    if isinstance(val, datetime.date):
        return val
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
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
    """Backwards-compatible alias for parse_iso_date."""
    return parse_iso_date(val, default_month, default_day)


parse_project_date = _parse_project_date


def parse_excel_serial_date(val: Any) -> Optional[str]:
    """Convert a 5-digit Excel serial date string/int to YYYY-MM-DD, or return None."""
    s = str(val).strip()
    if s.isdigit() and len(s) == 5:
        d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(s))
        return d.strftime("%Y-%m-%d")
    return None


def find_column(df: pd.DataFrame, target_name: str) -> Optional[str]:
    """Case-insensitive, whitespace-tolerant DataFrame column lookup."""
    target_clean = target_name.strip().lower()
    for col in df.columns:
        if str(col).strip().lower() == target_clean:
            return col
    return None


_find_column = find_column


def google_earth_url(lat: float, lon: float, length_mi: float = 0.25, distance_m: Optional[int] = None) -> str:
    """Build a Google Earth Web URL centered on (lat, lon) scaled to segment length."""
    eye_dist = distance_m if distance_m is not None else max(450, int(length_mi * 1609.34 * 1.6))
    return f"https://earth.google.com/web/@{lat:.6f},{lon:.6f},0a,{eye_dist}d,35y,0h,0t,0r"


def google_maps_url(lat: float, lon: float, zoom: int = 18) -> str:
    """Build a Google Maps satellite view URL centered on (lat, lon)."""
    return f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},{zoom}z/data=!3m1!1e3"


def assert_not_box_path(target_path: str) -> None:
    """Raise PermissionError if target_path points inside a protected Box source directory."""
    abs_path = os.path.abspath(target_path).lower()
    if "box-box" in abs_path or "cloudstorage/box" in abs_path:
        raise PermissionError(
            f"CRITICAL SAFETY VIOLATION: Refusing to modify Box spreadsheet at {target_path}. "
            "Direct edits to the Box master spreadsheet are strictly prohibited. "
            "Target only local repository files (e.g. staging/Master_CA_Project_List_Cleaned.csv)."
        )
