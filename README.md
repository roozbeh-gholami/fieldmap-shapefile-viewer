# FieldMap

A small local Python app for opening and exploring shapefiles in an interactive map. It provides OpenStreetMap, satellite imagery, and terrain basemaps; feature attribute popups; layer visibility controls; opacity; and zoom-to-layer.

## Start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

The app opens in your default browser at `http://127.0.0.1:8765`. Keep the terminal window open while you use it. The terminal shows each request, received file names, load results, and full error details. GeoPandas and pandas load when you first add data; the first load can take a little while, and the terminal will show its progress. Choose a `.shp` file together with its matching `.dbf` and `.shx` files, a ZIP containing the shapefile components, or a GeoJSON file. If the shapefile has a `.prj`, include it so coordinates can be projected correctly. Basemap tiles and Leaflet are loaded from the internet.

To use a different local port, run `python main.py --port 8766`.
