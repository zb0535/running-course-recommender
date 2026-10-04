"""별점 하나로는 무엇이 좋았는지 알 수 없다. 항목별 답으로 취향을 직접 배운다."""
import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.recommend import personalize as ps
from src.recommend.profile import elevation_target_m, resolve_target_distance_km

client = TestClient(main.app)

FLAT = {"id": "flat", "name": "평탄한 길", "region": "여수", "path": [[34.0, 127.0], [34.03, 127.0]],
        "distance_km": 5.0, "elevation_gain_m": 20, "tags": [], "traffic_signal_count": 6, "safety_score": 0.8}
HILLY = {**FLAT, "id": "hilly", "name": "언덕길", "elevation_gain_m": 110, "traffic_signal_count": 0}


def _recommend(user_id, **extra):
    body = {"user_id": user_id, "preferred_distance_km": 5, "route_type": "oneway", "elevation_preference": "medium",
            "top_n": 2, "use_live_environment": False, "time_of_day": "morning", **extra}
    return client.post("/recommend", json=body).json()["results"]


@pytest.fixture
def courses(monkeypatch):
    monkeypatch.setattr(main, "load_courses", lambda: [FLAT, HILLY])


class TestLearning:
    def test_complaint_raises_the_weight_of_that_factor_only(self):
        profile = ps.new_profile("u")
        before = dict(profile["weights"])
        ps.learn_aspects(profile, {"stops": "many"})
        after = profile["weights"]
        assert after["signal_free"] > before["signal_free"] * 1.2
        # 나머지는 합을 1로 맞추느라 같은 비율로 조금 줄 뿐, 서로의 순서는 그대로다
        others = [k for k in before if k != "signal_free"]
        ratios = {round(after[k] / before[k], 6) for k in others}
        assert len(ratios) == 1 and ratios.pop() < 1

    def test_complaint_counts_more_than_praise_and_ok_counts_against(self):
        gain = {}
        for answer in ("many", "few", "ok"):
            profile = ps.new_profile("u")
            base = profile["weights"]["signal_free"]
            ps.learn_aspects(profile, {"stops": answer})
            gain[answer] = profile["weights"]["signal_free"] / base
        assert gain["many"] > gain["few"] > 1 > gain["ok"]

    def test_too_hard_lowers_the_climb_this_person_is_given(self):
        profile = ps.new_profile("u")
        ps.learn_aspects(profile, {"elevation": "hard"})
        ps.learn_aspects(profile, {"elevation": "hard"})
        assert profile["elevation_scale"] == pytest.approx(1 / 1.15 ** 2, abs=0.002)
        user = {"elevation_preference": "medium"}
        assert elevation_target_m({**user, "elevation_scale": profile["elevation_scale"]}) < elevation_target_m(user)

    def test_adjustment_is_bounded(self):
        profile = ps.new_profile("u")
        for _ in range(30):
            ps.learn_aspects(profile, {"elevation": "hard", "distance": "long"})
        assert profile["elevation_scale"] == ps.ELEVATION_SCALE_RANGE[0]
        assert profile["distance_scale"] == ps.DISTANCE_SCALE_RANGE[0]

    def test_distance_answer_only_changes_distances_picked_by_time(self):
        """거리를 직접 고른 건 그대로 따른다. 시간으로 고른 경우에만 그 사람 기준으로 환산한다."""
        assert resolve_target_distance_km({"preferred_distance_km": 5, "distance_scale": 0.8}) == 5
        by_time = {"preferred_time_min": 30, "pace_min_per_km": 6}
        assert resolve_target_distance_km({**by_time, "distance_scale": 0.8}) == pytest.approx(4.0)
        assert resolve_target_distance_km(by_time) == pytest.approx(5.0)

    def test_just_right_changes_nothing(self):
        profile = ps.new_profile("u")
        before = dict(profile["weights"])
        ps.learn_aspects(profile, {"distance": "good", "elevation": "good"})
        assert profile["weights"] == pytest.approx(before)
        assert "elevation_scale" not in profile


class TestSummary:
    def test_summary_only_says_what_the_person_actually_told_us(self):
        profile = ps.new_profile("u")
        assert ps.taste_summary(profile) == []
        ps.learn_aspects(profile, {"stops": "many", "elevation": "hard"})
        lines = ps.taste_summary(profile)
        assert any("신호등" in line and "1번" in line for line in lines)
        assert any("완만한" in line for line in lines)
        assert not any("풍경" in line or "불안" in line for line in lines)


class TestApi:
    def test_questions_are_served_so_the_app_does_not_hardcode_them(self):
        questions = client.get("/onboarding/feedback").json()["questions"]
        assert [q["key"] for q in questions] == ["distance", "elevation", "stops", "scenery", "safety"]
        assert all(len(q["options"]) == 3 and q["question"].endswith("?") for q in questions)

    def test_hard_climb_feedback_changes_the_next_recommendation(self, courses):
        """'보통' 오르막을 고른 사람이 언덕길을 힘들었다고 하면, 다음엔 평탄한 길이 먼저 나온다."""
        assert _recommend("climber")[0]["course"]["id"] == "hilly"
        for _ in range(3):
            _recommend("climber")
            resp = client.post("/feedback", json={"user_id": "climber", "course_id": "hilly", "rating": 2,
                                                  "aspects": {"elevation": "hard"}})
            assert resp.status_code == 200
        body = resp.json()
        assert body["tuning"]["elevation_scale"] < 0.7
        assert any("완만한" in line for line in body["summary"])
        assert _recommend("climber")[0]["course"]["id"] == "flat"

    def test_answers_are_stored_with_the_review(self, courses):
        _recommend("a")
        client.post("/feedback", json={"user_id": "a", "course_id": "flat", "rating": 4,
                                       "aspects": {"stops": "many", "scenery": "good"}, "comment": "신호가 많네요"})
        review = client.get("/courses/flat/reviews").json()["reviews"][0]
        assert review["aspects"] == {"stops": "many", "scenery": "good"}

    def test_unknown_answer_is_rejected(self, courses):
        _recommend("a")
        resp = client.post("/feedback", json={"user_id": "a", "course_id": "flat", "rating": 4,
                                              "aspects": {"stops": "sometimes"}})
        assert resp.status_code == 422

    def test_star_only_feedback_still_works(self, courses):
        _recommend("a")
        resp = client.post("/feedback", json={"user_id": "a", "course_id": "flat", "rating": 5})
        assert resp.status_code == 200 and resp.json()["summary"] == []
