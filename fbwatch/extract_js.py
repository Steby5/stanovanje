"""The JavaScript that pulls posts out of a rendered group feed.

This runs inside the page in one round trip.  Facebook's markup has no stable
class names, so everything keys off structural attributes that survive their
frequent rewrites: `div[role="article"]` for a post, the `data-ad-*` message
containers for the body text, and permalink-shaped hrefs for the post URL.
Every step degrades to a heuristic rather than throwing, and `text_source`
records which path produced the text so `dump` can show what happened.
"""

EXTRACT_POSTS_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/ /g, ' ').replace(/[ \t]+/g, ' ').trim();

  // ---- which elements are posts -------------------------------------
  const all = Array.from(document.querySelectorAll('div[role="article"]'));
  // Comments are also role=article; keep only the outermost ones.
  const tops = all.filter(a => !all.some(b => b !== a && b.contains(a)));

  const POST_HREF = /facebook\.com\/(?:groups\/[^\/]+\/(?:posts|permalink)\/\d+|permalink\.php|story\.php|share\/p\/|photo\.php|watch\/?\?v=)/i;
  const POST_QUERY = /(?:multi_permalink_id|story_fbid|[?&]fbid)=/i;

  const TEXT_SELECTORS = [
    'div[data-ad-rendering-role="story_message"]',
    'div[data-ad-preview="message"]',
    'div[data-ad-comet-preview="message"]',
    'div[data-testid="post_message"]',
  ];

  // Lines of chrome that appear in innerText but are not part of the post.
  const CHROME = new RegExp(
    '^(?:' + [
      'like','všeč mi je','vsec mi je','comment','komentiraj','komentar','share','deli','delite',
      'see more','prikaži več','prikazi vec','see less','prikaži manj','more','več','vec',
      'all reactions','vse reakcije','top comments','najbolj priljubljeni komentarji',
      'write a comment','napiši komentar','napisi komentar','view more comments',
      'send','pošlji','poslji','follow','sledi','join','pridruži se','pridruzi se',
      'author','avtor','admin','skrbnik','moderator','anonymous member','anonimni član',
      '\\d+[\\s,.]*(?:comments?|komentarj\\w*|shares?|delitev\\w*|ogledov|views?)',
      '\\d+\\s*[hdwmsy]', '\\d+\\s*(?:min|ura|uri|ure|dan|dni|teden|tedna)\\w*',
    ].join('|') + ')$', 'i');

  const isChrome = (line) => !line || CHROME.test(line.trim());

  // ---- helpers -------------------------------------------------------
  function pickPermalink(art) {
    const anchors = Array.from(art.querySelectorAll('a[href]'));
    let best = null, bestTime = '';
    for (const a of anchors) {
      const href = a.href || '';
      if (!href || href.startsWith('javascript:')) continue;
      if (!POST_HREF.test(href) && !POST_QUERY.test(href)) continue;
      // The header timestamp link comes first in DOM order and is the one
      // that points at the post itself rather than at a comment or reaction.
      if (!best) {
        best = href;
        bestTime = norm(a.innerText) || a.getAttribute('aria-label') || '';
      }
    }
    return { permalink: best, timestamp: bestTime };
  }

  function pickAuthor(art) {
    const header = art.querySelector('h2, h3, h4');
    if (header) {
      const a = header.querySelector('a[href]');
      if (a) {
        const name = norm(a.innerText);
        if (name) return { author: name, author_url: a.href };
      }
      const name = norm(header.innerText);
      if (name) return { author: name.split('\n')[0], author_url: '' };
    }
    for (const a of Array.from(art.querySelectorAll('a[href]'))) {
      const href = a.href || '';
      if (/\/(?:user|profile\.php|people)\b/i.test(href) ||
          (/facebook\.com\/[^\/?#]+\/?$/i.test(href) && !/\/groups\//i.test(href))) {
        const name = norm(a.innerText);
        if (name && name.length < 90) return { author: name, author_url: href };
      }
    }
    return { author: '', author_url: '' };
  }

  function textFromSelectors(art) {
    for (const sel of TEXT_SELECTORS) {
      const el = art.querySelector(sel);
      if (el) {
        const t = (el.innerText || '').trim();
        if (t) return t;
      }
    }
    return '';
  }

  function textFromFallback(art) {
    // Facebook renders body copy in dir="auto" blocks.  Take those that are
    // not part of the header, a link, or a button, then drop fragments that
    // are already contained in a longer sibling (the DOM nests them).
    const raw = Array.from(art.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
      .filter(el => !el.closest('h2, h3, h4') &&
                    !el.closest('a') &&
                    !el.closest('[role="button"]') &&
                    !el.closest('ul[role="list"]'))
      .map(el => (el.innerText || '').trim())
      .filter(t => t.length > 0);

    const kept = [];
    for (const t of raw) {
      if (raw.some(other => other !== t && other.includes(t))) continue;
      if (!kept.includes(t)) kept.push(t);
    }
    const lines = kept.join('\n').split('\n').map(l => l.trim()).filter(l => !isChrome(l));
    return lines.join('\n').trim();
  }

  function pickImages(art) {
    const out = [];
    for (const im of Array.from(art.querySelectorAll('img'))) {
      const src = im.currentSrc || im.src || '';
      if (!src || !/^https?:/i.test(src)) continue;
      if (/\bemoji\b|\/rsrc\.php|static\.xx\.fbcdn\.net/i.test(src)) continue;
      if (/profile picture|profilna/i.test(im.getAttribute('alt') || '')) continue;
      if (im.closest('h2, h3, h4')) continue;             // author avatar
      if (im.closest('a[href*="/user/"], a[href*="profile.php"]')) continue;
      // Thumbnails encode their size in the path; skip the small ones.
      const sized = src.match(/\/[ps](\d{2,4})x(\d{2,4})\//);
      if (sized && (+sized[1] < 200 || +sized[2] < 200)) continue;
      const r = im.getBoundingClientRect();
      if (r.width && r.width < 150) continue;             // 0 when media is blocked
      if (!out.includes(src)) out.push(src);
      if (out.length >= 4) break;
    }
    return out;
  }

  // ---- collect -------------------------------------------------------
  const results = [];
  for (const art of tops) {
    const label = art.getAttribute('aria-label') || '';
    if (/^(?:comment|komentar)/i.test(label)) continue;

    const { permalink, timestamp } = pickPermalink(art);
    const { author, author_url } = pickAuthor(art);

    let text = textFromSelectors(art);
    let text_source = 'selector';
    if (!text) {
      text = textFromFallback(art);
      text_source = 'fallback';
    }
    // Drop the author name if it leaked in as the first line.
    if (author && text.startsWith(author)) {
      text = text.slice(author.length).trim();
    }

    if (!text && !permalink) continue;   // not a real post

    results.push({
      permalink: permalink || '',
      timestamp: timestamp || '',
      author, author_url,
      text: text.trim(),
      text_source,
      images: pickImages(art),
    });
  }
  return results;
}
"""

# Facebook truncates long posts behind a "See more" control.  These are the
# labels for the locales this is likely to run in; matching is done on the
# lowercased button text.
SEE_MORE_LABELS = {
    "see more",
    "prikaži več",
    "prikazi vec",
    "prikaži vec",
    "več",
    "vec",
    "mehr anzeigen",
    "ver más",
    "ver mas",
    "voir plus",
    "altro",
    "vidi više",
    "vidi vise",
    "prikaži još",
    "prikazi jos",
}

# A logged-out or checkpointed page shows one of these instead of the feed.
LOGIN_MARKERS_JS = r"""
() => {
  const url = location.href;
  if (/\/(?:login|checkpoint|recover)\b/i.test(url)) return 'redirected to ' + url;
  if (document.querySelector('input[name="email"][type="text"], input[name="pass"]')) {
    return 'login form present';
  }
  const body = (document.body ? document.body.innerText : '').slice(0, 1500).toLowerCase();
  for (const marker of ['log in to facebook', 'prijava v facebook', 'prijavite se v facebook',
                        'you must log in', 'za nadaljevanje se prijavite']) {
    if (body.includes(marker)) return marker;
  }
  return '';
}
"""
