"""Размытая копия фото для получателя без премиума. Исходные байты наружу не уходят."""

from __future__ import annotations

import io

from PIL import Image, ImageFilter, ImageOps

MAX_SIDE = 480
SHRINK = 16
BLUR_RADIUS = 6
JPEG_QUALITY = 70


def blur_photo(data: bytes) -> bytes:
    """JPEG до 480 px по большей стороне, сильно размытый и без EXIF.

    Битый файл или слишком большая картинка (DecompressionBombError) дают исключение.
    """
    with Image.open(io.BytesIO(data)) as img:
        if img.format == "JPEG":
            img.draft("RGB", (512, 512))
        img.seek(0)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    size = img.size
    small = img.resize((max(1, size[0] // SHRINK), max(1, size[1] // SHRINK)), Image.Resampling.BOX)
    img = small.resize(size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()
