import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import {
  normalizeBrief, normalizeSourceFiles, websiteFiles, auditFiles,
  buildArtifacts, sha256, zipStored, readStoredZip,
} from '../worker/artifacts.mjs';

const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });
const maxBytes = 2 * 1024 * 1024;
const brief = {
  business_name: 'River & Pine Plumbing',
  description: 'Owner supplied plumbing services for homes and local businesses.',
  contact_email: 'hello@example.com',
  industry: 'Plumbing', location: 'Portland, Oregon', phone: '+1 (503) 555-0100',
  site_url: 'https://example.com', repository_url: 'https://github.com/example/site',
  services: [{ name: 'Repairs', description: 'Repair appointments for plumbing fixtures.' }],
};
const minimalPage = (extra = '') => `<!doctype html><html lang="en"><head>
<title>Useful page</title><meta name="description" content="A factual page description.">
<link rel="canonical" href="https://example.com/">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>
</head><body><main><h1>A useful page</h1><p>${'Useful factual content. '.repeat(15)}</p>${extra}</main></body></html>`;
const digest = value => createHash('sha256').update(value).digest('hex');
const finding = (report, check, page = 'index.html') => {
  const row = report.findings.find(item => item.page === page && item.check === check);
  assert.ok(row, `Missing finding ${page}/${check}`);
  return row;
};
const validationError = error => error?.name === 'ValidationError';

// These fixtures and the ZIP inspector use no implementation helpers. They
// independently exercise ZIP headers and payload CRC rather than round-trip only.
function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function zipFixture(entries) {
  const locals = [], central = [];
  let offset = 0;
  for (const [filename, value] of entries) {
    const name = Buffer.from(filename);
    const data = typeof value === 'string' ? Buffer.from(value) : Buffer.from(value);
    const checksum = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6); local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(data.length, 18); local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(name.length, 26);
    const directory = Buffer.alloc(46);
    directory.writeUInt32LE(0x02014b50, 0); directory.writeUInt16LE(20, 4);
    directory.writeUInt16LE(20, 6); directory.writeUInt16LE(0x0800, 8);
    directory.writeUInt32LE(checksum, 16); directory.writeUInt32LE(data.length, 20);
    directory.writeUInt32LE(data.length, 24); directory.writeUInt16LE(name.length, 28);
    directory.writeUInt32LE(0x81a40000, 38); // Unix ordinary file, mode 0644.
    directory.writeUInt32LE(offset, 42);
    locals.push(local, name, data); central.push(directory, name);
    offset += local.length + name.length + data.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10); end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return new Uint8Array(Buffer.concat([...locals, directory, end]));
}

function inspectZip(bytes) {
  const data = Buffer.from(bytes);
  assert.equal(data.readUInt32LE(data.length - 22), 0x06054b50, 'EOCD missing');
  const end = data.length - 22;
  const count = data.readUInt16LE(end + 10);
  assert.equal(data.readUInt16LE(end + 8), count);
  const directoryStart = data.readUInt32LE(end + 16);
  assert.equal(directoryStart + data.readUInt32LE(end + 12), end);
  const files = {};
  let position = directoryStart;
  for (let i = 0; i < count; i++) {
    assert.equal(data.readUInt32LE(position), 0x02014b50);
    assert.equal(data.readUInt16LE(position + 10), 0, 'ZIP must use stored entries');
    const flags = data.readUInt16LE(position + 8);
    assert.equal(flags & 1, 0, 'ZIP must not encrypt entries');
    const size = data.readUInt32LE(position + 24);
    assert.equal(data.readUInt32LE(position + 20), size);
    const nameLength = data.readUInt16LE(position + 28);
    const filename = decoder.decode(data.subarray(position + 46, position + 46 + nameLength));
    const local = data.readUInt32LE(position + 42);
    assert.equal(data.readUInt32LE(local), 0x04034b50);
    assert.equal(data.readUInt16LE(local + 8), 0);
    assert.equal(data.readUInt16LE(local + 6), flags);
    assert.equal(data.readUInt32LE(local + 18), size);
    assert.equal(data.readUInt32LE(local + 22), size);
    const localNameLength = data.readUInt16LE(local + 26);
    assert.equal(decoder.decode(data.subarray(local + 30, local + 30 + localNameLength)), filename);
    const start = local + 30 + localNameLength + data.readUInt16LE(local + 28);
    const payload = data.subarray(start, start + size);
    assert.ok(start + size <= directoryStart);
    assert.equal(crc32(payload), data.readUInt32LE(local + 14));
    assert.equal(crc32(payload), data.readUInt32LE(position + 16));
    assert.ok(!Object.hasOwn(files, filename), 'ZIP filenames must be unique');
    files[filename] = decoder.decode(payload);
    position += 46 + nameLength + data.readUInt16LE(position + 30) + data.readUInt16LE(position + 32);
  }
  assert.equal(position, end);
  return files;
}

