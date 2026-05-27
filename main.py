from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image
import subprocess, tempfile, os, io, re, math, base64, asyncio, urllib.request
import xml.etree.ElementTree as ET
import ezdxf

app = FastAPI(title="PJ Backend", version="4.4.0")

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
    return {"status": "PJ Backend v4.3 - AI line art (Replicate) + correct DXF scale + bezier arcs"}

@app.get("/health")
def health():
    try:
        r = subprocess.run(["potrace", "--version"], capture_output=True, timeout=5)
        potrace_ok = r.returncode == 0
    except Exception:
        potrace_ok = False
    has_token = bool(os.environ.get("REPLICATE_API_TOKEN"))
    return {"status": "ok", "potrace": potrace_ok, "replicate_token": has_token}

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

# ââ Replicate AI helper (shared by /preprocess and /trace) âââââââââââââââââââ

async def _replicate_line_art(img_bgr) -> bytes | None:
    """
    Convert photo â clean line art via Replicate.
    Tries fofr/controlnet-lineart first (high quality portrait lineart),
    falls back to carolineec/informativedrawings if it fails.
    Returns PNG bytes, or None if both fail.
    """
    import cv2, sys
    replicate_token = os.environ.get("REPLICATE_API_TOKEN")
    if not replicate_token:
        return None

    # Resize to max 768px â ControlNet works best at this range
    h, w = img_bgr.shape[:2]
    max_dim = 768
    if max(h, w) > max_dim:
        ratio = max_dim / max(h, w)
        img_bgr = cv2.resize(img_bgr, (int(w*ratio), int(h*ratio)),
                             interpolation=cv2.INTER_AREA)

    def encode_img():
        _, enc = cv2.imencode('.jpg', img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
        buf = io.BytesIO(enc.tobytes())
        buf.name = "image.jpg"
        return buf

    def read_output(output):
        if isinstance(output, list):
            output = output[0]
        if hasattr(output, 'read'):
            return output.read()
        with urllib.request.urlopen(str(output)) as r:
            return r.read()

    import replicate as _replicate
    loop = asyncio.get_running_loop()

    # ââ Primary: fofr/controlnet-lineart âââââââââââââââââââââââââââââââââââââ
    # ControlNet lineart preprocessor â SD generation guided by lineart structure
    # Produces clean portrait line art with proper hair / face / clothing detail
    try:
        img_io = encode_img()
        output = await loop.run_in_executor(
            None,
            lambda: _replicate.run(
                "fofr/controlnet-lineart",
                input={
                    "image":               img_io,
                    "prompt":              "detailed line art portrait, white background, clean black lines, professional illustration, no colors",
                    "negative_prompt":     "color, shading, gradient, noise, blur, background, watermark",
                    "num_outputs":         1,
                    "guidance_scale":      9.0,
                    "num_inference_steps": 20,
                }
            )
        )
        result = read_output(output)
        print("[Replicate] controlnet-lineart OK", file=sys.stderr, flush=True)
        return result
    except Exception as err:
        print(f"[Replicate][controlnet-lineart] {type(err).__name__}: {err}", file=sys.stderr, flush=True)

    # ââ Fallback: carolineec/informativedrawings ââââââââââââââââââââââââââââââ
    try:
        img_io = encode_img()
        output = await loop.run_in_executor(
            None,
            lambda: _replicate.run(
                "carolineec/informativedrawings",
                input={"image": img_io}
            )
        )
        result = read_output(output)
        print("[Replicate] informativedrawings OK (fallback)", file=sys.stderr, flush=True)
        return result
    except Exception as err:
        print(f"[Replicate][informativedrawings] {type(err).__name__}: {err}", file=sys.stderr, flush=True)
        return None

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
            result_bytes = None
            _, png_buf = cv2.imencode('.png', cv2.bitwise_not(dark_mask))
            result_bytes = png_buf.tobytes()
            return Response(
                content=result_bytes,
                media_type="image/png",
                headers={"X-Mode": detected_mode, "Access-Control-Expose-Headers": "X-Mode"}
            )
        else:
            # photo mode: Replicate AI first, Canny fallback
            ai_bytes = await _replicate_line_art(img_bgr)
            if ai_bytes:
                return Response(
                    content=ai_bytes,
                    media_type="image/png",
                    headers={"X-Mode": detected_mode, "Access-Control-Expose-Headers": "X-Mode"}
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
                headers={"X-Mode": detected_mode, "Access-Control-Expose-Headers": "X-Mode"}
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

        # ââ try Replicate AI for clean line art ââââââââââââââââââââââââââââââ
        # AI line art â potrace = dramatically better curves than Canny â potrace
        bw_array = None
        ai_bytes = await _replicate_line_art(img_bgr)
        if ai_bytes:
            try:
                ai_arr = np.frombuffer(ai_bytes, np.uint8)
                ai_img = cv2.imdecode(ai_arr, cv2.IMREAD_GRAYSCALE)
                if ai_img is not None:
                    # AI output: white background, black lines
                    # Invert if needed (make black = ink), then threshold
                    _, bw_array = cv2.threshold(ai_img, 200, 255,
                                                cv2.THRESH_BINARY if invert_bool else cv2.THRESH_BINARY_INV)
            except Exception:
                bw_array = None

        # ââ Canny/threshold fallback âââââââââââââââââââââââââââââââââââââââââ
        if bw_array is None:
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (3,3), 0)
            gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8)).apply(gray)
            mode_flag = cv2.THRESH_BINARY_INV if invert_bool else cv2.THRESH_BINARY
            _, bw_array = cv2.threshold(gray, threshold, 255, mode_flag)
            bw_array = cv2.morphologyEx(bw_array, cv2.MORPH_CLOSE, np.ones((2,2), np.uint8))

        bw_pil = Image.fromarray(bw_array)

        bw_pil = Image.fromarray(bw_array)

        with tempfile.TemporaryDirectory() as tmpdir:
            bmp_path = os.path.join(tmpdir, "input.bmp")
            svg_path = os.path.join(tmpdir, "output.svg")
            bw_pil.save(bmp_path)
            result = subprocess.run(
                ["potrace","--svg","--flat",
                 "--turdsize","2",
                 "--alphamax","1",
                 "--opttolerance","0.4",
                 "-o", svg_path, bmp_path],
                capture_output=True, timeout=60)
            if result.returncode != 0:
                raise Exception("Potrace error: " + result.stderr.decode())
            with open(svg_path,"r",encoding="utf-8") as f:
                svg_content = f.read()

        return {"svg": svg_content,
                "path_count": len(re.findall(r"<path", svg_content)),
                "width": out_w, "height": out_h,
                "ai_used": ai_bytes is not None}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
