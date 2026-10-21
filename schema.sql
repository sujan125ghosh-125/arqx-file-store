-- Run once in Supabase SQL Editor. Keep these tables private; do not grant anon/authenticated access.
create table if not exists public.files (
  code text primary key, name text not null, file_type text not null,
  file_size bigint not null default 0, storage_chat_id bigint not null,
  storage_message_id bigint not null, created_at text not null
);
create table if not exists public.shares (
  share_code text primary key, file_code text not null references public.files(code) on delete cascade,
  created_at text not null, created_by bigint
);
create table if not exists public.channels (
  chat_id text primary key, title text not null, invite_link text not null
);
create table if not exists public.admins (
  user_id bigint primary key, added_at text not null, added_by bigint
);
create table if not exists public.command_menu_users (user_id bigint primary key);
create table if not exists public.settings (key text primary key, value text not null);
create table if not exists public.pending_uploads (user_id bigint primary key, chat_id bigint not null);
insert into public.settings(key,value) values ('forcejoin','off') on conflict do nothing;

-- Defense in depth for all tables. The bot connects with the private DATABASE_URL
-- and does not use the public Data API. No policies are created, so client roles have no access.
alter table public.files enable row level security;
alter table public.shares enable row level security;
alter table public.channels enable row level security;
alter table public.admins enable row level security;
alter table public.command_menu_users enable row level security;
alter table public.settings enable row level security;
alter table public.pending_uploads enable row level security;
