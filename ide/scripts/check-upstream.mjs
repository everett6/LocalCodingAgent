// Run in CI's disposable checkout. Never mutates ide/upstream or a local editor install.
import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const pinPath = new URL('../source/upstream.json', import.meta.url);
const headers = { Accept: 'application/vnd.github+json', ...(process.env.GITHUB_TOKEN ? { Authorization: `Bearer ${process.env.GITHUB_TOKEN}` } : {}) };
async function json(url) { const response = await fetch(url, { headers, signal: AbortSignal.timeout(30000) }); if (!response.ok) throw new Error(`Upstream API: HTTP ${response.status}`); return response.json(); }
const release = await json('https://api.github.com/repos/microsoft/vscode/releases/latest');
if (release.draft || release.prerelease || !/^\d+\.\d+\.\d+$/.test(release.tag_name)) throw new Error('Expected an official stable semantic version');
let object = (await json(`https://api.github.com/repos/microsoft/vscode/git/ref/tags/${release.tag_name}`)).object;
for (let count = 0; object.type === 'tag' && count < 5; count++) object = (await json(object.url)).object;
if (object.type !== 'commit' || !/^[a-f0-9]{40}$/.test(object.sha)) throw new Error('Stable tag did not resolve to an immutable commit');
const response = await fetch(`https://raw.githubusercontent.com/microsoft/vscode/${object.sha}/.nvmrc`, { signal: AbortSignal.timeout(30000) });
if (!response.ok) throw new Error(`Node pin: HTTP ${response.status}`);
const node = (await response.text()).trim();
if (!/^\d+\.\d+\.\d+$/.test(node)) throw new Error('Unexpected upstream Node version');
const previous = JSON.parse(await fs.readFile(pinPath, 'utf8'));
const changed = previous.commit !== object.sha;
if (changed) await fs.writeFile(pinPath, JSON.stringify({ ...previous, tag: release.tag_name, commit: object.sha, releaseUrl: release.html_url, releasePublished: release.published_at, node }, null, 2) + '\n');
if (process.env.GITHUB_OUTPUT) await fs.appendFile(process.env.GITHUB_OUTPUT, `changed=${changed}\ntag=${release.tag_name}\nnode=${node}\n`);
console.log(changed ? `Prepared fork baseline ${release.tag_name} at ${object.sha}; validation is required before merge.` : `Already pinned to stable ${release.tag_name}.`);
