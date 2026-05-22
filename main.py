from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
import subprocess, tempfile, os, io, re, base64

app = FastAPI(title="PJ Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ปรับเป็น domain จริงหลัง deploy
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "PJ Backend v2.0 — Potrace + OpenCV"}

@app.get("/health")
def health():
    # Check potrace available
    try:
        r = subprocess.run(["potrace", "--version"], capture_output=True, timeout=5)
        potrace_ok = r.returncode == 0
    except Exception:
        potrace_ok = False
    return {"status": "ok", "potrace": potrace_ok}

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

        # ── ปรับขนาดถ้าใหญ่เกินไป (max 2000px) ──
        max_size = 2000
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        # ── OpenCV preprocessing ──
        import cv2
        import numpy as np

        img_array = np.array(img)
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)

        # 1. Noise reduction
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        # 2. Contrast enhancement (CLAHE)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

        # 3. Apply threshold
        if invert_bool:
            _, bw = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)
        else:
            _, bw = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)

        # 4. Morphological cleanup (ลบ noise จุดเล็กๆ)
        kernel = np.ones((2, 2), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)

        # แปลง numpy array กลับเป็น PIL Image
        bw_pil = Image.fromarray(bw)

        # ── Potrace: แปลง bitmap → SVG ──
        with tempfile.TemporaryDirectory() as tmpdir:
            bmp_path = os.path.join(tmpdir, "input.bmp")
            svg_path = os.path.join(tmpdir, "output.svg")

            bw_pil.save(bmp_path)

            result = subprocess.run(
                [
                    "potrace",
                    "--svg",           # output format: SVG
                    "--flat",          # flat SVG (path per subpath, ดี for selection)
                    "--turdsize", "4", # ลบ noise จุดเล็ก < 4px
                    "--alphamax", "1", # corner threshold
                    "-o", svg_path,
                    bmp_path
                ],
                capture_output=True, timeout=60
            )

            if result.returncode != 0:
                raise Exception("Potrace error: " + result.stderr.decode())

            with open(svg_path, "r", encoding="utf-8") as f:
                svg_content = f.read()

        # นับ paths
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
