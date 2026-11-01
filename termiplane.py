"""Follow one aircraft using the public adsb.fi API."""

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from itertools import zip_longest
import json
import math
import os
from queue import Empty, SimpleQueue
import re
import shutil
import sys
from textwrap import wrap
from threading import Thread
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from termiplane_land import LAND
import termiplane_maps as maps

try:
    import curses
except ImportError:
    curses = None


API = "https://opendata.adsb.fi/api/v2"


def identify(identifier, by):
    """Classify and validate a callsign, registration, or ICAO address.

    :param identifier: User-supplied aircraft identifier.
    :param by: Requested identifier type, or ``auto``.
    :returns: A pair containing the API lookup type and normalized identifier.
    :raises argparse.ArgumentTypeError: If the identifier does not match its type.
    """
    value = identifier.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,11}", value):
        raise argparse.ArgumentTypeError(
            "use a callsign, registration, or six-digit ICAO hex address")
    if by == "auto":
        if re.fullmatch(r"[A-F0-9]{6}", value):
            by = "hex"
        elif "-" in value or re.match(r"N[0-9]", value):
            by = "registration"
        else:
            by = "callsign"
    if by == "hex" and not re.fullmatch(r"[A-F0-9]{6}", value):
        raise argparse.ArgumentTypeError(
            "ICAO hex addresses contain exactly six hexadecimal digits")
    return by, value


