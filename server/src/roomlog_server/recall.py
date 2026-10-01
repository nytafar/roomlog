"""Read-only current history projection; no ingestion, migration or model initialization."""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import logging
import math
import sqlite3
import time
from pathlib import Path

from . import db
from .query_policy import CHANNEL
from .times import now_ms

DAY = 86400000
LIMITS = dict(transcript_window_ms=DAY, spans_window_ms=DAY, timeline_window_ms=31*DAY,
              sessions_window_ms=31*DAY, search_window_ms=366*DAY, default_limit=200,
              max_limit=500, page_bytes=1048576, search_chars=512, recipient_chars=500,
              cursor_ttl_s=600, query_timeout_s=3, concurrent_reads=4, hourly_buckets=800,
              timeline_rows=10000)
RECIPIENTS = ('app','window','workspace','pane','program','session_id')
FILTERS = {'channel','lang',*(f'recipient_{x}' for x in RECIPIENTS)}


class RecallError(Exception):
    def __init__(self, status, code, message, field=None):
        self.status, self.code, self.message, self.field = status, code, message, field
        super().__init__(message)


def bad(message, field=None):
    raise RecallError(400, 'invalid_parameter', message, field)


def parse(route, pairs):
    allowed = set() if route == 'info' else {'from_ms','to_ms','device_id'} | FILTERS
    if route in ('transcript','spans','sessions','search'): allowed |= {'limit','cursor'}
    if route == 'timeline': allowed |= {'tz','bucket'}
    if route == 'search': allowed |= {'q','mode'}
    if route == 'spans': allowed = (allowed-{'channel'}) | {'cancelled'}
    p = {}
    for k,v in pairs:
        if k not in allowed or k in p: bad('Unknown or repeated parameter', k)
        if len(v)> (512 if k=='q' else 4096 if k=='cursor' else 500): bad('Parameter too long',k)
        p[k]=v
    if route == 'info': return p
    for k in ('from_ms','to_ms'):
        try:
            v=p[k]
            if not v.lstrip('-').isdigit(): raise ValueError()
            p[k]=int(v)
            if not -62135596800000 <= p[k] <= 253402300799999: raise ValueError()
        except (KeyError,ValueError): bad('Required integer UTC milliseconds', k)
    if p['from_ms']>=p['to_ms']: bad('Require from_ms < to_ms','to_ms')
    if p['to_ms']-p['from_ms']>LIMITS[f'{route}_window_ms']: bad('Window exceeds limit','to_ms')
    if route in ('transcript','spans') and not p.get('device_id'): bad('Required device_id','device_id')
    if 'device_id' in p and not p['device_id']: bad('Empty device_id','device_id')
    if route != 'spans':
        p.setdefault('channel','ambient')
        if p['channel'] not in ('ambient','dictation','all'): bad('Invalid channel','channel')
    if route in ('transcript','spans','sessions','search'):
        try: p['limit']=int(p.get('limit',200))
        except ValueError: bad('Invalid limit','limit')
        if not 1<=p['limit']<=500: bad('Limit must be 1..500','limit')
    if route=='spans' and 'cancelled' in p:
        if p['cancelled'] not in ('true','false'): bad('Expected true or false','cancelled')
    if route=='search':
        if not p.get('q','').strip(): bad('Required nonempty query','q')
        p.setdefault('mode','literal')
        if p['mode'] not in ('literal','fuzzy'): bad('Invalid mode','mode')
    return p


def overlap(alias='s'):
    return f"({alias}.start_utc_ms < ? AND ({alias}.end_utc_ms > ? OR ({alias}.end_utc_ms={alias}.start_utc_ms AND {alias}.start_utc_ms>=?)))"


def effective(key):
    return "(SELECT t.value FROM tags t WHERE t.target='segment' AND t.segment_id=s.id AND t.key=? ORDER BY (t.source='deterministic') DESC,t.id DESC LIMIT 1)"


def policy(p):
    where=['s.superseded_by IS NULL', '(s.span_id IS NOT NULL OR (s.model_id=c.model_id AND s.model_revision IS c.model_revision))']
    args=[]
    if p.get('device_id'): where+=['s.device_id=?']; args+=[p['device_id']]
    if p.get('channel','ambient')!='all':
        where += [f"{CHANNEL} {'!=' if p.get('channel','ambient')=='ambient' else '='} ?"]
        args+=['dictation']
    if 'lang' in p: where+=['s.lang=?']; args+=[p['lang']]
    for key in RECIPIENTS:
        if f'recipient_{key}' in p: where+=[effective(key)+'=?']; args += [key,p[f'recipient_{key}']]
    return where,args


def tags(conn, segment_id):
    rows=conn.execute("SELECT * FROM tags WHERE target='segment' AND segment_id=? ORDER BY (source='deterministic') DESC,id DESC",(segment_id,)).fetchall()
    seen=set(); result=[]
    for r in rows:
        d={k:r[k] for k in ('key','value','source','origin')}; d['effective']=r['key'] not in seen
        seen.add(r['key']); result.append(d)
    return result


