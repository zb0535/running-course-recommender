"""사용자 현위치 기반 실시간 코스 생성 (프로토타입).

DB에 미리 저장된 코스가 아니라, 요청이 들어온 그 순간 사용자 좌표 주변에서
실제 도로/지형 데이터를 조회해 코스를 즉석에서 만든다.

방식: 목표 둘레로 가상의 다각형 꼭짓점을 만들고, 각 꼭짓점을 OSM의 공원·보행로로
스냅한 뒤 Tmap passList에 넣는다. route_type="loop"이면 출발점과 도착점을
같게 하여 경유지를 순서대로 통과하는 순환 코스를 만든다.

왕복(roundtrip)은 여기서 만들지 않는다 — 갔던 길을 되짚는 것이라 편도 코스에
실제 복귀 경로를 붙이는 route_type.py 쪽이 담당한다.

한계 (검증 완료 사항):
- 요청 1건당 Overpass + Tmap + 고도 API를 순차 호출하므로 응답에 수 초 소요.
"""
import itertools
import math
import logging

import requests
from concurrent.futures import ThreadPoolExecutor

from ..api_clients.elevation import get_elevation_profile
from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route
from ..data_collection import scenery
from ..data_collection.enrich import haversine_m
from ..data_collection.osm_overpass import OverpassUnavailable, post_overpass_query
from .step_position import assign_positions, cumulative_distances

TAG_TO_OSM_FILTER = {
    "바다뷰": '["natural"="coastline"]',
    "숲길": '["natural"="wood"]',
}
SNAP_RADIUS_M = 200
VERTEX_COUNT = 3
RETRY_ANGLE_OFFSETS_DEG = (0.0, 20.0, -20.0)
# 순환 길이 ≈ 반경 × LOOP_LENGTH_PER_RADIUS. 경로는 원이 아니라 '출발점 → 꼭짓점 3개 → 출발점'이고
# 여기에 실제 도로의 우회가 더해진다. 막다른 길 왕복을 걷어낸 뒤의 실측값으로 맞췄다(광주 5.7).
# 예전 공식(원 둘레 = 목표)은 코스를 목표의 70% 안팎으로 만들었는데, 막다른 길 왕복이 거리를
# 부풀려서 우연히 맞아 보였을 뿐이다.
LOOP_LENGTH_PER_RADIUS = 5.7
LOOP_RADIUS_SCALES = (1.0, 0.9, 0.8)
logger = logging.getLogger(__name__)


def virtual_vertices(lat: float, lng: float, target_distance_km: float,
                     count: int = VERTEX_COUNT, angle_offset_deg: float = 0.0,
                     radius_scale: float = 1.0) -> list[tuple]:
    """목표 거리에서 도로 우회분을 고려한 다각형 꼭짓점을 계산한다."""
    radius_m = target_distance_km * radius_scale * 1000 / LOOP_LENGTH_PER_RADIUS
    lat_radius = radius_m / 111_320
    lng_radius = radius_m / (111_320 * math.cos(math.radians(lat)))
    return [
        (
            lat + lat_radius * math.sin(math.radians(angle_offset_deg + i * 360 / count)),
            lng + lng_radius * math.cos(math.radians(angle_offset_deg + i * 360 / count)),
        )
        for i in range(count)
    ]


RUNNABLE_HIGHWAY = '["highway"~"footway|path|pedestrian|living_street|residential|service|cycleway|track|unclassified|tertiary"]'


def _around_clause(lat: float, lng: float) -> str:
    return f"way(around:{SNAP_RADIUS_M},{lat},{lng}){RUNNABLE_HIGHWAY};"


def _nearest_point_on_ways(origin: tuple, elements: list) -> tuple | None:
    best_point = None
    best_distance_m = float("inf")
    for element in elements:
        geometry = element.get("geometry", [])
        for start, end in zip(geometry, geometry[1:]):
            point, distance_m = _project_onto_segment(
                origin, (start["lat"], start["lon"]), (end["lat"], end["lon"])
            )
            if distance_m < best_distance_m:
                best_point = point
                best_distance_m = distance_m
    return best_point if best_distance_m <= SNAP_RADIUS_M else None


