#!/usr/bin/env python3
"""Verify a media snapshot or restore it into a NEW, isolated directory.

This ad hoc tool never starts containers or writes to live application paths.
"""

import sys

import snapshot_restore

if __name__ == '__main__':
    sys.exit(snapshot_restore.main(snapshot_restore.APPS, __doc__))
