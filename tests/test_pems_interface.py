#!/usr/bin/env python3
"""
Unit and integration tests for pems_interface.py.

Strict verification checklist applied:
  1) One behavior per test/subcase.
  2) Assert what proves the branch (exact substrings, parsed URL parameters, specific exception messages).
  3) No coverage-masking fallbacks or catch-all passes.
  4) Consolidated setup helpers.
  5) Robust expectations (e.g. parse_qs instead of brittle string concatenation).
"""

import unittest
from unittest.mock import MagicMock, patch
import urllib.parse
import os
import glob
import datetime
import pandas as pd
import numpy as np
import folium

import pems_interface


class TestNormalizationHelpers(unittest.TestCase):
    """Tests for direction, route, and postmile normalization."""

    def test_normalize_direction_cardinal_nb(self):
        self.assertEqual(pems_interface.normalize_direction("NB"), "N")

    def test_normalize_direction_cardinal_sb(self):
        self.assertEqual(pems_interface.normalize_direction("SB"), "S")

    def test_normalize_direction_cardinal_eb(self):
        self.assertEqual(pems_interface.normalize_direction("EB"), "E")

    def test_normalize_direction_cardinal_wb(self):
        self.assertEqual(pems_interface.normalize_direction("WB"), "W")

    def test_normalize_direction_full_word(self):
        self.assertEqual(pems_interface.normalize_direction("North"), "N")

    def test_normalize_direction_empty_or_none(self):
        self.assertEqual(pems_interface.normalize_direction(""), "")
        self.assertEqual(pems_interface.normalize_direction(None), "")

    def test_normalize_direction_invalid_input_returns_empty(self):
        self.assertEqual(pems_interface.normalize_direction("FOO"), "")
        self.assertEqual(pems_interface.normalize_direction("123"), "")

    def test_normalize_route_strips_alphabetic_prefixes(self):
        self.assertEqual(pems_interface.normalize_route("SR-80"), "80")
        self.assertEqual(pems_interface.normalize_route("I-5"), "5")
        self.assertEqual(pems_interface.normalize_route("US-101"), "101")

    def test_normalize_route_handles_empty(self):
        self.assertEqual(pems_interface.normalize_route(""), "")
        self.assertEqual(pems_interface.normalize_route(None), "")

    def test_split_ca_pm_plain_numeric(self):
        pfx, num = pems_interface.split_ca_pm("12.4")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(num, 12.4)

    def test_split_ca_pm_with_prefix_letter(self):
        pfx, num = pems_interface.split_ca_pm("R3.80")
        self.assertEqual(pfx, "R")
        self.assertAlmostEqual(num, 3.8)

    def test_split_ca_pm_with_suffix_letter(self):
        pfx, num = pems_interface.split_ca_pm("12.4L")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(num, 12.4)

    def test_split_ca_pm_with_prefix_and_suffix(self):
        pfx, num = pems_interface.split_ca_pm("R12.4A")
        self.assertEqual(pfx, "R")
        self.assertAlmostEqual(num, 12.4)

    def test_split_ca_pm_empty_returns_nan(self):
        pfx, num = pems_interface.split_ca_pm("")
        self.assertEqual(pfx, "")
        self.assertTrue(np.isnan(num))

    def test_split_ca_pm_none_returns_nan(self):
        pfx, num = pems_interface.split_ca_pm(None)
        self.assertEqual(pfx, "")
        self.assertTrue(np.isnan(num))


class TestURLBuilders(unittest.TestCase):
    """Tests proving PeMS URL generation conforms exactly to Caltrans specifications."""

    def test_build_lane_config_url_parameters(self):
        url = pems_interface.build_lane_config_url("80", "E", "2020-01-15", "2024-12-31")
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "pems.dot.ca.gov")

        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs.get("fwy"), ["80"])
        self.assertEqual(qs.get("dir"), ["E"])
        self.assertEqual(qs.get("dnode"), ["Freeway"])
        self.assertEqual(qs.get("content"), ["elv"])
        self.assertEqual(qs.get("tab"), ["config_timeseries"])
        self.assertEqual(qs.get("export"), ["text"])
        self.assertEqual(qs.get("q"), ["lanes"])
        self.assertEqual(qs.get("gn"), ["day"])
        self.assertEqual(qs.get("s_time_id_f"), ["01/15/2020"])
        self.assertEqual(qs.get("e_time_id_f"), ["12/31/2024"])

    def test_build_station_aadt_url_timestamps(self):
        url = pems_interface.build_station_aadt_url("410756", 2021)
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(qs.get("station_id"), ["410756"])
        self.assertEqual(qs.get("dnode"), ["VDS"])
        self.assertEqual(qs.get("content"), ["analysis"])
        self.assertEqual(qs.get("tab"), ["aadt"])
        self.assertEqual(qs.get("export"), ["text"])
        # Jan 1 2021 00:00:00 UTC = 1609459200
        self.assertEqual(qs.get("s_time_id"), ["1609459200"])
        # Dec 31 2021 23:59:59 UTC = 1640995199
        self.assertEqual(qs.get("e_time_id"), ["1640995199"])
        self.assertEqual(qs.get("s_time_id_f"), ["01/2021"])
        self.assertEqual(qs.get("e_time_id_f"), ["12/2021"])

    def test_build_station_flow_url_parameters(self):
        url = pems_interface.build_station_flow_url("410756", 2021, granularity="month")
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(qs.get("station_id"), ["410756"])
        self.assertEqual(qs.get("content"), ["loops"])
        self.assertEqual(qs.get("tab"), ["det_timeseries"])
        self.assertEqual(qs.get("q"), ["flow"])
        self.assertEqual(qs.get("gn"), ["month"])
        self.assertEqual(qs.get("holidays"), ["on"])
        self.assertEqual(qs.get("s_time_id_f"), ["01/01/2021 00:00"])
        self.assertEqual(qs.get("e_time_id_f"), ["12/31/2021 23:59"])

    def test_build_fwy_latlng_url_parameters(self):
        url = pems_interface.build_fwy_latlng_url("80", "EB")
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs.get("fwy"), ["80"])
        self.assertEqual(qs.get("dir"), ["E"])
        self.assertEqual(qs.get("content"), ["fwy_latlngXY"])
        self.assertEqual(qs.get("export"), ["text"])

    def test_build_urls_with_xls_export_format(self):
        """Proof: setting export_format='xls' or 'xlsx' generates export=xls query parameter."""
        # Timeseries / Flow
        url_flow = pems_interface.build_station_flow_url("400001", 2022, export_format="xls")
        qs_flow = urllib.parse.parse_qs(urllib.parse.urlparse(url_flow).query)
        self.assertEqual(qs_flow.get("export"), ["xls"])

        url_flow_xlsx = pems_interface.build_station_flow_url("400001", 2022, export_format="xlsx")
        qs_flow_xlsx = urllib.parse.parse_qs(urllib.parse.urlparse(url_flow_xlsx).query)
        self.assertEqual(qs_flow_xlsx.get("export"), ["xls"])

        # AADT
        url_aadt = pems_interface.build_station_aadt_url("400001", 2022, export_format="xls")
        qs_aadt = urllib.parse.parse_qs(urllib.parse.urlparse(url_aadt).query)
        self.assertEqual(qs_aadt.get("export"), ["xls"])

        # Lane config
        url_lane = pems_interface.build_lane_config_url("80", "E", "2020-01-01", "2020-12-31", export_format="xls")
        qs_lane = urllib.parse.parse_qs(urllib.parse.urlparse(url_lane).query)
        self.assertEqual(qs_lane.get("export"), ["xls"])

        # Freeway geometry
        url_fwy = pems_interface.build_fwy_latlng_url("80", "EB", export_format="xls")
        qs_fwy = urllib.parse.parse_qs(urllib.parse.urlparse(url_fwy).query)
        self.assertEqual(qs_fwy.get("export"), ["xls"])



class TestDataValidationAndParsing(unittest.TestCase):
    """Tests checking discrimination between real PeMS data tables and HTML error pages."""

    def test_file_has_data_table_positive(self):
        valid_table = "Starting Month\tArithmetic Mean\tFlow\n01/01/2021\t1200\t5000\n"
        self.assertTrue(pems_interface.PeMSClient.file_has_data_table(valid_table))

    def test_file_has_data_table_negative_html_error(self):
        html_error = "<html><head><title>500 Internal Server Error</title></head><body>Error</body></html>"
        self.assertFalse(pems_interface.PeMSClient.file_has_data_table(html_error))

    def test_file_has_data_table_html_excel_table(self):
        html_table = "<table><tr><th>Month</th><th>Flow</th></tr><tr><td>01/2022</td><td>5000</td></tr></table>"
        self.assertTrue(pems_interface.PeMSClient.file_has_data_table(html_table))

    def test_file_has_data_table_binary_excel_magic(self):
        biff8_magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20
        self.assertTrue(pems_interface.PeMSClient.file_has_data_table(biff8_magic))
        zip_magic = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 20
        self.assertTrue(pems_interface.PeMSClient.file_has_data_table(zip_magic))

    def test_parse_table_content_skips_preamble(self):
        content_with_preamble = (
            "Caltrans Performance Measurement System\n"
            "Report Generated: 2024-01-01\n"
            "\n"
            "Starting Month\tArithmetic Mean\n"
            "01/01/2021\t145000\n"
            "02/01/2021\t152000\n"
        )
        df = pems_interface.PeMSClient.parse_table_content(content_with_preamble)
        self.assertIsNotNone(df)
        self.assertEqual(list(df.columns), ["Starting Month", "Arithmetic Mean"])
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["Arithmetic Mean"], 145000)

    def test_parse_table_content_empty_on_garbage(self):
        self.assertIsNone(pems_interface.PeMSClient.parse_table_content("No data here"))


