"""코스를 DB에 등재할 때 풍경 태그가 느슨한 규칙으로 덮어써지지 않아야 한다."""
from src.data_collection import enrich, scenery

OSM = {"green_areas": [], "coastline": [], "traffic_signals": []}
PATH = [[34.0, 127.0], [34.01, 127.0]]


def test_measured_tags_survive_registration():
    """생성 시점에 측정으로 붙인 태그를, 등재하면서 '녹지가 근처에 있으면 숲길' 식으로 바꾸면 안 된다."""
    course = {"id": "c", "path": PATH, "tags": ["강변"], "scenery": {"river": 0.8}}
    assert enrich.enrich_course(course, OSM)["tags"] == ["강변"]


def test_new_course_is_tagged_by_measurement(monkeypatch):
    monkeypatch.setattr(scenery, "tags_for_path", lambda path, course=None: ({"coast": 0.9}, ["바다뷰"]))
    enriched = enrich.enrich_course({"id": "c", "path": PATH}, OSM)
    assert enriched["tags"] == ["바다뷰"]
    assert enriched["scenery"] == {"coast": 0.9}


def test_new_course_outside_known_regions_is_left_untagged(monkeypatch):
    """지형 데이터가 없으면 추측으로 태그를 만들지 않는다."""
    monkeypatch.setattr(scenery, "tags_for_path", lambda path, course=None: None)
    enriched = enrich.enrich_course({"id": "c", "path": PATH}, OSM)
    assert enriched["tags"] == []
    assert enriched["scenery_pending"] is True
