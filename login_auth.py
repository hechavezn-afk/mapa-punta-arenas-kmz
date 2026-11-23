from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import smtplib
import ssl
import threading
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from secrets import randbelow, token_bytes, token_urlsafe
from zoneinfo import ZoneInfo

import streamlit as st

# Most browser sessions expire after five idle hours. HECTOR's signed browser token
# is remembered automatically and expires at midnight in the Punta Arenas timezone.
IDLE_TIMEOUT_SECONDS = 5 * 60 * 60
HECTOR_SESSION_TIMEOUT_SECONDS = 24 * 60 * 60
HECTOR_TIMEZONE = ZoneInfo("America/Punta_Arenas")
MFA_ENTRY_WINDOW_SECONDS = 3 * 60
MFA_ISSUER = "Redes Edelmag Magallanes"
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


def _is_hector(username: str | None) -> bool:
    return (username or "").strip().casefold() == "hector"


def _session_timeout(username: str) -> int:
    return HECTOR_SESSION_TIMEOUT_SECONDS if _is_hector(username) else IDLE_TIMEOUT_SECONDS


def _session_signing_key() -> bytes | None:
    """Derive a stable server-only key so HECTOR's day token survives app restarts."""
    try:
        from supabase_accounts import auth_config

        _url, _public_key, secret_key = auth_config()
    except Exception:
        return None
    return hmac.digest(secret_key.encode("utf-8"), b"redes-edelmag/hector-session/v1", "sha256")


def _issue_session_token(username: str) -> str:
    if not _is_hector(username):
        return token_urlsafe(32)
    key = _session_signing_key()
    if not key:
        return token_urlsafe(32)
    local_now = datetime.now(HECTOR_TIMEZONE)
    expires_at = int(local_now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())
    payload = f"HECTOR|{expires_at}|{token_urlsafe(24)}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.digest(key, encoded.encode("ascii"), "sha256")
    signature_text = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"h1.{encoded}.{signature_text}"


def _verified_hector_token(token: str) -> str | None:
    parts = str(token or "").split(".")
    if len(parts) != 3 or parts[0] != "h1":
        return None
    key = _session_signing_key()
    if not key:
        return None
    encoded, supplied_signature = parts[1], parts[2]
    try:
        expected = hmac.digest(key, encoded.encode("ascii"), "sha256")
        padding = "=" * (-len(supplied_signature) % 4)
        supplied = base64.urlsafe_b64decode(supplied_signature + padding)
        if not hmac.compare_digest(expected, supplied):
            return None
        payload_padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + payload_padding).decode("utf-8")
        username, raw_expiry, nonce = payload.split("|", 2)
        expires_at = int(raw_expiry)
        current_local = datetime.now(HECTOR_TIMEZONE)
        end_of_day = int(current_local.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())
        if (username != "HECTOR" or not nonce or expires_at <= int(time.time())
                or expires_at > end_of_day):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return "HECTOR"


