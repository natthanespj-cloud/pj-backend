from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image
import subprocess, tempfile, os, io, re, math
import xml.etree.ElementTree as ET
import ezdxf

app = FastAPI(title="PJ Backend", version="4.9.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

# ââ unit conversion ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# Potrace outputs SVG path coordinates in pixels at its default 72 dpi.
# 1 potrace pixel = 1 pt = 25.4/72 mm  â  apply this to every DXF coordinate
# so CypCut (INSUNITS=4, mm) gets the correct physical size.
PT_TO_MM = 25.4 / 72          # â 0.35278  (potrace default resolution: 72 dpi)
MIN_PATH_MM = 1.0              # discard polylines shorter than 1 mm (noise)

@app.get("/")
def root():
    return {"status": "PJ Backend v4.9.2 - photo line art (GrabCut+bilateral+kmeans) + XDoG cartoon", "version": "4.9.2"}

@app.get("/health")
def health():
    try:
        r = subprocess.run(["potrace", "--version"], capture_output=True, timeout=5)
        potrace_ok = r.returncode == 0
    except Exception:
        potrace_ok = False
    return {"status": "ok", "version": "4.9.2", "potrace": potrace_ok}

# ââ SVG / path helpers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def parse_svg_commands(d):
    tokens = re.findall(
        r'[MmCcLlZzHhVvSsQqTtAa]|[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?', d)
    commands = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if re.match(r'[MmCcLlZzHhVvSsQqTtAa]', tok):
            cmd = tok; i += 1; args = []
            while i < len(tokens) and not re.match(r'[MmCcLlZzHhVvSsQqTtAa]', tokens[i]):
                args.append(float(tokens[i])); i += 1
            commands.append((cmd, args))
        else:
            i += 1
    return commands

def parse_svg_transform(t):
    tx = ty = 0.0; sx = sy = 1.0
    m = re.search(r'translate\(\s*([^,\s)]+)\s*,\s*([^)\s]+)\s*\)', t)
    if m: tx, ty = float(m.group(1)), float(m.group(2))
    m = re.search(r'scale\(\s*([^,\s)]+)(?:\s*,\s*([^)\s]+))?\s*\)', t)
    if m:
        sx = float(m.group(1)); sy = float(m.group(2)) if m.group(2) else sx
    return tx, ty, sx, sy

def compute_bulge(p0, pm, p1):
    cx = p1[0]-p0[0]; cy = p1[1]-p0[1]
    chord = math.sqrt(cx*cx + cy*cy)
    if chord < 1e-10: return 0.0
    nx, ny = -cy/chord, cx/chord
    mx = (p0[0]+p1[0])/2; my = (p0[1]+p1[1])/2
    sagitta = (pm[0]-mx)*nx + (pm[1]-my)*ny
    bulge = 2.0*sagitta/chord
    return 0.0 if abs(bulge) > 1.0 else bulge

def bezier_at(p0, cp1, cp2, p3, t):
    mt = 1-t
    x = mt**3*p0[0]+3*mt**2*t*cp1[0]+3*mt*t**2*cp2[0]+t**3*p3[0]
    y = mt**3*p0[1]+3*mt**2*t*cp1[1]+3*mt*t**2*cp2[1]+t**3*p3[1]
    return (x, y)

def svg_path_to_polylines(d, sx_mm, sy_mm):
    """
    Potrace SVG: <g transform="translate(0,H) scale(s,-s)">
    sx_mm / sy_mm already include PT_TO_MM so output is in mm.
    Each Bezier subdivided into ~1-2 mm segments (fine for laser precision).
    """
    commands = parse_svg_commands(d)
    result = []; verts = []; cx = cy = 0.0; start_x = start_y = 0.0; closed = False

    def dx(x): return x * sx_mm
    def dy(y): return y * sy_mm

    def flush():
        if len(verts) >= 2: result.append((list(verts), closed))

    def add_bezier(p0x, p0y, cp1x, cp1y, cp2x, cp2y, p3x, p3y):
        nonlocal cx, cy
        chord_mm = math.sqrt((p3x-p0x)**2*sx_mm**2 + (p3y-p0y)**2*sy_mm**2)
        n = min(60, max(2, int(chord_mm / 1.5)))   # ~1.5 mm per segment
        prev = (p0x, p0y)
        for k in range(1, n+1):
            t2 = k/n; tm = (k-0.5)/n
            curr = bezier_at((p0x,p0y),(cp1x,cp1y),(cp2x,cp2y),(p3x,p3y),t2)
            mid  = bezier_at((p0x,p0y),(cp1x,cp1y),(cp2x,cp2y),(p3x,p3y),tm)
            b = compute_bulge((dx(prev[0]),dy(prev[1])),(dx(mid[0]),dy(mid[1])),(dx(curr[0]),dy(curr[1])))
            if verts: verts[-1][2] = b
            verts.append([dx(curr[0]),dy(curr[1]),0.0]); prev = curr
        cx, cy = p3x, p3y

    for cmd, args in commands:
        if cmd == 'M':
            flush(); verts.clear(); closed = False
            cx,cy = args[0],args[1]; start_x,start_y = cx,cy
            verts.append([dx(cx),dy(cy),0.0])
            for j in range(2,len(args),2):
                cx,cy=args[j],args[j+1]; verts.append([dx(cx),dy(cy),0.0])
        elif cmd == 'm':
            flush(); verts.clear(); closed = False
            cx+=args[0]; cy+=args[1]; start_x,start_y=cx,cy
            verts.append([dx(cx),dy(cy),0.0])
            for j in range(2,len(args),2):
                cx+=args[j]; cy+=args[j+1]; verts.append([dx(cx),dy(cy),0.0])
        elif cmd == 'C':
            for j in range(0,len(args),6):
                add_bezier(cx,cy,args[j],args[j+1],args[j+2],args[j+3],args[j+4],args[j+5])
        elif cmd == 'c':
            for j in range(0,len(args),6):
                add_bezier(cx,cy,cx+args[j],cy+args[j+1],cx+args[j+2],cy+args[j+3],cx+args[j+4],cy+args[j+5])
        elif cmd == 'L':
            for j in range(0,len(args),2):
                cx,cy=args[j],args[j+1]; verts.append([dx(cx),dy(cy),0.0])
        elif cmd == 'l':
            for j in range(0,len(args),2):
                cx+=args[j]; cy+=args[j+1]; verts.append([dx(cx),dy(cy),0.0])
        elif cmd in ('Z','z'):
            closed=True; cx,cy=start_x,start_y
            flush()
    return result

def _svg_path_to_polylines_yflip(d, svg_h, scale_mm):
    """Fallback: plain SVG (Y-down) â DXF (Y-up), coordinates in mm."""
    commands = parse_svg_commands(d)
    result = []; verts = []; cx = cy = 0.0; start_x = start_y = 0.0; closed = False

    def fy(y): return (svg_h - y) * scale_mm
    def fx(x): return x * scale_mm

    def flush():
        if len(verts) >= 2: result.append((list(verts), closed))

    def add_bezier(p0x, p0y, cp1x, cp1y, cp2x, cp2y, p3x, p3y):
        nonlocal cx, cy
        chord = math.sqrt((p3x-p0x)**2 + (p3y-p0y)**2) * scale_mm
        n = min(60, max(2, int(chord / 1.5)))
        prev = (p0x, p0y)
        for k in range(1, n+1):
            t2 = k/n; tm = (k-0.5)/n
            curr = bezier_at((p0x,p0y),(cp1x,cp1y),(cp2x,cp2y),(p3x,p3y),t2)
            mid  = bezier_at((p0x,p0y),(cp1x,cp1y),(cp2x,cp2y),(p3x,p3y),tm)
            b = compute_bulge((fx(prev[0]),fy(prev[1])),(fx(mid[0]),fy(mid[1])),(fx(curr[0]),fy(curr[1])))
            if verts: verts[-1][2] = b
            verts.append([fx(curr[0]),fy(curr[1]),0.0]); prev = curr
        cx, cy = p3x, p3y

    for cmd, args in commands:
        if cmd == 'M':
            flush(); verts.clear(); closed = False
            cx,cy=args[0],args[1]; start_x,start_y=cx,cy; verts.append([fx(cx),fy(cy),0.0])
            for j in range(2,len(args),2):
                cx,cy=args[j],args[j+1]; verts.append([fx(cx),fy(cy),0.0])
        elif cmd == 'm':
            flush(); verts.clear(); closed = False
            cx+=args[0]; cy+=args[1]; start_x,start_y=cx,cy; verts.append([fx(cx),fy(cy),0.0])
            for j in range(2,len(args),2):
                cx+=args[j]; cy+=args[j+1]; verts.append([fx(cx),fy(cy),0.0])
        elif cmd == 'C':
            for j in range(0,len(args),6):
                add_bezier(cx,cy,args[j],args[j+1],args[j+2],args[j+3],args[j+4],args[j+5])
        elif cmd == 'c':
            for j in range(0,len(args),6):
                add_bezier(cx,cy,cx+args[j],cy+args[j+1],cx+args[j+2],cy+args[j+3],cx+args[j+4],cy+args[j+5])
        elif cmd == 'L':
            for j in range(0,len(args),2):
                cx,cy=args[j],args[j+1]; verts.append([fx(cx),fy(cy),0.0])
        elif cmd == 'l':
            for j in range(0,len(args),2):
                cx+=args[j]; cy+=args[j+1]; verts.append([fx(cx),fy(cy),0.0])
        elif cmd in ('Z','z'):
            closed=True; cx,cy=start_x,start_y
            flush()
    return result

def _polyline_length_mm(verts):
    total = 0.0
    for i in range(len(verts)-1):
        dx = verts[i+1][0]-verts[i][0]; dy = verts[i+1][1]-verts[i][1]
        total += math.sqrt(dx*dx+dy*dy)
    return total

def build_dxf(svg_content, selected="all"):
    """Convert SVG string to DXF bytes (coordinates in mm, INSUNITS=4)."""
    svg_clean = re.sub(r'\sxmlns(?::\w+)?="[^"]*"', '', svg_content)
    svg_clean = re.sub(r'<\?xml[^?]*\?>', '', svg_clean).strip()
    try:
        root = ET.fromstring(svg_clean)
    except ET.ParseError:
        svg_clean = re.sub(r'<svg[^>]*>', '<svg>', svg_clean, count=1)
        root = ET.fromstring(svg_clean)

    vb = root.get('viewBox', '0 0 500 500').split()
    svg_h = float(vb[3]) if len(vb) >= 4 else 500.0

    # ââ detect potrace transform and build mm-scale factors ââââââââââââââââââ
    # Potrace emits: <g transform="translate(0,H) scale(s,-s)">
    # Path data coords are in potrace pixels; scale s converts pixelsâSVG pts.
    # We multiply by PT_TO_MM so DXF values come out in real millimetres.
    scale_x_raw = scale_y_raw = 1.0; has_transform = False
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ''
        if tag == 'g' or tag.endswith('}g'):
            t = el.get('transform', '')
            if t and 'scale' in t:
                _, _, sx, sy = parse_svg_transform(t)
                scale_x_raw = abs(sx); scale_y_raw = abs(sy); has_transform = True; break

    sx_mm = scale_x_raw * PT_TO_MM
    sy_mm = scale_y_raw * PT_TO_MM

    all_paths = [el for el in root.iter()
                 if isinstance(el.tag, str) and el.tag.lower().endswith('path')]
    if selected.strip() == "all":
        chosen = all_paths
    else:
        idxs = {int(x) for x in selected.split(',') if x.strip()}
        chosen = [p for i,p in enumerate(all_paths) if i in idxs]

    doc = ezdxf.new('R2010')
    doc.header['$INSUNITS'] = 4; doc.header['$MEASUREMENT'] = 1
    msp = doc.modelspace()

    for path_el in chosen:
        d = path_el.get('d', '').strip()
        if not d: continue
        if has_transform:
            polylines = svg_path_to_polylines(d, sx_mm, sy_mm)
        else:
            polylines = _svg_path_to_polylines_yflip(d, svg_h, PT_TO_MM)

        for verts, is_closed in polylines:
            if len(verts) < 2: continue
            # filter noise paths shorter than MIN_PATH_MM
            if _polyline_length_mm(verts) < MIN_PATH_MM: continue
            pts = [(v[0],v[1],0.0,0.0,v[2]) for v in verts]
            msp.add_lwpolyline(pts, dxfattribs={'layer': '0', 'closed': is_closed})

    buf = io.StringIO(); doc.write(buf)
    return buf.getvalue().encode('utf-8')

@app.post("/export-dxf")
async def export_dxf(svg: str = Form(...), selected: str = Form("all")):
    try:
        dxf_bytes = build_dxf(svg, selected)
        return Response(content=dxf_bytes, media_type="application/dxf",
                        headers={"Content-Disposition": 'attachment; filename="pj-result.dxf"'})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ââ AI line art via Replicate controlnet-hed âââââââââââââââââââââââââââââââââ

def _ai_line_art(img_bgr):
    """
    Convert photo â clean line art using jagilley/controlnet-hed via Replicate.
    Returns uint8 numpy array: 0=black lines, 255=white background.
    Raises exception if REPLICATE_API_TOKEN not set or call fails.
    """
    import cv2, numpy as np, replicate as _replicate, io as _io
    import urllib.request as _urllib_req
    from PIL import Image as _PilImage

    if not os.environ.get("REPLICATE_API_TOKEN"):
        raise Exception("REPLICATE_API_TOKEN not set")

    # Resize to 768px max (sweet spot for controlnet-hed)
    h, w = img_bgr.shape[:2]
    if max(h, w) > 768:
        ratio = 768 / max(h, w)
        img_bgr = cv2.resize(img_bgr, (int(w*ratio), int(h*ratio)),
                             interpolation=cv2.INTER_AREA)

    # Encode as JPEG for Replicate (must use io.BytesIO, not base64)
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil_img = _PilImage.fromarray(img_rgb)
    buf = _io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=90)
    buf.seek(0)

    output = _replicate.run(
        "jagilley/controlnet-hed:cde353130c86f37d0af4060cd757ab3009cac68caca13c21294823a8cef1f63e",
        input={
            "input_image": buf,
            "prompt": "line art, white background, pure black outline, coloring book, clean lines, no shading, no color fill, vector illustration",
            "num_samples": "1",
            "image_resolution": "512",
            "ddim_steps": 30,
            "scale": 9,
            "a_prompt": "best quality, extremely detailed, white background, crisp clean black lines",
            "n_prompt": "color, shading, gray tone, texture, noise, blur, photorealistic, painting, watercolor, sketch marks, hatching"
        }
    )

    # Handle all Replicate SDK output formats (>= 0.25):
    # may be iterator, list of FileOutput, list of URL strings, or single item
    if hasattr(output, '__iter__') and not isinstance(output, (str, bytes)):
        output_list = list(output)
    else:
        output_list = [output] if output is not None else []

    if not output_list:
        raise Exception("Replicate returned no output")

    item = output_list[0]

    # Fetch image bytes from whatever format Replicate returns
    if isinstance(item, str):
        # URL string
        with _urllib_req.urlopen(item) as resp:
            img_bytes = resp.read()
    elif hasattr(item, 'read'):
        # FileOutput / file-like object
        img_bytes = item.read()
    elif hasattr(item, 'url'):
        # Object with .url attribute
        with _urllib_req.urlopen(item.url) as resp:
            img_bytes = resp.read()
    else:
        raise Exception(f"Unrecognized Replicate output type: {type(item)}")

    arr = np.frombuffer(img_bytes, np.uint8)
    result_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if result_bgr is None:
        raise Exception("Cannot decode Replicate output image")

    # AI output is often already near-binary; OTSU handles both clean and noisy cases
    gray = cv2.cvtColor(result_bgr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary  # 0=black lines, 255=white background

# ââ Local line art: XDoG (Extended Difference of Gaussians) ââââââââââââââââââ

def _local_line_art(img_bgr):
    """
    Convert photo â clean line art using XDoG algorithm.
    Gives crisp illustration + anime/cartoon style edges for laser cutting.
    Returns uint8 numpy array: 0=black lines, 255=white background.
    """
    import cv2, numpy as np

    # Resize to max 1200px
    h, w = img_bgr.shape[:2]
    if max(h, w) > 1200:
        ratio = 1200 / max(h, w)
        img_bgr = cv2.resize(img_bgr, (int(w*ratio), int(h*ratio)),
                             interpolation=cv2.INTER_AREA)

    # Normalize contrast with CLAHE on L channel
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_ch)
    img_bgr = cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2BGR)

    # Light bilateral filter: smooth noise, preserve edges
    smooth = cv2.bilateralFilter(img_bgr, d=9, sigmaColor=75, sigmaSpace=75)

    # Convert to float grayscale [0, 1]
    gray = cv2.cvtColor(smooth, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0

    # ââ XDoG (Extended Difference of Gaussians) ââââââââââââââââââââââââââââââ
    # Parameters tuned for illustration + anime combined style
    sigma   = 1.0    # base Gaussian sigma (edge sharpness)
    k       = 1.6    # sigma ratio â k*sigma for second Gaussian
    epsilon = 0.98   # threshold level (higher = fewer, cleaner lines)
    phi     = 200.0  # steepness of soft threshold (higher = crisper edges)

    g1 = cv2.GaussianBlur(gray, (0, 0), sigma)
    g2 = cv2.GaussianBlur(gray, (0, 0), sigma * k)

    # DoG response: positive = background, negative = edge
    dog = g1 - epsilon * g2

    # XDoG soft threshold: background â 1.0, edges â 0.0
    xdog = np.where(dog >= 0, 1.0, 1.0 + np.tanh(phi * dog))
    xdog = np.clip(xdog, 0.0, 1.0)

    # Convert to uint8 (255=white bg, 0=black lines) and binarize
    result = (xdog * 255).astype(np.uint8)
    _, binary = cv2.threshold(result, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Remove tiny speckle noise
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    return binary  # 0=black lines, 255=white background

# ââ Photo line art: bilateral smoothing + k-means quantization âââââââââââââââ

def _photo_line_art(img_bgr):
    """
    Convert real photo â clean line art for laser cutting.

    Pipeline:
      1. Fast GrabCut on downscaled image â subject mask (removes background)
      2. Background filled with neutral gray â no spurious edge at boundary
      3. Heavy bilateral (7 passes)        â kills skin/fabric texture
      4. K-means 4 clusters                â merge into major colour zones
      5. Canny(18,50) on quantized         â main region boundary lines
      6. Detail pass: Canny(120,300) on lightly blurred â eyes, glasses, hair
      7. Mask edges to subject region      â suppress background objects
      8. Connected-component size filter   â remove tiny noise blobs
      9. Dilate Ã1                         â thicken for laser visibility

    Returns uint8 numpy array: 0=black lines, 255=white background.
    """
    import cv2, numpy as np

    # ââ Step 0: Resize to max 1000 px ââââââââââââââââââââââââââââââââââââââââ
    h, w = img_bgr.shape[:2]
    if max(h, w) > 1000:
        ratio = 1000 / max(h, w)
        img_bgr = cv2.resize(img_bgr, (int(w * ratio), int(h * ratio)),
                             interpolation=cv2.INTER_AREA)
    h, w = img_bgr.shape[:2]

    # ââ Step 1: Fast GrabCut at 400 px â subject mask ââââââââââââââââââââââââ
    # Downscale for speed (~0.7 s), upscale result mask back to full size
    scale  = 400 / max(h, w)
    small  = cv2.resize(img_bgr, (int(w * scale), int(h * scale)))
    sh, sw = small.shape[:2]
    gc_mask = np.zeros(small.shape[:2], np.uint8)
    bgd_m   = np.zeros((1, 65), np.float64)
    fgd_m   = np.zeros((1, 65), np.float64)
    rect    = (int(sw * 0.01), int(sh * 0.01), int(sw * 0.97), int(sh * 0.97))
    cv2.grabCut(small, gc_mask, rect, bgd_m, fgd_m, 3, cv2.GC_INIT_WITH_RECT)
    fg_small = np.where((gc_mask == 1) | (gc_mask == 3), 255, 0).astype(np.uint8)
    fg = cv2.resize(fg_small, (w, h), interpolation=cv2.INTER_NEAREST)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((20, 20), np.uint8))
    fg = cv2.dilate(fg, np.ones((5, 5), np.uint8), iterations=2)

    # ââ Step 2: Background â neutral gray ââââââââââââââââââââââââââââââââââââ
    work = img_bgr.copy()
    work[fg == 0] = [195, 195, 195]

    # ââ Step 3: Heavy bilateral â 7 passes (kills texture, preserves edges) ââ
    smooth = work.copy()
    for _ in range(7):
        smooth = cv2.bilateralFilter(smooth, d=11, sigmaColor=90, sigmaSpace=90)

    # ââ Step 4: K-means 4 clusters on smoothed image âââââââââââââââââââââââââ
    pixel_data = smooth.reshape((-1, 3)).astype(np.float32)
    criteria   = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, centers = cv2.kmeans(
        pixel_data, 4, None, criteria, 5, cv2.KMEANS_RANDOM_CENTERS)
    centers   = np.uint8(centers)
    quantized = centers[labels.flatten()].reshape(smooth.shape)
    gray_q    = cv2.cvtColor(quantized, cv2.COLOR_BGR2GRAY)
    edges1    = cv2.Canny(gray_q, 18, 50)

    # ââ Step 5: Detail pass â high-threshold Canny on lightly blurred âââââââââ
    med = work.copy()
    for _ in range(3):
        med = cv2.bilateralFilter(med, d=7, sigmaColor=50, sigmaSpace=50)
    gray_m = cv2.cvtColor(med, cv2.COLOR_BGR2GRAY)
    edges2 = cv2.Canny(gray_m, 120, 300)

    # ââ Step 6: Combine + morphological close ââââââââââââââââââââââââââââââââ
    combined = cv2.bitwise_or(edges1, edges2)
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    # ââ Step 7: Mask to subject region âââââââââââââââââââââââââââââââââââââââ
    fg_edge  = cv2.dilate(fg, np.ones((6, 6), np.uint8), iterations=2)
    combined = cv2.bitwise_and(combined, fg_edge)

    # ââ Step 8: Remove tiny noise (connected components < 20 px) âââââââââââââ
    nb, out, stats, _ = cv2.connectedComponentsWithStats(combined)
    cleaned = np.zeros_like(combined)
    for i in range(1, nb):
        if stats[i, cv2.CC_STAT_AREA] > 20:
            cleaned[out == i] = 255

    # ââ Step 9: Dilate for laser visibility âââââââââââââââââââââââââââââââââââ
    cleaned = cv2.dilate(cleaned, np.ones((2, 2), np.uint8), iterations=1)

    # Invert: lines=black(0), background=white(255)
    return cv2.bitwise_not(cleaned)

@app.post("/preprocess")
async def preprocess_image(image: UploadFile, mode: str = Form("auto")):
    """Color image -> B&W line art PNG. mode: auto, cartoon, photo"""
    try:
        import cv2, numpy as np
        contents = await image.read()
        arr = np.frombuffer(contents, np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            raise HTTPException(status_code=400, detail="Cannot decode image")

        max_size = 2000
        h, w = img_bgr.shape[:2]
        if max(h, w) > max_size:
            ratio = max_size / max(h, w)
            img_bgr = cv2.resize(img_bgr, (int(w*ratio), int(h*ratio)), interpolation=cv2.INTER_AREA)

        detected_mode = mode
        if mode == "auto":
            hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            v = hsv[:,:,2]
            total = img_bgr.shape[0] * img_bgr.shape[1]
            near_white = float((v > 200).sum()) / total
            near_black = float((v < 50).sum()) / total
            bw_ratio = near_white + near_black   # high â already B&W line art
            detected_mode = "cartoon" if bw_ratio > 0.85 else "photo"

        if detected_mode == "cartoon":
            hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            dark_mask = (hsv[:,:,2] < 100).astype(np.uint8) * 255
            kernel = np.ones((2,2), np.uint8)
            dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_OPEN, kernel)
            dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_CLOSE, kernel)
            _, png_buf = cv2.imencode('.png', cv2.bitwise_not(dark_mask))
            return Response(
                content=png_buf.tobytes(),
                media_type="image/png",
                headers={"X-Mode": detected_mode, "Access-Control-Expose-Headers": "X-Mode"}
            )
        else:
            # photo mode: AI (controlnet-hed) â photo line art (bilateral+kmeans) â Canny fallback
            sketch_arr = None
            ai_used = False
            try:
                sketch_arr = _ai_line_art(img_bgr)
                ai_used = True
            except Exception:
                pass
            if sketch_arr is None:
                try:
                    sketch_arr = _photo_line_art(img_bgr)
                except Exception:
                    pass
            if sketch_arr is not None:
                _, png_buf = cv2.imencode('.png', sketch_arr)
                return Response(
                    content=png_buf.tobytes(),
                    media_type="image/png",
                    headers={"X-Mode": detected_mode, "X-AI-Used": str(ai_used).lower(), "Access-Control-Expose-Headers": "X-Mode, X-AI-Used"}
                )
            # Canny fallback
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.bilateralFilter(gray, 9, 75, 75)
            edges = cv2.Canny(gray, 40, 120)
            kernel = np.ones((2,2), np.uint8)
            edges = cv2.dilate(edges, kernel, iterations=1)
            _, png_buf = cv2.imencode('.png', cv2.bitwise_not(edges))
            return Response(
                content=png_buf.tobytes(),
                media_type="image/png",
                headers={"X-Mode": detected_mode, "X-AI-Used": "false", "Access-Control-Expose-Headers": "X-Mode, X-AI-Used"}
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/trace")
async def trace_image(image: UploadFile, threshold: int = Form(128), invert: str = Form("false")):
    invert_bool = invert.lower() == "true"
    try:
        import cv2, numpy as np
        contents = await image.read()
        arr = np.frombuffer(contents, np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)

        # ââ resize ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
        max_size = 2000
        if img_bgr is not None:
            h, w = img_bgr.shape[:2]
            if max(h, w) > max_size:
                ratio = max_size / max(h, w)
                img_bgr = cv2.resize(img_bgr, (int(w*ratio), int(h*ratio)), interpolation=cv2.INTER_AREA)
            out_w, out_h = img_bgr.shape[1], img_bgr.shape[0]
        else:
            # fallback PIL path
            img_pil = Image.open(io.BytesIO(contents)).convert("RGB")
            if max(img_pil.size) > max_size:
                ratio = max_size / max(img_pil.size)
                img_pil = img_pil.resize((int(img_pil.width*ratio), int(img_pil.height*ratio)), Image.LANCZOS)
            img_bgr = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
            out_w, out_h = img_pil.width, img_pil.height

        # ââ Detect photo vs cartoon ââââââââââââââââââââââââââââââââââââââââââ
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        v = hsv[:, :, 2]
        total_px = img_bgr.shape[0] * img_bgr.shape[1]
        bw_ratio = float((v > 200).sum() + (v < 50).sum()) / total_px
        is_photo = bw_ratio <= 0.85

        # ââ AI â local (photo/cartoon) â Canny fallback âââââââââââââââââââââ
        bw_array = None
        sketch_ok = False

        # Try AI (controlnet-hed) first
        ai_used = False
        try:
            sketch = _ai_line_art(img_bgr)
            bw_array = cv2.bitwise_not(sketch) if invert_bool else sketch
            sketch_ok = True
            ai_used = True
        except Exception:
            pass

        # Local fallback: photo â bilateral+kmeans, cartoon â XDoG
        if bw_array is None:
            try:
                if is_photo:
                    sketch = _photo_line_art(img_bgr)
                else:
                    sketch = _local_line_art(img_bgr)
                bw_array = cv2.bitwise_not(sketch) if invert_bool else sketch
                sketch_ok = True
            except Exception:
                bw_array = None

        # ââ Canny/threshold fallback âââââââââââââââââââââââââââââââââââââââââ
        if bw_array is None:
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (3, 3), 0)
            _, bw_array = cv2.threshold(gray, 0, 255,
                                        cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            if invert_bool:
                bw_array = cv2.bitwise_not(bw_array)

        # ââ Encode result ââââââââââââââââââââââââââââââââââââââââââââââââââââ
        ok, buf = cv2.imencode(".png", bw_array)
        if not ok:
            raise HTTPException(status_code=500, detail="PNG encode failed")
        return Response(content=buf.tobytes(), media_type="image/png",
                        headers={"X-sketch-ok": str(sketch_ok), "X-AI-Used": str(ai_used).lower(), "Access-Control-Expose-Headers": "X-sketch-ok, X-AI-Used"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
