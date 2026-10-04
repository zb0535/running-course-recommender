"""내려받은 OSM 파일로 만든 로컬 지형 DB — 실행 중에 Overpass를 부르지 않는다."""
import pytest

from src.data_collection import area_cache, local_osm

LAT, LNG = 35.0, 127.0


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "local.sqlite")
    writer = local_osm.Writer(path)
    # 삼각형으로 이어진 길 셋 + 거기서 뻗은 막다른 길 하나
    writer.add_way([(LAT, LNG), (LAT, LNG + 0.002)], {"highway": "residential"}, 1, 2)
    writer.add_way([(LAT, LNG + 0.002), (LAT + 0.002, LNG + 0.001)], {"highway": "residential"}, 2, 3)
    writer.add_way([(LAT + 0.002, LNG + 0.001), (LAT, LNG)], {"highway": "footway"}, 3, 1)
    writer.add_way([(LAT, LNG + 0.001), (LAT - 0.001, LNG + 0.001)], {"highway": "residential"}, 7, 9)
    writer.add_way([(LAT - 0.0005, LNG - 0.01), (LAT - 0.0005, LNG + 0.01)], {"waterway": "river"})
    writer.add_node(LAT + 0.001, LNG + 0.001, {"shop": "bakery"})
    writer.add_rings([[(LAT + 0.003, LNG), (LAT + 0.003, LNG + 0.002), (LAT + 0.005, LNG + 0.002),
                       (LAT + 0.005, LNG), (LAT + 0.003, LNG)]], {"leisure": "park"})
    writer.add_way([(LAT + 1, LNG + 1), (LAT + 1, LNG + 1.001)], {"highway": "residential"}, 50, 51)  # 먼 동네
    writer.finish({1: 2, 2: 2, 3: 2, 7: 2, 9: 1, 50: 1, 51: 1})
    monkeypatch.setattr(local_osm, "DB_PATH", path)
    return path


def test_only_what_scenery_and_routing_use_is_kept():
    assert local_osm.keep_tags({"building": "yes"}) is None
    assert local_osm.keep_tags({"highway": "motorway"}) is None, "자동차 전용도로는 달릴 수 없다"
    assert local_osm.keep_tags({"highway": "motorway", "bridge": "yes"}) == {"highway": "motorway", "bridge": "yes"}
    assert local_osm.keep_tags({"highway": "primary", "lit": "yes", "maxspeed": "50"}) == {"highway": "primary", "lit": "yes"}
    assert local_osm.keep_tags({"highway": "footway", "name": "길", "surface": "x"}) == {"highway": "footway", "name": "길"}
    assert local_osm.keep_tags({"shop": "bakery"}, is_node=True) == {"shop": "bakery"}
    assert local_osm.keep_tags({"man_made": "surveillance"}, is_node=True) == {"man_made": "surveillance"}
    assert local_osm.keep_tags({"amenity": "police", "name": "파출소"}) == {"amenity": "police"}
    assert local_osm.keep_tags({"highway": "traffic_signals"}, is_node=True) == {"highway": "traffic_signals"}
    assert local_osm.keep_tags({"highway": "traffic_signals"}) is None, "신호등은 점이다"


def test_scenery_can_be_read_without_the_roads(db):
    box = (LAT - 0.02, LNG - 0.02, LAT + 0.02, LNG + 0.02)
    assert sorted(e["type"] for e in local_osm.load_bbox(box, kinds="s")) == ["relation", "way"]
    assert len(local_osm.load_bbox(box, kinds="sp")) == 3


def test_reads_only_the_requested_neighbourhood(db):
    elements = local_osm.load_bbox((LAT - 0.02, LNG - 0.02, LAT + 0.02, LNG + 0.02))
    assert len(elements) == 7
    kinds = sorted(e["type"] for e in elements)
    assert kinds == ["node", "relation", "way", "way", "way", "way", "way"]


def test_area_comes_from_the_local_db_without_overpass(db, monkeypatch):
    monkeypatch.setattr(area_cache, "_fetch", lambda bbox: pytest.fail("Overpass를 불렀다"))
    area = area_cache.load_area(LAT, LNG, radius_m=900)
    assert area.covers(LAT, LNG, 900)
    assert area.indexes["river"].near(LAT, LNG, 80)
    assert area.indexes["park"].near(LAT + 0.004, LNG + 0.001, 20)   # 면 안쪽
    assert area.indexes["shops"].size == 1


