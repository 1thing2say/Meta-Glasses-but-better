#!/usr/bin/env python3
"""
capture_flash.py
Unified ESP32 capture script with Gemini 2.5 Flash integration.

Uses Gemini 2.5 Flash for faster, cheaper multimodal analysis
while retaining native image + audio support.

Usage:
    python capture_flash.py
"""

import serial
import serial.tools.list_ports
import wave
import os
import io
import base64
import requests
import json
import subprocess
import tempfile
from datetime import datetime
from dotenv import load_dotenv

# Load API keys from .env file
load_dotenv()

# ==========================================
# SETTINGS
# ==========================================
USB_COM_PORT = '/dev/cu.usbserial-0001'
BAUD_RATE = 921600

SAVE_FOLDER = "captures"

# ==========================================
# OPENROUTER / GEMINI SETTINGS
# ==========================================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_MODEL = "google/gemini-3-flash-preview"

# ==========================================
# ELEVENLABS TTS SETTINGS
# ==========================================
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = "TDat60GFW24Clb3a96nu"  # Deku
ELEVENLABS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"

# The system prompt that tells Gemini to act as a voice assistant
SYSTEM_PROMPT = """You are a cheerful, anime-inspired voice assistant embedded in a wearable ESP32 device!
The user speaks to you through a microphone — their voice is in the audio clip.
A camera captures what the user is looking at — use the image as visual context if relevant to their question.
Listen to what the user says in the audio, then answer their question directly.
Rules:
- Be succinct. 1-3 sentences max unless they ask for detail.
- NEVER use special characters in your response. No asterisks, no markdown, no bullet points, no dashes, no hashtags, no emojis, no parentheses for emphasis. Your response will be read aloud by a text-to-speech engine, so write in plain, natural spoken language only.
- Sound like an enthusiastic anime sidekick! Use expressive language, excitement, and energy.
- Sprinkle in occasional Japanese expressions naturally (like sugoi, nani, yosh, sou desu ne) but keep the answer in English.
- Use dramatic flair when appropriate — hype things up!
- If the image is relevant to their question, reference what you see with excitement.
- If the image is NOT relevant, just answer the spoken question and ignore the image.
- Respond as if you're the user's loyal companion on an epic adventure!"""

# ==========================================
# MARKERS (must match your Arduino sketch)
# ==========================================
IMAGE_MARKER = bytes([0xFF, 0xAA, 0xBB, 0xCC])


