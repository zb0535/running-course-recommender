"""사전 전처리한 GraphML만 사용하는 경량 FastAPI Loop 라우터.

    pip install -r requirements.txt
    uvicorn router:app --host 0.0.0.0 --port 8000 --workers 1

Render 런타임에 OSMnx/GeoPandas/SciPy/Shapely를 설치하지 않는다.
전체 그래프 복사, 모든 노드까지의 경로 목록, 온라인 OSM 호출도 하지 않는다.
"""
from __future__ import annotations

from array import array
from contextlib import asynccontextmanager
from dataclasses import dataclass
from heapq import heappop, heappush
import json
import math
import os
from pathlib import Path
from threading import Lock
import time
from typing import Annotated
import xml.etree.ElementTree as ET

import networkx as nx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

SCHEMA = "running-loop-v1"
EARTH_M = 6_371_008.8


def peak_rss_mb() -> float | None:
    """Linux(Render)의 프로세스 최대 RSS. Windows 개발 환경은 측정 생략."""
    try:
        import resource
        import sys
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    except ImportError:
        return None


@dataclass(frozen=True)
class Settings:
    map_path: Path
    max_nodes: int = 60_000
    max_edges: int = 90_000
    max_map_mb: float = 64.0
    load_rss_limit_mb: float = 350.0
    max_snap_m: float = 200.0
    timeout_s: float = 5.0
    max_candidates: int = 64

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            map_path=Path(os.getenv("MAP_PATH", str(Path(__file__).with_name("yeouido_map.graphml")))),
            max_nodes=int(os.getenv("MAX_NODES", "60000")),
            max_edges=int(os.getenv("MAX_EDGES", "90000")),
            max_map_mb=float(os.getenv("MAX_MAP_MB", "64")),
            load_rss_limit_mb=float(os.getenv("LOAD_RSS_LIMIT_MB", "350")),
            max_snap_m=float(os.getenv("MAX_SNAP_M", "200")),
            timeout_s=float(os.getenv("ROUTE_TIMEOUT_S", "5")),
            max_candidates=int(os.getenv("MAX_CANDIDATES", "64")),
        )


def haversine_m(a: tuple | list, b: tuple | list) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def _finite(value, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"유한한 수가 아닌 {name}")
    return result


