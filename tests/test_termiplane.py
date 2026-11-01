import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from threading import Event
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import termiplane as app


PLANE = {
    "hex": "461e1a", "flight": "FIN40T  ", "r": "OH-LKK", "t": "E190",
    "lat": 60.033487, "lon": 20.81507, "alt_baro": 33450,
    "gs": 455.5, "track": 262.94, "baro_rate": -2944,
    "seen": 1, "seen_pos": 2, "squawk": "6020",
}

ROUTE = {"response": {"flightroute": {
    "callsign": "FIN40T", "callsign_icao": "FIN40T", "callsign_iata": "AY40T",
    "origin": {"iata_code": "HEL", "icao_code": "EFHK", "name": "Helsinki Vantaa Airport"},
    "destination": {"iata_code": "ARN", "icao_code": "ESSA", "name": "Stockholm-Arlanda Airport"},
}}}


class RouteTests(unittest.TestCase):
    def setUp(self):
        app.fetch_route.cache_clear()

    def test_fetches_and_caches_airports_by_callsign(self):
        with patch("termiplane.urlopen", return_value=io.BytesIO(json.dumps(ROUTE).encode())) as request:
            self.assertEqual(app.fetch_route("FIN40T"),
                             ("HEL/EFHK", "ARN/ESSA"))
            self.assertEqual(app.fetch_route("FIN40T"),
                             ("HEL/EFHK", "ARN/ESSA"))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(
            request.call_args.args[0].full_url, "https://api.adsbdb.com/v0/callsign/FIN40T")

    def test_unknown_malformed_and_mismatched_routes_are_unavailable(self):
        for payload in [{"response": "unknown callsign"}, [], {},
                        {"response": {"flightroute": {"callsign": "BAW123"}}}]:
            app.fetch_route.cache_clear()
            with self.subTest(payload=payload), patch(
                "termiplane.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
            ):
                self.assertEqual(app.fetch_route("FIN40T"), ("--", "--"))

    def test_route_network_failure_is_optional(self):
        with patch("termiplane.urlopen", side_effect=URLError("offline")):
            self.assertEqual(app.fetch_route("FIN40T"), ("--", "--"))

    def test_unknown_callsign_http_error_is_unavailable(self):
        error = HTTPError("url", 404, "Not found", {}, None)
        with patch("termiplane.urlopen", side_effect=error):
            self.assertEqual(app.fetch_route("FIN40T"), ("--", "--"))

    def test_airport_codes_are_validated_and_support_icao_only(self):
        payload = json.loads(json.dumps(ROUTE))
        payload["response"]["flightroute"]["origin"] = {"icao_code": "EFHK"}
        payload["response"]["flightroute"]["destination"] = {
            "iata_code": "A\x1b[2J", "icao_code": "../bad"}
        with patch("termiplane.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(app.fetch_route("FIN40T"), ("EFHK", "--"))


class NearestAirportTests(unittest.TestCase):
    def setUp(self):
        app.nearest_airport.cache_clear()
        self.addCleanup(app.nearest_airport.cache_clear)

    def test_distance_and_bearing_point_from_aircraft_to_airport(self):
        airports = [["EAST", "East Airport", 0, 1],
                    ["WEST", "West Airport", 0, -2]]
        with patch("termiplane.maps.layer", return_value=airports):
            code, name, distance, bearing = app.nearest_airport(0, 0)
        self.assertEqual((code, name), ("EAST", "East Airport"))
        self.assertAlmostEqual(distance, 111.195, places=2)
        self.assertAlmostEqual(bearing, 90, places=2)

    def test_nearest_airport_across_dateline(self):
        airports = [["NEAR", "Across date line", 0, -179.9],
                    ["FAR", "Far Airport", 0, 175]]
        with patch("termiplane.maps.layer", return_value=airports):
            result = app.nearest_airport(0, 179.9)
        self.assertEqual(result[0], "NEAR")
        self.assertAlmostEqual(result[2], 22.239, places=2)
        self.assertAlmostEqual(result[3], 90, places=2)

    def test_invalid_position_and_empty_database_are_unavailable(self):
        for lat, lon in [(None, 0), (91, 0), (0, -181), (float("nan"), 0)]:
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(app.nearest_airport(lat, lon))
        with patch("termiplane.maps.layer", return_value=[]):
            self.assertIsNone(app.nearest_airport(0, 0))

    def test_zero_distance_has_no_misleading_direction(self):
        with patch("termiplane.maps.layer", return_value=[["HERE", "Here Airport", 10, 20]]):
            result = app.nearest_airport(10, 20)
        self.assertEqual(result[2], 0)
        self.assertIsNone(result[3])


class Screen:
    def __init__(self, height=24, width=80, keys=()):
        self.height, self.width = height, width
        self.keys = iter(keys)
        self.frames = []
        self.writes = []

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows = [[" "] * self.width for _ in range(self.height)]

    def addnstr(self, y, x, text, count, style=0):
        text = text[:count]
        assert 0 <= y < self.height and 0 <= x < self.width
        assert x + len(text) < self.width
        self.rows[y][x:x + len(text)] = text
        self.writes.append((text, style))

    def refresh(self):
        self.frames.append("\n".join("".join(row).rstrip()
                           for row in self.rows))

    def timeout(self, milliseconds):
        self.wait = milliseconds

    def keypad(self, enabled):
        pass

    def bkgd(self, character, style):
        self.background = style

    def getch(self):
        key = next(self.keys)
        return key() if callable(key) else key


class LookupTests(unittest.TestCase):
    def test_identifier_detection_and_override(self):
        for identifier, kind, value in [
            (" 461E1A ", "hex", "461E1A"),
            ("fin40t", "callsign", "FIN40T"),
            ("OH-LKK", "registration", "OH-LKK"),
            ("N123AB", "registration", "N123AB"),
        ]:
            with self.subTest(identifier=identifier):
                self.assertEqual(app.identify(
                    identifier, "auto"), (kind, value))
        self.assertEqual(app.identify("ABC123", "callsign"),
                         ("callsign", "ABC123"))

    def test_rejects_invalid_input(self):
        for identifier in ["", "../all", "A,B", "A\x1b[2J", "A" * 13]:
            with self.subTest(identifier=identifier), self.assertRaises(argparse.ArgumentTypeError):
                app.identify(identifier, "auto")
        with self.assertRaises(argparse.ArgumentTypeError):
            app.identify("FIN40T", "hex")

    def test_fetch_reads_real_response_shape(self):
        body = {"ac": [PLANE], "now": 1000000, "msg": "No error"}
        with patch("termiplane.urlopen", return_value=io.BytesIO(json.dumps(body).encode())) as request:
            plane, timestamp = app.fetch("callsign", "FIN40T")
        self.assertEqual(plane, PLANE)
        self.assertEqual(timestamp, 1000)
        self.assertEqual(request.call_args.args[0].full_url,
                         "https://opendata.adsb.fi/api/v2/callsign/FIN40T")

    def test_hex_lookup_cannot_switch_to_another_aircraft(self):
        body = {"ac": [dict(PLANE, hex="abcdef")]}
        with patch("termiplane.urlopen", return_value=io.BytesIO(json.dumps(body).encode())), self.assertRaises(ValueError):
            app.fetch("hex", "461E1A")

    def test_no_match_is_not_an_error(self):
        with patch("termiplane.urlopen", return_value=io.BytesIO(b'{"ac": []}')):
            self.assertEqual(app.fetch("hex", "461E1A"), (None, None))

    def test_invalid_and_ambiguous_responses_are_rejected(self):
        for body in [[], {}, {"ac": None}, {"ac": [None]},
                     {"ac": [{"hex": "bad"}]}, {"ac": [PLANE, PLANE]},
                     {"ac": [], "msg": "Backend failed"}]:
            with self.subTest(body=body), patch(
                "termiplane.urlopen", return_value=io.BytesIO(json.dumps(body).encode())
            ), self.assertRaises(ValueError):
                app.fetch("callsign", "FIN40T")


class DashboardTests(unittest.TestCase):
    def test_airport_locations_match_either_code_and_ignore_unknown(self):
        app.airport_location.cache_clear()
        self.addCleanup(app.airport_location.cache_clear)
        with patch("termiplane.maps.layer", return_value=[["HEL/EFHK", "Helsinki", 60, 25]]):
            self.assertEqual(app.airport_location("EFHK"), (60, 25))
            self.assertEqual(app.airport_location("HEL/EFHK"), (60, 25))
            self.assertIsNone(app.airport_location("--"))
            self.assertIsNone(app.airport_location("UNKNOWN"))

    def test_overview_fits_aircraft_and_airports_across_dateline(self):
        airports = [("D", "DEP", 12, 179), ("A", "ARR", -10, -178),
                    ("N", "NEAR", 2, 178)]
        with patch("termiplane.land_mask", wraps=app.land_mask) as mask:
            output = app.position_map(0, 180, False, width=120, height=35, smooth=True,
                                      airports=airports, overview=True)
        body = "\n".join(output[1:-1])
        for label in ("D DEP", "A ARR", "X"):
            self.assertIn(label, body)
        self.assertNotIn("N NEAR", body)
        self.assertIn("Overview map", output[0])
        self.assertIn("km", output[-1])
        # The short date-line crossing must not be expanded into a world view.
        bounds = mask.call_args.args
        self.assertLess(bounds[3] - bounds[2], 60)
        self.assertEqual(len(output), 35)
        self.assertTrue(all(len(line) == 120 for line in output))

    def test_shared_airport_preserves_roles_and_aircraft(self):
        output = app.position_map(60, 25, False, width=120, height=35, smooth=True, zoom=128,
                                  airports=[("D", "HEL", 60, 25), ("N", "HEL", 60, 25)])
        body = "\n".join(output[1:-1])
        self.assertIn("D/N:HEL", body)
        self.assertEqual(body.count("X"), 1)

    def test_shared_airport_does_not_repeat_its_first_role(self):
        output = app.position_map(0, 0, False, width=120, height=35, smooth=True, zoom=128,
                                  airports=[("D", "HEL", 0, 0), ("N", "HEL", 0, 0)])
        body = "\n".join(output[1:-1])
        self.assertIn("+ D/N:HEL", body)
        self.assertNotIn("D D/N:HEL", body)

    def test_render_passes_route_and_nearest_airports_to_map(self):
        with patch("termiplane.position_map", return_value=[]) as draw:
            app.render("TEST", PLANE, 1000, "Tracking", 5, now=1001,
                       route=("HEL/EFHK", "ARN/ESSA"), overview=True)
        airports = draw.call_args.kwargs["airports"]
        self.assertEqual([airport[0] for airport in airports], ["D", "A"])
        self.assertTrue(draw.call_args.kwargs["overview"])
        with patch("termiplane.position_map", return_value=[]) as draw:
            app.render("TEST", PLANE, 1000, "Tracking", 5, now=1001, zoom=128)
        self.assertEqual(
            [airport[0] for airport in draw.call_args.kwargs["airports"]], ["D", "A", "N"])

    def test_overview_handles_missing_airports_and_polar_or_missing_position(self):
        for latitude, longitude in ((90, 180), (-90, -180), (None, None)):
            with self.subTest(latitude=latitude):
                output = app.position_map(latitude, longitude, False, width=80, height=24,
                                          smooth=True, overview=True)
                self.assertEqual(len(output), 24)
                self.assertTrue(all(len(line) == 80 for line in output))
                self.assertIn(
                    "Map unavailable" if latitude is None else "Overview map", output[0])

    def test_nearest_airport_appears_in_main_panel(self):
        with patch("termiplane.nearest_airport", return_value=("HEL/EFHK", "Helsinki Vantaa Airport", 12.3, 45)):
            output = app.render("TEST", PLANE, 1000, "Tracking", 5, now=1005,
                                width=80, height=24, framed=True)
        for value in ("Nearest", "HEL/EFHK", "Helsinki Vantaa Airport", "12.3 km", "NE", "45degT"):
            self.assertIn(value, output)
        self.assertLessEqual(len(output.splitlines()), 24)
        self.assertTrue(all(len(row) <= 80 for row in output.splitlines()))

    def test_altitude_trend_handles_ground_level_and_missing_rate(self):
        for rate, expected in [(101, "Climbing"), (100, "Level"), (-100, "Level"),
                               (-101, "Descending"), (None, "--"), (float("nan"), "--")]:
            with self.subTest(rate=rate):
                self.assertEqual(app.altitude_trend(
                    {"baro_rate": rate}), expected)
        self.assertEqual(app.altitude_trend(
            {"alt_baro": "ground", "baro_rate": 500}), "Ground")

    def test_main_panel_shows_trend_selected_altitude_and_mach(self):
        plane = dict(PLANE, mach=0.784, nav_altitude_mcp=38016)
        output = app.render("TEST", plane, 1000, "Tracking", 5, now=1005,
                            width=80, height=24, framed=True)
        for value in ("Trend", "Descending", "Selected", "38,016 ft", "Mach", "0.784"):
            self.assertIn(value, output)
        fallback = app.render("TEST", dict(plane, nav_altitude_mcp=None, nav_altitude_fms=35000),
                              1000, "Tracking", 5, now=1005)
        self.assertIn("35,000 ft", fallback)

    def test_expanded_details_show_optional_fields_with_correct_units(self):
        plane = dict(PLANE, ias=276, tas=458, true_heading=271.36, mag_heading=262.44,
                     wd=355, ws=67, oat=-48, nav_modes=["autopilot", "vnav", "lnav"])
        output = app.render("TEST", plane, 1000, "Tracking", 5, now=1005,
                            width=120, height=40, framed=True, expanded=True)
        for value in ("IAS", "276.0 kt", "TAS", "458.0 kt", "True hdg", "271.4 deg",
                      "Mag hdg", "262.4 deg", "Wind est", "355deg 67kt", "OAT est",
                      "-48 C", "Autopilot", "On", "VNAV LNAV"):
            self.assertIn(value, output)
        collapsed = app.render("TEST", plane, 1000,
                               "Tracking", 5, now=1005, framed=True)
        self.assertNotIn("Wind est", collapsed)

    def test_missing_expanded_values_and_untrusted_modes_are_safe(self):
        output = app.render("TEST", dict(PLANE, nav_modes={"bad": "data"}, ws=float("nan")),
                            1000, "Tracking", 5, now=1005, width=80, height=24,
                            framed=True, expanded=True)
        self.assertIn("Autopilot", output)
        self.assertIn("--", output)
        self.assertLessEqual(len(output.splitlines()), 24)
        self.assertTrue(all(len(row) <= 80 for row in output.splitlines()))
        output = app.render("TEST", dict(PLANE, nav_modes=["autopilot", "lnav\x1b[2J"]),
                            1000, "Tracking", 5, now=1005, width=120, height=40,
                            framed=True, expanded=True)
        self.assertNotIn("\x1b", output)

    def test_departure_and_arrival_appear_in_flight_panel(self):
        output = app.render("TEST", PLANE, 1000, "Tracking", 5, now=1005,
                            width=80, height=24, framed=True, route=("HEL/EFHK", "ARN/ESSA"))
        for value in ("Departure", "Arrival", "HEL/EFHK", "ARN/ESSA"):
            self.assertIn(value, output)
        self.assertLessEqual(len(output.splitlines()), 24)
        self.assertTrue(all(len(line) <= 80 for line in output.splitlines()))

    def test_detailed_map_omits_water_overlays(self):
        for lat, lon, zoom in [(44, -87, 64), (48.8566, 2.3522, 512)]:
            with self.subTest(lat=lat, lon=lon):
                output = app.position_map(lat, lon, False, width=120, height=35,
                                          smooth=True, zoom=zoom)
                body = "".join(output[1:-1])
                self.assertNotIn("~", body)
                self.assertIn("X", body)
                self.assertTrue(all(len(row) == 120 for row in output))

    def test_fine_coastlines_resolve_isle_of_wight_when_zoomed_in(self):
        coarse = app.land_mask(3, 3, -1.31, -1.29, 50.66, 50.64)
        fine = app.land_mask(3, 3, -1.31, -1.29, 50.66, 50.64, detail="10m")
        self.assertEqual(coarse[1][1], " ")
        self.assertEqual(fine[1][1], "#")

    def test_zoomed_map_adds_cities_and_country_borders(self):
        output = app.position_map(
            49, 5, False, width=120, height=35, smooth=True, zoom=32)
        body = "\n".join(output[1:-1])
        self.assertIn("Paris", body)
        self.assertIn(":", body)
        self.assertEqual(output[17][59], "X")
        self.assertTrue(all(len(line) == 120 for line in output))

    def test_world_view_keeps_labels_out_of_the_map(self):
        output = app.position_map(
            49, 5, False, width=120, height=35, smooth=True)
        self.assertNotIn("Paris", "\n".join(output[1:-1]))

    def test_city_label_at_aircraft_position_keeps_marker_visible(self):
        output = app.position_map(48.8566, 2.3522, False, width=120, height=35,
                                  smooth=True, zoom=32)
        self.assertIn("X Paris", "\n".join(output[1:-1]))

    def test_distance_scale_accounts_for_latitude_and_resolution(self):
        self.assertIn("~2000 km @ 0N", app.distance_scale(1, 0, 80))
        self.assertIn("~1000 km @ 60N", app.distance_scale(1, 60, 80))
        self.assertIn("~200 km @ 0N", app.distance_scale(0.1, 0, 80))
        self.assertIn("~200 m @ 0N", app.distance_scale(0.0001, 0, 80))
        self.assertIn("~1 km @ 0N", app.distance_scale(0.0005, 0, 80))
        self.assertIn("@ 60S", app.distance_scale(1, -60, 80))
        self.assertEqual(app.distance_scale(1, 0, 20), "")
        self.assertIn("unavailable", app.distance_scale(1, 90, 80))

    def test_map_scale_changes_with_zoom_and_preserves_dimensions(self):
        world = app.position_map(
            60, 24, False, width=82, height=24, smooth=True)
        zoomed = app.position_map(
            60, 24, False, width=82, height=24, smooth=True, zoom=8)
        for output in (world, zoomed):
            self.assertIn("km @ 60N", output[-1])
            self.assertEqual(len(output), 24)
            self.assertTrue(all(len(row) == 82 for row in output))
        self.assertNotEqual(world[-1], zoomed[-1])

    def test_trail_connects_positions_and_keeps_aircraft_marker(self):
        trail = [(0, -20, 1000), (0, 0, 1010)]
        output = app.position_map(
            0, 0, False, width=39, height=21, trail=trail)
        row = output[10]
        self.assertEqual(row[17:20], "..X")
        self.assertIn(". = trail", app.position_map(
            0, 0, False, width=100, trail=trail)[0])

    def test_trail_does_not_connect_across_dateline_or_coverage_gaps(self):
        for trail in [[(0, 170, 1000), (0, -170, 1010)],
                      [(0, -100, 1000), (0, 100, 1100)]]:
            with self.subTest(trail=trail):
                output = app.position_map(
                    0, 0, False, width=75, height=21, trail=trail)
                self.assertNotIn(".", output[10][25:50])

    def test_zoomed_trail_wraps_around_dateline(self):
        output = app.position_map(0, -179, False, width=82, height=24, smooth=True,
                                  zoom=4, trail=[(0, 179, 1000), (0, -179, 1010)])
        self.assertEqual(output[12][39:42], "..X")

    def test_actual_coastlines_include_iceland(self):
        output = app.position_map(None, None, False, width=363, height=183)
        self.assertEqual(output[1 + 26][1 + 160], "#")

    def test_inland_water_is_not_filled_as_land(self):
        self.assertEqual(app.land_mask(361, 181)[90 - 42][180 + 51], " ")

    def test_smooth_map_uses_half_cells_and_preserves_marker(self):
        output = app.position_map(
            60, 24, False, width=120, height=32, smooth=True)
        self.assertEqual(len(output), 32)
        self.assertTrue(all(len(line) == 120 for line in output))
        cells = "".join(output[1:-1])
        self.assertTrue(any(char in cells for char in "▀▄"))
        self.assertEqual(cells.count("X"), 1)
        self.assertNotIn("#", cells)

    def test_smooth_map_keeps_geographic_proportions_in_tall_window(self):
        output = app.position_map(
            0, 0, False, width=82, height=62, smooth=True)
        self.assertTrue(all(line[1:-1].isspace() for line in output[1:20]))
        self.assertIn("X", output[31])

    def test_world_map_projects_positions_and_edges(self):
        for lat, lon, row, column in [(0, 0, 5, 15), (90, -180, 0, 0),
                                      (-90, 180, 10, 30), (60, 24, 2, 17)]:
            with self.subTest(lat=lat, lon=lon):
                output = app.position_map(lat, lon, False, width=33, height=13)
                self.assertEqual(output[row + 1][column + 1], "X")
                self.assertEqual("".join(output[1:-1]).count("X"), 1)

    def test_map_scales_to_requested_dimensions(self):
        for width, height in [(80, 16), (160, 40), (200, 50)]:
            with self.subTest(width=width, height=height):
                output = app.position_map(
                    0, 0, False, width=width, height=height)
                self.assertEqual(len(output), height)
                self.assertTrue(all(len(line) == width for line in output))
                self.assertEqual(
                    output[1 + round((height - 3) / 2)][1 + round((width - 3) / 2)], "X")

    def test_zoom_centers_plane_and_wraps_dateline(self):
        for latitude, longitude in [(60, 24), (20, 179), (-30, -179), (89, 0)]:
            with self.subTest(latitude=latitude, longitude=longitude):
                output = app.position_map(latitude, longitude, False, width=82, height=24,
                                          smooth=True, zoom=4)
                self.assertIn("Follow map 4x", output[0])
                self.assertEqual(output[12][41], "X")
                self.assertTrue(all(len(line) == 82 for line in output))
        mask = app.land_mask(41, 21, west=170, east=190, north=-30, south=-50)
        self.assertTrue(any("#" in row for row in mask))

    def test_information_panel_has_aligned_groups_and_preserves_all_fields(self):
        output = app.render("TEST", PLANE, 1000, "Tracking", 5, now=1005,
                            width=100, height=35, framed=True)
        for heading in ["AIRCRAFT", "POSITION", "FLIGHT"]:
            self.assertIn(heading, output)
        for value in ["FIN40T", "OH-LKK", "461E1A", "E190", "60.03349", "20.81507",
                      "33,450 ft", "455.5 kt", "262.9 deg", "-2,944 ft/min", "6020", "7s"]:
            self.assertIn(value, output)
        self.assertTrue(all(len(line) <= 100 for line in output.splitlines()))

    def test_landmarks_are_geographically_recognizable(self):
        for lat, lon, land in [(48, 0, True), (0, -60, True), (-24, 132, True),
                               (0, -144, False), (0, 84, False)]:
            with self.subTest(lat=lat, lon=lon):
                output = app.position_map(
                    None, None, False, width=63, height=33)
                cell = output[1 + round((90 - lat) / 6)
                              ][1 + round((lon + 180) / 6)]
                self.assertEqual(cell == "#", land)

    def test_map_rejects_invalid_coordinates(self):
        for lat, lon in [(None, 0), (0, None), (91, 0), (0, -181),
                         (float("nan"), 0), ({}, 0)]:
            with self.subTest(lat=lat, lon=lon):
                self.assertIn("Map unavailable", "\n".join(
                    app.position_map(lat, lon, False)))

    def test_map_is_visible_in_standard_terminal_and_marks_stale_positions(self):
        output = app.render("TEST", PLANE, 1000, "Tracking", 5, now=1005)
        self.assertIn("World map", output)
        self.assertIn("X = aircraft", output)
        self.assertLessEqual(len(output.splitlines()), 24)
        self.assertTrue(all(len(line) <= 80 for line in output.splitlines()))
        stale = app.render("TEST", PLANE, 1000, "Tracking", 5, now=1100)
        self.assertIn("World map (STALE)", stale)

    def test_map_fills_space_and_flight_details_remain_visible_after_resizing(self):
        for width, height in [(80, 24), (160, 50), (40, 40)]:
            with self.subTest(width=width, height=height):
                output = app.render("TEST", PLANE, 1000, "Tracking", 5,
                                    now=1005, width=width, height=height)
                lines = output.splitlines()
                self.assertEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width for line in lines))
                self.assertLess(output.index("World map"),
                                output.index("Callsign"))
                self.assertIn("OH-LKK", output)
                self.assertIn("6020", output)
                self.assertIn("https://adsb.fi", output)
                map_rows = [line for line in lines if line.startswith("|")]
                self.assertTrue(all(len(line) == width for line in map_rows))
                if width >= 80:
                    self.assertGreaterEqual(len(map_rows), height // 2)

    def test_tiny_terminal_does_not_overflow(self):
        output = app.render("TEST", PLANE, 1000, "Tracking",
                            5, now=1005, width=20, height=8)
        self.assertLessEqual(len(output.splitlines()), 8)
        self.assertTrue(all(len(line) <= 20 for line in output.splitlines()))

    def test_flight_data_and_units(self):
        output = app.render("FIN40T", PLANE, 1000, "Tracking", 5, now=1005)
        for value in ["termiplane", "FIN40T", "OH-LKK", "60.03349", "20.81507",
                      "33,450 ft", "455.5 kt", "262.9", "-2,944 ft/min",
                      "7s", "https://adsb.fi"]:
            self.assertIn(value, output)

    def test_missing_values_ground_and_zero(self):
        output = app.render("TEST", {"hex": "abcdef", "alt_baro": "ground", "gs": 0},
                            1000, "Tracking", 5, now=1000)
        self.assertIn("On ground", output)
        self.assertIn("0.0 kt", output)
        self.assertIn("--", output)

    def test_stale_position_and_old_response_are_labelled(self):
        output = app.render("TEST", PLANE, 1000, "Tracking", 5, now=1100)
        self.assertIn("STALE", output)
        self.assertIn("102s", output)

    def test_untrusted_text_cannot_emit_terminal_controls(self):
        plane = dict(
            PLANE, flight="ABC\x1b[2J\nINJECT", gs=float("nan"), lat={})
        output = app.render("TEST", plane, 1000, "Tracking", 5, now=1000)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\nINJECT", output)
        self.assertNotIn("nan", output)


class DiscoveryTests(unittest.TestCase):
    def test_global_snapshot_filters_invalid_or_stale_positions_and_converts_units(self):
        state = ["461e1a", "FIN40T  ", "Finland", 998, 999, 20.8, 60, 10000,
                 False, 200, 260, -5, None, 10100, "6020", False, 0]
        states = [state, state, dict(PLANE), [],
                  ["badhex", *state[1:]],
                  [*state[:5], None, *state[6:]],
                  [*state[:3], 900, *state[4:]]]
        with patch("termiplane.urlopen", return_value=io.BytesIO(json.dumps({
            "time": 1000, "states": states
        }).encode())) as request, patch("termiplane.time.time", return_value=1000):
            planes, timestamp = app.fetch_aircraft_list()
        self.assertEqual(
            request.call_args.args[0].full_url, "https://opensky-network.org/api/states/all")
        self.assertEqual(timestamp, 1000)
        self.assertEqual(len(planes), 1)
        plane = planes[0]
        self.assertEqual((plane["hex"], plane["flight"],
                         plane["country"]), ("461e1a", "FIN40T", "Finland"))
        self.assertAlmostEqual(plane["alt_baro"], 32808.4, places=1)
        self.assertAlmostEqual(plane["gs"], 388.77, places=2)

    def test_malformed_snapshot_is_an_error_and_null_states_is_empty(self):
        for payload in ({}, [], {"time": 1000, "states": {}}, {"time": "bad", "states": []},
                        {"time": 1e30, "states": []}, {"time": -1, "states": []}):
            with self.subTest(payload=payload), patch("termiplane.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
                with self.assertRaises(ValueError):
                    app.fetch_aircraft_list()
        with patch("termiplane.urlopen", return_value=io.BytesIO(b'{"time":1000,"states":null}')):
            self.assertEqual(app.fetch_aircraft_list(), ([], 1000))

    def test_fuzzy_search_handles_gaps_case_and_no_match(self):
        planes = [dict(PLANE, country="Finland"),
                  dict(PLANE, hex="abcdef", flight="BAW123", r="G-TEST", country="United Kingdom")]
        for query in ("fn4t", "461E", "finland", "OHlK"):
            self.assertEqual(app.filter_aircraft(planes, query), [planes[0]])
        self.assertEqual(app.filter_aircraft(planes, "xyzxyz"), [])
        self.assertEqual(len(app.filter_aircraft(planes, "")), 2)

    def test_picker_lists_known_callsigns_before_unidentified_aircraft(self):
        unknown = dict(PLANE, hex="000001", flight="--")
        self.assertEqual(app.filter_aircraft(
            [unknown, PLANE], ""), [PLANE, unknown])


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.planes = [dict(PLANE, country="Finland"),
                       dict(PLANE, hex="abcdef", flight="BAW123", r="G-TEST", country="United Kingdom")]
        for name, value in (("fetch_aircraft_list", (self.planes, 1000)),
                            ("fetch", (PLANE, 1000)), ("curses.has_colors", False)):
            mocked = patch("termiplane." + name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        worker = patch("termiplane.Thread")
        mocked = worker.start()
        mocked.return_value.start.side_effect = lambda: mocked.call_args.kwargs["target"](
        )
        self.addCleanup(worker.stop)
        clock = patch("termiplane.time.time", return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def test_search_selects_and_verifies_by_hex(self):
        screen = Screen(height=24, width=100, keys=[*map(ord, "fn4t"), 10, -1])
        with patch("termiplane.fetch", return_value=(PLANE, 1000)) as fetch:
            self.assertEqual(app.pick_aircraft(screen), (PLANE, 1000))
        self.assertEqual(fetch.call_args.args, ("hex", "461E1A"))
        self.assertIn("OpenSky", screen.frames[0])
        self.assertNotIn("BAW123", screen.frames[4])

    def test_unavailable_selection_stays_in_picker_and_can_cancel(self):
        screen = Screen(keys=[*map(ord, "fn4t"), 10, -1, 27])
        with patch("termiplane.fetch", return_value=(None, None)):
            self.assertIsNone(app.pick_aircraft(screen))
        self.assertIn("No current adsb.fi position", screen.frames[-1])

    def test_navigation_backspace_and_clear_search(self):
        screen = Screen(height=24, width=100, keys=[app.curses.KEY_DOWN, ord("z"),
                        app.curses.KEY_BACKSPACE, 21, app.curses.KEY_DOWN, 10, -1])
        with patch("termiplane.fetch", return_value=(PLANE, 1000)) as fetch:
            self.assertEqual(app.pick_aircraft(screen), (PLANE, 1000))
        self.assertEqual(fetch.call_args.args, ("hex", "461E1A"))
        self.assertIn("No matches", screen.frames[2])

    def test_error_and_tiny_window_remain_cancellable(self):
        for size in ((24, 80), (3, 9)):
            screen = Screen(height=size[0], width=size[1], keys=[27])
            with patch("termiplane.fetch_aircraft_list", side_effect=URLError("offline")):
                self.assertIsNone(app.pick_aircraft(screen))

    def test_rate_limit_is_shown_and_refresh_respects_retry_after(self):
        error = HTTPError("url", 429, "Too Many Requests", {
                          "X-Rate-Limit-Retry-After-Seconds": "120"}, None)

        def refresh():
            clock.return_value = 100
            return 18

        screen = Screen(height=24, width=100, keys=[refresh, 27])
        with patch("termiplane.fetch_aircraft_list", side_effect=error) as fetch, patch(
            "termiplane.time.monotonic", return_value=10
        ) as clock:
            self.assertIsNone(app.pick_aircraft(screen))
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("429", screen.frames[0])
        self.assertIn("Wait", screen.frames[1])

    def test_stale_tracking_position_is_not_selected(self):
        screen = Screen(keys=[*map(ord, "fn4t"), 10, -1, 27])
        with patch("termiplane.fetch", return_value=(dict(PLANE, seen_pos=60), 1000)):
            self.assertIsNone(app.pick_aircraft(screen))
        self.assertIn("No current adsb.fi position", screen.frames[-1])


class CommandTests(unittest.TestCase):
    def test_no_argument_opens_picker_in_curses(self):
        with patch("sys.stdout.isatty", return_value=True), patch("sys.stdin.isatty", return_value=True), patch.dict(
            "os.environ", {"TERM": "xterm"}
        ), patch("termiplane.curses.wrapper", return_value=0) as wrapper:
            self.assertEqual(app.main([]), 0)
        self.assertEqual(wrapper.call_args.args, (app.run_curses, argparse.Namespace(
            identifier=None, by="auto", interval=5, once=False), None, None))

    def test_missing_identifier_requires_interactive_terminal(self):
        with patch("sys.stdout.isatty", return_value=False), redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                app.main([])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("identifier", output.getvalue())

    def setUp(self):
        route = patch("termiplane.fetch_route", return_value=("--", "--"))
        route.start()
        self.addCleanup(route.stop)

    def test_callsign_change_clears_previous_route(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((PLANE, 1000))
        self.assertEqual(tracker.route_callsign, "FIN40T")
        tracker.route = ("HEL/EFHK", "ARN/ESSA")
        tracker.accept((dict(PLANE, flight="FIN41T "), 1005))
        self.assertEqual(tracker.route_callsign, "FIN41T")
        self.assertEqual(tracker.route, ("--", "--"))

    def test_tracker_keeps_full_observed_trail_with_fresh_unique_positions(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        with patch("termiplane.time.time", return_value=1000) as clock:
            tracker.accept((PLANE, 1000))
            tracker.accept((PLANE, 1001))
            self.assertEqual(len(tracker.trail), 1)
            for lat, seen_pos in [(None, 1), (91, 1), (61, 60), (62, None)]:
                tracker.accept((dict(PLANE, lat=lat, seen_pos=seen_pos), 1000))
            self.assertEqual(len(tracker.trail), 1)
            for index in range(130):
                clock.return_value = 1002 + index
                tracker.accept(
                    (dict(PLANE, lat=40 + index / 100), 1002 + index))
        self.assertEqual(len(tracker.trail), 131)
        self.assertEqual(tracker.trail[-1][:2], (41.29, PLANE["lon"]))

    def test_ground_to_air_transition_keeps_full_session_trail(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        with patch("termiplane.time.time", return_value=1010):
            tracker.accept((PLANE, 1000))
            tracker.accept((dict(PLANE, lat=40, alt_baro="ground"), 1005))
            tracker.accept((dict(PLANE, lat=41, alt_baro=500), 1010))
        self.assertEqual([p[0] for p in tracker.trail], [PLANE["lat"], 40, 41])

    def test_rejects_invalid_refresh_intervals(self):
        for interval in ["0", "-5", "nan", "inf"]:
            with self.subTest(interval=interval), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                app.main(["461E1A", "--interval", interval])
            self.assertEqual(exit.exception.code, 2)

    def test_once_prints_snapshot_and_returns_success(self):
        with patch("termiplane.fetch", return_value=(PLANE, 1000)), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(["FIN40T", "--once"]), 0)
        self.assertIn("OH-LKK", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("Next refresh", output.getvalue())

    def test_once_failure_has_nonzero_exit_and_clear_message(self):
        for result, message in [(URLError("offline"), "offline"), ((None, None), "No aircraft")]:
            with self.subTest(result=result), patch("termiplane.fetch") as fetch, redirect_stdout(io.StringIO()) as output:
                if isinstance(result, Exception):
                    fetch.side_effect = result
                else:
                    fetch.return_value = result
                self.assertEqual(app.main(["FIN40T", "--once"]), 1)
            self.assertIn(message, output.getvalue())
            self.assertNotIn("Retrying", output.getvalue())

    def test_locks_to_aircraft_and_retains_stale_data_after_network_failure(self):
        with patch("termiplane.fetch", side_effect=[(PLANE, 1000), URLError("offline")]) as fetch, patch(
            "termiplane.time.sleep", side_effect=[None, KeyboardInterrupt]
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(["FIN40T"]), 0)
        self.assertEqual(fetch.call_args_list[1].args, ("hex", "461E1A"))
        self.assertIn("offline", output.getvalue())
        self.assertEqual(output.getvalue().count("OH-LKK"), 2)

    def test_rate_limit_waits_before_retry(self):
        error = HTTPError("url", 429, "Too many requests",
                          {"Retry-After": "120"}, None)
        with patch("termiplane.fetch", side_effect=error), patch(
            "termiplane.time.sleep", side_effect=KeyboardInterrupt
        ) as sleep, redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(["461E1A"]), 0)
        self.assertGreaterEqual(sleep.call_args.args[0], 120)

    def test_rejected_requests_stop_without_retrying(self):
        error = HTTPError("url", 403, "Forbidden", {}, None)
        self.addCleanup(error.close)
        with patch("termiplane.fetch", side_effect=error), patch("termiplane.time.sleep") as sleep, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(["461E1A"]), 1)
        sleep.assert_not_called()
        self.assertIn("HTTP 403", output.getvalue())

    def test_interactive_mode_uses_curses_wrapper(self):
        with patch("sys.stdout.isatty", return_value=True), patch.dict(
            "os.environ", {"TERM": "xterm"}
        ), patch("termiplane.curses.wrapper", return_value=0) as wrapper:
            self.assertEqual(app.main(["461E1A"]), 0)
        self.assertEqual(wrapper.call_args.args[0], app.run_curses)


class CursesTests(unittest.TestCase):
    def test_picker_can_cancel_while_discovery_is_blocked(self):
        started, release, completed = Event(), Event(), Event()

        def slow_list():
            started.set()
            release.wait(2)
            completed.set()
            return [], 1000

        def cancel():
            self.assertTrue(started.wait(1))
            return 27

        screen = Screen(keys=[cancel])
        try:
            with patch("termiplane.fetch_aircraft_list", side_effect=slow_list), patch(
                "termiplane.curses.has_colors", return_value=False
            ):
                self.assertIsNone(app.pick_aircraft(screen))
            self.assertFalse(completed.is_set())
        finally:
            release.set()
            self.assertTrue(completed.wait(1))

    def test_picker_selection_seeds_dashboard_and_avoids_immediate_refetch(self):
        screen = Screen(height=40, width=120, keys=[ord("q")])
        args = argparse.Namespace(identifier=None, interval=5)
        with patch("termiplane.pick_aircraft", return_value=(PLANE, 1000)), patch(
            "termiplane.fetch"
        ) as fetch, patch("termiplane.curses.has_colors", return_value=False), patch("termiplane.curses.curs_set"):
            self.assertEqual(app.run_curses(screen, args, None, None), 0)
        fetch.assert_not_called()
        self.assertEqual(args.identifier, "FIN40T")
        self.assertIn("OH-LKK", screen.frames[0])

    def test_f_toggles_overview_and_zoom_returns_to_follow(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((PLANE, 1000))
        screen = Screen(height=40, width=120,
                        keys=list(map(ord, "+fff+q")))
        args = argparse.Namespace(identifier="TEST", interval=5)
        with patch("termiplane.Tracker", return_value=tracker), patch(
            "termiplane.fetch", return_value=(PLANE, 1000)
        ), patch("termiplane.curses.has_colors", return_value=False), patch("termiplane.curses.curs_set"):
            self.assertEqual(app.run_curses(screen, args, "hex", "461E1A"), 0)
        self.assertIn("World map", screen.frames[0])
        self.assertIn("Follow map 2x", screen.frames[1])
        self.assertIn("Overview map", screen.frames[2])
        self.assertIn("Follow map 2x", screen.frames[3])
        self.assertIn("Overview map", screen.frames[4])
        self.assertIn("Follow map 4x", screen.frames[5])

    def test_i_toggles_expanded_information(self):
        plane = dict(PLANE, ias=276, tas=458, wd=355, ws=67)
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((plane, 1000))
        screen = Screen(height=40, width=120, keys=[
                        ord("i"), ord("i"), ord("q")])
        args = argparse.Namespace(identifier="TEST", interval=5)
        with patch("termiplane.Tracker", return_value=tracker), patch(
            "termiplane.fetch", return_value=(plane, 1000)
        ), patch("termiplane.curses.has_colors", return_value=False), patch("termiplane.curses.curs_set"):
            self.assertEqual(app.run_curses(screen, args, "hex", "461E1A"), 0)
        self.assertNotIn("Wind est", screen.frames[0])
        self.assertIn("Wind est", screen.frames[1])
        self.assertNotIn("Wind est", screen.frames[2])

    def setUp(self):
        route = patch("termiplane.fetch_route", return_value=("--", "--"))
        route.start()
        self.addCleanup(route.stop)

    def test_slow_route_lookup_does_not_block_quitting(self):
        started, release = Event(), Event()
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((PLANE, 1000))

        def route(callsign):
            started.set()
            release.wait(2)
            return "HEL/EFHK", "ARN/ESSA"

        screen = Screen(keys=[ord("q")])
        args = argparse.Namespace(identifier="TEST", interval=5)
        try:
            with patch("termiplane.Tracker", return_value=tracker), patch(
                "termiplane.fetch", return_value=(PLANE, 1000)
            ), patch("termiplane.fetch_route", side_effect=route), patch(
                "termiplane.curses.has_colors", return_value=False
            ), patch("termiplane.curses.curs_set"):
                self.assertEqual(app.run_curses(
                    screen, args, "hex", "461E1A"), 0)
                self.assertTrue(started.wait(1))
                self.assertFalse(release.is_set())
        finally:
            release.set()

    def test_zoom_reaches_1024_and_can_zoom_back_out(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((PLANE, 1000))
        screen = Screen(height=40, width=120,
                        keys=[ord("+")] * 11 + [ord("-"), ord("0"), ord("q")])
        args = argparse.Namespace(identifier="TEST", interval=5)
        with patch("termiplane.Tracker", return_value=tracker), patch(
            "termiplane.fetch", return_value=(PLANE, 1000)
        ), patch("termiplane.curses.has_colors", return_value=False), patch("termiplane.curses.curs_set"):
            self.assertEqual(app.run_curses(screen, args, "hex", "461E1A"), 0)
        self.assertIn("Follow map 1024x", screen.frames[10])
        self.assertIn("Follow map 1024x", screen.frames[11])
        self.assertIn("Follow map 512x", screen.frames[12])
        self.assertIn("World map", screen.frames[13])

    def test_trail_is_visible_by_default_and_toggles_without_losing_history(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.trail.extend([(60, 10, 998), (60, 20, 1008)])
        tracker.accept((PLANE, 1000))
        screen = Screen(height=40, width=120, keys=[
                        ord("t"), ord("t"), ord("q")])
        args = argparse.Namespace(identifier="TEST", interval=5)
        with patch("termiplane.Tracker", return_value=tracker), patch(
            "termiplane.fetch", return_value=(PLANE, 1000)
        ), patch("termiplane.curses.has_colors", return_value=False), patch("termiplane.curses.curs_set"):
            self.assertEqual(app.run_curses(screen, args, "hex", "461E1A"), 0)
        for frame, state in zip(screen.frames, ["ON", "OFF", "ON"]):
            self.assertIn(f"t trail {state}", frame)
        self.assertIn(". = trail", screen.frames[0])
        self.assertNotIn(". = trail", screen.frames[1])
        self.assertIn(". = trail", screen.frames[2])
        self.assertEqual(len(tracker.trail), 2)

    def test_live_positions_produce_colored_trail_in_zoomed_view(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        with patch("termiplane.time.time", return_value=1010):
            tracker.accept((dict(PLANE, lon=20), 1000))
            tracker.accept((dict(PLANE, lon=21), 1010))
        screen = Screen(height=40, width=120)
        app.draw_curses(screen, tracker, "TEST", 5, [
                        0, 1, 2, 3, 4, 5, 6], zoom=32)
        self.assertTrue(any(text.startswith(".") and style == 5 | app.curses.A_BOLD
                            for text, style in screen.writes))
        self.assertTrue(any(text == "X" for text, _ in screen.writes))

    def test_ascii_terminal_falls_back_without_unicode(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((PLANE, 1000))
        screen = Screen(height=40, width=120)
        screen.encoding = "ascii"
        app.draw_curses(screen, tracker, "TEST", 5, [0] * 7)
        self.assertTrue(screen.frames[-1].isascii())
        self.assertIn("#", screen.frames[-1])

    def test_colored_map_and_bordered_panel_preserve_flight_data(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((PLANE, 1000))
        screen = Screen(height=40, width=120)
        app.draw_curses(screen, tracker, "FIN40T", 5, [0, 1, 2, 3, 4, 5, 6])
        output = screen.frames[-1]
        for value in ["World map", "FLIGHT", "FIN40T", "OH-LKK", "6020", "q quit", "https://adsb.fi"]:
            self.assertIn(value, output)
        self.assertTrue(
            any(text == "X" and style for text, style in screen.writes))
        self.assertNotIn("🛫", output)

    def test_marker_at_map_edge_stays_inside_border(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((dict(PLANE, lon=180), 1000))
        screen = Screen(height=40, width=120)
        app.draw_curses(screen, tracker, "TEST", 5, [0] * 7)
        self.assertTrue(any(text == "X" for text, _ in screen.writes))
        self.assertIn("X = aircraft", screen.frames[-1])

    def test_zoom_keys_change_view_and_reset_to_world(self):
        screen = Screen(height=40, width=120, keys=[
                        ord("+"), ord("-"), ord("+"), ord("0"), ord("q")])
        args = argparse.Namespace(identifier="TEST", interval=5)
        with patch("termiplane.fetch", return_value=(PLANE, 1000)), patch(
            "termiplane.curses.has_colors", return_value=False
        ), patch("termiplane.curses.curs_set"):
            self.assertEqual(app.run_curses(screen, args, "hex", "461E1A"), 0)
        self.assertIn("World map", screen.frames[0])
        self.assertIn("Follow map 2x", screen.frames[1])
        self.assertIn("World map", screen.frames[2])
        self.assertIn("Follow map 2x", screen.frames[3])
        self.assertIn("World map", screen.frames[4])

    def test_solarized_colors_are_applied_and_restored_on_exit(self):
        screen = Screen(keys=[ord("q")])
        args = argparse.Namespace(identifier="TEST", interval=5)
        with patch("termiplane.fetch", return_value=(PLANE, 1000)), patch(
            "termiplane.curses.has_colors", return_value=True
        ), patch("termiplane.curses.can_change_color", return_value=True), patch(
            "termiplane.curses.COLORS", 256, create=True
        ), patch("termiplane.curses.color_content", return_value=(100, 200, 300)), patch(
            "termiplane.curses.init_color"
        ) as color, patch("termiplane.curses.init_pair"), patch(
            "termiplane.curses.color_pair", side_effect=lambda pair: pair * 256
        ), patch("termiplane.curses.curs_set"):
            self.assertEqual(app.run_curses(screen, args, "hex", "461E1A"), 0)
        self.assertIn((248, 0, 169, 212), [
                      call.args for call in color.call_args_list])
        self.assertEqual([call.args for call in color.call_args_list[-8:]],
                         [(index, 100, 200, 300) for index in range(248, 256)])

    def test_quit_and_resize_work_while_network_request_is_pending(self):
        release = Event()
        started = Event()

        def fetch(*args):
            started.set()
            release.wait(2)
            return PLANE, 1000

        screen = Screen(height=40, width=120)

        def resize():
            screen.height, screen.width = 24, 80
            return app.curses.KEY_RESIZE

        screen.keys = iter([resize, ord("q")])
        args = argparse.Namespace(identifier="FIN40T", interval=5)
        try:
            with patch("termiplane.fetch", side_effect=fetch), patch(
                "termiplane.curses.has_colors", return_value=False
            ), patch("termiplane.curses.curs_set"):
                self.assertEqual(app.run_curses(
                    screen, args, "callsign", "FIN40T"), 0)
            self.assertTrue(started.wait(1))
            self.assertEqual(len(screen.frames), 2)
            self.assertEqual(len(screen.frames[0].splitlines()), 40)
            self.assertEqual(len(screen.frames[1].splitlines()), 24)
            self.assertLessEqual(screen.wait, 250)
        finally:
            release.set()

    def test_monochrome_and_small_terminal_are_supported(self):
        tracker = app.Tracker("hex", "461E1A", 5)
        tracker.accept((PLANE, 1000))
        for height, width in [(24, 80), (8, 20), (1, 1)]:
            with self.subTest(height=height, width=width):
                screen = Screen(height=height, width=width)
                app.draw_curses(screen, tracker, "TEST", 5, [0] * 7)
                self.assertEqual(len(screen.frames[-1].split("\n")), height)


if __name__ == "__main__":
    unittest.main()