def test_dead_ends_marked_at_build_time_are_not_snap_targets(db):
    area = area_cache.load_area(LAT, LNG, radius_m=900)
    assert area.roads.size == 3
    # 막다른 길 끝(남쪽 110m) 근처에서 붙이면 막다른 길이 아니라 이어진 길로 간다
    snapped = area.roads.nearest_point(LAT - 0.001, LNG + 0.001, 200)
    assert snapped[0] == pytest.approx(LAT, abs=1e-6)


def test_stored_path_is_measured_from_the_local_db(db, monkeypatch):
    monkeypatch.setattr(area_cache, "_fetch", lambda bbox: pytest.fail("Overpass를 불렀다"))
    path = [[LAT, LNG], [LAT, LNG + 0.002]]
    assert area_cache.cached_area_for_path(path) is not None


def test_hole_in_an_area_is_not_part_of_it(tmp_path, monkeypatch):
    """섬을 두르는 강변 공원은 바깥 고리가 섬 전체를 감싼다. 구멍(섬 안 시가지)은 공원이 아니다."""
    path = str(tmp_path / "holes.sqlite")
    writer = local_osm.Writer(path)
    square = lambda half: [(LAT - half, LNG - half), (LAT - half, LNG + half), (LAT + half, LNG + half),
                           (LAT + half, LNG - half), (LAT - half, LNG - half)]
    writer.add_rings([square(0.01)], {"leisure": "park"}, holes=[square(0.006)])
    writer.finish({})
    monkeypatch.setattr(local_osm, "DB_PATH", path)
    park = area_cache.load_area(LAT, LNG, radius_m=900).indexes["park"]
    assert not park.near(LAT, LNG, 20), "구멍 안을 공원으로 잡았다"
    assert park.near(LAT + 0.008, LNG, 20), "구멍 밖 공원 안쪽"


def test_safety_facilities_are_read_separately_from_roads(tmp_path):
    path = str(tmp_path / "f.sqlite")
    writer = local_osm.Writer(path)
    writer.add_node(LAT, LNG, {"man_made": "surveillance"})
    writer.add_node(LAT, LNG + 0.001, {"highway": "traffic_signals"})
    writer.add_node(LAT, LNG + 0.002, {"amenity": "police"})
    writer.add_way([(LAT, LNG), (LAT, LNG + 0.002)], {"highway": "residential"}, 1, 2)
    writer.finish({1: 1, 2: 1})
    box = (LAT - 0.01, LNG - 0.01, LAT + 0.01, LNG + 0.01)
    assert len(local_osm.load_bbox(box, path, kinds="f")) == 3
    assert [e["type"] for e in local_osm.load_bbox(box, path, kinds="r")] == ["way"]


class TestSameAsTheTeam:
    def test_build_records_where_the_db_came_from(self, db):
        info = local_osm.info()
        assert info["elements"] == 8 and info["build_version"] == local_osm.BUILD_VERSION

    def test_db_from_a_different_map_file_does_not_match(self, db):
        """다른 날짜의 지도로 만든 DB는 같은 요청에도 다른 코스를 낸다. 기준과 다르다고 알려준다."""
        assert local_osm.info()["matches_team"] is False   # 테스트 DB는 기준 파일로 만들지 않았다

    def test_no_db_means_no_info(self, tmp_path):
        assert local_osm.info(str(tmp_path / "none.sqlite")) is None


def test_sidewalk_knowledge_starts_from_the_shared_seed(tmp_path, monkeypatch):
    """팀원 모두 같은 인도 확인 기록에서 시작한다."""
    import json

    from src.recommend import sidewalk

    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"35.0,127.0,35.0,127.001": True, "35.1,127.0,35.1,127.001": False}), encoding="utf-8")
    monkeypatch.setattr(sidewalk, "SEED_PATH", str(seed))
    monkeypatch.setattr(sidewalk, "FACTS_PATH", str(tmp_path / "facts.sqlite"))
    assert sidewalk.load_facts() == {"35.0,127.0,35.0,127.001": True, "35.1,127.0,35.1,127.001": False}
    sidewalk.save_facts({"35.2,127.0,35.2,127.001": True})
    assert sidewalk.export_seed() == 3
