"""Receipt-bound cancellation/rescheduling before a generation claim exists."""
import re
from datetime import datetime,timezone
from ..wiki.patch import canonical,digest
from ..wiki.json_input import decode_request
from .wiki_authoring_admin import exact

EVENTS='knowledge_wiki_queue_controls'
MAX_HISTORY=128


class QueueControl:
    def __init__(self,queue,db):
        self.queue=queue
        from .wiki_authoring_queue import TABLE
        if 'history_digest' not in {r[1] for r in db.execute(f'PRAGMA table_info({TABLE})')}:
            db.execute(f"ALTER TABLE {TABLE} ADD COLUMN history_digest TEXT NOT NULL DEFAULT ''")
        db.execute(f'''CREATE TABLE IF NOT EXISTS {EVENTS} (
            space_id TEXT NOT NULL, operation_id TEXT NOT NULL, command_id TEXT NOT NULL,
            command_json TEXT NOT NULL,previous_history TEXT NOT NULL,before_due INTEGER NOT NULL,
            after_due INTEGER NOT NULL,after_state TEXT NOT NULL,created_at TEXT NOT NULL,
            receipt TEXT NOT NULL,PRIMARY KEY(space_id,operation_id,command_id),UNIQUE(space_id,operation_id,receipt))''')

    def _event(self,db,op,receipt):
        space=self.queue.store.space_id
        sizes=db.execute(f'SELECT length(CAST(command_json AS BLOB)) FROM {EVENTS} WHERE space_id=? AND operation_id=? AND receipt=?',(space,op,receipt)).fetchone()
        if sizes is None or type(sizes[0]) is not int or sizes[0]>16384:raise ValueError('Queue control evidence unavailable')
        row=db.execute(f'SELECT command_id,command_json,previous_history,before_due,after_due,after_state,created_at,receipt FROM {EVENTS} WHERE space_id=? AND operation_id=? AND receipt=?',(space,op,receipt)).fetchone()
        if digest(canonical([space,op,*row[:7]]))!=row[7]:raise ValueError('Queue control commitment mismatch')
        return row

    def history(self,db,op,row):
        space=self.queue.store.space_id;head=row[5];due=row[2];state='queued' if row[3]=='rejected' else row[3]
        if row[3]=='cancelled' and not head:raise ValueError('Cancellation evidence missing')
        count=db.execute(f'SELECT count(*) FROM {EVENTS} WHERE space_id=? AND operation_id=?',(space,op)).fetchone()[0]
        if count>MAX_HISTORY:raise ValueError('Queue history budget exceeded')
        visited=set()
        while head:
            if head in visited or len(visited)>=MAX_HISTORY:raise ValueError('Invalid queue control chain')
            visited.add(head);event=self._event(db,op,head)
            if event[4]!=due or event[5]!=state:raise ValueError('Queue control state mismatch')
            command=decode_request(event[1]);exact(command,('actor','request','generation_digest'))
            body=command['request'];self.validate(body)
            old_receipt=self.queue.receipt(op,[row[0],row[1],event[3],'queued'],event[2])
            if command['actor']!=row[0] or command['generation_digest']!=digest(row[1]) or body['space_id']!=space or body['operation_id']!=op or body['command_id']!=event[0] or body['expected_receipt']!=old_receipt:raise ValueError('Queue control identity mismatch')
            expected_state='cancelled' if body['action']=='cancel' else 'queued'
            expected_due=event[3] if body['action']=='cancel' else body['not_before']
            if event[4]!=expected_due or event[5]!=expected_state:raise ValueError('Queue control command mismatch')
            head,due,state=event[2],event[3],'queued'
        if len(visited)!=count:raise ValueError('Queue control history is incomplete')
        return due

    def validate(self,body):
        if not isinstance(body,dict) or body.get('action') not in ('cancel','reschedule'):raise ValueError('Invalid queue action')
        required=('space_id','operation_id','command_id','expected_receipt','reason','action')
        exact(body,required+ (('not_before',) if body['action']=='reschedule' else ()))
        if not isinstance(body['command_id'],str) or not re.fullmatch('[A-Za-z0-9._-]{1,160}',body['command_id']):raise ValueError('Invalid queue command identity')
        if not isinstance(body['expected_receipt'],str) or not re.fullmatch('[a-f0-9]{64}',body['expected_receipt']):raise ValueError('Invalid queue receipt')
        if not isinstance(body['reason'],str) or not body['reason'].strip() or len(body['reason'].encode())>4096:raise ValueError('Invalid queue control reason')
        if body['action']=='reschedule' and (type(body['not_before']) is not int or not 0<=body['not_before']<=4102444800):raise ValueError('Invalid reschedule time')

    def _result(self,op,row,event):
        return {'operation_id':op,'command_id':event[0],'state':event[5],'not_before':event[4],
                'result_receipt':self.queue.receipt(op,[row[0],row[1],event[4],event[5]],event[7])}

    def apply(self,who,body):
        from .wiki_authoring_queue import TABLE
        self.validate(body);op=self.queue.admin.generation._identity(who,body)
        space=self.queue.store.space_id
        with self.queue.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            actor=db.execute(f'SELECT actor FROM {TABLE} WHERE space_id=? AND operation_id=?',(space,op)).fetchone()
            if actor is None:raise ValueError('Unknown queue admission')
            if actor[0]!=who.subject_id:raise PermissionError('Queue actor mismatch')
            row=self.queue._read(db,op)
            command=canonical({'actor':who.subject_id,'request':body,'generation_digest':digest(row[1])})
            if len(command.encode())>16384:raise ValueError('Queue control budget exceeded')
            previous=db.execute(f'SELECT receipt FROM {EVENTS} WHERE space_id=? AND operation_id=? AND command_id=?',(space,op,body['command_id'])).fetchone()
            if previous is not None:
                event=self._event(db,op,previous[0])
                if event[1]!=command:raise ValueError('Queue command identity reused')
                return self._result(op,row,event)
            if row[3]!='queued' or row[4]!=body['expected_receipt'] or self.queue.admin.generation._read(db,op) is not None:raise ValueError('Queue admission already changed or claimed')
            if db.execute(f'SELECT count(*) FROM {EVENTS} WHERE space_id=? AND operation_id=?',(space,op)).fetchone()[0]>=MAX_HISTORY:raise ValueError('Queue history budget exceeded')
            due=body['not_before'] if body['action']=='reschedule' else row[2]
            state='queued' if body['action']=='reschedule' else 'cancelled'
            event=[body['command_id'],command,row[5],row[2],due,state,datetime.now(timezone.utc).isoformat()]
            head=digest(canonical([space,op,*event]));receipt=self.queue.receipt(op,[row[0],row[1],due,state],head)
            db.execute(f'INSERT INTO {EVENTS} VALUES (?,?,?,?,?,?,?,?,?,?)',(space,op,*event,head))
            db.execute(f'UPDATE {TABLE} SET not_before=?,state=?,receipt_digest=?,history_digest=? WHERE space_id=? AND operation_id=?',(due,state,receipt,head,space,op))
            self.queue._read(db,op)
        return self._result(op,row,(*event,head))
