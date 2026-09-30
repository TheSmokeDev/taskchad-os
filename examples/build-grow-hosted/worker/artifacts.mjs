/** Offline, bounded business website drafts and technical artifact checks.
 * Worker-compatible ESM: no dependencies, provider calls, fetch, process, or DOM.
 */

export class ValidationError extends Error {
  constructor(message) { super(message); this.name = 'ValidationError'; }
}

export const MAX_SOURCE_BYTES = 2 * 1024 * 1024;
export const MAX_SOURCE_FILES = 30;
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });
const suffixes = new Set(['html', 'css', 'json', 'txt', 'xml']);
const ownObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value));
const unsupportedText = /[\u0000-\u0008\u000b-\u001f\uD800-\uDFFF]/u;

function text(value, name, maximum, required = false) {
  if (typeof value !== 'string') throw new ValidationError(`${name} must be text`);
  const result = value.trim();
  if (result.length > maximum || (required && !result)) throw new ValidationError(`${name} is missing or too long`);
  if (unsupportedText.test(result)) throw new ValidationError(`${name} contains unsupported characters`);
  return result;
}

function httpsUrl(value, name, github = false) {
  if (!value) return '';
  let url;
  try { url = new URL(value); } catch { throw new ValidationError(`${name} must be a valid HTTPS URL`); }
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.search || url.hash
      || /\s|\\|[?#]/.test(value)) throw new ValidationError(`${name} must be HTTPS without credentials, query, or fragment`);
  if (github && (url.hostname.toLowerCase() !== 'github.com'
      || !/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/i.test(value)
      || value.slice('https://github.com'.length).split('/').some(part => part === '.' || part === '..'))) {
    throw new ValidationError('repository_url must identify an HTTPS github.com repository');
  }
  return value;
}

export function normalizeBrief(brief) {
  if (!ownObject(brief)) throw new ValidationError('brief must be an object');
  const limits = { business_name: 160, description: 4000, industry: 120, location: 200,
    contact_email: 254, phone: 60, site_url: 1000, repository_url: 1000, brand_color: 7 };
  if (Object.keys(brief).some(key => !Object.hasOwn(limits, key) && key !== 'services')) {
    throw new ValidationError('brief contains unsupported fields');
  }
  const result = {};
  for (const [key, limit] of Object.entries(limits)) {
    result[key] = text(brief[key] ?? (key === 'brand_color' ? '#eab308' : ''), key, limit,
      ['business_name', 'description', 'contact_email'].includes(key));
  }
  if (!/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(result.contact_email)) throw new ValidationError('contact_email must be an email address');
  if (!/^#[0-9a-fA-F]{6}$/.test(result.brand_color)) throw new ValidationError('brand_color must be a six-digit hex color');
  result.site_url = httpsUrl(result.site_url, 'site_url');
  result.repository_url = httpsUrl(result.repository_url, 'repository_url', true);
  if (!Array.isArray(brief.services) || brief.services.length < 1 || brief.services.length > 20) throw new ValidationError('services must contain 1–20 services');
  result.services = brief.services.map(service => {
    if (!ownObject(service) || Object.keys(service).sort().join(',') !== 'description,name') throw new ValidationError('each service must contain name and description');
    return { name: text(service.name, 'service name', 160, true),
      description: text(service.description, 'service description', 2000, true) };
  });
  return result;
}

function sourceName(filename) {
  if (typeof filename !== 'string' || !filename || filename.length > 200
      || /[\\:\u0000-\u001f\uD800-\uDFFF]/u.test(filename) || filename.startsWith('/')
      || filename.split('/').some(part => !part || part === '.' || part === '..' || part.startsWith('.'))
      || !suffixes.has(filename.split('.').at(-1).toLowerCase())) throw new ValidationError('source files require safe relative HTML/CSS/JSON/TXT/XML paths');
  return filename;
}

export function normalizeSourceFiles(sourceFiles) {
  if (!ownObject(sourceFiles) || Object.keys(sourceFiles).length < 1 || Object.keys(sourceFiles).length > MAX_SOURCE_FILES) throw new ValidationError('source_files must contain 1–30 files');
  const result = {};
  let size = 0;
  for (const [filename, content] of Object.entries(sourceFiles)) {
    sourceName(filename);
    if (typeof content !== 'string' || /[\uD800-\uDFFF]/u.test(content)) throw new ValidationError('source file content must be valid UTF-8 text');
    if (content.length > MAX_SOURCE_BYTES) throw new ValidationError('source files exceed the 2 MiB limit');
    size += encoder.encode(content).byteLength;
    if (size > MAX_SOURCE_BYTES) throw new ValidationError('source files exceed the 2 MiB limit');
    result[filename] = content;
  }
  if (!Object.keys(result).some(name => name.toLowerCase().endsWith('.html'))) throw new ValidationError('GEO source files must include an HTML page');
  return result;
}

function sorted(value) {
  if (Array.isArray(value)) return value.map(sorted);
  if (ownObject(value)) return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, sorted(item)]));
  return value;
}
const json = value => JSON.stringify(sorted(value), null, 2);
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#x27;' })[char]);

