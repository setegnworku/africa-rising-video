# Africa Rising: Climate-Smart Cattle — Narrated Video Pipeline

**Automated pipeline to turn PowerPoint slides + a narration script into a fully narrated MP4 video.**

Developed for the presentation *"Climate-Smart Cattle: Africa Rising Together"*
by **Dr. Setegn Worku Alemu** (on behalf of Prof. Raphael Mrode) | ILRI / CGIAR
Global Methane Genetics Initiative — Africa Chapter

---

## What it does

1. Reads slide images (`slide1.png` … `slide8.png`) from the working directory
2. Parses `script.txt` into per-slide narration text
3. Generates MP3 audio for each slide via a text-to-speech API
4. Combines each slide image + audio into an MP4 segment using **ffmpeg**
5. Concatenates all segments and applies fade-in / fade-out
6. Outputs a single `final_video.mp4` at 1920×1080, H.264 Main Profile, stereo AAC

---

## Repository structure

```
├── create_video.py          # Main pipeline script
├── script.txt               # Narration script (SLIDE N markers)
├── Africa_Rising_Speech.md  # Full speech with segment timings and image prompts
├── slides/
│   ├── Slide1.JPG           # Slide images (export from PowerPoint)
│   ├── Slide2.JPG
│   └── ...
└── README.md
```

---

## Requirements

```bash
pip install -r requirements.txt
sudo apt install ffmpeg        # Linux
# macOS: brew install ffmpeg
# Windows: https://ffmpeg.org/download.html
```

---

## Usage

**1. Export your PowerPoint slides as PNG images** named `slide1.png`, `slide2.png`, etc. and place them in the same folder as `create_video.py`.

**2. Set your TTS API key** (see `create_video.py` for the variable name):
```bash
export TTS_API_KEY="your-key-here"
```

**3. Run the pipeline:**
```bash
python3 create_video.py
```

Output: `final_video.mp4` in the same directory.

---

## Script format (`script.txt`)

```
===================================
SLIDE 1 — Your Slide Title
===================================
Your narration text for slide 1...

===================================
SLIDE 2 — Next Slide Title
===================================
Your narration text for slide 2...

END OF SCRIPT
```

---

## Configuration

Edit the constants at the top of `create_video.py` to adjust:

| Setting | Default | Description |
|---|---|---|
| `VOICE_ID` | Adam (deep, warm) | Voice ID |
| `MODEL_ID` | eleven_multilingual_v2 | Supports African languages |
| `TARGET_WIDTH/HEIGHT` | 1920×1080 | Output resolution |
| `VIDEO_FPS` | 24 | Frames per second |
| `SILENCE_PADDING` | 0.5s | Pause between slides |
| `FADE_DURATION` | 0.5s | Fade-in / fade-out |
| `SKIP_EXISTING_AUDIO` | True | Reuse cached MP3s (saves API credits) |

---

## About the project

This video was created for the **BGP3 Workshop**, South Africa, 2026.
The pipeline supports any research presentation — just swap in your own slides and script.

The **Global Methane Genetics Initiative — Africa Chapter** aims to:
- Phenotype >4,000 African cattle for methane emissions
- Genotype >3,000 animals
- Collect >1,000 rumen microbiome samples
- Build climate-smart breeding programs across Kenya, Ethiopia, Burkina Faso, Benin, and South Africa

Learn more: [ILRI](https://www.ilri.org) | [Global Methane Hub](https://globalmethanehub.org)

---

## License

MIT — free to use and adapt for your own research presentations.
