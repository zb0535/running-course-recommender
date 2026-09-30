"""Android 앱이 붙일 추천 API 스켈레톤.

실행:
    uvicorn src.api.main:app --reload
    POST /recommend 로 온보딩 조건(+선택적 자연어 문장)을 보내면 상위 N개 코스를 반환.

현위치(current_lat/current_lng)를 같이 보내면:
    1) 그 주변 max_distance_km 안에 등록된 코스가 있으면 거기서 골라서 즉시 반환 (빠름)
    2) 없으면 그 자리에서 실제 도로/지형 데이터로 코스를 새로 만들어 반환하고(수 초 소요),
       백그라운드로 정밀 보강해 DB에 등록해 다음 요청부터는 빨라지게 한다.
"""
import json
import os
from contextlib import asynccontextmanager
from typing import List, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator
from dotenv import load_dotenv

from ..data_collection.add_course import add_course as register_course_full
from ..data_collection.refresh_route import refresh_course_in_db
from ..recommend.companion import COMPANIONS, companion_options
from ..recommend.environment import environment_context_map
from ..recommend.freshness import stale_courses, stamp_verified
from ..recommend.generate_live import generate_loop_candidates
from ..recommend.location import filter_nearby
from ..recommend.nl_keywords import extract_tags
from ..recommend.profile import purposes_for, resolve_target_distance_km
from ..recommend.route_type import apply_route_type, attach_actual_return_path
from ..recommend.score import recommend, score_course_breakdown
from ..recommend.trim import truncate_course

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
MAIN_DB_PATH = os.path.join(DATA_DIR, "courses.json")
SAMPLE_DB_PATH = os.path.join(DATA_DIR, "courses.sample.json")

# 키가 없으면 무엇이 안 되는지. 키를 소스에 넣지 않는 대신 이걸로 안내한다.
API_KEYS = {
    # 지도 그림은 MapLibre+OpenFreeMap(키 불필요)이라, 이 키는 경로·안내 데이터에만 쓰인다
    "TMAP_APP_KEY": "코스 생성, 턴바이턴 안내",
    "KMA_API_KEY": "실시간 날씨 반영",
}