RING_DIRECTIONS = 8  # 꼭짓점 후보 방향 수. 이 중 길이 있는 방향만 골라 쓴다


def fetch_runnable_ways(points: list) -> list:
    """여러 지점 주변의 보행 가능한 길을 한 번의 Overpass 질의로 받는다. 실패하면 빈 목록."""
    if not points:
        return []
    query = "[out:json][timeout:25];(" + "".join(_around_clause(lat, lng) for lat, lng in points) + ");out geom;"
    try:
        return post_overpass_query(query, timeout=30).json().get("elements", [])
    except OverpassUnavailable as error:
        logger.debug("주변 길을 받지 못해 계산 좌표를 그대로 씁니다: %s", error)
        return []


def ring_points(lat: float, lng: float, target_distance_km: float,
                angle_offset_deg: float = 0.0, radius_scale: float = 1.0) -> list:
    return virtual_vertices(lat, lng, target_distance_km, count=RING_DIRECTIONS,
                            angle_offset_deg=angle_offset_deg, radius_scale=radius_scale)


def pick_land_vertices(lat: float, lng: float, target_distance_km: float, ways: list,
                       angle_offset_deg: float = 0.0, radius_scale: float = 1.0) -> list:
    """길이 실제로 있는 방향에서만 꼭짓점을 고른다.

    출발점 둘레로 원을 그려 꼭짓점을 찍으면 해안가에선 절반이 바다에 떨어진다. 그러면 Tmap이
    해안선까지만 가서 코스가 목표의 1/3로 쪼그라든다(여수·해운대에서 5km 요청에 1.8~2.1km).
    8방향 중 근처에 길이 있는 방향만 남기고, 그중 서로 가장 고르게 떨어진 조합을 고른다.
    반환은 방향 순서대로라 경유지를 차례로 돌면 고리가 된다. 길이 있는 방향이 2개 미만이면 [].
    """
    step = 360 / RING_DIRECTIONS
    on_land = []
    for k, point in enumerate(ring_points(lat, lng, target_distance_km, angle_offset_deg, radius_scale)):
        snapped = _nearest_point_on_ways(point, ways)
        if snapped:
            on_land.append(((angle_offset_deg + k * step) % 360, snapped))
    if len(on_land) < 2:
        return []

    count = min(VERTEX_COUNT, len(on_land))

    def min_gap(combo):
        angles = sorted(a for a, _ in combo)
        return min((angles[(i + 1) % len(angles)] - angles[i]) % 360 or 360 for i in range(len(angles)))

    best = max(itertools.combinations(on_land, count), key=min_gap)
    return [point for _, point in sorted(best, key=lambda pair: pair[0])]


def snap_to_runnable_point(lat: float, lng: float) -> tuple | None:
    """가상 꼭짓점을 반경 내 보행 가능 도로의 중심선에 스냅한다."""
    query = f"""
    [out:json][timeout:15];
    {_around_clause(lat, lng)}
    out geom;
    """
    resp = post_overpass_query(query, timeout=20)
    return _nearest_point_on_ways((lat, lng), resp.json().get("elements", []))


