"""Open-to-traffic inference via PeMS station config step-changes and daily flow telemetry."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

from pems.client import PeMSClient, build_station_summary_url, build_station_timeseries_url
from pems.config import DEFAULT_MASTER_CSV, LOGGER
from pems.normalize import (
    build_date_from_parts,
    find_column,
    normalize_district,
    parse_iso_date,
)
from pems.projects import ProjectRecord, load_master_csv, save_master_csv_with_backup
from pems.reporting import write_dataframe_report
from pems.vds import IndexedVDSMetadata, get_indexed_vds_metadata


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
    change_type: str
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
    activation_type: str
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
    inferred_date: Optional[str]
    inferred_year: Optional[int]
    confidence: str
    method: str
    evidence: str
    station_id: Optional[str] = None
    station_name: Optional[str] = None
    candidate_events: List[Dict[str, Any]] = field(default_factory=list)
    stations_checked: List[str] = field(default_factory=list)


def _opt_float_cell(cells: List[str], idx: int) -> Optional[float]:
    if len(cells) <= idx:
        return None
    try:
        return float(cells[idx].strip())
    except ValueError:
        return None


def parse_station_summary_configuration(html_content: str) -> List[StationConfigRecord]:
    """Parse chronological configuration history table from PeMS Station Summary HTML."""
    if not html_content:
        return []
    soup = BeautifulSoup(html_content, "html.parser")
    records: List[StationConfigRecord] = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not (any("lanes" in h for h in headers) and any("status" in h for h in headers)):
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 4 or "/" not in cells[0]:
                continue
            try:
                dt = datetime.datetime.strptime(cells[0].strip(), "%m/%d/%Y").date()
                lanes_str = cells[3].strip()
                digits = re.findall(r"\d", lanes_str)
                if digits:
                    lane_count = int(lanes_str) if (lanes_str.isdigit() and len(lanes_str) == 1) else len(digits)
                else:
                    lane_count = 0
                records.append(
                    StationConfigRecord(
                        date=dt,
                        date_str=dt.strftime("%Y-%m-%d"),
                        status=cells[1].strip(),
                        name=cells[2].strip() if len(cells) > 2 else "",
                        lanes_str=lanes_str,
                        lane_count=lane_count,
                        ca_pm=cells[4].strip() if len(cells) > 4 else "",
                        abs_pm=cells[5].strip() if len(cells) > 5 else "",
                        length=_opt_float_cell(cells, 6),
                        lat=_opt_float_cell(cells, 7),
                        lng=_opt_float_cell(cells, 8),
                    )
                )
            except Exception:
                continue

    records.sort(key=lambda x: x.date)
    return records


def detect_lane_config_step_changes(
    config_records: List[StationConfigRecord],
    station_id: str = "",
    min_date: Optional[datetime.date] = None,
    max_date: Optional[datetime.date] = None,
) -> List[StationConfigChange]:
    """Identify detector commissioning, lane count increments, and status reactivations."""
    events: List[StationConfigChange] = []
    prev_lane_count: Optional[int] = None
    prev_status: Optional[str] = None
    prev_lanes_str: Optional[str] = None
    first_active_seen = False

    for r in config_records:
        dt = r.date
        if min_date and dt < min_date:
            prev_lane_count, prev_status, prev_lanes_str = r.lane_count, r.status, r.lanes_str
            if r.status.lower() == "active":
                first_active_seen = True
            continue
        if max_date and dt > max_date:
            continue

        status_low = r.status.lower()
        if status_low == "active" and not first_active_seen:
            first_active_seen = True
            events.append(
                StationConfigChange(
                    station_id=station_id,
                    change_date=dt,
                    change_type="DETECTOR_COMMISSIONED",
                    lanes_before=0,
                    lanes_after=r.lane_count,
                    lanes_str_before="",
                    lanes_str_after=r.lanes_str,
                    status=r.status,
                    name=r.name,
                    ca_pm=r.ca_pm,
                    abs_pm=r.abs_pm,
                    evidence=(
                        f"Detector station {station_id} commissioned as Active on {r.date_str} "
                        f"with {r.lane_count} lanes (mask: '{r.lanes_str}')"
                    ),
                )
            )
        elif prev_lane_count is not None and r.lane_count > prev_lane_count and status_low == "active":
            events.append(
                StationConfigChange(
                    station_id=station_id,
                    change_date=dt,
                    change_type="LANE_CONFIG_INCREMENT",
                    lanes_before=prev_lane_count,
                    lanes_after=r.lane_count,
                    lanes_str_before=prev_lanes_str,
                    lanes_str_after=r.lanes_str,
                    status=r.status,
                    name=r.name,
                    ca_pm=r.ca_pm,
                    abs_pm=r.abs_pm,
                    evidence=(
                        f"Station {station_id} lane configuration incremented from {prev_lane_count} "
                        f"to {r.lane_count} lanes on {r.date_str} (mask: '{r.lanes_str}')"
                    ),
                )
            )
        elif prev_status and prev_status.lower() in ("disabled", "inactive") and status_low == "active":
            events.append(
                StationConfigChange(
                    station_id=station_id,
                    change_date=dt,
                    change_type="STATUS_CHANGE",
                    lanes_before=prev_lane_count,
                    lanes_after=r.lane_count,
                    lanes_str_before=prev_lanes_str,
                    lanes_str_after=r.lanes_str,
                    status=r.status,
                    name=r.name,
                    ca_pm=r.ca_pm,
                    abs_pm=r.abs_pm,
                    evidence=(
                        f"Station {station_id} transitioned from {prev_status} to Active on "
                        f"{r.date_str} with {r.lane_count} lanes"
                    ),
                )
            )

        prev_lane_count, prev_status, prev_lanes_str = r.lane_count, r.status, r.lanes_str

    return events


def parse_daily_flow_telemetry(tsv_text: str) -> List[Dict[str, Any]]:
    """Parse PeMS daily flow telemetry TSV (Day, Flow, # Lane Points, % Observed)."""
    if not tsv_text:
        return []
    records: List[Dict[str, Any]] = []
    for line in tsv_text.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or "/" not in parts[0]:
            continue
        try:
            dt = datetime.datetime.strptime(parts[0].strip(), "%m/%d/%Y").date()
            records.append({
                "date": dt,
                "date_str": dt.strftime("%Y-%m-%d"),
                "flow": float(parts[1].replace(",", "").strip()),
                "lane_points": int(float(parts[2].replace(",", "").strip())),
                "obs_pct": float(parts[3].replace(",", "").replace("%", "").strip()),
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
    consecutive_days: int = 3,
) -> Optional[FlowActivationResult]:
    """Corroborate lane additions or detector commissioning using daily flow & # Lane Points."""
    url = build_station_timeseries_url(station_id, start_year=target_year, end_year=target_year, quantity="flow", granularity="day")
    try:
        tsv_text = client.get_text_content(url)
    except Exception as exc:
        LOGGER.debug(f"Failed to fetch flow telemetry for station {station_id} in {target_year}: {exc}")
        return None

    records = parse_daily_flow_telemetry(tsv_text or "")
    if not records:
        return None

    for i in range(1, len(records)):
        curr, prev = records[i], records[i - 1]
        if candidate_date is not None and abs((curr["date"] - candidate_date).days) > 14:
            continue
        if curr["lane_points"] >= prev["lane_points"] + 200:
            subsequent = records[i : i + 5]
            if len(subsequent) >= 3 and np.median([r["lane_points"] for r in subsequent]) >= prev["lane_points"] + 200:
                lp_prev, lp_curr = prev["lane_points"], curr["lane_points"]
                lanes_b, lanes_a = round(lp_prev / 288.0), round(lp_curr / 288.0)
                return FlowActivationResult(
                    station_id=station_id,
                    activation_date=curr["date"],
                    activation_type="LANE_POINTS_STEP_CHANGE",
                    lane_points_before=lp_prev,
                    lane_points_after=lp_curr,
                    flow_before=prev["flow"],
                    flow_after=curr["flow"],
                    obs_percent=curr["obs_pct"],
                    evidence=(
                        f"Daily detector telemetry at station {station_id} showed # Lane Points stepping from "
                        f"{lp_prev} (~{lanes_b} lanes) to {lp_curr} (~{lanes_a} lanes) on {curr['date_str']} "
                        f"(flow: {curr['flow']:,.0f} veh/day, {curr['obs_pct']:.1f}% observed)"
                    ),
                )

    for i in range(len(records) - consecutive_days + 1):
        window = records[i : i + consecutive_days]
        first = window[0]
        dt = first["date"]
        if (min_date and dt < min_date) or (max_date and dt > max_date):
            continue
        if all(r["flow"] >= flow_threshold and r["obs_pct"] > 0 for r in window):
            prior = records[max(0, i - 7) : i]
            if not prior or any(r["flow"] < flow_threshold or r["obs_pct"] == 0 for r in prior):
                return FlowActivationResult(
                    station_id=station_id,
                    activation_date=dt,
                    activation_type="SUSTAINED_FLOW_START",
                    lane_points_before=0,
                    lane_points_after=first["lane_points"],
                    flow_before=0.0,
                    flow_after=first["flow"],
                    obs_percent=first["obs_pct"],
                    evidence=(
                        f"Detector {station_id} recorded initial sustained mainline flow starting {first['date_str']} "
                        f"({first['flow']:,.0f} veh/day, {first['obs_pct']:.1f}% observed, {first['lane_points']} lane points)"
                    ),
                )

    return None


def infer_project_open_to_traffic(
    project_or_id: Any,
    client: Optional[PeMSClient] = None,
    master_csv_path: Optional[str] = None,
    vds_meta: Optional[IndexedVDSMetadata] = None,
    check_telemetry: bool = True,
    buffer_mi: float = 0.1,
    verbose: bool = True,
) -> OpenToTrafficInference:
    """Infer Open-to-Traffic date from PeMS station config step-changes and daily telemetry."""
    if isinstance(project_or_id, ProjectRecord):
        proj = project_or_id
    else:
        pid_str = str(project_or_id).strip()
        matching = [p for p in load_master_csv(master_csv_path) if str(p.project_id).strip() == pid_str]
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

    candidate_stations: List[str] = []
    for sid in proj.sensor_stations:
        s_clean = str(sid).strip()
        if s_clean and s_clean not in candidate_stations:
            candidate_stations.append(s_clean)

    if not candidate_stations:
        meta = vds_meta or get_indexed_vds_metadata()
        if meta:
            try:
                eff_abs_b, eff_abs_e = proj.effective_abs_pm_bounds
                corr_recs = meta.find_corridor_stations(
                    district=proj.dist,
                    route=proj.route,
                    direction=proj.pems_direction,
                    begin_pm=proj.begin_pm,
                    end_pm=proj.end_pm,
                    abs_begin_pm=eff_abs_b,
                    abs_end_pm=eff_abs_e,
                    county=proj.county,
                    buffer_mi=buffer_mi,
                    station_types=("ML", "HV"),
                )
                for cr in corr_recs[:6]:
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
            evidence=(
                f"No PeMS detector stations found on Route {proj.route} {proj.direction} "
                f"({proj.county}) corridor (uninstrumented corridor)."
            ),
            stations_checked=[],
        )

    c_begin_parsed = build_date_from_parts(
        proj.raw_row.get("construction_begin_year"),
        proj.raw_row.get("construction_begin_month"),
        proj.raw_row.get("construction_begin_day"),
        default_month=1,
        default_day=1,
    ) or parse_iso_date(proj.construction_begin_date, 1, 1)
    min_date = c_begin_parsed - datetime.timedelta(days=60) if c_begin_parsed else datetime.date(2000, 1, 1)
    max_date = datetime.date.today() + datetime.timedelta(days=180)

    pems_client = client or PeMSClient()
    if not pems_client.logged_in:
        pems_client.login()

    all_events: List[Dict[str, Any]] = []
    stations_checked: List[str] = []
    for sid in candidate_stations:
        stations_checked.append(sid)
        try:
            html_txt = pems_client.get_text_content(build_station_summary_url(sid))
        except Exception as exc:
            LOGGER.debug(f"Failed to fetch summary HTML for station {sid}: {exc}")
            continue
        configs = parse_station_summary_configuration(html_txt or "")
        if not configs:
            continue
        for sc in detect_lane_config_step_changes(configs, station_id=sid, min_date=min_date, max_date=max_date):
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
            evidence=(
                f"Checked {len(stations_checked)} corridor stations ({', '.join(stations_checked[:5])}); "
                "no configuration changes or detector activation events identified."
            ),
            stations_checked=stations_checked,
        )

    if check_telemetry:
        for ev in all_events:
            tel_res = detect_flow_telemetry_activation(
                client=pems_client,
                station_id=ev["station_id"],
                target_year=ev["date"].year,
                candidate_date=ev["date"],
                min_date=min_date,
                max_date=max_date,
            )
            if tel_res is not None:
                ev["telemetry"] = tel_res

    date_counts: Dict[datetime.date, int] = {}
    for ev in all_events:
        date_counts[ev["date"]] = date_counts.get(ev["date"], 0) + 1

    base_scores = {"LANE_CONFIG_INCREMENT": 100, "DETECTOR_COMMISSIONED": 70, "STATUS_CHANGE": 40}
    tel_bonus = {"LANE_CONFIG_INCREMENT": 50, "DETECTOR_COMMISSIONED": 40}

    best_event = None
    best_score = -1
    for ev in all_events:
        ev_type = ev["event_type"]
        score = base_scores.get(ev_type, 0)
        if ev.get("telemetry") is not None:
            score += tel_bonus.get(ev_type, 0)
        consensus = date_counts.get(ev["date"], 1)
        if consensus > 1:
            score += 25 * (consensus - 1)
        if c_begin_parsed:
            days_after = (ev["date"] - c_begin_parsed).days
            if 60 <= days_after <= 1825:
                score += 30
            elif days_after < 0:
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

    conf = "LOW"
    method = best_event["event_type"]
    if best_score >= 120:
        conf = "HIGH"
        if best_event.get("telemetry"):
            method = "CORROBORATED_TELEMETRY"
    elif best_score >= 70:
        conf = "MEDIUM"

    narrative_parts = [best_event["evidence"]]
    if best_event.get("telemetry"):
        narrative_parts.append(best_event["telemetry"].evidence)
    if date_counts.get(best_event["date"], 1) > 1:
        narrative_parts.append(
            f"Corroborated across {date_counts[best_event['date']]} distinct stations along the corridor "
            f"on {best_event['date_str']}."
        )

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
        evidence=" | ".join(narrative_parts),
        station_id=best_event["station_id"],
        station_name=best_event.get("station_name"),
        candidate_events=all_events,
        stations_checked=stations_checked,
    )


