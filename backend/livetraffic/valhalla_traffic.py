"""Live traffic into Valhalla: writes per-edge speeds into its traffic.tar.

Valhalla memory-maps traffic.tar (mjolnir.traffic_extract) MAP_SHARED and
reads it on every request, so writing a record here changes the next route
with no restart. The file is built once with
`valhalla_build_extract -e <tmp.tar> --with-traffic`: one tile per routing
tile, a 32-byte TrafficTileHeader, then one 8-byte TrafficSpeed per directed
edge. Layouts follow valhalla/baldr/traffictile.h exactly.

Live speeds only apply to requests with date_time type 0 ("current").
"""

import mmap
import os
import struct
import tarfile
import threading
import time

TRAFFIC_TAR = os.environ.get("VALHALLA_TRAFFIC_TAR",
                             os.path.expanduser("~/geo/valhalla/custom_files/traffic.tar"))

HEADER = struct.Struct("<QQIIII")   # tile_id, last_update, directed_edge_count, version, spare2, spare3
SPEED = struct.Struct("<Q")
UNKNOWN_SPEED_RAW = 127             # (1 << 7) - 1
MAX_CONGESTION = 63


def split_graph_id(value):
    """Valhalla GraphId value -> (level, tile id, edge index)."""
    return value & 0x7, (value >> 3) & 0x3FFFFF, (value >> 25) & 0x1FFFFF


def pack_speed(kph=None, closed=False, congestion=0, incident=False):
    """One TrafficSpeed record covering the whole edge (breakpoint1 = 255).

    kph None and not closed -> all zero: "no live data", so Valhalla falls
    back to predicted / free-flow speeds for the edge.
    """
    if closed:
        raw, congestion = 0, MAX_CONGESTION
    elif kph is None:
        return 0
    else:
        raw = max(1, min(UNKNOWN_SPEED_RAW - 1, int(round(kph / 2))))
    congestion = max(0, min(MAX_CONGESTION, int(congestion)))
    return (raw                  # overall_encoded_speed : 7
            | raw << 7           # encoded_speed1 : 7
            | 255 << 28          # breakpoint1 : 8 (speed1 covers the whole edge)
            | congestion << 44   # congestion1 : 6
            | int(incident) << 62)


def unpack_speed(bits):
    return {"kph": (bits & 0x7F) * 2, "breakpoint1": (bits >> 28) & 0xFF,
            "congestion": (bits >> 44) & 0x3F, "incident": bool(bits >> 62 & 1)}


class TrafficTar:
    """The traffic.tar Valhalla serves, open for in-place edge writes."""

    def __init__(self, path=TRAFFIC_TAR):
        self.path = path
        self._lock = threading.Lock()
        self._tiles = {}  # (level, tile id) -> byte offset of the tile's header
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                if member.name.endswith(".gph"):
                    self._tiles[self._tile_key(member.name)] = member.offset_data
        self._file = open(path, "r+b")
        self._map = mmap.mmap(self._file.fileno(), 0)

    @staticmethod
    def _tile_key(name):
        # "2/000/747/701.gph" -> (2, 747701)
        parts = name[:-4].split("/")
        return int(parts[0]), int("".join(parts[1:]))

    def _record_offset(self, edge_value):
        level, tile, index = split_graph_id(edge_value)
        base = self._tiles.get((level, tile))
        if base is None:
            raise KeyError(f"no traffic tile for level {level} tile {tile}")
        count = HEADER.unpack_from(self._map, base)[2]
        if index >= count:
            raise IndexError(f"edge {index} outside tile ({count} edges)")
        return base, base + HEADER.size + SPEED.size * index

    def set_edges(self, records):
        """records: {edge GraphId value: packed TrafficSpeed bits}. Bumps each tile's last_update."""
        now = int(time.time())
        with self._lock:
            touched = set()
            for edge, bits in records.items():
                base, offset = self._record_offset(edge)
                SPEED.pack_into(self._map, offset, bits)
                touched.add(base)
            for base in touched:
                struct.pack_into("<Q", self._map, base + 8, now)
            self._map.flush()

    def get_edge(self, edge_value):
        _, offset = self._record_offset(edge_value)
        return unpack_speed(SPEED.unpack_from(self._map, offset)[0])
