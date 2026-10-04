"""목적지를 직접 고르는 코스.

"오동도까지 뛰고 싶다"는 거리나 태그로 표현할 수 없는 요구다. 추천이 아니라 지정이므로
점수로 고르지 않고, 준 지점까지 실제 보행 경로를 그대로 만들어 준다.

장소 이름 또는 좌표를 받는다. 이름은 Tmap 장소 검색으로 찾고 현위치에서 가까운 곳을 우선한다
(api_clients/tmap_poi.py). 어느 곳으로 정했는지와 다른 후보를 같이 돌려줘서, 엉뚱한 곳이 잡혔을 때
사용자가 바로 고칠 수 있게 한다. 왕복이면 돌아오는 길도 실제 경로로 붙인다
(일방통행·횡단보도 때문에 갈 때와 올 때 안내가 다르다).
"""
from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route
from ..data_collection import area_cache, local_osm, scenery
from ..data_collection.enrich import haversine_m
from ..api_clients.tmap_poi import search_places
from ..data_collection.build_courses_from_landmarks import sample_elevation_gain
from .route_type import attach_actual_return_path, make_roundtrip
from .safety import safety_index, score_components

LOCAL_ROUTE_MAX_M = 8000   # 이보다 먼 목적지는 도로망을 그만큼 넓게 읽어야 해서 Tmap에 맡긴다


def _own_route(lat, lng, dest_lat, dest_lng, night: bool):
    """받아 둔 도로망에서 직접 짠 경로 (인도 없는 차도를 피하고, 밤에는 사람 있는 길로). 못 짜면 (None, None)."""
    from . import road_graph, sidewalk

    straight_m = haversine_m((lat, lng), (dest_lat, dest_lng))
    if not local_osm.available() or straight_m > LOCAL_ROUTE_MAX_M:
        return None, None
    area = area_cache.load_area(lat, lng, radius_m=straight_m + 500)
    if area is None or area.graph is None or not area.covers(dest_lat, dest_lng):
        return None, None
    start, end = (lat, lng), (dest_lat, dest_lng)
    plan = lambda: road_graph.route(area.graph, start, end, night=night)
    route = plan()
    if route is not None:
        route = sidewalk.confirm(area.graph, start, end, [], route, plan)
    if route is None or route.get("no_car_lanes") is False:
        return None, area   # 차도를 피해 갈 길이 없다 → Tmap 경로를 쓰되 확인되지 않았다고 표시한다
    return route, area
from .step_position import assign_positions


def course_to_destination(lat: float, lng: float, place: str = None,
                          dest_lat: float = None, dest_lng: float = None,
                          route_type: str = "oneway", night: bool = False) -> dict:
    """현위치에서 목적지까지의 코스. 장소 이름이나 좌표 중 하나는 있어야 한다."""
    found, others = None, []
    if dest_lat is None or dest_lng is None:
        if not place:
            raise ValueError("목적지를 장소 이름이나 좌표로 지정해주세요.")
        candidates = search_places(place, lat, lng)
        if not candidates:
            raise ValueError(f"'{place}'을(를) 찾지 못했습니다. 다른 이름으로 검색해 보세요.")
        found, others = candidates[0], candidates[1:]
        dest_lat, dest_lng = found["lat"], found["lng"]
        place = found["name"]   # 사용자가 친 말이 아니라 실제 장소 이름으로 보여준다

    # 추천 코스와 같은 규칙으로 간다: 인도 없는 차도는 지나지 않고, 밤에는 사람 있는 길로.
    # 도로망이 없거나 그렇게 갈 길이 없으면 Tmap 보행자 경로(최단 거리)를 쓴다.
    route, area = _own_route(lat, lng, dest_lat, dest_lng, night)
    own = route is not None
    if not own:
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
    if own:
        course.update(router=route["router"], road_mix=route["road_mix"], road_score=route["road_score"],
                      no_car_lanes=route["no_car_lanes"], sidewalk_only=route["sidewalk_only"],
                      lively_ratio=route["lively_ratio"], secluded_ratio=route["secluded_ratio"],
                      planned_for_night=night)
    else:
        course["no_car_lanes"] = None   # Tmap 최단 경로라 인도 여부를 확인하지 못했다
    if area is not None:
        points = area.safety_points
        course["safety_breakdown"] = score_components(path, points["cctv"], points["convenience"], points["police"])
        course["safety_score"] = safety_index(path, points["cctv"], points["convenience"], points["police"])
    if found is not None:
        # 어디로 정했는지, 그리고 그곳이 아닐 때 고를 수 있는 다른 후보
        course["destination"] = found
        course["destination_alternatives"] = others

    measured = scenery.tags_for_path(path, course)  # 지형 데이터가 있는 지역이면 실제 풍경을 잰다
    if measured is None:
        course["scenery_pending"] = True
    else:
        course["scenery"], course["tags"] = measured

    if route_type == "roundtrip":
        course = make_roundtrip(attach_actual_return_path(course))
        course["name"] = f"{label} 왕복"
    return course
