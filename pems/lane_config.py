"""PeMS station lane configuration scraping, disk JSON caching, tooltips, and HPMS fallback."""

from __future__ import annotations

import datetime
import json
import os
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

from pems.client import PeMSClient, build_station_config_url
from pems.config import (
    DEFAULT_MASTER_CSV,
    DEFAULT_STATION_CONFIG_CACHE_DIR,
    LOGGER,
    decode_hpms_attributes,
)
from pems.geo import resolve_absolute_postmiles
from pems.normalize import normalize_district, normalize_route, split_ca_pm
from pems.projects import ProjectRecord, load_master_csv
from pems.reporting import format_dict_table_lines
from pems.vds import (
    IndexedVDSMetadata,
    VDSStationRecord,
    compare_project_stations,
    diagnose_uninstrumented_corridor,
    load_vds_metadata,
)


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


def populate_station_config_from_vds(
    details: StationLaneConfigDetails,
    vds_record: Optional[VDSStationRecord],
    only_if_missing: bool = False,
    synth_lanes: bool = False,
) -> StationLaneConfigDetails:
    """Populate static identity & location fields on StationLaneConfigDetails from a VDSStationRecord."""
    if not vds_record:
        return details
    if not only_if_missing or not details.station_name:
        details.station_name = vds_record.name
    if not only_if_missing or not details.district:
        details.district = str(vds_record.district)
    if not only_if_missing or not details.route:
        details.route = str(vds_record.fwy)
    if not only_if_missing or not details.direction:
        details.direction = vds_record.dir
    if not only_if_missing or not details.county:
        details.county = str(vds_record.county)
    if not only_if_missing or not details.station_type:
        details.station_type = vds_record.type
    if not only_if_missing or not details.ca_pm:
        details.ca_pm = str(vds_record.state_pm)
    if (not only_if_missing or details.abs_pm is None) and vds_record.abs_pm is not None:
        details.abs_pm = vds_record.abs_pm
    if (not only_if_missing or details.latitude is None) and vds_record.latitude is not None:
        details.latitude = vds_record.latitude
    if (not only_if_missing or details.longitude is None) and vds_record.longitude is not None:
        details.longitude = vds_record.longitude
    if synth_lanes and not details.lane_table and vds_record.lanes:
        details.lane_table = [
            {"Lane": str(i + 1), "Type": vds_record.type or "Mainline"} for i in range(vds_record.lanes)
        ]
    return details


def _parse_html_dict_table(table: Any, header_predicate: Any) -> List[Dict[str, str]]:
    """Extract dict rows from an HTML table once its header row matches header_predicate."""
    col_names: List[str] = []
    rows_out: List[Dict[str, str]] = []
    for tr in table.find_all("tr"):
        th_cells = [th.get_text(strip=True) for th in tr.find_all("th")]
        if th_cells and header_predicate(th_cells):
            col_names = th_cells
            continue
        if col_names:
            td_cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(td_cells) == len(col_names):
                rows_out.append(dict(zip(col_names, td_cells)))
    return rows_out


