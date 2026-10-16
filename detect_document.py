#!/usr/bin/env python3
"""
detect_document.py  (v3)

Order of work:
  1. Classify the document (AI) -- or accept the label from --doc-type.
  2. Map the label to ONE reference standard (ID-1, ID-3, Two-Page Spread).
  3. Only then run the dimension check against that standard.

Documents that are not identity documents are reported "Not Applicable".
Documents the AI could not identify are reported "Needs Manual Review".

Usage:
    python3 detect_document.py <image> [--dpi X] [--doc-type "Passport"]
                               [--visualize out.jpg] [--no-llm] [--no-mrz]

Output (one CSV line, field order unchanged so the shell script still works):
    px_w,px_h,width_mm,height_mm,ratio,standard,deviation_pct,status,method
"""

import cv2
import numpy as np
import argparse
import sys
import os
import json

try:
    import pytesseract
    HAVE_TESSERACT = True
except ImportError:
    HAVE_TESSERACT = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DOCUMENT_STANDARDS = {
    "ID-1 Format": {"width_mm": 85.60, "height_mm": 53.98, "orientation": "landscape"},
    "ID-3 Format": {"width_mm": 88.00, "height_mm": 125.00, "orientation": "portrait"},
    "Two-Page Spread": {"width_mm": 176.00, "height_mm": 125.00, "orientation": "landscape"},
}

MIN_AREA_FRACTION = 0.10
MAX_AREA_FRACTION = 0.90
MIN_COLOR_TRANSITION = 18.0
PASS_BELOW = 4.0
WARN_UP_TO = 6.0


# ---------------------------------------------------------------- step 1-2
def get_document_type(image_path):
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from classify_document import classify as classify_doc_type
        label = classify_doc_type(image_path)
        if not label or label.strip().lower() in ("unclassified", "unknown"):
            return None
        return label.strip()
    except Exception:
        return None


def map_doc_type_to_standard(label, is_two_page_spread):
    """Return a standard name, or None when the label is not an ID document."""
    if not label:
        return None
    t = label.strip().lower()
    if "passport" in t:
        return "Two-Page Spread" if is_two_page_spread else "ID-3 Format"
    for word in ("licence", "license", "id card", "photo id", "identity card"):
        if word in t:
            return "ID-1 Format"
    return None


# ---------------------------------------------------------------- step 3
def standard_ratio(name):
    s = DOCUMENT_STANDARDS[name]
    return max(s["width_mm"], s["height_mm"]) / min(s["width_mm"], s["height_mm"])


def orientation_of(w, h):
    return "landscape" if w >= h else "portrait"


def check_against_standard(w, h, standard_name):
    ratio = max(w, h) / min(w, h)
    expected = standard_ratio(standard_name)
    dev = abs(ratio - expected) / expected * 100
    orientation_ok = orientation_of(w, h) == DOCUMENT_STANDARDS[standard_name]["orientation"]
    if not orientation_ok:
        status = "Fail"
    elif dev < PASS_BELOW:
        status = "Pass"
    elif dev <= WARN_UP_TO:
        status = "Warning"
    else:
        status = "Fail"
    return ratio, dev, orientation_ok, status


def _closest_standard_dev(w, h):
    ratio = max(w, h) / min(w, h)
    return min(abs(ratio - standard_ratio(k)) / standard_ratio(k) * 100 for k in DOCUMENT_STANDARDS)


def _alnum_score(text):
    return sum(c.isalnum() for c in text)


def _search_angle_range(small, sw, sh, search_range):
    best_angle, best_score = 0, 0
    for angle10 in search_range:
        angle = angle10 / 10.0
        M = cv2.getRotationMatrix2D((sw / 2, sh / 2), angle, 1.0)
        rotated = cv2.warpAffine(small, M, (sw, sh), borderValue=255)
        score = _alnum_score(pytesseract.image_to_string(rotated))
        if score > best_score:
            best_score, best_angle = score, angle
    return best_angle, best_score


