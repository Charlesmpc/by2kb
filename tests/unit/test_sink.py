import pytest

from pathlib import Path

from by2kb.sinks.filesystem import FilesystemSink


@pytest.fixture
def staging(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    return staging_dir


async def test_publish_places_artifacts_under_platform_video_dir(tmp_path, staging):
    raw = staging / "raw.标题.md"
    raw.write_text("body", encoding="utf-8")
    source = staging / "source.json"
    source.write_text("{}", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    receipt = await sink.publish(
        {"raw_md": raw, "source_json": source},
        platform="bilibili",
        video_id="BV1xx411c7mD",
    )
    assert Path(receipt.target) == tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    assert receipt.artifacts["raw_md"].endswith("raw.标题.md")
    assert receipt.artifacts["source_json"].endswith("source.json")


async def test_publish_retires_legacy_and_previous_title_files(tmp_path, staging):
    target_dir = tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    target_dir.mkdir(parents=True)
    legacy_raw = target_dir / "raw.md"
    legacy_raw.write_text("old", encoding="utf-8")
    old_title_raw = target_dir / "旧标题-BV1xx411c7mD.raw.md"
    old_title_raw.write_text("old", encoding="utf-8")
    old_prefix_raw = target_dir / "raw.旧标题.md"
    old_prefix_raw.write_text("old", encoding="utf-8")
    legacy_updated = target_dir / "updated.md"
    legacy_updated.write_text("old", encoding="utf-8")

    new_raw = staging / "raw.新标题.md"
    new_raw.write_text("new", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    await sink.publish({"raw_md": new_raw}, platform="bilibili", video_id="BV1xx411c7mD")

    assert not legacy_raw.exists()
    assert not old_title_raw.exists()
    assert not old_prefix_raw.exists()
    assert legacy_updated.exists()
    assert (target_dir / "raw.新标题.md").exists()


async def test_publish_retires_updated_kind_independently(tmp_path, staging):
    target_dir = tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    target_dir.mkdir(parents=True)
    old_updated = target_dir / "旧标题-BV1xx411c7mD.updated.md"
    old_updated.write_text("old", encoding="utf-8")
    old_long = target_dir / "long.旧标题.md"
    old_long.write_text("old", encoding="utf-8")
    kept_raw = target_dir / "raw.标题.md"
    kept_raw.write_text("keep", encoding="utf-8")

    new_updated = staging / "long.新标题.md"
    new_updated.write_text("new", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    await sink.publish(
        {"updated_md": new_updated}, platform="bilibili", video_id="BV1xx411c7mD"
    )

    assert not old_updated.exists()
    assert not old_long.exists()
    assert kept_raw.exists()
    assert (target_dir / "long.新标题.md").exists()


@pytest.mark.parametrize(
    ("publish_kind", "new_name", "legacy_suffix", "current_prefix"),
    [
        ("raw_md", "raw.新标题.md", "raw", "raw"),
        ("abstract_md", "short.新标题.md", "abstract", "short"),
        ("updated_md", "long.新标题.md", "updated", "long"),
    ],
)
async def test_legacy_suffix_cleanup_preserves_new_other_kinds(
    tmp_path, staging, publish_kind, new_name, legacy_suffix, current_prefix
):
    target_dir = tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    target_dir.mkdir(parents=True)
    current_files = {}
    for prefix in ("raw", "short", "long"):
        path = target_dir / f"{prefix}.Release.{legacy_suffix}.md"
        path.write_text(prefix, encoding="utf-8")
        current_files[prefix] = path
    old_legacy = target_dir / f"旧标题-BV1xx411c7mD.{legacy_suffix}.md"
    old_legacy.write_text("old", encoding="utf-8")
    new_artifact = staging / new_name
    new_artifact.write_text("new", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    await sink.publish(
        {publish_kind: new_artifact}, platform="bilibili", video_id="BV1xx411c7mD"
    )

    for prefix, path in current_files.items():
        assert path.exists() is (prefix != current_prefix)
    assert not old_legacy.exists()
    assert (target_dir / new_name).exists()


async def test_publish_retires_abstract_kind_independently(tmp_path, staging):
    target_dir = tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    target_dir.mkdir(parents=True)
    old_abstract = target_dir / "旧标题-BV1xx411c7mD.abstract.md"
    old_abstract.write_text("old", encoding="utf-8")
    old_short = target_dir / "short.旧标题.md"
    old_short.write_text("old", encoding="utf-8")
    kept_updated = target_dir / "long.标题.md"
    kept_updated.write_text("keep", encoding="utf-8")

    new_abstract = staging / "short.新标题.md"
    new_abstract.write_text("new", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    await sink.publish(
        {"abstract_md": new_abstract},
        platform="bilibili",
        video_id="BV1xx411c7mD",
    )

    assert not old_abstract.exists()
    assert not old_short.exists()
    assert kept_updated.exists()
    assert (target_dir / "short.新标题.md").exists()
