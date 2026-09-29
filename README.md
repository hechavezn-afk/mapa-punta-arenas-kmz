# Mapa de Punta Arenas

Visor web responsivo para revisar activos eléctricos y áreas de concesión de Punta Arenas. Permite activar y desactivar capas, buscar postes, equipos y subestaciones, consultar atributos, dibujar redes BT/MT, saltar a coordenadas y usar la ubicación GPS del dispositivo.

## Abrir la aplicación

La aplicación se publica en Streamlit Community Cloud después de conectar este repositorio.

## Ejecutar localmente

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

Los archivos KMZ incluidos son los datos usados por el visor:

- `Punta Arenas.kmz`: postes, equipos, subestaciones y redes.
- `concesiones2025v3 corregidas menor a 15.kmz`: concesión menor a 15.
- `concesiones2025v3 GEO superior a 14.kmz`: concesión GEO superior a 14.

Los símbolos vectoriales se inspiran en [Lucide Utility Pole](https://iconbuddy.com/lucide/utility-pole), [Fuse basic symbols](https://commons.wikimedia.org/wiki/File:Fuse-basic-symbols.svg), [IEC disconnect switch](https://commons.wikimedia.org/wiki/File:Rozlacznik.svg) y [SVG Repo electrical substation](https://www.svgrepo.com/svg/10798918/electrical-substation).
