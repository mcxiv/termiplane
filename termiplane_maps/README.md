Map data derived from Natural Earth v5.1.2, public domain:
https://www.naturalearthdata.com/about/terms-of-use/

Source files in https://github.com/nvkelso/natural-earth-vector/tree/v5.1.2/geojson:

- ne_50m_land.geojson
- ne_10m_land.geojson
- ne_10m_admin_0_boundary_lines_land.geojson
- ne_10m_populated_places_simple.geojson

The gzip JSON files retain coordinates rounded to four decimal degrees.
Land and border records store a bounding box followed by polygon rings or line
coordinates. Polygon holes are preserved. City records store scale rank,
negative population (for sorting), an ASCII name, longitude, and latitude.
Unused source properties are omitted. gzip timestamps are zero for reproducibility.

Natural Earth uses 50m/10m to mean map scales 1:50 million / 1:10 million,
not positional accuracy in meters. Boundaries and place data are generalized
and reflect this dataset's release, not current street or navigational data.

Airport data (`airports.json.gz`) derives from OurAirports, public domain:
https://ourairports.com/data/

Source CSV:
https://github.com/davidmegginson/ourairports-data/blob/28681fe715b46ce549cee84368a64c2101038fc1/airports.csv

Source timestamp: 2026-10-31T02:53:34Z
CSV SHA256: 6c600b7821829c2bd672c97b0c1e25b51a12e1e281fbd6fb1b698156b17d6acb

48,057 records retain identifiers, ASCII-transliterated airport names, latitude,
and longitude. Only small_airport, medium_airport, and large_airport records with
valid coordinates are included. Identifiers use IATA and ICAO/GPS codes when
available, otherwise the source ident. Closed facilities, heliports, seaplane
bases, and balloonports are excluded. The snapshot is not operational airport data.
