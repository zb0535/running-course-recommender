"""지금 있는 곳 주변에 실제로 있는 풍경만 선택지로 준다."""
import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.data_collection import local_osm
from src.recommend import nearby_scenery

client = TestClient(main.app)
LAT, LNG = 36.0, 127.5   # 저장된 코스가 없는 내륙


@pytest.fixture
def terrain(tmp_path, monkeypatch):
    path = str(tmp_path / "local.sqlite")
    writer = local_osm.Writer(path)
    writer.add_way([(LAT + 0.01, LNG - 0.02), (LAT + 0.01, LNG + 0.02)], {"waterway": "river"})          # 약 1.1km
    writer.add_rings([[(LAT + 0.04, LNG), (LAT + 0.04, LNG + 0.01), (LAT + 0.05, LNG + 0.01),
                       (LAT + 0.05, LNG), (LAT + 0.04, LNG)]], {"leisure": "park"})                       # 약 4.5km
    writer.add_way([(LAT + 0.2, LNG), (LAT + 0.2, LNG + 0.1)], {"natural": "coastline"})                  # 약 22km
    writer.add_way([(LAT, LNG + 0.001), (LAT + 0.002, LNG + 0.001)], {"highway": "cycleway"}, 1, 2)
    writer.add_way([(LAT + 0.05, LNG), (LAT + 0.06, LNG)], {"highway": "footway"}, 3, 4)                  # 5.5km — 길은 멀면 뺀다
    writer.finish({})
    monkeypatch.setattr(local_osm, "DB_PATH", path)
    monkeypatch.setattr(nearby_scenery, "_cache", {})


def test_only_scenery_within_the_radius_is_offered(terrain):
    found = nearby_scenery.nearby(LAT, LNG, 10)
    assert found["강변"] == pytest.approx(1.11, abs=0.05)
    assert found["공원"] == pytest.approx(4.45, abs=0.1)
    assert "바다뷰" not in found, "22km 떨어진 바다를 선택지로 줬다"


def test_road_types_count_only_when_close(terrain):
    found = nearby_scenery.nearby(LAT, LNG, 10)
    assert found["자전거길"] < 0.2
    assert "보행자길" not in found


def test_smaller_radius_offers_less(terrain):
    assert "공원" not in nearby_scenery.nearby(LAT, LNG, 3)


def test_api_lists_nearby_scenery_closest_first(terrain):
    body = client.get("/onboarding/scenery", params={"lat": LAT, "lng": LNG}).json()
    assert body["basis"] == "location" and body["radius_km"] == 10
    values = [o["value"] for o in body["scenery"]]
    assert values == ["차 없는 길", "강·하천", "공원"]
    assert "바다" not in values
    near = {o["value"]: o["near"] for o in body["scenery"]}
    assert near["차 없는 길"] == "바로 근처" and near["공원"] == "4.5km"


def test_without_local_terrain_falls_back_to_nearby_courses():
    """로컬 지형 DB가 없는 PC에서는 근처 저장 코스가 가진 풍경만 준다."""
    body = client.get("/onboarding/scenery", params={"lat": 34.7393, "lng": 127.7359}).json()
    assert body["basis"] == "nearby_courses"
    assert "바다" in [o["value"] for o in body["scenery"]]
    inland = client.get("/onboarding/scenery", params={"lat": LAT, "lng": LNG}).json()
    assert inland["scenery"] == []
