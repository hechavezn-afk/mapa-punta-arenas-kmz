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
  const general = createdFieldPosts.find(p => p.kind === 'style' && p.id === 'global-field-post-icon');
  const icon = point.icon || general?.icon;
  if (!icon || !/^data:image\/(png|jpeg|webp);base64,/.test(icon)) return surveyIcon('poste');
  return L.divIcon({className:'survey-div-icon', html:'<img alt="Poste" src="'+esc(icon)+'" style="width:30px;height:30px;object-fit:contain">', iconSize:[30,30], iconAnchor:[15,15]});
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
  document.getElementById('fieldpost-number').addEventListener('change', event => {
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
