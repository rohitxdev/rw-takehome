-- Normalized schema for the CBT Lab program data (SQLite).
-- Covers: applications.csv, attendance.csv, payments.csv, weekly_reflections.csv.
--
-- SQLite notes:
--   * Foreign keys are off by default: run `PRAGMA foreign_keys = ON;` per connection.
--   * Enums are TEXT columns with CHECK (... IN (...)).
--   * Timestamps are ISO 8601 TEXT in UTC ('2026-05-14T16:41:00Z').
--   * Booleans are INTEGER 0/1.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- People
-- ---------------------------------------------------------------------------

CREATE TABLE person (
    person_id       INTEGER PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE CHECK (email = lower(trim(email))),  -- one email per person
    full_name       TEXT NOT NULL,
    preferred_name  TEXT,
    country         TEXT,
    timezone        TEXT
);

-- A facilitator is a person holding the facilitator role (often a past participant).
CREATE TABLE facilitator (
    person_id   INTEGER PRIMARY KEY REFERENCES person (person_id),
    trained_at  TEXT,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- ---------------------------------------------------------------------------
-- Program structure
-- ---------------------------------------------------------------------------

CREATE TABLE cohort (
    cohort_id  INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE          -- '2026 Summer'
);

-- Assumption: each group (and so each session) has exactly one facilitator.
CREATE TABLE "group" (
    group_id        INTEGER PRIMARY KEY,
    group_number    INTEGER NOT NULL UNIQUE, -- 81, 82, ... (cleaned from 'Group 81' / '81')
    cohort_id       INTEGER NOT NULL REFERENCES cohort (cohort_id),
    facilitator_id  INTEGER NOT NULL REFERENCES facilitator (person_id),
    UNIQUE (group_id, cohort_id)             -- target for membership's cohort check
);

-- One session per week for weeks 1-8, then a final session in week 12.
-- A row exists only for a session that happened; no row = session didn't happen.
CREATE TABLE session (
    session_id   INTEGER PRIMARY KEY,
    group_id     INTEGER NOT NULL REFERENCES "group" (group_id),
    week_number  INTEGER NOT NULL CHECK (week_number BETWEEN 1 AND 12),
    held_on      TEXT,
    UNIQUE (group_id, week_number),
    UNIQUE (session_id, group_id)            -- target for attendance's group check
);

-- ---------------------------------------------------------------------------
-- Applications and participation
-- ---------------------------------------------------------------------------

CREATE TABLE application (
    application_id   TEXT PRIMARY KEY,       -- 'RW26-018'; required
    person_id        INTEGER NOT NULL REFERENCES person (person_id),
    cohort_id        INTEGER NOT NULL REFERENCES cohort (cohort_id),
    applied_at       TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN (
                         'for_matching', 'waiting_list', 'declined', 'in_facilitation', 'completed')),
    sharing_consent  INTEGER NOT NULL CHECK (sharing_consent IN (0, 1)),
    UNIQUE (person_id, cohort_id),           -- one application per person per cohort
    UNIQUE (application_id, cohort_id)       -- target for membership's cohort check
);

-- One stint in one group. A move (e.g. G82 -> G83 from week 3) closes the
-- current row (end_week = 2, end_reason = 'moved') and opens a new one
-- (start_week = 3), in one transaction.
CREATE TABLE group_membership (
    membership_id   INTEGER PRIMARY KEY,
    application_id  TEXT NOT NULL,
    cohort_id       INTEGER NOT NULL,
    group_id        INTEGER NOT NULL,
    start_week      INTEGER NOT NULL DEFAULT 1,
    end_week        INTEGER,                 -- NULL = still a member
    end_reason      TEXT CHECK (end_reason IN ('moved', 'dropped')),
    CHECK (end_week IS NULL OR end_week >= start_week),
    CHECK ((end_reason IS NULL) = (end_week IS NULL)),
    -- The group must belong to the application's cohort.
    FOREIGN KEY (application_id, cohort_id) REFERENCES application (application_id, cohort_id),
    FOREIGN KEY (group_id, cohort_id) REFERENCES "group" (group_id, cohort_id),
    UNIQUE (membership_id, group_id)         -- target for attendance's group check
);

