-- Esquema inicial para hallazgos compartidos de mantenimiento.
-- Las tablas quedan cerradas al acceso público; la app debe operar mediante
-- el servidor Streamlit usando SUPABASE_SERVICE_ROLE_KEY en sus secretos.

create extension if not exists pgcrypto;

create table if not exists public.maintenance_findings (
    id uuid primary key default gen_random_uuid(),
    locality text not null check (locality in ('Punta Arenas', 'Puerto Natales', 'Porvenir', 'Puerto Williams')),
    notice_number text not null default '',
    category text not null,
    description text not null default '',
    priority text not null default 'Media' check (priority in ('Baja', 'Media', 'Alta', 'Urgente')),
    status text not null default 'Pendiente' check (status in ('Pendiente', 'En revisión', 'Resuelto', 'Cerrado')),
    asset_code text,
    second_asset_code text,
    latitude double precision not null check (latitude between -90 and 90),
    longitude double precision not null check (longitude between -180 and 180),
    second_latitude double precision check (second_latitude is null or second_latitude between -90 and 90),
    second_longitude double precision check (second_longitude is null or second_longitude between -180 and 180),
    photos jsonb not null default '[]'::jsonb check (jsonb_typeof(photos) = 'array'),
    created_by text not null,
    updated_by text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    resolved_at timestamptz,
    constraint maintenance_findings_second_point_pair check (
        (second_latitude is null and second_longitude is null)
        or (second_latitude is not null and second_longitude is not null)
    )
);

create index if not exists maintenance_findings_locality_status_idx
    on public.maintenance_findings (locality, status, priority);
create index if not exists maintenance_findings_updated_at_idx
    on public.maintenance_findings (updated_at desc);

create table if not exists public.maintenance_finding_history (
    id bigint generated always as identity primary key,
    finding_id uuid not null references public.maintenance_findings(id) on delete cascade,
    action text not null check (action in ('Creado', 'Actualizado')),
    changed_by text not null,
    changed_at timestamptz not null default now(),
    snapshot jsonb not null
);

create index if not exists maintenance_finding_history_finding_idx
    on public.maintenance_finding_history (finding_id, changed_at desc);

create or replace function public.log_maintenance_finding_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    if tg_op = 'INSERT' then
        insert into public.maintenance_finding_history (finding_id, action, changed_by, snapshot)
        values (new.id, 'Creado', new.created_by, to_jsonb(new));
        return new;
    end if;

    new.updated_at := now();
    insert into public.maintenance_finding_history (finding_id, action, changed_by, snapshot)
    values (new.id, 'Actualizado', new.updated_by, to_jsonb(new));
    return new;
end;
$$;

drop trigger if exists maintenance_findings_audit on public.maintenance_findings;
create trigger maintenance_findings_audit
    before insert or update on public.maintenance_findings
    for each row execute function public.log_maintenance_finding_change();

alter table public.maintenance_findings enable row level security;
alter table public.maintenance_finding_history enable row level security;
revoke all on public.maintenance_findings from anon, authenticated;
revoke all on public.maintenance_finding_history from anon, authenticated;
grant all on public.maintenance_findings to service_role;
grant all on public.maintenance_finding_history to service_role;
grant usage, select on sequence public.maintenance_finding_history_id_seq to service_role;
revoke all on function public.log_maintenance_finding_change() from public, anon, authenticated;

-- Fotos se guardan en Storage y en la tabla solo se conservan sus metadatos/rutas.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'maintenance-finding-photos',
    'maintenance-finding-photos',
    false,
    10485760,
    array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

