"""Unit tests that do not call the YouTube Music API."""

from __future__ import annotations

import tempfile
from pathlib import Path

import ytm_purge


def test_matches_true_when_artist_in_targets() -> None:
    track = {"artists": [{"id": "ch1", "name": "A"}, {"id": "ch2", "name": "B"}]}
    assert ytm_purge.matches(track, {"ch1"})
    assert ytm_purge.matches(track, {"ch2"})
    assert ytm_purge.matches(track, {"ch1", "ch999"})


def test_matches_false_when_no_overlap() -> None:
    track = {"artists": [{"id": "ch1", "name": "A"}]}
    assert not ytm_purge.matches(track, {"ch2"})
    assert not ytm_purge.matches(track, set())


def test_matches_empty_artists() -> None:
    assert not ytm_purge.matches({"artists": []}, {"ch1"})
    assert not ytm_purge.matches({"title": "x"}, {"ch1"})


def test_read_marked_truthy_rows() -> None:
    content = (
        "delete,name,channel_id\n"
        "1,One,ch1\n"
        ",Keep,ch2\n"
        "YES,Three,ch3\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        path = f.name
    try:
        rows = ytm_purge.read_marked(path)
        assert len(rows) == 2
        ids = {r["channel_id"] for r in rows}
        assert ids == {"ch1", "ch3"}
    finally:
        Path(path).unlink()
