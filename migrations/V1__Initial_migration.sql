-- Revises: V0
-- Creation Date: 2022-04-25 03:26:29.804348 UTC
-- Reason: Initial migration

CREATE TABLE IF NOT EXISTS guild_mod_config (
    id BIGINT PRIMARY KEY,
    raid_mode SMALLINT,
    broadcast_channel BIGINT,
    mention_count SMALLINT,
    safe_mention_channel_ids BIGINT ARRAY,
    mute_role_id BIGINT,
    muted_members BIGINT ARRAY
);

CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    name TEXT,
    content TEXT,
    owner_id BIGINT,
    uses INTEGER DEFAULT (0),
    location_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE INDEX IF NOT EXISTS tags_name_idx ON tags (name);
CREATE INDEX IF NOT EXISTS tags_location_id_idx ON tags (location_id);
CREATE INDEX IF NOT EXISTS tags_name_trgm_idx ON tags USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS tags_name_lower_idx ON tags (LOWER(name));
CREATE UNIQUE INDEX IF NOT EXISTS tags_uniq_idx ON tags (LOWER(name), location_id);

CREATE TABLE IF NOT EXISTS tag_lookup (
    id SERIAL PRIMARY KEY,
    name TEXT,
    location_id BIGINT,
    owner_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc'),
    tag_id INTEGER REFERENCES tags (id) ON DELETE CASCADE ON UPDATE NO ACTION
);

CREATE INDEX IF NOT EXISTS tag_lookup_name_idx ON tag_lookup (name);
CREATE INDEX IF NOT EXISTS tag_lookup_location_id_idx ON tag_lookup (location_id);
CREATE INDEX IF NOT EXISTS tag_lookup_name_trgm_idx ON tag_lookup USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS tag_lookup_name_lower_idx ON tag_lookup (LOWER(name));
CREATE UNIQUE INDEX IF NOT EXISTS tag_lookup_uniq_idx ON tag_lookup (LOWER(name), location_id);

CREATE TABLE IF NOT EXISTS feeds (
    id SERIAL PRIMARY KEY,
    channel_id BIGINT,
    role_id BIGINT,
    name TEXT
);

CREATE TABLE IF NOT EXISTS starboard (
    id BIGINT PRIMARY KEY,
    channel_id BIGINT,
    threshold INTEGER DEFAULT (1) NOT NULL,
    locked BOOLEAN DEFAULT FALSE,
    max_age INTERVAL DEFAULT ('7 days'::interval) NOT NULL
);

CREATE TABLE IF NOT EXISTS starboard_entries (
    id SERIAL PRIMARY KEY,
    bot_message_id BIGINT,
    message_id BIGINT UNIQUE NOT NULL,
    channel_id BIGINT,
    author_id BIGINT,
    guild_id BIGINT REFERENCES starboard (id) ON DELETE CASCADE ON UPDATE NO ACTION NOT NULL
);

CREATE INDEX IF NOT EXISTS starboard_entries_bot_message_id_idx ON starboard_entries (bot_message_id);
CREATE INDEX IF NOT EXISTS starboard_entries_message_id_idx ON starboard_entries (message_id);
CREATE INDEX IF NOT EXISTS starboard_entries_guild_id_idx ON starboard_entries (guild_id);

CREATE TABLE IF NOT EXISTS starrers (
    id SERIAL PRIMARY KEY,
    author_id BIGINT NOT NULL,
    entry_id INTEGER REFERENCES starboard_entries (id) ON DELETE CASCADE ON UPDATE NO ACTION NOT NULL
);

CREATE INDEX IF NOT EXISTS starrers_entry_id_idx ON starrers (entry_id);
CREATE UNIQUE INDEX IF NOT EXISTS starrers_uniq_idx ON starrers (author_id, entry_id);

CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    expires TIMESTAMP WITH TIME ZONE,
    created TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc'),
    event TEXT,
    extra JSONB DEFAULT ('{}'::jsonb)
);

CREATE INDEX IF NOT EXISTS reminders_expires_idx ON reminders (expires);

CREATE TABLE IF NOT EXISTS commands (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    channel_id BIGINT,
    author_id BIGINT,
    used TIMESTAMP WITH TIME ZONE,
    prefix TEXT,
    command TEXT,
    failed BOOLEAN
);

