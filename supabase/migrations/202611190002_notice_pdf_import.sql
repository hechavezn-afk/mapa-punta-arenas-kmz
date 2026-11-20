-- Preserve structured metadata extracted from source customer notices and
-- support the priority variants printed on notice PDFs.
alter table public.maintenance_findings
    add column if not exists source_data jsonb not null default '{}'::jsonb;

alter table public.maintenance_findings
    drop constraint if exists maintenance_findings_priority_check;

alter table public.maintenance_findings
    add constraint maintenance_findings_priority_check
    check (priority in ('Baja', 'Media baja', 'Media', 'Media alta', 'Alta', 'Urgente'));
