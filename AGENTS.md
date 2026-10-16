# AGENTS.md

Guidance for AI coding agents (and humans in a hurry) working on this repository. `README.md` explains how to *use and deploy* the software; this file explains how it is built, what is easy to break, and how to verify a change safely.

## What this project is

A weekly pipeline that finds new open-access papers for configured topics, screens abstracts with an LLM, builds a podcast episode with Google NotebookLM, and publishes it to the owner's own website (PHP + MySQL) with an RSS feed for Spotify.

```
sources ──► dedupe ──► known/seen filter ──► screen (OpenRouter) ──► rank + license ──► download PDFs
 (4 APIs)   (DOI+fuzzy)  (state.db)            (Decisions API)        (eligible())       (acquire())
                                                                                              │
site (PHP) ◄── publish.py (chunked upload) ◄── episode.json + audio ◄── notebook.py (NotebookLM)
```

Entry point: `podcast` CLI (`src/podcast/cli.py`): `screen`, `fetch`, `run [--topic] [--until] [--publish] [--no-state]`, `publish`. Production runs from cron through `deploy/run_topic.sh`.

## Repository map

| Path | Role |
|---|---|
| `src/podcast/config.py` | Pydantic config models, `.env` loading, `require_env()` |
| `src/podcast/models.py` | `Paper`, `Screening`, `Episode`; `Episode.metadata()` is the JSON sent to the site |
| `src/podcast/sources/` | One module per source + `base.py` (keyword semantics shared by all) |
| `src/podcast/dedupe.py` | Exact (DOI) and conservative fuzzy (title + authors) de-duplication, also against history |
| `src/podcast/screen.py` | OpenRouter **Decisions API** client and threshold logic |
| `src/podcast/fetch_pdf.py` | PDF download: HTTP, then headless Chromium (Playwright) |
| `src/podcast/notebook.py` | NotebookLM stage (async, `notebooklm-py`) |
| `src/podcast/publish.py` | Chunked, resumable upload client |
| `src/podcast/pipeline.py` | Orchestration: `discover`, `run_search_and_screen`, `acquire`, `run_topic`, `publish_dir` |
| `src/podcast/state.py` | SQLite state (`data/state.db`) |
| `site/` | PHP website: `upload.php`, `feed.php`, `index.php`, `lib.php`, `schema.sql`, `assets/style.css` |
| `deploy/` | `run_topic.sh`, `keepalive.sh`, `crontab.example` |
| `tests/` | Offline pytest suite (about 109 tests, about 1 s) |

Runtime data lives in `data/` (git-ignored): `state.db`, `pdfs/`, `episodes/<run>/<topic>/{episode.m4a,episode.json,published.json}`, `logs/`.

## Safety rules: read before running anything

* **`config.yaml`, `.env` and the NotebookLM login are real.** `config.yaml` points `site.upload_url` at the owner's **live website**, `.env` holds a live OpenRouter key and the upload token, and `~/.notebooklm` is a real Google account. `podcast run --publish` creates a notebook, generates audio and **publishes publicly**. Never run it, or `podcast publish`, against the real config without being asked.
* Safe dry run (no side effects, costs fractions of a cent): `venv3.13/bin/podcast run --topic "<name>" --until screen --no-state`.
* For anything that publishes, use a **scratch config** (`--config /tmp/x.yaml` with its own `data_dir`) whose `site.upload_url` points to a local PHP server, and export `SITE_UPLOAD_TOKEN` in the environment (environment variables win over `.env`, `load_dotenv` does not override them).
* Never print, log, commit or paste secrets. Git-ignored on purpose: `.env`, `config.yaml`, `site/config.php`, `data/`, `venv3.13/`, uploaded media.
* Read-only requests to the live site (HEAD/GET) are fine for verification; do not POST to it.
* **The user's crontab has other entries** (backups, other projects). The pipeline's entries live in a marked block (`# >>> auto_scientific_podcast ... <<<`). Back up with `crontab -l` before any change, install with `crontab <file>`, and never replace the whole table.
* Do not commit unless asked. Do not push.
* `pkill -f <pattern>` kills your own shell if the pattern appears in its command line; use the bracket trick (`pkill -f "[p]hp -S"`).

