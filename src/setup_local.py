"""팀원 PC를 기준 환경과 똑같이 맞춘다. 한 번만 실행하면 된다.

    python -m src.setup_local

하는 일 (이미 된 단계는 건너뛴다):
  1. 파이썬 버전 확인 (3.10 이상)
  2. 패키지 설치: requirements.txt + 지도 변환용 osmium
  3. .env 준비: 없으면 .env.example을 복사하고, 비어 있는 키를 알려준다 (키 값은 팀 채널에서 받는다)
  4. 지도 원본: 팀 기준 날짜로 고정한 한국 OSM 파일(약 290MB)을 받고 MD5로 같은 파일인지 확인
  5. 로컬 지형 DB: 없거나 기준과 다르면 다시 만든다 (약 8분, 결과 약 880MB)
  6. 인도 확인 기록: 저장소의 씨앗 파일로 시작
  7. 확인: 테스트를 돌리고, 지금 상태를 기준과 비교해 보여준다

같은 데이터·같은 코드로 만들기 때문에, 끝나면 같은 요청에 같은 코스가 나온다.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _step(title: str) -> None:
    print(f"\n▶ {title}", flush=True)


def _pip(*args) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *args], check=True, cwd=ROOT)


def main() -> int:
    os.chdir(ROOT)
    problems = []

    _step("파이썬 버전")
    if sys.version_info < (3, 10):
        print(f"  파이썬 {sys.version.split()[0]} — 3.10 이상이 필요합니다. 새 파이썬으로 가상환경을 다시 만드세요.")
        return 1
    print(f"  {sys.version.split()[0]} 확인")

    _step("패키지 설치")
    _pip("-r", "requirements.txt")
    _pip("-r", "requirements-map.txt")
    print("  requirements.txt, requirements-map.txt 설치 완료")

    _step(".env (API 키)")
    from dotenv import dotenv_values

    # 키는 .env나 시스템 환경변수 중 어디에 있어도 된다 (서버도 둘 다 읽는다)
    keys = {key: os.environ.get(key) for key in ("TMAP_APP_KEY", "KMA_API_KEY")}
    if os.path.exists(".env"):
        keys = {key: value or dotenv_values(".env").get(key) for key, value in keys.items()}
    elif not all(keys.values()):
        shutil.copy(".env.example", ".env")
        print("  .env를 만들었습니다.")
    for key, value in keys.items():
        if value:
            print(f"  {key} 있음")
        else:
            print(f"  {key} 비어 있음 — 팀 채널에서 값을 받아 .env에 넣으세요 (저장소에는 올리지 않습니다)")
            problems.append(f"{key} 없음")

    from src.data_collection import local_osm
    from src.recommend import sidewalk

    _step("지도 원본 (한국 OSM, 팀 기준 날짜)")
    pbf = local_osm.PBF_PATH
    if os.path.exists(pbf) and local_osm.file_md5(pbf) == local_osm.PBF_MD5:
        print("  이미 있음 (기준 파일과 같음)")
    else:
        if os.path.exists(pbf):
            print("  있는 파일이 기준과 달라 다시 받습니다.")
            os.remove(pbf)
        local_osm.download(pbf)
        print("  받음 (기준 파일과 같음)")

    _step("로컬 지형 DB")
    info = local_osm.info()
    if info and info["matches_team"]:
        print(f"  이미 기준과 같음 (요소 {info['elements']:,}개)")
    else:
        # 없거나, 다른 날짜의 지도나 예전 코드로 만들었거나, 어떻게 만들었는지 기록이 없는 DB → 다시 만든다
        print("  만드는 중… (약 8분)")
        local_osm.build(pbf, local_osm.DB_PATH)

    _step("인도 확인 기록")
    print(f"  {len(sidewalk.load_facts()):,}개 길의 인도 여부를 알고 시작합니다.")

    _step("확인")
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    summary = (tests.stdout.strip().splitlines() or ["(결과 없음)"])[-1]
    print(f"  테스트: {summary}")
    if tests.returncode != 0:
        problems.append("테스트 실패")
    info = local_osm.info()
    print(f"  지형 DB: 요소 {info['elements']:,}개 · 기준 버전 {info['build_version']} · "
          f"{'기준과 같음' if info['matches_team'] else '기준과 다름'}")
    if not info["matches_team"]:
        problems.append("지형 DB가 기준과 다름")

    print("\n" + ("✔ 기준 환경과 같습니다. 서버: python -m uvicorn src.api.main:app" if not problems
                  else "남은 일: " + ", ".join(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
