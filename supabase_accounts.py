"""Supabase Auth setup and first-run account provisioning for the map app.

Administrative credentials are read only on the Streamlit server from secrets.
Generated passwords stay in the current server session and are downloadable as
a PDF; they are never written to source, a database, or application logs.
"""
from __future__ import annotations

from io import BytesIO
import json
import secrets
import string
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import streamlit as st


USER_ROSTER = [
    ("Luis Garrido", "lagarridob@grupocge.cl"),
    ("Sebastián Sotomayor", "sasotomayora@edelmag.cl"),
    ("Gerardo Castro", "gcastro@edelmag.cl"),
    ("Víctor Levicoy", "vmlevicoyo@edelmag.cl"),
    ("Cristian Miranda", "cemirandab@edelmag.cl"),
    ("Sergio Villegas", "svillegas@edelmag.cl"),
    ("Sergio Alvarado", "spalvaradob@edelmag.cl"),
    ("Luis Estrada", "lestradao@edelmag.cl"),
    ("Danilo Rivas", "darivasc@edelmag.cl"),
    ("Nicole Ojeda", "nojedab@edelmag.cl"),
    ("Andrés Montiel", "aamontielp@edelmag.cl"),
    ("César Chavez", "cchavez@edelmag.cl"),
    ("Luis Ojeda", "lojeda@edelmag.cl"),
]
HECTOR_ACCOUNT = ("Héctor", "HECTOR", "ingenierohcn@grupocge.cl")


class SupabaseSetupError(RuntimeError):
    pass


def auth_config() -> tuple[str, str, str]:
    """Return URL, publishable key, and server-only secret key from Streamlit."""
    try:
        public = dict(st.secrets["supabase"])
        private = dict(st.secrets["security"])
        url = str(public["url"]).strip().rstrip("/")
        public_key = str(public.get("key") or public.get("publishable_key") or public.get("anon_key") or "").strip()
        secret_key = str(private.get("supabase_secret_key") or private.get("secret_key") or private.get("service_role_key") or "").strip()
    except Exception:
        raise SupabaseSetupError(
            "Configura [supabase] url/key y [security] supabase_secret_key en Streamlit Secrets."
        ) from None
    if not url.startswith("https://") or not public_key or not secret_key.startswith("sb_secret_"):
        raise SupabaseSetupError("Las claves o la URL de Supabase no tienen el formato esperado.")
    return url, public_key, secret_key


def account_roster() -> dict[str, dict[str, str]]:
    rows = [("HECTOR", HECTOR_ACCOUNT[0], HECTOR_ACCOUNT[2])]
    rows.extend((email.split("@", 1)[0].upper(), name, email) for name, email in USER_ROSTER)
    return {alias.upper(): {"name": name, "email": email} for alias, name, email in rows}


def account_for_identity(identity: str) -> tuple[str, dict[str, str]] | None:
    raw = (identity or "").strip().casefold()
    for alias, account in account_roster().items():
        if raw in (alias.casefold(), account["email"].casefold()):
            return alias, account
    return None


def _admin_request(method: str, path: str, payload=None):
    url, _, key = auth_config()
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            content = response.read()
        return json.loads(content.decode("utf-8")) if content else {}
    except HTTPError as exc:
        # Do not echo arbitrary upstream text: it can contain personal data.
        raise SupabaseSetupError(f"Supabase rechazó la operación (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise SupabaseSetupError("No se pudo conectar con Supabase. Revisa URL, claves y conexión.") from None


def _existing_emails() -> set[str]:
    users = set()
    # Supabase Auth admin API paginates at 50 by default; iterate safely.
    for page in range(1, 101):
        result = _admin_request("GET", f"/auth/v1/admin/users?page={page}&per_page=100")
        items = result.get("users", []) if isinstance(result, dict) else []
        users.update(str(item.get("email", "")).casefold() for item in items if item.get("email"))
        if len(items) < 100:
            break
    return users


def _new_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%*-_+"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value)
                and any(c.isdigit() for c in value) and any(c in "!@#$%*-_+" for c in value)):
            return value