def _project_onto_segment(origin: tuple, start: tuple, end: tuple) -> tuple[tuple, float]:
    """Return closest (lat, lng) on a segment and distance in meters."""
    lat_scale = 111_320
    lng_scale = 111_320 * math.cos(math.radians(origin[0]))

    def to_xy(point: tuple) -> tuple[float, float]:
        return ((point[1] - origin[1]) * lng_scale, (point[0] - origin[0]) * lat_scale)

    start_xy = to_xy(start)
    end_xy = to_xy(end)
    dx = end_xy[0] - start_xy[0]
    dy = end_xy[1] - start_xy[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        fraction = 0.0
    else:
        fraction = max(0.0, min(1.0, -(start_xy[0] * dx + start_xy[1] * dy) / length_sq))

    projected_xy = (start_xy[0] + fraction * dx, start_xy[1] + fraction * dy)
    projected = (
        origin[0] + projected_xy[1] / lat_scale,
        origin[1] + projected_xy[0] / lng_scale,
    )
    return projected, math.hypot(*projected_xy)


def snap_vertices(vertices: list[tuple]) -> list[tuple]:
    """꼭짓점을 보행로에 붙인다. Overpass 질의 하나가 수 초씩 걸려 꼭짓점마다 순서대로
    물으면 이 단계만으로 코스 생성이 십수 초가 된다. 서로 독립이므로 같이 묻는다.
    Overpass가 응답하지 않으면 계산 좌표를 그대로 쓴다."""
    if not vertices:
        return []

    # Overpass 요청은 전역 락으로 직렬화되므로 꼭짓점마다 따로 물으면 그만큼 기다려야 한다.
    # 꼭짓점 세 곳의 주변 도로를 한 번에 받아 오고, 스냅 계산은 로컬에서 한다.
    query = "[out:json][timeout:25];(" + "".join(_around_clause(lat, lng) for lat, lng in vertices) + ");out geom;"
    try:
        elements = post_overpass_query(query, timeout=30).json().get("elements", [])
    except OverpassUnavailable as error:
        logger.debug("OSM 스냅을 건너뛰고 계산 좌표를 사용합니다: %s", error)
        return list(vertices)

    return [_nearest_point_on_ways(vertex, elements) or vertex for vertex in vertices]


def _orientation(a: tuple, b: tuple, c: tuple) -> float:
    return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])


def _segments_intersect(a: tuple, b: tuple, c: tuple, d: tuple) -> bool:
    ab_c = _orientation(a, b, c)
    ab_d = _orientation(a, b, d)
    cd_a = _orientation(c, d, a)
    cd_b = _orientation(c, d, b)
    return (ab_c * ab_d < 0 and cd_a * cd_b < 0)


SPUR_NEAR_M = 15          # 이 안이면 같은 길 위로 본다
SPUR_MIN_LENGTH_M = 40    # 이보다 짧은 되짚기는 교차로에서 방향만 트는 것
SPUR_OVERLAP = 0.7        # 돌아오는 길의 이만큼이 가는 길과 겹치면 '가지'


def _is_out_and_back(segment: list) -> bool:
    """segment가 한 지점에서 나갔다가 같은 길로 되돌아오는 모양인가.

    가장 멀리 간 지점을 반환점으로 보고, 돌아오는 절반이 가는 절반 위에 얼마나 겹치는지 본다.
    블록을 한 바퀴 도는 고리는 다른 길로 돌아오므로 겹치지 않는다.
    """
    if len(segment) < 3:
        return False
    far = max(range(len(segment)), key=lambda k: haversine_m(segment[0], segment[k]))
    outbound, inbound = segment[: far + 1], segment[far:]
    if len(inbound) < 2:
        return False
    overlapping = sum(1 for q in inbound if any(haversine_m(q, o) <= SPUR_NEAR_M for o in outbound))
    return overlapping / len(inbound) >= SPUR_OVERLAP


def prune_spurs(path: list) -> list:
    """순환 경로에서 막다른 길로 들어갔다가 되나오는 가지를 잘라낸다.

    경유지가 막다른 길에 찍히면 Tmap은 끝까지 갔다가 같은 길로 돌아 나온다. 달리는 사람에게
    의미 없는 구간이라(실측 4.84km 중 0.74km) 가지의 입구와 출구를 이어 붙여 없앤다.
    """
    return _prune_with_spans(path)[0]