CREATE INDEX IF NOT EXISTS commands_guild_id_idx ON commands (guild_id);
CREATE INDEX IF NOT EXISTS commands_author_id_idx ON commands (author_id);
CREATE INDEX IF NOT EXISTS commands_used_idx ON commands (used);
CREATE INDEX IF NOT EXISTS commands_command_idx ON commands (command);
CREATE INDEX IF NOT EXISTS commands_failed_idx ON commands (failed);

CREATE TABLE IF NOT EXISTS emoji_stats (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT,
    emoji_id BIGINT,
    total INTEGER DEFAULT (0)
);

CREATE INDEX IF NOT EXISTS emoji_stats_guild_id_idx ON emoji_stats (guild_id);
CREATE INDEX IF NOT EXISTS emoji_stats_emoji_id_idx ON emoji_stats (emoji_id);
CREATE UNIQUE INDEX IF NOT EXISTS emoji_stats_uniq_idx ON emoji_stats (guild_id, emoji_id);

CREATE TABLE IF NOT EXISTS plonks (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    entity_id BIGINT UNIQUE
);

CREATE INDEX IF NOT EXISTS plonks_guild_id_idx ON plonks (guild_id);
CREATE INDEX IF NOT EXISTS plonks_entity_id_idx ON plonks (entity_id);

CREATE TABLE IF NOT EXISTS command_config (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    channel_id BIGINT,
    name TEXT,
    whitelist BOOLEAN
);

CREATE INDEX IF NOT EXISTS command_config_guild_id_idx ON command_config (guild_id);
CREATE UNIQUE INDEX IF NOT EXISTS command_config_uniq_idx ON command_config (channel_id, name, whitelist);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id SERIAL PRIMARY KEY,
    user_id BIGINT,
    guild_id BIGINT,
    token TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE INDEX IF NOT EXISTS auth_tokens_user_id_idx ON auth_tokens (user_id);
CREATE INDEX IF NOT EXISTS auth_tokens_guild_id_idx ON auth_tokens (guild_id);


CREATE TABLE IF NOT EXISTS twitter_handles (
    channel_id BIGINT,
    handle TEXT,
    replies BOOLEAN,
    retweets BOOLEAN,
    PRIMARY KEY (channel_id, handle)
);

CREATE TABLE IF NOT EXISTS rss_feeds (
    channel_id BIGINT,
    feed TEXT,
    last_checked TIMESTAMP WITH TIME ZONE,
    ttl INTEGER
);

CREATE TABLE IF NOT EXISTS rss_entries (
    entry TEXT,
    feed TEXT,
    PRIMARY KEY (entry, feed)
);

