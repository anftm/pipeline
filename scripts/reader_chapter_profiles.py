"""Explicit, format-scoped chapter profile migrations.

A new current profile alone does not authorize a whole-format rebuild. Record
its reviewed predecessor set here after validating a representative canary.
Ordinary source changes still enter the queue irrespective of profile.
"""

CHM_PROFILE = 'chm-epub-chapters-v10-exact-sections'
EPUB_PROFILE = 'epub-chapters-v10-native-navigation'
KINDLE_PROFILE = 'kindle-chapters-v10-native-navigation'
FB2_PROFILE = 'fb2-chapters-v10-navigation'
LEGACY_EBOOK_PROFILE = 'epub-chapters-v9-cover-toc'

EBOOK_REBUILDS = {
    'epub': (EPUB_PROFILE, frozenset({None, 'epub-chapters-v8-bucket', LEGACY_EBOOK_PROFILE})),
    'mobi': (KINDLE_PROFILE, frozenset({LEGACY_EBOOK_PROFILE})),
    'azw3': (KINDLE_PROFILE, frozenset({None, LEGACY_EBOOK_PROFILE})),
    'fb2': (FB2_PROFILE, frozenset({LEGACY_EBOOK_PROFILE})),
}


def ebook_profile_upgrade(extension, prior):
    """Identify reviewed legacy tags without treating unknown/future tags as old."""
    if prior is not None and not isinstance(prior, str):
        return None
    target, predecessors = EBOOK_REBUILDS.get(extension, (None, ()))
    return target if prior in predecessors else None


REVIEWED_PREDECESSORS = {
    ('chm', 'chm-epub-chapters-v10-exact-sections'): frozenset({'chm-epub-chapters-v9-source-text'}),
}


def chm_profile_upgrade(prior):
    return isinstance(prior, str) and prior in REVIEWED_PREDECESSORS.get(('chm', CHM_PROFILE), ())
