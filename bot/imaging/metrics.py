"""Cheap programmatic geometry check: SSIM over Canny edge maps (mold vs candidate)."""
from __future__ import annotations

import io
import logging

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity

logger = logging.getLogger(__name__)

TARGET_SIZE = (512, 384)
JPEG_QUALITY = 95  # keep detail: this quality hits every model-facing re-encode


def load_gray(image_bytes: bytes, size: tuple[int, int] = TARGET_SIZE) -> np.ndarray:
    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    img = img.resize(size, Image.LANCZOS)
    return np.asarray(img, dtype=np.float64)


def edge_map(gray: np.ndarray) -> np.ndarray:
    from skimage import feature

    sigma = 2.0
    lo, hi = 0.08 * gray.max(), 0.18 * gray.max()
    edges = feature.canny(gray, sigma=sigma, low_threshold=lo, high_threshold=hi)
    return edges.astype(np.float64)


def edge_ssim(mold_bytes: bytes, candidate_bytes: bytes) -> float:
    """0..1 similarity of edge structure; 1 = identical geometry layout."""
    try:
        a = edge_map(load_gray(mold_bytes))
        b = edge_map(load_gray(candidate_bytes))
        # smooth the binary maps a bit so SSIM is less brittle
        from skimage import filters

        a = filters.gaussian(a, sigma=1.5)
        b = filters.gaussian(b, sigma=1.5)
        score = structural_similarity(a, b, data_range=float(max(a.max(), b.max(), 1e-6)))
        return float(max(0.0, min(1.0, score)))
    except Exception as e:  # pragma: no cover
        logger.warning("edge_ssim failed: %s", e)
        return 0.5  # neutral value: do not punish candidate on metric failure


def resize_image(image_bytes: bytes, max_side: int = 1024) -> bytes:
    """Downscale for model input / thumbnails, keep as JPEG."""
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def thumbnail_jpeg(image_bytes: bytes, max_side: int = 128) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70)
    return out.getvalue()
