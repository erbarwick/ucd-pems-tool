"""Interactive Folium / Leaflet map builders for projects and detector stations."""

from __future__ import annotations

import datetime
import html
import os
import webbrowser
from typing import Any, Dict, List, Optional, Tuple, Union

import folium
import numpy as np
from branca.element import MacroElement
from jinja2 import Template

from pems.client import build_station_aadt_url, build_station_flow_url, build_station_summary_url
from pems.config import (
    DISTRICT_COLORS,
    LOGGER,
    MONO_FONT,
    STATION_LAYER_LABELS,
    STATION_LAYER_ORDER,
    STATION_TYPE_COLORS,
    STATION_TYPE_NAMES,
)
from pems.geo import IndexedGeoReference, get_indexed_geo_ref, pm_to_coordinates
from pems.lane_config import load_cached_station_config
from pems.normalize import format_pm, normalize_district, split_ca_pm
from pems.projects import ProjectPeMSQueryBuilder, ProjectRecord, get_project_corridor_geometry, load_master_csv
from pems.vds import IndexedVDSMetadata, VDSStationRecord, get_indexed_vds_metadata, load_vds_metadata

_LEAFLET_BASE_CSS = """
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
<script>
  if (typeof L === 'undefined') {
    document.write('<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.3/leaflet.js"><\\/script>');
    document.write('<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.3/leaflet.css"/>');
  }
</script>
"""


def make_project_tooltip_html(project: ProjectRecord, length_mi: float = 0.0) -> str:
    """Generate compact hover tooltip HTML for a road path."""
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
    """Generate detailed click popup card HTML with monospaced styling and live PeMS links."""
    urls = builder.generate_all_urls()
    raw = project.raw_row
    tot_b = html.escape(str(raw.get("Total Lanes Before", raw.get("total_lanes_before", "?"))))
    tot_a = html.escape(str(raw.get("Total Lanes After", raw.get("total_lanes_after", "?"))))
    dir_b = html.escape(str(raw.get("dir_lanes_before", "?")))
    dir_a = html.escape(str(raw.get("dir_lanes_after", "?")))
    added_lm = html.escape(str(raw.get("Total Added Lanes (Lane Miles)", raw.get("New GP Lanes (Lane Miles)", "?"))))
    facility = html.escape(str(raw.get("Facility Class checked by Lu") or raw.get("Facility Class") or raw.get("facility_class") or "Freeway").strip())
    area_type = html.escape(
        str(
            raw.get("area_type")
            or raw.get("Area Type")
            or raw.get('Area Type ("Urban", "Suburban", "Exurban", "Rural", or "Unknown")')
            or "Unknown"
        )
        .strip()
        .capitalize()
    )
    desc = html.escape(str(project.project_description or raw.get("Project Description", "")).strip())

    b_pm = format_pm(project.begin_pm)
    e_pm = format_pm(project.end_pm)
    abs_b = format_pm(project.abs_begin_pm)
    abs_e = format_pm(project.abs_end_pm)
    abs_warning = (
        ' <span style="color:#b06000; font-weight:bold;">[BACKWARDS - USING CA PM]</span>'
        if project.has_inverted_abs_pm
        else ""
    )

    sensor_links = [
        f'<a href="{urls["station_summary_urls"].get(s_id, f"https://pems.dot.ca.gov/?dnode=VDS&station_id={s_id}")}" '
        f'target="_blank" style="color: #0000ee; text-decoration: underline;">{html.escape(s_id)}</a>'
        for s_id in project.sensor_stations
    ]
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
        {f'<tr style="border-bottom: 1px solid #ccc;"><td style="padding: 2px 4px; color: #333; border-right: 1px solid #ccc;">Abs PMs:</td><td style="padding: 2px 4px;">{abs_b} &rarr; {abs_e}{abs_warning}</td></tr>' if abs_b else ''}
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


