from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import streamlit as st
import streamlit.components.v1 as components


st.set_page_config(page_title="Mapa Punta Arenas", page_icon="🌎", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""
<style>
html, body, #root, [data-testid="stAppViewContainer"], .stApp {width:100%!important;height:100dvh!important;min-height:100dvh!important;margin:0!important;padding:0!important;background:#f3f6f8!important;overflow:hidden!important}
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"], #MainMenu, footer {display:none!important;height:0!important;visibility:hidden!important}
[data-testid="stMain"], [data-testid="stMainBlockContainer"] {position:relative!important;width:100vw!important;max-width:100vw!important;height:100dvh!important;min-height:100dvh!important;margin:0!important;padding:0!important}
[data-testid="stVerticalBlock"] {gap:0!important}
[data-testid="stElementContainer"]:has(iframe[title="st.iframe"]) {position:fixed!important;inset:0!important;width:100vw!important;height:100dvh!important;margin:0!important;padding:0!important;z-index:1!important}
iframe[title="st.iframe"] {position:absolute!important;inset:0!important;display:block!important;width:100vw!important;height:100dvh!important;max-height:100dvh!important;border:0!important}
</style>
""", unsafe_allow_html=True)
DATA_DIR = Path(__file__).parent
KML_NS = {"k": "http://www.opengis.net/kml/2.2"}


@st.cache_data(show_spinner="Preparando las capas del mapa…")
def read_kmz(filename: str) -> dict:
    """Read regular KMZ archives and KML files mistakenly named .kmz."""
    path = DATA_DIR / filename
    raw = path.read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(path) as archive:
            kml_name = next(n for n in archive.namelist() if n.lower().endswith(".kml"))
            raw = archive.read(kml_name)
    # These two concession files declare UTF-8 but contain legacy Windows-1252 bytes.
    try:
        xml = raw.decode("utf-8")
        ET.fromstring(xml)  # Some malformed exports declare UTF-8 but contain cp1252 bytes.
    except (UnicodeDecodeError, ET.ParseError):
        xml = raw.decode("cp1252")
    root = ET.fromstring(xml)
    styles = {s.get("id"): s for s in root.findall(".//k:Document/k:Style", KML_NS)}
    style_maps = {}
    for style_map in root.findall(".//k:Document/k:StyleMap", KML_NS):
        for pair in style_map.findall("k:Pair", KML_NS):
            if pair.findtext("k:key", default="", namespaces=KML_NS) == "normal":
                style_maps[style_map.get("id")] = pair.findtext("k:styleUrl", default="", namespaces=KML_NS).lstrip("#")
    out = {"groups": {}, "polygons": []}

    def coordinates(node):
        text = node.findtext("k:coordinates", default="", namespaces=KML_NS)
        result = []
        for pair in text.split():
            bits = pair.split(",")
            if len(bits) >= 2:
                try:
                    result.append([float(bits[0]), float(bits[1])])
                except ValueError:
                    continue
        return result

    def group_for(placemark):
        folders = []
        parent = parents.get(placemark)
        while parent is not None:
            if parent.tag.rsplit("}", 1)[-1] == "Folder":
                folders.append(parent.findtext("k:name", default="", namespaces=KML_NS))
            parent = parents.get(parent)
        folders.reverse()
        return folders[1] if len(folders) > 1 else (folders[0] if folders else "Red eléctrica")

    parents = {child: parent for parent in root.iter() for child in parent}
    for placemark in root.findall(".//k:Placemark", KML_NS):
        name = placemark.findtext("k:name", default="", namespaces=KML_NS)
        description = placemark.findtext("k:description", default="", namespaces=KML_NS)
        style_id = placemark.findtext("k:styleUrl", default="", namespaces=KML_NS).lstrip("#")
        style_id = style_maps.get(style_id, style_id)
        style = styles.get(style_id)
        icon = style.findtext("k:IconStyle/k:Icon/k:href", default="", namespaces=KML_NS) if style is not None else ""
        point_color = style.findtext("k:IconStyle/k:color", default="", namespaces=KML_NS) if style is not None else ""
        line_color = style.findtext("k:LineStyle/k:color", default="", namespaces=KML_NS) if style is not None else ""
        if not point_color:
            point_color = placemark.findtext("k:Style/k:IconStyle/k:color", default="", namespaces=KML_NS)
        if not line_color:
            line_color = placemark.findtext("k:Style/k:LineStyle/k:color", default="", namespaces=KML_NS)

        def css_color(kml_color, fallback):
            value = (kml_color or "").strip().lstrip("#")
            if len(value) == 8:
                return "#" + value[6:8] + value[4:6] + value[2:4]
            return fallback

        group = group_for(placemark)
        bucket = out["groups"].setdefault(group, {"points": [], "lines": []})
        fallback = {"Postes": "#ffca28", "Equipos": "#26a69a", "Subestaciones": "#ab47bc", "Redes MT": "#2962ff", "Red BT": "#fdd835"}.get(group, "#607d8b")
        attributes = {}
        for data in placemark.findall(".//k:Data", KML_NS):
            value = data.findtext("k:value", default="", namespaces=KML_NS)
            if data.get("name") and value:
                attributes[data.get("name")] = value
        for data in placemark.findall(".//k:SimpleData", KML_NS):
            if data.get("name") and data.text:
                attributes[data.get("name")] = data.text.strip()
        props = {"name": name, "description": description, "icon": icon,
                 "attributes": attributes, "color": css_color(point_color, fallback), "line_color": css_color(line_color, fallback)}
        for point in placemark.findall(".//k:Point", KML_NS):
            c = coordinates(point)
            if c:
                bucket["points"].append({**props, "coordinates": c[0]})
        for line in placemark.findall(".//k:LineString", KML_NS):
            c = coordinates(line)
            if len(c) > 1:
                bucket["lines"].append({**props, "coordinates": c})
        for polygon in placemark.findall(".//k:Polygon", KML_NS):
            rings = [coordinates(r) for r in polygon.findall(".//k:LinearRing", KML_NS)]
            rings = [r for r in rings if len(r) > 2]
            if rings:
                out["polygons"].append({**props, "coordinates": rings})
    return out


NETWORK = "Punta Arenas.kmz"
CONCESSION_LT = "concesiones2025v3 corregidas menor a 15.kmz"
CONCESSION_GE = "concesiones2025v3 GEO superior a 14.kmz"

try:
    network = read_kmz(NETWORK)
    concession_lt = read_kmz(CONCESSION_LT)
    concession_ge = read_kmz(CONCESSION_GE)
except Exception as exc:
    st.error(f"No se pudieron preparar las capas: {exc}")
    st.stop()

layers = [
    {"id": "network-" + str(i), "name": name,
     "color": {"Equipos": "#26a69a", "Postes": "#ffca28", "Subestaciones": "#ab47bc", "Redes MT": "#ff3030", "Red BT": "#1646ff"}.get(name, "#607d8b"),
     "data": {"points": group["points"], "lines": group["lines"], "polygons": []}}
    for i, (name, group) in enumerate(network["groups"].items())
    if group["points"] or group["lines"]
]
layers += [
    {"id": "lt15", "name": "Concesión menor a 15", "color": "#26c6da",
     "data": {"points": [], "lines": [], "polygons": concession_lt["polygons"]}},
    {"id": "ge14", "name": "Concesión GEO superior a 14", "color": "#ff7043",
     "data": {"points": [], "lines": [], "polygons": concession_ge["polygons"]}},
]

# Keep raw feature attributes in the browser search index; Leaflet renders the
# dense electrical network through a shared Canvas renderer.
layers_json = json.dumps(layers, ensure_ascii=False, separators=(",", ":"))
html = r"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box}html,body{margin:0;height:100%;font-family:Arial,sans-serif;color:#202124}#map{position:absolute;inset:0;background:#dbe5e5}
.panel{position:absolute;z-index:1000;top:12px;left:12px;width:min(340px,calc(100vw - 24px));max-height:calc(100% - 24px);overflow:auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px #0003;padding:14px;transition:opacity .16s ease,transform .16s ease}
.panel.is-hidden{display:none}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}.panel-head-main{min-width:0}.panel-close,.panel-open{border:0;background:#f1f3f4;color:#3c4043;border-radius:9px;font-size:20px;line-height:1;cursor:pointer;touch-action:manipulation}.panel-close{flex:none;width:32px;height:32px;margin:-3px -3px 0 0}.panel-open{position:absolute;z-index:1002;top:12px;left:12px;padding:9px 12px;background:rgba(31,78,121,.78);color:#fff;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.55);box-shadow:0 2px 10px #0003;font-size:14px;font-weight:700;min-height:42px;min-width:44px}.panel-open[hidden]{display:none}
.leaflet-top.leaflet-left{top:auto;bottom:12px;left:12px}
.title{font-size:18px;font-weight:700;margin-bottom:3px}.sub{font-size:12px;color:#5f6368;margin-bottom:8px}.searchbox{margin:10px 0}.searchbox input{width:100%;padding:10px 11px;border:1px solid #dadce0;border-radius:9px;font-size:14px}.search-actions{display:flex;gap:6px;margin-top:7px}.search-actions button{flex:1;border:1px solid #dadce0;border-radius:8px;background:#fff;padding:8px 6px;font-weight:600;color:#3c4043}.search-results{max-height:170px;overflow:auto}.result{width:100%;text-align:left;background:#fff;border:0;border-top:1px solid #eee;padding:9px 6px;font-size:13px;color:#202124}.result small{display:block;color:#70757a;margin-top:3px}.search-status{font-size:12px;color:#5f6368;margin:6px 2px}.layer-heading{font-size:12px;font-weight:700;color:#5f6368;margin-top:10px}.row{display:flex;align-items:center;gap:9px;padding:8px 4px;border-top:1px solid #eee;font-size:14px}.row input{width:18px;height:18px;accent-color:#4285f4}.swatch{width:11px;height:11px;border-radius:50%;flex:none}.count{display:block;color:#70757a;font-size:11px;margin-left:28px;margin-top:-6px;padding-bottom:7px}.map-style{position:absolute;z-index:1001;right:12px;bottom:34px;display:flex;padding:3px;gap:2px;background:rgba(255,255,255,.94);border:1px solid #d9dfe5;border-radius:12px;box-shadow:0 2px 10px #0003;backdrop-filter:blur(8px)}.map-style button{border:0;border-radius:9px;padding:9px 12px;background:transparent;color:#39434e;font-size:13px;font-weight:700;white-space:nowrap;cursor:pointer;touch-action:manipulation}.map-style button.active{background:#1f4e79;color:#fff}.map-style button:focus-visible{outline:2px solid #4285f4;outline-offset:1px}
.icon-key{display:flex;flex-wrap:wrap;gap:5px 10px;margin:2px 0 9px;font-size:11px;color:#48515c}.icon-key span{display:inline-flex;align-items:center;gap:4px}.key-icon{width:17px;height:17px;display:inline-flex;align-items:center;justify-content:center;border:1.5px solid #273444;color:white;font-size:10px;font-weight:800;background:#607d8b;border-radius:50%;font-style:normal}.key-post{background:#ffca28;color:#4b3b0b}.key-f{background:#ef6c00;border-radius:3px}.key-d{background:#008fa1;transform:rotate(45deg);border-radius:2px}.key-d b{transform:rotate(-45deg)}.key-r{background:#c62828}.key-se{background:#7b1fa2;border-radius:3px;font-size:8px}
@media(max-width:600px){.panel{top:8px;left:8px;padding:9px;width:min(276px,calc(100vw - 64px));max-height:52vh;border-radius:10px}.title{font-size:15px}.sub{font-size:11px;margin-bottom:5px}.panel-close{width:30px;height:30px}.row{padding:5px 3px;font-size:13px}.count{padding-bottom:4px}.searchbox{margin:6px 0}.searchbox input{padding:9px;font-size:16px}.search-actions button{padding:7px 4px;font-size:12px}.search-results{max-height:105px}.icon-key{gap:4px 6px;margin-bottom:5px;font-size:10px}.key-icon{width:16px;height:16px}.layer-heading{margin-top:7px}.panel-open{top:8px;left:8px;padding:8px 10px;min-height:40px}.leaflet-top.leaflet-left{bottom:12px;left:10px}.map-style{right:8px;bottom:38px;padding:2px}.map-style button{padding:8px 9px;font-size:12px}}
</style></head><body><div id="map"></div><div class="panel" id="layer-panel"><div class="panel-head"><div class="panel-head-main"><div class="title">🌎 Punta Arenas</div><div class="sub">Símbolos eléctricos · pulsa un activo para ver sus datos</div></div><button class="panel-close" id="panel-close" type="button" aria-label="Cerrar búsqueda y capas" title="Ocultar panel">×</button></div><div class="icon-key"><span><i class="key-icon key-post">┼</i>Poste</span><span><i class="key-icon key-f">F</i>Fusible</span><span><i class="key-icon key-d"><b>D</b></i>Desconectador</span><span><i class="key-icon key-r">R</i>Reconectador</span><span><i class="key-icon key-se">SE</i>Subestación</span></div><div class="searchbox"><input id="search" type="search" placeholder="Dirección, cruce, activo o coordenadas"><div class="search-actions"><button id="search-go">Buscar</button><button id="gps">◎ Mi ubicación</button></div><div class="search-status" id="search-status">Ej.: Ovejero 084 · Ovejero &amp; Av. España</div><div class="search-results" id="search-results"></div></div><div class="layer-heading">Capas</div><div id="layer-list"></div></div><button class="panel-open" id="panel-open" type="button" aria-label="Abrir búsqueda y capas" title="Mostrar búsqueda y capas" hidden>⌕ Buscar / capas</button><div class="map-style" role="group" aria-label="Estilo del mapa"><button id="style-satellite" type="button" aria-pressed="true">🛰️ Satélite</button><button id="style-streets" type="button" aria-pressed="false">🗺️ Normal</button></div>
<script>
const layers=__LAYERS__;
const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'});
const satellite=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles © Esri'});
const map=L.map('map',{preferCanvas:true,doubleClickZoom:false,layers:[satellite]}).setView([-53.15,-70.92],10);
const renderer=L.canvas({padding:0.5});let satelliteOn=true,gpsMarker=null,selectedMarker=null;const panel=document.getElementById('layer-panel'),openPanelButton=document.getElementById('panel-open'),searchInput=document.getElementById('search'),searchStatus=document.getElementById('search-status'),searchResults=document.getElementById('search-results');let searchIndex=[];
function hidePanel(){panel.classList.add('is-hidden');openPanelButton.hidden=false}
function showPanel(){panel.classList.remove('is-hidden');openPanelButton.hidden=true}
L.DomEvent.disableClickPropagation(panel);L.DomEvent.disableScrollPropagation(panel);L.DomEvent.disableClickPropagation(openPanelButton);
document.getElementById('panel-close').onclick=hidePanel;openPanelButton.onclick=showPanel;map.on('click',hidePanel);
function symbolType(layer,attrs={}){if(layer==='Postes')return 'poste';if(layer==='Subestaciones')return 'se';if(layer==='Equipos'){const t=norm(attrs.TIPO_LMDS);if(t.includes('fusible'))return 'fuse';if(t.includes('desconectador'))return 'disconnect';if(t.includes('reconectador'))return 'recloser'}return ''}
const originalCircle=renderer._updateCircle;renderer._updateCircle=function(layer){const type=layer.options.assetIcon;if(!type)return originalCircle.call(this,layer);if(!this._drawing||layer._empty())return;const ctx=this._ctx,p=layer._point,z=map.getZoom(),r=z>=15?9.5:z>=13?7:3.2;const colors={poste:'#ffca28',fuse:'#ef6c00',disconnect:'#008fa1',recloser:'#c62828',se:'#7b1fa2'};ctx.save();ctx.translate(p.x,p.y);ctx.lineWidth=z>=15?1.7:z>=13?1.2:0.8;ctx.strokeStyle=z>=13?'#25313e':'#fff';ctx.fillStyle=colors[type]||'#607d8b';ctx.beginPath();if(z<13)ctx.arc(0,0,r,0,Math.PI*2);else if(type==='disconnect'){ctx.moveTo(0,-r);ctx.lineTo(r,0);ctx.lineTo(0,r);ctx.lineTo(-r,0);ctx.closePath()}else if(type==='fuse'||type==='se'){ctx.rect(-r*.82,-r*.82,r*1.64,r*1.64)}else ctx.arc(0,0,r,0,Math.PI*2);ctx.fill();ctx.stroke();if(z>=13){ctx.strokeStyle=type==='poste'?'#59420b':'#fff';ctx.fillStyle=type==='poste'?'#59420b':'#fff';ctx.lineWidth=z>=15?1.8:1.2;ctx.lineCap='round';ctx.lineJoin='round';if(type==='poste'){ctx.beginPath();ctx.moveTo(0,-r*.72);ctx.lineTo(0,r*.72);ctx.moveTo(-r*.68,-r*.25);ctx.lineTo(r*.68,-r*.25);ctx.moveTo(-r*.38,-r*.48);ctx.lineTo(r*.38,-r*.48);ctx.stroke()}else if(type==='fuse'){ctx.beginPath();ctx.moveTo(-r*.48,-r*.42);ctx.lineTo(r*.48,-r*.42);ctx.moveTo(-r*.48,r*.42);ctx.lineTo(r*.48,r*.42);ctx.moveTo(0,-r*.42);ctx.lineTo(0,r*.42);ctx.stroke();if(z>=15){ctx.font='bold 10px Arial';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('F',0,r*.66)}}else if(type==='disconnect'){ctx.beginPath();ctx.moveTo(-r*.48,r*.45);ctx.lineTo(r*.4,-r*.42);ctx.moveTo(r*.32,-r*.48);ctx.lineTo(r*.56,-r*.3);ctx.stroke();ctx.beginPath();ctx.arc(-r*.53,r*.52,r*.13,0,Math.PI*2);ctx.fill();if(z>=15){ctx.font='bold 10px Arial';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('D',0,r*.63)}}else if(type==='recloser'){ctx.beginPath();ctx.arc(0,0,r*.43,-Math.PI*.78,Math.PI*.78);ctx.moveTo(-r*.22,-r*.45);ctx.lineTo(-r*.48,-r*.38);ctx.lineTo(-r*.37,-r*.13);ctx.stroke();if(z>=15){ctx.font='bold 10px Arial';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('R',0,r*.08)}}else if(type==='se'){ctx.beginPath();ctx.moveTo(-r*.52,r*.42);ctx.lineTo(-r*.52,-r*.15);ctx.lineTo(0,-r*.56);ctx.lineTo(r*.52,-r*.15);ctx.lineTo(r*.52,r*.42);ctx.moveTo(-r*.22,r*.42);ctx.lineTo(-r*.22,r*.05);ctx.lineTo(r*.22,r*.05);ctx.lineTo(r*.22,r*.42);ctx.stroke();if(z>=15){ctx.fillStyle='#fff';ctx.font='bold 6px Arial';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('SE',0,r*.68)}}}ctx.restore()};
function norm(v){return String(v||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase()}
function substationNumber(v){const s=String(v||'').trim();return /^10\d+$/.test(s)?s.slice(2):s}
function searchableAttrs(a={}){return Object.entries(a).map(([k,v])=>[k.toUpperCase()==='SE'||k.toUpperCase()==='SUBESTACION'?substationNumber(v):v, v]).flat().join(' ')}
function esc(v){return String(v||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function popupFields(layer,attrs){const fields=[],put=(label,...keys)=>{for(const key of keys){const value=attrs[key];if(value!==undefined&&String(value).trim()&&!['0','#N/D','null'].includes(String(value).trim())){fields.push([label,String(value).trim()]);return}}},putSubstation=(...keys)=>{for(const key of keys){const value=attrs[key];if(value!==undefined&&String(value).trim()){fields.push(['Subestación',substationNumber(value)]);return}}};
 if(layer==='Postes'){put('N° de poste','IDENTIFICADOR_VNR','POSTE_ID');putSubstation('SUBESTACION','SE');put('Alimentador','ALIMENTADOR','ALIMENTADOR_ID');let voltage=attrs['TIPO_TENSION_ID'];if(voltage){const v=String(voltage).trim().toUpperCase();if(v==='MT'||v==='BT')fields.push(['Red',v]);else if(v==='AMBAS'||v==='MT/BT'||v==='BT/MT')fields.push(['Red','MT y BT'])}}
 else if(layer==='Equipos'){put('Equipo','DENOMINACION','EQUIPO_ID');put('Tipo','TIPO_LMDS');putSubstation('SUBESTACION','SE');put('Alimentador','ALIMENTADOR_ID')}
 else if(layer==='Subestaciones'){putSubstation('SE');put('N° de transformador','TRANSFORMADOR_ID');put('Alimentador','ALIMENTADOR_ID');put('Potencia (kVA)','KVA')}
 else if(layer==='Redes MT'){put('Tramo MT','TRAMO_MT_ID');put('Alimentador','ALIMENTADOR_ID');put('Longitud (m)','LARGO_RED')}
 else if(layer==='Red BT'){put('Tramo BT','TRAMO_BT_ID');put('Alimentador','ALIMENTADOR_ID');put('Longitud (m)','LARGO_RED')}
 else for(const [k,v] of Object.entries(attrs).filter(([k])=>k.toUpperCase()!=='CODIGO_VNR').slice(0,8))if(v)fields.push([k,v]);return fields.map(([k,v])=>'<div><b>'+esc(k)+':</b> '+esc(v)+'</div>').join('')}
function assetTitle(layer,a,name){if(name&&layer!=='Subestaciones')return name;if(layer==='Postes')return a.IDENTIFICADOR_VNR||a.POSTE_ID||layer;if(layer==='Equipos')return a.DENOMINACION||a.EQUIPO_ID||layer;if(layer==='Subestaciones')return substationNumber(a.SE)||a.TRANSFORMADOR_ID||layer;if(layer==='Redes MT')return a.TRAMO_MT_ID||layer;if(layer==='Red BT')return a.TRAMO_BT_ID||layer;return a.NOMBRE_CONCESION||a.ID||Object.values(a)[0]||layer}
function assetSubtitle(layer,a){if(layer==='Postes')return a.TIPO_TENSION_ID||'';if(layer==='Equipos')return a.TIPO_LMDS||a.EQUIPO_ID||'';if(layer==='Subestaciones')return [a.TRANSFORMADOR_ID?'Trafo '+a.TRANSFORMADOR_ID:'',a.ALIMENTADOR_ID?'Alim. '+a.ALIMENTADOR_ID:''].filter(Boolean).join(' · ');if(layer==='Redes MT'||layer==='Red BT')return [a.ALIMENTADOR_ID?'Alim. '+a.ALIMENTADOR_ID:'',a.LARGO_RED?a.LARGO_RED+' m':''].filter(Boolean).join(' · ');return a.NOMBRE_CONCESION||''}
const poleGlyph='<svg aria-hidden="true" width="19" height="25" viewBox="0 0 19 25" style="vertical-align:middle;margin-right:5px"><path d="M9.5 4v18M3 7h13M5 4h9M6 7l-2 3m9-3 2 3" fill="none" stroke="#765313" stroke-width="2" stroke-linecap="round"/><circle cx="9.5" cy="4" r="2" fill="#ffca28" stroke="#765313" stroke-width="1.2"/></svg>';
function coordOf(g){if(g.type==='Point')return [g.coordinates[1],g.coordinates[0]];if(g.type==='LineString'){const c=g.coordinates[Math.floor(g.coordinates.length/2)];return [c[1],c[0]]}if(g.type==='Polygon'){const r=g.coordinates[0]||[];if(!r.length)return null;return [r.reduce((a,c)=>a+c[1],0)/r.length,r.reduce((a,c)=>a+c[0],0)/r.length]}return null}
function goTo(item){const c=coordOf(item.geometry);if(!c)return;hidePanel();if(selectedMarker)selectedMarker.remove();const target=L.circleMarker(c,{renderer,radius:10,color:'#fff',weight:2,fillColor:'#4285f4',fillOpacity:1,assetIcon:symbolType(item.layer,item.attrs||{})}).addTo(map);selectedMarker=target;const head=item.layer==='Postes'?poleGlyph+'<b>Poste</b>':item.layer==='Equipos'?'<b>Equipo</b>':item.layer==='Subestaciones'?'<b>Subestación '+esc(substationNumber(item.attrs?.SE))+'</b>':'<b>'+esc(item.title)+'</b>';target.bindPopup(head+popupFields(item.layer,item.attrs||{}));const point=L.latLng(c[0],c[1]),alreadyThere=map.getZoom()===17&&map.getCenter().distanceTo(point)<2;if(alreadyThere)target.openPopup();else map.once('moveend',()=>target.openPopup());map.flyTo(c,17,{duration:0.9})}
function parseCoordinate(q){let m=q.match(/^\s*(-?\d+(?:[.,]\d+)?)\s*[,; ]\s*(-?\d+(?:[.,]\d+)?)\s*$/);if(m){let a=Number(m[1].replace(',','.')),b=Number(m[2].replace(',','.'));if(Math.abs(a)<=90&&Math.abs(b)<=180)return [b,a];if(Math.abs(a)<=180&&Math.abs(b)<=90)return [a,b]}const re=/(\d+(?:[.,]\d+)?)\s*°\s*(\d+(?:[.,]\d+)?)\s*['′]\s*(\d+(?:[.,]\d+)?)\s*["″]?\s*([NSEW])/ig,parts=[...q.matchAll(re)];if(parts.length>=2){const nums=parts.slice(0,2).map(x=>{let v=Number(x[1].replace(',','.'))+Number(x[2].replace(',','.'))/60+Number(x[3].replace(',','.'))/3600;if(/[SW]/i.test(x[4]))v=-v;return [x[4].toUpperCase(),v]});const lat=nums.find(x=>/[NS]/.test(x[0]))?.[1],lon=nums.find(x=>/[EW]/.test(x[0]))?.[1];if(lat!==undefined&&lon!==undefined)return [lon,lat]}return null}
function showResults(items){searchResults.innerHTML='';for(const item of items.slice(0,15)){const b=document.createElement('button');b.className='result';b.textContent=item.title||item.layer;const small=document.createElement('small');small.textContent=item.layer+(item.subtitle?' · '+item.subtitle:'');b.appendChild(small);b.onclick=()=>goTo(item);searchResults.appendChild(b)}if(items.length>15){const more=document.createElement('div');more.className='search-status';more.textContent='Mostrando 15 de '+items.length+' resultados';searchResults.appendChild(more)}}
function searchRank(item,q){const a=item.attrs||{},exact=(keys)=>keys.some(k=>norm(a[k])===q);if(item.layer==='Subestaciones'&&substationNumber(a.SE)&&norm(substationNumber(a.SE))===q)return 145;if(item.layer==='Subestaciones'&&exact(['SE']))return 140;if(item.layer==='Postes'&&exact(['IDENTIFICADOR_VNR','POSTE_ID']))return 135;if(item.layer==='Equipos'&&exact(['DENOMINACION','EQUIPO_ID']))return 135;if(item.layer==='Redes MT'&&exact(['TRAMO_MT_ID']))return 135;if(item.layer==='Red BT'&&exact(['TRAMO_BT_ID']))return 135;if(item.layer.startsWith('Concesión')&&exact(['ID','POLIGONAL_ID']))return 135;if(item.layer==='Subestaciones'&&norm(substationNumber(a.SE)).includes(q))return 110;if(item.layer==='Subestaciones'&&exact(['TRANSFORMADOR_ID']))return 105;if(item.layer==='Subestaciones'&&norm(a.TRANSFORMADOR_ID).includes(q))return 90;if(item.title&&norm(item.title)===q)return 100;if(item.layer==='Postes'&&norm(a.IDENTIFICADOR_VNR).includes(q))return 85;if(item.layer==='Equipos'&&(norm(a.DENOMINACION).includes(q)||norm(a.EQUIPO_ID).includes(q)))return 85;return 10}
async function runSearch(){const q=searchInput.value.trim();searchResults.innerHTML='';if(!q)return;const coord=parseCoordinate(q);if(coord){hidePanel();map.flyTo([coord[1],coord[0]],16,{duration:0.9});if(selectedMarker)selectedMarker.remove();selectedMarker=L.circleMarker([coord[1],coord[0]],{renderer,radius:9,color:'#fff',weight:3,fillColor:'#4285f4',fillOpacity:1}).addTo(map);searchStatus.textContent='Coordenadas: '+coord[1].toFixed(6)+', '+coord[0].toFixed(6);return}const nq=norm(q),hits=searchIndex.filter(x=>x.search.includes(nq)).sort((a,b)=>searchRank(b,nq)-searchRank(a,nq));if(hits.length){searchStatus.textContent=hits.length+' elementos encontrados. Toca un resultado para ir.';showResults(hits);return}searchStatus.textContent='Buscando dirección o lugar en Punta Arenas…';try{const localQuery=q.replace(/\s*&\s*/g,' y ')+' Punta Arenas Chile',params=new URLSearchParams({q:localQuery,lat:'-53.15',lon:'-70.92',zoom:'12',location_bias_scale:'0.1',countrycode:'CL',lang:'es',limit:'6'});const response=await fetch('https://photon.komoot.io/api/?'+params.toString(),{headers:{'Accept':'application/geo+json'}});if(!response.ok)throw new Error('servicio no disponible');const places=(await response.json()).features||[];if(!places.length){searchStatus.textContent='No encontré coincidencias en Punta Arenas.';return}searchStatus.textContent='Lugares encontrados · Photon / OpenStreetMap';showResults(places.map(p=>{const x=p.properties||{},label=[x.name,x.housenumber,x.street,x.district,x.city,x.state].filter(Boolean).join(', ');return{title:label||'Lugar en el mapa',layer:'Lugar',geometry:{type:'Point',coordinates:p.geometry.coordinates}}}))}catch(e){searchStatus.textContent='No pude buscar la dirección. Revisa tu conexión e inténtalo de nuevo.'}}
document.getElementById('search-go').onclick=runSearch;searchInput.addEventListener('keydown',e=>{if(e.key==='Enter')runSearch()});
document.getElementById('gps').onclick=()=>{if(!navigator.geolocation){searchStatus.textContent='Este navegador no ofrece GPS.';return}searchStatus.textContent='Solicitando ubicación al navegador…';navigator.geolocation.getCurrentPosition(pos=>{const c=[pos.coords.latitude,pos.coords.longitude];hidePanel();map.flyTo(c,17,{duration:0.9});if(gpsMarker)gpsMarker.remove();gpsMarker=L.marker(c).addTo(map).bindPopup('Tu ubicación actual').openPopup();searchStatus.textContent='Ubicación actual'},err=>{searchStatus.textContent=err.code===1?'Permite la ubicación. En el celular se requiere HTTPS para habilitar GPS.':'No se pudo obtener la ubicación GPS.'},{enableHighAccuracy:true,timeout:12000,maximumAge:30000})};
function setMapStyle(style){satelliteOn=style==='satellite';if(satelliteOn){map.removeLayer(osm);map.addLayer(satellite)}else{map.removeLayer(satellite);map.addLayer(osm)}document.getElementById('style-satellite').classList.toggle('active',satelliteOn);document.getElementById('style-satellite').setAttribute('aria-pressed',String(satelliteOn));document.getElementById('style-streets').classList.toggle('active',!satelliteOn);document.getElementById('style-streets').setAttribute('aria-pressed',String(!satelliteOn))}document.getElementById('style-satellite').onclick=()=>setMapStyle('satellite');document.getElementById('style-streets').onclick=()=>setMapStyle('streets');setMapStyle('satellite');
const layerControls=document.getElementById('layer-list'),allBounds=[];
for(const LYR of layers){const d=LYR.data,group=L.layerGroup(),color=LYR.color||'#607d8b';const items=[];
 for(const f of d.points){const props={name:f.name||'',attrs:f.attributes||{},description:f.description||''},text=[props.name,props.description,searchableAttrs(props.attrs)].join(' '),icon=symbolType(LYR.name,props.attrs);items.push({title:assetTitle(LYR.name,props.attrs,props.name),subtitle:assetSubtitle(LYR.name,props.attrs),layer:LYR.name,attrs:props.attrs,assetIcon:icon,search:norm(text+' '+LYR.name),geometry:{type:'Point',coordinates:f.coordinates}});const ll=[f.coordinates[1],f.coordinates[0]],isPole=LYR.name==='Postes',marker=L.circleMarker(ll,{renderer,radius:9,color:'#333',weight:1,fillColor:f.color||color,fillOpacity:0.98,assetIcon:icon});const head=isPole?poleGlyph+'<b>Poste</b>':LYR.name==='Equipos'?'<b>Equipo</b>':LYR.name==='Subestaciones'?'<b>Subestación '+esc(substationNumber(props.attrs.SE))+'</b>':'<b>'+esc(props.name||LYR.name)+'</b>';marker.bindPopup(head+popupFields(LYR.name,props.attrs));marker.on('click',hidePanel);marker.on('dblclick',e=>{L.DomEvent.stop(e);map.flyTo(ll,17,{duration:0.8});marker.openPopup()});group.addLayer(marker)}
 if(d.lines.length){const coords=d.lines.map(f=>f.coordinates.map(c=>[c[1],c[0]]));group.addLayer(L.polyline(coords,{renderer,color:LYR.id==='network-3'?'#ff3030':LYR.id==='network-4'?'#1646ff':color,weight:2,opacity:0.95,interactive:false}));for(const f of d.lines){const attrs=f.attributes||{},text=[f.name,f.description,searchableAttrs(attrs)].join(' ');items.push({title:assetTitle(LYR.name,attrs,f.name),subtitle:assetSubtitle(LYR.name,attrs),layer:LYR.name,attrs,search:norm(text+' '+LYR.name),geometry:{type:'LineString',coordinates:f.coordinates}})}}
 for(const f of d.polygons){const latlngs=f.coordinates.map(r=>r.map(c=>[c[1],c[0]]));const poly=L.polygon(latlngs,{renderer,color,weight:1.5,fillColor:color,fillOpacity:0.25,interactive:false});group.addLayer(poly);const attrs=f.attributes||{},text=[f.name,f.description,searchableAttrs(attrs)].join(' ');items.push({title:assetTitle(LYR.name,attrs,f.name),subtitle:assetSubtitle(LYR.name,attrs),layer:LYR.name,attrs,search:norm(text+' '+LYR.name),geometry:{type:'Polygon',coordinates:f.coordinates}})}
 for(const item of items){if(item.geometry.type==='Point'||item.geometry.type==='LineString')searchIndex.push(item);else searchIndex.push(item)}
 const defaultOn=LYR.id.startsWith('network-');const row=document.createElement('div');row.innerHTML='<label class="row"><input type="checkbox" '+(defaultOn?'checked':'')+'><span class="swatch" style="background:'+color+'"></span><span>'+LYR.name+'</span></label><span class="count">'+items.length.toLocaleString('es-CL')+' elementos</span>';const check=row.querySelector('input');check.onchange=()=>check.checked?map.addLayer(group):map.removeLayer(group);layerControls.appendChild(row);if(defaultOn)map.addLayer(group);if(LYR.id.startsWith('network-')){const bounds=group.getBounds?.();if(bounds&&bounds.isValid())allBounds.push(bounds)}}
if(allBounds.length){const b=allBounds.reduce((acc,x)=>acc.extend(x),L.latLngBounds(allBounds[0]));map.fitBounds(b.pad(0.03),{maxZoom:12})}
</script></body></html>""".replace("__LAYERS__", layers_json)

components.html(html, height=850, scrolling=False)
