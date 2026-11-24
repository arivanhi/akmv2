/*
 * ============================================================================
 * MEDISYN (AKMv2) - Alat Ukur Tinggi Badan Ultrasonik BLE (Stadiometer Digital)
 * ============================================================================
 * Mikrokontroler : ESP32-C3 Super Mini
 * Sensor Jarak   : Ultrasonik HC-SR04 / HC-SR04P (3.3V-5V)
 * Layar          : LCD OLED 0.91 inch I2C (128x32 piksel, Driver SSD1306, 0x3C)
 * Input Tombol   : 3x Push Button (Internal INPUT_PULLUP, Aktif LOW ke GND)
 * Konektivitas   : Bluetooth Low Energy (BLE Server -> MEDISYN Raspberry Pi)
 *
 * Kompatibilitas BLE MEDISYN (ble_scale_service.py):
 *   - Advertised Name     : "bleScaleHeight"
 *   - Service UUID        : "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
 *   - Characteristic UUID : "beb5483e-36e1-4688-b7f5-ea07361b26a8"
 *   - Format Data         : String UTF-8 dalam cm (contoh: "168.5"), READ | NOTIFY
 *
 * Library Arduino IDE yang dibutuhkan:
 *   1. Board Package : "esp32" by Espressif Systems (Board: ESP32C3 Dev Module,
 *                      USB CDC On Boot: Enabled)
 *   2. Library       : "Adafruit SSD1306" & "Adafruit GFX Library" (via Library Manager)
 * ============================================================================
 */

#include <Arduino.h>
#include <math.h>
#include <Wire.h>
#include <Preferences.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ============================================================================
// 1. KONFIGURASI PIN ESP32-C3 SUPER MINI
// ============================================================================
// Catatan: Hindari GPIO2, GPIO8 (LED biru onboard), dan GPIO9 (tombol BOOT)
// untuk input eksternal agar proses boot/flashing ESP32-C3 selalu aman.
#define PIN_OLED_SDA     6    // I2C SDA -> OLED SDA
#define PIN_OLED_SCL     7    // I2C SCL -> OLED SCL

#define PIN_TRIG         4    // HC-SR04 TRIG
#define PIN_ECHO         5    // HC-SR04 ECHO (Gunakan pembagi tegangan 1k+2k jika VCC HC-SR04 = 5V!)

#define PIN_BTN_MEASURE  0    // Push Button 1: UKUR / KUNCI (HOLD) & KIRIM BLE / (+) saat Kalibrasi
#define PIN_BTN_CALIB    1    // Push Button 2: KALIBRASI MANUAL (Tekan Singkat) / AUTO-TARE LANTAI (Tahan 1.5 dtk)
#define PIN_BTN_RESET    3    // Push Button 3: RESET KE LIVE / MODE JARAK (Tahan 1.5 dtk) / (-) saat Kalibrasi

#define PIN_LED_STATUS   8    // LED Biru Onboard ESP32-C3 Super Mini (Aktif LOW)

// ============================================================================
// 2. KONFIGURASI LAYAR OLED 0.91" (128x32)
// ============================================================================
#define SCREEN_WIDTH     128
#define SCREEN_HEIGHT    32
#define OLED_RESET       -1
#define OLED_I2C_ADDR    0x3C

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
bool oledReady = false;

// ============================================================================
// 3. KONFIGURASI BLE (SESUAI PROTOKOL MEDISYN)
// ============================================================================
#define BLE_DEVICE_NAME       "bleScaleHeight"
#define BLE_SERVICE_UUID      "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define BLE_HEIGHT_CHAR_UUID  "beb5483e-36e1-4688-b7f5-ea07361b26a8"

BLEServer         *pServer         = nullptr;
BLECharacteristic *pHeightChar     = nullptr;
volatile bool      bleConnected    = false;
bool               prevBleConnected = false;

