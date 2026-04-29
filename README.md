# ytm-purge

Command-line helper to **clean up your YouTube Music library by artist**: export every artist that appears in your library (saved songs, likes, saved albums, subscriptions), **delete the rows** for anyone you do **not** want to keep, then one command removes everyone else still in the library from your account (matching tracks, likes, saved albums, and artist subscriptions).

You build a **keep list** by editing the CSV; the tool does not guess from language, country, or metadata — only who remains in the file.

**Scope:** it fixes **library** contents. It does **not** fix recommendations by itself (watch history and “don’t recommend” still behave as in the official app). See [What this tool does *not* do](#what-this-tool-does-not-do).

---

## Install

You need [uv](https://docs.astral.sh/uv/) and a copy of this repository.

```bash
cd /path/to/ytm_purge
uv sync
```

That creates a local virtual environment and installs dependencies. Day-to-day you only need the commands below.

---

## One-time setup: sign in and create `browser.json`

The tool talks to YouTube Music the same way your browser does, using a small **`browser.json`** file produced by **`ytmusicapi`**. That file contains session cookies — **treat it like a password**. Do not email it, do not commit it to git (this repo already lists it in `.gitignore`).

### 1. Log in in the browser

Open **[music.youtube.com](https://music.youtube.com)** and sign in with the Google account whose library you want to manage. Stay on that account for the next steps.

### 2. Open developer tools → Network

- **Firefox:** `F12` or `Ctrl+Shift+I` (Windows/Linux) / `Cmd+Option+I` (macOS), then open the **Network** tab.
- **Chrome / Edge:** same shortcuts, then **Network**.

Make sure recording is on (you should see new lines appearing after a reload).

### 3. Reload YouTube Music

With the **Network** tab open, reload the page (`Ctrl+R` / `Cmd+R`, or `Ctrl+Shift+R` for a hard reload). You should see many requests appear. If the list stays empty, reload again while **Network** is focused.

### 4. Choose a good request

Avoid random **beacon** or **`/api/stats/qoe`** lines — those are often analytics and make a bad copy.

Prefer a request like one of these:

- **`POST`** to **`music.youtube.com`** whose path looks like **`/youtubei/v1/...`** (often named **browse**, **player**, **next**, etc.), or
- **`POST`** to **`youtubei.googleapis.com`**.

Click a request that returned status **200** (or similar success).

### 5. Copy **request headers** (plain text, not JSON)

You need the **raw HTTP request headers**, not a JSON tree of the URL.

- **Firefox:** right-click the request → **Copy** → **Copy Request Headers**.
- **Chrome / Edge:** right-click the request → **Copy** → **Copy request headers**.

The pasted block should look like lines such as:

```http
Host: music.youtube.com
User-Agent: ...
Cookie: ...long string...
X-Goog-Authuser: 0
...
```

Important:

- A **`Cookie:`** line must be present (very long is normal).
- **`X-Goog-Authuser`** (or similar) should appear — `ytmusicapi` expects it.

Some browsers **omit** an **`Authorization:`** line when copying. Current `ytm-purge` builds work around that; if you see **`Authorization: ... SAPISIDHASH ...`**, that is fine too.

Do **not** paste the “JSON” view of the URL (e.g. an object with `"scheme"`, `"host"`, `"query"`). That is not valid input.

### 6. Run the setup command and paste

From the **same folder** where you want `browser.json` created (usually the repo root):

```bash
uv run ytmusicapi browser
```

When prompted, **paste** the full header block, then finish input:

- **Linux / macOS:** `Ctrl+D` on an empty line.
- **Windows (cmd):** `Ctrl+Z` then Enter (see the on-screen hint from `ytmusicapi` if it differs).

You should get **`browser.json`** in that directory. You can pass a different file with **`--auth`** on inventory/delete.

Optional: **`uv run ytmusicapi browser --file /path/to/my-auth.json`** to write somewhere else.

Official reference: [ytmusicapi setup](https://ytmusicapi.readthedocs.io/en/stable/usage/setup.html).

---

## Everyday usage

Run commands from the project directory (or pass full paths to `--auth` / `--in` / `--out`).

### Step 1 — Download your artist list (read-only)

```bash
uv run python ytm_purge.py inventory --out artists.csv
```

Uses `browser.json` in the current directory by default. Elsewhere:

```bash
uv run python ytm_purge.py inventory --auth /path/to/browser.json --out artists.csv
```

### Step 2 — Build your keep list

Open **`artists.csv`** in LibreOffice, Excel, Numbers, or a text editor. **Delete entire rows** for every artist you want **removed** from YouTube Music. Leave only rows for artists you want to **keep**. Keep the **header** line intact.

Do not strip or hand-edit **`channel_id`** on rows you keep — that column is how the next step matches your file to the live library.

### Step 3 — Preview, then run deletion

**Always preview first** (no changes to your library):

```bash
uv run python ytm_purge.py delete --in artists.csv --dry-run
```

Review the printed counts and sample tracks. Then run without `--dry-run`. You will get a **`Proceed? [y/N]`** prompt before anything is deleted.

After you confirm, the script prints **clear sections** (saved library, Liked Music, albums, subscriptions) and ends with a **`Run summary`** so you can see what succeeded vs what still needs manual cleanup — especially when library removal fails for some tracks.

**How `delete` decides:** it **fetches your library again** (same idea as **`inventory`**), builds the set of artist channel IDs currently present, and **removes** every artist that is **not** listed in your CSV **`channel_id`** column. There is **no separate snapshot file**.

**Numbers in the plan are live:** counts come from a fresh YouTube fetch. After a run removes items, the next plan is usually smaller. (Older behaviour used `rate_song` for likes, which often **left rows in Liked Music**; current versions remove LM entries via the **Liked Music playlist (`LM`)** when `setVideoId` is present.)

**Liked Music vs saved library:** If you only thumbs-up tracks, **remove from library** is often **0**; watch the **Liked Music** line. Library-token issues apply only to explicitly **saved** songs. If you don’t change the account elsewhere during a run, the plan you confirmed is what that run processes.

**Important:** If you **add** music after editing the CSV but **do not** add those artists’ rows (with correct `channel_id`) to the file, the next **`delete`** run can treat them as “missing from the keep list” and remove them. Run **`inventory`** again and merge new rows if you need an up-to-date keep list.

---

## CSV columns (inventory output)

| Column | Meaning |
|--------|--------|
| `name` | Artist name from YouTube (for your eyes only). |
| `channel_id` | Internal ID used for matching — do not edit unless you know what you are doing. |
| `songs` | Saved-to-library track count for this artist. |
| `liked` | Liked-track count (Liked Music is separate from “saved to library”). |
| `albums` | Saved album count. |
| `subscribed` | `1` if you follow that artist channel. |

Rows are sorted so artists with more library activity appear first.

---

## Troubleshooting

| Problem | What to try |
|--------|----------------|
| **Network tab is empty** | Open **Network** *before* reloading **music.youtube.com**; hard-reload (`Ctrl+Shift+R`); clear any text in the filter box. |
| **`ytmusicapi browser` says headers are missing** | Use another request (e.g. **`youtubei`** **POST**), ensure you are logged in, and use **Copy Request Headers**, not URL JSON. |
| **`InvalidHeader` or garbage in the error mentioning `POST /...`** | You accidentally stored a full request line as a header name. Delete `browser.json` and repeat [setup](#one-time-setup-sign-in-and-create-browserjson); use a **youtubei** request and **Copy Request Headers** only. Current versions of `ytm_purge` also drop invalid header keys when loading `browser.json`. |
| **OAuth / credentials error from `ytmusicapi`** | Usually means the JSON was not recognised as browser session headers. Regenerate from a logged-in **music.youtube.com** request as above. |
| **Session expired later** | Create a fresh `browser.json` the same way. |
| **Plan lists library songs but none are “Removed … from library”** | YouTube sometimes omits remove tokens on the library list. Current builds refetch tokens via the track’s watch playlist; if you still see warnings, remove those titles manually in the app. |
| **Same CSV, but unlike / library counts changed on the next run** | Normal. Each run (and `--dry-run`) refetches **current** Liked Music / library state. After tracks are unliked or removed, the next plan has **fewer** rows to act on. |

---

## What this tool does *not* do

- **Watch history.** The recommender can still suggest artists you used to play until you clear **YouTube Music** activity under [Google My Activity](https://myactivity.google.com). Your **library** and **library shuffle** can still be cleaned by this tool.
- **“Don’t recommend channel”.** That is separate from unsubscribing; you may still do it manually in the app.
- **Your own playlists.** Tracks only in user-made playlists are **not** removed (only library / likes / saved albums / subscriptions as described above).

---

## Safety notes

- **`inventory`** only **reads** your library; it does not delete anything.
- **`delete --dry-run`** only **reads** your library and your CSV, then prints a plan.
- **`delete`** without `--dry-run` asks for **`y`** before changing anything.
- An **empty** CSV (no data rows with a `channel_id`) is **rejected**: it would mean removing **every** artist in the library.
- Re-running **`delete`** with the same keep list is intended to be safe if something failed partway (operations are idempotent where the API allows).

---

## For contributors

If you are hacking on the script itself:

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
```

The repo includes [`.vscode/launch.json`](.vscode/launch.json) examples for debugging **inventory** and **delete --dry-run**; put **`browser.json`** in the workspace folder when using them.

---

## License

Author’s choice. MIT is a reasonable default for small tooling.

## Acknowledgements

- **[ytmusicapi](https://github.com/sigma67/ytmusicapi)** — unofficial Python client for YouTube Music.
- **[ytmusic-deleter](https://github.com/apastel/ytmusic-deleter)** — related batch-delete tool, consulted as prior art.
