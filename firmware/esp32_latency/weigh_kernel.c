/*
 * weigh_kernel.c — xem mô tả trong weigh_kernel.h
 *
 * Mức tối ưu của trình biên dịch cho RIÊNG file này:
 *   - Trên ESP32 (Arduino): đặt KERNEL_OPT_LEVEL trong config.h (0, 1, 2, 3, hoặc 's')
 *   - Trên gem5/PC: không dùng config.h, truyền cờ -O0/-O2/-O3 khi biên dịch như bình thường
 */
#if defined(ARDUINO)
#include "config.h"
#  if KERNEL_OPT_LEVEL == 0
#    pragma GCC optimize ("O0")
#  elif KERNEL_OPT_LEVEL == 1
#    pragma GCC optimize ("O1")
#  elif KERNEL_OPT_LEVEL == 2
#    pragma GCC optimize ("O2")
#  elif KERNEL_OPT_LEVEL == 3
#    pragma GCC optimize ("O3", "unroll-loops")
#  endif
#endif

#include "weigh_kernel.h"

/* FIR 16 tap thông thấp, hệ số Q8 (tổng = 256), đối xứng */
static const int32_t fir_coef[WK_FIR_TAPS] = {
    2, 4, 8, 13, 19, 24, 28, 30, 30, 28, 24, 19, 13, 8, 4, 2
};

const char *wk_filter_name(wk_filter_t f)
{
    switch (f) {
    case WK_EMA1:  return "EMA1";
    case WK_IIR16: return "IIR16";
    case WK_FIR16: return "FIR16";
    case WK_MED5:  return "MED5";
    default:       return "?";
    }
}

void wk_init(wk_state_t *s, int32_t offset, int32_t scale_q16)
{
    int i;
    for (i = 0; i < WK_FIR_TAPS; i++)   s->fir_buf[i] = 0;
    for (i = 0; i < WK_IIR_STAGES; i++) s->iir[i] = 0;
    for (i = 0; i < WK_MED_N; i++)      s->med_buf[i] = 0;
    s->fir_pos = 0;
    s->med_pos = 0;
    s->ema = 0;
    s->last_out = 0;
    s->seq = 0;
    s->offset = offset;
    s->scale_q16 = scale_q16;
    s->scale_f = (float)scale_q16 / 65536.0f;
    s->overload_g = 100000; /* 100 kg */
}

/* Các bộ lọc làm việc trên độ lệch so với điểm 0 (x = raw - offset), tránh tràn int32 */

static int32_t filt_ema1(wk_state_t *s, int32_t x)
{
    s->ema += (x - s->ema) >> 3;          /* alpha = 1/8 */
    return s->ema;
}

static int32_t filt_iir16(wk_state_t *s, int32_t x)
{
    /* 16 tầng nối tiếp: tầng k cần kết quả MỚI của tầng k-1 → chuỗi phụ thuộc dài 16 */
    int32_t in = x;
    int k;
    for (k = 0; k < WK_IIR_STAGES; k++) {
        s->iir[k] += (in - s->iir[k]) >> 2;
        in = s->iir[k];
    }
    return in;
}

static int32_t filt_fir16(wk_state_t *s, int32_t x)
{
    /* 16 phép nhân độc lập với nhau → nhiều ILP */
    int32_t acc = 0;
    uint32_t p, k;
    s->fir_buf[s->fir_pos] = x;
    s->fir_pos = (s->fir_pos + 1) & (WK_FIR_TAPS - 1);
    p = s->fir_pos;
    for (k = 0; k < WK_FIR_TAPS; k++) {
        acc += fir_coef[k] * s->fir_buf[(p + k) & (WK_FIR_TAPS - 1)];
    }
    return acc >> 8;
}

static int32_t filt_med5(wk_state_t *s, int32_t x)
{
    /* Trung vị 5 mẫu bằng insertion sort → nhiều rẽ nhánh phụ thuộc dữ liệu */
    int32_t a[WK_MED_N];
    int i, j;
    s->med_buf[s->med_pos] = x;
    s->med_pos = (s->med_pos + 1) % WK_MED_N;
    for (i = 0; i < WK_MED_N; i++) a[i] = s->med_buf[i];
    for (i = 1; i < WK_MED_N; i++) {
        int32_t v = a[i];
        j = i - 1;
        while (j >= 0 && a[j] > v) { a[j + 1] = a[j]; j--; }
        a[j + 1] = v;
    }
    return a[WK_MED_N / 2];
}

