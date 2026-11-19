"""Server-side shared maintenance findings for the Redes Edelmag map."""
from __future__ import annotations

from datetime import datetime, timezone
import base64
import csv
from html import escape
import json
import math
import re
import time
import zipfile
import mimetypes
from io import BytesIO, StringIO
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import streamlit as st

from finding_catalog import DEFAULT_FINDING_CATEGORIES
from supabase_accounts import auth_config, SupabaseSetupError

LOCALITIES = ("Punta Arenas", "Puerto Natales", "Porvenir", "Puerto Williams")
LOCALITY_CENTERS = {"Punta Arenas": (-53.15, -70.92), "Puerto Natales": (-51.73, -72.51), "Porvenir": (-53.30, -70.37), "Puerto Williams": (-54.93, -67.61)}
STATUSES = ("Pendiente", "En revisión", "Revisado", "Resuelto", "Cerrado")
PRIORITIES = ("Baja", "Media", "Alta", "Urgente")
CATEGORIES = DEFAULT_FINDING_CATEGORIES
DEFAULT_CATEGORY_ICONS = {
    "Poda preventiva": "🌳", "Poste desaplomado": "🪵", "Estructura dañada": "🛠️",
    "Retensado de línea": "🪢", "Tirante o protección": "⚠️", "Cruceta dañada": "🔩",
    "Aislador dañado": "⚡", "Conductor cortado o deteriorado": "🧵",
    "Empalme irregular": "🔧", "Vegetación cercana a la red": "🌿", "Otro": "📍",
}
DOCUMENT_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".csv": "text/csv",
}
DOCUMENT_EXTENSIONS = tuple(extension.lstrip(".") for extension in DOCUMENT_MIME_BY_EXTENSION)
DOCUMENT_MIME_TYPES = set(DOCUMENT_MIME_BY_EXTENSION.values())
FINDING_BUCKET_MIME_TYPES = sorted({"image/jpeg", "image/png", "image/webp", *DOCUMENT_MIME_TYPES})
FINDING_BUCKET_FILE_SIZE_LIMIT = 10 * 1024 * 1024
BUCKET = "maintenance-finding-photos"
DATA_DIR = Path(__file__).parent
ASSET_KMZ = {
    "Punta Arenas": ("Punta Arenas.kmz", "Punta Arenas_v2.kmz"),
    "Puerto Natales": ("Puerto Natales.kmz",),
    "Porvenir": ("Porvenir.kmz",),
    "Puerto Williams": ("Puerto Williams.kmz",),
}


class FindingsError(RuntimeError):
    pass


