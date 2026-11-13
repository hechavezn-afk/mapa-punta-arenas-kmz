begin;
-- Corrige el registro del historial de hallazgos.
-- El trigger de auditoría debe ejecutarse después del INSERT para que el hallazgo
-- padre ya exista al insertar su fila relacionada en maintenance_finding_history.
-- La fecha de actualización se mantiene en un trigger BEFORE UPDATE separado.

create or replace function public.stamp_maintenance_finding_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

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

    insert into public.maintenance_finding_history (finding_id, action, changed_by, snapshot)
    values (new.id, 'Actualizado', new.updated_by, to_jsonb(new));
    return new;
end;
$$;

drop trigger if exists maintenance_findings_audit on public.maintenance_findings;
drop trigger if exists maintenance_findings_stamp_updated_at on public.maintenance_findings;

create trigger maintenance_findings_stamp_updated_at
    before update on public.maintenance_findings
    for each row execute function public.stamp_maintenance_finding_update();

create trigger maintenance_findings_audit
    after insert or update on public.maintenance_findings
    for each row execute function public.log_maintenance_finding_change();

revoke all on function public.stamp_maintenance_finding_update() from public, anon, authenticated;
revoke all on function public.log_maintenance_finding_change() from public, anon, authenticated;
grant execute on function public.stamp_maintenance_finding_update() to service_role;
grant execute on function public.log_maintenance_finding_change() to service_role;



commit;
