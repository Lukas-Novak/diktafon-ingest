import os
import tempfile

# Config is read at import time — set the environment before `app` is touched.
os.environ["DIKTAFON_INGEST_TOKEN"] = "test-token"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="diktafon-ingest-test-")

TEST_TOKEN = "test-token"
