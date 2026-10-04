"""사용자별 데이터를 저장한다: 학습된 취향(프로필), 평가 이력, 별점·후기, 즐겨찾기.

취향 데이터는 그 사람이 직접 볼 수 있어야 한다 — 무엇을 답했고, 그래서 추천이 어떻게 바뀌었는지.
그래서 최신 상태(profiles)만이 아니라 평가할 때마다의 기록(feedback_log)을 남긴다. 그때의 가중치도
같이 남겨서 취향이 어떻게 변해 왔는지 보여줄 수 있다.

별점은 그동안 그 사람의 가중치 학습에만 쓰고 버렸다. 기록으로 남겨야 다른 사람에게 "이 코스는
평균 4.5점"을 보여줄 수 있고, 나중에 길 점수 학습의 정답으로도 쓸 수 있다.

즐겨찾기는 코스 내용을 통째로 같이 저장한다. 실시간으로 만든 코스는 DB에서 사라지거나 경로가
갱신될 수 있는데, 사용자가 저장한 건 그때 그 코스여야 한다.

SQLite 파일 하나(data/app.sqlite)에 둔다. 개인 데이터라 저장소에는 올리지 않는다.
"""
import json
import contextlib
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "app.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    user_id TEXT NOT NULL, course_id TEXT NOT NULL, rating INTEGER NOT NULL, comment TEXT,
    created_at REAL NOT NULL, aspects TEXT, PRIMARY KEY (user_id, course_id));
CREATE INDEX IF NOT EXISTS reviews_course ON reviews (course_id);
CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, salt TEXT NOT NULL,
    password_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feedback_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, course_id TEXT NOT NULL, course_name TEXT,
    labels TEXT, rating INTEGER NOT NULL, aspects TEXT, comment TEXT, weights TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS feedback_user ON feedback_log (user_id, created_at);
CREATE TABLE IF NOT EXISTS favorites (
    user_id TEXT NOT NULL, course_id TEXT NOT NULL, course TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY (user_id, course_id));
