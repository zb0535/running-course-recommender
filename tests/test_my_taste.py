"""취향 데이터는 그 사람이 직접 볼 수 있어야 한다: 무엇을 답했고, 그래서 무엇이 바뀌었는지."""
import json

import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.recommend import personalize, user_store

client = TestClient(main.app)

SEA = {"id": "sea", "name": "해안길", "region": "여수", "path": [[34.0, 127.0], [34.03, 127.0]],
       "distance_km": 5.0, "elevation_gain_m": 60, "tags": ["바다뷰", "공원"], "traffic_signal_count": 0,
       "safety_score": 0.8}
CITY = {**SEA, "id": "city", "name": "시내길", "tags": ["도심"], "traffic_signal_count": 8}


@pytest.fixture(autouse=True)
def courses(monkeypatch):
    monkeypatch.setattr(main, "load_courses", lambda: [SEA, CITY])


def _recommend(user_id):
    return client.post("/recommend", json={"user_id": user_id, "preferred_distance_km": 5, "route_type": "oneway",
                                           "top_n": 2, "use_live_environment": False, "time_of_day": "morning"})


def _rate(user_id, course_id, rating, **extra):
    _recommend(user_id)
    return client.post("/feedback", json={"user_id": user_id, "course_id": course_id, "rating": rating, **extra})


def test_nothing_stored_yet_is_a_404():
    assert client.get("/users/nobody/taste").status_code == 404


def test_my_taste_shows_what_i_answered_and_what_changed():
    _rate("me", "sea", 5, aspects={"scenery": "good"}, comment="바다가 좋았어요")
    _rate("me", "city", 2, aspects={"stops": "many", "elevation": "hard"})
    taste = client.get("/users/me/taste").json()

    assert taste["stats"]["feedback_count"] == 2
    assert taste["stats"]["average_rating"] == 3.5
    assert taste["stats"]["liked_scenery"][0] == {"label": "바다", "count": 1}
    assert any("신호등" in line for line in taste["summary"])
    assert taste["tuning"]["elevation_scale"] < 1

    stops = next(q for q in taste["answers"] if q["key"] == "stops")
    assert stops["question"].endswith("?")
    assert {a["label"]: a["count"] for a in stops["answers"]}["자주 멈췄어요"] == 1

    newest, oldest = taste["history"]
    assert newest["course_name"] == "시내길" and newest["answers"] == ["자주 멈췄어요", "힘들었어요"]
    assert oldest["course_name"] == "해안길" and oldest["comment"] == "바다가 좋았어요"
    assert oldest["scenery_labels"] == ["바다", "공원"]


def test_every_rating_is_kept_even_for_the_same_course():
    """후기는 코스당 최신 하나지만, 이력에는 평가한 횟수만큼 남는다."""
    _rate("me", "sea", 3)
    _rate("me", "sea", 5)
    taste = client.get("/users/me/taste").json()
    assert [item["rating"] for item in taste["history"]] == [5, 3]
    assert client.get("/courses/sea/reviews").json()["count"] == 1


def test_weight_history_shows_how_the_taste_moved():
    for _ in range(3):
        _rate("me", "city", 1, aspects={"stops": "many"})
    weights = [step["weights"]["signal_free"] for step in client.get("/users/me/taste").json()["weight_history"]]
    assert len(weights) == 3 and weights == sorted(weights) and weights[0] < weights[-1]


def test_people_only_see_their_own_data():
    _rate("me", "sea", 5, aspects={"scenery": "good"})
    _rate("you", "city", 1, aspects={"safety": "unsafe"})
    mine = client.get("/users/me/taste").json()
    assert [item["course_id"] for item in mine["history"]] == ["sea"]
    assert not any("불안" in line for line in mine["summary"])


def test_deleting_my_data_removes_everything_about_me():
    _rate("me", "sea", 5, comment="좋아요")
    client.post("/favorites", json={"user_id": "me", "course_id": "sea"})
    _rate("you", "sea", 4)

    deleted = client.delete("/users/me").json()["deleted"]
    assert deleted["profiles"] == 1 and deleted["feedback_log"] == 1 and deleted["favorites"] == 1
    assert client.get("/users/me/taste").status_code == 404
    assert client.get("/favorites/me").json()["favorites"] == []
    assert client.get("/courses/sea/reviews").json()["count"] == 1   # 다른 사람의 후기는 그대로
    assert client.delete("/users/me").status_code == 404


def test_profile_from_the_old_json_file_is_moved_into_the_db(tmp_path):
    """예전에는 JSON 파일 하나에 전부 넣어 두었다. 그 사람이 다시 오면 DB로 옮겨 온다."""
    old = personalize.new_profile("veteran")
    old["n_feedback"] = 7
    with open(personalize.PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump({"veteran": old}, f)
    assert user_store.get_profile("veteran") is None
    assert personalize.load_profile("veteran")["n_feedback"] == 7
    assert user_store.get_profile("veteran")["n_feedback"] == 7


def test_deleted_data_does_not_come_back_from_the_old_file():
    """예전 JSON 파일에 남은 사본 때문에, 지운 사람의 취향이 다음 조회 때 되살아나면 안 된다."""
    old = personalize.new_profile("veteran")
    with open(personalize.PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump({"veteran": old, "other": personalize.new_profile("other")}, f)
    assert client.get("/users/veteran/taste").status_code == 200    # 파일에서 DB로 옮겨 온다
    assert client.delete("/users/veteran").status_code == 200
    assert client.get("/users/veteran/taste").status_code == 404
    with open(personalize.PROFILES_PATH, encoding="utf-8") as f:
        assert list(json.load(f)) == ["other"]
