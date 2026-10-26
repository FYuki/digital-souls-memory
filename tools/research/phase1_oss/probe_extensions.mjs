// Native pure modules only. No browser, network, model, or SillyTavern runtime.
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import vm from 'node:vm';

const root = process.env.OSS_PROBE_ROOT || '/tmp/phase1-oss-reassessment';
const observations = [];
function check(candidate, name, fn, phase1) {
  fn();
  observations.push({ candidate, test: name, execution: 'PASS', phase1 });
}
const nativeImport = (repo, file) => import(pathToFileURL(path.join(root, repo, file)).href);

const smart = await nativeImport('smart-memory', 'parsers.js');
const parsed = smart.parseExtractionOutput('[fact:2:permanent] Aoi calls the planet Blue Star.');
check('smart-memory', 'parser_has_provenance_and_supersession_fields', () => {
  assert.equal(parsed.length, 1);
  for (const key of ['id', 'source_messages', 'valid_from', 'valid_to', 'supersedes', 'superseded_by']) {
    assert.ok(Object.hasOwn(parsed[0], key), key);
  }
}, 'PARTIAL: graph/provenance fields exist; pipeline must supply valid source references');
check('smart-memory', 'parser_does_not_invent_source_ids', () => {
  assert.deepEqual(parsed[0].source_messages, []);
}, 'MATCH at parser layer only; source assignment reviewed separately in longterm.js');
const smartUtils = await nativeImport('smart-memory', 'memory-utils.js');
const baseFact = { id: 'mention-1', type: 'fact', content: 'Aoi likes tea', ts: 1, entities: [] };
const newMention = { ...baseFact, id: 'mention-2', ts: 2 };
const unified = await smartUtils.reconcileTypeEntries([baseFact], [newMention], 0.7);
check('smart-memory', 'reconciliation_treats_repeated_fact_as_one', () => {
  assert.equal(unified.length, 1);
}, 'MATCH mechanism: native reconciliation collapses same fact into one entry');
const distinct = await smartUtils.reconcileTypeEntries([baseFact], [
  { ...newMention, content: 'Kuro lives in Kyoto' },
], 0.7);
check('smart-memory', 'reconciliation_keeps_distinct_fact', () => {
  assert.equal(distinct.length, 2);
}, 'MATCH control: unrelated statements are not collapsed by the deterministic fixture');

const char = await nativeImport('charmemory', 'lib.js');
const blocks = [{ chat: 'synthetic-chat', date: '2026-10-26 00:00', bullets: ['アオは「青い星」と呼ぶ'],
  source_turn_ids: ['turn-a'], perceived_by: ['aoi'] }];
const roundtrip = char.parseMemories(char.serializeMemories(blocks));
check('charmemory', 'memory_bullet_and_chat_roundtrip', () => {
  assert.equal(roundtrip[0].bullets[0], blocks[0].bullets[0]);
  assert.equal(roundtrip[0].chat, blocks[0].chat);
}, 'MATCH: character/chat memory text persists');
check('charmemory', 'turn_level_source_metadata_not_in_native_format', () => {
  assert.equal(roundtrip[0].source_turn_ids, undefined);
  assert.equal(roundtrip[0].perceived_by, undefined);
}, 'GAP: native serializer preserves chat/date/bullets, not Turn-level provenance');
const charConsolidation = await nativeImport('charmemory', 'consolidation.js');
let consolidationCalls = 0;
const consolidated = await charConsolidation.runChunkedConsolidation([
  { chat: 'chat-1', date: '2026-10-26', bullets: ['Aoi likes tea'] },
  { chat: 'chat-2', date: '2026-10-26', bullets: ['Tea is a favorite of Aoi'] },
], {
  runLLM: async () => {
    consolidationCalls += 1;
    return char.serializeMemories([{ chat: 'combined', date: '2026-10-26', bullets: ['Aoi likes tea'] }]);
  },
  logProgress: () => {}, isCancelled: () => false,
  packChunks: memories => [memories], parseOutput: char.parseMemories,
});
check('charmemory', 'native_consolidation_accepts_one_fact_from_multiple_mentions', () => {
  assert.equal(consolidationCalls, 2);
  assert.equal(char.parseMemories(consolidated).flatMap(b => b.bullets).length, 1);
}, 'PARTIAL: native map/reduce consolidation; merge output is a model fixture, not semantic quality proof');

