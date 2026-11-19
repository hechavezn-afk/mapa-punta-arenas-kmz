function municipalSubstation(feature) {
  const group = norm(feature.municipal_group || '').replace(/[^a-z]/g, '');
  return group === 'ssee' || /subestacion|transformador/.test(group)
    || /^s\s*\/?\s*e\s*\d/i.test(String(feature.name || '').trim());
}

function municipalLineStyle(feature) {
  const original = String(feature.line_color || '').toLowerCase();
  const group = norm(feature.municipal_group || '').replace(/[^a-z]/g, '');
  const color = /^#[0-9a-f]{6}$/.test(original) && original !== '#607d8b'
    ? original : group === 'mt' ? '#ffff00' : group.includes('camino') ? '#ff3737' : '#0000ff';
  const weight = color === '#0000ff' ? 6 : color === '#ffff00' ? 4.5 : 3.5;
  return {color, weight};
}

function addMunicipalLines(features, group) {
  const batches = new Map();
  for (const feature of features) {
    const style = municipalLineStyle(feature);
    const key = style.color + ':' + style.weight;
    if (!batches.has(key)) batches.set(key, {...style, coordinates:[]});
    batches.get(key).coordinates.push(feature.coordinates.map(c => [c[1], c[0]]));
  }
  for (const batch of batches.values()) {
    group.addLayer(L.polyline(batch.coordinates, {
      renderer:municipalLineRenderer, color:batch.color, weight:batch.weight,
      opacity:1, interactive:false, lineCap:'round', lineJoin:'round'
    }));
  }
}