def classify_station_relative_to_project(
    station: VDSStationRecord,
    project: ProjectRecord,
    tolerance_mi: float = 0.05,
) -> Tuple[bool, str, Optional[float]]:
    """Direction-aware check of whether a detector station lies within project postmile bounds."""
    is_descending = project.pems_direction in ("S", "W")

    if project.has_abs_pm_bounds and station.abs_pm is not None and not np.isnan(station.abs_pm):
        min_p = min(float(project.abs_begin_pm), float(project.abs_end_pm))  # type: ignore[arg-type]
        max_p = max(float(project.abs_begin_pm), float(project.abs_end_pm))  # type: ignore[arg-type]
        st_p = float(station.abs_pm)
    else:
        _, b_num = split_ca_pm(project.begin_pm)
        _, e_num = split_ca_pm(project.end_pm)
        _, st_num = station.ca_pm_prefix_and_num
        if np.isnan(b_num) or np.isnan(e_num) or np.isnan(st_num):
            return False, "Unable to determine postmile bounds", None
        min_p, max_p, st_p = min(b_num, e_num), max(b_num, e_num), st_num

    if min_p - tolerance_mi <= st_p <= max_p + tolerance_mi:
        if min_p <= st_p <= max_p:
            return True, f"PM {min_p:.2f} - {max_p:.2f}", 0.0
        return True, f"Boundary tolerance (±{tolerance_mi:.2f} mi)", 0.0
    if st_p < min_p:
        delta = min_p - st_p
        pos_label = "downstream / after end" if is_descending else "upstream / before start"
        return False, f"{delta:.2f} mi {pos_label}", delta
    delta = st_p - max_p
    pos_label = "upstream / before start" if is_descending else "downstream / after end"
    return False, f"{delta:.2f} mi {pos_label}", delta


def make_station_tooltip_html(
    station: VDSStationRecord,
    is_in_bounds: bool,
    bounds_note: str,
    is_in_sheet: bool,
    delta_mi: Optional[float] = None,
) -> str:
    """Build compact monospaced hover tooltip HTML displaying station metadata and status."""
    type_name = STATION_TYPE_NAMES.get(station.type, station.type)
    status_badge = (
        '<span style="color: #15803d; font-weight: bold;">✓ IN PROJECT BOUNDS</span>'
        if is_in_bounds
        else f'<span style="color: #b91c1c; font-weight: bold;">⚠ OUTSIDE BOUNDS ({bounds_note})</span>'
    )
    if is_in_sheet:
        sheet_badge = (
            '<span style="color: #15803d; font-weight: bold;">✓ Listed in Master CSV (Verified)</span>'
            if is_in_bounds
            else '<span style="color: #b91c1c; font-weight: bold;">⚠ Listed in Master CSV (Discrepancy)</span>'
        )
    else:
        sheet_badge = '<span style="color: #555555;">Not listed in Master CSV</span>'

    abs_str = f" · Abs PM {station.abs_pm:.2f}" if station.abs_pm is not None else ""
    return f"""<div style="font-family: {MONO_FONT}; font-size: 11.5px; line-height: 1.4; padding: 4px; min-width: 240px; color: #000; background: #fff;">
  <div style="font-weight: bold; font-size: 12px; border-bottom: 1px solid #000; padding-bottom: 3px; margin-bottom: 4px;">
    {status_badge}
  </div>
  <div><strong>Station ID:</strong> {station.id} &nbsp;({station.type} · {type_name})</div>
  <div><strong>Location:</strong> {station.name or 'N/A'}</div>
  <div><strong>Freeway:</strong> Route {station.fwy} {station.dir} · Dist {station.district}</div>
  <div><strong>Postmile:</strong> State PM {station.state_pm}{abs_str}</div>
  <div><strong>Lanes:</strong> {station.lanes or 'N/A'} &nbsp;|&nbsp; <strong>Length:</strong> {f'{station.length:.2f} mi' if station.length else 'N/A'}</div>
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
    delta_mi: Optional[float] = None,
) -> str:
    """Build detailed click popup HTML with PeMS links and ground-truth corridor context."""
    type_name = STATION_TYPE_NAMES.get(station.type, station.type)
    summary_url = build_station_summary_url(station.id)

    curr_year = datetime.datetime.now().year
    s_yr, e_yr = curr_year - 5, curr_year
    if project and project.construction_begin_date:
        try:
            s_yr = int(project.construction_begin_date.split("-")[0])
        except (ValueError, IndexError):
            pass
    if project and project.open_to_traffic_date:
        try:
            e_yr = min(curr_year, int(project.open_to_traffic_date.split("-")[0]) + 3)
        except (ValueError, IndexError):
            pass
    if s_yr > e_yr:
        s_yr = e_yr - 3

    aadt_url = build_station_aadt_url(station.id, s_yr, e_yr)
    flow_url = build_station_flow_url(station.id, s_yr, e_yr)

    bound_html = (
        f'<div style="color: #15803d; font-weight: bold;">✓ In Project Bounds ({bounds_note})</div>'
        if is_in_bounds
        else f'<div style="color: #b91c1c; font-weight: bold;">⚠ Outside Project Bounds: {bounds_note}</div>'
    )
    if is_in_sheet:
        sheet_html = (
            '<div style="color: #15803d;">✓ Included in Master Spreadsheet (Verified)</div>'
            if is_in_bounds
            else '<div style="color: #b91c1c; font-weight: bold;">⚠ Included in Master Spreadsheet (Corridor Discrepancy)</div>'
        )
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
    discrepancies_count: int = 0,
) -> None:
    """Add Ground-Truth Checker floating panel and interactive JS controls to a Folium map."""
    discrepancy_str = (
        f" · <span style='color: #dc2626; font-weight: bold;'>{discrepancies_count} out-of-range</span>"
        if discrepancies_count > 0
        else ""
    )
    gt_html = f"""
