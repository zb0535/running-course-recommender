import json

from src.map_view.graph_geojson import parse_bbox, public_graph


def test_public_graph_filters_bbox_and_crossings(tmp_path):
    graph = {
        "type": "FeatureCollection",
        "name": "test",
        "features": [
            {"type": "Feature", "properties": {"feature_type": "road_edge"},
             "geometry": {"type": "LineString", "coordinates": [[126.0, 35.0], [126.1, 35.1]]}},
            {"type": "Feature", "properties": {"feature_type": "crossing"},
             "geometry": {"type": "Point", "coordinates": [126.05, 35.05]}},
        ],
    }
    path = tmp_path / "graph.geojson"
    path.write_text(json.dumps(graph), encoding="utf-8")

    result = public_graph(str(path), (126.0, 35.0, 126.06, 35.06), include_crossings=False)
    assert result["name"] == "test"
    assert [f["properties"]["feature_type"] for f in result["features"]] == ["road_edge"]


def test_parse_bbox_rejects_bad_input():
    assert parse_bbox("126,35,127,36") == (126.0, 35.0, 127.0, 36.0)
    for value in ("126,35,126,36", "wrong"):
        try:
            parse_bbox(value)
        except ValueError:
            pass
        else:
            raise AssertionError("bad bbox accepted")
