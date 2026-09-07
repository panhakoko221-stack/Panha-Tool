-- Run this once in Supabase Dashboard > SQL Editor.
-- Clients can register/update themselves through the RPC but cannot read the table.

create table if not exists public.device_installations (
    installation_id uuid primary key,
    app_version text not null,
    os_name text not null,
    os_version text not null,
    architecture text not null,
    first_seen timestamptz not null default now(),
    last_seen timestamptz not null default now()
);

alter table public.device_installations enable row level security;
revoke all on table public.device_installations from anon, authenticated;

create or replace function public.register_device(
    p_installation_id uuid,
    p_app_version text,
    p_os_name text,
    p_os_version text,
    p_architecture text
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.device_installations (
        installation_id, app_version, os_name, os_version, architecture
    ) values (
        p_installation_id,
        left(p_app_version, 32),
        left(p_os_name, 64),
        left(p_os_version, 128),
        left(p_architecture, 64)
    )
    on conflict (installation_id) do update set
        app_version = excluded.app_version,
        os_name = excluded.os_name,
        os_version = excluded.os_version,
        architecture = excluded.architecture,
        last_seen = now();
end;
$$;

revoke all on function public.register_device(uuid, text, text, text, text) from public;
grant execute on function public.register_device(uuid, text, text, text, text) to anon;