class TestClientAuthentication(unittest.TestCase):
    """Tests verifying authentication branch handling."""

    @patch.dict(os.environ, {"PEMS_USERNAME": "", "PEMS_PASSWORD": ""}, clear=True)
    def test_login_missing_credentials_raises_value_error(self):
        client = pems_interface.PeMSClient(username="", password="")
        with self.assertRaisesRegex(ValueError, r"PeMS username and password must be provided"):
            client.login()

    @patch("requests.Session.get")
    @patch("requests.Session.post")
    def test_login_success_branch(self, mock_post, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = '<form action="/login"><input name="username"></form>'

        mock_post.return_value.status_code = 200
        mock_post.return_value.text = '<html><body>Welcome user | <a href="/logout">Logout</a></body></html>'

        client = pems_interface.PeMSClient(username="test_user", password="test_password")
        result = client.login()

        self.assertTrue(result)
        self.assertTrue(client.logged_in)
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args[1]["data"]["username"], "test_user")

    @patch("requests.Session.get")
    @patch("requests.Session.post")
    def test_login_failure_no_logout_link_raises_runtime_error(self, mock_post, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = '<form action="/login"><input name="username"></form>'

        mock_post.return_value.status_code = 200
        mock_post.return_value.text = '<html><body>Invalid username or password</body></html>'

        client = pems_interface.PeMSClient(username="test_user", password="wrong_password")
        with self.assertRaisesRegex(RuntimeError, r"Login failed: no 'Logout' link found in response."):
            client.login()
        self.assertFalse(client.logged_in)

    @patch.object(pems_interface.PeMSClient, "login")
    @patch("requests.Session.get")
    def test_get_url_reauthenticates_when_redirected_to_login(self, mock_get, mock_login):
        client = pems_interface.PeMSClient(username="test_user", password="test_password")
        client.logged_in = True

        # Expired response: contains login form and no Logout link
        expired_resp = MagicMock()
        expired_resp.url = "https://pems.dot.ca.gov/?dnode=Clearinghouse"
        expired_resp.text = '<html><body><form action="/login"><input name="username"></form></body></html>'

        valid_resp = MagicMock()
        valid_resp.url = "https://pems.dot.ca.gov/data.txt"
        valid_resp.text = "Starting Month\tMean\n01/01/2021\t100\n"

        mock_get.side_effect = [expired_resp, valid_resp]

        resp = client.get_url("https://pems.dot.ca.gov/data.txt")
        mock_login.assert_called_once()
        self.assertEqual(resp, valid_resp)


class TestGeometryAndCorridorLookups(unittest.TestCase):
    """Tests verifying geometric postmile matching against reference data."""

    def setUp(self):
        self.mock_ref = pd.DataFrame([
            {"District": "4", "County": "Solano", "CA PM": "11.4", "Abs PM": "38.9", "Latitude": "38.201", "Longitude": "-122.156", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.0", "Abs PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.8", "Abs PM": "40.3", "Latitude": "38.216", "Longitude": "-122.138", "Route": "80", "Direction": "E"},
            {"District": "3", "County": "El Dorado", "CA PM": "R11.7", "Abs PM": "145.5", "Latitude": "38.694", "Longitude": "-120.815", "Route": "49", "Direction": "N"},
        ])

    def test_pm_to_coordinates_absolute_match(self):
        coords = pems_interface.pm_to_coordinates("80", "E", 38.9, is_absolute=True, ref_df=self.mock_ref)
        self.assertIsNotNone(coords)
        self.assertAlmostEqual(coords[0], 38.201, places=3)
        self.assertAlmostEqual(coords[1], -122.156, places=3)

    def test_pm_to_coordinates_ca_pm_with_prefix(self):
        coords = pems_interface.pm_to_coordinates("49", "NB", "R11.7", county="El Dorado", is_absolute=False, ref_df=self.mock_ref)
        self.assertIsNotNone(coords)
        self.assertAlmostEqual(coords[0], 38.694, places=3)
        self.assertAlmostEqual(coords[1], -120.815, places=3)

    def test_pm_to_coordinates_nonexistent_returns_none(self):
        coords = pems_interface.pm_to_coordinates("80", "E", 999.0, is_absolute=True, ref_df=self.mock_ref)
        self.assertIsNone(coords)

    def test_get_corridor_stations_bounds(self):
        df_corr = pems_interface.get_corridor_stations("80", "E", 38.9, 39.5, is_absolute=True, ref_df=self.mock_ref)
        self.assertEqual(len(df_corr), 2)
        self.assertListEqual(list(df_corr["CA PM"]), ["11.4", "12.0"])


class TestMasterRowParsing(unittest.TestCase):
    """Tests parsing of cleaned master CSV rows into structured ProjectRecords."""

    def test_parse_master_row_prefers_iso_dates_and_normalizes_sensors(self):
        raw_row = {
            "Project ID": "418000244",
            "Dist": "4",
            "County": "Solano",
            "State": "CA",
            "Route": "80",
            "Direction": "EB",
            "Begin PM": "11.4",
            "End PM": "12.8",
            "temp_abs_begin": "38.9",
            "temp_abs_end": "40.3",
            "Begin Date (formatted YYYY-MM-DD)": "2021-09-01",
            "Open to Traffic Date (formatted YYYY-MM-DD)": "2022-10-01",
            "Sensor Station 1 ID ": "410756",
            "Sensor Station 2 ID ": "429041",
            "Sensor Station 3 ID ": "0",
            "Sensor Station 4 ID ": "nan"
        }
        rec = pems_interface.parse_master_row(raw_row)
        self.assertEqual(rec.project_id, "418000244")
        self.assertEqual(rec.dist, "4")
        self.assertEqual(rec.county, "Solano")
        self.assertEqual(rec.direction, "EB")
        self.assertEqual(rec.pems_direction, "E")
        self.assertEqual(rec.construction_begin_date, "2021-09-01")
        self.assertEqual(rec.open_to_traffic_date, "2022-10-01")
        self.assertEqual(rec.start_year, 2021)
        self.assertEqual(rec.end_year, 2026)
        self.assertEqual(rec.sensor_stations, ["410756", "429041"])


class TestProjectMapping(unittest.TestCase):
    """Tests verifying Folium interactive map generation for projects."""

    def setUp(self):
        self.mock_ref_df = pd.DataFrame([
            {"District": "4", "County": "Solano", "CA PM": "11.4", "Abs PM": "38.9", "Latitude": "38.201", "Longitude": "-122.156", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.0", "Abs PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.8", "Abs PM": "40.3", "Latitude": "38.216", "Longitude": "-122.138", "Route": "80", "Direction": "E"},
        ])
        self.mock_idx = pems_interface.IndexedGeoReference(self.mock_ref_df)

        self.mock_project = pems_interface.ProjectRecord(
            project_id="418000244",
            dist="4",
            county="Solano",
            state="CA",
            route="80",
            direction="EB",
            begin_pm="11.4",
            end_pm="12.8",
            abs_begin_pm=38.9,
            abs_end_pm=40.3,
            start_coords=(38.201, -122.156),
            end_coords=(38.216, -122.138),
            construction_begin_date="2021-09-01",
            open_to_traffic_date="2022-10-01",
            sensor_stations=["410756", "429041"],
            raw_row={
                "Project Description": "I-80 Managed Lanes expansion",
                "dir_lanes_before": "3",
                "dir_lanes_after": "4"
            }
        )
        self.mock_vds_df = pd.DataFrame([
            {"ID": "410756", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "12.0", "Abs_PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Length": "0.5", "Type": "ML", "Lanes": "4", "Name": "WEST TEXAS ST"},
            {"ID": "429041", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "12.5", "Abs_PM": "40.0", "Latitude": "38.212", "Longitude": "-122.142", "Length": "0.4", "Type": "HV", "Lanes": "1", "Name": "TRAVIS BLVD HOV"},
            {"ID": "410999", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "15.0", "Abs_PM": "42.5", "Latitude": "38.230", "Longitude": "-122.120", "Length": "0.6", "Type": "ML", "Lanes": "4", "Name": "OUT OF BOUNDS DETECTOR"},
        ])
        self.mock_vds_meta = pems_interface.IndexedVDSMetadata(self.mock_vds_df)

    def test_get_project_corridor_geometry_from_reference(self):
        coords = pems_interface.get_project_corridor_geometry(self.mock_project, self.mock_idx)
        self.assertEqual(len(coords), 3)
        self.assertAlmostEqual(coords[0][0], 38.201, places=3)
        self.assertAlmostEqual(coords[-1][0], 38.216, places=3)

    def test_get_project_corridor_geometry_fallback_to_endpoints(self):
        orphan_project = pems_interface.ProjectRecord(
            project_id="999",
            dist="1",
            county="Mendocino",
            state="CA",
            route="999",
            direction="NB",
            begin_pm="1.0",
            end_pm="2.0",
            start_coords=(39.1, -123.1),
            end_coords=(39.2, -123.2),
            sensor_stations=[]
        )
        coords = pems_interface.get_project_corridor_geometry(orphan_project, self.mock_idx)
        self.assertEqual(len(coords), 2)
        self.assertEqual(coords[0], (39.1, -123.1))
        self.assertEqual(coords[1], (39.2, -123.2))

    def test_classify_station_relative_to_project_bounds(self):
        st_in = self.mock_vds_meta.by_id["410756"]
        is_in, note, delta = pems_interface.classify_station_relative_to_project(st_in, self.mock_project)
        self.assertTrue(is_in)
        self.assertEqual(delta, 0.0)
        self.assertIn("PM 38.90 - 40.30", note)

        st_out = self.mock_vds_meta.by_id["410999"]
        is_in_out, note_out, delta_out = pems_interface.classify_station_relative_to_project(st_out, self.mock_project)
        self.assertFalse(is_in_out)
        self.assertAlmostEqual(delta_out, 2.2, places=2)
        self.assertIn("downstream", note_out)

    def test_make_station_tooltip_html(self):
        st_in = self.mock_vds_meta.by_id["410756"]
        tt_html = pems_interface.make_station_tooltip_html(st_in, True, "PM 38.9 - 40.3", True)
        self.assertIn("✓ IN PROJECT BOUNDS", tt_html)
        self.assertIn("Station ID:</strong> 410756", tt_html)
        self.assertIn("Mainline", tt_html)
        self.assertIn("Listed in Master CSV (Verified)", tt_html)

        st_out = self.mock_vds_meta.by_id["410999"]
        tt_oob_html = pems_interface.make_station_tooltip_html(st_out, False, "2.2 mi downstream", False)
        self.assertIn("⚠ OUTSIDE BOUNDS (2.2 mi downstream)", tt_oob_html)
        self.assertIn("Not listed in Master CSV", tt_oob_html)

    def test_make_station_popup_html(self):
        st_in = self.mock_vds_meta.by_id["410756"]
        pop_html = pems_interface.make_station_popup_html(st_in, self.mock_project, True, "In bounds", True)
        self.assertIn("Station 410756 · ML (Mainline)", pop_html)
        self.assertIn("Corridor Context (PID 418000244)", pop_html)
        self.assertIn("PeMS Station Summary", pop_html)
        self.assertIn("AADT Export", pop_html)
        self.assertIn("Flow Export", pop_html)

    def test_build_single_project_map(self):
        with patch.object(pems_interface, "get_indexed_geo_ref", return_value=self.mock_idx):
            m = pems_interface.build_single_project_map(
                "418000244",
                projects=[self.mock_project],
                vds_metadata=self.mock_vds_meta,
                station_buffer_mi=3.0
            )
            self.assertIsNotNone(m)
            html = m.get_root().render()
            self.assertIn("PID 418000244", html)
            self.assertIn("Route 80 EB", html)
            self.assertIn("Solano County", html)
            self.assertIn("Stations: Mainline (ML)", html)
            self.assertIn("Stations: Carpool/HOV (HV)", html)
            self.assertIn("PeMS Ground-Truth Checker", html)
            self.assertIn("pems-inbounds", html)
            self.assertIn("pems-oob", html)
            self.assertIn("pems-in-sheet", html)

    def test_build_single_project_map_no_stations(self):
        with patch.object(pems_interface, "get_indexed_geo_ref", return_value=self.mock_idx):
            m = pems_interface.build_single_project_map(
                "418000244",
                projects=[self.mock_project],
                include_stations=False
            )
            self.assertIsNotNone(m)
            html = m.get_root().render()
            self.assertIn("PID 418000244", html)
            self.assertNotIn("PeMS Ground-Truth Checker", html)
            self.assertNotIn("Stations: Mainline", html)

    def test_build_multi_project_map_layers(self):
        with patch.object(pems_interface, "get_indexed_geo_ref", return_value=self.mock_idx):
            m = pems_interface.build_multi_project_map(
                projects=[self.mock_project],
                include_stations=True,
                vds_metadata=self.mock_vds_meta
            )
            self.assertIsNotNone(m)
            html = m.get_root().render()
            self.assertIn("District 4 (1 projects)", html)
            self.assertIn("PID 418000244", html)
            self.assertIn("Georgia", html)
            self.assertIn("Stations: Mainline (ML)", html)
            self.assertIn("Stations: Carpool/HOV (HV)", html)

    def test_add_standard_basemaps_to_map(self):
        m = folium.Map(tiles=None)
        pems_interface.add_standard_basemaps_to_map(m)
        folium.LayerControl().add_to(m)
        html = m.get_root().render()
        self.assertIn("Esri Light Gray (Default)", html)
        self.assertIn("Esri Street Map", html)
        self.assertIn("USGS Topo", html)
        self.assertIn("server.arcgisonline.com", html)
        self.assertNotIn("basemaps.cartocdn.com", html)

    def test_add_standard_basemaps_with_carto_key(self):
        with patch.dict(os.environ, {"CARTO_API_KEY": "test_carto_key_12345"}):
            m = folium.Map(tiles=None)
            pems_interface.add_standard_basemaps_to_map(m)
            folium.LayerControl().add_to(m)
            html = m.get_root().render()
            self.assertIn("CartoDB Positron (Authenticated)", html)
            self.assertIn("test_carto_key_12345", html)


class TestFormattingAndSensors(unittest.TestCase):
    """Tests verifying float postmile cleanup, compound sensor extraction, and bare-bones serif styling."""

    def test_format_pm_float_precision(self):
        self.assertEqual(pems_interface.format_pm("8.6999999999999993"), "8.7")
        self.assertEqual(pems_interface.format_pm(78.400000000000006), "78.4")
        self.assertEqual(pems_interface.format_pm("R11.400000000000002"), "R11.4")
        self.assertEqual(pems_interface.format_pm("I-10 30.3"), "30.3")
        self.assertEqual(pems_interface.format_pm(None), "")

    def test_split_ca_pm_route_prefix_and_precision(self):
        pfx, val = pems_interface.split_ca_pm("I-10 30.3")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(val, 30.3, places=2)

        pfx, val = pems_interface.split_ca_pm("8.6999999999999993")
        self.assertEqual(pfx, "")
        self.assertAlmostEqual(val, 8.7, places=2)

    def test_parse_master_row_compound_sensors_and_census(self):
        row = {
            "Project ID": "1200020279",
            "Dist": "12",
            "County": "Orange",
            "State": "CA",
            "Route": "5",
            "Direction": "NB",
            "Begin PM": "6.2",
            "End PM": "8.6999999999999993",
            "temp_abs_begin": "78.400000000000006",
            "temp_abs_end": "80.900000000000006",
            "Sensor Station 1 ID ": "1204409",
            "Sensor Station 1 HOV ID": "1210066",
            "Sensor Station 2 ID ": "1204395+1210063 (HOV)\t",
            "Sensor Station 3 ID ": "SHEEP CREEK ROAD [5.764]",
            "area_type": "suburban"
        }
        rec = pems_interface.parse_master_row(row)
        self.assertEqual(rec.begin_pm, "6.2")
        self.assertEqual(rec.end_pm, "8.7")
        self.assertEqual(rec.abs_begin_pm, 78.4)
        self.assertEqual(rec.abs_end_pm, 80.9)
        # Asserts each station is isolated and not concatenated
        self.assertEqual(rec.sensor_stations, ["1204409", "1210066", "1204395", "1210063"])
        self.assertNotIn("12043951210063", rec.sensor_stations)
        self.assertNotIn("5764", rec.sensor_stations)

    def test_popup_and_tooltip_bare_bones_serif(self):
        rec = pems_interface.ProjectRecord(
            project_id="1200020279",
            dist="12",
            county="Orange",
            state="CA",
            route="5",
            direction="NB",
            begin_pm="6.2",
            end_pm="8.6999999999999993",
            abs_begin_pm=78.400000000000006,
            abs_end_pm=80.900000000000006,
            sensor_stations=["1204409", "1204395", "1210063"],
            raw_row={"area_type": "suburban"}
        )
        builder = pems_interface.ProjectPeMSQueryBuilder(rec)
        popup = pems_interface.make_project_popup_html(rec, builder, length_mi=2.5)
        tooltip = pems_interface.make_project_tooltip_html(rec, length_mi=2.5)

        # Basic serif typography check
        self.assertIn("font-family: Georgia, 'Times New Roman', Times, serif", popup)
        self.assertIn("font-family: Georgia, 'Times New Roman', Times, serif", tooltip)

        # Postmile clean formatting check
        self.assertIn("PM 6.2 &rarr; 8.7 (2.50 mi)", popup)
        self.assertIn("78.4 &rarr; 80.9", popup)
        self.assertNotIn("8.6999999999999993", popup)
        self.assertNotIn("8.6999999999999993", tooltip)

        # Classified area type check (not "Unknown")
        self.assertIn("Area: Suburban", popup)
        self.assertNotIn("Area: Unknown", popup)

        # Individual sensor links check
        self.assertIn("station_id=1204409", popup)
        self.assertIn("station_id=1204395", popup)
        self.assertIn("station_id=1210063", popup)


class TestVDSStationMatcherAndComparison(unittest.TestCase):
    """
    Unit tests for VDS detector metadata indexing, corridor matching,
    diagnostic discrepancy reporting, dry-run safety, and apply updates.
    """

    def setUp(self):
        # Create a synthetic VDS metadata table
        self.meta_df = pd.DataFrame([
            {
                "ID": "301001",
                "Fwy": "80",
                "Dir": "E",
                "District": "3",
                "County": "61",
                "City": "Colfax",
                "State_PM": "34.5",
                "Abs_PM": "135.5",
                "Latitude": "39.1",
                "Longitude": "-120.9",
                "Length": "1.0",
                "Type": "ML",
                "Lanes": "3",
                "Name": "Colfax Curve EB"
            },
            {
                "ID": "301002",
                "Fwy": "80",
                "Dir": "E",
                "District": "3",
                "County": "61",
                "City": "Colfax",
                "State_PM": "36.0",
                "Abs_PM": "137.0",
                "Latitude": "39.12",
                "Longitude": "-120.88",
                "Length": "1.0",
                "Type": "ML",
                "Lanes": "3",
                "Name": "Colfax East ML"
            },
            {
                "ID": "301003",
                "Fwy": "80",
                "Dir": "E",
                "District": "3",
                "County": "61",
                "City": "Colfax",
                "State_PM": "37.5",
                "Abs_PM": "138.5",
                "Latitude": "39.14",
                "Longitude": "-120.85",
                "Length": "1.0",
                "Type": "HV",
                "Lanes": "1",
                "Name": "Colfax East HV"
            },
            {
                "ID": "301004",
                "Fwy": "80",
                "Dir": "E",
                "District": "3",
                "County": "61",
                "City": "Colfax",
                "State_PM": "36.5",
                "Abs_PM": "137.5",
                "Latitude": "39.13",
                "Longitude": "-120.86",
                "Length": "0.5",
                "Type": "OR",
                "Lanes": "1",
                "Name": "Colfax On-Ramp"
            },
            {
                "ID": "301005",
                "Fwy": "80",
                "Dir": "W",
                "District": "3",
                "County": "61",
                "City": "Colfax",
                "State_PM": "36.0",
                "Abs_PM": "137.0",
                "Latitude": "39.12",
                "Longitude": "-120.88",
                "Length": "1.0",
                "Type": "ML",
                "Lanes": "3",
                "Name": "Colfax West ML"
            }
        ])
        self.indexed_meta = pems_interface.IndexedVDSMetadata(self.meta_df)

    def test_indexed_vds_metadata_partitioning_and_id_index(self):
        """Proof: Metadata is partitioned by (district, route, dir) and ID index resolves records."""
        sub = self.indexed_meta.index.get(("3", "80", "E"))
        self.assertIsNotNone(sub)
        self.assertEqual(len(sub), 4)

        rec = self.indexed_meta.by_id.get("301003")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.type, "HV")
        self.assertEqual(rec.abs_pm, 138.5)
        self.assertEqual(rec.name, "Colfax East HV")

    def test_find_corridor_stations_abs_pm(self):
        """Proof: Querying by absolute PM retrieves exactly the stations in corridor sorted by PM."""
        found = self.indexed_meta.find_corridor_stations(
            district="3",
            route="80",
            direction="EB",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            buffer_mi=0.1,
            station_types=("ML", "HV")
        )
        self.assertEqual([s.id for s in found], ["301002", "301003"])
        self.assertEqual(found[0].abs_pm, 137.0)
        self.assertEqual(found[1].abs_pm, 138.5)

    def test_find_corridor_stations_county_pm_fallback(self):
        """Proof: When absolute PMs are None, falls back to numeric State_PM matching."""
        found = self.indexed_meta.find_corridor_stations(
            district="3",
            route="80",
            direction="EB",
            begin_pm="34.0",
            end_pm="36.2",
            abs_begin_pm=None,
            abs_end_pm=None,
            buffer_mi=0.1,
            station_types=("ML",)
        )
        self.assertEqual([s.id for s in found], ["301001", "301002"])

    def test_find_corridor_stations_type_filtering(self):
        """Proof: Non-ramp types ('ML', 'HV') exclude on-ramps ('OR') unless explicitly included."""
        non_ramp = self.indexed_meta.find_corridor_stations(
            district="3",
            route="80",
            direction="EB",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            buffer_mi=0.1,
            station_types=("ML", "HV")
        )
        self.assertNotIn("301004", [s.id for s in non_ramp])

        with_ramps = self.indexed_meta.find_corridor_stations(
            district="3",
            route="80",
            direction="EB",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            buffer_mi=0.1,
            station_types=("ML", "HV", "OR")
        )
        self.assertIn("301004", [s.id for s in with_ramps])

    def test_compare_project_stations_exact_match(self):
        """Proof: When spreadsheet contains exactly the corridor stations, status is EXACT_MATCH."""
        proj = pems_interface.ProjectRecord(
            project_id="P_EXACT",
            dist="3",
            county="Placer",
            state="CA",
            route="80",
            direction="EB",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            sensor_stations=["301002", "301003"]
        )
        res = pems_interface.compare_project_stations(proj, vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res.status, "EXACT_MATCH")
        self.assertEqual(res.matched_stations, ["301002", "301003"])
        self.assertEqual(res.missing_in_sheet, [])
        self.assertEqual(res.discrepancies, [])

    def test_compare_project_stations_partial_match(self):
        """Proof: When spreadsheet has subset of active corridor stations, status is PARTIAL_MATCH."""
        proj = pems_interface.ProjectRecord(
            project_id="P_PARTIAL",
            dist="3",
            county="Placer",
            state="CA",
            route="80",
            direction="EB",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            sensor_stations=["301002"]
        )
        res = pems_interface.compare_project_stations(proj, vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res.status, "PARTIAL_MATCH")
        self.assertEqual(res.matched_stations, ["301002"])
        self.assertEqual(res.missing_in_sheet, ["301003"])
        self.assertEqual(res.discrepancies, [])

    def test_compare_project_stations_out_of_range_discrepancy(self):
        """Proof: When spreadsheet station is outside corridor bounds, reports out-of-range reason."""
        proj = pems_interface.ProjectRecord(
            project_id="P_DISC",
            dist="3",
            county="Placer",
            state="CA",
            route="80",
            direction="EB",
            begin_pm="36.0",
            end_pm="39.0",
            abs_begin_pm=136.5,
            abs_end_pm=139.0,
            sensor_stations=["301001"]  # at Abs PM 135.5 (1.0 mi before corridor start)
        )
        res = pems_interface.compare_project_stations(proj, vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res.status, "DISCREPANCY")
        self.assertEqual(len(res.discrepancies), 1)
        d = res.discrepancies[0]
        self.assertEqual(d["station_id"], "301001")
        self.assertTrue(d["found_in_pems"])
        self.assertTrue(any("Out of range" in r and "1.00 mi before" in r for r in d["reasons"]))

    def test_compare_project_stations_opposite_direction_discrepancy(self):
        """Proof: When spreadsheet station is on opposite direction, diagnoses direction mismatch."""
        proj = pems_interface.ProjectRecord(
            project_id="P_DIR_DISC",
            dist="3",
            county="Placer",
            state="CA",
            route="80",
            direction="EB",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            sensor_stations=["301005"]  # 301005 is WB
        )
        res = pems_interface.compare_project_stations(proj, vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res.status, "DISCREPANCY")
        d = res.discrepancies[0]
        self.assertEqual(d["station_id"], "301005")
        self.assertTrue(any("Opposite/Different Direction" in r for r in d["reasons"]))

    def test_compare_project_stations_unknown_sensor_discrepancy(self):
        """Proof: Obsolete or invalid sensor ID not found in metadata is flagged as unknown."""
        proj = pems_interface.ProjectRecord(
            project_id="P_UNKNOWN",
            dist="3",
            county="Placer",
            state="CA",
            route="80",
            direction="EB",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            sensor_stations=["9999999"]
        )
        res = pems_interface.compare_project_stations(proj, vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res.status, "DISCREPANCY")
        d = res.discrepancies[0]
        self.assertEqual(d["station_id"], "9999999")
        self.assertFalse(d["found_in_pems"])
        self.assertIn("Invalid or obsolete PeMS VDS ID", d["reasons"][0])

    def test_dry_run_pipeline_leaves_csv_untouched(self):
        """Proof: Dry run mode guarantees the input CSV file is untouched."""
        import tempfile
        with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as tf:
            tf.write("Project ID,Dist,County,Route,Direction,Begin PM,End PM,temp_abs_begin,temp_abs_end\n")
            tf.write("1001,3,Placer,80,EB,35.0,39.0,136.0,139.0\n")
            temp_path = tf.name

        try:
            mtime_before = os.path.getmtime(temp_path)
            with open(temp_path, "r", encoding="utf-8") as f:
                content_before = f.read()

            with patch("pems_interface.load_vds_metadata", return_value=self.indexed_meta):
                pems_interface.run_station_comparison_pipeline(
                    master_csv_path=temp_path,
                    dry_run=True,
                    apply_updates=False,
                    verbose=False
                )

            mtime_after = os.path.getmtime(temp_path)
            with open(temp_path, "r", encoding="utf-8") as f:
                content_after = f.read()
            self.assertEqual(mtime_before, mtime_after)
            self.assertEqual(content_before, content_after)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_apply_pipeline_creates_backup_and_updates_csv(self):
        """Proof: Apply mode creates a timestamped safety backup and populates sensor columns."""
        import tempfile
        header = (
            "Project ID,Dist,County,Route,Direction,Begin PM,End PM,temp_abs_begin,temp_abs_end,"
            "Number of Active Non-Ramp Sensors,Sensor Station 1 ID ,Sensor Station 1 HOV ID,"
            "Sensor Station 1 Name,Sensor Station 1 Type,Sensor Station 1: number of lanes after,"
            "Sensor Station 2 ID ,Sensor Station 2: number of lanes after\n"
        )
        row = "1001,3,Placer,80,EB,35.0,39.0,136.0,139.0,,,,,,,,\n"
        with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as tf:
            tf.write(header + row)
            temp_path = tf.name

        try:
            with patch("pems_interface.load_vds_metadata", return_value=self.indexed_meta):
                pems_interface.run_station_comparison_pipeline(
                    master_csv_path=temp_path,
                    dry_run=False,
                    apply_updates=True,
                    verbose=False
                )

            # Check backup file exists
            backups = glob.glob(f"{temp_path}.bak_*")
            self.assertEqual(len(backups), 1)

            # Check updated CSV
            df_updated = pd.read_csv(temp_path, dtype=str)
            self.assertEqual(df_updated.at[0, "Number of Active Non-Ramp Sensors"], "2")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 ID "], "301002")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 Type"], "ML")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 Name"], "Colfax East ML")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 HOV ID"], "301003")
            # Station 2 ID should NOT be populated with the HOV station (proper ML/HOV separation)
            self.assertTrue(pd.isna(df_updated.at[0, "Sensor Station 2 ID "]) or df_updated.at[0, "Sensor Station 2 ID "] == "")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            for b in glob.glob(f"{temp_path}.bak_*"):
                os.remove(b)


