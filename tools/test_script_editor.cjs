const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../server/static/index.html'), 'utf8');
const editor = html.slice(html.indexOf('function getSSValues()'), html.indexOf('function collectImgBundle()'));

function createEditor(storage = {}) {
  const renderedRows = [];
  const elements = {
    'default-delay': { value: '1000' },
    'steps-tbody': { innerHTML: '', appendChild(row) { renderedRows.push(row); } },
    'empty-hint': { style: {} },
    'step-cnt': {},
    'loop-cb': { checked: true },
    'script-name': { value: '' },
    'ss-delay': { value: '1000' },
    'ss-sid': { value: '2' },
    'ss-angle': { value: '45' },
    'ss-speed': { value: '50' },
    'ss-dur': { value: '300' },
    'ss-home': { checked: true },
    'ss-home-mk': { textContent: '1' },
    'home-desc': { style: {}, innerHTML: '' },
    'srv-action-list': { innerHTML: '' },
    'srv-action-label-form': { hidden: true },
    'srv-action-label': { value: '', focus() {} },
    'srv-action-rename-input': { value: '', focus() {} },
    'srv-action-label-error': { textContent: '' },
    'srv-action-edit-controls': { hidden: true },
    'srv-action-update-btn': { hidden: true },
    'srv-action-cancel-edit-btn': { hidden: true },
    'script-resume-btn': { hidden: true, disabled: false },
    'script-pause-btn': { disabled: false },
    'runtime-pause-btn': { disabled: false },
    'kb-lock-lbl': { style: {} },
    'kb-lock-msg': { style: {} },
    'sc-prog-step': { textContent: 'SRV 1/3' },
    'runtime-jobs': { innerHTML: '', value: '' },
    'runtime-content': { innerHTML: '' },
    'runtime-meta': { textContent: '' },
    'runtime-follow': { checked: true },
    'runtime-prog-lbl': { textContent: '' },
    'runtime-prog-fi': { style: {} },
    'runtime-prog-step': { textContent: '' },
  };
  const resetCalls = [];
  const context = vm.createContext({
    elements,
    resetCalls,
    renderedRows,
    storage,
    attachedMap: {},
    localStorage: {
      getItem: key => storage[key] ?? null,
      setItem: (key, value) => { storage[key] = String(value); },
      removeItem: key => { delete storage[key]; },
    },
    steps: [],
    stepIdCnt: 0,
    dragSrc: 2,
    homeVal: 1,
    servoActions: [],
    editingServoActionId: null,
    servoActionRenameId: null,
    servoActionLabelMode: null,
    servoActionLabelTargetId: null,
    kbLocked: true,
    scriptPauseState: null,
    scriptStatusRevision: 0,
    scriptStatusRequest: 0,
    fifoState: { running:false, queued:0, paused:false, pause:null, waiting:null },
    runtimeState: {session_id:null,revision:-1,active:null,queued:[]},
    runtimeSelectedId: null,
    runtimeFollow: true,
    runtimeDetailRequest: 0,
    runtimeDetailController: null,
    runtimeDetailJobId: null,
    runtimeRetiredSessions: new Set(),
    scriptView: 'edit',
    snapshots: [{ id: 'old-snapshot' }],
    conditions: [{ condition_id: 'old-condition' }],
    visualRevision: 0,
    renderSnapList: () => resetCalls.push('snapshots'),
    renderCondList: () => resetCalls.push('conditions'),
    renderImgPicker: () => resetCalls.push('picker'),
    closeCondModal: () => resetCalls.push('modal'),
    setFifoQueueData: value => resetCalls.push(['fifo', value.queued??0]),
    setProgress: (current, total) => resetCalls.push(['progress', current, total]),
    renderFifoQueue: () => resetCalls.push('render-fifo'),
    setHome: value => {
      elements['ss-home'].checked = value === 1;
      elements['ss-home-mk'].textContent = value === 1 ? '1' : '0';
    },
    drawCanvas: () => {},
    addLog: () => {},
    fetch: async () => ({ ok: true }),
    esc: value => String(value),
    document: {
      getElementById: id => elements[id],
      createElement: () => ({ dataset: {}, addEventListener() {} }),
    },
  });
  vm.runInContext(editor, context);
  vm.runInContext(html.match(/function esc\(s\)\{[^\r\n]+/)[0], context);
  return context;
}

test('servo quick actions persist a fixed six-field snapshot and execute that snapshot', async () => {
  const storage = {};
  const editor = createEditor(storage);
  editor.beginServoActionSave();
  editor.elements['srv-action-label'].value = 'Target PC POWER';
  editor.saveServoActionLabel();

  const action = editor.servoActions[0];
  assert.match(editor.elements['srv-action-list'].innerHTML,/button class="btn-b btn-sm srv-action-run"/);
  const expected = { delay_ms: 1000, servo_id: 2, angle: 45, speed: 50, duration_ms: 300, home: 1 };
  assert.equal(action.label, 'Target PC POWER');
  assert.deepEqual(JSON.parse(JSON.stringify(action.params)), expected);
  assert.deepEqual(JSON.parse(Object.values(storage)[0])[0].params, expected);

  editor.elements['ss-angle'].value = '120';
  editor.elements['ss-sid'].value = '4';
  let request;
  editor.fetch = async (url, options) => {
    request = { url, body: JSON.parse(options.body) };
    return { ok: true };
  };
  await editor.executeServoAction(action.id);
  assert.equal(request.url, '/srv/api/command');
  assert.deepEqual(request.body, expected);
});

test('servo quick action parameters can be loaded, cancelled, updated, renamed and deleted', () => {
  const storage = {};
  const editor = createEditor(storage);
  editor.beginServoActionSave();
  editor.elements['srv-action-label'].value = 'ESC';
  editor.saveServoActionLabel();
  const action = editor.servoActions[0];
  const original = JSON.stringify(action.params);

  editor.beginServoActionEdit(action.id);
  assert.equal(Number(editor.elements['ss-angle'].value), 45);
  assert.equal(Number(editor.elements['ss-sid'].value), 2);
  editor.elements['ss-angle'].value = '80';
  editor.cancelServoActionEdit();
  assert.equal(JSON.stringify(action.params), original);

  editor.beginServoActionEdit(action.id);
  editor.elements['ss-angle'].value = '80';
  editor.updateServoAction();
  assert.equal(action.params.angle, 80);

  editor.beginServoActionRename(action.id);
  const rowMarkup=editor.elements['srv-action-list'].innerHTML;
  assert.match(rowMarkup,/<div class="srv-action-row srv-action-renaming">[\s\S]*id="srv-action-rename-input"/);
  assert.equal(editor.elements['srv-action-label-form'].hidden,true);
  assert.equal(editor.elements['srv-action-rename-input'].value,'ESC');
  editor.elements['srv-action-rename-input'].value = 'Target PC ESC';
  editor.saveServoActionRename(action.id);
  assert.equal(action.label, 'Target PC ESC');
  assert.equal(action.params.angle, 80);

  editor.deleteServoAction(action.id);
  assert.equal(editor.servoActions.length, 0);
  assert.deepEqual(JSON.parse(Object.values(storage)[0]), []);
});

test('servo action update control is blue and labeled UPDATE',()=>{
  const button=html.match(/<button class="([^"]*)" id="srv-action-update-btn"[^>]*>([^<]*)<\/button>/);
  assert.ok(button);
  assert.match(button[1],/\bbtn-b\b/);
  assert.equal(button[2],'UPDATE');
});

