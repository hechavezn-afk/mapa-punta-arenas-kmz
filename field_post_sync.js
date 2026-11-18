const fieldSyncKey = 'field-post-sync-v1:' + LOGIN_USER;
let fieldSyncState = {}, fieldSyncPending = null, fieldSyncApplied = '';
try { fieldSyncState = JSON.parse(localStorage.getItem(fieldSyncKey) || '{}'); } catch {}
function fieldSyncSignature(post) { return JSON.stringify(post); }
function fieldSyncSaveState() { localStorage.setItem(fieldSyncKey, JSON.stringify(fieldSyncState)); }
function fieldPostMarkDeleted(post) {
  const known = fieldSyncState[post.id] || {revision:0};
  fieldSyncState[post.id] = {...known, deletePending:true, deleted:true, signature:''};
  fieldSyncSaveState();
}
async function fieldPostSyncExchange(reply) {
  const target = document.getElementById('fieldpost-share-user');
  if (target && !target.options.length) {
    for (const name of reply.users || []) {
      if (name === reply.actor) continue;
      target.add(new Option(name, name));
    }
  }
  if (reply.id && reply.id !== fieldSyncApplied) {
    fieldSyncApplied = reply.id;
    for (const ack of reply.acks || []) {
      const deletePending = fieldSyncState[ack.id]?.deletePending && !ack.deleted;
      fieldSyncState[ack.id] = {revision: ack.revision, signature: ack.signature, deleted: ack.deleted, deletePending};
    }
    for (const id of reply.conflicts || []) {
      if (fieldSyncState[id]?.deletePending || reply.rows?.some(row => row.id === id && row.deleted)) continue;
      const local = createdFieldPosts.find(p => p.id === id);
      if (local) {
        // Preserve this device's version separately before applying the newer cloud version.
        localStorage.setItem(fieldSyncKey + ':conflict:' + id, JSON.stringify(local));
        const copy = {...local, id:crypto.randomUUID(), visibility:'private', sharedWith:'', owner:LOGIN_USER};
        copy.notes = [copy.notes, 'Copia conservada por cambios simultáneos en otro equipo'].filter(Boolean).join(' · ');
        createdFieldPosts.splice(createdFieldPosts.indexOf(local), 1);
        createdFieldPosts.push(copy);
      }
      delete fieldSyncState[id];
    }
    for (const row of reply.rows || []) {
      const index = createdFieldPosts.findIndex(p => p.id === row.id);
      const local = index >= 0 ? createdFieldPosts[index] : null;
      const known = fieldSyncState[row.id];
      if (row.deleted) {
        if (index >= 0) createdFieldPosts.splice(index, 1);
        fieldSyncState[row.id] = {revision:row.revision, deleted:true, signature:''};
        continue;
      }
      if (known?.deletePending || known?.deleted) continue;
      const dirty = local && (!known || fieldSyncSignature(local) !== known.signature);
      if (dirty) continue;
      const post = row.data;
      for (const attachment of post.syncPhotos || []) {
        const blob = await (await fetch(attachment.data)).blob();
        await storeSurveyPdf(attachment.id, blob, attachment.name);
      }
      delete post.syncPhotos;
      post.owner = row.owner_name;
      if (index >= 0) createdFieldPosts[index] = post;
      else if (!known || !known.deleted) createdFieldPosts.push(post);
      fieldSyncState[row.id] = {revision:row.revision, signature:fieldSyncSignature(post), deleted:false};
    }
    fieldSyncSaveState();
    if (!reply.error) {
      const visibleIds = new Set((reply.rows || []).map(row => row.id));
      createdFieldPosts = createdFieldPosts.filter(post => {
        if (post.owner && post.owner !== LOGIN_USER && fieldSyncState[post.id] && !visibleIds.has(post.id)) {
          delete fieldSyncState[post.id];
          return false;
        }
        return true;
      });
      fieldSyncSaveState();
    }
    fieldPostPersist();
    renderCreatedFieldPosts();
    document.getElementById('fieldpost-sync-status').textContent = reply.error || (reply.conflicts?.length ? 'Había cambios simultáneos. Conservamos tu versión como copia privada y cargamos la versión compartida.' : 'Postes sincronizados entre equipos');
    fieldSyncPending = null;
    return null;
  }
  if (fieldSyncPending) return null;
  const operations = [];
  for (const post of createdFieldPosts) {
    const known = fieldSyncState[post.id];
    if (known?.deleted || known?.deletePending) continue;
    const signature = fieldSyncSignature(post);
    if (known?.signature === signature) continue;
    const photos = [];
    for (const id of post.photos || []) {
      const stored = await readSurveyPdf(id);
      if (!stored?.blob) continue;
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(stored.blob);
      });
      photos.push({id, name:stored.name, data});
    }
    operations.push({post:{...post, syncPhotos:photos}, signature, revision:known?.revision || 0});
  }
  for (const [id, known] of Object.entries(fieldSyncState)) {
    if (known.deletePending || (!known.deleted && !createdFieldPosts.some(p => p.id === id))) {
      operations.push({post:{id,visibility:'private'}, revision:known.revision, deleted:true, signature:''});
    }
  }
  fieldSyncPending = {id:crypto.randomUUID(), operations};
  document.getElementById('fieldpost-sync-status').textContent = 'Sincronizando postes…';
  return fieldSyncPending;
}