CREATE TABLE IF NOT EXISTS rss_errors (
    time_stamp TIMESTAMP WITH TIME ZONE PRIMARY KEY,
    feed TEXT,
    type TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS lewd_config (
    guild_id BIGINT PRIMARY KEY,
    blacklist TEXT ARRAY,
    auto_six_digits BOOLEAN
);

CREATE TABLE IF NOT EXISTS avatars (
    id SERIAL PRIMARY KEY,
    user_id BIGINT,
    attachment TEXT,
    avatar TEXT
);

CREATE INDEX IF NOT EXISTS avatars_user_id_idx ON avatars (user_id);
CREATE INDEX IF NOT EXISTS avatars_avatar_idx ON avatars (avatar);


CREATE TABLE IF NOT EXISTS snipe_deletes (
    id SERIAL PRIMARY KEY,
    user_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    parent_id BIGINT,
    message_id BIGINT,
    message_content BIGINT,
    attachment_urls TEXT ARRAY,
    delete_time BIGINT
);

CREATE INDEX IF NOT EXISTS snipe_deletes_guild_id_idx ON snipe_deletes (guild_id);
CREATE INDEX IF NOT EXISTS snipe_deletes_channel_id_idx ON snipe_deletes (channel_id);
CREATE INDEX IF NOT EXISTS snipe_deletes_parent_id_idx ON snipe_deletes (parent_id);


CREATE TABLE IF NOT EXISTS snipe_edits (
    id SERIAL PRIMARY KEY,
    user_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    parent_id BIGINT,
    message_id BIGINT,
    before_content TEXT,
    after_content TEXT,
    edited_time BIGINT,
    jump_url TEXT
);

CREATE INDEX IF NOT EXISTS snipe_edits_guild_id_idx ON snipe_edits (guild_id);
CREATE INDEX IF NOT EXISTS snipe_edits_channel_id_idx ON snipe_edits (channel_id);
CREATE INDEX IF NOT EXISTS snipe_edits_parent_id_idx ON snipe_edits (parent_id);

CREATE TABLE IF NOT EXISTS snipe_config (
    id BIGINT PRIMARY KEY,
    blacklisted_channels BIGINT ARRAY,
    blacklisted_members BIGINT ARRAY
);


CREATE TABLE IF NOT EXISTS tz_store (
    user_id BIGINT PRIMARY KEY,
    guild_ids BIGINT ARRAY,
    tz TEXT
);

CREATE TABLE IF NOT EXISTS todo (
    id SERIAL PRIMARY KEY,
    entity_id BIGINT,
    content TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

-- private extensions

/*
CREATE TABLE IF NOT EXISTS connect_four_games (
    game_id SERIAL PRIMARY KEY,
    players BIGINT ARRAY,
    winner SMALLINT,
    finished BOOLEAN
);

CREATE TABLE IF NOT EXISTS connect_four_ranking (
    user_id BIGINT PRIMARY KEY,
    ranking INTEGER DEFAULT (1000),
    games INTEGER DEFAULT (0),
    wins INTEGER DEFAULT (0),
    losses INTEGER DEFAULT (0)
);

CREATE TABLE IF NOT EXISTS message_log (
    channel_id BIGINT,
    message_id BIGINT,
    guild_id BIGINT,
    user_id BIGINT,
    content TEXT,
    nsfw BOOLEAN DEFAULT FALSE,
    deleted BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (channel_id, message_id)
);

CREATE INDEX IF NOT EXISTS message_log_guild_id_idx ON message_log (guild_id);
CREATE INDEX IF NOT EXISTS message_log_user_id_idx ON message_log (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS message_log_uniq_index ON message_log (message_id);

CREATE TABLE IF NOT EXISTS message_attachments (
    message_id BIGINT REFERENCES message_log (message_id) ON DELETE CASCADE ON UPDATE NO ACTION,
    attachment_id BIGINT,
    content TEXT,
    PRIMARY KEY (message_id)
);

CREATE TABLE IF NOT EXISTS message_edit_history (
    message_id BIGINT REFERENCES message_log (message_id) ON DELETE CASCADE ON UPDATE NO ACTION,
    created_at TIMESTAMP WITH TIME ZONE,
    content TEXT,
    PRIMARY KEY (message_id, created_at)
);


CREATE TABLE IF NOT EXISTS status_log (
    user_id BIGINT,
    timestamp TIMESTAMP WITH TIME ZONE,
    status TEXT,
    PRIMARY KEY (user_id, timestamp)
);

CREATE INDEX IF NOT EXISTS status_log_user_id_idx ON status_log (user_id);

CREATE TABLE IF NOT EXISTS channel_feeds (
    id SERIAL PRIMARY KEY,
    channel_id BIGINT,
    role_id BIGINT,
    name TEXT
);

CREATE TABLE IF NOT EXISTS cotd_config (
    guild_id BIGINT PRIMARY KEY,
    role_id BIGINT,
    required_roles BIGINT ARRAY,
    strategy SMALLINT NOT NULL DEFAULT (1)
);

CREATE INDEX IF NOT EXISTS cotd_config_strategy_idx ON cotd_config (strategy);

CREATE TABLE IF NOT EXISTS quiz_config (
    guild_id BIGINT PRIMARY KEY,
    qchannel_id BIGINT,
    pchannel_id BIGINT,
    qm_role_id BIGINT
);

CREATE INDEX IF NOT EXISTS quiz_config_qchannel_id_idx ON quiz_config (qchannel_id);
CREATE INDEX IF NOT EXISTS quiz_config_pchannel_id_idx ON quiz_config (pchannel_id);
CREATE INDEX IF NOT EXISTS quiz_config_qm_role_id_idx ON quiz_config (qm_role_id);

CREATE TABLE IF NOT EXISTS quizzes (
    id BIGINT PRIMARY KEY,
    guild_id BIGINT REFERENCES quiz_config (guild_id) ON DELETE CASCADE ON UPDATE NO ACTION,
    type SMALLINT
);

CREATE INDEX IF NOT EXISTS quizzes_guild_id_idx ON quizzes (guild_id);

CREATE TABLE IF NOT EXISTS ims_roles (
    id BIGINT PRIMARY KEY,
    description TEXT,
    icon TEXT,
    category TEXT NOT NULL
);

-- forms stuff 

CREATE TABLE IF NOT EXISTS ims_users (
    id BIGINT PRIMARY KEY,
    roles BIGINT ARRAY
);

*/