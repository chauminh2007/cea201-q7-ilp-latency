#!/usr/bin/env python3
"""
sensitivity.py — Ghép T_compute (gem5) với độ trễ I/O (datasheet) bằng định luật Amdahl,
và phân tích độ nhạy: ILP giảm được bao nhiêu % độ trễ "sensor trigger → data storage"?

Dùng (trong thư mục gem5_q7, sau khi đã có results.csv):
    python3 run_matrix.py --only-parse      # nếu cần tạo lại results.csv
    python3 sensitivity.py                  # mặc định: -O2, xung nhịp 240 MHz và 16 MHz

Kết quả:
    scenarios.csv          độ trễ từng đoạn và % giảm nhờ ILP cho từng kịch bản
    fig_sensitivity.png    % giảm độ trễ end-to-end theo tổng thời gian I/O
    fig_breakdown.png      tỷ lệ từng đoạn trong độ trễ tổng (kịch bản thực tế)

Mô hình:   T_total = T_irq + T_wait + T_adc + T_compute + T_store
           ILP chỉ thay đổi T_compute → % giảm = (Tc_base − Tc_ilp) / T_total_base
"""
import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FILTERS = ["EMA1", "IIR16", "FIR16", "MED5"]
BASELINE = "Minor-w1"                       # lõi in-order đơn issue ≈ vi điều khiển nhúng
ILP_CFGS = ["Minor-w2", "O3-w2", "O3-w4", "O3-w8"]
THRESHOLD = 0.20                            # ngưỡng "giảm đáng kể" do nhóm đặt ra

# ---- Độ trễ I/O (µs), nguồn: datasheet / tính toán; ghi rõ trong report ----
IO = {
    "irq":        2.0,      # vào ISR: AVR tối thiểu 4 cycle (ATmega328P datasheet) + overhead Arduino ~µs
    "adc":        104.0,    # ADC ATmega328P: 13 chu kỳ ADC ở 125 kHz = 104 µs (datasheet)
    "wait80_avg": 6250.0,   # HX711 80 SPS: chờ mẫu mới, phân bố đều 0–12.5 ms → trung bình 6.25 ms
    "wait80_max": 12500.0,
    "wait10_avg": 50000.0,  # HX711 10 SPS: trung bình 50 ms, tối đa 100 ms
    "wait10_max": 100000.0,
    "ram":        0.1,      # chép 24 byte vào RAM
    "uart":       2604.0,   # ~30 ký tự × 10 bit / 115200 baud
    "eeprom":     79200.0,  # ATmega328P EEPROM ~3.3 ms/byte × 24 byte (datasheet)
}

SCENARIOS = [
    # (tên, wait, store)
    ("Lý tưởng: không chờ HX711, lưu RAM", 0.0, "ram"),
    ("80 SPS (TB) + RAM", IO["wait80_avg"], "ram"),
    ("80 SPS (TB) + UART lên PC", IO["wait80_avg"], "uart"),
    ("80 SPS (TB) + EEPROM", IO["wait80_avg"], "eeprom"),
    ("80 SPS (xấu nhất) + EEPROM", IO["wait80_max"], "eeprom"),
    ("10 SPS (TB) + EEPROM", IO["wait10_avg"], "eeprom"),
]


