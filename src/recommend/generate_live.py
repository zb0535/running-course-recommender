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
from ..data_collection import area_cache, scenery
from ..data_collection.scenery import MEASURE_RADIUS
from ..data_collection.scenery import THRESHOLDS as SCENERY_THRESHOLDS
from ..data_collection.enrich import haversine_m
from ..data_collection.osm_overpass import OverpassUnavailable, post_overpass_query
from . import road_graph, sidewalk
from .safety import safety_index, score_components
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


def _over_water(area, lat: float, lng: float, point: tuple) -> bool:
    """출발점에서 이 지점까지 직선으로 가면 바다나 큰 강을 건너는가.

    건너편 섬이나 강 건너의 길은 가깝게 보여도 다리를 찾아 멀리 돌아가야 한다. 그런 곳에 경유지를
    잡으면 코스가 목표의 두 배가 되거나, 들어갔다 되나오는 구간이 된다(여수·여의도에서 실측).
    """
    return area is not None and area.water.crosses((lat, lng), point)


def _secluded_at_night(area, point: tuple) -> bool:
    """이 지점이 밤에 인적 드문 길(상점도 조명도 없는 공원·숲·물가 보행로) 위인가.

    밤에 경로만 사람 있는 길로 짜도, 경유지 자체가 강변 산책로 한가운데면 거기까지 갔다 와야 한다
    (여의도·망원동에서 낮과 밤 코스가 똑같이 나왔다). 밤에는 그런 곳을 경유지로 잡지 않는다.
    """
    graph = area.graph if area is not None else None
    if graph is None:
        return False
    ways = graph.ways_near(point[0], point[1], 30)
    return bool(ways) and all(graph.night_kind(way) == "secluded" for way in ways)


def pick_land_vertices(lat: float, lng: float, target_distance_km: float, ways: list,
                       angle_offset_deg: float = 0.0, radius_scale: float = 1.0, area=None,
                       night: bool = False) -> list:
    """길이 실제로 있는 방향에서만 꼭짓점을 고른다.

    출발점 둘레로 원을 그려 꼭짓점을 찍으면 해안가에선 절반이 바다에 떨어진다. 그러면 Tmap이
    해안선까지만 가서 코스가 목표의 1/3로 쪼그라든다(여수·해운대에서 5km 요청에 1.8~2.1km).
    8방향 중 근처에 길이 있는 방향만 남기고, 그중 서로 가장 고르게 떨어진 조합을 고른다.
    반환은 방향 순서대로라 경유지를 차례로 돌면 고리가 된다. 길이 있는 방향이 2개 미만이면 [].
    """
    step = 360 / RING_DIRECTIONS
    on_land = []
    for k, point in enumerate(ring_points(lat, lng, target_distance_km, angle_offset_deg, radius_scale)):
        snapped = _snap(point, ways)
        if snapped and not _over_water(area, lat, lng, snapped) and not (night and _secluded_at_night(area, snapped)):
            on_land.append(((angle_offset_deg + k * step) % 360, snapped))
    if len(on_land) < 2:
        return []

    count = min(VERTEX_COUNT, len(on_land))

    def min_gap(combo):
        angles = sorted(a for a, _ in combo)
        return min((angles[(i + 1) % len(angles)] - angles[i]) % 360 or 360 for i in range(len(angles)))

    best = max(itertools.combinations(on_land, count), key=min_gap)
    return [point for _, point in sorted(best, key=lambda pair: pair[0])]


def _snap(point: tuple, roads) -> tuple | None:
    """지점을 가장 가까운 길 위로 옮긴다. roads는 지역 캐시의 도로 색인이거나 예전 방식의 길 목록."""
    if hasattr(roads, "nearest_point"):
        return roads.nearest_point(point[0], point[1], SNAP_RADIUS_M)
    return _nearest_point_on_ways(point, roads)


def _offset(lat: float, lng: float, bearing_deg: float, distance_m: float) -> tuple:
    return (
        lat + distance_m / 111_320 * math.sin(math.radians(bearing_deg)),
        lng + distance_m / (111_320 * math.cos(math.radians(lat))) * math.cos(math.radians(bearing_deg)),
    )


