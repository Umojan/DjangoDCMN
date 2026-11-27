"""Redaction of engine response bodies before they are stored in EngineRequest.result (DESIGN §4.4).

The caller still receives the full body; only the stored copy loses client data: every `payload` becomes
`{api_name: {type, len}}` plus `payload_sha256`, and candidates keep only `{module, id, match}`.
"""

import hashlib
import json

CANDIDATE_KEPT_KEYS = ('module', 'id', 'match')


def redact_result(body: dict) -> dict:
    """A copy of `body` that is safe to store (no payload values, no candidate names)."""
    return _redact(body)


def payload_sha256(payload: dict) -> str:
    """Stable hash of a Zoho payload (key order does not matter)."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _redact(value):
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    redacted = {}
    for key, item in value.items():
        if key == 'payload' and isinstance(item, dict):
            redacted['payload'] = {name: _shape(field) for name, field in item.items()}
            redacted['payload_sha256'] = payload_sha256(item)
        elif key == 'candidates' and isinstance(item, list):
            redacted['candidates'] = [
                {k: candidate.get(k) for k in CANDIDATE_KEPT_KEYS} for candidate in item if isinstance(candidate, dict)
            ]
        else:
            redacted[key] = _redact(item)
    return redacted


def _shape(value) -> dict:
    if value is None:
        return {'type': 'null', 'len': 0}
    if isinstance(value, bool):
        return {'type': 'bool', 'len': 1}
    if isinstance(value, (int, float)):
        return {'type': type(value).__name__, 'len': len(str(value))}
    if isinstance(value, (list, tuple)):
        return {'type': 'list', 'len': len(value)}
    if isinstance(value, dict):
        return {'type': 'dict', 'len': len(value)}
    return {'type': 'str', 'len': len(str(value))}