def find_text_angle(gray, hint_angle=None):
    h, w = gray.shape
    scale = min(1.0, 650 / max(h, w))
    small = cv2.resize(gray, (int(w * scale), int(h * scale))) if scale < 1.0 else gray
    sh, sw = small.shape
    if hint_angle is not None:
        hinted = range(int(hint_angle * 10) - 99, int(hint_angle * 10) + 100, 8)
        angle, score = _search_angle_range(small, sw, sh, hinted)
        if score >= 40:
            return angle, score
    best_angle, best_score = 0, 0
    for angle in range(-20, 21, 5):
        M = cv2.getRotationMatrix2D((sw / 2, sh / 2), angle, 1.0)
        rotated = cv2.warpAffine(small, M, (sw, sh), borderValue=255)
        score = _alnum_score(pytesseract.image_to_string(rotated))
        if score > best_score:
            best_score, best_angle = score, angle
    if best_score < 15:
        return best_angle, best_score
    fine = range(int(best_angle * 10) - 39, int(best_angle * 10) + 40, 8)
    return _search_angle_range(small, sw, sh, fine)


def _looks_mrz_like(text):
    t = text.strip()
    if len(t) < 15:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<")
    return sum(1 for c in t.upper() if c in allowed) / len(t) >= 0.75


def find_mrz_box(gray, angle):
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(gray, M, (w, h), borderValue=255)
    data = pytesseract.image_to_data(rotated, output_type=pytesseract.Output.DICT)
    boxes = []
    for i in range(len(data["text"])):
        if _looks_mrz_like(data["text"][i]) and data["width"][i] >= 150:
            boxes.append((data["left"][i], data["top"][i], data["width"][i], data["height"][i]))
    return boxes[0] if boxes else None


def hough_boundary_from_angle(img, angle):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 11, 75, 75)
    med = float(np.median(filtered))
    edges = cv2.Canny(filtered, int(0.66 * med), int(1.33 * med))
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80, minLineLength=150, maxLineGap=20)
    if lines is None:
        return None
    theta = np.radians(angle)
    u = np.array([np.cos(theta), np.sin(theta)])
    v = np.array([-np.sin(theta), np.cos(theta)])
    along_pos, across_pos = [], []
    for line in lines:
        x1, y1, x2, y2 = [int(a) for a in np.array(line).flatten()[:4]]
        if np.hypot(x2 - x1, y2 - y1) < 150:
            continue
        tn = ((np.degrees(np.arctan2(y2 - y1, x2 - x1)) + 90) % 180) - 90
        d1 = abs(((tn - angle + 90) % 180) - 90)
        d2 = abs(((tn - (angle + 90) + 90) % 180) - 90)
        mid = np.array([(x1 + x2) / 2, (y1 + y2) / 2])
        if d1 <= 6:
            along_pos.append(np.dot(mid, v))
        elif d2 <= 6:
            across_pos.append(np.dot(mid, u))
    if len(along_pos) < 4 or len(across_pos) < 2:
        return None
    along_pos.sort()
    across_pos.sort()
    top_v, bottom_v = along_pos[0], along_pos[-1]
    left_u, right_u = across_pos[0], across_pos[-1]
    to_xy = lambda a, b: a * u + b * v
    return np.array([to_xy(left_u, top_v), to_xy(right_u, top_v),
                     to_xy(right_u, bottom_v), to_xy(left_u, bottom_v)], dtype=np.float32)


def sane_boundary(img, corners, max_dev=20):
    if corners is None:
        return False
    (_, _), (rw, rh), _ = cv2.minAreaRect(corners.astype(np.float32))
    if rw <= 0 or rh <= 0:
        return False
    area = rw * rh
    img_area = img.shape[0] * img.shape[1]
    return _closest_standard_dev(rw, rh) <= max_dev and 0.05 * img_area <= area <= 0.98 * img_area


def detect_via_mrz(img, hint_angle=None):
    if not HAVE_TESSERACT:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    angle, score = find_text_angle(gray, hint_angle=hint_angle)
    if score < 20 or find_mrz_box(gray, angle) is None:
        return None
    corners = hough_boundary_from_angle(img, angle)
    return corners if sane_boundary(img, corners) else None


def _color_transition_strength(img, rect):
    (cx, cy), (rw, rh), angle = rect
    h, w = img.shape[:2]
    inner = np.zeros((h, w), dtype=np.uint8)
    outer = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(inner, [cv2.boxPoints(((cx, cy), (rw * 0.85, rh * 0.85), angle)).astype(int)], 255)
    cv2.fillPoly(outer, [cv2.boxPoints(((cx, cy), (rw * 1.20, rh * 1.20), angle)).astype(int)], 255)
    ring = cv2.bitwise_and(outer, cv2.bitwise_not(inner))
    if cv2.countNonZero(inner) < 50 or cv2.countNonZero(ring) < 50:
        return 0.0
    a = np.array(cv2.mean(img, mask=inner)[:3])
    b = np.array(cv2.mean(img, mask=ring)[:3])
    return float(np.linalg.norm(a - b))


