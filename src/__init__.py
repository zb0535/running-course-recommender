"""프로젝트 루트의 .env를 자동으로 읽어 환경변수로 올린다.

API 키를 소스에 넣지 않으므로 각자 로컬에 .env를 두고 쓴다(.env는 .gitignore 대상).
서버든 수집 스크립트든 전부 src.* 를 거쳐 들어오므로 여기서 한 번만 로드하면 된다.
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
