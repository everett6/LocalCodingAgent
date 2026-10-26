const test = require('node:test');
const assert = require('node:assert/strict');
const { isAllowedApiRequest } = require('../sidecar.cjs');
const { isAllowedEngineRequest } = require('../../ide/extension/engine.cjs');

for (const [name, allowed] of [['manager', isAllowedApiRequest], ['IDE', isAllowedEngineRequest]]) {
  test(`${name} reliability controls expose explicit routes and methods only`, () => {
    const id = 'a'.repeat(32), finding = 'b'.repeat(24);
    for (const route of ['/evaluations', `/evaluations/${id}`, `/evaluations/jobs/${id}`, '/findings', `/findings/${finding}`, `/findings/jobs/${id}`, '/memory/retrievals', `/memory/retrievals/${id}`, '/context/accounting', '/research/advisories/updates', '/inference/tuning/history', `/inference/tuning/jobs/${id}`, '/doctor']) {
      assert.equal(allowed(route, 'GET'), true, route);
      assert.equal(allowed(route, 'POST'), false, route);
    }
    for (const route of ['/evaluations/run', '/evaluations/model/run', `/evaluations/jobs/${id}/cancel`, `/findings/${finding}/review`, `/findings/${finding}/verify`, `/findings/jobs/${id}/cancel`, '/context/tokenizer', '/research/advisories/update', '/inference/tuning/start', '/inference/tuning/apply', `/inference/tuning/jobs/${id}/cancel`, '/doctor/repair']) {
      assert.equal(allowed(route, 'POST'), true, route);
      assert.equal(allowed(route, 'GET'), false, route);
    }
    for (const route of ['/doctor/execute', '/doctor/repair-all', '/findings/unknown/verify', `/findings/${id}/review`, '/evaluations/../doctor', '/evaluations/%2e%2e/doctor', '//doctor', '/inference/tuning/jobs/not-an-id/cancel']) {
      assert.equal(allowed(route, 'POST'), false, route);
      assert.equal(allowed(route, 'GET'), false, route);
    }
  });
}
