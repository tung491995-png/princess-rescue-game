#!/usr/bin/env python3
from pathlib import Path
import base64, tarfile, io, hashlib, json, os, sys, time, urllib.request, importlib.util, subprocess

ROOT=Path(__file__).resolve().parent
BASE='http://127.0.0.1:8765/'
META=json.loads((ROOT/'payload_meta.json').read_text('utf-8'))
CANDIDATE_SHA=META['candidateSha256']
SOURCE_HASHES=META['projectionHashes']
EXPECTED_SCRIPTS=META['scriptList']
checks=[]

def check(name, cond, detail=None):
    ok=bool(cond); checks.append({'name':name,'pass':ok,'detail':detail})
    if not ok:
        raise AssertionError(name + (': '+str(detail) if detail is not None else ''))

def sha(b): return hashlib.sha256(b).hexdigest()

def wait_js(driver, expr, timeout=12):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver,timeout,poll_frequency=.08).until(lambda d: d.execute_script('return !!('+expr+')'))

def js(driver, code, *args): return driver.execute_script(code,*args)
def get_calls(driver): return js(driver,'return JSON.parse(JSON.stringify(window.__p16dCalls||[]))')
def set_mode(driver,mode): js(driver,'window.__p16dMode=arguments[0]; return true;',mode)

def clean_reload(driver, clear_storage=True):
    if clear_storage: js(driver,'localStorage.clear(); return true;')
    driver.refresh()
    wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function' && !!window.V3CoachAdapter")

# Harness-only dependency bootstrap, matching the successful Phase16A Stage8 retry pattern.
deps=ROOT/'.stage8_deps'
if importlib.util.find_spec('selenium') is None:
    deps.mkdir(exist_ok=True)
    subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--quiet','--target',str(deps),'selenium>=4.20,<5'],check=True)
    sys.path.insert(0,str(deps))
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# Decode exact browser-code projection and verify every source hash before Chrome launch.
projection=''.join(x.read_text('ascii') for x in sorted(ROOT.glob('projection.part.*'))); raw=base64.b64decode(projection,validate=True)
with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tf:
    tf.extractall(ROOT)
for p,expected in SOURCE_HASHES.items():
    actual=sha((ROOT/p).read_bytes()); check('source_hash:'+p,actual==expected,actual)
check('projection_source_count',len(SOURCE_HASHES)==24,len(SOURCE_HASHES))
for p in SOURCE_HASHES:
    if p.endswith(('.js','.css','.html','.json')):
        with urllib.request.urlopen(BASE+p,timeout=5) as r:
            check('http200:'+p,r.status==200,r.status)

PRELOAD=r"""(() => {
  window.__p16dCalls=[];
  window.__p16dMode='genericSafe';
  window.__p16dSeq=0;
  window.__p16dPendingResolve=null;
  window.V3CoachAdapter={
    respond(request){
      const copy=JSON.parse(JSON.stringify(request));
      window.__p16dCalls.push(copy); window.__p16dSeq++;
      const m=window.__p16dMode;
      if(m==='genericHandoffTrue') return {coach:request.coach,text:'GENERIC_'+window.__p16dSeq,action:{label:'EVIL',action:'start'},handoff:true};
      if(m==='genericSafe') return {coach:request.coach,text:'GENERIC_'+window.__p16dSeq,handoff:false};
      if(m==='formStopCta') return {coach:request.coach,text:'FORM_STOP',action:{label:'FACE LAB',action:'face'},handoff:false};
      if(m==='formHandoff'){
        if(request.dialogueContext) return {coach:request.coach,text:'FORM_SECOND',action:{label:'EVIL2',action:'start'},handoff:true};
        return {coach:request.coach,text:'FORM_FIRST',handoff:true};
      }
      if(m==='pending') return new Promise(resolve=>{window.__p16dPendingResolve=resolve});
      return {coach:request.coach,text:'DEFAULT',handoff:false};
    }
  };
})();"""

opts=Options()
for a in ['--headless=new','--disable-gpu','--window-size=1440,1000']:
    opts.add_argument(a)
opts.set_capability('goog:loggingPrefs',{'browser':'ALL'})
check('configured_no_no_sandbox','--no-sandbox' not in opts.arguments,opts.arguments)

