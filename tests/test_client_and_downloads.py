#!/usr/bin/env python3
"""Unit tests for PeMS HTTP authentication, URL builders, table validation, and batch downloads."""

import os
import tempfile
import unittest
import urllib.parse
from unittest.mock import MagicMock, patch

import pandas as pd

from pems.client import (
    PeMSClient,
    build_fwy_latlng_url,
    build_lane_config_url,
    build_station_aadt_url,
    build_station_config_url,
    build_station_flow_url,
    build_station_timeseries_url,
)
from pems.downloads import download_station_data


class TestURLBuilders(unittest.TestCase):
    """Tests proving PeMS URL generation conforms to Caltrans query parameters."""

    def test_build_lane_config_url_parameters(self):
        url = build_lane_config_url("80", "E", "2020-01-15", "2024-12-31")
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
        url = build_station_aadt_url("410756", 2021)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs.get("station_id"), ["410756"])
        self.assertEqual(qs.get("dnode"), ["VDS"])
        self.assertEqual(qs.get("content"), ["analysis"])
        self.assertEqual(qs.get("tab"), ["aadt"])
        self.assertEqual(qs.get("export"), ["text"])
        self.assertEqual(qs.get("s_time_id"), ["1609459200"])
        self.assertEqual(qs.get("e_time_id"), ["1640995199"])
        self.assertEqual(qs.get("s_time_id_f"), ["01/2021"])
        self.assertEqual(qs.get("e_time_id_f"), ["12/2021"])

    def test_build_station_flow_and_timeseries_urls(self):
        url_flow = build_station_flow_url("410756", 2021, granularity="month")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url_flow).query)
        self.assertEqual(qs.get("station_id"), ["410756"])
        self.assertEqual(qs.get("content"), ["loops"])
        self.assertEqual(qs.get("tab"), ["det_timeseries"])
        self.assertEqual(qs.get("q"), ["flow"])
        self.assertEqual(qs.get("gn"), ["month"])
        self.assertEqual(qs.get("holidays"), ["on"])
        self.assertEqual(qs.get("s_time_id_f"), ["01/01/2021 00:00"])
        self.assertEqual(qs.get("e_time_id_f"), ["12/31/2021 23:59"])

        url_ts = build_station_timeseries_url("400001", 2022, 2023, quantity="speed", granularity="hour")
        qs_ts = urllib.parse.parse_qs(urllib.parse.urlparse(url_ts).query)
        self.assertEqual(qs_ts.get("q"), ["speed"])
        self.assertEqual(qs_ts.get("gn"), ["hour"])

    def test_build_fwy_latlng_config_and_xls_formats(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(build_fwy_latlng_url("80", "EB")).query)
        self.assertEqual(qs.get("fwy"), ["80"])
        self.assertEqual(qs.get("dir"), ["E"])
        self.assertEqual(qs.get("content"), ["fwy_latlngXY"])
        self.assertEqual(qs.get("export"), ["text"])

        qs_cfg = urllib.parse.parse_qs(urllib.parse.urlparse(build_station_config_url("410756")).query)
        self.assertEqual(qs_cfg.get("dnode"), ["VDS"])
        self.assertEqual(qs_cfg.get("content"), ["sta_cfg"])
        self.assertEqual(qs_cfg.get("station_id"), ["410756"])

        for u in (
            build_station_flow_url("400001", 2022, export_format="xls"),
            build_station_flow_url("400001", 2022, export_format="xlsx"),
            build_station_aadt_url("400001", 2022, export_format="xls"),
            build_lane_config_url("80", "E", "2020-01-01", "2020-12-31", export_format="xls"),
            build_fwy_latlng_url("80", "EB", export_format="xls"),
        ):
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("export"), ["xls"])


class TestDataValidationAndParsing(unittest.TestCase):
    """Tests checking discrimination between real PeMS data tables and HTML error pages."""

    def test_file_has_data_table_variants(self):
        self.assertTrue(PeMSClient.file_has_data_table("Starting Month\tArithmetic Mean\tFlow\n01/01/2021\t1200\t5000\n"))
        self.assertFalse(PeMSClient.file_has_data_table("<html><head><title>500 Internal Server Error</title></head><body>Error</body></html>"))
        self.assertTrue(PeMSClient.file_has_data_table("<table><tr><th>Month</th><th>Flow</th></tr><tr><td>01/2022</td><td>5000</td></tr></table>"))
        self.assertTrue(PeMSClient.file_has_data_table(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20))
        self.assertTrue(PeMSClient.file_has_data_table(b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 20))

    def test_parse_table_content(self):
        content_with_preamble = (
            "Caltrans Performance Measurement System\n"
            "Report Generated: 2024-01-01\n\n"
            "Starting Month\tArithmetic Mean\n"
            "01/01/2021\t145000\n"
            "02/01/2021\t152000\n"
        )
        df = PeMSClient.parse_table_content(content_with_preamble)
        self.assertIsNotNone(df)
        self.assertEqual(list(df.columns), ["Starting Month", "Arithmetic Mean"])
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["Arithmetic Mean"], 145000)
        self.assertIsNone(PeMSClient.parse_table_content("No data here"))