// ============================================================================
// 4. PARAMETER PENGUKURAN, FILTER, & PENYIMPANAN KALIBRASI
// ============================================================================
#define DEFAULT_POLE_HEIGHT_CM  200.0f   // Tinggi pemasangan sensor dari lantai (default 200.0 cm)
#define MIN_POLE_HEIGHT_CM       50.0f
#define MAX_POLE_HEIGHT_CM      300.0f
#define MIN_VALID_HEIGHT_CM      30.0f   // Batas minimal tinggi valid untuk dikirim ke MEDISYN via BLE
#define AUTO_SEND_MIN_HEIGHT_CM  50.0f   // Batas minimal tinggi untuk auto-send saat stabil
#define ZERO_DEADZONE_CM          1.2f   // Toleransi +-1.2 cm dari lantai dianggap 0.0 cm
#define ROOM_TEMP_C              27.0f   // Estimasi suhu ruang klinik di Indonesia (untuk cepat rambat bunyi)
#define ECHO_TIMEOUT_US         26000UL  // Timeout ~4.5 meter (26 ms)
#define PING_INTERVAL_MS          65UL   // Interval antar-ping HC-SR04 (>= 60 ms sesuai datasheet)
#define FILTER_WINDOW_SIZE         7     // Jumlah sampel rolling median-trimmed mean

#define DEBOUNCE_MS               35UL
#define LONG_PRESS_MS           1500UL
#define REPEAT_INITIAL_MS        450UL
#define REPEAT_STEP_MS           120UL

Preferences prefs;

enum SystemMode {
  MODE_LIVE,        // Pengukuran tinggi badan langsung (real-time)
  MODE_HOLD,        // Hasil tinggi badan dikunci di layar & siap/sedang dikirim ke BLE
  MODE_RAW_DIST,    // Mode diagnostik: menampilkan jarak mentah sensor HC-SR04 (cm)
  MODE_CALIB_POLE   // Mode atur manual tinggi tiang dengan tombol (+) dan (-)
};

SystemMode currentMode = MODE_LIVE;

float poleHeightCm     = DEFAULT_POLE_HEIGHT_CM;
float filteredDistCm   = -1.0f;
float liveHeightCm     = 0.0f;
float lockedHeightCm   = 0.0f;
bool  sensorValid      = false;
bool  isReadingStable  = false;

// Buffer melingkar untuk filter ultrasonik & deteksi kestabilan
float distRing[FILTER_WINDOW_SIZE];
uint8_t distCount      = 0;
uint8_t distIndex      = 0;

float recentHeights[12];
uint8_t recentIdx      = 0;
uint8_t recentCount    = 0;
unsigned long stableSinceMs     = 0;
bool autoSentCurrentStanding    = false;

// Pewaktu non-blocking
unsigned long lastPingMs        = 0;
unsigned long lastOledMs        = 0;
unsigned long lastHoldBleSendMs = 0;
unsigned long lastLedBlinkMs    = 0;
bool ledBlinkState              = false;

// Pesan toast sementara di OLED (mis. "TERKIRIM!", "TARE OK!", "DISIMPAN!")
char toastText[12]              = "";
unsigned long toastUntilMs      = 0;

// Struktur pembacaan tombol (dideklarasikan di atas semua fungsi agar
// auto-prototype preprocessor Arduino IDE mengenali tipe datanya)
struct ButtonTracker {
  uint8_t       pin;
  bool          rawLast;
  bool          stablePressed;
  bool          longHandled;
  unsigned long lastDebounceMs;
  unsigned long pressedAtMs;
  unsigned long lastRepeatMs;
};

struct ButtonEvents {
  bool shortPress;
  bool longPress;
  bool repeatTick;
};

ButtonTracker btnMeasure = { PIN_BTN_MEASURE, false, false, false, 0, 0, 0 };
ButtonTracker btnCalib   = { PIN_BTN_CALIB,   false, false, false, 0, 0, 0 };
ButtonTracker btnReset   = { PIN_BTN_RESET,   false, false, false, 0, 0, 0 };

