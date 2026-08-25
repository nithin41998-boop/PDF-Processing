#!/usr/bin/env python3
"""
classify_document.py
Classifies a document image using a vision-capable LLM (Claude Haiku via
AWS Bedrock). Returns a single category label describing what the
document actually is (e.g. "Passport", "Driver's License", "Bank
Statement", "Insurance Claim", "Lottery Scam"), rather than a generic
ISO format label.

Usage:
    python3 classify_document.py <image_path>

Output: a single line to stdout: the classification label.
Prints "Unknown" if classification is unavailable or fails.
"""

import sys
import base64
import json
import cv2
import numpy as np


def classify(image_path):
    try:
        import boto3
        session = boto3.Session()
        client = session.client("bedrock-runtime", region_name="ap-southeast-2")

        with open(image_path, "rb") as f:
            image_bytes = f.read()
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        scale = min(1.0, 1024 / max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)))
        ok, buf = cv2.imencode(".jpg", img)
        image_b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
        prompt_lines = [
            "Look at this image and identify what kind of document it is. Note that this may show only the front OR only the back side of a card-style document (e.g. a driver's licence back often shows large printed numbers, a barcode, and regulatory text rather than a photo or name).",
            "",
            "Respond with ONLY a short, neutral, descriptive category label (2-4 words), no other text, no punctuation at the end. Describe what the document presents itself as -- do not judge or state whether it is genuine, fake, fraudulent, or a scam. Examples of good labels: Passport, Driver's License, National ID Card, Bank Statement, Insurance Claim, Invoice, Visa Approval Letter, Job Offer Letter, Lottery Notification, Tech Support Notice, Antivirus Renewal Notice, Charity Donation Request, Package Delivery Notice, IRS Notice, Property Claim, Utility Bill, Employment Contract.",
            "",
            "Read any visible printed text carefully first -- if the text explicitly names the document type (e.g. the word 'licence', 'passport', 'statement', 'invoice'), use that as the primary basis for your answer rather than general visual impression.",
            "",
            "If you genuinely cannot tell what it is, respond with exactly: Unknown"
        ]

        prompt = "\n".join(prompt_lines)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 30,
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
        label = result["content"][0]["text"].strip().strip(".").strip()
        return label if label else "Unknown"
    except Exception as e:
        return "Unknown"


if __name__ == "__main__":
    print(classify(sys.argv[1]))
