#!/usr/bin/env python3
from pathlib import Path
import base64, tarfile, io, gzip, hashlib, json, os, sys, time, urllib.request, importlib.util, subprocess
ROOT=Path(__file__).resolve().parent
BASE='http://127.0.0.1:8765/'
META=json.loads((ROOT/'mstr_meta.json').read_text('utf-8'))
EXPECTED_CANDIDATE_SHA=META['candidateSha256']
SOURCE_HASHES=META['projectionHashes']
EXPECTED_SCRIPTS=META['scriptList']
PATCH_FILES=META['patchFiles']
checks=[]

def check(name, cond, detail=None):
    ok=bool(cond); checks.append({'name':name,'pass':ok,'detail':detail})
    if not ok: raise AssertionError(name+(': '+str(detail) if detail is not None else ''))

def sha(b): return hashlib.sha256(b).hexdigest()

def wait_js(driver, expr, timeout=12):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver,timeout,poll_frequency=.08).until(lambda d: d.execute_script('return !!('+expr+')'))

def js(driver, code, *args): return driver.execute_script(code,*args)

def submit(driver,text):
    js(driver,"sendMessage(arguments[0]); return true",text)
    time.sleep(.25)

def safe_extract(raw):
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tf:
        members=tf.getmembers()
        for m in members:
            p=Path(m.name)
            check('safe_payload_path:'+m.name,not p.is_absolute() and '..' not in p.parts,m.name)
        tf.extractall(ROOT,filter='data')

projection=''.join(x.read_text('ascii') for x in sorted(ROOT.glob('projection.part.*')))
check('historical_projection_parts_present',bool(projection))
safe_extract(base64.b64decode(projection,validate=True))
patch_b64=''.join(x.read_text('ascii') for x in sorted(ROOT.glob('mstr_patch.part.*')))
patch_text=gzip.decompress(base64.b64decode(patch_b64,validate=True)).decode('utf-8')
patch_run=subprocess.run(['patch','-p1','--batch','--forward'],cwd=ROOT,input=patch_text,text=True,capture_output=True)
check('mstr_delta_patch_applied',patch_run.returncode==0,{'stdout':patch_run.stdout,'stderr':patch_run.stderr})
for rel in PATCH_FILES: check('patch_file_present:'+rel,(ROOT/rel).is_file(),rel)
for p,expected in SOURCE_HASHES.items():
    actual=sha((ROOT/p).read_bytes()); check('source_hash:'+p,actual==expected,actual)
check('projection_source_count',len(SOURCE_HASHES)==24,len(SOURCE_HASHES))
for p in SOURCE_HASHES:
    if p.endswith(('.js','.css','.html','.json')):
        with urllib.request.urlopen(BASE+p,timeout=5) as r: check('http200:'+p,r.status==200,r.status)

PRELOAD=r"""(() => {
  window.V3Phase16PlanningAdapter={
    version:'mstr-stage8-v1',
    select(args){
      const ids=((args&&args.frame&&args.frame.eligibleOptions)||[]).map(x=>x.optionId);
      const wanted=ids.includes('WEEK.PREFERENCE_WINDOW')?['WEEK.PREFERENCE_WINDOW']:(ids[0]?[ids[0]]:[]);
      return {selectedOptionIds:wanted,nextBestOptionId:wanted[0]||null,comparison:null,rationale:'MSTR_STAGE8_DETERMINISTIC_SELECTOR'};
    }
  };
})();"""

deps=ROOT/'.stage8_deps'
if importlib.util.find_spec('selenium') is None:
    deps.mkdir(exist_ok=True)
    subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--quiet','--target',str(deps),'selenium>=4.20,<5'],check=True)
    sys.path.insert(0,str(deps))
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

opts=Options()
for a in ['--headless=new','--disable-gpu','--window-size=1440,1000']: opts.add_argument(a)
opts.set_capability('goog:loggingPrefs',{'browser':'ALL'})
check('configured_no_no_sandbox','--no-sandbox' not in opts.arguments,opts.arguments)

