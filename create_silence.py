import wave
import os

def create_silence_wav(filename="silence.wav", duration=1):
    sample_rate = 44100
    num_channels = 1  # Mono
    sample_width = 2  # 16-bit
    num_frames = sample_rate * duration
    
    with wave.open(filename, "w") as wav_file:
        wav_file.setnchannels(num_channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        # Write zero bytes
        wav_file.writeframes(b"\x00" * num_frames * num_channels * sample_width)
    
    print(f"✅ Created {filename}")

if __name__ == "__main__":
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silence.wav")
    create_silence_wav(output_path)