def _interior_angles(quad):
    q = order_corners(quad)
    if q is None:
        return None
    tl, tr, bl, br = q
    ring = [tl, tr, br, bl]
    angles = []
    for i in range(4):
        p, c, n = ring[i - 1], ring[i], ring[(i + 1) % 4]
        v1, v2 = p - c, n - c
        cosang = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9)
        angles.append(np.degrees(np.arccos(np.clip(cosang, -1, 1))))
    return angles


def _quad_from_contour(c):
    """Four corners of a contour (a true quadrilateral, so a document photographed
    at an angle keeps its trapezoid shape). The quadrilateral is only trusted if it
    really fits the outline; otherwise the rotated rectangle is used, as before."""
    box = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
    hull = cv2.convexHull(c)
    hull_area = cv2.contourArea(hull)
    peri = cv2.arcLength(hull, True)
    if hull_area <= 0:
        return box
    for eps in (0.01, 0.02, 0.03, 0.04, 0.06):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) != 4:
            continue
        quad = approx.reshape(4, 2).astype(np.float32)
        if not cv2.isContourConvex(approx):
            continue
        if cv2.contourArea(quad) < 0.93 * hull_area:      # quad cuts off part of the outline
            continue
        angles = _interior_angles(quad)
        if angles is None or min(angles) < 50 or max(angles) > 130:
            continue
        return quad
    return box


def _sample_profile(gray, pt, normal, band):
    """Grey values along the normal through pt, from -band (inside) to +band (outside)."""
    offsets = np.arange(-band, band + 1, 1.0, dtype=np.float32)
    xs = (pt[0] + offsets * normal[0]).astype(np.float32).reshape(1, -1)
    ys = (pt[1] + offsets * normal[1]).astype(np.float32).reshape(1, -1)
    prof = cv2.remap(gray, xs, ys, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)[0]
    return offsets, prof.astype(np.float32)


def _fit_side(gray, p0, p1, center, band):
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
        grad[:2] = 0
        grad[-2:] = 0
        if grad.max() < 12:
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


def _intersect(l1, l2):
    (p, r), (q, s) = l1, l2
    cross = r[0] * s[1] - r[1] * s[0]
    if abs(cross) < 1e-6:
        return None
    t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / cross
    return p + t * r


def refine_quad_by_edges(img, quad):
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
        ln = _fit_side(gray, ring[i], ring[(i + 1) % 4], center, band)
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


def detect_via_generic_edges(img, standard_name):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 11, 75, 75)
    med = float(np.median(filtered))
    edges = cv2.Canny(filtered, int(max(0, 0.66 * med)), int(min(255, 1.33 * med)))
    kernel = np.ones((5, 5), np.uint8)
    edges = cv2.dilate(edges, kernel, iterations=2)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    img_area = img.shape[0] * img.shape[1]
    expected = standard_ratio(standard_name)
    best, best_dev = None, None
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_AREA_FRACTION * img_area or area > MAX_AREA_FRACTION * img_area:
            continue
        rect = cv2.minAreaRect(c)
        (_, _), (rw, rh), _ = rect
        if rw == 0 or rh == 0:
            continue
        if _color_transition_strength(img, rect) < MIN_COLOR_TRANSITION:
            continue
        dev = abs(max(rw, rh) / min(rw, rh) - expected) / expected * 100
        if best_dev is None or dev < best_dev:
            best, best_dev = c, dev
    if best is None:
        return None
    quad = _quad_from_contour(best)
    refined = refine_quad_by_edges(img, quad)
    return refined if refined is not None else quad


# ------------------------------------------------ perspective correction
ASSUMED_FOCAL_FRACTION = 0.9      # typical phone camera: focal length ~0.9 x longest image side
FLAT_SCORE = 1.02                 # opposite edges within 2% -> treat as photographed head-on
MAX_SCORE_TWO_AXIS = 1.60         # beyond this the angle is too steep to trust
MAX_SCORE_ASSUMED_F = 1.25        # only one axis tilted -> focal length is a guess, be stricter


