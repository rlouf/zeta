"""Filesystem event connector tests."""

import os
from pathlib import Path

from connectors import IngressBinding
from connectors.filesystem import (
    FILE_CREATED,
    collect_file_created,
    filesystem_event_connector,
)


def make_file(directory: Path, name: str, *, mtime: float, data: bytes = b"x") -> Path:
    path = directory / name
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def binding(directory: Path, glob: str | None = None) -> IngressBinding:
    filter_: dict[str, str] = {"dir": str(directory)}
    if glob is not None:
        filter_["glob"] = glob
    return IngressBinding(FILE_CREATED, filter=filter_)


def test_filesystem_emits_for_file_created_after_watermark(tmp_path):
    state: dict = {}
    collect_file_created(binding(tmp_path), state, now=1000.0, debounce_seconds=2.0)

    make_file(tmp_path, "note.md", mtime=1005.0)
    drafts = collect_file_created(
        binding(tmp_path), state, now=1010.0, debounce_seconds=2.0
    )

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.event_type == FILE_CREATED
    assert draft.source == "filesystem"
    assert draft.payload["name"] == "note.md"
    assert draft.payload["path"].endswith("note.md")
    assert draft.payload["dir"] == str(tmp_path)


def test_filesystem_ignores_pre_watermark_file(tmp_path):
    make_file(tmp_path, "old.md", mtime=500.0)
    state: dict = {}

    drafts = collect_file_created(
        binding(tmp_path), state, now=1000.0, debounce_seconds=0.0
    )

    assert drafts == ()


def test_filesystem_honors_glob_filter(tmp_path):
    state: dict = {}
    collect_file_created(
        binding(tmp_path, "*.md"), state, now=1000.0, debounce_seconds=0.0
    )
    make_file(tmp_path, "a.md", mtime=1005.0)
    make_file(tmp_path, "b.txt", mtime=1005.0)

    drafts = collect_file_created(
        binding(tmp_path, "*.md"), state, now=1010.0, debounce_seconds=0.0
    )

    assert len(drafts) == 1
    assert drafts[0].payload["name"] == "a.md"


def test_filesystem_does_not_reemit_seen_file(tmp_path):
    state: dict = {}
    collect_file_created(binding(tmp_path), state, now=1000.0, debounce_seconds=0.0)
    make_file(tmp_path, "a.md", mtime=1005.0)
    collect_file_created(binding(tmp_path), state, now=1010.0, debounce_seconds=0.0)

    drafts = collect_file_created(
        binding(tmp_path), state, now=1020.0, debounce_seconds=0.0
    )

    assert drafts == ()


def test_filesystem_skips_file_still_being_written(tmp_path):
    state: dict = {}
    collect_file_created(binding(tmp_path), state, now=1000.0, debounce_seconds=5.0)
    make_file(tmp_path, "a.md", mtime=1008.0)

    assert (
        collect_file_created(binding(tmp_path), state, now=1010.0, debounce_seconds=5.0)
        == ()
    )
    settled = collect_file_created(
        binding(tmp_path), state, now=1020.0, debounce_seconds=5.0
    )
    assert len(settled) == 1


def test_filesystem_factory_exposes_event_and_polled_ingress():
    connector = filesystem_event_connector()

    assert connector.id == "filesystem"
    assert FILE_CREATED in connector.events
    assert FILE_CREATED in connector.ingress
    assert FILE_CREATED in connector.filters
    assert connector.push_ingress is None
    assert connector.egress == {}
