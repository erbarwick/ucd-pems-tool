# CERCET California Highway Expansion & PeMS Interface

This repository provides tools for auditing, verifying, and querying Caltrans Performance Measurement System (PeMS) traffic sensor data, geometric alignments, and Caltrans Census stations for highway capacity expansion projects across California.

It supports the **CERCET** (Center for Research on Connected and Emerging Transportation) study on induced Vehicle Miles Traveled (VMT) and historical forecast accuracy.

---

## Architecture & File Structure

```
ag.workspace/
├── pems_interface.py            # Consolidated PeMS interface & batch query engine
├── app.py                       # Flask web app for visual route/postmile verification
├── main.py                      # Original postmile verification and Folium map generator
├── ca_routes_dirs.csv           # Official Caltrans PeMS route direction conventions
├── downloads/
│   └── merged_geo_pm.csv        # 331,888-row reference database (Route, Dir, CA PM, Abs PM, Lat, Lng)
├── staging/
│   ├── Master_CA_Project_List_Cleaned.csv   # Cleaned master project dataset (118 rows)
│   ├── Master_CA_Project_List_Cleaned.xlsx  # Excel formatted cleaned dataset
│   ├── transform_master.py                 # Pipeline that produced cleaned dataset
│   └── validate_lane_counts.py             # Lane count consistency validator
├── tests/
│   └── test_pems_interface.py  # Branch-proven automated test suite (36 unit tests)
└── README.md
```

---

## Getting Started

### 1. Environment & Credentials

Activate the Python virtual environment and configure your `.env` file with Caltrans PeMS credentials:

```bash
# In California/python/.env or ag.workspace/.env:
PEMS_USERNAME="your_email@domain.edu"
PEMS_PASSWORD="your_pems_password"
```

Verify your credentials against PeMS:

```bash
python pems_interface.py --test-login
```

---

## Usage Guide

### A. Command-Line Interface (CLI)

#### 1. Inspect a Specific Project
Inspect all project details (route, direction, postmile span, coordinates, dates, sensors) and generate all PeMS export URLs:
```bash
python pems_interface.py --inspect 418000244
```

#### 2. Inspect with Census Station Lookup
Find the closest Caltrans Census count stations to the project's begin postmile:
```bash
python pems_interface.py --inspect 300000061 --census
```

#### 3. Ad-Hoc Corridor Query
Query coordinates, corridor reference segments, and generate a lane configuration URL for arbitrary freeway bounds:
```bash
python pems_interface.py --route 80 --dir E --begin-pm 11.4 --end-pm 12.8 --county Solano
```

#### 4. Interactive Project Mapping
Generate interactive Folium/Leaflet web maps showing road paths/corridors, postmile boundaries, and hover/click metadata:
```bash
# Map a single project by PID (creates route_map_<PID>.html and opens in browser)
python pems_interface.py --map 418000244

# Map all 118 projects across California simultaneously (with layer controls by District)
python pems_interface.py --map-all

# Map all projects within a specific District (e.g. District 4 - Bay Area)
python pems_interface.py --map-all --dist 4

# Save map to a custom path without automatically launching the browser
python pems_interface.py --map-all --output-map maps/california_all.html --no-open
```

#### 5. Batch URL Overview
Generate and preview PeMS URLs across the master dataset:
```bash
python pems_interface.py --list-urls
```

---

### B. Python API

You can import `pems_interface` as a Python module in scripts or notebooks:

