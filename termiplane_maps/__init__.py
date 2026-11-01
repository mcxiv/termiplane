"""Bundled geographic and airport data; see README.md for provenance."""

from functools import lru_cache
import gzip
from importlib.resources import files
import json


@lru_cache(maxsize=5)
def layer(name):
    with files(__package__).joinpath(f"{name}.json.gz").open("rb") as source:
        return json.loads(gzip.decompress(source.read()))


def intersects(box, west, east, north, south):
    left, bottom, right, top = box
    return top >= south and bottom <= north and any(
        right + offset >= west and left + offset <= east for offset in (-360, 0, 360)
    )
