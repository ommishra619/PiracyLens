from collections import defaultdict
from hashlib import sha256
from pathlib import PurePosixPath
from typing import TypedDict

import streamlit as st
from PIL import Image, UnidentifiedImageError

from privacy_lens import (
    ImageAnalysis,
    METADATA_CATEGORIES,
    categorize_metadata,
    create_cleaned_zip,
    inspect_image,
    sanitize_image,
)


MAX_BATCH_FILES = 10
MAX_BATCH_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024
IMAGE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    ValueError,
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
)
FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
SENSITIVE_CATEGORIES = {"Location", "Personal details"}
PERSONAL_CATEGORIES = SENSITIVE_CATEGORIES | {"Capture time", "Device"}


class ProcessedImage(TypedDict):
    filename: str
    download_name: str
    original: bytes
    cleaned: bytes
    analysis: ImageAnalysis


def _batch_fingerprint(uploads: list[tuple[str, bytes]]) -> str:
    digest = sha256()
    for filename, image_bytes in uploads:
        encoded_name = filename.encode("utf-8", errors="replace")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(image_bytes).to_bytes(8, "big"))
        digest.update(image_bytes)
    return digest.hexdigest()


def _clean_filename(
    source_name: str, file_format: str, used_names: set[str]
) -> str:
    safe_source_name = PurePosixPath(source_name.replace("\\", "/")).name
    stem = PurePosixPath(safe_source_name).stem or "image"
    base = f"{stem}_clean"
    extension = FORMAT_EXTENSIONS[file_format]
    candidate = f"{base}{extension}"
    suffix = 2
    while candidate.casefold() in used_names:
        candidate = f"{base}_{suffix}{extension}"
        suffix += 1
    used_names.add(candidate.casefold())
    return candidate


def _show_metadata(analysis: ImageAnalysis) -> None:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for name, value in analysis.metadata:
        category = categorize_metadata(name)
        groups[category].append({"Field": name, "Value": value})

    present_categories = set(groups)
    if present_categories & SENSITIVE_CATEGORIES:
        st.error(
            "Potentially sensitive location or personal details were found. "
            "Review these before sharing the original."
        )
    elif present_categories & PERSONAL_CATEGORIES:
        st.warning("Capture time or device details may reveal information about you.")

    for category in METADATA_CATEGORIES:
        if category not in groups:
            continue
        with st.expander(
            f"{category} ({len(groups[category])})",
            expanded=category in SENSITIVE_CATEGORIES,
        ):
            st.table(groups[category])


def _process_uploads(
    uploads: list[tuple[str, bytes, ImageAnalysis]], fingerprint: str
) -> None:
    processed: list[ProcessedImage] = []
    used_names: set[str] = set()

    for filename, image_bytes, original_analysis in uploads:
        try:
            cleaned_bytes = sanitize_image(image_bytes)
            cleaned_analysis = inspect_image(cleaned_bytes)
            if cleaned_analysis.metadata:
                remaining = ", ".join(name for name, _ in cleaned_analysis.metadata)
                raise ValueError(f"Metadata remains after cleaning: {remaining}")
        except IMAGE_ERRORS as error:
            st.error(f"Could not clean {filename}: {error}")
            continue

        processed.append(
            {
                "filename": filename,
                "download_name": _clean_filename(
                    filename, original_analysis.file_format, used_names
                ),
                "original": image_bytes,
                "cleaned": cleaned_bytes,
                "analysis": cleaned_analysis,
            }
        )

    archive = b""
    if len(uploads) > 1 and processed:
        archive = create_cleaned_zip(
            [
                (str(item["download_name"]), bytes(item["cleaned"]))
                for item in processed
            ]
        )
    st.session_state["cleaned_result"] = {
        "fingerprint": fingerprint,
        "items": processed,
        "archive": archive,
    }


st.set_page_config(page_title="PrivacyLens", layout="centered")
st.title("PrivacyLens")
st.write(
    "Inspect image metadata, spot privacy-sensitive details, and create a cleaned copy."
)
st.caption(
    "When run locally, files stay on your machine and are processed in memory. "
    "If hosted, uploads are sent to that host; the app does not forward them to "
    "third-party services or save them to disk."
)