def order_corners(pts):
    """Return (top-left, top-right, bottom-left, bottom-right) or None."""
    pts = np.array(pts, dtype=np.float64).reshape(-1, 2)
    if len(pts) != 4:
        return None
    s = pts.sum(axis=1)
    d = pts[:, 0] - pts[:, 1]
    tl, br = pts[np.argmin(s)], pts[np.argmax(s)]
    tr, bl = pts[np.argmax(d)], pts[np.argmin(d)]
    if len({tuple(np.round(p, 1)) for p in (tl, tr, bl, br)}) < 4:
        return None
    return tl, tr, bl, br


def _edge_lengths(quad):
    tl, tr, bl, br = quad
    return (np.linalg.norm(tr - tl), np.linalg.norm(br - bl),
            np.linalg.norm(bl - tl), np.linalg.norm(br - tr))


def plain_width_height(quad):
    top, bot, lef, rig = _edge_lengths(quad)
    return (top + bot) / 2.0, (lef + rig) / 2.0


def perspective_score(quad):
    """1.0 = opposite edges equal (head-on). Larger = more angled."""
    top, bot, lef, rig = _edge_lengths(quad)
    return max(top / bot, bot / top, lef / rig, rig / lef)


def corrected_ratio(quad, img_w, img_h):
    """True width/height of a rectangle seen in perspective (Zhang & He method).
    Returns (ratio_w_over_h, how) where how is 'plain', 'perspective' or
    'perspective-assumed-focal'."""
    pw, ph = plain_width_height(quad)
    plain = pw / ph
    if perspective_score(quad) <= FLAT_SCORE:
        return plain, "plain"
    tl, tr, bl, br = quad
    m1, m2, m3, m4 = [np.array([p[0], p[1], 1.0]) for p in (tl, tr, bl, br)]
    u0, v0 = img_w / 2.0, img_h / 2.0
    longest = float(max(img_w, img_h))
    try:
        k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
        k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    except Exception:
        return plain, "plain"
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    if not np.isfinite([k2, k3]).all():
        return plain, "plain"

    f, how = None, "perspective"
    if abs(n2[2] * n3[2]) > 1e-9:
        f2 = -1.0 / (n2[2] * n3[2]) * (
            (n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0 + n2[2] * n3[2] * u0 ** 2) +
            (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0 + n2[2] * n3[2] * v0 ** 2))
        if np.isfinite(f2) and f2 > 0 and 0.4 * longest <= np.sqrt(f2) <= 2.5 * longest:
            f = float(np.sqrt(f2))
    if f is None:
        f, how = ASSUMED_FOCAL_FRACTION * longest, "perspective-assumed-focal"

    A = np.array([[f, 0, u0], [0, f, v0], [0, 0, 1.0]])
    Ai = np.linalg.inv(A)
    M = Ai.T @ Ai
    num, den = float(n2 @ M @ n2), float(n3 @ M @ n3)
    if num <= 0 or den <= 0:
        return plain, "plain"
    return float(np.sqrt(num / den)), how


def too_angled(quad, how):
    score = perspective_score(quad)
    limit = MAX_SCORE_ASSUMED_F if how == "perspective-assumed-focal" else MAX_SCORE_TWO_AXIS
    return score > limit


# ---------------------------------------------------------------- AI hint
def get_llm_rough_estimate(image_path):
    """Ask GPT-5.6 Luna for (rotation_degrees, has_background, is_two_page_spread).
    Returns (None, None, None) if unavailable -- the check still works without it."""
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from luna_helper import encode_image, ask_luna
        image_b64 = encode_image(image_path, max_side=1024)
        prompt = "\n".join([
            "Look at this image, which contains a rectangular identity document (passport, ID card, or driver's license) possibly on a background with other objects.",
            "",
            "Answer three questions about this image:",
            "1. What is the document's in-plane rotation angle in degrees (0 = upright, positive = rotated clockwise)?",
            "2. Does the image show visible background around the document (e.g. a table, desk, other objects, or empty space), or is it already tightly cropped to just the document with little to no visible background?",
            "3. Is this a passport shown as a two-page open spread (both facing pages visible, typically with a visible spine/crease down the middle), rather than a single page?",
            "",
            "Respond with ONLY a JSON object, no other text:",
            "{\"rotation_degrees\": <number>, \"has_background\": <true or false>, \"is_two_page_spread\": <true or false>}",
        ])
        text = ask_luna(prompt, image_b64, first_budget=150, retry_budget=600)
        if text is None:
            return None, None, None
        text = text.strip().strip("`").replace("json", "", 1).strip()
        p = json.loads(text)
        return (float(p["rotation_degrees"]), bool(p.get("has_background", False)),
                bool(p.get("is_two_page_spread", False)))
    except Exception as e:
        print(f"LLM HINT ERROR: {e}", file=sys.stderr)
        return None, None, None