export async function sha256(value) {
  const bytes = typeof value === 'string' ? encoder.encode(value) : value;
  if (!(bytes instanceof Uint8Array) && !(bytes instanceof ArrayBuffer)) throw new ValidationError('SHA-256 input must be bytes or text');
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

const styles = `:root{--ink:#13221f;--paper:#f7f5ef;--muted:#52645e;--brand:BRAND_COLOR;--line:#dce1d7;--radius:24px;font-family:Arial,Helvetica,sans-serif;color:var(--ink)}
*{box-sizing:border-box}body{margin:0;background:var(--paper);line-height:1.65}a{color:inherit}a:focus-visible{outline:3px solid var(--brand);outline-offset:5px}
a.skip{position:absolute;top:-100px;background:white;padding:12px}a.skip:focus{top:0;z-index:10}.wrap{max-width:1180px;margin:auto;padding:0 28px}
header{border-bottom:1px solid var(--line)}header .wrap{display:flex;align-items:center;justify-content:space-between;gap:24px;min-height:88px}.brand{font-size:21px;font-weight:800;text-decoration:none}
nav{display:flex;gap:25px;align-items:center}nav a{text-decoration:none;font-size:14px;font-weight:700}nav a[aria-current=page]{text-decoration:underline;text-underline-offset:7px}
.hero{padding:88px 0 72px}.hero-grid{display:grid;grid-template-columns:1.4fr 1fr;gap:70px;align-items:center}.eyebrow{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:2px;color:var(--muted);margin:0 0 20px}
h1{font-size:clamp(40px,6vw,76px);line-height:1.03;letter-spacing:-3px;margin:0 0 28px;max-width:820px}h2{font-size:34px;letter-spacing:-1px;line-height:1.2}h3{font-size:22px;line-height:1.3}.lead{font-size:19px;max-width:660px;color:var(--muted)}
.actions{display:flex;gap:14px;flex-wrap:wrap;margin-top:32px}.button{display:inline-block;padding:13px 23px;border-radius:99px;text-decoration:none;font-weight:700;background:var(--ink);color:white;border:1px solid var(--ink)}.button.secondary{background:transparent;color:var(--ink)}
.hero-card{border-radius:32px;background:var(--ink);color:var(--paper);padding:42px;min-height:350px;position:relative;overflow:hidden}.hero-card:after{content:'';position:absolute;right:-100px;top:-130px;width:350px;height:350px;border:65px solid var(--brand);border-radius:50%;opacity:.25}.hero-card>*{position:relative;z-index:1}.hero-card .eyebrow{color:#d0dad3}.hero-card h2{margin-top:85px;font-size:36px}.hero-card p{color:#d0dad3}
.section{padding:56px 0 74px;border-top:1px solid var(--line)}.section-heading{display:flex;justify-content:space-between;gap:28px;align-items:end;margin-bottom:30px}.section-heading h2{margin:0}.section-heading p{color:var(--muted);max-width:440px}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}.card{border:1px solid var(--line);border-radius:var(--radius);padding:30px;background:#fff}.card .number{font-size:12px;font-weight:800;letter-spacing:2px;color:var(--muted)}.card p{color:var(--muted)}.card a{font-weight:700;text-underline-offset:4px}
.contact-grid{display:grid;grid-template-columns:1.3fr 1fr;gap:32px}.contact-box{border-radius:var(--radius);background:var(--ink);color:white;padding:38px}.contact-box a{overflow-wrap:anywhere}.contact-box .eyebrow{color:#d0dad3}.detail{border-bottom:1px solid #ffffff30;padding:16px 0}.detail:last-child{border:0}
footer{border-top:1px solid var(--line);padding:30px 0;color:var(--muted);font-size:14px}footer .wrap{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}
@media(max-width:800px){.hero-grid,.contact-grid{grid-template-columns:1fr;gap:35px}.hero{padding:54px 0}.cards{grid-template-columns:1fr 1fr}.hero-card{min-height:230px}.hero-card h2{margin-top:30px}.section-heading{display:block}}
@media(max-width:540px){.wrap{padding:0 20px}header .wrap{flex-direction:column;align-items:start;justify-content:center;padding-top:20px;padding-bottom:20px;gap:14px}nav{gap:20px}.cards{grid-template-columns:1fr}h1{letter-spacing:-1.5px}.section{padding:40px 0}.hero-card{padding:28px}}`;

function schema(brief, base) {
  const value = { '@context': 'https://schema.org', '@type': 'LocalBusiness', name: brief.business_name,
    description: brief.description, email: brief.contact_email,
    hasOfferCatalog: { '@type': 'OfferCatalog', name: 'Services', itemListElement: brief.services.map(service => ({ '@type': 'Offer', itemOffered: { '@type': 'Service', ...service } })) } };
  if (base) Object.assign(value, { '@id': `${base}/#business`, url: `${base}/` });
  if (brief.location) value.areaServed = brief.location;
  if (brief.phone) value.telephone = brief.phone;
  return json(value).replace(/</g, '\\u003c').replace(/>/g, '\\u003e');
}

function page(brief, filename, title, description, body) {
  const base = brief.site_url.replace(/\/+$/, '');
  const canonical = base ? `<link rel="canonical" href="${escapeHtml(base + '/' + (filename === 'index.html' ? '' : filename))}">` : '';
  const nav = [['index.html', 'Home'], ['services.html', 'Services'], ['contact.html', 'Contact']].map(([path, label]) => `<a href="${path}"${filename === path ? ' aria-current="page"' : ''}>${label}</a>`).join('');
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title><meta name="description" content="${escapeHtml(description)}"><meta name="robots" content="noindex,follow">${canonical}
<link rel="stylesheet" href="styles.css"><script type="application/ld+json">${schema(brief, base)}</script></head>
<body><a class="skip" href="#main">Skip to content</a><header><div class="wrap"><a class="brand" href="index.html">${escapeHtml(brief.business_name)}</a><nav aria-label="Main navigation">${nav}</nav></div></header>
<main id="main">${body}</main><footer><div class="wrap"><span>${escapeHtml(brief.business_name)}</span><span>${escapeHtml(brief.location)}</span></div></footer></body></html>`;
}

export async function websiteFiles(input) {
  const brief = normalizeBrief(input);
  const name = escapeHtml(brief.business_name), description = escapeHtml(brief.description), location = escapeHtml(brief.location);
  const emailLink = 'mailto:' + encodeURIComponent(brief.contact_email).replace(/%40/g, '@');
  const phoneLink = 'tel:' + brief.phone.replace(/[^+0-9]/g, '');
  const area = location ? `<p>Service area: ${location}</p>` : '';
  const cards = brief.services.map((service, index) => `<article class="card"><span class="number">${String(index + 1).padStart(2, '0')}</span><h3>${escapeHtml(service.name)}</h3><p>${escapeHtml(service.description)}</p><a href="contact.html">Discuss this service &rarr;</a></article>`).join('');
  const home = `<section class="hero"><div class="wrap hero-grid"><div><p class="eyebrow">${escapeHtml(brief.industry || 'Our business')}${location ? ' &middot; ' + location : ''}</p><h1>${name}</h1><p class="lead">${description}</p><div class="actions"><a class="button" href="${escapeHtml(emailLink)}">Get in touch &rarr;</a><a class="button secondary" href="services.html">Explore our services</a></div></div><aside class="hero-card"><p class="eyebrow">Let's talk about your project</p><h2>Start with a conversation.</h2><p>Tell us what you need and ask about the services below.</p><a href="contact.html">Contact ${name} &rarr;</a></aside></div></section><section class="section"><div class="wrap"><div class="section-heading"><h2>Our services</h2>${area}</div><div class="cards">${cards}</div></div></section>`;
  const services = `<section class="hero"><div class="wrap"><p class="eyebrow">${name}</p><h1>Our services</h1><p class="lead">Explore the services offered by ${name}.</p>${area}</div></section><section class="section"><div class="wrap"><div class="cards">${cards}</div></div></section>`;
  const contact = `<section class="hero"><div class="wrap contact-grid"><div><p class="eyebrow">Contact ${name}</p><h1>Let's talk.</h1><p class="lead">Ask about our services or tell us about your project.</p><div class="actions"><a class="button" href="${escapeHtml(emailLink)}">Email us &rarr;</a></div></div><div class="contact-box"><p class="eyebrow">Get in touch</p><div class="detail"><strong>Email</strong><br><a href="${escapeHtml(emailLink)}">${escapeHtml(brief.contact_email)}</a></div>${brief.phone ? `<div class="detail"><strong>Phone</strong><br><a href="${escapeHtml(phoneLink)}">${escapeHtml(brief.phone)}</a></div>` : ''}${location ? `<div class="detail"><strong>Service area</strong><br>${location}</div>` : ''}</div></div></section>`;
  const files = { 'index.html': page(brief, 'index.html', brief.business_name, brief.description, home),
    'services.html': page(brief, 'services.html', `Services | ${brief.business_name}`, `Services offered by ${brief.business_name}.`, services),
    'contact.html': page(brief, 'contact.html', `Contact | ${brief.business_name}`, `Contact ${brief.business_name} about its services.`, contact),
    'styles.css': styles.replace('BRAND_COLOR', brief.brand_color), 'robots.txt': 'User-agent: *\nDisallow: /\n' };
  const base = brief.site_url.replace(/\/+$/, '');
  if (base) files['sitemap.xml'] = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ['', 'services.html', 'contact.html'].map(path => `<url><loc>${escapeHtml(base + '/' + path)}</loc></url>`).join('') + '</urlset>';
  files['review.txt'] = 'TASKCHAD WEBSITE DRAFT\n\nThis site uses the business owner\'s supplied facts. They have not been independently verified. No testimonials, rankings, availability promises, certifications or customer results were invented.\n\nOpen index.html locally to review. Email and phone links use the supplied contact details; there is no lead database or form-submission provider.\n\nThe draft is intentionally noindex and robots-blocked. Before publication, confirm the domain and business facts, review all pages, configure hosting and contacts, remove preview indexing restrictions, and verify the exact live release. Approval inside Build + Grow never publishes this archive.\n';
  const hashes = {};
  for (const [filename, content] of Object.entries(files).sort(([a], [b]) => a.localeCompare(b))) hashes[filename] = await sha256(content);
  files['manifest.json'] = json({ schema_version: 1, facts_source: 'owner_provided', preview_only: true,
    brief_sha256: await sha256(json(brief)), files: hashes });
  return normalizeSourceFiles(files);
}

const entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', middot: '·', rarr: '→' };
function decodeEntities(value) {
  return value.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z]+);/g, (match, code) => {
    if (Object.hasOwn(entities, code)) return entities[code];
    if (code.startsWith('#')) {
      const number = code.startsWith('#x') ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return number > 0 && number <= 0x10ffff && !(number >= 0xd800 && number <= 0xdfff) ? String.fromCodePoint(number) : '\ufffd';
    }
    return match;
  });
}