def parse_station_configuration_page(
    html_content: str,
    station_id: str = "",
    vds_record: Optional[VDSStationRecord] = None,
) -> StationLaneConfigDetails:
    """Parse PeMS station configuration HTML (content=sta_cfg)."""
    st_id = str(station_id).strip()
    details = StationLaneConfigDetails(
        station_id=st_id,
        url=build_station_config_url(st_id) if st_id else "",
    )
    populate_station_config_from_vds(details, vds_record)

    if not html_content:
        details.error = "PeMS server timed out or returned no content"
        return details

    soup = BeautifulSoup(html_content, "html.parser")
    page_text = soup.get_text()
    if "Application Halted" in page_text or "Missing numeric segment identifier" in page_text:
        details.error = "PeMS internal error: station uninstrumented or missing numeric segment identifier"
        return details

    if soup.title and soup.title.string:
        m_title = re.search(r">\s*(.*?VDS)\s*(\d+)\s*-\s*(.*?)\s*>", soup.title.string.strip())
        if m_title:
            if not details.station_type:
                details.station_type = m_title.group(1).strip()
            if not details.station_name:
                details.station_name = m_title.group(3).strip()

    for table in soup.find_all("table"):
        full_text = table.get_text(separator=" ", strip=True)
        th_lower = [th.get_text(strip=True).lower() for th in table.find_all("th")]

        if "roadway information" in full_text.lower():
            tsn_parsed = 0
            for tr in table.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) == 2 and cells[0]:
                    k, v = cells[0].strip(), cells[1].strip()
                    if "roadway information" not in k.lower():
                        details.tsn_roadway_info[k] = v
                        tsn_parsed += 1
                elif len(cells) == 1 and "not available" in cells[0].lower():
                    details.tsn_roadway_info["Status"] = "Not Available in PeMS"
            if tsn_parsed == 0 and "Status" not in details.tsn_roadway_info and "not available" in full_text.lower():
                details.tsn_roadway_info["Status"] = "Not Available in PeMS"

        elif any(h == "lane" for h in th_lower) and not any(
            h in ("status", "date", "start date", "lanes") for h in th_lower
        ):
            details.lane_table.extend(
                _parse_html_dict_table(table, lambda hs: any(h.lower() == "lane" for h in hs))
            )

        elif any("status" in h for h in th_lower) and any("lanes" in h for h in th_lower):
            details.change_log.extend(
                _parse_html_dict_table(
                    table, lambda hs: any("status" in h.lower() for h in hs) and any("lanes" in h.lower() for h in hs)
                )
            )

        elif any("start date" in h for h in th_lower) and any("end date" in h for h in th_lower):
            details.construction_history.extend(
                _parse_html_dict_table(table, lambda hs: any("start date" in h.lower() for h in hs))
            )

    if details.change_log:
        latest = details.change_log[-1]
        lat_v = latest.get("Lat") or latest.get("Latitude")
        lng_v = latest.get("Lng") or latest.get("Longitude") or latest.get("Lon")
        if lat_v and lng_v:
            try:
                lf, lg = float(lat_v), float(lng_v)
                if not (np.isnan(lf) or np.isnan(lg) or lf == 0):
                    details.latitude, details.longitude = lf, lg
            except (ValueError, TypeError):
                pass
        if latest.get("Name"):
            details.station_name = str(latest["Name"]).strip()
        ca_pm_v = latest.get("CA PM") or latest.get("State PM")
        if ca_pm_v:
            details.ca_pm = str(ca_pm_v).strip()
        if latest.get("Abs PM"):
            try:
                details.abs_pm = float(latest["Abs PM"])
            except (ValueError, TypeError):
                pass

    return details


def station_config_to_dict(st: StationLaneConfigDetails) -> Dict[str, Any]:
    """Serialize StationLaneConfigDetails to a JSON-compatible dictionary."""
    return asdict(st)


def dict_to_station_config(d: Dict[str, Any]) -> StationLaneConfigDetails:
    """Deserialize a dictionary to a StationLaneConfigDetails object."""
    def _opt_float(val: Any) -> Optional[float]:
        return float(val) if (val is not None and str(val).strip() != "") else None

    return StationLaneConfigDetails(
        station_id=str(d.get("station_id", "")),
        station_name=str(d.get("station_name", "")),
        district=d.get("district"),
        route=d.get("route"),
        direction=d.get("direction"),
        county=d.get("county"),
        station_type=d.get("station_type"),
        ca_pm=d.get("ca_pm"),
        abs_pm=_opt_float(d.get("abs_pm")),
        latitude=_opt_float(d.get("latitude")),
        longitude=_opt_float(d.get("longitude")),
        tsn_roadway_info=d.get("tsn_roadway_info", {}) or {},
        lane_table=d.get("lane_table", []) or [],
        change_log=d.get("change_log", []) or [],
        construction_history=d.get("construction_history", []) or [],
        url=str(d.get("url", "")),
        error=d.get("error"),
    )


def load_cached_station_config(
    station_id: str,
    cache_dir: Optional[str] = None,
    require_change_log: bool = True,
) -> Optional[StationLaneConfigDetails]:
    """Load cached station lane configuration details from disk if available."""
    cdir = cache_dir or DEFAULT_STATION_CONFIG_CACHE_DIR
    st_id = str(station_id).strip()
    cache_path = os.path.join(cdir, f"{st_id}.json")
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("error"):
            return None
        if require_change_log and not data.get("change_log"):
            return None
        return dict_to_station_config(data)
    except Exception as exc:
        LOGGER.debug(f"Failed to read cached config for station {st_id}: {exc}")
        return None


