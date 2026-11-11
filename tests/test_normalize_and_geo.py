#!/usr/bin/env python3
"""Unit tests for normalization, coordinate parsing, postmile geometry, and corridor search."""

import datetime
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import pems_interface
from pems.corridor import find_stations_by_coordinate_range, find_stations_by_postmile_range
from pems.geo import (
    IndexedGeoReference,
    convert_absolute_postmile,
    convert_california_postmile,
    get_corridor_stations,
    pm_to_coordinates,
    resolve_absolute_postmiles,
)
from pems.normalize import (
    build_date_from_parts,
    format_pm,
    haversine_distance_miles,
    normalize_ca_county,
    normalize_direction,
    normalize_route,
    parse_coordinate_string,
    parse_iso_date,
    split_ca_pm,
)
from pems.projects import ProjectPeMSQueryBuilder, ProjectRecord
from pems.reporting import format_single_postmile_report
from pems.vds import VDSStationRecord


class TestNormalizationHelpers(unittest.TestCase):
    """Tests for direction, route, county, postmile, and date normalization."""

    def test_normalize_direction_cardinal_nb(self):
        self.assertEqual(normalize_direction("NB"), "N")

    def test_normalize_direction_cardinal_sb(self):
        self.assertEqual(normalize_direction("SB"), "S")

    def test_normalize_direction_cardinal_eb(self):
        self.assertEqual(normalize_direction("EB"), "E")

    def test_normalize_direction_cardinal_wb(self):
        self.assertEqual(normalize_direction("WB"), "W")

    def test_normalize_direction_full_word(self):
        self.assertEqual(normalize_direction("North"), "N")

    def test_normalize_direction_empty_or_none(self):
        self.assertEqual(normalize_direction(""), "")
        self.assertEqual(normalize_direction(None), "")

    def test_normalize_direction_invalid_input_returns_empty(self):
        self.assertEqual(normalize_direction("FOO"), "")
        self.assertEqual(normalize_direction("123"), "")

    def test_normalize_route_strips_alphabetic_prefixes(self):
        self.assertEqual(normalize_route("SR-80"), "80")
        self.assertEqual(normalize_route("I-5"), "5")
        self.assertEqual(normalize_route("US-101"), "101")

    def test_normalize_route_float_values(self):
        self.assertEqual(normalize_route("5.0"), "5")
        self.assertEqual(normalize_route("80.00"), "80")
        self.assertEqual(normalize_route("SR-101"), "101")

    def test_normalize_route_handles_empty(self):
        self.assertEqual(normalize_route(""), "")
        self.assertEqual(normalize_route(None), "")

    def test_normalize_ca_county(self):
        self.assertEqual(normalize_ca_county("sonoma"), "Sonoma")
        self.assertEqual(normalize_ca_county("SON"), "Sonoma")
        self.assertEqual(normalize_ca_county("cc"), "Contra Costa")
        self.assertEqual(normalize_ca_county("MRN"), "Marin")
        self.assertEqual(normalize_ca_county("55"), "Napa")
        self.assertEqual(normalize_ca_county("13"), "Contra Costa")
        self.assertEqual(normalize_ca_county(""), "")
        self.assertEqual(normalize_ca_county(None), "")

    def test_split_ca_pm_variants(self):
        pfx, num = split_ca_pm("12.4")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(num, 12.4)

        pfx, num = split_ca_pm("R3.80")
        self.assertEqual(pfx, "R")
        self.assertAlmostEqual(num, 3.8)

        pfx, num = split_ca_pm("12.4L")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(num, 12.4)

        pfx, num = split_ca_pm("R12.4A")
        self.assertEqual(pfx, "R")
        self.assertAlmostEqual(num, 12.4)

        pfx, val = split_ca_pm("I-10 30.3")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(val, 30.3, places=2)

        pfx, val = split_ca_pm("8.6999999999999993")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(val, 8.7, places=2)

        pfx, num = split_ca_pm("")
        self.assertEqual(pfx, "")
        self.assertTrue(np.isnan(num))

        pfx, num = split_ca_pm(None)
        self.assertEqual(pfx, "")
        self.assertTrue(np.isnan(num))

    def test_format_pm_precision_prefixes_and_negatives(self):
        self.assertEqual(format_pm("8.6999999999999993"), "8.7")
        self.assertEqual(format_pm(78.400000000000006), "78.4")
        self.assertEqual(format_pm("R11.400000000000002"), "R11.4")
        self.assertEqual(format_pm("I-10 30.3"), "30.3")
        self.assertEqual(format_pm("04-ALA-80 R11.4"), "R11.4")
        self.assertEqual(format_pm("ALA 11.4"), "11.4")
        self.assertEqual(format_pm("-0.500"), "-0.5")
        self.assertEqual(format_pm("nan"), "")
        self.assertEqual(format_pm(None), "")

    def test_build_date_from_parts_and_parse_iso_date(self):
        self.assertEqual(build_date_from_parts(2019, 2, 1), datetime.date(2019, 2, 1))
        self.assertEqual(build_date_from_parts("2020", None, None, default_month=1, default_day=1), datetime.date(2020, 1, 1))
        self.assertEqual(build_date_from_parts(2022.0, 7.0, 6.0), datetime.date(2022, 7, 6))
        self.assertIsNone(build_date_from_parts(None, None, None))
        self.assertIsNone(build_date_from_parts("", "nan", "NaN"))

        self.assertEqual(parse_iso_date("2022-03-01"), datetime.date(2022, 3, 1))
        self.assertEqual(parse_iso_date("2019", 1, 1), datetime.date(2019, 1, 1))
        self.assertIsNone(parse_iso_date(None))
        self.assertIsNone(parse_iso_date(""))


