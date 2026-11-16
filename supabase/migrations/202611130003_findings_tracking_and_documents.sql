-- Extiende el seguimiento compartido de hallazgos sin eliminar registros previos.
alter table public.maintenance_findings
    add column if not exists requester text not null default '',
    add column if not exists reviewed_details text not null default '',
    add column if not exists resolution_details text not null default '',
    add column if not exists documents jsonb not null default '[]'::jsonb,
    add column if not exists reviewed_attachments jsonb not null default '[]'::jsonb,
    add column if not exists resolution_attachments jsonb not null default '[]'::jsonb;

alter table public.maintenance_findings
    drop constraint if exists maintenance_findings_documents_array_check,
    drop constraint if exists maintenance_findings_reviewed_attachments_array_check,
    drop constraint if exists maintenance_findings_resolution_attachments_array_check;
alter table public.maintenance_findings
    add constraint maintenance_findings_documents_array_check check (jsonb_typeof(documents) = 'array'),
    add constraint maintenance_findings_reviewed_attachments_array_check check (jsonb_typeof(reviewed_attachments) = 'array'),
    add constraint maintenance_findings_resolution_attachments_array_check check (jsonb_typeof(resolution_attachments) = 'array');

-- Asegura que el catálogo compartido exista aunque el despliegue anterior no
-- hubiese recibido su migración por separado.
create table if not exists public.maintenance_finding_categories (
    name text primary key check (length(trim(name)) between 1 and 120),
    icon text not null default '📍' check (length(icon) between 1 and 16),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
alter table public.maintenance_finding_categories enable row level security;
revoke all on public.maintenance_finding_categories from anon, authenticated;
grant all on public.maintenance_finding_categories to service_role;
insert into public.maintenance_finding_categories (name, icon) values
    ('Poda preventiva', '🌳'), ('Poste desaplomado', '🪵'), ('Estructura dañada', '🛠️'),
    ('Retensado de línea', '🪢'), ('Tirante o protección', '⚠️'), ('Cruceta dañada', '🔩'),
    ('Aislador dañado', '⚡'), ('Conductor cortado o deteriorado', '🧵'),
    ('Empalme irregular', '🔧'), ('Vegetación cercana a la red', '🌿'), ('Otro', '📍')
on conflict (name) do nothing;

-- Corrige únicamente el icono predeterminado erróneo conocido y respeta otros
-- iconos personalizados que el equipo haya configurado.
update public.maintenance_finding_categories
set icon = '🪵', updated_at = now()
where name = 'Poste desaplomado';

-- El bucket permanece privado y mantiene el límite por archivo.
update storage.buckets
set public = false,
    file_size_limit = 10485760,
    allowed_mime_types = array[
        'image/jpeg', 'image/png', 'image/webp',
        'application/pdf', 'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'text/plain', 'text/csv'
    ]
where id = 'maintenance-finding-photos';
