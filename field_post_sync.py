"""Server-authorized synchronization of field posts across devices."""
import json
import math
from urllib.parse import urlencode

import streamlit as st

from maintenance_findings import FindingsError, _request
from supabase_accounts import account_roster


_bridge = st.components.v2.component(
    'field_post_sync', html='<span></span>',
    js=r"""
    export default function({data, setStateValue}) {
      let stopped = false, busy = false;
      const tick = async () => {
        if (stopped || busy) return;
        busy = true;
        try {
          const frame = [...document.querySelectorAll('iframe')].find(f => {
            try { return typeof f.contentWindow.fieldPostSyncExchange === 'function'; }
            catch { return false; }
          });
          if (!frame) return;
          const batch = await frame.contentWindow.fieldPostSyncExchange(data);
          if (batch && !stopped) setStateValue('request', batch);
        } finally { busy = false; }
      };
      tick();
      const timer = setInterval(tick, 10000);
      return () => { stopped = true; clearInterval(timer); };
    }
    """,
)


@st.fragment
def render_field_post_sync():
    actor = str(st.session_state.get('map_login_user', '')).upper()
    if not actor:
        return
    reply = st.session_state.get('field_post_sync_reply', {})
    result = _bridge(data={**reply, 'users': list(account_roster()), 'actor': actor},
                     key='field_post_sync_bridge', default={'request': None},
                     on_request_change=lambda: None)
    request = getattr(result, 'request', None)
    if not request or request.get('id') == reply.get('id'):
        return
    response = {'id': request.get('id'), 'acks': [], 'rows': [], 'error': ''}
    try:
        for op in request.get('operations', [])[:100]:
            post = op['post']
            visibility = post.get('visibility', 'private')
            if visibility not in ('private', 'user', 'global'):
                raise FindingsError('Elige un acceso válido para el poste.')
            if visibility == 'user' and post.get('sharedWith') not in account_roster():
                raise FindingsError('Selecciona el usuario con quien compartir.')
            if len(json.dumps(post)) > 8 * 1024 * 1024:
                raise FindingsError('Las fotos del poste superan el tamaño permitido para sincronizar.')
            visibility = post.get('visibility', 'private')
            if visibility not in ('private', 'user', 'global'):
                raise FindingsError('Selecciona una opción válida para compartir.')
            if visibility == 'user' and post.get('sharedWith') not in account_roster():
                raise FindingsError('Selecciona un usuario registrado para compartir el poste.')
            if not op.get('deleted'):
                latitude, longitude = float(post['lat']), float(post['lon'])
                if not (math.isfinite(latitude) and math.isfinite(longitude)
                        and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise FindingsError('Las coordenadas del poste están fuera de rango.')
                if post.get('kind') != 'style' and not (-56.5 <= latitude <= -48 and -76 <= longitude <= -65):
                    raise FindingsError('Corrige la ubicación del poste: está fuera de Magallanes.')
            try:
                saved = _request('POST', '/rest/v1/rpc/sync_field_post', {
                    'actor': actor, 'post_id': str(post['id']),
                    'expected_revision': int(op.get('revision', 0)),
                    'post_data': post, 'remove_post': bool(op.get('deleted')),
                })
            except FindingsError as exc:
                if 'Codigo repetido:' in str(exc):
                    response['error'] = 'Ese código ya existe. Revisa los postes señalados como repetidos.'
                    continue
                if 'Conflicto:' not in str(exc):
                    raise
                response.setdefault('conflicts', []).append(post['id'])
                continue
            response['acks'].append({'id': post['id'], 'revision': saved['revision'],
                                     'signature': op['signature'], 'deleted': bool(op.get('deleted'))})
        query = urlencode({'select': '*', 'or': f'(owner_name.eq.{actor},visibility.eq.global,and(visibility.eq.user,shared_with.eq.{actor}))', 'order': 'updated_at.asc'})
        response['rows'] = []
        offset = 0
        while True:
            page = _request('GET', '/rest/v1/created_field_posts?' + query + f'&limit=500&offset={offset}') or []
            response['rows'].extend(page)
            if len(page) < 500:
                break
            offset += len(page)
    except (FindingsError, ValueError, KeyError, TypeError) as exc:
        response['error'] = str(exc)
    st.session_state.field_post_sync_reply = response
    st.rerun(scope='fragment')