// Deklarasi prototipe fungsi eksplisit
void showToast(const char* msg, unsigned long durationMs = 1300);
void sendHeightViaBLE(float heightVal, bool showOledFeedback);
float readSingleDistanceCm();
void sortFloatArray(float* arr, uint8_t n);
float capturePrecisionDistanceCm(uint8_t totalPings);
void updateContinuousUltrasonic();
void savePoleHeightToFlash();
void runAutoTareFloor();
ButtonEvents pollButton(ButtonTracker &btn);
void handleButtons();
void renderOledScreen();
void setupBLE();
void updateBleAndStatusLed();

// ============================================================================
// 5. CALLBACK BLE SERVER
// ============================================================================
class HeightServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* server) override {
    bleConnected = true;
  }

  void onDisconnect(BLEServer* server) override {
    bleConnected = false;
  }
};

// ============================================================================
// 6. FUNGSI UTILITAS TAMPILAN TOAST & PENGIRIMAN BLE
// ============================================================================
void showToast(const char* msg, unsigned long durationMs) {
  strncpy(toastText, msg, sizeof(toastText) - 1);
  toastText[sizeof(toastText) - 1] = '\0';
  toastUntilMs = millis() + durationMs;
}

void sendHeightViaBLE(float heightVal, bool showOledFeedback) {
  if (pHeightChar == nullptr || heightVal < MIN_VALID_HEIGHT_CM) {
    if (showOledFeedback && heightVal < MIN_VALID_HEIGHT_CM) {
      showToast("MIN 30 CM", 1200);
    }
    return;
  }

  char payload[16];
  dtostrf(heightVal, 4, 1, payload);

  // Hapus spasi depan hasil dtostrf agar bersih (contoh: "168.5")
  char* cleanPayload = payload;
  while (*cleanPayload == ' ') {
    cleanPayload++;
  }

  pHeightChar->setValue((uint8_t*)cleanPayload, strlen(cleanPayload));

  if (bleConnected) {
    pHeightChar->notify();
    Serial.printf("[BLE] Notify terkirim ke MEDISYN: %s cm\n", cleanPayload);
    if (showOledFeedback) {
      showToast("TERKIRIM!", 1200);
    }
  } else {
    Serial.printf("[HOLD] Tersimpan di lokal (BLE belum terhubung): %s cm\n", cleanPayload);
    if (showOledFeedback) {
      showToast("TERKUNCI!", 1000);
    }
  }
}

// ============================================================================
// 7. PEMBACAAN SENSOR ULTRASONIK HC-SR04 & FILTER MEDIAN-TRIMMED MEAN
// ============================================================================
float readSingleDistanceCm() {
  // Kecepatan suara di udara (m/s) = 331.3 + (0.606 * Suhu_C)
  // Dikonversi ke cm/us lalu dibagi 2 (perjalanan bolak-balik pantulan ultrasonik)
  const float soundCmPerUsHalf = ((331.3f + (0.606f * ROOM_TEMP_C)) * 0.0001f) * 0.5f;

  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(3);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);

  unsigned long durationUs = pulseIn(PIN_ECHO, HIGH, ECHO_TIMEOUT_US);
  if (durationUs == 0) {
    return -1.0f; // Tidak ada pantulan / timeout
  }

  float distCm = (float)durationUs * soundCmPerUsHalf;
  if (distCm < 2.5f || distCm > 420.0f) {
    return -1.0f; // Di luar rentang fisik wajar HC-SR04
  }
  return distCm;
}

void sortFloatArray(float* arr, uint8_t n) {
  for (uint8_t i = 0; i < n - 1; i++) {
    for (uint8_t j = i + 1; j < n; j++) {
      if (arr[j] < arr[i]) {
        float tmp = arr[i];
        arr[i] = arr[j];
        arr[j] = tmp;
      }
    }
  }
}

