"""Indexed highway geometry reference, California/Absolute postmile conversions, and Census lookups."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import numpy as np
import pandas as pd

from pems.config import CENSUS_PATHS, DEFAULT_REF_PATH
from pems.normalize import (
    haversine_distance_miles,
    normalize_ca_county,
    normalize_direction,
    normalize_district,
    normalize_route,
    parse_postmile,
    split_ca_pm,
)


class IndexedGeoReference:
    """O(1) partitioned highway centerline index over the Caltrans postmile reference table."""

    def __init__(self, df: pd.DataFrame):
        self.raw_df = df
        route_col = "Route" if "Route" in df.columns else "Fwy"
        dir_col = "Direction" if "Direction" in df.columns else ("Dir" if "Dir" in df.columns else None)

        clean_df = df.copy()
        clean_df["_route"] = clean_df[route_col].astype(str).str.strip().map(normalize_route)
        clean_df["_dir"] = (
            clean_df[dir_col].astype(str).str.strip().map(normalize_direction)
            if dir_col
            else ""
        )
        clean_df["_abs_pm"] = pd.to_numeric(clean_df["Abs PM"], errors="coerce") if "Abs PM" in clean_df.columns else np.nan
        clean_df["_county"] = (
            clean_df["County"].astype(str).str.strip().str.lower()
            if "County" in clean_df.columns
            else ""
        )

        if "CA PM" in clean_df.columns:
            ca_splits = clean_df["CA PM"].astype(str).map(split_ca_pm)
            clean_df["_ca_pfx"] = ca_splits.map(lambda x: x[0].upper())
            clean_df["_ca_num"] = ca_splits.map(lambda x: x[1])
        else:
            clean_df["_ca_pfx"] = ""
            clean_df["_ca_num"] = np.nan

        clean_df["_lat"] = pd.to_numeric(clean_df["Latitude"], errors="coerce") if "Latitude" in clean_df.columns else np.nan
        clean_df["_lng"] = pd.to_numeric(clean_df["Longitude"], errors="coerce") if "Longitude" in clean_df.columns else np.nan

        self.clean_df = clean_df
        self.lats = clean_df["_lat"]
        self.lngs = clean_df["_lng"]

        self.index: Dict[Tuple[str, str], pd.DataFrame] = {}
        for (r, d), grp in clean_df.groupby(["_route", "_dir"], sort=False):
            self.index[(r, d)] = grp

        self.route_index: Dict[str, pd.DataFrame] = {}
        for r, grp in clean_df.groupby("_route", sort=False):
            self.route_index[r] = grp

    def get_corridor(self, route: str, direction: str) -> Optional[pd.DataFrame]:
        """Return reference rows partitioned for (route, direction)."""
        return self.index.get((normalize_route(route), normalize_direction(direction)))

    def get_route_df(self, route: Any) -> pd.DataFrame:
        """Return reference rows for a route across all directions."""
        r_norm = normalize_route(route)
        sub = self.route_index.get(r_norm)
        return sub if sub is not None else self.clean_df.iloc[0:0]

    def valid_directions_for_route(self, route: str) -> Set[str]:
        """Return all valid Caltrans single-letter directions for a highway route."""
        r_norm = normalize_route(route)
        return {d for (r, d) in self.index.keys() if r == r_norm and d}

    def all_routes(self) -> List[str]:
        """Return sorted list of unique routes present in the reference index."""
        routes = [r for r in self.route_index.keys() if r]
        return sorted(routes, key=lambda r: (len(r), r))

    def find_candidate_highways_for_coords(
        self,
        lat: float,
        lng: float,
        max_dist_mi: float = 0.5,
    ) -> List[Tuple[pd.Series, float]]:
        """Find nearby highway segments grouped by route, ordered by haversine distance."""
        sub = self.clean_df.iloc[0:0]
        for delta in (0.15, 0.8):
            mask = (
                (self.lats >= lat - delta)
                & (self.lats <= lat + delta)
                & (self.lngs >= lng - delta)
                & (self.lngs <= lng + delta)
            )
            sub = self.clean_df[mask]
            if not sub.empty:
                break
        if sub.empty:
            sub = self.clean_df

        dists = haversine_distance_miles(lat, lng, self.lats[sub.index], self.lngs[sub.index])
        close_mask = dists <= max_dist_mi
        if not close_mask.any():
            idx = dists.idxmin()
            return [(sub.loc[idx], float(dists.loc[idx]))]

        res: List[Tuple[pd.Series, float]] = []
        close_sub = sub[close_mask]
        close_dists = dists[close_mask]
        for _, grp in close_sub.groupby("_route", sort=False):
            grp_dists = close_dists[grp.index]
            min_idx = grp_dists.idxmin()
            res.append((grp.loc[min_idx], float(grp_dists.loc[min_idx])))
        res.sort(key=lambda x: x[1])
        return res

    def auto_detect_highway_corridor(
        self,
        start_coord: Tuple[float, float],
        end_coord: Tuple[float, float],
    ) -> Tuple[str, pd.Series, pd.Series, float, float]:
        """Auto-detect the highway corridor connecting two coordinates."""
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
            sub_route = self.get_route_df(best_route)
            e_dists = haversine_distance_miles(
                end_coord[0], end_coord[1], self.lats[sub_route.index], self.lngs[sub_route.index]
            )
            e_idx = e_dists.idxmin()
            e_row = sub_route.loc[e_idx]
            e_dist = float(e_dists.loc[e_idx])

        return best_route, s_row, e_row, s_dist, e_dist


# Path-keyed geo cache so explicit ref_path overrides are never masked by stale globals
_GEO_CACHE: Dict[str, Tuple[pd.DataFrame, IndexedGeoReference]] = {}
_CENSUS_CACHE: Optional[pd.DataFrame] = None


def reset_geo_cache() -> None:
    """Clear cached geo reference tables and Census station index."""
    global _CENSUS_CACHE
    _GEO_CACHE.clear()
    _CENSUS_CACHE = None


def load_geo_reference(ref_path: Optional[str] = None) -> pd.DataFrame:
    """Load and index the Caltrans highway postmile reference table, cached per resolved path."""
    path = os.path.abspath(ref_path or DEFAULT_REF_PATH)
    if path in _GEO_CACHE:
        return _GEO_CACHE[path][0]

    if not os.path.exists(path):
        raise FileNotFoundError(f"Reference table not found at: {path}")

    for sep in (",", "\t"):
        try:
            df = pd.read_csv(path, sep=sep, dtype=str)
            df.columns = df.columns.str.strip()
            if "Route" in df.columns or "Fwy" in df.columns:
                idx_ref = IndexedGeoReference(df)
                _GEO_CACHE[path] = (df, idx_ref)
                return df
        except Exception:
            continue

    raise ValueError(f"Could not parse valid reference columns in {path}")


def get_indexed_geo_ref(ref_path: Optional[str] = None) -> IndexedGeoReference:
    """Return the cached IndexedGeoReference for ref_path (default reference table if omitted)."""
    path = os.path.abspath(ref_path or DEFAULT_REF_PATH)
    if path not in _GEO_CACHE:
        load_geo_reference(path)
    return _GEO_CACHE[path][1]


def coerce_geo_ref(ref_or_df: Any = None) -> IndexedGeoReference:
    """Normalize None, DataFrame, IndexedGeoReference, or MagicMock(raw_df=df) into IndexedGeoReference."""
    if ref_or_df is None:
        return get_indexed_geo_ref()
    if isinstance(ref_or_df, IndexedGeoReference):
        return ref_or_df
    if isinstance(ref_or_df, pd.DataFrame):
        return IndexedGeoReference(ref_or_df)
    clean = getattr(ref_or_df, "clean_df", None)
    if isinstance(clean, pd.DataFrame):
        return ref_or_df
    raw = getattr(ref_or_df, "raw_df", None)
    if isinstance(raw, pd.DataFrame):
        return IndexedGeoReference(raw)
    return get_indexed_geo_ref()


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
                continue
    return None


def _parse_numeric_pm(val: Any) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        _, num = split_ca_pm(val)
        return num


def pm_to_coordinates(
    route: str,
    direction: str,
    pm: Any,
    county: Optional[str] = None,
    is_absolute: bool = False,
    ref_df: Optional[Any] = None,
) -> Optional[Tuple[float, float]]:
    """Convert postmile to (latitude, longitude) using O(1) indexed lookup."""
    idx_ref = coerce_geo_ref(ref_df)
    sub = idx_ref.get_corridor(route, direction)
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
        matched = sub[(sub["_ca_num"].round(1) == num) & (sub["_county"] == county_clean)]
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
    ref_df: Optional[Any] = None,
) -> pd.DataFrame:
    """Find all reference segments along a corridor postmile range."""
    idx_ref = coerce_geo_ref(ref_df)
    sub = idx_ref.get_corridor(route, direction)
    if sub is None or sub.empty:
        return pd.DataFrame()

    b_val = _parse_numeric_pm(begin_pm)
    e_val = _parse_numeric_pm(end_pm)
    if np.isnan(b_val) or np.isnan(e_val):
        return pd.DataFrame()

    min_pm, max_pm = min(b_val, e_val), max(b_val, e_val)
    if is_absolute or county is None:
        matched = sub[(sub["_abs_pm"] >= min_pm - 0.05) & (sub["_abs_pm"] <= max_pm + 0.05)]
    else:
        county_clean = str(county).strip().lower()
        matched = sub[
            (sub["_ca_num"] >= min_pm - 0.05)
            & (sub["_ca_num"] <= max_pm + 0.05)
            & (sub["_county"] == county_clean)
        ]

    orig_cols = [c for c in sub.columns if not c.startswith("_")]
    return matched[orig_cols]


def find_closest_census_stations(
    route: str,
    district: Any,
    ca_pm: float,
    county: Optional[str] = None,
    top_n: int = 5,
) -> pd.DataFrame:
    """Find closest Caltrans Census stations to a corridor postmile."""
    cdf = load_census_reference()
    if cdf is None or cdf.empty:
        return pd.DataFrame()

    r_num = normalize_route(route)
    d_clean = normalize_district(district)

    df = cdf.copy()
    fwy_num = df["Fwy"].astype(str).str.replace(r"[^0-9]", "", regex=True)
    dist_num = pd.to_numeric(df["District"], errors="coerce").fillna(0).astype(int).astype(str)

    cond = (fwy_num == r_num) & ((dist_num == d_clean) | (dist_num == "0"))
    if county and "County" in df.columns:
        c_clean = str(county).strip().lower()
        matched = df[cond & (df["County"].astype(str).str.strip().str.lower() == c_clean)].copy()
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


def _resolve_directional_coord(
    idx_ref: IndexedGeoReference,
    route_norm: str,
    dir_norm: Optional[str],
    best_row: pd.Series,
    abs_pm: float,
) -> Tuple[float, float]:
    """Refine (lat, lng) onto the specified directional carriageway when available."""
    lat = float(best_row["_lat"]) if pd.notna(best_row.get("_lat")) else float("nan")
    lng = float(best_row["_lng"]) if pd.notna(best_row.get("_lng")) else float("nan")
    if dir_norm and str(best_row.get("_dir", "")) != dir_norm:
        dir_rows = idx_ref.get_corridor(route_norm, dir_norm)
        if dir_rows is not None and not dir_rows.empty:
            valid_dir = dir_rows.dropna(subset=["_abs_pm", "_lat", "_lng"])
            if not valid_dir.empty:
                coord_row = valid_dir.loc[(valid_dir["_abs_pm"] - abs_pm).abs().idxmin()]
                lat, lng = float(coord_row["_lat"]), float(coord_row["_lng"])
    return lat, lng


def convert_california_postmile(
    route: Union[str, int],
    county: str,
    ca_pm: Union[str, float, int],
    direction: Optional[str] = None,
    ref_df: Optional[Any] = None,
    idx_ref: Optional[Any] = None,
) -> Dict[str, Any]:
    """Convert a California county postmile (CA PM) to Absolute Postmile and centerline coords."""
    if not county or not str(county).strip():
        return {"error": "County must be provided to convert a California postmile."}

    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction) if direction else None
    county_norm = normalize_ca_county(county)
    ref = coerce_geo_ref(idx_ref if idx_ref is not None else ref_df)

    sub = ref.get_route_df(route_norm)
    sub = sub[sub["_county"] == county_norm.lower()].dropna(subset=["_ca_num", "_abs_pm"])
    if sub.empty:
        return {"error": f"Route '{route_norm}' in County '{county_norm}' not found in California highway database."}

    pfx, num = split_ca_pm(ca_pm)
    if np.isnan(num):
        return {"error": f"Invalid postmile format: '{ca_pm}'"}

    matched = sub
    if dir_norm:
        sub_dir = sub[sub["_dir"] == dir_norm]
        if not sub_dir.empty and (sub_dir["_ca_num"].min() - 1.0 <= num <= sub_dir["_ca_num"].max() + 1.0):
            matched = sub_dir

    if pfx:
        pfx_match = matched[matched["_ca_pfx"] == pfx.upper()]
        if not pfx_match.empty:
            matched = pfx_match

    best_row = matched.loc[(matched["_ca_num"] - num).abs().idxmin()]
    abs_pm = float(best_row["_abs_pm"])
    dist_val = normalize_district(best_row.get("District", ""))
    lat, lng = _resolve_directional_coord(ref, route_norm, dir_norm, best_row, abs_pm)

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
    idx_ref: Optional[Any] = None,
) -> Dict[str, Any]:
    """Convert a continuous Absolute Postmile to California County Postmile and centerline coords."""
    route_norm = normalize_route(route)
    dir_norm = normalize_direction(direction) if direction else None
    ref = coerce_geo_ref(idx_ref if idx_ref is not None else ref_df)

    sub = ref.get_route_df(route_norm)
    if sub.empty:
        return {"error": f"Route '{route_norm}' not found in California highway database."}

    try:
        target_abs = float(str(abs_pm).strip())
    except (TypeError, ValueError):
        return {"error": f"Invalid Absolute Postmile format: '{abs_pm}'"}

    sub = sub.dropna(subset=["_abs_pm"])
    if sub.empty:
        return {"error": f"No valid Absolute Postmiles found for Route {route_norm}."}

    matched = sub
    if dir_norm:
        sub_dir = sub[sub["_dir"] == dir_norm]
        if not sub_dir.empty:
            matched = sub_dir

    diff = (matched["_abs_pm"] - target_abs).abs()
    best_idx = diff.idxmin()
    best_row = matched.loc[best_idx]
    delta_mi = float(diff.loc[best_idx])
    snapped_abs = float(best_row["_abs_pm"])

    county_val = normalize_ca_county(best_row.get("County", ""))
    dist_val = normalize_district(best_row.get("District", ""))
    ca_pm_val = str(best_row.get("CA PM", best_row.get("State_PM", "")))
    pfx, ca_num = split_ca_pm(ca_pm_val)
    lat, lng = _resolve_directional_coord(ref, route_norm, dir_norm, best_row, snapped_abs)

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


def resolve_absolute_postmiles(
    route: Any,
    county: str,
    begin_pm: Any,
    end_pm: Any,
    ref_df: Optional[Any] = None,
    ref_path: Optional[str] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """Resolve County Postmiles to Absolute Postmiles with inversion and 2.0-mile snap guards."""
    if route is None or not str(route).strip() or not county or not str(county).strip():
        return None, None

    if isinstance(begin_pm, (int, float)) and not pd.isna(begin_pm):
        b_num: Optional[float] = float(begin_pm)
    else:
        b_num, _ = parse_postmile(begin_pm)

    if isinstance(end_pm, (int, float)) and not pd.isna(end_pm):
        e_num: Optional[float] = float(end_pm)
    else:
        e_num, _ = parse_postmile(end_pm)

    if b_num is None or e_num is None:
        return None, None

    # Cross-county boundary inversion guard: single-county lookup when begin > end yields bogus snaps
    if b_num > e_num:
        return None, None

    try:
        if ref_df is not None:
            if isinstance(ref_df, pd.DataFrame) and ref_df.empty:
                return None, None
            ref = coerce_geo_ref(ref_df)
        else:
            target = ref_path or DEFAULT_REF_PATH
            if not os.path.exists(target):
                return None, None
            ref = get_indexed_geo_ref(target)
    except Exception:
        return None, None

    county_clean = normalize_ca_county(county).lower()
    sub = ref.get_route_df(route)
    if sub.empty:
        return None, None

    # Accept both raw exact county name and normalized name
    raw_co_clean = str(county).strip().lower()
    sub_co = sub[sub["_county"].isin({county_clean, raw_co_clean})].dropna(subset=["_ca_num", "_abs_pm"])
    if sub_co.empty:
        return None, None

    sub_sorted = sub_co.sort_values(by="_ca_num")
    diff_b = (sub_sorted["_ca_num"] - b_num).abs()
    diff_e = (sub_sorted["_ca_num"] - e_num).abs()
    idx_b = diff_b.idxmin()
    idx_e = diff_e.idxmin()

    if float(diff_b.loc[idx_b]) > 2.0 or float(diff_e.loc[idx_e]) > 2.0:
        return None, None

    abs_b = float(sub_sorted.loc[idx_b, "_abs_pm"])
    abs_e = float(sub_sorted.loc[idx_e, "_abs_pm"])
    return min(abs_b, abs_e), max(abs_b, abs_e)
