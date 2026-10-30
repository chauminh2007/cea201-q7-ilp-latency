/*
 * esp32_latency.ino — CEA201 Q7: đo độ trễ từ "sensor trigger" đến "data storage"
 * trên trạm cân thông minh mô phỏng bằng ESP32.
 *
 * Phần cứng: CHỈ CẦN ESP32 + 1 dây jumper nối GPIO25 → GPIO26 (xem config.h).
 *
 * Chuỗi được đo (mỗi đoạn tính bằng chu kỳ CPU, 240 MHz → 1 µs = 240 cycle):
 *   T_irq     : từ lúc kích cạnh lên ở chân trigger đến lúc vào ISR
 *   T_sensor  : (a) thời gian 1 lần analogRead của ADC nội
 *               (b) thời gian chờ mẫu mới của HX711 giả lập (DRDY 80 SPS / 10 SPS)
 *   T_compute : lọc + hiệu chuẩn + đóng gói + CRC32 (weigh_kernel.c — dùng chung với gem5)
 *   T_storage : RAM / LittleFS (flash nội) / gửi qua Serial
 *   E2E       : chạy cả chuỗi cho từng mẫu, ghi lại từng đoạn
 *
 * Cách dùng:
 *   1. Arduino IDE: Board "ESP32 Dev Module". Partition scheme mặc định là được (có vùng cho LittleFS).
 *   2. Nạp code, cắm dây jumper.
 *   3. Chạy:  python analyze.py --port COM5      (Windows)  hoặc  --port /dev/ttyUSB0 (Linux)
 *      Hoặc mở Serial Monitor 921600 baud, gõ  r  rồi Enter.
 *   4. Sau khi chạy xong: nhấn nút BOOT để xem demo 1 lần cân (in độ trễ từng đoạn theo µs).
 *
 * Định dạng dữ liệu xuất (CSV qua Serial; dòng bắt đầu bằng '#' là thông tin, không phải dữ liệu):
 *   OVH,i,cycles                         chi phí của chính phép đo (đọc bộ đếm 2 lần)
 *   IRQ,i,cycles
 *   ADC,i,cycles
 *   DRDY,sps,i,cycles
 *   CMP,filter,i,c_filter,c_rest         c_rest = hiệu chuẩn + đóng gói + CRC
 *   CAL,mode,i,cycles                    hiệu chuẩn fixed-point vs float
 *   STO,variant,i,cycles
 *   E2E,sps,store,i,irq,wait,adc,compute,store,total
 */

#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include "esp_timer.h"
#include "esp_random.h"
#include "soc/soc.h"
#include "soc/gpio_reg.h"
#include "config.h"
#include "weigh_kernel.h"

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
#include "esp_cpu.h"
#define CYC() ((uint32_t)esp_cpu_get_cycle_count())
#else
#define CYC() ((uint32_t)ESP.getCycleCount())
#endif

#if PIN_TRIG_OUT >= 32
#error "PIN_TRIG_OUT phải < 32 (dùng thanh ghi GPIO_OUT_W1TS_REG)"
#endif

#define TRIG_HIGH() REG_WRITE(GPIO_OUT_W1TS_REG, (1UL << PIN_TRIG_OUT))
#define TRIG_LOW()  REG_WRITE(GPIO_OUT_W1TC_REG, (1UL << PIN_TRIG_OUT))

/* ---------- Biến dùng chung với ISR / timer ---------- */
static volatile uint32_t g_isr_cyc = 0;
static volatile uint32_t g_isr_count = 0;
static volatile uint32_t g_drdy_count = 0;
static volatile uint32_t g_btn_cyc = 0;
static volatile bool     g_btn_flag = false;

static esp_timer_handle_t g_drdy_timer = nullptr;

/* Kết quả tạm, in ra sau mỗi thí nghiệm để việc in không ảnh hưởng phép đo */
#define BUF_MAX 2000
static uint32_t buf_a[BUF_MAX];
static uint32_t buf_b[BUF_MAX];

