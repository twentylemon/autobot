"""Stub for v0. v0.1+ will use PyGithub here to poll PR comments and merge state.

In v0, all GitHub mutations are performed by Claude itself via the `gh` CLI;
the worker only reads back the structured result file Claude writes.
"""
