#include <Arduino.h>
#include <WiFi.h>
#include <driver/i2s.h>

// ==========================================
// ELEGOO CAMERA AP SETTINGS
// ==========================================
const char *cameraAP_SSID = "ELEGOO-2CAF18A0ED30";
const char *cameraAP_PASS = "";

// ==========================================
// I2S MICROPHONE (INMP441) PINS
// ==========================================
#define I2S_WS 15
#define I2S_SCK 14
#define I2S_SD 32
#define I2S_PORT I2S_NUM_0

// ==========================================
// AUDIO RECORDING SETTINGS
// ==========================================
#define SAMPLE_RATE 16000
#define RECORD_SECONDS 5
#define TOTAL_SAMPLES (SAMPLE_RATE * RECORD_SECONDS)
#define MIC_GAIN 32

// ==========================================
// HARDWARE SETTINGS
// ==========================================
#define LED_PIN 5
#define BUTTON_PIN 13
#define BAUD_RATE 921600

// Shared chunk buffers for streaming
uint8_t chunkBuffer[2048];
int16_t audioChunkBuffer[512];

bool lastButtonState = HIGH;

void setup() {
  Serial.begin(BAUD_RATE);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  digitalWrite(LED_PIN, LOW);

  // ----- I2S Microphone Setup -----
  i2s_config_t i2s_config = {.mode =
                                 (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
                             .sample_rate = SAMPLE_RATE,
                             .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
                             .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
                             .communication_format = I2S_COMM_FORMAT_STAND_I2S,
                             .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
                             .dma_buf_count = 8,
                             .dma_buf_len = 64,
                             .use_apll = false,
                             .tx_desc_auto_clear = false,
                             .fixed_mclk = 0};

  i2s_pin_config_t pin_config = {.bck_io_num = I2S_SCK,
                                 .ws_io_num = I2S_WS,
                                 .data_out_num = I2S_PIN_NO_CHANGE,
                                 .data_in_num = I2S_SD};

  i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  i2s_set_pin(I2S_PORT, &pin_config);

  // ----- Wi-Fi: Connect to Elegoo Camera AP -----
  WiFi.begin(cameraAP_SSID, cameraAP_PASS);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
  }

  digitalWrite(LED_PIN, LOW); // Ready — LED off until button press
  delay(1000);
}

void loop() {
  bool currentButtonState = digitalRead(BUTTON_PIN);

  // Detect button press (transition from HIGH to LOW)
  if (currentButtonState == LOW && lastButtonState == HIGH) {
    delay(50); // debounce

    // ==========================================
    // BUTTON PRESSED: Photo + 5s Audio
    // ==========================================
    digitalWrite(LED_PIN, HIGH); // LED ON for the entire capture

    // Step 1: Take a photo
    captureAndSendPhoto();

    // Step 2: Record 5 seconds of audio
    recordAndSendAudio();

    digitalWrite(LED_PIN, LOW); // LED OFF when done

    delay(200); // cooldown before next press
  }

  lastButtonState = currentButtonState;
}

