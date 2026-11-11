"""Batch Caltrans PeMS detector time-series and AADT download engine."""

from __future__ import annotations

import datetime
import io
import os
import re
import textwrap
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from pems.client import PeMSClient, build_station_aadt_url, build_station_timeseries_url
from pems.config import DEFAULT_MASTER_CSV, LOGGER
from pems.projects import load_master_csv
from pems.vds import get_indexed_vds_metadata

_VALID_QUANTITIES = {"flow", "occ", "speed", "vmt", "vht", "delay", "aadt"}
_VALID_GRANULARITIES = {"month", "day", "hour", "week", "year", "5min"}


def _parse_downloaded_year_table(content: str, filepath: str, is_xls: bool) -> Optional[pd.DataFrame]:
    """Parse an annual PeMS export (HTML-table XLS, binary Excel, TSV, or CSV) into a DataFrame."""
    low = content.lower()
    if is_xls and ("<table" in low or "<html" in low):
        try:
            html_dfs = pd.read_html(io.StringIO(content))
            if html_dfs:
                return html_dfs[0]
        except Exception:
            pass
    if is_xls:
        try:
            return pd.read_excel(filepath)
        except Exception:
            pass
    sep = "\t" if "\t" in content else ","
    return pd.read_csv(io.StringIO(content), sep=sep)


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
    export_format: str = "text",
) -> Dict[str, Any]:
    """Download detector data year-by-year from Caltrans PeMS and optionally merge across years."""
    st_id = re.sub(r"[^0-9]", "", str(station_id).strip())
    if not st_id:
        raise ValueError(f"Invalid station ID: {station_id}")

    data_type_clean = str(data_type).strip().lower()
    granularity_clean = str(granularity).strip().lower()
    if data_type_clean not in _VALID_QUANTITIES:
        raise ValueError(f"Unknown data type '{data_type}'. Allowed: {sorted(_VALID_QUANTITIES)}")
    if data_type_clean != "aadt" and granularity_clean not in _VALID_GRANULARITIES:
        raise ValueError(f"Unknown granularity '{granularity}'. Allowed: {sorted(_VALID_GRANULARITIES)}")

    fmt_norm = str(export_format).strip().lower()
    is_xls = fmt_norm in ("xls", "xlsx")
    ext = "xls" if is_xls else "txt"
    url_export = "xls" if is_xls else "text"

    e_year = end_year or datetime.datetime.now().year
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

    years_downloaded: List[int] = []
    years_no_data: List[int] = []
    all_dfs: List[pd.DataFrame] = []

    for year in range(start_year, e_year + 1):
        if data_type_clean == "aadt":
            url = build_station_aadt_url(st_id, year, year, export_format=url_export)
            fn = f"station_{st_id}_{year}_aadt.{ext}"
        else:
            url = build_station_timeseries_url(
                st_id, year, year, quantity=data_type_clean, granularity=granularity_clean, export_format=url_export
            )
            fn = f"station_{st_id}_{year}_{data_type_clean}_{granularity_clean}.{ext}"

        filepath = os.path.join(target_dir, fn)
        content: Optional[str] = None
        if os.path.exists(filepath) and not overwrite and pems_client.file_has_data_table(filepath):
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

            non_empty_lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
            if len(non_empty_lines) <= 1 and "<table" not in content.lower():
                years_no_data.append(year)
                if verbose:
                    print(f"  • {year}: Inactive / 0 records")
                continue

            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(content)

        try:
            df_year = _parse_downloaded_year_table(content, filepath, is_xls=is_xls)
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
        comb_fn = (
            f"station_{st_id}_aadt_all_years.csv"
            if data_type_clean == "aadt"
            else f"station_{st_id}_{data_type_clean}_{granularity_clean}_all_years.csv"
        )
        combined_file = os.path.join(target_dir, comb_fn)
        merged_df.to_csv(combined_file, index=False)
        if verbose:
            print("-" * 70)
            print(f"✓ Consolidated {total_records} total records across {len(years_downloaded)} active years into:")
            print(f"  -> {combined_file}")
            print("=" * 70 + "\n")
    elif verbose:
        print("-" * 70)
        if not years_downloaded:
            print(f"⚠ No active data found for Station {st_id} in years {start_year} - {e_year}.")
        else:
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
        "output_dir": target_dir,
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
    export_format: str = "text",
) -> Dict[str, Any]:
    """Locate detector stations associated with a project and download PeMS data for each station."""
    projs = load_master_csv(master_csv_path or DEFAULT_MASTER_CSV)
    target_pid = str(project_id).strip()
    matched = [p for p in projs if p.project_id == target_pid]
    if not matched:
        raise ValueError(f"Project ID '{target_pid}' not found in master project dataset.")

    st_ids: List[str] = []
    seen = set()
    for p in matched:
        for sid in p.sensor_stations:
            clean_id = re.sub(r"[^0-9]", "", str(sid).strip())
            if clean_id and clean_id not in seen:
                seen.add(clean_id)
                st_ids.append(clean_id)

    if not st_ids:
        try:
            vds = get_indexed_vds_metadata()
            for p in matched:
                eff_abs_b, eff_abs_e = p.effective_abs_pm_bounds
                for s in vds.find_corridor_stations(
                    district=p.dist,
                    route=p.route,
                    direction=p.direction,
                    begin_pm=p.begin_pm,
                    end_pm=p.end_pm,
                    abs_begin_pm=eff_abs_b,
                    abs_end_pm=eff_abs_e,
                    county=p.county,
                    station_types=("ML", "HV"),
                ):
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
            "output_dir": os.path.abspath(output_dir),
        }

    proj_dir = os.path.join(os.path.abspath(output_dir), f"project_{target_pid}")
    os.makedirs(proj_dir, exist_ok=True)

    if verbose:
        print("\n" + "=" * 70)
        print(f"PEMS PROJECT DETECTOR DOWNLOAD: Project {target_pid}")
        if matched[0].project_description:
            print(textwrap.fill(matched[0].project_description, width=68, initial_indent="  Description:        ", subsequent_indent="                      "))
        print(f"Associated Stations ({len(st_ids)}): {', '.join(st_ids)}")
        print(f"Export Format: {export_format.upper()}")
        print(f"Target Directory: {proj_dir}")
        print("=" * 70)

    client = PeMSClient()
    client.login()

    station_results = {
        sid: download_station_data(
            station_id=sid,
            data_type=data_type,
            granularity=granularity,
            start_year=start_year,
            end_year=end_year,
            output_dir=os.path.join(proj_dir, sid),
            client=client,
            combine=combine,
            overwrite=overwrite,
            pause_sec=pause_sec,
            verbose=verbose,
            export_format=export_format,
        )
        for sid in st_ids
    }

    return {
        "project_id": target_pid,
        "project_description": matched[0].project_description,
        "station_results": station_results,
        "total_stations": len(st_ids),
        "output_dir": proj_dir,
    }
