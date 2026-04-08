---
name: video-processor
description: Process video files for YouTube content creation and generate social media graphics - transcribe videos/clips into timestamped transcripts with SRT subtitles and AI-generated chapters, generate AI-powered YouTube thumbnails with face references, create LinkedIn post infographics, or generate custom images for any platform with flexible dimensions. Use when the user wants to transcribe video clips, create chapters, generate thumbnails, create LinkedIn graphics, or generate images for social media. Triggers on requests like "transcribe this video", "create chapters for...", "generate a thumbnail for...", "create a LinkedIn infographic", "generate an Instagram post", or any video transcription/image generation tasks.
---

# Video Processor

Process video files for YouTube content creation and generate social media graphics. This skill provides four main capabilities:

1. **Transcription & Chapters**: Convert video clips into timestamped transcripts, SRT subtitles, and AI-generated YouTube chapters
2. **YouTube Thumbnail Generation**: Create AI-powered YouTube thumbnails with face references and style matching
3. **LinkedIn Post Images**: Generate professional LinkedIn infographics and post images
4. **Generic Image Generation**: Create custom images for any platform with flexible dimensions and prompts

## When to Use This Skill

Use this skill when the user:
- Wants to transcribe video files or clips
- Needs to generate YouTube chapters
- Wants to regenerate chapters from existing transcripts
- Wants to create YouTube thumbnails
- Wants to create LinkedIn post images or infographics
- Wants to generate images for other platforms (Instagram, Twitter, Facebook, etc.)
- Mentions face swapping for thumbnails
- Asks about providing face or style references for graphics

## Choosing the Right Image Generation Tool

The skill provides three image generation scripts. Choose based on platform and requirements:

**Use `thumbnail.py` (YouTube thumbnails) when:**
- User mentions "YouTube", "thumbnail", or "video thumbnail"
- 16:9 aspect ratio requested
- Dramatic, eye-catching style with faces desired
- Default: 1920x1080

**Use `linkedin-post.py` (LinkedIn infographics) when:**
- User mentions "LinkedIn", "LinkedIn post", or "business infographic"
- Square format infographic requested
- Professional, dark theme, no faces
- Default: 1200x1200

**Use `image-gen.py` (generic) when:**
- Custom dimensions specified (Instagram, Twitter, Facebook, etc.)
- Platform is not YouTube or LinkedIn
- User explicitly requests flexible/custom tool
- User provides full prompt with specific style requirements
- Required parameter: `--dimensions WIDTHxHEIGHT`

**Priority:** Always prefer `thumbnail.py` or `linkedin-post.py` when the use case matches. Use `image-gen.py` as fallback for other platforms or custom requirements.

## Installation & Requirements

The video processor is located at `video-processor/` in the project repository.