def _prune_with_spans(path: list) -> tuple:
    """(잘라낸 경로, 잘린 구간들[(시작 누적m, 끝 누적m)])."""
    if len(path) < 4:
        return path, []
    cum = cumulative_distances(path)
    kept = [path[0]]
    spans = []
    i = 0
    last = len(path) - 1
    while i < last:
        jump = None
        # 출발점으로 돌아오는 마지막 지점은 순환의 끝이지 가지가 아니므로 제외한다
        for j in range(last - 1, i + 1, -1):
            if cum[j] - cum[i] < SPUR_MIN_LENGTH_M:
                break
            if haversine_m(path[i], path[j]) <= SPUR_NEAR_M and _is_out_and_back(path[i : j + 1]):
                jump = j
                break
        if jump is not None:
            spans.append((cum[i], cum[jump]))
            i = jump
        else:
            i += 1
        kept.append(path[i])
    return kept, spans


def retraced_ratio(path: list) -> float:
    """경로 중 이미 지나온 길을 다시 지나는 비율. 깨끗한 순환이면 0에 가깝다."""
    if len(path) < 3:
        return 0.0
    cum = cumulative_distances(path)
    total = cum[-1] or 1.0
    retraced = 0.0
    for i in range(1, len(path)):
        if cum[i] > total - 150:  # 출발점으로 돌아오는 마지막 구간은 정상
            break
        if any(haversine_m(path[i], path[j]) < SPUR_NEAR_M for j in range(i) if cum[i] - cum[j] > 150):
            retraced += cum[i] - cum[i - 1]
    return retraced / total


MAX_RETRACED = 0.03        # 순환 코스에서 왔던 길을 다시 지나는 비율 허용치
DISTANCE_TOLERANCE = 0.2   # 목표 거리와 이만큼 넘게 차이 나면 보정 생성을 한 번 더 한다


def _reroute_along(lat: float, lng: float, pruned: list, count: int = VERTEX_COUNT):
    """가지를 잘라낸 순환 경로를 따라 고르게 찍은 지점들을 경유지로 다시 요청한다.

    이 지점들은 이미 지나가는 길 위에 있으므로 막다른 길로 새지 않는다.
    """
    cum = cumulative_distances(pruned)
    total = cum[-1]
    if total <= 0:
        return None
    waypoints = []
    for k in range(1, count + 1):
        target = total * k / (count + 1)
        idx = next(i for i, c in enumerate(cum) if c >= target)
        waypoints.append(pruned[idx])
    return get_route(
        (lng, lat), (lng, lat),
        start_name="출발지", end_name="출발지",
        waypoints=[(p[1], p[0]) for p in waypoints],
    )


SPAN_MARGIN_M = 25  # 잘린 구간 경계의 갈림길 안내까지 함께 뺀다


def _try_reroute(lat: float, lng: float, pruned: list):
    """잘라낸 경로를 따라 다시 요청. 결과가 깨끗하고 길이가 크게 안 변했을 때만 (route, path, m)."""
    try:
        route = _reroute_along(lat, lng, pruned)
    except requests.HTTPError:
        return None
    if route is None:
        return None
    path, distance_m = extract_path(route)
    if not path or retraced_ratio(path) > MAX_RETRACED:
        return None
    expected = cumulative_distances(pruned)[-1]
    if expected and abs(cumulative_distances(path)[-1] - expected) / expected > 0.15:
        return None  # 다른 길로 새서 코스 자체가 바뀌었다
    return route, path, distance_m


def _drop_steps_in_spans(steps: list, path: list, spans: list) -> list:
    if not spans:
        return steps
    placed = assign_positions(steps, path)
    kept = []
    for original, positioned in zip(steps, placed):
        at = positioned["cum_m"]
        if any(a - SPAN_MARGIN_M <= at <= b + SPAN_MARGIN_M for a, b in spans):
            continue
        kept.append(original)
    return kept


