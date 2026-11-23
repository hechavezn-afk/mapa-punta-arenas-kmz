"""Session-only backing store for the original Hallazgos interface in practice."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import streamlit as st


def requested() -> bool:
    return st.query_params.get("practice") == "1"


def enabled() -> bool:
    return bool(st.session_state.get("map_practice_active")) or requested()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data() -> dict:
    if "practice_data" not in st.session_state:
        now = _now()
        st.session_state.practice_data = {
            "findings": {
                "00000000-0000-4000-8000-000000000001": {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "notice_number": "PRACTICA-001",
                    "category": "Poda preventiva",
                    "locality": "Punta Arenas",
                    "status": "Pendiente",
                    "priority": "Media",
                    "latitude": -53.152,
                    "longitude": -70.8881,
                    "description": "Aviso ficticio para practicar el seguimiento.",
                    "created_at": now,
                    "updated_at": now,
                }
            },
            "history": {},
            "categories": {},
            "files": {},
        }
    return st.session_state.practice_data


def file_data(path: str) -> tuple[bytes, str]:
    return _data()["files"][path]


def request(method: str, path: str, payload, content_type: str):
    """Handle the finding actions locally. Unrecognized actions stop here."""
    resource = urlsplit(path)
    filters = parse_qs(resource.query)
    data = _data()
    if resource.path == "/rest/v1/maintenance_findings":
        findings = data["findings"]
        if method == "GET":
            locality = filters.get("locality", [""])[0].removeprefix("eq.")
            rows = [row for row in findings.values() if not locality or row["locality"] == locality]
            return deepcopy(sorted(rows, key=lambda row: row["updated_at"], reverse=True))
        if method in ("POST", "PATCH"):
            created = method == "POST"
            item_id = payload["id"] if created else filters["id"][0].removeprefix("eq.")
            now = _now()
            prior = {} if created else findings[item_id]
            row = {**prior, **deepcopy(payload), "updated_at": now}
            if created:
                row["created_at"] = now
            findings[item_id] = row
            data["history"].setdefault(item_id, []).insert(0, {
                "action": "Creado" if created else "Actualizado",
                "changed_at": now,
                "changed_by": "Práctica",
            })
            return [deepcopy(row)]
    if resource.path == "/rest/v1/maintenance_finding_history" and method == "GET":
        item_id = filters["finding_id"][0].removeprefix("eq.")
        return deepcopy(data["history"].get(item_id, []))
    if resource.path == "/rest/v1/maintenance_finding_categories":
        categories = data["categories"]
        if method == "GET":
            return deepcopy(sorted(categories.values(), key=lambda row: row["name"]))
        if method == "POST":
            categories[payload["name"]] = deepcopy(payload)
            return [deepcopy(payload)]
    if resource.path.startswith("/storage/v1/object/"):
        _, _, object_path = resource.path.removeprefix("/storage/v1/object/").partition("/")
        if method == "POST" and object_path and isinstance(payload, bytes):
            data["files"][unquote(object_path)] = (payload, content_type)
            return None
        if method == "DELETE" and object_path:
            data["files"].pop(unquote(object_path), None)
            return None
    raise ValueError("Esta operación no está disponible en el espacio de práctica.")