test('accepted script submission switches to Execution Status and follows the active queue',()=>{
  const editor=createEditor();
  const views=[];
  editor.setScriptView=view=>views.push(view);
  editor.runtimeState.active={job_id:'active-job'};
  editor.runtimeSelectedId='queued-preview';
  editor.runtimeFollow=false;
  editor.focusRuntimeAfterSubmission();
  assert.deepEqual(views,['runtime']);
  assert.equal(editor.runtimeFollow,true);
  assert.equal(editor.runtimeSelectedId,'active-job');
});

test('Editor RUN marks its request local and leaves tab switching to external submissions',()=>{
  const runSource=html.slice(html.indexOf('async function runScript()'),html.indexOf('async function stopScript()'));
  assert.match(runSource,/['"]X-RKS-Source['"]\s*:\s*['"]script-editor['"]/);
  assert.ok(!runSource.includes('focusRuntimeAfterSubmission()'));
});

test('switching a new KBD step to KEY stores ENTER in the script payload', () => {
  const editor = createEditor();
  editor.addKbdStep();
  editor.updStepKbd(editor.steps[0]._id, 'KEY');
  const payload = JSON.parse(JSON.stringify(editor.steps[0]));
  assert.equal(payload.cmd_type, 'KEY');
  assert.equal(payload.key, 'ENTER');
});

test('switching to KEY preserves a supported special key', () => {
  const editor = createEditor();
  editor.addKbdStep();
  editor.updStep(editor.steps[0]._id, 'key', 'TAB');
  editor.updStepKbd(editor.steps[0]._id, 'KEY');
  assert.equal(editor.steps[0].key, 'TAB');
});

test('a selected special key survives a TYPE round trip', () => {
  const editor = createEditor();
  editor.addKbdStep();
  editor.updStepKbd(editor.steps[0]._id, 'KEY');
  editor.updStep(editor.steps[0]._id, 'key', 'F12');
  editor.updStepKbd(editor.steps[0]._id, 'TYPE');
  editor.updStepKbd(editor.steps[0]._id, 'KEY');
  assert.equal(editor.steps[0].key, 'F12');
});

test('switching to TYPE does not alter the existing combo key', () => {
  const editor = createEditor();
  editor.addKbdStep();
  editor.updStepKbd(editor.steps[0]._id, 'TYPE');
  assert.equal(editor.steps[0].key, 'C');
});

test('COMBO offers F10 only in the second key dropdown and preserves its selection', () => {
  const editor = createEditor();
  editor.addKbdStep();
  editor.updStep(editor.steps[0]._id, 'mod2', 'F10');
  editor.renderSteps();
  const row = editor.renderedRows.at(-1).innerHTML;
  const first = row.match(/<select[^>]+onchange="[^"]*'mod1'[^>]*>(.*?)<\/select>/s);
  const second = row.match(/<select[^>]+onchange="[^"]*'mod2'[^>]*>(.*?)<\/select>/s);
  assert.ok(first && second);
  assert.ok(!first[1].includes('F10'));
  assert.ok(second[1].includes('<option value="F10" selected>F10</option>'));
  const payload = JSON.parse(JSON.stringify(editor.steps[0]));
  assert.equal(payload.mod2, 'F10');
});

