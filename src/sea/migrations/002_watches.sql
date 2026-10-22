-- Watches = recurring monitors. A watch fetches a URL on an interval, extracts a value,
-- and notifies (Telegram) when the value changes / a condition flips. Unlike runs, a watch
-- is compiled once (its extractor/predicate tools are built by the ToolSmith) and then polled
-- cheaply with no LLM calls on the hot path.
CREATE TABLE watches (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL DEFAULT '',
    request             TEXT NOT NULL,                      -- the natural-language ask
    url                 TEXT NOT NULL,
    fetcher             TEXT NOT NULL DEFAULT 'static',     -- static | rendered
    extractor_tool      TEXT,                               -- generated tool: page text -> value
    predicate_tool      TEXT,                               -- generated tool: value -> bool (optional)
    interval_seconds    INTEGER NOT NULL DEFAULT 300,
    status              TEXT NOT NULL DEFAULT 'active',     -- active | paused | error | disabled
    chat_id             TEXT,                               -- per-watch Telegram chat (else global)
    last_checked        TEXT,
    last_value          TEXT,                               -- most recent extracted value
    last_hash           TEXT,                               -- hash of the extracted value
    last_notified_value TEXT,                               -- value at the last notification (edge-trigger)
    etag                TEXT,                               -- for conditional requests
    last_error          TEXT,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Append-only history of what a watch saw, mirroring the runs/events pattern.
CREATE TABLE watch_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id    TEXT NOT NULL REFERENCES watches(id),
    seq         INTEGER NOT NULL,
    type        TEXT NOT NULL,                              -- checked | changed | matched | notified | error
    value       TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (watch_id, seq)
);

CREATE INDEX idx_watch_events ON watch_events(watch_id, seq);
CREATE INDEX idx_watches_status ON watches(status);
