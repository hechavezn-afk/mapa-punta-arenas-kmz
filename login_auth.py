from __future__ import annotations

import hmac
import threading
import time
from secrets import token_urlsafe

import streamlit as st

LEASE_SECONDS = 180


def _users() -> dict[str, str]:
    users: dict[str, str] = {}
    try:
        primary = st.secrets["map_login"]
        users[str(primary["username"])] = str(primary["password"])
    except (KeyError, FileNotFoundError):
        pass
    try:
        users.update({str(name): str(password) for name, password in st.secrets["map_users"].items()})
    except (KeyError, FileNotFoundError):
        pass
    return users


@st.cache_resource
def _registry() -> dict:
    return {"guard": threading.Lock(), "owners": {}}


def _claim(username: str, session_id: str) -> bool:
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        owner = registry["owners"].get(username)
        if owner and owner["expires"] > now and owner["session"] != session_id:
            return False
        registry["owners"][username] = {"session": session_id, "expires": now + LEASE_SECONDS}
        return True


def _release(username: str, session_id: str) -> None:
    registry = _registry()
    with registry["guard"]:
        owner = registry["owners"].get(username)
        if owner and owner["session"] == session_id:
            registry["owners"].pop(username, None)


def _heartbeat(username: str, session_id: str) -> bool:
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        owner = registry["owners"].get(username)
        if owner and owner["expires"] > now and owner["session"] != session_id:
            return False
        registry["owners"][username] = {"session": session_id, "expires": now + LEASE_SECONDS}
        return True


@st.fragment(run_every="30s")
def _keep_session_alive() -> None:
    username = st.session_state.get("map_login_user")
    session_id = st.session_state.get("map_login_session_id")
    if username and session_id and not _heartbeat(username, session_id):
        st.session_state.pop("map_login_authenticated", None)
        st.session_state.pop("map_login_user", None)
        st.error("Esta cuenta se está usando en otra sesión. Prueba con otra cuenta.")
        st.rerun()


def require_login() -> None:
    users = _users()
    if not users:
        st.error("El acceso aún no está configurado en Streamlit Secrets.")
        st.stop()

    if st.session_state.get("map_login_authenticated"):
        username = st.session_state.get("map_login_user", "")
        session_id = st.session_state.get("map_login_session_id", "")
        if username and session_id and _heartbeat(username, session_id):
            _keep_session_alive()
            return
        st.session_state.pop("map_login_authenticated", None)
        st.session_state.pop("map_login_user", None)
        st.error("La sesión de esta cuenta ya no está activa. Inicia sesión con otra cuenta.")

    with st.form("map_login_form"):
        st.title("Mapa Punta Arenas")
        st.caption("Inicia sesión para continuar")
        username = st.text_input("Cuenta")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar", use_container_width=True)

    if submitted:
        expected = users.get(username.strip())
        if expected is None or not hmac.compare_digest(password, expected):
            st.error("Cuenta o contraseña incorrecta.")
        else:
            session_id = st.session_state.setdefault("map_login_session_id", token_urlsafe(24))
            if _claim(username.strip(), session_id):
                st.session_state["map_login_user"] = username.strip()
                st.session_state["map_login_authenticated"] = True
                st.rerun()
            else:
                st.error("Prueba otra cuenta; este usuario está ocupado.")
    st.stop()