test('SRV ID selectors leave room for the ID and native dropdown arrow', () => {
  const editor = createEditor();
  for (const servoId of [1, 2, 3, 4, 5, 6]) {
    editor.steps.push({ type: 'srv', _id: servoId, servo_id: servoId,
      delay_ms: 1000, angle: 90, speed: 50, duration_ms: 1000, home: 0 });
  }
  editor.renderSteps();
  assert.equal(editor.renderedRows.length, 6);
  editor.renderedRows.forEach((row, index) => {
    const selector = row.innerHTML.match(/<select[^>]+onchange="[^"]*'servo_id'[^>]*>(.*?)<\/select>/s);
    assert.ok(selector, 'SRV row renders a Servo ID selector');
    const width = selector[0].match(/width:(\d+)px/);
    assert.ok(width && Number(width[1]) >= 44, 'Servo ID selector needs at least 44px');
    assert.ok(selector[1].includes(`<option selected>${index + 1}</option>`));
  });
});

test('workspace clear resets editor, visual lists, keyboard lock and progress', () => {
  const editor = createEditor();
  editor.addKbdStep();
  editor.applyWorkspaceCleared();
  assert.equal(editor.steps.length, 0);
  assert.equal(editor.snapshots.length, 0);
  assert.equal(editor.conditions.length, 0);
  assert.equal(editor.kbLocked, false);
  assert.equal(editor.dragSrc, null);
  assert.equal(editor.elements['loop-cb'].checked, false);
  for (const id of ['kb-lock-lbl', 'kb-lock-msg']) {
    assert.equal(editor.elements[id].style.display, 'none');
  }
  assert.equal(editor.elements['sc-prog-step'].textContent, '');
  assert.equal(editor.visualRevision, 1);
  assert.deepEqual(editor.resetCalls, ['render-fifo', 'modal', 'snapshots', 'conditions', 'picker',
    ['fifo', 0], ['progress', null, null]]);
});

test('SRV WebSocket clear notification resets the editor', () => {
  const editor = createEditor();
  editor.addKbdStep();
  const sockets = [];
  editor.location = { host: 'test' };
  editor.addLog = () => {};
  editor.WebSocket = function () { sockets.push(this); };
  vm.runInContext(html.slice(html.indexOf('function srvWSConnect()'), html.indexOf('function kbmWSConnect()')), editor);
  editor.srvWSConnect();
  sockets[0].onmessage({ data: JSON.stringify({ type: 'workspace_cleared' }) });
  assert.equal(editor.steps.length, 0);
  assert.equal(editor.visualRevision, 1);
});

test('visual responses started before clear cannot restore stale lists', async () => {
  for (const [functionName, nextFunction] of [
    ['refreshSnapList', 'renderSnapList'], ['refreshCondList', 'renderCondList'],
  ]) {
    const editor = createEditor();
    let resolveResponse;
    const response = new Promise(resolve => { resolveResponse = resolve; });
    editor.fetch = async () => ({ ok: true, json: () => response });
    vm.runInContext(html.slice(html.indexOf(`async function ${functionName}()`),
      html.indexOf(`function ${nextFunction}()`)), editor);
    const refresh = editor[functionName]();
    editor.applyWorkspaceCleared();
    resolveResponse({ snapshots: [{ id: 'stale' }], conditions: [{ condition_id: 'stale' }] });
    await refresh;
    assert.equal(editor.snapshots.length, 0);
    assert.equal(editor.conditions.length, 0);
  }
});

test('OCR steps retain normalized ROI and settings through JSON round trips', () => {
  const editor = createEditor();
  editor.addOcrStep({ roi: [0.1, 0.2, 0.3, 0.4], variable: 'digits', timeout_ms: 5000, stable_hits: 2 });
  assert.equal(editor.steps[0].type, 'ocr');
  assert.equal(editor.steps[0].variable, 'digits');
  const exported = JSON.parse(JSON.stringify(editor.steps.map(({ _id, ...step }) => step)));
  const imported = editor.normalizeImportedSteps(exported);
  assert.deepEqual(JSON.parse(JSON.stringify(imported)), exported);
  assert.equal(exported[0].snapshot_id, undefined);
  assert.ok(editor.renderedRows[0].innerHTML.includes('OCR'));
});

test('KBD variable selection uses only earlier OCR steps and inserts a literal placeholder', () => {
  const editor = createEditor();
  editor.addOcrStep({ roi: [0, 0, 1, 1], variable: 'digits' });
  editor.addKbdStep();
  const keyboard = editor.steps[1];
  editor.updStepKbd(keyboard._id, 'TYPE');
  editor.addOcrStep({ roi: [0, 0, 1, 1], variable: 'later' });
  assert.deepEqual(Array.from(editor.getOcrVariables(keyboard._id)), ['digits']);
  editor.insertKbdVariable(keyboard._id, 'digits');
  assert.equal(keyboard.text, '${digits}');
  assert.ok(editor.renderedRows.some(row => row.innerHTML.includes('digits')));
});