def save_cached_station_config(st: StationLaneConfigDetails, cache_dir: Optional[str] = None) -> str:
    """Save station lane configuration details to disk cache as JSON."""
    cdir = cache_dir or DEFAULT_STATION_CONFIG_CACHE_DIR
    os.makedirs(cdir, exist_ok=True)
    st_id = str(st.station_id).strip()
    cache_path = os.path.join(cdir, f"{st_id}.json")
    try:
        data = station_config_to_dict(st)
        data["cached_at"] = datetime.datetime.now().isoformat()
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        LOGGER.debug(f"Failed to save cached config for station {st_id}: {exc}")
    return cache_path


def get_spreadsheet_sensor_details(
    station_id: str,
    proj: Optional[ProjectRecord],
) -> Optional[Dict[str, Any]]:
    """Extract sensor before/after lanes and activation year from the master spreadsheet row."""
    if not proj or not proj.raw_row:
        return None
    st_clean = str(station_id).strip()
    row = proj.raw_row

    def _clean(v: Any) -> Optional[str]:
        if v is not None and pd.notna(v):
            s = str(v).strip()
            if s and s.lower() != "nan":
                return s
        return None

    for k in range(1, 12):
        sid_keys = (f"Sensor Station {k} ID", f"Sensor Station {k} ID ", f"Sensor {k} ID", f"sensor_station_{k}_id")
        if any(str(row.get(key, "")).strip().replace(".0", "") == st_clean for key in sid_keys):
            clean_b = _clean(
                row.get(f"Sensor Station {k}: number of lanes before")
                or row.get(f"Sensor Station {k}: number of lanes before ")
                or row.get(f"Sensor Station {k}: lanes before")
            )
            clean_a = _clean(
                row.get(f"Sensor Station {k}: number of lanes after")
                or row.get(f"Sensor Station {k}: number of lanes after ")
                or row.get(f"Sensor Station {k}: lanes after")
            )
            clean_yr = _clean(row.get(f"s{k}_new_lane_year") or row.get(f"Sensor {k} Activation Date for New Lane"))
            clean_type = _clean(row.get(f"Sensor Station {k}: lane type") or row.get(f"Sensor Station {k} Type"))
            clean_w = _clean(row.get(f"Sensor Station {k}: lane width (ft)"))
            if clean_b or clean_a or clean_yr:
                return {
                    "index": k,
                    "lanes_before": clean_b,
                    "lanes_after": clean_a,
                    "new_lane_year": clean_yr,
                    "lane_type": clean_type,
                    "lane_width": clean_w,
                }
    return None


def _parse_change_log_date(d_str: str) -> datetime.datetime:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(d_str.strip(), fmt)
        except Exception:
            continue
    return datetime.datetime.min


