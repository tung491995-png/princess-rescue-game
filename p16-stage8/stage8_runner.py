#!/usr/bin/env python3
from pathlib import Path
import base64, tarfile, io, hashlib, json, os, sys, time, urllib.request, importlib.util, subprocess

ROOT=Path(__file__).resolve().parent
BASE='http://127.0.0.1:8765/'
EXPECTED_SHA='433bff046f8e60a86106f50a49b7df029ad4113df14f3a7b98395f3be8a9a2d5'
META=json.loads((ROOT/'payload_meta.json').read_text('utf-8'))
SOURCE_HASHES=META['projectionHashes']
checks=[]

def rec(name, ok, detail=None, hard=False):
    item={'name':name,'pass':bool(ok),'detail':detail}
    checks.append(item)
    if hard and not ok:
        raise AssertionError(name+(': '+str(detail) if detail is not None else ''))
    return bool(ok)

def sha(b): return hashlib.sha256(b).hexdigest()

def wait_js(driver, expr, timeout=12):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver,timeout,poll_frequency=.08).until(lambda d: d.execute_script('return !!('+expr+')'))

def js(driver, code, *args): return driver.execute_script(code,*args)

def get_calls(driver): return js(driver,'return JSON.parse(JSON.stringify(window.__ctaAuditCalls||[]))')

# Bootstrap Selenium only in the CI harness, never in product.
deps=ROOT/'.stage8_deps'
if importlib.util.find_spec('selenium') is None:
    deps.mkdir(exist_ok=True)
    subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--quiet','--target',str(deps),'selenium>=4.20,<5'],check=True)
    sys.path.insert(0,str(deps))
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# Decode exact frozen browser projection and re-hash it before browser launch.
rec('candidate_sha_meta_exact',META['candidateSha256']==EXPECTED_SHA,META['candidateSha256'],True)
projection=''.join(x.read_text('ascii') for x in sorted(ROOT.glob('projection.part.*')))
raw=base64.b64decode(projection,validate=True)
with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tf:
    tf.extractall(ROOT)
for p,expected in SOURCE_HASHES.items():
    actual=sha((ROOT/p).read_bytes())
    rec('source_hash:'+p,actual==expected,actual,True)
rec('projection_source_count',len(SOURCE_HASHES)==24,len(SOURCE_HASHES),True)
for p in SOURCE_HASHES:
    if p.endswith(('.js','.css','.html','.json')):
        with urllib.request.urlopen(BASE+p,timeout=5) as r:
            rec('http200:'+p,r.status==200,r.status,True)

PRELOAD=r"""(() => {
  window.__ctaAuditCalls=[];
  window.__ctaAuditMode='formStopCta';
  window.V3CoachAdapter={
    respond(request){
      window.__ctaAuditCalls.push(JSON.parse(JSON.stringify(request)));
      if(window.__ctaAuditMode==='formStopCta')
        return {coach:request.coach,text:'CTA_AUDIT_FORM',action:{label:'FACE LAB',action:'face'},handoff:false};
      return {coach:request.coach,text:'CTA_AUDIT_GENERIC',handoff:false};
    }
  };
})();"""

opts=Options()
for a in ['--headless=new','--disable-gpu','--window-size=1440,1000']:
    opts.add_argument(a)
opts.set_capability('goog:loggingPrefs',{'browser':'ALL'})
rec('configured_no_no_sandbox','--no-sandbox' not in opts.arguments,opts.arguments,True)

driver=webdriver.Chrome(options=opts)

def boot():
    driver.get(BASE)
    wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function' && !!window.V3CoachAdapter")
    js(driver,"localStorage.clear(); return true")
    driver.refresh()
    wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function' && !!window.V3CoachAdapter")

def produce_cta():
    js(driver,"window.__ctaAuditCalls=[]; window.__ctaClicks=0; window.__ctaTouches=0; return true")
    js(driver,"sendMessage('Mira form push'); return true")
    wait_js(driver,"window.__ctaAuditCalls.length===1",8)
    wait_js(driver,"document.getElementById('dialogueCopy').textContent==='CTA_AUDIT_FORM'",8)
    wait_js(driver,"document.getElementById('dialogueAction').hidden===false",8)
    js(driver,"const e=document.getElementById('dialogueAction'); e.addEventListener('click',()=>window.__ctaClicks++); e.addEventListener('touchstart',()=>window.__ctaTouches++); return true")
    return js(driver,"return JSON.parse(JSON.stringify(window.__ctaAuditCalls[0]))")

