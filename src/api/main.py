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
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

from ..data_collection.add_course import add_course as register_course_full
from ..data_collection.refresh_route import refresh_course_in_db
from ..data_collection.scenery import tag_descriptions
from ..recommend.companion import COMPANIONS, companion_options
from ..recommend.destination import course_to_destination
from ..recommend.environment import environment_context_map
from ..recommend.freshness import stale_courses, stamp_verified
from ..recommend.generate_live import generate_loop_candidates
from ..recommend.location import filter_nearby
from ..recommend.nl_keywords import extract_tags
from ..recommend.profile import purposes_for, resolve_target_distance_km
from ..data_collection.enrich import haversine_m
from ..recommend.route_type import apply_route_type, attach_actual_return_path, is_closed_loop
from ..recommend.personalize import (explain, learn, load_profile, new_profile, recalled, remember_recommendation,
                                     save_profile, weights_for)
from ..recommend.score import component_scores, filter_by_required_tags, recommend
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

SCENERY_TAGS = tag_descriptions()  # {태그: 붙는 기준}


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


LOOP_REUSE_START_M = 600        # 출발점이 이보다 멀면 거기까지 걸어가야 하므로 재사용하지 않는다
LOOP_REUSE_DISTANCE_RATIO = 0.2  # 목표 거리와 20% 넘게 차이 나면 다른 코스로 본다


def reusable_loops(courses: list, lat: float, lng: float, target_km: Optional[float]) -> list:
    """이미 만들어 둔 순환 코스 중 지금 요청에 그대로 쓸 수 있는 것."""
    if not target_km:
        return []
    usable = []
    for course in courses:
        path = course.get("path") or []
        if not path or not is_closed_loop(course):
            continue
        if haversine_m((lat, lng), path[0]) > LOOP_REUSE_START_M:
            continue
        distance = course.get("distance_km", 0)
        if abs(distance - target_km) / target_km <= LOOP_REUSE_DISTANCE_RATIO:
            usable.append(course)
    return usable


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
    route_type: Literal["roundtrip", "loop", "oneway"] = "roundtrip"  # 왕복 / 순환 / 편도
    user_id: Optional[str] = None           # 주면 이 사람의 만족도로 학습한 가중치로 추천한다
    destination: Optional[str] = None       # 목적지 장소 이름 (예: "오동도, 여수")
    destination_lat: Optional[float] = None  # 장소 이름 대신 좌표로 지정할 때
    destination_lng: Optional[float] = None
    companion: Optional[str] = None  # 값 목록: GET /onboarding/companions
    time_of_day: Optional[Literal["morning", "afternoon", "evening", "night"]] = None  # 생략하면 서버 시각 기준

    @field_validator("environment_tags")
    @classmethod
    def _known_scenery(cls, v):
        # 없는 태그(예: "벚꽃길")를 조용히 받으면 결과가 0개로 나가고 앱은 이유를 모른다
        unknown = [tag for tag in (v or []) if tag not in SCENERY_TAGS]
        if unknown:
            raise ValueError(f"알 수 없는 풍경 태그 {unknown}. 사용할 수 있는 값은 GET /onboarding/scenery 참고")
        return v

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
    score_breakdown: Optional[dict] = None  # 항목별 적합도 0~1 (왜 이 코스인지)


class FeedbackRequest(BaseModel):
    user_id: str
    course_id: str
    rating: int = Field(ge=1, le=5, description="러닝 후 만족도 1~5")


class RecommendResponse(BaseModel):
    results: List[CourseResult]
    source: str  # "db" | "generated"


@app.get("/onboarding/purposes")
def onboarding_purposes(experience_level: str = "beginner"):
    """숙련도별로 물어볼 목적 항목. 앱이 문항을 하드코딩하지 않도록 서버가 내려준다.

    입문자에게 "인터벌 훈련"을, 상급자에게 "5km 완주"를 물어보면 온보딩이 어색해진다.
    """
    return {"experience_level": experience_level, "purposes": purposes_for(experience_level)}


@app.get("/onboarding/scenery")
def onboarding_scenery():
    """고를 수 있는 풍경 목록. 실제로 그 풍경을 가진 코스가 있는 것만, 붙는 기준과 함께 내려준다.

    코스가 하나도 없는 풍경은 뺀다 — 골라도 결과가 0개인 선택지를 보여주지 않기 위해서다.
    """
    counts = {}
    for course in load_courses():
        for tag in course.get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    options = [
        {"value": tag, "description": description, "course_count": counts[tag]}
        for tag, description in SCENERY_TAGS.items() if counts.get(tag)
    ]
    options.sort(key=lambda o: -o["course_count"])
    return {"scenery": options}


@app.get("/onboarding/companions")
def onboarding_companions():
    return {"companions": companion_options()}


