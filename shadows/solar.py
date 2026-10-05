"""Положение солнца по алгоритму NOAA (точность ~0.01° для 1800–2100 гг.).

Без внешних зависимостей. Азимут отсчитывается от севера по часовой стрелке,
высота (altitude) — над горизонтом, с поправкой на атмосферную рефракцию.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Высота центра диска солнца в момент восхода/заката (рефракция + радиус диска).
SUNRISE_ALTITUDE = -0.833


@dataclass(frozen=True)
class SunPosition:
    azimuth: float  # градусы, 0 = север, 90 = восток
    altitude: float  # градусы над горизонтом, с рефракцией

    @property
    def is_up(self) -> bool:
        return self.altitude > 0


def _julian_day(dt: datetime) -> float:
    dt = dt.astimezone(timezone.utc)
    return dt.timestamp() / 86400.0 + 2440587.5


def _refraction(elevation: float) -> float:
    """Поправка на рефракцию в градусах (формула NOAA)."""
    if elevation > 85:
        return 0.0
    te = math.tan(math.radians(elevation))
    if elevation > 5:
        r = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
    elif elevation > -0.575:
        r = 1735 + elevation * (-518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711)))
    else:
        r = -20.772 / te
    return r / 3600.0


def _geometry(dt: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(азимут, геометрическая высота без рефракции) в градусах."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")

    jd = _julian_day(dt)
    t = (jd - 2451545.0) / 36525.0

    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (
        math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
        + math.sin(3 * mr) * 0.000289
    )
    true_long = l0 + c
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = true_long - 0.00569 - 0.00478 * math.sin(omega)
    obliq0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    obliq = math.radians(obliq0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(math.radians(app_long)))

    y = math.tan(obliq / 2) ** 2
    l0r = math.radians(l0)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr)
    )

    utc = dt.astimezone(timezone.utc)
    minutes = utc.hour * 60 + utc.minute + utc.second / 60 + utc.microsecond / 6e7
    true_solar_time = (minutes + eq_time + 4 * lon) % 1440
    hour_angle = math.radians(true_solar_time / 4 - 180)

    phi = math.radians(lat)
    cos_zenith = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    elevation = 90 - zenith

    azimuth = (
        math.degrees(
            math.atan2(
                math.sin(hour_angle),
                math.cos(hour_angle) * math.sin(phi) - math.tan(decl) * math.cos(phi),
            )
        )
        + 180
    ) % 360

    return azimuth, elevation


def sun_position(dt: datetime, lat: float, lon: float) -> SunPosition:
    """Азимут и видимая высота солнца для момента ``dt`` (aware datetime) в точке lat/lon."""
    azimuth, elevation = _geometry(dt, lat, lon)
    return SunPosition(azimuth=azimuth, altitude=elevation + _refraction(elevation))


def _geometric_altitude(dt: datetime, lat: float, lon: float) -> float:
    # Рефракция для восхода/заката уже учтена в константе SUNRISE_ALTITUDE.
    return _geometry(dt, lat, lon)[1]


def _find_crossing(t0: datetime, t1: datetime, lat: float, lon: float) -> datetime:
    a0 = _geometric_altitude(t0, lat, lon) - SUNRISE_ALTITUDE
    for _ in range(30):
        mid = t0 + (t1 - t0) / 2
        am = _geometric_altitude(mid, lat, lon) - SUNRISE_ALTITUDE
        if (am > 0) == (a0 > 0):
            t0, a0 = mid, am
        else:
            t1 = mid
        if t1 - t0 < timedelta(seconds=5):
            break
    return t0 + (t1 - t0) / 2


@dataclass(frozen=True)
class SunDay:
    sunrise: datetime | None
    sunset: datetime | None
    solar_noon: datetime
    noon_altitude: float
    polar: str | None  # "day" — полярный день, "night" — полярная ночь


def sun_day(day: date, lat: float, lon: float, tz: ZoneInfo) -> SunDay:
    """Восход, закат и истинный полдень для локальной даты ``day``."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    step = timedelta(minutes=10)
    samples = [start + step * i for i in range(int(timedelta(days=1) / step) + 1)]
    alts = [_geometric_altitude(t, lat, lon) - SUNRISE_ALTITUDE for t in samples]

    sunrise = sunset = None
    for i in range(len(samples) - 1):
        if alts[i] <= 0 < alts[i + 1] and sunrise is None:
            sunrise = _find_crossing(samples[i], samples[i + 1], lat, lon)
        elif alts[i] > 0 >= alts[i + 1]:
            sunset = _find_crossing(samples[i], samples[i + 1], lat, lon)

    noon_idx = max(range(len(samples)), key=lambda i: alts[i])
    lo = samples[max(noon_idx - 1, 0)]
    hi = samples[min(noon_idx + 1, len(samples) - 1)]
    # Тернарный поиск максимума высоты.
    for _ in range(40):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        if _geometric_altitude(m1, lat, lon) < _geometric_altitude(m2, lat, lon):
            lo = m1
        else:
            hi = m2
    noon = lo + (hi - lo) / 2

    polar = None
    if sunrise is None and sunset is None:
        polar = "day" if alts[0] > 0 else "night"

    return SunDay(
        sunrise=sunrise,
        sunset=sunset,
        solar_noon=noon,
        noon_altitude=sun_position(noon, lat, lon).altitude,
        polar=polar,
    )
