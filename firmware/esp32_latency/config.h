/*
 * config.h — Cấu hình thí nghiệm. Sửa ở đây rồi nạp lại.
 * Board mặc định: ESP32 bản gốc (ESP32 Dev Module / DevKit V1).
 */
#ifndef CONFIG_H
#define CONFIG_H

/* ---------- Chân GPIO ---------- */
/* Nối 1 dây jumper từ PIN_TRIG_OUT sang PIN_TRIG_IN (mô phỏng "xe cắt tia cảm biến") */
/* Tự chọn chân theo loại chip (theo board chọn trong Arduino IDE) */
#if defined(CONFIG_IDF_TARGET_ESP32S3)
#  define PIN_TRIG_OUT 4       /* ESP32-S3: nối jumper GPIO4 → GPIO5 */
#  define PIN_TRIG_IN  5
#  define PIN_ADC      1       /* ADC1_CH0 */
#  define PIN_BOOT_BTN 0
#elif defined(CONFIG_IDF_TARGET_ESP32C3)
#  define PIN_TRIG_OUT 4       /* ESP32-C3: nối jumper GPIO4 → GPIO5 */
#  define PIN_TRIG_IN  5
#  define PIN_ADC      2
#  define PIN_BOOT_BTN 9
#else
#  define PIN_TRIG_OUT 25      /* ESP32 bản gốc: nối jumper GPIO25 → GPIO26 */
#  define PIN_TRIG_IN  26
#  define PIN_ADC      34      /* ADC1 — để trống hoặc nối vào 3.3V/GND qua dây */
#  define PIN_BOOT_BTN 0       /* nút BOOT trên board, dùng cho chế độ demo */
#endif

/* ---------- Serial ---------- */
#define SERIAL_BAUD    921600

/* ---------- Số mẫu mỗi thí nghiệm ---------- */
#define N_OVERHEAD     1000
#define N_IRQ          2000
#define N_ADC          2000
#define N_DRDY_80      200
#define N_DRDY_10      60
#define N_COMPUTE      2000
#define N_STORE_RAM    2000
#define N_STORE_LFS    300
#define N_STORE_SERIAL 300
#define N_E2E_80       300
#define N_E2E_10       60

/* ---------- Mô phỏng HX711 ---------- */
/* HX711 có 2 tốc độ lấy mẫu: 10 SPS (100 ms) và 80 SPS (12.5 ms) — theo datasheet */
#define HX711_PERIOD_80_US  12500
#define HX711_PERIOD_10_US  100000

/* ---------- Bộ lọc dùng trong thí nghiệm end-to-end ---------- */
/* 0 = EMA1, 1 = IIR16, 2 = FIR16, 3 = MED5 */
#define E2E_FILTER     2

/* ---------- Mức tối ưu của trình biên dịch cho weigh_kernel.c ---------- */
/* 0 → -O0, 1 → -O1, 2 → -O2, 3 → -O3 + unroll. Giá trị khác → giữ mặc định của Arduino (-Os).
 * Đổi giá trị này, nạp lại và chạy lại để so sánh ILP do trình biên dịch khai thác. */
#define KERNEL_OPT_LEVEL 2

#endif
