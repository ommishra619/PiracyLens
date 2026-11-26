from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image, PngImagePlugin, UnidentifiedImageError

from privacy_lens import (
    categorize_metadata,
    create_cleaned_zip,
    inspect_image,
    sanitize_image,
)


def make_image(image_format: str, **save_options: object) -> bytes:
    image = Image.new("RGB", (4, 3), color="navy")
    output = BytesIO()
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def test_inspect_image_finds_exif_and_sanitized_copy_removes_it() -> None:
    exif = Image.Exif()
    exif[36867] = "2026:11:26 10:00:00"
    original = make_image("JPEG", exif=exif)

    analysis = inspect_image(original)
    cleaned = sanitize_image(original)

    assert analysis.file_format == "JPEG"
    assert ("DateTimeOriginal", "2026:11:26 10:00:00") in analysis.metadata
    assert inspect_image(cleaned).metadata == ()


def test_png_text_metadata_is_removed() -> None:
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("Comment", "sample location note")
    original = make_image("PNG", pnginfo=png_info)

    analysis = inspect_image(original)
    cleaned = sanitize_image(original)

    assert ("Comment", "sample location note") in analysis.metadata
    assert inspect_image(cleaned).metadata == ()


def test_inspect_image_displays_decimal_gps_coordinates() -> None:
    exif = Image.Exif()
    exif[34853] = {
        1: "N",
        2: (40, 42, 51),
        3: "W",
        4: (73, 59, 12),
    }
    original = make_image("JPEG", exif=exif)

    analysis = inspect_image(original)

    assert ("GPS latitude (decimal)", "40.714167 degrees") in analysis.metadata
    assert ("GPS longitude (decimal)", "-73.986667 degrees") in analysis.metadata


def test_metadata_is_grouped_by_privacy_relevance() -> None:
    assert categorize_metadata("GPSLatitude") == "Location"
    assert categorize_metadata("DateTimeOriginal") == "Capture time"
    assert categorize_metadata("Model") == "Device"
    assert categorize_metadata("UserComment") == "Personal details"
    assert categorize_metadata("ICC_Profile") == "Technical details"


def test_sanitization_applies_exif_orientation() -> None:
    exif = Image.Exif()
    exif[274] = 6
    original = make_image("JPEG", exif=exif)

    cleaned = sanitize_image(original)
    analysis = inspect_image(cleaned)

    assert (analysis.width, analysis.height) == (3, 4)
    assert analysis.metadata == ()


def test_png_transparency_is_preserved_when_cleaning() -> None:
    image = Image.new("RGBA", (2, 2), color=(20, 40, 60, 0))
    image.putpixel((0, 0), (20, 40, 60, 255))
    output = BytesIO()
    image.save(output, format="PNG")

    cleaned = sanitize_image(output.getvalue())

    with Image.open(BytesIO(cleaned)) as result:
        assert result.mode == "RGBA"
        assert result.getpixel((0, 0)) == (20, 40, 60, 255)
        assert result.getpixel((1, 1)) == (20, 40, 60, 0)
    assert inspect_image(cleaned).metadata == ()


def test_webp_is_supported_and_cleaned() -> None:
    original = make_image("WEBP")

    cleaned = sanitize_image(original)
    analysis = inspect_image(cleaned)

    assert analysis.file_format == "WEBP"
    assert (analysis.width, analysis.height) == (4, 3)
    assert analysis.metadata == ()


def test_image_without_metadata_is_reported_as_clean() -> None:
    analysis = inspect_image(make_image("PNG"))

    assert analysis.metadata == ()


def test_invalid_image_is_rejected() -> None:
    with pytest.raises(UnidentifiedImageError):
        inspect_image(b"not an image")


def test_cleaned_zip_flattens_paths_and_contains_each_image() -> None:
    archive_bytes = create_cleaned_zip(
        [
            ("../first_clean.png", b"first"),
            (r"folder\second_clean.webp", b"second"),
        ]
    )

    with ZipFile(BytesIO(archive_bytes)) as archive:
        assert archive.namelist() == ["first_clean.png", "second_clean.webp"]
        assert archive.read("first_clean.png") == b"first"
        assert archive.read("second_clean.webp") == b"second"
