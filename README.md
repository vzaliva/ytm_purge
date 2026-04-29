# ytm-purge

A small command-line utility for performing artist-level expungement of a personal YouTube Music library: every saved song, liked track, saved album, and artist subscription touching a set of unwanted artist channels is removed in a single run, with a hand-marked CSV as the curation interface.

The project is deliberately scoped: it solves library hygiene, not recommendation hygiene. Watch-history pruning and the *Don't recommend channel* signal remain manual tasks (see [Non-goals](#non-goals)).

## Motivation

YouTube Music exposes no built-in mechanism to filter or batch-delete a library by artist origin, language, or any other categorical attribute. The official client supports only per-track or per-album operations through its overflow menu, which is intractable for libraries above a few dozen items.

The use case that motivated this tool is value-based curation — for example, filtering a library by language or artist origin for personal or political reasons — but the design is content-agnostic: any criterion the user can apply by hand to a flat CSV of artists is supported.

The classification problem (*which artists to remove?*) is intentionally left to the human operator. Heuristics such as Cyrillic-script detection are surfaced as triage aids in the inventory output but do not drive decisions, because:

- Cyrillic script is shared across many languages (Ukrainian, Belarusian, Bulgarian, Serbian, Kazakh, etc.);
- Many artists publish under Latin-script names or in English regardless of origin;
- Origin metadata in YouTube Music's API is unreliable and frequently absent.

Hand-marking on a sorted CSV is, empirically, both faster and more accurate than any automated classifier the author was prepared to maintain.

## Approach

The workflow is a three-phase pipeline with the human in the loop between phases:

```
┌────────────┐    ┌────────────┐    ┌────────────┐
│ inventory  │──▶│  edit CSV  │──▶│   delete   │
│  (script)  │    │  (human)   │    │  (script)  │
└────────────┘    └────────────┘    └────────────┘
   YT Music ──▶ artists.csv ──▶ artists.csv* ──▶ YT Music
```

1. **Inventory.** The script enumerates every artist channel that appears anywhere across the user's library (saved songs, liked songs, saved albums, subscriptions), aggregates per-source counts, and writes one row per artist to a CSV. Rows are sorted by total footprint so heavy-tail artists surface first. A boolean `has_cyrillic` column is included as a triage convenience.

2. **Edit.** The user opens the CSV in any spreadsheet or text editor, sets the `delete` column to `1` on each artist to be expunged, and saves. No other column needs to be touched.

3. **Delete.** The script reads the marked CSV, re-fetches current library state, and for every track whose artist set intersects the target set:
   - removes the song from the library via its `feedbackTokens.remove` token (batched);
   - removes the like via `rate_song(..., 'INDIFFERENT')`;
   - removes the saved album via `rate_playlist(..., 'INDIFFERENT')`;
   - unsubscribes from the artist channel via `unsubscribe_artists`.

   A `--dry-run` flag prints the plan and a sample of affected tracks without mutating state. Live runs require an interactive `y/N` confirmation.

## Prerequisites