class TestRefactoringAndIntegrityFixes(unittest.TestCase):
    """Specific unit tests verifying the data integrity and architectural refactorings."""

    def test_county_isolation_in_vds_matching(self):
        """Proof: Passing county isolates stations when State Postmiles reset across county lines."""
        meta_df = pd.DataFrame([
            {
                "ID": "400101",
                "Fwy": "101",
                "Dir": "N",
                "District": "4",
                "County": "San Mateo",
                "City": "San Mateo",
                "State_PM": "10.0",
                "Abs_PM": None,
                "Latitude": "37.56",
                "Longitude": "-122.32",
                "Length": "1.0",
                "Type": "ML",
                "Lanes": "4",
                "Name": "US-101 SM 10.0"
            },
            {
                "ID": "400102",
                "Fwy": "101",
                "Dir": "N",
                "District": "4",
                "County": "Santa Clara",
                "City": "San Jose",
                "State_PM": "10.0",
                "Abs_PM": None,
                "Latitude": "37.33",
                "Longitude": "-121.88",
                "Length": "1.0",
                "Type": "ML",
                "Lanes": "4",
                "Name": "US-101 SCL 10.0"
            }
        ])
        vds_meta = pems_interface.IndexedVDSMetadata(meta_df)

        # Match with county="San Mateo"
        found_sm = vds_meta.find_corridor_stations(
            district="4",
            route="101",
            direction="N",
            begin_pm="9.0",
            end_pm="11.0",
            abs_begin_pm=None,
            abs_end_pm=None,
            county="San Mateo"
        )
        self.assertEqual([s.id for s in found_sm], ["400101"])

        # Match with county="Santa Clara"
        found_scl = vds_meta.find_corridor_stations(
            district="4",
            route="101",
            direction="N",
            begin_pm="9.0",
            end_pm="11.0",
            abs_begin_pm=None,
            abs_end_pm=None,
            county="Santa Clara"
        )
        self.assertEqual([s.id for s in found_scl], ["400102"])

    def test_normalize_route_float_values(self):
        """Proof: Float string routes like '5.0' are normalized to '5' without losing digits."""
        self.assertEqual(pems_interface.normalize_route("5.0"), "5")
        self.assertEqual(pems_interface.normalize_route("80.00"), "80")
        self.assertEqual(pems_interface.normalize_route("SR-101"), "101")

    def test_format_pm_prefixes_and_negatives(self):
        """Proof: format_pm strips county/route prefixes while preserving letters and negatives."""
        self.assertEqual(pems_interface.format_pm("04-ALA-80 R11.4"), "R11.4")
        self.assertEqual(pems_interface.format_pm("ALA 11.4"), "11.4")
        self.assertEqual(pems_interface.format_pm("-0.500"), "-0.5")
        self.assertEqual(pems_interface.format_pm("nan"), "")
        self.assertEqual(pems_interface.format_pm(None), "")

    def test_get_corridor_stations_with_string_prefixed_pms(self):
        """Proof: get_corridor_stations parses string-prefixed postmiles without raising ValueError."""
        raw_df = pd.DataFrame([
            {
                "Route": "80",
                "Direction": "E",
                "Abs PM": "10.0",
                "CA PM": "R5.0",
                "County": "Solano",
                "Latitude": "38.25",
                "Longitude": "-122.05"
            }
        ])
        idx_ref = pems_interface.IndexedGeoReference(raw_df)
        res = pems_interface.get_corridor_stations(
            route="80",
            direction="E",
            begin_pm="R4.0",
            end_pm="R6.0",
            is_absolute=False,
            county="Solano",
            ref_df=idx_ref
        )
        self.assertEqual(len(res), 1)

    def test_apply_station_updates_clears_stale_when_zero_stations(self):
        """Proof: When a project corridor has 0 detectors, existing stale sensors are cleared."""
        df = pd.DataFrame([
            {
                "Project ID": "P999",
                "Direction": "NB",
                "Number of Active Non-Ramp Sensors": "2",
                "Sensor Station 1 ID ": "OLD_1",
                "Sensor Station 1 HOV ID": "OLD_HOV",
                "Sensor Station 1 Name": "Old Station",
                "Sensor Station 1 Type": "ML",
                "Sensor Station 1: number of lanes after": "3",
                "Sensor Station 2 ID ": "OLD_2",
                "Sensor Station 2: number of lanes after": "3"
            }
        ])
        proj = pems_interface.ProjectRecord(
            project_id="P999", dist="4", county="Marin", state="CA", route="101", direction="NB",
            begin_pm="1.0", end_pm="2.0", abs_begin_pm=10.0, abs_end_pm=11.0, sensor_stations=["OLD_1", "OLD_2"]
        )
        comp = pems_interface.StationComparisonResult(
            project_id="P999",
            route="101",
            direction="NB",
            district="4",
            county="Marin",
            begin_pm="1.0",
            end_pm="2.0",
            abs_begin_pm=10.0,
            abs_end_pm=11.0,
            existing_stations=["OLD_1", "OLD_2"],
            found_stations=[],
            matched_stations=[],
            missing_in_sheet=[],
            discrepancies=[],
            status="UNINSTRUMENTED",
            sheet_sensor_count=2,
            found_sensor_count=0
        )
        updated = pems_interface.apply_station_updates_to_dataframe(df, [comp])
        self.assertEqual(updated.at[0, "Number of Active Non-Ramp Sensors"], "0")
        self.assertEqual(updated.at[0, "Sensor Station 1 ID "], "")
        self.assertEqual(updated.at[0, "Sensor Station 1 HOV ID"], "")
        self.assertEqual(updated.at[0, "Sensor Station 2 ID "], "")

    def test_markdown_export_without_tabulate(self):
        """Proof: Markdown report export succeeds even when the tabulate library is not installed."""
        proj = pems_interface.ProjectRecord(
            project_id="P_MD", dist="3", county="Placer", state="CA", route="80", direction="EB",
            begin_pm="35.0", end_pm="39.0", abs_begin_pm=136.0, abs_end_pm=139.0, sensor_stations=[]
        )
        comp = pems_interface.StationComparisonResult(
            project_id="P_MD",
            route="80",
            direction="EB",
            district="3",
            county="Placer",
            begin_pm="35.0",
            end_pm="39.0",
            abs_begin_pm=136.0,
            abs_end_pm=139.0,
            existing_stations=[],
            found_stations=[],
            matched_stations=[],
            missing_in_sheet=[],
            discrepancies=[],
            status="SHEET_EMPTY",
            sheet_sensor_count=0,
            found_sensor_count=0
        )
        import tempfile
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as tf:
            md_path = tf.name

        try:
            with patch.dict("sys.modules", {"tabulate": None}):
                pems_interface.run_station_comparison_pipeline(
                    projects=[proj],
                    output_report_path=md_path,
                    dry_run=True,
                    verbose=False
                )
            self.assertTrue(os.path.exists(md_path))
            with open(md_path, "r") as f:
                content = f.read()
            self.assertIn("# Caltrans PeMS Station Comparison Report", content)
            self.assertIn("P_MD", content)
        finally:
            if os.path.exists(md_path):
                os.remove(md_path)


