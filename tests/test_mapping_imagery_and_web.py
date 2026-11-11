#!/usr/bin/env python3
"""Unit tests for Folium mapping, Esri Wayback imagery slicing, HTML slice reports, and validation web dashboards."""

import datetime
import json
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import folium
import pandas as pd
import wayback_imagery.core as wayback

from pems.geo import IndexedGeoReference
from pems.imagery import (
    _make_imagery_session,
    _resolve_wayback_capture,
    calculate_corridor_curvature_bounds,
    calculate_corridor_curvature_deviation,
    get_project_imagery,
    slice_project_corridor,
)
from pems.lane_config import StationLaneConfigDetails, save_cached_station_config
from pems.mapping import (
    _compute_project_length_mi,
    add_standard_basemaps_to_map,
    build_multi_project_map,
    build_single_project_map,
    classify_station_relative_to_project,
    make_project_popup_html,
    make_project_tooltip_html,
    make_station_popup_html,
    make_station_tooltip_html,
)
from pems.projects import ProjectPeMSQueryBuilder, ProjectRecord, get_project_corridor_geometry, load_master_csv
from pems.vds import IndexedVDSMetadata, VDSStationRecord
from pems.web import generate_project_validation_web_page, generate_slices_html_report
from pems.web.context import _collect_and_fetch_station_configs


