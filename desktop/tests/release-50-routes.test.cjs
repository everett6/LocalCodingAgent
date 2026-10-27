const test = require('node:test');
const assert = require('node:assert/strict');
const { isAllowedApiRequest } = require('../sidecar.cjs');
const { isAllowedEngineRequest } = require('../../ide/extension/engine.cjs');
for (const [name, allow] of [['manager', isAllowedApiRequest], ['IDE', isAllowedEngineRequest]]) {
  test(`${name} summary and search doctor allow exact methods and paths`, () => {
    for (const route of ['/status', '/status?summary=1', '/jobs/job_123', '/context/debugger']) assert.equal(allow(route, 'GET'), true, route);
    for (const method of ['GET', 'POST']) assert.equal(allow('/integrations/search/doctor', method), true, method);
    for (const route of ['/integrations/search/doctor/execute', '/integrations/search/../doctor', '/integrations/search/doctor?execute=1', '/status?summary=2', '/status?summary=1&execute=1']) {
      for (const method of ['GET', 'POST']) assert.equal(allow(route, method), false, `${method} ${route}`);
    }
    assert.equal(allow('/context/debugger', 'POST'), false);
    assert.equal(allow('/evaluations/heldout/run', 'POST'), true);
    assert.equal(allow('/evaluations/heldout/run', 'GET'), false);
    for (const method of ['DELETE', 'PUT']) assert.equal(allow('/integrations/search/doctor', method), false, method);
  });
}
