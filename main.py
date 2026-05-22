from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image
import subprocess, tempfile, os, io, re, math
import xml.etree.ElementTree as ET
import ezdxf

app = FastAPI(title="PJ Backend", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "PJ Backend v3.0 — Potrace + OpenCV + ezdxf"}

@app.get("/health")
def health():
    try:
        r = subprocess.run(["potrace", "--version"], capture_output=True, timeout=5)
        potrace_ok = r.returncode == 0
    except Exception:
        potrace_ok = False
    return {"status": "ok", "potrace": potrace_ok}


# ── SVG Path → DXF with Bulge ────────────────────────────────

def parse_svg_commands(d: str):
    """Tokenize SVG path d attribute into (command, [args]) list."""
    tokens = re.findall(
        r'[MmCcLlZzHhVvSsQqTtAa]|[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?', d
    )
    commands = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if re.match(r'[MmCcLlZzHhVvSsQqTtAa]', tok):
            cmd = tok
            i += 1
            args = []
            while i < len(tokens) and not re.match(r'[MmCcLlZzHhVvSsQqTtAa]', tokens[i]):
                args.append(float(tokens[i]))
                i += 1
            commands.append((cmd, args))
        else:
            i += 1
    return commands


def compute_bulge(p0, pm, p1):
    """
    DXF bulge = tan(theta/4) for arc p0→p1 passing through pm.
    Positive = CCW, Negative = CW.
    """
    cx = p1[0] - p0[0]
    cy = p1[1] - p0[1]
    chord = math.sqrt(cx * cx + cy * cy)
    if chord < 1e-10:
        return 0.0
    nx, ny = -cy / chord, cx / chord          # normal (CCW)
    mx = (p0[0] + p1[0]) / 2
    my = (p0[1] + p1[1]) / 2
    sagitta = (pm[0] - mx) * nx + (pm[1] - my) * ny
    return 2.0 * sagitta / chord


def bezier_at(p0, cp1, cp2, p3, t):
    """Point on cubic Bezier at parameter t."""
    mt = 1 - t
    x = mt**3*p0[0] + 3*mt**2*t*cp1[0] + 3*mt*t**2*cp2[0] + t**3*p3[0]
    y = mt**3*p0[1] + 3*mt**2*t*cp1[1] + 3*mt*t**2*cp2[1] + t**3*p3[1]
    return (x, y)


def svg_path_to_polylines(d: str, svg_h: float):
    """
    Convert SVG path 'd' → list of (vertices, is_closed).
    vertices = list of [x, y, bulge]  (Y flipped for DXF).
    Each M command starts a new subpath → no stray connecting lines.
    """
    commands = parse_svg_commands(d)
    result = []
    verts = []
    cx, cy = 0.0, 0.0
    sx, sy = 0.0, 0.0
    closed = False

    def fy(y): return svg_h - y

    def flush():
        if len(verts) >= 2:
            result.append((list(verts), closed))

    def add_bezier(p0x, p0y, cp1x, cp1y, cp2x, cp2y, p3x, p3y):
        nonlocal cx, cy
        pm = bezier_at((p0x,p0y),(cp1x,cp1y),(cp2x,cp2y),(p3x,p3y), 0.5)
        bulge = compute_bulge(
            (p0x, fy(p0y)), (pm[0], fy(pm[1])), (p3x, fy(p3y))
        )
        if verts:
            verts[-1][2] = bulge
        verts.append([p3x, fy(p3y), 0.0])
        cx, cy = p3x, p3y

    for cmd, args in commands:

        if cmd == 'M':
            flush(); verts.clear(); closed = False
            cx, cy = args[0], args[1]; sx, sy = cx, cy
            verts.append([cx, fy(cy), 0.0])
            for j in range(2, len(args), 2):
                cx, cy = args[j], args[j+1]
                verts.append([cx, fy(cy), 0.0])

        elif cmd == 'm':
            flush(); verts.clear(); closed = False
            cx += args[0]; cy += args[1]; sx, sy = cx, cy
            verts.append([cx, fy(cy), 0.0])
            for j in range(2, len(args), 2):
                cx += args[j]; cy += args[j+1]
                verts.append([cx, fy(cy), 0.0])

        elif cmd == 'C':
            for j in range(0, len(args), 6):
                add_bezier(cx,cy, args[j],args[j+1],
                           args[j+2],args[j+3], args[j+4],args[j+5])

        elif cmd == 'c':
            for j in range(0, len(args), 6):
                add_bezier(cx,cy,
                           cx+args[j],   cy+args[j+1],
                           cx+args[j+2], cy+args[j+3],
                           cx+args[j+4], cy+args[j+5])

        elif cmd == 'L':
            for j in range(0, len(args), 2):
                cx, cy = args[j], args[j+1]
                verts.append([cx, fy(cy), 0.0])

        elif cmd == 'l':
            for j in range(0, len(args), 2):
                cx += args[j]; cy += args[j+1]
                verts.append([cx, fy(cy), 0.0])

        elif cmd in ('Z', 'z'):
            closed = True; cx, cy = sx, sy

    flush()
    return result


