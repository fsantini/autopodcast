<?php
// Episode upload endpoint. Auth: "Authorization: Bearer <upload_token>" (or header X-Upload-Token).
//
// A) Single request (small files; limited by PHP's upload_max_filesize / post_max_size):
//    POST  audio=<file>  metadata=<json>  [guid]
//
// B) Chunked (bypasses those limits; every request only carries one chunk):
//    POST  upload_id=<16-64 chars [A-Za-z0-9_-]>  offset=<byte offset>  audio=<chunk>      append a chunk
//    POST  upload_id  final=1  total_bytes  sha256  metadata=<json>  [guid]  [audio+offset]  finish: verify + publish
//    POST  upload_id  abort=1                                                                discard the partial file
//    GET   ?upload_id=<id>                                                                   {received, max_chunk_bytes}
//    A chunk is appended only when offset == bytes already received. A repeated chunk (lost response) is
//    acknowledged without being appended twice; a gap or overlap returns 409 with "received" so the client can resume.
declare(strict_types=1);

require __DIR__ . '/lib.php';

// ---------------------------------------------------------------- small helpers

function ini_bytes(string $key): int
{
    $v = trim((string)ini_get($key));
    if ($v === '') {
        return 0;
    }
    $n = (int)$v;
    switch (strtolower(substr($v, -1))) {
        case 'g': $n *= 1024; // no break
        case 'm': $n *= 1024; // no break
        case 'k': $n *= 1024;
    }
    return $n;
}

/** Largest chunk the PHP limits allow (leaves headroom for the other form fields). */
function max_chunk_bytes(): int
{
    $limits = array_filter([ini_bytes('upload_max_filesize'), ini_bytes('post_max_size')], static function ($v) {
        return $v > 0;
    });
    return $limits ? max(0, min($limits) - 65536) : 0; // 0 = no PHP limit
}

function max_audio_bytes(): int
{
    return (int)(cfg('max_audio_mb') ?: 200) * 1048576;
}

function text($v, int $max): string
{
    return is_string($v) ? str_cut(trim($v), $max) : '';
}

function valid_date($v): ?string
{
    if (!is_string($v) || !preg_match('/^\d{4}-\d{2}-\d{2}$/', $v)) {
        return null;
    }
    [$y, $m, $d] = array_map('intval', explode('-', $v));
    return checkdate($m, $d, $y) ? $v : null;
}

function truthy($v): bool
{
    return in_array(strtolower((string)$v), ['1', 'true', 'yes', 'on'], true);
}

function incoming_dir(): string
{
    $dir = __DIR__ . '/incoming';
    if (!is_dir($dir) && !mkdir($dir, 0700, true)) {
        json_out(500, ['error' => 'incoming directory is not writable.']);
    }
    if (!is_file($dir . '/.htaccess')) {
        @file_put_contents($dir . '/.htaccess', "Require all denied\n<IfModule !mod_authz_core.c>\nOrder allow,deny\nDeny from all\n</IfModule>\n");
    }
    return $dir;
}

function part_path(string $uploadId): string
{
    return incoming_dir() . '/' . $uploadId . '.part';
}

function require_upload_id($raw): string
{
    if (!is_string($raw) || !preg_match('/^[A-Za-z0-9_-]{16,64}$/', $raw)) {
        json_out(400, ['error' => 'upload_id must be 16-64 characters of A-Z a-z 0-9 _ -.']);
    }
    return $raw;
}

/** Delete partial uploads nobody touched for chunk_ttl_hours (default 24). */
function collect_stale_parts(): void
{
    $ttl = max(1, (int)(cfg('chunk_ttl_hours') ?: 24)) * 3600;
    foreach (glob(incoming_dir() . '/*.part') ?: [] as $f) {
        if (filemtime($f) < time() - $ttl) {
            @unlink($f);
        }
    }
}

function chunk_info(string $uploadId, int $received): array
{
    return ['upload_id' => $uploadId, 'received' => $received, 'max_chunk_bytes' => max_chunk_bytes()];
}