test('invalid OCR ROI, name and timing cannot be added', () => {
  for (const settings of [
    { roi: [0, 0, 0, 1] }, { roi: [0.9, 0, 0.2, 1] },
    { roi: [0, 0, 1, 1], variable: 'bad-name' },
    { roi: [0, 0, 1, 1], timeout_ms: 0 },
    { roi: [0, 0, 1, 1], stable_hits: 1.5 },
  ]) {
    const editor = createEditor();
    assert.throws(() => editor.addOcrStep(settings));
    assert.equal(editor.steps.length, 0);
  }
});

test('OCR ROI modal saves new steps and edits existing settings without changing delay', () => {
  const editor = createEditor();
  editor.vcmRoi = [0.1, 0.2, 0.3, 0.4];
  editor.vcmOcrStepId = null;
  editor.elements['vcm-name'] = { value: 'digits' };
  editor.elements['vcm-ocr-timeout'] = { value: '5000' };
  editor.elements['vcm-ocr-stable'] = { value: '2' };
  editor.alert = message => { throw new Error(message); };
  vm.runInContext(html.slice(html.indexOf('function ocrModalSettings()'), html.indexOf('async function testOcrRegion()')), editor);
  editor.saveOcrSettings();
  assert.equal(editor.steps[0].type, 'ocr');
  const id = editor.steps[0]._id;
  editor.steps[0].delay_ms = 123;
  editor.vcmOcrStepId = id;
  editor.elements['vcm-name'].value = 'next_digits';
  editor.saveOcrSettings();
  assert.equal(editor.steps.length, 1);
  assert.equal(editor.steps[0]._id, id);
  assert.equal(editor.steps[0].delay_ms, 123);
  assert.equal(editor.steps[0].variable, 'next_digits');
});

test('imported OCR steps materialize defaults for variable selection and export', () => {
  const editor = createEditor();
  const imported = editor.normalizeImportedSteps([
    { type: 'ocr', roi: [0, 0, 1, 1] },
    { type: 'kbd', cmd_type: 'TYPE', text: '' },
  ]);
  editor.steps = imported.map((step, index) => ({ ...step, _id: index + 1 }));
  assert.equal(editor.steps[0].variable, 'digits');
  assert.equal(editor.steps[0].timeout_ms, 5000);
  assert.equal(editor.steps[0].stable_hits, 2);
  assert.deepEqual(Array.from(editor.getOcrVariables(2)), ['digits']);
  editor.insertKbdVariable(2, 'digits');
  assert.equal(editor.steps[1].text, '${digits}');
});

test('IMAGE_MOVE steps embed their template and materialize portable defaults', () => {
  const editor = createEditor();
  editor.addImageMoveStep({ template_b64: 'dGVzdA==', source_width: 640, source_height: 360 });
  const step = editor.steps[0];
  assert.equal(step.type, 'mse');
  assert.equal(step.action, 'IMAGE_MOVE');
  assert.equal(step.image_move.button, undefined);
  assert.equal(step.image_move.stable_hits, 2);
  assert.deepEqual(Array.from(step.image_move.search_roi), [0, 0, 1, 1]);
  const exported = JSON.parse(JSON.stringify(editor.steps.map(({ _id, ...item }) => item)));
  const imported = editor.normalizeImportedSteps(exported);
  assert.deepEqual(JSON.parse(JSON.stringify(imported)), exported);
  assert.equal(imported[0].image_move.template_b64, 'dGVzdA==');
  assert.ok(editor.renderedRows.at(-1).innerHTML.includes('IMAGE_MOVE'));
});

test('saving IMAGE_MOVE into an existing MSE step preserves its ID and delay', () => {
  const editor = createEditor();
  editor.addMseStep();
  const id = editor.steps[0]._id;
  editor.steps[0].delay_ms = 123;
  editor.addImageMoveStep({ template_b64: 'dGVzdA==', source_width: 640, source_height: 360 }, id);
  assert.equal(editor.steps.length, 1);
  assert.equal(editor.steps[0]._id, id);
  assert.equal(editor.steps[0].delay_ms, 123);
  assert.equal(editor.steps[0].action, 'IMAGE_MOVE');
});

test('IMAGE_MOVE validates search region, threshold and template before adding', () => {
  for (const settings of [
    { template_b64: '' }, { search_roi: [0, 0, 0, 1] }, { threshold: 1.1 },
    { timeout_ms: 0 }, { source_width: 0 },
  ]) {
    const editor = createEditor();
    assert.throws(() => editor.addImageMoveStep({ template_b64: 'dGVzdA==', source_width: 640, source_height: 360, ...settings }));
    assert.equal(editor.steps.length, 0);
  }
});

function createImageMoveModal(){
  const editor=createEditor();
  Object.assign(editor,{vcmImageDirty:true,vcmImageConfig:null,vcmRoi:[0,0,0.2,0.2],
    vcmSnapId:'snap',vcmImageSelectionRevision:0,ocrTestRevision:0,
    ocrTestController:null,AbortController,DOMException});
  for(const [id,value] of Object.entries({'vcm-image-threshold':'0.92',
    'vcm-ocr-timeout':'5000','vcm-ocr-stable':'2','vcm-search-0':'0','vcm-search-1':'0','vcm-search-2':'1','vcm-search-3':'1'}))editor.elements[id]={value};
  for(const id of ['vcm-ocr-test','vcm-submit-btn','vcm-ocr-result','vcm-image-preview'])editor.elements[id]={disabled:false,hidden:false,textContent:''};
  vm.runInContext(html.slice(html.indexOf('function openImageMoveModal('),html.indexOf('function syncDrawCanvas()')),editor);
  return editor;
}

