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
import pandas as pd
import numpy as np

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


class TestDataValidationAndParsing(unittest.TestCase):
    """Tests checking discrimination between real PeMS data tables and HTML error pages."""

    def test_file_has_data_table_positive(self):
        valid_table = "Starting Month\tArithmetic Mean\tFlow\n01/01/2021\t1200\t5000\n"
        self.assertTrue(pems_interface.PeMSClient.file_has_data_table(valid_table))

    def test_file_has_data_table_negative_html_error(self):
        html_error = "<html><head><title>500 Internal Server Error</title></head><body>Error</body></html>"
        self.assertFalse(pems_interface.PeMSClient.file_has_data_table(html_error))

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


if __name__ == "__main__":
    unittest.main()
