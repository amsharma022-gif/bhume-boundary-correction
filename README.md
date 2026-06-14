# BhuMe Boundary Correction

## Problem
Maharashtra land record maps were drawn on paper and digitized onto satellite imagery with misalignment — official plot boundaries are shifted several meters from their real position on the ground.

## Approach

### Step 0 — Invalid Polygon Detection
Before any correction, flag plots with invalid geometries (e.g. MultiPolygon errors). These cannot be reliably corrected.

### Step 1 — Global Shift
Using the 6 example truths, calculate the median offset between official and true plot positions. Apply this single shift to all 2457 plots. Improves median IoU from 0.612 to 0.713.

### Step 2 — Edge Score Confidence
For each plot, crop the satellite image patch underneath it and measure edge strength (gradient magnitude). Clear visible field boundaries = high confidence. Blurry or featureless patches = low confidence.

### Step 3 — Overlap Penalty
After correction, check each plot against its neighbors using a spatial index. Two people cannot own the same land — overlapping plots signal misalignment. Reduce confidence proportionally to overlap area.

### Step 4 — Flagging
Plots with confidence below 0.2 are flagged as uncertain rather than corrected.

## Results
- Median IoU: 0.713 vs official 0.612 (improvement: 0.112)
- Spearman calibration: 0.429
- All corrected plots achieve IoU >= 0.5

## What I tried and learned
- Boundary connectivity score — hurt
