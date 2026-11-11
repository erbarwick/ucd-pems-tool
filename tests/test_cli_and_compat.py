#!/usr/bin/env python3
"""Unit tests for pems CLI parser/handlers, backwards-compatible facade, and Flask app endpoints."""

import io
import unittest
from unittest.mock import patch

import app as flask_app
import pems_interface


class TestCLIParserAndHandlers(unittest.TestCase):
    """Tests verifying organized CLI argument parsing and subcommand dispatch."""

    def test_parser_target_slices_imagery_download_audit(self):
        parser = pems_interface.build_parser()

        for flag in ("-p", "--project", "--inspect"):
            args = parser.parse_args([flag, "400000680"])
            self.assertEqual(args.project, "400000680")
            self.assertFalse(args.slices)
            self.assertFalse(args.imagery)
            self.assertFalse(args.map)

        args = parser.parse_args(["-p", "400000680", "--slices"])
        self.assertTrue(args.slices)
        self.assertEqual(parser.parse_args(["--slices", "400000680"]).slices, "400000680")
        self.assertEqual(parser.parse_args(["--corridor-slices", "400000680"]).slices, "400000680")

        args_img = parser.parse_args(["--imagery", "400000680", "--period", "before", "--date", "2018-05-01", "--zoom", "17", "--margin", "200"])
        self.assertEqual((args_img.imagery, args_img.imagery_period, args_img.imagery_date, args_img.imagery_zoom, args_img.imagery_margin), ("400000680", "before", "2018-05-01", 17, 200.0))

        self.assertTrue(parser.parse_args(["-p", "400000680", "--download"]).download_project)
        self.assertEqual(parser.parse_args(["--download-project", "400000680"]).download_project, "400000680")
        args_st = parser.parse_args(["-s", "404534", "--format", "xlsx"])
        self.assertEqual((args_st.download_station, args_st.export_format), ("404534", "xlsx"))

        args_aud = parser.parse_args(["--audit", "--dist", "4"])
        self.assertTrue(args_aud.match_stations)
        self.assertEqual(args_aud.dist, "4")
        args_ms = parser.parse_args(["--match-stations", "-p", "400000680"])
        self.assertTrue(args_ms.match_stations)
        self.assertEqual(args_ms.project, "400000680")

    def test_parser_lanes_coords_postmile_and_web_fast(self):
        parser = pems_interface.build_parser()

        self.assertTrue(parser.parse_args(["-p", "400000680", "--lane-config"]).lane_config)
        self.assertEqual(parser.parse_args(["--lanes", "400000680"]).lane_config, "400000680")
        args_sl = parser.parse_args(["-s", "410756", "--lanes"])
        self.assertEqual(args_sl.download_station, "410756")
        self.assertTrue(args_sl.lane_config)

        args_c = parser.parse_args(["-r", "101", "-d", "NB", "--coords", "38.186, -122.601", "38.220, -122.607"])
        self.assertEqual(args_c.coords, ["38.186, -122.601", "38.220, -122.607"])
        args_c2 = parser.parse_args(["-r", "101", "--start-coords", "38.186, -122.601", "--end-coords", "38.220, -122.607"])
        self.assertEqual((args_c2.start_coords, args_c2.end_coords), ("38.186, -122.601", "38.220, -122.607"))

        args_pm1 = parser.parse_args(["-r", "101", "-c", "Sonoma", "--pm", "0.21", "-d", "NB"])
        self.assertEqual((args_pm1.county, args_pm1.pm, args_pm1.dir), ("Sonoma", "0.21", "NB"))
        args_pm2 = parser.parse_args(["-r", "101", "--begin-county", "Marin", "--begin-pm", "25.0", "--end-county", "Sonoma", "--end-pm", "2.0", "--map"])
        self.assertTrue(args_pm2.map)
        self.assertEqual((args_pm2.begin_county, args_pm2.end_county), ("Marin", "Sonoma"))

        args_w = parser.parse_args(["-p", "414000032", "--web"])
        self.assertEqual(args_w.project, "414000032")
        self.assertTrue(args_w.web)
        self.assertFalse(args_w.fast)
        self.assertTrue(parser.parse_args(["--web", "414000032", "--fast"]).fast)

        self.assertTrue(parser.parse_args(["--download-lane-config"]).download_lane_config)
        self.assertEqual(parser.parse_args(["--download-lane-config", "414000032"]).download_lane_config, "414000032")
        args_dlc = parser.parse_args(["-s", "410756", "--download-lane-config", "--overwrite"])
        self.assertEqual((args_dlc.download_station, args_dlc.overwrite), ("410756", True))
        self.assertTrue(args_dlc.download_lane_config)

    def test_main_inspect_description_and_absolute_pms(self):
        cap1 = io.StringIO()
        with patch("sys.stdout", cap1):
            pems_interface.main(["-p", "400000680"])
        out1 = cap1.getvalue()
        self.assertIn("PROJECT INSPECTION: 400000680", out1)
        self.assertIn("Description:", out1)
        self.assertIn("In San Mateo County", out1)

        cap2 = io.StringIO()
        with patch("sys.stdout", cap2):
            pems_interface.main(["-p", "200000288"])
        out2 = cap2.getvalue()
        self.assertIn("PROJECT INSPECTION: 200000288", out2)
        self.assertIn("Postmiles:          Begin: 39 | End: 41.2", out2)
        self.assertIn("Absolute PMs:       Begin: 700.0 | End: 702.2", out2)

    @patch("pems.cli.launch_project_web_interface")
    def test_main_web_fast_flag_controls_fetch_online(self, mock_launch):
        pems_interface.main(["-p", "414000032", "--web", "--no-open"])
        self.assertTrue(mock_launch.call_args.kwargs["fetch_online"])

        pems_interface.main(["-p", "414000032", "--web", "--fast", "--no-open"])
        self.assertFalse(mock_launch.call_args.kwargs["fetch_online"])

    @patch("pems.cli.download_station_data")
    @patch("pems.cli.download_all_lane_configurations")
    def test_main_download_lane_config_dispatch(self, mock_dl_cfg, mock_dl_ts):
        pems_interface.main(["--download-lane-config", "--dist", "4", "--overwrite"])
        self.assertEqual(mock_dl_cfg.call_args.kwargs["dist_filter"], "4")
        self.assertIsNone(mock_dl_cfg.call_args.kwargs["project_id_filter"])
        self.assertTrue(mock_dl_cfg.call_args.kwargs["overwrite"])

        pems_interface.main(["-p", "414000032", "--download-lane-config"])
        self.assertEqual(mock_dl_cfg.call_args.kwargs["project_id_filter"], "414000032")
        self.assertFalse(mock_dl_cfg.call_args.kwargs["overwrite"])

        pems_interface.main(["-s", "410756", "--download-lane-config"])
        self.assertEqual(mock_dl_cfg.call_args.kwargs["station_id_filter"], "410756")
        mock_dl_ts.assert_not_called()


