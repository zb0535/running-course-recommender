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
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

from ..data_collection.add_course import add_course as register_course_full
from ..data_collection.refresh_route import refresh_course_in_db
from ..data_collection.scenery import tag_descriptions
from ..recommend.companion import COMPANIONS, companion_options
from ..api_clients.tmap_poi import search_places
from ..recommend.destination import course_to_destination
from ..recommend.environment import environment_context_map
from ..recommend.freshness import stale_courses, stamp_verified
from ..recommend.generate_live import generate_loop_candidates
from ..recommend.location import filter_nearby
from ..recommend.nl_keywords import extract_tags
from ..recommend.profile import purposes_for, resolve_target_distance_km
from ..data_collection.enrich import haversine_m
from ..recommend.route_type import apply_route_type, attach_actual_return_path, is_closed_loop
from ..recommend.personalize import forget_legacy
from ..recommend.personalize import (answer_labels, answers_view, explain, feedback_questions, invalid_aspects, learn,
                                     learn_aspects,
                                     load_profile, new_profile, recalled, remember_recommendation, save_profile,
                                     taste_summary, weights_for)
from ..recommend.score import component_scores, filter_by_required_tags, recommend
from ..recommend.trim import truncate_course
from ..recommend.graph_loop import graph_loop_course, load_engine
from ..recommend.graph_loop_engine import RouteFailure
from ..map_view.graph_geojson import parse_bbox, public_graph
from ..data_collection import local_osm
from ..recommend import auth, nearby_scenery, scenery_choices, user_store
from ..recommend.timeofday import current_period

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
MAIN_DB_PATH = os.path.join(DATA_DIR, "courses.json")
SAMPLE_DB_PATH = os.path.join(DATA_DIR, "courses.sample.json")
# GraphML은 서버의 탐색용 원본이다. 브라우저에는 변환된 GeoJSON만 필요한 영역만 보낸다.
GRAPH_GEOJSON_PATH = os.environ.get("GRAPH_GEOJSON_PATH", os.path.join(DATA_DIR, "graph", "yongbong.geojson"))
GRAPH_LOOP_MAP_PATH = os.environ.get("GRAPH_LOOP_MAP_PATH", os.path.join(DATA_DIR, "graph", "yongbong.graphml"))
GRAPH_LOOP_ENABLED = os.environ.get("GRAPH_LOOP_ENABLED", "0").lower() in {"1", "true", "yes"}

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
    # GraphML은 지역별로 수십~수백 MB까지 커질 수 있다. 일반 추천 배포에서는 로드하지 않고,
    # 용봉동 파일럿을 명시적으로 켠 서버에서만 워커당 한 번 메모리에 올린다.
    if GRAPH_LOOP_ENABLED:
        app.state.graph_loop_engine = load_engine(GRAPH_LOOP_MAP_PATH)
        print(f"[graph-loop] loaded {Path(GRAPH_LOOP_MAP_PATH).name}", flush=True)
    else:
        app.state.graph_loop_engine = None
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
    loop_engine: Literal["tmap", "graphml"] = "tmap"  # graphml은 검증된 지역 파일럿에서만 명시적으로 사용
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
        # 선택지 이름('바다')과 측정 태그('바다뷰') 둘 다 받는다
        unknown = [tag for tag in (v or []) if tag not in SCENERY_TAGS and not scenery_choices.is_choice(tag)]
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
    rating: Optional[dict] = None           # 다른 사용자들의 평가 {"average", "count"} (있을 때만)
    favorite: Optional[bool] = None         # user_id를 줬을 때, 이 사람이 즐겨찾기한 코스인가


class FeedbackRequest(BaseModel):
    user_id: str
    course_id: str
    rating: int = Field(ge=1, le=5, description="러닝 후 만족도 1~5")
    comment: Optional[str] = Field(default=None, max_length=500, description="후기 (선택)")
    # 항목별 답 (선택). 문항과 값은 GET /onboarding/feedback. 예: {"stops": "many", "elevation": "hard"}
    aspects: Optional[dict] = None

    @field_validator("aspects")
    @classmethod
    def _known_aspects(cls, v):
        bad = invalid_aspects(v)
        if bad:
            raise ValueError(f"알 수 없는 항목 {bad}. 문항과 값은 GET /onboarding/feedback 참고")
        return v


