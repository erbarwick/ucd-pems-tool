"""
Flask front-end for main.py — the PeMS postmile verification tool.

This module sits ON TOP of main.py and does not modify it. It imports main and
re-uses its functions and module-level config.
"""

import functools
import io
import os
import sys
import traceback

# --- make `import main` work no matter where this is launched from -----------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.chdir(_HERE)

import folium  # noqa: E402
import pandas as pd  # noqa: E402
from flask import Flask, Response, jsonify, render_template_string, request  # noqa: E402

try:
    import main  # noqa: E402  (runs load_dotenv, builds ors_client, inits PeMSHandler)
except ValueError as _exc:
    if "API key" in str(_exc):
        raise SystemExit(
            "Cannot import main.py: ORS_API_KEY is not set.\n"
            f"  Put ORS_API_KEY=... in {os.path.join(_HERE, '.env')} "
            "(main.py calls load_dotenv() at import), or export it in your shell."
        ) from _exc
    raise
except ModuleNotFoundError as _exc:
    raise SystemExit(
        f"Cannot import main.py: {_exc}\n"
        f"  app.py must live next to main.py (looked in {_HERE}) and share its venv.\n"
        "  Deps: flask pandas numpy folium openrouteservice python-dotenv plotly"
    ) from _exc


# --- speed: cache the reference CSV ------------------------------------------
_original_loader = main._load_merged_geo_pm


@functools.lru_cache(maxsize=4)
def _cached_loader(ref_df_path=main.MERGED_GEO_PM):
    return _original_loader(ref_df_path)


if not os.getenv("POSTMILE_NO_CACHE"):
    main._load_merged_geo_pm = _cached_loader


def reference_df():
    """The merged reference table, or None if it isn't on disk yet."""
    try:
        return main._load_merged_geo_pm(main.MERGED_GEO_PM)
    except Exception:
        return None


def _route_dir_cols(df):
    try:
        return main._route_dir_columns(df)
    except Exception:
        return "Route", "Direction"


app = Flask(__name__)


