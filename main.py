import os
import webbrowser

import pandas as pd
import numpy as np
import glob
import re

from numpy.ma.core import absolute

from handler import PeMSHandler
from dotenv import load_dotenv

import time
import openrouteservice as ors
import plotly.graph_objs as go
import plotly.express as px
import folium

load_dotenv()

ORS_API_KEY = os.getenv('ORS_API_KEY')
ors_client = ors.Client(key=ORS_API_KEY)

SAVE_PATH = os.getenv('PEMS_SAVE_PATH', './downloads')
MERGED_GEO_PM = SAVE_PATH + "/merged_geo_pm.csv"
ALL_ROUTES = ['10', '105', '110', '15', '205', '210', '215', '280', '380', '40', '405', '5', '505', '580', '605', '680',
              '710', '780', '8', '80', '805', '880', '905', '980', '1', '103', '104', '107', '108', '109', '11', '111',
              '112', '113', '114', '115', '116', '118', '119', '12', '120', '126', '121', '123', '124', '125', '127',
              '128', '129', '13', '130', '131', '132', '133', '134', '135', '136', '137', '138', '139', '14', '140',
              '142', '144', '145', '146', '147', '149', '150', '151', '152', '153', '154', '155', '156', '158', '16',
              '160', '161', '162', '163', '164', '165', '166', '167', '168', '169', '17', '170', '172', '173', '174',
              '175', '177', '178', '18', '180', '182', '183', '184', '185', '186', '187', '188', '189', '19', '190',
              '191', '192', '193', '195', '197', '198', '2', '20', '200', '201', '202', '203', '204', '207', '209',
              '211', '213', '216', '217', '218', '219', '22', '220', '221', '222', '223', '225', '227', '229', '23',
              '232', '233', '236', '237', '238', '24', '241', '242', '243', '244', '245', '246', '247', '25', '252',
              '253', '254', '255', '259', '26', '260', '261', '262', '263', '265', '266', '267', '269', '27', '270',
              '271', '273', '275', '28', '281', '283', '284', '29', '299', '3', '30', '32', '33', '330', '34', '35',
              '36', '37', '371', '38', '39', '4', '41', '43', '44', '45', '46', '47', '49', '51', '52', '53', '54',
              '55', '56', '57', '58', '59', '60', '61', '62', '63', '65', '66', '67', '68', '7', '70', '71', '72', '73',
              '74', '76', '77', '78', '79', '82', '83', '84', '85', '86', '87', '88', '89', '9', '90', '91', '92', '94',
              '96', '98', '99', '101', '199', '395', '50', '6', '75', '95', '97']

try:
    pems = PeMSHandler(debug=True)
except Exception as e:
    print(f"Failed to initialize PeMSHandler: {e}")


def _build_query_url(route, direction, export="text", content="fwy_latlngXY", dnode="Freeway", report_form="1"):
    return f'/?report_form={report_form}&dnode={dnode}&content={content}&export={export}&fwy={route}&dir={direction}'


def _load_merged_geo_pm(ref_df_path=MERGED_GEO_PM) -> pd.DataFrame:
    """Load merged_geo_pm.csv regardless of whether it was written as CSV or TSV.

    merged_df() used to_csv() with the default comma separator, but pm_to_coord()
    used to read it with sep='\\t'. That collapsed every header into one column
    and produced KeyError: 'Route'.
    """
    if not os.path.exists(ref_df_path):
        raise FileNotFoundError(
            f"Merged reference file not found: {ref_df_path}\n"
            "Uncomment merged_df(write=True) in main() after the PeMS downloads exist."
        )

    last_cols = []
    for sep in (",", "\t"):
        df = pd.read_csv(ref_df_path, sep=sep, dtype=str)
        df.columns = df.columns.str.strip()
        last_cols = list(df.columns)
        if "Route" in df.columns or "Fwy" in df.columns:
            return df

    raise KeyError(
        f"Neither 'Route' nor 'Fwy' found in {ref_df_path}. "
        f"Columns seen: {last_cols}\n"
        "Re-run query() (so Route/Direction are appended) then merged_df(write=True)."
    )


def _route_dir_columns(ref_df: pd.DataFrame) -> tuple[str, str]:
    route_col = "Route" if "Route" in ref_df.columns else "Fwy"
    if "Direction" in ref_df.columns:
        dir_col = "Direction"
    elif "Dir" in ref_df.columns:
        dir_col = "Dir"
    else:
        raise KeyError(f"No direction column in {list(ref_df.columns)}")
    return route_col, dir_col


