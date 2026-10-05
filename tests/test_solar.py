from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from shadows.solar import sun_day, sun_position

EKB = (56.8380, 60.6057)
EKB_TZ = ZoneInfo("Asia/Yekaterinburg")


def test_summer_solstice_noon_altitude():
    # Полуденная высота ≈ 90 − φ + 23.44°.
    day = sun_day(date(2026, 6, 21), *EKB, EKB_TZ)
    assert day.noon_altitude == pytest.approx(90 - EKB[0] + 23.44, abs=0.1)
    assert day.solar_noon.hour == 12 and 55 <= day.solar_noon.minute <= 59


def test_sunrise_sunset_ekaterinburg():
    # Справочные значения (timeanddate.com): 21.06 — 04:03 / 21:56, 21.12 — 09:33 / 16:17.
    summer = sun_day(date(2026, 6, 21), *EKB, EKB_TZ)
    winter = sun_day(date(2026, 12, 21), *EKB, EKB_TZ)
    assert _minutes(summer.sunrise) == pytest.approx(4 * 60 + 3, abs=3)
    assert _minutes(summer.sunset) == pytest.approx(21 * 60 + 56, abs=3)
    assert _minutes(winter.sunrise) == pytest.approx(9 * 60 + 33, abs=3)
    assert _minutes(winter.sunset) == pytest.approx(16 * 60 + 17, abs=3)


def test_azimuth_quadrants():
    morning = sun_position(datetime(2026, 3, 20, 9, 0, tzinfo=EKB_TZ), *EKB)
    noon = sun_position(datetime(2026, 3, 20, 13, 0, tzinfo=EKB_TZ), *EKB)
    evening = sun_position(datetime(2026, 3, 20, 17, 0, tzinfo=EKB_TZ), *EKB)
    assert 90 < morning.azimuth < 180
    assert noon.azimuth == pytest.approx(180, abs=5)
    assert 180 < evening.azimuth < 270
    night = sun_position(datetime(2026, 3, 20, 23, 0, tzinfo=EKB_TZ), *EKB)
    assert not night.is_up


def test_reference_point_greenwich():
    # Эталон NOAA Solar Calculator: Гринвич, 2020-06-21 12:00 UTC — az ≈ 179.6°, el ≈ 61.9°.
    pos = sun_position(datetime(2020, 6, 21, 12, 0, tzinfo=timezone.utc), 51.4779, 0.0)
    assert pos.altitude == pytest.approx(61.98, abs=0.1)
    assert pos.azimuth == pytest.approx(179.6, abs=0.6)


def test_polar_day_and_night():
    tz = ZoneInfo("Arctic/Longyearbyen")
    assert sun_day(date(2026, 6, 21), 78.22, 15.65, tz).polar == "day"
    assert sun_day(date(2026, 12, 21), 78.22, 15.65, tz).polar == "night"


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        sun_position(datetime(2026, 1, 1, 12), *EKB)


def _minutes(dt):
    return dt.hour * 60 + dt.minute + dt.second / 60