function attributes(token) {
  const attrs = {};
  const regex = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  for (const match of token.matchAll(regex)) attrs[match[1].toLowerCase()] = decodeEntities(match[2] ?? match[3] ?? match[4] ?? '');
  return attrs;
}

function parseDocument(content) {
  const doc = { title: '', language: '', h1_count: 0, has_main: false, meta: {}, canonicals: [], links: [], structured_data: [], text: [] };
  let cursor = 0, inTitle = false;
  const addText = value => { const decoded = decodeEntities(value); if (inTitle) doc.title += decoded; doc.text.push(decoded); };
  while (cursor < content.length) {
    const opening = content.indexOf('<', cursor);
    if (opening < 0) { addText(content.slice(cursor)); break; }
    addText(content.slice(cursor, opening));
    if (content.startsWith('<!--', opening)) {
      const end = content.indexOf('-->', opening + 4); cursor = end < 0 ? content.length : end + 3; continue;
    }
    let closing = opening + 1, quote = '';
    for (; closing < content.length; closing++) {
      const char = content[closing];
      if (quote) { if (char === quote) quote = ''; }
      else if (char === '"' || char === "'") quote = char;
      else if (char === '>') break;
    }
    if (closing === content.length) { addText(content.slice(opening)); break; }
    const token = content.slice(opening + 1, closing), match = /^\s*(\/?)\s*([A-Za-z][A-Za-z0-9:-]*)([\s\S]*)$/.exec(token);
    cursor = closing + 1;
    if (!match) continue;
    const end = Boolean(match[1]), tag = match[2].toLowerCase(), attrs = attributes(match[3]);
    if (end) { if (tag === 'title') inTitle = false; continue; }
    if (tag === 'title') inTitle = true;
    if (tag === 'html') doc.language = attrs.lang || '';
    if (tag === 'h1') doc.h1_count++;
    if (tag === 'main') doc.has_main = true;
    if (tag === 'meta' && attrs.name) doc.meta[attrs.name.toLowerCase()] = attrs.content || '';
    if (tag === 'link' && (attrs.rel || '').toLowerCase().split(/\s+/).includes('canonical')) doc.canonicals.push(attrs.href || '');
    if (tag === 'a' && attrs.href) doc.links.push(attrs.href);
    if (tag === 'script' || tag === 'style') {
      const rest = content.slice(cursor), close = new RegExp(`</${tag}\\s*>`, 'i').exec(rest);
      const raw = close ? rest.slice(0, close.index) : rest;
      if (tag === 'script' && (attrs.type || '').toLowerCase() === 'application/ld+json') doc.structured_data.push(raw);
      cursor = close ? cursor + close.index + close[0].length : content.length;
    }
  }
  return doc;
}