driver=webdriver.Chrome(options=opts)
try:
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',{'source':PRELOAD})
    driver.get(BASE)
    wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function' && !!window.V3CoachAdapter")
    check('runtime_no_no_sandbox','--no-sandbox' not in driver.capabilities.get('goog:chromeOptions',{}).get('args',[]),driver.capabilities.get('goog:chromeOptions',{}).get('args',[]))
    check('real_localhost_navigation',driver.current_url.startswith(BASE),driver.current_url)
    origin=js(driver,'return location.origin')
    check('real_localhost_origin',origin=='http://127.0.0.1:8765',origin)
    check('document_title',driver.title=='V3 — Mira × Kai',driver.title)
    dom=js(driver,"return {today:!!document.getElementById('todayView'),mira:!!document.getElementById('miraButton'),kai:!!document.getElementById('kaiButton'),input:!!document.getElementById('directInput'),copy:!!document.getElementById('dialogueCopy')}")
    check('mira_kai_today_dom',all(dom.values()),dom)
    rendered_scripts=js(driver,"return Array.from(document.scripts).map(s=>s.getAttribute('src')).filter(Boolean)")
    check('rendered_exact_script_list',rendered_scripts==EXPECTED_SCRIPTS,rendered_scripts)
    check('phase16d_module_available',js(driver,"return typeof Phase16DDialogueContext==='object' && Phase16DDialogueContext.VERSION_ID==='phase16d-dialogue-context-v1'"))

    # Real localStorage + reload survival.
    check('real_local_storage',js(driver,"localStorage.setItem('__p16d_stage8','ok'); return localStorage.getItem('__p16d_stage8')")=='ok')
    driver.refresh(); wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function'")
    check('local_storage_survives_reload',js(driver,"return localStorage.getItem('__p16d_stage8')")=='ok')
    js(driver,"localStorage.removeItem('__p16d_stage8'); return true")

    # A: two-turn generic/default=false flow, same user text twice. Second turn is sent through real UI form.
    clean_reload(driver,True); set_mode(driver,'genericHandoffTrue')
    user_text='Mira hello again'
    js(driver,"sendMessage(arguments[0]); return true",user_text)
    wait_js(driver,'window.__p16dCalls.length===1',8); wait_js(driver,"document.getElementById('dialogueCopy').textContent==='GENERIC_1'",8)
    c1=get_calls(driver)[0]
    check('generic_first_context_present','dialogueContext' in c1,c1)
    check('generic_first_context_empty',c1['dialogueContext']['messageCount']==0,c1['dialogueContext'])
    check('default_false_first_call_no_extra_handoff',len(get_calls(driver))==1)
    check('enriched_malicious_action_quarantined',js(driver,"return document.getElementById('dialogueAction').hidden===true"))
    js(driver,"openInput('mira'); return true")
    inp=driver.find_element(By.ID,'messageInput'); inp.clear(); inp.send_keys(user_text)
    driver.find_element(By.ID,'sendMessage').click()
    wait_js(driver,'window.__p16dCalls.length===2',8); wait_js(driver,"document.getElementById('dialogueCopy').textContent==='GENERIC_2'",8)
    c2=get_calls(driver)[1]; msgs=c2['dialogueContext']['messages']
    check('two_turn_current_text_request_once',c2['text']==user_text and sum(1 for m in msgs if m.get('text')==user_text)==1,{'requestText':c2['text'],'messages':msgs})
    check('two_turn_prior_dialogue_present',any(m.get('role')=='coach' and m.get('text')=='GENERIC_1' for m in msgs),msgs)
    check('two_turn_no_current_response_feedback',all(m.get('text')!='GENERIC_2' for m in msgs),msgs)
    check('ui_direct_input_submit_usable',js(driver,"return document.getElementById('messageInput').value===''"))
    check('default_false_adapter_true_still_one_per_turn',len(get_calls(driver))==2,len(get_calls(driver)))
    screenshot=driver.get_screenshot_as_png(); screenshot_sha=sha(screenshot)

    # Reload retains origin storage but not Phase16D free-form conversation context.
    js(driver,"localStorage.setItem('__p16d_reload_marker','persist'); return true")
    driver.refresh(); wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function'")
    check('reload_storage_origin_still_real',js(driver,"return localStorage.getItem('__p16d_reload_marker')")=='persist')
    set_mode(driver,'genericSafe'); js(driver,"sendMessage('Mira after reload'); return true")
    wait_js(driver,'window.__p16dCalls.length===1',8)
    cr=get_calls(driver)[0]
    check('reload_no_implicit_phase16d_context',cr.get('dialogueContext',{}).get('messageCount')==0,cr)

    # B: default=true + adapter=false remains one history-free call; frozen CTA remains usable.
    clean_reload(driver,True); set_mode(driver,'formStopCta')
    js(driver,"sendMessage('Mira form push'); return true")
    wait_js(driver,'window.__p16dCalls.length===1',8); wait_js(driver,"document.getElementById('dialogueCopy').textContent==='FORM_STOP'",8)
    fb=get_calls(driver)[0]
    check('default_true_adapter_false_one_call',len(get_calls(driver))==1)
    check('default_true_first_history_free','dialogueContext' not in fb,fb)
    wait_js(driver,"document.getElementById('dialogueAction').hidden===false",8)
    action_text=driver.find_element(By.ID,'dialogueAction').text
    check('cta_visible','FACE LAB' in action_text,action_text)
    driver.find_element(By.ID,'dialogueAction').click(); wait_js(driver,"!document.getElementById('faceView').hidden",5)
    check('cta_click_usable_face_view',js(driver,"return !document.getElementById('faceView').hidden"))

    # C: default=true + handoff=true; only already-authorized second call gets immutable pre-ingress context.
    clean_reload(driver,True); set_mode(driver,'genericSafe')
    js(driver,"sendMessage('Mira prior seed'); return true"); wait_js(driver,'window.__p16dCalls.length===1',8); wait_js(driver,"document.getElementById('dialogueCopy').textContent==='GENERIC_1'",8)
    prior=js(driver,"return JSON.parse(JSON.stringify(todayRuntime.getState().interaction.messages))")
    set_mode(driver,'formHandoff'); before=len(get_calls(driver)); js(driver,"sendMessage('Mira form push'); return true")
    wait_js(driver,f'window.__p16dCalls.length==={before+2}',12)
    calls=get_calls(driver); first=calls[before]; second=calls[before+1]
    check('default_true_handoff_first_history_free','dialogueContext' not in first,first)
    check('default_true_second_context_present','dialogueContext' in second,second)
    sm=second['dialogueContext']['messages']
    check('second_call_same_preingress_snapshot',sm==prior,{'expected':prior,'actual':sm})
    check('second_call_excludes_current_first_response',all(m.get('text') not in ('Mira form push','FORM_FIRST') for m in sm),sm)
    check('default_true_two_calls_exact',len(get_calls(driver))==before+2,len(get_calls(driver)))

    # D: stale enriched async completion rejected after view/context change.
    clean_reload(driver,True); set_mode(driver,'pending')
    js(driver,"sendMessage('Mira pending hello'); return true")
    wait_js(driver,'window.__p16dCalls.length===1 && typeof window.__p16dPendingResolve==="function"',8)
    js(driver,"setView('face'); return true")
    js(driver,"window.__p16dPendingResolve({coach:'mira',text:'LATE_RESPONSE',handoff:false}); return true")
    time.sleep(.7)
    late=js(driver,"return document.getElementById('dialogueCopy').textContent")
    check('stale_async_not_rendered',late!='LATE_RESPONSE',late)
    discard=js(driver,"return speechState.lastDiscard")
    check('stale_async_discard_recorded','stale async' in str(discard),discard)

    # Console/runtime error audit. Ignore only omitted binary visual assets/favicon; every HTML/CSS/JS/JSON code resource was checked HTTP 200.
    logs=driver.get_log('browser'); severe=[]
    for e in logs:
        if e.get('level')!='SEVERE': continue
        msg=e.get('message','')
        if 'Failed to load resource' in msg and ('/assets/' in msg or 'favicon.ico' in msg): continue
        severe.append(msg)
    check('no_severe_js_console_errors',len(severe)==0,severe)

    evidence={
      'schema':'P16D_STAGE8_CI_EVIDENCE_V1_2',
      'candidateSha256':CANDIDATE_SHA,
      'projectionManifestCount':len(SOURCE_HASHES),
      'projectionHashesVerified':True,
      'runner':{
        'os':subprocess.check_output(['bash','-lc','source /etc/os-release && echo $PRETTY_NAME'],text=True).strip(),
        'chromeVersion':subprocess.check_output(['google-chrome','--version'],text=True).strip(),
        'chromeBinary':subprocess.check_output(['which','google-chrome'],text=True).strip(),
        'euid':os.geteuid(),'configuredArgs':opts.arguments,'noNoSandbox':('--no-sandbox' not in opts.arguments)
      },
      'browser':{'baseUrl':BASE,'origin':origin,'title':driver.title,'realLocalStorage':True,'localStorageSurvivedReload':True,'renderedScripts':rendered_scripts,'domMarkers':dom,'severeJsConsoleErrors':severe},
      'behavior':{'twoTurnGenericContext':True,'defaultFalseNoEscalation':True,'defaultTrueAdapterFalseOneCall':True,'defaultTrueSecondCallContext':True,'ctaUsable':True,'staleAsyncRejected':True,'reloadNoImplicitContext':True},
      'screenshot':{'sha256':screenshot_sha,'bytes':len(screenshot)},
      'checks':checks,'checkCount':len(checks),'failedChecks':[c['name'] for c in checks if not c['pass']],
      'verdict':'PHASE16D_RENDERED_BROWSER_PASS'
    }
    print('P16D_STAGE8_EVIDENCE_JSON='+json.dumps(evidence,separators=(',',':'),ensure_ascii=False),flush=True)
    enc=base64.b64encode(screenshot).decode('ascii')
    for i in range(0,len(enc),6000): print('P16D_SCREENSHOT_B64_CHUNK='+enc[i:i+6000],flush=True)
    print('PHASE16D_RENDERED_BROWSER_PASS',flush=True)
finally:
    driver.quit()