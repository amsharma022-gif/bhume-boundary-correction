from __future__ import annotations
import numpy as np
import geopandas as gpd
from pathlib import Path
from bhume import load, write_predictions, patch_for_plot
from bhume.baseline import global_median_shift
from bhume.geo import open_imagery

def compute_edge_score(image: np.ndarray) -> float:
    """
    Score how many clear edges (field boundaries) are visible in this image patch.
    More visible edges = clearer boundary = higher confidence in correction.
    """
    gray = image.mean(axis=2).astype(np.float32)
    gx = np.diff(gray, axis=1)
    gy = np.diff(gray, axis=0)
    edge_strength = np.sqrt(gx[:-1,:]**2 + gy[:,:-1]**2)
    return float(edge_strength.mean())

def compute_overlap_penalty(preds: gpd.GeoDataFrame) -> dict:
    """
    For each plot, calculate how much it overlaps with neighbors.
    Two people can't own the same land — overlaps signal misalignment.
    Uses spatial index for efficiency instead of O(n^2) comparison.
    """
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

        overlap_pct = min(total_overlap / plot_area, 1.0)
        penalties[row['plot_number']] = overlap_pct

    return penalties

def predict(village_dir: str) -> None:
    village = load(village_dir)
    print(f"loaded {village.slug} - {len(village.plots)} plots")

    # step 0 - flag invalid polygons first
    invalid_plots = village.plots[~village.plots.geometry.is_valid].index.tolist()
    if invalid_plots:
        print(f"found {len(invalid_plots)} invalid polygons - flagging immediately")

    # step 1 - global shift
    preds = global_median_shift(village)
    print("global shift applied")

    # flag invalid plots immediately
    preds.loc[preds['plot_number'].isin(invalid_plots), 'status'] = 'flagged'
    preds.loc[preds['plot_number'].isin(invalid_plots), 'confidence'] = 0.0
    preds.loc[preds['plot_number'].isin(invalid_plots), 'method_note'] = 'invalid geometry - incomplete polygon'

    # step 2 - edge score from satellite image
    edge_scores = {}
    with open_imagery(village.imagery_path) as src:
        for pn in village.plots.index:
            try:
                patch = patch_for_plot(src, village.plot(pn), pad_m=20)
                edge_scores[pn] = compute_edge_score(patch.image)
            except Exception:
                edge_scores[pn] = 0.0

    # step 3 - normalize edge scores to 0-1
    vals = list(edge_scores.values())
    mn, mx = min(vals), max(vals)
    conf_map = {}
    for pn, sc in edge_scores.items():
        if mx > mn:
            conf_map[pn] = round((sc - mn) / (mx - mn), 3)
        else:
            conf_map[pn] = 0.5

    preds['confidence'] = preds['plot_number'].map(conf_map).fillna(0.5)

    # step 4 - overlap penalty
    print("computing overlap penalties...")
    penalties = compute_overlap_penalty(preds)
    penalty_series = preds['plot_number'].map(penalties).fillna(0.0)
    preds['confidence'] = (preds['confidence'] - penalty_series).clip(0.0, 1.0).round(3)

    # step 5 - flag low confidence plots
    preds.loc[preds['confidence'] < 0.2, 'status'] = 'flagged'
    preds['method_note'] = 'global_shift + edge_score + overlap_penalty'

    out = write_predictions(Path(village_dir) / 'predictions.geojson', preds)
    print(f"Wrote {len(preds)} predictions to {out}")

if __name__ == '__main__':
    import sys
    predict(sys.argv[1] if len(sys.argv) > 1 else 'data/vadnerbhairav')