#!/bin/bash
# Demo Video v4 - Floating screens background + trimmed end + QuickTime compatible
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC1="${DIR}/screen_recorder1.mp4"
SRC2="${DIR}/screen_recorder2.mp4"
BG="${DIR}/background_floating.mp4"   # NEW: floating screens background
INTRO="${DIR}/intro_video.mp4"
OUTRO="${DIR}/outro_video.mp4"
OUTPUT="${DIR}/demo_final.mp4"
OUTPUT_QT="${DIR}/demo_final_qt.mp4"
TMP="/tmp/demo_v4"

mkdir -p "$TMP"
echo "🎬 Demo Video v4 (Floating Screens + Trimmed)"
echo ""

# ── Extract & re-encode segments ──────────────────────────────────────────
echo "✂️  Extracting segments..."
# Recording 1
ffmpeg -y -ss 0 -i "$SRC1" -t 12 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s1.mp4" 2>/dev/null
ffmpeg -y -ss 28 -i "$SRC1" -t 12 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s2.mp4" 2>/dev/null
ffmpeg -y -ss 85 -i "$SRC1" -t 12 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s3.mp4" 2>/dev/null
ffmpeg -y -ss 145 -i "$SRC1" -t 10 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s4.mp4" 2>/dev/null
ffmpeg -y -ss 175 -i "$SRC1" -t 13 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s5.mp4" 2>/dev/null
ffmpeg -y -ss 205 -i "$SRC1" -t 10 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s6.mp4" 2>/dev/null
ffmpeg -y -ss 265 -i "$SRC1" -t 10 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s7.mp4" 2>/dev/null
# Segment 8: trimmed to 13s (was 15s) to cut screencapture stop notification
ffmpeg -y -ss 295 -i "$SRC1" -t 13 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s8.mp4" 2>/dev/null
# Recording 2 (scheduler)
ffmpeg -y -ss 8 -i "$SRC2" -t 18 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s9.mp4" 2>/dev/null
# Segment 10: trimmed to 5s (was 7s) to avoid screencapture stop
ffmpeg -y -ss 28 -i "$SRC2" -t 5 -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -vf "scale=720:1560:force_original_aspect_ratio=decrease,pad=720:1560:(ow-iw)/2:(oh-ih)/2" -r 30 "$TMP/s10.mp4" 2>/dev/null
echo "   ✅ 10 segments"

# ── Concatenate ────────────────────────────────────────────────────────────
echo "🔗 Concatenating..."
for i in $(seq 1 10); do echo "file 's${i}.mp4'"; done > "$TMP/list.txt"
ffmpeg -y -f concat -safe 0 -i "$TMP/list.txt" -c copy "$TMP/screen.mp4" 2>/dev/null
SDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/screen.mp4")
echo "   ✅ Screen: ${SDUR}s"

# ── Loop floating-screens background ──────────────────────────────────────
echo "🔄 Looping floating-screens background..."
ffmpeg -y -stream_loop -1 -i "$BG" -t "$SDUR" \
    -vf "scale=1280:720" -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -r 30 \
    "$TMP/bg.mp4" 2>/dev/null

# ── Composite: phone over floating screens ────────────────────────────────
echo "📱 Compositing phone on floating-screens..."
ffmpeg -y \
    -i "$TMP/bg.mp4" \
    -i "$TMP/screen.mp4" \
    -filter_complex "
        [1:v]scale=-1:600,
             pad=iw+20:ih+20:10:10:color=0x000000AA,
             fade=t=in:st=0:d=2:alpha=1,
             fade=t=out:st=$(echo "$SDUR - 1.5" | bc):d=1.5:alpha=1
             [phone];
        [0:v][phone]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p[out]
    " \
    -map "[out]" \
    -c:v libx264 -preset fast -crf 22 -pix_fmt yuv420p -r 30 \
    -t "$SDUR" \
    "$TMP/comp.mp4" 2>/dev/null
echo "   ✅ Composite done"

# ── Final assembly ────────────────────────────────────────────────────────
echo "🎬 Final assembly..."
IDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$INTRO")
CDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/comp.mp4")
ODUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTRO")

ffmpeg -y \
    -i "$INTRO" -i "$TMP/comp.mp4" -i "$OUTRO" \
    -filter_complex "
        [0:v]scale=1280:720,setsar=1,fps=30,format=yuv420p[v0];
        [1:v]scale=1280:720,setsar=1,fps=30,format=yuv420p[v1];
        [2:v]scale=1280:720,setsar=1,fps=30,format=yuv420p[v2];
        [v0][v1]xfade=transition=smoothleft:duration=1:offset=$(echo "$IDUR - 1" | bc)[v01];
        [v01][v2]xfade=transition=smoothright:duration=1:offset=$(echo "$IDUR + $CDUR - 2" | bc)[vout]
    " \
    -map "[vout]" \
    -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -movflags +faststart \
    "$OUTPUT" 2>/dev/null

cp "$OUTPUT" "$OUTPUT_QT"

FDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTPUT")
FSIZE=$(du -h "$OUTPUT" | cut -f1)

echo ""
echo "════════════════════════════════════════════"
echo "✅ Demo v4 ready!"
echo "   📹 ${OUTPUT##*/}  |  ⏱️ ${FDUR}s ($(echo "scale=1; $FDUR / 60" | bc) min)  |  📦 $FSIZE"
echo "   ✨ Floating screens BG + fade-in phone + smooth transitions"
echo "   ✨ Screencapture notification trimmed"
echo "   ✨ QuickTime compatible"
echo "════════════════════════════════════════════"

rm -rf "$TMP"
