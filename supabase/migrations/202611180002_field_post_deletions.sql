create or replace function public.sync_field_post(actor text, post_id text, expected_revision bigint, post_data jsonb, remove_post boolean default false)
returns jsonb language plpgsql security definer set search_path = public as $$
declare current_post public.created_field_posts; result public.created_field_posts;
begin
 perform pg_advisory_xact_lock(hashtextextended(post_id,0));
 select * into current_post from public.created_field_posts where id=post_id for update;
 if found then
  if current_post.owner_name <> actor and not (current_post.visibility='global' or (current_post.visibility='user' and current_post.shared_with=actor)) then
   raise exception 'Sin permiso para este poste';
  end if;
  if current_post.deleted then return to_jsonb(current_post); end if;
  if remove_post then
   if current_post.owner_name <> actor then raise exception 'Solo el propietario puede eliminar'; end if;
   update public.created_field_posts set deleted=true, data='{}'::jsonb, revision=revision+1, updated_at=now()
    where id=post_id returning * into result;
   return to_jsonb(result);
  end if;
  if current_post.revision <> expected_revision then
   raise exception 'Conflicto: el poste cambio en otro equipo. Actualiza antes de guardar.';
  end if;
  if current_post.owner_name <> actor and (remove_post or coalesce(post_data->>'visibility','private')<>current_post.visibility or coalesce(post_data->>'sharedWith','')<>coalesce(current_post.shared_with,'')) then
   raise exception 'Solo el propietario puede eliminar o cambiar el acceso';
  end if;
  update public.created_field_posts set data=post_data, visibility=coalesce(post_data->>'visibility','private'),
   shared_with=nullif(post_data->>'sharedWith',''), revision=revision+1, deleted=remove_post, updated_at=now()
   where id=post_id returning * into result;
 else
  if remove_post then
   insert into public.created_field_posts(id,owner_name,data,deleted) values(post_id,actor,'{}'::jsonb,true) returning * into result;
   return to_jsonb(result);
  end if;
  if expected_revision<>0 then raise exception 'Poste inexistente'; end if;
  insert into public.created_field_posts(id,owner_name,visibility,shared_with,data)
   values(post_id,actor,coalesce(post_data->>'visibility','private'),nullif(post_data->>'sharedWith',''),post_data) returning * into result;
 end if;
 return to_jsonb(result);
end $$;
revoke all on function public.sync_field_post(text,text,bigint,jsonb,boolean) from public, anon, authenticated;
grant execute on function public.sync_field_post(text,text,bigint,jsonb,boolean) to service_role;
