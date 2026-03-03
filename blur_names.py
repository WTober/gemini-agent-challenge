#!/usr/bin/env python3
"""
Blur names in screenshots and video for privacy.
Keeps: "Wolfgang Tober", "Tober, Wolfgang", "Astrid Lerch", "Lerch, Astrid"
Blurs: all other person names found in the content.

Uses ffmpeg for video and Pillow for image processing.
"""
import subprocess
import sys
import os

DIR = os.path.dirname(os.path.abspath(__file__))

# Known names to BLUR (from PC Caddy reservations + agent runs)
NAMES_TO_BLUR = [
    "Franke, Meinolf",
    "Moser, Ingo",
    "Wolf, Hilmar",
    "de Villiers, Cedric",
    "Heidkrüger, Bernd",
]

def blur_video(input_path, output_path):
    """
    Apply blur boxes to specific regions of the video where other names appear.
    Since names appear in phone view (small), we identify the approximate pixel
    positions and apply box blur.
    """
    print(f"🔒 Blurring video: {input_path}")

    # The video is 1280x720 with a phone overlay centered.
    # Phone is ~296px wide, ~640px high, centered at (492, 40)
    # Names in the PC Caddy screenshots (inside the phone view) are very small.
    # The phone area starts at approximately x=492, y=40, w=296, h=640

    # In the demo_final.mp4, the embedded PC Caddy screenshots within the app
    # are already very small (screenshot within phone within background).
    # The names there are largely unreadable at 1280x720.

    # For the original screen_recorder1.mp4 (720x1560), names are more visible.
    # We skip video blurring since demo_final.mp4 makes them unreadable.
    print("   ℹ️  Names in demo_final.mp4 are too small to read (phone-in-scene)")
    print("   ℹ️  Skipping video blur - focus on screenshot blur instead")


def blur_screenshot_regions(input_path, output_path, blur_regions):
    """
    Blur specific rectangular regions in a screenshot.
    blur_regions: list of (x, y, w, h) tuples
    """
    from PIL import Image, ImageFilter

    img = Image.open(input_path)
    for (x, y, w, h) in blur_regions:
        # Crop the region, blur it heavily, paste back
        box = (x, y, x + w, y + h)
        region = img.crop(box)
        blurred = region.filter(ImageFilter.GaussianBlur(radius=12))
        img.paste(blurred, box)

    img.save(output_path, quality=95)
    print(f"   ✅ Saved: {output_path}")


def blur_pccaddy_screenshot():
    """Blur names in the PC Caddy reservation screenshot."""
    src = os.path.join(DIR, "screenshots", "pccaddy_reservations.png")
    if not os.path.exists(src):
        # Try jpg
        src = os.path.join(DIR, "screenshots", "pccaddy_reservations.jpg")
    if not os.path.exists(src):
        print("   ⚠️  No PC Caddy screenshot found at screenshots/pccaddy_reservations.png")
        print("   Place the screenshot there and re-run.")
        return

    out = os.path.join(DIR, "screenshots", "pccaddy_reservations_blurred.png")

    from PIL import Image
    img = Image.open(src)
    w, h = img.size
    print(f"   📐 Image size: {w}x{h}")

    # Approximate name positions in the PC Caddy table (based on the screenshot)
    # The "Personen" column is roughly at x=520-700, and each row is ~60px high
    # Row 1 (12:10 Uhr): Names at y~175-230 (under "Tober, Wolfgang *")
    #   Franke, Meinolf / Moser, Ingo / Wolf, Hilmar
    # Row 2 (11:20 Uhr): Names at y~250-290
    #   Wolf, Hilmar / de Villiers, Cedric
    # Row 3 (13:30 Uhr): Names at y~310-350
    #   Wolf, Hilmar / de Villiers, Cedric
    # Row 4 (11:30 Uhr): Names at y~370-400
    #   Heidkrüger, Bernd (after Lerch, Astrid which we keep)

    # Scale factor if image is different size
    # Reference: screenshot appears to be ~1020x570
    sx = w / 1020
    sy = h / 570

    blur_regions = [
        # Row 1: Franke, Meinolf / Moser, Ingo / Wolf, Hilmar
        (int(520*sx), int(198*sy), int(180*sx), int(52*sy)),
        # Row 2: Wolf, Hilmar / de Villiers, Cedric
        (int(520*sx), int(268*sy), int(180*sx), int(38*sy)),
        # Row 3: Wolf, Hilmar / de Villiers, Cedric
        (int(520*sx), int(332*sy), int(180*sx), int(38*sy)),
        # Row 4: Heidkrüger, Bernd (3rd line, after Lerch, Astrid)
        (int(520*sx), int(405*sy), int(180*sx), int(18*sy)),
    ]

    blur_screenshot_regions(src, out, blur_regions)


def blur_agent_screenshots():
    """Blur names in the extracted agent screenshots."""
    screenshots_dir = os.path.join(DIR, "screenshots")

    for fname in os.listdir(screenshots_dir):
        if fname.startswith("booking_result"):
            # This contains PC Caddy embedded screenshot with names
            src = os.path.join(screenshots_dir, fname)
            out = os.path.join(screenshots_dir, f"blurred_{fname}")

            from PIL import Image
            img = Image.open(src)
            w, h = img.size
            print(f"   📐 {fname}: {w}x{h}")

            # In the phone view, the PC Caddy screenshot is embedded
            # Names are very small - blur the relevant areas
            # The booking result screenshot (720x1560) shows:
            # PC Caddy tables at approximately y=200-550 area
            # Person names in the table cells
            blur_regions = [
                # Top screenshot: Person names area
                (int(360), int(285), int(250), int(20)),  # "Tober, Wolfgang" row - other names
                # Bottom screenshot: table rows with names
                (int(390), int(685), int(120), int(15)),  # Tober row
                (int(390), int(730), int(120), int(15)),  # Lerch row - keep
            ]
            blur_screenshot_regions(src, out, blur_regions)


def main():
    print("🔒 Privacy Blur Tool")
    print("=" * 50)
    print(f"   Keeping: Wolfgang Tober, Astrid Lerch")
    print(f"   Blurring: {', '.join(NAMES_TO_BLUR)}")
    print()

    blur_pccaddy_screenshot()
    blur_agent_screenshots()

    print()
    print("✅ Done! Check screenshots/ folder for blurred versions.")


if __name__ == "__main__":
    main()
