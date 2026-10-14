import time
import openrouteservice as ors
import plotly.graph_objs as go
import pandas as pd
import plotly.express as px

import folium

from dotenv import load_dotenv
load_dotenv()

API_KEY=os.getenv('ORS_API_KEY')

ors_client = ors.Client(key=API_KEY)

# https://github.com/GIScience/openrouteservice-py/blob/master/examples/basic_example.ipynb
def draw_route(anchor: list[float], start_coord: list[float], end_coord: list[float], tiles=None) -> folium.Map:
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
    route = ors_client.directions(
        coordinates=[start_coord, end_coord],
        format='geojson',
        options={'avoid_features': ['steps']},
        validate=False
    )
    folium.PolyLine(locations=[list(reversed(coord)) for coord in route['features'][0]['geometry']['coordinates']]).add_to(m)
    return m