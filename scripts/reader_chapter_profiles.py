"""Explicit, format-scoped chapter profile migrations.

A new current profile alone does not authorize a whole-format rebuild. Record
its reviewed predecessor set here after validating a representative canary.
Ordinary source changes still enter the queue irrespective of profile.
"""

CHM_PROFILE = 'chm-epub-chapters-v10-exact-sections'

REVIEWED_PREDECESSORS = {
    ('chm', 'chm-epub-chapters-v10-exact-sections'): frozenset({'chm-epub-chapters-v9-source-text'}),
}


def chm_profile_upgrade(prior):
    return isinstance(prior, str) and prior in REVIEWED_PREDECESSORS.get(('chm', CHM_PROFILE), ())