int32_t wk_filter(wk_state_t *s, int32_t raw, wk_filter_t f)
{
    int32_t x = raw - s->offset;
    int32_t y;
    switch (f) {
    case WK_EMA1:  y = filt_ema1(s, x);  break;
    case WK_IIR16: y = filt_iir16(s, x); break;
    case WK_FIR16: y = filt_fir16(s, x); break;
    case WK_MED5:  y = filt_med5(s, x);  break;
    default:       y = x;                break;
    }
    s->last_out = y + s->offset;
    return s->last_out;
}

int32_t wk_calibrate_fixed(const wk_state_t *s, int32_t filtered)
{
    int64_t d = (int64_t)(filtered - s->offset) * s->scale_q16;
    return (int32_t)(d >> 16);
}

int32_t wk_calibrate_float(const wk_state_t *s, int32_t filtered)
{
    float g = (float)(filtered - s->offset) * s->scale_f;
    return (int32_t)(g + (g >= 0 ? 0.5f : -0.5f));
}

uint32_t wk_crc32(const void *data, size_t len)
{
    /* CRC-32 (IEEE 802.3), từng bit — vòng lặp có phụ thuộc nối tiếp */
    const uint8_t *p = (const uint8_t *)data;
    uint32_t crc = 0xFFFFFFFFu;
    size_t i;
    int b;
    for (i = 0; i < len; i++) {
        crc ^= p[i];
        for (b = 0; b < 8; b++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

void wk_pack(wk_state_t *s, wk_record_t *r, uint32_t ts, int32_t raw,
             int32_t weight_g, uint16_t vehicle_id)
{
    uint16_t flags = 0;
    if (weight_g > s->overload_g) flags |= 2u;
    {
        int32_t d = raw - s->last_out;
        if (d < 0) d = -d;
        if (d < 400) flags |= 1u;       /* coi là ổn định nếu nhiễu nhỏ */
    }
    r->seq = s->seq++;
    r->timestamp = ts;
    r->raw = raw;
    r->weight_g = weight_g;
    r->vehicle_id = vehicle_id;
    r->flags = flags;
    r->crc = wk_crc32(r, offsetof(wk_record_t, crc));
}

int32_t wk_process(wk_state_t *s, wk_filter_t f, int32_t raw, uint32_t ts,
                   uint16_t vehicle_id, wk_record_t *out)
{
    int32_t filtered = wk_filter(s, raw, f);
    int32_t w = wk_calibrate_fixed(s, filtered);
    wk_pack(s, out, ts, raw, w, vehicle_id);
    return w;
}

/* ---------------- Bộ sinh mẫu giả lập ---------------- */

static uint32_t lcg_next(uint32_t *st)
{
    *st = *st * 1664525u + 1013904223u;
    return *st;
}

void wk_gen_init(wk_gen_t *g, uint32_t seed)
{
    g->lcg = seed ? seed : 1u;
    g->t = 0;
    g->load_counts = 0;
}

int32_t wk_gen_sample(wk_gen_t *g)
{
    /* Chu kỳ 400 mẫu: 100 mẫu trống, 50 mẫu xe lên (tăng dần), 200 mẫu đứng yên, 50 mẫu xe xuống */
    const int32_t tare = 120000;     /* điểm 0 của load cell (counts) */
    const int32_t full = 1600000;    /* ~80 kg với scale 0.05 g/count */
    uint32_t ph = g->t % 400u;
    int32_t noise;
    if (ph < 100u)       g->load_counts = 0;
    else if (ph < 150u)  g->load_counts = (int32_t)((ph - 100u) * (uint32_t)(full / 50));
    else if (ph < 350u)  g->load_counts = full;
    else                 g->load_counts = (int32_t)((400u - ph) * (uint32_t)(full / 50));
    g->t++;
    noise = (int32_t)(lcg_next(&g->lcg) >> 23) - 256;   /* nhiễu đều ±256 counts */
    return tare + g->load_counts + noise;
}