def item(conn,r):
    d={k:r[k] for k in ('device_id','start_utc_ms','end_utc_ms','text','lang','channel','session_id')}
    for out,key in [('segment_id','id'),('chunk_id','chunk_id'),('span_id','span_id')]: d[out]=str(r[key]) if r[key] is not None else None
    d.update(producer=dict(kind='dictation' if r['span_id'] is not None else 'stt',model_id=r['model_id'],model_revision=r['model_revision']),speaker=None,timing='segment',words=None,tags=tags(conn,r['id']))
    if sum(len(str(r[k]).encode()) for k in ('text','words_json')) > LIMITS['page_bytes']:
        raise RecallError(422,'item_too_large','One item exceeds page budget')
    if r['words_json'] and r['chunk_start'] is not None:
        try:
            words=json.loads(r['words_json']); result=[]; previous=r['start_utc_ms']
            if not isinstance(words,list) or not words: raise ValueError()
            for w in words:
                a,b=w['s'],w['e']; probability=w.get('p')
                if type(a) is not int or type(b) is not int or not isinstance(w['w'],str): raise ValueError()
                a+=r['chunk_start']; b+=r['chunk_start']
                if not previous<=a<=b<=r['end_utc_ms']: raise ValueError()
                if probability is not None and (type(probability) not in (int,float) or not math.isfinite(probability) or not 0<=probability<=1): raise ValueError()
                previous=b; result.append(dict(text=w['w'],start_utc_ms=a,end_utc_ms=b,probability=probability))
            d.update(timing='word',words=result)
        except (ValueError,TypeError,KeyError): logging.getLogger(__name__).warning('Invalid word timing on segment %s',r['id'])
    return d


class Reader:
    def __init__(self,path, labels=None):
        self.path=Path(path); self.labels=labels or {}
        with closing(self.connection()) as conn:
            if db.user_version(conn)!=db.SCHEMA_VERSION: raise RuntimeError('Reader requires current database schema; migrate with writer separately')

    def connection(self):
        conn=db.connect(self.path,readonly=True)
        conn.execute('PRAGMA busy_timeout=100'); deadline=time.monotonic()+LIMITS['query_timeout_s']
        conn.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        return conn

    def read(self,route,p):
        conn=self.connection()
        try:
            conn.execute('BEGIN'); stamp=now_ms()
            if route=='info': return self.info(conn,stamp)
            if route != 'transcript': raise RecallError(404,'not_found','Route not available yet')
            return self.transcript(conn,p,stamp)
        except sqlite3.OperationalError as e:
            if 'interrupt' in str(e) or 'locked' in str(e): raise RecallError(503,'read_budget_exhausted','Reader is busy; retry') from e
            raise
        finally: conn.close()

    def sources(self,conn):
        rows=conn.execute('''SELECT device_id,min(a) first,max(b) last FROM (
        SELECT device_id,start_utc_ms a,end_utc_ms b FROM chunks UNION ALL
        SELECT device_id,start_utc_ms,start_utc_ms+CAST(n_samples*1000/16000 AS INTEGER) FROM raw_segments UNION ALL
        SELECT device_id,start_utc_ms,end_utc_ms FROM dictation_spans) GROUP BY device_id ORDER BY device_id''').fetchall()
        return [dict(device_id=r['device_id'],label=self.labels.get(r['device_id'],{}).get('label',r['device_id']),display_as=self.labels.get(r['device_id'],{}).get('display_as','device'),first_known_ms=r['first'],last_known_ms=r['last']) for r in rows]

    def info(self,conn,stamp):
        return dict(server_time_ms=stamp,limits=LIMITS,capabilities=dict(word_timing='optional',speaker_identity=False,audio=False,change_feed=False,saved_clips=False),sources=self.sources(conn))

    def transcript(self,conn,p,stamp):
        where,args=policy(p); where+=[overlap()]; args += [p['to_ms'],p['from_ms'],p['from_ms']]
        key=self.cursor('transcript',p,stamp)
        if key: where+=['(s.start_utc_ms,s.device_id,s.id)>(?,?,?)']; args+=key
        rows=conn.execute(f'''SELECT s.*,c.session_id,c.start_utc_ms chunk_start,{CHANNEL} channel
        FROM segments s LEFT JOIN chunks c ON c.id=s.chunk_id WHERE {' AND '.join(where)}
        ORDER BY s.start_utc_ms,s.device_id,s.id LIMIT ?''',args+[p['limit']+1])
        return self.page('transcript',p,stamp,((item(conn,r),[r['start_utc_ms'],r['device_id'],r['id']]) for r in rows))

    def digest(self,route,p):
        return hashlib.sha256(json.dumps([route,{k:v for k,v in p.items() if k not in ('cursor','limit')}],sort_keys=True).encode()).hexdigest()

    def cursor(self,route,p,stamp):
        if not p.get('cursor'): return None
        try:
            c=json.loads(base64.urlsafe_b64decode(p['cursor']+'='*(-len(p['cursor'])%4)))
            if not isinstance(c,dict) or c['digest']!=self.digest(route,p): raise ValueError()
            if type(c['expires']) is not int or c['expires']>stamp+600000: raise ValueError()
            key=c['key']
            if not isinstance(key,list) or len(key)!=3 or type(key[0]) is not int or not isinstance(key[1],str) or type(key[2]) is not int or key[2]<0: raise ValueError()
            if stamp>c['expires']: raise RecallError(409,'cursor_expired','Cursor expired; reload window')
            return key
        except (ValueError,KeyError,TypeError): bad('Invalid cursor','cursor')

    def page(self,route,p,stamp,rows):
        items=[]; last=None; more=False; size=0
        for value,key in rows:
            n=len(json.dumps(value,ensure_ascii=False).encode())+2
            if n>LIMITS['page_bytes']-4096: raise RecallError(422,'item_too_large','One item exceeds page budget')
            if len(items)>=p['limit'] or size+n>LIMITS['page_bytes']-4096: more=True; break
            items.append(value); last=key; size+=n
        cursor=None
        if more:
            cursor=base64.urlsafe_b64encode(json.dumps(dict(digest=self.digest(route,p),expires=stamp+600000,key=last)).encode()).decode().rstrip('=')
        return dict(as_of_ms=stamp,items=items,next_cursor=cursor)
