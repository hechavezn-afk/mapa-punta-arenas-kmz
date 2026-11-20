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
    let changed = false;
    for (const ack of reply.acks || []) {
      const deletePending = fieldSyncState[ack.id]?.deletePending && !ack.deleted;
      const photosDeferred = fieldSyncState[ack.id]?.photosDeferred || false;
      fieldSyncState[ack.id] = {revision: ack.revision, signature: ack.signature, deleted: ack.deleted, deletePending, photosDeferred};
    }
    for (const id of reply.conflicts || []) {
      if (fieldSyncState[id]?.deletePending || reply.rows?.some(row => row.id === id && row.deleted)) continue;
      const local = createdFieldPosts.find(p => p.id === id);
      if (local) {
        // Retain an offline backup without creating another active/shared post.
        localStorage.setItem(fieldSyncKey + ':conflict:' + id, JSON.stringify(local));
        createdFieldPosts.splice(createdFieldPosts.indexOf(local), 1);
        changed = true;
      }
      delete fieldSyncState[id];
    }
    for (const row of reply.rows || []) {
      const index = createdFieldPosts.findIndex(p => p.id === row.id);
      const local = index >= 0 ? createdFieldPosts[index] : null;
      const known = fieldSyncState[row.id];
      if (row.deleted) {
        if (index >= 0) { createdFieldPosts.splice(index, 1); changed = true; }
        fieldSyncState[row.id] = {revision:row.revision, deleted:true, signature:''};
        continue;
      }
      if (known?.deletePending || known?.deleted) continue;
      if (fieldPostRetiredCode(row.data || {}) || fieldPostRetiredCode(row.data || {}, row.owner_name || '')) continue;
      const dirty = local && (!known || fieldSyncSignature(local) !== known.signature);
      if (dirty) {
        if (!known && !fieldPostValidLocation(local.lat, local.lon)) {
          fieldSyncState[row.id] = {revision:row.revision, signature:fieldSyncSignature(local), deleted:false, photosDeferred:Boolean(row.photosDeferred)};
        }
        continue;
      }
      const post = row.data;
      for (const attachment of post.syncPhotos || []) {
        const blob = await (await fetch(attachment.data)).blob();
        await storeSurveyPdf(attachment.id, blob, attachment.name);
      }
      delete post.syncPhotos;
      post.owner = row.owner_name;
      if (index >= 0) createdFieldPosts[index] = post;
      else if (!known || !known.deleted) createdFieldPosts.push(post);
      changed = true;
      fieldSyncState[row.id] = {revision:row.revision, signature:fieldSyncSignature(post), deleted:false, photosDeferred:Boolean(row.photosDeferred)};
    }
    fieldSyncSaveState();
    if (!reply.error) {
      const visibleIds = new Set(reply.visibleIds || (reply.rows || []).map(row => row.id));
      createdFieldPosts = createdFieldPosts.filter(post => {
        if (post.owner && post.owner !== LOGIN_USER && fieldSyncState[post.id] && !visibleIds.has(post.id)) {
          delete fieldSyncState[post.id];
          changed = true;
          return false;
        }
        return true;
      });
      fieldSyncSaveState();
      if (!reply.remaining && Array.isArray(reply.ownedIds) && String(reply.actor || '').toUpperCase() === 'HECTOR') {
        const ownedIds = new Set(reply.ownedIds);
        const eligible = createdFieldPosts.filter(post => !visibleIds.has(post.id) || ownedIds.has(post.id));
        const obsolete = cleanLegacyFieldPosts(eligible, reply.actor);
        for (const post of obsolete) {
          try {
            localStorage.setItem(fieldSyncKey + ':cleanup:' + post.id, JSON.stringify(post));
            if (ownedIds.has(post.id)) fieldPostMarkDeleted(post);
            createdFieldPosts.splice(createdFieldPosts.indexOf(post), 1);
            changed = true;
          } catch (error) {
            console.warn('No se pudo respaldar el poste antes de depurarlo.', error);
          }
        }
      }
    }
    if (changed) {
      fieldPostPersist();
      renderCreatedFieldPosts();
    }
    document.getElementById('fieldpost-sync-status').textContent = reply.error || (reply.remaining
      ? 'Sincronizando · quedan '+reply.remaining+' postes'
      : reply.conflicts?.length ? 'Se cargó la versión actualizada del otro equipo. Tu cambio anterior quedó respaldado en este navegador.' : 'Postes sincronizados entre equipos');
    fieldSyncApplied = reply.id;
    fieldSyncPending = null;
    return null;
  }
  if (fieldSyncPending) return null;
  const operations = [];
  const postIds = new Set(createdFieldPosts.map(post => post.id));
  for (const [id, known] of Object.entries(fieldSyncState)) {
    if (operations.length >= 20) break;
    if (known.deletePending || (!known.deleted && !postIds.has(id))) {
      operations.push({post:{id,visibility:'private'}, revision:known.revision, deleted:true, signature:''});
    }
  }
  let payloadSize = 0;
  for (const post of createdFieldPosts) {
    if (operations.length >= 20) break;
    const known = fieldSyncState[post.id];
    if (known?.deleted || known?.deletePending) continue;
    if (post.kind !== 'style' && (fieldPostRetiredCode(post) || !fieldPostCode(post) || !fieldPostValidLocation(post.lat, post.lon))) continue;
    if (!known?.revision && post.kind !== 'style' && fieldPostDuplicates(post).length > 1) continue;
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
    const operation = {post:{...post, syncPhotos:photos}, signature, revision:known?.revision || 0};
    const size = JSON.stringify(operation).length;
    if (operations.length && payloadSize + size > 8 * 1024 * 1024) break;
    operations.push(operation);
    payloadSize += size;
  }
  const revisions = Object.fromEntries(Object.entries(fieldSyncState).map(([id, known]) => [id, known.revision]));
  const photosNeeded = createdFieldPosts.filter(post => fieldSyncState[post.id]?.photosDeferred && fieldPostValidLocation(post.lat, post.lon)).map(post => post.id);
  fieldSyncPending = {id:crypto.randomUUID(), operations, revisions, photosNeeded};
  if (operations.length) document.getElementById('fieldpost-sync-status').textContent = 'Sincronizando cambios de postes…';
  return fieldSyncPending;
}