"""


@contextlib.contextmanager
def _open():
    """연결을 열어 쓰고, 저장한 뒤 반드시 닫는다 (sqlite3의 with는 닫지 않아서 파일이 잠긴 채 남는다)."""
    db = _connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(_SCHEMA)
    if "aspects" not in [row[1] for row in db.execute("PRAGMA table_info(reviews)")]:
        db.execute("ALTER TABLE reviews ADD COLUMN aspects TEXT")   # 예전에 만든 파일
    return db


def save_review(user_id: str, course_id: str, rating: int, comment: str = None, aspects: dict = None) -> None:
    """한 사람이 한 코스에 남기는 평가는 하나다. 다시 남기면 고쳐 쓴다(후기를 안 주면 이전 후기는 유지)."""
    with _open() as db:
        db.execute(
            "INSERT INTO reviews (user_id, course_id, rating, comment, created_at, aspects) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (user_id, course_id) DO UPDATE SET rating = excluded.rating, "
            "comment = COALESCE(excluded.comment, comment), aspects = COALESCE(excluded.aspects, aspects), "
            "created_at = excluded.created_at",
            (user_id, course_id, rating, (comment or "").strip() or None, time.time(),
             json.dumps(aspects, ensure_ascii=False) if aspects else None))


def reviews_for(course_id: str, limit: int = 20) -> dict:
    with _open() as db:
        average, count = db.execute(
            "SELECT AVG(rating), COUNT(*) FROM reviews WHERE course_id = ?", (course_id,)).fetchone()
        rows = db.execute(
            "SELECT user_id, rating, comment, created_at, aspects FROM reviews WHERE course_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (course_id, limit)).fetchall()
    return {
        "course_id": course_id,
        "average": round(average, 2) if count else None,
        "count": count,
        "reviews": [{"user_id": u, "rating": r, "comment": c, "created_at": int(t),
                     "aspects": json.loads(a) if a else None} for u, r, c, t, a in rows],
    }


def ratings(course_ids: list) -> dict:
    """{코스 id: {"average", "count"}} — 평가가 있는 코스만."""
    if not course_ids:
        return {}
    marks = ",".join("?" * len(course_ids))
    with _open() as db:
        rows = db.execute(
            f"SELECT course_id, AVG(rating), COUNT(*) FROM reviews WHERE course_id IN ({marks}) GROUP BY course_id",
            list(course_ids)).fetchall()
    return {cid: {"average": round(avg, 2), "count": count} for cid, avg, count in rows}


def add_favorite(user_id: str, course: dict) -> None:
    with _open() as db:
        db.execute("INSERT OR REPLACE INTO favorites VALUES (?,?,?,?)",
                   (user_id, course["id"], json.dumps(course, ensure_ascii=False), time.time()))


def remove_favorite(user_id: str, course_id: str) -> bool:
    with _open() as db:
        return db.execute("DELETE FROM favorites WHERE user_id = ? AND course_id = ?",
                          (user_id, course_id)).rowcount > 0


def favorites(user_id: str) -> list:
    with _open() as db:
        rows = db.execute("SELECT course, created_at FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
                          (user_id,)).fetchall()
    return [{"course": json.loads(course), "saved_at": int(saved)} for course, saved in rows]


def favorite_ids(user_id: str) -> set:
    with _open() as db:
        return {row[0] for row in db.execute("SELECT course_id FROM favorites WHERE user_id = ?", (user_id,))}


# ── 프로필(학습된 취향)과 평가 이력 ──

def get_profile(user_id: str):
    with _open() as db:
        row = db.execute("SELECT body FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    return json.loads(row[0]) if row else None


def put_profile(profile: dict) -> None:
    with _open() as db:
        db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?)",
                   (profile["user_id"], json.dumps(profile, ensure_ascii=False), time.time()))


def log_feedback(user_id: str, course_id: str, rating: int, aspects: dict = None, comment: str = None,
                 course_name: str = None, labels: list = None, weights: dict = None) -> None:
    """평가 한 번을 그대로 남긴다. reviews는 코스당 최신 하나만 갖지만, 여기는 전부 쌓인다."""
    with _open() as db:
        db.execute(
            "INSERT INTO feedback_log (user_id, course_id, course_name, labels, rating, aspects, comment, weights, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, course_id, course_name, json.dumps(labels or [], ensure_ascii=False), rating,
             json.dumps(aspects, ensure_ascii=False) if aspects else None, (comment or "").strip() or None,
             json.dumps(weights) if weights else None, time.time()))


def feedback_history(user_id: str, limit: int = 100) -> list:
    """최근 것부터."""
    with _open() as db:
        rows = db.execute(
            "SELECT course_id, course_name, labels, rating, aspects, comment, weights, created_at FROM feedback_log "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [{"course_id": cid, "course_name": name, "scenery_labels": json.loads(labels or "[]"), "rating": rating,
             "aspects": json.loads(aspects) if aspects else {}, "comment": comment,
             "weights": json.loads(weights) if weights else None, "created_at": int(created)}
            for cid, name, labels, rating, aspects, comment, weights, created in rows]


def delete_user(user_id: str) -> dict:
    """그 사람의 데이터를 전부 지운다 → 지운 개수."""
    removed = {}
    with _open() as db:
        for table in ("profiles", "feedback_log", "reviews", "favorites", "sessions", "accounts"):
            removed[table] = db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,)).rowcount
    return removed


# ── 계정과 로그인 세션 (비밀번호·토큰은 해시만 저장한다. 해시는 auth.py가 만든다) ──

def add_account(user_id: str, username: str, salt: str, password_hash: str) -> bool:
    """아이디가 이미 있으면 False."""
    try:
        with _open() as db:
            db.execute("INSERT INTO accounts VALUES (?,?,?,?,?)", (user_id, username, salt, password_hash, time.time()))
        return True
    except sqlite3.IntegrityError:
        return False


def _account(where: str, value: str):
    with _open() as db:
        row = db.execute(f"SELECT user_id, username, salt, password_hash FROM accounts WHERE {where} = ?",
                         (value,)).fetchone()
    return dict(zip(("user_id", "username", "salt", "password_hash"), row)) if row else None


def account_by_username(username: str):
    return _account("username", username)


def account_by_id(user_id: str):
    return _account("user_id", user_id)


def is_account(user_id: str) -> bool:
    return account_by_id(user_id) is not None


def usernames(user_ids: list) -> dict:
    if not user_ids:
        return {}
    marks = ",".join("?" * len(user_ids))
    with _open() as db:
        return dict(db.execute(f"SELECT user_id, username FROM accounts WHERE user_id IN ({marks})", list(user_ids)))


def add_session(token_hash: str, user_id: str, expires_at: float) -> None:
    with _open() as db:
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))   # 만료된 것은 이때 치운다
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash, user_id, expires_at))


def session_user(token_hash: str, now: float):
    with _open() as db:
        row = db.execute("SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
                         (token_hash, now)).fetchone()
    return row[0] if row else None


def remove_session(token_hash: str) -> bool:
    with _open() as db:
        return db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,)).rowcount > 0


def move_user_data(from_id: str, to_id: str) -> None:
    """손님으로 쌓은 데이터를 계정으로 옮긴다 (가입할 때)."""
    profile = get_profile(from_id)
    with _open() as db:
        if profile is not None:
            profile["user_id"] = to_id
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?)",
                       (to_id, json.dumps(profile, ensure_ascii=False), time.time()))
            db.execute("DELETE FROM profiles WHERE user_id = ?", (from_id,))
        db.execute("UPDATE feedback_log SET user_id = ? WHERE user_id = ?", (to_id, from_id))
        db.execute("UPDATE OR REPLACE reviews SET user_id = ? WHERE user_id = ?", (to_id, from_id))
        db.execute("UPDATE OR REPLACE favorites SET user_id = ? WHERE user_id = ?", (to_id, from_id))
