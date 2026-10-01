import json
import sqlite3
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
from roomlog_server.recall import Reader, RecallError, parse
from roomlog_server.reader_http import ReaderServer
from roomlog_server.spans import add_tag


def chunk(conn,model='current'):
    return conn.execute("""INSERT INTO chunks(sha256,device_id,start_utc_ms,end_utc_ms,duration_ms,path,meta_json,run_id,epoch,received_utc_ms,model_id,status) VALUES('sha','nyta',1000,5000,4000,'x','{}','run',0,0,?,'done')""",(model,)).lastrowid


def segment(conn,cid,model='current',start=1500,end=2500,text='hello',words=None):
    return conn.execute('''INSERT INTO segments(chunk_id,device_id,idx,start_utc_ms,end_utc_ms,offset_ms,text,lang,model_id,words_json) VALUES(?,'nyta',0,?,?,0,?,'no',?,?)''',(cid,start,end,text,model,json.dumps(words) if words is not None else None)).lastrowid


def params(**kw):
    return parse('transcript',list(dict(device_id='nyta',from_ms='2000',to_ms='3000',**kw).items()))


def test_projection(cfg,conn):
    cid=chunk(conn);segment(conn,cid,model='old');sid=segment(conn,cid,words=[dict(w=' hello',s=500,e=1500,p=.8)])
    reader=Reader(cfg.db_path)
    row=reader.read('transcript',params())['items'][0]
    assert row['segment_id']==str(sid) and row['words'][0]['start_utc_ms']==1500
    add_tag(conn,'app','old','test',source='model',segment_id=sid)
    add_tag(conn,'app','new','test',segment_id=sid)
    assert reader.read('transcript',params(recipient_app='old'))['items']==[]
    assert reader.read('transcript',params(recipient_app='new'))['items']
    conn.execute("UPDATE chunks SET model_id='empty'")
    assert reader.read('transcript',params())['items']==[]
    assert reader.read('info',{})['sources'][0]['last_known_ms']==5000
    with reader.connection() as ro:
        with pytest.raises(sqlite3.OperationalError): ro.execute('DELETE FROM segments')


def test_paging_mutation(cfg,conn):
    cid=chunk(conn);ids=[segment(conn,cid,start=2200,end=2200,text=str(i)) for i in range(3)]
    reader=Reader(cfg.db_path);p=params(limit='1');page=reader.read('transcript',p)
    assert page['items'][0]['segment_id']==str(ids[0])
    conn.execute('DELETE FROM segments WHERE id=?',(ids[1],));p['cursor']=page['next_cursor']
    assert reader.read('transcript',p)['items'][0]['segment_id']==str(ids[2])
    p['channel']='all'
    with pytest.raises(RecallError): reader.read('transcript',p)
    with pytest.raises(RecallError): parse('info',[('unknown','x')])


def test_http(cfg,conn,tmp_path):
    cid=chunk(conn);segment(conn,cid,text='<script>')
    (tmp_path/'index.html').write_text('app')
    server=ReaderServer(('127.0.0.1',0),Reader(cfg.db_path),{'owner-secret':'owner','device-secret':'device'},tmp_path)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(path,token=None):
        req=Request(f'http://127.0.0.1:{server.server_port}'+path,headers={'Authorization':f'Bearer {token}'} if token else {})
        try:
            with urlopen(req) as r:return r.status,r.read(),r.headers
        except HTTPError as e:return e.code,e.read(),e.headers
    try:
        assert get('/')[1]==b'app';assert get('/healthz')[0]==200
        assert get('/v1/recall/info')[0]==401
        assert get('/v1/recall/info','device-secret')[0]==403
        status,data,headers=get('/v1/recall/transcript?device_id=nyta&from_ms=2000&to_ms=3000','owner-secret')
        assert status==200 and json.loads(data)['items'][0]['text']=='<script>'
        assert headers['Cache-Control']=='private, no-store'
        for _ in range(4):server.slots.acquire()
        assert get('/v1/recall/info','owner-secret')[0]==503
        for _ in range(4):server.slots.release()
    finally:server.shutdown();server.server_close();thread.join()


