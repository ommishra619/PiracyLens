from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
import warnings
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import ExifTags, Image, ImageOps


SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP"}
NON_PRIVATE_INFO_KEYS = {
    "background",
    "dpi",
    "duration",
    "jfif",
    "jfif_version",
    "jfif_unit",
    "jfif_density",
    "loop",
    "timestamp",
}
METADATA_CATEGORIES = (
    "Location",
    "Capture time",
    "Device",
    "Personal details",
    "Technical details",
    "Other metadata",
)


@dataclass(frozen=True)
class ImageAnalysis:
    file_format: str
    width: int
    height: int
    metadata: tuple[tuple[str, str], ...]


def categorize_metadata(name: str) -> str:
    key = "".join(character for character in name.casefold() if character.isalnum())

    if any(
        token in key
        for token in ("gps", "latitude", "longitude", "geolocation", "altitude")
    ):
        return "Location"
    if any(token in key for token in ("datetime", "timestamp", "datecreated", "createdate")):
        return "Capture time"
    if any(
        token in key
        for token in (
            "artist",
            "author",
            "copyright",
            "description",
            "keyword",
            "ownername",
            "person",
            "subject",
            "usercomment",
            "xpcomment",
            "xpauthor",
            "xptitle",
        )
    ):
        return "Personal details"
    if any(
        token in key
        for token in ("make", "model", "software", "lens", "serialnumber", "camerainfo")
    ):
        return "Device"
    if any(
        token in key
        for token in (
            "icc",
            "dpi",
            "jfif",
            "gamma",
            "chromaticity",
            "colorspace",
            "exifversion",
            "orientation",
            "progress",
            "resolution",
        )
    ):
        return "Technical details"
    return "Other metadata"


def _format_value(value: object) -> str:
    if isinstance(value, bytes):
        return f"{len(value)} bytes"
    text = str(value)
    if len(text) > 160:
        return f"{text[:157]}..."
    return text


def _gps_coordinate(
    gps: dict[int, object], value_tag: int, reference_tag: int
) -> str | None:
    value = gps.get(value_tag)
    reference = gps.get(reference_tag)
    if not isinstance(value, tuple) or len(value) != 3:
        return None

    try:
        degrees, minutes, seconds = (float(part) for part in value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    coordinate = degrees + minutes / 60 + seconds / 3600
    if reference in ("S", "W"):
        coordinate = -coordinate
    return f"{coordinate:.6f} degrees"


def inspect_image(image_bytes: bytes) -> ImageAnalysis:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format not in SUPPORTED_FORMATS:
                raise ValueError("Only JPEG, PNG, and WebP images are supported.")
            image.load()

            metadata: list[tuple[str, str]] = []
            exif = image.getexif()
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo) if ExifTags.IFD.GPSInfo in exif else {}

            for tag_id, value in exif.items():
                name = ExifTags.TAGS.get(tag_id, f"EXIF tag {tag_id}")
                if tag_id == ExifTags.IFD.GPSInfo:
                    for gps_tag_id, gps_value in gps.items():
                        gps_name = ExifTags.GPSTAGS.get(
                            gps_tag_id, f"GPS tag {gps_tag_id}"
                        )
                        metadata.append((gps_name, _format_value(gps_value)))
                    latitude = _gps_coordinate(gps, 2, 1)
                    longitude = _gps_coordinate(gps, 4, 3)
                    if latitude is not None:
                        metadata.append(("GPS latitude (decimal)", latitude))
                    if longitude is not None:
                        metadata.append(("GPS longitude (decimal)", longitude))
                else:
                    metadata.append((name, _format_value(value)))

            for key, value in image.info.items():
                if key == "exif" or key in NON_PRIVATE_INFO_KEYS:
                    continue
                metadata.append((key, _format_value(value)))

            return ImageAnalysis(
                file_format=image.format,
                width=image.width,
                height=image.height,
                metadata=tuple(metadata),
            )


def sanitize_image(image_bytes: bytes) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(image_bytes)) as source:
            if source.format not in SUPPORTED_FORMATS:
                raise ValueError("Only JPEG, PNG, and WebP images are supported.")
            source.load()
            file_format = source.format
            oriented = ImageOps.exif_transpose(source)

            if file_format == "JPEG":
                cleaned = oriented.convert("RGB")
            elif file_format == "PNG":
                has_alpha = "A" in oriented.getbands() or "transparency" in source.info
                if oriented.mode == "P":
                    cleaned = oriented.convert("RGBA" if has_alpha else "RGB")
                else:
                    cleaned = oriented.copy()
            else:
                cleaned = oriented.copy()

            cleaned.info.clear()
            output = BytesIO()
            save_options: dict[str, object] = {}
            if file_format == "JPEG":
                save_options.update(quality=95, optimize=True)
            elif file_format == "WEBP":
                save_options.update(lossless=True)
            cleaned.save(output, format=file_format, **save_options)
            return output.getvalue()


def create_cleaned_zip(images: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    used_names: set[str] = set()

    with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as archive:
        for filename, image_bytes in images:
            safe_name = PurePosixPath(filename.replace("\\", "/")).name
            if safe_name in ("", ".", ".."):
                raise ValueError("Archive filenames must include a file name.")
            if safe_name.casefold() in used_names:
                raise ValueError(f"Duplicate archive filename: {safe_name}")
            used_names.add(safe_name.casefold())
            archive.writestr(safe_name, image_bytes)

    return output.getvalue()
