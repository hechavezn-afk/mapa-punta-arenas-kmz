-- Catálogo global de tipos e iconos de hallazgos.
-- No modifica ni elimina casos existentes. La app accede con service_role.
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
    ('Poda preventiva', '🌳'),
    ('Poste desaplomado', '🪵'),
    ('Estructura dañada', '🛠️'),
    ('Retensado de línea', '🪢'),
    ('Tirante o protección', '⚠️'),
    ('Cruceta dañada', '🔩'),
    ('Aislador dañado', '⚡'),
    ('Conductor cortado o deteriorado', '🧵'),
    ('Empalme irregular', '🔧'),
    ('Vegetación cercana a la red', '🌿'),
    ('Otro', '📍')
on conflict (name) do nothing;
