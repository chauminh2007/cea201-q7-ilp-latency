#!/usr/bin/env python3
"""
run_matrix.py — Chạy toàn bộ ma trận cấu hình gem5 cho CEA201 Q7, gom kết quả, vẽ biểu đồ.

Dùng (trong thư mục gem5_q7, sau khi đã `make`):
    python3 run_matrix.py                    # chạy tất cả (song song), rồi phân tích
    python3 run_matrix.py --only-parse       # chỉ đọc lại kết quả đã có trong out/
    python3 run_matrix.py --gem5 ~/gem5 -j 10 -n 2000

Kết quả:
    out/<tên cấu hình>/stats.txt   thống kê gốc của gem5
    results.csv                     bảng tổng hợp (cycle/mẫu, IPC, lệnh/mẫu, đoán sai nhánh)
    fig_*.png                       biểu đồ cho report
"""
import argparse
import csv
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
FILTERS = ["EMA1", "IIR16", "FIR16", "MED5"]
FILTERS_ONLY = [f + "_only" for f in FILTERS]   # chỉ đo bộ lọc, không có CRC/đóng gói

# (cpu, width, rob) — rob = 0 nghĩa là để q7_config tự chọn (32 × width)
CORE_CONFIGS = [
    ("timing", 1, 0),
    ("minor", 1, 0),
    ("minor", 2, 0),
    ("o3", 1, 0),
    ("o3", 2, 0),
    ("o3", 4, 0),
    ("o3", 8, 0),
]
OPTS = ["O0", "O2", "O3"]
ROB_SWEEP = [16, 32, 128, 256]   # quét ROB với O3 width 4, -O2
BASELINE = ("minor", 1, 0)        # lõi in-order đơn issue ≈ vi điều khiển kiểu ESP32-C3 / Cortex-M4


def label(cpu, width, rob):
    if cpu == "timing":
        return "Timing"
    if cpu == "minor":
        return "Minor-w%d" % width
    return "O3-w%d" % width + ("-rob%d" % rob if rob else "")


def run_name(cpu, width, rob, opt):
    return "%s_w%d_rob%d_%s" % (cpu, width, rob, opt)


def build_jobs():
    jobs = []
    for opt in OPTS:
        for cpu, w, rob in CORE_CONFIGS:
            jobs.append((cpu, w, rob, opt))
    for rob in ROB_SWEEP:
        j = ("o3", 4, rob, "O2")
        if j not in jobs:
            jobs.append(j)
    return jobs


def run_one(gem5_bin, job, n):
    cpu, w, rob, opt = job
    outdir = os.path.join(HERE, "out", run_name(*job))
    os.makedirs(outdir, exist_ok=True)
    cmd = [gem5_bin, "-r", "-e", "--outdir=" + outdir, os.path.join(HERE, "q7_config.py"),
           "--cpu", cpu, "--width", str(w), "--rob", str(rob),
           "--cmd", os.path.join(HERE, "bench_" + opt), "--args", str(n)]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ok = r.returncode == 0 and os.path.exists(os.path.join(outdir, "stats.txt"))
    print("[%s] %s" % ("OK " if ok else "LỖI", run_name(*job)), flush=True)
    if not ok:
        print(r.stdout[-2000:])
        for f in ("simout", "simerr", "simout.txt", "simerr.txt"):
            p = os.path.join(outdir, f)
            if os.path.exists(p):
                print("---", f, "---")
                print(open(p, errors="replace").read()[-2000:])
    return ok


# ----------------------------- Đọc stats.txt -----------------------------

