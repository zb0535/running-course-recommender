"""로그인: 취향 데이터는 본인만 볼 수 있다."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.recommend import auth, user_store

client = TestClient(main.app)

COURSE = {"id": "sea", "name": "해안길", "region": "여수", "path": [[34.0, 127.0], [34.03, 127.0]],
          "distance_km": 5.0, "elevation_gain_m": 60, "tags": ["바다뷰"], "traffic_signal_count": 0, "safety_score": 0.8}
PASSWORD = "test-pass-1234"   # 테스트 전용 값


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(main, "load_courses", lambda: [COURSE])
    monkeypatch.setattr(auth, "_fails", {})
    monkeypatch.setattr(auth, "SCRYPT", {"n": 2 ** 4, "r": 8, "p": 1, "dklen": 32})   # 테스트에서는 빠르게


def _signup(username="runner", **extra):
    return client.post("/auth/signup", json={"username": username, "password": PASSWORD, **extra})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _recommend(user_id, headers=None):
    return client.post("/recommend", headers=headers or {}, json={
        "user_id": user_id, "preferred_distance_km": 5, "route_type": "oneway", "top_n": 1,
        "use_live_environment": False, "time_of_day": "morning"})


class TestAccounts:
    def test_signup_logs_in_and_tells_who_i_am(self):
        account = _signup().json()
        assert account["username"] == "runner" and account["user_id"].startswith("acct-")
        me = client.get("/auth/me", headers=_bearer(account["token"])).json()
        assert me == {"user_id": account["user_id"], "username": "runner"}

    def test_login_with_the_right_password(self):
        user_id = _signup().json()["user_id"]
        resp = client.post("/auth/login", json={"username": "runner", "password": PASSWORD})
        assert resp.status_code == 200 and resp.json()["user_id"] == user_id

    def test_wrong_password_and_unknown_user_get_the_same_answer(self):
        """어떤 아이디가 있는지 알려주지 않는다."""
        _signup()
        wrong = client.post("/auth/login", json={"username": "runner", "password": "wrong-password"})
        nobody = client.post("/auth/login", json={"username": "ghost", "password": PASSWORD})
        assert wrong.status_code == nobody.status_code == 401
        assert wrong.json() == nobody.json()

    def test_duplicate_username_is_refused_regardless_of_case(self):
        _signup("Runner")
        assert _signup("runner").status_code == 409

    def test_weak_password_and_odd_username_are_refused(self):
        assert client.post("/auth/signup", json={"username": "runner", "password": "short"}).status_code == 422
        assert client.post("/auth/signup", json={"username": "a b", "password": PASSWORD}).status_code == 422

    def test_repeated_failures_are_blocked_for_a_while(self):
        _signup()
        for _ in range(auth.MAX_FAILS):
            client.post("/auth/login", json={"username": "runner", "password": "wrong-password"})
        blocked = client.post("/auth/login", json={"username": "runner", "password": PASSWORD})
        assert blocked.status_code == 429

    def test_logout_ends_the_session(self):
        token = _signup().json()["token"]
        assert client.post("/auth/logout", headers=_bearer(token)).json() == {"logged_out": True}
        assert client.get("/auth/me", headers=_bearer(token)).status_code == 401

    def test_expired_session_is_not_accepted(self, monkeypatch):
        token = _signup().json()["token"]
        monkeypatch.setattr(auth.time, "time", lambda: 4102444800.0)   # 2100년
        assert client.get("/auth/me", headers=_bearer(token)).status_code == 401


class TestWhatIsStored:
    def test_password_and_token_are_never_stored_as_is(self):
        token = _signup().json()["token"]
        db = sqlite3.connect(user_store.DB_PATH)
        try:
            dump = " ".join(str(value) for table in ("accounts", "sessions")
                            for row in db.execute(f"SELECT * FROM {table}") for value in row)
        finally:
            db.close()
        assert PASSWORD not in dump and token not in dump

    def test_same_password_gives_different_hashes(self):
        _signup("one")
        _signup("two")
        hashes = {user_store.account_by_username(name)["password_hash"] for name in ("one", "two")}
        assert len(hashes) == 2


class TestOnlyTheOwnerSeesTheirData:
    @pytest.fixture
    def mine(self):
        account = _signup("me").json()
        headers = _bearer(account["token"])
        _recommend(account["user_id"], headers)
        client.post("/feedback", headers=headers, json={"user_id": account["user_id"], "course_id": "sea", "rating": 5,
                                                         "aspects": {"scenery": "good"}})
        return account, headers

    def test_owner_can_see_their_taste(self, mine):
        account, headers = mine
        taste = client.get(f"/users/{account['user_id']}/taste", headers=headers)
        assert taste.status_code == 200 and taste.json()["stats"]["feedback_count"] == 1

    def test_without_a_token_it_is_refused(self, mine):
        account, _ = mine
        user_id = account["user_id"]
        assert client.get(f"/users/{user_id}/taste").status_code == 401
        assert client.get(f"/profile/{user_id}").status_code == 401
        assert client.get(f"/favorites/{user_id}").status_code == 401
        assert client.delete(f"/users/{user_id}").status_code == 401
        assert _recommend(user_id).status_code == 401
        assert client.post("/feedback", json={"user_id": user_id, "course_id": "sea", "rating": 1}).status_code == 401
        assert client.post("/favorites", json={"user_id": user_id, "course_id": "sea"}).status_code == 401

    def test_someone_elses_token_is_refused(self, mine):
        account, _ = mine
        other = _bearer(_signup("other").json()["token"])
        assert client.get(f"/users/{account['user_id']}/taste", headers=other).status_code == 403
        assert client.delete(f"/users/{account['user_id']}", headers=other).status_code == 403

    def test_guest_ids_keep_working_without_login(self):
        """가입하지 않은 사람은 예전처럼 기기에서 만든 id로 쓴다."""
        assert _recommend("demo-abc123").status_code == 200
        assert client.get("/profile/demo-abc123").status_code == 200

    def test_reviews_show_a_name_not_the_internal_id(self, mine):
        review = client.get("/courses/sea/reviews").json()["reviews"][0]
        assert review["author"] == "me" and "user_id" not in review

    def test_deleting_the_account_removes_the_login_too(self, mine):
        account, headers = mine
        assert client.delete(f"/users/{account['user_id']}", headers=headers).status_code == 200
        assert client.get("/auth/me", headers=headers).status_code == 401
        assert client.post("/auth/login", json={"username": "me", "password": PASSWORD}).status_code == 401


class TestGuestDataMovesToTheAccount:
    def test_taste_learned_before_signing_up_is_kept(self):
        _recommend("demo-guest1")
        client.post("/feedback", json={"user_id": "demo-guest1", "course_id": "sea", "rating": 5,
                                       "aspects": {"stops": "many"}})
        client.post("/favorites", json={"user_id": "demo-guest1", "course_id": "sea"})

        account = _signup("me", guest_id="demo-guest1").json()
        headers = _bearer(account["token"])
        taste = client.get(f"/users/{account['user_id']}/taste", headers=headers).json()
        assert taste["stats"]["feedback_count"] == 1 and taste["stats"]["favorite_count"] == 1
        assert any("신호등" in line for line in taste["summary"])
        assert client.get("/users/demo-guest1/taste").status_code == 404   # 손님 쪽에는 남지 않는다

    def test_another_accounts_data_cannot_be_claimed(self):
        victim = _signup("victim").json()["user_id"]
        assert _signup("thief", guest_id=victim).status_code == 403

    def test_guest_profile_still_in_the_old_file_is_moved_and_does_not_reappear(self):
        import json

        from src.recommend import personalize

        old = personalize.new_profile("demo-old")
        old["n_feedback"] = 4
        with open(personalize.PROFILES_PATH, "w", encoding="utf-8") as f:
            json.dump({"demo-old": old}, f)
        account = _signup("me", guest_id="demo-old").json()
        taste = client.get(f"/users/{account['user_id']}/taste", headers=_bearer(account["token"])).json()
        assert taste["n_feedback"] == 4
        assert client.get("/users/demo-old/taste").status_code == 404
