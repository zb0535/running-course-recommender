"""Kakao Mobility Directions API 클라이언트 (스텁 — REST API 키 필요).

키 발급: https://developers.kakao.com
발급 후 환경변수 KAKAO_REST_API_KEY 설정.
"""
import os

import requests

DIRECTIONS_URL = "https://apis-navi.kakaomobility.com/v1/directions"


def get_route(origin: tuple, destination: tuple) -> dict:
    """origin/destination = (lng, lat). 경로 좌표·거리 반환."""
    api_key = os.environ.get("KAKAO_REST_API_KEY")
    if not api_key:
        raise RuntimeError("KAKAO_REST_API_KEY 환경변수가 설정되지 않았습니다.")

    headers = {"Authorization": f"KakaoAK {api_key}"}
    params = {
        "origin": f"{origin[0]},{origin[1]}",
        "destination": f"{destination[0]},{destination[1]}",
    }
    resp = requests.get(DIRECTIONS_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()
