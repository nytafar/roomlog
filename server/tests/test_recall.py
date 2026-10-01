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
