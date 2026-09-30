# 코스 후보 스키마

`data/courses.sample.json` 형식. 코스 1건 = 1 JSON object.

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | string | 코스 고유 ID |
| `name` | string | 코스 이름 |
| `region` | string | 지역 (예: "여수", "광주") |
| `distance_km` | number | 총 거리 (km) |
| `elevation_gain_m` | number | 누적 고도 상승 (m) |
| `safety_score` | number (0~1) | 가로등·CCTV·치안시설 밀도 기반 정규화 점수 |
| `traffic_signal_count` | int | 경로상 신호등/교차로 개수 |
| `green_ratio` | number (0~1) | 경로 주변 녹지(숲/공원) 비율 |
| `coastline_proximity` | number (0~1) | 해안선 근접도 (1 = 해안 바로 옆) |
| `surface` | string | 노면 상태: `paved` \| `unpaved` \| `mixed` |
| `tags` | string[] | 파생 태그: `숲길` \| `바다뷰` \| `도심` \| `차없는길` |
| `path` | [lat, lng][] | 경로 좌표 (지도 API 연동 후 채움) |
| `source` | string | 데이터 출처 (OSM / 수작업 큐레이션 등) |

## 코스 벡터 (추천 로직 입력)

`[distance_km, elevation_gain_m, safety_score, traffic_signal_count, green_ratio, coastline_proximity, surface]`

`src/recommend/score.py`의 `course_features()`가 위 스키마에서 이 벡터를 추출한다.
