"""목적지를 직접 고르는 코스.

"오동도까지 뛰고 싶다"는 거리나 태그로 표현할 수 없는 요구다. 추천이 아니라 지정이므로
점수로 고르지 않고, 준 지점까지 실제 보행 경로를 그대로 만들어 준다.

장소 이름(Nominatim 검색) 또는 좌표를 받는다. 왕복이면 돌아오는 길도 실제 경로로 붙인다
(일방통행·횡단보도 때문에 갈 때와 올 때 안내가 다르다).
"""
from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route
from ..data_collection import scenery
from ..data_collection.build_courses_from_landmarks import geocode, sample_elevation_gain
from .route_type import attach_actual_return_path, make_roundtrip
from .step_position import assign_positions


def course_to_destination(lat: float, lng: float, place: str = None,
                          dest_lat: float = None, dest_lng: float = None,
                          route_type: str = "oneway") -> dict:
    """현위치에서 목적지까지의 코스. 장소 이름이나 좌표 중 하나는 있어야 한다."""
    if dest_lat is None or dest_lng is None:
        if not place:
            raise ValueError("목적지를 장소 이름이나 좌표로 지정해주세요.")
        dest_lat, dest_lng = geocode(place)

    route = get_route((lng, lat), (dest_lng, dest_lat), start_name="현위치", end_name=place or "목적지")
    path, distance_m = extract_path(route)
    if not path:
        raise ValueError("목적지까지 걸어갈 수 있는 경로를 찾지 못했습니다.")

    label = place or f"{dest_lat:.4f},{dest_lng:.4f}"
    course = {
        "id": f"destination-{lat:.4f}-{lng:.4f}-{dest_lat:.4f}-{dest_lng:.4f}-{route_type}",
        "name": f"{label}까지",
        "region": "실시간 생성",
        "distance_km": round(distance_m / 1000, 2),
        "elevation_gain_m": sample_elevation_gain(path),
        "surface": "paved",
        "safety_score": 0.7,
        "path": path,
        "steps": assign_positions(extract_steps(route), path),
        "route_type": "oneway",
        "tags": [],
        "source": "live_generated",
    }

    measured = scenery.tags_for_path(path, course)  # 지형 데이터가 있는 지역이면 실제 풍경을 잰다
    if measured is None:
        course["scenery_pending"] = True
    else:
        course["scenery"], course["tags"] = measured

    if route_type == "roundtrip":
        course = make_roundtrip(attach_actual_return_path(course))
        course["name"] = f"{label} 왕복"
    return course
