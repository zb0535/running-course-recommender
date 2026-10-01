"""테스트가 실제 데이터와 외부 API를 건드리지 않도록 격리한다.

이게 없으면 테스트 한 번에 `data/courses.json`에 코스가 실제로 등재되고(백그라운드 등록이
TestClient에서 그대로 실행된다), Tmap 무료 할당량이 소모된다. 실제로 그런 일이 있었다.
"""
import json
import shutil

import pytest
import requests

from src.api import main
from src.data_collection import refresh_route


@pytest.fixture(autouse=True)
def isolated_course_db(tmp_path, monkeypatch):
    """코스 DB를 임시 파일로 복사해서 쓴다 — 테스트가 진짜 DB를 고치지 못하게."""
    # 테스트가 자기 tmp_path에 courses.json을 쓰기도 하므로 이름을 겹치지 않게 둔다
    copy = tmp_path / "isolated_courses.json"
    shutil.copy(main.MAIN_DB_PATH, copy)
    monkeypatch.setattr(main, "MAIN_DB_PATH", str(copy))
    monkeypatch.setattr(refresh_route, "DB_PATH", str(copy))
    monkeypatch.setattr(main, "_db_cache", {"key": None, "courses": None})
    # 사용자 프로필도 테스트마다 빈 파일에서 시작한다
    from src.recommend import personalize
    monkeypatch.setattr(personalize, "PROFILES_PATH", str(tmp_path / "user_profiles.json"))
    # 지형 캐시(data/osm_cache)는 저장소에 없다. 내 PC에 있다고 테스트 결과가 달라지면 안 되므로
    # 테스트에서는 항상 캐시가 없는 상태로 둔다 (필요한 테스트는 tags_for_path를 monkeypatch 한다)
    from src.data_collection import scenery, scenery_osm
    monkeypatch.setattr(scenery_osm, "CACHE_DIR", str(tmp_path / "osm_cache"))
    monkeypatch.setattr(scenery, "_index_cache", {})
    yield copy


@pytest.fixture(autouse=True)
def no_outbound_requests(monkeypatch):
    """외부 호출은 기본적으로 막는다. 필요한 테스트는 각자 상위 함수를 monkeypatch 한다."""
    def blocked(*args, **kwargs):
        # 실제 네트워크 장애와 같은 예외로 막아야 코드의 장애 대응(폴백) 경로가 그대로 검증된다
        raise requests.ConnectionError("테스트에서 외부 API를 호출했습니다. 해당 함수를 monkeypatch 하세요.")

    for name in ("get", "post", "request"):
        monkeypatch.setattr(requests, name, blocked)
    # 한 테스트의 Overpass 장애 쿨다운이 다음 테스트로 새지 않게
    from src.data_collection import osm_overpass
    monkeypatch.setattr(osm_overpass, "_retry_after", 0.0)


@pytest.fixture
def course_db(isolated_course_db):
    with open(isolated_course_db, encoding="utf-8") as f:
        return json.load(f)
