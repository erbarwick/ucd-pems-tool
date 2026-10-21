#!/usr/bin/env python3
"""Unit tests for HPMS Interface & Corridor Validation Engine."""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd

from hpms_interface import (
    HPMSClient,
    HPMSCorridorEngine,
    CA_COUNTY_FIPS,
    CA_FIPS_TO_COUNTY,
    HPMS_SPATIAL_ENDPOINTS,
    HPMS_SECTION_ENDPOINTS,
    parse_postmile,
    _fmt_aadt_display,
    _is_empty_cell
)


class TestHPMSConstants(unittest.TestCase):
    """Test county FIPS mappings and endpoint dictionaries."""

    def test_county_fips_mapping(self):
        self.assertEqual(CA_COUNTY_FIPS["los angeles"], 37)
        self.assertEqual(CA_COUNTY_FIPS["alameda"], 1)
        self.assertEqual(CA_COUNTY_FIPS["lake"], 33)
        self.assertEqual(CA_COUNTY_FIPS["kern"], 29)
        self.assertEqual(CA_COUNTY_FIPS["fresno"], 19)
        self.assertEqual(CA_COUNTY_FIPS["sacramento"], 67)

    def test_fips_to_county_inversion(self):
        self.assertEqual(CA_FIPS_TO_COUNTY[37], "Los Angeles")
        self.assertEqual(CA_FIPS_TO_COUNTY[1], "Alameda")
        self.assertEqual(CA_FIPS_TO_COUNTY[33], "Lake")

    def test_endpoints_registered(self):
        self.assertIn(2024, HPMS_SPATIAL_ENDPOINTS)
        self.assertIn(2020, HPMS_SECTION_ENDPOINTS)
        self.assertIn(2018, HPMS_SECTION_ENDPOINTS)


