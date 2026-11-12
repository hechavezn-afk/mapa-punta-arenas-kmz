from __future__ import annotations

import hashlib
import hmac
import threading
import time
from secrets import token_urlsafe

import streamlit as st

# Keep the browser login alive for two hours after the last detected activity.
# The matching token is stored in localStorage so a page refresh can resume it.
IDLE_TIMEOUT_SECONDS = 2 * 60 * 60
CONFIG_USERNAME = "configedelmag"
_CONFIG_PASSWORD_SALT = bytes.fromhex("8b6d6e6043e32a197d28bd24e4183006")
_CONFIG_PASSWORD_HASH = bytes.fromhex("93d206f1a03596d9ac99a7dc18c059f186cc62a7ae12cae445f1a11eca6e6272")
_STATIC_USERS = {
    "edelmag4": (
        "EDELMAG4",
        bytes.fromhex("038ce63c3451be146f3dbd272ee8c2b6"),
        bytes.fromhex("5ca61eb8c4c3c5532e7466f4ee14f5c00a074c0b13d34928ad225a986f37c370"),
    ),
    "edelmag5": (
        "EDELMAG5",
        bytes.fromhex("c656eddfd19ecd357a6d767b2ddd0177"),
        bytes.fromhex("1c678abd8ea2bfd34b5a71e81ba57ebbbe68b07c3d6e3f417f10434914c0f2fd"),
    ),
    "edelmag6": (
        "EDELMAG6",
        bytes.fromhex("45d6795bc58aa03500aea1261aa43337"),
        bytes.fromhex("3163c9c8f9097c33ecf1cd3a1e65f56141f1aae64c41eac06d89a182201a3e1c"),
    ),
    "edelmag7": (
        "EDELMAG7",
        bytes.fromhex("e3998b3368b9bbee1c4ee3be682c5594"),
        bytes.fromhex("e5c858539a55f19f81a301a6a6f1015e15df98dd9b016d245692894cff69e552"),
    ),
}


def is_config_user(username: str | None) -> bool:
    return hmac.compare_digest((username or "").strip().casefold(), CONFIG_USERNAME)


def _verify_config_password(password: str) -> bool:
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _CONFIG_PASSWORD_SALT, 310_000
    )
    return hmac.compare_digest(candidate, _CONFIG_PASSWORD_HASH)


def _verify_static_user(username: str, password: str) -> tuple[bool, str | None]:
    account = _STATIC_USERS.get(username.strip().casefold())
    if account is None:
        return False, None
    canonical_name, salt, expected_hash = account
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return hmac.compare_digest(candidate, expected_hash), canonical_name


_BROWSER_SESSION = st.components.v2.component(
    name="map_login_browser_session",
    html="<span aria-hidden='true'></span>",
    js="""
    export default function ({ data, setStateValue }) {
      const storageKey = "map_login_session_token";
      let token = "";

      try {
        if (data?.clear) {
          localStorage.removeItem(storageKey);
          sessionStorage.removeItem(storageKey);
        } else if (data?.token) {
          localStorage.setItem(storageKey, data.token);
          sessionStorage.setItem(storageKey, data.token);
          token = data.token;
        } else {
          token = localStorage.getItem(storageKey) || sessionStorage.getItem(storageKey) || "";
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
        if (now - lastSent < 30000) return;
        lastSent = now;
        setStateValue("activity_ms", now);
      };
      const activityEvents = ["pointerdown", "keydown", "touchstart", "wheel"];
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
        "map_login_role",
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
            st.session_state["map_login_role"] = "config" if is_config_user(username) else "user"
            st.session_state["map_login_session_token"] = browser_token
            st.session_state["map_login_authenticated"] = True
            _keep_session_alive()
            return
        st.session_state["map_login_clear_browser_session"] = True
        st.rerun()

    with st.form("map_login_form"):
        st.title("Redes Edelmag Magallanes")
        st.caption("Inicia sesión para continuar")
        username = st.text_input("Cuenta")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Ingresar", use_container_width=True)

    if submitted:
        normalized_username = username.strip()
        if is_config_user(normalized_username):
            credentials_ok = _verify_config_password(password)
            normalized_username = CONFIG_USERNAME
        else:
            credentials_ok, static_username = _verify_static_user(normalized_username, password)
            if static_username:
                normalized_username = static_username
            else:
                expected = users.get(normalized_username)
                credentials_ok = expected is not None and hmac.compare_digest(password, expected)
        if not credentials_ok:
            st.error("Cuenta o contraseña incorrecta.")
        else:
            session_id = st.session_state.setdefault("map_login_session_id", token_urlsafe(24))
            session_token = token_urlsafe(32)
            if _claim(normalized_username, session_id, session_token):
                st.session_state["map_login_user"] = normalized_username
                st.session_state["map_login_role"] = "config" if is_config_user(normalized_username) else "user"
                st.session_state["map_login_session_token"] = session_token
                st.session_state["map_login_authenticated"] = True
                st.rerun()
            else:
                st.error("Prueba otra cuenta; este usuario está ocupado.")
    st.stop()
