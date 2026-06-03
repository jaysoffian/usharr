"""Oxyde ORM configuration (used by the `oxyde` CLI: makemigrations/migrate).

At runtime usharr applies migrations programmatically (see usharr/database.py),
so this file is only needed for the development CLI. The connection URL mirrors
usharr/db.py's USHARR_DB resolution.
"""

import os

MODELS = ["usharr.models"]
DIALECT = "sqlite"
MIGRATIONS_DIR = "usharr/migrations"

_db_path = os.environ.get("USHARR_DB", "/config/usharr.db")
DATABASES = {"default": f"sqlite://{_db_path}"}