#define E2E_MAX 300
static uint32_t e2e[E2E_MAX][6];

static volatile int32_t g_sink;   /* chống trình biên dịch loại bỏ code */
static bool g_abort = false;

/* ---------- ISR ---------- */
void IRAM_ATTR isr_trig() {
  g_isr_cyc = CYC();
  g_isr_count++;
}

void IRAM_ATTR isr_button() {
  g_btn_cyc = CYC();
  g_btn_flag = true;
}

/* Callback của esp_timer chạy trong task esp_timer trên CORE 0, còn loop() chạy trên CORE 1.
 * Bộ đếm cycle của 2 core KHÔNG đồng bộ, nên ở đây chỉ tăng bộ đếm; thời điểm luôn được
 * đọc trên core 1 ngay khi phát hiện bộ đếm thay đổi. */
static void drdy_cb(void *arg) {
  (void)arg;
  g_drdy_count++;
}

/* ---------- Tiện ích ---------- */
static void drdy_start(uint32_t period_us) {
  if (g_drdy_timer) {
    esp_timer_stop(g_drdy_timer);
    esp_timer_start_periodic(g_drdy_timer, period_us);
    return;
  }
  esp_timer_create_args_t a = {};
  a.callback = &drdy_cb;
  a.arg = nullptr;
  a.dispatch_method = ESP_TIMER_TASK;
  a.name = "hx711_drdy";
  esp_timer_create(&a, &g_drdy_timer);
  esp_timer_start_periodic(g_drdy_timer, period_us);
}

static void drdy_stop() {
  if (g_drdy_timer) esp_timer_stop(g_drdy_timer);
}

/* Kích trigger và chờ ISR. Trả về false nếu không thấy ngắt (thiếu dây jumper). */
static bool fire_trigger(uint32_t *t0_out, uint32_t *tisr_out) {
  uint32_t c0 = g_isr_count;
  uint32_t t0 = CYC();
  TRIG_HIGH();
  uint32_t start = micros();
  while (g_isr_count == c0) {
    if (micros() - start > 5000) { TRIG_LOW(); return false; }
  }
  *t0_out = t0;
  *tisr_out = g_isr_cyc;
  return true;
}

static void rest_short() {
  TRIG_LOW();
  delayMicroseconds(100 + (esp_random() % 100));
}

static void hdr(const char *name) {
  Serial.printf("# --- %s ---\n", name);
  Serial.flush();
  delay(20);
}

/* ================= Các thí nghiệm ================= */

static void exp_overhead() {
  hdr("OVH: chi phi doc bo dem cycle");
  for (int i = 0; i < N_OVERHEAD; i++) {
    uint32_t t0 = CYC();
    uint32_t t1 = CYC();
    buf_a[i] = t1 - t0;
  }
  for (int i = 0; i < N_OVERHEAD; i++) Serial.printf("OVH,%d,%u\n", i, buf_a[i]);
}

static void exp_irq() {
  hdr("IRQ: do tre ngat GPIO (loopback)");
  for (int i = 0; i < N_IRQ; i++) {
    uint32_t t0, ti;
    if (!fire_trigger(&t0, &ti)) {
      Serial.printf("#ERR khong thay ngat tren GPIO%d. Kiem tra day jumper GPIO%d -> GPIO%d\n",
                    PIN_TRIG_IN, PIN_TRIG_OUT, PIN_TRIG_IN);
      g_abort = true;
      return;
    }
    buf_a[i] = ti - t0;
    rest_short();
  }
  for (int i = 0; i < N_IRQ; i++) Serial.printf("IRQ,%d,%u\n", i, buf_a[i]);
}