test('brief normalization preserves owner facts and supplies only documented defaults', () => {
  const normalized = normalizeBrief({ ...brief, business_name: `  ${brief.business_name}  ` });
  assert.equal(normalized.business_name, brief.business_name);
  assert.equal(normalized.brand_color, '#eab308');
  assert.deepEqual(normalized.services, brief.services);
  assert.notEqual(normalized.services, brief.services);
});

test('brief validation rejects unsupported facts, malformed contacts and injectable color', () => {
  for (const patch of [
    { business_name: '' }, { contact_email: 'javascript:alert(1)' },
    { brand_color: 'red;}' }, { services: [] }, { certifications: 'Invented' },
    { description: 'x'.repeat(4001) }, { business_name: 'bad\u0000name' },
    { description: '\ud800' }, { services: [{ name: 'Repair', description: 'OK', price: 20 }] },
  ]) assert.throws(() => normalizeBrief({ ...brief, ...patch }), validationError);
  assert.throws(() => normalizeBrief(null), validationError);
  assert.throws(() => normalizeBrief([]), validationError);
});

test('brief URLs require credential-free HTTPS and an actual GitHub repository', () => {
  for (const site_url of ['http://example.com', 'https://user:pass@example.com',
    'https://example.com/?token=secret', 'https://example.com/#fragment', 'https://example.com\\evil']) {
    assert.throws(() => normalizeBrief({ ...brief, site_url }), validationError);
  }
  for (const repository_url of ['https://example.com/owner/repo',
    'https://github.com/owner', 'https://github.com/owner/repo/issues',
    'https://github.com:443/owner/repo']) {
    assert.throws(() => normalizeBrief({ ...brief, repository_url }), validationError);
  }
});

test('website draft has linked reviewable pages and explicit preview indexing restrictions', async () => {
  const files = await websiteFiles(brief);
  assert.deepEqual(Object.keys(files).filter(name => name.endsWith('.html')).sort(),
    ['contact.html', 'index.html', 'services.html']);
  for (const name of ['styles.css', 'robots.txt', 'sitemap.xml', 'review.txt', 'manifest.json']) {
    assert.equal(typeof files[name], 'string', `${name} missing`);
  }
  for (const name of ['index.html', 'services.html', 'contact.html']) {
    assert.match(files[name], /<meta name="robots" content="noindex,follow">/);
    assert.equal((files[name].match(/<h1[\s>]/g) || []).length, 1);
    assert.match(files[name], /href="styles\.css"/);
  }
  assert.match(files['robots.txt'], /Disallow: \/\s*$/);
  assert.match(files['sitemap.xml'], /https:\/\/example\.com\/services\.html/);
  assert.match(files['review.txt'], /owner|supplied/i);
  assert.match(files['review.txt'], /noindex|indexing/i);
});

test('draft without final domain omits canonical and sitemap rather than inventing a URL', async () => {
  const files = await websiteFiles({ ...brief, site_url: '' });
  assert.ok(!Object.hasOwn(files, 'sitemap.xml'));
  assert.doesNotMatch(files['index.html'], /rel="canonical"/);
  const schema = JSON.parse(files['index.html'].match(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/)[1]);
  assert.ok(!Object.hasOwn(schema, 'url'));
  assert.ok(!Object.hasOwn(schema, '@id'));
});

