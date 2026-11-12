"""Server-side shared maintenance findings for the Redes Edelmag map."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import streamlit as st

from supabase_accounts import auth_config, SupabaseSetupError

LOCALITIES = ("Punta Arenas", "Puerto Natales", "Porvenir", "Puerto Williams")
STATUSES = ("Pendiente", "En revisión", "Resuelto", "Cerrado")
PRIORITIES = ("Baja", "Media", "Alta", "Urgente")
CATEGORIES = ("Poda preventiva", "Poste desaplomado", "Estructura dañada", "Choque de poste",
              "Retensado de línea", "Tirante o protección", "Equipo defectuoso", "Otro")
BUCKET = "maintenance-finding-photos"


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
        rows = [row for row in rows if row.get("status") in ("Pendiente", "En revisión")]
    elif status_filter != "Todos":
        rows = [row for row in rows if row.get("status") == status_filter]
    return rows


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


def _heatmap(rows):
    import pydeck as pdk
    if not rows:
        st.info("No hay hallazgos para los filtros elegidos.")
        return
    points = [{"latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
               "weight": {"Baja": 1, "Media": 2, "Alta": 3, "Urgente": 4}.get(row.get("priority"), 2),
               "label": f"{row.get('category','Hallazgo')} · {row.get('asset_code') or row.get('notice_number') or row['id'][:8]}"}
              for row in rows]
    center_lat = sum(p["latitude"] for p in points) / len(points)
    center_lon = sum(p["longitude"] for p in points) / len(points)
    layers = [
        pdk.Layer("HeatmapLayer", data=points, get_position="[longitude, latitude]", get_weight="weight",
                  radiusPixels=55, intensity=1.2, threshold=0.03, opacity=0.72),
        pdk.Layer("ScatterplotLayer", data=points, get_position="[longitude, latitude]", get_radius=42,
                  get_fill_color="[220, 70, 45, 210]", get_line_color="[255,255,255,240]",
                  line_width_min_pixels=1, pickable=True),
    ]
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(
        latitude=center_lat, longitude=center_lon, zoom=10 if len(rows) > 8 else 14,
        pitch=0, bearing=0), tooltip={"text": "{label}"},
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"), use_container_width=True)


def render_findings_page() -> None:
    st.title("Hallazgos de mantenimiento")
    st.caption("Registro compartido entre usuarios · Filtra por localidad, registra aviso y prioridad, y actualiza el seguimiento.")
    if st.button("← Volver al mapa"):
        st.query_params.clear()
        st.rerun()

    filter_cols = st.columns((1.4, 1.2, 1))
    locality = filter_cols[0].selectbox("Localidad", ("Todas",) + LOCALITIES, key="finding_locality")
    status_filter = filter_cols[1].selectbox("Estado", ("Abiertos", "Todos") + STATUSES, key="finding_status")
    if filter_cols[2].button("Actualizar lista", use_container_width=True):
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
        st.subheader("Mapa de concentración")
        _heatmap(rows)
        if rows:
            display = [{"Localidad": r.get("locality"), "Aviso": r.get("notice_number"),
                        "Activo": " ↔ ".join(x for x in (r.get("asset_code"), r.get("second_asset_code")) if x),
                        "Hallazgo": r.get("category"), "Prioridad": r.get("priority"), "Estado": r.get("status"),
                        "Actualizado": str(r.get("updated_at", ""))[:16].replace("T", " ")}
                       for r in rows]
            st.dataframe(display, use_container_width=True, hide_index=True)
    with form_col:
        st.subheader("Crear hallazgo")
        with st.form("create_shared_finding", clear_on_submit=True):
            f_locality = st.selectbox("Localidad", LOCALITIES, index=LOCALITIES.index(locality) if locality in LOCALITIES else 0)
            notice = st.text_input("N.º de aviso", placeholder="Número de aviso")
            category = st.selectbox("Tipo", CATEGORIES)
            priority = st.select_slider("Prioridad", options=PRIORITIES, value="Media")
            asset_code = st.text_input("Poste o activo", placeholder="Ej. 1-0020-020")
            second_asset = st.text_input("Segundo poste (si aplica)", placeholder="Para tramo entre dos activos")
            description = st.text_area("Descripción / acción requerida", max_chars=3000)
            lat_col, lon_col = st.columns(2)
            latitude = lat_col.number_input("Latitud", min_value=-90.0, max_value=90.0, value=-53.16, format="%.7f")
            longitude = lon_col.number_input("Longitud", min_value=-180.0, max_value=180.0, value=-70.90, format="%.7f")
            photos = st.file_uploader("Fotos (máximo 3)", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="finding_create_photos")
            submit = st.form_submit_button("Guardar hallazgo compartido", type="primary", use_container_width=True)
        if submit:
            if len(photos or []) > 3:
                st.error("Puedes adjuntar hasta 3 fotos por hallazgo.")
            else:
                actor = str(st.session_state.get("map_login_user", "usuario"))
                payload = {"locality": f_locality, "notice_number": notice.strip(), "category": category,
                           "description": description.strip(), "priority": priority, "status": "Pendiente",
                           "asset_code": asset_code.strip() or None, "second_asset_code": second_asset.strip() or None,
                           "latitude": latitude, "longitude": longitude, "created_by": actor, "updated_by": actor}
                try:
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
            choices = {f"{r.get('category')} · {r.get('asset_code') or 'sin activo'} · {r.get('locality')} · {r.get('status')} · {str(r['id'])[:8]}": r for r in rows}
            label = st.selectbox("Hallazgo", list(choices), key="finding_edit_choice")
            selected = choices[label]
            existing_photos = selected.get("photos") or []
            if existing_photos:
                st.caption(f"Fotos del hallazgo ({len(existing_photos)})")
                photo_cols = st.columns(min(3, len(existing_photos)))
                for index, photo in enumerate(existing_photos):
                    try:
                        photo_cols[index % len(photo_cols)].image(photo_url(photo["path"]), caption=photo.get("name", "Foto"), use_container_width=True)
                    except (FindingsError, KeyError):
                        photo_cols[index % len(photo_cols)].caption("No se pudo cargar esta foto.")
            with st.form("edit_shared_finding"):
                new_status = st.selectbox("Estado", STATUSES, index=STATUSES.index(selected.get("status", "Pendiente")))
                new_priority = st.selectbox("Prioridad", PRIORITIES, index=PRIORITIES.index(selected.get("priority", "Media")))
                new_notice = st.text_input("Aviso", value=selected.get("notice_number", ""))
                new_desc = st.text_area("Descripción", value=selected.get("description", ""), max_chars=3000)
                more_photos = st.file_uploader("Agregar fotos (máximo 3 por carga)", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="finding_edit_photos")
                save = st.form_submit_button("Guardar seguimiento", type="primary", use_container_width=True)
            if save:
                if len(more_photos or []) > 3:
                    st.error("Puedes adjuntar hasta 3 fotos en una carga.")
                else:
                    try:
                        update_finding(selected, {"status": new_status, "priority": new_priority,
                                                  "notice_number": new_notice.strip(), "description": new_desc.strip(),
                                                  "resolved_at": datetime.now(timezone.utc).isoformat() if new_status in ("Resuelto", "Cerrado") else None},
                                       more_photos, str(st.session_state.get("map_login_user", "usuario")))
                        st.success("Seguimiento actualizado y registrado en el historial.")
                        st.rerun()
                    except FindingsError as exc:
                        st.error(str(exc))
