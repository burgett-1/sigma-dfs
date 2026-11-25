from pipeline.sources import x_news


class Resp:
    def __init__(self, code, body, headers=None):
        self.status_code, self._b, self.headers, self.text = code, body, headers or {}, str(body)

    def json(self):
        return self._b


class Sess:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {}), headers))
        return self.replies.pop(0)


def test_new_posts_since_and_times(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    s = Sess([Resp(200, {"data": [{"id": "300", "text": "b", "created_at": "2026-11-25T22:01:05.000Z"},
                                  {"id": "200", "text": "a", "created_at": "2026-11-25T22:00:00.000Z"}]})])
    monkeypatch.setattr(x_news, "session", lambda: s)
    posts = x_news.new_posts("42", "100")
    assert [p["id"] for p in posts] == ["200", "300"]                 # oldest first
    assert posts[1]["at"] == "2026-11-25T22:01:05+00:00"
    url, params, headers = s.calls[0]
    assert url.endswith("/2/users/42/tweets") and params["since_id"] == "100"
    assert params["exclude"] == "retweets" and headers["Authorization"] == "Bearer t"


def test_fields_name_fallback(monkeypatch):
    monkeypatch.setattr(x_news, "_FIELDS_PARAM", ["post.fields"])
    s = Sess([Resp(400, {"errors": "unknown parameter post.fields"}),
              Resp(200, {"data": [{"id": "5", "text": "x", "created_at": "2026-11-25T22:00:00Z"}]})])
    monkeypatch.setattr(x_news, "session", lambda: s)
    assert x_news.new_posts("42", "1")[0]["id"] == "5"
    assert "tweet.fields" in s.calls[1][1] and "post.fields" not in s.calls[1][1]


def test_backoff(monkeypatch):
    import time
    assert x_news.backoff_until(x_news.XError(429, "", time.time() + 99)) > time.time() + 90
    assert x_news.backoff_until(x_news.XError(402, "no credits")) > time.time() + 500
