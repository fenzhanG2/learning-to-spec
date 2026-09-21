import assert from 'node:assert/strict';
import test from 'node:test';
import { availableSessionModels, currentSessionModel } from '../extensions/learning-to-spec/model-selection.mjs';

test('retry model list uses only selectable host entries, never pricing metadata or fixed models', async () => {
  const model = { count: 0, async list() { this.count += 1; return { list: [{ id: 'synthetic-one' }, { id: 'synthetic-two' }, 'auto', { id: '--bad' }, { id: 'synthetic-one' }], modelPriceCategories: [{ id: 'not-selectable' }] }; } };
  assert.deepEqual(await availableSessionModels({ rpc: { model } }), ['synthetic-one', 'synthetic-two', 'auto']);
  assert.equal(model.count, 1);
  assert.deepEqual(await availableSessionModels({}), []);
  assert.deepEqual(await availableSessionModels({ rpc: { model: { list: () => new Promise(() => {}) } } }, { timeoutMs: 5 }), []);
  assert.deepEqual(await availableSessionModels({ rpc: { model: { list() { throw new Error('PRIVATE'); } } } }), []);
});

test('host model getter preserves receiver and exact selection without switching', async () => {
  const model = { modelId: 'synthetic/model:v2', reads: 0,
    async getCurrent() { this.reads += 1; return { modelId: this.modelId, reasoningEffort: 'high' }; },
    async switchTo() { assert.fail('Read-only lookup'); },
  };
  assert.equal(await currentSessionModel({ rpc: { model } }), model.modelId);
  assert.equal(model.reads, 1);
  model.modelId = 'auto';
  assert.equal(await currentSessionModel({ rpc: { model } }), 'auto');
  assert.equal(model.reads, 2);
});

test('missing SDK and invalid model identifiers are optional, not guessed', async () => {
  for (const session of [null, {}, { rpc: {} }, { rpc: { model: {} } }]) {
    assert.equal(await currentSessionModel(session), null);
  }
  for (const modelId of [undefined, null, '', 42, {}, '--model', 'two words', 'name\n', 'a'.repeat(161)]) {
    assert.equal(await currentSessionModel({ rpc: { model: { getCurrent: async () => ({ modelId }) } } }), null);
  }
});

test('SDK failures are bounded and private', async () => {
  assert.equal(await currentSessionModel({ rpc: { model: { getCurrent() { throw new Error('PRIVATE_SDK_ERROR'); } } } }), null);
  assert.equal(await currentSessionModel({ rpc: { model: { getCurrent: () => new Promise(() => {}) } } }, { timeoutMs: 5 }), null);
  let rejectLate;
  assert.equal(await currentSessionModel({ rpc: { model: { getCurrent: () => new Promise((resolve, reject) => { rejectLate = reject; }) } } }, { timeoutMs: 5 }), null);
  rejectLate(new Error('PRIVATE_LATE_ERROR'));
  await new Promise(resolve => setImmediate(resolve));
});
