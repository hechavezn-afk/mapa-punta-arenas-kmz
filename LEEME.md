# Visor KMZ de Punta Arenas

Visor web simple, adaptable a celulares y tablets. Muestra la red eléctrica y las dos áreas de concesión con casillas para activar o quitar cada capa, al estilo Earth. El botón inferior derecho alterna entre calles y vista satelital. La red se dibuja con Canvas para manejar el volumen de líneas y postes.

El buscador encuentra activos en los atributos del KMZ. Toca un resultado para centrar el mapa y ver sus datos. El panel se puede cerrar con **×** o tocando el mapa y se vuelve a abrir con **⌕ Capas**. También acepta coordenadas decimales (`-53.03, -70.85`) y grados, minutos y segundos. Si no encuentra un activo, busca lugares en OpenStreetMap y envía el texto ingresado a ese servicio.

## Inicio en el computador

Instala Python y las dependencias con `python -m pip install -r requirements.txt`. Luego ejecuta `INICIAR_VISOR.bat` o `python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8511`.

## Abrir desde el celular/tablet en la misma red Wi-Fi

Conecta ambos dispositivos a la misma red Wi-Fi, ejecuta el inicio en el computador y abre en el móvil `http://IP-DEL-COMPUTADOR:8511`. Reemplaza `IP-DEL-COMPUTADOR` con la dirección local del equipo donde corre la app. La red local debe permitir conexiones entre dispositivos.

El mapa base y la vista satelital se sirven por internet. El botón **Mi ubicación** usa el GPS del navegador y necesita permiso de ubicación. Los navegadores suelen permitir GPS solo en sitios HTTPS o `localhost`; por eso, en un celular que abre la dirección local del computador por HTTP, se necesitará configurar HTTPS para que el GPS funcione.

## Símbolos de activos

Los símbolos vectoriales del mapa se inspiran en referencias abiertas: [poste eléctrico de Lucide](https://iconbuddy.com/lucide/utility-pole), [fusibles IEC/IEEE](https://commons.wikimedia.org/wiki/File:Fuse-basic-symbols.svg), [seccionador IEC](https://commons.wikimedia.org/wiki/File:Rozlacznik.svg) y [subestación CC0 de SVG Repo](https://www.svgrepo.com/svg/10798918/electrical-substation). Se redibujan dentro del Canvas del mapa para conservar el rendimiento; las letras identifican cada familia de activos. El KMZ clasifica los equipos como Fusible (F), Desconectador Bajo Carga (D) y Reconectador (R). Las subestaciones se distinguen con SE.