static void exp_adc() {
  hdr("ADC: thoi gian 1 lan analogRead");
  for (int i = 0; i < 10; i++) g_sink = analogRead(PIN_ADC);   /* khởi động ADC */
  for (int i = 0; i < N_ADC; i++) {
    uint32_t t0 = CYC();
    int v = analogRead(PIN_ADC);
    uint32_t t1 = CYC();
    g_sink = v;
    buf_a[i] = t1 - t0;
  }
  for (int i = 0; i < N_ADC; i++) Serial.printf("ADC,%d,%u\n", i, buf_a[i]);
}

static void exp_drdy_one(uint32_t period_us, int sps, int n) {
  drdy_start(period_us);
  delay(period_us / 1000 + 5);
  for (int i = 0; i < n; i++) {
    delayMicroseconds(esp_random() % period_us);   /* xe đến vào thời điểm ngẫu nhiên */
    uint32_t t = CYC();
    uint32_t c0 = g_drdy_count;
    while (g_drdy_count == c0) { }
    buf_a[i] = CYC() - t;
  }
  drdy_stop();
  for (int i = 0; i < n; i++) Serial.printf("DRDY,%d,%d,%u\n", sps, i, buf_a[i]);
}

static void exp_drdy() {
  hdr("DRDY: cho mau moi cua HX711 gia lap");
  exp_drdy_one(HX711_PERIOD_80_US, 80, N_DRDY_80);
  exp_drdy_one(HX711_PERIOD_10_US, 10, N_DRDY_10);
}

static void exp_compute() {
  hdr("CMP: loc + hieu chuan + dong goi + CRC32");
  for (int f = 0; f < WK_NUM_FILTERS; f++) {
    wk_state_t s;
    wk_gen_t g;
    wk_record_t r;
    wk_init(&s, 120000, 3277);
    wk_gen_init(&g, 12345);
    for (int i = 0; i < N_COMPUTE; i++) {
      int32_t raw = wk_gen_sample(&g);
      uint32_t t0 = CYC();
      int32_t y = wk_filter(&s, raw, (wk_filter_t)f);
      uint32_t t1 = CYC();
      int32_t w = wk_calibrate_fixed(&s, y);
      wk_pack(&s, &r, i, raw, w, 7);
      uint32_t t2 = CYC();
      g_sink = r.crc;
      buf_a[i] = t1 - t0;
      buf_b[i] = t2 - t1;
    }
    const char *nm = wk_filter_name((wk_filter_t)f);
    for (int i = 0; i < N_COMPUTE; i++) Serial.printf("CMP,%s,%d,%u,%u\n", nm, i, buf_a[i], buf_b[i]);
  }

  hdr("CAL: hieu chuan fixed-point vs float");
  {
    wk_state_t s;
    wk_gen_t g;
    wk_init(&s, 120000, 3277);
    wk_gen_init(&g, 777);
    const int n = 1000;
    for (int i = 0; i < n; i++) {
      int32_t raw = wk_gen_sample(&g);
      uint32_t t0 = CYC();
      int32_t a = wk_calibrate_fixed(&s, raw);
      uint32_t t1 = CYC();
      int32_t b = wk_calibrate_float(&s, raw);
      uint32_t t2 = CYC();
      g_sink = a + b;
      buf_a[i] = t1 - t0;
      buf_b[i] = t2 - t1;
    }
    for (int i = 0; i < n; i++) Serial.printf("CAL,fixed,%d,%u\n", i, buf_a[i]);
    for (int i = 0; i < n; i++) Serial.printf("CAL,float,%d,%u\n", i, buf_b[i]);
  }
}

static void make_record(wk_record_t *r, int i) {
  static wk_state_t s;
  static bool init = false;
  if (!init) { wk_init(&s, 120000, 3277); init = true; }
  wk_pack(&s, r, (uint32_t)i, 120000 + i, 50000 + i, 7);
}