def _request(method: str, path: str, payload=None, *, content_type="application/json", prefer=None,
             extra_headers: dict[str, str] | None = None):
    try:
        url, _, key = auth_config()
    except SupabaseSetupError as exc:
        raise FindingsError(str(exc)) from None
    if isinstance(payload, bytes):
        body = payload
    elif payload is None:
        body = None
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = content_type
    if prefer:
        headers["Prefer"] = prefer
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except HTTPError as exc:
        # Keep the useful PostgREST/Storage diagnostic. The previous generic 409
        # message incorrectly suggested that every conflict meant a missing migration.
        try:
            error_body = json.loads(exc.read().decode("utf-8", errors="replace"))
        except (ValueError, OSError):
            error_body = {}
        error_code = str(error_body.get("code") or "")
        error_message = str(error_body.get("message") or "").strip()
        error_detail = str(error_body.get("details") or "").strip()
        diagnostic = " · ".join(part for part in (error_message, error_detail) if part)
        if exc.code == 404:
            raise FindingsError("No encuentro las tablas o el bucket. Ejecuta la migración SQL de Supabase una vez.") from None
        if exc.code in (401, 403):
            raise FindingsError("Supabase rechazó el acceso del servidor. Revisa la clave privada configurada.") from None
        if exc.code == 409:
            if error_code == "23505":
                raise FindingsError(f"Supabase detectó un registro duplicado ({diagnostic or 'clave única'}). No se guardó el hallazgo; vuelve a intentarlo. Si persiste, comparte este detalle con soporte.") from None
            raise FindingsError(f"Supabase respondió HTTP 409 ({diagnostic or 'conflicto de datos'}). No se guardó el hallazgo. Revisa el detalle indicado; una migración pendiente solo es una de las posibles causas.") from None
        if diagnostic:
            raise FindingsError(f"Supabase respondió HTTP {exc.code}: {diagnostic}") from None
        raise FindingsError(f"Supabase respondió con error HTTP {exc.code}. Revisa la configuración de la base de datos.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise FindingsError("No se pudo conectar con Supabase. Comprueba la conexión y vuelve a intentar.") from None


def list_findings(locality: str, status_filter: str) -> list[dict]:
    query = {"select": "*", "order": "updated_at.desc", "limit": "2000"}
    if locality != "Todas":
        query["locality"] = f"eq.{locality}"
    rows = _request("GET", "/rest/v1/maintenance_findings?" + urlencode(query)) or []
    if status_filter == "Abiertos":
        rows = [row for row in rows if row.get("status") in ("Pendiente", "En revisión", "Revisado")]
    elif status_filter != "Todos":
        rows = [row for row in rows if row.get("status") == status_filter]
    return rows


def list_shared_categories() -> list[dict]:
    query = urlencode({"select": "name,icon,icon_path", "order": "name.asc", "limit": "500"})
    try:
        rows = _request("GET", "/rest/v1/maintenance_finding_categories?" + query) or []
        st.session_state.finding_category_catalog_ready = True
    except FindingsError as exc:
        # The finding workflow remains usable if the optional icon catalog migration
        # has not been applied yet. The base shared findings table is independent.
        if "tablas o el bucket" not in str(exc):
            raise
        st.session_state.finding_category_catalog_ready = False
        return [{"name": name, "icon": DEFAULT_CATEGORY_ICONS.get(name, "📍"), "icon_path": None} for name in CATEGORIES]
    if not rows:
        # Seed existing/default categories once; keeps current installations backward-compatible.
        rows = [{"name": name, "icon": DEFAULT_CATEGORY_ICONS.get(name, "📍"), "icon_path": None} for name in CATEGORIES]
        for row in rows:
            _request("POST", "/rest/v1/maintenance_finding_categories", row, prefer="resolution=ignore-duplicates")
        rows = _request("GET", "/rest/v1/maintenance_finding_categories?" + query) or rows
    return rows


def _upload_category_icon(file, name: str) -> str:
    mime = str(file.type or "").lower()
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if mime not in allowed:
        raise FindingsError(f"Formato no permitido para el icono {file.name}; usa PNG, JPG o WEBP.")
    content = file.getvalue()
    return _store_category_icon(content, name, mime)


def _store_category_icon(content: bytes, name: str, mime: str) -> str:
    mime = str(mime or "").lower().split(";", 1)[0]
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if mime not in allowed:
        raise FindingsError("Usa un icono PNG, JPG o WEBP.")
    if not content or len(content) > 2 * 1024 * 1024:
        raise FindingsError("Cada icono debe pesar menos de 2 MB.")
    signatures = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not signatures[mime]:
        raise FindingsError("El contenido no coincide con el formato de imagen indicado.")
    # Normalize user supplied artwork into a compact circular map icon. Remove
    # only near-white pixels connected to the image edge, preserving white details
    # inside the icon itself, then trim and center the artwork on transparency.
    try:
        from PIL import Image, ImageDraw
        image = Image.open(BytesIO(content)).convert("RGBA")
        image.thumbnail((512, 512))
        pixels = image.load()
        width, height = image.size
        pending = [(x, y) for x in range(width) for y in (0, height - 1)]
        pending.extend((x, y) for y in range(1, height - 1) for x in (0, width - 1))
        seen = set()
        while pending:
            x, y = pending.pop()
            if (x, y) in seen:
                continue
            seen.add((x, y))
            r, g, b, a = pixels[x, y]
            if a == 0 or min(r, g, b) >= 238 and max(r, g, b) - min(r, g, b) <= 22:
                pixels[x, y] = (r, g, b, 0)
                if x: pending.append((x - 1, y))
                if x + 1 < width: pending.append((x + 1, y))
                if y: pending.append((x, y - 1))
                if y + 1 < height: pending.append((x, y + 1))
        bounds = image.getchannel("A").getbbox()
        if bounds:
            image = image.crop(bounds)
        image.thumbnail((44, 44), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
        canvas.alpha_composite(image, ((48 - image.width) // 2, (48 - image.height) // 2))
        mask = Image.new("L", (48, 48), 0)
        ImageDraw.Draw(mask).ellipse((1, 1, 46, 46), fill=255)
        canvas.putalpha(Image.composite(canvas.getchannel("A"), Image.new("L", (48, 48), 0), mask))
        normalized = BytesIO()
        canvas.save(normalized, format="PNG", optimize=True)
        content, mime, extension = normalized.getvalue(), "image/png", "png"
    except Exception as exc:
        raise FindingsError("No se pudo procesar el icono. Prueba con una imagen PNG, JPG o WEBP válida.") from exc
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:48] or "hallazgo"
    path = f"finding-icons/{slug}/{uuid4().hex}.{extension}"
    _request("POST", f"/storage/v1/object/{BUCKET}/{quote(path, safe='/')}", content,
             content_type=mime, extra_headers={"x-upsert": "true"})
    return path


_finding_icon_clipboard = st.components.v2.component(
    "finding_icon_clipboard",
    html='<div class="paste-icon" tabindex="0" role="button" aria-label="Pegar imagen de icono"><strong>📋 Haz clic aquí y pega el icono</strong><span>Ctrl+V o menú Pegar · PNG, JPG o WEBP · máximo 2 MB</span><div class="paste-preview"></div></div>',
    css="""
      .paste-icon { border: 1px dashed #8ba6c1; border-radius: 10px; padding: 14px; text-align: center; cursor: text; color: #294d70; }
      .paste-icon:focus { outline: 2px solid #3b82f6; }
      .paste-icon span { display:block; color:#68798a; font-size:0.85em; margin-top:4px; }
      .paste-preview img { max-width:64px; max-height:64px; object-fit:contain; margin-top:8px; }
    """,
    js="""
      export default function(component) {
        const { parentElement, setStateValue } = component;
        const target = parentElement.querySelector('.paste-icon');
        if (!target || target.dataset.bound === 'true') return;
        target.dataset.bound = 'true';
        target.addEventListener('paste', (event) => {
          const items = Array.from(event.clipboardData?.items || []);
          const item = items.find((entry) => entry.type.startsWith('image/'));
          if (!item) return;
          event.preventDefault();
          const file = item.getAsFile();
          if (!file) return;
          const reader = new FileReader();
          reader.onload = () => {
            const dataUrl = String(reader.result || '');
            setStateValue('image', dataUrl);
            setStateValue('mime', file.type || '');
            const preview = target.querySelector('.paste-preview');
            if (preview) {
              const image = document.createElement('img');
              image.alt = 'Vista previa del icono';
              image.src = dataUrl;
              preview.replaceChildren(image);
            }
          };
          reader.readAsDataURL(file);
        });
        target.addEventListener('click', () => target.focus());
      }
    """,
)


def _decode_pasted_icon(data_url: str, mime: str) -> bytes:
    if not data_url or "," not in data_url:
        raise FindingsError("Primero pega una imagen en el recuadro.")
    data_mime = data_url[5:data_url.find(";")] if data_url.startswith("data:") else ""
    if data_mime != str(mime or "").lower():
        raise FindingsError("No pude validar el formato de la imagen pegada. Vuelve a copiarla y pegarla.")
    try:
        content = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (ValueError, TypeError):
        raise FindingsError("No pude leer la imagen pegada. Vuelve a copiarla y pegarla.") from None
    if str(mime or "").lower() not in {"image/png", "image/jpeg", "image/webp"}:
        raise FindingsError("El icono pegado debe ser PNG, JPG o WEBP.")
    if not content or len(content) > 2 * 1024 * 1024:
        raise FindingsError("Cada icono debe pesar menos de 2 MB.")
    return content


def save_shared_category(name: str, icon: str, *, icon_path: str | None = None,
                         clear_icon: bool = False) -> None:
    name = (name or "").strip()
    icon = (icon or "").strip()
    if not name or len(name) > 120:
        raise FindingsError("Escribe un nombre de tipo de hallazgo (máximo 120 caracteres).")
    if not icon or len(icon) > 16:
        raise FindingsError("Elige un emoji como icono (por ejemplo 🌳).")
    payload = {"name": name, "icon": icon}
    if icon_path is not None or clear_icon:
        payload["icon_path"] = icon_path
    _request("POST", "/rest/v1/maintenance_finding_categories", payload,
             prefer="resolution=merge-duplicates,return=minimal")


def _category_icon(name: str, categories: list[dict]) -> str:
    category = next((row for row in categories if row.get("name") == name), None)
    if category and category.get("icon_path"):
        return ""
    return str(category.get("icon") or "📍") if category else DEFAULT_CATEGORY_ICONS.get(name, "📍")


def _category_icon_url(category: dict) -> str:
    path = str(category.get("icon_path") or "")
    if not path:
        return ""
    cache = st.session_state.setdefault("finding_icon_signed_urls", {})
    cached = cache.get(path)
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        url = photo_url(path, expires_in=604800)
    except FindingsError:
        return ""
    cache[path] = (time.time() + 6 * 86400, url)
    return url


@st.dialog("Configurar iconos de hallazgos", width="large")
def _finding_icon_dialog(categories: list[dict], catalog_ready: bool) -> None:
    header, close = st.columns((8, 1), vertical_alignment="center")
    header.caption("Edita los tipos existentes, añade otros y define su icono con un emoji, un archivo o una imagen pegada. Los cambios se comparten entre usuarios.")
    if close.button("×", key="close_finding_icon_config", help="Cerrar configuración de iconos"):
        st.session_state["show_finding_icon_config"] = False
        st.rerun()
    if not catalog_ready:
        st.info("La configuración compartida de tipos todavía no está disponible en Supabase.")
        return
    icon_values, icon_uploads, clear_images = {}, {}, {}
    with st.form("finding_icon_config_form"):
        for index, category in enumerate(categories):
            name = str(category["name"])
            name_col, emoji_col, file_col = st.columns((2, 1, 2), vertical_alignment="center")
            icon_uploads[name] = file_col.file_uploader(
                "Reemplazar icono", type=["png", "jpg", "jpeg", "webp"],
                key=f"finding_icon_file_{index}", label_visibility="collapsed",
                max_upload_size=2,
                help="Reemplaza el icono actual con un PNG, JPG o WEBP de hasta 2 MB.",
            )
            name_col.markdown(f"**{escape(name)}**")
            current_image = _category_icon_url(category)
            if icon_uploads.get(name) is not None:
                name_col.image(icon_uploads[name].getvalue(), width=40)
            elif current_image:
                name_col.image(current_image, width=40)
            else:
                name_col.markdown(f"### {_category_icon(name, categories)}")
            icon_values[name] = str(category.get("icon") or "📍")
            if category.get("icon_path"):
                emoji_col.caption("Imagen activa")
            else:
                icon_values[name] = emoji_col.text_input(
                    "Emoji", value=icon_values[name], max_chars=16,
                    key=f"finding_icon_edit_{index}", help=f"Icono emoji para {name}",
                )
            clear_images[name] = file_col.checkbox("Usar emoji", key=f"finding_icon_clear_{index}",
                                                   disabled=not bool(category.get("icon_path")))
        save_all = st.form_submit_button("Guardar todos los iconos", type="primary", width="stretch")
    if save_all:
        try:
            changed = 0
            for category in categories:
                name = str(category["name"])
                icon = icon_values[name].strip()
                uploaded = icon_uploads[name]
                clear_icon = bool(clear_images[name])
                changed_icon = icon != str(category.get("icon") or "📍")
                if not (uploaded or clear_icon or changed_icon):
                    continue
                image_path = _upload_category_icon(uploaded, name) if uploaded else None
                save_shared_category(name, icon, icon_path=image_path, clear_icon=clear_icon)
                changed += 1
            st.success(f"Se actualizaron {changed} icono(s) para todas las personas usuarias.")
            st.session_state.pop("finding_icon_signed_urls", None)
            st.session_state["show_finding_icon_config"] = False
            st.rerun()
        except FindingsError as exc:
            st.error(str(exc))

    with st.expander("Añadir tipo de hallazgo", expanded=False):
        with st.form("finding_category_add_form"):
            new_name = st.text_input("Nombre del nuevo tipo", max_chars=120,
                                     placeholder="Ej. Poste chocado")
            new_icon = st.text_input("Emoji del tipo", value="📍", max_chars=16)
            new_upload = st.file_uploader("Icono del nuevo tipo (opcional)",
                                          type=["png", "jpg", "jpeg", "webp"],
                                          key="finding_icon_new_file", max_upload_size=2)
            add_category = st.form_submit_button("Añadir tipo compartido", type="primary")
        if add_category:
            try:
                if not new_name.strip():
                    raise FindingsError("Escribe el nombre del tipo de hallazgo.")
                if any(str(row["name"]).casefold() == new_name.strip().casefold() for row in categories):
                    raise FindingsError("Ese tipo ya existe en el catálogo compartido.")
                image_path = _upload_category_icon(new_upload, new_name) if new_upload else None
                save_shared_category(new_name, new_icon, icon_path=image_path)
                st.session_state.pop("finding_icon_signed_urls", None)
                st.success(f"Se añadió ‘{new_name.strip()}’ al catálogo compartido.")
                st.rerun()
            except FindingsError as exc:
                st.error(str(exc))

    st.markdown("#### Pegar un icono desde el portapapeles")
    category_names = [str(row["name"]) for row in categories]
    target_options = ["＋ Crear un tipo nuevo…", *category_names]
    target = st.selectbox("Aplicar el icono a", target_options, key="finding_icon_paste_target")
    if target == target_options[0]:
        paste_name = st.text_input("Nombre del nuevo tipo", max_chars=120,
                                   key="finding_icon_paste_new_name", placeholder="Ej. Poste chocado")
        paste_emoji = st.text_input("Emoji alternativo", value="📍", max_chars=16,
                                    key="finding_icon_paste_new_emoji")
    else:
        paste_name = target
        paste_emoji = next((str(row.get("icon") or "📍") for row in categories
                            if str(row["name"]) == target), "📍")
        paste_emoji = st.text_input("Emoji alternativo", value=paste_emoji, max_chars=16,
                                    key="finding_icon_paste_emoji")
    paste_nonce = int(st.session_state.get("finding_icon_clipboard_nonce", 0))
    pasted = _finding_icon_clipboard(
        key=f"finding_icon_clipboard_instance_{paste_nonce}", default={"image": "", "mime": ""},
        on_image_change=lambda: None, on_mime_change=lambda: None,
    )
    pasted_data = str(pasted.image or "")
    if pasted_data:
        st.caption("Imagen lista para guardar.")
    if st.button("Guardar icono pegado", type="primary", key="finding_icon_paste_save"):
        try:
            if not paste_name.strip():
                raise FindingsError("Escribe el nombre del tipo antes de guardar el icono.")
            if target == target_options[0] and any(
                    str(row["name"]).casefold() == paste_name.strip().casefold() for row in categories):
                raise FindingsError("Ese tipo ya existe; selecciónalo en la lista para cambiar su icono.")
            image_bytes = _decode_pasted_icon(pasted_data, str(pasted.mime or ""))
            image_path = _store_category_icon(image_bytes, paste_name, str(pasted.mime))
            save_shared_category(paste_name, paste_emoji, icon_path=image_path)
            st.session_state.pop("finding_icon_signed_urls", None)
            st.session_state["finding_icon_clipboard_nonce"] = paste_nonce + 1
            st.success(f"Icono guardado para ‘{paste_name.strip()}’ y compartido con los usuarios.")
            st.rerun()
        except FindingsError as exc:
            st.error(str(exc))


@st.cache_data(show_spinner="Indexando activos para vincular el hallazgo…")
def _asset_index() -> dict:
    """Build a lightweight lookup from the KMZ assets without loading map geometry."""
    index: dict[str, dict[str, dict]] = {locality: {} for locality in LOCALITIES}
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    for locality, filenames in ASSET_KMZ.items():
        for filename in filenames:
            path = DATA_DIR / filename
            if not path.exists():
                continue
            try:
                with zipfile.ZipFile(path) as archive:
                    kml_name = next((name for name in archive.namelist() if name.lower().endswith(".kml")), None)
                    if not kml_name:
                        continue
                    root = ET.fromstring(archive.read(kml_name))
            except (OSError, zipfile.BadZipFile, ET.ParseError):
                continue
            parents = {child: parent for parent in root.iter() for child in parent}
            for mark in root.findall(".//k:Placemark", ns):
                point = mark.find(".//k:Point/k:coordinates", ns)
                if point is None or not point.text:
                    continue
                try:
                    coords = [float(part) for part in point.text.strip().split(",")[:2]]
                    lon, lat = coords
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        continue
                except (ValueError, TypeError):
                    continue
                attrs = {}
                for node in mark.findall(".//k:Data", ns):
                    value = node.findtext("k:value", default="", namespaces=ns)
                    if node.get("name") and value:
                        attrs[node.get("name")] = value.strip()
                for node in mark.findall(".//k:SimpleData", ns):
                    if node.get("name") and node.text:
                        attrs[node.get("name")] = node.text.strip()
                name = (mark.findtext("k:name", default="", namespaces=ns) or "").strip()
                aliases = [name]
                for key in ("IDENTIFICADOR_VNR", "POSTE_ID", "EQUIPO_ID", "ID", "CODIGO", "CÓDIGO"):
                    if attrs.get(key):
                        aliases.append(str(attrs[key]).strip())
                folder_names = []
                parent = parents.get(mark)
                while parent is not None:
                    if parent.tag.rsplit("}", 1)[-1] == "Folder":
                        folder_names.append(parent.findtext("k:name", default="", namespaces=ns).strip())
                    parent = parents.get(parent)
                kind = next((value for value in folder_names if value), "Activo")
                record = {"name": name or next((a for a in aliases if a), "Activo"),
                          "kind": kind, "latitude": lat, "longitude": lon}
                for alias in aliases:
                    normalized = _normalize_asset_code(alias)
                    if normalized:
                        index[locality].setdefault(normalized, record)
    return index


def _normalize_asset_code(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def lookup_asset(locality: str, code: str) -> dict | None:
    return _asset_index().get(locality, {}).get(_normalize_asset_code(code))


def _upload_file(file, finding_id: str, *, document: bool = False) -> dict:
    suffix = Path(file.name).suffix.lower()
    if document:
        mime = DOCUMENT_MIME_BY_EXTENSION.get(suffix)
        if not mime:
            formats = ", ".join(extension.lstrip(".").upper() for extension in DOCUMENT_MIME_BY_EXTENSION)
            raise FindingsError(f"Formato no permitido para {file.name}; usa {formats}.")
        _ensure_finding_bucket_document_types()
    else:
        mime = str(file.type or "").lower()
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise FindingsError(f"Formato no permitido para {file.name}; usa JPG, PNG o WEBP.")
    content = file.getvalue()
    if len(content) > FINDING_BUCKET_FILE_SIZE_LIMIT:
        raise FindingsError(f"{file.name} supera el límite de 10 MB.")
    extension = suffix.lstrip(".") or mimetypes.guess_extension(mime) or "bin"
    path = f"{finding_id}/{uuid4().hex}.{extension}"
    _request("POST", f"/storage/v1/object/{BUCKET}/{quote(path, safe='/')}", content,
             content_type=mime, prefer=None)
    return {"path": path, "name": file.name, "type": mime, "size": len(content), "document": document}


@st.cache_resource(show_spinner=False)
def _ensure_finding_bucket_document_types() -> None:
    """Align the private bucket's live MIME allow-list before the first document upload."""
    _request("PUT", f"/storage/v1/bucket/{BUCKET}", {
        "public": False,
        "file_size_limit": FINDING_BUCKET_FILE_SIZE_LIMIT,
        "allowed_mime_types": FINDING_BUCKET_MIME_TYPES,
    })
    bucket = _request("GET", f"/storage/v1/bucket/{BUCKET}") or {}
    allowed_types = bucket.get("allowed_mime_types")
    if allowed_types is not None and not DOCUMENT_MIME_TYPES.issubset(set(allowed_types)):
        raise FindingsError(
            "Supabase no confirmó los formatos documentales del bucket. "
            "Revisa la clave de servicio configurada en Streamlit Secrets y vuelve a intentar."
        )


def delete_finding_attachment(finding: dict, path: str, actor: str) -> None:
    finding_id = str(finding.get("id") or "")
    if not finding_id or not path.startswith(f"{finding_id}/"):
        raise FindingsError("El archivo no pertenece a este hallazgo.")
    collection_names = ("photos", "documents", "reviewed_attachments", "resolution_attachments")
    patch, found = {}, False
    for collection in collection_names:
        items = list(finding.get(collection) or [])
        remaining = [item for item in items if str(item.get("path") or "") != path]
        if len(remaining) != len(items):
            patch[collection] = remaining
            found = True
    if not found:
        raise FindingsError("No encontré ese archivo en los adjuntos del hallazgo. Actualiza la lista e inténtalo otra vez.")

    _request("DELETE", f"/storage/v1/object/{BUCKET}/{quote(path, safe='/')}")
    patch["updated_by"] = actor
    query = urlencode({"id": f"eq.{finding_id}"})
    result = _request("PATCH", f"/rest/v1/maintenance_findings?{query}", patch,
                      prefer="return=representation")
    if not isinstance(result, list) or not result:
        raise FindingsError("El archivo se quitó del almacenamiento, pero Supabase no confirmó la actualización del hallazgo. Actualiza la lista y vuelve a intentarlo.")


def save_new(payload: dict, photos, documents=None) -> dict:
    finding_id = str(uuid4())
    metadata = [_upload_file(file, finding_id) for file in (photos or [])]
    document_metadata = [_upload_file(file, finding_id, document=True) for file in (documents or [])]
    payload = {**payload, "id": finding_id, "photos": metadata, "documents": document_metadata,
               "reviewed_attachments": [], "resolution_attachments": []}
    result = _request("POST", "/rest/v1/maintenance_findings", payload, prefer="return=representation")
    if isinstance(result, list) and result:
        return result[0]
    raise FindingsError("Supabase no confirmó que se guardara el hallazgo. Actualiza la lista antes de reintentar.")


def update_finding(finding: dict, patch: dict, photos, documents, actor: str, evidence_stage: str = "general") -> dict:
    added = [_upload_file(file, str(finding["id"])) for file in (photos or [])]
    added.extend(_upload_file(file, str(finding["id"]), document=True) for file in (documents or []))
    existing = list(finding.get("photos") or [])
    existing_documents = list(finding.get("documents") or [])
    payload = {**patch, "photos": existing, "documents": existing_documents, "updated_by": actor}
    if evidence_stage == "reviewed":
        payload["reviewed_attachments"] = list(finding.get("reviewed_attachments") or []) + added
    elif evidence_stage == "resolution":
        payload["resolution_attachments"] = list(finding.get("resolution_attachments") or []) + added
    else:
        photo_additions = [item for item in added if not item.get("document")]
        doc_additions = [item for item in added if item.get("document")]
        payload["photos"] = existing + photo_additions
        payload["documents"] = existing_documents + doc_additions
    query = urlencode({"id": f"eq.{finding['id']}"})
    result = _request("PATCH", f"/rest/v1/maintenance_findings?{query}", payload,
                      prefer="return=representation")
    if isinstance(result, list) and result:
        return result[0]
    raise FindingsError("Supabase no confirmó la actualización del hallazgo. Actualiza la lista antes de reintentar.")


def photo_url(path: str, *, expires_in: int = 3600) -> str:
    data = _request("POST", f"/storage/v1/object/sign/{BUCKET}/{quote(path, safe='/')}",
                    {"expiresIn": expires_in})
    value = (data or {}).get("signedURL", "")
    if not value:
        raise FindingsError(f"Supabase no devolvió un enlace para {Path(path).name}.")
    base, _, _ = auth_config()
    if value.startswith("/object/"):
        return base + "/storage/v1" + value
    return base + value if value.startswith("/") else value


def _download_file(path: str) -> bytes:
    """Download one private Storage object with the server key; never expose it to the browser."""
    try:
        base, _, key = auth_config()
    except SupabaseSetupError as exc:
        raise FindingsError(str(exc)) from None
    url = base + f"/storage/v1/object/{BUCKET}/{quote(path, safe='/')}"
    request = Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"}, method="GET")
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise FindingsError(f"No se pudo descargar {Path(path).name} desde el almacenamiento compartido.") from exc


def _finding_attachments(row: dict) -> list[tuple[str, dict]]:
    attachments = []
    for collection, stage, document in (
        (row.get("photos") or [], "Registro", False),
        (row.get("documents") or [], "Registro", True),
        (row.get("reviewed_attachments") or [], "Revisión", None),
        (row.get("resolution_attachments") or [], "Resolución", None),
    ):
        for item in collection:
            metadata = dict(item)
            if document is not None:
                metadata["document"] = document
            attachments.append((stage, metadata))
    return attachments


def finding_history(finding_id: str) -> list[dict]:
    query = urlencode({"select": "*", "finding_id": f"eq.{finding_id}",
                       "order": "changed_at.desc", "limit": "100"})
    return _request("GET", "/rest/v1/maintenance_finding_history?" + query) or []


def _map_payload(rows, show_heat, focus, categories, evidence_focus_id=None):
    cases, cells = [], {}
    colors = {"Baja": "#1976a3", "Media": "#b06300", "Alta": "#c53427", "Urgente": "#89152e"}
    for row in rows:
        start = [float(row["latitude"]), float(row["longitude"])]
        end = None
        if row.get("second_latitude") is not None and row.get("second_longitude") is not None:
            end = [float(row["second_latitude"]), float(row["second_longitude"])]
        label = " · ".join(str(row[key]) for key in ("category", "notice_number", "priority", "status") if row.get(key))
        case_id = str(row.get("id", ""))
        evidence = []
        if evidence_focus_id and case_id == str(evidence_focus_id):
            for stage, attachment in _finding_attachments(row):
                if not attachment.get("path"):
                    continue
                try:
                    signed_url = photo_url(attachment["path"], expires_in=3600)
                except FindingsError:
                    signed_url = ""
                evidence.append({"name": attachment.get("name") or Path(attachment["path"]).name,
                                 "document": bool(attachment.get("document")), "stage": stage,
                                 "url": signed_url})
        cases.append({"id": case_id, "start": start, "end": end, "label": label,
                      "icon": _category_icon(row.get("category", ""), categories),
                      "icon_url": _category_icon_url(next((item for item in categories if item.get("name") == row.get("category")), {})),
                      "category": row.get("category") or "", "notice": row.get("notice_number") or "",
                      "requester": row.get("requester") or "", "locality": row.get("locality") or "",
                      "status": row.get("status") or "Pendiente", "priority": row.get("priority") or "Media",
                      "asset": row.get("asset_code") or "", "second_asset": row.get("second_asset_code") or "",
                      "description": row.get("description") or "", "reviewed_details": row.get("reviewed_details") or "",
                      "resolution_details": row.get("resolution_details") or "",
                      "photos": len(row.get("photos") or []) + sum(not f.get("document") for f in (row.get("reviewed_attachments") or []) + (row.get("resolution_attachments") or [])),
                      "documents": len(row.get("documents") or []) + sum(bool(f.get("document")) for f in (row.get("reviewed_attachments") or []) + (row.get("resolution_attachments") or [])),
                      "evidence": evidence,
                      "color": colors.get(row.get("priority"), "#b06300")})
        if show_heat:
            for lat, lon in [start] + ([end] if end else []):
                key = (round(lat * 1113.2), round(lon * 1113.2 * math.cos(math.radians(lat))))
                cell = cells.setdefault(key, [0, 0, 0, 0])
                cell[0] += lat
                cell[1] += lon
                cell[2] += 1
                cell[3] += {"Baja": 1, "Media": 2, "Alta": 3, "Urgente": 4}.get(row.get("priority"), 2)
    heat = [{"center": [lat / count, lon / count], "weight": weight}
            for lat, lon, count, weight in cells.values()]
    return {"cases": cases, "heat": heat, "focus": focus}


def _coordinate(raw: str, asset: dict | None, axis: str, label: str, optional: bool = False):
    value = (raw or "").strip().replace(",", ".")
    if value:
        try:
            number = float(value)
        except ValueError:
            raise FindingsError(f"Revisa la {label}; debe ser un número decimal.") from None
        limit = 90 if axis == "latitude" else 180
        if not -limit <= number <= limit:
            raise FindingsError(f"La {label} está fuera de rango.")
        return number
    if asset:
        return float(asset[axis])
    if optional:
        return None
    raise FindingsError(f"Escribe la {label} o indica un código que exista en el inventario.")


def _edited_coordinate(raw: str, original, asset: dict | None, axis: str, label: str, optional: bool = False):
    """Use inventory coordinates when the code/locality changed but position did not."""
    if asset and (not str(raw or "").strip() or str(raw).strip() == str(original)):
        return float(asset[axis])
    return _coordinate(raw, None, axis, label, optional=optional)


def render_findings_panel() -> None:
    map_selected_id = str(st.session_state.pop("finding_map_selected_id", "") or "")
    heading, close = st.columns((5, 1), vertical_alignment="center")
    heading.subheader("Hallazgos de mantenimiento")
    if close.button("×", key="close_findings_panel", help="Cerrar hallazgos"):
        st.session_state.pop("show_findings", None)
        st.query_params.pop("findings", None)
        st.rerun(scope="fragment")

    st.html("<style>.st-key-finding_map_selected_id{display:none!important}</style>")
    st.text_input("Caso seleccionado desde el mapa", key="finding_map_selected_id", label_visibility="collapsed")

    filter_cols = st.columns(2)
    locality = filter_cols[0].selectbox("Localidad", ("Todas",) + LOCALITIES, key="finding_locality")
    status_filter = filter_cols[1].selectbox("Estado", ("Abiertos", "Todos") + STATUSES, key="finding_status")
    # Give the checkbox its own row: adjacent full-width Streamlit controls could
    # overlap their hit areas at browser zoom and accidentally open icon settings.
    show_heat = st.checkbox("Mostrar concentración del tipo seleccionado", key="finding_show_heat")
    st.button("Actualizar lista", key="refresh_findings_list", width="stretch")

    try:
        rows = list_findings(locality, status_filter)
    except FindingsError as exc:
        st.error(str(exc))
        return
    try:
        shared_categories = list_shared_categories()
    except FindingsError as exc:
        st.error(str(exc))
        return

    available_categories = [str(row["name"]) for row in shared_categories]
    category_filter = st.selectbox("Tipo de hallazgo", ("Todos",) + tuple(available_categories), key="finding_type_filter")
    if category_filter != "Todos":
        rows = [row for row in rows if row.get("category") == category_filter]
    edit_choices = {
        f"{('Aviso ' + str(row.get('notice_number')) + ' · ') if row.get('notice_number') else ''}{row.get('category')} · {' ↔ '.join(str(value) for value in (row.get('asset_code'), row.get('second_asset_code')) if value) or 'sin activo'} · {row.get('locality')} · {row.get('status')} · {str(row['id'])[:8]}": row
        for row in rows
    }
    st.caption(f"{len(rows)} hallazgos del tipo y filtros seleccionados")
    if rows:
        text_buffer = StringIO()
        writer = csv.DictWriter(text_buffer, fieldnames=["Aviso", "Solicitante", "Tipo", "Localidad", "Estado", "Prioridad", "Activo", "Latitud", "Longitud", "Fotos", "Documentos"])
        writer.writeheader()
        for row in rows:
            writer.writerow({"Aviso": row.get("notice_number") or "", "Solicitante": row.get("requester") or "",
                             "Tipo": row.get("category") or "", "Localidad": row.get("locality") or "",
                             "Estado": row.get("status") or "", "Prioridad": row.get("priority") or "",
                             "Activo": row.get("asset_code") or "", "Latitud": row.get("latitude"), "Longitud": row.get("longitude"),
                             "Fotos": sum(not file.get("document") for _, file in _finding_attachments(row)),
                             "Documentos": sum(bool(file.get("document")) for _, file in _finding_attachments(row))})
        st.download_button("Exportar informe de la lista filtrada (.csv)", data="\ufeff" + text_buffer.getvalue(),
                           file_name="Hallazgos_filtrados.csv", mime="text/csv", width="stretch")
    if st.button("🎨 Configurar iconos y tipos", key="open_finding_icon_config", width="stretch"):
        st.session_state["show_finding_icon_config"] = True
    if st.session_state.get("show_finding_icon_config", False):
        _finding_icon_dialog(shared_categories, bool(st.session_state.get("finding_category_catalog_ready", True)))
    if rows:
        st.dataframe([{"Aviso": row.get("notice_number") or "", "Solicitante": row.get("requester") or "",
                       "Tipo": f"{_category_icon(row.get('category', ''), shared_categories)} {row.get('category') or ''}",
                       "Localidad": row.get("locality") or "", "Estado": row.get("status") or "",
                       "Prioridad": row.get("priority") or "", "Activo": row.get("asset_code") or "",
                       "Latitud": row.get("latitude"), "Longitud": row.get("longitude")} for row in rows],
                   hide_index=True, width="stretch")
    focus = st.session_state.pop("finding_focus", None)
    evidence_focus_id = None
    if rows:
        by_id = {str(row["id"]): row for row in rows}
        def describe(finding_id):
            row = by_id[finding_id]
            return " · ".join(str(row[key]) for key in ("category", "notice_number", "asset_code", "status") if row.get(key))

        finding_id = st.selectbox("Caso en el mapa", list(by_id), format_func=describe)
        if map_selected_id and map_selected_id in by_id:
            selected_row = by_id[map_selected_id]
            selected_label = next((label for label, row in edit_choices.items()
                                   if str(row["id"]) == map_selected_id), None)
            if selected_label:
                st.session_state["finding_edit_choice"] = selected_label
            evidence_focus_id = map_selected_id
            focus = {"coordinates": [float(selected_row["latitude"]), float(selected_row["longitude"])],
                     "id": map_selected_id}
        if st.button("Ubicar en el mapa"):
            row = by_id[finding_id]
            focus = {"coordinates": [float(row["latitude"]), float(row["longitude"])], "id": str(row["id"])}
    payload = escape(json.dumps(_map_payload(rows, show_heat, focus, shared_categories, evidence_focus_id), ensure_ascii=False), quote=True)
    st.html(f'<span hidden id="findings-map-data" data-rows="{payload}"></span>')

    create_tab, follow_tab = st.tabs(("Crear hallazgo", "Seguimiento"))
    with create_tab:
        st.subheader("Crear hallazgo")
        if st.button("📍 Añadir ubicación en el mapa", key="finding_place_on_map", width="stretch"):
            st.session_state.finding_place_mode = not st.session_state.get("finding_place_mode", False)
        placing = bool(st.session_state.get("finding_place_mode", False))
        st.caption("Clic derecho en el mapa para colocar el hallazgo. En celular o tablet, activa este botón y toca el lugar. El marcador se puede arrastrar para corregirlo.")
        place_col, gps_col = st.columns(2)
        if place_col.button("Usar centro de la localidad", key="finding_use_map_center", width="stretch"):
            center_lat, center_lon = LOCALITY_CENTERS.get(locality, LOCALITY_CENTERS["Punta Arenas"])
            st.session_state.finding_location = {"lat": center_lat, "lon": center_lon, "source": "map"}
            st.rerun(scope="fragment")
        if gps_col.button("◎ Usar mi ubicación GPS", key="finding_use_gps", width="stretch"):
            st.session_state.finding_location = {"source": "gps", "requested_at": time.time()}
        location = st.session_state.get("finding_location", {})
        if not location:
            st.html("<script>try{const p=JSON.parse(sessionStorage.getItem('finding-pending-coordinate')||'null');if(p){const n=parent.document.querySelector('#finding-coordinate-data');if(n){n.dataset.lat=p.lat;n.dataset.lon=p.lon;n.dataset.source='map'}sessionStorage.removeItem('finding-pending-coordinate')}}catch(e){}</script>")
        coords_node = '<span hidden id="finding-coordinate-data" data-lat="{}" data-lon="{}" data-source="{}" data-requested-at="{}"></span>'.format(
            escape(str(location.get("lat", "")), quote=True), escape(str(location.get("lon", "")), quote=True),
            escape(str(location.get("source", "")), quote=True), escape(str(location.get("requested_at", "")), quote=True))
        st.html(coords_node)
        category_icons = {
            str(row["name"]): {"icon": str(row.get("icon") or "📍"), "url": _category_icon_url(row)}
            for row in shared_categories
        }
        st.html('<span hidden id="finding-category-icons" data-icons="{}"></span>'.format(
            escape(json.dumps(category_icons, ensure_ascii=False), quote=True)))
        if placing:
            st.info("Modo ubicación activo: toca el mapa para fijar el punto. En computador también puedes usar clic derecho.")
        with st.form("create_shared_finding", clear_on_submit=True):
            location = st.session_state.get("finding_location", {})
            suggested_locality = _detect_locality(location.get("lat"), location.get("lon")) if location.get("lat") is not None else None
            locality_default = suggested_locality or (locality if locality in LOCALITIES else "Punta Arenas")
            f_locality = st.selectbox("Localidad", LOCALITIES, index=LOCALITIES.index(locality_default))
            notice = st.text_input("N.º de aviso", placeholder="Número de aviso")
            requester = st.text_input("Solicitante", placeholder="Nombre o área solicitante")
            category_choice = st.selectbox("Hallazgo", available_categories,
                                           format_func=lambda name: f"{_category_icon(name, shared_categories)}  {name}")
            priority = st.select_slider("Prioridad", options=PRIORITIES, value="Media")
            asset_code = st.text_input("Poste o activo relacionado", value=str(location.get("asset_code", "")), placeholder="Código exacto; se buscará en el inventario")
            second_asset = st.text_input("Segundo poste / activo", placeholder="Para un caso entre dos activos")
            description = st.text_area("Descripción / acción requerida", max_chars=3000)
            st.caption("Si el código existe en el inventario, las coordenadas se completan desde el KMZ. También puedes indicarlas manualmente.")
            lat_col, lon_col = st.columns(2)
            latitude = lat_col.text_input("Latitud", value=str(location.get("lat", "")), placeholder="Se completa desde el activo")
            longitude = lon_col.text_input("Longitud", value=str(location.get("lon", "")), placeholder="Se completa desde el activo")
            second_lat_col, second_lon_col = st.columns(2)
            second_latitude = second_lat_col.text_input("Latitud segundo activo", placeholder="Si aplica")
            second_longitude = second_lon_col.text_input("Longitud segundo activo", placeholder="Si aplica")
            photos = st.file_uploader("Fotos (máximo 3)", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="finding_create_photos")
            documents = st.file_uploader("Documentos (máximo 2, 10 MB cada uno)", type=list(DOCUMENT_EXTENSIONS), accept_multiple_files=True, key="finding_create_documents", max_upload_size=10)
            submit = st.form_submit_button("Guardar hallazgo compartido", type="primary", width="stretch")
        if submit:
            if len(photos or []) > 3 or len(documents or []) > 2:
                st.error("Puedes adjuntar hasta 3 fotos y 2 documentos por hallazgo.")
            else:
                actor = str(st.session_state.get("map_login_user", "usuario"))
                try:
                    asset = lookup_asset(f_locality, asset_code) if asset_code.strip() else None
                    second = lookup_asset(f_locality, second_asset) if second_asset.strip() else None
                    lat = _coordinate(latitude, asset, "latitude", "latitud del primer activo")
                    lon = _coordinate(longitude, asset, "longitude", "longitud del primer activo")
                    # A finding may concern a single point. The second asset and
                    # its coordinates enrich the record only when available.
                    second_lat_raw = (second_latitude or "").strip()
                    second_lon_raw = (second_longitude or "").strip()
                    if second_lat_raw and second_lon_raw:
                        lat2 = _coordinate(second_lat_raw, None, "latitude", "latitud del segundo activo")
                        lon2 = _coordinate(second_lon_raw, None, "longitude", "longitud del segundo activo")
                    elif second and not second_lat_raw and not second_lon_raw:
                        lat2, lon2 = float(second["latitude"]), float(second["longitude"])
                    else:
                        # Ignore an incomplete optional pair rather than blocking
                        # a valid single-coordinate finding.
                        lat2, lon2 = None, None
                    category = category_choice
                    detected_locality = _detect_locality(lat, lon)
                    # Keep the editable locality selection when it is deliberate; use the
                    # coordinate-derived locality as the initial value for map/GPS points.
                    final_locality = f_locality or detected_locality or "Punta Arenas"
                    payload = {"locality": final_locality, "notice_number": notice.strip(), "requester": requester.strip(), "category": category,
                               "description": description.strip(), "priority": priority, "status": "Pendiente",
                               "asset_code": asset_code.strip() or None, "second_asset_code": second_asset.strip() or None,
                               "latitude": lat, "longitude": lon, "second_latitude": lat2, "second_longitude": lon2,
                               "created_by": actor, "updated_by": actor}
                    saved = save_new(payload, photos, documents)
                    st.session_state.finding_focus = {"coordinates": [lat, lon], "id": str(saved["id"])}
                    st.session_state.finding_locality = "Todas"
                    st.session_state.finding_status = "Abiertos"
                    st.session_state.finding_type_filter = "Todos"
                    st.session_state.finding_place_mode = False
                    st.session_state.pop("finding_location", None)
                    st.html("<script>try{sessionStorage.removeItem('finding-pending-coordinate')}catch(e){}</script>")
                    st.success("Hallazgo compartido guardado.")
                    st.rerun(scope="fragment")
                except FindingsError as exc:
                    st.error(str(exc))

    with follow_tab:
        if not rows:
            st.caption("Agrega o filtra hallazgos para iniciar el seguimiento.")
        else:
            label = st.selectbox("Hallazgo", list(edit_choices), key="finding_edit_choice")
            selected = edit_choices[label]
            existing_photos = [(file, "Registro") for file in (selected.get("photos") or [])]
            existing_photos += [(file, "Revisión") for file in (selected.get("reviewed_attachments") or []) if not file.get("document")]
            existing_photos += [(file, "Resolución") for file in (selected.get("resolution_attachments") or []) if not file.get("document")]
            existing_documents = [(file, "Registro") for file in (selected.get("documents") or [])]
            existing_documents += [(file, "Revisión") for file in (selected.get("reviewed_attachments") or []) if file.get("document")]
            existing_documents += [(file, "Resolución") for file in (selected.get("resolution_attachments") or []) if file.get("document")]
            if existing_photos:
                st.caption(f"Fotos del hallazgo ({len(existing_photos)})")
                photo_cols = st.columns(min(3, len(existing_photos)))
                for index, (photo, stage) in enumerate(existing_photos):
                    photo_col = photo_cols[index % len(photo_cols)]
                    try:
                        image_url = photo_url(photo["path"])
                        photo_col.image(image_url, caption=f"{stage} · {photo.get('name', 'Foto')}", width="stretch")
                        photo_col.link_button("Abrir foto", image_url, width="stretch")
                    except (FindingsError, KeyError) as exc:
                        photo_col.warning(f"No se pudo cargar esta foto: {exc}")
                    if photo_col.button("🗑 Eliminar foto", key=f"delete_finding_photo_{selected['id']}_{index}",
                                        help="Borra el archivo de Supabase y su referencia en este hallazgo.", width="stretch"):
                        try:
                            delete_finding_attachment(selected, photo["path"], str(st.session_state.get("map_login_user", "usuario")))
                            st.session_state.pop("finding_report_bundle", None)
                            st.rerun(scope="fragment")
                        except (FindingsError, KeyError) as exc:
                            photo_col.error(str(exc))
            if existing_documents:
                st.caption("Documentos adjuntos")
                for index, (document, stage) in enumerate(existing_documents):
                    document_col, delete_col = st.columns((5, 1), vertical_alignment="center")
                    try:
                        document_col.link_button(f"📎 {stage} · {document.get('name', 'Abrir documento')}", photo_url(document["path"]), width="stretch")
                    except (FindingsError, KeyError) as exc:
                        document_col.warning(f"No se pudo abrir {document.get('name', 'el documento')}: {exc}")
                    if delete_col.button("🗑", key=f"delete_finding_document_{selected['id']}_{index}",
                                         help=f"Eliminar {document.get('name', 'documento')} del almacenamiento y del hallazgo."):
                        try:
                            delete_finding_attachment(selected, document["path"], str(st.session_state.get("map_login_user", "usuario")))
                            st.session_state.pop("finding_report_bundle", None)
                            st.rerun(scope="fragment")
                        except (FindingsError, KeyError) as exc:
                            document_col.error(str(exc))
            st.dataframe([{"Aviso": selected.get("notice_number") or "", "Tipo": selected.get("category") or "",
                           "Localidad": selected.get("locality") or "", "Estado": selected.get("status") or "",
                           "Prioridad": selected.get("priority") or "", "Activo": selected.get("asset_code") or "",
                           "Solicitante": selected.get("requester") or ""}], hide_index=True, width="stretch")
            with st.form("edit_shared_finding"):
                edit_locality = st.selectbox("Localidad", LOCALITIES,
                                             index=LOCALITIES.index(selected.get("locality", LOCALITIES[0])))
                new_status = st.selectbox("Estado", STATUSES, index=STATUSES.index(selected.get("status", "Pendiente")))
                new_priority = st.selectbox("Prioridad", PRIORITIES, index=PRIORITIES.index(selected.get("priority", "Media")))
                new_notice = st.text_input("Aviso", value=selected.get("notice_number", ""))
                new_requester = st.text_input("Solicitante", value=selected.get("requester", ""))
                edit_options = list(dict.fromkeys(available_categories + [str(selected.get("category", "Otro"))]))
                edit_category = st.selectbox("Hallazgo", edit_options,
                                             index=available_categories.index(selected.get("category"))
                                             if selected.get("category") in available_categories else 0,
                                             format_func=lambda name: f"{_category_icon(name, shared_categories)}  {name}" if name in available_categories else f"📍  {name} (histórico)")
                edit_asset = st.text_input("Activo relacionado", value=selected.get("asset_code") or "")
                edit_second_asset = st.text_input("Segundo activo", value=selected.get("second_asset_code") or "")
                edit_lat_col, edit_lon_col = st.columns(2)
                edit_latitude = edit_lat_col.text_input("Latitud del punto", value=str(selected.get("latitude", "")))
                edit_longitude = edit_lon_col.text_input("Longitud del punto", value=str(selected.get("longitude", "")))
                edit_lat2_col, edit_lon2_col = st.columns(2)
                edit_second_latitude = edit_lat2_col.text_input("Latitud del segundo punto", value=str(selected.get("second_latitude") or ""))
                edit_second_longitude = edit_lon2_col.text_input("Longitud del segundo punto", value=str(selected.get("second_longitude") or ""))
                new_desc = st.text_area("Descripción", value=selected.get("description", ""), max_chars=3000)
                observed = st.text_area("Qué se revisó o encontró", value=selected.get("reviewed_details", ""), max_chars=3000,
                                        help="Se requiere al marcar el caso como Revisado.")
                resolution = st.text_area("Trabajo realizado para resolver", value=selected.get("resolution_details", ""), max_chars=3000,
                                          help="Se requiere al marcar el caso como Resuelto.")
                existing_all_photos = list(selected.get("photos") or []) + [f for f in (selected.get("reviewed_attachments") or []) + (selected.get("resolution_attachments") or []) if not f.get("document")]
                existing_all_documents = list(selected.get("documents") or []) + [f for f in (selected.get("reviewed_attachments") or []) + (selected.get("resolution_attachments") or []) if f.get("document")]
                more_photos = st.file_uploader("Agregar fotos (máximo 3 en total)", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="finding_edit_photos")
                more_documents = st.file_uploader("Agregar documentos (máximo 2 en total, 10 MB cada uno)", type=list(DOCUMENT_EXTENSIONS), accept_multiple_files=True, key="finding_edit_documents", max_upload_size=10)
                save = st.form_submit_button("Guardar seguimiento", type="primary", width="stretch")
            if save:
                if len(existing_all_photos) + len(more_photos or []) > 3 or len(existing_all_documents) + len(more_documents or []) > 2:
                    st.error("El hallazgo admite hasta 3 fotos y 2 documentos en total.")
                elif new_status == "Cerrado" and selected.get("status") not in ("Resuelto", "Cerrado"):
                    st.error("Para cerrar un hallazgo, primero debe quedar Resuelto y guardarse la descripción del trabajo realizado.")
                elif new_status in ("Revisado", "Resuelto", "Cerrado") and not observed.strip():
                    st.error("Describe qué se revisó o encontró antes de avanzar el seguimiento.")
                elif new_status in ("Resuelto", "Cerrado") and not resolution.strip():
                    st.error("Describe el trabajo realizado antes de guardar el estado Resuelto o Cerrado.")
                else:
                    try:
                        matched_asset = lookup_asset(edit_locality, edit_asset) if edit_asset.strip() else None
                        matched_second = lookup_asset(edit_locality, edit_second_asset) if edit_second_asset.strip() else None
                        edit_lat = _edited_coordinate(edit_latitude, selected.get("latitude"), matched_asset, "latitude", "latitud")
                        edit_lon = _edited_coordinate(edit_longitude, selected.get("longitude"), matched_asset, "longitude", "longitud")
                        second_lat_raw = (edit_second_latitude or "").strip()
                        second_lon_raw = (edit_second_longitude or "").strip()
                        if second_lat_raw and second_lon_raw:
                            edit_lat2 = _edited_coordinate(second_lat_raw, selected.get("second_latitude"), matched_second, "latitude", "latitud del segundo punto")
                            edit_lon2 = _edited_coordinate(second_lon_raw, selected.get("second_longitude"), matched_second, "longitude", "longitud del segundo punto")
                        elif matched_second and not second_lat_raw and not second_lon_raw:
                            edit_lat2, edit_lon2 = float(matched_second["latitude"]), float(matched_second["longitude"])
                        else:
                            edit_lat2, edit_lon2 = None, None
                        evidence_stage = "reviewed" if new_status == "Revisado" else "resolution" if new_status in ("Resuelto", "Cerrado") else "general"
                        resolved_at = None
                        if new_status in ("Resuelto", "Cerrado"):
                            resolved_at = selected.get("resolved_at") or datetime.now(timezone.utc).isoformat()
                        update_finding(selected, {"status": new_status, "priority": new_priority,
                                                  "locality": edit_locality, "notice_number": new_notice.strip(),
                                                  "requester": new_requester.strip(),
                                                  "category": edit_category,
                                                  "asset_code": edit_asset.strip() or None,
                                                  "second_asset_code": edit_second_asset.strip() or None,
                                                  "latitude": edit_lat, "longitude": edit_lon,
                                                  "second_latitude": edit_lat2, "second_longitude": edit_lon2,
                                                  "description": new_desc.strip(),
                                                  "reviewed_details": observed.strip(),
                                                  "resolution_details": resolution.strip(),
                                                  "resolved_at": resolved_at},
                                       more_photos, more_documents, str(st.session_state.get("map_login_user", "usuario")), evidence_stage)
                        st.success("Seguimiento actualizado y registrado en el historial.")
                        st.rerun(scope="fragment")
                    except FindingsError as exc:
                        st.error(str(exc))
            history = []
            try:
                with st.expander("Historial del caso"):
                    history = finding_history(str(selected["id"]))
                    if history:
                        for event in history:
                            snapshot = event.get("snapshot") or {}
                            when = str(event.get("changed_at", ""))[:16].replace("T", " ")
                            st.markdown(f"**{event.get('action')} · {when} · {event.get('changed_by', 'usuario')}**")
                            details = [snapshot.get(key) for key in ("status", "priority", "notice_number", "requester", "asset_code", "reviewed_details", "resolution_details") if snapshot.get(key)]
                            if details:
                                st.caption(" · ".join(str(value) for value in details))
                    else:
                        st.caption("Todavía no hay cambios registrados.")
            except FindingsError as exc:
                st.caption(f"No se pudo leer el historial: {exc}")

            try:
                report_history = finding_history(str(selected["id"]))
            except FindingsError:
                report_history = history
            report_name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(selected.get("notice_number") or selected["id"])).strip("_")
            report_col, bundle_col = st.columns(2)
            report_col.download_button("Informe HTML", data=_finding_report_html(selected, report_history),
                                       file_name=f"Hallazgo_{report_name}.html", mime="text/html", width="stretch",
                                       help="Incluye el seguimiento y enlaces temporales a los archivos compartidos.")
            if bundle_col.button("Preparar informe con archivos", key="prepare_finding_report_bundle", width="stretch"):
                try:
                    bundle = _finding_report_bundle(selected, report_history)
                    st.session_state["finding_report_bundle"] = {"id": str(selected["id"]), "data": bundle}
                except FindingsError as exc:
                    st.error(str(exc))
            bundle_state = st.session_state.get("finding_report_bundle") or {}
            if bundle_state.get("id") == str(selected["id"]):
                st.download_button("Descargar informe completo (.zip)", data=bundle_state["data"],
                                   file_name=f"Hallazgo_{report_name}_completo.zip", mime="application/zip",
                                   key="download_finding_report_bundle", width="stretch")

def _detect_locality(latitude, longitude):
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    thresholds = {"Punta Arenas": 0.60, "Puerto Natales": 0.48, "Porvenir": 0.40, "Puerto Williams": 0.35}
    candidates = []
    for name, (center_lat, center_lon) in LOCALITY_CENTERS.items():
        distance = math.hypot((lat-center_lat), (lon-center_lon)*math.cos(math.radians(lat)))
        candidates.append((distance, name))
    distance, name = min(candidates)
    return name if distance <= thresholds[name] else None


def _finding_report_html(row, history, attachment_links=None):
    safe = lambda value: escape(str(value or ""))
    fields = [("Estado", row.get("status")), ("Prioridad", row.get("priority")), ("Localidad", row.get("locality")),
              ("Aviso", row.get("notice_number")), ("Solicitante", row.get("requester")),
              ("Tipo", row.get("category")), ("Activo", row.get("asset_code")),
              ("Segundo activo", row.get("second_asset_code")), ("Descripción", row.get("description")),
              ("Revisión", row.get("reviewed_details")), ("Trabajo realizado", row.get("resolution_details")),
              ("Coordenadas", f"{row.get('latitude')}, {row.get('longitude')}")]
    map_url = f"https://www.google.com/maps?q={row.get('latitude')},{row.get('longitude')}"
    items = "".join(f"<tr><th>{safe(label)}</th><td>{safe(value)}</td></tr>" for label, value in fields if value not in (None, ""))
    events = "".join(f"<li><b>{safe(e.get('action'))}</b> · {safe(e.get('changed_at'))} · {safe(e.get('changed_by'))}</li>" for e in history)
    evidence = _finding_attachments(row)
    attachment_links = attachment_links or {}
    attachment_list, photo_sections = [], []
    for stage, item in evidence:
        try:
            url = escape(attachment_links.get(item["path"]) or photo_url(item["path"]), quote=True)
        except (FindingsError, KeyError):
            attachment_list.append(f"<li>{safe(item.get('name'))} · {stage} (enlace no disponible)</li>")
            continue
        name = safe(item.get("name"))
        if item.get("document"):
            attachment_list.append(f"<li><a href=\"{url}\" target=\"_blank\" rel=\"noopener\">{name}</a> · {stage}</li>")
        else:
            attachment_list.append(f"<li>{name} · {stage}</li>")
            photo_sections.append(f"<figure><img src=\"{url}\" alt=\"{name}\"><figcaption>{name} · {stage}</figcaption></figure>")
    attachment_list = "".join(attachment_list)
    photos_html = "".join(photo_sections)
    return ("<!doctype html><meta charset='utf-8'><title>Informe de hallazgo</title>"
            "<style>body{font:15px Arial,sans-serif;color:#17324d;max-width:900px;margin:36px auto;padding:0 22px}h1{color:#174a75}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:9px;border-bottom:1px solid #d8e1e8;vertical-align:top}th{width:190px;background:#f2f6f9}a{color:#1769aa}.photos{display:flex;flex-wrap:wrap;justify-content:center;gap:16px}.photos figure{margin:0;max-width:260px;text-align:center}.photos img{max-width:100%;max-height:360px;object-fit:contain}.photos figcaption{font-size:12px;color:#52687b;margin-top:5px}</style>"
            f"<h1>Informe de hallazgo · {safe(row.get('category'))}</h1><p>{safe(row.get('locality'))} · Actualizado {safe(row.get('updated_at'))}</p>"
            f"<table>{items}</table><p><a href='{escape(map_url, quote=True)}'>Abrir ubicación en Google Maps</a></p>"
            f"<h2>Registro fotográfico</h2><div class='photos'>{photos_html or '<p>Sin fotografías adjuntas</p>'}</div>"
            f"<h2>Archivos adjuntos</h2><ul>{attachment_list or '<li>Sin archivos adjuntos</li>'}</ul>"
            f"<h2>Seguimiento</h2><ul>{events or '<li>Sin movimientos registrados</li>'}</ul>")


def _finding_report_bundle(row: dict, history: list[dict]) -> bytes:
    archive_buffer = BytesIO()
    attachment_links, used_names = {}, set()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (_, item) in enumerate(_finding_attachments(row), 1):
            path = str(item.get("path") or "")
            if not path:
                continue
            original_name = Path(str(item.get("name") or Path(path).name)).name
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original_name).strip("._") or f"archivo_{index}"
            target = f"archivos/{index:02d}_{safe_name}"
            while target.casefold() in used_names:
                target = f"archivos/{index:02d}_{uuid4().hex[:6]}_{safe_name}"
            used_names.add(target.casefold())
            archive.writestr(target, _download_file(path))
            attachment_links[path] = target
        archive.writestr("Informe.html", _finding_report_html(row, history, attachment_links))
    return archive_buffer.getvalue()
