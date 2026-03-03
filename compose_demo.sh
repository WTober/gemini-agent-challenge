#!/bin/bash
# Compose the final demo video from screen_recorder1.mp4 + Veo assets
# Extracts the best segments, adds them to background, crossfades with intro/outro
#
# Timeline of screen_recorder1.mp4 (5:19):
# 0:00 - Skill Definition (DSL Steps, 31 Schritte)
# 0:30 - Erfolgsbedingung, Sandbox, Trigger-Methoden
# 1:00 - Agent bearbeiten (Eingaben)
# 1:30 - Agent bearbeiten (komplett)
# 2:00 - Datumseingabe
# 2:30 - Agent-Liste + Start-Button
# 3:00 - Agent läuft (Übersicht, User-Sicht, Igel)
# 3:30 - Admin-Detail (Live Steps, Schritt 11-13)
# 4:00 - Admin-Detail (Screenshot + Steps 24-25)
# 4:30 - Schritt 29-30 Buchung erfolgreich
# 5:00 - Ergebnis: Screenshots + "28 von 31 echt ausgeführt"
# 5:19 - Ende

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="${DIR}/screen_recorder1.mp4"
BG="${DIR}/background_loop.mp4"
INTRO="${DIR}/intro_video.mp4"
OUTRO="${DIR}/outro_video.mp4"
OUTPUT="${DIR}/demo_final.mp4"
TMP="/tmp/demo_compose"

mkdir -p "$TMP"

echo "🎬 Compositing Demo Video"
echo "   Source: $SRC (5:19)"
echo ""

# ── Segment extraction (best moments, speed up transitions) ────────────────
echo "✂️  Extracting segments..."

# Segment 1: Skill Definition - DSL Steps (0:00 → 0:15, show the skill editor)
ffmpeg -y -ss 0 -i "$SRC" -t 12 -c:v libx264 -preset fast -crf 23 "$TMP/seg1.mp4" 2>/dev/null
echo "   ✅ Seg 1: Skill Editor (12s)"

# Segment 2: Erfolgsbedingung + Sandbox + Trigger (0:28 → 0:45)
ffmpeg -y -ss 28 -i "$SRC" -t 12 -c:v libx264 -preset fast -crf 23 "$TMP/seg2.mp4" 2>/dev/null
echo "   ✅ Seg 2: Sandbox + Trigger (12s)"

# Segment 3: Agent Config - Eingaben sichtbar (1:25 → 1:40)
ffmpeg -y -ss 85 -i "$SRC" -t 12 -c:v libx264 -preset fast -crf 23 "$TMP/seg3.mp4" 2>/dev/null
echo "   ✅ Seg 3: Agent Config (12s)"

# Segment 4: Agent-Liste mit Start-Button (2:25 → 2:40)
ffmpeg -y -ss 145 -i "$SRC" -t 12 -c:v libx264 -preset fast -crf 23 "$TMP/seg4.mp4" 2>/dev/null
echo "   ✅ Seg 4: Agent-Liste + Start (12s)"

# Segment 5: Agent läuft (User view, Igel) (2:55 → 3:10)
ffmpeg -y -ss 175 -i "$SRC" -t 15 -c:v libx264 -preset fast -crf 23 "$TMP/seg5.mp4" 2>/dev/null
echo "   ✅ Seg 5: Agent läuft (15s)"

# Segment 6: Admin-Detail Steps live (3:25 → 3:40)
ffmpeg -y -ss 205 -i "$SRC" -t 12 -c:v libx264 -preset fast -crf 23 "$TMP/seg6.mp4" 2>/dev/null
echo "   ✅ Seg 6: Admin-Detail Live (12s)"

# Segment 7: Buchung erfolgreich Schritt 30 (4:25 → 4:40)
ffmpeg -y -ss 265 -i "$SRC" -t 12 -c:v libx264 -preset fast -crf 23 "$TMP/seg7.mp4" 2>/dev/null
echo "   ✅ Seg 7: Buchung erfolgreich (12s)"

