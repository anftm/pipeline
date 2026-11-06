"use strict";
// Exercise the real complete client in a fresh realm, without a DOM or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/tts-client.js"), "utf8");
const sandbox = {};
vm.runInNewContext(source, sandbox);
const parse = sandbox.parseSpeechResponse;
const id = "a".repeat(32);
const part = {index: 0, start: 0, end: 3, bytes: 100, audio_url: `/tts/audio/${id}/parts/0.mp3`,
  pitch_semitones: 0, pause_kind: "comma", pause_after_seconds: 0.6};
const normal = {id, state: "ready", progress: {total_parts: 1, parts: [part]}, result: {pitch_semitones: 0}};
const snapshot = value => JSON.parse(JSON.stringify(value));
assert.deepEqual(snapshot(parse({id, state: "queued"})), {id, state: "queued"});
assert.deepEqual(snapshot(parse(normal)), normal);
assert.equal(parse({...normal, first_audio: {encoding: "base64"}}).first_audio.encoding, "base64");
for (const invalid of [null, [], {}, {id: 42, state: "queued"}, {id, state: null},
  {id, state: "running", progress: []}, {id, state: "running", progress: {total_parts: 1, parts: [null]}},
  {id, state: "running", progress: {total_parts: 1, parts: [{...part, bytes: -1}]}},
  {id, state: "running", progress: {total_parts: 1, parts: [{...part, audio_url: null}]}},
  {id, state: "ready", result: {pitch_semitones: "0"}},
  {id, state: "running", progress: {total_parts: 65, parts: Array(65).fill(part)}}]) {
  assert.throws(() => parse(invalid));
}
assert.equal(parse({...normal, progress: {total_parts: 1, parts: [{...part, pause_kind: "future"}]}}).progress.parts[0].pause_kind, undefined);
console.log("speech response validates queued, ready and malformed payloads");
