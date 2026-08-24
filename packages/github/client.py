"""Async GitHub client: rate-limited, ETag-aware, retry/backoff, per-endpoint buckets.

Read-path code never touches this — only the indexer stages do. Every method routes
through `_request`, which enforces the correct rate bucket, replays conditional requests,
and parks the bucket on a `403/429` with a reset header.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from packages.core.enums import RateBucket
from packages.core.settings import Settings
from packages.github.auth import TokenProvider
from packages.github.ratelimit import RateLimiter

log = logging.getLogger("github.client")

_API = "https://api.github.com"
_LINK_LAST = re.compile(r'<([^>]+)>;\s*rel="last"')

# GitHub's code-search backend answers a query it cannot serve in time with a 408, and
# (worse) sometimes with a 200 carrying a real `total_count` and an EMPTY `items` array.
# Both are transient; both used to look like "no results" to us.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class GitHubError(RuntimeError):
    """Base for failures we can explain to a user rather than dumping a traceback."""

    #: Shown verbatim in the UI. Say what happened and what to do about it.
    user_message = "GitHub request failed."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.user_message)
        self.user_message = message or self.user_message


class GitHubAuthError(GitHubError):
    """401 — the token is missing, revoked, or expired."""


class GitHubForbidden(GitHubError):
    """403 that is NOT a rate limit — scopes, SSO, or a blocked resource."""


class GitHubUnavailable(GitHubError):
    """GitHub accepted the request but could not serve it (timeouts, 5xx, degraded
    empty result sets). Retrying later is the fix; there is nothing to change locally."""


@dataclass
class CachedResponse:
    """Minimal ETag cache entry, so 304s cost zero rate limit."""

    etag: str
    payload: Any


@dataclass
class GitHubHealth:
    """Live view of how GitHub is treating us, so the UI can say *why* a search is
    stalled instead of spinning forever. Updated from response headers on every call."""

    search_limit: int | None = None
    search_remaining: int | None = None
    search_reset_epoch: float | None = None
    token_expires_at: str | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    degraded_since: float | None = None
    last_ok_at: float | None = None

    def note_response(self, resp: httpx.Response) -> None:
        if resp.headers.get("x-ratelimit-resource") == "code_search":
            self.search_limit = _int_or_none(resp.headers.get("x-ratelimit-limit"))
            self.search_remaining = _int_or_none(resp.headers.get("x-ratelimit-remaining"))
            self.search_reset_epoch = _float_or_none(resp.headers.get("x-ratelimit-reset"))
        if expiry := resp.headers.get("github-authentication-token-expiration"):
            self.token_expires_at = expiry

    def note_error(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()

    def note_degraded(self) -> None:
        if self.degraded_since is None:
            self.degraded_since = time.time()

    def note_ok(self) -> None:
        self.last_ok_at = time.time()
        self.degraded_since = None
        self.last_error = None


@dataclass
class GitHubClient:
    settings: Settings
    limiter: RateLimiter
    health: GitHubHealth = field(default_factory=GitHubHealth)
    _tokens: TokenProvider = field(init=False)
    _http: httpx.AsyncClient = field(init=False)
    _etag_cache: dict[str, CachedResponse] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._tokens = TokenProvider(self.settings)
        self._http = httpx.AsyncClient(
            timeout=30,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-code-explorer",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------------- core --
    async def _request(
        self,
        method: str,
        url: str,
        *,
        bucket: RateBucket,
        use_etag: bool = False,
        max_retries: int = 4,
        **kwargs: Any,
    ) -> httpx.Response:
        headers_in = dict(kwargs.pop("headers", {}))
        for attempt in range(max_retries + 1):
            await self.limiter.acquire(bucket)
            headers = dict(headers_in)
            headers["Authorization"] = await self._tokens.authorization()
            cache_key = f"{method}:{url}"
            if use_etag and (cached := self._etag_cache.get(cache_key)):
                headers["If-None-Match"] = cached.etag

            resp = await self._http.request(method, url, headers=headers, **kwargs)
            self.health.note_response(resp)

            # 304: served from cache, did not consume rate limit — great.
            if resp.status_code == 304 and use_etag:
                return resp
            if use_etag and (etag := resp.headers.get("ETag")):
                self._etag_cache[cache_key] = CachedResponse(etag, _safe_json(resp))

            if resp.status_code == 401:
                # Never retry this: the credentials are wrong and will stay wrong.
                raise self._auth_error(resp)

            if resp.status_code in (403, 429) and _is_rate_limited(resp):
                log.warning(
                    "rate limited: %s %s → %d (resource=%s remaining=%s) — parking "
                    "bucket %s, attempt %d/%d",
                    method, url.replace(_API, ""), resp.status_code,
                    resp.headers.get("x-ratelimit-resource"),
                    resp.headers.get("x-ratelimit-remaining"),
                    bucket, attempt + 1, max_retries + 1,
                )
                message = _rate_limit_message(resp)
                self.health.note_error(message)
                self._park(bucket, resp)
                if attempt < max_retries:
                    continue
                raise GitHubUnavailable(message)
            elif resp.status_code == 403:
                raise GitHubForbidden(
                    "GitHub refused this request (403). The token may lack the required "
                    "scope, or SSO authorisation for the organisation."
                )

            if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                # 408 is GitHub's own "I timed out serving your query" — the single most
                # common failure for broad code searches, and always worth another go.
                delay = 2**attempt
                log.warning(
                    "%s %s → %d (%s) — retrying in %ds, attempt %d/%d",
                    method, url.replace(_API, ""), resp.status_code,
                    _brief_message(resp), delay, attempt + 1, max_retries + 1,
                )
                self.health.note_error(_transient_message(resp))
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 408:
                raise GitHubUnavailable(
                    "GitHub's code search kept timing out on this query. It is busy or "
                    "the query is too broad — narrow it (add a path, filename or "
                    "extension) or try again in a few minutes."
                )
            if resp.is_success:
                self.health.note_ok()
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def _auth_error(self, resp: httpx.Response) -> GitHubAuthError:
        kind = "GitHub App installation token" if self.settings.has_app_auth else "GH_PAT"
        expiry = resp.headers.get("github-authentication-token-expiration")
        detail = f" The token's stated expiry is {expiry}." if expiry else ""
        message = (
            f"GitHub rejected our credentials (401). Your {kind} is missing, revoked or "
            f"expired — update it in .env and restart the backend.{detail}"
        )
        self.health.note_error(message)
        return GitHubAuthError(message)

    def _park(self, bucket: RateBucket, resp: httpx.Response) -> None:
        import time

        reset = resp.headers.get("x-ratelimit-reset")
        retry_after = resp.headers.get("retry-after")
        if reset:
            self.limiter.note_server_reset(bucket, float(reset))
        elif retry_after:
            self.limiter.note_server_reset(bucket, time.time() + float(retry_after))
        else:
            # Secondary rate limits sometimes carry NO reset header. Without a default
            # park we'd retry immediately and hammer an already-angry endpoint.
            self.limiter.note_server_reset(bucket, time.time() + 60)

    def _cached_payload(self, method: str, url: str) -> Any:
        return self._etag_cache[f"{method}:{url}"].payload

    # ------------------------------------------------------------- endpoints --
    async def search_code(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 100,
        text_match: bool = True,
        soft_retries: int = 1,
    ) -> dict:
        """`GET /search/code` — the scarce SEARCH bucket. Max 1000 results per query.

        With `text_match`, requests the text-match media type so each item carries
        `text_matches[].fragment` — matched-line snippets we cache without a separate
        content fetch (the design's offline-snippet requirement, for free).

        Guards the *degraded success*: GitHub answers an overloaded code search with
        HTTP 200, a truthful `total_count`, and `items: []`. That is indistinguishable
        from "no matches" unless you look at the count, and it is why a collection could
        previously run for an hour and store nothing. One re-ask filters a blip while
        staying cheap against a 10/min quota; the caller (discovery) requires several
        consecutive degraded calls before giving up, so a real outage is still caught.
        """
        headers = (
            {"Accept": "application/vnd.github.text-match+json"} if text_match else {}
        )
        data: dict = {}
        for attempt in range(soft_retries + 1):
            resp = await self._request(
                "GET",
                f"{_API}/search/code",
                bucket=RateBucket.SEARCH,
                headers=headers,
                params={"q": query, "page": page, "per_page": per_page},
            )
            data = resp.json()
            # Request-level trace: one line per search call, so a stalled collection can
            # be diagnosed from data/debug.log instead of guessed at.
            log.info(
                "search_code q=%r page=%d → total=%s items=%d remaining=%s",
                query, page, data.get("total_count"), len(data.get("items") or []),
                resp.headers.get("x-ratelimit-remaining"),
            )
            if not _is_degraded(data):
                self.health.note_ok()
                return data
            self.health.note_degraded()
            self.health.note_error(
                f"GitHub reported {data.get('total_count'):,} matches but returned no "
                f"results — its code search is degraded right now."
            )
            log.warning(
                "degraded search response q=%r page=%d: total=%s but 0 items "
                "(attempt %d/%d)",
                query, page, data.get("total_count"), attempt + 1, soft_retries + 1,
            )
            if attempt < soft_retries:
                await asyncio.sleep(3 * (attempt + 1))
        return data

    # A narrow, stable query that must always match — GitHub's own documented example
    # for the code-search endpoint. Used only to tell "genuinely nothing" apart from
    # "the index isn't answering", which are otherwise identical responses.
    CANARY_QUERY = "repo:jquery/jquery addClass in:file"

    async def code_search_alive(self) -> bool:
        """Is code search actually returning results right now?

        A degraded backend does not only answer with a count and no items — for some
        queries it answers `total_count: 0`, which looks exactly like an honest empty
        result. One canary call is the only way to distinguish them, and it is worth the
        quota: reporting "no matches found" when GitHub is simply broken sends the user
        off to doubt their own search terms.
        """
        try:
            data = await self.search_code(self.CANARY_QUERY, per_page=1, soft_retries=0)
        except GitHubError:
            return False
        alive = bool(data.get("total_count"))
        if not alive:
            log.warning("code-search canary returned nothing — the index is not answering")
            self.health.note_degraded()
        return alive

    async def graphql(self, query: str, variables: dict | None = None) -> dict:
        resp = await self._request(
            "POST",
            f"{_API}/graphql",
            bucket=RateBucket.GRAPHQL,
            json={"query": query, "variables": variables or {}},
        )
        return resp.json()

    async def get_json(self, path: str, *, use_etag: bool = True, **params: Any) -> Any:
        """Generic CORE GET returning parsed JSON, ETag-cached (304 → cached payload)."""
        url = f"{_API}{path}"
        resp = await self._request(
            "GET", url, bucket=RateBucket.CORE, use_etag=use_etag, params=params or None
        )
        if resp.status_code == 304:
            return self._cached_payload("GET", url)
        return resp.json()

    async def commit_bounds_for_path(
        self, owner: str, repo: str, path: str
    ) -> tuple[dict, dict] | None:
        """Return `(oldest, newest)` commits touching `path` on the default branch.

        `oldest` is the approximate 'first appeared'; `newest` is 'last modified'. Costs
        ~2 CORE calls: page 1 (per_page=1) is the newest commit and carries the Link:last
        header pointing at the oldest. Does not follow renames (that's the clone tier).
        """
        url = f"{_API}/repos/{owner}/{repo}/commits"
        page1 = await self._request(
            "GET", url, bucket=RateBucket.CORE, params={"path": path, "per_page": 1}
        )
        commits = page1.json()
        if not commits:
            return None
        newest = commits[0]

        last_page = _last_page_from_link(page1.headers.get("Link", ""))
        if last_page is None or last_page == 1:
            return newest, newest  # single commit touched this path

        oldest_resp = await self._request(
            "GET",
            url,
            bucket=RateBucket.CORE,
            params={"path": path, "per_page": 1, "page": last_page},
        )
        oldest = oldest_resp.json()
        return (oldest[-1] if oldest else newest), newest

    # ----------------------------------------------------- inspection (on-demand) --
    async def get_contents(
        self, owner: str, repo: str, path: str = "", ref: str | None = None
    ) -> Any:
        """`GET /repos/{o}/{r}/contents/{path}`. A directory returns a list of entries;
        a file returns a single object with base64 `content`. Omitting `ref` uses the
        default branch. ETag-cached. CORE bucket.
        """
        suffix = f"/{path.strip('/')}" if path.strip("/") else ""
        params = {"ref": ref} if ref else {}
        return await self.get_json(f"/repos/{owner}/{repo}/contents{suffix}", **params)

    async def get_raw_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> bytes:
        """Raw bytes of one file via the raw media type — stays on api.github.com so the
        request is authenticated and rate-limited (unlike following a download_url off to
        another host). CORE bucket.
        """
        url = f"{_API}/repos/{owner}/{repo}/contents/{path.strip('/')}"
        params = {"ref": ref} if ref else None
        resp = await self._request(
            "GET", url, bucket=RateBucket.CORE,
            headers={"Accept": "application/vnd.github.raw"}, params=params,
        )
        return resp.content

    async def get_recursive_tree(
        self, owner: str, repo: str, ref: str | None = None
    ) -> tuple[list[dict], bool, str | None]:
        """The whole repo file tree in one shot, with per-blob sizes. Returns
        `(entries, truncated, commit_sha)`. Two CORE calls: the latest commit gives the
        root tree sha (works for any ref; omitting `ref` uses the default branch), then a
        single recursive `git/trees` fetch. `truncated` is GitHub's flag for repos too
        large to return in full (~100k entries) — the caller surfaces it rather than
        lying. `commit_sha` pins raw-download URLs to the exact snapshot listed.
        """
        params: dict[str, Any] = {"per_page": 1}
        if ref:
            params["sha"] = ref
        commits = await self.get_json(f"/repos/{owner}/{repo}/commits", **params)
        if not commits:
            return [], False, None
        commit_sha = commits[0]["sha"]
        tree_sha = commits[0]["commit"]["tree"]["sha"]
        data = await self.get_json(
            f"/repos/{owner}/{repo}/git/trees/{tree_sha}", recursive="1"
        )
        return data.get("tree", []), bool(data.get("truncated")), commit_sha

    async def raw_auth_header(self) -> str | None:
        """`Authorization: ...` header line for raw.githubusercontent.com fetches, or
        None when no credentials are configured (public content works without auth)."""
        try:
            return f"Authorization: {await self._tokens.authorization()}"
        except RuntimeError:
            return None

    async def resolve_archive_url(
        self, owner: str, repo: str, ref: str | None = None
    ) -> str:
        """Resolve the zipball to its final codeload URL by reading the 302 Location.

        We resolve it here (authenticated) rather than let the download engine follow the
        redirect, because aria2 forwards headers across redirected hosts — pre-resolving
        keeps the token on api.github.com and hands the engine a ready, pre-signed URL.
        """
        seg = f"/{ref}" if ref else ""
        try:
            resp = await self._request(
                "GET", f"{_API}/repos/{owner}/{repo}/zipball{seg}", bucket=RateBucket.CORE
            )
        except httpx.HTTPStatusError as exc:
            # httpx's raise_for_status raises on 3xx too (we don't follow redirects) —
            # for this endpoint the 302 IS the answer, so read Location off the error.
            r = exc.response
            if r is not None and r.is_redirect and r.headers.get("Location"):
                return r.headers["Location"]
            raise
        location = resp.headers.get("Location")
        if resp.is_redirect and location:
            return location
        # No redirect (rare) — fall back to the API URL itself.
        return str(resp.url)

    @asynccontextmanager
    async def stream_zipball(
        self, owner: str, repo: str, ref: str | None = None
    ) -> AsyncIterator[httpx.Response]:
        """Open a streaming response for the repo zipball. GitHub 302-redirects to
        codeload; httpx follows it and strips the Authorization header on the cross-host
        hop (the codeload URL is pre-signed), so the token never leaves api.github.com.
        Costs one CORE token. Caller streams `.aiter_bytes()` or buffers with `.aread()`.
        """
        seg = f"/{ref}" if ref else ""
        await self.limiter.acquire(RateBucket.CORE)
        headers = {"Authorization": await self._tokens.authorization()}
        req = self._http.build_request(
            "GET", f"{_API}/repos/{owner}/{repo}/zipball{seg}", headers=headers
        )
        resp = await self._http.send(req, stream=True, follow_redirects=True)
        try:
            resp.raise_for_status()
            yield resp
        finally:
            await resp.aclose()


def _last_page_from_link(link_header: str) -> int | None:
    m = _LINK_LAST.search(link_header)
    if not m:
        return None
    page_match = re.search(r"[?&]page=(\d+)", m.group(1))
    return int(page_match.group(1)) if page_match else None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _brief_message(resp: httpx.Response) -> str:
    """GitHub's own `message` field, when the body is JSON — far more useful than a
    bare status code in a log line or a UI banner."""
    payload = _safe_json(resp)
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return ""


def _transient_message(resp: httpx.Response) -> str:
    """A retryable failure, phrased so the user knows it is not their fault and not
    something they need to fix."""
    detail = _brief_message(resp)
    if resp.status_code == 408:
        return (
            "GitHub timed out serving this search — its code search is busy. Retrying."
        )
    if resp.status_code >= 500:
        # GitHub's own wording is worth repeating: "too many shards unavailable" says
        # their search index is partly down, which a bare "503" does not.
        because = f' — GitHub says: "{detail}"' if detail else ""
        return (
            f"GitHub's search index returned a server error ({resp.status_code}){because}. "
            f"This is a problem on their side, not with your token or your query. "
            f"Retrying automatically."
        )
    return detail or f"GitHub returned {resp.status_code} — retrying."


def _rate_limit_message(resp: httpx.Response) -> str:
    reset = _float_or_none(resp.headers.get("x-ratelimit-reset"))
    resource = resp.headers.get("x-ratelimit-resource") or "the API"
    if reset:
        wait = max(0, int(reset - time.time()))
        return (
            f"GitHub rate limit reached on {resource} — waiting {wait // 60}m {wait % 60}s "
            f"for the quota to reset."
        )
    return f"GitHub rate limit reached on {resource} — backing off."


def _is_degraded(data: dict) -> bool:
    """A code-search payload that claims matches but carries none."""
    return bool(data.get("total_count")) and not (data.get("items") or [])


def _is_rate_limited(resp: httpx.Response) -> bool:
    # 429 is BY DEFINITION rate limiting — GitHub's secondary/abuse 429s arrive with an
    # HTML body (no "rate limit" text) and even a full remaining quota, so never rely
    # on body sniffing alone.
    if resp.status_code == 429:
        return True
    remaining = resp.headers.get("x-ratelimit-remaining")
    return remaining == "0" or "rate limit" in resp.text.lower()


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None
