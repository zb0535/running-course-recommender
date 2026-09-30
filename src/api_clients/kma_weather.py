"""기상청 API허브 단기예보 클라이언트.

키 발급: https://apihub.kma.go.kr
발급 후 환경변수 KMA_API_KEY 설정. 실시간 추천 조정(기온/강수)에 사용.

단기예보 API는 위경도가 아니라 5km 격자 좌표(nx, ny)를 쓰므로, 코스 DB의 위경도를
매번 직접 격자로 변환한다 (기상청 공식 Lambert Conformal Conic 변환식).
"""
import math
import os
from datetime import datetime, timedelta

import requests

FORECAST_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"

# 기상청 격자 변환 상수 (LCC 투영)
_RE = 6371.00877
_GRID = 5.0
_SLAT1, _SLAT2 = 30.0, 60.0
_OLON, _OLAT = 126.0, 38.0
_XO, _YO = 43, 136

_RELEASE_TIMES = ["0200", "0500", "0800", "1100", "1400", "1700", "2000", "2300"]


def latlon_to_grid(lat: float, lon: float) -> tuple:
    """위경도 -> 기상청 5km 격자 (nx, ny)."""
    degrad = math.pi / 180.0
    re = _RE / _GRID
    slat1, slat2 = _SLAT1 * degrad, _SLAT2 * degrad
    olon, olat = _OLON * degrad, _OLAT * degrad

    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    )
    sf = math.pow(math.tan(math.pi * 0.25 + slat1 * 0.5), sn) * math.cos(slat1) / sn
    ro = re * sf / math.pow(math.tan(math.pi * 0.25 + olat * 0.5), sn)

    ra = re * sf / math.pow(math.tan(math.pi * 0.25 + lat * degrad * 0.5), sn)
    theta = (lon * degrad - olon) * sn
    nx = math.floor(ra * math.sin(theta) + _XO + 0.5)
    ny = math.floor(ro - ra * math.cos(theta) + _YO + 0.5)
    return int(nx), int(ny)


def latest_release(now: datetime = None) -> tuple:
    """가장 최근 발표된 base_date, base_time (발표 후 10분 여유)을 반환."""
    now = now or datetime.now()
    candidate = now - timedelta(minutes=10)
    today_times = [t for t in _RELEASE_TIMES if t <= candidate.strftime("%H%M")]
    if today_times:
        return candidate.strftime("%Y%m%d"), today_times[-1]
    yesterday = candidate - timedelta(days=1)
    return yesterday.strftime("%Y%m%d"), _RELEASE_TIMES[-1]


def get_short_term_forecast(lat: float, lon: float, base_date: str = None, base_time: str = None) -> dict:
    api_key = os.environ.get("KMA_API_KEY")
    if not api_key:
        raise RuntimeError("KMA_API_KEY 환경변수가 설정되지 않았습니다.")

    nx, ny = latlon_to_grid(lat, lon)
    if not base_date or not base_time:
        base_date, base_time = latest_release()

    params = {
        "authKey": api_key,
        "numOfRows": 1000,
        "pageNo": 1,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny,
    }
    resp = requests.get(FORECAST_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()
