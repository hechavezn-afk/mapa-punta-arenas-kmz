"""Server-authorized synchronization of field posts across devices."""
import json
import math
from urllib.parse import urlencode

import streamlit as st

from maintenance_findings import FindingsError, _request
from supabase_accounts import account_roster

SYNC_BATCH_SIZE = 20


def _preserve_post_photos(actor: str, post: dict, revision: int) -> dict:
    """Keep existing image bytes if this device only downloaded the post metadata."""
    incoming = {photo['id']: photo for photo in post.get('syncPhotos', [])}
    photo_ids = post.get('photos') or []
    missing = set(photo_ids) - incoming.keys()
    if missing and revision:
        query = urlencode({'select': 'photos:data->syncPhotos', 'id': f"eq.{post['id']}",
                           'or': f'(owner_name.eq.{actor},visibility.eq.global,and(visibility.eq.user,shared_with.eq.{actor}))'})
        rows = _request('GET', '/rest/v1/created_field_posts?' + query) or []
        existing = {photo['id']: photo for photo in ((rows[0].get('photos') or []) if rows else [])}
        for photo_id in missing:
            if photo_id in existing:
                incoming[photo_id] = existing[photo_id]
    if set(photo_ids) - incoming.keys():
        raise FindingsError('Faltan fotos del poste en este equipo. Sincroniza antes de guardar sus cambios.')
    return {**post, 'syncPhotos': [incoming[photo_id] for photo_id in photo_ids]}