// ==========================================
// CAMERA CAPTURE (streams via USB)
// ==========================================
void captureAndSendPhoto() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("PHOTO_SKIP:wifi_disconnected");
    return;
  }

  WiFiClient client;
  if (!client.connect("192.168.4.1", 80)) {
    Serial.println("PHOTO_SKIP:connection_failed");
    return;
  }

  Serial.println("PHOTO_DEBUG:requesting_image");

  // Request a snapshot — simple HTTP/1.0 to guarantee Connection: close
  // behavior
  client.print("GET /capture HTTP/1.0\r\nHost: 192.168.4.1\r\n\r\n");

  // Wait for the server's response
  unsigned long timeout = millis();
  while (client.available() == 0) {
    if (millis() - timeout > 5000) {
      Serial.println("PHOTO_SKIP:timeout");
      client.stop();
      return;
    }
  }

  // Parse HTTP headers — look for Content-Length (case-insensitive)
  size_t imageLength = 0;
  while (client.connected()) {
    String line = client.readStringUntil('\n');
    if (line == "\r")
      break;
    String lower = line;
    lower.toLowerCase();
    if (lower.startsWith("content-length:")) {
      imageLength = line.substring(line.indexOf(':') + 1).toInt();
    }
  }

  Serial.print("PHOTO_DEBUG:content_length=");
  Serial.println(imageLength);

  if (imageLength > 0) {
    // We know the exact size — stream with marker protocol
    Serial.write(0xFF);
    Serial.write(0xAA);
    Serial.write(0xBB);
    Serial.write(0xCC);

    Serial.write((imageLength >> 24) & 0xFF);
    Serial.write((imageLength >> 16) & 0xFF);
    Serial.write((imageLength >> 8) & 0xFF);
    Serial.write(imageLength & 0xFF);

    size_t bytesRemaining = imageLength;
    while (client.connected() && bytesRemaining > 0) {
      if (client.available()) {
        size_t toRead = (bytesRemaining > sizeof(chunkBuffer))
                            ? sizeof(chunkBuffer)
                            : bytesRemaining;
        size_t readBytes = client.read(chunkBuffer, toRead);
        if (readBytes > 0) {
          Serial.write(chunkBuffer, readBytes);
          bytesRemaining -= readBytes;
        }
      }
    }
  } else {
    // No Content-Length — buffer what the server sends, then transmit
    Serial.println("PHOTO_DEBUG:no_content_length_buffering");
    size_t totalRead = 0;
    const size_t MAX_PHOTO = 100000;
    uint8_t *photoData = (uint8_t *)malloc(MAX_PHOTO);
    if (photoData) {
      unsigned long readTimeout = millis();
      while ((client.connected() || client.available()) &&
             totalRead < MAX_PHOTO) {
        if (client.available()) {
          int b = client.read();
          if (b >= 0) {
            photoData[totalRead++] = (uint8_t)b;
            readTimeout = millis();
          }
        } else if (millis() - readTimeout > 2000) {
          break;
        }
      }

      Serial.print("PHOTO_DEBUG:buffered_bytes=");
      Serial.println(totalRead);

      if (totalRead > 0) {
        Serial.write(0xFF);
        Serial.write(0xAA);
        Serial.write(0xBB);
        Serial.write(0xCC);

        Serial.write((totalRead >> 24) & 0xFF);
        Serial.write((totalRead >> 16) & 0xFF);
        Serial.write((totalRead >> 8) & 0xFF);
        Serial.write(totalRead & 0xFF);

        Serial.write(photoData, totalRead);
      }
      free(photoData);
    }
  }
  client.stop();
}

// ==========================================
// AUDIO RECORDING (streams via USB)
// ==========================================
void recordAndSendAudio() {
  // Send text-based audio header
  Serial.print("AUDIO_START:");
  Serial.print(SAMPLE_RATE);
  Serial.print(":");
  Serial.println(TOTAL_SAMPLES);

  int samplesRemaining = TOTAL_SAMPLES;

  while (samplesRemaining > 0) {
    int samplesToRead = min(512, samplesRemaining);

    for (int i = 0; i < samplesToRead; i++) {
      int32_t raw_sample;
      size_t bytes_read;
      i2s_read(I2S_PORT, &raw_sample, sizeof(raw_sample), &bytes_read,
               portMAX_DELAY);

      int32_t amplified = (raw_sample >> 16) * MIC_GAIN;
      if (amplified > 32767)
        amplified = 32767;
      if (amplified < -32768)
        amplified = -32768;

      audioChunkBuffer[i] = (int16_t)amplified;
    }

    Serial.write((uint8_t *)audioChunkBuffer, samplesToRead * sizeof(int16_t));
    samplesRemaining -= samplesToRead;
  }

  Serial.println("AUDIO_END");
}
