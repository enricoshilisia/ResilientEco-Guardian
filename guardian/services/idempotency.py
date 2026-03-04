"""
Idempotency helpers for write-like API actions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Dict

from django.utils import timezone

from ..models import IdempotencyRequest


DEFAULT_TTL_HOURS = 24


def extract_idempotency_key(request) -> str:
    return (
        request.headers.get("Idempotency-Key")
        or request.data.get("idempotency_key")
        or ""
    ).strip()


def _actor_from_request(request) -> str:
    if getattr(request, "user", None) and request.user.is_authenticated:
        return f"user:{request.user.id}"

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR", "unknown")
    return f"anon:{ip}"


def _fingerprint(action: str, actor: str, payload: Dict[str, Any]) -> str:
    canonical = json.dumps(
        {"action": action, "actor": actor, "payload": payload},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def start_idempotent_request(request, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Start idempotent execution.
    Returns:
      {
        "key": str,
        "record": IdempotencyRequest | None,
        "replay": {"status_code": int, "payload": dict} | None,
        "error": {"status_code": int, "payload": dict} | None,
      }
    """
    key = extract_idempotency_key(request)
    if not key:
        return {"key": "", "record": None, "replay": None, "error": None}

    actor = _actor_from_request(request)
    request_fingerprint = _fingerprint(action, actor, payload or {})
    expires_at = timezone.now() + timedelta(hours=DEFAULT_TTL_HOURS)

    record, created = IdempotencyRequest.objects.get_or_create(
        key=key,
        action=action,
        actor=actor,
        defaults={
            "request_fingerprint": request_fingerprint,
            "status": "processing",
            "expires_at": expires_at,
        },
    )

    if created:
        return {"key": key, "record": record, "replay": None, "error": None}

    if record.request_fingerprint != request_fingerprint:
        return {
            "key": key,
            "record": None,
            "replay": None,
            "error": {
                "status_code": 409,
                "payload": {
                    "error": "Idempotency key reused with different request payload.",
                    "idempotency_key": key,
                    "action": action,
                },
            },
        }

    if record.status == "processing":
        return {
            "key": key,
            "record": None,
            "replay": None,
            "error": {
                "status_code": 409,
                "payload": {
                    "error": "Request with this idempotency key is still processing.",
                    "idempotency_key": key,
                    "action": action,
                },
            },
        }

    if record.response_payload is not None and record.response_status_code is not None:
        payload = record.response_payload
        if isinstance(payload, dict):
            payload = {**payload, "idempotency_replayed": True, "idempotency_key": key}
        return {
            "key": key,
            "record": None,
            "replay": {"status_code": int(record.response_status_code), "payload": payload},
            "error": None,
        }

    return {
        "key": key,
        "record": None,
        "replay": None,
        "error": {
            "status_code": 500,
            "payload": {
                "error": "Idempotency record exists without stored response.",
                "idempotency_key": key,
                "action": action,
            },
        },
    }


def finalize_idempotent_request(
    record: IdempotencyRequest | None,
    *,
    status_code: int,
    payload: Any,
    success: bool = True,
    error_message: str = "",
) -> None:
    if not record:
        return

    record.status = "completed" if success else "failed"
    record.response_status_code = int(status_code)
    record.response_payload = payload
    record.error_message = error_message or None
    record.save(
        update_fields=[
            "status",
            "response_status_code",
            "response_payload",
            "error_message",
            "updated_at",
        ]
    )
