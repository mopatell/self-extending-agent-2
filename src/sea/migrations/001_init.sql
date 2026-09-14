-- Conversations = chat threads. Runs = one task executed inside a conversation.
CREATE TABLE conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- User-visible chat history (what a chat UI would render).
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,            -- user | assistant
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    task            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending | planning | awaiting_plan_approval | building_tools | running
    -- | awaiting_approval | awaiting_input | completed | failed | cancelled
    plan_json       TEXT,                     -- approved Plan
    state_json      TEXT,                     -- checkpoint: everything needed to resume
    answer          TEXT,
    error           TEXT,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Append-only log of everything that happened in a run. The CLI, API and evals all read this.
CREATE TABLE events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (run_id, seq)
);

-- A human decision the run is waiting on (plan approval, risky tool call, clarification).
CREATE TABLE approvals (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id),
    kind          TEXT NOT NULL,              -- plan | tool_call | question
    payload_json  TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied | answered
    decision_json TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at   TEXT
);

-- Agent-written tools. One row per version; the highest active version is what gets used.
CREATE TABLE tools (
    name              TEXT NOT NULL,
    version           INTEGER NOT NULL,
    description       TEXT NOT NULL,
    input_schema_json TEXT NOT NULL,
    source            TEXT NOT NULL,
    test_code         TEXT NOT NULL,
    deps_json         TEXT NOT NULL DEFAULT '[]',
    risk              TEXT NOT NULL DEFAULT 'safe',   -- safe | approval
    status            TEXT NOT NULL DEFAULT 'active', -- active | deprecated
    created_by_run    TEXT,
    calls             INTEGER NOT NULL DEFAULT 0,
    failures          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (name, version)
);

CREATE INDEX idx_events_run ON events(run_id, seq);
CREATE INDEX idx_runs_conversation ON runs(conversation_id);
CREATE INDEX idx_approvals_run ON approvals(run_id, status);
