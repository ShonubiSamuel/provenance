"""Test-wide environment defaults. Imported by pytest before any test module, so these
are set before the app or settings are first constructed."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Never spawn a real aria2c daemon during tests — endpoints override the manager with a
# fake, and lifespan should not bind a port / write to ~/Downloads.
os.environ["DOWNLOADS_AUTOSTART"] = "false"
