"""Auto-generated migration.

Created: 2026-06-03 20:52:59
"""

depends_on = "0001_initial"


def upgrade(ctx):
    """Apply migration."""
    ctx.create_index("plex_item", {
    'name': 'plex_item_video_idx',
    'fields': [
        'video_path'
    ],
    'unique': False,
    'method': None
})
    ctx.create_index("movie", {
    'name': 'movie_video_idx',
    'fields': [
        'video_path'
    ],
    'unique': False,
    'method': None
})
    ctx.create_index("subtitle_file", {
    'name': 'subtitle_file_video_idx',
    'fields': [
        'video_path'
    ],
    'unique': False,
    'method': None
})


def downgrade(ctx):
    """Revert migration."""
    ctx.drop_index("subtitle_file", "subtitle_file_video_idx")
    ctx.drop_index("movie", "movie_video_idx")
    ctx.drop_index("plex_item", "plex_item_video_idx")
