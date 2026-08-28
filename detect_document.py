#!/usr/bin/env python3
"""
detect_document.py
Detects the boundary of a rectangular identity document (ID card, passport
page) within a larger image that may contain background clutter, then
measures its size and compares it against ISO/IEC 7810 ID-1 / ID-3 formats.

Usage:
    python3 detect_document.py <image_path> [--dpi X] [--visualize out.jpg] [--no-mrz] [--no-llm]
"""

import cv2
import numpy as np
import argparse
import sys
import os
import base64
import json

try:
    import pytesseract
    HAVE_TESSERACT = True
except ImportError:
    HAVE_TESSERACT = False

ID_RATIOS = {"ID-1 Format": 85.60 / 53.98, "ID-3 Format": 125.00 / 88.00}
MIN_AREA_FRACTION = 0.10
MAX_AREA_FRACTION = 0.90
MIN_COLOR_TRANSITION = 18.0
WHOLE_IMAGE_TRUST_THRESHOLD = 6.0


def ratio_deviation(w, h):
    ratio = max(w, h) / min(w, h)
    closest = min(ID_RATIOS, key=lambda k: abs(ID_RATIOS[k] - ratio))
    dev = abs(ratio - ID_RATIOS[closest]) / ID_RATIOS[closest] * 100
    return ratio, closest, dev


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
    """Find the rotation angle that makes OCR text most readable. Works for
    any document with readable text (not just passports with an MRZ)."""
    h, w = gray.shape
    scale = min(1.0, 650 / max(h, w))
    small = cv2.resize(gray, (int(w * scale), int(h * scale))) if scale < 1.0 else gray
    sh, sw = small.shape

    if hint_angle is not None:
        hinted_range = range(int(hint_angle * 10) - 99, int(hint_angle * 10) + 100, 8)
        angle, score = _search_angle_range(small, sw, sh, hinted_range)
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
    fine_range = range(int(best_angle * 10) - 39, int(best_angle * 10) + 40, 8)
    return _search_angle_range(small, sw, sh, fine_range)


def _looks_mrz_like(text):
    t = text.strip()
    if len(t) < 15:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<")
    matches = sum(1 for c in t.upper() if c in allowed)
    return matches / len(t) >= 0.75


def find_mrz_box(gray, angle):
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(gray, M, (w, h), borderValue=255)
    data = pytesseract.image_to_data(rotated, output_type=pytesseract.Output.DICT)
    boxes = []
    for i in range(len(data['text'])):
        text = data['text'][i]
        if _looks_mrz_like(text):
            x, y, bw, bh = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
            if bw >= 150:
                boxes.append((x, y, bw, bh))
    if len(boxes) < 1:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1, y1)


def hough_boundary_from_angle(img, angle):
    """Find the document's true outer boundary using Hough line detection,
    filtered to lines aligned with (or perpendicular to) the given angle,
    then taking the extreme line clusters as the page edges."""
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
        vals = np.array(line).flatten()
        x1, y1, x2, y2 = int(vals[0]), int(vals[1]), int(vals[2]), int(vals[3])
        length = np.hypot(x2 - x1, y2 - y1)
        if length < 150:
            continue
        line_theta = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        tn = ((line_theta + 90) % 180) - 90
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

    def uv_to_xy(u_val, v_val):
        return u_val * u + v_val * v

    corners = np.array([
        uv_to_xy(left_u, top_v), uv_to_xy(right_u, top_v),
        uv_to_xy(right_u, bottom_v), uv_to_xy(left_u, bottom_v)
    ], dtype=np.float32)
    return corners


def sane_boundary(img, corners, max_dev=20):
    """Sanity-check a candidate boundary: plausible document ratio, and
    covers a reasonable (not tiny, not near-total) fraction of the image."""
    if corners is None:
        return False
    rect = cv2.minAreaRect(corners.astype(np.float32))
    (_, _), (rw, rh), _ = rect
    if rw <= 0 or rh <= 0:
        return False
    _, _, dev = ratio_deviation(rw, rh)
    img_area = img.shape[0] * img.shape[1]
    detected_area = rw * rh
    return dev <= max_dev and 0.05 * img_area <= detected_area <= 0.98 * img_area


def _color_transition_strength(img, rect):
    (cx, cy), (rw, rh), angle = rect
    h, w = img.shape[:2]
    inner_rect = ((cx, cy), (rw * 0.85, rh * 0.85), angle)
    outer_rect = ((cx, cy), (rw * 1.20, rh * 1.20), angle)
    inner_mask = np.zeros((h, w), dtype=np.uint8)
    outer_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(inner_mask, [cv2.boxPoints(inner_rect).astype(int)], 255)
    cv2.fillPoly(outer_mask, [cv2.boxPoints(outer_rect).astype(int)], 255)
    ring_mask = cv2.bitwise_and(outer_mask, cv2.bitwise_not(inner_mask))
    if cv2.countNonZero(inner_mask) < 50 or cv2.countNonZero(ring_mask) < 50:
        return 0.0
    inner_mean = cv2.mean(img, mask=inner_mask)[:3]
    ring_mean = cv2.mean(img, mask=ring_mask)[:3]
    return float(np.linalg.norm(np.array(inner_mean) - np.array(ring_mean)))


def _find_candidates(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 11, 75, 75)
    med = float(np.median(filtered))
    edges = cv2.Canny(filtered, int(max(0, 0.66 * med)), int(min(255, 1.33 * med)))
    kernel = np.ones((5, 5), np.uint8)
    edges = cv2.dilate(edges, kernel, iterations=2)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return contours