**Requirements:**
- Python 3.9+
- [uv](https://docs.astral.sh/uv/) package manager
- FFmpeg (for transcription)
- OpenAI API key (for Whisper transcription)
- Gemini API key (for chapters and thumbnails)
- Replicate API token (optional, for face swap)

**Environment Setup:**
```bash
cd video-processor
uv sync
# Ensure .env exists with required API keys
```

## 1. Video Transcription & Chapters

### Basic Transcription Command

Transcribe video clips and generate transcript, subtitles, and chapters:

```bash
cd video-processor
uv run transcribe.py <folder_path>
```

**Example:**
```bash
uv run transcribe.py ./my-youtube-video/
```

### Input Format

Videos should be in a folder with numeric prefixes for ordering:

```
my-youtube-video/
├── 1.mp4          # Intro
├── 2.mp4          # Main content
├── 3.mov          # Demo
└── 4.mp4          # Outro
```

**Supported formats:** MP4, MOV, MKV, AVI, WEBM, M4V

### Output Files

The script generates three files in the same folder:

1. **transcript.md** - Markdown with timestamps
   ```markdown
   # Transcript

   [00:00] Welcome back to the channel...
   [00:15] Today we're going to talk about...
   ```

2. **transcript.srt** - SRT subtitles for video editors/YouTube
   ```
   1
   00:00:00,000 --> 00:00:15,500
   Welcome back to the channel...
   ```

3. **chapters.txt** - YouTube chapter suggestions (AI-generated)
   ```
   0:00 Introduction
   2:30 Setting Up
   5:45 Main Tutorial
   ```

### Features

- **Multi-clip support**: Processes numbered clips with automatic timestamp offset calculation
- **Long video support**: Automatically chunks videos longer than 20 minutes (API limit)
- **Timestamped transcript**: Markdown format with precise timestamps
- **AI chapters**: Gemini generates contextual chapter markers

### Regenerate Chapters Only

If you already have a transcript and want to regenerate chapters:

```bash
cd video-processor
uv run chapters.py <transcript.md or folder_path>
```

**Examples:**
```bash
# Point to transcript file directly
uv run chapters.py ./my-video/transcript.md

# Or point to folder containing transcript.md
uv run chapters.py ./my-video/
```

**Use cases:**
- Tweaking chapters without re-transcription
- Regenerating after editing the transcript
- Using a different Gemini model

### Important Notes

- Clips must be in a flat folder (no subdirectories)
- Videos are processed in numeric order by filename prefix
- Estimated cost: ~$0.006 per minute of audio (Whisper API)
- Long videos are automatically chunked to handle API limits
- Internet connection required

## 2. Thumbnail Generation

### Basic Thumbnail Command

Generate AI-powered YouTube thumbnails:

```bash
cd video-processor
uv run thumbnail.py "your thumbnail idea here"
```

**Default behavior:**
- Generates 3 variations
- Saves to: `./output/Thumbnails/[YYYY-MM-DD]/`
- Uses `assets/face.jpg` if it exists
- Output: 1920x1080 PNG files
- **Brand Guidelines**: See [references/brand-guidelines.md](references/brand-guidelines.md) for color palette, composition, and style best practices

### Thumbnail Parameters

#### Required
- **idea** (positional): Your thumbnail concept/idea

#### Face Reference Options
- `--face PATH`: Specify a face reference image (default: `assets/face.jpg`)
- `--no-face`: Don't include any face reference
- `--face-swap`: Post-process with Replicate to swap in real face (requires REPLICATE_API_TOKEN)

#### Style & References
- `--style PATH`: Style reference thumbnail - AI matches layout and aesthetic
- `--ref PATH`: Additional reference image (can use multiple times)

#### Logo & Output
- `--logo PATH`: Logo to composite onto thumbnail
- `--logo-position [bottom-right|bottom-left|top-right|top-left]`: Logo placement (default: bottom-right)
- `-o, --output PATH`: Output directory or file path
- `--variations N`: Number of variations to generate (default: 3)

### Thumbnail Examples

**Basic - 3 variations with default settings:**
```bash
uv run thumbnail.py "shocked reaction to mass layoffs at Google"
```

**With style reference:**
```bash
uv run thumbnail.py "my new video idea" --style ./existing-thumbnail.jpg --face ./me.jpg
```

**With face swap for exact likeness:**
```bash
uv run thumbnail.py "excited about new AI feature" --face ./me.jpg --face-swap
```

**Single variation for quick testing:**
```bash
uv run thumbnail.py "quick test idea" --variations 1
```

**Custom output directory:**
```bash
uv run thumbnail.py "my video concept" -o ./custom-folder/
```

**No face (scene only):**
```bash
uv run thumbnail.py "dramatic code on screen with error" --no-face
```

**With logo overlay:**
```bash
uv run thumbnail.py "my video concept" --logo ./assets/logo.png
```

### Face Reference Setup

For consistent thumbnails:

1. Add your headshot(s) to the `assets/` folder:
   ```
   video-processor/assets/
   ├── face.jpg           # Default face (auto-loaded)
   ├── face-excited.jpg   # Alternative expressions
   ├── face-shocked.jpg
   └── logo.png           # Channel logo
   ```

2. The script automatically uses `assets/face.jpg` if it exists
3. Override with `--face` for different expressions
4. Use `--no-face` to skip face reference entirely

### Style References

The `--style` flag matches existing thumbnail aesthetics:
- Overall layout and composition
- Color schemes and mood
- Text styling (font weight, colors, placement)
- Background style and effects

Perfect for maintaining consistent channel branding.

### Brand Guidelines & Best Practices

**IMPORTANT**: Before generating thumbnails, read [references/brand-guidelines.md](references/brand-guidelines.md) for:
- **Blue brand color usage** (as accent, not dominant)
- **Natural skin tone requirements** (no blue tint on face)
- **Balanced color palettes** (mix of vibrant colors)
- **Text highlighting patterns** (1-2 keywords in blue)
- **Composition guidelines** (layout, visual flow)
- **Proven examples** from past successful thumbnails

These guidelines ensure thumbnails maintain brand consistency while avoiding common pitfalls like over-saturation or unnatural lighting.

### Face Swap for Exact Likeness

AI-generated faces may not be exact. Use `--face-swap` for post-processing:

```bash
uv run thumbnail.py "my video idea" --face ./me.jpg --face-swap
```

**How it works:**
1. Generates thumbnail with AI (using face as reference for positioning)
2. Post-processes with Replicate to swap in your actual face

**Setup:**
- Get Replicate API token at https://replicate.com/account/api-tokens
- Add to `.env`: `REPLICATE_API_TOKEN=r8_your_token_here`
- Cost: ~$0.003 per face swap (~370 swaps per $1)

### Thumbnail Specifications

| Element | Value |
|---------|-------|
| **Resolution** | 1920x1080 (1080p) |
| **Aspect Ratio** | 16:9 |
| **Format** | PNG (or JPG with --output .jpg) |
| **Default Variations** | 3 |
| **Model** | Gemini 3 Pro (Nano Banana Pro) by default |

### Important Thumbnail Notes

- Gemini model supports reference images; Imagen does not
- Cost: ~$0.13 per image with Gemini 3 Pro
- Face swap adds ~$0.003 per thumbnail
- 3 thumbnails with face swap: ~$0.40 total
- Internet connection required

## 3. LinkedIn Post Image Generation

### Basic LinkedIn Image Command

Generate AI-powered LinkedIn post images and infographics:

```bash
cd video-processor
uv run linkedin-post.py "your post concept here"
```

**Default behavior:**
- Generates 3 variations
- Saves to: `./output/LinkedIn/[YYYY-MM-DD]/`
- Output: 1200x1200 PNG files (LinkedIn square format)
- Optimized for infographic layouts (no faces, white background, flat design)

### LinkedIn Image Parameters

#### Required
- **idea** (positional): Your LinkedIn post concept/idea

#### Style & References
- `--style PATH`: Style reference image - AI matches layout and aesthetic
- `--ref PATH`: Additional reference image (can use multiple times)

#### Logo & Output
- `--logo PATH`: Logo to composite onto image
- `--logo-position [bottom-right|bottom-left|top-right|top-left]`: Logo placement (default: bottom-right)
- `-o, --output PATH`: Output directory or file path
- `--variations N`: Number of variations to generate (default: 3)

### LinkedIn Image Examples

**Basic - 3 variations:**
```bash
uv run linkedin-post.py "AI Validation Pyramid with 5 layers showing human vs agent responsibilities"
```

**With style reference:**
```bash
uv run linkedin-post.py "my infographic concept" --style ./existing-infographic.jpg
```

**Single variation for testing:**
```bash
uv run linkedin-post.py "quick test" --variations 1
```

**Custom output directory:**
```bash
uv run linkedin-post.py "my concept" -o ./custom-folder/
```

**With logo overlay:**
```bash
uv run linkedin-post.py "my infographic" --logo ./assets/logo.png
```

### LinkedIn Image Specifications

| Element | Value |
|---------|-------|
| **Resolution** | 1200x1200 (LinkedIn square) |
| **Aspect Ratio** | 1:1 |
| **Format** | PNG (or JPG with --output .jpg) |
| **Default Variations** | 3 |
| **Model** | Gemini 3 Pro (Nano Banana Pro) by default |
| **Style** | Professional infographic, no faces, flat design |

### Important LinkedIn Image Notes

- Optimized for professional business infographics
- Dark theme with modern sleek aesthetic
- NO faces or people included (pure diagram/infographic style)
- Blue color theme with gradients
- Higher information density than YouTube thumbnails
- Cost: ~$0.13 per image with Gemini 3 Pro
- Internet connection required

## 4. Generic Image Generation

### Basic Generic Image Command

Generate AI-powered images with custom dimensions for any platform:

```bash
cd video-processor
uv run image-gen.py "your image concept" --dimensions WIDTHxHEIGHT
```

**Key features:**
- Custom dimensions (required parameter)
- Minimal prompt wrapping (maximum flexibility)
- Supports style references
- Saves to: `./output/Images/[YYYY-MM-DD]/`
- Default: 3 variations

### Generic Image Parameters

#### Required
- **idea** (positional): Your image concept with full prompt details
- `--dimensions WIDTHxHEIGHT`: Image dimensions (e.g., 1080x1080, 1200x675)

#### Style & References
- `--style PATH`: Style reference image - AI matches aesthetic
- `--ref PATH`: Additional reference image (can use multiple times)

#### Logo & Output
- `--logo PATH`: Logo to composite onto image
- `--logo-position [bottom-right|bottom-left|top-right|top-left]`: Logo placement (default: bottom-right)
- `-o, --output PATH`: Output directory or file path
- `--variations N`: Number of variations to generate (default: 3)

### Generic Image Examples

**Instagram square post:**
```bash
uv run image-gen.py "minimalist product photo on white background" --dimensions 1080x1080
```

**Instagram story:**
```bash
uv run image-gen.py "vertical motivational quote design" --dimensions 1080x1920
```

**Twitter post image:**
```bash
uv run image-gen.py "tech announcement graphic with blue theme" --dimensions 1200x675
```

**Facebook cover photo:**
```bash
uv run image-gen.py "professional business header" --dimensions 820x312
```

**With style reference:**
```bash
uv run image-gen.py "my concept" --dimensions 1200x1200 --style ./reference.jpg
```

### Common Dimensions Reference

| Platform | Size | Aspect Ratio | Command |
|----------|------|--------------|---------|
| **YouTube Thumbnail** | 1920x1080 | 16:9 | Use `thumbnail.py` instead |
| **LinkedIn Square** | 1200x1200 | 1:1 | Use `linkedin-post.py` instead |
| **Instagram Square** | 1080x1080 | 1:1 | `--dimensions 1080x1080` |
| **Instagram Story** | 1080x1920 | 9:16 | `--dimensions 1080x1920` |
| **Twitter Post** | 1200x675 | 16:9 | `--dimensions 1200x675` |
| **Facebook Post** | 1200x630 | 1.91:1 | `--dimensions 1200x630` |
| **Pinterest Pin** | 1000x1500 | 2:3 | `--dimensions 1000x1500` |

### Generic Image Specifications

| Element | Value |
|---------|-------|
| **Resolution** | Custom (user-specified via --dimensions) |
| **Aspect Ratio** | Auto-calculated from dimensions |
| **Format** | PNG (or JPG with --output .jpg) |
| **Default Variations** | 3 |
| **Model** | Gemini 3 Pro (Nano Banana Pro) by default |
| **Prompting** | Minimal wrapper - user has full control |

### Important Generic Image Notes

- **Use specific tools first**: Prefer `thumbnail.py` for YouTube or `linkedin-post.py` for LinkedIn
- **Maximum flexibility**: Minimal opinionated prompting - you control the style
- **Custom dimensions required**: Must specify `--dimensions` parameter
- **Platform-agnostic**: Works for any social media platform or use case
- Cost: ~$0.13 per image with Gemini 3 Pro
- Internet connection required

## Asking for Missing Information

When the user doesn't provide necessary information, ask them:

### For Transcription
- **Path to video folder**: "What's the path to the folder containing your video clips?"
- **Single video or clips**: "Is this a single video file or multiple clips that should be stitched together?"
- **If regenerating chapters**: "Do you already have a transcript, or should I transcribe the video first?"

### For Thumbnails

**Before generating**: Read [references/brand-guidelines.md](references/brand-guidelines.md) to understand color palette and composition requirements.

- **Thumbnail idea**: "What's your thumbnail idea or concept?"
- **Face reference**: "Would you like to include a face reference? If so, what's the path to the image?"
- **Face swap**: "Do you want to use face swap for exact likeness? (This requires REPLICATE_API_TOKEN)"
- **Style reference**: "Do you have an existing thumbnail style to match? If so, provide the path."
- **Logo**: "Would you like to add a logo to the thumbnail? If so, provide the path."
- **Number of variations**: "How many thumbnail variations would you like? (Default is 3)"
- **Output location**: "Where should I save the thumbnails? (Default is ./output/Thumbnails/[today's date]/)"

### For LinkedIn Images

- **LinkedIn post idea**: "What's your LinkedIn post or infographic concept?"
- **Style reference**: "Do you have an existing infographic style to match? If so, provide the path."
- **Logo**: "Would you like to add a logo to the image? If so, provide the path."
- **Number of variations**: "How many variations would you like? (Default is 3)"
- **Output location**: "Where should I save the LinkedIn images? (Default is ./output/LinkedIn/[today's date]/)"

### For Generic Images

- **Image concept**: "What's your image concept? Provide full prompt details including style, mood, and visual elements."
- **Dimensions (REQUIRED)**: "What dimensions do you need? (e.g., 1080x1080 for Instagram, 1200x675 for Twitter, 1080x1920 for Instagram Story)"
- **Platform**: "What platform is this for? (Instagram, Twitter, Facebook, etc.)"
- **Style reference**: "Do you have a style reference image? If so, provide the path."
- **Logo**: "Would you like to add a logo? If so, provide the path."
- **Number of variations**: "How many variations would you like? (Default is 3)"
- **Output location**: "Where should I save the images? (Default is ./output/Images/[today's date]/)"

## Cost Estimates

### Transcription
- **Whisper API**: $0.006 per minute of audio
- **Gemini (chapters)**: Free tier available, then pay-per-use
- **Example**: 20-minute video = ~$0.12-0.15 (transcription + chapters)

### Thumbnails
- **Gemini (generation)**: ~$0.13 per image
- **Face swap (optional)**: ~$0.003 per swap
- **Example**: 3 thumbnails with face swap = ~$0.40

### LinkedIn Images
- **Gemini (generation)**: ~$0.13 per image
- **Example**: 3 LinkedIn infographics = ~$0.40

### Generic Images
- **Gemini (generation)**: ~$0.13 per image
- **Example**: 3 Instagram posts = ~$0.40

## Configuration

Environment variables in `video-processor/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required) | OpenAI API key for Whisper |
| `WHISPER_MODEL` | `whisper-1` | Transcription model |
| `GEMINI_API_KEY` | (required) | Gemini API key |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Model for chapters |
| `GEMINI_THUMBNAIL_MODEL` | `gemini-3-pro-image-preview` | Model for thumbnails |
| `REPLICATE_API_TOKEN` | (optional) | For face swap feature |

## Error Handling

Common issues and solutions:

1. **"OPENAI_API_KEY not set"**: Add key to `.env` file
2. **"GEMINI_API_KEY not set"**: Add key to `.env` file
3. **"FFmpeg not installed"**: Install FFmpeg (see README)
4. **"No video files found"**: Check file extensions and ensure files are in folder root
5. **Quota errors (thumbnails)**: Ensure billing enabled on Google AI account

## Workflow Examples

### Complete Video Processing Workflow

1. **Transcribe video clips:**
   ```bash
   cd video-processor
   uv run transcribe.py ./my-video/
   ```

2. **Review and edit chapters if needed:**
   ```bash
   # Edit chapters.txt manually or regenerate
   uv run chapters.py ./my-video/transcript.md
   ```

3. **Generate thumbnails:**
   ```bash
   uv run thumbnail.py "my video concept" --face ./assets/face.jpg --variations 3
   ```

### Quick Thumbnail Testing

```bash
cd video-processor
uv run thumbnail.py "quick test" --variations 1 --no-face
```

### Professional Thumbnail with Branding

```bash
cd video-processor
uv run thumbnail.py "professional video title" \
  --face ./assets/face-excited.jpg \
  --style ./previous-thumbnail.jpg \
  --logo ./assets/logo.png \
  --logo-position bottom-right \
  --face-swap
```
