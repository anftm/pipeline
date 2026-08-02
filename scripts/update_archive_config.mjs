import crypto from "node:crypto";
import fs from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const original = String(payload.content);
const sandbox = { result: null };
const executable = original
  .replace(/^\s*export\s+default\s+/, "result = ")
  .replace(/;\s*$/, "");
vm.runInNewContext(executable, sandbox, { timeout: 1000 });
const config = sandbox.result;
if (!config || typeof config !== "object") throw new Error("config file did not export an object");

function twoDigits(value, fallback = "00") {
  if (value === undefined || value === null) return fallback;
  return value < 10 ? `0${value}` : String(value);
}

function articleId(article) {
  const dates = [...(article.dates || [])]
    .sort((a, b) => `${a.year || "0000"}-${twoDigits(a.month)}-${twoDigits(a.day)}`.localeCompare(`${b.year || "0000"}-${twoDigits(b.month)}-${twoDigits(b.day)}`))
    .map((date) => `${date.year || "0000"}-${twoDigits(date.month)}-${twoDigits(date.day)}`);
  const authors = [...(article.authors || [])].sort();
  return crypto.createHash("md5").update(JSON.stringify([
    article.title,
    dates,
    Boolean(article.is_range_date),
    authors,
    article.file_id || "",
  ])).digest("hex").slice(0, 10);
}

function skipQuoted(source, index) {
  const quote = source[index];
  index += 1;
  while (index < source.length) {
    if (source[index] === "\\") index += 2;
    else if (source[index] === quote) return index + 1;
    else index += 1;
  }
  throw new Error("unterminated string in config");
}

function skipComment(source, index) {
  if (source[index + 1] === "/") {
    const end = source.indexOf("\n", index + 2);
    return end < 0 ? source.length : end + 1;
  }
  if (source[index + 1] === "*") {
    const end = source.indexOf("*/", index + 2);
    if (end < 0) throw new Error("unterminated comment in config");
    return end + 2;
  }
  return index;
}

function matching(source, start, open, close) {
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (char === '"' || char === "'" || char === "`") {
      index = skipQuoted(source, index) - 1;
      continue;
    }
    if (char === "/" && (source[index + 1] === "/" || source[index + 1] === "*")) {
      index = skipComment(source, index) - 1;
      continue;
    }
    if (char === open) depth += 1;
    if (char === close) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  throw new Error(`unmatched ${open} in config`);
}

function propertyRange(source, property, objectStart, objectEnd = null) {
  const limit = objectEnd === null ? source.length : objectEnd;
  for (let index = objectStart + 1; index < limit; index += 1) {
    let key = "";
    let afterKey = index;
    const char = source[index];
    if (char === "{") {
      index = matching(source, index, "{", "}");
      continue;
    } else if (char === "[") {
      index = matching(source, index, "[", "]");
      continue;
    } else if (char === '"' || char === "'") {
      afterKey = skipQuoted(source, index);
      key = source.slice(index + 1, afterKey - 1);
    } else if (/[A-Za-z0-9_$]/.test(char)) {
      afterKey = index + 1;
      while (afterKey < limit && /[A-Za-z0-9_$]/.test(source[afterKey])) afterKey += 1;
      key = source.slice(index, afterKey);
    } else if (char === "`") {
      index = skipQuoted(source, index) - 1;
      continue;
    } else if (char === "/" && (source[index + 1] === "/" || source[index + 1] === "*")) {
      index = skipComment(source, index) - 1;
      continue;
    } else {
      continue;
    }
    let valueStart = afterKey;
    while (valueStart < limit && /\s/.test(source[valueStart])) valueStart += 1;
    if (key !== property || source[valueStart] !== ":") {
      index = afterKey - 1;
      continue;
    }
    valueStart += 1;
    while (valueStart < limit && /\s/.test(source[valueStart])) valueStart += 1;
    let valueEnd = valueStart;
    if (source[valueStart] === "{") valueEnd = matching(source, valueStart, "{", "}") + 1;
    else if (source[valueStart] === "[") valueEnd = matching(source, valueStart, "[", "]") + 1;
    else if (source[valueStart] === '"' || source[valueStart] === "'" || source[valueStart] === "`") valueEnd = skipQuoted(source, valueStart);
    else {
      while (valueEnd < limit && source[valueEnd] !== "," && source[valueEnd] !== "}") valueEnd += 1;
      while (valueEnd > valueStart && /\s/.test(source[valueEnd - 1])) valueEnd -= 1;
    }
    return { start: valueStart, end: valueEnd };
  }
  return null;
}

