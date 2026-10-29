"""Small stdlib-only client for Keycloak tokens and gateway calls."""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import personas as P

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:4300")
KEYCLOAK = os.environ.get("KEYCLOAK_URL", "http://localhost:8380")
TOKEN_URL = f"{KEYCLOAK}/realms/{P.KEYCLOAK_REALM}/protocol/openid-connect/token"
ADMIN = os.environ.get("AGW_ADMIN_URL", "http://localhost:15300")

_cache = {}
_lock = threading.Lock()


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def token(client_id, username=None):
    """Access token for a person on a client (password grant) or a service app (client credentials)."""
    key = (client_id, username)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] - time.time() > 30:
            return hit[0]
    if username:
        body = {"grant_type": "password", "client_id": client_id, "username": username,
                "password": P.PASSWORD, "scope": "openid"}
    else:
        body = {"grant_type": "client_credentials", "client_id": client_id,
                "client_secret": f"demo-secret-{client_id}"}
    tok = _post_form(TOKEN_URL, body)
    with _lock:
        _cache[key] = (tok["access_token"], time.time() + tok.get("expires_in", 300))
    return tok["access_token"]


def forget(client_id, username=None):
    """Drop a cached token, for example after Keycloak restarted with new signing keys."""
    with _lock:
        _cache.pop((client_id, username), None)


def claims(jwt):
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def call(jwt, model, input_tokens, output_tokens, prompt="Summarize the attached claim notes.", real=False,
         user_agent=P.HUMAN_UA):
    """POST /v1/messages through the gateway. Returns (status, body dict, seconds)."""
    body = {"model": model, "max_tokens": max(64, output_tokens * 2),
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
               "authorization": f"Bearer {jwt}", "user-agent": user_agent}
    if real:
        headers["x-demo-backend"] = "anthropic"
    else:
        headers["x-mock-input-tokens"] = str(input_tokens)
        headers["x-mock-output-tokens"] = str(output_tokens)
    req = urllib.request.Request(f"{GATEWAY}/v1/messages", data=json.dumps(body).encode(), headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}"), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw.decode(errors="replace")[:300]}
        if e.headers.get("retry-after"):
            payload["retry_after"] = e.headers["retry-after"]
        return e.code, payload, time.time() - t0
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"error": str(e)}, time.time() - t0