def geometry():
    return js(driver,r"""
      const ids=['dialogueAction','spatialDialogue','todayDialogueSlot','visualStage'];
      const cls=['hero-copy','companion-layer'];
      const out={};
      function snap(e,name){
        const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        out[name]={tag:e.tagName,id:e.id,cls:e.className,rect:{x:r.x,y:r.y,w:r.width,h:r.height},
          zIndex:s.zIndex,position:s.position,pointerEvents:s.pointerEvents,display:s.display,visibility:s.visibility,opacity:s.opacity};
      }
      for(const id of ids){const e=document.getElementById(id); if(e)snap(e,id)}
      for(const c of cls){const e=document.querySelector('.'+c); if(e)snap(e,c)}
      const a=document.getElementById('dialogueAction'),r=a.getBoundingClientRect();
      const x=r.left+r.width/2,y=r.top+r.height/2;
      out.center={x,y,stack:document.elementsFromPoint(x,y).map(e=>({tag:e.tagName,id:e.id,cls:e.className,
        z:getComputedStyle(e).zIndex,pe:getComputedStyle(e).pointerEvents,pos:getComputedStyle(e).position})).slice(0,12)};
      out.slotParentChain=[];
      let p=document.getElementById('todayDialogueSlot');
      while(p && out.slotParentChain.length<8){out.slotParentChain.push({tag:p.tagName,id:p.id,cls:p.className,z:getComputedStyle(p).zIndex,pos:getComputedStyle(p).position,pe:getComputedStyle(p).pointerEvents});p=p.parentElement}
      return out;
    """)

def dispatch_mouse_center():
    g=geometry(); x=g['center']['x']; y=g['center']['y']
    driver.execute_cdp_cmd('Input.dispatchMouseEvent',{'type':'mouseMoved','x':x,'y':y})
    driver.execute_cdp_cmd('Input.dispatchMouseEvent',{'type':'mousePressed','x':x,'y':y,'button':'left','clickCount':1})
    driver.execute_cdp_cmd('Input.dispatchMouseEvent',{'type':'mouseReleased','x':x,'y':y,'button':'left','clickCount':1})
    time.sleep(.5)
    return {'geometry':g,'clicks':js(driver,'return window.__ctaClicks||0'),'faceVisible':js(driver,"return !document.getElementById('faceView').hidden")}

