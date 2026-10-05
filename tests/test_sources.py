import json

from shadows.buildings import GeoJSONSource, OverpassSource


def _way(id_, lon, lat, tags):
    d = 0.0002
    pts = [(lon, lat), (lon + d, lat), (lon + d, lat + d), (lon, lat + d), (lon, lat)]
    return {"type": "way", "id": id_, "tags": tags, "geometry": [{"lon": x, "lat": y} for x, y in pts]}


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_overpass_tiles_cached_and_deduplicated(tmp_path, monkeypatch):
    src = OverpassSource(tmp_path)
    calls = []
    # Здание на границе тайлов попадает в ответы обоих тайлов.
    shared = _way(1, 60.6099, 56.835, {"building": "yes", "height": "20"})

    def fake_post(url, data, timeout):
        calls.append(data["data"])
        return FakeResponse({"elements": [shared, _way(len(calls) + 10, 60.605, 56.835, {"building": "house"})]})

    monkeypatch.setattr(src._session, "post", fake_post)
    found = src.query((60.605, 56.834, 60.612, 56.836))
    assert len(calls) == 2  # два тайла по долготе
    assert sorted(b.id for b in found).count("osm/way/1") == 1

    # Повторный запрос и новый экземпляр берут данные из кэша на диске.
    src.query((60.605, 56.834, 60.612, 56.836))
    src2 = OverpassSource(tmp_path)
    monkeypatch.setattr(src2._session, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    assert src2.query((60.605, 56.834, 60.612, 56.836))
    assert len(calls) == 2


def test_geojson_overture_properties(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "ov-1",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[60.6, 56.83], [60.601, 56.83], [60.601, 56.831], [60.6, 56.831], [60.6, 56.83]]],
                },
                "properties": {
                    "num_floors": 9,
                    "names": None,
                    "sources": [{"dataset": "OpenStreetMap"}, {"dataset": "Microsoft ML Buildings"}],
                },
            }
        ],
    }
    path = tmp_path / "b.geojson"
    path.write_text(json.dumps(fc))
    src = GeoJSONSource(path)
    (b,) = src.query((60.59, 56.82, 60.61, 56.84))
    assert b.id == "ov-1" and b.height == 27 and b.height_source == "levels"
    assert "OpenStreetMap" in src.attribution and "Microsoft" in src.attribution
