#!/usr/bin/env python3
"""Operator imports the three supplied examples through the real authenticated API.
No automatic content approval: pages/figures remain visible as review-required.
"""
import json,time,uuid
from pathlib import Path
import httpx
from korsik.settings import get_settings
s=get_settings();client=httpx.Client(base_url='http://api:8000/api/v1',timeout=180,trust_env=False)
r=client.post('/auth/login',json={'email':s.admin_email,'password':Path('/operator/password').read_text().strip()});r.raise_for_status();csrf=r.json()['csrf_token']
client.headers.update({'Cookie':'; '.join(f'{k}={v}' for k,v in r.cookies.items()),'X-CSRF-Token':csrf})
def call(method,path,**kwargs):
    headers=kwargs.pop('headers',{});headers['Idempotency-Key']=str(uuid.uuid4())
    response=client.request(method,path,headers=headers,**kwargs)
    if response.status_code>=400:raise RuntimeError(f'{method} {path}: {response.status_code} {response.text[:600]}')
    return response
records=[]
for source in sorted(Path('/srv/seed/examples').glob('*.pptx')):
    title=source.stem
    docs=call('GET','/knowledge/documents').json()['items'];doc=next((d for d in docs if d['title']==title),None)
    if not doc:doc=call('POST','/knowledge/documents',json={'title':title,'description':'Пример предоставлен командой для демонстрационного стенда. Требуется содержательная проверка HR перед корпоративным вводом.','document_category':'REGULATION','scope':{'site_ids':[],'audience_roles':['EMPLOYEE'],'employment_stages':['ACTIVE'],'employment_types':['OFFICE','PRODUCTION','OTHER'],'sensitivity':'STANDARD'}}).json()
    doc=call('GET','/knowledge/documents/'+doc['id']).json()
    revisions=call('GET',f"/knowledge/documents/{doc['id']}/revisions").json()['items']
    revision=next((r for r in revisions if r.get('filename')==source.name),None)
    if not revision:
        with source.open('rb') as file:
            revision=call('POST',f"/knowledge/documents/{doc['id']}/revisions",headers={'If-Match':doc['etag']},files={'file':(source.name,file,'application/vnd.openxmlformats-officedocument.presentationml.presentation')},data={'reason':'Загрузка исходного примера команды для демонстрации'}).json()['revision']
    record={'document_id':doc['id'],'revision_id':revision['id'],'title':title,'filename':source.name};records.append(record)
    print(json.dumps({'uploaded':title,'revision_id':revision['id']},ensure_ascii=False),flush=True)
Path('/data/demo-import.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
for record in records:
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        revision=call('GET','/knowledge/revisions/'+record['revision_id']).json()
        if revision['status'] not in {'UPLOADED','PARSING','PROCESSING','EXTRACTING','INGESTING'}:
            record['status']=revision['status'];record['extraction_revision_id']=revision.get('latest_extraction_revision_id');break
        time.sleep(2)
    print(json.dumps({'parsed':record['title'],'status':record.get('status','TIMEOUT')},ensure_ascii=False),flush=True)
Path('/data/demo-import.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