class FavoriteRequest(BaseModel):
    user_id: str
    course_id: str
    # 추천 응답으로 받은 코스를 그대로 넣어 준다. 실시간 생성 코스는 아직 DB에 없을 수 있다.
    course: Optional[dict] = None


class RecommendResponse(BaseModel):
    results: List[CourseResult]
    source: str  # "db" | "generated"


class Credentials(BaseModel):
    username: str
    password: str
    # 가입할 때만: 로그인 없이 쓰던 기기의 user_id. 주면 그동안 쌓인 취향·이력·즐겨찾기를 계정으로 옮긴다
    guest_id: Optional[str] = None


def _owner_only(user_id: Optional[str], authorization) -> None:
    """계정의 데이터는 그 계정으로 로그인한 요청만 다룰 수 있다. 손님 id는 그대로 통과한다."""
    try:
        auth.require_owner(user_id, authorization if isinstance(authorization, str) else None)
    except auth.AuthError as error:
        raise HTTPException(status_code=error.status, detail=error.message)


@app.post("/auth/signup")
def auth_signup(req: Credentials):
    """계정을 만들고 로그인한 상태로 돌려준다. 받은 `token`을 이후 요청의 `Authorization: Bearer <token>`으로,
    `user_id`를 각 요청의 user_id로 쓴다."""
    try:
        if req.guest_id:
            load_profile(req.guest_id)   # 예전 파일에만 있던 손님이면 먼저 DB로 가져온 뒤 옮긴다
        return auth.sign_up(req.username, req.password, req.guest_id)
    except auth.AuthError as error:
        raise HTTPException(status_code=error.status, detail=error.message)


@app.post("/auth/login")
def auth_login(req: Credentials):
    try:
        return auth.log_in(req.username, req.password)
    except auth.AuthError as error:
        raise HTTPException(status_code=error.status, detail=error.message)


@app.post("/auth/logout")
def auth_logout(authorization: Optional[str] = Header(default=None)):
    return {"logged_out": auth.log_out(authorization)}


