"""
Color clamping: Reduce an image to a set of colors such the original
image colors represented by the closest color to it in the set but with
the constraints that the image not lose any interpretability. This is diff
Code was completed by Claude Opus 4.6 based on prompts which directed 
it to adapt the method described in the paper:

 * https://www.sciencedirect.com/science/article/abs/pii/S0097849315000114
 
Description in paper is:

For a given user design, our algorithm automatically generates a set of stencil 
layers satisfying all required properties. The task is formulated as a constrained 
energy optimization problem and solved efficiently. Experiments, including a user 
study, are carried out to examine the complete algorithm as well as each individual step.
 
!! Claude has written everything below this line. !!

MRF-based color quantization for HTV vinyl layer separation.

Replaces PIL's per-pixel img.quantize() with spatially coherent
multi-label graph-cut segmentation (Jain et al., Computers & Graphics 2015).

Dependency:  pip install pygco numpy

The core energy being minimized is Eq. (4) from the paper:

    E(z) = Σ_i  φ_{i,ℓ} [[z_i = ℓ]]
         + Σ_{(i,j)} (β·ψ_{i,j} + 2α) [[z_i ≠ z_j]]

where:
    φ_{i,ℓ} = (1 / 2σ_c²) · ‖c_ℓ − c_i‖²         unary: color distance
    ψ_{i,j} = exp(−‖c_i − c_j‖² / σ_p²)            pairwise: contrast-sensitive

The unary term is what PIL quantize computes (nearest color), but the
pairwise term enforces spatial coherence: label boundaries are penalized
unless they coincide with strong edges in the image.
"""

import numpy as np
from PIL import Image