def test_oversized_row_and_invalid_word_fallback(cfg,conn):
    cid=chunk(conn);sid=segment(conn,cid,words=[dict(w='oops',s=-1,e=1)])
    reader=Reader(cfg.db_path)
    assert reader.read('transcript',params())['items'][0]['words'] is None
    conn.execute('UPDATE segments SET text=? WHERE id=?',('x'*1048576,sid))
    with pytest.raises(RecallError) as error: reader.read('transcript',params())
    assert error.value.status==422 and error.value.code=='item_too_large'


def request_params(route, **overrides):
    values = dict(from_ms='0', to_ms='10000')
    if route in ('transcript', 'spans'):
        values['device_id'] = 'nyta'
    if route == 'timeline':
        values.update(tz='Europe/Oslo', bucket='hour')
    values.update(overrides)
    return parse(route, [(k, str(v)) for k, v in values.items()])


def report(conn, device='nyta', start=3000, end=4000, cancelled=False, target=None):
    return conn.execute('''INSERT INTO dictation_spans
        (device_id,start_utc_ms,end_utc_ms,text,lang,engine,mode,origin,body_json,received_utc_ms,cancelled)
        VALUES(?,?,?,'report text','no','dictation-model','raw','test',?,0,?)''',
        (device,start,end,json.dumps(dict(target=target or {},secret='must not leak')),int(cancelled))).lastrowid


def raw(conn, start, end, status='segmented'):
    return conn.execute('''INSERT INTO raw_segments
        (sha256,device_id,run_id,epoch,n_start,n_samples,start_utc_ms,cut_reason,path,meta_json,received_utc_ms,status)
        VALUES(?,'nyta','run',0,0,?,?,'shutdown','raw','{}',0,?)''',
        (f'raw-{start}-{end}',(end-start)*16,start,status)).lastrowid


def test_dictation_without_audio_late_supersession_and_report_targets(cfg, conn):
    rid=report(conn,device='phone',target=dict(app='codex',pane='7',program='terminal',session_id='recipient-session'))
    sid=segment(conn,None,start=3000,end=4000,text='dictation text')
    conn.execute("UPDATE segments SET device_id='phone',span_id=? WHERE id=?",(rid,sid))
    add_tag(conn,'channel','dictation','test',segment_id=sid)
    add_tag(conn,'pane','7','test',segment_id=sid)
    reader=Reader(cfg.db_path)
    assert [s['device_id'] for s in reader.read('info',{})['sources']]==['phone']
    assert not reader.read('transcript',request_params('transcript',device_id='phone'))['items']
    data=reader.read('transcript',request_params('transcript',device_id='phone',channel='dictation',recipient_pane='7'))['items'][0]
    assert data['session_id'] is None and data['chunk_id'] is None and data['producer']['kind']=='dictation'
    assert data['processing_state'] is None
    reports=reader.read('spans',request_params('spans',device_id='phone',recipient_session_id='recipient-session'))['items']
    assert reports[0]['target']['pane']=='7' and 'secret' not in json.dumps(reports)
    cid=chunk(conn); old=segment(conn,cid)
    conn.execute('UPDATE segments SET superseded_by=? WHERE id=?',(rid,old))
    assert not reader.read('transcript',request_params('transcript'))['items']
    assert not reader.read('search',request_params('search',q='hello'))['items']


def test_cancelled_report_tags_keep_scope_and_producer(cfg,conn):
    cid=chunk(conn);sid=segment(conn,cid)
    rid=report(conn,start=1400,end=2600,cancelled=True,target=dict(app='editor'))
    add_tag(conn,'channel','dictation','report',segment_id=sid)
    add_tag(conn,'app','editor','report',segment_id=sid)
    add_tag(conn,'app','neighbor','other',device_id='nyta',start_utc_ms=2400,end_utc_ms=3500)
    reader=Reader(cfg.db_path)
    row=reader.read('transcript',request_params('transcript',channel='dictation',recipient_app='editor'))['items'][0]
    assert row['producer']['kind']=='stt' and row['span_id'] is None
    assert not reader.read('transcript',request_params('transcript',channel='all',recipient_app='neighbor'))['items']
    value=reader.read('spans',request_params('spans',cancelled='true'))['items'][0]
    assert value['span_id']==str(rid) and value['cancelled'] is True
    assert value['tags'][0]['target']=='span' and value['tags'][0]['start_utc_ms']==2400
    assert value['target']==dict(app='editor')