function upload_error(array $file): void
{
    $reasons = [
        UPLOAD_ERR_INI_SIZE => 'exceeds upload_max_filesize (' . ini_get('upload_max_filesize') . ') - use chunked upload',
        UPLOAD_ERR_FORM_SIZE => 'exceeds the form size limit',
        UPLOAD_ERR_PARTIAL => 'was only partially uploaded',
        UPLOAD_ERR_NO_FILE => 'is missing',
        UPLOAD_ERR_NO_TMP_DIR => 'cannot be stored (no temporary directory)',
        UPLOAD_ERR_CANT_WRITE => 'cannot be written to disk',
    ];
    json_out($file['error'] === UPLOAD_ERR_INI_SIZE ? 413 : 400,
        ['error' => 'Audio file ' . ($reasons[$file['error']] ?? 'upload failed') . '.', 'max_chunk_bytes' => max_chunk_bytes()]);
}

/** @return array{0:string,1:string}|null [extension, mime] from the file's magic bytes */
function sniff_audio(string $path): ?array
{
    $head = (string)file_get_contents($path, false, null, 0, 12);
    if (substr($head, 4, 4) === 'ftyp') {
        return ['m4a', 'audio/x-m4a'];
    }
    if (substr($head, 0, 3) === 'ID3' || (strlen($head) > 1 && ord($head[0]) === 0xFF && (ord($head[1]) & 0xE0) === 0xE0)) {
        return ['mp3', 'audio/mpeg'];
    }
    return null;
}

/** @return array{0:?array,1:string[]} [clean metadata, errors] */
function parse_metadata(string $raw): array
{
    $meta = json_decode($raw, true);
    if (!is_array($meta)) {
        return [null, ['metadata is not valid JSON']];
    }
    $errors = [];
    $m = [
        'title' => text($meta['title'] ?? null, 255),
        'topic' => text($meta['topic'] ?? null, 128),
        'date' => valid_date($meta['date'] ?? null),
        'language' => text($meta['language'] ?? 'en', 16) ?: 'en',
        'summary' => text($meta['summary'] ?? null, 10000),
        'disclaimer' => text($meta['disclaimer'] ?? null, 1000),
        'takeaways' => [],
        'papers' => [],
    ];
    if ($m['title'] === '') $errors[] = 'title is required';
    if ($m['topic'] === '') $errors[] = 'topic is required';
    if ($m['date'] === null) $errors[] = 'date must be YYYY-MM-DD';
    if (!preg_match('/^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$/', $m['language'])) $errors[] = 'language must look like "en" or "pt-BR"';

    foreach (array_slice(is_array($meta['takeaways'] ?? null) ? $meta['takeaways'] : [], 0, 50) as $t) {
        if (($t = text($t, 1000)) !== '') $m['takeaways'][] = $t;
    }
    $d = $meta['duration_seconds'] ?? null;
    $m['duration'] = (is_int($d) || is_float($d)) && $d > 0 && $d < 86400 * 2 ? (int)round($d) : null;

    $rawPapers = $meta['papers'] ?? null;
    if (!is_array($rawPapers) || count($rawPapers) < 1 || count($rawPapers) > 100) {
        $errors[] = 'papers must be a list of 1 to 100 entries';
    } else {
        foreach (array_values($rawPapers) as $i => $p) {
            if (!is_array($p) || text($p['title'] ?? null, 1000) === '') {
                $errors[] = "papers[$i].title is required";
                continue;
            }
            $authors = [];
            foreach (array_slice(is_array($p['authors'] ?? null) ? $p['authors'] : [], 0, 200) as $a) {
                if (($a = text($a, 200)) !== '') $authors[] = $a;
            }
            $m['papers'][] = [
                'title' => text($p['title'], 1000),
                'authors' => $authors,
                'journal' => text($p['journal'] ?? null, 255) ?: null,
                'published' => valid_date($p['published'] ?? null),
                'doi' => text($p['doi'] ?? null, 255) ?: null,
                'url' => safe_url(text($p['url'] ?? null, 1000)),
                'license' => text($p['license'] ?? null, 64) ?: 'unknown',
                'preprint' => !empty($p['preprint']) ? 1 : 0,
                'citation' => text($p['citation'] ?? null, 2000) ?: text($p['title'], 1000),
            ];
        }
    }
    return [$errors ? null : $m, $errors];
}