-- One group at a time: an application's week ranges may not overlap.
-- (SQLite has no exclusion constraints, so this is a trigger.)
CREATE TRIGGER membership_no_overlap_insert
BEFORE INSERT ON group_membership
WHEN EXISTS (
    SELECT 1 FROM group_membership gm
    WHERE gm.application_id = NEW.application_id
      AND NEW.start_week <= COALESCE(gm.end_week, 12)
      AND gm.start_week <= COALESCE(NEW.end_week, 12)
)
BEGIN
    SELECT RAISE(ABORT, 'membership overlaps an existing membership for this application');
END;

CREATE TRIGGER membership_no_overlap_update
BEFORE UPDATE OF start_week, end_week, application_id ON group_membership
WHEN EXISTS (
    SELECT 1 FROM group_membership gm
    WHERE gm.application_id = NEW.application_id
      AND gm.membership_id <> NEW.membership_id
      AND NEW.start_week <= COALESCE(gm.end_week, 12)
      AND gm.start_week <= COALESCE(NEW.end_week, 12)
)
BEGIN
    SELECT RAISE(ABORT, 'membership overlaps an existing membership for this application');
END;

-- Long format: one row per member per session they were a member for.
-- Totals and rates are derived.
CREATE TABLE attendance (
    membership_id  INTEGER NOT NULL,
    session_id     INTEGER NOT NULL,
    group_id       INTEGER NOT NULL,
    presence       REAL NOT NULL CHECK (presence IN (0, 0.5, 1)),  -- 0 absent, 0.5 half, 1 full
    PRIMARY KEY (membership_id, session_id),
    -- The session must be in the member's own group.
    FOREIGN KEY (membership_id, group_id) REFERENCES group_membership (membership_id, group_id),
    FOREIGN KEY (session_id, group_id) REFERENCES session (session_id, group_id)
);

-- The session's week must fall inside the membership's week range.
CREATE TRIGGER attendance_within_membership_insert
BEFORE INSERT ON attendance
WHEN NOT EXISTS (
    SELECT 1 FROM group_membership gm, session s
    WHERE gm.membership_id = NEW.membership_id
      AND s.session_id = NEW.session_id
      AND s.week_number >= gm.start_week
      AND (gm.end_week IS NULL OR s.week_number <= gm.end_week)
)
BEGIN
    SELECT RAISE(ABORT, 'session is outside the weeks of this membership');
END;

CREATE TRIGGER attendance_within_membership_update
BEFORE UPDATE OF membership_id, session_id ON attendance
WHEN NOT EXISTS (
    SELECT 1 FROM group_membership gm, session s
    WHERE gm.membership_id = NEW.membership_id
      AND s.session_id = NEW.session_id
      AND s.week_number >= gm.start_week
      AND (gm.end_week IS NULL OR s.week_number <= gm.end_week)
)
BEGIN
    SELECT RAISE(ABORT, 'session is outside the weeks of this membership');
END;

-- Attendance per application, summed across group moves.
CREATE VIEW application_attendance AS
SELECT gm.application_id,
       SUM(a.presence)             AS sessions_attended,
       COUNT(*)                    AS sessions_recorded,
       SUM(a.presence) / COUNT(*)  AS attendance_rate
FROM attendance a
JOIN group_membership gm USING (membership_id)
GROUP BY gm.application_id;

