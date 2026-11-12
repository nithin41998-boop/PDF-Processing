#!/usr/bin/env python3
"""
sam_retry.py -- try SAM 2 only on the documents that do not Pass.

For each image:
  1. run the normal dimension check (detect_document.py, unchanged);
  2. if the result is Pass, stop -- SAM 2 is not used;
  3. otherwise ask Luna for the card's rough corners, give them to SAM 2 as a
     box, turn the mask into four corners, snap them to the real edges and
     measure again.
It prints both results and what the final answer would be under each setting:
  rescue=review : a first-time Fail that SAM 2 passes becomes Needs Manual Review
  rescue=pass   : it becomes a Pass
detect_document.py is NOT changed by this script.

Usage:
    python3 sam_retry.py IMAGE [IMAGE ...] [--boxdir DIR] [--doc-type "Driver Licence"]

Needs:  uv pip install sam2   (inside the .venv-da environment)
        the SAM 2 model file sam2.1_hiera_tiny.pt next to this script
        (or set SAM2_CHECKPOINT / SAM2_CONFIG), and the Luna key variables.
"""
import os
import sys
import json
import subprocess
import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import detect_document as d
from detect_document import _sample_profile, _intersect, order_corners
from luna_helper import encode_image, ask_luna

SAM2_CKPT = os.environ.get("SAM2_CHECKPOINT", os.path.join(HERE, "sam2.1_hiera_tiny.pt"))
SAM2_CFG = os.environ.get("SAM2_CONFIG", "configs/sam2.1/sam2.1_hiera_t.yaml")


# ---- edge fitting with a soft-edge second try (copied from the experimental version)
def _fit_side2(gray, p0, p1, center, band, soft=False):
    """Find the true edge near the side p0->p1 and fit a straight line to it.
    Scans each profile from the OUTSIDE inwards and takes the first strong edge,
    so thin light borders on the card are included. Returns (point, direction) or None."""
    d = p1 - p0
    length = float(np.linalg.norm(d))
    if length < 20:
        return None
    d = d / length
    n = np.array([d[1], -d[0]])
    mid = (p0 + p1) / 2.0
    if np.dot(mid - center, n) < 0:
        n = -n                                   # make n point outward
    pts = []
    for t in np.linspace(0.15, 0.85, 45):
        pt = p0 + t * length * (p1 - p0) / length
        offs, prof = _sample_profile(gray, pt, n, band)
        padded = np.pad(prof, 1, mode="edge")                 # no fake edge at the ends
        prof = np.convolve(padded, np.ones(3) / 3.0, mode="valid")
        grad = np.abs(np.diff(prof))
        if soft:
            # step response: compare the mean of 5 pixels either side, so a soft
            # (blurred) edge of modest contrast is still found
            k = 5
            step = np.zeros_like(grad)
            for j in range(k, len(grad) - k):
                step[j] = abs(prof[j + 1:j + 1 + k].mean() - prof[j - k + 1:j + 1].mean())
            grad = step
        grad[:2] = 0
        grad[-2:] = 0
        if grad.max() < (8 if soft else 12):
            continue
        thr = 0.45 * grad.max()
        for i in range(len(grad) - 2, 1, -1):       # outside -> inside
            if grad[i] >= thr and grad[i] >= grad[i - 1] and grad[i] >= grad[i + 1]:
                pos = offs[i] + 0.5
                pts.append(pt + pos * n)
                break
    if len(pts) < 15:
        return None
    pts = np.array(pts, dtype=np.float32)
    for _ in range(2):                                # fit, drop outliers, refit
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_HUBER, 0, 0.01, 0.01).flatten()
        dist = np.abs((pts[:, 0] - x0) * vy - (pts[:, 1] - y0) * vx)
        keep = dist < 2.5
        if keep.sum() < 0.5 * len(pts) or keep.sum() < 12:
            return None
        pts = pts[keep]
    vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_HUBER, 0, 0.01, 0.01).flatten()
    return np.array([x0, y0]), np.array([vx, vy])


def refine_quad_by_edges2(img, quad, soft_fallback=False):
    """Snap a rough quadrilateral onto the document's real edges by fitting a
    straight line along each side, then intersecting neighbouring lines. This
    ignores rounded corners and works for documents seen at an angle.
    Returns the refined 4x2 corners, or None if the fit is not trustworthy."""
    q = order_corners(quad)
    if q is None:
        return None
    tl, tr, bl, br = q
    ring = [tl, tr, br, bl]
    center = np.mean(ring, axis=0)
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    short_side = min(np.linalg.norm(tr - tl), np.linalg.norm(bl - tl))
    band = int(max(18, min(40, 0.06 * short_side)))
    lines = []
    for i in range(4):
        ln = _fit_side2(gray, ring[i], ring[(i + 1) % 4], center, band)
        if ln is None and soft_fallback:
            ln = _fit_side2(gray, ring[i], ring[(i + 1) % 4], center, band, soft=True)
        if ln is None:
            return None
        lines.append(ln)
    corners = []
    for i in range(4):
        c = _intersect(lines[i - 1], lines[i])
        if c is None:
            return None
        corners.append(c)
    corners = np.array(corners, dtype=np.float32)
    # sanity: each refined corner must stay close to the rough one
    diag = float(np.linalg.norm(tr - bl))
    for rough, new in zip([tl, tr, br, bl], [corners[0], corners[1], corners[2], corners[3]]):
        pass
    # ring order of lines: line i joins ring[i]->ring[i+1]; corner i = line(i-1) x line(i)
    for rough, new in zip(ring, corners):
        if np.linalg.norm(rough - new) > 0.12 * diag:
            return None
    if not cv2.isContourConvex(corners.reshape(-1, 1, 2)):
        return None
    return corners


