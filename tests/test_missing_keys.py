"""API 키가 없는 환경(=저장소를 갓 clone한 팀원)에서 앱이 어떻게 반응해야 하는지.

키를 소스에 넣지 않는 건 맞지만, 키가 없을 때 "Internal Server Error"나 빈 지도로
끝나면 받는 쪽은 원인을 알 수 없다. 무엇이 왜 안 되는지 반드시 알려줘야 한다.
"""
from fastapi.testclient import TestClient

from src.api.main import app
from src.recommend import generate_live

client = TestClient(app)


def test_health_reports_which_keys_are_missing(monkeypatch):
    monkeypatch.delenv("TMAP_APP_KEY", raising=False)
    body = client.get("/health").json()
    assert body["keys"]["TMAP_APP_KEY"] is False


def test_health_never_leaks_key_values(monkeypatch):
    monkeypatch.setenv("TMAP_APP_KEY", "super-secret-value")
    assert "super-secret-value" not in client.get("/health").text


def test_missing_tmap_key_returns_actionable_error_not_500(monkeypatch):
    monkeypatch.delenv("TMAP_APP_KEY", raising=False)
    monkeypatch.setattr(generate_live, "find_nearby_point", lambda *a, **kw: (34.75, 127.75))

    resp = client.post("/recommend", json={
        "current_lat": 34.7604, "current_lng": 127.6622,
        "preferred_distance_km": 3, "max_distance_km": 0.01,
        "environment_tags": ["바다뷰"], "use_live_environment": False,
    })

    assert resp.status_code == 503
    assert "TMAP_APP_KEY" in resp.json()["detail"]


def test_demo_page_warns_when_map_key_is_missing(monkeypatch):
    monkeypatch.delenv("TMAP_APP_KEY", raising=False)
    assert "TMAP_APP_KEY" in client.get("/").text
