<?php
// Podcast RSS feed (RSS 2.0 + iTunes tags, as consumed by Spotify / Apple Podcasts). Served as /feed.xml via .htaccess.
// Optional ?topic=<name> returns a feed for one topic only.
declare(strict_types=1);

require __DIR__ . '/lib.php';

$podcast = cfg('podcast');
$topic = isset($_GET['topic']) && is_string($_GET['topic']) && $_GET['topic'] !== '' ? $_GET['topic'] : null;
$episodes = fetch_episodes($topic, null, 100, 0);

$base = base_url();
$selfUrl = $base . '/feed.xml' . ($topic ? '?topic=' . rawurlencode($topic) : '');

// Conditional GET: cheap for the polling podcast clients.
$lastModified = $episodes ? utc($episodes[0]['published_at']) : new DateTimeImmutable('@' . (int)filemtime(__DIR__ . '/config.php'));
$etag = '"' . md5($selfUrl . count($episodes) . $lastModified->format('U') . ($episodes ? $episodes[0]['id'] : 0) . filemtime(__DIR__ . '/config.php')) . '"';
header('ETag: ' . $etag);
header('Last-Modified: ' . gmdate('D, d M Y H:i:s', (int)$lastModified->format('U')) . ' GMT');
header('Cache-Control: public, max-age=600');
if (($_SERVER['HTTP_IF_NONE_MATCH'] ?? '') === $etag) {
    http_response_code(304);
    exit;
}
header('Content-Type: application/rss+xml; charset=utf-8');

function el(string $name, $value, array $attrs = []): string
{
    $a = '';
    foreach ($attrs as $k => $v) {
        $a .= ' ' . $k . '="' . xml_esc($v) . '"';
    }
    return "<$name$a>" . xml_esc($value) . "</$name>";
}

function plain(string $html): string
{
    return trim(preg_replace('/\s+/', ' ', html_entity_decode(strip_tags($html), ENT_QUOTES, 'UTF-8')));
}

/** HTML show notes: summary, takeaways, then the papers with links and licenses. */
function show_notes(array $e): string
{
    $html = '';
    if ($e['summary'] !== '') {
        $html .= '<p>' . h($e['summary']) . '</p>';
    }
    if ($e['takeaways']) {
        $html .= '<p><strong>Key takeaways</strong></p><ul>';
        foreach ($e['takeaways'] as $t) {
            $html .= '<li>' . h($t) . '</li>';
        }
        $html .= '</ul>';
    }
    $html .= '<p><strong>Papers covered</strong></p><ul>';
    foreach ($e['papers'] as $p) {
        $link = safe_url($p['url']) ?? ($p['doi'] ? 'https://doi.org/' . $p['doi'] : null);
        $html .= '<li>' . h($p['citation']);
        if ($p['preprint']) {
            $html .= ' (preprint, not peer reviewed)';
        }
        if ($link) {
            $html .= ' <a href="' . h($link) . '">' . h($p['doi'] ? 'doi:' . $p['doi'] : 'link') . '</a>';
        }
        $html .= ' License: ' . h($p['license']) . '.</li>';
    }
    $html .= '</ul>';
    if ($e['disclaimer'] !== '') {
        $html .= '<p><em>' . h($e['disclaimer']) . '</em></p>';
    }
    return $html;
}

$title = (string)$podcast['title'] . ($topic ? ': ' . $topic : '');
$image = $podcast['image'] ?? '';
if ($image !== '' && !preg_match('#^https?://#i', $image)) {
    $image = $base . '/' . ltrim($image, '/');
}

echo '<?xml version="1.0" encoding="UTF-8"?>' . "\n";
echo '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
    . 'xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">' . "\n<channel>\n";
echo el('title', $title) . "\n";
echo el('link', $podcast['link'] ?: $base . '/') . "\n";
echo '<atom:link href="' . xml_esc($selfUrl) . '" rel="self" type="application/rss+xml"/>' . "\n";
echo '<description>' . cdata($podcast['description']) . "</description>\n";
echo el('language', $podcast['language'] ?: 'en') . "\n";
if (!empty($podcast['copyright'])) {
    echo el('copyright', $podcast['copyright']) . "\n";
}
echo el('lastBuildDate', $lastModified->format(DATE_RSS)) . "\n";
echo el('generator', 'auto_scientific_podcast') . "\n";
echo el('itunes:author', $podcast['author']) . "\n";
echo el('itunes:type', 'episodic') . "\n";
echo el('itunes:explicit', !empty($podcast['explicit']) ? 'true' : 'false') . "\n";
echo '<itunes:summary>' . cdata($podcast['description']) . "</itunes:summary>\n";
echo '<itunes:owner>' . el('itunes:name', $podcast['owner_name'] ?: $podcast['author'])
    . el('itunes:email', $podcast['owner_email']) . "</itunes:owner>\n";
if ($image !== '') {
    echo '<itunes:image href="' . xml_esc($image) . '"/>' . "\n";
    echo '<image>' . el('url', $image) . el('title', $title) . el('link', $podcast['link'] ?: $base . '/') . "</image>\n";
}
if (!empty($podcast['category'])) {
    echo '<itunes:category text="' . xml_esc($podcast['category']) . '"/>' . "\n";
}

foreach ($episodes as $e) {
    $notes = show_notes($e);
    $summaryText = $e['summary'] !== '' ? $e['summary'] : plain($notes);
    echo "<item>\n";
    echo el('title', $e['title']) . "\n";
    echo el('link', $base . '/?e=' . rawurlencode($e['slug'])) . "\n";
    echo el('guid', $e['guid'], ['isPermaLink' => 'false']) . "\n";
    echo el('pubDate', utc($e['published_at'])->format(DATE_RSS)) . "\n";
    echo '<description>' . cdata($notes) . "</description>\n";
    echo '<content:encoded>' . cdata($notes) . "</content:encoded>\n";
    echo '<enclosure url="' . xml_esc(media_url($e['audio_file'])) . '" length="' . (int)$e['audio_bytes']
        . '" type="' . xml_esc($e['audio_mime']) . '"/>' . "\n";
    echo el('itunes:title', $e['title']) . "\n";
    echo el('itunes:summary', $summaryText) . "\n";
    if ($e['duration_seconds']) {
        echo el('itunes:duration', (int)$e['duration_seconds']) . "\n";
    }
    echo el('itunes:explicit', !empty($podcast['explicit']) ? 'true' : 'false') . "\n";
    echo el('itunes:episodeType', 'full') . "\n";
    echo "</item>\n";
}
echo "</channel>\n</rss>\n";