def parse_sections(path):
    txt = open(path, errors="replace").read()
    parts = txt.split("---------- Begin Simulation Statistics ----------")[1:]
    secs = []
    for p in parts:
        d = {}
        for line in p.splitlines():
            m = re.match(r"^(\S+)\s+([-+0-9.eE]+|nan|inf)\s", line + " ")
            if m:
                try:
                    d[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
        secs.append(d)
    return secs


def first_key(d, names):
    for k in names:
        if k in d:
            return d[k]
    return None


def find_insts(d):
    v = first_key(d, ["system.cpu.commitStats0.numInsts", "system.cpu.committedInsts",
                      "system.cpu.thread_0.numInsts", "system.cpu.exec_context.thread_0.numInsts"])
    if v is not None:
        return v
    for k, val in d.items():
        if k.startswith("system.cpu.") and (k.endswith("numInsts") or k.endswith("committedInsts")) \
                and "Ops" not in k:
            return val
    return None


def summarize(job, n):
    path = os.path.join(HERE, "out", run_name(*job), "stats.txt")
    if not os.path.exists(path):
        return []
    secs = parse_sections(path)
    if len(secs) < 2 * len(FILTERS):
        print("Cảnh báo: %s chỉ có %d khối thống kê" % (path, len(secs)))
        return []
    rows = []
    cpu, w, rob, opt = job
    for si in range(2 * len(FILTERS)):
        d = secs[si]
        fname = FILTERS[si // 2] + ("_only" if si % 2 == 0 else "")
        cyc = d.get("system.cpu.numCycles")
        ins = find_insts(d)
        ipc = d.get("system.cpu.ipc") or (ins / cyc if ins and cyc else None)
        mis = first_key(d, ["system.cpu.branchPred.condIncorrect"])
        prd = first_key(d, ["system.cpu.branchPred.condPredicted"])
        sim_s = d.get("simSeconds")
        rows.append({
            "config": label(cpu, w, rob), "cpu": cpu, "width": w, "rob": rob, "opt": opt,
            "filter": fname, "n": n,
            "cycles_per_sample": round(cyc / n, 2) if cyc else "",
            "us_per_sample": round(sim_s / n * 1e6, 4) if sim_s else "",
            "insts_per_sample": round(ins / n, 2) if ins else "",
            "ipc": round(ipc, 4) if ipc else "",
            "cond_mispred_rate": round(mis / prd, 4) if (mis is not None and prd) else "",
        })
    return rows


# ----------------------------- Biểu đồ -----------------------------

def plots(rows):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("Chưa có matplotlib → bỏ qua biểu đồ. Cài: sudo apt install -y python3-matplotlib")
        return

    def get(cfg, opt, filt):
        for r in rows:
            if r["config"] == cfg and r["opt"] == opt and r["filter"] == filt and r["cycles_per_sample"] != "":
                return float(r["cycles_per_sample"])
        return None

    cfgs = [label(*c) for c in CORE_CONFIGS]
    base = label(*BASELINE)

    # 1) cycle / mẫu theo cấu hình CPU (-O2)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    bw = 0.2
    for i, f in enumerate(FILTERS):
        ys = [get(c, "O2", f) or 0 for c in cfgs]
        ax.bar([x + (i - 1.5) * bw for x in range(len(cfgs))], ys, bw, label=f)
    ax.set_xticks(range(len(cfgs)))
    ax.set_xticklabels(cfgs)
    ax.set_ylabel("Chu kỳ CPU / mẫu")
    ax.set_title("T_compute theo cấu hình CPU (gem5 RISC-V, 240 MHz, -O2)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_cycles_per_sample.png"), dpi=150)
    plt.close(fig)

    # 2) speedup so với lõi in-order đơn issue
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), sharey=True)
    for ax, flist, ttl in ((axes[0], FILTERS_ONLY, "Chỉ bộ lọc"),
                           (axes[1], FILTERS, "Toàn bộ chuỗi (lọc + CRC + đóng gói)")):
        for f in flist:
            b = get(base, "O2", f)
            ys = [(b / get(c, "O2", f)) if (b and get(c, "O2", f)) else None for c in cfgs]
            ax.plot(cfgs, ys, marker="o", label=f.replace("_only", ""))
        ax.axhline(1.0, color="gray", lw=0.8)
        ax.set_title(ttl)
        ax.tick_params(axis="x", rotation=30)
        ax.grid(alpha=0.3)
        ax.legend()
    axes[0].set_ylabel("Speedup so với %s" % base)
    fig.suptitle("Mức tăng tốc phần tính toán nhờ ILP (-O2)")
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_speedup_ilp.png"), dpi=150)
    plt.close(fig)

    # 3) ảnh hưởng mức tối ưu của trình biên dịch
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=False)
    for ax, cfg in zip(axes, [base, "O3-w4"]):
        for i, f in enumerate(FILTERS):
            ys = [get(cfg, o, f) or 0 for o in OPTS]
            ax.bar([x + (i - 1.5) * bw for x in range(len(OPTS))], ys, bw, label=f)
        ax.set_xticks(range(len(OPTS)))
        ax.set_xticklabels(["-" + o for o in OPTS])
        ax.set_title(cfg)
        ax.set_ylabel("Chu kỳ / mẫu")
        ax.grid(axis="y", alpha=0.3)
    axes[0].legend()
    fig.suptitle("ILP do trình biên dịch khai thác: -O0 / -O2 / -O3")
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_compiler_opt.png"), dpi=150)
    plt.close(fig)

    # 4) quét kích thước ROB
    fig, ax = plt.subplots(figsize=(7, 4))
    for f in FILTERS:
        xs, ys = [], []
        for rob in ROB_SWEEP:
            v = None
            for r in rows:
                if r["cpu"] == "o3" and r["width"] == 4 and r["opt"] == "O2" and r["filter"] == f \
                        and r["rob"] == rob and r["cycles_per_sample"] != "":
                    v = float(r["cycles_per_sample"])
            if v:
                xs.append(rob)
                ys.append(v)
        ax.plot(xs, ys, marker="o", label=f)
    ax.set_xscale("log", base=2)
    ax.set_xlabel("Số entry ROB (O3, width 4)")
    ax.set_ylabel("Chu kỳ / mẫu")
    ax.set_title("Ảnh hưởng của cửa sổ lệnh (ROB)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig_rob_sweep.png"), dpi=150)
    plt.close(fig)
    print("Đã lưu: fig_cycles_per_sample.png, fig_speedup_ilp.png, fig_compiler_opt.png, fig_rob_sweep.png")