def build_dxf(svg_content: str, selected: str = "all") -> bytes:
    """Convert SVG string to DXF bytes with proper arc (bulge) representation."""
    svg_clean = re.sub(r'\sxmlns(?::\w+)?="[^"]*"', '', svg_content)
    svg_clean = re.sub(r'<\?xml[^?]*\?>', '', svg_clean).strip()

    try:
        root = ET.fromstring(svg_clean)
    except ET.ParseError:
        svg_clean = re.sub(r'<svg[^>]*>', '<svg>', svg_clean, count=1)
        root = ET.fromstring(svg_clean)

    vb = root.get('viewBox', '0 0 500 500').split()
    svg_h = float(vb[3]) if len(vb) >= 4 else 500.0

    all_paths = [el for el in root.iter() if el.tag.lower().endswith('path')]

    if selected.strip() == "all":
        chosen = all_paths
    else:
        idxs = {int(x) for x in selected.split(',') if x.strip()}
        chosen = [p for i, p in enumerate(all_paths) if i in idxs]

    doc = ezdxf.new('R2010')
    doc.header['$INSUNITS'] = 4    # mm
    doc.header['$MEASUREMENT'] = 1  # metric
    msp = doc.modelspace()

    for path_el in chosen:
        d = path_el.get('d', '').strip()
        if not d:
            continue
        for verts, is_closed in svg_path_to_polylines(d, svg_h):
            if len(verts) < 2:
                continue
            pts = [(v[0], v[1], 0.0, 0.0, v[2]) for v in verts]
            msp.add_lwpolyline(pts, dxfattribs={
                'layer': '0',
                'closed': is_closed
            })

    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode('utf-8')


# ── Endpoints ─────────────────────────────────────────────────

@app.post("/export-dxf")
async def export_dxf(
    svg: str = Form(...),
    selected: str = Form("all"),
):
    try:
        dxf_bytes = build_dxf(svg, selected)
        return Response(
            content=dxf_bytes,
            media_type="application/dxf",
            headers={"Content-Disposition": 'attachment; filename="pj-result.dxf"'}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trace")
async def trace_image(
    image: UploadFile,
    threshold: int = Form(128),
    invert: str = Form("false")
):
    invert_bool = invert.lower() == "true"
    try:
        contents = await image.read()
        img = Image.open(io.BytesIO(contents)).convert("RGB")

        max_size = 2000
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            img = img.resize(
                (int(img.width*ratio), int(img.height*ratio)), Image.LANCZOS
            )

        import cv2, numpy as np
        img_array = np.array(img)
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (3,3), 0)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        gray = clahe.apply(gray)

        mode = cv2.THRESH_BINARY_INV if invert_bool else cv2.THRESH_BINARY
        _, bw = cv2.threshold(gray, threshold, 255, mode)

        kernel = np.ones((2,2), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
        bw_pil = Image.fromarray(bw)

        with tempfile.TemporaryDirectory() as tmpdir:
            bmp_path = os.path.join(tmpdir, "input.bmp")
            svg_path = os.path.join(tmpdir, "output.svg")
            bw_pil.save(bmp_path)

            result = subprocess.run(
                ["potrace", "--svg", "--flat",
                 "--turdsize", "4", "--alphamax", "1",
                 "-o", svg_path, bmp_path],
                capture_output=True, timeout=60
            )
            if result.returncode != 0:
                raise Exception("Potrace error: " + result.stderr.decode())

            with open(svg_path, "r", encoding="utf-8") as f:
                svg_content = f.read()

        path_count = len(re.findall(r"<path", svg_content))
        return {
            "svg": svg_content,
            "path_count": path_count,
            "width": img.width,
            "height": img.height
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