def apply_open_date_updates_to_dataframe(
    df: pd.DataFrame,
    inferences: List[OpenToTrafficInference],
    allowed_confidences: Tuple[str, ...] = ("HIGH", "MEDIUM"),
) -> Tuple[pd.DataFrame, int]:
    """Apply inferred open-to-traffic dates to master dataframe columns."""
    updated = df.copy()
    col_pid = find_column(updated, "Project ID")
    col_dir = find_column(updated, "Direction")
    col_open = find_column(updated, "Open to Traffic Date")
    col_open_fmt = find_column(updated, "Open to Traffic Date (formatted YYYY-MM-DD)")
    col_yr = find_column(updated, "open_traffic_year")
    col_mo = find_column(updated, "open_traffic_month")
    col_day = find_column(updated, "open_traffic_day")
    col_s1_act = find_column(updated, "Sensor 1 Activation Date for New Lane")
    col_s1_id = find_column(updated, "Sensor Station 1 ID")

    allowed_set = {c.upper().strip() for c in allowed_confidences}
    update_count = 0
    for c in (col_open, col_open_fmt, col_yr, col_mo, col_day, col_s1_act):
        if c and c in updated.columns:
            updated[c] = updated[c].astype(object)

    for inf in inferences:
        if not inf.inferred_date or inf.confidence.upper() not in allowed_set:
            continue
        mask = updated[col_pid].astype(str).str.strip() == str(inf.project_id).strip()
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
            if col_s1_act:
                curr_act = str(updated.at[idx, col_s1_act]).strip()
                s1_id_val = str(updated.at[idx, col_s1_id]).strip() if col_s1_id else ""
                if not curr_act or curr_act.lower() in ("nan", "none", "") or (
                    inf.station_id and inf.station_id in s1_id_val
                ):
                    updated.at[idx, col_s1_act] = inf.inferred_date

    return updated, update_count