static void exp_storage() {
  hdr("STO: luu 1 ban ghi 24 byte");
  wk_record_t rec;

  /* 1) RAM: ghi vào bộ đệm vòng — mốc nhanh nhất */
  {
    static wk_record_t ring[256];
    for (int i = 0; i < N_STORE_RAM; i++) {
      make_record(&rec, i);
      uint32_t t0 = CYC();
      memcpy(&ring[i & 255], &rec, sizeof(rec));
      uint32_t t1 = CYC();
      buf_a[i] = t1 - t0;
    }
    g_sink = ring[(N_STORE_RAM - 1) & 255].crc;
    for (int i = 0; i < N_STORE_RAM; i++) Serial.printf("STO,RAM,%d,%u\n", i, buf_a[i]);
  }

  /* 2) LittleFS: write + flush mỗi bản ghi (ghi log an toàn khi mất điện) */
  {
    LittleFS.remove("/log_flush.bin");
    File f = LittleFS.open("/log_flush.bin", FILE_WRITE);
    if (!f) { Serial.println("#ERR khong mo duoc file LittleFS"); return; }
    for (int i = 0; i < N_STORE_LFS; i++) {
      make_record(&rec, i);
      uint32_t t0 = CYC();
      f.write((const uint8_t *)&rec, sizeof(rec));
      f.flush();
      uint32_t t1 = CYC();
      buf_a[i] = t1 - t0;
    }
    f.close();
    for (int i = 0; i < N_STORE_LFS; i++) Serial.printf("STO,LFS_FLUSH,%d,%u\n", i, buf_a[i]);
  }

  /* 3) LittleFS: chỉ write, để thư viện tự gom bộ đệm (nhanh hơn nhưng có thể mất dữ liệu khi mất điện) */
  {
    LittleFS.remove("/log_buf.bin");
    File f = LittleFS.open("/log_buf.bin", FILE_WRITE);
    for (int i = 0; i < N_STORE_LFS; i++) {
      make_record(&rec, i);
      uint32_t t0 = CYC();
      f.write((const uint8_t *)&rec, sizeof(rec));
      uint32_t t1 = CYC();
      buf_a[i] = t1 - t0;
    }
    f.close();
    for (int i = 0; i < N_STORE_LFS; i++) Serial.printf("STO,LFS_NOFLUSH,%d,%u\n", i, buf_a[i]);
  }

  /* 4) LittleFS: open-append-close mỗi lần (kiểu viết phổ biến trong code Arduino) */
  {
    LittleFS.remove("/log_oc.bin");
    for (int i = 0; i < N_STORE_LFS; i++) {
      make_record(&rec, i);
      uint32_t t0 = CYC();
      File f = LittleFS.open("/log_oc.bin", FILE_APPEND);
      f.write((const uint8_t *)&rec, sizeof(rec));
      f.close();
      uint32_t t1 = CYC();
      buf_a[i] = t1 - t0;
    }
    for (int i = 0; i < N_STORE_LFS; i++) Serial.printf("STO,LFS_OPENCLOSE,%d,%u\n", i, buf_a[i]);
  }

  /* 5) Serial: gửi bản ghi dạng text lên PC và chờ truyền xong (mô phỏng gửi về máy chủ trạm) */
  {
    for (int i = 0; i < N_STORE_SERIAL; i++) {
      make_record(&rec, i);
      uint32_t t0 = CYC();
      Serial.printf("#R,%u,%d,%08x\n", rec.seq, rec.weight_g, rec.crc);
      Serial.flush();
      uint32_t t1 = CYC();
      buf_a[i] = t1 - t0;
    }
    for (int i = 0; i < N_STORE_SERIAL; i++) Serial.printf("STO,SERIAL,%d,%u\n", i, buf_a[i]);
  }
}

