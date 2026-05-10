#!/usr/bin/env python3
"""Analyze KV transfer times from prefill/decode server logs, split by case."""
import re
import sys
import json
from collections import defaultdict

SEND_RE = re.compile(r"⏱️KV send: ts=([\d.]+), tensor_id:cmpl-.*?_([a-f0-9]{32})-\d+#model\.layers\.(\d+)")
RECV_RE = re.compile(r"⏱️KV recv: ts=([\d.]+), tensor_id:cmpl-.*?_([a-f0-9]{32})-\d+#model\.layers\.(\d+)")
GAP_THRESHOLD = 10.0  # seconds

def parse(path, pattern):
    data = defaultdict(dict)
    with open(path) as f:
        for line in f:
            m = pattern.search(line)
            if m:
                data[m.group(2)][int(m.group(3))] = float(m.group(1))
    return data

def split_cases(sends):
    """Split requests into cases by detecting time gaps."""
    ordered = sorted(sends.items(), key=lambda x: x[1].get(0, 0))
    cases, current = [], [ordered[0]]
    for i in range(1, len(ordered)):
        prev_t = ordered[i-1][1].get(0, 0)
        curr_t = ordered[i][1].get(0, 0)
        if curr_t - prev_t > GAP_THRESHOLD:
            cases.append({rid for rid, _ in current})
            current = []
        current.append(ordered[i])
    cases.append({rid for rid, _ in current})
    return cases

def stats(vals):
    vals = sorted(vals)
    n = len(vals)
    return {"count": n, "mean_ms": sum(vals)/n, "median_ms": vals[n//2],
            "min_ms": vals[0], "max_ms": vals[-1],
            "p90_ms": vals[int(n*0.9)], "p99_ms": vals[int(n*0.99)]}

def analyze_case(sends, recvs, rids, label):
    common = sorted(rids & set(sends) & set(recvs))
    print(f"\n{'='*60}")
    print(f"  {label}  ({len(common)} requests)")
    print(f"{'='*60}")

    req_totals = []
    for rid in common:
        s, r = sends[rid], recvs[rid]
        layers = sorted(set(s) & set(r))
        if not layers:
            continue
        req_totals.append({"req_id": rid, "total_ms": (r[max(layers)] - s[min(layers)]) * 1000})

    totals_ms = [r["total_ms"] for r in req_totals]
    st = stats(totals_ms)
    print(f"\n--- (1) Total KV Transfer Time (send layer0 → recv layer31) ---")
    for k, v in st.items():
        print(f"  {k:>10}: {v:.3f}" if isinstance(v, float) else f"  {k:>10}: {v}")

    layer_deltas = defaultdict(list)
    for rid in common:
        s, r = sends[rid], recvs[rid]
        for layer in sorted(set(s) & set(r)):
            layer_deltas[layer].append((r[layer] - s[layer]) * 1000)

    print(f"\n--- (2) Per-Layer KV Transfer Time ---")
    print(f"{'layer':>6} {'count':>6} {'mean':>10} {'median':>10} {'min':>10} {'max':>10} {'p99':>10}")
    layer_out = {}
    for layer in sorted(layer_deltas):
        ls = stats(layer_deltas[layer])
        print(f"{layer:>6} {ls['count']:>6} {ls['mean_ms']:>10.3f} {ls['median_ms']:>10.3f} {ls['min_ms']:>10.3f} {ls['max_ms']:>10.3f} {ls['p99_ms']:>10.3f}")
        layer_out[str(layer)] = ls

    return {"total_transfer": st, "per_layer": layer_out,
            "per_request": [{"req_id": r["req_id"], "total_ms": r["total_ms"]} for r in req_totals]}

def main(prefill_log, decode_log):
    sends = parse(prefill_log, SEND_RE)
    recvs = parse(decode_log, RECV_RE)
    cases = split_cases(sends)
    print(f"Detected {len(cases)} cases ({', '.join(str(len(c)) + ' reqs' for c in cases)})")

    case_labels = ["case1 (input=512, output=128)", "case2 (input=128, output=512)"]
    out = {}
    for i, rids in enumerate(cases):
        label = case_labels[i] if i < len(case_labels) else f"case{i+1}"
        out[f"case{i+1}"] = analyze_case(sends, recvs, rids, label)

    out_path = prefill_log.replace("prefill_server_", "kv_transfer_analysis_").replace(".log", ".json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved: {out_path}")

if __name__ == "__main__":
    if len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        print("Usage: python analyze_kv_transfer.py <prefill.log> <decode.log>")
