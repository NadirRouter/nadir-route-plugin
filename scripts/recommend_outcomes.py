"""Metadata-only receipts for Claude's recommendation mode. No model calls.

Stop is a completion signal, never a quality grade. Transcript usage is only
the observed main turn: Claude may flush its final message after Stop. The next
prompt refreshes that receipt before saving a new decision. Failed uploads stay
in a private outbox, bound to the decision endpoint and key fingerprint.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager

LIMIT = 32 * 1024 * 1024
FIELDS = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
          "cache_read_tokens": "cache_read_input_tokens",
          "cache_write_tokens": "cache_creation_input_tokens"}


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def enabled(env):
    return bool(env.get("NADIR_API_KEY")) and not any(
        env.get(k) == "1" for k in ("NADIR_ROUTE_DISABLE", "NADIR_OUTCOMES_DISABLE"))


def root(env):
    return Path(env.get("NADIR_CONTEXT_STATE_DIR") or Path.home() / ".nadir/context")


def binding(env):
    # Advice always uses bucket, even when a route-v2 endpoint is also set.
    bucket = env.get("NADIR_BUCKET_URL") or "https://api.getnadir.com/v1/bucket"
    return {"endpoint": env.get("NADIR_OUTCOME_URL") or bucket.rstrip("/") + "/outcome",
            "decision_endpoint": bucket, "key_hash": digest(env.get("NADIR_API_KEY", ""))}


def state_path(hook, env):
    session = hook.get("session_id")
    if isinstance(session, str) and 0 < len(session) <= 1024:
        return root(env) / digest(session) / "recommend-decision.json"


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(directory, name, nonblocking=False):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
    finally:
        os.close(fd)


def checkpoint(hook):
    """Bound this receipt to an append-only prefix, before asking for advice."""
    try:
        path = hook["transcript_path"]
        with open(path, "rb") as transcript:
            transcript.seek(0, 2)
            offset = transcript.tell()
            transcript.seek(max(0, offset - 4096))
            anchor = digest(transcript.read())
        return {"offset": offset, "anchor": anchor, "transcript_hash": digest(path)}
    except (OSError, KeyError, TypeError):
        return None


def remember(hook, env, request_id, mark):
    path = state_path(hook, env)
    if path is None:
        return
    if not enabled(env) or not mark or not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
        path.unlink(missing_ok=True)
        return
    atomic(path, dict(mark, request_id=request_id, **binding(env)))


def observed(hook, state):
    """Read cumulative, deduplicated main-thread usage, leaving unknowns absent."""
    unknown = {"model_evidence": "unknown"}
    try:
        path, offset = hook["transcript_path"], state["offset"]
        if digest(path) != state["transcript_hash"]:
            return unknown
        with open(path, "rb") as transcript:
            transcript.seek(0, 2)
            size = transcript.tell()
            if size < offset or size - offset > LIMIT:
                return unknown
            transcript.seek(max(0, offset - 4096))
            if digest(transcript.read(min(offset, 4096))) != state["anchor"]:
                return unknown
            raw = transcript.read(size - offset)
    except (KeyError, OSError, TypeError, ValueError):
        return unknown
    messages, requests, tool_ids = {}, set(), set()
    tools_known, requests_known = True, True
    # An incomplete trailing JSON line can be picked up on the next prompt.
    for line in raw.split(b"\n")[:-1]:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            return unknown
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("model") == "<synthetic>":
            continue
        mid = message.get("id")
        if not isinstance(mid, str) or not mid:
            return unknown  # without message identity streaming blocks can double count
        prior = messages.setdefault(mid, {"models": set(), "usage": {}})
        model = message.get("model")
        prior["models"].add(model if isinstance(model, str) and 0 < len(model) <= 200 else None)
        usage = message.get("usage")
        if isinstance(usage, dict):
            for key in FIELDS.values():
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    prior["usage"][key] = max(value, prior["usage"].get(key, 0))
        request = entry.get("requestId")
        if isinstance(request, str) and request:
            requests.add(request)
        else:
            requests_known = False
        content = message.get("content")
        if not isinstance(content, list):
            tools_known = False
        else:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    if isinstance(tid, str) and tid:
                        tool_ids.add(tid)
                    else:
                        tools_known = False
    if not messages:
        return unknown
    result = dict(unknown)
    models = set().union(*(m["models"] for m in messages.values()))
    if len(models) == 1 and None not in models:
        result.update(resolved_model=models.pop(), model_evidence="provider_response")
    for target, key in FIELDS.items():
        if all(key in m["usage"] for m in messages.values()):
            result[target] = sum(m["usage"][key] for m in messages.values())
    if requests_known:
        result["turns"] = len(requests)
    if tools_known:
        result["tool_uses"] = len(tool_ids)
    return result


def capture(hook, env):
    path = state_path(hook, env)
    if not enabled(env) or path is None:
        return
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(state, dict):
        return
    if any(state.get(k) != v for k, v in binding(env).items()):
        return
    event = hook.get("hook_event_name")
    if event == "StopFailure":
        state["status"] = "failed_ungraded"
    elif event == "Stop" and state.get("status") != "failed_ungraded":
        state["status"] = "completed_ungraded"
    body = dict(observed(hook, state), request_id=state["request_id"],
                status=state.get("status", "observed_ungraded"), usage_scope="observed_main_turn")
    if body["status"] == "failed_ungraded":
        body["is_error"] = True
    # Stop can precede the final transcript write. Completion is still known,
    # but usage and model attribution are left unknown until a later refresh.
    fingerprint = digest(json.dumps(body, sort_keys=True))
    if fingerprint == state.get("queued_hash"):
        return
    outbox = root(env) / "recommend-outbox"
    with locked(outbox, ".queue.lock"):
        atomic(outbox / (digest(state["request_id"]) + ".json"),
               dict(binding(env), body=body, fingerprint=fingerprint))
        atomic(path, dict(state, queued_hash=fingerprint))


def sync(env, post, log=lambda _: None):
    """Retry up to five receipts, serializing uploaders without blocking hooks."""
    if not enabled(env):
        return
    outbox = root(env) / "recommend-outbox"
    try:
        with locked(outbox, ".upload.lock", nonblocking=True):
            sent = 0
            for path in sorted(outbox.glob("*.json"), key=lambda p: p.stat().st_mtime):
                if sent >= 5:
                    break
                try:
                    receipt = json.loads(path.read_text())
                    if not isinstance(receipt, dict):
                        continue
                    if any(receipt.get(k) != v for k, v in binding(env).items()):
                        continue
                    sent += 1
                    response = post(receipt["body"], url=receipt["endpoint"])
                    if not isinstance(response, dict) or response.get("recorded") is not True or response.get("request_id") != receipt["body"]["request_id"]:
                        continue
                    with locked(outbox, ".queue.lock"):
                        if json.loads(path.read_text()).get("fingerprint") == receipt["fingerprint"]:
                            path.unlink()
                    log({"harness": "claude-code", "event": "outcome_uploaded", "mode": "recommend",
                         "request_id": receipt["body"]["request_id"],
                         "status": receipt["body"]["status"],
                         "resolved_model": receipt["body"].get("resolved_model")})
                except (OSError, ValueError, KeyError):
                    continue  # the durable receipt is retried on the next hook
    except BlockingIOError:
        pass
