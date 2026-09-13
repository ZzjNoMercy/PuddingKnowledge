"""Durable at-most-once generation attempts; ready proposals never self-publish."""
import re
from ..wiki.patch import canonical,digest
from ..wiki.json_input import decode_request
from .wiki_authoring_admin import exact,strings,parse_patch

TABLE='knowledge_wiki_authoring_proposals'
MAX_RECORD=32*1024*1024


class WikiAuthoringGeneration:
    def __init__(self,admin,model):
        self.admin,self.store,self.model=admin,admin.store,model
        with self.store._connect() as db:
            db.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
                space_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_json TEXT NOT NULL,
                context_digest TEXT NOT NULL, model_digest TEXT NOT NULL, state TEXT NOT NULL,
                patch_json TEXT NOT NULL, receipt_digest TEXT NOT NULL, PRIMARY KEY(space_id,operation_id))''')

    def _identity(self,who,request,generate=False):
        self.admin.authorize(who,request['space_id'])
        op=request['operation_id']
        if not isinstance(op,str) or not re.fullmatch('[A-Za-z0-9._-]{1,160}',op):raise ValueError('Invalid proposal identity')
        if generate:
            strings(request['slugs']);strings(request['selected_raw'])
            if not isinstance(request['instruction'],str) or not request['instruction'].strip() or len(request['instruction'].encode())>65536:raise ValueError('Invalid generation instruction')
            if not isinstance(request['expected_revision'],str) or not re.fullmatch('[a-f0-9]{64}',request['expected_revision']):raise ValueError('Invalid generation revision')
        return op

    def _read(self,db,op):
        sizes=db.execute(f'SELECT length(CAST(request_json AS BLOB)),length(CAST(patch_json AS BLOB)) FROM {TABLE} WHERE space_id=? AND operation_id=?',(self.store.space_id,op)).fetchone()
        if sizes is None:return None
        if any(type(s) is not int or s>MAX_RECORD for s in sizes):raise ValueError('Proposal record exceeds budget')
        row=db.execute(f'SELECT request_json,context_digest,model_digest,state,patch_json,receipt_digest FROM {TABLE} WHERE space_id=? AND operation_id=?',(self.store.space_id,op)).fetchone()
        if row[3] not in ('unsettled','ready','failed') or digest(canonical([self.store.space_id,op,*row[:5]]))!=row[5]:raise ValueError('Proposal commitment mismatch')
        if row[3]=='ready':parse_patch(decode_request(row[4]))
        elif row[4]!='':raise ValueError('Invalid proposal state')
        return row

    def _result(self,op,row):
        return {'operation_id':op,'state':row[3],'context_digest':row[1],'model_digest':row[2],
                'patch':decode_request(row[4]) if row[3]=='ready' else None,
                'execution_liveness':'unknown','automatic_retry':False,'publication_performed':False}

    def proposal(self,who,request):
        exact(request,('space_id','operation_id'));op=self._identity(who,request)
        with self.store._connect() as db:
            db.execute('BEGIN');row=self._read(db,op)
        if row is None:raise ValueError('Unknown proposal')
        return self._result(op,row)

    def generate(self,who,request):
        exact(request,('space_id','operation_id','expected_revision','slugs','selected_raw','instruction'))
        op=self._identity(who,request,True)
        request_json=canonical({'actor':who.subject_id,'request':request})
        with self.store._connect() as db:
            db.execute('BEGIN');previous=self._read(db,op)
        if previous is not None:
            if previous[0]!=request_json:raise ValueError('Generation identity reused')
            return self._result(op,previous)
        if self.model is None:raise ValueError('Wiki patch model is not configured')
        context=self.admin.context(who,{'space_id':request['space_id'],'slugs':request['slugs'],'selected_raw':request['selected_raw']})
        context['resolved_schema_yaml']=self.store.bundle.resolved_yaml
        if context['revision']!=request['expected_revision']:raise ValueError('Stale generation context')
        # Fail before a durable claim or network call if the full context cannot fit.
        from .wiki_patch_model import MAX_CONTEXT
        if len(canonical({'context':context,'instruction':request['instruction']}).encode())>MAX_CONTEXT:raise ValueError('Generation context exceeds budget')
        context_digest=digest(canonical(context));model_digest=digest(canonical(self.model.config))
        values=[request_json,context_digest,model_digest,'unsettled','']
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE');previous=self._read(db,op)
            if previous is not None:
                if previous[0]!=request_json:raise ValueError('Generation identity reused')
                return self._result(op,previous)
            if self.store._read(db)[0]!=context['revision']:raise ValueError('Stale generation context')
            receipt=digest(canonical([self.store.space_id,op,*values]))
            db.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?)',(self.store.space_id,op,*values,receipt))
        # Only this successful claimant may invoke the model. A process loss leaves
        # unsettled, which is not a claim of liveness and never triggers blind retry.
        try:
            draft=self.model.generate(context,request['instruction'])
            exact(draft,('changes','index','log_entry'))
            if not isinstance(draft['changes'],list) or not 1<=len(draft['changes'])<=100:raise ValueError('Invalid generated changes')
            inventory={p['slug']:p['digest'] for p in context['inventory']};changes=[]
            for c in draft['changes']:
                exact(c,('slug','markdown'),('replacement',))
                if not isinstance(c['slug'],str) or c['slug'] in inventory and c['slug'] not in context['pages']:raise ValueError('Unselected existing page mutation')
                changes.append({**c,'expected_digest':inventory.get(c['slug'])})
            patch={'expected_revision':context['revision'],'changes':changes,'selected_raw':request['selected_raw'],'index':draft['index'],'log_entry':draft['log_entry']}
            self.admin.preview(who,{'space_id':self.store.space_id,'patch':patch})
            patch_json=canonical(patch)
            if len(patch_json.encode())>MAX_RECORD:raise ValueError('Proposal exceeds budget')
            state='ready'
        except Exception:
            state='failed';patch_json=''
        values=[request_json,context_digest,model_digest,state,patch_json]
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE');current=self._read(db,op)
            if current is None or current[:5]!=(request_json,context_digest,model_digest,'unsettled',''):raise ValueError('Generation settlement conflict')
            receipt=digest(canonical([self.store.space_id,op,*values]))
            db.execute(f'UPDATE {TABLE} SET state=?,patch_json=?,receipt_digest=? WHERE space_id=? AND operation_id=?',(state,patch_json,receipt,self.store.space_id,op))
        return self._result(op,(*values,receipt))
