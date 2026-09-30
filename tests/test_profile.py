"""러너 프로필(페이스·숙련도·목적)이 실제로 추천을 바꾸는지 검증.

온보딩에서 물어본 값이 점수에 반영되지 않으면 물어본 의미가 없다.
"""
import pytest

from src.recommend.profile import (
    PURPOSES_BY_EXPERIENCE,
    elevation_target_m,
    purposes_for,
    resolve_pace,
    resolve_target_distance_km,
    tune_weights,
)
from src.recommend.score import DEFAULT_WEIGHTS


class TestPace:
    def test_direct_pace_wins(self):
        assert resolve_pace({"pace_min_per_km": 5.5, "experience_level": "beginner"}) == 5.5

    def test_falls_back_to_experience_when_pace_unknown(self):
        beginner = resolve_pace({"experience_level": "beginner"})
        advanced = resolve_pace({"experience_level": "advanced"})
        assert beginner > advanced  # 입문자가 더 느림

    def test_unknown_everything_still_returns_usable_pace(self):
        assert 4 < resolve_pace({}) < 12


class TestTimeToDistance:
    def test_time_converts_to_distance_using_pace(self):
        # 30분 / 6분per km = 5km
        km = resolve_target_distance_km({"preferred_time_min": 30, "pace_min_per_km": 6})
        assert km == pytest.approx(5.0)

    def test_explicit_distance_takes_priority_over_time(self):
        km = resolve_target_distance_km({"preferred_distance_km": 3, "preferred_time_min": 60, "pace_min_per_km": 6})
        assert km == 3

    def test_slower_runner_gets_shorter_course_for_same_time(self):
        slow = resolve_target_distance_km({"preferred_time_min": 30, "experience_level": "beginner"})
        fast = resolve_target_distance_km({"preferred_time_min": 30, "experience_level": "advanced"})
        assert slow < fast

    def test_no_time_and_no_distance_returns_none(self):
        assert resolve_target_distance_km({}) is None


class TestPurposeVocabulary:
    def test_purpose_options_differ_by_experience(self):
        assert purposes_for("beginner") != purposes_for("advanced")

    def test_every_level_offers_options(self):
        for level in PURPOSES_BY_EXPERIENCE:
            assert len(purposes_for(level)) >= 2

    def test_unknown_level_falls_back_to_beginner(self):
        assert purposes_for("nonsense") == purposes_for("beginner")


class TestPurposeChangesScoring:
    def test_weights_still_sum_to_one(self):
        tuned = tune_weights(DEFAULT_WEIGHTS, "기록 단축")
        assert sum(tuned.values()) == pytest.approx(1.0)

    def test_record_chasing_cares_more_about_signal_free(self):
        tuned = tune_weights(DEFAULT_WEIGHTS, "기록 단축")
        assert tuned["signal_free"] > DEFAULT_WEIGHTS["signal_free"]

    def test_unknown_purpose_leaves_weights_untouched(self):
        assert tune_weights(DEFAULT_WEIGHTS, "듣도보도못한목적") == DEFAULT_WEIGHTS

    def test_weight_loss_prefers_flatter_course_than_race_training(self):
        assert elevation_target_m({"purpose": "체중 감량"}) < elevation_target_m({"purpose": "대회 준비"})

    def test_elevation_preference_used_when_no_purpose(self):
        assert elevation_target_m({"elevation_preference": "high"}) > elevation_target_m({"elevation_preference": "low"})
