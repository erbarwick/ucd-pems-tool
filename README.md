# PeMS Tools for UCD CERCET Project Team

This repository contains tools for auditing, verifying, mapping, and downloading traffic data from the Caltrans
Performance Measurement System (PeMS). It was developed for a study on the accuracy of highway capacity expansion
forecasting.

## Setting it up:

Make sure you have Python 3.9 or later installed. On Mac, open the **Terminal** application and run `python3 --version`.
On **Windows**, open **PowerShell** and run `python --version`.

If Python is not installed, download the latest installer for your OS at https://www.python.org/downloads. If you are
installing on Windows, make sure you check the box that says "Add python.exe to PATH"

Once everything is installed, execute the following commands one at a time based on your OS:

### Mac (Terminal)

```bash
# Open the directory where you want to copy the repository
cd /path/to/folder

# Clone repository
git clone https://github.com/erbarwick/ucd-pems-tool.git
cd ucd-pems-tool

# Create the venv folder and activate your python virtual environment (typically required)
# When activated, your terminal prompt should show `(.venv)` on the left
python3 -m venv .venv
source .venv/bin/activate

# With .venv active:
pip install -r requirements.txt

# Copy .env template
cp .env.example .env

# This should work, but if not, just open .env from this folder in the TextEdit app. 
# If you can't see it in the folder, hit <Cmd+Shift+.> to show hidden files. 
open .env
```

In TextEdit you should see:

   ```env
   PEMS_USERNAME="your_email@university.edu"
   PEMS_PASSWORD="your_actual_password"
   ```

Replace the values with your own login. If you use this password on other sites, I highly recommend changing it on the
PeMS site, as PeMS does not always use encrypted HTTP.

To test that your credentials work, run `python3 pems_interface.py --test-login` (with the venv activated)

### Windows (PowerShell)

```shell
# Open the directory where you want to copy the repository
cd C:\path\to\folder

# Clone repository
git clone https://github.com/erbarwick/ucd-pems-tool.git
cd ucd-pems-tool

# Create the venv folder and activate your python virtual environment (typically required)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# With .venv active:
pip install -r requirements.txt 

# Copy .env template
copy .env.example .env

# This should work but if not, just open .env from this folder in the Notepad app 
notepad .env
```

> NOTE: If you get an error saying running scripts is disabled, run this command and try again:
> ```shell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

In Notepad you should see:

   ```env
   PEMS_USERNAME="your_email@university.edu"
   PEMS_PASSWORD="your_actual_password"
   ```

Replace the values with your own login. If you use this password on other sites, I highly recommend changing it on the
PeMS site, as PeMS does not always use encrypted HTTP.

To test that your credentials work, run `python pems_interface.py --test-login` (with the venv activated)

---

## Data sources explained

### Source 1: Master Project Spreadsheet (`staging/Master_CA_Project_List_Cleaned.csv`)

> To use a different or updated project spreadsheet, pass `--input path/to/your_file.csv` to any CLI command, or overwrite `staging/Master_CA_Project_List_Cleaned.csv` to make it the default. If you have a raw uncleaned spreadsheet, save it as `staging/master_sheet3_raw.csv` and run `python staging/transform_master.py` to clean and reformat it.

Contains highway capacity expansion projects across CA. Each row is a continuous section of roadway where a capacity
expansion took place (cannot have two highway directions in one row).

Columns:

- `Project ID`
- `District` (Caltrans District 1 through 12)
- `County` (e.g. San Mateo, Alameda, Los Angeles)
- `Route` (e.g. 80, 101, 5)
- `Direction` (NB, SB, EB, WB)
- `Begin PM` and `End PM` (postmile boundaries)
- `Sensor Station 1 ID`, `Sensor Station 1 HOV ID`, `Sensor Station 2 ID`, etc.

### Source 2: PeMS Highway Reference CSV (`downloads/merged_geo_pm.csv`)

Contains 331,888 rows, where each row is a 0.1-mile interval with physical alignment information (postmiles,
coordinates) of every
highway in California

- **State Postmile (`CA PM`) vs. Absolute Postmile (`Abs PM`)**:
    - **State Postmiles (`CA PM`)**: For state planning purposes; CA PM resets to 0 at every county boundary
    - **Absolute Postmiles (`Abs PM`)**: More stable over time; 0 starts at the southern or western boundary of
      California.

### Source 3: PeMS Detector Metadata (`downloads/metadata/`)

This folder contains sensor station metadata tables for Caltrans Districts 3-12 (one file for each)

**Columns:**

- Station ID
- Freeway
- Direction
- District
- County
- State PM
- Absolute PM
- Detector Type: (Mainline `ML`, HOV `HV`, On-Ramp `OR`, Off-Ramp `FR`)
- Number of Lanes
- Station Name

**`pems_interface.py` uses these files for finding a project's relevant sensors:**

1. Filters all detectors matching the project's exact `District`, `Route`, `Direction`, and `County`.
2. Selects detectors with PMs between `[Begin PM - buffer, End PM + buffer]`. Default buffer is `±0.1` miles.
3. Sorts detectors along the corridor by PM
4. Compares detectors in the postmile range with what we have in the spreadsheet, describes it as one of the
   following:
    - "Exact Match": The spreadsheet lists the exact same stations
    - "Partial Match": The spreadsheet lists some of the in-range stations
    - "Discrepancy": The spreadsheet lists a station that is not located on that highway section (might be the wrong
      direction).
    - "Sheet Empty": The spreadsheet has no station listed despite in-range stations existing
    - "Uninstrumented": Likely rural corridor where no PeMS stations exist.

### Source 4: Live PeMS queries

When downloading data, the program logs into `https://pems.dot.ca.gov` using your credentials (see near the top of this
document for a guide on setting up your environment). Due to the parameterized and consistent format of PeMS URLs, the
script constructs URLs that point directly to the exact download link needed.
See the "PEMS URL BUILDERS" section in `pems_interface.py`.

