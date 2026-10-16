"""
Flask front-end for PeMS postmile verification and Caltrans project mapping.

Directly integrated with pems_interface.py. Self-contained, robust, and free of
external proprietary routing API dependencies.
"""

import functools
import html
import io
import logging
import os
import sys
import traceback
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import folium
import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, render_template_string, request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.chdir(_HERE)

import pems_interface
from pems_interface import (
    DEFAULT_MASTER_CSV,
    DEFAULT_REF_PATH,
    IndexedGeoReference,
    add_standard_basemaps_to_map,
    build_multi_project_map,
    build_single_project_map,
    format_pm,
    get_corridor_stations,
    get_indexed_geo_ref,
    load_geo_reference,
    load_master_csv,
    normalize_direction,
    normalize_route,
    pm_to_coordinates,
    split_ca_pm,
)

LOGGER = logging.getLogger("pems_app")

app = Flask(__name__)


# ----------------------------------------------------------------------------
# Reference Data Access
# ----------------------------------------------------------------------------
def reference_df() -> Optional[pd.DataFrame]:
    """Retrieve the 331K-row reference table, or None if unavailable."""
    try:
        idx_ref = get_indexed_geo_ref()
        return idx_ref.raw_df
    except Exception as exc:
        LOGGER.warning(f"Reference table not loaded: {exc}")
        return None


def get_route_dir_columns(df: pd.DataFrame) -> Tuple[str, str]:
    route_col = "Route" if "Route" in df.columns else ("Fwy" if "Fwy" in df.columns else "Route")
    dir_col = "Direction" if "Direction" in df.columns else ("Dir" if "Dir" in df.columns else "Direction")
    return route_col, dir_col


