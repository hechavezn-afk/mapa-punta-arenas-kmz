function fieldPostValidLocation(latitude, longitude) {
  if (latitude == null || longitude == null || String(latitude).trim() === '' || String(longitude).trim() === '') return false;
  const lat = Number(latitude), lon = Number(longitude);
  return Number.isFinite(lat) && Number.isFinite(lon) && lat >= -56.5 && lat <= -48 && lon >= -76 && lon <= -65;
}
function fieldPostCode(post) {
  return String(post.number || '').replace(/\s+/g, '').toUpperCase();
}
function fieldPostRetiredCode(post, actor = LOGIN_USER) {
  return String(actor).toUpperCase() === 'HECTOR' && fieldPostCode(post) === '1-0390-043';
}
function fieldPostVisibleRecord(post) {
  return post.kind !== 'style' && !fieldPostRetiredCode(post) && !fieldPostRetiredCode(post, post.owner || '');
}
function fieldPostDuplicates(post) {
  const code = fieldPostCode(post);
  return code ? createdFieldPosts.filter(other => other.kind !== 'style' && fieldPostCode(other) === code) : [];
}
function fieldPostNumberError(number, editingId = '') {
  const code = fieldPostCode({number});
  if (!code) return 'Ingresa el número del poste.';
  if (fieldPostRetiredCode({number})) return 'El número 1-0390-043 fue retirado. Ingresa otro número.';
  if (fieldPostDuplicates({number}).some(post => post.id !== editingId)) {
    return 'El número ' + number.trim() + ' ya existe en Postes creados. Abre ese poste para modificarlo.';
  }
  return '';
}
function fieldPostWarnings(post) {
  const warnings = [];
  if (!fieldPostValidLocation(post.lat, post.lon)) warnings.push('Coordenadas fuera de Magallanes');
  if (fieldPostDuplicates(post).length > 1) warnings.push('Código repetido');
  return warnings;
}
function cleanLegacyFieldPosts(posts, actor) {
  const owned = posts.filter(post => post.kind !== 'style' && (!post.owner || post.owner === actor));
  const removed = new Set(owned.filter(post => fieldPostRetiredCode(post, actor) || !fieldPostValidLocation(post.lat, post.lon)));
  const groups = new Map();
  for (const post of owned) {
    const code = fieldPostCode(post);
    if (!code || removed.has(post)) continue;
    const visibility = post.visibility || 'private';
    const key = JSON.stringify([code, visibility, visibility === 'user' ? post.sharedWith : '']);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(post);
  }
  for (const group of groups.values()) {
    group.sort((a, b) => (b.photos?.length || 0) - (a.photos?.length || 0)
      || String(b.updatedAt || '').localeCompare(String(a.updatedAt || ''))
      || String(a.id).localeCompare(String(b.id)));
    const keep = [];
    for (const post of group) {
      const duplicate = keep.some(other =>
        distanceBetween([Number(post.lat), Number(post.lon)], [Number(other.lat), Number(other.lon)]) <= 8
        && (post.photos || []).every(id => (other.photos || []).includes(id)));
      if (duplicate) removed.add(post);
      else keep.push(post);
    }
  }
  return removed;
}
function polePhases(point) {
  if (point.phases === '') return '';
  const attrs = point.attrs || point.attributes || point;
  const value = point.phases ?? attrs.NUMERO_FASES ?? attrs.CANTIDAD_FASES;
  const number = String(value ?? '').trim();
  if (/^[123](?:F|\.0)?$/i.test(number)) return number[0] + 'F';
  const names = String(attrs.NOMBRE_FASES || '').toUpperCase().replace(/[\s,;\-/]+/g, '');
  if (!/^[RSTABCN]+$/.test(names)) return '';
  const count = new Set(names.replace(/N/g, '')).size;
  return count >= 1 && count <= 3 ? count + 'F' : '';
}
function poleNetworkLabel(point) {
  return [point.red || '', polePhases(point)].filter(Boolean).join(' ');
}
function fieldPostIcon(point = {}) {
  const duplicate = point.id && fieldPostDuplicates(point).length > 1;
  const general = createdFieldPosts.find(p => p.kind === 'style' && p.id === 'global-field-post-icon');
  const icon = point.icon || general?.icon;
  const zoom = map.getZoom(), size = 2 * inventorySymbolRadius(zoom), anchor = size / 2;
  const ring = duplicate ? 'outline:2px solid #c62828;outline-offset:-2px;' : '';
  let html;
  if (!icon || !/^data:image\/(png|jpeg|webp);base64,/.test(icon)) {
    const color = zoom >= 13 ? '#25313e' : '#fff';
    const glyph = zoom >= 13 ? '<i style="position:absolute;inset:15% 45%;background:#59420b"></i><i style="position:absolute;inset:30% 14% auto;height:10%;background:#59420b"></i><i style="position:absolute;inset:18% 28% auto;height:9%;background:#59420b"></i>' : '';
    html = '<span style="display:block;position:relative;width:100%;height:100%;border-radius:50%;background:#ffca28;box-shadow:inset 0 0 0 1px '+color+';'+ring+'">'+glyph+'</span>';
  } else {
    html = '<img alt="Poste" src="'+esc(icon)+'" style="'+ring+'border-radius:50%;width:100%;height:100%;object-fit:contain">';
  }
  return L.divIcon({className:'survey-div-icon', html, iconSize:[size,size], iconAnchor:[anchor,anchor]});
}
function showFieldPostPhoto(blob) {
  const dialog = document.createElement('dialog');
  dialog.style.cssText = 'padding:10px;border:0;border-radius:12px;max-width:96vw;max-height:94dvh;background:#17202a;color:#fff';
  const close = document.createElement('button');
  close.type = 'button';close.textContent = 'Cerrar ×';
  close.style.cssText = 'display:block;margin:0 0 8px auto;padding:10px 16px;cursor:pointer';
  const image = document.createElement('img'), url = URL.createObjectURL(blob);
  image.src = url;image.alt = 'Foto del poste ampliada';
  image.style.cssText = 'display:block;max-width:90vw;max-height:78dvh;object-fit:contain';
  dialog.append(close,image);document.body.append(dialog);
  close.onclick = () => dialog.close();
  dialog.addEventListener('close',()=>{URL.revokeObjectURL(url);dialog.remove()},{once:true});
  dialog.showModal();
}
async function setFieldPostIcon(file, global) {
  if (!file) return;
  if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size>2*1024*1024) {
    fieldPostStatus('Elige un icono PNG, JPG o WEBP de hasta 2 MB.');return;
  }
  const bitmap=await createImageBitmap(file),canvas=document.createElement('canvas');
  canvas.width=canvas.height=96;
  const ctx=canvas.getContext('2d'),scale=Math.min(96/bitmap.width,96/bitmap.height);
  ctx.drawImage(bitmap,(96-bitmap.width*scale)/2,(96-bitmap.height*scale)/2,bitmap.width*scale,bitmap.height*scale);
  bitmap.close?.();const icon=canvas.toDataURL('image/png');
  if (global) {
    let style=createdFieldPosts.find(p=>p.id==='global-field-post-icon');
    if (!style) {style={id:'global-field-post-icon',kind:'style',lat:0,lon:0,photos:[],visibility:'global',sharedWith:''};createdFieldPosts.push(style)}
    style.icon=icon;style.updatedAt=new Date().toISOString();fieldPostPersist();renderCreatedFieldPosts();
  } else {
    document.getElementById('fieldpost-icon-preview').src=icon;
    document.getElementById('fieldpost-icon-preview').hidden=false;
    fieldPostDraftDirty=true;fieldPostDraftMarker?.setIcon(fieldPostIcon({icon}));
  }
}
function initFieldPostExtras() {
  const numberInput = document.getElementById('fieldpost-number');
  numberInput.addEventListener('input', () => {
    const warning = document.getElementById('fieldpost-number-warning');
    warning.textContent = numberInput.value.trim() ? fieldPostNumberError(numberInput.value, fieldPostEditingId) : '';
    numberInput.setAttribute('aria-invalid', String(Boolean(warning.textContent)));
  });
  numberInput.addEventListener('change', event => {
    const code = event.target.value.trim().toUpperCase();
    if (!code || document.getElementById('fieldpost-phases').value) return;
    const matches = pointAssets.filter(asset => [asset.attrs?.IDENTIFICADOR_VNR, asset.attrs?.POSTE_ID, asset.name]
      .some(value => String(value || '').trim().toUpperCase() === code));
    const phases = new Set(matches.map(asset => polePhases(asset.attrs || {})).filter(Boolean));
    if (phases.size === 1) document.getElementById('fieldpost-phases').value = [...phases][0];
  });
  document.getElementById('fieldpost-icon-file').onchange=e=>setFieldPostIcon(e.target.files[0],false).catch(()=>fieldPostStatus('No se pudo leer el icono.'));
  document.getElementById('fieldpost-global-icon-file').onchange=e=>setFieldPostIcon(e.target.files[0],true).catch(()=>fieldPostStatus('No se pudo leer el icono.'));
  document.getElementById('fieldpost-icon-clear').onclick=()=>{
    const preview=document.getElementById('fieldpost-icon-preview');preview.removeAttribute('src');preview.hidden=true;
    fieldPostDraftDirty=true;fieldPostDraftMarker?.setIcon(fieldPostIcon());
  };
}
