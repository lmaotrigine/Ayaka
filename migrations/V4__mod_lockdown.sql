-- Revises: V3
-- Creation Date: 2022-08-20 23:13:04.392855 UTC
-- Reason: mod lockdown and ignoring multiple entitiy types

ALTER TABLE guild_mod_config RENAME COLUMN safe_automod_channel_ids TO safe_automod_entity_ids;
ALTER TABLE guild_mod_config ADD COLUMN locked_channels BIGINT ARRAY;