def load_light_graph(settings: Settings) -> nx.MultiGraph:
    """GraphML을 스트리밍해서 읽으며 원본 XML 노드를 즉시 버린다.

    nx.read_graphml은 파일 전체 XML 트리와 NetworkX 그래프가 동시에 존재하여
    로드 순간의 메모리가 크게 늘 수 있다. 서버에서는 우리 스키마의 작은 속성만
    파싱하고, 노드/간선 하나를 처리할 때마다 XML에서 제거한다.
    이 함수는 preprocess_map.py 출력 전용이며 임의의 OSMnx GraphML 로더가 아니다.
    """
    path = settings.map_path
    if not path.is_file():
        raise RuntimeError(f"지도 파일 없음: {path}. 로컬에서 preprocess_map.py를 먼저 실행하세요.")
    if path.stat().st_size > settings.max_map_mb * 1024 * 1024:
        raise RuntimeError("지도 파일이 MAX_MAP_MB 한도를 넘었습니다. 지역을 더 작게 나누세요.")
    graph = nx.MultiGraph()
    keys: dict[str, str] = {}
    metadata: dict[str, str] = {}
    graph_element = None
    edge_count = 0

    def values(element) -> dict:
        return {keys.get(child.attrib.get("key"), ""): child.text or ""
                for child in element if child.tag.rsplit("}", 1)[-1] == "data"}

    def memory_guard() -> None:
        rss = peak_rss_mb()
        if rss is not None and rss > settings.load_rss_limit_mb:
            raise RuntimeError(f"지도 로딩 RSS {rss:.1f}MB 초과. 지도 영역/속성을 줄여주세요.")

    with path.open("rb") as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = element.tag.rsplit("}", 1)[-1]
            if event == "start":
                if tag == "graph":
                    if graph_element is not None or element.get("edgedefault") != "undirected":
                        raise ValueError("무방향 그래프 1개만 지원합니다.")
                    graph_element = element
                continue
            if tag == "key":
                keys[element.attrib["id"]] = element.attrib.get("attr.name", "")
                element.clear()
            elif tag == "node":
                n = int(element.attrib["id"])
                if n in graph or len(graph) >= settings.max_nodes:
                    raise ValueError("중복 노드 또는 MAX_NODES 초과입니다.")
                d = values(element)
                lat, lng = _finite(d["y"], "위도"), _finite(d["x"], "경도")
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    raise ValueError("잘못된 지도 좌표입니다.")
                graph.add_node(n, lat=lat, lng=lng, mx=_finite(d["mx"], "mx"),
                               my=_finite(d["my"], "my"), crossing=d.get("crossing") == "True" or d.get("crossing") == "true")
                graph_element.remove(element)
                element.clear()
                if len(graph) % 2000 == 0:
                    memory_guard()
            elif tag == "edge":
                u, v = int(element.attrib["source"]), int(element.attrib["target"])
                key = int(element.attrib["id"])
                if u not in graph or v not in graph or u == v or graph.has_edge(u, v, key):
                    raise ValueError("참조 노드가 없거나 분할되지 않은 self-loop/중복 edge입니다.")
                if edge_count >= settings.max_edges:
                    raise ValueError("MAX_EDGES 초과. 지도 영역을 줄여주세요.")
                d = values(element)
                length = _finite(d["length"], "length")
                max_segment = float(metadata.get("max_segment_m", "50"))
                if not 0 < length <= max_segment + 1e-5:
                    raise ValueError("허용 길이를 넘거나 길이가 0인 간선입니다.")
                coords_raw = json.loads(d["coords"])
                if not 2 <= len(coords_raw) <= 2000:
                    raise ValueError("잘못된 간선 좌표 개수입니다.")
                coords = tuple((_finite(p[0], "lat"), _finite(p[1], "lng")) for p in coords_raw)
                a, b = int(d["geom_from"]), int(d["geom_to"])
                if {a, b} != {u, v}:
                    raise ValueError("geometry 방향 정보가 간선과 다릅니다.")
                for node, point in ((a, coords[0]), (b, coords[-1])):
                    nd = graph.nodes[node]
                    if haversine_m((nd["lat"], nd["lng"]), point) > 0.2:
                        raise ValueError("geometry와 노드가 이어지지 않습니다.")
                graph.add_edge(u, v, key=key, length=length, coords=coords, geom_from=a)
                edge_count += 1
                graph_element.remove(element)
                element.clear()
                if edge_count % 2000 == 0:
                    memory_guard()
            elif tag == "data" and graph_element is not None and element in graph_element:
                metadata[keys.get(element.get("key"), "")] = element.text or ""
                graph_element.remove(element)
                element.clear()
    if metadata.get("schema_version") != SCHEMA or not graph.number_of_edges():
        raise ValueError("지원하지 않는 지도 스키마/빈 지도입니다. preprocess_map.py로 생성하세요.")
    graph.graph.update(metadata)
    memory_guard()
    # 요청 사이에 그래프를 수정하지 않는다. 검색 상태는 모두 요청의 로컬 변수로 둔다.
    return nx.freeze(graph)


class SearchTimeout(Exception):
    pass


