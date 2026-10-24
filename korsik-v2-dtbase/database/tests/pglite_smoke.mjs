// Optional reproducible SQL smoke test. Native concurrency.py is still required.
import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const db=new PGlite();
function readSql(name) {
  return fs.readFileSync(path.join(root,name),'utf8').split('\n')
    .filter(line=>!line.startsWith('\\')).join('\n')
    .replaceAll(':hr_id','1').replaceAll(':employee_id','1').replaceAll(':answer_id','1');
}
try {
  await db.exec('CREATE ROLE korsik_app');
  console.log((await db.query('select version()')).rows[0].version);
  for(const name of ['schema.sql','operations.sql','migrations/003_survey_router.sql','seed_demo.sql','tests/invariants.sql','tests/flows.sql','tests/delivery_serial.sql','seed_survey_demo.sql','tests/survey_router.sql','queries.sql','partition_maintenance.sql','walkthrough.sql','walkthrough_survey.sql']) {
    await db.exec(readSql(name));
    console.log('PASS',name);
  }
} finally {await db.close();}
