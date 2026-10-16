"""Read-only current history projection; no ingestion, migration or model initialization."""
from __future__ import annotations

import base64
from contextlib import closing
from contextvars import ContextVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
              timeline_rows=10000, aggregate_rows=100000, device_chars=64, session_id_chars=256)
_DEADLINE = ContextVar("recall_deadline", default=float("inf"))


def check_budget():
    if time.monotonic() > _DEADLINE.get():
        raise RecallError(503, "read_budget_exhausted", "Reader deadline exceeded; narrow the window")
RECIPIENTS = ('app','window','workspace','pane','program','session_id')
FILTERS = {'channel','lang',*(f'recipient_{x}' for x in RECIPIENTS)}


class RecallError(Exception):
    def __init__(self, status, code, message, field=None):
        self.status, self.code, self.message, self.field = status, code, message, field
        super().__init__(message)


def bad(message, field=None):
    raise RecallError(400, 'invalid_parameter', message, field)


def parse(route, pairs):
    if route == 'audio':
        return parse_audio(pairs)
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
    if 'device_id' in p and len(p['device_id'])>LIMITS['device_chars']: bad('Device ID too long','device_id')
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
    if route=='timeline':
        if not p.get('tz'): bad('Required IANA timezone','tz')
        try: ZoneInfo(p['tz'])
        except (ValueError, ZoneInfoNotFoundError): bad('Unknown IANA timezone','tz')
        p.setdefault('bucket','hour')
        if p['bucket'] not in ('hour','day'): bad('Invalid bucket','bucket')
    if 'lang' in p and (not p['lang'] or len(p['lang'])>16 or not p['lang'].replace('-','').isalpha()): bad('Invalid language code','lang')
    if route=='search':
        if not p.get('q','').strip(): bad('Required nonempty query','q')
        p.setdefault('mode','literal')
        if p['mode'] not in ('literal','fuzzy'): bad('Invalid mode','mode')
    return p


def parse_audio(pairs):
    p = {}
    for k,v in pairs:
        if k != 'chunk_id' or k in p: bad('Unknown or repeated parameter', k)
        p[k] = v
    v = p.get('chunk_id', '')
    if not (v.isascii() and v.isdigit() and len(v) <= 19 and 0 < int(v) <= 9223372036854775807):
        bad('Required positive integer chunk ID', 'chunk_id')
    return dict(chunk_id=int(v))


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
    check_budget()
    rows=conn.execute("SELECT * FROM tags WHERE target='segment' AND segment_id=? ORDER BY (source='deterministic') DESC,id DESC",(segment_id,))
    seen=set(); result=[]
    size=0
    for r in rows:
        check_budget()
        size+=sum(len(str(r[k]).encode()) for k in ('key','value','source','origin'))
        if size>LIMITS['page_bytes']: raise RecallError(422,'item_too_large','One row has too many tags')
        d={k:r[k] for k in ('key','value','source','origin')}; d['effective']=r['key'] not in seen
        seen.add(r['key']); result.append(d)
    return result


def item(conn,r):
    check_budget()
    d={k:r[k] for k in ('device_id','start_utc_ms','end_utc_ms','text','lang','channel','session_id')}
    for out,key in [('segment_id','id'),('chunk_id','chunk_id'),('span_id','span_id')]: d[out]=str(r[key]) if r[key] is not None else None
    # Lets a client seek in the chunk's audio before it loads: offset = start_utc_ms - this.
    d['chunk_start_utc_ms']=r['chunk_start'] if r['chunk_id'] is not None else None
    d.update(processing_state=r['processing_state'],producer=dict(kind='dictation' if r['span_id'] is not None else 'stt',model_id=r['model_id'],model_revision=r['model_revision']),speaker=None,timing='segment',words=None,tags=tags(conn,r['id']))
    if sum(len(str(r[k]).encode()) for k in ('text','words_json')) > LIMITS['page_bytes']:
        raise RecallError(422,'item_too_large','One item exceeds page budget')
    if r['words_json'] and r['chunk_start'] is not None:
        try:
            words=json.loads(r['words_json']); result=[]; previous=r['start_utc_ms']
            if not isinstance(words,list) or not words: raise ValueError()
            for w in words:
                check_budget()
                a,b=w['s'],w['e']; probability=w.get('p')
                if type(a) is not int or type(b) is not int or not isinstance(w['w'],str): raise ValueError()
                a+=r['chunk_start']; b+=r['chunk_start']
                if not previous<=a<=b<=r['end_utc_ms']: raise ValueError()
                if probability is not None and (type(probability) not in (int,float) or not math.isfinite(probability) or not 0<=probability<=1): raise ValueError()
                previous=b; result.append(dict(text=w['w'],start_utc_ms=a,end_utc_ms=b,probability=probability))
            d.update(timing='word',words=result)
        except (ValueError,TypeError,KeyError,RecursionError,OverflowError): logging.getLogger(__name__).warning('Invalid word timing on segment %s',r['id'])
    return d