class RouteFailure(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class LoopEngine:
    def __init__(self, graph: nx.MultiGraph, settings: Settings):
        self.graph, self.settings = graph, settings
        # KD-tree를 위해 NumPy/SciPy를 설치하는 대신 단위구 좌표를 packed array로 저장.
        # 요청마다 O(N) 스캔이지만 지역 지도 규모에서는 단순하고 메모리 사용량이 작다.
        self.node_ids = array("q")
        self.xyz = array("d")
        for node, data in graph.nodes(data=True):
            lat, lng = math.radians(data["lat"]), math.radians(data["lng"])
            self.node_ids.append(node)
            self.xyz.extend((math.cos(lat) * math.cos(lng), math.cos(lat) * math.sin(lng), math.sin(lat)))

    def nearest_node(self, lat: float, lng: float) -> tuple[int, float]:
        rlat, rlng = math.radians(lat), math.radians(lng)
        qx, qy, qz = math.cos(rlat) * math.cos(rlng), math.cos(rlat) * math.sin(rlng), math.sin(rlat)
        best, best_dot = None, -math.inf
        for i, node in enumerate(self.node_ids):
            j = i * 3
            dot = qx * self.xyz[j] + qy * self.xyz[j + 1] + qz * self.xyz[j + 2]
            if dot > best_dot:
                best, best_dot = node, dot
        if best is None:
            raise RouteFailure("empty_graph", "빈 지도입니다.")
        d = self.graph.nodes[best]
        return best, haversine_m((lat, lng), (d["lat"], d["lng"]))

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise SearchTimeout()

    def _dijkstra(self, source: int, cutoff: float, deadline: float, *, target: int | None = None,
                  blocked_nodes: set | None = None, blocked_edges: set | None = None,
                  variant: int = 0) -> tuple[dict, dict]:
        """NetworkX의 인접 리스트를 읽는, predecessor만 보관하는 다익스트라.

        nx.single_source_dijkstra가 반환하는 '모든 노드의 전체 path'는 긴 체인에서
        O(N²) 메모리가 될 수 있다. dist + 부모 간선만 보관하여 O(V+E)로 제한한다.
        MultiGraph라 부모 '노드'뿐 아니라 선택한 간선 key도 보관해야 한다.
        variant=1은 양의 작은 결정적 가중치를 더해 다른 나가는 길을 시도한다.
        이는 환경 점수가 아니라 검색 다양성용이며 최종 거리는 언제나 length 합이다.
        """
        self._check_deadline(deadline)
        blocked_nodes = blocked_nodes or set()
        blocked_edges = blocked_edges or set()
        distance, parent = {source: 0.0}, {}
        heap = [(0.0, source)]
        iterations = 0
        while heap:
            cost, u = heappop(heap)
            if cost != distance[u]:
                continue
            iterations += 1
            if iterations % 128 == 0:
                self._check_deadline(deadline)
            if u == target:
                break
            for v, parallel in self.graph.adj[u].items():
                if v in blocked_nodes:
                    continue
                for key, data in parallel.items():
                    token = (min(u, v), max(u, v), key)
                    if token in blocked_edges:
                        continue
                    # split edge key는 파일 안에서 고유. Python hash의 실행별 난수성은 쓰지 않는다.
                    factor = 1.0 + (0.30 * ((key * 2654435761) % 997) / 997 if variant else 0.0)
                    candidate = cost + data["length"] * factor
                    if candidate <= cutoff and candidate < distance.get(v, math.inf):
                        distance[v] = candidate
                        parent[v] = (u, key)
                        heappush(heap, (candidate, v))
        return distance, parent

    @staticmethod
    def _reconstruct(parent: dict, source: int, target: int) -> tuple[list, list]:
        nodes, edges = [target], []
        cursor = target
        while cursor != source:
            previous, key = parent[cursor]
            edges.append((previous, cursor, key))
            nodes.append(previous)
            cursor = previous
        return list(reversed(nodes)), list(reversed(edges))

    def _candidates(self, source: int, distances: dict, target_m: float) -> list[int]:
        """거리대 7개 × 방위 16개에서 고르게 반환점을 고른다.

        이전 4×8 표본은 10km 요청에서도 목표의 절반보다 가까운 반환점에 편중돼,
        더 긴 비중복 고리가 가능한 방향을 탐색하지 못했다. 후보를 넓히되 각 거리대·방향에서
        한 노드만 남기고 시간 제한을 유지한다.
        """
        origin = self.graph.nodes[source]
        buckets = {}
        fractions = (0.20, 0.28, 0.35, 0.43, 0.50, 0.58, 0.65)
        sector_count = 16
        for node, distance in distances.items():
            if node == source or distance < min(30.0, target_m * 0.1):
                continue
            d = self.graph.nodes[node]
            angle = math.atan2(d["my"] - origin["my"], d["mx"] - origin["mx"])
            sector = int((angle + math.pi) / (2 * math.pi) * sector_count) % sector_count
            for band, fraction in enumerate(fractions):
                rank = abs(distance - target_m * fraction)
                bucket = (band, sector)
                if bucket not in buckets or rank < buckets[bucket][0]:
                    buckets[bucket] = (rank, node)
        # 먼 반환점부터 시도해 긴 Loop를 먼저 찾되, 방위별로 라운드로빈하여 편향을 막는다.
        order, seen = [], set()
        for band in reversed(range(len(fractions))):
            for sector in range(sector_count):
                if (band, sector) in buckets:
                    node = buckets[band, sector][1]
                    if node not in seen:
                        order.append(node)
                        seen.add(node)
        return order[:self.settings.max_candidates]

    def _length(self, edges: list) -> float:
        return sum(self.graph[u][v][key]["length"] for u, v, key in edges)

    def _coordinates(self, edges: list) -> list[list[float]]:
        path = []
        for u, v, key in edges:
            data = self.graph[u][v][key]
            points = data["coords"] if data["geom_from"] == u else reversed(data["coords"])
            for point in points:
                coordinate = list(point)
                if not path or coordinate != path[-1]:
                    path.append(coordinate)
        return path

    def find_loop(self, lat: float, lng: float, target_m: float, tolerance: float = 0.15) -> dict:
        started = time.monotonic()
        deadline = started + self.settings.timeout_s
        source, snap_m = self.nearest_node(lat, lng)
        if snap_m > self.settings.max_snap_m:
            raise RouteFailure("outside_map", f"가장 가까운 노드가 {snap_m:.0f}m 떨어져 있습니다. 지도 범위를 확인하세요.")
        if self.graph.degree(source) < 2:
            raise RouteFailure("no_loop_at_start", "시작 노드가 막다른 길입니다. 같은 길을 되짚지 않는 Loop를 만들 수 없습니다.")
        best = None
        attempts, truncated = 0, False
        # 요청 거리보다 35%까지 긴 경로도 검색하지만, 반환 시 허용 오차 충족 여부를 별도로 표시.
        # 정확한 목표 길이의 단순 cycle을 찾는 것은 일반적으로 어려워 휴리스틱을 사용한다.
        max_length = target_m * 1.35
        try:
            for variant in (0, 1):
                distances, parent = self._dijkstra(source, max_length / 2, deadline, variant=variant)
                for turn in self._candidates(source, distances, target_m):
                    self._check_deadline(deadline)
                    out_nodes, out_edges = self._reconstruct(parent, source, turn)
                    outbound_m = self._length(out_edges)
                    # 나가는 길의 내부 노드와 사용한 간선을 '금지'한다. 가중치만 높이는 방식과 달리
                    # 돌아오는 길에 같은 길이 섞이지 않는다. 원본 그래프는 수정/복사하지 않는다.
                    forbidden_nodes = set(out_nodes[1:-1])
                    forbidden_edges = {(min(u, v), max(u, v), k) for u, v, k in out_edges}
                    back_distances, back_parent = self._dijkstra(
                        turn, max_length - outbound_m, deadline, target=source,
                        blocked_nodes=forbidden_nodes, blocked_edges=forbidden_edges,
                    )
                    attempts += 1
                    if source not in back_distances:
                        continue
                    back_nodes, back_edges = self._reconstruct(back_parent, turn, source)
                    nodes = out_nodes + back_nodes[1:]
                    edges = out_edges + back_edges
                    length = outbound_m + self._length(back_edges)
                    # 독립 검증: 시작/끝 동일, 중간 노드와 물리 그래프 간선 재사용 없음.
                    tokens = [(min(u, v), max(u, v), k) for u, v, k in edges]
                    if nodes[0] != nodes[-1] or len(set(nodes[:-1])) != len(nodes) - 1 or len(set(tokens)) != len(tokens):
                        raise RuntimeError("Loop 불변식 검증 실패")
                    error = abs(length - target_m)
                    if best is None or error < best[0]:
                        best = (error, length, nodes, edges)
                    # 2% 이내면 불필요한 CPU 사용을 줄이고 조기 종료한다.
                    if error <= target_m * 0.02:
                        break
                if best and best[0] <= target_m * 0.02:
                    break
        except SearchTimeout:
            truncated = True
        if best is None:
            if truncated:
                raise RouteFailure("search_timeout", "시간 예산 안에 Loop를 찾지 못했습니다. 거리를 줄이거나 다시 시도하세요.")
            raise RouteFailure("no_loop_found", "검색 후보에서 겹치지 않는 Loop를 찾지 못했습니다. 위치/거리를 바꿔주세요. 모든 가능한 경로를 탐색한 결과는 아닙니다.")
        error, length, nodes, edges = best
        path = self._coordinates(edges)
        if len(path) < 3 or path[0] != path[-1]:
            raise RuntimeError("경로 좌표가 닫히지 않았습니다.")
        matched = error <= target_m * tolerance
        warnings = []
        if not matched:
            warnings.append("찾은 후보 중 가장 가까운 거리지만 요청한 거리 허용 오차를 충족하지 못했습니다.")
        if truncated:
            warnings.append("검색 시간 제한에 도달해 그때까지 찾은 최선 후보를 반환합니다.")
        if snap_m > 5:
            warnings.append("시작 위치를 도로 노드에 스냅했습니다. 사용자 위치에서 노드까지의 접근 경로는 포함하지 않습니다.")
        return {
            "source": "osm_graph", "route_type": "roundtrip",
            "requested_distance_m": target_m, "distance_m": round(length, 2),
            "distance_error_m": round(error, 2), "distance_error_ratio": round(error / target_m, 5),
            "within_tolerance": matched, "is_closed": True, "reused_edge_count": 0,
            "snapped_start": {"node_id": source, "lat": self.graph.nodes[source]["lat"],
                              "lng": self.graph.nodes[source]["lng"], "distance_from_user_m": round(snap_m, 2)},
            "path": path, "node_ids": nodes, "edge_ids": [k for _, _, k in edges],
            "known_crossing_node_count": sum(bool(self.graph.nodes[n].get("crossing")) for n in nodes[:-1]),
            "candidates_checked": attempts, "search_truncated": truncated,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1), "warnings": warnings,
        }