// Mengambil burst sampel presisi (digunakan saat menekan UKUR atau AUTO-TARE)
float capturePrecisionDistanceCm(uint8_t totalPings) {
  float validSamples[24];
  uint8_t validCount = 0;
  if (totalPings > 24) totalPings = 24;

  for (uint8_t i = 0; i < totalPings; i++) {
    float d = readSingleDistanceCm();
    if (d > 0.0f) {
      validSamples[validCount++] = d;
    }
    delay(60);
  }

  if (validCount == 0) {
    return -1.0f;
  }

  sortFloatArray(validSamples, validCount);

  // Buang 25% nilai terendah & 25% tertinggi (outlier rejection), rata-ratakan sisanya
  uint8_t trim = validCount / 4;
  float sum = 0.0f;
  uint8_t used = 0;
  for (uint8_t i = trim; i < validCount - trim; i++) {
    sum += validSamples[i];
    used++;
  }
  return (used > 0) ? (sum / (float)used) : validSamples[validCount / 2];
}

void updateContinuousUltrasonic() {
  unsigned long now = millis();
  if (now - lastPingMs < PING_INTERVAL_MS) {
    return;
  }
  lastPingMs = now;

  float rawDist = readSingleDistanceCm();
  if (rawDist < 0.0f) {
    // Jika gagal terus-menerus, tandai sensor tidak valid
    if (distCount > 0) {
      distCount--;
    } else {
      sensorValid = false;
      isReadingStable = false;
    }
    return;
  }

  distRing[distIndex] = rawDist;
  distIndex = (distIndex + 1) % FILTER_WINDOW_SIZE;
  if (distCount < FILTER_WINDOW_SIZE) {
    distCount++;
  }

  float sorted[FILTER_WINDOW_SIZE];
  for (uint8_t i = 0; i < distCount; i++) {
    sorted[i] = distRing[i];
  }
  sortFloatArray(sorted, distCount);

  float targetDist = sorted[distCount / 2];
  if (distCount >= 5) {
    // Trimmed mean dari 3 nilai tengah agar sangat halus namun tetap responsif
    uint8_t mid = distCount / 2;
    targetDist = (sorted[mid - 1] + sorted[mid] + sorted[mid + 1]) / 3.0f;
  }

  if (!sensorValid || filteredDistCm < 0.0f || fabs(targetDist - filteredDistCm) >= 3.0f) {
    // Perubahan besar (pasien masuk/keluar): langsung ikuti tanpa lag
    filteredDistCm = targetDist;
  } else {
    // Perubahan mikro: haluskan dengan EMA ringan
    filteredDistCm = (0.45f * targetDist) + (0.55f * filteredDistCm);
  }
  sensorValid = true;

  // Hitung tinggi badan = Tinggi Tiang - Jarak Kepala ke Sensor
  float rawHeight = poleHeightCm - filteredDistCm;
  if (fabs(rawHeight) <= ZERO_DEADZONE_CM) {
    liveHeightCm = 0.0f;
  } else if (rawHeight < 0.0f) {
    liveHeightCm = 0.0f;
  } else {
    liveHeightCm = roundf(rawHeight * 10.0f) / 10.0f; // Bulatkan ke 0.1 cm
  }

  // Evaluasi kestabilan tinggi badan (untuk indikator STABIL & opsional auto-notify)
  recentHeights[recentIdx] = liveHeightCm;
  recentIdx = (recentIdx + 1) % 12;
  if (recentCount < 12) recentCount++;

  if (recentCount >= 10 && liveHeightCm >= MIN_VALID_HEIGHT_CM) {
    float minH = recentHeights[0];
    float maxH = recentHeights[0];
    for (uint8_t i = 1; i < recentCount; i++) {
      if (recentHeights[i] < minH) minH = recentHeights[i];
      if (recentHeights[i] > maxH) maxH = recentHeights[i];
    }
    if ((maxH - minH) <= 0.6f) {
      if (!isReadingStable) {
        isReadingStable = true;
        stableSinceMs = now;
      }
    } else {
      isReadingStable = false;
    }
  } else {
    isReadingStable = false;
    if (liveHeightCm < 15.0f) {
      autoSentCurrentStanding = false; // Reset flag saat pasien sudah turun/keluar
    }
  }

  // Jika MEDISYN sedang konek di mode LIVE dan tinggi pasien sudah stabil penuh 2.5 detik,
  // kirim otomatis sekali agar operator di layar sentuh MEDISYN tetap menerima hasil
  // meskipun belum menekan tombol fisik di tiang.
  if (currentMode == MODE_LIVE &&
      bleConnected &&
      isReadingStable &&
      !autoSentCurrentStanding &&
      liveHeightCm >= AUTO_SEND_MIN_HEIGHT_CM &&
      (now - stableSinceMs >= 2500UL)) {
    autoSentCurrentStanding = true;
    sendHeightViaBLE(liveHeightCm, true);
  }
}

