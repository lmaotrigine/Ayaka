-- Revises: V6
-- Creation Date: 2022-09-02 17:31:22.039730 UTC
-- Reason: todo commands

CREATE TABLE IF NOT EXISTS todo (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    channel_id BIGINT,
    message_id BIGINT,
    guild_id BIGINT,
    due_date TIMESTAMP WITH TIME ZONE,
    content TEXT,
    completed_at TIMESTAMP WITH TIME ZONE,
    cached_content TEXT,
    reminder_triggered BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS todo_user_id_idx ON todo (user_id);
CREATE INDEX IF NOT EXISTS todo_message_id_idx ON todo (message_id);
CREATE INDEX IF NOT EXISTS todo_completed_at_idx ON todo (completed_at);
CREATE INDEX IF NOT EXISTS todo_due_date_idx ON todo (due_date);
