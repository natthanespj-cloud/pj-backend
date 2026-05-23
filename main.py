from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image
import subprocess, tempfile, os, io, re, math
import xml.etree.ElementTree as ET
import ezdxf

app = FastAPI(title="PJ Backend", version="4.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "PJ Backend v4.1 - Bezier subdivision + transform-aware DXF"}

@app.get("/health")
def health():
    try:
        r = subprocess.run(["potrace", "--version"], capture_output=True, timeout=5)
        potrace_ok = r.returncode == 0
    except Exception:
        potrace_ok = False
    return {"status": "ok", "potrace": potrace_ok}


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


def svg_path_to_polylines(d, sx, sy_abs):
    """
    Potrace SVG: <g transform="translate(0,H) scale(s,-s)">
    DXF_x = raw_x * sx,  DXF_y = raw_y * sy_abs  (no Y-flip)
    Each Bezier subdivided into ~6 DXF-unit segments (matching Convertio ~6.65 avg).
    """
    commands = parse_svg_commands(d)
    result = []; verts = []; cx = cy = 0.0; start_x = start_y = 0.0; closed = False

    def dx(x): return x * sx
    def dy(y): return y * sy_abs

    def flush():
        if len(verts) >= 2: result.append((list(verts), closed))

    def add_bezier(p0x, p0y, cp1x, cp1y, cp2x, cp2y, p3x, p3y):
        nonlocal cx, cy
        chord_dxf = math.sqrt((p3x-p0x)**2*sx**2 + (p3y-p0y)**2*sy_abs**2)
        n = min(60, max(2, int(chord_dxf / 6.0)))
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


def _svg_path_to_polylines_yflip(d, svg_h):
    """Fallback: plain SVG (Y-down) to DXF (Y-up), with bezier subdivision."""
    commands = parse_svg_commands(d)
    result = []; verts = []; cx = cy = 0.0; start_x = start_y = 0.0; closed = False

    def fy(y): return svg_h - y

    def flush():
        if len(verts) >= 2: result.append((list(verts), closed))

    def add_bezier(p0x, p0y, cp1x, cp1y, cp2x, cp2y, p3x, p3y):
        nonlocal cx, cy
        chord = math.sqrt((p3x-p0x)**2 + (p3y-p0y)**2)
        n = min(60, max(2, int(chord / 6.0)))
        prev = (p0x, p0y)
        for k in range(1, n+1):
            t2 = k/n; tm = (k-0.5)/n
            curr = bezier_at((p0x,p0y),(cp1x,cp1y),(cp2x,cp2y),(p3x,p3y),t2)
            mid  = bezier_at((p0x,p0y),(cp1x,cp1y),(cp2x,cp2y),(p3x,p3y),tm)
            b = compute_bulge((prev[0],fy(prev[1])),(mid[0],fy(mid[1])),(curr[0],fy(curr[1])))
            if verts: verts[-1][2] = b
            verts.append([curr[0],fy(curr[1]),0.0]); prev = curr
        cx, cy = p3x, p3y

    for cmd, args in commands:
        if cmd == 'M':
            flush(); verts.clear(); closed = False
            cx,cy=args[0],args[1]; start_x,start_y=cx,cy; verts.append([cx,fy(cy),0.0])
            for j in range(2,len(args),2):
                cx,cy=args[j],args[j+1]; verts.append([cx,fy(cy),0.0])
        elif cmd == 'm':
            flush(); verts.clear(); closed = False
            cx+=args[0]; cy+=args[1]; start_x,start_y=cx,cy; verts.append([cx,fy(cy),0.0])
            for j in range(2,len(args),2):
                cx+=args[j]; cy+=args[j+1]; verts.append([cx,fy(cy),0.0])
        elif cmd == 'C':
            for j in range(0,len(args),6):
                add_bezier(cx,cy,args[j],args[j+1],args[j+2],args[j+3],args[j+4],args[j+5])
        elif cmd == 'c':
            for j in range(0,len(args),6):
                add_bezier(cx,cy,cx+args[j],cy+args[j+1],cx+args[j+2],cy+args[j+3],cx+args[j+4],cy+args[j+5])
        elif cmd == 'L':
            for j in range(0,len(args),2):
                cx,cy=args[j],args[j+1]; verts.append([cx,fy(cy),0.0])
        elif cmd == 'l':
            for j in range(0,len(args),2):
                cx+=args[j]; cy+=args[j+1]; verts.append([cx,fy(cy),0.0])
        elif cmd in ('Z','z'):
            closed=True; cx,cy=start_x,start_y
    flush()
    return result


def build_dxf(svg_content, selected="all"):
    """Convert SVG string to DXF bytes."""
    svg_clean = re.sub(r'\sxmlns(?::\w+)?="[^"]*"', '', svg_content)
    svg_clean = re.sub(r'<\?xml[^?]*\?>', '', svg_clean).strip()
    try:
        root = ET.fromstring(svg_clean)
    except ET.ParseError:
        svg_clean = re.sub(r'<svg[^>]*>', '<svg>', svg_clean, count=1)
        root = ET.fromstring(svg_clean)

    vb = root.get('viewBox', '0 0 500 500').split()
    svg_h = float(vb[3]) if len(vb) >= 4 else 500.0

    scale_x = scale_y_abs = 1.0; has_transform = False
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ''
        if tag == 'g' or tag.endswith('}g'):
            t = el.get('transform', '')
            if t and 'scale' in t:
                _, _, sx, sy = parse_svg_transform(t)
                scale_x = abs(sx); scale_y_abs = abs(sy); has_transform = True; break

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
        polylines = (svg_path_to_polylines(d, scale_x, scale_y_abs) if has_transform
                     else _svg_path_to_polylines_yflip(d, svg_h))
        for verts, is_closed in polylines:
            if len(verts) < 2: continue
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

@app.post("/preprocess")
async def preprocess_image(image: UploadFile, mode: str = Form("auto")):
    """Color image â B&W line art PNG. mode: auto, cartoon, photo"""
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
            dark_ratio = float((hsv[:,:,2] < 80).sum()) / (img_bgr.shape[0] * img_bgr.shape[1])
            detected_mode = "cartoon" if dark_ratio > 0.03 else "photo"

        if detected_mode == "cartoon":
            hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            dark_mask = (hsv[:,:,2] < 100).astype(np.uint8) * 255
            kernel = np.ones((2,2), np.uint8)
            dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_OPEN, kernel)
            dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_CLOSE, kernel)
            result = cv2.bitwise_not(dark_mask)
        else:
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.bilateralFilter(gray, 9, 75, 75)
            edges = cv2.Canny(gray, 40, 120)
            kernel = np.ones((2,2), np.uint8)
            edges = cv2.dilate(edges, kernel, iterations=1)
            result = cv2.bitwise_not(edges)

        _, png_buf = cv2.imencode('.png', result)
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
        contents = await image.read()
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        max_size = 2000
        if max(img.size) > max_size:
            ratio = max_size/max(img.size)
            img = img.resize((int(img.width*ratio),int(img.height*ratio)), Image.LANCZOS)

        import cv2, numpy as np
        gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (3,3), 0)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8)).apply(gray)
        mode = cv2.THRESH_BINARY_INV if invert_bool else cv2.THRESH_BINARY
        _, bw = cv2.threshold(gray, threshold, 255, mode)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2,2), np.uint8))
        bw_pil = Image.fromarray(bw)

        with tempfile.TemporaryDirectory() as tmpdir:
            bmp_path = os.path.join(tmpdir, "input.bmp")
            svg_path = os.path.join(tmpdir, "output.svg")
            bw_pil.save(bmp_path)
            result = subprocess.run(
                ["potrace","--svg","--flat","--turdsize","4","--alphamax","1","-o",svg_path,bmp_path],
                capture_output=True, timeout=60)
            if result.returncode != 0:
                raise Exception("Potrace error: " + result.stderr.decode())
            with open(svg_path,"r",encoding="utf-8") as f:
                svg_content = f.read()

        return {"svg": svg_content,
                "path_count": len(re.findall(r"<path", svg_content)),
                "width": img.width, "height": img.height}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