// ============================================================================
// 8. KALIBRASI LANTAI OTOMATIS (AUTO-TARE) & SIMPAN NVS FLASH
// ============================================================================
void savePoleHeightToFlash() {
  prefs.putFloat("pole_cm", poleHeightCm);
  Serial.printf("[NVS] Tinggi tiang disimpan: %.1f cm\n", poleHeightCm);
}

void runAutoTareFloor() {
  if (oledReady) {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.print("KALIBRASI LANTAI...");
    display.drawFastHLine(0, 10, 128, SSD1306_WHITE);
    display.setCursor(0, 16);
    display.print("Kosongkan bawah alat");
    display.display();
  }

  Serial.println("[KALIBRASI] Memulai Auto-Tare ke lantai (16 sampel)...");
  float floorDist = capturePrecisionDistanceCm(16);

  if (floorDist >= MIN_POLE_HEIGHT_CM && floorDist <= MAX_POLE_HEIGHT_CM) {
    poleHeightCm = roundf(floorDist * 10.0f) / 10.0f;
    savePoleHeightToFlash();
    filteredDistCm = poleHeightCm;
    liveHeightCm   = 0.0f;
    distCount      = 0;
    currentMode    = MODE_LIVE;
    showToast("TARE OK!", 1500);
    Serial.printf("[KALIBRASI] Berhasil! Tinggi tiang baru = %.1f cm\n", poleHeightCm);
  } else {
    showToast("GAGAL TARE", 1500);
    Serial.printf("[KALIBRASI] Gagal Auto-Tare (bacaan: %.1f cm di luar 50-300 cm)\n", floorDist);
  }
}

// ============================================================================
// 9. LOGIKA 3 PUSH BUTTON (DEBOUNCE, SHORT PRESS, LONG PRESS, REPEAT)
// ============================================================================
ButtonEvents pollButton(ButtonTracker &btn) {
  ButtonEvents ev = { false, false, false };
  unsigned long now = millis();
  bool rawPressed = (digitalRead(btn.pin) == LOW); // INPUT_PULLUP: ditekan = LOW

  if (rawPressed != btn.rawLast) {
    btn.lastDebounceMs = now;
    btn.rawLast = rawPressed;
  }

  if ((now - btn.lastDebounceMs) >= DEBOUNCE_MS) {
    if (rawPressed && !btn.stablePressed) {
      // Tombol baru saja ditekan
      btn.stablePressed = true;
      btn.longHandled   = false;
      btn.pressedAtMs   = now;
      btn.lastRepeatMs  = now;
    } else if (!rawPressed && btn.stablePressed) {
      // Tombol baru saja dilepas
      btn.stablePressed = false;
      if (!btn.longHandled) {
        ev.shortPress = true;
      }
    } else if (rawPressed && btn.stablePressed) {
      // Tombol sedang ditahan
      if (!btn.longHandled && (now - btn.pressedAtMs >= LONG_PRESS_MS)) {
        btn.longHandled = true;
        ev.longPress    = true;
      }
      if ((now - btn.pressedAtMs >= REPEAT_INITIAL_MS) &&
          (now - btn.lastRepeatMs >= REPEAT_STEP_MS)) {
        btn.lastRepeatMs = now;
        ev.repeatTick    = true;
      }
    }
  }
  return ev;
}