driver=webdriver.Chrome(options=opts)
try:
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',{'source':PRELOAD})
    driver.get(BASE)
    wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function' && typeof phase15MemoryAuthority!=='undefined' && typeof phase16bContinuityStore!=='undefined'",20)
    check('runtime_no_no_sandbox','--no-sandbox' not in driver.capabilities.get('goog:chromeOptions',{}).get('args',[]),driver.capabilities.get('goog:chromeOptions',{}).get('args',[]))
    check('real_localhost_navigation',driver.current_url.startswith(BASE),driver.current_url)
    origin=js(driver,'return location.origin')
    check('real_localhost_origin',origin=='http://127.0.0.1:8765',origin)
    check('document_title',driver.title=='V3 — Mira × Kai',driver.title)
    dom=js(driver,"return {today:!!document.getElementById('todayView'),mira:!!document.getElementById('miraButton'),kai:!!document.getElementById('kaiButton'),input:!!document.getElementById('directInput'),copy:!!document.getElementById('dialogueCopy')}")
    check('mira_kai_today_dom',all(dom.values()),dom)
    rendered_scripts=js(driver,"return Array.from(document.scripts).map(s=>s.getAttribute('src')).filter(Boolean)")
    check('rendered_exact_script_list',rendered_scripts==EXPECTED_SCRIPTS,rendered_scripts)
    identity=js(driver,"return {key:Phase16BContinuityStore.STORE_KEY,schema:Phase16BContinuityStore.STORE_SCHEMA_VERSION,sem:Phase16BContinuitySupport.SEMANTICS_VERSION}")
    check('store_v3_key',identity['key']=='v3.phase16b.continuity.v3',identity)
    check('store_v3_schema',identity['schema']==3,identity)
    check('semantics_v2',identity['sem']=='phase16b-continuity-semantics-v2',identity)

    check('real_local_storage',js(driver,"localStorage.setItem('__mstr_stage8','persist-ok'); return localStorage.getItem('__mstr_stage8')")=='persist-ok')
    initial=js(driver,"return phase15MemoryAuthority.readSubjectState('workout.preferred_time')")
    check('initial_subject_absent',initial['status']=='ABSENT',initial)
    check('initial_absent_token',initial['subjectStateToken']=='P15S1:125824c91e9771f9514510cf7e14a7820a0c515232c79369e33920e6071f9cc8',initial)

    js(driver,"return phase15MemoryAuthority.mutate({logicalRequestId:'mstr-stage8-pref-evening',requestEpoch:1,operation:'UPSERT',subjectId:'workout.preferred_time',normalizedValue:'EVENING',source:'USER_EXPLICIT'})")
    evening=js(driver,"return phase15MemoryAuthority.readSubjectState('workout.preferred_time')")
    check('evening_subject_active',evening['status']=='ACTIVE' and evening['normalizedValue']=='EVENING',evening)
    check('evening_subject_token',evening['subjectStateToken']=='P15S1:906b1f53346277c07af06a835ab4fad3c339c2b7563b560674f116557b0abde5',evening)

    submit(driver,'Plan this week')
    wait_js(driver,"(()=>{try{return !!phase15LongitudinalExtension.inspectContinuity().capture}catch(e){return false}})()",10)
    capture=js(driver,"return phase15LongitudinalExtension.inspectContinuity().capture")
    check('capture_exists',bool(capture),capture)
    dep=capture['steps'][0]['memoryDependency']
    check('capture_memorydependency_v2',dep and dep['version']==2 and dep['subjectId']=='workout.preferred_time',dep)
    check('capture_subject_token_exact',dep['subjectStateToken']==evening['subjectStateToken'] and dep['normalizedValue']=='EVENING',dep)
    check('capture_has_no_global_stateToken','stateToken' not in dep,dep)

    submit(driver,'chốt kế hoạch này')
    wait_js(driver,"(()=>{try{return phase16bContinuityStore.readStrict().kind==='VALID'}catch(e){return false}})()",10)
    stored=js(driver,"return phase16bContinuityStore.readStrict()")
    doc=stored['document']; sdep=doc['anchor']['sourcePlan']['steps'][0]['memoryDependency']
    check('persisted_store_v3',doc['version']==3 and doc['phase16bSemanticsVersion']=='phase16b-continuity-semantics-v2',doc)
    check('persisted_anchor_v3',doc['anchor']['schemaVersion']==3,doc['anchor'])
    check('persisted_dependency_v2',sdep['version']==2 and sdep['subjectStateToken']==evening['subjectStateToken'] and sdep['normalizedValue']=='EVENING',sdep)

    sentinel='MSTR_HISTORICAL_V2_SENTINEL_NOT_JSON'
    js(driver,"localStorage.setItem('v3.phase16b.continuity.v2',arguments[0]); return true",sentinel)
    driver.refresh(); wait_js(driver,"document.readyState==='complete' && typeof phase15MemoryAuthority!=='undefined' && typeof phase16bContinuityStore!=='undefined'",20)
    check('local_storage_survives_reload',js(driver,"return localStorage.getItem('__mstr_stage8')")=='persist-ok')
    check('historical_v2_untouched',js(driver,"return localStorage.getItem('v3.phase16b.continuity.v2')")==sentinel)
    reload_subject=js(driver,"return phase15MemoryAuthority.readSubjectState('workout.preferred_time')")
    reload_store=js(driver,"return phase16bContinuityStore.readStrict()")
    check('subject_token_survives_reload',reload_subject['subjectStateToken']==evening['subjectStateToken'],reload_subject)
    check('v3_anchor_survives_reload',reload_store['kind']=='VALID' and reload_store['document']['version']==3,reload_store)

    js(driver,"return phase15MemoryAuthority.mutate({logicalRequestId:'mstr-stage8-pref-morning',requestEpoch:1,operation:'UPSERT',subjectId:'workout.preferred_time',normalizedValue:'MORNING',source:'USER_EXPLICIT'})")
    morning=js(driver,"return phase15MemoryAuthority.readSubjectState('workout.preferred_time')")
    check('morning_subject_token',morning['subjectStateToken']=='P15S1:c0f1e79a3bf3d742b1648bf43c37b4e686519b265e8d49b8d2fc5b7c0c600cb2',morning)
    submit(driver,'tiếp tục kế hoạch đã lưu')
    time.sleep(.7)
    after=js(driver,"return phase16bContinuityStore.readStrict()")
    saved_dep=after['document']['anchor']['sourcePlan']['steps'][0]['memoryDependency']
    check('relevant_change_not_silently_rewritten',saved_dep['subjectStateToken']==evening['subjectStateToken'] and saved_dep['normalizedValue']=='EVENING',saved_dep)
    check('historical_v2_still_untouched',js(driver,"return localStorage.getItem('v3.phase16b.continuity.v2')")==sentinel)

    logs=driver.get_log('browser'); severe=[]
    for e in logs:
        if e.get('level')!='SEVERE': continue
        msg=e.get('message','')
        if 'Failed to load resource' in msg and ('/assets/' in msg or 'favicon.ico' in msg): continue
        severe.append(msg)
    check('no_severe_js_console_errors',len(severe)==0,severe)
    for p in EXPECTED_SCRIPTS:
        with urllib.request.urlopen(BASE+p,timeout=5) as r: check('script_http200:'+p,r.status==200,r.status)

    screenshot=driver.get_screenshot_as_png()
    evidence={
      'schema':'MSTR_STAGE8_CI_EVIDENCE_V1_0','candidateSha256':EXPECTED_CANDIDATE_SHA,
      'projectionManifestCount':len(SOURCE_HASHES),'projectionHashesVerified':True,
      'runner':{'os':subprocess.check_output(['bash','-lc','source /etc/os-release && echo $PRETTY_NAME'],text=True).strip(),'chromeVersion':subprocess.check_output(['google-chrome','--version'],text=True).strip(),'chromeBinary':subprocess.check_output(['which','google-chrome'],text=True).strip(),'euid':os.geteuid(),'configuredArgs':opts.arguments,'noNoSandbox':('--no-sandbox' not in opts.arguments)},
      'browser':{'baseUrl':BASE,'origin':origin,'title':driver.title,'realLocalStorage':True,'localStorageSurvivedReload':True,'renderedScripts':rendered_scripts,'domMarkers':dom,'severeJsConsoleErrors':severe},
      'behavior':{'storeIdentity':identity,'initialSubject':initial,'eveningSubject':evening,'captureMemoryDependency':dep,'persistedMemoryDependency':sdep,'reloadSubject':reload_subject,'morningSubject':morning,'historicalV2Untouched':True,'relevantChangeNotSilentlyRewritten':True},
      'screenshot':{'sha256':sha(screenshot),'bytes':len(screenshot)},
      'checks':checks,'checkCount':len(checks),'failedChecks':[c['name'] for c in checks if not c['pass']],
      'verdict':'MSTR_STAGE8_FAIL' if any(not c['pass'] for c in checks) else 'MSTR_STAGE8_PASS'
    }
    print('MSTR_STAGE8_EVIDENCE_JSON='+json.dumps(evidence,separators=(',',':'),ensure_ascii=False),flush=True)
    enc=base64.b64encode(screenshot).decode('ascii')
    for i in range(0,len(enc),6000): print('MSTR_SCREENSHOT_B64_CHUNK='+enc[i:i+6000],flush=True)
    if evidence['failedChecks']: raise AssertionError('rendered failures: '+','.join(evidence['failedChecks']))
    print('MSTR_STAGE8_PASS',flush=True)
finally:
    driver.quit()
