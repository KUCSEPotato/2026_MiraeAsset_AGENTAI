const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

function setup(fetch) {
  const context = vm.createContext({
    document: { querySelector: () => ({}), querySelectorAll: () => [] },
    window: { addEventListener() {} },
    location: { pathname: '/' },
    URLSearchParams, AbortController, fetch,
  });
  vm.runInContext(readFileSync('frontend/assets/app.js', 'utf8'), context);
  vm.runInContext('render = () => {}; renderChat = () => {};', context);
  return { run: code => vm.runInContext(code, context) };
}

test('sends Korean questions to the same-origin API and renders escaped answers', async () => {
  const app = setup(async url => {
    const parsed = new URL(url, 'http://localhost');
    assert.equal(parsed.pathname, '/answer');
    assert.equal(parsed.searchParams.get('question'), '국내 ETF?');
    assert.match(parsed.searchParams.get('question_id'), /^finory-/);
    return { ok: true, json: async () => ({ answer: '<script>위험</script>\n답변' }) };
  });
  await app.run('askFinory("국내 ETF?")');
  assert.match(app.run('messageTemplate(messages.at(-1))'), /&lt;script&gt;/);
  assert.equal(app.run('activeRequest'), null);
});

test('prevents duplicate requests and ignores an answer after a new chat', async () => {
  let resolve;
  let calls = 0;
  const app = setup(() => { calls++; return new Promise(done => { resolve = done; }); });
  const first = app.run('askFinory("첫 질문")');
  await app.run('askFinory("중복 질문")');
  assert.equal(calls, 1);
  app.run('activeRequest.abort(); activeRequest = null; messages = messages.slice(0, 1);');
  resolve({ ok: true, json: async () => ({ answer: '늦은 응답' }) });
  await first;
  assert.equal(app.run('messages.length'), 1);
  assert.match(app.run('messages[0].content'), /안녕하세요/);
});

test('reports API failures and permits retry', async () => {
  const app = setup(async () => ({ ok: false, status: 504 }));
  await app.run('askFinory("질문")');
  assert.match(app.run('messages.at(-1).content'), /답변 시간이/);
  assert.equal(app.run('activeRequest'), null);
});