@app.get("/auth/me")
def auth_me(authorization: Optional[str] = Header(default=None)):
    """지금 토큰이 누구 것인지. 앱을 다시 열었을 때 로그인 상태를 확인하는 데 쓴다."""
    user = auth.current_user(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


@app.get("/onboarding/purposes")
def onboarding_purposes(experience_level: str = "beginner"):
    """숙련도별로 물어볼 목적 항목. 앱이 문항을 하드코딩하지 않도록 서버가 내려준다.

    입문자에게 "인터벌 훈련"을, 상급자에게 "5km 완주"를 물어보면 온보딩이 어색해진다.
    """
    return {"experience_level": experience_level, "purposes": purposes_for(experience_level)}


@app.get("/onboarding/scenery")
def onboarding_scenery(lat: Optional[float] = None, lng: Optional[float] = None, radius_km: float = 10.0):
    """고를 수 있는 풍경 선택지. 쉬운 이름과 한 줄 설명으로 준다.

    현위치(lat, lng)를 주면 반경(기본 10km) 안에 실제로 있는 풍경만, 가까운 순서로 준다 — 내륙에서
    '바다'가 선택지로 나오지 않게. 위치를 안 주면 저장된 코스에 있는 풍경 전체.
    받은 `value`를 그대로 `/recommend`의 `environment_tags`에 넣으면 된다.
    """
    courses = load_courses()
    has_location = lat is not None and lng is not None
    if has_location:
        courses = filter_nearby(courses, lat, lng, radius_km)
    around = nearby_scenery.nearby(lat, lng, radius_km) if has_location else None

    options = []
    for name, group, hint, tags in scenery_choices.CHOICES:
        count = sum(1 for course in courses if set(course.get("tags", [])) & set(tags))
        distances = [around[tag] for tag in tags if around and tag in around]
        if not count and not distances:
            continue  # 골라도 결과가 없는 선택지는 보여주지 않는다
        distance = min(distances) if distances else None
        options.append({
            "value": name, "label": name, "group": group, "hint": hint,
            "distance_km": distance, "near": scenery_choices.near_text(distance),
            "course_count": count,
            # 개발자용: 이 선택지가 어떤 측정 기준에 대응하는지
            "criteria": {tag: SCENERY_TAGS[tag] for tag in tags if tag in SCENERY_TAGS},
        })
    if around is not None:
        # 가까운 것부터. 거리를 모르는 것(근처 저장 코스에만 있는 풍경)은 뒤로
        options.sort(key=lambda o: (o["distance_km"] is None, o["distance_km"] or 0))
        basis = "location"
    else:
        basis = "nearby_courses" if has_location else "all_courses"
    return {"scenery": options, "groups": scenery_choices.GROUPS, "basis": basis,
            **({"radius_km": radius_km} if basis == "location" else {})}


@app.get("/places")
def get_places(q: str, lat: Optional[float] = None, lng: Optional[float] = None):
    """목적지 검색 후보. 입력창 아래에 목록으로 보여주고, 고른 것의 좌표를 `/recommend`의
    `destination_lat`·`destination_lng`로 보내면 된다. 현위치를 주면 가까운 곳을 우선한다."""
    return {"places": search_places(q, lat, lng)}


@app.get("/onboarding/feedback")
def onboarding_feedback():
    """러닝 후 물어볼 항목별 질문. 별점과 함께 `/feedback`의 `aspects`로 보낸다 (전부 선택 사항)."""
    return {"questions": feedback_questions()}


@app.get("/onboarding/companions")
def onboarding_companions():
    return {"companions": companion_options()}


@app.post("/feedback")
def post_feedback(req: FeedbackRequest, authorization: Optional[str] = Header(default=None)):
    """러닝 후 만족도를 받아 그 사람의 가중치를 학습한다.

    무엇 때문에 추천했는지(항목별 적합도)는 추천할 때 남겨 두었으므로, 앱은 코스 id와
    점수만 보내면 된다.
    """
    _owner_only(req.user_id, authorization)
    profile = load_profile(req.user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="추천을 받은 적 없는 사용자입니다. 먼저 user_id를 넣어 추천을 받아야 합니다.")
    remembered = recalled(profile, req.course_id)
    if remembered is None:
        raise HTTPException(status_code=404, detail="이 사용자에게 최근 추천된 코스가 아닙니다.")
    scores, baseline = remembered
    learn(profile, scores, req.rating, baseline)
    # 별점은 "무엇이" 좋았는지를 다른 코스와의 차이로 추정한다. 항목별 답은 그 항목에 대한 직접 증거다
    learn_aspects(profile, req.aspects)
    save_profile(profile)
    # 학습에 쓰고 버리지 않고 기록으로도 남긴다 — 다른 사람에게 평균 별점과 후기를 보여줄 수 있게
    user_store.save_review(req.user_id, req.course_id, req.rating, req.comment, req.aspects)
    _log_feedback(req, profile["weights"])
    return _profile_view(profile)


def _log_feedback(req, weights: Optional[dict]) -> None:
    """평가 한 번을 이력으로 남긴다 — 그 사람이 나중에 자기 취향이 어떻게 만들어졌는지 볼 수 있게."""
    course = _known_course(req.course_id) or next(
        (item["course"] for item in user_store.favorites(req.user_id) if item["course"]["id"] == req.course_id), None)
    user_store.log_feedback(
        req.user_id, req.course_id, req.rating, req.aspects, req.comment,
        course_name=course.get("name") if course else None,
        labels=scenery_choices.labels_for(course.get("tags")) if course else [],
        weights={k: round(v, 4) for k, v in weights.items()} if weights else None)


def _known_course(course_id: str):
    return next((c for c in load_courses() if c["id"] == course_id), None)


@app.post("/reviews")
def post_review(req: FeedbackRequest, authorization: Optional[str] = Header(default=None)):
    """추천받지 않은 코스(즐겨찾기에서 다시 뛴 코스 등)에도 별점·후기를 남긴다. 가중치 학습은 하지 않는다."""
    _owner_only(req.user_id, authorization)
    if _known_course(req.course_id) is None and req.course_id not in user_store.favorite_ids(req.user_id):
        raise HTTPException(status_code=404, detail="없는 코스입니다.")
    user_store.save_review(req.user_id, req.course_id, req.rating, req.comment, req.aspects)
    _log_feedback(req, None)
    return get_reviews(req.course_id)


@app.get("/courses/{course_id}/reviews")
def get_reviews(course_id: str, limit: int = 20):
    """코스의 평균 별점과 최근 후기. 누가 썼는지는 아이디(가입한 사람)나 '러너'로만 보여준다."""
    body = user_store.reviews_for(course_id, limit)
    names = user_store.usernames([review["user_id"] for review in body["reviews"]])
    for review in body["reviews"]:
        # 내부 user_id는 내보내지 않는다 — 다른 사람의 데이터를 가리키는 열쇠라서
        review["author"] = names.get(review.pop("user_id"), "러너")
    return body


@app.post("/favorites")
def post_favorite(req: FavoriteRequest, authorization: Optional[str] = Header(default=None)):
    """코스를 즐겨찾기에 저장한다. 코스 내용을 통째로 보관하므로 나중에 경로가 바뀌어도 저장한 그대로 남는다."""
    _owner_only(req.user_id, authorization)
    course = req.course or _known_course(req.course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="없는 코스입니다. 실시간 생성 코스는 course에 내용을 같이 보내주세요.")
    if course.get("id") != req.course_id or not course.get("path"):
        raise HTTPException(status_code=422, detail="course는 추천 응답으로 받은 코스 그대로여야 합니다(id, path 포함).")
    user_store.add_favorite(req.user_id, course)
    return {"saved": True, "count": len(user_store.favorite_ids(req.user_id))}


@app.get("/favorites/{user_id}")
def get_favorites(user_id: str, authorization: Optional[str] = Header(default=None)):
    _owner_only(user_id, authorization)
    saved = user_store.favorites(user_id)
    marks = user_store.ratings([item["course"]["id"] for item in saved])
    for item in saved:
        item["rating"] = marks.get(item["course"]["id"])
    return {"favorites": saved}


@app.delete("/favorites/{user_id}/{course_id}")
def delete_favorite(user_id: str, course_id: str, authorization: Optional[str] = Header(default=None)):
    _owner_only(user_id, authorization)
    if not user_store.remove_favorite(user_id, course_id):
        raise HTTPException(status_code=404, detail="즐겨찾기에 없는 코스입니다.")
    return {"saved": False}


@app.get("/profile/{user_id}")
def get_profile(user_id: str, authorization: Optional[str] = Header(default=None)):
    """학습된 취향. 어떤 요소를 기본값보다 더/덜 중시하게 됐는지 설명과 함께."""
    _owner_only(user_id, authorization)
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
        # 사용자에게 그대로 보여줄 수 있는 취향 요약 문장 (답한 내용에 근거가 있는 것만)
        "summary": taste_summary(profile),
        # 항목별 답으로 옮겨진 목표: 1보다 작으면 더 완만하게/짧게, 크면 더 가파르게/길게
        "tuning": {"elevation_scale": profile.get("elevation_scale", 1.0),
                   "distance_scale": profile.get("distance_scale", 1.0)},
        "aspect_log": profile.get("aspect_log", {}),
    }


