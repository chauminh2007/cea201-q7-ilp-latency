# Biên dịch chương trình đo cho gem5 (RISC-V, link tĩnh) ở 3 mức tối ưu
# Dùng: make            (GEM5 mặc định là ~/gem5)
#       make GEM5=/duong/dan/gem5
GEM5   ?= $(HOME)/gem5
CC      = riscv64-linux-gnu-gcc
KDIR    = ../firmware/esp32_latency
CFLAGS  = -static -Wall -I$(KDIR) -I$(GEM5)/include
M5LIB   = $(GEM5)/util/m5/build/riscv/out/libm5.a
SRC     = gem5_bench.c $(KDIR)/weigh_kernel.c

all: bench_O0 bench_O2 bench_O3

bench_O0: $(SRC)
	$(CC) $(CFLAGS) -O0 $(SRC) $(M5LIB) -o $@
bench_O2: $(SRC)
	$(CC) $(CFLAGS) -O2 $(SRC) $(M5LIB) -o $@
bench_O3: $(SRC)
	$(CC) $(CFLAGS) -O3 -funroll-loops $(SRC) $(M5LIB) -o $@

# Chạy thử trên PC (không qua gem5) để kiểm tra chương trình đúng
host: $(SRC)
	gcc -O2 -Wall -DNO_M5 -I$(KDIR) $(SRC) -o bench_host && ./bench_host 2000

clean:
	rm -f bench_O0 bench_O2 bench_O3 bench_host

.PHONY: all host clean
