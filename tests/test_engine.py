import math

import pytest
from shapely.geometry import Point, Polygon

from shadows.buildings import Building, estimate_height, parse_length, parse_overpass
from shadows.engine import M_PER_DEG_LAT, M_PER_DEG_LON_EQ, PointScene, building_shadow, shadow_polygons, shadow_vector
from shadows.solar import SunPosition

LAT0, LON0 = 56.8380, 60.6057
KX = M_PER_DEG_LON_EQ * math.cos(math.radians(LAT0))


def local(x, y):
    """Метры от (LON0, LAT0) -> lon/lat."""
    return LON0 + x / KX, LAT0 + y / M_PER_DEG_LAT


def square(x0, y0, size, height, min_height=0.0, id_="b"):
    poly = Polygon([local(x0, y0), local(x0 + size, y0), local(x0 + size, y0 + size), local(x0, y0 + size)])
    return Building(id=id_, geometry=poly, height=height, min_height=min_height, height_source="tag")


SUN_SOUTH_45 = SunPosition(azimuth=180.0, altitude=45.0)


def test_shadow_vector_points_away_from_sun():
    vx, vy = shadow_vector(SUN_SOUTH_45)
    assert vx == pytest.approx(0, abs=1e-9)
    assert vy == pytest.approx(1.0)  # тень на север, длина = высоте при 45°
    vx, vy = shadow_vector(SunPosition(azimuth=90.0, altitude=45.0))
    assert vx == pytest.approx(-1.0) and vy == pytest.approx(0, abs=1e-9)


def test_box_shadow_extends_north_by_height():
    b = square(0, 0, 10, height=20)
    vx, vy = shadow_vector(SUN_SOUTH_45)
    shadow = building_shadow(b, vx, vy)
    _, _, _, maxy = shadow.bounds
    assert (maxy - LAT0) * M_PER_DEG_LAT == pytest.approx(30, abs=0.05)  # 10 м здания + 20 м тени
    # Площадь: 10 м × 30 м.
    area_m2 = shadow.area * KX * M_PER_DEG_LAT
    assert area_m2 == pytest.approx(300, rel=0.01)


def test_elevated_building_leaves_gap():
    # Консоль на высоте 10..20 м: тень начинается в 10 м от основания.
    b = square(0, 0, 10, height=20, min_height=10)
    vx, vy = shadow_vector(SUN_SOUTH_45)
    shadow = building_shadow(b, vx, vy)
    assert not shadow.contains(Point(local(5, 5)))
    assert shadow.contains(Point(local(5, 15)))
    assert shadow.contains(Point(local(5, 29)))


def test_night_has_no_shadows():
    b = square(0, 0, 10, height=20)
    assert shadow_polygons([b], SunPosition(azimuth=0, altitude=-5)).is_empty


def test_point_shade_and_sun():
    b = square(0, 0, 10, height=20)
    scene_north = PointScene(*_latlon(5, 20), [b], observer_height=0)
    scene_south = PointScene(*_latlon(5, -5), [b], observer_height=0)
    st = scene_north.check(SUN_SOUTH_45)
    assert st.state == "shade" and st.blocker is b
    assert st.blocker_distance == pytest.approx(10, abs=0.1)
    assert scene_south.check(SUN_SOUTH_45).state == "sun"


def test_point_beyond_shadow_is_sunny():
    b = square(0, 0, 10, height=20)
    scene = PointScene(*_latlon(5, 31), [b], observer_height=0)
    assert scene.check(SUN_SOUTH_45).state == "sun"


def test_observer_height_above_roof_sees_sun():
    b = square(0, 0, 10, height=20)
    scene = PointScene(*_latlon(5, 15), [b], observer_height=25)
    assert scene.check(SUN_SOUTH_45).state == "sun"


def test_point_inside_building():
    b = square(0, 0, 10, height=20)
    scene = PointScene(*_latlon(5, 5), [b], observer_height=1.5)
    assert scene.check(SUN_SOUTH_45).state == "inside"