function canonicalValid(values) {
  if (values.length !== 1) return false;
  try { const url = new URL(values[0]); return url.protocol === 'https:' && Boolean(url.hostname) && !url.username && !url.password && !url.hash && !/\s|\\/.test(values[0]); } catch { return false; }
}

function localTarget(filename, href) {
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(href) || href.startsWith('//') || href.startsWith('#') || href.startsWith('?')) return null;
  let path;
  try { path = decodeURIComponent(href.split(/[?#]/, 1)[0]); } catch { return '__invalid_link__'; }
  if (!path) return null;
  const joined = path.startsWith('/') ? path.slice(1) : filename.split('/').slice(0, -1).concat(path).join('/');
  const parts = [];
  for (const part of joined.split('/')) { if (!part || part === '.') continue; if (part === '..') { if (parts.length) parts.pop(); else parts.push('..'); } else parts.push(part); }
  let target = parts.join('/');
  if (!target) target = 'index.html';
  else if (path.endsWith('/')) target += '/index.html';
  return target;
}

export async function auditFiles(input) {
  const sourceFiles = normalizeSourceFiles(input);
  const pages = Object.entries(sourceFiles).filter(([path]) => path.toLowerCase().endsWith('.html')).sort(([a], [b]) => a.localeCompare(b));
  const findings = [], titles = new Map();
  const record = (page, check, status, evidence, action = '') => findings.push({ page, check, status, evidence: evidence.length > 2000 ? evidence.slice(0, 1997) + '...' : evidence, action });
  for (const [path, content] of pages) {
    const doc = parseDocument(content);
    record(path, 'document_language', doc.language.trim() ? 'pass' : 'fail', doc.language || 'No html lang attribute', "Declare the page's actual language.");
    record(path, 'main_landmark', doc.has_main ? 'pass' : 'warning', doc.has_main ? 'main element present' : 'No main element', 'Identify the primary content with a main element.');
    record(path, 'primary_heading', doc.h1_count === 1 ? 'pass' : 'fail', `${doc.h1_count} h1 elements`, 'Use one descriptive primary heading.');
    const title = doc.title.trim(), folded = title.toLowerCase();
    record(path, 'document_title', !title ? 'fail' : titles.has(folded) ? 'warning' : 'pass', !title ? 'No document title' : titles.has(folded) ? `Title duplicates ${titles.get(folded)}` : title, 'Give each page a useful distinct title.');
    titles.set(folded, path);
    const description = (doc.meta.description || '').trim();
    record(path, 'meta_description', description ? 'pass' : 'warning', description || 'No meta description', "Summarize this page's actual content.");
    record(path, 'canonical', canonicalValid(doc.canonicals) ? 'pass' : 'warning', doc.canonicals.join(', ') || 'No canonical URL', 'Confirm the final domain and one absolute HTTPS canonical for this page.');
    const robots = doc.meta.robots || '';
    record(path, 'indexing_directive', robots.toLowerCase().includes('noindex') ? 'warning' : 'pass', robots || 'No page-level noindex directive observed', 'Keep drafts noindex; review indexing policy before the separate live release.');
    let parsed = 0, invalid = false;
    for (const raw of doc.structured_data) { try { JSON.parse(raw); parsed++; } catch { invalid = true; } }
    record(path, 'structured_data_json', invalid ? 'fail' : parsed ? 'pass' : 'warning', invalid ? 'Invalid JSON-LD' : `${parsed} parsed JSON-LD blocks`, 'Use valid JSON-LD with types and claims supported by the business facts.');
    const broken = [];
    for (const href of doc.links) { const target = localTarget(path, href); if (target && !Object.hasOwn(sourceFiles, target) && !Object.hasOwn(sourceFiles, target + '.html') && !Object.hasOwn(sourceFiles, target.replace(/\/$/, '') + '/index.html')) broken.push(href); }
    record(path, 'internal_links', broken.length ? 'fail' : 'pass', broken.length ? broken.slice(0, 8).join(', ') : 'Local navigation targets exist in this packet', 'Repair the missing target or include its rendered page in the evidence packet.');
    const words = doc.text.join(' ').trim().split(/\s+/).filter(Boolean).length;
    record(path, 'readable_content', words >= 30 ? 'pass' : 'warning', `${words} words in supplied HTML (navigation included)`, "Include a clear explanation of this page's purpose. Word count is not a ranking goal.");
  }
  for (const [check, evidence] of [['live_crawl_and_indexing', 'No authenticated crawl/index coverage was supplied.'], ['search_demand', 'No dated query-demand evidence was supplied.'], ['ai_citations', 'No versioned buyer-query answer panel was collected.'], ['qualified_leads', 'No analytics or lead outcome observations were supplied.']]) record('site', check, 'unknown', evidence, 'Measure this separately after a verified release.');
  const hashes = {};
  for (const [path, value] of Object.entries(sourceFiles).sort(([a], [b]) => a.localeCompare(b))) hashes[path] = await sha256(value);
  return { schema_version: 1, scope: 'supplied_static_artifacts', pages_checked: pages.map(([path]) => path), source_sha256: hashes,
    summary: Object.fromEntries(['pass', 'warning', 'fail', 'unknown'].map(status => [status, findings.filter(row => row.status === status).length])), findings,
    limits: ['Offline lexical artifact checks do not establish production behavior or full browser parsing.', 'No search ranking, citation lift, demand, or revenue score is inferred.', 'Business claims remain owner-provided and require review.'] };
}

const crcTable = Uint32Array.from({ length: 256 }, (_, index) => { let value = index; for (let i = 0; i < 8; i++) value = value & 1 ? (value >>> 1) ^ 0xedb88320 : value >>> 1; return value >>> 0; });
function crc32(bytes) { let crc = 0xffffffff; for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8); return (crc ^ 0xffffffff) >>> 0; }
function concat(parts) { const out = new Uint8Array(parts.reduce((size, part) => size + part.length, 0)); let cursor = 0; for (const part of parts) { out.set(part, cursor); cursor += part.length; } return out; }
function archiveText(bytes) {
  try { return decoder.decode(bytes); } catch { throw new ValidationError('Archive text must be valid UTF-8'); }
}

export function zipStored(input) {
  const files = normalizeSourceFiles(input), local = [], central = [];
  let offset = 0;
  for (const [filename, content] of Object.entries(files).sort(([a], [b]) => a.localeCompare(b))) {
    const name = encoder.encode(filename), bytes = encoder.encode(content), crc = crc32(bytes);
    const header = new Uint8Array(30), h = new DataView(header.buffer);
    h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true); h.setUint16(12, 33, true);
    h.setUint32(14, crc, true); h.setUint32(18, bytes.length, true); h.setUint32(22, bytes.length, true); h.setUint16(26, name.length, true);
    local.push(header, name, bytes);
    const directory = new Uint8Array(46), d = new DataView(directory.buffer);
    d.setUint32(0, 0x02014b50, true); d.setUint16(4, 0x0314, true); d.setUint16(6, 20, true); d.setUint16(8, 0x0800, true); d.setUint16(14, 33, true);
    d.setUint32(16, crc, true); d.setUint32(20, bytes.length, true); d.setUint32(24, bytes.length, true); d.setUint16(28, name.length, true); d.setUint32(38, 0x81a40000, true); d.setUint32(42, offset, true);
    central.push(directory, name); offset += header.length + name.length + bytes.length;
  }
  const directory = concat(central), end = new Uint8Array(22), e = new DataView(end.buffer);
  e.setUint32(0, 0x06054b50, true); e.setUint16(8, Object.keys(files).length, true); e.setUint16(10, Object.keys(files).length, true); e.setUint32(12, directory.length, true); e.setUint32(16, offset, true);
  return concat([...local, directory, end]);
}

export function readStoredZip(input) {
  if (!(input instanceof Uint8Array) || input.length > MAX_SOURCE_BYTES + 32768 || input.length < 22) throw new ValidationError('Invalid or oversized static archive');
  const view = new DataView(input.buffer, input.byteOffset, input.byteLength), files = {}, entries = [];
  let cursor = 0, size = 0;
  while (cursor + 4 <= input.length && view.getUint32(cursor, true) === 0x04034b50) {
    if (cursor + 30 > input.length) throw new ValidationError('Truncated ZIP header');
    const flags = view.getUint16(cursor + 6, true), method = view.getUint16(cursor + 8, true), crc = view.getUint32(cursor + 14, true), compressed = view.getUint32(cursor + 18, true), length = view.getUint32(cursor + 22, true), nameLength = view.getUint16(cursor + 26, true), extraLength = view.getUint16(cursor + 28, true);
    if (flags !== 0x0800 || method !== 0 || compressed !== length || !nameLength) throw new ValidationError('Only plain stored UTF-8 ZIP entries are supported');
    const start = cursor + 30 + nameLength + extraLength, end = start + length;
    if (end > input.length || ++size > MAX_SOURCE_FILES) throw new ValidationError('Truncated or excessive archive');
    const name = archiveText(input.subarray(cursor + 30, cursor + 30 + nameLength)); sourceName(name);
    if (Object.hasOwn(files, name)) throw new ValidationError('Duplicate archive filename');
    const bytes = input.subarray(start, end); if (crc32(bytes) !== crc) throw new ValidationError('Archive CRC mismatch');
    files[name] = archiveText(bytes); entries.push({ name, offset: cursor, crc, length }); cursor = end;
  }
  if (cursor + 4 > input.length || view.getUint32(cursor, true) !== 0x02014b50 || view.getUint32(input.length - 22, true) !== 0x06054b50) throw new ValidationError('Archive directory is missing');
  const directoryOffset = view.getUint32(input.length - 6, true), directoryLength = view.getUint32(input.length - 10, true), count = view.getUint16(input.length - 12, true);
  if (directoryOffset !== cursor || directoryOffset + directoryLength !== input.length - 22 || count !== size) throw new ValidationError('Archive directory does not match entries');
  if (view.getUint16(input.length - 18, true) || view.getUint16(input.length - 16, true) || view.getUint16(input.length - 14, true) !== count || view.getUint16(input.length - 2, true)) throw new ValidationError('Multi-disk or commented archives are unsupported');
  for (const entry of entries) {
    if (cursor + 46 > input.length - 22 || view.getUint32(cursor, true) !== 0x02014b50) throw new ValidationError('Archive directory is truncated');
    const nameLength = view.getUint16(cursor + 28, true), extraLength = view.getUint16(cursor + 30, true), commentLength = view.getUint16(cursor + 32, true);
    const end = cursor + 46 + nameLength + extraLength + commentLength;
    if (end > input.length - 22 || view.getUint16(cursor + 8, true) !== 0x0800 || view.getUint16(cursor + 10, true) !== 0
        || view.getUint32(cursor + 16, true) !== entry.crc || view.getUint32(cursor + 20, true) !== entry.length || view.getUint32(cursor + 24, true) !== entry.length
        || view.getUint32(cursor + 42, true) !== entry.offset || archiveText(input.subarray(cursor + 46, cursor + 46 + nameLength)) !== entry.name
        || view.getUint16(cursor + 34, true) !== 0 || (view.getUint32(cursor + 38, true) >>> 16 & 0xf000) !== 0x8000) throw new ValidationError('Archive directory disagrees with ordinary file entries');
    cursor = end;
  }
  if (cursor !== input.length - 22) throw new ValidationError('Archive directory has unexpected entries');
  return normalizeSourceFiles(files);
}

function reportText(report) {
  const lines = ['TASKCHAD SEO / GEO ARTIFACT CHECKS', '', `Scope: ${report.scope}`, `Pages checked: ${report.pages_checked.length}`, '', 'Observed findings:'];
  for (const row of report.findings) { lines.push(`[${row.status.toUpperCase()}] ${row.page} / ${row.check}: ${row.evidence}`); if (row.action && row.status !== 'pass') lines.push('  Next: ' + row.action); }
  return [...lines, '', 'Limits:', ...report.limits, ''].join('\n');
}

async function artifact(filename, media_type, input) {
  const content = typeof input === 'string' ? encoder.encode(input) : input;
  if (content.length > MAX_SOURCE_BYTES) throw new ValidationError('Artifact exceeds the 2 MiB limit');
  return { filename, media_type, content, sha256: await sha256(content), size_bytes: content.length };
}

export async function buildArtifacts(kind, brief, source_files = {}) {
  if (kind === 'website') {
    const files = await websiteFiles(brief), report = await auditFiles(files);
    if (report.summary.fail) throw new Error('Generated website failed its local artifact checks');
    return [await artifact('website-draft.zip', 'application/zip', zipStored(files)), await artifact('website-checks.json', 'application/json', json(report))];
  }
  if (kind === 'geo') {
    const report = await auditFiles(source_files);
    return [await artifact('geo-report.json', 'application/json', json(report)), await artifact('geo-report.txt', 'text/plain', reportText(report))];
  }
  throw new ValidationError('Unsupported Build + Grow job kind');
}