function existing_episode(PDO $pdo, string $guid): ?array
{
    $st = $pdo->prepare('SELECT id, slug, audio_file FROM ' . tbl('episodes') . ' WHERE guid = ?');
    $st->execute([$guid]);
    return $st->fetch() ?: null;
}

function reply_episode(int $status, string $state, array $row): void
{
    json_out($status, [
        'status' => $state,
        'id' => (int)$row['id'],
        'page_url' => base_url() . '/?e=' . rawurlencode($row['slug']),
        'audio_url' => media_url($row['audio_file']),
        'feed_url' => base_url() . '/feed.xml',
    ]);
}

/**
 * Move the finished audio into media/ and record the episode. $isUpload: file came from $_FILES (single request);
 * otherwise it is an assembled partial file. On DB failure the assembled file is put back so the client can retry.
 */
function store_episode(PDO $pdo, string $source, bool $isUpload, string $ext, string $mime, array $m, string $guid): void
{
    $mediaDir = __DIR__ . '/media';
    if (!is_dir($mediaDir) && !mkdir($mediaDir, 0755, true)) {
        json_out(500, ['error' => 'media directory is not writable.']);
    }
    $suffix = bin2hex(random_bytes(4));
    $slug = slugify($m['date'] . ' ' . $m['topic'] . ' ' . $m['title'], 140) . '-' . $suffix;
    $audioFile = $m['date'] . '-' . slugify($m['topic'] . ' ' . $m['title'], 50) . '-' . $suffix . '.' . $ext;
    $target = $mediaDir . '/' . $audioFile;
    if (!($isUpload ? move_uploaded_file($source, $target) : rename($source, $target))) {
        json_out(500, ['error' => 'Could not store the audio file.']);
    }
    chmod($target, 0644);

    try {
        $pdo->beginTransaction();
        $pdo->prepare('INSERT INTO ' . tbl('episodes') . ' (guid, slug, title, topic, language, episode_date, summary, takeaways_json, disclaimer,
                audio_file, audio_mime, audio_bytes, duration_seconds, published_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)')
            ->execute([$guid, $slug, $m['title'], $m['topic'], $m['language'], $m['date'], $m['summary'],
                json_encode($m['takeaways'], JSON_UNESCAPED_UNICODE), $m['disclaimer'], $audioFile, $mime,
                (int)filesize($target), $m['duration'], gmdate('Y-m-d H:i:s')]);
        $id = (int)$pdo->lastInsertId();
        $ins = $pdo->prepare('INSERT INTO ' . tbl('episode_papers') . ' (episode_id, position, title, authors_json, journal, published, doi, url,
                license, preprint, citation) VALUES (?,?,?,?,?,?,?,?,?,?,?)');
        foreach ($m['papers'] as $pos => $p) {
            $ins->execute([$id, $pos + 1, $p['title'], json_encode($p['authors'], JSON_UNESCAPED_UNICODE), $p['journal'],
                $p['published'], $p['doi'], $p['url'], $p['license'], $p['preprint'], $p['citation']]);
        }
        $pdo->commit();
    } catch (Throwable $e) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        if (!$isUpload) {
            @rename($target, $source); // keep the assembled file so the client can repeat the final request
        } else {
            @unlink($target);
        }
        if ($e instanceof PDOException && $e->getCode() === '23000' && ($row = existing_episode($pdo, $guid))) {
            if (!$isUpload) {
                @unlink($source);
            }
            reply_episode(200, 'exists', $row); // concurrent upload of the same guid won the race
        }
        error_log('podcast: storing episode failed: ' . $e->getMessage());
        json_out(500, ['error' => 'Could not store the episode.']);
    }
    reply_episode(201, 'created', ['id' => $id, 'slug' => $slug, 'audio_file' => $audioFile]);
}

function open_db(): PDO
{
    $pdo = db();
    try {
        ensure_schema($pdo);
    } catch (PDOException $e) {
        error_log('podcast: schema setup failed: ' . $e->getMessage());
        json_out(500, ['error' => 'Database setup failed (check CREATE privileges).']);
    }
    return $pdo;
}

// ---------------------------------------------------------------- chunked mode