def missing_keys() -> list:
    return [name for name in API_KEYS if not os.environ.get(name)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = missing_keys()
    if missing:
        lines = ["", "=" * 62, "  환경변수(API 키)가 설정되지 않아 일부 기능이 동작하지 않습니다."]
        lines += [f"    - {name:<18} 없음 -> {API_KEYS[name]} 불가" for name in missing]
        lines += ["", "  해결: .env.example을 .env로 복사하고 키를 채운 뒤 서버를 다시 시작하세요.",
                  "        (DB에 저장된 코스 추천은 키 없이도 동작합니다)", "=" * 62, ""]
        print("\n".join(lines), flush=True)
    yield


app = FastAPI(title="러닝 코스 추천 API", lifespan=lifespan)


_db_cache = {"key": None, "courses": None}


def load_courses() -> list:
    """파일이 바뀌었을 때만 다시 읽는다(백그라운드 갱신·신규 등록이 파일을 고치면 수정 시각이 바뀜).
    요청마다 전체 JSON을 파싱하면 경로·복귀 경로까지 담긴 DB라 그것만으로 0.15초가 든다."""
    path = MAIN_DB_PATH if os.path.exists(MAIN_DB_PATH) else SAMPLE_DB_PATH
    key = (path, os.path.getmtime(path))
    if _db_cache["key"] != key:
        with open(path, encoding="utf-8") as f:
            _db_cache["courses"] = json.load(f)
        _db_cache["key"] = key
    return _db_cache["courses"]


def build_env_context_map(courses: list, enabled: bool) -> Optional[dict]:
    if not enabled:
        return None
    return environment_context_map(courses)


def register_in_background(course: dict):
    try:
        register_course_full(stamp_verified(course), MAIN_DB_PATH)
    except Exception as e:
        print(f"[background] 코스 정밀 보강 등록 실패 ({course.get('id')}): {e}")


def refresh_in_background(course_id: str):
    """오래된 경로를 뒤에서 다시 받아 둔다. 실패해도 사용자 응답에는 영향이 없다."""
    try:
        refreshed = refresh_course_in_db(course_id, MAIN_DB_PATH)
        if refreshed and refreshed.get("previous_distance_km"):
            print(f"[background] 경로 변경 감지 {course_id}: "
                  f"{refreshed['previous_distance_km']}km -> {refreshed['distance_km']}km")
    except Exception as e:
        print(f"[background] 경로 갱신 실패 ({course_id}): {e}")


class RecommendRequest(BaseModel):
    # 페이스는 아는 사람만 직접 입력하고, 모르면 숙련도로 추정한다 (src/recommend/profile.py)
    pace_min_per_km: Optional[float] = None
    experience_level: Optional[Literal["beginner", "intermediate", "advanced"]] = None
    purpose: Optional[str] = None
    preferred_distance_km: Optional[float] = None
    preferred_time_min: Optional[int] = None
    elevation_preference: Optional[str] = "medium"
    environment_tags: Optional[List[str]] = []
    text: Optional[str] = None
    top_n: int = 5
    use_live_environment: bool = True
    current_lat: Optional[float] = None
    current_lng: Optional[float] = None
    max_distance_km: float = 5.0
    route_type: Literal["roundtrip", "oneway"] = "roundtrip"
    preview_candidates: bool = False
    companion: Optional[str] = None  # 값 목록: GET /onboarding/companions
    time_of_day: Optional[Literal["morning", "afternoon", "evening", "night"]] = None  # 생략하면 서버 시각 기준

    @field_validator("companion")
    @classmethod
    def _known_companion(cls, v):
        # 오타("유모차")를 조용히 무시하면 앱은 반영된 줄 안다 — 거부해서 바로 드러나게 한다
        if v is not None and v not in COMPANIONS:
            raise ValueError(f"companion은 {list(COMPANIONS)} 중 하나여야 합니다")
        return v


class CourseResult(BaseModel):
    course: dict
    score: float
    score_breakdown: List[dict] = []


class RecommendResponse(BaseModel):
    results: List[CourseResult]
    source: str  # "db" | "generated"


@app.get("/onboarding/purposes")
def onboarding_purposes(experience_level: str = "beginner"):
    """숙련도별로 물어볼 목적 항목. 앱이 문항을 하드코딩하지 않도록 서버가 내려준다.

    입문자에게 "인터벌 훈련"을, 상급자에게 "5km 완주"를 물어보면 온보딩이 어색해진다.
    """
    return {"experience_level": experience_level, "purposes": purposes_for(experience_level)}


@app.get("/onboarding/companions")
def onboarding_companions():
    return {"companions": companion_options()}


@app.get("/health")
def health():
    """키 '값'은 절대 내보내지 않고, 설정 여부만 알려준다 (팀원 환경 자가진단용)."""
    return {
        "status": "ok",
        "keys": {name: bool(os.environ.get(name)) for name in API_KEYS},
    }


@app.get("/")
def navigate_page():
    path = os.path.join(os.path.dirname(__file__), "static", "navigate.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()

    # 지도 자체는 키가 없어도 뜨지만(OpenFreeMap), 현위치 기반 코스 생성은 Tmap 경로가 필요하다.
    warning = ""
    if not os.environ.get("TMAP_APP_KEY"):
        warning = (
            '<div class="setup-warning"><strong>TMAP_APP_KEY 환경변수가 설정되지 않았습니다 — '
            '현위치 기반 코스 생성을 쓸 수 없습니다.</strong>'
            '프로젝트 폴더의 <code>.env.example</code>을 <code>.env</code>로 복사하고 '
            'TMAP_APP_KEY 값을 채운 뒤 서버를 다시 시작하세요. '
            '지도 표시와 DB에 저장된 코스 추천·턴바이턴 안내는 키 없이도 동작합니다.</div>'
        )

    html = html.replace("{{KEY_WARNING}}", warning)
    return HTMLResponse(html)


@app.post("/recommend", response_model=RecommendResponse)
def post_recommend(req: RecommendRequest, background_tasks: BackgroundTasks):
    user = req.model_dump()
    tags = set(user.get("environment_tags") or [])
    if req.text:
        tags |= extract_tags(req.text)
    user["environment_tags"] = tags
    # "30분 뛸래"처럼 시간만 준 경우 페이스로 거리를 환산해 이후 단계 전부에서 같은 값을 쓴다
    target_km = resolve_target_distance_km(user)

    courses = load_courses()
    has_location = req.current_lat is not None and req.current_lng is not None

    candidates = courses
    if has_location:
        candidates = filter_nearby(courses, req.current_lat, req.current_lng, req.max_distance_km)
    originals = {c["id"]: c for c in candidates}
    candidates = [apply_route_type(c, req.route_type, with_steps=False) for c in candidates]

    def trim_if_needed(course: dict) -> dict:
        # 생성된 순환 루프를 단순 거리 절단하면 마지막 복귀 구간이 잘려 열린 코스가 된다.
        if course.get("source") == "live_generated" and course.get("route_type") == "roundtrip":
            return course
        if target_km:
            return truncate_course(course, target_km)
        return course

    def result_payload(course: dict, scored_course: dict, score: float,
                       env_context_map: Optional[dict]) -> dict:
        context = (env_context_map or {}).get(scored_course["id"])
        breakdown = score_course_breakdown(scored_course, user, env_context=context)
        return {
            "course": trim_if_needed(course),
            "score": round(score, 4),
            "score_breakdown": breakdown["factors"],
        }

    # 현재 위치에서 왕복을 요청하면 저장된 편도 코스를 되짚지 않고,
    # 가상 꼭짓점 + OSM 스냅 + Tmap passList로 새 순환 코스를 만든다.
    prefer_live_loop = has_location and req.route_type == "roundtrip"

    if (not has_location or candidates) and not prefer_live_loop:
        env_context_map = build_env_context_map(candidates, req.use_live_environment)
        ranked = recommend(candidates, user, top_n=req.top_n, env_context_map=env_context_map)

        # 저장된 경로는 즉시 주되, 오래된 건 뒤에서 갱신해 다음 사람이 최신 경로를 받게 한다.
        # 한 요청이 Tmap 할당량을 몰아 쓰지 않도록 한 번에 한 개만 갱신한다.
        for stale in stale_courses([c for c, _ in ranked], limit=1):
            background_tasks.add_task(refresh_in_background, stale["id"])

        # 턴바이턴 위치 계산은 결과로 나갈 코스에만 한다
        return {
            "results": [
                result_payload(
                    apply_route_type(
                        attach_actual_return_path(originals[c["id"]])
                        if req.route_type == "roundtrip" else originals[c["id"]],
                        req.route_type,
                    ),
                    c,
                    s,
                    env_context_map,
                )
                for c, s in ranked
            ],
            "source": "db",
        }

    # 현재 위치 왕복 요청은 서로 다른 경유지 배치로 여러 루프를 만든 뒤 개인화 점수로 정렬한다.
    generate_km = target_km or 3.0
    try:
        generated_candidates = generate_loop_candidates(
            req.current_lat, req.current_lng, generate_km, tags, route_type=req.route_type
        )
    except ValueError as e:
        if prefer_live_loop and candidates:
            # 실시간 외부 API가 일시적으로 실패하면 기존 DB 추천은 계속 제공한다.
            env_context_map = build_env_context_map(candidates, req.use_live_environment)
            ranked = recommend(candidates, user, top_n=req.top_n, env_context_map=env_context_map)
            return {
                "results": [
                    result_payload(
                        apply_route_type(attach_actual_return_path(originals[c["id"]]), req.route_type),
                        c,
                        s,
                        env_context_map,
                    )
                    for c, s in ranked
                ],
                "source": "db",
            }
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        if prefer_live_loop and candidates:
            env_context_map = build_env_context_map(candidates, req.use_live_environment)
            ranked = recommend(candidates, user, top_n=req.top_n, env_context_map=env_context_map)
            return {
                "results": [
                    result_payload(
                        apply_route_type(attach_actual_return_path(originals[c["id"]]), req.route_type),
                        c,
                        s,
                        env_context_map,
                    )
                    for c, s in ranked
                ],
                "source": "db",
            }
        # 키 미설정으로 코스 생성이 불가능한 상태. 500으로 삼키면 받는 쪽이 원인을 알 수 없다.
        raise HTTPException(
            status_code=503,
            detail=f"{e} .env.example을 .env로 복사해 키를 채운 뒤 서버를 다시 시작하세요.",
        )

    generated_env_context_map = build_env_context_map(generated_candidates, req.use_live_environment)
    ranked = recommend(
        generated_candidates,
        user,
        top_n=req.top_n,
        env_context_map=generated_env_context_map,
    )
    if not req.preview_candidates:
        for course, _ in ranked:
            background_tasks.add_task(register_in_background, course)
    return {
        "results": [
            result_payload(course, course, score, generated_env_context_map)
            for course, score in ranked
        ],
        "source": "generated",
    }
