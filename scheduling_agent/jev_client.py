"""Thin wrapper over TypeSafe's SDK for Jev, the System One model.

Jev answers typed questions about a JSON state — `Noul` (a calibrated
probability of yes), `Choice` (a distribution over labels we define) — in one
parallel request, typically ~100ms. It never generates text, so it can't
hallucinate a value; it can only pick among the options it's given.

The API key is read from TYPESAFE_API_KEY (loaded from .env by
scheduling_agent/__init__.py).
"""

import logging
import os

from typesafe_sdk import RetryPolicy, SystemOneResponse, TypeSafeClient
from typesafe_sdk.constants import API_KEY_ENV

from scheduling_agent import usage_tracker

logger = logging.getLogger(__name__)

MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL") or "jev-latest"

# Jev answers in ~100ms; a request still running after this is hung. Far
# below dedup.REQUEST_TIMEOUT_SECONDS because nothing here generates tokens.
REQUEST_TIMEOUT_SECONDS = 15.0

_client = None


def has_api_key() -> bool:
    """Whether a TypeSafe key is configured. The SDK strips whitespace and
    rejects an empty key, so a blank or whitespace-only value counts as none."""
    return bool(os.environ.get(API_KEY_ENV, "").strip())


def _get_client() -> TypeSafeClient:
    """Lazily construct the client so importing this module does not require
    TYPESAFE_API_KEY (and so tests can swap in a fake)."""
    global _client
    if _client is None:
        _client = TypeSafeClient(
            model=MODEL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            retry=RetryPolicy(max_retries=1),
        )
    return _client


def ask(state: dict, questions: dict) -> SystemOneResponse:
    """One System One request. Records usage on success and a failure on any
    exception (then re-raises), so eval run-validity accounting covers Jev
    calls exactly like Anthropic ones."""
    try:
        response = _get_client().system_one(state=state, questions=questions)
    except Exception as e:
        usage_tracker.record_failure(repr(e))
        raise
    usage_tracker.record(response.model, response.usage)
    return response
