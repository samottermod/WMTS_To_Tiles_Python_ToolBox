# -*- coding: utf-8 -*-
import arcpy
import os
import requests
import xml.etree.ElementTree as ET
import sqlite3
import tempfile
import uuid
import time
import datetime
import urllib3

# Allow self-signed / invalid SSL certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WMTS_NS = {
    "wmts": "http://www.opengis.net/wmts/1.0",
    "ows": "http://www.opengis.net/ows/1.1"
}

# Standard Web Mercator ("GoogleMapsCompatible") world extent in EPSG:3857.
# Used by BOTH tools when converting the user's extent feature class into
# tile column/row ranges.
ORIGIN_SHIFT = 20037508.342789244
WORLD_MINX = -ORIGIN_SHIFT
WORLD_MAXX = ORIGIN_SHIFT
WORLD_MINY = -ORIGIN_SHIFT
WORLD_MAXY = ORIGIN_SHIFT


def lonlat_to_tile(x, y, zoom):
    """Convert a Web Mercator (EPSG:3857) coordinate to a tile column/row
    at the given zoom, using the standard GoogleMapsCompatible grid.
    The returned row follows the TOP-LEFT-origin convention (row 0 at the
    north edge), which matches WMTS TileRow and GeoPackage tile_row.
    MBTiles (TMS) needs this flipped bottom-to-top -- see the MBTiles tool."""
    mx = (x + ORIGIN_SHIFT) / (2 * ORIGIN_SHIFT)
    my = (ORIGIN_SHIFT - y) / (2 * ORIGIN_SHIFT)
    tiles = 2 ** zoom
    return int(mx * tiles), int(my * tiles)


def format_to_ext(fmt):
    """Map a WMTS ResourceURL 'format' attribute (e.g. 'image/jpeg') to a
    file extension. Defaults to png if the format is missing/unrecognised."""
    fmt = (fmt or "").lower()
    if "jpeg" in fmt or "jpg" in fmt:
        return "jpg"
    if "png" in fmt:
        return "png"
    return "png"


def project_extent(extent_fc, srid):
    """Reproject an extent feature class's bounding box to the given SRID
    and return (xmin, ymin, xmax, ymax)."""
    desc = arcpy.Describe(extent_fc)
    extent_geom = arcpy.Polygon(
        arcpy.Array([
            arcpy.Point(desc.extent.XMin, desc.extent.YMin),
            arcpy.Point(desc.extent.XMin, desc.extent.YMax),
            arcpy.Point(desc.extent.XMax, desc.extent.YMax),
            arcpy.Point(desc.extent.XMax, desc.extent.YMin)
        ]),
        desc.spatialReference
    )
    extent_proj = extent_geom.projectAs(arcpy.SpatialReference(srid))
    return (extent_proj.extent.XMin, extent_proj.extent.YMin,
            extent_proj.extent.XMax, extent_proj.extent.YMax)


# ============================================================================
# SHARED TOOL PARAMETERS
# Both tools take the same inputs in the same order; only the output file
# parameter and the crop help text differ.
# ============================================================================
def build_common_parameters(output_label):
    """Return the parameters every tool shares:
    (wmts_url, wmts_layer, extent_fc, min_zoom, max_zoom).
    output_label (e.g. "MBTiles") is used in the Maximum Zoom help text."""
    p_wmts_url = arcpy.Parameter(
        displayName="WMTS Capabilities URL",
        name="wmts_url",
        datatype="String",
        parameterType="Required",
        direction="Input"
    )
    p_wmts_url.description = ("WMTS Capabilities URL:\nURL pointing to the WMTS capabilities "
                               "document (WMTSCapabilities.xml).\nFor Example:\n"
                               "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/"
                               "MapServer/WMTS/1.0.0/WMTSCapabilities.xml")

    p_layer = arcpy.Parameter(
        displayName="WMTS Layer (Identifier Only)",
        name="wmts_layer",
        datatype="String",
        parameterType="Required",
        direction="Input"
    )
    p_layer.description = ("WMTS Layer (Identifier Only):\nLayer identifier parsed from the "
                            "capabilities file.\nThis should pull through from the dropdown.\n"
                            "The tool only works with EPSG 3857.\nExample: World_Imagery")
    p_layer.filter.type = "ValueList"

    p_extent = arcpy.Parameter(
        displayName="Extent Feature Class",
        name="extent_fc",
        datatype="Feature Class",
        parameterType="Required",
        direction="Input"
    )
    p_extent.description = ("Extent Feature Class:\nPolygon feature class or shapefile defining "
                             "the geographic area to download.\nThis must be in EPSG 3857.\n"
                             "The polygon feature class must be selected by clicking the folder "
                             "icon in the tool.")

    p_min_zoom = arcpy.Parameter(
        displayName="Minimum Zoom",
        name="min_zoom",
        datatype="Long",
        parameterType="Required",
        direction="Input"
    )
    p_min_zoom.description = "Minimum Zoom:\nLowest zoom level to download (0-19).\nLower zoom = fewer tiles."
    p_min_zoom.value = 0

    p_max_zoom = arcpy.Parameter(
        displayName="Maximum Zoom",
        name="max_zoom",
        datatype="Long",
        parameterType="Required",
        direction="Input"
    )
    p_max_zoom.description = ("Maximum Zoom:\nHighest zoom level to download (0-19).\n"
                               f"Higher zoom = more tiles, larger {output_label}.")
    p_max_zoom.value = 14

    return p_wmts_url, p_layer, p_extent, p_min_zoom, p_max_zoom


