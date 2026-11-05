const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const sketchPath = path.join(__dirname, '../arduino/pro_micro_hid/pro_micro_hid.ino');
const source = fs.readFileSync(sketchPath, 'utf8');
const legacyPath = path.join(__dirname, '../arduino/pro_micro/pro_micro.ino');
const legacySource = fs.readFileSync(legacyPath, 'utf8');

test('USB JSON and Serial1 command paths remain separate', () => {
  assert.match(source, /deserializeJson\s*\(/);
  assert.match(source, /handleUsbLine\s*\(/);
  assert.match(source, /processCommand\s*\(/);
  assert.match(source, /Serial1\.println\s*\(ok\s*\?/);
});

test('the JSON action runner covers the attached keyboard and mouse schema', () => {
  for (const action of [
    'text', 'key', 'combo', 'delay', 'mouse_move', 'mouse_scroll',
    'mouse_click', 'mouse_press', 'mouse_release',
  ]) {
    assert.ok(source.includes(`"${action}"`), `missing action type ${action}`);
  }
});

test('EVT lines still forward to the controller and USB replies stay protocol-only', () => {
  assert.match(source, /strncmp\s*\(.*"EVT:"/s);
  assert.match(source, /forwardEventToController\s*\(/);
  assert.doesNotMatch(source, /Serial\.print(?:ln)?\s*\(\s*F?\("\[(?:DBG|OK|ERR)/);
});

test('the sketch bounds JSON line size and action count for the Pro Micro', () => {
  assert.match(source, /USB_LINE_CAPACITY\s+768/);
  assert.match(source, /MAX_JSON_ACTIONS\s+16/);
});

test('both Pro Micro sketches allow DOWN and UP for mapped non-modifier keys', () => {
  for (const sketch of [legacySource, source]) {
    assert.ok(/strncmp\s*\(raw,\s*"DOWN:",\s*5\)[\s\S]{0,120}(findHoldKey|resolveKey)\(/.test(sketch));
    assert.ok(/strncmp\s*\(raw,\s*"UP:",\s*3\)[\s\S]{0,120}(findHoldKey|resolveKey)\(/.test(sketch));
  }
  assert.ok(/findHoldKey[\s\S]{0,180}findKey/.test(legacySource));
  assert.ok(/resolveKey[\s\S]{0,180}KEY_MAP/.test(source));
  assert.ok(/\{"ESC",\s*KEY_ESC\}/.test(legacySource));
  assert.ok(/\{"ESC",\s*KEY_ESC\}/.test(source));
});