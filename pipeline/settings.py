"""
Centralized environment variable loading.

load_dotenv() happens exactly once, here - every other module that needs an
env var imports it from this module instead of calling load_dotenv() and
os.environ.get() itself.

Per-stage model/experiment config lives in
pipeline/config/experiment_schema.py (YAML + Pydantic), not here - see
load_experiment_config().
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()


def get_ig_access_token(var_name: str = "IG_ACCESS_TOKEN") -> str | None:
    """Looks up an Instagram access token by env var name.

    Lets a second (or third) connected account's token live in .env under
    its own name (e.g. IG_ACCESS_TOKEN_GADGETS) instead of overwriting
    IG_ACCESS_TOKEN every time you switch which vendor you're working with -
    ingest.py and scripts/run_stage6.py both take a --token-env flag that
    resolves through this function.
    """
    return os.environ.get(var_name)


IG_ACCESS_TOKEN = get_ig_access_token()


def ig_auth_headers(token: str) -> dict[str, str]:
    """Authorization header for a Graph API call.

    Always prefer this over passing the token as an `access_token` query
    parameter. A token in the URL leaks into places that are easy to miss:
    urllib3's DEBUG log line prints the full path+query, requests'
    `raise_for_status()` embeds the URL in the exception message, and
    connection errors do the same - so an expired token, a rate limit, a
    network blip, or simply LOG_LEVEL=DEBUG is enough to print a live
    credential to the console. Confirmed working against graph.instagram.com
    (2026-09-07). See README.md.
    """
    return {"Authorization": f"Bearer {token}"}


def gemini_auth_headers(api_key: str | None) -> dict[str, str]:
    """Authorization header for a raw Google Generative Language REST call.

    Same reasoning as ig_auth_headers(): `?key=<GEMINI_API_KEY>` is the
    documented alternative, but requests embeds the full request URL - query
    string included - in the HTTPError it raises, so one 429 on an embedding
    call was enough to print the live key to the console. GEMINI_API_KEY is
    not embeddings-scoped: it is the key litellm resolves for every gemini/*
    completion in Stages 1-4, i.e. the pipeline's primary credential.
    Returns {} when the key is unset so the caller gets the API's own
    "missing key" error rather than a header with a None value.
    See README.md.
    """
    return {"x-goog-api-key": api_key} if api_key else {}


def get_typesafe_api_key(var_name: str = "TYPESAFE_API_KEY") -> str | None:
    """Looks up the TypeSafe (Jev) API key by env var name.

    Jev is reached at POST https://api.typesafe.ai/v1/systemone, which is
    NOT an OpenAI-compatible endpoint - it takes {state, model, questions}
    and returns typed decisions rather than text, so it cannot go through
    litellm's completion() or complete_structured(). Any call site that
    reaches it therefore resolves the key here and sets its own headers,
    the same way the raw Gemini embedding call in media_fingerprint.py does.

    Takes a var_name for the same reason get_ig_access_token() does: a
    second key (a teammate's, or a separate billing account) can live in
    .env under its own name without overwriting this one.
    """
    return os.environ.get(var_name)


def typesafe_auth_headers(api_key: str | None) -> dict[str, str]:
    """Authorization + content-type headers for a TypeSafe System One call.

    Bearer auth, per docs.typesafe.ai/api. Same reasoning as
    ig_auth_headers() and gemini_auth_headers(): the credential goes in a
    header and never in the URL, so a 429 or a connection error cannot print
    it through an exception message that embeds the request URL. Note that
    redact_tokens() already scrubs `Bearer <value>`, so a leaked header in a
    logged exception is covered too - provided the call site logs
    redact_tokens(exc) rather than a bare exc.

    Returns {} when the key is unset, so the caller gets TypeSafe's own
    "missing credentials" error rather than a header with a None value.
    """
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


# Credential-carrying query parameters, stripped from any URL that gets
# followed (pagination) and redacted out of anything that gets logged.
CREDENTIAL_QUERY_PARAMS = frozenset({"access_token", "key", "api_key", "api-key", "client_secret"})

# The leading \b stops `key=` matching inside a longer word - `monkey=` is
# not a credential. `key=` itself is Google's REST auth param; the optional
# api_/api- prefix covers the other providers' spellings.
_TOKEN_QUERY_RE = re.compile(r"\b((?:api[_-]?)?(?:access_token|key)=)[^&\s\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_GOOG_KEY_HEADER_RE = re.compile(
    r"(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+", re.IGNORECASE
)


def redact_tokens(text: object) -> str:
    """Strip credentials out of a string before logging it.

    Defence in depth for the exception path: even with header auth, a
    third-party library or a future call site may put a token somewhere that
    ends up in an error message. Log `redact_tokens(exc)`, never `exc`.
    """
    redacted = _TOKEN_QUERY_RE.sub(r"\1<redacted>", str(text))
    redacted = _BEARER_RE.sub(r"\1<redacted>", redacted)
    return _GOOG_KEY_HEADER_RE.sub(r"\1<redacted>", redacted)


def strip_url_credentials(url: str) -> tuple[str, bool]:
    """Removes credential query params from `url`.

    Returns (clean_url, had_credential). Meant for the `paging.next` URL the
    Graph API hands back: pagination follows that URL verbatim, so if Meta
    echoes an `access_token=` param into it, the follow-up request puts the
    token back into the query string even though the first call authenticated
    with a header. Stripping it is free and correct either way - the
    Authorization header still authenticates the follow-up - and the returned
    bool lets the caller record *whether* Graph does this without ever
    logging the value. See README.md.
    """
    parsed = urlsplit(url)
    if not parsed.query:
        return url, False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in CREDENTIAL_QUERY_PARAMS]
    if len(kept) == len(pairs):
        return url, False
    return urlunsplit(parsed._replace(query=urlencode(kept))), True