<div id="pems-ground-truth-control" style="
  position: absolute; top: 12px; left: 60px; z-index: 1000; background: #ffffff;
  border: 1px solid #000000; padding: 8px 12px; font-family: {MONO_FONT};
  font-size: 11.5px; line-height: 1.4; color: #000000; max-width: 340px; box-shadow: none;
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
      &nbsp;<span style="display: inline-block; width: 9px; height: 9px; background: #7e22ce; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>HV (HOV)
      &nbsp;<span style="display: inline-block; width: 9px; height: 9px; background: #15803d; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>OR (Ramp)<br>
      <span style="display: inline-block; width: 9px; height: 9px; background: #b45309; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>FR (Off-Ramp)
      &nbsp;<span style="display: inline-block; width: 9px; height: 9px; background: #475569; border: 1px solid #000; margin-right: 2px; vertical-align: middle;"></span>Other
      &nbsp;<span style="display: inline-block; width: 9px; height: 9px; border: 2.5px solid #d97706; background: #fff; margin-right: 2px; vertical-align: middle;"></span>In Master Sheet
    </div>
  </div>
</div>
""" + """<script>
var inBoundsOnlyActive = false;
function toggleInBoundsOnly(btn) {
  inBoundsOnlyActive = !inBoundsOnlyActive;
  btn.innerText = "In-Bounds Only: " + (inBoundsOnlyActive ? "ON" : "OFF");
  btn.style.background = inBoundsOnlyActive ? "#e2e8f0" : "#f4f4f4";
  btn.style.fontWeight = inBoundsOnlyActive ? "bold" : "normal";
  document.body.classList.toggle("hide-oob-stations", inBoundsOnlyActive);
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
  stationInputs.forEach(function(inp) { if (inp.checked !== target) inp.click(); });
}
function toggleGroundTruthPanel(btn) {
  var content = document.getElementById("pems-gt-content");
  if (!content) return;
  var show = content.style.display === "none";
  content.style.display = show ? "block" : "none";
  btn.innerText = show ? "[–]" : "[+]";
}
</script>
"""
    m.get_root().html.add_child(folium.Element(gt_html))


def add_standard_basemaps_to_map(m: folium.Map, default_tiles: Optional[str] = None) -> None:
    """Add unwatermarked Esri/USGS/optional-CARTO basemaps that work over both file:// and http://."""
    carto_key = os.getenv("CARTO_API_KEY", "").strip()
    is_street_default = default_tiles is None or default_tiles in ("esri_street", "street", "default")
    is_gray_default = default_tiles in ("esri_gray", "gray", "light_gray")

    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS",
        name="Esri Street Map (Default)" if is_street_default else "Esri Street Map",
        overlay=False,
        control=True,
        show=is_street_default,
        max_zoom=19,
    ).add_to(m)

    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ",
        name="Esri Light Gray (Default)" if is_gray_default else "Esri Light Gray",
        overlay=False,
        control=True,
        show=is_gray_default,
        max_zoom=16,
    ).add_to(m)

    folium.TileLayer(
        tiles="https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles courtesy of the U.S. Geological Survey",
        name="USGS Topo",
        overlay=False,
        control=True,
        show=False,
        max_zoom=16,
    ).add_to(m)

    if carto_key:
        folium.TileLayer(
            tiles=f"https://a.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}.png?key={carto_key}",
            attr="&copy; OpenStreetMap contributors &copy; CARTO",
            name="CartoDB Positron (Authenticated)",
            overlay=False,
            control=True,
            show=False,
            max_zoom=20,
        ).add_to(m)

    folium.TileLayer(
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="&copy; OpenStreetMap contributors (Note: tile.openstreetmap.org blocks local file:// URLs)",
        name="OpenStreetMap (HTTP Only)",
        overlay=False,
        control=True,
        show=False,
        max_zoom=19,
    ).add_to(m)

    if (
        default_tiles
        and not (is_street_default or is_gray_default)
        and default_tiles not in ("cartodbpositron", "cartodb_positron", "openstreetmap")
    ):
        folium.TileLayer(
            tiles=default_tiles,
            attr="Custom Tile Provider",
            name="Custom Base Layer",
            overlay=False,
            control=True,
            show=True,
        ).add_to(m)


