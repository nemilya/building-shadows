"""Расчёт теней от зданий-призм на плоской земле.

* ``shadow_polygons`` — тени на земле для набора зданий (для карты);
* ``PointScene`` — проверка «солнце/тень» в точке лучом к солнцу и дневной таймлайн.

Здание — вертикальная призма: основание ``geometry`` на высоте ``min_height``,
верх на ``height``. Тень призмы — сумма Минковского основания и отрезка
[min_height·v, height·v], где v — смещение тени на 1 м высоты.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping
from shapely.affinity import translate

from .buildings import Building
from .solar import SunPosition, sun_position

M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON_EQ = 111_320.0

MAX_SHADOW_LENGTH = 800.0  # м; ограничивает тени при солнце у горизонта
MIN_ALTITUDE = 0.5  # град; ниже считаем, что солнца нет (без рельефа)


def shadow_vector(sun: SunPosition) -> tuple[float, float]:
    """Смещение тени (east, north) в метрах на 1 м высоты объекта."""
    alt = max(sun.altitude, MIN_ALTITUDE)
    length = 1.0 / math.tan(math.radians(alt))
    az = math.radians(sun.azimuth)
    # Тень направлена от солнца.
    return -math.sin(az) * length, -math.cos(az) * length


def _rings(geom) -> list[np.ndarray]:
    polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    out = []
    for p in polys:
        out.append(np.asarray(p.exterior.coords)[:, :2])
        out.extend(np.asarray(r.coords)[:, :2] for r in p.interiors)
    return out


def building_shadow(b: Building, vx: float, vy: float, max_len: float = MAX_SHADOW_LENGTH):
    """Тень одного здания на земле (lon/lat). vx, vy — м на 1 м высоты."""
    lat = b.geometry.centroid.y
    kx = 1.0 / (M_PER_DEG_LON_EQ * math.cos(math.radians(lat)))
    ky = 1.0 / M_PER_DEG_LAT

    unit = math.hypot(vx, vy)
    h_top = b.height
    h_low = b.min_height
    if unit * h_top > max_len:
        h_top = max_len / unit
        h_low = min(h_low, h_top)
    lo = np.array([vx * h_low * kx, vy * h_low * ky])
    hi = np.array([vx * h_top * kx, vy * h_top * ky])

    parts = [translate(b.geometry, *lo), translate(b.geometry, *hi)]
    quads = []
    for ring in _rings(b.geometry):
        a, c = ring[:-1], ring[1:]
        quads.append(np.stack([a + lo, c + lo, c + hi, a + hi, a + lo], axis=1))
    if quads:
        q = shapely.polygons(np.concatenate(quads))
        q = q[shapely.area(q) > 1e-14]
        parts.extend(q)
    return shapely.union_all(parts)


def shadow_polygons(buildings: list[Building], sun: SunPosition, clip=None, max_len: float = MAX_SHADOW_LENGTH):
    """Объединённая геометрия теней всех зданий (или пустая, если солнце село).

    Векторизованный вариант ``building_shadow``: для выпуклых оснований тень —
    выпуклая оболочка нижнего и верхнего «отпечатков», для остальных добавляются
    четырёхугольники, заметаемые рёбрами. Всё объединяется одним union_all.
    """
    if not sun.is_up or not buildings:
        return shapely.Polygon()
    vx, vy = shadow_vector(sun)
    unit = math.hypot(vx, vy)

    geoms = np.array([b.geometry for b in buildings], dtype=object)
    h = np.array([b.height for b in buildings])
    hmin = np.minimum([b.min_height for b in buildings], h)
    h_top = np.minimum(h, max_len / unit)
    h_low = np.minimum(hmin, h_top)

    lat = shapely.get_y(shapely.centroid(geoms))
    kx = 1.0 / (M_PER_DEG_LON_EQ * np.cos(np.radians(lat)))
    ky = 1.0 / M_PER_DEG_LAT
    off_lo = np.column_stack([vx * h_low * kx, vy * h_low * ky])
    off_hi = np.column_stack([vx * h_top * kx, vy * h_top * ky])

    if clip is not None:
        # Отбрасываем здания, чья тень заведомо не попадает в область.
        b = shapely.bounds(geoms)
        cw, cs, ce, cn = clip.bounds
        reach_w = b[:, 0] + np.minimum(off_lo[:, 0], off_hi[:, 0])
        reach_e = b[:, 2] + np.maximum(off_lo[:, 0], off_hi[:, 0])
        reach_s = b[:, 1] + np.minimum(off_lo[:, 1], off_hi[:, 1])
        reach_n = b[:, 3] + np.maximum(off_lo[:, 1], off_hi[:, 1])
        keep = (reach_e >= cw) & (reach_w <= ce) & (reach_n >= cs) & (reach_s <= cn)
        if not keep.any():
            return shapely.Polygon()
        geoms, off_lo, off_hi = geoms[keep], off_lo[keep], off_hi[keep]

    coords, idx = shapely.get_coordinates(geoms, return_index=True)
    lo = shapely.set_coordinates(geoms.copy(), coords + off_lo[idx])
    hi = shapely.set_coordinates(geoms.copy(), coords + off_hi[idx])

    hull_area = shapely.area(shapely.convex_hull(geoms))
    convex = shapely.area(geoms) >= hull_area * 0.9999

    parts = []
    if convex.any():
        pairs = shapely.multipolygons(np.column_stack([lo[convex], hi[convex]]).tolist())
        parts.append(shapely.convex_hull(pairs))
    rest = np.flatnonzero(~convex)
    if len(rest):
        parts.append(lo[rest])
        parts.append(hi[rest])
        # Луч вдоль v входит в основание и выходит из него, поэтому достаточно
        # рёбер одной стороны: тех, у которых внешняя нормаль смотрит по v.
        quads = []
        for i in rest:
            v = off_hi[i] - off_lo[i]
            for ring in _rings(shapely.orient_polygons(geoms[i])):
                a, c = ring[:-1], ring[1:]
                d = c - a
                keep = d[:, 1] * v[0] - d[:, 0] * v[1] > 0
                a, c = a[keep], c[keep]
                quads.append(np.stack([a + off_lo[i], c + off_lo[i], c + off_hi[i], a + off_hi[i], a + off_lo[i]], axis=1))
        q = shapely.polygons(np.concatenate(quads))
        parts.append(q[shapely.area(q) > 1e-14])

    merged = shapely.union_all(np.concatenate(parts))
    if clip is not None:
        merged = merged.intersection(clip)
    return merged


def geometry_to_feature_collection(geom, precision: float = 1e-7) -> dict:
    if geom.is_empty:
        return {"type": "FeatureCollection", "features": []}
    geom = shapely.set_precision(geom, precision)
    polys = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": mapping(p), "properties": {}}
            for p in polys
            if isinstance(p, Polygon) and not p.is_empty
        ],
    }


# ---------------------------------------------------------------- point query


@dataclass
class PointState:
    state: str  # sun | shade | night | inside
    sun: SunPosition
    blocker: Building | None = None
    blocker_distance: float | None = None


class PointScene:
    """Окрестность точки в локальной метрической системе для быстрых лучевых проверок."""

    def __init__(self, lat: float, lon: float, buildings: list[Building], observer_height: float = 1.5):
        self.lat, self.lon = lat, lon
        self.observer_height = observer_height
        self._kx = M_PER_DEG_LON_EQ * math.cos(math.radians(lat))
        self._ky = M_PER_DEG_LAT
        self.buildings = buildings
        self._geoms = np.array(
            [
                shapely.transform(b.geometry, lambda c: (c - [lon, lat]) * [self._kx, self._ky])
                for b in buildings
            ],
            dtype=object,
        )
        self._heights = np.array([b.height for b in buildings])
        self._mins = np.array([b.min_height for b in buildings])
        self._tree = shapely.STRtree(self._geoms) if len(buildings) else None
        self.inside: Building | None = None
        origin = Point(0, 0)
        for i, g in enumerate(self._geoms):
            if g.covers(origin) and self._mins[i] <= observer_height <= self._heights[i]:
                self.inside = buildings[i]
                break

    @staticmethod
    def search_radius(sun: SunPosition, max_height: float, observer_height: float = 0.0) -> float:
        rise = max(max_height - observer_height, 0.0)
        alt = max(sun.altitude, MIN_ALTITUDE)
        return min(rise / math.tan(math.radians(alt)), MAX_SHADOW_LENGTH)

    def check(self, sun: SunPosition) -> PointState:
        if sun.altitude <= MIN_ALTITUDE:
            return PointState("night" if sun.altitude <= 0 else "shade", sun)
        if self.inside is not None:
            return PointState("inside", sun, self.inside, 0.0)
        if self._tree is None:
            return PointState("sun", sun)

        tan_alt = math.tan(math.radians(sun.altitude))
        top = float(self._heights.max())
        reach = (top - self.observer_height) / tan_alt
        if reach <= 0:
            return PointState("sun", sun)
        reach = min(reach, MAX_SHADOW_LENGTH * 2)
        az = math.radians(sun.azimuth)
        ux, uy = math.sin(az), math.cos(az)
        ray = LineString([(0, 0), (ux * reach, uy * reach)])

        idx = self._tree.query(ray, predicate="intersects")
        best = None
        for i in idx:
            hit = ray.intersection(self._geoms[i])
            for seg in _segments(hit):
                (x0, y0), (x1, y1) = seg[0], seg[-1]
                d0 = x0 * ux + y0 * uy
                d1 = x1 * ux + y1 * uy
                if d0 > d1:
                    d0, d1 = d1, d0
                z0 = self.observer_height + d0 * tan_alt
                z1 = self.observer_height + d1 * tan_alt
                # Луч поднимается; блокирует, если отрезок луча пересекает [min_h, h].
                if z0 < self._heights[i] and z1 > self._mins[i]:
                    entry = max(d0, (self._mins[i] - self.observer_height) / tan_alt)
                    if best is None or entry < best[1]:
                        best = (i, max(entry, 0.0))
        if best is None:
            return PointState("sun", sun)
        return PointState("shade", sun, self.buildings[best[0]], best[1])


def _segments(geom) -> list[list[tuple[float, float]]]:
    if geom.is_empty:
        return []
    if isinstance(geom, Point):
        return [[(geom.x, geom.y), (geom.x, geom.y)]]
    if isinstance(geom, LineString):
        return [list(geom.coords)]
    out = []
    for g in getattr(geom, "geoms", []):
        out.extend(_segments(g))
    return out


def day_timeline(scene: PointScene, day: date, tz: ZoneInfo, step_minutes: int = 5) -> list[dict]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    items = []
    for k in range(0, 24 * 60, step_minutes):
        t = start + timedelta(minutes=k)
        sun = sun_position(t, scene.lat, scene.lon)
        st = scene.check(sun)
        items.append(
            {
                "minute": k,
                "state": st.state,
                "azimuth": round(sun.azimuth, 2),
                "altitude": round(sun.altitude, 2),
            }
        )
    return items


def sun_minutes(timeline: list[dict], step_minutes: int) -> int:
    return sum(step_minutes for t in timeline if t["state"] == "sun")
