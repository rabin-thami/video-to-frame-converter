"""Application-specific exceptions with user-readable messages."""


class VideoFrameExtractorError(Exception):
    """Base class for errors that should be shown to the user."""


class ValidationError(VideoFrameExtractorError):
    """One or more settings are invalid."""


class VideoError(VideoFrameExtractorError):
    """The video cannot be opened or read."""


class ExtractionCancelled(VideoFrameExtractorError):
    """The user cancelled the extraction."""
