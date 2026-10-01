"""Bounded summaries and listings over the recall projection."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .query_policy import CHANNEL
from .recall import LIMITS, RECIPIENTS, RecallError, bad, check_budget, item, overlap, policy


def calendar_edges(p):
    """UTC edges of valid local calendar hours/days, clipped to the request."""
    zone = ZoneInfo(p['tz'])
    start, end = p['from_ms'], p['to_ms']
    first = datetime.fromtimestamp(start / 1000, zone).replace(tzinfo=None)
    last = datetime.fromtimestamp(end / 1000, zone).replace(tzinfo=None)
    day = first.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    stop = last.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)
    step = timedelta(hours=1) if p['bucket'] == 'hour' else timedelta(days=1)
    edges = {start, end}
    while day <= stop:
        for fold in (0, 1):
            local = day.replace(tzinfo=zone, fold=fold)
            stamp = int(local.timestamp() * 1000)
            roundtrip = datetime.fromtimestamp(stamp / 1000, zone).replace(tzinfo=None)
            # Missing hours have no boundary. For days, a missing midnight advances
            # to that date's first represented instant (e.g. historical Sao Paulo).
            valid = roundtrip == day or (p['bucket'] == 'day' and roundtrip.date() == day.date() and roundtrip > day)
            if valid and start < stamp < end:
                edges.add(stamp)
        day += step
    result = sorted(edges)
    if p['bucket'] == 'hour' and len(result) - 1 > LIMITS['hourly_buckets']:
        bad('Too many hourly buckets', 'bucket')
    return result


def merged(intervals):
    result = []
    for a, b in sorted(intervals):
        check_budget()
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result


def durations(intervals, edges):
    values = [0] * (len(edges) - 1)
    for a, b in merged(intervals):
        i = max(0, bisect_right(edges, a) - 1)
        while i < len(values) and edges[i] < b:
            check_budget()
            values[i] += max(0, min(b, edges[i + 1]) - max(a, edges[i]))
            i += 1
    return values


def subtract(intervals, removed):
    result = []
    cuts = merged(removed)
    index = 0
    for a, b in merged(intervals):
        while index < len(cuts) and cuts[index][1] <= a:
            index += 1
        i = index
        while i < len(cuts) and cuts[i][0] < b:
            check_budget()
            c, d = cuts[i]
            if a < c:
                result.append((a, min(c, b)))
            a = max(a, d)
            if a >= b:
                break
            i += 1
        if a < b:
            result.append((a, b))
    return result


def timeline(reader, conn, p, stamp):
    try:
        edges = calendar_edges(p)
    except (ValueError, OverflowError, OSError):
        bad('Time range cannot be represented in this timezone', 'from_ms')
    sources = reader.sources(conn)
    if p.get('device_id'):
        sources = [s for s in sources if s['device_id'] == p['device_id']]
    if len(sources) * (len(edges) - 1) > LIMITS['timeline_rows']:
        bad('Too many source/bucket combinations', 'device_id')
    intervals = defaultdict(lambda: defaultdict(list))
    counts = {s['device_id']: [0] * len(edges) for s in sources}
    where, args = policy(p)
    where += [overlap()]
    args += [p['to_ms'], p['from_ms'], p['from_ms']]
    rows = conn.execute(f'''SELECT s.device_id,s.start_utc_ms,s.end_utc_ms
        FROM segments s LEFT JOIN chunks c ON c.id=s.chunk_id
        WHERE {' AND '.join(where)} LIMIT ?''', args + [LIMITS['aggregate_rows'] + 1])
    for n, r in enumerate(rows):
        aggregate_budget(n)
        device, a, b = r
        if device not in counts:
            continue
        intervals[device]['text'].append((a, b))
        i = max(0, bisect_right(edges, a) - 1)
        j = min(len(edges) - 1, bisect_left(edges, b)) if b > a else i + 1
        counts[device][i] += 1
        counts[device][j] -= 1
    args = [p['to_ms'], p['from_ms'], p['from_ms']]
    device_filter = ''
    if p.get('device_id'):
        device_filter = ' AND device_id=?'
        args += [p['device_id']]
    evidence = conn.execute(f'''SELECT device_id,start_utc_ms,end_utc_ms,status,kind
        FROM chunks WHERE {overlap('chunks')}{device_filter}
        UNION ALL SELECT device_id,start_utc_ms,end_utc_ms,status,'raw' FROM
        (SELECT *,start_utc_ms+n_samples*1000/16000 end_utc_ms FROM raw_segments) raw
        WHERE {overlap('raw')}{device_filter} LIMIT ?''', args + args + [LIMITS['aggregate_rows'] + 1])
    for n, r in enumerate(evidence):
        aggregate_budget(n)
        device, a, b, status, kind = r
        group = intervals[device]
        group['captured'].append((a, b))
        if status in ('pending', 'failed'):
            group[status].append((a, b))
        if kind == 'raw' and status == 'segmented':
            group['segmented'].append((a, b))
        if kind == 'derived':
            group['derived'].append((a, b))
    rows = []
    for source in sources:
        device = source['device_id']
        group = intervals[device]
        group['no_detected_speech'] = subtract(group['segmented'], group['derived'])
        values = {k: durations(group[k], edges) for k in ('text', 'captured', 'pending', 'failed', 'no_detected_speech')}
        count = 0
        for i, (a, b) in enumerate(zip(edges, edges[1:])):
            count += counts[device][i]
            evidence = {k + '_ms': values[k][i] for k in ('captured', 'pending', 'failed', 'no_detected_speech')}
            evidence['unknown_ms'] = b - a - values['captured'][i]
            rows.append(dict(device_id=device, from_ms=a, to_ms=b, text_ms=values['text'][i], n_segments=count, evidence=evidence))
    result = dict(as_of_ms=stamp, tz=p['tz'], bucket=p['bucket'], rows=rows)
    reader.check_response(result)
    return result


def aggregate_budget(n):
    check_budget()
    if n >= LIMITS['aggregate_rows']:
        raise RecallError(503, 'read_budget_exhausted', 'Summary exceeds work budget; narrow the window')


def sessions(reader, conn, p, stamp):
    where, args = [overlap('se')], [p['to_ms'], p['from_ms'], p['from_ms']]
    if p.get('device_id'):
        where.append('se.device_id=?')
        args.append(p['device_id'])
    key = reader.cursor('sessions', p, stamp)
    if key:
        where.append('(se.start_utc_ms,se.device_id,se.id)>(?,?,?)')
        args += key
    current, current_args = policy(p)
    current.append('c.session_id=se.id')
    current_sql = f"FROM segments s JOIN chunks c ON c.id=s.chunk_id WHERE {' AND '.join(current)}"
    include_empty = p['channel'] == 'ambient' and not any(k == 'lang' or k.startswith('recipient_') for k in p)
    if not include_empty:
        where.append(f'EXISTS (SELECT 1 {current_sql})')
        args += current_args
    rows = conn.execute(f'''SELECT se.id,se.device_id,se.start_utc_ms,se.end_utc_ms,se.gap_s,
        (SELECT count(*) {current_sql}) n_visible_segments,
        (SELECT s.text {current_sql} ORDER BY s.start_utc_ms,s.device_id,s.id LIMIT 1) opening
        FROM sessions se WHERE {' AND '.join(where)}
        ORDER BY se.start_utc_ms,se.device_id,se.id LIMIT ?''',
        current_args + current_args + args + [p['limit'] + 1])
    def values():
        for r in rows:
            check_budget()
            value = {k: r[k] for k in ('device_id', 'start_utc_ms', 'end_utc_ms', 'gap_s', 'opening', 'n_visible_segments')}
            value.update(session_id=r['id'], closed=stamp > r['end_utc_ms'] + r['gap_s'] * 1000)
            yield value, [r['start_utc_ms'], r['device_id'], r['id']]
    return reader.page('sessions', p, stamp, values())


def target_expression(key):
    body = f"CASE WHEN json_valid(d.body_json) THEN CASE WHEN json_type(d.body_json,'$.target.{key}')='text' THEN nullif(json_extract(d.body_json,'$.target.{key}'),'') END END"
    return f"COALESCE(nullif(d.{key},''),{body})" if key in ('app', 'window') else body


def spans(reader, conn, p, stamp):
    where, args = [overlap('d'), 'd.device_id=?'], [p['to_ms'], p['from_ms'], p['from_ms'], p['device_id']]
    for name in ('lang', 'cancelled'):
        if name in p:
            where.append(f'd.{name}=?')
            args.append(int(p[name] == 'true') if name == 'cancelled' else p[name])
    for name in RECIPIENTS:
        if 'recipient_' + name in p:
            where.append(target_expression(name) + '=?')
            args.append(p['recipient_' + name])
    key = reader.cursor('spans', p, stamp)
    if key:
        where.append('(d.start_utc_ms,d.device_id,d.id)>(?,?,?)')
        args += key
    fields = ','.join(target_expression(k) + ' target_' + k for k in RECIPIENTS)
    rows = conn.execute(f'''SELECT d.id,d.device_id,d.start_utc_ms,d.end_utc_ms,d.text,d.lang,
        d.engine,d.mode,d.origin,d.cancelled,d.status,d.segment_id,{fields}
        FROM dictation_spans d WHERE {' AND '.join(where)}
        ORDER BY d.start_utc_ms,d.device_id,d.id LIMIT ?''', args + [p['limit'] + 1])
    def values():
        for r in rows:
            check_budget()
            value = {k: r[k] for k in ('device_id', 'start_utc_ms', 'end_utc_ms', 'text', 'lang', 'engine', 'mode', 'origin', 'status')}
            value.update(span_id=str(r['id']), segment_id=str(r['segment_id']) if r['segment_id'] is not None else None,
                         cancelled=bool(r['cancelled']), target={k: r['target_' + k] for k in RECIPIENTS if r['target_' + k] is not None})
            # These facts retain their device/time scope. An overlap is not a producer link.
            tags = conn.execute('''SELECT id,key,value,source,origin,device_id,start_utc_ms,end_utc_ms
                FROM tags WHERE target='span' AND device_id=? AND
                ((start_utc_ms < ? AND end_utc_ms > ?)
                 OR (start_utc_ms=end_utc_ms AND start_utc_ms>=? AND start_utc_ms<?)
                 OR (?=? AND ((start_utc_ms<=? AND end_utc_ms>?)
                     OR (end_utc_ms=start_utc_ms AND start_utc_ms=?))))
                ORDER BY start_utc_ms,id LIMIT ?''',
                (r['device_id'], r['end_utc_ms'], r['start_utc_ms'], r['start_utc_ms'], r['end_utc_ms'],
                 r['start_utc_ms'], r['end_utc_ms'], r['start_utc_ms'], r['start_utc_ms'],
                 r['start_utc_ms'], LIMITS['aggregate_rows'] + 1))
            value['tags'] = []
            size = 0
            for n, tag in enumerate(tags):
                aggregate_budget(n)
                record = dict(tag)
                record['tag_id'] = str(record.pop('id'))
                record['target'] = 'span'
                size += sum(len(str(v).encode()) for v in record.values())
                if size > LIMITS['page_bytes']:
                    raise RecallError(422, 'item_too_large', 'One report has too many tags')
                value['tags'].append(record)
            yield value, [r['start_utc_ms'], r['device_id'], r['id']]
    return reader.page('spans', p, stamp, values())


def search(reader, conn, p, stamp):
    where, args = policy(p)
    where.append(overlap())
    args += [p['to_ms'], p['from_ms'], p['from_ms']]
    table = 'segments_fts'
    terms = p['q'].split()
    if p['mode'] == 'fuzzy':
        long = [t for t in terms if len(t) >= 3]
        table = 'segments_tri' if long else None
        for term in terms:
            if len(term) < 3:
                where.append('casefold_contains(s.text,?)')
                args.append(term)
        terms = long
    if table:
        where.append(f'{table} MATCH ?')
        args.append(db.fts_query(' '.join(terms)))
    source = f'{table} JOIN segments s ON s.id={table}.rowid' if table else 'segments s'
    score = f'bm25({table})' if table else 'NULL'
    key = reader.cursor('search', p, stamp)
    paging, page_args = '', []
    if key:
        if table:
            paging = 'WHERE (r.score,r.start_utc_ms,r.device_id,r.id)>(?,?,?,?)'
            page_args = key
        else:
            paging = 'WHERE (-r.start_utc_ms,r.device_id,r.id)>(?,?,?)'
            page_args = [-key[0], key[1], key[2]]
    order = 'r.score,r.start_utc_ms,r.device_id,r.id' if table else 'r.start_utc_ms DESC,r.device_id,r.id'
    # Rank identities only; hydrate text after filtering and keyset pagination. The
    # materialized CTE also gives bm25 the direct FTS context required by SQLite.
    rows = conn.execute(f'''WITH ranked AS MATERIALIZED (
        SELECT s.id,s.start_utc_ms,s.device_id,{score} score
        FROM {source} LEFT JOIN chunks c ON c.id=s.chunk_id WHERE {' AND '.join(where)})
        SELECT r.id,r.start_utc_ms,r.device_id,r.score FROM ranked r
        {paging} ORDER BY {order} LIMIT ?''', args + page_args + [p['limit'] + 1])
    def values():
        for r in rows:
            value = reader.segment_item(conn, r['id'])
            value.update(score=r['score'], address=dict(device_id=r['device_id'], from_ms=value['start_utc_ms'], to_ms=value['end_utc_ms']))
            key = [r['start_utc_ms'], r['device_id'], r['id']]
            yield value, [r['score']] + key if table else key
    return reader.page('search', p, stamp, values())