def dispatch_touch_center():
    g=geometry(); x=g['center']['x']; y=g['center']['y']
    driver.execute_cdp_cmd('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y,'radiusX':2,'radiusY':2,'force':1}]})
    time.sleep(.08)
    driver.execute_cdp_cmd('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
    time.sleep(.7)
    return {'geometry':g,'touches':js(driver,'return window.__ctaTouches||0'),'clicks':js(driver,'return window.__ctaClicks||0'),'faceVisible':js(driver,"return !document.getElementById('faceView').hidden")}

FIX_CSS="""
.app[data-view="today"] .hero-copy{pointer-events:none!important}
.app[data-view="today"] .hero-copy button,
.app[data-view="today"] .hero-copy input,
.app[data-view="today"] .hero-copy textarea,
.app[data-view="today"] .hero-copy select,
.app[data-view="today"] .hero-copy a,
.app[data-view="today"] .hero-copy .direct-input,
.app[data-view="today"] .hero-copy .direct-input *{pointer-events:auto!important}
"""

def inject_probe_fix():
    js(driver,"const s=document.createElement('style'); s.id='ui-cta01-remediation-probe'; s.textContent=arguments[0]; document.head.appendChild(s); return true",FIX_CSS)

try:
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',{'source':PRELOAD})
    boot()
    rec('runtime_no_no_sandbox','--no-sandbox' not in driver.capabilities.get('goog:chromeOptions',{}).get('args',[]),driver.capabilities.get('goog:chromeOptions',{}).get('args',[]),True)
    rec('real_localhost_origin',js(driver,'return location.origin')=='http://127.0.0.1:8765',js(driver,'return location.origin'),True)
    rec('real_local_storage',js(driver,"localStorage.setItem('__cta01','ok'); const v=localStorage.getItem('__cta01'); localStorage.removeItem('__cta01'); return v")=='ok',None,True)
    rec('phase16d_module_loaded',js(driver,"return !!window.Phase16DDialogueContext && Phase16DDialogueContext.VERSION_ID==='phase16d-dialogue-context-v1'"),None,True)

    # DESKTOP — actual pointer acceptance on untouched frozen candidate.
    first=produce_cta()
    rec('desktop_default_true_adapter_false_one_call',len(get_calls(driver))==1,len(get_calls(driver)),True)
    rec('desktop_first_request_history_free','dialogueContext' not in first,first,True)
    d0=dispatch_mouse_center()
    desktop_blocked=(d0['clicks']==0 and not d0['faceVisible'])
    rec('desktop_pointer_bug_reproduced',desktop_blocked,d0,False)

    # Keyboard path remains valid on untouched candidate.
    if not d0['faceVisible']:
        from selenium.webdriver.common.keys import Keys
        driver.find_element('id','dialogueAction').send_keys(Keys.ENTER)
        time.sleep(.35)
    keyboard_ok=js(driver,"return !document.getElementById('faceView').hidden")
    rec('desktop_keyboard_activation_works',keyboard_ok,{'clicks':js(driver,'return window.__ctaClicks||0')},True)

    # DESKTOP — runtime-only remediation probe (candidate bytes remain untouched).
    js(driver,"setView('today'); return true"); time.sleep(.2)
    produce_cta(); inject_probe_fix()
    d1=dispatch_mouse_center()
    desktop_fix_ok=(d1['clicks']>=1 and d1['faceVisible'])
    rec('desktop_runtime_probe_fix_restores_pointer',desktop_fix_ok,d1,False)

    # Check frozen hero-copy interactive controls remain hit-testable under probe CSS.
    js(driver,"setView('today'); return true"); time.sleep(.2)
    interactive=js(driver,r"""
      const ids=['startWithKai','todayFaceCta'];
      const out={};
      for(const id of ids){
        const e=document.getElementById(id),r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,h=document.elementFromPoint(x,y);
        out[id]={hit:(h===e||e.contains(h)),top:h?(h.id||h.className||h.tagName):null,pe:getComputedStyle(e).pointerEvents};
      }
      return out;
    """)
    rec('desktop_probe_preserves_hero_buttons',all(v['hit'] for v in interactive.values()),interactive,False)

    # MOBILE — real touch emulation on untouched frozen candidate.
    # Remove runtime probe CSS, switch device metrics, reload clean.
    js(driver,"document.getElementById('ui-cta01-remediation-probe')?.remove(); return true")
    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'mobile':True,'width':390,'height':844,'deviceScaleFactor':1})
    driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled',{'enabled':True,'maxTouchPoints':5})
    driver.refresh(); wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function' && !!window.V3CoachAdapter")
    first_m=produce_cta()
    m0=dispatch_touch_center()
    mobile_blocked=(m0['touches']==0 and m0['clicks']==0 and not m0['faceVisible'])
    rec('mobile_touch_bug_reproduced',mobile_blocked,m0,False)

    # MOBILE runtime-only remediation probe.
    inject_probe_fix()
    m1=dispatch_touch_center()
    mobile_fix_ok=((m1['touches']>=1 or m1['clicks']>=1) and m1['faceVisible'])
    rec('mobile_runtime_probe_fix_restores_touch',mobile_fix_ok,m1,False)

    # Restore desktop emulation for screenshot.
    driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride',{})
    driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled',{'enabled':False})
    driver.refresh(); wait_js(driver,"document.readyState==='complete' && typeof sendMessage==='function'")
    produce_cta()
    screenshot=driver.get_screenshot_as_png()

    logs=driver.get_log('browser')
    severe=[]
    for e in logs:
        if e.get('level')!='SEVERE': continue
        msg=e.get('message','')
        if 'Failed to load resource' in msg and ('/assets/' in msg or 'favicon.ico' in msg): continue
        severe.append(msg)
    rec('no_severe_js_console_errors',len(severe)==0,severe,True)

    causal={
      'desktopOriginal':d0,
      'desktopProbe':d1,
      'mobileOriginal':m0,
      'mobileProbe':m1,
      'interactiveAfterProbe':interactive,
      'sourceFinding':{
        'heroCopy':'position:relative; z-index:8',
        'visualStage':'position:absolute; z-index:2',
        'todayDialogueSlot':'child of visualStage; position:absolute; z-index:8',
        'interpretation':'todayDialogueSlot cannot escape visualStage stacking context z=2; transparent hero-copy z=8 can intercept hit testing above it'
      }
    }
    bug_confirmed=desktop_blocked and mobile_blocked
    remediation_feasible=desktop_fix_ok and mobile_fix_ok
    verdict='UI_CTA01_CONFIRMED_HOTFIX_REQUIRED' if bug_confirmed else 'UI_CTA01_NOT_REPRODUCED'
    evidence={
      'schema':'UI_CTA01_ACCEPTANCE_AUDIT_V1_0',
      'candidateSha256':EXPECTED_SHA,
      'projectionManifestCount':len(SOURCE_HASHES),
      'projectionHashesVerified':True,
      'runner':{
        'os':subprocess.check_output(['bash','-lc','source /etc/os-release && echo $PRETTY_NAME'],text=True).strip(),
        'chromeVersion':subprocess.check_output(['google-chrome','--version'],text=True).strip(),
        'chromeBinary':subprocess.check_output(['which','google-chrome'],text=True).strip(),
        'euid':os.geteuid(),'configuredArgs':opts.arguments
      },
      'bugConfirmed':bug_confirmed,
      'remediationProbeFeasible':remediation_feasible,
      'candidateModified':False,
      'causalEvidence':causal,
      'severeJsConsoleErrors':severe,
      'checks':checks,
      'failedHardChecks':[c['name'] for c in checks if not c['pass'] and c['name'] in ['candidate_sha_meta_exact','configured_no_no_sandbox','runtime_no_no_sandbox','real_localhost_origin','real_local_storage','phase16d_module_loaded','desktop_keyboard_activation_works','no_severe_js_console_errors']],
      'diagnosticFalseChecks':[c['name'] for c in checks if not c['pass']],
      'screenshot':{'sha256':sha(screenshot),'bytes':len(screenshot)},
      'verdict':verdict
    }
    print('UI_CTA01_EVIDENCE_JSON='+json.dumps(evidence,separators=(',',':'),ensure_ascii=False),flush=True)
    enc=base64.b64encode(screenshot).decode('ascii')
    for i in range(0,len(enc),6000):
        print('UI_CTA01_SCREENSHOT_B64_CHUNK='+enc[i:i+6000],flush=True)
    print(verdict,flush=True)
finally:
    driver.quit()
