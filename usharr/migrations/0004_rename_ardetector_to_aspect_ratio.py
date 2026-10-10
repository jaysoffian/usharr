"""Rename the ardetector table to aspect_ratio.

SQLite cannot rename a foreign-key constraint, and Oxyde derives the
constraint name from the table name, so the table is rebuilt rather than
renamed in place.
"""

depends_on = "0003_add_ardetector_timeline"

COLUMNS = (
    "id",
    "error",
    "aspect_primary",
    "aspect_widest",
    "aspect_samples",
    "color_pct",
    "video_path",
    "timeline",
)


def column(name, python_type, db_type=None, nullable=True, primary_key=False):
    return {
        "name": name,
        "python_type": python_type,
        "db_type": db_type,
        "nullable": nullable,
        "primary_key": primary_key,
        "unique": False,
        "default": None,
        "auto_increment": False,
        "max_length": None,
        "max_digits": None,
        "decimal_places": None,
    }


def create(ctx, table):
    ctx.create_table(
        table,
        fields=[
            column("id", "int", primary_key=True),
            column("error", "str", db_type="TEXT"),
            column("aspect_primary", "float"),
            column("aspect_widest", "float"),
            column("aspect_samples", "str", db_type="TEXT"),
            column("color_pct", "float"),
            column("video_path", "str", nullable=False),
            column("timeline", "str", db_type="TEXT"),
        ],
        indexes=[
            {
                "name": f"{table}_video_uq",
                "fields": ["video_path"],
                "unique": True,
                "method": None,
            }
        ],
        foreign_keys=[
            {
                "name": f"fk_{table}_video_path",
                "columns": ["video_path"],
                "ref_table": "video_file",
                "ref_columns": ["path"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            }
        ],
    )


def move(ctx, old, new):
    create(ctx, new)
    cols = ", ".join(COLUMNS)
    ctx.execute(f"INSERT INTO {new} ({cols}) SELECT {cols} FROM {old}")
    ctx.drop_table(old)


def upgrade(ctx):
    move(ctx, "ardetector", "aspect_ratio")


def downgrade(ctx):
    move(ctx, "aspect_ratio", "ardetector")
