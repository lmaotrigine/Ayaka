-- Revises: V4
-- Creation Date: 2022-08-24 11:15:06.961583 UTC
-- Reason: stalking cog

CREATE TABLE IF NOT EXISTS last_seen (
    id BIGINT PRIMARY KEY,
    "date" TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS last_spoke (
    id BIGINT,
    guild_id BIGINT,
    "date" TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id, guild_id)
);

CREATE TABLE IF NOT EXISTS namechanges (
    id BIGINT,
    name TEXT NOT NULL,
    idx BIGINT NOT NULL,
    "date" TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id, idx)
);

CREATE TABLE IF NOT EXISTS nickchanges (
    id BIGINT,
    guild_id BIGINT,
    name TEXT,
    idx BIGINT NOT NULL,
    "date" TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id, guild_id, idx)
);
