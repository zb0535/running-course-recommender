"""풍경 선택지는 일반 사용자가 바로 이해할 수 있어야 한다."""
from fastapi.testclient import TestClient

from src.api import main
from src.data_collection.scenery import THRESHOLDS, tag_descriptions
from src.recommend import scenery_choices as sc
from src.recommend.score import tag_match

client = TestClient(main.app)


def test_every_measured_tag_belongs_to_exactly_one_choice():
    """측정 태그가 새로 생겼는데 선택지에 안 넣으면, 그 풍경은 아무도 고를 수 없다."""
    mapped = [tag for _, _, _, tags in sc.CHOICES for tag in tags]
    assert sorted(mapped) == sorted(tag_descriptions())
    assert len(mapped) == len(set(mapped))


def test_choices_are_few_and_plainly_worded():
    assert len(sc.CHOICES) <= 10
    for name, group, hint, _ in sc.CHOICES:
        assert group in sc.GROUPS
        assert hint.endswith("요"), f"{name}: 설명은 말하듯 쓴다"
        assert not any(ch.isdigit() for ch in hint) and "%" not in hint, f"{name}: 설명에 측정 기준을 넣지 않는다"


def test_choice_expands_to_its_measured_tags():
    tags, groups = sc.resolve(["바다", "공원"])
    assert tags == {"바다뷰", "해변", "공원"}
    assert groups == [["바다뷰", "해변"], ["공원"]]


def test_measured_tag_from_an_older_app_is_still_accepted():
    tags, groups = sc.resolve(["강변"])
    assert tags == {"강변"} and groups == [["강변"]]


def test_one_matching_tag_satisfies_the_whole_choice():
    """'바다'를 골랐을 때 바다뷰만 있는 코스가 절반만 맞는 것으로 계산되면 안 된다."""
    tags, groups = sc.resolve(["바다"])
    course = {"tags": ["바다뷰"], "scenery": {"coast": THRESHOLDS["바다뷰"][1] * 2}}
    assert tag_match(course, {"environment_tags": tags, "scenery_groups": groups}) == 1.0


def test_course_tags_are_shown_with_the_same_plain_names():
    assert sc.labels_for(["바다뷰", "해변", "보행자길", "전망"]) == ["바다", "차 없는 길", "명소·전망"]


def test_distance_is_said_in_words():
    assert sc.near_text(0.05) == "바로 근처"
    assert sc.near_text(0.4) == "걸어서 5분"
    assert sc.near_text(3.04) == "3.0km"
    assert sc.near_text(None) == ""


def test_recommend_accepts_a_choice_and_labels_the_results():
    resp = client.post("/recommend", json={"environment_tags": ["바다"], "route_type": "oneway",
                                           "preferred_distance_km": 4, "use_live_environment": False})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results
    for result in results:
        assert "바다" in result["course"]["scenery_labels"]


def test_unknown_choice_is_rejected():
    resp = client.post("/recommend", json={"environment_tags": ["우주"], "use_live_environment": False})
    assert resp.status_code == 422