const letta = await nativeImport('letta-code', 'src/memory-frontmatter.ts');
const locked = '---\ndescription: Synthetic identity\nread_only: true\n---\nAoi\n';
check('letta-code', 'legacy_read_only_identity_rejects_agent_edit', () => {
  const errors = letta.validateMemoryFileFrontmatter({ path: 'identity.md', content: locked.replace('Aoi', 'Other'),
    previousContent: locked, format: 'legacy' });
  assert.ok(errors.some(e => e.includes('read_only')));
}, 'PARTIAL: an explicit identity protection mechanism exists; caller must use it');
check('letta-code', 'agent_cannot_remove_legacy_read_only_flag', () => {
  const errors = letta.validateMemoryFileFrontmatter({ path: 'identity.md', content: locked.replace('read_only: true\n', ''),
    previousContent: locked, format: 'legacy' });
  assert.ok(errors.length > 0);
}, 'MATCH at legacy content-validator layer; full runtime enforcement not tested');

// Execute OpenVault's unmodified parser in a module harness. Only utility ID/log
// dependencies are stubs; parser/provenance/witness logic is the native source.
const context = vm.createContext({ console, Date, Math, Set, Map });
const source = await readFile(path.join(root, 'openvault/src/extraction/parser.js'), 'utf8');
const module = new vm.SourceTextModule(source, { context });
await module.link(async (specifier) => {
  if (specifier.endsWith('/utils.js')) {
    return new vm.SyntheticModule(['generateId', 'log'], function () {
      this.setExport('generateId', () => 'synthetic-id');
      this.setExport('log', () => {});
    }, { context });
  }
  if (specifier.endsWith('/constants.js')) {
    return new vm.SyntheticModule(['CHARACTERS_KEY', 'RELATIONSHIPS_KEY'], function () {
      this.setExport('CHARACTERS_KEY', 'characters');
      this.setExport('RELATIONSHIPS_KEY', 'relationships');
    }, { context });
  }
  throw new Error(`Unexpected runtime import: ${specifier}`);
});
await module.evaluate();
const event = module.namespace.parseExtractionResult(JSON.stringify({ summary: 'Aoi likes tea', witnesses: ['Aoi'] }),
  [{ id: 3, mes: 'tea' }, { id: 5, mes: 'unrelated' }], 'Aoi', 'User', 'synthetic-batch')[0];
check('openvault-unkarelian', 'source_message_ids_and_witnesses_are_retained', () => {
  assert.deepEqual(Array.from(event.message_ids), [3, 5]);
  assert.deepEqual(Array.from(event.witnesses), ['Aoi']);
}, 'PARTIAL: provenance/witness fields exist; references cover the whole extraction batch');
check('openvault-unkarelian', 'source_assignment_is_batch_not_claim_level', () => {
  assert.equal(event.message_ids.length, 2);
  assert.equal(event.batch_id, 'synthetic-batch');
}, 'GAP: does not itself prove which individual Turn supports the claim');
const repeatedEvents = module.namespace.parseExtractionResult(JSON.stringify([
  { summary: 'Aoi likes tea' }, { summary: 'Aoi likes tea' },
]), [{ id: 3 }], 'Aoi', 'User', 'synthetic-batch');
check('openvault-unkarelian', 'parser_keeps_duplicate_extractor_outputs', () => {
  assert.equal(repeatedEvents.length, 2);
}, 'ALLOWED at ingest; no separate native fact-consolidation mechanism was found in this candidate');

const result = { models: 'none', scope: 'native pure-module behavior; full UI/runtime NOT RUN', observations };
await writeFile(path.join(root, 'extensions-results.json'), JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify(result, null, 2));
