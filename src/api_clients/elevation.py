"""Open Topo Data 표고 API 클라이언트 (무료, 키 불필요).

VWorld는 표고를 포인트 단위 REST API가 아닌 DEM 래스터 파일 다운로드로만 제공하고,
OpenTopography Point Elevation API는 무료 등급이 하루 50건으로 너무 빡빡해서(개발 중 바로 소진됨)
Open Topo Data(api.opentopodata.org, SRTM 기반, 키 불필요, 요청당 최대 100지점 배치 조회,
하루 1000건)로 교체했다.
"""
import requests

ELEVATION_URL = "https://api.opentopodata.org/v1/srtm30m"
BATCH_SIZE = 100


def get_elevation(lat: float, lng: float) -> float:
    """지정 좌표의 표고(m)를 반환."""
    return get_elevation_profile([(lat, lng)])[0]


def get_elevation_profile(path: list) -> list:
    """path = [(lat, lng), ...] 순서로 각 지점의 표고(m) 리스트 반환. 최대 100개씩 배치 요청."""
    elevations = []
    for i in range(0, len(path), BATCH_SIZE):
        chunk = path[i:i + BATCH_SIZE]
        locations = "|".join(f"{lat},{lng}" for lat, lng in chunk)
        resp = requests.get(ELEVATION_URL, params={"locations": locations}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        elevations.extend(r["elevation"] for r in data["results"])
    return elevations