def print_table(rows, filters, title):
    base = label(*BASELINE)
    print("\n=== %s: chu kỳ / mẫu (-O2) và speedup so với %s ===" % (title, base))
    cfgs = [label(*c) for c in CORE_CONFIGS]
    print("%-10s" % "config" + "".join("%16s" % f for f in filters))
    bmap = {r["filter"]: float(r["cycles_per_sample"]) for r in rows
            if r["config"] == base and r["opt"] == "O2" and r["cycles_per_sample"] != ""}
    for c in cfgs:
        line = "%-10s" % c
        for f in filters:
            v = [float(r["cycles_per_sample"]) for r in rows
                 if r["config"] == c and r["opt"] == "O2" and r["filter"] == f and r["cycles_per_sample"] != ""]
            if v and f in bmap:
                line += "%9.1f (%4.2fx)" % (v[0], bmap[f] / v[0])
            else:
                line += "%16s" % "-"
        print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gem5", default=os.path.expanduser("~/gem5"))
    ap.add_argument("-j", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("-n", type=int, default=2000, help="số mẫu mỗi bộ lọc")
    ap.add_argument("--only-parse", action="store_true")
    a = ap.parse_args()

    gem5_bin = os.path.join(a.gem5, "build", "RISCV", "gem5.opt")
    jobs = build_jobs()

    if not a.only_parse:
        if not os.path.exists(gem5_bin):
            sys.exit("Không thấy %s" % gem5_bin)
        for opt in OPTS:
            if not os.path.exists(os.path.join(HERE, "bench_" + opt)):
                sys.exit("Thiếu bench_%s — chạy `make` trước" % opt)
        print("Chạy %d cấu hình, %d luồng song song..." % (len(jobs), a.j))
        with ThreadPoolExecutor(max_workers=a.j) as ex:
            results = list(ex.map(lambda j: run_one(gem5_bin, j, a.n), jobs))
        print("Xong: %d/%d thành công" % (sum(results), len(results)))

    rows = []
    for j in jobs:
        rows += summarize(j, a.n)
    if not rows:
        sys.exit("Không có kết quả nào để phân tích.")
    out_csv = os.path.join(HERE, "results.csv")
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("Đã ghi %s (%d dòng)" % (out_csv, len(rows)))
    print_table(rows, FILTERS_ONLY, "CHỈ BỘ LỌC")
    print_table(rows, FILTERS, "TOÀN BỘ CHUỖI XỬ LÝ")
    plots(rows)


if __name__ == "__main__":
    main()