def load(path, opt):
    d = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["opt"] != opt or r["cycles_per_sample"] == "" or r["filter"] not in FILTERS:
                continue
            if r.get("rob", "0") not in ("0", ""):
                continue
            d[(r["config"], r["filter"])] = float(r["cycles_per_sample"])
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(HERE, "results.csv"))
    ap.add_argument("--opt", default="O2")
    ap.add_argument("--mhz", default="240,16", help="các xung nhịp so sánh, ví dụ 240,16")
    a = ap.parse_args()
    if not os.path.exists(a.csv):
        sys.exit("Không thấy %s — chạy run_matrix.py trước" % a.csv)
    cyc = load(a.csv, a.opt)
    clocks = [float(x) for x in a.mhz.split(",")]
    main_mhz = clocks[0]

    def tc(cfg, f, mhz):
        return cyc[(cfg, f)] / mhz      # cycles / MHz = µs

    # ---------- Bảng kịch bản ----------
    rows = []
    print("\n=== Độ trễ end-to-end và mức giảm nhờ ILP (-%s, %g MHz) ===" % (a.opt, main_mhz))
    for name, wait, store in SCENARIOS:
        io = IO["irq"] + wait + IO["adc"] + IO[store]
        for f in FILTERS:
            if (BASELINE, f) not in cyc:
                continue
            base = tc(BASELINE, f, main_mhz)
            total = io + base
            for cfg in ILP_CFGS:
                if (cfg, f) not in cyc:
                    continue
                saved = base - tc(cfg, f, main_mhz)
                rows.append({
                    "scenario": name, "filter": f, "ilp_config": cfg, "mhz": main_mhz,
                    "t_irq_us": IO["irq"], "t_wait_us": wait, "t_adc_us": IO["adc"],
                    "t_store_us": IO[store], "t_compute_base_us": round(base, 3),
                    "t_total_base_us": round(total, 3), "saved_us": round(saved, 3),
                    "reduction_pct": round(100 * saved / total, 4),
                    "compute_share_pct": round(100 * base / total, 4),
                })
    with open(os.path.join(HERE, "scenarios.csv"), "w", newline="") as fo:
        w = csv.DictWriter(fo, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    f_main = "FIR16"
    best = "O3-w8"
    print("%-38s %12s %12s %10s" % ("Kịch bản (FIR16, %s → %s)" % (BASELINE, best),
                                   "T_total(µs)", "Compute %", "Giảm %"))
    for r in rows:
        if r["filter"] == f_main and r["ilp_config"] == best:
            print("%-38s %12.1f %11.3f%% %9.3f%%" % (r["scenario"], r["t_total_base_us"],
                                                     r["compute_share_pct"], r["reduction_pct"]))

    print("\n=== Ngưỡng hòa vốn: tổng I/O phải NHỎ HƠN bao nhiêu thì ILP giảm ≥ %d%% độ trễ ===" %
          int(THRESHOLD * 100))
    print("%-10s %-10s" % ("filter", "config") + "".join("%14s" % ("@%g MHz" % m) for m in clocks))
    for f in FILTERS:
        for cfg in ILP_CFGS:
            if (cfg, f) not in cyc or (BASELINE, f) not in cyc:
                continue
            line = "%-10s %-10s" % (f, cfg)
            for m in clocks:
                base, ilp = tc(BASELINE, f, m), tc(cfg, f, m)
                io_star = (base - ilp) / THRESHOLD - base
                line += "%11.1f µs" % io_star if io_star > 0 else "%14s" % "không đạt"
            print(line)

    # ---------- Biểu đồ ----------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("Chưa có matplotlib → bỏ qua biểu đồ")
        return

    import math
    xs = [10 ** (k / 20.0) for k in range(0, 101)]   # 1 µs → 100 ms
    fig, ax = plt.subplots(figsize=(10, 5.2))
    styles = {clocks[0]: "-", (clocks[1] if len(clocks) > 1 else None): "--"}
    for m in clocks:
        for cfg in ["Minor-w2", "O3-w2", "O3-w8"]:
            if (cfg, f_main) not in cyc:
                continue
            base, ilp = tc(BASELINE, f_main, m), tc(cfg, f_main, m)
            ys = [100 * (base - ilp) / (x + base) for x in xs]
            ax.plot(xs, ys, styles.get(m, ":"), label="%s, %g MHz" % (cfg, m))
    ax.axhline(100 * THRESHOLD, color="red", lw=1, ls=":")
    ax.text(1.2, 100 * THRESHOLD + 1.5, "ngưỡng %d%%" % int(100 * THRESHOLD), color="red")
    marks = [("HX711 80 SPS\n(chờ TB)", IO["wait80_avg"]), ("EEPROM\n24 byte", IO["eeprom"]),
             ("UART\n115200", IO["uart"]), ("ADC AVR", IO["adc"])]
    for txt, x in marks:
        ax.axvline(x, color="gray", lw=0.8, ls="--")
        ax.text(x * 1.08, 62, txt, fontsize=8, color="dimgray", va="top")
    ax.set_xscale("log")
    ax.set_xlim(1, 1e5)
    ax.set_ylim(0, 65)
    ax.set_xlabel("Tổng thời gian I/O của 1 lần cân (µs, thang log)")
    ax.set_ylabel("% giảm độ trễ end-to-end nhờ ILP")
    ax.set_title("Phân tích độ nhạy (FIR16, so với %s, -%s)" % (BASELINE, a.opt))
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8, ncol=2, loc="center right")
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_sensitivity.png"), dpi=150)
    plt.close(fig)

    # tỷ lệ các đoạn, thang log để thấy được đoạn compute
    fig, ax = plt.subplots(figsize=(10, 4.8))
    names, parts = [], {"irq": [], "chờ HX711": [], "ADC": [], "compute": [], "lưu trữ": []}
    for name, wait, store in SCENARIOS:
        names.append(name)
        parts["irq"].append(IO["irq"])
        parts["chờ HX711"].append(wait)
        parts["ADC"].append(IO["adc"])
        parts["compute"].append(tc(BASELINE, f_main, main_mhz))
        parts["lưu trữ"].append(IO[store])
    width = 0.15
    for i, (k, v) in enumerate(parts.items()):
        xs_k = [x + (i - 2) * width for x, t in zip(range(len(names)), v) if t > 0]
        ax.bar(xs_k, [t for t in v if t > 0], width, label=k, color="C%d" % i)
    ax.set_yscale("log")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=15, ha="right", fontsize=8)
    ax.set_ylabel("Thời gian (µs, thang log)")
    ax.set_title("Thành phần độ trễ từng kịch bản (compute = FIR16 trên %s, %g MHz)" % (BASELINE, main_mhz))
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_breakdown.png"), dpi=150)
    plt.close(fig)
    print("\nĐã lưu: scenarios.csv, fig_sensitivity.png, fig_breakdown.png")


if __name__ == "__main__":
    main()
