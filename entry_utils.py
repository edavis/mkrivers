"Utilities for processing/cleaning new feed entries"

import arrow
import bleach
from datetime import datetime

RIVER_CHAR_LIMIT = 280 # character limit in item description

def entry_fingerprint(entry):
    if entry.get('guid'):
        return entry.get('guid')
    else:
        return '|'.join([entry.get('title', ''), entry.get('link', '')])

def entry_timestamp(entry):
    for key in ['published_parsed', 'updated_parsed', 'created_parsed']:
        if not entry.get(key):
            continue

        obj = entry[key][:6]
        reported = arrow.get(datetime(*obj))

        # Return Jan 1, 1970 if item pubDate is pre-2000
        if reported < arrow.Arrow(2000, 1, 1):
            return arrow.Arrow(1970, 1, 1)

        # Return reported pubDate only if in the past (i.e., nothing in future)
        elif reported < arrow.utcnow():
            return reported

    # If all else fails, return current UTC as pubDate
    return arrow.utcnow()

def clean_text(text, limit=RIVER_CHAR_LIMIT, suffix=u'\u2026'):
    "Trim text to limit, adding suffix if text is longer than limit."
    cleaned = bleach.clean(text, tags=[], strip=True).strip()

    if len(cleaned) > limit:
        # trim to first space rather than cutting a word off in the middle of it
        cur_idx = limit - 1
        cur_char = cleaned[cur_idx]
        while cur_char != ' ':
            cur_idx -= 1
            cur_char = cleaned[cur_idx]

        c = cleaned[:cur_idx].strip('.,')
        return c + suffix
    else:
        return cleaned

def entry_text(entry):
    "Populate the title and body, depending on the existence and value of the other."
    obj = {}

    if not entry.get('title') and not entry.get('description'):
        return

    elif entry.get('title') and entry.get('description'):
        obj['title'] = entry.get('title')
        if entry.get('title') != entry.get('description'):
            obj['body'] = entry.get('description')
        else:
            obj['body'] = ''

    elif not entry.get('title') and entry.get('description'):
        obj = {
            'title': entry.get('description'),
            'body': '',
        }

    elif entry.get('title'):
        obj = {
            'title': entry.get('title'),
            'body': '',
        }

    return {
        'title': clean_text(obj['title']),
        'body': clean_text(obj['body']),
    }
