"""Здания: модель, оценка высоты и источники данных (OSM Overpass, GeoJSON-файл, демо)."""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import requests
import shapely
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import polygonize, unary_union

log = logging.getLogger(__name__)

LEVEL_HEIGHT = 3.0  # м на этаж
DEFAULT_LEVELS = 3

# Высоты по умолчанию для типов зданий без высоты и этажности, м.
DEFAULT_HEIGHT_BY_TYPE = {
    "garage": 3,
    "garages": 3,
    "shed": 3,
    "kiosk": 3,
    "carport": 3,
    "hut": 3,
    "toilets": 3,
    "service": 3,
    "transformer_tower": 4,
    "roof": 4,
    "house": 7,
    "detached": 7,
    "semidetached_house": 7,
    "bungalow": 5,
    "cabin": 5,
    "terrace": 7,
    "warehouse": 8,
    "industrial": 8,
    "retail": 6,
    "church": 15,
    "cathedral": 25,
}


@dataclass
class Building:
    id: str
    geometry: Polygon | MultiPolygon  # lon/lat, EPSG:4326
    height: float  # высота верха, м над землёй
    min_height: float = 0.0  # высота низа (для навесов, консолей), м
    height_source: str = "default"  # tag | levels | default
    levels: int | None = None
    kind: str | None = None
    name: str | None = None

    def to_feature(self) -> dict:
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": mapping(shapely.set_precision(self.geometry, 1e-7)),
            "properties": {
                "id": self.id,
                "height": round(self.height, 1),
                "min_height": round(self.min_height, 1),
                "height_source": self.height_source,
                "levels": self.levels,
                "kind": self.kind,
                "name": self.name,
            },
        }


