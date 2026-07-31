"""Importing fabric.app triggers create_app() -> load_config() at module
level (gunicorn imports it as `fabric.app:app`), so merely importing the
module requires FABRIC_ADDRESS/FABRIC_USERNAME/FABRIC_PASSWORD to be set.
Seed harmless dummies before collection so that import never fails here.

Tests that actually exercise auth behaviour build their own app via
create_app(config=...) with an explicit FabricConfig instead of relying on
these — they stay decoupled from whatever is in the environment.
"""

import os

os.environ.setdefault("FABRIC_ADDRESS", "fabric.test.invalid:51820")
os.environ.setdefault("FABRIC_USERNAME", "test-admin")
os.environ.setdefault("FABRIC_PASSWORD", "test-password")
