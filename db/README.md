# Database migrations

SQL files that need to run on the Supabase project that hosts auth +
realtime for `watchdogbot.cloud`.

## How to apply

Each migration is **idempotent** — re-running it is safe. To apply:

1. Open https://supabase.com/dashboard/project/_/sql
2. Paste the file contents
3. Run

Or with the Supabase CLI:

```bash
supabase db push
```

## Migrations

| File | What |
|---|---|
| `001_messages.sql` | `public.messages` table, RLS policies, realtime publication |
