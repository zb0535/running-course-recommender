from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_recommend_with_free_text():
    payload = {
        "preferred_distance_km": 4,
        "elevation_preference": "low",
        "text": "바다가 보이는 코스 추천해줘",
        "top_n": 1,
        "use_live_environment": False,
    }
    resp = client.post("/recommend", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    results = body["results"]
    assert body["source"] == "db"
    assert len(results) == 1
    # 실제 코스 DB는 라이브 데이터로 계속 갱신되므로 특정 id 대신 태그로 검증
    assert "바다뷰" in results[0]["course"]["tags"]


def test_companion_sent_through_the_api_actually_filters():
    """점수 함수만 테스트하면 API가 필드를 버려도 모른다 — 요청 경로 끝까지 확인한다."""
    resp = client.post("/recommend", json={
        "preferred_distance_km": 3, "companion": "유아차", "top_n": 10, "use_live_environment": False,
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results, "결과가 비면 필터가 동작하는지 알 수 없다"
    for r in results:
        assert r["course"]["elevation_gain_m"] <= 40


def test_companion_caps_apply_to_the_distance_actually_run():
    """코스는 목표 거리로 잘려서 나가므로, 상한은 잘린 뒤 실제로 뛰는 거리에 적용해야 한다.
    왕복(거리·고도 2배) 전체 길이로 거르면 입문자 30분 + 유아차 요청이 결과 0개가 된다."""
    resp = client.post("/recommend", json={
        "preferred_time_min": 30, "experience_level": "beginner", "companion": "유아차",
        "top_n": 3, "use_live_environment": False,
    })
    assert resp.status_code == 200
    assert resp.json()["results"]


def test_unknown_companion_is_rejected_instead_of_silently_ignored():
    resp = client.post("/recommend", json={"companion": "유모차", "use_live_environment": False})
    assert resp.status_code == 422


def test_time_of_day_is_part_of_the_request_contract():
    ok = client.post("/recommend", json={"time_of_day": "night", "top_n": 1, "use_live_environment": False})
    bad = client.post("/recommend", json={"time_of_day": "midnight", "use_live_environment": False})
    assert ok.status_code == 200
    assert bad.status_code == 422  # 모델에 없는 필드면 조용히 무시돼 422가 나지 않는다


def test_onboarding_lists_companion_options():
    options = client.get("/onboarding/companions").json()["companions"]
    assert {"value": "유아차", "label": "유아차를 밀면서"} in options


def test_recommend_with_nearby_location_uses_db():
    payload = {
        "preferred_distance_km": 4,
        "text": "바다가 보이는 코스",
        "top_n": 1,
        "use_live_environment": False,
        "current_lat": 34.7393,
        "current_lng": 127.7359,
        "max_distance_km": 5,
    }
    resp = client.post("/recommend", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "db"
    assert len(body["results"]) == 1


def test_scenery_options_list_only_tags_that_have_courses():
    """골라도 결과가 0개인 선택지는 보여주지 않는다."""
    options = client.get("/onboarding/scenery").json()["scenery"]
    assert options, "풍경 선택지가 비어 있다"
    assert all(o["course_count"] >= 1 for o in options)
    assert all(o["hint"] and o["group"] for o in options)
    values = [o["value"] for o in options]
    assert "바다" in values and "강·하천" in values


def test_every_offered_scenery_tag_returns_courses():
    for option in client.get("/onboarding/scenery").json()["scenery"]:
        resp = client.post("/recommend", json={"environment_tags": [option["value"]], "route_type": "oneway",
                                                "use_live_environment": False})
        assert resp.json()["results"], f"{option['value']}을 골랐는데 결과가 없다"


def test_unknown_scenery_tag_is_rejected():
    resp = client.post("/recommend", json={"environment_tags": ["벚꽃길"], "use_live_environment": False})
    assert resp.status_code == 422


def test_nearby_courses_without_the_requested_scenery_fall_through_to_generation(monkeypatch):
    """근처에 저장된 코스가 있어도 요청한 풍경이 없으면 빈 결과(200)가 아니라 그 자리에서 만든다."""
    from src.api import main

    made = {"id": "generated-test", "name": "만든 코스", "region": "실시간 생성", "distance_km": 3.0,
            "elevation_gain_m": 5, "surface": "paved", "safety_score": 0.7, "route_type": "oneway",
            "path": [[34.7393, 127.7359], [34.75, 127.74]], "steps": [], "tags": ["호수"], "source": "live_generated"}
    monkeypatch.setattr(main, "generate_loop_candidates", lambda *a, **k: [dict(made)])
    monkeypatch.setattr(main, "register_in_background", lambda course: None)
    resp = client.post("/recommend", json={
        "preferred_distance_km": 3, "route_type": "oneway", "environment_tags": ["호수"],
        "current_lat": 34.7393, "current_lng": 127.7359, "max_distance_km": 1, "use_live_environment": False,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "generated"
    assert body["results"][0]["course"]["id"] == "generated-test"
