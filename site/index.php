<?php
// Public download page: episode cards with player, download link and the papers behind each episode.
//   /                 newest episodes, paginated
//   /?topic=<name>    one topic
//   /?e=<slug>        a single episode (permalink, details expanded)
declare(strict_types=1);

require __DIR__ . '/lib.php';

$podcast = cfg('podcast');
$perPage = max(1, (int)(cfg('page_size') ?: 12));
$slug = isset($_GET['e']) && is_string($_GET['e']) ? $_GET['e'] : null;
$topic = isset($_GET['topic']) && is_string($_GET['topic']) && $_GET['topic'] !== '' ? $_GET['topic'] : null;
$page = max(1, (int)($_GET['page'] ?? 1));

if ($slug !== null) {
    $episodes = fetch_episodes(null, $slug, 1, 0);
    if (!$episodes) {
        http_response_code(404);
    }
    $total = count($episodes);
} else {
    $total = count_episodes($topic);
    $episodes = fetch_episodes($topic, null, $perPage, ($page - 1) * $perPage);
}
$topics = list_topics();
$pages = max(1, (int)ceil($total / $perPage));
$single = $slug !== null;

function href(array $params): string
{
    $params = array_filter($params, static function ($v) {
        return $v !== null && $v !== '' && $v !== 1;
    });
    return '?' . http_build_query($params);
}

$pageTitle = $single && $episodes ? $episodes[0]['title'] . ' - ' . $podcast['title'] : $podcast['title'];
$pageDesc = $single && $episodes ? str_cut($episodes[0]['summary'], 280) : (string)$podcast['description'];
$css = 'assets/style.css?v=' . (int)@filemtime(__DIR__ . '/assets/style.css');
?>
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= h($pageTitle) ?></title>
<meta name="description" content="<?= h($pageDesc) ?>">
<meta name="color-scheme" content="light dark">
<link rel="alternate" type="application/rss+xml" title="<?= h($podcast['title']) ?>" href="<?= h(base_url() . '/feed.xml') ?>">
<link rel="stylesheet" href="<?= h($css) ?>">
</head>
<body>
<header class="masthead">
  <div class="wrap masthead-row">
    <div>
      <h1><a href="./"><?= h($podcast['title']) ?></a></h1>
      <p class="lede"><?= h($podcast['description']) ?></p>
    </div>
    <a class="btn btn-solid" href="feed.xml" title="Add this address to Spotify, Apple Podcasts or any podcast app">Subscribe with RSS</a>
  </div>
</header>

<main class="wrap">
<?php if (!$single && count($topics) > 1): ?>
  <nav class="topics" aria-label="Topics">
    <a href="./"<?= $topic === null ? ' aria-current="page"' : '' ?>>All topics</a>
    <?php foreach ($topics as $t): ?>
      <a href="<?= h(href(['topic' => $t])) ?>"<?= $topic === $t ? ' aria-current="page"' : '' ?>><?= h($t) ?></a>
    <?php endforeach; ?>
  </nav>
<?php endif; ?>

<?php if ($single): ?>
  <p class="back"><a href="./">All episodes</a></p>
<?php endif; ?>

<?php if (!$episodes): ?>
  <section class="empty">
    <?php if ($single): ?>
      <h2>Episode not found</h2>
      <p>It may have been removed. <a href="./">Browse all episodes</a>.</p>
    <?php else: ?>
      <h2>No episodes yet</h2>
      <p>New episodes appear here after each weekly run. Subscribe with RSS to get them automatically.</p>
    <?php endif; ?>
  </section>