void handleButtons() {
  ButtonEvents evMeasure = pollButton(btnMeasure);
  ButtonEvents evCalib   = pollButton(btnCalib);
  ButtonEvents evReset   = pollButton(btnReset);

  // --- MODE KALIBRASI MANUAL TIANG (SET TIANG) ---
  if (currentMode == MODE_CALIB_POLE) {
    if (evMeasure.shortPress) {
      poleHeightCm = fminf(MAX_POLE_HEIGHT_CM, roundf((poleHeightCm + 0.1f) * 10.0f) / 10.0f);
    } else if (evMeasure.repeatTick) {
      poleHeightCm = fminf(MAX_POLE_HEIGHT_CM, roundf((poleHeightCm + 0.5f) * 10.0f) / 10.0f);
    }

    if (evReset.shortPress) {
      poleHeightCm = fmaxf(MIN_POLE_HEIGHT_CM, roundf((poleHeightCm - 0.1f) * 10.0f) / 10.0f);
    } else if (evReset.repeatTick) {
      poleHeightCm = fmaxf(MIN_POLE_HEIGHT_CM, roundf((poleHeightCm - 0.5f) * 10.0f) / 10.0f);
    }

    if (evCalib.shortPress || evCalib.longPress) {
      savePoleHeightToFlash();
      currentMode = MODE_LIVE;
      showToast("DISIMPAN!", 1300);
    }
    return;
  }

  // --- TOMBOL 2 (KALIBRASI): Long Press = Auto-Tare Lantai, Short Press = Menu Set Tiang ---
  if (evCalib.longPress) {
    runAutoTareFloor();
    return;
  }
  if (evCalib.shortPress) {
    currentMode = MODE_CALIB_POLE;
    return;
  }

  // --- TOMBOL 1 (UKUR / KUNCI & KIRIM BLE) ---
  if (evMeasure.shortPress) {
    if (currentMode == MODE_HOLD) {
      // Jika sudah terkunci, kirim ulang nilai yang terkunci ke BLE
      sendHeightViaBLE(lockedHeightCm, true);
      lastHoldBleSendMs = millis();
    } else {
      // Ambil nilai terfilter saat ini, kunci (HOLD), dan kirim via BLE
      if (!sensorValid) {
        showToast("CEK SENSOR", 1200);
      } else {
        lockedHeightCm = liveHeightCm;
        currentMode    = MODE_HOLD;
        lastHoldBleSendMs = millis();
        sendHeightViaBLE(lockedHeightCm, true);
      }
    }
  }

  // --- TOMBOL 3 (RESET KE LIVE / TAHAN UNTUK MODE CEK JARAK SENSOR) ---
  if (evReset.longPress) {
    if (currentMode == MODE_RAW_DIST) {
      currentMode = MODE_LIVE;
      showToast("MODE TINGGI", 1000);
    } else {
      currentMode = MODE_RAW_DIST;
      showToast("MODE JARAK", 1000);
    }
    return;
  }

  if (evReset.shortPress) {
    if (currentMode == MODE_HOLD || currentMode == MODE_RAW_DIST) {
      currentMode             = MODE_LIVE;
      autoSentCurrentStanding = false;
      showToast("SIAP UKUR", 800);
    }
  }
}