test('a stale template response cannot replace a newly selected image target',async()=>{
  const editor=createImageMoveModal();
  let respond,started;
  const pendingResponse=new Promise(resolve=>{started=resolve;});
  editor.fetch=async()=>({ok:true,json:()=>new Promise(resolve=>{respond=resolve;started();})});
  const controller=new AbortController();editor.ocrTestController=controller;
  const pending=editor.imageClickModalSettings(controller.signal);
  await pendingResponse;
  editor.vcmRoi=[0.5,0.5,0.2,0.2];editor.imageTargetChanged();
  respond({image_move:{template_b64:'dGVzdA==',source_width:640,source_height:360}});
  await assert.rejects(pending);
  assert.equal(editor.vcmImageConfig,null);
  assert.equal(editor.vcmImageDirty,true);
});

test('image-move save disables both actions and restores them after failure',async()=>{
  const editor=createImageMoveModal();
  let respond,started;
  const pendingResponse=new Promise(resolve=>{started=resolve;});
  editor.fetch=async()=>({ok:false,json:()=>new Promise(resolve=>{respond=resolve;started();})});
  const saving=editor.saveImageMoveSettings();
  await pendingResponse;
  assert.equal(editor.elements['vcm-submit-btn'].disabled,true);
  assert.equal(editor.elements['vcm-ocr-test'].disabled,true);
  respond({detail:'Invalid target'});await saving;
  assert.equal(editor.elements['vcm-submit-btn'].disabled,false);
  assert.equal(editor.elements['vcm-ocr-test'].disabled,false);
});

