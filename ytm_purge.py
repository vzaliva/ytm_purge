#!/usr/bin/env python3
"""
ytm_purge.py — semi-automated artist-level purge of a YouTube Music library.

Workflow
--------
1.  python ytm_purge.py inventory --out artists.csv
        Writes one row per artist that appears anywhere in your library,
        liked songs, saved albums, or subscriptions, sorted by total
        footprint. The `delete` column starts empty.

2.  Open artists.csv in your editor of choice (Excel, Numbers, vim, ...),
    set `delete` to `1` on rows you want expunged, save.

3.  python ytm_purge.py delete --in artists.csv [--dry-run]
        For every row marked `delete=1`:
          - removes that artist's saved songs from your library
          - removes (unlikes) that artist's tracks in Liked Music
          - removes saved albums by that artist
          - unsubscribes from the artist channel
        Use --dry-run first to inspect the plan without mutating state.

Auth
----
Run `ytmusicapi browser` once and follow the prompts to create
`browser.json` next to this script. See:
https://ytmusicapi.readthedocs.io/en/stable/usage/setup.html

Caveats
-------
- Does not touch user-created playlists. Trivial to add if needed.
- Does not prune watch history (recommendations / radio still draw on
  it). Use myactivity.google.com filtered to YouTube Music for that.
- A `has_cyrillic` column is included as a triage aid; filter on it
  in your spreadsheet to bulk-review likely candidates first.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from typing import Iterable

from ytmusicapi import YTMusic

AUTH_FILE = "browser.json"
CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def has_cyrillic(s: str | None) -> bool:
    return bool(s and CYRILLIC.search(s))


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

    liked = yt.get_liked_songs(limit=10_000) or {}
    for s in liked.get("tracks", []):
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
        w.writerow(["delete", "name", "channel_id", "songs", "liked",
                    "albums", "subscribed", "has_cyrillic"])
        for cid, m in rows:
            w.writerow([
                "",
                m["name"] or "",
                cid,
                m["songs"],
                m["liked"],
                m["albums"],
                int(m["subscribed"]),
                int(has_cyrillic(m["name"])),
            ])


def read_marked(path: str) -> list[dict]:
    truthy = {"1", "x", "X", "y", "Y", "true", "TRUE", "yes", "YES"}
    with open(path, "r", encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f)
                if (row.get("delete") or "").strip() in truthy]


def matches(track: dict, target_ids: set[str]) -> bool:
    return any((a.get("id") in target_ids) for a in (track.get("artists") or []))


def delete_artists(yt: YTMusic, marked: Iterable[dict], dry_run: bool) -> None:
    target_ids = {r["channel_id"] for r in marked if r.get("channel_id")}
    if not target_ids:
        print("No channel IDs found in marked rows.")
        return

    library_songs = yt.get_library_songs(limit=10_000) or []
    liked = (yt.get_liked_songs(limit=10_000) or {}).get("tracks", [])
    albums = yt.get_library_albums(limit=10_000) or []

    songs_to_unsave = [s for s in library_songs if matches(s, target_ids)]
    likes_to_unlike = [s for s in liked if matches(s, target_ids)]
    albums_to_unsave = [a for a in albums if matches(a, target_ids)]

    print("Plan:")
    print(f"  artists targeted:        {len(target_ids)}")
    print(f"  remove from library:     {len(songs_to_unsave)} songs")
    print(f"  unlike (Liked Music):    {len(likes_to_unlike)} songs")
    print(f"  remove saved albums:     {len(albums_to_unsave)}")
    print(f"  unsubscribe artists:     {len(target_ids)}")
    print()
    print("First 10 songs to remove from library:")
    for s in songs_to_unsave[:10]:
        artists = ", ".join(a.get("name", "") for a in (s.get("artists") or []))
        print(f"    - {s.get('title')} — {artists}")
    if dry_run:
        print("\n[dry-run] no changes made.")
        return

    if input("\nProceed? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    # Library removal: batch the feedback tokens.
    remove_tokens = [
        s["feedbackTokens"]["remove"]
        for s in songs_to_unsave
        if (s.get("feedbackTokens") or {}).get("remove")
    ]
    if remove_tokens:
        try:
            yt.edit_song_library_status(remove_tokens)
            print(f"Removed {len(remove_tokens)} songs from library.")
        except Exception as e:
            print(f"  ! library batch remove failed: {e}", file=sys.stderr)

    # Unlike: one call per track, but rate_song is cheap.
    for s in likes_to_unlike:
        vid = s.get("videoId")
        if not vid:
            continue
        try:
            yt.rate_song(vid, "INDIFFERENT")
        except Exception as e:
            print(f"  ! unlike failed for {s.get('title')}: {e}",
                  file=sys.stderr)
    if likes_to_unlike:
        print(f"Unliked {len(likes_to_unlike)} songs.")

    for alb in albums_to_unsave:
        bid = alb.get("browseId")
        if not bid:
            continue
        try:
            yt.rate_playlist(bid, "INDIFFERENT")
        except Exception as e:
            print(f"  ! album remove failed for {alb.get('title')}: {e}",
                  file=sys.stderr)
    if albums_to_unsave:
        print(f"Removed {len(albums_to_unsave)} saved albums.")

    try:
        yt.unsubscribe_artists(list(target_ids))
        print(f"Unsubscribed from {len(target_ids)} artists.")
    except Exception as e:
        print(f"  ! unsubscribe failed: {e}", file=sys.stderr)

    print("Done.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("inventory", help="dump artists to CSV")
    p_inv.add_argument("--out", default="artists.csv")

    p_del = sub.add_parser("delete", help="process a hand-marked CSV")
    p_del.add_argument("--in", dest="inp", default="artists.csv")
    p_del.add_argument("--dry-run", action="store_true")

    args = p.parse_args()
    yt = YTMusic(AUTH_FILE)

    if args.cmd == "inventory":
        idx = collect_artists(yt)
        write_csv(idx, args.out)
        print(f"Wrote {len(idx)} artists to {args.out}")
    elif args.cmd == "delete":
        marked = read_marked(args.inp)
        if not marked:
            print("No rows marked for deletion.")
            return
        delete_artists(yt, marked, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