@app.post("/feedback")
def post_feedback(req: FeedbackRequest):
    """러닝 후 만족도를 받아 그 사람의 가중치를 학습한다.

    무엇 때문에 추천했는지(항목별 적합도)는 추천할 때 남겨 두었으므로, 앱은 코스 id와
    점수만 보내면 된다.
    """
    profile = load_profile(req.user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="추천을 받은 적 없는 사용자입니다. 먼저 user_id를 넣어 추천을 받아야 합니다.")
    remembered = recalled(profile, req.course_id)
    if remembered is None:
        raise HTTPException(status_code=404, detail="이 사용자에게 최근 추천된 코스가 아닙니다.")
    scores, baseline = remembered
    learn(profile, scores, req.rating, baseline)
    save_profile(profile)
    return _profile_view(profile)


@app.get("/profile/{user_id}")
def get_profile(user_id: str):
    """학습된 취향. 어떤 요소를 기본값보다 더/덜 중시하게 됐는지 설명과 함께."""
    profile = load_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="프로필이 없습니다.")
    return _profile_view(profile)


def _profile_view(profile: dict) -> dict:
    return {
        "user_id": profile["user_id"],
        "n_feedback": profile["n_feedback"],
        "survey": profile.get("survey", {}),
        "weights": {k: round(v, 3) for k, v in profile["weights"].items()},
        "explanation": explain(profile),
    }


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


def _user_from_request(req) -> dict:
    user = req.model_dump()
    tags = set(user.get("environment_tags") or [])
    if req.text:
        tags |= extract_tags(req.text)
    user["environment_tags"] = tags
    return user


SURVEY_FIELDS = ("experience_level", "pace_min_per_km", "purpose", "companion",
                 "elevation_preference", "environment_tags", "preferred_distance_km")


@app.post("/recommend", response_model=RecommendResponse)
def post_recommend(req: RecommendRequest, background_tasks: BackgroundTasks):
    """user_id를 주면 그 사람의 학습된 가중치로 추천하고, 평가를 받을 수 있게 추천 근거를 남긴다."""
    user = _user_from_request(req)

    profile = None
    if req.user_id:
        profile = load_profile(req.user_id)
        if profile is None:
            # 첫 요청의 온보딩 답변이 곧 초기 설문이다
            survey = {k: user.get(k) for k in SURVEY_FIELDS if user.get(k) not in (None, [], set())}
            survey["environment_tags"] = sorted(survey.get("environment_tags", []))
            profile = new_profile(req.user_id, survey=survey)
    weights = weights_for(profile, req.use_live_environment) if profile else None

    response = _recommend(req, background_tasks, user, weights)

    # 항목별 적합도를 같이 내려준다: 앱은 "왜 이 코스인지" 보여줄 수 있고,
    # 서버는 나중에 만족도 평가가 오면 이걸로 이 사람의 가중치를 학습한다
    breakdowns = []
    for result in response["results"]:
        course = result["course"]
        env = environment_context_map([course]).get(course["id"]) if req.use_live_environment else None
        breakdown = component_scores(course, user, env)
        result["score_breakdown"] = {k: round(v, 3) for k, v in breakdown.items()}
        breakdowns.append((course["id"], breakdown))

    if profile is not None:
        for course_id, breakdown in breakdowns:
            # 비교 기준은 "이 코스 말고 같이 보여준 다른 코스들"의 평균
            others = [b for cid, b in breakdowns if cid != course_id]
            baseline = {k: sum(o[k] for o in others) / len(others) for k in breakdown} if others else None
            remember_recommendation(profile, course_id, breakdown, baseline)
        save_profile(profile)
    return response


