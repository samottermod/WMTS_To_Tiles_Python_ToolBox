# WMTS_To_Tiles_Python_ToolBox

## Introduction

The WMTS to Tiles toolbox tools download tiles from a WMTS service and packages them into an MBTiles or GeoPackage file suitable for offline use in applications. Parameters are the same between the MBTiles and GeoPackage tools only the output file type is different.

**MBTiles:**
- ATAK/WinTAK
- QGIS
- SITAWARE
- Skydio X10D Handsets

**GeoPackage:**
- ArcGIS Pro
- ATAK/WinTAK
- QGIS
- SITAWARE

It supports:
- WMTS Capabilities parsing
- GoogleMapsCompatible tile matrix sets
- Retry logic for unstable servers
- High‑zoom tile extraction
- EPSG: 3857 (Web Mercator) support only
- Esri World Imagery, GeoServer WMTS, MapProxy WMTS, RBT 1 & 2 WMTS, ArcGIS Enterprise and Lambton WMTS Tested

## Parameters

### WMTS Capabilities URL
URL pointing to the WMTS capabilities document (WMTSCapabilities.xml).

For Example:
`https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/WMTS/1.0.0/WMTSCapabilities.xml`

### WMTS Layer (Identifier Only)
Layer identifier parsed from the capabilities file.
This should pull through from the dropdown.
The tool only works with EPSG 3857.

Example: `World_Imagery`

### Extent Feature Class
Polygon feature class or shapefile defining the geographic area to download.
This must be in EPSG 3857.
The polygon feature class must be selected by clicking the folder icon in the tool.

### Clip Output To Exact Extent
Boolean Tick Box to clip output MBTiles or Geopackage to the exact geometry of the Extent Feature Class.
If unticked this caches all tiles that intersect with the Extent Feature Class.

For example, without clipping, at level 0 the whole world will be cached as it is 1 tile.
Whereas with clipping on, the level 0 tile will be clipped to the Extent Feature Class.

### Minimum Zoom
Lowest zoom level to download (0–19).
Lower zoom = fewer tiles.

### Maximum Zoom
Highest zoom level to download (0–19).
Higher zoom = more tiles, larger file.
With each zoom level the size of file will be exponentially larger.

### Output MBTiles
Path and name of the file to create.
The correct extension is added automatically.

## Environments

Do **NOT** use ArcGIS geoprocessing environments with this tool.
The tool manages its own workspace, projection, and tile maths.

## Zoom Levels

| Zoom Level | Approx Scale | TAK Resolution |
|-----------:|-------------:|---------------:|
| Level 0 | ~1:500,000,000 | 156.5km |
| Level 1 | ~1:250,000,000 | 78.3km |
| Level 2 | ~1:150,000,000 | 39.1km |
| Level 3 | ~1:70,000,000 | 19.6km |
| Level 4 | ~1:35,000,000 | 9.8km |
| Level 5 | ~1:15,000,000 | 4.9km |
| Level 6 | ~1:10,000,000 | 2.4km |
| Level 7 | ~1:4,000,000 | 1.2km |
| Level 8 | ~1:2,000,000 | 611m |
| Level 9 | ~1:1,000,000 | 306m |
| Level 10 | ~1:500,000 | 153m |
| Level 11 | ~1:250,000 | 76m |
| Level 12 | ~1:150,000 | 38m |
| Level 13 | ~1:70,000 | 19m |
| Level 14 | ~1:35,000 | 10m |
| Level 15 | ~1:15,000 | 5m |
| Level 16 | ~1:8,000 | 2m |
| Level 17 | ~1:4,500 | 1m |
| Level 18 | ~1:2,000 | 0.6m |
| Level 19 | ~1:1,000 | 0.3m |

## What to do with the MBTiles output

**ATAK:**
- Drop the raw MBTiles file in `internal storage\atak\imagery`
- Do not add any additional folders inside the imagery folder.
- Restart ATAK.

**WinTAK:**
- Drop the raw MBTiles file in `C:\ProgramData\WinTAK\Imagery`
- Do not add any additional folders inside the imagery folder.
- Restart WinTAK.

## Common Failures & Errors

| Error | Cause | Fix |
|-------|-------|-----|
| 502 errors | WMTS server throttling | Retry logic handles this automatically |
| DNS resolution failures | Network hiccups or Esri rate‑limiting | Tool retries automatically |
| NaN tile math errors | Extent shapefile has invalid or undefined projection | Define Projection or Project to EPSG:3857 |
| MBTiles cannot be created | Output folder does not exist or is not writable | Save to a local folder (e.g., `C:\temp`) |