# Segment 8: Ergebnis Screenshots + Sandbox-Ergebnis (4:55 → 5:15)
ffmpeg -y -ss 295 -i "$SRC" -t 18 -c:v libx264 -preset fast -crf 23 "$TMP/seg8.mp4" 2>/dev/null
echo "   ✅ Seg 8: Ergebnis (18s)"

# ── Concatenate all screen segments ────────────────────────────────────────
echo ""
echo "🔗 Concatenating screen segments..."

cat > "$TMP/segments.txt" << EOF
file 'seg1.mp4'
file 'seg2.mp4'
file 'seg3.mp4'
file 'seg4.mp4'
file 'seg5.mp4'
file 'seg6.mp4'
file 'seg7.mp4'
file 'seg8.mp4'
EOF

ffmpeg -y -f concat -safe 0 -i "$TMP/segments.txt" \
    -c:v libx264 -preset fast -crf 23 "$TMP/screen_cut.mp4" 2>/dev/null

SCREEN_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/screen_cut.mp4")
echo "   ✅ Total screen: ${SCREEN_DUR}s"

# ── Loop background to match screen duration ───────────────────────────────
echo "🔄 Looping background..."
ffmpeg -y -stream_loop -1 -i "$BG" -t "$SCREEN_DUR" \
    -c:v libx264 -preset fast -crf 23 \
    -vf "scale=1280:720" \
    "$TMP/bg_looped.mp4" 2>/dev/null

# ── Composite: phone on background ────────────────────────────────────────
echo "📱 Compositing phone-on-background..."
ffmpeg -y \
    -i "$TMP/bg_looped.mp4" \
    -i "$TMP/screen_cut.mp4" \
    -filter_complex "
        [0:v]scale=1280:720[bg];
        [1:v]scale=-1:640,
             pad=iw+20:ih+20:10:10:color=black@0.5[phone];
        [bg][phone]overlay=(W-w)/2:(H-h)/2[out]
    " \
    -map "[out]" \
    -c:v libx264 -preset fast -crf 22 \
    -t "$SCREEN_DUR" \
    "$TMP/composite.mp4" 2>/dev/null
echo "   ✅ Composite created"

# ── Final: Intro + Composite + Outro with crossfades ──────────────────────
echo "🎬 Final assembly with crossfades..."

INTRO_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$INTRO")
COMP_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/composite.mp4")
OUTRO_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTRO")
FADE=1

ffmpeg -y \
    -i "$INTRO" \
    -i "$TMP/composite.mp4" \
    -i "$OUTRO" \
    -filter_complex "
        [0:v]scale=1280:720,setsar=1,fps=30[v0];
        [1:v]scale=1280:720,setsar=1,fps=30[v1];
        [2:v]scale=1280:720,setsar=1,fps=30[v2];
        [v0][v1]xfade=transition=fade:duration=${FADE}:offset=$(echo "$INTRO_DUR - $FADE" | bc)[v01];
        [v01][v2]xfade=transition=fade:duration=${FADE}:offset=$(echo "$INTRO_DUR + $COMP_DUR - 2*$FADE" | bc)[vout]
    " \
    -map "[vout]" \
    -c:v libx264 -preset medium -crf 20 \
    "$OUTPUT" 2>/dev/null

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
FINAL_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTPUT")
FINAL_SIZE=$(du -h "$OUTPUT" | cut -f1)

echo "════════════════════════════════════════════════"
echo "✅ Demo video created!"
echo "   📹 Output:   $OUTPUT"
echo "   ⏱️  Duration: ${FINAL_DUR}s ($(echo "scale=1; $FINAL_DUR / 60" | bc) min)"
echo "   📦 Size:     $FINAL_SIZE"
echo ""
echo "   Breakdown:"
echo "   🎬 Intro:     ${INTRO_DUR}s"
echo "   📱 Demo:      ${COMP_DUR}s (8 segments from 5:19 original)"
echo "   🎬 Outro:     ${OUTRO_DUR}s"
echo "════════════════════════════════════════════════"

# Cleanup
rm -rf "$TMP"
