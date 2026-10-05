"""HTTP API и раздача фронтенда."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import box
from timezonefinder import TimezoneFinder

from .buildings import BuildingSource, create_source
from .engine import (
    MAX_SHADOW_LENGTH,
    M_PER_DEG_LAT,
    M_PER_DEG_LON_EQ,
    PointScene,
    day_timeline,
    geometry_to_feature_collection,
    shadow_polygons,
    sun_minutes,
)
from .solar import sun_day, sun_position

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

MAX_BBOX_DEG = 0.08  # ограничение размера запроса (~9 км по широте)
SHADOW_MARGIN_M = 400.0  # здания за пределами экрана, чьи тени в него попадают

app = FastAPI(title="Building shadows", version="0.1.0")

_source: BuildingSource | None = None
_tf = TimezoneFinder()


def get_source() -> BuildingSource:
    global _source
    if _source is None:
        _source = create_source()
    return _source


def set_source(source: BuildingSource) -> None:
    """Подмена источника (для тестов)."""
    global _source
    _source = source


@lru_cache(maxsize=1024)
def _tz_for(lat_r: float, lon_r: float) -> str:
    return _tf.timezone_at(lat=lat_r, lng=lon_r) or "UTC"


def tz_at(lat: float, lon: float, override: str | None = None) -> ZoneInfo:
    if override:
        try:
            return ZoneInfo(override)
        except Exception as exc:
            raise HTTPException(400, f"Неизвестный часовой пояс: {override}") from exc
    return ZoneInfo(_tz_for(round(lat, 2), round(lon, 2)))


def parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError as exc:
        raise HTTPException(400, "bbox должен быть 'west,south,east,north'") from exc
    if not (w < e and s < n and -180 <= w <= 180 and -90 <= s <= 90):
        raise HTTPException(400, "Некорректный bbox")
    if e - w > MAX_BBOX_DEG or n - s > MAX_BBOX_DEG:
        raise HTTPException(413, "Слишком большая область — приблизьте карту")
    return w, s, e, n


def expand_bbox(bbox, meters: float):
    w, s, e, n = bbox
    lat = (s + n) / 2
    dlat = meters / M_PER_DEG_LAT
    dlon = meters / (M_PER_DEG_LON_EQ * math.cos(math.radians(lat)))
    return w - dlon, s - dlat, e + dlon, n + dlat


def local_datetime(d: date, t: str, tz: ZoneInfo) -> datetime:
    try:
        tm = time.fromisoformat(t)
    except ValueError as exc:
        raise HTTPException(400, "time должен быть в формате HH:MM") from exc
    return datetime.combine(d, tm, tzinfo=tz)


def _load(bbox):
    try:
        return get_source().query(bbox)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


def sun_info(lat: float, lon: float, dt: datetime) -> dict:
    pos = sun_position(dt, lat, lon)
    day = sun_day(dt.date(), lat, lon, dt.tzinfo)
    fmt = lambda x: x.strftime("%H:%M") if x else None  # noqa: E731
    return {
        "azimuth": round(pos.azimuth, 2),
        "altitude": round(pos.altitude, 2),
        "is_up": pos.is_up,
        "sunrise": fmt(day.sunrise),
        "sunset": fmt(day.sunset),
        "solar_noon": fmt(day.solar_noon),
        "noon_altitude": round(day.noon_altitude, 2),
        "polar": day.polar,
        "timezone": str(dt.tzinfo),
        "local_time": dt.isoformat(),
    }


# -------------------------------------------------------------------- routes


@app.get("/api/info")
def info():
    src = get_source()
    return {"source": src.name, "attribution": src.attribution}


@app.get("/api/sun")
def api_sun(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    date_: date = Query(..., alias="date"),
    time_: str = Query("12:00", alias="time"),
    tz: str | None = None,
):
    dt = local_datetime(date_, time_, tz_at(lat, lon, tz))
    return sun_info(lat, lon, dt)


@app.get("/api/sun/day")
def api_sun_day(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    date_: date = Query(..., alias="date"),
    step: int = Query(5, ge=1, le=60),
    tz: str | None = None,
):
    """Положение солнца на весь день с шагом ``step`` минут — для отрисовки теней на клиенте."""
    zone = tz_at(lat, lon, tz)
    start = datetime.combine(date_, time(0, 0), tzinfo=zone)
    samples = []
    for minute in range(0, 24 * 60, step):
        pos = sun_position(start + timedelta(minutes=minute), lat, lon)
        samples.append({"minute": minute, "azimuth": round(pos.azimuth, 3), "altitude": round(pos.altitude, 3)})
    return {"lat": lat, "lon": lon, "date": date_.isoformat(), "timezone": str(zone), "step": step, "samples": samples}


@app.get("/api/buildings")
def api_buildings(bbox: str):
    bb = parse_bbox(bbox)
    buildings = _load(bb)
    return {"type": "FeatureCollection", "features": [b.to_feature() for b in buildings]}


@app.get("/api/shadows")
def api_shadows(
    bbox: str,
    date_: date = Query(..., alias="date"),
    time_: str = Query(..., alias="time"),
    tz: str | None = None,
):
    bb = parse_bbox(bbox)
    lat, lon = (bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2
    dt = local_datetime(date_, time_, tz_at(lat, lon, tz))
    sun = sun_position(dt, lat, lon)
    buildings = _load(expand_bbox(bb, SHADOW_MARGIN_M))
    clip = box(*expand_bbox(bb, 50))
    shadows = shadow_polygons(buildings, sun, clip=clip)
    fc = geometry_to_feature_collection(shadows)
    fc["sun"] = {"azimuth": round(sun.azimuth, 2), "altitude": round(sun.altitude, 2), "is_up": sun.is_up}
    return fc


def _scene(lat: float, lon: float, observer_height: float) -> PointScene:
    buildings = _load(expand_bbox((lon, lat, lon, lat), MAX_SHADOW_LENGTH))
    return PointScene(lat, lon, buildings, observer_height=observer_height)


def _building_ref(b, distance=None):
    if b is None:
        return None
    return {
        "id": b.id,
        "name": b.name,
        "height": round(b.height, 1),
        "height_source": b.height_source,
        "distance": None if distance is None else round(distance, 1),
    }


@app.get("/api/point")
def api_point(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    date_: date = Query(..., alias="date"),
    time_: str = Query(..., alias="time"),
    height: float = Query(1.5, ge=0, le=500, description="Высота наблюдателя над землёй, м"),
    step: int = Query(5, ge=1, le=60),
    tz: str | None = None,
):
    zone = tz_at(lat, lon, tz)
    dt = local_datetime(date_, time_, zone)
    scene = _scene(lat, lon, height)
    now = scene.check(sun_position(dt, lat, lon))
    timeline = day_timeline(scene, date_, zone, step)
    return {
        "lat": lat,
        "lon": lon,
        "observer_height": height,
        "state": now.state,
        "blocker": _building_ref(now.blocker, now.blocker_distance),
        "sun": sun_info(lat, lon, dt),
        "step": step,
        "timeline": timeline,
        "sun_minutes": sun_minutes(timeline, step),
        "inside": _building_ref(scene.inside),
    }


@app.get("/api/point/year")
def api_point_year(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    year: int = Query(..., ge=1900, le=2100),
    height: float = Query(1.5, ge=0, le=500),
    tz: str | None = None,
):
    """Часы прямого солнца в точке на 15-е число каждого месяца."""
    zone = tz_at(lat, lon, tz)
    scene = _scene(lat, lon, height)
    step = 10
    months = []
    for m in range(1, 13):
        d = date(year, m, 15)
        tl = day_timeline(scene, d, zone, step)
        daylight = sum(step for t in tl if t["state"] != "night")
        months.append({"month": m, "date": d.isoformat(), "sun_minutes": sun_minutes(tl, step), "daylight_minutes": daylight})
    return {"lat": lat, "lon": lon, "observer_height": height, "months": months}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
