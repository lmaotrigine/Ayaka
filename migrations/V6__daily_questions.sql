-- Revises: V5
-- Creation Date: 2022-09-01 08:02:04.377865 UTC
-- Reason: daily questions

CREATE TABLE IF NOT EXISTS dq_answers (
    id INTEGER NOT NULL,
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    answer TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    PRIMARY KEY (id, user_id, guild_id)
);