def build_crop_parameter(description):
    """Return the optional 'Crop Metadata Extent to Input Area' checkbox,
    checked by default. Each output format words its help text itself."""
    p_crop = arcpy.Parameter(
        displayName="Crop Metadata Extent to Input Area",
        name="crop_to_extent",
        datatype="Boolean",
        parameterType="Optional",
        direction="Input"
    )
    p_crop.description = description
    p_crop.value = True
    return p_crop


def read_common_inputs(parameters, output_ext):
    """Read the shared parameter values in the order build_common_parameters
    + build_crop_parameter + output produce them:
    [url, layer, extent, crop, min_zoom, max_zoom, output].
    Appends '.<output_ext>' to the output path if it is missing.
    Returns (wmts_url, layer_id, extent_fc, crop_to_extent, min_zoom,
    max_zoom, output_path)."""
    wmts_url = str(parameters[0].value)
    layer_id = str(parameters[1].value)
    extent_fc = str(parameters[2].value)
    min_zoom = int(parameters[4].value)
    max_zoom = int(parameters[5].value)
    output_path = str(parameters[6].value)
    if not output_path.lower().endswith("." + output_ext):
        output_path = output_path + "." + output_ext
    crop_to_extent = bool(parameters[3].value) if parameters[3].value is not None else False
    return wmts_url, layer_id, extent_fc, crop_to_extent, min_zoom, max_zoom, output_path


# ============================================================================
# SHARED WMTS CAPABILITIES PARSING
# ============================================================================
def fetch_capabilities(wmts_url):
    """Download and parse a WMTS capabilities document; returns the XML root."""
    xml_data = requests.get(wmts_url, verify=False).content
    return ET.fromstring(xml_data)


def parse_wmts_layers(root, layer_map, prefer_google_maps_compatible):
    """Fill layer_map from a capabilities document, as
    {identifier: (tms_name, resource_url, metadata, fmt, style_name)},
    and return the list of layer identifiers in document order.

    prefer_google_maps_compatible=False takes the layer's first
    TileMatrixSetLink ("unknown" if it has none) - this is what the
    dropdown in updateParameters uses. True picks "GoogleMapsCompatible" if
    the layer links to it, else its first link (None if it has none) - this
    is what the fallback parser in execute uses."""
    ns = WMTS_NS
    identifier_list = []
    for lyr in root.findall("wmts:Contents/wmts:Layer", ns):
        identifier = lyr.find("ows:Identifier", ns).text
        if prefer_google_maps_compatible:
            tms_links = lyr.findall("wmts:TileMatrixSetLink/wmts:TileMatrixSet", ns)
            tms_name = None
            for t in tms_links:
                if t.text == "GoogleMapsCompatible":
                    tms_name = "GoogleMapsCompatible"
                    break
            if tms_name is None and tms_links:
                tms_name = tms_links[0].text
        else:
            tms_elem = lyr.find("wmts:TileMatrixSetLink/wmts:TileMatrixSet", ns)
            tms_name = tms_elem.text if tms_elem is not None else "unknown"

        res_elem = lyr.find("wmts:ResourceURL", ns)
        resource_url = res_elem.attrib["template"] if res_elem is not None else ""
        fmt = res_elem.attrib.get("format", "unknown") if res_elem is not None else "unknown"

        # Capture the layer's default Style identifier. Many WMTS
        # servers (ArcGIS Enterprise/Server especially) put a
        # {Style} token in the ResourceURL template that must be
        # substituted (usually with "default") or the server
        # returns 400 Bad Request.
        style_elem = lyr.find("wmts:Style/ows:Identifier", ns)
        style_name = style_elem.text if style_elem is not None else "default"

        crs = "unknown"
        for tms in root.findall("wmts:Contents/wmts:TileMatrixSet", ns):
            tms_id = tms.find("{http://www.opengis.net/ows/1.1}Identifier").text
            if tms_id == tms_name:
                crs_elem = tms.find("ows:SupportedCRS", ns)
                if crs_elem is not None:
                    crs = crs_elem.text
                break

        metadata = f"{identifier} - {crs} - TileMatrixSet: {tms_name} - Format: {fmt}"
        layer_map[identifier] = (tms_name, resource_url, metadata, fmt, style_name)
        identifier_list.append(identifier)
    return identifier_list