class TestBatchDownloadEngine(unittest.TestCase):
    """Tests proving the PeMS detector batch download functionality."""

    def test_build_station_timeseries_url_quantities_and_granularities(self):
        """Proof: build_station_timeseries_url correctly encodes quantity and granularity."""
        url = pems_interface.build_station_timeseries_url("400001", 2022, 2023, quantity="speed", granularity="hour")
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(qs.get("station_id"), ["400001"])
        self.assertEqual(qs.get("q"), ["speed"])
        self.assertEqual(qs.get("gn"), ["hour"])
        self.assertEqual(qs.get("content"), ["loops"])
        self.assertEqual(qs.get("tab"), ["det_timeseries"])

    def test_download_station_data_validates_inputs(self):
        """Proof: download_station_data raises ValueError on invalid inputs."""
        with self.assertRaises(ValueError):
            pems_interface.download_station_data("", data_type="flow")
        with self.assertRaises(ValueError):
            pems_interface.download_station_data("400001", data_type="invalid_type")
        with self.assertRaises(ValueError):
            pems_interface.download_station_data("400001", data_type="flow", granularity="invalid_gran")
        with self.assertRaises(ValueError):
            pems_interface.download_station_data("400001", start_year=2025, end_year=2020)

    def test_download_station_data_skips_empty_years_and_consolidates(self):
        """Proof: download_station_data downloads active years and combines them into one master file."""
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp(prefix="pems_test_dl_")

        # Mock PeMSClient
        mock_client = MagicMock()
        mock_client.logged_in = True
        mock_client.file_has_data_table.side_effect = lambda c: bool(c and "Month" in str(c))

        # Year 2021: No data (header only)
        # Year 2022: 2 records
        # Year 2023: 2 records
        def mock_get_content(url):
            if "2021" in url:
                return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n"
            elif "2022" in url:
                return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n01/01/2022\t1000\t1440\t95.0\n02/01/2022\t1100\t1440\t96.0\n"
            elif "2023" in url:
                return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n01/01/2023\t1200\t1440\t97.0\n02/01/2023\t1300\t1440\t98.0\n"
            return None

        mock_client.get_text_content.side_effect = mock_get_content

        try:
            res = pems_interface.download_station_data(
                station_id="400001",
                data_type="flow",
                granularity="month",
                start_year=2021,
                end_year=2023,
                output_dir=temp_dir,
                client=mock_client,
                combine=True,
                pause_sec=0.0,
                verbose=False
            )

            self.assertEqual(res["station_id"], "400001")
            self.assertEqual(res["years_downloaded"], [2022, 2023])
            self.assertEqual(res["years_no_data"], [2021])
            self.assertEqual(res["total_records"], 4)

            # Check raw year files exist
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "station_400001_2022_flow_month.txt")))
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "station_400001_2023_flow_month.txt")))
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "station_400001_2021_flow_month.txt")))

            # Check consolidated CSV exists and has 4 rows + header
            comb_file = res["combined_file"]
            self.assertIsNotNone(comb_file)
            self.assertTrue(os.path.exists(comb_file))
            df_comb = pd.read_csv(comb_file)
            self.assertEqual(len(df_comb), 4)
            self.assertIn("Station_ID", df_comb.columns)
            self.assertIn("Month", df_comb.columns)
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    def test_download_station_data_with_xls_export_format(self):
        """Proof: download_station_data with export_format='xls' queries export=xls and saves .xls files."""
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp(prefix="pems_test_dl_xls_")

        mock_client = MagicMock()
        mock_client.logged_in = True
        mock_client.file_has_data_table.side_effect = lambda c: bool(c and "Month" in str(c))

        called_urls = []

        def mock_get_content(url):
            called_urls.append(url)
            return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n01/01/2022\t1000\t1440\t95.0\n"

        mock_client.get_text_content.side_effect = mock_get_content

        try:
            res = pems_interface.download_station_data(
                station_id="400001",
                data_type="flow",
                granularity="month",
                start_year=2022,
                end_year=2022,
                output_dir=temp_dir,
                client=mock_client,
                combine=True,
                pause_sec=0.0,
                verbose=False,
                export_format="xls"
            )

            self.assertEqual(res["export_format"], "xls")
            self.assertEqual(res["years_downloaded"], [2022])
            self.assertTrue(any("export=xls" in u for u in called_urls))

            raw_file = os.path.join(temp_dir, "station_400001_2022_flow_month.xls")
            self.assertTrue(os.path.exists(raw_file), f"Expected {raw_file} to exist")
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)


