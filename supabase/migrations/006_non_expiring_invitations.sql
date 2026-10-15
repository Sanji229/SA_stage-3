-- Migration 006: new teacher invitation codes stay valid until used or revoked.
-- Existing invitation codes also stop expiring; the application ignores expires_at.
alter table teacher_invitations alter column expires_at drop not null;
