"""Auto-generated migration.

Created: 2026-10-08 19:39:44
"""

depends_on = "0002_add_video_path_indexes"


def upgrade(ctx):
    """Apply migration."""
    ctx.add_column("ardetector", {
    'name': 'timeline',
    'python_type': 'str',
    'db_type': 'TEXT',
    'nullable': True,
    'primary_key': False,
    'unique': False,
    'default': None,
    'auto_increment': False,
    'max_length': None,
    'max_digits': None,
    'decimal_places': None
})


def downgrade(ctx):
    """Revert migration."""
    ctx.drop_column("ardetector", "timeline")
