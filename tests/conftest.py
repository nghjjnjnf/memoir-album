from __future__ import annotations

import os
import tempfile


os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="life-story-agent-tests-")
os.environ["USE_MOCK_LLM"] = "true"
os.environ["VISION_MODE"] = "disabled"
