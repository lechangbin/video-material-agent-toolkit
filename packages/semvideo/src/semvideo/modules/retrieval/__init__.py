"""Projection of video understanding results into retrieval-ready records."""

from .records import RetrievalSegmentRecord, build_record, list_records, write_records

__all__ = ["RetrievalSegmentRecord", "build_record", "list_records", "write_records"]
