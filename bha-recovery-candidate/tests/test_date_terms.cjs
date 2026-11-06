"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const start = "// BEGIN STRICT DATE TERMS", end = "// END STRICT DATE TERMS";
assert.equal(source.split(start).length, 2);
assert.equal(source.split(end).length, 2);
const htmlStart = "// BEGIN STRICT HTML ESCAPE", htmlEnd = "// END STRICT HTML ESCAPE";
assert.equal(source.split(htmlStart).length, 2); assert.equal(source.split(htmlEnd).length, 2);
const section = source.slice(source.indexOf(start), source.indexOf(end) + end.length) + "\n" +
  source.slice(source.indexOf(htmlStart), source.indexOf(htmlEnd) + htmlEnd.length);
const sandbox = {};
vm.runInNewContext(section, sandbox);
const snapshot = value => JSON.parse(JSON.stringify(value));
assert.deepEqual(snapshot(sandbox.parseDateTermParam("year:1967-month:8-day:29")), { year: "1967", month: "8", day: "29", display: "" });
assert.equal(sandbox.dateTermParam({year: "1967", month: "8", day: "29"}), "year:1967-month:8-day:29");
assert.equal(sandbox.dateTermKey({year: "1967", month: "08", day: "29"}), "1967|8|29");
assert.equal(sandbox.uniqDateTerms([{year: "1967", month: "08"}, {year: "1967", month: "8"}]).length, 1);
assert.equal(sandbox.resultDateTermKey("1967/8/29"), "1967|8|29");
assert.equal(sandbox.resultDateTermKey("不详"), "display|不详");
assert.equal(sandbox.parseDateTermParam("year:0-month:13-day:32"), null);
assert.equal(sandbox.escapeHTML('<甲&"'), "&lt;甲&amp;&quot;");
assert.equal(sandbox.escapeHTML(null), "");
const rStart = "// BEGIN STRICT DATE RANGES", rEnd = "// END STRICT DATE RANGES";
assert.equal(source.split(rStart).length, 2); assert.equal(source.split(rEnd).length, 2);
const ranges = `const STATE = {dateFromYear:'',dateFromMonth:'',dateFromDay:'',dateToYear:'',dateToMonth:'',dateToDay:'',dateTerms:[]};
const DOM = {dateError: {hidden:false,textContent:''}}; function showToast() {}\n` + section + "\n" +
  source.slice(source.indexOf(rStart), source.indexOf(rEnd) + rEnd.length) + `
setDateParts('from','202402'); setDateParts('to','202402');
globalThis.bounds = [rangeDateValue('from'), rangeDateValue('to'), daysInMonth('2024','2')];
setDateParts('from','20250101'); globalThis.invalid = !validateDateRange({show:false});`;
const rangeSandbox = {}; vm.runInNewContext(ranges, rangeSandbox);
assert.deepEqual(Array.from(rangeSandbox.bounds), [20240201, 20240229, 29]);
assert.equal(rangeSandbox.invalid, true);
const uStart = "// BEGIN STRICT DATE INPUT UPDATE", uEnd = "// END STRICT DATE INPUT UPDATE";
assert.equal(source.split(uStart).length, 2); assert.equal(source.split(uEnd).length, 2);
const updates = `const STATE={dateFromYear:'2024',dateFromMonth:'1',dateFromDay:'31',dateToYear:'2024',dateToMonth:'2',dateToDay:'29'};\n` +
  section + "\n" + source.slice(source.indexOf(rStart), source.indexOf(rEnd) + rEnd.length) + "\n" +
  source.slice(source.indexOf(uStart), source.indexOf(uEnd) + uEnd.length) + `
updateDateInputState('dateFromMonth','2'); globalThis.clamped=STATE.dateFromDay;
updateDateInputState('dateToMonth',''); globalThis.clearedMonth=STATE.dateToDay;
updateDateInputState('dateFromYear',''); globalThis.clearedYear=[STATE.dateFromMonth,STATE.dateFromDay];`;
const updateSandbox = {}; vm.runInNewContext(updates, updateSandbox);
assert.equal(updateSandbox.clamped, ""); assert.equal(updateSandbox.clearedMonth, "");
assert.deepEqual(Array.from(updateSandbox.clearedYear), ["",""]);
const hStart = "// BEGIN STRICT PREVIEW HIGHLIGHT", hEnd = "// END STRICT PREVIEW HIGHLIGHT";
assert.equal(source.split(hStart).length, 2); assert.equal(source.split(hEnd).length, 2);
const highlights = `const STATE = {q:'手.机', exact:true, previewHighlight:true};\n` + section + "\n" +
  source.slice(source.indexOf(hStart), source.indexOf(hEnd) + hEnd.length) + `
globalThis.exact = highlightPreviewText('<b>手.机</b> 手大机');
STATE.q='甲 乙';STATE.exact=false;globalThis.multi=highlightPreviewText('甲与乙');`;
const highlightSandbox = {}; vm.runInNewContext(highlights, highlightSandbox);
assert.equal(highlightSandbox.exact, "&lt;b&gt;<mark>手.机</mark>&lt;/b&gt; 手大机");
assert.equal(highlightSandbox.multi, "<mark>甲</mark>与<mark>乙</mark>");
const kStart = "// BEGIN STRICT FACET KEYS", kEnd = "// END STRICT FACET KEYS";
assert.equal(source.split(kStart).length, 2); assert.equal(source.split(kEnd).length, 2);
const facets = {}; vm.runInNewContext(source.slice(source.indexOf(kStart), source.indexOf(kEnd) + kEnd.length), facets);
assert.deepEqual(snapshot(facets.filterKeys("source")), {include: "sources", exclude: "excludeSources"});
assert.deepEqual(snapshot(facets.filterKeys("author")), {include: "authors", exclude: "excludeAuthors"});
assert.deepEqual(snapshot(facets.filterKeys("tag")), {include: "tags", exclude: "excludeTags"});
const sStart = "// BEGIN STRICT STABLE STRINGIFY", sEnd = "// END STRICT STABLE STRINGIFY";
assert.equal(source.split(sStart).length, 2); assert.equal(source.split(sEnd).length, 2);
const stable = {}; vm.runInNewContext(source.slice(source.indexOf(sStart), source.indexOf(sEnd) + sEnd.length), stable);
assert.equal(stable.stableStringify({z: [1, 2], a: {q: true, b: "x"}}), '{"a":{"b":"x","q":true},"z":[1,2]}');
assert.equal(stable.stableStringify({b: 1, a: 2}), stable.stableStringify({a: 2, b: 1}));
const jStart = "// BEGIN STRICT SEARCH RESPONSE CONTRACTS", jEnd = "// END STRICT SEARCH RESPONSE CONTRACTS";
assert.equal(source.split(jStart).length, 2); assert.equal(source.split(jEnd).length, 2);
const jsonContracts = {}; vm.runInNewContext(source.slice(source.indexOf(jStart), source.indexOf(jEnd) + jEnd.length), jsonContracts);
const doc = {doc_id: "id", title: null, publication_author: null, source_file_count: 1, date_display: ["1967"], authors: [], tag_names: [], snippet: []};
const search = {results: [doc], total: 1, page: 1, page_size: 20, indexing: false, index_status: "ready"};
assert.equal(jsonContracts.isSearchResponse(search), true);
assert.equal(jsonContracts.isBootstrapResponse({generation: "abc", search, sources: {archives: [{name: "1", value: 1, count: 1}]}}), true);
for (const invalid of [null, {...search, page: 0}, {...search, results: [{...doc, authors: [42]}]},
  {...search, results: [{...doc, source_file_count: -1}]}, {...search, indexing: "false"}]) {
  assert.throws(() => jsonContracts.parseSearchResponse(invalid));
}
assert.equal(jsonContracts.isBootstrapResponse({generation: "abc", search, sources: {years: [{count: -1}]}}), false);
const page = {items: [{name: "标签", value: "标签", count: 1}], total: 1, page: 1, page_size: 200, has_more: false};
assert.equal(jsonContracts.isFacetPage(page), true);
assert.equal(jsonContracts.isFacetPage({...page, items: [{name: "x", count: -2}]}), false);
assert.equal(jsonContracts.isFacetPage({...page, has_more: 1}), false);
assert.equal(jsonContracts.isBootstrapFacetsResponse({generation: "x", facets: {source: page, author: page, tag: page}}), true);
assert.equal(jsonContracts.isBootstrapFacetsResponse({generation: "x", facets: {source: page, author: page}}), false);
assert.equal(jsonContracts.isFacetSourcesResponse({sources: [], authors: [], tags: [], archives: [], types: [], years: []}), true);
const queueItem={doc_id:'id',archive_id:25,title:'待拆分',publication_name:'刊物',page_start:1,page_end:2,date_display:[],authors:[],source_files:[]};
const queue={total:1,page:1,page_size:20,items:[queueItem]};
assert.equal(jsonContracts.isParseQueueResponse(queue),true);
assert.equal(jsonContracts.isParseQueueResponse({...queue,items:[{...queueItem,source_files:[3]}]}),false);
assert.equal(jsonContracts.isParseQueueResponse({...queue,items:[{...queueItem,doc_id:2}]}),false);
const prStart = "// BEGIN STRICT PREVIEW RESPONSE CONTRACTS", prEnd = "// END STRICT PREVIEW RESPONSE CONTRACTS";
assert.equal(source.split(prStart).length, 2); assert.equal(source.split(prEnd).length, 2);
vm.runInNewContext(source.slice(source.indexOf(prStart), source.indexOf(prEnd) + prEnd.length), jsonContracts);
const preview = {doc_id:"id",content:"正文",title:null,source_file_count:1,authors:[],tag_names:[],date_display:[],
  source_files:[{index:0,name:"原稿.pdf",url:"https://example.test/source.pdf"}],variants:[],article:{}};
