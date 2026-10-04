"""로그인. 취향 데이터는 본인만 볼 수 있어야 한다.

그동안은 user_id만 알면 누구든 그 사람의 취향을 조회하고 지울 수 있었다. 계정을 만들면 그 계정의
user_id가 붙은 요청은 로그인 토큰이 있어야 통과한다.

- 비밀번호는 저장하지 않는다. 계정마다 다른 소금(salt)을 넣어 scrypt로 해시한 값만 둔다.
  DB가 유출돼도 비밀번호를 되돌릴 수 없고, 같은 비밀번호를 쓰는 두 사람의 해시가 다르다.
- 로그인하면 무작위 토큰을 준다. DB에는 토큰의 SHA-256만 둔다(DB를 본 사람이 토큰을 쓸 수 없게).
  30일 뒤 만료되고, 로그아웃하면 바로 지워진다.
- 아이디가 없을 때와 비밀번호가 틀렸을 때 같은 답을 준다(어떤 아이디가 있는지 알려주지 않는다).
- 같은 아이디로 5분 안에 5번 틀리면 잠시 막는다(무작위 대입 방지).

계정 없이 쓰는 사람(손님)은 예전처럼 기기에서 만든 임의의 id로 쓴다. 가입할 때 그 id를 같이 주면
그동안 쌓인 취향·이력·즐겨찾기가 계정으로 옮겨진다.

실제 서비스에서는 HTTPS로만 써야 한다 — 평문 HTTP에서는 비밀번호와 토큰이 그대로 지나간다.
"""
import hashlib
import hmac
import re
import secrets
import time

from . import user_store

SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD = 8
USERNAME_RULE = re.compile(r"^[A-Za-z0-9_가-힣]{2,20}$")
MAX_FAILS = 5
FAIL_WINDOW_SECONDS = 300
_fails = {}   # 아이디 -> 최근 실패 시각들 (서버 메모리)

SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    user_store.add_session(_hash_token(token), user_id, time.time() + SESSION_SECONDS)
    return token


def sign_up(username: str, password: str, guest_id: str = None) -> dict:
    """계정을 만들고 바로 로그인한 상태로 돌려준다 → {user_id, username, token}."""
    username = (username or "").strip()
    if not USERNAME_RULE.match(username):
        raise AuthError(422, "아이디는 2~20자의 한글·영문·숫자·밑줄만 쓸 수 있습니다.")
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(422, f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
    if guest_id and user_store.is_account(guest_id):
        raise AuthError(403, "다른 계정의 데이터는 옮길 수 없습니다.")

    salt = secrets.token_bytes(16)
    user_id = "acct-" + secrets.token_hex(8)
    if not user_store.add_account(user_id, username, salt.hex(), _hash_password(password, salt).hex()):
        raise AuthError(409, "이미 쓰고 있는 아이디입니다.")
    if guest_id:
        # 가입 전에 손님으로 쌓은 취향·이력·즐겨찾기를 계정으로 옮긴다
        user_store.move_user_data(guest_id, user_id)
    return {"user_id": user_id, "username": username, "token": _new_session(user_id)}


def log_in(username: str, password: str) -> dict:
    username = (username or "").strip()
    now = time.time()
    recent = [t for t in _fails.get(username, []) if now - t < FAIL_WINDOW_SECONDS]
    if len(recent) >= MAX_FAILS:
        raise AuthError(429, "로그인 시도가 너무 많습니다. 5분 뒤에 다시 시도해 주세요.")

    account = user_store.account_by_username(username)
    # 없는 아이디여도 해시 계산은 한다 — 응답 시간으로 아이디가 있는지 알아낼 수 없게
    salt = bytes.fromhex(account["salt"]) if account else b"\\0" * 16
    given = _hash_password(password or "", salt)
    if account is None or not hmac.compare_digest(given, bytes.fromhex(account["password_hash"])):
        _fails[username] = recent + [now]
        raise AuthError(401, "아이디 또는 비밀번호가 맞지 않습니다.")
    _fails.pop(username, None)
    return {"user_id": account["user_id"], "username": account["username"], "token": _new_session(account["user_id"])}


def _token_from(authorization: str):
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


def current_user(authorization: str):
    """Authorization 헤더의 토큰이 가리키는 계정 {user_id, username}. 없거나 만료됐으면 None."""
    token = _token_from(authorization)
    if token is None:
        return None
    user_id = user_store.session_user(_hash_token(token), time.time())
    if user_id is None:
        return None
    account = user_store.account_by_id(user_id)
    return {"user_id": user_id, "username": account["username"]} if account else None


def log_out(authorization: str) -> bool:
    token = _token_from(authorization)
    return bool(token) and user_store.remove_session(_hash_token(token))


def require_owner(user_id: str, authorization: str) -> None:
    """user_id가 계정이면, 그 계정으로 로그인한 요청만 통과시킨다. 손님 id는 예전처럼 그대로 통과한다."""
    if not user_id or not user_store.is_account(user_id):
        return
    user = current_user(authorization)
    if user is None:
        raise AuthError(401, "로그인이 필요합니다.")
    if user["user_id"] != user_id:
        raise AuthError(403, "다른 사람의 데이터에는 접근할 수 없습니다.")