def test_current_revision_and_failed_retry_retain_success(cfg,conn):
    cid=chunk(conn);old=segment(conn,cid,text='old');new=segment(conn,cid,text='new')
    conn.execute("UPDATE segments SET model_revision='revision2' WHERE id=?",(new,))
    conn.execute("UPDATE chunks SET model_revision='revision2',status='failed'")
    reader=Reader(cfg.db_path)
    rows=reader.read('transcript',params())['items']
    assert [r['segment_id'] for r in rows]==[str(new)]
    assert rows[0]['processing_state']=='failed'
    conn.execute('UPDATE chunks SET model_id=NULL')
    assert not reader.read('transcript',params())['items']


def test_timeline_union_points_and_independent_capture_evidence(cfg,conn):
    cid=chunk(conn);segment(conn,cid,start=1500,end=3000);segment(conn,cid,start=2500,end=4000)
    point=segment(conn,cid,start=4500,end=4500)
    add_tag(conn,'channel','dictation','test',segment_id=point)
    raw(conn,0,6000);raw(conn,2000,8000,status='pending')
    conn.execute("UPDATE chunks SET kind='derived',status='failed'")
    reader=Reader(cfg.db_path)
    value=reader.read('timeline',request_params('timeline'))['rows'][0]
    assert value['text_ms']==2500 and value['n_segments']==2
    assert value['evidence']==dict(captured_ms=8000,pending_ms=6000,failed_ms=4000,no_detected_speech_ms=2000,unknown_ms=2000)
    filtered=reader.read('timeline',request_params('timeline',channel='dictation'))['rows'][0]
    assert filtered['n_segments']==1 and filtered['text_ms']==0 and filtered['evidence']==value['evidence']
    empty=reader.read('timeline',request_params('timeline',from_ms=9000,to_ms=10000))['rows'][0]
    assert empty['evidence']['unknown_ms']==1000 and empty['text_ms']==0


@pytest.mark.parametrize('date,hours',[('2026-03-29',23),('2026-10-25',25)])
def test_oslo_dst_calendar_boundaries(cfg,conn,date,hours):
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    cid=chunk(conn)
    start=datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('Europe/Oslo'))
    a,b=int(start.timestamp()*1000),int((start+timedelta(days=1)).timestamp()*1000)
    reader=Reader(cfg.db_path)
    result=reader.read('timeline',request_params('timeline',from_ms=a,to_ms=b))
    rows=result['rows']
    assert len(rows)==hours and sum(r['to_ms']-r['from_ms'] for r in rows)==hours*3600000
    assert all(x['to_ms']==y['from_ms'] for x,y in zip(rows,rows[1:]))
    daily=reader.read('timeline',request_params('timeline',from_ms=a,to_ms=b,bucket='day'))['rows']
    assert len(daily)==1 and daily[0]['to_ms']-daily[0]['from_ms']==hours*3600000


def test_session_projection_closure_pending_exception_and_paging(cfg,conn,monkeypatch):
    import roomlog_server.recall as recall
    monkeypatch.setattr(recall,'now_ms',lambda:15001)
    cid=chunk(conn);sid=segment(conn,cid,text='first current');segment(conn,cid,model='old',text='old row')
    conn.execute("INSERT INTO sessions VALUES('a','nyta',10,1000,5000,1,999,0,NULL,NULL,NULL)")
    conn.execute("INSERT INTO sessions VALUES('b','nyta',10,1000,5000,0,0,0,NULL,NULL,NULL)")
    conn.execute("UPDATE chunks SET session_id='a'")
    reader=Reader(cfg.db_path)
    p=request_params('sessions',limit=1)
    page=reader.read('sessions',p)
    value=page['items'][0]
    assert value['opening']=='first current' and value['n_visible_segments']==1 and value['closed']
    p['cursor']=page['next_cursor'];value=reader.read('sessions',p)['items'][0]
    assert value['session_id']=='b' and value['opening'] is None and value['n_visible_segments']==0
    assert len(reader.read('sessions',request_params('sessions',lang='no'))['items'])==1
    assert len(reader.read('sessions',request_params('sessions',channel='all'))['items'])==1
    add_tag(conn,'channel','dictation','test',segment_id=sid)
    assert reader.read('sessions',request_params('sessions',channel='dictation'))['items'][0]['opening']=='first current'


