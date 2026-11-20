insert into public.maintenance_finding_categories (name, icon)
values ('Poste quemado', '🔥')
on conflict (name) do nothing;
