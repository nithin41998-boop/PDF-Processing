#!/usr/bin/env python3
"""
classify_document.py
Classifies a document image using a vision-capable LLM (GPT-5.6 Luna via the
AWS Bedrock OpenAI-compatible endpoint). Returns a single category label
describing what the document actually is (e.g. "Passport", "Driver's
License", "Bank Statement", "Insurance Claim", "Lottery Scam"), rather than
a generic ISO format label.

Needs OPENAI_API_KEY and OPENAI_BASE_URL in the environment.

Usage:
    python3 classify_document.py <image_path>

Output: a single line to stdout: the classification label.
Prints "Unknown" if classification is unavailable or fails.
"""

import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from luna_helper import encode_image, ask_luna


PROMPT = "\n".join([
    "Look at this image and identify what kind of document it is. Note that this may show only the front OR only the back side of a card-style document (e.g. a driver's licence back often shows large printed numbers, a barcode, and regulatory text rather than a photo or name).",
    "",
    "Respond with ONLY a short, neutral, descriptive category label (2-4 words), no other text, no punctuation at the end. Describe what the document presents itself as -- do not judge or state whether it is genuine, fake, fraudulent, or a scam. Examples of good labels: Passport, Driver's License, National ID Card, Bank Statement, Insurance Claim, Invoice, Visa Approval Letter, Job Offer Letter, Lottery Notification, Tech Support Notice, Antivirus Renewal Notice, Charity Donation Request, Package Delivery Notice, IRS Notice, Property Claim, Utility Bill, Employment Contract.",
    "",
    "Read any visible printed text carefully first -- if the text explicitly names the document type (e.g. the word 'licence', 'passport', 'statement', 'invoice'), use that as the primary basis for your answer rather than general visual impression.",
    "",
    "If you genuinely cannot tell what it is, respond with exactly: Unknown",
])


def classify(image_path):
    try:
        image_b64 = encode_image(image_path, max_side=1600)
        content = ask_luna(PROMPT, image_b64, first_budget=60, retry_budget=400)
        if content is None:
            return "Unknown"
        label = content.strip().strip(".").strip()
        if os.environ.get("DEBUG_LABEL"):
            print(f"RAW LABEL: {label!r}", file=sys.stderr)
        return label if label else "Unknown"
    except Exception as e:
        print(f"CLASSIFY ERROR on {image_path}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return "Unknown"


if __name__ == "__main__":
    print(classify(sys.argv[1]))
