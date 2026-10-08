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
def draw_route(anchor: list[float], start_coord: list[float], end_coord: list[float], tiles='cartodbpositron') -> folium.Map:
    m = folium.Map(location=anchor, tiles=tiles, zoom_start=13)
    route = ors_client.directions(
        coordinates=[start_coord, end_coord],
        format='geojson',
        options={'avoid_features': ['steps']},
        validate=False
    )
    folium.PolyLine(locations=[list(reversed(coord)) for coord in route['features'][0]['geometry']['coordinates']]).add_to(m)
    return m