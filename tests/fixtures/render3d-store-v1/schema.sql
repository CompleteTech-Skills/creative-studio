-- Scheduler v1 fixture schema produced by the actual historical JobStore constructor.
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job TEXT NOT NULL, at REAL NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);

CREATE TABLE frames(job TEXT NOT NULL, frame INTEGER NOT NULL,
                    spec TEXT NOT NULL, cache_key TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, token TEXT, pid INTEGER,
                    heartbeat REAL, error TEXT, PRIMARY KEY(job,frame));

CREATE TABLE jobs(id TEXT PRIMARY KEY, request TEXT NOT NULL,
                    snapshot TEXT NOT NULL, backend TEXT NOT NULL, state TEXT NOT NULL,
                    priority INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, cancel INTEGER NOT NULL DEFAULT 0);

CREATE TABLE storage_reservations(stage TEXT PRIMARY KEY, bytes INTEGER NOT NULL, pid INTEGER NOT NULL);
