import json
import os

import pytest

from src.recommend.nl_keywords import extract_tags
from src.recommend.score import distance_match, recommend, score_course, signal_free_match

SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "courses.sample.json")


@pytest.fixture
def courses():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_coastal_lover_prefers_coastal_course(courses):
    user = {
        "fitness_level": 3,
        "purpose": "힐링",
        "preferred_distance_km": 4,
        "elevation_preference": "low",
        "environment_tags": {"바다뷰"},
    }
    top = recommend(courses, user, top_n=1)[0][0]
    assert top["id"] == "yeosu-coastal-01"


def test_required_tag_excludes_courses_without_it(courses):
    """바다뷰를 골랐으면 바다가 없는 내륙 코스는 점수와 무관하게 결과에서 빠져야 한다."""
    user = {"preferred_distance_km": 5, "environment_tags": {"바다뷰"}}
    ranked = recommend(courses, user, top_n=10)
    ids = [c["id"] for c, _ in ranked]
    assert "gwangju-urban-01" not in ids
    assert "gwangju-park-01" not in ids
    assert "yeosu-coastal-01" in ids


def test_signal_free_preference_penalizes_urban_course(courses):
    user = {
        "preferred_distance_km": 5,
        "elevation_preference": "medium",
        "environment_tags": {"차없는길"},
    }
    urban = next(c for c in courses if c["id"] == "gwangju-urban-01")
    park = next(c for c in courses if c["id"] == "gwangju-park-01")
    assert score_course(park, user) > score_course(urban, user)


def test_signal_score_uses_density_not_raw_count():
    """신호등 총 개수로만 보면 긴 코스가 구조적으로 불리해진다. 체감은 km당 밀도다."""
    user = {"preferred_distance_km": 5, "environment_tags": set()}
    short_dense = {"distance_km": 2.0, "traffic_signal_count": 6}   # 3개/km
    long_sparse = {"distance_km": 9.0, "traffic_signal_count": 9}   # 1개/km
    assert signal_free_match(long_sparse, user) > signal_free_match(short_dense, user)


def test_signal_free_course_scores_full_marks():
    user = {"environment_tags": set()}
    assert signal_free_match({"distance_km": 5, "traffic_signal_count": 0}, user) == 1.0


def test_extract_tags_from_natural_language_sentence():
    text = "멈추지 않고 달릴 수 있고 나무가 많거나 바다가 보이는 코스"
    tags = extract_tags(text)
    assert tags == {"차없는길", "숲길", "바다뷰"}


def test_extract_tags_empty_for_no_match():
    assert extract_tags("아무거나 상관없어요") == set()


def test_distance_match_is_perfect_for_longer_courses():
    """중간에 끊고 돌아오면 되니까, 선호 거리보다 길기만 하면(6km든 20km든) 만점이어야 한다."""
    user = {"preferred_distance_km": 5}
    assert distance_match({"distance_km": 5.0}, user) == 1.0
    assert distance_match({"distance_km": 6.0}, user) == 1.0
    assert distance_match({"distance_km": 20.0}, user) == 1.0


def test_distance_match_penalizes_shorter_courses():
    """짧은 코스는 길을 늘릴 수 없으니 부족한 만큼 감점되어야 한다."""
    user = {"preferred_distance_km": 5}
    close = distance_match({"distance_km": 4.5}, user)
    far = distance_match({"distance_km": 1.0}, user)
    assert 0 < far < close < 1.0


def test_loop_distance_score_prefers_target_length():
    user = {"preferred_distance_km": 5.0}
    # 순환은 잘라낼 수 없으니 목표보다 길어도 감점한다
    exact = {"source": "live_generated", "route_type": "loop", "distance_km": 5.0}
    over = {"source": "live_generated", "route_type": "loop", "distance_km": 5.5}

    assert distance_match(exact, user) > distance_match(over, user)


def test_generated_loop_ranking_changes_with_elevation_preference():
    flat = {
        "id": "flat-loop", "source": "live_generated", "route_type": "loop",
        "distance_km": 5.0, "elevation_gain_m": 20, "safety_score": 0.7,
        "traffic_signal_count": 0, "tags": [],
    }
    hilly = {**flat, "id": "hilly-loop", "elevation_gain_m": 150}

    low_top = recommend([flat, hilly], {
        "preferred_distance_km": 5.0, "elevation_preference": "low", "environment_tags": set(),
    }, top_n=1)[0][0]
    high_top = recommend([flat, hilly], {
        "preferred_distance_km": 5.0, "elevation_preference": "high", "environment_tags": set(),
    }, top_n=1)[0][0]

    assert low_top["id"] == "flat-loop"
    assert high_top["id"] == "hilly-loop"
