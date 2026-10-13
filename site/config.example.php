<?php
// Copy to config.php and fill in. config.php holds secrets: it is blocked by .htaccess and must never be committed.
return [
    // MySQL / MariaDB
    'db' => [
        'host' => 'localhost',
        'port' => 3306,
        'name' => 'your_database',
        'user' => 'your_user',
        'pass' => 'your_password',
    ],

    // Prefix for the table names, so these tables can share a database with other applications
    // (e.g. 'podcast_' gives podcast_episodes and podcast_episode_papers). Letters, digits and underscores, max 32.
    'table_prefix' => 'podcast_',

    // Shared secret the pipeline sends as "Authorization: Bearer <token>" (>= 32 characters).
    // Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"
    'upload_token' => 'CHANGE_ME',

    // Public URL of this folder, without trailing slash (used in the RSS feed; set it explicitly).
    'base_url' => 'https://example.org/podcast',

    'max_audio_mb' => 200,
    'chunk_ttl_hours' => 24,   // unfinished chunked uploads are deleted after this long
    'page_size' => 12,

    // Channel details for Spotify / Apple Podcasts.
    'podcast' => [
        'title' => 'Research digests',
        'description' => 'Weekly audio overviews of the newest open-access papers on topics we follow.',
        'language' => 'en',
        'link' => 'https://example.org/podcast/',
        'author' => 'Your Name',
        'owner_name' => 'Your Name',
        'owner_email' => 'you@example.org',   // Spotify verifies the feed owner through this address
        'image' => 'cover.jpg',                // square JPG/PNG, 1400-3000 px; file or absolute URL
        'category' => 'Science',
        'explicit' => false,
        'copyright' => '',
    ],
];
