"""The JavaScript that pulls posts out of a rendered group feed.

This runs inside the page in one round trip.  Facebook's markup has no stable
class names and gets rewritten regularly, so everything keys off structural
attributes and degrades to a heuristic rather than throwing.  `text_source`
records which path produced the text, so `dump` shows what happened.

Note on post containers: group posts used to be `div[role="article"]`.  They
are not any more - that role now marks *comments*, and posts are virtualised
feed items carrying `aria-posinset`.  Rather than chase the wrapper, the code
works outwards from the parts that identify a post (its message body, its
author byline) and treats `role="article"` as a fallback for older markup.
"""

EXTRACT_POSTS_JS = r"""
() => {
  const NBSP = String.fromCharCode(160);
  const norm = (s) => (s || '').split(NBSP).join(' ').replace(/[ \t]+/g, ' ').trim();

  const POST_HREF = /facebook\.com\/(?:groups\/[^\/]+\/(?:posts|permalink)\/\d+|permalink\.php|story\.php|share\/p\/|photo\.php|watch\/?\?v=)/i;
  const POST_QUERY = /(?:multi_permalink_id|story_fbid|[?&]fbid)=/i;
  const RELATIVE_TIME = /^(?:\d+\s*(?:s|m|h|d|w|y|min|hr|ura|uri|ure|dan|dni|teden|tedna|leto)\w*|(?:yesterday|včeraj|vceraj|just now|pravkar)\b.*)$/i;

  const TEXT_SELECTORS = [
    'div[data-ad-rendering-role="story_message"]',
    'div[data-ad-preview="message"]',
    'div[data-ad-comet-preview="message"]',
    'div[data-testid="post_message"]',
  ];
  const AUTHOR_SELECTOR = '[data-ad-rendering-role="profile_name"]';

  const CHROME = new RegExp(
    '^(?:' + [
      'like','všeč mi je','vsec mi je','comment','komentiraj','komentar','share','deli','delite',
      'see more','prikaži več','prikazi vec','see less','prikaži manj','more','več','vec',
      'all reactions','vse reakcije','top comments','najbolj priljubljeni komentarji',
      'write a comment','napiši komentar','napisi komentar','view more comments','reply','odgovori',
      'send','pošlji','poslji','follow','sledi','join','pridruži se','pridruzi se',
      'author','avtor','admin','skrbnik','moderator','anonymous member','anonimni član',
      'anonymous participant','anonimni udeleženec','group member','član skupine',
      '\\d+[\\s,.]*(?:comments?|komentarj\\w*|shares?|delitev\\w*|ogledov|views?)',
      '\\d+\\s*[hdwmsy]', '\\d+\\s*(?:min|ura|uri|ure|dan|dni|teden|tedna)\\w*',
    ].join('|') + ')$', 'i');

  const isChrome = (line) => !line || CHROME.test(line.trim());
  const isComment = (el) => /^(?:comment|komentar)/i.test(el.getAttribute('aria-label') || '');

  // ---- which elements are posts -------------------------------------
  function climbToPost(el) {
    for (let node = el, i = 0; node && i < 30; node = node.parentElement, i++) {
      if (node.hasAttribute('aria-posinset')) return node;
      if (node.getAttribute('role') === 'article' && !isComment(node)) return node;
    }
    return null;
  }

  function postContainers() {
    const out = [];
    const add = (el) => {
      if (!el) return;
      // Never keep a container nested inside one already collected.
      if (out.some(o => o === el || o.contains(el) || el.contains(o))) return;
      out.push(el);
    };

    // Posts with a message body, and photo-only posts that still have a byline.
    for (const el of document.querySelectorAll(TEXT_SELECTORS.join(',') + ',' + AUTHOR_SELECTOR)) {
      add(climbToPost(el));
    }

    // Older markup: a top-level article that isn't a comment.
    const arts = Array.from(document.querySelectorAll('div[role="article"]'));
    for (const art of arts) {
      if (isComment(art)) continue;
      if (arts.some(b => b !== art && b.contains(art))) continue;
      add(art);
    }

    // Collected by selector, so restore feed order: the caller notifies
    // oldest-first by reversing this list, which only works if it is in the
    // order the posts appear on the page.
    out.sort((a, b) => {
      const rel = a.compareDocumentPosition(b);
      if (rel & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (rel & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
    return out;
  }

  // ---- fields --------------------------------------------------------
  function pickPermalink(post) {
    // Facebook often renders the timestamp as href="?__cft__[0]=..." with no
    // path, so a post's own link is frequently absent.  When a comment is
    // shown inline its link carries the parent post id, which is as good.
    for (const a of post.querySelectorAll('a[href]')) {
      const href = a.href || '';
      if (!href || href.startsWith('javascript:')) continue;
      if (!POST_HREF.test(href) && !POST_QUERY.test(href)) continue;
      return href.replace(/([?&])comment_id=[^&]*/, '$1').replace(/[?&]$/, '');
    }
    return '';
  }

  // Looks like something a clock would say.  Deliberately loose - Python does
  // the real parsing - but tight enough to tell "3 days ago" from "Learn More".
  const LOOKS_LIKE_TIME = /^(?:\d{1,3}\s*[a-z]{0,8}(?:\s+ago)?|(?:just now|now|pravkar|zdaj)|(?:yesterday|v[cč]eraj)\b.*|(?:pred\s+\d+\s+\w+)|[a-z]{3,12}\s+\d{1,2}(?:,\s*\d{4})?\s+(?:at|ob)\s+\d{1,2}:\d{2}.*|\d{1,2}\.\s*[a-z]{3,12}.*\d{1,2}:\d{2}.*)$/i;

  function spriteTexts(post) {
    // Facebook renders the post age as an SVG sprite: a <use> pointing at a
    // <text id="..."> that lives OUTSIDE the post, so no amount of searching
    // within the post will find the characters.  Follow the reference.
    const out = [];
    for (const use of post.querySelectorAll('use')) {
      const ref = use.getAttribute('xlink:href') || use.getAttribute('href') || '';
      if (!ref.startsWith('#')) continue;
      const target = document.getElementById(ref.slice(1));
      if (!target) continue;
      const text = norm(target.textContent);
      if (text) out.push(text);
    }
    return out;
  }

  function pickTimestamp(post) {
    // A post can carry more than one sprite - a link preview adds a "Learn
    // More" button - so take the first that reads like a time, not the first.
    for (const text of spriteTexts(post)) {
      if (text.length <= 40 && LOOKS_LIKE_TIME.test(text)) return text;
    }
    for (const a of post.querySelectorAll('a[role="link"], a[href], abbr')) {
      const label = a.getAttribute('aria-label') || '';
      if (/\d{4}/.test(label) && /\d/.test(label)) return norm(label);
      const text = norm(a.innerText);
      if (text && text.length <= 20 && RELATIVE_TIME.test(text)) return text;
    }
    for (const el of post.querySelectorAll('span, div')) {
      const text = norm(el.innerText);
      if (text && text.length <= 12 && RELATIVE_TIME.test(text)) return text;
    }
    return '';
  }

  function pickAuthor(post) {
    const byline = post.querySelector(AUTHOR_SELECTOR);
    if (byline) {
      const link = byline.querySelector('a[href]') || byline.closest('a[href]');
      const name = norm(byline.innerText).split('\n')[0];
      if (name) return { author: name, author_url: link ? link.href : '' };
    }
    const header = post.querySelector('h2, h3, h4');
    if (header) {
      const a = header.querySelector('a[href]');
      const name = norm(a ? a.innerText : header.innerText).split('\n')[0];
      if (name) return { author: name, author_url: a ? a.href : '' };
    }
    for (const a of post.querySelectorAll('a[href]')) {
      const href = a.href || '';
      if (/\/(?:user|profile\.php|people)\b/i.test(href)) {
        const name = norm(a.innerText) || a.getAttribute('aria-label') || '';
        if (name && name.length < 90) return { author: norm(name), author_url: href };
      }
    }
    return { author: '', author_url: '' };
  }

  function textFromSelectors(post) {
    for (const sel of TEXT_SELECTORS) {
      const el = post.querySelector(sel);
      if (el) {
        const t = (el.innerText || '').trim();
        if (t) return t;
      }
    }
    return '';
  }

  // True when the node sits inside a comment nested in this post - as opposed
  // to inside the post itself, which in the older markup is also an article.
  const inNestedComment = (el, post) => {
    const art = el.closest('[role="article"]');
    return !!art && art !== post;
  };

  function textFromFallback(post) {
    const raw = Array.from(post.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
      .filter(el => !el.closest('h2, h3, h4') &&
                    !el.closest('a') &&
                    !el.closest('[role="button"]') &&
                    !inNestedComment(el, post) &&
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

  function pickImages(post) {
    const out = [];
    for (const im of Array.from(post.querySelectorAll('img'))) {
      const src = im.currentSrc || im.src || '';
      if (!src || !/^https?:/i.test(src)) continue;
      if (/\bemoji\b|\/rsrc\.php|static\.xx\.fbcdn\.net/i.test(src)) continue;
      if (/profile picture|profilna/i.test(im.getAttribute('alt') || '')) continue;
      if (im.closest('h2, h3, h4')) continue;
      if (im.closest('a[href*="/user/"], a[href*="profile.php"]')) continue;
      if (inNestedComment(im, post)) continue;               // a commenter's avatar
      const sized = src.match(/\/[ps](\d{2,4})x(\d{2,4})\//);
      if (sized && (+sized[1] < 200 || +sized[2] < 200)) continue;
      const r = im.getBoundingClientRect();
      if (r.width && r.width < 150) continue;
      if (!out.includes(src)) out.push(src);
      if (out.length >= 4) break;
    }
    return out;
  }

  // ---- collect -------------------------------------------------------
  const results = [];
  for (const post of postContainers()) {
    const { author, author_url } = pickAuthor(post);

    let text = textFromSelectors(post);
    let text_source = 'selector';
    if (!text) {
      text = textFromFallback(post);
      text_source = 'fallback';
    }
    if (author && text.startsWith(author)) text = text.slice(author.length).trim();

    const permalink = pickPermalink(post);
    if (!text && !permalink) continue;   // an empty virtualised placeholder

    results.push({
      permalink,
      timestamp: pickTimestamp(post),
      author, author_url,
      text: text.trim(),
      text_source,
      images: pickImages(post),
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

# Anything that identifies a rendered post, for waiting on the feed and for
# counting how much has loaded.  Kept in one place because the wrapper element
# has changed before and will again.
POST_MARKER_SELECTOR = (
    'div[role="feed"] [aria-posinset], '
    '[data-ad-rendering-role="story_message"], '
    'div[role="article"]'
)

# Clicking the truncation toggles from Python meant one round trip per button
# to read its label, and a feed carries hundreds of buttons - it cost about two
# seconds a group.  Doing the whole pass in the page costs one round trip.
EXPAND_SEE_MORE_JS = r"""
([labels, max]) => {
  let clicked = 0;
  const nodes = document.querySelectorAll(
    'div[role="feed"] div[role="button"], div[role="article"] div[role="button"]'
  );
  for (const el of nodes) {
    if (clicked >= max) break;
    const label = (el.innerText || '').trim().toLowerCase();
    if (!labels.includes(label)) continue;
    try { el.click(); clicked++; } catch (e) { /* detached or covered */ }
  }
  return clicked;
}
"""

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