def pm_to_coord(pm,
                route, direction,
                ref_df_path=MERGED_GEO_PM,
                county=None):
    absolute = True if county is None else False

    route = str(route).strip()
    direction = str(direction).strip().upper()

    ref_df = _load_merged_geo_pm(ref_df_path)
    route_col, dir_col = _route_dir_columns(ref_df)

    # Filter ref_df to Route & Direction first
    mask = (ref_df[route_col].astype(str).str.strip() == route) & (
            ref_df[dir_col].astype(str).str.strip().str.upper() == direction
    )
    ref_df = ref_df.loc[mask]

    if absolute:
        try:
            pm_float = round(float(pm), 1)
        except (TypeError, ValueError):
            raise ValueError(f"Absolute postmile must be a valid number, got: {pm!r}")

        abs_pm = pd.to_numeric(ref_df["Abs PM"], errors="coerce").round(1)
        ref_df = ref_df.loc[abs_pm == pm_float]
    else:
        if county is None:
            raise ValueError("County must be provided when matching California postmiles")

        pm_str = str(pm).strip()
        user_prefix, user_num = _split_ca_pm(pm_str)
        if np.isnan(user_num):
            raise ValueError(f"Could not parse numeric part from California postmile: {pm_str!r}")
        user_num = round(user_num, 1)

        # Split reference table's CA PM values to avoid floating string mismatches
        ca_split = ref_df["CA PM"].astype(str).map(_split_ca_pm)
        ca_prefixes = ca_split.map(lambda x: x[0].strip().upper())
        ca_numerics = ca_split.map(lambda x: x[1])

        # Match numeric part (rounded to 1 decimal place) and county
        match_mask = (ca_numerics.round(1) == user_num) & (
                ref_df["County"].astype(str).str.strip().str.lower() == str(county).strip().lower()
        )

        # If user explicitly provided a letter prefix (e.g. 'R' in 'R3.8'), require it to match
        if user_prefix:
            match_mask = match_mask & (ca_prefixes == user_prefix.upper())

        ref_df = ref_df.loc[match_mask]

        if not ref_df.empty:
            abs_pm_val = ref_df["Abs PM"].iloc[0]
            print(f"ABSOLUTE POSTMILE from California postmile: {abs_pm_val}")

    if ref_df.empty:
        raise ValueError(
            f"No rows matched route={route} direction={direction} "
            f"{'Abs PM' if absolute else 'CA PM'}={pm}"
            + (f" county={county}" if not absolute else "")
        )

    row = ref_df.iloc[0]
    coord = [float(row["Latitude"]), float(row["Longitude"])]
    print(f"COORDINATES FROM {'ABSOLUTE' if absolute else 'CALIFORNIA'} POSTMILE: ")
    print(f"{coord[0]}, {coord[1]}")
    return coord


"""
Takes CA PM, which is sometimes a raw number value and sometimes has a prefix (for example, 'L0.60' or '1.71').
Returns a tuple with one string and one float, derived by splitting the CA PM (for example, ('L', 0.60) or ('', 1.71)
"""


def _split_ca_pm(ca_pm: str) -> tuple:
    s = "" if ca_pm is None or (isinstance(ca_pm, float) and np.isnan(ca_pm)) else str(ca_pm).strip()
    match = re.match(r"^([A-Za-z]*)([0-9]*\.?[0-9]+)$", s)
    if match and match.group(2):
        return match.group(1), float(match.group(2))
    try:
        return "", float(s)
    except (TypeError, ValueError):
        return s, np.nan


def coord_centroid(arr):
    arr = np.asarray(arr, dtype=float)
    if arr.size == 0 or arr.shape[-1] != 2:
        raise ValueError(f"coord_centroid expected [[lat, lon], ...], got {arr!r}")
    return np.mean(arr, axis=0).tolist()


# https://github.com/GIScience/openrouteservice-py/blob/master/examples/basic_example.ipynb
def draw_route(start_coord: list[float], end_coord: list[float], anchor: list[float],
               tiles=None) -> folium.Map:
    if not start_coord or not end_coord or not anchor:
        raise ValueError(
            f"Need [lat, lon] for start/end/anchor; got start={start_coord}, end={end_coord}, anchor={anchor}"
        )
    if tiles is None or tiles in ('cartodbpositron', 'cartodb_positron'):
        m = folium.Map(location=anchor, tiles=None, zoom_start=13)
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
            attr="Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ",
            name="Esri Light Gray",
            control=True,
            max_zoom=16
        ).add_to(m)
    else:
        m = folium.Map(location=anchor, tiles=tiles, zoom_start=13)
    start_coord = list(reversed(start_coord))
    end_coord = list(reversed(end_coord))
    route = ors_client.directions(
        coordinates=[start_coord, end_coord],
        format='geojson',
        # options=({'avoid_features': ['steps']}),
        validate=False
    )
    folium.PolyLine(
        locations=[list(reversed(coord)) for coord in route['features'][0]['geometry']['coordinates']]).add_to(m)
    return m


