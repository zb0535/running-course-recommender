"""Tmap(SK Open API) 보행자 경로 API 클라이언트.

카카오 Directions는 자동차 도로 기준이라 인도/공원 산책로를 반영하지 않는다.
러닝 코스는 실제로 사람이 걸을 수 있는 길이어야 하므로 보행자 전용 경로로 교체.

키 발급: https://openapi.sk.com (무료체험 등급, 경로안내 API그룹 하루 1,000건)
발급 후 환경변수 TMAP_APP_KEY 설정.
"""
import os

import requests

PEDESTRIAN_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian"


def get_route(start: tuple, end: tuple, start_name: str = "", end_name: str = "",
              waypoints: list[tuple] | None = None) -> dict:
    """start/end = (lng, lat), waypoints = [(lng, lat), ...].

    경유지는 Tmap의 passList 형식으로 전달한다. 시작점과 끝점을 같게 하고
    경유지를 넣으면 각 구간을 실제 보행로로 연결한 순환 경로를 받을 수 있다.
    """
    app_key = os.environ.get("TMAP_APP_KEY")
    if not app_key:
        raise RuntimeError("TMAP_APP_KEY 환경변수가 설정되지 않았습니다.")

    headers = {"appKey": app_key, "Content-Type": "application/json", "Accept": "application/json"}
    body = {
        "startX": str(start[0]), "startY": str(start[1]), "startName": start_name or "출발지",
        "endX": str(end[0]), "endY": str(end[1]), "endName": end_name or "도착지",
    }
    if waypoints:
        body["passList"] = "_".join(f"{lng},{lat}" for lng, lat in waypoints)
    resp = requests.post(PEDESTRIAN_URL, params={"version": 1}, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_path(route_json: dict) -> tuple:
    """(path=[[lat,lng],...], distance_m) 반환. LineString 구간을 순서대로 이어붙인다."""
    features = route_json.get("features", [])
    distance_m = 0
    for f in features:
        if f["geometry"]["type"] == "Point":
            props = f.get("properties", {})
            if "totalDistance" in props:
                distance_m = props["totalDistance"]
                break

    path = []
    for f in features:
        if f["geometry"]["type"] == "LineString":
            for lng, lat in f["geometry"]["coordinates"]:
                path.append([lat, lng])
    return path, distance_m


def extract_steps(route_json: dict) -> list:
    """턴바이턴 안내 목록: [{lat, lng, description, turn_type}, ...].

    Tmap이 각 안내 지점(Point)에 "우회전 후 OO로를 따라 31m 이동" 같은 자연어 설명을
    이미 만들어서 준다 — 직접 좌/우회전을 판정할 필요 없이 그대로 쓰면 된다.
    """
    steps = []
    for f in route_json.get("features", []):
        if f["geometry"]["type"] != "Point":
            continue
        props = f.get("properties", {})
        description = props.get("description")
        if not description:
            continue
        lng, lat = f["geometry"]["coordinates"]
        steps.append({
            "lat": lat, "lng": lng,
            "description": description,
            "turn_type": props.get("turnType"),
        })
    return steps