## Invariants and gotchas (each one cost time to find)

### Discovery
* **Keyword meaning is defined once**, in `sources/base.py::terms()`: a paper matches if *any* keyword matches; a keyword matches when *all its words* occur in any order; `"double quotes"` keep an exact phrase. Every source translates this into its own query syntax (Europe PMC `AND`, OpenAlex boolean `search`, arXiv `all:`; bioRxiv has no keyword search and is filtered client-side). Exact phrases were the original bug: they matched almost nothing. Keep the sources consistent.
* **Europe PMC**: never search `TITLE_ABS`. Almost every record from the last week (4120 of 4128 in one measurement) is a brand-new PMC article **without an indexed abstract**, so title/abstract queries find nothing. Search all fields; the abstract is fetched from `…/{PMCID}/fullTextXML` (`abstract_from_jats`) and records without one are dropped. Results are relevance-ordered and cut at `max_results_per_source`.
* **PDFs**: for PMC papers the first candidate URL is NCBI's open-data S3 bucket (`pmc_s3_urls`), which has no bot checks. The NCBI and Europe PMC renderers sit behind JavaScript challenges and only work through the browser fallback (which is flaky). `download_pdf` tries plain HTTP on **all** URLs before launching a browser.
* **arXiv** has no license in its API. `resolve_license` reads the abs page, lazily and only for papers that survive screening. Merged records lose the arXiv `source_id`; `arxiv_id()` recovers it from the PDF URL or DOI. By default **any** open-access license is accepted (`licenses` unset); an allow-list is optional.
* `max_results_per_source` is reached on broad topics; raising it increases abstract fetches and screening time.

### De-duplication (`dedupe.py`)
* A false merge silently loses a good paper, so the fuzzy rules are conservative and **every merge is logged** (`near-duplicate merged: …`). Required title similarity depends on author evidence and on how many of the two records are preprints; two **published** articles with different DOIs are never merged; titles differing by a number, "Part I/II" or a single swapped content word are never merged.
* Companion papers by the same group with similar titles (for example "…Cardiac Cine MRI Synthesis via Phase-Conditioned…" and "…4D…via Radial-Decomposed…") are intentionally **kept separate**. The owner decided to leave them as they are.
* The same logic is applied to history (`drop_seen_duplicates`, using `State.known_summaries`). Changing a threshold requires tests for both merges that must happen and near-misses that must not (`tests/test_dedupe_fuzzy.py`). `fuzzy_dedupe: false` disables it.

### Screening (`screen.py`)
* The model is a **decision model** on OpenRouter's `/api/alpha/decisions`, not a chat model: it returns probabilities and positions, never text, so there is **no rationale**. Four questions per abstract (relevance and quality as 5-level scores, study type as a choice, "engaging" as yes/no). Thresholds are applied in code; ranking score is `0.6·R + 0.4·Q + 2·E` (`Screening.score`).
* **Absolute scores drift.** The same papers scored about 3 points lower on a later day with the same reported model snapshot. Do not hard-code assumptions about score levels; the owner tuned `min_*` in `config.yaml` by looking at dry-run tables.
* A failed screening call returns `None` and the paper is deliberately **not recorded**, so the next run retries it.
* The default model id is an alias (`~perplexity/pplx-decider-latest`); each `Screening` stores the snapshot that answered (`model`).

### State machine (`state.py`, `pipeline.py`)
Table `papers(key, topic, status, data JSON, …)`, primary key `(key, topic)`; table `episodes(run_id, topic, dir, status)`.

| Paper status | Meaning |
|---|---|
| `screened` | Seen and judged; not used. Never reconsidered (known key) |
| `selected` | Chosen in the **current run** before the build. A finished run turns it into `used` or `carryover` |
| `carryover` | Kept for a later run (too few papers, or the build failed). Rejoins the pool with its screening and PDF |
| `used` | Part of a built episode |
| `expired` | Carried too long (`carryover_days`); stays in the table so it is **never rediscovered** |

