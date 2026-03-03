#!/usr/bin/env python3
"""
Generate Intro and Outro videos for the Gemini Live Agent Challenge demo
using Veo 3.1 via the Google GenAI SDK.

Usage:
  pip install google-genai
  export PROJECT_ID="golfstatus-a8d6c"
  python3 generate_videos.py
"""

import os
import time
import base64
from google import genai
from google.genai import types

PROJECT_ID = os.environ.get("PROJECT_ID", "golfstatus-a8d6c")
LOCATION = "us-central1"  # Veo models are available in us-central1
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__)) or "."

# Initialize client
client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION,
)

MODEL = "veo-3.1-generate-001"


def generate_video(prompt: str, filename: str, aspect_ratio: str = "16:9"):
    """Generate a video from a text prompt using Veo 3.1."""
    print(f"\n🎬 Generating: {filename}")
    print(f"   Prompt: {prompt[:80]}...")
    print(f"   Aspect Ratio: {aspect_ratio}")
    print(f"   Model: {MODEL}")
    print()

    operation = client.models.generate_videos(
        model=MODEL,
        prompt=prompt,
        config=types.GenerateVideosConfig(
            aspect_ratio=aspect_ratio,
            number_of_videos=1,
            duration_seconds=8,
            enhance_prompt=True,
            generate_audio=True,
        ),
    )

    # Poll until done
    print("   ⏳ Waiting for video generation...")
    while not operation.done:
        print("   ⏳ Still generating...")
        time.sleep(15)
        operation = client.operations.get(operation)

    if operation.result and operation.result.generated_videos:
        video = operation.result.generated_videos[0]
        filepath = os.path.join(OUTPUT_DIR, filename)

        # Get the video file reference
        video_file = video.video
        print(f"   📎 Video file: {video_file}")

        # Try to get download URI
        download_url = None
        if hasattr(video_file, 'download_uri') and video_file.download_uri:
            download_url = video_file.download_uri
        elif hasattr(video_file, 'uri') and video_file.uri:
            download_url = video_file.uri

        if download_url:
            print(f"   📥 Downloading from: {download_url[:80]}...")
            # Use authenticated request
            import google.auth
            import google.auth.transport.requests
            creds, _ = google.auth.default()
            auth_req = google.auth.transport.requests.Request()
            creds.refresh(auth_req)

            import urllib.request
            req = urllib.request.Request(download_url)
            req.add_header('Authorization', f'Bearer {creds.token}')
            with urllib.request.urlopen(req) as response:
                video_data = response.read()
            with open(filepath, 'wb') as f:
                f.write(video_data)
        else:
            print(f"   ⚠️  No download URL found. Dumping video_file attrs:")
            for attr in dir(video_file):
                if not attr.startswith('_'):
                    print(f"       {attr} = {getattr(video_file, attr, '?')}")
            return None

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"   ✅ Saved: {filepath} ({size_mb:.1f} MB)")
        return filepath
    else:
        print(f"   ❌ Failed to generate video")
        if operation.error:
            print(f"   Error: {operation.error}")
        return None


def main():
    print("=" * 60)
    print("🎬 Veo 3.1 Video Generator")
    print(f"   Project: {PROJECT_ID}")
    print(f"   Location: {LOCATION}")
    print(f"   Output: {OUTPUT_DIR}")
    print("=" * 60)

    # ── INTRO VIDEO ──────────────────────────────────────────────
    intro_prompt = (
        "Cinematic aerial drone shot of a beautiful golf course at golden hour sunrise. "
        "Smooth, slow camera movement gliding over lush green fairways with morning mist. "
        "The camera slowly reveals a futuristic, transparent holographic AI interface "
        "overlay floating above the landscape, showing a browser window with automated "
        "clicks happening. Clean, professional tech-meets-nature aesthetic. "
        "Soft orchestral background music. 4K quality, photorealistic."
    )

    # ── OUTRO VIDEO ──────────────────────────────────────────────
    outro_prompt = (
        "A cinematic split-screen transition: on the left side, a peaceful golf course "
        "with a golfer teeing off at sunset. On the right side, elegant flowing lines "
        "of code and neural network visualizations in blue and green tones. "
        "The two halves smoothly merge together into one harmonious image. "
        "Clean, minimal, professional ending. Soft ambient music. 4K quality."
    )

    results = []
    results.append(generate_video(intro_prompt, "intro_video.mp4"))
    results.append(generate_video(outro_prompt, "outro_video.mp4"))

    print("\n" + "=" * 60)
    print("📊 Results:")
    for r in results:
        status = "✅" if r else "❌"
        print(f"   {status} {r or 'Failed'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