// ============================================================================
// 10. TAMPILAN ANTARMUKA LCD OLED 0.91" (128x32 PIKSEL)
// ============================================================================
void renderOledScreen() {
  if (!oledReady) return;

  unsigned long now = millis();
  if (now - lastOledMs < 100UL) return; // Refresh ~10 FPS
  lastOledMs = now;

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // ---------------- BARIS ATAS (y = 0..7, Font Size 1) ----------------
  display.setTextSize(1);

  if (currentMode == MODE_HOLD) {
    // Badge putih mencolok saat nilai dikunci (HOLD)
    display.fillRect(0, 0, 28, 8, SSD1306_WHITE);
    display.setTextColor(SSD1306_BLACK, SSD1306_WHITE);
    display.setCursor(2, 0);
    display.print("HOLD");
    display.setTextColor(SSD1306_WHITE);
  } else if (currentMode == MODE_CALIB_POLE) {
    display.setCursor(0, 0);
    display.print("SET TIANG (+/-)");
  } else if (currentMode == MODE_RAW_DIST) {
    display.setCursor(0, 0);
    display.print("JARAK");
  } else {
    display.setCursor(0, 0);
    display.print(isReadingStable ? "STABIL" : "LIVE");
  }

  // Tampilkan info tinggi tiang & status BLE di baris atas (kecuali sedang mode SET TIANG)
  if (currentMode != MODE_CALIB_POLE) {
    char topMid[12];
    snprintf(topMid, sizeof(topMid), "T:%.0f", poleHeightCm);
    display.setCursor(44, 0);
    display.print(topMid);
  }

  display.setCursor(92, 0);
  display.print(bleConnected ? "BLE:OK" : "BLE:..");

  // Garis pemisah horizontal
  display.drawFastHLine(0, 9, 128, SSD1306_WHITE);

  // ---------------- AREA UTAMA (y = 14..30, Font Size 2) ----------------
  display.setTextSize(2);
  char mainLine[14];

  if (now < toastUntilMs && toastText[0] != '\0') {
    snprintf(mainLine, sizeof(mainLine), "%s", toastText);
  } else if (currentMode == MODE_CALIB_POLE) {
    snprintf(mainLine, sizeof(mainLine), "%.1f cm", poleHeightCm);
  } else if (currentMode == MODE_HOLD) {
    snprintf(mainLine, sizeof(mainLine), "%.1f cm", lockedHeightCm);
  } else if (!sensorValid) {
    snprintf(mainLine, sizeof(mainLine), "NO ECHO");
  } else if (currentMode == MODE_RAW_DIST) {
    snprintf(mainLine, sizeof(mainLine), "%.1f cm", filteredDistCm);
  } else {
    snprintf(mainLine, sizeof(mainLine), "%.1f cm", liveHeightCm);
  }

  // Ratakan tengah secara horizontal pada layar lebar 128 px (1 karakter size 2 = 12 px)
  int16_t textWidth = (int16_t)strlen(mainLine) * 12;
  int16_t xPos = (SCREEN_WIDTH - textWidth) / 2;
  if (xPos < 0) xPos = 0;

  display.setCursor(xPos, 14);
  display.print(mainLine);

  display.display();
}

// ============================================================================
// 11. PENGELOLAAN KONEKSI BLE & LED INDIKATOR ONBOARD
// ============================================================================
void setupBLE() {
  BLEDevice::init(BLE_DEVICE_NAME);
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new HeightServerCallbacks());

  BLEService *pService = pServer->createService(BLE_SERVICE_UUID);

  pHeightChar = pService->createCharacteristic(
    BLE_HEIGHT_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY
  );
  pHeightChar->addDescriptor(new BLE2902());
  pHeightChar->setValue((uint8_t*)"0.0", 3);

  pService->start();

  BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(BLE_SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  pAdvertising->setMinPreferred(0x06);
  pAdvertising->setMaxPreferred(0x12);
  BLEDevice::startAdvertising();

  Serial.println("[BLE] Server aktif. Nama perangkat: bleScaleHeight");
}

