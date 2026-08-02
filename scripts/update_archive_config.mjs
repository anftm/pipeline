import crypto from "node:crypto";
import fs from "node:fs";
import vm from "node:vm";

const input = fs.readFileSync(0, "utf8");
const payload = JSON.parse(input);
const sandbox = { result: null };
const source = String(payload.content)
  .replace(/^\s*export\s+default\s+/, "result = ")
  .replace(/;\s*$/, "");
vm.runInNewContext(source, sandbox, { timeout: 1000 });
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

const articles = config.parser_option && config.parser_option.articles;
if (!Array.isArray(articles)) throw new Error("config does not contain parser_option.articles");
let candidates = articles.filter((article) => articleId(article) === payload.article_id);
if (candidates.length !== 1 && payload.locator) {
  candidates = articles.filter((article) =>
    article.title === payload.locator.title
    && article.page_start === payload.locator.page_start
    && article.page_end === payload.locator.page_end
  );
}
if (candidates.length !== 1) throw new Error(`expected one config article, found ${candidates.length}`);

const metadata = payload.metadata || {};
const articlePatch = metadata.article || {};
for (const key of ["title", "authors", "dates", "tags"]) {
  if (Object.prototype.hasOwnProperty.call(articlePatch, key)) candidates[0][key] = articlePatch[key];
}
const sourcePatch = metadata.source || {};
for (const key of ["name", "author", "type", "files"]) {
  if (Object.prototype.hasOwnProperty.call(sourcePatch, key)) config.entity[key] = sourcePatch[key];
}
process.stdout.write(`export default ${JSON.stringify(config, null, 2)};\n`);
