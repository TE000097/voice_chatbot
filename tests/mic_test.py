import sounddevice as sd
import wavio

duration = 5  # seconds
fs = 16000  # sample rate

print("Recording for 5 seconds...")
recording = sd.rec(int(duration * fs), samplerate=fs, channels=1)
sd.wait()  # Wait until recording is finished

wavio.write("test_mic.wav", recording, fs, sampwidth=2)
print("Recording saved to test_mic.wav")
