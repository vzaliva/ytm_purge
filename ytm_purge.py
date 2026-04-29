#!/usr/bin/env python3
"""
ytm_purge.py — semi-automated artist-level purge of a YouTube Music library.

Workflow
--------
1.  uv run python ytm_purge.py inventory [--auth browser.json] --out artists.csv
        Writes one row per artist in your library (songs, likes, albums,
        subscriptions), sorted by footprint.

2.  Edit artists.csv: delete entire rows for artists you do **not** want to keep.
        The remaining file is your **keep list** (do not remove the header row).

3.  uv run python ytm_purge.py delete [--auth browser.json] --in artists.csv [--dry-run]
        Re-scans your library like inventory. Every artist **present in the
        library** whose channel_id is **not** listed in the CSV is purged:
        library songs, likes, saved albums, and channel unsubscribe.
        Use --dry-run first. Artists added to the library after you edited
        the CSV but not added to the file will also be purged — refresh the
        CSV or merge new channel_ids if you intend to keep them.

Auth
----
Run `uv run ytmusicapi browser` once and follow the prompts to create
`browser.json` (or pass another path via `--auth`). See:
https://ytmusicapi.readthedocs.io/en/stable/usage/setup.html

Caveats
-------
- Does not touch user-created playlists. Trivial to add if needed.
- Does not prune watch history (recommendations / radio still draw on
  it). Use myactivity.google.com filtered to YouTube Music for that.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from ytmusicapi import YTMusic
from ytmusicapi.continuations import CONTINUATION_ITEMS, get_continuation_token
from ytmusicapi.navigation import CONTENT, SECTION, TWO_COLUMN_RENDERER, nav

# RFC 9110 `tchar` — invalid keys break `requests` (e.g. a pasted "POST /path?..." line).
_VALID_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.0-9A-Za-z^_`|~]+$")


def _sanitize_auth_header_keys(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if _VALID_HEADER_NAME.fullmatch(str(k))}


def _coerce_browser_auth_dict(data: dict[str, Any]) -> dict[str, Any] | None:
    """ytmusicapi treats auth as browser only if Authorization contains SAPISIDHASH.
    Header exports (e.g. Firefox) often omit Authorization; cookies are still valid.
    Returns an updated dict copy, or None if no coercion applies."""
    lower = {str(k).lower() for k in data}
    if "cookie" not in lower or "x-goog-authuser" not in lower:
        return None
    authz = data.get("authorization")
    if authz and "SAPISIDHASH" in str(authz):
        return None
    return {**data, "authorization": "SAPISIDHASH 0_placeholder"}


def ytmusic_from_auth(auth: str) -> YTMusic:
    path = Path(auth)
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "cookie" in {str(k).lower() for k in raw}:
            cleaned = _sanitize_auth_header_keys(raw)
            fixed = _coerce_browser_auth_dict(cleaned)
            return YTMusic(fixed if fixed is not None else cleaned)
    return YTMusic(auth)


def collect_artists(yt: YTMusic) -> dict[str, dict]:
    """Aggregate every artist channel touching the library."""
    idx: dict[str, dict] = defaultdict(lambda: {
        "name": None,
        "songs": 0,
        "liked": 0,
        "albums": 0,
        "subscribed": False,
    })

    def bump(cid: str | None, name: str | None, key: str) -> None:
        if not cid:
            return
        idx[cid]["name"] = name or idx[cid]["name"]
        idx[cid][key] += 1

    for s in yt.get_library_songs(limit=10_000) or []:
        for a in s.get("artists") or []:
            bump(a.get("id"), a.get("name"), "songs")

    liked = yt.get_liked_songs(limit=None) or {}
    for s in _dedupe_liked_tracks(liked.get("tracks", [])):
        for a in s.get("artists") or []:
            bump(a.get("id"), a.get("name"), "liked")

    for alb in yt.get_library_albums(limit=10_000) or []:
        for a in alb.get("artists") or []:
            bump(a.get("id"), a.get("name"), "albums")

    for a in yt.get_library_subscriptions(limit=10_000) or []:
        cid = a.get("browseId")
        if cid:
            idx[cid]["name"] = a.get("artist") or idx[cid]["name"]
            idx[cid]["subscribed"] = True

    return idx


def write_csv(idx: dict[str, dict], path: str) -> None:
    rows = sorted(
        idx.items(),
        key=lambda kv: kv[1]["songs"] + kv[1]["liked"] + kv[1]["albums"],
        reverse=True,
    )
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "channel_id", "songs", "liked", "albums", "subscribed"])
        for cid, m in rows:
            w.writerow([
                m["name"] or "",
                cid,
                m["songs"],
                m["liked"],
                m["albums"],
                int(m["subscribed"]),
            ])


def read_keep_channel_ids(path: str) -> set[str]:
    """channel_id values from the keep-list CSV (one row per artist to retain)."""
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV is empty or has no header row.")
        cid_key = None
        for name in reader.fieldnames:
            if name.strip().lower() == "channel_id":
                cid_key = name
                break
        if cid_key is None:
            raise ValueError(
                "CSV must include a channel_id column. Found: "
                + ", ".join(reader.fieldnames)
            )
        out: set[str] = set()
        for row in reader:
            cid = (row.get(cid_key) or "").strip()
            if cid:
                out.add(cid)
    return out


# Liked Music is playlist "LM"; removing tracks requires edit_playlist + setVideoId.
# rate_song(…, INDIFFERENT) alone often leaves rows in LM, so the next run still lists them.
LIKED_SONGS_PLAYLIST_ID = "LM"
LM_REMOVE_CHUNK = 50


def matches(track: dict, target_ids: set[str]) -> bool:
    return any((a.get("id") in target_ids) for a in (track.get("artists") or []))


def _dedupe_liked_tracks(tracks: list[dict]) -> list[dict]:
    """One entry per videoId; prefer a row that includes setVideoId (needed for LM remove)."""
    by_vid: dict[str, dict] = {}
    for t in tracks:
        vid = t.get("videoId")
        if not vid:
            continue
        cur = by_vid.get(vid)
        if cur is None:
            by_vid[vid] = t
        elif t.get("setVideoId") and not cur.get("setVideoId"):
            by_vid[vid] = t
    return list(by_vid.values())


def _playlist_remove_succeeded(result: str | dict) -> bool:
    if isinstance(result, str):
        return "SUCCEEDED" in result.upper()
    return "SUCCEEDED" in str(result.get("status", "")).upper()


def _merge_playlist_edit_pairs_recursive(obj: Any, out: dict[str, str]) -> None:
    """Collect removedVideoId -> setVideoId from any playlistEditEndpoint in a JSON tree.

    ``ytmusicapi.parse_playlist_item`` only inspects certain menu paths; LM payloads
    occasionally nest the same data where a shallow parse misses it.
    """
    if isinstance(obj, dict):
        pep = obj.get("playlistEditEndpoint")
        if isinstance(pep, dict):
            for act in pep.get("actions") or []:
                if not isinstance(act, dict):
                    continue
                rid = act.get("removedVideoId") or act.get("videoId")
                svid = act.get("setVideoId")
                if rid and svid:
                    out[str(rid)] = str(svid)
        for v in obj.values():
            _merge_playlist_edit_pairs_recursive(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _merge_playlist_edit_pairs_recursive(item, out)


def _lm_video_setvideoid_map(yt: YTMusic) -> dict[str, str]:
    """videoId -> setVideoId for Liked Music, using raw browse/continuation payloads."""
    mapping: dict[str, str] = {}
    send = yt._send_request  # type: ignore[attr-defined]
    response = send("browse", {"browseId": "VLLM"})
    _merge_playlist_edit_pairs_recursive(response, mapping)

    section_list = nav(
        response, [*TWO_COLUMN_RENDERER, "secondaryContents", *SECTION], True
    )
    if not section_list:
        return mapping
    content_data = nav(section_list, [*CONTENT, "musicPlaylistShelfRenderer"], True)
    if not content_data:
        return mapping
    contents = content_data.get("contents")
    if not isinstance(contents, list):
        return mapping

    continuation_token = get_continuation_token(contents)
    while continuation_token:
        cont_resp = send("browse", {"continuation": continuation_token})
        _merge_playlist_edit_pairs_recursive(cont_resp, mapping)
        continuation_items = nav(cont_resp, CONTINUATION_ITEMS, True)
        if not continuation_items:
            break
        continuation_token = get_continuation_token(continuation_items)

    return mapping


def _enrich_liked_tracks_setvideoid_from_lm(yt: YTMusic, tracks: list[dict]) -> None:
    """Fill missing setVideoId on LM rows so remove_playlist_items can run."""
    if not any(t.get("videoId") and not t.get("setVideoId") for t in tracks):
        return
    try:
        m = _lm_video_setvideoid_map(yt)
    except Exception:
        return
    for t in tracks:
        if t.get("setVideoId"):
            continue
        vid = t.get("videoId")
        if vid and vid in m:
            t["setVideoId"] = m[vid]


def _library_remove_feedback_token(
    yt: YTMusic,
    song: dict,
    album_tracks_cache: dict[str, list[dict]] | None = None,
) -> str | None:
    """Resolve feedback token to remove a song from the library.

    ``get_library_songs`` rows sometimes omit ``feedbackTokens``; the watch
    playlist for the same video usually includes them. Song vs video variants
    can differ, so we try the same id candidates as LM ``rate_song`` fallback.

    When the watch queue still omits tokens (common for some uploads / features),
    ``get_album`` track rows for the same ``MPRE`` release often still expose
    library remove tokens.
    """
    ft = (song.get("feedbackTokens") or {}).get("remove")
    if ft:
        return str(ft)
    vid = song.get("videoId")
    if not vid:
        return None
    primary = str(vid)
    for cand in _rate_song_video_id_candidates(yt, primary):
        try:
            wp = yt.get_watch_playlist(videoId=str(cand))
            for t in wp.get("tracks") or []:
                tid = t.get("videoId")
                if tid not in (primary, cand):
                    continue
                tok = (t.get("feedbackTokens") or {}).get("remove")
                if tok:
                    return str(tok)
        except Exception:
            continue

    album = song.get("album")
    if isinstance(album, dict):
        bid = album.get("id")
        if bid and str(bid).startswith("MPRE"):
            tracks: list[dict] = []
            bkey = str(bid)
            try:
                if album_tracks_cache is not None:
                    if bkey not in album_tracks_cache:
                        alb = yt.get_album(bkey)
                        album_tracks_cache[bkey] = alb.get("tracks") or []
                    tracks = album_tracks_cache[bkey]
                else:
                    alb = yt.get_album(bkey)
                    tracks = alb.get("tracks") or []
            except Exception:
                if album_tracks_cache is not None:
                    album_tracks_cache[bkey] = []
            for t in tracks:
                if t.get("videoId") != primary:
                    continue
                tok = (t.get("feedbackTokens") or {}).get("remove")
                if tok:
                    return str(tok)
    return None


def _rate_song_video_id_candidates(yt: YTMusic, video_id: str) -> list[str]:
    """Ids to try with ``rate_song(..., INDIFFERENT)`` for one LM row.

    Liked Music rows sometimes list one watch id while ``like/removelike`` targets
    another (song vs music-video variant). ``get_watch_playlist`` exposes the
    paired ``counterpart`` when it exists (ytmusicapi issue #453).
    """
    seen: set[str] = set()
    ordered: list[str] = []

    def add(v: str | None) -> None:
        if v and v not in seen:
            seen.add(v)
            ordered.append(v)

    add(video_id)
    try:
        wp = yt.get_watch_playlist(videoId=str(video_id))
        tracks = wp.get("tracks") or []
        if tracks:
            t0 = tracks[0]
            add(t0.get("videoId"))
            cp = t0.get("counterpart")
            if isinstance(cp, dict):
                add(cp.get("videoId"))
    except Exception:
        pass
    return ordered


def _count_json_key_occurrences(obj: Any, key: str) -> int:
    """Count how many times ``key`` appears as a dict key anywhere in a JSON-like tree."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                n += 1
            n += _count_json_key_occurrences(v, key)
    elif isinstance(obj, list):
        for item in obj:
            n += _count_json_key_occurrences(item, key)
    return n


def _debug_print_delete_snapshot(
    yt: YTMusic,
    songs_to_unsave: list[dict],
    likes_to_unlike: list[dict],
) -> None:
    """Print one-shot API diagnostics to stdout. Pair with ``--dry-run`` to avoid mutations."""
    print("\n=== DEBUG (API snapshot) ===", flush=True)
    if songs_to_unsave:
        s0 = songs_to_unsave[0]
        vid = s0.get("videoId")
        print(f"[library] sample title: {s0.get('title')!r}", flush=True)
        print(f"[library] sample keys: {sorted(s0.keys())}", flush=True)
        print(f"[library] feedbackTokens: {s0.get('feedbackTokens')!r}", flush=True)
        print(f"[library] videoId: {vid!r}", flush=True)
        if vid:
            cands = _rate_song_video_id_candidates(yt, str(vid))
            print(f"[library] watch id candidates: {cands}", flush=True)
            for i, cand in enumerate(cands):
                try:
                    wp = yt.get_watch_playlist(videoId=str(cand))
                    tr = wp.get("tracks") or []
                    print(
                        f"[library] get_watch_playlist[{i}] id={cand!r} n_tracks={len(tr)}",
                        flush=True,
                    )
                    for j, t in enumerate(tr[:5]):
                        r = (t.get("feedbackTokens") or {}).get("remove")
                        print(
                            f"    [{j}] videoId={t.get('videoId')!r} "
                            f"has_remove_token={bool(r)} likeStatus={t.get('likeStatus')!r}",
                            flush=True,
                        )
                except Exception as e:
                    print(f"[library] get_watch_playlist[{i}] error: {e!r}", flush=True)
            alb = s0.get("album")
            if isinstance(alb, dict) and str(alb.get("id") or "").startswith("MPRE"):
                bid = str(alb["id"])
                try:
                    al = yt.get_album(bid)
                    tr = al.get("tracks") or []
                    hit = next((x for x in tr if x.get("videoId") == vid), None)
                    print(
                        f"[library] get_album({bid!r}) n_tracks={len(tr)} "
                        f"match_has_remove={bool((hit.get('feedbackTokens') or {}).get('remove')) if hit else False}",
                        flush=True,
                    )
                    if hit is not None:
                        print(
                            f"[library]   album row inLibrary={hit.get('inLibrary')!r} "
                            f"(library shelf can disagree with album/search UIs)",
                            flush=True,
                        )
                except Exception as e:
                    print(f"[library] get_album error: {e!r}", flush=True)
    if likes_to_unlike:
        l0 = likes_to_unlike[0]
        print(f"[LM] sample keys: {sorted(l0.keys())}", flush=True)
        print(
            f"[LM] setVideoId={l0.get('setVideoId')!r} videoId={l0.get('videoId')!r}",
            flush=True,
        )
    try:
        m = _lm_video_setvideoid_map(yt)
        sample = list(m.items())[:5]
        print(f"[LM] deep-scan map size={len(m)} sample={sample!r}", flush=True)
    except Exception as e:
        print(f"[LM] deep-scan map error: {e!r}", flush=True)
    try:
        send = yt._send_request  # type: ignore[attr-defined]
        raw = send("browse", {"browseId": "VLLM"})
        n_pe = _count_json_key_occurrences(raw, "playlistEditEndpoint")
        n_mrlir = _count_json_key_occurrences(raw, "musicResponsiveListItemRenderer")
        print(
            "[LM] raw VLLM browse (1st page): "
            f"playlistEditEndpoint={n_pe} "
            f"musicResponsiveListItemRenderer={n_mrlir}",
            flush=True,
        )
    except Exception as e:
        print(f"[LM] raw VLLM browse error: {e!r}", flush=True)
    print(
        "[hint] If playlistEditEndpoint=0 and library rows lack remove tokens (watch + "
        "album + search), the mobile/desktop app must remove those saves; the Data API "
        "often omits the same controls for some releases.",
        flush=True,
    )
    print("=== END DEBUG ===\n", flush=True)


def delete_artists(yt: YTMusic, target_ids: set[str], dry_run: bool, debug: bool = False) -> None:
    if not target_ids:
        print("No artists to remove.")
        return

    library_songs = yt.get_library_songs(limit=10_000) or []
    likes_raw = _dedupe_liked_tracks(
        (yt.get_liked_songs(limit=None) or {}).get("tracks", [])
    )
    likes_to_unlike = [s for s in likes_raw if matches(s, target_ids)]
    _enrich_liked_tracks_setvideoid_from_lm(yt, likes_to_unlike)
    albums = yt.get_library_albums(limit=10_000) or []

    songs_to_unsave = [s for s in library_songs if matches(s, target_ids)]
    albums_to_unsave = [a for a in albums if matches(a, target_ids)]

    subs = yt.get_library_subscriptions(limit=10_000) or []
    subscribed_ids = {a.get("browseId") for a in subs if a.get("browseId")}
    unsub_targets = target_ids & subscribed_ids

    print("Plan:")
    print(f"  artists targeted:        {len(target_ids)}")
    print(f"  remove from library:     {len(songs_to_unsave)} songs")
    print(f"  unlike (Liked Music):    {len(likes_to_unlike)} unique video(s)")
    if likes_to_unlike:
        n_no_sv = sum(
            1 for s in likes_to_unlike
            if not (s.get("setVideoId") and s.get("videoId"))
        )
        if n_no_sv:
            print(
                f"    ({n_no_sv} without setVideoId — LM API often omits it; "
                "unlike uses rate_song, trying watch-playlist id variants per track)"
            )
    print(f"  remove saved albums:     {len(albums_to_unsave)}")
    print(f"  unsubscribe channels:    {len(unsub_targets)} subscribed (others need no unsubscribe)")
    print()
    if songs_to_unsave:
        n_prev = len(songs_to_unsave)
        prev_n = min(10, n_prev)
        print(
            f"Preview: first {prev_n} of {n_prev} song(s) to remove from library "
            "(not a separate approval step):"
        )
        for s in songs_to_unsave[:10]:
            artists = ", ".join(a.get("name", "") for a in (s.get("artists") or []))
            print(f"    - {s.get('title')} — {artists}")
    if debug:
        _debug_print_delete_snapshot(yt, songs_to_unsave, likes_to_unlike)
    if dry_run:
        print("\n[dry-run] no changes made.")
        return

    print(
        "\nOne confirmation applies to the whole plan: library, Liked Music, "
        "saved albums, and subscription cancels."
    )
    if input("\nProceed with full plan? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    print("\n--- Library (saved songs) ---")
    # Library removal: batch feedback tokens (resolve missing tokens via watch playlist).
    remove_tokens: list[str] = []
    library_no_token: list[dict] = []
    album_tracks_cache: dict[str, list[dict]] = {}
    for s in songs_to_unsave:
        tok = _library_remove_feedback_token(yt, s, album_tracks_cache)
        if tok:
            remove_tokens.append(tok)
        else:
            library_no_token.append(s)
    library_removed = 0
    if remove_tokens:
        try:
            yt.edit_song_library_status(remove_tokens)
            library_removed = len(remove_tokens)
            print(f"Removed {library_removed} song(s) from library.")
        except Exception as e:
            print(f"  ! library batch remove failed: {e}", file=sys.stderr)
    elif songs_to_unsave:
        print("No library-remove tokens resolved; nothing removed from saved songs.")
    if library_no_token:
        n_err = len(library_no_token)
        listed = min(10, n_err)
        print(
            f"  ! could not get library-remove token for {n_err} song(s); "
            f"listing {listed} of {n_err} — try removing them manually in the app",
            file=sys.stderr,
        )
        for s in library_no_token[:10]:
            print(f"      · {s.get('title')}", file=sys.stderr)

    lm_removed_ok = 0
    lm_playlist_err = 0
    like_fallback_ok = 0
    like_fallback_fail = 0
    like_no_vid = 0

    print("\n--- Liked Music ---")
    if not likes_to_unlike:
        print("No matching liked tracks to process.")
    else:
        need_fallback = [
            s for s in likes_to_unlike
            if not (s.get("setVideoId") and s.get("videoId"))
        ]
        with_setvid = [
            s for s in likes_to_unlike
            if s.get("setVideoId") and s.get("videoId")
        ]
        print(
            f"Removing {len(likes_to_unlike)} unique video(s) "
            f"({len(with_setvid)} via playlist LM, {len(need_fallback)} fallback)…",
            flush=True,
        )
        for i in range(0, len(with_setvid), LM_REMOVE_CHUNK):
            chunk = with_setvid[i : i + LM_REMOVE_CHUNK]
            try:
                res = yt.remove_playlist_items(LIKED_SONGS_PLAYLIST_ID, chunk)
                if _playlist_remove_succeeded(res):
                    lm_removed_ok += len(chunk)
                else:
                    lm_playlist_err += len(chunk)
                    print(
                        f"  ! LM playlist remove not confirmed for {len(chunk)} item(s): {res!r}",
                        file=sys.stderr,
                    )
            except Exception as e:
                lm_playlist_err += len(chunk)
                print(
                    f"  ! LM playlist remove failed ({len(chunk)} item(s)): {e}",
                    file=sys.stderr,
                )

        for s in need_fallback:
            vid = s.get("videoId")
            if not vid:
                like_no_vid += 1
                continue
            last_err: Exception | None = None
            ok = False
            for cand in _rate_song_video_id_candidates(yt, str(vid)):
                try:
                    yt.rate_song(cand, "INDIFFERENT")
                    ok = True
                    break
                except Exception as e:
                    last_err = e
            if ok:
                like_fallback_ok += 1
            else:
                like_fallback_fail += 1
                detail = f": {last_err}" if last_err else ""
                print(
                    f"  ! rate_song fallback failed for {s.get('title')}{detail}",
                    file=sys.stderr,
                )

        print(
            f"Liked Music result: LM playlist removed {lm_removed_ok} item(s)"
            + (f"; {lm_playlist_err} not confirmed / failed" if lm_playlist_err else "")
            + f"; rate_song fallback: {like_fallback_ok} ok, {like_fallback_fail} error(s), "
            f"{like_no_vid} no videoId."
        )

    print("\n--- Saved albums ---")
    albums_removed = 0
    for alb in albums_to_unsave:
        bid = alb.get("browseId")
        if not bid:
            continue
        try:
            yt.rate_playlist(bid, "INDIFFERENT")
            albums_removed += 1
        except Exception as e:
            err = str(e)
            if "404" in err:
                continue
            print(f"  ! album remove failed for {alb.get('title')}: {e}",
                  file=sys.stderr)
    if albums_to_unsave:
        print(f"Removed {albums_removed} saved album(s) "
              f"({len(albums_to_unsave) - albums_removed} skipped or already gone).")

    print("\n--- Artist channel subscriptions ---")
    unsub_ok = 0
    for cid in sorted(unsub_targets):
        try:
            yt.unsubscribe_artists([cid])
            unsub_ok += 1
        except Exception as e:
            print(f"  ! unsubscribe failed for channel {cid}: {e}", file=sys.stderr)
    if unsub_targets:
        print(f"Unsubscribed from {unsub_ok} of {len(unsub_targets)} channel(s).")
    else:
        print("No matching artist subscriptions to cancel.")

    leftover_likes = _dedupe_liked_tracks(
        (yt.get_liked_songs(limit=None) or {}).get("tracks", [])
    )
    leftover_likes = [s for s in leftover_likes if matches(s, target_ids)]
    if leftover_likes:
        print(
            f"\nWARNING: {len(leftover_likes)} liked track(s) still match purge targets "
            "after this run.",
            file=sys.stderr,
        )
        for s in leftover_likes[:25]:
            print(
                f"  · {s.get('title')} — videoId={s.get('videoId')}",
                file=sys.stderr,
            )

    print("\n=== Run summary ===")
    print(
        f"  Library (saved songs): {library_removed} removed; "
        f"{len(library_no_token)} left for manual removal (no token)"
    )
    if likes_to_unlike:
        print(
            f"  Liked Music: playlist removed {lm_removed_ok} item(s)"
            + (f", {lm_playlist_err} playlist error(s)" if lm_playlist_err else "")
            + f"; rate_song fallback {like_fallback_ok} ok / {like_fallback_fail} fail"
            f"; {like_no_vid} no videoId"
            + f"; {len(leftover_likes)} still in Liked Music for targeted artists"
        )
    else:
        print("  Liked Music: no matching tracks")
    print(
        f"  Saved albums: {albums_removed} removed "
        f"(of {len(albums_to_unsave)} planned)"
    )
    print(
        f"  Subscriptions: {unsub_ok}/{len(unsub_targets)} unsubscribed"
        if unsub_targets
        else "  Subscriptions: none targeted"
    )

    print("\nDone.")


def main() -> None:
    auth_kw = {
        "default": "browser.json",
        "help": "path to ytmusicapi browser auth JSON",
    }
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("inventory", help="dump artists to CSV")
    p_inv.add_argument("--auth", **auth_kw)
    p_inv.add_argument("--out", default="artists.csv")

    p_del = sub.add_parser(
        "delete",
        help="purge artists in library but missing from keep-list CSV",
    )
    p_del.add_argument("--auth", **auth_kw)
    p_del.add_argument("--in", dest="inp", default="artists.csv")
    p_del.add_argument("--dry-run", action="store_true")
    p_del.add_argument(
        "--debug",
        action="store_true",
        help="print API snapshot (library/LM) after plan; use with --dry-run to avoid changes",
    )

    args = p.parse_args()
    yt = ytmusic_from_auth(args.auth)

    if args.cmd == "inventory":
        idx = collect_artists(yt)
        write_csv(idx, args.out)
        print(f"Wrote {len(idx)} artists to {args.out}")
    elif args.cmd == "delete":
        try:
            keep_ids = read_keep_channel_ids(args.inp)
        except ValueError as e:
            print(f"{e}", file=sys.stderr)
            sys.exit(1)
        if not keep_ids:
            print(
                "Refusing to run: keep-list CSV has no channel_id rows "
                "(would remove every artist in the library).",
                file=sys.stderr,
            )
            sys.exit(1)
        live = collect_artists(yt)
        target_ids = set(live) - keep_ids
        if not target_ids:
            print(
                "Nothing to remove: every artist currently in your library "
                "appears in the keep-list CSV."
            )
            return
        delete_artists(yt, target_ids, dry_run=args.dry_run, debug=args.debug)


if __name__ == "__main__":
    main()
