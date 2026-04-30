# -*- coding: utf-8 -*-
# PyAudio デバイス一覧 + RMS確認スクリプト
# 使い方: python check_audio_devices.py
import pyaudio
import struct, math, time

pa = pyaudio.PyAudio()

print("=" * 60)
print("  入力デバイス一覧")
print("=" * 60)
input_devices = []
for i in range(pa.get_device_count()):
    info = pa.get_device_info_by_index(i)
    if info["maxInputChannels"] > 0:
        input_devices.append(i)
        default_mark = " ← デフォルト" if i == pa.get_default_input_device_info()["index"] else ""
        print(f"  [{i}] {info['name']}{default_mark}")
        print(f"       サンプルレート: {int(info['defaultSampleRate'])}Hz  "
              f"入力ch: {info['maxInputChannels']}")

print("=" * 60)
print(f"\nデフォルトデバイスindex: {pa.get_default_input_device_info()['index']}")
print(f"デフォルトデバイス名:   {pa.get_default_input_device_info()['name']}")
print()

# 各デバイスで0.5秒録音してRMSを計測
RATE   = 16000
CHUNK  = 1024
FORMAT = pyaudio.paInt16

def rms(data):
    n = len(data) // 2
    if n == 0: return 0.0
    shorts = struct.unpack(f"{n}h", data[:n*2])
    return math.sqrt(sum(s*s for s in shorts) / n)

print("各デバイスのRMS計測（0.5秒）...")
print("-" * 60)
for idx in input_devices:
    info = pa.get_device_info_by_index(idx)
    try:
        stream = pa.open(
            format=FORMAT, channels=1, rate=RATE,
            input=True, input_device_index=idx,
            frames_per_buffer=CHUNK)
        vals = []
        for _ in range(8):
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
                vals.append(rms(data))
            except Exception:
                pass
        stream.stop_stream()
        stream.close()
        avg = sum(vals)/len(vals) if vals else 0
        print(f"  [{idx}] {info['name'][:40]:<40}  RMS={avg:.1f}")
    except Exception as e:
        print(f"  [{idx}] {info['name'][:40]:<40}  ERROR: {e}")

pa.terminate()
print()
print("→ 声を拾うべきマイクのindexをメモしてください")
