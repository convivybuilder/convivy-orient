"""Unit tests for tailnet.json config reading + the whois delegation
(CQ·150, E21)."""
import json

import tailnet_identity as ti


def test_missing_file_disables(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "TAILNET_CONFIG", tmp_path / "does-not-exist.json")
    cfg = ti.tailnet_config()
    assert cfg["enabled"] is False


def test_malformed_file_disables(tmp_path, monkeypatch):
    f = tmp_path / "tailnet.json"
    f.write_text("not json")
    monkeypatch.setattr(ti, "TAILNET_CONFIG", f)
    assert ti.tailnet_config()["enabled"] is False


def test_enabled_with_a_bind_reads_through(tmp_path, monkeypatch):
    f = tmp_path / "tailnet.json"
    f.write_text(json.dumps({"enabled": True, "bind": "192.0.2.9", "port": 9100}))
    monkeypatch.setattr(ti, "TAILNET_CONFIG", f)
    assert ti.tailnet_config() == {"enabled": True, "bind": "192.0.2.9", "port": 9100}


def test_enabled_without_a_bind_stays_disabled(tmp_path, monkeypatch):
    """"enabled": true with a blank bind is not something to listen on —
    fail closed, same direction as access_identity.jwt_config's own
    missing-file default."""
    f = tmp_path / "tailnet.json"
    f.write_text(json.dumps({"enabled": True, "bind": ""}))
    monkeypatch.setattr(ti, "TAILNET_CONFIG", f)
    assert ti.tailnet_config()["enabled"] is False


def test_port_defaults_when_omitted(tmp_path, monkeypatch):
    f = tmp_path / "tailnet.json"
    f.write_text(json.dumps({"enabled": True, "bind": "192.0.2.9"}))
    monkeypatch.setattr(ti, "TAILNET_CONFIG", f)
    assert ti.tailnet_config()["port"] == ti.DEFAULT_PORT


def test_login_for_peer_delegates_to_tailscale_whois(monkeypatch):
    seen = {}

    def fake_whois(host, port):
        seen["args"] = (host, port)
        return "person-a@example.com"

    monkeypatch.setattr(ti.tailscale_whois, "whois", fake_whois)
    assert ti.login_for_peer("192.0.2.1", 555) == "person-a@example.com"
    assert seen["args"] == ("192.0.2.1", 555)
