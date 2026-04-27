"""Celery worker package.

Long-running scheduling jobs and notification dispatch live here so the API
process stays responsive (PRD §1.3 — "non-blocking scheduling").
"""
