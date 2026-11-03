"""Read legacy static menu data and HTML contents pages without running scripts."""
import posixpath
import re
import esprima
from lxml import html


def literal(node):
    if node.type == 'Literal' and isinstance(node.value, (str, int, float, bool)):
        return node.value
    if node.type == 'ArrayExpression':
        return [literal(n) for n in node.elements]
    if node.type == 'ObjectExpression':
        result = {}
        for prop in node.properties:
            if prop.type != 'Property' or prop.computed or prop.kind != 'init':
                raise ValueError('nonliteral menu property')
            key = prop.key.name if prop.key.type == 'Identifier' else literal(prop.key)
            if key in result:
                raise ValueError('duplicate menu property')
            result[key] = literal(prop.value)
        return result
    raise ValueError('nonliteral CHM data')


def declarations(text):
    """Only top-level literal declarations; source calls are never evaluated."""
    tree = esprima.parseScript(text)
    values = {}
    for statement in tree.body:
        if statement.type == 'VariableDeclaration':
            for declaration in statement.declarations:
                if declaration.id.type == 'Identifier' and declaration.init is not None:
                    try:
                        values[declaration.id.name] = literal(declaration.init)
                    except ValueError:
                        pass
    return values


def script_page(text, allow_writes=False):
    if 'makechm_a_content' not in text and not allow_writes:
        return None
    values = declarations(text)
    body = values.get('makechm_a_content')
    def string(expr):
        if expr.type == 'Identifier' and isinstance(values.get(expr.name), str):
            return values[expr.name]
        if expr.type == 'BinaryExpression' and expr.operator == '+':
            return string(expr.left)+string(expr.right)
        value=literal(expr)
        if not isinstance(value,str):
            raise ValueError('non-string chapter write')
        return value
    writes=[]
    for statement in esprima.parseScript(text).body:
        if statement.type != 'ExpressionStatement':continue
        call=statement.expression
        if call.type!='CallExpression':continue
        callee=call.callee
        if callee.type=='MemberExpression' and not callee.computed and callee.object.type=='Identifier' and callee.object.name=='document' and callee.property.name in {'write','writeln'}:
            writes.append(''.join(string(a) for a in call.arguments))
    if writes:
        return values.get('makechm_a_title', ''), ''.join(writes)
    if isinstance(body,str):
        raise ValueError('script chapter has no supported content writes')
    return None


