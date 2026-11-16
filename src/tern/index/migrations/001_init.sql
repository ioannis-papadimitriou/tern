-- Index schema v1: architecture §4 (#5 as written) plus the additive two-way choices of #30.
-- Applied by tern.index.schema inside one transaction; schema_version is written by the runner.
-- No TB-issued id (tb_id, headerMessageId cache, cursor) is stored anywhere (#5, M10).

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);              -- schema_version, tb_profile_id

CREATE TABLE accounts(account_id TEXT PRIMARY KEY, name TEXT, type TEXT);

CREATE TABLE folders(folder_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, path TEXT NOT NULL,
  special_use TEXT,                                                -- JSON array (TB specialUse)
  tb_total INTEGER, last_reconciled_at INTEGER);

CREATE TABLE messages(mid TEXT PRIMARY KEY,                        -- sha256(normalized Message-ID) | 'x-'||sha256(raw header bytes || raw body bytes)
  header_message_id TEXT,                                          -- parsed Message-ID, brackets stripped, case kept; NULL for x- mids
  thread_id TEXT, date_utc INTEGER, from_addr TEXT, from_name TEXT,
  to_addrs TEXT, cc_addrs TEXT,                                    -- JSON arrays
  subject TEXT, in_reply_to TEXT,
  refs TEXT,                                                       -- JSON array
  lang TEXT,                                                       -- NULL in v1 (#30)
  size INTEGER, has_attachments INTEGER,
  body_text TEXT, parse_status TEXT CHECK(parse_status IN ('ok','partial','failed')),
  conflicted INTEGER NOT NULL DEFAULT 0,                           -- copies of this mid disagree (#5, T14)
  content_hash TEXT,                                               -- #30: the copy whose content fills this row
  conflict_class TEXT,                                             -- #30: classify_pair result while conflicted
  indexed_at INTEGER);

CREATE TABLE locations(mid TEXT NOT NULL REFERENCES messages(mid), folder_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,                                      -- #5 round 2; 'full-'… over RAW_MAX (#30)
  tags TEXT,                                                       -- JSON array, sorted
  flagged INTEGER, read INTEGER,
  tb_date INTEGER NOT NULL,                                        -- #30: TB PageItem date, epoch s; non-identity
  PRIMARY KEY(mid, folder_id));                                    -- no TB ids, ever
CREATE INDEX locations_folder_date ON locations(folder_id, tb_date);

-- #30: extra copies of a mid in one folder whose content_hash differs from the location row's.
-- Identical copies collapse into the location row.
CREATE TABLE same_folder_copies(mid TEXT NOT NULL REFERENCES messages(mid),
  folder_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  PRIMARY KEY(mid, folder_id, content_hash));

CREATE TABLE sent_recipients(addr TEXT PRIMARY KEY, first_seen INTEGER, count INTEGER);
                                                                   -- To/Cc of messages in Sent folders (§2 step 7, §6)
-- #30: mids already counted into sent_recipients (counting stays idempotent across re-runs).
CREATE TABLE sent_counted(mid TEXT PRIMARY KEY);

CREATE TABLE attachments(mid TEXT, idx INTEGER, filename TEXT, mime TEXT, size INTEGER,
  PRIMARY KEY(mid, idx));                                          -- metadata only in v1

CREATE TABLE chunks(chunk_id INTEGER PRIMARY KEY, mid TEXT NOT NULL, ord INTEGER,
  kind TEXT CHECK(kind IN ('subject','body','attachment_name')), text TEXT,
  fts_text TEXT NOT NULL);                                         -- #30: text folded (NFD, no Mn, NFC)
CREATE INDEX chunks_mid ON chunks(mid);

CREATE VIRTUAL TABLE chunks_fts USING fts5(fts_text, content='chunks', content_rowid='chunk_id',
  tokenize='unicode61 remove_diacritics 2');

CREATE TRIGGER chunks_fts_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, fts_text) VALUES (new.chunk_id, new.fts_text);
END;
CREATE TRIGGER chunks_fts_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, fts_text) VALUES ('delete', old.chunk_id, old.fts_text);
END;
CREATE TRIGGER chunks_fts_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, fts_text) VALUES ('delete', old.chunk_id, old.fts_text);
  INSERT INTO chunks_fts(rowid, fts_text) VALUES (new.chunk_id, new.fts_text);
END;

-- chunks_vec is created per embedding model in plan 05 (M9), with its dim from embedding_model.
CREATE TABLE embedding_model(model_id TEXT, dim INTEGER, quant TEXT, created_at INTEGER);

CREATE TABLE ingest_state(folder_id TEXT PRIMARY KEY,               -- '*' = jobs not tied to one folder (#30)
  watermark_date INTEGER,                                          -- oldest fully ingested window boundary
  watermark_mids TEXT,                                             -- JSON: mids seen at watermark_date
  status TEXT,                                                     -- also 'error:<ExceptionType>' (#30)
  updated_at INTEGER);                                             -- no TB cursor (session-scoped)

-- #30: items TB holds headers only (never fetched: getRaw would hit the network). mid =
-- mid_of(header_message_id) for non-'md5:' ids, NULL otherwise. ordinal = 0-based index, in page
-- order, among this folder's rows with the same tb_date; a re-page replaces its window's rows.
CREATE TABLE headers_only_items(folder_id TEXT NOT NULL, tb_date INTEGER NOT NULL,
  ordinal INTEGER NOT NULL, mid TEXT,
  PRIMARY KEY(folder_id, tb_date, ordinal));

-- #30: items not indexed, counted: 'large_noid' = over RAW_MAX with no Message-ID in the full
-- form; 'tb_error' = TB failed to read the item (raw/full gave tb_error; plan 04 merge review M5).
CREATE TABLE skipped_items(folder_id TEXT NOT NULL, tb_date INTEGER NOT NULL,
  ordinal INTEGER NOT NULL, reason TEXT NOT NULL CHECK(reason IN ('large_noid','tb_error')),
  PRIMARY KEY(folder_id, tb_date, ordinal));

-- #30: one row per job run (per folder for backfill), counts only.
CREATE TABLE ingest_runs(ts INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('backfill','hourly','daily','event')),
  folder_id TEXT, account_type TEXT,
  n_msgs INTEGER NOT NULL DEFAULT 0, secs REAL NOT NULL DEFAULT 0,
  concurrency INTEGER, throttle REAL,
  missed INTEGER NOT NULL DEFAULT 0, stale_retries INTEGER NOT NULL DEFAULT 0,
  skipped_large_noid INTEGER NOT NULL DEFAULT 0, md5_ids INTEGER NOT NULL DEFAULT 0,
  x_mid_churn INTEGER NOT NULL DEFAULT 0, hmid_mismatch INTEGER NOT NULL DEFAULT 0,
  unresolved_events INTEGER NOT NULL DEFAULT 0);
CREATE INDEX ingest_runs_ts ON ingest_runs(ts);
