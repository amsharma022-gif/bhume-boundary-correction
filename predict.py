from __future__ import annotations
import numpy as np
import geopandas as gpd
from pathlib import Path
from bhume import load, write_predictions, patch_for_plot
from bhume.baseline import global_median_shift
from bhume.geo import open_imagery

def compute_edge_score(image: np.ndarray) -> float:
    gray = image.mean(axis=2).astype(np.float32)
    gx = np.diff(gray, axis=1)
    gy = np.diff(gray, axis=0)
    edge_strength = np.sqrt(gx[:-1,:]**2 + gy[:,:-1]**2)
    return float(edge_strength.mean())

def compute_crop_row_alignment(image: np.ndarray, geom) -> float:
    import cv2
    gray = image.mean(axis=2).astype(np.uint8)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=20,
                             minLineLength=20, maxLineGap=5)
    if lines is None or len(lines) < 3:
        return 0.5
    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1)) % 180
        angles.append(angle)
    angles = np.array(angles)
    dominant_angle = np.median(angles)
    aligned = np.sum(
        (np.abs(angles - dominant_angle) < 15) |
        (np.abs(angles - (dominant_angle + 90) % 180) < 15)
    )
    return float(aligned / len(angles))

def compute_boundary_color_contrast(image: np.ndarray) -> float:
    """Kept for reference - tried this, didn't improve calibration."""
    h, w = image.shape[:2]
    inner_h1, inner_h2 = h // 4, 3 * h // 4
    inner_w1, inner_w2 = w // 4, 3 * w // 4
    inner_pixels = image[inner_h1:inner_h2, inner_w1:inner_w2]
    outer_pixels = np.concatenate([
        image[:h//4, :].reshape(-1, 3),
        image[3*h//4:, :].reshape(-1, 3),
        image[:, :w//4].reshape(-1, 3),
        image[:, 3*w//4:].reshape(-1, 3),
    ])
    inner_mean = inner_pixels.reshape(-1, 3).mean(axis=0)
    outer_mean = outer_pixels.mean(axis=0)
    color_diff = np.sqrt(np.sum((inner_mean - outer_mean) ** 2))
    return float(min(color_diff / 100.0, 1.0))

def compute_overlap_penalty(preds: gpd.GeoDataFrame) -> dict:
    """Two people can't own the same land - overlaps signal misalignment."""
    utm_crs = f'EPSG:{32600 + int((preds.geometry.iloc[0].centroid.x + 180) // 6) + 1}'
    preds_utm = preds.to_crs(utm_crs).reset_index(drop=True)
    sindex = preds_utm.sindex
    penalties = {}
    total = len(preds_utm)
    for i, row in preds_utm.iterrows():
        if i % 200 == 0:
            print(f"  checking overlaps... {i}/{total}")
        plot_geom = row.geometry
        plot_area = plot_geom.area
        if plot_area == 0:
            penalties[row['plot_number']] = 0.0
            continue
        candidate_idx = list(sindex.intersection(plot_geom.bounds))
        total_overlap = 0.0
        for j in candidate_idx:
            if j == i:
                continue
            neighbor_geom = preds_utm.iloc[j].geometry
            if plot_geom.intersects(neighbor_geom):
                overlap = plot_geom.intersection(neighbor_geom).area
                total_overlap += overlap
        penalties[row['plot_number']] = min(total_overlap / plot_area, 1.0)
    return penalties

def compute_boundary_hint_score(village, preds: gpd.GeoDataFrame) -> dict:
    """
    Tried using boundaries.tif pixel density as confidence signal.
    Kept here for reference - tested but did not improve calibration.
    """
    import rasterio
    from rasterio.windows import from_bounds
    if village.boundaries_path is None:
        return {}
    scores = {}
    with rasterio.open(village.boundaries_path) as src:
        for pn in village.plots.index:
            try:
                plot_geom = village.plots.loc[pn, 'geometry']
                gdf = gpd.GeoDataFrame(geometry=[plot_geom], crs='EPSG:4326')
                gdf_proj = gdf.to_crs(src.crs)
                bounds = gdf_proj.geometry.iloc[0].bounds
                window = from_bounds(bounds[0], bounds[1], bounds[2], bounds[3], src.transform)
                data = src.read(1, window=window)
                if data.size == 0:
                    scores[pn] = 0.0
                else:
                    scores[pn] = float((data > 128).sum() / data.size)
            except Exception:
                scores[pn] = 0.0
    return scores

def compute_boundary_edge_ratio(image: np.ndarray) -> float:
    """
    Measure edge strength specifically at the plot boundary (border of image patch)
    vs the interior. High ratio = our corrected boundary lands on a real visible edge.
    Low ratio = boundary landed in uniform texture = unverifiable correction.
    """
    h, w = image.shape[:2]
    gray = image.mean(axis=2).astype(np.float32)
    gx = np.diff(gray, axis=1, prepend=gray[:, :1])
    gy = np.diff(gray, axis=0, prepend=gray[:1, :])
    edge_map = np.sqrt(gx**2 + gy**2)
    margin_h = max(1, h // 7)
    margin_w = max(1, w // 7)
    border_mask = np.zeros((h, w), dtype=bool)
    border_mask[:margin_h, :] = True
    border_mask[-margin_h:, :] = True
    border_mask[:, :margin_w] = True
    border_mask[:, -margin_w:] = True
    border_edge = edge_map[border_mask].mean()
    interior_edge = edge_map[~border_mask].mean()
    if interior_edge == 0:
        return 0.5
    return float(min(border_edge / interior_edge, 3.0) / 3.0)

def normalize(d):
    vals = list(d.values())
    mn, mx = min(vals), max(vals)
    if mx == mn:
        return {k: 0.5 for k in d}
    return {k: (d[k] - mn) / (mx - mn) for k in d}

def predict(village_dir: str) -> None:
    village = load(village_dir)
    print(f"loaded {village.slug} - {len(village.plots)} plots")

    # step 0 - flag invalid polygons first
    # broken geometry = can't trust correction
    invalid_plots = village.plots[~village.plots.geometry.is_valid].index.tolist()
    if invalid_plots:
        print(f"found {len(invalid_plots)} invalid polygons - flagging immediately")

    # step 1 - global shift
    preds = global_median_shift(village)
    print("global shift applied")

    preds.loc[preds['plot_number'].isin(invalid_plots), 'status'] = 'flagged'
    preds.loc[preds['plot_number'].isin(invalid_plots), 'confidence'] = 0.0
    preds.loc[preds['plot_number'].isin(invalid_plots), 'method_note'] = 'invalid geometry'

    # step 2 - image signals per plot
    edge_scores = {}
    crop_scores = {}
    boundary_ratios = {}

    with open_imagery(village.imagery_path) as src:
        for pn in village.plots.index:
            try:
                patch = patch_for_plot(src, village.plot(pn), pad_m=20)
                edge_scores[pn] = compute_edge_score(patch.image)
                crop_scores[pn] = compute_crop_row_alignment(
                    patch.image, village.plots.loc[pn, 'geometry'])
                boundary_ratios[pn] = compute_boundary_edge_ratio(patch.image)
            except Exception:
                edge_scores[pn] = 0.0
                crop_scores[pn] = 0.5
                boundary_ratios[pn] = 0.0

    # step 3 - normalize and combine signals
    # best combination found through testing: edge 60% + crop row 40%
    # boundary hints and color contrast tested but hurt calibration - kept as functions
    edge_norm = normalize(edge_scores)
    crop_norm = normalize(crop_scores)

    conf_map = {}
    for pn in village.plots.index:
        combined = (edge_norm[pn] * 0.6) + (crop_norm[pn] * 0.4)
        conf_map[pn] = round(float(combined), 3)

    preds['confidence'] = preds['plot_number'].map(conf_map).fillna(0.5)

    # step 4 - overlap penalty
    # two people can't own the same land
    print("computing overlap penalties...")
    penalties = compute_overlap_penalty(preds)
    penalty_series = preds['plot_number'].map(penalties).fillna(0.0)
    preds['confidence'] = (preds['confidence'] - penalty_series).clip(0.0, 1.0).round(3)

    # step 5 - smarter flagging using boundary edge ratio
    # if corrected boundary has no edge signal underneath it,
    # we can't verify the correction - flag it as uncertain
    flagged_count = 0
    for pn in village.plots.index:
        ratio = boundary_ratios.get(pn, 0.5)
        mask = preds['plot_number'] == pn
        current_conf = preds.loc[mask, 'confidence'].values
        if len(current_conf) > 0:
            # flag if: low boundary edge signal AND low confidence
            if ratio < 0.15 and current_conf[0] < 0.35:
                preds.loc[mask, 'status'] = 'flagged'
                preds.loc[mask, 'method_note'] = 'flagged: no edge signal at boundary'
                flagged_count += 1

    # also flag anything below 0.15 confidence regardless
    low_conf_flags = (preds['confidence'] < 0.15) & (preds['status'] != 'flagged')
    preds.loc[low_conf_flags, 'status'] = 'flagged'
    flagged_count += low_conf_flags.sum()

    preds['method_note'] = preds['method_note'].fillna(
        'global_shift + edge_score + crop_row_alignment + overlap_penalty'
    )

    print(f"flagged {flagged_count} plots as uncertain")

    out = write_predictions(Path(village_dir) / 'predictions.geojson', preds)
    print(f"Wrote {len(preds)} predictions to {out}")

if __name__ == '__main__':
    import sys
    predict(sys.argv[1] if len(sys.argv) > 1 else 'data/vadnerbhairav')