class TestFacadeBackwardsCompatibility(unittest.TestCase):
    """Ensure pems_interface exposes its full documented compatibility surface."""

    def test_facade_exports_all_listed_symbols(self):
        for sym in pems_interface.__all__:
            self.assertTrue(hasattr(pems_interface, sym), f"Missing export: {sym}")


class TestFlaskAppEndpoints(unittest.TestCase):
    """Smoke tests for app.py HTTP endpoints using Flask test_client."""

    def setUp(self):
        self.client = flask_app.app.test_client()

    def test_home_and_healthz(self):
        resp_home = self.client.get("/")
        self.assertEqual(resp_home.status_code, 200)

        resp_health = self.client.get("/healthz")
        self.assertEqual(resp_health.status_code, 200)
        self.assertTrue(resp_health.get_json()["ok"])

    def test_api_routes_and_reload(self):
        resp_reload = self.client.post("/api/reload")
        self.assertEqual(resp_reload.status_code, 200)
        reload_data = resp_reload.get_json()
        self.assertTrue(reload_data["reloaded"])
        self.assertGreater(reload_data["rows"], 0)

        resp_routes = self.client.get("/api/routes")
        self.assertEqual(resp_routes.status_code, 200)
        routes_data = resp_routes.get_json()
        self.assertIn("101", routes_data["routes"])

        resp_r101 = self.client.get("/api/route/101")
        self.assertEqual(resp_r101.status_code, 200)
        r101_data = resp_r101.get_json()
        self.assertEqual(r101_data["route"], "101")


if __name__ == "__main__":
    unittest.main()