def number(value):
    """Return whether a value is a finite number, excluding booleans.

    :param value: Value received from an API response.
    :returns: ``True`` for finite integers and floats.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fetch(kind, identifier):
    """Fetch a single aircraft from adsb.fi and validate its identity.

    :param kind: API endpoint identifier type.
    :param identifier: Callsign, registration, or ICAO hex value.
    :returns: Aircraft data and the response timestamp, or ``(None, None)``.
    :raises OSError: If the request fails.
    :raises ValueError: If the response is invalid or ambiguous.
    """
    request = Request(f"{API}/{kind}/{identifier}",
                      headers={"User-Agent": "termiplane/0.1.0"})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("msg", "No error") != "No error":
        raise ValueError("Invalid API response or upstream error")
    aircraft = payload.get("ac")
    if not isinstance(aircraft, list):
        raise ValueError("Invalid aircraft list in API response")
    if not aircraft:
        return None, None
    if len(aircraft) > 1:
        raise ValueError("Multiple aircraft matched; use an ICAO hex address")
    plane = aircraft[0]
    if not isinstance(plane, dict) or not re.fullmatch(r"[a-fA-F0-9]{6}", str(plane.get("hex", ""))):
        raise ValueError("Invalid aircraft identity in API response")
    if kind == "hex" and plane["hex"].upper() != identifier.upper():
        raise ValueError(
            "API returned a different aircraft; keeping the original ICAO address")
    timestamp = payload.get("now")
    timestamp = timestamp / \
        1000 if number(timestamp) and timestamp > 0 else time.time()
    return plane, min(timestamp, time.time())


def clean(value):
    """Strip non-printable and non-ASCII characters from terminal text.

    :param value: Untrusted display value.
    :returns: Safe printable text, or ``--`` for empty values.
    """
    if value is None:
        return "--"
    return "".join(c for c in str(value) if c.isascii() and c.isprintable()).strip() or "--"


def fetch_aircraft_list():
    """Fetch fresh, globally visible aircraft for the interactive picker.

    OpenSky's anonymous global snapshot is discovery-only; selected aircraft are
    checked against the tracking feed before the dashboard opens.

    :returns: A pair containing normalized aircraft records and snapshot time.
    :raises OSError: If OpenSky cannot be reached.
    :raises ValueError: If the response has an invalid snapshot structure.
    """
    request = Request("https://opensky-network.org/api/states/all",
                      headers={"User-Agent": "termiplane/0.1.0"})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    now = time.time()
    if not (isinstance(payload, dict) and number(payload.get("time")) and 0 < payload["time"] <= now + 60):
        raise ValueError("Invalid discovery snapshot")
    states = payload.get("states")
    if "states" not in payload or states is not None and not isinstance(states, list):
        raise ValueError("Invalid discovery aircraft list")
    timestamp = payload["time"]
    planes = {}
    for state in states or []:
        if not isinstance(state, list) or len(state) < 17:
            continue
        hexcode, callsign, country, position_time = state[:4]
        lon, lat = state[5:7]
        age = max(now, timestamp) - \
            position_time if number(position_time) else None
        if not (isinstance(hexcode, str) and re.fullmatch(r"[a-fA-F0-9]{6}", hexcode)
                and number(lat) and number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
                and age is not None and 0 <= age <= 30):
            continue
        planes[hexcode.lower()] = {
            "hex": hexcode.lower(), "flight": clean(callsign), "country": clean(country),
            "lat": lat, "lon": lon,
            "alt_baro": "ground" if state[8] is True else state[7] / 0.3048 if number(state[7]) else None,
            "gs": state[9] * 3600 / 1852 if number(state[9]) else None,
        }
    return list(planes.values()), timestamp


def filter_aircraft(planes, query):
    """Return picker candidates matching a case-insensitive fuzzy query.

    :param planes: Aircraft records from :func:`fetch_aircraft_list`.
    :param query: Text typed into the picker.
    :returns: Matching records, with closest matches first.
    """
    needle = "".join(query.upper().split())
    matches = []
    for plane in planes:
        text = " ".join(clean(plane.get(field)) for field in (
            "flight", "hex", "r", "t", "country")).upper()
        index = text.find(needle)
        if index >= 0:
            score = (0, index, 0)
        else:
            positions = []
            end = 0
            for character in needle:
                index = text.find(character, end)
                if index < 0:
                    break
                positions.append(index)
                end = index + 1
            else:
                score = (1, positions[-1] -
                         positions[0] - len(needle), positions[0])
            if index < 0:
                continue
        callsign = clean(plane.get("flight"))
        matches.append(
            (score, callsign == "--", callsign, plane["hex"], plane))
    return [match[-1] for match in sorted(matches, key=lambda match: match[:-1])]


# shortcut: callsign routes can be outdated; use a flight-status API for verified schedules.
@lru_cache(maxsize=128)
def fetch_route(callsign):
    """Look up cached departure and arrival codes for an observed callsign.

    :param callsign: Validated callsign from the aircraft feed.
    :returns: Departure and destination code strings, or ``--`` values.
    """
    unavailable = ("--", "--")
    if not re.fullmatch(r"[A-Z0-9]{2,8}", callsign):
        return unavailable
    request = Request(f"https://api.adsbdb.com/v0/callsign/{callsign}",
                      headers={"User-Agent": "termiplane/0.1.0"})
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (OSError, ValueError) as error:
        if isinstance(error, HTTPError):
            error.close()
        return unavailable
    response = payload.get("response") if isinstance(payload, dict) else None
    route = response.get("flightroute") if isinstance(response, dict) else None
    if not isinstance(route, dict) or callsign not in (
        route.get("callsign"), route.get(
            "callsign_icao"), route.get("callsign_iata")
    ):
        return unavailable
    airports = []
    for endpoint in ("origin", "destination"):
        airport = route.get(endpoint)
        codes = []
        if isinstance(airport, dict):
            for key, pattern in (("iata_code", r"[A-Z]{3}"), ("icao_code", r"[A-Z0-9]{4}")):
                value = airport.get(key)
                if isinstance(value, str) and re.fullmatch(pattern, value):
                    codes.append(value)
        airports.append("/".join(codes) or "--")
    return tuple(airports)


def format_number(value, spec, unit=""):
    """Format a finite number with a display unit, or ``--`` if unavailable.

    :param value: Numeric API field.
    :param spec: Python numeric format specification.
    :param unit: Optional suffix for the formatted value.
    :returns: Formatted display value.
    """
    return f"{value:{spec}}{unit}" if number(value) else "--"


def altitude_trend(plane):
    """Classify the aircraft's reported vertical rate.

    :param plane: Aircraft API record.
    :returns: ``Climbing``, ``Descending``, ``Level``, ``Ground``, or ``--``.
    """
    if plane.get("alt_baro") == "ground":
        return "Ground"
    rate = plane.get("baro_rate")
    if not number(rate):
        return "--"
    if rate > 100:
        return "Climbing"
    if rate < -100:
        return "Descending"
    return "Level"


@lru_cache(maxsize=16)
def nearest_airport(lat, lon):
    """Find the nearest listed airport to a valid WGS-84 position.

    :param lat: Aircraft latitude in degrees.
    :param lon: Aircraft longitude in degrees.
    :returns: Airport code, name, distance in kilometres, and true bearing,
        or ``None`` if the position or airport list is unavailable.
    """
    if not (number(lat) and number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    latitude = math.radians(lat)

    def separation(airport):
        other_lat = math.radians(airport[2])
        delta_lon = math.radians(airport[3] - lon)
        return (math.sin((other_lat - latitude) / 2) ** 2
                + math.cos(latitude) * math.cos(other_lat) * math.sin(delta_lon / 2) ** 2)

    airport = min(maps.layer("airports"), key=separation, default=None)
    if airport is None:
        return None
    angle = min(1, max(0, separation(airport)))
    distance = 6371.0088 * 2 * math.asin(math.sqrt(angle))
    other_lat = math.radians(airport[2])
    delta_lon = math.radians(airport[3] - lon)
    y = math.sin(delta_lon) * math.cos(other_lat)
    x = math.cos(latitude) * math.sin(other_lat) - \
        math.sin(latitude) * math.cos(other_lat) * math.cos(delta_lon)
    bearing = (math.degrees(math.atan2(y, x)) +
               360) % 360 if distance else None
    return airport[0], airport[1], distance, bearing


@lru_cache(maxsize=128)
def airport_location(code):
    """Resolve an airport code against the bundled airport database.

    :param code: One or more IATA/ICAO codes separated by ``/``.
    :returns: Latitude and longitude, or ``None`` when not found.
    """
    codes = set(code.split("/")) - {"--", ""}
    if codes:
        for airport_codes, _, latitude, longitude in maps.layer("airports"):
            if codes.intersection(airport_codes.split("/")):
                return latitude, longitude
    return None


@lru_cache(maxsize=8)
def land_mask(columns, rows, west=-180, east=180, north=90, south=-90, detail="110m"):
    """Rasterize bundled land polygons for a geographic viewport.

    :param columns: Raster width.
    :param rows: Raster height.
    :param west: Western longitude bound.
    :param east: Eastern longitude bound.
    :param north: Northern latitude bound.
    :param south: Southern latitude bound.
    :param detail: Natural Earth coastline resolution.
    :returns: Rows of ``#`` land cells and space ocean cells.
    """
    polygons = LAND if detail == "110m" else (
        rings for box, rings in maps.layer(f"land_{detail}")
        if maps.intersects(box, west, east, north, south)
    )
    edges = []
    for polygon in polygons:
        edges.append([(ax, ay, bx, by) for ring in polygon
                      for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1])
                      if max(ay, by) >= south and min(ay, by) <= north])
    grid = [[" "] * columns for _ in range(rows)]
    for y, row in enumerate(grid):
        latitude = north - y * (north - south) / (rows - 1)
        if not -90 <= latitude <= 90:
            continue
        for polygon in edges:
            crossings = sorted(
                ax + (bx - ax) * (latitude - ay) / (by - ay)
                for ax, ay, bx, by in polygon
                if (ay > latitude) != (by > latitude)
            )
            for start_lon, end_lon in zip(crossings[::2], crossings[1::2]):
                for offset in (-360, 0, 360):
                    start = max(0, math.ceil(
                        (start_lon + offset - west) / (east - west) * (columns - 1)))
                    end = min(columns - 1, math.floor((end_lon +
                              offset - west) / (east - west) * (columns - 1)))
                    if start <= end:
                        row[start:end + 1] = ["#"] * (end - start + 1)
    return tuple("".join(row) for row in grid)


def draw_line(grid, start, end, character):
    """Draw a clipped line between map-grid cells.

    :param grid: Mutable character grid.
    :param start: Starting ``(x, y)`` cell.
    :param end: Ending ``(x, y)`` cell.
    :param character: Character used to draw the line.
    """
    x1, y1 = start
    x2, y2 = end
    rows, columns = len(grid), len(grid[0])
    if max(x1, x2) < 0 or min(x1, x2) >= columns or max(y1, y2) < 0 or min(y1, y2) >= rows:
        return
    steps = max(abs(x2 - x1), abs(y2 - y1), 1)
    for step in range(steps + 1):
        x = round(x1 + (x2 - x1) * step / steps)
        y = round(y1 + (y2 - y1) * step / steps)
        if 0 <= x < columns and 0 <= y < rows:
            grid[y][x] = character


def distance_scale(degrees_per_column, latitude, width):
    """Build a latitude-adjusted map scale bar.

    :param degrees_per_column: Longitude span represented by one cell.
    :param latitude: Latitude where horizontal distance is measured.
    :param width: Available terminal width.
    :returns: Scale-bar text, or an empty string when it cannot fit.
    """
    if width < 30:
        return ""
    cosine = math.cos(math.radians(latitude))
    if cosine < 1e-6:
        return "Scale unavailable at pole"
    meters_per_column = 60 * 1852 * cosine * degrees_per_column
    target = min(20, (width - 24) // 2) * meters_per_column
    magnitude = 10 ** math.floor(math.log10(target))
    distance = max(step * magnitude for step in (1, 2, 5)
                   if step * magnitude <= target)
    cells = max(1, round(distance / meters_per_column))
    label = f"{distance / 1000:g} km" if distance >= 1000 else f"{distance:g} m"
    scale = f"|{'-' * (cells - 1)}| ~{label} @ {abs(latitude):.0f}{'S' if latitude < 0 else 'N'}"
    return scale if len(scale) <= width - 4 else ""


def position_map(lat, lon, stale, width=80, height=14, smooth=False, zoom=1, trail=(), airports=(), overview=False):
    """Render the geographic map, projected trail, and visible airport markers.

    :param zoom: Follow-map magnification, from 1x to 1024x.
    :param airports: ``(role, code, latitude, longitude)`` marker records.
    :param overview: Fit the aircraft and supplied airports in the viewport.
    :returns: Fixed-width map lines, or an empty list for insufficient space.
    """
    if width < 4 or height < 4:
        return []
    columns, rows = width - 2, height - 2
    pixel_rows = rows * 2 if smooth else rows
    map_columns = min(columns, pixel_rows * 2) if smooth else columns
    map_rows = min(pixel_rows, max(2, round(columns / 2))) if smooth else rows
    valid = number(lat) and number(lon) and - \
        90 <= lat <= 90 and -180 <= lon <= 180
    focused = zoom > 1 and valid
    center_lon = lon
    bounds = (-180, 180, 90, -90)
    if overview and valid:
        focused = True
        map_columns, map_rows = columns, pixel_rows
        points = [(lat, lon), *[(a[2], a[3]) for a in airports]]
        longitudes = sorted(longitude % 360 for _, longitude in points)
        # Remove the largest gap so date-line routes fit along their short arc.
        gaps = [(longitudes[(i + 1) % len(longitudes)] + (360 if i == len(longitudes) - 1 else 0)
                 - value, i) for i, value in enumerate(longitudes)]
        gap, index = max(gaps)
        west = longitudes[(index + 1) % len(longitudes)]
        center_lon = (west + (360 - gap) / 2 + 180) % 360 - 180
        north, south = max(p[0] for p in points), min(p[0] for p in points)
        center_lat = (north + south) / 2
        aspect = (map_rows - 1) / (map_columns - 1) * (1 if smooth else 2)
        lon_span = min(360, max((360 - gap) * 1.25,
                       (north - south) * 1.25 / aspect, 0.25))
        lat_span = max(lon_span * aspect, (north - south) * 1.25)
        bounds = (center_lon - lon_span / 2, center_lon + lon_span / 2,
                  center_lat + lat_span / 2, center_lat - lat_span / 2)
        zoom = 360 / lon_span
    elif focused:
        map_columns, map_rows = columns, pixel_rows
        lon_span = 360 / zoom
        lat_span = lon_span * (map_rows - 1) / \
            (map_columns - 1) * (1 if smooth else 2)
        bounds = (lon - lon_span / 2, lon + lon_span / 2,
                  lat + lat_span / 2, lat - lat_span / 2)
    detail = "10m" if focused and zoom >= 16 else "50m" if focused and zoom >= 4 else "110m"
    left, top = (columns - map_columns) // 2, (pixel_rows - map_rows) // 2
    pixels = [[" "] * columns for _ in range(pixel_rows)]
    for y, row in enumerate(land_mask(map_columns, map_rows, *bounds, detail=detail)):
        pixels[top + y][left:left + map_columns] = row
    if smooth:
        grid = [[" ▀▄█"[(pixels[2 * y][x] == "#") + 2 * (pixels[2 * y + 1][x] == "#")]
                 for x in range(columns)] for y in range(rows)]
    else:
        grid = pixels

    def project(latitude, longitude):
        if focused:
            longitude = center_lon + (longitude - center_lon + 180) % 360 - 180
        west, east, north, south = bounds
        x = left + round((longitude - west) /
                         (east - west) * (map_columns - 1))
        y = (top + round((north - latitude) / (north - south)
             * (map_rows - 1))) // (2 if smooth else 1)
        return x, y, longitude

    if focused and zoom >= 4:
        for box, line in maps.layer("borders"):
            if not maps.intersects(box, *bounds):
                continue
            for (lon1, lat1), (lon2, lat2) in zip(line, line[1:]):
                x1, y1, wrapped1 = project(lat1, lon1)
                x2, y2, wrapped2 = project(lat2, lon2)
                if abs(wrapped1 - wrapped2) <= 180:
                    draw_line(grid, (x1, y1), (x2, y2), ":")

    previous = None
    for latitude, longitude, observed_at in trail:
        x, y, projected_lon = project(latitude, longitude)
        start_x, start_y = x, y
        if previous is not None:
            px, py, previous_lon, previous_time = previous
            if 0 < observed_at - previous_time <= 60 and abs(projected_lon - previous_lon) <= 180:
                start_x, start_y = px, py
        draw_line(grid, (start_x, start_y), (x, y), ".")
        previous = x, y, projected_lon, observed_at
    occupied = {(y, x) for y, row in enumerate(grid)
                for x, cell in enumerate(row) if cell == "."}
    if valid:
        marker_x, marker_y, _ = project(lat, lon)
        occupied.add((marker_y, marker_x))
    airport_cells = {}
    for role, code, latitude, longitude in airports:
        # The closest airport crowds the aircraft at low zoom, so reserve it for close-ups.
        if role == "N" and (overview or zoom < 128):
            continue
        x, y, _ = project(latitude, longitude)
        if 0 <= x < columns and 0 <= y < rows:
            airport_cells.setdefault((x, y), []).append(
                (role, code.split("/")[0]))
            occupied.add((y, x))
    for (x, y), entries in airport_cells.items():
        roles = "/".join(role for role, _ in entries)
        codes = "/".join(dict.fromkeys(code for _, code in entries))
        shared = len(entries) > 1
        under_plane = valid and (x, y) == (marker_x, marker_y)
        label = f"{roles}:{codes}" if shared or under_plane else codes
        for start in (x + 2, x - len(label) - 1):
            if 0 <= start and start + len(label) <= columns and all(
                (y, column) not in occupied for column in range(start, start + len(label))
            ):
                for column in (start - 1, start + len(label)):
                    if 0 <= column < columns and (y, column) not in occupied:
                        grid[y][column] = " "
                grid[y][start:start + len(label)] = label
                occupied.update((row, column) for row in range(y - 1, y + 2)
                                for column in range(start - 1, start + len(label) + 1))
                break
    for (x, y), entries in airport_cells.items():
        grid[y][x] = "+" if len(entries) > 1 else entries[0][0]
    if focused and zoom >= 8:
        count = 0
        for rank, _, name, city_lon, city_lat in maps.layer("cities"):
            if zoom < 32 and rank > 3:
                continue
            x, y, _ = project(city_lat, city_lon)
            label = f"o {name} "
            if not (0 <= y < rows and 0 <= x and x + len(label) <= columns):
                continue
            allowed = {(marker_y, marker_x)} if (
                x, y) == (marker_x, marker_y) else set()
            if any((y, column) in occupied and (y, column) not in allowed
                   for column in range(x - 1, x + len(label) + 1)):
                continue
            if x > 0:
                grid[y][x - 1] = " "
            grid[y][x:x + len(label)] = label
            occupied.update((row, column) for row in range(y - 1, y + 2)
                            for column in range(x - 1, x + len(label) + 1))
            count += 1
            if count >= max(1, min(12, columns * rows // 180)):
                break
    if valid:
        x, y, _ = project(lat, lon)
        grid[y][x] = "X"
    title = "Overview map" if overview and valid else f"Follow map {zoom}x" if focused else "World map"
    title += " (STALE)" if stale else " (north up)"
    if not valid:
        title = "Map unavailable (no position)"
    title += " | X = aircraft | " + ("Natural Earth" if smooth else "# = land")
    if trail:
        title += " | . = trail"
    top = ("+- " + title + " ")[:width - 1].ljust(width - 1, "-") + "+"
    scale = distance_scale(
        (bounds[1] - bounds[0]) / (map_columns - 1), lat if valid else 0, width)
    bottom = ("+- " + scale + " ").ljust(width - 1, "-") + \
        "+" if scale else "+" + "-" * columns + "+"
    return [top, *["|" + "".join(row) + "|" for row in grid], bottom]


def flight_panel(groups, width):
    """Lay out grouped flight details within a bordered terminal panel.

    :param groups: Section names and their label/value pairs.
    :param width: Panel width in terminal columns.
    :returns: Panel rows padded to the requested width.
    """
    inside = width - 4
    columns = 3 if width >= 76 else 1
    cell_width = (inside - 3 * (columns - 1)) // columns
    sections = []
    for title, fields in groups:
        rows = wrap(title, width=cell_width)
        for label, value in fields:
            rows.extend(wrap(f"{label:<9} {value}", width=cell_width,
                             subsequent_indent=" " * min(10, cell_width // 2)))
        sections.append(rows)
    if columns == 3:
        lines = [" | ".join(f"{cell:<{cell_width}}" for cell in row)
                 for row in zip_longest(*sections, fillvalue="")]
    else:
        lines = [line for section in sections for line in section]
    border = "+" + "-" * (width - 2) + "+"
    return [("+- FLIGHT DATA ")[:width - 1].ljust(width - 1, "-") + "+",
            *[f"| {line:<{inside}} |" for line in lines], border]


def render(identifier, plane, timestamp, status, delay, now=None, width=80, height=24, framed=False, smooth=False, zoom=1, trail=(), trail_visible=True, route=("--", "--"), expanded=False, overview=False):
    """Compose the terminal dashboard, map, airport data, and controls.

    :param plane: Latest aircraft response, or ``None`` while unavailable.
    :param zoom: Follow-map magnification; nearest-airport markers appear at 128x.
    :param overview: Whether the map fits the flight and route airports.
    :returns: Dashboard text clipped to the requested terminal dimensions.
    """
    now = time.time() if now is None else now
    width, height = max(1, width), max(1, height)
    header = [
        f"termiplane | {clean(identifier)} | UTC {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M:%S}"]
    lat, lon, position_state = None, None, ""
    airports = []
    if plane is not None:
        elapsed = max(0, now - timestamp)
        seen = plane.get("seen")
        seen_pos = plane.get("seen_pos")
        age = elapsed + max(0, seen) if number(seen) else None
        position_age = elapsed + max(0, seen_pos) if number(seen_pos) else None
        state = "LIVE" if age is not None and age <= 30 and status == "Tracking" else "STALE / last known"
        altitude = plane.get("alt_baro")
        altitude = "On ground" if altitude == "ground" else format_number(
            altitude, ",.0f", " ft")
        trend = altitude_trend(plane)
        selected = plane.get("nav_altitude_mcp")
        if not number(selected):
            selected = plane.get("nav_altitude_fms")
        selected = format_number(selected, ",.0f", " ft")
        mach = format_number(plane.get("mach"), ".3f")
        lat, lon = plane.get("lat"), plane.get("lon")
        position = "--"
        if number(lat) and number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            position = f"{lat:.5f}, {lon:.5f}"
        nearest = nearest_airport(lat, lon) if position != "--" else None
        airport_markers = [("D", route[0]), ("A", route[1])]
        if zoom >= 128 and not overview and nearest is not None:
            airport_markers.append(("N", nearest[0]))
        for role, code in airport_markers:
            location = airport_location(code)
            if location is not None:
                airports.append((role, code, *location))
        nearest_text = "--"
        if nearest is not None:
            code, name, distance, bearing = nearest
            distance = f"{distance * 1000:.0f} m" if distance < 1 else f"{distance:.1f} km"
            direction = "--"
            if bearing is not None:
                compass = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[
                    int((bearing + 22.5) / 45) % 8]
                direction = f"{compass} ({bearing:.0f}degT)"
            nearest_text = f"{clean(code)} {clean(name)} | {distance} {direction}"
        position_state = " (STALE)" if position_age is None or position_age > 30 or state != "LIVE" else ""
        header.append(f"{clean(status)} | {state}")
        details = [
            f"Callsign {clean(plane.get('flight'))} | Registration {clean(plane.get('r'))} | "
            f"ICAO {clean(plane.get('hex'))} | Type {clean(plane.get('t'))} | Mach {mach}",
            f"Position {position}{position_state} | Altitude {altitude} | Selected {selected}",
            f"Speed {format_number(plane.get('gs'), '.1f', ' kt')} | "
            f"Track {format_number(plane.get('track'), '.1f', ' deg')} | "
            f"Climb {format_number(plane.get('baro_rate'), ',.0f', ' ft/min')} | Trend {trend}",
            f"Squawk {clean(plane.get('squawk'))} | Emergency {clean(plane.get('emergency'))} | "
            f"Report age {format_number(age, '.0f', 's')} | Position age {format_number(position_age, '.0f', 's')}",
            f"Departure {route[0]} | Arrival {route[1]} (callsign DB)",
            f"Nearest {nearest_text}",
        ]
        if expanded:
            modes = plane.get("nav_modes")
            if isinstance(modes, list) and all(isinstance(mode, str) for mode in modes):
                autopilot = "On" if "autopilot" in modes else "Off"
                modes = clean(" ".join(mode.upper()
                              for mode in modes if mode != "autopilot"))
            else:
                autopilot, modes = "--", "--"
            wind = "--"
            if number(plane.get("wd")) and number(plane.get("ws")):
                wind = f"{plane['wd']:.0f}deg {plane['ws']:.0f}kt"
            extra_aircraft = [("Autopilot", autopilot), ("Nav modes", modes)]
            extra_position = [("Wind est", wind), ("OAT est",
                                                   format_number(plane.get("oat"), ".0f", " C"))]
            extra_flight = [("IAS", format_number(plane.get("ias"), ".1f", " kt")),
                            ("TAS", format_number(plane.get("tas"), ".1f", " kt")),
                            ("True hdg", format_number(
                                plane.get("true_heading"), ".1f", " deg")),
                            ("Mag hdg", format_number(plane.get("mag_heading"), ".1f", " deg"))]
            details.extend(" | ".join(f"{label} {value}" for label, value in fields)
                           for fields in (extra_aircraft, extra_position, extra_flight))
    else:
        header.append(clean(status))
        details = ["Waiting for aircraft coverage..."]
    footer = [
        f"Next refresh in {delay:g}s" if delay is not None else "Polling stopped",
        "Data: https://adsb.fi | Route DB: adsbdb.com | Personal, non-commercial use",
    ]
    if framed:
        footer[0] += f" | +/- zoom | 0 world | f {'follow' if overview else 'overview'} | t trail {'ON' if trail_visible else 'OFF'} | i {'less' if expanded else 'more'} | q quit"
    else:
        footer[0] += " | Ctrl-C to quit"
    panel = framed and width >= 8
    header = [part for line in header for part in wrap(line, width=width)]
    details = [part for line in details for part in wrap(
        line, width=width - 4 if panel else width)]
    footer = [part for line in footer for part in wrap(line, width=width)]
    if panel:
        if plane is not None:
            groups = [
                ("AIRCRAFT", [("Callsign", clean(plane.get("flight"))),
                              ("Reg", clean(plane.get("r"))),
                              ("ICAO", clean(plane.get("hex")).upper()),
                              ("Type", clean(plane.get("t"))),
                              ("Squawk", clean(plane.get("squawk"))),
                              ("Emergency", clean(plane.get("emergency")))]),
                ("POSITION", [("Latitude", format_number(lat, ".5f") if position != "--" else "--"),
                              ("Longitude", format_number(lon, ".5f")
                               if position != "--" else "--"),
                              ("Last seen", format_number(age, ".0f", "s")),
                              ("Pos age", format_number(position_age, ".0f", "s")),
                              ("Position", "Unavailable" if position ==
                               "--" else "STALE" if position_state else "LIVE"),
                              ("Trend", trend)]),
                ("FLIGHT", [("Altitude", altitude),
                            ("Speed", format_number(plane.get("gs"), ".1f", " kt")),
                            ("Track", format_number(
                                plane.get("track"), ".1f", " deg")),
                            ("Climb", format_number(
                                plane.get("baro_rate"), ",.0f", " ft/min")),
                            ("Mach", mach), ("Selected", selected)])]
            if expanded:
                for group, fields in zip(groups, (extra_aircraft, extra_position, extra_flight)):
                    group[1].extend(fields)
            details = flight_panel(groups, width)
            details[1:1] = [f"| {line:<{width - 4}} |" for text in (
                f"Departure [D] {route[0]}   ->   Arrival [A] {route[1]}", f"Nearest [N] {nearest_text}")
                for line in wrap(text, width=width - 4)]
        else:
            border = "+" + "-" * (width - 2) + "+"
            details = [
                border, *[f"| {line:<{width - 4}} |" for line in details], border]
    map_height = height - len(header) - len(details) - len(footer)
    map_lines = position_map(lat, lon, bool(position_state), width=width, height=map_height,
                             smooth=smooth, zoom=zoom, trail=trail if trail_visible else (),
                             airports=airports, overview=overview)
    return "\n".join((header + map_lines + details + footer)[:height])


def retry_after(headers):
    """Read either standard or OpenSky-specific rate-limit retry headers.

    :param headers: HTTP response headers, if available.
    :returns: Non-negative delay in seconds, or zero if not supplied.
    """
    value = headers.get(
        "Retry-After", headers.get("X-Rate-Limit-Retry-After-Seconds", "")) if headers else ""
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


def lookup(kind, identifier):
    """Fetch one aircraft and turn expected network errors into results.

    :param kind: API identifier type.
    :param identifier: Aircraft identifier.
    :returns: A ``(plane, timestamp)`` result or the encountered exception.
    """
    try:
        return fetch(kind, identifier)
    except (HTTPError, URLError, OSError, ValueError) as error:
        return error


@dataclass
class Tracker:
    """Track live aircraft data, failures, and the current-session position trail.

    :param kind: API identifier type.
    :param identifier: Aircraft identifier.
    :param interval: Requested polling interval in seconds.
    """
    """Maintain one aircraft's live state, route, retry status, and observed trail.

    :param kind: API identifier type.
    :param identifier: Aircraft identifier.
    :param interval: Requested polling interval in seconds.
    """
    kind: str
    identifier: str
    interval: float
    plane: dict | None = None
    timestamp: float | None = None
    status: str = "Connecting..."
    failures: int = 0
    delay: float = 0
    stopped: bool = False
    success: bool = False
    trail: deque = field(default_factory=deque)
    route_callsign: str = ""
    route: tuple = ("--", "--")

    def accept(self, result):
        """Update tracker state from a lookup result and extend a fresh trail.

        :param result: A ``(plane, timestamp)`` pair or lookup exception.
        """
        self.delay = self.interval
        self.success = False
        if isinstance(result, Exception):
            self.failures += 1
            self.delay = max(self.interval, min(
                60, 5 * 2 ** min(self.failures - 1, 4)))
            if isinstance(result, HTTPError):
                self.stopped = result.code in (400, 401, 403, 404)
                self.delay = max(self.delay, retry_after(result.headers))
                self.status = f"HTTP {result.code}: {result.reason}"
                result.close()
            else:
                self.status = f"API unavailable: {result}"
            self.status += " | Stopped" if self.stopped else " | Retrying"
        else:
            found, timestamp = result
            self.failures = 0
            if found is None:
                self.status = "No aircraft in coverage; retrying"
            else:
                self.plane, self.timestamp = found, timestamp
                self.kind, self.identifier = "hex", found["hex"].upper()
                self.status, self.success = "Tracking", True
                callsign = found.get("flight")
                callsign = callsign.strip().upper() if isinstance(callsign, str) else ""
                if not re.fullmatch(r"[A-Z0-9]{2,8}", callsign):
                    callsign = ""
                if callsign != self.route_callsign:
                    self.route_callsign, self.route = callsign, ("--", "--")
                lat, lon, seen_pos = found.get("lat"), found.get(
                    "lon"), found.get("seen_pos")
                if (number(lat) and number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
                        and number(seen_pos) and seen_pos >= 0
                        and 0 <= time.time() - timestamp + seen_pos <= 30):
                    point = (lat, lon, timestamp - seen_pos)
                    if not self.trail or (point[:2] != self.trail[-1][:2] and point[2] > self.trail[-1][2]):
                        self.trail.append(point)


def draw_curses(screen, tracker, identifier, delay, styles, zoom=1, trail_visible=True, expanded=False, overview=False):
    """Render a dashboard frame with Solarized curses styles.

    :param screen: Curses window to draw into.
    :param tracker: Current :class:`Tracker` state.
    :param identifier: Display identifier.
    :param delay: Seconds until the next refresh.
    :param styles: Initialized curses style attributes.
    :param zoom: Follow-map magnification.
    :param trail_visible: Whether the recorded trail is displayed.
    :param expanded: Whether optional flight fields are shown.
    :param overview: Whether the map fits route airports.
    """
    height, columns = screen.getmaxyx()
    if columns < 2 or height < 1:
        screen.erase()
        screen.refresh()
        return
    width = max(1, columns - 1)
    try:
        "▀▄█".encode(getattr(screen, "encoding", "utf-8"))
        smooth = True
    except (UnicodeEncodeError, LookupError):
        smooth = False
    lines = render(identifier, tracker.plane, tracker.timestamp, tracker.status, delay,
                   width=width, height=height, framed=True, smooth=smooth, zoom=zoom,
                   trail=tracker.trail, trail_visible=trail_visible, route=tracker.route,
                   expanded=expanded, overview=overview).splitlines()
    screen.erase()
    in_map = False
    in_panel = False
    try:
        for y, line in enumerate(lines):
            if y == 0:
                style = styles[1] | curses.A_BOLD
            elif y == 1:
                style = styles[5 if "STALE" in line or "Tracking" not in line else 3] | curses.A_BOLD
            elif line.startswith("+"):
                style = styles[2] | curses.A_BOLD
            else:
                style = styles[6] if line.startswith(
                    ("Next refresh", "Polling stopped", "Data:")) else 0
            if line.startswith(("+- World map", "+- Follow map", "+- Overview map", "+- Map unavailable")):
                in_map = True
            elif in_map and line.startswith("+"):
                in_map = False
            if line.startswith("+- FLIGHT DATA"):
                in_panel = True
            elif in_panel and line.startswith("+"):
                in_panel = False
            if in_map and line.startswith("|"):
                for match in re.finditer(r"[DAN](?:/[DAN])*:[A-Z0-9/-]+|[oX] [A-Za-z0-9'-]+(?: [A-Za-z0-9'-]+)*|[#▀▄█]+|\.+|X|[A-Z0-9][A-Z0-9/-]*|[^#▀▄█A-Z0-9.o]+|o", line):
                    text = match.group()
                    if text.startswith("X "):
                        screen.addnstr(
                            y, match.start(), "X", 1, styles[4] | curses.A_BOLD | curses.A_REVERSE)
                        screen.addnstr(y, match.start(
                        ) + 1, text[1:], width - match.start() - 1, styles[6] | curses.A_BOLD)
                        continue
                    attr = styles[3] if text[0] in "#▀▄█" else styles[2] | curses.A_DIM
                    if text == "X":
                        attr = styles[4] | curses.A_BOLD | curses.A_REVERSE
                    elif text.startswith("."):
                        attr = styles[5] | curses.A_BOLD
                    elif text.startswith("o "):
                        attr = styles[6] | curses.A_BOLD
                    elif text[0].isupper() or text[0].isdigit():
                        attr = styles[2] | curses.A_BOLD
                    screen.addnstr(y, match.start(), text,
                                   width - match.start(), attr)
            elif in_panel and line.startswith("|"):
                heading = line[2:].startswith(
                    ("AIRCRAFT", "POSITION", "FLIGHT"))
                screen.addnstr(
                    y, 0, line, width, (styles[2] if heading else styles[6]) | curses.A_BOLD)
                if not heading:
                    for label in re.finditer(r"\b(Callsign|Reg|ICAO|Type|Squawk|Departure|Arrival|Nearest|Latitude|Longitude|Last seen|Pos age|Position|Trend|Altitude|Speed|Track|Climb|Emergency|Mach|Selected|IAS|TAS|True hdg|Mag hdg|Wind est|OAT est|Autopilot|Nav modes)\b", line):
                        screen.addnstr(y, label.start(), label.group(
                        ), width - label.start(), styles[2])
            else:
                screen.addnstr(y, 0, line.ljust(width) if y ==
                               0 else line, width, style)
        screen.refresh()
    except curses.error:
        # A resize can invalidate coordinates between getmaxyx() and drawing.
        pass


@contextmanager
def solarized(screen):
    """Temporarily apply the Solarized Dark palette where curses supports it.

    :param screen: Curses window whose background uses the palette.
    :yields: Style attributes for headers, labels, map, and aircraft.
    """
    styles = [0] * 7
    originals = []
    try:
        if curses.has_colors():
            palette = [(curses.COLOR_WHITE, curses.COLOR_BLACK),
                       (curses.COLOR_BLUE, curses.COLOR_BLACK),
                       (curses.COLOR_CYAN, curses.COLOR_BLACK),
                       (curses.COLOR_YELLOW, curses.COLOR_BLACK),
                       (curses.COLOR_RED, curses.COLOR_BLACK),
                       (curses.COLOR_WHITE, curses.COLOR_BLACK)]
            if curses.COLORS >= 256:
                palette = [(245, 235), (33, 234), (37, 234),
                           (136, 234), (166, 234), (244, 234)]
            if curses.COLORS >= 16 and curses.can_change_color():
                # Solarized base03, base02, base0, base1, cyan, blue, yellow, orange.
                colors = (0x002B36, 0x073642, 0x839496, 0x93A1A1,
                          0x2AA198, 0x268BD2, 0xB58900, 0xCB4B16)
                first = curses.COLORS - len(colors)
                for index, rgb in enumerate(colors, start=first):
                    originals.append((index, curses.color_content(index)))
                    curses.init_color(index, *(round(((rgb >> shift) & 255) * 1000 / 255)
                                               for shift in (16, 8, 0)))
                palette = [(first + 3, first + 1), (first + 5, first),
                           (first + 4, first), (first + 6, first),
                           (first + 7, first), (first + 2, first)]
            for pair, (foreground, background) in enumerate(palette, start=1):
                curses.init_pair(pair, foreground, background)
                styles[pair] = curses.color_pair(pair)
            screen.bkgd(" ", styles[6])
        yield styles
    finally:
        for index, rgb in originals:
            curses.init_color(index, *rgb)


def run_curses(screen, args, kind, identifier):
    """Run the curses picker/dashboard loop and return when the user quits.

    :param screen: Curses window supplied by :func:`curses.wrapper`.
    :param args: Parsed command-line options.
    :param kind: Lookup type, or ``None`` to open the aircraft picker.
    :param identifier: Selected identifier, or ``None`` to open the picker.
    :returns: Zero on quit or cancellation.
    """
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(200)
    initial = None
    if identifier is None:
        initial = pick_aircraft(screen)
        if initial is None:
            return 0
        kind, identifier = "hex", initial[0]["hex"].upper()
        args.identifier = clean(initial[0].get("flight"))
        if args.identifier == "--":
            args.identifier = identifier
    with solarized(screen) as styles:
        tracker = Tracker(kind, identifier, args.interval)
        if initial is not None:
            tracker.accept(initial)
        results = SimpleQueue()
        route_results = SimpleQueue()
        pending, deadline = False, time.monotonic() + tracker.delay if initial else 0
        route_pending, attempted_callsign = False, ""
        zoom = 1
        trail_visible = True
        expanded = False
        overview = False

        def request():
            results.put(lookup(tracker.kind, tracker.identifier))

        def request_route(callsign):
            route_results.put((callsign, fetch_route(callsign)))

        while True:
            if not pending and not tracker.stopped and time.monotonic() >= deadline:
                Thread(target=request, daemon=True).start()
                pending = True
            try:
                tracker.accept(results.get_nowait())
                pending = False
                deadline = time.monotonic() + tracker.delay
            except Empty:
                pass
            if not tracker.route_callsign:
                attempted_callsign = ""
            elif not route_pending and tracker.route_callsign != attempted_callsign:
                attempted_callsign = tracker.route_callsign
                Thread(target=request_route, args=(
                    attempted_callsign,), daemon=True).start()
                route_pending = True
            try:
                callsign, route = route_results.get_nowait()
                route_pending = False
                if callsign == tracker.route_callsign:
                    tracker.route = route
            except Empty:
                pass
            delay = None if tracker.stopped else max(
                0, math.ceil(deadline - time.monotonic()))
            if pending:
                delay = args.interval
            draw_curses(screen, tracker, args.identifier, delay, styles, zoom=zoom,
                        trail_visible=trail_visible, expanded=expanded, overview=overview)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return 1 if tracker.stopped else 0
            if key == 27:
                return 2
            if key in (ord("+"), ord("=")):
                zoom = min(1024, zoom * 2)
                overview = False
            elif key in (ord("-"), ord("_")):
                zoom = max(1, zoom // 2)
                overview = False
            elif key == ord("0"):
                zoom = 1
                overview = False
            elif key in (ord("f"), ord("F")):
                overview = not overview
            elif key in (ord("t"), ord("T")):
                trail_visible = not trail_visible
            elif key in (ord("i"), ord("I")):
                expanded = not expanded


def pick_aircraft(screen):
    """Browse candidates and verify a selected aircraft on the tracking feed.

    :param screen: Curses window used to show the searchable aircraft list.
    :returns: A fresh ``(aircraft, timestamp)`` pair, or ``None`` on cancel.
    """
    screen.keypad(True)
    screen.timeout(200)
    results = SimpleQueue()
    planes, matches = [], []
    query, selected = "", 0
    status = "Loading worldwide aircraft..."
    list_pending, checking = False, False
    next_refresh, next_check = 0, 0
    timestamp = None

    def request_list():
        try:
            result = fetch_aircraft_list()
        except (OSError, ValueError) as error:
            result = error
        results.put(("list", result))

    def request_plane(hexcode):
        results.put(("plane", lookup("hex", hexcode)))

    with solarized(screen) as styles:
        while True:
            if not list_pending and timestamp is None and not next_refresh:
                # Discovery is a global request; wait for explicit refreshes after this attempt.
                list_pending = True
                next_refresh = time.monotonic() + 30
                Thread(target=request_list, daemon=True).start()
            try:
                kind, result = results.get_nowait()
                if kind == "list":
                    list_pending = False
                else:
                    checking = False
                if isinstance(result, Exception):
                    status = clean(result)
                    if isinstance(result, HTTPError):
                        cooldown = max(30, retry_after(result.headers))
                        if kind == "list":
                            next_refresh = time.monotonic() + cooldown
                        else:
                            next_check = time.monotonic() + cooldown
                        result.close()
                elif kind == "list":
                    planes, timestamp = result
                    matches = filter_aircraft(planes, query)
                    selected = 0
                    status = "Choose a candidate; Enter checks its current adsb.fi position."
                else:
                    plane, observed_at = result
                    if plane is not None:
                        lat, lon, seen_pos = plane.get("lat"), plane.get(
                            "lon"), plane.get("seen_pos")
                        if (number(lat) and number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
                                and number(seen_pos) and max(0, time.time() - observed_at) + max(0, seen_pos) <= 30):
                            return result
                    status = "No current adsb.fi position; choose another aircraft or refresh."
            except Empty:
                pass

            height, screen_width = screen.getmaxyx()
            width = max(1, screen_width - 1)
            visible = max(0, height - 7)
            selected = min(selected, max(0, len(matches) - 1))
            start = selected // max(1, visible) * max(1, visible)
            snapshot = datetime.fromtimestamp(timestamp, timezone.utc).strftime(
                "%H:%M:%S UTC") if timestamp is not None else "loading"
            lines = [
                "termiplane | CHOOSE AN AIRCRAFT | OpenSky discovery / adsb.fi tracking",
                f"Search > {query}",
                f"{len(matches):,} matches / {len(planes):,} candidates | Snapshot {snapshot}",
                f"  {'CALLSIGN':<10} {'ICAO':<8} {'REG. COUNTRY':<23} {'ALT ft':>9} {'kt':>5}   LAT / LON",
            ]
            for index, plane in enumerate(matches[start:start + visible], start=start):
                altitude = "Ground" if plane.get("alt_baro") == "ground" else format_number(
                    plane.get("alt_baro"), ",.0f")
                lines.append(f"{'>' if index == selected else ' '} {clean(plane.get('flight'))[:10]:<10} "
                             f"{plane['hex'].upper():<8} {clean(plane.get('country'))[:23]:<23} "
                             f"{altitude:>9} {format_number(plane.get('gs'), '.0f'):>5}   "
                             f"{plane['lat']:.2f} / {plane['lon']:.2f}")
            if not matches and visible:
                lines.append(
                    "No matches" if planes else "Loading..." if list_pending else "No aircraft available")
            footer = ["Checking adsb.fi..." if checking else "Refreshing list..." if list_pending else status,
                      "Enter follow | Esc quit | Arrows/PgUp/Dn | Ctrl-U clear | Ctrl-R refresh",
                      "Discovery: https://opensky-network.org | Tracking: https://adsb.fi"]
            screen.erase()
            try:
                for y, line in enumerate(lines[:max(0, height - 3)]):
                    style = styles[1] | curses.A_BOLD if y == 0 else styles[2] if y < 4 else styles[6]
                    if y >= 4 and line.startswith(">"):
                        style = styles[4] | curses.A_BOLD | curses.A_REVERSE
                    screen.addnstr(y, 0, line.ljust(width), width, style)
                for y, line in enumerate(footer[-height:], start=max(0, height - 3)):
                    screen.addnstr(y, 0, line.ljust(width), width,
                                   styles[2] if y == height - 2 else styles[6])
                screen.refresh()
            except curses.error:
                pass
            key = screen.getch()
            if key in (27, 3):
                return None
            if key in (10, 13, curses.KEY_ENTER) and matches and not checking:
                if time.monotonic() < next_check:
                    status = f"Wait {math.ceil(next_check - time.monotonic())}s before checking another aircraft."
                else:
                    checking = True
                    next_check = time.monotonic() + 1
                    hexcode = matches[selected]["hex"].upper()
                    Thread(target=lambda: request_plane(
                        hexcode), daemon=True).start()
            elif key == 18 and not list_pending and not checking:
                if time.monotonic() < next_refresh:
                    status = f"Wait {math.ceil(next_refresh - time.monotonic())}s before refreshing."
                else:
                    list_pending = True
                    next_refresh = time.monotonic() + 30
                    Thread(target=request_list, daemon=True).start()
            elif key in (curses.KEY_UP, 16):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, 14):
                selected = min(max(0, len(matches) - 1), selected + 1)
            elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                selected = max(0, min(len(matches) - 1, selected +
                               max(1, visible) * (1 if key == curses.KEY_NPAGE else -1)))
            elif key in (curses.KEY_BACKSPACE, 127, 8, 21) or 32 <= key <= 126:
                query = "" if key == 21 else query[:-1] if key in (
                    curses.KEY_BACKSPACE, 127, 8) else (query + chr(key))[:80]
                matches = filter_aircraft(planes, query)
                selected = 0


def main(argv=None):
    """Parse command-line options and run the picker or aircraft dashboard.

    :param argv: Optional argument list; defaults to process arguments.
    :returns: Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifier", nargs="?",
                        help="callsign, registration, or ICAO hex; omit to open the aircraft picker")
    parser.add_argument("--by", choices=["auto", "callsign", "registration", "hex"], default="auto",
                        help="identifier type; override automatic detection when ambiguous")
    parser.add_argument("--interval", type=float, default=5,
                        metavar="SECONDS", help="refresh interval, at least 1 (default: 5)")
    parser.add_argument("--once", action="store_true",
                        help="print one snapshot and exit; exit 1 if unavailable")
    args = parser.parse_args(argv)
    kind, identifier = None, None
    try:
        if args.identifier is not None:
            kind, identifier = identify(args.identifier, args.by)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error("--interval must be a finite number of at least 1 second")

    interactive = sys.stdout.isatty() and os.environ.get(
        "TERM") != "dumb" and not args.once
    if identifier is None and not (interactive and curses is not None and sys.stdin.isatty()):
        parser.error(
            "provide an identifier, or run in an interactive curses terminal to choose an aircraft")
    try:
        if interactive and curses is not None:
            while True:
                try:
                    result = curses.wrapper(run_curses, args, kind, identifier)
                except curses.error as error:
                    print(
                        f"Cannot initialize curses: {error}. Try --once or TERM=dumb.", file=sys.stderr)
                    return 1
                if result != 2:
                    return result
                kind, identifier = None, None
        tracker = Tracker(kind, identifier, args.interval)
        while True:
            tracker.accept(lookup(tracker.kind, tracker.identifier))
            if tracker.route_callsign:
                tracker.route = fetch_route(tracker.route_callsign)
            if args.once:
                tracker.status = tracker.status.replace(
                    " | Retrying", " | Stopped").removesuffix("; retrying")
            width, height = shutil.get_terminal_size()
            print(render(args.identifier, tracker.plane, tracker.timestamp, tracker.status,
                         None if args.once or tracker.stopped else tracker.delay,
                         width=max(1, width - 1), height=max(1, height - 1),
                         trail=tracker.trail, route=tracker.route) + "\n", flush=True)
            if args.once or tracker.stopped:
                return 0 if tracker.success else 1
            time.sleep(tracker.delay)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
