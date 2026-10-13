<?php
// Shared helpers for upload.php, feed.php and index.php. Needs PHP >= 7.4 with PDO MySQL; no other extensions.
declare(strict_types=1);

function cfg(?string $key = null)
{
    static $cfg = null;
    if ($cfg === null) {
        $file = __DIR__ . '/config.php';
        if (!is_file($file)) {
            plain_error(500, 'Server is not configured: config.php is missing.');
        }
        $cfg = require $file;
    }
    return $key === null ? $cfg : ($cfg[$key] ?? null);
}

// ---------------------------------------------------------------- responses

function json_out(int $status, array $data): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT) . "\n";
    exit;
}

function plain_error(int $status, string $message): void
{
    http_response_code($status);
    header('Content-Type: text/plain; charset=utf-8');
    echo $message . "\n";
    exit;
}

// ---------------------------------------------------------------- database

function db(): PDO
{
    static $pdo = null;
    if ($pdo === null) {
        $d = cfg('db');
        $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4', $d['host'], (int)($d['port'] ?? 3306), $d['name']);
        try {
            $pdo = new PDO($dsn, $d['user'], $d['pass'], [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES => false,
            ]);
        } catch (PDOException $e) {
            error_log('podcast: database connection failed: ' . $e->getMessage());
            plain_error(500, 'Database connection failed.');
        }
    }
    return $pdo;
}

/** Validated table_prefix from config.php (it becomes part of SQL identifiers, so nothing else is allowed). */
function table_prefix(): string
{
    $prefix = (string)cfg('table_prefix');
    if (!preg_match('/^[A-Za-z0-9_]{0,32}$/', $prefix)) {
        plain_error(500, 'Invalid table_prefix in config.php: use up to 32 letters, digits or underscores.');
    }
    return $prefix;
}

/** Quoted, prefixed table name: tbl('episodes') -> `podcast_episodes` with table_prefix "podcast_". */
function tbl(string $name): string
{
    return '`' . table_prefix() . $name . '`';
}

function ensure_schema(PDO $pdo): void
{
    $sql = file_get_contents(__DIR__ . '/schema.sql');
    $sql = str_replace(['{{episodes}}', '{{episode_papers}}', '{{p}}'],
        [tbl('episodes'), tbl('episode_papers'), table_prefix()], (string)$sql);
    $sql = preg_replace('/^\s*--.*$/m', '', (string)$sql);
    foreach (preg_split('/;\s*(\r?\n|$)/', (string)$sql) as $statement) {
        if (trim($statement) !== '') {
            $pdo->exec($statement);
        }
    }
}

/** True when the failure is "table does not exist" (nothing uploaded yet). */
function table_missing(PDOException $e): bool
{
    return $e->getCode() === '42S02';
}

function decode_episode(array $row): array
{
    $row['takeaways'] = json_decode((string)$row['takeaways_json'], true) ?: [];
    $row['papers'] = [];
    return $row;
}

/** @return array[] episodes (newest first) with their papers attached */
function fetch_episodes(?string $topic = null, ?string $slug = null, int $limit = 12, int $offset = 0): array
{
    $where = [];
    $args = [];
    if ($topic !== null && $topic !== '') {
        $where[] = 'topic = ?';
        $args[] = $topic;
    }
    if ($slug !== null && $slug !== '') {
        $where[] = 'slug = ?';
        $args[] = $slug;
    }
    $sql = 'SELECT * FROM ' . tbl('episodes') . ($where ? ' WHERE ' . implode(' AND ', $where) : '')
        . ' ORDER BY published_at DESC, id DESC LIMIT ' . max(1, $limit) . ' OFFSET ' . max(0, $offset);
    try {
        $st = db()->prepare($sql);
        $st->execute($args);
        $episodes = array_map('decode_episode', $st->fetchAll());
        if (!$episodes) {
            return [];
        }
        $byId = [];
        foreach ($episodes as $i => $e) {
            $byId[(int)$e['id']] = $i;
        }
        $in = implode(',', array_fill(0, count($byId), '?'));
        $ps = db()->prepare('SELECT * FROM ' . tbl('episode_papers') . " WHERE episode_id IN ($in) ORDER BY episode_id, position");
        $ps->execute(array_keys($byId));
        foreach ($ps->fetchAll() as $p) {
            $p['authors'] = json_decode((string)$p['authors_json'], true) ?: [];
            $episodes[$byId[(int)$p['episode_id']]]['papers'][] = $p;
        }
        return $episodes;
    } catch (PDOException $e) {
        if (table_missing($e)) {
            return [];
        }
        throw $e;
    }
}

function count_episodes(?string $topic = null): int
{
    try {
        $st = db()->prepare('SELECT COUNT(*) FROM ' . tbl('episodes') . ($topic ? ' WHERE topic = ?' : ''));
        $st->execute($topic ? [$topic] : []);
        return (int)$st->fetchColumn();
    } catch (PDOException $e) {
        if (table_missing($e)) {
            return 0;
        }
        throw $e;
    }
}