```python
from pems_interface import (
    PeMSClient,
    load_master_csv,
    ProjectPeMSQueryBuilder,
    pm_to_coordinates,
    build_lane_config_url,
    build_station_aadt_url,
    build_station_flow_url,
    find_closest_census_stations,
)

# 1. High-speed postmile-to-coordinate conversion (O(1) indexed lookup)
coords = pm_to_coordinates(route="80", direction="E", pm="11.4", county="Solano")
print(f"Latitude, Longitude: {coords}")
# (38.201952, -122.156967)

# 2. Build PeMS export URLs
lane_url = build_lane_config_url(
    route="80", direction="E", start_date="2021-01-01", end_date="2024-12-31"
)
aadt_url = build_station_aadt_url(station_id="410756", start_year=2021, end_year=2022)
flow_url = build_station_flow_url(station_id="410756", start_year=2021, granularity="month")

# 3. Process projects from the master CSV
projects = load_master_csv("staging/Master_CA_Project_List_Cleaned.csv")
for proj in projects[:5]:
    builder = ProjectPeMSQueryBuilder(proj)
    start_c, end_c = builder.resolve_geometry()
    urls = builder.generate_all_urls()
    print(f"[{proj.project_id}] {urls['corridor']} -> Sensors: {proj.sensor_stations}")

# 4. Authenticate & stream downloads
client = PeMSClient()
client.login()
client.download_file(lane_url, "downloads/lane_config_80E.txt")

# 5. Closest Caltrans Census stations
census_df = find_closest_census_stations(route="80", district="4", ca_pm=11.4, top_n=3)

# 6. Interactive Folium Road Path Mapping
from pems_interface import build_single_project_map, build_multi_project_map

# Generate and view map for a single project (or split project pairs)
single_map = build_single_project_map("418000244", output_path="route_map_418000244.html")

# Generate and view map for all 118 projects with District layer controls
all_map = build_multi_project_map(output_path="all_ca_projects_map.html")
```

---

### C. Web Interface (Flask)

The local Flask application includes full interactive mapping:

```bash
python app.py
```

- Open `http://127.0.0.1:5000/projects/map` to view all 118 highway projects statewide on an interactive Leaflet map with District toggle controls.
- Filter by district: `http://127.0.0.1:5000/projects/map?dist=4`.
- View an individual project corridor: `http://127.0.0.1:5000/projects/map/<pid>`.
- Hovering over any corridor reveals the Project ID, Route, Direction, District, County, Postmile bounds, and directional lane counts. Clicking opens a comprehensive project card with direct external links to Caltrans PeMS and sensor stations.

---

## PeMS Query Specifications

`pems_interface.py` standardizes query parameters according to Caltrans PeMS web endpoints:

| Endpoint | PeMS Node / Content | Tab | Format / Output |
| :--- | :--- | :--- | :--- |
| **Lane Config Time Series** | `dnode=Freeway&content=elv` | `config_timeseries` | `export=text&q=lanes&gn=day` |
| **Station Annual AADT** | `dnode=VDS&content=analysis` | `aadt` | `export=text&station_id={id}` (epoch timestamps) |
| **Station Detector Flow** | `dnode=VDS&content=loops` | `det_timeseries` | `export=text&station_id={id}&q=flow&gn=month` |
| **Freeway Lat/Long Path** | `dnode=Freeway&content=fwy_latlngXY` | - | `export=text&fwy={rte}&dir={dir}` |
| **Clearinghouse Raw** | `srq=clearinghouse` | - | `district_id={dist}&yy={year}&type={type}` |

---

## Running Automated Tests

Run the full test suite using Python's standard `unittest` framework:

```bash
python -m unittest tests/test_pems_interface.py -v
```

The test suite covers:
- Input normalization (`normalize_direction`, `normalize_route`, prefix/suffix `split_ca_pm`).
- Query URL parameters and Unix epoch timestamp alignment.
- Validation between valid PeMS text tables and HTML error pages.
- Authentication state transitions, credential validation, and automatic re-login on expired sessions.
- Geometric postmile bounds matching and coordinate interpolation.
- Master CSV row parsing and ISO date resolution.

---

## Safety & Data Integrity

- **Box Folder Safeguard**: The external Box sync directory is strictly treated as read-only. All transformations, outputs, and downloads are restricted to the local working directory (`ag.workspace`).
