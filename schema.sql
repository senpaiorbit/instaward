CREATE TABLE IF NOT EXISTS processed_media (
  media_code TEXT PRIMARY KEY,
  author_username TEXT NOT NULL,
  original_url TEXT,
  cached_download_url TEXT,
  published_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  archived INTEGER DEFAULT 0,
  archive_scanned INTEGER DEFAULT 0,
  repost_code TEXT,
  repost_pk TEXT
);

CREATE TABLE IF NOT EXISTS session_cache (
  key TEXT PRIMARY KEY,
  state_json TEXT NOT NULL,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