def _compute_project_length_mi(project: ProjectRecord) -> float:
    if project.has_abs_pm_bounds:
        return abs(float(project.abs_end_pm) - float(project.abs_begin_pm))  # type: ignore[arg-type]
    _, b_n = split_ca_pm(project.begin_pm)
    _, e_n = split_ca_pm(project.end_pm)
    if not np.isnan(b_n) and not np.isnan(e_n):
        return abs(e_n - b_n)
    return 0.0


def _add_project_to_layer(
    project: ProjectRecord,
    idx_ref: IndexedGeoReference,
    target_layer: Any,
    poly_weight: int,
    poly_opacity: float,
    marker_radius: float,
    start_tooltip: str,
    end_tooltip: str,
) -> Optional[List[Tuple[float, float]]]:
    """Draw a project's highway corridor polyline and start/end endpoint markers."""
    coords = get_project_corridor_geometry(project, idx_ref)
    if not coords:
        return None
    length_mi = _compute_project_length_mi(project)
    builder = ProjectPeMSQueryBuilder(project, idx_ref)
    color = DISTRICT_COLORS.get(project.dist, "#2563eb")

    folium.PolyLine(
        coords,
        weight=poly_weight,
        color=color,
        opacity=poly_opacity,
        tooltip=folium.Tooltip(make_project_tooltip_html(project, length_mi), sticky=True),
        popup=folium.Popup(make_project_popup_html(project, builder, length_mi), max_width=370),
    ).add_to(target_layer)

    folium.CircleMarker(
        coords[0],
        radius=marker_radius,
        color="#16a34a",
        fill=True,
        fill_color="#16a34a",
        fill_opacity=1.0 if marker_radius >= 5 else 0.9,
        tooltip=start_tooltip,
    ).add_to(target_layer)

    folium.CircleMarker(
        coords[-1],
        radius=marker_radius,
        color="#dc2626",
        fill=True,
        fill_color="#dc2626",
        fill_opacity=1.0 if marker_radius >= 5 else 0.9,
        tooltip=end_tooltip,
    ).add_to(target_layer)
    return coords