def _recommend(req: RecommendRequest, background_tasks: BackgroundTasks, user: dict, weights: Optional[dict]):
    tags = user["environment_tags"]
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
        if course.get("route_type") == "loop":
            return course
        if target_km:
            return truncate_course(course, target_km)
        return course

    # 현재 위치에서 왕복을 요청하면 저장된 편도 코스를 되짚지 않고,
    # 가상 꼭짓점 + OSM 스냅 + Tmap passList로 새 순환 코스를 만든다.
    # 목적지를 지정했으면 추천이 아니라 지정이다 — 점수로 고르지 않고 거기까지 경로를 만든다.
    if req.destination or (req.destination_lat is not None and req.destination_lng is not None):
        if not has_location:
            raise HTTPException(
                status_code=400,
                detail="목적지까지의 코스를 만들려면 현위치(current_lat, current_lng)가 필요합니다.",
            )
        try:
            course = course_to_destination(
                req.current_lat, req.current_lng, req.destination,
                req.destination_lat, req.destination_lng, route_type=req.route_type,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=f"{e} .env에 TMAP_APP_KEY를 넣고 서버를 다시 시작하세요.")
        return {"results": [{"course": course, "score": 1.0}], "source": "destination"}

    # 순환은 갔던 길을 되짚는 왕복과 다른 코스다. 고른 대로 내보낸다.
    wants_loop = req.route_type == "loop"
    if wants_loop and not has_location:
        raise HTTPException(
            status_code=400,
            detail="순환 코스는 그 자리에서 만들기 때문에 현위치(current_lat, current_lng)가 필요합니다.",
        )

    prefer_live_loop = wants_loop
    if prefer_live_loop:
        # 그 자리에 맞는 루프를 전에 만들어 뒀으면 다시 만들지 않는다. 생성 1건에 Tmap 3건이
        # 드는데 무료 한도가 하루 1,000건이라, 매번 만들면 하루 333회로 팀 전체가 막힌다.
        # 왕복 변환을 거친 코스는 편도라도 시작=끝이 되므로, 반드시 원본으로 판단한다
        cached = reusable_loops(list(originals.values()), req.current_lat, req.current_lng, target_km)
        # 요청한 풍경이 아닌 순환은 재사용하지 않는다. 재사용하면 태그 필터에 전부 걸려 결과가 0개가 된다.
        cached = filter_by_required_tags(cached, user)
        if cached:
            candidates, originals = cached, {c["id"]: c for c in cached}
            prefer_live_loop = False

    if (not has_location or candidates) and not prefer_live_loop:
        env_context_map = build_env_context_map(candidates, req.use_live_environment)
        ranked = recommend(candidates, user, top_n=req.top_n, env_context_map=env_context_map, weights=weights)

        # 저장된 경로는 즉시 주되, 오래된 건 뒤에서 갱신해 다음 사람이 최신 경로를 받게 한다.
        # 한 요청이 Tmap 할당량을 몰아 쓰지 않도록 한 번에 한 개만 갱신한다.
        for stale in stale_courses([c for c, _ in ranked], limit=1):
            background_tasks.add_task(refresh_in_background, stale["id"])

        # 턴바이턴 위치 계산은 결과로 나갈 코스에만 한다
        return {
            "results": [
                {
                    "course": trim_if_needed(
                        apply_route_type(
                            attach_actual_return_path(originals[c["id"]])
                            if req.route_type == "roundtrip" else originals[c["id"]],
                            req.route_type,
                        )
                    ),
                    "score": round(s, 4),
                }
                for c, s in ranked
            ],
            "source": "db",
        }

    # 순환 요청은 서로 다른 경유지 배치로 여러 루프를 만든 뒤 개인화 점수로 정렬한다.
    generate_km = target_km or 3.0
    try:
        generated_candidates = generate_loop_candidates(
            req.current_lat, req.current_lng, generate_km, tags, route_type=req.route_type
        )
    except ValueError as e:
        if prefer_live_loop and candidates:
            # 실시간 외부 API가 일시적으로 실패하면 기존 DB 추천은 계속 제공한다.
            env_context_map = build_env_context_map(candidates, req.use_live_environment)
            ranked = recommend(candidates, user, top_n=req.top_n, env_context_map=env_context_map, weights=weights)
            return {
                "results": [
                    {
                        "course": trim_if_needed(
                            apply_route_type(
                                attach_actual_return_path(originals[c["id"]]), req.route_type
                            )
                        ),
                        "score": round(s, 4),
                    }
                    for c, s in ranked
                ],
                "source": "db",
            }
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        if prefer_live_loop and candidates:
            env_context_map = build_env_context_map(candidates, req.use_live_environment)
            ranked = recommend(candidates, user, top_n=req.top_n, env_context_map=env_context_map, weights=weights)
            return {
                "results": [
                    {
                        "course": trim_if_needed(
                            apply_route_type(
                                attach_actual_return_path(originals[c["id"]]), req.route_type
                            )
                        ),
                        "score": round(s, 4),
                    }
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
        weights=weights,
    )
    for course in generated_candidates:
        # 요청한 풍경이 아니어도 만든 코스는 저장해 둔다 — 다음에 조건이 맞는 요청에서 다시 쓴다
        background_tasks.add_task(register_in_background, course)
    if not ranked:
        # 만들긴 했지만 요청한 풍경을 지나는 코스가 없다. 빈 목록을 200으로 주면 앱은 이유를 모른다.
        wanted = ", ".join(sorted(tags)) or "조건"
        raise HTTPException(
            status_code=404,
            detail=f"이 주변에서는 '{wanted}'을(를) 지나는 코스를 만들지 못했습니다. 풍경 조건을 빼거나 다른 코스 형태로 시도해 보세요.",
        )
    return {
        "results": [
            {"course": trim_if_needed(course), "score": round(score, 4)}
            for course, score in ranked
        ],
        "source": "generated",
    }
