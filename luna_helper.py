#!/usr/bin/env python3
"""
luna_helper.py
Shared call to GPT-5.6 Luna (AWS Bedrock OpenAI-compatible endpoint) for the
dimension-check tools. Needs the same environment variables as the claim
classifier:  OPENAI_API_KEY  and  OPENAI_BASE_URL.

Luna is a reasoning model, so a small budget can be used up on invisible
reasoning, leaving no visible answer. ask_luna() therefore tries a small
budget first and retries once with a much larger one only when needed.
"""

import os
import base64
import cv2
import numpy as np

MODEL = "global.openai.gpt-5.6-luna"


def encode_image(image_path, max_side=1600):
    """Read an image, shrink it so the longest side is at most max_side,
    and return it as a base64 JPEG string."""
    with open(image_path, "rb") as f:
        raw = f.read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not decode image {image_path}")
    h, w = img.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", img)
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _log_tokens(resp):
    log_path = os.environ.get("TOKEN_LOG_FILE")
    if not log_path:
        return
    try:
        with open(log_path, "a") as lf:
            lf.write(f"{resp.usage.prompt_tokens},{resp.usage.completion_tokens}\n")
    except Exception:
        pass


def ask_luna(prompt, image_b64, first_budget, retry_budget):
    """Send prompt + image. Returns the visible text, or None if Luna gave
    no visible answer even after the larger retry."""
    from openai import OpenAI
    client = OpenAI()

    def call(max_tok):
        resp = client.chat.completions.create(
            model=MODEL,
            max_completion_tokens=max_tok,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }],
        )
        _log_tokens(resp)
        return resp.choices[0].message.content

    content = call(first_budget)
    if content is None or not content.strip():
        content = call(retry_budget)
    if content is None or not content.strip():
        return None
    return content.strip()
