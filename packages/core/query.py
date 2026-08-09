"""One input box → the right GitHub code-search query.

The user should never have to decide whether what they typed is a keyword, a path, a
filename, an extension or a URL. They paste or type *a thing*; this module works out what
kind of thing it is, builds the GitHub query for it, and explains its choice in one line
so the guess is always visible and overridable.

Lives in `core` (not the API layer) so the indexer, the API and the tests all resolve
queries through exactly one implementation — the UI just renders what this returns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from packages.core.enums import SearchType

# Qualifiers a power user may type directly. If we see one, the input is already a
# GitHub query and we must not "help" by wrapping it.
_QUALIFIER = re.compile(
    r"\b(repo|org|user|path|filename|extension|language|size|fork|in|sort):", re.I
)

# `*.cs`, `.cs`, `*.unitypackage` — a bare extension, not a filename.
_EXT_ONLY = re.compile(r"^\*?\.([A-Za-z0-9_+-]{1,15})$")

# `Something.cs` — a filename: no whitespace, no slash, ends in a plausible extension.
_FILENAME = re.compile(r"^[^\s/\\]+\.[A-Za-z0-9_+-]{1,15}$")

_GITHUB_HOSTS = {"github.com", "www.github.com", "raw.githubusercontent.com"}


@dataclass
class DetectedQuery:
    """What we decided the input means.

    `query` is the term we searched for (stored as `Search.keyword`), `normalized` is the
    literal string sent to GitHub, and `explanation` is the one-liner shown in the UI.
    `repo`/`path`/`ref` are set when the input pinned a specific repo — the UI offers to
    open the inspector there instead of (or alongside) running the search.
    """

    search_type: SearchType
    query: str
    normalized: str
    explanation: str
    repo: str | None = None
    path: str | None = None
    ref: str | None = None


def _github_url_parts(text: str) -> tuple[str, list[str]] | None:
    """→ (host, path segments) for a GitHub URL, else None. Accepts a bare
    `github.com/...` with no scheme, which is what people actually paste."""
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.netloc.lower() not in _GITHUB_HOSTS:
        return None
    return parsed.netloc.lower(), [p for p in parsed.path.split("/") if p]


def _from_url(text: str) -> DetectedQuery | None:
    parts = _github_url_parts(text)
    if parts is None:
        return None
    host, seg = parts
    if len(seg) < 2:
        return None
    owner, repo = seg[0], seg[1]
    full = f"{owner}/{repo}"

    # raw.githubusercontent.com/owner/repo/<ref>/path...
    if host == "raw.githubusercontent.com" and len(seg) > 3:
        return _from_repo_path(full, seg[2], seg[3:])

    # github.com/owner/repo/(blob|tree|raw)/<ref>/path...
    if len(seg) > 4 and seg[2] in ("blob", "tree", "raw"):
        return _from_repo_path(full, seg[3], seg[4:])

    # github.com/owner/repo → nothing to search; the repo itself is the answer.
    return DetectedQuery(
        search_type=SearchType.REPO,
        query=full,
        normalized=f"repo:{full}",
        explanation=f"GitHub repo link — opening {full} in the inspector.",
        repo=full,
    )


def _from_repo_path(full: str, ref: str, tail: list[str]) -> DetectedQuery:
    """A link that points *inside* a repo. We search for the asset globally (the whole
    point of this tool is 'where else does this appear, and when did it first show up'),
    while remembering the repo so the UI can offer to inspect it directly."""
    path = "/".join(tail)
    leaf = tail[-1]
    if "." in leaf:  # a file
        return DetectedQuery(
            search_type=SearchType.FILENAME,
            query=leaf,
            normalized=f"filename:{leaf}",
            explanation=(
                f"File link in {full} — searching every repo for files named “{leaf}”."
            ),
            repo=full,
            path=path,
            ref=ref,
        )
    return DetectedQuery(
        search_type=SearchType.PATH,
        query=path,
        normalized=f"{path} in:path",
        explanation=(
            f"Folder link in {full} — searching every repo for the path “{path}”."
        ),
        repo=full,
        path=path,
        ref=ref,
    )


def detect(text: str) -> DetectedQuery:
    """Classify raw user input. Never raises; the worst case is a keyword search."""
    raw = (text or "").strip()
    if not raw:
        return DetectedQuery(
            SearchType.KEYWORD, "", "", "Type anything — a name, a path, or a GitHub link."
        )

    if (url := _from_url(raw)) is not None:
        return url

    # An explicitly quoted string is an exact-phrase request.
    if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1].strip()
        if inner:
            return DetectedQuery(
                SearchType.PHRASE, inner, f'"{inner}"',
                f"Quoted — matching the exact phrase “{inner}”.",
            )

    # Already a GitHub query: pass it through untouched.
    if _QUALIFIER.search(raw):
        return DetectedQuery(
            SearchType.RAW, raw, raw,
            "Recognised GitHub search syntax — sent to GitHub as typed.",
        )

    if (m := _EXT_ONLY.match(raw)) is not None:
        ext = m.group(1)
        return DetectedQuery(
            SearchType.EXTENSION, ext, f"extension:{ext}",
            f"Looks like a file extension — searching every .{ext} file.",
        )

    # A path (or path fragment) — slashes are the giveaway. Checked before the filename
    # rule so `Assets/Foo/Bar.cs` searches the whole path, not just its last segment.
    if "/" in raw or "\\" in raw:
        path = raw.replace("\\", "/").strip("/")
        return DetectedQuery(
            SearchType.PATH, path, f"{path} in:path",
            f"Looks like a path — matching “{path}” anywhere in a file's path.",
        )

    if _FILENAME.match(raw):
        return DetectedQuery(
            SearchType.FILENAME, raw, f"filename:{raw}",
            f"Looks like a filename — searching for files named “{raw}”.",
        )

    return DetectedQuery(
        SearchType.KEYWORD, raw, raw,
        f"Searching file contents for “{raw}”.",
    )


def normalize(keyword: str, search_type: SearchType) -> DetectedQuery:
    """Resolve (input, chosen type) → the query we actually run.

    `SearchType.AUTO` defers to `detect`; anything else is the user overriding the guess,
    and we honour it literally. This is the single place a UI-level choice becomes GitHub
    syntax, so `/index` and `/detect` can never disagree.
    """
    kw = (keyword or "").strip()
    if search_type == SearchType.AUTO:
        return detect(kw)

    match search_type:
        case SearchType.PHRASE:
            return DetectedQuery(
                search_type, kw, f'"{kw}"', f"Exact phrase “{kw}”."
            )
        case SearchType.FILENAME:
            return DetectedQuery(
                search_type, kw, f"filename:{kw}", f"Files named “{kw}”."
            )
        case SearchType.PATH:
            # `kw in:path` matches the term anywhere in the file path — folder names
            # included. (A bare `path:kw` qualifier does NOT find folder names on the
            # legacy code-search API; verified empirically.)
            return DetectedQuery(
                search_type, kw, f"{kw} in:path", f"Paths containing “{kw}”."
            )
        case SearchType.EXTENSION:
            ext = kw.lstrip("*.")
            return DetectedQuery(
                search_type, ext, f"extension:{ext}", f"Every .{ext} file."
            )
        case SearchType.LANGUAGE:
            return DetectedQuery(
                search_type, kw, f"language:{kw}", f"Every {kw} file."
            )
        case SearchType.REPO:
            repo = kw.strip("/")
            return DetectedQuery(
                search_type, repo, f"repo:{repo}", f"The repo {repo}.", repo=repo
            )
        case SearchType.RAW:
            return DetectedQuery(search_type, kw, kw, "Sent to GitHub as typed.")
        case _:
            return DetectedQuery(
                SearchType.KEYWORD, kw, kw, f"File contents containing “{kw}”."
            )
