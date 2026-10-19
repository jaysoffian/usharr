"""Auto-generated migration.

Created: 2026-10-19 00:58:53
"""

depends_on = "0005_add_plex_posters"


def upgrade(ctx):
    """Apply migration."""
    ctx.add_column("plex_poster", {
    'name': 'style',
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
    ctx.drop_column("plex_poster", "style")