def test_point_under_canopy():
    # Навес 3..4 м прямо над точкой закрывает солнце в зените.
    b = square(-5, -5, 10, height=4, min_height=3)
    scene = PointScene(LAT0, LON0, [b], observer_height=1.5)
    assert scene.check(SunPosition(azimuth=180, altitude=80)).state == "shade"


def test_point_agrees_with_shadow_polygon():
    buildings = [square(0, 0, 12, 30, id_="a"), square(30, 15, 8, 12, id_="b"), square(-25, 40, 15, 45, id_="c")]
    sun = SunPosition(azimuth=215.0, altitude=28.0)
    shadow = shadow_polygons(buildings, sun)
    for x in range(-40, 80, 7):
        for y in range(-20, 120, 7):
            lon, lat = local(x + 0.3, y + 0.3)
            if any(b.geometry.contains(Point(lon, lat)) for b in buildings):
                continue
            in_poly = shadow.contains(Point(lon, lat))
            state = PointScene(lat, lon, buildings, observer_height=0).check(sun).state
            assert in_poly == (state == "shade"), (x, y)


def _latlon(x, y):
    lon, lat = local(x, y)
    return lat, lon


# --------------------------------------------------------------- heights


@pytest.mark.parametrize(
    "value, expected",
    [("12", 12.0), ("12.5 m", 12.5), ("12,5м", 12.5), ("40'", 12.192), ("40 ft", 12.192), (7, 7.0), ("abc", None), (None, None)],
)
def test_parse_length(value, expected):
    result = parse_length(value)
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_estimate_height_sources():
    assert estimate_height({"height": "25"})[2] == "tag"
    h, mh, src, lv = estimate_height({"building:levels": "9"})
    assert (h, mh, src, lv) == (27.0, 0.0, "levels", 9)
    h, _, src, _ = estimate_height({"building": "garage"})
    assert (h, src) == (3.0, "default")
    # Overture: num_floors / min_height
    h, mh, src, _ = estimate_height({"num_floors": 5, "min_height": 3})
    assert (h, mh, src) == (15.0, 3.0, "levels")


def test_parse_overpass_way_and_relation():
    ring = lambda pts: [{"lat": lat, "lon": lon} for lon, lat in pts]  # noqa: E731
    outer = [(0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001), (0, 0)]
    inner = [(0.0004, 0.0004), (0.0006, 0.0004), (0.0006, 0.0006), (0.0004, 0.0006), (0.0004, 0.0004)]
    data = {
        "elements": [
            {"type": "way", "id": 1, "tags": {"building": "yes", "building:levels": "5"}, "geometry": ring(outer)},
            {
                "type": "relation",
                "id": 2,
                "tags": {"building": "yes", "type": "multipolygon", "height": "30"},
                "members": [
                    # внешнее кольцо разбито на две линии
                    {"type": "way", "role": "outer", "geometry": ring(outer[:3])},
                    {"type": "way", "role": "outer", "geometry": ring(outer[2:])},
                    {"type": "way", "role": "inner", "geometry": ring(inner)},
                ],
            },
        ]
    }
    way, rel = parse_overpass(data)
    assert way.id == "osm/way/1" and way.height == 15
    assert rel.height == 30 and rel.geometry.area == pytest.approx(1e-6 - 4e-8)


def test_vectorized_shadows_match_reference():
    import shapely

    from shadows.engine import shadow_vector

    court = square(0, 0, 40, 18, id_="court")
    court.geometry = court.geometry.difference(square(10, 10, 20, 1).geometry)  # двор
    ell = square(60, 0, 1, 25, id_="ell")
    ell.geometry = Polygon([local(60, 0), local(90, 0), local(90, 10), local(70, 10), local(70, 30), local(60, 30)])
    buildings = [court, ell, square(0, 60, 12, 30, 6, id_="canopy"), square(40, 60, 9, 9, id_="box")]
    for az, alt in [(200, 25), (95, 10), (300, 40), (180, 60)]:
        sun = SunPosition(azimuth=az, altitude=alt)
        vx, vy = shadow_vector(sun)
        fast = shadow_polygons(buildings, sun)
        ref = shapely.union_all([building_shadow(b, vx, vy) for b in buildings])
        assert fast.symmetric_difference(ref).area / ref.area < 1e-6