def _resolve_station_lat_lng(
    station: VDSStationRecord,
    idx_ref: IndexedGeoReference,
    use_config_cache: bool = True,
    cache_dir: Optional[str] = None,
) -> Optional[Tuple[float, float]]:
    """Resolve station coordinates from cached sta_cfg, VDS metadata, or postmile fallback."""
    lat, lng = station.latitude, station.longitude
    if use_config_cache:
        cached_st = load_cached_station_config(station.id, cache_dir=cache_dir)
        if cached_st and cached_st.latitude and cached_st.longitude:
            lat, lng = cached_st.latitude, cached_st.longitude
    if lat is None or lng is None or np.isnan(lat) or np.isnan(lng) or lat == 0:
        _, st_num = station.ca_pm_prefix_and_num
        if not np.isnan(st_num):
            coords = pm_to_coordinates(station.fwy, station.dir, st_num, county=station.county, ref_df=idx_ref)
            if coords:
                lat, lng = coords
    if lat is None or lng is None or np.isnan(lat) or np.isnan(lng) or lat == 0:
        return None
    return float(lat), float(lng)


def _resolve_vds_metadata(
    vds_metadata: Optional[IndexedVDSMetadata],
    metadata_dir: Optional[str],
    context_label: str,
) -> Optional[IndexedVDSMetadata]:
    if vds_metadata is not None:
        return vds_metadata
    try:
        return load_vds_metadata(metadata_dir) if metadata_dir is not None else get_indexed_vds_metadata()
    except Exception as exc:
        LOGGER.warning(f"Could not load PeMS VDS metadata for {context_label}: {exc}")
        return None


def _fit_and_save_map(
    m: folium.Map,
    all_coords: List[Tuple[float, float]],
    padding: Tuple[int, int],
    output_path: Optional[str],
    open_browser: bool,
    log_label: str,
) -> None:
    if all_coords:
        lats = [c[0] for c in all_coords]
        lngs = [c[1] for c in all_coords]
        m.fit_bounds([[min(lats), min(lngs)], [max(lats), max(lngs)]], padding=padding)
    else:
        m.location = [36.7783, -119.4179]
        m.zoom_start = 6

    folium.LayerControl(position="topright", collapsed=False).add_to(m)

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        m.save(output_path)
        LOGGER.info(f"{log_label} saved to {output_path}")
        if open_browser:
            webbrowser.open("file://" + os.path.abspath(output_path))


class _BindLeafletMap(MacroElement):
    """Attach initialized Leaflet map object directly to window._leaflet_map."""

    def __init__(self, var_name: str):
        super().__init__()
        self._template = Template(
            f"""
            {{% macro script(this, kwargs) %}}
                window._leaflet_map = {var_name};
                try {{ {var_name}.invalidateSize(); }} catch(e) {{}}
                window.addEventListener('resize', function() {{
                    try {{ {var_name}.invalidateSize(); }} catch(e) {{}}
                }});
                setTimeout(function() {{
                    try {{ {var_name}.invalidateSize(); }} catch(e) {{}}
                }}, 200);
                setTimeout(function() {{
                    try {{ {var_name}.invalidateSize(); }} catch(e) {{}}
                }}, 600);
            {{% endmacro %}}
            """
        )