def format_station_lane_config_tooltip(
    st: StationLaneConfigDetails,
    vds_rec: Optional[VDSStationRecord] = None,
    project: Optional[ProjectRecord] = None,
) -> str:
    """Format high-density monospace hover tooltip summary for station lane configuration."""
    st_type = st.station_type or (vds_rec.type if vds_rec else "VDS")
    name = st.station_name or (vds_rec.name if vds_rec else "")
    fwy = st.route or (vds_rec.fwy if vds_rec else "-")
    d = st.direction or (vds_rec.dir if vds_rec else "")
    co = st.county or (vds_rec.county if vds_rec else "-")
    pm = st.ca_pm or (vds_rec.state_pm if vds_rec else "-")

    abs_pm = "-"
    if st.abs_pm is not None and not np.isnan(st.abs_pm):
        abs_pm = f"{st.abs_pm:.2f}"
    elif vds_rec and vds_rec.abs_pm is not None and not np.isnan(vds_rec.abs_pm):
        abs_pm = f"{vds_rec.abs_pm:.2f}"

    lines = [
        f"STATION {st.station_id} ({st_type})" + (f" - {name}" if name else ""),
        f"Fwy: {fwy} {d} | County: {co} PM {pm} (Abs: {abs_pm})",
        "-" * 46,
    ]

    curr_lanes = len(st.lane_table) if st.lane_table else (vds_rec.lanes if vds_rec else None)
    curr_str = f"{curr_lanes}" if curr_lanes is not None else "-"
    lines.append(f"CURRENT LANES: {curr_str}")
    if st.lane_table:
        for r in st.lane_table:
            l_tech = r.get("Sensor Tech")
            lines.append(f"  • Lane {r.get('Lane', '?')}: {r.get('Type', 'Lane')}" + (f" ({l_tech})" if l_tech else ""))
    elif vds_rec and vds_rec.lanes:
        lines.append(f"  • {vds_rec.lanes} mainline lanes recorded in VDS index")
    lines.append("-" * 46)

    lines.append("LANE COUNTS OVER TIME (PeMS Change Log):")
    if st.change_log:
        sorted_cl = sorted(st.change_log, key=lambda x: _parse_change_log_date(x.get("Date", "")))
        table_lines = format_dict_table_lines(sorted_cl, indent="  ")
        lines.extend(table_lines or ["  (No configuration records found)"])
    else:
        lines.append(f"  • Current: {curr_str} lanes")
        lines.append(
            "  (Historical configuration not cached; PeMS query timed out)"
            if st.error
            else "  (No configuration history recorded in PeMS)"
        )

    if st.construction_history:
        lines.extend(["-" * 46, "CONSTRUCTION HISTORY:"])
        lines.extend(format_dict_table_lines(st.construction_history, indent="  "))

    sheet_audit = get_spreadsheet_sensor_details(st.station_id, project) if project else None
    if sheet_audit:
        lines.extend([
            "-" * 46,
            f"SPREADSHEET AUDIT (Sensor #{sheet_audit['index']}):",
            f"  Lanes: {sheet_audit.get('lanes_before') or '-'} before -> {sheet_audit.get('lanes_after') or '-'} after",
        ])
        if sheet_audit.get("new_lane_year"):
            lines.append(f"  New Lane Activation: {sheet_audit['new_lane_year']}")
        if sheet_audit.get("lane_type"):
            lines.append(f"  Type: {sheet_audit['lane_type']}")

    if st.tsn_roadway_info and st.tsn_roadway_info.get("Status") != "Not Available in PeMS":
        rw = st.tsn_roadway_info.get("Road Width")
        lw = st.tsn_roadway_info.get("Lane Width")
        spd = st.tsn_roadway_info.get("Design Speed Limit")
        med = st.tsn_roadway_info.get("Inner Median Type")
        if rw or lw or spd or med:
            lines.extend(["-" * 46, "TSN ROADWAY CROSS-SECTION:"])
            if rw or lw:
                lines.append(f"  Road Width: {rw or '-'} | Lane Width: {lw or '-'}")
            if spd or med:
                lines.append(f"  Speed Limit: {spd or '-'} | Median: {med or '-'}")

    return "\n".join(lines)


def fetch_station_lane_configuration(
    station_id: str,
    client: Optional[PeMSClient] = None,
    vds_record: Optional[VDSStationRecord] = None,
    timeout: float = 60.0,
    use_cache: bool = True,
    cache_dir: Optional[str] = None,
    refresh_cache: bool = False,
) -> StationLaneConfigDetails:
    """Fetch and parse PeMS station lane config, change log, and TSN metrics with disk cache."""
    st_id = str(station_id).strip()
    if use_cache and not refresh_cache:
        cached = load_cached_station_config(st_id, cache_dir=cache_dir, require_change_log=False)
        if cached is not None:
            return populate_station_config_from_vds(cached, vds_record, only_if_missing=True)

    if client is None:
        client = PeMSClient()
        client.login()

    url = build_station_config_url(st_id)
    try:
        html_txt = client.get_text_content(url, timeout=timeout)
        if not html_txt:
            details = StationLaneConfigDetails(
                station_id=st_id,
                url=url,
                error=f"PeMS server timed out or returned no content (backend query took > {timeout:.0f}s)",
            )
            populate_station_config_from_vds(details, vds_record, synth_lanes=True)
            return details

        details = parse_station_configuration_page(html_txt, station_id=st_id, vds_record=vds_record)
        if use_cache and not details.error:
            save_cached_station_config(details, cache_dir=cache_dir)
        return details
    except Exception as exc:
        LOGGER.warning(f"Failed to fetch configuration for station {st_id}: {exc}")
        details = StationLaneConfigDetails(station_id=st_id, url=url, error=str(exc))
        populate_station_config_from_vds(details, vds_record)
        return details


