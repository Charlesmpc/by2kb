import os

import pytest

from by2kb.filenames import (
    MAX_TITLE_CHARS,
    artifact_basename,
    markdown_artifact_name,
    sanitize_title,
)


def test_plain_title_passes_through():
    assert sanitize_title("世界经济危机真的要来了吗？") == "世界经济危机真的要来了吗？"


@pytest.mark.parametrize(
    "raw",
    [
        "a/b\\c",
        "a:b*c?d\"e<f>g|h",
        "line\nbreak\rand\ttab",
        "\x00\x1fcontrol",
    ],
)
def test_unsafe_characters_are_removed(raw):
    result = sanitize_title(raw)
    assert "\\" not in result and "/" not in result
    assert not any(ord(ch) < 0x20 for ch in result)
    assert result == result.strip()


def test_trailing_dots_and_spaces_are_stripped():
    assert sanitize_title("title... ") == "title"
    assert sanitize_title("  ...  ") == "untitled"


def test_windows_reserved_names_are_prefixed():
    assert sanitize_title("CON") == "_CON"
    assert sanitize_title("com1.txt") == "_com1.txt"
    assert sanitize_title("NUL") == "_NUL"


def test_empty_title_becomes_untitled():
    assert sanitize_title("") == "untitled"
    assert sanitize_title(None) == "untitled"


def test_long_titles_truncate_deterministically():
    long_title = "长" * 500
    result = sanitize_title(long_title)
    assert len(result) <= MAX_TITLE_CHARS
    assert result == sanitize_title(long_title)


def test_no_path_escape():
    malicious = "../../etc/passwd"
    result = sanitize_title(malicious)
    assert os.sep not in result and "/" not in result
    name = markdown_artifact_name(malicious, "BV1xx411c7mD", "raw")
    assert "/" not in name and "\\" not in name and ".." not in name.replace(
        " ", ""
    ).split("-")[0]


def test_basename_keeps_video_identity():
    base = artifact_basename("世界经济危机真的要来了吗？", "BV1Xzju6sEY9")
    assert base.endswith("-BV1Xzju6sEY9")
    assert base.startswith("世界经济危机真的要来了吗？")


def test_markdown_artifact_names_are_distinct():
    raw = markdown_artifact_name("标题", "BV1xx411c7mD", "raw")
    updated = markdown_artifact_name("标题", "BV1xx411c7mD", "updated")
    assert raw == "标题-BV1xx411c7mD.raw.md"
    assert updated == "标题-BV1xx411c7mD.updated.md"
    assert raw != updated


def test_markdown_artifact_name_rejects_unknown_kind():
    with pytest.raises(ValueError):
        markdown_artifact_name("标题", "BV1xx411c7mD", "source")