@app.get("/users/{user_id}/taste")
def get_my_taste(user_id: str, limit: int = 50, authorization: Optional[str] = Header(default=None)):
    """내 취향 데이터 전부: 요약, 항목별 중요도, 맞춰진 기준, 지금까지 한 답, 평가 이력, 취향이 변해 온 과정.

    앱의 '내 취향' 화면이 이것 하나로 그려진다. 평가한 적이 없어도(프로필만 있어도) 빈 이력으로 응답한다.
    """
    _owner_only(user_id, authorization)
    profile = load_profile(user_id)
    history = user_store.feedback_history(user_id, limit)
    if profile is None and not history:
        raise HTTPException(status_code=404, detail="아직 저장된 취향 데이터가 없습니다.")

    ratings = [item["rating"] for item in history]
    liked = {}
    for item in history:
        if item["rating"] >= 4:
            for label in item["scenery_labels"]:
                liked[label] = liked.get(label, 0) + 1
    view = _profile_view(profile) if profile else {"user_id": user_id, "n_feedback": 0, "survey": {}, "weights": {},
                                                    "explanation": [], "summary": [], "tuning": {}, "aspect_log": {}}
    view.pop("aspect_log", None)
    return {
        **view,
        "stats": {
            "feedback_count": len(ratings),
            "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
            # 4점 이상 준 코스들이 지나던 풍경 (많은 순)
            "liked_scenery": [{"label": label, "count": count}
                              for label, count in sorted(liked.items(), key=lambda kv: -kv[1])],
            "favorite_count": len(user_store.favorite_ids(user_id)),
        },
        "answers": answers_view(profile) if profile else [],
        "history": [{
            "created_at": item["created_at"], "course_id": item["course_id"], "course_name": item["course_name"],
            "scenery_labels": item["scenery_labels"], "rating": item["rating"],
            "answers": answer_labels(item["aspects"]), "comment": item["comment"],
        } for item in history],
        # 평가할 때마다의 가중치 (오래된 것부터) — 취향이 어떻게 변해 왔는지 그래프로 그릴 수 있다
        "weight_history": [{"created_at": item["created_at"], "weights": item["weights"]}
                           for item in reversed(history) if item["weights"]],
    }