Episodes: `built` (audio ready, not uploaded), `published`.

* `Paper.key` is `doi:<lowercase doi>` or `<source>:<source_id>`. `known_keys()` returns **every** status, so nothing ever reappears as new.
* `State.carryover()` treats leftover `selected` rows as carry-over: a run killed outright (power loss, `timeout`, OOM) skips the `except Exception` cleanup, and without this those papers would be lost. **Call it before the current run records anything.**
* `keep_for_later()` marks papers `carryover`, or deletes them (`forget`) when `carryover_days: 0`.
* Episode directories are stored resolved (`.resolve()`); `mark_published` resolves too. Keep both consistent.

### NotebookLM (`notebook.py`)
* Unofficial, undocumented API: expect breakage. Everything goes through `build_episode_with(client, …)`, which takes an injected client so tests use a fake. Keep that seam.
* The notebook is deleted in a `finally` (also when the build fails) unless `delete_notebook: false`; deletion failure must not lose the episode.
* `duration_seconds` is a **float** (this crashed a live run once); the audio extension is sniffed from the file header; the show notes come from a prompt to the notebook (`SHOW_NOTES_PROMPT`, parsed by `parse_show_notes`, falling back to the notebook summary). Citations and licenses are **never** taken from NotebookLM text, only from our own records.
* Long operation: generation takes minutes. Do not wrap it in short timeouts.

### Publishing (`publish.py` ⇄ `site/upload.php`)
* Upload id = first 40 hex chars of the file's SHA-256, so an interrupted upload resumes (even in a later run) and publishing twice answers `exists`.
* Chunks are appended only at the exact offset; duplicates are acknowledged; 409 means "continue from `received`"; 413 halves the chunk; the final request carries `total_bytes`, `sha256` and the metadata.
* **Redirects are resolved once up front** with a GET (`resolve_upload_url`), never followed on POST (a 301 would turn a POST into a GET and drop the data). Only same-site redirects (apex ⇄ `www.`, http→https) are followed; cross-site and https→http are refused so the token cannot leak. The owner's site does redirect apex to `www.`.
* `published.json` keeps the first `created` response; later `exists` answers do not overwrite it.

### The PHP site (`site/`)
* Must stay **PHP 7.4 compatible** (shared hosting): no `match`, `str_contains`, named arguments, constructor promotion. `declare(strict_types=1)`, PDO prepared statements only, no extensions beyond PDO MySQL (no DOM/XMLWriter: the feed is built as strings with `xml_esc()` and `cdata()`).
* **Every table name goes through `tbl()`** (it validates and quotes `table_prefix`). `schema.sql` uses placeholders `{{episodes}}`, `{{episode_papers}}`, `{{p}}`, replaced in `ensure_schema()`. Never write a raw table name or a fixed constraint/index name.
* Escape all output: `h()` for HTML, `xml_esc()`/`cdata()` for the feed, `safe_url()` for links (only http/https). Metadata arrives from the pipeline but is treated as untrusted.
* `.htaccess` blocks `config.php`, `lib.php`, `schema.sql`, `.user.ini` and `incoming/`; `media/` may not execute scripts. Check after changes that these still return 403 on a test server.
* **Contracts that must change together**: `Episode.metadata()` (`models.py`) ⇄ `parse_metadata()` (`upload.php`); the chunk protocol in `publish.py` ⇄ `upload.php` ⇄ `site/README.md` ⇄ `FakeSite` in `tests/test_publish.py`; `config.example.php` ⇄ the config table in `README.md`.
* `config.php` holds secrets and is never committed; the example file has placeholders only.

