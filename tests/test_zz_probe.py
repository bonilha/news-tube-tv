"""Temporary probe left by a failed cleanup; no tests live here.

Windows refuses to delete this file from inside the session, so it stays as an
empty module until it can be removed by hand:

    del tests\\test_zz_probe.py
"""