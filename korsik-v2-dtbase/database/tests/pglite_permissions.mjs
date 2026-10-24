// Partial object-grant smoke only. Database grants are skipped explicitly.
import {PGlite} from '@electric-sql/pglite';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const db=new PGlite();
const strip=s=>s.split('\n').filter(l=>!l.startsWith('\\')).join('\n');
try {
 const dn=(await db.query('select current_database() name')).rows[0].name;
 for(const stmt of strip(fs.readFileSync(path.join(root,'permissions.sql'),'utf8')).replaceAll(':"dbname"','"'+dn+'"').split(';')) {const clean=stmt.split('\n').filter(l=>!l.trim().startsWith('--')).join('\n').trim();if(clean){if(clean.startsWith('GRANT CONNECT ON DATABASE') || clean.startsWith('GRANT CREATE ON DATABASE')){console.log('SKIP database grant (PGlite catalog limitation)');continue;} console.log('STMT',clean);await db.exec(clean);}}
 console.log('PARTIAL permissions.sql: database grants and native ownership NOT validated');
 // Objects are created by the embedded default owner; native ownership still untested.
 for(const f of ['schema.sql','operations.sql','migrations/003_survey_router.sql','grants.sql','seed_demo.sql']){
  await db.exec(strip(fs.readFileSync(path.join(root,f),'utf8')));console.log('PASS SQL with embedded default owner:',f);
 }
 await db.exec('RESET ROLE; SET ROLE korsik_app');
 for(const q of ['select * from korsik.hr_dashboard','select korsik.prepare_due_events(1)','select * from korsik.claim_outbox(1)']){
  await db.query(q);console.log('PASS app operation');
 }
 await db.exec(strip(fs.readFileSync(path.join(root,'tests/permissions_check.sql'),'utf8')));console.log('PASS tests/permissions_check.sql');
 for(const q of ['delete from korsik.employees','update korsik.audit_log set action=action','select * from korsik.schema_migrations','create table korsik.forbidden(id int)']){
  let rejected=false;
  try{await db.query(q);}catch(e){if(e.code!=='42501')throw e;rejected=true;}
  if(!rejected)throw new Error('Unexpected privilege: '+q);
  console.log('PASS denied',q);
 }
} catch(e){console.error('FAIL',e.message,e.code,e.where);process.exitCode=1;}
finally{await db.close();}
