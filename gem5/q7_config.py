"""
q7_config.py — Cấu hình gem5 (SE mode, RISC-V) cho thí nghiệm CEA201 Q7.

Mô hình hệ thống gần với bộ điều khiển trạm cân nhúng:
  - Xung nhịp mặc định 240 MHz (bằng ESP32) → số cycle/thời gian so sánh trực tiếp được
  - L1I 16 KB, L1D 16 KB, không có L2 (giống vi điều khiển / SoC nhúng nhỏ)

Ví dụ:
  build/RISCV/gem5.opt --outdir=out/o3_w4 q7_config.py --cpu o3 --width 4 --rob 64 \
      --cmd ./bench_O2 --args 2000

  --cpu timing : TimingSimpleCPU — không pipeline, mỗi lệnh xong mới tới lệnh sau
  --cpu minor  : MinorCPU — pipeline in-order. --width 1 = đơn issue (giống ESP32-C3, Cortex-M4),
                 --width 2 = dual issue (giống Cortex-M7)
  --cpu o3     : O3CPU — out-of-order superscalar, --width 1/2/4/8, --rob kích thước reorder buffer
"""
import argparse

import m5
from m5.objects import *

p = argparse.ArgumentParser()
p.add_argument("--cpu", choices=["timing", "minor", "o3"], default="o3")
p.add_argument("--width", type=int, default=2)
p.add_argument("--rob", type=int, default=0, help="0 = tự chọn theo width")
p.add_argument("--clock", default="240MHz")
p.add_argument("--l1", default="16kB")
p.add_argument("--cmd", required=True)
p.add_argument("--args", nargs="*", default=[])
a = p.parse_args()


def pick(*names):
    """Chọn lớp CPU theo tên, tương thích cả gem5 mới (RiscvO3CPU) và cũ (DerivO3CPU)."""
    for n in names:
        if n in globals():
            return globals()[n]
    raise SystemExit("Không tìm thấy lớp CPU nào trong: %s" % (names,))


class L1(Cache):
    assoc = 4
    tag_latency = 1
    data_latency = 1
    response_latency = 1
    mshrs = 4
    tgts_per_mshr = 8


system = System()
system.clk_domain = SrcClockDomain(clock=a.clock, voltage_domain=VoltageDomain())
system.mem_mode = "timing"
system.mem_ranges = [AddrRange("512MB")]

w = max(1, a.width)
if a.cpu == "timing":
    system.cpu = pick("RiscvTimingSimpleCPU", "TimingSimpleCPU")()
elif a.cpu == "minor":
    cpu = pick("RiscvMinorCPU", "MinorCPU")()
    cpu.decodeInputWidth = w
    cpu.executeInputWidth = w
    cpu.executeIssueLimit = w
    cpu.executeCommitLimit = w
    system.cpu = cpu
else:
    cpu = pick("RiscvO3CPU", "DerivO3CPU", "O3CPU")()
    for prm in ("fetchWidth", "decodeWidth", "renameWidth", "dispatchWidth",
                "issueWidth", "wbWidth", "commitWidth", "squashWidth"):
        setattr(cpu, prm, w)
    # Bộ đệm truyền tín hiệu giữa các tầng pipeline (mặc định 5). Với width 1, gem5 cần lớn hơn,
    # nếu không sẽ báo "TimeBuffer out-of-bounds access". Không ảnh hưởng tới hiệu năng mô phỏng.
    cpu.backComSize = 10
    cpu.forwardComSize = 10
    rob = a.rob if a.rob > 0 else max(16, 32 * w)
    cpu.numROBEntries = rob
    iq = max(8, rob // 2)
    try:
        cpu.numIQEntries = iq                 # gem5 cũ
    except AttributeError:
        cpu.instQueues[0].numEntries = iq     # gem5 mới (IQUnit)
    cpu.LQEntries = max(8, rob // 4)
    cpu.SQEntries = max(8, rob // 4)
    system.cpu = cpu

system.membus = SystemXBar()

system.cpu.icache = L1(size=a.l1)
system.cpu.dcache = L1(size=a.l1)
system.cpu.icache_port = system.cpu.icache.cpu_side
system.cpu.dcache_port = system.cpu.dcache.cpu_side
system.cpu.icache.mem_side = system.membus.cpu_side_ports
system.cpu.dcache.mem_side = system.membus.cpu_side_ports

system.cpu.createInterruptController()

system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = DDR3_1600_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports
system.system_port = system.membus.cpu_side_ports

system.workload = SEWorkload.init_compatible(a.cmd)
process = Process()
process.cmd = [a.cmd] + a.args
system.cpu.workload = process
system.cpu.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()
ev = m5.simulate()
print("Exit @ tick %d: %s" % (m5.curTick(), ev.getCause()))
