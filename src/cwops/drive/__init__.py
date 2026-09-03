"""Google Drive access, behind a narrow Protocol.

The Protocol is the seam that lets every safety gate be built and tested before
any real Google call exists, and lets the whole system run offline in demo mode.
"""

from .fake import FakeDriveClient
from .protocol import MUTATING_METHODS, DriveClient

__all__ = ["MUTATING_METHODS", "DriveClient", "FakeDriveClient"]