class Reader:
    def __init__(self,path, labels=None, archive_dir=None):
        self.path=Path(path); self.labels=labels or {}
        # Chunk paths are relative to the archive; the server's default is data_dir/archive
        # beside data_dir/roomlog.db.
        self.archive_dir=Path(archive_dir or self.path.parent/'archive').expanduser().resolve()
        with closing(self.connection()) as conn:
            if db.user_version(conn)!=db.SCHEMA_VERSION: raise RuntimeError('Reader requires current database schema; migrate with writer separately')

    def connection(self):
        conn=db.connect(self.path,readonly=True)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2*LIMITS['page_bytes'])
        conn.execute('PRAGMA busy_timeout=100'); deadline=time.monotonic()+LIMITS['query_timeout_s']
        conn.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        return conn

    def read(self,route,p):
        deadline = _DEADLINE.set(time.monotonic() + LIMITS['query_timeout_s'])
        conn = None
        try:
            conn = self.connection()
            conn.execute('BEGIN')
            stamp = now_ms()
            if route == 'info':
                result = self.info(conn,stamp)
                self.check_response(result)
                return result
            if route == 'transcript':
                return self.transcript(conn,p,stamp)
            from . import recall_routes
            if route not in ('timeline','sessions','spans','search'):
                raise RecallError(404,'not_found','Unknown route')
            return getattr(recall_routes,route)(self,conn,p,stamp)
        except sqlite3.DataError as e:
            raise RecallError(422,'item_too_large','Stored item exceeds reader byte budget') from e
        except sqlite3.OperationalError as e:
            if 'interrupt' in str(e) or 'locked' in str(e):
                raise RecallError(503,'read_budget_exhausted','Reader is busy; retry') from e
            raise
        finally:
            if conn is not None:
                conn.close()
            _DEADLINE.reset(deadline)

    def audio(self,chunk_id):
        """The archive file of one chunk and its sha256; 404 unless it is a regular file inside the archive."""
        with closing(self.connection()) as conn:
            row=conn.execute('SELECT path,sha256 FROM chunks WHERE id=?',(chunk_id,)).fetchone()
        if row is None: raise RecallError(404,'not_found','Unknown chunk')
        unavailable=RecallError(404,'audio_unavailable','Chunk audio is not available')
        # Derived chunks (ADR 0005) are slices of raw captures and have no file of their own.
        if not row['path']: raise unavailable
        try: target=(self.archive_dir/row['path']).resolve()
        except (ValueError,OSError): raise unavailable
        if not target.is_relative_to(self.archive_dir):
            logging.getLogger(__name__).warning('Chunk %s path escapes the archive',chunk_id)
            raise unavailable
        if not target.is_file(): raise unavailable
        return target,row['sha256']

    def check_response(self,result):
        check_budget()
        if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode()) > LIMITS['page_bytes']:
            raise RecallError(422,'response_too_large','Summary exceeds response budget; narrow the request')

    def sources(self,conn):
        rows=conn.execute('''SELECT device_id,min(a) first,max(b) last FROM (
        SELECT device_id,start_utc_ms a,end_utc_ms b FROM chunks UNION ALL
        SELECT device_id,start_utc_ms,start_utc_ms+CAST(n_samples*1000/16000 AS INTEGER) FROM raw_segments UNION ALL
        SELECT device_id,start_utc_ms,end_utc_ms FROM dictation_spans) GROUP BY device_id ORDER BY device_id LIMIT ?''',(LIMITS['timeline_rows']+1,)).fetchall()
        if len(rows)>LIMITS['timeline_rows']: raise RecallError(422,'response_too_large','Too many discovered sources')
        return [dict(device_id=r['device_id'],label=self.labels.get(r['device_id'],{}).get('label',r['device_id']),display_as=self.labels.get(r['device_id'],{}).get('display_as','device'),first_known_ms=r['first'],last_known_ms=r['last']) for r in rows]

    def info(self,conn,stamp):
        return dict(server_time_ms=stamp,limits=LIMITS,capabilities=dict(word_timing='optional',speaker_identity=False,audio=True,change_feed=False,saved_clips=False),sources=self.sources(conn))

    def transcript(self,conn,p,stamp):
        where,args=policy(p); where+=[overlap()]; args += [p['to_ms'],p['from_ms'],p['from_ms']]
        key=self.cursor('transcript',p,stamp)
        if key: where+=['(s.start_utc_ms,s.device_id,s.id)>(?,?,?)']; args+=key
        # Sort bounded identities first. Sorting full text can make an oversized
        # later row fail before SQLite delivers the first valid page item.
        rows=conn.execute(f'''SELECT s.id,s.start_utc_ms,s.device_id
        FROM segments s LEFT JOIN chunks c ON c.id=s.chunk_id WHERE {' AND '.join(where)}
        ORDER BY s.start_utc_ms,s.device_id,s.id LIMIT ?''',args+[p['limit']+1])
        def values():
            for r in rows:
                yield self.segment_item(conn,r['id']),[r['start_utc_ms'],r['device_id'],r['id']]
        return self.page('transcript',p,stamp,values())

    def segment_item(self,conn,identity):
        row=conn.execute(f'''SELECT s.*,c.session_id,c.status processing_state,
            c.start_utc_ms chunk_start,{CHANNEL} channel FROM segments s
            LEFT JOIN chunks c ON c.id=s.chunk_id WHERE s.id=?''',(identity,)).fetchone()
        return item(conn,row)

    def digest(self,route,p):
        return hashlib.sha256(json.dumps([route,{k:v for k,v in p.items() if k not in ('cursor','limit')}],sort_keys=True).encode()).hexdigest()

    def cursor(self,route,p,stamp):
        if not p.get('cursor'):
            return None
        try:
            c=json.loads(base64.urlsafe_b64decode(p['cursor']+'='*(-len(p['cursor'])%4)))
            if not isinstance(c,dict) or c['digest']!=self.digest(route,p):
                raise ValueError()
            if type(c['expires']) is not int or c['expires']>stamp+LIMITS['cursor_ttl_s']*1000:
                raise ValueError()
            key=c['key']
            ranked = route=='search' and (p['mode']=='literal' or any(len(t)>=3 for t in p['q'].split()))
            if not isinstance(key,list) or len(key)!=(4 if ranked else 3):
                raise ValueError()
            if ranked and (type(key[0]) not in (int,float) or not math.isfinite(key[0])):
                raise ValueError()
            start,device,identity=key[-3:]
            if type(start) is not int or not -62135596800000<=start<=253402300799999 or not isinstance(device,str) or len(device)>LIMITS['device_chars']:
                raise ValueError()
            if route=='sessions':
                if not isinstance(identity,str) or not identity or len(identity)>LIMITS['session_id_chars']:
                    raise ValueError()
            elif type(identity) is not int or not 0<identity<=9223372036854775807:
                raise ValueError()
            if stamp>c['expires']:
                raise RecallError(409,'cursor_expired','Cursor expired; reload window')
            return key
        except (ValueError,KeyError,TypeError,RecursionError):
            bad('Invalid cursor','cursor')

    def page(self,route,p,stamp,rows):
        items=[]
        last=None
        more=False
        size=0
        iterator=iter(rows)
        while True:
            check_budget()
            try:
                value,key=next(iterator)
            except StopIteration:
                break
            except (RecallError,sqlite3.DataError) as error:
                oversized=isinstance(error,sqlite3.DataError) or error.code=='item_too_large'
                if not items or not oversized:
                    raise
                more=True
                break
            if len(items)>=p['limit']:
                more=True
                break
            n=len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())+2
            if n>LIMITS['page_bytes']-4096:
                if not items:
                    raise RecallError(422,'item_too_large','One item exceeds page budget')
                more=True
                break
            if size+n>LIMITS['page_bytes']-4096:
                more=True
                break
            items.append(value)
            last=key
            size+=n
        cursor=None
        if more:
            cursor=base64.urlsafe_b64encode(json.dumps(dict(digest=self.digest(route,p),expires=stamp+LIMITS['cursor_ttl_s']*1000,key=last)).encode()).decode().rstrip('=')
        result=dict(as_of_ms=stamp,items=items,next_cursor=cursor)
        self.check_response(result)
        return result