def has_self_intersection(path: list[list[float]]) -> bool:
    """인접 선분과 닫힌 고리의 시작·끝 선분은 제외하고 교차 여부를 검사한다."""
    if len(path) < 4:
        return False
    points = [tuple(point) for point in path]
    segment_count = len(points) - 1
    for first in range(segment_count):
        for second in range(first + 1, segment_count):
            if second - first <= 1:
                continue
            if first == 0 and second == segment_count - 1:
                continue
            if _segments_intersect(points[first], points[first + 1], points[second], points[second + 1]):
                return True
    return False


def find_nearby_points(lat: float, lng: float, radius_m: float, osm_filter: str, limit: int = 3) -> list:
    """기존 호출부 호환용으로 주변 지형의 대표 좌표를 반환한다."""
    query = f"""
    [out:json][timeout:25];
    (way(around:{radius_m},{lat},{lng}){osm_filter};);
    out geom 3;
    """
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=OSM_HEADERS, timeout=30)
    resp.raise_for_status()
    elements = resp.json().get("elements", [])
    points = []
    for element in elements:
        geometry = element.get("geometry", [])
        if not geometry:
            continue
        for point in (geometry[0], geometry[len(geometry) // 2], geometry[-1]):
            candidate = (point["lat"], point["lon"])
            if candidate not in points:
                points.append(candidate)

    if not points:
        return []

    # 출발점에서 먼 순서로 배치한다.
    points.sort(key=lambda point: (point[0] - lat) ** 2 + (point[1] - lng) ** 2, reverse=True)
    return points[:limit]


def find_nearby_point(lat: float, lng: float, radius_m: float, osm_filter: str) -> tuple:
    """기존 호출부 호환용으로 가장 먼 후보 하나를 반환한다."""
    points = find_nearby_points(lat, lng, radius_m, osm_filter, limit=1)
    return points[0] if points else None


def sample_elevation_gain(path: list, n: int = 20) -> float:
    if len(path) < 2:
        return 0.0
    step = max(1, len(path) // n)
    samples = path[::step]
    elevations = get_elevation_profile(samples)
    return round(sum(max(0.0, elevations[i] - elevations[i - 1]) for i in range(1, len(elevations))), 1)


def generate_loop_course(lat: float, lng: float, target_distance_km: float, tags: set,
                         route_type: str = "loop", angle_offset_deg: float | None = None,
                         snap_to_osm: bool = True, radius_scale: float = 1.0,
                         ways: list | None = None) -> dict:
    """Generate one Tmap course candidate for a waypoint polygon orientation.

    ways(주변 보행로)를 주면 길이 있는 방향에서만 꼭짓점을 고른다. 없으면 예전처럼 원 위에 찍는다.
    """
    matched_tag = next((tag for tag in TAG_TO_OSM_FILTER if tag in tags), None)
    offsets = (angle_offset_deg,) if angle_offset_deg is not None else RETRY_ANGLE_OFFSETS_DEG

    for offset in offsets:
        if route_type == "loop":
            if ways:
                waypoints = pick_land_vertices(lat, lng, target_distance_km, ways, offset, radius_scale)
                if not waypoints:
                    # 주변 길을 알고 있는데 길 있는 방향이 부족하다 = 이 반경·방향으론 순환이 안 나온다.
                    # 계산 좌표로 억지로 요청하면 바다 같은 곳이 경유지가 되어 Tmap이 400을 준다.
                    continue
            else:
                vertices = virtual_vertices(
                    lat, lng, target_distance_km,
                    angle_offset_deg=offset,
                    radius_scale=radius_scale,
                )
                waypoints = snap_vertices(vertices) if snap_to_osm and ways is None else vertices
            try:
                route = get_route(
                    (lng, lat), (lng, lat),
                    start_name="출발지", end_name="출발지",
                    waypoints=[(point[1], point[0]) for point in waypoints],
                )
            except requests.HTTPError as error:
                logger.debug("이 경유지 조합은 Tmap이 거절했습니다: %s", error)
                continue
        else:
            vertices = virtual_vertices(lat, lng, target_distance_km, count=1)
            waypoint = globals()["snap_vertices"](vertices)[0]
            route = get_route((lng, lat), (waypoint[1], waypoint[0]))

        full_path, distance_m = extract_path(route)
        if not full_path:
            continue
        raw_steps = extract_steps(route)
        if route_type == "loop" and retraced_ratio(full_path) > MAX_RETRACED:
            # 막다른 길로 들어갔다 나오는 가지가 있다. 먼저 잘라낸 경로 위 지점들을 경유지로 다시
            # 받아 안내까지 실제 데이터로 맞춰 본다(가지만 자르면 갈림길 안내가 틀린 채로 남는다).
            pruned, spans = _prune_with_spans(full_path)
            rerouted = _try_reroute(lat, lng, pruned)
            if rerouted is not None:
                route, full_path, distance_m = rerouted
                raw_steps = extract_steps(route)
            else:
                # 재요청이 오히려 다른 길로 새면, 잘라낸 경로를 쓰고 잘린 갈림길의 안내만 뺀다.
                # 틀린 안내를 남기거나 없는 안내를 지어내는 것보다 그 지점 안내를 생략하는 게 낫다.
                raw_steps = _drop_steps_in_spans(raw_steps, full_path, spans)
                full_path, distance_m = pruned, cumulative_distances(pruned)[-1]
        if route_type == "loop" and has_self_intersection(full_path):
            continue
        break
    else:
        if route_type == "loop":
            raise ValueError("Tmap 보행자 순환 경로를 찾지 못했습니다.")
        raise ValueError("Tmap 보행자 경로를 찾지 못했습니다.")

    steps = assign_positions(raw_steps, full_path)
    offset_suffix = f"-a{offset:g}-r{radius_scale:g}" if route_type == "loop" else ""
    name_suffix = f", {offset:g}°, 반경 {radius_scale:.0%}" if route_type == "loop" else ""
    name = f"현위치 기반 {'순환' if route_type == 'loop' else '편도'} 코스 (기본{name_suffix})"

    course = {
        "id": f"generated-{lat:.4f}-{lng:.4f}-{target_distance_km}-{route_type}{offset_suffix}",
        "name": name,
        "region": "실시간 생성",
        "distance_km": round(distance_m / 1000, 2),
        "elevation_gain_m": sample_elevation_gain(full_path),
        "surface": "paved",
        "safety_score": 0.7,
        "path": full_path,
        "steps": steps,
        "route_type": route_type,
        "tags": [],
        "source": "live_generated",
    }
    # 풍경 태그는 요청값이 아니라 측정값으로 붙인다. "바다뷰를 요청했으니 바다뷰"라고 붙이면
    # 바다가 안 보이는 코스가 바다뷰로 나간다. 지형 데이터가 없는 곳이면 붙이지 않고 표시만 한다.
    measured = scenery.tags_for_path(full_path, course)
    if measured is None:
        course["scenery_pending"] = True
    else:
        course["scenery"], course["tags"] = measured
        if course["tags"]:
            course["name"] = name.replace("(기본", f"({'·'.join(course['tags'][:2])}")
    return course


def generate_loop_candidates(lat: float, lng: float, target_distance_km: float, tags: set,
                             route_type: str = "loop") -> list[dict]:
    """Generate distinct orientations; OSM-snaps only the first to limit external requests."""
    if route_type != "loop":
        return [generate_loop_course(lat, lng, target_distance_km, tags, route_type)]

    shapes = list(enumerate(zip(RETRY_ANGLE_OFFSETS_DEG, LOOP_RADIUS_SCALES)))
    # 모든 후보의 꼭짓점 후보 지점 주변 길을 한 번에 받아 둔다. 후보마다 Overpass에 물으면
    # 느리고(요청이 전역 락으로 직렬화됨) 속도 제한에 걸린다.
    ways = fetch_runnable_ways([
        point for _, (angle, scale) in shapes
        for point in ring_points(lat, lng, target_distance_km, angle, scale)
    ])

    def build(index_and_shape):
        index, (angle_offset, radius_scale) = index_and_shape
        return generate_loop_course(
            lat, lng, target_distance_km, tags,
            route_type=route_type,
            angle_offset_deg=angle_offset,
            snap_to_osm=index == 0,
            radius_scale=radius_scale,
            ways=ways,
        )

    # 후보끼리는 서로를 기다릴 이유가 없다. 순서대로 만들면 사용자가 20초를 기다린다.
    candidates = []
    errors = []
    with ThreadPoolExecutor(max_workers=len(shapes)) as pool:
        for future in [pool.submit(build, shape) for shape in shapes]:
            try:
                candidates.append(future.result())
            except (ValueError, requests.HTTPError) as error:
                # 후보 하나가 실패해도 나머지 후보로 답한다
                errors.append(str(error))

    if not candidates:
        raise ValueError(errors[-1] if errors else "Tmap 보행자 순환 경로를 찾지 못했습니다.")

    # 가지를 잘라내면 순환이 목표보다 짧아지기 쉽다. 전부 크게 벗어났으면, 목표에 가장 가까운
    # 후보의 반경을 거리 비율만큼 키워서 한 번만 더 만든다 (Tmap 호출 +1).
    closest_shape = None
    best_gap = float("inf")
    for (_, (angle, scale)), course in zip(shapes, candidates):
        gap = abs(course["distance_km"] - target_distance_km) / target_distance_km
        if gap < best_gap:
            best_gap, closest_shape = gap, (angle, scale, course["distance_km"])
    if best_gap > DISTANCE_TOLERANCE and closest_shape and closest_shape[2] > 0:
        angle, scale, actual = closest_shape
        corrected = min(max(scale * target_distance_km / actual, 0.4), 1.8)
        try:
            candidates.append(generate_loop_course(
                lat, lng, target_distance_km, tags, route_type=route_type,
                angle_offset_deg=angle, snap_to_osm=False, radius_scale=corrected,
                ways=fetch_runnable_ways(ring_points(lat, lng, target_distance_km, angle, corrected)),
            ))
        except (ValueError, requests.HTTPError):
            pass
    useful = keep_useful_loops(candidates, target_distance_km)
    if not useful:
        # 빈 목록을 내보내면 사용자는 결과 0개를 받는다. 실패로 알려서 저장된 코스로 넘어가게 한다.
        raise ValueError("이 주변에서는 목표 거리에 맞는 순환 코스를 만들지 못했습니다.")
    return useful


MIN_LOOP_RATIO = 0.4       # 목표의 이 비율도 안 되는 순환은 코스로 내보내지 않는다
DUPLICATE_OVERLAP = 0.7    # 경로가 이만큼 겹치면 같은 코스로 본다


def _overlap(a: list, b: list) -> float:
    step_a = max(1, len(a) // 40)
    step_b = max(1, len(b) // 120)
    sample_a, sample_b = a[::step_a], b[::step_b]
    if not sample_a:
        return 0.0
    near = sum(1 for p in sample_a if any(haversine_m(p, q) <= 30 for q in sample_b))
    return near / len(sample_a)


def keep_useful_loops(candidates: list, target_distance_km: float) -> list:
    """쓸모없는 후보를 걸러낸다.

    - 막다른 길 왕복을 걷어내고 나면 거의 안 남는 후보(실측 0.04km 등)는 코스가 아니다.
    - 방향만 조금 달리 잡았을 뿐 사실상 같은 길인 후보는 목표 거리에 가까운 하나만 남긴다.
    """
    useful = [c for c in candidates if c.get("distance_km", 0) >= target_distance_km * MIN_LOOP_RATIO]
    useful.sort(key=lambda c: abs(c.get("distance_km", 0) - target_distance_km))
    kept = []
    for course in useful:
        path = course.get("path") or []
        if any(path and k.get("path") and _overlap(path, k["path"]) >= DUPLICATE_OVERLAP for k in kept):
            continue
        kept.append(course)
    return kept