@app.delete("/users/{user_id}")
def delete_my_data(user_id: str, authorization: Optional[str] = Header(default=None)):
    """내 데이터 전부 삭제: 학습된 취향, 평가 이력, 별점·후기, 즐겨찾기. 되돌릴 수 없다."""
    _owner_only(user_id, authorization)
    forget_legacy(user_id)   # 예전 파일에 남은 것까지 지운다 (안 그러면 다음 조회 때 되살아난다)
    removed = user_store.delete_user(user_id)
    if not any(removed.values()):
        raise HTTPException(status_code=404, detail="저장된 데이터가 없습니다.")
    return {"deleted": removed}


@app.get("/health")
def health():
    """키 '값'은 절대 내보내지 않고, 설정 여부만 알려준다 (팀원 환경 자가진단용)."""
    from ..recommend import sidewalk

    return {
        "status": "ok",
        "keys": {name: bool(os.environ.get(name)) for name in API_KEYS},
        # 로컬 지형 DB: 없으면 직접 경로 탐색 대신 Tmap 최단 경로로 동작한다.
        # matches_team이 true면 팀 기준 파일·코드로 만든 것 (python -m src.setup_local 로 맞춘다)
        "local_map": local_osm.info(),
        "sidewalk_facts": len(sidewalk.load_facts()),
    }


@app.get("/map/graph")
def map_graph(bbox: Optional[str] = None, crossings: bool = True):
    """운영자 검증 토글용 GraphML 도로망 GeoJSON.

    일반 추천 응답에는 이 데이터를 넣지 않는다. 필요할 때만 지도 화면 범위를 전달해
    GraphML 전체를 모바일 브라우저에 보내지 않도록 한다.
    """
    try:
        return public_graph(GRAPH_GEOJSON_PATH, parse_bbox(bbox), include_crossings=crossings)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="그래프 시각화 데이터가 설정되지 않았습니다")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


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
    chosen = list(user.get("environment_tags") or [])
    if req.text:
        chosen += sorted(extract_tags(req.text))
    # 사용자가 고른 선택지('바다')를 측정 태그(바다뷰·해변)로 푼다. 이후 단계는 측정 태그로 동작한다
    user["environment_tags"], user["scenery_groups"] = scenery_choices.resolve(chosen)
    return user