@pytest.mark.parametrize('mode,q,expected',[('literal','går',[0,2]),('literal','gar',[1]),('fuzzy','GÅ',[2,0]),('fuzzy','år må',[0]),('literal','" OR *',[])])
def test_search_semantics_projection_filters_and_overlap(cfg,conn,mode,q,expected):
    cid=chunk(conn)
    ids=[segment(conn,cid,start=1500+i*100,end=3000,text=t) for i,t in enumerate(['går må','gar','går kveld'])]
    segment(conn,cid,model='old',text='går må')
    reader=Reader(cfg.db_path)
    result=reader.read('search',request_params('search',from_ms=2000,to_ms=3000,q=q,mode=mode))['items']
    assert [r['segment_id'] for r in result]==[str(ids[i]) for i in expected]
    for r in result: assert r['address']['from_ms']<2000
    add_tag(conn,'app','needle','test',segment_id=ids[0])
    result=reader.read('search',request_params('search',q='går',recipient_app='needle',limit=1))['items']
    assert [r['segment_id'] for r in result]==[str(ids[0])]


@pytest.mark.parametrize('mode,q',[('literal','same'),('fuzzy','sa')])
def test_search_pagination(cfg,conn,mode,q):
    cid=chunk(conn);ids=[segment(conn,cid,start=2200,end=2200,text='same') for _ in range(4)]
    reader=Reader(cfg.db_path);p=request_params('search',q=q,mode=mode,limit=1)
    found=[]
    while True:
        page=reader.read('search',p);found += [x['segment_id'] for x in page['items']]
        if not page['next_cursor']:break
        p['cursor']=page['next_cursor']
    assert found==list(map(str,ids))


@pytest.mark.parametrize('route,extra',[('timeline',dict(tz='Bogus/Nowhere')),('timeline',dict(bucket='minute')),('timeline',dict(tz='')),('spans',dict(channel='dictation')),('search',dict(q='x',mode='regex')),('transcript',dict(limit='501')),('transcript',dict(from_ms='1.5')),('transcript',dict(lang=''))])
def test_invalid_parameters(route,extra):
    with pytest.raises(RecallError) as error:request_params(route,**extra)
    assert error.value.status==400


def test_cursor_expiration_byte_paging_and_work_budget(cfg,conn,monkeypatch):
    import roomlog_server.recall as recall
    cid=chunk(conn)
    for i in range(3):segment(conn,cid,start=2200,end=2200,text='x'*1500)
    reader=Reader(cfg.db_path)
    monkeypatch.setitem(recall.LIMITS,'page_bytes',8000)
    p=params();page=reader.read('transcript',p)
    assert 0<len(page['items'])<3 and page['next_cursor']
    p['cursor']=page['next_cursor']
    monkeypatch.setattr(recall,'now_ms',lambda:page['as_of_ms']+600001)
    with pytest.raises(RecallError) as error:reader.read('transcript',p)
    assert error.value.status==409
    monkeypatch.setitem(recall.LIMITS,'query_timeout_s',-1)
    with pytest.raises(RecallError) as error:reader.read('timeline',request_params('timeline'))
    assert error.value.status==503


