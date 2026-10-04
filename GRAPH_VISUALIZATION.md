# GraphML 지도 시각화 (용봉동 파일럿)

## 원칙

- 일반 사용자 지도에는 추천된 코스만 굵은 파란 선으로 표시한다.
- GraphML 전체 도로망과 횡단보도는 검증·운영자용 레이어이며 기본적으로 숨긴다.
- 브라우저에는 GraphML을 직접 보내지 않는다. 배포 전 GraphML을 GeoJSON으로 변환해 두고, 현재 지도 영역(`bbox`)만 `/map/graph`에서 반환한다.

## 포함 파일

```text
data/graph/yongbong.geojson  # 용봉동 GraphML에서 변환한 road_edge/crossing GeoJSON
src/map_view/graph_geojson.py # GeoJSON 캐시·bbox 필터
GET /map/graph                # 검증 레이어 API
```

## 사용 방법

서버를 실행한 뒤 아래처럼 `graph_debug=1`을 붙여 접속합니다.

```text
http://127.0.0.1:8000/?graph_debug=1
```

코스를 선택한 뒤 안내 모드로 들어가면, 우측의 `⌘` 버튼으로 현재 지도 영역의 GraphML 도로망과 횡단보도를 켜고 끌 수 있습니다. 일반 접속에서는 해당 버튼도, 그래프 데이터 요청도 없습니다.

## 운영 배포

다른 지역 GraphML을 사용할 때는 해당 GraphML을 같은 스키마의 GeoJSON으로 변환하고 환경변수를 지정합니다.

```text
GRAPH_GEOJSON_PATH=/app/data/graph/지역명.geojson
```

전국 단위에서는 단일 GeoJSON을 제공하지 않습니다. 주변 타일만 읽는 bbox API 또는 벡터 타일(MVT) 방식으로 바꿔야 합니다.