### Scheduling (`deploy/`)
* `run_topic.sh` assumes the virtualenv is `venv3.13/` in the project root, takes a global lock (`data/podcast.lock`, exit 75 = skipped), stops a run after 4 h, and logs to `data/logs/`. Extra arguments are forwarded to `podcast run`, so `run_topic.sh "<topic>" --until screen --no-state` tests the exact cron path.
* `keepalive.sh` runs every 20 minutes; `notebooklm-py` recommends 15 to 20 minutes, and a looser cadence can let the login expire. Cron runs with a minimal environment: test with `env -i HOME=$HOME PATH=/usr/bin:/bin deploy/run_topic.sh …`.
* Topic names in the crontab must match `name:` in `config.yaml` exactly.

## How to verify a change

1. **Unit tests**: `venv3.13/bin/pytest -q` (offline; the suite also passes with all network access blocked). Add a test for every bug fix and for both sides of every new rule.
2. **Pipeline without side effects**: `venv3.13/bin/podcast run --topic "<name>" --until screen --no-state`, and look at the ranked table.
3. **PHP changes**: lint with `php -l`, then test against a real MySQL:
   ```bash
   docker run -d --name podcast-test-mysql -e MYSQL_ROOT_PASSWORD=testpw -e MYSQL_DATABASE=podcast \
       -p 127.0.0.1:33307:3306 mysql:8.0              # first start takes about 70 s
   cp -r site /tmp/site_test                           # then write a test config.php (db host 127.0.0.1, port 33307)
   # router.php: send /feed.xml to feed.php (php -S ignores .htaccess)
   PHP_CLI_SERVER_WORKERS=4 php -d upload_max_filesize=1M -d post_max_size=2M -S 127.0.0.1:8099 router.php
   ```
   A 1 MB limit proves the chunked path. Exercise: wrong or missing token (401), bad metadata (422), non-audio (415), upload, repeat (200 `exists`), resume, escaping with hostile titles (`<script>`, `]]>`), feed validity (parse the XML), pagination. Remove the container and the server afterwards.
4. **End to end** (creates a NotebookLM notebook and a real episode, takes about 5 minutes): only with the owner's agreement, and against a local test site.
5. **Docs**: update `README.md`, `site/README.md`, `config.example.yaml`, `site/config.example.php` and `deploy/crontab.example` whenever options, protocol or behaviour change.

## Conventions

* Python 3.13, `from __future__ import annotations`, type hints, Pydantic v2 models, standard `logging` (`log = logging.getLogger(__name__)`). Comments explain *why*, not what.
* HTTP is synchronous `httpx` with retries (`tenacity` or the explicit loop in `publish._send`); only the NotebookLM stage is async. One broken source or topic must not stop the others (errors are logged and counted in the exit code).
* New configuration goes into the Pydantic model, the example file and the README table, with a safe default.
* Keep changes small and test-backed; avoid new heavy dependencies.

## Current state and known gaps

Built and verified: discovery from four sources, fuzzy and historical de-duplication, screening, PDF acquisition, NotebookLM build, chunked publish, PHP site with RSS feed, cron wrappers, carry-over and crash recovery. One real episode has been published to the live site, and the feed is submitted to Spotify and working (the owner confirms).

Known gaps and open points:
* The PHP site has **no automated tests**. The owner reports having tested it on their real server; locally, after the `table_prefix` change (`tbl()`, the `schema.sql` placeholders), it was only syntax-checked, and the earlier local MySQL recipe above was last run before that change. After any further PHP change, re-run the recipe or ask the owner to test on the real server.
* Failure alerts rely on `data/logs/failures.log` and cron mail (`MAILTO`); no push channel is configured.
* `data/pdfs` and `data/episodes` grow without bound; there is no cleanup.
* The screening score level drifts over time (see above); there is no automatic recalibration.
* There is no `status` command; inspect state with `sqlite3 data/state.db "select topic, status, count(*) from papers group by 1,2; select * from episodes;"`.
* Fuzzy de-duplication can still merge two different works in rare cases; read the merge log lines after changing it.
* NotebookLM access is unofficial; if it breaks, the failure shows up in `data/logs/failures.log` and the keepalive log. Do not paper over it with retries beyond the existing ones.
