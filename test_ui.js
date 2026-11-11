/* Static UI contract checks. No browser or third-party dependencies needed. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('ui/index.html', 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
for (const [, script] of scripts) new Function(script);
const start = html.indexOf('  const i18n = ');
const end = html.indexOf('  const $ = ', start);
assert(start > 0 && end > start);
const dictionary = new Function(html.slice(start, end) + '; return i18n;')();
assert.deepEqual(Object.keys(dictionary.zh).sort(), Object.keys(dictionary.en).sort());
for (const language of ['zh', 'en']) {
  for (const [key, value] of Object.entries(dictionary[language])) {
    assert.equal(typeof value, 'string', key);
    assert(value.trim().length > 0, key);
    const other = language === 'zh' ? 'en' : 'zh';
    assert.deepEqual([...value.matchAll(/\{(\w+)\}/g)].map(x => x[1]).sort(),
      [...dictionary[other][key].matchAll(/\{(\w+)\}/g)].map(x => x[1]).sort(), key);
  }
}
for (const [, key] of html.matchAll(/data-i18n(?:-placeholder|-aria|-title)?="([^"]+)"/g)) {
  assert(key in dictionary.zh, 'Missing translation: ' + key);
}
for (const [, key] of html.matchAll(/\bt\('([^']+)'\s*[,)]/g)) {
  assert(key in dictionary.zh, 'Missing dynamic translation: ' + key);
}
for (const state of ['IDLE','LOCKED_START','CRUISING_LOOP','CRUISING_DETOUR','SAFE_STOPPED']) assert('state_'+state in dictionary.zh);
const python = fs.readFileSync('main.py','utf8');
for (const [, code] of python.matchAll(/AppError\("([^"]+)"\)/g)) assert(code in dictionary.zh, 'Untranslated API error: '+code);
for (const [key, value] of Object.entries(dictionary.en)) assert(!/[\u3400-\u9fff]/.test(value), key);
assert.match(html, /backdrop-filter:blur\(28px\) saturate\(190%\)/);
assert.match(html, /font-variant-numeric:tabular-nums/);
assert.match(html, /doubleClickZoom:false/);
assert.match(html, /preferCanvas:true/);
assert.deepEqual([...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map(match=>match[1]), ['/vendor/leaflet.js']);
assert.deepEqual([...html.matchAll(/<link rel="stylesheet" href="([^"]+)"/g)].map(match=>match[1]), ['/vendor/leaflet.css']);
assert.match(html, /meta name="referrer" content="no-referrer"/);

// Verify tile coordinates and layer order in both renderers, including the canvas fit zoom.
const mapSource = html.slice(html.indexOf('  const MAP_NATIVE_MAX_ZOOM'),
  html.indexOf('  function bearing('));
const leafletTiles = [];
const canvasTiles = [];
const mapModel = {layer: 'hybrid'};
const MapView = new Function('L', 'model', '$', 'Image', 'getComputedStyle', 'document',
  mapSource + '; return MapView;')(
  {tileLayer: (url, options) => ({url, options, on() {}})}, mapModel,
  () => ({setAttribute() {}}), class { set src(url) { canvasTiles.push(new URL(url)); } },
  () => ({fontFamily: 'sans-serif'}), {body: {}});
const map = Object.assign(Object.create(MapView.prototype), {
  leaflet: true, lang: 'en', tiles: new Map(),
  baseGroup: {clearLayers() { leafletTiles.length = 0; }, addLayer(layer) { leafletTiles.push(layer); }},
});
map.setLayer('hybrid');
assert.deepEqual(leafletTiles.map(tile => new URL(tile.url.replace('{s}', '2')).searchParams.get('style')), ['6', '8']);
for (const tile of leafletTiles) {
  assert.match(tile.url, /&x=\{x\}&y=\{y\}&z=\{z\}$/);
  assert.equal(tile.options.maxNativeZoom, 18);
}
map.setLayer('street');
assert.match(leafletTiles[0].url, /webst0\{s\}.*style=7&lang=en/);
map.lang = 'zh';
map.setLayer('street');
assert.match(leafletTiles[0].url, /webrd0\{s\}.*style=8&lang=zh_cn/);

const canvasMap = Object.assign(Object.create(MapView.prototype), {
  leaflet: false, lang: 'en', width: 1000, height: 700, tiles: new Map(),
  shapes: {track: [], anchors: [], active: [], draft: []},
  ctx: {clearRect() {}, fillRect() {}, strokeRect() {}, fillText() {}},
});
canvasMap.fit([[35.97647, 120.20660], [35.97649, 120.20662]]);
assert.equal(canvasMap.zoom, 18);
assert(canvasTiles.length > 0);
assert(canvasTiles.every(url => url.searchParams.get('z') === '18' &&
  url.searchParams.get('style') === '7' && url.searchParams.get('lang') === 'en'));
canvasTiles.length = 0;
canvasMap.setLayer('hybrid');
assert(canvasTiles.length > 0 && canvasTiles.length % 2 === 0);
for (let i = 0; i < canvasTiles.length; i += 2) {
  const [satellite, roads] = canvasTiles.slice(i, i+2);
  assert.equal(satellite.searchParams.get('style'), '6');
  assert.equal(roads.searchParams.get('style'), '8');
  for (const axis of ['x', 'y', 'z']) assert.equal(satellite.searchParams.get(axis), roads.searchParams.get(axis));
}

// The first-run decision must come from the saved template, not browser storage.
const firstRunStart = html.indexOf('  function shouldAutoStartTutorial(');
const firstRunEnd = html.indexOf('  function considerTutorial()', firstRunStart);
const shouldStartTutorial = new Function(html.slice(firstRunStart, firstRunEnd) + '; return shouldAutoStartTutorial;')();
assert.equal(shouldStartTutorial(null), false);
assert.equal(shouldStartTutorial({}), false);
assert.equal(shouldStartTutorial({track_points: []}), true);
assert.equal(shouldStartTutorial({track_points: [{}, {}]}), true);
assert.equal(shouldStartTutorial({track_points: [{}, {}, {}]}), false);
const tutorialStart = html.indexOf('  const TUTORIAL_STEPS = ');
const tutorialEnd = html.indexOf('  const tutorial = ', tutorialStart);
const tutorialSteps = new Function(html.slice(tutorialStart, tutorialEnd) + '; return TUTORIAL_STEPS;')();
assert.equal(tutorialSteps.length, 5);
for (let step = 1; step <= 5; step++) {
  for (const suffix of ['Title', 'Body']) {
    const key = `tutorial${step}${suffix}`;
    assert(key in dictionary.zh && key in dictionary.en, key);
    assert(!/[a-zA-Z]/.test(dictionary.zh[key]), 'Chinese tutorial contains English: ' + key);
  }
}
for (const key of ['tutorial','tutorialProgress','tutorialPrevious','tutorialNext','tutorialFinish','tutorialLater','tutorialHint','check7','settleOrder']) {
  assert(key in dictionary.zh && key in dictionary.en, key);
}
assert.match(html, /mapView\.render\(\);considerTutorial\(\);return true/);
assert.match(html, /function openModal\(kind\)\{closeTutorial\(/, 'Safety modals must preempt the tutorial');
const teachingCode = html.slice(tutorialStart, html.indexOf('  // Complete metric map adapter.', tutorialStart));
assert(!/\bapi\(/.test(teachingCode), 'A tutorial must never issue motion or device commands');
assert.match(html, /if\(kind==='checklist'\)model\.checks=Array\(7\)\.fill\(false\)/);
assert.match(python, /PREFLIGHT_CHECK_COUNT = 7/);
assert.match(html, /weather-check/);
assert.match(html, /settlement-order/);
const saveStart = html.indexOf('  function scheduleTrackSave()');
const saveEnd = html.indexOf('  function saveView()', saveStart);
const timers = [];
const saveModel = {track: [], connected: true, curveLength: 0};
const scheduleSave = new Function('model','trackLocked','setTimeout','clearTimeout','saveTimer',
  html.slice(saveStart, saveEnd)+'; return scheduleTrackSave;')(
    saveModel, () => false, callback => { timers.push(callback); return timers.length; }, () => undefined, 0);
for (const count of [1, 2]) {
  saveModel.track = Array.from({length:count}, () => ({lat:31,lon:121}));
  scheduleSave();
  assert.equal(timers.length, 0, 'Unfinished tracks must not trigger invalid-save errors');
}
saveModel.track = [{lat:31,lon:121},{lat:31.001,lon:121},{lat:31.001,lon:121.001}];
scheduleSave();
assert.equal(timers.length, 0, 'Invalid geometry must not be autosaved');
saveModel.curveLength = 400;
scheduleSave();
assert.equal(timers.length, 1, 'Valid tracks must still autosave');
saveModel.track = [];
scheduleSave();
assert.equal(timers.length, 2, 'Clearing a track must still persist');
assert.match(html, /const TRACE_LIMIT = 3600/);
assert.match(html, /id="current-lap"/);
assert.match(html, /id="previous-lap"/);
assert.match(html, /renderer:L\.canvas\(/);
for (const key of ['currentLap','lapNumber','previousLap','previousLapDetail','traceUnavailable']) assert(key in dictionary.zh);
for (const key of ['trackWidth','trackWidthNote','invalid_track_width','detour_crosses_infield']) assert(key in dictionary.zh);
assert.match(html, /id="track-width" type="number" min="1" max="8"/);
assert.match(html, /track_width_m:Number\(\$\('track-width'\)\.value\)/);
assert.match(html, /\$\('track-width'\)\.disabled=active\|\|model\.busy/);
const traceStart=html.indexOf('  function formatDuration('),traceEnd=html.indexOf('  let traceBusy=',traceStart);
const traceModel={trace:[],traceSession:null,traceSequence:0};
const rendered=[];
const traceFunctions=new Function('model','TRACE_LIMIT','mapView',html.slice(traceStart,traceEnd)+';return {ingestTrace,formatDuration,formatPace};')(traceModel,3600,{setTrace:points=>rendered.push(points)});
assert.equal(traceFunctions.formatDuration(151), '02:31');
assert.equal(traceFunctions.formatPace(378), '6′18″');
const tracePoints=Array.from({length:4000},(_,index)=>({sequence:index+1,lat:31+index/1e7,lon:121}));
traceFunctions.ingestTrace({reset:true,session_id:'one',sequence:4000,points:tracePoints});
assert.equal(traceModel.trace.length,3600);
assert.equal(traceModel.trace[0].sequence,401);
traceFunctions.ingestTrace({reset:false,session_id:'one',sequence:4000,points:tracePoints.slice(-10)});
assert.equal(traceModel.trace.length,3600,'Repeated deltas must not duplicate points');
traceFunctions.ingestTrace({reset:true,session_id:'two',sequence:1,points:[{sequence:1,lat:32,lon:120}]});
assert.equal(traceModel.trace.length,1,'New sessions must clear the previous trace');
assert.deepEqual(rendered.at(-1),[[32,120]]);

async function checkTraceConcurrency(){
  const source=html.slice(traceEnd,html.indexOf('  let priorFocus=null;',traceEnd));
  const state={state:{session_id:'old',trace_sequence:1},traceSession:null,traceSequence:0};
  let complete,accepted=0;
  const sync=new Function('model','performance','api','ingestTrace','toast',source+';return syncTrace;')(
    state,{now:()=>0},()=>new Promise(resolve=>{complete=resolve;}),()=>{accepted++;},()=>{});
  const request=sync();
  state.state={session_id:'new',trace_sequence:0};
  complete({session_id:'old',points:[],sequence:1,reset:true});
  await request;
  assert.equal(accepted,0,'A response from an old session must be ignored');
}
checkTraceConcurrency().then(()=>console.log(`UI contracts passed: JavaScript syntax, ${Object.keys(dictionary.zh).length} translation pairs, tutorial, lap metrics, 3600-point delta history, session isolation, safety gates and map settings.`)).catch(error=>{console.error(error);process.exitCode=1;});

// Exercise user-visible failure and finish states, not just markup presence.
const nodes = new Map();
const element = id => {
  if (!nodes.has(id)) nodes.set(id, {textContent:'',disabled:false,style:{},classes:new Set(),classList:{
    toggle(name,enabled){if(enabled)this.owner.classes.add(name);else this.owner.classes.delete(name);},
    owner:null
  }});
  const node=nodes.get(id);node.classList.owner=node;return node;
};
const presentationModel={busy:false,connected:true,track:[{},{},{}],curveLength:400,state:{
  state:'SAFE_STOPPED',session_active:true,engine_issue:true,device:{mode:'SANDBOX_MODE'},
  distance_m:50,target_m:100,elapsed_s:20,speed_mps:0,laps:0,current_lap:1,
  hold_elapsed_s:12,countdown_s:0,stop_reason:'engine_error',system:{},settling:false
}};
let presentationLanguage='zh';
const translate=(key,values={})=>dictionary[presentationLanguage][key].replace(/\{(\w+)\}/g,(_,k)=>values[k]);
const presentationStart=html.indexOf('  function renderState() {');
const presentationEnd=html.indexOf('  function animatedPosition(',presentationStart);
const present=new Function('model','$','t','formatCoordinate','formatDuration','performance','restCooldownUntil',
  html.slice(presentationStart,presentationEnd)+';return renderState;')(
    presentationModel,element,translate,()=>'',n=>String(n),{now:()=>0},0);
present();
assert.equal(element('state-caption').textContent,dictionary.zh.engineStopped);
assert.equal(element('goal-text').textContent,dictionary.zh.goalFailed);
assert.equal(element('session-error').textContent,dictionary.zh.engineStoppedNote);
assert(!element('session-error').classes.has('hidden'));
assert(!element('safe-settle').disabled,'Engine failure must leave settlement available');
presentationModel.state.engine_issue=false;
presentationModel.state.finishing=true;
presentationModel.state.state='CRUISING_LOOP';
present();
assert.equal(element('state-caption').textContent,dictionary.zh.finishing);
assert.equal(element('session-note').textContent,dictionary.zh.finishingNote);
assert(element('session-error').classes.has('hidden'));
presentationModel.state.state='SAFE_STOPPED';presentationModel.state.finishing=false;
presentationModel.state.distance_m=100;
present();
assert(element('session-note').textContent.includes('12'),'Holding duration must be separate from motion time');
presentationModel.state.session_active=false;present();
assert.equal(element('goal-text').textContent,dictionary.zh.goalEnded,'Restored sessions must not ask the user to submit again');

const initialPresentation={...presentationModel.state};
for(const [phase,seconds] of [['injecting',0],['countdown',4],['interrupted',0],['ready',0]]) {
  presentationModel.state={...initialPresentation,state:'LOCKED_START',session_active:true,
    start_phase:phase,countdown_s:seconds};
  present();
  assert.equal(element('start-cruise').disabled,phase!=='ready');
  assert(!element('discard-session').disabled);
  if(phase==='injecting')assert.equal(element('session-note').textContent,dictionary.zh.start_injecting);
  if(phase==='interrupted')assert.equal(element('session-error').textContent,dictionary.zh.start_interrupted);
}
for(const phase of ['running','slowing','WALKING_REST']) {
  presentationModel.state={...initialPresentation,state:'CRUISING_LOOP',session_active:true,pause_phase:phase};
  present();
  assert.equal(element('hold-position').classes.has('hidden'),phase==='running');
}
presentationModel.state={...initialPresentation,restore_dispatched:true};
present();
assert.equal(element('session-note').textContent,dictionary.zh.restoredNote);
assert.match(element('session-note').textContent,/恢复指令已发送.*地图.*重启手机/);
presentationModel.state=initialPresentation;

const dialogModel={language:'zh',modal:'checklist',dialogError:null};
const errorStart=html.indexOf('  function showError(error) {');
const errorEnd=html.indexOf('  function applyLanguage()',errorStart);
let outsideErrors=0;
const errors=new Function('model','$','t','i18n','toast',html.slice(errorStart,errorEnd)+';return {showError,renderDialogError};')(
  dialogModel,element,translate,dictionary,()=>outsideErrors++);
errors.showError(new Error('clock_required'));
assert.equal(element('dialog-error').textContent,dictionary.zh.clock_required);
assert(!element('dialog-error').classes.has('hidden'));
errors.renderDialogError();
assert.equal(element('dialog-error').textContent,dictionary.zh.clock_required,'Modal rerenders must retain failures');
for(const [code,detail] of [
  ['device_usb_not_detected',/USB 未检测到 iPhone.*iTunes.*信任此电脑/],
  ['device_pairing_unready',/USB 已识别到 iPhone.*信任此电脑/],
  ['device_rsd_unready',/RSD.*管理员.*开发者模式/],
  ['device_ddi_unready',/DDI.*开发者模式/],
  ['device_dvt_unready',/DVT.*管理员.*开发者模式/],
]){
  errors.showError(new Error(code));
  assert.match(element('dialog-error').textContent,detail);
}
dialogModel.modal=null;errors.showError(new Error('connectionLost'));
assert.equal(outsideErrors,1);
console.log('Finish, engine failure, settlement availability and persistent inline dialog errors passed.');


presentationModel.state.session_active=true;
presentationModel.state.state='CRUISING_LOOP';
presentationModel.state.distance_m=30;
presentationModel.state.pause_phase='slowing';
present();
assert.equal(element('rest-toggle').textContent,dictionary.zh.restSlowing);
assert(element('rest-toggle').disabled);
assert(!element('rest-panel').classes.has('hidden'));
presentationModel.state.pause_phase='WALKING_REST';
presentationModel.state.rest_elapsed_s=12;presentationModel.state.rest_total_s=24;presentationModel.state.session_elapsed_s=44;
present();
assert.equal(element('rest-toggle').textContent,dictionary.zh.restEnd);
assert(!element('rest-toggle').disabled);
assert.equal(element('state-caption').textContent,dictionary.zh.restResting);
assert(element('rest-times').textContent.includes('24'));
presentationModel.state.pause_phase='resuming';present();
assert.equal(element('rest-toggle').textContent,dictionary.zh.restResuming);
assert(element('rest-toggle').disabled);
presentationModel.state.pause_phase='running';present();
assert.equal(element('rest-toggle').textContent,dictionary.zh.restButton);
assert(!element('rest-toggle').disabled);
presentationModel.state.finishing=true;present();
assert(element('rest-toggle').disabled);
presentationModel.state.target_walking=true;presentationModel.state.target_reached=true;
presentationModel.state.pause_phase='WALKING_REST';presentationModel.state.distance_m=120;present();
assert.equal(element('state-caption').textContent,dictionary.zh.targetWalking);
assert.equal(element('session-note').textContent,dictionary.zh.targetWalkingNote);
assert.equal(element('rest-status').textContent,dictionary.zh.targetWalkingNote);
assert.equal(element('rest-toggle').textContent,dictionary.zh.targetWalkingButton);
assert(element('rest-toggle').disabled);
assert(!element('safe-settle').disabled);
presentationModel.state.target_walking=false;presentationModel.state.target_reached=false;
console.log('Rest button phases, control availability and separate clocks passed.');

// Polling must preserve the clickable text node while its phase is unchanged.
let buttonWrites=0,buttonText=element('rest-toggle').textContent;
Object.defineProperty(element('rest-toggle'),'textContent',{configurable:true,get(){return buttonText;},set(value){buttonWrites++;buttonText=value;}});
presentationModel.state.finishing=false;presentationModel.state.pause_phase='WALKING_REST';present();
const writesAfterTransition=buttonWrites;present();present();
assert.equal(buttonWrites,writesAfterTransition,'Polling must not replace rest-button text during a click');

// Render every new control phase in both languages, with live moving metrics.
const savedPresentation=structuredClone(presentationModel.state);
for (const language of ['zh','en']) {
  presentationLanguage=language;
  const labels=dictionary[language];
  Object.assign(presentationModel.state,{session_active:true,state:'CRUISING_LOOP',finishing:false,
    target_walking:false,stop_pending:false,control_cooldown_s:0,speed_mps:1.45,stride_estimate_m:1.45/3.2});
  for (const [phase,key] of Object.entries({running:'restButton',slowing:'restSlowing',WALKING_REST:'restEnd',resuming:'restResuming'})) {
    presentationModel.state.pause_phase=phase;present();
    assert.equal(element('rest-toggle').textContent,labels[key]);
    assert.equal(element('rest-toggle').disabled,['slowing','resuming'].includes(phase));
    assert(!element('rest-status').textContent.includes('undefined'));
  }
  presentationModel.state.pause_phase='WALKING_REST';presentationModel.state.control_cooldown_s=1;present();
  assert(element('rest-toggle').disabled,'Backend cooldown must disable the control');
  Object.assign(presentationModel.state,{target_walking:true,finishing:true,control_cooldown_s:0});present();
  assert.equal(element('rest-toggle').textContent,labels.targetWalkingButton);
  assert.equal(element('session-note').textContent,labels.targetWalkingNote);
  assert(element('rest-toggle').disabled);
  assert(!element('safe-settle').disabled);
  for (const state of ['IDLE','LOCKED_START','SAFE_STOPPED']) {
    Object.assign(presentationModel.state,{state,target_walking:false,finishing:false});present();
    assert(element('rest-toggle').disabled,'Walking control must be disabled outside cruise');
  }
}
presentationModel.state=savedPresentation;presentationLanguage='zh';
console.log('Chinese and English: all walking phases, cooldown, disabled states and manual settlement rendered successfully.');

async function checkWalkingControlCooldown(){
  const a=html.indexOf('  let restCooldownUntil=0;'),b=html.indexOf("  $('rest-toggle').addEventListener",a);
  const m={busy:false,state:{pause_phase:'running'}},clock={value:0,now(){return this.value;}},calls=[];
  const action=new Function('model','performance','busyAction','acceptState','api',html.slice(a,b)+';return toggleWalkingBreak;')(
    m,clock,async fn=>await fn(),()=>{},async method=>{calls.push(method);return {};});
  await Promise.all(Array.from({length:5},()=>action()));
  assert.deepEqual(calls,['pause']);
  clock.value=1499;await action();assert.equal(calls.length,1);
  clock.value=1500;m.state.pause_phase='WALKING_REST';await action();
  assert.deepEqual(calls,['pause','resume']);
  console.log('Walking controls: 1500 ms cooldown and five-click burst passed.');
}
checkWalkingControlCooldown().catch(error=>{console.error(error);process.exitCode=1;});

// An earlier dirty session is visible before cruise and blocks a new lock.
for (const language of ['zh','en']) {
  presentationLanguage=language;
  Object.assign(presentationModel.state,{state:'IDLE',session_active:false,recovery_required:true,
    recovering:false,recovery_invalid:false,recovery_sandbox:false,engine_issue:false});
  present();
  assert(!element('recovery-panel').classes.has('hidden'));
  assert.equal(element('recovery-message').textContent,dictionary[language].recoveryNotice);
  assert(!element('recover-previous').disabled);
  assert(element('lock-start').disabled);
  presentationModel.state.recovering=true;present();
  assert(element('recover-previous').disabled);
  assert.equal(element('recover-previous').textContent,dictionary[language].recoveringLocation);
  presentationModel.state.recovering=false;presentationModel.state.recovery_sandbox=true;present();
  assert(element('recover-previous').disabled);
  assert.equal(element('recovery-message').textContent,dictionary[language].recovery_sandbox);
  presentationModel.state.recovery_sandbox=false;presentationModel.state.recovery_invalid=true;present();
  assert(element('recover-previous').disabled);
  assert.equal(element('recovery-message').textContent,dictionary[language].recovery_invalid);
  presentationModel.state.recovery_invalid=false;presentationModel.state.recovery_required=false;present();
  assert(element('recovery-panel').classes.has('hidden'));
  assert(!element('lock-start').disabled);
}
presentationModel.state=savedPresentation;presentationLanguage='zh';
console.log('Dirty-session recovery: startup warning, matching action, busy, sandbox, invalid marker and cleared states passed in both languages.');

// Idle connection failures must carry concrete diagnostics in both languages.
for(const language of ['zh','en']) {
  presentationLanguage=language;
  presentationModel.state={...savedPresentation,state:'IDLE',session_active:false,engine_issue:false,
    device_issue:true,device_error:'Permission denied: <script>unsafe()</script>',
    device:{mode:'SANDBOX_MODE',error_code:'multiple_devices'}};
  present();
  assert.equal(element('session-error').textContent,dictionary[language].multiple_devices+'\nPermission denied: <script>unsafe()</script>');
  assert(!element('session-error').classes.has('hidden'));
  assert(!Object.hasOwn(element('session-error'),'innerHTML'),'Diagnostics must remain literal text');
}
presentationModel.state=savedPresentation;presentationLanguage='zh';

async function checkBridgeDeadlinesAndStateOrdering() {
  const apiStart=html.indexOf('  async function api('),apiEnd=html.indexOf('  function showError(',apiStart);
  const source=html.slice(apiStart,apiEnd);
  const timers=new Map();let nextTimer=0,finishNative;
  const schedule=(callback,delay)=>{assert.equal(delay,8000);timers.set(++nextTimer,callback);return nextTimer;};
  const cancel=id=>timers.delete(id);
  const native={api:{get_state:()=>new Promise(resolve=>{finishNative=resolve;})}};
  const call=new Function('window','document','setTimeout','clearTimeout',source+';return api;')(
    {pywebview:native},{},schedule,cancel);
  const busyStart=html.indexOf('  async function busyAction('),busyEnd=html.indexOf("  $('dialog').addEventListener",busyStart);
  const busyModel={busy:false,modal:null};const errors=[];
  const action=new Function('model','renderDialogError','renderState','renderEditor','showError',
    html.slice(busyStart,busyEnd)+';return busyAction;')(busyModel,()=>{},()=>{},()=>{},error=>errors.push(error.message));
  const pending=action(()=>call('get_state'));
  assert(busyModel.busy);
  await Promise.resolve();
  [...timers.values()][0]();
  await pending;
  assert(!busyModel.busy,'A native bridge timeout must release busyAction');
  assert.deepEqual(errors,['api_timeout']);assert.equal(timers.size,0);
  finishNative({ok:true,data:{seq:1}});await Promise.resolve();
  native.api.get_state=()=>Promise.resolve({ok:true,data:{seq:2}});
  assert.deepEqual(await call('get_state'),{seq:2});assert.equal(timers.size,0);
  native.api.get_state=()=>{throw new Error('bridge failed');};
  await assert.rejects(call('get_state'),/bridge failed/);assert.equal(timers.size,0);
  const fallback=new Function('window','document','setTimeout','clearTimeout','fetch','AbortController',source+';return api;')(
    {},{querySelector:()=>({content:'session-token'})},schedule,cancel,
    (url,options)=>new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted')))),AbortController);
  const http=fallback('get_state');[...timers.values()][0]();
  await assert.rejects(http,/api_timeout/);assert.equal(timers.size,0);

  const stateStart=html.indexOf('  function acceptState('),stateEnd=html.indexOf('  function renderState()',stateStart);
  const stateModel={state:null,stateSeq:0,activePath:[],modal:null};let renders=0;
  const accept=new Function('model','mapView','renderState','renderEditor','syncTrace','toast','performance',
    "let lastPositionKey='',motion=null,announcedReady=false;const systemNotices=new Set();"+
    html.slice(stateStart,stateEnd)+';return acceptState;')(
      stateModel,{render:()=>{},setPosition:()=>{}},()=>{renders++;},()=>{},()=>{},()=>{},{now:()=>0});
  const snapshot=(seq,active)=>({seq,session_active:active,state:active?'CRUISING_LOOP':'IDLE',
    device:{low_battery:false},clock:{},system:{},gcj_position:null});
  assert(accept(snapshot(10,false)));assert(accept(snapshot(12,true)));
  const count=renders;
  assert.equal(accept(snapshot(11,false)),false);
  assert.equal(accept(snapshot(undefined,false)),false);
  assert.equal(stateModel.state.seq,12);assert(stateModel.state.session_active);assert.equal(renders,count);
  assert(accept(snapshot(13,false)));assert.equal(stateModel.stateSeq,13);
  // A newer poll can arrive before a successful action response. Keep its
  // state, while still finishing the action's dialog lifecycle.
  const lockSource=html.slice(html.indexOf("    if(id==='confirm-lock')"),html.indexOf("    if(id==='confirm-activate')"));
  let closed=0;const lockButton={};
  await new Function('id','busyAction','clearTimeout','saveTimer','api','model','$','acceptState','closeModal',
    'button','t',
    'return (async()=>{'+lockSource+'})();')('confirm-lock',fn=>fn(),()=>{},0,
      async()=>snapshot(12,true),{track:[],checks:[]},()=>({value:'2',checked:false}),accept,()=>{closed++;},
      lockButton,translate);
  assert.equal(lockButton.textContent,dictionary.zh.start_injecting);
  assert.equal(closed,1,'A successful action must finish even when its snapshot is older than a poll');
  assert.equal(stateModel.stateSeq,13);
  assert.match(html,/finally\{setTimeout\(poll,/,'Polling must reschedule after errors');
  console.log('Native bridge/HTTP deadlines, busy recovery, stale-state rejection and idle diagnostics passed.');
}
checkBridgeDeadlinesAndStateOrdering().catch(error=>{console.error(error);process.exitCode=1;});
