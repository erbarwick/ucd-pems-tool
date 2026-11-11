"""Configuration, paths, environment loading, and static Caltrans/HPMS constants."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_URL = "https://pems.dot.ca.gov"

try:
    from dotenv import find_dotenv

    _found_env = find_dotenv()
    load_dotenv(_found_env if _found_env else None)
except Exception:
    try:
        load_dotenv()
    except Exception:
        pass

DEFAULT_REF_PATH = os.path.join(WORKSPACE_DIR, "downloads", "merged_geo_pm.csv")
DEFAULT_MASTER_CSV = os.path.join(WORKSPACE_DIR, "staging", "Master_CA_Project_List_Cleaned.csv")
DEFAULT_METADATA_DIR = os.path.join(WORKSPACE_DIR, "downloads", "metadata")
DEFAULT_STATION_CONFIG_CACHE_DIR = os.path.join(WORKSPACE_DIR, "downloads", "station_config_cache")
DEFAULT_HPMS_CACHE_DIR = os.path.join(WORKSPACE_DIR, "downloads", "hpms_cache")

CENSUS_PATHS = [
    os.path.join(WORKSPACE_DIR, "ALL_CENSUS_STATIONS.tsv"),
    os.path.join(WORKSPACE_DIR, "downloads", "ALL_CENSUS_STATIONS.tsv"),
    os.path.abspath(os.path.join(WORKSPACE_DIR, "..", "..", "..", "notes", "AADT Comparisons", "data", "ALL_CENSUS_STATIONS.tsv")),
]

TABLE_MARKERS = (
    "Starting Month",
    "Month",
    "Day",
    "Time",
    "Station",
    "VDS",
    "Hour",
    "ID\t",
    "ID,",
    "Latitude\t",
    "Latitude,",
    "Fwy\t",
    "Fwy,",
    "Abs_PM\t",
    "State_PM\t",
)

MONO_FONT = "'Andale Mono', Menlo, Monaco, 'Courier New', Courier, monospace"

LOGGER = logging.getLogger("pems_interface")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        stream=sys.stdout,
    )

CALTRANS_COUNTY_ABBREVIATIONS: Dict[str, str] = {
    "ala": "Alameda", "alp": "Alpine", "ama": "Amador", "but": "Butte", "cal": "Calaveras",
    "col": "Colusa", "cc": "Contra Costa", "dn": "Del Norte", "ed": "El Dorado", "fre": "Fresno",
    "gle": "Glenn", "hum": "Humboldt", "imp": "Imperial", "iny": "Inyo", "ker": "Kern",
    "kin": "Kings", "lak": "Lake", "las": "Lassen", "la": "Los Angeles", "mad": "Madera",
    "mrn": "Marin", "mpa": "Mariposa", "men": "Mendocino", "mer": "Merced", "mod": "Modoc",
    "mno": "Mono", "mon": "Monterey", "nap": "Napa", "nev": "Nevada", "ora": "Orange",
    "pla": "Placer", "plu": "Plumas", "riv": "Riverside", "sac": "Sacramento", "sbt": "San Benito",
    "sbd": "San Bernardino", "sd": "San Diego", "sf": "San Francisco", "sj": "San Joaquin",
    "slo": "San Luis Obispo", "sm": "San Mateo", "sba": "Santa Barbara", "scl": "Santa Clara",
    "scr": "Santa Cruz", "sha": "Shasta", "sie": "Sierra", "sis": "Siskiyou", "sol": "Solano",
    "son": "Sonoma", "sta": "Stanislaus", "sut": "Sutter", "teh": "Tehama", "tri": "Trinity",
    "tul": "Tulare", "tuo": "Tuolumne", "ven": "Ventura", "yol": "Yolo", "yub": "Yuba",
}

CA_STATE_FIPS = 6

CA_COUNTY_FIPS: Dict[str, int] = {
    "alameda": 1, "alpine": 3, "amador": 5, "butte": 7, "calaveras": 9,
    "colusa": 11, "contra costa": 13, "del norte": 15, "el dorado": 17, "fresno": 19,
    "glenn": 21, "humboldt": 23, "imperial": 25, "inyo": 27, "kern": 29,
    "kings": 31, "lake": 33, "lassen": 35, "los angeles": 37, "madera": 39,
    "marin": 41, "mariposa": 43, "mendocino": 45, "merced": 47, "modoc": 49,
    "mono": 51, "monterey": 53, "napa": 55, "nevada": 57, "orange": 59,
    "placer": 61, "plumas": 63, "riverside": 65, "sacramento": 67, "san benito": 69,
    "san bernardino": 71, "san diego": 73, "san francisco": 75, "san joaquin": 77,
    "san luis obispo": 79, "san mateo": 81, "santa barbara": 83, "santa clara": 85,
    "santa cruz": 87, "shasta": 89, "sierra": 91, "siskiyou": 93, "solano": 95,
    "sonoma": 97, "stanislaus": 99, "sutter": 101, "tehama": 103, "trinity": 105,
    "tulare": 107, "tuolumne": 109, "ventura": 111, "yolo": 113, "yuba": 115,
}
CA_FIPS_TO_COUNTY: Dict[int, str] = {v: k.title() for k, v in CA_COUNTY_FIPS.items()}

HPMS_SPATIAL_ENDPOINTS: Dict[int, str] = {2024: "42um-tgh5"}

HPMS_SECTION_ENDPOINTS: Dict[int, str] = {
    2024: "4wpf-u82y",
    2023: "8gz6-23ex",
    2022: "fsfq-kawt",
    2021: "trfk-mhda",
    2020: "q8hw-cdnd",
    2019: "rkzg-z7ht",
    2018: "v9ae-hsuk",
}

HPMS_HISTORIC_UNIVERSE: Dict[int, str] = {
    2008: "65pp-nswq", 2007: "9vq4-dn8b", 2006: "w95q-6nqw", 2005: "8xu8-ax8r",
    2004: "kpa7-pmax", 2003: "ismp-dmnm", 2002: "brry-xrw9", 2001: "w2yw-84zu",
    2000: "hddu-nghv", 1999: "6mnf-74vh", 1998: "dzuk-qpr3", 1997: "sgat-h8mv",
    1996: "922p-sj4m", 1995: "3n3x-bbb4", 1994: "affx-yhsw", 1993: "g5nf-5mpb",
    1992: "5cn3-yynq", 1991: "mhuc-n78n", 1990: "mp2w-t2nd", 1989: "mxiv-t53i",
    1988: "dbr2-hxmv", 1987: "e56t-hfa9", 1986: "cu4q-nx8c", 1985: "2m53-wgng",
    1984: "j2gk-sh7k", 1983: "6itn-mw5q", 1982: "br2w-67fy", 1981: "wekv-ifsj",
    1980: "efu4-vidc",
}

HPMS_FACILITY_TYPES: Dict[str, str] = {
    "1": "One-Way Roadway",
    "2": "Two-Way Divided (Freeway/Expressway)",
    "4": "Two-Way Undivided",
    "5": "Others / Ramps",
}

HPMS_ACCESS_CONTROL: Dict[str, str] = {
    "1": "Full Access Control (Freeway)",
    "2": "Partial Access Control (Expressway)",
    "3": "No Access Control (Conventional Highway)",
}

HPMS_SURFACE_TYPES: Dict[str, str] = {
    "1": "Unpaved",
    "2": "Bituminous (Asphalt)",
    "3": "JPCP (Jointed Plain Concrete)",
    "4": "JRCP (Jointed Reinforced Concrete)",
    "5": "CRCP (Continuously Reinforced Concrete)",
    "6": "AC Overlay on AC",
    "7": "AC Overlay on PCC",
    "8": "PCC Overlay on PCC",
    "9": "Other",
}

HPMS_FUNCTIONAL_SYSTEMS: Dict[str, str] = {
    "1": "Interstate",
    "2": "Principal Arterial - Other Freeways & Expressways",
    "3": "Principal Arterial - Other",
    "4": "Minor Arterial",
    "5": "Major Collector",
    "6": "Minor Collector",
    "7": "Local",
}


def decode_hpms_attributes(sections: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Decode categorical HPMS roadway attributes across a list of sections."""

    def _collect(field_name: str, mapping: Dict[str, str]) -> Optional[str]:
        vals = [
            mapping.get(str(sec.get(field_name)), str(sec.get(field_name)))
            for sec in sections
            if sec.get(field_name) is not None
        ]
        return ", ".join(dict.fromkeys(vals)) if vals else None

    fa_vals = [sec.get("future_aadt") for sec in sections if sec.get("future_aadt")]
    fa_yr_vals = [str(sec.get("future_aadt_year"))[:4] for sec in sections if sec.get("future_aadt_year")]
    return {
        "facility_type": _collect("facility_type", HPMS_FACILITY_TYPES),
        "access_control": _collect("access_control", HPMS_ACCESS_CONTROL),
        "surface_type": _collect("surface_type", HPMS_SURFACE_TYPES),
        "functional_system": _collect("f_system", HPMS_FUNCTIONAL_SYSTEMS),
        "future_aadt": fa_vals[0] if fa_vals else None,
        "future_aadt_year": fa_yr_vals[0] if fa_yr_vals else None,
    }