class UnifiedReceiver:
    def __init__(self, port, baud):
        self.ser = serial.Serial(port, baud, timeout=1)
        self.image_count = 0
        self.audio_count = 0
        self.capture_count = 0

        if not os.path.exists(SAVE_FOLDER):
            os.makedirs(SAVE_FOLDER)
            print(f"  Created folder: {SAVE_FOLDER}/")

        if not OPENROUTER_API_KEY:
            print("\n  ⚠️  WARNING: No OPENROUTER_API_KEY set!")
            print("  Set it with: export OPENROUTER_API_KEY='sk-or-v1-your-key'")
            print("  Captures will still be saved, but Gemini analysis will be skipped.\n")

    def listen(self):
        print(f"\nListening on {self.ser.port}...")
        print("Press the button on your ESP32 to capture a photo + audio.\n")

        pending_text = bytearray()
        marker_progress = 0

        # Track the latest capture pair
        latest_image_path = None
        latest_audio_path = None

        while True:
            raw = self.ser.read(1)
            if not raw:
                if pending_text:
                    self._handle_text_line(
                        pending_text.decode('utf-8', errors='ignore').strip(),
                        latest_image_path, latest_audio_path
                    )
                    pending_text.clear()
                continue

            byte = raw[0]

            # --- Try to match the binary image marker ---
            if byte == IMAGE_MARKER[marker_progress]:
                marker_progress += 1
                if marker_progress == len(IMAGE_MARKER):
                    marker_progress = 0
                    pending_text.clear()
                    latest_image_path = self._receive_image()
                continue
            else:
                if marker_progress > 0:
                    pending_text.extend(IMAGE_MARKER[:marker_progress])
                    marker_progress = 0

            # --- Accumulate text bytes ---
            if byte == ord('\n'):
                line = pending_text.decode('utf-8', errors='ignore').strip()
                pending_text.clear()
                if line:
                    result = self._handle_text_line(line, latest_image_path, latest_audio_path)
                    if result:
                        latest_audio_path = result
                        # Both image and audio are now captured — send to Gemini!
                        if latest_image_path or latest_audio_path:
                            self._send_to_gemini(latest_image_path, latest_audio_path)
                            latest_image_path = None
                            latest_audio_path = None
            else:
                pending_text.append(byte)

    def _handle_text_line(self, line, latest_image, latest_audio):
        """Process a complete text line from the ESP32. Returns audio path if audio was received."""
        if line.startswith("AUDIO_START:"):
            return self._receive_audio(line)
        else:
            print(f"  [ESP32] {line}")
            return None

    # --------------------------------------------------
    # IMAGE RECEPTION
    # --------------------------------------------------
    def _receive_image(self):
        size_bytes = self.ser.read(4)
        if len(size_bytes) != 4:
            print("  ERROR: Could not read image size header.")
            return None

        image_size = int.from_bytes(size_bytes, byteorder='big')

        if image_size <= 0 or image_size > 500000:
            print(f"  ERROR: Invalid image size: {image_size}")
            return None

        data = bytearray()
        remaining = image_size
        timeouts = 0
        while remaining > 0 and timeouts < 15:
            chunk = self.ser.read(remaining)
            if chunk:
                data.extend(chunk)
                remaining -= len(chunk)
                timeouts = 0
            else:
                timeouts += 1

        if len(data) == image_size:
            self.image_count += 1
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"photo_{timestamp}_{self.image_count}.jpg"
            filepath = os.path.join(SAVE_FOLDER, filename)
            with open(filepath, 'wb') as f:
                f.write(data)
            print(f"  📷 Saved {filename} ({len(data)} bytes)")
            return filepath
        else:
            print(f"  ERROR: Image incomplete. Expected {image_size}, got {len(data)}")
            return None

    # --------------------------------------------------
    # AUDIO RECEPTION
    # --------------------------------------------------
    def _receive_audio(self, header_line):
        parts = header_line.split(":")
        sample_rate = int(parts[1])
        num_samples = int(parts[2])
        num_bytes = num_samples * 2

        print(f"  🎙️  Recording {num_samples} samples at {sample_rate} Hz...")

        raw_data = bytearray()
        remaining = num_bytes
        timeouts = 0
        while remaining > 0 and timeouts < 15:
            chunk = self.ser.read(remaining)
            if chunk:
                raw_data.extend(chunk)
                remaining -= len(chunk)
                timeouts = 0
            else:
                timeouts += 1

        filepath = None
        if len(raw_data) == num_bytes:
            self.audio_count += 1
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"audio_{timestamp}_{self.audio_count}.wav"
            filepath = os.path.join(SAVE_FOLDER, filename)

            with wave.open(filepath, 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(raw_data)

            print(f"  🎙️  Saved {filename} ({len(raw_data)} bytes)")
        else:
            print(f"  ERROR: Audio incomplete. Expected {num_bytes}, got {len(raw_data)}")

        # Drain the AUDIO_END marker
        for _ in range(10):
            end_line = self.ser.readline().decode('utf-8', errors='ignore').strip()
            if "AUDIO_END" in end_line:
                break

        return filepath

    # --------------------------------------------------
    # GEMINI FLASH ANALYSIS VIA OPENROUTER
    # --------------------------------------------------
    def _send_to_gemini(self, image_path, audio_path):
        if not OPENROUTER_API_KEY:
            print("  ⚠️  Skipping Gemini analysis (no API key set)\n")
            return

        self.capture_count += 1
        print(f"\n  ⚡ Sending capture #{self.capture_count} to Gemini Flash...")

        # Build the multimodal content array
        content_parts = []

        # Add the image
        if image_path and os.path.exists(image_path):
            with open(image_path, 'rb') as f:
                img_b64 = base64.b64encode(f.read()).decode('utf-8')
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{img_b64}"
                }
            })

        # Add the audio
        if audio_path and os.path.exists(audio_path):
            with open(audio_path, 'rb') as f:
                audio_b64 = base64.b64encode(f.read()).decode('utf-8')
            content_parts.append({
                "type": "input_audio",
                "input_audio": {
                    "data": audio_b64,
                    "format": "wav"
                }
            })

        # Add a text prompt
        content_parts.append({
            "type": "text",
            "text": "Listen to my question in the audio clip. The image shows what I'm currently looking at — use it as context if it's relevant to my question. Answer me directly."
        })

        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/esp32-capture",
            "X-Title": "ESP32 Capture Tool"
        }

        payload = {
            "model": GEMINI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content_parts}
            ],
            "max_tokens": 1024
        }

        try:
            response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                reply = result["choices"][0]["message"]["content"]

                # Split transcript from answer
                lines = reply.strip().split('\n')
                transcript = ""
                answer = reply

                for i, line in enumerate(lines):
                    if line.strip().lower().startswith("you said:"):
                        transcript = line.strip()
                        answer = '\n'.join(lines[i+1:]).strip()
                        break

                if transcript:
                    print(f"\n  🎤 {transcript}")
                print(f"  ⚡ Gemini Flash: {answer}\n")

                # Only speak the answer, not the transcript
                self._speak(answer)
            else:
                print(f"  ❌ OpenRouter API error {response.status_code}: {response.text}\n")

        except requests.exceptions.Timeout:
            print("  ❌ Gemini request timed out.\n")
        except Exception as e:
            print(f"  ❌ Error sending to Gemini: {e}\n")

        print("  Ready for next capture.\n")

    # --------------------------------------------------
    # ELEVENLABS TEXT-TO-SPEECH
    # --------------------------------------------------
    def _speak(self, text):
        if not ELEVENLABS_API_KEY:
            return

        try:
            headers = {
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            }
            payload = {
                "text": text,
                "model_id": "eleven_turbo_v2",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75
                }
            }

            response = requests.post(ELEVENLABS_URL, headers=headers, json=payload, timeout=15)

            if response.status_code == 200:
                # Save to a temp file and play it
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp.write(response.content)
                    tmp_path = tmp.name
                # Play audio on macOS
                subprocess.run(["afplay", tmp_path], check=True)
                os.unlink(tmp_path)
            else:
                print(f"  ⚠️  ElevenLabs TTS error {response.status_code}: {response.text[:100]}")

        except Exception as e:
            print(f"  ⚠️  TTS failed: {e}")


if __name__ == "__main__":
    print("========================================")
    print("  ESP32 Capture + Gemini 2.5 Flash")
    print("  (Camera + INMP441 Audio → Flash)")
    print("========================================")

    print("\nAvailable COM Ports:")
    for p in serial.tools.list_ports.comports():
        print(f"  - {p.device}: {p.description}")

    print(f"\nOpening port: {USB_COM_PORT}")
    try:
        receiver = UnifiedReceiver(USB_COM_PORT, BAUD_RATE)
        receiver.listen()
    except serial.SerialException:
        print(f"\nERROR: Could not open {USB_COM_PORT}.")
        print("1. Ensure your ESP32 is plugged in via USB.")
        print("2. Ensure the Arduino Serial Monitor is CLOSED.")
        print("3. Check the port list above and update USB_COM_PORT in the script.")
    except KeyboardInterrupt:
        print("\nStopping capture tool.")