def test_wire_fixtures_are_actual_reader_responses(cfg,conn,monkeypatch):
    from pathlib import Path
    import roomlog_server.recall as recall
    fixtures=Path(__file__).resolve().parents[2]/'contract'/'recall'
    first=json.loads((fixtures/'transcript.json').read_text())
    phone=json.loads((fixtures/'standalone-dictation.json').read_text())
    start=first['items'][0]['start_utc_ms']
    monkeypatch.setattr(recall,'now_ms',lambda:first['as_of_ms'])
    cid=chunk(conn,model='fixture-model')
    conn.execute("UPDATE chunks SET start_utc_ms=?,end_utc_ms=?,session_id='session-1'",(start,start+5000))
    rid=report(conn,device='phone',start=start+1000,end=start+3000,target=dict(app='codex'))
    conn.execute('UPDATE dictation_spans SET id=10 WHERE id=?',(rid,))
    for row in sorted(first['items']+phone['items'],key=lambda r:int(r['segment_id'])):
        words=None
        if row['words']:
            words=[dict(w=w['text'],s=w['start_utc_ms']-start,e=w['end_utc_ms']-start,p=w['probability']) for w in row['words']]
        sid=segment(conn,int(row['chunk_id']) if row['chunk_id'] else None,model='fixture-model',start=row['start_utc_ms'],end=row['end_utc_ms'],text=row['text'],words=words)
        conn.execute('UPDATE segments SET device_id=?,span_id=? WHERE id=?',(row['device_id'],row['span_id'],sid))
        # Reverse insertion preserves the documented priority order of fixture tags.
        for tag in reversed(row['tags']):add_tag(conn,tag['key'],tag['value'],tag['origin'],source=tag['source'],segment_id=sid)
    reader=Reader(cfg.db_path)
    for device,expected in [('nyta',first),('phone',phone)]:
        actual=reader.read('transcript',request_params('transcript',device_id=device,from_ms=start,to_ms=start+10000,channel='all'))
        assert actual==expected
    assert reader.read('transcript',request_params('transcript',from_ms=start+10000,to_ms=start+11000))==json.loads((fixtures/'empty-transcript.json').read_text())


def test_http_all_routes_validation_and_static_containment(cfg,conn,tmp_path):
    from urllib.parse import urlencode
    cid=chunk(conn);segment(conn,cid);report(conn)
    root=tmp_path/'public';root.mkdir();(root/'index.html').write_text('app')
    outside=tmp_path/'private';outside.write_text('secret')
    (root/'leak').symlink_to(outside)
    directory=root/'directory';directory.mkdir();(directory/'index.html').symlink_to(outside)
    server=ReaderServer(('127.0.0.1',0),Reader(cfg.db_path),{'reader':'owner','device':'device'},root)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(path,token='reader'):
        request=Request(f'http://127.0.0.1:{server.server_port}'+path,headers={'Authorization':'Bearer '+token})
        try:
            with urlopen(request) as response:return response.status,response.read()
        except HTTPError as error:return error.code,error.read()
    try:
        for route in ('transcript','sessions','spans','search','timeline'):
            values=dict(from_ms=0,to_ms=10000)
            if route in ('transcript','spans'):values['device_id']='nyta'
            if route=='search':values['q']='hello'
            if route=='timeline':values['tz']='Europe/Oslo'
            status,body=get('/v1/recall/'+route+'?'+urlencode(values))
            assert status==200,(route,body)
            assert 'rows' in json.loads(body) if route=='timeline' else 'items' in json.loads(body)
        assert get('/v1/recall/info?unknown=x')[0]==400
        assert get('/v1/recall/transcript?device_id=nyta&from_ms=0&from_ms=1&to_ms=10000')[0]==400
        assert get('/v1/recall/info','å')[0]==401
        for path in ('/leak','/directory/','/%2e%2e/private'):
            status,body=get(path);assert status==404 and b'secret' not in body
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('route',['transcript','search'])
def test_oversize_lookahead_preserves_completed_page(cfg,conn,route):
    cid=chunk(conn);first=segment(conn,cid,start=2200,end=2200,text='needle small')
    segment(conn,cid,start=2201,end=2201,text='needle '+'x'*3000000)
    reader=Reader(cfg.db_path)
    p=request_params(route,limit=1,**({'q':'needle'} if route=='search' else {}))
    page=reader.read(route,p)
    assert page['items'][0]['segment_id']==str(first) and page['next_cursor']
    p['cursor']=page['next_cursor']
    with pytest.raises(RecallError) as error:reader.read(route,p)
    assert error.value.code=='item_too_large'
