"""Test-run isolation. Runs before any test module imports app/models, so
every data/, config/ and ~/.repodoctor path points at a throwaway directory
and a test run never pollutes the real debug log, groups, or credentials."""

import os
import tempfile

os.environ.setdefault("REPODOCTOR_HOME", tempfile.mkdtemp(prefix="repodoctor-test-"))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
