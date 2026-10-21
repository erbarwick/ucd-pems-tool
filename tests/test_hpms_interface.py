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
    HPMS_SECTION_ENDPOINTS
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


if __name__ == "__main__":
    unittest.main()