def provision_accounts(hector_password: str) -> tuple[list[dict[str, str]], list[str]]:
    """Create absent Auth users; leave existing accounts and passwords untouched."""
    if not hector_password or len(hector_password) < 8:
        raise SupabaseSetupError("Escribe la clave inicial de HECTOR (mínimo 8 caracteres).")
    existing = _existing_emails()
    candidates = [(HECTOR_ACCOUNT[0], HECTOR_ACCOUNT[1], HECTOR_ACCOUNT[2], hector_password)]
    candidates += [
        (name, email.split("@", 1)[0].upper(), email, _new_password())
        for name, email in USER_ROSTER
    ]
    created, skipped = [], []
    for name, alias, email, password in candidates:
        if email.casefold() in existing:
            skipped.append(f"{alias} ({email}) ya existe; no se modificó su clave.")
            continue
        _admin_request("POST", "/auth/v1/admin/users", {
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"full_name": name, "account_alias": alias},
        })
        created.append({"name": name, "alias": alias, "email": email, "password": password})
        existing.add(email.casefold())
    return created, skipped


def _credentials_pdf(rows: list[dict[str, str]]) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    except ImportError:
        raise SupabaseSetupError("Falta ReportLab. La app debe desplegarse con requirements.txt actualizado.") from None
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=0.55*inch, leftMargin=0.55*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch, title="Cuentas Redes Edelmag")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="BrandTitle", parent=styles["Title"], textColor=colors.HexColor("#174b78"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8, leading=10))
    esc = lambda x: str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    data = [["Nombre", "Cuenta", "Correo", "Clave inicial"]]
    data += [[Paragraph(esc(r["name"]), styles["Small"]), Paragraph(esc(r["alias"]), styles["Small"]),
              Paragraph(esc(r["email"]), styles["Small"]), Paragraph(esc(r["password"]), styles["Small"])] for r in rows]
    table = Table(data, colWidths=[1.35*inch, 1.15*inch, 2.45*inch, 2.0*inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#174b78")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 8), ("GRID", (0,0), (-1,-1), .35, colors.HexColor("#ccd6df")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f2f6f9")]),
        ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6), ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    doc.build([Paragraph("Redes Edelmag Magallanes", styles["BrandTitle"]),
               Paragraph("Cuentas creadas · claves iniciales", styles["Heading2"]),
               Paragraph("Entrega cada clave de forma individual. Al iniciar sesión, cada usuario deberá configurar su segundo factor.", styles["BodyText"]),
               Spacer(1, 12), table, Spacer(1, 12),
               Paragraph("Las claves se muestran solo en este documento generado desde la sesión de administración. Guárdalo en un lugar seguro y elimina las copias temporales.", styles["Small"])])
    return buffer.getvalue()


def render_account_admin() -> None:
    st.title("Administrar cuentas de acceso")
    st.caption("La clave privada se usa solo en el servidor. Las cuentas existentes no se alteran.")
    st.info("Para que funcione el segundo factor por correo, habilita Email en Supabase Auth y configura la plantilla de acceso para incluir el token {{ .Token }}. Authenticator queda disponible mediante TOTP.")
    if st.session_state.get("map_provisioned_credentials"):
        rows = st.session_state["map_provisioned_credentials"]
        st.success(f"Se crearon {len(rows)} cuentas nuevas.")
        st.download_button("Descargar PDF de claves iniciales", _credentials_pdf(rows),
                           file_name="Cuentas_Redes_Edelmag.pdf", mime="application/pdf", type="primary")
        if st.button("Borrar claves de esta sesión"):
            st.session_state.pop("map_provisioned_credentials", None)
            st.rerun()
    skipped = st.session_state.get("map_provision_skipped", [])
    for message in skipped:
        st.info(message)
    with st.form("map_provision_accounts"):
        st.write(f"Se intentará crear HECTOR y {len(USER_ROSTER)} cuentas de usuarios. Las existentes se omiten.")
        hector_password = st.text_input("Clave inicial de HECTOR", type="password", help="La clave se usa solo al crear la cuenta y no queda guardada en el código.")
        confirm = st.checkbox("Confirmo crear las cuentas que aún no existen", value=False)
        submitted = st.form_submit_button("Crear cuentas faltantes", type="primary", disabled=not confirm or not hector_password)
    if submitted:
        try:
            created, skipped = provision_accounts(hector_password)
            st.session_state["map_provisioned_credentials"] = created
            st.session_state["map_provision_skipped"] = skipped
            if not created:
                st.warning("No se crearon cuentas nuevas; todas ya existen. No se cambiaron sus claves.")
            st.rerun()
        except SupabaseSetupError as exc:
            st.error(str(exc))
    st.divider()
    st.subheader("Siguiente paso")
    st.write("Para que estas cuentas ingresen al mapa, el despliegue debe usar autenticación Supabase. La app conserva temporalmente el acceso actual hasta completar ese cambio.")
    st.caption("Ruta segura: volver a la app")
    if st.button("Volver al mapa"):
        st.query_params.clear()
        st.rerun()