def _arc(a: float, b: float) -> float:
    """두 방향 사이의 작은 쪽 각도 (0~180)."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _mid_bearing(a: float, b: float) -> float:
    """a에서 b로 작은 쪽 호를 따라 갈 때의 가운데 방향."""
    diff = (b - a + 540) % 360 - 180
    return (a + diff / 2) % 360


def _steerable(tags: set) -> list:
    """요청한 풍경 중 따라갈 수 있는(선이나 면으로 있는) 것의 측정값 이름."""
    return [SCENERY_THRESHOLDS[tag][0] for tag in sorted(tags)
            if tag in SCENERY_THRESHOLDS and SCENERY_THRESHOLDS[tag][0] in MEASURE_RADIUS]


SCENIC_BEARING_STEP = 15                 # 풍경 지점을 찾을 때 훑는 방향 간격
SCENIC_REACH = (0.6, 0.85, 1.1)          # 반경의 몇 배 거리에서 찾을지
STANDARD_REACH = 0.85
WIDE_APART_DEG = 150                     # 풍경 지점 둘이 이보다 벌어져 있으면 사이에 풍경 밖 지점을 넣는다


def scenic_vertices(area, tags: set, lat: float, lng: float, target_distance_km: float,
                    radius_scale: float = 1.0, angle_offset_deg: float = 0.0) -> list:
    """요청한 풍경 위에 경유지를 잡는다. 주변에 그 풍경이 없거나 따라갈 수 없는 종류면 [].

    풍경을 요청해도 길이 있는 아무 방향으로 고리를 만들면, 다 만든 뒤 재보고 버릴 수밖에 없다
    (강변을 요청했는데 강으로 가지 않는다). 받아 둔 지형에서 그 풍경에 닿는 길 위 지점을 찾아
    경유지로 쓴다.

    풍경 지점 중 가장 벌어진 둘을 고르고 그 사이에 하나를 더 넣는다.
    - 풍경이 한쪽에 있으면(해안, 조금 떨어진 강) 사이 지점도 풍경 위에서 골라 그 구간을 따라 달린다.
    - 풍경이 출발점을 지나가면(강가에서 출발) 양쪽 지점만 이으면 같은 길을 되짚게 되므로,
      사이 지점은 풍경 밖에서 골라 강을 따라 나갔다가 돌아서 반대쪽 강으로 들어오게 한다.
    """
    names = _steerable(tags)
    if not names or area is None:
        return []
    name = names[0]  # 한 가지 풍경을 따라 길을 잡는다. 나머지는 만든 뒤 측정으로 확인한다
    feature, near_m = area.indexes[name], MEASURE_RADIUS[name]

    radius_m = target_distance_km * radius_scale * 1000 / LOOP_LENGTH_PER_RADIUS
    scenic, plain = {}, {}
    for k in range(360 // SCENIC_BEARING_STEP):
        bearing = (angle_offset_deg + k * SCENIC_BEARING_STEP) % 360
        for reach in SCENIC_REACH:
            snapped = _snap(_offset(lat, lng, bearing, radius_m * reach), area.roads)
            if snapped is None or _over_water(area, lat, lng, snapped):
                continue
            if feature.near(snapped[0], snapped[1], near_m):
                # 같은 방향이면 표준 거리에 가까운 지점을 쓴다
                if bearing not in scenic or abs(reach - STANDARD_REACH) < abs(scenic[bearing][0] - STANDARD_REACH):
                    scenic[bearing] = (reach, snapped)
            elif reach == STANDARD_REACH:
                plain[bearing] = snapped
    if not scenic:
        return []

    bearings = sorted(scenic)
    first, last = max(itertools.combinations(bearings, 2), key=lambda pair: _arc(*pair),
                      default=(bearings[0], bearings[0]))
    chosen = {first: scenic[first][1], last: scenic[last][1]}
    middle = _mid_bearing(first, last)
    others = {b: s[1] for b, s in scenic.items() if b not in chosen}
    wide = _arc(first, last) > WIDE_APART_DEG
    pool = (plain or others) if wide else (others or plain)
    if pool is others and _arc(first, last) >= 60:
        # 풍경을 따라가는 구간: 사이에 풍경 지점을 둘 넣어 그 사이를 풍경에서 벗어나지 않고 잇게 한다.
        # 하나만 넣으면 Tmap이 한 블록 안쪽의 빠른 길로 이어 풍경 비율이 기준에 못 미친다(여수 39%).
        swing = (last - first + 540) % 360 - 180
        for fraction in (1 / 3, 2 / 3):
            wanted = (first + swing * fraction) % 360
            between = min(pool, key=lambda b: _arc(b, wanted))
            if _arc(between, wanted) <= 30:
                chosen[between] = pool[between]
    elif pool:
        if wide:
            # 거의 정반대로 벌어졌으면 '사이'가 양쪽에 있다. 한쪽은 물일 수 있으니(강가·해안에서 출발)
            # 실제로 길이 있는 쪽을 고른다.
            middle = min((middle, (middle + 180) % 360), key=lambda m: min(_arc(b, m) for b in pool))
        between = min(pool, key=lambda b: _arc(b, middle))
        if _arc(between, middle) <= 60:
            chosen[between] = pool[between]
    if wide and len(chosen) < 3:
        return []  # 풍경 양쪽 지점만 이으면 같은 길을 갔다 되짚어 오는 코스가 된다
    if len(chosen) < 2:
        return []

    # 한 방향으로 돌도록 정렬한다 (가운데 방향을 기준으로 양쪽으로 펼친 순서)
    ordered = sorted(chosen, key=lambda b: (b - middle + 540) % 360)
    return [chosen[b] for b in ordered]


LOOP_DETOUR = 1.35                       # 경유지를 직선으로 이은 둘레 대비 실제 길 길이
SCENIC_SCALES = (0.5, 0.65, 0.8, 1.0, 1.2, 1.45, 1.7, 2.0)
SCENIC_SHARE_WEIGHT = 0.5              # 풍경 구간이 40%p 많으면 길이가 20% 어긋나는 것과 맞바꾼다


def estimated_loop_km(lat: float, lng: float, waypoints: list) -> float:
    """경유지를 순서대로 돌아 출발점으로 오는 길이의 추정치 (Tmap을 부르지 않고 고르기 위한 것)."""
    ring = [(lat, lng)] + list(waypoints) + [(lat, lng)]
    return sum(haversine_m(a, b) for a, b in zip(ring, ring[1:])) * LOOP_DETOUR / 1000


def scenic_vertices_for_length(area, tags: set, lat: float, lng: float, want_km: float,
                               angle_offset_deg: float = 0.0) -> list:
    """풍경 위의 경유지 중에서 실제 길이가 want_km에 가장 가까울 조합을 고른다.

    풍경 지점은 풍경이 있는 곳에만 잡히므로 반경을 정해도 길이가 그대로 따라오지 않는다
    (실측: 4km 요청에 2.2km, 반경을 키우자 7.4km). 반경을 여러 개로 바꿔 가며 받아 둔 지형에서
    조합을 만들어 보고 — 외부 호출 없이 — 길이 추정치가 목표에 가깝고 풍경 구간이 긴 것을 쓴다.
    """
    names = _steerable(tags)
    if not names or area is None:
        return []
    feature, near_m = area.indexes[names[0]], MEASURE_RADIUS[names[0]]

    def scenic_share(waypoints: list) -> float:
        """고리 둘레 중 양 끝이 모두 풍경 위인 변의 비율 — 그 변은 풍경을 따라 달릴 가능성이 높다."""
        ring = [(lat, lng)] + list(waypoints) + [(lat, lng)]
        on = [feature.near(p[0], p[1], near_m) for p in ring]
        legs = [(haversine_m(a, b), on[i] and on[i + 1]) for i, (a, b) in enumerate(zip(ring, ring[1:]))]
        total = sum(length for length, _ in legs) or 1.0
        return sum(length for length, scenic in legs if scenic) / total

    best, best_cost = [], float("inf")
    for scale in SCENIC_SCALES:
        waypoints = scenic_vertices(area, tags, lat, lng, want_km, scale, angle_offset_deg)
        if not waypoints:
            continue
        gap = abs(estimated_loop_km(lat, lng, waypoints) - want_km) / want_km
        cost = gap - SCENIC_SHARE_WEIGHT * scenic_share(waypoints)
        if cost < best_cost:
            best, best_cost = waypoints, cost
    return best


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


ONE_WAY_DETOUR = 1.3  # 실제 길은 직선거리보다 이만큼 길다


def _one_way_end(lat, lng, target_distance_km, route_type, tags, area, offset):
    """편도 경로의 도착점. 왕복이면 목표의 절반까지만 나간다.

    예전에는 여기서도 순환용 반경 공식을 써서 5km 편도 요청에 1km짜리가 만들어졌다.
    요청한 풍경이 있으면 그 풍경 위의 지점을, 없으면 길이 있는 방향을 고른다.
    """
    leg_km = target_distance_km / 2 if route_type == "roundtrip" else target_distance_km
    straight_m = leg_km * 1000 / ONE_WAY_DETOUR
    if area is None or not area.covers(lat, lng, straight_m):
        # 받아 둔 범위 밖까지 나간다. 길에 붙이지 못하니 계산 좌표를 주고 Tmap이 가까운 길로 잡게 한다
        return _offset(lat, lng, offset, straight_m)
    names = _steerable(tags)
    fallback = None
    for k in range(360 // SCENIC_BEARING_STEP):
        snapped = _snap(_offset(lat, lng, (offset + k * SCENIC_BEARING_STEP) % 360, straight_m), area.roads)
        if snapped is None or _over_water(area, lat, lng, snapped):
            continue
        if not names or area.indexes[names[0]].near(snapped[0], snapped[1], MEASURE_RADIUS[names[0]]):
            return snapped
        fallback = fallback or snapped
    return fallback


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


def _route(area, start: tuple, end: tuple, waypoints: list = None, night: bool = False) -> dict:
    """start → 경유지 → end 경로. 좌표는 (lat, lng).

    받아 둔 도로망이 있으면 거기서 직접 짠다 — 최단 거리가 아니라 달리기 좋은 길(보행 전용길, 물가)을
    따라간다. 그런 다음 Tmap 보행 네트워크로 그 길이 정말 인도인지 확인하고, 아니면 피해서 다시 짠다
    (sidewalk.confirm). 도로망이 없거나 길이 이어지지 않으면 Tmap에 바로 묻는다.
    """
    graph = area.graph if area is not None else None
    if graph is not None:
        plan = lambda: road_graph.route(graph, start, end, waypoints, night=night)
        routed = plan()
        if routed is not None:
            return sidewalk.confirm(graph, start, end, waypoints or [], routed, plan)
    if start == end:
        return get_route((start[1], start[0]), (end[1], end[0]), start_name="출발지", end_name="출발지",
                         waypoints=[(p[1], p[0]) for p in waypoints or []])
    return get_route((start[1], start[0]), (end[1], end[0]))


def generate_loop_course(lat: float, lng: float, target_distance_km: float, tags: set,
                         route_type: str = "loop", angle_offset_deg: float | None = None,
                         snap_to_osm: bool = True, radius_scale: float = 1.0,
                         ways: list | None = None, area=None, night: bool = False) -> dict:
    """Generate one Tmap course candidate for a waypoint polygon orientation.

    ways(주변 보행로)를 주면 길이 있는 방향에서만 꼭짓점을 고른다. 없으면 예전처럼 원 위에 찍는다.
    area(받아 둔 주변 길·지형)를 주면 요청한 풍경 위에 경유지를 잡는다.
    순환이 아니면 편도 경로를 만든다 — 왕복은 목표의 절반까지 나가고, 복귀 구간은 호출 쪽이 붙인다.
    """
    offsets = (angle_offset_deg,) if angle_offset_deg is not None else RETRY_ANGLE_OFFSETS_DEG
    if ways is None and area is not None:
        ways = area.roads

    not_sidewalk_only = False
    for offset in offsets:
        if route_type == "loop":
            waypoints = scenic_vertices_for_length(area, tags, lat, lng, target_distance_km * radius_scale, offset)
            if waypoints:
                pass
            elif ways:
                waypoints = pick_land_vertices(lat, lng, target_distance_km, ways, offset, radius_scale, area, night)
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
                route = _route(area, (lat, lng), (lat, lng), waypoints, night)
            except requests.HTTPError as error:
                logger.debug("이 경유지 조합은 Tmap이 거절했습니다: %s", error)
                continue
        else:
            waypoint = _one_way_end(lat, lng, target_distance_km, route_type, tags, area, offset)
            if waypoint is None:
                continue
            try:
                route = _route(area, (lat, lng), waypoint, night=night)
            except requests.HTTPError as error:
                logger.debug("이 도착점은 Tmap이 거절했습니다: %s", error)
                continue

        if route is None:
            continue
        if "router" not in route:
            # 도로망 없이 Tmap만으로 만든 경로: 구간 종류는 Tmap이 주므로 같은 기준으로 확인한다
            tmap_mix = sidewalk.mix(route)
            typed = any("roadType" in (f.get("properties") or {}) for f in route.get("features", []))
            route = dict(route, road_mix=tmap_mix if typed else None, road_score=None,
                         # 구간 종류가 아예 오지 않았으면 확인하지 못한 것이다(None)
                         # 도로망이 없으면 골목과 차도를 구분할 수 없다. 종류 미상 구간만 본다
                         no_car_lanes=(tmap_mix["unknown"] <= sidewalk.UNKNOWN_ALLOWED) if typed else None,
                         sidewalk_only=(tmap_mix["alley"] == 0 and tmap_mix["unknown"] <= sidewalk.UNKNOWN_ALLOWED)
                         if typed else None)
        if route.get("no_car_lanes") is False:
            # 인도 없는 차도를 지나는 구간이 있다. 달리기 코스로 내보내지 않는다.
            not_sidewalk_only = True
            continue
        full_path, distance_m = extract_path(route)
        if not full_path:
            continue
        raw_steps = extract_steps(route)
        if route_type == "loop" and retraced_ratio(full_path) > MAX_RETRACED:
            # 막다른 길로 들어갔다 나오는 가지가 있다. 먼저 잘라낸 경로 위 지점들을 경유지로 다시
            # 받아 안내까지 실제 데이터로 맞춰 본다(가지만 자르면 갈림길 안내가 틀린 채로 남는다).
            pruned, spans = _prune_with_spans(full_path)
            # 직접 짠 경로는 Tmap에 다시 묻지 않는다 (그러면 최단 거리 경로로 바뀐다)
            rerouted = None if route.get("router") else _try_reroute(lat, lng, pruned)
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
        if not_sidewalk_only:
            raise ValueError("차도를 피해 가는 코스를 만들지 못했습니다. 거리나 코스 형태를 바꿔 보세요.")
        if route_type == "loop":
            raise ValueError("Tmap 보행자 순환 경로를 찾지 못했습니다.")
        raise ValueError("Tmap 보행자 경로를 찾지 못했습니다.")

    steps = assign_positions(raw_steps, full_path)
    return_route = None
    if route_type == "roundtrip" and route.get("router") == "local":
        # 돌아오는 길도 같은 도로망에서 짠다 (없으면 호출 쪽이 Tmap으로 붙인다)
        return_route = _route(area, tuple(full_path[-1]), (lat, lng), night=night)
        if return_route.get("no_car_lanes") is False:
            return_route = None
    if route_type != "loop":
        route_type = "oneway"  # 만든 건 편도 경로다. 왕복이면 호출 쪽에서 실제 복귀 경로를 붙인다
    offset_suffix = f"-a{offset:g}-r{radius_scale:g}" if route_type == "loop" else ""
    name_suffix = f", {offset:g}°, 반경 {radius_scale:.0%}" if route_type == "loop" else ""
    name = f"현위치 기반 {'순환' if route_type == 'loop' else '편도'} 코스 (기본{name_suffix})"

    course = {
        # 요청한 풍경이 다르면 다른 코스다 — 같은 id를 쓰면 저장할 때 서로 덮어쓴다
        "id": f"generated-{lat:.4f}-{lng:.4f}-{target_distance_km}-{route_type}{offset_suffix}"
              + "".join(f"-{tag}" for tag in sorted(tags)),
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
    if route.get("road_mix"):
        course["road_mix"] = route["road_mix"]       # 어떤 길로 가는지 (길이 비율)
    if route.get("router"):
        course["router"] = route["router"]
        course["road_score"] = route["road_score"]   # 달리기 좋은 길 점수 평균 (0~1)
    # 인도 없는 차도를 지나지 않는가(False인 코스는 여기까지 오지 않는다). None은 Tmap으로 확인하지 못한 경우
    course["no_car_lanes"] = route.get("no_car_lanes")
    # 골목도 지나지 않고 인도·차 없는 길·횡단시설로만 가는가
    course["sidewalk_only"] = route.get("sidewalk_only")
    if route.get("access_m"):
        # 출발 지점이 인도가 없는 길이라, 코스는 가장 가까운 인도에서 시작한다. 거기까지는 걸어서 간다
        course["access_m"] = route["access_m"]
    if return_route is not None:
        course["return_path"], _ = extract_path(return_route)
        course["return_steps"] = extract_steps(return_route)
    if route.get("lively_ratio") is not None:
        course["lively_ratio"] = route["lively_ratio"]       # 상가·큰길·조명 있는 길의 비율
        course["secluded_ratio"] = route["secluded_ratio"]   # 인적 드문 공원·숲·물가 보행로의 비율
        course["planned_for_night"] = night
    if area is not None and getattr(area, "local", False):
        # 안전 점수와 신호등 수를 고정값이 아니라 그 자리의 실제 시설로 계산한다 (저장된 코스와 같은 기준)
        points = area.safety_points
        course["safety_breakdown"] = score_components(full_path, points["cctv"], points["convenience"], points["police"])
        course["safety_score"] = safety_index(full_path, points["cctv"], points["convenience"], points["police"])
        course["traffic_signal_count"] = _signals_on(full_path, points["signals"])
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


SIGNAL_ON_PATH_M = 25


def _signals_on(path: list, signals: list) -> int:
    """경로가 지나는 신호등 수 (경로 25m 안)."""
    if not signals or not path:
        return 0
    lats, lngs = [p[0] for p in path], [p[1] for p in path]
    pad = 0.0005
    box = (min(lats) - pad, min(lngs) - pad, max(lats) + pad, max(lngs) + pad)
    close = [s for s in signals if box[0] <= s["lat"] <= box[2] and box[1] <= s["lng"] <= box[3]]
    return sum(1 for s in close if any(haversine_m((s["lat"], s["lng"]), p) <= SIGNAL_ON_PATH_M for p in path))


def generate_loop_candidates(lat: float, lng: float, target_distance_km: float, tags: set,
                             route_type: str = "loop", night: bool = False) -> list[dict]:
    """Generate distinct orientations; OSM-snaps only the first to limit external requests."""
    # 출발점 주변의 길·지형은 지역당 한 번만 받는다(area_cache). 받아 둔 동네면 Overpass 호출이 없다.
    if route_type == "loop":
        reach_m = target_distance_km * 1000 * max(LOOP_RADIUS_SCALES) * max(SCENIC_REACH) / LOOP_LENGTH_PER_RADIUS
    else:
        reach_m = target_distance_km * 1000 / (2 if route_type == "roundtrip" else 1) / ONE_WAY_DETOUR
    area = area_cache.load_area(lat, lng, radius_m=reach_m + SNAP_RADIUS_M)

    if route_type != "loop":
        return [generate_loop_course(lat, lng, target_distance_km, tags, route_type, area=area, night=night)]

    shapes = list(enumerate(zip(RETRY_ANGLE_OFFSETS_DEG, LOOP_RADIUS_SCALES)))
    if area is not None:
        ways = area.roads
    else:
        # 지역 데이터를 못 받았을 때만 예전 방식: 꼭짓점 후보 주변 길을 한 번에 받는다
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
            area=area,
            night=night,
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
                ways=ways if area is not None else fetch_runnable_ways(
                    ring_points(lat, lng, target_distance_km, angle, corrected)),
                area=area,
                night=night,
            ))
        except (ValueError, requests.HTTPError):
            pass
    useful = keep_useful_loops(candidates, target_distance_km)
    if night:
        useful = safest_at_night(useful)
    if not useful:
        # 빈 목록을 내보내면 사용자는 결과 0개를 받는다. 실패로 알려서 저장된 코스로 넘어가게 한다.
        raise ValueError("이 주변에서는 목표 거리에 맞는 순환 코스를 만들지 못했습니다.")
    return useful


NIGHT_SECLUDED_SLACK = 0.05   # 밤에는 인적 드문 구간이 가장 적은 후보와 이만큼 안에 드는 후보만 남긴다


def safest_at_night(candidates: list) -> list:
    """밤에는 거리가 조금 덜 맞더라도 인적 드문 구간이 적은 코스를 준다.

    경로를 사람 있는 길로 짜도 경유지 배치에 따라 강변·공원 구간을 피할 수 없는 후보가 있다. 그런 후보가
    거리가 더 잘 맞는다는 이유로 1순위가 되면 밤 경로를 따로 짠 의미가 없다(여의도·망원동에서 확인).
    인적 드문 비율을 잴 수 없는 후보(도로망 없이 만든 것)는, 잰 후보가 하나라도 있으면 뺀다.
    """
    measured = [c for c in candidates if c.get("secluded_ratio") is not None]
    if not measured:
        return candidates
    best = min(c["secluded_ratio"] for c in measured)
    return [c for c in measured if c["secluded_ratio"] <= best + NIGHT_SECLUDED_SLACK]


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