/** Append one chunk under an exclusive lock. Returns [state, bytes received after the call]. */
function append_chunk(string $part, int $offset, string $chunkFile, int $len): array
{
    $fh = fopen($part, 'c+b');
    if (!$fh || !flock($fh, LOCK_EX)) {
        json_out(500, ['error' => 'Could not open the partial upload.']);
    }
    clearstatcache(true, $part);
    $size = (int)fstat($fh)['size'];
    if ($offset + $len <= $size) {
        flock($fh, LOCK_UN);
        fclose($fh);
        return ['duplicate', $size]; // already stored (the earlier response was probably lost)
    }
    if ($offset !== $size) {
        flock($fh, LOCK_UN);
        fclose($fh);
        json_out(409, ['error' => $offset > $size ? 'Gap: this chunk starts beyond the bytes received.'
            : 'Overlap: this chunk starts inside bytes already received.'] + chunk_info(basename($part, '.part'), $size));
    }
    if ($size + $len > max_audio_bytes()) {
        flock($fh, LOCK_UN);
        fclose($fh);
        json_out(413, ['error' => 'Audio would exceed the maximum of ' . (int)(max_audio_bytes() / 1048576) . ' MB.']);
    }
    $in = fopen($chunkFile, 'rb');
    fseek($fh, 0, SEEK_END);
    $written = (int)stream_copy_to_stream($in, $fh);
    fflush($fh);
    fclose($in);
    if ($written !== $len) {
        ftruncate($fh, $size); // undo a short write (disk full) so the client can retry this chunk
    }
    flock($fh, LOCK_UN);
    fclose($fh);
    if ($written !== $len) {
        json_out(500, ['error' => 'Could not write the chunk to disk.']);
    }
    return ['appended', $size + $len];
}