DISTRICT_COLORS: Dict[str, str] = {
    "1": "#2563eb",
    "2": "#059669",
    "3": "#16a34a",
    "4": "#0284c7",
    "5": "#d97706",
    "6": "#0d9488",
    "7": "#dc2626",
    "8": "#9333ea",
    "9": "#b45309",
    "10": "#ea580c",
    "11": "#c026d3",
    "12": "#e11d48",
}

STATION_TYPE_NAMES: Dict[str, str] = {
    "ML": "Mainline",
    "HV": "HOV / Carpool",
    "OR": "On-Ramp",
    "FR": "Off-Ramp",
    "FF": "Freeway Connector",
    "CD": "Collector-Distributor",
    "CH": "Commercial / Inspection",
}

STATION_TYPE_COLORS: Dict[str, str] = {
    "ML": "#1d4ed8",
    "HV": "#7e22ce",
    "OR": "#15803d",
    "FR": "#b45309",
    "FF": "#475569",
    "CD": "#0f766e",
    "CH": "#6b7280",
}

STATION_LAYER_ORDER = ("ML", "HV", "OR", "FR", "Other")
STATION_LAYER_LABELS: Dict[str, str] = {
    "ML": "Mainline (ML)",
    "HV": "Carpool/HOV (HV)",
    "OR": "On-Ramp (OR)",
    "FR": "Off-Ramp (FR)",
    "Other": "Other (FF/CD/CH)",
}