- Python 3.10 or later.
- [`ytmusicapi`](https://ytmusicapi.readthedocs.io/) (unofficial; emulates the YouTube Music web client using the user's browser cookie):

  ```bash
  pip install ytmusicapi
  ```

- A browser-cookie authentication file. Generate it once with:

  ```bash
  ytmusicapi browser
  ```

  and follow the prompts. This produces `browser.json` in the working directory, which the script reads at startup. See the [ytmusicapi setup docs](https://ytmusicapi.readthedocs.io/en/stable/usage/setup.html) for the cookie-extraction procedure.

  OAuth-based authentication is also supported by `ytmusicapi` but has been intermittent following recent changes to Google's OAuth client restrictions; browser-cookie auth is the recommended path at the time of writing.

## Usage

### 1. Inventory

```bash
python ytm_purge.py inventory --out artists.csv
```

Writes `artists.csv` with columns:

| column        | description                                                       |
| ------------- | ----------------------------------------------------------------- |
| `delete`      | empty by default; set to `1` to mark for deletion                 |
| `name`        | artist display name as reported by the API                        |
| `channel_id`  | YouTube channel ID — the canonical key                            |
| `songs`       | count of saved-library songs by this artist                       |
| `liked`       | count of liked songs (separate store from saved library)          |
| `albums`      | count of saved albums                                             |
| `subscribed`  | `1` if the user subscribes to the artist channel                  |
| `has_cyrillic`| `1` if the artist name contains characters in U+0400–U+04FF       |

### 2. Edit

Open `artists.csv` in any tool that round-trips CSV cleanly (LibreOffice Calc, Numbers, Excel with care, vim, etc.). Filter or sort as desired and set `delete=1` on the rows to be removed. Truthy values accepted by the parser: `1`, `x`, `X`, `y`, `Y`, `yes`, `true` (case-insensitive variants). Anything else — including blank — is treated as keep.

### 3. Delete

```bash
python ytm_purge.py delete --in artists.csv --dry-run
```

Prints a summary:

```
Plan:
  artists targeted:        12
  remove from library:     147 songs
  unlike (Liked Music):    23 songs
  remove saved albums:     8
  unsubscribe artists:     12

First 10 songs to remove from library:
    - <title> — <artists>
    ...

[dry-run] no changes made.
```

If the plan is correct, drop `--dry-run` and re-run; the script will print the same summary and prompt for `y/N` confirmation before executing.

## Architecture

The script is a single Python file, `ytm_purge.py`, organised into four functional layers:

### Aggregation: `collect_artists`

Walks each library-state endpoint exposed by `ytmusicapi` —

- `get_library_songs` — songs the user has explicitly saved to library;
- `get_liked_songs` — tracks in the *Liked Music* playlist (a separate store; a saved library song is not necessarily liked, and vice versa);
- `get_library_albums` — saved albums;
- `get_library_subscriptions` — followed artist channels;

— and folds them into a `dict[channel_id, {name, songs, liked, albums, subscribed}]`. The artist channel ID is the canonical key throughout; display names are recorded for human inspection only and are never used for matching.

### Serialisation: `write_csv`

Sorts the aggregated dict by `songs + liked + albums` descending and writes the CSV. The `has_cyrillic` column is computed here from the cached `CYRILLIC` regex (`[\u0400-\u04FF]`).

### Marking input: `read_marked`

Reads the CSV with `utf-8-sig` to tolerate BOMs introduced by Windows-side editors, and returns the subset of rows whose `delete` cell matches the truthy set.

### Mutation: `delete_artists`

Re-fetches current library state at run time (a deliberate choice — the inventory CSV may be hours or days old, and individual `videoId`/`feedbackToken` values are session-scoped), computes the intersection with the target channel-ID set, prints a plan, and on confirmation:

1. Batches all `feedbackTokens.remove` tokens into a single `edit_song_library_status` call (the API accepts a list).
2. Iterates `rate_song(videoId, 'INDIFFERENT')` over the like set (one call per track; the endpoint is cheap and not batchable).
3. Iterates `rate_playlist(browseId, 'INDIFFERENT')` over the album set.
4. Calls `unsubscribe_artists(target_ids)` once.

Errors are logged per-item to stderr and do not abort the run; partial completion is preferred over rollback because the operations are individually idempotent (re-running with the same CSV is safe).

### CLI: `main`

Standard `argparse` with two subcommands, `inventory` and `delete`. Configuration is intentionally minimal — `AUTH_FILE` is a module-level constant (`browser.json`) and the inventory page-size limit is hard-coded at 10,000, which exceeds any plausible personal-library size.

## Working on this repo with Claude Code

A few notes for Claude Code or other agentic tooling picking this up:

- **Single-file by design.** Resist splitting into modules unless the codebase grows materially. The four functional layers above are organised as plain functions in dependency order in `ytm_purge.py`.
- **Idempotency is a load-bearing invariant.** Any new mutation should be safe to re-run with the same input CSV. Avoid introducing operations that fail on already-applied state without catching the corresponding exception.
- **Channel ID, not name.** All matching is on `channel_id`. Display-name matching has been considered and deliberately rejected (homonyms, transliteration variants, Unicode normalisation hazards).
- **No automated origin classification.** The `has_cyrillic` column is a UI hint, not a decision input. Proposals to integrate MusicBrainz, language detection, or LLM-based classification should be raised in an issue first; the human-in-the-loop CSV is a feature, not a limitation.
- **Read-state freshness.** `delete_artists` re-fetches library state at run time rather than trusting the inventory CSV. Preserve this when refactoring; inventory data is for human review only.
- **Error handling: log, continue.** Per-item failures during the mutation phase are logged to stderr and do not abort. The user can re-run with the same CSV to retry.

## Non-goals

The following are explicitly out of scope for this tool:

- **Watch history pruning.** YouTube Music's recommender (home feed, station radio, autoplay) is conditioned on watch history independently of library state. Until the user prunes history at [myactivity.google.com](https://myactivity.google.com) filtered to YouTube Music, removed artists may continue to appear in radio extensions. *Library shuffle itself* will be clean immediately after a run.
- **The `Don't recommend channel` signal.** This is a distinct endpoint from artist unsubscription. `ytmusicapi` does not currently expose a stable wrapper, and it is the one step best done by hand on the artist page.
- **User-created playlists.** Tracks in user-curated playlists are untouched. Adding this is straightforward (`get_library_playlists` → `get_playlist` → `remove_playlist_items` per matching track) but has not been needed in practice; library shuffle does not draw from custom playlists.
- **Origin classification.** As above — the human is the classifier.

## Possible extensions

Listed roughly in order of plausible utility:

- A `playlists` subcommand that scrubs target artists from user-created playlists.
- A `history` subcommand calling `remove_history_items`. The Activity-controls UI is more thorough but a programmatic option would close the loop within the script.
- Persisting a deletion log (CSV or JSONL) of `(timestamp, artist_id, action, target_id, status)` for auditability and possible undo.
- A `--from-list` mode that takes a plain text file of channel IDs or artist names rather than a marked CSV, for use when the target set is known *a priori* (e.g. exported from another source).

## License

Author's choice. MIT is suggested as a permissive default for tooling of this kind.

## Acknowledgements

- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) by sigma67 — the unofficial Python client that does all the actual work of speaking to YouTube Music.
- [`ytmusic-deleter`](https://github.com/apastel/ytmusic-deleter) by apastel — a more general-purpose batch-delete tool for YouTube Music libraries; consulted as prior art.

