"""Oxyde ORM configuration (used by the `oxyde` CLI: makemigrations/migrate).

At runtime usharr applies migrations programmatically (see usharr/database.py),
so this file is only needed for the development CLI. The connection URL mirrors
usharr/database.py's USHARR_DB resolution.
"""

import os

MODELS = ["usharr.models"]
DIALECT = "sqlite"
MIGRATIONS_DIR = "usharr/migrations"

db_path = os.environ.get("USHARR_DB", "/config/usharr.db")
DATABASES = {"default": f"sqlite://{db_path}"}
