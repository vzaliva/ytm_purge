"""Unit tests that do not call the YouTube Music API."""

from __future__ import annotations

import tempfile
from pathlib import Path

import ytm_purge


def test_coerce_browser_auth_adds_sapisid_placeholder_when_missing() -> None:
    raw = {"cookie": "a=b", "x-goog-authuser": "0"}
    out = ytm_purge._coerce_browser_auth_dict(raw)
    assert out is not None
    assert "SAPISIDHASH" in out["authorization"]
    assert out["cookie"] == "a=b"


def test_coerce_browser_auth_noop_when_sapisid_present() -> None:
    raw = {
        "cookie": "a=b",
        "x-goog-authuser": "0",
        "authorization": "SAPISIDHASH 123_abc",
    }
    assert ytm_purge._coerce_browser_auth_dict(raw) is None


def test_coerce_browser_auth_noop_without_browser_shape() -> None:
    assert ytm_purge._coerce_browser_auth_dict({"access_token": "x"}) is None


def test_sanitize_auth_header_keys_drops_junk_request_line_key() -> None:
    raw = {
        "cookie": "a=b",
        "POST /api/foo?x=1": "",
        "x-goog-authuser": "0",
    }
    out = ytm_purge._sanitize_auth_header_keys(raw)
    assert "POST /api/foo?x=1" not in out
    assert out["cookie"] == "a=b"


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


def test_read_keep_channel_ids() -> None:
    content = "name,channel_id,songs\nA,ch1,1\nB,ch2,2\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        path = f.name
    try:
        assert ytm_purge.read_keep_channel_ids(path) == {"ch1", "ch2"}
    finally:
        Path(path).unlink()


def test_read_keep_channel_ids_case_insensitive_header() -> None:
    content = "name,Channel_ID\nX,id1\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        path = f.name
    try:
        assert ytm_purge.read_keep_channel_ids(path) == {"id1"}
    finally:
        Path(path).unlink()


def test_read_keep_ignores_blank_channel_id_rows() -> None:
    content = "name,channel_id\nA,ch1\nB,\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        path = f.name
    try:
        assert ytm_purge.read_keep_channel_ids(path) == {"ch1"}
    finally:
        Path(path).unlink()


def test_read_keep_header_only_yields_empty() -> None:
    content = "name,channel_id\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        path = f.name
    try:
        assert ytm_purge.read_keep_channel_ids(path) == set()
    finally:
        Path(path).unlink()


def test_read_keep_missing_channel_id_column() -> None:
    content = "name,x\na,b\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        path = f.name
    try:
        try:
            ytm_purge.read_keep_channel_ids(path)
        except ValueError as e:
            assert "channel_id" in str(e)
        else:
            raise AssertionError("expected ValueError")
    finally:
        Path(path).unlink()


def test_library_remove_token_from_song_when_present() -> None:
    class _NoWatch:
        def get_watch_playlist(self, **kwargs):
            raise AssertionError("should not need watch playlist")

    song = {"videoId": "v1", "feedbackTokens": {"remove": "direct-token"}}
    assert ytm_purge._library_remove_feedback_token(_NoWatch(), song) == "direct-token"


def test_library_remove_token_fetched_from_watch_playlist() -> None:
    class _Yt:
        def get_watch_playlist(self, videoId=None, **kwargs):
            return {
                "tracks": [{"videoId": "v1", "feedbackTokens": {"remove": "wp-token"}}]
            }

    song = {"videoId": "v1", "title": "Track"}
    assert ytm_purge._library_remove_feedback_token(_Yt(), song) == "wp-token"


