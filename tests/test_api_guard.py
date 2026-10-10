"""api_common.api_guard: an unhandled error answers a terse 500 and leaves a
traceback in stderr (which serve.sh sends to serve.log).

It used to log nothing, so a failed save showed up only as a status code in
access.log and its cause had to be reconstructed by hand (2026-09-18)."""
from __future__ import annotations

import sqlite3

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

import api_common


def _app() -> TestClient:
    app = FastAPI()

    @app.post("/boom/{tail:path}")
    @api_common.api_guard
    async def boom(tail: str, request: Request):
        raise RuntimeError("secret-cause-9271")

    @app.post("/db/{tail:path}")
    @api_common.api_guard
    async def db(tail: str, request: Request):
        raise DBAPIError(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES (?, ?, ?)",
            ("https://fcm.example/ENDPOINT-TOKEN", "P256DH-KEY", "AUTHSECRET-xyz"),
            sqlite3.OperationalError("database is locked"))

    @app.post("/multiline/{tail:path}")
    @api_common.api_guard
    async def multiline(tail: str, request: Request):
        raise RuntimeError("first\nforged-log-line\u2028second")

    @app.get("/fine")
    @api_common.api_guard
    async def fine(request: Request):
        return {"ok": True}

    return TestClient(app)


def test_a_raising_handler_is_a_500_and_a_logged_traceback(capsys):
    r = _app().post("/boom/some-doc")

    assert r.status_code == 500
    assert r.json() == {"error": "internal"}
    assert "secret-cause-9271" not in r.text, "the cause belongs in the log, not the response"

    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: secret-cause-9271" in err
    assert "POST /boom/some-doc" in err, "the log must name the request that failed"


def test_a_handler_that_returns_logs_nothing(capsys):
    r = _app().get("/fine")
    assert r.status_code == 200
    assert capsys.readouterr().err == ""


def test_a_path_cannot_forge_a_log_line(capsys):
    """The path is caller-supplied; an encoded line separator in it must not
    start a new line in serve.log. U+2028 because it is one a line-oriented
    reader splits on and that survives URL parsing: `urlsplit` strips CR and
    LF itself, and the route's `.*` does not match LF."""
    r = _app().post("/boom/x%E2%80%A8forged-line-marker")
    assert r.status_code == 500

    lines = capsys.readouterr().err.splitlines()
    assert any("forged-line-marker" in ln for ln in lines)
    assert not any(ln.startswith("forged-line-marker") for ln in lines)


def test_a_failure_while_logging_does_not_change_the_response(monkeypatch):
    def broken(*a, **k):
        raise OSError("stderr is closed")

    monkeypatch.setattr(api_common.traceback, "format_tb", broken)
    r = _app().post("/boom/some-doc")
    assert r.status_code == 500
    assert r.json() == {"error": "internal"}


def test_a_database_error_does_not_put_its_bound_parameters_in_the_log(capsys):
    """`str()` of a SQLAlchemy `DBAPIError` appends `[SQL: ...]` and
    `[parameters: ...]`, which is where a push subscription's auth secret or a
    blog draft would come from. Only the driver's own message may be logged."""
    r = _app().post("/db/x")
    assert r.status_code == 500

    err = capsys.readouterr().err
    assert "OperationalError: database is locked" in err, "the cause must still be logged"
    for leaked in ("AUTHSECRET-xyz", "ENDPOINT-TOKEN", "P256DH-KEY", "[parameters:",
                   "push_subscriptions"):
        assert leaked not in err, f"{leaked!r} reached the log"


def test_an_exception_message_cannot_forge_a_log_line(capsys):
    r = _app().post("/multiline/x")
    assert r.status_code == 500

    lines = capsys.readouterr().err.splitlines()
    assert any("forged-log-line" in ln for ln in lines)
    assert not any(ln.startswith("forged-log-line") or ln.startswith("second")
                   for ln in lines)


def test_a_long_exception_message_is_cut_to_500_characters(capsys):
    app = FastAPI()

    @app.get("/long")
    @api_common.api_guard
    async def long(request: Request):
        raise RuntimeError("x" * 5000)

    TestClient(app).get("/long")
    err = capsys.readouterr().err
    assert "x" * 500 in err and "x" * 501 not in err
