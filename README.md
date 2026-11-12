# Redes Edelmag Magallanes

Visor web responsivo para revisar activos eléctricos y áreas de concesión de Magallanes. Permite activar y desactivar capas, buscar postes, equipos, subestaciones y direcciones de las principales localidades, consultar atributos, ver redes BT/MT, saltar a coordenadas y usar el GPS del dispositivo.

Las capas eléctricas se organizan por localidad: Punta Arenas, Puerto Natales, Porvenir y Puerto Williams. Cada grupo contiene solamente sus postes, equipos, transformadores y redes disponibles.

## Abrir la aplicación

[Ver el mapa en Streamlit Community Cloud](https://mapa-punta-arenas.streamlit.app/)

En teléfono, el panel ocupa menos espacio. Ciérralo con **×** o tocando el mapa y reábrelo desde **⌕ Capas**. Toca un resultado de búsqueda para ir al elemento; al centrar el mapa se oculta el panel.

## Ejecutar localmente

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

## Supabase: accesos y hallazgos compartidos

En Streamlit Cloud configura los secretos del servidor (nunca los subas al repositorio):

```toml
[supabase]
url = "https://TU-PROJECT-REF.supabase.co"
key = "sb_publishable_..."

[security]
supabase_secret_key = "sb_secret_..."
```

La clave `sb_secret_...` solo la usa Python en el servidor. Tras desplegar, entra con la cuenta de configuración y abre **Configuración general → Administrar cuentas** para crear las cuentas que falten y descargar el PDF con sus claves iniciales. Para entrar a esa pantalla también se puede usar `?admin=accounts`. Los usuarios nuevos deben completar segundo factor con Authenticator o correo; los códigos por correo dependen de habilitar Email y ajustar la plantilla de Supabase para mostrar `{{ .Token }}`.

Los usuarios ingresan con su nombre y apellido juntos, en mayúsculas y sin espacios (por ejemplo, `LUISGARRIDO`), su contraseña de Supabase y luego el segundo factor. También pueden ingresar con su correo. En cada ingreso pueden elegir **Recordar sesión** para restaurarla tras recargar o reabrir el navegador; toda sesión vence tras cinco horas sin actividad.

Para habilitar el almacenamiento compartido de hallazgos, ejecuta una vez el SQL de `supabase/migrations/202611120001_shared_maintenance_findings.sql` en el SQL Editor del proyecto. La migración crea tablas privadas y un bucket privado; no revoca ni modifica permisos globales de `storage.objects`.

Los archivos KMZ incluidos son los datos usados por el visor:

- `Punta Arenas.kmz`: postes, equipos, subestaciones y redes.
- `concesiones2025v3 corregidas menor a 15.kmz`: concesión menor a 15.
- `concesiones2025v3 GEO superior a 14.kmz`: concesión GEO superior a 14.

Los símbolos vectoriales se inspiran en [Lucide Utility Pole](https://iconbuddy.com/lucide/utility-pole), [Fuse basic symbols](https://commons.wikimedia.org/wiki/File:Fuse-basic-symbols.svg), [IEC disconnect switch](https://commons.wikimedia.org/wiki/File:Rozlacznik.svg) y [SVG Repo electrical substation](https://www.svgrepo.com/svg/10798918/electrical-substation).