def navigation(root, paths, decode, label):
    def node(title, source='', children=None, fragment=''):
        return {'label':label(title), 'href':source + ('#'+fragment if fragment else ''),
                'source':source or None, 'fragment':fragment,
                'resolution':'exact' if source else 'group', 'children':children or []}
    def plain(value):
        return label(html.fragment_fromstring(value or '<span/>', create_parent='div').text_content())
    def result(nodes, evidence, excluded=None):
        return {'hhc':None, 'hhc_files':[], 'nodes':nodes,
                'navigation_evidence':evidence, 'nonlinear_source_pages':excluded or []}
    for filename in sorted(paths.names):
        if posixpath.basename(filename).lower() not in {'page.js','data.js'}:
            continue
        text = decode(root/filename)
        if not re.search(r'\bpages\s*\[', text):
            continue
        records = []
        for statement in esprima.parseScript(text).body:
            if statement.type != 'ExpressionStatement':
                continue
            expr = statement.expression
            if expr.type != 'AssignmentExpression' or expr.operator != '=':
                continue
            left = expr.left
            if left.type != 'MemberExpression' or left.object.type != 'Identifier' or left.object.name != 'pages' or not left.computed:
                continue
            records.append(literal(expr.right))
        if not records:
            continue
        nodes = []; current = nodes
        if all(isinstance(r, list) and len(r) >= 2 for r in records):
            for record in records:
                source, _, state = paths.resolve(filename, '../txt/'+record[0]+'.txt')
                if not source:
                    raise ValueError('static menu chapter missing: '+record[0])
                if len(record)>3 and plain(record[3]):
                    group = node(plain(record[3])); nodes.append(group); current = group['children']
                title = plain(record[1])
                if '<' in record[1] and len(title)>100:
                    title = '内容简介'
                current.append(node(title, source))
        elif all(isinstance(r, dict) for r in records):
            def convert(record):
                source, _, state = paths.resolve(filename, '../txt/'+record['id']+'.js')
                if not source:
                    raise ValueError('static menu chapter missing: '+record['id'])
                return node(record['title'], source, [convert(c) for c in record.get('child',[])])
            nodes = [convert(r) for r in records]
            listed = set()
            def collect(entries):
                for n in entries:
                    listed.add(n['source']); collect(n['children'])
            collect(nodes)
            for name in sorted(paths.names):
                if name.startswith('txt/') and name.endswith('.js') and name not in listed:
                    page = script_page(decode(root/name), allow_writes=True)
                    if page:
                        nodes.append(node(page[0] or '内容简介', name))
        else:
            raise ValueError('mixed static menu records')
        # These HTML files are the inspected template engine's chapter/volume/
        # readall views. Keep their bytes available, but read the actual chapters.
        excluded = [n for n in paths.names if n.lower().endswith(('.htm','.html'))
                    and re.search(r'<script\b', decode(root/n), re.I)]
        return result(nodes, {'kind':'static_menu_literals','file':filename}, sorted(excluded))
    numbered = {int(m.group(1)):name for name in paths.names
                if (m:=re.fullmatch(r'page_(\d+)\.htm',name))}
    if numbered and 1 in numbered:
        match = re.search(r'\bvar\s+g_nTotal\s*=\s*(\d+)\s*;',decode(root/numbered[1]))
        if match and set(numbered)==set(range(1,int(match.group(1))+1)):
            return result([node('第 '+str(i)+' 页',numbered[i]) for i in sorted(numbered)],
                          {'kind':'numbered_pages','file':numbered[1],'total':len(numbered)},
                          [n for n in ['main.htm','music.htm'] if n in paths.names])
    controls = re.compile(r'^(?:上一[页章节]|下一[页章节]|回目录|目录页|总目录|返回.*|回封面|首页|尾页)$')
    candidates = []
    for name in sorted(paths.names):
        if not name.lower().endswith(('.htm','.html','.xhtml')):
            continue
        try:
            doc = html.document_fromstring(decode(root/name) or '<html/>')
        except (ValueError, html.etree.ParserError):
            continue
        links = []
        for a in doc.xpath('//a[@href]'):
            title = label(a.text_content())
            source, fragment, state = paths.resolve(name,a.get('href'))
            if title and not controls.fullmatch(title) and source and source!=name and source.lower().endswith(('.htm','.html','.xhtml','.txt')):
                links.append((a, node(title,source,fragment=fragment)))
        unique = len({n['source'] for a,n in links})
        if unique>=4:
            candidates.append((unique,name,doc,links))
    if not candidates:
        return result([], {'kind':'unavailable'})
    candidates.sort(key=lambda c:(-c[0],c[1]))
    _, name, doc, links = candidates[0]
    nodes = []; current = nodes; chapter = None
    by_element = {a:n for a,n in links}
    seen = set()
    # Source contents use centered, unlinked volume labels and linked chapters.
    for element in doc.iter():
        if element.tag in {'center','h1','h2','h3'} and not element.xpath('.//a'):
            title = label(element.text_content())
            if re.match(r'^(?:[上中下前后]\s*卷|第[一二三四五六七八九十百0-9]+[部篇卷])',title) and len(title)<150:
                group=node(title);nodes.append(group);current=group['children'];chapter=None
        if element not in by_element:
            continue
        n = by_element[element]; key=(n['label'],n['href'])
        if key in seen:
            continue
        seen.add(key)
        if re.match(r'^第[一二三四五六七八九十百0-9]+章',n['label']):
            current.append(n);chapter=n['children']
        elif chapter is not None and re.match(r'^(?:[一二三四五六七八九十]+[、\s]|\d+[．.、])',n['label']):
            chapter.append(n)
        else:
            current.append(n);chapter=None
    return result(nodes, {'kind':'html_contents','file':name,'unique_targets':candidates[0][0]})