test('Snap Pool and Conditions share equal-height panels with independent scrolling', () => {
  const column = html.match(/#visual-col\{([^}]+)\}/)[1];
  assert.ok(column.includes('display:grid'));
  assert.ok(column.includes('grid-template-rows:repeat(2,minmax(0,1fr))'));
  assert.match(html, /#snap-list,#cond-list\{[^}]*min-height:0;[^}]*overflow-y:auto/);
});

test('SNAP thumbnail and CON OCR MOV delete actions share one compact row without showing ID', () => {
  const editor = createEditor();
  editor.elements['snap-list'] = { innerHTML: '' };
  editor.snapshots = [{ id: 'snapshot1', thumb_b64: 'image' }];
  vm.runInContext(html.slice(html.indexOf('function renderSnapList()'), html.indexOf('async function deleteSnapshot(')), editor);
  editor.renderSnapList();
  const markup = editor.elements['snap-list'].innerHTML;
  const row = markup.match(/<div class="snap-meta">([\s\S]*?)<\/div>\s*<\/div>/);
  assert.ok(row, 'Snapshot thumbnail and controls share one row');
  assert.ok(!markup.includes('snap-id'));
  assert.deepEqual(Array.from(row[1].matchAll(/<button[^>]*>([^<]+)<\/button>/g), match => match[1]), ['CON', 'OCR', 'MOV', '✕']);
  const snapButtonClasses=Array.from(row[1].matchAll(/<button class="([^"]*)"/g),match=>match[1]);
  assert.ok(snapButtonClasses.slice(0,3).every(value=>value.includes('btn-sm')&&!/\bbtn-[btp]\b/.test(value)));
  for (const handler of ['openCondModal', 'openOcrModal', 'openImageMoveModal']) {
    assert.ok(row[1].includes(`${handler}('snapshot1')`));
  }
  assert.ok(row[1].includes("deleteSnapshot('snapshot1')"));
  assert.match(html, /\.snap-thumb\{[^}]*width:34px;[^}]*height:24px/);
  assert.match(html, /\.snap-actions\{[^}]*display:flex/);
});

test('architecture page covers recent script, visual, firmware and editor features', () => {
  const architecture = fs.readFileSync(path.join(__dirname, '../server/static/arch.html'), 'utf8');
  for (const feature of ['fifo', 'clear', 'ocr', 'image-move', 'absolute-hid', 'editor', 'runtime-view']) {
    assert.ok(architecture.includes(`data-feature="${feature}"`), `Architecture documents ${feature}`);
  }
  for (const term of ['TESSERACT_CMD', '${digits}', 'IMAGE_MOVE', 'MOUSE:ABS', 'ABS_RANGE=0..32767',
    'workspace_cleared', 'CON / OCR / MOV', 'F10', '44px', '/visual/image-move/test',
    '/static/click-target.html', 'schema_version', 'allow_send']) {
    assert.ok(architecture.includes(term), `Architecture includes ${term}`);
  }
  assert.ok(!architecture.includes('Validate pending script release based on target-side EVT events.'));
  assert.ok(!architecture.includes('send TYPE:test.'));
});

test('FIFO pause displays retry state without unlocking the keyboard', () => {
  const editor = createEditor();
  editor.kbLocked = false;
  editor.setScriptPause({evt:'IMG.MATCH.test',step:1,total:3,message:'timeout',resuming:false});
  assert.equal(editor.elements['script-resume-btn'].hidden,false);
  assert.equal(editor.elements['script-resume-btn'].disabled,false);
  assert.equal(editor.kbLocked,true);
  editor.setScriptPause({evt:'IMG.MATCH.test',step:1,total:3,resuming:true});
  assert.equal(editor.elements['script-resume-btn'].disabled,true);
  editor.setScriptPause(null);
  assert.equal(editor.elements['script-resume-btn'].hidden,true);
  assert.equal(editor.kbLocked,true);
});

test('status refresh restores FIFO pause after reconnect or reload', async () => {
  const editor = createEditor();
  editor.fetch=async()=>({json:async()=>({pending:{total:0,buckets:[]},paused:true,
    pause:{evt:'HELLO',step:0,total:2,resuming:false}})});
  vm.runInContext(html.slice(html.indexOf('async function refreshScriptStatus()'),html.indexOf('function classifyKbm(')),editor);
  await editor.refreshScriptStatus();
  assert.equal(editor.elements['script-resume-btn'].hidden,false);
  assert.equal(editor.scriptPauseState.evt,'HELLO');
  assert.equal(editor.kbLocked,true);
});

test('late retry HTTP response cannot restore pause after WebSocket resumed it',async()=>{
  const editor=createEditor();
  editor.setScriptPause({evt:'HELLO',step:0,total:2,resuming:false});
  editor.addLog=()=>{};
  editor.refreshScriptStatus=async()=>{};
  editor.fetch=async()=>{
    editor.setScriptPause(null);
    return {ok:true,json:async()=>({ok:true,evt:'HELLO',step:0})};
  };
  await editor.resumeScript();
  assert.equal(editor.scriptPauseState,null);
  assert.equal(editor.elements['script-resume-btn'].hidden,true);
});

test('stale status cannot restore pause after stop and idle status clears pause locks',async()=>{
  const editor=createEditor();
  let respond;
  editor.fetch=async()=>({json:()=>new Promise(resolve=>{respond=resolve;})});
  vm.runInContext(html.slice(html.indexOf('async function refreshScriptStatus()'),html.indexOf('function classifyKbm(')),editor);
  const refreshing=editor.refreshScriptStatus();
  await new Promise(resolve=>setImmediate(resolve));
  editor.setScriptPause(null);editor.kbLocked=false;
  respond({running:true,paused:true,pause:{pause_id:'old',evt:'HELLO',step:0,total:1},pending:{total:0,buckets:[]}});
  await refreshing;
  assert.equal(editor.scriptPauseState,null);
  assert.equal(editor.kbLocked,false);
  editor.setScriptPause({pause_id:'old',evt:'HELLO',step:0,total:1});
  editor.fetch=async()=>({json:async()=>({running:false,paused:false,pause:null,pending:{total:0,buckets:[]}})});
  await editor.refreshScriptStatus();
  assert.equal(editor.kbLocked,false);
  assert.equal(editor.elements['kb-lock-lbl'].style.display,'none');
  assert.equal(editor.elements['kb-lock-msg'].style.display,'none');
});

test('FIFO Queue summarizes the active job, phase, current step and queued count',()=>{
  const editor=createEditor();
  editor.elements['fifo-queue-body']={innerHTML:''};
  editor.elements['fifo-queue-total']={textContent:''};
  vm.runInContext(html.slice(html.indexOf('function renderFifoQueue()'),html.indexOf('async function refreshScriptStatus()')),editor);
  editor.runtimeState.active={name:'Unlock Script',phase:'running',current_step:2,total:5};
  editor.setFifoQueueData({running:true,queued:2,waiting:{evt:'IMG.MATCH.current',step:2,total:5}});
  assert.equal(editor.elements['fifo-queue-total'].textContent,'2');
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/Waiting/);
  assert.ok(!editor.elements['fifo-queue-body'].innerHTML.includes('2 queued'));
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/Unlock Script/);
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/Step 3 \/ 5/);
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/IMG.MATCH.current/);
  assert.ok(!editor.elements['fifo-queue-body'].innerHTML.includes('<button'));
  editor.setFifoQueueData({running:true,queued:2,paused:true,pause:{evt:'HELLO',step:0,total:2}});
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/Paused/);
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/HELLO/);
  editor.runtimeState.active={name:'Unlock Script',phase:'paused',current_step:0,total:5};
  editor.setFifoQueueData({running:true,queued:2,paused:true,pause:{step:0,total:5}});
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/Step 1 \/ 5/);
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/Unlock Script/);
  editor.runtimeState.active=null;
  editor.setFifoQueueData({running:false,queued:0,waiting:null,paused:false});
  assert.match(editor.elements['fifo-queue-body'].innerHTML,/Idle/);
  assert.ok(!editor.elements['fifo-queue-body'].innerHTML.includes('HELLO'));
});

test('right column has no duplicate standalone Progress panel',()=>{
  const rightColumn=html.slice(html.indexOf('<!-- RIGHT:'),html.indexOf('<div class="pn" id="smon"'));
  assert.ok(!rightColumn.includes('<div class="ph">Progress</div>'));
  assert.ok(rightColumn.includes('<div class="ph">FIFO Queue'));
});