def query(route, direction):
    url_path = _build_query_url(route, direction)
    file_name = f"pems_fwy{route}_{direction}_latlng.txt"

    print(f"Fetching data for Route {route} {direction}...")
    success = pems.download_file(file_name, url_path, save_path=SAVE_PATH)

    if success:
        file_path = os.path.join(SAVE_PATH, file_name)
        print(f"✓ Data saved to {file_path}")

        # Append freeway and direction to downloaded file
        try:
            df = pd.read_csv(file_path, sep="\t")
            df['Route'] = route
            df['Direction'] = direction
            df.to_csv(file_path, sep="\t", index=False)
            print(df.head())
        except Exception as e:
            print(f"[X] Failed to process file: {e}")

    else:
        print("[X] Failed to download file.")
    return


def loop_query_ca():
    df = pd.read_csv("ca_routes_dirs.csv", header=0, encoding="utf‑8-sig")
    df.set_index('route', inplace=True)

    for route in ALL_ROUTES:
        try:
            fwy_directions = df.at[route, 'directions'].split(",")
            for d in fwy_directions:
                query(route, d)
        except KeyError:
            print(f"Route {route} not found")
    return


def merged_df(write=False):
    files = glob.glob(os.path.join(SAVE_PATH, "pems_fwy*_latlng.txt"))
    if not files:
        raise FileNotFoundError(f"No pems_fwy*_latlng.txt files under {SAVE_PATH}")

    df = pd.concat(
        [pd.read_csv(f, sep="\t") for f in files],
        ignore_index=True
    )

    # Keep this a real CSV (comma) to match the .csv filename. _load_merged_geo_pm
    # will still accept a legacy TSV if that's what is already on disk.
    if write:
        df.to_csv(MERGED_GEO_PM, index=False)
        print(f"Wrote {len(df)} rows to {MERGED_GEO_PM}; columns={list(df.columns)}")
    return df


def user_input():
    print("--- PeMS Postmile Verification Tool ---")
    route = input("Enter freeway ROUTE number (e.g. '5' for I-5): ")
    direction = input("Enter freeway DIRECTION (e.g. 'N' for I-405N): ")
    begin_pm = input("Enter the START postmile (PM) value from spreadsheet: ")
    end_pm = input("Enter the END postmile (PM) value from spreadsheet: ")
    return route, direction, begin_pm, end_pm


def save_html_map(m: folium.Map, input_dict: dict):
    output_file = f"route_map_{input_dict['route']}{input_dict['direction']}_{input_dict['begin_pm']}_{input_dict['end_pm']}.html"
    m.save(output_file)
    print(f"Map saved to {output_file}")
    webbrowser.open(output_file)


def main():
    # Uncomment if you need to write the merged df again or download the data again:
    # merged_df(write=True)
    # loop_query_ca()

    # CHANGE THIS TO TEST DIFFERENT PMS
    # absolutely = True
    absolutely = False

    input_dict = {}
    for v in ("route", "direction", "begin_pm", "end_pm", ("county" if not absolutely else "")):
        print("Enter freeway ROUTE number (e.g. '5' for I-5): " if v == "route" else
              "Enter freeway DIRECTION (e.g. 'N' for I-405N): " if v == "direction" else
              "Enter the START postmile (PM) value from spreadsheet: " if v == "begin_pm" else
              "Enter the END postmile (PM) value from spreadsheet: " if v == "end_pm" else
              "Enter the county name from the spreadsheet: " if v == "county" else "")
        input_dict[v] = input().strip()

    if absolutely:
        start = pm_to_coord(input_dict["begin_pm"], input_dict["route"], input_dict["direction"])
        end = pm_to_coord(input_dict["end_pm"], input_dict["route"], input_dict["direction"])
    else:
        start = pm_to_coord(input_dict["begin_pm"], input_dict["route"], input_dict["direction"],
                            county=input_dict["county"])
        end = pm_to_coord(input_dict["end_pm"], input_dict["route"], input_dict["direction"],
                          county=input_dict["county"])

    # If the generated map matches the report map, the PM is absolute.
    # If it does not, the PM is CA.
    m = draw_route(start, end, anchor=coord_centroid((start, end)))
    save_html_map(m, input_dict)


if __name__ == '__main__':
    main()