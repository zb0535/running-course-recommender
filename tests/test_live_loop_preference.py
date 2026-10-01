from fastapi import BackgroundTasks

from src.api import main


def test_current_location_loop_prefers_live_loop_when_db_courses_exist(monkeypatch):
    db_course = {
        "id": "stored-course",
        "name": "저장 코스",
        "path": [[34.0, 127.0], [34.01, 127.01]],
        "distance_km": 2.0,
        "elevation_gain_m": 10,
        "tags": [],
    }
    generated_courses = [
        {
            "id": "loop-over-target", "name": "긴 순환 코스",
            "path": [[34.0, 127.0], [34.01, 127.01], [34.0, 127.0]],
            "distance_km": 5.433, "elevation_gain_m": 10, "safety_score": 0.7,
            "traffic_signal_count": 0, "tags": [], "route_type": "loop",
            "source": "live_generated",
        },
        {
            "id": "loop-on-target", "name": "목표 거리 순환 코스",
            "path": [[34.0, 127.0], [34.005, 127.005], [34.0, 127.0]],
            "distance_km": 5.0, "elevation_gain_m": 10, "safety_score": 0.7,
            "traffic_signal_count": 0, "tags": [], "route_type": "loop",
            "source": "live_generated",
        },
    ]
    generated_calls = []

    monkeypatch.setattr(main, "load_courses", lambda: [db_course])
    monkeypatch.setattr(main, "filter_nearby", lambda courses, lat, lng, radius: courses)
    monkeypatch.setattr(main, "generate_loop_candidates", lambda *args, **kwargs: generated_calls.append(args) or generated_courses)

    response = main.post_recommend(
        main.RecommendRequest(
            current_lat=34.0,
            current_lng=127.0,
            route_type="loop",
            preferred_distance_km=5.0,
            use_live_environment=False,
        ),
        BackgroundTasks(),
    )

    assert response["source"] == "generated"
    assert [result["course"]["id"] for result in response["results"]] == [
        "loop-on-target", "loop-over-target",
    ]
    assert all(result["course"]["path"][0] == result["course"]["path"][-1] for result in response["results"])
    assert response["results"][0]["score"] > response["results"][1]["score"]
    assert generated_calls