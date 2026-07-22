"""Pydantic v2 request/response schemas for the auth + household slice.

Response schemas never expose password hashes, raw/hashed session tokens or internal
integer PKs — only the public UUID and non-sensitive attributes.
"""