function arrayObjects(source, arrayStart) {
  const end = matching(source, arrayStart, "[", "]");
  const ranges = [];
  for (let index = arrayStart + 1; index < end; index += 1) {
    const char = source[index];
    if (char === '"' || char === "'" || char === "`") {
      index = skipQuoted(source, index) - 1;
    } else if (char === "/" && (source[index + 1] === "/" || source[index + 1] === "*")) {
      index = skipComment(source, index) - 1;
    } else if (char === "{") {
      const objectEnd = matching(source, index, "{", "}");
      ranges.push([index, objectEnd + 1]);
      index = objectEnd;
    }
  }
  return ranges;
}

function formattedReplacement(source, start, value) {
  const lineStart = source.lastIndexOf("\n", start - 1) + 1;
  const indent = (source.slice(lineStart, start).match(/^\s*/) || [""])[0];
  return JSON.stringify(value, null, 2).split("\n").map((line, index) => index ? indent + line : line).join("\n");
}

function fieldReplacements(source, objectStart, objectEnd, values) {
  const replacements = [];
  const missing = [];
  for (const [key, value] of Object.entries(values)) {
    const range = propertyRange(source, key, objectStart, objectEnd);
    if (range) replacements.push({ ...range, value });
    else missing.push([key, value]);
  }
  if (missing.length) {
    const lineStart = source.lastIndexOf("\n", objectStart - 1) + 1;
    const indent = (source.slice(lineStart, objectStart).match(/^\s*/) || [""])[0] + "  ";
    const body = source.slice(objectStart + 1, objectEnd - 1).trimEnd();
    const separator = body.trim() && !body.trim().endsWith(",") ? "," : "";
    const text = separator + "\n" + missing.map(([key, value]) =>
      `${indent}${JSON.stringify(key)}: ${formattedReplacement(source, objectStart + indent.length, value)}`
    ).join(",\n");
    replacements.push({ start: objectEnd - 1, end: objectEnd - 1, raw: text });
  }
  return replacements;
}

const articles = config.parser_option && config.parser_option.articles;
if (!Array.isArray(articles)) throw new Error("config does not contain parser_option.articles");
let articleIndex = articles.findIndex((article) => articleId(article) === payload.article_id);
if (articleIndex < 0 && payload.locator) {
  const matches = articles.map((article, index) => ({ article, index })).filter(({ article }) =>
    article.title === payload.locator.title
    && article.page_start === payload.locator.page_start
    && article.page_end === payload.locator.page_end
  );
  if (matches.length === 1) articleIndex = matches[0].index;
}
if (articleIndex < 0) throw new Error("expected one config article, found none");

const metadata = payload.metadata || {};
const articlePatch = metadata.article || {};
for (const key of ["title", "authors", "dates", "tags"]) {
  if (Object.prototype.hasOwnProperty.call(articlePatch, key)) articles[articleIndex][key] = articlePatch[key];
}
const sourcePatch = metadata.source || {};
for (const key of ["name", "author", "type", "files"]) {
  if (Object.prototype.hasOwnProperty.call(sourcePatch, key)) config.entity[key] = sourcePatch[key];
}

const replacements = [];
if (Object.keys(articlePatch).length) {
  const rootStart = original.indexOf("{");
  const rootEnd = matching(original, rootStart, "{", "}") + 1;
  const parserRange = propertyRange(original, "parser_option", rootStart, rootEnd);
  if (!parserRange || original[parserRange.start] !== "{") throw new Error("config parser_option is not an object literal");
  const articlesRange = propertyRange(original, "articles", parserRange.start, parserRange.end);
  if (!articlesRange || original[articlesRange.start] !== "[") throw new Error("config articles is not an array literal");
  const ranges = arrayObjects(original, articlesRange.start);
  if (!ranges[articleIndex]) throw new Error("config article text range was not found");
  replacements.push(...fieldReplacements(
    original, ranges[articleIndex][0], ranges[articleIndex][1], articlePatch,
  ));
}
if (Object.keys(sourcePatch).length) {
  const rootStart = original.indexOf("{");
  const rootEnd = matching(original, rootStart, "{", "}") + 1;
  const entityRange = propertyRange(original, "entity", rootStart, rootEnd);
  if (!entityRange || original[entityRange.start] !== "{") throw new Error("config entity is not an object literal");
  replacements.push(...fieldReplacements(
    original, entityRange.start, entityRange.end, sourcePatch,
  ));
}
let content = original;
for (const replacement of replacements.sort((a, b) => b.start - a.start)) {
  content = content.slice(0, replacement.start)
    + (replacement.raw === undefined ? formattedReplacement(original, replacement.start, replacement.value) : replacement.raw)
    + content.slice(replacement.end);
}
process.stdout.write(JSON.stringify({ content, article_id: articleId(articles[articleIndex]) }));