# ----------------------------------------------------------------------------
# Map Builders
# ----------------------------------------------------------------------------
def _points_map(start: Tuple[float, float], end: Tuple[float, float]) -> folium.Map:
    """Map the two postmile locations with a straight dashed connection."""
    centroid = [(start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0]
    m = folium.Map(location=centroid, tiles=None, zoom_start=12)
    add_standard_basemaps_to_map(m)
    folium.PolyLine(
        [start, end],
        weight=4,
        dash_array="6,8",
        color="#b42318",
        tooltip="Straight-line distance (endpoints only)"
    ).add_to(m)
    folium.CircleMarker(
        start,
        radius=6,
        color="#16a34a",
        fill=True,
        fill_color="#16a34a",
        fill_opacity=1.0,
        tooltip=f"Begin PM ({start[0]:.5f}, {start[1]:.5f})"
    ).add_to(m)
    folium.CircleMarker(
        end,
        radius=6,
        color="#dc2626",
        fill=True,
        fill_color="#dc2626",
        fill_opacity=1.0,
        tooltip=f"End PM ({end[0]:.5f}, {end[1]:.5f})"
    ).add_to(m)
    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    m.fit_bounds([start, end], padding=(40, 40))
    return m


def _route_corridor_map(
    route: str,
    direction: str,
    begin_pm: str,
    end_pm: str,
    is_abs: bool,
    county: Optional[str],
    start: Tuple[float, float],
    end: Tuple[float, float]
) -> folium.Map:
    """Map the actual highway corridor path using high-resolution reference segments."""
    centroid = [(start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0]
    m = folium.Map(location=centroid, tiles=None, zoom_start=12)
    add_standard_basemaps_to_map(m)

    corr_df = get_corridor_stations(
        route=route,
        direction=direction,
        begin_pm=begin_pm,
        end_pm=end_pm,
        is_absolute=is_abs,
        county=county
    )

    coords: List[Tuple[float, float]] = []
    if corr_df is not None and not corr_df.empty:
        lat_col = "Latitude" if "Latitude" in corr_df.columns else "Lat"
        lng_col = "Longitude" if "Longitude" in corr_df.columns else "Lng"

        if lat_col in corr_df.columns and lng_col in corr_df.columns:
            pts_df = corr_df.dropna(subset=[lat_col, lng_col]).copy()
            pts_df[lat_col] = pd.to_numeric(pts_df[lat_col], errors="coerce")
            pts_df[lng_col] = pd.to_numeric(pts_df[lng_col], errors="coerce")
            pts_df = pts_df.dropna(subset=[lat_col, lng_col])

            try:
                b_num = float(begin_pm) if is_abs else split_ca_pm(begin_pm)[1]
                e_num = float(end_pm) if is_abs else split_ca_pm(end_pm)[1]
                ascending = float(b_num) <= float(e_num)
            except Exception:
                ascending = True

            sort_col = "_abs_pm" if is_abs and "_abs_pm" in pts_df.columns else "_ca_num"
            if sort_col in pts_df.columns:
                pts_df = pts_df.sort_values(by=sort_col, ascending=ascending)

            for _, row in pts_df.iterrows():
                coords.append((float(row[lat_col]), float(row[lng_col])))

    if len(coords) < 2:
        coords = [start, end]
    else:
        if coords[0] != start:
            coords.insert(0, start)
        if coords[-1] != end:
            coords.append(end)

    safe_route = html.escape(str(route))
    safe_dir = html.escape(str(direction))
    safe_begin_pm = html.escape(str(begin_pm))
    safe_end_pm = html.escape(str(end_pm))

    folium.PolyLine(
        coords,
        weight=6,
        color="#2563eb",
        opacity=0.85,
        tooltip=f"Route {safe_route} {safe_dir} (PM {safe_begin_pm} - {safe_end_pm})"
    ).add_to(m)

    folium.CircleMarker(
        start,
        radius=5,
        color="#16a34a",
        fill=True,
        fill_color="#16a34a",
        fill_opacity=1.0,
        tooltip=f"Begin PM {safe_begin_pm}"
    ).add_to(m)

    folium.CircleMarker(
        end,
        radius=5,
        color="#dc2626",
        fill=True,
        fill_color="#dc2626",
        fill_opacity=1.0,
        tooltip=f"End PM {safe_end_pm}"
    ).add_to(m)

    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    m.fit_bounds(coords, padding=(30, 30))
    return m


# ----------------------------------------------------------------------------
# HTML Templates
# ----------------------------------------------------------------------------
PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PeMS Postmile Verification</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.45 Georgia, "Times New Roman", Times, serif;
         background:#ffffff; color:#000000; }
  .wrap { max-width:1180px; margin:0 auto; padding:18px 16px 36px; }
  h1 { font-size:20px; font-weight:bold; margin:0 0 2px; }
  p.sub { margin:0 0 14px; color:#333333; font-size:13px; }
  form { background:#ffffff; border:1px solid #000000; border-radius:0; padding:10px 12px;
         display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
         align-items:end; }
  label { display:block; font-size:11.5px; font-weight:bold; color:#000000;
          text-transform:uppercase; letter-spacing:.03em; margin-bottom:4px; }
  input[type=text] { width:100%; padding:5px 8px; font-size:14px; font-family:inherit;
                     border:1px solid #000000; border-radius:0; background:#ffffff; }
  input:disabled { background:#eeeeee; color:#777777; }
  .chk { display:flex; align-items:center; gap:6px; font-size:13px; padding-bottom:6px; }
  .chk label { margin:0; text-transform:none; letter-spacing:0; font-size:13px; font-weight:normal; }
  button { padding:6px 14px; font-size:14px; font-family:inherit; font-weight:bold;
           border:1px solid #000000; border-radius:0; background:#f0f0f0; color:#000000;
           cursor:pointer; width:100%; }
  button:hover { background:#e0e0e0; }
  .hint { font-size:12px; color:#444444; min-height:16px; margin:6px 2px 0; }
  .banner { margin:12px 0 0; padding:8px 12px; border-radius:0; font-size:13px;
            background:#ffffff; border:1px solid #000000; color:#000000; }
  .card { margin-top:14px; background:#ffffff; border:1px solid #000000; border-radius:0;
          overflow:hidden; }
  .card > header { padding:6px 10px; border-bottom:1px solid #000000; font-size:13px;
                   background:#f4f4f4; color:#000000; display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .card a { color:#0000ee; text-decoration:underline; font-weight:normal; }
  .card a:hover { text-decoration:underline; }
  iframe { display:block; width:100%; height:620px; border:0; background:#ffffff; }
  code { background:#eeeeee; padding:1px 4px; border:1px solid #cccccc; border-radius:0; font-size:12px; font-family:monospace; }
</style>
<div class="wrap">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 10px; border-bottom: 1px solid #000; padding-bottom: 8px;">
    <h1 style="margin:0;">PeMS Postmile Verification</h1>
    <a href="/projects/map" target="_blank" style="color:#0000ee; text-decoration:underline; font-size:14px;">[View All CA Projects Map]</a>
  </div>
  <form id="f" method="get" action="/map" target="mapframe">
    <div>
      <label for="route">Route</label>
      <input type="text" id="route" name="route" list="routes" value="{{ v.route }}" placeholder="5" autocomplete="off">
      <datalist id="routes">{% for r in routes %}<option value="{{ r }}">{% endfor %}</datalist>
    </div>
    <div>
      <label for="direction">Direction</label>
      <input type="text" id="direction" name="direction" list="dirs" value="{{ v.direction }}" placeholder="N" autocomplete="off">
      <datalist id="dirs"></datalist>
    </div>
    <div><label for="begin_pm">Begin PM</label><input type="text" id="begin_pm" name="begin_pm" value="{{ v.begin_pm }}" placeholder="12.3" autocomplete="off"></div>
    <div><label for="end_pm">End PM</label><input type="text" id="end_pm" name="end_pm" value="{{ v.end_pm }}" placeholder="18.7" autocomplete="off"></div>
    <div>
      <label for="county">County</label>
      <input type="text" id="county" name="county" list="counties" value="{{ v.county }}" placeholder="CA PM only" autocomplete="off">
      <datalist id="counties"></datalist>
    </div>
    <div class="chk"><input type="checkbox" id="absolute" name="absolute" value="1" {{ 'checked' if v.absolute else '' }}><label for="absolute">Absolute PM</label></div>
    <div><button type="submit">Draw route</button></div>
  </form>
  <div class="hint" id="hint"></div>

  {% if not ref_ok %}
  <div class="banner">
    <b>{{ merged_path }}</b> not loaded. Ensure <code>merged_geo_pm.csv</code> exists in <code>downloads/</code>
    and run <code>POST /api/reload</code>.
  </div>
  {% endif %}

  <div class="card">
    <header>
      <span id="maplabel">Map</span>
      <span><a id="popout" href="#" target="_blank" rel="noopener">open in new tab</a> &nbsp;·&nbsp;
            <a id="dl" href="#">download .html</a></span>
    </header>
    <iframe name="mapframe" id="mapframe" src="about:blank" title="route map"></iframe>
  </div>
</div>
<script>
const f = document.getElementById('f');
const hint = document.getElementById('hint');
const absBox = document.getElementById('absolute');
const county = document.getElementById('county');

function syncCounty() {
  county.disabled = absBox.checked;
  if (absBox.checked) county.value = '';
}
absBox.addEventListener('change', syncCounty);
syncCounty();

async function loadRoute() {
  const r = document.getElementById('route').value.trim();
  if (!r) { hint.textContent = ''; return; }
  try {
    const res = await fetch('/api/route/' + encodeURIComponent(r));
    const d = await res.json();
    if (!d.found) { hint.textContent = 'Route ' + r + ' is not in the reference file.'; return; }
    document.getElementById('dirs').innerHTML =
      d.directions.map(x => '<option value="' + x.direction + '">').join('');
    document.getElementById('counties').innerHTML =
      d.counties.map(x => '<option value="' + x + '">').join('');
    hint.textContent = 'Route ' + r + ' — ' +
      d.directions.map(x => x.direction + ': Abs PM ' + x.min_pm + '–' + x.max_pm +
                            ' (' + x.rows + ' stations)').join('   ·   ');
  } catch (e) { hint.textContent = ''; }
}
document.getElementById('route').addEventListener('change', loadRoute);
if (document.getElementById('route').value) loadRoute();

function qs(extra) {
  const p = new URLSearchParams(new FormData(f));
  if (county.disabled) p.delete('county');
  for (const k in extra) p.set(k, extra[k]);
  return '/map?' + p.toString();
}
f.addEventListener('submit', () => {
  const p = new URLSearchParams(new FormData(f));
  document.getElementById('maplabel').textContent =
    'Route ' + (p.get('route')||'?') + (p.get('direction')||'') + ' · PM ' +
    (p.get('begin_pm')||'?') + ' → ' + (p.get('end_pm')||'?') +
    (p.get('absolute') ? ' · absolute' : ' · California');
  document.getElementById('popout').href = qs({});
  document.getElementById('dl').href = qs({download: '1'});
});
</script>
"""

ERROR_PAGE = """<!doctype html><meta charset="utf-8">
<style>
 body{font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;padding:22px;
      background:#fdeceb;color:#7f1d16}
 h3{margin:0 0 8px;font-size:15px}
 p{margin:0 0 12px}
 a{color:#b42318;font-weight:600}
 pre{white-space:pre-wrap;background:#fff;border:1px solid #f3b0ab;border-radius:8px;
     padding:10px 12px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#4b1410;
     max-height:340px;overflow:auto}
</style>
<h3>{{ title }}</h3>
{% if suggestion %}<p>{{ suggestion|safe }}</p>{% endif %}
{% if detail %}<pre>{{ detail }}</pre>{% endif %}
"""


def _err(title: str, detail: Optional[str] = None, suggestion: Optional[str] = None, code: int = 400):
    return render_template_string(
        ERROR_PAGE, title=title, detail=detail, suggestion=suggestion
    ), code


# ----------------------------------------------------------------------------
# Request Parameter Parsing
# ----------------------------------------------------------------------------
def _params(src) -> Dict[str, Any]:
    return {
        "route": (src.get("route") or "").strip(),
        "direction": (src.get("direction") or "").strip().upper(),
        "begin_pm": (src.get("begin_pm") or "").strip(),
        "end_pm": (src.get("end_pm") or "").strip(),
        "county": (src.get("county") or "").strip(),
        "absolute": (src.get("absolute") or "").lower() in ("1", "true", "on", "yes"),
        "mode": (src.get("mode") or "route").strip().lower(),
        "download": (src.get("download") or "").lower() in ("1", "true", "on", "yes"),
    }


# ----------------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    v = _params(request.args)
    if not request.args:
        v["absolute"] = True

    df = reference_df()
    if df is not None:
        route_col, _ = get_route_dir_columns(df)
        routes = sorted(
            df[route_col].astype(str).str.strip().unique(),
            key=lambda r: (len(r), r)
        )
    else:
        routes = [
            "5", "10", "15", "80", "101", "205", "210", "280", "405", "505",
            "580", "605", "680", "710", "805", "880", "980"
        ]

    return render_template_string(
        PAGE, v=v, routes=routes, ref_ok=df is not None,
        merged_path=DEFAULT_REF_PATH,
    )


@app.route("/map", methods=["GET", "POST"])
def map_view():
    src = request.form if request.method == "POST" else request.args
    v = _params(src)

    missing = [k for k in ("route", "direction", "begin_pm", "end_pm") if not v[k]]
    if missing:
        return _err("Missing input", detail="Required: " + ", ".join(missing))

    import re
    if not re.match(r"^[A-Za-z0-9\-\.\s]+$", v["route"]):
        return _err("Bad input", detail="Route contains invalid characters.")
    if not re.match(r"^[A-Za-z]+$", v["direction"]):
        return _err("Bad input", detail="Direction contains invalid characters.")
    if v["county"] and not re.match(r"^[A-Za-z0-9\s\-]+$", v["county"]):
        return _err("Bad input", detail="County contains invalid characters.")

    if not v["absolute"] and not v["county"]:
        return _err(
            "Missing input",
            detail="County is required when matching California State Postmiles (PM resets at county lines)."
        )

    # Validate postmile numeric inputs
    for k in ("begin_pm", "end_pm"):
        val = v[k]
        if v["absolute"]:
            try:
                float(val)
            except ValueError:
                return _err("Bad input", detail=f"{k}={val!r} is not a valid Absolute Postmile number.")
        else:
            _, num = split_ca_pm(val)
            if np.isnan(num):
                return _err(
                    "Bad input",
                    detail=f"{k}={val!r} is not a valid California Postmile format (e.g. '3.8' or 'R3.8')."
                )

    county = None if v["absolute"] else v["county"]

    # 1) Convert postmiles -> coordinates
    try:
        start = pm_to_coordinates(
            v["route"], v["direction"], v["begin_pm"],
            county=county, is_absolute=v["absolute"]
        )
        end = pm_to_coordinates(
            v["route"], v["direction"], v["end_pm"],
            county=county, is_absolute=v["absolute"]
        )
    except FileNotFoundError as exc:
        return _err(
            "Reference data missing",
            detail=str(exc),
            suggestion="Ensure <code>merged_geo_pm.csv</code> exists in <code>downloads/</code>, then <code>POST /api/reload</code>.",
            code=503
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", detail=traceback.format_exc(), code=500)

    # Sanitize route input for safe reflection in suggestions
    safe_route = html.escape(v["route"])
    url_route = urllib.parse.quote(v["route"])

    if not start or not end:
        failed_pts = []
        if not start:
            failed_pts.append(f"Begin PM {v['begin_pm']}")
        if not end:
            failed_pts.append(f"End PM {v['end_pm']}")
        err_detail = f"Could not locate coordinates for Route {v['route']} {v['direction']} ({', '.join(failed_pts)})."
        return _err(
            "No matching postmile",
            detail=err_detail,
            suggestion=f'Check the Abs PM range for this route: <a href="/api/route/{url_route}">/api/route/{safe_route}</a>',
            code=404
        )

    # 2) Build Folium map
    if v["mode"] == "points":
        m = _points_map(start, end)
    else:
        try:
            m = _route_corridor_map(
                route=v["route"],
                direction=v["direction"],
                begin_pm=v["begin_pm"],
                end_pm=v["end_pm"],
                is_abs=v["absolute"],
                county=county,
                start=start,
                end=end
            )
        except Exception as exc:
            qsargs = request.query_string.decode("utf-8", errors="replace")
            qsargs_safe = html.escape(qsargs)
            sep = "&" if qsargs else ""
            return _err(
                f"Routing failed — {type(exc).__name__}: {exc}",
                detail=traceback.format_exc(),
                suggestion=f'The postmile lookups succeeded; <a href="/map?{qsargs_safe}{sep}mode=points">plot the two endpoints without routing</a>.',
                code=502
            )

    html_content = m.get_root().render()

    if v["download"]:
        name = f"route_map_{v['route']}{v['direction']}_{v['begin_pm']}_{v['end_pm']}.html"
        return Response(
            html_content,
            mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="{name}"'}
        )
    return Response(html_content, mimetype="text/html")


# ----------------------------------------------------------------------------
# JSON APIs
# ----------------------------------------------------------------------------
@app.get("/api/routes")
def api_routes():
    df = reference_df()
    if df is None:
        fallback = [
            "5", "10", "15", "80", "101", "205", "210", "280", "405", "505",
            "580", "605", "680", "710", "805", "880", "980"
        ]
        return jsonify({"source": "fallback", "routes": fallback})
    route_col, _ = get_route_dir_columns(df)
    routes = sorted(df[route_col].astype(str).str.strip().unique(), key=lambda r: (len(r), r))
    return jsonify({"source": DEFAULT_REF_PATH, "routes": routes})


@app.get("/api/route/<route>")
def api_route(route):
    df = reference_df()
    if df is None:
        return jsonify({"found": False, "reason": "reference file not loaded"}), 503

    route_norm = normalize_route(route)
    route_col, dir_col = get_route_dir_columns(df)
    sub = df.loc[df[route_col].astype(str).str.strip() == route_norm]
    if sub.empty:
        return jsonify({"found": False, "route": route, "directions": [], "counties": []})

    out = []
    for d, grp in sub.groupby(sub[dir_col].astype(str).str.strip().str.upper()):
        pm = pd.to_numeric(grp.get("Abs PM"), errors="coerce").dropna()
        out.append({
            "direction": d,
            "rows": int(len(grp)),
            "min_pm": round(float(pm.min()), 1) if len(pm) else None,
            "max_pm": round(float(pm.max()), 1) if len(pm) else None,
        })

    counties = []
    if "County" in sub.columns:
        counties = sorted(sub["County"].astype(str).str.strip().replace("nan", "").unique())
        counties = [c for c in counties if c]

    return jsonify({
        "found": True,
        "route": str(route),
        "directions": sorted(out, key=lambda x: x["direction"]),
        "counties": counties
    })


@app.post("/api/reload")
def api_reload():
    pems_interface._INDEXED_GEO_REF = None
    pems_interface._RAW_GEO_DF = None
    try:
        idx_ref = get_indexed_geo_ref()
        rows = len(idx_ref.raw_df) if idx_ref.raw_df is not None else 0
        cols = list(idx_ref.raw_df.columns) if idx_ref.raw_df is not None else []
    except Exception:
        rows = None
        cols = []
    return jsonify({"reloaded": True, "rows": rows, "columns": cols})


# ----------------------------------------------------------------------------
# Project Maps
# ----------------------------------------------------------------------------
@app.get("/projects/map")
def projects_map_view():
    dist = request.args.get("dist")
    m = build_multi_project_map(dist=dist)
    html_content = m.get_root().render()
    return Response(html_content, mimetype="text/html")


@app.get("/projects/map/<pid>")
def project_single_map_view(pid):
    m = build_single_project_map(pid)
    if m is None:
        return _err("Project not found", detail=f"No project found matching ID: {pid}", code=404)
    html_content = m.get_root().render()
    return Response(html_content, mimetype="text/html")


@app.get("/api/download/station/<sid>")
def api_download_station_view(sid):
    try:
        import datetime
        data_type = request.args.get("type", "flow").strip().lower()
        granularity = request.args.get("granularity", "month").strip().lower()
        start_year = int(request.args.get("start_year", 1999))
        end_year = int(request.args.get("end_year", datetime.datetime.now().year))
        combine = request.args.get("combine", "1").lower() in ("1", "true", "yes")
        export_fmt = request.args.get("export_format", "text").strip().lower()

        out_dir = os.path.join(_HERE, "downloads", "stations", str(sid).strip())
        res = pems_interface.download_station_data(
            station_id=sid,
            data_type=data_type,
            granularity=granularity,
            start_year=start_year,
            end_year=end_year,
            output_dir=out_dir,
            combine=combine,
            verbose=False,
            export_format=export_fmt
        )

        if request.args.get("format") == "file" and res.get("combined_file") and os.path.exists(res["combined_file"]):
            with open(res["combined_file"], "r", encoding="utf-8") as f:
                content = f.read()
            return Response(
                content,
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{os.path.basename(res["combined_file"])}"'}
            )

        return jsonify({"ok": True, "download": res})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/healthz")
def healthz():
    df = reference_df()
    return jsonify({
        "ok": True,
        "cwd": os.getcwd(),
        "default_ref_path": DEFAULT_REF_PATH,
        "reference_rows": None if df is None else int(len(df)),
        "master_csv": DEFAULT_MASTER_CSV,
        "pems_module": getattr(pems_interface, "__file__", None),
    })


if __name__ == "__main__":
    print(f"pems_interface : {getattr(pems_interface, '__file__', '?')}")
    print(f"reference      : {DEFAULT_REF_PATH}")
    df0 = reference_df()
    print(f"                 {'not found' if df0 is None else str(len(df0)) + ' rows'}")
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=int(os.getenv("FLASK_PORT", "5000")),
        debug=bool(os.getenv("FLASK_DEBUG")),
        threaded=True,
    )