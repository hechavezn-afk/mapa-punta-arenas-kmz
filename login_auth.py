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
        "map_supabase_auth_client",
        "map_supabase_pending_identity",
        "map_supabase_pending_email",
        "map_supabase_mfa_enrollment",
    ):
        st.session_state.pop(key, None)


def _supabase_auth_ready() -> bool:
    try:
        from supabase_accounts import auth_config
        auth_config()
        return True
    except Exception:
        return False


def _finish_supabase_login(alias: str) -> None:
    session_id = st.session_state.setdefault("map_login_session_id", token_urlsafe(24))
    session_token = token_urlsafe(32)
    if not _claim(alias, session_id, session_token):
        st.error("Esta cuenta ya tiene una sesión activa. Intenta nuevamente más tarde.")
        return
    st.session_state["map_login_user"] = alias
    st.session_state["map_login_role"] = "user"
    st.session_state["map_login_session_token"] = session_token
    st.session_state["map_login_authenticated"] = True
    st.session_state.pop("map_supabase_pending_identity", None)
    st.session_state.pop("map_supabase_pending_email", None)
    st.session_state.pop("map_supabase_mfa_enrollment", None)
    st.rerun()


def _supabase_second_factor() -> None:
    """Require either a verified Authenticator factor or a one-time email code."""
    from supabase_accounts import SupabaseSetupError

    client = st.session_state.get("map_supabase_auth_client")
    alias = st.session_state.get("map_supabase_pending_identity")
    email = st.session_state.get("map_supabase_pending_email")
    if not client or not alias or not email:
        return
    st.subheader("Verificación en dos pasos")
    try:
        factors = client.auth.mfa.list_factors()
        verified = [f for f in (getattr(factors, "totp", None) or []) if getattr(f, "status", "") == "verified"]
    except Exception:
        st.error("No se pudo consultar el segundo factor de Supabase. Intenta recargar la página.")
        return

    options = ["Aplicación Authenticator", "Código temporal por correo"]
    method = st.radio("Elige cómo verificar este ingreso", options, horizontal=True, key="map_mfa_method")
    if method == "Código temporal por correo":
        if st.button("Enviar código al correo", key="map_email_otp_send"):
            try:
                client.auth.sign_in_with_otp({"email": email, "options": {"should_create_user": False}})
                st.session_state["map_email_otp_sent"] = True
                st.success("Código enviado. Revisa tu correo, incluida la carpeta de correo no deseado.")
            except Exception:
                st.error("No se pudo enviar el código. Comprueba que el correo esté habilitado en Supabase Auth.")
        if st.session_state.get("map_email_otp_sent"):
            with st.form("map_email_otp_form", clear_on_submit=True):
                code = st.text_input("Código de 6 dígitos", max_chars=12)
                verify = st.form_submit_button("Verificar e ingresar", type="primary")
            if verify:
                try:
                    client.auth.verify_otp({"email": email, "token": code.strip(), "type": "email"})
                    st.session_state.pop("map_email_otp_sent", None)
                    _finish_supabase_login(alias)
                except Exception:
                    st.error("El código no es válido o ya venció. Solicita uno nuevo.")
        return

    if verified:
        factor_id = str(verified[0].id)
    else:
        enrollment = st.session_state.get("map_supabase_mfa_enrollment")
        if not enrollment:
            try:
                enrolled = client.auth.mfa.enroll({"factor_type": "totp", "friendly_name": "Redes Edelmag"})
                totp = getattr(enrolled, "totp", None)
                if not totp or not getattr(totp, "qr_code", ""):
                    raise ValueError("Missing QR")
                enrollment = {"factor_id": str(enrolled.id), "qr_code": str(totp.qr_code),
                              "secret": str(getattr(totp, "secret", ""))}
                st.session_state["map_supabase_mfa_enrollment"] = enrollment
            except Exception:
                st.error("No se pudo iniciar la configuración de Authenticator. Puedes usar el código por correo.")
                return
        st.write("Escanea este QR con Microsoft Authenticator, Google Authenticator u otra app TOTP.")
        qr = enrollment["qr_code"]
        if qr.startswith("data:image/"):
            st.markdown(f'<img alt="QR para configurar Authenticator" width="190" src="{qr}">', unsafe_allow_html=True)
        if enrollment.get("secret"):
            st.caption("Si no puedes escanear: " + enrollment["secret"])
        factor_id = enrollment["factor_id"]

    with st.form("map_totp_form", clear_on_submit=True):
        code = st.text_input("Código de Authenticator", max_chars=12)
        verify = st.form_submit_button("Verificar e ingresar", type="primary")
    if verify:
        try:
            client.auth.mfa.challenge_and_verify({"factor_id": factor_id, "code": code.strip()})
            _finish_supabase_login(alias)
        except Exception:
            st.error("El código no es válido o ya venció. Revisa la hora automática del teléfono e inténtalo de nuevo.")
    if st.button("Cancelar e iniciar con otra cuenta", key="map_mfa_cancel"):
        try:
            client.auth.sign_out()
        except Exception:
            pass
        _clear_authentication()
        st.rerun()


def _try_supabase_login(identity: str, password: str) -> bool:
    """Start password verification for the explicitly configured roster only."""
    from supabase_accounts import account_for_identity, auth_config

    account = account_for_identity(identity)
    if not account:
        return False
    alias, details = account
    try:
        from supabase import ClientOptions, create_client
        url, public_key, _ = auth_config()
        client = create_client(url, public_key, options=ClientOptions(auto_refresh_token=True, persist_session=False))
        client.auth.sign_in_with_password({"email": details["email"], "password": password})
        verified = client.auth.get_user()
        if not verified or not verified.user or str(verified.user.email).casefold() != details["email"].casefold():
            raise ValueError("Identity mismatch")
    except Exception:
        st.error("Cuenta o contraseña incorrecta, o el servicio de autenticación no está disponible.")
        return True
    st.session_state["map_supabase_auth_client"] = client
    st.session_state["map_supabase_pending_identity"] = alias
    st.session_state["map_supabase_pending_email"] = details["email"]
    st.session_state.pop("map_email_otp_sent", None)
    st.rerun()
    return True


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

    if st.session_state.get("map_supabase_pending_identity"):
        _supabase_second_factor()
        st.stop()

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
            if _supabase_auth_ready() and _try_supabase_login(normalized_username, password):
                st.stop()
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
