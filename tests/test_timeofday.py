"""달리는 시간대에 따라 좋은 코스가 달라진다. 사용자에게 묻지 않고 지금 시각으로 판단한다.

밤에 인적 드문 숲길을 1순위로 주면 안 되고, 한여름 한낮에 그늘 없는 길을 주면 안 된다.
"""
from datetime import datetime

from src.recommend.timeofday import current_period, time_fit_match

FOREST = {"green_ratio": 0.9, "traffic_signal_count": 0, "distance_km": 5}
CITY = {"green_ratio": 0.35, "traffic_signal_count": 15, "distance_km": 5}


def _at(hour):
    return datetime(2026, 9, 16, hour, 0)


class TestPeriod:
    def test_hours_map_to_periods(self):
        assert current_period(_at(7)) == "morning"
        assert current_period(_at(14)) == "afternoon"
        assert current_period(_at(19)) == "evening"
        assert current_period(_at(23)) == "night"
        assert current_period(_at(3)) == "night"


class TestNight:
    def test_night_avoids_isolated_green_courses(self):
        """밤에는 인적 드문 숲길보다 사람이 다니는 길이 낫다."""
        user = {"time_of_day": "night"}
        assert time_fit_match(CITY, user) > time_fit_match(FOREST, user)


class TestDaytime:
    def test_hot_afternoon_prefers_shade(self):
        user = {"time_of_day": "afternoon"}
        assert time_fit_match(FOREST, user) > time_fit_match(CITY, user)

    def test_morning_does_not_strongly_prefer_either(self):
        user = {"time_of_day": "morning"}
        assert abs(time_fit_match(FOREST, user) - time_fit_match(CITY, user)) < 0.2


class TestDefaults:
    def test_period_is_detected_when_user_did_not_say(self):
        """사용자가 고르지 않아도 지금 시각으로 판단한다 — 물어볼 필요가 없는 정보다."""
        assert time_fit_match(FOREST, {}) is not None

    def test_unknown_period_is_neutral(self):
        assert time_fit_match(FOREST, {"time_of_day": "언젠가"}) == 0.5
