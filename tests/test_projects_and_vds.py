#!/usr/bin/env python3
"""Unit tests for Master CSV parsing, ProjectRecord, VDS metadata indexing, and station comparison audits."""

import datetime
import glob
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import pems_interface
from pems.config import DEFAULT_MASTER_CSV
from pems.geo import IndexedGeoReference
from pems.normalize import build_date_from_parts
from pems.projects import ProjectRecord, get_project_corridor_geometry, parse_master_row
from pems.vds import (
    IndexedVDSMetadata,
    StationComparisonResult,
    VDSStationRecord,
    apply_station_updates_to_dataframe,
    compare_project_stations,
    run_station_comparison_pipeline,
)


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
            "Sensor Station 4 ID ": "nan",
        }
        rec = parse_master_row(raw_row)
        self.assertEqual(rec.project_id, "418000244")
        self.assertEqual(rec.dist, "4")
        self.assertEqual(rec.county, "Solano")
        self.assertEqual(rec.direction, "EB")
        self.assertEqual(rec.pems_direction, "E")
        self.assertEqual(rec.construction_begin_date, "2021-09-01")
        self.assertEqual(rec.open_to_traffic_date, "2022-10-01")
        self.assertEqual(rec.start_year, 2021)
        self.assertEqual(rec.end_year, max(2026, datetime.date.today().year))
        self.assertEqual(rec.sensor_stations, ["410756", "429041"])

    def test_parse_master_row_compound_sensors_and_description(self):
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
            "area_type": "suburban",
            "Project Description": "Widen freeway from 4 to 6 lanes with HOV facility.",
        }
        rec = parse_master_row(row)
        self.assertEqual(rec.begin_pm, "6.2")
        self.assertEqual(rec.end_pm, "8.7")
        self.assertEqual(rec.abs_begin_pm, 78.4)
        self.assertEqual(rec.abs_end_pm, 80.9)
        self.assertEqual(rec.sensor_stations, ["1204409", "1210066", "1204395", "1210063"])
        self.assertNotIn("12043951210063", rec.sensor_stations)
        self.assertNotIn("5764", rec.sensor_stations)
        self.assertEqual(rec.project_description, "Widen freeway from 4 to 6 lanes with HOV facility.")

        for empty_val in ("", "nan", "None", None):
            r = parse_master_row({"Project ID": "1", "Dist": "4", "Route": "101", "Direction": "NB", "Begin PM": "1", "End PM": "2", "Project Description": empty_val})
            self.assertEqual(r.project_description, "")


