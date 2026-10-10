"""Cloudflare Access identity + tier authorization for convivy-orient.

The trust boundary of the whole site, extracted from server.py (WS0.3, ARCH-348)
so the read server and the coming FastAPI control plane share ONE verification
path rather than two. The code below moved verbatim from server.py; behavior is
unchanged.

Identity comes from one of two places, controlled by jwt.json["enforce"]:

- **enforce = false (monitor):** trust the `Cf-Access-Authenticated-User-Email`
  header. Any JWT present is still verified, observe-only.
- **enforce = true (hardened, ARCH-056 follow-up):** trust ONLY a cryptographically
  verified `Cf-Access-Jwt-Assertion` — full RS256 signature check against the team
  JWKS, plus iss / exp / nbf / aud. The plaintext email header is not trusted.

A missing, unreadable or malformed jwt.json is not "monitor" — `jwt_config()`
returns `enforce=True` with an empty team_domain, so every request is refused
until a real config is in place. Defaulting a read failure to monitor mode
would silently trust an unverified header on the exact path meant to require
verification (issue_01M1F1H8RAQD2JGWQGDGW10M0B).

Tier rule (`tier_for`): a verified email in tiers.json["full"] → full bundle;
every other authenticated email → contractor bundle (least privilege by default).
tiers.json and jwt.json are re-read per request, so adding someone to "full" or
flipping enforce takes effect without a restart.

Pure stdlib on purpose (no cryptography dependency): the RSASSA-PKCS1-v1_5 verify
is hand-rolled per RFC 8017. To exercise the identity path in a test, monkeypatch
the symbol on THIS module (e.g. `access_identity._jwks_key`, `access_identity.TIERS`)
— server.py re-exports the callables for direct use, but the functions resolve
their module globals here, so a patch must target this module.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.request

from paths import CONFIG_ROOT

TIERS = CONFIG_ROOT / "tiers.json"
JWT_CONFIG = CONFIG_ROOT / "jwt.json"
EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"
JWT_HEADER = "Cf-Access-Jwt-Assertion"

# DER prefix for the SHA-256 DigestInfo in EMSA-PKCS1-v1_5 (RFC 8017 §9.2).
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")

# Cached JWKS keys: {kid: (n, e)} for the configured team certs URL.
_jwks_cache: dict = {"fetched": 0.0, "url": "", "keys": {}}


def full_emails() -> set[str]:
    try:
        return {e.strip().lower() for e in json.loads(TIERS.read_text()).get("full", [])}
    except Exception:
        return set()


def tier_for(email: str) -> str:
    """The tier decision: a verified email in tiers.json["full"] gets the full
    bundle, everyone else the contractor bundle (least privilege by default)."""
    return "full" if email in full_emails() else "contractor"


def jwt_config() -> dict:
    try:
        c = json.loads(JWT_CONFIG.read_text())
    except Exception:
        # A missing, unreadable or malformed jwt.json must fail CLOSED:
        # enforce=True, not the field's normal False default. enforce=False
        # makes _identity() trust the plaintext email header with no
        # cryptographic check at all, so a config read failure would
        # otherwise silently drop into the weaker of the two modes
        # (issue_01M1F1H8RAQD2JGWQGDGW10M0B). team_domain empty means
        # verify_access_jwt() always fails, so this also refuses outright
        # rather than accepting some other team's tokens.
        return {"team_domain": "", "aud": "", "enforce": True}
    return {
        "team_domain": (c.get("team_domain") or "").strip(),
        "aud": (c.get("aud") or "").strip(),
        "enforce": bool(c.get("enforce", False)),
    }


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _jwks_key(team_domain: str, kid: str) -> tuple[int, int] | None:
    """Return (n, e) for kid from the team JWKS, fetching/refreshing as needed.
    Keys are cached 10 min; a cache miss on kid forces a refetch (key rotation)."""
    url = f"https://{team_domain}/cdn-cgi/access/certs"
    stale = (time.time() - _jwks_cache["fetched"] > 600) or _jwks_cache["url"] != url
    if stale or kid not in _jwks_cache["keys"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                doc = json.loads(r.read())
            keys = {}
            for k in doc.get("keys", []):
                if k.get("kty") == "RSA" and k.get("kid"):
                    keys[k["kid"]] = (
                        int.from_bytes(_b64url(k["n"]), "big"),
                        int.from_bytes(_b64url(k["e"]), "big"),
                    )
            _jwks_cache.update(fetched=time.time(), url=url, keys=keys)
        except Exception:
            pass  # keep any stale cache; verification just fails closed below
    return _jwks_cache["keys"].get(kid)


def _rsa_pkcs1_verify(n: int, e: int, msg: bytes, sig: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verify with SHA-256, pure stdlib (RFC 8017 §8.2.2)."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_DIGESTINFO + hashlib.sha256(msg).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def verify_access_jwt(token: str, team_domain: str, aud: str) -> tuple[bool, dict, str]:
    """Verify a Cloudflare Access JWT. Returns (ok, claims, reason). Checks the
    RS256 signature against the team JWKS, iss, and exp/nbf (60s leeway); checks
    aud only when one is configured (empty aud during the monitor phase)."""
    if not team_domain:
        return False, {}, "no-team-domain"
    parts = token.split(".")
    if len(parts) != 3:
        return False, {}, "malformed"
    h_b64, p_b64, s_b64 = parts
    try:
        header = json.loads(_b64url(h_b64))
        claims = json.loads(_b64url(p_b64))
        sig = _b64url(s_b64)
    except Exception:
        return False, {}, "undecodable"
    if header.get("alg") != "RS256":
        return False, claims, f"alg={header.get('alg')}"
    key = _jwks_key(team_domain, header.get("kid", ""))
    if not key:
        return False, claims, "unknown-kid"
    if not _rsa_pkcs1_verify(key[0], key[1], f"{h_b64}.{p_b64}".encode("ascii"), sig):
        return False, claims, "bad-signature"
    now = time.time()
    if claims.get("iss") != f"https://{team_domain}":
        return False, claims, "bad-iss"
    if now > float(claims.get("exp", 0)) + 60:
        return False, claims, "expired"
    if now < float(claims.get("nbf", 0)) - 60:
        return False, claims, "not-yet-valid"
    if aud:
        token_aud = claims.get("aud", [])
        token_aud = [token_aud] if isinstance(token_aud, str) else token_aud
        if aud not in token_aud:
            return False, claims, "bad-aud"
    return True, claims, "ok"
