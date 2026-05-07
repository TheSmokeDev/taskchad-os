import { marked } from 'marked';
import DOMPurify from 'dompurify';

// Markdown renderer for chat. GFM (tables, code fences, autolinks) without
// images (rare in chat replies, XSS vector). Every render path goes
// through DOMPurify before reaching the DOM — never raw HTML, never
// dangerouslySetInnerHTML without this wrapper.

marked.use({
  gfm: true,
  breaks: true,
  async: false,
});

const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    'p', 'br', 'b', 'strong', 'i', 'em', 'u', 's', 'del', 'mark',
    'a', 'code', 'pre', 'blockquote',
    'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'span', 'div',
  ],
  ALLOWED_ATTR: ['href', 'title', 'class'],
  ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
};

export function renderMarkdown(text: string): string {
  if (!text) return '';
  const raw = marked.parse(text) as string;
  return DOMPurify.sanitize(raw, PURIFY_CONFIG);
}

/** Plain-text DOMPurify pass for any user-rendered HTML that isn't markdown
 *  but still needs a sanitizer (e.g. server-side rendered HTML snippets). */
export function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, PURIFY_CONFIG);
}