function handle_chunked(string $uploadId): void
{
    collect_stale_parts();
    $part = part_path($uploadId);

    if (truthy($_POST['abort'] ?? '')) {
        $existed = is_file($part) && unlink($part);
        json_out(200, ['status' => $existed ? 'aborted' : 'nothing_to_abort', 'upload_id' => $uploadId]);
    }

    $final = truthy($_POST['final'] ?? '');
    $file = $_FILES['audio'] ?? null;
    $state = null;

    if ($file && ($file['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_NO_FILE) {
        if ($file['error'] !== UPLOAD_ERR_OK) {
            upload_error($file);
        }
        if ($file['size'] <= 0) {
            json_out(400, ['error' => 'Empty chunk.']);
        }
        if (!isset($_POST['offset']) || !preg_match('/^\d+$/', (string)$_POST['offset'])) {
            json_out(400, ['error' => '"offset" (byte offset of this chunk, from 0) is required with each chunk.']);
        }
        [$state, $received] = append_chunk($part, (int)$_POST['offset'], $file['tmp_name'], (int)$file['size']);
    } elseif (!$final) {
        json_out(400, ['error' => 'Send an "audio" chunk, final=1, abort=1, or GET the status.']);
    }

    if (!$final) {
        json_out(200, ['status' => $state === 'duplicate' ? 'chunk_duplicate' : 'chunk_ok'] + chunk_info($uploadId, $received));
    }

    // ---- final request: verify, validate, publish
    $totalBytes = $_POST['total_bytes'] ?? '';
    $sha256 = strtolower((string)($_POST['sha256'] ?? ''));
    if (!preg_match('/^\d+$/', (string)$totalBytes) || !preg_match('/^[0-9a-f]{64}$/', $sha256)) {
        json_out(400, ['error' => 'final=1 requires total_bytes and sha256 (hex) of the complete file.']);
    }
    $guid = text($_POST['guid'] ?? null, 191) ?: 'sha256:' . $sha256;
    $pdo = open_db();
    if (!is_file($part)) {
        // The final request may be a retry after a lost response: the episode is then already published.
        if ($row = existing_episode($pdo, $guid)) {
            reply_episode(200, 'exists', $row);
        }
        json_out(404, ['error' => 'Unknown upload_id: no chunks received (or it expired). Start again from offset 0.']);
    }
    clearstatcache(true, $part);
    $size = (int)filesize($part);
    if ($size !== (int)$totalBytes) {
        json_out(409, ['error' => "Incomplete: $size of $totalBytes bytes received."] + chunk_info($uploadId, $size));
    }
    if (!hash_equals($sha256, (string)hash_file('sha256', $part))) {
        @unlink($part); // corrupt or mis-ordered data cannot be repaired by resuming
        json_out(422, ['error' => 'sha256 does not match the assembled file; the partial upload was discarded. Start again from offset 0.']);
    }
    if (!($type = sniff_audio($part))) {
        @unlink($part);
        json_out(415, ['error' => 'Unsupported audio format: expected .m4a (MPEG-4) or .mp3.']);
    }
    $raw = (string)($_POST['metadata'] ?? '');
    if ($raw === '' || strlen($raw) > 1048576) {
        json_out(400, ['error' => 'Missing "metadata" field (JSON, max 1 MB).'] + chunk_info($uploadId, $size));
    }
    [$m, $errors] = parse_metadata($raw);
    if ($m === null) {
        // The assembled file is kept: fix the metadata and repeat only the final request.
        json_out(422, ['error' => 'Invalid metadata.', 'details' => $errors] + chunk_info($uploadId, $size));
    }
    if ($row = existing_episode($pdo, $guid)) {
        @unlink($part);
        reply_episode(200, 'exists', $row);
    }
    store_episode($pdo, $part, false, $type[0], $type[1], $m, $guid);
}

// ---------------------------------------------------------------- single-request mode

function handle_single(): void
{
    $file = $_FILES['audio'] ?? null;
    if (!$file || !isset($file['error'])) {
        json_out(400, ['error' => 'Missing "audio" file field.']);
    }
    if ($file['error'] !== UPLOAD_ERR_OK) {
        upload_error($file);
    }
    if ($file['size'] <= 0 || $file['size'] > max_audio_bytes()) {
        json_out(413, ['error' => 'Audio file must be between 1 byte and ' . (int)(max_audio_bytes() / 1048576) . ' MB.']);
    }
    if (!($type = sniff_audio($file['tmp_name']))) {
        json_out(415, ['error' => 'Unsupported audio format: expected .m4a (MPEG-4) or .mp3.']);
    }
    $raw = (string)($_POST['metadata'] ?? '');
    if ($raw === '' || strlen($raw) > 1048576) {
        json_out(400, ['error' => 'Missing "metadata" field (JSON, max 1 MB).']);
    }
    [$m, $errors] = parse_metadata($raw);
    if ($m === null) {
        json_out(422, ['error' => 'Invalid metadata.', 'details' => $errors]);
    }
    $guid = text($_POST['guid'] ?? null, 191) ?: 'sha256:' . hash_file('sha256', $file['tmp_name']);
    $pdo = open_db();
    if ($row = existing_episode($pdo, $guid)) {
        reply_episode(200, 'exists', $row);
    }
    store_episode($pdo, $file['tmp_name'], true, $type[0], $type[1], $m, $guid);
}

// ---------------------------------------------------------------- dispatch

$method = $_SERVER['REQUEST_METHOD'] ?? '';
if ($method !== 'POST' && $method !== 'GET') {
    header('Allow: GET, POST');
    json_out(405, ['error' => 'Use POST (or GET ?upload_id= for the status of a chunked upload).']);
}
require_upload_auth();

if ($method === 'GET') {
    $uploadId = require_upload_id($_GET['upload_id'] ?? null);
    $part = part_path($uploadId);
    clearstatcache(true, $part);
    json_out(200, ['status' => is_file($part) ? 'in_progress' : 'unknown'] + chunk_info($uploadId, is_file($part) ? (int)filesize($part) : 0));
}

// A body larger than post_max_size arrives with empty $_POST and $_FILES.
if (empty($_FILES) && empty($_POST) && (int)($_SERVER['CONTENT_LENGTH'] ?? 0) > 0) {
    json_out(413, ['error' => 'Request exceeds post_max_size (' . ini_get('post_max_size') . '); send smaller chunks.',
        'max_chunk_bytes' => max_chunk_bytes()]);
}

if (isset($_POST['upload_id'])) {
    handle_chunked(require_upload_id($_POST['upload_id']));
} else {
    handle_single();
}