def update_layer_dropdown(tool, parameters):
    """Shared updateParameters body: when a capabilities URL is entered,
    fill the layer dropdown and cache the parsed layers on tool.layer_map."""
    wmts_url_param = parameters[0]
    layer_param = parameters[1]
    if not wmts_url_param.value:
        return
    wmts_url = str(wmts_url_param.value)
    try:
        root = fetch_capabilities(wmts_url)
        tool.layer_map = {}
        identifier_list = parse_wmts_layers(root, tool.layer_map, False)
        layer_param.filter.list = identifier_list
    except Exception as e:
        arcpy.AddWarning(f"Failed to read WMTS capabilities: {e}")


def log_layer_debug(tool, parameters, include_repr):
    """Debug output about the selected layer before it is resolved."""
    arcpy.AddMessage("DEBUG: Raw dropdown value:")
    arcpy.AddMessage(f"DEBUG: '{parameters[1].value}'")
    if include_repr:
        arcpy.AddMessage(f"DEBUG repr: {repr(parameters[1].value)}")
    arcpy.AddMessage(f"DEBUG available keys BEFORE fallback: {list(tool.layer_map.keys())}")


def resolve_wmts_layer(tool, wmts_url, layer_id):
    """Return the layer_map entry for layer_id, re-parsing the capabilities
    first if tool.layer_map is empty (e.g. the dropdown's cache was lost)."""
    if not tool.layer_map:
        arcpy.AddMessage("WARNING: layer_map empty - running fallback WMTS parser.")
        try:
            root = fetch_capabilities(wmts_url)
            parse_wmts_layers(root, tool.layer_map, True)
            arcpy.AddMessage(f"DEBUG available keys AFTER fallback: {list(tool.layer_map.keys())}")
        except Exception as e:
            raise arcpy.ExecuteError(f"Fallback WMTS parsing failed: {e}")

    if layer_id not in tool.layer_map:
        raise arcpy.ExecuteError(f"Layer '{layer_id}' not found even after fallback parsing.")
    return tool.layer_map[layer_id]


def read_tile_matrices(wmts_url, tile_matrix_set_name):
    """Fetch the capabilities and return the tile matrices of the named
    TileMatrixSet as {zoom: (matrix_width, matrix_height, tile_width,
    tile_height, tile_matrix_id)}. Tile width/height default to 256.

    tile_matrix_id is the RAW TileMatrix identifier text. Some WMTS servers
    use zero-padded identifiers ("00", "01", ...) - requests must use that
    exact text, not str(z), or the server will reject/mis-serve the tile
    (silently leaving that zoom empty)."""
    root = fetch_capabilities(wmts_url)
    ns = WMTS_NS

    tile_matrix_set = None
    for tms in root.findall("wmts:Contents/wmts:TileMatrixSet", ns):
        identifier = tms.find("{http://www.opengis.net/ows/1.1}Identifier").text
        if identifier == tile_matrix_set_name:
            tile_matrix_set = tms
            break
    if tile_matrix_set is None:
        raise arcpy.ExecuteError(f"TileMatrixSet {tile_matrix_set_name} not found.")

    tile_matrices = {}
    for tm in tile_matrix_set.findall("wmts:TileMatrix", ns):
        tm_id_text = tm.find("{http://www.opengis.net/ows/1.1}Identifier").text
        z = int(tm_id_text)
        mw = int(tm.find("wmts:MatrixWidth", ns).text)
        mh = int(tm.find("wmts:MatrixHeight", ns).text)
        tw_elem = tm.find("wmts:TileWidth", ns)
        th_elem = tm.find("wmts:TileHeight", ns)
        tw = int(tw_elem.text) if tw_elem is not None else 256
        th = int(th_elem.text) if th_elem is not None else 256
        tile_matrices[z] = (mw, mh, tw, th, tm_id_text)
    return tile_matrices


def wmts_tile_url(resource_url, style_name, tile_matrix_set_name, tm_id, tx, ty):
    """Fill a WMTS RESTful ResourceURL template for one tile."""
    url = resource_url.replace("{Style}", style_name)
    url = url.replace("{TileMatrixSet}", tile_matrix_set_name)
    url = url.replace("{TileMatrix}", tm_id)
    url = url.replace("{TileCol}", str(tx))
    url = url.replace("{TileRow}", str(ty))
    return url


