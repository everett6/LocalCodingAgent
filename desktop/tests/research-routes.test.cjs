const test = require('node:test');
const assert = require('node:assert/strict');
const { isAllowedApiRequest } = require('../sidecar.cjs');
const { isAllowedEngineRequest } = require('../../ide/extension/engine.cjs');

for (const [name, allowed] of [['manager', isAllowedApiRequest], ['IDE', isAllowedEngineRequest]]) {
  test(`${name} research bridge keeps explicit methods and blocks arbitrary operations`, () => {
    for (const route of ['/research/capabilities', '/research/evidence', '/research/report?format=sarif', '/research/dependencies?path=src', '/research/advisories', '/memory/structure?path=src']) {
      assert.equal(allowed(route, 'GET'), true);
      assert.equal(allowed(route, 'POST'), false);
    }
    for (const route of ['/research/audit', '/research/sarif/import', '/research/advisories/import']) {
      assert.equal(allowed(route, 'POST'), true);
      assert.equal(allowed(route, 'GET'), false);
    }
    for (const route of ['/research/execute', '/research/scan-network', '//research/audit', 'https://example.invalid/research/audit']) {
      assert.equal(allowed(route, 'POST'), false);
    }
  });
}
