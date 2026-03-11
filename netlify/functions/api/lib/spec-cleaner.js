/**
 * Markdown cleanup + What's Next extraction for repo detail view.
 */

function cleanMarkdown(text) {
  if (!text) return '';

  const lines = text.split('\n');
  const cleaned = [];
  let inFrontmatter = false;
  let prevBlank = false;

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    let stripped = line.trim();

    if (stripped === '---' && i === 0) { inFrontmatter = true; continue; }
    if (inFrontmatter) { if (stripped === '---') inFrontmatter = false; continue; }
    if (/^[-=*]{3,}\s*$/.test(stripped)) continue;
    if (/^<\/?[a-z]/i.test(stripped)) continue;

    const headingMatch = stripped.match(/^(#{1,6})\s+(.+)/);
    if (headingMatch) {
      let content = headingMatch[2].replace(/\s*#+\s*$/, '');
      stripped = headingMatch[1].length <= 2 ? content.toUpperCase() : content;
      line = stripped;
    }

    line = line.replace(/\*\*(.+?)\*\*/g, '$1');
    line = line.replace(/__(.+?)__/g, '$1');
    line = line.replace(/(?<!\w)\*([^*]+?)\*(?!\w)/g, '$1');
    line = line.replace(/`([^`]+)`/g, '$1');
    line = line.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
    line = line.replace(/!\[[^\]]*\]\([^)]+\)/g, '');
    line = line.replace(/^>\s*/, '');

    stripped = line.trim();
    if (!stripped) {
      if (prevBlank) continue;
      prevBlank = true;
      cleaned.push('');
      continue;
    }
    prevBlank = false;
    cleaned.push(line.trimEnd());
  }

  while (cleaned.length && !cleaned[0].trim()) cleaned.shift();
  while (cleaned.length && !cleaned[cleaned.length - 1].trim()) cleaned.pop();
  return cleaned.join('\n');
}

function extractWhatsNext(specs, conversations) {
  const items = [];
  const seen = new Set();

  function add(item) {
    const normalized = item.trim().toLowerCase();
    const skipWords = ['to be filled', 'tbd', 'none identified', 'not yet tracked'];
    if (skipWords.some(s => normalized.includes(s))) return;
    if (!normalized || normalized.length <= 5) return;

    const normWords = new Set((normalized.match(/\w{3,}/g) || []));
    for (const existing of seen) {
      const existingWords = new Set((existing.match(/\w{3,}/g) || []));
      if (normWords.size && existingWords.size) {
        let overlap = 0;
        for (const w of normWords) { if (existingWords.has(w)) overlap++; }
        const smaller = Math.min(normWords.size, existingWords.size);
        if (smaller > 0 && overlap / smaller > 0.6) return;
      }
    }
    seen.add(normalized);
    items.push(item.trim());
  }

  const statusText = specs.PROJECT_STATUS || '';
  if (statusText) {
    extractSectionItems(statusText, ['Next Steps', "What's In Progress", "What's Broken", 'Blockers'], add);
  }

  const notesText = specs.SESSION_NOTES || '';
  if (notesText) {
    const boundary = notesText.substring(100).match(/\n---\n|\n#{1,2}\s+Session/);
    const firstSession = boundary ? notesText.substring(0, boundary.index + 100) : notesText.substring(0, 3000);
    extractSectionItems(firstSession, ['Next Steps', 'Questions', 'Blockers', 'TODO'], add);
  }

  const specText = specs.PRODUCT_SPEC || '';
  if (specText) {
    const roadmapMatch = specText.match(/(?:Near Term|Roadmap|Next|TODO)[^\n]*\n((?:[\s\S](?!##))*)/i);
    if (roadmapMatch) extractListItems(roadmapMatch[1], add);

    const inactiveRe = /(?:inactive|pending|not yet|code complete)[^.\n]*/gi;
    let match;
    while ((match = inactiveRe.exec(specText)) !== null) {
      const context = match[0].trim();
      if (context.length > 10) add(`Activate: ${context}`);
    }
  }

  if (conversations) {
    for (const conv of conversations.slice(0, 5)) {
      const name = conv.name || '';
      if (name.length > 10) {
        const dateStr = conv.date_display || '';
        const prefix = dateStr ? `(${dateStr}) ` : '';
        add(`${prefix}Discussed: ${name.substring(0, 120)}`);
      }
    }
  }

  return items.slice(0, 12);
}

function extractSectionItems(text, sectionNames, addFn) {
  for (const section of sectionNames) {
    const escaped = section.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    let pattern = new RegExp(`(?:^|\\n)#+\\s*${escaped}[^\\n]*\\n([\\s\\S]{0,1500}?)(?=^#|$)`, 'mi');
    let match = text.match(pattern);
    if (!match) {
      pattern = new RegExp(`(?:^|\\n)\\*?\\*?${escaped}\\*?\\*?:?\\s*\\n([\\s\\S]{0,1500}?)(?=^#|\\n---|$)`, 'mi');
      match = text.match(pattern);
    }
    if (match) extractListItems(match[1], addFn);
  }
}

function extractListItems(block, addFn) {
  for (const line of block.split('\n')) {
    const stripped = line.trim();
    const itemMatch = stripped.match(/^(?:[-*+]|\d+[.)]\s)\s*(.*)/);
    if (itemMatch) {
      let item = itemMatch[1].trim();
      item = item.replace(/\*\*(.+?)\*\*/g, '$1');
      item = item.replace(/`([^`]+)`/g, '$1');
      item = item.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
      item = item.replace(/^\[[ xX]\]\s*/, '');
      item = item.replace(/^[^\w\s]{1,3}\s*/, '');
      if (item) addFn(item);
    }
  }
}

module.exports = { cleanMarkdown, extractWhatsNext };
