#!/usr/bin/env python3
"""
Generate premium English voiceover using Google Cloud TTS (Neural2 male voice).
"""
import subprocess, os, json, struct

DIR = os.path.dirname(os.path.abspath(__file__))
TMP = "/tmp/voiceover_premium"
os.makedirs(TMP, exist_ok=True)

# Voiceover segments: (start_second, text)
SEGMENTS = [
    (1,  "Meet the GolfStatus Browser Agent. An AI that books tee times for you, automatically."),
    (9,  "First, define a Skill. A simple DSL describes every step the agent should take. Thirty-one steps to navigate a real booking portal."),
    (21, "Set success conditions, enable sandbox mode for safe testing, and choose how the agent is triggered."),
    (33, "Users configure their agent with personal booking details. Date, tee time, playing partners. All stored securely."),
    (45, "One tap on Start, and the agent begins its work. No coding required."),
    (55, "Watch as the agent navigates the booking portal in real time. It uses Gemini Vision to understand each page and decide the next action."),
    (68, "Every step is logged live. Administrators can see exactly what the agent does."),
    (78, "Step thirty. Booking confirmed. Login, calendar, time selection, player registration. All done autonomously."),
    (88, "Screenshots prove the result. The real booking portal confirms: reservation completed, email sent."),
    (101, "Here's the magic. Switch from manual to scheduled execution. Pick a weekday, set the time. The agent books your tee time the moment the reservation window opens. Every week, automatically."),
    (119, "The agent is now scheduled and ready to go."),
    (124, "GolfStatus Browser Agent. Built with Gemini Vision, Playwright, and Cloud Run. By a seventy-year-old developer with fifty years of experience."),
]


def generate_gcloud_tts(text, output_path):
    """Use Google Cloud TTS API via gcloud for premium neural voice."""
    import google.auth
    import google.auth.transport.requests
    import urllib.request

    creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    url = "https://texttospeech.googleapis.com/v1/text:synthesize"
    body = json.dumps({
        "input": {"text": text},
        "voice": {
            "languageCode": "en-US",
            "name": "en-US-Neural2-J",  # Male, deep, professional
        },
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": 1.05,
            "pitch": -1.0,
            "volumeGainDb": 2.0,
        }
    }).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {creds.token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-goog-user-project", "golfstatus-a8d6c")

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())

    import base64
    audio_bytes = base64.b64decode(result["audioContent"])
    with open(output_path, "wb") as f:
        f.write(audio_bytes)
    return True


def main():
    print("🎙️  Premium English Voiceover (Google Neural2 Male)")
    print("=" * 55)

    audio_files = []
    for i, (start, text) in enumerate(SEGMENTS):
        fname = f"{TMP}/vo_{i:02d}.mp3"
        short = text[:55] + "..." if len(text) > 55 else text
        print(f"   🗣️  [{i+1:2d}] @ {start:3d}s: \"{short}\"")
        try:
            generate_gcloud_tts(text, fname)
        except Exception as e:
            print(f"      ❌ GCloud TTS failed: {e}")
            print(f"      ⏭️  Falling back to macOS TTS")
            aiff = fname.replace('.mp3', '.aiff')
            subprocess.run(['say', '-v', 'Daniel', '-r', '185', '-o', aiff, text],
                          capture_output=True)
            subprocess.run(['ffmpeg', '-y', '-i', aiff, '-codec:a', 'libmp3lame',
                          '-b:a', '192k', fname], capture_output=True)
            if os.path.exists(aiff): os.remove(aiff)
        audio_files.append((start, fname))

    # Create 130s silent track
    print("\n🔧 Mixing audio...")
    silent = f"{TMP}/silent.mp3"
    subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i',
                   'anullsrc=r=44100:cl=stereo', '-t', '130',
                   '-codec:a', 'libmp3lame', '-b:a', '128k', silent],
                  capture_output=True)

    # Position each segment at correct timestamp
    inputs = ['-i', silent]
    parts = []
    for i, (start, fname) in enumerate(audio_files):
        inputs.extend(['-i', fname])
        parts.append(f"[{i+1}:a]adelay={start*1000}|{start*1000},volume=1.0[a{i}]")

    mix = ''.join(f'[a{i}]' for i in range(len(audio_files)))
    parts.append(f"[0:a]{mix}amix=inputs={len(audio_files)+1}:duration=first:dropout_transition=0[out]")

    vo_out = f"{DIR}/voiceover.mp3"
    subprocess.run(['ffmpeg', '-y'] + inputs +
                  ['-filter_complex', ';'.join(parts),
                   '-map', '[out]', '-codec:a', 'libmp3lame', '-b:a', '192k', vo_out],
                  capture_output=True)

    # Merge with video
    print("📹 Merging with video...")
    video_in = f"{DIR}/demo_final.mp4"
    video_out = f"{DIR}/demo_final_vo.mp4"

    subprocess.run(['ffmpeg', '-y',
                   '-i', video_in, '-i', vo_out,
                   '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                   '-map', '0:v', '-map', '1:a', '-shortest',
                   '-movflags', '+faststart', video_out],
                  capture_output=True)

    size = os.path.getsize(video_out) / (1024*1024)
    print(f"\n✅ Done!")
    print(f"   📹 {video_out} ({size:.0f} MB)")
    print(f"   🎙️  Voice: en-US-Neural2-J (male, professional)")


if __name__ == "__main__":
    main()
