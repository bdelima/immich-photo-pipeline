---
name: "photo-mat-recipe"
description: "Build a beveled 3D matted photo (or portrait-photo collage) for a screensaver/TV display: optimize, straighten if safe, crop to 16:9 including the mat, and pick a mat color from the photo's own palette."
---

# Photo Mat Recipe

Turns a photo (or a set of portrait photos) into a museum-style matted image sized for a 16:9 screensaver/TV, with a beveled 3D mat edge in a color pulled from the photo itself.

Work in Python with Pillow (PIL) and OpenCV (cv2) + numpy. Always preserve full source resolution - never upscale.

## Pipeline order (always this order)

1. Load + fix orientation: ImageOps.exif_transpose() on every photo before anything else - portrait phone photos are often stored rotated.
2. Straighten (only if a confident reference exists - see below).
3. Optimize (contrast/saturation/sharpness/brightness).
4. Determine the mat color by sampling the OPTIMIZED image's palette.
5. Crop to the final canvas (mat math first, see below), then build the mat with bevel + drop shadow.
6. Export at quality 100 (or 95 for very large multi-photo collages), subsampling=0.

Doing color sampling before the final crop, and settling canvas size before cropping, avoids rework.

## 1. Straightening - be conservative

Most "crooked" complaints are about a genuine level reference (ocean horizon, sand/water line, a treeline, an architectural roofline or post) - but natural terrain (dunes, hills, mountain ridges, forest floor, waterfall spray) is NOT a level reference and must not be straightened against. When no reliable reference exists, leave the photo untouched rather than guess.

Finding the reference:
- Sea/sky horizon or sand/water line: scan image columns; for each column find the row where brightness crosses a threshold derived from a sampled reference region (e.g. sky brightness from the top 8-10% of the frame, or brightest-pixel row in a band for a water-reflection line). Collect (x, y) points.
- Architectural line (roofline, post, railing): either detect via cv2.HoughLinesP on Canny edges restricted to a region of interest, or track a feature's center-of-mass column by column/row by row (e.g. dark post against a light wall).
- Fit a line to the points with np.polyfit, iterating 3-4 rounds of outlier trimming (keep points within ~1.0-1.5x the residual percentile each round) to reject noise like waves, spray, or foreground clutter.
- Convert the fitted slope to a tilt angle: angle = degrees(atan(slope)).
- ALWAYS verify empirically before committing: rotate the image by the candidate angle (and its negative) and re-measure the same reference - confirm which sign actually levels it, and fine-tune by testing several nearby values. Never trust the sign from the math alone; direction of rotate() and the atan2 argument order are easy to invert.
- Prefer the cleanest, least-noisy reference available (e.g. a distant sand line over a tree-covered ridge; a left-side open horizon over a right-side headland) and restrict the x or y range of the fit to that clean region.
- Only apply a correction when the fit is confident: enough inlier points, a tight residual spread, and a reasonable span across the frame width. If uncertain, say so and leave the photo unstraightened rather than force it.
- After rotating, crop to the largest axis-aligned rectangle that avoids the rotated image's transparent/black corners (standard "rotated rect inscribed after rotation" formula - compute from original w,h and the angle, then center-crop).

## 2. Optimization (always applied, lightly)

Compute stats first, then choose factors - never use fixed blind multipliers:

