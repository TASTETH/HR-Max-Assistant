\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
DO $$
DECLARE h bigint;e bigint;p bigint;t bigint;v bigint;def jsonb;final_def jsonb;
BEGIN
 SELECT id,hr_id INTO STRICT e,h FROM employees WHERE external_employee_id='DEMO-001';
 SELECT id INTO STRICT p FROM employee_plans WHERE employee_id=e AND status='active';
 PERFORM cancel_plan(h,p,false);
 PERFORM set_employee_context(h,e,'GrPP','office','formal',CURRENT_DATE+90);
 INSERT INTO plan_templates(code,name) VALUES('DEMO-SURVEY-V2','Демонстрация многошаговых опросов') RETURNING id INTO t;
 INSERT INTO plan_versions(template_id,version_no,created_by) VALUES(t,1,h) RETURNING id INTO v;
 def:='{
  "schema_version":1,"completion_text":"Спасибо! Девять вопросов завершены.",
  "questions":[
   {"code":"load","type":"rating","scale_direction":"low_bad","prompt":"Насколько комфортна нагрузка?","analyze":true},
   {"code":"tasks_clear","type":"boolean","prompt":"Понятны ли рабочие задачи?","comment_rule":{"mode":"values","values":[false]},"analyze":true},
   {"code":"help","type":"single_choice","prompt":"Кто помогает вам адаптироваться?","options":[{"id":"mentor","label":"Наставник"},{"id":"team","label":"Коллеги"},{"id":"other","label":"Свой вариант"}],"comment_rule":{"mode":"values","values":["other"]},"analyze":true},
   {"code":"contacts","type":"multi_choice","prompt":"С кем вы уже познакомились?","options":[{"id":"mentor","label":"Наставник"},{"id":"hr","label":"Закреплённый HR"},{"id":"none","label":"Никто"}],"exclusive_options":["none"],"analyze":true},
   {"code":"feedback","type":"text","prompt":"Что помогло в первые дни?","analyze":true},
   {"code":"photo","type":"attachment","prompt":"При желании прикрепите фото для знакомства.","required":false,"analyze":false},
   {"code":"expectations","type":"rating","scale_direction":"low_bad","prompt":"Насколько оправдались ожидания?","when":{"question_code":"tasks_clear","operator":"equals","value":false},"analyze":true},
   {"code":"stay","type":"single_choice","prompt":"Планируете продолжить работу?","options":[{"id":"yes","label":"Да, однозначно"},{"id":"unsure","label":"Пока не уверен"},{"id":"leave","label":"Нет, задумываюсь об уходе"}],"comment_rule":{"mode":"values","values":["unsure","leave"]},"analyze":true},
   {"code":"improve","type":"text","prompt":"Что можно улучшить?","analyze":true}
  ],
  "variants":[{"audience":{"location_tags":["GrPP"],"employee_types":["office"]},"body":"Демонстрационный опрос для офисного сотрудника GrPP."}]
 }';
 INSERT INTO template_events(version_id,code,position,kind,body,definition,workday_no,local_time)
 VALUES(v,'survey9',1,'survey','Общий демонстрационный опрос.',def,1,'00:00');
 INSERT INTO template_events(version_id,code,position,kind,body,local_time,max_reminders,definition)
 VALUES(v,'site_material',2,'info','Демонстрационный материал вашей площадки.','00:00',0,
 '{"schema_version":1,"audience":{"location_tags":["GrPP"]},"attachments":[{"asset_ref":"demo:map-grpp","kind":"map"}]}');
 SELECT jsonb_build_object('schema_version',1,'questions',jsonb_agg(jsonb_build_object(
  'code','final_'||i,'type','rating','scale_direction','low_bad','prompt','Демонстрационная итоговая оценка №'||i||' (1–10).','analyze',true) ORDER BY i))
 INTO final_def FROM generate_series(1,12) i;
 INSERT INTO template_events(version_id,code,position,kind,body,definition,schedule_anchor,local_time)
 VALUES(v,'final12',3,'survey','Итоговый демонстрационный опрос.',final_def,'probation_end_date','09:00');
 UPDATE plan_versions SET published_at=clock_timestamp() WHERE id=v;
 -- Explicit synthetic seven-day calendar: this is NOT the company work calendar.
 PERFORM assign_plan(h,e,v,'{"weekdays":[1,2,3,4,5,6,7],"holidays":[],"extra_workdays":[]}');
END $$;
COMMIT;
