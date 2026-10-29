'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {isAllowedApiRequest} = require('../sidecar.cjs');
const PROFILE = '0123456789abcdef'.repeat(2);

test('desktop provider and discovery controls admit only their explicit methods', () => {
  const reads = [
    '/providers/models', '/providers/models?source=openrouter&refresh=1',
    '/providers/usage', '/providers/quota?refresh=1', '/providers/profiles',
    '/inference/discovery',
  ];
  const writes = [
    '/providers/discover', '/providers/profiles/save',
    `/providers/profiles/${PROFILE}/select`, `/providers/profiles/${PROFILE}/delete`,
    '/inference/discover', '/inference/discovery/cancel', '/inference/discovery/select',
  ];
  for (const route of reads) {
    assert.equal(isAllowedApiRequest(route, 'GET'), true, route);
    assert.equal(isAllowedApiRequest(route, 'POST'), false, route);
  }
  for (const route of writes) {
    assert.equal(isAllowedApiRequest(route, 'POST'), true, route);
    assert.equal(isAllowedApiRequest(route, 'GET'), false, route);
  }
  assert.equal(isAllowedApiRequest('/engagements/defaults', 'GET'), true);
  assert.equal(isAllowedApiRequest('/engagements/defaults', 'POST'), true);
});

test('new bridge routes cannot expand into arbitrary profiles, paths or engine actions', () => {
  const blocked = [
    '/providers', '/providers/profiles/delete', '/providers/profiles/select',
    `/providers/profiles/${PROFILE}`, `/providers/profiles/${PROFILE}/execute`,
    `/providers/profiles/${'A'.repeat(32)}/select`,
    `/providers/profiles/${'g'.repeat(32)}/delete`,
    `/providers/profiles/${'a'.repeat(31)}/select`,
    `/providers/profiles/${'a'.repeat(33)}/delete`,
    '/providers/profiles/../settings', '/providers/%2e%2e/settings',
    '/inference/discovery/execute', '/inference/discovery/../start',
    '/inference/discovery/%2e%2e/start', '/inference/discovery/select/extra',
    '/engagements/defaults/../../settings', '/engagements/defaults/delete',
    '//other.example/providers/models', 'https://other.example/providers/models',
    '/providers/models#fragment', '/providers/models\\extra',
    '/providers/models\r\nAuthorization: secret', '/shutdown',
    '/integrations/mcp/call', '/harness/activate', '/language/execute',
  ];
  for (const route of blocked) {
    for (const method of ['GET', 'POST', 'DELETE', 'PUT', 'PATCH']) {
      assert.equal(isAllowedApiRequest(route, method), false, `${method} ${route}`);
    }
  }
  for (const route of ['/providers/models', '/providers/profiles/save', '/engagements/defaults']) {
    for (const method of ['DELETE', 'PUT', 'PATCH', 'OPTIONS', 'get', 'post']) {
      assert.equal(isAllowedApiRequest(route, method), false, `${method} ${route}`);
    }
  }
});
