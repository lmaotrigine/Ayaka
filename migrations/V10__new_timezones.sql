-- Revises: V9
-- Creation Date: 2023-03-26 18:12:27.901805 UTC
-- Reason: new timezones

DROP TABLE IF EXISTS tz_store;

CREATE TABLE IF NOT EXISTS user_settings (
  id BIGINT PRIMARY KEY,
  timezone TEXT
);

ALTER TABLE reminders ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE todo ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';