When requesting several years of data at once, PeMS truncates data. The tool queries PeMS **one year at a time** and
verifies that the returned text contains an actual data table.

---

## Command-Line Interface (CLI) Quick Reference

The CLI in `pems_interface.py` provides an intuitive, streamlined interface organized around your target project and task. All flags can be combined naturally with `-p <PID>` or passed directly:

```bash
# 1. INSPECT a project (description, postmiles, dates, sensor stations, and PeMS URLs):
python pems_interface.py -p 400000680

# 2. HIGH-RES CORRIDOR SLICES (Native Zoom 19 Before/After curtain-wipe comparison report):
python pems_interface.py -p 400000680 --slices

# 3. SATELLITE IMAGERY (download Before & After full corridor or segment image):
python pems_interface.py -p 400000680 --imagery
python pems_interface.py -p 400000680 --imagery --aligned     # Rotated corridor strip

# 4. INTERACTIVE MAP (open project road path and detector stations in browser):
python pems_interface.py -p 400000680 --map
python pems_interface.py --map-all                           # All California projects

# 5. DOWNLOAD PeMS DETECTOR DATA:
python pems_interface.py -p 400000680 --download             # All stations for project
python pems_interface.py -s 404534                           # Single station across years

# 6. AUDIT SPREADSHEET VS. ACTIVE PeMS DETECTORS:
python pems_interface.py --audit                             # Dry-run statewide
python pems_interface.py -p 400000680 --audit                # Audit specific project
python pems_interface.py --audit --apply                     # Commit updates with backup
```

> **Note:** All previous commands (such as `--inspect <PID>`, `--download-project <PID>`, `--download-station <SID>`, `--corridor-slices`, and `--match-stations`) remain 100% supported for backward compatibility. Run `python pems_interface.py --help` to see all grouped options and aliases.

---

## Downloading data from PeMS

### With the command-line interface (CLI)

The easiest way is via the CLI, either in macOS Terminal or Windows PowerShell. After following the setup steps at the
top of the README, you can try some of the examples below. `pems_interface.py` also has a lot of good examples.

This command downloads all monthly **flow** data for a station across all available years.  
This is a lot of data take a while due to PeMS' slow servers, so 

```bash
python pems_interface.py --download-station 400001
```

This command will:

- Log into PeMS using your `.env` credentials (see above)
- Constructs the download links for station `400001`'s flow data year by year, and fetches the files at those links
  (years where the station was not yet installed or had no data are automatically skipped)
- Saves each raw annual file as `station_400001_2022_flow_month.txt`, `station_400001_2023_flow_month.txt`, etc.
- Merges all active years into one CSV file, such as `station_400001_flow_month_all_years.csv`

---

### Choosing data type and time granularity (examples)

```bash
# Download DAILY flow data for all years
python pems_interface.py --download-station 400001 --granularity day

# Download HOURLY average speed data for all years
python pems_interface.py --download-station 400001 --data-type speed --granularity hour

# Download 5-MINUTE flow data for a specific year range
python pems_interface.py --download-station 400001 --granularity 5min --start-year 2023 --end-year 2023

# Download ANNUAL AADT reports
python pems_interface.py --download-station 400001 --data-type aadt
```

### Supported options

| Parameter       | Possible values                                       | Default           | Description                                                                |
|:----------------|:------------------------------------------------------|:------------------|:---------------------------------------------------------------------------|
| `--data-type`   | `flow`, `occ`, `speed`, `vmt`, `vht`, `delay`, `aadt` | `flow`            | Traffic volume, occupancy %, speed (mph), VMT, VHT, delay, or annual AADT. |
| `--granularity` | `month`, `day`, `hour`, `week`, `year`, `5min`        | `month`           | Time aggregation interval (verify if supported for your `data-type`)       |
| `--start-year`  | Any 4-digit year (e.g. `2015`)                        | `1999`            | Earliest year to query.                                                    |
| `--end-year`    | Any 4-digit year (e.g. `2024`)                        | Current year      | Latest year to query.                                                      |
| `--output-dir`  | Any folder path (e.g. `data/flow`)                    | `.` (current dir) | Local folder where files will be saved.                                    |
| `--no-combine`  | Flag (no value)                                       | Disabled          | Saves raw annual `.txt` files but does not create the master `.csv`.       |
| `--overwrite`   | Flag (no value)                                       | Disabled          | Re-downloads data even if valid files already exist locally.               |

