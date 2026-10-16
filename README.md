# Auto Scientific Podcast

Finds the newest **open-access** scientific papers on the topics you choose, lets an LLM screen their abstracts, turns the best ones into a **podcast episode with Google NotebookLM**, and publishes it on **your own website** with an RSS feed that Spotify (and any podcast app) can subscribe to. Run it from cron and a new episode appears every week without touching anything.

```
keywords ─► discover ─► de-duplicate ─► screen ─► download ─► NotebookLM ─► upload ─► your website
            Europe PMC   DOI + fuzzy     OpenRouter  PDFs      audio +        chunked    episode page
            OpenAlex     titles          decision    (HTTP /   show notes     + resume   + RSS feed
            arXiv                        model       browser)                            ─► Spotify
            bioRxiv/medRxiv
```

1. **Discover** papers published in the last `window_days` from Europe PMC, OpenAlex, arXiv and bioRxiv/medRxiv. Only open-access papers with a downloadable PDF are kept.
2. **De-duplicate** by DOI and by near-identical titles (a preprint and its published version count once), also against everything seen in earlier runs.
3. **Screen** each abstract with a decision model on OpenRouter: relevance, quality and "would this make a good segment". The best `top_n` papers continue.
4. **Download** the PDFs locally (plain HTTP, the PMC open-data bucket, or a headless browser for sites that block scripts).
5. **Build** the episode with NotebookLM: upload the PDFs, generate the audio overview, ask the notebook for the title and show notes, download the audio, then delete the notebook.
6. **Publish** the audio and metadata to the PHP site with a resumable, checksummed chunked upload. The site stores the metadata in MySQL and serves the episode cards and the RSS feed.

If a week yields fewer than `min_sources` usable papers, no episode is made and the papers wait (up to `carryover_days`) for the next run.