class TestProjectMapping(unittest.TestCase):
    """Tests verifying Folium interactive map generation, station layers, and hash focus JS."""

    def setUp(self):
        self.mock_ref_df = pd.DataFrame([
            {"District": "4", "County": "Solano", "CA PM": "11.4", "Abs PM": "38.9", "Latitude": "38.201", "Longitude": "-122.156", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.0", "Abs PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Route": "80", "Direction": "E"},
            {"District": "4", "County": "Solano", "CA PM": "12.8", "Abs PM": "40.3", "Latitude": "38.216", "Longitude": "-122.138", "Route": "80", "Direction": "E"},
        ])
        self.mock_idx = IndexedGeoReference(self.mock_ref_df)
        self.mock_project = ProjectRecord(
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
            raw_row={"Project Description": "I-80 Managed Lanes expansion", "dir_lanes_before": "3", "dir_lanes_after": "4"},
        )
        self.mock_vds_df = pd.DataFrame([
            {"ID": "410756", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "12.0", "Abs_PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Length": "0.5", "Type": "ML", "Lanes": "4", "Name": "WEST TEXAS ST"},
            {"ID": "429041", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "12.5", "Abs_PM": "40.0", "Latitude": "38.212", "Longitude": "-122.142", "Length": "0.4", "Type": "HV", "Lanes": "1", "Name": "TRAVIS BLVD HOV"},
            {"ID": "410999", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "15.0", "Abs_PM": "42.5", "Latitude": "38.230", "Longitude": "-122.120", "Length": "0.6", "Type": "ML", "Lanes": "4", "Name": "OUT OF BOUNDS DETECTOR"},
        ])
        self.mock_vds_meta = IndexedVDSMetadata(self.mock_vds_df)

    def test_get_project_corridor_geometry_reference_and_fallback(self):
        coords = get_project_corridor_geometry(self.mock_project, self.mock_idx)
        self.assertEqual(len(coords), 3)
        self.assertAlmostEqual(coords[0][0], 38.201, places=3)
        self.assertAlmostEqual(coords[-1][0], 38.216, places=3)

        orphan = ProjectRecord(
            project_id="999", dist="1", county="Mendocino", state="CA", route="999", direction="NB",
            begin_pm="1.0", end_pm="2.0", start_coords=(39.1, -123.1), end_coords=(39.2, -123.2),
        )
        self.assertEqual(get_project_corridor_geometry(orphan, self.mock_idx), [(39.1, -123.1), (39.2, -123.2)])

    def test_station_classification_tooltips_and_popups(self):
        st_in = self.mock_vds_meta.by_id["410756"]
        is_in, note, delta = classify_station_relative_to_project(st_in, self.mock_project)
        self.assertTrue(is_in)
        self.assertEqual(delta, 0.0)
        self.assertIn("PM 38.90 - 40.30", note)

        st_out = self.mock_vds_meta.by_id["410999"]
        is_in_out, note_out, delta_out = classify_station_relative_to_project(st_out, self.mock_project)
        self.assertFalse(is_in_out)
        self.assertAlmostEqual(delta_out, 2.2, places=2)
        self.assertIn("downstream", note_out)

        tt_html = make_station_tooltip_html(st_in, True, "PM 38.9 - 40.3", True)
        self.assertIn("✓ IN PROJECT BOUNDS", tt_html)
        self.assertIn("Station ID:</strong> 410756", tt_html)
        self.assertIn("Listed in Master CSV (Verified)", tt_html)

        tt_oob = make_station_tooltip_html(st_out, False, "2.2 mi downstream", False)
        self.assertIn("⚠ OUTSIDE BOUNDS (2.2 mi downstream)", tt_oob)
        self.assertIn("Not listed in Master CSV", tt_oob)

        pop_html = make_station_popup_html(st_in, self.mock_project, True, "In bounds", True)
        self.assertIn("Station 410756 · ML (Mainline)", pop_html)
        self.assertIn("Corridor Context (PID 418000244)", pop_html)
        self.assertIn("AADT Export", pop_html)
        self.assertIn("Flow Export", pop_html)

    def test_project_popup_and_tooltip_monospace_formatting(self):
        rec = ProjectRecord(
            project_id="1200020279", dist="12", county="Orange", state="CA", route="5", direction="NB",
            begin_pm="6.2", end_pm="8.6999999999999993", abs_begin_pm=78.400000000000006, abs_end_pm=80.900000000000006,
            sensor_stations=["1204409", "1204395", "1210063"], raw_row={"area_type": "suburban"},
        )
        builder = ProjectPeMSQueryBuilder(rec)
        popup = make_project_popup_html(rec, builder, length_mi=2.5)
        tooltip = make_project_tooltip_html(rec, length_mi=2.5)
        self.assertIn("Andale Mono", popup)
        self.assertIn("Andale Mono", tooltip)
        self.assertIn("PM 6.2 &rarr; 8.7 (2.50 mi)", popup)
        self.assertIn("78.4 &rarr; 80.9", popup)
        self.assertNotIn("8.6999999999999993", popup)
        self.assertNotIn("8.6999999999999993", tooltip)
        self.assertIn("Area: Suburban", popup)
        for sid in ("1204409", "1204395", "1210063"):
            self.assertIn(f"station_id={sid}", popup)

    def test_build_single_and_multi_project_maps(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch("pems.mapping.get_indexed_geo_ref", return_value=self.mock_idx):
            m = build_single_project_map(
                "418000244",
                projects=[self.mock_project],
                vds_metadata=self.mock_vds_meta,
                station_buffer_mi=3.0,
                cache_dir=tmpdir,
            )
            html = m.get_root().render()
            for token in (
                "PID 418000244",
                "Route 80 EB",
                "Solano County",
                "Stations: Mainline (ML)",
                "Stations: Carpool/HOV (HV)",
                "PeMS Ground-Truth Checker",
                "pems-inbounds",
                "pems-oob",
                "pems-in-sheet",
                "centerFromHash",
                "clearFocusMarkers",
                "_currentFocusMarkers",
                "_hasDismissClick",
            ):
                self.assertIn(token, html)
            self.assertNotIn("Slice Focus Center", html)

            m_no = build_single_project_map("418000244", projects=[self.mock_project], include_stations=False)
            html_no = m_no.get_root().render()
            self.assertNotIn("PeMS Ground-Truth Checker", html_no)
            self.assertNotIn("Stations: Mainline", html_no)

            m_all = build_multi_project_map(projects=[self.mock_project], include_stations=True, vds_metadata=self.mock_vds_meta)
            html_all = m_all.get_root().render()
            self.assertIn("District 4 (1 projects)", html_all)
            self.assertIn("Andale Mono", html_all)
            self.assertIn("Stations: Mainline (ML)", html_all)

    def test_add_standard_basemaps_with_and_without_carto_key(self):
        m = folium.Map(tiles=None)
        add_standard_basemaps_to_map(m)
        folium.LayerControl().add_to(m)
        html = m.get_root().render()
        self.assertIn("Esri Street Map (Default)", html)
        self.assertIn("USGS Topo", html)
        self.assertIn("server.arcgisonline.com", html)
        self.assertNotIn("basemaps.cartocdn.com", html)

        with patch.dict(os.environ, {"CARTO_API_KEY": "test_carto_key_12345"}):
            m2 = folium.Map(tiles=None)
            add_standard_basemaps_to_map(m2)
            folium.LayerControl().add_to(m2)
            html2 = m2.get_root().render()
            self.assertIn("CartoDB Positron (Authenticated)", html2)
            self.assertIn("test_carto_key_12345", html2)


class TestImageryAndSlicesReports(unittest.TestCase):
    """Tests for Wayback session helpers, curvature slicing, and utilitarian slice HTML reports."""

    def test_make_imagery_session_and_wayback_capture_fallback(self):
        # _make_imagery_session must not crash even though wayback._make_session() takes 0 args
        sess = _make_imagery_session(workers=8)
        self.assertIsNotNone(sess)

        # Verify wayback._pick_by_date still selects closest prior release as expected
        rel1 = wayback.Release(1, datetime.date(2016, 5, 1), "2016-05-01", "tpl", None)
        rel2 = wayback.Release(2, datetime.date(2018, 9, 12), "2018-09-12", "tpl", None)
        cap1 = wayback.CaptureInfo(rel1, datetime.date(2016, 5, 1), 0.30, 1.0, "Esri")
        cap2 = wayback.CaptureInfo(rel2, datetime.date(2018, 9, 12), 0.60, 1.0, "Maxar")
        picked = wayback._pick_by_date([cap1, cap2], lambda c: c.capture_date, datetime.date(2019, 1, 1), "before", "test")
        self.assertEqual(picked.release.release_id, 2)

        # Verify _resolve_wayback_capture degrades gracefully on TypeError / ValueError
        mock_wb = MagicMock()
        mock_wb.find_release_by_capture.side_effect = [TypeError("unexpected kwarg"), ValueError("bad mode"), cap1]
        res = _resolve_wayback_capture(mock_wb, datetime.date(2019, 1, 1), -122.0, 37.5, 19, "before", sess, 16, True, 5.0)
        self.assertEqual(res.release.release_id, 1)
        self.assertEqual(mock_wb.find_release_by_capture.call_count, 3)

    def test_get_project_imagery_mocked_and_invalid_pid(self):
        with self.assertRaises(ValueError):
            get_project_imagery("9999999999999_invalid")

        with tempfile.TemporaryDirectory() as tmpdir, patch("wayback_imagery.core.generate_image") as mock_gen:
            mock_gen.return_value = {"path": "/mock/path.png", "capture_date": "2018-09-06", "release_date": "2019-06-26", "zoom": 17}
            res = get_project_imagery("313000020", period="both", output_dir=tmpdir, verbose=False)
            self.assertIn("before", res["imagery"])
            self.assertIn("after", res["imagery"])
            self.assertEqual(res["project_id"], "313000020")
            self.assertEqual(mock_gen.call_count, 2)

    def test_slice_corridor_and_curvature_deviation(self):
        p0, p1 = (37.0, -122.0), (37.0, -121.9)
        self.assertAlmostEqual(calculate_corridor_curvature_deviation(p0, p1, [p0, (37.0, -121.95), p1]), 0.0, places=3)
        dev_curved = calculate_corridor_curvature_deviation(p0, p1, [p0, (37.001, -121.95), p1])
        self.assertGreater(dev_curved, 100.0)
        self.assertLess(dev_curved, 120.0)

        _, max_d, max_abs, shift, hspan = calculate_corridor_curvature_bounds(p0, p1, [p0, (37.001, -121.95), p1])
        self.assertAlmostEqual(max_abs, dev_curved, places=4)
        self.assertAlmostEqual(shift, max_d / 2.0, places=4)
        self.assertAlmostEqual(hspan, max_d / 2.0, places=4)

        projects = load_master_csv()
        p = next(pr for pr in projects if pr.project_id == "313000020")
        slices = slice_project_corridor(p, slice_mi=0.4)
        self.assertGreaterEqual(len(slices), 3)
        self.assertEqual(slices[0]["index"], 1)
        self.assertGreater(slices[0]["length_mi"], 0)

    def test_slices_html_report_utilitarian_style_and_links(self):
        proj = ProjectRecord(
            project_id="414000032", dist="4", county="San Mateo", state="CA", route="84", direction="NB",
            begin_pm="25.30", end_pm="25.70", abs_begin_pm=25.30, abs_end_pm=25.70,
            construction_begin_date="2019-01-01", open_to_traffic_date="2024-12-31",
            project_description="Widen Woodside Road to six lanes.",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            b_img, a_img, s_img = [os.path.join(tmpdir, f"slice_01_{k}.png") for k in ("before", "after", "stacked")]
            for p in (b_img, a_img, s_img):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("dummy")
            sample_slice = {
                "index": 1, "start_pm": 25.30, "end_pm": 25.50, "length_mi": 0.20, "bearing": 90.0,
                "before_image": b_img, "after_image": a_img, "stacked_image": s_img,
                "before_capture_date": "2017-08-28", "after_capture_date": "2025-07-30",
                "zoom": 19, "start_coords": (37.478, -122.215), "end_coords": (37.480, -122.210),
                "midpoint": (37.479, -122.2125), "bbox": [-122.216, 37.477, -122.209, 37.481],
                "width": 1600, "height": 1100, "sensors": [{"id": "400001", "type": "ML", "pm": 25.40, "lanes": "3"}],
            }
            html_out = generate_slices_html_report(
                project=proj,
                slices_data=[sample_slice],
                before_meta={"release_id": 20399, "capture_date": "2017-08-28"},
                after_meta={"release_id": 13192, "capture_date": "2025-07-30"},
                output_dir=tmpdir,
                map_file="project_map.html",
            )
            with open(html_out, "r", encoding="utf-8") as fh:
                content = fh.read()

            for req in (
                "Widen Woodside Road to six lanes.",
                "Andale Mono",
                "background: #ffffff",
                "color: #000000",
                'class="project-info-table"',
                'class="segment-section"',
                "border-radius: 0",
                "earth.google.com/web/@37.479000,-122.212500",
                "project_map.html#37.479000,-122.212500,17",
                "google.com/maps/@37.479000,-122.212500",
                'href="project_map.html"',
                "Full Project Map with Stations",
            ):
                self.assertIn(req, content)
            for forbidden in ("info-grid", "info-card", "segment-card", "badge-overlay", "badge-left", "badge-right", "global-controls", "btn-all-slider", 'class="actions"'):
                self.assertNotIn(forbidden, content)


class TestProjectWebDashboard(unittest.TestCase):
    """Tests for the offline single-page research validation web dashboard."""

    def test_generate_project_validation_web_page_with_slices_and_midpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            imagery_base = os.path.join(tmpdir, "imagery")
            slice_dir = os.path.join(imagery_base, "PID_414000032_Route84_N_PM25.3-25.7")
            os.makedirs(slice_dir, exist_ok=True)
            mock_meta = {
                "project_id": "414000032", "route": "84", "direction": "NB", "county": "San Mateo", "postmiles": ["25.3", "25.7"],
                "before_flight": {"capture_date": "2017-08-28", "release_id": "20399", "provider": "Esri", "resolution_m": "0.3"},
                "after_flight": {"capture_date": "2025-07-30", "release_id": "13192", "provider": "Maxar", "resolution_m": "0.3"},
                "slices": [
                    {"index": 1, "start_pm": 25.3, "end_pm": 25.5, "length_mi": 0.2},
                    {"index": 2, "start_pm": 25.5, "end_pm": 25.7, "length_mi": 0.2},
                ],
            }
            with open(os.path.join(slice_dir, "metadata.json"), "w", encoding="utf-8") as fh:
                json.dump(mock_meta, fh)
            for idx in (1, 2):
                for kind in ("stacked", "before", "after"):
                    open(os.path.join(slice_dir, f"slice_{idx:02d}_{kind}.png"), "w", encoding="utf-8").close()

            web_out = os.path.join(tmpdir, "web_out")
            out_html = generate_project_validation_web_page(
                project_id="414000032",
                output_dir=web_out,
                fetch_online=False,
                cache_dir=os.path.join(tmpdir, "cache"),
                imagery_base_dir=imagery_base,
            )
            map_path = os.path.join(web_out, "project_map.html")
            self.assertTrue(os.path.exists(out_html))
            self.assertTrue(os.path.exists(map_path))

            with open(out_html, "r", encoding="utf-8") as fh:
                content = fh.read()
            for req in (
                "Andale Mono",
                "[PROJECT VALIDATION DASHBOARD]",
                "GROUND TRUTH VALIDATION MATRIX",
                "CORRIDOR DETECTOR STATIONS AUDIT",
                "INTERACTIVE CORRIDOR MAP &amp; DETECTOR OVERLAY",
                "HISTORICAL SATELLITE IMAGERY SLICES",
                "MASTER SPREADSHEET RECORD ATTRIBUTES",
                "focusMap(",
                "Google Earth",
                "Google Maps",
                "slider-tag-before",
                "slider-tag-after",
                "inset(0 50% 0 0)",
                "37.484986,-122.215335",
                "37.487359,-122.213706",
            ):
                self.assertIn(req, content)

            emoji_pattern = re.compile(r"[\U00010000-\U0010ffff]|[\u2600-\u26ff]|[\u2700-\u27bf]|&#x1F")
            self.assertIsNone(emoji_pattern.search(content))

            ge_matches = re.findall(r'href="(https://earth\.google\.com/web/@[^"]+)"', content)
            self.assertGreaterEqual(len(ge_matches), 3)
            self.assertNotEqual(ge_matches[-2], ge_matches[-1])

            with open(map_path, "r", encoding="utf-8") as mf:
                map_content = mf.read()
            self.assertIn("Esri Street Map (Default)", map_content)
            self.assertIn("clearFocusMarkers", map_content)
            self.assertIn("_currentFocusMarkers", map_content)
            self.assertNotIn("Slice Focus Center", map_content)

    def test_web_page_contains_hover_tooltips_and_isolated_cache(self):
        st = StationLaneConfigDetails(
            station_id="410756", station_name="EB 12 OC", district="4", route="80", direction="E", county="Solano",
            station_type="ML", ca_pm="11.86", abs_pm=39.33,
            lane_table=[{"Lane": "1", "Type": "Mainline"}],
            change_log=[{"Date": "04/09/2016", "Status": "Active", "Lanes": "12345"}],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "cache")
            imagery_dir = os.path.join(tmpdir, "imagery")
            save_cached_station_config(st, cache_dir=cache_dir)

            out_html = generate_project_validation_web_page(
                project_id="418000244",
                output_dir=os.path.join(tmpdir, "web"),
                fetch_online=False,
                cache_dir=cache_dir,
                imagery_base_dir=imagery_dir,
            )
            with open(out_html, "r", encoding="utf-8") as fh:
                content = fh.read()

            self.assertIn('id="global-station-tooltip"', content)
            self.assertIn('id="station-tooltips-data"', content)
            self.assertIn('class="st-hover"', content)
            self.assertIn('data-station-id="410756"', content)
            m = re.search(r'<script id="station-tooltips-data" type="application/json">\s*(\{.*?\})\s*</script>', content, re.DOTALL)
            self.assertIsNotNone(m)
            tooltips = json.loads(m.group(1))
            self.assertIn("410756", tooltips)
            self.assertIn("STATION 410756", tooltips["410756"])
            self.assertIn("LANE COUNTS OVER TIME", tooltips["410756"])

    def test_web_live_scrape_restricts_to_inbounds_and_spreadsheet(self):
        proj = ProjectRecord(
            project_id="418000244", dist="4", county="Solano", state="CA", route="80", direction="EB",
            begin_pm="11.4", end_pm="12.8", abs_begin_pm=38.9, abs_end_pm=40.3,
            sensor_stations=["410999"],
        )
        vds_df = pd.DataFrame([
            {"ID": "410756", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "12.0", "Abs_PM": "39.5", "Latitude": "38.208", "Longitude": "-122.147", "Length": "0.5", "Type": "ML", "Lanes": "4", "Name": "IN BOUNDS"},
            {"ID": "410888", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "14.5", "Abs_PM": "42.0", "Latitude": "38.228", "Longitude": "-122.125", "Length": "0.5", "Type": "OR", "Lanes": "1", "Name": "OOB RAMP"},
            {"ID": "410999", "Fwy": "80", "Dir": "E", "District": "4", "County": "Solano", "City": "Fairfield", "State_PM": "15.0", "Abs_PM": "42.5", "Latitude": "38.230", "Longitude": "-122.120", "Length": "0.6", "Type": "ML", "Lanes": "4", "Name": "SHEET OOB"},
        ])
        vds_meta = IndexedVDSMetadata(vds_df)

        with tempfile.TemporaryDirectory() as cache_dir, patch("pems.web.context.fetch_station_configs_batch") as mock_batch:
            mock_batch.side_effect = lambda sids, **kwargs: {
                sid: StationLaneConfigDetails(station_id=sid, station_name=f"Live {sid}", lane_table=[{"Lane": "1", "Type": "Mainline"}])
                for sid in sids
            }
            env_patch = {"PEMS_USERNAME": "u", "PEMS_PASSWORD": "p"}
            with patch.dict(os.environ, env_patch):
                os.environ.pop("PEMS_OFFLINE", None)
                dirs, all_st, cfgs = _collect_and_fetch_station_configs(
                    matched=[proj], vds_meta=vds_meta, station_buffer_mi=3.0, fetch_online=True, cache_dir=cache_dir
                )

            # All 3 stations remain listed in the dashboard table & dict
            self.assertEqual(set(all_st.keys()), {"410756", "410888", "410999"})
            self.assertEqual(len(dirs["dir-1"]), 3)
            # Only in-bounds (410756) + spreadsheet sensor (410999) were live-scraped; buffer ramp 410888 used VDS fallback
            scraped_ids = set(mock_batch.call_args.args[0])
            self.assertEqual(scraped_ids, {"410756", "410999"})
            self.assertEqual(cfgs["410888"].station_name, "OOB RAMP")
            self.assertEqual(cfgs["410756"].station_name, "Live 410756")

    def test_web_flags_backwards_abs_pm_and_uses_ca_pm(self):
        proj = ProjectRecord(
            project_id="700021080", dist="7", county="Los Angeles", state="CA", route="710", direction="NB",
            begin_pm="3.6", end_pm="6", abs_begin_pm=2.4, abs_end_pm=1.0, sensor_stations=["777212"],
        )
        st = VDSStationRecord(
            id="777212", fwy="710", dir="N", district="7", county="Los Angeles", city="Long Beach",
            state_pm="5.324", abs_pm=0.364, latitude=33.7675, longitude=-118.2078, length=0.2, type="ML", lanes=3, name="Anaheim",
        )
        in_bounds, reason, _ = classify_station_relative_to_project(st, proj)
        self.assertTrue(in_bounds, msg=reason)
        self.assertAlmostEqual(_compute_project_length_mi(proj), 2.4, places=2)

        builder = ProjectPeMSQueryBuilder(proj)
        popup = make_project_popup_html(proj, builder, length_mi=2.4)
        self.assertIn("[BACKWARDS - USING CA PM]", popup)

        vds_df = pd.DataFrame([
            {"ID": "777212", "Fwy": "710", "Dir": "N", "District": "7", "County": "Los Angeles", "City": "Long Beach", "State_PM": "5.324", "Abs_PM": "0.364", "Latitude": "33.7675", "Longitude": "-118.2078", "Length": "0.2", "Type": "ML", "Lanes": "3", "Name": "Anaheim"},
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            out_html = generate_project_validation_web_page(
                project_id="700021080",
                projects=[proj],
                output_dir=os.path.join(tmpdir, "web"),
                fetch_online=False,
                cache_dir=os.path.join(tmpdir, "cache"),
                imagery_base_dir=os.path.join(tmpdir, "imagery"),
                vds_meta=IndexedVDSMetadata(vds_df),
            )
            with open(out_html, "r", encoding="utf-8") as fh:
                content = fh.read()
            import html as html_lib
            unescaped = html_lib.unescape(content)
            self.assertIn("[DATA WARNING: BACKWARDS ABSOLUTE POSTMILES]", unescaped)
            self.assertIn("2.40 > 1.00", unescaped)
            self.assertIn("[FLAG: Backwards Abs PM (2.40 > 1.00); using CA PM", unescaped)
            self.assertIn("[BACKWARDS - USING CA PM]", unescaped)
            self.assertIn("Los Angeles PM 3.6", unescaped)
            emoji_pattern = re.compile(r"[\U00010000-\U0010ffff]|[\u2600-\u26ff]|[\u2700-\u27bf]|&#x1F")
            self.assertIsNone(emoji_pattern.search(content))


if __name__ == "__main__":
    unittest.main()