# ============================================================================
# SHARED EXTENT / TILE MATH
# ============================================================================
def tile_range(extent, z, mw, mh):
    """Return (tx_min, tx_max, ty_min, ty_max) of the top-left-origin tiles
    covering extent (xmin, ymin, xmax, ymax in EPSG:3857) at zoom z,
    clamped to a matrix_width x matrix_height grid."""
    xmin, ymin, xmax, ymax = extent
    tx_min, ty_min = lonlat_to_tile(xmin, ymax, z)
    tx_max, ty_max = lonlat_to_tile(xmax, ymin, z)
    tx_min = max(0, min(tx_min, mw - 1))
    tx_max = max(0, min(tx_max, mw - 1))
    ty_min = max(0, min(ty_min, mh - 1))
    ty_max = max(0, min(ty_max, mh - 1))
    return tx_min, tx_max, ty_min, ty_max


def tile_count(tx_min, tx_max, ty_min, ty_max):
    return (tx_max - tx_min + 1) * (ty_max - ty_min + 1)


def log_tile_ranges(extent, min_zoom, max_zoom, tile_matrices):
    """Report how many tiles each requested zoom needs, warning about any
    zoom the TileMatrixSet does not define."""
    arcpy.AddMessage("Calculating tile ranges for all zoom levels...")
    arcpy.SetProgressor("default", "Preparing tile ranges...")
    arcpy.ResetProgressor()
    for z in range(min_zoom, max_zoom + 1):
        if z not in tile_matrices:
            arcpy.AddWarning(f"Zoom {z} not defined in TileMatrixSet; skipping.")
            continue
        mw, mh, tw, th, tm_id = tile_matrices[z]
        total_tiles = tile_count(*tile_range(extent, z, mw, mh))
        arcpy.AddMessage(f"Zoom {z}: {total_tiles} tiles")


# ============================================================================
# SHARED TEMP FOLDER + TILE DOWNLOAD
# Tiles are cached as <folder>/<z>/<x>/<y>.<ext> and later packed into the
# output file by the shared tile-packing helpers below.
# ============================================================================
def make_temp_tile_folder(prefix):
    """Create and report a unique temporary folder for downloaded tiles."""
    xyz_folder = os.path.join(tempfile.gettempdir(), f"{prefix}_{uuid.uuid4().hex}")
    os.makedirs(xyz_folder, exist_ok=True)
    arcpy.AddMessage(f"Temporary XYZ folder: {xyz_folder}")
    return xyz_folder