uploaded_files = st.file_uploader(
    "Choose one or more photos",
    type=("jpg", "jpeg", "png", "webp"),
    accept_multiple_files=True,
    help="Supports JPEG, PNG, and WebP; up to 10 files and 100 MB total.",
)

if not uploaded_files:
    st.session_state.pop("cleaned_result", None)
else:
    uploads = [(uploaded.name, uploaded.getvalue()) for uploaded in uploaded_files]
    total_bytes = sum(len(image_bytes) for _, image_bytes in uploads)
    too_large_files = [
        filename
        for filename, image_bytes in uploads
        if len(image_bytes) > MAX_FILE_BYTES
    ]

    if len(uploads) > MAX_BATCH_FILES:
        st.session_state.pop("cleaned_result", None)
        st.error(f"Select no more than {MAX_BATCH_FILES} images at a time.")
    elif too_large_files:
        st.session_state.pop("cleaned_result", None)
        st.error(
            f"Each image must be 25 MB or smaller. Too large: "
            f"{', '.join(too_large_files)}"
        )
    elif total_bytes > MAX_BATCH_BYTES:
        st.session_state.pop("cleaned_result", None)
        st.error("The selected images exceed the 100 MB total batch limit.")
    else:
        fingerprint = _batch_fingerprint(uploads)
        saved_result = st.session_state.get("cleaned_result")
        if saved_result and saved_result["fingerprint"] != fingerprint:
            st.session_state.pop("cleaned_result", None)
            saved_result = None

        valid_uploads: list[tuple[str, bytes, object]] = []
        for filename, image_bytes in uploads:
            st.subheader(filename)
            try:
                analysis = inspect_image(image_bytes)
            except IMAGE_ERRORS as error:
                st.error(f"Could not read this image: {error}")
                continue

            valid_uploads.append((filename, image_bytes, analysis))
            preview_column, details_column = st.columns((1, 2))
            with preview_column:
                st.image(image_bytes, caption="Original", width=240)
            with details_column:
                dimensions, file_format, metadata_count = st.columns(3)
                dimensions.metric(
                    "Dimensions", f"{analysis.width} × {analysis.height}"
                )
                file_format.metric("Format", analysis.file_format)
                metadata_count.metric("Metadata fields", len(analysis.metadata))
                if analysis.metadata:
                    _show_metadata(analysis)
                else:
                    st.success("No embedded metadata was detected.")

        if valid_uploads:
            st.warning(
                "Cleaning re-encodes images. JPEG output may lose a little quality, "
                "and embedded color profiles are not retained."
            )
        if st.button(
            "Clean and verify valid images",
            type="primary",
            disabled=not valid_uploads,
        ):
            _process_uploads(valid_uploads, fingerprint)
            saved_result = st.session_state["cleaned_result"]

        if saved_result and saved_result["fingerprint"] == fingerprint:
            cleaned_items = saved_result["items"]
            if cleaned_items:
                st.subheader("Before and after")
                for item in cleaned_items:
                    st.markdown(f"**{item['filename']}**")
                    before, after = st.columns(2)
                    with before:
                        st.image(item["original"], caption="Original", use_container_width=True)
                    with after:
                        st.image(item["cleaned"], caption="Cleaned", use_container_width=True)
                    if not item["analysis"].metadata:
                        st.success("Re-check passed: no embedded metadata detected.")
                    else:
                        st.error("Re-check found metadata in the cleaned image.")

                if len(uploads) > 1:
                    st.download_button(
                        "Download cleaned images (ZIP)",
                        data=saved_result["archive"],
                        file_name="privacylens_cleaned_images.zip",
                        mime="application/zip",
                    )
                else:
                    item = cleaned_items[0]
                    file_format = item["analysis"].file_format
                    mime_type = (
                        "image/jpeg" if file_format == "JPEG" else f"image/{file_format.lower()}"
                    )
                    st.download_button(
                        "Download cleaned image",
                        data=item["cleaned"],
                        file_name=item["download_name"],
                        mime=mime_type,
                    )
            else:
                st.error("No images were cleaned successfully.")
