"""추천 → 러닝 → 평가 → 다음 추천이 달라지는 흐름을 API 끝까지 확인한다."""
import pytest
from fastapi.testclient import TestClient

from src.api import main

client = TestClient(main.app)

# 한적한 길: 신호등이 없는 대신 오르막이 심하고 안전점수가 낮다.
# 번화한 길: 신호등이 좀 있지만 고도가 딱 맞고 안전점수가 높다.
# 기본 가중치로는 번화한 길이 앞선다 — 평가로 이 순위가 뒤집히는지 본다.
QUIET = {"id": "quiet", "name": "한적한 길", "region": "여수", "path": [[34.0, 127.0], [34.03, 127.0]],
         "distance_km": 5.0, "elevation_gain_m": 150, "tags": [], "traffic_signal_count": 0,
         "safety_score": 0.3, "green_ratio": 0.5}
BUSY = {**QUIET, "id": "busy", "name": "번화한 길", "elevation_gain_m": 60,
        "traffic_signal_count": 8, "safety_score": 0.9}


def _recommend(user_id, **extra):
    body = {"user_id": user_id, "preferred_distance_km": 5, "route_type": "oneway",
            "top_n": 2, "use_live_environment": False, "time_of_day": "morning", **extra}
    return client.post("/recommend", json=body)


def _stub_courses(monkeypatch):
    monkeypatch.setattr(main, "load_courses", lambda: [QUIET, BUSY])


def test_recommend_returns_why_each_course_scored(monkeypatch):
    _stub_courses(monkeypatch)
    result = _recommend("runner-a").json()["results"][0]
    assert set(result["score_breakdown"]) >= {"distance", "elevation", "safety", "signal_free"}


def test_feedback_updates_the_profile_and_explains_it(monkeypatch):
    _stub_courses(monkeypatch)
    _recommend("runner-b")
    resp = client.post("/feedback", json={"user_id": "runner-b", "course_id": "quiet", "rating": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_feedback"] == 1
    changes = {row["factor"]: row["change"] for row in body["explanation"]}
    # 만족한 코스가 강했던 항목(신호등)은 오르고, 약했던 항목(고도·안전)은 내려간다
    assert changes["signal_free"] > 0
    assert changes["elevation"] < 0
    assert changes["safety"] < 0


def test_factor_shared_by_all_recommended_courses_is_not_learned(monkeypatch):
    """두 코스 모두 목표 거리와 같았다면, 한적한 길을 좋아한 이유는 거리가 아니다."""
    _stub_courses(monkeypatch)
    _recommend("runner-g")
    before = client.get("/profile/runner-g").json()["weights"]
    after = client.post("/feedback", json={"user_id": "runner-g", "course_id": "quiet", "rating": 5}).json()["weights"]
    # 거리와 풍경은 두 코스가 똑같았다 — 선택에 영향이 없었으니 서로의 비율이 그대로여야 한다.
    # (다른 항목이 줄면 합을 1로 맞추느라 둘 다 같은 비율로 조금 오를 수는 있다)
    assert after["distance"] / after["tag_match"] == pytest.approx(before["distance"] / before["tag_match"], rel=0.02)
    # 실제로 선택을 가른 신호등은 비율 자체가 커져야 한다
    assert after["signal_free"] / after["tag_match"] > before["signal_free"] / before["tag_match"] * 1.1


def test_feedback_changes_the_next_recommendation(monkeypatch):
    """신호등 없는 길을 계속 높게 평가한 사람에게는 안전점수가 높아도 신호 많은 길이 밀려야 한다."""
    _stub_courses(monkeypatch)
    first = [r["course"]["id"] for r in _recommend("runner-c").json()["results"]]
    assert first[0] == "busy"  # 처음엔 안전점수가 높은 쪽이 앞선다

    for _ in range(10):
        _recommend("runner-c")
        client.post("/feedback", json={"user_id": "runner-c", "course_id": "quiet", "rating": 5})
        client.post("/feedback", json={"user_id": "runner-c", "course_id": "busy", "rating": 1})

    after = [r["course"]["id"] for r in _recommend("runner-c").json()["results"]]
    assert after[0] == "quiet"


def test_feedback_for_a_course_never_recommended_is_rejected(monkeypatch):
    _stub_courses(monkeypatch)
    _recommend("runner-d")
    resp = client.post("/feedback", json={"user_id": "runner-d", "course_id": "made-up", "rating": 5})
    assert resp.status_code == 404


def test_feedback_from_unknown_user_is_rejected():
    resp = client.post("/feedback", json={"user_id": "ghost", "course_id": "quiet", "rating": 5})
    assert resp.status_code == 404


def test_rating_out_of_range_is_rejected(monkeypatch):
    _stub_courses(monkeypatch)
    _recommend("runner-e")
    resp = client.post("/feedback", json={"user_id": "runner-e", "course_id": "quiet", "rating": 9})
    assert resp.status_code == 422


def test_profile_shows_learned_preferences(monkeypatch):
    _stub_courses(monkeypatch)
    _recommend("runner-f", experience_level="beginner")
    client.post("/feedback", json={"user_id": "runner-f", "course_id": "quiet", "rating": 5})
    profile = client.get("/profile/runner-f").json()
    assert profile["n_feedback"] == 1
    assert profile["survey"]["experience_level"] == "beginner"
    assert "signal_free" in profile["weights"]


def test_recommending_without_user_id_learns_nothing(monkeypatch):
    _stub_courses(monkeypatch)
    _recommend(None)
    assert client.get("/profile/None").status_code == 404