def test_library_remove_token_via_counterpart_watch_id() -> None:
    calls: list[str | None] = []

    class _Yt:
        def get_watch_playlist(self, videoId=None, **kwargs):
            calls.append(videoId)
            if videoId == "main":
                return {
                    "tracks": [
                        {
                            "videoId": "main",
                            "counterpart": {"videoId": "alt"},
                        }
                    ]
                }
            if videoId == "alt":
                return {
                    "tracks": [
                        {
                            "videoId": "alt",
                            "feedbackTokens": {"remove": "tok-alt"},
                        }
                    ]
                }
            return {"tracks": []}

    song = {"videoId": "main"}
    assert ytm_purge._library_remove_feedback_token(_Yt(), song) == "tok-alt"
    assert "main" in calls and "alt" in calls


def test_library_remove_token_from_album_track() -> None:
    class _Yt:
        def get_watch_playlist(self, **kwargs):
            return {"tracks": []}

        def get_album(self, browseId):
            assert browseId == "MPREb_x"
            return {
                "tracks": [
                    {
                        "videoId": "v1",
                        "feedbackTokens": {"remove": "album-remove"},
                    }
                ]
            }

    song = {
        "videoId": "v1",
        "album": {"id": "MPREb_x", "name": "Album"},
    }
    cache: dict[str, list[dict]] = {}
    assert (
        ytm_purge._library_remove_feedback_token(_Yt(), song, cache) == "album-remove"
    )
    assert "MPREb_x" in cache


def test_dedupe_liked_tracks_prefers_set_video_id() -> None:
    tracks = [
        {"videoId": "a", "title": "first"},
        {"videoId": "a", "title": "second", "setVideoId": "sv1"},
    ]
    d = ytm_purge._dedupe_liked_tracks(tracks)
    assert len(d) == 1
    assert d[0]["setVideoId"] == "sv1"


def test_dedupe_liked_drops_missing_videoid() -> None:
    assert ytm_purge._dedupe_liked_tracks([{"title": "x"}]) == []


def test_playlist_remove_succeeded() -> None:
    assert ytm_purge._playlist_remove_succeeded("STATUS_SUCCEEDED")
    assert ytm_purge._playlist_remove_succeeded({"status": "STATUS_SUCCEEDED"})
    assert not ytm_purge._playlist_remove_succeeded("FAILED")


def test_merge_playlist_edit_pairs_nested_menu() -> None:
    payload = {
        "deep": {
            "menu": {
                "playlistEditEndpoint": {
                    "actions": [
                        {
                            "setVideoId": "sv99",
                            "removedVideoId": "vidA",
                            "action": "ACTION_REMOVE_VIDEO",
                        }
                    ]
                }
            }
        },
        "other": [
            {
                "playlistEditEndpoint": {
                    "actions": [{"setVideoId": "sv2", "removedVideoId": "vidB"}]
                }
            }
        ],
    }
    out: dict[str, str] = {}
    ytm_purge._merge_playlist_edit_pairs_recursive(payload, out)
    assert out == {"vidA": "sv99", "vidB": "sv2"}


def test_merge_playlist_edit_pairs_accepts_video_id_field() -> None:
    payload = {
        "playlistEditEndpoint": {
            "actions": [
                {"setVideoId": "sv1", "videoId": "vidZ", "action": "ACTION_REMOVE_VIDEO"}
            ]
        }
    }
    out: dict[str, str] = {}
    ytm_purge._merge_playlist_edit_pairs_recursive(payload, out)
    assert out == {"vidZ": "sv1"}


def test_rate_song_video_id_candidates_includes_counterpart() -> None:
    class _Yt:
        def get_watch_playlist(self, videoId=None, **kwargs):
            assert videoId == "main"
            return {
                "tracks": [
                    {
                        "videoId": "main",
                        "counterpart": {"videoId": "alt"},
                    }
                ]
            }

    assert ytm_purge._rate_song_video_id_candidates(_Yt(), "main") == ["main", "alt"]


def test_rate_song_video_id_candidates_watch_error_keeps_primary() -> None:
    class _Yt:
        def get_watch_playlist(self, **kwargs):
            raise RuntimeError("network")

    assert ytm_purge._rate_song_video_id_candidates(_Yt(), "x") == ["x"]