_BROWSER_SESSION = st.components.v2.component(
    name="map_login_browser_session",
    html="<span aria-hidden='true'></span>",
    js="""
    export default function ({ data, setStateValue }) {
      const storageKey = "map_login_session_token";
      const rememberKey = storageKey + "_persistent";
      const deviceKey = "map_login_device_id";
      let token = "";
      let remembered = false;
      let deviceId = "";
      let deviceLabel = "Navegador";

      try {
        if (data?.clear) {
          localStorage.removeItem(storageKey);
          localStorage.removeItem(rememberKey);
          sessionStorage.removeItem(storageKey);
        } else if (data?.token) {
          remembered = Boolean(data.remember);
          localStorage.setItem(rememberKey, remembered ? "1" : "0");
          if (remembered) localStorage.setItem(storageKey, data.token);
          else localStorage.removeItem(storageKey);
          sessionStorage.setItem(storageKey, data.token);
          token = data.token;
        } else {
          const persistentToken = localStorage.getItem(storageKey);
          token = persistentToken || sessionStorage.getItem(storageKey) || "";
          remembered = Boolean(persistentToken);
        }
      } catch (_) {
        token = "";
      }

      try {
        deviceId = localStorage.getItem(deviceKey) || "";
        if (!deviceId) {
          deviceId = globalThis.crypto?.randomUUID?.() || (Date.now().toString(36) + Math.random().toString(36).slice(2));
          localStorage.setItem(deviceKey, deviceId);
        }
      } catch (_) {
        try {
          deviceId = sessionStorage.getItem(deviceKey) || (Date.now().toString(36) + Math.random().toString(36).slice(2));
          sessionStorage.setItem(deviceKey, deviceId);
        } catch (_) { deviceId = "device-" + Date.now().toString(36); }
      }
      const ua = navigator.userAgent || "";
      const platform = navigator.userAgentData?.platform || navigator.platform || "Dispositivo";
      const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /CriOS\//.test(ua) ? "Chrome" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Navegador";
      const os = /Android/i.test(ua) ? "Android" : /iPhone|iPad|iPod/i.test(ua) ? "iOS" : /Windows/i.test(platform + ua) ? "Windows" : /Mac/i.test(platform + ua) ? "Mac" : /Linux/i.test(platform + ua) ? "Linux" : "Dispositivo";
      deviceLabel = `${os} · ${browser}`;

      setStateValue("remembered", remembered);
      setStateValue("browser_token", token);
      setStateValue("ready", true);
      setStateValue("device_id", deviceId);
      setStateValue("device_label", deviceLabel);

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
      const boundFrames = new WeakSet();
      const bindFrame = (frame) => {
        if (boundFrames.has(frame)) return;
        boundFrames.add(frame);
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
    return {"guard": threading.Lock(), "owners": {}, "revoked_tokens": set()}


def _session_limit(username: str) -> int:
    return 3 if username.strip().casefold() == "hector" else 1


def _sessions_for(registry: dict, username: str) -> dict:
    """Return a per-user token map, upgrading any in-memory single-session entry."""
    owners = registry["owners"]
    key = next((name for name in owners if name.casefold() == username.casefold()), username)
    owner = owners.get(key)
    if owner is None:
        owner = {"sessions": {}}
        owners[key] = owner
    elif "sessions" not in owner:
        owner = {"sessions": {owner["token"]: owner}}
        owners[key] = owner
    return owner["sessions"]


def _claim(username: str, session_id: str, token: str, device_id: str = "", device_label: str = "") -> bool:
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        revoked_tokens = registry.setdefault("revoked_tokens", set())
        if token in revoked_tokens:
            return False
        sessions = _sessions_for(registry, username)
        for old_token, owner in list(sessions.items()):
            if now - owner["last_activity"] >= _session_timeout(username):
                sessions.pop(old_token, None)
                continue
            if _is_hector(username) and old_token.startswith("h1.") and not _verified_hector_token(old_token):
                sessions.pop(old_token, None)
        existing = next(
            ((old_token, owner) for old_token, owner in sessions.items() if owner["session"] == session_id),
            None,
        )
        if existing:
            sessions.pop(existing[0], None)
        elif token not in sessions and len(sessions) >= _session_limit(username):
            return False
        sessions[token] = {
            "session": session_id,
            "last_activity": now,
            "activity_ms": 0,
            "device_id": device_id[:96],
            "device_label": device_label[:64] or "Navegador",
        }
        return True


def _active_sessions(username: str) -> list[tuple[str, dict, float]]:
    """Return active sessions with elapsed idle time, pruning expired entries."""
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        sessions = _sessions_for(registry, username)
        active = []
        for token, owner in list(sessions.items()):
            idle = now - owner["last_activity"]
            expired = idle >= _session_timeout(username)
            expired = expired or (_is_hector(username) and token.startswith("h1.")
                                  and not _verified_hector_token(token))
            if expired:
                sessions.pop(token, None)
            else:
                active.append((token, dict(owner), idle))
        return sorted(active, key=lambda item: item[2])


def _device_name(owner: dict) -> str:
    label = str(owner.get("device_label") or "Dispositivo anterior")
    device_id = str(owner.get("device_id") or "")
    return f"{label} · {device_id[-5:]}" if device_id else label


def _revoke_session(username: str, token: str) -> bool:
    registry = _registry()
    with registry["guard"]:
        sessions = _sessions_for(registry, username)
        removed = sessions.pop(token, None) is not None
        if removed:
            registry.setdefault("revoked_tokens", set()).add(token)
        return removed


def _finish_pending_claim() -> bool:
    pending = st.session_state.get("map_login_pending_claim")
    if not pending:
        return False
    username = pending["username"]
    if not _claim(username, pending["session_id"], pending["token"],
                  pending.get("device_id", ""), pending.get("device_label", "Navegador")):
        return False
    st.session_state["map_login_user"] = username
    st.session_state["map_login_role"] = "config" if is_config_user(username) else "user"
    st.session_state["map_login_session_token"] = pending["token"]
    st.session_state["map_login_authenticated"] = True
    if _is_hector(username):
        st.session_state["map_login_remember"] = True
    st.session_state.pop("map_login_pending_claim", None)
    st.rerun()
    return True


def _render_pending_claim() -> None:
    pending = st.session_state.get("map_login_pending_claim")
    if not pending:
        return
    username = pending["username"]
    st.title("Sesiones abiertas")
    st.caption("Elige qué otro equipo desconectar para continuar en este equipo.")
    current_token = st.session_state.get("map_login_session_token")
    others = [(token, owner, idle) for token, owner, idle in _active_sessions(username)
              if token != current_token]
    if not others:
        if _finish_pending_claim():
            return
        st.info("No hay sesiones anteriores que cerrar. Vuelve a intentar ingresar.")
    else:
        for index, (token, owner, idle) in enumerate(others, start=1):
            minutes = max(1, int(idle // 60))
            label = _device_name(owner)
            activity = "menos de un minuto" if idle < 60 else f"hace {minutes} min"
            if st.button(f"Cerrar {label} · actividad {activity}", key=f"close_login_session_{index}",
                         use_container_width=True):
                _revoke_session(username, token)
                if _finish_pending_claim():
                    return
                st.rerun()
        if len(others) > 1 and st.button("Cerrar todas las demás sesiones", key="close_all_login_sessions",
                                        use_container_width=True):
            for token, _owner, _idle in others:
                _revoke_session(username, token)
            _finish_pending_claim()
    if st.button("Cancelar ingreso", key="cancel_pending_session_login"):
        client = st.session_state.get("map_supabase_auth_client")
        if client:
            try:
                client.auth.sign_out()
            except Exception:
                pass
        st.session_state.pop("map_login_pending_claim", None)
        st.session_state.pop("map_supabase_auth_client", None)
        st.rerun()


@st.dialog("Cerrar sesión")
def _render_session_dialog() -> None:
    from practice_mode import enabled as practice_enabled

    if practice_enabled():
        st.caption("Estas acciones son de muestra. Tu sesión real sigue abierta.")
        current_col, all_col = st.columns(2)
        if current_col.button("Cerrar esta sesión", key="practice_logout_current", width="stretch"):
            st.success("Salida de este dispositivo simulada.")
        if all_col.button("Cerrar todas las sesiones", key="practice_logout_all", width="stretch"):
            st.success("Salida de todos los dispositivos simulada.")
        return
    username = st.session_state.get("map_login_user", "")
    current_token = st.session_state.get("map_login_session_token", "")
    active = _active_sessions(username)
    current_col, all_col = st.columns(2)
    if current_col.button("Cerrar esta sesión", key="logout_current", width="stretch"):
        logout()
        st.rerun()
    if all_col.button("Cerrar todas las sesiones", key="logout_all", width="stretch"):
        logout(all_sessions=True)
        st.rerun()
    if not active:
        return
    st.caption("Puedes desconectar otros equipos. Esta sesión seguirá abierta.")
    others = [(token, owner, idle) for token, owner, idle in active if token != current_token]
    current = next((owner for token, owner, _idle in active if token == current_token), None)
    if current:
        st.caption("Este equipo: " + _device_name(current))
    if not others:
        st.write("Solo está activa esta sesión.")
    for index, (token, owner, idle) in enumerate(others, start=1):
        minutes = max(1, int(idle // 60))
        label = _device_name(owner)
        activity = "menos de un minuto" if idle < 60 else f"hace {minutes} min"
        if st.button(f"Cerrar {label} · actividad {activity}", key=f"close_active_session_{index}",
                     use_container_width=True):
            _revoke_session(username, token)
            st.rerun()


def _resume(token: str, session_id: str, device_id: str = "", device_label: str = "") -> str | None:
    registry = _registry()
    now = time.monotonic()
    persistent_username = _verified_hector_token(token) if str(token).startswith("h1.") else None
    if str(token).startswith("h1.") and not persistent_username:
        return None
    with registry["guard"]:
        if token in registry.get("revoked_tokens", set()):
            return None
        for username, owner_group in list(registry["owners"].items()):
            sessions = _sessions_for(registry, username)
            for active_token, owner in list(sessions.items()):
                expired = now - owner["last_activity"] >= _session_timeout(username)
                expired = expired or (_is_hector(username) and active_token.startswith("h1.")
                                      and not _verified_hector_token(active_token))
                if expired:
                    sessions.pop(active_token, None)
                    continue
                if active_token == token:
                    owner["session"] = session_id
                    return username
    if persistent_username and _claim(persistent_username, session_id, token, device_id, device_label):
        return persistent_username
    return None


def _heartbeat(username: str, session_id: str, token: str, activity_ms: int = 0) -> bool:
    registry = _registry()
    now = time.monotonic()
    with registry["guard"]:
        sessions = _sessions_for(registry, username)
        owner = sessions.get(token)
        if not owner or owner["session"] != session_id:
            return False
        expired = now - owner["last_activity"] >= _session_timeout(username)
        expired = expired or (_is_hector(username) and token.startswith("h1.")
                              and not _verified_hector_token(token))
        if expired:
            sessions.pop(token, None)
            return False
        if activity_ms and activity_ms > owner["activity_ms"]:
            owner["activity_ms"] = activity_ms
            owner["last_activity"] = now
        return True


def _release(username: str | None, session_id: str | None, token: str | None) -> None:
    if not username or not session_id or not token:
        return
    registry = _registry()
    with registry["guard"]:
        sessions = _sessions_for(registry, username)
        owner = sessions.get(token)
        if owner and owner["session"] == session_id:
            sessions.pop(token, None)


def _bridge(token: str | None = None, *, clear: bool = False, remember: bool = True):
    return _BROWSER_SESSION(
        data={"token": token or "", "clear": clear, "remember": remember},
        default={"browser_token": "", "remembered": False, "ready": False, "activity_ms": 0, "device_id": "", "device_label": "Navegador"},
        key="map_auth_browser_session",
        on_browser_token_change=lambda: None,
        on_remembered_change=lambda: None,
        on_ready_change=lambda: None,
        on_activity_ms_change=lambda: None,
        on_device_id_change=lambda: None,
        on_device_label_change=lambda: None,
    )


def _clear_authentication() -> None:
    _release(
        st.session_state.get("map_login_user"),
        st.session_state.get("map_login_session_id"),
        st.session_state.get("map_login_session_token"),
    )
    for key in (
        "map_login_authenticated",
        "map_login_user",
        "map_login_session_id",
        "map_login_session_token",
        "map_login_role",
        "map_login_pending_claim",
        "map_login_show_sessions",
        "map_supabase_auth_client",
        "map_supabase_pending_identity",
        "map_supabase_pending_email",
        "map_supabase_mfa_enrollment",
        "map_email_otp_challenge",
        "map_email_otp_failures",
        "map_mfa_deadline",
    ):
        st.session_state.pop(key, None)


def logout(*, all_sessions: bool = False) -> None:
    username = st.session_state.get("map_login_user")
    token = st.session_state.get("map_login_session_token")
    if username:
        registry = _registry()
        with registry["guard"]:
            sessions = _sessions_for(registry, username)
            revoked = registry.setdefault("revoked_tokens", set())
            if token:
                revoked.add(token)
            if all_sessions:
                revoked.update(sessions)
                sessions.clear()
    client = st.session_state.get("map_supabase_auth_client")
    if client:
        try:
            client.auth.sign_out({"scope": "global" if all_sessions else "local"})
        except Exception:
            pass
    _clear_authentication()
    st.session_state["map_login_remember"] = False
    st.session_state["map_login_clear_browser_session"] = True


def _supabase_auth_ready() -> bool:
    try:
        from supabase_accounts import auth_config
        auth_config()
        return True
    except Exception:
        return False


def _finish_supabase_login(alias: str) -> None:
    session_id = st.session_state.setdefault("map_login_session_id", token_urlsafe(24))
    session_token = _issue_session_token(alias)
    device_id = st.session_state.get("map_login_device_id", "")
    device_label = st.session_state.get("map_login_device_label", "Navegador")
    if not _claim(alias, session_id, session_token, device_id, device_label):
        st.session_state["map_login_pending_claim"] = {
            "username": alias, "session_id": session_id, "token": session_token,
            "device_id": device_id, "device_label": device_label,
        }
        # The password and second factor have already been verified. Do not ask for them again
        # while the user chooses which older device to disconnect.
        for key in ("map_supabase_pending_identity", "map_supabase_pending_email", "map_supabase_mfa_enrollment",
                    "map_email_otp_challenge", "map_email_otp_failures", "map_mfa_deadline"):
            st.session_state.pop(key, None)
        st.rerun()
        return
    st.session_state["map_login_user"] = alias
    st.session_state["map_login_role"] = "user"
    st.session_state["map_login_session_token"] = session_token
    st.session_state["map_login_authenticated"] = True
    if _is_hector(alias):
        st.session_state["map_login_remember"] = True
    st.session_state.pop("map_supabase_pending_identity", None)
    st.session_state.pop("map_supabase_pending_email", None)
    st.session_state.pop("map_supabase_mfa_enrollment", None)
    st.session_state.pop("map_email_otp_challenge", None)
    st.session_state.pop("map_email_otp_failures", None)
    st.session_state.pop("map_mfa_deadline", None)
    st.rerun()


def _totp_enrollment_error(exc: Exception) -> str:
    """Turn Supabase MFA enrollment failures into actionable, safe guidance."""
    code = str(getattr(exc, "code", "") or "").strip().casefold()
    message = str(getattr(exc, "message", "") or exc).strip()
    details = f"{code} {message}".casefold()
    if "mfa_totp_enroll_not_enabled" in details:
        return (
            "Supabase tiene desactivada la inscripción de Authenticator (TOTP). "
            "En el proyecto abre Authentication → Multi-factor Authentication y activa "
            "TOTP enrollment y TOTP verification; guarda los cambios y vuelve a ingresar."
        )
    if "mfa_totp_verify_not_enabled" in details:
        return (
            "Supabase tiene desactivada la verificación TOTP. En Authentication → "
            "Multi-factor Authentication activa TOTP verification, guarda y vuelve a ingresar."
        )
    if "mfa_factor_name_conflict" in details:
        return (
            "Ya existe un factor Authenticator con ese nombre. Actualiza la página e inténtalo "
            "otra vez; la app generará un nombre de configuración nuevo."
        )
    # Show the server's code and concise message, but never request or expose credentials.
    safe_message = " ".join(message.split())[:240]
    if code:
        return f"Supabase rechazó la configuración ({code}). {safe_message}"
    return f"No se pudo configurar Authenticator. Detalle de Supabase: {safe_message}"


def _send_login_code(user_email: str, code: str) -> str | None:
    """Deliver an app-generated code over the same Gmail SMTP flow as AMI."""
    logger = logging.getLogger(__name__)
    try:
        config = dict(st.secrets["security"])
        sender = str(config.get("smtp_email", "")).strip()
        host = str(config.get("smtp_host", "")).strip()
        port = int(config.get("smtp_port", 587))
        password = "".join(str(config.get("smtp_app_password", "")).split())
    except Exception as exc:
        logger.error("Login-code SMTP secrets unavailable (%s).", type(exc).__name__)
        return "configuration"
    if host != "smtp.gmail.com" or port != 587 or not sender or not password:
        missing = [
            key for key, value in (
                ("smtp_host", host == "smtp.gmail.com"),
                ("smtp_port", port == 587),
                ("smtp_email", bool(sender)),
                ("smtp_app_password", bool(password)),
            ) if not value
        ]
        logger.error("Login-code SMTP secrets invalid or incomplete (keys: %s).", ", ".join(missing))
        return "configuration"
    message = EmailMessage()
    # Keep the same trusted sender identity as the AMI access-code email.
    # The SMTP address and app password remain private in Streamlit Secrets.
    message["From"] = formataddr(("Seguridad AMI Magallanes", sender))
    message["To"] = user_email
    message["Subject"] = "Código temporal / Temporary code · AMI Magallanes"
    message.set_content(
        "Código temporal de acceso / Temporary sign-in code: "
        f"{code}\n\n"
        "Úsalo una sola vez dentro del tiempo indicado en la app (hasta 3 minutos). "
        "No lo compartas. Si no lo solicitaste, ignora este mensaje.\n\n"
        "Use it once before the timer in the app ends (up to 3 minutes). "
        "Do not share it. If you did not request it, ignore this message."
    )
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(sender, password)
            smtp.send_message(message, from_addr=sender, to_addrs=[user_email])
        return None
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("Login-code SMTP authentication rejected (SMTP %s).", exc.smtp_code)
        return "authentication"
    except smtplib.SMTPRecipientsRefused as exc:
        codes = sorted({str(reply[0]) for reply in exc.recipients.values() if reply})
        logger.error("Login-code recipient rejected by SMTP (codes: %s).", ", ".join(codes) or "unknown")
        return "recipient"
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        smtp_code = getattr(exc, "smtp_code", None)
        logger.error(
            "Login-code SMTP delivery failed (%s%s).",
            type(exc).__name__,
            f", SMTP {smtp_code}" if smtp_code else "",
        )
        return "delivery"


@st.cache_resource
def _email_code_throttle() -> tuple[threading.Lock, dict[str, float]]:
    return threading.Lock(), {}


def _reserve_email_send(email: str) -> bool:
    lock, next_send = _email_code_throttle()
    now = time.monotonic()
    with lock:
        for key in [key for key, until in next_send.items() if until <= now]:
            del next_send[key]
        key = email.casefold()
        if key in next_send:
            return False
        next_send[key] = now + 60
        return True


def _render_email_code(alias: str, email: str, user_id: str) -> None:
    deadline = float(st.session_state.get("map_mfa_deadline") or 0)
    if deadline and time.time() >= deadline:
        st.session_state.pop("map_email_otp_challenge", None)
        st.error("El código venció. Vuelve a iniciar sesión para solicitar otro.")
        return

    challenge = st.session_state.get("map_email_otp_challenge")
    if not challenge and st.button("Enviar código temporal", key="map_email_otp_send"):
        if not _reserve_email_send(email):
            st.warning("Espera un minuto antes de pedir otro código.")
        else:
            code = f"{randbelow(1_000_000):06d}"
            secret = token_bytes(32)
            error = _send_login_code(email, code)
            if error is None:
                digest = hmac.digest(secret, code.encode("ascii"), "sha256")
                st.session_state["map_email_otp_challenge"] = (user_id, secret, digest)
                st.session_state["map_mfa_deadline"] = time.time() + MFA_ENTRY_WINDOW_SECONDS
                st.rerun()
            messages = {
                "configuration": "El servidor tiene incompleta la configuración de correo.",
                "authentication": "El servicio de correo rechazó el acceso del remitente.",
                "recipient": "El servicio de correo rechazó el destinatario de esta cuenta.",
                "delivery": "El servicio de correo no pudo enviar el código. Intenta de nuevo en un minuto.",
            }
            st.error(messages[error])
    if challenge and challenge[0] == user_id:
        st.info("Código enviado. Revisa tu bandeja de entrada y correo no deseado.")
        with st.form("map_email_otp_form", clear_on_submit=True):
            code = st.text_input("Código temporal de 6 dígitos", type="password", max_chars=12,
                                 autocomplete="one-time-code")
            verify = st.form_submit_button("Verificar e ingresar", type="primary")
        if verify:
            digits = []
            for char in str(code or "").strip():
                if char.isdecimal():
                    digits.append(str(unicodedata.decimal(char)))
                elif char.isspace() or char in "-‐‑‒–—\u200b\u200e\u200f":
                    continue
                else:
                    digits = []
                    break
            normalized = "".join(digits)
            supplied = hmac.digest(challenge[1], normalized.encode("ascii"), "sha256")
            if (time.time() < float(deadline) and len(normalized) == 6
                    and hmac.compare_digest(challenge[2], supplied)):
                st.session_state.pop("map_email_otp_challenge", None)
                st.session_state.pop("map_mfa_deadline", None)
                st.session_state.pop("map_email_otp_failures", None)
                _finish_supabase_login(alias)
            else:
                failures = int(st.session_state.get("map_email_otp_failures", 0)) + 1
                st.session_state["map_email_otp_failures"] = failures
                if failures >= 3:
                    st.session_state.pop("map_email_otp_challenge", None)
                    st.session_state.pop("map_email_otp_failures", None)
                    st.error("Se agotaron los intentos. Inicia sesión nuevamente para pedir otro código.")
                else:
                    st.error(f"El código no es válido o ya venció. Intento {failures} de 3.")


def _supabase_second_factor() -> None:
    """Require either a verified Authenticator factor or a one-time email code."""
    from supabase_accounts import SupabaseSetupError

    client = st.session_state.get("map_supabase_auth_client")
    alias = st.session_state.get("map_supabase_pending_identity")
    email = st.session_state.get("map_supabase_pending_email")
    if not client or not alias or not email:
        return
    st.subheader("Verificación en dos pasos")
    st.caption(f"Cuenta {alias} · correo asociado: {email}")
    options = ["Aplicación Authenticator", "Código temporal por correo"]
    method = st.radio("Elige cómo verificar este ingreso", options, horizontal=True, key="map_mfa_method")
    if st.button("Cancelar e iniciar con otra cuenta", key="map_mfa_cancel"):
        try:
            client.auth.sign_out()
        except Exception:
            pass
        _clear_authentication()
        st.rerun()
    if method == "Código temporal por correo":
        try:
            user_response = client.auth.get_user()
            user = user_response.user if user_response else None
        except Exception:
            user = None
        if user and str(user.email).casefold() == email.casefold():
            _render_email_code(alias, email, str(user.id))
        else:
            st.error("No se pudo validar la cuenta. Vuelve a iniciar sesión para pedir el código.")
        return

    try:
        factors = client.auth.mfa.list_factors()
        verified = [f for f in (getattr(factors, "totp", None) or []) if getattr(f, "status", "") == "verified"]
    except Exception:
        st.error("No se pudo consultar el segundo factor de Supabase. Intenta recargar la página.")
        return

    if verified:
        factor_id = str(verified[0].id)
    else:
        enrollment = st.session_state.get("map_supabase_mfa_enrollment")
        if not enrollment:
            try:
                # Discard abandoned enrollments like AMI before creating a new QR.
                try:
                    factors = client.auth.mfa.list_factors()
                    for old_factor in list(getattr(factors, "all", None) or []):
                        if (getattr(old_factor, "factor_type", None) == "totp"
                                and getattr(old_factor, "status", None) == "unverified"):
                            try:
                                client.auth.mfa.unenroll({"factor_id": str(old_factor.id)})
                            except Exception:
                                pass
                except Exception:
                    pass
                # A unique name avoids collisions with abandoned old enrollments.
                friendly_name = f"Redes Edelmag {token_urlsafe(5)}"
                enrolled = client.auth.mfa.enroll({"factor_type": "totp", "friendly_name": friendly_name,
                                                   "issuer": MFA_ISSUER})
                totp = getattr(enrolled, "totp", None)
                if not totp or not getattr(totp, "qr_code", ""):
                    raise ValueError("Missing QR")
                enrollment = {"factor_id": str(enrolled.id), "qr_code": str(totp.qr_code),
                              "secret": str(getattr(totp, "secret", ""))}
                st.session_state["map_supabase_mfa_enrollment"] = enrollment
            except Exception as exc:
                st.error(_totp_enrollment_error(exc))
                st.caption("También puedes elegir «Código temporal por correo» mientras se habilita TOTP.")
                return
        st.write("Escanea este QR con Microsoft Authenticator, Google Authenticator u otra app TOTP.")
        qr = enrollment["qr_code"]
        if qr.startswith("data:image/"):
            st.markdown(f'<img alt="QR para configurar Authenticator" width="190" src="{qr}">', unsafe_allow_html=True)
        elif "<svg" in qr[:300].lower():
            qr_svg = base64.b64encode(qr.encode("utf-8")).decode("ascii")
            st.markdown(
                f'<img alt="QR para configurar Authenticator" width="190" '
                f'src="data:image/svg+xml;base64,{qr_svg}">',
                unsafe_allow_html=True,
            )
        if enrollment.get("secret"):
            st.caption("Si no puedes escanear, ingresa esta clave manualmente en la app Authenticator:")
            st.code(enrollment["secret"], language=None)
        factor_id = enrollment["factor_id"]

    with st.form("map_totp_form", clear_on_submit=True):
        code = st.text_input("Código de Authenticator", max_chars=12, placeholder="123456")
        verify = st.form_submit_button("Verificar e ingresar", type="primary")
    if verify:
        try:
            client.auth.mfa.challenge_and_verify({"factor_id": factor_id, "code": code.strip().replace(" ", "")})
            assurance = client.auth.mfa.get_authenticator_assurance_level()
            level = getattr(assurance, "current_level", "")
            if "aal2" not in str(getattr(level, "value", level)).casefold():
                raise RuntimeError("Supabase no confirmó el nivel AAL2")
            _finish_supabase_login(alias)
        except Exception as exc:
            detail = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', exc)}".casefold()
            if "mfa_verification_failed" in detail or "mfa_challenge_expired" in detail:
                st.error("El código no es válido o ya venció. Revisa la hora automática del teléfono e inténtalo de nuevo.")
            else:
                st.error("Supabase no pudo validar el factor Authenticator. Cierra sesión, vuelve a entrar y revisa que TOTP esté habilitado en Authentication → Multi-factor Authentication.")
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
    bridge = _bridge(
        expected_token if st.session_state.get("map_login_authenticated") else None,
        clear=clear_browser,
        remember=bool(st.session_state.get("map_login_remember", False)
                       or _is_hector(st.session_state.get("map_login_user"))),
    )
    if not getattr(bridge, "ready", False):
        st.info("Restaurando sesión…")
        st.stop()

    browser_token = "" if clear_browser else (getattr(bridge, "browser_token", "") or "")
    activity_ms = int(getattr(bridge, "activity_ms", 0) or 0)
    device_id = str(getattr(bridge, "device_id", "") or "")
    device_label = str(getattr(bridge, "device_label", "Navegador") or "Navegador")
    if device_id:
        st.session_state["map_login_device_id"] = device_id
    if device_label:
        st.session_state["map_login_device_label"] = device_label

    if st.session_state.get("map_login_authenticated"):
        username = st.session_state.get("map_login_user", "")
        session_id = st.session_state.get("map_login_session_id", "")
        token = st.session_state.get("map_login_session_token", "")
        if browser_token != token:
            st.info("Restaurando sesión…")
            st.stop()
        if username and session_id and token and _heartbeat(username, session_id, token, activity_ms):
            if _is_hector(username):
                st.session_state["map_login_remember"] = True
            _keep_session_alive()
            return
        _clear_authentication()
        st.session_state["map_login_clear_browser_session"] = True
        st.rerun()

    if browser_token and not clear_browser:
        username = _resume(browser_token, st.session_state.setdefault("map_login_session_id", token_urlsafe(24)),
                           device_id, device_label)
        if username:
            st.session_state["map_login_remember"] = bool(_is_hector(username)
                                                           or getattr(bridge, "remembered", False))
            st.session_state["map_login_user"] = username
            st.session_state["map_login_role"] = "config" if is_config_user(username) else "user"
            st.session_state["map_login_session_token"] = browser_token
            st.session_state["map_login_authenticated"] = True
            _keep_session_alive()
            return
        st.session_state["map_login_clear_browser_session"] = True
        st.rerun()

    if st.session_state.get("map_login_pending_claim"):
        _render_pending_claim()
        st.stop()

    if st.session_state.get("map_supabase_pending_identity"):
        _supabase_second_factor()
        st.stop()

    with st.form("map_login_form"):
        st.title("Redes Edelmag Magallanes")
        st.caption("Inicia sesión para continuar")
        username = st.text_input("Usuario", help="Nombre y apellido juntos, en mayúsculas. También puedes usar tu correo.")
        password = st.text_input("Contraseña", type="password")
        remember_session = st.checkbox("Recordar sesión (máximo 5 horas sin actividad)")
        submitted = st.form_submit_button("Ingresar", use_container_width=True)

    if submitted:
        normalized_username = username.strip()
        st.session_state["map_login_remember"] = bool(remember_session or _is_hector(normalized_username))
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
            session_token = _issue_session_token(normalized_username)
            device_id = st.session_state.get("map_login_device_id", "")
            device_label = st.session_state.get("map_login_device_label", "Navegador")
            if _claim(normalized_username, session_id, session_token, device_id, device_label):
                st.session_state["map_login_user"] = normalized_username
                st.session_state["map_login_role"] = "config" if is_config_user(normalized_username) else "user"
                st.session_state["map_login_session_token"] = session_token
                st.session_state["map_login_authenticated"] = True
                st.rerun()
            else:
                st.session_state["map_login_pending_claim"] = {
                    "username": normalized_username, "session_id": session_id, "token": session_token,
                    "device_id": device_id, "device_label": device_label,
                }
                st.rerun()
    st.stop()


def render_session_dialog() -> None:
    _render_session_dialog()


@st.fragment
def render_session_controls() -> None:
    if st.button("Administrar sesiones", key="manage_sessions_from_map"):
        _render_session_dialog()
