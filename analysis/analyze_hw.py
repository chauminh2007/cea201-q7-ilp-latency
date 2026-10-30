#!/usr/bin/env python3
"""
analyze_hw.py — Phân tích log đo phần cứng (ESP32 / ESP32-S3 / Uno) cho CEA201 Q7
và ghép với kết quả gem5 (results.csv) theo định luật Amdahl.

Dùng (trong thư mục gem5_q7):
    python3 analyze_hw.py esp32_log.txt
    python3 analyze_hw.py esp32_log.txt --csv results.csv

Kết quả (thư mục hw_out/):
    hw_summary.csv            thống kê từng phép đo (µs): n, median, mean, p99, max
    hw_e2e.csv                độ trễ từng đoạn của mỗi lần cân, tỷ lệ compute, % giảm dự kiến nhờ ILP
    fig_hw_e2e.png            thành phần độ trễ end-to-end (đo thật)
    fig_hw_storage.png        thời gian lưu 1 bản ghi theo cách lưu (thấy rõ đỉnh trễ của flash)
    fig_hw_irq.png            phân bố độ trễ ngắt
    fig_hw_vs_gem5.png        T_compute đo thật so với gem5
"""
import argparse
import csv
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FILTERS = ["EMA1", "IIR16", "FIR16", "MED5"]


def pct(xs, p):
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return s[k]


def parse_log(path):
    meta = {"cpu_mhz": 240.0, "chip": "?", "e2e_filter": "FIR16"}
    d = {"OVH": [], "IRQ": [], "ADC": [], "DRDY": {}, "CMP": {}, "CAL": {}, "STO": {}, "E2E": {}}
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if "-> " in line[:20]:                  # bỏ timestamp của Serial Monitor "12:00:01.123 -> "
                line = line.split("-> ", 1)[1].strip()
            if line.startswith("#META"):
                p = line.split(",")
                for i in range(1, len(p) - 1, 2):
                    meta[p[i]] = p[i + 1]
                meta["cpu_mhz"] = float(meta.get("cpu_mhz", 240))
                continue
            if not line or line.startswith("#"):
                continue
            p = line.split(",")
            try:
                t = p[0]
                if t in ("OVH", "IRQ", "ADC") and len(p) == 3:
                    d[t].append(int(p[2]))
                elif t == "DRDY" and len(p) == 4:
                    d["DRDY"].setdefault(int(p[1]), []).append(int(p[3]))
                elif t == "CMP" and len(p) == 5:
                    d["CMP"].setdefault(p[1], []).append((int(p[3]), int(p[4])))
                elif t == "CAL" and len(p) == 4:
                    d["CAL"].setdefault(p[1], []).append(int(p[3]))
                elif t == "STO" and len(p) == 4:
                    d["STO"].setdefault(p[1], []).append(int(p[3]))
                elif t == "E2E" and len(p) == 10:
                    key = (int(p[1]), p[2])
                    d["E2E"].setdefault(key, []).append([int(x) for x in p[4:10]])
            except ValueError:
                pass      # dòng bị cắt / lỗi khi copy
    return meta, d