/** @return string[] topics that have at least one episode */
function list_topics(): array
{
    try {
        return db()->query('SELECT topic FROM ' . tbl('episodes') . ' GROUP BY topic ORDER BY MAX(published_at) DESC')->fetchAll(PDO::FETCH_COLUMN);
    } catch (PDOException $e) {
        if (table_missing($e)) {
            return [];
        }
        throw $e;
    }
}

// ---------------------------------------------------------------- auth

function request_token(): string
{
    $header = $_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '';
    if ($header === '' && function_exists('getallheaders')) {
        foreach (getallheaders() as $name => $value) {
            if (strcasecmp((string)$name, 'Authorization') === 0) {
                $header = (string)$value;
            }
        }
    }
    if (preg_match('/^Bearer\s+(\S+)$/i', trim($header), $m)) {
        return $m[1];
    }
    // Some shared hosts strip the Authorization header; X-Upload-Token is the fallback.
    return trim($_SERVER['HTTP_X_UPLOAD_TOKEN'] ?? '');
}

function require_upload_auth(): void
{
    $expected = (string)cfg('upload_token');
    if (strlen($expected) < 32) {
        json_out(500, ['error' => 'upload_token is not configured (needs at least 32 characters).']);
    }
    if (!hash_equals($expected, request_token())) {
        usleep(400000); // slow down guessing
        header('WWW-Authenticate: Bearer');
        json_out(401, ['error' => 'Missing or invalid upload token.']);
    }
}

// ---------------------------------------------------------------- strings, urls, formatting

function h($value): string
{
    return htmlspecialchars((string)$value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/** Escape for XML text/attributes after dropping characters XML 1.0 forbids. */
function xml_esc($value): string
{
    $s = preg_replace('/[^\x{9}\x{A}\x{D}\x{20}-\x{D7FF}\x{E000}-\x{FFFD}\x{10000}-\x{10FFFF}]/u', '', (string)$value);
    return htmlspecialchars($s === null ? '' : $s, ENT_XML1 | ENT_QUOTES, 'UTF-8');
}

/** CDATA section that survives "]]>" inside the content. */
function cdata($value): string
{
    $s = preg_replace('/[^\x{9}\x{A}\x{D}\x{20}-\x{D7FF}\x{E000}-\x{FFFD}\x{10000}-\x{10FFFF}]/u', '', (string)$value);
    return '<![CDATA[' . str_replace(']]>', ']]]]><![CDATA[>', $s === null ? '' : $s) . ']]>';
}

function safe_url(?string $url): ?string
{
    if ($url === null || $url === '' || !preg_match('#^https?://[^\s<>"\']+$#i', $url)) {
        return null;
    }
    return $url;
}

function slugify(string $text, int $max = 60): string
{
    if (function_exists('iconv')) {
        $t = @iconv('UTF-8', 'ASCII//TRANSLIT//IGNORE', $text);
        if ($t !== false) {
            $text = $t;
        }
    }
    $slug = trim(strtolower((string)preg_replace('/[^A-Za-z0-9]+/', '-', $text)), '-');
    $slug = trim(substr($slug, 0, $max), '-');
    return $slug !== '' ? $slug : 'episode';
}

function str_cut(string $s, int $max): string
{
    return function_exists('mb_substr') ? mb_substr($s, 0, $max, 'UTF-8') : substr($s, 0, $max);
}

function base_url(): string
{
    $configured = (string)cfg('base_url');
    if ($configured !== '') {
        return rtrim($configured, '/');
    }
    $https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
        || (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https');
    $dir = rtrim(str_replace('\\', '/', dirname($_SERVER['SCRIPT_NAME'] ?? '/')), '/');
    return ($https ? 'https' : 'http') . '://' . ($_SERVER['HTTP_HOST'] ?? 'localhost') . $dir;
}

function media_url(string $file): string
{
    return base_url() . '/media/' . rawurlencode($file);
}

function format_duration(?int $seconds): string
{
    if (!$seconds) {
        return '';
    }
    $h = intdiv($seconds, 3600);
    $m = intdiv($seconds % 3600, 60);
    $s = $seconds % 60;
    if ($h > 0) {
        return sprintf('%d h %02d min', $h, $m);
    }
    return $m > 0 ? sprintf('%d min %02d s', $m, $s) : sprintf('%d s', $s);
}

function format_bytes(int $bytes): string
{
    return $bytes >= 1048576 ? sprintf('%.1f MB', $bytes / 1048576) : sprintf('%d KB', max(1, (int)round($bytes / 1024)));
}

function format_date(string $ymd): string
{
    try {
        return (new DateTimeImmutable($ymd))->format('j M Y');
    } catch (Exception $e) {
        return $ymd;
    }
}

function utc(string $mysqlDatetime): DateTimeImmutable
{
    return new DateTimeImmutable($mysqlDatetime, new DateTimeZone('UTC'));
}