/* Một lần cân hoàn chỉnh. store_mode: 0 = RAM, 1 = LittleFS (write + flush) */
static bool e2e_trial(wk_state_t *s, wk_gen_t *g, File *f, int store_mode, int idx, uint32_t out[6]) {
  static wk_record_t ring[64];
  wk_record_t rec;
  uint32_t t0, t_isr;

  if (!fire_trigger(&t0, &t_isr)) return false;          /* 1. xe cắt tia → ngắt */
  uint32_t d0 = g_drdy_count;
  while (g_drdy_count == d0) { }                          /* 2. chờ HX711 có mẫu mới */
  uint32_t ta = CYC();
  int adc = analogRead(PIN_ADC);                          /* 3. đọc ADC */
  uint32_t tb = CYC();
  int32_t raw = wk_gen_sample(g) + (adc & 3);             /*    giá trị cân lấy từ bộ sinh giả lập */
  uint32_t tc = CYC();
  wk_process(s, (wk_filter_t)E2E_FILTER, raw, (uint32_t)idx, 7, &rec);   /* 4. xử lý */
  uint32_t td = CYC();
  if (store_mode == 0) {                                  /* 5. lưu */
    memcpy(&ring[idx & 63], &rec, sizeof(rec));
  } else {
    f->write((const uint8_t *)&rec, sizeof(rec));
    f->flush();
  }
  uint32_t te = CYC();
  TRIG_LOW();

  out[0] = t_isr - t0;   /* irq     */
  out[1] = ta - t_isr;   /* wait    */
  out[2] = tb - ta;      /* adc     */
  out[3] = td - tc;      /* compute */
  out[4] = te - td;      /* store   */
  out[5] = te - t0;      /* total   */
  g_sink = ring[idx & 63].crc;
  return true;
}

static void exp_e2e() {
  hdr("E2E: trigger -> luu tru, tung doan");
  const int sps_list[2] = {80, 10};
  const uint32_t per_list[2] = {HX711_PERIOD_80_US, HX711_PERIOD_10_US};
  const int n_list[2] = {N_E2E_80, N_E2E_10};
  const char *store_name[2] = {"RAM", "LFS"};

  for (int si = 0; si < 2; si++) {
    for (int st = 0; st < 2; st++) {
      wk_state_t s;
      wk_gen_t g;
      wk_init(&s, 120000, 3277);
      wk_gen_init(&g, 2026);
      File f;
      if (st == 1) {
        LittleFS.remove("/e2e.bin");
        f = LittleFS.open("/e2e.bin", FILE_WRITE);
      }
      int n = n_list[si];
      if (n > E2E_MAX) n = E2E_MAX;
      drdy_start(per_list[si]);
      delay(per_list[si] / 1000 + 5);
      for (int i = 0; i < n; i++) {
        delayMicroseconds(esp_random() % per_list[si]);   /* xe đến ngẫu nhiên */
        if (!e2e_trial(&s, &g, &f, st, i, e2e[i])) {
          Serial.println("#ERR khong thay ngat trong E2E");
          g_abort = true;
          drdy_stop();
          return;
        }
        delayMicroseconds(200);
      }
      drdy_stop();
      if (st == 1) f.close();
      for (int i = 0; i < n; i++) {
        Serial.printf("E2E,%d,%s,%d,%u,%u,%u,%u,%u,%u\n", sps_list[si], store_name[st], i,
                      e2e[i][0], e2e[i][1], e2e[i][2], e2e[i][3], e2e[i][4], e2e[i][5]);
      }
    }
  }
}

static void run_all() {
  g_abort = false;
  Serial.printf("#META,chip,%s,cpu_mhz,%u,opt,%d,e2e_filter,%s,baud,%d\n",
                ESP.getChipModel(), getCpuFrequencyMhz(), KERNEL_OPT_LEVEL,
                wk_filter_name((wk_filter_t)E2E_FILTER), SERIAL_BAUD);
  Serial.println("#BEGIN");
  drdy_stop();
  exp_overhead();
  exp_irq();
  if (g_abort) { Serial.println("#END"); return; }
  exp_adc();
  exp_drdy();
  exp_compute();
  exp_storage();
  exp_e2e();
  Serial.println("#END");
  drdy_start(HX711_PERIOD_80_US);   /* HX711 giả lập chạy nền cho chế độ demo */
  Serial.println("# Xong. Nhan nut BOOT de chay demo 1 lan can. Go r de chay lai.");
}

