# CEA201 – Q7: Can ILP reduce sensor-to-storage latency?

**Course:** CEA201 – Computer Organization and Architecture (FPT University), Smart Weighing Station track
**Research question (Q7, CLO9):** *Can instruction-level parallelism significantly reduce the latency between sensor trigger and data storage?*
**Group 6 – AI21B:** Nguyen Thi Minh Chau, Nguyen Viet Hai, Dang Hoang Gia Bao

## Summary

The trigger-to-storage latency is modelled as

```
T_total = T_irq + T_wait + T_adc + T_compute + T_store
```

ILP only affects `T_compute`. We measure it in **gem5** (in-order vs. out-of-order RISC-V cores, 240 MHz) and on a real **ESP32-S3**, using the same C kernel (`weigh_kernel.c`).

| Finding | Value |
|---|---|
| Compute speedup from ILP (filter only) | 1.4–6.5× (IIR16 saturates at 2.2×) |
| Compute speedup, full chain | ≤ 2.64× (bit-serial CRC32 dominates) |
| Compute share of end-to-end latency (ESP32-S3, measured) | 0.01–0.08 % |
| Max end-to-end reduction from ILP | 0.049 % (threshold for "significant": 20 %) |
| Break-even total I/O time at 240 MHz | ≈ 20 µs |

**Conclusion:** ILP speeds up computation but does **not** significantly reduce the end-to-end latency, which is dominated by the HX711 sample wait (6–50 ms) and flash storage (31 ms median, up to 138 ms).

## Repository structure

```
cea201-q7-ilp-latency/
├── firmware/esp32_latency/   Arduino sketch for ESP32 / ESP32-S3 (+ shared weigh_kernel.c/.h)
├── gem5/                     gem5 benchmark, CPU config and experiment runner
├── analysis/                 Amdahl / sensitivity analysis and hardware-log analysis
├── data/
│   ├── esp32/                raw ESP32-S3 log + summary CSVs
│   └── gem5/                 results.csv from run_matrix.py
├── figures/                  all figures used in the report
└── report/main.tex           LaTeX report (Overleaf)
```

## Environment

| Component | Version |
|---|---|
| gem5 | v26.0.0.0 (stable branch), `build/RISCV/gem5.opt` |
| Host OS | Windows 11 + WSL2 Ubuntu 22.04 (12 cores, 24 GB RAM) |
| Cross compiler | `riscv64-linux-gnu-gcc` (Ubuntu package `gcc-riscv64-linux-gnu`) |
| Arduino | Arduino IDE 2.x, esp32 board package 3.3.12 |
| Board | ESP32-S3 (QFN56, rev v0.2, 8 MB PSRAM), 240 MHz, *USB CDC On Boot: Enabled* |
| Python | 3.10, matplotlib 3.5 |

## Reproduce

### 1. gem5 experiments

```bash
# build gem5 (once)
git clone --branch stable https://github.com/gem5/gem5.git ~/gem5
cd ~/gem5 && scons build/RISCV/gem5.opt -j10
cd util/m5 && scons riscv.CROSS_COMPILE=riscv64-linux-gnu- build/riscv/out/m5

# run the 25-configuration matrix
cd gem5
make host                 # sanity check on the PC
make                      # bench_O0 / bench_O2 / bench_O3 (RISC-V, static)
python3 run_matrix.py -j 10
cp results.csv ../data/gem5/
```

### 2. Hardware measurement (ESP32-S3)

1. Connect a jumper wire **GPIO4 → GPIO5** (ESP32 classic: GPIO25 → GPIO26).
2. Open `firmware/esp32_latency/esp32_latency.ino`, board *ESP32S3 Dev Module*, upload.
3. Close the Arduino IDE and capture the log with PowerShell (sends `r`, stops at `#END`):

```powershell
$port = New-Object System.IO.Ports.SerialPort "COM3",921600
$port.ReadTimeout = 3000; $port.NewLine = "`n"; $port.Open()
Start-Sleep -Seconds 2; $port.DiscardInBuffer(); $port.Write("r")
$w = New-Object System.IO.StreamWriter "esp32_log.txt"
try { while ($true) { try { $l = $port.ReadLine().TrimEnd("`r") } catch [System.TimeoutException] { continue }
      $w.WriteLine($l); if ($l -like "#END*") { break } } } finally { $w.Close(); $port.Close() }
```

### 3. Analysis

```bash
cd analysis
python3 analyze_hw.py ../data/esp32/esp32_log.txt --csv ../data/gem5/results.csv --out ../data/esp32
python3 sensitivity.py --csv ../data/gem5/results.csv
```

## Notes and limitations

- The HX711 load-cell ADC was **emulated** from its datasheet data rates (80 / 10 SPS); interrupt, ADC, CPU and flash timings are measured on real hardware.
- gem5 runs in syscall-emulation mode; absolute cycle counts differ from the ESP32-S3 (≈1.85×, different ISA), so only **relative** speedups are used.
- Results come from one board and one measurement run.

## AI usage

AI tools were used to help write and debug the measurement code and scripts. All code was compiled and tested, and every number in the report comes from the logs in `data/`. See the *AI Usage Statement* in the report for details.
