import json

import requests

from src.data_collection import add_course, osm_overpass


class GatewayTimeoutResponse:
    status_code = 504

    def raise_for_status(self):
        raise requests.HTTPError("504 Gateway Timeout", response=self)


def test_overpass_failure_enters_shared_cooldown(monkeypatch):
    calls = []

    def fail_request(*args, **kwargs):
        calls.append(args)
        return GatewayTimeoutResponse()

    monkeypatch.setattr(osm_overpass, "_retry_after", 0.0)
    monkeypatch.setattr(osm_overpass.requests, "post", fail_request)

    first = osm_overpass.fetch_osm_features("34,127,34.01,127.01")
    second = osm_overpass.fetch_osm_features("35,129,35.01,129.01")

    assert first["osm_available"] is False
    assert second["osm_available"] is False
    assert len(calls) == 1


def test_course_is_saved_when_osm_enrichment_is_unavailable(monkeypatch, tmp_path, capsys):
    def fail_request(*args, **kwargs):
        return GatewayTimeoutResponse()

    monkeypatch.setattr(osm_overpass, "_retry_after", 0.0)
    monkeypatch.setattr(osm_overpass.requests, "post", fail_request)
    db_path = tmp_path / "courses.json"
    raw_course = {
        "id": "generated-loop",
        "name": "실시간 순환 코스",
        "distance_km": 5.0,
        "path": [[34.0, 127.0], [34.01, 127.01], [34.0, 127.0]],
        "tags": [],
    }

    saved = add_course.add_course(raw_course, str(db_path))
    stored = json.loads(db_path.read_text(encoding="utf-8"))

    assert saved["osm_enrichment_pending"] is True
    assert stored[0]["id"] == "generated-loop"
    assert "green_ratio" not in stored[0]
    assert capsys.readouterr().out == ""