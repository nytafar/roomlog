"""Visibility policy shared by legacy consumers and the recall projection."""
CHANNEL = """COALESCE((SELECT t.value FROM tags t WHERE t.target='segment' AND t.segment_id=s.id
AND t.key='channel' ORDER BY (t.source='deterministic') DESC,t.id DESC LIMIT 1),'ambient')"""
NOT_DICTATION = f"{CHANNEL} != 'dictation'"
