"""Five-minute real HTTP webhook load; synthetic bot-sender events emit no replies."""
import asyncio,time,json,uuid,hashlib
from pathlib import Path
from datetime import datetime,timezone
import httpx
from sqlalchemy import select
from korsik.db import SessionLocal
from korsik.models import TransportInbox
RUN=str(uuid.uuid4());N=3000;RATE=10
secret=Path('/operator/webhook_secret').read_text().strip()
results=[];ids=[]
def event(i):return {'update_type':'bot_started','timestamp':int(time.time()*1000)+i,'chat_id':0,'user':{'user_id':227218523,'name':'Synthetic load sender','is_bot':True},'verification_run':RUN,'verification_seq':i}
async def main():
    async with httpx.AsyncClient(base_url='http://max-connector:8001',timeout=10,trust_env=False,limits=httpx.Limits(max_connections=30)) as client:
        tasks=[];started=time.monotonic()
        async def send(i):
            body=event(i);raw=json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False);ids.append(hashlib.sha256(raw.encode()).hexdigest());at=time.monotonic()
            try:r=await client.post('/hooks/max',json=body,headers={'X-Max-Bot-Api-Secret':secret});results.append({'seconds':time.monotonic()-at,'status':r.status_code})
            except Exception as e:results.append({'seconds':time.monotonic()-at,'status':0,'error_type':type(e).__name__})
        for i in range(N):
            await asyncio.sleep(max(0,started+i/RATE-time.monotonic()));tasks.append(asyncio.create_task(send(i)))
            if i and i%300==0:print(json.dumps({'sent':i,'elapsed':round(time.monotonic()-started,1)}),flush=True)
        await asyncio.gather(*tasks)
        negative=await client.post('/hooks/max',json=event(N+1),headers={'X-Max-Bot-Api-Secret':'wrong-secret'})
    with SessionLocal() as db:
        rows=db.scalars(select(TransportInbox).where(TransportInbox.id.in_(ids))).all();durable=len(rows);done=sum(r.status=='DONE' for r in rows)
    times=sorted(r['seconds'] for r in results);p95=times[int(.95*(len(times)-1))];successful=sum(r['status']==200 for r in results)
    report={'test':'MAX_WEBHOOK_NFR01','run_id':RUN,'started_at':datetime.now(timezone.utc).isoformat(),'events':N,'target_events_per_second':RATE,'elapsed_seconds':round(time.monotonic()-started,3),'http200':successful,'persisted_events':durable,'processed_at_end':done,'p95_seconds':round(p95,4),'max_seconds':round(max(times),4),'negative_secret_status':negative.status_code,'synthetic_bot_sender_no_external_sends':True,'status':'PASS' if successful==N and durable==N and p95<=1 and negative.status_code==403 else 'FAIL'}
    root=Path('/data/verification');root.mkdir(exist_ok=True);(root/'webhook-load.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
asyncio.run(main())