class TestHPMSClient(unittest.TestCase):
    """Test HPMSClient caching, query building, and data retrieval."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.client = HPMSClient(cache_dir=self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("urllib.request.urlopen")
    def test_query_caching(self, mock_urlopen):
        # Mock network response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'[{"test_key": "test_val"}]'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # First query should hit network
        res1 = self.client.query("test_uid", where="route=99", use_cache=True)
        self.assertEqual(res1, [{"test_key": "test_val"}])
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second identical query should hit disk cache without network call
        res2 = self.client.query("test_uid", where="route=99", use_cache=True)
        self.assertEqual(res2, [{"test_key": "test_val"}])
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch.object(HPMSClient, "query")
    def test_get_spatial_sections(self, mock_query):
        mock_query.return_value = [
            {"begin_point": "10.0", "end_point": "12.0", "through_lanes": "4", "aadt": "50000"}
        ]
        res = self.client.get_spatial_sections(route_number=99, begin_pm=10.0, end_pm=12.0)
        self.assertEqual(len(res), 1)
        mock_query.assert_called_once()
        args, kwargs = mock_query.call_args
        self.assertEqual(args[0], HPMS_SPATIAL_ENDPOINTS[2024])
        self.assertIn("route_number=99", kwargs["where"])


class TestHPMSCorridorEngine(unittest.TestCase):
    """Test corridor resolution, project inspection, audit, and mapping."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

        # Create dummy master CSV
        self.master_csv = os.path.join(self.temp_dir.name, "master.csv")
        df_master = pd.DataFrame([{
            "Project ID": 9990001,
            "Route": 138,
            "County": "Los Angeles",
            "Begin PM": 55.0,
            "End PM": 56.3,
            "temp_abs_begin": 48.5,
            "temp_abs_end": 49.8,
            "Project Description": "Widen conventional highway from 2 to 4 lanes.",
            "total_lanes_before": 2.0,
            "total_lanes_after": 4.0,
            "dir_lanes_before": 1.0,
            "dir_lanes_after": 2.0,
            "construction_begin_year": 2019,
            "open_traffic_year": 2022
        }])
        df_master.to_csv(self.master_csv, index=False)

        # Create dummy ref postmile CSV
        self.ref_csv = os.path.join(self.temp_dir.name, "ref.csv")
        df_ref = pd.DataFrame([
            {"Route": 138, "County": "Los Angeles", "CA PM": "55.0", "Abs PM": 48.5},
            {"Route": 138, "County": "Los Angeles", "CA PM": "56.3", "Abs PM": 49.8}
        ])
        df_ref.to_csv(self.ref_csv, index=False)

        self.engine = HPMSCorridorEngine(
            master_csv_path=self.master_csv,
            ref_path=self.ref_csv
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_absolute_postmiles(self):
        b, e = self.engine.resolve_absolute_postmiles(138, "Los Angeles", 55.0, 56.3)
        self.assertAlmostEqual(b, 48.5, places=1)
        self.assertAlmostEqual(e, 49.8, places=1)

    @patch.object(HPMSClient, "get_spatial_sections")
    def test_inspect_project(self, mock_get_spatial):
        mock_get_spatial.return_value = [
            {
                "begin_point": "48.5",
                "end_point": "49.0",
                "through_lanes": "4",
                "dir_through_lanes": "2",
                "aadt": "16000",
                "facility_type": "2",
                "line": {"type": "LineString", "coordinates": [[-117.9, 34.5], [-117.8, 34.5]]}
            },
            {
                "begin_point": "49.0",
                "end_point": "49.8",
                "through_lanes": "4",
                "dir_through_lanes": "2",
                "aadt": "16000",
                "facility_type": "2",
                "line": {"type": "LineString", "coordinates": [[-117.8, 34.5], [-117.7, 34.5]]}
            }
        ]

        res = self.engine.inspect_project(9990001)
        self.assertEqual(res["project_id"], 9990001)
        self.assertEqual(res["hpms_predominant_lanes_2024"], 4)
        self.assertEqual(res["hpms_avg_lanes_2024"], 4.0)
        self.assertEqual(res["hpms_avg_aadt_2024"], 16000)
        self.assertEqual(res["hpms_sections_count"], 2)

        # Terminal output formatting check
        text = self.engine.format_inspection_output(res)
        self.assertIn("Widen conventional highway from 2 to 4 lanes.", text)
        self.assertIn("MATCH [HPMS 4 == Spreadsheet 4]", text)

    @patch.object(HPMSClient, "get_spatial_sections")
    @patch.object(HPMSClient, "get_historical_event_lanes")
    def test_audit_project_exact_match(self, mock_hist, mock_spatial):
        mock_spatial.return_value = [
            {
                "begin_point": "48.5", "end_point": "49.8",
                "through_lanes": "4", "dir_through_lanes": "2",
                "aadt": "16000", "facility_type": "2"
            }
        ]
        mock_hist.return_value = [
            {"begin_point": 48.5, "end_point": 49.8, "value_numeric": "2"}
        ]

        audit = self.engine.audit_project(9990001)
        self.assertEqual(audit["audit_status"], "EXACT_MATCH")
        self.assertEqual(audit["hpms_before_lanes"], 2)
        self.assertEqual(audit["hpms_after_lanes"], 4)

        audit_text = self.engine.format_audit_output(audit)
        self.assertIn("EXACT MATCH", audit_text)
        self.assertIn("HPMS Before:      2 lanes", audit_text)
        self.assertIn("HPMS After:       4 lanes", audit_text)

    @patch.object(HPMSClient, "get_spatial_sections")
    def test_generate_hpms_map(self, mock_get_spatial):
        mock_get_spatial.return_value = [
            {
                "begin_point": "48.5", "end_point": "49.8",
                "through_lanes": "4", "dir_through_lanes": "2",
                "aadt": "16000", "facility_type": "2",
                "line": {"type": "LineString", "coordinates": [[-117.9, 34.5], [-117.8, 34.5]]}
            }
        ]
        out_html = os.path.join(self.temp_dir.name, "test_map.html")
        res_path = self.engine.generate_hpms_map(9990001, output_html=out_html)
        self.assertTrue(os.path.exists(res_path))
        with open(res_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Project 9990001", content)
            self.assertIn("Widen conventional highway", content)

    @patch.object(HPMSClient, "get_spatial_sections")
    @patch.object(HPMSClient, "get_historical_event_lanes")
    def test_tandem_audit_project_uninstrumented(self, mock_hist, mock_spatial):
        mock_spatial.return_value = [
            {
                "begin_point": "48.5", "end_point": "49.8",
                "through_lanes": "4", "dir_through_lanes": "2",
                "aadt": "16000", "facility_type": "2",
                "future_aadt": "22000", "future_aadt_year": "2040"
            }
        ]
        mock_hist.return_value = [
            {"begin_point": 48.5, "end_point": 49.8, "value_numeric": "2"}
        ]

        tandem = self.engine.tandem_audit_project(9990001)
        self.assertEqual(tandem["lane_verdict"], "UNINSTRUMENTED_PEMS_RESOLVED_BY_HPMS")
        self.assertEqual(tandem["hpms_after_lanes"], 4)
        self.assertEqual(tandem["hpms_2024_aadt"], 16000)
        self.assertEqual(tandem["hpms_future_aadt"], 22000)
        self.assertEqual(tandem["hpms_future_year"], 2040)

        report = self.engine.format_tandem_output(tandem)
        self.assertIn("CERCET TRIAD CORRIDOR VERIFICATION", report)
        self.assertIn("UNINSTRUMENTED_PEMS_RESOLVED_BY_HPMS", report)
        self.assertIn("16,000 vpd", report)
        self.assertIn("22,000 vpd", report)

    def test_fill_local_spreadsheet_box_guard(self):
        """CRITICAL: Ensure engine strictly forbids targeting any Box-Box directory path."""
        box_path = "/Users/erb/Library/CloudStorage/Box-Box/CERCET - Historical Accuracy of TDM Forecasts/Master.csv"
        with self.assertRaises(PermissionError) as ctx:
            self.engine.fill_local_spreadsheet(target_path=box_path)
        self.assertIn("CRITICAL SAFETY VIOLATION", str(ctx.exception))
        self.assertIn("Box spreadsheet", str(ctx.exception))

    @patch.object(HPMSClient, "get_spatial_sections")
    @patch.object(HPMSClient, "get_historical_event_lanes")
    def test_fill_local_spreadsheet_preview_and_apply(self, mock_hist, mock_spatial):
        mock_spatial.return_value = [
            {
                "begin_point": "48.5", "end_point": "49.8",
                "through_lanes": "4", "dir_through_lanes": "2",
                "aadt": "16000", "facility_type": "2",
                "future_aadt": "25000", "future_aadt_year": "2040"
            }
        ]
        mock_hist.return_value = [
            {"begin_point": 48.5, "end_point": 49.8, "value_numeric": "2"}
        ]

        # 1. Preview mode (apply=False)
        cand_df = self.engine.fill_local_spreadsheet(target_path=self.master_csv, project_id=9990001, apply=False)
        self.assertFalse(cand_df.empty)
        fields = cand_df["field"].tolist()
        self.assertIn("Existing AADT", fields)
        self.assertTrue(any("forecast AADT" in f for f in fields))

        # File on disk should be unchanged
        orig_df = pd.read_csv(self.master_csv)
        self.assertNotIn("Existing AADT", orig_df.columns)

        # Add empty Existing AADT column to master CSV for apply test
        orig_df["Existing AADT"] = None
        orig_df["year "] = None
        orig_df.to_csv(self.master_csv, index=False)

        # 2. Apply mode (apply=True)
        applied_df = self.engine.fill_local_spreadsheet(target_path=self.master_csv, project_id=9990001, apply=True)
        self.assertFalse(applied_df.empty)

        # Verify update on disk
        updated_df = pd.read_csv(self.master_csv)
        self.assertEqual(int(updated_df.loc[0, "Existing AADT"]), 16000)
        self.assertEqual(int(updated_df.loc[0, "year "]), 2024)

        # Verify backup was created
        backups = [f for f in os.listdir(self.temp_dir.name) if f.startswith("master.csv.bak_")]
        self.assertEqual(len(backups), 1)


class TestRobustDataIntegrity(unittest.TestCase):
    """Test rigorous data integrity guards, edge-case postmiles, strict querying,
    qualitative cell protection, and direction-aware resolution."""

    def test_parse_postmile(self):
        # Numeric inputs
        self.assertEqual(parse_postmile(12.3), (12.3, "12.3"))
        self.assertEqual(parse_postmile(0.0), (0.0, "0.0"))
        self.assertEqual(parse_postmile("18.5"), (18.5, "18.5"))

        # Caltrans alignment prefix codes (R, M, L, C, G)
        self.assertEqual(parse_postmile("R18.1"), (18.1, "R18.1"))
        self.assertEqual(parse_postmile("M5.0"), (5.0, "M5.0"))
        self.assertEqual(parse_postmile("L10.5"), (10.5, "L10.5"))
        self.assertEqual(parse_postmile("C2.3"), (2.3, "C2.3"))
        self.assertEqual(parse_postmile("G0.8"), (0.8, "G0.8"))

        # Suffixes and explicit 'PM' text
        self.assertEqual(parse_postmile("PM 50.6"), (50.6, "PM 50.6"))
        self.assertEqual(parse_postmile("14.2R"), (14.2, "14.2R"))
        self.assertEqual(parse_postmile("R14.2A"), (14.2, "R14.2A"))

        # Empty, None, NaN
        self.assertEqual(parse_postmile(None), (None, ""))
        self.assertEqual(parse_postmile(""), (None, ""))
        self.assertEqual(parse_postmile("nan"), (None, ""))
        self.assertEqual(parse_postmile("None"), (None, ""))

    def test_is_empty_cell(self):
        # Empty cells
        self.assertTrue(_is_empty_cell(None))
        self.assertTrue(_is_empty_cell(float("nan")))
        self.assertTrue(_is_empty_cell(""))
        self.assertTrue(_is_empty_cell("   "))
        self.assertTrue(_is_empty_cell("nan"))
        self.assertTrue(_is_empty_cell("None"))
        self.assertTrue(_is_empty_cell("0"))
        self.assertTrue(_is_empty_cell("0.0"))
        self.assertTrue(_is_empty_cell(0))

        # Non-empty cells containing qualitative researcher notes or values
        self.assertFalse(_is_empty_cell(16000))
        self.assertFalse(_is_empty_cell("16000"))
        self.assertFalse(_is_empty_cell("8,510; 2020 \n 10,000; 2030"))
        self.assertFalse(_is_empty_cell("ADT: 23,000-36,000"))
        self.assertFalse(_is_empty_cell("10,000 (2025)"))
        self.assertFalse(_is_empty_cell("4 lanes (2 HOV)"))

    def test_fmt_aadt_display(self):
        # Integers and formatted strings
        self.assertEqual(_fmt_aadt_display(16000), "16,000")
        self.assertEqual(_fmt_aadt_display("25000"), "25,000")
        self.assertEqual(_fmt_aadt_display("25000.0"), "25,000")
        self.assertEqual(_fmt_aadt_display(25000.0), "25,000")

        # Complex archival annotations preserved verbatim
        raw_note = "8,510; 2020 \n 10,000; 2030"
        self.assertEqual(_fmt_aadt_display(raw_note), raw_note)
        raw_range = "23,000-36,000"
        self.assertEqual(_fmt_aadt_display(raw_range), raw_range)

        # Empty / None
        self.assertEqual(_fmt_aadt_display(None), "(None)")
        self.assertEqual(_fmt_aadt_display(""), "(None)")
        self.assertEqual(_fmt_aadt_display("nan"), "(None)")

    def test_cross_county_inversion_guard(self):
        """Verify that begin_pm > end_pm within a single county is guarded against false snapping."""
        temp_dir = tempfile.TemporaryDirectory()
        ref_csv = os.path.join(temp_dir.name, "ref.csv")
        df_ref = pd.DataFrame([
            {"Route": 101, "County": "Santa Clara", "CA PM": "50.6", "Abs PM": 400.0},
            {"Route": 101, "County": "Santa Clara", "CA PM": "6.3", "Abs PM": 355.6}
        ])
        df_ref.to_csv(ref_csv, index=False)
        engine = HPMSCorridorEngine(ref_path=ref_csv)

        # Inverted corridor (begin_pm 50.6 > end_pm 6.3 in Santa Clara)
        b, e = engine.resolve_absolute_postmiles(101, "Santa Clara", 50.6, 6.3)
        self.assertIsNone(b)
        self.assertIsNone(e)
        temp_dir.cleanup()

    def test_snap_threshold_guard(self):
        """Verify that postmiles that differ by > 2.0 miles from any reference point return None."""
        temp_dir = tempfile.TemporaryDirectory()
        ref_csv = os.path.join(temp_dir.name, "ref.csv")
        df_ref = pd.DataFrame([
            {"Route": 4, "County": "Contra Costa", "CA PM": "15.0", "Abs PM": 30.0},
            {"Route": 4, "County": "Contra Costa", "CA PM": "20.0", "Abs PM": 35.0}
        ])
        df_ref.to_csv(ref_csv, index=False)
        engine = HPMSCorridorEngine(ref_path=ref_csv)

        # 25.0 is 5 miles away from closest reference point (20.0) -> exceeds 2.0 mi threshold
        b, e = engine.resolve_absolute_postmiles(4, "Contra Costa", 15.0, 25.0)
        self.assertIsNone(b)
        self.assertIsNone(e)
        temp_dir.cleanup()

    @patch("hpms_interface.HPMSClient.query")
    def test_strict_historical_route_id_query(self, mock_query):
        """Verify that HPMS historical queries strictly match Caltrans SHS routes and omit dangerous wildcards."""
        client = HPMSClient(cache_dir=tempfile.mkdtemp())
        mock_query.return_value = []

        # 2018 query
        client.get_historical_event_lanes(route_number=4, begin_pm=10.0, end_pm=20.0, year=2018)
        self.assertTrue(mock_query.called)
        where_clause = mock_query.call_args[1]["where"]

        # Ensure exact SHS patterns are checked
        self.assertIn("route_id='SHS_004_P'", where_clause)
        self.assertIn("route_id='SHS_4_P'", where_clause)
        # Ensure dangerous wildcard like '%4%P%' is NOT present
        self.assertNotIn("%4%P%", where_clause)

    def test_dual_direction_handling(self):
        """Verify that passing direction='NB' vs direction='SB' correctly targets directional rows."""
        temp_dir = tempfile.TemporaryDirectory()
        master_csv = os.path.join(temp_dir.name, "master.csv")
        df_master = pd.DataFrame([
            {
                "Project ID": 1014000158,
                "Route": 99,
                "County": "San Joaquin",
                "Direction": "NB",
                "Begin PM": 10.0,
                "End PM": 15.0,
                "total_lanes_before": 4.0,
                "total_lanes_after": 6.0,
                "dir_lanes_before": 2.0,
                "dir_lanes_after": 3.0,
                "Project Description": "NB widening"
            },
            {
                "Project ID": 1014000158,
                "Route": 99,
                "County": "San Joaquin",
                "Direction": "SB",
                "Begin PM": 12.0,
                "End PM": 17.0,
                "total_lanes_before": 4.0,
                "total_lanes_after": 6.0,
                "dir_lanes_before": 2.0,
                "dir_lanes_after": 3.0,
                "Project Description": "SB widening"
            }
        ])
        df_master.to_csv(master_csv, index=False)

        ref_csv = os.path.join(temp_dir.name, "ref.csv")
        df_ref = pd.DataFrame([
            {"Route": 99, "County": "San Joaquin", "CA PM": "10.0", "Abs PM": 100.0},
            {"Route": 99, "County": "San Joaquin", "CA PM": "12.0", "Abs PM": 102.0},
            {"Route": 99, "County": "San Joaquin", "CA PM": "15.0", "Abs PM": 105.0},
            {"Route": 99, "County": "San Joaquin", "CA PM": "17.0", "Abs PM": 107.0}
        ])
        df_ref.to_csv(ref_csv, index=False)

        engine = HPMSCorridorEngine(master_csv_path=master_csv, ref_path=ref_csv)

        # Inspect NB
        nb_insp = engine.inspect_project(1014000158, direction="NB")
        self.assertEqual(nb_insp["direction"], "NB")
        self.assertEqual(nb_insp["begin_pm"], 10.0)
        self.assertEqual(nb_insp["end_pm"], 15.0)

        # Inspect SB
        sb_insp = engine.inspect_project(1014000158, direction="SB")
        self.assertEqual(sb_insp["direction"], "SB")
        self.assertEqual(sb_insp["begin_pm"], 12.0)
        self.assertEqual(sb_insp["end_pm"], 17.0)

        temp_dir.cleanup()

    @patch("hpms_interface.HPMSCorridorEngine.tandem_audit_project")
    def test_fill_local_spreadsheet_protects_qualitative_cells(self, mock_audit):
        """Verify fill_local_spreadsheet never overwrites existing qualitative notes or ranges."""
        temp_dir = tempfile.TemporaryDirectory()
        master_csv = os.path.join(temp_dir.name, "master.csv")
        df_master = pd.DataFrame([{
            "Project ID": 8880001,
            "Route": 80,
            "County": "Solano",
            "Direction": "EB",
            "Existing AADT": "8,510; 2020 \n 10,000; 2030",
            "forecast AADT": "ADT: 23,000-36,000",
            "total_lanes_after": "4 lanes (2 HOV)",
            "dir_lanes_after": 2.0
        }])
        df_master.to_csv(master_csv, index=False)

        mock_audit.return_value = {
            "hpms_2024_aadt": 25000,
            "hpms_future_aadt": 35000,
            "hpms_after_lanes": 4,
            "pems_predom_lanes": 2
        }

        engine = HPMSCorridorEngine(master_csv_path=master_csv)
        cand_df = engine.fill_local_spreadsheet(target_path=master_csv, project_id=8880001, apply=False)

        # None of the qualitative fields should be proposed for replacement!
        self.assertTrue(cand_df.empty, f"Expected empty candidates, but found: {cand_df}")
        temp_dir.cleanup()

    @patch("hpms_interface.HPMSCorridorEngine.tandem_audit_project")
    def test_fill_local_spreadsheet_no_odd_lane_halving(self, mock_audit):
        """Verify fill_local_spreadsheet does not naively halve odd lane counts (which are asymmetric)."""
        temp_dir = tempfile.TemporaryDirectory()
        master_csv = os.path.join(temp_dir.name, "master.csv")
        df_master = pd.DataFrame([{
            "Project ID": 8880002,
            "Route": 101,
            "County": "Mendocino",
            "Direction": "NB",
            "dir_lanes_after": None,
            "dir_lanes_before": None
        }])
        df_master.to_csv(master_csv, index=False)

        # 3 lanes total (odd count, e.g. 2 NB, 1 SB)
        mock_audit.return_value = {
            "hpms_after_lanes": 3,
            "hpms_before_lanes": 3,
            "pems_predom_lanes": None  # Rural, no PeMS
        }

        engine = HPMSCorridorEngine(master_csv_path=master_csv)
        cand_df = engine.fill_local_spreadsheet(target_path=master_csv, project_id=8880002, apply=False)

        # Directional lanes should NOT be proposed as 1.5
        dir_fields = cand_df[cand_df["field"].isin(["dir_lanes_after", "dir_lanes_before"])]
        self.assertTrue(dir_fields.empty, f"Expected no fractional directional proposals, found: {dir_fields}")
        temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
