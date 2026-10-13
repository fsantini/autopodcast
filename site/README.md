# Podcast site (PHP + MySQL)

Receives episodes from the pipeline, stores metadata in MySQL, serves a download page and a Spotify-ready RSS feed.

| File | Purpose |
|---|---|
| `config.php` | Secrets and channel details (DB login, `upload_token`, `base_url`, podcast info). Blocked by `.htaccess`, gitignored. |
| `upload.php` | `POST` endpoint used by the pipeline. Creates the tables on first use. |
| `feed.php` | RSS feed, served as `/feed.xml` (`?topic=` for one topic). |
| `index.php`, `assets/style.css` | Download page with one card per episode. `?e=<slug>` is a permalink. |
| `media/` | Uploaded audio (no script execution). |
| `schema.sql` | Tables, for manual setup. |

## Deploy
1. Create a MySQL database and put its login in `config.php`; set `base_url` to the public URL of this folder (no trailing slash) and fill in the `podcast` section (owner email, author, category).
2. Add a square `cover.jpg` (1400-3000 px) next to `index.php`; Spotify and Apple reject feeds without cover art.
3. Upload the folder (everything except `media/*` files). Make `media/` writable by PHP.
4. PHP's `upload_max_filesize` / `post_max_size` only cap a single request, not the episode: the pipeline uploads in chunks sized from `max_chunk_bytes` (see below). `.user.ini` raises the limits on CGI/FPM hosts; even without it, any limit of about 1 MB or more works.
5. Submit `https://<your-site>/feed.xml` once to Spotify for Creators.

## Upload API
All requests need `Authorization: Bearer <upload_token>` (or header `X-Upload-Token`).

**Single request** (files below PHP's `upload_max_filesize` / `post_max_size`):
```
POST upload.php   audio=<file>  metadata=<json form field>  [guid]
```

**Chunked** (no size limit beyond `max_audio_mb`; every request only carries one chunk):
```
GET  upload.php?upload_id=ID                  -> {status, received, max_chunk_bytes}   resume point + largest allowed chunk
POST upload_id=ID offset=N audio=<chunk>      -> {status: chunk_ok | chunk_duplicate, received}
POST upload_id=ID final=1 total_bytes sha256 metadata=<json> [guid] [audio+offset]   -> 201 created (verified + published)
POST upload_id=ID abort=1                     -> discard the partial file
```
- `upload_id`: 16-64 characters `A-Za-z0-9_-`, chosen by the client (a random hex string).
- A chunk is appended only if `offset` equals the bytes already received. A repeated chunk (lost response) is acknowledged
  without being appended twice (`chunk_duplicate`). A gap or overlap gives **409** with `received`: continue from there.
- The final request checks `total_bytes` and the SHA-256 of the whole file, then validates the metadata and publishes.
  Bad metadata gives **422** and keeps the assembled file: fix the metadata and repeat only the final request.
  A checksum mismatch discards the partial file (start again from offset 0).
- Repeating the final request after the episode was published returns **200** `exists` (safe retry).
- Partial files live in `incoming/` (not web accessible) and are deleted after `chunk_ttl_hours` (default 24) without activity.
- Pick the chunk size from `max_chunk_bytes` (about the smaller of the two PHP limits minus 64 KB); 4-8 MB is typical.

Other responses: 200 `exists` / 201 `created` `{id, page_url, audio_url, feed_url}`, 401 bad token, 413 too large,
415 not m4a/mp3, 422 invalid metadata (lists the problems).

Example (single request): `curl -X POST $URL/upload.php -H "Authorization: Bearer $TOKEN" -F audio=@episode.m4a -F "metadata=<episode.json" -F guid=run1:topic`