test('Conditions are script-controlled and expose no manual ARM or DISARM actions',()=>{
  const context=createEditor();
  context.elements['cond-list']={innerHTML:''};
  context.elements['cond-count']={textContent:''};
  context.conditions=[{condition_id:'unlock',name:'Unlock',enabled:false,in_cooldown:false,
    last_score:0.5,threshold:0.9}];
  vm.runInContext(html.slice(html.indexOf('function renderCondList()'),html.indexOf('async function testCond(')),context);
  context.renderCondList();
  const markup=context.elements['cond-list'].innerHTML;
  const conditionButtonClasses=Array.from(markup.matchAll(/<button class="([^"]*btn-sm[^"]*)"/g),match=>match[1]);
  assert.ok(!markup.includes('ARM</button>'));
  assert.ok(!markup.includes('DISARM'));
  assert.ok(!markup.includes('armCond('));
  assert.ok(!markup.includes('disarmCond('));
  assert.match(markup,/INACTIVE/);
  for(const action of ['TEST','EDIT','+SCRIPT'])assert.ok(markup.includes(action));
  assert.equal(conditionButtonClasses.length,3);
  assert.ok(conditionButtonClasses.every(value=>! /\bbtn-[btp]\b/.test(value)));
  assert.ok(!html.includes('async function armCond('));
  assert.ok(!html.includes('async function disarmCond('));
});

test('Conditions delete button aligns to the right edge like Snap Pool',()=>{
  assert.match(html,/\.cond-acts \.del-btn\s*\{[^}]*margin-left:\s*auto/s);
});

test('Mouse R click button shares the green style with L',()=>{
  assert.match(html,/\.mb-l,\s*\.mb-r\{[^}]*border:1px solid var\(--grn2\)[^}]*background:rgba\(34,197,94,\.1\)[^}]*color:var\(--grn\)/s);
  assert.match(html,/<button class="mb-r" onclick="mouseClick\('R'\)">R<\/button>/);
});

test('runtime snapshots preserve draft and reject older same-session updates',()=>{
  const editor=createEditor();editor.addKbdStep();
  const draft=JSON.stringify(editor.steps);
  editor.setRuntimeSnapshot({session_id:'session',revision:2,active:{job_id:'a',name:'Active',phase:'waiting',total:2},queued:[{job_id:'b',phase:'queued',total:1}]});
  assert.equal(editor.runtimeSelectedId,'a');
  editor.selectRuntimeJob('b');
  assert.equal(editor.runtimeFollow,false);
  editor.setRuntimeSnapshot({session_id:'session',revision:3,active:{job_id:'a',phase:'paused',total:2},queued:[{job_id:'b',phase:'queued',total:1}]});
  assert.equal(editor.runtimeSelectedId,'b');
  editor.setRuntimeSnapshot({session_id:'session',revision:1,active:null,queued:[]});
  assert.equal(editor.runtimeState.revision,3);
  assert.equal(editor.resetCalls.filter(call=>call==='render-fifo').length,2);
  assert.equal(JSON.stringify(editor.steps),draft);
});

test('runtime detail shows full escaped TYPE and current row without editable controls',()=>{
  const editor=createEditor();editor.addKbdStep();
  const draft=JSON.stringify(editor.steps);
  editor.runtimeSelectedId='a';
  editor.renderRuntimeDetail({job_id:'a',name:'Example',active:true,phase:'waiting',iteration:0,
    remaining_ms:1200,conditions:[],steps:[
      {state:'completed',params:{type:'kbd',cmd_type:'TYPE',text:'<full secret>',delay_ms:250},result:null},
      {state:'active',params:{type:'evt',evt:'HELLO'},result:null},
    ]});
  const markup=editor.elements['runtime-content'].innerHTML;
  assert.ok(markup.includes('&lt;full secret&gt;'));
  assert.ok(markup.includes('250ms'));
  assert.ok(markup.includes('data-runtime-state="active"'));
  assert.ok(!/<input|<select|<button|draggable=/.test(markup));
  assert.equal(JSON.stringify(editor.steps),draft);
});

test('selecting another runtime job clears the old job content immediately',()=>{
  const editor=createEditor();
  editor.setRuntimeSnapshot({session_id:'s',revision:1,active:{job_id:'a',phase:'waiting',total:1},queued:[{job_id:'b',phase:'queued',total:1}]});
  editor.renderRuntimeDetail({job_id:'a',active:true,phase:'waiting',steps:[{state:'active',params:{type:'kbd',cmd_type:'TYPE',text:'OLD-A-CONTENT'}}],conditions:[]});
  editor.selectRuntimeJob('b');
  assert.ok(!editor.elements['runtime-content'].innerHTML.includes('OLD-A-CONTENT'));
  assert.ok(editor.elements['runtime-meta'].textContent.includes('queued'));
});

