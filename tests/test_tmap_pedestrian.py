from src.api_clients import tmap_pedestrian


def test_get_route_sends_waypoints_as_tmap_pass_list(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": []}

    def post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv("TMAP_APP_KEY", "test-key")
    monkeypatch.setattr(tmap_pedestrian.requests, "post", post)

    tmap_pedestrian.get_route(
        (127.0, 34.0), (127.0, 34.0),
        waypoints=[(127.001, 34.001), (127.002, 34.002)],
    )

    assert captured["json"]["passList"] == "127.001,34.001_127.002,34.002"