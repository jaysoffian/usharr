"""Auto-generated migration.

Created: 2026-10-18 21:16:57
"""

depends_on = "0004_rename_ardetector_to_aspect_ratio"


def upgrade(ctx):
    """Apply migration."""
    ctx.create_table(
        "plex_poster",
        fields=[
            {
                'name': 'id',
                'python_type': 'int',
                'db_type': None,
                'nullable': True,
                'primary_key': True,
                'unique': False,
                'default': None,
                'auto_increment': False,
                'max_length': None,
                'max_digits': None,
                'decimal_places': None
            },
            {
                'name': 'edition',
                'python_type': 'str',
                'db_type': 'TEXT',
                'nullable': False,
                'primary_key': False,
                'unique': False,
                'default': None,
                'auto_increment': False,
                'max_length': None,
                'max_digits': None,
                'decimal_places': None
            },
            {
                'name': 'thumb',
                'python_type': 'str',
                'db_type': 'TEXT',
                'nullable': False,
                'primary_key': False,
                'unique': False,
                'default': None,
                'auto_increment': False,
                'max_length': None,
                'max_digits': None,
                'decimal_places': None
            },
            {
                'name': 'applied_at',
                'python_type': 'datetime',
                'db_type': None,
                'nullable': False,
                'primary_key': False,
                'unique': False,
                'default': None,
                'auto_increment': False,
                'max_length': None,
                'max_digits': None,
                'decimal_places': None
            },
            {
                'name': 'item_rating_key',
                'python_type': 'str',
                'db_type': None,
                'nullable': False,
                'primary_key': False,
                'unique': False,
                'default': None,
                'auto_increment': False,
                'max_length': None,
                'max_digits': None,
                'decimal_places': None
            }
        ],
        indexes=[
            {
                'name': 'plex_poster_item_uq',
                'fields': [
                    'item_rating_key'
                ],
                'unique': True,
                'method': None
            }
        ],
        foreign_keys=[
            {
                'name': 'fk_plex_poster_item_rating_key',
                'columns': [
                    'item_rating_key'
                ],
                'ref_table': 'plex_item',
                'ref_columns': [
                    'rating_key'
                ],
                'on_delete': 'CASCADE',
                'on_update': 'CASCADE'
            }
        ],
    )
    ctx.add_column("plex_item", {
    'name': 'edition',
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
    ctx.drop_column("plex_item", "edition")
    ctx.drop_table("plex_poster")
