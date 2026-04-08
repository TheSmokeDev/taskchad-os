# Thumbnail Generation Best Practices

This document captures learnings and best practices for generating YouTube thumbnails using the video-processor skill.

## Brand Color Guidelines

### Blue Accent Strategy
- **Use blue as an ACCENT color**, not the dominant color
- Blue works best for:
  - Highlighting 1-2 key words in text (e.g., "REAL PURPOSE" in blue, rest in white)
  - Subtle glows around elements
  - One of multiple colored arrows/elements
  - Borders or outlines on boxes/icons
  - Checkmarks or small UI elements

### What to Avoid
- ❌ Making the entire thumbnail blue
- ❌ Applying blue tint/cast to the person's face
- ❌ Using only blue colors (creates monotone, overwhelming look)
- ❌ Blue background lighting that affects skin tone

### Balanced Approach
- ✅ Mix of vibrant colors (orange, yellow, red, blue)
- ✅ Blue for select text highlights
- ✅ Natural skin tones with realistic lighting
- ✅ Dark backgrounds with subtle blue accents
- ✅ Professional, colorful aesthetic with brand consistency

## Face/Person Guidelines

### Natural Skin Tone (CRITICAL)
- Person must have **natural, realistic skin tone**
- No blue tint or color cast on face, even if background has blue elements
- Good lighting on face separate from background lighting
- Face should be 30-40% of frame and clearly visible

### Expression & Positioning
- Expression should match thumbnail concept (excited, surprised, serious)
- Direct eye contact with viewer when appropriate
- Position face similar to style reference if provided

## Text Guidelines

### Balanced Text Highlighting
- Highlight 1-2 key words in blue
- Keep remaining text white or high-contrast color
- Example patterns:
  - "**REAL PURPOSE** OF RALPH" (blue + white)
  - "REAL **PURPOSE** OF RALPH" (white + blue + white)
  - "**3 POCs**, 1 LOOP" (blue + white)

### Text Requirements
- Bold, large, readable (max 4 words)
- High contrast against background
- Not overwhelming the visual composition

## Color Palette Examples

### Good Combinations
1. **Balanced Multi-Color**: Orange, yellow, blue arrows + blue text highlights
2. **Blue Accent**: White text with blue keyword + mixed colored elements
3. **Professional Tech**: Dark background + blue outlines + natural lighting on face

### Ralph Wiggum Video Learnings
From thumbnails 10-12 (2026-01-20 batch):
- Three parallel arrows in orange/yellow/blue (not all blue)
- "REAL" highlighted in blue, "PURPOSE OF RALPH" in white
- Blue-accented boxes/checkmarks on right
- Natural face lighting on left
- Dark tech background with subtle blue elements

## Composition Guidelines

### Layout Balance
- Person on left (30-40% of frame)
- Visual elements/flow on right
- Text at top or along composition flow
- Dark/uncluttered background

### Visual Flow
- Use arrows or lines to guide eye
- Parallel elements for "multiple options" concepts
- Icons/boxes for destinations or outcomes
- Clean professional aesthetic

## Testing Approach

When generating variations:
1. Start with 1-2 test variations to check color balance
2. If too much blue: regenerate with "balanced colors" emphasis
3. If face has blue tint: regenerate with "natural skin tone" emphasis
4. Generate 3 final variations once balance is correct

## Prompt Examples

### Well-Balanced Blue Accent Prompt
```
Person left side natural skin tone excited expression, three parallel loop arrows
flowing right mix of blue orange yellow, top right text REAL PURPOSE with blue
highlight OF RALPH in white, three boxes right POC 1 POC 2 POC 3 with blue accent
checkmarks, RALPH LOOPS center with blue glow accent, dark tech background
professional balanced colors
```

### What NOT to Do
```
❌ Person with blue lighting, all blue arrows and text, blue background,
   everything blue themed
```

## Style Reference Usage

When using `--style` flag:
- Match layout and composition
- Match text positioning and weight
- DO NOT blindly copy all colors if reference is too blue
- Balance color palette while maintaining aesthetic
- Can combine style reference with color adjustments in prompt

## Summary

**Core principle**: Blue is part of the brand, but balance is key. Natural faces + mixed colors + blue accents = professional, eye-catching thumbnails that maintain brand consistency without overwhelming viewers.