_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_length(value) -> float | None:
    """'12', '12.5 m', '12,5м', "40'", '40 ft' -> метры."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).strip().lower()
    match = _NUM_RE.search(text)
    if not match:
        return None
    number = float(match.group().replace(",", "."))
    if "ft" in text or "'" in text:
        number *= 0.3048
    return number


def parse_int(value) -> int | None:
    number = parse_length(value)
    if number is None:
        return None
    return int(round(number))


def estimate_height(tags: dict) -> tuple[float, float, str, int | None]:
    """По тегам OSM/Overture возвращает (height, min_height, source, levels)."""
    levels = parse_int(tags.get("building:levels") or tags.get("num_floors"))
    min_level = parse_int(tags.get("building:min_level") or tags.get("min_floor"))

    height = parse_length(tags.get("height") or tags.get("building:height"))
    source = "tag"
    if height is None or height <= 0:
        if levels:
            roof_levels = parse_int(tags.get("roof:levels")) or 0
            height = (levels + roof_levels * 0.5) * LEVEL_HEIGHT
            source = "levels"
        else:
            kind = tags.get("building") or tags.get("subtype") or tags.get("class")
            height = float(DEFAULT_HEIGHT_BY_TYPE.get(kind, DEFAULT_LEVELS * LEVEL_HEIGHT))
            source = "default"

    min_height = parse_length(tags.get("min_height"))
    if min_height is None:
        if min_level:
            min_height = min_level * LEVEL_HEIGHT
        elif (tags.get("building") or tags.get("class")) == "roof":
            min_height = max(height - 1.0, 0.0)
        else:
            min_height = 0.0
    min_height = max(0.0, min(min_height, height - 0.1))
    return height, min_height, source, levels


def _tag_name(tags: dict) -> str | None:
    name = tags.get("name")
    if isinstance(name, dict):  # Overture: {"primary": "..."}
        name = name.get("primary")
    if name:
        return str(name)
    street, number = tags.get("addr:street"), tags.get("addr:housenumber")
    if street and number:
        return f"{street}, {number}"
    return None


def make_building(id_: str, geometry, tags: dict) -> Building | None:
    if geometry is None or geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    geometry = _only_polygons(geometry)
    if geometry is None:
        return None
    height, min_height, source, levels = estimate_height(tags)
    return Building(
        id=id_,
        geometry=geometry,
        height=height,
        min_height=min_height,
        height_source=source,
        levels=levels,
        kind=tags.get("building") or tags.get("class") or tags.get("subtype"),
        name=_tag_name(tags),
    )


def _only_polygons(geometry) -> Polygon | MultiPolygon | None:
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry if not geometry.is_empty else None
    polys = [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


class BuildingSource(Protocol):
    name: str
    attribution: str

    def query(self, bbox: tuple[float, float, float, float]) -> list[Building]: ...


class _Index:
    """Пространственный индекс зданий в памяти (STRtree перестраивается лениво)."""

    def __init__(self) -> None:
        self._buildings: dict[str, Building] = {}
        self._tree: shapely.STRtree | None = None
        self._items: list[Building] = []
        self._lock = threading.Lock()

    def add(self, buildings: Iterable[Building]) -> None:
        with self._lock:
            for b in buildings:
                self._buildings[b.id] = b
            self._tree = None

    def query(self, bbox) -> list[Building]:
        with self._lock:
            if self._tree is None:
                self._items = list(self._buildings.values())
                self._tree = shapely.STRtree([b.geometry for b in self._items])
            idx = self._tree.query(box(*bbox))
            return [self._items[i] for i in idx]

    def __len__(self) -> int:
        return len(self._buildings)


# --------------------------------------------------------------------------- OSM


OVERPASS_URLS = [
    u.strip()
    for u in os.environ.get(
        "SHADOWS_OVERPASS_URL",
        "https://overpass-api.de/api/interpreter,https://overpass.private.coffee/api/interpreter",
    ).split(",")
    if u.strip()
]

TILE_DEG = 0.01  # размер тайла кэша, ~1.1 км × 0.6 км на широте Екатеринбурга


def _overpass_query(s: float, w: float, n: float, e: float) -> str:
    bb = f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}"
    return (
        "[out:json][timeout:90];("
        f'way["building"]["building"!="no"]({bb});'
        f'relation["building"]["building"!="no"]["type"="multipolygon"]({bb});'
        ");out geom;"
    )


def _ring_coords(geom: list[dict]) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in geom if p]


def _relation_geometry(element: dict):
    outer, inner = [], []
    for member in element.get("members", []):
        if member.get("type") != "way" or not member.get("geometry"):
            continue
        coords = _ring_coords(member["geometry"])
        if len(coords) < 2:
            continue
        (inner if member.get("role") == "inner" else outer).append(shapely.LineString(coords))
    if not outer:
        return None
    shell = unary_union(list(polygonize(unary_union(outer))))
    if inner:
        holes = unary_union(list(polygonize(unary_union(inner))))
        shell = shell.difference(holes)
    return shell


def parse_overpass(data: dict) -> list[Building]:
    buildings = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        geom = None
        if el["type"] == "way" and el.get("geometry"):
            coords = _ring_coords(el["geometry"])
            if len(coords) >= 4 and coords[0] == coords[-1]:
                geom = Polygon(coords)
        elif el["type"] == "relation":
            geom = _relation_geometry(el)
        b = make_building(f"osm/{el['type']}/{el['id']}", geom, tags)
        if b is not None:
            buildings.append(b)
    return buildings


class OverpassSource:
    """Здания OpenStreetMap через Overpass API с дисковым кэшем по тайлам."""

    name = "osm"
    attribution = "© OpenStreetMap contributors (ODbL)"

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir / "overpass"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._index = _Index()
        self._loaded: set[tuple[int, int]] = set()
        self._tile_locks: dict[tuple[int, int], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "building-shadows-mvp/0.1"

    def _tile_lock(self, key):
        with self._locks_guard:
            return self._tile_locks.setdefault(key, threading.Lock())

    def _fetch_tile(self, tx: int, ty: int) -> dict:
        path = self.cache_dir / f"{tx}_{ty}.json"
        if path.exists():
            return json.loads(path.read_text())
        w, s = tx * TILE_DEG, ty * TILE_DEG
        query = _overpass_query(s, w, s + TILE_DEG, w + TILE_DEG)
        last_error: Exception | None = None
        for url in OVERPASS_URLS:
            try:
                resp = self._session.post(url, data={"data": query}, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                path.write_text(json.dumps(data))
                return data
            except (requests.RequestException, ValueError) as exc:
                log.warning("Overpass %s failed: %s", url, exc)
                last_error = exc
        raise RuntimeError(f"Не удалось загрузить здания из Overpass: {last_error}")

    def _ensure(self, bbox) -> None:
        w, s, e, n = bbox
        for tx in range(math.floor(w / TILE_DEG), math.floor(e / TILE_DEG) + 1):
            for ty in range(math.floor(s / TILE_DEG), math.floor(n / TILE_DEG) + 1):
                key = (tx, ty)
                if key in self._loaded:
                    continue
                with self._tile_lock(key):
                    if key in self._loaded:
                        continue
                    self._index.add(parse_overpass(self._fetch_tile(tx, ty)))
                    self._loaded.add(key)

    def query(self, bbox) -> list[Building]:
        self._ensure(bbox)
        return self._index.query(bbox)


# --------------------------------------------------------------------- GeoJSON


class GeoJSONSource:
    """Здания из локального GeoJSON (например, выгрузка Overture Maps, см. docs/spec.md)."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._index = _Index()
        data = json.loads(path.read_text())
        features = data["features"] if data.get("type") == "FeatureCollection" else [data]
        buildings = []
        sources = set()
        for i, f in enumerate(features):
            props = f.get("properties") or {}
            for src in props.get("sources") or []:
                if isinstance(src, dict) and src.get("dataset"):
                    sources.add(src["dataset"])
            b = make_building(str(f.get("id") or props.get("id") or i), shape(f["geometry"]), props)
            if b is not None:
                buildings.append(b)
        self._index.add(buildings)
        if sources:
            self.attribution = "Данные: " + ", ".join(sorted(sources)) + "; © OpenStreetMap contributors"
        else:
            self.attribution = "Данные из файла " + path.name
        log.info("Loaded %d buildings from %s", len(self._index), path)

    def query(self, bbox) -> list[Building]:
        return self._index.query(bbox)


