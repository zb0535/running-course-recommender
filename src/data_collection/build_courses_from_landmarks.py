"""랜드마크 이름 두 개(시작~끝)만 주면 실제 코스를 통째로 만들어 등록한다.

파이프라인: OSM Nominatim(무료, 키 불필요)으로 랜드마크 좌표 조회
-> Tmap 보행자 경로 API로 실제 인도/보행로 경로 획득 -> Open Topo Data로 고도 샘플링
-> enrich_course()로 OSM 기반 태그 자동 계산 -> data/courses.json에 등록.

좌표를 사람이 직접 찍을 필요 없이, 팀원이 아는 코스 이름(시작 랜드마크, 끝 랜드마크)만 알려주면
이 스크립트가 나머지를 전부 자동으로 처리한다.

사용:
    python -m src.data_collection.build_courses_from_landmarks \
        --id yeosu-coastal-05 --name "코스이름" --region 여수 \
        --start "돌산대교" --end "여수엑스포"
"""
import argparse
import time

import requests

from .add_course import add_course
from ..api_clients.elevation import get_elevation_profile
from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "running-course-recommender/0.1 (school project)"}
DB_PATH = "data/courses.json"


def geocode(place: str) -> tuple:
    resp = requests.get(
        NOMINATIM_URL, headers=NOMINATIM_HEADERS,
        params={"q": place, "format": "json", "limit": 1}, timeout=15,
    )
    resp.raise_for_status()
    docs = resp.json()
    if not docs:
        raise ValueError(f"'{place}' 좌표를 찾지 못했습니다. 검색어를 더 구체적으로 입력해보세요 (예: '오동도, 여수').")
    return float(docs[0]["lat"]), float(docs[0]["lon"])


def sample_elevation_gain(path: list, n: int = 20) -> float:
    if len(path) < 2:
        return 0.0
    step = max(1, len(path) // n)
    samples = path[::step]
    elevations = get_elevation_profile(samples)
    gain = sum(max(0.0, elevations[i] - elevations[i - 1]) for i in range(1, len(elevations)))
    return round(gain, 1)


def build_course(course_id: str, name: str, region: str, start: str, end: str, safety_score: float = 0.75) -> dict:
    start_lat, start_lng = geocode(start)
    time.sleep(1.1)  # Nominatim 요청 제한(초당 1회) 준수
    end_lat, end_lng = geocode(end)

    route = get_route((start_lng, start_lat), (end_lng, end_lat), start, end)
    path, distance_m = extract_path(route)
    if not path:
        raise ValueError("Tmap 보행자 경로를 찾지 못했습니다.")

    raw_course = {
        "id": course_id,
        "name": name,
        "region": region,
        "distance_km": round(distance_m / 1000, 2),
        "elevation_gain_m": sample_elevation_gain(path),
        "surface": "paved",
        "safety_score": safety_score,
        "path": path,
        "steps": extract_steps(route),
        "route_type": "oneway",
    }
    return add_course(raw_course, DB_PATH)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--start", required=True, help="시작 랜드마크 이름 (예: '오동도, 여수')")
    parser.add_argument("--end", required=True, help="끝 랜드마크 이름")
    args = parser.parse_args()

    enriched = build_course(args.id, args.name, args.region, args.start, args.end)
    print(f"등재 완료: {enriched['id']} -> {enriched['distance_km']}km, tags={enriched['tags']}")


if __name__ == "__main__":
    main()
