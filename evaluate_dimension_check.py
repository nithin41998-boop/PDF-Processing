#!/usr/bin/env python3
"""
evaluate_dimension_check.py
Runs detect_document.py on a folder of GENUINE images and a folder of MANIPULATED
images (made by make_manipulated.py, or your own), then scores the result.

Usage:
    python3 evaluate_dimension_check.py <genuine_folder> <manipulated_folder> [--out results.csv] [--no-llm] [--doc-type "Driver's License"]

Put this file in the same folder as detect_document.py. Set OPENAI_API_KEY and
OPENAI_BASE_URL first (the check asks the classifier for each image). --no-llm skips
the AI hint only; the classifier is still called.

Scoring (printed at the end):
  Genuine images:     Pass / Warning / Fail / Needs Manual Review / Not Applicable
  FRR (false reject)  = genuine images that FAIL, as % of all genuine images
  Manipulated images: scored per variant, and in two groups
     "shape changed"  (stretch, squash, crop)  -> expected NOT to pass
     "same shape"     (resize_50)              -> control, expected to pass
  FAR (false accept)  = shape-changed images that PASS, as % of shape-changed images
  FRR strict also counts Warning as a reject. For FAR, a Warning, Fail or Needs Manual Review
  all count as "caught"; only a Pass counts as accepted.
"""
import sys, os, csv, subprocess, collections

def run(script, path, no_llm, doc_type=None):
    cmd = [sys.executable, script, path, "--dpi", "0"]
    if doc_type:
        cmd += ["--doc-type", doc_type]
    if no_llm:
        cmd.append("--no-llm")
    r = subprocess.run(cmd, capture_output=True, text=True)
    line = (r.stdout.strip().splitlines() or [""])[-1]
    f = line.split(",")
    if len(f) < 9:
        return "Error", "", "", (r.stderr.strip().splitlines() or ["no output"])[-1][:120]
    return f[7], f[6], f[5], f[8]

def images(folder):
    return [os.path.join(folder, n) for n in sorted(os.listdir(folder))
            if n.lower().endswith((".jpg", ".jpeg", ".png"))]

def pct(a, b):
    return f"{100.0 * a / b:.1f}%" if b else "n/a"

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_llm = "--no-llm" in sys.argv
    doc_type = None
    if "--doc-type" in sys.argv:
        doc_type = sys.argv[sys.argv.index("--doc-type") + 1]
        args = [a for a in args if a != doc_type]
    out = "results.csv"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        args = [a for a in args if a != out]
    if len(args) != 2:
        print(__doc__); sys.exit(1)
    gen, man = args
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "detect_document.py")
    rows = []
    for group, folder in (("genuine", gen), ("manipulated", man)):
        files = images(folder)
        for i, p in enumerate(files, 1):
            status, dev, std, method = run(script, p, no_llm, doc_type)
            name = os.path.basename(p)
            variant = name.rsplit("__", 1)[-1].rsplit(".", 1)[0] if "__" in name else ""
            rows.append([group, name, variant, std, dev, status, method])
            print(f"[{group} {i}/{len(files)}] {name}: {status} {dev}%", flush=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group", "file", "variant", "standard", "deviation_pct", "status", "method"])
        w.writerows(rows)

    g = [r for r in rows if r[0] == "genuine"]
    m = [r for r in rows if r[0] == "manipulated"]
    gc = collections.Counter(r[5] for r in g)
    print("\n=== GENUINE images:", len(g))
    for s in ("Pass", "Warning", "Fail", "Needs Manual Review", "Not Applicable", "Error"):
        if gc[s]: print(f"  {s:20s} {gc[s]}")
    print(f"  FRR (Fail)               {pct(gc['Fail'], len(g))}")
    print(f"  FRR strict (Warn+Fail)   {pct(gc['Fail'] + gc['Warning'], len(g))}")

    print("\n=== MANIPULATED images:", len(m))
    byv = collections.defaultdict(collections.Counter)
    for r in m: byv[r[2]][r[5]] += 1
    print(f"  {'variant':12s} {'n':>3s} {'Pass':>5s} {'Warn':>5s} {'Fail':>5s} {'Manual':>6s} {'Other':>5s}")
    for v in sorted(byv):
        c = byv[v]; n = sum(c.values())
        other = n - c['Pass'] - c['Warning'] - c['Fail'] - c['Needs Manual Review']
        print(f"  {v:12s} {n:3d} {c['Pass']:5d} {c['Warning']:5d} {c['Fail']:5d} {c['Needs Manual Review']:6d} {other:5d}")
    changed = [r for r in m if r[2] != "resize_50"]
    cc = collections.Counter(r[5] for r in changed)
    print(f"\n  Shape-changed images: {len(changed)}")
    print(f"  FAR (Pass)               {pct(cc['Pass'], len(changed))}")
    print(f"  Caught as Fail           {pct(cc['Fail'], len(changed))}")
    print(f"  Warning                  {pct(cc['Warning'], len(changed))}")
    print(f"  Needs manual review      {pct(cc['Needs Manual Review'], len(changed))}")
    # large changes only (10 percent and more)
    big = [r for r in changed if r[2] in ("stretch_w10", "stretch_w20", "squash_h10", "squash_h20", "crop_w15", "crop_h15")]
    bc = collections.Counter(r[5] for r in big)
    print(f"  FAR on changes of 10%+ ({len(big)} images)   {pct(bc['Pass'], len(big))}")
    print(f"\nFull results written to {out}")

main()