def fetch_project_lane_configurations(
    project_id: str,
    master_csv_path: Optional[str] = None,
    client: Optional[PeMSClient] = None,
    metadata_dir: Optional[str] = None,
    buffer_mi: float = 0.2,
    include_corridor_vds: bool = True,
    cache_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch PeMS station lane config tables, TSN metrics, and HPMS summary for a project."""
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
            "error": f"Project ID '{pid_clean}' not found in {csv_file}",
        }

    if client is None:
        client = PeMSClient()
        client.login()

    vds_meta: Optional[IndexedVDSMetadata] = None
    try:
        vds_meta = load_vds_metadata(metadata_dir)
    except Exception as exc:
        LOGGER.debug(f"Could not load VDS metadata: {exc}")

    spreadsheet_sids: List[str] = []
    for p in matched_projects:
        for sid in p.sensor_stations:
            s_clean = str(sid).strip()
            if s_clean and s_clean not in spreadsheet_sids:
                spreadsheet_sids.append(s_clean)

    corridor_sids: List[str] = []
    if include_corridor_vds and vds_meta is not None:
        for p in matched_projects:
            comp = compare_project_stations(p, vds_meta=vds_meta, buffer_mi=buffer_mi, station_types=("ML", "HV"))
            for s in comp.found_stations:
                s_clean = str(s.id).strip()
                if s_clean and s_clean not in corridor_sids and s_clean not in spreadsheet_sids:
                    corridor_sids.append(s_clean)

    ordered_sids = spreadsheet_sids + [s for s in corridor_sids if s not in spreadsheet_sids]
    results = [
        fetch_station_lane_configuration(
            sid,
            client=client,
            vds_record=vds_meta.by_id.get(sid) if vds_meta else None,
            cache_dir=cache_dir,
        )
        for sid in ordered_sids
    ]

    hpms_summary: Optional[Dict[str, Any]] = None
    try:
        from hpms_interface import HPMSCorridorEngine

        engine = HPMSCorridorEngine(master_csv_path=csv_file)
        hpms_res = engine.inspect_project(pid_clean)
        if hpms_res and hpms_res.get("sections"):
            secs = hpms_res["sections"]
            hpms_summary = {
                "sections_count": hpms_res.get("hpms_sections_count", len(secs)),
                "predominant_lanes_2024": hpms_res.get("hpms_predominant_lanes_2024"),
                "avg_lanes_2024": hpms_res.get("hpms_avg_lanes_2024"),
                "lane_distribution_mi": hpms_res.get("hpms_lane_distribution_mi"),
                "avg_aadt_2024": hpms_res.get("hpms_avg_aadt_2024"),
                **decode_hpms_attributes(secs),
            }
    except Exception as exc:
        LOGGER.debug(f"HPMS corridor lookup failed: {exc}")

    uninstrumented_info: Optional[Dict[str, Any]] = None
    if not results and matched_projects:
        first_p = matched_projects[0]
        min_abs = min(first_p.abs_begin_pm or 0.0, first_p.abs_end_pm or 0.0)
        max_abs = max(first_p.abs_begin_pm or 0.0, first_p.abs_end_pm or 0.0)
        _, b_num = split_ca_pm(first_p.begin_pm or "0")
        uninstrumented_info = diagnose_uninstrumented_corridor(
            normalize_route(first_p.route),
            min_abs,
            max_abs,
            first_p.county,
            b_num,
            first_p.dist or "4",
            vds_meta,
        )

    return {
        "project_id": pid_clean,
        "projects": matched_projects,
        "stations": results,
        "spreadsheet_stations": spreadsheet_sids,
        "corridor_stations": corridor_sids,
        "hpms_summary": hpms_summary,
        "uninstrumented_info": uninstrumented_info,
    }


def _collect_station_ids_for_cache(
    projects: List[ProjectRecord],
    vds_meta: Optional[IndexedVDSMetadata],
    buffer_mi: float,
    station_types: Tuple[str, ...],
) -> List[str]:
    """Collect deduplicated spreadsheet + corridor station IDs across projects."""
    seen = set()
    ordered_sids: List[str] = []
    for p in projects:
        for sid in p.sensor_stations:
            s_clean = str(sid).strip()
            if s_clean and s_clean not in seen:
                seen.add(s_clean)
                ordered_sids.append(s_clean)
        if vds_meta is None:
            continue
        if p.abs_begin_pm is None or p.abs_end_pm is None:
            calc_b, calc_e = resolve_absolute_postmiles(p.route, p.county, p.begin_pm, p.end_pm)
            if calc_b is not None and calc_e is not None:
                if p.abs_begin_pm is None:
                    p.abs_begin_pm = calc_b
                if p.abs_end_pm is None:
                    p.abs_end_pm = calc_e
        eff_abs_b, eff_abs_e = p.effective_abs_pm_bounds
        for st in vds_meta.find_corridor_stations(
            district=p.dist,
            route=p.route,
            direction=p.pems_direction,
            begin_pm=p.begin_pm,
            end_pm=p.end_pm,
            abs_begin_pm=eff_abs_b,
            abs_end_pm=eff_abs_e,
            buffer_mi=buffer_mi,
            station_types=station_types,
            county=p.county,
        ):
            s_clean = str(st.id).strip()
            if s_clean and s_clean not in seen:
                seen.add(s_clean)
                ordered_sids.append(s_clean)
    return ordered_sids


def fetch_station_configs_batch(
    station_ids: List[str],
    vds_records: Optional[Dict[str, VDSStationRecord]] = None,
    client: Optional[PeMSClient] = None,
    timeout: float = 30.0,
    workers: int = 4,
    use_cache: bool = True,
    cache_dir: Optional[str] = None,
    refresh_cache: bool = False,
    on_complete: Optional[Callable[[int, int, str, Optional[StationLaneConfigDetails], Optional[Exception]], None]] = None,
) -> Dict[str, StationLaneConfigDetails]:
    """Fetch PeMS station lane configs using isolated per-worker sessions (avoids PHP session locking)."""
    if not station_ids:
        return {}

    total = len(station_ids)
    results: Dict[str, StationLaneConfigDetails] = {}
    records = vds_records or {}

    if client is not None or workers <= 1 or total == 1:
        single_client = client
        owns_client = False
        if single_client is None:
            single_client = PeMSClient()
            owns_client = True
        if not single_client.logged_in:
            single_client.login()

        try:
            for idx, sid in enumerate(station_ids, 1):
                try:
                    cfg = fetch_station_lane_configuration(
                        sid,
                        client=single_client,
                        vds_record=records.get(sid),
                        timeout=timeout,
                        use_cache=use_cache,
                        cache_dir=cache_dir,
                        refresh_cache=refresh_cache,
                    )
                    results[sid] = cfg
                    if on_complete:
                        on_complete(idx, total, sid, cfg, None)
                except Exception as exc:
                    if on_complete:
                        on_complete(idx, total, sid, None, exc)
        finally:
            if owns_client:
                try:
                    single_client.session.close()
                except Exception:
                    pass
        return results

    # Multi-worker mode: each worker uses its own PeMSClient & PHPSESSID to prevent PHP session-lock stalls.
    max_w = max(1, min(workers, total))
    first_client = PeMSClient()
    first_client.login()

    client_pool: queue.Queue = queue.Queue()
    client_pool.put(first_client)
    created_clients: List[PeMSClient] = [first_client]
    pool_lock = threading.Lock()

    def _acquire_client() -> PeMSClient:
        try:
            return client_pool.get_nowait()
        except queue.Empty:
            with pool_lock:
                if len(created_clients) < max_w:
                    wc = PeMSClient()
                    wc.login()
                    created_clients.append(wc)
                    return wc
            return client_pool.get()

    def _run_one(sid_to_fetch: str) -> StationLaneConfigDetails:
        wc = _acquire_client()
        try:
            return fetch_station_lane_configuration(
                sid_to_fetch,
                client=wc,
                vds_record=records.get(sid_to_fetch),
                timeout=timeout,
                use_cache=use_cache,
                cache_dir=cache_dir,
                refresh_cache=refresh_cache,
            )
        finally:
            client_pool.put(wc)

    try:
        with ThreadPoolExecutor(max_workers=max_w) as pool:
            future_map = {pool.submit(_run_one, sid): sid for sid in station_ids}
            completed = 0
            for fut in as_completed(future_map):
                sid = future_map[fut]
                completed += 1
                try:
                    cfg = fut.result()
                    results[sid] = cfg
                    if on_complete:
                        on_complete(completed, total, sid, cfg, None)
                except Exception as exc:
                    if on_complete:
                        on_complete(completed, total, sid, None, exc)
    finally:
        for wc in created_clients:
            try:
                wc.session.close()
            except Exception:
                pass

    return results


def download_all_lane_configurations(
    projects: Optional[List[ProjectRecord]] = None,
    master_csv_path: Optional[str] = None,
    project_id_filter: Optional[str] = None,
    dist_filter: Optional[str] = None,
    station_id_filter: Optional[str] = None,
    metadata_dir: Optional[str] = None,
    cache_dir: Optional[str] = None,
    buffer_mi: float = 3.0,
    station_types: Tuple[str, ...] = ("ALL",),
    workers: int = 4,
    timeout: float = 30.0,
    overwrite: bool = False,
    client: Optional[PeMSClient] = None,
    vds_meta: Optional[IndexedVDSMetadata] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Download and locally cache PeMS station lane configs across projects, a project, or a station."""
    cdir = os.path.abspath(cache_dir or DEFAULT_STATION_CONFIG_CACHE_DIR)
    os.makedirs(cdir, exist_ok=True)

    loaded_vds = vds_meta
    if loaded_vds is None:
        try:
            loaded_vds = load_vds_metadata(metadata_dir)
        except Exception as exc:
            LOGGER.debug(f"Could not load VDS metadata during cache pre-fetch: {exc}")

    if station_id_filter:
        st_clean = str(station_id_filter).strip()
        ordered_sids = [st_clean] if st_clean else []
        scope_label = f"Station {st_clean}"
    else:
        csv_file = master_csv_path or DEFAULT_MASTER_CSV
        target_projects = list(projects) if projects is not None else load_master_csv(csv_file)
        if project_id_filter:
            pid_clean = str(project_id_filter).strip()
            target_projects = [p for p in target_projects if str(p.project_id).strip() == pid_clean]
            scope_label = f"Project {pid_clean} ({len(target_projects)} directional records)"
        elif dist_filter:
            d_clean = normalize_district(dist_filter)
            target_projects = [p for p in target_projects if normalize_district(p.dist) == d_clean]
            scope_label = f"District {d_clean} ({len(target_projects)} project records)"
        else:
            scope_label = f"All projects ({len(target_projects)} project records)"
        ordered_sids = _collect_station_ids_for_cache(target_projects, loaded_vds, buffer_mi, station_types)

    already_cached: List[str] = []
    to_fetch: List[str] = []
    for sid in ordered_sids:
        if not overwrite and load_cached_station_config(sid, cache_dir=cdir, require_change_log=False) is not None:
            already_cached.append(sid)
        else:
            to_fetch.append(sid)

    if verbose:
        print("\n" + "=" * 76)
        print("PEMS STATION LANE CONFIGURATION CACHE DOWNLOAD")
        print(f"Scope:            {scope_label}")
        print(f"Cache Directory:  {cdir}")
        print(f"Unique Stations:  {len(ordered_sids)}")
        print(f"Already Cached:   {len(already_cached)}")
        print(f"To Download:      {len(to_fetch)}")
        print("=" * 76)

    if not to_fetch:
        if verbose:
            print("All target stations are already cached locally.")
        return {
            "total_stations": len(ordered_sids),
            "already_cached": len(already_cached),
            "downloaded": 0,
            "failed": 0,
            "cache_dir": cdir,
            "station_ids": ordered_sids,
            "failed_station_ids": [],
        }

    downloaded = 0
    failed_ids: List[str] = []
    vds_map = loaded_vds.by_id if loaded_vds else {}

    def _on_item(idx: int, total_cnt: int, sid: str, res: Optional[StationLaneConfigDetails], exc: Optional[Exception]) -> None:
        nonlocal downloaded
        if exc is not None:
            failed_ids.append(sid)
            status = f"FAILED ({exc})"
        elif res and not res.error:
            downloaded += 1
            status = "OK"
        else:
            failed_ids.append(sid)
            status = f"FAILED ({res.error})" if res else "FAILED"
        if verbose:
            print(f"  [{idx}/{total_cnt}] Station {sid}: {status}")

    fetch_station_configs_batch(
        to_fetch,
        vds_records=vds_map,
        client=client,
        timeout=timeout,
        workers=workers,
        use_cache=True,
        cache_dir=cdir,
        refresh_cache=overwrite,
        on_complete=_on_item,
    )

    failed_ids.sort()
    if verbose:
        print("-" * 76)
        print(f"Summary: {downloaded} downloaded, {len(already_cached)} skipped (cached), {len(failed_ids)} failed.")
        print("Tip: Run `pems_interface.py -p <PID> --web --fast` to render offline from cache.")
        print("=" * 76)

    return {
        "total_stations": len(ordered_sids),
        "already_cached": len(already_cached),
        "downloaded": downloaded,
        "failed": len(failed_ids),
        "cache_dir": cdir,
        "station_ids": ordered_sids,
        "failed_station_ids": failed_ids,
    }