def apply_open_date_to_master_csv(
    inferences: List[OpenToTrafficInference],
    master_csv_path: Optional[str] = None,
    output_csv_path: Optional[str] = None,
    allowed_confidences: Tuple[str, ...] = ("HIGH", "MEDIUM"),
) -> Tuple[str, str, int]:
    """Commit inferred open dates to the master CSV file with an automated backup."""
    target_in = master_csv_path or DEFAULT_MASTER_CSV
    raw_df = pd.read_csv(target_in, low_memory=False)
    updated_df, count = apply_open_date_updates_to_dataframe(
        raw_df, inferences, allowed_confidences=allowed_confidences
    )
    backup_path, target_out = save_master_csv_with_backup(
        target_in, updated_df, output_csv_path=output_csv_path, use_iso_timestamp=True
    )
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
    verbose: bool = True,
) -> List[OpenToTrafficInference]:
    """Execute end-to-end Open-to-Traffic Date inference pipeline across highway projects."""
    csv_file = master_csv_path or DEFAULT_MASTER_CSV
    target_projects = list(projects) if projects is not None else load_master_csv(csv_file)

    if project_id_filter:
        pid_clean = str(project_id_filter).strip()
        target_projects = [p for p in target_projects if str(p.project_id).strip() == pid_clean]
    elif dist_filter:
        d_clean = normalize_district(dist_filter)
        target_projects = [p for p in target_projects if normalize_district(p.dist) == d_clean]
    else:
        missing_or_vague = [
            p
            for p in target_projects
            if not (p.open_to_traffic_date and str(p.open_to_traffic_date).strip().lower() not in ("nan", "none", ""))
            or (len(str(p.open_to_traffic_date).strip()) <= 4 and str(p.open_to_traffic_date).strip().isdigit())
        ]
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
            p, client=pems_client, master_csv_path=csv_file, vds_meta=vds_meta, check_telemetry=check_telemetry, verbose=False
        )
        inferences.append(inf)
        if verbose:
            print(f" -> Inferred: {inf.inferred_date} ({inf.confidence})" if inf.inferred_date else f" -> {inf.confidence}")

    print("\n" + "=" * 80)
    print("INFERENCE RESULTS SUMMARY")
    print("=" * 80)
    print(f"{'PID':<11} {'Route':<7} {'Dir':<4} {'Sheet Open':<12} {'Inferred Open':<14} {'Conf':<8} {'Method':<22} {'Station':<8}")
    print("-" * 80)
    for inf in inferences:
        print(
            f"{inf.project_id:<11} {inf.route:<7} {inf.direction:<4} "
            f"{str(inf.current_open_date or 'None')[:10]:<12} {str(inf.inferred_date or 'None'):<14} "
            f"{inf.confidence:<8} {inf.method:<22} {str(inf.station_id or '-'):<8}"
        )
    print("=" * 80)

    resolved = [inf for inf in inferences if inf.inferred_date]
    if resolved:
        print("\nDETAILED EVIDENCE LOG:")
        for inf in resolved:
            print(f"  • Project {inf.project_id} (Route {inf.route} {inf.direction}):")
            print(f"    Inferred Open Date: {inf.inferred_date} [Confidence: {inf.confidence}]")
            print(f"    Primary Station:    {inf.station_id} ({inf.station_name or 'VDS'})")
            print(f"    Evidence:           {inf.evidence}")

    if output_report_path:
        report_df = pd.DataFrame([
            {
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
            }
            for inf in inferences
        ])
        write_dataframe_report(
            report_df,
            output_report_path,
            markdown_title="PeMS Open-to-Traffic Date Inference Report",
            header_lines=[
                f"**Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
                "**Method:** PeMS Lane Configuration Step-Change & Detector Telemetry (Option 1)  ",
                f"**Telemetry Verification:** {'Enabled' if check_telemetry else 'Disabled'}  ",
            ],
        )
        print(f"\n✓ Exported inference report -> {output_report_path}")

    if apply_updates:
        backup_p, target_p, count = apply_open_date_to_master_csv(
            inferences=inferences, master_csv_path=csv_file, output_csv_path=output_csv_path
        )
        print(f"\n✓ Successfully updated {count} project dates in master CSV at: {target_p}")
        print(f"  Safety backup preserved at: {backup_p}")
    else:
        print("\n[DRY RUN COMPLETE]: No changes have been written to the spreadsheet.")
        print("To commit these inferred dates to the master CSV, run with: --find-open-date --apply")

    return inferences
