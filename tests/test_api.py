import pytest
from fastapi.testclient import TestClient

from shadows import api
from shadows.buildings import DemoSource

BBOX = "60.598,56.834,60.613,56.842"


@pytest.fixture(scope="module")
def client():
    api.set_source(DemoSource())
    return TestClient(api.app)


def test_index(client):
    assert client.get("/").status_code == 200


def test_buildings(client):
    data = client.get("/api/buildings", params={"bbox": BBOX}).json()
    assert data["type"] == "FeatureCollection" and len(data["features"]) > 50
    props = data["features"][0]["properties"]
    assert {"height", "min_height", "height_source"} <= props.keys()


def test_bbox_validation(client):
    assert client.get("/api/buildings", params={"bbox": "1,2,3"}).status_code == 400
    assert client.get("/api/buildings", params={"bbox": "60,56,61,57"}).status_code == 413


def test_shadows_day_and_night(client):
    day = client.get("/api/shadows", params={"bbox": BBOX, "date": "2026-06-21", "time": "10:00"}).json()
    assert day["sun"]["is_up"] and len(day["features"]) > 0
    night = client.get("/api/shadows", params={"bbox": BBOX, "date": "2026-06-21", "time": "23:30"}).json()
    assert not night["sun"]["is_up"] and night["features"] == []


def test_point_timeline(client):
    data = client.get(
        "/api/point", params={"lat": 56.8385, "lon": 60.6057, "date": "2026-06-21", "time": "12:00", "step": 10}
    ).json()
    assert data["sun"]["timezone"] == "Asia/Yekaterinburg"
    assert data["sun"]["sunrise"].startswith("04:0")
    assert len(data["timeline"]) == 144
    assert data["state"] in {"sun", "shade"}
    assert 0 < data["sun_minutes"] < 18 * 60
    states = {t["state"] for t in data["timeline"]}
    assert "night" in states and "sun" in states


def test_point_year(client):
    data = client.get("/api/point/year", params={"lat": 56.8385, "lon": 60.6057, "year": 2026}).json()
    months = data["months"]
    assert len(months) == 12
    assert months[5]["daylight_minutes"] > months[11]["daylight_minutes"]
    assert all(m["sun_minutes"] <= m["daylight_minutes"] for m in months)


def test_sun_endpoint_and_bad_time(client):
    ok = client.get("/api/sun", params={"lat": 56.8385, "lon": 60.6057, "date": "2026-06-21", "time": "13:00"})
    assert ok.status_code == 200 and ok.json()["altitude"] > 50
    bad = client.get("/api/sun", params={"lat": 56.8385, "lon": 60.6057, "date": "2026-06-21", "time": "25:99"})
    assert bad.status_code == 400


def test_sun_day_matches_sun_endpoint(client):
    day = client.get("/api/sun/day", params={"lat": 56.84, "lon": 60.61, "date": "2026-06-21", "step": 5}).json()
    assert day["timezone"] == "Asia/Yekaterinburg" and len(day["samples"]) == 288
    noon = day["samples"][13 * 12]
    assert noon["minute"] == 13 * 60
    one = client.get("/api/sun", params={"lat": 56.84, "lon": 60.61, "date": "2026-06-21", "time": "13:00"}).json()
    assert abs(noon["azimuth"] - one["azimuth"]) < 0.01 and abs(noon["altitude"] - one["altitude"]) < 0.01