class TestProjectImagery(unittest.TestCase):
    def test_parse_project_date(self):
        # Full ISO string
        self.assertEqual(pems_interface._parse_project_date("2020-06-01"), datetime.date(2020, 6, 1))
        # Year only
        self.assertEqual(pems_interface._parse_project_date("2019", 1, 1), datetime.date(2019, 1, 1))
        self.assertEqual(pems_interface._parse_project_date("2019", 12, 31), datetime.date(2019, 12, 31))
        # Slash format
        self.assertEqual(pems_interface._parse_project_date("04/15/2018"), datetime.date(2018, 4, 15))
        # Empty / None
        self.assertIsNone(pems_interface._parse_project_date(None))
        self.assertIsNone(pems_interface._parse_project_date(""))
        self.assertIsNone(pems_interface._parse_project_date("nan"))

    def test_get_project_imagery_invalid_pid(self):
        with self.assertRaises(ValueError):
            pems_interface.get_project_imagery("9999999999999_invalid")

    @unittest.mock.patch("wayback_imagery.core.generate_image")
    def test_get_project_imagery_mocked(self, mock_gen):
        mock_gen.return_value = {
            "path": "/mock/path.png",
            "capture_date": "2018-09-06",
            "release_date": "2019-06-26",
            "zoom": 17,
        }
        res = pems_interface.get_project_imagery("313000020", period="both", verbose=False)
        self.assertIn("before", res["imagery"])
        self.assertIn("after", res["imagery"])
        self.assertEqual(res["project_id"], "313000020")
        self.assertEqual(mock_gen.call_count, 2)

    def test_slice_project_corridor(self):
        projects = pems_interface.load_master_csv()
        p = next(p for p in projects if p.project_id == "313000020")
        slices = pems_interface.slice_project_corridor(p, slice_mi=0.4)
        self.assertGreaterEqual(len(slices), 3)
        self.assertEqual(slices[0]["index"], 1)
        self.assertIn("start_coords", slices[0])
        self.assertIn("end_coords", slices[0])
        self.assertGreater(slices[0]["length_mi"], 0)


