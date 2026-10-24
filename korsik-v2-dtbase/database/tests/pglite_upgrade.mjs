import {PGlite} from '@electric-sql/pglite';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const clean=p=>fs.readFileSync(p,'utf8').split('\n').filter(l=>!l.startsWith('\\')).join('\n');
const migrate=clean(path.join(root,'migrations/003_survey_router.sql'));
for(const mode of ['normal','conflicting_forms','conflicting_sends']){
 const db=new PGlite();
 try{
  await db.exec('CREATE ROLE korsik_app');
  for(const f of ['schema.sql','operations.sql','seed_demo.sql'])await db.exec(clean(path.join(root,f)));
  const before=JSON.stringify((await db.query(`select id,employee_id,plan_id,kind,rating,comment,status,scheduled_at,deadline_at from korsik.plan_events order by id`)).rows);
  const inv=JSON.stringify((await db.query('select * from korsik.invitations order by id')).rows);
  if(mode==='conflicting_forms')await db.exec(`update korsik.plan_events set status='queued' where employee_id=1 and code in ('task','day14')`);
  if(mode==='conflicting_sends')await db.exec(`select korsik.claim_outbox(100)`);
  if(mode==='normal'){
   await db.exec(migrate);
   const after=JSON.stringify((await db.query(`select id,employee_id,plan_id,kind,rating,comment,status,scheduled_at,deadline_at from korsik.plan_events order by id`)).rows);
   if(before!==after)throw new Error('Legacy event data changed');
   if(inv!==JSON.stringify((await db.query('select * from korsik.invitations order by id')).rows))throw new Error('Invitation data changed');
   if((await db.query('select count(*) n from korsik.message_analysis')).rows[0].n!==1)throw new Error('Analysis lost');
   console.log('PASS v1 -> v2 migration preserves legacy responses, event times/statuses and invitations');
   let rerunRejected=false;try{await db.exec(migrate);}catch(e){if(e.code!=='P0001')throw e;rerunRejected=true;await db.exec('ROLLBACK');}if(!rerunRejected)throw new Error('Migration re-run was not rejected');console.log('PASS migration re-run rejects version 3 without changing it');
  }else{
   let rejected=false;
   try{await db.exec(migrate);}catch(e){if(e.code!=='P0001')throw e;rejected=true;await db.exec('ROLLBACK');}
   if(!rejected)throw new Error('Unsafe migration accepted '+mode);
   if((await db.query('select max(version) v from korsik.schema_migrations')).rows[0].v!==2)throw new Error('Partial migration persisted');
   console.log('PASS migration rejects '+mode+' without partial schema changes');
  }
 }finally{await db.close();}
}