def new_download_session():
    """Persistent HTTP session reused for every tile request."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    return session


def download_tile(session, url, out_file, z, tx, ty):
    """GET one tile with up to 3 attempts and save it to out_file on HTTP 200.
    Non-200 responses and errors are reported as warnings, never raised."""
    for attempt in range(3):
        try:
            r = session.get(url, timeout=10, verify=False)
            if r.status_code == 200:
                with open(out_file, "wb") as f:
                    f.write(r.content)
                return True
            else:
                arcpy.AddWarning(f"Tile Z{z} X{tx} Y{ty} returned {r.status_code}")
        except Exception as e:
            if attempt == 2:
                arcpy.AddWarning(f"Tile Z{z} X{tx} Y{ty} failed after retries: {e}")
            else:
                time.sleep(0.5)
    return False


def download_tiles(extent, min_zoom, max_zoom, tile_matrices, xyz_folder, tile_ext,
                   tile_url, stored_row):
    """Download every tile covering extent for each zoom in the TileMatrixSet.

    tile_url(z, tm_id, tx, ty) returns the request URL for one tile (tx, ty
    are top-left-origin column/row). stored_row(ty, mh) returns the row
    number used for the cached file name, so each output format can pick
    its own row convention (e.g. TMS flip for MBTiles)."""
    session = new_download_session()

    arcpy.AddMessage("Downloading tiles...")
    for z in range(min_zoom, max_zoom + 1):
        if z not in tile_matrices:
            continue
        mw, mh, tw, th, tm_id = tile_matrices[z]
        tx_min, tx_max, ty_min, ty_max = tile_range(extent, z, mw, mh)
        total_tiles = tile_count(tx_min, tx_max, ty_min, ty_max)
        arcpy.AddMessage(f"Zoom {z}: {total_tiles} tiles to download")
        arcpy.SetProgressor("step", f"Downloading tiles for zoom {z}", 0, total_tiles, 1)
        downloaded = 0

        for tx in range(tx_min, tx_max + 1):
            for ty in range(ty_min, ty_max + 1):
                url = tile_url(z, tm_id, tx, ty)

                out_dir = os.path.join(xyz_folder, str(z), str(tx))
                os.makedirs(out_dir, exist_ok=True)
                out_file = os.path.join(out_dir, f"{stored_row(ty, mh)}.{tile_ext}")

                download_tile(session, url, out_file, z, tx, ty)

                downloaded += 1
                arcpy.SetProgressorPosition(downloaded)
                if downloaded % 50 == 0 or downloaded == total_tiles:
                    pct = int((downloaded / total_tiles) * 100)
                    arcpy.AddMessage(f"Zoom {z}: {downloaded}/{total_tiles} tiles ({pct}%)")

    arcpy.ResetProgressor()


# ============================================================================
# SHARED TILE PACKING (cached tile folder -> SQLite 'tiles' table)
# MBTiles and GeoPackage both store tiles in a table named 'tiles' with
# zoom_level / tile_column / tile_row / tile_data columns.
# ============================================================================
def iter_cached_tiles(xyz_folder, tile_ext):
    """Yield (z, x, y, tile_path) for every cached <z>/<x>/<y>.<ext> tile."""
    for z in os.listdir(xyz_folder):
        z_path = os.path.join(xyz_folder, z)
        if not os.path.isdir(z_path):
            continue
        for x in os.listdir(z_path):
            x_path = os.path.join(z_path, x)
            if not os.path.isdir(x_path):
                continue
            for yfile in os.listdir(x_path):
                if not yfile.endswith(f".{tile_ext}"):
                    continue
                y = int(yfile.replace(f".{tile_ext}", ""))
                yield int(z), int(x), y, os.path.join(x_path, yfile)


def begin_tile_packing(xyz_folder, tile_ext, output_label, output_path):
    """Count the cached tiles, start the write progressor and delete any
    existing output file. Returns the tile count."""
    arcpy.AddMessage(f"Writing {output_label}...")
    total_tiles = sum(1 for _ in iter_cached_tiles(xyz_folder, tile_ext))
    arcpy.AddMessage(f"Total tiles to write into {output_label}: {total_tiles}")
    arcpy.SetProgressor("step", f"Writing {output_label}", 0, total_tiles, 1)

    if os.path.exists(output_path):
        os.remove(output_path)
    return total_tiles


def insert_cached_tiles(cur, xyz_folder, tile_ext, total_tiles, output_label):
    """Insert every cached tile into the 'tiles' table, reporting progress."""
    processed = 0
    for z, x, y, tile_path in iter_cached_tiles(xyz_folder, tile_ext):
        with open(tile_path, "rb") as f:
            tile_data = f.read()
        cur.execute(
            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
            (z, x, y, tile_data)
        )
        processed += 1
        arcpy.SetProgressorPosition(processed)
        if processed % 100 == 0 or processed == total_tiles:
            pct = int((processed / total_tiles) * 100) if total_tiles else 100
            arcpy.AddMessage(f"{output_label} writing: {processed}/{total_tiles} tiles ({pct}%)")


def finish_tile_packing(conn, output_label, xyz_folder):
    """Commit and close the output database and report where tiles were left."""
    conn.commit()
    conn.close()
    arcpy.ResetProgressor()
    arcpy.AddMessage(f"WMTS to {output_label} completed.")
    arcpy.AddMessage(f"Downloaded tiles were left in the temporary folder: {xyz_folder}")


class Toolbox(object):
    def __init__(self):
        self.label = "WMTS to Tiles"
        self.alias = "wmts_to_tiles"
        self.tools = [WMTS_to_MBTiles, WMTS_to_GeoPackage]


# ============================================================================
# TOOL 1 - WMTS to MBTiles
# ============================================================================
class WMTS_to_MBTiles(object):
    def __init__(self):
        self.label = "WMTS to MBTiles"
        self.description = ("The WMTS to MBTiles tool downloads tiles from a WMTS service and "
                             "packages them into an MBTiles file suitable for offline use in "
                             "applications such as ATAK/WinTAK, QGIS, SITAWARE and Skydio X10D "
                             "Handsets. Not ArcGIS Pro.\nIt supports:\n"
                             " - WMTS Capabilities parsing.\n"
                             " - GoogleMapsCompatible tile matrix sets.\n"
                             " - Retry logic for unstable servers.\n"
                             " - High-zoom tile extraction\n"
                             " - Esri World Imagery, GeoServer WMTS, MapProxy WMTS, RBT WMTS Tested.")
        self.category = "WMTS Tools"
        self.layer_map = {}

    def getParameterInfo(self):
        p_wmts_url, p_layer, p_extent, p_min_zoom, p_max_zoom = build_common_parameters("MBTiles")

        p_mbtiles = arcpy.Parameter(
            displayName="Output MBTiles",
            name="output_mbtiles",
            datatype="File",
            parameterType="Required",
            direction="Output"
        )
        p_mbtiles.description = "Output MBTiles:\nPath and name of the MBTiles file to create."
        p_mbtiles.filter.list = ["mbtiles"]

        p_crop = build_crop_parameter(
            "Crop Metadata Extent to Input Area:\nIf checked, the MBTiles "
            "'bounds' metadata field is set to the tight bounding box of your "
            "Extent Feature Class (in WGS84 lon/lat, as the MBTiles spec "
            "requires). Checked by default. If unchecked, no 'bounds' field is written, "
            "matching the tool's previous behaviour.\nNote: this only affects "
            "descriptive metadata some viewers use for 'zoom to layer'/panning "
            "limits - it does NOT crop or renumber the actual tiles.")

        return [p_wmts_url, p_layer, p_extent, p_crop, p_min_zoom, p_max_zoom, p_mbtiles]

    def updateParameters(self, parameters):
        update_layer_dropdown(self, parameters)
        return

    def execute(self, parameters, messages):
        (wmts_url, layer_id, extent_fc, crop_to_extent,
         min_zoom, max_zoom, output_mbtiles) = read_common_inputs(parameters, "mbtiles")

        log_layer_debug(self, parameters, include_repr=True)
        tile_matrix_set_name, resource_url, metadata, resource_fmt, style_name = \
            resolve_wmts_layer(self, wmts_url, layer_id)
        tile_ext = format_to_ext(resource_fmt)
        arcpy.AddMessage("Selected WMTS Layer Metadata:")
        arcpy.AddMessage(metadata)
        arcpy.AddMessage(f"Tile image format detected: {resource_fmt} (.{tile_ext})")
        arcpy.AddMessage(f"Style identifier: {style_name}")

        xyz_folder = make_temp_tile_folder("WMTS_MBTiles")
        tile_matrices = read_tile_matrices(wmts_url, tile_matrix_set_name)
        extent = project_extent(extent_fc, 3857)

        log_tile_ranges(extent, min_zoom, max_zoom, tile_matrices)

        # MBTiles uses TMS row order (0 at bottom) -> flip from our
        # top-origin ty when naming the cached tile files.
        download_tiles(
            extent, min_zoom, max_zoom, tile_matrices, xyz_folder, tile_ext,
            tile_url=lambda z, tm_id, tx, ty: wmts_tile_url(
                resource_url, style_name, tile_matrix_set_name, tm_id, tx, ty),
            stored_row=lambda ty, mh: (mh - 1) - ty)

        # ---------------- MBTILES WRITING ----------------
        total_tiles = begin_tile_packing(xyz_folder, tile_ext, "MBTiles", output_mbtiles)

        conn = sqlite3.connect(output_mbtiles)
        cur = conn.cursor()
        cur.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        cur.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        cur.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
        cur.execute("INSERT INTO metadata (name, value) VALUES ('format',?)", (tile_ext,))
        cur.execute("INSERT INTO metadata (name, value) VALUES ('type','baselayer')")
        cur.execute("INSERT INTO metadata (name, value) VALUES ('name','WMTS Export')")
        cur.execute("INSERT INTO metadata (name, value) VALUES ('minzoom',?)", (min_zoom,))
        cur.execute("INSERT INTO metadata (name, value) VALUES ('maxzoom',?)", (max_zoom,))
        if crop_to_extent:
            # MBTiles 'bounds' is WGS84 lon/lat "west,south,east,north".
            # This is purely descriptive metadata used by some viewers for
            # zoom-to-layer / panning limits - it does not affect how tiles
            # are addressed or positioned.
            b_xmin, b_ymin, b_xmax, b_ymax = project_extent(extent_fc, 4326)
            bounds_str = f"{b_xmin},{b_ymin},{b_xmax},{b_ymax}"
            cur.execute("INSERT INTO metadata (name, value) VALUES ('bounds',?)", (bounds_str,))
            arcpy.AddMessage(f"Cropped metadata bounds (WGS84): {bounds_str}")

        insert_cached_tiles(cur, xyz_folder, tile_ext, total_tiles, "MBTiles")
        finish_tile_packing(conn, "MBTiles", xyz_folder)


# ============================================================================
# TOOL 2 - WMTS to GeoPackage
# ============================================================================
class WMTS_to_GeoPackage(object):
    def __init__(self):
        self.label = "WMTS to GeoPackage"
        self.description = ("The WMTS to GeoPackage tool downloads tiles from a WMTS service and "
                             "packages them into an OGC GeoPackage (.gpkg) tile file suitable for "
                             "offline use in applications such as ATAK/WinTAK and QGIS. Not ArcGIS Pro "
                             "(though it can be viewed there too).\nIt supports:\n"
                             " - WMTS Capabilities parsing.\n"
                             " - GoogleMapsCompatible tile matrix sets (EPSG:3857 only).\n"
                             " - Retry logic for unstable servers.\n"
                             " - High-zoom tile extraction\n"
                             " - Tile size and image format read from the WMTS capabilities.\n"
                             " - Esri World Imagery, GeoServer WMTS, MapProxy WMTS, RBT WMTS Tested.")
        self.category = "WMTS Tools"
        self.layer_map = {}

    def getParameterInfo(self):
        p_wmts_url, p_layer, p_extent, p_min_zoom, p_max_zoom = build_common_parameters("GeoPackage")

        p_gpkg = arcpy.Parameter(
            displayName="Output GeoPackage",
            name="output_gpkg",
            datatype="File",
            parameterType="Required",
            direction="Output"
        )
        p_gpkg.description = "Output GeoPackage:\nPath and name of the GeoPackage (.gpkg) file to create."
        p_gpkg.filter.list = ["gpkg"]

        p_crop = build_crop_parameter(
            "Crop Metadata Extent to Input Area:\nIf checked, the "
            "gpkg_contents bounding box (used by QGIS/ArcGIS for 'zoom to "
            "layer' and shown as the layer's extent) is set to the tight "
            "bounding box of your Extent Feature Class. Checked by default. "
            "If unchecked, it is set to the full world Web Mercator extent, "
            "matching the tool's previous behaviour.\nNote: this only affects "
            "descriptive metadata. The gpkg_tile_matrix_set table (which "
            "controls where tiles are actually positioned) always uses the "
            "full world grid, regardless of this setting - tiles keep their "
            "native WMTS column/row numbers either way, so georeferencing is "
            "never affected by this option.")

        return [p_wmts_url, p_layer, p_extent, p_crop, p_min_zoom, p_max_zoom, p_gpkg]

    def updateParameters(self, parameters):
        update_layer_dropdown(self, parameters)
        return

    def execute(self, parameters, messages):
        (wmts_url, layer_id, extent_fc, crop_to_extent,
         min_zoom, max_zoom, output_gpkg) = read_common_inputs(parameters, "gpkg")

        log_layer_debug(self, parameters, include_repr=False)
        tile_matrix_set_name, resource_url, metadata, resource_fmt, style_name = \
            resolve_wmts_layer(self, wmts_url, layer_id)
        tile_ext = format_to_ext(resource_fmt)
        arcpy.AddMessage("Selected WMTS Layer Metadata:")
        arcpy.AddMessage(metadata)
        arcpy.AddMessage(f"Style identifier: {style_name}")
        arcpy.AddMessage(f"Tile image format detected: {resource_fmt} (.{tile_ext})")

        xyz_folder = make_temp_tile_folder("WMTS_GPKG")
        # Per-zoom tile pixel width/height (also returned) are needed for
        # gpkg_tile_matrix below.
        tile_matrices = read_tile_matrices(wmts_url, tile_matrix_set_name)
        extent = project_extent(extent_fc, 3857)
        xmin, ymin, xmax, ymax = extent

        log_tile_ranges(extent, min_zoom, max_zoom, tile_matrices)

        # NOTE: no row flip here. GeoPackage (like WMTS TileRow) uses a
        # TOP-LEFT origin -- row 0 is the northernmost row at each zoom.
        # This is the opposite of MBTiles/TMS, which is why the MBTiles
        # tool flips its row and this one does not.
        download_tiles(
            extent, min_zoom, max_zoom, tile_matrices, xyz_folder, tile_ext,
            tile_url=lambda z, tm_id, tx, ty: wmts_tile_url(
                resource_url, style_name, tile_matrix_set_name, tm_id, tx, ty),
            stored_row=lambda ty, mh: ty)

        # ---------------- GEOPACKAGE WRITING ----------------
        total_tiles = begin_tile_packing(xyz_folder, tile_ext, "GeoPackage", output_gpkg)

        conn = sqlite3.connect(output_gpkg)
        cur = conn.cursor()

        # Identify the file as a GeoPackage 1.3.0 SQLite database.
        cur.execute("PRAGMA application_id = 1196444487;")   # 'GPKG' big-endian
        cur.execute("PRAGMA user_version = 10300;")           # GeoPackage 1.3.0

        # --- gpkg_spatial_ref_sys -------------------------------------------------
        cur.execute("""
            CREATE TABLE gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            );
        """)
        wkt_4326 = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
                    'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
                    'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
                    'AUTHORITY["EPSG","4326"]]')
        wkt_3857 = ('PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",'
                    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
                    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
                    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]],'
                    'PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],'
                    'PARAMETER["scale_factor",1],PARAMETER["false_easting",0],'
                    'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
                    'AXIS["X",EAST],AXIS["Y",NORTH],AUTHORITY["EPSG","3857"]]')
        cur.executemany(
            "INSERT INTO gpkg_spatial_ref_sys "
            "(srs_name, srs_id, organization, organization_coordsys_id, definition, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
                ("WGS 84 geodetic", 4326, "EPSG", 4326, wkt_4326, "longitude/latitude WGS84"),
                ("WGS 84 / Pseudo-Mercator", 3857, "EPSG", 3857, wkt_3857, "Web Mercator"),
            ]
        )

        # --- gpkg_contents ----------------------------------------------------------
        cur.execute("""
            CREATE TABLE gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT UNIQUE,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                min_x DOUBLE,
                min_y DOUBLE,
                max_x DOUBLE,
                max_y DOUBLE,
                srs_id INTEGER,
                CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
            );
        """)

        # --- gpkg_tile_matrix_set -----------------------------------------------
        cur.execute("""
            CREATE TABLE gpkg_tile_matrix_set (
                table_name TEXT NOT NULL PRIMARY KEY,
                srs_id INTEGER NOT NULL,
                min_x DOUBLE NOT NULL,
                min_y DOUBLE NOT NULL,
                max_x DOUBLE NOT NULL,
                max_y DOUBLE NOT NULL,
                CONSTRAINT fk_gtms_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
            );
        """)

        # --- gpkg_tile_matrix -----------------------------------------------------
        cur.execute("""
            CREATE TABLE gpkg_tile_matrix (
                table_name TEXT NOT NULL,
                zoom_level INTEGER NOT NULL,
                matrix_width INTEGER NOT NULL,
                matrix_height INTEGER NOT NULL,
                tile_width INTEGER NOT NULL,
                tile_height INTEGER NOT NULL,
                pixel_x_size DOUBLE NOT NULL,
                pixel_y_size DOUBLE NOT NULL,
                CONSTRAINT pk_ttm PRIMARY KEY (table_name, zoom_level),
                CONSTRAINT fk_tmm_table_name FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name)
            );
        """)

        # --- the actual tiles table -------------------------------------------------
        cur.execute("""
            CREATE TABLE tiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zoom_level INTEGER NOT NULL,
                tile_column INTEGER NOT NULL,
                tile_row INTEGER NOT NULL,
                tile_data BLOB NOT NULL,
                UNIQUE (zoom_level, tile_column, tile_row)
            );
        """)

        # gpkg_contents bounds: informational only (used by viewers for
        # "zoom to layer"). Tight-cropped if requested, else full world
        # extent. gpkg_tile_matrix_set below ALWAYS stays at the full world
        # extent regardless of this choice, because that's the origin every
        # tile's column/row is numbered against - tightening it would shift
        # every tile to the wrong location.
        if crop_to_extent:
            contents_minx, contents_miny, contents_maxx, contents_maxy = xmin, ymin, xmax, ymax
            arcpy.AddMessage(f"Cropped gpkg_contents bounds (EPSG:3857): {xmin},{ymin},{xmax},{ymax}")
        else:
            contents_minx, contents_miny, contents_maxx, contents_maxy = WORLD_MINX, WORLD_MINY, WORLD_MAXX, WORLD_MAXY

        now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cur.execute(
            "INSERT INTO gpkg_contents "
            "(table_name, data_type, identifier, description, last_change, min_x, min_y, max_x, max_y, srs_id) "
            "VALUES (?, 'tiles', ?, ?, ?, ?, ?, ?, ?, 3857)",
            ("tiles", layer_id, metadata, now_iso, contents_minx, contents_miny, contents_maxx, contents_maxy)
        )
        cur.execute(
            "INSERT INTO gpkg_tile_matrix_set (table_name, srs_id, min_x, min_y, max_x, max_y) "
            "VALUES ('tiles', 3857, ?, ?, ?, ?)",
            (WORLD_MINX, WORLD_MINY, WORLD_MAXX, WORLD_MAXY)
        )

        for z in range(min_zoom, max_zoom + 1):
            if z not in tile_matrices:
                continue
            mw, mh, tw, th, tm_id = tile_matrices[z]
            pixel_x_size = (WORLD_MAXX - WORLD_MINX) / (mw * tw)
            pixel_y_size = (WORLD_MAXY - WORLD_MINY) / (mh * th)
            cur.execute(
                "INSERT INTO gpkg_tile_matrix "
                "(table_name, zoom_level, matrix_width, matrix_height, tile_width, tile_height, "
                "pixel_x_size, pixel_y_size) VALUES ('tiles', ?, ?, ?, ?, ?, ?, ?)",
                (z, mw, mh, tw, th, pixel_x_size, pixel_y_size)
            )

        conn.commit()

        # --- write the tile blobs ----------------------------------------------------
        insert_cached_tiles(cur, xyz_folder, tile_ext, total_tiles, "GeoPackage")
        finish_tile_packing(conn, "GeoPackage", xyz_folder)
