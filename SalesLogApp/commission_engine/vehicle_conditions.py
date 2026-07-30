def normalize_vehicle_condition(value):
    """Return the canonical automotive condition used by imports and calculations."""
    normalized = str(value or '').strip().lower().replace('_', ' ')
    normalized = normalized.replace('-', ' ')
    normalized = ' '.join(normalized.split())
    if normalized == 'new':
        return 'new'
    if normalized in {'used', 'pre owned', 'preowned', 'retired sslp'}:
        return 'used'
    return ''
