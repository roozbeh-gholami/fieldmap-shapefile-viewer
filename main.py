"""A lightweight local shapefile viewer with a Leaflet map interface."""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
import threading
import time
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fieldmap")

gpd = None
gpd_import_error = None
gpd_import_lock = threading.Lock()


def load_geopandas():
    """Import the GIS stack only when the user first opens vector data."""
    global gpd, gpd_import_error
    if gpd is not None:
        return gpd
    with gpd_import_lock:
        if gpd is not None:
            return gpd
        if gpd_import_error is not None:
            raise RuntimeError(gpd_import_error)
        logger.info("Loading GeoPandas and pandas for the first time; this can take a little while")
        try:
            import geopandas
            gpd = geopandas
            logger.info("GeoPandas %s loaded successfully", gpd.__version__)
            return gpd
        except Exception as exc:
            gpd_import_error = (
                "Could not load GeoPandas and its dependencies. Reinstall them with "
                "'.venv\\Scripts\\python.exe -m pip install -r requirements.txt'. "
                f"Details: {exc}"
            )
            logger.exception("Could not initialize GeoPandas")
            raise RuntimeError(gpd_import_error) from exc


def json_safe(value):
    """Convert pandas/numpy attribute values into standard JSON values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


MAX_RASTER_PIXELS = 2048


def inspect_raster(path: Path) -> dict:
    import rasterio

    with rasterio.open(path) as src:
        crs = None
        if src.crs:
            epsg = src.crs.to_epsg()
            crs = f"EPSG:{epsg}" if epsg else src.crs.to_string()
        return {
            "name": path.stem,
            "crs": crs,
            "crs_name": src.crs.to_string()[:200] if src.crs else None,
            "width": src.width,
            "height": src.height,
            "bands": src.count,
        }


def render_raster(path: Path, crs_text: str | None) -> dict:
    """Reproject a GeoTIFF to EPSG:4326 and render it as a transparent PNG overlay."""
    import base64
    import io

    import numpy as np
    import rasterio
    from affine import Affine
    from PIL import Image
    from rasterio.crs import CRS
    from rasterio.transform import array_bounds
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(path) as src:
        crs = CRS.from_user_input(crs_text) if crs_text else src.crs
        if crs is None:
            raise ValueError("This GeoTIFF has no CRS. Enter one (for example EPSG:32633).")
        transform, width, height = calculate_default_transform(
            crs, "EPSG:4326", src.width, src.height, *src.bounds
        )
        factor = max(width, height) / MAX_RASTER_PIXELS
        if factor > 1:
            transform = transform * Affine.scale(factor)
            width, height = max(1, int(width / factor)), max(1, int(height / factor))
        band_ids = [1, 2, 3] if src.count >= 3 else [1]
        data = np.full((len(band_ids), height, width), np.nan, dtype=np.float32)
        for i, band in enumerate(band_ids):
            reproject(
                source=rasterio.band(src, band),
                destination=data[i],
                src_transform=src.transform,
                src_crs=crs,
                src_nodata=src.nodata,
                dst_transform=transform,
                dst_crs="EPSG:4326",
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
        is_byte = src.dtypes[0] == "uint8"
        valid = np.isfinite(data).all(axis=0)
        if not valid.any():
            raise ValueError("The raster does not contain any visible pixels with this CRS.")
        rgb = np.zeros((3, height, width), dtype=np.uint8)
        for i in range(len(band_ids)):
            channel = data[i]
            if is_byte:
                scaled = channel
            else:
                low, high = np.percentile(channel[valid], (2, 98))
                scaled = (channel - low) / (high - low) * 255 if high > low else np.zeros_like(channel)
            rgb[i] = np.nan_to_num(np.clip(scaled, 0, 255)).astype(np.uint8)
        if len(band_ids) == 1:
            rgb[1] = rgb[2] = rgb[0]
        alpha = (valid * 255).astype(np.uint8)
        image = Image.fromarray(np.dstack([rgb[0], rgb[1], rgb[2], alpha]), "RGBA")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        west, south, east, north = array_bounds(height, width, transform)
        return {
            "name": path.stem,
            "crs": crs_text or (f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_string()[:80]),
            "bounds": [[south, west], [north, east]],
            "image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
            "size": [src.width, src.height],
            "bands": src.count,
        }


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FieldMap — Shapefile Viewer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css">
<style>
:root{--bg:#111820;--panel:#19232d;--panel2:#202c37;--line:#2b3945;--text:#e7edf2;--muted:#91a0ac;--accent:#55c6a5;--blue:#6d9eff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif}button,input{font:inherit}.app{height:100vh;display:grid;grid-template-rows:58px 1fr}.top{display:flex;align-items:center;padding:0 18px;background:#151e26;border-bottom:1px solid var(--line);gap:16px}.brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:700;letter-spacing:.2px}.brand-icon{display:grid;place-items:center;width:32px;height:32px;border-radius:9px;color:#10231e;background:var(--accent)}.subtitle{color:var(--muted);font-size:12px;border-left:1px solid var(--line);padding-left:15px}.top-right{margin-left:auto;color:var(--muted);font-size:12px}.layout{min-height:0;display:grid;grid-template-columns:304px 1fr}.sidebar{background:var(--panel);border-right:1px solid var(--line);overflow:auto}.section{padding:17px 16px;border-bottom:1px solid var(--line)}.section-title{font-size:11px;text-transform:uppercase;letter-spacing:1.1px;color:#a9b6c0;font-weight:700;margin-bottom:13px}.open-btn{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;padding:11px;border:1px solid #347e6b;border-radius:7px;background:#22483f;color:#dffaf2;cursor:pointer;font-weight:650}.open-btn:hover{background:#2c5a4f}.hint{font-size:11px;color:var(--muted);margin-top:9px;line-height:1.5}.layer-empty{padding:12px;border:1px dashed #3a4854;border-radius:7px;color:var(--muted);font-size:12px;text-align:center}.layer{padding:11px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:7px;margin-bottom:8px}.layer-head{display:flex;align-items:center;gap:8px}.swatch{width:11px;height:11px;border-radius:3px;background:#57c6a5;flex:none}.layer-name{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}.icon-btn{background:none;border:0;color:var(--muted);padding:3px 5px;cursor:pointer}.icon-btn:hover{color:#fff}.layer-meta{font-size:10px;color:var(--muted);margin:5px 0 0 19px}.layer-controls{display:flex;align-items:center;gap:7px;margin:9px 0 0 19px;color:var(--muted);font-size:10px}.layer-controls input{width:100%;accent-color:var(--accent)}.base-option{display:flex;align-items:center;gap:9px;padding:7px 0;color:#d7e0e6;font-size:12px;cursor:pointer}.base-option input{accent-color:var(--accent)}.base-note{font-size:10px;color:var(--muted);margin-top:7px}.map-wrap{position:relative;min-width:0;min-height:0}.map{height:100%;background:#dce4e7}.map-tools{position:absolute;z-index:500;right:15px;top:14px;display:flex;gap:7px}.tool{border:1px solid #d5dde2;background:white;color:#24313b;border-radius:6px;padding:8px 10px;box-shadow:0 2px 8px #17232d22;cursor:pointer;font-size:12px}.tool:hover{background:#f2f7f8}.status{position:absolute;z-index:500;bottom:13px;left:13px;background:#17222ddd;color:#e4edf2;padding:6px 10px;border-radius:5px;font-size:11px;pointer-events:none}.leaflet-popup-content-wrapper{border-radius:8px}.popup{max-height:250px;overflow:auto;min-width:180px}.popup-title{font-weight:700;margin-bottom:7px}.popup-row{padding:3px 0;border-top:1px solid #edf0f2;font-size:11px}.popup-row b{color:#687782;margin-right:5px}.leaflet-control-attribution{font-size:9px!important}.modal-bg{position:fixed;inset:0;z-index:2000;background:#000a;display:none;place-items:center}.modal-bg.show{display:grid}.modal{width:min(420px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px}.modal h3{margin:0 0 4px;font-size:16px}.modal p{margin:4px 0 12px;color:var(--muted);font-size:12px}.modal label{display:block;font-size:11px;color:#a9b6c0;margin-bottom:5px}.modal input[type=text]{width:100%;padding:9px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);color:var(--text)}.modal-info{font-size:11px;color:var(--muted);margin:10px 0}.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}.modal-actions button{padding:8px 14px;border-radius:6px;border:1px solid var(--line);background:var(--panel2);color:var(--text);cursor:pointer}.modal-actions .primary{background:#22483f;border-color:#347e6b}@media(max-width:700px){.layout{grid-template-columns:245px 1fr}.subtitle{display:none}.top{padding:0 11px}}@media(max-width:520px){.layout{grid-template-columns:1fr;grid-template-rows:220px 1fr}.sidebar{border-right:0;border-bottom:1px solid var(--line)}.app{grid-template-rows:52px 1fr}}
</style></head><body><div class="app"><header class="top"><div class="brand"><span class="brand-icon"><i class="fa-solid fa-map-location-dot"></i></span>FieldMap</div><div class="subtitle">Shapefile viewer</div><div class="top-right" id="connection">Local workspace</div></header><div class="layout"><aside class="sidebar"><section class="section"><div class="section-title">Data</div><button class="open-btn" onclick="document.getElementById('files').click()"><i class="fa-solid fa-folder-open"></i> Add shapefile</button><input id="files" type="file" multiple accept=".shp,.shx,.dbf,.prj,.cpg,.geojson,.json,.zip" hidden><button class="open-btn" style="margin-top:9px" onclick="document.getElementById('tif').click()"><i class="fa-solid fa-image"></i> Add GeoTIFF</button><input id="tif" type="file" accept=".tif,.tiff" hidden><div class="hint">Choose a .shp file together with its matching .dbf and .shx files. ZIP archives and GeoJSON are also supported.</div></section><section class="section"><div class="section-title">Layers <span id="layer-count"></span></div><div id="layers"><div class="layer-empty">Add a data file to begin exploring</div></div></section><section class="section"><div class="section-title">Basemap</div><label class="base-option"><input type="radio" name="base" value="osm" checked> OpenStreetMap</label><label class="base-option"><input type="radio" name="base" value="imagery"> Satellite imagery</label><label class="base-option"><input type="radio" name="base" value="terrain"> Terrain</label><div class="base-note">Basemap tiles require an internet connection.</div></section><section class="section"><div class="section-title">About</div><div class="hint">A simple, local GIS viewer for exploring vector data. Your files stay on this computer.</div></section></aside><main class="map-wrap"><div class="map-tools"><button class="tool" onclick="zoomAll()"><i class="fa-solid fa-expand"></i> Zoom to layers</button></div><div id="map" class="map"></div><div class="status" id="status">Ready · Add a shapefile to get started</div></main></div></div>
<div class="modal-bg" id="crs-modal"><div class="modal"><h3>GeoTIFF coordinate system</h3><p id="crs-file"></p><label for="crs-input">CRS (EPSG code, e.g. EPSG:4326 or EPSG:32633, or WKT/PROJ string)</label><input type="text" id="crs-input" placeholder="EPSG:4326"><div class="modal-info" id="crs-info"></div><div class="modal-actions"><button id="crs-cancel">Cancel</button><button class="primary" id="crs-ok">Add layer</button></div></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const map=L.map('map',{zoomControl:true}).setView([20,0],2);const bases={osm:L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}),imagery:L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles &copy; Esri'}),terrain:L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{maxZoom:17,attribution:'Map data: &copy; OpenStreetMap contributors, SRTM | Map style: &copy; OpenTopoMap'})};let activeBase='osm';bases.osm.addTo(map);let layers=[];const colors=['#55c6a5','#6d9eff','#f3a65a','#ce83e3','#ed7183','#ddc64e'];
document.querySelectorAll('[name=base]').forEach(r=>r.addEventListener('change',e=>{map.removeLayer(bases[activeBase]);activeBase=e.target.value;bases[activeBase].addTo(map)}));
document.getElementById('files').addEventListener('change',async e=>{const fs=[...e.target.files];if(!fs.length)return;document.getElementById('status').textContent='Preparing map libraries and loading '+fs.length+' file(s)… First use may take a little while';const fd=new FormData();fs.forEach(f=>fd.append('files',f));try{const res=await fetch('/upload',{method:'POST',body:fd});const data=await res.json();if(!res.ok)throw Error(data.error||'Could not open these files');const color=colors[layers.length%colors.length];const geo=L.geoJSON(data.geojson,{style:feature=>({color,weight:1.5,fillColor:color,fillOpacity:.22}),pointToLayer:(f,ll)=>L.circleMarker(ll,{radius:6,color,fillColor:color,fillOpacity:.85,weight:1.5}),onEachFeature:(f,lyr)=>lyr.bindPopup(popup(f.properties||{}))}).addTo(map);const layer={id:Date.now()+Math.random(),name:data.name,count:data.count,crs:data.crs,color,geo,opacity:100};layers.push(layer);renderLayers();zoomLayer(layer);document.getElementById('status').textContent=`Loaded ${data.count.toLocaleString()} features · ${data.crs||'CRS unknown'}`}catch(err){const message=err instanceof TypeError&&err.message.toLowerCase().includes('fetch')?'Could not reach FieldMap. Check that its terminal window is still running, then reload this page and try again.':err.message;document.getElementById('status').textContent='Could not load files';alert(message)}finally{e.target.value=''}});
function askCrs(file,info){return new Promise(resolve=>{const m=document.getElementById('crs-modal'),inp=document.getElementById('crs-input');document.getElementById('crs-file').textContent=file.name;inp.value=info.crs||'';document.getElementById('crs-info').textContent=(info.crs?'Detected CRS: '+info.crs:'No CRS found in this file. Please enter one.')+` · ${info.width}×${info.height} px · ${info.bands} band(s)`;const done=v=>{m.classList.remove('show');ok.onclick=cancel.onclick=null;resolve(v)};const ok=document.getElementById('crs-ok'),cancel=document.getElementById('crs-cancel');ok.onclick=()=>{if(!inp.value.trim()){inp.focus();return}done(inp.value.trim())};cancel.onclick=()=>done(null);inp.onkeydown=e=>{if(e.key==='Enter')ok.click();if(e.key==='Escape')cancel.click()};m.classList.add('show');inp.focus()})}
async function postTif(file,query){const fd=new FormData();fd.append('files',file);const res=await fetch('/raster?'+query,{method:'POST',body:fd});const data=await res.json();if(!res.ok)throw Error(data.error||'Could not read this GeoTIFF');return data}
document.getElementById('tif').addEventListener('change',async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;const st=document.getElementById('status');try{st.textContent='Reading GeoTIFF metadata…';const info=await postTif(f,'mode=inspect');const crs=await askCrs(f,info);if(!crs){st.textContent='GeoTIFF import cancelled';return}st.textContent='Rendering GeoTIFF… large files may take a moment';const data=await postTif(f,'crs='+encodeURIComponent(crs));const img=L.imageOverlay(data.image,data.bounds,{opacity:1}).addTo(map);const layer={id:Date.now()+Math.random(),name:data.name,meta:`GeoTIFF · ${data.size[0]}×${data.size[1]} px · ${data.bands} band(s) · ${data.crs}`,color:'#f3a65a',geo:img,opacity:100,raster:true};layers.push(layer);renderLayers();zoomLayer(layer);st.textContent='Loaded GeoTIFF · '+data.crs}catch(err){st.textContent='Could not load GeoTIFF';alert(err.message)}});
function popup(props){const entries=Object.entries(props).slice(0,40);return '<div class="popup"><div class="popup-title">Feature attributes</div>'+entries.map(([k,v])=>`<div class="popup-row"><b>${esc(k)}</b>${esc(v===null?'—':String(v))}</div>`).join('')+'</div>'}function esc(x){return String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function renderLayers(){const box=document.getElementById('layers');document.getElementById('layer-count').textContent=layers.length?`(${layers.length})`:'';if(!layers.length){box.innerHTML='<div class="layer-empty">Add a data file to begin exploring</div>';return}box.innerHTML=layers.map(l=>`<div class="layer"><div class="layer-head"><span class="swatch" style="background:${l.color}"></span><span class="layer-name" title="${esc(l.name)}">${esc(l.name)}</span><button class="icon-btn" title="Zoom to layer" onclick="zoomLayerId('${l.id}')"><i class="fa-solid fa-crosshairs"></i></button><button class="icon-btn" title="Remove layer" onclick="removeLayer('${l.id}')"><i class="fa-solid fa-xmark"></i></button></div><div class="layer-meta">${esc(l.meta||`${l.count.toLocaleString()} features · ${l.crs||'CRS unknown'}`)}</div><div class="layer-controls"><i class="fa-regular fa-sun"></i><input aria-label="Layer opacity" type="range" min="10" max="100" value="${l.opacity}" oninput="setOpacity('${l.id}',this.value)"><span>${l.opacity}%</span></div></div>`).join('')}
function zoomLayer(l){const b=l.geo.getBounds();if(b.isValid())map.fitBounds(b,{padding:[30,30],maxZoom:15})}function zoomLayerId(id){const l=layers.find(x=>String(x.id)===String(id));if(l)zoomLayer(l)}function zoomAll(){const group=L.featureGroup(layers.map(l=>l.geo)),b=group.getBounds();if(b.isValid())map.fitBounds(b,{padding:[35,35],maxZoom:15})}function removeLayer(id){const i=layers.findIndex(x=>String(x.id)===String(id));if(i>=0){map.removeLayer(layers[i].geo);layers.splice(i,1);renderLayers()}}function setOpacity(id,value){const l=layers.find(x=>String(x.id)===String(id));if(!l)return;l.opacity=Number(value);if(l.raster)l.geo.setOpacity(l.opacity/100);else l.geo.setStyle({fillOpacity:.22*l.opacity/100,opacity:l.opacity/100});renderLayers();const sliders=document.querySelectorAll('.layer input');const ix=layers.indexOf(l);if(sliders[ix])sliders[ix].value=value}
</script></body></html>'''


class ViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        logger.info("%s - %s", self.client_address[0], _args[0] % _args[1:])

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain")

    def _handle_raster(self, query):
        started = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("The upload was empty.")
            raw = self.rfile.read(length)
            mime = self.headers.get("Content-Type", "application/octet-stream")
            message = BytesParser(policy=default).parsebytes((f"Content-Type: {mime}\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
            with tempfile.TemporaryDirectory(prefix="fieldmap-") as folder:
                path = None
                for part in message.iter_parts():
                    name = Path(part.get_filename() or "").name
                    if name:
                        path = Path(folder) / name
                        path.write_bytes(part.get_payload(decode=True) or b"")
                        break
                if path is None or path.suffix.lower() not in (".tif", ".tiff"):
                    raise ValueError("Select a .tif or .tiff file.")
                if query.get("mode", [""])[0] == "inspect":
                    result = inspect_raster(path)
                else:
                    crs_text = (query.get("crs", [""])[0] or "").strip() or None
                    result = render_raster(path, crs_text)
            self._send(200, json.dumps(result, allow_nan=False).encode(), "application/json; charset=utf-8")
            logger.info("Raster request finished (%.2fs)", time.monotonic() - started)
        except Exception as exc:
            logger.exception("Raster request failed")
            try:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/raster":
            return self._handle_raster(parse_qs(url.query))
        if url.path != "/upload":
            return self._send(404, b'{"error":"Not found"}', "application/json")
        started = time.monotonic()
        content_length = int(self.headers.get("Content-Length", "0"))
        logger.info("Upload started from %s (%s bytes)", self.client_address[0], content_length)
        try:
            geopandas = load_geopandas()
            if content_length <= 0:
                raise ValueError("The upload was empty. Select the shapefile components and try again.")
            raw = self.rfile.read(content_length)
            mime = self.headers.get("Content-Type", "application/octet-stream")
            message = BytesParser(policy=default).parsebytes((f"Content-Type: {mime}\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
            files = []
            with tempfile.TemporaryDirectory(prefix="fieldmap-") as folder:
                root = Path(folder)
                for part in message.iter_parts():
                    name = Path(part.get_filename() or "").name
                    if not name:
                        continue
                    destination = root / name
                    destination.write_bytes(part.get_payload(decode=True) or b"")
                    files.append(destination)
                logger.info("Received files: %s", ", ".join(p.name for p in files) or "none")
                archives = [p for p in files if p.suffix.lower() == ".zip"]
                if archives:
                    import zipfile
                    extracted = root / "extracted"
                    extracted.mkdir()
                    with zipfile.ZipFile(archives[0]) as zf:
                        for member in zf.infolist():
                            target = (extracted / member.filename).resolve()
                            if extracted.resolve() not in target.parents and target != extracted.resolve():
                                raise ValueError("Unsafe path in ZIP archive")
                        zf.extractall(extracted)
                    files = list(extracted.rglob("*.shp")) + list(extracted.rglob("*.geojson")) + list(extracted.rglob("*.json"))
                candidates = [p for p in files if p.suffix.lower() in (".shp", ".geojson", ".json")]
                if not candidates:
                    raise ValueError("No .shp or GeoJSON file found. Select the shapefile components together, or upload a ZIP.")
                path = candidates[0]
                frame = geopandas.read_file(path)
                if frame.empty:
                    raise ValueError("This file contains no features.")
                crs = str(frame.crs) if frame.crs else None
                if frame.crs:
                    frame = frame.to_crs("EPSG:4326")
                geojson = json.loads(frame.to_json(drop_id=True, default=json_safe))
                payload = json.dumps(
                    {"name": path.stem, "count": len(frame), "crs": crs, "geojson": geojson},
                    allow_nan=False,
                    default=json_safe,
                ).encode()
                self._send(200, payload, "application/json; charset=utf-8")
                logger.info("Loaded %s: %s features, CRS %s, response %s bytes (%.2fs)", path.name, len(frame), crs or "unknown", len(payload), time.monotonic() - started)
        except Exception as exc:
            logger.exception("Upload failed after %.2fs", time.monotonic() - started)
            try:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                logger.warning("Browser disconnected before the upload error could be returned")


def main():
    parser = argparse.ArgumentParser(description="Open and explore shapefiles in a local interactive map.")
    parser.add_argument("--port", type=int, default=8765, help="Local web server port (default: 8765)")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ViewerHandler)
    url = f"http://127.0.0.1:{args.port}"
    logger.info("FieldMap is running at %s", url)
    logger.info("GIS libraries will initialize when you first add a shapefile")
    logger.info("Keep this terminal open while using the map. Press Ctrl+C to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping FieldMap")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