# ------------------------------------------------------------------------ demo


class DemoSource:
    """Синтетический квартал для работы без сети (центр — Екатеринбург, Плотинка)."""

    name = "demo"
    attribution = "Синтетические данные (демо)"

    def __init__(self, center=(60.6057, 56.8380), size_m=900, seed=42) -> None:
        self._index = _Index()
        rng = random.Random(seed)
        lon0, lat0 = center
        m_lat = 1 / 110_540
        m_lon = 1 / (111_320 * math.cos(math.radians(lat0)))
        buildings = []
        block, street = 90, 25
        n = int(size_m // (block + street))
        for i in range(-n // 2, n // 2 + 1):
            for j in range(-n // 2, n // 2 + 1):
                x0 = i * (block + street)
                y0 = j * (block + street)
                if (i, j) == (0, 0):
                    continue  # площадь
                layout = rng.choice(["perimeter", "towers", "slabs", "houses"])
                for k, (x, y, w, h, levels) in enumerate(_demo_block(rng, layout, block)):
                    poly = Polygon(
                        [
                            (lon0 + (x0 + px) * m_lon, lat0 + (y0 + py) * m_lat)
                            for px, py in [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                        ]
                    )
                    tags = {"building": "apartments", "building:levels": levels}
                    buildings.append(make_building(f"demo/{i}_{j}_{k}", poly, tags))
        self._index.add(b for b in buildings if b)

    def query(self, bbox) -> list[Building]:
        return self._index.query(bbox)


def _demo_block(rng: random.Random, layout: str, size: float):
    if layout == "perimeter":
        lv = rng.choice([5, 6, 9])
        d = 14
        return [
            (0, 0, size, d, lv),
            (0, size - d, size, d, lv),
            (0, d, d, size - 2 * d, lv),
            (size - d, d, d, size - 2 * d, rng.choice([5, 9, 12])),
        ]
    if layout == "towers":
        return [
            (10, 10, 25, 25, rng.choice([16, 20, 25])),
            (size - 40, size - 45, 28, 28, rng.choice([10, 16, 30])),
            (size - 45, 8, 35, 18, 3),
        ]
    if layout == "slabs":
        return [(8, 8 + k * 30, size - 16, 13, rng.choice([5, 9])) for k in range(3)]
    return [
        (8 + (k % 4) * 21, 8 + (k // 4) * 28, 12, 14, rng.choice([1, 2, 2, 3]))
        for k in range(12)
    ]


def create_source() -> BuildingSource:
    kind = os.environ.get("SHADOWS_SOURCE", "auto").lower()
    path = os.environ.get("SHADOWS_BUILDINGS_FILE")
    cache = Path(os.environ.get("SHADOWS_CACHE_DIR", Path(__file__).resolve().parent.parent / "data" / "cache"))
    if kind == "demo":
        return DemoSource()
    if kind == "file" or (kind == "auto" and path):
        if not path:
            raise RuntimeError("SHADOWS_SOURCE=file требует SHADOWS_BUILDINGS_FILE")
        return GeoJSONSource(Path(path))
    return OverpassSource(cache)
