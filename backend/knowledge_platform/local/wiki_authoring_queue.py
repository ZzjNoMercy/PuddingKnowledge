"""Durable generation admission; execution authority remains the proposal claim."""
import time
import threading
from ..wiki.patch import canonical,digest
from ..wiki.json_input import decode_request
from .wiki_authoring_admin import exact
from .wiki_authoring_generation import TABLE as PROPOSALS

TABLE='knowledge_wiki_authoring_queue'
FIELDS=('space_id','operation_id','expected_revision','slugs','selected_raw','instruction')
MAX_ADMISSIONS=50000


class WikiAuthoringQueue:
    def __init__(self,admin):
        self.admin,self.store=admin,admin.store
        self.worker=None
        with self.store._connect() as db:
            db.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
                space_id TEXT NOT NULL,operation_id TEXT NOT NULL,actor TEXT NOT NULL,
                request_json TEXT NOT NULL,not_before INTEGER NOT NULL,state TEXT NOT NULL,
                receipt_digest TEXT NOT NULL,PRIMARY KEY(space_id,operation_id))''')

    def _read(self,db,op):
        size=db.execute(f'SELECT length(CAST(request_json AS BLOB)) FROM {TABLE} WHERE space_id=? AND operation_id=?',(self.store.space_id,op)).fetchone()
        if size is None:return None
        if type(size[0]) is not int or size[0]>256*1024:raise ValueError('Queue request budget exceeded')
        row=db.execute(f'SELECT actor,request_json,not_before,state,receipt_digest FROM {TABLE} WHERE space_id=? AND operation_id=?',(self.store.space_id,op)).fetchone()
        if row[3] not in ('queued','rejected') or digest(canonical([self.store.space_id,op,*row[:4]]))!=row[4]:raise ValueError('Queue commitment mismatch')
        request=decode_request(row[1]);exact(request,FIELDS)
        if request['operation_id']!=op or request['space_id']!=self.store.space_id:raise ValueError('Queue identity mismatch')
        return row

    def _status(self,db,op,row):
        proposal=self.admin.generation._read(db,op)
        if proposal is not None and proposal[0]!=canonical({'actor':row[0],'request':decode_request(row[1])}):raise ValueError('Queue proposal identity mismatch')
        if row[3]=='rejected' and proposal is not None:raise ValueError('Rejected admission has a proposal')
        return {'operation_id':op,'not_before':row[2],'state':proposal[3] if proposal is not None else row[3],
                'admission_receipt':row[4],'proposal_receipt':proposal[5] if proposal is not None else None,
                'execution_liveness':'unknown','automatic_retry':False}

    def enqueue(self,who,body):
        exact(body,('request','not_before'));request=body['request'];exact(request,FIELDS)
        op=self.admin.generation._identity(who,request,True)
        due=body['not_before']
        if type(due) is not int or not 0<=due<=4102444800:raise ValueError('Invalid queue not_before timestamp')
        data=canonical(request)
        if len(data.encode())>256*1024:raise ValueError('Queue request budget exceeded')
        values=[who.subject_id,data,due,'queued'];receipt=digest(canonical([self.store.space_id,op,*values]))
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self._read(db,op)
            if row is not None:
                if row[:3]!=(who.subject_id,data,due):raise ValueError('Queue operation identity reused')
                return self._status(db,op,row)
            if db.execute(f'SELECT count(*) FROM {TABLE} WHERE space_id=?',(self.store.space_id,)).fetchone()[0]>=MAX_ADMISSIONS:raise ValueError('Queue admission budget exceeded')
            # Validate immutable selection and revision before accepting an admission.
            context=self.admin.context(who,{'space_id':self.store.space_id,'slugs':request['slugs'],'selected_raw':request['selected_raw']})
            if context['revision']!=request['expected_revision']:raise ValueError('Stale queue revision')
            db.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?)',(self.store.space_id,op,*values,receipt))
            return self._status(db,op,(*values,receipt))

    def queue(self,who,body):
        exact(body,('space_id',),('after','limit'));self.admin.authorize(who,body['space_id'])
        after=body.get('after','');limit=body.get('limit',20)
        if not isinstance(after,str) or len(after)>160 or type(limit) is not int or not 1<=limit<=50:raise ValueError('Invalid queue page')
        with self.store._connect() as db:
            db.execute('BEGIN')
            ids=[r[0] for r in db.execute(f'SELECT operation_id FROM {TABLE} WHERE space_id=? AND operation_id>? ORDER BY operation_id LIMIT ?',(self.store.space_id,after,limit+1))]
            # Each proposal is verified in full; bound the aggregate before loading.
            total=0;items=[]
            for op in ids[:limit]:
                size=db.execute(f'SELECT length(CAST(patch_json AS BLOB)) FROM {PROPOSALS} WHERE space_id=? AND operation_id=?',(self.store.space_id,op)).fetchone()
                total+=size[0] if size is not None else 0
                if total>64*1024*1024:raise ValueError('Queue result verification budget exceeded')
                items.append(self._status(db,op,self._read(db,op)))
        return {'items':items,'next_after':ids[limit-1] if len(ids)>limit else None,
                'worker':self.worker.status() if self.worker else {'enabled':False,'running':False}}

    def check_claim(self,db,who,request):
        row=self._read(db,request['operation_id'])
        if row is not None and (row[3]!='queued' or row[0]!=who.subject_id or row[1]!=canonical(request) or row[2]>int(time.time())):
            raise ValueError('Queue admission cannot authorize this claim')

    def run_queue(self,who,body):
        exact(body,('space_id',));self.admin.authorize(who,body['space_id'])
        if self.admin.generation.model is None:raise ValueError('Queue model is not configured')
        with self.store._connect() as db:
            db.execute('BEGIN')
            rows=db.execute(f'''SELECT q.operation_id FROM {TABLE} q WHERE q.space_id=? AND q.actor=?
                AND q.state='queued' AND q.not_before<=? AND NOT EXISTS
                (SELECT 1 FROM {PROPOSALS} p WHERE p.space_id=q.space_id AND p.operation_id=q.operation_id)
                ORDER BY q.not_before,q.operation_id LIMIT 1''',(self.store.space_id,who.subject_id,int(time.time()))).fetchall()
            if not rows:return {'processed':None}
            op=rows[0][0];row=self._read(db,op)
            if row[0]!=who.subject_id:raise PermissionError('Queue actor mismatch')
            request=decode_request(row[1])
        try:
            self.admin.generate(who,request)
        except ValueError:
            # Only a pre-claim failure may reject admission. A committed proposal
            # remains authoritative, including unsettled after observation loss.
            with self.store._connect() as db:
                db.execute('BEGIN IMMEDIATE');current=self._read(db,op)
                proposal=self.admin.generation._read(db,op)
                if proposal is None and current[3]=='queued' and current[2]<=int(time.time()):
                    values=[*current[:3],'rejected'];receipt=digest(canonical([self.store.space_id,op,*values]))
                    db.execute(f'UPDATE {TABLE} SET state=?,receipt_digest=? WHERE space_id=? AND operation_id=?',('rejected',receipt,self.store.space_id,op))
        with self.store._connect() as db:
            db.execute('BEGIN');return {'processed':self._status(db,op,self._read(db,op))}


class WikiQueueWorker:
    def __init__(self,queue,principal):
        self.queue,self.principal=queue,principal
        self.stop_event=threading.Event();self.error=None
        self.thread=threading.Thread(target=self._run,name='knowledge-wiki-queue',daemon=True)
        queue.worker=self
    def _run(self):
        while not self.stop_event.is_set():
            try:self.queue.run_queue(self.principal,{'space_id':self.queue.store.space_id})
            except Exception:
                self.error='queue_worker_unavailable';return
            self.stop_event.wait(1)
    def start(self):self.thread.start()
    def close(self):self.stop_event.set();self.thread.join(1)
    def status(self):return {'enabled':True,'running':self.thread.is_alive(),'error':self.error}