class TestVDSStationMatcherAndComparison(unittest.TestCase):
    """Tests for VDS metadata indexing, corridor matching, dry-run safety, and apply updates."""

    def setUp(self):
        self.meta_df = pd.DataFrame([
            {"ID": "301001", "Fwy": "80", "Dir": "E", "District": "3", "County": "61", "City": "Colfax", "State_PM": "34.5", "Abs_PM": "135.5", "Latitude": "39.1", "Longitude": "-120.9", "Length": "1.0", "Type": "ML", "Lanes": "3", "Name": "Colfax Curve EB"},
            {"ID": "301002", "Fwy": "80", "Dir": "E", "District": "3", "County": "61", "City": "Colfax", "State_PM": "36.0", "Abs_PM": "137.0", "Latitude": "39.12", "Longitude": "-120.88", "Length": "1.0", "Type": "ML", "Lanes": "3", "Name": "Colfax East ML"},
            {"ID": "301003", "Fwy": "80", "Dir": "E", "District": "3", "County": "61", "City": "Colfax", "State_PM": "37.5", "Abs_PM": "138.5", "Latitude": "39.14", "Longitude": "-120.85", "Length": "1.0", "Type": "HV", "Lanes": "1", "Name": "Colfax East HV"},
            {"ID": "301004", "Fwy": "80", "Dir": "E", "District": "3", "County": "61", "City": "Colfax", "State_PM": "36.5", "Abs_PM": "137.5", "Latitude": "39.13", "Longitude": "-120.86", "Length": "0.5", "Type": "OR", "Lanes": "1", "Name": "Colfax On-Ramp"},
            {"ID": "301005", "Fwy": "80", "Dir": "W", "District": "3", "County": "61", "City": "Colfax", "State_PM": "36.0", "Abs_PM": "137.0", "Latitude": "39.12", "Longitude": "-120.88", "Length": "1.0", "Type": "ML", "Lanes": "3", "Name": "Colfax West ML"},
        ])
        self.indexed_meta = IndexedVDSMetadata(self.meta_df)

    def test_indexed_vds_metadata_partitioning_and_id_index(self):
        sub = self.indexed_meta.index.get(("3", "80", "E"))
        self.assertIsNotNone(sub)
        self.assertEqual(len(sub), 4)
        rec = self.indexed_meta.by_id.get("301003")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.type, "HV")
        self.assertEqual(rec.abs_pm, 138.5)
        self.assertEqual(rec.name, "Colfax East HV")

    def test_find_corridor_stations_abs_pm_fallback_and_types(self):
        found = self.indexed_meta.find_corridor_stations(
            district="3", route="80", direction="EB", begin_pm="35.0", end_pm="39.0", abs_begin_pm=136.0, abs_end_pm=139.0, buffer_mi=0.1, station_types=("ML", "HV")
        )
        self.assertEqual([s.id for s in found], ["301002", "301003"])
        self.assertNotIn("301004", [s.id for s in found])

        found_ca = self.indexed_meta.find_corridor_stations(
            district="3", route="80", direction="EB", begin_pm="34.0", end_pm="36.2", abs_begin_pm=None, abs_end_pm=None, buffer_mi=0.1, station_types=("ML",)
        )
        self.assertEqual([s.id for s in found_ca], ["301001", "301002"])

        with_ramps = self.indexed_meta.find_corridor_stations(
            district="3", route="80", direction="EB", begin_pm="35.0", end_pm="39.0", abs_begin_pm=136.0, abs_end_pm=139.0, buffer_mi=0.1, station_types=("ML", "HV", "OR")
        )
        self.assertIn("301004", [s.id for s in with_ramps])

    def test_county_isolation_in_vds_matching(self):
        meta_df = pd.DataFrame([
            {"ID": "400101", "Fwy": "101", "Dir": "N", "District": "4", "County": "San Mateo", "City": "San Mateo", "State_PM": "10.0", "Abs_PM": None, "Latitude": "37.56", "Longitude": "-122.32", "Length": "1.0", "Type": "ML", "Lanes": "4", "Name": "SM"},
            {"ID": "400102", "Fwy": "101", "Dir": "N", "District": "4", "County": "Santa Clara", "City": "San Jose", "State_PM": "10.0", "Abs_PM": None, "Latitude": "37.33", "Longitude": "-121.88", "Length": "1.0", "Type": "ML", "Lanes": "4", "Name": "SCL"},
        ])
        vds_meta = IndexedVDSMetadata(meta_df)
        self.assertEqual([s.id for s in vds_meta.find_corridor_stations("4", "101", "N", "9.0", "11.0", None, None, county="San Mateo")], ["400101"])
        self.assertEqual([s.id for s in vds_meta.find_corridor_stations("4", "101", "N", "9.0", "11.0", None, None, county="Santa Clara")], ["400102"])

    def test_compare_project_stations_statuses_and_discrepancies(self):
        base = dict(dist="3", county="Placer", state="CA", route="80", direction="EB", begin_pm="35.0", end_pm="39.0", abs_begin_pm=136.0, abs_end_pm=139.0)

        res_exact = compare_project_stations(ProjectRecord(project_id="P_EXACT", sensor_stations=["301002", "301003"], **base), vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res_exact.status, "EXACT_MATCH")
        self.assertEqual(res_exact.matched_stations, ["301002", "301003"])
        self.assertEqual(res_exact.discrepancies, [])

        res_partial = compare_project_stations(ProjectRecord(project_id="P_PARTIAL", sensor_stations=["301002"], **base), vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res_partial.status, "PARTIAL_MATCH")
        self.assertEqual(res_partial.missing_in_sheet, ["301003"])

        oob_proj = ProjectRecord(project_id="P_DISC", dist="3", county="Placer", state="CA", route="80", direction="EB", begin_pm="36.0", end_pm="39.0", abs_begin_pm=136.5, abs_end_pm=139.0, sensor_stations=["301001"])
        res_oob = compare_project_stations(oob_proj, vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertEqual(res_oob.status, "DISCREPANCY")
        self.assertTrue(any("Out of range" in r and "1.00 mi before" in r for r in res_oob.discrepancies[0]["reasons"]))

        res_dir = compare_project_stations(ProjectRecord(project_id="P_DIR", sensor_stations=["301005"], **base), vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertTrue(any("Opposite/Different Direction" in r for r in res_dir.discrepancies[0]["reasons"]))

        res_unk = compare_project_stations(ProjectRecord(project_id="P_UNK", sensor_stations=["9999999"], **base), vds_meta=self.indexed_meta, buffer_mi=0.1)
        self.assertFalse(res_unk.discrepancies[0]["found_in_pems"])
        self.assertIn("Invalid or obsolete PeMS VDS ID", res_unk.discrepancies[0]["reasons"][0])

    def test_dry_run_and_apply_pipeline_via_facade_mock(self):
        header = (
            "Project ID,Dist,County,Route,Direction,Begin PM,End PM,temp_abs_begin,temp_abs_end,"
            "Number of Active Non-Ramp Sensors,Sensor Station 1 ID ,Sensor Station 1 HOV ID,"
            "Sensor Station 1 Name,Sensor Station 1 Type,Sensor Station 1: number of lanes after,"
            "Sensor Station 2 ID ,Sensor Station 2: number of lanes after\n"
        )
        row = "1001,3,Placer,80,EB,35.0,39.0,136.0,139.0,,,,,,,,\n"
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = os.path.join(tmp_dir, "master.csv")
            with open(temp_path, "w", encoding="utf-8") as fh:
                fh.write(header + row)

            content_before = header + row
            with patch("pems_interface.load_vds_metadata", return_value=self.indexed_meta):
                pems_interface.run_station_comparison_pipeline(
                    master_csv_path=temp_path, dry_run=True, apply_updates=False, verbose=False
                )
            with open(temp_path, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), content_before)
            self.assertEqual(glob.glob(f"{temp_path}.bak_*"), [])

            with patch("pems_interface.load_vds_metadata", return_value=self.indexed_meta):
                pems_interface.run_station_comparison_pipeline(
                    master_csv_path=temp_path, dry_run=False, apply_updates=True, verbose=False
                )
            self.assertEqual(len(glob.glob(f"{temp_path}.bak_*")), 1)
            df_updated = pd.read_csv(temp_path, dtype=str)
            self.assertEqual(df_updated.at[0, "Number of Active Non-Ramp Sensors"], "2")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 ID "], "301002")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 Type"], "ML")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 Name"], "Colfax East ML")
            self.assertEqual(df_updated.at[0, "Sensor Station 1 HOV ID"], "301003")
            self.assertTrue(pd.isna(df_updated.at[0, "Sensor Station 2 ID "]) or df_updated.at[0, "Sensor Station 2 ID "] == "")

    def test_apply_station_updates_clears_stale_and_formats_excel_dates(self):
        df = pd.DataFrame([{
            "Project ID": "P999",
            "Direction": "NB",
            "Number of Active Non-Ramp Sensors": "2",
            "Sensor Station 1 ID ": "OLD_1",
            "Sensor Station 1 HOV ID": "OLD_HOV",
            "Sensor Station 1 Name": "Old Station",
            "Sensor Station 1 Type": "ML",
            "Sensor Station 1: number of lanes after": "3",
            "Sensor Station 2 ID ": "OLD_2",
            "Sensor Station 2: number of lanes after": "3",
        }])
        comp_zero = StationComparisonResult(
            project_id="P999", route="101", direction="NB", district="4", county="Marin",
            begin_pm="1.0", end_pm="2.0", abs_begin_pm=10.0, abs_end_pm=11.0,
            existing_stations=["OLD_1", "OLD_2"], found_stations=[], matched_stations=[],
            missing_in_sheet=[], discrepancies=[], status="UNINSTRUMENTED", sheet_sensor_count=2, found_sensor_count=0,
        )
        updated = apply_station_updates_to_dataframe(df, [comp_zero])
        self.assertEqual(updated.at[0, "Number of Active Non-Ramp Sensors"], "0")
        self.assertEqual(updated.at[0, "Sensor Station 1 ID "], "")
        self.assertEqual(updated.at[0, "Sensor Station 1 HOV ID"], "")
        self.assertEqual(updated.at[0, "Sensor Station 2 ID "], "")

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
            "Sensor Station 2: number of lanes after": "2",
        }])
        st_new = VDSStationRecord(id="3047131", fwy="80", dir="EB", district="3", county="Placer", city="Colfax", state_pm="37.0", abs_pm=138.0, latitude=39.13, longitude=-120.91, length=0.5, type="ML", lanes=2, name="Secret Town")
        comp_new = StationComparisonResult(
            project_id="300020420", route="80", direction="EB", district="3", county="Placer",
            begin_pm="35.1", end_pm="38.3", abs_begin_pm=136.6, abs_end_pm=139.8,
            existing_stations=["314000", "314001"], found_stations=[st_new], matched_stations=[],
            missing_in_sheet=[], discrepancies=[], status="EXACT_MATCH", sheet_sensor_count=2, found_sensor_count=1,
        )
        res = apply_station_updates_to_dataframe(df_dummy, [comp_new])
        self.assertEqual(res.at[0, "Sensor Station 1 ID "], "3047131")
        self.assertEqual(res.at[0, "Sensor 1 Activation Date for New Lane"], "2022-07-06")
        self.assertEqual(res.at[0, "Sensor Station 2 ID "], "")

    def test_markdown_export_without_tabulate(self):
        proj = ProjectRecord(project_id="P_MD", dist="3", county="Placer", state="CA", route="80", direction="EB", begin_pm="35.0", end_pm="39.0", abs_begin_pm=136.0, abs_end_pm=139.0)
        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path = os.path.join(tmp_dir, "report.md")
            with patch.dict("sys.modules", {"tabulate": None}):
                run_station_comparison_pipeline(
                    projects=[proj], output_report_path=md_path, dry_run=True, verbose=False, vds_meta=self.indexed_meta
                )
            with open(md_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("# Caltrans PeMS Station Comparison Report", content)
            self.assertIn("P_MD", content)


class TestMasterCSVDataIntegrity(unittest.TestCase):
    """Verify lane counts and activation dates in cleaned Master CSV."""

    def test_lane_counts_accuracy_713000217_and_twltl(self):
        df = pd.read_csv(DEFAULT_MASTER_CSV, dtype=str).fillna("")
        for pid in ("713000217", "800000609"):
            sub = df[df["Project ID"].str.strip() == pid]
            self.assertEqual(len(sub), 2)
            for _, row in sub.iterrows():
                self.assertEqual(row["dir_lanes_before"].split(".")[0], "1")
                self.assertEqual(row["dir_lanes_after"].split(".")[0], "2")
                self.assertEqual(row["total_lanes_before"].split(".")[0], "2")
                self.assertEqual(row["total_lanes_after"].split(".")[0], "4")

    def test_sensor_activation_dates_formatted_properly(self):
        df = pd.read_csv(DEFAULT_MASTER_CSV, dtype=str).fillna("")
        r3 = df[df["Project ID"].str.strip() == "300020420"]
        self.assertFalse(r3.empty)
        self.assertEqual(build_date_from_parts(r3.iloc[0]["s1_new_lane_year"], r3.iloc[0]["s1_new_lane_month"], r3.iloc[0]["s1_new_lane_day"]), datetime.date(2022, 7, 6))
        for idx, row in df.iterrows():
            val = row.get("Sensor 1 Activation Date for New Lane", "").strip()
            if val and val.isdigit() and len(val) == 5:
                self.fail(f"Found unformatted Excel serial date '{val}' at row {idx}")


class TestBackwardsAbsPMFallback(unittest.TestCase):
    """Verify projects with backwards Abs PM bounds (begin > end) fall back to California PMs."""

    def test_inverted_abs_pm_uses_ca_pm_for_geometry_and_stations(self):
        proj = ProjectRecord(
            project_id="700021080",
            dist="7",
            county="Los Angeles",
            state="CA",
            route="710",
            direction="NB",
            begin_pm="3.6",
            end_pm="6",
            abs_begin_pm=2.4,
            abs_end_pm=1.0,
            sensor_stations=["777212", "777223"],
        )
        self.assertTrue(proj.has_inverted_abs_pm)
        self.assertTrue(proj.has_ca_pm_bounds)
        self.assertFalse(proj.has_abs_pm_bounds)
        self.assertEqual(proj.effective_abs_pm_bounds, (None, None))

        ref_df = pd.DataFrame([
            {"Fwy": "710", "Dir": "N", "County": "Los Angeles", "CA PM": "4.96", "Abs PM": "0.0", "Latitude": "33.76648", "Longitude": "-118.213674"},
            {"Fwy": "710", "Dir": "N", "County": "Los Angeles", "CA PM": "5.46", "Abs PM": "0.5", "Latitude": "33.76941", "Longitude": "-118.207150"},
            {"Fwy": "710", "Dir": "N", "County": "Los Angeles", "CA PM": "5.96", "Abs PM": "1.0", "Latitude": "33.77661", "Longitude": "-118.206906"},
            {"Fwy": "710", "Dir": "N", "County": "Los Angeles", "CA PM": "7.36", "Abs PM": "2.4", "Latitude": "33.79679", "Longitude": "-118.206798"},
        ])
        coords = get_project_corridor_geometry(proj, IndexedGeoReference(ref_df))
        self.assertEqual(coords, [(33.76648, -118.213674), (33.76941, -118.20715), (33.77661, -118.206906)])

        vds_df = pd.DataFrame([
            {"ID": "777212", "Fwy": "710", "Dir": "N", "District": "7", "County": "Los Angeles", "City": "Long Beach", "State_PM": "5.324", "Abs_PM": "0.364", "Latitude": "33.7675", "Longitude": "-118.2078", "Length": "0.2", "Type": "ML", "Lanes": "3", "Name": "Anaheim"},
            {"ID": "777223", "Fwy": "710", "Dir": "N", "District": "7", "County": "Los Angeles", "City": "Long Beach", "State_PM": "5.52", "Abs_PM": "0.56", "Latitude": "33.7700", "Longitude": "-118.2070", "Length": "0.2", "Type": "ML", "Lanes": "3", "Name": "PCH"},
            {"ID": "777999", "Fwy": "710", "Dir": "N", "District": "7", "County": "Los Angeles", "City": "Long Beach", "State_PM": "7.20", "Abs_PM": "2.24", "Latitude": "33.7940", "Longitude": "-118.2068", "Length": "0.3", "Type": "ML", "Lanes": "3", "Name": "Bogus North"},
        ])
        vds_meta = IndexedVDSMetadata(vds_df)
        found = vds_meta.find_corridor_stations(
            district="7", route="710", direction="NB",
            begin_pm="3.6", end_pm="6", abs_begin_pm=2.4, abs_end_pm=1.0,
            buffer_mi=0.1, station_types=("ML",), county="Los Angeles",
        )
        self.assertEqual([s.id for s in found], ["777212", "777223"])
        comp = compare_project_stations(proj, vds_meta=vds_meta, buffer_mi=0.1, station_types=("ML",))
        self.assertEqual(comp.status, "EXACT_MATCH")
        self.assertEqual(comp.matched_stations, ["777212", "777223"])


if __name__ == "__main__":
    unittest.main()
