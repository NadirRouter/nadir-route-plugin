"""Shared hook transport: authorize the origin before sending prompts or keys."""
import json
import os
from pathlib import Path
import pwd
import sys
import urllib.error
import urllib.parse
import urllib.request


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("URL credentials and fragments are forbidden")
    host = parsed.hostname
    if not host or parsed.scheme not in ("http", "https"):
        raise ValueError("Invalid routing origin")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote routing requires HTTPS")
    return parsed.scheme, host.lower(), port


def _user_settings():
    # Project settings may override HOME and NADIR_AUTH_ORIGIN. Read the OS
    # user's configuration directly instead of trusting either environment.
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        return json.loads((home / ".claude/settings.json").read_text()).get("env", {})
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def headers_for(endpoint):
    origin = _origin(endpoint)
    trusted = {("https", "api.getnadir.com", 443)}
    configured = _user_settings().get("NADIR_AUTH_ORIGIN")
    if configured:
        trusted.add(_origin(configured))
    headers = {"Content-Type": "application/json"}
    if origin not in trusted:
        if origin[1] in ("localhost", "127.0.0.1", "::1"):
            return headers  # local development gets no account credential
        raise ValueError("Routing endpoint is not a trusted user origin")
    key = os.environ.get("NADIR_API_KEY")
    if key:
        headers["X-API-Key"] = key
    return headers


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post(endpoint, data, session=None):
    headers = headers_for(endpoint)
    if data is None:
        headers.pop("X-API-Key", None)
    if isinstance(session, str) and 0 < len(session) <= 200:
        headers["X-Nadir-Session-Id"] = session
    req = urllib.request.Request(endpoint, data=data, headers=headers)
    try:
        timeout = float(os.environ.get("NADIR_TIMEOUT") or 5)
    except ValueError:
        timeout = 5
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            return response.status, response.read(2_000_000)
    except urllib.error.HTTPError as error:
        return error.code, error.read(2_000_000)


if __name__ == "__main__":
    try:
        status, body = post(sys.argv[1], sys.stdin.buffer.read(), session=sys.argv[2] if len(sys.argv) > 2 else None)
        sys.stdout.buffer.write(body + ("\nNADIR_HTTP_STATUS:%s" % status).encode())
    except Exception:
        sys.exit(0)  # hook failures leave the user's task untouched
