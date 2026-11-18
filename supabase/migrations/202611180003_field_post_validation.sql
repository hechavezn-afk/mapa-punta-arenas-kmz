-- Validate new records and code changes atomically across devices.
create or replace function public.validate_created_field_post()
returns trigger language plpgsql set search_path = public as $$
declare code text;
begin
 if new.deleted or coalesce(new.data->>'kind','') = 'style' then
  return new;
 end if;
 if not coalesce(
  (new.data->>'lat')::double precision between -56.5 and -48
  and (new.data->>'lon')::double precision between -76 and -65, false
 ) then
  raise exception 'Ubicacion fuera de Magallanes';
 end if;
 code := upper(regexp_replace(coalesce(new.data->>'number',''),'[[:space:]]','','g'));
 if code <> '' then
  perform pg_advisory_xact_lock(hashtextextended('field-post-code:' || code,0));
  if (
   TG_OP = 'INSERT' or code <> upper(regexp_replace(coalesce(old.data->>'number',''),'[[:space:]]','','g'))
  ) and exists (
   select 1 from public.created_field_posts
   where not deleted and id <> new.id
    and upper(regexp_replace(coalesce(data->>'number',''),'[[:space:]]','','g')) = code
    and (owner_name = new.owner_name or visibility = 'global'
     or (visibility = 'user' and shared_with = new.owner_name))
  ) then
   raise exception 'Codigo repetido: abre el poste existente para modificarlo';
  end if;
 end if;
 return new;
end $$;

create or replace trigger validate_created_field_post
before insert or update on public.created_field_posts
for each row execute function public.validate_created_field_post();