<?php else: ?>
  <p class="legend">
    <span class="rail-key"><i class="seg"></i> Peer-reviewed paper</span>
    <span class="rail-key"><i class="seg pre"></i> Preprint, not peer reviewed</span>
  </p>

  <div class="grid<?= $single ? ' single' : '' ?>">
  <?php foreach ($episodes as $e):
      $audio = media_url($e['audio_file']);
      $nPre = 0;
      foreach ($e['papers'] as $p) {
          $nPre += (int)$p['preprint'];
      }
      $nPapers = count($e['papers']);
      $railLabel = $nPapers . ($nPapers === 1 ? ' paper' : ' papers') . ($nPre ? ', ' . $nPre . ' preprint' . ($nPre > 1 ? 's' : '') : '');
  ?>
    <article class="card" id="<?= h($e['slug']) ?>" lang="<?= h($e['language']) ?>">
      <div class="rail" role="img" aria-label="<?= h($railLabel) ?>">
        <?php foreach ($e['papers'] as $p): ?>
          <span class="seg<?= $p['preprint'] ? ' pre' : '' ?>" title="<?= h($p['title']) ?>"></span>
        <?php endforeach; ?>
      </div>
      <div class="card-body">
        <p class="meta">
          <a class="topic" href="<?= h(href(['topic' => $e['topic']])) ?>"><?= h($e['topic']) ?></a>
          <time datetime="<?= h($e['episode_date']) ?>"><?= h(format_date($e['episode_date'])) ?></time>
          <?php if ($e['duration_seconds']): ?><span class="dur"><?= h(format_duration((int)$e['duration_seconds'])) ?></span><?php endif; ?>
        </p>
        <h2><a href="<?= h(href(['e' => $e['slug']])) ?>"><?= h($e['title']) ?></a></h2>
        <?php if ($e['summary'] !== ''): ?><p class="summary"><?= h($e['summary']) ?></p><?php endif; ?>

        <audio controls preload="none" src="<?= h($audio) ?>">
          Your browser cannot play audio. <a href="<?= h($audio) ?>">Download the episode</a>.
        </audio>

        <div class="actions">
          <a class="btn" href="<?= h($audio) ?>" download>Download <?= h(strtoupper(pathinfo($e['audio_file'], PATHINFO_EXTENSION))) ?>, <?= h(format_bytes((int)$e['audio_bytes'])) ?></a>
        </div>

        <details class="sources"<?= $single ? ' open' : '' ?>>
          <summary><?= h($railLabel) ?> behind this episode</summary>
          <?php if ($e['takeaways']): ?>
            <h3>Key takeaways</h3>
            <ul class="takeaways">
              <?php foreach ($e['takeaways'] as $t): ?><li><?= h($t) ?></li><?php endforeach; ?>
            </ul>
          <?php endif; ?>
          <h3>Papers</h3>
          <ol class="papers">
            <?php foreach ($e['papers'] as $p):
                $link = safe_url($p['url']) ?? ($p['doi'] ? 'https://doi.org/' . $p['doi'] : null);
            ?>
              <li>
                <span class="cite"><?= h($p['citation']) ?></span>
                <span class="tags">
                  <?php if ($p['preprint']): ?><span class="tag tag-pre">Preprint</span><?php endif; ?>
                  <span class="tag"><?= h($p['license']) ?></span>
                  <?php if ($link): ?><a href="<?= h($link) ?>" rel="noopener"><?= h($p['doi'] ? 'doi:' . $p['doi'] : 'Source') ?></a><?php endif; ?>
                </span>
              </li>
            <?php endforeach; ?>
          </ol>
          <?php if ($e['disclaimer'] !== ''): ?><p class="disclaimer"><?= h($e['disclaimer']) ?></p><?php endif; ?>
        </details>
      </div>
    </article>
  <?php endforeach; ?>
  </div>

  <?php if (!$single && $pages > 1): ?>
    <nav class="pager" aria-label="Pages">
      <?php if ($page > 1): ?><a class="btn" href="<?= h(href(['topic' => $topic, 'page' => $page - 1])) ?>">Newer episodes</a><?php endif; ?>
      <span>Page <?= $page ?> of <?= $pages ?></span>
      <?php if ($page < $pages): ?><a class="btn" href="<?= h(href(['topic' => $topic, 'page' => $page + 1])) ?>">Older episodes</a><?php endif; ?>
    </nav>
  <?php endif; ?>
<?php endif; ?>
</main>

<footer class="wrap foot">
  <p>Audio overviews are generated by AI from the open-access papers listed under each episode and can contain errors. Check the original papers before relying on them.</p>
</footer>
</body>
</html>