class TestCLIAndProjectDescription(unittest.TestCase):
    """Tests verifying the streamlined CLI parsing and comprehensive project description display."""

    def test_parse_master_row_extracts_project_description(self):
        row = {
            "Project ID": "123456",
            "Dist": "4",
            "Route": "101",
            "Direction": "NB",
            "Begin PM": "1.0",
            "End PM": "2.0",
            "Project Description": "Widen freeway from 4 to 6 lanes with HOV facility."
        }
        rec = pems_interface.parse_master_row(row)
        self.assertEqual(rec.project_description, "Widen freeway from 4 to 6 lanes with HOV facility.")

    def test_parse_master_row_handles_missing_description(self):
        for val in ["", "nan", "None", None]:
            row = {
                "Project ID": "123456",
                "Dist": "4",
                "Route": "101",
                "Direction": "NB",
                "Begin PM": "1.0",
                "End PM": "2.0",
                "Project Description": val
            }
            rec = pems_interface.parse_master_row(row)
            self.assertEqual(rec.project_description, "")

    def test_parser_target_project_options(self):
        parser = pems_interface.build_parser()

        # -p PID
        args = parser.parse_args(["-p", "400000680"])
        self.assertEqual(args.project, "400000680")
        self.assertFalse(args.slices)
        self.assertFalse(args.imagery)
        self.assertFalse(args.map)

        # --project PID
        args = parser.parse_args(["--project", "400000680"])
        self.assertEqual(args.project, "400000680")

        # --inspect PID (backward compatibility)
        args = parser.parse_args(["--inspect", "400000680"])
        self.assertEqual(args.project, "400000680")

    def test_parser_slices_options(self):
        parser = pems_interface.build_parser()

        # -p PID --slices
        args = parser.parse_args(["-p", "400000680", "--slices"])
        self.assertEqual(args.project, "400000680")
        self.assertTrue(args.slices)

        # --slices PID
        args = parser.parse_args(["--slices", "400000680"])
        self.assertEqual(args.slices, "400000680")

        # --corridor-slices PID (backward compatibility)
        args = parser.parse_args(["--corridor-slices", "400000680"])
        self.assertEqual(args.slices, "400000680")

    def test_parser_imagery_options(self):
        parser = pems_interface.build_parser()

        # -p PID --imagery
        args = parser.parse_args(["-p", "400000680", "--imagery"])
        self.assertEqual(args.project, "400000680")
        self.assertTrue(args.imagery)

        # --imagery PID
        args = parser.parse_args(["--imagery", "400000680"])
        self.assertEqual(args.imagery, "400000680")

        # aliases
        args = parser.parse_args(["--imagery", "400000680", "--period", "before", "--date", "2018-05-01", "--zoom", "17", "--margin", "200"])
        self.assertEqual(args.imagery_period, "before")
        self.assertEqual(args.imagery_date, "2018-05-01")
        self.assertEqual(args.imagery_zoom, 17)
        self.assertEqual(args.imagery_margin, 200.0)

    def test_parser_download_options(self):
        parser = pems_interface.build_parser()

        # -p PID --download
        args = parser.parse_args(["-p", "400000680", "--download"])
        self.assertEqual(args.project, "400000680")
        self.assertTrue(args.download_project)

        # --download PID
        args = parser.parse_args(["--download", "400000680"])
        self.assertEqual(args.download_project, "400000680")

        # --download-project PID (backward compatibility)
        args = parser.parse_args(["--download-project", "400000680"])
        self.assertEqual(args.download_project, "400000680")

        # Station download aliases
        args = parser.parse_args(["-s", "404534", "--format", "xlsx"])
        self.assertEqual(args.download_station, "404534")
        self.assertEqual(args.export_format, "xlsx")

        args = parser.parse_args(["--station", "404534"])
        self.assertEqual(args.download_station, "404534")

    def test_parser_audit_options(self):
        parser = pems_interface.build_parser()

        args = parser.parse_args(["--audit", "--dist", "4"])
        self.assertTrue(args.match_stations)
        self.assertEqual(args.dist, "4")

        args = parser.parse_args(["--match-stations", "-p", "400000680"])
        self.assertTrue(args.match_stations)
        self.assertEqual(args.project, "400000680")

    def test_inspect_output_includes_description(self):
        import io
        from unittest.mock import patch

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            pems_interface.main(["-p", "400000680"])
        out = captured.getvalue()
        self.assertIn("PROJECT INSPECTION: 400000680", out)
        self.assertIn("Description:", out)
        self.assertIn("In San Mateo County", out)

    def test_generate_slices_html_report_includes_description(self):
        proj = pems_interface.ProjectRecord(
            project_id="TEST_DESC_01",
            dist="4",
            county="San Francisco",
            state="CA",
            route="101",
            direction="NB",
            begin_pm="1.0",
            end_pm="2.0",
            construction_begin_date="2018-01-01",
            open_to_traffic_date="2020-01-01",
            project_description="Add aux lane and ramp metering on Route 101."
        )
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            before_meta = {"release": {"release_id": "r1"}, "capture_date": "2017-06-01"}
            after_meta = {"release": {"release_id": "r2"}, "capture_date": "2021-06-01"}
            html_out = pems_interface.generate_slices_html_report(
                project=proj,
                slices_data=[],
                before_meta=before_meta,
                after_meta=after_meta,
                output_dir=tmp_dir
            )
            with open(html_out, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("Project Description:", content)
            self.assertIn("Add aux lane and ramp metering on Route 101.", content)


    def test_lane_counts_accuracy_713000217_and_twltl(self):
        """Proof: Projects with shared median turn lanes (like 713000217 and 800000609) have correct 2 dir / 4 total lanes."""
        csv_path = pems_interface.DEFAULT_MASTER_CSV
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        
        # PID 713000217 (SR 138 Littlerock: widen from 2 to 4 lanes with shared center turn lane)
        r7 = df[df["Project ID"].str.strip() == "713000217"]
        self.assertEqual(len(r7), 2)
        for _, row in r7.iterrows():
            self.assertEqual(row["dir_lanes_before"].split(".")[0], "1")
            self.assertEqual(row["dir_lanes_after"].split(".")[0], "2")
            self.assertEqual(row["total_lanes_before"].split(".")[0], "2")
            self.assertEqual(row["total_lanes_after"].split(".")[0], "4")

        # PID 800000609 (SR 138 Phelan: widen from 2 to 4 lanes with two-way left turn)
        r8 = df[df["Project ID"].str.strip() == "800000609"]
        self.assertEqual(len(r8), 2)
        for _, row in r8.iterrows():
            self.assertEqual(row["dir_lanes_before"].split(".")[0], "1")
            self.assertEqual(row["dir_lanes_after"].split(".")[0], "2")
            self.assertEqual(row["total_lanes_before"].split(".")[0], "2")
            self.assertEqual(row["total_lanes_after"].split(".")[0], "4")

    def test_sensor_activation_dates_formatted_properly(self):
        """Proof: Sensor activation dates are formatted ISO YYYY-MM-DD and not unformatted Excel integers like 44748."""
        csv_path = pems_interface.DEFAULT_MASTER_CSV
        df = pd.read_csv(csv_path, dtype=str).fillna("")

        # PID 300020420 (Excel date serial 44748 -> 2022-07-06)
        r3 = df[df["Project ID"].str.strip() == "300020420"]
        self.assertFalse(r3.empty)
        act_date = r3.iloc[0]["Sensor 1 Activation Date for New Lane"]
        self.assertEqual(act_date, "2022-07-06")

        # Verify no 5-digit Excel serial numbers remain in Sensor 1 Activation Date column
        for idx, row in df.iterrows():
            val = row.get("Sensor 1 Activation Date for New Lane", "").strip()
            if val and val.isdigit() and len(val) == 5:
                self.fail(f"Found unformatted Excel serial date '{val}' at row {idx} for PID {row.get('Project ID')}")

    def test_apply_station_updates_formats_dates_and_avoids_misplacement(self):
        """Proof: apply_station_updates_to_dataframe formats Excel date serials and does not misplace values."""
        df_dummy = pd.DataFrame([{
            "Project ID": "300020420",
            "Direction": "EB",
            "Number of Active Non-Ramp Sensors": "2",
            "Sensor Station 1 ID ": "314000",
            "Sensor Station 1: number of lanes after": "2",
            "Sensor Station 1 Type": "VDS",
            "Sensor Station 1 Name": "Old Station",
            "Sensor 1 Activation Date for New Lane": "44748",
            "Sensor Station 2 ID ": "314001",
            "Sensor Station 2: number of lanes after": "2"
        }])

        st_new = pems_interface.VDSStationRecord(
            id="3047131", fwy="80", dir="EB", district="3", county="Placer", city="Colfax",
            state_pm="37.0", abs_pm=138.0, latitude=39.13, longitude=-120.91, length=0.5,
            type="ML", lanes=2, name="Secret Town"
        )
        comp = pems_interface.StationComparisonResult(
            project_id="300020420", route="80", direction="EB", district="3", county="Placer",
            begin_pm="35.1", end_pm="38.3", abs_begin_pm=136.6, abs_end_pm=139.8,
            existing_stations=["314000", "314001"], found_stations=[st_new], matched_stations=[],
            missing_in_sheet=[], discrepancies=[], status="EXACT_MATCH", sheet_sensor_count=2, found_sensor_count=1
        )

        res = pems_interface.apply_station_updates_to_dataframe(df_dummy, [comp])
        self.assertEqual(res.at[0, "Sensor Station 1 ID "], "3047131")
        self.assertEqual(res.at[0, "Sensor 1 Activation Date for New Lane"], "2022-07-06")
        self.assertEqual(res.at[0, "Sensor Station 2 ID "], "")


class TestAbsolutePostmileCalculation(unittest.TestCase):
    """Tests for calculating absolute postmiles from California postmiles in pems_interface."""

    def setUp(self):
        self.mock_ref = pd.DataFrame([
            {"Route": "5", "Direction": "S", "County": "Shasta", "CA PM": "39.0", "Abs PM": "700.0", "Latitude": "40.8688", "Longitude": "-122.3663"},
            {"Route": "5", "Direction": "S", "County": "Shasta", "CA PM": "41.2", "Abs PM": "702.2", "Latitude": "40.8946", "Longitude": "-122.3833"},
            {"Route": "138", "Direction": "W", "County": "Los Angeles", "CA PM": "R55.0", "Abs PM": "48.5", "Latitude": "34.5", "Longitude": "-117.9"},
            {"Route": "138", "Direction": "W", "County": "Los Angeles", "CA PM": "56.3", "Abs PM": "49.8", "Latitude": "34.5", "Longitude": "-117.8"},
        ])

    def test_resolve_absolute_postmiles_numeric_and_prefixed(self):
        # Plain numeric string route and PM
        b, e = pems_interface.resolve_absolute_postmiles("5", "Shasta", "39", "41.2", ref_df=self.mock_ref)
        self.assertAlmostEqual(b, 700.0, places=1)
        self.assertAlmostEqual(e, 702.2, places=1)

        # Prefixed CA PM (e.g. 'R55.0') and integer route
        b, e = pems_interface.resolve_absolute_postmiles(138, "Los Angeles", "R55.0", "56.3", ref_df=self.mock_ref)
        self.assertAlmostEqual(b, 48.5, places=1)
        self.assertAlmostEqual(e, 49.8, places=1)

    def test_builder_resolves_missing_absolute_postmiles(self):
        proj = pems_interface.ProjectRecord(
            project_id="200000288",
            dist="2",
            county="Shasta",
            state="CA",
            route="5",
            direction="SB",
            begin_pm="39",
            end_pm="41.2",
            abs_begin_pm=None,
            abs_end_pm=None
        )
        self.assertIsNone(proj.abs_begin_pm)
        self.assertIsNone(proj.abs_end_pm)

        builder = pems_interface.ProjectPeMSQueryBuilder(proj, ref_df=self.mock_ref)
        b, e = builder.resolve_absolute_postmiles()
        self.assertAlmostEqual(b, 700.0, places=1)
        self.assertAlmostEqual(e, 702.2, places=1)
        self.assertAlmostEqual(proj.abs_begin_pm, 700.0, places=1)
        self.assertAlmostEqual(proj.abs_end_pm, 702.2, places=1)

    def test_project_inspection_cli_calculates_and_prints_absolute_pms(self):
        """Proof: python pems_interface.py -p <PID> calculates absolute PMs from Begin PM and End PM."""
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            pems_interface.main(["-p", "200000288"])
        output = f.getvalue()

        self.assertIn("PROJECT INSPECTION: 200000288", output)
        self.assertIn("Postmiles:          Begin: 39 | End: 41.2", output)
        self.assertIn("Absolute PMs:       Begin: 700.0 | End: 702.2", output)


class TestOpenToTrafficInference(unittest.TestCase):
    """Unit tests for Option 1: Open to Traffic Date inference via PeMS configuration & telemetry."""

    def test_parse_any_date_formats(self):
        # 5-digit Excel serial
        d_excel = pems_interface.parse_any_date("43488")
        self.assertEqual(d_excel, datetime.date(2019, 1, 23))

        # Year only
        d_yr = pems_interface.parse_any_date("2019")
        self.assertEqual(d_yr, datetime.date(2019, 1, 1))

        # YYYY/MM and MM/YYYY
        d_ym = pems_interface.parse_any_date("2022/10")
        self.assertEqual(d_ym, datetime.date(2022, 10, 1))
        d_my = pems_interface.parse_any_date("10/2022")
        self.assertEqual(d_my, datetime.date(2022, 10, 1))

        # Textual date formats
        d_text1 = pems_interface.parse_any_date("April 30th, 2018")
        self.assertEqual(d_text1, datetime.date(2018, 4, 30))
        d_text2 = pems_interface.parse_any_date("December, 2019")
        self.assertEqual(d_text2, datetime.date(2019, 12, 1))
        d_text3 = pems_interface.parse_any_date("May 22 2020")
        self.assertEqual(d_text3, datetime.date(2020, 5, 22))

        # ISO and slash dates
        d_iso = pems_interface.parse_any_date("2020-02-28")
        self.assertEqual(d_iso, datetime.date(2020, 2, 28))
        d_slash = pems_interface.parse_any_date("07/06/2022")
        self.assertEqual(d_slash, datetime.date(2022, 7, 6))

        # Invalid/empty
        self.assertIsNone(pems_interface.parse_any_date(""))
        self.assertIsNone(pems_interface.parse_any_date("nan"))
        self.assertIsNone(pems_interface.parse_any_date(None))

    def test_parse_station_summary_configuration(self):
        sample_html = """
        <html>
        <body>
        <table class="grid">
            <tr>
                <th>Date</th><th>Status</th><th>Name</th><th>Lanes</th><th>CA PM</th><th>Abs PM</th><th>Length</th><th>Lat</th><th>Lng</th>
            </tr>
            <tr>
                <td>02/06/2019</td><td>Active</td><td>Colfax Curve</td><td>12</td><td>34.447</td><td>135.93</td><td>5.00</td><td>39.1143</td><td>-120.9442</td>
            </tr>
            <tr>
                <td>07/06/2022</td><td>Active</td><td>Colfax Curve</td><td>123</td><td>34.447</td><td>135.93</td><td>5.00</td><td>39.1143</td><td>-120.9442</td>
            </tr>
            <tr>
                <td>12/15/2022</td><td>Active</td><td>Colfax Curve</td><td>123</td><td>34.447</td><td>135.93</td><td>4.48</td><td>39.1143</td><td>-120.9442</td>
            </tr>
        </table>
        </body>
        </html>
        """
        recs = pems_interface.parse_station_summary_configuration(sample_html)
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[0].date, datetime.date(2019, 2, 6))
        self.assertEqual(recs[0].lane_count, 2)
        self.assertEqual(recs[0].lanes_str, "12")

        self.assertEqual(recs[1].date, datetime.date(2022, 7, 6))
        self.assertEqual(recs[1].lane_count, 3)
        self.assertEqual(recs[1].lanes_str, "123")

    def test_detect_lane_config_step_changes(self):
        recs = [
            pems_interface.StationConfigRecord(
                date=datetime.date(2018, 1, 1),
                date_str="2018-01-01",
                status="Active",
                name="Test Station",
                lanes_str="12",
                lane_count=2,
                ca_pm="10.0",
                abs_pm="100.0"
            ),
            pems_interface.StationConfigRecord(
                date=datetime.date(2020, 6, 15),
                date_str="2020-06-15",
                status="Active",
                name="Test Station",
                lanes_str="123",
                lane_count=3,
                ca_pm="10.0",
                abs_pm="100.0"
            ),
        ]
        # Detect lane increment
        events = pems_interface.detect_lane_config_step_changes(recs, station_id="12345")
        self.assertEqual(len(events), 2)
        # Event 1: Commissioning / first active
        self.assertEqual(events[0].change_type, "DETECTOR_COMMISSIONED")
        self.assertEqual(events[0].change_date, datetime.date(2018, 1, 1))
        # Event 2: Lane increment
        self.assertEqual(events[1].change_type, "LANE_CONFIG_INCREMENT")
        self.assertEqual(events[1].change_date, datetime.date(2020, 6, 15))
        self.assertEqual(events[1].lanes_before, 2)
        self.assertEqual(events[1].lanes_after, 3)

        # Test min_date filtering: If min_date is 2019-01-01, baseline is 2 lanes and only lane increment is emitted
        events_filtered = pems_interface.detect_lane_config_step_changes(
            recs, station_id="12345", min_date=datetime.date(2019, 1, 1)
        )
        self.assertEqual(len(events_filtered), 1)
        self.assertEqual(events_filtered[0].change_type, "LANE_CONFIG_INCREMENT")
        self.assertEqual(events_filtered[0].change_date, datetime.date(2020, 6, 15))

    def test_parse_daily_flow_telemetry(self):
        sample_tsv = (
            "Day\tFlow (Veh/Day)\t# Lane Points\t% Observed\n"
            "07/04/2022\t10,310.0\t576\t100.0\n"
            "07/05/2022\t13,355.0\t576\t100.0\n"
            "07/06/2022\t16,772.0\t863\t98.8\n"
            "07/07/2022\t18,980.0\t864\t100.0\n"
            "07/08/2022\t24,163.0\t864\t100.0\n"
        )
        flow_records = pems_interface.parse_daily_flow_telemetry(sample_tsv)
        self.assertEqual(len(flow_records), 5)
        self.assertEqual(flow_records[0]["date"], datetime.date(2022, 7, 4))
        self.assertEqual(flow_records[0]["lane_points"], 576)
        self.assertEqual(flow_records[2]["date"], datetime.date(2022, 7, 6))
        self.assertEqual(flow_records[2]["lane_points"], 863)

    def test_detect_flow_telemetry_activation_lane_points_jump(self):
        mock_client = unittest.mock.MagicMock()
        mock_client.get_text_content.return_value = (
            "Day\tFlow (Veh/Day)\t# Lane Points\t% Observed\n"
            "07/01/2022\t10,000.0\t576\t100.0\n"
            "07/02/2022\t10,000.0\t576\t100.0\n"
            "07/03/2022\t10,000.0\t576\t100.0\n"
            "07/04/2022\t10,310.0\t576\t100.0\n"
            "07/05/2022\t13,355.0\t576\t100.0\n"
            "07/06/2022\t16,772.0\t863\t98.8\n"
            "07/07/2022\t18,980.0\t864\t100.0\n"
            "07/08/2022\t24,163.0\t864\t100.0\n"
            "07/09/2022\t25,000.0\t864\t100.0\n"
        )
        res = pems_interface.detect_flow_telemetry_activation(
            client=mock_client,
            station_id="314000",
            target_year=2022,
            candidate_date=datetime.date(2022, 7, 6),
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.activation_date, datetime.date(2022, 7, 6))
        self.assertEqual(res.activation_type, "LANE_POINTS_STEP_CHANGE")
        self.assertEqual(res.lane_points_before, 576)
        self.assertEqual(res.lane_points_after, 863)
        self.assertIn("576 (~2 lanes) to 863 (~3 lanes)", res.evidence)

    def test_infer_project_open_to_traffic_corroborated(self):
        mock_client = unittest.mock.MagicMock()
        mock_client.logged_in = True

        sample_summary_html = """
        <table>
            <tr><th>Date</th><th>Status</th><th>Name</th><th>Lanes</th><th>CA PM</th><th>Abs PM</th></tr>
            <tr><td>02/06/2019</td><td>Active</td><td>Colfax</td><td>12</td><td>34.4</td><td>135.9</td></tr>
            <tr><td>07/06/2022</td><td>Active</td><td>Colfax</td><td>123</td><td>34.4</td><td>135.9</td></tr>
        </table>
        """
        sample_tsv = (
            "Day\tFlow (Veh/Day)\t# Lane Points\t% Observed\n"
            "07/04/2022\t10,000.0\t576\t100.0\n"
            "07/05/2022\t10,000.0\t576\t100.0\n"
            "07/06/2022\t16,000.0\t864\t100.0\n"
            "07/07/2022\t18,000.0\t864\t100.0\n"
            "07/08/2022\t20,000.0\t864\t100.0\n"
        )

        def mock_get_content(url):
            if "content=summary" in url:
                return sample_summary_html
            elif "tab=det_timeseries" in url:
                return sample_tsv
            return ""

        mock_client.get_text_content.side_effect = mock_get_content

        proj = pems_interface.ProjectRecord(
            project_id="300020420",
            dist="3",
            county="Placer",
            state="CA",
            route="80",
            direction="EB",
            begin_pm="34.4",
            end_pm="39.4",
            construction_begin_date="2019-01-01",
            open_to_traffic_date="2019-01-23",
            sensor_stations=["314000"],
        )

        inf = pems_interface.infer_project_open_to_traffic(
            proj,
            client=mock_client,
            check_telemetry=True,
            verbose=False,
        )

        self.assertEqual(inf.project_id, "300020420")
        self.assertEqual(inf.inferred_date, "2022-07-06")
        self.assertEqual(inf.inferred_year, 2022)
        self.assertEqual(inf.confidence, "HIGH")
        self.assertEqual(inf.method, "CORROBORATED_TELEMETRY")
        self.assertEqual(inf.station_id, "314000")
        self.assertIn("incremented from 2 to 3 lanes on 2022-07-06", inf.evidence)

    def test_apply_open_date_updates_to_dataframe(self):
        raw_data = {
            "Project ID": ["300020420", "9990001"],
            "Direction": ["EB", "NB"],
            "Open to Traffic Date": ["43488", None],
            "Open to Traffic Date (formatted YYYY-MM-DD)": ["2019-01-23", None],
            "open_traffic_year": [2019, None],
            "open_traffic_month": [1, None],
            "open_traffic_day": [23, None],
            "Sensor Station 1 ID ": ["314000", "123456"],
            "Sensor 1 Activation Date for New Lane": [None, None],
        }
        df = pd.DataFrame(raw_data)

        inferences = [
            pems_interface.OpenToTrafficInference(
                project_id="300020420",
                route="80",
                direction="EB",
                district="3",
                county="Placer",
                current_open_date="2019-01-23",
                inferred_date="2022-07-06",
                inferred_year=2022,
                confidence="HIGH",
                method="CORROBORATED_TELEMETRY",
                evidence="Increment detected and verified",
                station_id="314000",
            )
        ]

        updated, count = pems_interface.apply_open_date_updates_to_dataframe(df, inferences)
        self.assertEqual(count, 1)
        self.assertEqual(updated.at[0, "Open to Traffic Date"], "2022-07-06")
        self.assertEqual(updated.at[0, "Open to Traffic Date (formatted YYYY-MM-DD)"], "2022-07-06")
        self.assertEqual(str(updated.at[0, "open_traffic_year"]), "2022")
        self.assertEqual(str(updated.at[0, "open_traffic_month"]), "7")
        self.assertEqual(str(updated.at[0, "open_traffic_day"]), "6")
        self.assertEqual(updated.at[0, "Sensor 1 Activation Date for New Lane"], "2022-07-06")

        # Second row unchanged
        self.assertTrue(pd.isna(updated.at[1, "Open to Traffic Date"]))

    def test_apply_open_date_to_master_csv_creates_backup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "test_master.csv")
            df = pd.DataFrame({
                "Project ID": ["300020420"],
                "Direction": ["EB"],
                "Open to Traffic Date": ["43488"],
                "Open to Traffic Date (formatted YYYY-MM-DD)": ["2019-01-23"],
                "open_traffic_year": [2019],
                "open_traffic_month": [1],
                "open_traffic_day": [23],
                "Sensor Station 1 ID ": ["314000"],
                "Sensor 1 Activation Date for New Lane": [None],
            })
            df.to_csv(csv_path, index=False)

            inf = pems_interface.OpenToTrafficInference(
                project_id="300020420",
                route="80",
                direction="EB",
                district="3",
                county="Placer",
                current_open_date="2019-01-23",
                inferred_date="2022-07-06",
                inferred_year=2022,
                confidence="HIGH",
                method="CORROBORATED_TELEMETRY",
                evidence="Verified",
                station_id="314000",
            )

            backup_path, target_path, count = pems_interface.apply_open_date_to_master_csv([inf], master_csv_path=csv_path)

            self.assertTrue(os.path.exists(backup_path))
            self.assertTrue(os.path.exists(target_path))
            self.assertEqual(count, 1)

            # Check updated file
            updated_df = pd.read_csv(target_path)
            self.assertEqual(updated_df.at[0, "Open to Traffic Date"], "2022-07-06")