# ----------------------------------------------------------------------------
# templates
# ----------------------------------------------------------------------------
PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PeMS Postmile Verification</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         background:#f4f6f8; color:#16181d; }
  .wrap { max-width:1180px; margin:0 auto; padding:22px 20px 40px; }
  h1 { font-size:19px; margin:0 0 3px; }
  p.sub { margin:0 0 16px; color:#667085; font-size:13px; }
  form { background:#fff; border:1px solid #e2e5ea; border-radius:10px; padding:14px 16px;
         display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
         align-items:end; }
  label { display:block; font-size:11px; font-weight:700; color:#475467;
          text-transform:uppercase; letter-spacing:.05em; margin-bottom:5px; }
  input[type=text] { width:100%; padding:8px 10px; font-size:14px; border:1px solid #cfd4dc;
                     border-radius:6px; background:#fff; font-variant-numeric:tabular-nums; }
  input:disabled { background:#f2f4f7; color:#98a2b3; }
  .chk { display:flex; align-items:center; gap:7px; font-size:13px; padding-bottom:8px; }
  .chk label { margin:0; text-transform:none; letter-spacing:0; font-size:13px; font-weight:500; }
  button { padding:9px 16px; font-size:14px; font-weight:600; border:0; border-radius:6px;
           background:#2563eb; color:#fff; cursor:pointer; width:100%; }
  button:hover { background:#1d4ed8; }
  .hint { font-size:12px; color:#667085; min-height:18px; margin:8px 2px 0; }
  .banner { margin:14px 0 0; padding:10px 14px; border-radius:8px; font-size:13px;
            background:#fff7e6; border:1px solid #f0c36d; color:#7a5200; }
  .card { margin-top:16px; background:#fff; border:1px solid #e2e5ea; border-radius:10px;
          overflow:hidden; }
  .card > header { padding:9px 14px; border-bottom:1px solid #eef0f3; font-size:12.5px;
                   color:#475467; display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .card a { color:#2563eb; text-decoration:none; font-weight:600; }
  .card a:hover { text-decoration:underline; }
  iframe { display:block; width:100%; height:620px; border:0; background:#fff; }
  code { background:#eef1f5; padding:1px 5px; border-radius:4px; font-size:12px; }
</style>
<div class="wrap">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 12px;">
    <h1 style="margin:0;">PeMS Postmile Verification</h1>
    <a href="/projects/map" target="_blank" style="background:#2563eb; color:#fff; padding:6px 14px; border-radius:6px; text-decoration:none; font-size:13px; font-weight:600;">🗺️ View All CA Projects Map</a>
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
    <b>{{ merged_path }}</b> not loaded. Run <code>merged_df(write=True)</code> in main.py
    (after the PeMS downloads exist) and then <code>POST /api/reload</code>.
    Route suggestions below fall back to <code>main.ALL_ROUTES</code>.
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


def _err(title, detail=None, suggestion=None, code=400):
    return render_template_string(
        ERROR_PAGE, title=title, detail=detail, suggestion=suggestion
    ), code


# ----------------------------------------------------------------------------
# request parsing
# ----------------------------------------------------------------------------
def _params(src):
    return {
        "route": (src.get("route") or "").strip(),
        "direction": (src.get("direction") or "").strip().upper(),
        "begin_pm": (src.get("begin_pm") or "").strip(),
        "end_pm": (src.get("end_pm") or "").strip(),
        "county": (src.get("county") or "").strip(),
        "absolute": (src.get("absolute") or "") .lower() in ("1", "true", "on", "yes"),
        "mode": (src.get("mode") or "route").strip().lower(),
        "download": (src.get("download") or "").lower() in ("1", "true", "on", "yes"),
    }


def _points_map(start, end, tiles=r"https://basemaps.cartocdn.com/rastertiles/voyager/%7Bz%7D/%7Bx%7D/%7By%7D.png?key=cb1_32r8_1_97041a40e86fa5f20f73e0c3"):
    """ORS-free fallback: the two postmile locations + a straight line."""
    m = folium.Map(location=main.coord_centroid((start, end)), tiles=tiles, zoom_start=12)
    folium.PolyLine([start, end], weight=4, dash_array="6,8", color="#b42318").add_to(m)
    folium.Marker(start, tooltip=f"begin PM  {start[0]:.5f}, {start[1]:.5f}",
                  icon=folium.Icon(color="green")).add_to(m)
    folium.Marker(end, tooltip=f"end PM  {end[0]:.5f}, {end[1]:.5f}",
                  icon=folium.Icon(color="red")).add_to(m)
    m.fit_bounds([start, end], padding=(40, 40))
    return m


# ----------------------------------------------------------------------------
# views
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    v = _params(request.args)
    if not request.args:
        v["absolute"] = True  # main.py's default

    df = reference_df()
    if df is not None:
        route_col, _ = _route_dir_cols(df)
        routes = sorted(df[route_col].astype(str).str.strip().unique(),
                        key=lambda r: (len(r), r))
    else:
        routes = main.ALL_ROUTES

    return render_template_string(
        PAGE, v=v, routes=routes, ref_ok=df is not None,
        merged_path=main.MERGED_GEO_PM,
    )


@app.route("/map", methods=["GET", "POST"])
def map_view():
    src = request.form if request.method == "POST" else request.args
    v = _params(src)

    missing = [k for k in ("route", "direction", "begin_pm", "end_pm") if not v[k]]
    if missing:
        return _err("Missing input", detail="Required: " + ", ".join(missing))
    if not v["absolute"] and not v["county"]:
        return _err("Missing input",
                    detail="County is required when matching California postmiles.")

    # Custom postmile input validations
    for k in ("begin_pm", "end_pm"):
        val = v[k]
        if v["absolute"]:
            # Absolute postmiles must be strict floats
            try:
                float(val)
            except ValueError:
                return _err("Bad input", detail=f"{k}={val!r} is not a valid Absolute Postmile number.")
        else:
            # California postmiles can be strings like 'R3.8' or '3.80'
            _, num = main._split_ca_pm(val)
            if pd.isna(num):
                return _err("Bad input", detail=f"{k}={val!r} is not a valid California Postmile format (e.g. '3.8' or 'R3.8').")

    county = None if v["absolute"] else v["county"]

    # 1) postmiles -> coordinates
    try:
        start = main.pm_to_coord(v["begin_pm"], v["route"], v["direction"], county=county)
        end = main.pm_to_coord(v["end_pm"], v["route"], v["direction"], county=county)
    except FileNotFoundError as exc:
        return _err("Reference data missing", detail=str(exc),
                    suggestion="Run <code>merged_df(write=True)</code> in main.py, then "
                               "<code>POST /api/reload</code>.", code=503)
    except ValueError as exc:
        return _err("No matching postmile", detail=str(exc),
                    suggestion=f'Check the Abs PM range for this route: '
                               f'<a href="/api/route/{v["route"]}">/api/route/{v["route"]}</a>',
                    code=404)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", detail=traceback.format_exc(), code=500)

    # 2) coordinates -> folium map
    if v["mode"] == "points":
        m = _points_map(start, end)
    else:
        try:
            m = main.draw_route(start, end, anchor=main.coord_centroid((start, end)))
        except Exception as exc:
            qsargs = request.query_string.decode()
            sep = "&" if qsargs else ""
            return _err(
                f"Routing failed — {type(exc).__name__}: {exc}",
                detail=traceback.format_exc(),
                suggestion="The postmile lookups succeeded; only the ORS directions call "
                           f'failed. <a href="/map?{qsargs}{sep}mode=points">Plot the two '
                           "endpoints without routing</a> — that's enough to tell absolute "
                           "from California postmiles.",
                code=502,
            )

    html = m.get_root().render()

    # 3) same document either way: streamed to browser or offered as file download
    if v["download"]:
        name = (f"route_map_{v['route']}{v['direction']}_"
                f"{v['begin_pm']}_{v['end_pm']}.html")
        return Response(
            html, mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return Response(html, mimetype="text/html")


# ----------------------------------------------------------------------------
# small JSON API the form uses
# ----------------------------------------------------------------------------
@app.get("/api/routes")
def api_routes():
    df = reference_df()
    if df is None:
        return jsonify({"source": "main.ALL_ROUTES", "routes": main.ALL_ROUTES})
    route_col, _ = _route_dir_cols(df)
    routes = sorted(df[route_col].astype(str).str.strip().unique(), key=lambda r: (len(r), r))
    return jsonify({"source": main.MERGED_GEO_PM, "routes": routes})


@app.get("/api/route/<route>")
def api_route(route):
    df = reference_df()
    if df is None:
        return jsonify({"found": False, "reason": "reference file not loaded"}), 503

    route_col, dir_col = _route_dir_cols(df)
    sub = df.loc[df[route_col].astype(str).str.strip() == str(route).strip()]
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

    return jsonify({"found": True, "route": str(route),
                    "directions": sorted(out, key=lambda x: x["direction"]),
                    "counties": counties})


@app.post("/api/reload")
def api_reload():
    _cached_loader.cache_clear()
    df = reference_df()
    return jsonify({"reloaded": True, "rows": None if df is None else int(len(df)),
                    "columns": [] if df is None else list(df.columns)})


# ----------------------------------------------------------------------------
# project maps (multi-project and single-project)
# ----------------------------------------------------------------------------
@app.get("/projects/map")
def projects_map_view():
    import pems_interface
    dist = request.args.get("dist")
    m = pems_interface.build_multi_project_map(dist=dist)
    html = m.get_root().render()
    return Response(html, mimetype="text/html")


@app.get("/projects/map/<pid>")
def project_single_map_view(pid):
    import pems_interface
    m = pems_interface.build_single_project_map(pid)
    if m is None:
        return _err("Project not found", detail=f"No project with ID {pid}", code=404)
    html = m.get_root().render()
    return Response(html, mimetype="text/html")


@app.get("/healthz")
def healthz():
    df = reference_df()
    return jsonify({
        "ok": True,
        "main_file": getattr(main, "__file__", None),
        "cwd": os.getcwd(),
        "save_path": main.SAVE_PATH,
        "merged_geo_pm": main.MERGED_GEO_PM,
        "reference_rows": None if df is None else int(len(df)),
        "ors_key_set": bool(main.ORS_API_KEY),
        "pems_handler": hasattr(main, "pems"),
        "cache": "off" if os.getenv("POSTMILE_NO_CACHE") else "on",
    })


if __name__ == "__main__":
    print(f"main.py      : {getattr(main, '__file__', '?')}")
    print(f"reference    : {main.MERGED_GEO_PM}")
    df0 = reference_df()
    print(f"               {'not found' if df0 is None else str(len(df0)) + ' rows'}")
    print(f"ORS key set  : {bool(main.ORS_API_KEY)}")
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=int(os.getenv("FLASK_PORT", "5000")),
        debug=bool(os.getenv("FLASK_DEBUG")),
        threaded=True,
    )