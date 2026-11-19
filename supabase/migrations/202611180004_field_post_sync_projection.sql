-- Keep image payloads out of ordinary synchronization reads.
create or replace view public.created_field_posts_sync as
select id, owner_name, visibility, shared_with, revision, deleted, updated_at,
       data - 'syncPhotos' as data
from public.created_field_posts;
revoke all on public.created_field_posts_sync from public, anon, authenticated;
grant select on public.created_field_posts_sync to service_role;

create index if not exists created_field_posts_active_code_idx
on public.created_field_posts
  (upper(regexp_replace(coalesce(data->>'number',''),'[[:space:]]','','g')))
where not deleted;
