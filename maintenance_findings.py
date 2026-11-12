"""Server-side shared maintenance findings for the Redes Edelmag map."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import streamlit as st

from supabase_accounts import auth_config, SupabaseSetupError

LOCALITIES = ("Punta Arenas", "Puerto Natales", "Porvenir", "Puerto Williams")
STATUSES = ("Pendiente", "En revisión", "Revisado", "Resuelto", "Cerrado")
PRIORITIES = ("Baja", "Media", "Alta", "Urgente")
CATEGORIES = ("Poda preventiva", "Poste desaplomado", "Estructura dañada", "Choque de poste",
              "Retensado de línea", "Tirante o protección", "Equipo defectuoso", "Otro")
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


def _request(method: str, path: str, payload=None, *, content_type="application/json", prefer=None):
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
    request = Request(url + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except HTTPError as exc:
        if exc.code == 404:
            raise FindingsError("No encuentro las tablas o el bucket. Ejecuta la migración SQL de Supabase una vez.") from None
        if exc.code in (401, 403):
            raise FindingsError("Supabase rechazó el acceso del servidor. Revisa la clave privada configurada.") from None
        raise FindingsError(f"Supabase respondió con error HTTP {exc.code}. Revisa que la migración esté aplicada.") from None
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


def _upload_photo(file, finding_id: str) -> dict:
    mime = str(file.type or "").lower()
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        raise FindingsError(f"Formato no permitido para {file.name}; usa JPG, PNG o WEBP.")
    content = file.getvalue()
    if len(content) > 10 * 1024 * 1024:
        raise FindingsError(f"{file.name} supera el límite de 10 MB.")
    suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
    path = f"{finding_id}/{uuid4().hex}.{suffix}"
    _request("POST", f"/storage/v1/object/{BUCKET}/{quote(path, safe='/')}", content,
             content_type=mime, prefer=None)
    return {"path": path, "name": file.name, "type": mime, "size": len(content)}


def save_new(payload: dict, photos) -> dict:
    finding_id = str(uuid4())
    metadata = [_upload_photo(file, finding_id) for file in (photos or [])]
    payload = {**payload, "id": finding_id, "photos": metadata}
    result = _request("POST", "/rest/v1/maintenance_findings", payload, prefer="return=representation")
    if isinstance(result, list) and result:
        return result[0]
    return payload


def update_finding(finding: dict, patch: dict, photos, actor: str) -> dict:
    added = [_upload_photo(file, str(finding["id"])) for file in (photos or [])]
    existing = list(finding.get("photos") or [])
    payload = {**patch, "photos": existing + added, "updated_by": actor}
    query = urlencode({"id": f"eq.{finding['id']}"})
    result = _request("PATCH", f"/rest/v1/maintenance_findings?{query}", payload,
                      prefer="return=representation")
    if isinstance(result, list) and result:
        return result[0]
    return {**finding, **payload}


def photo_url(path: str) -> str:
    data = _request("POST", f"/storage/v1/object/sign/{BUCKET}/{quote(path, safe='/')}",
                    {"expiresIn": 3600})
    value = (data or {}).get("signedURL", "")
    base, _, _ = auth_config()
    if value.startswith("/object/"):
        return base + "/storage/v1" + value
    return base + value if value.startswith("/") else value


def finding_history(finding_id: str) -> list[dict]:
    query = urlencode({"select": "*", "finding_id": f"eq.{finding_id}",
                       "order": "changed_at.desc", "limit": "100"})
    return _request("GET", "/rest/v1/maintenance_finding_history?" + query) or []


def _heatmap(rows):
    import pydeck as pdk
    if not rows:
        st.info("No hay hallazgos para los filtros elegidos.")
        return
    points, paths, cells = [], [], {}
    for row in rows:
        weight = {"Baja": 1, "Media": 2, "Alta": 3, "Urgente": 4}.get(row.get("priority"), 2)
        label = f"{row.get('category','Hallazgo')} · {row.get('asset_code') or row.get('notice_number') or row['id'][:8]}"
        start = [float(row["longitude"]), float(row["latitude"])]
        points.append({"latitude": start[1], "longitude": start[0], "weight": weight, "label": label})
        end_lat, end_lon = row.get("second_latitude"), row.get("second_longitude")
        if end_lat is not None and end_lon is not None:
            end = [float(end_lon), float(end_lat)]
            paths.append({"path": [start, end], "label": label})
            points.append({"latitude": end[1], "longitude": end[0], "weight": weight, "label": label})
    # Aggregate in approximately 100 m cells so the hotspot footprint stays
    # geographically meaningful as the map zoom changes.
    for point in points:
        lat, lon = point["latitude"], point["longitude"]
        cell_y = round(lat * 111_320 / 100)
        cell_x = round(lon * 111_320 * max(0.2, abs(math.cos(math.radians(lat)))) / 100)
        cell = cells.setdefault((cell_x, cell_y), {"latitude": 0.0, "longitude": 0.0, "weight": 0.0, "count": 0})
        cell["latitude"] += lat
        cell["longitude"] += lon
        cell["weight"] += point["weight"]
        cell["count"] += 1
    heat_points = [{**cell, "latitude": cell["latitude"] / cell["count"],
                    "longitude": cell["longitude"] / cell["count"],
                    "label": f"{cell['count']} ubicaciones de hallazgos · prioridad acumulada {cell['weight']:.0f}"}
                   for cell in cells.values()]
    latitudes = [p["latitude"] for p in points]
    longitudes = [p["longitude"] for p in points]
    center_lat = (min(latitudes) + max(latitudes)) / 2
    center_lon = (min(longitudes) + max(longitudes)) / 2
    span = max(max(latitudes) - min(latitudes),
               (max(longitudes) - min(longitudes)) * abs(math.cos(math.radians(center_lat))),
               0.0001)
    zoom = max(3, min(18, math.log2(180 / span) - 0.25))
    layers = [
        pdk.Layer("HeatmapLayer", data=heat_points, get_position="[longitude, latitude]", get_weight="weight",
                  radiusPixels=55, intensity=1.2, threshold=0.03, opacity=0.72),
        pdk.Layer("PathLayer", data=paths, get_path="path", get_color=[210, 45, 35, 220],
                  width_min_pixels=3, pickable=True),
        pdk.Layer("ScatterplotLayer", data=points, get_position="[longitude, latitude]", get_radius=42,
                  get_fill_color="[220, 70, 45, 210]", get_line_color="[255,255,255,240]",
                  line_width_min_pixels=1, pickable=True),
    ]
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(
        latitude=center_lat, longitude=center_lon, zoom=zoom,
        pitch=0, bearing=0), tooltip={"text": "{label}"},
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"), width="stretch")


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


def render_findings_page() -> None:
    st.title("Hallazgos de mantenimiento")
    st.caption("Registro compartido entre usuarios · Filtra por localidad, registra aviso y prioridad, y actualiza el seguimiento.")
    if st.button("← Volver al mapa"):
        st.query_params.clear()
        st.rerun()

    filter_cols = st.columns((1.4, 1.2, 1))
    locality = filter_cols[0].selectbox("Localidad", ("Todas",) + LOCALITIES, key="finding_locality")
    status_filter = filter_cols[1].selectbox("Estado", ("Abiertos", "Todos") + STATUSES, key="finding_status")
    if filter_cols[2].button("Actualizar lista", width="stretch"):
        st.rerun()

    try:
        rows = list_findings(locality, status_filter)
    except FindingsError as exc:
        st.error(str(exc))
        st.info("La migración no borra datos ni cambia los permisos globales de Storage. Está en `supabase/migrations/202611120001_shared_maintenance_findings.sql`.")
        return

    count_cols = st.columns(4)
    for col, label, count in zip(count_cols, ("Hallazgos", "Pendientes", "Alta / urgente", "Resueltos"),
                                 (len(rows), sum(r.get("status") == "Pendiente" for r in rows),
                                  sum(r.get("priority") in ("Alta", "Urgente") for r in rows),
                                  sum(r.get("status") in ("Resuelto", "Cerrado") for r in rows))):
        col.metric(label, count)

    map_col, form_col = st.columns((1.6, 1))
    with map_col:
        st.subheader("Mapa de concentración · celdas de 100 m")
        _heatmap(rows)
        if rows:
            display = [{"Localidad": r.get("locality"), "Aviso": r.get("notice_number"),
                        "Activo": " ↔ ".join(x for x in (r.get("asset_code"), r.get("second_asset_code")) if x),
                        "Hallazgo": r.get("category"), "Prioridad": r.get("priority"), "Estado": r.get("status"),
                        "Actualizado": str(r.get("updated_at", ""))[:16].replace("T", " "),
                        "Abrir en Maps": f"https://www.google.com/maps?q={r.get('latitude')},{r.get('longitude')}"}
                       for r in rows]
            st.dataframe(display, width="stretch", hide_index=True,
                         column_config={"Abrir en Maps": st.column_config.LinkColumn("Abrir en Maps", display_text="Ver punto")})
    with form_col:
        st.subheader("Crear hallazgo")
        with st.form("create_shared_finding", clear_on_submit=True):
            f_locality = st.selectbox("Localidad", LOCALITIES, index=LOCALITIES.index(locality) if locality in LOCALITIES else 0)
            notice = st.text_input("N.º de aviso", placeholder="Número de aviso")
            category = st.selectbox("Tipo", CATEGORIES)
            priority = st.select_slider("Prioridad", options=PRIORITIES, value="Media")
            asset_code = st.text_input("Poste o activo relacionado", placeholder="Código exacto; se buscará en el inventario")
            second_asset = st.text_input("Segundo poste / activo (si corresponde)", placeholder="Para un caso entre dos activos")
            description = st.text_area("Descripción / acción requerida", max_chars=3000)
            st.caption("Si el código existe en el inventario, las coordenadas se completan desde el KMZ. También puedes indicarlas manualmente.")
            lat_col, lon_col = st.columns(2)
            latitude = lat_col.text_input("Latitud", placeholder="Se completa desde el activo")
            longitude = lon_col.text_input("Longitud", placeholder="Se completa desde el activo")
            second_lat_col, second_lon_col = st.columns(2)
            second_latitude = second_lat_col.text_input("Latitud segundo activo", placeholder="Opcional")
            second_longitude = second_lon_col.text_input("Longitud segundo activo", placeholder="Opcional")
            photos = st.file_uploader("Fotos (máximo 3)", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="finding_create_photos")
            submit = st.form_submit_button("Guardar hallazgo compartido", type="primary", width="stretch")
        if submit:
            if len(photos or []) > 3:
                st.error("Puedes adjuntar hasta 3 fotos por hallazgo.")
            else:
                actor = str(st.session_state.get("map_login_user", "usuario"))
                try:
                    asset = lookup_asset(f_locality, asset_code) if asset_code.strip() else None
                    second = lookup_asset(f_locality, second_asset) if second_asset.strip() else None
                    lat = _coordinate(latitude, asset, "latitude", "latitud del primer activo")
                    lon = _coordinate(longitude, asset, "longitude", "longitud del primer activo")
                    lat2 = _coordinate(second_latitude, second, "latitude", "latitud del segundo activo", optional=True)
                    lon2 = _coordinate(second_longitude, second, "longitude", "longitud del segundo activo", optional=True)
                    if (lat2 is None) != (lon2 is None):
                        raise FindingsError("Completa latitud y longitud del segundo activo, o deja ambas vacías.")
                    if second_asset.strip() and lat2 is None:
                        raise FindingsError("No encontré el segundo activo en esa localidad; indica ambas coordenadas.")
                    payload = {"locality": f_locality, "notice_number": notice.strip(), "category": category,
                               "description": description.strip(), "priority": priority, "status": "Pendiente",
                               "asset_code": asset_code.strip() or None, "second_asset_code": second_asset.strip() or None,
                               "latitude": lat, "longitude": lon, "second_latitude": lat2, "second_longitude": lon2,
                               "created_by": actor, "updated_by": actor}
                    save_new(payload, photos)
                    st.success("Hallazgo compartido guardado.")
                    st.rerun()
                except FindingsError as exc:
                    st.error(str(exc))

        st.divider()
        st.subheader("Actualizar seguimiento")
        if not rows:
            st.caption("Agrega o filtra hallazgos para iniciar el seguimiento.")
        else:
            choices = {f"{('Aviso ' + r.get('notice_number') + ' · ') if r.get('notice_number') else ''}{r.get('category')} · {' ↔ '.join(x for x in (r.get('asset_code'), r.get('second_asset_code')) if x) or 'sin activo'} · {r.get('locality')} · {r.get('status')} · {str(r['id'])[:8]}": r for r in rows}
            label = st.selectbox("Hallazgo", list(choices), key="finding_edit_choice")
            selected = choices[label]
            existing_photos = selected.get("photos") or []
            if existing_photos:
                st.caption(f"Fotos del hallazgo ({len(existing_photos)})")
                photo_cols = st.columns(min(3, len(existing_photos)))
                for index, photo in enumerate(existing_photos):
                    try:
                        photo_cols[index % len(photo_cols)].image(photo_url(photo["path"]), caption=photo.get("name", "Foto"), width="stretch")
                    except (FindingsError, KeyError):
                        photo_cols[index % len(photo_cols)].caption("No se pudo cargar esta foto.")
            with st.form("edit_shared_finding"):
                edit_locality = st.selectbox("Localidad", LOCALITIES,
                                             index=LOCALITIES.index(selected.get("locality", LOCALITIES[0])))
                new_status = st.selectbox("Estado", STATUSES, index=STATUSES.index(selected.get("status", "Pendiente")))
                new_priority = st.selectbox("Prioridad", PRIORITIES, index=PRIORITIES.index(selected.get("priority", "Media")))
                new_notice = st.text_input("Aviso", value=selected.get("notice_number", ""))
                edit_asset = st.text_input("Activo relacionado", value=selected.get("asset_code") or "")
                edit_second_asset = st.text_input("Segundo activo", value=selected.get("second_asset_code") or "")
                edit_lat_col, edit_lon_col = st.columns(2)
                edit_latitude = edit_lat_col.text_input("Latitud del punto", value=str(selected.get("latitude", "")))
                edit_longitude = edit_lon_col.text_input("Longitud del punto", value=str(selected.get("longitude", "")))
                edit_lat2_col, edit_lon2_col = st.columns(2)
                edit_second_latitude = edit_lat2_col.text_input("Latitud del segundo punto", value=str(selected.get("second_latitude") or ""))
                edit_second_longitude = edit_lon2_col.text_input("Longitud del segundo punto", value=str(selected.get("second_longitude") or ""))
                new_desc = st.text_area("Descripción", value=selected.get("description", ""), max_chars=3000)
                more_photos = st.file_uploader("Agregar fotos (máximo 3 en total)", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="finding_edit_photos")
                save = st.form_submit_button("Guardar seguimiento", type="primary", width="stretch")
            if save:
                if len(existing_photos) + len(more_photos or []) > 3:
                    st.error("El hallazgo admite hasta 3 fotos en total.")
                else:
                    try:
                        matched_asset = lookup_asset(edit_locality, edit_asset) if edit_asset.strip() else None
                        matched_second = lookup_asset(edit_locality, edit_second_asset) if edit_second_asset.strip() else None
                        edit_lat = _edited_coordinate(edit_latitude, selected.get("latitude"), matched_asset, "latitude", "latitud")
                        edit_lon = _edited_coordinate(edit_longitude, selected.get("longitude"), matched_asset, "longitude", "longitud")
                        edit_lat2 = _edited_coordinate(edit_second_latitude, selected.get("second_latitude"), matched_second, "latitude", "latitud del segundo punto", optional=True)
                        edit_lon2 = _edited_coordinate(edit_second_longitude, selected.get("second_longitude"), matched_second, "longitude", "longitud del segundo punto", optional=True)
                        if (edit_lat2 is None) != (edit_lon2 is None):
                            raise FindingsError("Completa latitud y longitud del segundo punto, o deja ambas vacías.")
                        update_finding(selected, {"status": new_status, "priority": new_priority,
                                                  "locality": edit_locality, "notice_number": new_notice.strip(),
                                                  "asset_code": edit_asset.strip() or None,
                                                  "second_asset_code": edit_second_asset.strip() or None,
                                                  "latitude": edit_lat, "longitude": edit_lon,
                                                  "second_latitude": edit_lat2, "second_longitude": edit_lon2,
                                                  "description": new_desc.strip(),
                                                  "resolved_at": datetime.now(timezone.utc).isoformat() if new_status in ("Resuelto", "Cerrado") else None},
                                       more_photos, str(st.session_state.get("map_login_user", "usuario")))
                        st.success("Seguimiento actualizado y registrado en el historial.")
                        st.rerun()
                    except FindingsError as exc:
                        st.error(str(exc))
            try:
                with st.expander("Historial del caso"):
                    history = finding_history(str(selected["id"]))
                    if history:
                        for event in history:
                            snapshot = event.get("snapshot") or {}
                            when = str(event.get("changed_at", ""))[:16].replace("T", " ")
                            st.markdown(f"**{event.get('action')} · {when} · {event.get('changed_by', 'usuario')}**")
                            details = [snapshot.get(key) for key in ("status", "priority", "notice_number", "asset_code") if snapshot.get(key)]
                            if details:
                                st.caption(" · ".join(str(value) for value in details))
                    else:
                        st.caption("Todavía no hay cambios registrados.")
            except FindingsError as exc:
                st.caption(f"No se pudo leer el historial: {exc}")
