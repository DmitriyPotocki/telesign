import pathlib

from PIL import Image

from bot.imaging.metrics import edge_ssim, resize_image, thumbnail_jpeg

FIX = pathlib.Path(__file__).parent / "fixtures"
MOLD = FIX / "mold.png"
CAND = FIX / "candidate.png"


def test_edge_ssim_identical_is_high():
    data = MOLD.read_bytes()
    assert edge_ssim(data, data) > 0.9


def test_edge_ssim_different_is_lower():
    mold = MOLD.read_bytes()
    cand = CAND.read_bytes()
    s_diff = edge_ssim(mold, cand)
    s_same = edge_ssim(mold, mold)
    assert s_diff < s_same


def test_edge_ssim_bad_input_neutral():
    assert edge_ssim(b"not an image", b"not an image") == 0.5


def test_resize_image():
    img = Image.new("RGB", (2000, 1000), (200, 100, 50))
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = resize_image(buf.getvalue(), max_side=512)
    img2 = Image.open(io.BytesIO(out))
    assert max(img2.size) <= 512
    assert img2.format == "JPEG"


def test_thumbnail_jpeg():
    import io
    img = Image.new("RGB", (800, 600), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    thumb = thumbnail_jpeg(buf.getvalue())
    img2 = Image.open(io.BytesIO(thumb))
    assert max(img2.size) <= 128
