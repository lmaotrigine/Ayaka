-- Revises: V2
-- Creation Date: 2022-08-20 21:46:23.884664 UTC
-- Reason: app command statistics

ALTER TABLE commands ADD COLUMN IF NOT EXISTS app_command BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS commands_app_command_idx ON commands (app_command);
