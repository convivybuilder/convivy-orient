"""Unit tests for the Tailscale whois client (CQ·150, E21).

The `whois` tests stub `_request`, and the `_request` tests stub
`subprocess.run`, so none of these runs the real `tailscale` CLI. The answer
shapes are the ones `tailscale whois --json` gave on the production host on 2026-10-15."""
import json
import subprocess

import tailscale_whois as whois_mod


def _body(login: str | None, tags: list[str] | None = None) -> bytes:
    profile = {"LoginName": login} if login is not None else {}
    return json.dumps({"Node": {"Tags": tags}, "UserProfile": profile}).encode()


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(whois_mod, "_cache", {})


def test_whois_returns_the_login_the_daemon_reports(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(whois_mod, "_request", lambda addr, **kw: _body("person-a@example.com"))
    assert whois_mod.whois("192.0.2.1", 54321) == "person-a@example.com"


def test_whois_returns_none_for_a_tagged_node_whatever_its_login_reads(monkeypatch):
    """The host's own answer: a tagged node's `LoginName` is the machine's own
    name, not a sentinel. Taken as an identity it would get the contractor
    tier, so the tags decide."""
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(whois_mod, "_request",
                        lambda addr, **kw: _body("host-a.example.test", ["tag:apps"]))
    assert whois_mod.whois("192.0.2.2", 54321) is None


def test_whois_returns_none_for_the_tagged_devices_login(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(whois_mod, "_request", lambda addr, **kw: _body("tagged-devices"))
    assert whois_mod.whois("192.0.2.9", 54321) is None


def test_whois_returns_none_when_the_daemon_cannot_answer(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(whois_mod, "_request", lambda addr, **kw: None)
    assert whois_mod.whois("192.0.2.3", 54321) is None


def test_whois_returns_none_when_the_peer_has_no_profile(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(whois_mod, "_request", lambda addr, **kw: json.dumps({}).encode())
    assert whois_mod.whois("192.0.2.4", 54321) is None


def test_whois_returns_none_for_a_malformed_body(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(whois_mod, "_request", lambda addr, **kw: b"not json")
    assert whois_mod.whois("192.0.2.5", 54321) is None


def test_whois_caches_per_address_across_connection_ports(monkeypatch):
    """Each connection arrives on a new port, so the cache is keyed on the
    address alone, or it would never hit."""
    _fresh_cache(monkeypatch)
    calls = []

    def fake_request(addr, **kw):
        calls.append(addr)
        return _body("person-a@example.com")

    monkeypatch.setattr(whois_mod, "_request", fake_request)
    assert whois_mod.whois("192.0.2.6", 50001) == "person-a@example.com"
    assert whois_mod.whois("192.0.2.6", 50002) == "person-a@example.com"
    assert calls == ["192.0.2.6"]


def test_whois_does_not_share_a_cache_entry_across_different_peers(monkeypatch):
    _fresh_cache(monkeypatch)

    def fake_request(addr, **kw):
        return _body("person-a@example.com") if addr == "192.0.2.7" else _body("teammate@example.com")

    monkeypatch.setattr(whois_mod, "_request", fake_request)
    assert whois_mod.whois("192.0.2.7", 1) == "person-a@example.com"
    assert whois_mod.whois("192.0.2.8", 1) == "teammate@example.com"


# --- _request: the CLI call itself ---------------------------------------

def test_request_runs_the_cli_as_an_argv_list_with_a_minimal_env(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"], seen["timeout"] = cmd, kw["env"], kw["timeout"]
        return subprocess.CompletedProcess(cmd, 0, stdout=_body("person-a@example.com"), stderr=b"")

    monkeypatch.setattr(whois_mod.paths, "resolve_tool", lambda name: "/usr/local/bin/tailscale")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    monkeypatch.setattr(whois_mod.subprocess, "run", fake_run)
    assert whois_mod._request("192.0.2.1", timeout=2.0) == _body("person-a@example.com")
    assert seen["cmd"] == ["/usr/local/bin/tailscale", "whois", "--json", "192.0.2.1"]
    assert set(seen["env"]) <= {"HOME", "PATH"}
    assert seen["timeout"] == 2.0


def test_request_returns_none_when_the_peer_is_not_found(monkeypatch):
    monkeypatch.setattr(whois_mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout=b"", stderr=b"peer not found\n"))
    assert whois_mod._request("192.0.2.254", timeout=2.0) is None


def test_request_returns_none_when_the_cli_times_out_or_is_missing(monkeypatch):
    def timed_out(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(whois_mod.subprocess, "run", timed_out)
    assert whois_mod._request("192.0.2.1", timeout=2.0) is None

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(whois_mod.subprocess, "run", missing)
    assert whois_mod._request("192.0.2.1", timeout=2.0) is None
