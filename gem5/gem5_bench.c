/*
 * gem5_bench.c — Đo T_compute của trạm cân trên gem5 (RISC-V, chế độ SE)
 *
 * Dùng lại NGUYÊN VẸN weigh_kernel.c của ESP32 → so sánh được trực tiếp.
 *
 * Với mỗi bộ lọc (EMA1, IIR16, FIR16, MED5):
 *   - sinh trước N mẫu vào mảng (không tính vào vùng đo, giống trên ESP32)
 *   - chạy 64 mẫu khởi động (làm ấm cache, bộ đoán nhánh)
 *   - vùng đo 1: chỉ bộ lọc;  vùng đo 2: toàn bộ chuỗi (lọc + hiệu chuẩn + đóng gói + CRC32)
 * → stats.txt có 8 khối thống kê: EMA1-lọc, EMA1-toàn bộ, IIR16-lọc, IIR16-toàn bộ, ...
 *
 * Cách dùng:  gem5_bench [N]      (mặc định N = 2000)
 * Biên dịch với -DNO_M5 để chạy thử trên PC (không có m5ops).
 */
#include <stdio.h>
#include <stdlib.h>
#include "weigh_kernel.h"

#ifdef NO_M5
#define m5_reset_stats(a, b) ((void)0)
#define m5_dump_stats(a, b)  ((void)0)
#else
#include <gem5/m5ops.h>
#endif

#define N_MAX    20000
#define N_WARMUP 64

static int32_t samples[N_MAX + N_WARMUP];
static wk_record_t ring[64];
volatile int32_t sink;

int main(int argc, char **argv)
{
    int n = (argc > 1) ? atoi(argv[1]) : 2000;
    int f, i;
    uint32_t checksum = 0;
    if (n < 1 || n > N_MAX) n = 2000;

    for (f = 0; f < WK_NUM_FILTERS; f++) {
        wk_state_t s;
        wk_gen_t g;
        wk_init(&s, 120000, 3277);
        wk_gen_init(&g, 12345);
        for (i = 0; i < n + N_WARMUP; i++) samples[i] = wk_gen_sample(&g);

        /* Vùng đo 1: CHỈ bộ lọc (không có hiệu chuẩn/đóng gói/CRC) */
        {
            int32_t acc = 0;
            for (i = 0; i < N_WARMUP; i++) acc += wk_filter(&s, samples[i], (wk_filter_t)f);
            m5_reset_stats(0, 0);
            for (i = N_WARMUP; i < n + N_WARMUP; i++) acc += wk_filter(&s, samples[i], (wk_filter_t)f);
            m5_dump_stats(0, 0);
            sink = acc;
        }

        /* Vùng đo 2: toàn bộ chuỗi xử lý (lọc + hiệu chuẩn + đóng gói + CRC32) */
        wk_init(&s, 120000, 3277);
        for (i = 0; i < N_WARMUP; i++)
            wk_process(&s, (wk_filter_t)f, samples[i], (uint32_t)i, 7, &ring[i & 63]);

        m5_reset_stats(0, 0);
        for (i = N_WARMUP; i < n + N_WARMUP; i++)
            wk_process(&s, (wk_filter_t)f, samples[i], (uint32_t)i, 7, &ring[i & 63]);
        m5_dump_stats(0, 0);

        for (i = 0; i < 64; i++) checksum ^= ring[i].crc;
        printf("filter=%s n=%d last_weight_g=%d\n", wk_filter_name((wk_filter_t)f), n,
               ring[(n + N_WARMUP - 1) & 63].weight_g);
    }
    sink = (int32_t)checksum;
    printf("checksum=%08x\n", checksum);
    return 0;
}