def mrf_quantize(
    img: Image.Image,
    color_specs: list[tuple[str, tuple[int, int, int]]],
    alpha: float = 8.0,
    beta: float = 40.0,
    sigma_color: float = 50.0,
    sigma_pair: float = 30.0,
    scale: int = 10,
) -> tuple[np.ndarray, Image.Image, list[tuple[str, tuple, Image.Image]]]:
    """
    MRF multi-label segmentation via alpha-expansion graph cuts.

    Parameters
    ----------
    img : PIL.Image
        Input RGB image (e.g. ESRGAN-upscaled diffusion output).

    color_specs : list of (name, (r, g, b))
        Target palette.  White-first ordering is fine but not required.

    alpha : float
        Boundary length penalty.  Higher → simpler cut paths, fewer small
        regions.  This directly controls total cut length on the vinyl
        cutter.  Start around 5–15; increase if you get too many tiny
        fragments to weed.

    beta : float
        Edge-adherence weight.  Higher → label boundaries snap harder to
        actual color edges in the image.  Controls how much the MRF
        respects the diffusion model's drawn contours vs. smoothing
        through them.  Start around 20–60.

    sigma_color : float
        Unary scaling (the σ in φ_{i,ℓ} = ‖c_ℓ − c_i‖² / 2σ²).
        Smaller → more aggressive color assignment, less tolerance for
        near-matches.  Larger → softer unaries, pairwise term dominates.
        Typical range 30–80.

    sigma_pair : float
        Pairwise contrast sensitivity (the denominator in ψ_{i,j}).
        Smaller → only very strong edges suppress the smoothness penalty.
        Larger → even gentle gradients are treated as edges.
        Typical range 20–50.

    scale : int
        Integer multiplier for converting float costs to int32 (pygco
        requires integer costs).  Increase if you see quantization
        artifacts in the energy; decrease if you hit int32 overflow on
        very large images.

    Returns
    -------
    labels : np.ndarray (H, W), int
        Label index per pixel (index into color_specs).

    clamped : PIL.Image
        Reconstructed RGB image with each pixel set to its label's color.

    color_layers : list of (name, rgb_tuple, mask_image)
        Per-color binary masks in mode "L": 0 = this color, 255 = not.
        Same format as your existing pipeline expects for vtracer.
    """
    from pygco import cut_from_graph

    rgb_arr = np.array(img.convert("RGB"), dtype=np.float64)
    H, W = rgb_arr.shape[:2]
    n_pixels = H * W
    n_labels = len(color_specs)
    pixels = rgb_arr.reshape(-1, 3)

    # ── Unary potentials ────────────────────────────────────────────────
    #   φ_{i,ℓ} = (1 / 2σ²) ‖c_ℓ − c_i‖²
    #
    # This is the same distance that PIL quantize uses for nearest-color,
    # but kept as a soft cost instead of taking argmin.

    unary = np.zeros((n_pixels, n_labels), dtype=np.int32)
    for idx, (_, rgb) in enumerate(color_specs):
        c = np.array(rgb, dtype=np.float64)
        dist_sq = np.sum((pixels - c) ** 2, axis=1)
        unary[:, idx] = (scale * dist_sq / (2.0 * sigma_color ** 2)).astype(np.int32)

    # ── Pairwise label cost ─────────────────────────────────────────────
    #   Potts model: cost 1 if labels differ, 0 if same.
    #   The actual edge-dependent weight goes into the edge weight arrays.

    pairwise = (
        np.ones((n_labels, n_labels), dtype=np.int32)
        - np.eye(n_labels, dtype=np.int32)
    )

    # ── Build explicit edge list with contrast-sensitive weights ────────
    #   w_{i,j} = β · exp(−‖c_i − c_j‖² / σ_p²) + 2α
    #
    #   At strong edges (high color diff): w → 2α  (low penalty for label change)
    #   In smooth regions (low color diff): w → β + 2α  (high penalty)
    #
    #   This is what makes boundaries snap to image edges instead of
    #   wandering through anti-aliased gradients.
    #
    #   cut_from_graph needs:
    #     edges:   (n_edges, 2)  int32  — pairs of pixel indices
    #     weights: (n_edges,)    int32  — per-edge cost multiplier

    # Pixel index grid: pixel (r,c) has flat index r*W + c
    idx_grid = np.arange(n_pixels, dtype=np.int32).reshape(H, W)

    # Horizontal edges: (r,c) ↔ (r,c+1)
    h_src = idx_grid[:, :-1].ravel()
    h_dst = idx_grid[:, 1:].ravel()
    h_diff_sq = np.sum((rgb_arr[:, :-1] - rgb_arr[:, 1:]) ** 2, axis=2).ravel()
    h_psi = np.exp(-h_diff_sq / (sigma_pair ** 2))
    h_w = (scale * (beta * h_psi + 2.0 * alpha)).astype(np.int32)

    # Vertical edges: (r,c) ↔ (r+1,c)
    v_src = idx_grid[:-1, :].ravel()
    v_dst = idx_grid[1:, :].ravel()
    v_diff_sq = np.sum((rgb_arr[:-1, :] - rgb_arr[1:, :]) ** 2, axis=2).ravel()
    v_psi = np.exp(-v_diff_sq / (sigma_pair ** 2))
    v_w = (scale * (beta * v_psi + 2.0 * alpha)).astype(np.int32)

    # Concatenate into single edge array with weights as third column
    #   pygco expects (n_edges, 3) int32: [src, dst, weight]
    edges = np.column_stack([
        np.concatenate([h_src, v_src]),
        np.concatenate([h_dst, v_dst]),
        np.concatenate([h_w, v_w]),
    ]).astype(np.int32)

    # ── Solve via alpha-expansion ───────────────────────────────────────
    #   Boykov et al. (2001) — the same solver the paper uses in its
    #   Multi-Label-Segmentation subroutine.
    #
    #   cut_from_graph(edges, unary, pairwise, n_iter, algorithm)
    #   edges is (n_edges, 3) with columns [src, dst, weight]
    #   returns flat label array (n_pixels,)

    labels = cut_from_graph(
        edges,
        unary,
        pairwise,
        n_iter=-1,
        algorithm="expansion",
    )
    labels = labels.reshape(H, W)

    # ── Reconstruct clamped image and per-color masks ───────────────────
    clamped_arr = np.zeros((H, W, 3), dtype=np.uint8)
    color_layers = []

    for idx, (name, rgb) in enumerate(color_specs):
        mask_bool = labels == idx
        clamped_arr[mask_bool] = rgb

        # Inverted mask for vtracer: 0 = foreground (this color), 255 = not
        mask_data = np.where(mask_bool, 0, 255).astype(np.uint8)
        color_layers.append((name, rgb, Image.fromarray(mask_data, mode="L")))

    clamped = Image.fromarray(clamped_arr, mode="RGB")
    return labels, clamped, color_layers