# ---------------------------------------------------------------- main
def emit(px_w, px_h, w_mm, h_mm, ratio, standard, dev, status, method):
    print(f"{px_w:.0f},{px_h:.0f},{w_mm:.2f},{h_mm:.2f},{ratio:.4f},{standard},{dev:.2f},{status},{method}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image_path")
    ap.add_argument("--dpi", type=float, default=0.0)
    ap.add_argument("--doc-type", default=None, help="Label already produced by the classifier")
    ap.add_argument("--visualize", default=None)
    ap.add_argument("--no-mrz", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    img = cv2.imread(args.image_path)
    if img is None:
        print(f"ERROR: could not read image {args.image_path}", file=sys.stderr)
        sys.exit(1)
    h, w = img.shape[:2]

    # 1. classify first
    label = args.doc_type if args.doc_type is not None else get_document_type(args.image_path)
    if label is not None and label.strip().lower() in ("", "unknown", "unclassified"):
        label = None
    if label is None:
        emit(max(w, h), min(w, h), 0, 0, 0, "Unknown", 0, "Needs Manual Review", "type-unknown")
        return

    # AI hint (rotation / background / two-page spread) -- passports and IDs only
    hint_angle, has_background, is_spread = (None, None, None)
    looks_like_id = any(k in label.lower() for k in
                        ("passport", "licence", "license", "id card", "photo id", "identity card"))
    if looks_like_id and not args.no_llm:
        hint_angle, has_background, is_spread = get_llm_rough_estimate(args.image_path)

    # 2. choose the one reference standard from the classified type
    standard = map_doc_type_to_standard(label, bool(is_spread))
    if standard is None:
        emit(max(w, h), min(w, h), 0, 0, 0, label, 0, "Not Applicable", "non-id-document")
        return

    # 3. locate the document and measure it
    corners, method = None, "whole-image"
    if has_background is not False:
        if standard in ("ID-3 Format", "Two-Page Spread") and not args.no_mrz:
            corners = detect_via_mrz(img, hint_angle=hint_angle)
            method = "mrz-anchored" if corners is not None else method
        if corners is None:
            corners = detect_via_generic_edges(img, standard)
            method = "boundary-detected" if corners is not None else "whole-image-fallback"
    # 4. measure: correct for camera angle, then compare with the standard
    how = "plain"
    quad = order_corners(corners) if corners is not None else None
    if quad is not None:
        ratio_wh, how = corrected_ratio(quad, w, h)
        if too_angled(quad, how):
            pw, ph = plain_width_height(quad)
            emit(pw, ph, 0, 0, ratio_wh, standard, 0, "Needs Manual Review", method + "+too-angled")
            if args.visualize:
                vis = img.copy()
                cv2.polylines(vis, [corners.astype(np.int32)], True, (0, 0, 255), 4)
                cv2.imwrite(args.visualize, vis)
            return
        pw, ph = plain_width_height(quad)
        px_w, px_h = pw, pw / ratio_wh
        if how != "plain" and abs(ratio_wh - pw / ph) / (pw / ph) > 0.015:
            method += "+perspective-corrected"
    else:
        px_w, px_h = float(w), float(h)

    if args.dpi > 0:
        w_mm, h_mm = px_w / args.dpi * 25.4, px_h / args.dpi * 25.4
    else:
        w_mm = h_mm = 0.0

    ratio, dev, orientation_ok, status = check_against_standard(px_w, px_h, standard)
    if not orientation_ok:
        method += "+wrong-orientation"
    emit(px_w, px_h, w_mm, h_mm, ratio, standard, dev, status, method)

    if args.visualize:
        vis = img.copy()
        if corners is not None:
            cv2.polylines(vis, [np.array(corners).astype(np.int32)], True, (0, 0, 255), 4)
        else:
            cv2.rectangle(vis, (2, 2), (w - 2, h - 2), (0, 255, 0), 4)
        cv2.imwrite(args.visualize, vis)


if __name__ == "__main__":
    main()