> **Unofficial and AI-generated.** NotebookLM is driven through the community library [notebooklm-py](https://github.com/teng-lin/notebooklm-py), which uses undocumented Google endpoints and can break without notice. The audio is AI-generated and can contain errors; the site says so, and every episode lists the papers it is based on.

---

## Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Configuration: `.env`](#configuration-env)
4. [Configuration: `config.yaml`](#configuration-configyaml)
5. [Running the pipeline](#running-the-pipeline)
6. [Scheduling with cron](#scheduling-with-cron)
7. [Deploying the podcast website](#deploying-the-podcast-website)
8. [Adapting `config.php`](#adapting-configphp)
9. [Customizing the website](#customizing-the-website)
10. [Submitting the feed to Spotify](#submitting-the-feed-to-spotify)
11. [When something goes wrong](#when-something-goes-wrong)
12. [Project layout, tests, license](#project-layout-tests-license)

---

## Requirements

| For | You need |
|---|---|
| The pipeline | Linux (or macOS), **Python 3.13**, `cron`, and disk space: PDFs and audio stay under `data/`, up to a few hundred MB per episode, so delete old `data/pdfs/` and `data/episodes/` folders when you need the space |
| NotebookLM | A Google account (a dedicated one is recommended) |
| Screening | An [OpenRouter](https://openrouter.ai) API key with a little credit (screening 500 abstracts costs a fraction of a cent) |
| The website | Web hosting with **PHP 7.4+** (developed on 8.x), **MySQL or MariaDB**, and Apache with `.htaccess` enabled and `mod_rewrite`. Nginx works with the notes [below](#a-note-on-nginx) |

## Installation

The cron scripts expect the virtual environment to be called **`venv3.13`** and to live in the project root.

```bash
git clone <this repository> auto_scientific_podcast
cd auto_scientific_podcast

python3.13 -m venv venv3.13
venv3.13/bin/pip install -e ".[dev]"        # the pipeline, its dependencies and pytest
venv3.13/bin/playwright install chromium     # headless browser, used for PDFs that block plain downloads

cp config.example.yaml config.yaml           # then edit, see below
touch .env && chmod 600 .env                 # then edit, see below

venv3.13/bin/notebooklm login                # opens a browser: sign in to the Google account used for NotebookLM
venv3.13/bin/notebooklm auth check           # confirms the stored login works
```

`notebooklm login` stores the session under `~/.notebooklm/profiles/<profile>/`. It is not in `.env` and not in this repository. Treat that folder like a password: whoever has it can use the Google account.

Run the tests to make sure the install is healthy: `venv3.13/bin/pytest -q`.

---

## Configuration: `.env`

Secrets live in `.env` in the project root. It is loaded automatically (also under cron) and is git-ignored.

```ini
OPENROUTER_API_KEY=sk-or-...                    # required
SITE_UPLOAD_TOKEN=<64 random hex characters>    # required to publish; must equal 'upload_token' in the server's config.php
SITE_UPLOAD_URL=https://example.org/podcast/upload.php   # optional if site.upload_url is set in config.yaml
CONTACT_EMAIL=you@example.org                   # optional; sent to OpenAlex/Europe PMC as a polite-pool contact
```

| Variable | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | Screening with the decision model |
| `SITE_UPLOAD_TOKEN` | to publish | Shared secret sent as `Authorization: Bearer ...` to `upload.php` |
| `SITE_UPLOAD_URL` | no | Address of `upload.php`; `site.upload_url` in `config.yaml` takes precedence |
| `OPENALEX_API_KEY` | recommended | Free key from openalex.org/settings/api; OpenAlex requires it for paging past the first page and gives a larger daily budget |
| `CONTACT_EMAIL` | no | Identifies you to the public paper APIs (they give faster access to polite clients) |

Generate the token once and use the **same value** in `.env` and in the server's `config.php`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Configuration: `config.yaml`

Copy `config.example.yaml` to `config.yaml` (git-ignored) and edit it. Every key has a default except `topics`.

### Global options

| Key | Default | Meaning |
|---|---|---|
| `window_days` | `7` | Only papers published in the last N days. Match it to how often a topic runs |
| `sources` | all four | Any of `europepmc`, `openalex`, `arxiv`, `biorxiv` |
| `max_results_per_source` | `300` | Cap per source and topic. Broad topics hit it; raising it means longer runs |
| `licenses` | *(unset)* | Optional allow-list such as `[cc0, cc-by, cc-by-sa]`. Unset = any open-access paper with a downloadable PDF. See the note below |
| `fuzzy_dedupe` | `true` | Also merge near-identical titles (preprint vs published) and skip works seen in earlier runs |
| `carryover_days` | `28` | Screened papers wait this long for an episode when too few were found; `0` drops them |
| `contact_email` | *(unset)* | Alternative to `CONTACT_EMAIL` in `.env` |
| `data_dir` | `./data` | Where PDFs, episodes, logs and the state database go |

> **About licenses.** The audio is published on your site. Papers under `cc-by-nc`, `cc-by-nd` or arXiv's own license allow reading, but you should check the terms before publishing derivatives, especially if the feed is monetized. Set `licenses: [cc0, cc-by, cc-by-sa]` to be strict.

### Topics: one episode series per topic

```yaml
topics:
  - name: MSK MRI                       # unique; used by --topic and by the cron entries
    keywords: ["muscle MRI", "skeletal muscle imaging", "MSK MRI"]
    exclude: ["case report"]            # optional: drop papers whose title/abstract contain any of these
    podcast:
      format: deep_dive                 # deep_dive | brief | critique | debate
      length: default                   # short | default | long
      language: en                      # language of the audio
      instructions: >                   # free text for the hosts
        Weekly research digest for MR physicists. Name each paper's first author and year,
        distinguish peer-reviewed work from preprints, and be critical about limitations.
```

**How keywords match.** A paper matches if *any* keyword matches. A keyword matches when *all of its words* occur, in any order, anywhere in the text: `Deep learning MRI` needs "deep", "learning" and "MRI". Put double quotes around words that must stay together as an exact phrase: `"deep learning" MRI`. (Exact phrases such as `"Deep learning MRI"` almost never occur verbatim, which is why this is not the default.) Matching is broad on purpose, because the screening step judges relevance from the abstract.

### Screening

```yaml
screening:
  model: "~perplexity/pplx-decider-latest"   # any OpenRouter "decisions" model
  min_relevance: 6        # 0-10
  min_quality: 5          # 0-10
  min_engaging: 0.5       # probability 0-1
  top_n: 6                # papers per episode
  concurrency: 4
  allow_preprints: true
```

The model answers four questions per abstract (via OpenRouter's Decisions API, which returns probabilities rather than text): **relevance** and **quality** (5 levels, rescaled to 0-10), the **study type**, and whether the paper is **engaging**. A paper is kept when all three numbers reach their minimum; the best `top_n` by score (`0.6·relevance + 0.4·quality + 2·engaging`) go into the episode.

**Calibrate the thresholds on your own topics** instead of trusting the defaults:

```bash
venv3.13/bin/podcast run --topic "MSK MRI" --until screen --no-state
```

This prints every candidate with its scores (`R` relevance, `Q` quality, `E` engaging) and records nothing. Choose minimums that keep a handful of papers per week. The model's absolute scores can shift from one day to another (the same papers scored about 3 points lower on a later day in testing), so keep the minimums comfortably below the top of the range and let `top_n` do the selecting. `~...-latest` is an alias that follows new model snapshots; each result records the snapshot that produced it.

### NotebookLM

```yaml
notebooklm:
  delete_notebook: true      # delete the notebook once audio + show notes are saved (also if the build fails)
  min_sources: 2             # papers NotebookLM must accept; fewer = no episode this time
  generation_timeout: 1800   # seconds to wait for the audio
  # source_timeout: 300      # seconds per PDF to be processed
  # generation_attempts: 2
  # profile: default         # notebooklm-py login profile
```

### Site

```yaml
site:
  upload_url: https://example.org/podcast/upload.php   # or SITE_UPLOAD_URL in .env
  chunk_mb: 8        # largest single upload request; the server can lower it
  retries: 4
```

Uploads are chunked, resumable and verified with a SHA-256 checksum, so the size limits of your hosting do not matter. Use the exact address of `upload.php`: if your site redirects (for example to `www.`), the client follows redirects within the same site, but it warns you to correct the address.

---

## Running the pipeline

```bash
venv3.13/bin/podcast run --topic "MSK MRI" --publish     # one topic, full pipeline, upload to the site
venv3.13/bin/podcast run --publish                       # every topic in config.yaml
venv3.13/bin/podcast run --topic "MSK MRI"               # build the episode but do not upload

venv3.13/bin/podcast run --topic "MSK MRI" --until screen --no-state   # dry run: search and score only
venv3.13/bin/podcast run --topic "MSK MRI" --until fetch               # ... and download the PDFs
venv3.13/bin/podcast publish                             # upload every built-but-unpublished episode
venv3.13/bin/podcast publish data/episodes/<run>/<topic> # upload one specific episode folder
```

`--topic` must match a topic `name` exactly. The exit code is 1 if any topic or upload failed. Add `-v` (before the subcommand) for debug output and `--config other.yaml` to use another configuration.

Everything it produces stays under `data/`:

| Path | Content |
|---|---|
| `data/state.db` | SQLite: every paper seen (and its status), built and published episodes |
| `data/pdfs/<run>/<topic>/` | Downloaded PDFs |
| `data/episodes/<run>/<topic>/` | `episode.m4a`, `episode.json` (title, summary, papers, licenses), `published.json` |
| `data/logs/` | Cron logs, see below |

---

## Scheduling with cron

The folder `deploy/` has what cron needs:

| File | What it does |
|---|---|
| `deploy/run_topic.sh "<topic>"` | Runs one topic and publishes it. One run at a time (lock), stops a hung run after 4 h, logs to `data/logs/podcast-YYYY-MM.log`, appends failures to `data/logs/failures.log` and prints them on stderr |
| `deploy/keepalive.sh` | Refreshes the NotebookLM login every 20 minutes (the interval notebooklm-py recommends). Silent while healthy; reports the first failure once |
| `deploy/crontab.example` | The crontab lines |

Install by adding the lines of `deploy/crontab.example` to `crontab -e`, with your path and topic names:

```cron
# Episodes at 01:00 local time: Monday / Wednesday / Thursday
0 1 * * 1  /path/to/auto_scientific_podcast/deploy/run_topic.sh "MSK MRI"
0 1 * * 3  /path/to/auto_scientific_podcast/deploy/run_topic.sh "Segmentation MRI"
0 1 * * 4  /path/to/auto_scientific_podcast/deploy/run_topic.sh "AI Recon MRI"
# NotebookLM login keepalive
*/20 * * * *  /path/to/auto_scientific_podcast/deploy/keepalive.sh
```

Things to know:

* Times are the machine's **local time**. The computer must be **on** at that time and between runs: cron does not catch up on missed runs.
* Give each topic its own day when `window_days` is 7, so each run looks at a week of new papers.
* Extra arguments after the topic go to `podcast run`, so `deploy/run_topic.sh "MSK MRI" --until screen --no-state` is a safe test of the exact cron setup.
* Cron mails any output to the local user (or to `MAILTO=you@example.org` at the top of the crontab) if local mail works. The one-line failure messages are meant for that. Without mail, look at `data/logs/failures.log`.

### If a run fails

Fix the cause (log in again with `notebooklm login` if the login expired, wait for the site to come back, ...) and run it again. A rerun reuses everything it can:

```bash
deploy/run_topic.sh "MSK MRI"       # reuses screened papers and already downloaded PDFs
venv3.13/bin/podcast publish        # only upload: the audio is already built, NotebookLM is not used again
sqlite3 -header -column data/state.db "select topic, status, count(*) n from papers group by 1,2; select run_id, topic, status from episodes;"
```

Papers of a failed build are kept for `carryover_days`, including after a crash or power loss.

---

## Deploying the podcast website

The `site/` folder is a small PHP application: `upload.php` receives episodes, `feed.php` generates the RSS feed (served as `/feed.xml`), `index.php` is the public page with one card per episode (player, download button, key takeaways and the papers with their licenses). It needs no framework and no Composer.

### 1. Create the database

Create an empty MySQL/MariaDB database and a user for it. The tables are created automatically at the first upload (the user needs `CREATE` and the usual table privileges). To share a database with other applications, set a `table_prefix` in `config.php`.

### 2. Write `config.php`

```bash
cp site/config.example.php site/config.php
```

Fill in the database login, the upload token (the same value as `SITE_UPLOAD_TOKEN` in `.env`), the public URL and your podcast details. Every key is described [below](#adapting-configphp). `config.php` is git-ignored and blocked from the web by `.htaccess`.

### 3. Add your cover image

Put a square JPG or PNG of **at least 1400 × 1400 px** (at most 3000 × 3000) next to `index.php` and set `image` in `config.php` to its name. Spotify and Apple reject feeds without proper cover art. Keep it well under 500 KB and do not include logos you have no right to use (for instance Spotify's).

### 4. Upload the files

Upload everything in `site/` to the folder on your server, for example `public_html/podcast/`, with FTP, SFTP or rsync:

* **Include the hidden files** `.htaccess`, `.user.ini`, `media/.htaccess` and `incoming/.htaccess`; many FTP clients hide dotfiles by default. They block access to secrets and stop uploaded files from executing.
* Do **not** upload `README.md` (it would be publicly readable), your local `media/*` test files, or `incoming/*.part`.
* Make `media/` and `incoming/` **writable by PHP** (typically mode 755 or 775, owned by the web user).

### 5. Check it

```bash
SITE=https://example.org/podcast
for f in config.php lib.php schema.sql .user.ini incoming/x.part media/; do
  printf '%-18s %s\n' "$f" "$(curl -s -o /dev/null -w '%{http_code}' $SITE/$f)"; done   # all 403 (or 404)
curl -s -o /dev/null -w 'index %{http_code}\n'      $SITE/            # 200, "No episodes yet"
curl -s -o /dev/null -w 'feed  %{http_code}\n'      $SITE/feed.xml    # 200
curl -s -w ' -> HTTP %{http_code}\n' $SITE/upload.php                 # {"error": "Missing or invalid upload token."} -> 401
```

If `config.php` or `lib.php` answer **200**, the `.htaccess` is not being applied: ask your host to allow `AllowOverride All` (or at least `FileInfo`, `Limit` and `Options`) for the folder, or move `config.php` out of the web root.

### 6. Connect the pipeline and publish a first episode

Put `site.upload_url` in `config.yaml` (or `SITE_UPLOAD_URL` in `.env`) and the token in `.env`, then:

```bash
venv3.13/bin/podcast run --topic "MSK MRI" --publish
```

Open the page: the episode card is there, and `feed.xml` lists it. Then [submit the feed to Spotify](#submitting-the-feed-to-spotify).

### Upload limits

The upload is chunked, so PHP's `upload_max_filesize` and `post_max_size` only limit one request, not the episode. Each request must stay below the smaller of the two; the server reports its limit and the pipeline adapts. `.user.ini` raises the limits where PHP runs as CGI/FPM; otherwise any limit of about 1 MB or more works. The protocol is documented in [`site/README.md`](site/README.md).

### A note on nginx

`.htaccess` is ignored by nginx, so repeat its rules in the server configuration: rewrite `/feed.xml` to `feed.php`; return 403 for `config.php`, `lib.php`, `schema.sql`, `.user.ini` and the whole `incoming/` folder; do not execute PHP inside `media/`; pass the `Authorization` header to PHP (`fastcgi_param HTTP_AUTHORIZATION $http_authorization;`); and raise `client_max_body_size` above your chunk size. This setup has not been tested here.

---

## Adapting `config.php`

`site/config.php` returns one PHP array. Only the first four blocks are mandatory to change.

| Key | Meaning |
|---|---|
| `db.host`, `db.port`, `db.name`, `db.user`, `db.pass` | MySQL/MariaDB connection |
| `table_prefix` | Prefix for the table names (`podcast_` gives `podcast_episodes` and `podcast_episode_papers`), so the tables can share a database. Letters, digits, `_`, at most 32; empty is allowed |
| `upload_token` | Shared secret, at least 32 characters, identical to `SITE_UPLOAD_TOKEN` in the pipeline's `.env`. Anyone with it can publish episodes: keep it secret and rotate it if it leaks |
| `base_url` | Public address of this folder **without trailing slash**, for example `https://example.org/podcast`. It builds the links in the RSS feed, so set it explicitly |
| `max_audio_mb` | Largest accepted episode (default 200) |
| `chunk_ttl_hours` | Unfinished chunked uploads are deleted after this long (default 24) |
| `page_size` | Episodes per page on the website (default 12) |
| `podcast.title` | Name of the show, shown on the page and in Spotify |
| `podcast.description` | One or two sentences about the show |
| `podcast.language` | RSS language code such as `en`, `it`, `de` |
| `podcast.link` | Address of the website page |
| `podcast.author`, `podcast.owner_name` | Names shown in the feed |
| `podcast.owner_email` | **Spotify sends its verification code to this address**, so it must be a mailbox you can read. It also appears in the public feed |
| `podcast.image` | Cover image: a file name next to `index.php` or an absolute URL |
| `podcast.category` | Top-level podcast category, for example `Science` |
| `podcast.explicit` | `true` or `false` |
| `podcast.copyright` | Optional copyright line for the feed |

Changing `title`, `image` or `category` updates the website and feed immediately; podcast apps pick it up the next time they read the feed.

---

## Customizing the website

* **Look and feel:** edit `site/assets/style.css`. Colors and fonts are CSS variables at the top (`--paper`, `--prussian`, `--serif`, ...), with a dark-mode block below them. The thin bar on top of each card has one segment per paper (solid = published, hatched = preprint); `.rail` and `.seg` style it.
* **Layout and wording:** `site/index.php` (cards, filters, pagination, footer text).
* **Feed content:** `site/feed.php` builds the show notes shown in podcast apps (summary, takeaways, papers with DOI links and licenses).
* **Episode text:** the title, summary and takeaways come from NotebookLM; steer them with `podcast.instructions` per topic in `config.yaml`, or change the prompt in `src/podcast/notebook.py` (`SHOW_NOTES_PROMPT`).

---

## Submitting the feed to Spotify

1. Check `https://example.org/podcast/feed.xml` loads, has at least one episode, and shows the right title, cover (1400 px or more) and owner email.
2. Go to **creators.spotify.com**, sign in, and choose to add a podcast you **already have** (import by RSS feed). Paste the feed address.
3. Spotify emails a code to `podcast.owner_email`; enter it.
4. Confirm language, country and category and submit. The show appears within hours to about a day, and new episodes arrive on their own as Spotify re-reads the feed. Apple Podcasts and other apps accept the same address.

Menu names on Spotify's site change from time to time; follow its on-screen wording.

---

## When something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| `podcast publish` says the server rejected the token (401) | `SITE_UPLOAD_TOKEN` in `.env` differs from `upload_token` in the server's `config.php` |
| Upload fails with a redirect message | `upload_url` points to a different site than the one that answers. Use the final address (for example with `www.`) |
| 0 usable papers although the topic is busy | The thresholds are too high for the model's current scores. Run the dry run above and lower `min_relevance`/`min_quality` |
| A source returns few or no papers | Check the keywords (all words must occur) and `window_days`. Europe PMC has almost no indexed abstracts for brand-new papers, which is why it is searched in full text |
| NotebookLM errors about login or authentication | Run `venv3.13/bin/notebooklm login`; look at `data/logs/keepalive.log` |
| "No such topic" | `--topic` and the cron entries must match `name:` in `config.yaml` exactly |
| Spotify rejects the feed | Cover smaller than 1400 px, a logo in the cover, missing owner email, or no episode yet |
| Page shows nothing after an upload | Check the PHP error log; the database user needs privileges for the tables and `table_prefix` must be valid |
| A run seems stuck | `pgrep -fa "podcast run"`; the lock is released when the process ends, and `run_topic.sh` stops a run after 4 hours |

---

## Project layout, tests, license

```
src/podcast/        the pipeline: sources/ (Europe PMC, OpenAlex, arXiv, bioRxiv), dedupe.py, screen.py,
                    fetch_pdf.py, notebook.py, publish.py, pipeline.py, state.py, cli.py
site/               the PHP website: upload.php, feed.php, index.php, lib.php, schema.sql, assets/, media/, incoming/
deploy/             cron scripts: run_topic.sh, keepalive.sh, crontab.example
tests/              pytest suite (no network needed): venv3.13/bin/pytest -q
config.example.yaml configuration template (copy to config.yaml)
```

Licensed under the **Apache License 2.0**, see [`LICENSE`](LICENSE).