---

### Downloading all stations for a project

You can download data for every detector station associated with a specific project ID in the master spreadsheet:

```bash
# Using the target project flag:
python pems_interface.py -p 418000244 --download --output-dir my_project_data

# Or directly:
python pems_interface.py --download 418000244 --output-dir my_project_data
```

This creates a project folder containing subfolders for each sensor station, with raw annual text exports and merged CSVs.

---

### Downloading historical satellite imagery (Before & After)

You can download and stitch historical aerial/satellite imagery from the Esri Wayback archive for any project:

```bash
# Download both BEFORE (pre-construction) and AFTER (post-opening) imagery
python pems_interface.py -p 218000125 --imagery

# Download only pre-construction imagery
python pems_interface.py -p 218000125 --imagery --period before

# Download aligned corridor imagery (rotated horizontally along highway alignment)
python pems_interface.py -p 218000125 --imagery --aligned

# Download imagery for a custom date
python pems_interface.py -p 218000125 --imagery --date 2019-06-01

# High-Resolution (Native Zoom 19) Slices with Interactive Curtain-Wipe HTML Report:
python pems_interface.py -p 313000020 --slices

# Customize slice length (e.g. 0.25 miles for winding corridors)
python pems_interface.py -p 313000020 --slices --slice-mi 0.25
```

When using `--corridor-slices`, the tool creates an organized folder per project containing:
- `01_PM..._stacked.png`: Native Zoom 19 (~0.3m / 11 in per pixel) vertically stacked Before/After comparison images with road geometry aligned pixel-for-pixel.
- `index.html`: A self-contained, interactive HTML comparison report with interactive curtain-wipe split sliders, view toggles (wipe, stacked, side-by-side), and PeMS sensor pins along the corridor.

---

## 4. Auditing project data

To inspect all project attributes (route, direction, postmiles, coordinates, dates, and sensors) and view PeMS URLs:

```bash
python pems_interface.py --inspect 418000244
```

To run a check across all projects to identify which spreadsheet sensors match active PeMS sensors and flag
discrepancies:

```bash
# Dry run across all projects (CSV not modified)
python pems_interface.py --match-stations

# Filter check to a single district or project ID
python pems_interface.py --match-stations --dist 4
python pems_interface.py --match-stations -p 414000032

# Export audit report (CSV/Markdown)
python pems_interface.py --match-stations --output-report reports/station_audit.csv
```

To update your local copy of the master spreadsheet with the verified corridor stations:

```bash
python pems_interface.py --match-stations --apply
``` 

> Note: This command also automatically creates a timestamped backup of the master CSV before modifying.
 
---

## See the data on a map in your browser

### Generate standalone web map using the CLI:

```bash
# Map a single project with detectors and open in your default browser
python pems_interface.py --map 418000244

# Map all projects across California with layer toggles by District
python pems_interface.py --map-all

# Map all projects in District 4 with detectors
python pems_interface.py --map-all --dist 4 --include-stations
```

In the web map interface you can toggle station types on/off, hover and click over elements to show an overview of their
data and direct PeMS links.

---

### Web app for mapping and postmile verification:

```bash
python app.py
```

Then open your web browser to:

- `http://127.0.0.1:5000`: postmile route calculator and map generator
- `http://127.0.0.1:5000/projects/map`: full map of all CA projects
- `http://127.0.0.1:5000/projects/map/<PID>`: map for a specific project ID (e.g. `/projects/map/414000032`).

---

## How to use this in your own Python scripts and notebooks

You can import `pems_interface` as long as `pems_interface.py` is in the same directory

```python
import pems_interface

# PM to lat/long:
coords = pems_interface.pm_to_coordinates(
    route="80", direction="E", pm="11.4", county="Solano"
)
print("Latitude, Longitude:", coords)
# (38.201952, -122.156967)

# Download monthly flow data for a station:
summary = pems_interface.download_station_data(
    station_id="400001",
    data_type="flow",
    granularity="month",
    start_year=2020,
    end_year=2024,
    output_dir="./data"
)
print(f"Downloaded {summary['total_records']} records to {summary['combined_file']}")

# Find the closest Census count stations:
census_df = pems_interface.find_closest_census_stations(
    route="80", district="4", ca_pm=11.4, top_n=3
)
print(census_df[["ID", "CA PM", "Name", "pm_diff"]])
# Download BEFORE and AFTER satellite imagery for a project:
imagery = pems_interface.get_project_imagery("218000125", period="both")
print("Before image path:", imagery["imagery"]["before"]["path"])
print("After image path:", imagery["imagery"]["after"]["path"])
```
