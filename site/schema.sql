-- Created automatically by upload.php (ensure_schema) on first upload. {{episodes}}, {{episode_papers}} and {{p}} are replaced
-- by the table_prefix from config.php; for a manual setup replace them yourself, e.g. {{p}} -> podcast_ and {{episodes}} -> `podcast_episodes`.
-- Requires MySQL 5.6+ / MariaDB 10.0+ (InnoDB, utf8mb4). Timestamps are stored in UTC.

CREATE TABLE IF NOT EXISTS {{episodes}} (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  guid VARCHAR(191) NOT NULL,                 -- idempotency key sent by the pipeline (or audio sha256)
  slug VARCHAR(160) NOT NULL,
  title VARCHAR(255) NOT NULL,
  topic VARCHAR(128) NOT NULL,
  language VARCHAR(16) NOT NULL DEFAULT 'en',
  episode_date DATE NOT NULL,                 -- the week the episode covers
  summary MEDIUMTEXT NOT NULL,
  takeaways_json MEDIUMTEXT NOT NULL,
  disclaimer TEXT NOT NULL,
  audio_file VARCHAR(255) NOT NULL,           -- file name inside media/
  audio_mime VARCHAR(64) NOT NULL,
  audio_bytes BIGINT UNSIGNED NOT NULL,
  duration_seconds INT UNSIGNED NULL,
  published_at DATETIME NOT NULL,             -- UTC, used for the RSS pubDate
  PRIMARY KEY (id),
  UNIQUE KEY {{p}}uq_episodes_guid (guid),
  UNIQUE KEY {{p}}uq_episodes_slug (slug),
  KEY {{p}}idx_episodes_published (published_at),
  KEY {{p}}idx_episodes_topic_published (topic, published_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{episode_papers}} (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  episode_id INT UNSIGNED NOT NULL,
  position SMALLINT UNSIGNED NOT NULL,
  title TEXT NOT NULL,
  authors_json MEDIUMTEXT NOT NULL,
  journal VARCHAR(255) NULL,
  published DATE NULL,
  doi VARCHAR(255) NULL,
  url VARCHAR(1000) NULL,
  license VARCHAR(64) NOT NULL DEFAULT 'unknown',
  preprint TINYINT(1) NOT NULL DEFAULT 0,
  citation TEXT NOT NULL,
  PRIMARY KEY (id),
  KEY {{p}}idx_papers_episode (episode_id, position),
  CONSTRAINT {{p}}fk_papers_episode FOREIGN KEY (episode_id) REFERENCES {{episodes}} (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