def detect_via_generic_edges(img):
    contours = _find_candidates(img)
    if not contours:
        return None
    img_area = img.shape[0] * img.shape[1]
    scored = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_AREA_FRACTION * img_area or area > MAX_AREA_FRACTION * img_area:
            continue
        rect = cv2.minAreaRect(c)
        (_, _), (rw, rh), _ = rect
        if rw == 0 or rh == 0:
            continue
        _, _, dev = ratio_deviation(rw, rh)
        transition = _color_transition_strength(img, rect)
        scored.append((rect, dev, transition))
    trusted = [s for s in scored if s[2] >= MIN_COLOR_TRANSITION]
    if not trusted:
        return None
    best = min(trusted, key=lambda s: s[1])
    return best[0]


def get_llm_rough_estimate(image_path):
    """Ask a vision-capable model for a rough rotation estimate. Returns
    rotation_degrees or None if unavailable (no boto3, no AWS credentials,
    network issue, etc.) -- this must fail gracefully since the rest of
    the pipeline works fully offline without it."""
    try:
        import boto3
        session = boto3.Session()
        client = session.client("bedrock-runtime", region_name="ap-southeast-2")

        with open(image_path, "rb") as f:
            image_bytes = f.read()
        img_small = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        h, w = img_small.shape[:2]
        scale = min(1.0, 1024 / max(h, w))
        if scale < 1.0:
            img_small = cv2.resize(img_small, (int(w * scale), int(h * scale)))
        ok, buf = cv2.imencode(".jpg", img_small)
        image_b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

        prompt_lines = [
            "Look at this image, which contains a rectangular identity document (passport, ID card, or driver's license) possibly on a background with other objects.",
            "",
            "Estimate the document's in-plane rotation angle in degrees (0 = upright, positive = rotated clockwise). Respond with ONLY a JSON object, no other text:",
            "{\"rotation_degrees\": <number>}"
        ]
        prompt = "\n".join(prompt_lines)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        }
        response = client.invoke_model(
            modelId="au.anthropic.claude-haiku-4-5-20251001-v1:0",
            body=json.dumps(body)
        )
        result = json.loads(response["body"].read())
        usage = result.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        log_path = os.environ.get("TOKEN_LOG_FILE")
        if log_path:
            with open(log_path, "a") as lf:
                lf.write(f"{in_tok},{out_tok}\n")
        text = result["content"][0]["text"]
        text = text.strip().strip("`").replace("json", "", 1).strip()
        parsed = json.loads(text)
        return float(parsed["rotation_degrees"])
    except Exception:
        return None


def classify_size(width_mm, height_mm):
    ratio, closest_label, dev = ratio_deviation(width_mm, height_mm)
    if dev < 4:
        status = "Pass"
    elif dev <= 6:
        status = "Warning"
    else:
        status = "Fail"
    return ratio, closest_label, dev, status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image_path")
    ap.add_argument("--dpi", type=float, default=0.0)
    ap.add_argument("--visualize", default=None)
    ap.add_argument("--no-mrz", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    img = cv2.imread(args.image_path)
    if img is None:
        print(f"ERROR: could not read image {args.image_path}", file=sys.stderr)
        sys.exit(1)

    h, w = img.shape[:2]
    img_area = h * w
    corners = None
    method = None

    llm_angle_hint = None
    if not args.no_llm:
        llm_angle_hint = get_llm_rough_estimate(args.image_path)

    if HAVE_TESSERACT and not args.no_mrz:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        refined_angle, text_score = find_text_angle(gray, hint_angle=llm_angle_hint)

        if text_score >= 15:
            mrz_box = find_mrz_box(gray, refined_angle)
            if mrz_box is not None:
                candidate = hough_boundary_from_angle(img, refined_angle)
                if sane_boundary(img, candidate):
                    corners = candidate
                    method = "mrz-anchored"
            if corners is None:
                candidate = hough_boundary_from_angle(img, refined_angle)
                if sane_boundary(img, candidate):
                    corners = candidate
                    method = "text-angle-hough"

    if corners is None:
        _, _, whole_dev = ratio_deviation(w, h)
        if whole_dev < WHOLE_IMAGE_TRUST_THRESHOLD:
            method = "whole-image-good-match"
        else:
            rect = detect_via_generic_edges(img)
            if rect is not None:
                (cx, cy), (rw, rh), angle = rect
                detected_area = rw * rh
                if MIN_AREA_FRACTION * img_area <= detected_area <= MAX_AREA_FRACTION * img_area:
                    corners = cv2.boxPoints(rect)
                    method = "boundary-detected"
            if corners is None and method is None:
                method = "whole-image-fallback"

    if corners is not None:
        rect = cv2.minAreaRect(corners.astype(np.float32))
        (_, _), (rw, rh), _ = rect
        px_w, px_h = max(rw, rh), min(rw, rh)
    else:
        px_w, px_h = float(max(w, h)), float(min(w, h))

    if args.dpi > 0:
        width_mm = px_w / args.dpi * 25.4
        height_mm = px_h / args.dpi * 25.4
    else:
        width_mm = height_mm = 0.0

    if width_mm > 0 and height_mm > 0:
        ratio, closest_label, dev, status = classify_size(width_mm, height_mm)
    else:
        ratio, closest_label, dev = ratio_deviation(px_w, px_h)
        status = "Pass" if dev < 4 else ("Warning" if dev <= 6 else "Fail")

    print(f"{px_w:.0f},{px_h:.0f},{width_mm:.2f},{height_mm:.2f},{ratio:.4f},{closest_label},{dev:.2f},{status},{method}")

    if args.visualize:
        vis = img.copy()
        if corners is not None:
            cv2.polylines(vis, [corners.astype(np.int32)], True, (0, 0, 255), 4)
        else:
            cv2.rectangle(vis, (2, 2), (w - 2, h - 2), (0, 255, 0), 4)
        cv2.imwrite(args.visualize, vis)


if __name__ == "__main__":
    main()