- gray = mean of RGB channels; compute 1st and 99th percentile brightness, overall mean brightness, and mean HSV saturation.
- contrast_factor: 1.10 if the 1st-99th percentile range is under ~230 (image isn't using the full range), else 1.05.
- sat_factor: 1.20 if mean saturation is under ~100 (image reads flat/muted), else 1.08.
- brightness_factor: 1.08 if mean brightness < 110 (dark/underexposed), 0.96 if mean brightness > 190 (blown out/bright), else 1.0.
- sharpness: flat +1.3 (ImageEnhance.Sharpness) works well across the board as a mild clarity boost.
- Apply via PIL.ImageEnhance in order: Contrast, Color, Sharpness, Brightness.

## 3. Picking the mat color - the part that needs the most judgment

Goal: the mat should look like an intentional curatorial choice that complements the photo, not a random average or an invented color.

Rules learned from iteration:

- Sample the DOMINANT colors of the OPTIMIZED photo (PIL quantize to ~8-12 colors on a small resize, e.g. 150x70, then rank by pixel-count share).
- The chosen base color MUST be a color that is actually, visibly present in the photo - not a synthetic hue shift invented to sound nice (e.g. don't turn a red flower into a "terracotta" that appears nowhere in frame; if warmth is wanted, shift toward a hue that's still represented, or pick a different real dominant color like foliage green).
- Prefer sampling from the actual SUBJECT (e.g. the flower itself, the bird's feathers, the sand dollar, the oxcart) over the generic background when the subject has a distinctive, nameable color - this makes mats feel chosen, not averaged.
- Reject/avoid as mat candidates: colors that are the single most vivid/saturated accent in the frame (e.g. a bright red shirt, electric green grass) picked up as "dominant" purely by pixel count - these make garish mats. Filter candidates by saturation ceiling (e.g. reject if (max-min) of RGB > ~55-90) before ranking by prevalence, unless deliberately matching a vivid subject (e.g. hibiscus petals, macaw feathers) at reduced saturation.
- For a photo that is dark AND highly saturated overall (e.g. dense rainforest canopy, night shots) - don't force a tinted mat averaged from "low saturation" dominant colors, because even the darkest-green "desaturated" dominant color blended toward white still reads muddy/gray. In that case default to a clean soft white/off-white mat instead.
- Lightness/saturation targeting: convert the chosen base RGB to HSV, keep the hue, reduce saturation by roughly 40-60% (sat_mult ~0.4-0.6), and set value/lightness to a fixed moderate-light target (~0.80-0.82 in HSV V, or scale RGB so mean channel value lands around 200-210). This keeps the mat light enough to read as a mat but NOT so light/near-white that the bevel highlight/shadow lose contrast and the 3D effect disappears. Never go so light the mat reads flat, and the user has said: never go dark.
- If shifting hue for warmth/coolness, verify the shift direction empirically (hue is circular; +N degrees vs -N degrees can go the wrong way, e.g. red shifting toward pink/mauve instead of orange/terracotta) - render a candidate and LOOK at it before committing, don't trust the arithmetic blindly.
- When asked to fine-tune ('too pink', 'too gray', 'needs more warmth', 'match the lighter petals instead'), re-sample a MORE SPECIFIC region of the photo (e.g. crop just the inner petal, just the subject's main body) rather than nudging the old numeric value blindly - go back to actual pixels.
- For a multi-photo collage, pick ONE shared mat color that either (a) is common to both/all photos' palettes, or (b) bridges them - e.g. a wood tone that appears as a minor element in one photo (a birdhouse) and ties thematically to bark/nest tones dominant in the others. Avoid a color that is only strongly present in one of the photos and reads as arbitrary next to the others.

## 4. Canvas math - mat-first, not photo-first

The mat width is typically 100px. CRITICAL: compute the TOTAL canvas (photo + mat) as 16:9 first, then back-solve the required photo crop size - never crop the photo to 16:9 first and add a mat on top, or the combined image drifts off 16:9 and will letterbox/stretch oddly on a TV.

For a single photo:
```
mat = 100
canvas_h = target_height  # e.g. 2068, chosen to match native photo height + 2*mat with no upscaling
canvas_w = round(canvas_h * 16 / 9)
photo_h_needed = canvas_h - 2*mat
photo_w_needed = canvas_w - 2*mat
```
Then center-crop the (optimized, optionally straightened) photo to photo_w_needed x photo_h_needed. If the photo's native resolution is smaller than needed in either dimension, DO NOT upscale - instead shrink canvas_h to fit the native photo height (ph_target = min(native_h, desired_h)) and rebuild canvas_w/photo_w_needed from that, OR tell the user the source is too small for that exact target. Always check: is the crop even possible without upscaling? If a crazy-oversized source (e.g. 12000x9000 panorama) is involved, you MUST resize it down to the needed photo_w x photo_h after cropping - cropping alone on a huge image pastes a giant/zoomed fragment, not the intended view.

## 5. Protecting subjects during crop (critical photographer's judgment)

Before committing to any crop (single photo or collage), visually check the photo's edges/margins for content that must not be cut:
- Crop and view thin strips at each edge (top 10-20%, bottom 10-20%, etc.) to see exactly what's there before deciding a crop amount.
- For a well-camouflaged or edge-adjacent subject (e.g. a snake draped across a frame, a tail feather nearly touching an edge, a surfboard filling almost the whole frame top-to-bottom), trace its FULL extent first (zoom into regions, check both directions) before applying any centered crop.
- If a subject already touches the true edge of the original photo, a crop that trims ONLY from the opposite side (an asymmetric 'left_only' or 'right_only' crop, not centered) can preserve it rather than cutting further into it.
- When a user flags a photo as containing something easy to miss, do NOT just trust a naive centered-crop math - always verify visually afterward that the subject survived intact.

## 6. Building the mat with bevel + drop shadow (the 3D effect)

Given the final photo and chosen mat_color:

```python
highlight = lighten(mat_color, ~55%)   # top+left edge of the bevel ring
shade = darken(mat_color, ~74-88%)     # bottom+right edge of the bevel ring (use 0.74 for a visibly 3D bevel; lighter mats need the darker end of that range to still show contrast)

canvas = solid mat_color, full canvas size

# drop shadow: soft blurred dark rectangle, offset down-right from where the photo will sit
shadow_layer = transparent RGBA same size as canvas
draw dark rectangle (alpha ~90-110) at photo position + (14,14) offset, sized to the photo
blur it (GaussianBlur radius ~20-22)
composite shadow_layer onto canvas

paste the photo onto canvas at (mat, mat)

# bevel: draw 4 thin rectangles (~7-8px) framing the photo - highlight on top+left, shade on bottom+right
# then re-paste the photo on top so the bevel only shows as a ring around it
# slightly blur just the bevel ring (via a mask: bevel-ring area only, blur radius ~1-1.2) so it doesn't look hard-edged/drawn-on
```

Light is assumed to come from the upper-left (standard convention) - don't flip this without being asked.

## 7. Multi-photo collages (portrait photos side-by-side)

When the user wants portrait-orientation photos combined into one wide image (they fill a 16:9 screen far better combined than one portrait alone):

- 2 photos side-by-side require cropping each down to roughly half their original height to reach true 16:9 - this is often too aggressive and risks cutting into tall subjects (e.g. a surfboard filling the frame). It's fine to accept a less-than-16:9 ratio (some letterboxing) rather than ruin a photo; tell the user this tradeoff plainly.
- 3 photos side-by-side reach true 16:9 with a MUCH gentler per-photo crop (~80% of original height kept, vs ~50% for 2-up) because the added photo increases total content width while mat/gap overhead stays nearly fixed. Prefer 3-up over 2-up when the user has 3 photos and screen-filling matters, and explain the crop-amount tradeoff in these terms if asked.
- Always check each photo's actual safe margins (see section 5) before picking a shared crop-trim amount; different photos in the same collage may need different trim amounts if their subjects' safe margins differ - apply the smallest safe trim as the common amount, or crop asymmetrically per-photo as needed.
- Build each photo panel by cropping it to the same target height, running it through optimize(), and placing panels left-to-right into one canvas with a shared mat (mat border on the outside, a narrower gap of ~80px between photos), each with its own bevel+shadow treatment (same mat_color for all panels, each panel beveled individually with that color).
- Pick ONE mat color per collage per the rules in section 3, considering all photos in the set together.
- A photo that is already a comfortable "wide" shape (e.g. a close-up bird/nature photo not shot portrait) can be resized (not cropped) to match the target panel height, since it may not need trimming to fit - only crop photos that actually need their aspect changed.

## 8. Batch processing from a zip of photos

- Extract the zip, use ImageOps.exif_transpose on every image to correct orientation before measuring anything.
- Preview/identify each photo (thumbnail) before batch-processing, especially when the user refers to photos by content ("the tiger one", "the one with the snake") rather than filename.
- Sample palette and decide straighten/mat-color per-photo (don't apply one blind setting to a whole batch) - use the per-photo analysis described above for each file.
- Deliver outputs as INDIVIDUAL FILES, not bundled back into a zip, unless the user asks otherwise (this is a standing preference once stated).
- Spot-check a handful of outputs visually (Read the resulting image) before declaring a batch done, especially after any code change - resizing bugs (e.g. forgetting to downscale an oversized source after cropping) are easy to introduce silently.

## 9. When the user pushes back

Treat every correction as information about a wrong assumption in the recipe, not just a one-off fix:
- "Mat looks pink/mauve, should be coral/terracotta" -> hue-shift direction or sampled region was wrong; re-sample a better region and verify the shift direction visually.
- "Mat lost the 3D bevel effect" -> mat was pushed too close to white; keep mat lightness in the 195-215ish RGB mean range, not above it.
- "That invented color isn't in the photo" -> always validate a candidate mat color is traceable to real, sampled pixels, not synthesized from a hue-shift that invents a new family of color.
- "Still looks crooked" -> the previously used reference wasn't the true level line; find a better/cleaner reference (e.g. the matching background subject shared with a straight neighboring photo) and re-verify with empirical rotation tests, not just trusting the first automated angle.
- "Horizon looks fine actually" after you "fixed" it -> some apparent tilt is real elevated terrain (headland, hill) next to a genuinely level horizon; measure precisely (print exact per-column y-values) before concluding there's an error, and be ready to show the math/overlay if the user disputes it.