void updateBleAndStatusLed() {
  unsigned long now = millis();

  // Restart advertising otomatis saat klien BLE (MEDISYN / HP) selesai dan putus koneksi
  if (!bleConnected && prevBleConnected) {
    delay(50);
    BLEDevice::startAdvertising();
    Serial.println("[BLE] Klien terputus -> Advertising diaktifkan kembali.");
  }
  if (bleConnected && !prevBleConnected) {
    Serial.println("[BLE] Klien MEDISYN terhubung!");
    // Jika operator sudah mengunci tinggi badan (HOLD) lebih dulu sebelum klik di MEDISYN,
    // jadwalkan pengiriman segera begitu MEDISYN mengaktifkan start_notify()
    lastHoldBleSendMs = now - 600UL;
  }
  prevBleConnected = bleConnected;

  // Selama mode HOLD aktif & MEDISYN terhubung, kirim notify berkala tiap 1 detik
  // sehingga handle_height_capture_task() di MEDISYN langsung menangkap hasil terkunci!
  if (currentMode == MODE_HOLD &&
      bleConnected &&
      lockedHeightCm >= MIN_VALID_HEIGHT_CM &&
      (now - lastHoldBleSendMs >= 1000UL)) {
    lastHoldBleSendMs = now;
    sendHeightViaBLE(lockedHeightCm, false);
  }

  // LED Biru Onboard ESP32-C3 Super Mini (GPIO8 adalah Aktif LOW):
  // - Nyala solid jika terhubung ke BLE
  // - Berkedip pelan jika standby/advertising
  if (bleConnected) {
    digitalWrite(PIN_LED_STATUS, LOW);
  } else if (now - lastLedBlinkMs >= 800UL) {
    lastLedBlinkMs = now;
    ledBlinkState  = !ledBlinkState;
    digitalWrite(PIN_LED_STATUS, ledBlinkState ? LOW : HIGH);
  }
}

// ============================================================================
// 12. SETUP & LOOP UTAMA
// ============================================================================
void setup() {
  Serial.begin(115200);
  delay(200);

  // Inisialisasi GPIO Sensor Ultrasonik, 3 Push Button, dan LED Status
  pinMode(PIN_TRIG, OUTPUT);
  digitalWrite(PIN_TRIG, LOW);
  pinMode(PIN_ECHO, INPUT);

  pinMode(PIN_BTN_MEASURE, INPUT_PULLUP);
  pinMode(PIN_BTN_CALIB,   INPUT_PULLUP);
  pinMode(PIN_BTN_RESET,   INPUT_PULLUP);

  pinMode(PIN_LED_STATUS, OUTPUT);
  digitalWrite(PIN_LED_STATUS, HIGH); // Matikan dulu (aktif LOW)

  // Muat kalibrasi tinggi tiang dari memori permanen NVS Flash
  prefs.begin("medisyn_hgt", false);
  poleHeightCm = prefs.getFloat("pole_cm", DEFAULT_POLE_HEIGHT_CM);
  if (isnan(poleHeightCm) || poleHeightCm < MIN_POLE_HEIGHT_CM || poleHeightCm > MAX_POLE_HEIGHT_CM) {
    poleHeightCm = DEFAULT_POLE_HEIGHT_CM;
  }

  // Inisialisasi I2C & Layar OLED 0.91 inch (128x32)
  Wire.begin(PIN_OLED_SDA, PIN_OLED_SCL);
  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR)) {
    oledReady = true;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(16, 2);
    display.print("MEDISYN HEIGHT");
    display.drawFastHLine(0, 12, 128, SSD1306_WHITE);
    display.setCursor(8, 18);
    display.printf("BLE: %s", BLE_DEVICE_NAME);
    display.display();
    delay(1200);
  } else {
    Serial.println("[OLED] Gagal mendeteksi SSD1306 pada alamat 0x3C!");
  }

  // Inisialisasi BLE Server
  setupBLE();

  Serial.println("====================================================");
  Serial.println("MEDISYN - Alat Ukur Tinggi Badan BLE Siap Digunakan!");
  Serial.printf("Tinggi Tiang Aktif : %.1f cm\n", poleHeightCm);
  Serial.println("BTN1 (GPIO0) : UKUR / HOLD & Kirim BLE / (+)");
  Serial.println("BTN2 (GPIO1) : Set Tiang (Klik) / Auto-Tare (Tahan)");
  Serial.println("BTN3 (GPIO3) : Reset ke Live (Klik) / Mode Jarak (Tahan)");
  Serial.println("====================================================");
}

void loop() {
  handleButtons();
  updateContinuousUltrasonic();
  updateBleAndStatusLed();
  renderOledScreen();
}
