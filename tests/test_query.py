"""Auto-detection: whatever the user pastes, we work out what it means.

These are the cases people actually type into the box — a name, a folder, a file, a
GitHub link copied from the address bar — and each one has exactly one sensible reading.
"""
from __future__ import annotations

from packages.core.enums import SearchType
from packages.core.query import detect, normalize


def test_plain_word_is_a_keyword():
    d = detect("HighlightPlus")
    assert d.search_type == SearchType.KEYWORD
    assert d.normalized == "HighlightPlus"


def test_filename_with_extension():
    d = detect("PolyfewRuntime.cs")
    assert d.search_type == SearchType.FILENAME
    assert d.normalized == "filename:PolyfewRuntime.cs"


def test_path_wins_over_filename_when_it_has_slashes():
    # The whole path is the interesting thing, not just its last segment.
    d = detect("Assets/HighlightPlus/Runtime/HP.cs")
    assert d.search_type == SearchType.PATH
    assert d.normalized == "Assets/HighlightPlus/Runtime/HP.cs in:path"


def test_windows_style_path_is_normalised():
    assert detect(r"Assets\Exoa\Common").normalized == "Assets/Exoa/Common in:path"


def test_bare_extension():
    for text in (".shader", "*.shader"):
        d = detect(text)
        assert d.search_type == SearchType.EXTENSION
        assert d.normalized == "extension:shader"


def test_quoted_input_is_an_exact_phrase():
    d = detect('"using DOTween"')
    assert d.search_type == SearchType.PHRASE
    assert d.normalized == '"using DOTween"'


def test_existing_github_syntax_is_passed_through_untouched():
    d = detect("repo:jquery/jquery addClass")
    assert d.search_type == SearchType.RAW
    assert d.normalized == "repo:jquery/jquery addClass"


def test_repo_url_is_a_repo_not_a_search():
    d = detect("https://github.com/Unity-Technologies/UnityCsReference")
    assert d.search_type == SearchType.REPO
    assert d.repo == "Unity-Technologies/UnityCsReference"


def test_url_without_scheme_still_recognised():
    assert detect("github.com/acme/game").search_type == SearchType.REPO


def test_blob_url_searches_for_that_file_everywhere():
    d = detect("https://github.com/acme/game/blob/main/Assets/HP/Highlight.cs")
    assert d.search_type == SearchType.FILENAME
    assert d.normalized == "filename:Highlight.cs"
    # The source repo is remembered so the UI can offer to inspect it directly.
    assert d.repo == "acme/game"
    assert d.path == "Assets/HP/Highlight.cs"
    assert d.ref == "main"


def test_tree_url_searches_for_that_folder():
    d = detect("https://github.com/acme/game/tree/main/Assets/HighlightPlus")
    assert d.search_type == SearchType.PATH
    assert d.normalized == "Assets/HighlightPlus in:path"


def test_raw_githubusercontent_url():
    d = detect("https://raw.githubusercontent.com/acme/game/main/Assets/HP/Highlight.cs")
    assert d.search_type == SearchType.FILENAME
    assert d.repo == "acme/game"


def test_non_github_url_is_not_special_cased():
    # It has slashes, so it reads as a path — but it must not claim to be a repo.
    assert detect("https://example.com/a/b").repo is None


def test_empty_input_never_raises():
    assert detect("   ").normalized == ""


def test_explicit_type_overrides_detection():
    """The user's override is honoured literally — `Foo.cs` as a KEYWORD searches file
    contents, even though auto-detection would have called it a filename."""
    assert normalize("Foo.cs", SearchType.KEYWORD).normalized == "Foo.cs"
    assert normalize("Foo.cs", SearchType.AUTO).normalized == "filename:Foo.cs"


def test_every_detection_explains_itself():
    for text in ("HighlightPlus", "Foo.cs", "a/b/c", ".cs", '"x y"', "github.com/a/b"):
        assert detect(text).explanation.strip()
