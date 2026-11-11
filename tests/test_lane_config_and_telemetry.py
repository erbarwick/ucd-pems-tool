#!/usr/bin/env python3
"""Unit tests for PeMS station lane configuration parsing/caching and Open-to-Traffic telemetry inference."""

import datetime
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from pems.client import build_station_config_url
from pems.lane_config import (
    StationLaneConfigDetails,
    download_all_lane_configurations,
    fetch_station_configs_batch,
    fetch_station_lane_configuration,
    format_station_lane_config_tooltip,
    load_cached_station_config,
    parse_station_configuration_page,
    save_cached_station_config,
)
from pems.projects import ProjectRecord
from pems.reporting import format_lane_config_report
from pems.vds import IndexedVDSMetadata
from pems.telemetry import (
    OpenToTrafficInference,
    StationConfigRecord,
    apply_open_date_to_master_csv,
    apply_open_date_updates_to_dataframe,
    detect_flow_telemetry_activation,
    detect_lane_config_step_changes,
    infer_project_open_to_traffic,
    parse_daily_flow_telemetry,
    parse_station_summary_configuration,
)


class TestOpenToTrafficInference(unittest.TestCase):
    """Unit tests for Option 1: Open-to-Traffic date inference via PeMS configuration & telemetry."""

    def test_parse_station_summary_configuration(self):
        sample_html = """
        <html><body><table class="grid">
            <tr><th>Date</th><th>Status</th><th>Name</th><th>Lanes</th><th>CA PM</th><th>Abs PM</th><th>Length</th><th>Lat</th><th>Lng</th></tr>
            <tr><td>02/06/2019</td><td>Active</td><td>Colfax Curve</td><td>12</td><td>34.447</td><td>135.93</td><td>5.00</td><td>39.1143</td><td>-120.9442</td></tr>
            <tr><td>07/06/2022</td><td>Active</td><td>Colfax Curve</td><td>123</td><td>34.447</td><td>135.93</td><td>5.00</td><td>39.1143</td><td>-120.9442</td></tr>
            <tr><td>12/15/2022</td><td>Active</td><td>Colfax Curve</td><td>123</td><td>34.447</td><td>135.93</td><td>4.48</td><td>39.1143</td><td>-120.9442</td></tr>
        </table></body></html>
        """
        recs = parse_station_summary_configuration(sample_html)
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[0].date, datetime.date(2019, 2, 6))
        self.assertEqual(recs[0].lane_count, 2)
        self.assertEqual(recs[0].lanes_str, "12")
        self.assertEqual(recs[1].date, datetime.date(2022, 7, 6))
        self.assertEqual(recs[1].lane_count, 3)
        self.assertEqual(recs[1].lanes_str, "123")

    def test_detect_lane_config_step_changes(self):
        recs = [
            StationConfigRecord(date=datetime.date(2018, 1, 1), date_str="2018-01-01", status="Active", name="Test", lanes_str="12", lane_count=2, ca_pm="10.0", abs_pm="100.0"),
            StationConfigRecord(date=datetime.date(2020, 6, 15), date_str="2020-06-15", status="Active", name="Test", lanes_str="123", lane_count=3, ca_pm="10.0", abs_pm="100.0"),
        ]
        events = detect_lane_config_step_changes(recs, station_id="12345")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].change_type, "DETECTOR_COMMISSIONED")
        self.assertEqual(events[0].change_date, datetime.date(2018, 1, 1))
        self.assertEqual(events[1].change_type, "LANE_CONFIG_INCREMENT")
        self.assertEqual(events[1].change_date, datetime.date(2020, 6, 15))
        self.assertEqual((events[1].lanes_before, events[1].lanes_after), (2, 3))

        events_filtered = detect_lane_config_step_changes(recs, station_id="12345", min_date=datetime.date(2019, 1, 1))
        self.assertEqual(len(events_filtered), 1)
        self.assertEqual(events_filtered[0].change_type, "LANE_CONFIG_INCREMENT")

    def test_parse_daily_flow_telemetry_and_step_activation(self):
        sample_tsv = (
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
        flow_records = parse_daily_flow_telemetry(sample_tsv)
        self.assertEqual(len(flow_records), 9)
        self.assertEqual(flow_records[5]["date"], datetime.date(2022, 7, 6))
        self.assertEqual(flow_records[5]["lane_points"], 863)

        mock_client = MagicMock()
        mock_client.get_text_content.return_value = sample_tsv
        res = detect_flow_telemetry_activation(
            client=mock_client, station_id="314000", target_year=2022, candidate_date=datetime.date(2022, 7, 6)
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.activation_date, datetime.date(2022, 7, 6))
        self.assertEqual(res.activation_type, "LANE_POINTS_STEP_CHANGE")
        self.assertEqual((res.lane_points_before, res.lane_points_after), (576, 863))
        self.assertIn("576 (~2 lanes) to 863 (~3 lanes)", res.evidence)

    def test_infer_project_open_to_traffic_corroborated(self):
        mock_client = MagicMock()
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
        mock_client.get_text_content.side_effect = lambda url: sample_summary_html if "content=summary" in url else (sample_tsv if "tab=det_timeseries" in url else "")

        proj = ProjectRecord(
            project_id="300020420", dist="3", county="Placer", state="CA", route="80", direction="EB",
            begin_pm="34.4", end_pm="39.4", construction_begin_date="2019-01-01", open_to_traffic_date="2019-01-23",
            sensor_stations=["314000"],
        )
        inf = infer_project_open_to_traffic(proj, client=mock_client, check_telemetry=True, verbose=False)
        self.assertEqual(inf.inferred_date, "2022-07-06")
        self.assertEqual(inf.inferred_year, 2022)
        self.assertEqual(inf.confidence, "HIGH")
        self.assertEqual(inf.method, "CORROBORATED_TELEMETRY")
        self.assertEqual(inf.station_id, "314000")
        self.assertIn("incremented from 2 to 3 lanes on 2022-07-06", inf.evidence)

    def test_apply_open_date_updates_to_dataframe_and_csv_backup(self):
        df = pd.DataFrame({
            "Project ID": ["300020420", "9990001"],
            "Direction": ["EB", "NB"],
            "Open to Traffic Date": ["43488", None],
            "Open to Traffic Date (formatted YYYY-MM-DD)": ["2019-01-23", None],
            "open_traffic_year": [2019, None],
            "open_traffic_month": [1, None],
            "open_traffic_day": [23, None],
            "Sensor Station 1 ID ": ["314000", "123456"],
            "Sensor 1 Activation Date for New Lane": [None, None],
        })
        inf = OpenToTrafficInference(
            project_id="300020420", route="80", direction="EB", district="3", county="Placer",
            current_open_date="2019-01-23", inferred_date="2022-07-06", inferred_year=2022,
            confidence="HIGH", method="CORROBORATED_TELEMETRY", evidence="Verified", station_id="314000",
        )
        updated, count = apply_open_date_updates_to_dataframe(df, [inf])
        self.assertEqual(count, 1)
        self.assertEqual(updated.at[0, "Open to Traffic Date"], "2022-07-06")
        self.assertEqual(updated.at[0, "Open to Traffic Date (formatted YYYY-MM-DD)"], "2022-07-06")
        self.assertEqual((str(updated.at[0, "open_traffic_year"]), str(updated.at[0, "open_traffic_month"]), str(updated.at[0, "open_traffic_day"])), ("2022", "7", "6"))
        self.assertEqual(updated.at[0, "Sensor 1 Activation Date for New Lane"], "2022-07-06")
        self.assertTrue(pd.isna(updated.at[1, "Open to Traffic Date"]))

        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = os.path.join(tmp_dir, "test_master.csv")
            df.iloc[[0]].to_csv(csv_path, index=False)
            backup_path, target_path, applied = apply_open_date_to_master_csv([inf], master_csv_path=csv_path)
            self.assertTrue(os.path.exists(backup_path))
            self.assertEqual(applied, 1)
            self.assertEqual(pd.read_csv(target_path).at[0, "Open to Traffic Date"], "2022-07-06")


class TestStationLaneConfigurationAndCaching(unittest.TestCase):
    """Tests for PeMS station lane config HTML parsing, reporting, disk caching, and tooltips."""

    def test_build_station_config_url(self):
        self.assertEqual(
            build_station_config_url("410756"),
            "https://pems.dot.ca.gov/?dnode=VDS&content=sta_cfg&tab=sta_cfg&station_id=410756",
        )

    def test_parse_station_configuration_page_and_errors(self):
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
          </table>
          <table>
            <tr><th colspan="9">Change Log</th></tr>
            <tr><th>Date</th><th>Status</th><th>Name</th><th>Lanes</th><th>CA PM</th><th>Abs PM</th><th>Length</th><th>Lat</th><th>Lng</th></tr>
            <tr><td>04/09/2016</td><td>Active</td><td>EB 12 OC</td><td>1234</td><td>11.86</td><td>39.33</td><td>0.82</td><td>38.20691</td><td>-122.151326</td></tr>
            <tr><td>05/20/2017</td><td>Active</td><td>EB 12 OC</td><td>12345</td><td>11.86</td><td>39.33</td><td>0.75</td><td>38.20691</td><td>-122.151326</td></tr>
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
        </body></html>
        """
        details = parse_station_configuration_page(sample_html, station_id="410756")
        self.assertEqual((details.station_id, details.station_name, details.station_type), ("410756", "EB 12 OC", "Mainline VDS"))
        self.assertIsNone(details.error)
        self.assertEqual(details.tsn_roadway_info.get("Road Width"), "48 ft")
        self.assertEqual(details.tsn_roadway_info.get("Design Speed Limit"), "70 mph")
        self.assertEqual(len(details.lane_table), 3)
        self.assertEqual(len(details.change_log), 2)
        self.assertEqual(details.change_log[1]["Lanes"], "12345")
        self.assertEqual(details.construction_history[0]["End Date"], "01/14/2018")

        err_html = "<html><head><title>PeMS Internal Error</title></head><body><h1>Application Halted</h1><p>Missing numeric segment identifier.</p></body></html>"
        err_details = parse_station_configuration_page(err_html, station_id="716177")
        self.assertIn("PeMS internal error", err_details.error)

        tsn_na_html = """
        <html><head><title>Caltrans PeMS > Mainline VDS 3411074 - 65NB Galleria Blvd > Change Log</title></head>
        <body>
          <table class="blue_outline_table"><tr><td colspan="2">Roadway Information (from TSN)</td></tr><tr><td>Not Available</td></tr></table>
          <table><tr><th>Lane</th><th>Slot</th><th>ID</th><th>Type</th></tr><tr><td>1</td><td>1</td><td>34110717</td><td>Mainline</td></tr></table>
        </body></html>
        """
        na_details = parse_station_configuration_page(tsn_na_html, station_id="3411074")
        self.assertEqual(na_details.tsn_roadway_info.get("Status"), "Not Available in PeMS")
        self.assertEqual(na_details.lane_table[0]["ID"], "34110717")

    def test_format_lane_config_report_standard_and_hpms(self):
        details = StationLaneConfigDetails(
            station_id="410756", station_name="EB 12 OC", station_type="ML",
            tsn_roadway_info={"Road Width": "48 ft", "Lane Width": "12.0 ft"},
            lane_table=[{"Lane": "1", "Slot": "1", "Sensor Tech": "Dual Loop", "Type": "Mainline"}],
            change_log=[{"Date": "04/09/2016", "Status": "Active", "Lanes": "12345"}],
            construction_history=[{"Start Date": "03/14/2017", "End Date": "01/14/2018"}],
        )
        report = format_lane_config_report(details)
        self.assertIn("STATION 410756: EB 12 OC (ML)", report)
        self.assertIn("48 ft", report)
        self.assertIn("04/09/2016", report)
        self.assertIn("03/14/2017", report)

        na_st = StationLaneConfigDetails(station_id="3411074", station_name="65NB Galleria", tsn_roadway_info={"Status": "Not Available in PeMS"})
        hpms_info = {
            "sections_count": 41, "predominant_lanes_2024": 6, "avg_lanes_2024": 5.93,
            "facility_type": "Two-Way Divided (Freeway/Expressway)", "access_control": "Full Access Control (Freeway)",
            "surface_type": "Concrete", "functional_system": "Principal Arterial",
            "avg_aadt_2024": 123594, "future_aadt": 137007, "future_aadt_year": "2044",
        }
        hpms_rep = format_lane_config_report(na_st, hpms_summary=hpms_info)
        self.assertIn("HPMS Ground-Truth Roadway Attributes", hpms_rep)
        self.assertIn("Through Lanes (2024):         6 (Corridor length-weighted avg: 5.93)", hpms_rep)
        self.assertIn("123,594 vehicles/day", hpms_rep)
        self.assertIn("137,007 vehicles/day (Year 2044)", hpms_rep)
        self.assertIn("Not Available in PeMS for this station", hpms_rep)

    def test_fetch_station_lane_configuration_mock_isolated_cache(self):
        mock_client = MagicMock()
        mock_client.get_text_content.return_value = """
        <html><head><title>Caltrans PeMS > Mainline VDS 123456 - Test Station > Change Log</title></head>
        <body>
          <table><tr><th colspan="2">Roadway Information (from TSN)</th></tr><tr><td>Road Width</td><td>36 ft</td></tr></table>
          <table><tr><th>Lane</th><th>Slot</th><th>Sensor Tech</th><th>Type</th></tr><tr><td>1</td><td>1</td><td>Dual Loop</td><td>Mainline</td></tr></table>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            details = fetch_station_lane_configuration("123456", client=mock_client, use_cache=True, cache_dir=tmpdir)
            self.assertEqual(details.station_id, "123456")
            self.assertEqual(details.tsn_roadway_info.get("Road Width"), "36 ft")
            self.assertEqual(len(details.lane_table), 1)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "123456.json")))

    def test_station_config_disk_cache_and_tooltip_formatting(self):
        st = StationLaneConfigDetails(
            station_id="410756", station_name="EB 12 OC", district="4", route="80", direction="E",
            county="Solano", station_type="ML", ca_pm="11.86", abs_pm=39.33,
            latitude=38.20691, longitude=-122.151326,
            tsn_roadway_info={"Road Width": "48 ft", "Lane Width": "12.0 ft", "Design Speed Limit": "70 mph"},
            lane_table=[
                {"Lane": "1", "Type": "Mainline", "Sensor Tech": "Dual Loop"},
                {"Lane": "2", "Type": "Mainline", "Sensor Tech": "Dual Loop"},
                {"Lane": "3", "Type": "Mainline", "Sensor Tech": "Dual Loop"},
            ],
            change_log=[
                {"Date": "04/09/2016", "Status": "Active", "Lanes": "123"},
                {"Date": "05/20/2017", "Status": "Active", "Lanes": "1234"},
            ],
            construction_history=[{"Start Date": "03/14/2017", "End Date": "01/14/2018"}],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = save_cached_station_config(st, cache_dir=tmpdir)
            self.assertTrue(os.path.exists(cache_file))
            loaded = load_cached_station_config("410756", cache_dir=tmpdir)
            self.assertEqual(loaded.station_name, "EB 12 OC")
            self.assertEqual(loaded.tsn_roadway_info["Road Width"], "48 ft")

        proj = ProjectRecord(
            project_id="418000244", dist="4", county="Solano", state="CA", route="80", direction="E",
            begin_pm="11.4", end_pm="12.8",
            raw_row={
                "Sensor Station 1 ID": "410756",
                "Sensor Station 1: number of lanes before": "3",
                "Sensor Station 1: number of lanes after": "4",
                "s1_new_lane_year": "2017",
                "Sensor Station 1: lane type": "Mainline",
            },
        )
        tooltip = format_station_lane_config_tooltip(st, project=proj)
        self.assertIn("STATION 410756 (ML) - EB 12 OC", tooltip)
        self.assertIn("CURRENT LANES: 3", tooltip)
        self.assertIn("LANE COUNTS OVER TIME (PeMS Change Log):", tooltip)
        self.assertIn("SPREADSHEET AUDIT (Sensor #1):", tooltip)
        self.assertIn("Lanes: 3 before -> 4 after", tooltip)
        self.assertIn("TSN ROADWAY CROSS-SECTION:", tooltip)

        change_log = [{"Date": f"01/01/200{i}" if i < 10 else f"01/01/20{i}", "Status": "Active", "Lanes": "1234"} for i in range(17)]
        st_full = StationLaneConfigDetails(station_id="401318", station_name="Red Top Rd", station_type="ML", lane_table=[{"Lane": str(i + 1), "Type": "Mainline"} for i in range(4)], change_log=change_log)
        full_tip = format_station_lane_config_tooltip(st_full)
        self.assertNotIn("unchanged checkpoints", full_tip)
        for i in range(17):
            dt = f"01/01/200{i}" if i < 10 else f"01/01/20{i}"
            self.assertIn(dt, full_tip)

    def test_download_all_lane_configurations_deduplicates_skips_cached_and_overwrites(self):
        vds_df = pd.DataFrame([
            {"ID": "410756", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "12.0", "Abs_PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Length": "0.5", "Type": "ML", "Lanes": "4", "Name": "WEST TEXAS"},
            {"ID": "429041", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "12.5", "Abs_PM": "40.0", "Latitude": "38.212", "Longitude": "-122.142", "Length": "0.4", "Type": "HV", "Lanes": "1", "Name": "TRAVIS HOV"},
        ])
        vds_meta = IndexedVDSMetadata(vds_df)
        projs = [
            ProjectRecord(project_id="P1", dist="4", county="Solano", state="CA", route="80", direction="EB", begin_pm="11.4", end_pm="12.8", abs_begin_pm=38.9, abs_end_pm=40.3, sensor_stations=["410756"]),
            ProjectRecord(project_id="P2", dist="3", county="Placer", state="CA", route="80", direction="EB", begin_pm="34.0", end_pm="36.0", abs_begin_pm=135.0, abs_end_pm=137.0, sensor_stations=["314000"]),
        ]
        sample_html = lambda sid: f"""
        <html><head><title>Caltrans PeMS > Mainline VDS {sid} - St {sid} > Change Log</title></head>
        <body><table><tr><th>Lane</th><th>Type</th></tr><tr><td>1</td><td>Mainline</td></tr></table></body></html>
        """

        with tempfile.TemporaryDirectory() as cache_dir:
            # Pre-cache 410756 cleanly (even with empty change_log!)
            save_cached_station_config(
                StationLaneConfigDetails(station_id="410756", station_name="Cached 410756", lane_table=[{"Lane": "1", "Type": "Mainline"}]),
                cache_dir=cache_dir,
            )
            mock_client = MagicMock()
            mock_client.logged_in = True
            mock_client.get_text_content.side_effect = lambda url, timeout=30.0: sample_html(url.split("station_id=")[-1])

            res1 = download_all_lane_configurations(
                projects=projs,
                dist_filter="4",
                cache_dir=cache_dir,
                vds_meta=vds_meta,
                client=mock_client,
                buffer_mi=0.5,
                verbose=False,
            )
            self.assertEqual(res1["total_stations"], 2)
            self.assertEqual(res1["already_cached"], 1)
            self.assertEqual(res1["downloaded"], 1)
            self.assertEqual(res1["failed"], 0)
            self.assertTrue(os.path.exists(os.path.join(cache_dir, "429041.json")))
            self.assertEqual(mock_client.get_text_content.call_count, 1)

            # Second run with overwrite=False skips PeMS calls completely (even without login)
            mock_client2 = MagicMock()
            mock_client2.logged_in = False
            res2 = download_all_lane_configurations(
                projects=projs, dist_filter="4", cache_dir=cache_dir, vds_meta=vds_meta, client=mock_client2, buffer_mi=0.5, verbose=False
            )
            self.assertEqual((res2["already_cached"], res2["downloaded"]), (2, 0))
            mock_client2.login.assert_not_called()

            # Overwrite=True re-downloads both
            mock_client3 = MagicMock()
            mock_client3.logged_in = True
            mock_client3.get_text_content.side_effect = lambda url, timeout=30.0: sample_html(url.split("station_id=")[-1])
            res3 = download_all_lane_configurations(
                projects=projs, dist_filter="4", cache_dir=cache_dir, vds_meta=vds_meta, client=mock_client3, buffer_mi=0.5, overwrite=True, verbose=False
            )
            self.assertEqual((res3["already_cached"], res3["downloaded"]), (0, 2))

            # Single-station error does not overwrite cache with an error file
            mock_err = MagicMock()
            mock_err.logged_in = True
            mock_err.get_text_content.return_value = None
            res_err = download_all_lane_configurations(
                station_id_filter="999888", cache_dir=cache_dir, vds_meta=vds_meta, client=mock_err, verbose=False
            )
            self.assertEqual(res_err["failed"], 1)
            self.assertFalse(os.path.exists(os.path.join(cache_dir, "999888.json")))

    @patch("pems.lane_config.PeMSClient")
    def test_fetch_station_configs_batch_uses_independent_worker_sessions(self, mock_client_cls):
        created = []

        def _make_client():
            mc = MagicMock()
            mc.logged_in = False

            def _login():
                mc.logged_in = True
                return True

            mc.login.side_effect = _login
            mc.get_text_content.side_effect = lambda url, timeout=30.0: (
                time.sleep(0.02)
                or f"<html><head><title>Caltrans PeMS > Mainline VDS {url.split('station_id=')[-1]} - St > Change Log</title></head>"
                "<body><table><tr><th>Lane</th><th>Type</th></tr><tr><td>1</td><td>Mainline</td></tr></table></body></html>"
            )
            created.append(mc)
            return mc

        mock_client_cls.side_effect = _make_client
        with tempfile.TemporaryDirectory() as cache_dir:
            res = fetch_station_configs_batch(
                ["400001", "400002", "400003", "400004"],
                workers=2,
                use_cache=True,
                cache_dir=cache_dir,
            )
        self.assertEqual(set(res.keys()), {"400001", "400002", "400003", "400004"})
        self.assertEqual(len(created), 2)
        for mc in created:
            mc.login.assert_called_once()
            mc.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