SURVEY_FIELDS = ("experience_level", "pace_min_per_km", "purpose", "companion",
                 "elevation_preference", "environment_tags", "preferred_distance_km")


@app.post("/recommend", response_model=RecommendResponse)
def post_recommend(req: RecommendRequest, background_tasks: BackgroundTasks,
                   authorization: Optional[str] = Header(default=None)):
    """user_id를 주면 그 사람의 학습된 가중치로 추천하고, 평가를 받을 수 있게 추천 근거를 남긴다."""
    _owner_only(req.user_id, authorization)
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
    if profile:
        # 러닝 후 "힘들었다/길었다" 같은 답으로 맞춰 둔 이 사람의 기준
        user["elevation_scale"] = profile.get("elevation_scale", 1.0)
        user["distance_scale"] = profile.get("distance_scale", 1.0)

    response = _recommend(req, background_tasks, user, weights)

    # 항목별 적합도를 같이 내려준다: 앱은 "왜 이 코스인지" 보여줄 수 있고,
    # 서버는 나중에 만족도 평가가 오면 이걸로 이 사람의 가중치를 학습한다
    breakdowns = []
    marks = user_store.ratings([result["course"]["id"] for result in response["results"]])
    saved = user_store.favorite_ids(req.user_id) if req.user_id else None
    for result in response["results"]:
        # 코스 카드에 보여줄 쉬운 이름 (측정 태그 16종 대신 선택지 이름)
        result["course"] = dict(result["course"],
                                scenery_labels=scenery_choices.labels_for(result["course"].get("tags")))
        result["rating"] = marks.get(result["course"]["id"])
        if saved is not None:
            result["favorite"] = result["course"]["id"] in saved
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
                night=(user.get("time_of_day") or current_period()) == "night",
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

    if req.loop_engine == "graphml":
        if not wants_loop:
            raise HTTPException(status_code=400, detail="GraphML 엔진은 순환(loop) 코스에만 사용할 수 있습니다.")
        engine = getattr(app.state, "graph_loop_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="GraphML Loop 엔진이 비활성화되어 있습니다. GRAPH_LOOP_ENABLED=1을 설정하세요.")
        try:
            course = graph_loop_course(engine, req.current_lat, req.current_lng, target_km or 3.0)
        except RouteFailure as error:
            raise HTTPException(status_code=422, detail={"code": error.code, "message": error.message}) from error
        return {"results": [{"course": course, "score": 1.0}], "source": "osm_graph"}

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

        # 근처에 저장된 코스가 있어도 요청한 조건(풍경 등)에 맞는 게 없으면, 빈 결과를 주지 않고
        # 그 자리에서 조건에 맞는 코스를 만든다.
        if ranked or not has_location:
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
        # 밤에는 상가·큰길처럼 사람이 있는 길 쪽으로 경로를 짠다 (시간대는 요청값, 없으면 서버 시각)
        night = (user.get("time_of_day") or current_period()) == "night"
        generated_candidates = generate_loop_candidates(
            req.current_lat, req.current_lng, generate_km, tags, route_type=req.route_type, night=night
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

    for course in generated_candidates:
        # 요청한 풍경이 아니어도 만든 코스는 저장해 둔다 — 다음에 조건이 맞는 요청에서 다시 쓴다
        background_tasks.add_task(register_in_background, course)
    if req.route_type != "loop":
        # 생성기는 편도 경로를 준다. 왕복이면 실제 복귀 경로를 붙여 왕복 거리로 점수를 매긴다
        generated_candidates = [
            apply_route_type(attach_actual_return_path(c) if req.route_type == "roundtrip" else c, req.route_type)
            for c in generated_candidates
        ]
    generated_env_context_map = build_env_context_map(generated_candidates, req.use_live_environment)
    ranked = recommend(
        generated_candidates,
        user,
        top_n=req.top_n,
        env_context_map=generated_env_context_map,
        weights=weights,
    )
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