class TestClientAuthentication(unittest.TestCase):
    """Tests verifying authentication branch handling."""

    @patch.dict(os.environ, {"PEMS_USERNAME": "", "PEMS_PASSWORD": ""}, clear=True)
    def test_login_missing_credentials_raises_value_error(self):
        client = PeMSClient(username="", password="")
        with self.assertRaisesRegex(ValueError, r"PeMS username and password must be provided"):
            client.login()

    @patch("requests.Session.get")
    @patch("requests.Session.post")
    def test_login_success_branch(self, mock_post, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = '<form action="/login"><input name="username"></form>'
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = '<html><body>Welcome user | <a href="/logout">Logout</a></body></html>'

        client = PeMSClient(username="test_user", password="test_password")
        self.assertTrue(client.login())
        self.assertTrue(client.logged_in)
        self.assertEqual(mock_post.call_args[1]["data"]["username"], "test_user")

    @patch("requests.Session.get")
    @patch("requests.Session.post")
    def test_login_failure_no_logout_link_raises_runtime_error(self, mock_post, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = '<form action="/login"><input name="username"></form>'
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "<html><body>Invalid username or password</body></html>"

        client = PeMSClient(username="test_user", password="wrong_password")
        with self.assertRaisesRegex(RuntimeError, r"Login failed: no 'Logout' link found in response."):
            client.login()
        self.assertFalse(client.logged_in)

    @patch.object(PeMSClient, "login")
    @patch("requests.Session.get")
    def test_get_url_reauthenticates_when_redirected_to_login(self, mock_get, mock_login):
        client = PeMSClient(username="test_user", password="test_password")
        client.logged_in = True

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


class TestBatchDownloadEngine(unittest.TestCase):
    """Tests proving the PeMS detector batch download functionality."""

    def test_download_station_data_validates_inputs(self):
        with self.assertRaises(ValueError):
            download_station_data("", data_type="flow")
        with self.assertRaises(ValueError):
            download_station_data("400001", data_type="invalid_type")
        with self.assertRaises(ValueError):
            download_station_data("400001", data_type="flow", granularity="invalid_gran")
        with self.assertRaises(ValueError):
            download_station_data("400001", start_year=2025, end_year=2020)

    def test_download_station_data_skips_empty_years_and_consolidates(self):
        with tempfile.TemporaryDirectory(prefix="pems_test_dl_") as temp_dir:
            mock_client = MagicMock()
            mock_client.logged_in = True
            mock_client.file_has_data_table.side_effect = lambda c: bool(c and "Month" in str(c))

            def mock_get_content(url):
                if "2021" in url:
                    return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n"
                if "2022" in url:
                    return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n01/01/2022\t1000\t1440\t95.0\n02/01/2022\t1100\t1440\t96.0\n"
                if "2023" in url:
                    return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n01/01/2023\t1200\t1440\t97.0\n02/01/2023\t1300\t1440\t98.0\n"
                return None

            mock_client.get_text_content.side_effect = mock_get_content
            res = download_station_data(
                station_id="400001",
                data_type="flow",
                granularity="month",
                start_year=2021,
                end_year=2023,
                output_dir=temp_dir,
                client=mock_client,
                combine=True,
                pause_sec=0.0,
                verbose=False,
            )
            self.assertEqual(res["years_downloaded"], [2022, 2023])
            self.assertEqual(res["years_no_data"], [2021])
            self.assertEqual(res["total_records"], 4)
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "station_400001_2022_flow_month.txt")))
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "station_400001_2021_flow_month.txt")))
            df_comb = pd.read_csv(res["combined_file"])
            self.assertEqual(len(df_comb), 4)
            self.assertIn("Station_ID", df_comb.columns)
            self.assertIn("Month", df_comb.columns)

    def test_download_station_data_with_xls_export_format(self):
        with tempfile.TemporaryDirectory(prefix="pems_test_dl_xls_") as temp_dir:
            mock_client = MagicMock()
            mock_client.logged_in = True
            mock_client.file_has_data_table.side_effect = lambda c: bool(c and "Month" in str(c))
            called_urls = []

            def mock_get_content(url):
                called_urls.append(url)
                return "Month\tFlow (Veh/Month)\t# Lane Points\t% Observed\n01/01/2022\t1000\t1440\t95.0\n"

            mock_client.get_text_content.side_effect = mock_get_content
            res = download_station_data(
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
                export_format="xls",
            )
            self.assertEqual(res["export_format"], "xls")
            self.assertEqual(res["years_downloaded"], [2022])
            self.assertTrue(any("export=xls" in u for u in called_urls))
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "station_400001_2022_flow_month.xls")))


if __name__ == "__main__":
    unittest.main()