class TestGeometryAndPostmileLookups(unittest.TestCase):
    """Tests verifying geometric postmile matching and CA <-> Abs PM conversions."""

    def setUp(self):
        self.mock_ref = pd.DataFrame([
            {"District": "4", "County": "Solano", "CA PM": "11.4", "Abs PM": "38.9", "Latitude": "38.201", "Longitude": "-122.156", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.0", "Abs PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.8", "Abs PM": "40.3", "Latitude": "38.216", "Longitude": "-122.138", "Route": "80", "Direction": "E"},
            {"District": "3", "County": "El Dorado", "CA PM": "R11.7", "Abs PM": "145.5", "Latitude": "38.694", "Longitude": "-120.815", "Route": "49", "Direction": "N"},
            {"Route": "5", "Direction": "S", "County": "Shasta", "CA PM": "39.0", "Abs PM": "700.0", "Latitude": "40.8688", "Longitude": "-122.3663"},
            {"Route": "5", "Direction": "S", "County": "Shasta", "CA PM": "41.2", "Abs PM": "702.2", "Latitude": "40.8946", "Longitude": "-122.3833"},
            {"Route": "138", "Direction": "W", "County": "Los Angeles", "CA PM": "R55.0", "Abs PM": "48.5", "Latitude": "34.5", "Longitude": "-117.9"},
            {"Route": "138", "Direction": "W", "County": "Los Angeles", "CA PM": "56.3", "Abs PM": "49.8", "Latitude": "34.5", "Longitude": "-117.8"},
        ])

    def test_pm_to_coordinates_absolute_match(self):
        coords = pm_to_coordinates("80", "E", 38.9, is_absolute=True, ref_df=self.mock_ref)
        self.assertIsNotNone(coords)
        self.assertAlmostEqual(coords[0], 38.201, places=3)
        self.assertAlmostEqual(coords[1], -122.156, places=3)

    def test_pm_to_coordinates_ca_pm_with_prefix(self):
        coords = pm_to_coordinates("49", "NB", "R11.7", county="El Dorado", is_absolute=False, ref_df=self.mock_ref)
        self.assertIsNotNone(coords)
        self.assertAlmostEqual(coords[0], 38.694, places=3)
        self.assertAlmostEqual(coords[1], -120.815, places=3)

    def test_pm_to_coordinates_nonexistent_returns_none(self):
        self.assertIsNone(pm_to_coordinates("80", "E", 999.0, is_absolute=True, ref_df=self.mock_ref))

    def test_get_corridor_stations_bounds(self):
        df_corr = get_corridor_stations("80", "E", 38.9, 39.5, is_absolute=True, ref_df=self.mock_ref)
        self.assertEqual(len(df_corr), 2)
        self.assertListEqual(list(df_corr["CA PM"]), ["11.4", "12.0"])

    def test_get_corridor_stations_with_string_prefixed_pms(self):
        raw_df = pd.DataFrame([
            {"Route": "80", "Direction": "E", "Abs PM": "10.0", "CA PM": "R5.0", "County": "Solano", "Latitude": "38.25", "Longitude": "-122.05"}
        ])
        idx_ref = IndexedGeoReference(raw_df)
        res = get_corridor_stations(
            route="80", direction="E", begin_pm="R4.0", end_pm="R6.0", is_absolute=False, county="Solano", ref_df=idx_ref
        )
        self.assertEqual(len(res), 1)

    def test_resolve_absolute_postmiles_numeric_and_prefixed(self):
        b, e = resolve_absolute_postmiles("5", "Shasta", "39", "41.2", ref_df=self.mock_ref)
        self.assertAlmostEqual(b, 700.0, places=1)
        self.assertAlmostEqual(e, 702.2, places=1)

        b, e = resolve_absolute_postmiles(138, "Los Angeles", "R55.0", "56.3", ref_df=self.mock_ref)
        self.assertAlmostEqual(b, 48.5, places=1)
        self.assertAlmostEqual(e, 49.8, places=1)

    def test_builder_resolves_missing_absolute_postmiles(self):
        proj = ProjectRecord(
            project_id="200000288", dist="2", county="Shasta", state="CA", route="5", direction="SB", begin_pm="39", end_pm="41.2"
        )
        builder = ProjectPeMSQueryBuilder(proj, ref_df=self.mock_ref)
        b, e = builder.resolve_absolute_postmiles()
        self.assertAlmostEqual(b, 700.0, places=1)
        self.assertAlmostEqual(e, 702.2, places=1)
        self.assertAlmostEqual(proj.abs_begin_pm, 700.0, places=1)
        self.assertAlmostEqual(proj.abs_end_pm, 702.2, places=1)


class TestCoordinateAndPostmileSearch(unittest.TestCase):
    """Tests for coordinate parsing, haversine calculation, and offline corridor searches."""

    def test_parse_coordinate_string_variants(self):
        c = parse_coordinate_string("38.186164, -122.601642")
        self.assertAlmostEqual(c[0], 38.186164)
        self.assertAlmostEqual(c[1], -122.601642)

        c2 = parse_coordinate_string("(38.186164, -122.601642)")
        self.assertAlmostEqual(c2[0], 38.186164)
        self.assertAlmostEqual(c2[1], -122.601642)

        self.assertEqual(parse_coordinate_string((38.186164, -122.601642)), (38.186164, -122.601642))

        c_dms = parse_coordinate_string("38°11'10.19\"N 122°36'05.91\"W")
        self.assertAlmostEqual(c_dms[0], 38.18616388, places=4)
        self.assertAlmostEqual(c_dms[1], -122.60164166, places=4)

        c_dms2 = parse_coordinate_string('38°11"10.19"N 122°36"05.91"W')
        self.assertAlmostEqual(c_dms2[0], 38.18616388, places=4)
        self.assertAlmostEqual(c_dms2[1], -122.60164166, places=4)

        self.assertIsNone(parse_coordinate_string("invalid string"))
        self.assertIsNone(parse_coordinate_string(""))
        self.assertIsNone(parse_coordinate_string(12345))

    def test_haversine_distance_miles(self):
        self.assertAlmostEqual(haversine_distance_miles(38.0, -122.0, 38.0, -122.0), 0.0)
        self.assertAlmostEqual(haversine_distance_miles(38.0, -122.0, 39.0, -122.0), 69.05, delta=0.5)

    @patch("pems.corridor.fetch_hpms_corridor_summary", return_value=None)
    def test_find_stations_by_coordinate_range_mock(self, _mock_hpms):
        mock_ref = MagicMock()
        mock_ref.raw_df = pd.DataFrame([
            {"Route": "101", "Direction": "N", "CA PM": "0.0", "Abs PM": 10.0, "Latitude": 38.0, "Longitude": -122.0, "County": "SON", "District": "4"},
            {"Route": "101", "Direction": "N", "CA PM": "2.0", "Abs PM": 12.0, "Latitude": 38.03, "Longitude": -122.02, "County": "SON", "District": "4"},
            {"Route": "101", "Direction": "N", "CA PM": "4.0", "Abs PM": 14.0, "Latitude": 38.06, "Longitude": -122.04, "County": "SON", "District": "4"},
            {"Route": "80", "Direction": "E", "CA PM": "1.0", "Abs PM": 50.0, "Latitude": 38.5, "Longitude": -121.5, "County": "PLA", "District": "3"},
        ])
        st1 = VDSStationRecord(id="101001", fwy="101", dir="N", type="ML", abs_pm=11.0, latitude=38.015, longitude=-122.01, name="Test St 1", state_pm="1.0", district="4", county="SON", city="Petaluma", length=0.5, lanes=3)
        st2 = VDSStationRecord(id="101002", fwy="101", dir="N", type="ML", abs_pm=13.0, latitude=38.045, longitude=-122.03, name="Test St 2", state_pm="3.0", district="4", county="SON", city="Petaluma", length=0.5, lanes=3)
        st_out = VDSStationRecord(id="101003", fwy="101", dir="N", type="ML", abs_pm=25.0, latitude=38.2, longitude=-122.1, name="Outside", state_pm="15.0", district="4", county="SON", city="Santa Rosa", length=0.5, lanes=3)
        mock_meta = MagicMock()
        mock_meta.by_id = {"101001": st1, "101002": st2, "101003": st_out}

        res = find_stations_by_coordinate_range(
            route="101",
            start_coord=(38.0, -122.0),
            end_coord=(38.06, -122.04),
            direction="NB",
            buffer_mi=0.2,
            station_types=("ML",),
            idx_ref=mock_ref,
            vds_meta=mock_meta,
        )
        self.assertNotIn("error", res)
        self.assertEqual(res["route"], "101")
        self.assertEqual(res["direction"], "N")
        self.assertEqual(res["snapped_start"]["abs_pm"], 10.0)
        self.assertEqual(res["snapped_end"]["abs_pm"], 14.0)
        found_ids = [s.id for s in res["stations"]]
        self.assertEqual(set(found_ids), {"101001", "101002"})

    @patch("pems.corridor.fetch_hpms_corridor_summary", return_value=None)
    def test_auto_detect_route_switching_and_direction_fallback(self, _mock_hpms):
        clean_df = pd.DataFrame([
            {"_route": "84", "_dir": "N", "_abs_pm": 5.0, "_lat": 37.48, "_lng": -122.21, "_county": "san mateo", "County": "San Mateo", "District": "4", "CA PM": "5.0", "_ca_num": 5.0},
            {"_route": "4", "_dir": "E", "_abs_pm": 10.0, "_lat": 38.00, "_lng": -122.10, "_county": "contra costa", "County": "Contra Costa", "District": "4", "CA PM": "10.0", "_ca_num": 10.0},
            {"_route": "4", "_dir": "W", "_abs_pm": 15.0, "_lat": 38.01, "_lng": -122.02, "_county": "contra costa", "County": "Contra Costa", "District": "4", "CA PM": "15.0", "_ca_num": 15.0},
        ])
        mock_ref = MagicMock()
        mock_ref.clean_df = clean_df
        mock_ref.lats = clean_df["_lat"]
        mock_ref.lngs = clean_df["_lng"]
        mock_ref.valid_directions_for_route.return_value = {"E", "W"}
        mock_ref.auto_detect_highway_corridor.return_value = ("4", clean_df.iloc[1], clean_df.iloc[2], 0.01, 0.01)
        mock_meta = MagicMock()
        mock_meta.by_id = {}

        # Auto-detect route=None
        res1 = find_stations_by_coordinate_range(route=None, start_coord=(38.00, -122.10), end_coord=(38.01, -122.02), idx_ref=mock_ref, vds_meta=mock_meta)
        self.assertNotIn("error", res1)
        self.assertEqual(res1["route"], "4")
        self.assertTrue(res1["auto_detected_route"])
        self.assertEqual(res1["travel_vector"], "EB")

        # Auto-switch mismatched route=84 -> 4
        res2 = find_stations_by_coordinate_range(route="84", start_coord=(38.00, -122.10), end_coord=(38.01, -122.02), idx_ref=mock_ref, vds_meta=mock_meta)
        self.assertNotIn("error", res2)
        self.assertEqual(res2["route"], "4")
        self.assertIn("Auto-switching corridor search to Route 4", res2.get("route_mismatch_notice", ""))

        # Direction fallback NB -> both on E/W Route 4
        res3 = find_stations_by_coordinate_range(route="4", direction="NB", start_coord=(38.00, -122.10), end_coord=(38.01, -122.02), idx_ref=mock_ref, vds_meta=mock_meta)
        self.assertNotIn("error", res3)
        self.assertIn("Route 4 is an East-West highway", res3.get("dir_mismatch_notice", ""))
        self.assertIsNone(res3["direction"])

    def test_convert_california_and_absolute_postmiles_mock(self):
        err = convert_california_postmile("101", "", "0.21")
        self.assertIn("County must be provided", err.get("error", ""))

        mock_ref = MagicMock()
        mock_ref.raw_df = pd.DataFrame([
            {"Route": "101", "Direction": "N", "CA PM": "0.0", "Abs PM": 468.1, "Latitude": 38.177, "Longitude": -122.595, "County": "Sonoma", "District": "4"},
            {"Route": "101", "Direction": "N", "CA PM": "0.21", "Abs PM": 468.3, "Latitude": 38.179, "Longitude": -122.597, "County": "Sonoma", "District": "4"},
            {"Route": "101", "Direction": "N", "CA PM": "50.56", "Abs PM": 400.2, "Latitude": 37.431, "Longitude": -122.104, "County": "Santa Clara", "District": "4"},
        ])

        res_ca = convert_california_postmile("101", "Sonoma", "0.21", direction="NB", idx_ref=mock_ref)
        self.assertEqual(res_ca["abs_pm"], 468.3)
        self.assertAlmostEqual(res_ca["coord"][0], 38.179)

        res_abs = convert_absolute_postmile("101", 400.2, direction="NB", idx_ref=mock_ref)
        self.assertEqual(res_abs["conversion_type"], "ABS_TO_CA")
        self.assertEqual(res_abs["county"], "Santa Clara")
        self.assertEqual(res_abs["ca_pm"], "50.56")

        report = format_single_postmile_report(res_abs)
        self.assertIn("Absolute PM -> California County PM", report)
        self.assertIn("Santa Clara County PM 50.56", report)

    @patch("pems.corridor.fetch_hpms_corridor_summary", return_value=None)
    def test_find_stations_by_postmile_range_abs_and_ca_mock(self, _mock_hpms):
        mock_ref = MagicMock()
        mock_ref.raw_df = pd.DataFrame([
            {"Route": "101", "Direction": "N", "CA PM": "50.56", "Abs PM": 400.2, "Latitude": 37.431, "Longitude": -122.104, "County": "Santa Clara", "District": "4"},
            {"Route": "101", "Direction": "N", "CA PM": "21.8", "Abs PM": 424.0, "Latitude": 37.652, "Longitude": -122.407, "County": "San Mateo", "District": "4"},
            {"Route": "101", "Direction": "N", "CA PM": "0.0", "Abs PM": 468.1, "Latitude": 38.177, "Longitude": -122.595, "County": "Sonoma", "District": "4"},
            {"Route": "101", "Direction": "N", "CA PM": "4.5", "Abs PM": 472.5, "Latitude": 38.235, "Longitude": -122.618, "County": "Sonoma", "District": "4"},
        ])
        st_sc = VDSStationRecord(id="402380", fwy="101", dir="N", type="ML", abs_pm=402.04, latitude=37.451, longitude=-122.125, name="LOC 78", state_pm="52.4", district="4", county="Santa Clara", city="Palo Alto", length=0.8, lanes=4)
        st_son = VDSStationRecord(id="424573", fwy="101", dir="N", type="ML", abs_pm=468.91, latitude=38.187, longitude=-122.600, name="N of San Antonio", state_pm=".21", district="4", county="Sonoma", city="Petaluma", length=0.8, lanes=3)
        st_far = VDSStationRecord(id="999999", fwy="101", dir="N", type="ML", abs_pm=490.0, latitude=38.5, longitude=-122.8, name="Far", state_pm="20.0", district="4", county="Sonoma", city="Santa Rosa", length=0.5, lanes=3)
        mock_meta = MagicMock()
        mock_meta.by_id = {"402380": st_sc, "424573": st_son, "999999": st_far}

        res_abs = find_stations_by_postmile_range("101", 400.2, 424.0, is_absolute=True, direction="NB", idx_ref=mock_ref, vds_meta=mock_meta)
        self.assertEqual(res_abs["begin_county"], "Santa Clara")
        self.assertEqual(res_abs["end_county"], "San Mateo")
        self.assertEqual([s.id for s in res_abs["stations"]], ["402380"])

        res_ca = find_stations_by_postmile_range("101", "0.0", "4.5", county="Sonoma", direction="NB", buffer_mi=0.1, station_types=("ML",), idx_ref=mock_ref, vds_meta=mock_meta)
        self.assertEqual(res_ca["corridor_range"]["abs_min"], 468.1)
        self.assertEqual(res_ca["corridor_range"]["abs_max"], 472.5)
        self.assertEqual([s.id for s in res_ca["stations"]], ["424573"])


if __name__ == "__main__":
    unittest.main()