def _read_post_changes(actor: str, revisions: dict, conflicts: list[str]) -> dict:
    """Poll revisions cheaply and transfer a bounded batch of changed records."""
    access = f'(owner_name.eq.{actor},visibility.eq.global,and(visibility.eq.user,shared_with.eq.{actor}))'
    columns = 'id,revision,deleted,owner_name'
    if actor == 'HECTOR':
        columns += ',number:data->>number'
    query = urlencode({'select': columns, 'or': access, 'order': 'id.asc'})
    metadata = []
    offset = 0
    while True:
        page = _request('GET', '/rest/v1/created_field_posts?' + query + f'&limit=500&offset={offset}') or []
        metadata.extend(page)
        if len(page) < 500:
            break
        offset += len(page)
    if actor == 'HECTOR':
        for row in metadata:
            if row['deleted'] or row['owner_name'] != actor or ''.join((row.get('number') or '').split()).upper() != '1-0390-043':
                continue
            deleted = _request('POST', '/rest/v1/rpc/sync_field_post', {
                'actor': actor, 'post_id': row['id'], 'expected_revision': row['revision'],
                'post_data': {}, 'remove_post': True,
            })
            row.update(deleted=True, revision=deleted['revision'])
    rows = [row for row in metadata if row['deleted']]
    changed = [row for row in metadata if not row['deleted'] and (
        str(revisions.get(row['id'], '')) != str(row['revision']) or row['id'] in conflicts
    )]
    # Resolve conflicts before ordinary downloads so a partial batch cannot lose local edits.
    changed.sort(key=lambda row: row['id'] not in conflicts)
    selected = changed[:SYNC_BATCH_SIZE]
    if selected:
        identifiers = ','.join('"' + row['id'].replace('"', '') + '"' for row in selected)
        detail_query = urlencode({'select': '*', 'id': f'in.({identifiers})', 'or': access, 'order': 'id.asc'})
        try:
            details = _request('GET', '/rest/v1/created_field_posts_sync?' + detail_query) or []
        except FindingsError as exc:
            if 'tablas o el bucket' not in str(exc):
                raise
            # Older deployments can sync before the optional projection view is installed.
            details = _request('GET', '/rest/v1/created_field_posts?' + detail_query) or []
        for detail in details:
            post = detail['data']
            valid = post.get('kind') == 'style'
            if not valid:
                try:
                    valid = -56.5 <= float(post.get('lat')) <= -48 and -76 <= float(post.get('lon')) <= -65
                except (TypeError, ValueError):
                    valid = False
            if valid and post.get('photos') and 'syncPhotos' not in post:
                photos_query = urlencode({'select': 'photos:data->syncPhotos', 'id': f"eq.{detail['id']}", 'or': access})
                photos = _request('GET', '/rest/v1/created_field_posts?' + photos_query) or []
                post['syncPhotos'] = (photos[0].get('photos') or []) if photos else []
            if not valid:
                post.pop('syncPhotos', None)
                detail['photosDeferred'] = bool(post.get('photos'))
            rows.append(detail)
    return {'rows': rows, 'visibleIds': [row['id'] for row in metadata],
            'ownedIds': [row['id'] for row in metadata if row['owner_name'] == actor and not row['deleted']],
            'remaining': max(0, len(changed) - len(selected))}


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
        } catch (error) {
          console.warn('No se pudo completar la sincronización de postes. Se reintentará.', error);
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
    if request.get('annotationSync') is not None:
        response['annotationSync'] = True
        try:
            if actor != 'HECTOR':
                raise FindingsError('Esta función está reservada a la cuenta HECTOR.')
            operation = request['annotationSync']
            if operation.get('read'):
                response['annotations'] = _request(
                    'GET', '/rest/v1/private_pole_annotations?select=pole_key,annotation'
                )
            else:
                key, note = operation['key'], operation['note']
                if not isinstance(key, str) or not 1 <= len(key) <= 256:
                    raise FindingsError('Identificador de poste inválido.')
                if not isinstance(note, dict) or len(json.dumps(note)) > 8 * 1024 * 1024:
                    raise FindingsError('La anotación supera el tamaño permitido.')
                _request('POST', '/rest/v1/private_pole_annotations?on_conflict=pole_key',
                         {'pole_key': key, 'annotation': note},
                         prefer='resolution=merge-duplicates,return=minimal')
        except (FindingsError, ValueError, KeyError, TypeError, AttributeError) as exc:
            response['error'] = str(exc)
        st.session_state.field_post_sync_reply = response
        st.rerun()
    try:
        for op in request.get('operations', [])[:SYNC_BATCH_SIZE]:
            post = op['post']
            visibility = post.get('visibility', 'private')
            if visibility not in ('private', 'user', 'global'):
                raise FindingsError('Elige un acceso válido para el poste.')
            if visibility == 'user' and post.get('sharedWith') not in account_roster():
                raise FindingsError('Selecciona el usuario con quien compartir.')
            if len(json.dumps(post)) > 8 * 1024 * 1024:
                raise FindingsError('Las fotos del poste superan el tamaño permitido para sincronizar.')
            if not op.get('deleted'):
                number = ''.join(str(post.get('number') or '').split()).upper()
                if actor == 'HECTOR' and number == '1-0390-043' and post.get('kind') != 'style':
                    response['error'] = 'El número 1-0390-043 fue retirado.'
                    continue
                if post.get('kind') != 'style' and not str(post.get('number') or '').strip():
                    raise FindingsError('Ingresa el número del poste.')
                latitude, longitude = float(post['lat']), float(post['lon'])
                if not (math.isfinite(latitude) and math.isfinite(longitude)
                        and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise FindingsError('Las coordenadas del poste están fuera de rango.')
                if post.get('kind') != 'style' and not (-56.5 <= latitude <= -48 and -76 <= longitude <= -65):
                    raise FindingsError('Corrige la ubicación del poste: está fuera de Magallanes.')
                post = _preserve_post_photos(actor, post, int(op.get('revision', 0)))
            try:
                saved = _request('POST', '/rest/v1/rpc/sync_field_post', {
                    'actor': actor, 'post_id': str(post['id']),
                    'expected_revision': int(op.get('revision', 0)),
                    'post_data': post, 'remove_post': bool(op.get('deleted')),
                })
            except FindingsError as exc:
                if 'Codigo repetido:' in str(exc):
                    response['error'] = 'Ese código ya existe. Revisa los postes señalados como repetidos.'
                    if not op.get('revision'):
                        response.setdefault('conflicts', []).append(post['id'])
                    continue
                if 'Conflicto:' not in str(exc):
                    raise
                response.setdefault('conflicts', []).append(post['id'])
                continue
            response['acks'].append({'id': post['id'], 'revision': saved['revision'],
                                     'signature': op['signature'], 'deleted': bool(op.get('deleted'))})
        # Acknowledged local writes need no immediate download of the same photos.
        revisions = dict(request.get('revisions') or {})
        for ack in response['acks']:
            revisions[ack['id']] = ack['revision']
        for post_id in request.get('photosNeeded') or []:
            revisions.pop(post_id, None)
        response.update(_read_post_changes(actor, revisions, response.get('conflicts', [])))
    except (FindingsError, ValueError, KeyError, TypeError) as exc:
        response['error'] = str(exc)
    st.session_state.field_post_sync_reply = response
    # Component state updates can reach this fragment during a full-app run.
    # An app-scoped rerun is valid in either execution context.
    st.rerun()
