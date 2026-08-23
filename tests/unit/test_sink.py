import pytest

from pathlib import Path

from by2kb.sinks.filesystem import FilesystemSink


@pytest.fixture
def staging(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    return staging_dir


async def test_publish_places_artifacts_under_platform_video_dir(tmp_path, staging):
    raw = staging / "标题-BV1xx411c7mD.raw.md"
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
    assert receipt.artifacts["raw_md"].endswith("标题-BV1xx411c7mD.raw.md")
    assert receipt.artifacts["source_json"].endswith("source.json")


async def test_publish_retires_legacy_and_previous_title_files(tmp_path, staging):
    target_dir = tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    target_dir.mkdir(parents=True)
    legacy_raw = target_dir / "raw.md"
    legacy_raw.write_text("old", encoding="utf-8")
    old_title_raw = target_dir / "旧标题-BV1xx411c7mD.raw.md"
    old_title_raw.write_text("old", encoding="utf-8")
    legacy_updated = target_dir / "updated.md"
    legacy_updated.write_text("old", encoding="utf-8")

    new_raw = staging / "新标题-BV1xx411c7mD.raw.md"
    new_raw.write_text("new", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    await sink.publish({"raw_md": new_raw}, platform="bilibili", video_id="BV1xx411c7mD")

    assert not legacy_raw.exists()
    assert not old_title_raw.exists()
    assert legacy_updated.exists()
    assert (target_dir / "新标题-BV1xx411c7mD.raw.md").exists()


async def test_publish_retires_updated_kind_independently(tmp_path, staging):
    target_dir = tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    target_dir.mkdir(parents=True)
    old_updated = target_dir / "旧标题-BV1xx411c7mD.updated.md"
    old_updated.write_text("old", encoding="utf-8")
    kept_raw = target_dir / "标题-BV1xx411c7mD.raw.md"
    kept_raw.write_text("keep", encoding="utf-8")

    new_updated = staging / "新标题-BV1xx411c7mD.updated.md"
    new_updated.write_text("new", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    await sink.publish(
        {"updated_md": new_updated}, platform="bilibili", video_id="BV1xx411c7mD"
    )

    assert not old_updated.exists()
    assert kept_raw.exists()
    assert (target_dir / "新标题-BV1xx411c7mD.updated.md").exists()


async def test_publish_retires_abstract_kind_independently(tmp_path, staging):
    target_dir = tmp_path / "library" / "bilibili" / "BV1xx411c7mD"
    target_dir.mkdir(parents=True)
    old_abstract = target_dir / "旧标题-BV1xx411c7mD.abstract.md"
    old_abstract.write_text("old", encoding="utf-8")
    kept_updated = target_dir / "标题-BV1xx411c7mD.updated.md"
    kept_updated.write_text("keep", encoding="utf-8")

    new_abstract = staging / "新标题-BV1xx411c7mD.abstract.md"
    new_abstract.write_text("new", encoding="utf-8")

    sink = FilesystemSink(tmp_path / "library")
    await sink.publish(
        {"abstract_md": new_abstract},
        platform="bilibili",
        video_id="BV1xx411c7mD",
    )

    assert not old_abstract.exists()
    assert kept_updated.exists()
    assert (target_dir / "新标题-BV1xx411c7mD.abstract.md").exists()