/* Demo cho lúc thuyết trình: nhấn BOOT = "xe lên bàn cân" */
static void demo_once() {
  static wk_state_t s;
  static wk_gen_t g;
  static bool init = false;
  if (!init) { wk_init(&s, 120000, 3277); wk_gen_init(&g, 99); init = true; }
  for (int i = 0; i < 160; i++) g_sink = wk_gen_sample(&g);   /* đưa bộ sinh tới đoạn "xe đứng yên" */

  uint32_t t_btn = g_btn_cyc;   /* thời điểm nhấn nút (ISR chạy trên core 1, cùng core với loop) */
  uint32_t d0 = g_drdy_count;
  while (g_drdy_count == d0) { }
  uint32_t ta = CYC();
  int adc = analogRead(PIN_ADC);
  uint32_t tb = CYC();
  wk_record_t rec;
  int32_t w = wk_process(&s, (wk_filter_t)E2E_FILTER, wk_gen_sample(&g) + (adc & 3), 0, 7, &rec);
  uint32_t tc = CYC();
  File f = LittleFS.open("/demo.bin", FILE_APPEND);
  f.write((const uint8_t *)&rec, sizeof(rec));
  f.flush();
  uint32_t td = CYC();
  f.close();

  float mhz = (float)getCpuFrequencyMhz();
  Serial.printf("# DEMO  can = %.2f kg\n", w / 1000.0f);
  Serial.printf("#   cho mau HX711 : %9.1f us\n", (ta - t_btn) / mhz);
  Serial.printf("#   doc ADC       : %9.1f us\n", (tb - ta) / mhz);
  Serial.printf("#   xu ly (CPU)   : %9.1f us   <- phan ILP co the tang toc\n", (tc - tb) / mhz);
  Serial.printf("#   ghi flash     : %9.1f us\n", (td - tc) / mhz);
  Serial.printf("#   TONG          : %9.1f us\n", (td - t_btn) / mhz);
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(300);

  pinMode(PIN_TRIG_OUT, OUTPUT);
  TRIG_LOW();
  pinMode(PIN_TRIG_IN, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(PIN_TRIG_IN), isr_trig, RISING);

  pinMode(PIN_BOOT_BTN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_BOOT_BTN), isr_button, FALLING);

  analogReadResolution(12);

  if (!LittleFS.begin(true)) {
    Serial.println("#ERR LittleFS.begin that bai (kiem tra Partition Scheme co vung SPIFFS/LittleFS)");
  }

  /* Kiểm tra dây jumper */
  TRIG_HIGH();
  delayMicroseconds(50);
  bool ok_hi = digitalRead(PIN_TRIG_IN) == HIGH;
  TRIG_LOW();
  delayMicroseconds(50);
  bool ok_lo = digitalRead(PIN_TRIG_IN) == LOW;
  if (!(ok_hi && ok_lo)) {
    Serial.printf("#WARN chua thay day jumper GPIO%d -> GPIO%d\n", PIN_TRIG_OUT, PIN_TRIG_IN);
  }
  drdy_start(HX711_PERIOD_80_US);   /* HX711 giả lập chạy nền cho chế độ demo */
  g_btn_flag = false;
}

void loop() {
  static uint32_t last_ready = 0;
  if (millis() - last_ready > 1000) {
    Serial.println("#READY (go r de bat dau)");
    last_ready = millis();
  }
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'r' || c == 'R') {
      run_all();
      g_btn_flag = false;
    }
  }
  if (g_btn_flag) {
    demo_once();
    while (digitalRead(PIN_BOOT_BTN) == LOW) delay(5);
    delay(80);                  /* chống dội phím */
    g_btn_flag = false;
  }
}