class TestStationLaneConfiguration(unittest.TestCase):
    """Test suite for PeMS station lane tables, change logs, and TSN roadway metrics inspection."""

    def test_build_station_config_url(self):
        url = pems_interface.build_station_config_url("410756")
        self.assertEqual(url, "https://pems.dot.ca.gov/?report_form=1&dnode=VDS&content=sta_cfg&station_id=410756")

    def test_parse_station_configuration_page(self):
        sample_html = """
        <html>
        <head><title>Caltrans PeMS > Mainline VDS 410756 - EB 12 OC > Change Log</title></head>
        <body>
          <table>
            <tr><th colspan="2">Roadway Information (from TSN)</th></tr>
            <tr><td>Road Width</td><td>48 ft</td></tr>
            <tr><td>Lane Width</td><td>12.0 ft</td></tr>
            <tr><td>Inner Shoulder Width</td><td>8 ft</td></tr>
            <tr><td>Outer Shoulder Width</td><td>10 ft</td></tr>
            <tr><td>Design Speed Limit</td><td>70 mph</td></tr>
            <tr><td>Functional Class</td><td>Principal Arterial W/ C/L Prin Arterial</td></tr>
            <tr><td>Inner Median Type</td><td>Unpaved</td></tr>
          </table>

          <table>
            <tr><th colspan="9">Change Log</th></tr>
            <tr>
              <th>Date</th><th>Status</th><th>Name</th><th>Lanes</th>
              <th>CA PM</th><th>Abs PM</th><th>Length</th><th>Lat</th><th>Lng</th>
            </tr>
            <tr>
              <td>04/09/2016</td><td>Active</td><td>EB 12 OC</td><td>1234</td>
              <td>11.86</td><td>39.33</td><td>0.82</td><td>38.20691</td><td>-122.151326</td>
            </tr>
            <tr>
              <td>05/20/2017</td><td>Active</td><td>EB 12 OC</td><td>12345</td>
              <td>11.86</td><td>39.33</td><td>0.75</td><td>38.20691</td><td>-122.151326</td>
            </tr>
          </table>

          <table>
            <tr><th colspan="2">Construction History</th></tr>
            <tr><th>Start Date</th><th>End Date</th></tr>
            <tr><td>03/14/2017</td><td>01/14/2018</td></tr>
          </table>

          <table>
            <tr><th>Lane</th><th>Slot</th><th>Sensor Tech</th><th>Type</th></tr>
            <tr><td>1</td><td>1</td><td>Dual Loop</td><td>Mainline</td></tr>
            <tr><td>2</td><td>2</td><td>Dual Loop</td><td>Mainline</td></tr>
            <tr><td>3</td><td>3</td><td>Dual Loop</td><td>Mainline</td></tr>
          </table>
        </body>
        </html>
        """
        details = pems_interface.parse_station_configuration_page(sample_html, station_id="410756")
        self.assertEqual(details.station_id, "410756")
        self.assertEqual(details.station_name, "EB 12 OC")
        self.assertEqual(details.station_type, "Mainline VDS")
        self.assertIsNone(details.error)

        # TSN checks
        self.assertEqual(details.tsn_roadway_info.get("Road Width"), "48 ft")
        self.assertEqual(details.tsn_roadway_info.get("Lane Width"), "12.0 ft")
        self.assertEqual(details.tsn_roadway_info.get("Inner Shoulder Width"), "8 ft")
        self.assertEqual(details.tsn_roadway_info.get("Outer Shoulder Width"), "10 ft")
        self.assertEqual(details.tsn_roadway_info.get("Design Speed Limit"), "70 mph")

        # Lane table checks
        self.assertEqual(len(details.lane_table), 3)
        self.assertEqual(details.lane_table[0]["Lane"], "1")
        self.assertEqual(details.lane_table[0]["Sensor Tech"], "Dual Loop")
        self.assertEqual(details.lane_table[0]["Type"], "Mainline")

        # Change log checks
        self.assertEqual(len(details.change_log), 2)
        self.assertEqual(details.change_log[0]["Date"], "04/09/2016")
        self.assertEqual(details.change_log[0]["Lanes"], "1234")
        self.assertEqual(details.change_log[1]["Date"], "05/20/2017")
        self.assertEqual(details.change_log[1]["Lanes"], "12345")

        # Construction history checks
        self.assertEqual(len(details.construction_history), 1)
        self.assertEqual(details.construction_history[0]["Start Date"], "03/14/2017")
        self.assertEqual(details.construction_history[0]["End Date"], "01/14/2018")

    def test_parse_station_configuration_error_page(self):
        err_html = """
        <html>
        <head><title>PeMS Internal Error</title></head>
        <body>
          <h1>Application Halted</h1>
          <p>Missing numeric segment identifier.</p>
        </body>
        </html>
        """
        details = pems_interface.parse_station_configuration_page(err_html, station_id="716177")
        self.assertEqual(details.station_id, "716177")
        self.assertIsNotNone(details.error)
        self.assertIn("PeMS internal error", details.error)

    def test_parse_station_configuration_tsn_not_available_and_id_header(self):
        sample_html = """
        <html>
        <head><title>Caltrans PeMS > Mainline VDS 3411074 - 65NB Galleria Blvd > Change Log</title></head>
        <body>
          <table class="blue_outline_table">
            <tr><td class="blue_outline_caption" colspan="2">Roadway Information (from TSN)</td></tr>
            <tr><td style="font-style: italic">Not Available</td></tr>
          </table>
          <table>
            <tr><th>Lane</th><th>Slot</th><th>ID</th><th>Type</th></tr>
            <tr class="even"><td>1</td><td>1</td><td>34110717</td><td>Mainline</td></tr>
            <tr class="odd"><td>2</td><td>2</td><td>34110718</td><td>Mainline</td></tr>
            <tr class="even"><td>3</td><td>3</td><td>34110719</td><td>Mainline</td></tr>
          </table>
        </body>
        </html>
        """
        details = pems_interface.parse_station_configuration_page(sample_html, station_id="3411074")
        self.assertEqual(details.station_id, "3411074")
        self.assertEqual(details.tsn_roadway_info.get("Status"), "Not Available in PeMS")
        self.assertEqual(len(details.lane_table), 3)
        self.assertEqual(details.lane_table[0]["Lane"], "1")
        self.assertEqual(details.lane_table[0]["ID"], "34110717")
        self.assertEqual(details.lane_table[0]["Type"], "Mainline")

    def test_format_lane_config_report(self):
        details = pems_interface.StationLaneConfigDetails(
            station_id="410756",
            station_name="EB 12 OC",
            district="4",
            route="80",
            direction="E",
            county="Solano",
            station_type="ML",
            ca_pm="11.86",
            abs_pm=39.33,
            latitude=38.20691,
            longitude=-122.151326,
            tsn_roadway_info={"Road Width": "48 ft", "Lane Width": "12.0 ft"},
            lane_table=[{"Lane": "1", "Slot": "1", "Sensor Tech": "Dual Loop", "Type": "Mainline"}],
            change_log=[{"Date": "04/09/2016", "Status": "Active", "Lanes": "12345"}],
            construction_history=[{"Start Date": "03/14/2017", "End Date": "01/14/2018"}],
            url="https://pems.dot.ca.gov/?report_form=1&dnode=VDS&content=sta_cfg&station_id=410756"
        )
        report = pems_interface.format_lane_config_report(details)
        self.assertIn("STATION 410756: EB 12 OC (ML)", report)
        self.assertIn("Road Width:", report)
        self.assertIn("48 ft", report)
        self.assertIn("Dual Loop", report)
        self.assertIn("04/09/2016", report)
        self.assertIn("03/14/2017", report)

    def test_format_lane_config_report_with_hpms(self):
        details = pems_interface.StationLaneConfigDetails(
            station_id="3411074",
            station_name="65NB Galleria Blvd",
            tsn_roadway_info={"Status": "Not Available in PeMS"},
            lane_table=[{"Lane": "1", "Slot": "1", "ID": "34110717", "Type": "Mainline"}]
        )
        hpms_info = {
            "sections_count": 41,
            "predominant_lanes_2024": 6,
            "avg_lanes_2024": 5.93,
            "facility_type": "Two-Way Divided (Freeway/Expressway)",
            "access_control": "Full Access Control (Freeway)",
            "surface_type": "Concrete",
            "functional_system": "Principal Arterial",
            "avg_aadt_2024": 123594,
            "future_aadt": 137007,
            "future_aadt_year": "2044"
        }
        report = pems_interface.format_lane_config_report(details, hpms_summary=hpms_info)
        self.assertIn("HPMS Ground-Truth Roadway Attributes", report)
        self.assertIn("Through Lanes (2024):         6 (Corridor length-weighted avg: 5.93)", report)
        self.assertIn("Two-Way Divided (Freeway/Expressway)", report)
        self.assertIn("Full Access Control (Freeway)", report)
        self.assertIn("123,594 vehicles/day", report)
        self.assertIn("137,007 vehicles/day (Year 2044)", report)
        self.assertIn("Not Available in PeMS for this station", report)

    def test_parser_lane_config_cli_flags(self):
        parser = pems_interface.build_parser()

        # -p PID --lane-config
        args = parser.parse_args(["-p", "400000680", "--lane-config"])
        self.assertEqual(args.project, "400000680")
        self.assertTrue(args.lane_config)

        # --lanes PID
        args = parser.parse_args(["--lanes", "400000680"])
        self.assertEqual(args.lane_config, "400000680")

        # -s STATION_ID --lanes
        args = parser.parse_args(["-s", "410756", "--lanes"])
        self.assertEqual(args.download_station, "410756")
        self.assertTrue(args.lane_config)

    def test_fetch_station_lane_configuration_mock(self):
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.get_text_content.return_value = """
        <html>
        <head><title>Caltrans PeMS > Mainline VDS 123456 - Test Station > Change Log</title></head>
        <body>
          <table>
            <tr><th colspan="2">Roadway Information (from TSN)</th></tr>
            <tr><td>Road Width</td><td>36 ft</td></tr>
          </table>
          <table>
            <tr><th>Lane</th><th>Slot</th><th>Sensor Tech</th><th>Type</th></tr>
            <tr><td>1</td><td>1</td><td>Dual Loop</td><td>Mainline</td></tr>
          </table>
        </body>
        </html>
        """
        details = pems_interface.fetch_station_lane_configuration("123456", client=mock_client)
        self.assertEqual(details.station_id, "123456")
        self.assertEqual(details.tsn_roadway_info.get("Road Width"), "36 ft")
        self.assertEqual(len(details.lane_table), 1)


if __name__ == "__main__":
    unittest.main()