test('renderer escapes HTML and JSON-LD cannot close its raw-text script', async () => {
  const attack = '</script><img src=x onerror="alert(1)">&';
  const facts = { ...brief, business_name: attack, description: attack,
    location: attack, services: [{ name: attack, description: attack }] };
  const files = await websiteFiles(facts);
  for (const name of ['index.html', 'services.html', 'contact.html']) {
    const html = files[name];
    assert.doesNotMatch(html, /<img src=x/);
    assert.match(html, /&lt;\/script&gt;/);
    const scripts = [...html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)];
    assert.equal(scripts.length, 1);
    assert.doesNotMatch(scripts[0][1], /[<>]/);
    const schema = JSON.parse(scripts[0][1]);
    assert.equal(schema.name, attack);
    assert.equal(schema.description, attack);
    assert.equal(schema.areaServed, attack);
    assert.equal(schema.hasOfferCatalog.itemListElement[0].itemOffered.name, attack);
    assert.equal((html.match(/<\/script>/g) || []).length, 1);
  }
});

test('manifest hashes cover every delivered file except the manifest itself', async () => {
  const files = await websiteFiles(brief);
  const manifest = JSON.parse(files['manifest.json']);
  assert.equal(manifest.schema_version, 1);
  assert.equal(manifest.preview_only, true);
  assert.equal(manifest.facts_source, 'owner_provided');
  assert.match(manifest.brief_sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual(Object.keys(manifest.files).sort(), Object.keys(files).filter(name => name !== 'manifest.json').sort());
  for (const [name, hash] of Object.entries(manifest.files)) assert.equal(hash, digest(files[name]), name);
});

test('SHA-256 matches independent known vectors and bytes', async () => {
  assert.equal(await sha256('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.equal(await sha256(encoder.encode('River 🌲')), digest('River 🌲'));
  await assert.rejects(async () => sha256({ bad: true }), validationError);
});

test('artifact generation returns content metadata matching independent digests', async () => {
  const artifacts = await buildArtifacts('website', brief);
  assert.deepEqual(artifacts.map(row => row.filename).sort(), ['website-checks.json', 'website-draft.zip']);
  for (const artifact of artifacts) {
    assert.ok(artifact.content instanceof Uint8Array);
    assert.equal(artifact.size_bytes, artifact.content.byteLength);
    assert.equal(artifact.sha256, digest(artifact.content));
  }
  const archive = artifacts.find(row => row.filename.endsWith('.zip'));
  assert.equal(archive.media_type, 'application/zip');
  const files = inspectZip(archive.content);
  assert.deepEqual(files, await websiteFiles(brief));
  const report = JSON.parse(decoder.decode(artifacts.find(row => row.filename.endsWith('.json')).content));
  assert.equal(report.summary.fail, 0);
  assert.equal(report.summary.unknown, 4);
});

test('stored ZIP is independently valid and reader agrees on UTF-8 members', async () => {
  const files = { 'index.html': minimalPage(), 'nested/notes.txt': 'Owner facts 🌲 & evidence' };
  const bytes = await zipStored(files);
  assert.deepEqual(inspectZip(bytes), files);
  assert.deepEqual(await readStoredZip(bytes), files);
  assert.deepEqual(await readStoredZip(zipFixture(Object.entries(files))), files);
});

test('ZIP reader rejects corruption, truncation, unsupported compression and encryption', async () => {
  const valid = zipFixture([['index.html', minimalPage()]]);
  const badCrc = valid.slice();
  badCrc[30 + Buffer.byteLength('index.html')] ^= 1;
  const compressed = valid.slice(); new DataView(compressed.buffer).setUint16(8, 8, true);
  const encrypted = valid.slice(); new DataView(encrypted.buffer).setUint16(6, 0x0801, true);
  for (const value of [badCrc, valid.slice(0, 15), valid.slice(0, -8), compressed, encrypted]) {
    await assert.rejects(async () => readStoredZip(value), validationError);
  }
});

test('ZIP reader rejects traversal, duplicate paths and invalid UTF-8', async () => {
  for (const entries of [
    [['../index.html', 'bad']],
    [['index.html', 'first'], ['index.html', 'second']],
    [['index.html', new Uint8Array([0xff])]],
  ]) await assert.rejects(async () => readStoredZip(zipFixture(entries)), validationError);
});

test('ZIP reader requires central directory to agree with local file records', async () => {
  const valid = zipFixture([['index.html', minimalPage()]]);
  const end = valid.length - 22;
  const directory = new DataView(valid.buffer).getUint32(end + 16, true);
  const badOffset = valid.slice(); new DataView(badOffset.buffer).setUint32(directory + 42, 1, true);
  const badDirectoryCrc = valid.slice(); new DataView(badDirectoryCrc.buffer).setUint32(directory + 16, 0, true);
  const symlink = valid.slice(); new DataView(symlink.buffer).setUint32(directory + 38, 0xa1ff0000, true);
  const badName = valid.slice(); badName[directory + 46] = 'x'.charCodeAt(0);
  const multiDisk = valid.slice(); new DataView(multiDisk.buffer).setUint16(end + 4, 1, true);
  for (const value of [badOffset, badDirectoryCrc, symlink, badName, multiDisk]) {
    await assert.rejects(async () => readStoredZip(value), validationError);
  }
});

test('ZIP reader applies file-count and expanded-byte limits', async () => {
  const tooMany = [['index.html', 'x'], ...Array.from({ length: 30 }, (_, index) => [`${index}.txt`, 'x'])];
  await assert.rejects(async () => readStoredZip(zipFixture(tooMany)), validationError);
  await assert.rejects(async () => readStoredZip(zipFixture([['index.html', 'x'.repeat(maxBytes + 1)]])), validationError);
});

test('source normalization accepts supported nested paths and valid Unicode without mutation', () => {
  const files = { 'index.html': minimalPage(), 'assets/site.css': 'body{}',
    'data/claims.json': '{}', 'notes.txt': '🌲', 'sitemap.xml': '<urlset/>' };
  assert.deepEqual(normalizeSourceFiles(files), files);
  assert.notEqual(normalizeSourceFiles(files), files);
});

test('source normalization rejects absolute, traversal, hidden and unsupported filenames', () => {
  for (const filename of ['/index.html', '../index.html', 'x/../index.html', './index.html',
    'x//index.html', 'x\\index.html', 'C:index.html', '.hidden/index.html', 'app.js', 'x\u0000.html']) {
    assert.throws(() => normalizeSourceFiles({ 'index.html': minimalPage(), [filename]: 'bad' }), validationError, filename);
  }
});

test('source normalization requires rendered HTML, text content and bounded UTF-8 byte count', () => {
  for (const files of [{}, [], { 'styles.css': 'body{}' }, { 'index.html': 1 },
    { 'index.html': '\ud800' }, { 'index.html': 'x'.repeat(maxBytes + 1) },
    { 'index.html': 'é'.repeat(maxBytes / 2 + 1) }]) {
    assert.throws(() => normalizeSourceFiles(files), validationError);
  }
  assert.equal(normalizeSourceFiles({ 'index.html': 'x'.repeat(maxBytes) })['index.html'].length, maxBytes);
  const thirty = { 'index.html': 'x', ...Object.fromEntries(Array.from({ length: 29 }, (_, i) => [`${i}.txt`, 'x'])) };
  assert.equal(Object.keys(normalizeSourceFiles(thirty)).length, 30);
  assert.throws(() => normalizeSourceFiles({ ...thirty, 'overflow.txt': 'x' }), validationError);
});

test('audit report has consistent counts, supplied hashes and exactly four unknown external outcomes', async () => {
  const files = { 'index.html': minimalPage(), 'notes.txt': 'owner facts' };
  const report = await auditFiles(files);
  assert.equal(report.schema_version, 1);
  assert.equal(report.scope, 'supplied_static_artifacts');
  assert.deepEqual(report.pages_checked, ['index.html']);
  assert.deepEqual(report.source_sha256, Object.fromEntries(Object.entries(files).map(([name, value]) => [name, digest(value)])));
  for (const status of ['pass', 'warning', 'fail', 'unknown']) {
    assert.equal(report.summary[status], report.findings.filter(row => row.status === status).length);
  }
  assert.deepEqual(report.findings.filter(row => row.status === 'unknown').map(row => row.check).sort(),
    ['ai_citations', 'live_crawl_and_indexing', 'qualified_leads', 'search_demand']);
  for (const row of report.findings) {
    assert.deepEqual(Object.keys(row).sort(), ['action', 'check', 'evidence', 'page', 'status']);
    assert.equal(typeof row.evidence, 'string');
    assert.equal(typeof row.action, 'string');
  }
  assert.equal(report.summary.fail, 0);
});

test('audit fails missing title, wrong heading count and absent local navigation target', async () => {
  const html = minimalPage('<a href="missing.html">Missing page</a>')
    .replace('<title>Useful page</title>', '').replace('<h1>A useful page</h1>', '<h2>A useful page</h2>');
  const report = await auditFiles({ 'index.html': html });
  for (const check of ['document_title', 'primary_heading', 'internal_links']) assert.equal(finding(report, check).status, 'fail');
  assert.match(finding(report, 'internal_links').evidence, /missing\.html/);
  const duplicateHeading = await auditFiles({ 'index.html': minimalPage('<h1>Another heading</h1>') });
  assert.equal(finding(duplicateHeading, 'primary_heading').status, 'fail');
});

test('audit keeps missing/unsafe canonical and duplicate page titles as explicit warnings', async () => {
  for (const canonical of ['', '<link rel="canonical" href="http://example.com/">',
    '<link rel="canonical" href="https://user:pass@example.com/">',
    '<link rel="canonical" href="https://example.com/#fragment">']) {
    const html = minimalPage().replace('<link rel="canonical" href="https://example.com/">', canonical);
    const report = await auditFiles({ 'index.html': html });
    assert.equal(finding(report, 'canonical').status, 'warning');
  }
  const report = await auditFiles({ 'a.html': minimalPage(), 'b.html': minimalPage() });
  assert.equal(finding(report, 'document_title', 'a.html').status, 'pass');
  assert.equal(finding(report, 'document_title', 'b.html').status, 'warning');
});

test('audit flags malformed JSON-LD and does not mistake script text for page headings', async () => {
  const badJson = minimalPage().replace('{"@context":"https://schema.org","@type":"WebPage"}', '{invalid');
  assert.equal(finding(await auditFiles({ 'index.html': badJson }), 'structured_data_json').status, 'fail');
  const missingSchema = minimalPage().replace(/<script[\s\S]*?<\/script>/, '');
  assert.equal(finding(await auditFiles({ 'index.html': missingSchema }), 'structured_data_json').status, 'warning');
  const fakeHeading = minimalPage('<script>const text = "<h1>Fake</h1>";</script>');
  assert.equal(finding(await auditFiles({ 'index.html': fakeHeading }), 'primary_heading').status, 'pass');
});

test('audit resolves nested and clean local URLs while leaving external and contact links alone', async () => {
  const files = {
    'index.html': minimalPage('<a href="/services">Services</a><a href="./nested/">Details</a>'),
    'services.html': minimalPage('<a href="https://other.example/">External</a><a href="mailto:hello@example.com">Email</a><a href="#main">Section</a>'),
    'nested/index.html': minimalPage('<a href="../services.html?view=all#info">Services</a>'),
  };
  const report = await auditFiles(files);
  for (const page of Object.keys(files)) assert.equal(finding(report, 'internal_links', page).status, 'pass');
});

test('generated draft audit discloses noindex rather than treating preview as a live release', async () => {
  const report = await auditFiles(await websiteFiles(brief));
  for (const page of report.pages_checked) {
    assert.equal(finding(report, 'indexing_directive', page).status, 'warning');
    assert.equal(finding(report, 'canonical', page).status, 'pass');
  }
  assert.equal(report.summary.fail, 0);
  assert.equal(report.summary.unknown, 4);
});

test('GEO artifacts contain the same bounded report and readable observed evidence', async () => {
  const files = { 'index.html': minimalPage('<a href="missing.html">Missing</a>') };
  const artifacts = await buildArtifacts('geo', brief, files);
  assert.deepEqual(artifacts.map(row => row.filename).sort(), ['geo-report.json', 'geo-report.txt']);
  const jsonArtifact = artifacts.find(row => row.filename.endsWith('.json'));
  const textArtifact = artifacts.find(row => row.filename.endsWith('.txt'));
  assert.equal(jsonArtifact.media_type, 'application/json');
  assert.equal(textArtifact.media_type, 'text/plain');
  assert.deepEqual(JSON.parse(decoder.decode(jsonArtifact.content)), await auditFiles(files));
  assert.match(decoder.decode(textArtifact.content), /missing\.html/);
  for (const artifact of artifacts) {
    assert.equal(artifact.sha256, digest(artifact.content));
    assert.equal(artifact.size_bytes, artifact.content.byteLength);
  }
  await assert.rejects(async () => buildArtifacts('deploy', brief), validationError);
  await assert.rejects(async () => buildArtifacts('geo', brief, { 'notes.txt': 'No HTML' }), validationError);
});