-- One submission per application per program week (1-8, 12).
CREATE TABLE weekly_reflection (
    reflection_id   INTEGER PRIMARY KEY,
    application_id  TEXT NOT NULL REFERENCES application (application_id),
    week_number     INTEGER NOT NULL CHECK (week_number BETWEEN 1 AND 12),
    submitted_at    TEXT NOT NULL,
    -- Session ratings (engaging, facilitator, felt_supported) are on an assumed 1-10 scale.
    -- PHQ-4 items: 0 not at all, 1 several days, 2 more than half the days, 3 nearly every day
    phq_nervous     INTEGER CHECK (phq_nervous BETWEEN 0 AND 3),
    phq_worrying    INTEGER CHECK (phq_worrying BETWEEN 0 AND 3),
    phq_interest    INTEGER CHECK (phq_interest BETWEEN 0 AND 3),
    phq_down        INTEGER CHECK (phq_down BETWEEN 0 AND 3),
    difficulty      TEXT,
    attended        INTEGER CHECK (attended IN (0, 1)),
    engaging        INTEGER CHECK (engaging BETWEEN 1 AND 10),
    facilitator     INTEGER CHECK (facilitator BETWEEN 1 AND 10),
    felt_supported  INTEGER CHECK (felt_supported BETWEEN 1 AND 10),
    liked           TEXT,
    wished          TEXT,
    home_practice   INTEGER CHECK (home_practice BETWEEN 0 AND 100),
    UNIQUE (application_id, week_number)
);

-- ---------------------------------------------------------------------------
-- Money
-- ---------------------------------------------------------------------------

-- The deposit commitment and its outcome. No row = no deposit applies.
-- Refunded when: >= 8 of 9 sessions attended (half = 0.5), all 9 weekly
-- reflections submitted, and both wellbeing trackers (weeks 1 and 12) done.
CREATE TABLE deposit (
    application_id  TEXT PRIMARY KEY REFERENCES application (application_id),
    amount_minor    INTEGER NOT NULL CHECK (amount_minor IN (5000, 20000)),  -- USD 50 / 200
    currency        TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
    outcome         TEXT NOT NULL DEFAULT 'pending'
                        CHECK (outcome IN ('pending', 'refunded', 'forfeited', 'donated')),
    decided_at      TEXT
);

-- One row per money movement. A participant's one payment covers the deposit
-- plus any voluntary contribution ('combined'); the contribution is the part
-- above deposit.amount_minor and is never refunded. A Stripe charge with
-- Amount Refunded > 0 becomes a charge row plus a refund row, matching Wise.
CREATE TABLE payment (
    payment_id      INTEGER PRIMARY KEY,
    provider        TEXT NOT NULL CHECK (provider IN ('stripe', 'wise', 'paypal')),
    provider_ref    TEXT NOT NULL UNIQUE,                          -- 'ch_SYN...', 'WISE-...'
    application_id  TEXT REFERENCES application (application_id),  -- NULL = unmatched
    kind            TEXT NOT NULL CHECK (kind IN ('charge', 'refund')),
    refund_of       INTEGER REFERENCES payment (payment_id),
    purpose         TEXT NOT NULL CHECK (purpose IN ('combined', 'deposit', 'contribution')),
    amount_minor    INTEGER NOT NULL CHECK (amount_minor > 0),     -- gross; direction from kind
    currency        TEXT NOT NULL CHECK (length(currency) = 3),
    fee_minor       INTEGER CHECK (fee_minor >= 0),                -- deducted from amount; NULL = unknown
    status          TEXT NOT NULL CHECK (status IN ('succeeded', 'failed', 'pending')),
    created_at      TEXT,                                          -- NULL = unknown (Stripe export has no refund date)
    payer_name      TEXT,                                          -- provider snapshot (e.g. employer)
    payer_email     TEXT,
    CHECK ((kind = 'refund') = (refund_of IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- Import
-- ---------------------------------------------------------------------------

-- Problems found while loading the CSV exports (skipped rows, guesses, conflicts).
CREATE TABLE import_issue (
    issue_id        INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,           -- 'applications.csv'
    line            INTEGER,                 -- CSV file line (header = 1)
    application_id  TEXT,                    -- no FK: the row may have been skipped
    message         TEXT NOT NULL
);