assert.equal(jsonContracts.isPreviewResponse(preview),true);
assert.equal(jsonContracts.parsePreviewResponse(preview,"id").content,"正文");
for (const invalid of [{...preview, content: []}, {...preview, source_files:[{index:-1,name:"x",url:"x"}]},
  {...preview, source_files:[{index:0,name:"x",url:"x",initial_page:{}}]},
  {...preview, source_files:[{index:0,name:"x",url:"x",pages:[42]}]},
  {...preview, source_files:[{index:0,name:"x",url:"x",page_numbers:[-1]}]},
  {...preview, source_files:[{index:0,name:"x",url:"x",page_count:0}]},
  {...preview, variants:[{doc_id:0,publication_name:"版本"}]}, {...preview, article:[]}]) {
  assert.throws(() => jsonContracts.parsePreviewResponse(invalid,"id"));
}
assert.throws(() => jsonContracts.parsePreviewResponse(preview,"other"));
const dStart = "// BEGIN STRICT COMPARISON DIFF", dEnd = "// END STRICT COMPARISON DIFF";
assert.equal(source.split(dStart).length, 2); assert.equal(source.split(dEnd).length, 2);
const diffSandbox = {}; vm.runInNewContext(section + "\n" + source.slice(source.indexOf(dStart), source.indexOf(dEnd) + dEnd.length), diffSandbox);
const compared = diffSandbox.diffHTML("共同\n<script>旧</script>", "共同\n新&");
assert.match(compared.left, /diff-del/);
assert.match(compared.left, /&lt;script&gt;旧&lt;\/script&gt;/);
assert.match(compared.right, /diff-add/);
assert.match(compared.right, /新&amp;/);
assert.doesNotMatch(compared.left, /<script>/);
const huge = "一\n".repeat(1201) + "<b>";
assert.equal(diffSandbox.diffHTML(huge, "").left, diffSandbox.escapeHTML(huge));
const deltaStart = "// BEGIN STRICT APPLY TEXT DELTA", deltaEnd = "// END STRICT APPLY TEXT DELTA";
assert.equal(source.split(deltaStart).length, 2); assert.equal(source.split(deltaEnd).length, 2);
const deltaSandbox = {}; vm.runInNewContext(source.slice(source.indexOf(deltaStart), source.indexOf(deltaEnd) + deltaEnd.length), deltaSandbox);
assert.equal(deltaSandbox.applyTextDelta("甲😀乙", "=1\t-2\t+𠮷%25%09\t=1"), "甲𠮷%\t乙");
assert.equal(deltaSandbox.applyTextDelta("甲乙", "=1\t+%E6%96%B0\t=1"), "甲新乙");
assert.throws(() => deltaSandbox.applyTextDelta("甲乙", "=1"), /delta length mismatch/);
const createStart = "// BEGIN STRICT CREATE TEXT DELTA", createEnd = "// END STRICT CREATE TEXT DELTA";
assert.equal(source.split(createStart).length, 2); assert.equal(source.split(createEnd).length, 2);
const generator = {window: {}}; vm.runInNewContext(source.slice(source.indexOf(createStart), source.indexOf(createEnd) + createEnd.length), generator);
for (const [oldText, newText] of [["甲😀乙", "甲😀校乙"], ["旧文", "新%\t文"], ["甲乙", ""], ["", "𠮷"]]) {
  const delta = generator.textDelta(oldText, newText);
  assert.equal(deltaSandbox.applyTextDelta(oldText, delta), newText);
}
generator.window.diff_match_patch = class { diff_main() { return [[0, "甲"], [-1, "乙"], [1, "𠮷%\t"]]; } };
assert.equal(deltaSandbox.applyTextDelta("甲乙", generator.textDelta("甲乙", "甲𠮷%\t")), "甲𠮷%\t");
const cStart = "// BEGIN STRICT PREVIEW CACHE", cEnd = "// END STRICT PREVIEW CACHE";
assert.equal(source.split(cStart).length, 2); assert.equal(source.split(cEnd).length, 2);
const cacheSandbox = {Date: {now: () => 1000}};
vm.runInNewContext(`const previewCache=new Map();let previewCacheChars=0;const PREVIEW_CACHE_TTL=100, PREVIEW_CACHE_LIMIT=2, PREVIEW_CACHE_MAX_CHARS=10;\n` +
  source.slice(source.indexOf(cStart), source.indexOf(cEnd) + cEnd.length) + `
setCachedPreview('a',{content:'甲甲'});setCachedPreview('b',{content:'乙乙'});getCachedPreview('a');
setCachedPreview('c',{content:'丙丙'});
globalThis.afterLRU={a:getCachedPreview('a').content,b:getCachedPreview('b'),c:getCachedPreview('c').content,chars:previewCacheChars};
setCachedPreview('a',{content:'丁丁丁'});globalThis.afterReplace=previewCacheChars;
setCachedPreview('huge',{content:'x'.repeat(500001)});globalThis.oversize=getCachedPreview('huge');
Date.now=()=>1200;globalThis.expired=getCachedPreview('a');globalThis.afterExpiry=previewCacheChars;\n`, cacheSandbox);
assert.deepEqual(snapshot(cacheSandbox.afterLRU), {a:"甲甲",b:null,c:"丙丙",chars:4});
assert.equal(cacheSandbox.afterReplace, 5);
assert.equal(cacheSandbox.oversize, null);
assert.equal(cacheSandbox.expired, null);
assert.equal(cacheSandbox.afterExpiry, 2);
const dlStart = "// BEGIN STRICT PREVIEW DOWNLOAD LINKS", dlEnd = "// END STRICT PREVIEW DOWNLOAD LINKS";
assert.equal(source.split(dlStart).length, 2); assert.equal(source.split(dlEnd).length, 2);
const links = {}; vm.runInNewContext(source.slice(source.indexOf(dlStart), source.indexOf(dlEnd) + dlEnd.length), links);
const unsafeId = "文/😀?x=<y>&z=1";
assert.equal(links.previewDownloadHTML({doc_id:unsafeId,source_file_count:0}), "");
assert.match(links.previewDownloadHTML({doc_id:unsafeId,source_file_count:1}), /\/api\/download\/source\/.*%3F/);
assert.match(links.previewDownloadHTML({doc_id:unsafeId,source_file_count:2}), /\/api\/download\/source-zip\//);
assert.equal(links.sourceDownloadURL({doc_id:unsafeId}, "1&bad=2", true),
  "/api/preview/source/" + encodeURIComponent(unsafeId) + "?file=1%26bad%3D2&inline=true");
assert.equal(links.pageImageURL(unsafeId, 0, 2), "/api/preview/source-page/" + encodeURIComponent(unsafeId) + "?file=0&page=2");
const metaStart = "// BEGIN STRICT PREVIEW METADATA", metaEnd = "// END STRICT PREVIEW METADATA";
assert.equal(source.split(metaStart).length, 2); assert.equal(source.split(metaEnd).length, 2);
const metadata = {}; vm.runInNewContext(section + "\n" + source.slice(source.indexOf(metaStart), source.indexOf(metaEnd) + metaEnd.length), metadata);
assert.doesNotMatch(metadata.previewTitleHTML({title:"<img src=x>"}), /<img/);
const metaHtml = metadata.previewMetaHTML({publication_name:"<刊物>",authors:["作者&"],tag_names:Array.from({length:9},(_,i)=>i===0?"<标签>":"标签"+i)});
assert.match(metaHtml, /&lt;刊物&gt;/); assert.match(metaHtml, /作者&amp;/); assert.match(metaHtml, /&lt;标签&gt;/);
assert.match(metaHtml, /data-total-pages="2"/); assert.doesNotMatch(metaHtml, /<刊物>|<标签>/);
const pStart = "// BEGIN STRICT PROOFREAD COMPARISON", pEnd = "// END STRICT PROOFREAD COMPARISON";
assert.equal(source.split(pStart).length, 2); assert.equal(source.split(pEnd).length, 2);
const proofread = {}; vm.runInNewContext(source.slice(source.indexOf(pStart), source.indexOf(pEnd) + pEnd.length), proofread);
assert.equal(proofread.deepSame([{name:"甲",type:"主题",note:""}], [{type:"主题",name:"甲",note:null}]), true);
assert.equal(proofread.deepSame({name:"甲"}, {name:"乙"}), false);
assert.equal(proofread.deepSame([1,2], [2,1]), false);
const aStart = "// BEGIN STRICT PROOFREAD LINE ANCHORS", aEnd = "// END STRICT PROOFREAD LINE ANCHORS";
assert.equal(source.split(aStart).length, 2); assert.equal(source.split(aEnd).length, 2);
const anchors = {}; vm.runInNewContext(source.slice(source.indexOf(aStart), source.indexOf(aEnd) + aEnd.length), anchors);
assert.deepEqual(snapshot(anchors.matchingLineAnchors(["甲","乙","丙"],["甲","新","丙"])), [[0,0],[2,2]]);
assert.deepEqual(snapshot(anchors.matchingLineAnchors([], ["新"])), []);
const pvStart = "// BEGIN STRICT COMMENT PIVOTS", pvEnd = "// END STRICT COMMENT PIVOTS";
assert.equal(source.split(pvStart).length, 2); assert.equal(source.split(pvEnd).length, 2);
const pivots = {}; vm.runInNewContext(source.slice(source.indexOf(pvStart), source.indexOf(pvEnd) + pvEnd.length), pivots);
pivots.isJsonObject = jsonContracts.isJsonObject;
assert.equal(pivots.proofreadArticleParts({parts:[null]}), null);
assert.equal(pivots.proofreadArticleParts({parts:[],comment_pivots:[null]}), null);
assert.equal(pivots.proofreadArticleParts({parts:[{text:'甲'}],comment_pivots:[{part_idx:0,offset:1,index:2}]}).parts.length, 1);
assert.equal(pivots.partTextWithPivots({parts:[{text:"甲😀乙"}],comment_pivots:[{part_idx:0,offset:2,index:1}]}, 0), "甲😀〔1〕乙");
const pcStart = "// BEGIN STRICT PARSE COVERAGE", pcEnd = "// END STRICT PARSE COVERAGE";
assert.equal(source.split(pcStart).length, 2); assert.equal(source.split(pcEnd).length, 2);
const parse = {}; vm.runInNewContext(source.slice(source.indexOf(pcStart), source.indexOf(pcEnd) + pcEnd.length), parse);
const row = {title:"标题",content:"正文",base_part_count:1,page_start:1,page_end:2};
assert.equal(parse.parseCoverageError([row],2), "");
assert.match(parse.parseCoverageError([{...row,title:""}],2), /标题/);
assert.match(parse.parseCoverageError([{...row,content:"  "}],2), /正文/);
assert.match(parse.parseCoverageError([{...row,base_part_count:0}],2), /基线/);
assert.match(parse.parseCoverageError([{...row,page_start:0}],2), /页码范围/);
assert.match(parse.parseCoverageError([row],3), /3/);
const oStart = "// BEGIN STRICT PARSE OCR ROWS", oEnd = "// END STRICT PARSE OCR ROWS";
assert.equal(source.split(oStart).length, 2); assert.equal(source.split(oEnd).length, 2);
const ocr = {}; vm.runInNewContext(source.slice(source.indexOf(oStart), source.indexOf(oEnd) + oEnd.length), ocr);
ocr.isJsonObject = jsonContracts.isJsonObject;
const ocrPage = {dimensions:{width:1000,height:1000},ocr_results:[
  {text:"正文",box:[[400,450],[600,550]]}, {text:"页眉",box:[[400,20],[600,80]]}, {text:"无坐标"}]};
assert.equal(ocr.parseOCRText(ocrPage,[0.2,0,0,0]), "无坐标\n正文");
assert.equal(ocr.parseOCRText({ocr_results:[]}), "");
assert.equal(ocr.parseOCRText({ocr_results:[{text:"空坐标",box:[]},{text:"无坐标"},{text:"正常",box:[[3,4]]}]}), "空坐标\n无坐标\n正常");
assert.equal(ocr.isParseOCRPage({ocr_results:[{text:"空坐标",box:[]}]}), true);
assert.equal(ocr.isParseOCRPage(ocrPage), true);
assert.equal(ocr.isParseOCRPage({ocr_results:[{text:'x',box:[null]}]}), false);
assert.equal(ocr.isParseOCRPage({ocr_results:[{text:'x',box:[[1,'bad']]}]}), false);
assert.equal(ocr.isParseOCRPage({dimensions:{width:-1},ocr_results:[]}), false);
console.log("BHA date, search, PDF, cache, proofread, deltas, pivots and parse coverage passed");
