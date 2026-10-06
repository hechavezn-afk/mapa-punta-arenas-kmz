from __future__ import annotations

import hmac
import threading
import time
from secrets import token_urlsafe

import streamlit as st

IDLE_TIMEOUT_SECONDS = 15 * 60


_BROWSER_SESSION = st.components.v2.component(
    name="map_login_browser_session",
    html="<span aria-hidden='true'></span>",
    js="""
    export default function ({ data, setStateValue }) {
      const storageKey = "map_login_session_token";
      let token = "";

      try {
        if (data?.clear) {
          sessionStorage.removeItem(storageKey);
        } else if (data?.token) {
          sessionStorage.setItem(storageKey, data.token);
          token = data.token;
        } else {
          token = sessionStorage.getItem(storageKey) || "";
        }
      } catch (_) {
        token = "";
      }

      setStateValue("browser_token", token);
      setStateValue("ready", true);

      let lastSent = 0;
      const listeners = [];
      const bind = (target, eventName, handler, options = true) => {
        target.addEventListener(eventName, handler, options);
        listeners.push([target, eventName, handler, options]);
      };
      const reportActivity = (event) => {
        if (!event.isTrusted || document.visibilityState !== "visible") return;
        const now = Date.now();
        if (now - lastSent < 1500) return;
        lastSent = now;
        setStateValue("activity_ms", now);
      };
      const activityEvents = ["pointerdown", "pointermove", "keydown", "touchstart", "wheel", "scroll"];
      for (const eventName of activityEvents) {
        bind(document, eventName, reportActivity);
      }

      const boundDocuments = new WeakSet();
      const bindFrame = (frame) => {
        const attach = () => {
          try {
            const frameDocument = frame.contentDocument;
            if (!frameDocument || boundDocuments.has(frameDocument)) return;
            boundDocuments.add(frameDocument);
            for (const eventName of activityEvents) {
              bind(frameDocument, eventName, reportActivity);
            }
          } catch (_) {}
        };
        bind(frame, "load", attach, false);
        attach();
      };
      const bindFrames = () => {
        for (const frame of document.querySelectorAll("iframe")) bindFrame(frame);
      };
      bindFrames();
      const observer = new MutationObserver(bindFrames);
      observer.observe(document.documentElement, { childList: true, subtree: true });

      return () => {
        observer.disconnect();
        for (const [target, eventName, handler, options] of listeners) {
          target.removeEventListener(eventName, handler, options);
        }
      };
    }
    """,
)


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


def _claim(username: str, session_id: str, token: str) -> bool:
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        owner = registry["owners"].get(username)
        if owner and now - owner["last_activity"] < IDLE_TIMEOUT_SECONDS:
            if owner["token"] != token:
                return False
            owner["session"] = session_id
            return True
        registry["owners"][username] = {
            "session": session_id,
            "token": token,
            "last_activity": now,
            "activity_ms": 0,
        }
        return True


def _resume(token: str, session_id: str) -> str | None:
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        for username, owner in list(registry["owners"].items()):
            if owner["token"] != token:
                continue
            if now - owner["last_activity"] >= IDLE_TIMEOUT_SECONDS:
                registry["owners"].pop(username, None)
                return None
            owner["session"] = session_id
            return username
    return None


def _heartbeat(username: str, session_id: str, token: str, activity_ms: int = 0) -> bool:
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        owner = registry["owners"].get(username)
        if not owner or owner["session"] != session_id or owner["token"] != token:
            return False
        if now - owner["last_activity"] >= IDLE_TIMEOUT_SECONDS:
            registry["owners"].pop(username, None)
            return False
        if activity_ms and activity_ms > owner["activity_ms"]:
            owner["activity_ms"] = activity_ms
            owner["last_activity"] = now
        return True


def _bridge(token: str | None = None, *, clear: bool = False):
    return _BROWSER_SESSION(
        data={"token": token or "", "clear": clear},
        default={"browser_token": "", "ready": False, "activity_ms": 0},
        key="map_auth_browser_session",
        on_browser_token_change=lambda: None,
        on_ready_change=lambda: None,
        on_activity_ms_change=lambda: None,
    )


def _clear_authentication() -> None:
    for key in (
        "map_login_authenticated",
        "map_login_user",
        "map_login_session_id",
        "map_login_session_token",
    ):
        st.session_state.pop(key, None)


@st.fragment(run_every="30s")
def _keep_session_alive() -> None:
    username = st.session_state.get("map_login_user")
    session_id = st.session_state.get("map_login_session_id")
    token = st.session_state.get("map_login_session_token")
    bridge_state = st.session_state.get("map_auth_browser_session", {})
    activity_ms = int(bridge_state.get("activity_ms") or 0)
    if username and session_id and token and not _heartbeat(username, session_id, token, activity_ms):
        _clear_authentication()
        st.session_state["map_login_clear_browser_session"] = True
        st.rerun()


def require_login() -> None:
    users = _users()
    if not users:
        st.error("El acceso aún no está configurado en Streamlit Secrets.")
        st.stop()

    clear_browser = bool(st.session_state.pop("map_login_clear_browser_session", False))
    expected_token = st.session_state.get("map_login_session_token")
    bridge = _bridge(expected_token if st.session_state.get("map_login_authenticated") else None, clear=clear_browser)
    if not getattr(bridge, "ready", False):
        st.info("Restaurando sesión…")
        st.stop()

    browser_token = "" if clear_browser else (getattr(bridge, "browser_token", "") or "")
    activity_ms = int(getattr(bridge, "activity_ms", 0) or 0)

    if st.session_state.get("map_login_authenticated"):
        username = st.session_state.get("map_login_user", "")
        session_id = st.session_state.get("map_login_session_id", "")
        token = st.session_state.get("map_login_session_token", "")
        if browser_token != token:
            st.info("Restaurando sesión…")
            st.stop()
        if username and session_id and token and _heartbeat(username, session_id, token, activity_ms):
            _keep_session_alive()
            return
        _clear_authentication()
        st.session_state["map_login_clear_browser_session"] = True
        st.rerun()

    if browser_token and not clear_browser:
        username = _resume(browser_token, st.session_state.setdefault("map_login_session_id", token_urlsafe(24)))
        if username:
            st.session_state["map_login_user"] = username
            st.session_state["map_login_session_token"] = browser_token
            st.session_state["map_login_authenticated"] = True
            _keep_session_alive()
            return
        st.session_state["map_login_clear_browser_session"] = True
        st.rerun()

    with st.form("map_login_form"):
        st.title("Mapa Punta Arenas")
        st.caption("Inicia sesión para continuar")
        username = st.text_input("Cuenta")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar", use_container_width=True)

    if submitted:
        normalized_username = username.strip()
        expected = users.get(normalized_username)
        if expected is None or not hmac.compare_digest(password, expected):
            st.error("Cuenta o contraseña incorrecta.")
        else:
            session_id = st.session_state.setdefault("map_login_session_id", token_urlsafe(24))
            session_token = token_urlsafe(32)
            if _claim(normalized_username, session_id, session_token):
                st.session_state["map_login_user"] = normalized_username
                st.session_state["map_login_session_token"] = session_token
                st.session_state["map_login_authenticated"] = True
                st.rerun()
            else:
                st.error("Prueba otra cuenta; este usuario está ocupado.")
    st.stop()
