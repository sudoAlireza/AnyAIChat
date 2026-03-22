import os
import logging

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".ogg", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".mp3", ".wav", ".webp"}
DATA_DIR = os.path.realpath(os.path.abspath("data"))


def safe_filename(file_id: str, original_name: str = None, prefix: str = "file") -> str:
    """
    Build a safe file path using file_id as the name.
    Validates extension against allowlist and ensures path stays within data/.
    """
    ext = ""
    if original_name:
        _, ext = os.path.splitext(original_name)
        ext = ext.lower()

    if ext and ext not in ALLOWED_EXTENSIONS:
        logger.warning(f"Blocked file extension: {ext} for file_id={file_id}")
        ext = ".bin"

    filename = f"{prefix}_{file_id}{ext}"

    # Remove any path traversal characters from file_id
    filename = filename.replace("/", "_").replace("\\", "_").replace("..", "_")

    full_path = os.path.join(DATA_DIR, filename)

    # Verify the resolved path is still inside data/ using commonpath
    real_path = os.path.realpath(full_path)
    try:
        common = os.path.commonpath([real_path, DATA_DIR])
        if common != DATA_DIR:
            raise ValueError("Path escapes data directory")
    except ValueError:
        logger.error(f"Path traversal attempt detected: {full_path} -> {real_path}")
        raise ValueError("Invalid file path")

    return full_path
