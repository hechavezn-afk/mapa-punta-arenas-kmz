create table if not exists public.private_pole_annotations (
    pole_key text primary key,
    annotation jsonb not null
);

alter table public.private_pole_annotations enable row level security;
revoke all on public.private_pole_annotations from public, anon, authenticated;
grant select, insert, update on public.private_pole_annotations to service_role;
