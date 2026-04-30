-- ============================================================================
--  001_messages.sql — chat messages + Supabase Realtime
-- ============================================================================
--
--  Apply this once on the Supabase project that hosts watchdogbot.cloud auth.
--  Two ways to run it:
--
--    a) Supabase Dashboard → SQL Editor → New Query → paste this entire file
--       → Run.
--
--    b) `supabase db push` if you have the CLI configured against the project.
--
--  Idempotent: re-running won't break anything (uses `if not exists`,
--  `or replace`, etc.).
-- ============================================================================

create extension if not exists "pgcrypto";

-- ── Messages table ──────────────────────────────────────────────────────────
create table if not exists public.messages (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  username    text,
  avatar_url  text,
  content     text not null,
  room_id     text not null default 'global',

  -- Bound message length so a malicious client can't blow out clients'
  -- memory by spamming megabyte-sized messages over realtime.
  constraint messages_content_len  check (char_length(content) between 1 and 4000),
  constraint messages_room_len     check (char_length(room_id) between 1 and 64)
);

-- Cheap "give me the latest N messages in this room" lookup
create index if not exists messages_room_created_idx
  on public.messages (room_id, created_at desc);

-- ── Row Level Security ──────────────────────────────────────────────────────
alter table public.messages enable row level security;

-- Anyone signed in can read every room (one global community feed).
-- For private DMs / private rooms, replace this with a join against a
-- `room_members` table.
drop policy if exists "messages_select_authenticated" on public.messages;
create policy "messages_select_authenticated"
  on public.messages for select
  to authenticated
  using (true);

-- Users can only insert messages where the user_id is themselves.
-- This is the key defense — without it, any signed-in user could
-- impersonate anyone else by setting a different user_id on insert.
drop policy if exists "messages_insert_own" on public.messages;
create policy "messages_insert_own"
  on public.messages for insert
  to authenticated
  with check (auth.uid() = user_id);

-- Users can delete their own messages (optional — comment out if you want
-- messages to be permanent).
drop policy if exists "messages_delete_own" on public.messages;
create policy "messages_delete_own"
  on public.messages for delete
  to authenticated
  using (auth.uid() = user_id);

-- ── Enable Supabase Realtime broadcast for this table ───────────────────────
-- Adds the table to the supabase_realtime publication so postgres_changes
-- subscriptions receive INSERT/UPDATE/DELETE events. No-op if already
-- present. (Equivalent to the toggle in Dashboard → Database → Replication.)
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename  = 'messages'
  ) then
    execute 'alter publication supabase_realtime add table public.messages';
  end if;
end $$;

-- ── Index for performance on user lookups (DM later) ────────────────────────
create index if not exists messages_user_created_idx
  on public.messages (user_id, created_at desc);
