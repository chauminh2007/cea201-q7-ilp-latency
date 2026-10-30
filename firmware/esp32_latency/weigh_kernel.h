/*
 * weigh_kernel.h — Phần "T_compute" của trạm cân thông minh (Smart Weighing Station)
 *
 * C thuần (chỉ dùng <stdint.h>, <stddef.h>), KHÔNG phụ thuộc Arduino,
 * để dùng lại nguyên vẹn trong gem5 (RISC-V/ARM, chế độ SE).
 *
 * Chuỗi xử lý 1 mẫu:  raw ADC (24-bit) -> lọc -> hiệu chuẩn -> đóng gói bản ghi -> CRC32
 *
 * Các bộ lọc được chọn để so sánh mức ILP:
 *   WK_EMA1  : 1 tầng EMA  — rất ít lệnh, giống firmware cân thực tế
 *   WK_IIR16 : 16 tầng EMA nối tiếp — 16 phép tính PHỤ THUỘC nhau (chuỗi dài, ILP thấp)
 *   WK_FIR16 : FIR 16 tap — 16 phép nhân-cộng ĐỘC LẬP (ILP cao)
 *   WK_MED5  : trung vị 5 mẫu — nhiều rẽ nhánh (branch-heavy)
 * IIR16 và FIR16 có số phép toán tương đương nhau, chỉ khác cấu trúc phụ thuộc dữ liệu.
 */
#ifndef WEIGH_KERNEL_H
#define WEIGH_KERNEL_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    WK_EMA1 = 0,
    WK_IIR16 = 1,
    WK_FIR16 = 2,
    WK_MED5 = 3,
    WK_NUM_FILTERS = 4
} wk_filter_t;

#define WK_FIR_TAPS   16
#define WK_IIR_STAGES 16
#define WK_MED_N      5

/* Bản ghi cân lưu xuống bộ nhớ: 24 byte */
typedef struct {
    uint32_t seq;        /* số thứ tự giao dịch */
    uint32_t timestamp;  /* thời điểm (đơn vị tùy nền tảng) */
    int32_t  raw;        /* giá trị ADC thô */
    int32_t  weight_g;   /* khối lượng đã hiệu chuẩn (gram) */
    uint16_t vehicle_id; /* mã xe */
    uint16_t flags;      /* bit0: ổn định, bit1: quá tải */
    uint32_t crc;        /* CRC32 của 20 byte đầu */
} wk_record_t;

typedef struct {
    int32_t  fir_buf[WK_FIR_TAPS]; /* bộ đệm vòng cho FIR */
    uint32_t fir_pos;
    int32_t  iir[WK_IIR_STAGES];   /* trạng thái các tầng IIR */
    int32_t  ema;                  /* trạng thái EMA1 */
    int32_t  med_buf[WK_MED_N];
    uint32_t med_pos;
    int32_t  last_out;
    uint32_t seq;
    /* hiệu chuẩn */
    int32_t  offset;               /* giá trị ADC khi không tải */
    int32_t  scale_q16;            /* gram trên 1 count, dạng Q16.16 */
    float    scale_f;              /* cùng hệ số, dạng float */
    int32_t  overload_g;
} wk_state_t;

/* Bộ sinh mẫu giả lập load cell (nhiễu + xe lên/xuống bàn cân) */
typedef struct {
    uint32_t lcg;
    uint32_t t;
    int32_t  load_counts;
} wk_gen_t;

void     wk_init(wk_state_t *s, int32_t offset, int32_t scale_q16);
int32_t  wk_filter(wk_state_t *s, int32_t raw, wk_filter_t f);
int32_t  wk_calibrate_fixed(const wk_state_t *s, int32_t filtered);
int32_t  wk_calibrate_float(const wk_state_t *s, int32_t filtered);
void     wk_pack(wk_state_t *s, wk_record_t *r, uint32_t ts, int32_t raw,
                 int32_t weight_g, uint16_t vehicle_id);
uint32_t wk_crc32(const void *data, size_t len);

/* Toàn bộ chuỗi: lọc + hiệu chuẩn (fixed-point) + đóng gói + CRC. Trả về weight_g. */
int32_t  wk_process(wk_state_t *s, wk_filter_t f, int32_t raw, uint32_t ts,
                    uint16_t vehicle_id, wk_record_t *out);

void     wk_gen_init(wk_gen_t *g, uint32_t seed);
int32_t  wk_gen_sample(wk_gen_t *g);   /* trả về giá trị ADC 24-bit có dấu */

const char *wk_filter_name(wk_filter_t f);

#ifdef __cplusplus
}
#endif
#endif /* WEIGH_KERNEL_H */
