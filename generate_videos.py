#!/usr/bin/env python3
"""
Generate Intro and Outro videos using Veo 3.1 via Gemini Developer API.
Uses GEMINI_API_KEY (not Vertex AI) because video download requires the Developer client.

Usage:
  export GEMINI_API_KEY="your-api-key"
  python3 generate_videos.py
"""
import os, time
from google import genai
from google.genai import types

API_KEY = os.environ.get("GEMINI_API_KEY", "")
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__)) or "."
MODEL = "veo-3.1-generate-preview"

if not API_KEY:
    print("❌ Set GEMINI_API_KEY environment variable first.")
    print("   Get one at: https://aistudio.google.com/apikey")
    exit(1)

client = genai.Client(api_key=API_KEY)


def generate_video(prompt: str, filename: str):
    """Generate a video and save it."""
    print(f"\n🎬 Generating: {filename}")
    print(f"   Prompt: {prompt[:80]}...")

    op = client.models.generate_videos(
        model=MODEL,
        prompt=prompt,
        config=types.GenerateVideosConfig(
            aspect_ratio="16:9",
            number_of_videos=1,
            duration_seconds=8,
        ),
    )

    print("   ⏳ Waiting for video generation...")
    while not op.done:
        print("   ⏳ Still generating...")
        time.sleep(15)
        op = client.operations.get(op)

    if not (op.result and op.result.generated_videos):
        print(f"   ❌ Failed: {op.error}")
        return None

    vid = op.result.generated_videos[0]
    vf = vid.video
    filepath = os.path.join(OUTPUT_DIR, filename)

    # Debug: show available info
    vname = getattr(vf, 'name', None) or getattr(vf, 'uri', None) or str(vf)
    vmime = getattr(vf, 'mime_type', 'unknown')
    print(f"   📎 Video: {vname} ({vmime})")

    # Download via Gemini Developer API
    try:
        video_bytes = client.files.download(file=vf)
        with open(filepath, 'wb') as f:
            f.write(video_bytes)
    except Exception as e:
        # Fallback: try with name string
        print(f"   ⚠️  Direct download failed ({e}), trying name-based...")
        if vname:
            video_bytes = client.files.download(file=vname)
            with open(filepath, 'wb') as f:
                f.write(video_bytes)
        else:
            print(f"   ❌ No download method available")
            return None

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    print(f"   ✅ Saved: {filepath} ({size_mb:.1f} MB)")
    return filepath


def main():
    print("=" * 60)
    print("🎬 Veo 3.1 Video Generator (Gemini Developer API)")
    print("=" * 60)

    intro = (
        "Cinematic aerial drone shot of a beautiful golf course at golden hour sunrise. "
        "Smooth slow camera gliding over lush green fairways with morning mist. "
        "Camera reveals a futuristic holographic AI interface overlay floating above "
        "the landscape showing automated browser clicks. Tech-meets-nature. "
        "Soft orchestral background music. 4K quality, photorealistic."
    )
    outro = (
        "Cinematic split-screen: left side peaceful golf course with golfer at sunset, "
        "right side elegant code and neural network visualizations in blue-green tones. "
        "Two halves smoothly merge into one harmonious image. "
        "Clean minimal professional ending. Soft ambient music. 4K."
    )

    r1 = generate_video(intro, "intro_video.mp4")
    r2 = generate_video(outro, "outro_video.mp4")

    print(f"\n📊 Results:")
    print(f"   Intro: {'✅ ' + r1 if r1 else '❌ Failed'}")
    print(f"   Outro: {'✅ ' + r2 if r2 else '❌ Failed'}")


if __name__ == "__main__":
    main()
