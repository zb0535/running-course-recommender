"""별점·후기·즐겨찾기를 저장하고 돌려준다."""
from fastapi.testclient import TestClient

from src.api import main

client = TestClient(main.app)

COURSE = {"id": "quiet", "name": "한적한 길", "region": "여수", "path": [[34.0, 127.0], [34.03, 127.0]],
          "distance_km": 5.0, "elevation_gain_m": 60, "tags": [], "traffic_signal_count": 0, "safety_score": 0.8}
OTHER = {**COURSE, "id": "busy", "name": "번화한 길", "traffic_signal_count": 8}


def _stub(monkeypatch):
    monkeypatch.setattr(main, "load_courses", lambda: [COURSE, OTHER])


def _recommend(user_id):
    return client.post("/recommend", json={"user_id": user_id, "preferred_distance_km": 5, "route_type": "oneway",
                                           "top_n": 2, "use_live_environment": False, "time_of_day": "morning"})


def test_feedback_is_kept_as_a_review(monkeypatch):
    _stub(monkeypatch)
    _recommend("a")
    client.post("/feedback", json={"user_id": "a", "course_id": "quiet", "rating": 5, "comment": "조용해서 좋아요"})
    body = client.get("/courses/quiet/reviews").json()
    assert body["average"] == 5 and body["count"] == 1
    assert body["reviews"][0]["comment"] == "조용해서 좋아요"


def test_average_covers_everyone_and_one_person_counts_once(monkeypatch):
    _stub(monkeypatch)
    for user, rating in (("a", 5), ("b", 2), ("a", 4)):   # a가 다시 평가하면 고쳐 쓴다
        client.post("/reviews", json={"user_id": user, "course_id": "quiet", "rating": rating})
    body = client.get("/courses/quiet/reviews").json()
    assert body["count"] == 2 and body["average"] == 3.0


def test_review_for_a_course_that_does_not_exist_is_rejected(monkeypatch):
    _stub(monkeypatch)
    assert client.post("/reviews", json={"user_id": "a", "course_id": "nope", "rating": 3}).status_code == 404


def test_recommendation_shows_other_peoples_rating(monkeypatch):
    _stub(monkeypatch)
    client.post("/reviews", json={"user_id": "b", "course_id": "quiet", "rating": 4})
    results = {r["course"]["id"]: r for r in _recommend("a").json()["results"]}
    assert results["quiet"]["rating"] == {"average": 4.0, "count": 1}
    assert results["busy"]["rating"] is None


def test_favorites_round_trip(monkeypatch):
    _stub(monkeypatch)
    assert client.post("/favorites", json={"user_id": "a", "course_id": "quiet"}).json()["count"] == 1
    saved = client.get("/favorites/a").json()["favorites"]
    assert [item["course"]["id"] for item in saved] == ["quiet"]
    assert saved[0]["course"]["path"] == COURSE["path"]
    assert {r["course"]["id"]: r["favorite"] for r in _recommend("a").json()["results"]} == {"quiet": True, "busy": False}
    assert client.delete("/favorites/a/quiet").status_code == 200
    assert client.get("/favorites/a").json()["favorites"] == []
    assert client.delete("/favorites/a/quiet").status_code == 404


def test_generated_course_can_be_saved_with_its_content(monkeypatch):
    """실시간 생성 코스는 아직 DB에 없을 수 있다. 받은 코스를 그대로 보내면 저장된다."""
    _stub(monkeypatch)
    made = {**COURSE, "id": "generated-x"}
    assert client.post("/favorites", json={"user_id": "a", "course_id": "generated-x"}).status_code == 404
    assert client.post("/favorites", json={"user_id": "a", "course_id": "generated-x", "course": made}).status_code == 200
    # 저장해 둔 코스에는 DB에 없어도 후기를 남길 수 있다
    assert client.post("/reviews", json={"user_id": "a", "course_id": "generated-x", "rating": 5}).status_code == 200


def test_favorite_with_mismatched_content_is_rejected(monkeypatch):
    _stub(monkeypatch)
    resp = client.post("/favorites", json={"user_id": "a", "course_id": "quiet", "course": {"id": "other"}})
    assert resp.status_code == 422