test('late runtime status cannot revive a retired server session',async()=>{
  const editor=createEditor();
  editor.setRuntimeSnapshot({session_id:'old',revision:10,active:{job_id:'a',phase:'waiting',total:1},queued:[]});
  let respond;
  editor.fetch=async()=>({json:()=>new Promise(resolve=>{respond=resolve;})});
  vm.runInContext(html.slice(html.indexOf('async function refreshScriptStatus()'),html.indexOf('function classifyKbm(')),editor);
  const pending=editor.refreshScriptStatus();
  await new Promise(resolve=>setImmediate(resolve));
  editor.setRuntimeSnapshot({session_id:'new',revision:1,active:{job_id:'b',phase:'waiting',total:1},queued:[]});
  respond({runtime:{session_id:'old',revision:10,active:{job_id:'a',phase:'waiting',total:1},queued:[]},running:true,queued:0,paused:false});
  await pending;
  assert.equal(editor.runtimeState.session_id,'new');
  editor.setRuntimeSnapshot({session_id:'old',revision:99,active:{job_id:'a',phase:'waiting',total:1},queued:[]});
  assert.equal(editor.runtimeState.session_id,'new');
});

test('manual PAUSE uses its endpoint without resubmitting a script and renders no null event',async()=>{
  const editor=createEditor();
  editor.addLog=()=>{};editor.refreshScriptStatus=async()=>{};
  const requests=[];
  editor.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({ok:true})};};
  await editor.pauseScript();
  assert.deepEqual(requests.map(request=>request.url),['/script/pause']);
  editor.setScriptPause({pause_id:'manual',reason:'manual',evt:null,step:1,total:3});
  assert.equal(editor.elements['script-pause-btn'].disabled,true);
  assert.equal(editor.elements['runtime-pause-btn'].disabled,true);
  assert.ok(!editor.resetCalls.some(call=>Array.isArray(call)&&call[0]==='message'));
});

test('execution status uses Edit columns and badges with readonly values and one current detail row',()=>{
  const editor=createEditor();
  editor.runtimeSelectedId='style-job';
  editor.renderRuntimeDetail({job_id:'style-job',active:true,phase:'waiting',current_step:1,iteration:0,conditions:[
    {condition_id:'current',name:'Current condition',roi:[0.2,0.3,0.2,0.1],threshold:0.92,min_hits:3,consecutive_hits:1,last_score:0.8,enabled:true},
    {condition_id:'later',name:'Later condition',roi:[0,0,1,1],threshold:0.92,min_hits:3},
  ],steps:[
    {state:'completed',params:{type:'srv',servo_id:2,angle:20,delay_ms:250},result:{value:'HIDDEN_OLD_RESULT'}},
    {state:'active',params:{type:'evt',evt:'IMG.MATCH.current',delay_ms:0},result:{value:'CURRENT_RESULT'}},
    {state:'pending',params:{type:'kbd',cmd_type:'TYPE',text:'Visible <TYPE> & unmasked',delay_ms:1000}},
    {state:'pending',params:{type:'mse',action:'DBLCLICK',btn:'R',delay_ms:100}},
  ]});

  const markup=editor.elements['runtime-content'].innerHTML;
  assert.ok(markup.includes('class="stbl runtime-steps"'));
  assert.ok(markup.includes('>TYPE</th>')&&markup.includes('>DELAY</th>')&&markup.includes('>PARAMS</th>'));
  for(const badge of ['b-srv','b-img','b-kbd','b-mse'])assert.ok(markup.includes(badge));
  assert.ok(markup.includes('runtime-value'));
  assert.ok(markup.includes('Visible &lt;TYPE&gt; &amp; unmasked'));
  assert.equal((markup.match(/data-runtime-detail=/g)||[]).length,1);
  assert.ok(markup.includes('Current condition')&&markup.includes('CURRENT_RESULT'));
  assert.ok(markup.includes('ARM | score 0.800 / 0.92 | hits 1 / 3 | ROI 0.20, 0.30, 0.20, 0.10'));
  assert.ok(!markup.includes('<br>ROI'));
  const conditionMarkup=markup.match(/<div class="runtime-condition">[\s\S]*?<\/div>/)?.[0]||'';
  assert.ok(!conditionMarkup.includes('class="cdim">current</span>'));
  assert.ok(!markup.includes('Later condition')&&!markup.includes('HIDDEN_OLD_RESULT'));
  assert.equal(editor.elements['runtime-prog-lbl'].textContent,'1 / 4');
  assert.equal(editor.elements['runtime-prog-fi'].style.width,'25%');
  assert.ok(editor.elements['runtime-prog-step'].textContent.includes('Step 2 / 4'));
  assert.ok(!/<input|<select|<button|draggable=/.test(markup));
  const selector=html.match(/<select[^>]*id="runtime-jobs"[^>]*>/)[0];
  assert.ok(!selector.includes('size='));
  editor.resetRuntimeView();
  assert.equal(editor.elements['runtime-prog-lbl'].textContent,'0 / 0');
  assert.equal(editor.elements['runtime-prog-fi'].style.width,'0%');
});

test('execution status formats active elapsed time as HH:MM:SS without wrapping hours',()=>{
  const editor=createEditor();
  assert.equal(editor.formatElapsed(0,90061),'25:01:01');
  assert.equal(editor.formatElapsed(null,90061),null);
  editor.runtimeSelectedId='elapsed-job';
  editor.renderRuntimeDetail({job_id:'elapsed-job',name:'Example',active:true,phase:'waiting',
    started_at:Date.now()/1000-65,iteration:0,conditions:[],steps:[]});
  assert.match(editor.elements['runtime-meta'].textContent,/elapsed \d{2}:\d{2}:\d{2}/);
});