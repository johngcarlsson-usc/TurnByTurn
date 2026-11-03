import math

import polyline
from shapely.geometry import LineString, Point


EARTH_RADIUS_M = 6371008.8


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def decode_shape(shape):
    """Valhalla precision-6 polyline -> list of (lat, lon)."""
    return polyline.decode(shape, 6)


class LocalProjection:
    """Equirectangular projection to metres around a reference latitude.

    Accurate to well under 1% across a metro area, which is all the corridor
    and buffer maths here needs, and it keeps shapely distances in metres.
    """

    def __init__(self, ref_lat):
        self.kx = 111320.0 * math.cos(math.radians(ref_lat))
        self.ky = 110540.0

    def xy(self, lat, lon):
        return lon * self.kx, lat * self.ky

    def latlon(self, x, y):
        return y / self.ky, x / self.kx

    def line(self, latlons):
        return LineString([self.xy(lat, lon) for lat, lon in latlons])

    def point(self, lat, lon):
        return Point(self.xy(lat, lon))

    def polygon_to_lonlat(self, polygon):
        return [[x / self.kx, y / self.ky] for x, y in polygon.exterior.coords]


def circle_lonlat(lat, lon, radius_m, segments=32):
    """A circle as a closed [lon, lat] ring, for Valhalla and for display."""
    proj = LocalProjection(lat)
    poly = proj.point(lat, lon).buffer(radius_m, quad_segs=max(4, segments // 4))
    return proj.polygon_to_lonlat(poly)