# ------------------------------------------------------------------ steps
def first_check(path, doc_type=None):
    cmd = [sys.executable, os.path.join(HERE, "detect_document.py"), path]
    if doc_type:
        cmd += ["--doc-type", doc_type]
    r = subprocess.run(cmd, capture_output=True, text=True)
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    if not lines:
        return None
    f = lines[-1].split(",")
    if len(f) < 9:
        return None
    return {"standard": f[5], "dev": float(f[6]), "status": f[7], "method": f[8], "line": lines[-1]}


def luna_corners(path):
    prompt = "\n".join([
        "This image contains a rectangular identity document (a card or a passport page), possibly held in a hand or lying on a table.",
        "Where are the four outer corners of the document itself (NOT the hand, table or other objects)?",
        "Give each corner as [x, y] with x and y between 0 and 1, where [0,0] is the top-left of the image and [1,1] is the bottom-right.",
        "List them in order around the document: top-left, top-right, bottom-right, bottom-left, as it appears in the image.",
        "Respond with ONLY a JSON object, no other text: {\"corners\": [[x,y],[x,y],[x,y],[x,y]]}",
    ])
    text = ask_luna(prompt, encode_image(path, max_side=1024), first_budget=400, retry_budget=1200)
    if text is None:
        return None
    text = text.strip().strip("`").replace("json", "", 1).strip()
    try:
        cs = np.array(json.loads(text)["corners"], dtype=np.float32).reshape(4, 2)
    except Exception:
        return None
    return cs if (np.all(cs >= -0.02) and np.all(cs <= 1.02)) else None


_PRED = None

def sam2_quad(img, luna_rel):
    """SAM 2 mask for the card -> four corners in full-image pixels, or None."""
    global _PRED
    if _PRED is None:
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        _PRED = SAM2ImagePredictor(build_sam2(SAM2_CFG, SAM2_CKPT, device="cpu"))
    h, w = img.shape[:2]
    scale = min(1.0, 1024.0 / max(h, w))
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1.0 else img
    sh, sw = small.shape[:2]
    _PRED.set_image(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
    if luna_rel is not None:
        pts = luna_rel * np.array([sw, sh], dtype=np.float32)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        mx, my = 0.03 * (x1 - x0), 0.03 * (y1 - y0)
        box = np.array([max(0, x0 - mx), max(0, y0 - my), min(sw - 1, x1 + mx), min(sh - 1, y1 + my)])
        centre = pts.mean(axis=0)
    else:
        box = np.array([0.02 * sw, 0.02 * sh, 0.98 * sw, 0.98 * sh])
        centre = np.array([sw / 2.0, sh / 2.0])
    masks, scores, _ = _PRED.predict(point_coords=centre.reshape(1, 2), point_labels=np.array([1]),
                                     box=box, multimask_output=False)
    mask = (np.asarray(masks[0]) > 0).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.05 * sw * sh:
        return None
    quad = d._quad_from_contour(c)
    return np.array(quad, dtype=np.float32) / scale


def measure(img, quad, standard):
    q = order_corners(quad)
    ratio_wh, how = d.corrected_ratio(q, img.shape[1], img.shape[0])
    if d.too_angled(q, how):
        return "Needs Manual Review", 0.0, "sam2-corners-refined+too-angled"
    pw, ph = d.plain_width_height(q)
    px_w, px_h = pw, pw / ratio_wh
    ratio, dev, orientation_ok, status = d.check_against_standard(px_w, px_h, standard)
    method = "sam2-corners-refined" + ("" if orientation_ok else "+wrong-orientation")
    return status, dev, method


def main():
    args = sys.argv[1:]
    boxdir, doc_type, paths = None, None, []
    i = 0
    while i < len(args):
        if args[i] == "--boxdir":
            boxdir = args[i + 1]; i += 2
        elif args[i] == "--doc-type":
            doc_type = args[i + 1]; i += 2
        else:
            paths.append(args[i]); i += 1
    for path in paths:
        name = os.path.basename(path)
        first = first_check(path, doc_type)
        if first is None:
            print(f"{name}: first check gave no result")
            continue
        print(f"{name}")
        print(f"   first check : {first['status']} {first['dev']:.2f}% ({first['method']})")
        if first["status"] == "Pass":
            print("   SAM 2       : not used (already a Pass)")
            continue
        if first["standard"] not in d.DOCUMENT_STANDARDS:
            print("   SAM 2       : not used (not an identity document the check measures)")
            continue
        img = cv2.imread(path)
        h, w = img.shape[:2]
        rel = luna_corners(path)
        try:
            quad = sam2_quad(img, rel)
        except Exception as e:
            print(f"   SAM 2       : ERROR {e}")
            continue
        if quad is None:
            print("   SAM 2       : found no card")
            continue
        refined = refine_quad_by_edges2(img, quad, soft_fallback=True)
        if refined is None:
            print("   SAM 2       : card found but its edges could not be snapped -> Needs Manual Review")
            continue
        status, dev, method = measure(img, refined, first["standard"])
        print(f"   SAM 2       : {status} {dev:.2f}% ({method})")
        if boxdir:
            os.makedirs(boxdir, exist_ok=True)
            vis = img.copy()
            cv2.polylines(vis, [np.array(refined).astype(np.int32)], True, (0, 0, 255), 4)
            cv2.imwrite(os.path.join(boxdir, os.path.splitext(name)[0] + "_sam2.jpg"), vis)
        if status == "Pass":
            rv = "Needs Manual Review" if first["status"] == "Fail" else first["status"]
            print(f"   final       : rescue=review -> {rv}   |   rescue=pass -> Pass")
        else:
            print(f"   final       : {first['status']} (SAM 2 did not pass it, so the first result stands)")


if __name__ == "__main__":
    main()
