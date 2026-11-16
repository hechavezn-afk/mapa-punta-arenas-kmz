-- Adds shared image icons for maintenance finding categories.
-- Existing emoji icons and finding records remain unchanged.
alter table public.maintenance_finding_categories
    add column if not exists icon_path text;

-- The table already uses RLS and grants writes only to service_role.
-- Icon files use the existing private maintenance-finding-photos bucket.