def load_gem5(path, opt="O2"):
    g = {}
    if not path or not os.path.exists(path):
        return g
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["opt"] == opt and r["cycles_per_sample"] and r.get("rob", "0") in ("0", ""):
                g[(r["config"], r["filter"])] = float(r["cycles_per_sample"])
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--csv", default=os.path.join(HERE, "results.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "hw_out"))
    a = ap.parse_args()
    meta, d = parse_log(a.log)
    mhz = meta["cpu_mhz"]
    us = lambda c: c / mhz
    os.makedirs(a.out, exist_ok=True)

    n_total = len(d["OVH"]) + len(d["IRQ"]) + len(d["ADC"]) + sum(len(v) for v in d["E2E"].values())
    if n_total == 0:
        sys.exit("Không đọc được dòng dữ liệu nào trong %s" % a.log)
    ovh = st.median(d["OVH"]) if d["OVH"] else 0
    print("Chip: %s @ %g MHz | chi phí đo (OVH) = %s cycle" % (meta.get("chip"), mhz, ovh))

    # ---------- Bảng tổng hợp ----------
    summ = []

    def add(name, cycles, sub_ovh=True):
        if not cycles:
            return
        xs = [us(max(0, c - (ovh if sub_ovh else 0))) for c in cycles]
        summ.append({"metric": name, "n": len(xs), "median_us": round(st.median(xs), 3),
                     "mean_us": round(st.mean(xs), 3), "p99_us": round(pct(xs, 99), 3),
                     "max_us": round(max(xs), 3),
                     "stdev_us": round(st.pstdev(xs), 3)})

    add("T_irq (ngắt GPIO)", d["IRQ"])
    add("T_adc (analogRead)", d["ADC"])
    for sps, v in sorted(d["DRDY"].items(), reverse=True):
        add("Chờ HX711 %d SPS (giả lập)" % sps, v, False)
    for f in FILTERS:
        if f in d["CMP"]:
            add("Compute %s: chỉ lọc" % f, [x[0] for x in d["CMP"][f]])
            add("Compute %s: toàn bộ chuỗi" % f, [x[0] + x[1] for x in d["CMP"][f]])
    for k, v in d["CAL"].items():
        add("Hiệu chuẩn %s" % k, v)
    for k, v in d["STO"].items():
        add("Lưu trữ %s" % k, v, k == "RAM")

    with open(os.path.join(a.out, "hw_summary.csv"), "w", newline="", encoding="utf-8") as fo:
        w = csv.DictWriter(fo, fieldnames=list(summ[0].keys()))
        w.writeheader()
        w.writerows(summ)
    print("\n%-34s %6s %11s %11s %11s %11s" % ("Phép đo", "n", "median µs", "mean µs", "p99 µs", "max µs"))
    for r in summ:
        print("%-34s %6d %11.2f %11.2f %11.2f %11.2f" % (r["metric"], r["n"], r["median_us"],
                                                         r["mean_us"], r["p99_us"], r["max_us"]))

    # ---------- So sánh với gem5 ----------
    g = load_gem5(a.csv)
    ef = meta.get("e2e_filter", "FIR16")
    if g:
        print("\n=== T_compute: phần cứng thật so với gem5 (-O2, cycle/mẫu, toàn bộ chuỗi) ===")
        print("%-8s %14s %14s %14s" % ("filter", "%s" % meta.get("chip"), "gem5 Minor-w1", "gem5 O3-w8"))
        for f in FILTERS:
            if f in d["CMP"]:
                hw = st.median([x[0] + x[1] - ovh for x in d["CMP"][f]])
                print("%-8s %14.0f %14s %14s" % (f, hw, g.get(("Minor-w1", f), "-"), g.get(("O3-w8", f), "-")))

    # ---------- End-to-end + Amdahl ----------
    stages = ["irq", "wait", "adc", "compute", "store"]
    e2e_rows = []
    print("\n=== End-to-end đo thật (trung vị, µs) và % giảm nếu thay lõi bằng cấu hình ILP của gem5 ===")
    ilp_cfgs = [c for c in ["Minor-w2", "O3-w2", "O3-w4", "O3-w8"] if (c, ef) in g and ("Minor-w1", ef) in g]
    hdr = "%-14s" % "kịch bản" + "".join("%10s" % s for s in stages) + "%11s%11s" % ("total", "compute%")
    hdr += "".join("%11s" % ("↓" + c) for c in ilp_cfgs)
    print(hdr)
    for (sps, store), v in sorted(d["E2E"].items(), key=lambda kv: (-kv[0][0], kv[0][1])):
        med = [st.median([row[i] for row in v]) for i in range(6)]
        mu = [us(x) for x in med]
        share = med[3] / med[5] if med[5] else 0
        row = {"sps": sps, "store": store, "n": len(v)}
        row.update({"%s_us" % s: round(mu[i], 3) for i, s in enumerate(stages)})
        row["total_us"] = round(mu[5], 3)
        row["compute_share_pct"] = round(100 * share, 4)
        line = "%-14s" % ("%d SPS + %s" % (sps, store)) + "".join("%10.1f" % x for x in mu[:5])
        line += "%11.1f%10.3f%%" % (mu[5], 100 * share)
        for c in ilp_cfgs:
            speed = g[("Minor-w1", ef)] / g[(c, ef)]
            red = share * (1 - 1 / speed)
            row["reduction_pct_%s" % c] = round(100 * red, 4)
            line += "%10.3f%%" % (100 * red)
        e2e_rows.append(row)
        print(line)
    if e2e_rows:
        with open(os.path.join(a.out, "hw_e2e.csv"), "w", newline="", encoding="utf-8") as fo:
            w = csv.DictWriter(fo, fieldnames=list(e2e_rows[0].keys()))
            w.writeheader()
            w.writerows(e2e_rows)
    if not g:
        print("(Không thấy results.csv của gem5 → chưa tính được cột % giảm nhờ ILP)")

    # ---------- Biểu đồ ----------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("Chưa có matplotlib → bỏ qua biểu đồ")
        return

    chip = "%s @ %g MHz" % (meta.get("chip"), mhz)

    if d["E2E"]:
        keys = sorted(d["E2E"].keys(), key=lambda k: (-k[0], k[1]))
        fig, ax = plt.subplots(figsize=(9, 4.6))
        bw = 0.16
        for i, s in enumerate(stages):
            ys = [us(st.median([r[i] for r in d["E2E"][k]])) for k in keys]
            ax.bar([x + (i - 2) * bw for x in range(len(keys))], [max(y, 1e-3) for y in ys], bw,
                   label=s, color="C%d" % i)
        ax.set_yscale("log")
        ax.set_xticks(range(len(keys)))
        ax.set_xticklabels(["%d SPS + %s" % k for k in keys])
        ax.set_ylabel("Trung vị (µs, thang log)")
        ax.set_title("Thành phần độ trễ trigger → lưu trữ, đo trên %s" % chip)
        ax.legend(fontsize=8, ncol=5)
        ax.grid(axis="y", alpha=0.3, which="both")
        fig.tight_layout()
        fig.savefig(os.path.join(a.out, "fig_hw_e2e.png"), dpi=150)
        plt.close(fig)

    if d["STO"]:
        names = list(d["STO"].keys())
        fig, ax = plt.subplots(figsize=(9, 4.6))
        ax.boxplot([[max(us(c), 1e-3) for c in d["STO"][k]] for k in names], showfliers=True)
        ax.set_xticks(range(1, len(names) + 1))
        ax.set_xticklabels(names)
        ax.set_yscale("log")
        ax.set_ylabel("µs / bản ghi 24 byte (thang log)")
        ax.set_title("Thời gian lưu 1 bản ghi, đo trên %s" % chip)
        ax.grid(axis="y", alpha=0.3, which="both")
        fig.tight_layout()
        fig.savefig(os.path.join(a.out, "fig_hw_storage.png"), dpi=150)
        plt.close(fig)

    if d["IRQ"]:
        xs = [us(max(0, c - ovh)) for c in d["IRQ"]]
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(xs, bins=50)
        ax.set_xlabel("Độ trễ ngắt (µs)")
        ax.set_ylabel("Số lần")
        ax.set_title("Phân bố T_irq (n=%d, trung vị %.2f µs, p99 %.2f µs)" %
                     (len(xs), st.median(xs), pct(xs, 99)))
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(a.out, "fig_hw_irq.png"), dpi=150)
        plt.close(fig)

    if g and d["CMP"]:
        fs = [f for f in FILTERS if f in d["CMP"]]
        fig, ax = plt.subplots(figsize=(8, 4.4))
        bw = 0.27
        series = [(meta.get("chip"), [st.median([x[0] + x[1] - ovh for x in d["CMP"][f]]) for f in fs]),
                  ("gem5 Minor-w1", [g.get(("Minor-w1", f), 0) for f in fs]),
                  ("gem5 O3-w8", [g.get(("O3-w8", f), 0) for f in fs])]
        for i, (lab, ys) in enumerate(series):
            ax.bar([x + (i - 1) * bw for x in range(len(fs))], ys, bw, label=lab)
        ax.set_xticks(range(len(fs)))
        ax.set_xticklabels(fs)
        ax.set_ylabel("Chu kỳ / mẫu (toàn bộ chuỗi)")
        ax.set_title("T_compute: phần cứng thật so với gem5")
        ax.legend()
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(a.out, "fig_hw_vs_gem5.png"), dpi=150)
        plt.close(fig)

    print("\nĐã lưu bảng và biểu đồ vào %s/" % a.out)


if __name__ == "__main__":
    main()