def _inject_single_project_focus_js(m: folium.Map) -> None:
    """Attach Leaflet focus/centerMapAt JS and hash/postMessage synchronization."""
    map_var_name = m.get_name()
    m.add_child(_BindLeafletMap(map_var_name))
    m.get_root().header.add_child(folium.Element(_LEAFLET_BASE_CSS))
    m.get_root().html.add_child(
        folium.Element(
            f"""
<script>
  window._currentFocusMarkers = [];

  function getLeafletMap() {{
    if (window._leaflet_map && typeof window._leaflet_map.setView === 'function') return window._leaflet_map;
    if (window['{map_var_name}'] && typeof window['{map_var_name}'].setView === 'function') {{
      window._leaflet_map = window['{map_var_name}'];
      return window._leaflet_map;
    }}
    for (var k in window) {{
      try {{
        if (k.indexOf('map_') === 0 && window[k] && typeof window[k].setView === 'function') {{
          window._leaflet_map = window[k];
          return window[k];
        }}
      }} catch (e) {{}}
    }}
    return null;
  }}

  window.clearFocusMarkers = function() {{
    var mapObj = getLeafletMap();
    if (mapObj && window._currentFocusMarkers) {{
      while (window._currentFocusMarkers.length > 0) {{
        var mk = window._currentFocusMarkers.pop();
        try {{ mapObj.removeLayer(mk); }} catch(e) {{}}
      }}
    }}
    window._currentFocusMarkers = [];
  }};

  window.centerMapAt = function(lat, lon, zoom, label) {{
    if (isNaN(lat) || isNaN(lon)) return;
    var z = (zoom && !isNaN(zoom)) ? parseInt(zoom) : 17;
    var mapObj = getLeafletMap();
    if (!mapObj) return;
    try {{ mapObj.invalidateSize(); }} catch (e) {{}}
    window.clearFocusMarkers();
    mapObj.setView([lat, lon], z, {{animate: true}});

    if (window.L && typeof window.L.circleMarker === 'function') {{
      var marker = window.L.circleMarker([lat, lon], {{
        radius: 12, color: '#e11d48', fillColor: '#fb7185', fillOpacity: 0.5, weight: 3
      }}).addTo(mapObj);
      if (label) marker.bindTooltip(label, {{permanent: true, direction: 'top'}}).openTooltip();
      marker.on('click', function(ev) {{
        if (window.L && window.L.DomEvent) window.L.DomEvent.stopPropagation(ev);
        window.clearFocusMarkers();
      }});
      window._currentFocusMarkers.push(marker);
    }}
  }};

  function setupMapDismissListeners() {{
    var mapObj = getLeafletMap();
    if (mapObj && !mapObj._hasDismissClick) {{
      mapObj._hasDismissClick = true;
      mapObj.on('click', function() {{ window.clearFocusMarkers(); }});
    }}
  }}

  function centerFromHash() {{
    var h = window.location.hash;
    if (!h || h.length < 2) return;
    var parts = h.substring(1).replace(/^#/, '').split(',');
    if (parts.length >= 2) {{
      var lat = parseFloat(parts[0]);
      var lon = parseFloat(parts[1]);
      var z = parts.length >= 3 ? parseInt(parts[2]) : 17;
      var lbl = parts.length >= 4 ? decodeURIComponent(parts[3]) : 'Selected Location';
      if (!isNaN(lat) && !isNaN(lon)) window.centerMapAt(lat, lon, z, lbl);
    }}
  }}

  window.addEventListener('message', function(event) {{
    if (!event.data) return;
    if (event.data.type === 'FOCUS_MAP') {{
      window.centerMapAt(event.data.lat, event.data.lon, event.data.zoom, event.data.label);
    }} else if (event.data.type === 'INVALIDATE_SIZE') {{
      var mapObj = getLeafletMap();
      if (mapObj && typeof mapObj.invalidateSize === 'function') mapObj.invalidateSize();
    }}
  }});

  window.addEventListener('DOMContentLoaded', function() {{
    setupMapDismissListeners();
    [250, 600].forEach(function(delay) {{
      setTimeout(function() {{
        setupMapDismissListeners();
        centerFromHash();
        var mapObj = getLeafletMap();
        if (mapObj && typeof mapObj.invalidateSize === 'function') mapObj.invalidateSize();
      }}, delay);
    }});
    window.addEventListener('hashchange', centerFromHash);
  }});
</script>
"""
        )
    )


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
    precomputed_stations: Optional[List[VDSStationRecord]] = None,
    cache_dir: Optional[str] = None,
) -> Optional[folium.Map]:
    """Build interactive Folium map for a single project with detector layers and focus JS."""
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
        coords = _add_project_to_layer(
            p,
            idx_ref,
            m,
            poly_weight=6,
            poly_opacity=0.9,
            marker_radius=5,
            start_tooltip=f"Start: Route {p.route} {p.direction} PM {p.begin_pm}",
            end_tooltip=f"End: Route {p.route} {p.direction} PM {p.end_pm}",
        )
        if coords:
            all_coords.extend(coords)

    if include_stations:
        vds = _resolve_vds_metadata(vds_metadata, metadata_dir, "mapping")
        if vds is not None or precomputed_stations is not None:
            proj_by_dir = {p.pems_direction: p for p in matched}
            all_st_records: List[VDSStationRecord] = []
            seen_ids = set()

            if precomputed_stations is not None:
                for s in precomputed_stations:
                    if s.id not in seen_ids:
                        seen_ids.add(s.id)
                        all_st_records.append(s)
            elif vds is not None:
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
                        buffer_mi=station_buffer_mi,
                        station_types=station_types or ("ALL",),
                        county=p.county,
                    ):
                        if s.id not in seen_ids:
                            seen_ids.add(s.id)
                            all_st_records.append(s)

            sheet_sensor_ids = set()
            for p in matched:
                for sid in p.sensor_stations:
                    sheet_sensor_ids.add(sid)
                    if vds is not None and sid not in seen_ids and sid in vds.by_id:
                        seen_ids.add(sid)
                        all_st_records.append(vds.by_id[sid])

            type_groups: Dict[str, List[Tuple[VDSStationRecord, bool, str, bool, ProjectRecord]]] = {
                k: [] for k in STATION_LAYER_ORDER
            }
            in_bounds_cnt = oob_cnt = sheet_verified_cnt = sheet_discrepancy_cnt = 0

            for st in all_st_records:
                target_p = proj_by_dir.get(st.dir) or matched[0]
                is_in_bounds, bounds_note, _ = classify_station_relative_to_project(st, target_p)
                is_in_sheet = st.id in sheet_sensor_ids
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

            default_show = {"ML", "HV"}
            for grp_key in STATION_LAYER_ORDER:
                st_tuples = type_groups[grp_key]
                if not st_tuples:
                    continue
                fg = folium.FeatureGroup(
                    name=f"Stations: {STATION_LAYER_LABELS[grp_key]} ({len(st_tuples)})",
                    show=(grp_key in default_show),
                )
                for st, in_bounds, b_note, in_sheet, target_p in st_tuples:
                    resolved = _resolve_station_lat_lng(st, idx_ref, use_config_cache=True, cache_dir=cache_dir)
                    if not resolved:
                        continue
                    lat, lng = resolved
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
                        tooltip=folium.Tooltip(make_station_tooltip_html(st, in_bounds, b_note, in_sheet), sticky=True),
                        popup=folium.Popup(make_station_popup_html(st, target_p, in_bounds, b_note, in_sheet), max_width=370),
                    ).add_to(fg)
                fg.add_to(m)

            add_ground_truth_control(
                m=m,
                project=matched[0],
                in_bounds_count=in_bounds_cnt,
                oob_count=oob_cnt,
                sheet_sensors_total=len(sheet_sensor_ids),
                sheet_sensors_verified=sheet_verified_cnt,
                discrepancies_count=sheet_discrepancy_cnt,
            )

    _fit_and_save_map(m, all_coords, padding=(30, 30), output_path=None, open_browser=False, log_label="Project map")
    _inject_single_project_focus_js(m)
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
    """Build statewide or district-filtered Folium map with District and detector layer toggles."""
    projs = projects or load_master_csv()
    if dist:
        dist_clean = normalize_district(dist)
        projs = [p for p in projs if normalize_district(p.dist) == dist_clean]
        LOGGER.info(f"Filtered to District {dist_clean} ({len(projs)} projects)")

    idx_ref = get_indexed_geo_ref()
    m = folium.Map(tiles=None)
    add_standard_basemaps_to_map(m, default_tiles=tiles)
    all_coords: List[Tuple[float, float]] = []

    dist_groups: Dict[str, List[ProjectRecord]] = {}
    for p in projs:
        dist_groups.setdefault(p.dist, []).append(p)

    for d in sorted(dist_groups.keys(), key=lambda key: int(key) if key.isdigit() else 999):
        p_list = dist_groups[d]
        fg = folium.FeatureGroup(name=f"District {d} ({len(p_list)} projects)", show=True)
        for p in p_list:
            coords = _add_project_to_layer(
                p,
                idx_ref,
                fg,
                poly_weight=5,
                poly_opacity=0.85,
                marker_radius=3.5,
                start_tooltip=f"PID {p.project_id} Start PM {p.begin_pm}",
                end_tooltip=f"PID {p.project_id} End PM {p.end_pm}",
            )
            if coords:
                all_coords.extend(coords)
        fg.add_to(m)

    if include_stations:
        vds = _resolve_vds_metadata(vds_metadata, None, "multi-project map")
        if vds is not None:
            seen_sids: Dict[str, Tuple[VDSStationRecord, List[str]]] = {}
            for p in projs:
                eff_abs_b, eff_abs_e = p.effective_abs_pm_bounds
                found = vds.find_corridor_stations(
                    district=p.dist,
                    route=p.route,
                    direction=p.direction,
                    begin_pm=p.begin_pm,
                    end_pm=p.end_pm,
                    abs_begin_pm=eff_abs_b,
                    abs_end_pm=eff_abs_e,
                    buffer_mi=station_buffer_mi,
                    station_types=station_types or ("ALL",),
                    county=p.county,
                )
                for s in found:
                    if s.id not in seen_sids:
                        seen_sids[s.id] = (s, [p.project_id])
                    elif p.project_id not in seen_sids[s.id][1]:
                        seen_sids[s.id][1].append(p.project_id)
                for sid in p.sensor_stations:
                    if sid in vds.by_id:
                        s = vds.by_id[sid]
                        if sid not in seen_sids:
                            seen_sids[sid] = (s, [p.project_id])
                        elif p.project_id not in seen_sids[sid][1]:
                            seen_sids[sid][1].append(p.project_id)

            type_groups: Dict[str, List[Tuple[VDSStationRecord, List[str]]]] = {k: [] for k in STATION_LAYER_ORDER}
            for st, pids in seen_sids.values():
                grp_key = st.type if st.type in ("ML", "HV", "OR", "FR") else "Other"
                type_groups[grp_key].append((st, pids))

            for grp_key in STATION_LAYER_ORDER:
                st_list = type_groups[grp_key]
                if not st_list:
                    continue
                fg = folium.FeatureGroup(
                    name=f"Stations: {STATION_LAYER_LABELS[grp_key]} ({len(st_list)})", show=False
                )
                for st, pids in st_list:
                    resolved = _resolve_station_lat_lng(st, idx_ref, use_config_cache=False)
                    if not resolved:
                        continue
                    lat, lng = resolved
                    pid_str = ", ".join(pids) if pids else "Corridor"
                    type_name = STATION_TYPE_NAMES.get(st.type, st.type)
                    tt_html = f"""<div style="font-family: {MONO_FONT}; font-size: 11.5px; line-height: 1.35; padding: 3px; min-width: 200px; color: #000; background: #fff;">
  <div style="font-weight: bold; border-bottom: 1px solid #000; padding-bottom: 2px; margin-bottom: 3px;">Station {st.id} ({st.type} - {type_name})</div>
  <div><strong>Name:</strong> {st.name or 'N/A'}</div>
  <div><strong>Route:</strong> Route {st.fwy} {st.dir} · PM {st.state_pm}</div>
  <div><strong>Lanes:</strong> {st.lanes or 'N/A'}</div>
  <div><strong>Project(s):</strong> PID {pid_str}</div>
</div>"""
                    folium.CircleMarker(
                        location=[lat, lng],
                        radius=4.5,
                        color="#000000",
                        weight=1.5,
                        fill=True,
                        fill_color=STATION_TYPE_COLORS.get(st.type, "#2563eb"),
                        fill_opacity=0.85,
                        className="pems-station-marker",
                        tooltip=folium.Tooltip(tt_html, sticky=True),
                        popup=folium.Popup(make_station_popup_html(st, None, True, f"Project(s): PID {pid_str}"), max_width=350),
                    ).add_to(fg)
                fg.add_to(m)

    m.get_root().header.add_child(folium.Element(_LEAFLET_BASE_CSS))
    _fit_and_save_map(
        m, all_coords, padding=(20, 20), output_path=output_path, open_browser=open_browser, log_label="Multi-project map"
    )
    return m
